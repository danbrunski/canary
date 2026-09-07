"""Tests for the MCP tool functions (read + write)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError
from src.mcp_server.tools import anomalies as anomalies_tool
from src.mcp_server.tools import behavior as behavior_tool
from src.mcp_server.tools import power as power_tool
from src.mcp_server.tools import sessions as sessions_tool
from src.mcp_server.tools import status as status_tool
from src.mcp_server.tools import telemetry as telemetry_tool
from src.mcp_server.tools import vehicle_ledger as vehicle_ledger_tool
from src.mcp_server.write_tools import captures as captures_tool
from src.mcp_server.write_tools import feedback as feedback_tool
from src.mcp_server.write_tools import power as power_write_tool
from src.mcp_server.write_tools import vehicle_ledger as vehicle_ledger_write_tool


async def _fake_daemon(routes: dict[str, Any]) -> DaemonClient:
    """Return a :class:`DaemonClient` whose handler dispatches off a route table.

    ``routes`` maps ``"GET /status"`` etc. to a response dict (or to a callable
    that takes the request and returns one). Missing routes return 404.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        if key not in routes:
            return httpx.Response(404, json={"detail": f"no fixture for {key}"})
        target = routes[key]
        body = target(request) if callable(target) else target
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    client = DaemonClient("http://daemon")
    # Replace the internal AsyncClient with a MockTransport-backed one so we
    # don't need a live server. ``await aclose()`` because we're already in
    # an event loop (pytest-asyncio).
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://daemon",
        transport=httpx.MockTransport(handler),
    )
    return client


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


async def test_get_status_delegates_to_daemon() -> None:
    """``get_status`` returns whatever ``GET /status`` returned."""
    client = await _fake_daemon({"GET /status": {"free_mb": 100.0, "vehicle": "Xterra"}})
    result = await status_tool.get_status(client)
    await client.aclose()
    assert result == {"free_mb": 100.0, "vehicle": "Xterra"}


async def test_get_recent_anomalies_filters_by_source_and_mode() -> None:
    """Client-side filtering of source + mode happens after the daemon call."""
    events = [
        {"id": "a", "source": "isolation_forest", "operating_mode": "idle"},
        {"id": "b", "source": "lstm_autoencoder", "operating_mode": "idle"},
        {"id": "c", "source": "isolation_forest", "operating_mode": "highway"},
    ]
    client = await _fake_daemon({"GET /anomalies": {"events": events, "count": len(events)}})
    result = await anomalies_tool.get_recent_anomalies(
        client, source="isolation_forest", mode="idle"
    )
    await client.aclose()
    assert [e["id"] for e in result["events"]] == ["a"]
    assert result["count"] == 1


async def test_get_recent_anomalies_forwards_since_param() -> None:
    """``since`` is forwarded as a query param when provided."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> dict[str, Any]:
        captured["url"] = str(request.url)
        return {"events": [], "count": 0}

    client = await _fake_daemon({"GET /anomalies": handler})
    await anomalies_tool.get_recent_anomalies(client, since="2026-05-17T00:00:00Z")
    await client.aclose()
    assert "since=2026-05-17T00%3A00%3A00Z" in captured["url"]


async def test_get_recent_telemetry_filters_channel() -> None:
    """Channel filter is applied after fetch."""
    rows = [
        {"channel": "RPM", "value_number": 800.0},
        {"channel": "SPEED", "value_number": 0.0},
        {"channel": "RPM", "value_number": 820.0},
    ]
    client = await _fake_daemon({"GET /telemetry/latest": {"rows": rows, "count": len(rows)}})
    result = await telemetry_tool.get_recent_telemetry(client, n=100, channel="RPM")
    await client.aclose()
    assert len(result["rows"]) == 2
    assert all(r["channel"] == "RPM" for r in result["rows"])


async def test_list_sessions_forwards_time_bounds_and_limit() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> dict[str, Any]:
        captured["params"] = dict(request.url.params)
        return {"sessions": [], "count": 0}

    client = await _fake_daemon({"GET /control/sessions": handler})
    await sessions_tool.list_sessions(
        client,
        since="2026-07-27T00:00:00Z",
        until="2026-07-28T00:00:00Z",
        limit=5,
    )
    await client.aclose()

    assert captured["params"] == {
        "since": "2026-07-27T00:00:00Z",
        "until": "2026-07-28T00:00:00Z",
        "limit": "5",
    }


async def test_get_session_timeseries_forwards_channels_and_rate() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> dict[str, Any]:
        captured["params"] = dict(request.url.params)
        return {"session_id": "2026-07-27T225711", "channels": {}}

    path = "/control/sessions/2026-07-27T225711/chart"
    client = await _fake_daemon({f"GET {path}": handler})
    result = await sessions_tool.get_session_timeseries(
        client,
        session_id="2026-07-27T225711",
        channels=["obd:RPM", "obd:ENGINE_LOAD"],
        resample_hz=5.0,
    )
    await client.aclose()

    assert result["session_id"] == "2026-07-27T225711"
    assert captured["params"] == {
        "channels": "obd:RPM,obd:ENGINE_LOAD",
        "resample_hz": "5.0",
    }


async def test_get_behavior_live_passthrough() -> None:
    """Behavior tools just forward the daemon response."""
    snapshot = {"observed": 42, "operating_mode": "idle", "models": {}}
    client = await _fake_daemon({"GET /models/behavior/live": snapshot})
    assert await behavior_tool.get_behavior_live(client) == snapshot
    await client.aclose()


async def test_get_behavior_live_defaults_series_false() -> None:
    """Default omits the rolling trail — this is what overflowed the MCP
    tool-result size cap with enough registered surfaces."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> dict[str, Any]:
        captured["url"] = str(request.url)
        return {"observed": 0, "operating_mode": None, "models": {}}

    client = await _fake_daemon({"GET /models/behavior/live": handler})
    await behavior_tool.get_behavior_live(client)
    await client.aclose()
    assert "series=false" in captured["url"]


async def test_get_behavior_live_include_series_true_forwards_it() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> dict[str, Any]:
        captured["url"] = str(request.url)
        return {"observed": 0, "operating_mode": None, "models": {}}

    client = await _fake_daemon({"GET /models/behavior/live": handler})
    await behavior_tool.get_behavior_live(client, include_series=True)
    await client.aclose()
    assert "series=true" in captured["url"]


async def test_get_power_status_passthrough() -> None:
    """``get_power_status`` is a pure read — forwards GET /control/power."""
    snapshot = {"power": {"watchdog_running": True, "soc": 88.0, "on_battery": False}}
    client = await _fake_daemon({"GET /control/power": snapshot})
    assert await power_tool.get_power_status(client) == snapshot
    await client.aclose()


# ---------------------------------------------------------------------------
# query_telemetry (sync, no daemon)
# ---------------------------------------------------------------------------


def test_query_telemetry_returns_ok_false_on_validation_error(tmp_path: Path) -> None:
    """Validator failures are returned as ``{"ok": False, ...}`` not raised."""
    from src.mcp_server.parquet_query import TelemetryQuery

    runner = TelemetryQuery(tmp_path)
    out = telemetry_tool.query_telemetry(runner, sql="DROP VIEW telemetry")
    assert out["ok"] is False
    assert "not allowed" in out["error"].lower() or "select" in out["error"].lower()


# ---------------------------------------------------------------------------
# Write tools — audit log assertions
# ---------------------------------------------------------------------------


async def test_start_capture_records_audit_on_success(tmp_path: Path) -> None:
    """A successful capture-start writes one ``ok`` audit entry."""
    client = await _fake_daemon({"POST /control/captures": {"id": "cap_1", "session": "S"}})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    result = await captures_tool.start_capture(
        client, audit, purpose="test drive", protocol="drive"
    )
    await client.aclose()
    assert result == {"id": "cap_1", "session": "S"}
    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["action"] == "mcp.start_capture"
    assert entries[0]["result"] == "ok"


async def test_start_capture_records_audit_on_failure(tmp_path: Path) -> None:
    """An error path still records the attempt with ``result="error"``."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "capture already active"})

    client = await _fake_daemon({"POST /control/captures": respond})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    with pytest.raises(DaemonError):
        await captures_tool.start_capture(client, audit, purpose="x")
    await client.aclose()
    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["result"] == "error"
    assert "capture already active" in entries[0]["payload"]["error"]


async def test_stop_capture_passes_optional_reason(tmp_path: Path) -> None:
    """``reason`` is sent as the request body when provided."""
    captured: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"closed": True})

    client = await _fake_daemon({"POST /control/captures/current/stop": respond})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    await captures_tool.stop_capture(client, audit, reason="arrived")
    await client.aclose()
    assert captured["body"] == {"reason": "arrived"}


async def test_tag_current_capture_sends_label_and_fields(tmp_path: Path) -> None:
    """``tag_current_capture`` posts label + optional fields and audits success."""
    captured: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"tagged": True})

    client = await _fake_daemon({"POST /control/captures/current/tags": respond})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    await captures_tool.tag_current_capture(
        client, audit, label="highway_cruise", fields={"speed_target": 70}
    )
    await client.aclose()
    assert captured["body"]["label"] == "highway_cruise"
    assert captured["body"]["fields"]["speed_target"] == 70
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["action"] == "mcp.tag_current_capture"


async def test_tag_current_capture_audits_failure(tmp_path: Path) -> None:
    """``tag_current_capture`` records ``result="error"`` when the daemon refuses."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no active capture"})

    client = await _fake_daemon({"POST /control/captures/current/tags": respond})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    with pytest.raises(DaemonError):
        await captures_tool.tag_current_capture(client, audit, label="x")
    await client.aclose()
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["result"] == "error"


async def test_stop_capture_audits_failure(tmp_path: Path) -> None:
    """Stop-failure path records the error in the audit log."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "no capture active"})

    client = await _fake_daemon({"POST /control/captures/current/stop": respond})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    with pytest.raises(DaemonError):
        await captures_tool.stop_capture(client, audit, reason="x")
    await client.aclose()
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["action"] == "mcp.stop_capture"
    assert entry["result"] == "error"


async def test_get_current_capture_passthrough() -> None:
    """``get_current_capture`` is a pure read — no audit, no rewriting."""
    snapshot = {"id": "cap_42", "purpose": "test"}
    client = await _fake_daemon({"GET /control/captures/current": snapshot})
    assert await sessions_tool.get_current_capture(client) == snapshot
    await client.aclose()


async def test_set_ups_keepalive_sends_minutes_and_audits_success(tmp_path: Path) -> None:
    """``set_ups_keepalive`` posts {minutes} and records an ``ok`` audit entry."""
    captured: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(
            200, json={"power": {"keepalive_remaining_s": 600.0}, "expiry": 1700.0}
        )

    client = await _fake_daemon({"POST /control/power/keepalive": respond})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    result = await power_write_tool.set_ups_keepalive(client, audit, minutes=10.0)
    await client.aclose()
    assert captured["body"] == {"minutes": 10.0}
    assert result["expiry"] == 1700.0
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["action"] == "mcp.set_ups_keepalive"
    assert entry["result"] == "ok"


async def test_set_ups_keepalive_audits_failure(tmp_path: Path) -> None:
    """A daemon-side rejection (e.g. runtime dir missing) audits as ``error``."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "UPS keep-alive file not writable"})

    client = await _fake_daemon({"POST /control/power/keepalive": respond})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    with pytest.raises(DaemonError):
        await power_write_tool.set_ups_keepalive(client, audit, minutes=5.0)
    await client.aclose()
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["action"] == "mcp.set_ups_keepalive"
    assert entry["result"] == "error"


async def test_record_feedback_audit_carries_label(tmp_path: Path) -> None:
    """Feedback writes audit entry with label + the event in payload."""
    client = await _fake_daemon({"POST /control/feedback": {"stored": True}})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    event = {"timestamp": "2026-07-27T23:02:33Z", "source": "isolation_forest", "score": 0.07}
    await feedback_tool.record_feedback(
        client,
        audit,
        event=event,
        label="false_positive",
        reason="dirt road, MAF spike expected",
    )
    await client.aclose()
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["payload"]["event"] == event
    assert entry["payload"]["label"] == "false_positive"


async def test_list_vehicle_events_delegates_to_daemon() -> None:
    client = await _fake_daemon(
        {"GET /control/vehicle-ledger": {"events": [{"id": "e1"}], "count": 1}}
    )
    result = await vehicle_ledger_tool.list_vehicle_events(client)
    assert result["count"] == 1
    await client.aclose()


async def test_record_vehicle_event_audit_carries_kind_and_title(tmp_path: Path) -> None:
    client = await _fake_daemon({"POST /control/vehicle-ledger": {"event": {"id": "e1"}}})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    await vehicle_ledger_write_tool.record_vehicle_event(
        client,
        audit,
        occurred_at="2026-07-19",
        kind="maintenance",
        title="Oil change",
        corpus_boundary=False,
    )
    await client.aclose()
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["action"] == "mcp.record_vehicle_event"
    assert entry["result"] == "ok"
    assert entry["payload"]["kind"] == "maintenance"
    assert entry["payload"]["title"] == "Oil change"


async def test_record_vehicle_event_audits_failure(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "vehicle ledger API is not available"})

    client = await _fake_daemon({"POST /control/vehicle-ledger": respond})
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    with pytest.raises(DaemonError):
        await vehicle_ledger_write_tool.record_vehicle_event(
            client,
            audit,
            occurred_at="2026-07-19",
            kind="repair",
            title="Head gasket",
        )
    await client.aclose()
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["action"] == "mcp.record_vehicle_event"
    assert entry["result"] == "error"


# ---------------------------------------------------------------------------
# Tuning capture — arm-time verification and the capture quality tool (C03.9)
# ---------------------------------------------------------------------------


async def test_arm_tuning_capture_verifies_through_the_daemons_quality_route(
    tmp_path: Path,
) -> None:
    """The arm-time check asks the daemon for the same verdict the sidecar
    gets — never re-derives it from a trailing /status window."""
    from src.mcp_server.write_tools import tuning_capture as tuning_capture_tool

    quality = {
        "schema_version": 2,
        "scope": "live",
        "verdict": "fail",
        "failures": [{"clause": "tuple_incomplete", "channels": ["RPM"]}],
        "tuples": {"complete": 0},
    }
    client = await _fake_daemon(
        {
            "POST /control/capture": {"capture_profile": {"name": "fuel-map"}},
            "GET /status": {"vehicle_state": {"state": "engine_running"}},
            "GET /control/capture/quality": quality,
        }
    )
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    result = await tuning_capture_tool.arm_tuning_capture(
        client, audit, profile="fuel-map", rate_hz=4.0, verify_seconds=0.001
    )
    await client.aclose()
    assert result["verdict"] == "fail"
    assert result["scope"] == "live"
    assert result["failing_constraints"] == ["tuple_incomplete"]
    assert result["rate_verification"]["requested_rate_hz"] == 4.0
    assert result["vehicle_state"] == "engine_running"


async def test_get_capture_quality_is_live_or_recomputed_by_session() -> None:
    from src.mcp_server.write_tools import tuning_capture as tuning_capture_tool

    client = await _fake_daemon(
        {
            "GET /control/capture/quality": {"scope": "live", "verdict": "pass"},
            "GET /control/capture/quality/_capture-fuel-map-2026-08-10T120000": {
                "session_id": "_capture-fuel-map-2026-08-10T120000",
                "recomputed": {"scope": "recompute"},
                "stored": {"scope": "trailing_snapshot"},
            },
        }
    )
    live = await tuning_capture_tool.get_capture_quality(client)
    assert live == {"scope": "live", "verdict": "pass"}
    historic = await tuning_capture_tool.get_capture_quality(
        client, session_id="_capture-fuel-map-2026-08-10T120000"
    )
    await client.aclose()
    assert historic["stored"]["scope"] == "trailing_snapshot"
    assert historic["recomputed"]["scope"] == "recompute"
