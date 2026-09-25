#!/usr/bin/env python3
"""Offline motion-aware auto-tagger (Phase 2a.5, imu-gps-integration.md).

Runs the shared detection taxonomy (``src.analysis.autotag.detect_tags``)
over one or more sessions' resampled/segmented Parquet and appends the
resulting events to each session's ``tags.jsonl`` via the same
``src.collection.tags.write_tag`` every other tagging path uses — so every
existing consumer (analyzer windows, dataset export, correction flow) works
unchanged.

Idempotent / re-runnable: each candidate is skipped if a tag with the same
label already exists in ``tags.jsonl`` within a small time tolerance (so
re-running after tuning a threshold in ``canary.yaml``'s ``autotag:`` block
doesn't duplicate tags that already fired last time).

The GPS-only tags (``stopped``, ``highway_cruise``) work against any session
that has ``gps:speed`` — real stored sessions today. The IMU-dependent tags
(``hard_accel``/``hard_brake``/``cornering``/``launch``) are correctly wired
but dormant until a session actually has ``imu:*`` columns (Phase 2b
hardware) — running this against today's corpus simply produces none of
those labels, not an error.

Usage:

    .venv/bin/python scripts/autotag.py --session 2026-05-24T193045
    .venv/bin/python scripts/autotag.py --all
    .venv/bin/python scripts/autotag.py --session 2026-05-24T193045 --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from _cli import (
    add_common_args,
    add_log_level_arg,
    discover_live_session_dirs,
    load_config,
)

from src.analysis.autotag import TagEvent, detect_tags
from src.collection.tags import write_tag
from src.config import CanaryConfig
from src.log import configure_logging
from src.preprocessing.loader import resample_and_segment
from src.session import TAGS_FILENAME

log = logging.getLogger(__name__)

#: Auto-tagged records carry this in their fields so a later run (or a
#: manual tag with the same label) is never mistaken for one of ours, and
#: so idempotency only ever compares against the auto-tagger's own history.
_SOURCE_FIELD = "source"
_SOURCE_VALUE = "autotag"

#: Two identical-label auto-tags within this many wall-clock seconds are
#: treated as the same event — comfortably wider than one resample tick, so
#: re-running after a threshold tweak that shifts a peak by a row or two
#: still recognizes it as "already tagged" instead of duplicating it.
_DEDUPE_TOLERANCE_SECONDS = 2.0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="autotag",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        help="Storage root to scan. Defaults to collection.storage_path from --config.",
    )
    parser.add_argument(
        "--session",
        action="append",
        default=[],
        help="One session id (e.g. 2026-05-24T193045). Repeatable. Overrides --all.",
    )
    add_common_args(parser)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Auto-tag every session under --sessions-root.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect and log candidate tags but don't write tags.jsonl.",
    )
    add_log_level_arg(parser)
    return parser.parse_args(argv)


def _existing_autotag_records(session_dir: Path) -> list[dict[str, object]]:
    """Raw tag records this script previously wrote to ``session_dir``.

    Reads ``tags.jsonl`` directly rather than via ``load_tags`` — ``load_tags``
    silently drops any tag an operator later ``invalidate``d, which would
    make dedup blind to it and re-create the exact tag a correction just
    removed. Idempotency here only needs "did autotag already write this
    label at roughly this time", not the windowing semantics ``load_tags``
    is for.
    """
    path = session_dir / TAGS_FILENAME
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    with path.open("r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get(_SOURCE_FIELD) == _SOURCE_VALUE:
                records.append(record)
    return records


def _already_tagged(existing: list[dict[str, object]], event: TagEvent) -> bool:
    target_t = event.timestamp.timestamp()
    for record in existing:
        if record.get("label") != event.label:
            continue
        try:
            recorded_t = float(record["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(recorded_t - target_t) <= _DEDUPE_TOLERANCE_SECONDS:
            return True
    return False


def autotag_session(session_dir: Path, config: CanaryConfig, *, dry_run: bool = False) -> int:
    """Detect + write auto-tags for one session. Returns the count written."""
    frame = resample_and_segment([session_dir], config)
    if frame.empty:
        log.warning("no telemetry rows in %s; skipping", session_dir.name)
        return 0

    events = detect_tags(frame, config.autotag)
    existing = _existing_autotag_records(session_dir)
    written = 0
    for event in events:
        if _already_tagged(existing, event):
            continue
        if dry_run:
            log.info("[dry-run] %s: %s %s", session_dir.name, event.label, event.fields)
        else:
            write_tag(
                session_dir,
                event.label,
                timestamp=event.timestamp.timestamp(),
                **{_SOURCE_FIELD: _SOURCE_VALUE, **event.fields},
            )
        written += 1
    return written


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.log_level)

    config = load_config(args)
    sessions_root = args.sessions_root or config.collection.storage_path

    if args.session:
        targets = [sessions_root / name for name in args.session]
        missing = [path.name for path in targets if not path.is_dir()]
        if missing:
            log.error("session(s) not found under %s: %s", sessions_root, ", ".join(missing))
            return 1
    elif args.all:
        targets = discover_live_session_dirs(sessions_root)
    else:
        log.error("pass --session <id> (repeatable) or --all")
        return 1

    if not targets:
        log.warning("no sessions found at %s", sessions_root)
        return 0

    total = 0
    errors = 0
    for session_dir in targets:
        try:
            written = autotag_session(session_dir, config, dry_run=args.dry_run)
        except FileNotFoundError as exc:
            log.warning("skip %s: %s", session_dir.name, exc)
            errors += 1
            continue
        except Exception:  # noqa: BLE001 — keep the batch moving
            log.exception("autotag failed for %s", session_dir.name)
            errors += 1
            continue
        log.info("%s: %d tag(s) %s", session_dir.name, written, "(dry-run)" if args.dry_run else "")
        total += written

    log.info("done total_tags=%d errors=%d", total, errors)
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
