#!/usr/bin/env python3
"""Retrain both anomaly detectors from recent sessions (Task 5.17).

Manual trigger in v1; cron later. The companion piece to `refit_behavior_models.py`:
runs the Isolation Forest trainer (`scripts/train_anomaly_model.train`) and
the LSTM autoencoder trainer (`scripts/train_lstm.train`) over the same
session set, side by side, in one process.

Each trainer writes its own ``model.joblib`` (or ``model.pt``) +
``model.manifest.json`` under a dedicated output directory. The
``ModelManager`` (5.16) running in the daemon picks up the new artifacts on
its next poll — no daemon restart needed, hot-swap as long as the schema
matches.

Usage:

    .venv/bin/python scripts/retrain.py
    .venv/bin/python scripts/retrain.py --days 14
    .venv/bin/python scripts/retrain.py --skip-lstm    # IF-only refresh
    .venv/bin/python scripts/retrain.py --skip-if      # LSTM-only refresh

Skips on failure: a broken LSTM trainer doesn't prevent the IF artifact from
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
from refit_behavior_models import _filter_sessions_by_age  # noqa: E402

from src.config import CanaryConfig  # noqa: E402
from src.log import configure_logging  # noqa: E402
from src.models.manifest import ManifestValidationError  # noqa: E402
from src.models.training_ledger import (  # noqa: E402
    DEFAULT_TRAINING_LEDGER_PATH,
    TrainingLedger,
    build_run_record_from_manifest,
)
from src.preprocessing import discover_sessions  # noqa: E402

log = logging.getLogger(__name__)


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
        "--lstm-out",
        type=Path,
        default=Path("models/lstm_autoencoder"),
        help="Output directory for the LSTM autoencoder artifact + manifest.",
    )
    parser.add_argument(
        "--skip-if",
        action="store_true",
        help="Skip the Isolation Forest trainer (LSTM-only refresh).",
    )
    parser.add_argument(
        "--skip-lstm",
        action="store_true",
        help="Skip the LSTM autoencoder trainer (IF-only refresh).",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="LSTM training device (cpu / cuda). Default cpu — Pi target.",
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
    config = CanaryConfig.from_yaml(args.config)

    if args.skip_if and args.skip_lstm:
        print("error: --skip-if and --skip-lstm together leave nothing to do.", file=sys.stderr)
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

    log.info(
        "starting retrain",
        extra={
            "sessions": len(sessions),
            "skip_if": args.skip_if,
            "skip_lstm": args.skip_lstm,
        },
    )

    results: dict[str, str] = {}

    ledger = None if args.no_ledger else TrainingLedger(args.ledger)

    if not args.skip_if:
        try:
            if_trainer.train(
                sessions,
                config,
                args.if_out,
                bindings_path=args.bindings,
            )
            results["isolation_forest"] = "ok"
            _ingest_ledger(ledger, args.if_out, "succeeded", args.job_id)
        except Exception as exc:  # noqa: BLE001 — surface + continue to the next trainer
            log.exception("isolation forest trainer failed", extra={"error": str(exc)})
            results["isolation_forest"] = f"failed: {exc}"

    if not args.skip_lstm:
        try:
            lstm_trainer.train(
                sessions,
                config,
                args.lstm_out,
                bindings_path=args.bindings,
                device=args.device,
            )
            results["lstm_autoencoder"] = "ok"
            _ingest_ledger(ledger, args.lstm_out, "succeeded", args.job_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("lstm trainer failed", extra={"error": str(exc)})
            results["lstm_autoencoder"] = f"failed: {exc}"

    _print_report(sessions, results, args)
    # Partial success is the documented contract (a broken LSTM must not block
    # the IF artifact, and vice versa — see the module docstring). Exit
    # non-zero ONLY when every requested trainer failed, so the caller
    # (sync-train.sh) still runs the independent behavior-model refit instead
    # of `set -e` aborting the whole pipeline on a partial run.
    succeeded = [k for k, v in results.items() if v == "ok"]
    if not succeeded:
        log.error("all requested trainers failed", extra={"results": results})
        return 1
    if len(succeeded) != len(results):
        log.warning(
            "partial retrain — ok: %s; failed: %s",
            ", ".join(succeeded),
            ", ".join(k for k, v in results.items() if v != "ok"),
        )
    return 0


def _ingest_ledger(
    ledger: TrainingLedger | None,
    out_dir: Path,
    status: str,
    job_id: str | None,
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
        ledger.append(record)
        log.info("recorded training run", extra={"run_id": record.run_id})
    except (ManifestValidationError, OSError) as exc:
        log.warning("ledger ingest skipped: %s", exc)


def _print_report(sessions: list[Path], results: dict[str, str], args: argparse.Namespace) -> None:
    print("=" * 72)
    print(f"RETRAIN  ({len(sessions)} sessions)")
    print("=" * 72)
    if "isolation_forest" in results:
        status = results["isolation_forest"]
        symbol = "✓" if status == "ok" else "✗"
        print(f"  {symbol} isolation_forest  →  {args.if_out}  {status}")
    if "lstm_autoencoder" in results:
        status = results["lstm_autoencoder"]
        symbol = "✓" if status == "ok" else "✗"
        print(f"  {symbol} lstm_autoencoder  →  {args.lstm_out}  {status}")
    print()


if __name__ == "__main__":
    sys.exit(main())
