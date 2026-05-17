#!/usr/bin/env python3
"""Validate the Mode 22 DID map against captured DID snapshots.

The Mode-22 counterpart of `decode_bindings.py`. DIDs aren't in the
broadcast `frames.log` — they're in the per-state sweep snapshots
(`data/sessions/<ts>/dids_*.json`, the `{did_hex: payload_hex}` maps).
This walks those snapshots, decodes every `data/dids.yaml` entry with
`decode_did_raw` + `apply_scaling`, and reports:

  * per-entry raw / decoded range observed + plausibility (catches a
    misapplied scale/offset, like decode_bindings did for the coolant
    −40 sentinel),
  * coverage — swept DIDs that have no map entry yet (naming backlog),
    and map entries absent from the given snapshots.

Usage:
  tools/decode_dids.py data/sessions/<ts>/                 # all dids_*.json in it
  tools/decode_dids.py data/sessions/<ts>/dids_idle_c0.json ...
  tools/decode_dids.py <session> --dids data/dids.yaml --confidence high,medium
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from src.decode.dids import apply_scaling, decode_did_raw, is_plausible, load_did_map

_CONF_ORDER = ("confirmed", "high", "medium", "low", "hypothesized")


def _snapshot_files(args_paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in args_paths:
        if p.is_dir():
            out.extend(sorted(p.glob("dids_*.json")))
        elif p.is_file():
            out.append(p)
    return out


def _load_snapshots(files: list[Path]) -> dict[str, dict[int, bytes]]:
    """Return {label: {did_int: payload_bytes}} per snapshot file."""
    snaps: dict[str, dict[int, bytes]] = {}
    for f in files:
        doc = json.loads(f.read_text())
        vals = doc.get("values", {})
        snaps[f.stem] = {int(k, 16): bytes.fromhex(v) for k, v in vals.items() if v is not None}
    return snaps


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="decode_dids",
        description="Validate data/dids.yaml against captured DID snapshots.",
    )
    p.add_argument("paths", nargs="+", type=Path, help="Session dir(s) or dids_*.json file(s).")
    p.add_argument("--dids", type=Path, default=Path("data/dids.yaml"))
    p.add_argument(
        "--confidence",
        default=None,
        help="Comma-separated levels to include (default: all).",
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    files = _snapshot_files(list(args.paths))
    if not files:
        print("error: no dids_*.json snapshots found in the given paths", file=sys.stderr)
        return 1
    did_map = load_did_map(args.dids)
    keep = set(args.confidence.split(",")) if args.confidence else set(_CONF_ORDER)
    snaps = _load_snapshots(files)

    print(
        f"snapshots: {len(files)}  |  map: {len(did_map.dids)} DIDs  |  "
        f"access: req 0x{did_map.access.ecu_request_id:X} session "
        f"{f'0x{did_map.access.session:X}' if did_map.access.session is not None else 'default'}"
    )
    seen_in_map: set[int] = set()
    bad = 0
    for entry in sorted(did_map.dids, key=lambda e: (_CONF_ORDER.index(e.confidence), e.did)):
        if entry.confidence not in keep:
            continue
        seen_in_map.add(entry.did)
        raws: list[int] = []
        for vals in snaps.values():
            payload = vals.get(entry.did)
            if payload is None:
                continue
            r = decode_did_raw(entry, payload)
            if r is not None:
                raws.append(r)
        tag = f"[{entry.confidence}]"
        if not raws:
            print(f"  0x{entry.did:04X} {entry.name:32} {tag:13} — not in these snapshots")
            continue
        rlo, rhi = min(raws), max(raws)
        dlo, dhi = apply_scaling(entry, rlo), apply_scaling(entry, rhi)
        if dlo is None:
            decoded = "—  (scale not pinned)"
        else:
            lo, hi = sorted((dlo, dhi))
            verdict = ""
            for d in (lo, hi):
                if is_plausible(entry, d) is False:
                    verdict = "  ✗ OUT OF PLAUSIBLE RANGE"
                    bad += 1
                    break
            decoded = f"{lo:.3g}..{hi:.3g} {entry.unit}{verdict}"
        print(
            f"  0x{entry.did:04X} {entry.name:32} {tag:13} "
            f"raw {rlo}..{rhi} (n={len(raws)})  →  {decoded}"
        )

    all_swept = {d for vals in snaps.values() for d in vals}
    unidentified = sorted(all_swept - {e.did for e in did_map.dids})
    print(
        f"\ncoverage: {len(seen_in_map & all_swept)}/{len(did_map.dids)} map DIDs present; "
        f"{len(unidentified)} swept DIDs unidentified (naming backlog)"
    )
    if bad:
        print(
            f"\n⚠ {bad} entr{'y' if bad == 1 else 'ies'} decoded OUTSIDE plausible bounds "
            f"— scale/offset likely wrong."
        )
        return 2
    print("\n✓ no plausibility violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
