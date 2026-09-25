#!/usr/bin/env python3
"""Re-verify catalogued RAM addresses against known-answer DID relations."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from src.analysis.memory_verification import (
    ChannelVerdict,
    RestampRefusedError,
    plan_restamp,
    restamp,
    verify_channel,
)
from src.decode.dids import load_did_map
from src.paths import DIDS_YAML
from tools._bus_tool import (
    PASSIVE_REFUSED_EXIT,
    PassiveRefusedError,
    add_bus_tool_args,
    open_client,
    parse_header,
    require_passive,
    resolve_session,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    add_bus_tool_args(parser, passive_check_seconds=30.0)
    parser.add_argument("--dids", type=Path, default=DIDS_YAML)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--channel", action="append", default=[])
    parser.add_argument("--record-revision", default=None)
    parser.add_argument("--carry-unverifiable", default=None, metavar="REASON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        request_id = parse_header(args.header)
        session = resolve_session(args.session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.samples <= 0:
        print("error: --samples must be positive", file=sys.stderr)
        return 1
    did_map = load_did_map(args.dids)
    channels = list(did_map.memory)
    if args.channel:
        wanted = {name.strip().upper() for name in args.channel}
        channels = [channel for channel in channels if channel.name.upper() in wanted]
        missing = wanted - {channel.name.upper() for channel in channels}
        if missing:
            print(f"error: no such memory channel: {', '.join(sorted(missing))}", file=sys.stderr)
            return 1
    if not channels:
        print(f"error: {args.dids} declares no memory channels", file=sys.stderr)
        return 1
    try:
        passive = require_passive(args)
    except PassiveRefusedError as exc:
        print(str(exc), file=sys.stderr)
        return PASSIVE_REFUSED_EXIT
    verdicts: list[ChannelVerdict] = []
    with open_client(args, passive) as client:
        for channel in channels:
            verdicts.append(
                verify_channel(
                    client,
                    channel,
                    samples=args.samples,
                    request_id=request_id,
                    session=session,
                )
            )
    for verdict in verdicts:
        print(verdict.render())
    failed = [verdict.channel for verdict in verdicts if verdict.outcome == "fail"]
    if args.record_revision and not failed:
        stamps, notes = plan_restamp(channels, verdicts, carry_reason=args.carry_unverifiable)
        try:
            changed = restamp(args.dids, stamps, args.record_revision)
        except RestampRefusedError as exc:
            print(f"not recording: {exc}", file=sys.stderr)
            return 1
        print(f"stamped {changed} channel(s) as verified on {args.record_revision}")
        for note in notes:
            print(f"  {note}", file=sys.stderr)
    elif args.record_revision:
        print(f"not recording: failed channels: {', '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ChannelVerdict",
    "RestampRefusedError",
    "main",
    "plan_restamp",
    "restamp",
    "verify_channel",
]
