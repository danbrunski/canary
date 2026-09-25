"""The behavior-model overlay detector: residuals on the loop, deviation emits (R03.9)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.alerts.fusion import (
    FusionScorer,
    FusionSource,
)
from src.coerce import finite_float
from src.daemon.components import DaemonComponents
from src.daemon.detectors.common import (
    TickContext,
    _publish_score,
    gate_and_collect,
)
from src.dashboard.behavior_live import BehaviorLiveState, BehaviorObservation
from src.decode.behavior_decoder import deviation, deviation_for_override
from src.decode.behavior_eligibility import resolve_output_column
from src.decode.behavior_models import (
    BehaviorModel1D,
    serving_status,
)
from src.preprocessing.live import LiveFeatureSource
from src.preprocessing.segmenter import MODE_COLUMN
from src.telemetry import DECODED_SOURCE, SOURCE_SEP, channel_key
from src.timefmt import format_timestamp


class BehaviorDetector:
    """Behavior/physics-model residual overlay → first-class behavior events
    + fusion sources (Task 3.1; Phase 7.4).

    Unlike IF/sequence, one tick can contribute zero, one, or several
    ``FusionSource`` instances — one per fired behavior model, since sibling
    models graded on the same output channel share a fusion group but models
    on different channels are independent voices. Reads
    ``source.latest_operating_point`` (via ``_observe_behavior``), not
    ``row`` — the live overlay predates the shared per-tick ``row`` and
    already has everything it needs off the feature source directly.
    """

    def __init__(
        self, components: DaemonComponents, fusion: FusionScorer, source: LiveFeatureSource
    ) -> None:
        self._components = components
        self._fusion = fusion
        self._source = source

    def prepare(self, row: Any, ctx: TickContext) -> list[_BehaviorResidual]:
        """Loop side (C08.4): grade every behavior model off the source's
        ``latest_operating_point`` and feed the live overlay. This reads
        loop-owned ``LiveFeatureSource`` state, so it stays on the loop —
        there is no worker-side half for the behavior detector (the
        surfaces are small interpolations, far cheaper than the feature
        stage; see docs/inference-scheduling.md)."""
        del row, ctx
        return _observe_behavior(self._source, self._components.behavior_live)

    def apply(self, residuals: list[_BehaviorResidual], ctx: TickContext) -> list[FusionSource]:
        """Loop side: persistence, ``/ws/scores``, per-model emit, fusion
        sources — one per fired model."""
        sources: list[FusionSource] = []
        warn_severity = self._components.config.models.fusion.behavior_warning_severity
        persistence = self._components.persistence_gate
        tuner = self._components.alert_tuner
        for residual in residuals:
            # B01.6 — a diagnostic-only model never raises a trusted alert or
            # a fusion vote; it stays visible on the overlay with its reason.
            if not residual.alerting:
                continue
            # Surfaces over one measurement (same output channel, or one
            # family such as MAF airflow or fuel trim) share a fusion group,
            # derived inside ``behavior_source``: one agreeing voice, not two.
            behavior = self._fusion.behavior_source(
                f"behavior:{residual.name}",
                residual.severity,
                channel=residual.channel,
            )
            # 6.1 — off-mode is a missing verdict (the model didn't evaluate
            # this tick at all), not a miss: freeze the M-of-N window instead
            # of pushing a False that would decay real evidence accumulated
            # the last time this model was in-mode. On-mode-but-not-deviating
            # is a genuine miss and still displaces a stale hit (flood
            # protection intact).
            # B01.4 — likewise any tick the model refused (ineligible,
            # off-map, missing actual): no measurement is not a clean one.
            verdict: bool | None = (
                behavior.fired if residual.mode_match and residual.reason is None else None
            )
            # 6.3/3.1: a tuner override for (behavior, mode) fully owns
            # persistence for this key instead of stacking — see
            # `gate_and_collect`, shared with the IF + sequence detectors.
            sustained = gate_and_collect(
                persistence,
                f"behavior:{residual.name}",
                verdict,
                tuner=tuner,
                source="behavior",
                mode=residual.operating_mode,
                now=ctx.now.timestamp(),
                counters=self._components.emitter.counters,
            )
            if verdict is not True or not sustained:
                continue
            sources.append(behavior)
            ev_ts = ctx.timestamp_str or residual.timestamp
            if ev_ts is None:
                continue
            _publish_score(
                self._components.scores_broadcaster,
                source="behavior",
                score=behavior.severity,
                threshold=warn_severity,
                operating_mode=residual.operating_mode,
                model_version=f"behavior/{residual.name}",
                timestamp=ev_ts,
                missing_features=False,
            )
            self._components.emitter.emit_behavior(
                model=residual.name,
                kind=residual.kind,
                channel=residual.channel,
                timestamp=ev_ts,
                severity_band=residual.severity,
                score=behavior.severity,
                threshold=warn_severity,
                actual=residual.actual,
                expected=residual.expected,
                deviation=residual.deviation,
                operating_mode=residual.operating_mode,
                override_reason=residual.override_reason,
            )
        return sources

    def tick(self, row: Any, ctx: TickContext) -> list[FusionSource]:
        """Synchronous prepare → apply on the caller's thread."""
        return self.apply(self.prepare(row, ctx), ctx)


@dataclass(frozen=True)
class _BehaviorResidual:
    """One behavior/physics model's graded deviation for a tick (Phase 7.4).

    Carries everything the first-class behavior detector needs to emit an
    event *and* feed the fusion path, computed once off the same operating
    point the overlay uses."""

    name: str
    kind: str
    channel: str
    actual: float | None
    expected: float | None
    deviation: float | None
    severity: str
    mode_match: bool
    operating_mode: str | None
    timestamp: str | None
    #: 2.2 — the active accepted-baseline override's provenance reason when
    #: this tick's severity was graded against a recentered one-sided band
    #: instead of the model's own static config; ``None`` otherwise.
    override_reason: str | None = None
    #: B01.4 — why no deviation was produced this tick (``None`` when one
    #: was). A refused tick is a missing verdict, never a healthy miss.
    reason: str | None = None
    #: B01.6 — ``False`` for a diagnostic-only model (legacy artifact without
    #: a serving contract, or no held-out validation): still graded and
    #: shown, never emitted or fused.
    alerting: bool = True


def _observe_behavior(
    source: LiveFeatureSource, behavior_live: BehaviorLiveState
) -> list[_BehaviorResidual]:
    """Feed one tick of per-model deviations into the live overlay state.

    Returns one :class:`_BehaviorResidual` per model so the caller can both
    emit the first-class behavior detector event (Phase 7.4) and feed the
    fusion path (Phase 7.2) off the exact graded deviation the overlay shows
    — ``deviation()`` already returns ``"ok"`` for an off-mode or no-signal
    model, so a returned band is mode-safe.

    Reads the exact wide row the feature pipeline used (the source's
    ``latest_operating_point``) and runs every behavior model through the
    same ``behavior_decoder.deviation`` the preprocessing path uses. The
    Models▸Behavior overlay therefore shows backend truth — mode-gated,
    domain-clamped, empty-cell aware — not a client-side re-derivation that
    could silently drift from what the IF/LSTM actually see.

    Also grades any ``channel``-targeted accepted-baseline override (2.2) —
    unlike a ``behavior_model``-targeted one, there is no surface backing a
    raw channel, so these never appear in ``source.behavior_models`` and
    need their own residual straight off ``deviation_for_override``; see
    the loop below. Doesn't touch ``observations``/the live overlay — that
    panel is surface-model-shaped (x/y axis position) and a raw channel
    has no axis to place a "you are here" marker at.

    Best-effort: the caller wraps this in the live-inference try/except, so
    one malformed row can't kill the inference stream.
    """
    models = source.behavior_models
    point = source.latest_operating_point
    if point is None:
        return []
    raw_mode = point.get(MODE_COLUMN)
    mode = raw_mode if isinstance(raw_mode, str) else None
    ts = point.get("timestamp")
    if ts is None:
        timestamp = None
    else:
        try:
            timestamp = format_timestamp(ts)
        except (ValueError, TypeError):
            timestamp = str(ts)
    observations, residuals = _grade_surface_models(source, point, mode, timestamp)
    residuals.extend(_grade_channel_overrides(source, point, mode, timestamp))
    # Only touch the overlay when there were surface models to report on —
    # a channel-override-only tick (no behavior_models.yaml models at all)
    # has nothing overlay-shaped to add, same no-op posture as before 2.2's
    # channel-override branch existed.
    if models is not None and models.models:
        behavior_live.observe(timestamp, mode, observations)
    return residuals


def _grade_surface_models(
    source: LiveFeatureSource,
    point: Mapping[str, Any],
    mode: str | None,
    timestamp: str | None,
) -> tuple[list[BehaviorObservation], list[_BehaviorResidual]]:
    """Grade every behavior model (surface) on this tick: the overlay's
    observations and one residual per model (R03.9: the first of
    ``_observe_behavior``'s two loops)."""
    models = source.behavior_models
    observations: list[BehaviorObservation] = []
    residuals: list[_BehaviorResidual] = []
    for model in models.models if models is not None else []:
        # Same actual-column resolution as append_deviation_columns: a
        # qualified source:channel output is verbatim; a bare CAN signal
        # name falls back to the legacy decoded:<NAME>.
        actual_col = resolve_output_column(model.output)
        status = serving_status(model)
        actual = finite_float(point.get(actual_col))
        # 2.2 — an active, non-expired override for this model recenters
        # the band (worse side trips, better side never alerts, per
        # `behavior_decoder._grade_directional`); `active_override_for`
        # already returns `None` past `review_by`, so an expired override
        # is silently inert here — the model grades against its own static
        # config (if any) exactly as if no override existed.
        override = (
            source.accepted_baselines.active_override_for(model.name, mode)
            if source.accepted_baselines is not None
            else None
        )
        result = deviation(model, actual, point, override=override)
        if isinstance(model, BehaviorModel1D):
            x = finite_float(point.get(model.axis.channel))
            y = None
        else:
            x = finite_float(point.get(model.axes[0].channel))
            y = finite_float(point.get(model.axes[1].channel))
        mode_match = model.operating_mode is None or mode == model.operating_mode
        observations.append(
            BehaviorObservation(
                name=model.name,
                kind=model.kind,
                x=x,
                y=y,
                actual=actual,
                expected=result.expected,
                deviation=result.deviation,
                severity=result.severity,
                mode_match=mode_match,
                reason=result.reason,
                status=status.state,
                status_reasons=status.reasons,
                lifecycle=status.lifecycle,
                permitted_uses=tuple(sorted(use.value for use in status.permitted_uses)),
                artifact_version=model.version,
            )
        )
        residuals.append(
            _BehaviorResidual(
                name=model.name,
                kind=model.kind,
                channel=actual_col,
                actual=actual,
                expected=result.expected,
                deviation=result.deviation,
                severity=result.severity,
                mode_match=mode_match,
                operating_mode=mode,
                timestamp=timestamp,
                override_reason=None if override is None else override.provenance.reason,
                reason=result.reason,
                alerting=status.alerting,
            )
        )
    return observations, residuals


def _grade_channel_overrides(
    source: LiveFeatureSource,
    point: Mapping[str, Any],
    mode: str | None,
    timestamp: str | None,
) -> list[_BehaviorResidual]:
    """Grade each ``channel``-targeted accepted-baseline override (2.2) on
    this tick (R03.9: the second of ``_observe_behavior``'s two loops)."""
    residuals: list[_BehaviorResidual] = []
    # 2.2 — raw-channel overrides (e.g. `obd:SHORT_FUEL_TRIM_1`) have no
    # behavior_model/surface behind them, so they're never covered by the
    # loop above; grade each distinct channel target directly against
    # `deviation_for_override` instead of `deviation()`. Skips cleanly (no
    # residual) when no override is active for this tick's mode (expired,
    # or scoped to a different mode — `active_override_for` already
    # encodes both) or the channel isn't present/decoded this tick — same
    # "missing signal ⇒ no residual" posture as the behavior_model branch.
    baselines = source.accepted_baselines
    if baselines is not None:
        seen_channels: set[str] = set()
        for candidate in baselines.overrides:
            if candidate.channel is None or candidate.channel in seen_channels:
                continue
            seen_channels.add(candidate.channel)
            channel_override = baselines.active_override_for(candidate.channel, mode)
            if channel_override is None:
                continue
            actual_col = (
                candidate.channel
                if SOURCE_SEP in candidate.channel
                else channel_key(DECODED_SOURCE, candidate.channel)
            )
            actual = finite_float(point.get(actual_col))
            if actual is None:
                continue
            result = deviation_for_override(channel_override, actual)
            residuals.append(
                _BehaviorResidual(
                    name=f"override:{candidate.channel}",
                    kind="channel_override",
                    channel=actual_col,
                    actual=actual,
                    expected=result.expected,
                    deviation=result.deviation,
                    severity=result.severity,
                    # `active_override_for` already matched this tick's mode
                    # (or the override is unscoped) to get here, so this
                    # residual always speaks for the current tick.
                    mode_match=True,
                    operating_mode=mode,
                    timestamp=timestamp,
                    override_reason=channel_override.provenance.reason,
                )
            )
    return residuals
