"""Tests for the fingerprint hook in ``src/control/classification.py`` (Task 4.2,
eval-longitudinal-and-dtc.md) — the daemon's on-demand classify route. Also
covers the Task 4.6 trend-alert hook that rides the same pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger
from src.analysis.reference_epoch import ReferenceEpoch, StatSummary, write_reference_epoch
from src.config import CanaryConfig
from src.control.classification import classify_one_session
from src.storage.parquet_writer import write_records_to_parquet
from src.storage.session_products import FINGERPRINT_FILENAME, read_fingerprint
from src.telemetry import TelemetryRecord
from tests.configs import example_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _config_with_storage(tmp_path: Path) -> CanaryConfig:
    config = example_config(EXAMPLE_YAML)
    return config.model_copy(
        update={"collection": config.collection.model_copy(update={"storage_path": tmp_path})}
    )


def _write_session(root: Path, *, name: str = "2026-05-20T120000", n: int = 60) -> Path:
    session = root / name
    records: list[TelemetryRecord] = []
    for i in range(n):
        t = 1_779_300_000.0 + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 800.0, "rpm"),
                TelemetryRecord.obd(t, "SPEED", 0.0, "km/h"),
                TelemetryRecord.obd(t, "COOLANT_TEMP", 85.0, "C"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_classify_one_session_also_writes_fingerprint(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    config = _config_with_storage(tmp_path)

    classify_one_session(config, session.name)

    fingerprint_path = session / FINGERPRINT_FILENAME
    assert fingerprint_path.exists()
    loaded = read_fingerprint(session)
    assert loaded is not None
    assert loaded.session_id == session.name


def test_classify_one_session_survives_broken_behavior_models_file(tmp_path: Path) -> None:
    """A malformed behavior_models.yaml must not block classification or the
    fingerprint sidecar — only the behavior-deviation stats are omitted."""
    session = _write_session(tmp_path)
    config = _config_with_storage(tmp_path)
    broken = tmp_path / "broken_behavior_models.yaml"
    broken.write_text("not: [valid, behavior, models\n")
    config = config.model_copy(
        update={"daemon": config.daemon.model_copy(update={"behavior_models": broken})}
    )

    payload = classify_one_session(config, session.name)
    assert payload["session_id"] == session.name
    assert (session / FINGERPRINT_FILENAME).exists()


# --------------------------------------------------------------------------- #
# Task 4.6 — trend alerting rides the same post-classify pass
# --------------------------------------------------------------------------- #


def _config_with_alarm_epoch(tmp_path: Path) -> CanaryConfig:
    """Storage + a reference epoch whose ``idle_rpm_mean`` is far enough from
    ``_write_session``'s flat 800 rpm that a SINGLE session already trips the
    CUSUM alarm (z=(800-750)/5=10, way past the default k=0.5/h=5.0)."""
    config = example_config(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(update={"storage_path": tmp_path}),
            "daemon": config.daemon.model_copy(
                update={
                    "reference_epoch": tmp_path / "reference_epoch.json",
                    "trend_alert_state": tmp_path / "trend_alert_state.json",
                    "anomalies_dir": tmp_path / "anomalies",
                    # Trend evaluation reads the catalog; keep it off the
                    # repo tree (R07.2).
                    "catalog_db": tmp_path / "catalog.db",
                }
            ),
        }
    )
    epoch = ReferenceEpoch(
        frozen_at="2026-05-01T00:00:00+00:00",
        sessions=("s0",),
        stats={
            "idle_rpm_mean": StatSummary(
                mean=750.0, std=5.0, quantiles={"p10": 745.0, "p90": 755.0}, n=5
            )
        },
    )
    write_reference_epoch(config.daemon.reference_epoch, epoch)
    return config


def test_classify_one_session_emits_trend_alert_via_standalone_emitter(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    config = _config_with_alarm_epoch(tmp_path)

    classify_one_session(config, session.name)

    log_files = list(config.daemon.anomalies_dir.glob("anomalies-*.jsonl"))
    assert len(log_files) == 1
    lines = [json.loads(line) for line in log_files[0].read_text().splitlines() if line]
    assert len(lines) == 1
    assert lines[0]["source"] == "trend"
    assert lines[0]["detail"]["stat"] == "idle_rpm_mean"
    assert lines[0]["session"] == session.name


def test_classify_one_session_routes_trend_alert_through_given_emitter(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    config = _config_with_alarm_epoch(tmp_path)
    other_dir = tmp_path / "other-anomalies"
    other_dir.mkdir()
    emitter = AnomalyEventEmitter(AnomalyEventLogger(other_dir), min_seconds_between_events=0)

    classify_one_session(config, session.name, emitter=emitter)

    assert not config.daemon.anomalies_dir.exists()
    assert list(other_dir.glob("anomalies-*.jsonl"))


# --------------------------------------------------------------------------- #
# R03.2 — the route runs the whole-session half in the bounded child
# --------------------------------------------------------------------------- #


def _route_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    row_budget: int | None = None,
    span_budget: float | None = None,
) -> tuple[TestClient, CanaryConfig, list[str]]:
    """App with the classify route; the child pool is a thread (the spawn path
    is ``test_workbench_analysis``'s real-child test) and records each job."""
    from concurrent.futures import ThreadPoolExecutor

    from src.control import workbench_jobs
    from src.dashboard.api import create_app

    config = _config_with_alarm_epoch(tmp_path)
    config = config.model_copy(
        update={
            "daemon": config.daemon.model_copy(
                update={
                    "catalog_db": tmp_path / "catalog.db",
                    "digest_row_budget": row_budget,
                    "digest_span_budget_seconds": span_budget,
                }
            )
        }
    )
    jobs: list[str] = []
    real = workbench_jobs.run_workbench_job

    def recording(kind: str, *args: Any, **kwargs: Any) -> Any:
        jobs.append(kind)
        return real(kind, *args, **kwargs)

    monkeypatch.setattr(workbench_jobs, "run_workbench_job", recording)
    monkeypatch.setattr(workbench_jobs, "_spawn_pool", lambda: ThreadPoolExecutor(max_workers=1))
    # The child caps its own RLIMIT_AS; in a thread it would cap the test process.
    monkeypatch.setattr(workbench_jobs, "limit_address_space", lambda nbytes: (nbytes, nbytes))
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    return TestClient(app), config, jobs


_AUTH = {"Authorization": "Bearer secret"}


def test_classification_route_computes_in_the_bounded_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _write_session(tmp_path)
    client, config, jobs = _route_client(tmp_path, monkeypatch)

    response = client.post(f"/control/sessions/{session.name}/classification", headers=_AUTH)

    assert response.status_code == 201, response.text
    assert response.json()["classification"]["session_id"] == session.name
    assert jobs == ["classify"]
    assert (session / FINGERPRINT_FILENAME).exists()
    # The parent half ran too: the trend alert and the catalog row.
    assert list(config.daemon.anomalies_dir.glob("anomalies-*.jsonl"))
    assert config.daemon.catalog_db.exists()


def test_classification_route_defers_while_a_capture_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _write_session(tmp_path)
    client, _config, jobs = _route_client(tmp_path, monkeypatch)
    monkeypatch.setattr("src.control.workbench_jobs.tuning_profile.is_active", lambda: True)

    response = client.post(f"/control/sessions/{session.name}/classification", headers=_AUTH)

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "analysis_deferred"
    assert jobs == []
    assert not (session / FINGERPRINT_FILENAME).exists()


def test_classification_route_refuses_over_the_span_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P02.1: classifying runs the part-by-part ``value_number`` chain, whose
    peak follows the recorded span, so the span budget refuses it."""
    session = _write_session(tmp_path)  # 59 s of span
    client, _config, jobs = _route_client(tmp_path, monkeypatch, span_budget=10)

    response = client.post(f"/control/sessions/{session.name}/classification", headers=_AUTH)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "over_budget"
    assert "h of recorded span" in detail["message"]
    assert jobs == []


def test_classification_route_is_not_budgeted_by_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row budget bounds whole-session raw reads; classifying no longer
    makes one (P02.1), so a drive over it still classifies."""
    session = _write_session(tmp_path)  # 180 rows
    client, _config, jobs = _route_client(tmp_path, monkeypatch, row_budget=10)

    response = client.post(f"/control/sessions/{session.name}/classification", headers=_AUTH)

    assert response.status_code == 201, response.text
    assert jobs == ["classify"]


def test_classification_route_keeps_its_400_and_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _config, jobs = _route_client(tmp_path, monkeypatch)

    bad = client.post("/control/sessions/_replay-2026-05-20T120000/classification", headers=_AUTH)
    missing = client.post("/control/sessions/2026-05-20T130000/classification", headers=_AUTH)

    assert bad.status_code == 400
    assert missing.status_code == 404
    # Plain-string details, as before the route moved into the child.
    assert "non-live-session" in bad.json()["detail"]
    assert "not found" in missing.json()["detail"]
    assert jobs == []
