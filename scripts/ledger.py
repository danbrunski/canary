#!/usr/bin/env python3
"""Inspect / maintain the training-run ledger (Task 6.21).

The retrain CLI ingests manifests automatically; this is the operator-facing
companion for reviewing history and recording publish decisions without a
running daemon (the companion-app / offline workflow needs exactly this).

    .venv/bin/python scripts/ledger.py list
    .venv/bin/python scripts/ledger.py ingest models/lstm_autoencoder/model.manifest.json
    .venv/bin/python scripts/ledger.py publish <run_id> --status active

Task 3.3/3.4 additions (online-training-strategy.md, "closing the loop"):

    # gated: only sets active if the gate passes
    .venv/bin/python scripts/ledger.py promote <run_id>
    # explicit operator assertion, never automatic
    .venv/bin/python scripts/ledger.py golden <run_id>
    # live-vs-golden divergence
    .venv/bin/python scripts/ledger.py golden-status <model_type>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

try:
    import _cli  # run as a file: scripts/ is on sys.path; bootstraps the repo root
except ImportError:  # loaded as `scripts.ledger` — repo root on path, not scripts/
    import scripts._cli as _cli  # type: ignore[no-redef]

from src.models.manifest import ManifestValidationError  # noqa: E402
from src.models.promotion_gate import (  # noqa: E402
    DEFAULT_AGREEMENT_TOLERANCE,
    DEFAULT_FP_BUDGET_PER_HOUR,
    DEFAULT_MAX_QUARANTINE_RELATIVE_RISE,
    DEFAULT_MAX_REJECT_FRACTION,
    DEFAULT_QUARANTINE_TREND_WINDOW,
    DEFAULT_RECALL_TOLERANCE,
    compute_golden_divergence,
    promote_challenger,
)
from src.models.training_ledger import (  # noqa: E402
    DEFAULT_TRAINING_LEDGER_PATH,
    TrainingLedger,
    TrainingRunRecord,
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

    p_promote = sub.add_parser(
        "promote",
        help=(
            "Champion/challenger promotion gate (Task 3.3): validate run_id "
            "against the golden run and, only if it passes, set it active. "
            "Never automatic — this is the explicit operator action the gate "
            "requires."
        ),
    )
    p_promote.add_argument("run_id")
    p_promote.add_argument(
        "--golden-run-id",
        default=None,
        help="Golden run to validate against (default: the run_id's model_type's "
        "declared golden run, if any).",
    )
    p_promote.add_argument("--recall-tolerance", type=float, default=DEFAULT_RECALL_TOLERANCE)
    p_promote.add_argument("--fp-budget-per-hour", type=float, default=DEFAULT_FP_BUDGET_PER_HOUR)
    p_promote.add_argument("--agreement-tolerance", type=float, default=DEFAULT_AGREEMENT_TOLERANCE)
    p_promote.add_argument("--max-reject-fraction", type=float, default=DEFAULT_MAX_REJECT_FRACTION)
    p_promote.add_argument(
        "--quarantine-trend-window", type=int, default=DEFAULT_QUARANTINE_TREND_WINDOW
    )
    p_promote.add_argument(
        "--max-quarantine-relative-rise",
        type=float,
        default=DEFAULT_MAX_QUARANTINE_RELATIVE_RISE,
    )
    p_promote.add_argument("--notes", default=None)

    p_golden = sub.add_parser(
        "golden", help="Declare run_id the frozen golden baseline for its model_type (Task 3.4)."
    )
    p_golden.add_argument("run_id")
    p_golden.add_argument("--notes", default=None)
    _cli.add_common_args(p_golden)
    _cli.add_bindings_arg(p_golden)
    p_golden.add_argument(
        "--no-freeze-baselines",
        action="store_true",
        help=(
            "Skip re-freezing this model_type's disposition baselines (Task 3.7) from "
            "run_id's recorded input_sessions. On by default -- 'declare golden' also "
            "re-freezes the baselines classify_dispositions screens future windows "
            "against; best-effort either way, never fails the golden declaration."
        ),
    )

    p_golden_status = sub.add_parser(
        "golden-status",
        help="Print the live-vs-golden divergence for model_type (Task 3.4).",
    )
    p_golden_status.add_argument("model_type")
    p_golden_status.add_argument(
        "--current-run-id",
        default=None,
        help="Run to compare against golden (default: the current active run of "
        "model_type, if any).",
    )
    return parser.parse_args(argv)


def _freeze_golden_baselines(
    updated: TrainingRunRecord, ledger_path: Path, args: argparse.Namespace
) -> Path | None:
    """Best-effort re-freeze of ``updated.model_type``'s golden disposition
    baselines (Task 3.7) from its recorded ``input_sessions``, right after
    ``ledger golden`` declares it — "re-declaring golden re-freezes the
    baselines" per the doc. Never fails the golden declaration itself: a
    session that moved/was archived since training, or a config/bindings
    mismatch, just means no sidecar is (re)written this time —
    ``classify_dispositions`` falls back to window-local screening, same as
    when no golden exists at all. Returns the sidecar path on success.
    """
    try:
        import pandas as pd

        from src.analysis.training_disposition import compute_golden_baselines
        from src.models.golden_baselines import golden_baselines_path, write_golden_baselines
        from src.preprocessing.feature_pipeline import (
            candidate_feature_columns,
            disposition_screen_frames,
        )

        config = _cli.load_config(args)
        sessions = [Path(s) for s in updated.input_sessions if Path(s).exists()]
        if not sessions:
            return None
        framed_parts, _reject_counts = disposition_screen_frames(
            sessions, config, bindings_path=args.bindings
        )
        if not framed_parts:
            return None
        combined = pd.concat(framed_parts, ignore_index=True)
        channels = candidate_feature_columns(combined)
        baselines = compute_golden_baselines(combined, channels=channels)
        path = golden_baselines_path(ledger_path, updated.model_type)
        write_golden_baselines(path, baselines)
        return path
    except Exception:  # noqa: BLE001 — best-effort, see docstring
        return None


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

    if args.command == "promote":
        result, updated = promote_challenger(
            ledger,
            args.run_id,
            golden_run_id=args.golden_run_id,
            recall_tolerance=args.recall_tolerance,
            fp_budget_per_hour=args.fp_budget_per_hour,
            agreement_tolerance=args.agreement_tolerance,
            max_reject_fraction=args.max_reject_fraction,
            quarantine_trend_window=args.quarantine_trend_window,
            max_quarantine_relative_rise=args.max_quarantine_relative_rise,
            notes=args.notes,
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        if not result.eligible:
            print(f"error: {args.run_id} is not eligible for promotion", file=sys.stderr)
            return 1
        assert updated is not None
        print(f"{updated.run_id} → {updated.publish_status}")
        return 0

    if args.command == "golden":
        updated = ledger.set_golden(args.run_id, notes=args.notes)
        if updated is None:
            print(f"error: unknown run_id {args.run_id!r}", file=sys.stderr)
            return 1
        print(f"{updated.run_id} declared golden ({updated.model_type})")
        if not args.no_freeze_baselines:
            sidecar = _freeze_golden_baselines(updated, args.ledger, args)
            if sidecar is not None:
                print(f"froze disposition baselines -> {sidecar}")
            else:
                print(
                    "note: could not freeze disposition baselines this time "
                    "(unreadable input sessions?) -- classify_dispositions falls "
                    "back to window-local screening for this model_type"
                )
        return 0

    if args.command == "golden-status":
        golden = ledger.golden_run(args.model_type)
        if golden is None:
            print(f"error: no golden run declared for {args.model_type!r}", file=sys.stderr)
            return 1
        current = (
            ledger.get(args.current_run_id)
            if args.current_run_id
            else next(
                (
                    r
                    for r in ledger.records()
                    if r.model_type == args.model_type and r.publish_status == "active"
                ),
                None,
            )
        )
        if current is None:
            print(
                f"error: no current run to compare — pass --current-run-id or "
                f"declare {args.model_type!r} active",
                file=sys.stderr,
            )
            return 1
        divergence = compute_golden_divergence(golden, current)
        print(json.dumps(divergence, indent=2, sort_keys=True))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
