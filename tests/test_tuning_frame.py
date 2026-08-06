from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.analysis.afr import load_afr_calibration
from src.analysis.tuning_frame import build_tuning_frame, section_capabilities
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord


def test_all_did_frame_uses_confirmed_semantic_roles(tmp_path: Path) -> None:
    session = tmp_path / "_capture-tuning-did-2026-08-05T120000"
    timestamp = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    write_records_to_parquet(
        [
            TelemetryRecord(timestamp=timestamp, source="obd", channel="DID_1219", value="09c4"),
            TelemetryRecord(timestamp=timestamp, source="obd", channel="DID_1225", value="01b8"),
            TelemetryRecord(timestamp=timestamp, source="obd", channel="DID_113A", value="80"),
            TelemetryRecord(timestamp=timestamp, source="obd", channel="DID_1135", value="81"),
            TelemetryRecord(timestamp=timestamp, source="obd", channel="DID_1163", value="82"),
        ],
        session,
    )
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "profile": "tuning-did",
                "started_at": timestamp.isoformat(),
                "tune_revision": "test-revision",
                "profile_snapshot": {"corpus_eligible": False},
            }
        )
    )

    frame = build_tuning_frame(
        session,
        providers_path=Path("data/nissan-xterra-2008/tuning_providers.yaml"),
        calibration=load_afr_calibration(Path("data/nissan-xterra-2008/afr_calibration.yaml")),
    )

    assert frame.capture_profile == "tuning-did"
    assert frame.tune_revision == "test-revision"
    assert frame.capability["rpm"]["available"] is True
    assert frame.values["rpm"].iloc[0] == pytest.approx(2500.0)
    assert frame.values["measured_afr_b1_raw_voltage"].iloc[0] == pytest.approx(2.2)
    assert frame.values["measured_afr_b1"].iloc[0] == pytest.approx(14.83625)
    assert bool(frame.values["measured_afr_b1_censored"].iloc[0]) is False
    assert frame.capability["cam_command"]["available"] is True
    assert frame.capability["cam_measured"]["available"] is True
    assert frame.capability["cam_measured_b"]["available"] is True
    sections = section_capabilities(frame.capability)
    assert sections["measured_afr"]["available"] is True
    assert sections["cam_tracking"]["available"] is True
