#!/usr/bin/env python3
"""Decode an UpRev flash of the Xterra ECM from a ``candump -L`` log and check it
against the image that was running and the image that was flashed.

    .venv/bin/python scripts/decode_uprev_flash.py CAPTURE.log \
        --old ZS44A-Tuned-20261110.OsirisROM --new ZS44A-Tuned-20261111.OsirisROM \
        [--json evidence.json]

``--old`` / ``--new`` take an ``.OsirisROM`` container or a raw 1 MiB image.

UpRev programs through a loader that talks on three CAN ids (2026-09-20,
2026-10-04 and 2026-11-11 flashes; ``docs/skunkworks/uprev-flash-procedure.md``):

    0x742 tester -> loader     0x762 loader -> tester     0x743 raw page data

    04                 -> 04 00 00 26 1C   loader status
    65                 -> 65               unexplained; precedes the first CRCs
    20 AAAAAAAA LLLLLL -> 20 CRC32         CRC-32 of flash [A, A+L)
    50 SS              -> 50 00            erase sector SS
    64                 -> 64               open the page buffer; 1,024 frames
                                           (8 KiB) follow on 0x743
    22                 -> 22 CRC32         CRC-32 of the buffer as received
    55 AAAAAAAA        -> 55 00            program the buffer at A
    67 CRC32           -> 67 00            finalize; the argument is the CRC-32
                                           of the whole 1 MiB image
    66                 -> (no reply)       leave the loader, after the key cycle

The verdict is ``verified`` only when the capture itself proves the ECU holds
the new image: the range CRCs taken after the last page cover the whole image
and all match it. Anything short of that says what is missing, because "the
capture ended early" and "a CRC did not match" need opposite responses.

``scripts/decode_flash.py`` is the ISO-TP/UDS decoder for the 0x7E0 side; it
cannot read this raw channel.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zlib
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.timefmt import format_epoch  # noqa: E402

LINE = re.compile(r"^\((?P<ts>\d+\.\d+)\)\s+\S+\s+(?P<id>[0-9A-Fa-f]+)#(?P<data>[0-9A-Fa-f]*)")
TESTER, LOADER, DATA = 0x742, 0x762, 0x743
PAGE = 8192

Frame = tuple[float, int, bytes]


def parse_candump(text: str) -> list[Frame]:
    """(time, id, data) for every ``candump -L`` line; other lines are skipped."""
    frames: list[Frame] = []
    for line in text.splitlines():
        m = LINE.match(line)
        if m and len(m["data"]) % 2 == 0:
            frames.append((float(m["ts"]), int(m["id"], 16), bytes.fromhex(m["data"])))
    return frames


def _crc(data: bytes) -> str:
    return f"{zlib.crc32(data):08x}"


def decode(frames: list[Frame], old: bytes, new: bytes) -> dict[str, Any]:
    """What the loader was told to do and whether the result is ``new``."""
    range_crcs: list[dict[str, Any]] = []
    erases: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    pending: tuple[float, bytes] | None = None
    buffer: list[bytes] | None = None
    checked: tuple[str, bytes] | None = None
    for ts, can_id, data in frames:
        if can_id == DATA:
            if buffer is not None:
                buffer.append(data)
            continue
        if can_id == TESTER and data:
            pending = (ts, data)
            if data[0] == 0x64:
                buffer = []
            continue
        if can_id != LOADER or pending is None or not data:
            continue
        sent_at, cmd = pending
        pending = None
        op = cmd[0]
        if op == 0x20 and len(cmd) >= 8:
            start = int.from_bytes(cmd[1:5], "big")
            length = int.from_bytes(cmd[5:8], "big")
            crc = data[1:5].hex()
            range_crcs.append(
                {
                    "time": format_epoch(ts, zulu=True),
                    "phase": "after" if pages else "before",
                    "start": start,
                    "length": length,
                    "crc32": crc,
                    "matches_old": _crc(old[start : start + length]) == crc,
                    "matches_new": _crc(new[start : start + length]) == crc,
                }
            )
        elif op == 0x50:
            erases.append(
                {
                    "sector": cmd[1],
                    "time": format_epoch(sent_at, zulu=True),
                    "seconds": round(ts - sent_at, 3),
                    "reply": data.hex(),
                }
            )
        elif op == 0x22:
            checked = (data[1:5].hex(), b"".join(buffer or []))
            buffer = None
        elif op == 0x55 and len(cmd) >= 5:
            address = int.from_bytes(cmd[1:5], "big")
            loader_crc, payload = checked or ("", b"")
            checked = None
            pages.append(
                {
                    "address": address,
                    "time": format_epoch(sent_at, zulu=True),
                    "bytes": len(payload),
                    "loader_crc32": loader_crc,
                    "received_crc32": _crc(payload),
                    "equals_new": payload == new[address : address + PAGE],
                    "equals_old": payload == old[address : address + PAGE],
                    "reply": data.hex(),
                }
            )
        elif op != 0x64:
            other.append(
                {
                    "command": cmd.hex(),
                    "time": format_epoch(sent_at, zulu=True),
                    "reply": data.hex(),
                }
            )
    if pending is not None and pending[1][0] != 0x64:
        other.append(
            {
                "command": pending[1].hex(),
                "time": format_epoch(pending[0], zulu=True),
                "reply": None,
            }
        )

    before = [r for r in range_crcs if r["phase"] == "before"]
    after = [r for r in range_crcs if r["phase"] == "after"]
    changed = [p for p in range(0, len(new), PAGE) if new[p : p + PAGE] != old[p : p + PAGE]]
    written = {p["address"] for p in pages}
    finalize = next((o for o in other if o["command"].startswith("67")), None)

    problems: list[str] = []
    if not any(can_id in (TESTER, LOADER) for _, can_id, _ in frames):
        problems.append(
            "no loader traffic (0x742/0x762) in the capture: not a flash, or not on CAN"
        )
    elif not pages:
        problems.append("no page was programmed in the capture")
    if pages:
        covered = sorted((r["start"], r["start"] + r["length"]) for r in after)
        end = 0
        for start, stop in covered:
            if start > end:
                break
            end = max(end, stop)
        if end < len(new):
            problems.append(
                f"range CRCs after programming cover only 0x0-{end:#x} of {len(new):#x}: "
                "the capture ended before the loader finished checking"
            )
        problems.extend(
            f"range CRC after programming at {r['start']:#x} does not match the new image"
            for r in after
            if not r["matches_new"]
        )
        problems.extend(
            f"page {p['address']:#x}: {p['bytes']} bytes received, "
            f"{'equal to' if p['equals_new'] else 'NOT equal to'} the new image "
            f"(loader CRC {p['loader_crc32']}, received {p['received_crc32']})"
            for p in pages
            if not p["equals_new"] or p["loader_crc32"] != p["received_crc32"]
        )
        if end < len(new):
            # Only worth saying when the final CRCs cannot settle it: with full
            # coverage they already prove every page, written or not.
            problems.extend(
                f"page {p:#x} differs between the images but was not written"
                for p in changed
                if p not in written
            )
        if finalize is not None and finalize["command"][2:10] != _crc(new):
            problems.append("the finalize command's CRC is not the new image's CRC-32")
    before_note = None
    if before and not all(r["matches_old"] for r in before):
        before_note = (
            "range CRCs before programming do not all match --old: the ECU was not "
            "running that image"
        )

    return {
        "verdict": "verified" if not problems else "not_verified",
        "problems": problems,
        "before_note": before_note,
        "boundary": after[-1]["time"] if after and not problems else None,
        "old_image_sha256": hashlib.sha256(old).hexdigest(),
        "new_image_sha256": hashlib.sha256(new).hexdigest(),
        "new_image_crc32": _crc(new),
        "capture_span": [
            format_epoch(frames[0][0], zulu=True),
            format_epoch(frames[-1][0], zulu=True),
        ]
        if frames
        else None,
        "summary": {
            "before_crcs_matching_old": f"{sum(r['matches_old'] for r in before)}/{len(before)}",
            "after_crcs_matching_new": f"{sum(r['matches_new'] for r in after)}/{len(after)}",
            "pages_equal_to_new": f"{sum(p['equals_new'] for p in pages)}/{len(pages)}",
            "erased_sectors": [e["sector"] for e in erases],
            "changed_pages": [f"{p:#x}" for p in changed],
            "data_frames": sum(1 for _, can_id, _ in frames if can_id == DATA),
        },
        "range_crcs": range_crcs,
        "erases": erases,
        "pages": pages,
        "other": other,
    }


def read_image(path: Path) -> bytes:
    """The 1 MiB image in ``path``: an ``.OsirisROM`` container or raw bytes."""
    raw = path.read_bytes()
    if path.suffix.lower() == ".osirisrom":
        from src.analysis.osiris import Container

        return Container.parse(raw).decoded()
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("capture", type=Path, help="candump -L log of the flash")
    parser.add_argument("--old", type=Path, required=True, help="image running before the flash")
    parser.add_argument("--new", type=Path, required=True, help="image that was flashed")
    parser.add_argument("--json", type=Path, help="write the full evidence here")
    args = parser.parse_args(argv)

    capture = args.capture.read_bytes()
    result = decode(
        parse_candump(capture.decode("ascii", "replace")),
        read_image(args.old),
        read_image(args.new),
    )
    result = {"capture_sha256": hashlib.sha256(capture).hexdigest(), **result}
    brief = {k: result[k] for k in ("verdict", "boundary", "problems", "before_note", "summary")}
    print(json.dumps(brief, indent=1))
    for page in result["pages"]:
        tags = [t for t in ("new", "old") if page[f"equals_{t}"]]
        print(f"  page {page['address']:#08x} {page['time']} {page['bytes']} B = {'/'.join(tags)}")
    for item in result["other"]:
        print(f"  {item['time']} {item['command']} -> {item['reply']}")
    if args.json:
        args.json.write_text(json.dumps(result, indent=1) + "\n")
    return 0 if result["verdict"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
