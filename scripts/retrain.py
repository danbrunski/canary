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
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Reuse the trainer modules as importable libraries — same pattern
# refit_behavior_models.py uses.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import detector_benchmark  # noqa: E402
import train_anomaly_model as if_trainer  # noqa: E402
import train_lstm as lstm_trainer  # noqa: E402
import train_tcn as tcn_trainer  # noqa: E402
from refit_behavior_models import _filter_sessions_by_age  # noqa: E402

from src.alerts.feedback import DEFAULT_FEEDBACK_PATH, FeedbackStore  # noqa: E402
from src.config import CanaryConfig  # noqa: E402
from src.log import configure_logging  # noqa: E402
from src.models.isolation_forest import load_isolation_forest_model  # noqa: E402
from src.models.manifest import ManifestValidationError  # noqa: E402
from src.models.retraining_policy import (  # noqa: E402
    RetrainingFeedbackReport,
    apply_retraining_feedback_policy,
)
from src.models.sequence_training import (  # noqa: E402
    configure_torch_threads,
    default_training_device,
)
from src.models.training_ledger import (  # noqa: E402
    DEFAULT_TRAINING_LEDGER_PATH,
    TrainingLedger,
    build_run_record_from_manifest,
)
from src.preprocessing import discover_sessions  # noqa: E402
from src.preprocessing.feature_pipeline import (  # noqa: E402
    DEFAULT_FEATURE_CACHE_DIR,
    build_feature_frame,
)

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


def _pick_calibration_holdouts(
    sessions: Sequence[Path],
    *,
    max_holdouts: int = 3,
) -> list[Path]:
    """Most-recent substantial sessions, chosen for operating-mode coverage.

    The historical single auto-holdout had a blind spot: if the freshest
    drive was an idle session, highway/city earned no per-mode band AND the
    global fallback threshold was calibrated from an idle-dominated error
    distribution — too tight or too loose for the first real drive.

    Walk newest→oldest: the freshest substantial session anchors the holdout
    (the legacy rule, so unclassified corpora behave exactly as before);
    further sessions are added only when their classifier sidecar covers a
    mode not yet held out, until every mode seen in the classified corpus is
    covered or ``max_holdouts`` is reached.
    """
    corpus_modes: set[str] = set()
    for session in sessions:
        modes = _session_mode_coverage(session)
        if modes:
            corpus_modes |= modes

    selected: list[Path] = []
    covered: set[str] = set()
    for session in reversed(sessions):
        if len(selected) >= max_holdouts:
            break
        if _session_parquet_bytes(session) < _MIN_CALIBRATION_HOLDOUT_BYTES:
            continue
        modes = _session_mode_coverage(session)
        if not selected:
            selected.append(session)
            covered |= modes or set()
        elif modes and modes - covered:
            selected.append(session)
            covered |= modes
        if corpus_modes and covered >= corpus_modes:
            break
    return selected


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
        "--config",
        type=Path,
        default=Path("canary.yaml"),
        help="Path to canary.yaml.",
    )
    parser.add_argument(
        "--bindings",
        type=Path,
        default=Path("data/bindings.yaml"),
        help="Path to bindings.yaml for CAN-signal decode.",
    )
    parser.add_argument(
        "--if-out",
        type=Path,
        default=Path("models/isolation_forest"),
        help="Output directory for the Isolation Forest artifact + manifest.",
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
        "--skip-benchmark",
        action="store_true",
        help="Skip the post-retrain detector-benchmark regression gate (Task 2.6).",
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


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    configure_torch_threads()
    config = CanaryConfig.from_yaml(args.config)

    # Resolve the sequence-detector backbone (config default, --sequence-model
    # override) and stash the resolved model_type + output dir on args so the
    # report/failure helpers stay backbone-agnostic.
    sequence_model = args.sequence_model or config.models.sequence_detector
    seq_model_type, seq_trainer = SEQUENCE_TRAINERS[sequence_model]
    args.sequence_model = sequence_model
    args.sequence_out = args.sequence_out or Path("models") / seq_model_type
    args.seq_model_type = seq_model_type

    if args.skip_if and args.skip_sequence:
        print(
            "error: --skip-if and --skip-sequence together leave nothing to do.",
            file=sys.stderr,
        )
        return 1

    sessions: list[Path] = list(args.sessions)
    if not sessions:
        sessions = discover_sessions(args.sessions_root)
        if not args.use_all_sessions:
            sessions = _filter_sessions_by_age(sessions, args.days, datetime.now(UTC))
    if not sessions:
        print(
            f"error: no sessions under {args.sessions_root} within --days {args.days}. "
            "Pass session paths directly or use --all.",
            file=sys.stderr,
        )
        return 1

    # Pick the calibration holdout for the sequence detector. Out-of-sample
    # threshold calibration is the default — in-sample mean+k·σ underestimates
    # cross-session error and floods false positives on unseen drives. Explicit
    # --calibration-sessions wins; otherwise auto-hold-out the most-recent
    # *substantial* discovered session — abandoned in-progress captures (single
    # truncated chunk a few KB on disk) are skipped because they don't have the
    # rows to make a useful calibration band, and picking them would just push
    # everything to the global threshold. With fewer than 3 sessions we can't
    # afford to hold one out, so we log and fall back to in-sample.
    calibration_sessions: list[Path] = []
    if not args.skip_sequence:
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

    log.info(
        "starting retrain",
        extra={
            "sessions": len(sessions),
            "calibration_sessions": [str(s) for s in calibration_sessions],
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
    needs_shared_feature_frame = any_trainer_requested and (
        (not args.skip_if and not args.skip_sequence) or bool(feedback_labels)
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

    feature_kwargs = (
        {"feature_frame": shared_feature_frame}
        if shared_feature_frame is not None
        else {
            "feature_cache_dir": args.feature_cache,
            "rebuild_feature_cache": args.rebuild_feature_cache,
        }
    )

    # out_dirs of trainers that produced a fresh artifact this run — ledger
    # ingestion is deferred until after the benchmark gate (below) so each
    # record can link the report the gate produced against that artifact.
    ledger_pending: list[Path] = []

    if not args.skip_if:
        try:
            if_trainer.train(
                sessions,
                config,
                args.if_out,
                bindings_path=args.bindings,
                **feature_kwargs,
            )
            results["isolation_forest"] = "ok"
            ledger_pending.append(args.if_out)
        except Exception as exc:  # noqa: BLE001 — surface + continue to the next trainer
            log.exception("isolation forest trainer failed", extra={"error": str(exc)})
            results["isolation_forest"] = f"failed: {exc}"

    if not args.skip_sequence:
        try:
            seq_trainer.train(  # type: ignore[attr-defined]
                sessions,
                config,
                args.sequence_out,
                bindings_path=args.bindings,
                device=args.device,
                calibration_sessions=calibration_sessions or None,
                **feature_kwargs,
            )
            results[seq_model_type] = "ok"
            ledger_pending.append(args.sequence_out)
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "sequence trainer failed", extra={"error": str(exc), "model": seq_model_type}
            )
            results[seq_model_type] = f"failed: {exc}"

    benchmark_report_path: Path | None = None
    benchmark_diff: dict[str, Any] | None = None
    if not args.skip_benchmark:
        benchmark_report_path, benchmark_diff = _run_benchmark_gate(
            config, calibration_sessions, args
        )
    for out_dir in ledger_pending:
        _ingest_ledger(
            ledger,
            out_dir,
            "succeeded",
            args.job_id,
            feedback_report=feedback_report,
            detector_benchmark_report_path=benchmark_report_path,
        )

    _print_report(
        sessions,
        results,
        args,
        feedback_report=feedback_report,
        benchmark_report_path=benchmark_report_path,
        benchmark_diff=benchmark_diff,
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


def _ingest_ledger(
    ledger: TrainingLedger | None,
    out_dir: Path,
    status: str,
    job_id: str | None,
    *,
    feedback_report: RetrainingFeedbackReport | None = None,
    detector_benchmark_report_path: Path | None = None,
) -> None:
    """Best-effort: record the trainer's manifest in the training ledger.

    Never fails the retrain — a missing/unreadable manifest (e.g. a stubbed
    trainer in tests, or a trainer that crashed after reporting ok) is logged
    and skipped. The ledger is an index, not a gate.
    """
    if ledger is None:
        return
    manifest_path = out_dir / "model.manifest.json"
    if not manifest_path.exists():
        log.debug("no manifest at %s; skipping ledger ingest", manifest_path)
        return
    try:
        record = build_run_record_from_manifest(
            manifest_path,
            status="succeeded" if status == "succeeded" else "failed",
            job_id=job_id,
            detector_benchmark_report_path=detector_benchmark_report_path,
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


def _latest_benchmark_report(analyses_dir: Path) -> Path | None:
    """Most recent prior `detector_benchmark.py` report under `analyses_dir`.

    Filenames sort lexicographically by timestamp (`benchmark-YYYYMMDD-HHMMSS.json`),
    so a plain sorted glob picks the latest without parsing anything.
    """
    if not analyses_dir.is_dir():
        return None
    candidates = sorted(analyses_dir.glob("benchmark-*.json"))
    return candidates[-1] if candidates else None


def _run_benchmark_gate(
    config: CanaryConfig,
    calibration_sessions: Sequence[Path],
    args: argparse.Namespace,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Post-retrain regression gate (Task 2.6): benchmark the freshly-trained
    artifacts on held-out sessions and diff against the most recent prior
    report. Returns ``(report_path, diff)``, or ``(None, None)`` when the
    gate didn't run (skipped, or nothing to compare it against).

    Requires calibration holdouts: those are sessions excluded from training
    specifically so they're honest out-of-sample eval data (see
    `_pick_calibration_holdouts`); scoring the benchmark against training
    sessions would just measure memorization. No holdouts (e.g.
    `--no-calibration-holdout`, or too small a corpus) means no honest
    benchmark, so the gate is skipped — logged, not a failure — rather than
    silently testing on training data.

    Never raises: like the trainers themselves, an unexpected failure here is
    logged and treated as "gate didn't run", not a retrain-flow crash.
    """
    if not calibration_sessions:
        log.warning(
            "no held-out sessions for the post-retrain benchmark gate; skipping "
            "(see --calibration-sessions/--no-calibration-holdout)"
        )
        return None, None

    if_model_path = args.if_out / "model.joblib"
    try:
        trained_if = load_isolation_forest_model(if_model_path)
    except FileNotFoundError:
        log.warning("no isolation forest artifact at %s; skipping benchmark gate", if_model_path)
        return None, None

    try:
        trained_sequence = detector_benchmark._load_trained_sequence(config, None, skip=False)

        sessions_loaded: list[tuple[str, Any]] = []
        for session_dir in calibration_sessions:
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

        baseline_path = args.benchmark_baseline or _latest_benchmark_report(args.analyses_dir)
        diff: dict[str, Any] | None = None
        if baseline_path is not None and Path(baseline_path).exists():
            try:
                baseline_report = json.loads(Path(baseline_path).read_text())
                diff = detector_benchmark.diff_reports(
                    baseline_report, report, tolerance=args.benchmark_tolerance
                )
                report["regressions"] = diff
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("benchmark gate: cannot read baseline %s: %s", baseline_path, exc)

        args.analyses_dir.mkdir(parents=True, exist_ok=True)
        report_path = args.analyses_dir / f"benchmark-{timestamp}.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return report_path, diff
    except Exception as exc:  # noqa: BLE001 — the gate is best-effort, not a retrain crash
        log.exception("benchmark gate failed", extra={"error": str(exc)})
        return None, None


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
) -> None:
    print("=" * 72)
    print(f"RETRAIN  ({len(sessions)} sessions)")
    print("=" * 72)
    if feedback_report is not None:
        print(
            "  feedback policy  →  "
            f"{feedback_report.clean_baseline_labels} clean labels, "
            f"{feedback_report.excluded_labels} excluded labels, "
            f"{feedback_report.excluded_rows} rows removed"
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
        elif benchmark_diff["regressed"]:
            symbol, status = "✗", "regression detected vs baseline"
        else:
            symbol, status = "✓", "ok"
        print(f"  {symbol} benchmark  →  {benchmark_report_path}  {status}")
    print()


if __name__ == "__main__":
    sys.exit(main())
