"""The profile-capture lifecycle, out of the route closures (R03.5).

A profile capture (``POST /control/capture``) is one transition across five
owners: the labelled capture window, the Parquet writer's session, the bus
budget, the active polling profile and the loss-counter epoch. Until R03.5 the
state that ties them together lived as seven ``nonlocal`` variables inside
``register_control_routes``, so the only code able to start or stop a capture
was a route handler: the autostart loop called handlers with ``_auth=None``,
and daemon shutdown re-implemented half of the stop without the in-memory
epoch — a capture ended by shutdown reported ``loss_epoch_start: None``.

:class:`ProfileCaptureSession` owns that state. Routes, the autostart loop and
daemon shutdown all call it. It raises :class:`ProfileCaptureError` carrying
the HTTP status and detail the routes have always answered with, so the
routes stay thin and their contract is unchanged. Control events are
published by the callers (route or autostart), never from here.

:class:`CaptureAutostart` owns the autostart decider and its tick task.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from src.collection import bus_budget, tuning_profile
from src.collection.capture_profiles import CaptureProfileStore
from src.control.capture import CaptureController, CaptureState
from src.control.capture_autostart import CaptureAutostartDecider, CaptureAutostartRule
from src.control.capture_finalize import (
    StartEpoch,
    finalize_capture,
    live_capture_quality,
    record_capture_start,
)
from src.control.capture_manifest import read_capture_manifest, stamp_capture_end
from src.models.health import HealthMonitor
from src.session import format_session_timestamp
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.vehicle_state import ReplayStatusSource, derive_from_live_source

log = logging.getLogger(__name__)

#: How often the capture-autostart tick classifies the vehicle. Its inputs
#: are in-memory, so the tick is cheap; the debounces live in the decider.
AUTOSTART_TICK_SECONDS = 2.0

#: Why a profile capture ended — the manifest's ``end_reason`` values a
#: running process can stamp (``interrupted`` is stamped at recovery).
StopReason = Literal["operator_stop", "daemon_shutdown"]

#: Publishes one control event: ``(action, result, payload)``.
EventPublisher = Callable[[str, str, dict[str, Any]], None]


class ProfileCaptureError(Exception):
    """A refused or failed transition. ``status`` is the HTTP code the route
    answers with; ``detail`` its body (a string or a structured dict)."""

    def __init__(self, status: int, detail: str | dict[str, Any]) -> None:
        super().__init__(detail if isinstance(detail, str) else str(detail))
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class ProfileCaptureStart:
    """What starting a profile capture takes; the route's request body, minus
    FastAPI (this layer must not import the dashboard)."""

    profile: str
    tune_revision: str | None = None
    rate_hz: float | None = None
    bus_budget_requests_per_second: float | None = None
    bus_budget_ttl_seconds: float | None = None


class ProfileCaptureSession:
    """Start, stop, pause and score the one profile capture the daemon runs.

    ``storage_writer`` is required to start or stop (a 503 otherwise, as the
    routes always answered); ``capture_controller`` and ``health_monitor``
    are optional, exactly as they are to the routes.
    """

    def __init__(
        self,
        *,
        storage_writer: AsyncParquetTelemetryWriter | None,
        capture_controller: CaptureController | None,
        profile_store: CaptureProfileStore,
        health_monitor: HealthMonitor | None,
    ) -> None:
        self.storage_writer = storage_writer
        self.capture_controller = capture_controller
        self.profile_store = profile_store
        self.health_monitor = health_monitor
        #: Bus-budget override in force before a capture raised it, so
        #: stopping restores the operator's setting instead of clearing it.
        self._budget_before_capture: dict[str, Any] | None = None
        #: The immediately preceding stop response makes a lost HTTP response
        #: recoverable: retrying disarm must not clear a budget that was
        #: already restored. In memory by design; after a restart the durable
        #: capture manifest identifies the completed session.
        self._last_ended_capture: dict[str, Any] | None = None
        #: A labelled window (e.g. ``protocol: drive``) that a profile capture
        #: closed in order to open its own, reopened on the new session when
        #: the capture stops or fails to start (2026-09-22: an open drive
        #: window made every capture start a bare 500 and cost two pulls).
        self._superseded_window: CaptureState | None = None
        #: Loss-counter epoch and generation recorded when the active capture
        #: armed (C03.5); the stop reconciles against it. ``capture_epoch.json``
        #: is the durable copy.
        self._start_epoch: StartEpoch | None = None
        self._lock = asyncio.Lock()

    # -- queries -----------------------------------------------------------

    @property
    def start_epoch(self) -> StartEpoch | None:
        return self._start_epoch

    def writer_session_dir(self) -> Path | None:
        """The directory the writer is filling right now, if any."""
        if self.storage_writer is None:
            return None
        return Path(self.storage_writer.paths.root)

    def is_writer_session(self, session_id: str) -> bool:
        """Whether ``session_id`` names the directory being written (R03.3)."""
        current = self.writer_session_dir()
        return current is not None and current.name == session_id

    def _require_writer(self) -> AsyncParquetTelemetryWriter:
        if self.storage_writer is None:
            raise ProfileCaptureError(503, "capture storage writer is not available")
        return self.storage_writer

    def _reopen_superseded_window(self) -> dict[str, Any] | None:
        window, self._superseded_window = self._superseded_window, None
        if window is None or self.capture_controller is None:
            return None
        reopened = self.capture_controller.start_capture(
            purpose=window.purpose,
            protocol=window.protocol,
            vehicle_state=window.vehicle_state,
            fields={"resumes_capture_id": window.capture_id},
        )
        return reopened.to_dict()

    # -- transitions -------------------------------------------------------

    async def start(self, request: ProfileCaptureStart) -> dict[str, Any]:
        """Rotate into the profile's capture session; roll back on any failure."""
        storage_writer = self._require_writer()
        profile = self.profile_store.load().by_name(request.profile)
        if profile is None:
            raise ProfileCaptureError(404, f"capture profile not found: {request.profile}")
        if profile.name == "default":
            raise ProfileCaptureError(409, "default collection is already active")
        # `requires_tune_revision` is advisory, by operator decision 2026-08-10:
        # a missing revision must never block a capture. The information is not
        # lost — the manifest records `tune_revision: null` and the digest
        # reports the revision source as `unknown` — so an analysis across a
        # flash boundary can still tell that the calibration was unrecorded.
        # It is surfaced as a hint in the UI, not as a gate here.
        if request.rate_hz is not None:
            profile = profile.model_copy(update={"target_rate_hz": request.rate_hz})
        async with self._lock:
            if tuning_profile.is_active():
                active = tuning_profile.active_profile()
                manifest = read_capture_manifest(storage_writer.paths.root)
                same_capture = (
                    active.name == profile.name
                    and active.target_rate_hz == profile.target_rate_hz
                    and manifest is not None
                    and manifest.tune_revision == request.tune_revision
                )
                if same_capture:
                    return {
                        "capture_profile": tuning_profile.snapshot(),
                        "output_session_dir": str(storage_writer.paths.root),
                        "bus_budget": bus_budget.snapshot(),
                        "idempotent": True,
                    }
                raise ProfileCaptureError(409, "a different capture profile is already active")
            capture_id = format_session_timestamp(datetime.now(UTC))
            requested_budget = request.bus_budget_requests_per_second
            if requested_budget is None:
                requested_budget = profile.bus_budget_override
            previous_budget = bus_budget.snapshot()
            previous_profile = tuning_profile.active_profile()
            previous_writer_dir = storage_writer.paths.root
            entered = False
            failed_step = "capture_window"
            controller = self.capture_controller
            try:
                open_window = controller.current_capture() if controller is not None else None
                if open_window is not None:
                    assert controller is not None
                    controller.stop_capture(
                        reason="superseded_by_capture_profile",
                        fields={"capture_profile": profile.name},
                    )
                    self._superseded_window = open_window
                failed_step = "storage_rotation"
                await storage_writer.enter_capture_mode(
                    profile,
                    capture_id,
                    request.tune_revision,
                    self.profile_store.resolved_plan(profile),
                )
                entered = True
                if requested_budget is not None:
                    failed_step = "bus_budget"
                    bus_budget.set_override(
                        requested_budget,
                        source=f"capture:{profile.name}",
                        ttl_seconds=request.bus_budget_ttl_seconds,
                    )
                failed_step = "profile_activation"
                tuning_profile.activate(profile, session_dir=str(storage_writer.paths.root))
                failed_step = "capture_window_start"
                if controller is not None:
                    controller.start_capture(
                        purpose=profile.name,
                        protocol="capture_profile",
                        fields={"tune_revision": request.tune_revision},
                    )
                self._last_ended_capture = None
                self._start_epoch = record_capture_start(
                    Path(storage_writer.paths.root), self.health_monitor
                )
                self._budget_before_capture = (
                    {**previous_budget, "_capture_started_monotonic": time.monotonic()}
                    if requested_budget is not None
                    else None
                )
            except Exception as exc:
                rollback_errors: list[str] = []
                # A writer can rotate its path before a later manifest write
                # fails, so inspect its actual path as well as the success flag.
                if entered or storage_writer.paths.root != previous_writer_dir:
                    try:
                        await storage_writer.exit_capture_mode()
                    except Exception as rollback_exc:  # noqa: BLE001
                        rollback_errors.append(f"writer: {rollback_exc}")
                try:
                    bus_budget.restore(previous_budget)
                except Exception as rollback_exc:  # noqa: BLE001
                    rollback_errors.append(f"bus_budget: {rollback_exc}")
                try:
                    tuning_profile.activate(
                        previous_profile,
                        session_dir=str(storage_writer.paths.root),
                        source="rollback",
                    )
                except Exception as rollback_exc:  # noqa: BLE001
                    rollback_errors.append(f"profile: {rollback_exc}")
                if controller is not None and self._superseded_window is not None:
                    try:
                        if controller.current_capture() is not None:
                            controller.stop_capture(reason="capture_start_failed")
                        self._reopen_superseded_window()
                    except Exception as rollback_exc:  # noqa: BLE001
                        rollback_errors.append(f"capture_window: {rollback_exc}")
                detail: dict[str, Any] = {
                    "error": "capture_start_failed",
                    "failed_step": failed_step,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "restored": {
                        "writer_session_dir": str(storage_writer.paths.root),
                        "profile": tuning_profile.snapshot(),
                        "bus_budget": bus_budget.snapshot(),
                    },
                }
                if rollback_errors:
                    detail["rollback_errors"] = rollback_errors
                raise ProfileCaptureError(500, detail) from exc
            return {
                "capture_profile": tuning_profile.snapshot(),
                "output_session_dir": str(storage_writer.paths.root),
                "bus_budget": bus_budget.snapshot(),
                "superseded_capture_window": (
                    self._superseded_window.to_dict()
                    if self._superseded_window is not None
                    else None
                ),
            }

    async def stop(self, reason: StopReason = "operator_stop") -> dict[str, Any]:
        """End the capture: judge it, stamp it, and return to ``default``.

        ``operator_stop`` is the full stop the routes and autostart use.
        ``daemon_shutdown`` ends the capture the same way up to the stamp —
        boundary, drain, flush, judge against the in-memory start epoch (so
        ``loss_epoch_start`` is reported, which the shutdown path's own copy
        of this sequence could not do), stamp — and then leaves the capture
        window, the writer and the profile where they are, as shutdown always
        did: the process is exiting, and rotating would leave an empty live
        session behind and move the trip-end target off the capture.
        """
        storage_writer = self._require_writer()
        async with self._lock:
            if not tuning_profile.is_active():
                if self._last_ended_capture is not None:
                    return {**self._last_ended_capture, "idempotent": True}
                raise ProfileCaptureError(409, "default collection is already active")
            ended_capture_dir = str(storage_writer.paths.root)
            # Boundary, drain, flush, judge, write — *before* the writer
            # rotates away and the profile resets the lifetime ledger (C03.8):
            # after exit_capture_mode `paths.root` no longer names the session
            # this data belongs to, and after activate(default) the ledger is
            # gone.
            quality = await finalize_capture(
                Path(ended_capture_dir),
                health=self.health_monitor,
                writer=storage_writer,
                start=self._start_epoch,
            )
            stamp_capture_end(Path(ended_capture_dir), reason)
            self._start_epoch = None
            if reason == "daemon_shutdown":
                return {
                    "capture_profile": tuning_profile.snapshot(),
                    "ended_capture_dir": ended_capture_dir,
                    "capture_quality": quality,
                }
            controller = self.capture_controller
            if controller is not None and controller.current_capture() is not None:
                controller.stop_capture(reason=reason)
            await storage_writer.exit_capture_mode()
            default_profile = self.profile_store.load().by_name("default")
            assert default_profile is not None
            tuning_profile.activate(default_profile, session_dir=str(storage_writer.paths.root))
            if self._budget_before_capture is not None:
                restored_budget = dict(self._budget_before_capture)
                remaining = restored_budget.get("ttl_remaining_seconds")
                started_at = restored_budget.pop("_capture_started_monotonic", None)
                if remaining is not None and started_at is not None:
                    restored_budget["ttl_remaining_seconds"] = max(
                        0.0, float(remaining) - (time.monotonic() - float(started_at))
                    )
                    if restored_budget["ttl_remaining_seconds"] == 0.0:
                        restored_budget["override_requests_per_second"] = None
                bus_budget.restore(restored_budget)
            self._budget_before_capture = None
            payload = {
                "capture_profile": tuning_profile.snapshot(),
                "ended_capture_dir": ended_capture_dir,
                "bus_budget": bus_budget.snapshot(),
                "capture_quality": quality,
                "resumed_capture_window": self._reopen_superseded_window(),
            }
            self._last_ended_capture = dict(payload)
            return payload

    def pause(self, paused: bool) -> dict[str, Any]:
        """Suspend or resume polling while keeping the capture session open.

        Rows already written stay on disk — the writer flushes on its own
        timer, so pausing never risks the log so far.
        """
        if not tuning_profile.is_active():
            raise ProfileCaptureError(409, "no capture is active")
        tuning_profile.set_paused(paused)
        return {"capture_profile": tuning_profile.snapshot()}

    async def live_quality(self) -> Any:
        """The active capture scored on what has been persisted so far.

        Raises ``CaptureQualityUnavailableError`` (the route's 503) when the
        rows cannot be scored.
        """
        if self.storage_writer is None or self.health_monitor is None:
            raise ProfileCaptureError(503, "capture quality is not available")
        if not tuning_profile.is_active():
            raise ProfileCaptureError(409, "no capture is active")
        return await live_capture_quality(
            Path(self.storage_writer.paths.root),
            health=self.health_monitor,
            writer=self.storage_writer,
            start=self._start_epoch,
        )


class CaptureAutostart:
    """The armed autostart rule and the task that acts on it.

    The rule lives in memory by design: a restart ends the capture it
    started, and re-arming is one request — a rule that outlived the daemon
    would start captures nobody asked for.

    The tick starts and stops through the same :class:`ProfileCaptureSession`
    an operator's request uses, so a capture the daemon starts is identical to
    one started by hand (quality sidecar, budget override, the drive window
    it supersedes). ``tick_seconds`` is read on every tick, so a test can
    shorten it.
    """

    def __init__(
        self,
        session: ProfileCaptureSession,
        live_source: Any,
        replay_controller: ReplayStatusSource | None,
        *,
        tick_seconds: Callable[[], float],
    ) -> None:
        self.session = session
        self.live_source = live_source
        self.replay_controller = replay_controller
        self._tick_seconds = tick_seconds
        self.decider: CaptureAutostartDecider | None = None
        self._task: asyncio.Task[None] | None = None
        self._publish: EventPublisher | None = None

    @property
    def can_fire(self) -> bool:
        return self.live_source is not None

    def snapshot(self) -> dict[str, Any]:
        decider = self.decider
        if decider is None:
            return {"armed": False}
        return {
            "armed": True,
            "rule": decider.rule.to_dict(),
            "reason": decider.reason,
            "capture_active": tuning_profile.is_active(),
            "can_fire": self.can_fire,
        }

    def arm(self, rule: CaptureAutostartRule, *, publish: EventPublisher | None) -> dict[str, Any]:
        """Arm ``rule`` and make sure the tick is running. Starts nothing."""
        if self.session.profile_store.load().by_name(rule.profile) is None:
            raise ProfileCaptureError(404, f"unknown profile: {rule.profile}")
        if not self.can_fire:
            raise ProfileCaptureError(
                503,
                "no live telemetry source, so the daemon cannot classify the "
                "vehicle; start the capture directly with POST /control/capture",
            )
        self.decider = CaptureAutostartDecider(rule=rule)
        self._publish = publish
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())
        return self.snapshot()

    def disarm(self) -> dict[str, Any]:
        """Disarm. A capture already running is left running, by design:
        disarming is "stop deciding for me", not "throw away my data"."""
        was_armed = self.decider is not None
        self.decider = None
        return {"armed": False, "was_armed": was_armed}

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._tick_seconds())
            await self.tick()

    async def tick(self) -> None:
        """One decision: classify the vehicle, then start or stop if due."""
        decider = self.decider
        if decider is None or self.live_source is None:
            return
        try:
            vehicle = derive_from_live_source(
                self.live_source, replay_controller=self.replay_controller
            )
            action = decider.update(
                state=vehicle.state,
                coolant_temp=vehicle.coolant_temp,
                capture_active=tuning_profile.is_active(),
                now=time.monotonic(),
            )
            if action is None:
                return
            rule = decider.rule
            if action == "start":
                payload = await self.session.start(
                    ProfileCaptureStart(
                        profile=rule.profile,
                        rate_hz=rule.rate_hz,
                        bus_budget_requests_per_second=rule.bus_budget_requests_per_second,
                        tune_revision=rule.tune_revision,
                    )
                )
                self._emit("capture_profile.start", payload)
                log.warning("capture autostart started %s (%s)", rule.profile, decider.reason)
            else:
                payload = await self.session.stop("operator_stop")
                self._emit("capture_profile.stop", payload)
                log.warning("capture autostart stopped %s (%s)", rule.profile, decider.reason)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # A failed transition must not kill the loop: the next tick
            # re-reads the daemon's own view and tries again.
            log.exception("capture autostart tick failed")

    def _emit(self, action: str, payload: dict[str, Any]) -> None:
        if self._publish is not None:
            self._publish(action, "ok", payload)

    async def aclose(self) -> None:
        """Cancel the tick task (daemon shutdown)."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


__all__ = [
    "AUTOSTART_TICK_SECONDS",
    "CaptureAutostart",
    "ProfileCaptureStart",
    "EventPublisher",
    "ProfileCaptureError",
    "ProfileCaptureSession",
    "StopReason",
]
