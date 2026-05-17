#!/usr/bin/env python3
"""Emit a draft DBC from data/bindings.yaml (Task 4.D.2).

One-way, derived artifact: `bindings.yaml` is the source of truth (it
carries confidence + evidence a DBC can't). This produces a draft
`data/dbc/xterra_2008.dbc` for SavvyCAN / Cabana / candump interop and
external sharing. Regenerate it; never hand-edit the DBC.

Signals with no pinned scale are emitted raw (scale 1 / offset 0) so the
DBC still loads and shows the byte field. Confidence is written into each
signal's comment so a DBC consumer can see how trustworthy it is.

Usage:
  tools/emit_dbc.py
  tools/emit_dbc.py --bindings data/bindings.yaml --out data/dbc/xterra_2008.dbc
  tools/emit_dbc.py --min-confidence high
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from cantools.database import Database, Message, Signal
from cantools.database.conversion import BaseConversion

from src.decode.bindings import Signal as Binding
from src.decode.bindings import load_bindings

_CONF_RANK = {"hypothesized": 0, "low": 1, "medium": 2, "high": 3, "confirmed": 4}


def _to_signal(b: Binding) -> Signal:
    """One bindings.yaml entry → a cantools Signal.

    bindings use byte_start/byte_length + byte_order. For big-endian
    (Motorola) the cantools start bit is the MSB of the first byte in the
    sawtooth numbering: byte_start*8 + 7. Little-endian starts at the LSB:
    byte_start*8. The round-trip test pins this.
    """
    if b.byte_order == "big":
        start = b.byte_start * 8 + 7
        order = "big_endian"
    else:
        start = b.byte_start * 8
        order = "little_endian"
    scale = b.scale if b.scale is not None else 1
    offset = b.offset if b.offset is not None else 0
    # is_float = the value is an IEEE float *on the wire* (DBC only allows
    # that for 32/64-bit). Our signals are integer-encoded; scale/offset
    # still yield fractional engineering values via the linear conversion.
    return Signal(
        name=b.name,
        start=start,
        length=b.byte_length * 8,
        byte_order=order,
        is_signed=False,
        conversion=BaseConversion.factory(scale=scale, offset=offset, is_float=False),
        minimum=b.plausible_min,
        maximum=b.plausible_max,
        unit=(b.unit if b.unit and b.unit != "unknown" else None),
        comment=f"confidence={b.confidence}" + (f"; {b.notes.splitlines()[0]}" if b.notes else ""),
    )


def build_database(bindings_path: Path, min_rank: int = 0) -> Database:
    bindings = load_bindings(bindings_path)
    by_id: dict[int, list[Binding]] = defaultdict(list)
    for b in bindings.signals:
        if _CONF_RANK.get(b.confidence, 0) >= min_rank:
            by_id[b.can_id].append(b)
    messages: list[Message] = []
    for can_id, sigs in sorted(by_id.items()):
        messages.append(
            Message(
                frame_id=can_id,
                name=f"M_{can_id:03X}",
                length=8,
                signals=[_to_signal(b) for b in sigs],
                comment=f"Xterra 0x{can_id:X} (draft from bindings.yaml)",
            )
        )
    return Database(messages=messages)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="emit_dbc", description="bindings.yaml → draft DBC.")
    p.add_argument("--bindings", type=Path, default=Path("data/bindings.yaml"))
    p.add_argument("--out", type=Path, default=Path("data/dbc/xterra_2008.dbc"))
    p.add_argument(
        "--min-confidence",
        choices=list(_CONF_RANK),
        default="hypothesized",
        help="Lowest confidence to include (default: all).",
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    db = build_database(args.bindings, _CONF_RANK[args.min_confidence])
    if not db.messages:
        print("error: no signals at/above the confidence floor", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(db.as_dbc_string())
    n_sig = sum(len(m.signals) for m in db.messages)
    print(f"wrote {args.out}: {len(db.messages)} messages, {n_sig} signals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
