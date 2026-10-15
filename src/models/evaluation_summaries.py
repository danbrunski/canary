"""Statistical summaries of a detector evaluation (A02.4).

Per-detector false alarms per engine-hour, recall against severity, threshold-
free VUS-PR, the single-quantity shift verdicts, the alert-budget threshold
and the compact injection list. Each reads the per-drive records and the
injection outcomes the scorer produced and returns the JSON the report
carries; none loads a session or scores a row. Independent drives are the
resampling unit of every interval, and a ``withheld`` or ``masked`` outcome is
never in a recall denominator. Extracted from ``scripts/evaluate_detectors.py``
(R15.4); that script re-exports these names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from src.models.evaluation import (
    PersistenceSpec,
    ScoreTrace,
    alert_budget_threshold,
    bootstrap_ratio_ci,
    engine_running,
    vus_pr,
)
from src.models.evaluation_records import DriveRecord, SessionScore


def compact_injections(outcomes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One record per injection (what was changed, and where) with every
    detector's outcome inside it — not the injection repeated per detector."""
    by_injection: dict[tuple[Any, ...], dict[str, Any]] = {}
    for outcome in outcomes:
        key = (outcome["drive"], outcome["kind"], outcome["severity"], outcome["rep"])
        entry = by_injection.get(key)
        if entry is None:
            injection = outcome["injection"]
            entry = {
                "drive": outcome["drive"],
                "session": outcome["session"],
                "rep": outcome["rep"],
                **(injection.to_dict() if hasattr(injection, "to_dict") else dict(injection)),
                "outcomes": {},
            }
            by_injection[key] = entry
        entry["outcomes"][outcome["detector"]] = {
            "status": outcome["status"],
            "time_to_detect_seconds": outcome["time_to_detect_seconds"],
        }
    return list(by_injection.values())


def summarize_false_alarms(
    records: Sequence[DriveRecord],
    names: Sequence[str],
    *,
    seed: int,
    roles: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """False alarms per engine-hour per detector, overall and per stratum,
    with drive-bootstrap intervals.

    Only ``alert``-role detectors (``models.roles``, A06.5) raise alarms; an
    ``evidence`` detector's episodes are reported per drive but are not
    counted against the alert budget here.
    """
    out: dict[str, Any] = {}
    for name in names:
        if (roles or {}).get(name, "alert") != "alert":
            out[name] = {
                "role": "evidence",
                "false_alarms_per_engine_hour": {
                    "estimate": None,
                    "note": "evidence role: its episodes are not alerts",
                },
                "withheld_share": None,
                "strata": {"baro": {}, "ambient": {}},
            }
            continue
        with_time = [r for r in records if r.hours > 0 and name in r.detectors]
        overall = bootstrap_ratio_ci(
            [r.detectors[name]["false_alarm_episodes"] for r in with_time],
            [r.hours for r in with_time],
            seed=seed,
        )
        strata: dict[str, Any] = {}
        for axis, key in (("baro", "baro_band"), ("ambient", "ambient_band")):
            groups: dict[str, list[DriveRecord]] = {}
            for record in with_time:
                groups.setdefault(record.to_dict()[key], []).append(record)
            strata[axis] = {
                band: bootstrap_ratio_ci(
                    [r.detectors[name]["false_alarm_episodes"] for r in members],
                    [r.hours for r in members],
                    seed=seed,
                ).to_dict()
                for band, members in sorted(groups.items())
            }
        rows = sum(r.detectors[name]["rows"] for r in records if name in r.detectors)
        withheld = sum(r.detectors[name]["withheld_rows"] for r in records if name in r.detectors)
        out[name] = {
            "role": "alert",
            "false_alarms_per_engine_hour": overall.to_dict(),
            "drives_with_engine_time": len(with_time),
            "withheld_share": None if rows == 0 else withheld / rows,
            "strata": strata,
        }
    return out


def summarize_recall(
    outcomes: Sequence[Mapping[str, Any]], names: Sequence[str], *, seed: int
) -> dict[str, Any]:
    """Recall per (detector, kind, severity), bootstrapped over drives.

    ``withheld`` (the detector did not look) and ``masked`` (it was already
    alerting on the clean rows of that window) outcomes are excluded from the
    denominator and counted separately: neither is a miss, and neither is a
    catch.
    """
    table: dict[str, Any] = {}
    for name in names:
        per_kind: dict[str, Any] = {}
        for kind in sorted({o["kind"] for o in outcomes}):
            per_severity: dict[str, Any] = {}
            for severity in sorted(
                {o["severity"] for o in outcomes if o["kind"] == kind}, key=float
            ):
                rows = [
                    o
                    for o in outcomes
                    if o["detector"] == name and o["kind"] == kind and o["severity"] == severity
                ]
                graded = [o for o in rows if o["status"] in ("detected", "missed")]
                drives = sorted({o["drive"] for o in graded})
                estimate = bootstrap_ratio_ci(
                    [
                        sum(1 for o in graded if o["drive"] == d and o["status"] == "detected")
                        for d in drives
                    ],
                    [sum(1 for o in graded if o["drive"] == d) for d in drives],
                    seed=seed,
                )
                latencies = [
                    o["time_to_detect_seconds"] for o in graded if o["status"] == "detected"
                ]
                per_severity[str(severity)] = {
                    "recall": estimate.to_dict(),
                    "injections": len(rows),
                    "graded": len(graded),
                    "detected": sum(1 for o in graded if o["status"] == "detected"),
                    "withheld": sum(1 for o in rows if o["status"] == "withheld"),
                    "masked": sum(1 for o in rows if o["status"] == "masked"),
                    "median_time_to_detect_seconds": (
                        float(np.median(latencies)) if latencies else None
                    ),
                }
            per_kind[kind] = per_severity
        table[name] = per_kind
    return table


def summarize_vus(
    slices: Sequence[Mapping[str, Any]], names: Sequence[str], *, seed: int
) -> dict[str, Any]:
    """Threshold-free VUS-PR per (detector, fault kind): computed per drive on
    that drive's injected slices, then averaged over drives (bootstrap CI)."""
    out: dict[str, Any] = {}
    for name in names:
        per_kind: dict[str, Any] = {}
        for kind in sorted({s["kind"] for s in slices}):
            values: list[float] = []
            for drive in sorted({s["drive"] for s in slices if s["kind"] == kind}):
                picked = [
                    s
                    for s in slices
                    if s["detector"] == name and s["kind"] == kind and s["drive"] == drive
                ]
                if not picked or not any(np.isfinite(x["scores"]).any() for x in picked):
                    # A verdict-only detector (fused) has no score to rank.
                    continue
                value = vus_pr(
                    np.concatenate([s["scores"] for s in picked]),
                    np.concatenate([s["labels"] for s in picked]),
                )
                if value is not None:
                    values.append(value)
            per_kind[kind] = (
                bootstrap_ratio_ci(values, [1.0] * len(values), seed=seed).to_dict()
                if values
                else {"estimate": None, "note": "withheld: no score to rank"}
            )
        out[name] = per_kind
    # What a ranking with no information scores: the labelled share of the
    # slices. Every VUS-PR above is read against it.
    out["_base_rate"] = {
        kind: float(np.mean(np.concatenate([s["labels"] for s in slices if s["kind"] == kind])))
        for kind in sorted({s["kind"] for s in slices})
    }
    return out


def summarize_shift(per_drive: Sequence[Mapping[str, Any]], names: Sequence[str]) -> dict[str, Any]:
    """Per detector: how many drives passed, failed, were invalid or withheld,
    and per (quantity, size) the flag rate on each drive (never pooled)."""
    out: dict[str, Any] = {}
    for name in names:
        verdicts: dict[str, int] = {}
        cells: dict[str, dict[str, list[float | None]]] = {}
        for entry in per_drive:
            report = entry["detectors"].get(name)
            if report is None:
                continue
            verdicts[report["verdict"]] = verdicts.get(report["verdict"], 0) + 1
            for outcome in [*report["outcomes"], report.get("control") or {}]:
                if not outcome:
                    continue
                cells.setdefault(outcome["quantity"], {}).setdefault(
                    f"{outcome['sigma']:g}", []
                ).append(outcome["flag_rate"])
        out[name] = {"verdicts": verdicts, "flag_rate_per_drive": cells}
    return out


def budget_thresholds_on(
    records: Sequence[DriveRecord],
    scores: Mapping[str, Mapping[str, SessionScore]],
    frames: Mapping[str, pd.DataFrame],
    names: Sequence[str],
    *,
    persistence: PersistenceSpec,
    block_seconds: float,
    budget: float,
    roles: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """A02.3: the single score threshold that would meet the alert budget on
    these drives, per detector with a score — the level a recalibration would
    choose on its evaluation sessions. In-sample to the drives it is measured
    on, so it is a target for the next calibration, never a held-out number.
    """
    out: dict[str, Any] = {}
    sessions = [name for record in records for name in record.sessions]
    for name in names:
        if (roles or {}).get(name, "alert") != "alert":
            out[name] = {"note": "evidence role: no alerts to hold to the budget"}
            continue
        traces = [
            ScoreTrace(
                scores[session][name].timestamps,
                scores[session][name].score,
                engine_running(frames[session]),
            )
            for session in sessions
            if name in scores.get(session, {})
        ]
        if not traces or not any(np.isfinite(t.scores).any() for t in traces):
            out[name] = {"note": "withheld: this detector reports no score"}
            continue
        out[name] = alert_budget_threshold(
            traces,
            persistence=persistence,
            block_seconds=block_seconds,
            budget_per_engine_hour=budget,
        ).to_dict()
    return out
