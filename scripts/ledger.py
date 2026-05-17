#!/usr/bin/env python3
"""Inspect / maintain the training-run ledger (Task 6.21).

The retrain CLI ingests manifests automatically; this is the operator-facing
companion for reviewing history and recording publish decisions without a
running daemon (the companion-app / offline workflow needs exactly this).

    .venv/bin/python scripts/ledger.py list
    .venv/bin/python scripts/ledger.py ingest models/lstm_autoencoder/model.manifest.json
    .venv/bin/python scripts/ledger.py publish <run_id> --status active
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models.manifest import ManifestValidationError  # noqa: E402
from src.models.training_ledger import (  # noqa: E402
    DEFAULT_TRAINING_LEDGER_PATH,
    TrainingLedger,
    build_run_record_from_manifest,
)

_PUBLISH_CHOICES = ["unpublished", "published", "active", "rolled_back"]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ledger", description="Training-run ledger tool.")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_TRAINING_LEDGER_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List runs (latest state per run).")

    p_ingest = sub.add_parser("ingest", help="Ingest a trainer manifest as a run record.")
    p_ingest.add_argument("manifest", type=Path)
    p_ingest.add_argument("--fp-benchmark", type=Path, default=None)
    p_ingest.add_argument("--job-id", default=None)
    p_ingest.add_argument("--notes", default=None)

    p_pub = sub.add_parser("publish", help="Set a run's publish status.")
    p_pub.add_argument("run_id")
    p_pub.add_argument("--status", choices=_PUBLISH_CHOICES, default="active")
    p_pub.add_argument("--notes", default=None)

    p_show = sub.add_parser("show", help="Print one run's full JSON.")
    p_show.add_argument("run_id")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    ledger = TrainingLedger(args.ledger)

    if args.command == "list":
        runs = ledger.records()
        if not runs:
            print("(no training runs recorded)")
            return 0
        for r in runs:
            print(
                f"{r.created_at}  {r.model_type:<18}  {r.status:<9}  "
                f"{r.publish_status:<11}  {r.run_id}"
            )
        return 0

    if args.command == "ingest":
        try:
            record = build_run_record_from_manifest(
                args.manifest,
                job_id=args.job_id,
                false_positive_benchmark_path=args.fp_benchmark,
                notes=args.notes,
            )
        except ManifestValidationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        ledger.append(record)
        print(f"recorded {record.run_id} ({record.model_type})")
        return 0

    if args.command == "publish":
        updated = ledger.set_publish_status(args.run_id, args.status, notes=args.notes)
        if updated is None:
            print(f"error: unknown run_id {args.run_id!r}", file=sys.stderr)
            return 1
        print(f"{updated.run_id} → {updated.publish_status}")
        return 0

    if args.command == "show":
        run = ledger.get(args.run_id)
        if run is None:
            print(f"error: unknown run_id {args.run_id!r}", file=sys.stderr)
            return 1
        print(json.dumps(run.to_dict(), indent=2, sort_keys=True))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
