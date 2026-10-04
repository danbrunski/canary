#!/usr/bin/env python
"""Cut a small, committable slice of a real session for the ground-truth suite.

The regression suite in ``tests/test_analysis_ground_truth.py`` needs data
whose answers are already known, and it has to be *real* data: bytes off the
wire, written by the production parquet writer. A synthesised fixture would
only prove the analysis chain agrees with whatever assumptions built it, which
is exactly the circularity the suite exists to break.

So this extracts a window from a session on disk, keeping only the channels the
expectations name, and writes it under ``tests/fixtures/``. Sessions are
gitignored (``/sessions/``, ``*.parquet``); the fixture path is un-ignored
deliberately so this one slice travels with the repo.

Regenerate (paths are the operator's local pull, not the Pi)::

    .venv/bin/python tools/make_ground_truth_fixture.py \\
        --session sessions/nissan-xterra-2008/2026-08-03T163440 \\
        --start 2026-08-03T17:21:00-06:00 --seconds 120

The DID speed slice (``did-speed-slice``) adds the Mode 22 rows it compares,
which are stored ``source='obd'`` under their ``DID_<hex>`` channel::

    .venv/bin/python tools/make_ground_truth_fixture.py \\
        --session ~/canary-data/sessions/nissan-xterra-2008/_capture-rom-trace-2026-09-21T142926 \\
        --start 2026-09-21T08:29:26-06:00 --seconds 212 --can-stride 10 \\
        --can-id 0x6F7 --keep-obd DID_1102 --keep-obd DID_121A --keep-obd DID_1125 \\
        --out tests/fixtures/ground_truth/did-speed-slice

Keep the window short. This is a regression anchor, not a corpus — every byte
of it lives in git forever.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

from src.storage.broadcast_storage import ChangeOnlyStorageError, refuse_change_only

DEFAULT_OUT = Path("tests/fixtures/ground_truth/drive-slice")

#: Broadcast ids the expectations reference. 0x1F9/0x551/0x6F7 carry bindings
#: checked against Mode 01; 0x233 and 0x23D carry the coolant duplicates whose
#: exact byte relationship is itself an expectation.
KEEP_CAN_IDS = ("0x1F9", "0x551", "0x6F7", "0x233", "0x23D")

#: Mode 01 references. These are the independent ground truth — a binding is
#: only "known good" because a different instrument agrees with it.
KEEP_OBD = ("RPM", "SPEED", "COOLANT_TEMP", "THROTTLE_POS", "ENGINE_LOAD")

KEEP_GPS = ("speed", "heading")


def extract(
    session: Path,
    start: pd.Timestamp,
    seconds: float,
    out: Path,
    can_stride: int = 1,
    *,
    can_ids: tuple[str, ...] = KEEP_CAN_IDS,
    obd_channels: tuple[str, ...] = KEEP_OBD,
) -> pd.DataFrame:
    """Write the slice. Refuses a session that stores CAN frames change-only
    (P02.10): ``--can-stride`` keeps every Nth *frame* per id, which on
    change-only rows would keep every Nth payload change, and the slice is
    written without the source's storage footer, so it would read back as
    stored every frame and its rows as the bus's frames."""
    refuse_change_only(session, reader="ground-truth fixture slice")
    end = start + pd.Timedelta(seconds=seconds)
    pattern = str(session / "telemetry-*.parquet")
    con = duckdb.connect()
    frame = con.execute(
        f"""
        SELECT * FROM read_parquet('{pattern}')
        WHERE timestamp >= ? AND timestamp < ?
          AND (
                (source = 'can' AND list_contains(?, channel))
             OR (source = 'obd' AND list_contains(?, channel))
             OR (source = 'gps' AND channel IN {KEEP_GPS})
          )
        ORDER BY timestamp
        """,
        [start.to_pydatetime(), end.to_pydatetime(), list(can_ids), list(obd_channels)],
    ).df()
    if can_stride > 1:
        # Keep every Nth frame per broadcast id. These are still real frames —
        # nothing is resampled or interpolated — just fewer of them, which
        # buys a longer window (and so a slower channel's samples) for the
        # same bytes in git. A window long enough to move coolant matters more
        # than 100 Hz cadence, which no expectation depends on.
        can = frame[frame["source"] == "can"]
        kept = can[can.groupby("channel").cumcount() % can_stride == 0]
        frame = pd.concat([frame[frame["source"] != "can"], kept]).sort_values("timestamp")
        frame = frame.reset_index(drop=True)
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("telemetry-*.parquet"):
        stale.unlink()
    frame.to_parquet(out / "telemetry-00000.parquet", index=False)
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--start", required=True, help="ISO timestamp with offset")
    parser.add_argument("--seconds", type=float, default=420.0)
    parser.add_argument(
        "--can-stride",
        type=int,
        default=5,
        help="keep every Nth broadcast frame per id (real frames, just fewer)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--can-id",
        action="append",
        default=None,
        help=f"broadcast id to keep (repeatable; default {', '.join(KEEP_CAN_IDS)})",
    )
    parser.add_argument(
        "--keep-obd",
        action="append",
        default=[],
        help="extra source='obd' channel to keep, e.g. DID_1102 (repeatable)",
    )
    args = parser.parse_args()

    try:
        frame = extract(
            args.session,
            pd.Timestamp(args.start),
            args.seconds,
            args.out,
            can_stride=args.can_stride,
            can_ids=tuple(args.can_id) if args.can_id else KEEP_CAN_IDS,
            obd_channels=KEEP_OBD + tuple(args.keep_obd),
        )
    except ChangeOnlyStorageError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    size = sum(p.stat().st_size for p in args.out.glob("*.parquet"))
    print(f"wrote {args.out} — {len(frame):,} rows, {size / 1024:.0f} KiB")
    for (source, channel), rows in frame.groupby(["source", "channel"]):
        print(f"  {source:8s} {channel:16s} {len(rows):7,d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
