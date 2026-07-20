"""Tests for the fingerprint hook in ``src/control/classification.py`` (Task 4.2,
eval-longitudinal-and-dtc.md) — the daemon's on-demand classify route. Also
covers the Task 4.6 trend-alert hook that rides the same pass.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger
from src.analysis.fingerprint import FINGERPRINT_FILENAME, read_fingerprint
from src.analysis.reference_epoch import ReferenceEpoch, StatSummary, write_reference_epoch
from src.config import CanaryConfig
from src.control.classification import classify_one_session
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _config_with_storage(tmp_path: Path) -> CanaryConfig:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
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
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(update={"storage_path": tmp_path}),
            "daemon": config.daemon.model_copy(
                update={
                    "reference_epoch": tmp_path / "reference_epoch.json",
                    "trend_alert_state": tmp_path / "trend_alert_state.json",
                    "anomalies_dir": tmp_path / "anomalies",
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
