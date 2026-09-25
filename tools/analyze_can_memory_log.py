#!/usr/bin/env python3
"""Rank RAM byte/word candidates against DIDs in a ``can_memory_log`` capture.

The logger interleaves one service-0x23 RAM read with one or more Mode-22
reads.  This analyzer deliberately routes every relationship through
``compare_channels``: observations are paired on their acquisition times,
used at most once, and rejected when either side barely moved.  It never
compares per-window aggregates.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis.compare import compare_channels
from src.analysis.pairing import ComparisonRefusedError, PairingPolicy
from tools._bus_tool import record_kind


@dataclass(frozen=True)
class Sample:
    index: int
    acquired_at: float
    payload: bytes


@dataclass(frozen=True)
class MemoryCapture:
    address: int
    length: int
    memory: tuple[Sample, ...]
    dids: dict[int, tuple[Sample, ...]]


def _acquired_at(row: dict[str, Any]) -> float:
    return (float(row["t_send"]) + float(row["t_recv"])) / 2.0


def load_capture(path: Path) -> MemoryCapture:
    """Load successful memory and DID observations from one JSONL capture."""
    header: dict[str, Any] | None = None
    memory: list[Sample] = []
    dids: dict[int, list[Sample]] = defaultdict(list)
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
            kind = record_kind(row)
            if kind == "header":
                if header is not None:
                    raise ValueError(f"{path}:{line_number}: duplicate header")
                header = row
            elif kind == "read" and row.get("payload_hex") is not None:
                memory.append(
                    Sample(
                        index=int(row["index"]),
                        acquired_at=_acquired_at(row),
                        payload=bytes.fromhex(row["payload_hex"]),
                    )
                )
            elif kind == "did" and row.get("payload_hex") is not None:
                did = int(str(row["did"]), 16)
                dids[did].append(
                    Sample(
                        index=int(row["memory_index"]),
                        acquired_at=_acquired_at(row),
                        payload=bytes.fromhex(row["payload_hex"]),
                    )
                )
    if header is None:
        raise ValueError(f"{path}: missing header")
    address = int(str(header["address"]), 16)
    length = int(header["length"])
    for sample in memory:
        if len(sample.payload) != length:
            raise ValueError(
                f"{path}: memory sample {sample.index} has {len(sample.payload)} bytes, "
                f"header says {length}"
            )
    return MemoryCapture(
        address=address,
        length=length,
        memory=tuple(memory),
        dids={did: tuple(samples) for did, samples in dids.items()},
    )


def _series_rows(
    channel: str, samples: Sequence[Sample], values: Sequence[int]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sample, value in zip(samples, values, strict=True):
        stamp = pd.Timestamp(sample.acquired_at, unit="s", tz="UTC")
        rows.append(
            {
                "timestamp": stamp,
                "source": "uprev",
                "channel": channel,
                "value_number": value,
                "unit": "counts",
                "metadata_json": json.dumps({"acquired_at": sample.acquired_at}),
            }
        )
    return rows


def _candidates(capture: MemoryCapture) -> list[tuple[str, list[int]]]:
    candidates: list[tuple[str, list[int]]] = []
    for offset in range(capture.length):
        address = capture.address + offset
        candidates.append(
            (
                f"MEM_{address:08X}_U8",
                [sample.payload[offset] for sample in capture.memory],
            )
        )
    for offset in range(capture.length - 1):
        address = capture.address + offset
        candidates.append(
            (
                f"MEM_{address:08X}_U16BE",
                [
                    int.from_bytes(sample.payload[offset : offset + 2], "big")
                    for sample in capture.memory
                ],
            )
        )
    return candidates


def analyze_capture(
    capture: MemoryCapture,
    *,
    tolerance: str = "15ms",
    min_pairs: int = 30,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Compare every U8/U16BE RAM view with every captured raw DID."""
    policy = PairingPolicy.within(tolerance)
    results: list[dict[str, Any]] = []
    refusals: list[dict[str, str]] = []
    for candidate, values in _candidates(capture):
        candidate_rows = _series_rows(candidate, capture.memory, values)
        for did, samples in sorted(capture.dids.items()):
            did_channel = f"DID_{did:04X}_RAW"
            did_values = [int.from_bytes(sample.payload, "big") for sample in samples]
            frame = pd.DataFrame(candidate_rows + _series_rows(did_channel, samples, did_values))
            try:
                comparison = compare_channels(
                    frame,
                    f"uprev:{candidate}",
                    f"uprev:{did_channel}",
                    policy=policy,
                    min_pairs=min_pairs,
                )
            except ComparisonRefusedError as exc:
                refusals.append({"candidate": candidate, "did": f"0x{did:04X}", "reason": str(exc)})
                continue
            result = comparison.as_dict()
            result["candidate_address"] = f"0x{int(candidate[4:12], 16):08X}"
            result["did"] = f"0x{did:04X}"
            results.append(result)
    results.sort(key=lambda item: float(item["r_squared"]), reverse=True)
    return results, refusals


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--tolerance", default="15ms")
    parser.add_argument("--min-pairs", type=int, default=30)
    parser.add_argument("--min-r-squared", type=float, default=0.8)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--out", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    capture = load_capture(args.capture)
    results, refusals = analyze_capture(capture, tolerance=args.tolerance, min_pairs=args.min_pairs)
    report = {
        "capture": str(args.capture),
        "address": f"0x{capture.address:08X}",
        "length": capture.length,
        "memory_samples": len(capture.memory),
        "did_samples": {f"0x{did:04X}": len(rows) for did, rows in capture.dids.items()},
        "pairing_tolerance": args.tolerance,
        "results": results,
        "refusals": refusals,
    }
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    shown = [row for row in results if float(row["r_squared"]) >= args.min_r_squared]
    for row in shown[: args.limit]:
        print(
            f"{row['a']} -> {row['b']}: r2={row['r_squared']:.5f} "
            f"n={row['n_pairs']} slope={row['slope']:.8g} "
            f"offset={row['offset']:.8g} [{row['scale_verdict']}]"
        )
    print(f"compared={len(results)} refused={len(refusals)} shown={min(len(shown), args.limit)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
