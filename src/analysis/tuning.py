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

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import numpy as np
import pandas as pd

from src.analysis.afr import AfrCalibration
from src.analysis.channels import read_aligned, read_channels
from src.analysis.noise_floor import NoiseFloor
from src.analysis.tune_revisions import TuneRevisionRegistry, resolve_session_revision

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
    #: Calibration in force when the session was recorded, when a registry
    #: resolves it. Comparing delivery error across a flash boundary without
    #: this is comparing two different engines.
    tune_revision: str | None = None
    tune_revision_source: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "tune_revision": self.tune_revision,
            "tune_revision_source": self.tune_revision_source,
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


def _refuse_missing_inputs(
    session: Path | pd.DataFrame,
    specs: list[str],
    *,
    bindings_path: Path,
    dids_path: Path,
    calibration: AfrCalibration | None = None,
    afr_stoich: float = 14.7,
) -> NoReturn:
    """Say *why* there is nothing to analyse instead of reporting zero bands.

    An empty aligned frame has two causes that demand opposite responses, and
    reporting ``started: 0`` for both made them indistinguishable:

    * the gates filtered everything — a real answer about the drive, meaning
      go and capture the regime you care about;
    * the inputs were never captured — not an answer at all, and no amount of
      driving will change it.

    Measured 2026-08-06: a clean 25-minute all-DID capture, 18k samples per
    channel at 14.93 Hz, returned ``started: 0``. That reads as "the gates
    rejected everything". In fact the ``tuning-did`` profile sets
    ``mode01_enabled: false`` — it must, because Mode 01 and Mode 22 are
    mutually exclusive on this ECM — and commanded AFR exists only as Mode 01
    PID 0x44. The question was unanswerable before the truck moved.
    """
    missing: list[str] = []
    for spec in specs:
        try:
            rows = read_channels(
                session,
                [spec],
                bindings_path=bindings_path,
                dids_path=dids_path,
                calibration=calibration,
                afr_stoich=afr_stoich,
            )
        except Exception as exc:  # unresolvable spec is itself a missing input
            missing.append(f"{spec} ({type(exc).__name__}: {exc})")
            continue
        if rows.empty:
            missing.append(spec)

    detail = ""
    if isinstance(session, Path):
        manifest = session / "capture_profile.json"
        if manifest.exists():
            try:
                profile = json.loads(manifest.read_text())
            except ValueError:
                profile = {}
            snapshot = profile.get("profile_snapshot") or profile
            if snapshot.get("mode01_enabled") is False:
                detail = (
                    f" This session was captured with profile "
                    f"{profile.get('profile')!r}, which disables Mode 01 — and Mode 01 "
                    "cannot run alongside Mode 22 on this ECM. Commanded AFR (PID 0x44) "
                    "is therefore absent by construction, not by chance."
                )

    raise GateError(
        "this session cannot answer delivery error — its inputs were never "
        "captured, which is not the same as the gates rejecting every sample. "
        + (
            "Empty in this session: " + ", ".join(missing)
            if missing
            else "No two required channels overlapped in time."
        )
        + detail
        + " Use a Mode 01 capture for commanded-vs-measured fuelling, or feed a"
        " semantic frame to delivery_error_from_semantic_frame() once a Mode 22"
        " provider for commanded AFR exists."
    )


def _refuse_non_mixture_measurement(
    session: Path | pd.DataFrame, measured_spec: str, *, dids_path: Path
) -> None:
    """Refuse a measured channel that is not a mixture reading.

    This function compares the measured channel against a commanded AFR
    as-is, so it must BE lambda or AFR. The A/F sensor's raw voltage is not:
    on 2026-09-21 `did:ECM_AF_SENSOR_V_B1` was passed here and ~1.5 V was
    compared against a 12.6 AFR target, returning a confident -87 % instead
    of an error. Voltage needs the calibrated transfer, which is what the
    semantic path does (`build_tuning_frame` with a `voltage_afr` provider,
    then :func:`delivery_error_from_semantic_frame`).
    """
    from src.analysis.channels import parse_spec

    spec = parse_spec(measured_spec)
    if spec.source != "did":
        return
    from src.decode import dids as dids_decode

    entries = {e.name: e for e in dids_decode.load_did_map(dids_path).dids}
    entry = entries.get(spec.name)
    if entry is not None and entry.unit in ("V", "mV"):
        raise GateError(
            f"{measured_spec} is a sensor voltage ({entry.unit}), not a mixture reading; "
            "delivery_error compares it against commanded AFR as-is. Use the semantic "
            "path, which applies the calibrated voltage-to-AFR transfer: build_tuning_frame "
            "with a voltage_afr provider, then delivery_error_from_semantic_frame."
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
    revisions: TuneRevisionRegistry | None = None,
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
    _refuse_non_mixture_measurement(session, measured_spec, dids_path=dids_path)
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
    if frame.empty:
        _refuse_missing_inputs(
            session,
            specs,
            bindings_path=bindings_path,
            dids_path=dids_path,
            calibration=calibration,
            afr_stoich=afr_stoich,
        )
    measured = frame[measured_spec].astype(float)
    semantic = pd.DataFrame(
        {
            "timestamp": frame["timestamp"],
            "rpm": frame["obd:RPM"],
            "measured_afr": measured * afr_stoich if measured_is_lambda else measured,
            "commanded_afr": frame["obd:COMMANDED_AFR"],
            "fueling_active": frame["obd:FUEL_STATUS"].astype(float).eq(OPEN_LOOP),
        }
    )
    if min_load is not None:
        semantic["load"] = frame["obd:ENGINE_LOAD"]

    revision = None
    revision_source = "unknown"
    if isinstance(session, Path):
        revision, revision_source, _conflict = resolve_session_revision(session, revisions)
    return delivery_error_from_semantic_frame(
        semantic,
        measured_spec=measured_spec,
        afr_stoich=afr_stoich,
        rpm_bands=rpm_bands,
        min_load=min_load,
        grid=grid,
        fueling_gate_name="closed_loop",
        tune_revision=revision,
        tune_revision_source=revision_source,
    )


def delivery_error_from_semantic_frame(
    frame: pd.DataFrame,
    *,
    measured_spec: str,
    afr_stoich: float = 14.7,
    rpm_bands: tuple[float, ...] = DEFAULT_RPM_BANDS,
    min_load: float | None = None,
    grid: str = "1s",
    fueling_gate_name: str = "fueling_inactive",
    tune_revision: str | None = None,
    tune_revision_source: str = "semantic_frame",
) -> DeliveryError:
    """Apply delivery gates to semantic roles instead of transport names.

    Required columns are ``timestamp``, ``rpm``, ``measured_afr``,
    ``commanded_afr``, and boolean ``fueling_active``.  An optional
    ``measured_afr_censored`` column removes transfer-boundary samples before
    they can become a false mixture error.  ``load`` is optional and may only
    be used when an independently validated load provider exists.

    This is the all-DID adapter boundary: callers resolve/transform roles
    before this function, so an A/F voltage can never be mislabeled as AFR by
    a transport-specific shortcut.
    """
    required = {"timestamp", "rpm", "measured_afr", "commanded_afr", "fueling_active"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise GateError("semantic delivery frame is missing roles: " + ", ".join(missing))
    if min_load is not None and "load" not in frame:
        raise GateError(
            "delivery load gate requested but no validated semantic load provider exists"
        )

    values = frame.copy()
    values["timestamp"] = pd.to_datetime(values["timestamp"], utc=True)
    # A row exists only when every required role was independently observed in
    # the same grid cell. Grouping does not forward-fill across cells.
    columns = ["rpm", "measured_afr", "commanded_afr", "fueling_active"]
    if min_load is not None:
        columns.append("load")
    if "measured_afr_censored" in values:
        columns.append("measured_afr_censored")
    values = (
        values.loc[:, ["timestamp", *columns]]
        .sort_values("timestamp", kind="stable")
        .groupby(pd.Grouper(key="timestamp", freq=grid), sort=True)
        .last()
        .dropna(subset=["rpm", "measured_afr", "commanded_afr", "fueling_active"])
        .reset_index()
    )
    report = GateReport(started=len(values))

    before = len(values)
    values = values[values["fueling_active"].astype(bool)]
    report.drop(fueling_gate_name, before, len(values))

    # Enrichment, not overrun. A semantic fueling-active role is deliberately
    # separate from the mixture target: a future DID decoder can identify fuel
    # cut without carrying Mode-01's overloaded `FUEL_STATUS == 4` convention.
    before = len(values)
    values = values[values["commanded_afr"].astype(float) < afr_stoich]
    report.drop("not_enrichment", before, len(values))

    if min_load is not None:
        before = len(values)
        values = values[values["load"].astype(float) > min_load]
        report.drop("below_min_load", before, len(values))

    if "measured_afr_censored" in values:
        before = len(values)
        values = values[~values["measured_afr_censored"].astype(bool)]
        report.drop("censored_measured_afr", before, len(values))

    before = len(values)
    values = values[(values["rpm"] >= rpm_bands[0]) & (values["rpm"] <= rpm_bands[-1])]
    report.drop("outside_rpm_bands", before, len(values))
    report.remaining = len(values)

    if values.empty:
        return DeliveryError(
            bands=pd.DataFrame(columns=["rpm_band", "n", "commanded", "measured", "error_pct"]),
            band_averaged_pct=float("nan"),
            pooled_pct=float("nan"),
            n=0,
            gates=report.as_dict(),
            rpm_window=(float("nan"), float("nan")),
            measured_spec=measured_spec,
            tune_revision=tune_revision,
            tune_revision_source=tune_revision_source,
        )

    values = values.assign(
        error_pct=(values["measured_afr"] / values["commanded_afr"] - 1.0) * 100.0
    )
    binned = values.groupby(pd.cut(values["rpm"], list(rpm_bands)), observed=True)
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
        pooled_pct=float(values["error_pct"].mean()),
        n=int(len(values)),
        gates=report.as_dict(),
        rpm_window=(float(values["rpm"].min()), float(values["rpm"].max())),
        measured_spec=measured_spec,
        tune_revision=tune_revision,
        tune_revision_source=tune_revision_source,
    )


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "tune_revisions": list(self.tune_revisions),
            "tune_revision_sources": list(self.tune_revision_sources),
            "tune_revision_conflicts": list(self.tune_revision_conflicts),
            "floor_conditions": self.floor_conditions,
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
    noise_floor: NoiseFloor | float | None = None,
    floor_channel: str | None = None,
    revisions: TuneRevisionRegistry | None = None,
) -> SessionComparison:
    """A-vs-B on a matched grid, refusing load as an axis.

    ``noise_floor`` is either a :class:`~src.analysis.noise_floor.NoiseFloor`
    (with ``floor_channel``) or a bare per-cell sd. Prefer the object: it
    carries the regime the floor was measured in, and that regime is part of
    the measurement — the same null pair gives 0.48 deg of spark spread at WOT
    and 7.7 deg across all conditions, so quoting one against the other
    understates noise by ~16x. Without any floor the sigma columns read
    ``unavailable`` rather than an invented significance.
    """
    for axis in axes:
        _assert_axis_allowed(axis, unsafe=unsafe_axes)
    if isinstance(noise_floor, NoiseFloor) and floor_channel is None:
        raise GateError(
            "a NoiseFloor was given without floor_channel — name the channel whose "
            "spread should gate these deltas. Picking one silently would also pick "
            "the regime it was measured in, and a floor is only valid for its own."
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
        keys = []
        for axis, edges in zip(axes, bins, strict=True):
            keys.append(pd.cut(frame[axis], list(edges)).astype(str).rename(axis))
        return frame.groupby(keys, observed=True).agg(n=(value, "size"), mean=(value, "mean"))

    cells_a, cells_b = cell_frame(frame_a), cell_frame(frame_b)
    joined = cells_a.join(cells_b, how="inner", lsuffix="_a", rsuffix="_b")
    joined = joined[(joined["n_a"] >= min_cell_samples) & (joined["n_b"] >= min_cell_samples)]
    joined = joined.assign(delta=joined["mean_b"] - joined["mean_a"])

    floor_sd: float | None = None
    floor_conditions: str | None = None
    if isinstance(noise_floor, NoiseFloor) and floor_channel is not None:
        floor_sd = noise_floor.sd_for(floor_channel)
        entry = noise_floor.channels.get(floor_channel)
        floor_conditions = entry.conditions if entry is not None else None
    elif isinstance(noise_floor, (int, float)):
        floor_sd = float(noise_floor)

    if floor_sd is not None and floor_sd > 0:
        joined["delta_sigma"] = joined["delta"] / floor_sd
        joined["distinguishable"] = joined["delta_sigma"].abs() >= 2.0
    else:
        joined["delta_sigma"] = np.nan
        joined["distinguishable"] = pd.NA

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
    "delivery_error_from_semantic_frame",
    "scale_stability",
]
