#!/usr/bin/env python3
"""Score detectors on held-out drives, on the same rows, by one protocol (A02.4).

One command for every detector — the served Isolation Forest, sequence model
and fusion, and the A02.5 baselines — on drives none of them trained or
calibrated on:

    .venv/bin/python scripts/evaluate_detectors.py \\
        --config canary.example.yaml --profile nissan-xterra-2008 \\
        --sessions-root ~/canary-data/sessions/nissan-xterra-2008 \\
        --sessions-root ~/canary-data/sessions-0918 \\
        --out data/analyses/<date>-detector-baseline.json

The protocol lives in :mod:`src.models.evaluation`; the detectors in
:mod:`src.models.evaluation_detectors`; the realistic faults in
:mod:`src.models.synthetic_fault_eval`. The evaluation itself is a library
(R15): scoring and its worker pool in :mod:`src.models.evaluation_scorer` and
:mod:`src.models.evaluation_workers`, the cache identity in
:mod:`src.models.evaluation_identity`, the summaries and the Markdown in
:mod:`src.models.evaluation_summaries` and :mod:`src.models.evaluation_report`,
and the phases that need the labelled events -- plan, assemble, score,
aggregate, report -- in ``src.analysis.detector_evaluation_*``. This script
owns the arguments, logging, the exit status and writing the report: it loads
sessions (through the derived-frame store under ``--derived-dir``), decides
which drives are held out for *every* compared detector, scores clean and
injected rows, and writes a JSON report and a Markdown table with one row per
drive.

A drive is *skipped* (with the reason) when a detector trained or calibrated
on any of its sessions, when it straddles a tune or hardware boundary, or
when it has no telemetry. Captures are scored: modes come from the same
segmenter the daemon runs (``featurize_wide_frame``), never from sidecars.
Rows are featurized with the profile's behavior models, as the trainers and
the daemon featurize them, so an artifact trained on ``deviation:*`` columns
is scored on those columns (the retired grid's blind spot, R06.5).

Drives are scored in parallel (``--workers``, P02.5) and every detector's
scores are cached under ``--cache-dir/scores``, keyed by the artifact's
sha256, the session frame's content, the injection and the code
(:mod:`src.models.evaluation_cache`): an unchanged re-run reads everything
back, and a new artifact is rescored for its own detector alone. The report
does not depend on ``--workers``; ``--scoring-threads`` fixes torch's thread
count in every process, because the LSTM's scores move in the last bits
between thread counts. An injected slice is featurized from its clean slice,
recomputing only what the injected channels reach (P02.4).

It is also retrain's post-train regression gate (R06.5, replacing the retired
``detector_benchmark`` grid): ``--baseline <report.json>`` diffs this run
against an earlier report of the same drives under the same protocol
parameters (:mod:`src.models.benchmark_gate`) and exits 3 on a regression.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from _cli import (  # type: ignore[import-not-found]
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        add_profile_arg,
        load_config,
        scoped_config,
        scoped_models_dir,
    )
except ImportError:  # loaded as `scripts.evaluate_detectors` — repo root on path, not scripts/
    from scripts._cli import (
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        add_profile_arg,
        load_config,
        scoped_config,
        scoped_models_dir,
    )

from src.analysis.detector_evaluation_drives import clean_drive_record, summarize_events
from src.analysis.detector_evaluation_inputs import (
    EvaluationRefusedError,
    EvaluationRequest,
    prepare_behavior,
    prepare_inputs,
)
from src.analysis.detector_evaluation_plan import load_planned_sessions, plan_drives, render_plan
from src.analysis.detector_evaluation_report import build_report, compare_to_baseline
from src.analysis.detector_evaluation_run import (
    aggregate_results,
    assemble_detectors,
    plan_scoring_tasks,
    score_planned_drives,
    scoring_keys,
    settle_score_cache,
    sweep_spec,
)
from src.analysis.labelled_events import LABELLED_EVENTS_FILENAME
from src.config import CanaryConfig
from src.log import configure_logging
from src.models.benchmark_gate import DEFAULT_TOLERANCE, regression_summary
from src.models.evaluation_cache import ScoreCache, code_version
from src.models.evaluation_heldout import (
    DETECTORS_OF_ARTIFACT,
    PER_DETECTOR_SEEN_ROLES,
    in_sample_detectors,
    record_seen,
)
from src.models.evaluation_identity import (
    KeySources,
    changed_key_inputs,
    file_sha256,
    key_inputs,
    sequence_identity,
)
from src.models.evaluation_loading import EVALUATION_SESSION_KINDS, load_resampled_sessions
from src.models.evaluation_records import DriveRecord, SessionScore, drop_in_sample, session_scores
from src.models.evaluation_report import render_markdown
from src.models.evaluation_scorer import (
    ROWS_ENTRY,
    SHIFT_ROWS_ENTRY,
    Component,
    DriveResult,
    DriveScorer,
    DriveTask,
    SweepSpec,
    detector_components,
    score_model_identity,
    shift_slice,
)
from src.models.evaluation_streams import CachedStreams, load_raw_streams
from src.models.evaluation_summaries import (
    budget_thresholds_on,
    compact_injections,
    summarize_false_alarms,
    summarize_recall,
    summarize_shift,
    summarize_vus,
)
from src.models.evaluation_workers import (
    DEFAULT_SCORING_THREADS,
    SCORING_MEMORY_FRACTION,
    SCORING_WORKER_PEAK_BYTES,
    ScorerSpec,
    _cgroup_memory_headroom,
    _init_scoring_worker,
    build_worker_scorer,
    pin_threads,
    score_drives,
    scoring_workers,
)
from src.models.false_positive_benchmark import V1_ALERTS_PER_HOUR_BUDGET
from src.paths import DATA_DIR
from src.preprocessing.anomaly_injection import REALISTIC_FAULTS, load_fault_channel_roles
from src.preprocessing.derived_store import DEFAULT_DERIVED_DIR
from src.timefmt import assume_utc
from src.vehicle.profiles import resolve_profile_for_tooling

log = logging.getLogger(__name__)

#: The names this script exported before R15 moved the evaluation into libraries
#: (``src.models.evaluation_*``, ``src.analysis.detector_evaluation_*``): each is
#: the owner's object, kept so a caller importing from the script still works
#: (``tests/test_detector_evaluation_boundaries.py`` checks identity). Nothing
#: in ``src`` imports this list's source: libraries import their owners.
__all__ = [
    "DEFAULT_CACHE_DIR",
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_LEAD_SECONDS",
    "DEFAULT_SCORING_THREADS",
    "DEFAULT_WINDOW_SECONDS",
    "DETECTORS_OF_ARTIFACT",
    "EVALUATION_SESSION_KINDS",
    "PER_DETECTOR_SEEN_ROLES",
    "ROWS_ENTRY",
    "SCORING_MEMORY_FRACTION",
    "SCORING_WORKER_PEAK_BYTES",
    "SHIFT_ROWS_ENTRY",
    "CachedStreams",
    "Component",
    "DriveRecord",
    "DriveResult",
    "DriveScorer",
    "DriveTask",
    "KeySources",
    "ScorerSpec",
    "SessionScore",
    "SweepSpec",
    "_cgroup_memory_headroom",
    "_evaluate",
    "_init_scoring_worker",
    "budget_thresholds_on",
    "build_worker_scorer",
    "changed_key_inputs",
    "clean_drive_record",
    "compact_injections",
    "detector_components",
    "drop_in_sample",
    "file_sha256",
    "in_sample_detectors",
    "key_inputs",
    "load_raw_streams",
    "load_resampled_sessions",
    "main",
    "parse_args",
    "pin_threads",
    "record_seen",
    "render_markdown",
    "score_drives",
    "score_model_identity",
    "scoring_workers",
    "sequence_identity",
    "session_scores",
    "shift_slice",
    "summarize_events",
    "summarize_false_alarms",
    "summarize_recall",
    "summarize_shift",
    "summarize_vus",
]

#: Detector score caches: gitignored, regenerable, keyed on every input. The
#: resampled frames themselves live in the derived-frame store (P01.2).
DEFAULT_CACHE_DIR = DATA_DIR / "cache" / "evaluation_frames"


#: Clean rows before an injected window: the segmenter's lookback plus the
#: rolling window and one sequence window, so every scorer is warm.
DEFAULT_LEAD_SECONDS = 240.0


DEFAULT_WINDOW_SECONDS = 240.0


#: Fault-injection faults need a grace past the window for rolling features
#: and a sequence window to see the change.
DEFAULT_GRACE_SECONDS = 60.0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="evaluate_detectors", description=__doc__.split("\n")[0])
    parser.add_argument("sessions", nargs="*", type=Path, help="Session directories.")
    parser.add_argument(
        "--sessions-root",
        action="append",
        type=Path,
        default=[],
        help="Directory of sessions to consider (repeatable).",
    )
    add_common_args(parser)
    add_profile_arg(parser)
    parser.add_argument("--if-model", type=Path, default=None)
    parser.add_argument("--sequence-model", type=Path, default=None)
    parser.add_argument("--no-sequence", action="store_true")
    parser.add_argument(
        "--baselines",
        nargs="*",
        default=["robust_z", "pca", "untrained_autoencoder", "plausibility"],
        choices=[
            "robust_z",
            "pca",
            "untrained_autoencoder",
            "plausibility",
            "robust_z_curated",
            "mahalanobis_curated",
        ],
        help="Baselines scored beside the served detectors. `plausibility` is A05's "
        "deterministic checks over arrived samples (needs the profile's plausibility.yaml). "
        "`robust_z_curated` / `mahalanobis_curated` (A04.13) read the profile's "
        "curated_outlier_channels.yaml (--curated-channels).",
    )
    parser.add_argument(
        "--curated-channels",
        type=Path,
        default=None,
        help="Curated channel list for the *_curated baselines "
        "(default: data/<profile>/curated_outlier_channels.yaml).",
    )
    parser.add_argument(
        "--baseline-fit-sessions",
        type=int,
        default=12,
        help="Fit baselines on this many of the sequence model's training sessions, "
        "spread evenly in time (default 12).",
    )
    parser.add_argument(
        "--faults",
        nargs="*",
        default=list(REALISTIC_FAULTS),
        choices=list(REALISTIC_FAULTS),
    )
    parser.add_argument("--injections-per-drive", type=int, default=2)
    parser.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--lead-seconds", type=float, default=DEFAULT_LEAD_SECONDS)
    parser.add_argument("--grace-seconds", type=float, default=DEFAULT_GRACE_SECONDS)
    parser.add_argument("--labels", type=Path, default=None)
    parser.add_argument("--tune-revisions", type=Path, default=None)
    parser.add_argument("--fault-roles", type=Path, default=None)
    add_bindings_arg(parser)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="Detector score caches (default data/cache/evaluation_frames).",
    )
    parser.add_argument(
        "--no-score-cache",
        action="store_true",
        help="Score everything afresh and keep nothing (P02.5; default: detector scores "
        "are cached under --cache-dir/scores by artifact, frame, injection and code).",
    )
    parser.add_argument(
        "--derived-dir",
        type=Path,
        default=DEFAULT_DERIVED_DIR,
        help="Derived session frames, read back or built here (P01.2; default "
        "data/cache/derived_sessions).",
    )
    parser.add_argument(
        "--no-derived-store",
        dest="derived_dir",
        action="store_const",
        const=None,
        help="Reduce every session in memory and keep nothing.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Processes for session loading and for scoring drives (P02.5; the pool is "
        "capped by memory and cores). The report does not depend on it.",
    )
    parser.add_argument(
        "--scoring-threads",
        type=int,
        default=DEFAULT_SCORING_THREADS,
        help="Torch threads in every scoring process, the same whatever --workers is "
        "(the LSTM's scores move in the last bits between thread counts). 0 keeps torch's "
        "default, the harness's behaviour before P02.5 (default %(default)s).",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--parity-rows", type=int, default=200)
    parser.add_argument("--budget", type=float, default=V1_ALERTS_PER_HOUR_BUDGET)
    parser.add_argument(
        "--behavior-models",
        type=Path,
        default=None,
        help="Behavior-model surfaces the rows are featurized with (default: the "
        "profile-scoped daemon.behavior_models, as the trainers and the daemon use).",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="An earlier report of the same drives under the same protocol parameters "
        "(R06.5): diff this run against it and exit 3 on a regression beyond --tolerance.",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        # A literal "%" in argparse help must be doubled.
        help=f"Relative regression tolerance for --baseline (default "
        f"{DEFAULT_TOLERANCE * 100:.0f}%%; see benchmark_gate.DEFAULT_TOLERANCE).",
    )
    parser.add_argument("--out", type=Path, required=True, help="JSON report path.")
    parser.add_argument("--markdown", type=Path, default=None, help="Default: --out with .md.")
    parser.add_argument(
        "--since",
        type=_utc_date,
        default=None,
        help="Only evaluate drives starting at/after this UTC date or time.",
    )
    parser.add_argument(
        "--until",
        type=_utc_date,
        default=None,
        help="Only evaluate drives starting before this UTC date or time.",
    )
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Score only sessions whose derived frame in --derived-dir is fresh; name "
        "the rest as skipped.",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Print which drives would be evaluated or skipped (and why) and which "
        "sessions would be loaded, then exit without loading anything.",
    )
    parser.add_argument(
        "--no-shift-test",
        action="store_true",
        help="Skip the A04.1 known answer (a single quantity shifted by 3-100 sigma).",
    )
    parser.add_argument(
        "--shift-rows",
        type=int,
        default=900,
        help="Rows per drive the single-quantity shift is scored on (default 900).",
    )
    add_log_level_arg(parser, default="WARNING")
    return parser.parse_args(argv)


def _utc_date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return assume_utc(parsed)


def _profile_data_dir(config: CanaryConfig, profile: str | None) -> Path:
    """``data/<slug>/`` — the vehicle's labels, tune registry and fault roles."""
    return DATA_DIR / resolve_profile_for_tooling(config, profile).slug


def _harness_files() -> tuple[Path, ...]:
    here = Path(__file__).resolve()
    return (here, here.with_name("_cli.py"))


def request_from_args(args: argparse.Namespace) -> EvaluationRequest:
    """The parsed arguments as the library's request: every default resolved here,
    the config scoped once (P02.11).

    Featurization, the plausibility limits and the tune registry all read the
    vehicle's data directory. This harness used to featurize through the
    unscoped config — without the vehicle's afr_calibration.yaml the daemon
    applies — while hashing that absent file.
    """
    config = scoped_config(load_config(args), args.profile)
    models_dir = scoped_models_dir(config, args.profile)
    data_dir = _profile_data_dir(config, args.profile)
    return EvaluationRequest(
        config=config,
        config_path=Path(args.config),
        data_dir=data_dir,
        if_path=args.if_model or models_dir / "isolation_forest" / "model.joblib",
        sequence_path=args.sequence_model or models_dir / "lstm_autoencoder" / "model.pt",
        use_sequence=not args.no_sequence,
        behavior_path=args.behavior_models or config.daemon.behavior_models,
        bindings_path=args.bindings,
        sessions=tuple(args.sessions),
        sessions_roots=tuple(args.sessions_root),
        labels_path=args.labels or data_dir / LABELLED_EVENTS_FILENAME,
        tune_revisions_path=args.tune_revisions,
        fault_roles_path=args.fault_roles or data_dir / "fault_injection.yaml",
        curated_channels_path=(args.curated_channels or data_dir / "curated_outlier_channels.yaml"),
        baselines=tuple(args.baselines),
        baseline_fit_sessions=args.baseline_fit_sessions,
        faults=tuple(args.faults),
        injections_per_drive=args.injections_per_drive,
        window_seconds=args.window_seconds,
        lead_seconds=args.lead_seconds,
        grace_seconds=args.grace_seconds,
        cache_dir=args.cache_dir,
        use_score_cache=not args.no_score_cache,
        derived_dir=args.derived_dir,
        workers=args.workers,
        scoring_threads=args.scoring_threads,
        seed=args.seed,
        parity_rows=args.parity_rows,
        budget=args.budget,
        since=args.since,
        until=args.until,
        cache_only=args.cache_only,
        shift_test=not args.no_shift_test,
        shift_rows=args.shift_rows,
        harness_files=_harness_files(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # P02.5: the code and the config file are hashed first, before the config
    # is parsed or any module does work, so the key they give is what this
    # run executes; ``changed_key_inputs`` re-checks both later.
    early = {
        "code": code_version(_harness_files()),
        "config_file": file_sha256(Path(args.config)) if Path(args.config).is_file() else None,
    }
    configure_logging(args.log_level)
    # Before any torch work: the parity check and the untrained autoencoder's
    # calibration run here, and must match what the scoring workers compute.
    restore = pin_threads(args.scoring_threads)
    try:
        with ExitStack() as cleanup:
            return _evaluate(args, cleanup, early)
    finally:
        restore()


def _evaluate(args: argparse.Namespace, cleanup: ExitStack, early: Mapping[str, str | None]) -> int:
    """The phases of one evaluation, in order; this function prints, writes and exits.

    Each phase is a library function taking the request and the earlier phases'
    frozen results (``src.analysis.detector_evaluation_*``). A refusal is an
    :class:`EvaluationRefusedError`; the exit status and the stderr lines are its.
    """
    started = time.time()
    # The feature pipeline's per-session INFO chatter (R14: its loggers are the owner modules').
    for owner in (
        "feature_build",
        "feature_spec",
        "feature_transform",
        "feature_workers",
        "session_resampling",
    ):
        logging.getLogger(f"src.features.{owner}").setLevel(logging.WARNING)
    request = request_from_args(args)
    try:
        prepared = prepare_inputs(request, early)
        plan = plan_drives(request, prepared)
        if args.plan:
            for line in render_plan(plan):
                print(line)
            return 0
        behavior = prepare_behavior(request, prepared)
        loaded = load_planned_sessions(request, plan)
    except EvaluationRefusedError as refusal:
        for line in refusal.lines:
            print(line, file=sys.stderr)
        return refusal.exit_code
    cache = ScoreCache(request.cache_dir / "scores" if request.use_score_cache else None)
    # Staged entries of a run that fails or is interrupted are dropped.
    cleanup.callback(cache.discard)
    keys = scoring_keys(request, prepared, loaded)
    detectors = assemble_detectors(request, prepared, plan, loaded, keys, behavior, cache)
    roles = load_fault_channel_roles(request.fault_roles_path)
    sweep = sweep_spec(request)
    tasks = plan_scoring_tasks(request, plan, loaded, keys)
    scored = score_planned_drives(
        request, prepared, behavior, detectors, keys, cache, roles, sweep, tasks
    )
    aggregate = aggregate_results(plan, loaded, scored, detectors.names)
    changed = settle_score_cache(cache, prepared.key_sources, prepared.inputs, started=started)
    if changed:
        print(
            "warning: score cache not updated: key inputs changed during the run: "
            + ", ".join(changed),
            file=sys.stderr,
        )
    report = build_report(
        request=request,
        prepared=prepared,
        behavior=behavior,
        plan=plan,
        loaded=loaded,
        detectors=detectors,
        roles=roles,
        aggregate=aggregate,
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        command=" ".join(sys.argv),
    )
    regression: dict[str, Any] | None = None
    if args.baseline is not None:
        try:
            baseline_report = json.loads(args.baseline.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: cannot read baseline report {args.baseline}: {exc}", file=sys.stderr)
            return 2
        regression = compare_to_baseline(
            report, baseline_report, baseline_path=args.baseline, tolerance=args.tolerance
        )
        report["regressions"] = regression
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    markdown = args.markdown or args.out.with_suffix(".md")
    markdown.write_text(render_markdown(report))
    print(f"wrote {args.out} and {markdown}")
    print(
        f"{len(aggregate.records)} held-out drive(s) scored, {len(aggregate.skipped)} skipped; "
        f"{len(aggregate.outcomes)} injection outcome(s)"
    )
    print(
        f"score cache: {aggregate.cache_hits} of {aggregate.cache_lookups} lookups read back"
        + ("" if cache.root is not None else " (--no-score-cache)")
    )
    if regression is not None:
        lines = regression_summary(regression)
        print(f"regression compare vs {args.baseline} (tolerance {args.tolerance:.0%}):")
        for line in lines or ["no regressions beyond tolerance"]:
            print(f"  {line}")
        if regression["regressed"]:
            print("error: regression(s) detected vs baseline", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
