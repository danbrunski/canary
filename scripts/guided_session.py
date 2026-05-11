#!/usr/bin/env python3
"""Walk through a tag-protocol step-by-step, hitting Enter to tag each event.

Designed for in-vehicle use where typing `scripts/tag.py "label"` for every
event is impractical — driver-seat ergonomics. Run this in one terminal
alongside `scripts/record.py` in another. The script auto-finds the latest
session and writes each tag with the correct label as soon as you press
Enter.

Usage:
  scripts/guided_session.py                  # uses the 'rev' protocol
  scripts/guided_session.py --protocol rev
  scripts/guided_session.py --session data/sessions/<timestamp>

Quit with Ctrl-C at any time; tags written so far stay in tags.jsonl.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.collection.tags import find_latest_session, write_tag


@dataclass(frozen=True)
class Step:
    """One labeled event in a protocol.

    `advisory_duration_s` is just shown to the user as a hint — the script
    doesn't enforce timing. The user controls pace by when they press Enter.
    """

    label: str
    description: str
    advisory_duration_s: int | None = None


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

# Rev protocol: seated, parked, engine off → cold idle → warm idle → blips
# (2k/3k/4k) with idle bracketing → 2500 sustain → engine off again. Each
# step is one tag; the implicit window for each is [tag time, next tag time]
# so build_windows produces clean per-phase segments.
PROTOCOL_REV: list[Step] = [
    Step("key_on_engine_off", "Key ON, engine OFF. Just sit.", 30),
    Step("engine_start", "Crank the engine now. Hit Enter the moment it cranks.", None),
    Step("idle_cold", "Engine running, settling. Just idle.", 60),
    Step("idle_warm", "Engine warmer now. Keep idling.", 90),
    Step("blip_2k_start", "About to blip to ~2000 RPM. Hit Enter just BEFORE you blip.", None),
    Step("blip_2k_end", "Blip done. Hit Enter the moment you released the throttle.", None),
    Step("idle_recover_a", "Let it settle at idle.", 15),
    Step("blip_3k_start", "About to blip to ~3000 RPM.", None),
    Step("blip_3k_end", "Throttle released.", None),
    Step("idle_recover_b", "Idle again.", 15),
    Step("sustain_2500_start", "About to hold steady ~2500 RPM for ~15s.", None),
    Step("sustain_2500_end", "Releasing throttle.", None),
    Step("idle_recover_c", "Idle again.", 15),
    Step("blip_4k_start", "About to blip to ~4000 RPM.", None),
    Step("blip_4k_end", "Throttle released.", None),
    Step("idle_recover_d", "Final idle.", 15),
    Step("engine_off_key_on", "Kill engine, leave key ON.", 30),
]

PROTOCOLS: dict[str, list[Step]] = {
    "rev": PROTOCOL_REV,
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _format_header(step_num: int, total: int, label: str) -> str:
    bar = "=" * 78
    title = f" GUIDED PROTOCOL                                   Step {step_num}/{total}"
    return f"\n{bar}\n{title}\n{bar}\n\n  Next:  {label}"


def _format_prompt(step: Step) -> str:
    lines = [f"\n  {step.description}"]
    if step.advisory_duration_s is not None:
        lines.append(f"  (Suggested ~{step.advisory_duration_s}s before advancing.)")
    lines.append("\n  >>> Press Enter to tag and advance: ")
    return "\n".join(lines)


def run_step(
    step: Step,
    step_num: int,
    total: int,
    session: Path,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    write_tag_fn: Callable[..., dict[str, Any]] = write_tag,
) -> dict[str, Any]:
    """Display one step, await Enter, write the tag, return the record."""
    output_fn(_format_header(step_num, total, step.label))
    input_fn(_format_prompt(step))
    record = write_tag_fn(session, step.label, kind="event")
    ts = record["t"]
    output_fn(
        f"  ✓ Tagged '{step.label}' at "
        f"{time.strftime('%H:%M:%S', time.localtime(ts))}.{int((ts % 1) * 1000):03d}"
    )
    return record


def run_protocol(
    steps: list[Step],
    session: Path,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    write_tag_fn: Callable[..., dict[str, Any]] = write_tag,
) -> list[dict[str, Any]]:
    """Walk every step in order, returning the list of tag records written."""
    written: list[dict[str, Any]] = []
    total = len(steps)
    for i, step in enumerate(steps, start=1):
        record = run_step(
            step,
            i,
            total,
            session,
            input_fn=input_fn,
            output_fn=output_fn,
            write_tag_fn=write_tag_fn,
        )
        written.append(record)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="guided_session",
        description=(
            "Walk through a tag protocol step-by-step; hit Enter to fire each "
            "tag while you do the action. Pair with `scripts/record.py` "
            "running in another terminal."
        ),
    )
    parser.add_argument(
        "--protocol",
        default="rev",
        choices=sorted(PROTOCOLS.keys()),
        help="Which protocol to run.",
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    if args.session is not None:
        session = args.session
    else:
        try:
            session = find_latest_session(args.sessions_root)
        except (FileNotFoundError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            print(
                "Start `scripts/record.py` first; this script tags into its session.",
                file=sys.stderr,
            )
            return 1

    steps = PROTOCOLS[args.protocol]
    print(f"Tagging into session: {session}")
    print(f"Protocol: {args.protocol} ({len(steps)} steps)")
    print(
        "\nMake sure `scripts/record.py` is already running.\n"
        "Press Ctrl-C at any time to stop (tags so far are preserved)."
    )

    try:
        run_protocol(steps, session)
    except KeyboardInterrupt:
        print("\n\nInterrupted. Tags written so far are saved.")
        return 130
    except FileNotFoundError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1

    print("\n" + "=" * 78)
    print(" Protocol complete. Stop record.py when ready.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
