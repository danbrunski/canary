"""Streaming feature pipeline for live inference (Milestone B).

The offline path (``build_feature_frame``) is a batch transform over a
recorded session's Parquet. Live inference needs the same outputs row-by-row
as records arrive from the collectors. ``LiveFeatureSource`` is the
incremental version:

1. **observe(record)** — update the "current value" dict from one
   ``TelemetryRecord``. OBD records map directly (``obd:RPM`` ← value).
   CAN records get decoded via ``bindings.yaml`` (same byte-extraction
   path as ``decode_can_rows``), producing ``decoded:<SIGNAL>`` entries.
   This is the only side-effect — emitting a wide row is a separate call.

2. **tick(now)** — emit one wide row (current_values as of ``now``),
   append to the rolling buffer, segment the row's operating mode,
   append deviation columns (if behavior models loaded), recompute
   ``compute_features`` over the buffered window, and return the *last*
   row of the result. ``None`` until enough history has accumulated.

3. **feature_columns** — the columns a downstream inference engine will
   see. Stable across ticks once observation has begun.

The ring buffer holds ``2 × window_seconds * resample_hz`` rows so
``compute_features`` always has a full window even right after startup.
At 1 Hz with 30 s windows that's 60 rows — trivial to recompute each
tick. Higher resample rates scale linearly; revisit if it ever becomes
the bottleneck (NFR-01 budget is <50% Pi 5 CPU).

Decode policy intentionally matches the offline path:
``decode_can_rows`` emits the *raw integer* (not scaled), because the
scaler normalizes anyway and many bindings have no pinned scale yet.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from src.config import CanaryConfig
from src.decode.behavior_decoder import append_deviation_columns
from src.decode.behavior_models import BehaviorModels
from src.decode.bindings import Bindings, Signal
from src.preprocessing.feature_pipeline import segment_or_constant
from src.preprocessing.features import FeatureConfig, compute_features
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)


@dataclass
class LiveFeatureSource:
    """Incremental feature pipeline. One per inference engine, or shared.

    ``session`` is the label attached to every emitted row — analogous to
    the offline pipeline's session-dir name. The session-keyed Parquet
    writer (1.11) rotates separately; this is a logical label for the
    feature stage, used only by the segmenter / feature-window grouping
    so windows reset on a session change.
    """

    config: CanaryConfig
    bindings: Bindings | None = None
    behavior_models: BehaviorModels | None = None
    dbc_database: Any | None = None
    session: str = "live"

    _current_values: dict[str, float] = field(default_factory=dict)
    _buffer: deque[dict[str, Any]] = field(init=False)
    _bindings_by_id: dict[int, list[Signal]] = field(init=False)
    _dbc_message_by_id: dict[int, Any] = field(init=False)
    _feature_config: FeatureConfig = field(init=False)
    _max_rows: int = field(init=False)

    def __post_init__(self) -> None:
        hz = max(self.config.preprocessing.resample_hz, 1.0)
        window_s = self.config.preprocessing.rolling_window_seconds
        # Keep a slack factor — pandas' time-window rolling can use anything
        # in the buffer, but we want enough rows to actually fill a window
        # plus a margin so the ROC shift doesn't fall off the end.
        self._max_rows = max(32, int(window_s * hz * 2))
        self._buffer = deque(maxlen=self._max_rows)
        self._feature_config = FeatureConfig(window_seconds=window_s)
        # Pre-index bindings by CAN ID so decode is O(signals per ID), not
        # O(all signals) per CAN frame.
        self._bindings_by_id = {}
        if self.bindings is not None:
            for sig in self.bindings.signals:
                self._bindings_by_id.setdefault(sig.can_id, []).append(sig)
        # Same idea for the DBC database: frame_id → message so a per-frame
        # decode is an O(1) lookup, not a scan of every message.
        self._dbc_message_by_id = {}
        if self.dbc_database is not None:
            for msg in self.dbc_database.messages:
                self._dbc_message_by_id[msg.frame_id] = msg

    # ------------------------------------------------------------------
    # observe — update the current-value dict
    # ------------------------------------------------------------------

    def observe(self, record: TelemetryRecord) -> None:
        """Apply one ``TelemetryRecord`` to the running current-values dict.

        Side-effects only. Emitting a wide row is a separate tick.
        """
        if record.source == "obd":
            value = _to_float(record.value)
            if value is not None:
                self._current_values[f"obd:{record.channel}"] = value
            return
        if record.source == "can":
            self._apply_can_decode(record)
            return
        # Anything else (e.g. a future "decoded" source pushed directly) gets
        # stored as-is; the pipeline will surface it as a new column.
        value = _to_float(record.value)
        if value is not None:
            self._current_values[f"{record.source}:{record.channel}"] = value

    def decode_for_telemetry(self, record: TelemetryRecord) -> tuple[TelemetryRecord, ...]:
        """Return decoded signal rows for a raw CAN record.

        This is the dashboard-facing counterpart to ``observe``'s CAN decode:
        inference wants the values in ``_current_values``; the UI also needs
        named ``decoded:<SIGNAL>`` rows so it can render gauges instead of raw
        arbitration IDs.
        """
        if record.source != "can" or record.payload is None:
            return ()
        can_id = _parse_can_id(record.channel)
        if can_id is None:
            return ()
        signals = self._bindings_by_id.get(can_id)
        if not signals:
            return ()
        payload = bytes(record.payload)
        decoded: list[TelemetryRecord] = []
        for sig in signals:
            raw = _decode_signal_raw(sig, payload)
            if raw is None:
                continue
            decoded.append(
                TelemetryRecord.decoded(
                    record.timestamp,
                    sig.name,
                    raw,
                    sig.unit if sig.unit != "unknown" else "",
                    metadata={
                        "can_id": can_id,
                        "confidence": sig.confidence,
                    },
                )
            )
        return tuple(decoded)

    def _apply_can_decode(self, record: TelemetryRecord) -> None:
        for decoded in self.decode_for_telemetry(record):
            # Raw integer matches decode_can_rows' policy — the scaler
            # normalizes downstream and most bindings have no pinned scale.
            value = _to_float(decoded.value)
            if value is not None:
                self._current_values[f"decoded:{decoded.channel}"] = value
        self._apply_dbc_decode(record)

    def _apply_dbc_decode(self, record: TelemetryRecord) -> None:
        """Mirror ``decode_can_rows_dbc``: emit ``dbc:<SIGNAL>`` current values.

        The offline trainer decodes CAN through the DBC database too, so the
        model's feature columns include ``dbc:*``. Without this the live row
        is missing every ``dbc:*`` column and inference fails the schema
        check. Same policy as the offline path: cantools ``decode`` with
        ``decode_choices=False``, numeric signals only (named/enum choices
        skip the float() cast).
        """
        if self.dbc_database is None or record.payload is None:
            return
        can_id = _parse_can_id(record.channel)
        if can_id is None:
            return
        message = self._dbc_message_by_id.get(can_id)
        if message is None:
            return
        try:
            decoded = message.decode(bytes(record.payload), decode_choices=False)
        except Exception:  # noqa: BLE001 — cantools raises a family of errors
            return
        for signal_name, value in decoded.items():
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue  # named choice / enum signals — skip in numeric path
            self._current_values[f"dbc:{signal_name}"] = numeric

    # ------------------------------------------------------------------
    # tick — emit one feature row
    # ------------------------------------------------------------------

    def tick(self, now: datetime) -> pd.DataFrame | None:
        """Emit one feature row at ``now``.

        Returns a one-row ``DataFrame`` (so callers can index columns by
        name) or ``None`` when there's nothing meaningful to emit
        (no observations yet). The returned frame's index is reset; row
        index 0 is the only row.
        """
        if not self._current_values:
            return None
        # Snapshot — copy so a later observation can't mutate this tick's row.
        snapshot = {
            "timestamp": pd.Timestamp(now, tz="UTC") if now.tzinfo is None else pd.Timestamp(now),
            "session": self.session,
            **self._current_values,
        }
        self._buffer.append(snapshot)

        wide = pd.DataFrame(list(self._buffer))
        wide["timestamp"] = pd.to_datetime(wide["timestamp"], utc=True)

        # The segmenter looks at the operating-mode signal columns; if
        # they're not present yet it falls back to a constant "all" mode,
        # matching the offline pipeline's behavior on no-OBD captures.
        wide = segment_or_constant(wide, self.config)

        if self.behavior_models is not None and self.behavior_models.models:
            wide = append_deviation_columns(wide, self.behavior_models)

        featured = compute_features(wide, config=self._feature_config)
        if featured.empty:
            return None
        return featured.iloc[[-1]].reset_index(drop=True)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    @property
    def known_channels(self) -> tuple[str, ...]:
        """Channels we've observed at least once. Stable enough for telemetry."""
        return tuple(sorted(self._current_values))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_can_id(channel: str) -> int | None:
    """Parse a CAN channel hex string (``"0x551"`` / ``"551"``) into an int."""
    try:
        return int(channel, 16)
    except (ValueError, TypeError):
        return None


def _decode_signal_raw(signal: Signal, payload: bytes) -> int | None:
    end = signal.byte_start + signal.byte_length
    if len(payload) < end:
        return None
    return int.from_bytes(payload[signal.byte_start : end], byteorder=signal.byte_order)


__all__ = ["LiveFeatureSource"]
