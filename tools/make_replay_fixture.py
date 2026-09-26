#!/usr/bin/env python
"""Cut a small replay-regression slice that *already contains* ``decoded`` rows.

``tests/fixtures/ground_truth/drive-slice`` holds only ``can``/``obd``/``gps``,
so a replay of it cannot show D02.9's doubling: every ``decoded`` row in its
output is a correct one-to-many re-derivation of a replayed frame. D03.7 needs
a source whose stored ``decoded``/``derived`` rows would be re-emitted beside
the re-derived ones if the emit filter regressed — which is what a real
post-2026-07-29 session looks like.

The slice keeps, from one window of a real session written by the production
writer:

- the raw ``can`` frames of the broadcast ids that carry decoded bindings,
  thinned to every Nth frame per id (real frames, just fewer);
- exactly the stored ``decoded`` rows those kept frames produced, matched on
  ``(timestamp, metadata.can_id)`` so the slice keeps an exact frame → rows
  correspondence (a doubled replay then reads 2.000x, not "about 2");
- a few Mode 01 references, every ``gps`` row and every ``derived`` row.

It deliberately keeps the shape of a session recorded **before P02.8**
(2026-09-26): nothing stores ``decoded`` rows since then, so such a session
is the only kind that can carry them, and the replay emit filter must still
skip them when an old session is replayed. The tool refuses a source with no
stored ``decoded`` rows rather than cut a slice that tests nothing.

Regenerate (the source is the D02.9 evidence session preserved on the laptop)::

    .venv/bin/python tools/make_replay_fixture.py \\
        --session ~/canary-data/_d029-evidence/2026-09-10T142711 \\
        --start 2026-09-10T08:32:00-06:00 --seconds 20

Keep the window short: every byte lives in git forever.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import pandas as pd

DEFAULT_OUT = Path("tests/fixtures/replay/decoded-slice")

#: Broadcast ids with confirmed bindings in ``data/bindings.yaml`` (their
#: stored ``decoded`` rows are what a regressed replay would double).
KEEP_CAN_IDS = ("0x1F9", "0x551", "0x23D", "0x6F7")

KEEP_OBD = ("RPM", "SPEED", "COOLANT_TEMP", "THROTTLE_POS")


def _can_id_of(metadata_json: object) -> int | None:
    if not isinstance(metadata_json, str) or not metadata_json:
        return None
    value = json.loads(metadata_json).get("can_id")
    return int(value) if value is not None else None


def extract(
    session: Path, start: pd.Timestamp, seconds: float, out: Path, can_stride: int
) -> pd.DataFrame:
    end = start + pd.Timedelta(seconds=seconds)
    pattern = str(session / "telemetry-*.parquet")
    frame = (
        duckdb.connect()
        .execute(
            f"""
            SELECT * FROM read_parquet('{pattern}')
            WHERE timestamp >= ? AND timestamp < ?
              AND (
                    (source = 'can' AND channel IN {KEEP_CAN_IDS})
                 OR (source = 'obd' AND channel IN {KEEP_OBD})
                 OR source IN ('decoded', 'derived', 'gps')
              )
            ORDER BY timestamp
            """,
            [start.to_pydatetime(), end.to_pydatetime()],
        )
        .df()
    )
    can = frame[frame["source"] == "can"]
    kept_can = can[can.groupby("channel").cumcount() % can_stride == 0]
    kept_keys = {
        (ts, int(channel, 16))
        for ts, channel in zip(kept_can["timestamp"], kept_can["channel"], strict=True)
    }
    decoded = frame[frame["source"] == "decoded"]
    if decoded.empty:
        raise SystemExit(
            f"error: {session.name} stores no decoded rows in that window: a session "
            "recorded after P02.8 (2026-09-26) never does, and this slice exists to "
            "carry them; cut it from an older recording"
        )
    decoded_keys = [
        (ts, _can_id_of(meta))
        for ts, meta in zip(decoded["timestamp"], decoded["metadata_json"], strict=True)
    ]
    kept_decoded = decoded[[key in kept_keys for key in decoded_keys]]
    rest = frame[frame["source"].isin(["obd", "gps", "derived"])]
    sliced = (
        pd.concat([kept_can, kept_decoded, rest])
        .sort_values("timestamp", kind="stable")
        .reset_index(drop=True)
    )
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("telemetry-*.parquet"):
        stale.unlink()
    sliced.to_parquet(out / "telemetry-00000.parquet", index=False)
    return sliced


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--start", required=True, help="ISO timestamp with offset")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--can-stride", type=int, default=10)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    frame = extract(
        args.session.expanduser(),
        pd.Timestamp(args.start),
        args.seconds,
        args.out,
        can_stride=args.can_stride,
    )
    size = sum(p.stat().st_size for p in args.out.glob("*.parquet"))
    print(f"wrote {args.out} — {len(frame):,} rows, {size / 1024:.0f} KiB")
    for source, rows in frame.groupby("source"):
        print(f"  {source:8s} {len(rows):7,d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
