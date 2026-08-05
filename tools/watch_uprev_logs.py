#!/usr/bin/env python3
"""Import any UpRev logs that haven't been imported yet.

Laptop-side by design: the UpRev logger writes to a Windows directory the Pi
never sees (see the `uprev-importer-local-only` note), so this runs where the
CSVs land, not on the vehicle.

One-shot rather than a daemon — run it after a drive, or from cron. It skips
logs already imported (matched by original filename in each session manifest),
so re-running is free.

Timestamps come from the filename, which is the laptop's local clock with no
timezone; ``--timezone`` supplies the missing offset. With ``--align-session``
resolution enabled, each import additionally cross-correlates RPM against an
overlapping CANary session and refines that anchor — the two clocks really do
differ by seconds.

Usage:

    .venv/bin/python tools/watch_uprev_logs.py --logs-dir ~/UpRev\\ Logs \\
        --timezone -06:00 --config canary.dev.example.yaml
    .venv/bin/python tools/watch_uprev_logs.py --logs-dir ... --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import CanaryConfig

try:
    from import_uprev_log import _FILENAME_TIME  # type: ignore[import-not-found]
except ImportError:  # invoked as `tools/watch_uprev_logs.py`
    from tools.import_uprev_log import _FILENAME_TIME


def already_imported(uprev_root: Path) -> set[str]:
    """Original filenames recorded in existing ``_uprev`` session manifests."""
    seen: set[str] = set()
    if not uprev_root.exists():
        return seen
    for manifest in uprev_root.glob("*/manifest.json"):
        try:
            payload = json.loads(manifest.read_text())
        except (OSError, ValueError):
            continue
        name = payload.get("original_filename")
        if isinstance(name, str):
            seen.add(name)
    return seen


def filename_started_at(name: str, offset: timedelta) -> datetime | None:
    """Parse the filename's local timestamp and attach ``offset``."""
    match = _FILENAME_TIME.search(name)
    if match is None:
        return None
    hour = int(match.group("hour")) % 12
    if match.group("meridiem").upper() == "PM":
        hour += 12
    return datetime(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        hour,
        int(match.group("minute")),
        int(match.group("second")),
        tzinfo=timezone(offset),
    )


def parse_offset(raw: str) -> timedelta:
    """Parse ``-06:00`` / ``+01:00`` into a timedelta."""
    sign = -1 if raw.startswith("-") else 1
    body = raw.lstrip("+-")
    hours, _, minutes = body.partition(":")
    return sign * timedelta(hours=int(hours), minutes=int(minutes or 0))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="watch_uprev_logs", description=__doc__)
    parser.add_argument("--logs-dir", type=Path, required=True)
    parser.add_argument(
        "--timezone",
        default="-06:00",
        help="UTC offset the UpRev filenames are written in (they carry none).",
    )
    parser.add_argument("--config", type=Path, default=Path("canary.yaml"))
    parser.add_argument(
        "--align-session",
        default=None,
        help="Session id to refine the clock anchor against (see import_uprev_log).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Import at most N logs (0 = all).")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.logs_dir.exists():
        print(f"error: {args.logs_dir} does not exist", file=sys.stderr)
        return 1

    config = CanaryConfig.from_yaml(args.config)
    uprev_root = config.collection.storage_path / "_uprev"
    seen = already_imported(uprev_root)
    offset = parse_offset(args.timezone)

    candidates = sorted(p for p in args.logs_dir.glob("*.csv") if p.name not in seen)
    if not candidates:
        print(f"nothing to import ({len(seen)} already imported)")
        return 0

    imported = failed = 0
    for path in candidates:
        started_at = filename_started_at(path.name, offset)
        if started_at is None:
            # Autosaves and hand-renamed exports carry no parseable stamp;
            # they need an explicit --started-at through the importer itself.
            print(f"skip (no timestamp in filename): {path.name}")
            continue
        if args.dry_run:
            print(f"would import {path.name} at {started_at.isoformat()}")
            imported += 1
        else:
            cmd = [
                sys.executable,
                "tools/import_uprev_log.py",
                "--csv",
                str(path),
                "--started-at",
                started_at.isoformat(),
                "--config",
                str(args.config),
            ]
            if args.align_session:
                cmd += ["--align-session", args.align_session]
            result = subprocess.run(cmd, check=False)
            if result.returncode == 0:
                imported += 1
            else:
                # One unparseable log must not stop the sweep; the importer
                # already explained itself on stderr.
                failed += 1
        if args.limit and imported >= args.limit:
            break

    print(f"{'would import' if args.dry_run else 'imported'} {imported}, failed {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
