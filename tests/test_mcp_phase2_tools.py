"""Tests for the Phase 2 tools (bindings, sessions, jobs, tuning, etc.)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError
from src.mcp_server.tools import bindings as bindings_tool
from src.mcp_server.tools import digest as digest_tool
from src.mcp_server.tools import dtc as dtc_tool
from src.mcp_server.tools import jobs as jobs_tool
from src.mcp_server.tools import monitors as monitors_tool
from src.mcp_server.tools import sessions as sessions_tool
from src.mcp_server.tools import trends as trends_tool
from src.mcp_server.tools import trip_summary as trip_summary_tool
from src.mcp_server.tools import tuning as tuning_tool
from src.mcp_server.write_tools import jobs as jobs_write_tool
from src.mcp_server.write_tools import sessions as sessions_write_tool
from src.mcp_server.write_tools import tuning as tuning_write_tool


async def _fake_daemon(routes: dict[str, Any], *, token: str | None = None) -> DaemonClient:
    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        if key not in routes:
            return httpx.Response(404, json={"detail": f"no fixture for {key}"})
        target = routes[key]
        body = target(request) if callable(target) else target
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    client = DaemonClient("http://daemon", token)
    await client._client.aclose()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client._client = httpx.AsyncClient(
        base_url="http://daemon",
        headers=headers,
        transport=httpx.MockTransport(handler),
    )
    return client


# ---------------------------------------------------------------------------
# bindings / DIDs / coverage
# ---------------------------------------------------------------------------


async def test_bindings_dids_coverage_passthrough() -> None:
    """All three forward the daemon body verbatim."""
    client = await _fake_daemon(
        {
            "GET /bindings": {"version": 3, "bindings": [{"can_id": "0x551"}]},
            "GET /dids": {"schema_version": 1, "dids": [{"id": "0x1101"}]},
            "GET /obd/coverage": {"supported": ["RPM"], "decoded": ["RPM"]},
        }
    )
    assert (await bindings_tool.get_bindings(client))["version"] == 3
    assert (await bindings_tool.get_dids(client))["schema_version"] == 1
    assert "supported" in await bindings_tool.get_obd_coverage(client)
    await client.aclose()


# ---------------------------------------------------------------------------
# DTC
# ---------------------------------------------------------------------------


async def test_get_dtc_passthrough() -> None:
    """``get_dtc`` forwards the cached DTC report verbatim, including staleness."""
    report = {
        "captured_at": "2026-07-19T12:00:00+00:00",
        "stored": [
            {
                "code": "P0300",
                "description": "Random/Multiple Cylinder Misfire Detected",
                "system": "powertrain",
                "generic": True,
                "curated": True,
            }
        ],
        "pending": [],
        "permanent": [],
        "mil_on": True,
        "dtc_count": 1,
        "stale": False,
    }
    client = await _fake_daemon({"GET /obd/dtc": report})
    result = await dtc_tool.get_dtc(client)
    await client.aclose()
    assert result == report


# ---------------------------------------------------------------------------
# Monitors (Task 3.1)
# ---------------------------------------------------------------------------


async def test_get_monitors_passthrough() -> None:
    """``get_monitors`` forwards the cached Mode 06 sweep verbatim."""
    report = {
        "captured_at": "2026-07-19T12:00:00+00:00",
        "misfire_counts": {"1": 3},
        "results": [
            {
                "monitor_id": 11,
                "test_id": 1,
                "unit_and_scaling_id": 16,
                "test_value": 3,
                "min_limit": 0,
                "max_limit": 255,
                "passed": True,
            }
        ],
        "stale": False,
    }
    client = await _fake_daemon({"GET /obd/monitors": report})
    result = await monitors_tool.get_monitors(client)
    await client.aclose()
    assert result == report


# ---------------------------------------------------------------------------
# Trends (Task 4.5)
# ---------------------------------------------------------------------------


async def test_get_trends_passthrough() -> None:
    """``get_trends`` forwards the corpus trend summary verbatim."""
    body = {
        "available": True,
        "frozen_at": "2026-06-01T00:00:00+00:00",
        "reference_sessions": 5,
        "stats": {
            "idle_rpm_mean": {
                "stat": "idle_rpm_mean",
                "n": 4,
                "latest_session": "2026-06-01T000003",
                "latest_value": 820.0,
                "reference_mean": 800.0,
                "reference_std": 10.0,
                "reference_band": [790.0, 810.0],
                "mann_kendall": None,
                "cusum": {"alarm": True, "direction": "upper"},
            }
        },
    }
    client = await _fake_daemon({"GET /control/trends": body})
    result = await trends_tool.get_trends(client)
    await client.aclose()
    assert result == body


async def test_get_trends_unavailable_passthrough() -> None:
    body = {"available": False, "reason": "no reference epoch frozen yet", "stats": {}}
    client = await _fake_daemon({"GET /control/trends": body})
    result = await trends_tool.get_trends(client)
    await client.aclose()
    assert result == body


# ---------------------------------------------------------------------------
# Trip summary (Task 1.5, engine-analysis-and-mapping.md)
# ---------------------------------------------------------------------------


async def test_get_trip_summary_passthrough() -> None:
    """``get_trip_summary`` forwards the sidecar body verbatim."""
    body = {
        "schema_version": 1,
        "computed_at": "2026-07-19T12:00:00Z",
        "session_id": "2026-07-19T120000",
        "summary": {
            "duration_s": 600.0,
            "distance_km": 12.3,
            "fuel_used_l": 1.1,
            "avg_economy_l_per_100km": 8.9,
            "avg_economy_mpg": 26.4,
            "co2_kg": 2.5,
            "idle_time_s": 30.0,
            "idle_fraction": 0.05,
            "max_speed_kph": 95.0,
            "avg_speed_kph": 60.0,
            "zero_to_60_mph_s": None,
            "quarter_mile_s": None,
            "quarter_mile_trap_kph": None,
        },
    }
    client = await _fake_daemon({"GET /sessions/2026-07-19T120000/trip-summary": body})
    result = await trip_summary_tool.get_trip_summary(client, session_id="2026-07-19T120000")
    await client.aclose()
    assert result == body


async def test_get_trip_summary_missing_session_raises_daemon_error() -> None:
    client = await _fake_daemon(
        {
            "GET /sessions/2099-01-01T000000/trip-summary": httpx.Response(
                404, json={"detail": "no trip summary for session '2099-01-01T000000'"}
            )
        }
    )
    with pytest.raises(DaemonError) as exc_info:
        await trip_summary_tool.get_trip_summary(client, session_id="2099-01-01T000000")
    await client.aclose()
    assert exc_info.value.status == 404


async def test_get_session_digest_passthrough() -> None:
    """``get_session_digest`` forwards the sidecar body verbatim, over the
    authenticated `/control/sessions/{id}/digest` route (auth-consistency
    follow-up to Task 5.6 — the digest aggregates the vehicle ledger and
    fingerprint z-scores, both otherwise only readable via authenticated
    /control/* routes), with the client's bearer token attached."""
    body = {
        "schema_version": 1,
        "computed_at": "2026-07-19T12:00:00Z",
        "session_id": "2026-07-19T120000",
        "duration_seconds": 600.0,
        "mode_seconds": {"highway": 600.0},
        "anomaly_counts_by_source": {"fused": 2},
        "top_behavior_deviations": [],
        "fingerprint_top_movers": [],
        "new_dtcs": [],
        "new_ledger_events": [],
    }

    def _authed(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer secret":
            return httpx.Response(401, json={"detail": "missing/invalid control token"})
        return httpx.Response(200, json=body)

    client = await _fake_daemon(
        {"GET /control/sessions/2026-07-19T120000/digest": _authed}, token="secret"
    )
    result = await digest_tool.get_session_digest(client, session_id="2026-07-19T120000")
    await client.aclose()
    assert result == body


async def test_get_session_digest_missing_session_raises_daemon_error() -> None:
    client = await _fake_daemon(
        {
            "GET /control/sessions/2099-01-01T000000/digest": httpx.Response(
                404, json={"detail": "no digest for session '2099-01-01T000000'"}
            )
        },
        token="secret",
    )
    with pytest.raises(DaemonError) as exc_info:
        await digest_tool.get_session_digest(client, session_id="2099-01-01T000000")
    await client.aclose()
    assert exc_info.value.status == 404


# ---------------------------------------------------------------------------
# Sessions (read)
# ---------------------------------------------------------------------------


async def test_list_sessions_and_get_session() -> None:
    """Read tools delegate cleanly to the dashboard."""
    client = await _fake_daemon(
        {
            "GET /control/sessions": {"sessions": [{"id": "S1"}, {"id": "S2"}], "count": 2},
            "GET /control/sessions/S1": {"session": {"id": "S1", "metadata": {}}},
        }
    )
    listing = await sessions_tool.list_sessions(client)
    detail = await sessions_tool.get_session(client, session_id="S1")
    await client.aclose()
    assert listing["count"] == 2
    assert detail["session"]["id"] == "S1"


async def test_dataset_health_and_training_runs() -> None:
    """Both forward whatever the daemon returns."""
    client = await _fake_daemon(
        {
            "GET /control/datasets/health": {"modes": {"idle": {"samples": 9001}}},
            "GET /control/training-runs": {"runs": [{"id": "run-1"}], "count": 1},
        }
    )
    health = await sessions_tool.get_dataset_health(client)
    runs = await sessions_tool.get_training_runs(client)
    await client.aclose()
    assert health["modes"]["idle"]["samples"] == 9001
    assert runs["count"] == 1


# ---------------------------------------------------------------------------
# Sessions (write)
# ---------------------------------------------------------------------------


async def test_set_session_metadata_audits(tmp_path: Path) -> None:
    """``set_session_metadata`` sends body, audits success."""
    captured: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"session_id": "S1", "metadata": {"k": "v"}})

    client = await _fake_daemon({"POST /control/sessions/S1/metadata": respond})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    result = await sessions_write_tool.set_session_metadata(
        client, audit, session_id="S1", metadata={"k": "v"}, merge=False
    )
    await client.aclose()
    assert captured["body"] == {"metadata": {"k": "v"}, "merge": False}
    assert result["metadata"] == {"k": "v"}
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["action"] == "mcp.set_session_metadata"


async def test_pin_session_with_reason(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"session": {"id": "S1"}, "metadata": {"pinned": True}})

    client = await _fake_daemon({"POST /control/sessions/S1/pin": respond})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    await sessions_write_tool.pin_session(
        client, audit, session_id="S1", reason="training candidate"
    )
    await client.aclose()
    assert captured["body"] == {"reason": "training candidate"}


async def test_unpin_session_audits_failure(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not pinned"})

    client = await _fake_daemon({"POST /control/sessions/S1/unpin": respond})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    with pytest.raises(DaemonError):
        await sessions_write_tool.unpin_session(client, audit, session_id="S1")
    await client.aclose()
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["result"] == "error"
    assert "not pinned" in entry["payload"]["error"]


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


async def test_jobs_read_tools() -> None:
    client = await _fake_daemon(
        {
            "GET /control/jobs": {"jobs": [{"id": "j-1"}], "count": 1},
            "GET /control/jobs/j-1": {"job": {"id": "j-1", "status": "running"}},
            "GET /control/jobs/j-1/logs": {"job_id": "j-1", "log": "line1\nline2"},
        }
    )
    assert (await jobs_tool.list_jobs(client))["count"] == 1
    assert (await jobs_tool.get_job(client, job_id="j-1"))["job"]["status"] == "running"
    assert "line1" in (await jobs_tool.get_job_logs(client, job_id="j-1"))["log"]
    await client.aclose()


async def test_start_job_passes_kind_and_params(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(201, json={"job": {"id": "j-9"}})

    client = await _fake_daemon({"POST /control/jobs": respond})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    await jobs_write_tool.start_job(client, audit, kind="retrain_if", params={"days": 14})
    await client.aclose()
    assert captured["body"] == {"kind": "retrain_if", "params": {"days": 14}}


async def test_cancel_job_audits(tmp_path: Path) -> None:
    client = await _fake_daemon(
        {"POST /control/jobs/j-1/cancel": {"job": {"id": "j-1", "status": "cancelled"}}}
    )
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    result = await jobs_write_tool.cancel_job(client, audit, job_id="j-1")
    await client.aclose()
    assert result["job"]["status"] == "cancelled"
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["action"] == "mcp.cancel_job"


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------


async def test_tuning_read_tools() -> None:
    client = await _fake_daemon(
        {
            "GET /control/tuning": {"tuning": {"version": 7, "global_threshold": 0.8}},
            "GET /control/feedback": {
                "feedback": [{"id": "fb-1"}],
                "count": 1,
                "summary": {"true_positive": 1},
            },
        }
    )
    tuning = await tuning_tool.get_tuning(client)
    feedback = await tuning_tool.get_feedback(client)
    await client.aclose()
    assert tuning["tuning"]["version"] == 7
    assert feedback["summary"]["true_positive"] == 1


async def test_set_tuning_audits_success(tmp_path: Path) -> None:
    client = await _fake_daemon(
        {"POST /control/tuning": {"tuning": {"version": 8, "global_threshold": 0.9}}}
    )
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    result = await tuning_write_tool.set_tuning(
        client,
        audit,
        config={"global_threshold": 0.9},
        change="loosen for highway",
        by="claude",
    )
    await client.aclose()
    assert result["tuning"]["version"] == 8
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["payload"]["change"] == "loosen for highway"
    assert entry["payload"]["by"] == "claude"


async def test_estimate_tuning_forwards_since(tmp_path: Path) -> None:
    """``since`` is passed as a query parameter."""
    captured: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"before": {"fp": 5}, "after": {"fp": 3}})

    client = await _fake_daemon({"POST /control/tuning/estimate": respond})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    await tuning_write_tool.estimate_tuning(
        client,
        audit,
        config={"global_threshold": 0.9},
        since="2026-05-15T00:00:00Z",
    )
    await client.aclose()
    assert "since=2026-05-15T00" in captured["url"]


async def test_revert_tuning_audits(tmp_path: Path) -> None:
    client = await _fake_daemon({"POST /control/tuning/revert": {"tuning": {"version": 9}}})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    result = await tuning_write_tool.revert_tuning(client, audit, version=3)
    await client.aclose()
    assert result["tuning"]["version"] == 9
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["payload"]["version"] == 3
