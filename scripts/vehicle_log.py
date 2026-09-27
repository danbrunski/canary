#!/usr/bin/env python3
"""Record / list the vehicle maintenance-intervention ledger (Task 3.3).

Offline companion to the `record_vehicle_event` / `list_vehicle_events` MCP
tools — no running daemon required. Named ``vehicle_log.py`` (not
``ledger.py``, which is the training-run ledger CLI).

    .venv/bin/python scripts/vehicle_log.py add --occurred 2026-07-19 \\
        --kind maintenance --title "Oil change" --odometer 145000
    .venv/bin/python scripts/vehicle_log.py add --occurred 2026-07-19 \\
        --kind mod --title "Cold air intake" --channel obd:MAF --boundary
    .venv/bin/python scripts/vehicle_log.py list
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

try:
    from _cli import (
        add_common_args,
        load_config,
    )
except ImportError:  # loaded as `scripts.vehicle_log` — repo root on path, not scripts/
    from scripts._cli import (  # type: ignore[no-redef]
        add_common_args,
        load_config,
    )

from src.vehicle.profiles import resolve_profile_for_tooling, scope_config  # noqa: E402
from src.vehicle.vehicle_ledger import VEHICLE_EVENT_KINDS, VehicleLedgerStore  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vehicle_log", description="Vehicle maintenance/intervention ledger tool."
    )
    add_common_args(parser)
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Vehicle profile slug to scope the ledger to. Default: the vehicle: block's profile.",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="Ledger path override. Default: the (profile-scoped) daemon.vehicle_ledger.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Append a new event.")
    p_add.add_argument("--occurred", required=True, help="Date it happened, YYYY-MM-DD.")
    p_add.add_argument("--kind", required=True, choices=sorted(VEHICLE_EVENT_KINDS))
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--details", default="")
    p_add.add_argument("--odometer", type=float, default=None, help="Odometer reading, km.")
    p_add.add_argument(
        "--channel",
        action="append",
        dest="channels",
        default=None,
        help="Affected channel (repeatable), e.g. --channel obd:COOLANT_TEMP.",
    )
    p_add.add_argument(
        "--boundary",
        action="store_true",
        help="Mark this event as a corpus boundary (changes what 'normal' looks like).",
    )

    sub.add_parser("list", help="List events (latest per id, oldest occurrence first).")

    return parser.parse_args(argv)


def _resolve_store(args: argparse.Namespace) -> VehicleLedgerStore:
    config = load_config(args)
    scoped = (
        scope_config(config, resolve_profile_for_tooling(config, args.profile))
        if config.profiles.enabled
        else config
    )
    path = args.ledger or scoped.daemon.vehicle_ledger
    return VehicleLedgerStore(path)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    store = _resolve_store(args)

    if args.command == "add":
        try:
            occurred_at = date.fromisoformat(args.occurred)
        except ValueError:
            print(f"error: --occurred must be YYYY-MM-DD, got {args.occurred!r}", file=sys.stderr)
            return 1
        event = store.record(
            occurred_at=occurred_at,
            kind=args.kind,
            title=args.title,
            details=args.details,
            odometer_km=args.odometer,
            affected_channels=args.channels,
            corpus_boundary=args.boundary,
        )
        print(f"recorded {event.id}  {event.occurred_at}  {event.kind}  {event.title}")
        return 0

    if args.command == "list":
        records = store.records()
        if not records:
            print("(no vehicle events recorded)")
            return 0
        for e in records:
            boundary = " [boundary]" if e.corpus_boundary else ""
            print(f"{e.occurred_at}  {e.kind:<11}  {e.title}{boundary}  ({e.id})")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
