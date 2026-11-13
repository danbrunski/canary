"""Decode CAN payloads into named signal columns using data/bindings.yaml.

Bridges the two halves of CANary:

  RE half:  captures → data/bindings.yaml (signal byte positions) →
            src/decode/bindings.py
  ML half:  loader → resample → segment → features → scaler → IsolationForest

Without this module the preprocessing pipeline sees CAN payloads only as
opaque `object`-dtype byte blobs — `compute_features` correctly skips them,
so the anomaly detector has nothing from the CAN bus to learn from. After
this, every binding becomes a numeric column the model can use, on data we
already have (no ELM327 required).

Decoded rows use `source="decoded"`, `channel=<signal name>`, so the
existing `to_wide` produces columns like `decoded:ECM_COOLANT_TEMP`. The
value is the RAW integer extracted via the binding's byte range (NOT the
scaled engineering value) — for anomaly detection the scaler normalizes
anyway, and many bindings don't have a pinned scale yet. `decode_bindings.py`
still uses `apply_scaling` for human-readable reports.

Note on scale: a bound CAN ID at ~10ms produces ~6k decoded rows/minute per
binding; over a multi-hour capture across ~16 bindings that's millions of
transient rows during the `to_wide` pivot (which then collapses to one
column per signal, downsampled by the resampler). Fine for the session
lengths we record now; revisit if multi-hour captures strain memory.

**Decoded rows are decoded on read, never stored (P02.8, operator
2026-09-26).** Raw CAN frames with their payloads are the source of truth.
Until then the recorder also stored a ``decoded`` row per confirmed (or
``persist: true``) binding per frame: 28 % of every session's rows, 95 % of
them repeating the previous value, and all of them recomputable (0 of 2.70 M
same-instant pairs differed, P01 round 2). :func:`decode_on_read` is the one
place those rows come from now: the session loader
(:mod:`src.preprocessing.session_reader`) replaces any stored ``decoded`` row with it,
for old sessions and new alike, so every reader sees the same rows with the
current catalog. Old sessions keep their stored rows on disk; nothing reads
them. (``tools/backfill_decoded.py``, which wrote ``decoded`` rows into old
sessions, is retired with it: it rewrote sessions to store what is now
decoded on read.)

**This module is a row transform, not a reader (R13).** It imports no session
reader: the two functions that read a session and then decode it
(``read_session_decoded``, ``read_session_decoded_subset``) live in
:mod:`src.preprocessing.decoded_reader`, above both this module and
``session_reader``. :func:`__getattr__` below keeps their old path here
resolving.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterable
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

from src.artifact_hash import sha256_json
from src.can_ids import parse_can_channel
from src.decode.bindings import Bindings, Signal
from src.decode.on_read import can_channel_spellings
from src.decode.slices import payload_lengths, sentinel_channel, slice_raw_column
from src.paths import BINDINGS_YAML
from src.telemetry import CAN_SOURCE, channel_key
from src.telemetry import DECODED_SOURCE as DECODED_SOURCE

DEFAULT_BINDINGS_PATH = BINDINGS_YAML


def decoded_channel(signal_name: str) -> str:
    """Return the wide-column name a decoded signal will occupy after `to_wide`."""
    return channel_key(DECODED_SOURCE, signal_name)


def _raw_column(payloads: pd.Series, signal: Signal) -> np.ndarray[Any, np.dtype[Any]]:
    """``signal``'s raw integer in each payload, as float64; NaN where a
    payload is not bytes or is too short for the slice
    (:func:`src.decode.slices.slice_raw_column`, the vectorized
    :func:`~src.decode.slices.slice_raw`: a session's decode on read runs over
    millions of frames)."""
    values = payloads.to_numpy(dtype=object)
    return _slice_column(values, payload_lengths(values), signal)


def _slice_column(
    values: np.ndarray[Any, np.dtype[Any]],
    lengths: np.ndarray[Any, np.dtype[Any]],
    signal: Signal,
) -> np.ndarray[Any, np.dtype[Any]]:
    column: np.ndarray[Any, np.dtype[Any]] = slice_raw_column(
        values,
        lengths,
        signal.byte_start,
        signal.byte_length,
        signal.byte_order,
        signed=signal.signed,
    )
    return column


def decode_signal_column(payloads: pd.Series, signal: Signal) -> np.ndarray[Any, np.dtype[Any]]:
    """Raw integer value of ``signal`` in each payload, as float64 (R04.4).

    NaN where a payload is not bytes or is too short for the binding's byte
    range, and where it holds the binding's declared sentinel word (A04.6: a
    state, not a number — :func:`decode_sentinel_mask` finds those). The byte
    slice is :func:`src.decode.slices.slice_raw`'s (vectorized in
    :func:`_raw_column`), the rule the live ``FrameDecoder`` applies per
    frame, so the batch paths and the live path cannot extract a signal
    differently.
    """
    values = _raw_column(payloads, signal)
    if signal.sentinel_raw is None:
        return values
    return np.where(values == signal.sentinel_raw, np.nan, values)


def decode_sentinel_mask(payloads: pd.Series, signal: Signal) -> np.ndarray[Any, np.dtype[Any]]:
    """``True`` where a payload holds ``signal``'s declared sentinel word.

    All ``False`` for a binding without one. The rows it marks are the
    ``decoded:<NAME>_SENTINEL`` state rows (``sentinel_frame``), exactly where
    :func:`decode_signal_column` returns NaN for the sentinel.
    """
    if signal.sentinel_raw is None:
        return np.zeros(len(payloads), dtype=bool)
    return np.asarray(_raw_column(payloads, signal) == signal.sentinel_raw, dtype=bool)


def sentinel_frame(
    timestamps: Any, signal: Signal, metadata_json: str, **columns: Any
) -> pd.DataFrame:
    """Long-format ``decoded:<NAME>_SENTINEL`` rows (value = the stated meaning).

    The batch twin of :func:`src.decode.frame_decoder.sentinel_record`, so a
    re-decoded session and a stored one carry the same state rows.
    ``columns`` adds session columns (``session``, ``session_path``, ...).
    """
    meaning = signal.sentinel_meaning or "sentinel"
    channel = sentinel_channel(signal.name)
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "source": DECODED_SOURCE,
            "channel": channel,
            "value_number": None,
            "value_text": meaning,
            "value_bool": None,
            "unit": "",
            "payload": None,
            "metadata_json": metadata_json,
            **columns,
        }
    )
    frame["value_number"] = frame["value_number"].astype("float64")
    return frame


def decode_can_rows(
    long_frame: pd.DataFrame,
    bindings: Bindings,
    *,
    confidence_levels: set[str] | None = None,
) -> pd.DataFrame:
    """Append decoded-signal rows to a long-format telemetry frame.

    For every CAN row (`source == "can"`) whose arbitration ID has at least
    one binding, emit one new row per binding:
      source="decoded", channel=signal.name, value=raw integer from the
      binding's byte range. Rows whose payload is too short for a binding's
      byte range get NaN.

    `confidence_levels`, when given, restricts decoding to bindings at those
    confidence levels (e.g. `{"high", "medium"}` to skip hypothesized).
    Default `None` decodes all bindings.

    Every selected signal is decoded, always (P02.8): ``decoded`` rows of a
    signal already in the frame (and of its ``_SENTINEL`` channel) are
    replaced by the recomputed ones, never kept beside them or preferred to
    them. Such rows are the loader's own decode on read
    (:func:`decode_on_read`, same values), or a stored row of an old
    session that an ``as_recorded`` read passed through.

    Returns a new frame with the decoded rows concatenated and re-sorted by
    timestamp. The input is not mutated. If `long_frame` is empty or no
    binding is selected, it's returned unchanged; with no CAN row a selected
    binding reads, it's returned without the replaced rows.
    """
    if long_frame.empty or "source" not in long_frame.columns:
        return long_frame

    signals = bindings.signals
    if confidence_levels is not None:
        signals = [s for s in signals if s.confidence in confidence_levels]
    if not signals:
        return long_frame
    replaced = {s.name for s in signals} | {sentinel_channel(s.name) for s in signals}
    earlier = (long_frame["source"] == DECODED_SOURCE) & long_frame["channel"].isin(replaced)
    if bool(earlier.any()):
        long_frame = long_frame.loc[~earlier]

    can_rows = long_frame[long_frame["source"] == "can"]
    if can_rows.empty:
        return long_frame

    by_can_id: dict[int, list[Signal]] = {}
    for sig in signals:
        by_can_id.setdefault(sig.can_id, []).append(sig)

    decoded_frames: list[pd.DataFrame] = []
    # Group by the hex channel string so we parse the CAN ID once per group.
    for channel, group in can_rows.groupby("channel", sort=False):
        can_id = parse_can_channel(str(channel))
        if can_id is None or can_id not in by_can_id:
            continue
        payloads = group["payload"]
        n = len(group)
        timestamps = group["timestamp"].to_numpy()
        sessions = group["session"].to_numpy()
        session_paths = (
            group["session_path"].to_numpy() if "session_path" in group.columns else [None] * n
        )
        for sig in by_can_id[can_id]:
            meta = json.dumps(
                {"can_id": can_id, "binding_confidence": sig.confidence},
                sort_keys=True,
            )
            values = decode_signal_column(payloads, sig)
            numeric = pd.DataFrame(
                {
                    "timestamp": timestamps,
                    "source": DECODED_SOURCE,
                    "channel": sig.name,
                    "value_number": values,
                    "value_text": None,
                    "value_bool": None,
                    "unit": sig.display_unit,
                    "payload": None,
                    "metadata_json": meta,
                    "session": sessions,
                    "session_path": session_paths,
                    "source_channel": decoded_channel(sig.name),
                    "value": values,
                }
            )
            sentinel = decode_sentinel_mask(payloads, sig)
            if sentinel.any():
                # A04.6: the sentinel word is a state row, not a NaN number
                # row, the same split FrameDecoder makes on the live path.
                numeric = numeric[~sentinel]
                state = sentinel_frame(
                    timestamps[sentinel],
                    sig,
                    meta,
                    session=np.asarray(sessions)[sentinel],
                    session_path=np.asarray(session_paths, dtype=object)[sentinel],
                    source_channel=decoded_channel(sentinel_channel(sig.name)),
                )
                state["value"] = state["value_text"]
                decoded_frames.append(state)
            decoded_frames.append(numeric)

    if not decoded_frames:
        return long_frame

    combined = pd.concat([long_frame, *decoded_frames], ignore_index=True)
    return combined.sort_values(["timestamp", "source", "channel"], kind="stable").reset_index(
        drop=True
    )


def decoded_subset_plan(
    bindings: Bindings | None, decoded: Iterable[str]
) -> tuple[list[list[tuple[str, str, Any]]], Bindings | None, set[int]]:
    """``(filters, subset, ids)`` for :func:`read_session_decoded_subset`: the
    parquet pushdown, the narrowed bindings and the CAN ids they read. Split
    out so a part-by-part reader (``plausibility.offline.load_stream``) reads
    exactly the rows the whole-session reader does."""
    wanted = set(decoded)
    signals = [s for s in bindings.signals if s.name in wanted] if bindings is not None else []
    subset = bindings.model_copy(update={"signals": signals}) if bindings and signals else None
    filters: list[list[tuple[str, str, Any]]] = [[("source", "!=", "can")]]
    ids = {signal.can_id for signal in signals}
    if ids:
        filters.append([("source", "==", "can"), ("channel", "in", can_channel_spellings(ids))])
    return filters, subset, ids


def keep_can_ids(long: pd.DataFrame, ids: set[int]) -> pd.DataFrame:
    """Drop raw CAN rows whose parsed id is not in ``ids`` (the pushdown matched
    spellings; this matches the id exactly)."""
    is_can = (long["source"] == "can").to_numpy()
    if is_can.any():
        parsed = long.loc[is_can, "channel"].map(lambda c: parse_can_channel(str(c)))
        keep = np.ones(len(long), dtype=bool)
        keep[np.flatnonzero(is_can)] = parsed.isin(ids).to_numpy(dtype=bool)
        long = long.loc[keep].reset_index(drop=True)
    return long


# ---------------------------------------------------------------------------
# Decode on read (P02.8)
# ---------------------------------------------------------------------------


def on_read_hash(catalog: Bindings | None) -> str:
    """A version of ``catalog``'s decode, for caches of what readers see
    (the derived store's ``value_number`` frame): the on-read signals' specs,
    in order. Any edit to one of them changes it."""
    specs = [] if catalog is None else [s.model_dump(mode="json") for s in catalog.signals]
    # compact=False: on_read_hash in each stored frame's meta.json was made so
    return sha256_json(specs, compact=False)[:16]


def on_read_can_ids(catalog: Bindings | None) -> set[int]:
    """The CAN ids ``catalog``'s signals read (empty without a catalog)."""
    return set() if catalog is None else {signal.can_id for signal in catalog.signals}


def decode_on_read(frame: pd.DataFrame, catalog: Bindings | None) -> pd.DataFrame:
    """``frame``'s rows as every reader sees them (P02.8).

    Stored ``decoded`` rows are dropped, and each raw CAN row that a
    ``catalog`` signal reads (:func:`on_read_catalog`) becomes exactly the
    rows the recorder's storage decoder used to write for it
    (``FrameDecoder.decode``): the raw integer as float64 for a slice the
    payload holds, the ``<NAME>_SENTINEL`` state row (value text = the stated
    meaning) for the declared sentinel word, and no row for a payload too
    short for the slice. So a session recorded before P02.8 and a copy with
    its ``decoded`` rows removed read identically, and both read with the
    current catalog.

    The new rows carry ``frame``'s own columns, each in its dtype: ``unit``
    the binding's display unit, ``metadata_json`` ``{"binding_confidence",
    "can_id"}`` (the stored rows' serialization), ``payload`` null, and any
    other column (``session``, ...) copied from the frame the row came from.
    Each follows its frame directly, in catalog signal order, where the
    storage decoder wrote it, so even a reader of row order (the last N rows)
    sees the stored sequence. The index is reset.
    """
    if frame.empty or "source" not in frame.columns:
        return frame
    stored = (frame["source"] == DECODED_SOURCE).to_numpy(dtype=bool)
    base = frame.loc[~stored] if stored.any() else frame
    if catalog is None or "payload" not in base.columns:
        return base
    decoded = _on_read_rows(base, catalog)
    if decoded is None:
        return base
    rows, frame_positions = decoded
    order = np.argsort(
        np.concatenate([2 * np.arange(len(base)), 2 * frame_positions + 1]), kind="stable"
    )
    return pd.concat([base, rows], ignore_index=True).iloc[order].reset_index(drop=True)


def _on_read_rows(
    frame: pd.DataFrame, catalog: Bindings
) -> tuple[pd.DataFrame, np.ndarray[Any, np.dtype[Any]]] | None:
    """The rows :func:`decode_on_read` adds to ``frame`` (no stored ones in
    it) and each one's frame position, built as one frame: a session is
    decoded chunk by chunk or part by part, so per-signal frames would cost
    more than the decode itself."""
    is_can = (frame["source"] == CAN_SOURCE).to_numpy(dtype=bool)
    if not is_can.any():
        return None
    by_id: dict[int, list[Signal]] = {}
    for signal in catalog.signals:
        by_id.setdefault(signal.can_id, []).append(signal)
    can_positions = np.flatnonzero(is_can)
    # One hash pass over the CAN rows' channel spellings; each id's rows are
    # then an integer compare, not a string compare per spelling.
    codes, spellings = pd.factorize(frame["channel"].iloc[can_positions])
    payloads = frame["payload"].to_numpy(dtype=object)[can_positions]
    positions: list[np.ndarray[Any, np.dtype[Any]]] = []
    values: list[np.ndarray[Any, np.dtype[Any]]] = []
    labels: list[tuple[str, str | None, str, str, int]] = []
    for code, channel in enumerate(spellings):
        can_id = parse_can_channel(str(channel))
        signals = by_id.get(can_id) if can_id is not None else None
        if not signals:
            continue
        in_channel = codes == code
        group = can_positions[in_channel]
        group_payloads = payloads[in_channel]
        lengths = payload_lengths(group_payloads)
        for signal in signals:
            raw = _slice_column(group_payloads, lengths, signal)
            if signal.sentinel_raw is None:
                sentinel = np.zeros(raw.size, dtype=bool)
            else:
                sentinel = raw == float(signal.sentinel_raw)
            numeric = ~np.isnan(raw) & ~sentinel
            meta = json.dumps(
                {"can_id": can_id, "binding_confidence": signal.confidence}, sort_keys=True
            )
            for mask, name, text, unit, value in (
                (numeric, signal.name, None, signal.display_unit, raw),
                (
                    sentinel,
                    sentinel_channel(signal.name),
                    signal.sentinel_meaning or "sentinel",
                    "",
                    np.full(raw.size, np.nan),
                ),
            ):
                count = int(mask.sum())
                if count:
                    positions.append(group[mask])
                    values.append(value[mask])
                    labels.append((name, text, unit, meta, count))
    if not positions:
        return None
    at = np.concatenate(positions)
    return _rows_from(frame, at, np.concatenate(values), labels), at


def _rows_from(
    frame: pd.DataFrame,
    positions: np.ndarray[Any, np.dtype[Any]],
    values: np.ndarray[Any, np.dtype[Any]],
    labels: list[tuple[str, str | None, str, str, int]],
) -> pd.DataFrame:
    """``decoded`` rows at ``positions`` of ``frame`` (their CAN rows), in
    ``frame``'s columns and dtypes; ``labels`` names each run of rows.

    Every column keeps the frame's dtype: a stored row read back from parquet
    has a ``str`` value_text (NaN when null), a tz-aware timestamp, and so on,
    and the concatenated frame must not change type under them.
    """
    counts = np.array([label[4] for label in labels], dtype=np.int64)
    index = pd.RangeIndex(positions.size)

    def repeated(field: int, column: str) -> Any:
        return _repeated([label[field] for label in labels], counts, frame[column].dtype)

    fields: dict[str, Any] = {
        "source": lambda: pd.Series(DECODED_SOURCE, index=index, dtype=frame["source"].dtype),
        "channel": lambda: repeated(0, "channel"),
        "value_number": lambda: pd.Series(values, index=index, dtype=frame["value_number"].dtype),
        "value_text": lambda: repeated(1, "value_text"),
        "value_bool": lambda: pd.Series(None, index=index, dtype=frame["value_bool"].dtype),
        "unit": lambda: repeated(2, "unit"),
        "payload": lambda: pd.Series(None, index=index, dtype=frame["payload"].dtype),
        "metadata_json": lambda: repeated(3, "metadata_json"),
        "source_channel": lambda: _repeated(
            [f"{DECODED_SOURCE}:{label[0]}" for label in labels],
            counts,
            frame["source_channel"].dtype,
        ),
        "value": lambda: pd.Series(
            np.where(
                np.repeat(np.array([label[1] is None for label in labels]), counts),
                values.astype(object),
                np.repeat(np.array([label[1] for label in labels], dtype=object), counts),
            ),
            index=index,
            dtype=frame["value"].dtype,
        ),
    }
    data = {
        column: (
            fields[column]()
            if column in fields
            else frame[column].iloc[positions].reset_index(drop=True)
        )
        for column in frame.columns
    }
    return pd.DataFrame(data, index=index)


def _repeated(labels: list[Any], counts: np.ndarray[Any, np.dtype[Any]], dtype: Any) -> Any:
    """``labels[i]`` repeated ``counts[i]`` times, as a column of ``dtype``.

    A pyarrow-backed ``str`` column is built by an Arrow ``take`` over the few
    distinct labels (6x faster than converting a Python object per row, and a
    session's decode on read makes millions of rows); anything else from an
    object array.
    """
    if isinstance(dtype, pd.StringDtype) and dtype.storage == "pyarrow":
        codes = pa.array(np.repeat(np.arange(len(labels)), counts))
        column = pa.array(labels, type=pa.large_string()).take(codes)
        return pd.Series(pd.array(column, dtype=dtype))
    return pd.Series(np.repeat(np.array(labels, dtype=object), counts), dtype=dtype)


#: Names that moved to :mod:`src.preprocessing.decoded_reader` (R13), still
#: importable from here. Resolved by name on first access: a static import
#: would put the reader back under the module it must sit above.
_MOVED_TO_DECODED_READER = frozenset({"read_session_decoded", "read_session_decoded_subset"})


def __getattr__(name: str) -> Any:
    if name in _MOVED_TO_DECODED_READER:
        value = getattr(importlib.import_module("src.preprocessing.decoded_reader"), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
