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

See ``docs/roadmap/tuning-analysis-toolkit.md`` Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.afr import AfrCalibration
from src.analysis.channels import read_aligned

#: Axes that must never grid a cross-flash comparison. ``CAL/LD`` is computed
#: from BFS, so it moves when fuelling moves; see the module docstring.
FORBIDDEN_AXES: frozenset[str] = frozenset(
    {"ENGINE_LOAD", "CAL/LD", "CAL/LD VALUE", "CALCULATED_LOAD", "ABSOLUTE_LOAD", "LOAD"}
)

#: Default rpm band edges for band-averaging. Wide enough that a WOT pull
#: lands several samples per band, narrow enough that delivery error's rpm
#: dependence does not hide inside one bucket.
DEFAULT_RPM_BANDS: tuple[float, ...] = (2500.0, 3000.0, 3500.0, 4000.0, 4500.0, 5000.0, 6500.0)

#: `FUEL_STATUS` bit for "open loop". Covers enrichment *and* decel fuel cut,
#: which is why it is never used alone.
OPEN_LOOP = 4.0


class GateError(ValueError):
    """A caller asked for an analysis a gate refuses to perform."""


@dataclass
class GateReport:
    """How many samples each gate removed, and why."""

    started: int = 0
    filtered: dict[str, int] = field(default_factory=dict)
    remaining: int = 0

    def drop(self, gate: str, before: int, after: int) -> None:
        if before != after:
            self.filtered[gate] = self.filtered.get(gate, 0) + (before - after)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "filtered": dict(self.filtered),
            "remaining": self.remaining,
        }


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "band_averaged_pct": self.band_averaged_pct,
            "pooled_pct": self.pooled_pct,
            "n": self.n,
            "rpm_window": list(self.rpm_window),
            "measured_spec": self.measured_spec,
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


def delivery_error(
    session: Path | pd.DataFrame,
    *,
    calibration: AfrCalibration,
    measured_spec: str = "obd:O2_WR_LAMBDA_S1",
    measured_is_lambda: bool = True,
    afr_stoich: float = 14.7,
    rpm_bands: tuple[float, ...] = DEFAULT_RPM_BANDS,
    min_load: float | None = 85.0,
    grid: str = "1s",
    cell_agg: str = "last",
    bindings_path: Path = Path("data/bindings.yaml"),
    dids_path: Path = Path("data/dids.yaml"),
) -> DeliveryError:
    """Delivery error at enrichment, with every gate applied.

    ``measured_spec`` is a parameter on purpose. The ECM's own λ (PID 0x24)
    and the A/F sensor voltage path disagree by ~3.8 % at WOT (2026-08-05,
    commit 78592f4) and nobody knows which is right, so this function must not
    quietly pick a side — the caller names the source and the answer carries it.

    Gates, in order:

    1. same-instant pairing (nothing stale-joined; :func:`read_aligned`)
    2. open loop **and** an enrichment target strictly richer than stoich, so
       decel fuel cut is excluded by construction rather than by threshold
    3. optional load floor, for WOT-only questions
    4. band-averaged over rpm, so band composition cannot masquerade as an effect
    """
    specs = ["obd:RPM", "obd:FUEL_STATUS", "obd:COMMANDED_AFR", measured_spec]
    if min_load is not None:
        specs.append("obd:ENGINE_LOAD")

    frame = read_aligned(
        session,
        specs,
        grid=grid,
        require_all=True,
        cell_agg=cell_agg,  # type: ignore[arg-type]
        calibration=calibration,
        afr_stoich=afr_stoich,
        bindings_path=bindings_path,
        dids_path=dids_path,
    )
    report = GateReport(started=len(frame))

    measured = frame[measured_spec].astype(float)
    frame = frame.assign(
        measured_afr=measured * afr_stoich if measured_is_lambda else measured,
        commanded_afr=frame["obd:COMMANDED_AFR"].astype(float),
    )

    before = len(frame)
    frame = frame[frame["obd:FUEL_STATUS"].astype(float) == OPEN_LOOP]
    report.drop("closed_loop", before, len(frame))

    # Enrichment, not overrun. DFCO is also FUEL_STATUS 4, but its commanded
    # mixture is not richer than stoich, so this excludes it structurally
    # instead of guessing a lean threshold.
    before = len(frame)
    frame = frame[frame["commanded_afr"] < afr_stoich]
    report.drop("not_enrichment", before, len(frame))

    if min_load is not None:
        before = len(frame)
        frame = frame[frame["obd:ENGINE_LOAD"].astype(float) > min_load]
        report.drop("below_min_load", before, len(frame))

    before = len(frame)
    frame = frame[(frame["obd:RPM"] >= rpm_bands[0]) & (frame["obd:RPM"] <= rpm_bands[-1])]
    report.drop("outside_rpm_bands", before, len(frame))
    report.remaining = len(frame)

    if frame.empty:
        return DeliveryError(
            bands=pd.DataFrame(columns=["rpm_band", "n", "commanded", "measured", "error_pct"]),
            band_averaged_pct=float("nan"),
            pooled_pct=float("nan"),
            n=0,
            gates=report.as_dict(),
            rpm_window=(float("nan"), float("nan")),
            measured_spec=measured_spec,
        )

    frame = frame.assign(error_pct=(frame["measured_afr"] / frame["commanded_afr"] - 1.0) * 100.0)
    binned = frame.groupby(pd.cut(frame["obd:RPM"], list(rpm_bands)), observed=True)
    bands = binned.agg(
        n=("error_pct", "size"),
        commanded=("commanded_afr", "mean"),
        measured=("measured_afr", "mean"),
        error_pct=("error_pct", "mean"),
    ).reset_index(names="rpm_band")
    bands["rpm_band"] = bands["rpm_band"].astype(str)

    return DeliveryError(
        bands=bands,
        band_averaged_pct=float(bands["error_pct"].mean()),
        pooled_pct=float(frame["error_pct"].mean()),
        n=int(len(frame)),
        gates=report.as_dict(),
        rpm_window=(float(frame["obd:RPM"].min()), float(frame["obd:RPM"].max())),
        measured_spec=measured_spec,
    )


@dataclass
class SessionComparison:
    """Per-cell deltas between two sessions on a matched grid."""

    cells: pd.DataFrame
    mean_delta: float
    n_cells: int
    axes: tuple[str, ...]
    value: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "axes": list(self.axes),
            "mean_delta": self.mean_delta,
            "n_cells": self.n_cells,
            "cells": self.cells.to_dict(orient="records"),
        }


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
    afr_stoich: float = 14.7,
    bindings_path: Path = Path("data/bindings.yaml"),
    dids_path: Path = Path("data/dids.yaml"),
    noise_floor: float | None = None,
) -> SessionComparison:
    """A-vs-B on a matched grid, refusing load as an axis.

    ``noise_floor``, when given, is the per-cell sd from a same-tune null run
    (:mod:`src.analysis.noise_floor`); each cell then carries ``delta_sigma``
    and a ``distinguishable`` flag. Without it those columns read
    ``unavailable`` rather than an invented significance.
    """
    for axis in axes:
        _assert_axis_allowed(axis, unsafe=unsafe_axes)

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
        keys = []
        for axis, edges in zip(axes, bins, strict=True):
            keys.append(pd.cut(frame[axis], list(edges)).astype(str).rename(axis))
        return frame.groupby(keys, observed=True).agg(n=(value, "size"), mean=(value, "mean"))

    cells_a, cells_b = cell_frame(frame_a), cell_frame(frame_b)
    joined = cells_a.join(cells_b, how="inner", lsuffix="_a", rsuffix="_b")
    joined = joined[(joined["n_a"] >= min_cell_samples) & (joined["n_b"] >= min_cell_samples)]
    joined = joined.assign(delta=joined["mean_b"] - joined["mean_a"])

    if noise_floor is not None and noise_floor > 0:
        joined["delta_sigma"] = joined["delta"] / noise_floor
        joined["distinguishable"] = joined["delta_sigma"].abs() >= 2.0
    else:
        joined["delta_sigma"] = np.nan
        joined["distinguishable"] = pd.NA

    return SessionComparison(
        cells=joined.reset_index(),
        mean_delta=float(joined["delta"].mean()) if len(joined) else float("nan"),
        n_cells=int(len(joined)),
        axes=tuple(axes),
        value=value,
    )


@dataclass
class ScaleStability:
    """Does one linear map survive on data it was not fitted to?"""

    scale_a: float
    offset_a: float
    scale_b: float
    drift_pct: float
    oos_r2: float
    verdict: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "scale_a": self.scale_a,
            "offset_a": self.offset_a,
            "scale_b": self.scale_b,
            "drift_pct": self.drift_pct,
            "oos_r2": self.oos_r2,
            "verdict": self.verdict,
        }


#: Drift thresholds, calibrated against the ECM_RPM control (0.10 %) and the
#: 0x23D outcomes: pedal/throttle held 0.24 %/0.16 % and were promoted to
#: `confirmed`; the best airflow candidate drifted -6.5 % and was deliberately
#: left unpinned. See commit c341570.
STABLE_DRIFT_PCT = 1.5
UNSTABLE_DRIFT_PCT = 3.0


def scale_stability(
    x_a: np.ndarray | pd.Series,
    y_a: np.ndarray | pd.Series,
    x_b: np.ndarray | pd.Series,
    y_b: np.ndarray | pd.Series,
) -> ScaleStability:
    """Fit ``y = m·x + c`` on window A, then test it on window B.

    Correlation alone picks the wrong byte whenever the true signal drives
    whatever you regressed against — that is how one binding was mislabelled
    twice (RPM, then airflow, when it was pedal position). What separates a
    real binding from a correlated one is whether the *same* linear map holds
    on independent data.
    """
    xa = np.asarray(x_a, dtype=float)
    ya = np.asarray(y_a, dtype=float)
    xb = np.asarray(x_b, dtype=float)
    yb = np.asarray(y_b, dtype=float)
    for name, arr in (("A", xa), ("B", xb)):
        if len(arr) < 3 or float(np.std(arr)) == 0.0:
            raise GateError(f"window {name} needs >=3 samples with non-zero variance")

    m_a, c_a = np.polyfit(xa, ya, 1)
    m_b, _ = np.polyfit(xb, yb, 1)
    predicted = m_a * xb + c_a
    ss_res = float(np.sum((yb - predicted) ** 2))
    ss_tot = float(np.sum((yb - yb.mean()) ** 2))
    oos_r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    drift = 100.0 * (m_b - m_a) / m_a if m_a else float("nan")

    magnitude = abs(drift)
    if magnitude <= STABLE_DRIFT_PCT:
        verdict = "stable"
    elif magnitude >= UNSTABLE_DRIFT_PCT:
        verdict = "unstable"
    else:
        verdict = "indeterminate"

    return ScaleStability(
        scale_a=float(m_a),
        offset_a=float(c_a),
        scale_b=float(m_b),
        drift_pct=float(drift),
        oos_r2=float(oos_r2),
        verdict=verdict,
    )


__all__ = [
    "DEFAULT_RPM_BANDS",
    "FORBIDDEN_AXES",
    "DeliveryError",
    "GateError",
    "ScaleStability",
    "SessionComparison",
    "compare_sessions",
    "delivery_error",
    "scale_stability",
]
