"""Identify and evaluate the E07.3 fuel plant on recorded drives.

Session -> samples -> steady regions -> (identification | held-out evaluation),
plus the ground-truth check of the ECM's pulse chain. The physics is
:mod:`src.analysis.fuel_plant`; the ECM arithmetic is
:mod:`src.analysis.injector_pulse`. Rules this module enforces rather than
documents:

* **One anchor per injector-pulse read** (``did:ECM_INJECTOR_PULSE_1``, the
  actuator command). Fast channels (rpm, MAF, lambda) are paired one-to-one
  on the acquisition clock with :func:`src.signals.pairing.match_ordered`;
  slow environment channels are taken as-of within a declared age; nothing is
  averaged across signals. Lambda is paired at ``anchor + delay`` with B06's
  frozen delay, and at both bracket ends for the sensitivity.
* **The target is a label, never an input.** The reference phi only decides
  the regime (closed loop / enrichment); it is not a column the plant reads.
* **Split first.** :func:`identify` refuses a held-out recording;
  :func:`evaluate` refuses one that is not held out, one the baseline was
  fitted on, one that informed any of B06's constants, one already scored
  under another baseline (``spent_held_out``), an unfrozen baseline, and a
  physical drive with recordings on both sides of the split (``leaked_drive``).
* **Every ineligible sample says why** (``state``), so an empty result names
  its kind: ``missing_input`` is not ``transient``.
* **Regions are the unit of uncertainty.** Samples inside one steady stretch
  are correlated, so bootstrap intervals resample regions.
* **A revised plant's MAP refuses, never caps.** For a
  ``speed_density_refusing`` plant, a read whose steady MAP at the fitted
  VE(rpm) would exceed ambient is ``manifold_above_ambient`` (outside the
  fitted rpm range: ``outside_ve_fit``), named and not scored
  (:func:`apply_manifold_refusals`). VE(rpm) itself is fitted on
  identification drives only (:func:`identify_ve`).
* **Two versions are compared per sample, never by their aggregates, and
  never on fresh evidence.** :func:`compare_versions` scores every frozen
  version on the identical samples and reports the paired per-sample change;
  it returns no verdict, spends nothing, and refuses a recording that is
  neither in-sample nor already spent (looking would spend it).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.channels import read_channels
from src.analysis.engine_dynamics import breathing_samples
from src.analysis.fuel_plant import (
    MAP_ABOVE_AMBIENT,
    MAP_OUTSIDE_VE_FIT,
    REFUSING_MODELS,
    B06Rules,
    FloatArray,
    FuelPlantProfile,
    FuelPlantRefusalError,
    PlantInputs,
    PulseChain,
    ResolvedParameters,
    VeCurve,
    bootstrap_median,
    predict,
    refuse_spent_elsewhere,
    resolve_parameters,
    sensitivity_cases,
)
from src.analysis.injector_pulse import (
    PULSE_COUNT_MS,
    PulseChainCalibration,
    dead_time_counts,
    predict_pulse,
    trim_word,
    vb_counts_from_volts,
)
from src.preprocessing.drives import physical_drives
from src.session import is_synthetic_session
from src.signals.pairing import PairingPolicy, match_ordered, to_seconds

#: Every state a pulse read can be in, first failing check wins.
STATES: tuple[str, ...] = (
    "not_co_observed",
    "missing_input",
    "cold",
    "fuel_cut",
    "lambda_rail",
    "post_fuel_cut",
    "transient",
    "short_pulse",
    # Set after the prediction, on otherwise eligible reads, by a plant whose
    # MAP refuses (apply_manifold_refusals); a capping plant never sets them.
    MAP_OUTSIDE_VE_FIT,
    MAP_ABOVE_AMBIENT,
    "eligible",
)
#: MAF bands for coverage, g/s.
MAF_BANDS: tuple[float, ...] = (0.0, 5.0, 10.0, 20.0, 40.0, 80.0, math.inf)


@dataclass(frozen=True)
class _Series:
    times: FloatArray
    values: FloatArray

    @property
    def empty(self) -> bool:
        return len(self.times) == 0


def _series(long: pd.DataFrame, spec: str) -> _Series:
    rows = long[long["spec"] == spec].sort_values("timestamp", kind="stable")
    rows = rows[np.isfinite(rows["value"].to_numpy(dtype=float))]
    return _Series(to_seconds(rows["timestamp"]), rows["value"].to_numpy(dtype=float))


def _pair(anchor: FloatArray, series: _Series, tolerance_s: float) -> FloatArray:
    """One-to-one, anchor-referenced pairing; unmatched anchors are NaN."""
    out = np.full(len(anchor), np.nan)
    if series.empty or not len(anchor):
        return out
    policy = PairingPolicy.within(f"{tolerance_s}s", reference="a")
    matched = match_ordered(anchor, series.times, policy)
    out[matched.index_a] = series.values[matched.index_b]
    return out


def _as_of(series: _Series, at: FloatArray, max_age_s: float) -> FloatArray:
    """Nearest read within ``max_age_s`` (reuse allowed: slow environment only)."""
    out = np.full(len(at), np.nan)
    if series.empty:
        return out
    t = series.times
    i = np.searchsorted(t, at)
    lo, hi = np.clip(i - 1, 0, len(t) - 1), np.clip(i, 0, len(t) - 1)
    d_lo, d_hi = np.abs(at - t[lo]), np.abs(t[hi] - at)
    j = np.where(d_lo <= d_hi, lo, hi)
    ok = np.minimum(d_lo, d_hi) <= max_age_s
    out[ok] = series.values[j[ok]]
    return out


def _bracketed(series: _Series, at: FloatArray, max_age_s: float) -> FloatArray:
    """A piecewise-constant reference: the value when the reads either side agree.

    The commanded-phi word is polled near 1 Hz; between two equal reads no more
    than ``max_age_s`` away it held that value, while a nearest-read would
    stamp a step on the wrong side.
    """
    out = np.full(len(at), np.nan)
    if series.empty:
        return out
    t, v = series.times, series.values
    i = np.searchsorted(t, at)
    lo, hi = np.clip(i - 1, 0, len(t) - 1), np.clip(i, 0, len(t) - 1)
    ok = (i > 0) & (i < len(t)) & (at - t[lo] <= max_age_s) & (t[hi] - at <= max_age_s)
    ok &= v[lo] == v[hi]
    out[ok] = v[lo[ok]]
    return out


def _window(
    series: _Series, at: FloatArray, back_s: float, fwd_s: float
) -> tuple[FloatArray, FloatArray, np.ndarray]:
    """Min, max and count of reads in ``[at - back, at + fwd]``."""
    lo = np.searchsorted(series.times, at - back_s, side="left")
    hi = np.searchsorted(series.times, at + fwd_s, side="right")
    mn = np.full(len(at), np.nan)
    mx = np.full(len(at), np.nan)
    for k, (a, b) in enumerate(zip(lo, hi, strict=True)):
        if b > a:
            mn[k] = series.values[a:b].min()
            mx[k] = series.values[a:b].max()
    return mn, mx, (hi - lo)


def _window_median(series: _Series, at: FloatArray, half_s: float) -> FloatArray:
    """Median of one channel's own reads around each anchor (an anti-alias
    filter on a 100 Hz pressure trace, not a cross-signal aggregate)."""
    lo = np.searchsorted(series.times, at - half_s, side="left")
    hi = np.searchsorted(series.times, at + half_s, side="right")
    out = np.full(len(at), np.nan)
    for k, (a, b) in enumerate(zip(lo, hi, strict=True)):
        if b > a:
            out[k] = float(np.median(series.values[a:b]))
    return out


def _last_event_before(events: FloatArray, at: FloatArray) -> FloatArray:
    """Seconds since the most recent event at or before each time (inf if none)."""
    if not len(events):
        return np.full(len(at), np.inf)
    ev = np.sort(events)
    i = np.searchsorted(ev, at, side="right") - 1
    out = np.full(len(at), np.inf)
    ok = i >= 0
    out[ok] = at[ok] - ev[i[ok]]
    return out


def _specs(profile: FuelPlantProfile) -> list[str]:
    s = profile.specs
    return list(
        dict.fromkeys(
            [
                s.pulse,
                s.rpm,
                s.maf,
                s.lambda_observed,
                s.baro,
                s.intake_temp,
                s.coolant,
                s.battery_voltage,
                s.rail_pressure,
                s.bfs,
                s.stft,
                s.ltft,
                s.reference_phi,
                s.fuel_cut_reference,
            ]
        )
    )


def build_samples(session: Path, profile: FuelPlantProfile, delay: B06Rules) -> pd.DataFrame:
    """One row per bank-1 pulse read, every input paired, every state named."""
    long = read_channels(session, _specs(profile), acquired_time=True)
    s, rule, chain = profile.specs, profile.steady, profile.pulse_chain
    pulse = _series(long, s.pulse)
    t = pulse.times
    frame = pd.DataFrame({"session": session.name, "t": t, "pulse_ms": pulse.values})
    if not len(t):
        return frame
    counts = np.rint(pulse.values / PULSE_COUNT_MS)
    rpm_s, maf_s, lam_s = (
        _series(long, s.rpm),
        _series(long, s.maf),
        _series(long, s.lambda_observed),
    )
    frame["rpm"] = _pair(t, rpm_s, rule.pair_tolerance_s)
    frame["maf_gps"] = _pair(t, maf_s, rule.pair_tolerance_s)
    for col, d in (
        ("lambda_obs", delay.seconds),
        ("lambda_obs_lo", delay.bracket[0]),
        ("lambda_obs_hi", delay.bracket[1]),
    ):
        frame[col] = _pair(t + d, lam_s, rule.pair_tolerance_s)
    frame["baro_kpa"] = _as_of(_series(long, s.baro), t, rule.slow_max_age_s)
    frame["iat_c"] = _as_of(_series(long, s.intake_temp), t, rule.slow_max_age_s)
    frame["coolant_c"] = _as_of(_series(long, s.coolant), t, rule.slow_max_age_s)
    frame["vb_v"] = _as_of(_series(long, s.battery_voltage), t, rule.battery_max_age_s)
    rail = _series(long, s.rail_pressure)
    frame["rail_kpa"] = _window_median(rail, t, rule.rail_window_s)
    # ECU-side reads, for the pulse-chain check only.
    bfs = _series(long, s.bfs)
    frame["bfs"] = _pair(t, bfs, rule.ecu_pair_tolerance_s)
    frame["bfs_before"] = _as_of(bfs, t - chain.check_bfs_lag_s, rule.ecu_pair_tolerance_s * 2)
    frame["stft_pct"] = _as_of(_series(long, s.stft), t, rule.pair_tolerance_s * 2)
    frame["ltft_pct"] = _as_of(_series(long, s.ltft), t, rule.slow_max_age_s)
    phi = _bracketed(_series(long, s.reference_phi), t, rule.reference_phi_max_age_s)
    frame["reference_phi"] = phi
    cut_ref = _as_of(_series(long, s.fuel_cut_reference), t, rule.ecu_pair_tolerance_s * 3)

    # The ECM's dead time: from PID 0x42 where logged, else from this
    # session's own fuel-cut pulse reads (in cut the word *is* Ts).
    base, slope = chain.dead_time_base.value, chain.dead_time_slope.value
    ts_vb = dead_time_counts(
        vb_counts_from_volts(np.nan_to_num(frame["vb_v"].to_numpy(), nan=0.0)),
        base=base,
        slope=slope,
        vref=chain.dead_time_vref,
    ).astype(float)
    cut_reads = counts[cut_ref == 0]
    ts_cut = float(np.median(cut_reads)) if len(cut_reads) else math.nan
    has_vb = np.isfinite(frame["vb_v"].to_numpy())
    frame["ecu_dead_time_counts"] = np.where(has_vb, ts_vb, ts_cut)
    frame["dead_time_source"] = np.where(
        has_vb, "battery_voltage", "fuel_cut_reads" if len(cut_reads) else "none"
    )
    frame["ecu_dead_time_ms"] = frame["ecu_dead_time_counts"] * PULSE_COUNT_MS
    frame["fuel_cut_reads"] = len(cut_reads)

    regime = np.full(len(t), "unknown", dtype=object)
    band = delay.closed_loop_band
    regime[np.abs(phi - 1.0) <= band] = "closed_loop"
    regime[phi > 1.0 + band] = "enrichment"
    regime[phi < 1.0 - band] = "lean_target"
    frame["regime"] = regime

    # Fuel-cut events: a pulse word at the dead time, or lambda on its rail.
    lam_all = lam_s.values
    cut_pulse = counts - frame["ecu_dead_time_counts"].to_numpy() <= rule.fuel_cut_margin_counts
    events = np.concatenate([t[cut_pulse], lam_s.times[lam_all >= rule.lambda_rail]])
    since_cut = _last_event_before(events, t + delay.bracket[1])
    holdoff = np.where(
        regime == "enrichment", delay.holdoff_enrichment_s, delay.holdoff_closed_loop_s
    )

    rmin, rmax, rn = _window(rpm_s, t, rule.window_s, 0.0)
    mmin, mmax, mn = _window(maf_s, t, rule.window_s, 0.0)
    pmin, pmax, pn = _window(pulse, t, rule.window_s, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        steady = (
            ((rmax - rmin) / frame["rpm"].to_numpy() <= rule.rpm_span)
            & ((mmax - mmin) / frame["maf_gps"].to_numpy() <= rule.maf_span)
            & ((pmax - pmin) / pulse.values <= rule.pulse_span)
            & (rn >= rule.min_rpm_reads)
            & (mn >= rule.min_maf_reads)
            & (pn >= rule.min_pulse_reads)
        )
    effective = pulse.values - frame["ecu_dead_time_ms"].to_numpy()
    needed = ["rpm", "maf_gps", "lambda_obs", "baro_kpa", "iat_c", "coolant_c", "ecu_dead_time_ms"]
    missing = ~np.all(np.isfinite(frame[needed].to_numpy(dtype=float)), axis=1)
    lam = frame["lambda_obs"].to_numpy()
    lam_edges = np.fmax(frame["lambda_obs_lo"].to_numpy(), frame["lambda_obs_hi"].to_numpy())
    state = np.full(len(t), "eligible", dtype=object)
    checks: list[tuple[str, np.ndarray]] = [
        ("short_pulse", effective < rule.min_effective_pulse_ms),
        ("transient", ~steady),
        ("post_fuel_cut", since_cut < holdoff),
        ("lambda_rail", (lam >= rule.lambda_rail) | (lam_edges >= rule.lambda_rail)),
        ("fuel_cut", cut_pulse),
        ("cold", frame["coolant_c"].to_numpy() < rule.min_coolant_c),
        ("missing_input", missing),
        # Pulse read with no rpm, MAF or lambda partner at all: the profile
        # polled Mode 22 and Mode 01 apart (session 0xC0), not a gap in one input.
        (
            "not_co_observed",
            ~np.isfinite(frame[["rpm", "maf_gps", "lambda_obs"]].to_numpy(dtype=float)).any(axis=1),
        ),
    ]
    for name, mask in checks:  # last assignment wins, so STATES order is precedence
        state[np.asarray(mask, dtype=bool)] = name
    frame["state"] = state
    frame["steady_window"] = steady
    frame["fuelled"] = ~cut_pulse & (effective >= rule.min_effective_pulse_ms)
    frame["rail_measured"] = np.isfinite(frame["rail_kpa"].to_numpy())
    return _regions(frame, rule.region_gap_s)


def _regions(frame: pd.DataFrame, gap_s: float) -> pd.DataFrame:
    """Number consecutive eligible reads (same regime, gaps <= ``gap_s``)."""
    region = np.full(len(frame), -1)
    current, last_t, last_regime = -1, -math.inf, None
    for k, (st, tt, rg) in enumerate(zip(frame["state"], frame["t"], frame["regime"], strict=True)):
        if st != "eligible":
            continue
        if tt - last_t > gap_s or rg != last_regime:
            current += 1
        region[k], last_t, last_regime = current, tt, rg
    frame = frame.copy()
    frame["region"] = region
    frame["region_id"] = [
        f"{s}#{r}" if r >= 0 else "" for s, r in zip(frame["session"], region, strict=True)
    ]
    return frame


def plant_inputs(frame: pd.DataFrame) -> PlantInputs:
    """The plant's inputs from a sample frame. The reference phi is not one."""

    def col(name: str) -> FloatArray:
        values: FloatArray = frame[name].to_numpy(dtype=float)
        return values

    return PlantInputs(
        pulse_ms=col("pulse_ms"),
        ecu_dead_time_ms=col("ecu_dead_time_ms"),
        rpm=col("rpm"),
        maf_gps=col("maf_gps"),
        baro_kpa=col("baro_kpa"),
        iat_c=col("iat_c"),
        rail_kpa=col("rail_kpa"),
    )


# -- identification ------------------------------------------------------------------


def _refuse_sessions(names: Iterable[str], refused: Iterable[str], reason: str, why: str) -> None:
    bad = sorted(set(names) & set(refused))
    if bad:
        raise FuelPlantRefusalError(reason, f"{bad}: {why}")


def check_origin(sessions: Iterable[str], profile: FuelPlantProfile) -> None:
    """Simulator output never identifies or scores a real plant, and a labelled
    synthetic plant never claims a recording: ``synthetic_as_observed`` /
    ``synthetic_plant_on_recording``."""
    names = sorted(set(sessions))
    synthetic = [n for n in names if is_synthetic_session(n)]
    recorded = [n for n in names if not is_synthetic_session(n)]
    if not profile.synthetic and synthetic:
        raise FuelPlantRefusalError(
            "synthetic_as_observed",
            f"{synthetic} are simulator output: a synthetic trace is never plant evidence",
        )
    if profile.synthetic and recorded:
        raise FuelPlantRefusalError(
            "synthetic_plant_on_recording",
            f"a labelled synthetic plant is identified and scored on _sim-* traces only, "
            f"not {recorded}",
        )


def check_drive_independence(profile: FuelPlantProfile, sessions_root: Path) -> list[Any]:
    """Refuse a physical drive with recordings on both sides of the split.

    Grouped by :func:`src.preprocessing.drives.physical_drives` over every
    split recording under ``sessions_root`` (siblings bridge, as everywhere);
    a recording that is not on disk is placed at its name's start time.
    """
    split = profile.split
    names = [*split.identification, *split.held_out]
    drives = physical_drives([sessions_root / n for n in names])
    for drive in drives:
        ident = sorted(set(drive.sessions) & set(split.identification))
        held = sorted(set(drive.sessions) & set(split.held_out))
        if ident and held:
            raise FuelPlantRefusalError(
                "leaked_drive",
                f"{drive.drive_id} holds identification {ident} and held-out {held}",
            )
    return drives


def apply_manifold_refusals(
    frame: pd.DataFrame, profile: FuelPlantProfile, params: ResolvedParameters
) -> pd.DataFrame:
    """Name the eligible reads a refusing plant's MAP refuses.

    A ``speed_density_capped`` plant returns the frame unchanged (its MAP
    never refuses), so fuel-mv-1's samples are bit-identical. Otherwise each
    eligible read whose MAP is refused takes the refusal as its state; regions
    keep their ids (a refused read drops out of its region)."""
    if params.manifold_model not in REFUSING_MODELS or not len(frame):
        return frame
    eligible = (frame["state"] == "eligible").to_numpy()
    if not eligible.any():
        return frame
    out = predict(plant_inputs(frame[eligible]), params, profile.geometry)
    assert out.manifold_refusal is not None
    state = frame["state"].to_numpy(dtype=object).copy()
    idx = np.flatnonzero(eligible)
    refused = out.manifold_refusal != ""
    state[idx[refused]] = out.manifold_refusal[refused]
    frame = frame.copy()
    frame["state"] = state
    return frame


def ve_samples(session: Path, profile: FuelPlantProfile) -> pd.DataFrame:
    """Per MAF read, the charge fraction (``engine_dynamics.breathing_samples``:
    rpm paired one-to-one on the acquisition clock, baro and IAT as-of) and
    whether the read is steady under the plant's own rule (rpm and MAF spans
    over the preceding ``window_s``, with the minimum read counts)."""
    out = breathing_samples(session, profile)
    rule = profile.steady
    t = out["t"].to_numpy(dtype=float)
    rpm = out["rpm"].to_numpy(dtype=float)
    ok = np.isfinite(rpm)
    rmin, rmax, rn = _window(_Series(t[ok], rpm[ok]), t, rule.window_s, 0.0)
    maf = _Series(t, out["maf_gps"].to_numpy(dtype=float))
    mmin, mmax, mn = _window(maf, t, rule.window_s, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        steady = (
            ((rmax - rmin) / rpm <= rule.rpm_span)
            & ((mmax - mmin) / maf.values <= rule.maf_span)
            & (rn >= rule.min_rpm_reads)
            & (mn >= rule.min_maf_reads)
        )
    out["steady"] = steady
    return out


def identify_ve(samples: pd.DataFrame, profile: FuelPlantProfile) -> dict[str, Any]:
    """Fit VE(rpm) on identification drives: per declared rpm band, the
    largest steady warm charge fraction (:class:`~src.analysis.fuel_plant.VeCurve`).

    On a naturally aspirated engine ``charge fraction = VE x p_m / p_ambient
    <= VE`` on every read, so the envelope is the smallest VE consistent with
    the identification drives, and VE itself where they reached full
    breathing. It is identified from MAF, rpm, baro and IAT alone: a MAF bias
    is a VE bias here, and nothing measures VE independently. Refuses any
    recording outside ``split.identification``, and a band whose steady
    charge fraction exceeds 1 (the airflow boundary is wrong there).
    """
    rule = profile.volumetric_efficiency
    if rule is None:
        raise FuelPlantRefusalError(
            "no_ve_fit", f"{profile.plant_version} runs one working VE; nothing to fit"
        )
    sessions = set(samples["session"])
    check_origin(sessions, profile)
    outside = sorted(sessions - set(profile.split.identification))
    if outside:
        raise FuelPlantRefusalError(
            "not_identification", f"{outside} are not declared identification recordings"
        )
    floor = profile.registry.get("manifold_pressure").working_assumption
    assert floor is not None
    edges = np.asarray(rule.rpm_edges, dtype=float)
    rpm = samples["rpm"].to_numpy(dtype=float)
    cf = samples["charge_fraction"].to_numpy(dtype=float)
    used = (
        samples["steady"].to_numpy(dtype=bool)
        & np.isfinite(cf)
        & (samples["coolant_c"].to_numpy(dtype=float) >= profile.steady.min_coolant_c)
        & (rpm >= edges[0])
        & (rpm <= edges[-1])
    )
    band = np.clip(np.searchsorted(edges, rpm, side="right") - 1, 0, len(edges) - 2)
    envelope: list[float | None] = []
    n_reads: list[int] = []
    bands: dict[str, dict[str, Any]] = {}
    for k in range(len(edges) - 1):
        values = cf[used & (band == k)]
        n_reads.append(len(values))
        top = float(values.max()) if len(values) else math.nan
        if len(values) and top > 1.0:
            raise FuelPlantRefusalError(
                "charge_fraction_above_one",
                f"rpm {edges[k]:g}-{edges[k + 1]:g}: {int((values > 1.0).sum())} steady reads "
                "fill more than the displacement at ambient; the airflow boundary is wrong there",
            )
        fitted = len(values) >= rule.min_reads
        envelope.append(top if fitted else None)
        bands[f"{edges[k]:g}-{edges[k + 1]:g}"] = {
            "n": len(values),
            "envelope": top if fitted else None,
            "p99": float(np.quantile(values, 0.99)) if len(values) else None,
            "above_floor": int((values > floor.value).sum()),
            "runs_at": max(top, floor.value) if fitted else floor.value,
        }
    if not used.any():
        raise FuelPlantRefusalError("no_ve_reads", "no steady warm read to fit VE on")
    baro = samples["baro_kpa"].to_numpy(dtype=float)[used]
    iat = samples["iat_c"].to_numpy(dtype=float)[used]
    curve = VeCurve(
        rpm_edges=list(rule.rpm_edges),
        envelope=envelope,
        n_reads=n_reads,
        baro_kpa=(float(baro.min()), float(baro.max())),
        iat_c=(float(iat.min()), float(iat.max())),
    )
    return {
        "ve_curve": curve,
        "floor": floor.value,
        "bands": bands,
        "reads_used": int(used.sum()),
        "reads": len(samples),
        "sessions": sorted(sessions),
    }


def identify(
    frames: Sequence[pd.DataFrame],
    profile: FuelPlantProfile,
    delay: B06Rules,
    *,
    sessions_root: Path,
    ve_curve: VeCurve | None = None,
) -> dict[str, Any]:
    """Fit the lumped stoichiometric-air-per-open-ms scale on identification drives.

    Per sample the ratio ``lambda_pred(s=1) / lambda_obs`` *is* that sample's
    ``s``; the fit is the median of region medians, with a bootstrap over
    regions. Refuses any recording outside ``split.identification``. A
    ``speed_density_refusing`` plant is handed the VE(rpm) :func:`identify_ve`
    fitted on the same drives, and its MAP refusals are named first.
    """
    data = pd.concat([f for f in frames if len(f)], ignore_index=True)
    sessions = set(data["session"])
    check_origin(sessions, profile)
    outside = sorted(sessions - set(profile.split.identification))
    if outside:
        raise FuelPlantRefusalError(
            "not_identification", f"{outside} are not declared identification recordings"
        )
    check_drive_independence(profile, sessions_root)
    params = resolve_parameters(profile, delay, for_identification=True, ve_curve=ve_curve)
    data = apply_manifold_refusals(data, profile, params)
    eligible = data[data["state"] == "eligible"]
    out = predict(plant_inputs(eligible), params, profile.geometry)
    ratio = out.lambda_delivered / eligible["lambda_obs"].to_numpy(dtype=float)
    groups = [
        ratio[(eligible["region_id"] == r).to_numpy()] for r in eligible["region_id"].unique()
    ]
    acc = profile.acceptance
    value, lo, hi = bootstrap_median(
        groups, resamples=acc.bootstrap_resamples, level=acc.bootstrap_level, seed=acc.seed
    )
    lumped = profile.registry.get("stoich_air_per_open_ms")
    per_session = {
        name: float(np.median(ratio[(eligible["session"] == name).to_numpy()]))
        for name in sorted(set(eligible["session"]))
    }
    return {
        "parameter": "stoich_air_per_open_ms",
        "value": value,
        "interval": (lo, hi),
        "n_samples": len(ratio),
        "n_regions": len(groups),
        "sample_p10": float(np.quantile(ratio, 0.1)) if len(ratio) else math.nan,
        "sample_p90": float(np.quantile(ratio, 0.9)) if len(ratio) else math.nan,
        "per_session_median": per_session,
        "prior": lumped.prior,
        "fit_over_prior": value / lumped.prior if lumped.prior else math.nan,
        "states": data["state"].value_counts().to_dict(),
        "sessions": sorted(sessions),
        # The plant at the fitted scale, scored on the drives it was fitted on.
        # In-sample: a miss here is the model's structure, not generalisation.
        "in_sample": _stats(
            100.0 * (ratio / value - 1.0), eligible["region_id"].to_numpy(), profile
        ),
        "in_sample_null_lambda_one": _stats(
            100.0 * (1.0 / eligible["lambda_obs"].to_numpy(dtype=float) - 1.0),
            eligible["region_id"].to_numpy(),
            profile,
        ),
    }


# -- evaluation ----------------------------------------------------------------------


def _region_groups(values: np.ndarray, region_ids: np.ndarray) -> list[np.ndarray]:
    return [values[region_ids == r] for r in pd.unique(region_ids)]


def _stats(err: np.ndarray, regions: np.ndarray, profile: FuelPlantProfile) -> dict[str, Any]:
    acc = profile.acceptance
    groups = _region_groups(err, regions)
    med, lo, hi = bootstrap_median(
        groups, resamples=acc.bootstrap_resamples, level=acc.bootstrap_level, seed=acc.seed
    )
    region_medians = np.array([float(np.median(g)) for g in groups]) if groups else np.array([])
    return {
        "n_samples": len(err),
        "n_regions": len(groups),
        "median_pct": float(np.median(err)) if len(err) else math.nan,
        "p10_pct": float(np.quantile(err, 0.1)) if len(err) else math.nan,
        "p90_pct": float(np.quantile(err, 0.9)) if len(err) else math.nan,
        "abs_p90_pct": float(np.quantile(np.abs(err), 0.9)) if len(err) else math.nan,
        "region_median_of_medians_pct": med,
        "region_interval_pct": (lo, hi),
        "region_pass_fraction": (
            float(np.mean(np.abs(region_medians) <= acc.region_abs_pct))
            if len(region_medians)
            else math.nan
        ),
    }


def _errors(
    eligible: pd.DataFrame,
    profile: FuelPlantProfile,
    delay: B06Rules,
    overrides: Mapping[str, float] | None = None,
    lambda_column: str = "lambda_obs",
) -> np.ndarray:
    params = resolve_parameters(profile, delay, overrides=overrides)
    out = predict(plant_inputs(eligible), params, profile.geometry)
    err: np.ndarray = 100.0 * (
        out.lambda_delivered / eligible[lambda_column].to_numpy(dtype=float) - 1.0
    )
    return err


def evaluate(
    frames: Sequence[pd.DataFrame],
    profile: FuelPlantProfile,
    delay: B06Rules,
    *,
    sessions_root: Path,
    profile_dir: Path,
) -> dict[str, Any]:
    """Score the frozen plant on held-out steady regions against predeclared bounds.

    ``profile_dir`` is where the plant is declared: every plant version's
    spent ledger there is read (``fuel_plant.refuse_spent_elsewhere``), so a
    drive a sibling version spent is refused even for a profile built in
    memory. A plant with no declared held-out recording is refused by name
    (``held_out_pending``).

    Returns ``spent_records``: every recording scored, to be added to the
    profile's ``spent_held_out`` (the CLI does it). From then on the profile
    refuses to hold those recordings out for any other baseline or plant.
    """
    baseline = profile.baseline
    if baseline is None or not baseline.frozen:
        raise FuelPlantRefusalError(
            "baseline_not_frozen", "freeze the hardware parameter set before held-out evaluation"
        )
    if not profile.split.held_out:
        raise FuelPlantRefusalError(
            "held_out_pending",
            f"{profile.plant_version} declares no held-out recording: "
            f"{profile.split.held_out_pending}",
        )
    present = [f for f in frames if len(f)]
    if not present:
        raise FuelPlantRefusalError("no_samples", "no pulse read in any frame to score")
    data = pd.concat(present, ignore_index=True)
    sessions = set(data["session"])
    check_origin(sessions, profile)
    outside = sorted(sessions - set(profile.split.held_out))
    if outside:
        raise FuelPlantRefusalError("not_held_out", f"{outside} are not declared held-out")
    _refuse_sessions(sessions, baseline.fitted_on, "leaked_fit", "the baseline was fitted on them")
    _refuse_sessions(
        sessions, delay.informed_by, "leaked_b06", "they informed B06's delay or regime rules"
    )
    refuse_spent_elsewhere(profile_dir, profile, sorted(sessions))
    sha = baseline.sha256()
    spent_elsewhere = sorted(
        name
        for name, rec in profile.spent().items()
        if name in sessions
        and (rec.baseline_sha256, rec.plant_version) != (sha, profile.plant_version)
    )
    if spent_elsewhere:
        raise FuelPlantRefusalError(
            "spent_held_out",
            f"{spent_elsewhere} were scored under another baseline or plant: not held-out evidence",
        )
    check_drive_independence(profile, sessions_root)

    params = resolve_parameters(profile, delay)
    data = apply_manifold_refusals(data, profile, params)
    eligible = data[data["state"] == "eligible"].reset_index(drop=True)
    out = predict(plant_inputs(eligible), params, profile.geometry)
    lam_obs = eligible["lambda_obs"].to_numpy(dtype=float)
    err = 100.0 * (out.lambda_delivered / lam_obs - 1.0)
    fuel_err = 100.0 * (lam_obs / out.lambda_delivered - 1.0)
    regions = eligible["region_id"].to_numpy()
    acc = profile.acceptance

    pooled = _stats(err, regions, profile)
    fuel = _stats(fuel_err, regions, profile)
    enrich_mask = (eligible["regime"] == "enrichment").to_numpy()
    enrichment = _stats(err[enrich_mask], regions[enrich_mask], profile)
    closed_mask = (eligible["regime"] == "closed_loop").to_numpy()
    closed = _stats(err[closed_mask], regions[closed_mask], profile)
    # Null model: predict lambda = 1 always. In closed loop the ECM holds the
    # sensor near 1, so a plant that only matches there has shown little.
    null = _stats(100.0 * (1.0 / lam_obs - 1.0), regions, profile)

    # Parameter uncertainty: the lumped scale's interval moves lambda as 1/s.
    s_val = baseline.values["stoich_air_per_open_ms"]
    s_lo, s_hi = baseline.intervals["stoich_air_per_open_ms"]
    parameter_band = (100.0 * (s_val / s_hi - 1.0), 100.0 * (s_val / s_lo - 1.0))

    sensitivity: dict[str, float] = {}
    for name, case in sensitivity_cases(profile, delay).items():
        if "lambda_observation_delay" in case:
            col = "lambda_obs_lo" if name.endswith("=lo") else "lambda_obs_hi"
            ok = np.isfinite(eligible[col].to_numpy(dtype=float))
            e = _errors(eligible[ok], profile, delay, lambda_column=col)
        else:
            e = _errors(eligible, profile, delay, overrides=case)
        # A refusing plant at a bracket end can refuse a read the working case
        # scored (its MAP crosses ambient there): the shift is over the rest.
        e = e[np.isfinite(e)]
        sensitivity[name] = float(np.median(e)) - pooled["median_pct"] if len(e) else math.nan

    fuelled = data[~data["state"].isin(["fuel_cut", "cold", "missing_input", "not_co_observed"])]
    maf_band = pd.cut(eligible["maf_gps"], list(MAF_BANDS), right=False).astype(str)
    coverage = {
        "eligible_of_fuelled_warm": (
            float(len(eligible) / len(fuelled)) if len(fuelled) else math.nan
        ),
        "states": {k: int(v) for k, v in data["state"].value_counts().items()},
        "regions_by_regime": eligible.groupby("regime")["region_id"].nunique().to_dict(),
        "regions_by_maf_band": eligible.groupby(maf_band)["region_id"].nunique().to_dict(),
        "median_error_by_maf_band_pct": pd.Series(err)
        .groupby(maf_band.to_numpy())
        .agg(["size", "median"])
        .to_dict(orient="index"),
        "rail_measured_fraction": float(eligible["rail_measured"].mean()) if len(eligible) else 0.0,
        "dead_time_source": eligible["dead_time_source"].value_counts().to_dict(),
    }
    unresolved = list(params.effects)
    if not eligible["rail_measured"].all():
        unresolved.append(params.rail_effect)

    per_session = {
        name: _stats(
            err[(eligible["session"] == name).to_numpy()],
            regions[(eligible["session"] == name).to_numpy()],
            profile,
        )
        for name in sorted(set(eligible["session"]))
    }
    rail_split = {
        label: _stats(err[mask], regions[mask], profile)
        for label, mask in (
            ("rail_measured", eligible["rail_measured"].to_numpy(dtype=bool)),
            ("rail_assumed", ~eligible["rail_measured"].to_numpy(dtype=bool)),
        )
        if mask.any()
    }

    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, value: float, bound: float, ok: bool | None) -> None:
        checks[name] = {"value": value, "bound": bound, "pass": ok}

    enough = pooled["n_regions"] >= acc.min_regions
    check("min_regions", pooled["n_regions"], acc.min_regions, enough)
    check(
        "pooled_median_abs_pct",
        abs(pooled["median_pct"]),
        acc.pooled_median_abs_pct,
        abs(pooled["median_pct"]) <= acc.pooled_median_abs_pct,
    )
    check(
        "sample_abs_p90_pct",
        pooled["abs_p90_pct"],
        acc.sample_abs_p90_pct,
        pooled["abs_p90_pct"] <= acc.sample_abs_p90_pct,
    )
    check(
        "region_pass_fraction",
        pooled["region_pass_fraction"],
        acc.region_pass_fraction,
        pooled["region_pass_fraction"] >= acc.region_pass_fraction,
    )
    if enrichment["n_regions"] >= acc.min_enrichment_regions:
        check(
            "enrichment_median_abs_pct",
            abs(enrichment["median_pct"]),
            acc.enrichment_median_abs_pct,
            abs(enrichment["median_pct"]) <= acc.enrichment_median_abs_pct,
        )
    else:
        # Not a pass: too little enrichment to judge says so by name.
        check("enrichment_median_abs_pct", math.nan, acc.enrichment_median_abs_pct, None)
    verdict = (
        "pass"
        if enough and all(c["pass"] for c in checks.values() if c["pass"] is not None)
        else "fail"
    )
    if verdict == "pass" and checks["enrichment_median_abs_pct"]["pass"] is None:
        verdict = "pass_closed_loop_only"
    return {
        "verdict": verdict,
        "checks": checks,
        "baseline_sha256": params.baseline_sha256,
        "plant_version": profile.plant_version,
        "lambda": pooled,
        "fuel": fuel,
        "closed_loop": closed,
        "enrichment": enrichment,
        "null_lambda_one": null,
        "per_session": per_session,
        "rail": rail_split,
        "parameter_band_pct": parameter_band,
        "sensitivity_median_shift_pct": sensitivity,
        "coverage": coverage,
        "unresolved_effects": sorted(set(unresolved)),
        "sessions": sorted(sessions),
        "spent_records": [
            {
                "recording": name,
                "baseline_id": baseline.id,
                "baseline_sha256": sha,
                "plant_version": profile.plant_version,
                "verdict": verdict,
            }
            for name in sorted(sessions)
        ],
    }


# -- comparing plant versions ---------------------------------------------------------


def compare_versions(
    frames: Sequence[pd.DataFrame], profiles: Sequence[FuelPlantProfile], delay: B06Rules
) -> dict[str, Any]:
    """Every frozen version's lambda error on the identical samples, per sample.

    A diagnostic, never a verdict: it returns no ``verdict`` and no spent
    records, so nothing here can qualify a plant. It refuses a recording that
    is neither in-sample for every version (in every baseline's ``fitted_on``)
    nor already spent by one of them: a never-scored drive is held-out
    evidence, and looking at it would spend it. Samples are built once (the
    versions must share their sample rules); a read any version refuses is
    counted by version and scored by none. The comparison is the per-sample
    change ``|err_b| - |err_a|`` against the first version, never two
    aggregates side by side.
    """
    if len(profiles) < 2:
        raise FuelPlantRefusalError("one_version", "a comparison needs two plant versions")
    first = profiles[0]
    for other in profiles[1:]:
        for name in ("geometry", "specs", "steady", "pulse_chain", "delay", "command_specs"):
            if getattr(other, name) != getattr(first, name):
                raise FuelPlantRefusalError(
                    "sample_rules_differ",
                    f"{other.plant_version} and {first.plant_version} differ in {name}: their "
                    "samples are not the same samples",
                )
        if other.hardware_configuration != first.hardware_configuration:
            raise FuelPlantRefusalError("hardware_mismatch", "one hardware configuration only")
    for p in profiles:
        if p.baseline is None or not p.baseline.frozen:
            raise FuelPlantRefusalError(
                "baseline_not_frozen", f"{p.plant_version}: freeze before comparing"
            )
    data = pd.concat([f for f in frames if len(f)], ignore_index=True)
    sessions = sorted(set(data["session"]))
    for p in profiles:
        check_origin(sessions, p)
    label: dict[str, str] = {}
    for name in sessions:
        fitted = [name in (p.baseline.fitted_on if p.baseline else ()) for p in profiles]
        if all(fitted):
            label[name] = "in_sample"
        elif not any(fitted) and any(name in p.spent() for p in profiles):
            label[name] = "spent_held_out"
        else:
            raise FuelPlantRefusalError(
                "unspent_recording",
                f"{name} is neither in-sample for every version nor already spent: a "
                "comparison on it would spend held-out evidence",
            )

    versions = [p.plant_version for p in profiles]
    states: dict[str, np.ndarray] = {}
    for p in profiles:
        params = resolve_parameters(p, delay)
        states[p.plant_version] = apply_manifold_refusals(data, p, params)["state"].to_numpy()
    base_eligible = (data["state"] == "eligible").to_numpy()
    common = base_eligible.copy()
    for st in states.values():
        common &= st == "eligible"
    refused = {
        v: {
            str(k): int(c)
            for k, c in pd.Series(st[base_eligible & (st != "eligible")]).value_counts().items()
        }
        for v, st in states.items()
    }
    rows = data[common].reset_index(drop=True)
    lam_obs = rows["lambda_obs"].to_numpy(dtype=float)
    errors: dict[str, np.ndarray] = {}
    for p in profiles:
        out = predict(plant_inputs(rows), resolve_parameters(p, delay), p.geometry)
        errors[p.plant_version] = 100.0 * (out.lambda_delivered / lam_obs - 1.0)
    regions = rows["region_id"].to_numpy()
    groups = rows["session"].map(label).to_numpy()
    maf_band = pd.cut(rows["maf_gps"], list(MAF_BANDS), right=False).astype(str).to_numpy()

    def change_summary(change: np.ndarray) -> dict[str, Any]:
        return {
            "n": len(change),
            "median": float(np.median(change)) if len(change) else math.nan,
            "p10": float(np.quantile(change, 0.1)) if len(change) else math.nan,
            "p90": float(np.quantile(change, 0.9)) if len(change) else math.nan,
            "fraction_better": float(np.mean(change < 0)) if len(change) else math.nan,
            "fraction_worse": float(np.mean(change > 0)) if len(change) else math.nan,
        }

    def paired(mask: np.ndarray) -> dict[str, Any]:
        out: dict[str, Any] = {
            "versions": {v: _stats(errors[v][mask], regions[mask], first) for v in versions}
        }
        base = np.abs(errors[versions[0]][mask])
        bands = maf_band[mask]
        for v in versions[1:]:
            change = np.abs(errors[v][mask]) - base
            key = f"abs_error_change_{v}_minus_{versions[0]}_pct"
            out[key] = change_summary(change)
            # Per MAF band, still per sample: never two band medians side by side.
            out[f"{key}_by_maf_band"] = {
                str(b): change_summary(change[bands == b]) for b in sorted(set(bands))
            }
        return out

    result: dict[str, Any] = {
        "diagnostic": "not held-out evidence: no verdict, nothing spent",
        "versions": versions,
        "baselines": {
            p.plant_version: p.baseline.sha256() for p in profiles if p.baseline is not None
        },
        "sessions": label,
        "refused_by_version": refused,
        "common_samples": int(common.sum()),
    }
    for group in ("in_sample", "spent_held_out"):
        mask = groups == group
        if not mask.any():
            continue
        result[group] = paired(mask)
    return result


# -- the ECM's pulse chain: ground truth -------------------------------------------


def calibration_from_image(image: bytes, chain: PulseChain) -> PulseChainCalibration:
    """Read the chain's calibration from a whole image; refuse a declared mismatch."""

    def word(cw: Any) -> int:
        raw = int.from_bytes(image[cw.address : cw.address + cw.width], "big")
        if raw != cw.value:
            raise FuelPlantRefusalError(
                "calibration_mismatch",
                f"0x{cw.address:X} reads {raw}, the profile declares {cw.value}: another program?",
            )
        return raw

    return PulseChainCalibration(
        dead_time_base=word(chain.dead_time_base),
        dead_time_slope=word(chain.dead_time_slope),
        dead_time_vref=chain.dead_time_vref,
        rail_reference=word(chain.rail_reference),
        flow_scaler=word(chain.flow_scaler),
        vacuum_load_curve=tuple(image[chain.vacuum_load_curve : chain.vacuum_load_curve + 16]),
        vacuum_rpm_curve=tuple(image[chain.vacuum_rpm_curve : chain.vacuum_rpm_curve + 16]),
        minimum_g=chain.minimum_g,
    )


def without_pressure_correction(cal: PulseChainCalibration) -> PulseChainCalibration:
    """Negative control: the chain with the ECM's rail-to-manifold correction
    removed (vacuum curves pinned so the estimated vacuum is ~0). The
    firmware session saw the ratio drift 0.97 -> 0.91 with vacuum this way; a
    known answer that this control also passed would prove nothing."""
    return replace(cal, vacuum_load_curve=(255,) * 16, vacuum_rpm_curve=(0,) * 16)


def pulse_chain_ratios(
    frame: pd.DataFrame, cal: PulseChainCalibration, chain: PulseChain
) -> pd.DataFrame:
    """``(DID - Ts) / (predicted - Ts)`` per steady, fuelled pulse read.

    Steady is the plant's rpm/MAF/pulse window plus the check's own BFS rule
    (``chain.check_bfs_span`` over ``chain.check_bfs_lag_s``), with BFS read
    within the ECU pairing tolerance and the phi word bracketed. Lambda is not
    needed: this checks the ECM's arithmetic, not the plant.
    """
    needed = ["bfs", "bfs_before", "rpm", "reference_phi", "stft_pct", "baro_kpa", "iat_c"]
    finite = np.isfinite(frame[[*needed, "ecu_dead_time_counts"]].to_numpy(dtype=float)).all(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        bfs_steady = (frame["bfs"] - frame["bfs_before"]).abs() / frame["bfs"] <= (
            chain.check_bfs_span
        )
    usable = frame[
        frame["steady_window"].astype(bool)
        & frame["fuelled"].astype(bool)
        & finite
        & bfs_steady
        & (frame["reference_phi"] > 0)
    ].copy()
    if not len(usable):
        empty: pd.DataFrame = usable.assign(ratio=pd.Series(dtype=float))
        return empty
    vb = usable["vb_v"].to_numpy(dtype=float)
    pred = predict_pulse(
        bfs=usable["bfs"],
        phi_q14=usable["reference_phi"].to_numpy(dtype=float) * 16384,
        stft_word=trim_word(usable["stft_pct"]),
        ltft_word=trim_word(np.nan_to_num(usable["ltft_pct"].to_numpy(dtype=float))),
        vb_counts=vb_counts_from_volts(np.nan_to_num(vb, nan=cal.dead_time_vref * 0.08)),
        rpm=usable["rpm"],
        baro_kpa=usable["baro_kpa"],
        iat_c=usable["iat_c"],
        cal=cal,
    )
    measured = np.rint(usable["pulse_ms"].to_numpy(dtype=float) / PULSE_COUNT_MS)
    ts = usable["ecu_dead_time_counts"].to_numpy(dtype=float)
    usable["predicted_effective"] = pred.effective
    usable["measured_effective"] = measured - ts
    usable["vacuum_kpa"] = pred.vacuum_kpa
    usable["ratio"] = (measured - ts) / pred.effective
    result: pd.DataFrame = usable
    return result


def summarize_ratios(ratios: np.ndarray) -> dict[str, float]:
    if not len(ratios):
        return {"n": 0, "median": math.nan, "p10": math.nan, "p90": math.nan}
    return {
        "n": len(ratios),
        "median": float(np.median(ratios)),
        "p10": float(np.quantile(ratios, 0.1)),
        "p90": float(np.quantile(ratios, 0.9)),
    }
