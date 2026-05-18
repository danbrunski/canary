"""Daemon-log tail tool.

The daemon writes single-line JSON to stdout via :mod:`src.log`; on the Pi
systemd captures that into the journal. Falls back to reading a plain log
file when ``journalctl`` isn't available (dev machines without systemd).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _read_journal(unit: str, *, n: int, since: str | None) -> list[dict[str, Any]]:
    """Return the last ``n`` journal entries for ``unit`` as parsed records.

    Uses ``journalctl --output=json``; each line is one event. Errors
    (missing binary, unit not found, parse failure) are swallowed and
    surfaced as a single ``error`` record so the tool always returns
    something usable to the LLM.
    """
    cmd = ["journalctl", "--unit", unit, "-n", str(n), "--output=json", "--no-pager"]
    if since is not None:
        cmd += ["--since", since]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    if proc.returncode != 0:
        return [{"error": proc.stderr.strip() or f"journalctl exit={proc.returncode}"}]
    records: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            records.append({"raw": line})
    return records


def _read_fallback_file(path: Path, *, n: int) -> list[dict[str, Any]]:
    """Last ``n`` lines of a JSON-lines log file. Used on non-systemd hosts."""
    if not path.exists():
        return []
    # Cheap "last n lines": read the whole file. CANary logs are short and
    # rotated; if this ever becomes a hot path we'll switch to seek-from-end.
    lines = path.read_text(errors="replace").splitlines()[-n:]
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            records.append({"raw": line})
    return records


def tail_daemon_log(
    *,
    n: int = 100,
    since: str | None = None,
    unit: str = "canary",
    fallback_path: Path | None = None,
) -> dict[str, Any]:
    """Recent daemon log lines, parsed as JSON when possible.

    Parameters
    ----------
    n:
        Number of lines to return (default 100, capped at 2000 by ``journalctl``).
    since:
        Optional ``journalctl --since`` value, e.g. ``"5 minutes ago"`` or
        an ISO timestamp.
    unit:
        Systemd unit name. Defaults to ``"canary"``; pass ``"canary-mcp"`` to
        see this server's own log.
    fallback_path:
        File to read when ``journalctl`` is unavailable. The daemon emits
        single-line JSON via :mod:`src.log` so each line parses cleanly.
    """
    n = max(1, min(int(n), 2000))
    if shutil.which("journalctl") is not None:
        records = _read_journal(unit, n=n, since=since)
        source = "journal"
    elif fallback_path is not None:
        records = _read_fallback_file(fallback_path, n=n)
        source = f"file:{fallback_path}"
    else:
        records = []
        source = "unavailable"
    return {"source": source, "unit": unit, "count": len(records), "records": records}


__all__ = ["tail_daemon_log"]
