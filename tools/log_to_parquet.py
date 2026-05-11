#!/usr/bin/env python3
"""Convert a captured session (candump frames.log + jsonl sidecars) to Parquet.

The daemon writes telemetry directly to Parquet via `ParquetTelemetryWriter`.
This tool produces the *same shape* offline from the candump-format captures
we've accumulated before the daemon existed, so downstream consumers
(bindings decoder, ML preprocessing, anomaly models) don't have to care
whether a session was streamed live or recorded as candump.

Reads:
  * `<session>/frames.log` — required, CAN frames in candump text format
  * `<session>/pids.jsonl` — optional, OBD-II Mode 01 readings

Writes:
  * `<session>/telemetry-NNNNN.parquet` — chunked Parquet using the canonical
    TELEMETRY_SCHEMA. Each chunk holds up to `--max-rows` records (default
    10,000). Records from frames.log and pids.jsonl are merged by timestamp
    so the output is in temporal order regardless of source.

Tags.jsonl and manifest.json are left alone — they're small and human-readable.

Usage:
  tools/log_to_parquet.py data/sessions/<timestamp>/
  tools/log_to_parquet.py data/sessions/<timestamp>/ --max-rows 5000 --force
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from heapq import merge
from pathlib import Path

from src.analysis.iso_tp import iter_frames
from src.session import session_paths
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord


def _frames_to_records(frames_path: Path) -> Iterator[TelemetryRecord]:
    """Yield TelemetryRecord.can() entries for every parseable frame.

    We deliberately don't pass `channel=frame.channel` here — the daemon's
    `frame_to_record` doesn't either, and `channel` in TELEMETRY_SCHEMA is the
    stream identifier within a source, which for CAN means the CAN ID. The
    candump channel field ("vcan0") is bus-layer metadata that downstream
    consumers don't care about; preserving it would mean offline-converted
    sessions look different from daemon-recorded ones, defeating the purpose.
    """
    for frame in iter_frames(frames_path):
        yield TelemetryRecord.can(
            frame.timestamp,
            frame.can_id,
            frame.data,
        )


def _pids_to_records(pids_path: Path) -> Iterator[TelemetryRecord]:
    """Yield TelemetryRecord.obd() entries from a pids.jsonl sidecar."""
    if not pids_path.exists():
        return
    with pids_path.open("r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            yield TelemetryRecord.obd(
                float(rec["t"]),
                str(rec["pid"]),
                rec.get("value"),
                str(rec.get("unit", "")),
            )


def convert(
    session_dir: Path,
    *,
    max_rows: int = 10_000,
    compression: str = "snappy",
    include_pids: bool = True,
    force: bool = False,
) -> list[Path]:
    """Convert one session to chunked Parquet; returns list of files written.

    Frames and OBD records are merged by timestamp via heapq.merge — both
    inputs are already in temporal order, so the merge is O(n+m) with O(1)
    memory beyond the chunk buffer.
    """
    paths = session_paths(session_dir)
    if not paths.frames.exists():
        raise FileNotFoundError(f"no frames.log in {session_dir}")

    existing = sorted(paths.root.glob("telemetry-*.parquet"))
    if existing and not force:
        raise FileExistsError(
            f"{len(existing)} telemetry-*.parquet files already exist in "
            f"{paths.root}. Pass --force to overwrite."
        )
    for path in existing:
        path.unlink()

    frames_iter = _frames_to_records(paths.frames)
    merged: Iterator[TelemetryRecord]
    if include_pids and paths.pids.exists():
        pids_iter = _pids_to_records(paths.pids)
        merged = merge(frames_iter, pids_iter, key=lambda r: r.timestamp)
    else:
        merged = frames_iter

    return write_records_to_parquet(
        merged,
        paths.root,
        max_rows=max_rows,
        compression=compression,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="log_to_parquet",
        description=(
            "Convert a captured session (candump frames.log + optional "
            "pids.jsonl) into the canonical TELEMETRY_SCHEMA Parquet shape "
            "the daemon emits live. Offline-produced and daemon-produced "
            "Parquet are downstream-indistinguishable."
        ),
    )
    parser.add_argument("session_dir", type=Path)
    parser.add_argument(
        "--max-rows",
        type=int,
        default=10_000,
        help="Max telemetry rows per Parquet chunk file (default 10000).",
    )
    parser.add_argument(
        "--compression",
        default="snappy",
        choices=["snappy", "gzip", "zstd", "none"],
        help="Parquet compression codec (default snappy).",
    )
    parser.add_argument(
        "--no-pids",
        action="store_true",
        help="Skip pids.jsonl even if it exists (frames-only output).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing telemetry-*.parquet files in the session dir.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        files = convert(
            args.session_dir,
            max_rows=args.max_rows,
            compression=args.compression if args.compression != "none" else "uncompressed",
            include_pids=not args.no_pids,
            force=args.force,
        )
    except (FileNotFoundError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    total_size = sum(p.stat().st_size for p in files)
    print(f"Wrote {len(files)} Parquet file(s), {total_size / 1024 / 1024:.1f} MiB total:")
    for p in files:
        size_mb = p.stat().st_size / 1024 / 1024
        print(f"  {p.name}  ({size_mb:.2f} MiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
