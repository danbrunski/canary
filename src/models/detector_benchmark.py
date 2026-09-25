"""The synthetic-fault benchmark grid behind retrain's regression gate (Task 2.5/2.6).

Session x target channel x fault kind: each (session, channel) cell runs
:func:`src.models.synthetic_fault_eval.evaluate_detectors`, the cells roll up
into one per-detector x per-fault-class table (:func:`aggregate_cells`), and
two reports of the same sessions and decode chain diff into a regression
verdict (:func:`diff_reports`). ``scripts/detector_benchmark.py`` is the CLI;
``scripts/retrain.py`` runs the grid as its post-retrain gate. Both used to
share this code through a sibling import between scripts (R06.4).

Known limitation: ``evaluate_detectors`` calls ``featurize_wide_frame`` with
no ``behavior_models``, so an IF artifact trained with behavior-model
deviation (``deviation:*``) columns cannot be schema-matched here (see the
CLI's docstring).
"""

from __future__ import annotations

import hashlib
import logging
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CanaryConfig
from src.models.isolation_forest import TrainedIsolationForest
from src.models.sequence_protocol import TrainedSequenceModel
from src.models.sequence_registry import resolve_sequence_backend
from src.models.synthetic_fault_eval import DEFAULT_FAULT_KINDS, evaluate_detectors
from src.preprocessing import AnomalyKind

log = logging.getLogger(__name__)

#: A default spread of engine-telemetry channels this repo's OBD capture
#: reliably produces (see `src/analysis/events/*.py`, `src/analysis/derived.py`)
#: — enough variety (a fast RPM-derived signal, a slow-changing temperature,
#: a driver-input signal) to exercise the detectors without hardcoding this
#: vehicle's full PID set.
DEFAULT_TARGET_CHANNELS: tuple[str, ...] = (
    "obd:RPM",
    "obd:SPEED",
    "obd:COOLANT_TEMP",
    "obd:THROTTLE_POS",
    "obd:ENGINE_LOAD",
)

#: Regression tolerance (Task 2.6), a single global relative-change fraction
#: applied to every metric. WHY 0.15 and not tighter: the grid is small (a
#: handful of sessions x channels, `n_injections` runs per cell) so a per-cell
#: `recall` can swing by a whole injection flipping detected/undetected —
#: on an 8-run cell that's 12.5 pts of "recall" with zero real behavior
#: change. A tolerance below that floor would flag grid noise as regression
#: on every run; 0.15 clears it with margin while still catching an actual
#: broken detector (whose recall/precision typically collapses by much more
#: than one flipped run, or whose alerts-per-hour-clean — pooled over full
#: corpus exposure hours, not a handful of runs, so it's less noisy per-run —
#: jumps well past a 15% bump from an actual false-alarm flood).
DEFAULT_TOLERANCE = 0.15

#: Which direction is "worse" for each rollup metric a regression compare
#: cares about — `precision` can be `None` (no denominator) and is skipped
#: rather than treated as -inf/+inf.
_REGRESSION_DIRECTION: dict[str, str] = {
    "recall": "down",
    "precision": "down",
    "median_time_to_detect_seconds": "up",
    "alerts_per_hour_clean": "up",
}


@dataclass(frozen=True)
class GridCell:
    """One (session, channel) grid unit — its `evaluate_detectors` report
    covers every requested fault kind for that channel in one call."""

    session: str
    channel: str
    seed: int
    report: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "channel": self.channel,
            "seed": self.seed,
            "report": self.report,
        }


def cell_seed(base_seed: int, session: str, channel: str) -> int:
    """Deterministic per-(session, channel) seed derived from `--seed`.

    `hash()` is salted per-process (`PYTHONHASHSEED`) and would make two runs
    with the same `--seed` diverge; SHA-256 of the identity string doesn't.
    """
    digest = hashlib.sha256(f"{base_seed}:{session}:{channel}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def select_target_channels(resampled: pd.DataFrame, candidates: Sequence[str]) -> list[str]:
    """Candidates actually usable in this session's frame.

    `AnomalyInjector` raises `KeyError` on a channel absent from the frame,
    and every injection kind needs at least one real (non-NaN) value to
    perturb or hold — so a candidate missing or all-NaN in this session is
    silently skipped rather than crashing the whole grid.
    """
    return [c for c in candidates if c in resampled.columns and resampled[c].notna().any()]


def run_grid(
    sessions: Sequence[tuple[str, pd.DataFrame]],
    config: CanaryConfig,
    trained_if: TrainedIsolationForest,
    *,
    fault_kinds: Sequence[AnomalyKind] = DEFAULT_FAULT_KINDS,
    channels: Sequence[str] = DEFAULT_TARGET_CHANNELS,
    n_injections: int = 5,
    seed: int = 42,
    trained_sequence: TrainedSequenceModel | None = None,
) -> list[GridCell]:
    """Run `evaluate_detectors` over every (session, usable channel) cell.

    One call per cell covers every fault kind for that channel (the harness
    already loops fault kinds internally) plus that cell's clean/nominal
    pass — nothing here re-implements either.
    """
    cells: list[GridCell] = []
    for session_name, resampled in sessions:
        usable = select_target_channels(resampled, channels)
        if not usable:
            log.warning("session %s: no requested channel usable; skipping", session_name)
            continue
        for channel in usable:
            seed_for_cell = cell_seed(seed, session_name, channel)
            try:
                report = evaluate_detectors(
                    resampled,
                    config,
                    trained_if,
                    fault_channel=channel,
                    fault_kinds=fault_kinds,
                    n_injections=n_injections,
                    seed=seed_for_cell,
                    trained_sequence=trained_sequence,
                )
            except KeyError as e:
                # The clean/nominal pass (channel-independent) is scored
                # first inside evaluate_detectors, so a schema mismatch
                # between this session's captured channels and the IF
                # artifact's trained feature columns (e.g. a session with no
                # GPS fix against a model trained with a GPS committee
                # feature) fails identically for every channel — skip the
                # rest of this session rather than repeat the same error.
                log.warning(
                    "session %s: IF model feature schema mismatch (%s); "
                    "skipping remaining channels for this session",
                    session_name,
                    e,
                )
                break
            cells.append(
                GridCell(session=session_name, channel=channel, seed=seed_for_cell, report=report)
            )
    return cells


def _nominal_by_session(cells: Sequence[GridCell]) -> dict[str, dict[str, Any]]:
    """One `nominal_fp` block per session.

    `evaluate_detectors`'s clean/nominal pass scores the unmodified frame —
    identical across every channel of the same session — so keeping only the
    first cell's block per session avoids double-counting the same clean
    hours once per channel.
    """
    by_session: dict[str, dict[str, Any]] = {}
    for cell in cells:
        by_session.setdefault(cell.session, cell.report["nominal_fp"])
    return by_session


def aggregate_cells(cells: Sequence[GridCell]) -> dict[str, Any]:
    """Roll the per-(session, channel) grid up into one per-detector x
    per-fault-class table.

    - ``recall`` pools raw `detected`/`runs` across every cell for that
      (detector, fault_kind) — a session/channel-weighted average, not a
      mean of per-cell rates, so a channel with more injections carries
      proportionally more weight.
    - ``alerts_per_hour_clean`` pools each detector's clean-pass alerts and
      hours once per SESSION (see `_nominal_by_session`) into one corpus-wide
      rate; it's fault-kind-agnostic (a detector's baseline FP rate doesn't
      depend on which fault it's being graded against) so the same number
      repeats across that detector's fault-kind rows.
    - ``precision`` has no labeled negatives to count directly — the harness
      only injects positives. It's estimated instead: the detector's clean
      corpus alert rate predicts how many false alarms it would raise, absent
      the fault, over the same total exposure time (`n_injections` full-length
      scoring passes per cell) the fault kind's grid cells covered.
      `TP / (TP + FP_estimate)` is the closest analogue to precision this
      harness can honestly report — not a substitute for a labeled
      precision/recall curve over real negatives.
    """
    nominal_by_session = _nominal_by_session(cells)
    detector_names: set[str] = set()
    for nominal in nominal_by_session.values():
        detector_names.update(nominal)

    alerts_per_hour: dict[str, float] = {}
    nominal_totals: dict[str, dict[str, float]] = {}
    for name in sorted(detector_names):
        total_alerts = sum(n[name]["alerts"] for n in nominal_by_session.values() if name in n)
        total_hours = sum(n[name]["hours"] for n in nominal_by_session.values() if name in n)
        rate = (total_alerts / total_hours) if total_hours else 0.0
        alerts_per_hour[name] = rate
        nominal_totals[name] = {
            "alerts": float(total_alerts),
            "hours": float(total_hours),
            "per_hour": rate,
        }

    pooled: dict[tuple[str, str], dict[str, Any]] = {}
    for cell in cells:
        for metric in cell.report["per_fault"]:
            key = (metric["detector"], metric["fault_kind"])
            bucket = pooled.setdefault(
                key, {"runs": 0, "detected": 0, "latencies": [], "exposure_hours": 0.0}
            )
            bucket["runs"] += metric["runs"]
            bucket["detected"] += metric["detected"]
            bucket["latencies"].extend(metric["latencies_seconds"])
            hours = cell.report["nominal_fp"].get(metric["detector"], {}).get("hours", 0.0)
            bucket["exposure_hours"] += hours * metric["runs"]

    table: list[dict[str, Any]] = []
    for (detector, fault_kind), bucket in sorted(pooled.items()):
        runs = bucket["runs"]
        detected = bucket["detected"]
        recall = (detected / runs) if runs else 0.0
        per_hour = alerts_per_hour.get(detector, 0.0)
        fp_estimate = per_hour * bucket["exposure_hours"]
        denom = detected + fp_estimate
        precision = (detected / denom) if denom > 0 else None
        latencies = bucket["latencies"]
        median_latency = float(statistics.median(latencies)) if latencies else None
        table.append(
            {
                "detector": detector,
                "fault_kind": fault_kind,
                "runs": runs,
                "detected": detected,
                "recall": recall,
                "precision": precision,
                "median_time_to_detect_seconds": median_latency,
                "alerts_per_hour_clean": per_hour,
            }
        )

    withheld: dict[str, str] = {}
    for cell in cells:
        for name, reason in cell.report.get("withheld", {}).items():
            withheld.setdefault(name, reason)
    return {
        "sessions": sorted(nominal_by_session),
        "channels": sorted({cell.channel for cell in cells}),
        "fault_kinds": sorted({fault_kind for _, fault_kind in pooled}),
        "nominal_fp": nominal_totals,
        "table": table,
        # A02.9: detectors the grid could not grade, with the reason — never a
        # silent 0.0 recall row.
        "withheld": withheld,
    }


#: Version of the per-session decode chain behind a report
#: (`feature_pipeline.load_resampled_session`). 1: bindings-only decode, no DID/DBC decode, no
#: quality gate (reports without the field). 2: the trainers' chain (A02). Two
#: reports from different chains score different rows and are not compared.
LOADER_VERSION = 2


def build_report(
    cells: Sequence[GridCell],
    *,
    seed: int,
    n_injections: int,
    generated_at: str,
) -> dict[str, Any]:
    """The full JSON-safe report: aggregate table + every per-cell detail
    (kept so `--baseline` regression compare — 2.6, via `diff_reports` — has
    the raw per-session/channel numbers, not just the rolled-up table)."""
    aggregate = aggregate_cells(cells)
    return {
        "generated_at": generated_at,
        "loader_version": LOADER_VERSION,
        "seed": seed,
        "n_injections": n_injections,
        "grid_cells": len(cells),
        **aggregate,
        "cells": [cell.to_dict() for cell in cells],
    }


def diff_reports(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Per-(detector, fault_kind) regression compare (Task 2.6).

    Only cells present in BOTH reports' ``table`` are graded against
    ``tolerance`` — a cell that only exists in one report (a detector/channel
    dropped or added between runs) is recorded as ``added``/``removed`` for
    visibility but can't have "regressed", there being nothing to compare.

    Two reports that scored different sessions, different evaluation
    holdouts, or rows from a different decode chain are ``incomparable``
    (``comparable: False`` with the reason, never ``regressed``): their
    totals come from different drives, and comparing them is comparing
    aggregates of different populations.
    """
    reason = incomparable_reason(baseline, current)
    if reason is not None:
        return {
            "tolerance": tolerance,
            "regressed": False,
            "comparable": False,
            "reason": reason,
            "cells": [],
        }
    baseline_by_key = {(row["detector"], row["fault_kind"]): row for row in baseline["table"]}
    current_by_key = {(row["detector"], row["fault_kind"]): row for row in current["table"]}

    cells: list[dict[str, Any]] = []
    any_regressed = False
    for detector, fault_kind in sorted(set(baseline_by_key) | set(current_by_key)):
        base_row = baseline_by_key.get((detector, fault_kind))
        cur_row = current_by_key.get((detector, fault_kind))
        if base_row is None:
            cells.append(
                {"detector": detector, "fault_kind": fault_kind, "status": "added", "metrics": {}}
            )
            continue
        if cur_row is None:
            cells.append(
                {
                    "detector": detector,
                    "fault_kind": fault_kind,
                    "status": "removed",
                    "metrics": {},
                }
            )
            continue

        metrics: dict[str, Any] = {}
        for metric_name, direction in _REGRESSION_DIRECTION.items():
            base_value = base_row.get(metric_name)
            cur_value = cur_row.get(metric_name)
            regressed = False
            if base_value is not None and cur_value is not None:
                if direction == "down":
                    regressed = cur_value < base_value * (1 - tolerance)
                else:
                    regressed = cur_value > base_value * (1 + tolerance)
            metrics[metric_name] = {
                "baseline": base_value,
                "current": cur_value,
                "regressed": regressed,
            }
            any_regressed = any_regressed or regressed
        cells.append(
            {
                "detector": detector,
                "fault_kind": fault_kind,
                "status": "compared",
                "metrics": metrics,
            }
        )

    return {"tolerance": tolerance, "regressed": any_regressed, "comparable": True, "cells": cells}


def incomparable_reason(baseline: Mapping[str, Any], current: Mapping[str, Any]) -> str | None:
    """Why two benchmark reports cannot be diffed, or ``None``."""
    base_loader = int(baseline.get("loader_version", 1))
    cur_loader = int(current.get("loader_version", 1))
    if base_loader != cur_loader:
        return f"decode chain differs (loader_version {base_loader} vs {cur_loader})"
    base_sessions = sorted(baseline.get("sessions", []))
    cur_sessions = sorted(current.get("sessions", []))
    if base_sessions != cur_sessions:
        return f"scored different sessions ({base_sessions} vs {cur_sessions})"
    base_holdout = (baseline.get("holdout") or {}).get("evaluation")
    cur_holdout = (current.get("holdout") or {}).get("evaluation")
    if base_holdout is not None and cur_holdout is not None:
        if sorted(base_holdout) != sorted(cur_holdout):
            return (
                f"different evaluation holdouts ({sorted(base_holdout)} vs {sorted(cur_holdout)})"
            )
    return None


def load_trained_sequence(
    config: CanaryConfig,
    override: Path | None,
    *,
    skip: bool,
) -> TrainedSequenceModel | None:
    """The configured `sequence_detector` backend's trained model, or `None`
    when disabled/unavailable/skipped — `evaluate_detectors` already handles
    `trained_sequence=None` gracefully (IF/fused only), same as no manifest
    configured in the daemon."""
    if skip:
        return None
    backend = resolve_sequence_backend(config)
    if backend is None:
        if override is not None:
            log.warning(
                "--sequence-model given but no sequence_detector backend is "
                "enabled in config; skipping sequence coverage"
            )
        return None
    missing = backend.missing_requirements()
    if missing:
        log.warning(
            "sequence backend %s missing requirements %s; skipping", backend.selector, missing
        )
        return None
    path = override if override is not None else backend.artifact_path(config)
    if not path.exists():
        log.warning("sequence-detector artifact not found at %s; skipping", path)
        return None
    return backend.load_model(path)


__all__ = [
    "DEFAULT_TARGET_CHANNELS",
    "DEFAULT_TOLERANCE",
    "LOADER_VERSION",
    "GridCell",
    "aggregate_cells",
    "build_report",
    "cell_seed",
    "diff_reports",
    "incomparable_reason",
    "load_trained_sequence",
    "run_grid",
    "select_target_channels",
]
