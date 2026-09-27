"""Tests for the `/control/vehicle-ledger` HTTP routes (Task 3.3)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.dashboard.api import create_app
from src.vehicle.vehicle_ledger import VehicleLedgerStore
from tests.configs import isolated_config


def _client(tmp_path: Path) -> tuple[TestClient, VehicleLedgerStore]:
    storage = tmp_path / "storage"
    (storage / "2026-05-17T120000").mkdir(parents=True)
    config = isolated_config(tmp_path, storage=storage)
    store = VehicleLedgerStore(tmp_path / "vehicle_ledger.jsonl")
    anomalies = tmp_path / "anomalies"
    anomalies.mkdir()
    app = create_app(
        config,
        anomalies_dir=anomalies,
        control_token="secret",
        vehicle_ledger_store=store,
    )
    return TestClient(app), store


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_post_vehicle_ledger_requires_auth(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.post(
        "/control/vehicle-ledger",
        json={"occurred_at": "2026-07-19", "kind": "maintenance", "title": "Oil change"},
    )
    assert resp.status_code == 401


def test_post_and_get_vehicle_ledger(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    resp = client.post(
        "/control/vehicle-ledger",
        headers=_auth(),
        json={
            "occurred_at": "2026-07-19",
            "kind": "mod",
            "title": "Cold air intake",
            "affected_channels": ["obd:MAF"],
            "corpus_boundary": True,
        },
    )
    assert resp.status_code == 201
    body = resp.json()["event"]
    assert body["kind"] == "mod"
    assert body["corpus_boundary"] is True
    assert store.get(body["id"]) is not None

    got = client.get("/control/vehicle-ledger", headers=_auth())
    assert got.status_code == 200
    listed = got.json()
    assert listed["count"] == 1
    assert listed["events"][0]["title"] == "Cold air intake"


def test_event_id_corrects_in_place(tmp_path: Path) -> None:
    """Re-posting under an existing id supersedes it without losing the original.

    The store is append-only and reads collapse to the latest line per id, but
    nothing exposed that through the API — correcting a typo'd entry meant
    hand-appending JSON to the ledger file over ssh.
    """
    client, store = _client(tmp_path)
    first = client.post(
        "/control/vehicle-ledger",
        headers=_auth(),
        json={
            "occurred_at": "2026-07-27",
            "kind": "mod",
            "title": "Fuel K x1.07",
            "details": "typo'd, and corpus_boundary was missed",
            "corpus_boundary": False,
        },
    )
    assert first.status_code == 201
    event_id = first.json()["event"]["id"]

    corrected = client.post(
        "/control/vehicle-ledger",
        headers=_auth(),
        json={
            "occurred_at": "2026-07-27",
            "kind": "mod",
            "title": "Fuel K x1.07",
            "details": "corrected",
            "corpus_boundary": True,
            "event_id": event_id,
        },
    )
    assert corrected.status_code == 201
    assert corrected.json()["event"]["id"] == event_id

    listed = client.get("/control/vehicle-ledger", headers=_auth()).json()
    assert listed["count"] == 1, "a correction must not read as a second event"
    assert listed["events"][0]["details"] == "corrected"
    assert listed["events"][0]["corpus_boundary"] is True

    # The superseded line is still on disk — corrections amend the view, not history.
    assert len(list(store.raw_records())) == 2


def test_invalid_kind_rejected(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.post(
        "/control/vehicle-ledger",
        headers=_auth(),
        json={"occurred_at": "2026-07-19", "kind": "bogus", "title": "?"},
    )
    assert resp.status_code == 422


def test_vehicle_ledger_503_when_not_configured(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    (storage / "2026-05-17T120000").mkdir(parents=True)
    config = isolated_config(tmp_path, storage=storage)
    anomalies = tmp_path / "anomalies"
    anomalies.mkdir()
    app = create_app(config, anomalies_dir=anomalies, control_token="secret")
    client = TestClient(app)
    resp = client.get("/control/vehicle-ledger", headers=_auth())
    assert resp.status_code == 503
