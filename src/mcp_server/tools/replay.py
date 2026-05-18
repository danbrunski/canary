"""MCP wrapper around the ``replay_window`` daemon job.

Submits via ``/control/jobs``, optionally polls until the job reaches a
terminal status, and (on success) returns the structured replay report the
job wrote to disk. The actual replay runs in the daemon's job runner so the
heavy imports (torch, scikit-learn, the full preprocessing pipeline) stay
out of this process — MCP stays lightweight.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError

DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TIMEOUT = 600.0
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def _build_params(
    *,
    session_id: str,
    start: str | None,
    end: str | None,
    if_model: str | None,
    lstm_model: str | None,
    anomalies_dir: str | None,
    max_rows: int | None,
    max_windows: int | None,
) -> dict[str, Any]:
    """Pack only the keys the user actually supplied; lets the daemon defaults win."""
    params: dict[str, Any] = {"session_id": session_id}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    if if_model is not None:
        params["if_model"] = if_model
    if lstm_model is not None:
        params["lstm_model"] = lstm_model
    if anomalies_dir is not None:
        params["anomalies_dir"] = anomalies_dir
    if max_rows is not None:
        params["max_rows"] = max_rows
    if max_windows is not None:
        params["max_windows"] = max_windows
    return params


def _resolve_result_path(job: dict[str, Any]) -> Path | None:
    """The replay JSON lives next to the log under <jobs_dir>/<job_id>.replay.json."""
    log_path = job.get("log_path")
    job_id = job.get("job_id")
    if not isinstance(log_path, str) or not isinstance(job_id, str):
        return None
    return Path(log_path).parent / f"{job_id}.replay.json"


async def _poll_until_terminal(
    client: DaemonClient,
    *,
    job_id: str,
    poll_interval: float,
    timeout: float,
) -> dict[str, Any]:
    """Poll ``/control/jobs/{id}`` until status is terminal or we time out."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        payload = await client.get_json(f"/control/jobs/{job_id}")
        job = payload.get("job") or payload
        status = job.get("status")
        if status in TERMINAL_STATUSES:
            return cast(dict[str, Any], job)
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(
                f"replay_window job {job_id} did not terminate within {timeout}s "
                f"(last status: {status!r})"
            )
        await asyncio.sleep(poll_interval)


async def replay_window(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    session_id: str,
    start: str | None = None,
    end: str | None = None,
    if_model: str | None = None,
    lstm_model: str | None = None,
    anomalies_dir: str | None = None,
    max_rows: int | None = None,
    max_windows: int | None = None,
    wait: bool = True,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Submit a ``replay_window`` job; optionally wait + return the report.

    Parameters
    ----------
    session_id:
        Session directory name (e.g. ``"2026-05-17T120000"``).
    start / end:
        Optional ISO-8601 bounds (inclusive). Omitted bounds replay the
        whole session.
    if_model / lstm_model:
        Optional artifact path overrides; defaults follow ``canary.yaml``.
    anomalies_dir:
        Optional override for where to look up historical events.
    max_rows / max_windows:
        Caps on the per-row IF score list and per-window LSTM list to keep
        the JSON response bounded; defaults are 5000 each.
    wait:
        Block until the job terminates (default). When ``False``, returns
        the queued ``JobRecord`` and the caller polls ``get_job`` /
        ``get_job_logs`` themselves.
    poll_interval / timeout:
        Polling cadence and overall wall-clock cap when ``wait=True``.

    Returns
    -------
    On success and ``wait=True``: ``{"ok": True, "job": {...}, "report": {...}}``.
    On ``wait=False``: ``{"ok": True, "job": {...}, "report": None}``.
    On failure: ``{"ok": False, "job": {...}, "error": "..."}``.
    """
    params = _build_params(
        session_id=session_id,
        start=start,
        end=end,
        if_model=if_model,
        lstm_model=lstm_model,
        anomalies_dir=anomalies_dir,
        max_rows=max_rows,
        max_windows=max_windows,
    )
    audit_payload: dict[str, Any] = {"params": params, "wait": wait}
    try:
        submission = await client.post_json(
            "/control/jobs", {"kind": "replay_window", "params": params}
        )
    except DaemonError as exc:
        audit.record(
            tool="replay_window",
            result="error",
            payload={**audit_payload, "error": str(exc)},
        )
        raise
    job = submission.get("job") or submission
    audit.record(
        tool="replay_window",
        result="ok",
        payload={**audit_payload, "job_id": job.get("job_id")},
    )

    if not wait:
        return {"ok": True, "job": job, "report": None}

    try:
        terminal = await _poll_until_terminal(
            client, job_id=str(job["job_id"]), poll_interval=poll_interval, timeout=timeout
        )
    except TimeoutError as exc:
        return {"ok": False, "job": job, "error": str(exc), "report": None}

    if terminal.get("status") != "succeeded":
        return {
            "ok": False,
            "job": terminal,
            "error": (
                f"replay_window finished with status={terminal.get('status')} "
                f"returncode={terminal.get('returncode')}"
            ),
            "report": None,
        }

    result_path = _resolve_result_path(terminal)
    if result_path is None or not result_path.exists():
        return {
            "ok": False,
            "job": terminal,
            "error": f"replay report not found (expected at {result_path})",
            "report": None,
        }
    try:
        report = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "job": terminal, "error": f"unreadable report: {exc}", "report": None}
    return {"ok": True, "job": terminal, "report": report}


__all__ = ["replay_window", "DEFAULT_POLL_INTERVAL", "DEFAULT_TIMEOUT"]
