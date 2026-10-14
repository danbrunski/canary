#!/usr/bin/env python3
"""Replay recorded Parquet sessions through preprocessing and optional IF scoring.

This is the no-hardware path for exercising the model pipeline:

  session Parquet -> wide frame -> resample -> segment -> rolling features
      -> optional IsolationForest score

The script intentionally reports intermediate row/column counts. CAN-only
captures currently produce payload rows but no useful numeric value channels;
that should show up clearly in the report rather than failing mysteriously.

The operation itself is :func:`src.analysis.replay.replay_sessions` (R12); this
script owns only argument parsing, the JSON printed to stdout and the exit code.
``ReplayFrames``, ``ReplayResult`` and ``replay_sessions`` are re-exported here
for programmatic callers that predate the move.

Behavior *events* (what the daemon's behavior detector would persist, emit
and fuse, served set vs a candidate) are not this script's job: use
``tools/replay_behavior_compare.py`` (B03.7), which ticks the live pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

try:
    from _cli import add_common_args, load_scoped_config  # type: ignore[import-not-found]
except ImportError:  # loaded as `scripts.replay` — repo root on path, not scripts/
    from scripts._cli import add_common_args, load_scoped_config

from src.analysis.replay import ReplayFrames as ReplayFrames
from src.analysis.replay import ReplayResult as ReplayResult
from src.analysis.replay import replay_sessions


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="replay",
        description=(
            "Replay one or more Parquet session directories through CANary's "
            "preprocessing pipeline and optionally score with an IsolationForest artifact."
        ),
    )
    parser.add_argument("session_dir", type=Path, nargs="+")
    add_common_args(parser, help="Path to canary.yaml (default: ./canary.yaml).")
    parser.add_argument(
        "--model",
        type=Path,
        default=None,
        help="Optional IsolationForest joblib artifact to score after feature generation.",
    )
    parser.add_argument(
        "--value",
        choices=["value_number", "value_bool"],
        default="value_number",
        help="Telemetry value column to pivot into numeric features (default: value_number).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON report.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        # The daemon's featurization reads the scoped config (P02.11).
        config = load_scoped_config(args, None)
        result = replay_sessions(
            args.session_dir,
            config,
            value=args.value,
            model_path=args.model,
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    indent = 2 if args.pretty else None
    print(json.dumps(result.report, indent=indent, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
