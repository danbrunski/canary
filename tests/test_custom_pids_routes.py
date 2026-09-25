"""Tests for the `/control/custom-pids` HTTP routes (Task 2.8c)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.dashboard.api import create_app
from tests.configs import isolated_config


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    storage = tmp_path / "storage"
    (storage / "2026-05-17T120000").mkdir(parents=True)
    custom_pids_path = tmp_path / "custom_pids.yaml"
    config = isolated_config(tmp_path, storage=storage, daemon={"custom_pids": custom_pids_path})
    anomalies = tmp_path / "anomalies"
    anomalies.mkdir()
    app = create_app(config, anomalies_dir=anomalies, control_token="secret")
    return TestClient(app), custom_pids_path


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


_OIL_PRESS = {
    "name": "OIL_PRESS",
    "display_name": "Oil pressure",
    "mode": 0x22,
    "pid": 0x1234,
    "formula": "A * 0.5",
    "unit": "psi",
}


def test_get_custom_pids_empty_when_no_file(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.get("/control/custom-pids", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"custom_pids": [], "count": 0}


def test_get_custom_pids_requires_auth(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.get("/control/custom-pids")
    assert resp.status_code == 401


def test_post_custom_pids_requires_auth(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.post("/control/custom-pids", json=_OIL_PRESS)
    assert resp.status_code == 401


def test_post_and_get_custom_pid(tmp_path: Path) -> None:
    client, path = _client(tmp_path)
    resp = client.post("/control/custom-pids", headers=_auth(), json=_OIL_PRESS)
    assert resp.status_code == 201
    body = resp.json()["custom_pid"]
    assert body["name"] == "OIL_PRESS"
    assert path.exists()

    got = client.get("/control/custom-pids", headers=_auth())
    assert got.status_code == 200
    listed = got.json()
    assert listed["count"] == 1
    assert listed["custom_pids"][0]["name"] == "OIL_PRESS"


def test_post_custom_pid_write_mode_rejected(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    bad = {**_OIL_PRESS, "mode": 0x2E}
    resp = client.post("/control/custom-pids", headers=_auth(), json=bad)
    assert resp.status_code == 400


def test_post_custom_pid_duplicate_name_rejected(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    first = client.post("/control/custom-pids", headers=_auth(), json=_OIL_PRESS)
    assert first.status_code == 201
    dupe = client.post("/control/custom-pids", headers=_auth(), json=_OIL_PRESS)
    assert dupe.status_code == 400


def test_post_custom_pid_bad_formula_rejected(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    bad = {**_OIL_PRESS, "formula": "__import__('os')"}
    resp = client.post("/control/custom-pids", headers=_auth(), json=bad)
    assert resp.status_code == 400


def test_delete_custom_pid(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    client.post("/control/custom-pids", headers=_auth(), json=_OIL_PRESS)
    resp = client.delete("/control/custom-pids/OIL_PRESS", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"name": "OIL_PRESS", "deleted": True}
    got = client.get("/control/custom-pids", headers=_auth())
    assert got.json()["count"] == 0


def test_delete_custom_pid_requires_auth(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.delete("/control/custom-pids/OIL_PRESS")
    assert resp.status_code == 401


def test_delete_missing_custom_pid_404(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.delete("/control/custom-pids/NOPE", headers=_auth())
    assert resp.status_code == 404


_TORQUE_CSV = (
    "Name,ShortName,ModeAndPID,Equation,Min Value,Max Value,Units,Header\n"
    "Oil Temp,OIL_TEMP,220F40,A-40,,,C,\n"
    "Boost,BOOST,220F41,(A*256+B)/10,,,kPa,\n"
)


def test_post_custom_pids_import(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.post(
        "/control/custom-pids/import",
        headers=_auth(),
        json={"csv_text": _TORQUE_CSV},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["added_count"] == 2
    assert {p["name"] for p in body["added"]} == {"OIL_TEMP", "BOOST"}
    assert body["warnings"] == []

    got = client.get("/control/custom-pids", headers=_auth())
    assert got.json()["count"] == 2


def test_post_custom_pids_import_requires_auth(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.post("/control/custom-pids/import", json={"csv_text": _TORQUE_CSV})
    assert resp.status_code == 401


def test_post_custom_pids_import_skips_duplicate(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    client.post(
        "/control/custom-pids",
        headers=_auth(),
        json={**_OIL_PRESS, "name": "OIL_TEMP"},
    )
    resp = client.post(
        "/control/custom-pids/import",
        headers=_auth(),
        json={"csv_text": _TORQUE_CSV},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["added_count"] == 1
    assert any("already exists" in w for w in body["warnings"])


def test_post_custom_pids_import_bad_csv_text_rejected(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.post("/control/custom-pids/import", headers=_auth(), json={"csv_text": ""})
    assert resp.status_code == 422
