"""The plausibility evaluator in the live daemon (A05.9).

Two hooks, both on the event loop:

* **samples** — :attr:`LiveFeatureSource.sample_observer` hands every arrived
  value to the evaluator *before* the quality gate (so a spike the gate drops
  is still seen), timed into :class:`~src.plausibility.evaluator.ObserveTimer`;
* **ticks** — :meth:`LivePlausibility.tick` runs the freshness check with the
  tick's operating mode, then records the tick's whole plausibility cost
  (freshness + every sample observed since the last tick) as the
  ``plausibility`` stage of ``/status`` ``detectors.inference.stages`` and
  publishes the evaluator's block as ``detectors.inference.plausibility``.

The evaluator restarts whenever the session changes — a new recording
directory (which is also how a capture starts), or a replay starting or
stopping — carrying the previous drive's pending records of
``across_drives`` checks. Replay evidence is written beside the replay's
anomaly log, never into the live file.

Off unless ``plausibility.enabled``; with it on but no ``plausibility.yaml``
for the vehicle, nothing runs and ``/status`` names why (withheld, not quiet).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from src.plausibility.evaluator import (
    EVIDENCE_FILENAME,
    EvidenceStore,
    ObserveTimer,
    PlausibilityEvaluator,
)

if TYPE_CHECKING:
    from src.daemon.components import DaemonComponents
    from src.daemon.inference_metrics import InferenceTimingRecorder
    from src.plausibility.limits import LimitsCatalog
    from src.preprocessing.live import LiveFeatureSource

log = logging.getLogger(__name__)

STAGE_NAME = "plausibility"


def evidence_path(anomalies_dir: Path, replay_id: str | None) -> Path:
    """Live evidence file, or the replay's isolated one."""
    if replay_id is not None:
        return anomalies_dir / "_replay" / f"{replay_id}.{EVIDENCE_FILENAME}"
    return anomalies_dir / EVIDENCE_FILENAME


def live_session_key(components: DaemonComponents) -> tuple[str, str | None]:
    """``(session, replay_id)`` the live evaluator is scoped to right now."""
    replay_id: str | None = None
    if components.replay_controller is not None:
        state = components.replay_controller.status()
        if state.running:
            replay_id = state.replay_id
    if replay_id is not None:
        return f"_replay-{replay_id}", replay_id
    writer = components.storage_writer
    if writer is not None:
        try:
            return writer.paths.root.name, None
        except Exception:  # noqa: BLE001 — a writer between sessions has no paths yet
            pass
    return "live", None


class LivePlausibility:
    """Evaluator + evidence store + cost accounting for the live tick."""

    def __init__(
        self,
        catalog: LimitsCatalog,
        *,
        anomalies_dir: Path,
        recorder: InferenceTimingRecorder | None,
        session: str = "live",
        replay_id: str | None = None,
    ) -> None:
        self._anomalies_dir = anomalies_dir
        self._recorder = recorder
        self._timer = ObserveTimer()
        self._replay_id = replay_id
        self._store = EvidenceStore(evidence_path(anomalies_dir, replay_id))
        self.evaluator = PlausibilityEvaluator(
            catalog,
            session=session,
            sink=self._store.append,
            carried=self._carried(session),
        )
        self._last_tick_seconds = 0.0
        self._last_observe_seconds = 0.0
        self._last_samples = 0
        self._max_tick_seconds = 0.0

    @classmethod
    def build(
        cls,
        components: DaemonComponents,
        source: LiveFeatureSource,
        recorder: InferenceTimingRecorder | None,
    ) -> LivePlausibility | None:
        """The live evaluator, or ``None`` when disabled or withheld."""
        config = components.config
        if not config.plausibility.enabled:
            return None
        from src.plausibility.limits import resolve_limits_for_config

        try:
            catalog = resolve_limits_for_config(
                config, bindings=source.bindings, did_map=source.did_map
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            # OSError covers a missing or unreadable file; ValueError covers
            # schema refusals (pydantic's ValidationError is one).
            log.warning("plausibility checks withheld: %s", exc)
            if recorder is not None:
                recorder.set_plausibility({"enabled": True, "withheld": str(exc)})
            return None
        session, replay_id = live_session_key(components)
        live = cls(
            catalog,
            anomalies_dir=config.daemon.anomalies_dir,
            recorder=recorder,
            session=session,
            replay_id=replay_id,
        )
        live.attach(source)
        return live

    def _carried(self, session: str) -> list[Any]:
        if self._replay_id is not None:
            return []  # a replay never confirms against, or feeds, real drives
        try:
            return self._store.carried_pending(session)
        except OSError:
            log.exception("could not read plausibility evidence for carried pendings")
            return []

    def attach(self, source: LiveFeatureSource) -> None:
        evaluator = self.evaluator

        def observe(channel: str, value: float, at: float) -> None:
            evaluator.observe(channel, value, at)

        timed = self._timer.timed(observe)

        def on_sample(channel: str, value: float, observed_at: datetime) -> None:
            try:
                timed(channel, value, observed_at.timestamp())
            except Exception:  # noqa: BLE001 — a check must never cost a sample
                log.exception("plausibility observe failed for %s", channel)

        source.sample_observer = on_sample

    def _rescope(self, session: str, replay_id: str | None) -> None:
        if replay_id != self._replay_id:
            self._replay_id = replay_id
            self._store = EvidenceStore(evidence_path(self._anomalies_dir, replay_id))
            self.evaluator.sink = self._store.append
        self.evaluator.reset(session, carried=self._carried(session))

    def tick(
        self,
        now: datetime,
        mode: str | None,
        *,
        session: str,
        replay_id: str | None = None,
    ) -> None:
        """Freshness at ``now`` in ``mode``; publish cost and state."""
        if session != self.evaluator.session:
            self._rescope(session, replay_id)
        self.evaluator.mode = mode
        started = time.perf_counter()
        self.evaluator.check_freshness(now.timestamp(), mode)
        tick_seconds = time.perf_counter() - started
        observe_seconds, samples = self._timer.take()
        total = tick_seconds + observe_seconds
        self._last_tick_seconds = total
        self._last_observe_seconds = observe_seconds
        self._last_samples = samples
        self._max_tick_seconds = max(self._max_tick_seconds, total)
        if self._recorder is not None:
            self._recorder.record_stage(STAGE_NAME, total)
            self._recorder.set_plausibility(self.snapshot())

    def tick_live(self, components: DaemonComponents, now: datetime, mode: str | None) -> None:
        """:meth:`tick`, scoped to the daemon's current session/replay."""
        session, replay_id = live_session_key(components)
        self.tick(now, mode, session=session, replay_id=replay_id)

    def snapshot(self) -> dict[str, Any]:
        block = self.evaluator.snapshot()
        block["enabled"] = True
        block["evidence_path"] = str(self._store.path)
        block["profile_path"] = self.evaluator.catalog.profile_path
        block["cost"] = {
            "last_tick_ms": round(self._last_tick_seconds * 1000.0, 3),
            "last_observe_ms": round(self._last_observe_seconds * 1000.0, 3),
            "last_tick_samples": self._last_samples,
            "max_tick_ms": round(self._max_tick_seconds * 1000.0, 3),
        }
        return block


__all__ = ["STAGE_NAME", "LivePlausibility", "evidence_path", "live_session_key"]
