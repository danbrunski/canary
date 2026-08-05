"""The tuning half of a post-drive digest.

"Drive's over" should produce a report, not an hour of ad-hoc SQL. This
assembles what the operator actually wants to know after a tuning capture:
how much usable WOT it caught, whether the capture ran at the rate it was
armed for, what the delivery error was, and how the cam tracked its command.

Deliberately separate from :mod:`src.analysis.digest`: this section only
exists for capture sessions, needs an AFR calibration and a tune-revision
registry the ordinary digest has no use for, and must degrade to ``None``
rather than failing a normal drive's digest when a channel is missing.

See ``docs/roadmap/tuning-analysis-toolkit.md`` Phase 5.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.afr import load_afr_calibration
from src.analysis.channels import read_aligned, read_channels
from src.analysis.noise_floor import load_noise_floor
from src.analysis.tune_revisions import load_tune_revisions, session_started_at
from src.analysis.tuning import DEFAULT_RPM_BANDS, GateError, delivery_error
from src.config import CanaryConfig

#: Load floor for the WOT inventory. The OBD throttle percentage tops out
#: around 89 % and understates true WOT, so load is the better gate.
WOT_LOAD_FLOOR = 85.0
WOT_RPM_FLOOR = 2500.0


@dataclass
class TuningDigest:
    """Tuning section of a capture session's digest."""

    session_id: str
    capture_profile: str | None
    tune_revision: str | None
    wot_seconds: int
    wot_with_afr_pair: int
    wot_by_band: list[dict[str, Any]]
    achieved_hz: dict[str, float]
    delivery: dict[str, Any] | None
    cam: dict[str, Any] | None
    #: Delta against the previous capture sharing this profile *and* tune
    #: revision — the "did the flash do what I meant" answer. Requires both
    #: to match: across a flash boundary the delta is the flash, not drift.
    previous: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "capture_profile": self.capture_profile,
            "tune_revision": self.tune_revision,
            "wot_seconds": self.wot_seconds,
            "wot_with_afr_pair": self.wot_with_afr_pair,
            "wot_by_band": list(self.wot_by_band),
            "achieved_hz": dict(self.achieved_hz),
            "delivery": self.delivery,
            "cam": self.cam,
            "previous": self.previous,
            "notes": list(self.notes),
        }


def _capture_profile_name(session_dir: Path) -> str | None:
    """Profile recorded in ``control.json`` when the capture was armed."""
    control = session_dir / "control.json"
    if not control.exists():
        return None
    try:
        payload = json.loads(control.read_text())
    except (OSError, ValueError):
        return None
    for key in ("capture_profile", "profile"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("name"), str):
            return str(value["name"])
    return None


def _achieved_hz(session_dir: Path, specs: list[str]) -> dict[str, float]:
    """Real per-channel rate over the session, from the rows themselves.

    Computed from stored data rather than read off `/status`, so it still
    works long after the drive — and so the number in the digest is what was
    actually written, not what the scheduler believed it was doing.
    """
    out: dict[str, float] = {}
    for spec in specs:
        try:
            frame = read_channels(session_dir, [spec])
        except Exception:  # noqa: BLE001 — a missing channel is not a digest failure
            continue
        if frame.empty:
            continue
        span = (frame["timestamp"].max() - frame["timestamp"].min()).total_seconds()
        if span > 0:
            out[spec] = round(len(frame) / span, 2)
    return out


def _wot_inventory(session_dir: Path) -> tuple[int, int, list[dict[str, Any]]]:
    """Seconds at WOT by rpm band, and how many carry a usable AFR pair.

    The pair count is the number that matters: WOT seconds without both a
    measured and a commanded AFR in the same second cannot answer anything,
    and the July corpus had 25 such seconds against 4 usable ones.
    """
    try:
        frame = read_aligned(
            session_dir,
            ["obd:RPM", "obd:ENGINE_LOAD"],
            grid="1s",
            require_all=True,
        )
    except Exception:  # noqa: BLE001
        return 0, 0, []
    if frame.empty:
        return 0, 0, []
    wot = frame[(frame["obd:RPM"] > WOT_RPM_FLOOR) & (frame["obd:ENGINE_LOAD"] > WOT_LOAD_FLOOR)]
    if wot.empty:
        return 0, 0, []

    bands = (
        wot.groupby(pd.cut(wot["obd:RPM"], list(DEFAULT_RPM_BANDS)), observed=True)
        .size()
        .reset_index(name="seconds")
    )
    rows: list[dict[str, Any]] = [
        {"rpm_band": str(band), "seconds": int(seconds)}
        for band, seconds in zip(
            bands.iloc[:, 0].astype(str), bands["seconds"].astype(int), strict=True
        )
    ]

    paired = 0
    try:
        with_afr = read_aligned(
            session_dir,
            ["obd:RPM", "obd:ENGINE_LOAD", "obd:O2_WR_LAMBDA_S1", "obd:COMMANDED_EQUIV_RATIO"],
            grid="1s",
            require_all=True,
        )
        paired = int(
            (
                (with_afr["obd:RPM"] > WOT_RPM_FLOOR)
                & (with_afr["obd:ENGINE_LOAD"] > WOT_LOAD_FLOOR)
            ).sum()
        )
    except Exception:  # noqa: BLE001
        paired = 0
    return int(len(wot)), paired, rows


def _cam_tracking(session_dir: Path) -> dict[str, Any] | None:
    """How the measured cam angle followed its command.

    Only meaningful on an all-DID capture — cam angle exists as a DID and
    nowhere else, so a Mode 01 capture legitimately has nothing here.
    """
    try:
        frame = read_aligned(
            session_dir,
            ["did:ECM_INTAKE_CAM_ANGLE_COMMANDED", "did:ECM_INTAKE_CAM_ANGLE_MEASURED"],
            grid="1s",
            require_all=True,
        )
    except Exception:  # noqa: BLE001
        return None
    if len(frame) < 3:
        return None
    commanded = frame["did:ECM_INTAKE_CAM_ANGLE_COMMANDED"].astype(float)
    measured = frame["did:ECM_INTAKE_CAM_ANGLE_MEASURED"].astype(float)
    error = measured - commanded
    return {
        "n": int(len(frame)),
        "commanded_range_deg": [round(float(commanded.min()), 1), round(float(commanded.max()), 1)],
        "measured_range_deg": [round(float(measured.min()), 1), round(float(measured.max()), 1)],
        "mean_tracking_error_deg": round(float(error.mean()), 2),
        "max_tracking_error_deg": round(float(error.abs().max()), 2),
        "correlation": round(float(np.corrcoef(commanded, measured)[0, 1]), 4)
        if commanded.std() > 0 and measured.std() > 0
        else None,
    }


def _previous_capture(
    session_dir: Path, *, profile: str | None, revision: str | None, revisions: Any
) -> Path | None:
    """The newest earlier capture sharing this profile and tune revision.

    Both must match. Comparing across a flash boundary answers a different
    question — the delta is then whatever the flash changed, which is
    interesting but is not "is this drive like the last one".
    """
    if revision is None:
        return None
    started = session_started_at(session_dir)
    if started is None:
        return None
    root = session_dir.parent

    # Order by the PARSED start time, never by directory name. Capture dirs
    # carry different prefixes (`_capture-fuel-map-...` vs `_tuning-...`) and
    # string order sorts by prefix first, so a `_capture-` session a month
    # later compares as "earlier" than a `_tuning-` one.
    dated: list[tuple[datetime, Path]] = []
    for path in list(root.glob("_capture-*")) + list(root.glob("_tuning-*")):
        if not path.is_dir():
            continue
        when = session_started_at(path)
        if when is not None and when < started:
            dated.append((when, path))

    for _when, candidate in sorted(dated, key=lambda item: item[0], reverse=True):
        if profile is not None and _capture_profile_name(candidate) != profile:
            continue
        found = revisions.for_session(candidate) if revisions is not None else None
        if found is not None and found.revision == revision:
            return candidate
    return None


def compute_tuning_digest(
    session_dir: Path,
    config: CanaryConfig,
) -> TuningDigest | None:
    """Tuning section for a capture session, or ``None`` for an ordinary drive.

    Returns ``None`` rather than raising whenever the inputs are not a tuning
    capture — a normal drive's digest must not fail because this section had
    nothing to say.
    """
    profile = _capture_profile_name(session_dir)
    is_capture = profile is not None or session_dir.name.startswith("_")
    if not is_capture:
        return None

    notes: list[str] = []
    profile_data = config.daemon.afr_calibration.parent
    revisions = load_tune_revisions(profile_data / "tune_revisions.yaml")
    revision = None
    if revisions is not None:
        found = revisions.for_session(session_dir)
        revision = None if found is None else found.revision

    wot_seconds, paired, bands = _wot_inventory(session_dir)
    if wot_seconds and not paired:
        notes.append(
            "WOT seconds were captured but none carry both a measured and a commanded AFR "
            "in the same second, so delivery error cannot be computed from this drive."
        )

    achieved = _achieved_hz(
        session_dir,
        [
            "obd:RPM",
            "obd:O2_WR_LAMBDA_S1",
            "obd:TIMING_ADVANCE",
            "did:ECM_AF_SENSOR_V_B1",
            "did:ECM_INTAKE_CAM_ANGLE_MEASURED",
        ],
    )

    delivery: dict[str, Any] | None = None
    calibration_path = config.daemon.afr_calibration
    if calibration_path.exists():
        try:
            result = delivery_error(
                session_dir,
                calibration=load_afr_calibration(calibration_path),
                revisions=revisions,
            )
            delivery = result.as_dict() if result.n else None
            if result.n == 0:
                notes.append(
                    "no samples survived the delivery-error gates: "
                    + ", ".join(f"{k} -{v}" for k, v in result.gates["filtered"].items())
                )
        except (GateError, ValueError) as exc:
            notes.append(f"delivery error unavailable: {exc}")

    floor = load_noise_floor(profile_data / "noise_floor.yaml")
    if floor is None:
        notes.append(
            "no measured noise floor for this vehicle, so deltas in any comparison against "
            "this drive carry no significance verdict; record one from a same-tune pair."
        )

    previous: dict[str, Any] | None = None
    earlier = _previous_capture(
        session_dir, profile=profile, revision=revision, revisions=revisions
    )
    if earlier is not None:
        previous = {"session_id": earlier.name}
        if delivery is not None and calibration_path.exists():
            try:
                before = delivery_error(
                    earlier,
                    calibration=load_afr_calibration(calibration_path),
                    revisions=revisions,
                )
            except (GateError, ValueError):
                before = None
            if before is not None and before.n:
                previous["delivery_band_averaged_pct"] = before.band_averaged_pct
                previous["delta_pct"] = delivery["band_averaged_pct"] - before.band_averaged_pct
                sd = floor.sd_for("delivery_error_pct") if floor is not None else None
                if sd:
                    sigma = previous["delta_pct"] / sd
                    previous["delta_sigma"] = round(sigma, 2)
                    previous["distinguishable"] = abs(sigma) >= 2.0
                else:
                    previous["delta_sigma"] = None
                    previous["distinguishable"] = None

    return TuningDigest(
        session_id=session_dir.name,
        capture_profile=profile,
        tune_revision=revision,
        wot_seconds=wot_seconds,
        wot_with_afr_pair=paired,
        wot_by_band=bands,
        achieved_hz=achieved,
        delivery=delivery,
        cam=_cam_tracking(session_dir),
        previous=previous,
        notes=notes,
    )


__all__ = ["TuningDigest", "compute_tuning_digest"]
