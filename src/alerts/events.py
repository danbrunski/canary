"""Anomaly event schema — the common record both detectors emit (Task 5.1).

The Isolation Forest scores rows and the LSTM autoencoder scores sliding
windows; an :class:`AnomalyEvent` is the unified, JSON-safe record that the
event logger persists and the dashboard backend serves. The two adapter
constructors (:meth:`AnomalyEvent.from_lstm_result`,
:meth:`AnomalyEvent.from_isolation_forest_row`) keep the mapping from each
detector's native output in one place.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # avoid importing pandas/torch-touching modules at import time
    import pandas as pd

    from src.models.inference import LSTMInferenceResult

AnomalySource = Literal["isolation_forest", "lstm_autoencoder"]


class AnomalyEvent(BaseModel):
    """One anomaly detection, from either detector.

    ``timestamp`` is the moment the anomaly was attributed to (for the LSTM,
    the window's end timestamp; for the IF, the row's timestamp). ``score`` is
    larger-is-worse on whatever scale the source uses (IF anomaly score / LSTM
    global reconstruction error); ``threshold`` is the value it crossed when
    the source has a fixed one (the LSTM does; the IF's contamination is a
    quantile, so it's left ``None``). ``affected_channels`` is the source's best
    attribution of which signals drove it. Everything source-specific lands in
    ``detail`` so the schema stays stable.
    """

    # protected_namespaces=() so the `model_version` field doesn't collide with
    # Pydantic's reserved `model_*` namespace.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    timestamp: str
    source: AnomalySource
    model_version: str
    score: float
    threshold: float | None = None
    affected_channels: tuple[str, ...] = ()
    operating_mode: str | None = None
    session: str | None = None
    window_start_timestamp: str | None = None
    window_end_timestamp: str | None = None
    latency_ms: float | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnomalyEvent:
        return cls.model_validate(data)

    @classmethod
    def from_lstm_result(
        cls,
        result: LSTMInferenceResult,
        *,
        operating_mode: str | None = None,
    ) -> AnomalyEvent:
        """Build an event from a ``LSTMInferenceResult`` (caller checks ``is_anomaly``)."""
        return cls(
            timestamp=result.timestamp,
            source="lstm_autoencoder",
            model_version=result.model_version,
            score=result.global_score,
            threshold=result.global_threshold,
            affected_channels=tuple(result.anomalous_channels),
            operating_mode=operating_mode,
            session=result.session or None,
            window_start_timestamp=result.window_start_timestamp or None,
            window_end_timestamp=result.window_end_timestamp or None,
            latency_ms=result.latency_ms,
            detail={
                "channel_scores": dict(result.channel_scores),
                "channel_thresholds": dict(result.channel_thresholds),
            },
        )

    @classmethod
    def from_isolation_forest_row(
        cls,
        row: pd.Series,
        *,
        model_version: str,
        timestamp: str,
        score_column: str = "isolation_forest_score",
        mode_column: str = "operating_mode",
        affected_channels: tuple[str, ...] = (),
        session: str | None = None,
    ) -> AnomalyEvent:
        """Build an event from one row of a scored Isolation Forest frame."""
        mode = row.get(mode_column)
        return cls(
            timestamp=timestamp,
            source="isolation_forest",
            model_version=model_version,
            score=float(row[score_column]),
            threshold=None,
            affected_channels=affected_channels,
            operating_mode=None if mode is None else str(mode),
            session=session,
            latency_ms=None,
        )
