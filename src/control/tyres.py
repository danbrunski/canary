"""Tyre wear from GPS: the trip digests' rolling circumference, trended.

Every drive's ``rolling_circumference.json`` holds the driven tyres' rolling
circumference over its steady cruise (:mod:`src.analysis.rolling_circumference`;
written by the trip-end digest, by trip end alone when the digest is refused,
and by ``scripts/backfill_rolling_circumference.py``). One
millimetre of tread is 2π mm of circumference, so a change in circumference
is a change in tread depth:

    Δdepth = ΔC / 2π

That is a **trend**, not an absolute depth. A tyre model predicts the
circumference to ~±1 % (±4 mm of tread), far worse than the wear it would
track. So absolute depth needs one gauge reading, recorded on the vehicle
ledger as a ``tyres: {action: tread_measured, tread_depth_mm}`` event: the
drives around it fix the circumference that depth corresponds to, and later
drives move it.

What this reads, and nothing else: those sidecars (small JSON) and the
ledger. Safe to serve live on the Pi.

Rules:
- A ``rotated`` or ``replaced`` event starts a new series. The driven axle
  then carries different tyres, so drives either side never trend together,
  and an earlier gauge reading stops applying.
- A ``pressure_changed`` event since the baseline is named in ``caveats``.
  About 6 psi moves the circumference as much as ~0.8 mm of tread.
- Drives measured by a different method version are never combined.
- Uncertainty comes from the drives themselves (their scatter / √n). The
  Xterra's drives on the Pi scattered by 0.43 % (≈1.7 mm of tread) per drive
  (2026-10-04, 9 drives; one +1 % outlier).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from src.analysis.rolling_circumference import METHOD_VERSION
from src.storage.session_products import read_rolling_circumference
from src.storage.sessions import is_session_dir_name, session_started_at
from src.vehicle.vehicle_ledger import VehicleEvent, VehicleLedgerStore

#: Drives the "current" circumference is the median of.
CURRENT_DRIVES = 10
#: A gauge reading is tied to the drives within this many days of it.
BASELINE_WINDOW_DAYS = 21
#: Fewest drives that fix a baseline circumference, and the most used (the
#: ones nearest the reading).
MIN_BASELINE_DRIVES = 3
BASELINE_DRIVES = 5
#: Per-drive scatter used when too few drives exist to measure it (2026-09).
DEFAULT_DRIVE_SCATTER_PCT = 0.15


def tyre_status(
    storage_path: Path,
    ledger: VehicleLedgerStore,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The tyre-wear readout: drives, current circumference, estimated depth."""
    events = [e for e in ledger.records() if e.tyres is not None]
    reset = _latest(events, ("rotated", "replaced"))
    series_start = reset.occurred_at if reset is not None else None
    drives = [d for d in _drives(storage_path) if series_start is None or d["date"] >= series_start]
    out: dict[str, Any] = {
        "method_version": METHOD_VERSION,
        "series_start": series_start.isoformat() if series_start else None,
        "series_reset_by": _event_ref(reset),
        "drives": [_drive_json(d) for d in drives],
        "current": None,
        "baseline": None,
        "estimate": None,
        "caveats": [],
    }
    if not drives:
        out["caveats"].append("no drive since the series start has a steady-cruise measurement")
        return out
    scatter_pct = _scatter_pct(drives)
    recent = drives[-CURRENT_DRIVES:]
    current_m = float(np.median([d["median_m"] for d in recent]))
    current_se_m = current_m * scatter_pct / 100.0 / math.sqrt(len(recent))
    out["current"] = {
        "circumference_m": round(current_m, 5),
        "uncertainty_m": round(current_se_m, 5),
        "drives": len(recent),
        "drive_scatter_pct": round(scatter_pct, 3),
        "through": recent[-1]["date"].isoformat(),
    }
    measured = _latest(
        [e for e in events if series_start is None or e.occurred_at >= series_start],
        ("tread_measured",),
    )
    if measured is None:
        out["caveats"].append(
            "no gauge reading since the series start: record one on the vehicle ledger "
            "(tyres: tread_measured) to turn the circumference into tread depth"
        )
        return out
    window = timedelta(days=BASELINE_WINDOW_DAYS)
    # The drives nearest the reading, not every drive in the window: a window
    # wide enough to find drives would otherwise also take in the wear since.
    near = sorted(
        (d for d in drives if abs(d["date"] - measured.occurred_at) <= window),
        key=lambda d: abs(d["started_at"].date() - measured.occurred_at),
    )[:BASELINE_DRIVES]
    assert measured.tyres is not None and measured.tyres.tread_depth_mm is not None
    baseline: dict[str, Any] = {
        "tread_depth_mm": measured.tyres.tread_depth_mm,
        "measured_on": measured.occurred_at.isoformat(),
        "event": _event_ref(measured),
        "drives": len(near),
    }
    out["baseline"] = baseline
    if len(near) < MIN_BASELINE_DRIVES:
        baseline["status"] = "pending"
        out["caveats"].append(
            f"the gauge reading has {len(near)} measured drive(s) within "
            f"{BASELINE_WINDOW_DAYS} days; {MIN_BASELINE_DRIVES} fix its circumference"
        )
        return out
    base_m = float(np.median([d["median_m"] for d in near]))
    base_se_m = base_m * scatter_pct / 100.0 / math.sqrt(len(near))
    baseline.update({"status": "fixed", "circumference_m": round(base_m, 5)})
    worn_mm = (base_m - current_m) / (2.0 * math.pi) * 1000.0
    worn_se_mm = math.hypot(base_se_m, current_se_m) / (2.0 * math.pi) * 1000.0
    out["estimate"] = {
        "tread_depth_mm": round(measured.tyres.tread_depth_mm - worn_mm, 2),
        "worn_since_baseline_mm": round(worn_mm, 2),
        "uncertainty_mm": round(worn_se_mm, 2),
        "as_of": (now or datetime.now(UTC)).date().isoformat(),
    }
    pressure = [
        e
        for e in events
        if e.tyres is not None
        and e.tyres.action == "pressure_changed"
        and e.occurred_at > measured.occurred_at
    ]
    if pressure:
        out["caveats"].append(
            f"tyre pressure changed {len(pressure)} time(s) since the gauge reading: "
            "~6 psi moves the circumference as much as ~0.8 mm of tread"
        )
    return out


def _drives(storage_path: Path) -> list[dict[str, Any]]:
    """Every session's tyre measurement, oldest first."""
    found: list[dict[str, Any]] = []
    if not storage_path.is_dir():
        return found
    for session_dir in storage_path.iterdir():
        if not session_dir.is_dir() or not is_session_dir_name(session_dir.name):
            continue
        block = read_rolling_circumference(session_dir)
        if not isinstance(block, dict) or not block.get("available"):
            continue
        if block.get("method_version") != METHOD_VERSION:
            continue
        started = session_started_at(session_dir.name)
        if started is None or not isinstance(block.get("median_m"), (int, float)):
            continue
        found.append(
            {
                "session": session_dir.name,
                "started_at": started,
                "date": started.date(),
                "median_m": float(block["median_m"]),
                "n_pairs": int(block.get("n_pairs") or 0),
                "se_pct": block.get("se_pct"),
            }
        )
    return sorted(found, key=lambda d: d["started_at"])


def _scatter_pct(drives: list[dict[str, Any]]) -> float:
    """Drive-to-drive σ of the medians, % (the measured floor when too few)."""
    values = np.asarray([d["median_m"] for d in drives], dtype=float)
    if len(values) < 5:
        return DEFAULT_DRIVE_SCATTER_PCT
    return max(float(values.std(ddof=1) / np.median(values) * 100.0), 0.01)


def _latest(events: list[VehicleEvent], actions: tuple[str, ...]) -> VehicleEvent | None:
    chosen = [e for e in events if e.tyres is not None and e.tyres.action in actions]
    return max(chosen, key=lambda e: (e.occurred_at, e.recorded_at)) if chosen else None


def _event_ref(event: VehicleEvent | None) -> dict[str, Any] | None:
    if event is None or event.tyres is None:
        return None
    return {
        "id": event.id,
        "occurred_at": event.occurred_at.isoformat(),
        "title": event.title,
        "action": event.tyres.action,
    }


def _drive_json(drive: dict[str, Any]) -> dict[str, Any]:
    return {
        "session": drive["session"],
        "started_at": drive["started_at"].isoformat(),
        "circumference_m": drive["median_m"],
        "n_pairs": drive["n_pairs"],
        "se_pct": drive["se_pct"],
    }


__all__ = ["tyre_status"]
