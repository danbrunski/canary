"""Every MCP tool's name, description and schemas, pinned (D03.23).

``src/mcp_server/server.py`` is the assembly layer between FastMCP and the
``tools/`` / ``write_tools/`` delegates. Refactoring it has one silent failure
mode: FastMCP infers each tool's input schema and description from whatever
function it is handed, so a changed signature, a leaked ``client``/``audit``
parameter, a lost docstring or a dropped registration all still "work" — the
server starts, clients just see a different contract. This pins the whole
listing, built with every conditional tool's precondition satisfied.

Regenerate after an *intended* contract change::

    CANARY_UPDATE_MCP_SNAPSHOT=1 .venv/bin/python -m pytest -n0 tests/test_mcp_tool_snapshot.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from src.mcp_server.config import MCPConfig
from src.mcp_server.server import build_server

SNAPSHOT = Path(__file__).parent / "fixtures" / "mcp_tool_snapshot.json"


def _full_config(tmp_path: Path) -> MCPConfig:
    """Satisfy every conditional registration (storage, signals, config)."""
    storage = tmp_path / "storage"
    storage.mkdir()
    signals = tmp_path / "signals.json"
    signals.write_text("{}")
    config_file = tmp_path / "canary.yaml"
    config_file.write_text("collection:\n  storage_path: /var/canary/sessions\n")
    return MCPConfig(
        daemon_url="http://127.0.0.1:8000",
        daemon_token=None,
        bind_host="127.0.0.1",
        bind_port=8001,
        control_token=None,
        audit_path=tmp_path / "audit.jsonl",
        storage_path=storage,
        signals_path=signals,
        log_fallback_path=None,
        daemon_unit="canary",
        anomalies_dir=tmp_path / "anomalies",
        config_path=config_file,
        mcp_stateless=False,
    )


async def _listing(tmp_path: Path) -> dict[str, Any]:
    server = build_server(_full_config(tmp_path))
    tools = await server.list_tools()
    return {
        tool.name: tool.model_dump(mode="json", exclude_none=True)
        for tool in sorted(tools, key=lambda t: t.name)
    }


async def test_tool_listing_matches_snapshot(tmp_path: Path) -> None:
    listing = await _listing(tmp_path)
    if os.environ.get("CANARY_UPDATE_MCP_SNAPSHOT"):
        SNAPSHOT.write_text(json.dumps(listing, indent=2, sort_keys=True) + "\n")
    expected = json.loads(SNAPSHOT.read_text())
    assert sorted(listing) == sorted(expected), "tool registrations added or dropped"
    for name, tool in expected.items():
        assert listing[name] == tool, f"tool {name!r} contract changed"


async def test_no_tool_exposes_injected_dependencies(tmp_path: Path) -> None:
    """``client``/``audit``/paths are injected by the assembly layer; a
    delegate's own signature leaking through would expose them as inputs."""
    listing = await _listing(tmp_path)
    injected = {"client", "audit", "query_runner", "config_path", "signals_path"}
    for name, tool in listing.items():
        params = set(tool["inputSchema"].get("properties", {}))
        assert not params & injected, (name, params & injected)
        assert tool.get("description"), f"{name} has no description"


async def test_delegated_tool_forwards_injected_client_and_audit(tmp_path: Path) -> None:
    """A table row reaches its delegate with ``client``/``audit`` bound and the
    tool inputs as keywords — through FastMCP's own call path."""
    from src.mcp_server import server as server_module

    seen: dict[str, Any] = {}

    async def fake_pin(client: Any, audit: Any, *, session_id: str) -> dict[str, Any]:
        seen.update(client=client, audit=audit, session_id=session_id)
        return {"ok": True}

    fake_pin.__name__ = "unpin_session"
    entry = server_module.Delegation(fake_pin, needs_audit=True)
    client, audit = object(), object()
    tool = server_module.bind_delegation(entry, client, audit)  # type: ignore[arg-type]
    mcp = FastMCP(name="t")
    mcp.tool()(tool)
    await mcp.call_tool("unpin_session", {"session_id": "2026-09-10T142711"})
    assert seen == {"client": client, "audit": audit, "session_id": "2026-09-10T142711"}
    listed = {t.name: t for t in await mcp.list_tools()}
    assert set(listed["unpin_session"].inputSchema["properties"]) == {"session_id"}


def test_mis_shaped_delegate_is_refused_at_registration() -> None:
    """A delegate whose leading parameters are not the injected ones would
    bind ``client`` to a tool input; registration refuses it instead."""
    from src.mcp_server import server as server_module

    async def positional_input(client: Any, session_id: str) -> dict[str, Any]:
        return {}

    async def no_audit(client: Any, *, session_id: str) -> dict[str, Any]:
        return {}

    with pytest.raises(TypeError):
        server_module.bind_delegation(server_module.Delegation(positional_input), object(), None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        server_module.bind_delegation(
            server_module.Delegation(no_audit, needs_audit=True),
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )


# --- R29.5: a closure may not restate its delegate's signature wrongly --------

#: Tools that stay hand-written closures (``cfg``-derived arguments,
#: conditional registration, or two delegates behind one tool): the delegate(s)
#: each forwards to, and the delegate parameters the closure fills itself and
#: so does not expose. A closure's tool inputs must be exactly its delegates'
#: parameters minus those. R29.4 (``get_binned_analysis`` dropped ``units``)
#: was this drift; a new closure that is not listed here fails the next test.
_INJECTED = frozenset({"client", "audit"})


def _closure_delegates() -> dict[str, tuple[tuple[Any, ...], frozenset[str]]]:
    from src.mcp_server.tools import (
        analysis,
        behavior,
        logs,
        search,
        service_health,
        sessions,
        telemetry,
        tuning_analysis,
    )
    from src.mcp_server.write_tools import capture_profiles, captures, models

    return {
        "get_session_timeseries": ((sessions.get_session_timeseries,), frozenset()),
        "tail_daemon_log": ((logs.tail_daemon_log,), frozenset({"fallback_path"})),
        "get_service_health": ((service_health.get_service_health,), frozenset()),
        "search_anomaly_log": (
            (search.search_anomaly_log,),
            frozenset({"anomalies_dir", "audit_path"}),
        ),
        "signal_lookup": ((analysis.signal_lookup,), frozenset({"catalog_path"})),
        "query_telemetry": ((telemetry.query_telemetry,), frozenset({"query_runner"})),
        "get_behavior_deviations": (
            (behavior.get_behavior_deviations,),
            frozenset({"config_path"}),
        ),
        # ``profile`` is the CLI's ``--profile``; the MCP tools always run on the
        # configured default profile, so it is deliberately not a tool input.
        "get_delivery_error": (
            (tuning_analysis.get_delivery_error,),
            frozenset({"config_path", "profile"}),
        ),
        "compare_tuning_sessions": (
            (tuning_analysis.compare_tuning_sessions,),
            frozenset({"config_path", "profile"}),
        ),
        # One tool, two delegates: a named profile starts a profile capture,
        # otherwise the legacy tag window.
        "start_capture": (
            (captures.start_capture, capture_profiles.start_capture),
            frozenset(),
        ),
        "stop_capture": ((captures.stop_capture,), frozenset()),
        "upload_model_artifact": ((models.upload_model_artifact,), frozenset()),
    }


async def test_every_closure_exposes_exactly_its_delegates_parameters(tmp_path: Path) -> None:
    import inspect

    from src.mcp_server import server as server_module

    listing = await _listing(tmp_path)
    closures = _closure_delegates()
    delegated = {
        entry.delegate.__name__
        for entry in (*server_module.READ_DELEGATIONS, *server_module.WRITE_DELEGATIONS)
    }
    unlisted = set(listing) - delegated - set(closures)
    assert not unlisted, f"closure tools with no declared delegate: {sorted(unlisted)}"
    assert not set(closures) & delegated, "a tool is both a Delegation row and a closure"
    for name, (delegates, hidden) in closures.items():
        expected: set[str] = set()
        for delegate in delegates:
            expected |= set(inspect.signature(delegate).parameters)
        expected -= _INJECTED | hidden
        assert set(listing[name]["inputSchema"].get("properties", {})) == expected, name


async def test_ledger_kind_literal_is_one_definition(tmp_path: Path) -> None:
    """The MCP tool, the daemon's request schema and ``VEHICLE_EVENT_KINDS``
    all carry the same kinds (R29.5)."""
    from src.dashboard.schemas import VehicleEventRequest
    from src.vehicle.vehicle_ledger import VEHICLE_EVENT_KINDS

    listing = await _listing(tmp_path)
    tool_kinds = set(listing["record_vehicle_event"]["inputSchema"]["properties"]["kind"]["enum"])
    route_kinds = set(VehicleEventRequest.model_json_schema()["properties"]["kind"]["enum"])
    assert tool_kinds == route_kinds == set(VEHICLE_EVENT_KINDS)


async def test_feedback_label_literal_is_one_definition(tmp_path: Path) -> None:
    """The MCP tool, the daemon's request schema and ``FEEDBACK_LABELS`` carry
    the same verdicts. The tool once offered ``unknown`` (the daemon refuses
    it) and could not send ``ignored`` or ``needs_review`` (R29.5)."""
    from src.dashboard.schemas import FeedbackRequest
    from src.models.feedback import FEEDBACK_LABELS

    listing = await _listing(tmp_path)
    tool_labels = set(listing["record_feedback"]["inputSchema"]["properties"]["label"]["enum"])
    route_labels = set(FeedbackRequest.model_json_schema()["properties"]["label"]["enum"])
    assert tool_labels == route_labels == set(FEEDBACK_LABELS)
