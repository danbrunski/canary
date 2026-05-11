"""Parquet session loading utilities for preprocessing."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

import pandas as pd

PARQUET_PATTERN = "telemetry-*.parquet"
INDEX_COLUMNS = ["session", "timestamp"]
TelemetryValueKind = Literal["value", "value_number", "value_text", "value_bool", "payload"]


def discover_sessions(root: Path) -> list[Path]:
    """Return session directories under root that contain telemetry Parquet chunks."""
    if not root.exists():
        return []
    return sorted(
        child for child in root.iterdir() if child.is_dir() and list(child.glob(PARQUET_PATTERN))
    )


def read_session(session_dir: Path) -> pd.DataFrame:
    """Read one telemetry session directory into a long-format DataFrame."""
    files = sorted(session_dir.glob(PARQUET_PATTERN))
    if not files:
        raise FileNotFoundError(f"no {PARQUET_PATTERN} files in {session_dir}")

    frames = [pd.read_parquet(path) for path in files]
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
) -> pd.DataFrame:
    """Pivot long telemetry rows to one column per `source:channel`."""
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
    return pivoted.reset_index().sort_values(list(index_columns), kind="stable")


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
