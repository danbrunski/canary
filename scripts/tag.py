#!/usr/bin/env python3
"""Append a labeled tag to a recording session's `tags.jsonl`.

Usage:
  scripts/tag.py "steering full left"
  scripts/tag.py "phase_start" --kind phase_start --field phase=wheel_speed
  scripts/tag.py "consult_iac" --session data/sessions/2026-05-10T203045

Backfilling tags from notes taken during a drive:
  scripts/tag.py "highway_entry" --timestamp 2026-05-11T15:42:30Z
  scripts/tag.py "first_hard_brake" --offset 22m30s
  scripts/tag.py "arrived" --offset 01:18:42

`--timestamp` accepts ISO 8601 (with or without timezone — naive is assumed
UTC) or a Unix epoch float. `--offset` accepts a duration relative to the
session's `started_at` in any of: plain seconds (`750`), suffix-form
(`12m`, `1h30m`, `90s`, `1h 2m 3s`), or colon-form (`12:30` mm:ss or
`01:12:30` hh:mm:ss). The two flags are mutually exclusive; omit both for
"now".

By default the tag is written to the most recently created session under
`--sessions-root` (default `data/sessions`), so during a live recording you
can just `scripts/tag.py "label"` from a second terminal without copy-pasting
the session path.

Extra fields go via `--field key=value` (repeatable). Values are parsed as
JSON when possible, otherwise treated as strings — so `--field n=3` stores
the int 3, `--field name=foo` stores the string "foo".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from src.collection.tags import find_latest_session, write_tag

# Matches "1h", "30m", "90s", "1h30m", "1h 2m 3s", etc. Any subset of h/m/s.
_DURATION_SUFFIX_RE = re.compile(
    r"^\s*(?:(\d+(?:\.\d+)?)\s*h)?"
    r"\s*(?:(\d+(?:\.\d+)?)\s*m)?"
    r"\s*(?:(\d+(?:\.\d+)?)\s*s)?\s*$"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tag", description="Tag a recording session.")
    parser.add_argument("label", help="Short human-readable label for this event.")
    parser.add_argument(
        "--kind",
        default="event",
        help="Tag kind (e.g. event, phase_start, phase_end). Default: event.",
    )
    parser.add_argument(
        "--session",
        type=Path,
        default=None,
        help="Session directory. Defaults to the latest under --sessions-root.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path("data/sessions"),
        help="Where to look for the latest session. Default: ./data/sessions.",
    )
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra field; repeatable. Values parsed as JSON when possible.",
    )
    when = parser.add_mutually_exclusive_group()
    when.add_argument(
        "--timestamp",
        default=None,
        help=(
            "Backfill at a specific wall-clock time. Accepts ISO 8601 "
            "(e.g. '2026-05-11T15:42:30Z'; naive is assumed UTC) or a "
            "Unix epoch float. Mutually exclusive with --offset."
        ),
    )
    when.add_argument(
        "--offset",
        default=None,
        help=(
            "Backfill at a duration relative to the session's started_at. "
            "Accepts plain seconds ('750'), suffix-form ('12m', '1h30m', "
            "'90s'), or colon-form ('12:30' mm:ss, '01:12:30' hh:mm:ss). "
            "Mutually exclusive with --timestamp."
        ),
    )
    return parser.parse_args(argv)


def _parse_fields(raw: list[str]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for item in raw:
        if "=" not in item:
            raise ValueError(f"--field {item!r}: expected KEY=VALUE")
        key, value = item.split("=", 1)
        try:
            fields[key] = json.loads(value)
        except json.JSONDecodeError:
            fields[key] = value
    return fields


def parse_timestamp(s: str) -> float:
    """Parse `--timestamp` input into a UTC epoch float.

    Accepts ISO 8601 (with or without timezone; naive → UTC) or a Unix
    epoch number. Raises ValueError on anything else.
    """
    stripped = s.strip()
    # Try Unix epoch first — pure number is the common case for log copy-paste.
    try:
        return float(stripped)
    except ValueError:
        pass
    # ISO 8601. Python's fromisoformat accepts "Z" suffix as of 3.11.
    try:
        dt = datetime.fromisoformat(stripped)
    except ValueError as e:
        raise ValueError(f"--timestamp {s!r}: not a Unix epoch or ISO 8601 datetime") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def parse_duration(s: str) -> float:
    """Parse `--offset` input into a seconds float.

    Forms accepted:
      * plain number      → seconds (`750` → 750.0)
      * suffix-form       → `12m`, `1h30m`, `90s`, `1h 2m 3s`
      * colon-form mm:ss  → `12:30`  → 750.0
      * colon-form hh:mm:ss → `01:12:30` → 4350.0
    """
    stripped = s.strip()
    if not stripped:
        raise ValueError("--offset: empty value")

    # Plain number → seconds.
    try:
        return float(stripped)
    except ValueError:
        pass

    # Colon-separated: 2 parts = mm:ss, 3 parts = hh:mm:ss. Reject 1- or 4-part.
    if ":" in stripped:
        parts = stripped.split(":")
        if len(parts) == 2:
            mins, secs = parts
            return float(mins) * 60 + float(secs)
        if len(parts) == 3:
            hours, mins, secs = parts
            return float(hours) * 3600 + float(mins) * 60 + float(secs)
        raise ValueError(f"--offset {s!r}: colon-form must be mm:ss or hh:mm:ss")

    # Suffix-form (h/m/s, any subset).
    match = _DURATION_SUFFIX_RE.match(stripped)
    if match is None or not any(match.groups()):
        raise ValueError(f"--offset {s!r}: not a plain number, hh:mm:ss, or h/m/s suffix form")
    hours, mins, secs = match.groups()
    return (
        (float(hours) if hours else 0.0) * 3600
        + (float(mins) if mins else 0.0) * 60
        + (float(secs) if secs else 0.0)
    )


def _load_session_start(session_dir: Path) -> float | None:
    """Read `manifest.json`'s `started_at` into a UTC epoch float; None if missing."""
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return None
    started_at = manifest.get("started_at")
    if not isinstance(started_at, str):
        return None
    try:
        dt = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _load_session_end(session_dir: Path) -> float | None:
    """Read `manifest.json`'s `ended_at` into a UTC epoch float; None if missing."""
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return None
    ended_at = manifest.get("ended_at")
    if not isinstance(ended_at, str):
        return None
    try:
        dt = datetime.fromisoformat(ended_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _warn_if_outside_session(
    timestamp: float, session_dir: Path, stream: TextIO | None = None
) -> None:
    """Print a stderr warning if `timestamp` falls outside the session bounds.

    Bounds come from the manifest; if it's missing or unparseable we stay
    quiet — there's no ground truth to check against. `stream` defaults to
    the current `sys.stderr` (looked up at call time so pytest's capsys
    monkey-patch is honored).
    """
    out: TextIO = stream if stream is not None else sys.stderr
    started = _load_session_start(session_dir)
    if started is not None and timestamp < started:
        print(
            f"warning: timestamp {timestamp:.3f} is before session start "
            f"({started:.3f}); tag will sort before all frames.",
            file=out,
        )
    ended = _load_session_end(session_dir)
    if ended is not None and timestamp > ended:
        print(
            f"warning: timestamp {timestamp:.3f} is after session end "
            f"({ended:.3f}); tag will sort after all frames.",
            file=out,
        )


def _resolve_timestamp(args: argparse.Namespace, session_dir: Path) -> float | None:
    """Resolve --timestamp / --offset into an epoch float, or None for 'now'."""
    if args.timestamp is not None:
        return parse_timestamp(args.timestamp)
    if args.offset is not None:
        offset_seconds = parse_duration(args.offset)
        started = _load_session_start(session_dir)
        if started is None:
            raise ValueError(f"--offset requires a manifest with started_at in {session_dir}")
        return started + offset_seconds
    return None


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        fields = _parse_fields(args.field)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    session = args.session if args.session is not None else find_latest_session(args.sessions_root)

    try:
        resolved = _resolve_timestamp(args, session)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if resolved is not None:
        _warn_if_outside_session(resolved, session)

    record = write_tag(session, args.label, kind=args.kind, timestamp=resolved, **fields)
    # Echo the written record so the user has confirmation + a copy-pasteable
    # JSON line they can grep for in tags.jsonl later.
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
