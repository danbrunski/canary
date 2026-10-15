"""Per-drive reading of a detector evaluation against the labelled events (A02.4).

The half of the evaluation that needs the workbench's labelled events, so it
lives in the analysis layer: :func:`clean_drive_record` turns one drive's clean
scores into a :class:`~src.models.evaluation_records.DriveRecord` (every
episode is a false alarm unless it starts inside a ``fault`` label), and
:func:`summarize_events` reports how each detector behaved inside each labelled
interval. ``fault`` events would count toward recall; ``novelty`` and
``intervention`` responses are reported, never counted as catches. Extracted
from ``scripts/evaluate_detectors.py`` (R15.4); that script re-exports these
names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.labelled_events import LabelledEvent
from src.columns import TIMESTAMP_COLUMN
from src.models.evaluation import (
    AMBIENT_CHANNEL,
    BARO_CHANNEL,
    Drive,
    alert_episodes,
    engine_hours,
    engine_running,
    observed_median,
)
from src.models.evaluation_records import DriveRecord, SessionScore


def _interval_mask(timestamps: pd.Series, event: LabelledEvent) -> np.ndarray[Any, Any]:
    start = pd.Timestamp(event.start)
    end = pd.Timestamp(event.end)
    return ((timestamps >= start) & (timestamps <= end)).to_numpy()


def clean_drive_record(
    drive: Drive,
    session_frames: Mapping[str, pd.DataFrame],
    scores: Mapping[str, Mapping[str, SessionScore]],
    names: Sequence[str],
    labels: Sequence[LabelledEvent],
) -> DriveRecord:
    """Per-drive clean metrics for every detector.

    Every episode is a false alarm unless it starts inside a ``fault`` label —
    a catch. Episodes inside ``novelty`` or ``intervention`` labels are still
    false alarms for a fault detector (an altitude trip is not a fault), and
    are broken out by kind so the report shows how many of them the labels
    explain. An earlier version exempted every labelled episode; the 81-day
    LTFT-cleared label then hid whole drives (133 robust-z episodes on one).
    """
    record = DriveRecord(drive, sorted(session_frames))
    frames = [session_frames[name] for name in record.sessions]
    joined = pd.concat(frames, ignore_index=True)
    record.rows = len(joined)
    record.hours = sum(
        engine_hours(pd.to_datetime(f[TIMESTAMP_COLUMN], utc=True), engine_running(f))
        for f in frames
    )
    record.baro_kpa = observed_median(joined, BARO_CHANNEL)
    record.ambient_c = observed_median(joined, AMBIENT_CHANNEL)
    for name in names:
        rows = scored = flagged = episodes = 0
        by_kind: dict[str, int] = {}
        for session in record.sessions:
            result = scores[session].get(name)
            if result is None:
                continue
            verdicts = list(result.verdict)
            rows += len(verdicts)
            scored += sum(1 for v in verdicts if v is not None)
            flagged += sum(1 for v in verdicts if v is True or v is np.True_)
            found = alert_episodes(result.timestamps, result.alerting, verdicts=verdicts)
            episodes += len(found)
            for episode in found:
                for event in labels:
                    if pd.Timestamp(event.start) <= episode.start <= pd.Timestamp(event.end):
                        by_kind[event.kind] = by_kind.get(event.kind, 0) + 1
                        break
        false_alarms = episodes - by_kind.get("fault", 0)
        record.detectors[name] = {
            "rows": rows,
            "scored_rows": scored,
            "withheld_rows": rows - scored,
            "flagged_rows": flagged,
            "episodes": episodes,
            "labelled_episodes": by_kind,
            "false_alarm_episodes": false_alarms,
            "false_alarms_per_engine_hour": (
                None if record.hours <= 0 else false_alarms / record.hours
            ),
        }
    record.labels = sorted(
        event.id
        for event in labels
        if pd.Timestamp(event.start) <= pd.Timestamp(drive.end)
        and pd.Timestamp(event.end) >= pd.Timestamp(drive.start)
    )
    return record


def summarize_events(
    events: Sequence[LabelledEvent],
    scores: Mapping[str, Mapping[str, SessionScore]],
    names: Sequence[str],
) -> list[dict[str, Any]]:
    """How each detector behaved inside each labelled interval.

    ``fault`` events would count toward recall; ``novelty`` and
    ``intervention`` responses are reported, never counted as catches.
    """
    out: list[dict[str, Any]] = []
    for event in events:
        entry: dict[str, Any] = {
            "id": event.id,
            "kind": event.kind,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "detectors": {},
        }
        covered_sessions = []
        for session, by_detector in scores.items():
            for name in names:
                result = by_detector.get(name)
                if result is None:
                    continue
                inside = _interval_mask(result.timestamps, event)
                if not inside.any():
                    continue
                if session not in covered_sessions:
                    covered_sessions.append(session)
                block = entry["detectors"].setdefault(
                    name, {"rows": 0, "scored_rows": 0, "flagged_rows": 0, "episodes_started": 0}
                )
                verdicts = [result.verdict[i] for i in np.flatnonzero(inside)]
                block["rows"] += int(inside.sum())
                block["scored_rows"] += sum(1 for v in verdicts if v is not None)
                block["flagged_rows"] += sum(1 for v in verdicts if v is True or v is np.True_)
                block["episodes_started"] += sum(
                    1
                    for episode in alert_episodes(
                        result.timestamps, result.alerting, verdicts=list(result.verdict)
                    )
                    if pd.Timestamp(event.start) <= episode.start <= pd.Timestamp(event.end)
                )
        entry["evaluated_sessions"] = sorted(covered_sessions)
        if not covered_sessions:
            entry["status"] = "skipped: no evaluated drive overlaps this interval"
        out.append(entry)
    return out
