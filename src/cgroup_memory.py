"""Is a memory ceiling enforced on this process? (D03.21; moved from ``daemon.health`` by R15.)

Read from the kernel's cgroup files, never from ``systemctl show``. A root
module of the lowest layer because two layers need it: the daemon reports it in
``/status``, and the detector evaluation's worker pool sizes itself to it
(:mod:`src.models.evaluation_workers`) -- a layer-4 library must not import the
layer-7 daemon for a file parser. ``src.daemon.health`` re-exports both names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["CGROUP_ROOT", "memory_limit_status"]


#: Where the unified (v2) cgroup hierarchy is mounted on the Pi.
CGROUP_ROOT = Path("/sys/fs/cgroup")


def memory_limit_status(
    *,
    cgroup_root: Path = CGROUP_ROOT,
    self_cgroup: Path = Path("/proc/self/cgroup"),
) -> dict[str, Any]:
    """Is a memory ceiling actually enforced on this process right now?

    Read from the cgroup file, never from ``systemctl show`` (D03.21): on
    2026-09-18 the unit carried ``MemoryMax=6G`` and `systemctl show` echoed
    it back for four days while the kernel enforced nothing, because the Pi
    booted with ``cgroup_disable=memory``. The limits exist because the
    trip-end digest once wedged the daemon for 38 minutes (D03.6), so
    "configured" is not the question — "enforced" is.

    ``memory_limits_enforced`` is deliberately three-valued: ``True`` (a byte
    ceiling is set), ``False`` (the controller is there and the ceiling is
    ``max``, i.e. unlimited), and ``None`` — no cgroup file, or it could not
    be read, which is the *unknown* a laptop or container gives and must not
    read as either reassurance or alarm.
    """
    status: dict[str, Any] = {
        "memory_limits_enforced": None,
        "memory_max_bytes": None,
        "memory_max_source": None,
    }
    try:
        # "0::/system.slice/canary.service" — the unified hierarchy's entry.
        relative: str | None = None
        for line in self_cgroup.read_text().splitlines():
            hierarchy, _, group = line.partition("::")
            if hierarchy == "0" and group:
                relative = group.strip().lstrip("/")
                break
        if relative is None:
            return status
        memory_max = cgroup_root / relative / "memory.max"
        status["memory_max_source"] = str(memory_max)
        raw = memory_max.read_text().strip()
    except (OSError, ValueError):
        return status
    if raw == "max":
        status["memory_limits_enforced"] = False
        return status
    try:
        status["memory_max_bytes"] = int(raw)
    except ValueError:
        return status
    status["memory_limits_enforced"] = True
    return status
