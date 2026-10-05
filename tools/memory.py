#!/usr/bin/env python3
"""Read the vehicle's investigation memory: query records, render a card.

E01.3/E01.5 read path over the memory ledger (``daemon.memory_ledger``,
``control/<profile>/memory.jsonl`` under ``state_dir``). Read-only: nothing
here appends. The HTTP/MCP/dashboard surfaces are E04's and expose the same
query (:class:`src.evidence.MemoryQuery`) and card
(:class:`src.evidence.InvestigationCard`).

Usage:
  tools/memory.py --config canary.dev.example.yaml --profile nissan-xterra-2008 \\
      query --kind evidence --text "fuel pressure" --as-of 2026-09-10T00:00:00Z
  tools/memory.py --config ... card inv-0123456789abcdef01234567 [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _instant(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{text!r}: give a UTC offset (e.g. ...Z)")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="memory", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", type=Path, required=True, help="canary.yaml to scope by.")
    parser.add_argument("--profile", default=None, help="Vehicle profile slug.")
    parser.add_argument("--replay-id", default=None, help="Read a replay's isolated ledger.")
    commands = parser.add_subparsers(dest="command", required=True)

    query = commands.add_parser("query", help="Exact filters plus text search.")
    query.add_argument("--kind", action="append", default=[], help="Record kind (repeatable).")
    query.add_argument("--incident", default=None)
    query.add_argument("--subject", default=None)
    query.add_argument("--session", default=None, help="Evidence read from this session.")
    query.add_argument("--regime", default=None, help="Exact conditions.regime.")
    query.add_argument("--calibration", default=None, help="Calibration ('unknown' = None).")
    query.add_argument("--program-id", default=None, help="ECU program ('unknown' = None).")
    query.add_argument("--valid-at", type=_instant, default=None)
    query.add_argument("--valid-before", type=_instant, default=None)
    query.add_argument("--valid-after", type=_instant, default=None)
    query.add_argument("--as-of", type=_instant, default=None, help="What was believed then.")
    query.add_argument("--text", default=None)
    query.add_argument("--history", action="store_true", help="Include superseded/retracted.")
    query.add_argument("--limit", type=int, default=None)
    query.add_argument("--json", action="store_true")

    card = commands.add_parser("card", help="Render an investigation card (no model).")
    card.add_argument("investigation")
    card.add_argument("--as-of", type=_instant, default=None)
    card.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    sys.path.insert(0, str(REPO_ROOT))
    from src.config import CanaryConfig
    from src.evidence import (
        ANY,
        MemoryQuery,
        investigation_card,
        memory_ledger_for,
        record_to_dict,
        render_text,
    )
    from src.vehicle.profiles import resolve_profile_for_tooling, scope_config

    base = CanaryConfig.from_yaml(args.config)
    config = scope_config(base, resolve_profile_for_tooling(base, args.profile))
    ledger = memory_ledger_for(config, replay_id=args.replay_id)
    if args.command == "card":
        built = investigation_card(ledger, args.investigation, as_of=args.as_of)
        if args.json:
            print(json.dumps(built.model_dump(mode="json"), indent=2, sort_keys=True))
        else:
            print(render_text(built), end="")
        return 0

    def nullable(value: str | None) -> object:
        if value is None:
            return ANY
        return None if value == "unknown" else value

    found = ledger.query(
        MemoryQuery(
            kinds=tuple(args.kind),
            incident=args.incident,
            subject=args.subject,
            session=args.session,
            regime=args.regime,
            calibration=nullable(args.calibration),  # type: ignore[arg-type]
            program_id=nullable(args.program_id),  # type: ignore[arg-type]
            valid_at=args.valid_at,
            valid_before=args.valid_before,
            valid_after=args.valid_after,
            as_of=args.as_of,
            text=args.text,
            include_superseded=args.history,
            limit=args.limit,
        )
    )
    if args.json:
        rows = [
            {
                "record": record_to_dict(entry.record),
                "superseded_at": None if entry.superseded_at is None else str(entry.superseded_at),
                "superseded_by": list(entry.superseded_by),
                "retracted_by": entry.retracted_by,
                "retired_by": entry.retired_by,
            }
            for entry in found
        ]
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    for entry in found:
        record = entry.record
        state = "current" if entry.current else f"superseded {entry.superseded_at}"
        if entry.retracted_by:
            state = f"retracted by {entry.retracted_by}"
        if entry.retired_by:
            state += f"; retired by {entry.retired_by}"
        label = getattr(record, "subject", None) or getattr(record, "title", None) or record.kind
        print(f"{record.id}  {record.recorded_at}  {label}  [{state}]")
    print(f"{len(found)} record(s)  ({ledger.path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
