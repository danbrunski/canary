#!/usr/bin/env python3
"""Report corpus health + "ready to train?" verdict (Task 6.23).

    .venv/bin/python scripts/dataset_health.py
    .venv/bin/python scripts/dataset_health.py --json
    .venv/bin/python scripts/dataset_health.py --sessions-root data/sessions

Run this before kicking off a retrain — it tells you whether the corpus is
thick and clean enough, and exactly what to capture next if it isn't.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import CanaryConfig  # noqa: E402
from src.datasets.health import analyze_corpus  # noqa: E402
from src.log import configure_logging  # noqa: E402
from src.preprocessing.loader import discover_sessions  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="dataset_health", description=__doc__)
    p.add_argument("--config", type=Path, default=Path("canary.yaml"))
    p.add_argument("--sessions-root", type=Path, default=None)
    p.add_argument("--json", action="store_true", help="Emit the full report as JSON.")
    p.add_argument("--min-sessions", type=int, default=3)
    p.add_argument("--min-rows", type=int, default=1000)
    p.add_argument("--log-level", default="WARNING")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = CanaryConfig.from_yaml(args.config)
    root = args.sessions_root or config.collection.storage_path
    sessions = discover_sessions(root) if root.exists() else []
    report = analyze_corpus(
        sessions, config, min_sessions=args.min_sessions, min_total_rows=args.min_rows
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
        return 0 if report.readiness.ready else 2

    print("=" * 72)
    print(f"DATASET HEALTH  ({len(report.sessions)} sessions, {root})")
    print("=" * 72)
    print(f"  total rows:     {report.total_rows}")
    print(f"  total duration: {report.total_duration_seconds:.0f}s")
    print(f"  mode coverage:  {report.mode_coverage or '—'}")
    print()
    for s in report.sessions:
        flag = "📌" if s.pinned else ("★" if s.training_candidate else " ")
        warn = f"  ⚠ {'; '.join(s.warnings)}" if s.warnings else ""
        print(f"  {flag} {s.session_id}  rows={s.row_count}  modes={s.mode_coverage or '—'}{warn}")
    print()
    verdict = "READY ✓" if report.readiness.ready else "NOT READY ✗"
    print(f"  retrain readiness: {verdict}")
    for reason in report.readiness.reasons:
        print(f"    - {reason}")
    return 0 if report.readiness.ready else 2


if __name__ == "__main__":
    sys.exit(main())
