"""R29.7: the public read routes answer what they answered before the split.

``register_public_routes`` was one 383-line function; it now calls three
registrars (``status_routes``, ``catalog_reads``, ``analysis_reads``). The
route-table snapshot pins method, path and auth, and ``test_dashboard_api`` pins
most bodies. This file pins ``(status, body)`` for the routes nothing else
pinned, and the ones whose body is a refusal. The literals were taken from the
tree *before* the split.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from src.config import CanaryConfig
from src.dashboard.api import create_app
from src.dashboard.readers import read_can_coverage
from src.dashboard.routes import register_public_routes
from src.paths import REPO_ROOT
from src.yaml_load import load_yaml
from tests.configs import isolated_config

_DIDS_YAML = """
schema_version: 1
vehicle: {make: Nissan, model: Xterra, year: 2008}
access:
  ecu_request_id: 0x7E0
  ecu_response_id: 0x7E8
  session: 0xC0
dids:
  - did: 0x1225
    name: ECM_AF_SENSOR_V_B1
    byte_start: 0
    byte_length: 2
    confidence: high
"""


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    return isolated_config(
        tmp_path,
        "daemon",
        storage=tmp_path / "storage",
        profiles={"enabled": False},
        collection={"obd_dids": load_yaml("[ECM_AF_SENSOR_V_B1, '1208', not_a_did]")},
    )


@pytest.fixture
def client(config: CanaryConfig, tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            config,
            anomalies_dir=tmp_path / "anomalies",
            control_token="secret",
            dids_path=tmp_path / "dids.yaml",
            bindings_path=tmp_path / "bindings.yaml",
            behavior_models_path=tmp_path / "behavior_models.yaml",
            test_profiles_dir=tmp_path / "test-profiles",
        )
    )


def _answer(client: TestClient, path: str, **params: Any) -> tuple[int, Any]:
    response = client.get(path, params=params)
    return response.status_code, response.json()


def test_identity_answers_without_a_token(client: TestClient) -> None:
    status, body = _answer(client, "/identity")
    assert status == 200
    assert body["api"] == "identity/1"
    assert body["product"] == "canary"
    assert set(body) == {
        "api",
        "dashboard_port",
        "git_sha",
        "host_kind",
        "hostname",
        "product",
        "profile",
        "version",
    }


def test_status_both_views_and_a_bad_view(client: TestClient) -> None:
    full_status, full = _answer(client, "/status")
    compact_status, compact = _answer(client, "/status", view="compact")
    assert (full_status, compact_status) == (200, 200)
    assert compact["view"] == "compact"
    assert {"accepted_baseline_overrides", "network", "latest_session"} <= set(full)
    assert full["accepted_baseline_overrides"]["active"] == 0
    assert full["network"] is None
    bad_status, bad = _answer(client, "/status", view="x")
    assert bad_status == 422
    assert bad["detail"][0]["loc"] == ["query", "view"]


def test_behavior_live_without_a_daemon_is_empty_not_an_error(client: TestClient) -> None:
    expected: dict[str, Any] = {
        "observed": 0,
        "timestamp": None,
        "operating_mode": None,
        "models": {},
    }
    assert _answer(client, "/models/behavior/live") == (200, expected)
    assert _answer(client, "/models/behavior/live", series=False) == (200, expected)


def test_dids_absent_catalog_is_the_empty_document(client: TestClient) -> None:
    assert _answer(client, "/dids") == (
        200,
        {"schema_version": None, "vehicle": {}, "access": None, "dids": [], "polled": []},
    )


def test_dids_polled_resolves_names_and_hex_ids_and_drops_the_rest(
    config: CanaryConfig, tmp_path: Path
) -> None:
    (tmp_path / "dids.yaml").write_text(_DIDS_YAML)
    client = TestClient(
        create_app(config, anomalies_dir=tmp_path / "an", dids_path=tmp_path / "dids.yaml")
    )
    status, body = _answer(client, "/dids")
    assert status == 200
    # ``1208`` is a bare hex id the catalog above does not hold; ``not_a_did`` is neither.
    assert body["polled"] == ["ECM_AF_SENSOR_V_B1"]
    assert [entry["name"] for entry in body["dids"]] == ["ECM_AF_SENSOR_V_B1"]


def test_test_profiles_without_a_directory_is_an_empty_catalog(client: TestClient) -> None:
    assert _answer(client, "/test-profiles") == (200, {"errors": [], "profiles": []})


def test_can_coverage_is_the_readers_catalog(client: TestClient, tmp_path: Path) -> None:
    status, body = _answer(client, "/can/coverage")
    assert status == 200
    assert body == read_can_coverage(tmp_path / "bindings.yaml")
    assert body["signal_count"] > 0 and body["decoded_id_count"] > 0


def test_obd_coverage_reports_the_catalog_with_nothing_responded(client: TestClient) -> None:
    status, body = _answer(client, "/obd/coverage")
    assert status == 200
    assert body["custom_pids"] == []
    assert body["responded_count"] == 0 and body["backed_off_count"] == 0
    assert {row["status"] for row in body["rows"]} == {"unsupported"}


def test_binned_refusals_are_400_with_the_value_named(client: TestClient) -> None:
    base = {"value": "obd:RPM", "x": "obd:RPM"}
    assert _answer(client, "/analysis/binned", **base, agg="sum") == (
        400,
        {"detail": "unsupported agg: 'sum'"},
    )
    assert _answer(client, "/analysis/binned", **base, units="x") == (
        400,
        {"detail": "unsupported units: 'x'"},
    )


def test_binned_without_its_required_parameters_is_422(client: TestClient) -> None:
    status, body = _answer(client, "/analysis/binned")
    assert status == 422
    assert [item["loc"] for item in body["detail"]] == [["query", "value"], ["query", "x"]]


def test_llm_cards_disabled_is_an_explicit_state(client: TestClient) -> None:
    assert _answer(client, "/analysis/llm-assist/cards") == (200, {"enabled": False})


def test_trip_summary_for_a_missing_session_is_404(client: TestClient) -> None:
    assert _answer(client, "/sessions/nope/trip-summary") == (
        404,
        {"detail": "no trip summary for session 'nope'"},
    )


def test_bad_anomalies_and_telemetry_parameters_are_422(client: TestClient) -> None:
    for path, name in (("/anomalies", "limit"), ("/telemetry/latest", "n")):
        status, body = _answer(client, path, **{name: 0})
        assert status == 422, path
        assert body["detail"][0]["loc"] == ["query", name]


def _bare_public_app(config: CanaryConfig, tmp_path: Path) -> FastAPI:
    app = FastAPI()
    register_public_routes(
        app,
        config=config,
        anomalies_dir=tmp_path / "an",
        models_dir=None,
        emitter=None,
        feedback_store=None,
        behavior_models_path=tmp_path / "behavior_models.yaml",
        bindings_path=tmp_path / "bindings.yaml",
        dids_path=tmp_path / "dids.yaml",
        test_profiles_dir=tmp_path / "test-profiles",
        replay_controller=None,
        health_monitor=None,
        behavior_live=None,
    )
    return app


def _refuse(*_args: Any, **_kwargs: Any) -> None:
    from src.control.workbench_jobs import WorkbenchRefusedError

    raise WorkbenchRefusedError(409, {"error": "pinned", "message": "bare app"})


def test_register_public_routes_alone_serves_a_refusal_from_a_bare_app(
    config: CanaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare-app entry point still installs the ``StatusError`` handler (R29.3)."""
    from src.dashboard.routes import analysis_reads

    monkeypatch.setattr(analysis_reads, "capture_deferral", _refuse)
    app = _bare_public_app(config, tmp_path)
    response = TestClient(app).get("/analysis/binned", params={"value": "a", "x": "b"})
    assert (response.status_code, response.json()) == (
        409,
        {"detail": {"error": "pinned", "message": "bare app"}},
    )


def test_the_analysis_registrar_installs_the_handler_when_called_on_its_own(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare app that mounts only these routes answers their refusal, not a 500."""
    from src.dashboard.routes import analysis_reads

    monkeypatch.setattr(analysis_reads, "capture_deferral", _refuse)
    app = FastAPI()
    analysis_reads.register_analysis_read_routes(app, config=config)
    response = TestClient(app).get("/analysis/binned", params={"value": "a", "x": "b"})
    assert (response.status_code, response.json()) == (
        409,
        {"detail": {"error": "pinned", "message": "bare app"}},
    )


#: Which route module serves each public route: the split R29.7 made, by concern.
OWNERS = {
    "src.dashboard.routes.status_routes": {
        "/identity",
        "/status",
        "/config",
        "/anomalies",
        "/telemetry/latest",
    },
    "src.dashboard.routes.catalog_reads": {
        "/models/behavior",
        "/models/behavior/live",
        "/bindings",
        "/dids",
        "/test-profiles",
        "/obd/coverage",
        "/can/coverage",
        "/obd/dtc",
        "/obd/monitors",
    },
    "src.dashboard.routes.analysis_reads": {
        "/analysis/binned",
        "/analysis/llm-assist/cards",
        "/sessions/{session_id}/trip-summary",
    },
}


def test_each_public_route_is_served_by_the_module_for_its_concern(
    config: CanaryConfig, tmp_path: Path
) -> None:
    """``public.py`` registers nothing itself, and no route is in two modules."""
    served: dict[str, set[str]] = {}
    for route in _bare_public_app(config, tmp_path).routes:
        if isinstance(route, APIRoute) and route.endpoint.__module__.startswith(
            "src.dashboard.routes"
        ):
            served.setdefault(route.endpoint.__module__, set()).add(route.path)
    assert served == OWNERS
    owned = [path for paths in OWNERS.values() for path in paths]
    assert len(owned) == len(set(owned)) == 17


_MOVED_OUT_OF_ROUTE_MODULES = {
    "vehicle_reading",
    "_vin_refusal",
    "_polled_did_names",
    "_decoded_did_rows",
    "_pending",
    "_edit_payload",
    "_delete_refusal",
}


def test_the_logic_r29_7_moved_is_not_defined_in_a_route_module_again() -> None:
    """A route module holds routes. These were domain logic in closures (and
    ``vehicle_reading`` a route module serving another); they live in
    ``src/control`` and ``dashboard/readers.py``."""
    import ast

    routes = REPO_ROOT / "src" / "dashboard" / "routes"
    found = sorted(
        f"{path.relative_to(routes)}:{node.name}"
        for path in routes.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in _MOVED_OUT_OF_ROUTE_MODULES
    )
    assert found == []


def test_a_control_router_does_not_import_the_rom_library_routes() -> None:
    """``system.py`` imported ``vehicle_reading`` from ``rom_library.py``: one route
    module serving another. The control routers take shared logic from ``src``."""
    import ast

    for path in sorted((REPO_ROOT / "src" / "dashboard" / "routes" / "control").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("src.dashboard.routes.rom_"), path.name
