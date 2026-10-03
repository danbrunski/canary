"""The sequence-autoencoder (LSTM|TCN) live detector and its engine helpers (R03.9)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.columns import is_flag_column
from src.daemon.components import DaemonComponents
from src.daemon.detectors.common import (
    TickContext,
    _log_schema_reconciliation_once,
    _PreparedDetector,
    _publish_score,
    _with_effective_role,
    gate_and_collect,
    log,
)
from src.daemon.inference_scheduling import GenerationToken
from src.models.fusion import (
    FusionSource,
    downgrade_unsustained,
    lstm_source,
)
from src.preprocessing.feature_pipeline import (
    reconcile_feature_frame_to_spec,
)


@dataclass(frozen=True)
class _SequenceComputeJob:
    """Loop-snapshotted inputs for one sequence forward pass (C08.4)."""

    engine: Any
    row: Any
    #: Clear the window before appending ``row`` (A01.3): the generation
    #: changed since the last job, so nothing before this row belongs with it.
    reset: bool = False


class SequenceDetector(_PreparedDetector[_SequenceComputeJob, Any]):
    """Sequence-autoencoder (LSTM|TCN, Task 2.1) per-tick scoring → per-source
    ``lstm_autoencoder`` event → sequence fusion source (Task 3.1).

    Backbone-agnostic: drives whichever ``components.sequence_model_type``
    selected. Owns the engine hot-swap-on-version-change bookkeeping that
    used to live as loop-local state in ``run_live_inference``.
    """

    def __init__(self, components: DaemonComponents) -> None:
        self._components = components
        self._manager = (
            components.model_managers.get(components.sequence_model_type)
            if components.sequence_model_type is not None
            else None
        )
        self._engine: Any = None
        self._engine_version: str | None = None
        #: Generation of the last job handed out; a different one on the next
        #: job means a replay, capture or model change happened in between.
        self._last_generation: GenerationToken | None = None
        #: Last-scored model version, read by ``run_live_inference`` for the
        #: fused event's ``lstm=...`` version tag.
        self.model_version: str | None = None

    def prepare(
        self, row: Any, ctx: TickContext, *, generation: GenerationToken | None = None
    ) -> _SequenceComputeJob | None:
        """Loop side (C08.4): version bookkeeping and the engine hot-swap.
        The engine is rebuilt *here*, on the loop, never while a worker
        pass over the old one could still be running (the pipeline only
        snapshots when no compute job is in flight).

        ``generation`` is the pipeline's token for this tick. When it differs
        from the last job's, the job asks for the window to be cleared first
        (A01.3) — in this process and in the scoring child alike — so a replay
        starting or stopping, a capture change or a model swap never shares a
        window with what came before."""
        loaded = self._manager.current if self._manager is not None else None
        # serving_version — see IsolationForestDetector.prepare.
        self.model_version = loaded.serving_version if loaded is not None else None
        if loaded is None or row is None or not ctx.scoring_allowed:
            return None
        if self._engine_version != self.model_version:
            self._engine = _build_sequence_engine(
                loaded, suppress_channels=_suppress_channels_for(self._components, loaded)
            )
            self._engine_version = self.model_version if self._engine is not None else None
            _publish_sequence_suppression(self._components, self._engine)
        if self._engine is None:
            return None
        reset = (
            generation is not None
            and self._last_generation is not None
            and generation != self._last_generation
        )
        if generation is not None:
            self._last_generation = generation
        return _SequenceComputeJob(engine=self._engine, row=row, reset=reset)

    @staticmethod
    def compute(job: _SequenceComputeJob) -> Any:
        """Worker side: window append + torch forward pass. The engine's
        window buffer is detector-owned and only ever advanced from here,
        one job at a time (C08.6 bounds in-flight jobs to one)."""
        if job.reset:
            job.engine.reset()
        return _sequence_observe(job.engine, job.row)

    def apply(self, result: Any, ctx: TickContext) -> list[FusionSource]:
        """Loop side: ``/ws/scores``, persistence, per-source emit, fusion
        source. ``result`` is ``None`` until the window is full."""
        if result is None:
            return []
        _publish_score(
            self._components.scores_broadcaster,
            source="lstm_autoencoder",
            score=result.global_score,
            threshold=result.global_threshold,
            operating_mode=ctx.operating_mode,
            model_version=result.model_version,
            timestamp=result.timestamp,
            missing_features=False,
        )
        # 6.3/3.1: a tuner override for (lstm_autoencoder, mode) fully owns
        # persistence for this key instead of stacking on the gate's own
        # M-of-N — see `gate_and_collect`, shared with the IF + behavior
        # detectors.
        sustained = gate_and_collect(
            self._components.persistence_gate,
            "lstm_autoencoder",
            result.is_anomaly,
            tuner=self._components.alert_tuner,
            source="lstm_autoencoder",
            mode=ctx.operating_mode,
            now=ctx.now.timestamp(),
            counters=self._components.emitter.counters,
        )
        if not (result.is_anomaly and not sustained):
            self._components.emitter.emit_lstm(result, operating_mode=ctx.operating_mode)
        return [
            _with_effective_role(
                self._components,
                downgrade_unsustained(
                    lstm_source(result.global_score, result.global_threshold), sustained
                ),
            )
        ]


def _build_sequence_engine(loaded: Any, *, suppress_channels: tuple[str, ...] = ()) -> Any:
    """Build a ``SequenceInferenceEngine`` for a freshly-swapped model, or
    ``None`` on a bad checkpoint (retried next tick). Backbone-agnostic: the
    engine drives whichever sequence detector the manager loaded.

    ``suppress_channels`` is forwarded so config-listed noise channels (fuel
    level, HVAC, etc.) get their mask zeroed at every window — keeps the
    runtime mask knob honoured from *every* engine-construction path.
    """
    try:
        from src.models.inference import SequenceInferenceEngine

        return SequenceInferenceEngine(loaded.artifact, suppress_channels=suppress_channels)
    except Exception:  # noqa: BLE001 — bad checkpoint shouldn't kill the task
        log.exception("failed to build SequenceInferenceEngine; will retry next tick")
        return None


def _publish_sequence_suppression(components: Any, engine: Any) -> None:
    """Put the engine's training-vs-serving suppress comparison on ``/status``
    (A01.5, ``health.sequence_suppression``). The parent builds its engine
    from the same artifact and config the scoring child does, so its
    comparison stands for both."""
    health = getattr(components, "health_monitor", None)
    if health is None:
        return
    comparison = getattr(engine, "suppression", None)
    health.observe_sequence_suppression(comparison.to_dict() if comparison is not None else None)


def _suppress_channels_for(components: Any, loaded: Any) -> tuple[str, ...]:
    """Pick the right backbone's ``suppress_channels`` for the loaded
    artifact's ``model_type``, so an LSTM↔TCN switch honours its own knob."""
    artifact_manifest = getattr(loaded.artifact, "manifest", None)
    model_type = getattr(artifact_manifest, "model_type", None)
    if model_type == "tcn_autoencoder":
        seq_cfg = components.config.models.tcn_autoencoder
    else:
        seq_cfg = components.config.models.lstm_autoencoder
    return tuple(getattr(seq_cfg, "suppress_channels", ()))


def _sequence_observe(engine: Any, row: Any) -> Any:
    """Align ``row`` to the sequence engine's expected feature columns and observe.

    ``SequenceWindowBuffer.append`` already handles missing-column / non-finite
    cases by clearing the buffer (return False, no result), so we don't need
    to pre-validate — we just present the columns the engine asks for. The
    return value is ``SequenceInferenceResult`` once a full window is ready,
    ``None`` otherwise.

    The GRU-D backbone needs each value column's ``<channel>__gap`` flag, and
    (A03.1) its ``<channel>__arrived`` flag, to derive the observed-mask.
    ``reconcile_feature_frame_to_spec`` only keeps structural + value columns
    (it's shared with the IF path), so re-attach the flag columns from the
    original live row after reconciling.
    """
    schema = _schema_for_engine(engine)
    aligned, report = reconcile_feature_frame_to_spec(row, schema)
    _log_schema_reconciliation_once(
        _engine_model_type(engine),
        _engine_version(engine),
        report,
    )
    observed = aligned.iloc[0].to_dict()
    # A06.2: the novelty screen reads its driving-condition columns from the
    # row whether or not the artifact takes them as inputs.
    conditions = set(getattr(engine, "condition_columns", ()) or ())
    for column in row.columns:
        name = str(column)
        if (is_flag_column(name) or name in conditions) and name not in observed:
            observed[name] = row.iloc[0][column]
    return engine.observe(observed)


def _schema_for_engine(engine: Any) -> dict[str, Any]:
    trained = getattr(engine, "trained", None)
    manifest = getattr(trained, "manifest", None)
    schema = getattr(manifest, "schema", None)
    if isinstance(schema, dict):
        return schema
    return {"feature_columns": list(getattr(engine, "feature_columns", ()))}


def _engine_version(engine: Any) -> str:
    trained = getattr(engine, "trained", None)
    manifest = getattr(trained, "manifest", None)
    version = getattr(manifest, "model_version", None) or getattr(engine, "model_version", None)
    return str(version or "unknown")


def _engine_model_type(engine: Any) -> str:
    """``model_type`` of the engine's loaded model, for log attribution. Falls
    back to a generic label for test doubles without a manifest."""
    trained = getattr(engine, "trained", None)
    manifest = getattr(trained, "manifest", None)
    return str(getattr(manifest, "model_type", None) or "sequence_autoencoder")
