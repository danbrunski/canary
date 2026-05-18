"""Pure file-backed dashboard readers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.alerts.event_logger import read_events_dir
from src.config import CanaryConfig
from src.dashboard.paths import DEFAULT_BINDINGS_PATH
from src.dashboard.status import _resolve_latest_session
from src.decode.behavior_models import (
    DEFAULT_BEHAVIOR_MODELS_PATH,
    load_behavior_models,
)
from src.decode.bindings import load_bindings
from src.preprocessing.decode import decoded_channel
from src.preprocessing.loader import PARQUET_PATTERN


def read_latest_telemetry_rows(
    storage_path: Path,
    *,
    n: int = 100,
) -> list[dict[str, Any]]:
    """Return the last ``n`` rows of the newest session, spanning chunks."""
    latest = _resolve_latest_session(storage_path)
    if latest is None:
        return []
    chunks = sorted(latest.glob(PARQUET_PATTERN))
    if not chunks:
        return []
    collected: list[pd.DataFrame] = []
    total = 0
    for chunk in reversed(chunks):  # newest-first; stop once we have >= n
        if chunk.stat().st_size == 0:
            continue
        df = pd.read_parquet(chunk)
        collected.append(df)
        total += len(df)
        if total >= n:
            break
    table = (
        collected[0]
        if len(collected) == 1
        else pd.concat(collected[::-1], ignore_index=True)  # back to chronological
    )
    if len(table) > n:
        table = table.tail(n)
    rows: list[dict[str, Any]] = []
    for record in table.to_dict(orient="records"):
        rows.append(_normalize_telemetry_row({str(k): v for k, v in record.items()}))
    return rows


_PER_CHANNEL_COLUMNS = [
    "timestamp",
    "source",
    "channel",
    "value_number",
    "value_text",
    "value_bool",
    "unit",
]


def read_latest_per_channel(
    storage_path: Path,
    *,
    window_seconds: float = 90.0,
    max_chunks: int = 300,
) -> list[dict[str, Any]]:
    """Return the most recent row for every distinct channel in the newest session."""
    latest = _resolve_latest_session(storage_path)
    if latest is None:
        return []
    chunks = [c for c in sorted(latest.glob(PARQUET_PATTERN)) if c.stat().st_size > 0]
    if not chunks:
        return []
    collected: list[pd.DataFrame] = []
    for chunk in reversed(chunks[-max_chunks:]):  # newest-first
        collected.append(pd.read_parquet(chunk, columns=_PER_CHANNEL_COLUMNS))
        spanned = pd.concat(collected) if len(collected) > 1 else collected[0]
        ts = pd.to_datetime(spanned["timestamp"], utc=True)
        if (ts.max() - ts.min()).total_seconds() >= window_seconds:
            break
    table = pd.concat(collected[::-1], ignore_index=True)
    table["timestamp"] = pd.to_datetime(table["timestamp"], utc=True)
    table = table.sort_values("timestamp", kind="stable")
    latest_rows = table.groupby(["source", "channel"], as_index=False).last()
    rows: list[dict[str, Any]] = []
    for record in latest_rows.to_dict(orient="records"):
        rows.append(_normalize_telemetry_row({str(k): v for k, v in record.items()}))
    return rows


def read_obd_coverage(storage_path: Path) -> dict[str, Any]:
    """OBD capability/coverage derived from observed telemetry."""
    from src.analysis.obd_coverage import build_coverage_report, channels_to_pids

    latest = _resolve_latest_session(storage_path)
    observed: set[str] = set()
    if latest is not None:
        for chunk in sorted(latest.glob(PARQUET_PATTERN)):
            try:
                df = pd.read_parquet(chunk, columns=["source", "channel"])
            except (OSError, ValueError, KeyError):
                continue
            obd_rows = df[df["source"] == "obd"]
            observed.update(str(c) for c in obd_rows["channel"].unique())
    supported = channels_to_pids(observed)
    return build_coverage_report(supported).to_dict()


def _normalize_telemetry_row(record: dict[str, Any]) -> dict[str, Any]:
    """Collapse value columns into one stable dashboard ``value`` field."""
    value = record.get("value_number")
    if value is None or pd.isna(value):
        value = record.get("value_text")
    if value is None or (isinstance(value, float) and pd.isna(value)):
        value = record.get("value_bool")
    return {
        "timestamp": _iso(record.get("timestamp")),
        "source": record.get("source"),
        "channel": record.get("channel"),
        "value": None if (isinstance(value, float) and pd.isna(value)) else value,
        "unit": record.get("unit"),
    }


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[no-any-return]
    return str(value)


def read_anomalies_since(
    anomalies_dir: Path,
    *,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Read anomalies from ``anomalies_dir`` with optional ISO timestamp filter."""
    if not anomalies_dir.exists():
        return []
    events = read_events_dir(anomalies_dir)
    if since is not None:
        # Validate by parse-roundtrip; we don't need the result, just to fail
        # loudly on garbage.
        pd.Timestamp(since)
        events = [e for e in events if e.timestamp >= since]
    return [e.to_dict() for e in events]


def echo_config(config: CanaryConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")


def read_bindings_for_display(
    path: Path = DEFAULT_BINDINGS_PATH,
    *,
    confidence_levels: tuple[str, ...] = ("confirmed", "high", "medium"),
) -> dict[str, Any]:
    """Surface the display-relevant subset of ``bindings.yaml`` for the frontend."""
    if not path.exists():
        return {}
    bindings = load_bindings(path)
    out: dict[str, dict[str, Any]] = {}
    for signal in bindings.signals:
        if signal.confidence not in confidence_levels:
            continue
        out[decoded_channel(signal.name)] = {
            "name": signal.name,
            "scale": signal.scale,
            "offset": signal.offset,
            "unit": signal.unit,
            "confidence": signal.confidence,
            "plausible_min": signal.plausible_min,
            "plausible_max": signal.plausible_max,
        }
    return out


def read_behavior_models(path: Path = DEFAULT_BEHAVIOR_MODELS_PATH) -> dict[str, Any]:
    """Return ``behavior_models.yaml`` as a JSON-safe payload for the dashboard."""
    if not path.exists():
        return {"schema_version": None, "models": []}
    return load_behavior_models(path).model_dump(mode="json")


__all__ = [
    "echo_config",
    "read_anomalies_since",
    "read_behavior_models",
    "read_bindings_for_display",
    "read_latest_per_channel",
    "read_latest_telemetry_rows",
    "read_obd_coverage",
]
