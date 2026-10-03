#!/usr/bin/env python3
"""Independent UPS power-loss auto-shutdown watchdog (Geekworm X1202).

Watches the X1202's power-loss-detect line and, once input power has been
*continuously* lost for ``UPS_SHUTDOWN_DELAY`` seconds, cleanly powers the Pi
off — **but only if the battery is actually being drained**. The X1202 then
cuts battery output, so the next time switched 12 V returns (key-on) it's a
fresh power-apply event and the board auto-boots.

PLD polarity (per the vendor's ``suptronics/x120x`` ``pld.py``): GPIO6
**HIGH = input power OK**, **LOW = input power lost**. The line is *not*
internally pulled up — the board drives it HIGH when powered and it reads LOW
on loss (we apply a pull-DOWN so an undriven line fails to the "lost" side).
NB: an earlier version had this inverted (pull-up + HIGH-means-lost), which
made the watchdog read "power lost" even on good barrel-jack power.

Why the discharge check matters — bench vs. key-off:
  Input can read "lost" in two very different situations:
    1. In the vehicle at key-off: the barrel-jack 12 V is gone and the Pi is
       running down the UPS battery. We want to shut down.
    2. On the bench: the Pi is powered from its **own USB-C**, the X1202's
       barrel jack is simply unplugged, and the battery is *idle*. We do NOT
       want to shut down — there's stable external power.
  PLD alone cannot tell these apart. But the fuel gauge can: only case (1)
  shows the battery *discharging*. So the watchdog reads state-of-charge from
  the gauge it already uses for the presence guard, and treats "input lost"
  as real only when the battery is genuinely draining (or already critically
  low). Bench USB-C power keeps the SoC flat, so it never trips. This is what
  makes bench-top power a first-class, non-disruptive mode.

Design notes:
  * **Deliberately standalone.** Runs on the *system* interpreter with
    ``lgpio`` (Pi 5 native), NOT the canary venv, and is not tied to
    canary.service. It must still shut the Pi down if the daemon is hung or
    dead — that's the whole point of a power-loss watchdog.
  * **Debounced.** The delay makes brief drops — e.g. the ACC line dipping
    during cranking — a no-op: power comes back, the countdown resets. Only a
    real key-off (input gone *and* battery draining for the full delay)
    triggers shutdown.
  * **Bench-safe (two ways).** (a) With no UPS attached, the pulled-down
    GPIO6 reads LOW and looks like "power lost"; the watchdog refuses to arm
    unless the X1202 fuel gauge answers on I2C (``UPS_I2C_ADDR``, default
    0x36). (b) With a UPS attached but the Pi on external USB-C, the battery
    isn't discharging, so the discharge gate keeps it from ever counting down.
  * **Grace window = transition budget.** ``UPS_SHUTDOWN_DELAY`` (default 300s
    / 5 min) is how long the Pi runs on battery after input loss before
    shutting down. It doubles as the truck->bench move budget: unplug the
    barrel jack, carry the Pi, and plug into bench USB-C before the grace
    expires — the discharge stops, the countdown aborts, and the session is
    never interrupted. A real key-off with no rescue shuts down after the
    grace. ``UPS_CRITICAL_SOC`` is an independent hard floor.
  * **Keep-alive hold.** For a bench transition *longer* than the grace, the
    operator sets "keep alive N min" from the dashboard/MCP, which writes an
    expiry epoch to ``UPS_KEEPALIVE_FILE``; while unexpired the watchdog holds
    regardless of power. This is also the backstop if the gauge can't be read.

Configuration is via environment variables (set in the systemd unit):
  UPS_PLD_GPIO=6            BCM pin for power-loss detect
  UPS_GPIOCHIP=0            gpiochip index (/dev/gpiochipN)
  UPS_SHUTDOWN_DELAY=300    grace seconds on battery before poweroff (transition budget)
  UPS_POLL_INTERVAL=1.0     seconds between samples
  UPS_I2C_BUS=1             fuel-gauge bus (presence guard + SoC reads)
  UPS_I2C_ADDR=0x36         fuel-gauge address (0 disables the guard + gate)
  UPS_REQUIRE_DISCHARGE=1   1 = hold while externally powered (bench-safe);
                            0 = legacy PLD-only behavior
  UPS_MIN_DISCHARGE_RATE=6  %/hr of SoC drop that counts as "on battery"
  UPS_CRITICAL_SOC=15       at/below this SoC (%), input-lost => shut down
                            regardless of the measured rate (protect cells)
  UPS_DISCHARGE_WINDOW=120  seconds of SoC history used to estimate the rate
  UPS_DISCHARGE_MIN_SPAN=40 min seconds of history before trusting the rate
  UPS_KEEPALIVE_FILE=...    hold file (epoch expiry); default /run/canary/ups_keepalive
  UPS_STATUS_FILE=...       status heartbeat (JSON); default /run/canary/ups_status
  UPS_POWEROFF_CMD=...      shutdown command (default: systemctl poweroff)
  UPS_DRY_RUN=0             1 = log instead of powering off (for testing)
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

log = logging.getLogger("ups_shutdown")

_I2C_SLAVE = 0x0703  # linux/i2c-dev.h
_REG_SOC = 0x04  # MAX1704x state-of-charge register (chip-agnostic decode)
_REG_VCELL = 0x02  # MAX1704x cell-voltage register


# --------------------------------------------------------------------------
# Pure decision logic (no hardware) — unit-tested in tests/test_ups_shutdown.py
# --------------------------------------------------------------------------
@dataclass
class ShutdownDecider:
    """Debounced power-loss -> shutdown state machine.

    Feed it ``power_lost`` samples with a monotonic ``now``; it returns True
    once power has been *continuously* lost for ``delay_s``. Any single
    power-good sample resets the countdown (so cranking dips are ignored).

    ``power_lost`` here is the *fused* signal — input-lost AND on-battery —
    not the raw PLD line (see ``power_truly_lost``).
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


@dataclass
class DischargeEstimator:
    """Sliding-window estimate of battery discharge rate from SoC samples.

    Feed ``(soc, now)`` samples; ``rate_pct_per_hour`` returns the signed
    slope over the retained window (negative = discharging), or ``None`` until
    the samples span at least ``min_span_s`` (so a fresh boot doesn't act on a
    single reading). Chip-agnostic: SoC decode is identical for MAX17040/48.
    """

    window_s: float
    min_span_s: float = 5.0
    _samples: deque[tuple[float, float]] = field(default_factory=deque)

    def update(self, soc: float, now: float) -> None:
        self._samples.append((now, soc))
        cutoff = now - self.window_s
        # Keep at least two samples so a rate is always computable once warm.
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def rate_pct_per_hour(self) -> float | None:
        if len(self._samples) < 2:
            return None
        t0, s0 = self._samples[0]
        t1, s1 = self._samples[-1]
        dt = t1 - t0
        if dt < self.min_span_s:
            return None
        return (s1 - s0) / dt * 3600.0


def on_battery(
    rate_pct_per_hour: float | None,
    soc: float | None,
    *,
    min_discharge_rate: float,
    critical_soc: float,
) -> bool | None:
    """Decide whether the Pi is genuinely running down the UPS battery.

    Returns:
      * ``True``  — discharging faster than ``min_discharge_rate`` %/hr, or SoC
        already at/below ``critical_soc`` (protect the cells): treat input-loss
        as real.
      * ``False`` — battery flat/charging: external power is present (bench),
        so input-loss is *not* actionable.
      * ``None``  — not enough gauge data yet to tell (caller decides how to
        fail; default is to hold).
    """
    if soc is not None and soc <= critical_soc:
        return True
    if rate_pct_per_hour is None:
        return None
    return rate_pct_per_hour <= -abs(min_discharge_rate)


def power_truly_lost(
    input_is_lost: bool,
    battery_state: bool | None,
    *,
    require_discharge: bool,
) -> bool:
    """Fuse the PLD "input lost" signal with the battery-discharge state into
    the boolean the debouncer counts toward shutdown.

    With ``require_discharge`` (default): once the barrel-jack input is lost we
    count down toward shutdown *unless we can positively confirm the Pi is
    still externally powered* — i.e. the battery is **not** discharging
    (``battery_state is False``, the bench-USB-C case). Discharging (``True``)
    or not-yet-determined (``None``) both count, so a real key-off shuts down
    predictably after the grace window even before the discharge rate is
    confirmed, while plugging into bench power mid-countdown (which stops the
    discharge) aborts it. With ``require_discharge`` off, this collapses to the
    legacy PLD-only behavior.

    The bench stays up because a flat SoC is confirmed as ``False`` within the
    discharge window (well under the grace period); the keep-alive hold is the
    backstop for the rare case where the gauge can't be read.
    """
    if not input_is_lost:
        return False
    if not require_discharge:
        return True
    return battery_state is not False


def input_lost(gpio_level: int) -> bool:
    """Map the raw PLD GPIO level to "input power lost".

    Per the vendor ``suptronics/x120x`` ``pld.py``: GPIO6 HIGH (1) = power OK,
    LOW (0) = power lost. (The previous inverted mapping is what made the
    watchdog fire on good barrel-jack power.)
    """
    return gpio_level == 0


def keepalive_remaining(text: str | None, now_wall: float) -> float:
    """Seconds of keep-alive hold remaining, from a hold-file's contents.

    The file holds a single float: the wall-clock (epoch) time the hold
    expires. Returns 0.0 when there's no hold, it's expired, or the contents
    are unparseable. Pure so it's unit-testable; ``read_keepalive`` does the
    I/O. The operator sets the hold from the dashboard/MCP ("keep alive N
    min") before unplugging for a long bench transition.
    """
    if text is None:
        return 0.0
    try:
        expiry = float(text.strip())
    except ValueError:
        return 0.0
    return max(0.0, expiry - now_wall)


def read_keepalive(path: str) -> str | None:
    """Read the keep-alive hold file's contents, or ``None`` if absent/unreadable."""
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def build_status_record(
    *,
    armed: bool,
    input_is_lost: bool,
    on_battery: bool | None,
    soc: float | None,
    vcell: float | None,
    seconds_lost: float,
    grace_s: float,
    keepalive_remaining_s: float,
    now_wall: float,
) -> dict[str, Any]:
    """Build the JSON-serializable heartbeat record written to ``UPS_STATUS_FILE``.

    Pure (no I/O) so it's unit-testable; ``write_status`` does the atomic write.
    Consumed by the daemon's power reader (``src/daemon/ups.py``) so the
    dashboard/MCP can show live UPS state without talking to GPIO/I2C directly
    (those require root; the daemon runs as ``canary``).
    """
    return {
        "armed": armed,
        "input_lost": input_is_lost,
        "on_battery": on_battery,
        "soc": soc,
        "vcell": vcell,
        "seconds_lost": round(seconds_lost, 1),
        "grace_s": grace_s,
        "keepalive_remaining_s": round(keepalive_remaining_s, 1),
        "ts": now_wall,
    }


def write_status(path: str, record: dict[str, Any]) -> None:
    """Atomically write the status heartbeat (temp file + rename).

    Best-effort: a write failure (e.g. ``/run/canary`` not yet created) is
    logged and swallowed — the watchdog's shutdown logic never depends on this
    file, only the daemon-side reader does.
    """
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("failed to write status heartbeat %s: %s", path, exc)


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


def _read_reg16(bus: int, addr: int, reg: int) -> int | None:
    """Read a 16-bit big-endian register from an I2C device, or ``None`` on error."""
    path = f"/dev/i2c-{bus}"
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        fcntl.ioctl(fd, _I2C_SLAVE, addr)
        os.write(fd, bytes([reg]))
        data = os.read(fd, 2)
        if len(data) != 2:
            return None
        return (data[0] << 8) | data[1]
    except OSError:
        return None
    finally:
        os.close(fd)


def read_soc(bus: int, addr: int) -> float | None:
    """Read state-of-charge (%) from the MAX1704x fuel gauge, or ``None`` on any
    I2C error. Decode ``raw / 256`` is the same across MAX17040 and MAX17048,
    so this is chip-agnostic. ``addr == 0`` disables gauge reads."""
    if addr == 0:
        return None
    raw = _read_reg16(bus, addr, _REG_SOC)
    return None if raw is None else raw / 256.0


def read_vcell(bus: int, addr: int) -> float | None:
    """Read cell voltage (V) from the MAX1704x fuel gauge, or ``None`` on error.

    VCELL is the top 12 bits of the 16-bit register, 1.25 mV/LSB (MAX17048;
    MAX17040 uses 1.25 mV/LSB over the top 12 bits too — same decode).
    ``addr == 0`` disables gauge reads.
    """
    if addr == 0:
        return None
    raw = _read_reg16(bus, addr, _REG_VCELL)
    return None if raw is None else (raw >> 4) * 0.00125


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
    # Grace window = the operator's truck->bench transition budget: run on
    # battery this long after input loss before shutting down. Plugging into
    # bench power before it expires (which stops the discharge) aborts it.
    delay = _env_float("UPS_SHUTDOWN_DELAY", 300.0)
    poll = max(0.1, _env_float("UPS_POLL_INTERVAL", 1.0))
    i2c_bus = _env_int("UPS_I2C_BUS", 1)
    i2c_addr = _env_int("UPS_I2C_ADDR", 0x36)
    require_discharge = _env_int("UPS_REQUIRE_DISCHARGE", 1) != 0
    min_discharge_rate = _env_float("UPS_MIN_DISCHARGE_RATE", 6.0)
    critical_soc = _env_float("UPS_CRITICAL_SOC", 15.0)
    # Wider window + min-span so the slow SoC drop of a big 4-cell pack is
    # detected reliably (a 30s look was too subtle -> missed real discharge).
    discharge_window = _env_float("UPS_DISCHARGE_WINDOW", 120.0)
    discharge_min_span = _env_float("UPS_DISCHARGE_MIN_SPAN", 40.0)
    keepalive_file = os.environ.get("UPS_KEEPALIVE_FILE", "/run/canary/ups_keepalive")
    status_file = os.environ.get("UPS_STATUS_FILE", "/run/canary/ups_status")
    dry_run = _env_int("UPS_DRY_RUN", 0) != 0

    if not ups_present(i2c_bus, i2c_addr):
        log.warning(
            "X1202 not detected at i2c-%s 0x%02x; watchdog disarmed (bench mode).",
            i2c_bus,
            i2c_addr,
        )
        write_status(
            status_file,
            build_status_record(
                armed=False,
                input_is_lost=False,
                on_battery=None,
                soc=None,
                vcell=None,
                seconds_lost=0.0,
                grace_s=delay,
                keepalive_remaining_s=0.0,
                now_wall=time.time(),
            ),
        )
        return 0

    if require_discharge and i2c_addr == 0:
        # No gauge address => no discharge signal; the gate would hold forever.
        log.warning(
            "UPS_REQUIRE_DISCHARGE=1 but UPS_I2C_ADDR=0 (no gauge); "
            "falling back to PLD-only behavior."
        )
        require_discharge = False

    # Lazy import: keeps the module import-safe under the venv (no lgpio there,
    # only system python3), so tests can import the pure logic above.
    import lgpio  # type: ignore[import-not-found]  # noqa: PLC0415

    handle = lgpio.gpiochip_open(chip)
    # Pull-DOWN: the board drives GPIO6 HIGH when powered and reads LOW on loss
    # (vendor pld.py). A pull-down makes an undriven line fail to "lost", never
    # a false "OK". (Was SET_PULL_UP, which inverted the reading.)
    lgpio.gpio_claim_input(handle, pin, lgpio.SET_PULL_DOWN)

    decider = ShutdownDecider(delay_s=delay)
    estimator = DischargeEstimator(window_s=discharge_window, min_span_s=discharge_min_span)
    stop = {"stop": False}
    _install_signal_handlers(stop)

    log.info(
        "UPS watchdog armed: GPIO%d on gpiochip%d, %.0fs grace, poll %.1fs, "
        "discharge-gate=%s (>= %.1f%%/hr or SoC <= %.0f%%), keep-alive=%s%s",
        pin,
        chip,
        delay,
        poll,
        "on" if require_discharge else "off",
        min_discharge_rate,
        critical_soc,
        keepalive_file,
        " [DRY-RUN]" if dry_run else "",
    )
    was_counting = False
    warned_external = False
    warned_keepalive = False
    try:
        while not stop["stop"]:
            now = time.monotonic()
            input_is_lost = input_lost(lgpio.gpio_read(handle, pin))

            soc = read_soc(i2c_bus, i2c_addr)
            vcell = read_vcell(i2c_bus, i2c_addr)
            if soc is not None:
                estimator.update(soc, now)
            rate = estimator.rate_pct_per_hour()
            battery_state = on_battery(
                rate,
                soc,
                min_discharge_rate=min_discharge_rate,
                critical_soc=critical_soc,
            )
            truly_lost = power_truly_lost(
                input_is_lost, battery_state, require_discharge=require_discharge
            )

            # Operator keep-alive hold: "keep alive N min" from the dashboard/MCP
            # pins the watchdog open regardless of power (for a long bench move).
            hold_s = keepalive_remaining(read_keepalive(keepalive_file), time.time())

            write_status(
                status_file,
                build_status_record(
                    armed=True,
                    input_is_lost=input_is_lost,
                    on_battery=battery_state,
                    soc=soc,
                    vcell=vcell,
                    seconds_lost=decider.seconds_lost(now),
                    grace_s=delay,
                    keepalive_remaining_s=hold_s,
                    now_wall=time.time(),
                ),
            )

            if hold_s > 0:
                if truly_lost and not warned_keepalive:
                    log.warning(
                        "input lost but keep-alive hold active (%.0fs left) — not shutting down",
                        hold_s,
                    )
                    warned_keepalive = True
                decider.update(power_lost=False, now=now)  # keep clock reset
                was_counting = False
                time.sleep(poll)
                continue
            warned_keepalive = False

            # Visibility: PLD reads "power lost" but the battery isn't draining
            # -> external (bench) power present. Log once per episode, not per poll.
            if input_is_lost and require_discharge and not truly_lost:
                if not warned_external:
                    log.info(
                        "PLD reads power-lost but battery not discharging "
                        "(SoC=%s, rate=%s %%/hr) — external/bench power present, "
                        "holding.",
                        "n/a" if soc is None else f"{soc:.1f}%",
                        "n/a" if rate is None else f"{rate:.1f}",
                    )
                    warned_external = True
            else:
                warned_external = False

            if decider.update(power_lost=truly_lost, now=now):
                log.warning(
                    "on battery for %.0fs (>= %.0fs), SoC=%s — powering off",
                    decider.seconds_lost(now),
                    delay,
                    "n/a" if soc is None else f"{soc:.1f}%",
                )
                _poweroff(dry_run)
                if dry_run:
                    decider = ShutdownDecider(delay_s=delay)  # re-arm for testing
                else:
                    break
            elif decider.counting_down and not was_counting:
                log.warning("running on battery; shutdown in %.0fs unless input restored", delay)
            elif was_counting and not decider.counting_down:
                log.info("input/battery restored; shutdown aborted")
            was_counting = decider.counting_down
            time.sleep(poll)
    finally:
        lgpio.gpiochip_close(handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
