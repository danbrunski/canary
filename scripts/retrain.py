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
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

# Reuse the trainer modules as importable libraries — same pattern
# refit_behavior_models.py uses.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import train_anomaly_model as if_trainer  # noqa: E402
import train_lstm as lstm_trainer  # noqa: E402
import train_tcn as tcn_trainer  # noqa: E402
from refit_behavior_models import _filter_sessions_by_age  # noqa: E402

from src.alerts.feedback import DEFAULT_FEEDBACK_PATH, FeedbackStore  # noqa: E402
from src.config import CanaryConfig  # noqa: E402
from src.log import configure_logging  # noqa: E402
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
    # discovered session. With fewer than 3 sessions we can't afford to hold
    # one out, so we log and fall back to in-sample.
    calibration_sessions: list[Path] = []
    if not args.skip_sequence:
        if args.calibration_sessions:
            requested = {Path(p).resolve() for p in args.calibration_sessions}
            calibration_sessions = [s for s in sessions if Path(s).resolve() in requested]
            sessions = [s for s in sessions if Path(s).resolve() not in requested]
        elif not args.no_calibration_holdout:
            if len(sessions) >= 3:
                # discover_sessions sorts ascending by capture time, so the last
                # entry is the freshest clean drive — the most representative
                # held-out anchor for fresh-drive deployment.
                calibration_sessions = [sessions[-1]]
                sessions = sessions[:-1]
            else:
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
            _ingest_ledger(
                ledger,
                args.if_out,
                "succeeded",
                args.job_id,
                feedback_report=feedback_report,
            )
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
            _ingest_ledger(
                ledger,
                args.sequence_out,
                "succeeded",
                args.job_id,
                feedback_report=feedback_report,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "sequence trainer failed", extra={"error": str(exc), "model": seq_model_type}
            )
            results[seq_model_type] = f"failed: {exc}"

    _print_report(sessions, results, args, feedback_report=feedback_report)
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
    print()


if __name__ == "__main__":
    sys.exit(main())
