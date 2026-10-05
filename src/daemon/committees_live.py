"""Sensor committees on the live tick (H04.2).

Each inference tick hands the feature source's newest operating point — the
raw-channel row ``featurize_wide_frame`` produced, member transforms and
``__arrived`` flags included — to one
:class:`~src.signals.committees.CommitteeEvaluator`. A sustained departure
becomes an anomaly event through the emitter (``source: committee``), so
tuning, dedupe, detector roles, the anomaly log and the push all apply, and a
replay's events land in the replay's log (the logger's replay mode). The
per-committee state is published as ``/status``
``detectors.inference.committees``.

**Not gated by the capture profile (R03.12).** Operator decision 2026-09-24
keeps tuning captures out of the Isolation Forest and the sequence detector
because both score against a *learned* normal that a different polling set
invalidates. A committee learns nothing: it compares two instruments against
a relation measured once, per vehicle. Like the plausibility checks it keeps
running under every profile — and the speed committee has a broadcast member
precisely because ``tuning`` does not poll the speed PID.

**Not gated by vehicle state either.** Its own conditions (moving at ≥ 20 km/h
on a good fix, for the speed committee) are stricter than ``engine_running``.

The evaluator restarts on a session change (a new recording directory, a
replay starting or stopping), so a run never spans two drives. A vehicle with
no ``committees.yaml`` runs every committee as ``not_configured``; a malformed
file withholds the evaluator and ``/status`` says why.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml

from src.signals.committees import (
    IMU_ACCEL_LON_MEAN,
    CommitteeEvaluator,
    CommitteeFlag,
    physical_committee_point,
    resolve_committee_profile,
)

if TYPE_CHECKING:
    from src.daemon.components import DaemonComponents
    from src.daemon.inference_metrics import InferenceTimingRecorder
    from src.decode.bindings import Bindings
    from src.features.live import LiveFeatureSource

log = logging.getLogger(__name__)


def _session(components: DaemonComponents) -> str:
    # Lazy: plausibility_live pulls the plausibility evaluator, which a daemon
    # with those checks off never loads (A05.9).
    from src.daemon.plausibility_live import live_session_key

    session, _replay_id = live_session_key(components)
    return session


def _alert_sink(components: DaemonComponents) -> Callable[[CommitteeFlag], object] | None:
    """The emitter's ``emit_committee``; ``None`` for a stand-in without one."""
    emitter = getattr(components, "emitter", None)
    return None if emitter is None else emitter.emit_committee


class LiveCommittees:
    """Evaluator + session scoping + ``/status`` publication for the live tick."""

    def __init__(
        self,
        evaluator: CommitteeEvaluator,
        *,
        bindings: Bindings | None,
        recorder: InferenceTimingRecorder | None,
        alert_sink: Callable[[CommitteeFlag], object] | None,
        producer_status: Callable[[datetime], dict[str, str]] | None = None,
    ) -> None:
        self.evaluator = evaluator
        #: What each producer says right now (H04.4): the live IMU frame's
        #: step, so a missing IMU member names *why* in its verdict and in
        #: ``/status`` (``producer_status``).
        self._producer_status = producer_status
        self._bindings = bindings
        self._recorder = recorder
        self._alert_sink = alert_sink
        self.flags_emitted = 0

    @classmethod
    def build(
        cls,
        components: DaemonComponents,
        source: LiveFeatureSource,
        recorder: InferenceTimingRecorder | None,
    ) -> LiveCommittees | None:
        """The live evaluator, or ``None`` when the profile file is malformed."""
        try:
            profile, path = resolve_committee_profile(components.config)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            log.warning("sensor committees withheld: %s", exc)
            if recorder is not None:
                recorder.set_committees({"withheld": str(exc)})
            return None
        live = cls(
            CommitteeEvaluator(profile, profile_path=path, session=_session(components)),
            bindings=source.bindings,
            recorder=recorder,
            alert_sink=_alert_sink(components),
            producer_status=lambda now: {
                IMU_ACCEL_LON_MEAN: source.imu_frame.lon_mean_absence(now)
            },
        )
        live.publish()
        return live

    def tick(
        self,
        point: dict[str, Any] | None,
        now: datetime,
        mode: str | None,
        *,
        session: str,
    ) -> list[CommitteeFlag]:
        """Evaluate ``point`` (the tick's operating point) at ``now``."""
        if session != self.evaluator.session:
            self.evaluator.reset(session)
        flags: list[CommitteeFlag] = []
        if point is not None:
            self.evaluator.mode = mode if mode is not None else _point_mode(point)
            physical = physical_committee_point(point, self._bindings)
            # The row's own time, not the wall clock: an operating point left
            # over from a discarded tick carries its old stamp, and the
            # evaluator ignores a stamp it has already seen.
            at = _point_time(point, now)
            if self._producer_status is not None:
                self.evaluator.producer_status = self._producer_status(
                    datetime.fromtimestamp(at, tz=UTC)
                )
            flags = self.evaluator.observe(physical, at)
        for flag in flags:
            log.warning("sensor committee disagreement: %s", flag.text)
            if self._alert_sink is not None:
                self._alert_sink(flag)
            self.flags_emitted += 1
        self.publish()
        return flags

    def tick_live(
        self,
        components: DaemonComponents,
        point: dict[str, Any] | None,
        now: datetime,
        mode: str | None,
    ) -> list[CommitteeFlag]:
        """:meth:`tick`, scoped to the daemon's current session/replay."""
        return self.tick(point, now, mode, session=_session(components))

    def publish(self) -> None:
        if self._recorder is not None:
            self._recorder.set_committees(self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        block = self.evaluator.snapshot()
        block["enabled"] = True
        block["flags_emitted"] = self.flags_emitted
        block["capture_profile_gated"] = False
        return block


def _point_time(point: dict[str, Any], fallback: datetime) -> float:
    stamp = point.get("timestamp")
    if isinstance(stamp, datetime):
        return stamp.timestamp()
    return fallback.timestamp()


def _point_mode(point: dict[str, Any]) -> str | None:
    mode = point.get("operating_mode")
    return mode if isinstance(mode, str) else None


def build_live_committees(
    components: DaemonComponents,
    source: LiveFeatureSource,
    recorder: InferenceTimingRecorder,
) -> LiveCommittees | None:
    """:meth:`LiveCommittees.build`, never raising into the inference loop.

    ``None`` when ``committees.enabled`` is off (``/status`` says so).
    """
    if not components.config.committees.enabled:
        recorder.set_committees({"enabled": False})
        return None
    try:
        return LiveCommittees.build(components, source, recorder)
    except Exception as exc:  # noqa: BLE001 — a broken committee must not stop scoring
        log.exception("sensor committee evaluator failed to start; committees are off")
        recorder.set_committees({"withheld": f"evaluator failed to start: {exc!r}"})
        return None


__all__ = ["LiveCommittees", "build_live_committees"]
