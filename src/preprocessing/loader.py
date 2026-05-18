"""Parquet session loading utilities for preprocessing."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

import pandas as pd

log = logging.getLogger(__name__)

PARQUET_PATTERN = "telemetry-*.parquet"
INDEX_COLUMNS = ["session", "timestamp"]
TelemetryValueKind = Literal["value", "value_number", "value_text", "value_bool", "payload"]


def discover_sessions(root: Path) -> list[Path]:
    """Return session directories under root that contain telemetry Parquet chunks.

    Directories whose name starts with ``_`` are skipped, so quarantined /
    schema-incomplete sessions can be moved under ``<root>/_archive/`` and
    excluded from every training run without touching the data.
    """
    if not root.exists():
        return []
    return sorted(
        child
        for child in root.iterdir()
        if child.is_dir()
        and not child.name.startswith("_")
        and any(f.stat().st_size > 0 for f in child.glob(PARQUET_PATTERN))
    )


def read_session(session_dir: Path) -> pd.DataFrame:
    """Read one telemetry session directory into a long-format DataFrame."""
    candidates = sorted(session_dir.glob(PARQUET_PATTERN))
    if not candidates:
        raise FileNotFoundError(f"no {PARQUET_PATTERN} files in {session_dir}")

    # A daemon crash mid-write leaves trailing 0-byte or truncated chunks
    # (the atomic writer's target appears before its content lands). One bad
    # tail chunk must not poison the whole training corpus — skip the
    # unreadable ones and keep going.
    frames: list[pd.DataFrame] = []
    skipped: list[str] = []
    for path in candidates:
        if path.stat().st_size == 0:
            skipped.append(path.name)
            continue
        try:
            frames.append(pd.read_parquet(path))
        except (OSError, ValueError, pd.errors.ParserError):
            skipped.append(path.name)
    if skipped:
        log.warning(
            "skipped %d unreadable chunk(s) in %s: %s",
            len(skipped),
            session_dir.name,
            ", ".join(skipped),
        )
    if not frames:
        raise FileNotFoundError(f"no readable {PARQUET_PATTERN} files in {session_dir}")
    frame = pd.concat(frames, ignore_index=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["session"] = session_dir.name
    frame["session_path"] = str(session_dir)
    frame["source_channel"] = frame["source"].astype(str) + ":" + frame["channel"].astype(str)
    frame["value"] = _coalesce_value_columns(frame)
    frame = frame.sort_values(["timestamp", "source", "channel"], kind="stable")
    return frame.reset_index(drop=True)


def read_sessions(session_dirs: Iterable[Path]) -> pd.DataFrame:
    """Read several session directories into one long-format DataFrame."""
    frames = [read_session(path) for path in session_dirs]
    if not frames:
        return _empty_long_frame()
    return pd.concat(frames, ignore_index=True).sort_values(
        ["session", "timestamp", "source", "channel"],
        kind="stable",
    )


def to_wide(
    telemetry: pd.DataFrame,
    *,
    value: TelemetryValueKind = "value",
    index_columns: Sequence[str] = INDEX_COLUMNS,
    coerce_numeric: bool = True,
) -> pd.DataFrame:
    """Pivot long telemetry rows to one column per `source:channel`.

    The `value` column is `object`-dtype (it coalesces numeric / text / bool
    values per row), so the pivot output is `object` too — which means the
    downstream resampler and feature stages would treat even genuinely
    numeric channels (OBD PIDs, decoded CAN signals) as non-numeric and skip
    them. With `coerce_numeric=True` (the default), each value column whose
    non-null entries are all numbers is cast to a numeric dtype after the
    pivot. Channels carrying strings (VIN) or bools are left untouched.
    """
    if telemetry.empty:
        return pd.DataFrame(columns=[*index_columns])

    required = {*index_columns, "source_channel", value}
    missing = required - set(telemetry.columns)
    if missing:
        raise KeyError(f"missing required columns: {', '.join(sorted(missing))}")

    pivoted = telemetry.pivot_table(
        index=list(index_columns),
        columns="source_channel",
        values=value,
        aggfunc="last",
        dropna=False,
    )
    pivoted.columns = [str(column) for column in pivoted.columns]
    wide = pivoted.reset_index().sort_values(list(index_columns), kind="stable")

    if coerce_numeric:
        index_set = set(index_columns)
        for column in wide.columns:
            if column in index_set:
                continue
            col = wide[column]
            if pd.api.types.is_numeric_dtype(col):
                # Already numeric (rare on the object-valued read_session
                # path; happens when to_wide is called with a numeric
                # `value` kind). Narrow float64 → float32; leave int / bool
                # channels untouched so their semantics don't change.
                if col.dtype == "float64":
                    wide[column] = col.astype("float32")
                continue
            converted = pd.to_numeric(col, errors="coerce")
            # Only keep the conversion if it didn't drop any real values —
            # i.e. every non-null original entry was numeric. A string-bearing
            # channel (VIN) or a bool channel stays as-is.
            original_notna = col.notna()
            if bool((original_notna == converted.notna()).all()):
                # Store as float32: telemetry is integer-origin sensor data
                # (≤16-bit raw range, simple scale/offset) — exactly
                # representable in a 24-bit mantissa, so this is lossless for
                # the values that matter. This is the dominant pre-resample
                # memory cost (the dense pivot); halving it is what actually
                # keeps full-corpus feature-building off the OOM line.
                # Scaler μ/σ still reduce in float64 (see normalization.py).
                wide[column] = converted.astype("float32")

    return wide


def channel_key(source: str, channel: str) -> str:
    """Return the canonical wide-column name for a telemetry channel."""
    return f"{source}:{channel}"


def _coalesce_value_columns(frame: pd.DataFrame) -> pd.Series:
    value = frame["value_number"].astype("object")
    text_mask = frame["value_text"].notna()
    bool_mask = frame["value_bool"].notna()
    value.loc[text_mask] = frame.loc[text_mask, "value_text"]
    value.loc[bool_mask] = frame.loc[bool_mask, "value_bool"]
    return value


def _empty_long_frame() -> pd.DataFrame:
    columns: list[str] = [
        "timestamp",
        "source",
        "channel",
        "value_number",
        "value_text",
        "value_bool",
        "unit",
        "payload",
        "metadata_json",
        "session",
        "session_path",
        "source_channel",
        "value",
    ]
    return pd.DataFrame(columns=columns)
