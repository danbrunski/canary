#!/usr/bin/env python3
"""Retrain both anomaly detectors from recent sessions (Task 5.17).

Manual trigger in v1; cron later. The companion piece to `refit_behavior_models.py`:
runs the Isolation Forest trainer and the **configured sequence-detector**
trainer — the GRU-D LSTM or the TCN, selected by ``models.sequence_detector``
and overridable with ``--sequence-model`` — over the same session set, side by
side, in one process. Only the *selected* sequence backbone is trained, never
both. Both trainers are ``src.models.training_entry`` (R06.4); the holdout
pickers are ``src.models.evaluation``, and staging/publishing behind the
alert-budget gate is ``src.models.publish`` (R06.3).

Each trainer writes its own ``model.joblib`` (or ``model.pt``) +
``model.manifest.json`` under a dedicated output directory. The
``ModelManager`` (5.16) running in the daemon picks up the new artifacts on
its next poll — no daemon restart needed, hot-swap as long as the schema
matches.

Usage:

    .venv/bin/python scripts/retrain.py
    .venv/bin/python scripts/retrain.py --days 14
    .venv/bin/python scripts/retrain.py --skip-sequence   # IF-only refresh
    .venv/bin/python scripts/retrain.py --skip-if         # sequence-only refresh
    .venv/bin/python scripts/retrain.py --sequence-model tcn

Skips on failure: a broken sequence trainer doesn't prevent the IF artifact from
landing (or vice versa) — partial coverage beats none. Each trainer validates
inputs and raises with a useful message; we log + continue.

``main`` runs the phases below in order over one :class:`RetrainRun`; ``args``
is never written to (R06.3):

sessions → holdouts → frames (+ quarantine, feedback, disposition summary) →
stage → trainers → benchmark gate → alert-budget gate → publish → ledger →
report → exit code.
"""

from __future__ import annotations

import argparse
import dataclasses
import gc
import json
import logging
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from _cli import (
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        add_override_reason_arg,
        add_profile_arg,
        apply_since_boundary,
        load_scoped_config,
        parse_since_argument,
        resolve_sessions,
        scoped_models_dir,
    )
except ImportError:  # loaded as `scripts.retrain` — repo root on path, not scripts/
    from scripts._cli import (  # type: ignore[no-redef]
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        add_override_reason_arg,
        add_profile_arg,
        apply_since_boundary,
        load_scoped_config,
        parse_since_argument,
        resolve_sessions,
        scoped_models_dir,
    )

from src.alerts.feedback import DEFAULT_FEEDBACK_PATH, FeedbackStore
from src.analysis.training_disposition import classify_dispositions, compute_quarantine_mask
from src.artifact_hash import sha256_file_or_none
from src.config import CanaryConfig
from src.decode.accepted_baselines import AcceptedBaselines, load_accepted_baselines
from src.log import configure_logging
from src.models import training_entry
from src.models.alert_budget import (
    AlertBudgetCheck,
    alert_role_sources,
    check_alert_budget,
    evaluation_drives,
)
from src.models.benchmark_gate import (
    DEFAULT_TOLERANCE,
    diff_reports,
    regression_summary,
)
from src.models.evaluation import (
    MIN_CALIBRATION_HOLDOUT_SESSIONS,
    HoldoutSplit,
    holdout_by_drive,
    pick_calibration_holdouts,
    pick_evaluation_holdouts,
)
from src.models.golden_baselines import (
    GoldenBaselines,
    golden_baselines_path,
    read_golden_baselines,
)
from src.models.manifest import ManifestValidationError
from src.models.publish import (
    ISOLATION_FOREST_SOURCE,
    SEQUENCE_SOURCE,
    artifact_name,
    publish_staged,
    sequence_artifact_name,
    set_aside_refused,
    staging_dir,
    write_artifact_verdict,
)
from src.models.retrain_exit_codes import (
    ALERT_BUDGET_REFUSED,
    WINDOW_TOO_SMALL_FOR_HOLDOUT,
)
from src.models.retraining_policy import (
    RetrainingFeedbackReport,
    apply_retraining_feedback_policy,
)
from src.models.sequence_registry import SequenceTrainingBackend, resolve_sequence_backend
from src.models.sequence_training import (
    configure_torch_threads,
    default_training_device,
)
from src.models.training_entry import SEQUENCE_BACKBONES
from src.models.training_ledger import (
    DEFAULT_TRAINING_LEDGER_PATH,
    TrainingLedger,
    build_run_record_from_manifest,
)
from src.preprocessing.derived_store import on_the_pi, use_store
from src.preprocessing.feature_pipeline import (
    DEFAULT_FEATURE_CACHE_DIR,
    FeaturePolicy,
    build_feature_frame,
    candidate_feature_columns,
    disposition_screen_frames,
    resolve_feature_policy,
)
from src.preprocessing.quality_gate import apply_quarantine_gate

log = logging.getLogger(__name__)

#: A02's harness, run as its own process by the benchmark and alert-budget
#: gates. Scripts do not import each other (R06.4); this one is a CLI with a
#: JSON report.
EVALUATE_DETECTORS_SCRIPT = Path(__file__).resolve().with_name("evaluate_detectors.py")


@dataclass
class RetrainRun:
    """One retrain's resolved state, filled phase by phase.

    ``args`` stays exactly as parsed. ``main`` used to carry run state by
    writing onto it (``if_out``, ``sequence_out``, ``skip_if``,
    ``skip_sequence``, ``holdout_split``, ``seq_model_type``,
    ``sequence_model``), so what a flag *said* and what the run *did* were the
    same attribute, and ``if_out`` meant the staging directory for half the
    run and the served one for the rest (R06.3).
    """

    args: argparse.Namespace
    config: CanaryConfig
    #: ``lstm`` / ``tcn``: ``--sequence-model``, else ``models.sequence_detector``.
    sequence_model: str
    #: The served output directories: what the daemon's ModelManager watches.
    if_out: Path
    sequence_out: Path
    #: The flags, then also a model disabled in config (a no-op intent).
    skip_if: bool
    skip_sequence: bool
    #: Training sessions, after every holdout has taken its drives.
    sessions: list[Path] = field(default_factory=list)
    calibration_sessions: list[Path] = field(default_factory=list)
    evaluation_sessions: list[Path] = field(default_factory=list)
    holdout_split: HoldoutSplit | None = None
    #: Trainer name → ``ok`` / ``failed: …`` / ``skipped …`` / ``refused: …``.
    results: dict[str, str] = field(default_factory=dict)
    #: Where each alert-role source trains before the budget gate (A06.5). A
    #: source absent here trains straight into its served directory.
    staging: dict[str, Path] = field(default_factory=dict)

    @property
    def sequence_backend(self) -> SequenceTrainingBackend:
        return SEQUENCE_BACKBONES[self.sequence_model]

    @property
    def seq_model_type(self) -> str:
        return self.sequence_backend.model_type

    def served_dir(self, source: str) -> Path:
        return self.if_out if source == ISOLATION_FOREST_SOURCE else self.sequence_out

    def train_dir(self, source: str) -> Path:
        """Where ``source``'s trainer writes: its staging directory while the
        alert-budget gate is pending, else the served one."""
        return self.staging.get(source, self.served_dir(source))

    def result_name(self, source: str) -> str:
        return "isolation_forest" if source == ISOLATION_FOREST_SOURCE else self.seq_model_type


@dataclass
class TrainingInputs:
    """What the trainers are handed, built once for both (the frames phase)."""

    feature_policy: FeaturePolicy
    feature_kwargs: dict[str, Any]
    calibration_kwargs: dict[str, Any]
    if_calibration_kwargs: dict[str, Any]
    calibration_policy: dict[str, Any]
    quarantined_cells: dict[str, pd.Index] | None
    feedback_report: RetrainingFeedbackReport | None
    disposition_summary: dict[str, Any] | None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="retrain",
        description=(
            "Retrain the Isolation Forest and LSTM autoencoder over recent "
            "sessions, writing versioned artifacts. The daemon's ModelManager "
            "picks them up on its next poll without restart."
        ),
    )
    parser.add_argument(
        "sessions",
        nargs="*",
        type=Path,
        help="Session directories. Default: all under --sessions-root within --days.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path("data/sessions"),
        help="Where to discover sessions when none given explicitly.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Use sessions captured in the last N days. Default 30. --all overrides.",
    )
    parser.add_argument(
        "--all",
        dest="use_all_sessions",
        action="store_true",
        help="Override --days; use every session under --sessions-root.",
    )
    parser.add_argument(
        "--since",
        type=parse_since_argument,
        default=None,
        help=(
            "Sliding-window session selection (Task 3.1): only sessions captured "
            "at/after this date (YYYY-MM-DD or full ISO-8601), instead of --days's "
            "relative lookback. Wins over --days/--all when given. The window "
            "always reserves a calibration holdout (--no-calibration-holdout still "
            "opts out explicitly); a window too small to spare one refuses the run "
            f"(exit {WINDOW_TOO_SMALL_FOR_HOLDOUT}) rather than silently falling "
            "back to in-sample calibration — the posture a scheduled retrain needs."
        ),
    )
    parser.add_argument(
        "--since-boundary",
        action="store_true",
        help=(
            "Exclude sessions captured before the latest corpus_boundary event in "
            "the vehicle ledger (Task 3.5) — e.g. don't blend pre/post-mod behavior "
            "into one corpus. No-op if no boundary event is recorded."
        ),
    )
    add_common_args(parser)
    add_profile_arg(parser)
    add_bindings_arg(parser)
    parser.add_argument(
        "--if-out",
        type=Path,
        default=None,
        help="Output directory for the Isolation Forest artifact + manifest. "
        "Default: the (profile-scoped) <models_dir>/isolation_forest from --config.",
    )
    parser.add_argument(
        "--sequence-model",
        choices=sorted(SEQUENCE_BACKBONES),
        default=None,
        help="Sequence-detector backbone to retrain (default: models.sequence_detector).",
    )
    parser.add_argument(
        "--calibration-sessions",
        nargs="+",
        type=Path,
        default=None,
        metavar="SESSION",
        help="Held-out clean session(s) for the sequence detector's *out-of-sample* "
        "threshold calibration. Excluded from the training set automatically. "
        "Default: auto-hold-out the most-recent discovered session — in-sample "
        "calibration inflates cross-session false positives. Pass "
        "--no-calibration-holdout to disable.",
    )
    parser.add_argument(
        "--evaluation-sessions",
        nargs="+",
        type=Path,
        default=None,
        metavar="SESSION",
        help="Held-out session(s) the post-retrain benchmark gate scores (A02.1). "
        "Must be disjoint from --calibration-sessions — a session in both is refused. "
        "Default: the newest healthy drives left after calibration, grouped by "
        "physical drive.",
    )
    add_override_reason_arg(
        parser,
        "--accept-thin-calibration",
        help="Publish a sequence model whose calibration does not cover what it will "
        "score (fewer than three drives, a scored mode without its own band, a "
        "channel never observed), or an IF mode whose threshold rests on fewer than "
        "min_calibration_drives_per_mode drives (A04.4). Refused without this (A02.2); "
        "the reason and the violations are stamped into the manifest.",
    )
    parser.add_argument(
        "--no-calibration-holdout",
        action="store_true",
        help="Skip the automatic calibration holdout and calibrate sequence "
        "thresholds on the training windows (the pre-fix behavior; only useful "
        "when you have too few sessions to spare one for calibration).",
    )
    parser.add_argument(
        "--sequence-out",
        type=Path,
        default=None,
        help="Output directory for the sequence model artifact + manifest "
        "(default: models/<model_type>).",
    )
    parser.add_argument(
        "--skip-if",
        action="store_true",
        help="Skip the Isolation Forest trainer (sequence-only refresh).",
    )
    parser.add_argument(
        "--skip-sequence",
        action="store_true",
        help="Skip the sequence-detector trainer (IF-only refresh).",
    )
    parser.add_argument(
        "--device",
        default=default_training_device(),
        help="Sequence-model training device. Default: cuda when available (dev "
        "laptop), else cpu (Pi). Artifacts load device-agnostically at serve "
        "time, so GPU training is safe for the Pi deployment.",
    )
    parser.add_argument(
        "--feature-cache",
        type=Path,
        default=DEFAULT_FEATURE_CACHE_DIR,
        help=(
            f"Directory for cached model-ready feature frames. Default {DEFAULT_FEATURE_CACHE_DIR}."
        ),
    )
    parser.add_argument(
        "--no-feature-cache",
        dest="feature_cache",
        action="store_const",
        const=None,
        help="Disable feature-frame caching for this retrain.",
    )
    parser.add_argument(
        "--rebuild-feature-cache",
        action="store_true",
        help="Ignore any existing feature-frame cache entry and rewrite it.",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_TRAINING_LEDGER_PATH,
        help=(
            "Training-run ledger JSONL (Task 6.21). Each successful trainer's "
            "manifest is ingested as a run record. Default data/control/."
        ),
    )
    parser.add_argument(
        "--no-ledger",
        action="store_true",
        help="Skip writing the training-run ledger.",
    )
    parser.add_argument(
        "--feedback",
        type=Path,
        default=DEFAULT_FEEDBACK_PATH,
        help=(
            "Anomaly feedback JSONL for label-aware retraining. False positives "
            "are treated as reviewed clean baseline; true positives, ignored, "
            "and needs-review labels exclude their event windows."
        ),
    )
    parser.add_argument(
        "--no-feedback-policy",
        action="store_true",
        help="Do not apply operator feedback labels to the retraining frame.",
    )
    parser.add_argument(
        "--feedback-window-seconds",
        type=float,
        default=30.0,
        help=(
            "Fallback margin on each side of a labelled event timestamp when "
            "the event has no explicit window. Default 30 seconds."
        ),
    )
    parser.add_argument(
        "--job-id",
        default=None,
        help="Correlate ledger records with this control-plane job id.",
    )
    parser.add_argument(
        "--compute-target",
        default=None,
        choices=("laptop", "pi"),
        help=(
            "Which machine is running this job (Task 6.36) — the companion's "
            "JobController passes 'laptop', the Pi daemon's passes 'pi'. "
            "Recorded on the ledger record so the training UI can show/filter "
            "by where a model was actually trained."
        ),
    )
    parser.add_argument(
        "--skip-benchmark",
        action="store_true",
        help="Skip the post-retrain benchmark regression gate (Task 2.6; A02's "
        "evaluate_detectors protocol since R06.5).",
    )
    add_override_reason_arg(
        parser,
        "--accept-alert-budget",
        help=(
            "Publish an alert-role artifact that failed the alert-budget gate (A06.5: "
            "at most 1 alert per engine-hour, upper bound, on the evaluation drives). "
            "Needs a stated reason; it is recorded in the gate's report and in the "
            "verdict beside the artifact. The daemon still serves such an artifact as "
            "evidence: an override is not a passing verdict (operator decision "
            "2026-09-23). Evidence-role detectors (models.roles) are never gated."
        ),
    )
    parser.add_argument(
        "--skip-disposition-summary",
        action="store_true",
        help=(
            "Skip computing the Phase-1 disposition summary (regime gate -> "
            "quality gate -> four-tier classify, `scripts/disposition_report.py`'s "
            "screen) over this run's training sessions. On by default so a real "
            "retrain finally populates `TrainingRunRecord.dispositions` (Task 1.4) "
            "for the champion/challenger promotion gate's contamination sanity "
            "check (Task 3.3); best-effort either way, never fails the retrain."
        ),
    )
    parser.add_argument(
        "--skip-quarantine-exclusion",
        action="store_true",
        help=(
            "Skip masking Quarantine-tier cells out of the training matrix (Task 3.6, "
            "online-training-strategy.md). On by default so the disposition table's "
            "'Quarantine -> train: No' is real, not just reported; cell-level NaN "
            "through the same gap machinery the Reject tier already uses "
            "(`quality_gate.apply_quarantine_gate`), never a row drop."
        ),
    )
    parser.add_argument(
        "--analyses-dir",
        type=Path,
        default=Path("data/analyses"),
        help="Directory for the benchmark gate's evaluate_detectors reports "
        "(benchmark-<ts>.json, Task 2.6 / R06.5) and the alert-budget reports. Also "
        "where prior benchmark reports are discovered as the baseline when "
        "--benchmark-baseline isn't given.",
    )
    parser.add_argument(
        "--benchmark-baseline",
        type=Path,
        default=None,
        help="Baseline benchmark report JSON for the regression gate (default: the "
        "most recent data/analyses/benchmark-*.json, if any).",
    )
    parser.add_argument(
        "--benchmark-tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        # argparse runs `help % params` for every action (for %(default)s
        # etc.), so a literal "%" here must be doubled or it's parsed as a
        # (broken) format spec.
        help="Relative regression tolerance for the post-retrain benchmark gate "
        f"(default {DEFAULT_TOLERANCE * 100:.0f}%%).",
    )
    add_log_level_arg(parser)
    return parser.parse_args(argv)


def derived_dir_for(args: argparse.Namespace) -> Path | None:
    """The derived session frames' directory (P01.2): beside the feature-frame
    cache (so the defaults give ``data/cache/derived_sessions`` and a test's
    ``--feature-cache`` keeps them in its tree); none with ``--no-feature-cache``."""
    if args.feature_cache is None:
        return None
    return Path(args.feature_cache).parent / "derived_sessions"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # The trainers read each session's 1 Hz frame through the derived store
    # (feature_pipeline.resample_session_bounded -> active_store()).
    with use_store(derived_dir_for(args)):
        return _main(args)


def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    configure_torch_threads()
    # Scoped once, here (P02.11): every trainer, the feature frames and the
    # accepted baselines read the vehicle's data, as the daemon does.
    config = load_scoped_config(args, args.profile)
    run = _start_run(args, config)

    if run.skip_if and run.skip_sequence:
        print(
            "error: --skip-if and --skip-sequence together leave nothing to do.",
            file=sys.stderr,
        )
        return 1
    for phase in (_select_sessions, _hold_out):
        code = phase(run)
        if code is not None:
            return code

    log.info(
        "starting retrain",
        extra={
            "sessions": len(run.sessions),
            "calibration_sessions": [str(s) for s in run.calibration_sessions],
            "evaluation_sessions": [str(s) for s in run.evaluation_sessions],
            "skip_if": run.skip_if,
            "skip_sequence": run.skip_sequence,
            "sequence_model": run.seq_model_type,
            "device": args.device,
        },
    )
    _skip_disabled(run)

    inputs = _prepare_inputs(run)
    if isinstance(inputs, int):
        return inputs

    # What sat in each served dir BEFORE this run. `_ingest_ledger` refuses a
    # manifest that is byte-for-byte the one already there: see its docstring.
    manifests_before = {
        out_dir: _manifest_fingerprint(out_dir) for out_dir in (run.if_out, run.sequence_out)
    }
    budget_gated = _stage_alert_role_outputs(run)
    # Served dirs of trainers that produced a fresh artifact this run. Ledger
    # ingestion waits for the benchmark gate so each record can link the report
    # the gate produced against that artifact.
    ledger_pending = _run_trainers(run, inputs)

    benchmark_report_path: Path | None = None
    benchmark_diff: dict[str, Any] | None = None
    if not args.skip_benchmark:
        # A02.1: evaluation sessions only — never the ones thresholds were cut from.
        benchmark_report_path, benchmark_diff = _run_benchmark_gate(
            config, run.evaluation_sessions, run
        )
    published = [
        source
        for source in (ISOLATION_FOREST_SOURCE, SEQUENCE_SOURCE)
        if run.results.get(run.result_name(source)) == "ok"
    ]
    budget_check = _run_alert_budget_gate(config, run.evaluation_sessions, run, published)
    refused_dirs = _publish_or_refuse(run, budget_check, budget_gated, published)

    ledger = None if args.no_ledger else TrainingLedger(args.ledger)
    for out_dir in ledger_pending:
        refused_at = refused_dirs.get(out_dir)
        _ingest_ledger(
            ledger,
            refused_at if refused_at is not None else out_dir,
            "failed" if refused_at is not None else "succeeded",
            args.job_id,
            feedback_report=inputs.feedback_report,
            detector_benchmark_report_path=benchmark_report_path,
            dispositions=inputs.disposition_summary,
            compute_target=args.compute_target,
            manifest_before=manifests_before.get(out_dir),
        )

    _print_report(
        run,
        feedback_report=inputs.feedback_report,
        benchmark_report_path=benchmark_report_path,
        benchmark_diff=benchmark_diff,
        budget_check=budget_check,
    )
    return _exit_code(run, budget_check, benchmark_diff, benchmark_report_path)


def _start_run(args: argparse.Namespace, config: CanaryConfig) -> RetrainRun:
    """Resolve the sequence backbone (config default, ``--sequence-model``
    override) and the served output directories (profile-scoped defaults)."""
    sequence_model = args.sequence_model or config.models.sequence_detector
    models_dir = scoped_models_dir(config, args.profile)
    return RetrainRun(
        args=args,
        config=config,
        sequence_model=sequence_model,
        if_out=args.if_out or models_dir / "isolation_forest",
        sequence_out=args.sequence_out
        or models_dir / SEQUENCE_BACKBONES[sequence_model].model_type,
        skip_if=args.skip_if,
        skip_sequence=args.skip_sequence,
    )


def _select_sessions(run: RetrainRun) -> int | None:
    """Explicit sessions, else ``--days``/``--all``/``--since`` discovery, then
    ``--since-boundary``. An exit code when nothing is left."""
    args = run.args
    sessions = resolve_sessions(args, args.sessions_root)
    sessions = apply_since_boundary(args, run.config, sessions)
    if not sessions:
        print(
            f"error: no sessions under {args.sessions_root} within --days {args.days}. "
            "Pass session paths directly or use --all.",
            file=sys.stderr,
        )
        return 1
    run.sessions = list(sessions)
    return None


def _hold_out(run: RetrainRun) -> int | None:
    """Calibration and evaluation holdouts, by physical drive (A02.1).

    Out-of-sample threshold calibration is the default: in-sample calibration
    underestimates cross-session error and floods false positives on unseen
    drives. Explicit ``--calibration-sessions`` wins; otherwise
    :func:`~src.models.evaluation.pick_calibration_holdouts` picks healthy,
    substantial sessions for mode coverage (abandoned in-progress captures
    don't have the rows to make a useful band). With fewer than 3 sessions we
    can't afford to hold one out, so we log and fall back to in-sample. The
    same holdout goes to every enabled detector.

    The regression gate scores sessions disjoint from calibration, grouped by
    physical drive. ``holdout_by_drive`` expands every named or picked session
    to its whole drive and refuses a drive on both sides; the evaluation
    drives come from what calibration left, keeping at least two sessions for
    training. An exit code on a refusal.
    """
    args = run.args
    sessions = run.sessions
    pool = list(sessions)
    calibration_sessions: list[Path] = []
    evaluation_sessions: list[Path] = []
    if args.calibration_sessions and args.evaluation_sessions:
        try:
            HoldoutSplit(
                calibration=tuple(Path(p).name for p in args.calibration_sessions),
                evaluation=tuple(Path(p).name for p in args.evaluation_sessions),
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.calibration_sessions:
        requested = {Path(p).resolve() for p in args.calibration_sessions}
        calibration_sessions = [s for s in sessions if Path(s).resolve() in requested]
        sessions = [s for s in sessions if Path(s).resolve() not in requested]
    elif not args.no_calibration_holdout and len(sessions) >= 3:
        # Hold out up to 3 sessions for mode coverage, but always keep
        # at least 2 for training.
        holdouts = pick_calibration_holdouts(sessions, max_holdouts=min(3, len(sessions) - 2))
        if holdouts:
            calibration_sessions = holdouts
            held = set(holdouts)
            sessions = [s for s in sessions if s not in held]
        else:
            log.warning(
                "no substantial session available for out-of-sample "
                "calibration; falling back to in-sample",
                extra={"session_count": len(sessions)},
            )
    elif not args.no_calibration_holdout:
        log.warning(
            "too few sessions for an out-of-sample calibration holdout; "
            "falling back to in-sample (cross-session FPR likely inflated)",
            extra={"session_count": len(sessions)},
        )

    try:
        after_calibration = holdout_by_drive(pool, calibration=calibration_sessions)
        if args.evaluation_sessions:
            requested = {Path(p).resolve() for p in args.evaluation_sessions}
            evaluation_sessions = [s for s in pool if Path(s).resolve() in requested]
        elif (
            calibration_sessions
            and not args.no_calibration_holdout
            and len(after_calibration.training) > 2
        ):
            evaluation_sessions = pick_evaluation_holdouts(
                after_calibration.training,
                max_sessions=len(after_calibration.training) - 2,
            )
        holdout = holdout_by_drive(
            pool, calibration=calibration_sessions, evaluation=evaluation_sessions
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    moved = sorted(
        {s.name for s in (*holdout.calibration, *holdout.evaluation)}
        - {Path(s).name for s in (*calibration_sessions, *evaluation_sessions)}
    )
    if moved:
        log.info("holdouts take the rest of their drives", extra={"sessions": moved})
    run.sessions = list(holdout.training)
    run.calibration_sessions = list(holdout.calibration)
    run.evaluation_sessions = list(holdout.evaluation)
    run.holdout_split = holdout.split

    # Task 3.1: a --since window is explicitly the scheduled-retrain shape (a
    # rolling window, not a fixed corpus). The calibration-holdout fallback
    # above (warn + train in-sample) is the right posture for an ad hoc manual
    # --days run with a thin corpus, but a *scheduled* run must never silently
    # degrade to in-sample calibration (that's exactly the FPR≈1.0 flood the
    # ml-hardening review traced to in-sample calibration). Refuse instead,
    # with a distinct exit code so a caller (the online-training scheduler,
    # 3.2) can log this as a routine skip rather than a hard failure.
    if (
        args.since is not None
        and not run.skip_sequence
        and not args.no_calibration_holdout
        and not run.calibration_sessions
    ):
        print(
            f"error: --since window ({len(run.sessions)} session(s)) is too small to "
            "reserve a calibration holdout; a scheduled retrain never calibrates "
            "in-sample. Widen --since, pass explicit --calibration-sessions, or "
            "(manual runs only) --no-calibration-holdout.",
            file=sys.stderr,
        )
        return WINDOW_TOO_SMALL_FOR_HOLDOUT
    return None


def _skip_disabled(run: RetrainRun) -> None:
    """A model disabled in config is "skipped", not "failed": its trainer
    would raise ``_validate_config`` ("... config is disabled"), which is a
    no-op intent, not an error. Treated like an implicit ``--skip-<m>`` so a
    disabled trainer doesn't poison the exit code, the report, or the
    all-failed check (and sync-train.sh still runs the behavior refit)."""
    models = run.config.models
    if not run.skip_if and not models.isolation_forest.enabled:
        log.info("isolation_forest disabled in config; skipping")
        run.results["isolation_forest"] = "skipped (disabled in config)"
        run.skip_if = True
    if not run.skip_sequence and not getattr(models, run.seq_model_type).enabled:
        log.info("%s disabled in config; skipping", run.seq_model_type)
        run.results[run.seq_model_type] = "skipped (disabled in config)"
        run.skip_sequence = True


def _prepare_inputs(run: RetrainRun) -> TrainingInputs | int:
    """The shared training and calibration frames, screened once for both
    trainers: quarantine exclusion, the feedback-label policy, the IF's
    calibration policy and the disposition summary. An exit code (after
    printing the report) when a shared step fails."""
    args, config = run.args, run.config
    sessions = run.sessions
    calibration_sessions = run.calibration_sessions
    feedback_labels = {} if args.no_feedback_policy else FeedbackStore(args.feedback).labels()
    any_trainer_requested = not run.skip_if or not run.skip_sequence
    # The shared frame is where the quarantine screen and the feedback policy
    # are applied, so it is built whenever either applies -- not only when both
    # trainers run. An IF-only retrain (--skip-sequence) used to hand the
    # trainer an unscreened frame: the 2026-09-23 A06.3 candidate IF lost 0
    # rows to quarantine where the same corpus, both trainers, lost 369 of 709.
    needs_shared_feature_frame = any_trainer_requested and (
        (not run.skip_if and not run.skip_sequence)
        or bool(feedback_labels)
        or not args.skip_quarantine_exclusion
    )
    shared_feature_frame = None
    feedback_report: RetrainingFeedbackReport | None = None
    if needs_shared_feature_frame:
        try:
            shared_feature_frame = build_feature_frame(
                sessions,
                config,
                bindings_path=args.bindings,
                cache_dir=args.feature_cache,
                force_rebuild=args.rebuild_feature_cache,
            )
        except Exception as exc:  # noqa: BLE001 - preprocessing is shared by both trainers
            log.exception("feature frame build failed", extra={"error": str(exc)})
            _mark_requested_failures(run, f"failed: preprocessing: {exc}")
            _print_report(run)
            return 1

    # One calibration frame for every detector, built HERE — before either
    # trainer holds its matrices. Each trainer used to rebuild it privately
    # (uncached), so the sequence trainer decoded the holdout sessions a second
    # time with the full window dataset resident; on 2026-09-20 that second
    # build died silently twice and the LSTM was re-run on ONE holdout while
    # the IF kept three. Sharing the frame makes "same holdout set, pooled"
    # true by construction rather than by two code paths agreeing.
    calibration_kwargs: dict[str, Any] = {}
    if calibration_sessions and any_trainer_requested:
        try:
            calibration_kwargs["calibration_frame"] = build_feature_frame(
                calibration_sessions,
                config,
                bindings_path=args.bindings,
                cache_dir=args.feature_cache,
                force_rebuild=args.rebuild_feature_cache,
            )
        except Exception as exc:  # noqa: BLE001 - shared by both trainers
            log.exception("calibration feature frame build failed", extra={"error": str(exc)})
            _mark_requested_failures(run, f"failed: calibration preprocessing: {exc}")
            _print_report(run)
            return 1
    if calibration_sessions and len(calibration_sessions) < MIN_CALIBRATION_HOLDOUT_SESSIONS:
        log.warning(
            "calibrating on %d held-out session(s); %d are needed for thresholds that "
            "reflect more than one drive's conditions (recorded in the manifests)",
            len(calibration_sessions),
            MIN_CALIBRATION_HOLDOUT_SESSIONS,
            extra={"calibration_sessions": [s.name for s in calibration_sessions]},
        )

    # Trainability policy (binding confidence / model_feature opt-outs, DIDs the
    # serving profile actually polls) resolved once against the SAME vehicle
    # profile the artifacts are written for.
    feature_policy = resolve_feature_policy(
        config, profile_slug=args.profile, bindings_path=args.bindings
    )

    quarantine_excluded: dict[str, int] | None = None
    quarantined_cells: dict[str, pd.Index] | None = None
    if shared_feature_frame is not None and not args.skip_quarantine_exclusion:
        shared_feature_frame, quarantine_excluded, quarantined_cells = _apply_quarantine_exclusion(
            shared_feature_frame, config, ledger_path=args.ledger
        )

    if shared_feature_frame is not None and feedback_labels:
        try:
            shared_feature_frame, feedback_report = apply_retraining_feedback_policy(
                shared_feature_frame,
                feedback_labels,
                fallback_margin_seconds=args.feedback_window_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - label policy is a retrain gate
            log.exception("feedback label policy failed", extra={"error": str(exc)})
            _mark_requested_failures(run, f"failed: feedback policy: {exc}")
            _print_report(run, feedback_report=feedback_report)
            return 1
        if shared_feature_frame.empty:
            msg = "feedback policy excluded every training row"
            log.error(msg)
            _mark_requested_failures(run, f"failed: {msg}")
            _print_report(run, feedback_report=feedback_report)
            return 1
        log.info(
            "applied feedback retraining policy",
            extra=feedback_report.to_dict(),
        )

    # A04.3: the IF's calibration rows go through the same feedback-label
    # policy as training — an operator-labelled anomaly is not nominal, and a
    # threshold cut over it is too high. Quarantine is deliberately NOT
    # applied: serve-time scoring never quarantines, and a threshold must be
    # cut on the population the served detector actually scores. Both facts
    # are recorded in the IF manifest (``calibration.policy``). The filtered
    # frame goes to the IF only: the sequence trainer keeps the frame it had,
    # because its manifest has nowhere to record the policy (yet).
    if_calibration_kwargs = dict(calibration_kwargs)
    calibration_policy: dict[str, Any] = {
        "quarantine": {
            "applied": False,
            "reason": "serve-time scoring does not quarantine; thresholds are cut on the "
            "population the served detector scores",
        },
        "feedback_labels": {"applied": False, "reason": "no feedback labels"},
    }
    if calibration_kwargs.get("calibration_frame") is not None and feedback_labels:
        try:
            if_calibration_kwargs["calibration_frame"], calibration_feedback = (
                apply_retraining_feedback_policy(
                    calibration_kwargs["calibration_frame"],
                    feedback_labels,
                    fallback_margin_seconds=args.feedback_window_seconds,
                )
            )
        except Exception as exc:  # noqa: BLE001 - label policy is a retrain gate
            log.exception("calibration feedback label policy failed", extra={"error": str(exc)})
            _mark_requested_failures(run, f"failed: calibration feedback policy: {exc}")
            _print_report(run, feedback_report=feedback_report)
            return 1
        calibration_policy["feedback_labels"] = {
            "applied": True,
            "excluded_rows": calibration_feedback.excluded_rows,
            "excluded_labels": calibration_feedback.excluded_labels,
        }

    feature_kwargs = (
        {"feature_frame": shared_feature_frame}
        if shared_feature_frame is not None
        else {
            "feature_cache_dir": args.feature_cache,
            "rebuild_feature_cache": args.rebuild_feature_cache,
        }
    )

    disposition_summary: dict[str, Any] | None = None
    if not args.skip_disposition_summary and any_trainer_requested:
        disposition_summary = _compute_disposition_summary(
            sessions, config, bindings_path=args.bindings
        )
    if quarantine_excluded is not None and disposition_summary is not None:
        # Task 3.6: per-channel counts of cells actually masked out of the
        # training matrix, alongside the informational screen above (the
        # two are computed from different frame representations — see
        # `_apply_quarantine_exclusion`'s docstring — so this is reported
        # as its own key rather than folded into `totals`).
        disposition_summary["excluded"] = quarantine_excluded

    return TrainingInputs(
        feature_policy=feature_policy,
        feature_kwargs=feature_kwargs,
        calibration_kwargs=calibration_kwargs,
        if_calibration_kwargs=if_calibration_kwargs,
        calibration_policy=calibration_policy,
        quarantined_cells=quarantined_cells,
        feedback_report=feedback_report,
        disposition_summary=disposition_summary,
    )


def _stage_alert_role_outputs(run: RetrainRun) -> set[str]:
    """A06.5: an alert-role artifact trains into a staging directory and
    reaches the served one only through the alert-budget gate, so the daemon's
    ModelManager can never hot-swap an artifact the gate then refuses. Returns
    the gated sources."""
    gated = set(
        alert_role_sources(
            run.config.models.roles.by_source(),
            [
                source
                for source, skipped in (
                    (ISOLATION_FOREST_SOURCE, run.skip_if),
                    (SEQUENCE_SOURCE, run.skip_sequence),
                )
                if not skipped
            ],
        )
    )
    for source in sorted(gated):
        run.staging[source] = staging_dir(run.served_dir(source))
    return gated


def _run_trainers(run: RetrainRun, inputs: TrainingInputs) -> list[Path]:
    """Each requested trainer in turn; one failing does not stop the other.
    Returns the served directories of trainers that produced an artifact."""
    args, config = run.args, run.config
    calibration_sessions = run.calibration_sessions or None
    evaluation_sessions = run.evaluation_sessions or None
    produced: list[Path] = []
    if not run.skip_if:
        try:
            training_entry.train_isolation_forest(
                run.sessions,
                config,
                run.train_dir(ISOLATION_FOREST_SOURCE),
                bindings_path=args.bindings,
                calibration_sessions=calibration_sessions,
                feature_policy=inputs.feature_policy,
                evaluation_sessions=evaluation_sessions,
                quarantined_cells=inputs.quarantined_cells,
                calibration_policy=inputs.calibration_policy,
                calibration_override=args.accept_thin_calibration,
                **inputs.if_calibration_kwargs,
                **inputs.feature_kwargs,
            )
            run.results["isolation_forest"] = "ok"
            produced.append(run.if_out)
        except Exception as exc:  # noqa: BLE001 — surface + continue to the next trainer
            log.exception("isolation forest trainer failed", extra={"error": str(exc)})
            run.results["isolation_forest"] = f"failed: {exc}"

        # The sequence trainer needs the shared feature frame, but none of the
        # IF mode matrices. Reclaim cyclic/joblib temporaries before allocating
        # the LSTM dataset's values/mask/delta arrays.
        gc.collect()

    if not run.skip_sequence:
        try:
            training_entry.train_sequence_detector(
                run.sequence_backend,
                run.sessions,
                config,
                run.train_dir(SEQUENCE_SOURCE),
                bindings_path=args.bindings,
                device=args.device,
                calibration_sessions=calibration_sessions,
                feature_policy=inputs.feature_policy,
                evaluation_sessions=evaluation_sessions,
                calibration_override=args.accept_thin_calibration,
                **inputs.calibration_kwargs,
                **inputs.feature_kwargs,
            )
            run.results[run.seq_model_type] = "ok"
            produced.append(run.sequence_out)
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "sequence trainer failed", extra={"error": str(exc), "model": run.seq_model_type}
            )
            run.results[run.seq_model_type] = f"failed: {exc}"
    return produced


def _publish_or_refuse(
    run: RetrainRun,
    budget_check: AlertBudgetCheck,
    gated: set[str],
    published: Sequence[str],
) -> dict[Path, Path]:
    """Every staged artifact either reaches its served directory or is set
    aside beside it. Returns ``{served dir: refused dir}`` for the refused.

    The verdict travels with the binary (operator decision 2026-09-23): models
    reach the Pi by scp and data/analyses never does, so the daemon serves an
    `alert` role only beside an accepted verdict that names this artifact's
    sha256 (src/models/role_gate.py).
    """
    refused_dirs: dict[Path, Path] = {}
    for source in sorted(gated):
        staged = run.train_dir(source)
        served = run.served_dir(source)
        if source not in published:
            # The trainer failed: nothing to publish or keep.
            if staged.exists():
                shutil.rmtree(staged)
            continue
        write_artifact_verdict(
            budget_check, source, staged / artifact_name(run.config, source, run.seq_model_type)
        )
        if budget_check.refused:
            refused_dirs[served] = set_aside_refused(staged, served)
            run.results[run.result_name(source)] = (
                "refused: alert budget (see the alert-budget report)"
            )
        else:
            publish_staged(staged, served)
    # Every later step (the report, the ledger) names the served paths.
    run.staging.clear()
    return refused_dirs


def _exit_code(
    run: RetrainRun,
    budget_check: AlertBudgetCheck,
    benchmark_diff: dict[str, Any] | None,
    benchmark_report_path: Path | None,
) -> int:
    """Partial success is the documented contract (a broken LSTM must not
    block the IF artifact, and vice versa). Non-zero ONLY when every requested
    trainer failed, the alert budget refused, or the benchmark regressed, so
    the caller (sync-train.sh) still runs the independent behavior-model refit
    instead of `set -e` aborting the whole pipeline on a partial run."""
    if budget_check.refused:
        # A06.5: a refusal, not a report. The refused artifact sits in a
        # ``_refused-alert-budget-*`` directory beside the served one.
        log.error(budget_check.message())
        print(f"error: {budget_check.message()}", file=sys.stderr)
        return ALERT_BUDGET_REFUSED
    if budget_check.override_reason is not None and budget_check.violations:
        log.warning(
            "alert budget not met; published under --accept-alert-budget %r: %s",
            budget_check.override_reason,
            "; ".join(budget_check.violations),
        )
    results = run.results
    succeeded = [k for k, v in results.items() if v == "ok"]
    failed = [k for k, v in results.items() if v.startswith("failed")]
    skipped = [k for k, v in results.items() if v.startswith("skipped")]
    if failed and not succeeded:
        log.error("all attempted trainers failed", extra={"results": results})
        return 1
    if not succeeded and not failed:
        # Everything was skipped (disabled in config / --skip flags) — a
        # legitimate no-op, not a failure; sync-train.sh proceeds to refit.
        log.warning("no trainers ran (all skipped: %s)", ", ".join(skipped) or "none")
        return 0
    if benchmark_diff is not None and benchmark_diff["regressed"]:
        # The benchmark gate is the pre-deploy check (Task 2.6): a regression
        # is a hard failure of this retrain run regardless of trainer
        # partial-success, distinct from "a trainer errored" above — the
        # artifacts trained fine, they're just worse than the baseline.
        log.error(
            "post-retrain benchmark regression detected vs baseline",
            extra={"report": str(benchmark_report_path)},
        )
        return 1
    if failed:
        log.warning(
            "partial retrain — ok: %s; failed: %s%s",
            ", ".join(succeeded),
            ", ".join(failed),
            f"; skipped: {', '.join(skipped)}" if skipped else "",
        )
    return 0


def _apply_quarantine_exclusion(
    feature_frame: pd.DataFrame,
    config: CanaryConfig,
    *,
    ledger_path: Path,
) -> tuple[pd.DataFrame, dict[str, int], dict[str, pd.Index]]:
    """Mask Quarantine-tier cells out of the actual training matrix (Task 3.6,
    online-training-strategy.md).

    The disposition report has screened this corpus since Phase 1 (1.4/1.5)
    without ever acting on what it found — the table's "Quarantine ->
    train: No" was aspirational until now. This screens ``feature_frame``
    directly (the frame the trainers are about to fit on, not a second,
    separately-decoded representation of the same sessions — no
    cross-pipeline cell alignment to get wrong) and excludes matching cells
    the same cell-level way the Reject tier already does
    (``quality_gate.apply_quarantine_gate``): NaN, never a row drop.

    Golden-anchored (Task 3.7): when the isolation-forest model type has a
    declared golden run with frozen baselines on disk, screens against
    those instead of this window's own statistics — the fix for the
    self-referential blind spot the 2026-07-20 review identified. Falls
    back to window-local screening (Phase 1 behavior) when no golden
    baselines are on disk, exactly like ``classify_dispositions`` always
    has.

    Best-effort: any failure (schema surprise, empty frame) returns
    ``feature_frame`` unchanged with empty counts rather than blocking the
    retrain — masking bad cells is a quality improvement, not something the
    retrain can't proceed without.
    """
    try:
        overrides: AcceptedBaselines | None = None
        if config.daemon.accepted_baselines.exists():
            overrides = load_accepted_baselines(config.daemon.accepted_baselines)

        golden_baselines: GoldenBaselines | None = None
        sidecar = golden_baselines_path(ledger_path, "isolation_forest")
        golden_baselines = read_golden_baselines(sidecar)

        channels = candidate_feature_columns(feature_frame)
        mask = compute_quarantine_mask(
            feature_frame,
            channels=channels,
            overrides=overrides,
            golden_baselines=golden_baselines,
        )
        masked_frame, result = apply_quarantine_gate(feature_frame, mask)
        if result.reject_counts:
            log.info(
                "quarantine exclusion applied to training matrix",
                extra={"excluded_counts": result.reject_counts},
            )
        # A04.3: which cells, so the IF can count the rows they cost per mode.
        cells = {str(c): mask.index[mask[c].to_numpy()] for c in mask.columns if mask[c].any()}
        return masked_frame, dict(result.reject_counts), cells
    except Exception as exc:  # noqa: BLE001 — best-effort, see docstring
        log.warning("quarantine exclusion skipped: %s", exc)
        return feature_frame, {}, {}


def _compute_disposition_summary(
    sessions: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path,
) -> dict[str, Any] | None:
    """Best-effort disposition summary for this run's training sessions.

    The same regime-gate -> data-quality-gate -> four-tier classify screen
    `scripts/disposition_report.py` runs standalone (reused via
    `feature_pipeline.disposition_screen_frames`, not reimplemented), applied
    here so a real retrain run finally populates `TrainingRunRecord.dispositions`
    (Task 1.4 added the schema field with the note "populated in Phase 3"; this is that wiring).
    Feeds the champion/challenger promotion gate's contamination sanity check
    (Task 3.3).

    Never raises: a screen failure (e.g. an unreadable session, a decode
    error) must not block the retrain itself — it just means this run's
    ledger record carries `dispositions=None` (unverifiable, not itself a red
    flag; `build_run_record_from_manifest`'s docstring already documents that
    absence).
    """
    try:
        overrides = None
        if config.daemon.accepted_baselines.exists():
            overrides = load_accepted_baselines(config.daemon.accepted_baselines)
        framed_parts, reject_counts = disposition_screen_frames(
            list(sessions), config, bindings_path=bindings_path
        )
        if not framed_parts:
            return None
        combined = pd.concat(framed_parts, ignore_index=True)
        channels = candidate_feature_columns(combined)
        summary = classify_dispositions(
            combined, reject_counts=reject_counts, channels=channels, overrides=overrides
        )
        return summary.to_dict()
    except Exception as exc:  # noqa: BLE001 — best-effort, see docstring
        log.warning("disposition summary skipped: %s", exc)
        return None


def _manifest_fingerprint(out_dir: Path) -> str | None:
    """Content hash of ``out_dir``'s manifest, ``None`` when there is none."""
    return sha256_file_or_none(out_dir / "model.manifest.json")


def _ingest_ledger(
    ledger: TrainingLedger | None,
    out_dir: Path,
    status: str,
    job_id: str | None,
    *,
    feedback_report: RetrainingFeedbackReport | None = None,
    detector_benchmark_report_path: Path | None = None,
    dispositions: dict[str, Any] | None = None,
    compute_target: str | None = None,
    manifest_before: str | None = None,
) -> None:
    """Best-effort: record the trainer's manifest in the training ledger.

    Never fails the retrain — a missing/unreadable manifest (e.g. a stubbed
    trainer in tests, or a trainer that crashed after reporting ok) is logged
    and skipped. The ledger is an index, not a gate.

    ``manifest_before`` is the out_dir manifest's fingerprint from *before* the
    trainer ran. If the manifest is unchanged, this run did not produce it, and
    it is refused: a trainer that returns without writing (stubbed in a test,
    or short-circuited) used to get the PREVIOUS run's manifest re-ingested as
    a fresh ``succeeded`` record. That is how the deployed 2026-09-20 models
    acquired ledger rows whose ``detector_benchmark_report_path`` pointed into
    a pytest tmp dir — and since the latest line per ``run_id`` wins, each such
    row also reset the real run's ``publish_status``/``approved`` to defaults.
    """
    if ledger is None:
        return
    manifest_path = out_dir / "model.manifest.json"
    if not manifest_path.exists():
        log.debug("no manifest at %s; skipping ledger ingest", manifest_path)
        return
    if manifest_before is not None and _manifest_fingerprint(out_dir) == manifest_before:
        log.warning(
            "ledger ingest refused: %s was not written by this run (the trainer "
            "reported ok without producing a manifest); not re-recording a previous run",
            manifest_path,
        )
        return
    try:
        record = build_run_record_from_manifest(
            manifest_path,
            status="succeeded" if status == "succeeded" else "failed",
            job_id=job_id,
            detector_benchmark_report_path=detector_benchmark_report_path,
            dispositions=dispositions,
            compute_target=compute_target,
        )
        if feedback_report is not None:
            record = record.model_copy(
                update={
                    "metrics": {
                        **record.metrics,
                        "feedback_policy": feedback_report.to_dict(),
                    }
                }
            )
        ledger.append(record)
        log.info("recorded training run", extra={"run_id": record.run_id})
    except (ManifestValidationError, OSError) as exc:
        log.warning("ledger ingest skipped: %s", exc)


def _prior_benchmark_reports(analyses_dir: Path) -> list[Path]:
    """Prior benchmark reports, newest first — the gate diffs against the
    newest one of the same drives under the same protocol parameters
    (`benchmark_gate.incomparable_reason`). Grid-era reports (retired, R06.5)
    share the name and are passed over as incomparable, by name."""
    if not analyses_dir.is_dir():
        return []
    return sorted(analyses_dir.glob("benchmark-*.json"), reverse=True)


def _run_benchmark_gate(
    config: CanaryConfig,
    evaluation_sessions: Sequence[Path],
    run: RetrainRun,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Post-retrain regression gate (Task 2.6): score the freshly-trained
    artifacts on the evaluation holdout with A02's protocol
    (``scripts/evaluate_detectors.py``: realistic faults swept over severities,
    false alarms per engine-hour; R06.5 retired the synthetic-fault grid it
    used to run) and diff against the newest comparable prior report. Returns
    ``(report_path, diff)``, or ``(None, None)`` when the gate didn't run.

    Scores the *evaluation* holdout only (A02.1). It used to score the
    calibration sessions — the sessions the thresholds were cut from — so the
    sequence detector's clean-alert rate read ~0.1 % by construction while it
    flagged 38-75 % of windows on unseen drives. No evaluation holdout (a
    corpus too small to spare one after calibration, or
    ``--no-calibration-holdout``) means no honest benchmark, so the gate is
    skipped — logged, not a failure — rather than silently testing on
    calibration or training data.

    Scores the artifacts where they were trained (the staging directory of an
    alert-role source, before the budget gate publishes it), with the
    sequence artifact's own backend (LSTM or TCN, from its manifest).

    Never raises: like the trainers themselves, an unexpected failure here is
    logged and treated as "gate didn't run", not a retrain-flow crash.
    """
    args = run.args
    if not evaluation_sessions:
        log.warning(
            "no evaluation holdout for the post-retrain benchmark gate; skipping "
            "(the gate never scores calibration sessions; see --evaluation-sessions)"
        )
        return None, None

    if_model_path = run.train_dir(ISOLATION_FOREST_SOURCE) / "model.joblib"
    if not if_model_path.exists():
        log.warning("no isolation forest artifact at %s; skipping benchmark gate", if_model_path)
        return None, None

    # The artifact this run wrote (profile-scoped), never the unscoped default:
    # the 2026-07-26 retrain looked in the pre-migration location, missed, and
    # produced a benchmark with no LSTM coverage at all. A sequence detector
    # disabled in config is not scored, as before.
    written = run.train_dir(SEQUENCE_SOURCE) / sequence_artifact_name(config, run.seq_model_type)
    sequence_path = (
        written if resolve_sequence_backend(config) is not None and written.exists() else None
    )
    try:
        # Listed before this run writes its own report into the same directory.
        candidates = (
            [Path(args.benchmark_baseline)]
            if args.benchmark_baseline is not None
            else _prior_benchmark_reports(args.analyses_dir)
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        args.analyses_dir.mkdir(parents=True, exist_ok=True)
        report_path = args.analyses_dir / f"benchmark-{timestamp}.json"
        report = _evaluate_on_drives(
            evaluation_sessions,
            run,
            if_model=if_model_path,
            sequence_model=sequence_path,
            out=report_path,
            injections=True,
        )
        split = run.holdout_split
        report["holdout"] = (
            split.to_dict()
            if split is not None
            else {"calibration": [], "evaluation": [s.name for s in evaluation_sessions]}
        )

        diff: dict[str, Any] | None = None
        first_reason: str | None = None
        for baseline_path in candidates:
            try:
                baseline_report = json.loads(Path(baseline_path).read_text())
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("benchmark gate: cannot read baseline %s: %s", baseline_path, exc)
                continue
            attempt = diff_reports(baseline_report, report, tolerance=args.benchmark_tolerance)
            if attempt.get("comparable", True):
                diff = {**attempt, "baseline": str(baseline_path)}
                break
            first_reason = first_reason or f"{Path(baseline_path).name}: {attempt['reason']}"
        if diff is None and first_reason is not None:
            # A02: a baseline from other drives, another protocol or the
            # retired grid is not a regression check; say so and do not fail
            # the retrain on it.
            log.warning(
                "benchmark gate: no comparable prior report (%s); nothing to regress against",
                first_reason,
            )
            diff = {
                "tolerance": args.benchmark_tolerance,
                "regressed": False,
                "comparable": False,
                "reason": first_reason,
                "cells": [],
            }
        if diff is not None:
            report["regressions"] = diff
            for line in regression_summary(diff) if diff["regressed"] else ():
                log.error("benchmark gate: %s", line)

        report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
        return report_path, diff
    except Exception as exc:  # noqa: BLE001 — the gate is best-effort, not a retrain crash
        log.exception("benchmark gate failed", extra={"error": str(exc)})
        return None, None


def run_evaluate_detectors(argv: Sequence[str]) -> int:
    """Run A02's harness (``scripts/evaluate_detectors.py``) as its own
    process; its exit code. Its own process, not an import: scripts never
    import each other (R06.4), and the harness reconfigures logging for its
    process, which in-process used to silence this retrain's INFO log for
    the rest of the run."""
    return subprocess.run(
        [sys.executable, str(EVALUATE_DETECTORS_SCRIPT), *argv], check=False
    ).returncode


#: The benchmark gate's injection seed: fixed, so two retrains of the same
#: holdout inject the same windows and their reports are comparable.
BENCHMARK_SEED = 42


def _evaluate_on_drives(
    evaluation_sessions: Sequence[Path],
    run: RetrainRun,
    *,
    if_model: Path,
    sequence_model: Path | None,
    out: Path,
    injections: bool = False,
) -> dict[str, Any]:
    """A02's harness on the evaluation drives; returns its JSON report.

    Never the A02.5 baselines or the A04.1 shift test: neither gate reads
    them. ``injections=False`` (the alert-budget gate) scores clean rows only;
    ``injections=True`` (the benchmark gate) also sweeps every realistic fault
    at the harness's default severities and repetitions, with a fixed seed.
    """
    args = run.args
    argv = [
        *(str(path) for path in evaluation_sessions),
        "--config",
        str(args.config),
        "--bindings",
        str(args.bindings),
        "--if-model",
        str(if_model),
        "--baselines",
        "--no-shift-test",
        "--seed",
        str(BENCHMARK_SEED),
        "--out",
        str(out),
    ]
    if not injections:
        argv += ["--faults", "--injections-per-drive", "0"]
    if args.feature_cache is not None:
        # Beside the trainers' feature-frame cache (the harness's default when
        # that is the default too), so a test run's cache stays in its tree.
        argv += ["--cache-dir", str(Path(args.feature_cache).parent / "evaluation_frames")]
    on_pi = on_the_pi() or getattr(args, "compute_target", None) == "pi"
    if on_pi:
        # The harness's score cache is laptop-only and refuses on the Pi
        # (P01's rules): a Pi-side retrain scores its gates afresh (P02.5).
        argv.append("--no-score-cache")
    derived = derived_dir_for(args)
    # The derived store refuses to build on the Pi too, and a refused load
    # is a skipped session: the gate would score nothing. Reduce in memory.
    argv += ["--no-derived-store"] if derived is None or on_pi else ["--derived-dir", str(derived)]
    if args.profile:
        argv += ["--profile", str(args.profile)]
    argv += (
        ["--no-sequence"] if sequence_model is None else ["--sequence-model", str(sequence_model)]
    )
    code = run_evaluate_detectors(argv)
    if code != 0:
        raise RuntimeError(f"evaluate_detectors exited {code}")
    report: dict[str, Any] = json.loads(out.read_text())
    return report


def _run_alert_budget_gate(
    config: CanaryConfig,
    evaluation_sessions: Sequence[Path],
    run: RetrainRun,
    published: Sequence[str],
) -> AlertBudgetCheck:
    """A06.5 — at most 1 alert per engine-hour on the evaluation drives, as a
    refusal (``src.models.alert_budget``). Only alert-role artifacts are held
    to it; with both detectors ``evidence`` (the operator's 2026-09-23
    decision) it returns "not required" without scoring anything.

    Unlike the benchmark gate, a failure here is not "gate didn't run": an
    alert-role artifact that could not be measured has not shown the budget,
    and is refused like one that failed it.
    """
    args = run.args
    roles = config.models.roles.by_source()
    override = args.accept_alert_budget
    if not alert_role_sources(roles, published):
        return check_alert_budget(
            None, roles_by_source=roles, published_sources=published, override_reason=override
        )
    if_model = run.train_dir(ISOLATION_FOREST_SOURCE) / "model.joblib"
    sequence_model: Path | None = None
    missing: str | None = None
    if run.seq_model_type == "lstm_autoencoder":
        candidate = run.train_dir(SEQUENCE_SOURCE) / Path(config.daemon.lstm_artifact).name
        sequence_model = candidate if candidate.exists() else None
    elif roles.get(SEQUENCE_SOURCE) == "alert" and SEQUENCE_SOURCE in published:
        missing = f"A02's harness scores LSTM sequence artifacts only, not {run.seq_model_type}"
    if not evaluation_sessions:
        missing = (
            "no evaluation holdout (a corpus too small to spare one, or --no-calibration-holdout)"
        )
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = args.analyses_dir / f"alert-budget-{stamp}.json"
    report: dict[str, Any] | None = None
    if missing is None:
        try:
            args.analyses_dir.mkdir(parents=True, exist_ok=True)
            report = _evaluate_on_drives(
                evaluation_sessions,
                run,
                if_model=if_model,
                sequence_model=sequence_model,
                out=out,
            )
        except Exception as exc:  # noqa: BLE001 — unmeasured is refused below, not a crash
            log.exception("alert-budget evaluation failed", extra={"error": str(exc)})
            missing = f"the evaluation failed: {exc}"
    expected = {
        "isolation_forest": _file_sha256(if_model)
        if ISOLATION_FOREST_SOURCE in published
        else None,
        "sequence": _file_sha256(sequence_model)
        if sequence_model is not None and SEQUENCE_SOURCE in published
        else None,
    }
    check = check_alert_budget(
        report,
        roles_by_source=roles,
        published_sources=published,
        override_reason=override,
        missing_report_reason=missing,
        expected_artifacts=expected,
        report_path=str(out) if report is not None else None,
    )
    check = dataclasses.replace(check, extra={"evaluation_drives": evaluation_drives(report)})
    verdict_path = out.with_name(f"{out.stem}-verdict.json")
    try:
        args.analyses_dir.mkdir(parents=True, exist_ok=True)
        verdict_path.write_text(json.dumps(check.to_dict(), indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        log.warning("cannot write the alert-budget verdict %s: %s", verdict_path, exc)
    return check


#: R01.6: the one definition is ``src.artifact_hash.sha256_file_or_none``.
_file_sha256 = sha256_file_or_none


def _mark_requested_failures(run: RetrainRun, status: str) -> None:
    if not run.skip_if:
        run.results["isolation_forest"] = status
    if not run.skip_sequence:
        run.results[run.seq_model_type] = status


def _print_report(
    run: RetrainRun,
    *,
    feedback_report: RetrainingFeedbackReport | None = None,
    benchmark_report_path: Path | None = None,
    benchmark_diff: dict[str, Any] | None = None,
    budget_check: AlertBudgetCheck | None = None,
) -> None:
    results = run.results
    print("=" * 72)
    print(f"RETRAIN  ({len(run.sessions)} sessions)")
    print("=" * 72)
    split = run.holdout_split
    if split is not None:
        print(f"  calibration holdout  →  {', '.join(split.calibration) or 'none'}")
        print(f"  evaluation holdout   →  {', '.join(split.evaluation) or 'none (gate skipped)'}")
    if feedback_report is not None:
        print(
            "  feedback policy  →  "
            f"{feedback_report.clean_baseline_labels} clean labels, "
            f"{feedback_report.excluded_labels} excluded labels, "
            f"{feedback_report.excluded_rows} rows removed, "
            f"{feedback_report.enriched_labels} enriched (hypothesis/Q&A)"
        )
    out_dirs = {"isolation_forest": run.if_out, run.seq_model_type: run.sequence_out}
    for name, out_dir in out_dirs.items():
        if name not in results:
            continue
        status = results[name]
        if status == "ok":
            symbol = "✓"
        elif status.startswith("skipped"):
            symbol = "○"
        else:
            symbol = "✗"
        print(f"  {symbol} {name}  →  {out_dir}  {status}")
    if benchmark_report_path is not None:
        if benchmark_diff is None:
            symbol, status = "✓", "ok (no prior baseline to compare)"
        elif not benchmark_diff.get("comparable", True):
            symbol, status = "○", f"not compared: {benchmark_diff['reason']}"
        elif benchmark_diff["regressed"]:
            symbol, status = "✗", "regression detected vs baseline"
        else:
            symbol, status = "✓", "ok"
        print(f"  {symbol} benchmark  →  {benchmark_report_path}  {status}")
    if budget_check is not None and budget_check.required:
        if budget_check.refused:
            symbol, status = "✗", "refused: " + "; ".join(budget_check.violations)
        elif budget_check.violations:
            symbol, status = "!", f"accepted over budget: {budget_check.override_reason}"
        else:
            symbol, status = "✓", "within budget"
        print(f"  {symbol} alert budget  →  {budget_check.report or 'no report'}  {status}")
    print()


if __name__ == "__main__":
    sys.exit(main())
