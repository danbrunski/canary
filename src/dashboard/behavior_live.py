"""Live behavior-model state for the dashboard (Models▸Behavior overlay).

The static surfaces served by ``GET /models/behavior`` answer "what does
normal look like?". This module answers "where is the engine *right now*
relative to those surfaces?" — the per-tick expected / actual / deviation /
severity the daemon already computes via :mod:`src.decode.behavior_decoder`,
plus a short rolling trail so the UI can draw a trajectory and a Δ sparkline.

Design mirrors :class:`src.models.health.HealthMonitor`: a single-writer
in-memory accumulator (the daemon's live-inference task is the only writer,
exactly like the emitter / health monitor), bounded deques, and a JSON-safe
:meth:`snapshot` the FastAPI process reads on the same event loop. No locking
for the same reason ``HealthMonitor`` needs none — one writer, snapshot reads
are atomic dict/deque copies.

A bare ``GET /models/behavior/live`` poll (~2 s on the client) is enough: the
trail lives server-side in the deque, so a slow poll still returns a smooth
history instead of the sparse points a client-side sampler would collect.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: Rolling trail length. ~4 min at 1 Hz live-inference cadence (the default
#: ``1 / preprocessing.resample_hz``); higher resample rates shorten the
#: wall-clock window but keep the payload bounded. Plenty for the Curve
#: trail + the per-card Δ sparkline without bloating the 2 s poll.
TRAIL_MAXLEN = 240


@dataclass(frozen=True)
class BehaviorObservation:
    """One model's evaluation at a single live tick.

    ``x`` / ``y`` are the *input-axis* values (``y`` is ``None`` for 1D
    surfaces) so the UI can place the "you are here" marker; ``actual`` is
    the model's output channel. Any field is ``None`` when the model didn't
    speak for the point (outside domain, empty grid cell, mode mismatch,
    missing input) — the UI renders that as "off-map" rather than a marker.
    """

    name: str
    kind: str  # "surface_1d" | "surface_2d"
    x: float | None
    y: float | None
    actual: float | None
    expected: float | None
    deviation: float | None
    severity: str  # "ok" | "warning" | "critical"
    mode_match: bool  # model.operating_mode is None OR == current mode


class BehaviorLiveState:
    """Rolling per-model live deviation accumulator.

    Single-writer (the daemon inference task), like the emitter / health
    monitor. :meth:`observe` is called once per live-inference tick with the
    full set of model evaluations; :meth:`snapshot` is the JSON the
    ``/models/behavior/live`` endpoint serves.
    """

    def __init__(self, trail_maxlen: int = TRAIL_MAXLEN) -> None:
        self._trail_maxlen = trail_maxlen
        self._observed = 0
        self._timestamp: str | None = None
        self._operating_mode: str | None = None
        self._latest: dict[str, BehaviorObservation] = {}
        self._trail: dict[str, deque[dict[str, Any]]] = {}

    def observe(
        self,
        timestamp: str | None,
        operating_mode: str | None,
        observations: Iterable[BehaviorObservation],
    ) -> None:
        self._observed += 1
        self._timestamp = timestamp
        self._operating_mode = operating_mode
        for obs in observations:
            self._latest[obs.name] = obs
            trail = self._trail.get(obs.name)
            if trail is None:
                trail = deque(maxlen=self._trail_maxlen)
                self._trail[obs.name] = trail
            trail.append(
                {
                    "t": timestamp,
                    "x": obs.x,
                    "y": obs.y,
                    "actual": obs.actual,
                    "expected": obs.expected,
                    "deviation": obs.deviation,
                    "severity": obs.severity,
                }
            )

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe live block for ``GET /models/behavior/live``."""
        models: dict[str, Any] = {}
        for name, obs in self._latest.items():
            models[name] = {
                "kind": obs.kind,
                "x": obs.x,
                "y": obs.y,
                "actual": obs.actual,
                "expected": obs.expected,
                "deviation": obs.deviation,
                "severity": obs.severity,
                "mode_match": obs.mode_match,
                "series": list(self._trail.get(name, ())),
            }
        return {
            "observed": self._observed,
            "timestamp": self._timestamp,
            "operating_mode": self._operating_mode,
            "models": models,
        }
