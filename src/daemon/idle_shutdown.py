"""Escalate a sustained *parked-in-the-vehicle* state to a system shutdown.

The rig has no ignition sense and no 12 V sense of its own
(``docs/CANary-HAT-Circuits.md`` §3.7.6), so the accepted consequences were
either "unplug it when not in use" or "let the daemon infer engine-off from
bus activity and command a shutdown". This is the second half, which did not
exist: :func:`~src.vehicle_state.classify_vehicle_state` already names the
parked states and ``RecordArmingConfig.disarm_after_seconds`` already detects
the quiet period, but nothing escalated either to ``systemctl poweroff``.

This is **not** the power-loss watchdog, and the two are complementary rather
than redundant. ``scripts/ups_shutdown_watch.py`` is a *safety* watchdog: it
runs standalone on the system interpreter so it survives a hung daemon, and it
reacts to the X1202 **losing** input power. This is an *idle timeout*: it lives
in the daemon because that is where the vehicle-state classifier lives, and it
reacts to the vehicle being parked while input power is **still present** —
the always-hot OBD pin 16 case, where nothing else would ever power the rig
down. Their trigger conditions are mutually exclusive on ``input_lost``:

===================  ====================  =============================
X1202 input          Other evidence        Who acts
===================  ====================  =============================
present              bus quiet for hours   **this module**
lost                 battery discharging   ``ups_shutdown_watch.py``
lost                 battery idle          **nobody** — bench USB-C
===================  ====================  =============================

Bench safety
------------

The bench case is the one this must not break: the Pi on its own USB-C at a
desk, the X1202's barrel jack unplugged, no vehicle anywhere. That reads as
``vehicle_state == "no_data"`` — *exactly* an idle state — so a naive timeout
would power off the desk rig. Two independent guards prevent it, either of
which is sufficient:

1. **Input power must be present.** The bench rig has the barrel jack out, so
   the watchdog heartbeat reports ``input_lost: true`` and this module holds.
   This also covers carrying the rig from truck to desk mid-uptime.
2. **The vehicle must have been seen alive this uptime.** The countdown only
   arms after observing ``engine_running`` or ``ignition_only``. A rig that
   booted at a desk has never seen a live bus and can never arm.

Guard 1 is the load-bearing one; guard 2 also covers the case where the
watchdog is not running at all (no heartbeat, so no power evidence).

Other inhibits
--------------

The decision rests on an *inference* — "OBD went stale, so the ECM is powered
down" — and there are known conditions where that inference is invalid.
Recording the wrong answer costs some rows; powering the Pi off mid-capture
costs the capture and the drive it was recorded on, so each of these is a hard
refusal rather than a heuristic:

* **Passive mode armed** (``tx_gate.is_passive``). Same reasoning as
  :class:`~src.collection.record_arming.RecordArmingGate`: we stopped polling
  ourselves, and the event being observed may quiet the bus on purpose
  (reprogramming tools issue CommunicationControl / ControlDTCSetting to stop
  normal transmission). Every input this gate reads is suppressed by design
  during that window.
* **An all-DID / Mode 22 capture is running.** On this ECM, diagnostic session
  0xC0 and Mode 01 are mutually exclusive, so OBD goes stale **by design** for
  the whole capture — the exact false ``engine_off`` that the
  ``rpm_from_broadcast`` flag documents in ``classify_vehicle_state``.
* **A replay is running.** ``classify_vehicle_state`` already returns
  ``replay``, so it never matches an idle state; the guard is explicit rather
  than incidental.
* **The operator keep-alive hold is set.** The operator wrote an expiry to
  tell the *other* shutdown path "do not power this off, I am moving it to the
  bench". A hold honoured by only one of two shutdown paths is worse than no
  hold.

Ages come from the caller's monotonic clock. Any single non-idle observation
resets the countdown, so a momentary reclassification (a stale read, a brief
bus dropout) cannot accumulate toward a shutdown.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.collection import tuning_profile, tx_gate
from src.config import IdleShutdownConfig
from src.daemon.ups import read_keepalive_remaining, read_watchdog_status

log = logging.getLogger(__name__)

#: Vehicle states that count as "parked / disconnected". Deliberately excludes
#: ``ignition_only`` (KOEO is an operator at the wheel running diagnostics) and
#: ``unknown`` (the classifier could not decide, so neither should we).
#: Not ``bench``: idle shutdown saves a parked vehicle's battery, and a bench
#: Pi draws from USB-C or the UPS, not the vehicle.
DEFAULT_IDLE_STATES: tuple[str, ...] = ("engine_off", "parked", "no_data")

#: States proving we are actually attached to a live vehicle. Observing one of
#: these arms the countdown; until then the rig might be sitting on a bench.
DEFAULT_ACTIVE_STATES: tuple[str, ...] = ("engine_running", "ignition_only")


def input_power_present(status: dict[str, Any] | None) -> bool | None:
    """Is the X1202 seeing input power, per the watchdog heartbeat?

    ``None`` means *unknown* — no heartbeat file, or a heartbeat without the
    field (watchdog not running, or running an older version). Callers must
    treat unknown as "do not shut down": the whole point of this signal is to
    distinguish a parked vehicle from a bench, and guessing gets the bench
    case wrong in the direction that costs the operator their session.
    """
    if not status:
        return None
    value = status.get("input_lost")
    if not isinstance(value, bool):
        return None
    return not value


@dataclass
class IdleShutdownDecider:
    """Debounced parked-state -> shutdown state machine.

    Feed it one observation per tick. Returns ``True`` exactly once, when the
    vehicle has been continuously idle for ``idle_after_seconds`` *and* every
    guard agrees. The caller then powers the machine off; a second trigger
    while that is in flight would be noise, so it latches.
    """

    idle_after_seconds: float
    idle_states: tuple[str, ...] = DEFAULT_IDLE_STATES
    active_states: tuple[str, ...] = DEFAULT_ACTIVE_STATES
    require_prior_vehicle: bool = True
    _idle_since: float | None = field(default=None, init=False)
    _seen_vehicle: bool = field(default=False, init=False)
    _fired: bool = field(default=False, init=False)

    def update(
        self,
        *,
        state: str,
        now: float,
        input_present: bool | None,
        inhibited: bool = False,
    ) -> bool:
        """Advance the state machine.

        :param state: the current ``VehicleState.state`` name.
        :param now: monotonic seconds.
        :param input_present: from :func:`input_power_present`. ``None``
            (unknown) blocks shutdown — see that function.
        :param inhibited: caller-side refusals (passive mode, active capture,
            keep-alive hold). Holds the countdown *without* resetting it, so a
            brief capture inside a long parked window does not restart the
            clock from zero.
        """
        if state in self.active_states:
            self._seen_vehicle = True

        if state not in self.idle_states:
            self._idle_since = None
            return False

        if self._idle_since is None:
            self._idle_since = now

        if self._fired or inhibited:
            return False
        if self.require_prior_vehicle and not self._seen_vehicle:
            return False
        # Unknown power state blocks: bench-safety depends on this being
        # positively confirmed, not assumed.
        if input_present is not True:
            return False
        if (now - self._idle_since) < self.idle_after_seconds:
            return False
        self._fired = True
        return True

    def idle_seconds(self, now: float) -> float | None:
        """How long we have been continuously idle, or ``None``."""
        if self._idle_since is None:
            return None
        return now - self._idle_since

    @property
    def armed(self) -> bool:
        """Has a live vehicle been observed this uptime?"""
        return self._seen_vehicle

    def reset(self) -> None:
        self._idle_since = None
        self._seen_vehicle = False
        self._fired = False


def run_shutdown(command: tuple[str, ...], *, dry_run: bool) -> bool:
    """Execute the shutdown, or log what would have happened.

    Returns ``True`` when the command was issued (or would have been, in a dry
    run). Failures are logged and swallowed: a daemon that crashes because it
    could not power the machine off is strictly worse than one that keeps
    collecting.
    """
    if dry_run:
        log.warning("idle shutdown: DRY RUN, would have run %s", " ".join(command))
        return True
    log.warning(
        "idle shutdown: vehicle parked and input power present, running %s",
        " ".join(command),
    )
    try:
        subprocess.run(command, check=True)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("idle shutdown: command failed: %s", exc)
        return False
    return True


@dataclass
class IdleShutdownMonitor:
    """Ties the pure decider to the daemon's tick: I/O, inhibits, execution.

    Lives here rather than inline in ``run_live_inference`` so the hot loop
    calls one method and the file reads are throttled in one place. The
    decider is fed on **every** tick (it is pure and cheap) so a brief
    ``engine_running`` blip always resets the countdown, but the watchdog
    heartbeat and keep-alive files are re-read only every ``poll_seconds`` —
    they change on a scale of seconds, and the countdown runs for minutes.
    """

    config: IdleShutdownConfig
    decider: IdleShutdownDecider
    status_path: Path
    keepalive_path: Path
    poll_seconds: float = 15.0
    _last_poll: float | None = field(default=None, init=False)
    _input_present: bool | None = field(default=None, init=False)
    _held: bool = field(default=False, init=False)

    @classmethod
    def from_config(cls, config: IdleShutdownConfig) -> IdleShutdownMonitor:
        return cls(
            config=config,
            decider=IdleShutdownDecider(
                idle_after_seconds=config.idle_after_seconds,
                idle_states=config.idle_states,
            ),
            status_path=Path(config.status_file),
            keepalive_path=Path(config.keepalive_file),
        )

    def _refresh(self, now: float) -> None:
        if self._last_poll is not None and (now - self._last_poll) < self.poll_seconds:
            return
        self._last_poll = now
        self._input_present = input_power_present(read_watchdog_status(self.status_path))
        self._held = (
            self.config.respect_keepalive and read_keepalive_remaining(self.keepalive_path) > 0.0
        )

    def observe(self, *, state: str, now: float) -> bool:
        """Advance one tick. ``True`` means "power the machine off now"."""
        self._refresh(now)
        # tx_gate / tuning_profile are in-memory module state, so these are
        # free to read every tick — and they are exactly the conditions where
        # OBD goes stale by design and the parked inference is invalid.
        inhibited = self._held or tx_gate.is_passive() or tuning_profile.is_active()
        return self.decider.update(
            state=state,
            now=now,
            input_present=self._input_present,
            inhibited=inhibited,
        )

    def trigger(self) -> bool:
        """Run the configured shutdown command."""
        log.warning(
            "idle shutdown: %s for %.0fs with vehicle input power present — powering off",
            self.decider.idle_states,
            self.config.idle_after_seconds,
        )
        return run_shutdown(self.config.poweroff_command, dry_run=self.config.dry_run)
