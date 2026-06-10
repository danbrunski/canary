"""Bridge inference results to the AnomalyEventLogger (Tasks 5.1+5.2 wire-up).

The Isolation Forest and LSTM autoencoder produce different shapes; this
module turns each into the unified ``AnomalyEvent`` and persists it through
``AnomalyEventLogger``. It owns:

* **emission policy** — *when* a result counts as an event. The LSTM has
  ``result.is_anomaly`` already; the IF labels rows ``-1`` (outlier) or
  ``1`` (inlier) plus the ``isolation_forest_missing_features`` flag for
  the dropout case. Both surface here as one method call.
* **dedupe** — back-to-back identical events from a noisy detector would
  spam the logger and the dashboard. A 1-second minimum gap per
  ``(source, model_version)`` is applied by default; turn it off with
  ``min_seconds_between_events=0`` for tests / replay.
* **counter accounting** — ``emitted`` / ``suppressed`` are observable so
  the daemon's `/status` endpoint (5.3) can surface them.

Pure wiring; no torch / sklearn imports — kept narrow on purpose so the
daemon can construct one without touching the model layers.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.events import AnomalyEvent
from src.alerts.tuning import AlertTuner
from src.models.isolation_forest import (
    CALIBRATED_ANOMALY_COLUMN,
    CALIBRATED_THRESHOLD_COLUMN,
    LABEL_COLUMN,
    MISSING_FEATURES_COLUMN,
    SCORE_COLUMN,
)

if TYPE_CHECKING:  # avoid importing torch-touching modules at import time
    from src.alerts.fusion import FusedResult
    from src.models.inference import SequenceInferenceResult

log = logging.getLogger(__name__)


@dataclass
class EmitterCounters:
    """Observable counts; rolling totals since the emitter was constructed."""

    emitted: int = 0
    suppressed_not_anomaly: int = 0
    suppressed_dedupe: int = 0
    #: Suppressed by an operator tuning rule (Task 6.25) — distinct from
    #: dedupe so the dashboard can show "tuning is muting N/hr" explicitly.
    suppressed_tuning: int = 0
    #: Over-threshold ticks withheld by the persistence gate (Phase 7.5) —
    #: the detector fired but hadn't fired for min_consecutive_ticks yet.
    suppressed_persistence: int = 0

    def reset(self) -> None:
        self.emitted = 0
        self.suppressed_not_anomaly = 0
        self.suppressed_dedupe = 0
        self.suppressed_tuning = 0
        self.suppressed_persistence = 0

    def to_dict(self) -> dict[str, int]:
        """JSON-safe snapshot — what ``GET /status`` exposes per detector."""
        return {
            "emitted": self.emitted,
            "suppressed_not_anomaly": self.suppressed_not_anomaly,
            "suppressed_dedupe": self.suppressed_dedupe,
            "suppressed_tuning": self.suppressed_tuning,
            "suppressed_persistence": self.suppressed_persistence,
        }


class AnomalyEventEmitter:
    """Convert per-detector results into ``AnomalyEvent`` and log them.

    Construct one per running detector (or share across both — the dedupe
    key is ``(source, model_version)`` so a single emitter feeds both
    safely). Not thread-safe; wrap with a lock if the LSTM and IF run on
    different threads, which they currently don't.
    """

    def __init__(
        self,
        logger: AnomalyEventLogger,
        *,
        min_seconds_between_events: float = 1.0,
        tuner: AlertTuner | None = None,
    ) -> None:
        self._logger = logger
        self._min_seconds_between_events = min_seconds_between_events
        self._tuner = tuner
        self._last_emit: dict[tuple[str, str], float] = {}
        self.counters = EmitterCounters()

    def emit_lstm(
        self,
        result: SequenceInferenceResult,
        *,
        operating_mode: str | None = None,
    ) -> AnomalyEvent | None:
        """Emit an event from a streaming sequence-detector result, if anomalous + not deduped."""
        if not result.is_anomaly:
            self.counters.suppressed_not_anomaly += 1
            return None
        event = AnomalyEvent.from_lstm_result(result, operating_mode=operating_mode)
        return self._emit(event)

    def emit_isolation_forest_row(
        self,
        row: pd.Series,
        *,
        model_version: str,
        timestamp: str,
        affected_channels: tuple[str, ...] = (),
        session: str | None = None,
        score_column: str = SCORE_COLUMN,
        label_column: str = LABEL_COLUMN,
        missing_features_column: str = MISSING_FEATURES_COLUMN,
        calibrated_anomaly_column: str = CALIBRATED_ANOMALY_COLUMN,
        calibrated_threshold_column: str = CALIBRATED_THRESHOLD_COLUMN,
        mode_column: str = "operating_mode",
        feature_contributors: Mapping[str, float] | None = None,
    ) -> AnomalyEvent | None:
        """Emit an event from one row of a scored Isolation Forest frame.

        Anomaly rule (Phase 7.2): prefer the per-mode *calibrated* verdict
        (``isolation_forest_calibrated_anomaly``) when the row's mode was
        calibrated; otherwise fall back to the legacy contamination label
        (``-1``). The missing-features dropout flag forces anomalous in both
        cases. Rows that hit neither ``suppressed_not_anomaly`` and return
        ``None`` without persisting.
        """
        missing = bool(row.get(missing_features_column, False))
        calibrated = row.get(calibrated_anomaly_column)
        if calibrated is not None and pd.notna(calibrated):
            is_anomaly = missing or bool(calibrated)
        else:
            label = row.get(label_column)
            is_anomaly = missing or (label is not None and pd.notna(label) and int(label) == -1)
        if not is_anomaly:
            self.counters.suppressed_not_anomaly += 1
            return None
        raw_threshold = row.get(calibrated_threshold_column)
        threshold = (
            float(raw_threshold) if raw_threshold is not None and pd.notna(raw_threshold) else None
        )
        event = AnomalyEvent.from_isolation_forest_row(
            row,
            model_version=model_version,
            timestamp=timestamp,
            score_column=score_column,
            mode_column=mode_column,
            affected_channels=affected_channels,
            session=session,
            threshold=threshold,
            feature_contributors=feature_contributors,
        )
        if missing:
            # tag dropout vs ordinary outlier so the dashboard can render
            # the two cases differently — same source/model, different reason.
            detail = dict(event.detail)
            detail["missing_features"] = True
            event = event.model_copy(update={"detail": detail})
        return self._emit(event)

    def emit_fused(
        self,
        result: FusedResult,
        *,
        model_version: str,
        severity_threshold: float,
    ) -> AnomalyEvent | None:
        """Emit the fused event (Phase 7.2), if anomalous + not deduped/tuned.

        Goes through the same ``_emit`` gate as the per-source events, so
        dedupe and the operator tuner now also operate on the fused event —
        its ``(source="fused", model_version)`` dedupe key is independent of
        the per-detector keys, so keeping both streams doesn't cross-suppress.
        """
        if not result.is_anomaly:
            self.counters.suppressed_not_anomaly += 1
            return None
        event = AnomalyEvent.from_fused(
            result,
            model_version=model_version,
            severity_threshold=severity_threshold,
        )
        return self._emit(event)

    def emit_behavior(
        self,
        *,
        model: str,
        kind: str,
        channel: str,
        timestamp: str,
        severity_band: str,
        score: float,
        threshold: float,
        actual: float | None,
        expected: float | None,
        deviation: float | None,
        operating_mode: str | None = None,
    ) -> AnomalyEvent | None:
        """Emit a behavior/physics residual event (Phase 7.4).

        First-class detector, kept alongside the per-source + fused events.
        Only ``warning`` / ``critical`` bands fire (``ok`` ⇒
        ``suppressed_not_anomaly``); dedupe keys on
        ``(source="behavior", "behavior/<model>")`` so each physical model
        debounces independently and never cross-suppresses another.
        """
        if severity_band not in ("warning", "critical"):
            self.counters.suppressed_not_anomaly += 1
            return None
        event = AnomalyEvent.from_behavior(
            model=model,
            kind=kind,
            channel=channel,
            timestamp=timestamp,
            severity_band=severity_band,
            score=score,
            threshold=threshold,
            actual=actual,
            expected=expected,
            deviation=deviation,
            operating_mode=operating_mode,
        )
        return self._emit(event)

    def _emit(self, event: AnomalyEvent) -> AnomalyEvent | None:
        # Operator tuning gate (6.25) runs before dedupe: a tuned-out event
        # should never reach the logger/dashboard at all. Suppression-only by
        # contract — the tuner can mute, never manufacture.
        if self._tuner is not None:
            decision = self._tuner.decide(event, now=_seconds(event.timestamp))
            if not decision.emit:
                self.counters.suppressed_tuning += 1
                log.debug("tuning suppressed event", extra={"reason": decision.reason})
                return None
        if self._is_deduped(event):
            self.counters.suppressed_dedupe += 1
            return None
        self._logger.log(event)
        self.counters.emitted += 1
        self._last_emit[(event.source, event.model_version)] = _seconds(event.timestamp)
        if self._tuner is not None:
            self._tuner.notify_emitted(event, now=_seconds(event.timestamp))
        return event

    def _is_deduped(self, event: AnomalyEvent) -> bool:
        if self._min_seconds_between_events <= 0:
            return False
        key = (event.source, event.model_version)
        prior = self._last_emit.get(key)
        if prior is None:
            return False
        now = _seconds(event.timestamp)
        return (now - prior) < self._min_seconds_between_events


def _seconds(timestamp: str) -> float:
    """Coerce an event timestamp into an epoch-seconds float.

    Accepts the ISO-8601 strings the adapters produce (``..."+00:00"`` or
    ``..."Z"``); raises ``ValueError`` on garbage so the emitter doesn't
    silently accept timestamps that won't compare meaningfully.
    """
    cleaned = timestamp.replace("Z", "+00:00") if timestamp.endswith("Z") else timestamp
    dt = datetime.fromisoformat(cleaned)
    return dt.timestamp()


# ---------------------------------------------------------------------------
# Streaming IF inference — analogous to SequenceInferenceEngine
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
        # module from importing sklearn at import time (the daemon may
        # construct an emitter long before any model is loaded).
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


__all__ = [
    "AnomalyEventEmitter",
    "EmitterCounters",
    "IsolationForestInferenceEngine",
]
