#!/usr/bin/env python3
"""Append `kind="invalidate"` corrections to a session's tags.jsonl.

Use when a tag fired but the physical action didn't happen at that moment —
e.g. the driver hit Enter on `hvac_ac_on` but the A/C compressor didn't
actually engage, or a sharp-turn tag was followed by traffic that prevented
the turn. Invalidating the tag tells the analyzer to skip the corresponding
window during binding differentials, without rewriting history (tags.jsonl
stays append-only).

Usage:
  # invalidate every occurrence of a label in the latest session
  scripts/correct_tag.py --invalidate hvac_ac_on

  # invalidate multiple labels at once
  scripts/correct_tag.py --invalidate hvac_ac_on --invalidate right_turn_sharp

  # narrow to one specific occurrence by exact timestamp (when a label is
  # tagged multiple times in one session and only some are bad)
  scripts/correct_tag.py --invalidate hvac_ac_on --at 1778810686.205896

  # target a specific session instead of the latest
  scripts/correct_tag.py --session data/sessions/2026-05-15T020322 \\
    --invalidate hvac_ac_on --reason "AC compressor never engaged"
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from src.collection.tags import find_latest_session, write_correction
from src.session import TAGS_FILENAME, load_tags


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="correct_tag",
        description=(
            "Append invalidate-kind corrections to a session's tags.jsonl. "
            "The analyzer drops windows whose start tag has been invalidated."
        ),
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
        "--invalidate",
        action="append",
        default=[],
        metavar="LABEL",
        help=(
            "Label of an event tag to invalidate. Pass multiple times to "
            "invalidate several labels in one go. Without --at, invalidates "
            "every occurrence of LABEL in the session."
        ),
    )
    parser.add_argument(
        "--at",
        type=float,
        default=None,
        help=(
            "When set, narrow the invalidation to the single event tag with "
            "this exact unix timestamp. Only valid with a single --invalidate."
        ),
    )
    parser.add_argument(
        "--reason",
        default=None,
        help="Free-form note stored on each correction record.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the session's current (corrected) tag list and exit.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    if args.session is not None:
        session = args.session
    else:
        try:
            session = find_latest_session(args.sessions_root)
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    if not session.is_dir():
        print(f"error: not a directory: {session}", file=sys.stderr)
        return 1

    if args.list:
        tags = load_tags(session / TAGS_FILENAME)
        if not tags:
            print(f"(no tags in {session})")
            return 0
        print(f"# {session} — {len(tags)} effective tag(s) after corrections")
        for t in sorted(tags, key=lambda x: float(x["t"])):
            print(f"  {float(t['t']):.6f}  {t['label']}")
        return 0

    if not args.invalidate:
        print("error: nothing to do — pass --invalidate LABEL or --list", file=sys.stderr)
        return 1

    if args.at is not None and len(args.invalidate) != 1:
        print(
            "error: --at narrows a single label; pass only one --invalidate with it",
            file=sys.stderr,
        )
        return 1

    written = 0
    for label in args.invalidate:
        record = write_correction(
            session,
            label,
            target_t=args.at,
            reason=args.reason,
        )
        scope = f"@t={args.at}" if args.at is not None else "all occurrences"
        print(f"  ✓ invalidated {label} ({scope}) → {record['t']:.6f}")
        written += 1

    print(f"\nwrote {written} correction record(s) to {session / TAGS_FILENAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
