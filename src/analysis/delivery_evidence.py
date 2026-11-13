"""Mint E01 evidence from delivery error and fuel-pressure droop (E03.1, E03.2).

:func:`mint_delivery_error` runs B06's :func:`~src.analysis.tuning.delivery_error`
(the Mode 01 route, the profile's WOT load floor and rpm bands) on a stored
session and writes one :class:`~src.evidence.records.Evidence` record;
:func:`mint_fuel_pressure_droop` mints the fuel-pressure residuals of the
vehicle's fault model (``fault_hypotheses.yaml``): closed-loop droop at high
airflow, and the injector flow lost to droop at the rows B06's enrichment
eligibility admits — the observation that can refute delivery sag. Neither
rebuilds delivery analysis: both consume B06's eligibility, lag and gates
exactly as the MCP tool and the digest do.

What the records say is fixed here, not by the caller:

* **Engine response, not ECU computation.** Delivery error compares the
  measured mixture with the ECM's command; the target-phi twin (E02/E05)
  predicts the command itself. ``labels["evidence_class"]`` says which, and
  the investigation refuses the other class (``delivery_investigation``).
* **Which kind of empty.** Commanded AFR never captured is ``missing_input``
  (with the capture-profile cause when the manifest names it); captured but
  never co-observed within the acquisition separation is ``stale_input``; the
  gates leaving no eligible enrichment row is ``uncovered`` with every gate's
  count. None of them carries a residual, so none can read as a zero error.
* **A sign is qualified or absent.** ``labels["sign"]`` is written only
  against the regime's condition-matched floor (B06's ``regime_noise_floor``),
  only for a delay-stable result, and **never for a drive that informed the
  enrichment constants** (``lambda_delivery.yaml`` enrichment
  ``informed_by``): its residual is in-sample, so the record carries
  ``labels["sign_refused"]`` instead and no sign exists to qualify a
  discrepancy (the same independence rule B06's ``LeakedDriveError`` enforces).
* **Which channels the drive had.** ``labels["fault_model_channels"]`` lists
  the fault model's channels the session holds, so the structural analysis is
  computed per drive on that drive's own channels.

Re-minting the same session supersedes through the ledger (subject over the
same data); the same incident on another drive is a recurrence.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import duckdb
import numpy as np
import pandas as pd

import src.analysis.channels as channels_module
import src.analysis.channels_align as channels_align_module
import src.analysis.channels_plan as channels_plan_module
import src.analysis.fault_model as fault_model_module
import src.analysis.lambda_delivery as lambda_delivery_module
import src.analysis.tuning as tuning_module
from src.analysis.channels import read_channels
from src.analysis.compare_evidence import comparison_identity
from src.analysis.fault_model import FaultModel, observed_sign
from src.analysis.lambda_delivery import LambdaDeliveryDeclaration, regime_noise_floor
from src.analysis.tuning import (
    EQUIV_RATIO_SPEC,
    DeliveryError,
    GateError,
    InputsNotCapturedError,
    delivery_error,
    gate_delivery_samples,
    mode01_route,
    read_delivery_inputs,
)
from src.analysis.tuning_context import TuningContext
from src.artifact_hash import sha256_file
from src.evidence.ledger import MemoryLedger
from src.evidence.provenance import code_version, hash_json, hash_session_files
from src.evidence.records import (
    DETECTABLE_SIGMA,
    Conditions,
    Evidence,
    Provenance,
    Quality,
    Quantity,
    Result,
    SourceRef,
    UnevaluableReason,
    VehicleIdentity,
)
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.signals.pairing import to_seconds
from src.storage.session_files import session_files_fingerprint, session_footer_span
from src.storage.sessions import session_started_at

__all__ = [
    "CLOSED_LOOP_STATUS",
    "DELIVERY_INCIDENT",
    "DELIVERY_QUANTITY",
    "DELIVERY_REGION",
    "DELIVERY_TOOL",
    "DELIVERY_TOOL_VERSION",
    "DROOP_INCIDENT",
    "DROOP_LIMITATIONS",
    "DROOP_QUANTITY",
    "DROOP_TOOL",
    "DROOP_TOOL_VERSION",
    "ENGINE_RESPONSE",
    "FLOW_LOSS_QUANTITY",
    "INFORMED_REFUSAL",
    "MANIFOLD_PRESSURE_SPECS",
    "drive_informed_constants",
    "mint_delivery_error",
    "mint_fuel_pressure_droop",
    "session_specs",
]

DELIVERY_TOOL = "lambda_delivery_error"
#: Bump when the mapping from a delivery result to evidence changes.
DELIVERY_TOOL_VERSION = "2"
#: The recurring phenomenon: commanded-versus-measured lambda at enrichment.
DELIVERY_INCIDENT = "lambda_delivery:enrichment"
DELIVERY_QUANTITY = "delivery_error_pct"
#: The fault model's residual and region this evidence observes.
DELIVERY_RESIDUAL = "delivery_error"
DELIVERY_REGION = "enrichment"

DROOP_TOOL = "fuel_pressure_droop"
#: 3: ``labels["limitations"]``, the induction refusals, and standard errors
#: from non-overlapping pressure windows (M01.14).
DROOP_TOOL_VERSION = "3"
DROOP_INCIDENT = "fuel_pressure:droop_with_airflow"
#: Closed-loop high-airflow droop (``fuel_pressure_droop@high_airflow``).
DROOP_QUANTITY = "fuel_pressure_droop_pct"
DROOP_RESIDUAL = "fuel_pressure_droop"
#: Injector flow lost to droop at enrichment (``pressure_flow_loss@enrichment``).
FLOW_LOSS_QUANTITY = "fuel_pressure_flow_loss_pct"
FLOW_LOSS_RESIDUAL = "pressure_flow_loss"

#: ``labels["limitations"]`` on every fuel-pressure record, whatever its result:
#: what the number does not contain (M01.14).
DROOP_LIMITATIONS = (
    "manifold pressure is not in it: the value is a ratio of rail gauge pressures, "
    "while injector flow follows the pressure across the injector (rail gauge + baro - "
    "manifold). It equals that ratio only where manifold pressure equals baro in both "
    "groups; on a naturally aspirated engine it answers whether the pump sagged, to "
    "within a fraction of a point. Under boost a gauge-referenced rail loses flow it "
    "does not show (38 kPa of boost on a flat 350 kPa rail: flow -5.6 %, this 0 %)"
)

#: ``labels["evidence_class"]``: what a residual is a disagreement *of*.
ENGINE_RESPONSE = "engine_response"

PRESSURE_SPEC = "sensor:FUEL_PRESSURE"
#: Manifold absolute pressure, under either name the derived channels read it
#: by (``src.signals.derived``): the Mode 01 PID 0x0B or a decoded broadcast.
MANIFOLD_PRESSURE_SPECS: tuple[str, ...] = ("obd:INTAKE_PRESSURE", "decoded:MAP")
MAF_SPEC = "obd:MAF"
FUEL_STATUS_SPEC = "obd:FUEL_STATUS"
#: SAE J1979 PID 0x03 fuel system status: 2 = closed loop using the oxygen
#: sensor (4 = open loop for load, :data:`src.analysis.tuning.OPEN_LOOP`).
CLOSED_LOOP_STATUS = 2.0

#: Why an in-sample drive's residual carries no sign.
INFORMED_REFUSAL = (
    "this drive informed the enrichment eligibility constants "
    "(lambda_delivery.yaml enrichment informed_by): its residual is in-sample, not "
    "independent evidence of a discrepancy"
)


def _aware(stamp: pd.Timestamp | datetime) -> datetime:
    value = pd.Timestamp(stamp)
    value = value.tz_localize(UTC) if value.tzinfo is None else value.tz_convert(UTC)
    return value.floor("us").to_pydatetime()


def _session_window(session: Path) -> tuple[datetime, datetime]:
    span = session_footer_span(session)
    if span.first is not None and span.last is not None:
        return _aware(span.first), _aware(span.last)
    started = session_started_at(session.name)
    if started is None:
        raise ValueError(f"{session.name}: no stored timestamps and no parseable start")
    return _aware(started), _aware(started)


def session_specs(session: Path) -> frozenset[str]:
    """Every ``source:channel`` spec stored in ``session`` (a channel already
    namespaced, like ``mem:<NAME>``, is kept as it is). Two columns only."""
    files = sorted(Path(session).glob("telemetry-*.parquet"))
    if not files:
        return frozenset()
    rows = duckdb.sql(
        "SELECT DISTINCT source, channel FROM read_parquet($files)",
        params={"files": [str(f) for f in files]},
    ).fetchall()
    return frozenset(
        str(channel) if ":" in str(channel) else f"{source}:{channel}" for source, channel in rows
    )


def _model_channels(model: FaultModel, session: Path) -> str:
    """The fault model's channels this session holds, comma-joined."""
    wanted = {spec for r in model.residuals for group in r.channel_sets for spec in group}
    return ",".join(sorted(wanted & session_specs(session)))


def drive_informed_constants(declaration: LambdaDeliveryDeclaration | None, session: Path) -> bool:
    """Whether ``session`` chose B06's enrichment eligibility constants."""
    if declaration is None:
        return False
    return Path(session).name in declaration.eligibility.regimes.enrichment.informed_by


def _hash_model(obj: Any) -> str:
    dump = getattr(obj, "model_dump", None)
    return hash_json(dump(mode="json") if callable(dump) else repr(obj))


def _band_key(label: str) -> str:
    return "band:" + label.strip("([])").replace(", ", "-")


def _delivery_values(result: DeliveryError) -> dict[str, Quantity]:
    values = {
        "band_averaged": Quantity(value=result.band_averaged_pct, unit="percent"),
        "pooled": Quantity(value=result.pooled_pct, unit="percent"),
        "eligible_rows": Quantity(value=float(result.n), unit="1"),
    }
    for row in result.bands.to_dict(orient="records"):
        values[_band_key(str(row["rpm_band"]))] = Quantity(
            value=float(row["error_pct"]), unit="percent"
        )
    sensitivity = result.delay_sensitivity
    if sensitivity is not None and sensitivity.get("spread_pct") is not None:
        values["delay_bracket_spread"] = Quantity(
            value=float(sensitivity["spread_pct"]), unit="percent"
        )
    return values


class _Minter:
    """One evidence subject's shared envelope; ``record`` appends one result."""

    def __init__(
        self,
        ledger: MemoryLedger,
        session: Path,
        *,
        vehicle: VehicleIdentity,
        provenance: Provenance,
        conditions: Conditions,
        subject: str,
        incident: str,
        specs: Sequence[str],
        inputs: dict[str, str],
        policy: dict[str, Any],
        labels: dict[str, str],
        flags: tuple[str, ...],
        timestamp_basis: str,
        now: datetime | None,
    ) -> None:
        self.ledger = ledger
        self.session = session
        self.vehicle = vehicle
        self.provenance = provenance
        self.conditions = conditions
        self.subject = subject
        self.incident = incident
        self.specs = tuple(specs)
        self.inputs = inputs
        self.policy = policy
        self.labels = labels
        self.flags = flags
        self.timestamp_basis = timestamp_basis
        self.now = now
        self.window = _session_window(session)

    def record(
        self,
        result: Result,
        *,
        values: dict[str, Quantity] | None = None,
        n: int = 0,
        extra: dict[str, str] | None = None,
        window: tuple[datetime, datetime] | None = None,
    ) -> str:
        start, end = window or self.window
        evidence = Evidence(
            vehicle=self.vehicle,
            provenance=self.provenance,
            conditions=self.conditions,
            valid_from=start,
            valid_to=end,
            subject=self.subject,
            incident=self.incident,
            sources=(
                SourceRef(
                    session=self.session.name,
                    window_start=start,
                    window_end=end,
                    channels=self.specs,
                    n_samples=n,
                ),
            ),
            input_hashes=self.inputs,
            policy=self.policy,
            values=values or {},
            labels={**self.labels, **(extra or {})},
            quality=Quality(
                n_observations=n, timestamp_basis=self.timestamp_basis, flags=self.flags
            ),
            result=result,
        )
        return self.ledger.append(evidence, now=self.now).id

    def unevaluable(self, reason: UnevaluableReason, detail: str, kind: str) -> str:
        return self.record(
            Result(status="unevaluable", reason=reason, detail=detail),
            extra={"empty_kind": kind},
        )


def _undeclared(context: TuningContext) -> list[str]:
    return [
        name
        for name, value in (
            ("lambda_delivery.yaml", context.lambda_delivery),
            ("tuning_providers.yaml mode01_wot", context.mode01_wot),
            ("afr_calibration.yaml", context.calibration),
        )
        if value is None
    ]


def _not_captured(minter: _Minter, refusal: InputsNotCapturedError) -> str:
    if refusal.kind == "not_captured":
        return minter.unevaluable("missing_input", str(refusal), "not_captured")
    return minter.unevaluable("stale_input", str(refusal), "not_co_observed")


def mint_delivery_error(
    ledger: MemoryLedger,
    session: Path,
    context: TuningContext,
    model: FaultModel,
    model_sha256: str,
    *,
    vin: str | None,
    program_id: str | None,
    measured_spec: str = "obd:O2_WR_LAMBDA_S1",
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    now: datetime | None = None,
) -> str:
    """Delivery error at enrichment on ``session`` as evidence; returns its id.

    The Mode 01 route at the profile's ``mode01_wot`` load floor and rpm
    bands, as the MCP ``get_delivery_error`` default. The record's regime is
    the gate set it actually applied, stated by this tool. ``now`` is the
    ledger's record time (tests replaying a timeline; ``None``: the clock).
    """
    session = Path(session)
    vehicle, calibration_conflict = comparison_identity(
        ledger.vehicle, session, vin=vin, program_id=program_id, tune_registry=context.registry
    )
    declaration = context.lambda_delivery
    wot = context.mode01_wot
    informed = drive_informed_constants(declaration, session)
    policy: dict[str, Any] = {
        "route": "mode01",
        "measured_spec": measured_spec,
        "min_load_pct": None if wot is None else wot.load_floor_pct,
        "rpm_bands": None if wot is None else list(wot.rpm_bands),
        "sign_sigma": DETECTABLE_SIGMA,
        "fault_model_version": model.version,
    }
    minter = _Minter(
        ledger,
        session,
        vehicle=vehicle,
        provenance=Provenance(
            producer=DELIVERY_TOOL,
            producer_kind="tool",
            version=DELIVERY_TOOL_VERSION,
            code_version=code_version(
                sys.modules[__name__],
                tuning_module,
                lambda_delivery_module,
                channels_module,
                channels_plan_module,
                channels_align_module,
            ),
        ),
        conditions=Conditions(
            regime=(
                "enrichment: lambda_delivery.yaml enrichment eligibility, Mode 01 open loop, "
                f"load above {policy['min_load_pct']} percent, rpm bands {policy['rpm_bands']}"
            ),
            vehicle_state="engine_running",
            operating_mode=DELIVERY_REGION,
        ),
        subject=f"{DELIVERY_TOOL}:{session.name}:mode01:{measured_spec}:{hash_json(policy)[:16]}",
        incident=DELIVERY_INCIDENT,
        specs=(EQUIV_RATIO_SPEC, measured_spec),
        inputs={
            "session_files": hash_session_files(session_files_fingerprint(session)),
            "lambda_delivery": _hash_model(declaration),
            "afr_calibration": _hash_model(context.calibration),
            "noise_floor": _hash_model(context.noise_floor),
            "fault_model": model_sha256,
            "bindings": sha256_file(bindings_path),
            "dids": sha256_file(dids_path),
        },
        policy=policy,
        labels={
            "evidence_class": ENGINE_RESPONSE,
            "residual": DELIVERY_RESIDUAL,
            "region": DELIVERY_REGION,
            "quantity": DELIVERY_QUANTITY,
            "measured_spec": measured_spec,
            "session_informed_constants": str(informed).lower(),
            "fault_model_version": str(model.version),
            "fault_model_channels": _model_channels(model, session),
        },
        flags=(
            *(("calibration_conflict",) if calibration_conflict else ()),
            *(("informed_constants",) if informed else ()),
        ),
        timestamp_basis="acquired",
        now=now,
    )
    if declaration is None or wot is None or context.calibration is None:
        return minter.unevaluable(
            "unknown_context",
            "this vehicle declares no "
            + ", ".join(_undeclared(context))
            + "; delivery error is refused",
            "not_declared",
        )
    try:
        result = delivery_error(
            session,
            declaration=declaration,
            calibration=context.calibration,
            rpm_bands=wot.rpm_bands,
            min_load=wot.load_floor_pct,
            measured_spec=measured_spec,
            revisions=context.registry,
            bindings_path=bindings_path,
            dids_path=dids_path,
            mode01_exclusion_note=context.mode01_exclusion_note,
        )
    except InputsNotCapturedError as refusal:
        return _not_captured(minter, refusal)
    except GateError as refusal:
        return minter.unevaluable("refused", str(refusal), "refused")

    if result.n == 0:
        filtered = result.gates.get("filtered", {})
        detail = (
            "no commanded-target read survived the delivery gates at enrichment: the "
            "drive did not reach the region, which is not a zero error. Removed: "
            + ", ".join(f"{gate} {count}" for gate, count in filtered.items())
        )
        return minter.unevaluable("uncovered", detail, "no_eligible_rows")

    extra: dict[str, str] = {"tune_revision": str(result.tune_revision)}
    values = _delivery_values(result)
    floor = regime_noise_floor(context.noise_floor, DELIVERY_REGION, declaration)
    sensitivity = result.delay_sensitivity
    if floor.get("available"):
        threshold = DETECTABLE_SIGMA * float(floor["sd_pct"])
        values["floor_sd"] = Quantity(value=float(floor["sd_pct"]), unit="percent")
        values["detectable"] = Quantity(value=threshold, unit="percent")
        extra["floor_conditions"] = str(floor["conditions"])
    if informed:
        extra["sign_refused"] = INFORMED_REFUSAL
    elif not floor.get("available"):
        extra["sign_refused"] = f"no condition-matched floor: {floor.get('reason')}"
    elif sensitivity is not None and not sensitivity.get("stable", True):
        extra["sign_refused"] = f"unstable across the delay bracket: {sensitivity.get('reason')}"
    else:
        extra["sign"] = observed_sign(result.band_averaged_pct, values["detectable"].value)
    return minter.record(
        Result(
            status="evaluated",
            residual=Quantity(value=result.band_averaged_pct, unit="percent"),
        ),
        values=values,
        n=result.n,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Fuel pressure


def _window_means(
    times: np.ndarray, values: np.ndarray, at: np.ndarray, half: float
) -> tuple[np.ndarray, np.ndarray]:
    """Mean of ``values`` within ``±half`` s of each ``at``, and the count."""
    total = np.concatenate([[0.0], np.cumsum(values)])
    lo = np.searchsorted(times, at - half, side="left")
    hi = np.searchsorted(times, at + half, side="right")
    count = hi - lo
    mean = np.where(count > 0, (total[hi] - total[lo]) / np.maximum(count, 1), np.nan)
    return mean, count


def _non_overlapping(times: np.ndarray, half: float) -> np.ndarray:
    """Indices of ``times`` whose ``±half`` windows share no sample.

    Greedy in time order: a read is kept when it lies more than ``2 * half``
    after the last one kept (:func:`_window_means` includes both window ends,
    so windows exactly ``2 * half`` apart would share an edge sample).
    """
    order = np.argsort(times, kind="stable")
    kept: list[int] = []
    last = -np.inf
    for i in order:
        if times[i] > last + 2.0 * half:
            kept.append(int(i))
            last = float(times[i])
    return np.asarray(kept, dtype=int)


def _median_se(times: np.ndarray, values: np.ndarray, half: float) -> float:
    """Standard error of the median of window means ``values`` taken at ``times``.

    Normal approximation, 1.2533 sd / sqrt n, over **non-overlapping windows
    only** (M01.14): MAF reads at 4-15 Hz put their ±0.25 s windows 60-90 %
    over one another, so neighbouring means share most of their samples and
    counting each as independent made the error about 2x too tight. Windows
    that share no sample are independent while the pressure noise decorrelates
    within a window (the 100 Hz trace's aliased pulsation does). Deterministic,
    unlike a block bootstrap, so re-minting a drive reproduces its record; per
    pull would leave a drive a handful of values. The median itself is still
    taken over every read.
    """
    independent = values[_non_overlapping(times, half)]
    if len(independent) < 2:
        return float("inf")
    return float(1.2533 * np.std(independent, ddof=1) / np.sqrt(len(independent)))


def _long(frame: pd.DataFrame, spec: str) -> tuple[np.ndarray, np.ndarray, pd.Series]:
    rows = frame[frame["spec"] == spec].sort_values("timestamp", kind="stable")
    stamps = pd.to_datetime(rows["timestamp"], utc=True)
    times = np.asarray(to_seconds(stamps), dtype=float)
    values = pd.to_numeric(rows["value"], errors="coerce").to_numpy(dtype=float)
    return times, values, stamps


def _bracketed_state(
    times: np.ndarray, state_t: np.ndarray, state_v: np.ndarray, within: float
) -> np.ndarray:
    """A polled state at each time, only where it is bracketed.

    Fuel status is polled far slower than MAF. A time takes the status only
    when the reads just before and just after it agree and both lie within
    ``within`` seconds — a held state, never one carried across a transition
    or a gap (NaN otherwise).
    """
    out = np.full(len(times), np.nan)
    if not len(times) or not len(state_t):
        return out
    after = np.searchsorted(state_t, times, side="left")
    before = np.searchsorted(state_t, times, side="right") - 1
    ok = (before >= 0) & (after < len(state_t))
    b, a = np.clip(before, 0, len(state_t) - 1), np.clip(after, 0, len(state_t) - 1)
    ok &= (times - state_t[b] <= within) & (state_t[a] - times <= within)
    ok &= state_v[b] == state_v[a]
    out[ok] = state_v[b][ok]
    return out


DroopRegion = Literal["high_airflow", "enrichment"]


def mint_fuel_pressure_droop(
    ledger: MemoryLedger,
    session: Path,
    context: TuningContext,
    model: FaultModel,
    model_sha256: str,
    *,
    region: DroopRegion,
    vin: str | None,
    program_id: str | None,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    now: datetime | None = None,
) -> str:
    """A fuel-pressure residual of the fault model in ``region``; evidence id.

    The reference is the closed-loop low-airflow pressure of the same drive:
    each MAF read with a closed-loop fuel status bracketed by agreeing status
    reads (fuel status is polled far slower than MAF) and the mean pressure
    within the declared window (the 100 Hz trace aliases injector pulsation,
    so single samples are not pressures).

    * ``high_airflow`` (``fuel_pressure_droop``): closed-loop MAF reads at or
      above the high-airflow limit. Value ``100 * (1 - median_high /
      median_low)``. The sign says only fell / rose beyond ``sigma`` standard
      errors plus the low-airflow drift between the drive's halves; no
      healthy-droop floor exists, so "steady" is never claimed.
    * ``enrichment`` (``pressure_flow_loss``): the rows B06's enrichment
      eligibility admits (:func:`~src.analysis.tuning.gate_delivery_samples`,
      the delivery-error gates). Value: injector flow lost to droop,
      ``100 * (1 - sqrt(median_enrichment / median_low))``. Its sign is
      qualified against the delivery error's condition-matched detectable
      change, with the same margin: ``+`` when the loss alone could produce a
      lean the delivery residual detects, ``0`` when it could not. A drive that
      informed the enrichment constants gets no sign.

    Policy comes from the fault model's residual, so the artifact and the
    evidence cannot disagree.

    Every record carries ``labels["limitations"]`` (:data:`DROOP_LIMITATIONS`):
    both values are ratios of rail *gauge* pressure, with no manifold pressure
    in them (M01.14). Hence two refusals before anything is read: a profile
    that declares no ``induction`` is ``unknown_context``, and one that
    declares ``forced`` is always refused: ``forced_induction_without_manifold_
    pressure`` when the session recorded no manifold-pressure channel
    (:data:`MANIFOLD_PRESSURE_SPECS`), ``forced_induction_delta_p_not_
    implemented`` when it did (the pressure across the injector is not
    computed yet). Standard errors count only
    non-overlapping pressure windows (:func:`_median_se`).
    """
    session = Path(session)
    residual_name = DROOP_RESIDUAL if region == "high_airflow" else FLOW_LOSS_RESIDUAL
    residual = model.residual(residual_name)
    if region not in residual.regions:
        raise ValueError(f"{residual_name} is not defined in region {region}")
    policy = dict(residual.policy)
    for key in (
        "window_s",
        "min_samples_per_window",
        "running_min_maf_gs",
        "low_airflow_max_gs",
        "high_airflow_min_gs",
        "min_pairs_low_airflow",
        "min_pairs_high_airflow",
        "min_pairs_enrichment",
        "fuel_status_bracket_s",
        "sigma",
    ):
        if key not in policy:
            raise ValueError(f"fault model residual {residual_name} declares no policy {key}")
    policy["fault_model_version"] = model.version
    policy["region"] = region
    declaration = context.lambda_delivery
    informed = region == "enrichment" and drive_informed_constants(declaration, session)
    vehicle, calibration_conflict = comparison_identity(
        ledger.vehicle, session, vin=vin, program_id=program_id, tune_registry=context.registry
    )
    reference = (
        f"closed-loop MAF from {policy['running_min_maf_gs']} to below "
        f"{policy['low_airflow_max_gs']} g/s, same drive"
    )
    regime = (
        f"high airflow: closed-loop MAF at or above {policy['high_airflow_min_gs']} g/s "
        f"against {reference}"
        if region == "high_airflow"
        else "enrichment: B06 delivery-error enrichment eligibility and gates (Mode 01 "
        f"route, mode01_wot floors) against {reference}"
    )
    quantity = DROOP_QUANTITY if region == "high_airflow" else FLOW_LOSS_QUANTITY
    specs: tuple[str, ...] = (PRESSURE_SPEC, MAF_SPEC, FUEL_STATUS_SPEC)
    if region == "enrichment" and declaration is not None:
        specs += (EQUIV_RATIO_SPEC,)
    minter = _Minter(
        ledger,
        session,
        vehicle=vehicle,
        provenance=Provenance(
            producer=DROOP_TOOL,
            producer_kind="tool",
            version=DROOP_TOOL_VERSION,
            code_version=code_version(
                sys.modules[__name__],
                channels_module,
                channels_plan_module,
                channels_align_module,
                fault_model_module,
                tuning_module,
            ),
        ),
        conditions=Conditions(regime=regime, vehicle_state="engine_running", operating_mode=region),
        subject=f"{DROOP_TOOL}:{session.name}:{region}:{hash_json(policy)[:16]}",
        incident=DROOP_INCIDENT,
        specs=specs,
        inputs={
            "session_files": hash_session_files(session_files_fingerprint(session)),
            "fault_model": model_sha256,
            "lambda_delivery": _hash_model(declaration),
            "noise_floor": _hash_model(context.noise_floor),
            "bindings": sha256_file(bindings_path),
            "dids": sha256_file(dids_path),
        },
        policy=policy,
        labels={
            "evidence_class": ENGINE_RESPONSE,
            "residual": residual_name,
            "region": region,
            "quantity": quantity,
            "session_informed_constants": str(informed).lower(),
            "fault_model_version": str(model.version),
            "fault_model_channels": _model_channels(model, session),
            "limitations": DROOP_LIMITATIONS,
            "induction": context.induction,
        },
        flags=(
            *(("calibration_conflict",) if calibration_conflict else ()),
            *(("informed_constants",) if informed else ()),
            "pressure_on_stored_clock",
        ),
        timestamp_basis="acquired/stored",
        now=now,
    )

    if context.induction == "undeclared":
        return minter.unevaluable(
            "unknown_context",
            "the vehicle profile declares no induction (vehicle parameter induction: "
            "naturally_aspirated or forced). A rail gauge pressure is the pressure across "
            "the injector only without boost, so this evidence cannot be computed for an "
            "engine that may have it",
            "not_declared",
        )
    if context.induction == "forced":
        recorded = session_specs(session)
        if not any(spec in recorded for spec in MANIFOLD_PRESSURE_SPECS):
            return minter.unevaluable(
                "refused",
                "forced_induction_without_manifold_pressure: the vehicle profile declares "
                "induction forced and this session recorded no manifold pressure ("
                + " or ".join(MANIFOLD_PRESSURE_SPECS)
                + "). Under boost the manifold pushes back on a gauge-referenced rail, so "
                "injector flow falls with no change on the gauge; the gauge ratio alone "
                "would read that loss as zero",
                "refused",
            )
        return minter.unevaluable(
            "refused",
            "forced_induction_delta_p_not_implemented: the vehicle profile declares "
            "induction forced. This evidence is a ratio of rail gauge pressures; under "
            "boost the flow loss needs the pressure across the injector (rail gauge + baro "
            "- manifold, fuel_plant.injector_delta_p_kpa), which it does not compute yet. "
            "The session has manifold pressure, so the input exists",
            "refused",
        )
    frame = read_channels(
        session,
        [PRESSURE_SPEC, MAF_SPEC, FUEL_STATUS_SPEC],
        acquired_time=True,
        bindings_path=bindings_path,
        dids_path=dids_path,
    )
    present = set(frame["spec"].unique()) if not frame.empty else set()
    missing = [s for s in (PRESSURE_SPEC, MAF_SPEC, FUEL_STATUS_SPEC) if s not in present]
    if missing:
        return minter.unevaluable(
            "missing_input",
            "never captured in this session: "
            + ", ".join(missing)
            + ". Nothing about the drive can change this; the capture must record them "
            "(fuel pressure comes from the analog gauge input).",
            "not_captured",
        )
    window = float(policy["window_s"])
    p_t, p_v, _ = _long(frame, PRESSURE_SPEC)
    finite = np.isfinite(p_v)
    p_t, p_v = p_t[finite], p_v[finite]
    maf_t, maf_v, maf_stamps = _long(frame, MAF_SPEC)
    fs_t, fs_v, _ = _long(frame, FUEL_STATUS_SPEC)
    status = _bracketed_state(maf_t, fs_t, fs_v, float(policy["fuel_status_bracket_s"]))
    mean, count = _window_means(p_t, p_v, maf_t, window)
    usable = (
        (count >= int(policy["min_samples_per_window"]))
        & np.isfinite(maf_v)
        & (maf_v >= float(policy["running_min_maf_gs"]))
        & (status == CLOSED_LOOP_STATUS)
    )
    low = usable & (maf_v < float(policy["low_airflow_max_gs"]))
    low_p = mean[low]
    n_low = int(low.sum())
    values: dict[str, Quantity] = {
        "low_airflow_pairs": Quantity(value=float(n_low), unit="1"),
        "max_maf": Quantity(
            value=float(np.nanmax(maf_v)) if np.isfinite(maf_v).any() else 0.0, unit="g/s"
        ),
    }

    def uncovered(what: str, have: int, need: int, n: int) -> str:
        return minter.record(
            Result(
                status="unevaluable",
                reason="uncovered",
                detail=(
                    f"fuel pressure was captured, but the drive did not cover {what} "
                    f"({have} of {need} paired reads); that is not a steady pressure"
                ),
            ),
            values=values,
            n=n,
            extra={"empty_kind": "no_eligible_rows"},
        )

    need_low = int(policy["min_pairs_low_airflow"])
    if n_low < need_low:
        return uncovered("closed-loop low airflow", n_low, need_low, n_low)

    if region == "high_airflow":
        target = usable & (maf_v >= float(policy["high_airflow_min_gs"]))
        target_p = mean[target]
        target_t = maf_t[target]
        target_stamps = maf_stamps[target]
        need = int(policy["min_pairs_high_airflow"])
        what = "closed-loop high airflow"
    else:
        wot = context.mode01_wot
        if declaration is None or wot is None or context.calibration is None:
            return minter.unevaluable(
                "unknown_context",
                "this vehicle declares no "
                + ", ".join(_undeclared(context))
                + "; B06 enrichment eligibility cannot be applied",
                "not_declared",
            )
        try:
            route = mode01_route(
                declaration, measured_spec="obd:O2_WR_LAMBDA_S1", min_load=wot.load_floor_pct
            )
            inputs = read_delivery_inputs(
                session,
                route,
                declaration,
                calibration=context.calibration,
                bindings_path=bindings_path,
                dids_path=dids_path,
                mode01_exclusion_note=context.mode01_exclusion_note,
            )
            eligible, _report = gate_delivery_samples(
                inputs,
                route,
                declaration=declaration,
                calibration=context.calibration,
                rpm_bands=wot.rpm_bands,
                min_load=wot.load_floor_pct,
                session=session,
                mode01_exclusion_note=context.mode01_exclusion_note,
            )
        except InputsNotCapturedError as refusal:
            return _not_captured(minter, refusal)
        except GateError as refusal:
            return minter.unevaluable("refused", str(refusal), "refused")
        target_stamps = pd.to_datetime(eligible["timestamp"], utc=True)
        e_t = np.asarray(to_seconds(target_stamps), dtype=float)
        e_mean, e_count = _window_means(p_t, p_v, e_t, window)
        keep = e_count >= int(policy["min_samples_per_window"])
        target_p = e_mean[keep]
        target_t = e_t[keep]
        target_stamps = target_stamps[keep]
        need = int(policy["min_pairs_enrichment"])
        what = "B06-eligible enrichment"
    n_target = len(target_p)
    values["target_pairs"] = Quantity(value=float(n_target), unit="1")
    if n_target < need:
        return uncovered(what, n_target, need, n_low + n_target)

    med_low, med_target = float(np.median(low_p)), float(np.median(target_p))
    se_low = _median_se(maf_t[low], low_p, window)
    se_target = _median_se(target_t, target_p, window)
    halves = np.array_split(low_p[np.argsort(maf_t[low])], 2)
    drift = abs(float(np.median(halves[0])) - float(np.median(halves[1]))) / med_low
    ratio = med_target / med_low
    se_ratio = ratio * float(np.hypot(se_low / med_low, se_target / med_target))
    droop = 100.0 * (1.0 - ratio)
    loss = 100.0 * (1.0 - float(np.sqrt(ratio)))
    sigma = float(policy["sigma"])
    values.update(
        {
            "pressure_low_airflow": Quantity(value=med_low, unit="kPa"),
            "pressure_target": Quantity(value=med_target, unit="kPa"),
            "droop": Quantity(value=droop, unit="percent", uncertainty=100.0 * se_ratio),
            "low_airflow_drift": Quantity(value=100.0 * drift, unit="percent"),
            # Injector flow scales with the square root of pressure across it.
            "implied_fuel_flow_change": Quantity(value=-loss, unit="percent"),
        }
    )
    extra: dict[str, str] = {}
    if region == "high_airflow":
        margin = sigma * 100.0 * se_ratio + 100.0 * drift
        values["margin"] = Quantity(value=margin, unit="percent")
        residual_value = droop
        uncertainty = 100.0 * se_ratio
        if droop - margin > 0:
            extra["sign"] = "+"
        elif droop + margin < 0:
            extra["sign"] = "-"
        else:
            extra["sign_refused"] = (
                "within the drive's own uncertainty; no healthy-droop floor exists to call "
                "the pressure steady"
            )
    else:
        # d(loss)/d(ratio) = -50 / sqrt(ratio): carry the ratio's error and drift.
        slope = 50.0 / float(np.sqrt(ratio))
        uncertainty = slope * se_ratio
        margin = sigma * uncertainty + slope * drift
        values["margin"] = Quantity(value=margin, unit="percent")
        residual_value = loss
        detectable = float("nan")
        assert declaration is not None  # the enrichment gates above required it
        floor = regime_noise_floor(context.noise_floor, "enrichment", declaration)
        if floor.get("available"):
            detectable = DETECTABLE_SIGMA * float(floor["sd_pct"])
            values["detectable"] = Quantity(value=detectable, unit="percent")
            extra["floor_conditions"] = str(floor["conditions"])
        if informed:
            extra["sign_refused"] = INFORMED_REFUSAL
        elif not floor.get("available"):
            extra["sign_refused"] = f"no condition-matched delivery floor: {floor.get('reason')}"
        elif loss - margin >= detectable:
            extra["sign"] = "+"
        elif loss + margin <= -detectable:
            extra["sign"] = "-"
        elif abs(loss) + margin < detectable:
            extra["sign"] = "0"
        else:
            extra["sign_refused"] = (
                "the flow loss and its margin straddle the delivery error's detectable "
                "change: it neither explains nor rules out a detectable lean"
            )
    stamps = pd.concat([maf_stamps[low], pd.Series(target_stamps)])
    window_span = (_aware(stamps.min()), _aware(stamps.max()))
    return minter.record(
        Result(
            status="evaluated",
            residual=Quantity(value=residual_value, unit="percent", uncertainty=uncertainty),
        ),
        values=values,
        n=n_low + n_target,
        extra=extra,
        window=window_span,
    )
