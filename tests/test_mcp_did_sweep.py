"""Tests for the MCP ``diff_did_snapshots`` tool.

Pure read: fetch each job's record over ``/control/jobs/{id}`` (mocked),
then read the ``<job_id>.did_sweep.json`` snapshot straight off
``tmp_path`` — no real daemon needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.mcp_server.client import DaemonClient
from src.mcp_server.tools import did_sweep as did_sweep_tool


async def _client_with_handler(handler: Any) -> DaemonClient:
    client = DaemonClient("http://daemon")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://daemon", transport=httpx.MockTransport(handler)
    )
    return client


def _job(
    job_id: str, jobs_dir: Path, *, status: str = "succeeded", kind: str = "did_sweep"
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "kind": kind,
        "status": status,
        "log_path": str(jobs_dir / f"{job_id}.log"),
    }


def _write_snapshot(jobs_dir: Path, job_id: str, *, label: str, values: dict[str, str]) -> None:
    (jobs_dir / f"{job_id}.did_sweep.json").write_text(
        json.dumps({"label": label, "captured_at": 1.0, "values": values})
    )


async def test_diff_reports_varying_constant_and_intermittent(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    _write_snapshot(
        jobs_dir,
        "job-a",
        label="control",
        values={"1100": "0a0b", "1101": "ff", "1102": "01"},
    )
    _write_snapshot(
        jobs_dir,
        "job-b",
        label="wot",
        values={"1100": "0a0b", "1101": "00", "1103": "02"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        job_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"job": _job(job_id, jobs_dir)})

    client = await _client_with_handler(handler)
    result = await did_sweep_tool.diff_did_snapshots(client, job_a="job-a", job_b="job-b")

    assert result["a"] == {"job_id": "job-a", "label": "control"}
    assert result["b"] == {"job_id": "job-b", "label": "wot"}
    assert result["counts"] == {"varying": 1, "constant": 1, "intermittent": 2}
    varying = {entry["did"]: entry for entry in result["varying"]}
    assert varying["0x1101"] == {"did": "0x1101", "a_hex": "ff", "b_hex": "00"}
    constant = {entry["did"] for entry in result["constant"]}
    assert constant == {"0x1100"}
    intermittent = {entry["did"] for entry in result["intermittent"]}
    assert intermittent == {"0x1102", "0x1103"}


async def test_diff_rejects_a_non_did_sweep_job(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"job": _job("job-a", jobs_dir, kind="corpus_report")})

    client = await _client_with_handler(handler)
    with pytest.raises(ValueError, match="not a did_sweep job"):
        await did_sweep_tool.diff_did_snapshots(client, job_a="job-a", job_b="job-b")


async def test_diff_rejects_an_unfinished_job(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"job": _job("job-a", jobs_dir, status="running")})

    client = await _client_with_handler(handler)
    with pytest.raises(ValueError, match="has not succeeded"):
        await did_sweep_tool.diff_did_snapshots(client, job_a="job-a", job_b="job-b")


async def test_diff_rejects_a_missing_snapshot_file(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"job": _job("job-a", jobs_dir)})

    client = await _client_with_handler(handler)
    with pytest.raises(ValueError, match="result not found"):
        await did_sweep_tool.diff_did_snapshots(client, job_a="job-a", job_b="job-b")
