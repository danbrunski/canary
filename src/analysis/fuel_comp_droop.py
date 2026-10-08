"""Fuel-supply droop, separated from the air-model error (N02.4).

The fuelling error N02.3 places on Fuel Comp cells is everything between the
fuel the ECM meant to deliver and what the engine got. Part of it is the fuel
pump: rail pressure falls with flow (about -1.9 psi at 107 g/s on 2026-10-04,
``data/analyses/2026-10-04-fuel-pressure-first-drive.md``), and an injector's
flow follows the square root of the pressure across it. The pump is being
replaced, so that part belongs to the pump and the **remainder** is what a
Fuel Comp edit should own.

Per sample (``fuel_comp_fit.yaml`` ``droop:``, :class:`src.analysis.
fuel_comp_inputs.DroopRules`):

- **measured**: the session logged rail pressure. The pressure is the mean
  of the reads within the declared half-width of the sample's acquisition
  time (the 100 Hz trace carries aliased pulsation, so never one read), with
  no fault row inside it, against the **drive's own** low-flow reference (the
  median window mean over engine-running MAF reads below the declared flow);
- **modelled**: elsewhere, ``droop_psi = a x maf + b x maf^2`` fitted on
  every engine-running MAF read of the drives that logged pressure
  (:func:`fit_droop_model`), labelled with its fit, n and drives, applied only
  inside the MAF range it was fitted on, from the pooled reference;
- **none**: neither, with the reason (``droop_basis``).

Flow factor ``f = sqrt(P / P_ref)`` on gauge pressure; droop error
``sqrt(P_ref / P) - 1`` (positive = lean, as the error is); remainder
``(1 + error) / (1 + droop error) - 1``. Rail-to-manifold pressure is not
modelled -- the vehicle has no MAP sensor -- and the declaration must say so
(``limitations`` is required).

:func:`droop_known_answer` reproduces the recorded figure through this path.
Nothing here writes a session or a ROM.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from src.analysis.channels import ChannelSpecError, parse_spec, raw_read_filters, read_channels
from src.analysis.fuel_comp_inputs import DroopRules
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.preprocessing.loader import read_session
from src.signals.pairing import to_seconds
from src.storage.session_files import PARQUET_PATTERN

#: Bumped whenever a kind, a column or the decomposition changes meaning.
DROOP_VERSION = 1

DROOP_KINDS = ("measured", "modelled", "none")

#: Columns :func:`apply_droop` adds, in order.
DROOP_COLUMNS = (
    "droop_kind",
    "droop_basis",
    "pressure_psi",
    "reference_psi",
    "droop_psi",
    "flow_factor",
    "droop_error_pct",
    "remainder_pct",
)

_EMPTY = np.array([], dtype=float)


@dataclass(frozen=True)
class PressureSession:
    """One session's rail pressure (kPa), fault instants, MAF and rpm reads,
    each sorted on its acquisition clock (float seconds)."""

    session: str
    pressure_times: np.ndarray
    pressure_kpa: np.ndarray
    fault_times: np.ndarray
    maf_times: np.ndarray
    maf_gs: np.ndarray
    rpm_times: np.ndarray
    rpm: np.ndarray

    @property
    def captured(self) -> bool:
        return bool(len(self.pressure_times))


def _sorted(frame: pd.DataFrame, spec: str) -> tuple[np.ndarray, np.ndarray]:
    rows = frame[frame["spec"] == spec].sort_values("timestamp", kind="stable")
    if rows.empty:
        return _EMPTY, _EMPTY
    times = np.asarray(to_seconds(pd.to_datetime(rows["timestamp"], utc=True)), dtype=float)
    values = pd.to_numeric(rows["value"], errors="coerce").to_numpy(dtype=float)
    keep = np.isfinite(times) & np.isfinite(values)
    return times[keep], values[keep]


def read_pressure_session(
    session: Path | pd.DataFrame,
    rules: DroopRules,
    *,
    name: str | None = None,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
) -> PressureSession:
    """The reads droop needs from one session (empty arrays when never captured).

    A fault row is a state, not a number (``usb_adc_sensors.yaml``), so its
    instants are taken from the stored rows themselves.
    """
    label = name or (session.name if isinstance(session, Path) else "frame")
    specs = [rules.pressure_channel, rules.fault_channel, rules.maf_channel, rules.rpm_channel]
    rows: pd.DataFrame
    if isinstance(session, Path):
        filters = raw_read_filters(specs, bindings_path=bindings_path, dids_path=dids_path)
        rows = read_session(session, filters=filters or None)
    else:
        rows = session
    numeric = [rules.pressure_channel, rules.maf_channel, rules.rpm_channel]
    try:
        long = read_channels(
            rows, numeric, acquired_time=True, bindings_path=bindings_path, dids_path=dids_path
        )
    except (ChannelSpecError, KeyError, ValueError):
        long = pd.DataFrame(columns=["timestamp", "spec", "value", "unit"])
    pressure_times, pressure = _sorted(long, rules.pressure_channel)
    maf_times, maf = _sorted(long, rules.maf_channel)
    rpm_times, rpm = _sorted(long, rules.rpm_channel)
    fault = parse_spec(rules.fault_channel)
    fault_times = _EMPTY
    if not rows.empty and {"source", "channel", "timestamp"} <= set(rows.columns):
        mask = (rows["source"].astype(str) == fault.source) & (
            rows["channel"].astype(str).str.upper() == fault.name.upper()
        )
        if mask.any():
            stamps = pd.to_datetime(rows.loc[mask, "timestamp"], utc=True)
            fault_times = np.sort(np.asarray(to_seconds(stamps), dtype=float))
    return PressureSession(
        session=label,
        pressure_times=pressure_times,
        pressure_kpa=pressure,
        fault_times=fault_times,
        maf_times=maf_times,
        maf_gs=maf,
        rpm_times=rpm_times,
        rpm=rpm,
    )


def window_pressure(
    session: PressureSession, at: np.ndarray, rules: DroopRules
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(mean kPa, reads, fault)`` within +- the half-width of each instant.

    The mean is NaN where fewer than the declared reads fall in the window;
    ``fault`` is whether a fault row lies inside it.
    """
    at = np.asarray(at, dtype=float)
    width = rules.window_half_width_s
    mean = np.full(len(at), np.nan)
    count = np.zeros(len(at), dtype=int)
    fault = np.zeros(len(at), dtype=bool)
    if not session.captured or not len(at):
        return mean, count, fault
    times, values = session.pressure_times, session.pressure_kpa
    lo = np.searchsorted(times, at - width, side="left")
    hi = np.searchsorted(times, at + width, side="right")
    cumulative = np.concatenate([[0.0], np.cumsum(values)])
    count = (hi - lo).astype(int)
    enough = count >= rules.min_window_reads
    mean[enough] = (cumulative[hi[enough]] - cumulative[lo[enough]]) / count[enough]
    if len(session.fault_times):
        flo = np.searchsorted(session.fault_times, at - width, side="left")
        fhi = np.searchsorted(session.fault_times, at + width, side="right")
        fault = fhi > flo
    return mean, count, fault


def _running_maf(session: PressureSession, rules: DroopRules) -> tuple[np.ndarray, np.ndarray]:
    """Every MAF read with the engine running (rpm as of it, within the age)."""
    if not len(session.maf_times) or not len(session.rpm_times):
        return _EMPTY, _EMPTY
    index = np.searchsorted(session.rpm_times, session.maf_times, side="right") - 1
    ok = index >= 0
    age = np.full(len(index), np.inf)
    age[ok] = session.maf_times[ok] - session.rpm_times[index[ok]]
    rpm = np.full(len(index), np.nan)
    fresh = ok & (age <= rules.rpm_max_age_s)
    rpm[fresh] = session.rpm[index[fresh]]
    running = fresh & (rpm >= rules.running_rpm_min)
    return session.maf_times[running], session.maf_gs[running]


@dataclass(frozen=True)
class DriveReference:
    """One drive's low-flow rail pressure, or why it has none."""

    session: str
    psi: float | None
    n: int
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"session": self.session, "psi": self.psi, "n": self.n, "reason": self.reason}


def _flow_pairs(session: PressureSession, rules: DroopRules) -> tuple[np.ndarray, np.ndarray]:
    """``(maf, window psi)`` for every engine-running MAF read whose window
    holds enough reads and no fault."""
    times, maf = _running_maf(session, rules)
    mean, _, fault = window_pressure(session, times, rules)
    ok = np.isfinite(mean) & ~fault
    return maf[ok], mean[ok] / rules.kpa_per_psi


def drive_reference(
    session: PressureSession | Sequence[PressureSession],
    rules: DroopRules,
    *,
    drive: str | None = None,
) -> DriveReference:
    """The declared reference: median window pressure over engine-running MAF
    reads below the declared flow, from at least the declared reads, pooled
    over every session of one drive (``fit.drive_unit``)."""
    ref = rules.reference
    parts = [session] if isinstance(session, PressureSession) else list(session)
    label = drive or "+".join(p.session for p in parts)
    if not any(p.captured for p in parts):
        return DriveReference(label, None, 0, f"never_captured: no {rules.pressure_channel}")
    if not any(len(p.maf_times) for p in parts):
        return DriveReference(label, None, 0, f"never_captured: no {rules.maf_channel}")
    pooled = [_flow_pairs(p, rules) for p in parts]
    maf = np.concatenate([m for m, _ in pooled])
    psi = np.concatenate([v for _, v in pooled])
    session = parts[0]
    low = maf < ref.maf_max_gs
    n = int(low.sum())
    if n < ref.min_reads:
        return DriveReference(
            label,
            None,
            n,
            f"no_low_flow_reference: {n} engine-running reads below {ref.maf_max_gs:g} g/s "
            f"with a full pressure window, the reference needs {ref.min_reads}",
        )
    return DriveReference(label, float(np.median(psi[low])), n, None)


@dataclass(frozen=True)
class DroopModel:
    """The fitted droop-versus-MAF relation, or the refusal that stands for it."""

    a: float | None
    b: float | None
    n_pairs: int
    drives: list[str]
    maf_min_gs: float | None
    maf_max_gs: float | None
    #: Pooled reference the modelled droop is applied from (median of the
    #: measured drives' references).
    reference_psi: float | None
    residual_rms_psi: float | None
    refusal: str | None
    form: str = "quadratic_through_origin"
    per_drive_pairs: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.refusal is None

    def droop_psi(self, maf: np.ndarray) -> np.ndarray:
        if not self.ok or self.a is None or self.b is None:
            return np.full(np.shape(maf), np.nan)
        flow = np.asarray(maf, dtype=float)
        return np.asarray(self.a * flow + self.b * flow * flow, dtype=float)

    def as_dict(self) -> dict[str, Any]:
        return {
            "form": self.form,
            "a_psi_per_gs": self.a,
            "b_psi_per_gs2": self.b,
            "n_pairs": self.n_pairs,
            "drives": list(self.drives),
            "per_drive_pairs": dict(self.per_drive_pairs),
            "fitted_maf_range_gs": [self.maf_min_gs, self.maf_max_gs],
            # Zero at zero flow by construction, so it is applied from 0.
            "applied_maf_range_gs": [0.0, self.maf_max_gs],
            "reference_psi": self.reference_psi,
            "residual_rms_psi": self.residual_rms_psi,
            "refusal": self.refusal,
        }


def model_pairs(
    sessions: Mapping[str, PressureSession],
    references: Mapping[str, DriveReference],
    rules: DroopRules,
) -> pd.DataFrame:
    """``session, drive, maf_gs, psi, droop_psi`` for every engine-running MAF
    read of every session whose drive has a reference (one pair per read)."""
    frames = []
    for name, session in sessions.items():
        ref = references.get(name)
        if ref is None or ref.psi is None:
            continue
        maf, psi = _flow_pairs(session, rules)
        frames.append(
            pd.DataFrame(
                {
                    "session": name,
                    "drive": ref.session,
                    "maf_gs": maf,
                    "psi": psi,
                    "droop_psi": psi - ref.psi,
                }
            )
        )
    if not frames:
        return pd.DataFrame(columns=["session", "drive", "maf_gs", "psi", "droop_psi"])
    return pd.concat(frames, ignore_index=True)


def fit_droop_model(
    pairs: pd.DataFrame, references: Mapping[str, DriveReference], rules: DroopRules
) -> DroopModel:
    """Least squares ``droop_psi = a maf + b maf^2`` over every pair; refused
    with fewer than the declared pairs or drives."""
    spec = rules.model
    key = "drive" if "drive" in pairs.columns else "session"
    drives = sorted(set(pairs[key])) if len(pairs) else []
    per_drive = {d: int((pairs[key] == d).sum()) for d in drives}
    refs = list(
        {
            r.session: r.psi
            for r in references.values()
            if r.psi is not None and r.session in drives
        }.values()
    )
    if len(pairs) < spec.min_pairs or len(drives) < spec.min_drives:
        return DroopModel(
            None,
            None,
            int(len(pairs)),
            drives,
            None,
            None,
            None,
            None,
            f"model_refused: {len(pairs)} pairs from {len(drives)} drives with rail pressure "
            f"and a low-flow reference; the model needs {spec.min_pairs} pairs and "
            f"{spec.min_drives} drives",
            per_drive_pairs=per_drive,
        )
    maf = pairs["maf_gs"].to_numpy(dtype=float)
    droop = pairs["droop_psi"].to_numpy(dtype=float)
    design = np.column_stack([maf, maf * maf])
    (a, b), *_ = np.linalg.lstsq(design, droop, rcond=None)
    residual = droop - design @ np.array([a, b])
    return DroopModel(
        float(a),
        float(b),
        int(len(pairs)),
        drives,
        float(maf.min()),
        float(maf.max()),
        float(np.median(refs)),
        float(np.sqrt(np.mean(residual * residual))),
        None,
        per_drive_pairs=per_drive,
    )


def _decompose(
    pressure_psi: np.ndarray, reference_psi: np.ndarray, error_pct: np.ndarray
) -> dict[str, np.ndarray]:
    flow = np.sqrt(pressure_psi / reference_psi)
    droop_error = 100.0 * (1.0 / flow - 1.0)
    remainder = 100.0 * ((1.0 + error_pct / 100.0) / (1.0 + droop_error / 100.0) - 1.0)
    return {
        "droop_psi": pressure_psi - reference_psi,
        "flow_factor": flow,
        "droop_error_pct": droop_error,
        "remainder_pct": remainder,
    }


def apply_droop(
    samples: pd.DataFrame,
    sessions: Mapping[str, PressureSession],
    references: Mapping[str, DriveReference],
    model: DroopModel,
    rules: DroopRules,
) -> pd.DataFrame:
    """:data:`DROOP_COLUMNS` for each row of ``samples`` (same index).

    ``samples`` needs ``session``, ``timestamp``, ``maf_gs`` and
    ``error_pct``. Measured where the row's session has pressure, a
    reference and a full fault-free window at the row's instant; modelled
    where the model holds the row's MAF; otherwise none. ``droop_basis``
    says why the better kind was not used.
    """
    n = len(samples)
    out: dict[str, np.ndarray] = {
        "droop_kind": np.full(n, "none", dtype=object),
        "droop_basis": np.full(n, None, dtype=object),
        **{c: np.full(n, np.nan) for c in DROOP_COLUMNS[2:]},
    }
    if not n:
        return pd.DataFrame(out, index=samples.index)
    error = pd.to_numeric(samples["error_pct"], errors="coerce").to_numpy(dtype=float)
    maf = pd.to_numeric(samples["maf_gs"], errors="coerce").to_numpy(dtype=float)
    stamps = pd.to_datetime(samples["timestamp"], utc=True)
    times = np.asarray(to_seconds(stamps), dtype=float)
    names = samples["session"].astype(str).to_numpy()
    measured = np.zeros(n, dtype=bool)
    basis = out["droop_basis"]
    for name in np.unique(names):
        rows = np.flatnonzero(names == name)
        session = sessions.get(name)
        if session is None or not session.captured:
            basis[rows] = f"no_pressure_logged ({rules.pressure_channel})"
            continue
        ref = references.get(name)
        if ref is None or ref.psi is None:
            basis[rows] = (ref.reason if ref is not None else None) or "no_low_flow_reference"
            continue
        mean, count, fault = window_pressure(session, times[rows], rules)
        ok = np.isfinite(mean) & ~fault
        for i, row in enumerate(rows):
            if fault[i]:
                basis[row] = f"pressure_fault_in_window ({rules.fault_channel})"
            elif not ok[i]:
                basis[row] = (
                    f"pressure_window_short: {int(count[i])} reads, need {rules.min_window_reads}"
                )
        take = rows[ok]
        measured[take] = True
        out["pressure_psi"][take] = mean[ok] / rules.kpa_per_psi
        out["reference_psi"][take] = ref.psi
    out["droop_kind"][measured] = "measured"
    basis[measured] = "measured"
    rest = ~measured
    if model.ok and model.maf_max_gs is not None and model.reference_psi is not None:
        inside = rest & np.isfinite(maf) & (maf >= 0.0) & (maf <= model.maf_max_gs)
        outside = rest & np.isfinite(maf) & ~inside
        unpaired = rest & ~np.isfinite(maf)
        out["droop_kind"][inside] = "modelled"
        out["pressure_psi"][inside] = model.reference_psi + model.droop_psi(maf[inside])
        out["reference_psi"][inside] = model.reference_psi
        for row in np.flatnonzero(inside):
            basis[row] = f"modelled ({basis[row]})"
        for row in np.flatnonzero(outside):
            basis[row] = (
                f"maf_beyond_fit_range: {maf[row]:.1f} g/s outside 0..{model.maf_max_gs:.1f} "
                f"({basis[row]})"
            )
        for row in np.flatnonzero(unpaired):
            basis[row] = f"maf_unpaired ({basis[row]})"
    else:
        for row in np.flatnonzero(rest):
            basis[row] = f"{model.refusal} ({basis[row]})"
    placed = out["droop_kind"] != "none"
    parts = _decompose(out["pressure_psi"][placed], out["reference_psi"][placed], error[placed])
    for key, values in parts.items():
        out[key][placed] = values
    return pd.DataFrame(out, index=samples.index)


@dataclass(frozen=True)
class DroopEvidence:
    """Everything droop was computed from, for the report."""

    references: dict[str, DriveReference]
    model: DroopModel
    pressure_sessions: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "droop_version": DROOP_VERSION,
            "references": {k: v.as_dict() for k, v in self.references.items()},
            "model": self.model.as_dict(),
            "pressure_sessions": list(self.pressure_sessions),
        }


def droop_evidence(
    sessions: Mapping[str, PressureSession],
    rules: DroopRules,
    drives: Mapping[str, str] | None = None,
) -> DroopEvidence:
    """One reference per drive (``drives`` maps session -> drive, the fit's
    declared drive unit; each session alone when omitted), every session
    keyed to its drive's reference, and the model fitted on them."""
    with_pressure = {name: s for name, s in sessions.items() if s.captured}
    drive_of = {name: (drives or {}).get(name, name) for name in with_pressure}
    by_drive: dict[str, list[PressureSession]] = {}
    for name, session in with_pressure.items():
        by_drive.setdefault(drive_of[name], []).append(session)
    pooled = {d: drive_reference(parts, rules, drive=d) for d, parts in by_drive.items()}
    references = {name: pooled[drive_of[name]] for name in with_pressure}
    pairs = model_pairs(with_pressure, references, rules)
    return DroopEvidence(
        references=references,
        model=fit_droop_model(pairs, references, rules),
        pressure_sessions=sorted(with_pressure),
    )


def droop_known_answer(
    sessions: Mapping[str, PressureSession],
    evidence: DroopEvidence,
    rules: DroopRules,
) -> dict[str, Any]:
    """The recorded droop at the declared MAF band, through the measured path.

    Every engine-running MAF read of the declared sessions inside the band,
    with a full fault-free window, as droop from its own drive's reference;
    passes when the median is within the declared tolerance of the recorded
    figure. Also reports the fitted model at the band's median MAF. An empty
    answer says which kind: a session never on disk or without pressure, no
    reference, or no reads in the band.
    """
    rule = rules.known_answer
    lo, hi = rule.maf_band_gs
    per_session: dict[str, Any] = {}
    droops: list[np.ndarray] = []
    flows: list[np.ndarray] = []
    for name in rule.sessions:
        session = sessions.get(name)
        if session is None:
            per_session[name] = "never_captured: session not read (absent from the corpus)"
            continue
        if not session.captured:
            per_session[name] = f"never_captured: no {rules.pressure_channel}"
            continue
        ref = evidence.references.get(name) or drive_reference(session, rules)
        if ref.psi is None:
            per_session[name] = ref.reason
            continue
        maf, psi = _flow_pairs(session, rules)
        band = (maf >= lo) & (maf < hi)
        per_session[name] = {
            "reference_psi": ref.psi,
            "reference_n": ref.n,
            "pairs_in_band": int(band.sum()),
        }
        droops.append(psi[band] - ref.psi)
        flows.append(maf[band])
    droop = np.concatenate(droops) if droops else _EMPTY
    flow = np.concatenate(flows) if flows else _EMPTY
    result: dict[str, Any] = {
        "sessions": per_session,
        "maf_band_gs": [lo, hi],
        "expected_psi": rule.expected_psi,
        "tolerance_psi": rule.tolerance_psi,
        "source": rule.source,
        "n": int(len(droop)),
    }
    if len(droop) < rule.min_pairs:
        result["passed"] = False
        result["reason"] = (
            f"filtered: {len(droop)} reads in {lo:g}-{hi:g} g/s, need {rule.min_pairs}"
            if any(isinstance(v, dict) for v in per_session.values())
            else "unresolvable: no declared session has rail pressure and a reference"
        )
        return result
    median = float(np.median(droop))
    result.update(
        median_droop_psi=median,
        median_maf_gs=float(np.median(flow)),
        p10_p90_droop_psi=[float(np.quantile(droop, 0.1)), float(np.quantile(droop, 0.9))],
        flow_change_pct=None,
        passed=abs(median - rule.expected_psi) <= rule.tolerance_psi,
    )
    references = [v["reference_psi"] for v in per_session.values() if isinstance(v, dict)]
    ref_psi = float(np.median(references))
    result["flow_change_pct"] = 100.0 * (np.sqrt((ref_psi + median) / ref_psi) - 1.0)
    if evidence.model.ok:
        result["model_at_median_maf_psi"] = float(
            evidence.model.droop_psi(np.array([result["median_maf_gs"]]))[0]
        )
    return result


def logged_pressure(session_path: Path, rules: DroopRules) -> bool:
    """Whether the session stored any rail-pressure row: a one-column read
    per file, filtered on the channel, so the corpus can be scanned without
    reading every session in full. A file the scan cannot read counts as
    logged, so the full reader decides rather than a silent skip."""
    spec = parse_spec(rules.pressure_channel)
    files = [str(path) for path in sorted(session_path.glob(PARQUET_PATTERN))]
    if not files:
        return False
    try:
        table = pq.read_table(  # type: ignore[no-untyped-call, unused-ignore]
            files,
            columns=["channel"],
            filters=[("source", "==", spec.source), ("channel", "==", spec.name)],
        )
    except (OSError, ValueError, TypeError, KeyError):
        return True
    return bool(table.num_rows)


def pressure_sessions(
    session_paths: Sequence[Path],
    rules: DroopRules,
    *,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
) -> dict[str, PressureSession]:
    """:func:`read_pressure_session` for each path that logged rail pressure
    (:func:`logged_pressure`), keyed by session name."""
    return {
        path.name: read_pressure_session(
            path, rules, name=path.name, bindings_path=bindings_path, dids_path=dids_path
        )
        for path in session_paths
        if logged_pressure(path, rules)
    }


__all__ = [
    "DROOP_COLUMNS",
    "DROOP_KINDS",
    "DROOP_VERSION",
    "DriveReference",
    "DroopEvidence",
    "DroopModel",
    "PressureSession",
    "apply_droop",
    "drive_reference",
    "droop_evidence",
    "droop_known_answer",
    "fit_droop_model",
    "logged_pressure",
    "model_pairs",
    "pressure_sessions",
    "read_pressure_session",
    "window_pressure",
]
