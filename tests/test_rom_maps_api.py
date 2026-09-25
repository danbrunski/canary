"""The viewer never stores uploaded ROMs or changes calibration state."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import CanaryConfig
from src.dashboard.routes import rom_maps


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    rom_maps.register_rom_routes(app, config=config, resolved_control_token="secret")
    monkeypatch.setattr(
        rom_maps, "inspect_rom", lambda data, path, dids=None, **_kw: {"bytes": len(data)}
    )
    with TestClient(app) as client:
        yield client


def test_upload_auth_and_raw_body(client):
    assert client.post("/control/rom/inspect", content=b"ROM").status_code == 401
    headers = {"Authorization": "Bearer secret", "Content-Type": "application/octet-stream"}
    response = client.post("/control/rom/inspect", content=b"ROM", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"bytes": 3}


def test_upload_size_type_and_errors(client, monkeypatch):
    headers = {"Authorization": "Bearer secret", "Content-Type": "application/octet-stream"}
    assert (
        client.post(
            "/control/rom/inspect", content=b"x" * (rom_maps.MAX_ROM_UPLOAD + 1), headers=headers
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/control/rom/inspect", json={}, headers={"Authorization": "Bearer secret"}
        ).status_code
        == 415
    )

    def reject(data, path, dids=None, **_kw):
        raise ValueError("Unrecognized source hash")

    monkeypatch.setattr(rom_maps, "inspect_rom", reject)
    response = client.post("/control/rom/inspect", content=b"bad", headers=headers)
    assert response.status_code == 422
    assert "Unrecognized" in response.json()["detail"]


def test_profile_driven_slots_and_unverified_tables(tmp_path, monkeypatch):
    root = Path("data/nissan-xterra-2008")
    for name in ["rom_viewer.yaml", "tuning_providers.yaml", "tune_revisions.yaml"]:
        (tmp_path / name).write_text((root / name).read_text())
    fake = SimpleNamespace(
        definition=SimpleNamespace(map_slot=1, afr_numerator=1881.6),
        export=lambda: {
            "revision": "synthetic",
            "bfs_ms": [8, 12],
            "rpm": [1000, 2000],
            "afr": [[14.7, 13], [13, 12]],
            "raw": [[128, 145], [145, 157]],
        },
    )
    monkeypatch.setattr(rom_maps.TargetEditor, "from_bytes", lambda data, path: fake)
    before = set(tmp_path.iterdir())
    result = rom_maps.inspect_rom(
        b"synthetic", tmp_path, registry_path=tmp_path / "tune_revisions.yaml"
    )
    assert set(tmp_path.iterdir()) == before
    assert result["map_slots"] == [1, 2, 3, 4, 5]
    assert len(result["tables"]) == 10
    available = [t for t in result["tables"] if t["status"] == "available"]
    assert len(available) == 1
    assert available[0]["slot"] == 1 and available[0]["kind"] == "fuel_targets"
    assert all(not t["values"] for t in result["tables"] if t["status"] == "tbd")
    assert result["live"]["base_fuel_schedule"]["factor"] == 0.00049
    assert not result["matches_recorded_revision"]
    assert result["live"]["rpm"]["spec"] == "did:ECM_RPM"
    # W05.1: the available table carries its trace mapping and request
    # channels; the X axis is unverified, so it carries no scale.
    trace = available[0]["trace"]
    assert trace["x"]["axis_mapping"] == "unverified" and trace["x"]["scale"] is None
    assert trace["x"]["request_channel"] == "mem:fuel_axis"
    assert trace["y"]["channel"] == "mem:ECM_ENGINE_SPEED_RAM"
    assert trace["y"]["axis_mapping"] == "verified"
    assert all(t["trace"] is None for t in result["tables"] if t["status"] == "tbd")
    with pytest.raises(ValueError, match="empty"):
        rom_maps.inspect_rom(b"", tmp_path, registry_path=tmp_path / "tune_revisions.yaml")
