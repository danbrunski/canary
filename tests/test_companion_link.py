"""Companion mode on the daemon: ``/identity``, the ``companion`` config and ``/companion/*``.

The sync, publish, retry and auth cases are the standalone companion API's
(``tests/test_companion_api.py``, retired 2026-10-16), re-homed on the daemon
app where the routes now live.
"""

from __future__ import annotations

import base64
import json
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.companion.discovery import DaemonIdentity, DiscoveredPi
from src.companion.library import CanaryLibrary
from src.config import CanaryConfig
from src.dashboard.api import create_app
from src.dashboard.identity import host_kind
from src.dashboard.routes import companion_link
from tests.configs import isolated_config

AUTH = {"Authorization": "Bearer secret"}
IDENTITY = DaemonIdentity(
    product="canary",
    hostname="canary-8974",
    host_kind="raspberry_pi",
    version="0.3.0",
    profile="nissan-xterra-2008",
    dashboard_port=8000,
    source_path="/identity",
)


def found(base_url: str, hostname: str = "canary-8974", source: str = "sweep") -> DiscoveredPi:
    identity = DaemonIdentity(**{**IDENTITY.to_dict(), "hostname": hostname})
    return DiscoveredPi(
        base_url=base_url,
        host=base_url.split("//")[1].split(":")[0],
        source=source,
        identity=identity,
    )


def config_for(tmp_path: Path, **companion: Any) -> CanaryConfig:
    block = {"enabled": True, "library_root": str(tmp_path / "library"), "discover": False}
    block.update(companion)
    return isolated_config(tmp_path, companion=block)


def app_for(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch | None = None,
    request_json: Any = None,
    download: Any = None,
    discover_fn: Any = None,
    probe_fn: Any = None,
    **companion: Any,
) -> tuple[TestClient, CanaryLibrary]:
    config = config_for(tmp_path, **companion)
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    link: companion_link.CompanionLink = app.state.companion_link
    # The route closures hold the helpers they were registered with; swap
    # them on the link's app for the test's doubles through re-registration
    # when a test needs to intercept the Pi round trips.
    if request_json is not None or download is not None or discover_fn or probe_fn:
        from fastapi import FastAPI

        fresh = FastAPI()
        kwargs: dict[str, Any] = {}
        if request_json is not None:
            kwargs["request_json"] = request_json
        if download is not None:
            kwargs["download_session_export"] = download
        if discover_fn is not None:
            kwargs["discover_fn"] = discover_fn
        if probe_fn is not None:
            kwargs["probe_fn"] = probe_fn
        link = companion_link.register_companion_link_routes(
            fresh, config=config, resolved_control_token="secret", lib=link.lib, **kwargs
        )
        app = fresh
    return TestClient(app), link.lib


def _session_zip(session_id: str = "2026-05-17T120000") -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(f"{session_id}/manifest.json", json.dumps({"started_at": "now"}))
        archive.writestr(f"{session_id}/tags.jsonl", '{"label":"baseline"}\n')
        archive.writestr(f"{session_id}/telemetry-00000.parquet", b"parquet-ish")
    return buf.getvalue()


def _local_manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "artifact_format_version": 1,
        "model_type": "isolation_forest",
        "model_version": "if_v1",
        "created_at": "2026-05-13T12:00:00+00:00",
        "training_data_hash": "sha256:deadbeef",
        "training_sessions": ["2026-05-13T120000"],
        "input_rows": 1000,
        "hyperparams": {"n_estimators": 100, "contamination": 0.01},
        "schema": {"feature_columns": ["obd:RPM", "obd:SPEED"]},
    }
    base.update(overrides)
    return base


# -- /identity and the config ---------------------------------------------------------------


def test_the_daemon_names_itself_without_a_token(tmp_path: Path) -> None:
    config = isolated_config(tmp_path)
    client = TestClient(create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="s"))
    payload = client.get("/identity").json()
    assert payload["product"] == "canary" and payload["api"] == "identity/1"
    assert payload["host_kind"] in {"raspberry_pi", "host"}
    assert payload["dashboard_port"] == config.dashboard.port
    assert set(payload) == {
        "product",
        "api",
        "hostname",
        "host_kind",
        "profile",
        "version",
        "git_sha",
        "dashboard_port",
    }
    # The probe side reads exactly this shape.
    identity = DaemonIdentity.from_payload(payload, path="/identity", host="h")
    assert identity.hostname == payload["hostname"]


def test_host_kind_reads_the_device_tree(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.write_bytes(b"Raspberry Pi 5 Model B Rev 1.0\x00")
    assert host_kind(model) == "raspberry_pi"
    model.write_bytes(b"Some Laptop\x00")
    assert host_kind(model) == "host"
    assert host_kind(tmp_path / "absent") == "host"


def test_companion_mode_puts_the_library_under_the_state_dir_and_its_sessions_as_the_store(
    tmp_path: Path,
) -> None:
    raw = isolated_config(tmp_path).model_dump(mode="json")
    raw["daemon"]["state_dir"] = str(tmp_path / "state")
    raw["companion"] = {"enabled": True}
    config = CanaryConfig.model_validate(raw)
    assert config.companion.library_root == tmp_path / "state" / "library"
    assert config.collection.storage_path == tmp_path / "state" / "library" / "sessions"
    # No state dir at all: the companion's own default, under the home directory.
    homeless = isolated_config(tmp_path, companion={"enabled": True})
    assert homeless.daemon.state_dir is None
    assert homeless.companion.library_root == Path.home() / ".canary" / "library"
    explicit = isolated_config(tmp_path, companion={"enabled": True, "library_root": "~/elsewhere"})
    assert explicit.companion.library_root == Path.home() / "elsewhere"
    assert explicit.collection.storage_path == Path.home() / "elsewhere" / "sessions"
    off = isolated_config(tmp_path)
    assert off.companion.enabled is False and off.companion.library_root is None
    assert off.collection.storage_path == tmp_path / "storage"


def test_without_companion_mode_the_routes_do_not_exist(tmp_path: Path) -> None:
    client = TestClient(
        create_app(isolated_config(tmp_path), anomalies_dir=tmp_path / "a", control_token="s")
    )
    assert client.get("/companion/status").status_code == 404


# -- status, discovery and the link -----------------------------------------------------------


def test_status_names_the_configured_pi_and_an_idle_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CANARY_CONTROL_TOKEN", raising=False)
    client, lib = app_for(tmp_path, pi_host="pi.local:8000")
    status = client.get("/companion/status").json()
    assert status["mode"] == "local_library"
    assert status["library_root"] == str(lib.root)
    assert status["pi"] == {
        "base_url": "http://pi.local:8000",
        "identity": None,
        "source": "configured",
        "selected_at": None,
    }
    assert status["discovery"]["state"] == "idle"
    assert status["pi_token_present"] is False


def test_a_lone_pi_found_at_startup_is_linked_several_are_listed(tmp_path: Path) -> None:
    one = [found("http://192.168.0.42:8000")]
    config = config_for(tmp_path, discover=True)
    from fastapi import FastAPI

    app = FastAPI()
    link = companion_link.register_companion_link_routes(
        app, config=config, resolved_control_token="secret", discover_fn=lambda **_k: one
    )
    link.wait(10)
    status = TestClient(app).get("/companion/status").json()
    assert status["pi"]["source"] == "discovered"
    assert status["pi"]["identity"]["hostname"] == "canary-8974"
    assert status["discovery"]["state"] == "done" and len(status["discovery"]["found"]) == 1
    # Remembered: a second daemon on the same library starts linked and does not search.
    again = FastAPI()
    second = companion_link.register_companion_link_routes(
        again, config=config, resolved_control_token="secret", discover_fn=lambda **_k: []
    )
    assert second.pi()["source"] == "discovered" and second.discovery.state == "idle"

    two = [found("http://a:8000", "canary-aaaa"), found("http://b:8000", "canary-bbbb")]
    other = FastAPI()
    link = companion_link.register_companion_link_routes(
        other,
        config=config_for(tmp_path / "other", discover=True),
        resolved_control_token="secret",
        discover_fn=lambda **_k: two,
    )
    link.wait(10)
    status = TestClient(other).get("/companion/status").json()
    assert status["pi"] is None
    assert [f["identity"]["hostname"] for f in status["discovery"]["found"]] == [
        "canary-aaaa",
        "canary-bbbb",
    ]


def test_discover_on_demand_select_and_unlink(tmp_path: Path) -> None:
    calls: list[Any] = []

    def discover_fn(**kwargs: Any) -> list[DiscoveredPi]:
        calls.append(kwargs)
        return [found("http://192.168.0.42:8000")]

    def probe_fn(url: str) -> DaemonIdentity | None:
        return IDENTITY if url == "http://192.168.0.42:8000" else None

    client, lib = app_for(tmp_path, discover_fn=discover_fn, probe_fn=probe_fn)
    assert client.get("/companion/discover").status_code == 401
    listing = client.get("/companion/discover?sweep=false", headers=AUTH).json()
    assert calls == [{"sweep": False}]
    assert listing["found"][0]["identity"]["hostname"] == "canary-8974"
    # The on-demand search linked the lone find, as the startup one would.
    assert listing["pi"]["source"] == "discovered"

    refused = client.post("/companion/pi", json={"base_url": "nowhere:8000"}, headers=AUTH)
    assert refused.status_code == 400 and "no CANary daemon answers" in refused.json()["detail"]
    chosen = client.post("/companion/pi", json={"base_url": "192.168.0.42"}, headers=AUTH).json()
    assert chosen["pi"]["source"] == "selected"
    assert chosen["pi"]["base_url"] == "http://192.168.0.42:8000"
    assert json.loads((lib.root / "pi.json").read_text())["source"] == "selected"

    cleared = client.delete("/companion/pi", headers=AUTH).json()
    assert cleared["pi"] is None
    assert not (lib.root / "pi.json").exists()


def test_a_sync_without_any_pi_is_refused_by_name(tmp_path: Path) -> None:
    client, _lib = app_for(tmp_path)
    response = client.post("/companion/sync/sessions/x", json={}, headers=AUTH)
    assert response.status_code == 400
    assert "no Pi linked" in response.json()["detail"]


# -- sync, publish, retry (ported) ----------------------------------------------------------


def test_sync_session_from_pi_uses_control_export_and_optional_pin(tmp_path: Path) -> None:
    calls: list[tuple[str, str, str, str | None, dict[str, Any] | None]] = []

    def fake_json(
        host: str,
        method: str,
        path: str,
        *,
        token: str | None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append((host, method, path, token, body))
        if path.endswith("/export/checksum"):
            return {"session_id": "2026-05-17T130000", "sha256": "expected-digest"}
        return {"ok": True}

    download_calls: list[tuple[str, str, str | None, Path, str | None]] = []

    def fake_download(
        host: str,
        session_id: str,
        *,
        token: str | None,
        dest_dir: Path,
        expected_sha256: str | None = None,
    ) -> bytes:
        download_calls.append((host, session_id, token, dest_dir, expected_sha256))
        return _session_zip(session_id)

    client, lib = app_for(
        tmp_path, request_json=fake_json, download=fake_download, pi_host="pi.local:8000"
    )
    # The Pi token comes from the environment variable the config names.
    assert client.post("/companion/sync/sessions/x", json={}).status_code == 401
    response = client.post(
        "/companion/sync/sessions/2026-05-17T130000",
        json={"pin": True, "index_with_config": False, "token": "secret"},
        headers=AUTH,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["session"]["session_id"] == "2026-05-17T130000"
    assert body["sha256"] == "expected-digest"
    assert "action_id" in body
    assert calls[0] == (
        "http://pi.local:8000",
        "POST",
        "/control/sessions/2026-05-17T130000/pin",
        "secret",
        {"reason": "synced to companion library", "metadata": {}},
    )
    assert calls[1][:4] == (
        "http://pi.local:8000",
        "GET",
        "/control/sessions/2026-05-17T130000/export/checksum",
        "secret",
    )
    assert download_calls == [
        ("http://pi.local:8000", "2026-05-17T130000", "secret", lib.sync_dir, "expected-digest")
    ]
    actions = lib.list_actions()
    assert actions[-1]["kind"] == "sync_session"
    assert actions[-1]["status"] == "done"
    # The synced session is in the daemon's own session store.
    assert (lib.sessions_dir / "2026-05-17T130000" / "manifest.json").exists()


def test_sync_session_from_pi_marks_action_failed_on_error(tmp_path: Path) -> None:
    def fake_json(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"sha256": "does-not-matter"}

    def fake_download(*_a: Any, **_k: Any) -> bytes:
        raise RuntimeError("connection dropped mid-transfer")

    client, lib = app_for(
        tmp_path, request_json=fake_json, download=fake_download, pi_host="pi.local:8000"
    )
    response = client.post("/companion/sync/sessions/2026-05-17T130000", json={}, headers=AUTH)
    assert response.status_code == 502
    actions = lib.list_actions()
    assert actions[-1]["kind"] == "sync_session"
    assert actions[-1]["status"] == "failed"
    assert "connection dropped" in actions[-1]["detail"]


def _write_artifact(config: CanaryConfig) -> None:
    artifact = config.daemon.models_dir / "isolation_forest" / "model.joblib"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"model bytes")
    artifact.with_suffix(".manifest.json").write_text(json.dumps(_local_manifest()))


def test_publish_model_to_pi_posts_existing_local_artifact(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(
        host: str,
        method: str,
        path: str,
        *,
        token: str | None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append({"host": host, "method": method, "path": path, "token": token, "body": body})
        if path.endswith("/manifest"):
            return {"model_type": "isolation_forest", "manifest": _local_manifest()}
        if path == "/status":
            return {"health": {"active_model_version": "if_v1"}}
        return {"model": {"artifact_bytes": 11}}

    client, lib = app_for(tmp_path, request_json=fake_request, pi_host="http://canary.local:8000")
    _write_artifact(config_for(tmp_path))
    response = client.post(
        "/companion/models/isolation_forest/publish", json={"token": "pi-token"}, headers=AUTH
    )
    assert response.status_code == 200
    publish = response.json()["publish"]
    assert publish["target"] == "http://canary.local:8000"
    assert publish["status"] == "active"
    assert publish["compatibility"]["compatible"] is True
    assert "action_id" in publish
    upload_call = next(c for c in calls if c["path"] == "/control/models/isolation_forest/artifact")
    assert upload_call["token"] == "pi-token"
    assert upload_call["body"]["artifact_b64"] == base64.b64encode(b"model bytes").decode("ascii")
    assert upload_call["body"]["manifest"] == _local_manifest()
    assert lib.publish_history("if_v1")[0]["status"] == "active"


def test_publish_model_to_pi_reports_schema_incompatibility(tmp_path: Path) -> None:
    def fake_request(
        host: str,
        method: str,
        path: str,
        *,
        token: str | None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if path.endswith("/manifest"):
            return {
                "model_type": "isolation_forest",
                "manifest": _local_manifest(schema={"feature_columns": ["obd:RPM"]}),
            }
        if path == "/status":
            return {"health": {"active_model_version": "if_v0"}}
        return {"model": {"artifact_bytes": 11}}

    client, _lib = app_for(tmp_path, request_json=fake_request, pi_host="http://canary.local:8000")
    _write_artifact(config_for(tmp_path))
    response = client.post("/companion/models/isolation_forest/publish", json={}, headers=AUTH)
    assert response.status_code == 200
    publish = response.json()["publish"]
    assert publish["compatibility"]["compatible"] is False
    assert publish["compatibility"]["hot_swappable"] is False
    assert publish["status"] in {"accepted", "active", "uploaded"}


def test_publish_model_retryable_action_survives_a_failed_upload(tmp_path: Path) -> None:
    state = {"fail": True}

    def flaky_request(
        host: str,
        method: str,
        path: str,
        *,
        token: str | None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if path.endswith("/manifest"):
            return {"model_type": "isolation_forest", "manifest": None}
        if path == "/status":
            return {"health": {}}
        if state["fail"]:
            raise RuntimeError("POST /control/models/isolation_forest/artifact failed: offline")
        return {"model": {"artifact_bytes": 11}}

    client, lib = app_for(tmp_path, request_json=flaky_request, pi_host="http://canary.local:8000")
    _write_artifact(config_for(tmp_path))
    response = client.post("/companion/models/isolation_forest/publish", json={}, headers=AUTH)
    assert response.status_code == 400
    actions = lib.list_actions()
    assert actions[-1]["kind"] == "publish_model"
    assert actions[-1]["status"] == "failed"

    state["fail"] = False
    retry = client.post(f"/companion/actions/{actions[-1]['id']}/retry", headers=AUTH)
    assert retry.status_code == 200
    assert retry.json()["publish"]["status"] == "uploaded"
    assert lib.list_actions()[-1]["status"] == "done"
    state_view = client.get("/companion/sync/state", headers=AUTH).json()
    assert state_view["pi"]["base_url"] == "http://canary.local:8000"
    assert state_view["publish_history"]


def test_every_mutating_companion_route_needs_the_control_token(tmp_path: Path) -> None:
    client, _lib = app_for(tmp_path)
    assert client.get("/companion/status").status_code == 200
    for method, path in [
        ("GET", "/companion/discover"),
        ("POST", "/companion/pi"),
        ("DELETE", "/companion/pi"),
        ("POST", "/companion/sessions/import"),
        ("POST", "/companion/sync/sessions/x"),
        ("POST", "/companion/models/isolation_forest/publish"),
        ("GET", "/companion/actions"),
        ("POST", "/companion/actions/1/retry"),
        ("GET", "/companion/sync/state"),
    ]:
        response = client.request(
            method, path, json={"base_url": "x"} if method == "POST" else None
        )
        assert response.status_code == 401, (method, path, response.status_code)
