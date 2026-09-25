"""Commanded-versus-measured lambda: declared quantities, named states, frozen lag (B06).

The behavior review (``data/analyses/2026-09-21-behavior-model-review.md``)
chose a commanded-vs-measured lambda relationship over an unconditional
AFR-voltage surface, because only a residual against the ECU's own target can
tell an intentional mixture change from a delivery error. This module is the
contract that relationship has to satisfy before anything is fitted on it:

* **Declared quantities.** Which channel is the target, which is the
  measurement, what each one *is* (phi, lambda, AFR), and the evidence for
  that convention live in the vehicle's ``lambda_delivery.yaml`` — not in
  code. phi and lambda are reciprocals that both read 1.0 at stoich, so a
  convention is only evidence when it was checked away from stoich; the
  known-answer check here (:func:`check_target_convention`) refuses to call
  a convention verified from stoich-only data.
* **One residual.** :func:`residual_pct` is the single signed residual,
  ``100 * (measured / target - 1)`` in lambda or AFR (positive = lean of
  target). :func:`src.analysis.tuning.delivery_error_from_gated` uses it too,
  so analysis and any later serving path cannot drift apart in sign or
  definition. It refuses phi, whose ratio inverts the sign.
* **Named states, never manufactured numbers.** Fuel cut (the RAM word's
  ``_SENTINEL`` or PID 0x44 = 0), invalid targets, sensor rails, missing
  pairs, an unverified convention and a source used outside the regime it
  was verified in are each a named ineligible state with ``residual_pct``
  left NaN — never a zero, never a large lean error.
* **Acquisition-time pairing.** Target and measurement are separate polls.
  Each target observation is paired with at most one measured observation
  within the declared acquisition tolerance
  (:func:`src.analysis.pairing.match_ordered`); nothing is averaged onto a
  one-second grid, so no result here can claim instantaneous tracking from
  a mean.
* **Transport delay without leakage.** Acquisition mismatch (how far apart
  two reads were taken) and exhaust/sensor response delay (how long the
  mixture takes to reach the sensor) are separate quantities. A delay is
  estimated only by :func:`estimate_transport_delay` on named training
  drives, frozen as a :class:`TransportDelay`, and refused on any drive it
  was fitted on (or that informed the declared eligibility constants). A
  test drive can never pick its own lag.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.afr import AfrCalibration, voltage_to_afr
from src.analysis.compare import align_pairs
from src.analysis.noise_floor import NoiseFloor
from src.analysis.pairing import PairingPolicy, match_ordered, to_seconds
from src.paths import BINDINGS_YAML, DIDS_YAML

Quantity = Literal["phi", "lambda", "afr"]
Regime = Literal["closed_loop", "enrichment", "lean_target"]
IneligibleState = Literal[
    "fuel_cut",
    "post_fuel_cut",
    "target_invalid",
    "convention_unverified",
    "calibration_unknown",
    "measured_missing",
    "measured_rail",
    "measured_censored",
    "target_transient",
    "source_not_valid_in_regime",
    "unsupported_regime",
]
#: The state of a row that may carry a delivery residual.
ELIGIBLE = "eligible"
#: Every state a sample can be in, in precedence order (first match wins).
STATES: tuple[str, ...] = (
    "fuel_cut",
    "target_invalid",
    "convention_unverified",
    "calibration_unknown",
    "measured_missing",
    "measured_rail",
    "measured_censored",
    "post_fuel_cut",
    "target_transient",
    "source_not_valid_in_regime",
    "unsupported_regime",
    ELIGIBLE,
)


class LambdaDeliveryError(ValueError):
    """A lambda-delivery question this contract refuses to answer."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResidualDefinition(_Strict):
    """The residual's definition, pinned so data and code cannot disagree."""

    definition: Literal["100 * (lambda_measured / lambda_target - 1)"]
    unit: Literal["percent"]
    positive: Literal["lean"]


class CommandedSource(_Strict):
    """One channel that can carry the ECU's commanded mixture."""

    name: str
    spec: str
    #: ``from_calibration`` defers to ``AfrCalibration.
    #: commanded_equiv_ratio_convention`` (Mode 01 PID 0x44); every other value
    #: must be backed by ``convention_evidence`` measured away from stoich.
    quantity: Quantity | Literal["from_calibration"]
    convention_evidence: str = Field(min_length=1)
    #: Regimes in which this source has been shown to *be* the final target.
    valid_regimes: list[Regime] = Field(min_length=1)
    #: Companion channel whose rows mark fuel cut (a state, not a number).
    sentinel_spec: str | None = None
    #: A raw value that *means* fuel cut on this source (PID 0x44 reads 0.0).
    fuel_cut_value: float | None = None
    #: Target values outside this range (source quantity) are invalid, not
    #: a mixture. Required: an unbounded target turns a decode error into a
    #: delivery "finding".
    plausible_min: float
    plausible_max: float
    #: Named where it matters, e.g. PID 0x44's closed-loop behaviour.
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _checks(self) -> CommandedSource:
        if not self.plausible_min < self.plausible_max:
            raise ValueError(f"{self.name}: plausible_min must be below plausible_max")
        if self.sentinel_spec is None and self.fuel_cut_value is None:
            raise ValueError(
                f"{self.name}: a commanded source must say how fuel cut appears on it "
                "(sentinel_spec or fuel_cut_value); otherwise a cut sample would pass "
                "as a target"
            )
        return self


class RefusedSource(_Strict):
    """A channel that looks like a target but must not be used as one."""

    spec: str
    reason: str = Field(min_length=1)


class MeasuredSource(_Strict):
    """One measured-mixture instrument."""

    name: str
    spec: str
    quantity: Literal["lambda", "afr"]
    #: ``voltage_afr`` applies the calibration's voltage transfer (the raw
    #: reading is a sensor voltage) and its censoring bounds.
    transform: Literal["identity", "voltage_afr"] = "identity"
    convention_evidence: str = Field(min_length=1)
    #: Readings at or beyond a rail are the sensor's limit, not a mixture
    #: (PID 0x24 pegs at 2.000 under fuel cut). Source quantity.
    rail_low: float | None = None
    rail_high: float | None = None


class SourceDisagreement(_Strict):
    """A measured disagreement between two instruments, kept visible."""

    between: tuple[str, str]
    value_pct: float
    conditions: str = Field(min_length=1)
    status: Literal["unresolved", "resolved"]
    evidence: str = Field(min_length=1)


class Eligibility(_Strict):
    """The steady-target / steady-operation rule and where its numbers came from."""

    #: ``|lambda_target - 1| <= closed_loop_band`` is the closed-loop regime.
    closed_loop_band: float = Field(gt=0)
    #: The target must have held within ``target_step_tolerance`` (lambda)
    #: for this long before a sample is eligible.
    steady_target_dwell_s: float = Field(ge=0)
    target_step_tolerance: float = Field(ge=0)
    #: Target observations further apart than this cannot establish a dwell.
    max_target_gap_s: float = Field(gt=0)
    #: Samples this soon after the last fuel-cut observation are the
    #: post-cut recovery, not steady delivery.
    post_fuel_cut_holdoff_s: float = Field(ge=0)
    #: Sessions whose data informed these constants. They are not
    #: independent evidence for any evaluation that uses them.
    informed_by: list[str] = Field(default_factory=list)
    justification: str = Field(min_length=1)


class Acquisition(_Strict):
    """Largest separation two reads may have and still be one observation.

    This is acquisition mismatch, a property of the polling schedule. It is
    not exhaust transport or sensor response delay (:class:`TransportDelay`).
    """

    max_separation_s: float = Field(gt=0)
    evidence: str = Field(min_length=1)


class TransportDelayDeclaration(_Strict):
    status: Literal["not_estimated", "frozen"]
    note: str = Field(min_length=1)


class RegimeFloor(_Strict):
    """Which measured noise floor a regime may quote, and under what conditions."""

    channel: str
    #: Must equal the floor's recorded ``conditions`` exactly; a floor
    #: measured under other conditions is refused, not quoted.
    conditions: str


class LambdaDeliveryDeclaration(_Strict):
    schema_version: Literal[1] = 1
    residual: ResidualDefinition
    commanded_sources: list[CommandedSource] = Field(min_length=1)
    refused_commanded: list[RefusedSource] = Field(default_factory=list)
    measured_sources: list[MeasuredSource] = Field(min_length=1)
    source_disagreements: list[SourceDisagreement] = Field(default_factory=list)
    eligibility: Eligibility
    acquisition: Acquisition
    transport_delay: TransportDelayDeclaration
    noise_floors: dict[Regime, RegimeFloor] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> LambdaDeliveryDeclaration:
        names = [s.name for s in self.commanded_sources] + [s.name for s in self.measured_sources]
        if len(names) != len(set(names)):
            raise ValueError("source names must be unique")
        refused = {r.spec for r in self.refused_commanded}
        clash = sorted(refused & {s.spec for s in self.commanded_sources})
        if clash:
            raise ValueError(f"refused commanded sources also declared usable: {clash}")
        measured = {s.name for s in self.measured_sources}
        for item in self.source_disagreements:
            unknown = [name for name in item.between if name not in measured]
            if unknown:
                raise ValueError(f"source_disagreements names unknown sources: {unknown}")
        return self

    def commanded(self, name: str | None = None) -> CommandedSource:
        if name is None:
            return self.commanded_sources[0]
        for source in self.commanded_sources:
            if source.name == name:
                return source
        raise LambdaDeliveryError(f"no commanded source named {name!r}")

    def measured(self, name: str | None = None) -> MeasuredSource:
        if name is None:
            return self.measured_sources[0]
        for source in self.measured_sources:
            if source.name == name:
                return source
        raise LambdaDeliveryError(f"no measured source named {name!r}")

    def disagreements_for(self, name: str) -> list[SourceDisagreement]:
        return [item for item in self.source_disagreements if name in item.between]


def load_lambda_delivery(
    path: Path,
    *,
    dids_path: Path | None = None,
    providers_path: Path | None = None,
) -> LambdaDeliveryDeclaration:
    """Load and cross-check a declaration against the catalogs it names.

    A ``mem:`` target must exist in the memory catalog with the declared
    quantity as its unit and a declared sentinel matching ``sentinel_spec``;
    the refused DIDs must exist; a target the provider registry pins for
    ``commanded_afr`` must be declared here (usable or refused), so the two
    files cannot silently name different targets.
    """
    declaration = LambdaDeliveryDeclaration.model_validate(yaml.safe_load(path.read_text()))
    if dids_path is not None:
        from src.decode.dids import load_did_map

        did_map = load_did_map(dids_path)
        memory = {entry.name: entry for entry in did_map.memory}
        dids = {entry.name for entry in did_map.dids}
        for source in declaration.commanded_sources:
            kind, _, name = source.spec.partition(":")
            if kind == "mem":
                entry = memory.get(name)
                if entry is None:
                    raise LambdaDeliveryError(f"{source.spec} is not in the memory catalog")
                if entry.unit != source.quantity:
                    raise LambdaDeliveryError(
                        f"{source.spec}: catalog unit {entry.unit!r} disagrees with declared "
                        f"quantity {source.quantity!r}"
                    )
                expected = f"{source.spec}_SENTINEL" if entry.sentinel_raw is not None else None
                if source.sentinel_spec != expected:
                    raise LambdaDeliveryError(
                        f"{source.spec}: sentinel_spec must be {expected!r} to match the "
                        "catalog's declared sentinel"
                    )
        for refused in declaration.refused_commanded:
            kind, _, name = refused.spec.partition(":")
            if kind == "did" and name not in dids:
                raise LambdaDeliveryError(f"refused source {refused.spec} is not in dids.yaml")
    if providers_path is not None and providers_path.exists():
        from src.analysis.tuning_frame import load_provider_registry

        registry = load_provider_registry(providers_path)
        declared = {s.spec for s in declaration.commanded_sources} | {
            r.spec for r in declaration.refused_commanded
        }
        for provider in registry.providers:
            if provider.role == "commanded_afr" and provider.spec not in declared:
                raise LambdaDeliveryError(
                    f"tuning_providers.yaml pins commanded_afr to {provider.spec}, which "
                    f"{path.name} does not declare"
                )
    return declaration


# ---------------------------------------------------------------------------
# Conventions and the one residual


def to_lambda(values: pd.Series | np.ndarray, quantity: Quantity, *, afr_stoich: float) -> Any:
    """Convert a mixture reading to lambda. Non-positive readings become NaN.

    ``phi`` is fuel/air relative to stoich, so ``lambda = 1 / phi``; ``afr``
    is ``afr / stoich``. A non-positive phi is fuel cut or garbage, never a
    mixture, so it maps to NaN rather than dividing into infinity.
    """
    array = pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(array) & (array > 0)
    out = np.full(len(array), np.nan)
    if quantity == "phi":
        out[valid] = 1.0 / array[valid]
    elif quantity == "lambda":
        out[valid] = array[valid]
    elif quantity == "afr":
        if not afr_stoich > 0:
            raise LambdaDeliveryError(f"afr_stoich must be positive, got {afr_stoich}")
        out[valid] = array[valid] / afr_stoich
    else:  # pragma: no cover - Literal guards this
        raise LambdaDeliveryError(f"unknown mixture quantity {quantity!r}")
    if isinstance(values, pd.Series):
        return pd.Series(out, index=values.index, dtype=float)
    return out


def residual_pct(measured: Any, target: Any, *, quantity: Literal["lambda", "afr"]) -> Any:
    """``100 * (measured / target - 1)``: positive means leaner than commanded.

    The one delivery residual (B06.4). ``measured`` and ``target`` must be in
    the same quantity — lambda or AFR, where the ratio is identical because
    stoich cancels. phi is refused: fuel/air runs the other way, so the same
    ratio of phis would report a lean delivery as rich.
    """
    if quantity not in ("lambda", "afr"):
        raise LambdaDeliveryError(
            f"residual_pct compares lambda or AFR, not {quantity!r}: phi is fuel/air, so "
            "its ratio inverts the sign. Convert with to_lambda() first."
        )
    return (measured / target - 1.0) * 100.0


def resolve_quantity(
    source: CommandedSource, calibration: AfrCalibration | None
) -> Quantity | None:
    """The source's convention, or ``None`` when it is unverified/unknown."""
    if source.quantity != "from_calibration":
        return source.quantity
    if calibration is None:
        return None
    convention = calibration.commanded_equiv_ratio_convention
    return None if convention == "unverified" else convention


def regime_of(target_lambda: np.ndarray, band: float) -> np.ndarray:
    """``closed_loop`` / ``enrichment`` / ``lean_target`` by commanded lambda."""
    out = np.full(len(target_lambda), "", dtype=object)
    finite = np.isfinite(target_lambda)
    out[finite & (np.abs(target_lambda - 1.0) <= band)] = "closed_loop"
    out[finite & (target_lambda < 1.0 - band)] = "enrichment"
    out[finite & (target_lambda > 1.0 + band)] = "lean_target"
    return out


# ---------------------------------------------------------------------------
# Transport delay: estimated on training drives only, then frozen


@dataclass(frozen=True)
class TransportDelay:
    """Exhaust transport + sensor response delay, frozen before evaluation.

    ``fitted_on`` names every drive whose data chose ``seconds``; applying the
    delay to any of them raises :class:`LeakedDriveError`.
    """

    seconds: float
    fitted_on: tuple[str, ...]
    method: str
    candidates: tuple[float, ...]
    scores: tuple[float | None, ...]
    n_rows: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "seconds": self.seconds,
            "fitted_on": list(self.fitted_on),
            "method": self.method,
            "candidates": list(self.candidates),
            "scores": list(self.scores),
            "n_rows": self.n_rows,
        }


def _refuse_dependent_drive(
    drive_id: str | None,
    declaration: LambdaDeliveryDeclaration,
    delay: TransportDelay | None,
    *,
    evaluation: bool,
) -> None:
    if delay is not None and drive_id is None:
        raise LambdaDeliveryError(
            "a frozen transport delay needs the drive id it is applied to, so its "
            "independence from the fitting drives can be checked"
        )
    if drive_id is None:
        return
    # Lazy: behavior_validation pulls the whole serving stack, and this module
    # is imported by the lightweight tuning analysis path.
    from src.analysis.behavior_validation import LeakedDriveError

    if delay is not None and drive_id in delay.fitted_on:
        raise LeakedDriveError(
            f"{drive_id} chose the transport delay ({delay.seconds:g} s); it cannot also "
            "evaluate it — a test drive may not pick its own lag"
        )
    if evaluation and drive_id in declaration.eligibility.informed_by:
        raise LeakedDriveError(
            f"{drive_id} informed the declared eligibility constants; it is not independent "
            "evidence for an evaluation that uses them"
        )


# ---------------------------------------------------------------------------
# Acquisition-time pairing and per-sample states


@dataclass
class DeliverySamples:
    """Per-target-observation states and residuals, with every count named."""

    frame: pd.DataFrame
    target_source: str
    measured_source: str
    delay_s: float
    acquisition_max_separation_s: float
    counts: dict[str, int] = field(default_factory=dict)

    def summary(
        self, floor: NoiseFloor | None, declaration: LambdaDeliveryDeclaration
    ) -> dict[str, Any]:
        """Counts, per-regime residual statistics and the regime's noise floor."""
        eligible = self.frame[self.frame["state"] == ELIGIBLE]
        regimes: dict[str, Any] = {}
        for regime in ("closed_loop", "enrichment"):
            rows = eligible[eligible["regime"] == regime]
            values = rows["residual_pct"].to_numpy(dtype=float)
            regimes[regime] = {
                "n": int(len(values)),
                "median_pct": float(np.median(values)) if len(values) else None,
                "p05_pct": float(np.quantile(values, 0.05)) if len(values) else None,
                "p95_pct": float(np.quantile(values, 0.95)) if len(values) else None,
                "noise_floor": regime_noise_floor(floor, regime, declaration),
            }
        separations = self.frame["separation_s"].dropna().to_numpy(dtype=float)
        return {
            "target_source": self.target_source,
            "measured_source": self.measured_source,
            "residual": declaration.residual.definition,
            "transport_delay_s": self.delay_s,
            "acquisition": {
                "max_separation_s": self.acquisition_max_separation_s,
                "median_separation_s": float(np.median(separations)) if len(separations) else None,
                "max_observed_s": float(separations.max()) if len(separations) else None,
            },
            "states": dict(self.counts),
            "regimes": regimes,
            "measured_disagreements": [
                item.model_dump() for item in declaration.disagreements_for(self.measured_source)
            ],
        }


def regime_noise_floor(
    floor: NoiseFloor | None, regime: str, declaration: LambdaDeliveryDeclaration
) -> dict[str, Any]:
    """The noise floor a regime may quote, or why it may not quote one.

    A floor is only valid under the conditions it was measured in: the
    delivery floor on record is WOT-only, so it is refused for closed loop
    rather than quoted ~16x too tight.
    """
    wanted = declaration.noise_floors.get(regime)  # type: ignore[call-overload]
    if wanted is None:
        return {"available": False, "reason": f"no noise floor is declared for {regime}"}
    entry = None if floor is None else floor.channels.get(wanted.channel)
    if entry is None:
        return {"available": False, "reason": f"no measured floor for {wanted.channel}"}
    if entry.conditions != wanted.conditions:
        return {
            "available": False,
            "reason": (
                f"{wanted.channel} was measured under {entry.conditions!r}, not the "
                f"declared {wanted.conditions!r}"
            ),
        }
    if entry.unit != "percent":
        return {"available": False, "reason": f"{wanted.channel} is in {entry.unit}, not percent"}
    return {
        "available": True,
        "channel": wanted.channel,
        "sd_pct": entry.sd,
        "conditions": entry.conditions,
        "provenance": dict(floor.provenance) if floor is not None else {},
    }


def _long_values(frame: pd.DataFrame | None) -> tuple[np.ndarray, np.ndarray]:
    if frame is None or frame.empty:
        return np.array([], dtype=float), np.array([], dtype=float)
    ordered = frame.sort_values("timestamp", kind="stable")
    times = to_seconds(pd.to_datetime(ordered["timestamp"], utc=True))
    values = pd.to_numeric(ordered["value"], errors="coerce").to_numpy(dtype=float)
    return np.asarray(times, dtype=float), values


@dataclass(frozen=True)
class _TargetSide:
    """Target observations merged with fuel-cut sentinels, time-ordered."""

    times: np.ndarray
    raw: np.ndarray
    cut: np.ndarray
    invalid: np.ndarray
    target_lambda: np.ndarray


def _target_side(
    target: pd.DataFrame,
    sentinel: pd.DataFrame | None,
    *,
    commanded: CommandedSource,
    quantity: Quantity | None,
    afr_stoich: float,
) -> _TargetSide:
    """One row per target observation *and* per fuel-cut sentinel observation,
    so a cut is counted as ``fuel_cut`` instead of disappearing."""
    t_times, t_raw = _long_values(target)
    s_times, _ = _long_values(sentinel)
    cut_value = commanded.fuel_cut_value
    is_cut = np.zeros(len(t_raw), dtype=bool)
    if cut_value is not None:
        is_cut = np.isclose(t_raw, cut_value, rtol=0.0, atol=1e-9)
    times = np.concatenate([t_times, s_times])
    raw = np.concatenate([t_raw, np.full(len(s_times), np.nan)])
    cut = np.concatenate([is_cut, np.ones(len(s_times), dtype=bool)])
    order = np.argsort(times, kind="stable")
    times, raw, cut = times[order], raw[order], cut[order]
    plausible = (
        np.isfinite(raw) & (raw >= commanded.plausible_min) & (raw <= commanded.plausible_max)
    )
    invalid = ~cut & ~plausible
    target_lambda = (
        np.full(len(times), np.nan)
        if quantity is None
        else np.where(cut | invalid, np.nan, to_lambda(raw, quantity, afr_stoich=afr_stoich))
    )
    return _TargetSide(times, raw, cut, invalid, target_lambda)


@dataclass(frozen=True)
class _MeasuredSide:
    """Measured observations as lambda, with their censoring and rail flags."""

    times: np.ndarray
    lam: np.ndarray
    railed: np.ndarray
    censored: np.ndarray
    calibration_missing: bool


def _measured_side(
    measured: pd.DataFrame,
    *,
    instrument: MeasuredSource,
    calibration: AfrCalibration | None,
    afr_stoich: float,
) -> _MeasuredSide:
    m_times, m_raw = _long_values(measured)
    calibration_missing = False
    censored = np.zeros(len(m_raw), dtype=bool)
    if instrument.transform == "voltage_afr":
        transfer = None if calibration is None else calibration.voltage_transfer
        if transfer is None:
            calibration_missing = True
            m_value = np.full(len(m_raw), np.nan)
        else:
            m_value = voltage_to_afr(pd.Series(m_raw), transfer).to_numpy(dtype=float)
            censored = (
                ~np.isfinite(m_value)
                | (m_value <= transfer.censored_floor_afr)
                | (m_value >= transfer.censored_ceiling_afr)
            )
    else:
        m_value = m_raw
    railed = np.zeros(len(m_raw), dtype=bool)
    if instrument.rail_high is not None:
        railed |= m_raw >= instrument.rail_high
    if instrument.rail_low is not None:
        railed |= m_raw <= instrument.rail_low
    m_lambda = to_lambda(m_value, instrument.quantity, afr_stoich=afr_stoich)
    return _MeasuredSide(m_times, m_lambda, railed, censored, calibration_missing)


def _seconds_since_cut(times: np.ndarray, cut: np.ndarray) -> np.ndarray:
    """Seconds since the latest fuel-cut observation at or before each row
    (``inf`` before the first cut)."""
    last_cut = np.where(cut, times, -np.inf)
    return np.asarray(times - np.maximum.accumulate(last_cut), dtype=float)


def _range_extreme_table(values: np.ndarray, op: Any) -> list[np.ndarray]:
    """Sparse table: ``table[k][i]`` is ``op`` over ``values[i : i + 2**k]``."""
    table = [values]
    width = 1
    while 2 * width <= len(values):
        previous = table[-1]
        table.append(op(previous[: len(previous) - width], previous[width:]))
        width *= 2
    return table


def _range_query(table: list[np.ndarray], lo: np.ndarray, hi: np.ndarray, op: Any) -> np.ndarray:
    """``op`` over ``values[lo..hi]`` (inclusive) per pair, from :func:`_range_extreme_table`."""
    length = hi - lo + 1
    level = np.floor(np.log2(np.maximum(length, 1))).astype(np.intp)
    out = np.empty(len(lo), dtype=float)
    for k in np.unique(level):
        rows = level == k
        block = table[int(k)]
        out[rows] = op(block[lo[rows]], block[hi[rows] - (1 << int(k)) + 1])
    return out


def _steady_target_mask(
    times: np.ndarray,
    target_lambda: np.ndarray,
    cut: np.ndarray,
    *,
    dwell_s: float,
    max_gap_s: float,
    tolerance: float,
) -> np.ndarray:
    """Which target observations sit on a steady target.

    Row ``i`` is steady when every target observation in ``[t_i - dwell, t_i)``
    is finite, not a fuel cut, within ``tolerance`` of row ``i``'s own target,
    consecutive observations are no more than ``max_gap_s`` apart, and the
    observations reach back the whole dwell (the earliest one examined is at
    or before ``t_i - dwell``). Rows whose own target is not finite are never
    steady.

    Vectorized (R05.12) from a per-row backward walk, which was
    O(n x dwell) in pure Python; the walk's rule is kept exactly — the tests
    compare the two on randomized traces. The walk from row ``i`` stops at
    the first earlier row at or before ``t_i - dwell``, which is the last
    row of the ``times <= t_i - dwell`` prefix, so each row's window is
    ``[k_i, i)``: gaps are checked across ``(k_i, i]``, targets over
    ``[k_i, i)`` with range min/max queries.
    """
    times = np.asarray(times, dtype=float)
    target = np.asarray(target_lambda, dtype=float)
    cut = np.asarray(cut, dtype=bool)
    n = len(times)
    steady = np.zeros(n, dtype=bool)
    if n == 0:
        return steady
    finite = np.isfinite(target)
    start = times - dwell_s
    reached_at_once = times <= start
    steady[finite & reached_at_once] = True
    rows = np.flatnonzero(finite & ~reached_at_once)
    if rows.size == 0:
        return steady
    first = np.searchsorted(times, start[rows], side="right") - 1
    walkable = first >= 0
    rows, first = rows[walkable], first[walkable]
    if rows.size == 0:
        return steady

    def count_between(flags: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        """How many of ``flags[lo..hi]`` (inclusive) are set."""
        cumulative = np.concatenate([[0], np.cumsum(flags, dtype=np.int64)])
        return np.asarray(cumulative[hi + 1] - cumulative[lo])

    gap_bad = np.zeros(n, dtype=bool)
    gap_bad[1:] = np.diff(times) > max_gap_s
    point_bad = cut | ~finite
    ok = (count_between(gap_bad, first + 1, rows) == 0) & (
        count_between(point_bad, first, rows - 1) == 0
    )
    if ok.any():
        clean = np.where(finite, target, 0.0)
        highest = _range_query(_range_extreme_table(clean, np.maximum), first, rows - 1, np.maximum)
        lowest = _range_query(_range_extreme_table(clean, np.minimum), first, rows - 1, np.minimum)
        own = target[rows]
        ok &= (highest - own <= tolerance) & (own - lowest <= tolerance)
    steady[rows[ok]] = True
    return steady


def _assign_states(n: int, conditions: list[tuple[str, np.ndarray]]) -> np.ndarray:
    """Each row's state: the first condition in order that holds, else eligible."""
    state = np.full(n, ELIGIBLE, dtype=object)
    assigned = np.zeros(n, dtype=bool)
    for name, mask in conditions:
        take = mask & ~assigned
        state[take] = name
        assigned |= take
    return state


def delivery_samples(
    *,
    target: pd.DataFrame,
    measured: pd.DataFrame,
    declaration: LambdaDeliveryDeclaration,
    target_source: str | None = None,
    measured_source: str | None = None,
    sentinel: pd.DataFrame | None = None,
    calibration: AfrCalibration | None = None,
    afr_stoich: float,
    delay: TransportDelay | None = None,
    drive_id: str | None = None,
    evaluation: bool = False,
) -> DeliverySamples:
    """Pair each target observation with one measured observation and name its state.

    ``target``/``measured``/``sentinel`` are long frames (``timestamp``,
    ``value``) on the acquisition clock, as :func:`src.analysis.channels.
    read_channels` returns them with ``acquired_time=True``. ``measured`` is
    in the measured source's own quantity (a voltage for ``voltage_afr``).

    One row per target observation *and* per fuel-cut observation, so a cut
    is counted as ``fuel_cut`` instead of disappearing. ``residual_pct`` is
    set only on ``eligible`` rows. ``delay`` shifts the measured clock back by
    a frozen transport delay before pairing; the acquisition tolerance still
    bounds how far apart the two reads may be after that shift.

    ``evaluation=True`` additionally refuses drives that informed the
    declared eligibility constants.
    """
    commanded = declaration.commanded(target_source)
    instrument = declaration.measured(measured_source)
    _refuse_dependent_drive(drive_id, declaration, delay, evaluation=evaluation)
    elig = declaration.eligibility
    quantity = resolve_quantity(commanded, calibration)

    target_side = _target_side(
        target, sentinel, commanded=commanded, quantity=quantity, afr_stoich=afr_stoich
    )
    times, raw, cut = target_side.times, target_side.raw, target_side.cut
    target_lambda = target_side.target_lambda
    n = len(times)
    measured_side = _measured_side(
        measured, instrument=instrument, calibration=calibration, afr_stoich=afr_stoich
    )

    delay_s = 0.0 if delay is None else float(delay.seconds)
    policy = PairingPolicy.within(
        pd.Timedelta(seconds=declaration.acquisition.max_separation_s), reference="a"
    )
    matched = match_ordered(times, measured_side.times - delay_s, policy)
    paired = np.full(n, -1, dtype=np.intp)
    separation = np.full(n, np.nan)
    paired[matched.index_a] = matched.index_b
    separation[matched.index_a] = matched.separation_s
    has_pair = paired >= 0
    measured_lambda = np.where(has_pair, measured_side.lam[np.maximum(paired, 0)], np.nan)
    pair_railed = has_pair & measured_side.railed[np.maximum(paired, 0)]
    pair_censored = has_pair & measured_side.censored[np.maximum(paired, 0)]

    regime = regime_of(target_lambda, elig.closed_loop_band)
    steady = _steady_target_mask(
        times,
        target_lambda,
        cut,
        dwell_s=elig.steady_target_dwell_s,
        max_gap_s=elig.max_target_gap_s,
        tolerance=elig.target_step_tolerance,
    )
    since_cut = _seconds_since_cut(times, cut)

    state = _assign_states(
        n,
        [
            ("fuel_cut", cut),
            ("target_invalid", target_side.invalid),
            ("convention_unverified", np.full(n, quantity is None)),
            ("calibration_unknown", np.full(n, measured_side.calibration_missing)),
            ("measured_missing", ~has_pair),
            ("measured_rail", pair_railed),
            ("measured_censored", pair_censored | (has_pair & ~np.isfinite(measured_lambda))),
            ("post_fuel_cut", since_cut < elig.post_fuel_cut_holdoff_s),
            ("target_transient", ~steady),
            (
                "source_not_valid_in_regime",
                np.isin(regime, ["closed_loop", "enrichment", "lean_target"])
                & ~np.isin(regime, list(commanded.valid_regimes)),
            ),
            ("unsupported_regime", regime == "lean_target"),
        ],
    )

    eligible = state == ELIGIBLE
    residual = np.full(n, np.nan)
    residual[eligible] = residual_pct(
        measured_lambda[eligible], target_lambda[eligible], quantity="lambda"
    )
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(times, unit="s", utc=True),
            "target_raw": raw,
            "target_lambda": target_lambda,
            "measured_lambda": measured_lambda,
            "separation_s": separation,
            "regime": pd.Series(regime, dtype=object).replace("", None),
            "state": state,
            "residual_pct": residual,
        }
    )
    counts = {name: int((state == name).sum()) for name in STATES}
    counts["total"] = int(n)
    return DeliverySamples(
        frame=frame,
        target_source=commanded.name,
        measured_source=instrument.name,
        delay_s=delay_s,
        acquisition_max_separation_s=declaration.acquisition.max_separation_s,
        counts=counts,
    )


@dataclass(frozen=True)
class DriveInputs:
    """One training drive's long frames, as :func:`delivery_samples` takes them."""

    target: pd.DataFrame
    measured: pd.DataFrame
    sentinel: pd.DataFrame | None = None


def estimate_transport_delay(
    train: Mapping[str, DriveInputs],
    *,
    declaration: LambdaDeliveryDeclaration,
    candidates: Sequence[float],
    target_source: str | None = None,
    measured_source: str | None = None,
    calibration: AfrCalibration | None = None,
    afr_stoich: float,
    min_rows: int = 30,
) -> TransportDelay:
    """Choose one delay on training drives only and freeze it.

    Each candidate shifts the measured clock; the score is the *mean*
    absolute residual over every row that has both a target and a
    measurement (steady rows and the transitions the dwell rule excludes).
    Two details matter and were each got wrong first: a **median** reads
    zero at every candidate, because most rows are steady and match whatever
    the lag; and scoring the **post-step rows alone** rewards an arbitrarily
    large lag, since over-shifting moves the mismatch to the rows *before*
    the step. The smallest candidate achieving the minimum wins, so noise
    cannot push the lag up. Refuses — rather than returning a guess — when
    too few rows qualify.
    """
    if not train:
        raise LambdaDeliveryError("a transport delay needs at least one training drive")
    ordered = sorted({float(c) for c in candidates})
    if not ordered or ordered[0] < 0:
        raise LambdaDeliveryError("candidate delays must be non-empty and non-negative")
    scores: list[float | None] = []
    rows_used = 0
    for lag in ordered:
        frozen = TransportDelay(lag, tuple(), "candidate", tuple(ordered), tuple(), 0)
        values: list[np.ndarray] = []
        for inputs in train.values():
            samples = delivery_samples(
                target=inputs.target,
                measured=inputs.measured,
                sentinel=inputs.sentinel,
                declaration=declaration,
                target_source=target_source,
                measured_source=measured_source,
                calibration=calibration,
                afr_stoich=afr_stoich,
                delay=frozen,
                drive_id="__training__",
            ).frame
            rows = samples[samples["state"].isin([ELIGIBLE, "target_transient"])]
            values.append(
                np.abs(
                    residual_pct(
                        rows["measured_lambda"].to_numpy(dtype=float),
                        rows["target_lambda"].to_numpy(dtype=float),
                        quantity="lambda",
                    )
                )
            )
        pooled = np.concatenate(values) if values else np.array([])
        pooled = pooled[np.isfinite(pooled)]
        if len(pooled) < min_rows:
            scores.append(None)
            continue
        rows_used = max(rows_used, len(pooled))
        # Mean, not median: a step's mismatch lives in a few rows either
        # side of it, and a median over a mostly-steady drive reads zero at
        # every candidate.
        scores.append(float(np.mean(pooled)))
    finite = [(score, lag) for score, lag in zip(scores, ordered, strict=True) if score is not None]
    if not finite:
        raise LambdaDeliveryError(
            f"insufficient training evidence for a transport delay: no candidate had "
            f"{min_rows} paired rows on {sorted(train)}"
        )
    best = min(score for score, _ in finite)
    chosen = min(lag for score, lag in finite if score == best)
    return TransportDelay(
        seconds=chosen,
        fitted_on=tuple(sorted(train)),
        method="mean_abs_residual_at_target_transitions",
        candidates=tuple(ordered),
        scores=tuple(scores),
        n_rows=rows_used,
    )


# ---------------------------------------------------------------------------
# Known-answer checks (B06.2), on the sanctioned acquisition-time pairing


@dataclass(frozen=True)
class ConventionCheck:
    """Which convention a target channel follows, judged away from stoich."""

    verdict: Literal["phi", "lambda", "unresolvable", "neither"]
    n_pairs_off_stoich: int
    median_abs_error_as_phi_pct: float | None
    median_abs_error_as_lambda_pct: float | None
    tolerance_pct: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "n_pairs_off_stoich": self.n_pairs_off_stoich,
            "median_abs_error_as_phi_pct": self.median_abs_error_as_phi_pct,
            "median_abs_error_as_lambda_pct": self.median_abs_error_as_lambda_pct,
            "tolerance_pct": self.tolerance_pct,
        }


def check_target_convention(
    target_values: np.ndarray,
    measured_lambda: np.ndarray,
    *,
    off_stoich_band: float,
    tolerance_pct: float,
    min_pairs: int = 10,
    side: Literal["above", "below", "both"] = "both",
) -> ConventionCheck:
    """Decide phi vs lambda from paired values *away from stoich* only.

    Pairs whose target is within ``off_stoich_band`` of 1.0 are discarded
    before anything is judged — both conventions read 1.0 there, so they
    carry no evidence. With fewer than ``min_pairs`` pairs left the verdict
    is ``unresolvable``, never a default.

    ``side`` judges readings above or below 1.0 separately. A channel may
    behave differently on each side — Mode 01 PID 0x44 follows phi on the
    enrichment side (> 1) yet its closed-loop readings below 1.0 track the
    lambda sensor numerically (2026-09-22 report) — and pooling both sides
    would let one behaviour outvote the other.
    """
    target_values = np.asarray(target_values, dtype=float)
    measured_lambda = np.asarray(measured_lambda, dtype=float)
    keep = (
        np.isfinite(target_values)
        & np.isfinite(measured_lambda)
        & (target_values > 0)
        & (np.abs(target_values - 1.0) > off_stoich_band)
    )
    if side == "above":
        keep &= target_values > 1.0
    elif side == "below":
        keep &= target_values < 1.0
    n = int(keep.sum())
    if n < min_pairs:
        return ConventionCheck("unresolvable", n, None, None, tolerance_pct)
    target = target_values[keep]
    actual = measured_lambda[keep]
    as_phi = float(np.median(np.abs(residual_pct(actual, 1.0 / target, quantity="lambda"))))
    as_lambda = float(np.median(np.abs(residual_pct(actual, target, quantity="lambda"))))
    phi_ok = as_phi <= tolerance_pct
    lambda_ok = as_lambda <= tolerance_pct
    verdict: Literal["phi", "lambda", "unresolvable", "neither"]
    if phi_ok and not lambda_ok:
        verdict = "phi"
    elif lambda_ok and not phi_ok:
        verdict = "lambda"
    elif phi_ok and lambda_ok:
        verdict = "unresolvable"
    else:
        verdict = "neither"
    return ConventionCheck(verdict, n, as_phi, as_lambda, tolerance_pct)


def steady_neighbours(times: np.ndarray, values: np.ndarray, *, window_s: float) -> np.ndarray:
    """True where every observation within ``window_s`` either side is equal.

    Separates "two reads of one steady target disagree" (a real mismatch)
    from "the target moved between the two reads" (acquisition skew).
    """
    order = np.argsort(times, kind="stable")
    t = np.asarray(times, dtype=float)[order]
    v = np.asarray(values, dtype=float)[order]
    lo = np.searchsorted(t, t - window_s, side="left")
    hi = np.searchsorted(t, t + window_s, side="right")
    steady_sorted = np.array(
        [
            bool(np.all(v[a:b] == v[i])) and (b - a) > 1
            for i, (a, b) in enumerate(zip(lo, hi, strict=True))
        ]
    )
    out = np.zeros(len(t), dtype=bool)
    out[order] = steady_sorted
    return out


def target_source_agreement(
    session: Path | pd.DataFrame,
    primary: str,
    secondary: str,
    *,
    policy: PairingPolicy,
    closed_loop_band: float,
    steady_window_s: float,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
) -> dict[str, Any]:
    """Per-sample agreement of two target channels, split by regime.

    Uses :func:`src.analysis.compare.align_pairs` (acquisition-time pairing)
    and reports the per-sample ratio, never a correlation of aggregates.
    Pairs where either side is a cut (zero) are counted separately; only
    pairs whose primary target was steady across ``steady_window_s`` judge
    agreement, so a target step between the two reads is not read as
    disagreement.
    """
    aligned = align_pairs(
        session, primary, secondary, policy=policy, bindings_path=bindings_path, dids_path=dids_path
    )
    pairs = aligned.pairs
    a = pairs["a"].to_numpy(dtype=float)
    b = pairs["b"].to_numpy(dtype=float)
    times = to_seconds(pd.to_datetime(pairs["timestamp_a"], utc=True))
    from src.analysis.channels import read_channels

    primary_long = read_channels(
        session, [primary], acquired_time=True, bindings_path=bindings_path, dids_path=dids_path
    )
    p_times, p_values = _long_values(primary_long)
    steady_all = steady_neighbours(p_times, p_values, window_s=steady_window_s)
    steady = np.zeros(len(a), dtype=bool)
    if len(p_times):
        index = np.searchsorted(p_times, times)
        index = np.clip(index, 0, len(p_times) - 1)
        exact = np.isclose(p_times[index], times, rtol=0, atol=1e-6)
        steady = exact & steady_all[index]
    cut = (a <= 0) | (b <= 0)
    out: dict[str, Any] = {
        "primary": primary,
        "secondary": secondary,
        "max_separation_s": policy.max_separation_s,
        "n_pairs": int(len(a)),
        "cut_pairs": int(cut.sum()),
        "regimes": {},
    }
    ratio = np.where(cut, np.nan, a / np.where(b == 0, np.nan, b))
    for regime, mask in (
        ("closed_loop", np.abs(a - 1.0) <= closed_loop_band),
        ("enrichment", a > 1.0 + closed_loop_band),
    ):
        sel = mask & ~cut & steady
        r = ratio[sel]
        out["regimes"][regime] = {
            "steady_pairs": int(sel.sum()),
            "exact_fraction": float(np.mean(r == 1.0)) if len(r) else None,
            "within_0p5pct_fraction": float(np.mean(np.abs(r - 1.0) <= 0.005)) if len(r) else None,
            "ratio_p05": float(np.quantile(r, 0.05)) if len(r) else None,
            "ratio_p95": float(np.quantile(r, 0.95)) if len(r) else None,
        }
    return out


__all__ = [
    "ELIGIBLE",
    "STATES",
    "ConventionCheck",
    "DeliverySamples",
    "DriveInputs",
    "LambdaDeliveryDeclaration",
    "LambdaDeliveryError",
    "TransportDelay",
    "check_target_convention",
    "delivery_samples",
    "estimate_transport_delay",
    "load_lambda_delivery",
    "regime_noise_floor",
    "regime_of",
    "residual_pct",
    "resolve_quantity",
    "steady_neighbours",
    "target_source_agreement",
    "to_lambda",
]
