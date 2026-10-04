#!/usr/bin/env python3
"""Add the tyre rolling circumference to stored trip digests (tyre wear).

Trip digests written before the field existed have no ``rolling_circumference``,
so ``/control/tyres`` has nothing to trend until new trips arrive. This
measures each session with a ``digest.json`` and merges the result in, touching
only that key. A session without a digest is skipped and counted: computing a
whole digest late would end its anomaly window at "now" (A06.5).

It reads two channels per session (rpm and GPS speed), but run it on the Pi only
with the truck parked, never while it collects (AGENTS.md).

Usage:
  scripts/backfill_rolling_circumference.py --config canary.yaml [--force] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from src.analysis.digest import _rolling_circumference
from src.analysis.rolling_circumference import METHOD_VERSION
from src.config import CanaryConfig
from src.storage.session_products import DIGEST_FILENAME, read_digest
from src.storage.sessions import is_session_dir_name
from src.storage.sidecar import write_json_sidecar
from src.vehicle.profiles import resolve_profile_for_tooling, scope_config


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path("canary.yaml"))
    parser.add_argument("--profile", default=None, help="Vehicle profile slug.")
    parser.add_argument(
        "--force", action="store_true", help="Re-measure digests that already have the field."
    )
    parser.add_argument("--dry-run", action="store_true", help="Measure and print; write nothing.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    base = CanaryConfig.from_yaml(args.config)
    config = scope_config(base, resolve_profile_for_tooling(base, args.profile))
    root = config.collection.storage_path
    counts = {"measured": 0, "unavailable": 0, "current": 0, "no_digest": 0}
    for session_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if not is_session_dir_name(session_dir.name):
            continue
        digest = read_digest(session_dir)
        if digest is None:
            counts["no_digest"] += 1
            continue
        existing = digest.get("rolling_circumference")
        if (
            not args.force
            and isinstance(existing, dict)
            and existing.get("method_version") == METHOD_VERSION
        ):
            counts["current"] += 1
            continue
        block = _rolling_circumference(session_dir, config)
        counts["measured" if block.get("available") else "unavailable"] += 1
        print(session_dir.name, json.dumps(block, default=str))
        if not args.dry_run:
            write_json_sidecar(
                session_dir / DIGEST_FILENAME, {**digest, "rolling_circumference": block}
            )
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
