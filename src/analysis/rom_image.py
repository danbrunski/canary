"""Bytes in, a description out: the ROM library's analysis core (W08.2, W08.4, W08.5).

Given a file, :func:`load_image` decides what it is — an ``.OsirisROM``
(decoded with :mod:`src.analysis.osiris`), a plain image of the vehicle's
exact size, a known undecodable format (refused by name), or anything else
(refused by name with its size). :func:`describe` then reports what the bytes
say: identity, the vehicle checksum recomputed against the stored words,
which recorded revision (by plain-image hash) it is, 4 KiB block statistics,
and the vehicle's map (:mod:`src.analysis.rom_map`) resolved against it.

A failing checksum is reported, never refused: inspection is separate from
editing, and the edit path keeps its own known-hash guard
(:class:`src.analysis.rom_targets.TargetEditor`). An image whose calibration
ID has no map definition still gets identity and block statistics, with
``map: null`` and the reason.

Read-only throughout: nothing here writes an image anywhere.
"""

from __future__ import annotations

import hashlib
import math
import zlib
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from src.analysis.osiris import SIGNATURE, Container
from src.analysis.rom_map import DTYPE_BYTES, ROM_MAP_FILE, Decode, RomMap, Table
from src.analysis.rom_targets import Checksums
from src.vehicle.tune_revisions import TuneRevisionRegistry

#: Block statistics granularity.
BLOCK_SIZE = 4096
#: Display classes for a block. Statistics, not meaning: ``erased`` is all
#: 0xFF; ``sparse`` is at least this share 0xFF; ``dense`` (code, packed
#: tables) has at least this many bits of entropy per byte; ``data`` is the
#: rest (constants and tables). On the 2008 Xterra image, code blocks measure
#: 6.4-7.1 bits and calibration blocks 4.4-5.3.
SPARSE_MIN_FF_PCT = 50.0
DENSE_MIN_ENTROPY = 6.0
#: Changed bytes closer than this merge into one compare range (W08.5).
COMPARE_MERGE_GAP = 16

ImageFormat = Literal["plain", "osirisrom"]


class ImageFormatError(ValueError):
    """The file is not an image this vehicle's library accepts; the message
    names what it is and why."""


@dataclass(frozen=True)
class LoadedImage:
    """A plain image and where it came from."""

    image: bytes
    format: ImageFormat
    #: sha256 of the ``.OsirisROM`` file, for an Osiris upload.
    container_sha256: str | None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def crc32_hex(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


def load_image(data: bytes, rom_map: RomMap) -> LoadedImage:
    """Detect the format and return the plain image, or refuse by name."""
    size = rom_map.image_size
    if data.startswith(SIGNATURE):
        try:
            image = Container.parse(data).decoded()
        except ValueError as exc:
            raise ImageFormatError(
                f"not a readable .OsirisROM ({len(data):,} bytes): {exc}"
            ) from exc
        if len(image) != size:
            raise ImageFormatError(
                f".OsirisROM decodes to {len(image):,} bytes; this vehicle's image is {size:,}"
            )
        return LoadedImage(image=image, format="osirisrom", container_sha256=sha256_hex(data))
    for refused in rom_map.refused_formats:
        if refused.matches(data):
            raise ImageFormatError(f"{refused.name}: {refused.reason}")
    if len(data) == size:
        return LoadedImage(image=bytes(data), format="plain", container_sha256=None)
    raise ImageFormatError(
        f"unrecognised file of {len(data):,} bytes: expected a plain image of exactly "
        f"{size:,} bytes or an .OsirisROM"
    )


def read_calibration_id(image: bytes, rom_map: RomMap) -> str | None:
    """ASCII calibration ID at the declared offset, or ``None`` if not text."""
    where = rom_map.calibration_id
    raw = image[where.offset : where.offset + where.length]
    body = raw.rstrip(b"\x00\xff ")
    if not body or any(byte < 0x20 or byte > 0x7E for byte in body):
        return None
    return body.decode("ascii")


def map_applies(calibration_id: str | None, rom_map: RomMap) -> str | None:
    """``None`` when the map describes this image, else the reason it does not."""
    if calibration_id in rom_map.calibration_ids:
        return None
    if calibration_id is None:
        return (
            "no map definition for this image: no ASCII calibration ID at "
            f"0x{rom_map.calibration_id.offset:06X}"
        )
    return f"no map definition for calibration {calibration_id}"


def checksum_values(image: bytes, checksums: Checksums) -> tuple[int, int]:
    """:meth:`Checksums.values`, vectorised: (sum, xor) over the BE32 words.

    Same validation and the same words; pinned to it by test. The pure-Python
    loop costs ~0.2 s per 1 MiB image, which a library listing would pay per
    entry.
    """
    offsets = [
        checksums.start,
        checksums.sum_offset,
        checksums.xor_offset,
        *checksums.excluded,
        *checksums.markers,
    ]
    if len(image) % 4 or any(o < 0 or o % 4 or o + 4 > len(image) for o in offsets):
        raise ValueError("Invalid checksum word offsets")
    if checksums.sum_offset == checksums.xor_offset or not {
        checksums.sum_offset,
        checksums.xor_offset,
    }.issubset(checksums.excluded):
        raise ValueError("Checksum words must be distinct and excluded")
    words = np.frombuffer(image, dtype=">u4")
    for offset, expected in checksums.markers.items():
        if int(words[offset // 4]) != expected:
            raise ValueError(
                f"Unsupported ROM checksum style: word at 0x{offset:06X} is "
                f"0x{int(words[offset // 4]):08X}, expected 0x{expected:08X}"
            )
    mask = np.zeros(len(words), dtype=bool)
    mask[checksums.start // 4 :] = True
    for offset in checksums.excluded:
        mask[offset // 4] = False
    covered = words[mask]
    total = int(covered.astype(np.uint64).sum() & 0xFFFFFFFF)
    xor = int(np.bitwise_xor.reduce(covered)) if covered.size else 0
    return total, xor


def checksum_verdict(image: bytes, checksums: Checksums) -> dict[str, Any]:
    """Recomputed vs stored checksum words, shown whether they pass or fail."""
    words = np.frombuffer(image, dtype=">u4") if len(image) % 4 == 0 else None
    stored = None
    if words is not None and max(checksums.sum_offset, checksums.xor_offset) + 4 <= len(image):
        stored = {
            "sum": f"0x{int(words[checksums.sum_offset // 4]):08X}",
            "xor": f"0x{int(words[checksums.xor_offset // 4]):08X}",
        }
    try:
        total, xor = checksum_values(image, checksums)
    except ValueError as exc:
        return {"ok": False, "stored": stored, "computed": None, "reason": str(exc)}
    computed = {"sum": f"0x{total:08X}", "xor": f"0x{xor:08X}"}
    ok = stored == computed
    reason = None
    if not ok:
        wrong = [
            name for name in ("sum", "xor") if stored is None or stored[name] != computed[name]
        ]
        reason = (
            f"checksum mismatch: stored {' and '.join(wrong)} word(s) differ from the recomputed"
        )
    return {
        "ok": ok,
        "stored": stored,
        "computed": computed,
        "reason": reason,
        "algorithm": checksums.algorithm,
    }


def revision_match(image_sha256: str, registry: TuneRevisionRegistry | None) -> dict[str, Any]:
    """Which recorded calibration this image is, by plain-image sha256.

    ``matched``: a flashed revision's ``image_sha256``. ``reference``: a known
    image that was never flashed as-is (a tuner's base ROM). ``unrecognised``:
    neither, or no flash history.
    """
    revision, reference = (None, None) if registry is None else registry.match_image(image_sha256)
    if revision is not None:
        status = "matched"
    elif reference is not None:
        status = "reference"
    else:
        status = "unrecognised"
    return {"revision": revision, "status": status, "reference": reference}


def block_stats(image: bytes) -> list[dict[str, Any]]:
    """Per 4 KiB block: share of 0xFF bytes, Shannon entropy, display class."""
    data = np.frombuffer(image, dtype=np.uint8)
    blocks = []
    for offset in range(0, len(data), BLOCK_SIZE):
        chunk = data[offset : offset + BLOCK_SIZE]
        counts = np.bincount(chunk, minlength=256)
        n = int(chunk.size)
        ff_pct = 100.0 * int(counts[0xFF]) / n
        p = counts[counts > 0] / n
        entropy = float(-(p * np.log2(p)).sum()) if n else 0.0
        entropy = 0.0 if math.isclose(entropy, 0.0, abs_tol=1e-12) else entropy
        if counts[0xFF] == n:
            cls = "erased"
        elif ff_pct >= SPARSE_MIN_FF_PCT:
            cls = "sparse"
        elif entropy >= DENSE_MIN_ENTROPY:
            cls = "dense"
        else:
            cls = "data"
        blocks.append(
            {
                "offset": offset,
                "ff_pct": round(ff_pct, 2),
                "entropy": round(entropy, 3),
                "class": cls,
            }
        )
    return blocks


def _decode_payload(decode: Decode | None) -> dict[str, Any] | None:
    if decode is None:
        return None
    return {
        "formula": decode.formula,
        "kind": decode.kind,
        "quantity": decode.quantity,
        "unit": decode.unit,
        # The numbers behind the formula string, so a client inverts an engineering
        # value exactly rather than parsing a rounded rendering of them (W09.3).
        "scale": decode.scale,
        "offset": decode.offset,
        "numerator": decode.numerator,
        "mask": decode.mask,
        "confidence": decode.confidence,
        "evidence": decode.evidence,
    }


def resolved_regions(rom_map: RomMap) -> list[dict[str, Any]]:
    """Absolute regions, then each map slot's regions resolved per slot."""
    out = [
        {
            "name": region.name,
            "start": region.start,
            "end": region.end,
            "size": region.end - region.start + 1,
            "kind": region.kind,
            "confidence": region.confidence,
            "added_by_uprev": region.added_by_uprev,
            "evidence": region.evidence,
            "slot": None,
        }
        for region in rom_map.regions
    ]
    slots = rom_map.map_slots
    if slots is not None:
        for slot in range(1, slots.count + 1):
            base = slots.slot_base(slot)
            for region in slots.regions:
                out.append(
                    {
                        "name": f"Map {slot}: {region.name}",
                        "start": base + region.start,
                        "end": base + region.end,
                        "size": region.end - region.start + 1,
                        "kind": region.kind,
                        "confidence": region.confidence,
                        "added_by_uprev": region.added_by_uprev,
                        "evidence": region.evidence,
                        "slot": slot,
                    }
                )
    return sorted(out, key=lambda r: (r["start"], -r["end"]))


def _table_label(table: Table, slot: int | None) -> str:
    return table.name if slot is None else f"{table.name} (Map {slot})"


def table_placements(rom_map: RomMap) -> list[tuple[Table, int | None, int, int]]:
    """Every (table, slot, start, end-inclusive) the map places."""
    placed = []
    for table in rom_map.tables:
        for slot in rom_map.table_slots(table):
            start = rom_map.table_address(table, slot)
            placed.append((table, slot, start, start + table.size - 1))
    return placed


def _axis_ids(rom_map: RomMap) -> set[str]:
    return {ref for t in rom_map.tables for ref in (t.axes.rows, t.axes.cols) if ref is not None}


def table_definitions(rom_map: RomMap) -> list[dict[str, Any]]:
    """Each table placement, without values (the description's ``tables``)."""
    axes = _axis_ids(rom_map)
    return [
        {
            "id": table.id,
            "kind": table.kind,
            "name": table.name,
            "label": _table_label(table, slot),
            "slot": slot,
            "offset": start,
            "end": end,
            "rows": table.rows,
            "cols": table.cols,
            "dtype": table.dtype,
            "role": "axis" if table.id in axes else "table",
            "decode": _decode_payload(table.decode),
            "axes": {"rows": table.axes.rows, "cols": table.axes.cols},
            "confidence": table.confidence,
            "evidence": table.evidence,
        }
        for table, slot, start, end in table_placements(rom_map)
    ]


def _map_info(rom_map: RomMap, calibration_id: str) -> dict[str, Any]:
    checks = rom_map.checksums
    return {
        "definition": ROM_MAP_FILE,
        "calibration_id": calibration_id,
        "calibration_ids": list(rom_map.calibration_ids),
        "image_size": rom_map.image_size,
        "erase_sectors": [
            {
                "index": sector.index,
                "start": sector.start,
                "end": sector.end,
                "size": sector.end - sector.start + 1,
                "confidence": sector.confidence,
                "evidence": sector.evidence,
            }
            for sector in rom_map.erase_sectors
        ],
        "checksums": {
            "algorithm": checks.algorithm,
            "start": checks.start,
            "sum_offset": checks.sum_offset,
            "xor_offset": checks.xor_offset,
            "excluded": list(checks.excluded),
        },
    }


def _map_slots_info(rom_map: RomMap) -> dict[str, Any] | None:
    slots = rom_map.map_slots
    if slots is None:
        return None
    return {
        "base": slots.base,
        "stride": slots.stride,
        "count": slots.count,
        "end": slots.end,
        "confidence": slots.confidence,
        "evidence": slots.evidence,
        "slots": [
            {"slot": n, "start": slots.slot_base(n), "end": slots.slot_base(n) + slots.stride - 1}
            for n in range(1, slots.count + 1)
        ],
    }


def identity(image: bytes, rom_map: RomMap, *, container_sha256: str | None) -> dict[str, Any]:
    return {
        "sha256": sha256_hex(image),
        "crc32": crc32_hex(image),
        "container_sha256": container_sha256,
        "calibration_id": read_calibration_id(image, rom_map),
        "size": len(image),
    }


def describe(
    image: bytes,
    rom_map: RomMap,
    registry: TuneRevisionRegistry | None,
    *,
    container_sha256: str | None = None,
) -> dict[str, Any]:
    """The full W08.2 description of a plain image."""
    if len(image) != rom_map.image_size:
        raise ImageFormatError(
            f"image is {len(image):,} bytes; this vehicle's image is {rom_map.image_size:,}"
        )
    ident = identity(image, rom_map, container_sha256=container_sha256)
    reason = map_applies(ident["calibration_id"], rom_map)
    mapped = reason is None
    return {
        **ident,
        "checksum": checksum_verdict(image, rom_map.checksums) if mapped else None,
        "revision_match": revision_match(ident["sha256"], registry),
        "blocks": block_stats(image),
        "regions": resolved_regions(rom_map) if mapped else [],
        "tables": table_definitions(rom_map) if mapped else [],
        "map_slots": _map_slots_info(rom_map) if mapped else None,
        "map": _map_info(rom_map, ident["calibration_id"]) if mapped else None,
        "map_reason": reason,
    }


def _cell(image: bytes, address: int, dtype: str) -> int:
    width = DTYPE_BYTES[dtype]
    return int.from_bytes(image[address : address + width], "big")


def _cells(image: bytes, table: Table, address: int) -> list[list[int]]:
    width = DTYPE_BYTES[table.dtype]
    return [
        [
            _cell(image, address + (row * table.cols + col) * width, table.dtype)
            for col in range(table.cols)
        ]
        for row in range(table.rows)
    ]


def _decoded(decode: Decode | None, raw: list[list[int]]) -> list[list[float | None]] | None:
    if decode is None:
        return None
    return [[decode.apply(value) for value in row] for row in raw]


def _axis_payload(image: bytes, rom_map: RomMap, ref: str | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    axis = rom_map.table(ref)
    address = rom_map.table_address(axis, None)
    raw = [value for row in _cells(image, axis, address) for value in row]
    decode = axis.decode
    return {
        "id": axis.id,
        "name": axis.name,
        "offset": address,
        "raw": raw,
        "decoded": None if decode is None else [decode.apply(v) for v in raw],
        "unit": None if decode is None else decode.unit,
        "decode": _decode_payload(decode),
        "confidence": axis.confidence,
    }


def table_values(image: bytes, rom_map: RomMap) -> list[dict[str, Any]]:
    """Every table in every slot it occupies, raw and (where declared) decoded."""
    axes = _axis_ids(rom_map)
    out = []
    for table, slot, start, end in table_placements(rom_map):
        raw = _cells(image, table, start)
        out.append(
            {
                "id": table.id,
                "kind": table.kind,
                "name": table.name,
                "label": _table_label(table, slot),
                "slot": slot,
                "offset": start,
                "end": end,
                "rows": table.rows,
                "cols": table.cols,
                "dtype": table.dtype,
                "role": "axis" if table.id in axes else "table",
                "unit": None if table.decode is None else table.decode.unit,
                "raw": raw,
                "decoded": _decoded(table.decode, raw),
                "decode": _decode_payload(table.decode),
                "axes": {
                    "rows": _axis_payload(image, rom_map, table.axes.rows),
                    "cols": _axis_payload(image, rom_map, table.axes.cols),
                },
                "confidence": table.confidence,
                "evidence": table.evidence,
                # Where a tuning editor labels these axes differently from
                # the code; every slot copy carries it, so every view warns.
                "editor_disagreement": None
                if table.editor_disagreement is None
                else table.editor_disagreement.model_dump(),
            }
        )
    return out


def tables_payload(image: bytes, rom_map: RomMap) -> dict[str, Any]:
    """``GET /control/rom/library/{sha}/tables``: values, or why there are none."""
    calibration_id = read_calibration_id(image, rom_map)
    reason = map_applies(calibration_id, rom_map)
    slots = rom_map.map_slots
    return {
        "sha256": sha256_hex(image),
        "calibration_id": calibration_id,
        "map_slots": [] if reason or slots is None else list(range(1, slots.count + 1)),
        "tables": [] if reason else table_values(image, rom_map),
        "reason": reason,
    }


def changed_ranges(
    a: bytes, b: bytes, *, gap: int = COMPARE_MERGE_GAP
) -> list[tuple[int, int, int]]:
    """``(start, end inclusive, changed byte count)`` per merged run of differences.

    Two differing bytes ``gap`` or fewer apart land in one range.
    """
    if len(a) != len(b):
        raise ValueError(f"images differ in size ({len(a):,} vs {len(b):,} bytes)")
    diff = np.flatnonzero(np.frombuffer(a, dtype=np.uint8) != np.frombuffer(b, dtype=np.uint8))
    ranges: list[list[int]] = []
    for index in diff.tolist():
        if ranges and index - ranges[-1][1] <= gap:
            ranges[-1][1] = index
            ranges[-1][2] += 1
        else:
            ranges.append([index, index, 1])
    return [(start, end, count) for start, end, count in ranges]


def compare_images(
    a: bytes,
    b: bytes,
    rom_map: RomMap,
    *,
    gap: int = COMPARE_MERGE_GAP,
) -> dict[str, Any]:
    """Changed byte ranges of ``a`` -> ``b``, labelled by the map (W08.5).

    The map labels the ranges only when it describes both images (both
    calibration IDs are in ``calibration_ids``); otherwise the ranges come
    back unlabelled with the reason.
    """
    reasons = [map_applies(read_calibration_id(img, rom_map), rom_map) for img in (a, b)]
    reason = next((r for r in reasons if r is not None), None)
    mapped = reason is None
    regions = resolved_regions(rom_map) if mapped else []
    placements = table_placements(rom_map) if mapped else []
    checks = rom_map.checksums
    checksum_words = (
        [("Checksum sum word", checks.sum_offset), ("Checksum XOR word", checks.xor_offset)]
        if mapped
        else []
    )
    ranges = []
    total = 0
    diff_a = np.frombuffer(a, dtype=np.uint8)
    diff_b = np.frombuffer(b, dtype=np.uint8)
    for start, end, count in changed_ranges(a, b, gap=gap):
        total += count
        overlaps: list[dict[str, Any]] = []
        cells: list[dict[str, Any]] = []
        for table, slot, t_start, t_end in placements:
            if t_end < start or t_start > end:
                continue
            overlaps.append(
                {
                    "type": "table",
                    "id": table.id,
                    "label": _table_label(table, slot),
                    "slot": slot,
                    "confidence": table.confidence,
                }
            )
            width = DTYPE_BYTES[table.dtype]
            for row in range(table.rows):
                for col in range(table.cols):
                    address = t_start + (row * table.cols + col) * width
                    if address + width - 1 < start or address > end:
                        continue
                    if not (
                        diff_a[address : address + width] != diff_b[address : address + width]
                    ).any():
                        continue
                    old = _cell(a, address, table.dtype)
                    new = _cell(b, address, table.dtype)
                    decode = table.decode
                    cells.append(
                        {
                            "table": table.id,
                            "table_label": _table_label(table, slot),
                            "slot": slot,
                            "row": row,
                            "col": col,
                            "address": address,
                            "old": old,
                            "new": new,
                            "old_decoded": None if decode is None else decode.apply(old),
                            "new_decoded": None if decode is None else decode.apply(new),
                            "unit": None if decode is None else decode.unit,
                        }
                    )
        for name, offset in checksum_words:
            if offset <= end and offset + 3 >= start:
                overlaps.append(
                    {
                        "type": "checksum",
                        "id": None,
                        "label": name,
                        "slot": None,
                        "confidence": "verified",
                    }
                )
        for region in sorted(regions, key=lambda r: r["end"] - r["start"]):
            if region["end"] < start or region["start"] > end:
                continue
            overlaps.append(
                {
                    "type": "slot_region" if region["slot"] is not None else "region",
                    "id": None,
                    "label": region["name"],
                    "slot": region["slot"],
                    "confidence": region["confidence"],
                }
            )
        # One entry per label: the checksum words are also declared as
        # regions in a map that names them.
        unique: list[dict[str, Any]] = []
        for item in overlaps:
            if all(item["label"] != kept["label"] for kept in unique):
                unique.append(item)
        overlaps = unique
        labels = [item["label"] for item in overlaps]
        ranges.append(
            {
                "start": start,
                "end": end,
                "length": end - start + 1,
                "changed": count,
                "labels": labels,
                "overlaps": overlaps,
                "cells": cells,
            }
        )
    return {
        "a": sha256_hex(a),
        "b": sha256_hex(b),
        "changed_bytes": total,
        "gap": gap,
        "ranges": ranges,
        "map_applied": mapped,
        "map_reason": reason,
    }


__all__ = [
    "BLOCK_SIZE",
    "COMPARE_MERGE_GAP",
    "DENSE_MIN_ENTROPY",
    "SPARSE_MIN_FF_PCT",
    "ImageFormat",
    "ImageFormatError",
    "LoadedImage",
    "block_stats",
    "changed_ranges",
    "checksum_values",
    "checksum_verdict",
    "compare_images",
    "crc32_hex",
    "describe",
    "identity",
    "load_image",
    "map_applies",
    "read_calibration_id",
    "resolved_regions",
    "revision_match",
    "sha256_hex",
    "table_definitions",
    "table_placements",
    "table_values",
    "tables_payload",
]
