"""Offline replay of a single session window through the inference pipeline.

Composes the existing pieces — :func:`scripts.replay.replay_sessions` for
preprocessing + Isolation Forest scoring, :class:`SequenceInferenceEngine` for
window-level LSTM scoring, and the on-disk anomaly log — into a single
structured report. The output is consumed by the MCP ``replay_window`` tool
and intended for inference debugging: "what does today's model say about this
window, and how does that compare to what historically fired?"

The job runner is :mod:`tools.replay_window`; this module is pure and
testable (no subprocess, no stdout side-effects).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd

from src.coerce import finite_float
from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN
from src.config import CanaryConfig
from src.models.isolation_forest import (
    LABEL_COLUMN,
    SCORE_COLUMN,
)
from src.models.lstm_autoencoder import MissingLSTMAutoencoderDependencyError
from src.timefmt import parse_iso_utc

#: Per-row score caps so a 30-minute window doesn't return a 100k-row JSON
#: blob. Adjustable from the CLI via ``--max-rows`` / ``--max-windows``.
DEFAULT_MAX_ROWS = 5_000
DEFAULT_MAX_WINDOWS = 5_000
#: Default tolerance for matching replayed and historical events. The live
#: LSTM emits at the *end* timestamp of a window, so a couple seconds of slack
#: is enough for most matches; the comparison is only diagnostic, not exact.
DEFAULT_DIFF_TOLERANCE_SECONDS = 2.0


@dataclass(frozen=True)
class ReplayWindowReport:
    """Structured replay output. Serialised to JSON by :meth:`to_dict`."""

    session_id: str
    window: dict[str, str | None]
    summary: dict[str, int]
    isolation_forest: dict[str, Any]
    lstm_autoencoder: dict[str, Any]
    historical_events: list[dict[str, Any]]
    diff: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "window": self.window,
            "summary": self.summary,
            "isolation_forest": self.isolation_forest,
            "lstm_autoencoder": self.lstm_autoencoder,
            "historical_events": self.historical_events,
            "diff": self.diff,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def replay_window(
    *,
    session_dir: Path,
    config: CanaryConfig,
    start: datetime | None = None,
    end: datetime | None = None,
    if_model_path: Path | None = None,
    lstm_model_path: Path | None = None,
    anomalies_dir: Path | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_windows: int = DEFAULT_MAX_WINDOWS,
    diff_tolerance_seconds: float = DEFAULT_DIFF_TOLERANCE_SECONDS,
) -> ReplayWindowReport:
    """Replay one session window through IF + LSTM and diff against history.

    All model arguments are optional; missing artifacts are reported as
    ``loaded=False`` in the corresponding block rather than failing the
    whole replay. The intent is to see *as much as is recoverable* — useful
    when only one of the two models is currently deployed.
    """
    # Import lazily — scripts/ is not a real package, and this keeps the
    # cost of importing src.analysis.replay_window low for tests.
    from scripts.replay import replay_sessions

    warnings: list[str] = []
    session_id = session_dir.name

    replay_result = replay_sessions(
        [session_dir],
        config,
        model_path=if_model_path if (if_model_path and if_model_path.exists()) else None,
    )
    features = replay_result.frames.features
    scored = replay_result.frames.scored  # may be None if no IF model

    features = _ensure_utc_timestamp(features)
    if scored is not None:
        scored = _ensure_utc_timestamp(scored)
    in_window_features = _filter_window(features, start=start, end=end)
    in_window_scored = _filter_window(scored, start=start, end=end) if scored is not None else None

    if_block = _build_if_block(
        in_window_scored,
        if_model_path=if_model_path,
        max_rows=max_rows,
        warnings=warnings,
    )

    lstm_block = _build_lstm_block(
        in_window_features,
        lstm_model_path=lstm_model_path,
        max_windows=max_windows,
        warnings=warnings,
    )

    historical = _load_historical_events(
        anomalies_dir=anomalies_dir,
        session_id=session_id,
        start=start,
        end=end,
        warnings=warnings,
    )

    diff = _diff_events(
        replayed_if=if_block.get("anomalies", []),
        replayed_lstm=lstm_block.get("anomalies", []),
        historical=historical,
        tolerance_seconds=diff_tolerance_seconds,
    )

    summary = {
        "rows_scored": int(if_block.get("rows_scored", 0)),
        "if_anomalies_replayed": len(if_block.get("anomalies", [])),
        "lstm_windows_scored": int(lstm_block.get("windows_scored", 0)),
        "lstm_anomalies_replayed": len(lstm_block.get("anomalies", [])),
        "historical_events": len(historical),
        "matched": int(diff["matched"]),
        "replayed_only": int(diff["replayed_only"]),
        "historical_only": int(diff["historical_only"]),
    }
    window_payload = {
        "start": _iso(start),
        "end": _iso(end),
        "actual_start": _series_min(in_window_features),
        "actual_end": _series_max(in_window_features),
    }
    return ReplayWindowReport(
        session_id=session_id,
        window=window_payload,
        summary=summary,
        isolation_forest=if_block,
        lstm_autoencoder=lstm_block,
        historical_events=historical,
        diff=diff,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_utc_timestamp(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce the ``timestamp`` column to UTC datetime64 in place-safe fashion."""
    if frame.empty or TIMESTAMP_COLUMN not in frame.columns:
        return frame
    out = frame.copy()
    out[TIMESTAMP_COLUMN] = pd.to_datetime(out[TIMESTAMP_COLUMN], utc=True)
    return out


def _filter_window(
    frame: pd.DataFrame,
    *,
    start: datetime | None,
    end: datetime | None,
) -> pd.DataFrame:
    """Slice ``frame`` to ``[start, end]`` by timestamp. Inclusive bounds."""
    if frame.empty or TIMESTAMP_COLUMN not in frame.columns:
        return frame
    mask = pd.Series(True, index=frame.index)
    if start is not None:
        mask &= frame[TIMESTAMP_COLUMN] >= pd.Timestamp(start)
    if end is not None:
        mask &= frame[TIMESTAMP_COLUMN] <= pd.Timestamp(end)
    return frame.loc[mask].reset_index(drop=True)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat()


def _series_min(frame: pd.DataFrame) -> str | None:
    if frame.empty or TIMESTAMP_COLUMN not in frame.columns:
        return None
    value = frame[TIMESTAMP_COLUMN].min()
    return None if pd.isna(value) else cast(pd.Timestamp, value).isoformat()


def _series_max(frame: pd.DataFrame) -> str | None:
    if frame.empty or TIMESTAMP_COLUMN not in frame.columns:
        return None
    value = frame[TIMESTAMP_COLUMN].max()
    return None if pd.isna(value) else cast(pd.Timestamp, value).isoformat()


def _build_if_block(
    scored: pd.DataFrame | None,
    *,
    if_model_path: Path | None,
    max_rows: int,
    warnings: list[str],
) -> dict[str, Any]:
    """Pack the IF scoring block for the report."""
    if scored is None:
        if if_model_path is not None and not if_model_path.exists():
            warnings.append(f"IF model not found at {if_model_path}; skipped IF scoring")
        return {
            "loaded": False,
            "model_version": None,
            "rows_scored": 0,
            "scores": [],
            "anomalies": [],
        }
    if scored.empty:
        return {
            "loaded": True,
            "model_version": _model_version_from_scored(scored),
            "rows_scored": 0,
            "scores": [],
            "anomalies": [],
        }
    rows = scored.head(max_rows)
    if len(scored) > max_rows:
        warnings.append(f"IF scores truncated to first {max_rows} rows (of {len(scored)})")
    score_records = [
        {
            "timestamp": _ts(row),
            "operating_mode": row.get(MODE_COLUMN),
            "score": finite_float(row.get(SCORE_COLUMN)),
            "label": _int_or_none(row.get(LABEL_COLUMN)),
        }
        for row in cast(list[dict[str, Any]], rows.to_dict(orient="records"))
    ]
    anomalies = [r for r in score_records if r["label"] == -1]
    return {
        "loaded": True,
        "model_version": _model_version_from_scored(scored),
        "rows_scored": int(len(scored)),
        "scores": score_records,
        "anomalies": anomalies,
    }


def _model_version_from_scored(scored: pd.DataFrame) -> str | None:
    """Best-effort: the manifest version isn't on the scored frame, but the
    score column has a stable name so we just return a placeholder. The MCP
    tool can fetch the version separately from ``/status`` if it matters."""
    if scored is None or scored.empty:
        return None
    return None


def _build_lstm_block(
    features: pd.DataFrame,
    *,
    lstm_model_path: Path | None,
    max_windows: int,
    warnings: list[str],
) -> dict[str, Any]:
    """Pack the LSTM scoring block: per-window global + channel errors."""
    if lstm_model_path is None or not lstm_model_path.exists():
        if lstm_model_path is not None:
            warnings.append(f"LSTM model not found at {lstm_model_path}; skipped LSTM scoring")
        return {
            "loaded": False,
            "model_version": None,
            "windows_scored": 0,
            "windows": [],
            "anomalies": [],
        }

    try:
        from src.models.inference import SequenceInferenceEngine, SequenceInferenceResult
        from src.models.lstm_training import load_lstm_autoencoder_model
    except MissingLSTMAutoencoderDependencyError as exc:
        warnings.append(f"LSTM dependency missing: {exc}")
        return {
            "loaded": False,
            "model_version": None,
            "windows_scored": 0,
            "windows": [],
            "anomalies": [],
        }

    try:
        trained = load_lstm_autoencoder_model(lstm_model_path, device="cpu")
    except (FileNotFoundError, ValueError) as exc:
        warnings.append(f"LSTM load failed: {exc}")
        return {
            "loaded": False,
            "model_version": None,
            "windows_scored": 0,
            "windows": [],
            "anomalies": [],
        }

    engine = SequenceInferenceEngine(trained, device="cpu")
    windows: list[dict[str, Any]] = []
    truncated = False
    for row in cast(list[dict[str, Any]], features.to_dict(orient="records")):
        # The buffer needs SESSION/TIMESTAMP plus every feature column; the
        # feature frame already carries them post build_feature_frame.
        result = engine.observe(row)
        if not isinstance(result, SequenceInferenceResult):
            continue
        windows.append(
            {
                "timestamp": result.timestamp,
                "window_start_timestamp": result.window_start_timestamp,
                "window_end_timestamp": result.window_end_timestamp,
                "global_score": result.global_score,
                "global_threshold": result.global_threshold,
                "anomalous_channels": list(result.anomalous_channels),
                "channel_scores": dict(result.channel_scores),
                "channel_thresholds": dict(result.channel_thresholds),
                "is_anomaly": result.is_anomaly,
                "latency_ms": result.latency_ms,
            }
        )
        if len(windows) >= max_windows:
            truncated = True
            break
    if truncated:
        warnings.append(f"LSTM windows truncated at {max_windows}")

    anomalies = [w for w in windows if w["is_anomaly"]]
    return {
        "loaded": True,
        "model_version": trained.manifest.model_version,
        "windows_scored": len(windows),
        "windows": windows,
        "anomalies": anomalies,
    }


def _load_historical_events(
    *,
    anomalies_dir: Path | None,
    session_id: str,
    start: datetime | None,
    end: datetime | None,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Read anomaly events whose session matches and timestamp falls in window."""
    if anomalies_dir is None or not anomalies_dir.exists():
        if anomalies_dir is not None:
            warnings.append(f"anomalies directory not found: {anomalies_dir}")
        return []
    events: list[dict[str, Any]] = []
    for path in sorted(anomalies_dir.glob("anomalies-*.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("session") and record["session"] != session_id:
                continue
            ts = _parse_event_ts(record.get("timestamp"))
            if ts is None:
                continue
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            events.append(record)
    events.sort(key=lambda r: r.get("timestamp") or "")
    return events


def _diff_events(
    *,
    replayed_if: list[dict[str, Any]],
    replayed_lstm: list[dict[str, Any]],
    historical: list[dict[str, Any]],
    tolerance_seconds: float,
) -> dict[str, Any]:
    """Match replayed events to historical by timestamp + source.

    Two replayed sets (IF + LSTM) get a unified diff: each is bucketed by
    source, then matched against historical events of the same source within
    ``tolerance_seconds``. Anything unmatched is reported under
    ``replayed_only`` or ``historical_only``.
    """
    replayed: list[tuple[str, datetime]] = []
    for ev in replayed_if:
        ts = _parse_event_ts(ev.get("timestamp"))
        if ts is not None:
            replayed.append(("isolation_forest", ts))
    for ev in replayed_lstm:
        ts = _parse_event_ts(ev.get("timestamp"))
        if ts is not None:
            replayed.append(("lstm_autoencoder", ts))

    hist_by_source: dict[str, list[datetime]] = {}
    for ev in historical:
        ts = _parse_event_ts(ev.get("timestamp"))
        source = ev.get("source")
        if ts is None or not isinstance(source, str):
            continue
        hist_by_source.setdefault(source, []).append(ts)

    matched = 0
    replayed_only = 0
    used_indices: dict[str, set[int]] = {k: set() for k in hist_by_source}
    for source, ts in replayed:
        candidates = hist_by_source.get(source, [])
        match_index = _nearest_match(
            ts, candidates, used=used_indices.get(source, set()), tolerance=tolerance_seconds
        )
        if match_index is None:
            replayed_only += 1
            continue
        matched += 1
        used_indices.setdefault(source, set()).add(match_index)

    historical_only = sum(
        len(times) - len(used_indices.get(src, set())) for src, times in hist_by_source.items()
    )
    return {
        "matched": matched,
        "replayed_only": replayed_only,
        "historical_only": historical_only,
        "tolerance_seconds": tolerance_seconds,
    }


def _nearest_match(
    target: datetime,
    candidates: list[datetime],
    *,
    used: set[int],
    tolerance: float,
) -> int | None:
    """Return the index of the closest unused candidate within tolerance."""
    best_index: int | None = None
    best_delta = tolerance
    for index, candidate in enumerate(candidates):
        if index in used:
            continue
        delta = abs((candidate - target).total_seconds())
        if delta <= best_delta:
            best_delta = delta
            best_index = index
    return best_index


def _parse_event_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return parse_iso_utc(raw)
    except ValueError:
        return None


def _ts(row: dict[str, Any]) -> str | None:
    value = row.get(TIMESTAMP_COLUMN)
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value)


def _int_or_none(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Re-export for convenience
__all__ = [
    "ReplayWindowReport",
    "replay_window",
    "DEFAULT_MAX_ROWS",
    "DEFAULT_MAX_WINDOWS",
    "DEFAULT_DIFF_TOLERANCE_SECONDS",
    "SESSION_COLUMN",  # commonly needed alongside this module
]
