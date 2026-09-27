"""Verify the warmup inputs before modelling them (B07.1).

``coolant_warmup``, ``cold_start_idle_rpm_vs_coolant`` and ``cat_warmup`` are
functions of three channels — coolant temperature, the run-time clock and the
catalyst temperature — and the 2026-09-21 review graded the catalyst artifact
without ever asking what its input *is*. These checks answer that from stored
data, per sample, before any fit:

* :func:`runtime_clock_report` — does ``RUN_TIME`` advance one second per
  second, and where does it reset? A run-time axis that drifts or restarts
  mid-run is not a "time since start" axis.
* :func:`catalyst_estimation_evidence` — is the catalyst channel *measured*
  or ECU-*estimated*? The difference decides what a deviation on it can
  possibly mean: a temperature-trajectory deviation either way, but an
  independent health claim only if a sensor is there. The verdict is
  ``ecu_estimated`` only when several independent indicators hold, and
  ``unresolved`` otherwise — never inferred from one.

Coolant identity is already pinned elsewhere and is not re-derived here: the
ground-truth suite asserts the broadcast binding and Mode 01 PID 0x05 agree
1:1 (``tests/fixtures/ground_truth/expectations.yaml``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.channels import refuse_as_of
from src.signals.pairing import PairingPolicy, match_ordered, to_seconds


def _series(long: pd.DataFrame, spec: str) -> tuple[np.ndarray, np.ndarray]:
    """``spec``'s rows as time-ordered arrays. Each row is a reading here
    (counted, differenced, paired one-to-one), so a change-only broadcast
    read (P02.10; one row per payload change) is refused."""
    refuse_as_of(long, reader="warmup input check", specs=[spec])
    rows = long[long["spec"] == spec] if "spec" in long else long
    if rows.empty:
        return np.array([]), np.array([])
    rows = rows.sort_values("timestamp", kind="stable")
    times = np.asarray(to_seconds(pd.to_datetime(rows["timestamp"], utc=True)), dtype=float)
    values = pd.to_numeric(rows["value"], errors="coerce").to_numpy(dtype=float)
    return times, values


@dataclass(frozen=True)
class RuntimeClockReport:
    """How the run-time clock behaved: rate, resets and gaps."""

    reads: int
    #: Median of ``d(RUN_TIME)/d(wall clock)`` over consecutive reads within
    #: one run. A real seconds-since-start clock sits at 1.0.
    median_rate: float | None
    p05_rate: float | None
    p95_rate: float | None
    #: Steps where the clock went backwards: engine restarts (or a reconnect
    #: that hid one). Each ends the previous start's context.
    resets: int
    first_reading_s: float | None
    max_reading_s: float | None
    #: Consecutive reads more than ``gap_seconds`` apart, where the clock
    #: cannot testify to what happened between them.
    gaps: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "reads": self.reads,
            "median_rate": self.median_rate,
            "p05_rate": self.p05_rate,
            "p95_rate": self.p95_rate,
            "resets": self.resets,
            "first_reading_s": self.first_reading_s,
            "max_reading_s": self.max_reading_s,
            "gaps": self.gaps,
        }


def runtime_clock_report(
    long: pd.DataFrame, *, runtime_spec: str = "obd:RUN_TIME", gap_seconds: float = 30.0
) -> RuntimeClockReport:
    """Per-sample rate, resets and gaps of the run-time clock."""
    times, values = _series(long, runtime_spec)
    finite = np.isfinite(values)
    times, values = times[finite], values[finite]
    if len(times) < 2:
        return RuntimeClockReport(len(times), None, None, None, 0, None, None, 0)
    d_wall = np.diff(times)
    d_clock = np.diff(values)
    forward = (d_clock >= 0) & (d_wall > 0) & (d_wall <= gap_seconds)
    rate = d_clock[forward] / d_wall[forward]
    return RuntimeClockReport(
        reads=int(len(times)),
        median_rate=float(np.median(rate)) if len(rate) else None,
        p05_rate=float(np.quantile(rate, 0.05)) if len(rate) else None,
        p95_rate=float(np.quantile(rate, 0.95)) if len(rate) else None,
        resets=int(np.sum(d_clock < 0)),
        first_reading_s=float(values[0]),
        max_reading_s=float(values.max()),
        gaps=int(np.sum(d_wall > gap_seconds)),
    )


@dataclass(frozen=True)
class CatalystEvidence:
    """Whether the catalyst channel behaves like a probe or like a model."""

    verdict: str
    indicators: dict[str, Any]
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "indicators": self.indicators, "reasons": self.reasons}


def catalyst_estimation_evidence(
    long: pd.DataFrame,
    *,
    catalyst_spec: str = "obd:CATALYST_TEMP_B1S1",
    second_bank_spec: str = "obd:CATALYST_TEMP_B2S1",
    intake_spec: str = "obd:INTAKE_TEMP",
    policy: PairingPolicy | None = None,
    min_pairs: int = 20,
) -> CatalystEvidence:
    """Judge measured-vs-estimated from three independent indicators.

    #. **Starts below ambient.** The first reading of a run against the
       intake-air temperature at the same time. A probe sitting in an exhaust
       that has been running cannot read colder than the air entering it; an
       ECU model initialised at zero can.
    #. **Two banks that agree exactly.** Separately plumbed thermocouples do
       not return identical numbers sample after sample; one model driven by
       the same rpm/load inputs does. Judged per sample on the acquisition
       clock, never by comparing the two banks' means.
    #. **Coarse quantization.** PID 0x3C reports 1 °C steps; readings landing
       only on a much coarser grid are an internal model's resolution.

    Two of the three must hold for ``ecu_estimated``; otherwise the verdict
    is ``unresolved`` and the caller must not claim catalyst health either
    way.
    """
    policy = policy or PairingPolicy.within(pd.Timedelta(seconds=2.0), reference="a")
    times, values = _series(long, catalyst_spec)
    indicators: dict[str, Any] = {"reads": int(len(values))}
    reasons: list[str] = []
    if len(values) < 3:
        return CatalystEvidence("unresolved", indicators, [f"{catalyst_spec} has < 3 readings"])

    # 1. First reading against intake air at the same time.
    iat_times, iat_values = _series(long, intake_spec)
    below_ambient = None
    if len(iat_times):
        matched = match_ordered(times[:1], iat_times, policy)
        if matched.n_pairs:
            first_iat = float(iat_values[matched.index_b[0]])
            below_ambient = bool(values[0] < first_iat)
            indicators["first_reading_c"] = float(values[0])
            indicators["intake_air_at_first_reading_c"] = first_iat
    indicators["first_reading_below_intake_air"] = below_ambient
    if below_ambient:
        reasons.append(
            f"first reading {values[0]:g} °C is below the intake air "
            f"{indicators['intake_air_at_first_reading_c']:g} °C at the same instant"
        )

    # 2. The two banks, per sample.
    other_times, other_values = _series(long, second_bank_spec)
    identical_fraction = None
    if len(other_times):
        matched = match_ordered(times, other_times, policy)
        if matched.n_pairs >= min_pairs:
            a = values[matched.index_a]
            b = other_values[matched.index_b]
            identical_fraction = float(np.mean(a == b))
            indicators["bank_pairs"] = matched.n_pairs
    indicators["banks_identical_fraction"] = identical_fraction
    if identical_fraction is not None and identical_fraction >= 0.5:
        reasons.append(
            f"the two banks return identical values on {identical_fraction:.0%} of "
            f"{indicators['bank_pairs']} same-instant pairs"
        )

    # 3. Quantization: the greatest common step of the observed values.
    finite = values[np.isfinite(values)]
    step = None
    if len(finite) >= 3:
        rounded = np.round(finite).astype(np.int64)
        step = (
            int(np.gcd.reduce(np.abs(np.diff(np.unique(rounded)))))
            if len(np.unique(rounded)) > 1
            else None
        )
    indicators["value_step_c"] = step
    if step is not None and step >= 2:
        reasons.append(f"readings land only on a {step} °C grid, coarser than the PID's 1 °C")

    verdict = "ecu_estimated" if len(reasons) >= 2 else "unresolved"
    return CatalystEvidence(verdict, indicators, reasons)


__all__ = [
    "CatalystEvidence",
    "RuntimeClockReport",
    "catalyst_estimation_evidence",
    "runtime_clock_report",
]
