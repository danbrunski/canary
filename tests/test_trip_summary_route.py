"""R29.1: ``GET /sessions/{id}/trip-summary`` never computes in the daemon's own process.

The first request for a session reads the whole stored session, which is the
work the bounded child exists for (AGENTS.md, "No analysis on the Pi while it
collects"). The route is public and its sidecar format is unchanged: a sidecar
already on disk is served directly; a missing one is computed in the workbench
child (job kind ``trip_summary``) and answers 409 ``analysis_deferred`` while a
capture records. A thread pool stands in for the spawn child so the test stays
fast; the child entry itself is called directly below.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.analysis.trip_summary_sidecar import SCHEMA_VERSION, TRIP_SUMMARY_FILENAME
from src.collection import tuning_profile
from src.config import CanaryConfig
from src.control import child_pool, workbench_jobs
from src.dashboard.api import create_app
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import isolated_config

SESSION_ID = "2026-07-19T120000"


def _write_session(storage: Path, name: str = SESSION_ID) -> Path:
    session = storage / name
    t0 = 1_779_400_000.0
    records = [TelemetryRecord.obd(t0 + i, "SPEED", 60.0, "km/h") for i in range(10)] + [
        TelemetryRecord.obd(t0 + i, "RPM", 2000.0, "rpm") for i in range(10)
    ]
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


@pytest.fixture
def route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path, list[str]]:
    storage = tmp_path / "storage"
    storage.mkdir()
    config = isolated_config(tmp_path, storage=storage)
    jobs: list[str] = []
    real = workbench_jobs.run_workbench_job

    def recording(kind: str, *args: Any, **kwargs: Any) -> Any:
        jobs.append(kind)
        return real(kind, *args, **kwargs)

    monkeypatch.setattr(workbench_jobs, "run_workbench_job", recording)
    monkeypatch.setattr(child_pool, "spawn_pool", lambda: ThreadPoolExecutor(max_workers=1))
    # The child caps its own RLIMIT_AS; in a thread it would cap the test process.
    monkeypatch.setattr(workbench_jobs, "limit_address_space", lambda nbytes: (nbytes, nbytes))
    # No control token: the route is public, and this proves it stayed so.
    app = create_app(config, anomalies_dir=tmp_path / "anomalies")
    return TestClient(app), storage, jobs


def _capture_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tuning_profile, "is_active", lambda: True)
    profile = tuning_profile.active_profile().model_copy(update={"name": "tuning"})
    monkeypatch.setattr(tuning_profile, "active_profile", lambda: profile)


def test_the_job_kind_exists_and_needs_nothing_but_the_config() -> None:
    assert "trip_summary" in workbench_jobs.JOB_KINDS
    assert "trip_summary" in workbench_jobs._CONFIG_ONLY_KINDS


def test_the_first_request_computes_in_the_bounded_child_and_later_ones_read_the_sidecar(
    route: tuple[TestClient, Path, list[str]],
) -> None:
    client, storage, jobs = route
    session = _write_session(storage)

    first = client.get(f"/sessions/{SESSION_ID}/trip-summary")

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["session_id"] == SESSION_ID
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["summary"]["distance_km"] > 0
    assert jobs == ["trip_summary"]  # computed in the child, not in the route
    assert (session / TRIP_SUMMARY_FILENAME).exists()

    again = client.get(f"/sessions/{SESSION_ID}/trip-summary")
    assert again.json() == body
    assert jobs == ["trip_summary"]  # the sidecar was served directly


def test_an_existing_sidecar_is_served_during_a_capture_without_a_job(
    route: tuple[TestClient, Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, storage, jobs = route
    _write_session(storage)
    body = client.get(f"/sessions/{SESSION_ID}/trip-summary").json()
    jobs.clear()
    _capture_recording(monkeypatch)

    response = client.get(f"/sessions/{SESSION_ID}/trip-summary")

    assert response.status_code == 200
    assert response.json() == body
    assert jobs == []


def test_the_first_request_during_a_capture_answers_409_analysis_deferred(
    route: tuple[TestClient, Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, storage, jobs = route
    session = _write_session(storage)
    _capture_recording(monkeypatch)

    response = client.get(f"/sessions/{SESSION_ID}/trip-summary")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "analysis_deferred"
    assert detail["reason"] == "capture_active"
    assert detail["profile"] == "tuning"
    assert SESSION_ID in detail["laptop_path"]
    assert jobs == []
    assert not (session / TRIP_SUMMARY_FILENAME).exists()


def test_a_sidecar_from_an_older_schema_is_recomputed_not_served(
    route: tuple[TestClient, Path, list[str]],
) -> None:
    client, storage, jobs = route
    session = _write_session(storage)
    (session / TRIP_SUMMARY_FILENAME).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION - 1, "session_id": SESSION_ID, "summary": {}})
    )

    response = client.get(f"/sessions/{SESSION_ID}/trip-summary")

    assert response.status_code == 200
    assert response.json()["schema_version"] == SCHEMA_VERSION
    assert jobs == ["trip_summary"]


def test_an_unknown_session_is_404_without_a_job(
    route: tuple[TestClient, Path, list[str]],
) -> None:
    client, _storage, jobs = route
    assert client.get("/sessions/2099-01-01T000000/trip-summary").status_code == 404
    assert jobs == []


def test_a_session_with_no_telemetry_is_404_from_the_child(
    route: tuple[TestClient, Path, list[str]],
) -> None:
    client, storage, jobs = route
    (storage / SESSION_ID).mkdir()

    response = client.get(f"/sessions/{SESSION_ID}/trip-summary")

    assert response.status_code == 404
    assert jobs == ["trip_summary"]


def test_a_second_session_while_the_child_is_busy_is_409_analysis_busy(
    route: tuple[TestClient, Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, storage, jobs = route
    _write_session(storage)
    monkeypatch.setitem(workbench_jobs._IN_FLIGHT, "other", object())

    response = client.get(f"/sessions/{SESSION_ID}/trip-summary")

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "analysis_busy"
    assert jobs == []


def test_the_child_entry_returns_the_sidecar_or_an_explicit_none(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    config: CanaryConfig = isolated_config(tmp_path, storage=storage)
    _write_session(storage)
    got = workbench_jobs.run_workbench_job(
        "trip_summary",
        {"session_id": SESSION_ID},
        config,
        bindings_path=None,
        dids_path=None,
    )
    assert got["trip_summary"]["session_id"] == SESSION_ID
    nothing = workbench_jobs.run_workbench_job(
        "trip_summary",
        {"session_id": "2099-01-01T000000"},
        config,
        bindings_path=None,
        dids_path=None,
    )
    assert nothing == {"trip_summary": None}
