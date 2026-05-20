"""End-to-end tests for :func:`classify_session`.

The classifier driver is the user-facing surface for the labeling pipeline;
this test set pins the contract that retraining / dashboards / MCP tools
all read against. Individual event-detector tests live in
``tests/test_events_*.py``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from src.analysis.session_classifier import (
    CLASSIFICATION_FILENAME,
    CLASSIFIER_VERSION,
    DERIVED_TAGS_FILENAME,
    SCHEMA_VERSION,
    DetectedEvent,
    SessionClassification,
    classify_session,
    is_stale,
    read_classification,
    write_classification,
)
from src.config import CanaryConfig
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _config() -> CanaryConfig:
    return CanaryConfig.from_yaml(EXAMPLE_YAML)


def _write_session(
    root: Path,
    *,
    name: str = "2026-05-20T120000",
    n: int = 120,
    coolant_temp: float = 85.0,
) -> Path:
    """Synthesize a session with idle + city + idle, full OBD coverage."""
    session = root / name
    records: list[TelemetryRecord] = []
    for i in range(n):
        t = 1_779_300_000.0 + i
        # Half idle, half city, last quarter idle again — exercises segment
        # boundary extraction.
        if i < n // 4 or i >= 3 * n // 4:
            rpm, speed, throttle = 800.0, 0.0, 5.0
        else:
            rpm, speed, throttle = 1900.0, 35.0, 22.0
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", rpm, "rpm"),
                TelemetryRecord.obd(t, "SPEED", speed, "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", throttle, "%"),
                TelemetryRecord.obd(t, "COOLANT_TEMP", coolant_temp, "C"),
                TelemetryRecord.obd(t, "RUN_TIME", float(i + 1), "s"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_classify_session_writes_self_consistent_sidecar(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    classification = classify_session(session, _config())
    write_classification(session, classification)

    payload = json.loads((session / CLASSIFICATION_FILENAME).read_text())
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["classifier_version"] == CLASSIFIER_VERSION
    assert payload["session_id"] == session.name
    # Mode coverage sums to roughly session duration.
    total = sum(payload["summary"]["mode_seconds"].values())
    assert total > 0
    assert abs(total - payload["duration_seconds"]) < 5.0  # rounding tolerance


def test_classify_session_extracts_both_idle_and_city_segments(tmp_path: Path) -> None:
    session = _write_session(tmp_path, n=120)
    classification = classify_session(session, _config())
    modes = {segment.mode for segment in classification.mode_segments}
    assert "idle" in modes
    assert "city" in modes


def test_classify_session_picks_warmup_when_coolant_cold(tmp_path: Path) -> None:
    """Cold coolant routes the idle stretches to ``warmup``, mirroring the
    operator's actual cold-start scenario from this morning."""
    session = _write_session(tmp_path, coolant_temp=30.0, n=120)
    classification = classify_session(session, _config())
    modes = {segment.mode for segment in classification.mode_segments}
    # warmup mode picks up the stopped portions; the moving portion has
    # speed > 0 so it doesn't match warmup (which gates on speed_max=0).
    assert "warmup" in modes


def test_classify_session_raises_when_no_parquet(tmp_path: Path) -> None:
    session = tmp_path / "missing"
    session.mkdir()
    with pytest.raises(FileNotFoundError):
        classify_session(session, _config())


def test_is_stale_detects_missing_and_old_classifier_version(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    assert is_stale(None) is True
    classification = classify_session(session, _config())
    assert is_stale(classification) is False
    # Simulate a stale sidecar from an older classifier version.
    stale = SessionClassification(
        schema_version=classification.schema_version,
        classifier_version="0.0.0",
        classified_at=classification.classified_at,
        session_id=classification.session_id,
        duration_seconds=classification.duration_seconds,
        mode_seconds=classification.mode_seconds,
        event_counts=classification.event_counts,
        mode_segments=classification.mode_segments,
        events=classification.events,
        quality=classification.quality,
        disposition=classification.disposition,
    )
    assert is_stale(stale) is True


def test_read_classification_round_trip(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    classification = classify_session(session, _config())
    write_classification(session, classification)
    loaded = read_classification(session)
    assert loaded is not None
    assert loaded.session_id == classification.session_id
    assert loaded.mode_seconds == classification.mode_seconds
    assert {e.kind for e in loaded.events} == {e.kind for e in classification.events}


def test_classification_writes_derived_tags_jsonl(tmp_path: Path) -> None:
    """Auto-detected events go to ``derived_tags.jsonl``, not ``tags.jsonl``,
    so operator-emitted tags stay distinguishable from auto-emitted ones.
    """
    session = _write_session(tmp_path)
    classification = classify_session(session, _config())
    write_classification(session, classification)

    derived = session / DERIVED_TAGS_FILENAME
    if classification.events:
        assert derived.exists()
        lines = [json.loads(line) for line in derived.read_text().splitlines() if line.strip()]
        assert all(record["kind"] == "derived" for record in lines)
        assert all(record["classifier_version"] == CLASSIFIER_VERSION for record in lines)


def test_classifier_passes_through_custom_detectors(tmp_path: Path) -> None:
    """Injecting an event-detector callable lets future agents add ad-hoc
    detectors without touching the registry — keeps the classifier composable.
    """
    session = _write_session(tmp_path)

    def synthetic(segmented: pd.DataFrame, mode_segments: Sequence[object]) -> list[DetectedEvent]:
        del mode_segments
        if segmented.empty:
            return []
        return [
            DetectedEvent(
                timestamp="2026-05-20T12:00:00+00:00",
                kind="synthetic_marker",
                fields={"reason": "test"},
            )
        ]

    classification = classify_session(
        session,
        _config(),
        event_detectors=[synthetic],
    )
    assert any(event.kind == "synthetic_marker" for event in classification.events)


def test_disposition_quarantines_low_completeness(tmp_path: Path) -> None:
    """Simulate a flaky-OBD session where most rows have missing features."""
    session = tmp_path / "flaky"
    records: list[TelemetryRecord] = []
    for i in range(60):
        t = 1_779_300_000.0 + i
        # Only RPM — no SPEED / THROTTLE / COOLANT / RUN_TIME.
        records.append(TelemetryRecord.obd(t, "RPM", 800.0 + (i % 3), "rpm"))
    write_records_to_parquet(records, session, max_rows=10_000)
    classification = classify_session(session, _config())
    assert classification.disposition in {"quarantine", "review"}


def test_write_classification_is_idempotent(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    classification = classify_session(session, _config())
    write_classification(session, classification)
    first_bytes = (session / CLASSIFICATION_FILENAME).read_bytes()
    # Write a second time with the same content — should not raise, file
    # content unchanged.
    write_classification(session, classification)
    second_bytes = (session / CLASSIFICATION_FILENAME).read_bytes()
    assert first_bytes == second_bytes


def test_classified_at_fixed_when_now_passed(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    pinned = datetime.fromisoformat("2026-05-20T12:00:00+00:00")
    classification = classify_session(session, _config(), now=pinned)
    assert classification.classified_at == "2026-05-20T12:00:00Z"
