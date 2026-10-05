"""The brain over the Pi's interfaces (E04.5): every ``/control/brain/*`` route
returns its one shared function's result, and every MCP brain tool returns the
same bytes through that route. ECU computation and measured engine response
stay distinct parts of an investigation card.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from src.config import CanaryConfig
from src.control import brain_reads
from src.dashboard.api import create_app
from src.dashboard.routes.control import brain as brain_routes
from src.dashboard.routes.rom_maps import application_model_report
from src.evidence import Hypothesis, memory_ledger_for
from src.mcp_server.client import DaemonClient
from src.mcp_server.tools import brain as brain_tool
from src.vehicle.profiles import active_registry, profile_dir
from tests.test_brain_jobs import SESSION, SLUG, scheduler, scoped_config, write_session
from tests.test_evidence_cards import _investigation
from tests.test_evidence_memory import _evidence, _vehicle

TOKEN = "secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def populated(tmp_path: Path) -> tuple[CanaryConfig, dict[str, str]]:
    """A finished twin job, an engine-response finding and an investigation
    citing both."""
    config = scoped_config(tmp_path)
    write_session(config)
    client = TestClient(create_app(config, anomalies_dir=tmp_path / "an", control_token=TOKEN))
    queued = client.post(
        "/control/brain/jobs",
        json={"kind": "twin_session_evaluation", "params": {"session": SESSION}},
        headers=AUTH,
    )
    assert queued.status_code == 202, queued.text
    done = asyncio.run(scheduler(config).step())
    assert done is not None and done.state == "completed" and done.result is not None
    (twin_id,) = done.result["evidence"]
    ledger = memory_ledger_for(config)
    measured = ledger.append(_evidence(vehicle=_vehicle(SLUG)))
    investigation = ledger.append(
        _investigation(
            vehicle=_vehicle(SLUG),
            observations=(measured.id,),
            hypotheses=(Hypothesis(label="the command is as decoded", supporting=(twin_id,)),),
            unknowns=("the commanded word under sustained enrichment",),
            next_measurements=("capture the command word with the X trace in one read",),
        )
    )
    return config, {
        "twin": twin_id,
        "measured": measured.id,
        "investigation": investigation.id,
        "job": done.job_id,
    }


def _mcp_client(app: Any) -> DaemonClient:
    client = DaemonClient("http://daemon", TOKEN)
    client._client = httpx.AsyncClient(
        base_url="http://daemon", headers=AUTH, transport=httpx.ASGITransport(app=app)
    )
    return client


def test_http_and_mcp_return_the_same_computed_facts(
    tmp_path: Path, populated: tuple[CanaryConfig, dict[str, str]]
) -> None:
    config, ids = populated
    app = create_app(config, anomalies_dir=tmp_path / "an", control_token=TOKEN)
    http = TestClient(app)

    def get(path: str, **params: Any) -> Any:
        response = http.get(path, params=params, headers=AUTH)
        assert response.status_code == 200, (path, response.text)
        return response.json()

    started = brain_routes._STARTED_AT
    cases = [
        (
            get("/control/brain/status"),
            brain_reads.brain_status(config, process_started_at=started),
            brain_tool.get_brain_status,
            {},
        ),
        (
            get("/control/brain/memory", kind="evidence", session=SESSION),
            brain_reads.memory_query(config, {"kind": "evidence", "session": SESSION}),
            brain_tool.query_memory,
            {"kind": "evidence", "session": SESSION},
        ),
        (
            get(f"/control/brain/evidence/{ids['twin']}"),
            brain_reads.evidence_record(config, ids["twin"]),
            brain_tool.resolve_evidence,
            {"evidence_id": ids["twin"]},
        ),
        (
            get(f"/control/brain/investigations/{ids['investigation']}/card"),
            brain_reads.investigation_card_payload(config, ids["investigation"]),
            brain_tool.get_investigation_card,
            {"investigation_id": ids["investigation"]},
        ),
        (
            get(f"/control/brain/twin/sessions/{SESSION}"),
            brain_reads.twin_session_evaluation(config, SESSION),
            brain_tool.get_twin_session_evaluation,
            {"session_id": SESSION},
        ),
        (
            get("/control/ecu/application-model"),
            application_model_report(profile_dir(config), revisions=active_registry(config)),
            brain_tool.get_ecu_application_model,
            {},
        ),
        (
            get("/control/brain/jobs"),
            brain_reads.brain_jobs(config),
            brain_tool.list_brain_jobs,
            {},
        ),
        (
            get(f"/control/brain/jobs/{ids['job']}"),
            brain_reads.brain_job(config, ids["job"]),
            brain_tool.get_brain_job,
            {"job_id": ids["job"]},
        ),
    ]

    async def through_mcp() -> list[Any]:
        client = _mcp_client(app)
        try:
            return [await tool(client, **kwargs) for _http, _direct, tool, kwargs in cases]
        finally:
            await client.aclose()

    via_mcp = asyncio.run(through_mcp())
    for (via_http, direct, tool, _kwargs), mcp in zip(cases, via_mcp, strict=True):
        assert via_http == direct, tool.__name__
        assert mcp == via_http, tool.__name__


def test_the_card_keeps_ecu_computation_and_engine_response_apart(
    tmp_path: Path, populated: tuple[CanaryConfig, dict[str, str]]
) -> None:
    config, ids = populated
    payload = brain_reads.investigation_card_payload(config, ids["investigation"])
    assert payload["parts"] == {
        "ecu_computation": [ids["twin"]],
        "engine_response": [ids["measured"]],
    }
    card = payload["card"]
    assert card["next_measurements"] and card["unknowns"]
    twin = brain_reads.twin_session_evaluation(config, SESSION)
    (evaluation,) = twin["result"]["evaluations"]
    assert evaluation["trace"][0].keys() >= {"t", "observed_phi", "predicted_phi", "state"}
    assert evaluation["capture_requirement"]["message"]
    assert twin["currency"]["program_current"] is True
    assert brain_reads.evidence_record(config, ids["twin"])["part"] == "ecu_computation"
    assert brain_reads.evidence_record(config, ids["measured"])["part"] == "engine_response"


def test_refusals_are_named(tmp_path: Path) -> None:
    config = scoped_config(tmp_path)
    http = TestClient(create_app(config, anomalies_dir=tmp_path / "an", control_token=TOKEN))
    assert http.get("/control/brain/jobs").status_code == 401
    missing = http.get("/control/brain/evidence/ev-000000000000000000000000", headers=AUTH)
    assert missing.status_code == 404
    bad_time = http.get("/control/brain/memory", params={"as_of": "2026-10-04"}, headers=AUTH)
    assert bad_time.status_code == 400
    unknown = http.post(
        "/control/brain/jobs",
        json={"kind": "twin_session_evaluation", "params": {"session": "2020-01-01T000000"}},
        headers=AUTH,
    )
    assert unknown.status_code == 404
    never = http.get(f"/control/brain/twin/sessions/{SESSION}", headers=AUTH).json()
    assert never["state"] == "not_evaluated"
