"""Phase six of a detector evaluation: the report dict and its regression compare (A02.4).

:func:`build_report` assembles the JSON report from the phase results -- the
protocol parameters that make two reports comparable (R06.5,
:mod:`src.models.benchmark_gate`), the artifacts, the per-drive records, the
summaries, the known-answer shift runs, the injections and every skip with its
reason -- and :func:`compare_to_baseline` diffs it against an earlier report.
Nothing here writes a file, prints, or reads the clock: the caller supplies
``generated_at`` and ``command`` and decides where the report goes. The
Markdown renderer is :func:`src.models.evaluation_report.render_markdown`.
Extracted from ``scripts/evaluate_detectors.py`` (R15.4).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.analysis.detector_evaluation_drives import summarize_events
from src.analysis.detector_evaluation_inputs import (
    BehaviorInputs,
    EvaluationRequest,
    PreparedInputs,
)
from src.analysis.detector_evaluation_plan import DrivePlan, LoadedSessions
from src.analysis.detector_evaluation_run import Aggregate, DetectorSet
from src.models.alert_budget import CADENCE_KEY
from src.models.benchmark_gate import PROTOCOL_NAME, PROTOCOL_VERSION, diff_reports
from src.models.evaluation_baselines import block_seconds_of
from src.models.evaluation_detectors import detector_roles
from src.models.evaluation_summaries import (
    budget_thresholds_on,
    compact_injections,
    summarize_false_alarms,
    summarize_recall,
    summarize_shift,
    summarize_vus,
)


def build_report(
    *,
    request: EvaluationRequest,
    prepared: PreparedInputs,
    behavior: BehaviorInputs,
    plan: DrivePlan,
    loaded: LoadedSessions,
    detectors: DetectorSet,
    roles: Mapping[str, Sequence[str]],
    aggregate: Aggregate,
    generated_at: str,
    command: str,
) -> dict[str, Any]:
    """The evaluation report: every field the JSON, the Markdown and the gates read."""
    config = request.config
    persistence = prepared.persistence
    names = detectors.names
    records = aggregate.records
    frames = loaded.frames
    role_of = detector_roles(config, names)
    return {
        "generated_at": generated_at,
        "command": command,
        "config": str(request.config_path),
        # R06.5: what makes two reports comparable (benchmark_gate): the
        # protocol, and every parameter that decides which windows are
        # injected, with what, and when an alert counts.
        "protocol": {
            "name": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "seed": request.seed,
            "faults": sorted(request.faults),
            "injections_per_drive": request.injections_per_drive,
            "window_seconds": request.window_seconds,
            "lead_seconds": request.lead_seconds,
            "grace_seconds": request.grace_seconds,
            "persistence": {
                "required": persistence.required,
                "window": persistence.window,
                "window_seconds": persistence.window_seconds,
            },
        },
        "behavior_models": behavior.record,
        "artifacts": prepared.artifacts,
        "persistence": {
            "required": persistence.required,
            "window": persistence.window,
            "window_seconds": persistence.window_seconds,
        },
        "budget_per_engine_hour": request.budget,
        # A06.6: the sequence and fused verdicts were scored only on the
        # windows serving forward-passes at this cadence; the alert-budget
        # gate refuses a report scored at another one.
        CADENCE_KEY: config.models.sequence_forward_every_n_ticks,
        "detectors": names,
        "withheld_detectors": detectors.withheld,
        # What the baselines actually rest on: a requested session that did
        # not load is named under ``baseline_*_not_loaded``, never counted.
        "baseline_fit_sessions": [n for n in plan.fit_names if n in frames],
        "baseline_threshold_sessions": [n for n in plan.calibration_names if n in frames],
        "baseline_fit_sessions_not_loaded": [n for n in plan.fit_names if n not in frames],
        "baseline_threshold_sessions_not_loaded": [
            n for n in plan.calibration_names if n not in frames
        ],
        "baseline_thresholds": detectors.baselines.thresholds,
        "sequence_parity": detectors.parity,
        "drives": [r.to_dict() for r in records],
        "skipped_drives": aggregate.skipped,
        "training_eras": sorted(e.label for e in plan.training_eras),
        "roles": role_of,
        "false_alarms": summarize_false_alarms(
            [r for r in records if not r.cross_era], names, seed=request.seed, roles=role_of
        ),
        "recall": (
            summarize_recall(aggregate.outcomes, names, seed=request.seed)
            if aggregate.outcomes
            else {}
        ),
        "vus_pr": (
            summarize_vus(aggregate.slices, names, seed=request.seed) if aggregate.slices else {}
        ),
        "budget_threshold_on_evaluation": budget_thresholds_on(
            [r for r in records if not r.cross_era],
            aggregate.scores,
            frames,
            names,
            persistence=persistence,
            block_seconds=block_seconds_of(prepared.trained_seq, config),
            budget=request.budget,
            roles=role_of,
        ),
        "events": summarize_events(plan.registry.events, aggregate.scores, names),
        "fault_events": len(plan.registry.of_kind("fault")),
        "known_answers": {
            "single_quantity_shift": {
                "quantities": {role: list(channels) for role, channels in roles.items()},
                "not_applicable": {
                    r.names[0]: "reads arrived samples, not the featurized rows"
                    for r in detectors.raw_detectors
                },
                "per_drive": aggregate.shift_runs,
                "summary": summarize_shift(
                    aggregate.shift_runs, [n for d in detectors.detectors for n in d.names]
                ),
            }
        },
        "injections": compact_injections(aggregate.outcomes),
        "skipped_injections": aggregate.skipped_injections,
        "load_errors": loaded.load_errors,
    }


def compare_to_baseline(
    report: Mapping[str, Any],
    baseline_report: Mapping[str, Any],
    *,
    baseline_path: Path,
    tolerance: float,
) -> dict[str, Any]:
    """Diff ``report`` against an earlier report of the same drives and protocol
    (R06.5); the returned record is what the report carries as ``regressions``.

    ``report`` is round-tripped through JSON first: the diff reads the report as
    it will be written, so a value that does not survive serialisation cannot
    be compared as something it is not.
    """
    regression = diff_reports(
        baseline_report,
        json.loads(json.dumps(report, default=str)),
        tolerance=tolerance,
    )
    return {**regression, "baseline": str(baseline_path)}
