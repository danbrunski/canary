"""UPS/power status for the dashboard and MCP.

The ``ups-shutdown`` watchdog (``scripts/ups_shutdown_watch.py``) runs as
root, standalone from the daemon, and already owns all direct GPIO/I2C
access (see its module docstring). This module does **not** duplicate that
hardware access — the unprivileged ``canary`` daemon process would otherwise
race the watchdog for the same GPIO line (``lgpio`` line-requests are
typically exclusive) for no benefit, since the watchdog already publishes
everything the daemon needs as a JSON heartbeat.

So this is purely file-backed: read the watchdog's heartbeat
(``UPS_STATUS_FILE``), and read/write the operator keep-alive hold
(``UPS_KEEPALIVE_FILE``) — the same file the watchdog polls every tick. Both
paths default to the shared ``/run/canary`` tmpfs dir (see
``systemd/tmpfiles-canary.conf``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from src.atomic_write import atomic_write_text

DEFAULT_UPS_STATUS_PATH = Path("/run/canary/ups_status")
DEFAULT_UPS_KEEPALIVE_PATH = Path("/run/canary/ups_keepalive")

#: A heartbeat older than this (watchdog's default poll is 1s) means the
#: watchdog process has likely stopped or hung, not just "not powered".
DEFAULT_STALE_AFTER_S = 10.0


def read_watchdog_status(path: Path = DEFAULT_UPS_STATUS_PATH) -> dict[str, Any] | None:
    """Read the watchdog's JSON heartbeat, or ``None`` if absent/unreadable.

    A missing file means the watchdog isn't running (or hasn't written its
    first heartbeat yet) — not necessarily a power problem. Never raises.
    """
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def read_keepalive_remaining(
    path: Path = DEFAULT_UPS_KEEPALIVE_PATH, *, now: float | None = None
) -> float:
    """Seconds of keep-alive hold remaining, read directly from the hold file.

    The file holds a single float: the wall-clock (epoch) expiry. This is a
    deliberate duplicate of ``ups_shutdown_watch.keepalive_remaining`` — that
    module is system-python + ``lgpio`` only and isn't importable from the
    venv, and the format is a two-line contract (write a float, read a float),
    not worth sharing code for.
    """
    try:
        text = path.read_text()
    except OSError:
        return 0.0
    try:
        expiry = float(text.strip())
    except ValueError:
        return 0.0
    now = time.time() if now is None else now
    return max(0.0, expiry - now)


def write_keepalive(
    minutes: float, path: Path = DEFAULT_UPS_KEEPALIVE_PATH, *, now: float | None = None
) -> float:
    """Set (``minutes`` > 0) or cancel (``minutes`` <= 0) the keep-alive hold.

    Returns the expiry epoch written (``0.0`` when cancelled). Atomic write
    (temp file + rename) so the watchdog never reads a partial value.
    """
    now = time.time() if now is None else now
    expiry = 0.0 if minutes <= 0 else now + minutes * 60.0
    atomic_write_text(path, str(expiry))
    return expiry


def build_power_snapshot(
    status: dict[str, Any] | None,
    keepalive_remaining_s: float,
    *,
    now: float | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> dict[str, Any]:
    """Assemble the ``GET /control/power`` payload.

    ``status`` is the watchdog heartbeat (``read_watchdog_status()``);
    ``keepalive_remaining_s`` is read separately (``read_keepalive_remaining()``)
    so it reflects the current hold even if the watchdog's own copy (recorded
    at its last poll, up to ~1s old) is slightly behind. Pure — takes already-read
    values so it's trivially unit-testable.
    """
    now = time.time() if now is None else now
    if status is None:
        return {
            "watchdog_running": False,
            "watchdog_stale": None,
            "armed": None,
            "input_lost": None,
            "on_battery": None,
            "soc": None,
            "vcell": None,
            "seconds_lost": None,
            "grace_s": None,
            "keepalive_remaining_s": keepalive_remaining_s,
            "status_age_s": None,
        }
    ts = status.get("ts")
    age = max(0.0, now - ts) if isinstance(ts, int | float) else None
    return {
        "watchdog_running": True,
        "watchdog_stale": age is not None and age > stale_after_s,
        "armed": status.get("armed"),
        "input_lost": status.get("input_lost"),
        "on_battery": status.get("on_battery"),
        "soc": status.get("soc"),
        "vcell": status.get("vcell"),
        "seconds_lost": status.get("seconds_lost"),
        "grace_s": status.get("grace_s"),
        "keepalive_remaining_s": keepalive_remaining_s,
        "status_age_s": age,
    }
