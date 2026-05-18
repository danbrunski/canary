#!/usr/bin/env python3
"""Classify telemetry sessions as trainable or schema-incomplete / corrupt.

A session is trainable only if its parquet carries the OBD diagnostic stream
(``source == "obd"``) alongside CAN frames. Sessions captured before the
collection fix (recorder path ignoring ``collection.obd_source``) are CAN-only;
crashed writes leave empty / zero-byte-only dirs. Neither should enter a
training run — unioned with healthy sessions they collapse the LSTM windows and
starve the behavior models.

Read-only by default: prints a verdict table. With ``--apply`` it MOVES (never
deletes) ``can_only`` + ``corrupt`` sessions into ``<root>/_archive/``, which
``discover_sessions`` skips. Point it at a local mirror or use the printed
paths to purge the same junk on the Pi.

    scripts/classify_sessions.py sessions            # dry run, just report
    scripts/classify_sessions.py sessions --apply    # quarantine the bad ones
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

import pyarrow.parquet as pq

ARCHIVE_DIRNAME = "_archive"


def classify(session_dir: Path) -> tuple[str, str]:
    """Return (verdict, detail). verdict ∈ {ok, can_only, corrupt}."""
    chunks = sorted(session_dir.glob("telemetry-*.parquet"))
    if not chunks:
        return "corrupt", "no telemetry parquet at all"
    zero = sum(1 for c in chunks if c.stat().st_size == 0)
    readable = 0
    unreadable = 0
    for c in chunks:
        if c.stat().st_size == 0:
            continue
        try:
            for batch in pq.ParquetFile(c).iter_batches(columns=["source"]):
                readable += 1
                col = batch.column(0)
                if "obd" in {v for v in col.to_pylist() if v is not None}:
                    return (
                        "ok",
                        f"{len(chunks)} chunks, {zero} zero-byte, has source=obd",
                    )
        except Exception:  # noqa: BLE001 - any unreadable chunk is just skipped
            unreadable += 1
    if readable == 0:
        return "corrupt", f"{len(chunks)} chunks, {zero} zero-byte, {unreadable} unreadable"
    return (
        "can_only",
        f"{len(chunks)} chunks, {zero} zero-byte, NO source=obd rows (CAN-only)",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="classify_sessions", description=__doc__)
    p.add_argument("root", type=Path, help="Sessions root to scan (e.g. ./sessions).")
    p.add_argument(
        "--apply",
        action="store_true",
        help=f"Move can_only + corrupt sessions into <root>/{ARCHIVE_DIRNAME}/.",
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root: Path = args.root
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    archive = root / ARCHIVE_DIRNAME
    sessions = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith("_"))
    counts = {"ok": 0, "can_only": 0, "corrupt": 0}
    moved = 0
    print(f"{'SESSION':24s} {'VERDICT':9s} DETAIL")
    print("-" * 78)
    for s in sessions:
        verdict, detail = classify(s)
        counts[verdict] += 1
        print(f"{s.name:24s} {verdict:9s} {detail}")
        if args.apply and verdict != "ok":
            archive.mkdir(exist_ok=True)
            shutil.move(str(s), str(archive / s.name))
            moved += 1

    print("-" * 78)
    print(
        f"ok={counts['ok']}  can_only={counts['can_only']}  "
        f"corrupt={counts['corrupt']}  (total {len(sessions)})"
    )
    if args.apply:
        print(f"moved {moved} bad session(s) -> {archive}/  (reversible: just move back)")
    elif counts["can_only"] or counts["corrupt"]:
        print("dry run — re-run with --apply to quarantine the bad ones.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
