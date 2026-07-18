#!/usr/bin/env python3
"""Independent UPS power-loss auto-shutdown watchdog (Geekworm X1202).

Watches the X1202's power-loss-detect line (GPIO6: LOW = input power good,
HIGH = running on battery) and, once input power has been *continuously*
lost for ``UPS_SHUTDOWN_DELAY`` seconds, cleanly powers the Pi off. The
X1202 then cuts battery output, so the next time switched 12 V returns
(key-on) it's a fresh power-apply event and the board auto-boots.

Design notes:
  * **Deliberately standalone.** Runs on the *system* interpreter with
    ``lgpio`` (Pi 5 native), NOT the canary venv, and is not tied to
    canary.service. It must still shut the Pi down if the daemon is hung or
    dead — that's the whole point of a power-loss watchdog.
  * **Debounced.** The delay makes brief drops — e.g. the ACC line dipping
    during cranking — a no-op: power comes back, the countdown resets. Only a
    real key-off (power gone for the full delay) triggers shutdown.
  * **Bench-safe.** With no UPS attached, GPIO6's pull-up floats HIGH and
    looks like "power lost". So the watchdog refuses to arm unless the X1202
    fuel gauge answers on I2C (``UPS_I2C_ADDR``, default 0x36). No UPS ->
    exit cleanly, shut nothing down.

Configuration is via environment variables (set in the systemd unit):
  UPS_PLD_GPIO=6            BCM pin for power-loss detect
  UPS_GPIOCHIP=0            gpiochip index (/dev/gpiochipN)
  UPS_SHUTDOWN_DELAY=30     seconds of continuous power loss before poweroff
  UPS_POLL_INTERVAL=1.0     seconds between samples
  UPS_I2C_BUS=1             fuel-gauge bus for the presence guard
  UPS_I2C_ADDR=0x36         fuel-gauge address (0 disables the guard)
  UPS_POWEROFF_CMD=...      shutdown command (default: systemctl poweroff)
  UPS_DRY_RUN=0             1 = log instead of powering off (for testing)
"""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from types import FrameType

log = logging.getLogger("ups_shutdown")

_I2C_SLAVE = 0x0703  # linux/i2c-dev.h


# --------------------------------------------------------------------------
# Pure decision logic (no hardware) — unit-tested in tests/test_ups_shutdown.py
# --------------------------------------------------------------------------
@dataclass
class ShutdownDecider:
    """Debounced power-loss -> shutdown state machine.

    Feed it ``power_lost`` samples with a monotonic ``now``; it returns True
    once power has been *continuously* lost for ``delay_s``. Any single
    power-good sample resets the countdown (so cranking dips are ignored).
    """

    delay_s: float
    _lost_since: float | None = None

    def update(self, power_lost: bool, now: float) -> bool:
        if not power_lost:
            self._lost_since = None
            return False
        if self._lost_since is None:
            self._lost_since = now
            return False
        return (now - self._lost_since) >= self.delay_s

    @property
    def counting_down(self) -> bool:
        return self._lost_since is not None

    def seconds_lost(self, now: float) -> float:
        return 0.0 if self._lost_since is None else max(0.0, now - self._lost_since)


# --------------------------------------------------------------------------
# Hardware helpers
# --------------------------------------------------------------------------
def ups_present(bus: int, addr: int) -> bool:
    """True if a device ACKs at ``addr`` on ``/dev/i2c-{bus}`` (the X1202 fuel
    gauge). Used as the arm guard so a bench Pi with no UPS never shuts down.
    ``addr == 0`` disables the guard (always present)."""
    if addr == 0:
        return True
    path = f"/dev/i2c-{bus}"
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError as exc:
        log.warning("i2c bus %s not available (%s); treating UPS as absent", path, exc)
        return False
    try:
        fcntl.ioctl(fd, _I2C_SLAVE, addr)
        os.read(fd, 1)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("bad %s=%r; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw, 0)  # 0 => honor 0x.. hex
    except ValueError:
        log.warning("bad %s=%r; using default %s", name, raw, default)
        return default


def _poweroff(dry_run: bool) -> None:
    cmd = os.environ.get("UPS_POWEROFF_CMD", "systemctl poweroff").split()
    if dry_run:
        log.warning("[dry-run] would run: %s", " ".join(cmd))
        return
    log.warning("triggering shutdown: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("poweroff command failed: %s", exc)


def _install_signal_handlers(stop: dict[str, bool]) -> None:
    def _handle(signum: int, _frame: FrameType | None) -> None:
        log.info("received signal %s; exiting", signum)
        stop["stop"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    pin = _env_int("UPS_PLD_GPIO", 6)
    chip = _env_int("UPS_GPIOCHIP", 0)
    delay = _env_float("UPS_SHUTDOWN_DELAY", 30.0)
    poll = max(0.1, _env_float("UPS_POLL_INTERVAL", 1.0))
    i2c_bus = _env_int("UPS_I2C_BUS", 1)
    i2c_addr = _env_int("UPS_I2C_ADDR", 0x36)
    dry_run = _env_int("UPS_DRY_RUN", 0) != 0

    if not ups_present(i2c_bus, i2c_addr):
        log.warning(
            "X1202 not detected at i2c-%s 0x%02x; watchdog disarmed (bench mode).",
            i2c_bus,
            i2c_addr,
        )
        return 0

    # Lazy import: keeps the module import-safe under the venv (no lgpio there,
    # only system python3), so tests can import the pure logic above.
    import lgpio  # type: ignore[import-not-found]  # noqa: PLC0415

    handle = lgpio.gpiochip_open(chip)
    lgpio.gpio_claim_input(handle, pin, lgpio.SET_PULL_UP)

    decider = ShutdownDecider(delay_s=delay)
    stop = {"stop": False}
    _install_signal_handlers(stop)

    log.info(
        "UPS watchdog armed: GPIO%d on gpiochip%d, %.0fs delay, poll %.1fs%s",
        pin,
        chip,
        delay,
        poll,
        " [DRY-RUN]" if dry_run else "",
    )
    was_counting = False
    try:
        while not stop["stop"]:
            now = time.monotonic()
            power_lost = lgpio.gpio_read(handle, pin) == 1
            if decider.update(power_lost, now):
                log.warning(
                    "input power lost for %.0fs (>= %.0fs); powering off",
                    decider.seconds_lost(now),
                    delay,
                )
                _poweroff(dry_run)
                if dry_run:
                    decider = ShutdownDecider(delay_s=delay)  # re-arm for testing
                else:
                    break
            elif decider.counting_down and not was_counting:
                log.warning("input power lost; shutdown in %.0fs unless restored", delay)
            elif was_counting and not decider.counting_down:
                log.info("input power restored; shutdown aborted")
            was_counting = decider.counting_down
            time.sleep(poll)
    finally:
        lgpio.gpiochip_close(handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
