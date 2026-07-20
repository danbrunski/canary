"""Tests for the fingerprint hook in ``src/control/classification.py`` (Task 4.2,
eval-longitudinal-and-dtc.md) — the daemon's on-demand classify route.
"""

from __future__ import annotations

from pathlib import Path

from src.analysis.fingerprint import FINGERPRINT_FILENAME, read_fingerprint
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
