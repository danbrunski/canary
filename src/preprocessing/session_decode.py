"""Decode a session's raw rows and reduce them to resampler cells (R09.6).

Moved out of ``feature_pipeline`` so the layers below feature assembly (the
derived store, plausibility's offline reader) can decode and reduce a session
without importing it. Nothing here builds a feature.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd

from src.decode.dbc import decode_can_rows_dbc
from src.decode.dids import decode_did_rows
from src.preprocessing.decode import decode_can_rows
from src.preprocessing.derived import ReducedSession, grid_interval_ns, reduce_long_frames
from src.preprocessing.loader import SESSION_PART_ROWS, iter_session_parts
from src.preprocessing.resampler import ResampleConfig
from src.storage.broadcast_storage import session_can_storage

#: Raw CAN rows fed through the decoders per chunk. Decoding is row-local (each
#: CAN row yields its own decoded rows; no cross-row state), so chunking is
#: exactly equivalent to decoding the frame in one pass — it only bounds peak
#: memory. Sized so one chunk plus its decoded output stays in the low hundreds
#: of MB at the ~2.2 signals/frame this vehicle's bindings+DBC produce.
_CAN_DECODE_CHUNK_ROWS = 250_000

#: Canonical long-frame ordering, matching ``read_session``'s own sort. The
#: chunked decode reassembles rows out of order, and ``to_wide``'s
#: ``aggfunc="last"`` is order-sensitive, so the pieces are re-sorted on this
#: key before the pivot.
_LONG_SORT_COLUMNS = ["session", "timestamp", "source", "channel"]


def decode_long_frame(
    long: pd.DataFrame,
    bindings: Any,
    dbc_database: Any,
    did_map: Any,
) -> pd.DataFrame:
    """Decode one session's long frame, chunking the CAN decode.

    Raw CAN frames are ~99% of the rows and carry no numeric value (their bytes
    live in ``payload``); they exist only to be decoded into ``decoded:*`` /
    ``dbc:*`` rows. Decoding them in a single pass holds every raw row *and*
    every decoded row at once — on one dense 45-minute drive that is ~4.5M raw
    plus ~10M decoded object-dtype rows, which OOM-killed feature-build workers
    at ~7.6 GB RSS (2026-07-26, both the 4-worker and the 2-worker attempt).
    Decoding in chunks and keeping only each chunk's decoded output bounds peak
    memory by the chunk instead of the session.

    Equivalent to the old single-pass decode, not an approximation: both
    decoders emit one row per (CAN row × matching signal) with no dependence on
    neighbouring rows, and the result is re-sorted on ``_LONG_SORT_COLUMNS``
    before it reaches the order-sensitive pivot.
    """
    if long.empty or "source" not in long.columns:
        return long
    is_can = long["source"] == "can"
    # DID decoding reads Mode-22 ``DID_<hex>`` OBD rows, never CAN rows, so it
    # runs once on the non-CAN side rather than per chunk.
    rest = long.loc[~is_can]
    if did_map is not None:
        rest = decode_did_rows(rest, did_map)
    if bindings is None and dbc_database is None:
        return rest.sort_values(_LONG_SORT_COLUMNS, kind="stable")

    # Every binding is recomputed from the raw frames here. A session this
    # chain reads (``decoded="as_recorded"``) may still carry the ``decoded:*``
    # rows the daemon stored before P02.8 (2026-09-26); the chunks below hold
    # CAN rows alone, so both copies reach the pivot and the recomputed row
    # (sorted after the stored one) is the ``last`` it keeps. That is the live
    # path's answer (``FrameDecoder`` decodes every frame with the current
    # bindings), and on real sessions the two copies agree: 0 of 2.70M
    # same-instant pairs differed across 2026-07-29T205841,
    # 2026-08-07T161543, 2026-09-18T160921 and
    # _capture-fuel-target-research-2026-09-21T195844 (2026-09-26). After a
    # binding edit the recomputed value is the current decode, as live.
    # Reading sessions on read instead would change no frame, only add a
    # second decode of the on-read signals that loses to this one; a session
    # recorded after P02.8 has no stored row to lose.
    can_rows = long.loc[is_can]
    pieces: list[pd.DataFrame] = [rest]
    for start in range(0, len(can_rows), _CAN_DECODE_CHUNK_ROWS):
        chunk = can_rows.iloc[start : start + _CAN_DECODE_CHUNK_ROWS]
        if bindings is not None:
            chunk = decode_can_rows(chunk, bindings)
        if dbc_database is not None:
            chunk = decode_can_rows_dbc(chunk, dbc_database)
        # Keep only what the decode produced; the raw CAN rows that seeded it
        # are dead weight downstream (all-NaN pivot columns) and dropping them
        # here is what keeps the accumulated pieces small.
        decoded = chunk.loc[chunk["source"] != "can"]
        if not decoded.empty:
            pieces.append(decoded)
        del chunk, decoded
    combined = pd.concat(pieces, ignore_index=True) if len(pieces) > 1 else pieces[0]
    return combined.sort_values(_LONG_SORT_COLUMNS, kind="stable")


def reduce_session(
    session_dir: Path,
    bindings: Any,
    dbc_database: Any,
    did_map: Any,
    resample_config: ResampleConfig,
    *,
    part_rows: int = SESSION_PART_ROWS,
    filters: Any = None,
    raw_channels: set[tuple[str, str]] | None = None,
) -> ReducedSession:
    """Decode a session part by part and reduce it (P01.1).

    Each time-disjoint part (:func:`iter_session_parts`) is decoded on its own,
    which is exact: decoding is row-local and no pivot cell spans two parts.
    ``filters`` narrows the raw read (a per-channel re-derive reads only the
    rows its decoder needs). ``raw_channels``, when given, collects every
    recorded ``(source, channel)`` pair before decoding (the derived store
    versions its decode units against them).
    """
    interval_ns = grid_interval_ns(resample_config)

    def decoded() -> Iterator[pd.DataFrame]:
        # As recorded: this chain recomputes every binding itself, and the
        # recomputed row wins over a stored one (``decode_long_frame``).
        parts = iter_session_parts(
            session_dir, target_rows=part_rows, filters=filters, decoded="as_recorded"
        )
        for long in parts:
            if raw_channels is not None and not long.empty:
                pairs = long[["source", "channel"]].astype(str).drop_duplicates()
                raw_channels.update(zip(pairs["source"], pairs["channel"], strict=True))
            yield decode_long_frame(long, bindings, dbc_database, did_map)

    return reduce_long_frames(
        session_dir.name, decoded(), interval_ns, can_storage=session_can_storage(session_dir)
    )


__all__ = ["decode_long_frame", "reduce_session"]
