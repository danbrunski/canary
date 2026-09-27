"""Runtime inference helpers for trained anomaly models.

The LSTM path is intentionally small and synchronous: callers feed already
preprocessed feature rows in timestamp order, the buffer maintains the latest
fixed-length sequence, and the engine emits one result per full window.
"""

from __future__ import annotations

import importlib
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pandas as pd

from src.columns import GAP_SUFFIX, SESSION_COLUMN, TIMESTAMP_COLUMN, base_channel
from src.models.sequence_calibration import SequenceThresholdCalibration
from src.models.sequence_dataset import (
    MASK_ARRIVED_SAMPLE,
    MASK_FRESH_WITHIN_MAX_GAP,
    WINDOW_MAX_GAP_SECONDS,
    arrival_column_for,
    observation_mask_from_schema,
)
from src.models.sequence_protocol import TrainedSequenceModel
from src.models.sequence_scoring import SequenceScoringRule, score_exclusions
from src.timefmt import format_timestamp

log = logging.getLogger(__name__)

LSTM_SCORE_COLUMN = "lstm_reconstruction_error"
LSTM_THRESHOLD_COLUMN = "lstm_reconstruction_threshold"
LSTM_ANOMALY_COLUMN = "lstm_anomaly"

#: Phase 7.2a — how many top channels (by ratio) the LSTM result carries
#: detail for. Keeps the ``AnomalyEvent.detail`` payload bounded; ~90 trained
#: channels × sequence_length 30 would flood the WebSocket otherwise.
LSTM_TOP_CHANNELS = 5


@dataclass(frozen=True)
class SequenceInferenceResult:
    """One sequence-detector inference result for the current sliding window."""

    model_version: str
    timestamp: str
    session: str
    window_start_timestamp: str
    window_end_timestamp: str
    global_score: float
    global_threshold: float
    channel_scores: Mapping[str, float]
    channel_thresholds: Mapping[str, float]
    anomalous_channels: tuple[str, ...]
    is_anomaly: bool
    latency_ms: float
    #: Phase 7.2a evidence (compact, bounded by :data:`LSTM_TOP_CHANNELS`).
    #: ``top_channels`` are the channels with the largest ratio of recon
    #: error to threshold; ``reconstruction_series`` is the per-timestep
    #: MAE for each of those channels; ``reconstruction_overlays`` carries
    #: ``actual`` / ``reconstructed`` arrays so the dashboard can render
    #: the divergence directly. Empty when the model hasn't produced
    #: a window yet (back-compat default for stubs in tests).
    top_channels: tuple[str, ...] = ()
    reconstruction_series: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    reconstruction_overlays: Mapping[str, Mapping[str, tuple[float, ...]]] = field(
        default_factory=dict
    )
    family_scores: Mapping[str, float] = field(default_factory=dict)
    family_threshold: float | None = None
    #: A01.7 — how much wall time the window's rows cover, and its longest
    #: step between consecutive rows. The model assumes one tick per row; a
    #: tick dropped under load leaves a longer step inside the window.
    #: ``None`` when the rows carry no timestamps.
    window_rows: int | None = None
    window_span_seconds: float | None = None
    window_max_step_seconds: float | None = None
    #: A03.7 — what ``top_channels`` ranks over: ``"all_channels"`` (driving
    #: conditions included) or ``"engine_response_targets"`` (an artifact whose
    #: context inputs are kept out of the score, A03.2).
    ranking_scope: str = "all_channels"
    #: A06.2 — the novelty screen: the window against the artifact's recorded
    #: training envelope (:meth:`TrainingEnvelope.check`). ``None`` when the
    #: artifact recorded no envelope — "not recorded", never "inside".
    envelope: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "timestamp": self.timestamp,
            "session": self.session,
            "window_start_timestamp": self.window_start_timestamp,
            "window_end_timestamp": self.window_end_timestamp,
            "global_score": self.global_score,
            "global_threshold": self.global_threshold,
            "channel_scores": dict(self.channel_scores),
            "channel_thresholds": dict(self.channel_thresholds),
            "anomalous_channels": list(self.anomalous_channels),
            "is_anomaly": self.is_anomaly,
            "latency_ms": self.latency_ms,
            "top_channels": list(self.top_channels),
            "reconstruction_series": {k: list(v) for k, v in self.reconstruction_series.items()},
            "family_scores": dict(self.family_scores),
            "family_threshold": self.family_threshold,
            "window_rows": self.window_rows,
            "window_span_seconds": self.window_span_seconds,
            "window_max_step_seconds": self.window_max_step_seconds,
            "ranking_scope": self.ranking_scope,
            "envelope": None if self.envelope is None else dict(self.envelope),
            "reconstruction_overlays": {
                k: {sub: list(seq) for sub, seq in overlay.items()}
                for k, overlay in self.reconstruction_overlays.items()
            },
        }


#: Manifest ``schema`` key recording the ``suppress_channels`` training ran
#: with (A01.5): ``{"configured": [...], "excluded_columns": [...]}``.
#: Training drops suppressed columns from the schema, so serving with a
#: different list either scores without columns the thresholds were
#: calibrated on, or has nothing to suppress. Absent on artifacts trained
#: before it was recorded.
TRAINING_SUPPRESS_SCHEMA_KEY = "suppress_channels"


def training_suppress_record(
    configured: Sequence[str], excluded_columns: Sequence[str]
) -> dict[str, list[str]]:
    """The schema entry a trainer writes under :data:`TRAINING_SUPPRESS_SCHEMA_KEY`."""
    return {
        "configured": [str(name) for name in configured],
        "excluded_columns": [str(column) for column in excluded_columns],
    }


@dataclass(frozen=True)
class SuppressionComparison:
    """Training-time against serve-time ``suppress_channels`` (A01.5).

    ``trained_columns_suppressed_at_serve`` is the difference that matters:
    columns the encoder was trained and calibrated with that serving keeps out
    of the score. It is known even for an artifact that did not record its
    list, because training drops every column it suppresses — a suppressed
    column still in the schema was not suppressed when it was trained.
    ``training`` is ``None`` when the manifest predates the record, so
    ``added_at_serve`` / ``dropped_at_serve`` are unknown rather than empty.
    """

    model_version: str
    training: tuple[str, ...] | None
    serving: tuple[str, ...]
    trained_columns_suppressed_at_serve: tuple[str, ...]

    @property
    def added_at_serve(self) -> tuple[str, ...] | None:
        if self.training is None:
            return None
        return tuple(name for name in self.serving if name not in set(self.training))

    @property
    def dropped_at_serve(self) -> tuple[str, ...] | None:
        if self.training is None:
            return None
        return tuple(name for name in self.training if name not in set(self.serving))

    @property
    def differs(self) -> bool:
        return bool(self.trained_columns_suppressed_at_serve) or (
            self.training is not None and set(self.training) != set(self.serving)
        )

    def to_dict(self) -> dict[str, Any]:
        added = self.added_at_serve
        dropped = self.dropped_at_serve
        return {
            "model_version": self.model_version,
            "recorded_at_training": self.training is not None,
            "training": None if self.training is None else list(self.training),
            "serving": list(self.serving),
            "differs": self.differs,
            "added_at_serve": None if added is None else list(added),
            "dropped_at_serve": None if dropped is None else list(dropped),
            "trained_columns_suppressed_at_serve": list(self.trained_columns_suppressed_at_serve),
        }


def compare_suppression(
    trained: TrainedSequenceModel,
    *,
    serving: Sequence[str],
    suppressed_columns: Sequence[str],
) -> SuppressionComparison:
    """Compare the manifest's training-time suppress list with ``serving``.

    ``suppressed_columns`` is ``serving`` already expanded against the
    trained schema (what the engine keeps out of the score).
    """
    record = trained.manifest.schema.get(TRAINING_SUPPRESS_SCHEMA_KEY)
    training: tuple[str, ...] | None = None
    if isinstance(record, Mapping):
        configured = record.get("configured")
        if isinstance(configured, Sequence) and not isinstance(configured, str):
            training = tuple(str(name) for name in configured)
    return SuppressionComparison(
        model_version=str(trained.manifest.model_version),
        training=training,
        serving=tuple(str(name) for name in serving),
        trained_columns_suppressed_at_serve=tuple(suppressed_columns),
    )


#: A06.2 — the novelty screen's verdicts. ``outside`` and ``mode_not_trained``
#: are both "outside the learned envelope"; ``not_compared`` means no recorded
#: quantity could be compared in this window (no fresh value, or never measured
#: in training) — an empty result that says which kind of empty it is, never
#: read as ``inside``.
ENVELOPE_INSIDE = "inside"
ENVELOPE_OUTSIDE = "outside"
ENVELOPE_MODE_NOT_TRAINED = "mode_not_trained"
ENVELOPE_NOT_COMPARED = "not_compared"
#: Per-quantity verdicts beside ``inside`` / ``outside``.
ENVELOPE_QUANTITY_NOT_TRAINED = "not_trained"
ENVELOPE_QUANTITY_NO_DATA = "no_data"
#: Units for the envelope sentence, keyed by the quantity names A03.8 records
#: (``sequence_envelope.ENVELOPE_QUANTITIES``). An unknown name prints bare.
ENVELOPE_UNITS: dict[str, str] = {"baro_kpa": "kPa", "iat_c": "°C", "speed_kph": "km/h"}


@dataclass(frozen=True)
class TrainingEnvelope:
    """The per-mode ranges a sequence artifact trained on (A03.8), served (A06.2).

    Built once from the manifest's ``training_envelope``; :meth:`check` is a
    few comparisons per scored window. The range is the recorded ``[min,
    max]``, not p05–p95: "outside" means no training row was there at all.
    """

    #: quantity -> column, e.g. ``baro_kpa -> obd:BAROMETRIC_PRESSURE``.
    channels: Mapping[str, str]
    #: mode -> quantity -> (trained min, trained max). A quantity the mode
    #: never measured is absent (``unmeasured`` in the record).
    ranges: Mapping[str, Mapping[str, tuple[float, float]]]
    #: mode -> engine-hours of training data in it.
    hours: Mapping[str, float]

    @classmethod
    def from_record(cls, record: Mapping[str, Any] | None) -> TrainingEnvelope | None:
        """``None`` for an artifact that recorded no envelope (pre-A03.8)."""
        if not isinstance(record, Mapping):
            return None
        modes = record.get("modes")
        if not isinstance(modes, Mapping):
            return None
        channels = {
            str(name): str(column) for name, column in (record.get("quantities") or {}).items()
        }
        ranges: dict[str, dict[str, tuple[float, float]]] = {}
        hours: dict[str, float] = {}
        for mode, summary in modes.items():
            per_quantity: dict[str, tuple[float, float]] = {}
            for name, stats in (summary.get("quantities") or {}).items():
                if not stats or not int(stats.get("rows", 0)):
                    continue
                per_quantity[str(name)] = (float(stats["min"]), float(stats["max"]))
                channels.setdefault(str(name), str(stats.get("channel", name)))
            ranges[str(mode)] = per_quantity
            hours[str(mode)] = float(summary.get("hours", 0.0) or 0.0)
        return cls(channels=channels, ranges=ranges, hours=hours)

    @property
    def columns(self) -> tuple[str, ...]:
        """The row columns the window must carry for :meth:`check`."""
        return tuple(self.channels.values())

    def check(
        self,
        mode: str | None,
        observed: Mapping[str, tuple[float, float] | None],
    ) -> dict[str, Any]:
        """The window against the envelope of ``mode``.

        ``observed`` is quantity -> the window's (min, max) over its finite
        values (forward-filled ones included: baro, IAT and speed move slowly
        enough that a held value is still the condition), or ``None`` when the
        window held none.
        """
        quantities: dict[str, dict[str, Any]] = {}
        trained = self.ranges.get(mode) if mode is not None else None
        for name, column in self.channels.items():
            window = observed.get(name)
            entry: dict[str, Any] = {
                "channel": column,
                "unit": ENVELOPE_UNITS.get(name, ""),
                "window_min": None if window is None else window[0],
                "window_max": None if window is None else window[1],
                "trained_min": None,
                "trained_max": None,
            }
            span = None if trained is None else trained.get(name)
            if span is not None:
                entry["trained_min"], entry["trained_max"] = span
            if trained is None:
                entry["status"] = None
            elif span is None:
                entry["status"] = ENVELOPE_QUANTITY_NOT_TRAINED
            elif window is None:
                entry["status"] = ENVELOPE_QUANTITY_NO_DATA
            elif window[0] < span[0] or window[1] > span[1]:
                entry["status"] = ENVELOPE_OUTSIDE
            else:
                entry["status"] = ENVELOPE_INSIDE
            quantities[name] = entry
        outside_on = [n for n, e in quantities.items() if e["status"] == ENVELOPE_OUTSIDE]
        reason: str | None = None
        if mode is None:
            status = ENVELOPE_NOT_COMPARED
            reason = "the window carries no operating mode"
        elif trained is None:
            status = ENVELOPE_MODE_NOT_TRAINED
        elif outside_on:
            status = ENVELOPE_OUTSIDE
        elif any(e["status"] == ENVELOPE_INSIDE for e in quantities.values()):
            status = ENVELOPE_INSIDE
        else:
            status = ENVELOPE_NOT_COMPARED
            reason = "no recorded quantity had a fresh value in the window and a trained range"
        return {
            "mode": mode,
            "status": status,
            "outside": status in (ENVELOPE_OUTSIDE, ENVELOPE_MODE_NOT_TRAINED),
            "outside_on": outside_on,
            "trained_hours": None if mode is None else self.hours.get(mode),
            "reason": reason,
            "quantities": quantities,
        }


class SequenceWindowBuffer:
    """Fixed-length buffer for sequence-detector feature rows (value + mask + Δt).

    Each appended row is split into a value vector, an observed-mask vector
    (what ``1`` means is the artifact's ``observation_mask`` rule, A03.1:
    ``<channel>__arrived`` — a sample arrived this tick — for artifacts trained
    on it, ``¬<channel>__gap`` for the legacy ones), and a Δt vector (seconds
    since each channel was last observed). Δt is accumulated **inside the buffer** exactly as
    ``sequence_dataset`` accumulates it offline — reset to 0 on an observed tick,
    otherwise incremented by the inter-tick second delta — so train and serve
    feed the encoder identical Δt for the same observation history.
    """

    def __init__(
        self,
        *,
        feature_columns: tuple[str, ...],
        sequence_length: int,
        timestamp_column: str = TIMESTAMP_COLUMN,
        session_column: str = SESSION_COLUMN,
        mode_column: str = "operating_mode",
        dtype: str = "float32",
        max_gap_seconds: float = WINDOW_MAX_GAP_SECONDS,
        observation_mask: str = MASK_FRESH_WITHIN_MAX_GAP,
        freshness_fallback_columns: frozenset[str] = frozenset(),
        condition_columns: tuple[str, ...] = (),
    ) -> None:
        if sequence_length <= 0:
            raise ValueError("sequence_length must be positive")
        if not feature_columns:
            raise ValueError("feature_columns must not be empty")
        self.feature_columns = feature_columns
        self.sequence_length = sequence_length
        self.timestamp_column = timestamp_column
        self.session_column = session_column
        self.mode_column = mode_column
        self.dtype = dtype
        #: A window never spans a hole longer than this (A01.3); the offline
        #: builder splits on the same value.
        self.max_gap_seconds = float(max_gap_seconds)
        self._gap_columns = tuple(
            f"{base_channel(column)}{GAP_SUFFIX}" for column in feature_columns
        )
        #: A03.1 — the mask the artifact was trained on. Under the arrival rule
        #: a column reads its ``__arrived`` flag (absent ⇒ not arrived), except
        #: the columns training itself had to mask by freshness.
        self.observation_mask = observation_mask
        self._arrival_columns: tuple[str | None, ...] = tuple(
            None
            if observation_mask != MASK_ARRIVED_SAMPLE or column in freshness_fallback_columns
            else arrival_column_for(column)
            for column in feature_columns
        )
        #: A06.2 — driving-condition columns kept beside the window (raw, in
        #: the row's own units) for the novelty screen's envelope check. They
        #: need not be model inputs; absent or non-finite reads as no value.
        self.condition_columns = tuple(condition_columns)
        self._conditions: deque[tuple[float, ...]] = deque(maxlen=sequence_length)
        self._values: deque[np.ndarray[Any, np.dtype[np.float32]]] = deque(maxlen=sequence_length)
        self._masks: deque[np.ndarray[Any, np.dtype[np.float32]]] = deque(maxlen=sequence_length)
        self._deltas: deque[np.ndarray[Any, np.dtype[np.float32]]] = deque(maxlen=sequence_length)
        self._metadata: deque[tuple[str, str]] = deque(maxlen=sequence_length)
        #: Parsed row timestamps, for the window's wall span (A01.7).
        self._stamps: deque[pd.Timestamp | None] = deque(maxlen=sequence_length)
        self._session: str | None = None
        #: Running per-channel Δt (seconds since last observed) carried across
        #: ticks so it survives the deque's bounded length, mirroring the
        #: dataset's per-session cumulative Δt.
        #:
        #: 9.3 — cold-start note: this accumulator (and ``_last_timestamp``
        #: below) always starts at zero, in-process. A ``SequenceWindowBuffer``
        #: is rebuilt from scratch on every daemon (re)start (see
        #: ``_build_sequence_engine`` in ``src/daemon/inference.py``), with no
        #: persisted record of each channel's true last-observed time from
        #: before the restart. A channel that was already stale going into the
        #: restart therefore reports Δt=0 (not its real elapsed staleness) on
        #: the first post-restart tick, and its Δt understates the truth until
        #: that channel is observed fresh again — at which point it resets to
        #: 0 correctly and accumulates truthfully from there. The distortion
        #: is self-bounding (capped by that channel's own poll cadence, not
        #: unbounded) and only affects the GRU-D decay gate on the first
        #: window(s) after a restart. Given that plus this being a read-only,
        #: not-safety-critical research daemon (no actuation depends on this
        #: score), we accept it rather than adding cross-restart persistence
        #: machinery for a bounded, self-correcting cold-start blip.
        self._delta_accumulator: np.ndarray[Any, np.dtype[np.float64]] = np.zeros(
            len(feature_columns), dtype=np.float64
        )
        self._last_timestamp: pd.Timestamp | None = None
        #: Phase 7.5 — the most recent appended row's operating mode, used by
        #: ``SequenceInferenceEngine.observe`` to pick the per-mode threshold.
        self._last_mode: str | None = None

    def append(self, row: Mapping[str, Any] | pd.Series) -> bool:
        """Append a feature row and return whether a full window is ready.

        A feature that is absent or non-finite this tick (a channel still
        warming up, or a whole sensor family missing on this drive — e.g. no GPS
        fix) is treated as **unobserved**: its cell is masked and imputed, never
        a reason to drop the row. This mirrors the offline dataset, which masks
        all-NaN / leading-NaN cells rather than discarding them, so the model —
        trained on GPS-less sessions exactly this way — keeps scoring on the
        channels it *does* have. A fully unobserved tick (no finite feature at
        all) resets the buffer, as does a session change, and so does a row
        more than ``max_gap_seconds`` after the previous one or earlier than it.
        The live row's session label never changes (it is always ``"live"``),
        so the time gap is what keeps a window from straddling a stop, a stale
        stretch or a replay (A01.3).

        The mask is ``¬<channel>__gap`` AND value-is-finite; Δt is accumulated
        from the mask and the inter-tick interval.
        """
        session = str(row.get(self.session_column, ""))
        timestamp_str = _format_timestamp(row.get(self.timestamp_column))
        timestamp = _coerce_timestamp(row.get(self.timestamp_column))
        if self._session is not None and session != self._session:
            self.clear()
        if timestamp is not None and self._last_timestamp is not None:
            step = (timestamp - self._last_timestamp).total_seconds()
            if step < 0 or step > self.max_gap_seconds:
                self.clear()
        self._session = session

        values = np.array(
            [_coerce_feature_value(row.get(column)) for column in self.feature_columns],
            dtype=np.float64,
        )
        finite = np.isfinite(values)
        if not finite.any():
            # No signal at all this tick — treat it as a gap and restart the
            # window rather than score an entirely masked sequence.
            self.clear()
            return False

        mask = self._row_mask(row)
        # An absent / non-finite channel is unobserved regardless of any gap
        # flag; the value is imputed downstream (0 in z-scored space) and the
        # mask keeps it out of the reconstruction error.
        mask[~finite] = 0.0
        delta = self._advance_delta(mask, timestamp)

        self._values.append(values.astype(self.dtype))
        self._masks.append(mask)
        self._deltas.append(delta.astype(self.dtype))
        self._metadata.append((timestamp_str, session))
        self._stamps.append(timestamp)
        if self.condition_columns:
            self._conditions.append(
                tuple(_coerce_feature_value(row.get(column)) for column in self.condition_columns)
            )
        raw_mode = row.get(self.mode_column)
        self._last_mode = raw_mode if isinstance(raw_mode, str) else None
        return self.ready

    def _row_mask(
        self, row: Mapping[str, Any] | pd.Series
    ) -> np.ndarray[Any, np.dtype[np.float32]]:
        """Per-channel observed mask under the artifact's rule.

        Arrival rule: ``<channel>__arrived`` (absent or NaN ⇒ 0 — nothing is
        known to have arrived). Freshness rule, and arrival-rule fallback
        columns: ``¬<channel>__gap`` (absent ⇒ observed).
        """
        mask = np.ones(len(self.feature_columns), dtype=self.dtype)
        for index, gap_column in enumerate(self._gap_columns):
            arrival = self._arrival_columns[index]
            if arrival is not None:
                raw = row[arrival] if arrival in row else None
                arrived = raw is not None and not _is_nan(raw) and bool(raw)
                mask[index] = 1.0 if arrived else 0.0
                continue
            if gap_column in row:
                raw = row[gap_column]
                stale = bool(raw) if raw is not None and not _is_nan(raw) else True
                mask[index] = 0.0 if stale else 1.0
        return mask

    def _advance_delta(
        self,
        mask: np.ndarray[Any, np.dtype[np.float32]],
        timestamp: pd.Timestamp | None,
    ) -> np.ndarray[Any, np.dtype[np.float64]]:
        """Update the running Δt accumulator and return this tick's Δt vector."""
        step_seconds = 0.0
        if timestamp is not None and self._last_timestamp is not None:
            step_seconds = max((timestamp - self._last_timestamp).total_seconds(), 0.0)
        observed = mask > 0.5
        # Accumulate first, then reset observed channels to 0 — an observed
        # tick has Δt 0, a stale tick carries the prior age plus this step.
        self._delta_accumulator = self._delta_accumulator + step_seconds
        self._delta_accumulator[observed] = 0.0
        if timestamp is not None:
            self._last_timestamp = timestamp
        return self._delta_accumulator.copy()

    @property
    def ready(self) -> bool:
        return len(self._values) == self.sequence_length

    def clear(self) -> None:
        self._values.clear()
        self._masks.clear()
        self._deltas.clear()
        self._metadata.clear()
        self._stamps.clear()
        self._conditions.clear()
        self._session = None
        self._last_mode = None
        self._delta_accumulator = np.zeros(len(self.feature_columns), dtype=np.float64)
        self._last_timestamp = None

    @property
    def last_mode(self) -> str | None:
        """Operating mode of the most recently appended row (7.5)."""
        return self._last_mode

    def window_array(self) -> np.ndarray[Any, np.dtype[np.float32]]:
        if not self.ready:
            raise ValueError("sequence buffer is not ready")
        return np.stack(tuple(self._values), axis=0)

    def mask_array(self) -> np.ndarray[Any, np.dtype[np.float32]]:
        if not self.ready:
            raise ValueError("sequence buffer is not ready")
        return np.stack(tuple(self._masks), axis=0)

    def delta_array(self) -> np.ndarray[Any, np.dtype[np.float32]]:
        if not self.ready:
            raise ValueError("sequence buffer is not ready")
        return np.stack(tuple(self._deltas), axis=0)

    def span(self) -> tuple[float | None, float | None]:
        """Wall seconds from the first row to the last, and the longest step
        between consecutive rows (A01.7). ``(None, None)`` when any row lacks
        a timestamp."""
        stamps = tuple(self._stamps)
        if len(stamps) < 2 or any(stamp is None for stamp in stamps):
            return None, None
        steps = [
            (later - earlier).total_seconds()  # type: ignore[operator]
            for earlier, later in zip(stamps, stamps[1:], strict=False)
        ]
        return sum(steps), max(steps)

    def condition_ranges(self) -> dict[str, tuple[float, float] | None]:
        """Per condition column, the window's (min, max) over its finite
        values; ``None`` for a column the window never held a value for."""
        out: dict[str, tuple[float, float] | None] = {}
        for index, column in enumerate(self.condition_columns):
            values = [row[index] for row in self._conditions if math.isfinite(row[index])]
            out[column] = (min(values), max(values)) if values else None
        return out

    def metadata(self) -> tuple[str, str, str]:
        """Return start timestamp, end timestamp, and current session."""
        if not self.ready:
            raise ValueError("sequence buffer is not ready")
        start_timestamp, session = self._metadata[0]
        end_timestamp, _session = self._metadata[-1]
        return start_timestamp, end_timestamp, session


class SequenceInferenceEngine:
    """Streaming sequence-autoencoder inference over model-ready feature rows."""

    def __init__(
        self,
        trained: TrainedSequenceModel,
        *,
        device: str = "cpu",
        dtype: str = "float32",
        time_fn: Callable[[], float] = time.perf_counter,
        suppress_channels: Sequence[str] = (),
    ) -> None:
        if trained.manifest.thresholds is None:
            raise ValueError("trained LSTM manifest has no calibrated thresholds")
        self.trained = trained
        self.device = device
        self.dtype = dtype
        self.time_fn = time_fn
        self.calibration = SequenceThresholdCalibration.from_dict(trained.manifest.thresholds)
        self.feature_columns = trained.feature_columns
        self.sequence_length = int(trained.manifest.schema["sequence_length"])
        # A03.1: feed the mask the artifact was trained on — the 2026-09-20
        # artifact has no record and keeps ``¬__gap``; a retrained one reads
        # ``__arrived``.
        mask_rule, fallback_columns = observation_mask_from_schema(trained.manifest.schema)
        self.observation_mask = mask_rule
        #: A06.2 — the novelty screen's reference, ``None`` for an artifact
        #: that recorded no envelope (every result then says so).
        self.envelope: TrainingEnvelope | None = TrainingEnvelope.from_record(
            getattr(trained.manifest, "training_envelope", None)
        )
        #: Row columns the envelope check reads, model inputs or not; the
        #: daemon keeps them on the row it hands to :meth:`observe`.
        self.condition_columns: tuple[str, ...] = (
            self.envelope.columns if self.envelope is not None else ()
        )
        self.buffer = SequenceWindowBuffer(
            feature_columns=self.feature_columns,
            sequence_length=self.sequence_length,
            dtype=dtype,
            observation_mask=mask_rule,
            freshness_fallback_columns=fallback_columns,
            condition_columns=self.condition_columns,
        )
        # Resolve suppress_channels (config-driven; base names auto-expand to
        # rolling-derivative siblings), the A02.12 uncalibrated channels and the
        # A03.2 context inputs into the columns kept out of the score, once
        # (R04.2: the same resolution the benchmark and the batched scorer use).
        # ``score_window`` then just zeros the score mask at those indices.
        # Channels missing from the trained schema drop out -- a config typo or
        # a renamed channel is a no-op, not a serve crash.
        exclusions = score_exclusions(
            self.feature_columns,
            self.calibration,
            trained.manifest.schema,
            tuple(suppress_channels),
        )
        suppressed = exclusions.suppressed
        self.suppressed_channels: tuple[str, ...] = suppressed
        #: A01.5 — serving's suppress list against the one training ran with.
        #: Surfaced on ``/status`` (``health.sequence_suppression``).
        self.suppression = compare_suppression(
            trained, serving=tuple(suppress_channels), suppressed_columns=suppressed
        )
        if self.suppression.differs:
            log.warning(
                "sequence model %s: serving suppress_channels differ from training "
                "(training=%s, serving=%s); %d trained column(s) are calibrated in "
                "the thresholds but kept out of the score: %s",
                trained.manifest.model_version,
                "unrecorded"
                if self.suppression.training is None
                else list(self.suppression.training),
                list(self.suppression.serving),
                len(suppressed),
                ", ".join(suppressed),
            )
        column_index = {col: idx for idx, col in enumerate(self.feature_columns)}
        # A03.2: an ``engine_response`` artifact conditions on context inputs
        # (rpm, baro, speed, ...) that it was never trained to reconstruct or
        # calibrated on. They reach the encoder and never the score.
        self.context_columns: tuple[str, ...] = exclusions.context
        # A02.12: a channel whose calibrated threshold is not positive was
        # masked in every calibration window — never observed — so the global,
        # per-mode and family thresholds were all computed without it. Score it
        # the same way: out of the score mask, the family path and the
        # evidence, until a calibration covers it. Before this, a threshold of
        # 0.0 flagged it on any nonzero error (ECM_INJECTOR_PULSE_1/2 in the
        # 2026-09-20 artifact, once those DIDs joined the default profile).
        self.uncalibrated_channels: tuple[str, ...] = exclusions.uncalibrated
        if self.uncalibrated_channels:
            log.warning(
                "sequence model %s: %d channel(s) were never observed in calibration "
                "and are not scored until a calibration covers them: %s",
                trained.manifest.model_version,
                len(self.uncalibrated_channels),
                ", ".join(self.uncalibrated_channels),
            )
        excluded = exclusions.columns
        #: Columns kept out of the score, family path and evidence (suppressed
        #: by config or uncalibrated). The encoder still receives them.
        self._score_excluded: frozenset[str] = excluded
        self._score_excluded_indices: np.ndarray[Any, np.dtype[np.intp]] = np.array(
            sorted(column_index[col] for col in excluded if col in column_index),
            dtype=np.intp,
        )
        #: R04.2 — the verdict rule, shared with the benchmark and the harness.
        self.scoring_rule = SequenceScoringRule(self.calibration, self.feature_columns)
        self._torch = _torch_module()
        self._device = self._torch.device(device)
        self.trained.model.to(device=self._device, dtype=getattr(self._torch, dtype))
        self.trained.model.eval()
        # Materialize the per-column (means, stds) arrays once so each
        # ``score_window`` is a single broadcast subtract+divide instead of
        # building Python dicts per call. ``manifest.scaler`` is required
        # when thresholds are present (the load-time guard enforces it),
        # but allow ``None`` here so test doubles without a scaler keep
        # working as long as they also don't provide thresholds.
        self._scaler_means: np.ndarray[Any, np.dtype[np.float64]] | None
        self._scaler_stds: np.ndarray[Any, np.dtype[np.float64]] | None
        if trained.manifest.scaler is not None:
            from src.preprocessing.normalization import ZScoreScaler

            scaler = ZScoreScaler.from_dict(dict(trained.manifest.scaler))
            # R04.15: the scaler's own denominator rule (std, or 1.0 under the
            # floor), never a re-derivation of it.
            self._scaler_means, self._scaler_stds = scaler.vectors(self.feature_columns)
        else:
            self._scaler_means = None
            self._scaler_stds = None

    def reset(self) -> None:
        """Drop the current window; scoring resumes once it refills."""
        self.buffer.clear()

    def observe(self, row: Mapping[str, Any] | pd.Series) -> SequenceInferenceResult | None:
        """Append one row and score the current window once enough rows exist."""
        if not self.buffer.append(row):
            return None
        start_timestamp, end_timestamp, session = self.buffer.metadata()
        result = self.score_window(
            self.buffer.window_array(),
            mask=self.buffer.mask_array(),
            delta=self.buffer.delta_array(),
            timestamp=end_timestamp,
            session=session,
            window_start_timestamp=start_timestamp,
            window_end_timestamp=end_timestamp,
            operating_mode=self.buffer.last_mode,
        )
        span_seconds, max_step_seconds = self.buffer.span()
        return replace(
            result,
            window_rows=self.sequence_length,
            window_span_seconds=span_seconds,
            window_max_step_seconds=max_step_seconds,
            envelope=self.envelope_check(self.buffer.last_mode),
        )

    def envelope_check(self, mode: str | None) -> dict[str, Any] | None:
        """The current window against the training envelope (A06.2): a few
        comparisons per scored window. ``None`` when none was recorded."""
        if self.envelope is None:
            return None
        ranges = self.buffer.condition_ranges()
        return self.envelope.check(
            mode,
            {name: ranges.get(column) for name, column in self.envelope.channels.items()},
        )

    def score_window(
        self,
        window: np.ndarray[Any, np.dtype[np.float32]],
        *,
        mask: np.ndarray[Any, np.dtype[np.float32]] | None = None,
        delta: np.ndarray[Any, np.dtype[np.float32]] | None = None,
        timestamp: str,
        session: str,
        window_start_timestamp: str,
        window_end_timestamp: str,
        operating_mode: str | None = None,
    ) -> SequenceInferenceResult:
        """Score one complete ``(T, F)`` sequence window.

        ``mask`` / ``delta`` are the GRU-D per-cell observed-mask and Δt windows.
        When omitted (callers that only have values, e.g. tests scoring a raw
        window) every cell is treated as freshly observed with Δt 0 — the
        all-fresh case the model reduces to a plain autoencoder on.
        """
        expected_shape = (self.sequence_length, len(self.feature_columns))
        if window.shape != expected_shape:
            raise ValueError(
                "window shape must be "
                f"({self.sequence_length}, {len(self.feature_columns)}); got {window.shape}"
            )
        if mask is None:
            mask = np.ones(expected_shape, dtype=np.float32)
        if delta is None:
            delta = np.zeros(expected_shape, dtype=np.float32)
        if mask.shape != expected_shape or delta.shape != expected_shape:
            raise ValueError("mask and delta must match the value window shape")
        # Suppress configured noise channels from the *score* only (A01.5). The
        # mask is also an encoder input (GRU-D and TCN), so zeroing it there fed
        # the model mask=0 with Δt=0 — a combination training never produced —
        # and shifted every other channel's reconstruction. The encoder gets
        # the mask it was trained on; ``score_mask`` zeroes the suppressed
        # columns for the error, the global score and the top-channel evidence.
        score_mask = mask
        if self._score_excluded_indices.size:
            score_mask = mask.copy()
            score_mask[:, self._score_excluded_indices] = 0.0
        started = self.time_fn()
        # z-score the *values* against the training-time scaler so reconstruction
        # error is in the same scale as the calibrated threshold. Mask and Δt are
        # model-structure inputs and pass through unscaled.
        scaled_window: np.ndarray[Any, np.dtype[np.float32]] = window
        if self._scaler_means is not None and self._scaler_stds is not None:
            scaled_window = (
                (window.astype(np.float64) - self._scaler_means) / self._scaler_stds
            ).astype(np.float32)
        # Impute unobserved (non-finite) cells to 0 in z-scored space — the
        # channel mean — exactly as the offline dataset does (`where(isfinite,
        # value, 0)`), so a masked GPS-less cell never feeds the model a NaN.
        # The mask already keeps these cells out of the reconstruction error.
        scaled_window = np.where(np.isfinite(scaled_window), scaled_window, 0.0).astype(np.float32)
        torch_dtype = getattr(self._torch, self.dtype)
        value_t = (
            self._torch.as_tensor(scaled_window, dtype=torch_dtype).unsqueeze(0).to(self._device)
        )
        mask_t = self._torch.as_tensor(mask, dtype=torch_dtype).unsqueeze(0).to(self._device)
        delta_t = self._torch.as_tensor(delta, dtype=torch_dtype).unsqueeze(0).to(self._device)
        with self._torch.no_grad():
            # Phase 7.2a: one forward → the error the rule scores + the
            # actual/recon arrays the dashboard overlay needs.
            reconstructed = self.trained.model.forward(value_t, mask_t, delta_t)
            actual_np = value_t.detach().cpu().numpy()[0]  # (T, F)
            reconstructed_np = reconstructed.detach().cpu().numpy()[0]  # (T, F)
        # R04.2: the served rule, shared with the benchmark and the harness. The
        # error is **masked** so a forward-filled (stale) cell never inflates
        # the score, matching the masked thresholds calibrated at training time.
        abs_error = np.abs(reconstructed_np - actual_np)
        scores = self.scoring_rule.score_batch(
            abs_error[None, ...], score_mask[None, ...], [operating_mode]
        )
        global_score = float(scores.global_score[0])
        channel_values = scores.channel[0]
        timestep_error_np = abs_error * score_mask  # (T, F)
        latency_ms = (self.time_fn() - started) * 1000.0

        channel_scores = {
            column: float(channel_values[index])
            for index, column in enumerate(self.feature_columns)
        }
        channel_thresholds = {
            column: self.calibration.channel_errors[column].threshold
            for column in self.feature_columns
        }
        anomalous_channels = tuple(
            column for column, score in channel_scores.items() if score > channel_thresholds[column]
        )

        # Phase 7.2a evidence — rank channels by ratio (score / threshold)
        # when the threshold is positive (compares apples to apples across
        # channels with very different magnitudes); fall back to the raw
        # score otherwise. Keep only ``LSTM_TOP_CHANNELS`` so the per-event
        # detail payload stays bounded.
        def _ratio(column: str) -> float:
            threshold = channel_thresholds[column]
            score = channel_scores[column]
            return score / threshold if threshold > 0 else score

        rankable = [c for c in self.feature_columns if c not in self._score_excluded]
        top_channels = tuple(
            sorted(rankable or self.feature_columns, key=_ratio, reverse=True)[:LSTM_TOP_CHANNELS]
        )
        column_index = {column: index for index, column in enumerate(self.feature_columns)}
        reconstruction_series = {
            column: tuple(float(v) for v in timestep_error_np[:, column_index[column]])
            for column in top_channels
        }
        reconstruction_overlays = {
            column: {
                "actual": tuple(float(v) for v in actual_np[:, column_index[column]]),
                "reconstructed": tuple(float(v) for v in reconstructed_np[:, column_index[column]]),
            }
            for column in top_channels
        }
        # Phase 7.5 per-mode band with the global fallback, and the family
        # path, both from the shared rule (R04.2). Gate the emit on the
        # *global* error and the family path only: the per-channel thresholds
        # run far too tight live (with ~90 channels at least one is always over
        # its band), so an ``or bool(anomalous_channels)`` clause flooded
        # /ws/anomalies. ``anomalous_channels`` / ``channel_scores`` stay in the
        # event detail so a genuine anomaly is fully drillable.
        global_threshold = float(scores.global_threshold[0])
        family_scores = {
            family: float(value)
            for family, value in zip(scores.family_names, scores.family_scores[0], strict=True)
        }
        family_threshold = scores.family_threshold
        is_anomaly = bool(scores.verdict[0])
        return SequenceInferenceResult(
            model_version=trained_version(self.trained),
            timestamp=timestamp,
            session=session,
            window_start_timestamp=window_start_timestamp,
            window_end_timestamp=window_end_timestamp,
            global_score=global_score,
            global_threshold=global_threshold,
            channel_scores=channel_scores,
            channel_thresholds=channel_thresholds,
            anomalous_channels=anomalous_channels,
            is_anomaly=is_anomaly,
            latency_ms=latency_ms,
            top_channels=top_channels,
            reconstruction_series=reconstruction_series,
            reconstruction_overlays=reconstruction_overlays,
            family_scores=family_scores,
            family_threshold=family_threshold,
            ranking_scope="engine_response_targets" if self.context_columns else "all_channels",
        )


def trained_version(trained: TrainedSequenceModel) -> str:
    """Return the manifest model version for a trained sequence-detector bundle."""
    return str(trained.manifest.model_version)


def _format_timestamp(value: Any) -> str:
    if value is None or value == "":
        return ""
    return format_timestamp(value)


def _coerce_timestamp(value: Any) -> pd.Timestamp | None:
    """Parse a row's timestamp into a UTC ``pd.Timestamp`` for Δt arithmetic."""
    if value is None or value == "":
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _is_nan(value: Any) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _coerce_feature_value(value: Any) -> float:
    """Coerce a row's feature cell to float; absent / non-numeric ⇒ NaN.

    NaN flags the cell as unobserved for the mask — an absent sensor (no GPS) or
    a channel before its first observation, both legitimately masked rather than
    fatal.
    """
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _torch_module() -> Any:
    try:
        return importlib.import_module("torch")
    except ImportError as exc:
        # Imported here, not at module level: ``lstm_autoencoder``'s class
        # bases import ``torch.nn``, and this module is on the daemon's path
        # to recording (S01.2).
        from src.models.lstm_autoencoder import MissingLSTMAutoencoderDependencyError

        raise MissingLSTMAutoencoderDependencyError(
            "torch is required for LSTM inference; run scripts/install_deps.py "
            "or install the 'ml' optional dependency"
        ) from exc


# ---------------------------------------------------------------------------
# Streaming IF inference — beside SequenceInferenceEngine (moved from
# src.alerts.emitter, R01.8; that module still re-exports it)
# ---------------------------------------------------------------------------


class IsolationForestInferenceEngine:
    """Streaming Isolation Forest inference over single rows.

    Thin wrapper around ``TrainedIsolationForest.score`` that takes one
    row at a time and returns the single scored row. Symmetric with
    ``SequenceInferenceEngine.observe`` so the daemon can wire both detectors
    through the same row-processing loop.
    """

    def __init__(self, trained: Any) -> None:
        # `trained` is a TrainedIsolationForest; typed as Any to keep this
        # module from importing sklearn at import time.
        self.trained = trained
        self.feature_columns: tuple[str, ...] = tuple(trained.feature_columns)
        self.mode_column: str = trained.mode_column
        self.model_version: str = trained.manifest.model_version

    def observe(self, row: Mapping[str, Any] | pd.Series) -> pd.Series:
        """Score one row; return the scored Series.

        The caller passes the row to ``emit_isolation_forest_row`` to
        decide whether it merits an event. Splitting score / emit lets
        consumers log the score even when it didn't fire, useful for
        backfilling distributions.
        """
        if isinstance(row, pd.Series):
            frame = row.to_frame().T
        else:
            frame = pd.DataFrame([dict(row)])
        scored = self.trained.score(frame)
        out: pd.Series = scored.iloc[0]
        return out

    def feature_contributions(
        self, row: Mapping[str, Any] | pd.Series, *, top_k: int
    ) -> dict[str, float]:
        """The scoring mode model's own features, ranked by how much each
        raised this row's score (A04.2, occlusion — see
        ``TrainedIsolationForest.feature_contributions``). Never the
        ``feature_columns`` union: most of those are not features of the
        model that scored the row."""
        if isinstance(row, pd.Series):
            frame = row.to_frame().T
        else:
            frame = pd.DataFrame([dict(row)])
        contributions: dict[str, float] = self.trained.feature_contributions(frame, top_k=top_k)
        return contributions

    def contribution_reference(self, row: Mapping[str, Any] | pd.Series) -> str | None:
        """``mode_median`` / ``pooled_mean``: what occlusion replaced features
        with for this row's mode (A04.2)."""
        reference: str | None = self.trained.contribution_reference(row.get(self.mode_column))
        return reference
