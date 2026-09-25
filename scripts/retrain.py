#!/usr/bin/env python3
"""Retrain both anomaly detectors from recent sessions (Task 5.17).

Manual trigger in v1; cron later. The companion piece to `refit_behavior_models.py`:
runs the Isolation Forest trainer (`scripts/train_anomaly_model.train`) and the
**configured sequence-detector** trainer — the GRU-D LSTM (`scripts/train_lstm`)
or the TCN (`scripts/train_tcn`), selected by ``models.sequence_detector`` and
overridable with ``--sequence-model`` — over the same session set, side by side,
in one process. Only the *selected* sequence backbone is trained, never both.

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
landing (or vice versa) — partial coverage beats none. Each trainer's
``train()`` already validates inputs and raises with a useful message; we
log + continue.
"""

from __future__ import annotations

import argparse
import dataclasses
import gc
import json
import logging
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

# Reuse the trainer modules as importable libraries — same pattern
# refit_behavior_models.py uses.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import detector_benchmark  # noqa: E402
import disposition_report  # noqa: E402
import train_anomaly_model as if_trainer  # noqa: E402
import train_lstm as lstm_trainer  # noqa: E402
import train_tcn as tcn_trainer  # noqa: E402
from _cli import (  # noqa: E402
    add_common_args,
    add_profile_arg,
    load_config,
    parse_since_argument,
    resolve_sessions,
    scoped_models_dir,
)
from refit_behavior_models import _filter_sessions_by_boundary  # noqa: E402

from src.alerts.feedback import DEFAULT_FEEDBACK_PATH, FeedbackStore  # noqa: E402
from src.analysis.training_disposition import (  # noqa: E402
    GoldenBaselines,
    classify_dispositions,
    compute_quarantine_mask,
)
from src.analysis.vehicle_ledger import VehicleLedgerStore  # noqa: E402
from src.config import CanaryConfig  # noqa: E402
from src.decode.accepted_baselines import AcceptedBaselines, load_accepted_baselines  # noqa: E402
from src.log import configure_logging  # noqa: E402
from src.models.alert_budget import (  # noqa: E402
    AlertBudgetCheck,
    alert_role_sources,
    check_alert_budget,
    evaluation_drives,
    file_sha256,
    verdict_record,
    write_verdict,
)
from src.models.artifact_hash import sha256_file_or_none  # noqa: E402
from src.models.evaluation import (  # noqa: E402
    HoldoutSplit,
    drives_of_sessions,
    holdout_by_drive,
)
from src.models.golden_baselines import golden_baselines_path, read_golden_baselines  # noqa: E402
from src.models.isolation_forest import load_isolation_forest_model  # noqa: E402
from src.models.manifest import ManifestValidationError  # noqa: E402
from src.models.retrain_exit_codes import (  # noqa: E402
    ALERT_BUDGET_REFUSED,
    WINDOW_TOO_SMALL_FOR_HOLDOUT,
)
from src.models.retraining_policy import (  # noqa: E402
    RetrainingFeedbackReport,
    apply_retraining_feedback_policy,
)
from src.models.sequence_registry import MIN_CALIBRATION_SESSIONS  # noqa: E402
from src.models.sequence_training import (  # noqa: E402
    configure_torch_threads,
    default_training_device,
)
from src.models.training_ledger import (  # noqa: E402
    DEFAULT_TRAINING_LEDGER_PATH,
    TrainingLedger,
    build_run_record_from_manifest,
)
from src.paths import BINDINGS_YAML  # noqa: E402
from src.preprocessing.feature_pipeline import (  # noqa: E402
    DEFAULT_FEATURE_CACHE_DIR,
    build_feature_frame,
    candidate_feature_columns,
    resolve_feature_policy,
)
from src.preprocessing.quality_gate import apply_quarantine_gate  # noqa: E402
from src.session import session_sort_key  # noqa: E402

log = logging.getLogger(__name__)

#: Selectable sequence-detector backbones: config selector value → (model_type,
#: trainer module). Only the configured/selected backbone is trained per run.
SEQUENCE_TRAINERS: dict[str, tuple[str, object]] = {
    "lstm": ("lstm_autoencoder", lstm_trainer),
    "tcn": ("tcn_autoencoder", tcn_trainer),
}

#: Minimum total parquet bytes for a session to be considered "substantial"
#: enough to anchor an out-of-sample calibration holdout. A single in-progress
#: capture (1 truncated chunk a few KB on disk) doesn't have the rows to form
#: useful per-mode bands — auto-holdout skips it and picks an earlier real
#: drive instead. ~1 MB is roughly a minute of telemetry at our chunk rate;
#: way below any actual drive (the 05-18 short idles are still ~1.5 MB).
_MIN_CALIBRATION_HOLDOUT_BYTES = 1_000_000


def _session_parquet_bytes(session_dir: Path) -> int:
    try:
        return sum(p.stat().st_size for p in session_dir.glob("telemetry-*.parquet"))
    except OSError:
        return 0


#: A mode must run at least this long in a session (per the classifier
#: sidecar) before the session counts as "covering" that mode for holdout
#: selection — a 5 s highway blip can't calibrate a highway band.
_MIN_HOLDOUT_MODE_SECONDS = 60.0


def _session_mode_coverage(session: Path) -> set[str] | None:
    """Modes a session meaningfully covers, from its classifier sidecar.

    ``None`` when the session hasn't been classified (run
    ``scripts/label_sessions.py``) — callers treat that as unknown coverage.
    """
    from src.analysis.session_classifier import read_classification

    classification = read_classification(session)
    if classification is None:
        return None
    return {
        mode
        for mode, seconds in classification.mode_seconds.items()
        if seconds >= _MIN_HOLDOUT_MODE_SECONDS and mode != "unknown"
    }


def _session_holdout_health(session: Path) -> tuple[str, int] | None:
    """Classifier disposition and observed feature width for holdout screening."""
    from src.analysis.session_classifier import read_classification

    classification = read_classification(session)
    if classification is None:
        return None
    return (
        classification.disposition,
        int(classification.quality.get("feature_columns", 0)),
    )


#: Both detectors calibrate on the same pooled holdout set; the floor is the
#: sequence registry's (one definition — the manifest stamps a shortfall).
MIN_CALIBRATION_HOLDOUT_SESSIONS = MIN_CALIBRATION_SESSIONS


def _started_at_key(session: Path) -> datetime:
    """Sort key by *parsed* start time (never by directory name — capture
    prefixes sort first). Unparseable names keep their given order, oldest:
    the time half of :func:`src.session.session_sort_key` (R01.1)."""
    return session_sort_key(session)[0]


def _pick_calibration_holdouts(
    sessions: Sequence[Path],
    *,
    max_holdouts: int = MIN_CALIBRATION_HOLDOUT_SESSIONS,
) -> list[Path]:
    """Healthy substantial sessions: mode coverage first, then spread in time.

    The historical single auto-holdout had a blind spot: if the freshest
    drive was an idle session, highway/city earned no per-mode band AND the
    global fallback threshold was calibrated from an idle-dominated error
    distribution — too tight or too loose for the first real drive.

    Pass 1 walks newest→oldest: the freshest substantial session anchors the
    holdout (the legacy rule, so unclassified corpora behave exactly as
    before); further sessions are added when their classifier sidecar covers a
    mode not yet held out, until every mode seen in the classified corpus is
    covered or ``max_holdouts`` is reached.

    Pass 2 tops the set up to ``max_holdouts``. Pass 1 alone stops at ONE
    session whenever the freshest drive happens to touch every mode, and a
    single drive is a single day's conditions. The extra sessions are the
    healthy ones *farthest in time* from those already held out, so the
    calibration set spans the corpus rather than clustering on its last day.
    The same list goes to every detector (see ``main``).
    """
    ordered = sorted(sessions, key=_started_at_key)
    corpus_modes: set[str] = set()
    classified_widths = [
        health[1]
        for session in ordered
        if (health := _session_holdout_health(session)) is not None
        and health[0] == "ready"
        and health[1] > 0
    ]
    minimum_compatible_width = 0.9 * max(classified_widths, default=0)
    for session in ordered:
        modes = _session_mode_coverage(session)
        if modes:
            corpus_modes |= modes

    healthy: list[Path] = []
    for session in reversed(ordered):
        if _session_parquet_bytes(session) < _MIN_CALIBRATION_HOLDOUT_BYTES:
            continue
        health = _session_holdout_health(session)
        if health is not None and (health[0] != "ready" or health[1] < minimum_compatible_width):
            log.info(
                "skipping unhealthy calibration holdout",
                extra={
                    "session": session.name,
                    "disposition": health[0],
                    "feature_columns": health[1],
                    "minimum_compatible_feature_columns": minimum_compatible_width,
                },
            )
            continue
        healthy.append(session)

    # One pick per physical drive (A02.1/A02.2): three sessions of one day's
    # drive are one drive's conditions, and the sequence trainer refuses a
    # calibration resting on fewer than MIN_CALIBRATION_SESSIONS drives.
    drives = drives_of_sessions(ordered)
    drive_of = {name: drive.drive_id for drive in drives for name in drive.session_names}
    drive_members = {
        drive.drive_id: [span.path for span in drive.sessions if span.path is not None]
        for drive in drives
    }

    def new_drive(candidate: Path, chosen: Sequence[Path]) -> bool:
        taken = {drive_of.get(held.name, held.name) for held in chosen}
        return drive_of.get(candidate.name, candidate.name) not in taken

    selected: list[Path] = []
    covered: set[str] = set()
    for session in healthy:
        if len(selected) >= max_holdouts:
            break
        modes = _session_mode_coverage(session)
        if not selected:
            selected.append(session)
            covered |= modes or set()
        elif modes and modes - covered and new_drive(session, selected):
            selected.append(session)
            covered |= modes
        if corpus_modes and covered >= corpus_modes:
            break

    while selected and len(selected) < max_holdouts:
        remaining = [
            session
            for session in healthy
            if session not in selected and new_drive(session, selected)
        ]
        if not remaining:
            break
        selected.append(
            max(
                remaining,
                key=lambda candidate: min(
                    abs((_started_at_key(candidate) - _started_at_key(held)).total_seconds())
                    for held in selected
                ),
            )
        )
    # Whole drives, in pick order: a picked session's restart/capture siblings
    # are held out with it, so no caller of the picker can train on half a
    # calibration drive (callers still split through ``holdout_by_drive``).
    whole: list[Path] = []
    for session in selected:
        for member in drive_members.get(drive_of.get(session.name, ""), [session]):
            if member not in whole:
                whole.append(member)
    return whole


#: Held-out drives the post-retrain gate scores (A02.1), disjoint from the
#: calibration drives. Same count as the calibration floor: one drive is one
#: day's conditions, for grading as much as for calibrating.
EVALUATION_HOLDOUT_DRIVES = MIN_CALIBRATION_HOLDOUT_SESSIONS


def _pick_evaluation_holdouts(
    sessions: Sequence[Path],
    *,
    max_sessions: int,
    max_drives: int = EVALUATION_HOLDOUT_DRIVES,
) -> list[Path]:
    """The newest healthy, substantial drives, for the regression gate only.

    Drawn from ``sessions`` after calibration has taken its drives, so the gate
    never scores a session the thresholds were cut from (A02.1). Whole drives
    or nothing, newest first, within ``max_sessions`` so training keeps its
    floor.
    """
    selected: list[Path] = []
    drives = 0
    for drive in reversed(drives_of_sessions(sessions)):
        if drives >= max_drives:
            break
        members = [span.path for span in drive.sessions if span.path is not None]
        if len(selected) + len(members) > max_sessions:
            continue
        healthy = [
            m
            for m in members
            if _session_parquet_bytes(m) >= _MIN_CALIBRATION_HOLDOUT_BYTES
            and ((health := _session_holdout_health(m)) is None or health[0] == "ready")
        ]
        if not healthy:
            continue
        selected.extend(members)
        drives += 1
    return sorted(selected, key=_started_at_key)


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
    parser.add_argument(
        "--bindings",
        type=Path,
        default=BINDINGS_YAML,
        help="Path to bindings.yaml for CAN-signal decode.",
    )
    parser.add_argument(
        "--if-out",
        type=Path,
        default=None,
        help="Output directory for the Isolation Forest artifact + manifest. "
        "Default: the (profile-scoped) <models_dir>/isolation_forest from --config.",
    )
    parser.add_argument(
        "--sequence-model",
        choices=sorted(SEQUENCE_TRAINERS),
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
    parser.add_argument(
        "--accept-thin-calibration",
        default=None,
        metavar="REASON",
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
        help="Skip the post-retrain detector-benchmark regression gate (Task 2.6).",
    )
    parser.add_argument(
        "--accept-alert-budget",
        type=_stated_reason,
        default=None,
        metavar="REASON",
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
        help="Directory for detector_benchmark.py reports (Task 2.6 regression gate). "
        "Also where the most recent prior report is discovered as the --baseline "
        "when --benchmark-baseline isn't given.",
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
        default=detector_benchmark.DEFAULT_TOLERANCE,
        # argparse runs `help % params` for every action (for %(default)s
        # etc.), so a literal "%" here must be doubled or it's parsed as a
        # (broken) format spec.
        help="Relative regression tolerance for the post-retrain benchmark gate "
        f"(default {detector_benchmark.DEFAULT_TOLERANCE * 100:.0f}%%).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def _stated_reason(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("an override needs a stated reason")
    return value.strip()


#: A06.5 — the event source each retrained artifact publishes under
#: (``DetectorRolesConfig.by_source``): both sequence backbones fill the one
#: ``lstm_autoencoder`` slot.
SEQUENCE_SOURCE = "lstm_autoencoder"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    configure_torch_threads()
    config = load_config(args)

    # Resolve the sequence-detector backbone (config default, --sequence-model
    # override) and stash the resolved model_type + output dir on args so the
    # report/failure helpers stay backbone-agnostic.
    sequence_model = args.sequence_model or config.models.sequence_detector
    seq_model_type, seq_trainer = SEQUENCE_TRAINERS[sequence_model]
    args.sequence_model = sequence_model
    models_dir = scoped_models_dir(config, args.profile)
    args.if_out = args.if_out or models_dir / "isolation_forest"
    args.sequence_out = args.sequence_out or models_dir / seq_model_type
    args.seq_model_type = seq_model_type

    if args.skip_if and args.skip_sequence:
        print(
            "error: --skip-if and --skip-sequence together leave nothing to do.",
            file=sys.stderr,
        )
        return 1

    sessions = resolve_sessions(args, args.sessions_root)
    if args.since_boundary:
        boundary = VehicleLedgerStore(config.daemon.vehicle_ledger).latest_boundary()
        if boundary is not None:
            before = len(sessions)
            sessions = _filter_sessions_by_boundary(sessions, boundary.occurred_at)
            log.info(
                "--since-boundary applied",
                extra={
                    "boundary_event": boundary.title,
                    "boundary_occurred_at": str(boundary.occurred_at),
                    "sessions_before": before,
                    "sessions_after": len(sessions),
                },
            )
    if not sessions:
        print(
            f"error: no sessions under {args.sessions_root} within --days {args.days}. "
            "Pass session paths directly or use --all.",
            file=sys.stderr,
        )
        return 1

    # Pick calibration holdouts shared by every enabled detector. Out-of-sample
    # threshold calibration is the default — in-sample calibration underestimates
    # cross-session error and floods false positives on unseen drives. Explicit
    # --calibration-sessions wins; otherwise auto-hold-out the most-recent
    # *substantial* discovered session — abandoned in-progress captures (single
    # truncated chunk a few KB on disk) are skipped because they don't have the
    # rows to make a useful calibration band, and picking them would just push
    # everything to the global threshold. With fewer than 3 sessions we can't
    # afford to hold one out, so we log and fall back to in-sample.
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
        holdouts = _pick_calibration_holdouts(sessions, max_holdouts=min(3, len(sessions) - 2))
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

    # A02.1: the regression gate scores sessions disjoint from calibration,
    # grouped by physical drive. ``holdout_by_drive`` expands every named or
    # picked session to its whole drive and refuses a drive on both sides;
    # the evaluation drives come from what calibration left, keeping at
    # least two sessions for training.
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
            evaluation_sessions = _pick_evaluation_holdouts(
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
    sessions = list(holdout.training)
    calibration_sessions = list(holdout.calibration)
    evaluation_sessions = list(holdout.evaluation)
    args.holdout_split = holdout.split

    # Task 3.1: a --since window is explicitly the scheduled-retrain shape (a
    # rolling window, not a fixed corpus) — the calibration-holdout fallback
    # above (warn + train in-sample) is the right posture for an ad hoc manual
    # --days run with a thin corpus, but a *scheduled* run must never silently
    # degrade to in-sample calibration (that's exactly the FPR≈1.0 flood the
    # ml-hardening review traced to in-sample calibration). Refuse instead —
    # a distinct exit code so a caller (the online-training scheduler, 3.2)
    # can log this as a routine skip rather than a hard failure.
    if (
        args.since is not None
        and not args.skip_sequence
        and not args.no_calibration_holdout
        and not calibration_sessions
    ):
        print(
            f"error: --since window ({len(sessions)} session(s)) is too small to "
            "reserve a calibration holdout; a scheduled retrain never calibrates "
            "in-sample. Widen --since, pass explicit --calibration-sessions, or "
            "(manual runs only) --no-calibration-holdout.",
            file=sys.stderr,
        )
        return WINDOW_TOO_SMALL_FOR_HOLDOUT

    log.info(
        "starting retrain",
        extra={
            "sessions": len(sessions),
            "calibration_sessions": [str(s) for s in calibration_sessions],
            "evaluation_sessions": [str(s) for s in evaluation_sessions],
            "skip_if": args.skip_if,
            "skip_sequence": args.skip_sequence,
            "sequence_model": seq_model_type,
            "device": args.device,
        },
    )

    results: dict[str, str] = {}

    # A model disabled in config is "skipped", not "failed": running its
    # trainer would raise `_validate_config` ("... config is disabled"), which
    # is a no-op intent, not an error. Treat it like an implicit --skip-<m> so
    # a disabled trainer doesn't poison the exit code, the report, or the
    # all-failed check (and sync-train.sh still runs the behavior refit).
    if not args.skip_if and not config.models.isolation_forest.enabled:
        log.info("isolation_forest disabled in config; skipping")
        results["isolation_forest"] = "skipped (disabled in config)"
        args.skip_if = True
    if not args.skip_sequence and not getattr(config.models, seq_model_type).enabled:
        log.info("%s disabled in config; skipping", seq_model_type)
        results[seq_model_type] = "skipped (disabled in config)"
        args.skip_sequence = True

    ledger = None if args.no_ledger else TrainingLedger(args.ledger)
    feedback_labels = {} if args.no_feedback_policy else FeedbackStore(args.feedback).labels()
    any_trainer_requested = not args.skip_if or not args.skip_sequence
    # The shared frame is where the quarantine screen and the feedback policy
    # are applied, so it is built whenever either applies -- not only when both
    # trainers run. An IF-only retrain (--skip-sequence) used to hand the
    # trainer an unscreened frame: the 2026-09-23 A06.3 candidate IF lost 0
    # rows to quarantine where the same corpus, both trainers, lost 369 of 709.
    needs_shared_feature_frame = any_trainer_requested and (
        (not args.skip_if and not args.skip_sequence)
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
            _mark_requested_failures(results, args, f"failed: preprocessing: {exc}")
            _print_report(sessions, results, args)
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
            _mark_requested_failures(results, args, f"failed: calibration preprocessing: {exc}")
            _print_report(sessions, results, args)
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
            _mark_requested_failures(results, args, f"failed: feedback policy: {exc}")
            _print_report(sessions, results, args, feedback_report=feedback_report)
            return 1
        if shared_feature_frame.empty:
            msg = "feedback policy excluded every training row"
            log.error(msg)
            _mark_requested_failures(results, args, f"failed: {msg}")
            _print_report(sessions, results, args, feedback_report=feedback_report)
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
            _mark_requested_failures(results, args, f"failed: calibration feedback policy: {exc}")
            _print_report(sessions, results, args, feedback_report=feedback_report)
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

    # out_dirs of trainers that produced a fresh artifact this run — ledger
    # ingestion is deferred until after the benchmark gate (below) so each
    # record can link the report the gate produced against that artifact.
    ledger_pending: list[Path] = []
    # What sat in each out_dir BEFORE this run. `_ingest_ledger` refuses a
    # manifest that is byte-for-byte the one already there: see its docstring.
    manifests_before = {
        out_dir: _manifest_fingerprint(out_dir) for out_dir in (args.if_out, args.sequence_out)
    }
    # A06.5: an alert-role artifact trains into a staging directory and reaches
    # the served one only through the alert-budget gate below, so the daemon's
    # ModelManager can never hot-swap an artifact the gate then refuses.
    served_out = {"isolation_forest": args.if_out, SEQUENCE_SOURCE: args.sequence_out}
    budget_gated = set(
        alert_role_sources(
            config.models.roles.by_source(),
            [
                source
                for source, skipped in (
                    ("isolation_forest", args.skip_if),
                    (SEQUENCE_SOURCE, args.skip_sequence),
                )
                if not skipped
            ],
        )
    )
    if "isolation_forest" in budget_gated:
        args.if_out = _staging_dir(args.if_out)
    if SEQUENCE_SOURCE in budget_gated:
        args.sequence_out = _staging_dir(args.sequence_out)

    if not args.skip_if:
        try:
            if_trainer.train(
                sessions,
                config,
                args.if_out,
                bindings_path=args.bindings,
                calibration_sessions=calibration_sessions or None,
                feature_policy=feature_policy,
                evaluation_sessions=evaluation_sessions or None,
                quarantined_cells=quarantined_cells,
                calibration_policy=calibration_policy,
                calibration_override=args.accept_thin_calibration,
                **if_calibration_kwargs,
                **feature_kwargs,
            )
            results["isolation_forest"] = "ok"
            ledger_pending.append(served_out["isolation_forest"])
        except Exception as exc:  # noqa: BLE001 — surface + continue to the next trainer
            log.exception("isolation forest trainer failed", extra={"error": str(exc)})
            results["isolation_forest"] = f"failed: {exc}"

        # The sequence trainer needs the shared feature frame, but none of the
        # IF mode matrices. Reclaim cyclic/joblib temporaries before allocating
        # the LSTM dataset's values/mask/delta arrays.
        gc.collect()

    if not args.skip_sequence:
        try:
            seq_trainer.train(  # type: ignore[attr-defined]
                sessions,
                config,
                args.sequence_out,
                bindings_path=args.bindings,
                device=args.device,
                calibration_sessions=calibration_sessions or None,
                feature_policy=feature_policy,
                evaluation_sessions=evaluation_sessions or None,
                calibration_override=args.accept_thin_calibration,
                **calibration_kwargs,
                **feature_kwargs,
            )
            results[seq_model_type] = "ok"
            ledger_pending.append(served_out[SEQUENCE_SOURCE])
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "sequence trainer failed", extra={"error": str(exc), "model": seq_model_type}
            )
            results[seq_model_type] = f"failed: {exc}"

    benchmark_report_path: Path | None = None
    benchmark_diff: dict[str, Any] | None = None
    if not args.skip_benchmark:
        # A02.1: evaluation sessions only — never the ones thresholds were cut from.
        benchmark_report_path, benchmark_diff = _run_benchmark_gate(
            config, evaluation_sessions, args
        )
    published = [
        source
        for source, name in (
            ("isolation_forest", "isolation_forest"),
            (SEQUENCE_SOURCE, seq_model_type),
        )
        if results.get(name) == "ok"
    ]
    budget_check = _run_alert_budget_gate(config, evaluation_sessions, args, published)
    refused_dirs: dict[Path, Path] = {}
    for source in sorted(budget_gated):
        staged = args.if_out if source == "isolation_forest" else args.sequence_out
        name = "isolation_forest" if source == "isolation_forest" else seq_model_type
        if source not in published:
            # The trainer failed: nothing to publish or keep.
            if staged.exists():
                shutil.rmtree(staged)
            continue
        # The verdict travels with the binary (operator decision 2026-09-23):
        # models reach the Pi by scp and data/analyses never does, so the
        # daemon serves an `alert` role only beside an accepted verdict that
        # names this artifact's sha256 (src/models/role_gate.py).
        _write_artifact_verdict(budget_check, source, staged / _artifact_name(config, source, args))
        if budget_check.refused:
            refused_dirs[served_out[source]] = _set_aside_refused(staged, served_out[source])
            results[name] = "refused: alert budget (see the alert-budget report)"
        else:
            _publish_staged(staged, served_out[source])
    if budget_gated:
        # Every later step (the report, the ledger) names the served paths.
        args.if_out = served_out["isolation_forest"]
        args.sequence_out = served_out[SEQUENCE_SOURCE]
    for out_dir in ledger_pending:
        refused_at = refused_dirs.get(out_dir)
        _ingest_ledger(
            ledger,
            refused_at if refused_at is not None else out_dir,
            "failed" if refused_at is not None else "succeeded",
            args.job_id,
            feedback_report=feedback_report,
            detector_benchmark_report_path=benchmark_report_path,
            dispositions=disposition_summary,
            compute_target=args.compute_target,
            manifest_before=manifests_before.get(out_dir),
        )

    _print_report(
        sessions,
        results,
        args,
        feedback_report=feedback_report,
        benchmark_report_path=benchmark_report_path,
        benchmark_diff=benchmark_diff,
        budget_check=budget_check,
    )
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
    # Partial success is the documented contract (a broken LSTM must not block
    # the IF artifact, and vice versa — see the module docstring). Exit
    # non-zero ONLY when every requested trainer failed, so the caller
    # (sync-train.sh) still runs the independent behavior-model refit instead
    # of `set -e` aborting the whole pipeline on a partial run.
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
    `scripts/disposition_report.py` runs standalone (reused via its
    `_scan_sessions`, not reimplemented), applied here so a real retrain run
    finally populates `TrainingRunRecord.dispositions` (Task 1.4 added the
    schema field with the note "populated in Phase 3"; this is that wiring).
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
        framed_parts, reject_counts = disposition_report._scan_sessions(
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
    """Prior `detector_benchmark.py` reports, newest first — the gate diffs
    against the newest one that scored the same sessions with the same
    decode chain (`detector_benchmark.incomparable_reason`)."""
    if not analyses_dir.is_dir():
        return []
    return sorted(analyses_dir.glob("benchmark-*.json"), reverse=True)


def _run_benchmark_gate(
    config: CanaryConfig,
    evaluation_sessions: Sequence[Path],
    args: argparse.Namespace,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Post-retrain regression gate (Task 2.6): benchmark the freshly-trained
    artifacts on held-out sessions and diff against the most recent prior
    report. Returns ``(report_path, diff)``, or ``(None, None)`` when the
    gate didn't run (skipped, or nothing to compare it against).

    Scores the *evaluation* holdout only (A02.1). It used to score the
    calibration sessions — the sessions the thresholds were cut from — so the
    sequence detector's clean-alert rate read ~0.1 % by construction while it
    flagged 38-75 % of windows on unseen drives. No evaluation holdout (a
    corpus too small to spare one after calibration, or
    ``--no-calibration-holdout``) means no honest benchmark, so the gate is
    skipped — logged, not a failure — rather than silently testing on
    calibration or training data.

    Never raises: like the trainers themselves, an unexpected failure here is
    logged and treated as "gate didn't run", not a retrain-flow crash.
    """
    if not evaluation_sessions:
        log.warning(
            "no evaluation holdout for the post-retrain benchmark gate; skipping "
            "(the gate never scores calibration sessions; see --evaluation-sessions)"
        )
        return None, None

    if_model_path = args.if_out / "model.joblib"
    try:
        trained_if = load_isolation_forest_model(if_model_path)
    except FileNotFoundError:
        log.warning("no isolation forest artifact at %s; skipping benchmark gate", if_model_path)
        return None, None

    # The benchmark's default artifact path is UNSCOPED
    # (``models_dir/<type>/model.pt``), but retrain writes to the
    # profile-scoped ``args.sequence_out``. Passing ``None`` here let it look in
    # the pre-profile-migration location, miss, and silently skip the sequence
    # detector — so the 2026-07-26 retrain produced a benchmark report with no
    # LSTM coverage at all. Pass the path we actually just wrote.
    sequence_model_type = getattr(args, "seq_model_type", config.models.sequence_detector)
    sequence_artifact_name = Path(
        config.daemon.tcn_artifact
        if sequence_model_type == "tcn_autoencoder"
        else config.daemon.lstm_artifact
    ).name
    sequence_artifact_path = (
        None if args.sequence_out is None else args.sequence_out / sequence_artifact_name
    )
    try:
        trained_sequence = detector_benchmark._load_trained_sequence(
            config, sequence_artifact_path, skip=False
        )

        sessions_loaded: list[tuple[str, Any]] = []
        for session_dir in evaluation_sessions:
            try:
                resampled = detector_benchmark.load_resampled_session(
                    session_dir, config, bindings_path=args.bindings
                )
            except FileNotFoundError as exc:
                log.warning("benchmark gate: skipping unreadable session %s: %s", session_dir, exc)
                continue
            sessions_loaded.append((session_dir.name, resampled))
        if not sessions_loaded:
            log.warning("benchmark gate: no readable held-out sessions; skipping")
            return None, None

        cells = detector_benchmark.run_grid(
            sessions_loaded, config, trained_if, trained_sequence=trained_sequence
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        report = detector_benchmark.build_report(
            cells, seed=42, n_injections=5, generated_at=timestamp
        )
        split = getattr(args, "holdout_split", None)
        report["holdout"] = (
            split.to_dict()
            if split is not None
            else {"calibration": [], "evaluation": [s.name for s in evaluation_sessions]}
        )

        diff: dict[str, Any] | None = None
        candidates = (
            [Path(args.benchmark_baseline)]
            if args.benchmark_baseline is not None
            else _prior_benchmark_reports(args.analyses_dir)
        )
        first_reason: str | None = None
        for baseline_path in candidates:
            try:
                baseline_report = json.loads(Path(baseline_path).read_text())
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("benchmark gate: cannot read baseline %s: %s", baseline_path, exc)
                continue
            attempt = detector_benchmark.diff_reports(
                baseline_report, report, tolerance=args.benchmark_tolerance
            )
            if attempt.get("comparable", True):
                diff = {**attempt, "baseline": str(baseline_path)}
                break
            first_reason = first_reason or f"{Path(baseline_path).name}: {attempt['reason']}"
        if diff is None and first_reason is not None:
            # A02: a baseline from other drives or another decode chain is not
            # a regression check; say so and do not fail the retrain on it.
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

        args.analyses_dir.mkdir(parents=True, exist_ok=True)
        report_path = args.analyses_dir / f"benchmark-{timestamp}.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return report_path, diff
    except Exception as exc:  # noqa: BLE001 — the gate is best-effort, not a retrain crash
        log.exception("benchmark gate failed", extra={"error": str(exc)})
        return None, None


def _staging_dir(served: Path) -> Path:
    """Where an alert-role artifact trains before the budget gate (A06.5): a
    ``_``-prefixed sibling of the served directory, which neither the
    ModelManager nor training discovery reads. Emptied first, so a stale
    artifact from an earlier run can never be the one that gets published."""
    staging = served.parent / f"_staging-{served.name}"
    if staging.exists():
        shutil.rmtree(staging)
    return staging


def _artifact_name(config: CanaryConfig, source: str, args: argparse.Namespace) -> str:
    """The binary a gated source's verdict names (``model.joblib`` / ``model.pt``)."""
    if source == "isolation_forest":
        return Path(config.daemon.isolation_forest_artifact).name
    if getattr(args, "seq_model_type", None) == "tcn_autoencoder":
        return Path(config.daemon.tcn_artifact).name
    return Path(config.daemon.lstm_artifact).name


def _write_artifact_verdict(check: AlertBudgetCheck, source: str, artifact: Path) -> None:
    if not artifact.exists():
        log.warning("no artifact at %s to write an alert-budget verdict beside", artifact)
        return
    write_verdict(
        artifact,
        verdict_record(check, source=source, artifact_path=artifact, sha256=file_sha256(artifact)),
    )


def _publish_staged(staged: Path, served: Path) -> None:
    """Move a gated artifact into the served directory: the alert-budget
    verdict first, then the binaries, the manifest last. A poller then never
    pairs a new manifest with an old binary, and never sees the new binary
    without its verdict (which would serve it as evidence until the next
    poll)."""
    served.mkdir(parents=True, exist_ok=True)

    def order(path: Path) -> int:
        if path.name.endswith(".alert-budget.json"):
            return 0
        return 2 if path.name.endswith(".manifest.json") else 1

    files = sorted(staged.iterdir(), key=order)
    for path in files:
        path.replace(served / path.name)
    staged.rmdir()


def _set_aside_refused(staged: Path, served: Path) -> Path:
    """Keep a refused artifact for inspection, out of every served path."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = served.parent / f"_refused-alert-budget-{stamp}-{served.name}"
    staged.replace(target)
    return target


def _evaluate_on_drives(
    config: CanaryConfig,
    evaluation_sessions: Sequence[Path],
    args: argparse.Namespace,
    *,
    if_model: Path,
    sequence_model: Path | None,
    out: Path,
) -> dict[str, Any]:
    """A02's harness (``scripts/evaluate_detectors.py``) on the evaluation
    drives, clean scoring only: no baselines, injections or shift test, which
    the budget does not read. Returns its JSON report."""
    import evaluate_detectors

    argv = [
        *(str(path) for path in evaluation_sessions),
        "--config",
        str(args.config),
        "--bindings",
        str(args.bindings),
        "--if-model",
        str(if_model),
        "--baselines",
        "--faults",
        "--injections-per-drive",
        "0",
        "--no-shift-test",
        "--out",
        str(out),
    ]
    if getattr(args, "profile", None):
        argv += ["--profile", str(args.profile)]
    argv += (
        ["--no-sequence"] if sequence_model is None else ["--sequence-model", str(sequence_model)]
    )
    code = evaluate_detectors.main(argv)
    if code != 0:
        raise RuntimeError(f"evaluate_detectors exited {code}")
    report: dict[str, Any] = json.loads(out.read_text())
    return report


def _run_alert_budget_gate(
    config: CanaryConfig,
    evaluation_sessions: Sequence[Path],
    args: argparse.Namespace,
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
    roles = config.models.roles.by_source()
    override = getattr(args, "accept_alert_budget", None)
    if not alert_role_sources(roles, published):
        return check_alert_budget(
            None, roles_by_source=roles, published_sources=published, override_reason=override
        )
    sequence_model: Path | None = None
    missing: str | None = None
    sequence_type = getattr(args, "seq_model_type", "lstm_autoencoder")
    if sequence_type == "lstm_autoencoder":
        candidate = args.sequence_out / Path(config.daemon.lstm_artifact).name
        sequence_model = candidate if candidate.exists() else None
    elif roles.get(SEQUENCE_SOURCE) == "alert" and SEQUENCE_SOURCE in published:
        missing = f"A02's harness scores LSTM sequence artifacts only, not {sequence_type}"
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
                config,
                evaluation_sessions,
                args,
                if_model=args.if_out / "model.joblib",
                sequence_model=sequence_model,
                out=out,
            )
        except Exception as exc:  # noqa: BLE001 — unmeasured is refused below, not a crash
            log.exception("alert-budget evaluation failed", extra={"error": str(exc)})
            missing = f"the evaluation failed: {exc}"
    expected = {
        "isolation_forest": _file_sha256(args.if_out / "model.joblib")
        if "isolation_forest" in published
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


#: R01.6: the one definition is ``src.models.artifact_hash.sha256_file_or_none``.
_file_sha256 = sha256_file_or_none


def _mark_requested_failures(
    results: dict[str, str],
    args: argparse.Namespace,
    status: str,
) -> None:
    if not args.skip_if:
        results["isolation_forest"] = status
    if not args.skip_sequence:
        results[args.seq_model_type] = status


def _print_report(
    sessions: list[Path],
    results: dict[str, str],
    args: argparse.Namespace,
    *,
    feedback_report: RetrainingFeedbackReport | None = None,
    benchmark_report_path: Path | None = None,
    benchmark_diff: dict[str, Any] | None = None,
    budget_check: AlertBudgetCheck | None = None,
) -> None:
    print("=" * 72)
    print(f"RETRAIN  ({len(sessions)} sessions)")
    print("=" * 72)
    split = getattr(args, "holdout_split", None)
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
    out_dirs = {"isolation_forest": args.if_out, args.seq_model_type: args.sequence_out}
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
