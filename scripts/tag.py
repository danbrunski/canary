#!/usr/bin/env python3
"""Append a labeled tag to a recording session's `tags.jsonl`.

Usage:
  scripts/tag.py "steering full left"
  scripts/tag.py "phase_start" --kind phase_start --field phase=wheel_speed
  scripts/tag.py "consult_iac" --session data/sessions/2026-05-10T203045

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
import sys
from collections.abc import Sequence
from pathlib import Path

from src.collection.tags import find_latest_session, write_tag


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


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        fields = _parse_fields(args.field)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    session = args.session if args.session is not None else find_latest_session(args.sessions_root)
    record = write_tag(session, args.label, kind=args.kind, **fields)
    # Echo the written record so the user has confirmation + a copy-pasteable
    # JSON line they can grep for in tags.jsonl later.
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
