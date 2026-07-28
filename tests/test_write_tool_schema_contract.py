"""Contract: every MCP write tool's request body must satisfy its route model.

Two silent breakages motivated this, both found on 2026-07-27:

* ``start_capture`` sent ``metadata``; ``CaptureStartRequest`` calls the field
  ``fields``. Every call that used the parameter 422'd.
* ``record_feedback`` sent ``{anomaly_id, label, note}``; ``FeedbackRequest``
  wants ``{event, label, reason}``. *Every* call 422'd, so the active-labeling
  loop was unreachable over MCP for as long as it had existed.

Neither showed up in testing because the write-tool tests mock the daemon with
a handler that accepts any JSON. Every control request model sets
``extra="forbid"``, so a body the model rejects is a hard failure in
production and a pass against the mock. This module closes that gap by
validating the real body against the real model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from src.dashboard import schemas
from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools import (
    can_activity,
    captures,
    feedback,
    obd_request,
    passive,
    power,
    replay,
    sessions,
    trigger_poll,
    tuning,
    tuning_mode,
    vehicle_ledger,
)

pytestmark = pytest.mark.asyncio


async def _capture_body(call: Any, tmp_path: Path) -> dict[str, Any]:
    """Run a write tool against a mock daemon and return the JSON it POSTed."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.content:
            seen.update(_json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    client = DaemonClient("http://daemon")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://daemon", transport=httpx.MockTransport(handler)
    )
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    await call(client, audit)
    await client.aclose()
    return seen


#: (label, invoke-the-tool, request model it must satisfy). Each tool is called
#: with *every* optional argument populated — an unused parameter is exactly
#: how both regressions hid.
CASES: list[tuple[str, Any, type]] = [
    (
        "start_capture",
        lambda c, a: captures.start_capture(
            c,
            a,
            purpose="post-flash validation",
            protocol="drive",
            fields={"tune_revision": "ZS44A + K x1.07"},
            vehicle_state="engine_running",
        ),
        schemas.CaptureStartRequest,
    ),
    (
        "tag_current_capture",
        lambda c, a: captures.tag_current_capture(c, a, label="wot_pull_3rd", fields={"gear": 3}),
        schemas.CaptureTagRequest,
    ),
    (
        "stop_capture",
        lambda c, a: captures.stop_capture(c, a, reason="drive complete"),
        schemas.CaptureStopRequest,
    ),
    (
        "record_feedback",
        lambda c, a: feedback.record_feedback(
            c,
            a,
            event={"timestamp": "2026-07-27T23:02:33Z", "source": "isolation_forest"},
            label="false_positive",
            reason="tuning mode narrowed the rotation",
            driving_context={"mode": "city"},
            labeled_by="mcp",
            hypothesis="stale GPS speed features",
            clarifying_question="was SPEED being polled?",
            clarifying_answer="no",
        ),
        schemas.FeedbackRequest,
    ),
    (
        "record_vehicle_event",
        lambda c, a: vehicle_ledger.record_vehicle_event(
            c,
            a,
            occurred_at="2026-07-27",
            kind="mod",
            title="Fuel K x1.07",
            details="first correction for the 8% lean WOT miss",
            odometer_km=123456.0,
            affected_channels=["SHORT_FUEL_TRIM_1"],
            corpus_boundary=True,
            event_id="abc123",
        ),
        schemas.VehicleEventRequest,
    ),
    (
        "set_passive_mode",
        lambda c, a: passive.set_passive_mode(c, a, enabled=True, ttl_seconds=300.0),
        schemas.PassiveModeRequest,
    ),
    (
        "can_activity",
        lambda c, a: can_activity.can_activity(c, a, seconds=5.0, ids=[0x100, 0x200]),
        schemas.CanActivityRequest,
    ),
    (
        "obd_request",
        lambda c, a: obd_request.obd_request(
            c, a, service=0x22, identifier=0x1101, header=0x7E0, timeout_seconds=3.0
        ),
        schemas.ObdRequestRequest,
    ),
    (
        "trigger_poll",
        lambda c, a: trigger_poll.trigger_poll(c, a, channels=["RPM", "DTC_STORED"]),
        schemas.TriggerPollRequest,
    ),
    (
        "set_tuning_mode",
        lambda c, a: tuning_mode.set_tuning_mode(c, a, enabled=True, channels=["RPM", "MAF"]),
        schemas.TuningModeRequest,
    ),
    (
        "set_tuning",
        lambda c, a: tuning.set_tuning(c, a, config={"k": 1}, change="bump", by="mcp"),
        schemas.TuningUpdateRequest,
    ),
    (
        "revert_tuning",
        lambda c, a: tuning.revert_tuning(c, a, version=3, by="mcp"),
        schemas.TuningRevertRequest,
    ),
    (
        "set_ups_keepalive",
        lambda c, a: power.set_ups_keepalive(c, a, minutes=5.0),
        schemas.UpsKeepaliveRequest,
    ),
    (
        "start_replay",
        lambda c, a: replay.start_replay(
            c, a, session_id="2026-07-27T120000", speed=2.0, loop=True
        ),
        schemas.ReplayStartRequest,
    ),
    (
        "set_session_metadata",
        lambda c, a: sessions.set_session_metadata(
            c, a, session_id="2026-07-27T120000", metadata={"note": "x"}, merge=True
        ),
        schemas.SessionMetadataRequest,
    ),
]


@pytest.mark.parametrize("label,call,model", CASES, ids=[c[0] for c in CASES])
async def test_write_tool_body_satisfies_route_model(
    label: str, call: Any, model: type, tmp_path: Path
) -> None:
    body = await _capture_body(call, tmp_path)
    # Guard against a vacuous pass: some models are all-optional, so an empty
    # captured body would validate and prove nothing.
    assert body, f"{label} POSTed no body — the contract check would be vacuous"
    # Raises on both a missing required field and an unexpected one, which are
    # the two ways these tools have actually broken.
    model(**body)
