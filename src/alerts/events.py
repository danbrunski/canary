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
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # avoid importing pandas/torch-touching modules at import time
    import pandas as pd

    from src.alerts.fusion import FusedResult
    from src.models.inference import SequenceInferenceResult

#: ``fused`` (Phase 7.2) is the agreement-aware calibrated severity over the
#: per-tick IF + LSTM + behavior-residual sources. ``behavior`` (Phase 7.4)
#: is the behavior/physics-model residual promoted to a first-class
#: detector — one event per model with a human-readable physical cause.
#: ``dtc`` (Task 1.6) is a new/escalated trouble code from the periodic
#: Mode 03/07/0A poll. ``trend`` (Task 4.6) is a session-fingerprint stat in
#: sustained CUSUM alarm against the frozen reference epoch — a low-frequency,
#: per-session event, not a per-tick one. ``misfire`` (Task 3.1) is a
#: per-cylinder misfire count *increase* from the periodic Mode 06 poll. All
#: are emitted alongside the per-detector events, not instead of them.
AnomalySource = Literal[
    "isolation_forest", "lstm_autoencoder", "fused", "behavior", "dtc", "trend", "misfire"
]


#: A03.7 — the sequence detector's channel ranking, named for what it is.
SEQUENCE_CHANNEL_RANKING = "largest_deviation_from_training_envelope"
#: Keyed by ``SequenceInferenceResult.ranking_scope``: an ``all_channels``
#: artifact ranks driving conditions too; an ``engine_response`` one ranks its
#: engine-response targets only (context inputs never scored, A03.2).
SEQUENCE_CHANNEL_RANKING_NOTES: dict[str, str] = {
    "all_channels": (
        "Channels ranked by reconstruction error over their calibrated band, driving "
        "conditions included: the largest deviations from the training envelope, not a cause."
    ),
    "engine_response_targets": (
        "Engine-response channels ranked by reconstruction error over their calibrated band "
        "(driving conditions are context, never ranked): the largest deviations from the "
        "training envelope, not a cause."
    ),
}


#: A06.2 — how many of the ranked channels the novelty sentence names.
NOVELTY_SUMMARY_DEVIATIONS = 3


def _channel_label(column: str) -> str:
    """``obd:BAROMETRIC_PRESSURE`` -> ``BAROMETRIC_PRESSURE`` for prose."""
    return column.split(":", 1)[-1]


def _envelope_quantity_phrase(entry: Mapping[str, Any]) -> str:
    unit = f" {entry['unit']}" if entry.get("unit") else ""
    low, high = entry["trained_min"], entry["trained_max"]
    seen: list[float] = []
    if entry["window_min"] is not None and entry["window_min"] < low:
        seen.append(entry["window_min"])
    if entry["window_max"] is not None and entry["window_max"] > high:
        seen.append(entry["window_max"])
    observed = "–".join(f"{value:g}" for value in seen)
    return (
        f"{_channel_label(str(entry['channel']))} ({observed}{unit} vs trained "
        f"{low:g}–{high:g}{unit})"
    )


def sequence_envelope_summary(
    envelope: Mapping[str, Any] | None, top_channels: tuple[str, ...] = ()
) -> str:
    """A06.2 — the novelty screen's sentence for one sequence window.

    Leads with where the window sits against the artifact's training envelope
    (``SequenceInferenceResult.envelope``), then names the largest
    deviations. An artifact without an envelope says "not recorded"; a window
    nothing could be compared on says why — neither reads as "inside".
    """
    mode = None if envelope is None else envelope.get("mode")
    in_mode = f" in {mode}" if mode else ""
    if envelope is None:
        lead = "Training envelope not recorded for this artifact"
    elif envelope.get("status") == "mode_not_trained":
        lead = f"Outside the learned envelope: mode {mode!r} was never in training"
    elif envelope.get("status") == "outside":
        quantities = envelope.get("quantities", {})
        phrases = [
            _envelope_quantity_phrase(quantities[name])
            for name in envelope.get("outside_on", ())
            if name in quantities
        ]
        lead = f"Outside the learned envelope{in_mode} on " + ", ".join(phrases)
    elif envelope.get("status") == "inside":
        compared = [
            _channel_label(str(entry["channel"]))
            for entry in envelope.get("quantities", {}).values()
            if entry.get("status") == "inside"
        ]
        lead = f"Inside the learned envelope{in_mode} on " + ", ".join(compared)
    else:
        lead = f"Envelope not compared: {envelope.get('reason') or 'no comparable quantity'}"
    deviations = [_channel_label(c) for c in top_channels[:NOVELTY_SUMMARY_DEVIATIONS]]
    if deviations:
        return f"{lead}; largest deviations: {', '.join(deviations)}."
    return f"{lead}."


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
    #: 6.4 — the emitter's dedupe identity when it differs from
    #: ``(source, model_version)``. ``None`` (every non-fused event) falls
    #: back to ``model_version`` as before. The fused event sets this to a
    #: stable ``"fused"`` because its ``model_version`` embeds BOTH backing
    #: model versions (``if=<v>|lstm=<v>``) purely for provenance — hashing
    #: that into the dedupe key meant any IF/LSTM hot-swap changed the key
    #: and let a duplicate fused alert burst through right at the swap.
    dedupe_key: str | None = None
    #: A06.5 — ``evidence`` marks an event from a detector configured as
    #: non-alerting (``models.roles``): stored and queryable as low-weight
    #: evidence, but never pushed and never shown as an active alert. Stamped
    #: by ``AnomalyEventEmitter`` from config, never promoted back to
    #: ``alert``. Records written before A06.5 load as ``alert``, which is
    #: what they were.
    role: Literal["alert", "evidence"] = "alert"

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
        result: SequenceInferenceResult,
        *,
        operating_mode: str | None = None,
    ) -> AnomalyEvent:
        """Build an event from a ``SequenceInferenceResult`` (caller checks
        ``is_anomaly``).

        Phase 7.2a — when the result carries top-channel evidence, the
        event surfaces it: ``affected_channels`` is the top-by-ratio set
        (cleaner than the noisy "above per-channel threshold" list, which
        is kept in ``detail.anomalous_channels`` for diagnostics);
        ``channel_scores`` and ``channel_thresholds`` are filtered to the
        same top channels so the per-event payload stays bounded;
        ``reconstruction_series`` and ``reconstruction_overlays`` give the
        dashboard heatmap and overlay panels the data they need without a
        round-trip to the model.
        """
        top_channels = tuple(getattr(result, "top_channels", ()) or ())
        channel_scores = dict(result.channel_scores)
        channel_thresholds = dict(result.channel_thresholds)
        if top_channels:
            channel_scores = {
                column: channel_scores[column]
                for column in top_channels
                if column in channel_scores
            }
            channel_thresholds = {
                column: channel_thresholds[column]
                for column in top_channels
                if column in channel_thresholds
            }
            affected = top_channels
        else:
            affected = tuple(result.anomalous_channels)
        detail: dict[str, Any] = {
            "channel_scores": channel_scores,
            "channel_thresholds": channel_thresholds,
            "anomalous_channels": list(result.anomalous_channels),
            # A03.7 — what the channel ranking is. It ranks each channel's
            # reconstruction error against its own calibrated band over every
            # column, driving conditions included (it named baro on every
            # altitude-trip event, D03.20): the largest deviation from the
            # training envelope, not the cause of the event.
            "channel_ranking": SEQUENCE_CHANNEL_RANKING,
            "channel_ranking_note": SEQUENCE_CHANNEL_RANKING_NOTES.get(
                str(getattr(result, "ranking_scope", "all_channels")),
                SEQUENCE_CHANNEL_RANKING_NOTES["all_channels"],
            ),
        }
        # A06.2 — the novelty screen: where the window sits against the
        # artifact's training envelope (``None``: not recorded), in a sentence
        # beside the structured verdict.
        envelope = getattr(result, "envelope", None)
        detail["envelope"] = None if envelope is None else dict(envelope)
        detail["novelty_summary"] = sequence_envelope_summary(envelope, top_channels)
        recon_series = getattr(result, "reconstruction_series", None)
        if recon_series:
            detail["reconstruction_series"] = {k: list(v) for k, v in recon_series.items()}
        recon_overlays = getattr(result, "reconstruction_overlays", None)
        if recon_overlays:
            detail["reconstruction_overlays"] = {
                k: {sub: list(seq) for sub, seq in overlay.items()}
                for k, overlay in recon_overlays.items()
            }
        return cls(
            timestamp=result.timestamp,
            source="lstm_autoencoder",
            model_version=result.model_version,
            score=result.global_score,
            threshold=result.global_threshold,
            affected_channels=affected,
            operating_mode=operating_mode,
            session=result.session or None,
            window_start_timestamp=result.window_start_timestamp or None,
            window_end_timestamp=result.window_end_timestamp or None,
            latency_ms=result.latency_ms,
            detail=detail,
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
        seed of 7.2a's richer attribution). ``model_version`` (the
        ``if=<v>|lstm=<v>`` provenance string) stays on the event for
        debugging but is NOT the dedupe identity (6.4) — ``dedupe_key`` is a
        stable ``"fused"`` so an IF/LSTM hot-swap mid-drive can't burst a
        duplicate fused alert by changing the dedupe key underneath an
        active refractory window.
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
            dedupe_key="fused",
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
        override_reason: str | None = None,
    ) -> AnomalyEvent:
        """Build a behavior/physics residual event (Phase 7.4).

        The behavior models are the most interpretable signal available, so
        the event leads with a human-readable physical cause and carries the
        residual in ``detail.behavior_residuals`` — the exact shape the
        dashboard's ``extractResiduals`` already renders (expected vs actual
        + band). ``score``/``threshold`` use the same normalized-severity
        contract as the fused/per-source events so the tuner and dashboard
        treat it uniformly.

        ``override_reason`` (2.2, online-training-strategy.md Phase 2) is
        the active accepted-baseline override's ``provenance.reason`` when
        this tick's band came from a recentered/one-sided override rather
        than the model's own static config — surfaced in ``detail`` (not a
        new event shape) so a silenced-then-worsening channel always reads
        back *why* it has a sharpened, one-sided expectation instead of a
        symmetric one.
        """
        detail: dict[str, Any] = {
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
        }
        if override_reason is not None:
            detail["override_reason"] = override_reason
        return cls(
            timestamp=timestamp,
            source="behavior",
            model_version=f"behavior/{model}",
            score=score,
            threshold=threshold,
            affected_channels=(channel,),
            operating_mode=operating_mode,
            detail=detail,
        )

    @classmethod
    def from_dtc(
        cls,
        *,
        code: str,
        description: str,
        system: str,
        status: str,
        kind: Literal["new", "escalated"],
        timestamp: str,
        mil_on: bool | None = None,
        session: str | None = None,
    ) -> AnomalyEvent:
        """Build a new/escalated DTC event (Task 1.6).

        No fixed score/threshold contract applies here (a DTC either fired
        or didn't) — ``score`` is a constant ``1.0`` and ``threshold`` stays
        ``None``, matching the IF's "no fixed threshold" convention.
        ``dedupe_key`` includes the code + status so distinct codes (or the
        same code's pending→stored escalation) don't suppress each other
        under the emitter's refractory window.
        """
        return cls(
            timestamp=timestamp,
            source="dtc",
            model_version="dtc",
            score=1.0,
            threshold=None,
            session=session,
            detail={
                "code": code,
                "description": description,
                "system": system,
                "status": status,
                "kind": kind,
                "mil_on": mil_on,
            },
            dedupe_key=f"dtc:{code}:{status}",
        )

    @classmethod
    def from_trend(
        cls,
        *,
        stat: str,
        session: str,
        timestamp: str,
        latest_value: float,
        reference_mean: float,
        reference_std: float,
        cusum_sh: float,
        cusum_sl: float,
        cusum_h: float,
        direction: Literal["upper", "lower"],
        mann_kendall_trend: str | None = None,
        mann_kendall_p_value: float | None = None,
    ) -> AnomalyEvent:
        """Build a sustained-CUSUM-alarm session-fingerprint trend event
        (Task 4.6).

        ``score`` is the alarming side's CUSUM statistic (``sh`` for an
        ``"upper"`` excursion, ``sl`` for ``"lower"``) and ``threshold`` is
        the configured decision interval ``h``, matching the
        score/threshold-on-the-same-scale contract every other detector
        uses. ``dedupe_key`` is ``"trend:<stat>"`` — independent per stat, so
        one stat's ongoing alarm never suppresses another's — the actual
        "at most once per session per stat" guarantee is enforced by the
        caller (:func:`src.control.trends.evaluate_and_alert_trends`)
        BEFORE this is even constructed, via a tiny persisted
        already-alerted marker; this dedupe key just keeps the emitter's own
        refractory-window behavior sane on top of that.
        """
        score = cusum_sh if direction == "upper" else cusum_sl
        return cls(
            timestamp=timestamp,
            source="trend",
            model_version="trend",
            score=score,
            threshold=cusum_h,
            session=session,
            detail={
                "stat": stat,
                "latest_value": latest_value,
                "reference_mean": reference_mean,
                "reference_std": reference_std,
                "cusum_sh": cusum_sh,
                "cusum_sl": cusum_sl,
                "direction": direction,
                "mann_kendall_trend": mann_kendall_trend,
                "mann_kendall_p_value": mann_kendall_p_value,
            },
            dedupe_key=f"trend:{stat}",
        )

    @classmethod
    def from_misfire(
        cls,
        *,
        cylinder: int,
        count: int,
        previous_count: int,
        timestamp: str,
        session: str | None = None,
    ) -> AnomalyEvent:
        """Build a per-cylinder misfire-count-increase event (Task 3.1).

        Same "no fixed score/threshold" contract as ``from_dtc`` — a Mode 06
        misfire counter either increased or didn't. ``score`` is the raw
        count so the dashboard/tuner can at least rank severity across
        events without a synthetic normalization; ``dedupe_key`` includes
        the cylinder + count so a second cylinder incrementing (or the same
        cylinder incrementing again later) isn't suppressed by the emitter's
        refractory window.
        """
        return cls(
            timestamp=timestamp,
            source="misfire",
            model_version="misfire",
            score=float(count),
            threshold=None,
            session=session,
            affected_channels=(f"CYLINDER_{cylinder}",),
            detail={
                "cylinder": cylinder,
                "count": count,
                "previous_count": previous_count,
            },
            dedupe_key=f"misfire:{cylinder}:{count}",
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
        feature_contributors: Mapping[str, float] | None = None,
    ) -> AnomalyEvent:
        """Build an event from one row of a scored Isolation Forest frame.

        ``threshold`` is the 7.2 per-mode calibrated decision boundary when
        the row was scored by a calibrated mode (``None`` for a legacy
        contamination-label mode, preserving the old "IF has no fixed
        threshold" contract). ``feature_contributors`` (Phase 7.2a) is the
        ``{feature: |scaled-z|}`` ranking the daemon computes from the
        scaler's view of this row — placed in ``detail.feature_contributors``
        so the dashboard's evidence panel can rank by feature distance from
        the training baseline (the cheap-first-pass attribution; SHAP-style
        offline analysis is the optional follow-up).
        """
        mode = row.get(mode_column)
        detail: dict[str, Any] = {}
        if feature_contributors:
            detail["feature_contributors"] = {
                str(name): float(value) for name, value in feature_contributors.items()
            }
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
            detail=detail,
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
