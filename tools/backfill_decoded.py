#!/usr/bin/env python3
"""Backfill stored ``decoded:*`` rows for historical sessions.

Sessions recorded before decoded-channel persistence landed (see
``src/decode/frame_decoder.py``) carry raw CAN payload bytes but no decoded
rows — every query against them must hand-decode hex. This tool decodes the
bound signals once and writes the result as a ``telemetry-decoded-backfill``
Parquet chunk *inside* the session directory, so the standard
``telemetry-*.parquet`` globs (training loader, query_telemetry view) pick
it up with zero further plumbing.

Semantics match the ingest path and ``decode_can_rows`` exactly: RAW
integer values, ``{"can_id", "binding_confidence"}`` metadata — which is
also what lets ``decode_can_rows``' skip-existing guard prevent
double-decoding a backfilled session at training time.

Idempotent: a session is skipped when it already has decoded rows (either a
prior backfill chunk or ingest-persisted rows). ``--force`` rewrites the
backfill chunk — the refresh path after a binding definition changes; it
never touches the original ``telemetry-NNNNN`` chunks.

Usage:
  tools/backfill_decoded.py --config canary.dev.example.yaml
  tools/backfill_decoded.py --config canary.dev.example.yaml \
      --session 2026-07-28T164657 --confidence confirmed high --force
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.can_ids import parse_can_channel
from src.config import CanaryConfig
from src.dashboard.paths import DEFAULT_BINDINGS_PATH
from src.decode.bindings import Signal, load_bindings
from src.preprocessing.decode import decode_sentinel_mask, decode_signal_column, sentinel_frame
from src.storage.parquet_writer import TELEMETRY_SCHEMA

# Same Any-alias pattern as src/storage/parquet_writer.py — pyarrow ships
# incomplete type stubs.
_PA: Any = pa
_PQ: Any = pq

BACKFILL_STEM = "telemetry-decoded-backfill"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        "backfill_decoded", description="Backfill decoded:* rows into stored sessions."
    )
    parser.add_argument(
        "--config", type=Path, default=Path("canary.yaml"), help="canary.yaml path."
    )
    parser.add_argument(
        "--session",
        action="append",
        default=None,
        help="Session id(s) to backfill (repeatable). Default: every session under storage. "
        "Leading-underscore dirs are skipped unless named explicitly.",
    )
    parser.add_argument(
        "--bindings", type=Path, default=DEFAULT_BINDINGS_PATH, help="bindings.yaml path."
    )
    parser.add_argument(
        "--confidence",
        nargs="+",
        default=["confirmed"],
        help="Binding confidence levels to decode (default: confirmed).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite an existing backfill chunk (refresh after binding changes). "
        "Sessions whose decoded rows came from ingest are still skipped.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be written, write nothing."
    )
    return parser.parse_args(argv)


def _session_dirs(root: Path, only: list[str] | None) -> list[Path]:
    if only:
        dirs = []
        for sid in only:
            d = root / sid
            if not d.is_dir():
                print(f"warning: no such session dir: {d}", file=sys.stderr)
                continue
            dirs.append(d)
        return dirs
    return sorted(
        d
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith("_") and any(d.glob("telemetry-[0-9]*.parquet"))
    )


def _decoded_row_state(session_dir: Path) -> str:
    """One of "none", "backfill", "ingest" — where existing decoded rows live."""
    if any(session_dir.glob(f"{BACKFILL_STEM}*.parquet")):
        return "backfill"
    for chunk in sorted(session_dir.glob("telemetry-[0-9]*.parquet")):
        sources = _PQ.read_table(chunk, columns=["source"])["source"]
        if "decoded" in sources.unique().to_pylist():
            return "ingest"
    return "none"


def decode_session_frame(can_rows: pd.DataFrame, signals: list[Signal]) -> pd.DataFrame:
    """Vectorized decode of raw CAN rows into TELEMETRY_SCHEMA-shaped rows."""
    by_can_id: dict[int, list[Signal]] = {}
    for sig in signals:
        by_can_id.setdefault(sig.can_id, []).append(sig)

    out_frames: list[pd.DataFrame] = []
    for channel, group in can_rows.groupby("channel", sort=False):
        can_id = parse_can_channel(str(channel))
        if can_id is None or can_id not in by_can_id:
            continue
        payloads = group["payload"]
        for sig in by_can_id[can_id]:
            meta = json.dumps(
                {"can_id": can_id, "binding_confidence": sig.confidence}, sort_keys=True
            )
            # R04.4: the preprocessing decode path's own extraction, so the
            # stored rows ("stored rows win" in decode_can_rows) are the same
            # values a re-decode would produce.
            values = pd.Series(decode_signal_column(payloads, sig), index=group.index)
            frame = pd.DataFrame(
                {
                    "timestamp": group["timestamp"],
                    "source": "decoded",
                    "channel": sig.name,
                    "value_number": values,
                    "value_text": None,
                    "value_bool": None,
                    "unit": sig.display_unit,
                    "payload": None,
                    "metadata_json": meta,
                }
            )
            out_frames.append(frame[frame["value_number"].notna()])
            sentinel = decode_sentinel_mask(payloads, sig)
            if sentinel.any():
                # A04.6: the declared sentinel word is stored as the
                # decoded:<NAME>_SENTINEL state row the live FrameDecoder writes.
                out_frames.append(
                    sentinel_frame(group["timestamp"].to_numpy()[sentinel], sig, meta)
                )

    if not out_frames:
        return pd.DataFrame()
    return (
        pd.concat(out_frames, ignore_index=True)
        .sort_values("timestamp", kind="stable")
        .reset_index(drop=True)
    )


def backfill_session(session_dir: Path, signals: list[Signal], *, dry_run: bool) -> str:
    chunks = sorted(session_dir.glob("telemetry-[0-9]*.parquet"))
    frames = [
        pd.read_parquet(chunk, columns=["timestamp", "source", "channel", "payload"])
        for chunk in chunks
    ]
    combined = pd.concat(frames, ignore_index=True)
    can_rows = combined[(combined["source"] == "can") & combined["payload"].notna()]
    if can_rows.empty:
        return "no raw CAN rows"

    decoded = decode_session_frame(can_rows, signals)
    if decoded.empty:
        return "no bound signals present"

    summary = ", ".join(
        f"{ch}:{n}" for ch, n in decoded["channel"].value_counts().sort_index().items()
    )
    if dry_run:
        return f"would write {len(decoded)} rows ({summary})"

    table = _PA.Table.from_pandas(decoded, schema=TELEMETRY_SCHEMA, preserve_index=False)
    out_path = session_dir / f"{BACKFILL_STEM}.parquet"
    _PQ.write_table(table, out_path, compression="snappy")
    return f"wrote {len(decoded)} rows ({summary}) -> {out_path.name}"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = CanaryConfig.from_yaml(args.config)
    root = config.collection.storage_path
    if not root.is_dir():
        print(f"error: storage path does not exist: {root}", file=sys.stderr)
        return 1
    if not args.bindings.exists():
        print(f"error: bindings file not found: {args.bindings}", file=sys.stderr)
        return 1
    bindings = load_bindings(args.bindings)
    levels = set(args.confidence)
    signals = [s for s in bindings.signals if s.confidence in levels]
    if not signals:
        print(f"error: no bindings at confidence {sorted(levels)}", file=sys.stderr)
        return 1

    dirs = _session_dirs(root, args.session)
    if not dirs:
        print("no sessions found", file=sys.stderr)
        return 1

    for session_dir in dirs:
        state = _decoded_row_state(session_dir)
        if state == "ingest":
            print(f"{session_dir.name}: skip (decoded rows persisted at ingest)")
            continue
        if state == "backfill" and not args.force:
            print(f"{session_dir.name}: skip (already backfilled; --force to refresh)")
            continue
        if state == "backfill" and args.force and not args.dry_run:
            for stale in session_dir.glob(f"{BACKFILL_STEM}*.parquet"):
                stale.unlink()
        result = backfill_session(session_dir, signals, dry_run=args.dry_run)
        print(f"{session_dir.name}: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
