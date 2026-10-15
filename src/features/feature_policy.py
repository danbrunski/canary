"""Feature policy: which sourced channels may become trained features.

The static exclusions (GPS position, counters, tank level, RAM channels), the
:class:`FeaturePolicy` that adds the data-file gates (binding confidence,
serving capture profile, DID views) and :func:`candidate_feature_columns`,
the one place trainability policy is applied to a frame's columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import pandas as pd

from src.collection.capture_profiles import MODE01_AUTO
from src.collection.obd_registry import MISFIRE_COUNT_CHANNEL_PREFIX, MODE01_REGISTRY
from src.columns import MODE_COLUMN, base_channel, is_flag_column
from src.config import CanaryConfig
from src.decode.bindings import Bindings, Signal, is_trainable_binding
from src.decode.dids import MEMORY_SOURCE, DidMap, load_did_map
from src.decode.engineering import DECODED_PREFIX, repo_bindings
from src.features.feature_paths import DEFAULT_CAPTURE_PROFILES_PATH, DEFAULT_DIDS_PATH
from src.preprocessing.channel_units import channel_units_for
from src.preprocessing.drives import DRIVE_COLUMN
from src.preprocessing.features import CONTEXT_COLUMN_PREFIX
from src.signals.afr import TARGET_AFR_CHANNELS, load_axis_refusal
from src.signals.committees import non_trainable_committee_outputs
from src.signals.vehicle_signals import did_views
from src.trainability import source_exclusion_reason

#: GPS channels logged for trip reconstruction and data-quality context but
#: deliberately excluded from model features. Absolute position would teach the
#: detectors location identity; heading wraps at 360°; and fix-quality /
#: satellite-count / HDOP describe the receiver, not the vehicle. Since H01.1
#: the source gate (``src.trainability``) excludes *every* ``gps:`` channel,
#: ``gps:speed`` included (H00 operator decision 1: external sensors never
#: train or score the detectors; ``gps:speed`` still backs up the segmenter's
#: speed signal, which is mode labelling, not a feature). This name list is
#: kept as the named, older reason for these seven.
NON_TRAINABLE_GPS_CHANNELS: frozenset[str] = frozenset(
    {
        "gps:latitude",
        "gps:longitude",
        "gps:altitude",
        "gps:heading",
        "gps:fix_quality",
        "gps:num_satellites",
        "gps:hdop",
    }
)

#: Monotonic counters / odometers / run-clocks: they only ever increase (or
#: reset on a DTC clear / key cycle), so their absolute level and rolling
#: derivatives carry no anomaly signal — yet the LSTM can't reconstruct an
#: ever-rising ramp and flags it on every window (RUN_TIME/DISTANCE were the
#: top false-alarm contributors on a known-good drive), and they add noise to
#: the IF. Excluded from trained features but kept in the frame so behavior
#: models can still read them (e.g. ``obd:RUN_TIME`` is the coolant_warmup
#: time-since-start axis). Same mechanism as NON_TRAINABLE_GPS_CHANNELS.
#: ``derived:*`` trip integrals (Task 1.1/1.3) — cumulative sums over the
#: session (fuel used, distance, CO2, idle time, running average economy)
#: are monotonic-by-construction the same way ``obd:RUN_TIME`` is: real
#: anomaly information, zero, and the LSTM can't reconstruct an ever-rising
#: ramp. Excluded here for the same reason and by the same mechanism as the
#: OBD counters below, in case a future feature frame ever includes
#: ``compute_derived_channels``'s cumulative output (today only
#: ``compute_instantaneous_channels``'s row-local subset feeds
#: ``featurize_wide_frame`` — see there).
_NON_TRAINABLE_DERIVED_INTEGRALS: frozenset[str] = frozenset(
    {
        "derived:TRIP_DISTANCE_KM",
        "derived:FUEL_USED_L",
        "derived:CO2_KG",
        "derived:IDLE_TIME_S",
        "derived:AVG_ECONOMY_L_PER_100KM",
    }
)

#: Task 4.6 — ``derived:GEAR_ESTIMATE`` is a clustered/categorical label
#: (nearest known gear ratio), not a continuous physical residual like the
#: rest of ``compute_instantaneous_channels``'s output. It's redundant with
#: rpm+speed (already trained features) for anomaly purposes and a
#: stepwise/discrete signal would only add reconstruction noise to the IF
#: z-score scaler and the LSTM's continuous-signal assumption — explicit
#: model-trainability decision per the engine-analysis-and-mapping.md Phase
#: 4 acceptance checklist. Kept in the frame (same mechanism as the counters
#: below) so analysis/UI code can still read it.
_NON_TRAINABLE_CATEGORICAL_CHANNELS: frozenset[str] = frozenset(
    {
        "derived:GEAR_ESTIMATE",
    }
)

#: Per-cylinder misfire *counters* (Mode 06-style tallies surfaced as PIDs).
#: Same defect as the odometers below: a count that only accumulates until the
#: next DTC clear / key cycle. Its level says how long ago that was, and its
#: rolling derivatives are zero on every healthy tick — a misfire is caught by
#: the DTC/monitor path, not by teaching a forest what "0 forever" looks like.
#: Matched by the registry's channel prefix, not an enumerated cylinder list,
#: so a V8's CYL7/CYL8 are covered without anyone remembering to add them.
_NON_TRAINABLE_MISFIRE_COUNTER_PREFIX = f"obd:{MISFIRE_COUNT_CHANNEL_PREFIX}"

NON_TRAINABLE_COUNTER_CHANNELS: frozenset[str] = frozenset(
    {
        "obd:RUN_TIME",
        "obd:DISTANCE_SINCE_DTC_CLEAR",
        "obd:DISTANCE_W_MIL",
        "obd:WARMUPS_SINCE_DTC_CLEAR",
    }
    | _NON_TRAINABLE_DERIVED_INTEGRALS
    | _NON_TRAINABLE_CATEGORICAL_CHANNELS
)

#: Slow *state* of the vehicle rather than behaviour of the engine. The tank
#: level drifts over days, not within a drive: a corpus sees a handful of
#: distinct levels, so every fill-up (or every quarter-tank the corpus happened
#: not to contain) is "novel" to a detector while saying nothing about engine
#: health. Kept in the frame (trip/economy code reads it), never trained on —
#: same mechanism as the counters above.
NON_TRAINABLE_STATE_CHANNELS: frozenset[str] = frozenset(
    {
        "obd:FUEL_LEVEL",
    }
)

#: Bookkeeping columns every feature frame carries; never features, and never
#: judged by :class:`FeaturePolicy` (they name no source).
_STRUCTURAL_COLUMNS: frozenset[str] = frozenset({"session", "timestamp", MODE_COLUMN, DRIVE_COLUMN})

DID_PREFIX = "did:"

DBC_PREFIX = "dbc:"

OBD_PREFIX = "obd:"

DERIVED_PREFIX = "derived:"

#: Wide-column prefix of a service-0x23 memory channel: the poller emits
#: ``mem:<NAME>`` readings under source ``obd``.
MEMORY_FEATURE_PREFIX = f"{OBD_PREFIX}{MEMORY_SOURCE}:"

#: Why a memory channel is never a detector feature (B08.4, 2026-10-08). The
#: source gate passes it (its source is ``obd``) and no serving gate covered
#: it, because it is neither a Mode 01 PID nor a DID: the 2026-09-24 sequence
#: artifact learned ``obd:mem:ECM_ENGINE_SPEED_RAM`` and
#: ``obd:mem:ECM_IGNITION_TIMING_RAM`` from two drives that polled them,
#: scaled on one idle count (775 rpm, std 12.5), and the serving profile never
#: polled them. RAM channels are firmware-context evidence for the behavior and
#: twin work; behavior recipes still read them (``src.trainability`` passes
#: them), only the IF and sequence detectors do not.
MEMORY_CHANNEL_EXCLUSION = (
    "memory_channel: a service-0x23 RAM read is firmware-context evidence for "
    "behavior and twin work, not an IF/sequence detector feature (B08.4, 2026-10-08)"
)

#: Mode 01 PID channel names, the ones a capture profile's ``mode01`` selects.
_MODE01_CHANNELS = frozenset(d.channel.upper() for d in MODE01_REGISTRY.values())


def _is_mode01_channel(column: str) -> bool:
    return column.removeprefix(OBD_PREFIX).upper() in _MODE01_CHANNELS


#: Prefix of the standing exclusion of a vehicle DID view (P02.14).
DID_VIEW_EXCLUSION = "did_view"


def _did_view_channels() -> frozenset[str]:
    return frozenset(view.channel for view in did_views())


@dataclass(frozen=True)
class FeaturePolicy:
    """Which *sourced* channels may become trained features — resolved facts.

    The static exclusions (GPS position, counters, tank level) are constants in
    this module. These two depend on data files, so they are resolved once
    (:func:`resolve_feature_policy`) and carried here:

    ``bindings``
        The catalog ``decoded:`` columns were produced from. A ``decoded:``
        channel trains only if its binding passes
        :func:`src.decode.bindings.is_trainable_binding` — confidence
        ``confirmed``/``high`` and not opted out with ``model_feature: false``.
        ``None`` = no catalog could be loaded; then *no* ``decoded:`` channel
        trains, because an unverifiable byte is not evidence.

    ``serving_dids``
        Upper-cased DID names the serving (``default``) capture profile polls.
        A ``did:`` channel trains only if it is in here; one polled solely by a
        research capture profile is NaN for the model's whole serving life.
        ``None`` = unresolvable, and then no ``did:`` channel trains.

    ``serving_mode01``
        The same answer for Mode 01
        (:func:`~src.collection.capture_profiles.serving_mode01_channels`):
        upper-cased channel names, or ``AUTO`` for every supported PID. An
        ``obd:`` Mode 01 channel trains only if the serving profile polls it,
        and a ``derived:`` channel only if each of its required input groups
        has an input the serving profile supplies. The Xterra ``default``
        dropped ten PIDs on 2026-09-26 (D03.34) that the old corpus still
        carries. ``None`` = unresolvable, and then no Mode 01 channel trains.

    ``did_map``
        The DID catalog the frame was decoded with (P02.14 verification). The
        load-axis refusal and the DID-view exclusion read units from it, never
        from the repo file, so a frame decoded with another catalog is judged
        by that catalog. ``None`` = no DID catalog decoded the frame, and then
        every DID view and DID-backed load axis is refused. Required: a
        default would be the repo file, which is the mismatch this prevents.
    """

    bindings: Bindings | None
    serving_dids: frozenset[str] | None
    serving_mode01: frozenset[str] | None
    did_map: DidMap | None = field(hash=False)

    @cached_property
    def _signals_by_name(self) -> dict[str, Signal]:
        # reconcile_feature_frame_to_spec consults this per serve tick.
        return {} if self.bindings is None else {sig.name: sig for sig in self.bindings.signals}

    def exclusion_reason(self, base: str) -> str | None:
        """Why base channel ``base`` may not train, or ``None`` if it may.

        The source gate comes first (H01.1, :func:`src.trainability.source_exclusion_reason`):
        a channel whose origin is not on the ECU-origin allowlist — ``gps:``,
        ``imu:``, ``sensor:``, a ``derived:`` channel computed from one, an
        undeclared ``derived:`` channel, an unclassified namespace — is
        refused as ``source_not_trainable: ...`` before any data-file gate.
        Enforced at *training*: a deployed artifact that already carries such
        a feature keeps being served it (the serve-side reconciler aligns to
        the manifest, never to this policy); the load audit names it as a
        policy violation until the retrain replaces it.
        """
        # P02.13 first: the source gate also refuses TARGET_AFR while its load
        # input group is empty, and the load-axis reason is the one that says
        # why. P02.14's DID views are refused by name here too.
        if base in TARGET_AFR_CHANNELS or base in _did_view_channels():
            standing = self.standing_exclusions().get(base)
            if standing is not None:
                return standing
        source_reason = source_exclusion_reason(base)
        if source_reason is not None:
            return source_reason
        if base.startswith(MEMORY_FEATURE_PREFIX):
            return MEMORY_CHANNEL_EXCLUSION
        if base.startswith(DECODED_PREFIX) or base.startswith(DBC_PREFIX):
            signal = self._signals_by_name.get(base.split(":", 1)[1])
            if signal is None:
                if base.startswith(DBC_PREFIX):
                    # A genuinely external DBC signal: no binding to judge it by,
                    # and none needed — it never came from bindings.yaml.
                    return None
                return (
                    "no bindings catalog loaded"
                    if self.bindings is None
                    else "not in the bindings catalog"
                )
            if not signal.model_feature:
                return "binding opted out (model_feature: false)"
            if not is_trainable_binding(signal):
                return f"binding confidence {signal.confidence!r} is below 'high'"
            return None
        if base.startswith(DID_PREFIX):
            if self.serving_dids is None:
                return "serving capture profile unresolvable"
            if base.removeprefix(DID_PREFIX).upper() not in self.serving_dids:
                return "not polled by the serving (default) capture profile"
        if base.startswith(OBD_PREFIX) and _is_mode01_channel(base):
            if self.serving_mode01 is None:
                return "serving capture profile unresolvable"
            if not self._served(base, frozenset()):
                return "not polled by the serving (default) capture profile"
        if base.startswith(DERIVED_PREFIX):
            missing = self._unserved_group(base, frozenset())
            if missing is not None:
                return (
                    f"needs one of {', '.join(missing)}, which the serving (default) "
                    "capture profile does not poll"
                )
        return None

    def standing_exclusions(self) -> dict[str, str]:
        """``{channel: reason}`` the policy refuses by name, whether or not a
        frame carries the channel.

        P02.13 (operator decision 2026-09-27): ``derived:TARGET_AFR`` and
        ``derived:AFR_ERROR_B*`` stay out of detector features until a channel
        is verified to report the target map's load axis in its unit. They
        were looked up with calculated load (%) on a base-fuel-schedule axis
        (ms). The refusal lifts itself once a vehicle declares that channel
        (``src.signals.vehicle_signals`` ``LOAD_AXIS_CHANNELS``) whose catalog
        unit is the axis unit (:mod:`src.preprocessing.channel_units`). Recorded on
        every manifest (:func:`build_feature_spec`), so an artifact answers
        why the channels are absent even from a frame that never had them.

        P02.14: each vehicle DID view (``derived:BASE_FUEL_SCHEDULE`` on the
        Xterra) is its source DID times a constant. The source is already a
        candidate, so the view would enter the detectors as a second copy of
        one reading. It is refused by name, :data:`DID_VIEW_EXCLUSION`.
        """
        exclusions = {
            view.channel: (
                f"{DID_VIEW_EXCLUSION}: {view.channel} is {view.source} times a constant "
                f"(dids.yaml evidence {view.scale_method!r}); the source is the feature, and "
                "the view would be a second copy of one reading (P02.14)"
            )
            for view in did_views()
        }
        axis_refusal = load_axis_refusal(channel_units=channel_units_for(self.did_map))
        if axis_refusal is not None:
            exclusions.update(dict.fromkeys(TARGET_AFR_CHANNELS, axis_refusal))
        return exclusions

    def _served(self, column: str, seen: frozenset[str]) -> bool:
        """Will the serving profile supply ``column``? Namespaces other than
        polled Mode 01, Mode 22 and derived ones are not this gate's to judge,
        except a memory channel: never a detector input, so a derived channel
        needing one is never served to a detector either."""
        if column.startswith(MEMORY_FEATURE_PREFIX):
            return False
        if column.startswith(OBD_PREFIX) and _is_mode01_channel(column):
            if self.serving_mode01 is None:
                return False
            name = column.removeprefix(OBD_PREFIX).upper()
            return MODE01_AUTO in self.serving_mode01 or name in self.serving_mode01
        if column.startswith(DID_PREFIX):
            dids = self.serving_dids
            return dids is not None and column.removeprefix(DID_PREFIX).upper() in dids
        if column.startswith(DERIVED_PREFIX):
            return self._unserved_group(column, seen) is None
        return True

    def _unserved_group(self, channel: str, seen: frozenset[str]) -> tuple[str, ...] | None:
        """The first required input group of derived ``channel`` with no served
        input, or ``None``. Undeclared channels are the source gate's business."""
        from src.signals.derived import DERIVED_CHANNEL_INPUTS

        groups = DERIVED_CHANNEL_INPUTS.get(channel)
        if groups is None or channel in seen:
            return None
        nested = seen | {channel}
        for group in groups:
            if not any(self._served(column, nested) for column in group):
                return group
        return None


#: ``{path: ((mtime_ns, size), DidMap)}`` for :func:`_did_map_at`.
_DID_MAP_CACHE: dict[Path, tuple[tuple[int, int], DidMap]] = {}


def _did_map_at(path: Path) -> DidMap | None:
    """The DID catalog at ``path``, re-read when its mtime or size changes;
    ``None`` when absent. :func:`repo_bindings`' pattern for the other catalog.

    :func:`reconcile_feature_frame_to_spec` resolves the default
    :class:`FeaturePolicy` on every serve tick, once per detector, and
    re-parsing ``dids.yaml`` there was ~300 ms of its ~400 ms on the laptop
    (P02.9 measurement, 2026-10-05).
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _DID_MAP_CACHE.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    did_map = load_did_map(path)
    _DID_MAP_CACHE[path] = (stamp, did_map)
    return did_map


def resolve_feature_policy(
    config: CanaryConfig | None = None,
    *,
    profile_slug: str | None = None,
    bindings: Bindings | None = None,
    bindings_path: Path | None = None,
    dids_path: Path = DEFAULT_DIDS_PATH,
) -> FeaturePolicy:
    """The :class:`FeaturePolicy` for a training/selection run.

    Bindings resolve the way :mod:`src.decode.engineering` does it: an explicit
    catalog when threaded, else ``bindings_path``, else the repo catalog.

    The DID catalog is ``dids_path`` (``None`` in the policy when absent), the
    same default :func:`build_feature_frame` decodes with: a caller decoding
    with another catalog passes that path here too.

    The serving DID set comes from the ``default`` capture profile — the saved
    catalog at the (vehicle-scoped, when profiles are enabled) configured path,
    or what that catalog would be seeded with (``collection.obd_dids``). With no
    ``config`` it falls back to the repo's ``data/capture_profiles.yaml``.
    """
    from src.collection.capture_profiles import (
        CaptureProfileSources,
        serving_mode01_channels,
        serving_mode22_dids,
    )

    # No config: the starter catalog alone, with no machine's overrides.
    starter_only = CaptureProfileSources(DEFAULT_CAPTURE_PROFILES_PATH, None)

    catalog = bindings
    if catalog is None:
        catalog = (
            repo_bindings(bindings_path.resolve()) if bindings_path is not None else repo_bindings()
        )
    did_map = _did_map_at(dids_path)
    if config is None:
        return FeaturePolicy(
            bindings=catalog,
            serving_dids=serving_mode22_dids(starter_only),
            serving_mode01=serving_mode01_channels(starter_only),
            did_map=did_map,
        )
    scoped = config
    if config.profiles.enabled:
        from src.vehicle.profiles import resolve_profile_for_tooling, scope_config

        scoped = scope_config(config, resolve_profile_for_tooling(config, profile_slug))
    return FeaturePolicy(
        bindings=catalog,
        serving_dids=serving_mode22_dids(
            CaptureProfileSources.of(scoped.daemon), scoped.collection
        ),
        serving_mode01=serving_mode01_channels(
            CaptureProfileSources.of(scoped.daemon), scoped.collection
        ),
        did_map=did_map,
    )


def feature_policy_exclusions(
    features: pd.DataFrame, policy: FeaturePolicy | None = None
) -> dict[str, str]:
    """``{base channel: reason}`` for channels :class:`FeaturePolicy` keeps out.

    Recorded in the manifest's feature spec so "why is ``did:X`` not a feature"
    is answered by the artifact instead of re-derived from the data files as
    they happen to look later.
    """
    resolved = policy if policy is not None else resolve_feature_policy()
    out: dict[str, str] = {}
    for raw in features.columns:
        column = str(raw)
        if (
            column in _STRUCTURAL_COLUMNS
            or is_flag_column(column)
            or column.startswith(CONTEXT_COLUMN_PREFIX)
        ):
            continue
        base = base_channel(column)
        if base in out:
            continue
        reason = resolved.exclusion_reason(base)
        if reason is not None:
            out[base] = reason
    return dict(sorted(out.items()))


def candidate_feature_columns(
    features: pd.DataFrame, *, policy: FeaturePolicy | None = None
) -> list[str]:
    """Numeric, non-structural feature candidates before all-NaN filtering.

    The one place trainability policy lives; every selector
    (:func:`trainable_feature_columns`, the IF and sequence variants,
    :func:`build_feature_spec`) goes through it. ``policy=None`` resolves the
    repo defaults — the gates cannot be skipped by not passing an argument.
    """
    resolved_policy = policy if policy is not None else resolve_feature_policy()
    skip_exact = _STRUCTURAL_COLUMNS
    non_trainable_committees = non_trainable_committee_outputs()
    # ``dbc:*`` signals that also exist as ``decoded:*`` are the same wire
    # bytes decoded twice (data/dbc/xterra_2008.dbc was *emitted from*
    # bindings.yaml). Training both double-weights those signals in the IF
    # and doubles their share of sequence reconstruction loss for zero new
    # information — keep the ``decoded:*`` copy, drop the duplicate.
    # Genuinely external DBC signals (no bindings counterpart) still train.
    decoded_signal_names = {
        base_channel(str(c)).removeprefix("decoded:")
        for c in features.columns
        if str(c).startswith("decoded:")
    }
    cols: list[str] = []
    for raw in features.columns:
        col = str(raw)
        if col in skip_exact or is_flag_column(col):
            continue
        # Behavior serving context (B01.5) — eligibility bookkeeping, never
        # a learned feature.
        if col.startswith(CONTEXT_COLUMN_PREFIX):
            continue
        if (
            col.startswith("dbc:")
            and base_channel(col).removeprefix("dbc:") in decoded_signal_names
        ):
            continue
        # GPS position/quality channels (and their rolling derivatives) are
        # logged but never modeled — see NON_TRAINABLE_GPS_CHANNELS.
        if base_channel(col) in NON_TRAINABLE_GPS_CHANNELS:
            continue
        # Monotonic counters / odometers / run-clocks — see
        # NON_TRAINABLE_COUNTER_CHANNELS (kept in frame for behavior axes).
        base = base_channel(col)
        if base in NON_TRAINABLE_COUNTER_CHANNELS or base.startswith(
            _NON_TRAINABLE_MISFIRE_COUNTER_PREFIX
        ):
            continue
        # Slow vehicle state (tank level) — see NON_TRAINABLE_STATE_CHANNELS.
        if base in NON_TRAINABLE_STATE_CHANNELS:
            continue
        # Data-file gates: binding confidence / model_feature opt-out for
        # ``decoded:``, servability for ``did:`` — see FeaturePolicy.
        if resolved_policy.exclusion_reason(base) is not None:
            continue
        # Committee divergence residuals marked non-trainable: computed and
        # logged, but kept out of the trained set — see signals.committees.
        if base_channel(col) in non_trainable_committees:
            continue
        if pd.api.types.is_numeric_dtype(features[col]):
            cols.append(col)
    return cols
