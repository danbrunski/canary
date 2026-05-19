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

    from src.alerts.fusion import FusedResult
    from src.models.inference import LSTMInferenceResult

#: ``fused`` (Phase 7.2) is the agreement-aware calibrated severity over the
#: per-tick IF + LSTM + behavior-residual sources. ``behavior`` (Phase 7.4)
#: is the behavior/physics-model residual promoted to a first-class
#: detector — one event per model with a human-readable physical cause. Both
#: are emitted alongside the per-detector events, not instead of them.
AnomalySource = Literal["isolation_forest", "lstm_autoencoder", "fused", "behavior"]


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
    def from_fused(
        cls,
        result: FusedResult,
        *,
        model_version: str,
        severity_threshold: float,
    ) -> AnomalyEvent:
        """Build the fused event (Phase 7.2).

        ``score`` is the agreement-aware severity in ``[0, 1]`` and
        ``threshold`` is the configured emit boundary, so the dashboard and
        tuner can reason about it on the same score/threshold contract as the
        per-source events. The per-source breakdown lives in ``detail`` (the
        seed of 7.2a's richer attribution).
        """
        return cls(
            timestamp=result.timestamp,
            source="fused",
            model_version=model_version,
            score=result.severity,
            threshold=severity_threshold,
            affected_channels=tuple(result.contributing),
            operating_mode=result.operating_mode,
            detail=result.to_detail(),
        )

    @classmethod
    def from_behavior(
        cls,
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
    ) -> AnomalyEvent:
        """Build a behavior/physics residual event (Phase 7.4).

        The behavior models are the most interpretable signal available, so
        the event leads with a human-readable physical cause and carries the
        residual in ``detail.behavior_residuals`` — the exact shape the
        dashboard's ``extractResiduals`` already renders (expected vs actual
        + band). ``score``/``threshold`` use the same normalized-severity
        contract as the fused/per-source events so the tuner and dashboard
        treat it uniformly.
        """
        return cls(
            timestamp=timestamp,
            source="behavior",
            model_version=f"behavior/{model}",
            score=score,
            threshold=threshold,
            affected_channels=(channel,),
            operating_mode=operating_mode,
            detail={
                "cause": behavior_cause(
                    model=model,
                    channel=channel,
                    severity_band=severity_band,
                    actual=actual,
                    expected=expected,
                    deviation=deviation,
                ),
                "model": model,
                "kind": kind,
                "behavior_residuals": [
                    {
                        "name": model,
                        "channel": channel,
                        "severity": severity_band,
                        "actual": actual,
                        "expected": expected,
                        "deviation": deviation,
                    }
                ],
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
        threshold: float | None = None,
    ) -> AnomalyEvent:
        """Build an event from one row of a scored Isolation Forest frame.

        ``threshold`` is the 7.2 per-mode calibrated decision boundary when
        the row was scored by a calibrated mode (``None`` for a legacy
        contamination-label mode, preserving the old "IF has no fixed
        threshold" contract).
        """
        mode = row.get(mode_column)
        return cls(
            timestamp=timestamp,
            source="isolation_forest",
            model_version=model_version,
            score=float(row[score_column]),
            threshold=threshold,
            affected_channels=affected_channels,
            operating_mode=None if mode is None else str(mode),
            session=session,
            latency_ms=None,
        )


def behavior_cause(
    *,
    model: str,
    channel: str,
    severity_band: str,
    actual: float | None,
    expected: float | None,
    deviation: float | None,
) -> str:
    """One human-readable line explaining a behavior-residual detection.

    Physical and generic — names the model, the channel that diverged, and
    the observed-vs-expected gap. No hardware editorializing.
    """

    def _fmt(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.4g}"

    delta = "" if deviation is None else f", Δ {deviation:+.4g}"
    return (
        f"{model} {severity_band}: {channel} actual {_fmt(actual)} "
        f"vs expected {_fmt(expected)}{delta}"
    )
