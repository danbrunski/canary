"""Shared offline feature pipeline: recorded sessions → model-ready feature frame.

Both anomaly trainers (Isolation Forest and LSTM autoencoder) consume the same
preprocessing path: read recorded Parquet sessions, decode CAN payloads via
``bindings.yaml`` into numeric columns, pivot to wide, resample to a uniform
grid, segment operating modes (or fall back to a single ``"all"`` mode when no
OBD PIDs are present), and compute rolling features. Keeping it here means the
training CLIs stay thin and behave identically.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
from collections.abc import Mapping, Sequence
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.afr import append_afr_channels, load_afr_calibration
from src.analysis.committees import append_committee_residuals, non_trainable_committee_outputs
from src.analysis.derived import compute_anomaly_signals, compute_instantaneous_channels
from src.analysis.drives import DRIVE_COLUMN, stamp_physical_drives
from src.collection.obd_registry import MISFIRE_COUNT_CHANNEL_PREFIX
from src.config import CanaryConfig
from src.decode.behavior_decoder import append_deviation_columns, deviation_model_name
from src.decode.behavior_models import (
    DEFAULT_BEHAVIOR_MODELS_PATH,
    BehaviorModels,
    artifact_fingerprint,
    load_behavior_models,
)
from src.decode.bindings import Bindings, Signal, is_trainable_binding, load_bindings
from src.decode.dbc import DEFAULT_DBC_DIR, decode_can_rows_dbc, load_dbc_dir
from src.decode.dids import decode_did_rows, load_did_map
from src.decode.engineering import DECODED_PREFIX, repo_bindings
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.preprocessing.decode import decode_can_rows
from src.preprocessing.feature_cache import (
    FEATURE_FRAME_CACHE_VERSION as FEATURE_FRAME_CACHE_VERSION,
)
from src.preprocessing.feature_cache import (
    _feature_frame_cache_metadata,
    _write_feature_frame_cache,
)
from src.preprocessing.feature_cache import (
    build_feature_frame_cache_key as build_feature_frame_cache_key,
)
from src.preprocessing.features import (
    CONTEXT_COLUMN_PREFIX,
    FeatureConfig,
    base_channel,
    compute_features,
    split_rolling,
)
from src.preprocessing.loader import read_session, to_wide
from src.preprocessing.quality_gate import apply_quality_gate
from src.preprocessing.resampler import GAP_SUFFIX, ResampleConfig, is_flag_column, resample_wide
from src.preprocessing.resampler import resample_config_for as resample_config_for
from src.preprocessing.segmenter import (
    MODE_COLUMN,
    UNKNOWN_MODE,
    SegmenterConfig,
    segment_operating_modes,
)
from src.telemetry import source_of
from src.trainability import source_exclusion_reason

log = logging.getLogger(__name__)

#: Default location of the hand-edited CAN signal bindings file -- the repo's
#: own, never the working directory's (R01.2).
DEFAULT_BINDINGS_PATH = BINDINGS_YAML

#: Default location of the Mode-22 DID map (data/dids.yaml), repo-rooted. Used
#: to decode raw ``DID_<hex>`` poll readings into named ``did:<NAME>`` signals.
DEFAULT_DIDS_PATH = DIDS_YAML

#: Default retrain-time cache for model-ready feature frames.
DEFAULT_FEATURE_CACHE_DIR = Path("data/cache/feature_frames")

#: Mode label used when no OBD PIDs are present and the threshold segmenter
#: can't run — everything becomes one mode and the model fits a single global
#: detector. Per-mode models materialize once OBD data exists.
ALL_MODE = "all"

#: OBD channels the operating-mode segmenter needs as inputs. ``coolant_temp``
#: is required for the cold-start / warm-idle split (see ``warmup`` /
#: ``idle`` modes in canary.example.yaml — without it the segmenter can't
#: tell a cold engine from a warm one and behavior models fit one surface
#: to both regimes, which is what caused the May 2026 R²=0.41 warmup mess.
#:
#: Deliberately NOT ``segmenter.DEFAULT_SIGNAL_COLUMNS``, despite the overlap:
#: ``have_inputs`` below requires *every* key here to resolve, and that table
#: carries optional signals (``imu:``) absent from all pre-Phase-2b data. Using
#: it would send every historical session to ``ALL_MODE``. This is the
#: *required* set; that one is the full candidate map. Keep the per-signal
#: candidate lists in step between them — a fallback added there and not here
#: silently does nothing on the training path.
SEGMENTER_SIGNAL_COLUMNS: dict[str, str | tuple[str, ...]] = {
    # Broadcast CAN sits last: ~98 Hz against Mode 01's 0.26-0.93 Hz, so it
    # fills the rows where the PID was stale rather than overriding it. See
    # segmenter.DEFAULT_SIGNAL_COLUMNS for the measured agreement and for why
    # those NaN rows mattered (they became the degenerate `unknown` mode).
    "rpm": ("obd:RPM", "decoded:ECM_RPM"),
    # OBD wheel-speed PID is the reliable primary; GPS is the back-stop when OBD
    # is absent, coalesced row-wise (see segmenter). GPS↔OBD disagreement is a
    # sensor-committee residual, not a coalesce winner.
    "speed": ("obd:SPEED", "decoded:WHEEL_SPEED_6F7_C", "gps:speed"),
    "throttle": "obd:THROTTLE_POS",
    "coolant_temp": ("obd:COOLANT_TEMP", "decoded:ECM_COOLANT_TEMP"),
}
#: Pre-R04.14 private name, kept for its importers.
_SEGMENTER_SIGNAL_COLUMNS = SEGMENTER_SIGNAL_COLUMNS


def segmenter_input_columns() -> tuple[str, ...]:
    """Every column :data:`SEGMENTER_SIGNAL_COLUMNS` names, flattened, in order,
    once each: what a bounded reader must load for mode labels to match the
    training path."""
    columns: list[str] = []
    for candidates in SEGMENTER_SIGNAL_COLUMNS.values():
        columns.extend((candidates,) if isinstance(candidates, str) else candidates)
    return tuple(dict.fromkeys(columns))


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

FEATURE_SPEC_VERSION = 1

#: Bookkeeping columns every feature frame carries; never features, and never
#: judged by :class:`FeaturePolicy` (they name no source).
_STRUCTURAL_COLUMNS: frozenset[str] = frozenset({"session", "timestamp", MODE_COLUMN, DRIVE_COLUMN})

#: Repo-root-anchored serving capture-profile catalog, used when no config is
#: threaded (same anchoring as ``engineering.DEFAULT_BINDINGS_PATH``).
DEFAULT_CAPTURE_PROFILES_PATH = Path(__file__).resolve().parents[2] / "data/capture_profiles.yaml"

DID_PREFIX = "did:"
DBC_PREFIX = "dbc:"


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
    """

    bindings: Bindings | None
    serving_dids: frozenset[str] | None

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
        source_reason = source_exclusion_reason(base)
        if source_reason is not None:
            return source_reason
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
        return None


def resolve_feature_policy(
    config: CanaryConfig | None = None,
    *,
    profile_slug: str | None = None,
    bindings: Bindings | None = None,
    bindings_path: Path | None = None,
) -> FeaturePolicy:
    """The :class:`FeaturePolicy` for a training/selection run.

    Bindings resolve the way :mod:`src.decode.engineering` does it: an explicit
    catalog when threaded, else ``bindings_path``, else the repo catalog.

    The serving DID set comes from the ``default`` capture profile — the saved
    catalog at the (vehicle-scoped, when profiles are enabled) configured path,
    or what that catalog would be seeded with (``collection.obd_dids``). With no
    ``config`` it falls back to the repo's ``data/capture_profiles.yaml``.
    """
    from src.collection.capture_profiles import serving_mode22_dids

    catalog = bindings
    if catalog is None:
        catalog = (
            repo_bindings(bindings_path.resolve()) if bindings_path is not None else repo_bindings()
        )
    if config is None:
        return FeaturePolicy(
            bindings=catalog, serving_dids=serving_mode22_dids(DEFAULT_CAPTURE_PROFILES_PATH)
        )
    scoped = config
    if config.profiles.enabled:
        from src.profiles import resolve_profile_for_tooling, scope_config

        scoped = scope_config(config, resolve_profile_for_tooling(config, profile_slug))
    return FeaturePolicy(
        bindings=catalog,
        serving_dids=serving_mode22_dids(scoped.daemon.capture_profiles, scoped.collection),
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


#: Worker-process state for the parallel feature build. Populated once per
#: process via :func:`_init_feature_worker` so the heavy decoder objects
#: (bindings, DBC database, DID map) are sent across the pool boundary one time
#: per worker — not pickled per submitted session.
_FEATURE_WORKER_STATE: dict[str, Any] = {}


def _init_feature_worker(
    bindings: Any,
    dbc_database: Any,
    did_map: Any,
    resample_config: ResampleConfig,
) -> None:
    _FEATURE_WORKER_STATE["bindings"] = bindings
    _FEATURE_WORKER_STATE["dbc"] = dbc_database
    _FEATURE_WORKER_STATE["dids"] = did_map
    _FEATURE_WORKER_STATE["resample"] = resample_config


#: Raw CAN rows fed through the decoders per chunk. Decoding is row-local (each
#: CAN row yields its own decoded rows; no cross-row state), so chunking is
#: exactly equivalent to decoding the frame in one pass — it only bounds peak
#: memory. Sized so one chunk plus its decoded output stays in the low hundreds
#: of MB at the ~2.2 signals/frame this vehicle's bindings+DBC produce.
_CAN_DECODE_CHUNK_ROWS = 250_000

#: Canonical long-frame ordering, matching ``read_session``'s own sort. The
#: chunked decode reassembles rows out of order, and ``to_wide``'s
#: ``aggfunc="last"`` is order-sensitive, so the pieces are re-sorted on this
#: key before the pivot.
_LONG_SORT_COLUMNS = ["session", "timestamp", "source", "channel"]


def _decode_long_frame(
    long: pd.DataFrame,
    bindings: Any,
    dbc_database: Any,
    did_map: Any,
) -> pd.DataFrame:
    """Decode one session's long frame, chunking the CAN decode.

    Raw CAN frames are ~99% of the rows and carry no numeric value (their bytes
    live in ``payload``); they exist only to be decoded into ``decoded:*`` /
    ``dbc:*`` rows. Decoding them in a single pass holds every raw row *and*
    every decoded row at once — on one dense 45-minute drive that is ~4.5M raw
    plus ~10M decoded object-dtype rows, which OOM-killed feature-build workers
    at ~7.6 GB RSS (2026-07-26, both the 4-worker and the 2-worker attempt).
    Decoding in chunks and keeping only each chunk's decoded output bounds peak
    memory by the chunk instead of the session.

    Equivalent to the old single-pass decode, not an approximation: both
    decoders emit one row per (CAN row × matching signal) with no dependence on
    neighbouring rows, and the result is re-sorted on ``_LONG_SORT_COLUMNS``
    before it reaches the order-sensitive pivot.
    """
    if long.empty or "source" not in long.columns:
        return long
    is_can = long["source"] == "can"
    # DID decoding reads Mode-22 ``DID_<hex>`` OBD rows, never CAN rows, so it
    # runs once on the non-CAN side rather than per chunk.
    rest = long.loc[~is_can]
    if did_map is not None:
        rest = decode_did_rows(rest, did_map)
    if bindings is None and dbc_database is None:
        return rest.sort_values(_LONG_SORT_COLUMNS, kind="stable")

    can_rows = long.loc[is_can]
    pieces: list[pd.DataFrame] = [rest]
    for start in range(0, len(can_rows), _CAN_DECODE_CHUNK_ROWS):
        chunk = can_rows.iloc[start : start + _CAN_DECODE_CHUNK_ROWS]
        if bindings is not None:
            chunk = decode_can_rows(chunk, bindings)
        if dbc_database is not None:
            chunk = decode_can_rows_dbc(chunk, dbc_database)
        # Keep only what the decode produced; the raw CAN rows that seeded it
        # are dead weight downstream (all-NaN pivot columns) and dropping them
        # here is what keeps the accumulated pieces small.
        decoded = chunk.loc[chunk["source"] != "can"]
        if not decoded.empty:
            pieces.append(decoded)
        del chunk, decoded
    combined = pd.concat(pieces, ignore_index=True) if len(pieces) > 1 else pieces[0]
    return combined.sort_values(_LONG_SORT_COLUMNS, kind="stable")


def _decode_session_to_wide(
    session_dir: Path,
    bindings: Any,
    dbc_database: Any,
    did_map: Any,
) -> pd.DataFrame:
    """parquet → decode → wide pivot, split out of
    :func:`_decode_session_to_resampled` (Task 1.2) so the quality gate's
    reject counts stay inspectable per-session (``scripts/disposition_report.py``)
    without duplicating the decode steps.
    """
    long = _decode_long_frame(read_session(session_dir), bindings, dbc_database, did_map)
    return to_wide(long, coerce_numeric=True)


def _decode_session_to_resampled(
    session_dir: Path,
    bindings: Any,
    dbc_database: Any,
    did_map: Any,
    resample_config: ResampleConfig,
) -> pd.DataFrame:
    """The per-session pipeline: parquet → decode → wide → reject gate → resample.

    Top-level (not nested) so :class:`ProcessPoolExecutor` can pickle it. The
    serial path also calls it directly, so there's exactly one implementation
    of the per-session work.

    The quality gate (Task 1.2) runs on the wide frame, before
    ``resample_wide`` — a physically-impossible cell must be NaN'd before the
    resampler's ffill/``__gap`` bookkeeping ever sees it, so a reject is
    indistinguishable from any other gap to every downstream consumer.
    """
    return resample_long_session(
        read_session(session_dir),
        bindings,
        dbc_database,
        did_map,
        resample_config,
        session_name=session_dir.name,
    )


def resample_long_session(
    long: pd.DataFrame,
    bindings: Any,
    dbc_database: Any,
    did_map: Any,
    resample_config: ResampleConfig,
    *,
    session_name: str,
) -> pd.DataFrame:
    """decode → wide → reject gate → resample for one session's long frame.

    The body of :func:`_decode_session_to_resampled`, taking the long frame
    instead of reading it, so a bounded reader (the behavior-validation
    evaluator reads only the CAN ids its decoded channels need) runs the
    identical decode/gate/resample chain rather than a copy of it.
    """
    wide = to_wide(_decode_long_frame(long, bindings, dbc_database, did_map), coerce_numeric=True)
    gated, quality = apply_quality_gate(wide)
    if quality.total_rejected:
        log.info(
            "quality gate rejected cells in session",
            extra={"session": session_name, "reject_counts": quality.reject_counts},
        )
    return resample_wide(gated, config=resample_config)


def _worker_process_session(
    session_dir: Path,
) -> tuple[str, pd.DataFrame | None, str | None]:
    """ProcessPool task body. Returns ``(session_name, frame_or_none, error_or_none)``
    so a single bad session is reported as a skip rather than killing the pool."""
    state = _FEATURE_WORKER_STATE
    try:
        frame = _decode_session_to_resampled(
            session_dir,
            state["bindings"],
            state["dbc"],
            state["dids"],
            state["resample"],
        )
        return session_dir.name, frame, None
    except Exception as exc:  # noqa: BLE001 — one bad session must not kill the corpus
        return session_dir.name, None, f"{type(exc).__name__}: {exc}"


#: Skip the process pool below this session count: per-session work is short
#: enough that a fork + IPC round-trip wipes out the parallel win. Above this,
#: the pool consistently beats serial wall time on our corpus.
_PARALLEL_SESSION_THRESHOLD = 3

#: Hard ceiling on auto-resolved workers, before the memory budget below trims
#: it further. Callers can pass an explicit ``max_workers`` to override both.
_PARALLEL_WORKER_CAP = 4

#: Share of ``MemAvailable`` the pool may claim. Deliberately well under half:
#: the parent process is concurrently accumulating every returned session frame
#: in ``resampled_parts`` (a full-corpus retrain parent has been observed at
#: 8 GB RSS), and it, not the workers, holds the final concatenated frame.
_PARALLEL_MEMORY_FRACTION = 0.35

#: Resident floor for one ``spawn`` worker before it touches data: a fresh
#: interpreter re-importing pandas/pyarrow/torch-free decode stack measures
#: ~130-200 MB on the dev laptop.
_WORKER_BASELINE_BYTES = 250 * 1024 * 1024

#: Peak working set per byte of session parquet, for the decode → long →
#: wide-pivot → resample chain. Measured on this corpus (2026-07-26,
#: ``resample_hz: 1``): 28.5 MB on disk → 487 MB peak (17×), 45.6 MB → 1360 MB
#: (30×). Rounded up to 40× because the expansion scales with a session's
#: *channel count and wall-clock span*, not just its byte count, and an
#: underestimate is what gets a worker OOM-killed.
_WORKER_BYTES_PER_DISK_BYTE = 40


def _available_memory_bytes() -> int | None:
    """Best-effort ``MemAvailable``, or ``None`` if the platform won't say.

    ``MemAvailable`` (not ``MemFree``) is the right number: it accounts for
    reclaimable page cache, which is most of what a just-finished parquet read
    leaves behind.
    """
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:  # non-Linux fallback: free pages only, no reclaimable cache
        return int(os.sysconf("SC_AVPHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, AttributeError):
        return None


def _largest_session_bytes(session_dirs: Sequence[Path]) -> int:
    """On-disk parquet bytes of the biggest session in the batch.

    Sizing off the max rather than the mean because ``pool.map`` hands sessions
    out in order: several of the largest drives can be in flight simultaneously,
    and it only takes one worker over the line to get the pool OOM-killed.
    """
    largest = 0
    for session_dir in session_dirs:
        try:
            total = sum(path.stat().st_size for path in session_dir.glob("*.parquet"))
        except OSError:
            continue
        largest = max(largest, total)
    return largest


def _resolve_worker_count(session_dirs: Sequence[Path], max_workers: int | None) -> int:
    """Pick a worker count that fits in RAM.

    A fixed cap is wrong on both ends: it OOM-kills a wide corpus on a loaded
    box (4 workers × multi-GB frames on a 15 GB laptop) and leaves cores idle on
    a small one. Derive it from what's actually free and what one worker is
    actually going to hold, and log the arithmetic so a surprising choice is
    inspectable rather than mysterious.
    """
    if max_workers is None:
        configured = os.environ.get("CANARY_FEATURE_WORKERS")
        if configured:
            try:
                max_workers = int(configured)
            except ValueError:
                log.warning("ignoring invalid CANARY_FEATURE_WORKERS=%r", configured)
    if max_workers is not None:
        return max(1, int(max_workers))

    ceiling = min(len(session_dirs), os.cpu_count() or 1, _PARALLEL_WORKER_CAP)
    if ceiling <= 1:
        return max(1, ceiling)

    available = _available_memory_bytes()
    if available is None:
        return ceiling

    per_worker = _WORKER_BASELINE_BYTES + _largest_session_bytes(session_dirs) * (
        _WORKER_BYTES_PER_DISK_BYTE
    )
    budget = int(available * _PARALLEL_MEMORY_FRACTION)
    affordable = max(1, budget // per_worker)
    workers = min(ceiling, affordable)
    log.info(
        "sized parallel feature build to available memory",
        extra={
            "workers": workers,
            "cpu_ceiling": ceiling,
            "available_mb": round(available / 1024 / 1024),
            "budget_mb": round(budget / 1024 / 1024),
            "per_worker_mb": round(per_worker / 1024 / 1024),
        },
    )
    return workers


def build_feature_frame(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    behavior_models_path: Path = DEFAULT_BEHAVIOR_MODELS_PATH,
    dbc_dir: Path = DEFAULT_DBC_DIR,
    dids_path: Path = DEFAULT_DIDS_PATH,
    cache_dir: Path | None = None,
    force_rebuild: bool = False,
    max_workers: int | None = None,
) -> pd.DataFrame:
    """Run the full preprocessing pipeline over ``session_dirs`` into a feature frame.

    ``read_session`` → ``decode_can_rows`` (bindings.yaml, if present) →
    ``decode_can_rows_dbc`` (any ``.dbc`` files under ``dbc_dir``, if present —
    Task 1.8 interop hook) → ``decode_did_rows`` (``dids_path``, if present) →
    ``to_wide(coerce_numeric=True)`` → ``resample_wide`` → operating-mode
    segment (or constant ``"all"`` mode) → ``append_deviation_columns``
    (behavior models, if present — Task 4.E.7) → ``compute_features``.

    Every catalog path defaults to the repo's own file (:mod:`src.paths`), never
    the working directory, and the cache key hashes exactly the files loaded
    (R01.2): a build started outside the repo root used to drop every ``did:``
    feature while its cache key hashed a different constant.

    The deviation columns land *before* ``compute_features`` so they pick up
    rolling mean/std/roc derivatives just like raw / decoded channels. The
    ``ZScoreScaler`` then normalizes them through ``trainable_feature_columns``
    without changes to either module.
    """
    session_dirs = [Path(session_dir) for session_dir in session_dirs]
    cache_path: Path | None = None
    cache_metadata: dict[str, Any] | None = None
    if cache_dir is not None:
        cache_metadata = _feature_frame_cache_metadata(
            session_dirs,
            config,
            bindings_path=bindings_path,
            behavior_models_path=behavior_models_path,
            dbc_dir=dbc_dir,
            dids_path=dids_path,
        )
        cache_path = Path(cache_dir) / f"{cache_metadata['cache_key']}.pkl"
        if not force_rebuild:
            cached = _load_cached(cache_path)
            if cached is not None:
                return _with_physical_drives(cached, session_dirs)

    inputs = _load_decode_inputs(
        bindings_path=bindings_path,
        behavior_models_path=behavior_models_path,
        dbc_dir=dbc_dir,
        dids_path=dids_path,
    )
    resampled = _resample_sessions(
        session_dirs, inputs, resample_config_for(config), max_workers=max_workers
    )
    features = featurize_wide_frame(
        resampled, config, behavior_models=inputs.behavior_models, bindings=inputs.bindings
    ).features
    if cache_path is not None and cache_metadata is not None:
        _write_feature_frame_cache(cache_path, features, cache_metadata)
    return _with_physical_drives(features, session_dirs)


def _load_cached(cache_path: Path) -> pd.DataFrame | None:
    """The cached feature frame at ``cache_path``, or ``None`` (absent or
    unreadable: a corrupt cache rebuilds, it never fails a retrain)."""
    if not cache_path.exists():
        return None
    try:
        cached = pd.read_pickle(cache_path)
        if not isinstance(cached, pd.DataFrame):
            raise TypeError(f"cached object is {type(cached).__name__}, not DataFrame")
    except Exception as exc:  # noqa: BLE001 - cache corruption should not fail retrain
        log.warning(
            "feature frame cache unreadable; rebuilding",
            extra={"cache": str(cache_path), "error": str(exc)},
        )
        return None
    log.info(
        "loaded feature frame cache",
        extra={"cache": str(cache_path), "rows": len(cached), "columns": len(cached.columns)},
    )
    return cached


@dataclass(frozen=True)
class _DecodeInputs:
    """The catalogs a feature build decodes with; ``None`` where the file is absent."""

    bindings: Bindings | None
    behavior_models: BehaviorModels | None
    dbc_database: Any
    did_map: Any


def _load_decode_inputs(
    *, bindings_path: Path, behavior_models_path: Path, dbc_dir: Path, dids_path: Path
) -> _DecodeInputs:
    return _DecodeInputs(
        bindings=load_bindings(bindings_path) if bindings_path.exists() else None,
        behavior_models=(
            load_behavior_models(behavior_models_path) if behavior_models_path.exists() else None
        ),
        dbc_database=load_dbc_dir(dbc_dir),
        did_map=load_did_map(dids_path) if dids_path.exists() else None,
    )


def _resample_sessions(
    session_dirs: Sequence[Path],
    inputs: _DecodeInputs,
    resample_config: ResampleConfig,
    *,
    max_workers: int | None,
) -> pd.DataFrame:
    """Decode → pivot → resample one session at a time, then concatenate.

    ``resample_wide`` and ``compute_features`` already group by session (no
    cross-session bleed), so per-session-then-concat is identical to
    concat-then-resample — but peak memory is one session's raw bus-rate pivot
    instead of all of them concatenated, which OOM-kills the Pi on a full
    corpus.

    Sessions are independent, so for >= ``_PARALLEL_SESSION_THRESHOLD`` inputs
    the per-session pipeline fans out across a ``ProcessPoolExecutor`` (one
    process per session, capped by CPU count and by the memory budget in
    ``_resolve_worker_count``). ``max_workers=1`` forces the serial path — kept
    exactly as it was for back-compat with tests and the Pi cold-start
    fall-back, where multiprocessing overhead would lose. An unreadable session
    is skipped and named; no readable session at all raises.
    """
    resampled_parts: list[pd.DataFrame] = []
    skipped: list[str] = []

    effective_workers = _resolve_worker_count(session_dirs, max_workers)
    parallel = effective_workers > 1 and len(session_dirs) >= _PARALLEL_SESSION_THRESHOLD

    def _build_serial() -> None:
        for session_dir in session_dirs:
            try:
                resampled_parts.append(
                    _decode_session_to_resampled(
                        session_dir,
                        inputs.bindings,
                        inputs.dbc_database,
                        inputs.did_map,
                        resample_config,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — one bad session must not kill the corpus
                skipped.append(session_dir.name)
                log.warning(
                    "skipping unreadable session",
                    extra={"session": str(session_dir), "error": f"{type(exc).__name__}: {exc}"},
                )

    if parallel:
        log.info(
            "building feature frame in parallel",
            extra={"sessions": len(session_dirs), "workers": effective_workers},
        )
        try:
            with ProcessPoolExecutor(
                max_workers=effective_workers,
                initializer=_init_feature_worker,
                initargs=(inputs.bindings, inputs.dbc_database, inputs.did_map, resample_config),
                # ``spawn`` re-imports the codebase per worker rather than
                # forking the parent's address space, which avoids the classic
                # fork-with-threads deadlock (numpy / torch keep background
                # threads alive). The extra ~0.5-1 s of worker startup is
                # rounded off by even a single-session feature build.
                mp_context=multiprocessing.get_context("spawn"),
            ) as pool:
                # ``map`` preserves order, so the concatenated frame is
                # identical to the serial run — no ordering drift between runs.
                for name, frame, err in pool.map(_worker_process_session, session_dirs):
                    if frame is not None:
                        resampled_parts.append(frame)
                    else:
                        skipped.append(name)
                        log.warning(
                            "skipping unreadable session",
                            extra={"session": name, "error": err},
                        )
        except BrokenExecutor as exc:
            # OOM-kill, segfault, or a worker dying mid-task all surface as
            # BrokenExecutor. Don't fail the retrain — fall back to the
            # serial path that's been in production all along. Partial
            # results from the pool are dropped (we can't trust the ordering
            # or completeness) and we restart fresh, serially.
            log.warning(
                "parallel feature build aborted (worker died); falling back to serial",
                extra={
                    "sessions": len(session_dirs),
                    "workers": effective_workers,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            resampled_parts.clear()
            skipped.clear()
            _build_serial()
    else:
        _build_serial()
    if not resampled_parts:
        raise ValueError(
            f"no readable sessions among the {len(session_dirs)} given "
            f"(all skipped: {', '.join(skipped) or 'none'})"
        )
    if skipped:
        log.warning(
            "feature frame built from %d/%d sessions; %d skipped: %s",
            len(resampled_parts),
            len(session_dirs),
            len(skipped),
            ", ".join(skipped),
        )
    resampled = pd.concat(resampled_parts, ignore_index=True)
    return drop_fully_stale_rows(resampled)


def _with_physical_drives(features: pd.DataFrame, session_dirs: Sequence[Path]) -> pd.DataFrame:
    """Label every row with its physical drive (``src.analysis.drives``).

    Stamped after the cache, never into it: which drive a row belongs to
    depends on the recordings beside it (captures bridge drives, quarantine
    moves recordings away), not only on the sessions this frame was built from.
    Every trainer's drive floor, validation split and count reads this label.
    """
    if features.empty or "session" not in features.columns:
        return features
    return stamp_physical_drives(features, session_dirs)


def drop_fully_stale_rows(resampled: pd.DataFrame) -> pd.DataFrame:
    """Drop resampled rows where *every* gap-flagged channel is stale.

    The resampler emits a row per second across a session's whole span and
    forward-fills values across data gaps, flagging each fast channel's
    staleness in a ``<channel>__gap`` column. A row with *all* gap flags set had
    no fresh observation that second — it's a synthesized fill (e.g. a session
    left open across a long power-off gap), not telemetry, and training on it
    poisons the affected mode (a 12 h gap forward-fills into tens of thousands
    of identical "warmup" rows). Rows with only *some* channels stale (a slow
    PID between updates) are real observations and kept. ``LiveFeatureSource.tick``
    (src/preprocessing/live.py) applies the same all-gap-flags-set rule per tick
    and returns ``None`` instead of emitting a row — dropping here moves training
    rows toward "seconds actually observed", which is what serve scores.
    """
    gap_cols = [c for c in resampled.columns if str(c).endswith(GAP_SUFFIX)]
    if not gap_cols or resampled.empty:
        return resampled
    fully_stale = resampled[gap_cols].fillna(True).all(axis=1)
    dropped = int(fully_stale.sum())
    if dropped:
        log.info(
            "dropped fully-stale (gap-filled) rows from training frame",
            extra={"dropped": dropped, "total": len(resampled)},
        )
    return resampled.loc[~fully_stale].reset_index(drop=True)


@dataclass(frozen=True)
class FeaturizedFrame:
    """Output of :func:`featurize_wide_frame`.

    ``features`` is the model-ready frame (rolling features appended).
    ``operating_frame`` is the *pre-feature* wide row set after segmentation
    and behavior-deviation columns but before ``compute_features`` — the live
    overlay reads its last row even during the feature-window warmup, so it is
    returned rather than recomputed.
    ``segmenter_fell_back`` (4.7) is ``True`` when :func:`segment_or_constant`
    used the constant ``"all"`` mode because the segmenter's *inputs* weren't
    present in this frame — every mode-gated surface silently never fires in
    that case. ``False`` both on normal segmentation and on an explicit
    ``preprocessing.single_operating_mode`` config (that's an intentional
    choice, not a diagnostic-worthy fallback).
    """

    features: pd.DataFrame
    operating_frame: pd.DataFrame
    segmenter_fell_back: bool = False


#: Pre-B02 private name, kept for existing importers.
_drop_fully_stale_rows = drop_fully_stale_rows


def featurize_wide_frame(
    wide: pd.DataFrame,
    config: CanaryConfig,
    *,
    behavior_models: BehaviorModels | None = None,
    feature_config: FeatureConfig | None = None,
    bindings: Bindings | None = None,
) -> FeaturizedFrame:
    """The single wide → model-ready feature transform (7.1 keystone).

    Both the offline batch path (:func:`build_feature_frame`, after
    read/decode/resample) and the online streaming path
    (``LiveFeatureSource.tick``, after observe/snapshot) call this so the
    schema-producing tail is *one* implementation, not two that must be kept
    byte-identical by hand. The head differs by necessity — batch reads and
    resamples a recorded session, streaming accumulates a current-value
    snapshot per tick — and the persisted feature spec plus
    :func:`reconcile_feature_frame_to_spec` are the contract that bridges any
    residual head divergence at serve time. This function is the part that
    must never diverge: ``segment_or_constant`` → ``append_deviation_columns``
    (when behavior models are loaded) → ``compute_features``.

    ``feature_config`` lets the streaming caller pass its once-built
    ``FeatureConfig`` instead of reconstructing it each tick; when omitted it
    is derived from ``config.preprocessing.rolling_window_seconds`` exactly as
    the batch path always did — the two are value-identical.

    ``bindings`` is forwarded to ``segment_or_constant`` so the ``decoded:``
    mode candidates are scaled with the catalog that decoded them; omitted, the
    accessor uses the repo ``data/bindings.yaml`` (identical to the daemon's
    ``dashboard.paths.DEFAULT_BINDINGS_PATH`` — asserted in
    tests/test_decoded_mode_signals.py, which is why the per-tick live path
    does not have to ship a catalog across the scoring-process boundary).

    B01.2 — behavior models are evaluated *before* ``compute_features``;
    ``append_deviation_columns`` builds the context their serving contracts
    read first (``__roc`` derivatives at this transform's rolling horizon,
    ``context:*`` engine-start evidence). The live path's
    :class:`~src.preprocessing.live.LiveFeatureSource` carries start
    evidence across ticks itself (its ring buffer is far shorter than a
    warmup) and has already written those columns into ``wide``.
    """
    framed, segmenter_fell_back = segment_or_constant(wide, config, bindings=bindings)
    # Derived anomaly signals (air mass per cycle, VE, bank-balance
    # asymmetries, total trim, catalyst efficiency, AFR/TPS cross-checks)
    # appended BEFORE behavior deviations + feature extraction so recipes can
    # target them as outputs and the rolling feature builder picks up their
    # derivatives. `vehicle` unlocks VE_PERCENT (Task 4.1), which needs the
    # configured displacement.
    afr_calibration = (
        load_afr_calibration(config.daemon.afr_calibration)
        if config.daemon.afr_calibration.exists()
        else None
    )
    framed = append_afr_channels(framed, config.vehicle.parameters, afr_calibration)
    framed = compute_anomaly_signals(framed, config.vehicle.parameters)
    # Performance channels (Task 1.1): fuel rate, instant economy,
    # boost/vacuum, estimated power/torque. Row-local only — the cumulative
    # trip integrals (FUEL_USED_L, TRIP_DISTANCE_KM, ...) are deliberately
    # NOT computed here, because this frame is a live ring-buffer *window*
    # on the streaming path, not a full session; a cumsum over it would
    # reset every time the window evicts old rows. See
    # ``compute_instantaneous_channels`` and ``LiveFeatureSource``'s
    # session-scoped accumulators (Task 1.3) for the integrated versions.
    # `calibration` is not optional decoration: PID 0x44 is φ on some ECMs and
    # λ on others, the two are reciprocals, and they agree at stoich — so
    # guessing is silent at idle and worst under enrichment, exactly where the
    # fuel estimate matters. Passing it lets the fuel math use the verified
    # convention instead of hard-coding λ.
    framed = compute_instantaneous_channels(
        framed, config.vehicle.parameters, calibration=afr_calibration
    )
    # Sensor-committee divergence residuals (redundant sensors of one quantity,
    # e.g. OBD↔GPS speed). Distinct from the physical residuals above; gated on
    # input quality. Non-trainable committees are emitted here but excluded from
    # the trained feature set below — see analysis.committees.
    framed = append_committee_residuals(framed)
    cfg = feature_config or FeatureConfig(
        window_seconds=config.preprocessing.rolling_window_seconds
    )
    if behavior_models is not None and behavior_models.models:
        framed = append_deviation_columns(
            framed, behavior_models, rate_window_seconds=cfg.window_seconds
        )
    features = compute_features(framed, config=cfg)
    return FeaturizedFrame(
        features=features, operating_frame=framed, segmenter_fell_back=segmenter_fell_back
    )


def segment_or_constant(
    resampled: pd.DataFrame, config: CanaryConfig, *, bindings: Bindings | None = None
) -> tuple[pd.DataFrame, bool]:
    """Apply the operating-mode segmenter when its inputs exist, else add a
    constant ``operating_mode = "all"`` column so a single model is fit.

    Returns ``(frame, fell_back)`` — ``fell_back`` (4.7) is ``True`` only for
    the *runtime* fallback (segmenter input columns missing from this frame),
    not for an explicit ``single_operating_mode`` config, which is an
    intentional choice rather than a diagnostic-worthy degradation. Callers
    that care about the constant-mode outcome but not *why* can ignore the
    second element.

    ``bindings`` is the catalog the ``decoded:`` candidates in
    ``SEGMENTER_SIGNAL_COLUMNS`` are scaled with — pass the same one the
    frame was *decoded* with (``build_feature_frame`` does, so ``--bindings``
    reaches the mode rules too). Omitted, the shared accessor uses the repo's
    ``data/bindings.yaml``, which is what every production decode path loads.
    """
    if config.preprocessing.single_operating_mode:
        out = resampled.copy()
        out[MODE_COLUMN] = ALL_MODE
        return out, False
    seg_cfg = SegmenterConfig(
        signal_columns=dict(SEGMENTER_SIGNAL_COLUMNS),
        recent_motion_window_seconds=config.preprocessing.idle_transient_lookback_seconds,
        mode_dwell_ticks=config.preprocessing.mode_dwell_ticks,
        bindings=bindings,
    )
    # A signal is available when *any* of its candidate columns is present, so
    # the speed gate is satisfied by gps:speed or obd:SPEED.
    have_inputs = all(
        _signal_present(resampled, value) for value in seg_cfg.signal_columns.values()
    )
    if have_inputs:
        segmented = segment_operating_modes(
            resampled, config.preprocessing.operating_modes, config=seg_cfg
        )
        return segmented, False
    out = resampled.copy()
    out[MODE_COLUMN] = ALL_MODE
    log.info("no OBD PID columns present; using a single operating mode", extra={"mode": ALL_MODE})
    return out, True


def trainable_feature_columns(
    features: pd.DataFrame, *, policy: FeaturePolicy | None = None
) -> list[str]:
    """Numeric feature columns with informative variation in the corpus.

    Excludes structural columns (``session``, ``timestamp``, the mode column)
    and gap flags; keeps ``decoded:*`` and OBD channels plus their
    ``__mean``/``__std``/``__roc`` derivatives. Drops all-NaN columns (signals
    whose CAN ID never appeared this session) so the z-score scaler and the
    sequence-window builder don't choke, and drops columns *constant across
    the entire corpus*: a channel with zero training variance teaches the
    detectors nothing, and the moment the sensor comes alive after training
    it dominates every event's attribution (the May/June models trained while
    ``obd:ABSOLUTE_LOAD`` polling was dead-constant; when the PID started
    reporting in July, its family appeared in 37/37 LSTM events). Constancy
    is judged over the whole multi-session frame, so a channel that is merely
    steady *within* one session (coolant at temperature) survives as long as
    the corpus saw it move. ``summarize_feature_health`` documents the drops
    in the manifest (``dropped_constant_columns``).
    """
    cols: list[str] = []
    for col in candidate_feature_columns(features, policy=policy):
        health = _column_health(features[col])
        if health["finite_count"] == 0:
            continue
        if health["constant"]:
            continue
        cols.append(col)
    return cols


#: Minimum fraction of rows a feature must be observed on to be trainable by
#: the **Isolation Forest**. Unlike the sequence detector — which is handed
#: missingness explicitly via GRU-D mask/Δt channels and is expected to see
#: sparse round-robin PIDs — the IF scores one row at a time and
#: ``TrainedIsolationForest.score`` refuses any row with a NaN in ANY trained
#: feature (``group[feats].isna().any(axis=1)`` → score=inf, missing flag set,
#: and the daemon's fail-safe then suppresses the emit). So every sparse column
#: in the IF's schema multiplies the chance that a row is unscoreable, and a
#: single structurally-absent column makes the detector permanently blind.
#:
#: Measured on the 2026-07-26 corpus: the IF trained 273 features of which NONE
#: reached 75% coverage, so it could not score a single row — matching the Pi's
#: observed ``missing_feature_rate: 1.0``. Offenders split in two:
#: genuinely-absent channels (``derived:TPS_VOLTAGE_PER_PCT`` at 0.21%,
#: ``did:*`` at ~1.5% — polled only inside periodic Mode-22 windows) and
#: channels NaN'd as stale because the resampler's ceiling assumed a poll rate
#: the contended bus never achieves. This threshold removes the former; the
#: staleness ceiling (``preprocessing.max_gap_factor``) governs the latter.
MIN_ISOLATION_FOREST_COVERAGE = 0.5


def isolation_forest_feature_columns(
    features: pd.DataFrame,
    *,
    min_coverage: float = MIN_ISOLATION_FOREST_COVERAGE,
    policy: FeaturePolicy | None = None,
) -> list[str]:
    """Trainable columns restricted to those the IF can actually score on.

    :func:`trainable_feature_columns` plus a per-column coverage floor — see
    :data:`MIN_ISOLATION_FOREST_COVERAGE` for why the IF needs one and the
    sequence detector does not. ``min_coverage=0`` recovers the old behaviour.
    """
    columns = trainable_feature_columns(features, policy=policy)
    if min_coverage <= 0 or features.empty:
        return columns
    scored = isolation_forest_coverage_rows(features)
    rows = len(scored)
    return [c for c in columns if float(scored[c].notna().sum()) / rows >= min_coverage]


def isolation_forest_coverage_rows(features: pd.DataFrame) -> pd.DataFrame:
    """The rows the IF coverage floor is measured over: those in a classified
    operating mode.

    The IF never fits the ``unknown`` mode (``SKIP_UNCLASSIFIED_MODE``) and
    serving refuses one (A04.8), so an unclassified row is a row it can never
    score. Counting them let parked key-off chatter decide the feature set: in
    the 2026-09-21..23 corpus 72 % of rows were ``unknown`` (the Pi recording
    GPS with the engine off), every engine channel fell under the floor, and
    the retrained IF kept two channels. A frame with no classified row (or no
    mode column) is measured whole.
    """
    if MODE_COLUMN not in features.columns:
        return features
    classified = features[features[MODE_COLUMN].astype(str) != UNKNOWN_MODE]
    return classified if len(classified) else features


def low_coverage_columns(
    features: pd.DataFrame,
    columns: Sequence[str],
    *,
    min_coverage: float = MIN_ISOLATION_FOREST_COVERAGE,
) -> dict[str, float]:
    """``{column: coverage}`` for ``columns`` below the coverage floor.

    Reported in the manifest so a blind-IF diagnosis doesn't require
    re-deriving coverage from the corpus by hand.
    """
    if features.empty:
        return {}
    scored = isolation_forest_coverage_rows(features)
    rows = len(scored)
    out: dict[str, float] = {}
    for column in columns:
        if column not in scored.columns:
            continue
        coverage = float(scored[column].notna().sum()) / rows
        if coverage < min_coverage:
            out[str(column)] = round(coverage, 6)
    return out


def _is_constant_column(series: pd.Series) -> bool:
    """True when every finite value in ``series`` is identical (or none exist).

    Same definition as ``_column_health``'s ``constant`` flag so selection and
    the manifest's health report can never disagree."""
    return bool(_column_health(series)["constant"])


def restrict_isolation_forest_expansion(
    columns: Sequence[str],
    *,
    drop_suffixes: Sequence[str] = (),
) -> list[str]:
    """Drop rolling-feature-expansion columns ending in ``drop_suffixes`` from
    an already-resolved IF feature list (e.g. ``trainable_feature_columns``'s
    output).

    Task 2.5 (ml-correctness-hardening): each channel expands 4x (raw +
    ``__mean`` + ``__std`` + ``__roc``) and the IF trial benchmark compared
    that full set against raw+``__roc``-only and raw-only subsets, passing
    the trimmed list to ``fit_isolation_forest``'s existing ``feature_columns``
    override (no new config knob needed). The trial found the full 4x set won
    on both detection rate and nominal FP — the diluted-random-split theory
    didn't hold up here because ``__std``/``__roc`` collapsing to ~0 IS the
    primary signature of a "stuck" sensor fault, not a redundant copy of the
    raw channel — so this is unused by the default IF training path today
    (``scripts/train_anomaly_model.py`` keeps calling
    ``trainable_feature_columns`` unfiltered). Kept here, not deleted, so a
    future retrial (a real multi-session corpus, a different channel mix)
    doesn't have to rebuild the plumbing from scratch. Never applied to
    ``sequence_feature_columns`` — the sequence detector keeps the full
    expansion (masking makes it cheap there, per the task).
    """
    return [c for c in columns if not any(c.endswith(suffix) for suffix in drop_suffixes)]


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
        # logged, but kept out of the trained set — see analysis.committees.
        if base_channel(col) in non_trainable_committees:
            continue
        if pd.api.types.is_numeric_dtype(features[col]):
            cols.append(col)
    return cols


def _signal_present(frame: pd.DataFrame, value: str | tuple[str, ...]) -> bool:
    """True when at least one of a segmenter signal's candidate columns exists."""
    columns = (value,) if isinstance(value, str) else value
    return any(column in frame.columns for column in columns)


#: Prefix of behavior-model deviation columns (raw + __mean/__std/__roc).
DEVIATION_PREFIX = "deviation:"


def sequence_feature_columns(
    features: pd.DataFrame, *, policy: FeaturePolicy | None = None
) -> list[str]:
    """GRU-D LSTM-autoencoder feature columns: all trainable sensor channels.

    Differs from :func:`trainable_feature_columns` (which the Isolation Forest
    uses) in one way: it **drops the entire ``deviation:`` family** (raw and
    rolling aggregates). Deviations are behavior-model-derived scores that are
    NaN by contract whenever the model can't speak (off operating-regime) —
    scattered NaN, not a gap. They are designed for the IF path, which imputes;
    an autoencoder reconstructing sensor *sequences* should not consume them.

    The previous density / every-session pruning is **gone**: GRU-D feeds
    missingness to the model via the mask/Δt channels, so a round-robin OBD PID
    (stale most ticks) or a channel absent from some drives is kept at its native
    rate rather than dropped. The remaining drops — all-NaN-everywhere and
    corpus-constant (zero-variance) columns — are inherited from
    :func:`trainable_feature_columns`.
    """
    return [
        col
        for col in trainable_feature_columns(features, policy=policy)
        if not col.startswith(DEVIATION_PREFIX)
    ]


def expand_suppress_channels(
    suppress_bases: Sequence[str], feature_columns: Sequence[str]
) -> tuple[str, ...]:
    """Expand each ``suppress_channels`` entry to the matching base column
    plus its rolling-derivative siblings.

    Listing ``obd:FUEL_LEVEL`` matches all of ``obd:FUEL_LEVEL``,
    ``obd:FUEL_LEVEL__mean``, ``obd:FUEL_LEVEL__std``, ``obd:FUEL_LEVEL__roc``.
    Entries that don't match any column are silently dropped (a typo or a
    channel that disappears from the corpus becomes a no-op, not a hard
    failure that breaks the serve path). Order matches ``feature_columns``
    so the result composes cleanly with index lookups.
    """
    suppress_set = {str(s) for s in suppress_bases}
    if not suppress_set:
        return ()
    matched: list[str] = []
    for column in feature_columns:
        if column in suppress_set:
            matched.append(column)
            continue
        # Rolling derivatives follow their base channel, so the operator lists
        # the sensor once and its feature-pipeline siblings come along.
        base, suffix = split_rolling(column)
        if suffix and base in suppress_set:
            matched.append(column)
    return tuple(matched)


def align_features_to_schema(
    features: pd.DataFrame, feature_columns: Sequence[str]
) -> pd.DataFrame:
    """Ensure every model feature column exists, adding absent ones as all-NaN.

    A drive may legitimately lack a whole sensor family — e.g. no GPS fix, so no
    ``gps:*`` channels — while the model's schema (trained on a corpus that did
    have it) still expects those columns. The GRU-D mask/Δt path already treats
    an all-NaN channel as unobserved (it imputes the cell and masks it, so the
    loss never scores a fill), which is exactly how the model saw GPS-less
    sessions at training time. So an absent column should be scored as
    masked-missing, never raise in the scaler or the ``.loc`` window slice.

    Returns ``features`` unchanged when nothing is missing.
    """
    missing = [str(column) for column in feature_columns if column not in features.columns]
    if not missing:
        return features
    aligned = features.copy()
    for column in missing:
        aligned[column] = np.nan
    log.info(
        "aligned feature frame to model schema; absent columns masked as unobserved",
        extra={"missing_columns": missing, "missing_count": len(missing)},
    )
    return aligned


def build_feature_spec(
    features: pd.DataFrame,
    feature_columns: Sequence[str],
    *,
    config: CanaryConfig | None = None,
    sequence_length: int | None = None,
    sequence_stride: int | None = None,
    policy: FeaturePolicy | None = None,
    behavior_models: BehaviorModels | None = None,
) -> dict[str, Any]:
    """Return the versioned feature contract persisted in model manifests.

    This is the first explicit train/serve schema artifact for Phase 7:
    ordered feature columns, their dtypes, the preprocessing knobs that shape
    them, source families, and a compact health report. Later 7.1 work can
    compare this spec against the live source before scoring.

    ``behavior_models`` is the artifact set that produced the frame's
    ``deviation:*`` columns. When given, the spec records the
    :func:`~src.decode.behavior_models.artifact_fingerprint` of every model a
    consumed deviation column came from (``behavior_model_fingerprints``), so
    a later refit that changes that column's meaning is caught by the
    producibility check instead of being fed to this artifact (B03.4). Left
    ``None``, the field is absent and the provenance is *unknown* — which the
    check reports as such, never as a pass.
    """
    columns = [str(col) for col in feature_columns]
    resolved_policy = policy if policy is not None else resolve_feature_policy()
    candidate_columns = candidate_feature_columns(features, policy=resolved_policy)
    spec: dict[str, Any] = {
        "feature_spec_version": FEATURE_SPEC_VERSION,
        "feature_columns": columns,
        "feature_dtypes": {column: str(features[column].dtype) for column in columns},
        "source_prefixes": sorted({_source_prefix(column) for column in columns}),
        "behavior_model_columns": sorted(
            {base_channel(column) for column in columns if column.startswith("deviation:")}
        ),
        "feature_health": summarize_feature_health(
            features,
            columns,
            candidate_columns=candidate_columns,
        ),
        # Channels present in the corpus that policy kept out, with the reason
        # as it stood at training time (binding confidence / opt-out, DID not
        # served). Empty when nothing was gated.
        "feature_policy_exclusions": feature_policy_exclusions(features, resolved_policy),
    }
    if behavior_models is not None:
        spec["behavior_model_fingerprints"] = consumed_behavior_fingerprints(
            columns, behavior_models
        )
    if config is not None:
        spec["preprocessing"] = {
            "resample_hz": config.preprocessing.resample_hz,
            "rolling_window_seconds": config.preprocessing.rolling_window_seconds,
            "operating_modes": sorted(config.preprocessing.operating_modes),
        }
        # The resampler's max_gap is what flips a channel's ``__gap`` flag to
        # stale at training time. GRU-D serve must mirror it exactly (it has no
        # resampler), so persist it on the spec: the training recipe's
        # max_gap, (1 / hz) * config.preprocessing.max_gap_factor. This once
        # built ResampleConfig(hz=...) and so recorded the default factor 2.0
        # whatever the config said (D03.27).
        spec["max_gap_seconds"] = float(resample_config_for(config).max_gap.total_seconds())
        spec["slow_channel_max_gap_seconds"] = float(
            config.preprocessing.slow_channel_max_gap_seconds
        )
    if sequence_length is not None:
        spec["sequence_length"] = sequence_length
    if sequence_stride is not None:
        spec["sequence_stride"] = sequence_stride
    return spec


def consumed_behavior_fingerprints(
    feature_columns: Sequence[str], behavior_models: BehaviorModels
) -> dict[str, str]:
    """``{behavior model name: artifact_fingerprint}`` for every consumed ``deviation:*`` column.

    Refuses a consumed column whose model is not in ``behavior_models``: the
    frame was then featurized against a different artifact set than the one
    being recorded (a stale feature cache, or a model retired since), which
    is exactly the unknown provenance this record exists to prevent — rebuild
    the feature cache against the current ``behavior_models.yaml`` instead.
    """
    by_name = {model.name: model for model in behavior_models.models}
    consumed = sorted(
        {name for column in feature_columns if (name := deviation_model_name(column)) is not None}
    )
    missing = [name for name in consumed if name not in by_name]
    if missing:
        raise ValueError(
            "feature spec consumes deviation columns of behavior models not in the "
            f"loaded artifact set: {', '.join(missing)} — the frame was featurized "
            "against a different behavior_models.yaml (stale feature cache?); rebuild it"
        )
    return {name: artifact_fingerprint(by_name[name]) for name in consumed}


def summarize_feature_health(
    features: pd.DataFrame,
    feature_columns: Sequence[str],
    *,
    candidate_columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Summarize missingness, finiteness, and constant features.

    ``candidate_columns`` lets callers report numeric columns dropped before
    training, especially all-NaN decoded/deviation columns.
    """
    columns = [str(col) for col in feature_columns]
    candidates = [str(col) for col in (candidate_columns or columns)]
    selected = set(columns)

    all_nan_columns: list[str] = []
    constant_columns: list[str] = []
    non_finite_columns: list[str] = []
    column_health: dict[str, dict[str, Any]] = {}

    for column in columns:
        health = _column_health(features[column])
        column_health[column] = health
        if health["non_null_count"] == 0:
            all_nan_columns.append(column)
        if health["non_finite_count"] > 0:
            non_finite_columns.append(column)
        if health["constant"]:
            constant_columns.append(column)

    dropped_all_nan_columns = [
        column
        for column in candidates
        if column not in selected
        and column in features.columns
        and pd.api.types.is_numeric_dtype(features[column])
        and not bool(features[column].notna().any())
    ]
    # Candidates excluded by trainable_feature_columns' zero-variance drop —
    # observed but never moving across the whole corpus (usually a dead poll,
    # e.g. ABSOLUTE_LOAD before its PID worked). Documented here so a manifest
    # answers "why isn't channel X in the model?" without re-running training.
    dropped_constant_columns = [
        column
        for column in candidates
        if column not in selected
        and column in features.columns
        and pd.api.types.is_numeric_dtype(features[column])
        and bool(features[column].notna().any())
        and _is_constant_column(features[column])
    ]
    # Per-column coverage for everything that IS selected, plus the sub-floor
    # coverage of what the IF's filter excluded. A blind detector
    # (missing_feature_rate 1.0) is diagnosed straight from here: if any
    # selected column's coverage is well under 1.0, per-row scoring will
    # frequently hit a NaN and self-suppress.
    coverage = {
        column: round(float(features[column].notna().sum()) / len(features), 6)
        for column in columns
        if len(features)
    }
    dropped_low_coverage_columns = low_coverage_columns(
        features, [c for c in candidates if c not in selected]
    )

    return {
        "row_count": int(len(features)),
        "candidate_feature_count": len(candidates),
        "selected_feature_count": len(columns),
        "dropped_all_nan_columns": dropped_all_nan_columns,
        "dropped_constant_columns": dropped_constant_columns,
        "dropped_low_coverage_columns": dropped_low_coverage_columns,
        "min_selected_coverage": (min(coverage.values()) if coverage else None),
        "all_nan_columns": all_nan_columns,
        "constant_columns": constant_columns,
        "non_finite_columns": non_finite_columns,
        "columns": column_health,
    }


def validate_feature_health(feature_health: Mapping[str, Any]) -> None:
    """Fail fast on selected columns that cannot produce meaningful scores."""
    all_nan = [str(col) for col in feature_health.get("all_nan_columns", [])]
    if all_nan:
        raise ValueError("selected feature columns are all-NaN: " + ", ".join(all_nan))


@dataclass(frozen=True)
class FeatureSpecReconciliation:
    """Result of aligning a live/offline frame to a persisted feature spec."""

    row_count: int
    expected_feature_columns: tuple[str, ...]
    missing_feature_columns: tuple[str, ...]
    extra_feature_columns: tuple[str, ...]
    filled_feature_columns: tuple[str, ...]
    coerced_feature_columns: tuple[str, ...]
    output_columns: tuple[str, ...]

    @property
    def has_divergence(self) -> bool:
        return bool(
            self.missing_feature_columns
            or self.extra_feature_columns
            or self.coerced_feature_columns
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_count": self.row_count,
            "expected_feature_columns": list(self.expected_feature_columns),
            "missing_feature_columns": list(self.missing_feature_columns),
            "extra_feature_columns": list(self.extra_feature_columns),
            "filled_feature_columns": list(self.filled_feature_columns),
            "coerced_feature_columns": list(self.coerced_feature_columns),
            "output_columns": list(self.output_columns),
            "has_divergence": self.has_divergence,
        }


def reconcile_feature_frame_to_spec(
    frame: pd.DataFrame,
    schema: Mapping[str, Any],
    *,
    structural_columns: Sequence[str] | None = None,
    fill_missing: float = float("nan"),
    coerce_numeric: bool = True,
) -> tuple[pd.DataFrame, FeatureSpecReconciliation]:
    """Align ``frame`` to the model feature spec persisted in a manifest.

    The serving policy is deliberately explicit:

    * expected feature columns absent from the live row are added as ``NaN``;
    * extra live feature columns are dropped before scoring;
    * expected feature columns are coerced numeric because every trained
      feature column is numeric by construction.

    Structural columns (timestamp/session/mode) are preserved ahead of feature
    columns so model adapters still have the metadata they need.
    """
    feature_columns = tuple(str(column) for column in schema.get("feature_columns", ()))
    if not feature_columns:
        raise ValueError("feature schema has no feature_columns")

    # Every column finished before any frame is built (D01.10). The previous
    # form copied ``frame`` and then inserted each fill and re-assigned each
    # coerced column in place: ~650 block-manager writes (``iset`` /
    # ``_iset_split_block`` / ``blocks.delete``) per one-row tick, which was
    # the whole cost. Keys follow the order the old insert sequence left them
    # in — ``frame``'s own columns, then the fills — so the frame the extras
    # are judged against is the same one.
    if frame.columns.has_duplicates:
        duplicated = sorted({str(c) for c in frame.columns[frame.columns.duplicated()]})
        raise ValueError("feature frame has duplicate columns: " + ", ".join(duplicated))
    finished: dict[Any, pd.Series] = dict(frame.items())
    present = {str(column) for column in frame.columns}
    missing = tuple(column for column in feature_columns if column not in present)
    # One fill column shared by every missing key (the frame build copies it
    # into place), so a spec that is mostly absent costs one Series, not one
    # per absent feature.
    fill = pd.Series(fill_missing, index=frame.index)
    for column in missing:
        finished[column] = fill

    dtype_spec = schema.get("feature_dtypes", {})
    coerced: list[str] = []
    if coerce_numeric:
        coerced_fill: pd.Series | None = None
        for column in feature_columns:
            series = finished[column]
            before = str(series.dtype)
            if series is fill:
                if coerced_fill is None:
                    coerced_fill = pd.to_numeric(fill, errors="coerce")
                series = coerced_fill
            else:
                series = pd.to_numeric(series, errors="coerce")
            finished[column] = series
            after = str(series.dtype)
            expected_dtype = (
                str(dtype_spec.get(column, "")) if isinstance(dtype_spec, Mapping) else ""
            )
            if before != after or (expected_dtype and after != expected_dtype):
                coerced.append(column)

    out = pd.DataFrame(finished, index=frame.index, copy=False)
    extras = tuple(
        column for column in candidate_feature_columns(out) if column not in feature_columns
    )
    structural = _resolve_structural_columns(out, schema, structural_columns)
    output_columns = tuple(dict.fromkeys([*structural, *feature_columns]))
    aligned = out.loc[:, [column for column in output_columns if column in out.columns]].copy()
    report = FeatureSpecReconciliation(
        row_count=int(len(aligned)),
        expected_feature_columns=feature_columns,
        missing_feature_columns=missing,
        extra_feature_columns=extras,
        filled_feature_columns=missing,
        coerced_feature_columns=tuple(coerced),
        output_columns=tuple(str(column) for column in aligned.columns),
    )
    return aligned, report


def _resolve_structural_columns(
    frame: pd.DataFrame,
    schema: Mapping[str, Any],
    override: Sequence[str] | None,
) -> tuple[str, ...]:
    if override is not None:
        return tuple(str(column) for column in override if str(column) in frame.columns)

    candidates = [
        schema.get("timestamp_column", "timestamp"),
        schema.get("session_column", "session"),
        schema.get("mode_column", MODE_COLUMN),
    ]
    return tuple(
        dict.fromkeys(str(column) for column in candidates if str(column) in frame.columns)
    )


def _column_health(series: pd.Series) -> dict[str, Any]:
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=np.float64, copy=False)
    finite_mask = np.isfinite(values)
    finite_values = values[finite_mask]
    non_null_count = int(series.notna().sum())
    finite_count = int(finite_mask.sum())
    unique_finite_count = int(pd.Series(finite_values).nunique(dropna=True))
    return {
        "dtype": str(series.dtype),
        "non_null_count": non_null_count,
        "finite_count": finite_count,
        "nan_count": int(series.isna().sum()),
        "non_finite_count": non_null_count - finite_count,
        "unique_finite_count": unique_finite_count,
        "constant": finite_count > 0 and unique_finite_count <= 1,
    }


def _source_prefix(column: str) -> str:
    return source_of(base_channel(column)) or "derived"
