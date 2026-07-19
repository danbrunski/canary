"""Systemd service-health read tool.

Answers "is the daemon/MCP-server/watchdog actually up and stable?" without
an SSH round-trip — the diagnostic that would have made today's UPS
crash-loop (canary.service restarting every ~30s, ``NRestarts`` climbing)
visible from the very first tool call instead of ad-hoc ``journalctl``
scraping.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

#: Services the daemon stack is made of; also the default set queried.
DEFAULT_UNITS = ("canary", "canary-mcp", "ups-shutdown")


def _read_uptime_seconds() -> float | None:
    """Seconds since boot, from ``/proc/uptime`` — the same monotonic clock
    systemd's ``...Monotonic`` timestamps are relative to, so no timezone
    parsing is needed to compute a unit's uptime."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _service_status(unit: str, *, boot_uptime_s: float | None) -> dict[str, Any]:
    """One unit's health via ``systemctl show`` (structured, not text-scraped).

    ``NRestarts`` is the crash-loop tell — a healthy service reads 0 (or a
    small number from intentional deploy restarts); a service stuck
    restarting every few seconds (like the UPS watchdog before it was fixed)
    climbs continuously across polls.
    """
    props = ["ActiveState", "SubState", "NRestarts", "ActiveEnterTimestampMonotonic"]
    try:
        proc = subprocess.run(
            ["systemctl", "show", unit, "--no-page", *[f"-p{p}" for p in props]],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"unit": unit, "error": str(exc)}
    if proc.returncode != 0:
        return {"unit": unit, "error": proc.stderr.strip() or f"exit={proc.returncode}"}

    values: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key] = value

    active_state = values.get("ActiveState", "unknown")
    monotonic_usec = values.get("ActiveEnterTimestampMonotonic", "0")
    uptime_s: float | None = None
    if active_state == "active" and boot_uptime_s is not None:
        try:
            uptime_s = round(boot_uptime_s - int(monotonic_usec) / 1_000_000.0, 1)
        except ValueError:
            uptime_s = None

    return {
        "unit": unit,
        "is_active": active_state == "active",
        "active_state": active_state,
        "sub_state": values.get("SubState", "unknown"),
        "restart_count": int(values["NRestarts"]) if "NRestarts" in values else None,
        "uptime_seconds": uptime_s,
    }


def get_service_health(units: list[str] | None = None) -> dict[str, Any]:
    """``systemctl`` health snapshot for the CANary service stack.

    Parameters
    ----------
    units:
        Unit names to check. Defaults to ``canary``, ``canary-mcp``,
        ``ups-shutdown`` — the whole stack this MCP server runs alongside.

    Runs where the MCP server itself runs (the Pi in normal deployment), no
    daemon HTTP round-trip. Returns ``{"available": False, ...}`` when
    ``systemctl`` isn't on PATH (dev machine without systemd) rather than
    raising, matching ``tail_daemon_log``'s fallback pattern.
    """
    if shutil.which("systemctl") is None:
        return {"available": False, "services": []}
    resolved_units = units if units else list(DEFAULT_UNITS)
    boot_uptime_s = _read_uptime_seconds()
    return {
        "available": True,
        "services": [_service_status(unit, boot_uptime_s=boot_uptime_s) for unit in resolved_units],
    }


__all__ = ["get_service_health", "DEFAULT_UNITS"]
