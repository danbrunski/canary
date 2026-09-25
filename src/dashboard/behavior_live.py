"""Live behavior-model state for the dashboard (Models▸Behavior overlay).

The static surfaces served by ``GET /models/behavior`` answer "what does
normal look like?". This module answers "where is the engine *right now*
relative to those surfaces?" — the per-tick expected / actual / deviation /
severity the daemon already computes via :mod:`src.decode.behavior_decoder`,
plus a short rolling trail so the UI can draw a trajectory and a Δ sparkline.

Design mirrors :class:`src.daemon.health.HealthMonitor`: a single-writer
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

from src.decode.behavior_eligibility import OPERATING_MODE_MISMATCH

#: Rolling trail length. ~4 min at 1 Hz live-inference cadence (the default
#: ``1 / preprocessing.resample_hz``); higher resample rates shorten the
#: wall-clock window but keep the payload bounded. Plenty for the Curve
#: trail + the per-card Δ sparkline without bloating the 2 s poll.
TRAIL_MAXLEN = 240

#: ``not_scored`` for a row that has no deviation and no named reason — an
#: older grader, or a model with nothing to say. Never reported as ``ok``.
NO_DEVIATION = "no_deviation"


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
    #: ``"ok"`` | ``"warning"`` | ``"critical"`` for a scored tick, and
    #: ``None`` whenever the tick was not scored (B03.9): no deviation, or the
    #: model's operating mode is not the current one. The grader returns
    #: ``"ok"`` beside a missing deviation for API compatibility; that is
    #: normalised away here, at construction, so no serialiser can report an
    #: unscored row as healthy. :attr:`not_scored` says which kind of
    #: unscored it is.
    severity: str | None
    mode_match: bool  # model.operating_mode is None OR == current mode
    #: B01.4 — why the model produced no deviation this tick (``None`` when
    #: it did): an unmet eligibility condition, ``off_map``, or a
    #: missing/stale actual. Additive; older fields keep their meaning.
    reason: str | None = None
    #: B01.6 — ``"active"`` or ``"diagnostic_only"`` (see
    #: ``behavior_models.serving_status``) plus the named reasons.
    status: str = "active"
    status_reasons: tuple[str, ...] = ()
    #: B03.8 — ``validated`` | ``unvalidated`` | ``exploratory`` | ``demoted``
    #: | ``quarantined`` and the typed permitted uses (B03.2); the artifact
    #: identity and validation scope are on ``GET /models/behavior``.
    lifecycle: str = "validated"
    permitted_uses: tuple[str, ...] = ("alert", "display", "learned_feature")
    artifact_version: int | None = None

    def __post_init__(self) -> None:
        if self.not_scored is not None:
            object.__setattr__(self, "severity", None)

    @property
    def not_scored(self) -> str | None:
        """Which kind of not-scored this tick is, or ``None`` when it was scored.

        The category of the tick's own reason with its detail stripped
        (``off_map``, ``actual_stale``, ``actual_missing``,
        ``operating_mode_excluded``, ``operating_mode_mismatch``,
        ``rate_exceeded``, ...); ``reason`` keeps the detail. A row without a
        deviation and without a named reason is ``no_deviation`` rather than
        a silent healthy one.
        """
        if not self.mode_match:
            return OPERATING_MODE_MISMATCH
        if self.reason is not None:
            return self.reason.split(":", 1)[0]
        if self.deviation is None:
            return NO_DEVIATION
        return None

    @property
    def abstain_reason(self) -> str | None:
        """Why this tick raised no trusted verdict, or ``None`` if it could have.

        The tick's own no-prediction reason (ineligible, off-map, missing
        actual, out of mode) comes first; otherwise a model that may not
        alert abstains because of its lifecycle.
        """
        if not self.mode_match:
            return OPERATING_MODE_MISMATCH
        if self.reason is not None:
            return self.reason
        if "alert" not in self.permitted_uses:
            return f"not_alerting:{self.lifecycle}"
        return None


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
                    "not_scored": obs.not_scored,
                    "reason": obs.reason,
                }
            )

    def snapshot(self, *, include_series: bool = True) -> dict[str, Any]:
        """JSON-safe live block for ``GET /models/behavior/live``.

        ``include_series=False`` drops the rolling trail (the bulk of the
        payload — up to ``TRAIL_MAXLEN`` points per model) and returns just
        the latest actual/expected/deviation/severity per model. The FE's
        Curve trail needs the series; a one-shot status check (dashboard
        summary tiles, MCP tool callers) doesn't and would otherwise pull the
        whole trail on every poll for no reason — this is what made
        ``get_behavior_live`` overflow a 275 KB MCP tool-result cap in
        practice with enough models registered.
        """
        models: dict[str, Any] = {}
        for name, obs in self._latest.items():
            entry: dict[str, Any] = {
                "kind": obs.kind,
                "x": obs.x,
                "y": obs.y,
                "actual": obs.actual,
                "expected": obs.expected,
                "deviation": obs.deviation,
                "severity": obs.severity,
                "not_scored": obs.not_scored,
                "mode_match": obs.mode_match,
                "reason": obs.reason,
                "status": obs.status,
                "status_reasons": list(obs.status_reasons),
                "lifecycle": obs.lifecycle,
                "permitted_uses": list(obs.permitted_uses),
                "artifact_version": obs.artifact_version,
                "abstain_reason": obs.abstain_reason,
            }
            if include_series:
                entry["series"] = list(self._trail.get(name, ()))
            models[name] = entry
        return {
            "observed": self._observed,
            "timestamp": self._timestamp,
            "operating_mode": self._operating_mode,
            "models": models,
        }
