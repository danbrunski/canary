"""Tuning analysis with the methodology gates enforced in code.

The 2026-08 campaign retracted three findings in three days, each because an
ad-hoc script re-derived analysis without a gate the project already knew
about. Those gates lived in prose (``docs/roadmap/engine-tuning-support.md``),
so every new script had to remember them, and none did:

* **Closed-loop dilution.** In closed loop the ECU corrects to target
  regardless of what fuelling was changed, so pooling those samples pushes any
  measured effect toward zero *by construction*. A K step of +0.95 % read as
  "unchanged" because 92 % of the samples were at stoich.
* **Band-composition bias.** Delivery error varies with rpm, so pooling across
  two datasets with different rpm coverage compares coverage, not fuelling.
  This alone manufactured a "K responds 1:1" result.
* **DFCO masquerading as enrichment.** ``FUEL_STATUS == 4`` means "open loop",
  which covers *both* power enrichment and decel fuel cut. A bare ``fs == 4``
  filter over the July corpus is 88 % overrun.
* **Gridding on load.** ``CAL/LD`` is derived from BFS, so conditioning on it
  while fuelling moves partly conditions on the thing that moved — and it is
  not even stable at fixed fuel (it shifted 1.43 points at identical airflow
  across a same-tune null run).

Every function here refuses the unsafe form rather than trusting the caller to
remember, and reports which gate dropped how many samples so a diluted result
is visible instead of silent.

See ``docs/roadmap/archive/tuning-analysis-toolkit.md`` Phase 2.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NoReturn

import numpy as np
import pandas as pd

from src.analysis.channels import (
    TIMESTAMP_BASIS_COLUMN,
    raw_read_filters,
    read_aligned,
    read_channels,
    refuse_as_of,
)
from src.analysis.lambda_delivery import (
    ELIGIBLE,
    STATES,
    CommandedSource,
    LambdaDeliveryDeclaration,
    MeasuredSource,
    RegimeDelay,
    delivery_samples,
)
from src.analysis.noise_floor import FloorRefusal, NoiseFloor
from src.analysis.tuning_frame import ProviderRegistry
from src.decode.surface_eval import bin_indices
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.signals.afr import AfrCalibration
from src.signals.pairing import (
    PairingPolicy,
    check_clock_support,
    match_ordered,
    to_seconds,
    weakest_basis,
)
from src.storage.capture_manifest import read_capture_manifest
from src.vehicle.tune_revisions import TuneRevisionRegistry, resolve_session_revision

#: Axes that must never grid a cross-flash comparison. ``CAL/LD`` is computed
#: from BFS, so it moves when fuelling moves; see the module docstring.
FORBIDDEN_AXES: frozenset[str] = frozenset(
    {"ENGINE_LOAD", "CAL/LD", "CAL/LD VALUE", "CALCULATED_LOAD", "ABSOLUTE_LOAD", "LOAD"}
)

# rpm band edges for band-averaging are a vehicle fact (the top edge is the
# engine's redline): every caller passes ``rpm_bands`` from the profile's
# ``tuning_providers.yaml`` ``mode01_wot`` (R05.13). There is no default.

#: `FUEL_STATUS` bit for "open loop". Covers enrichment *and* decel fuel cut,
#: which is why it is never used alone.
OPEN_LOOP = 4.0


class GateError(ValueError):
    """A caller asked for an analysis a gate refuses to perform."""


class InputsNotCapturedError(GateError):
    """The session never held what the analysis needs — not a gate verdict.

    Structured so a caller can tell the kinds apart without parsing the
    message (R05.11): ``kind`` is ``"not_captured"`` when named inputs are
    empty in the session, ``"not_co_observed"`` when every input is present
    but no two overlapped in time; ``missing`` names the empty inputs.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: Literal["not_captured", "not_co_observed"],
        missing: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.missing = missing


@dataclass
class GateReport:
    """How many samples each gate removed, and why."""

    started: int = 0
    filtered: dict[str, int] = field(default_factory=dict)
    remaining: int = 0

    def drop(self, gate: str, before: int, after: int) -> None:
        if before != after:
            self.filtered[gate] = self.filtered.get(gate, 0) + (before - after)

    def merge(self, other: GateReport) -> None:
        """Fold another report in — for pooling sessions gated separately."""
        self.started += other.started
        self.remaining += other.remaining
        for gate, count in other.filtered.items():
            self.filtered[gate] = self.filtered.get(gate, 0) + count

    def as_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "filtered": dict(self.filtered),
            "remaining": self.remaining,
        }


#: The delivery-error engine the payloads carry (R05.16). 1 was the 1 s
#: last-cell grid (``read_aligned``); 2 pairs target and measurement by
#: acquisition time (:func:`src.analysis.lambda_delivery.delivery_samples`).
#: A stored result without ``engine_version`` is engine 1.
DELIVERY_ENGINE_VERSION = 2

#: Raw PID 0x44, the Mode 01 commanded equivalence ratio. Read raw so a
#: fuel-cut sample (ratio 0.0) is a named ``fuel_cut`` state rather than a
#: row that vanishes.
EQUIV_RATIO_SPEC = "obd:COMMANDED_EQUIV_RATIO"


@dataclass
class DeliveryError:
    """Measured-vs-commanded AFR, band-averaged, with its gate report."""

    bands: pd.DataFrame
    band_averaged_pct: float
    pooled_pct: float
    n: int
    gates: dict[str, Any]
    rpm_window: tuple[float, float]
    measured_spec: str
    #: Calibration in force when the session was recorded, when a registry
    #: resolves it. Comparing delivery error across a flash boundary without
    #: this is comparing two different engines.
    tune_revision: str | None = None
    tune_revision_source: str = "unknown"
    #: How target and measurement were paired (:func:`pairing_block`).
    pairing: dict[str, Any] = field(default_factory=dict)
    #: The result at each end of the route's delay bracket
    #: (:func:`delay_sensitivity`); ``None`` when the route models no delay.
    delay_sensitivity: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine_version": DELIVERY_ENGINE_VERSION,
            "tune_revision": self.tune_revision,
            "tune_revision_source": self.tune_revision_source,
            "band_averaged_pct": self.band_averaged_pct,
            "pooled_pct": self.pooled_pct,
            "n": self.n,
            "rpm_window": list(self.rpm_window),
            "measured_spec": self.measured_spec,
            "pairing": dict(self.pairing),
            "delay_sensitivity": self.delay_sensitivity,
            "gates": self.gates,
            "bands": self.bands.to_dict(orient="records"),
        }


def _assert_axis_allowed(axis: str, *, unsafe: bool) -> None:
    bare = axis.partition(":")[2] or axis
    if (
        bare.upper().replace("_", "/") in {a.replace("_", "/") for a in FORBIDDEN_AXES}
        and not unsafe
    ):
        raise GateError(
            f"{axis!r} is not a valid comparison axis: calculated load is derived from BFS, "
            "so conditioning on it while fuelling moves partly conditions on what moved. "
            "It is not stable at fixed fuel either — it shifted 1.43 points at identical "
            "airflow across the 2026-08-04 same-tune null run. Grid on rpm x MAF (real "
            "airflow) or rpm x BFS (the actual table cell) instead, or pass "
            "unsafe_axes=True if you have a reason."
        )


@dataclass(frozen=True)
class DeliveryRoute:
    """Which declared sources answer delivery error, and the context channels.

    The target and measured sources are *names* in the vehicle's
    ``lambda_delivery.yaml``, so what each one is (phi, lambda, AFR, a
    voltage through the calibrated transfer), how fuel cut shows on it and in
    which regimes it is the final target all come from that declaration,
    never from this module. ``rpm_spec`` bins the result; ``fuel_status_spec``
    is the Mode 01 open-loop gate, needed only where the target is not the
    final one in closed loop (PID 0x44 reads sensor-like values there);
    ``load_spec`` backs a ``min_load`` floor.
    """

    target_source: str
    measured_source: str
    rpm_spec: str
    #: What the result says it measured with (``measured_spec`` in payloads).
    measured_label: str
    fuel_status_spec: str | None = None
    load_spec: str | None = None


def _declared_measured(declaration: LambdaDeliveryDeclaration, spec: str) -> MeasuredSource:
    for source in declaration.measured_sources:
        if source.spec == spec:
            return source
    declared = ", ".join(f"{s.spec} ({s.name})" for s in declaration.measured_sources)
    raise GateError(
        f"{spec} is not a measured-mixture source this vehicle's lambda_delivery.yaml "
        f"declares ({declared}). A measurement is compared against the commanded "
        "target only once its quantity and conversion are declared with evidence: on "
        "2026-09-21 a raw A/F sensor voltage (~1.5 V) was compared against a 12.6 AFR "
        "target as-is and returned a confident -87 %."
    )


def _declared_target(declaration: LambdaDeliveryDeclaration, spec: str) -> CommandedSource:
    for source in declaration.commanded_sources:
        if source.spec == spec:
            return source
    refused = {r.spec: r.reason for r in declaration.refused_commanded}
    if spec in refused:
        raise GateError(f"{spec} is refused as a commanded target: {refused[spec]}")
    raise GateError(
        f"{spec} is not a commanded-mixture source this vehicle's lambda_delivery.yaml declares"
    )


def mode01_route(
    declaration: LambdaDeliveryDeclaration,
    *,
    measured_spec: str,
    min_load: float | None,
) -> DeliveryRoute:
    """The Mode 01 route: PID 0x44 against ``measured_spec``, gated on open loop."""
    target = _declared_target(declaration, EQUIV_RATIO_SPEC)
    measured = _declared_measured(declaration, measured_spec)
    return DeliveryRoute(
        target_source=target.name,
        measured_source=measured.name,
        rpm_spec="obd:RPM",
        measured_label=measured_spec,
        fuel_status_spec="obd:FUEL_STATUS",
        load_spec="obd:ENGINE_LOAD" if min_load is not None else None,
    )


def semantic_route(
    declaration: LambdaDeliveryDeclaration,
    providers: ProviderRegistry,
    *,
    measured_role: str,
) -> DeliveryRoute:
    """The all-DID route: the provider registry's ``commanded_afr``, ``rpm`` and
    ``measured_role`` channels, each resolved to its declared source.

    No open-loop gate: the declared target is the ECU's final one in every
    regime it lists, and fuel cut is its sentinel state. No load floor
    either: calculated load is Mode 01 only, and no validated semantic load
    provider exists.
    """
    specs: dict[str, str] = {
        str(provider.role): provider.spec
        for provider in providers.providers
        if provider.confidence == "confirmed"
    }
    missing = [role for role in ("commanded_afr", "rpm", measured_role) if role not in specs]
    if missing:
        raise GateError(
            "this vehicle's tuning_providers.yaml confirms no provider for " + ", ".join(missing)
        )
    target = _declared_target(declaration, specs["commanded_afr"])
    measured = _declared_measured(declaration, specs[measured_role])
    return DeliveryRoute(
        target_source=target.name,
        measured_source=measured.name,
        rpm_spec=specs["rpm"],
        measured_label=f"semantic:{measured_role} ({measured.spec})",
    )


@dataclass(frozen=True)
class DeliveryInputs:
    """One session's delivery channels as long frames on the acquisition clock."""

    target: pd.DataFrame
    sentinel: pd.DataFrame | None
    measured: pd.DataFrame
    rpm: pd.DataFrame
    fuel_status: pd.DataFrame | None
    load: pd.DataFrame | None


_EMPTY_LONG = ["timestamp", "spec", "value", "unit"]


def _session_rows(
    session: Path | pd.DataFrame,
    specs: list[str],
    *,
    bindings_path: Path,
    dids_path: Path,
) -> Path | pd.DataFrame:
    """The session read once, only the rows ``specs`` read (P02.2).

    A whole-session read carries ~100k raw CAN frames per minute. This used to
    narrow to ``obd`` rows only when every spec was a polled one, and read the
    whole session otherwise; :func:`raw_read_filters` narrows every source.
    """
    if not isinstance(session, Path):
        return session
    from src.preprocessing.loader import read_session

    filters = raw_read_filters(specs, bindings_path=bindings_path, dids_path=dids_path)
    return read_session(session, filters=filters or None)


def _refuse_not_captured(
    session: Path | pd.DataFrame,
    missing: list[str],
    *,
    mode01_exclusion_note: str | None,
    kind: Literal["not_captured", "not_co_observed"] = "not_captured",
) -> NoReturn:
    """Say *why* there is nothing to analyse instead of reporting zero bands.

    An empty result has two causes that demand opposite responses, and
    reporting ``started: 0`` for both made them indistinguishable:

    * the gates filtered everything — a real answer about the drive, meaning
      go and capture the regime you care about;
    * the inputs were never captured (or never overlapped) — not an answer at
      all, and no amount of driving will change it.

    Measured 2026-08-06: a clean 25-minute all-DID capture, 18k samples per
    channel at 14.93 Hz, returned ``started: 0``. The ``tuning-did`` profile
    disables Mode 01, and commanded AFR existed only as a Mode 01 PID, so the
    question was unanswerable before the truck moved.

    Raises :class:`InputsNotCapturedError`. Why a vehicle's all-DID captures
    lack Mode 01 is a vehicle fact, so it is ``mode01_exclusion_note`` from
    the profile's ``tuning_providers.yaml``, never a sentence in shared code.
    """
    detail = ""
    if isinstance(session, Path):
        manifest = read_capture_manifest(session)
        if manifest is not None and manifest.profile_snapshot.get("mode01_enabled") is False:
            detail = (
                f" This session was captured with profile {manifest.profile!r}, which "
                "disables Mode 01, so its Mode 01 channels are absent by construction, "
                "not by chance."
            )
            if mode01_exclusion_note:
                detail += f" {mode01_exclusion_note.strip()}"
    raise InputsNotCapturedError(
        "this session cannot answer delivery error — its inputs were never "
        "captured, which is not the same as the gates rejecting every sample. "
        + (
            "Empty in this session: " + ", ".join(missing)
            if kind == "not_captured"
            else "Every input is present, but no commanded-target read has a measured "
            "read within the declared acquisition separation."
        )
        + detail
        + " Use a Mode 01 capture for commanded-vs-measured fuelling, or the semantic"
        " route (the provider registry's commanded_afr) on an all-DID capture.",
        kind=kind,
        missing=tuple(missing),
    )


def read_delivery_inputs(
    session: Path | pd.DataFrame,
    route: DeliveryRoute,
    declaration: LambdaDeliveryDeclaration,
    *,
    calibration: AfrCalibration | None,
    afr_stoich: float,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    mode01_exclusion_note: str | None = None,
) -> DeliveryInputs:
    """Read ``route``'s channels once, on the acquisition clock.

    Raises :class:`InputsNotCapturedError` naming every required channel the
    session holds no reading of (an unresolvable spec counts as absent).
    """
    target = declaration.commanded(route.target_source)
    measured = declaration.measured(route.measured_source)
    wanted = {
        "target": target.spec,
        "measured": measured.spec,
        "rpm": route.rpm_spec,
    }
    if target.sentinel_spec is not None:
        wanted["sentinel"] = target.sentinel_spec
    if route.fuel_status_spec is not None:
        wanted["fuel_status"] = route.fuel_status_spec
    if route.load_spec is not None:
        wanted["load"] = route.load_spec
    rows = _session_rows(
        session, list(wanted.values()), bindings_path=bindings_path, dids_path=dids_path
    )
    frames: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for role, spec in wanted.items():
        try:
            frame = read_channels(
                rows,
                [spec],
                acquired_time=True,
                calibration=calibration,
                afr_stoich=afr_stoich,
                bindings_path=bindings_path,
                dids_path=dids_path,
            )
        except MemoryError:
            # Out of memory is the child's refusal, never "inputs never
            # captured" (P02.1 verification, 2026-09-26).
            raise
        except Exception as exc:  # an unresolvable spec is itself a missing input
            frame = pd.DataFrame(columns=_EMPTY_LONG)
            if role != "sentinel":
                missing.append(f"{spec} ({type(exc).__name__}: {exc})")
                continue
        if frame.empty and role != "sentinel":
            missing.append(spec)
        frames[role] = frame
    if missing:
        _refuse_not_captured(session, missing, mode01_exclusion_note=mode01_exclusion_note)
    return DeliveryInputs(
        target=frames["target"],
        sentinel=frames.get("sentinel"),
        measured=frames["measured"],
        rpm=frames["rpm"],
        fuel_status=frames.get("fuel_status"),
        load=frames.get("load"),
    )


def _check_clock(inputs: DeliveryInputs, policy: PairingPolicy) -> None:
    """Refuse a declared separation the stored timestamps cannot carry.

    Also refuses a change-only broadcast input (P02.10): every side is paired
    row to row by acquisition time, and such a read's rows are its payload
    changes, not its frames.
    """
    sides = [("target", inputs.target), ("measured", inputs.measured), ("rpm", inputs.rpm)]
    sides += [
        (name, frame)
        for name, frame in (("fuel_status", inputs.fuel_status), ("load", inputs.load))
        if frame is not None
    ]
    for name, frame in sides:
        refuse_as_of(frame, reader=f"delivery error's {name} pairing")
    reference = inputs.target
    ref_basis = weakest_basis(reference.get(TIMESTAMP_BASIS_COLUMN, pd.Series([], dtype=object)))
    ref_times = to_seconds(pd.to_datetime(reference["timestamp"], utc=True))
    for name, frame in sides[1:]:
        check_clock_support(
            policy,
            basis_a=ref_basis,
            times_a=ref_times,
            basis_b=weakest_basis(frame.get(TIMESTAMP_BASIS_COLUMN, pd.Series([], dtype=object))),
            times_b=to_seconds(pd.to_datetime(frame["timestamp"], utc=True)),
            label_a="commanded target",
            label_b=name,
        )


def _attach(times: np.ndarray, frame: pd.DataFrame | None, policy: PairingPolicy) -> np.ndarray:
    """Each target row's paired value from ``frame`` (NaN where none is in tolerance).

    One-to-one acquisition pairing (:func:`match_ordered`), the same rule as
    target against measurement: no reading is handed to two targets. So a
    change-only broadcast read (P02.10), one row per payload change, is
    refused rather than paired.
    """
    out = np.full(len(times), np.nan)
    if frame is None or frame.empty or not len(times):
        return out
    refuse_as_of(frame, reader="delivery error's context pairing")
    ordered = frame.sort_values("timestamp", kind="stable")
    other = to_seconds(pd.to_datetime(ordered["timestamp"], utc=True))
    values = pd.to_numeric(ordered["value"], errors="coerce").to_numpy(dtype=float)
    matched = match_ordered(times, other, policy)
    out[matched.index_a] = values[matched.index_b]
    return out


#: Said on every result whose route has no frozen delay.
DELAY_NOT_MODELLED = (
    "transport delay is not modelled on this route: each target read pairs with the "
    "measured read nearest it as acquired"
)


def _delay_fields(delay: RegimeDelay) -> dict[str, Any]:
    modelled = delay.status == "frozen"
    return {
        "transport_delay_modelled": modelled,
        "transport_delay_s": delay.seconds if modelled else None,
        "transport_delay_bracket_s": (
            list(delay.bracket_s) if modelled and delay.bracket_s is not None else None
        ),
        "transport_delay_note": delay.note if modelled else f"{DELAY_NOT_MODELLED}. {delay.note}",
    }


def bracket_ends(
    route: DeliveryRoute, declaration: LambdaDeliveryDeclaration
) -> tuple[float, float] | None:
    """The route's enrichment delay bracket, when it has a frozen delay with one."""
    delay = declaration.eligibility.regimes.delay("enrichment", route.target_source)
    if delay.status != "frozen" or delay.bracket_s is None:
        return None
    return (float(delay.bracket_s[0]), float(delay.bracket_s[1]))


def delay_sensitivity(
    chosen: DeliveryError, ends: dict[float, DeliveryError], chosen_delay_s: float
) -> dict[str, Any]:
    """The same result at each end of the delay bracket, and whether it holds.

    A frozen delay is a bracket, not a point (R05.16): a result is **unstable**
    when its band-averaged sign changes across the bracket, or when it exists
    at one end and not at another. It is reported, never smoothed over.
    """

    def entry(delay_s: float, result: DeliveryError) -> dict[str, Any]:
        return {
            "delay_s": delay_s,
            "n": result.n,
            "band_averaged_pct": result.band_averaged_pct if result.n else None,
            "pooled_pct": result.pooled_pct if result.n else None,
        }

    rows = [entry(chosen_delay_s, chosen), *(entry(d, r) for d, r in sorted(ends.items()))]
    values = [row["band_averaged_pct"] for row in rows if row["band_averaged_pct"] is not None]
    reason: str | None = None
    if any(row["n"] == 0 for row in rows) and any(row["n"] > 0 for row in rows):
        reason = "the result exists at some delays in the bracket and not at others"
    elif values and min(values) < 0 < max(values):
        reason = "the band-averaged sign changes across the delay bracket"
    return {
        "at": rows,
        "stable": reason is None,
        "reason": reason,
        "spread_pct": (max(values) - min(values)) if values else None,
    }


def pairing_block(route: DeliveryRoute, declaration: LambdaDeliveryDeclaration) -> dict[str, Any]:
    """What "same instant" means in a delivery result, stated rather than implied."""
    rule = declaration.eligibility.regimes.enrichment
    return {
        "method": "acquisition-time match_ordered",
        "max_separation_s": declaration.acquisition.max_separation_s,
        "target_source": route.target_source,
        "target_spec": declaration.commanded(route.target_source).spec,
        "measured_source": route.measured_source,
        "measured_spec": declaration.measured(route.measured_source).spec,
        # Delivery error is reported at enrichment, so the enrichment rule is
        # the one every surviving sample passed (R05.16).
        "regime": "enrichment",
        "steady_target_dwell_s": rule.steady_target_dwell_s,
        "target_step_tolerance": rule.target_step_tolerance,
        "post_fuel_cut_holdoff_s": rule.post_fuel_cut_holdoff_s,
        **_delay_fields(declaration.eligibility.regimes.delay("enrichment", route.target_source)),
        "constants_informed_by": list(rule.informed_by),
    }


#: Gate order after the per-sample states; each is reported by name.
_CONTEXT_GATES = (
    "not_enrichment",
    "rpm_missing",
    "fuel_status_missing",
    "closed_loop",
    "load_missing",
    "below_min_load",
    "outside_rpm_bands",
)


def gate_delivery_samples(
    inputs: DeliveryInputs,
    route: DeliveryRoute,
    *,
    declaration: LambdaDeliveryDeclaration,
    calibration: AfrCalibration | None,
    afr_stoich: float,
    rpm_bands: tuple[float, ...],
    min_load: float | None,
    session: Path | pd.DataFrame | None = None,
    mode01_exclusion_note: str | None = None,
    enrichment_delay_s: float | None = None,
) -> tuple[pd.DataFrame, GateReport]:
    """One session's surviving samples and the report of what removed the rest.

    Samples come from :func:`src.analysis.lambda_delivery.delivery_samples`:
    one row per commanded-target observation (and per fuel-cut sentinel),
    each paired with at most one measured read within the declaration's
    acquisition separation, each in exactly one named state. Only
    ``eligible`` rows — a steady target, outside the post-cut holdoff, a
    source valid in its regime, a measurement neither railed nor censored —
    carry a residual. Every other state is a named gate here, followed by:

    1. ``not_enrichment``: an eligible target at stoich (closed loop dilutes
       any fuelling effect toward zero by construction);
    2. ``rpm_missing`` / ``fuel_status_missing`` / ``load_missing``: no read
       of that context channel within the same separation;
    3. ``closed_loop``: the Mode 01 route only — ``FUEL_STATUS`` not open
       loop, where PID 0x44 is not the final target;
    4. ``below_min_load``, then ``outside_rpm_bands`` (bands are
       ``(lo, hi]``, so the gate keeps exactly what the bands bin).

    Gated per session on its own clock, so several sessions can be pooled
    and band-averaged once (:func:`delivery_error_from_gated`).
    ``enrichment_delay_s`` replaces the route's declared enrichment delay (the
    bracket-end check in :func:`delay_sensitivity`).
    """
    if min_load is not None and route.load_spec is None:
        raise GateError(
            "a load floor needs a validated load provider, and this route has none: "
            "calculated load is Mode 01 only. Clear min_load, or use the Mode 01 route."
        )
    policy = PairingPolicy.within(
        pd.Timedelta(seconds=declaration.acquisition.max_separation_s), reference="a"
    )
    _check_clock(inputs, policy)
    samples = delivery_samples(
        target=inputs.target,
        measured=inputs.measured,
        sentinel=inputs.sentinel,
        declaration=declaration,
        target_source=route.target_source,
        measured_source=route.measured_source,
        calibration=calibration,
        afr_stoich=afr_stoich,
        regime_delays_s=None if enrichment_delay_s is None else {"enrichment": enrichment_delay_s},
    )
    frame = samples.frame
    # At a bracket end (an override) no pair is an answer about that delay,
    # counted as measured_missing, not a refusal of the session.
    if enrichment_delay_s is None and len(frame) and frame["separation_s"].notna().sum() == 0:
        target_spec = declaration.commanded(route.target_source).spec
        measured_spec = declaration.measured(route.measured_source).spec
        _refuse_not_captured(
            session if session is not None else pd.DataFrame(),
            [target_spec, measured_spec],
            mode01_exclusion_note=mode01_exclusion_note,
            kind="not_co_observed",
        )
    times = to_seconds(frame["timestamp"])
    frame = frame.assign(
        rpm=_attach(times, inputs.rpm, policy),
        fuel_status=_attach(times, inputs.fuel_status, policy),
        load=_attach(times, inputs.load, policy),
    )
    report = GateReport(started=samples.counts["total"])
    for state in STATES:
        if state != ELIGIBLE:
            report.drop(state, samples.counts.get(state, 0), 0)
    values = frame[frame["state"] == ELIGIBLE]

    masks: dict[str, Any] = {
        "not_enrichment": lambda v: v["regime"] == "enrichment",
        "rpm_missing": lambda v: v["rpm"].notna(),
    }
    if route.fuel_status_spec is not None:
        masks["fuel_status_missing"] = lambda v: v["fuel_status"].notna()
        masks["closed_loop"] = lambda v: v["fuel_status"].eq(OPEN_LOOP)
    if min_load is not None:
        masks["load_missing"] = lambda v: v["load"].notna()
        masks["below_min_load"] = lambda v: v["load"] > min_load
    masks["outside_rpm_bands"] = lambda v: (v["rpm"] > rpm_bands[0]) & (v["rpm"] <= rpm_bands[-1])
    for gate in _CONTEXT_GATES:
        keep = masks.get(gate)
        if keep is None:
            continue
        before = len(values)
        values = values[keep(values)]
        report.drop(gate, before, len(values))
    report.remaining = len(values)
    out = pd.DataFrame(
        {
            "timestamp": values["timestamp"],
            "rpm": values["rpm"].astype(float),
            "commanded_afr": values["target_lambda"].astype(float) * afr_stoich,
            "measured_afr": values["measured_lambda"].astype(float) * afr_stoich,
            "error_pct": values["residual_pct"].astype(float),
            "separation_s": values["separation_s"].astype(float),
        }
    ).reset_index(drop=True)
    return out, report


def delivery_error_from_gated(
    values: pd.DataFrame,
    report: GateReport,
    *,
    measured_spec: str,
    rpm_bands: tuple[float, ...],
    pairing: dict[str, Any] | None = None,
    tune_revision: str | None = None,
    tune_revision_source: str = "unknown",
) -> DeliveryError:
    """Band-average already-gated samples (one session or a pool of them).

    ``error_pct`` is the one delivery residual (B06.4,
    :func:`src.analysis.lambda_delivery.residual_pct`): positive = leaner than
    commanded. Each band reports its per-sample spread (``p05_pct`` /
    ``p95_pct``) beside its mean.
    """
    columns = ["rpm_band", "n", "commanded", "measured", "error_pct", "p05_pct", "p95_pct"]
    if values.empty:
        return DeliveryError(
            bands=pd.DataFrame(columns=columns),
            band_averaged_pct=float("nan"),
            pooled_pct=float("nan"),
            n=0,
            gates=report.as_dict(),
            rpm_window=(float("nan"), float("nan")),
            measured_spec=measured_spec,
            tune_revision=tune_revision,
            tune_revision_source=tune_revision_source,
            pairing=dict(pairing or {}),
        )
    binned = values.groupby(pd.cut(values["rpm"], list(rpm_bands)), observed=True)
    bands = binned.agg(
        n=("error_pct", "size"),
        commanded=("commanded_afr", "mean"),
        measured=("measured_afr", "mean"),
        error_pct=("error_pct", "mean"),
        p05_pct=("error_pct", lambda v: float(np.quantile(v, 0.05))),
        p95_pct=("error_pct", lambda v: float(np.quantile(v, 0.95))),
    ).reset_index(names="rpm_band")
    bands["rpm_band"] = bands["rpm_band"].astype(str)
    return DeliveryError(
        bands=bands.loc[:, columns],
        band_averaged_pct=float(bands["error_pct"].mean()),
        pooled_pct=float(values["error_pct"].mean()),
        n=int(len(values)),
        gates=report.as_dict(),
        rpm_window=(float(values["rpm"].min()), float(values["rpm"].max())),
        measured_spec=measured_spec,
        tune_revision=tune_revision,
        tune_revision_source=tune_revision_source,
        pairing=dict(pairing or {}),
    )


def delivery_error(
    session: Path | pd.DataFrame,
    *,
    declaration: LambdaDeliveryDeclaration | None,
    calibration: AfrCalibration | None,
    afr_stoich: float,
    rpm_bands: tuple[float, ...],
    min_load: float | None,
    measured_spec: str = "obd:O2_WR_LAMBDA_S1",
    route: DeliveryRoute | None = None,
    revisions: TuneRevisionRegistry | None = None,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    mode01_exclusion_note: str | None = None,
) -> DeliveryError:
    """Delivery error at enrichment, with every gate applied.

    ``route`` defaults to the Mode 01 route (:func:`mode01_route`) with
    ``measured_spec``; the all-DID path passes :func:`semantic_route`.
    ``measured_spec`` is a parameter on purpose: the ECM's own λ (PID 0x24)
    and the A/F sensor voltage path disagree by ~3.8 % at WOT (2026-08-05,
    commit 78592f4), so this function must not quietly pick a side — the
    caller names the source and the answer carries it.

    Pairing is by acquisition time (R05.16): each commanded-target read meets
    at most one measured read within the declaration's separation, and only
    steady-target samples carry a residual. It replaced a 1 s last-value grid
    whose cells joined a target and a measurement up to a second apart, so a
    target that stepped inside the cell was scored as a delivery error.
    """
    if declaration is None:
        raise GateError(
            "this vehicle has no lambda_delivery.yaml, so nothing declares which channel is "
            "the commanded target, what quantity each source is, or how fuel cut shows. "
            "Delivery error is refused rather than guessed."
        )
    chosen = route or mode01_route(declaration, measured_spec=measured_spec, min_load=min_load)
    inputs = read_delivery_inputs(
        session,
        chosen,
        declaration,
        calibration=calibration,
        afr_stoich=afr_stoich,
        bindings_path=bindings_path,
        dids_path=dids_path,
        mode01_exclusion_note=mode01_exclusion_note,
    )

    def gated(delay_s: float | None) -> tuple[pd.DataFrame, GateReport]:
        return gate_delivery_samples(
            inputs,
            chosen,
            declaration=declaration,
            calibration=calibration,
            afr_stoich=afr_stoich,
            rpm_bands=rpm_bands,
            min_load=min_load,
            session=session,
            mode01_exclusion_note=mode01_exclusion_note,
            enrichment_delay_s=delay_s,
        )

    values, report = gated(None)
    revision = None
    revision_source = "unknown"
    if isinstance(session, Path):
        revision, revision_source, _conflict = resolve_session_revision(session, revisions)
    pairing = pairing_block(chosen, declaration)
    if isinstance(session, Path):
        # Not independent evidence: this drive chose the enrichment constants.
        pairing["session_informed_constants"] = session.name in pairing["constants_informed_by"]
    result = delivery_error_from_gated(
        values,
        report,
        measured_spec=chosen.measured_label,
        rpm_bands=rpm_bands,
        pairing=pairing,
        tune_revision=revision,
        tune_revision_source=revision_source,
    )
    ends = bracket_ends(chosen, declaration)
    if ends is not None:
        at_ends = {
            end: delivery_error_from_gated(
                *gated(end), measured_spec=chosen.measured_label, rpm_bands=rpm_bands
            )
            for end in ends
        }
        result.delay_sensitivity = delay_sensitivity(
            result, at_ends, float(pairing["transport_delay_s"])
        )
    return result


@dataclass
class SessionComparison:
    """Per-cell deltas between two sessions on a matched grid."""

    cells: pd.DataFrame
    mean_delta: float
    n_cells: int
    axes: tuple[str, ...]
    value: str
    #: Calibrations in force for each side. Equal revisions mean this is a
    #: null run (the spread IS the noise floor); different ones mean the
    #: delta includes whatever the flash changed.
    tune_revisions: tuple[str | None, str | None] = (None, None)
    tune_revision_sources: tuple[str, str] = ("unknown", "unknown")
    tune_revision_conflicts: tuple[bool, bool] = (False, False)
    #: Regime the quoted floor was measured in. A floor is only valid for its
    #: own conditions — the same null pair gives 0.48 deg of spark spread at
    #: WOT and 7.7 deg across all conditions — so this travels with the sigma
    #: figures rather than being assumed to match.
    floor_conditions: str | None = None
    #: The regime the caller declared this comparison was made under.
    comparison_conditions: str | None = None
    #: Why the cells carry no sigma, when a floor was asked for and refused
    #: (:class:`~src.analysis.noise_floor.FloorRefusal`), else ``None``.
    floor_refusal: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tune_revisions": list(self.tune_revisions),
            "tune_revision_sources": list(self.tune_revision_sources),
            "tune_revision_conflicts": list(self.tune_revision_conflicts),
            "floor_conditions": self.floor_conditions,
            "comparison_conditions": self.comparison_conditions,
            "floor_refusal": self.floor_refusal,
            "same_revision": (
                self.tune_revisions[0] == self.tune_revisions[1]
                if self.tune_revisions[0] is not None and self.tune_revisions[1] is not None
                else None
            ),
            "value": self.value,
            "axes": list(self.axes),
            "mean_delta": self.mean_delta,
            "n_cells": self.n_cells,
            "cells": self.cells.to_dict(orient="records"),
        }


def _cell_labels(values: pd.Series, edges: Sequence[float]) -> pd.Series:
    """Each value's cell under the one bin rule (:func:`src.decode.surface_eval.bin_indices`).

    Labels read ``[lo, hi)`` (the last cell ``[lo, hi]``). Values outside the
    edges get no cell. R05.10: this used right-closed ``pd.cut``, under which
    the minimum of auto-computed ``linspace`` edges fell in no cell and every
    sample at the lowest operating point was silently dropped.
    """
    edge_array = np.asarray(edges, dtype=float)
    numeric = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    inside = np.isfinite(numeric) & (numeric >= edge_array[0]) & (numeric <= edge_array[-1])
    index = bin_indices(np.where(inside, numeric, edge_array[0]), edge_array)
    last = len(edge_array) - 2
    names = [
        str(pd.Interval(edge_array[i], edge_array[i + 1], closed="both" if i == last else "left"))
        for i in range(last + 1)
    ]
    labels = [names[i] if ok else None for i, ok in zip(index, inside, strict=True)]
    return pd.Series(labels, index=values.index, dtype=object)


def compare_sessions(
    a: Path | pd.DataFrame,
    b: Path | pd.DataFrame,
    *,
    value: str,
    axes: tuple[str, ...] = ("obd:RPM", "obd:MAF"),
    bins: tuple[tuple[float, ...], ...] | None = None,
    min_cell_samples: int = 4,
    unsafe_axes: bool = False,
    grid: str = "1s",
    calibration: AfrCalibration | None = None,
    afr_stoich: float,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    noise_floor: NoiseFloor | None = None,
    floor_channel: str | None = None,
    conditions: str | None = None,
    revisions: TuneRevisionRegistry | None = None,
) -> SessionComparison:
    """A-vs-B on a matched grid, refusing load as an axis.

    ``noise_floor`` (with ``floor_channel``) gates the deltas through
    :meth:`~src.analysis.noise_floor.NoiseFloor.significance`, which needs the
    regime this comparison was made under as ``conditions``. The floor carries
    the regime it was measured in, and that regime is part of the measurement
    — the same null pair gives 0.48 deg of spark spread at WOT and 7.7 deg
    across all conditions, so quoting one against the other understates noise
    by ~16x. A mismatch is refused and reported in ``floor_refusal``; without
    any floor the sigma columns read ``unavailable`` rather than an invented
    significance. (A bare float floor is no longer accepted: it has no regime.)
    """
    for axis in axes:
        _assert_axis_allowed(axis, unsafe=unsafe_axes)
    if isinstance(noise_floor, NoiseFloor) and floor_channel is None:
        raise GateError(
            "a NoiseFloor was given without floor_channel — name the channel whose "
            "spread should gate these deltas. Picking one silently would also pick "
            "the regime it was measured in, and a floor is only valid for its own."
        )
    if noise_floor is not None and conditions is None:
        raise GateError(
            "a NoiseFloor was given without conditions — state the regime this "
            "comparison was made under. The floor is refused unless it was measured "
            "under the same one (R05.2); a WOT floor quoted against idle cells "
            "understates noise ~16x."
        )

    specs = [value, *axes]
    read = dict(
        grid=grid,
        require_all=True,
        calibration=calibration,
        afr_stoich=afr_stoich,
        bindings_path=bindings_path,
        dids_path=dids_path,
    )
    frame_a = read_aligned(a, specs, **read)  # type: ignore[arg-type]
    frame_b = read_aligned(b, specs, **read)  # type: ignore[arg-type]

    for name, frame in (("a", frame_a), ("b", frame_b)):
        if frame.empty:
            raise GateError(
                f"session {name} has no rows where every one of {specs} is fresh in the "
                "same cell — either a channel is absent from that capture, or the two "
                "were never sampled together. Check that before widening the grid."
            )

    if bins is None:
        merged = pd.concat([frame_a, frame_b], ignore_index=True)
        edges: list[tuple[float, ...]] = []
        for axis in axes:
            lo, hi = float(merged[axis].min()), float(merged[axis].max())
            if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
                raise GateError(
                    f"axis {axis!r} has no usable spread across these sessions "
                    f"(min={lo}, max={hi}), so it cannot bin anything. Pick an axis that "
                    "varies, or pass explicit bins."
                )
            edges.append(tuple(np.linspace(lo, hi, 7)))
        bins = tuple(edges)

    def cell_frame(frame: pd.DataFrame) -> pd.DataFrame:
        keys = [
            _cell_labels(frame[axis], edges).rename(axis)
            for axis, edges in zip(axes, bins, strict=True)
        ]
        return frame.groupby(keys, observed=True).agg(n=(value, "size"), mean=(value, "mean"))

    cells_a, cells_b = cell_frame(frame_a), cell_frame(frame_b)
    joined = cells_a.join(cells_b, how="inner", lsuffix="_a", rsuffix="_b")
    joined = joined[(joined["n_a"] >= min_cell_samples) & (joined["n_b"] >= min_cell_samples)]
    joined = joined.assign(delta=joined["mean_b"] - joined["mean_a"])

    floor_conditions: str | None = None
    floor_refusal: dict[str, Any] | None = None
    joined["delta_sigma"] = np.nan
    joined["distinguishable"] = pd.NA
    if noise_floor is not None and floor_channel is not None and conditions is not None:
        entry = noise_floor.channels.get(floor_channel)
        floor_conditions = entry.conditions if entry is not None else None
        verdict = noise_floor.significance(floor_channel, joined["delta"], conditions=conditions)
        if isinstance(verdict, FloorRefusal):
            floor_refusal = verdict.as_dict()
        else:
            joined["delta_sigma"] = verdict.delta_sigma
            joined["distinguishable"] = verdict.distinguishable

    def _revision(session: Path | pd.DataFrame) -> tuple[str | None, str, bool]:
        if not isinstance(session, Path):
            return None, "unknown", False
        return resolve_session_revision(session, revisions)

    revision_a = _revision(a)
    revision_b = _revision(b)

    return SessionComparison(
        tune_revisions=(revision_a[0], revision_b[0]),
        tune_revision_sources=(revision_a[1], revision_b[1]),
        tune_revision_conflicts=(revision_a[2], revision_b[2]),
        floor_conditions=floor_conditions,
        comparison_conditions=conditions,
        floor_refusal=floor_refusal,
        cells=joined.reset_index(),
        mean_delta=float(joined["delta"].mean()) if len(joined) else float("nan"),
        n_cells=int(len(joined)),
        axes=tuple(axes),
        value=value,
    )


__all__ = [
    "DELIVERY_ENGINE_VERSION",
    "EQUIV_RATIO_SPEC",
    "FORBIDDEN_AXES",
    "DeliveryError",
    "DeliveryInputs",
    "DeliveryRoute",
    "GateError",
    "GateReport",
    "InputsNotCapturedError",
    "SessionComparison",
    "DELAY_NOT_MODELLED",
    "bracket_ends",
    "compare_sessions",
    "delay_sensitivity",
    "delivery_error",
    "delivery_error_from_gated",
    "gate_delivery_samples",
    "mode01_route",
    "pairing_block",
    "read_delivery_inputs",
    "semantic_route",
]
