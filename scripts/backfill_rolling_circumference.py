#!/usr/bin/env python3
"""Measure the tyre rolling circumference of stored sessions (tyre wear).

Writes each session's ``rolling_circumference.json``, the one input
``/control/tyres`` trends. Every session is measured, with or without a trip
digest: the drives the digest refused (the long ones) are the measurement's
best input, and it reads only two channels (rpm and GPS speed), so the digest's
budgets do not apply to it.

A digest written while the measurement was a digest field (2026-10-03) already
holds one; it is copied out rather than measured again.

Run it on the Pi only with the truck parked, never while it collects
(AGENTS.md).

Usage:
  scripts/backfill_rolling_circumference.py --config canary.yaml [--force] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from src.analysis.rolling_circumference import (
    METHOD_VERSION,
    measure_session,
    write_rolling_circumference,
)
from src.config import CanaryConfig
from src.storage.session_products import read_digest, read_rolling_circumference
from src.storage.sessions import is_session_dir_name
from src.vehicle.profiles import resolve_profile_for_tooling, scope_config


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path("canary.yaml"))
    parser.add_argument("--profile", default=None, help="Vehicle profile slug.")
    parser.add_argument(
        "--force", action="store_true", help="Re-measure sessions that already have one."
    )
    parser.add_argument("--dry-run", action="store_true", help="Measure and print; write nothing.")
    return parser.parse_args(argv)


def _current(block: object) -> bool:
    return isinstance(block, dict) and block.get("method_version") == METHOD_VERSION


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    base = CanaryConfig.from_yaml(args.config)
    config = scope_config(base, resolve_profile_for_tooling(base, args.profile))
    root = config.collection.storage_path
    counts = {"measured": 0, "unavailable": 0, "current": 0, "from_digest": 0}
    for session_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if not is_session_dir_name(session_dir.name):
            continue
        if not args.force and _current(read_rolling_circumference(session_dir)):
            counts["current"] += 1
            continue
        held = (read_digest(session_dir) or {}).get("rolling_circumference")
        if not args.force and _current(held):
            block = dict(held)  # type: ignore[arg-type]
            counts["from_digest"] += 1
        else:
            block = measure_session(session_dir, config)
            counts["measured" if block.get("available") else "unavailable"] += 1
        print(session_dir.name, json.dumps(block, default=str))
        if not args.dry_run:
            write_rolling_circumference(session_dir, block)
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
