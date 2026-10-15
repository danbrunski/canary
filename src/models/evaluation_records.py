"""One session's clean scores and one drive's record of them (A02.4).

The handoff types between scoring and every summary: :class:`SessionScore` is
one detector's clean scoring of one session, :class:`DriveRecord` is what a
drive contributes to the report (its rows, engine hours, atmosphere and each
detector's episode counts), and :func:`drop_in_sample` removes the detectors a
drive is in-sample for from everything a summary would read. Extracted from
``scripts/evaluate_detectors.py`` (R15.3); that script re-exports these names.

The label-dependent half of building a record, which needs the labelled
events (:func:`~src.analysis.detector_evaluation_drives.clean_drive_record`),
lives in :mod:`src.analysis.detector_evaluation_drives`: this module imports
no workbench concept.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.models.evaluation import (
    AMBIENT_BANDS_C,
    BARO_BANDS_KPA,
    Drive,
    PersistenceSpec,
    band_of,
    sustained_alerts,
)
from src.models.evaluation_detectors import DetectorOutput


@dataclass
class SessionScore:
    """One detector's clean scoring of one session."""

    timestamps: pd.Series
    verdict: np.ndarray[Any, np.dtype[np.object_]]
    score: np.ndarray[Any, np.dtype[np.float64]]
    alerting: np.ndarray[Any, np.dtype[np.bool_]]


def session_scores(
    outputs: Mapping[str, DetectorOutput],
    timestamps: pd.Series,
    persistence: PersistenceSpec,
) -> dict[str, SessionScore]:
    """Every detector's output on one unit's rows, through the persistence gate."""
    out: dict[str, SessionScore] = {}
    for name, output in outputs.items():
        alerting = sustained_alerts(
            timestamps, list(output.verdict), persistence, pre_gated=output.pre_gated
        )
        out[name] = SessionScore(timestamps, output.verdict, output.score, alerting)
    return out


@dataclass
class DriveRecord:
    drive: Drive
    sessions: list[str]
    rows: int = 0
    hours: float = 0.0
    baro_kpa: float | None = None
    ambient_c: float | None = None
    labels: list[str] = field(default_factory=list)
    detectors: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Recorded in a tune/hardware era none of the scored artifacts trained
    #: in: reported per drive, kept out of every summary (no fold mixes eras).
    cross_era: bool = False
    #: Sessions of the drive kept out of every metric, and why (R04.6: tuning
    #: captures bridge the drive but are not the default profile).
    excluded_sessions: dict[str, str] = field(default_factory=dict)
    #: A02.3: detectors whose results on this drive are in-sample (the drive
    #: chose the sequence artifact's alert level), and why. They are left out
    #: of ``detectors``, the injections and every summary for this drive; the
    #: other detectors are scored here as usual.
    in_sample: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "drive": self.drive.drive_id,
            "in_sample": dict(sorted(self.in_sample.items())),
            "cross_era": self.cross_era,
            "sessions": self.sessions,
            # Sessions of this drive that could not be loaded: the drive was
            # scored on the rest, and says so.
            "missing_sessions": sorted(
                set(self.drive.session_names) - set(self.sessions) - set(self.excluded_sessions)
            ),
            "excluded_sessions": dict(sorted(self.excluded_sessions.items())),
            "start": self.drive.start.isoformat(),
            "end": self.drive.end.isoformat(),
            "era": self.drive.era.label if self.drive.era else None,
            "rows": self.rows,
            "engine_hours": self.hours,
            "baro_kpa": self.baro_kpa,
            "baro_band": band_of(self.baro_kpa, BARO_BANDS_KPA),
            "ambient_c": self.ambient_c,
            "ambient_band": band_of(self.ambient_c, AMBIENT_BANDS_C),
            "labels": self.labels,
            "detectors": self.detectors,
        }


def drop_in_sample(
    in_sample: Mapping[str, str],
    *,
    sessions: Sequence[str],
    scores: dict[str, dict[str, SessionScore]],
    record: DriveRecord,
    outcomes: list[dict[str, Any]],
    slices: list[dict[str, Any]],
    shift: dict[str, Any] | None,
) -> None:
    """Remove the in-sample detectors' results on one drive, everywhere a
    summary would read them (clean scores, the drive record, injections,
    VUS slices, the known-answer shift), and record why on the drive."""
    if not in_sample:
        return
    for session in sessions:
        for name in in_sample:
            scores.get(session, {}).pop(name, None)
    for name in in_sample:
        record.detectors.pop(name, None)
    record.in_sample = dict(in_sample)
    outcomes[:] = [o for o in outcomes if o.get("detector") not in in_sample]
    slices[:] = [s for s in slices if s.get("detector") not in in_sample]
    if shift is not None:
        for name in in_sample:
            (shift.get("detectors") or {}).pop(name, None)
