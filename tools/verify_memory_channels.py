#!/usr/bin/env python3
"""Re-verify catalogued RAM addresses against known-answer DID relations."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from src.analysis.memory_verification import (
    ChannelVerdict,
    RestampRefusedError,
    plan_restamp,
    restamp,
    verify_channel,
)
from src.collection.can_collector import bus_factory
from src.collection.can_obd_client import CanObdClient
from src.config import CanaryConfig
from src.decode.dids import load_did_map
from src.paths import DIDS_YAML
from tools.did_log import (
    PASSIVE_REASON,
    _passive_problem,
    _resolve_session,
    _resolve_token,
    http_passive_checker,
)


@contextmanager
def _open_client(
    config_path: Path, *, request_id: int, read_timeout: float
) -> Iterator[CanObdClient]:
    config = CanaryConfig.from_yaml(config_path)
    bus = bus_factory(config.hardware.can)
    try:
        yield CanObdClient(
            bus,
            request_id=request_id,
            response_id_min=request_id + 8,
            response_id_max=request_id + 8,
            timeout_seconds=read_timeout,
        )
    finally:
        bus.shutdown()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("canary.yaml"))
    parser.add_argument("--dids", type=Path, default=DIDS_YAML)
    parser.add_argument("--header", default="7E0", help="Request CAN id (hex).")
    parser.add_argument("--session", default="auto")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--read-timeout", type=float, default=1.0)
    parser.add_argument("--channel", action="append", default=[])
    parser.add_argument("--record-revision", default=None)
    parser.add_argument("--carry-unverifiable", default=None, metavar="REASON")
    parser.add_argument("--daemon-url", default="http://localhost:8000")
    parser.add_argument("--passive-check-seconds", type=float, default=30.0)
    parser.add_argument("--no-daemon", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        request_id = int(args.header, 16)
        session = _resolve_session(args.session)
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
    if not args.no_daemon:
        problem = _passive_problem(
            http_passive_checker(args.daemon_url, _resolve_token())(),
            args.passive_check_seconds,
        )
        if problem is not None:
            print(
                f"refusing: daemon passive mode is not usable ({problem}); {PASSIVE_REASON}",
                file=sys.stderr,
            )
            return 2
    verdicts: list[ChannelVerdict] = []
    with _open_client(args.config, request_id=request_id, read_timeout=args.read_timeout) as client:
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
