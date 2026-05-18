"""Tests for the MCP ``replay_window`` tool.

The tool's job is to submit a daemon ``replay_window`` job, optionally poll
until terminal status, and (on success) read the result JSON the job wrote.
Tests mock all three steps via :class:`httpx.MockTransport`; no real daemon
or filesystem traversal beyond ``tmp_path`` is required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError
from src.mcp_server.tools import replay as replay_tool


async def _client_with_handler(handler: Any) -> DaemonClient:
    client = DaemonClient("http://daemon")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://daemon",
        transport=httpx.MockTransport(handler),
    )
    return client


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


async def test_submission_packs_only_supplied_params(tmp_path: Path) -> None:
    """Unset kwargs are *not* included in the params blob."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(
            201,
            json={
                "job": {
                    "job_id": "j-1",
                    "kind": "replay_window",
                    "status": "queued",
                    "log_path": str(tmp_path / "j-1.log"),
                }
            },
        )

    client = await _client_with_handler(handler)
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    out = await replay_tool.replay_window(
        client, audit, session_id="S1", start="2026-05-17T12:00:00Z", wait=False
    )
    await client.aclose()
    assert captured["body"]["kind"] == "replay_window"
    assert captured["body"]["params"] == {"session_id": "S1", "start": "2026-05-17T12:00:00Z"}
    assert out["ok"] is True
    assert out["job"]["job_id"] == "j-1"
    assert out["report"] is None  # wait=False short-circuits


async def test_audit_records_submission(tmp_path: Path) -> None:
    """Each submission appends one ok audit entry with the job_id."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "job": {
                    "job_id": "j-9",
                    "kind": "replay_window",
                    "status": "queued",
                    "log_path": str(tmp_path / "j-9.log"),
                }
            },
        )

    client = await _client_with_handler(handler)
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    await replay_tool.replay_window(client, audit, session_id="S2", wait=False)
    await client.aclose()
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["action"] == "mcp.replay_window"
    assert entry["payload"]["job_id"] == "j-9"


async def test_daemon_error_during_submission_is_raised(tmp_path: Path) -> None:
    """If POST /control/jobs 4xx's, the tool surfaces it and audits the error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "unsupported job kind"})

    client = await _client_with_handler(handler)
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    with pytest.raises(DaemonError):
        await replay_tool.replay_window(client, audit, session_id="S1")
    await client.aclose()
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["result"] == "error"


# ---------------------------------------------------------------------------
# Polling + result read
# ---------------------------------------------------------------------------


async def test_wait_until_succeeded_reads_report(tmp_path: Path) -> None:
    """When the job succeeds, read the report JSON beside the log."""
    log_path = tmp_path / "j-7.log"
    result_path = tmp_path / "j-7.replay.json"
    expected_report = {"session_id": "S1", "summary": {"rows_scored": 5}}
    result_path.write_text(json.dumps(expected_report))

    poll_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "job": {
                        "job_id": "j-7",
                        "kind": "replay_window",
                        "status": "queued",
                        "log_path": str(log_path),
                    }
                },
            )
        # GET /control/jobs/j-7 — first call returns running, second succeeded.
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            return httpx.Response(
                200,
                json={"job": {"job_id": "j-7", "status": "running", "log_path": str(log_path)}},
            )
        return httpx.Response(
            200,
            json={
                "job": {
                    "job_id": "j-7",
                    "status": "succeeded",
                    "log_path": str(log_path),
                    "returncode": 0,
                }
            },
        )

    client = await _client_with_handler(handler)
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    out = await replay_tool.replay_window(
        client, audit, session_id="S1", poll_interval=0.01, timeout=5.0
    )
    await client.aclose()
    assert out["ok"] is True
    assert out["report"] == expected_report
    assert out["job"]["status"] == "succeeded"


async def test_failed_job_returns_ok_false(tmp_path: Path) -> None:
    """Terminal status != succeeded → ok=False with an error and the job dict."""
    log_path = tmp_path / "j-5.log"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "job": {
                        "job_id": "j-5",
                        "kind": "replay_window",
                        "status": "queued",
                        "log_path": str(log_path),
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "job": {
                    "job_id": "j-5",
                    "status": "failed",
                    "log_path": str(log_path),
                    "returncode": 1,
                }
            },
        )

    client = await _client_with_handler(handler)
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    out = await replay_tool.replay_window(
        client, audit, session_id="S1", poll_interval=0.01, timeout=5.0
    )
    await client.aclose()
    assert out["ok"] is False
    assert "failed" in out["error"]
    assert out["report"] is None


async def test_missing_report_file_reports_ok_false(tmp_path: Path) -> None:
    """The daemon says succeeded but the result file is absent → structured error."""
    log_path = tmp_path / "j-3.log"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "job": {
                        "job_id": "j-3",
                        "kind": "replay_window",
                        "status": "queued",
                        "log_path": str(log_path),
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "job": {
                    "job_id": "j-3",
                    "status": "succeeded",
                    "log_path": str(log_path),
                    "returncode": 0,
                }
            },
        )

    client = await _client_with_handler(handler)
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    out = await replay_tool.replay_window(
        client, audit, session_id="S1", poll_interval=0.01, timeout=5.0
    )
    await client.aclose()
    assert out["ok"] is False
    assert "not found" in out["error"]


async def test_timeout_returns_ok_false(tmp_path: Path) -> None:
    """A job that never reaches terminal status times out gracefully."""
    log_path = tmp_path / "j-2.log"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "job": {
                        "job_id": "j-2",
                        "kind": "replay_window",
                        "status": "queued",
                        "log_path": str(log_path),
                    }
                },
            )
        return httpx.Response(
            200,
            json={"job": {"job_id": "j-2", "status": "running", "log_path": str(log_path)}},
        )

    client = await _client_with_handler(handler)
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    out = await replay_tool.replay_window(
        client, audit, session_id="S1", poll_interval=0.01, timeout=0.05
    )
    await client.aclose()
    assert out["ok"] is False
    assert "did not terminate" in out["error"]
