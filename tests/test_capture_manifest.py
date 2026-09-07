"""Sidecar readers keep a historic verdict's scope explicit (roadmap C03.9)."""

from __future__ import annotations

import json
from pathlib import Path

from src.control.capture_manifest import (
    CAPTURE_EPOCH_FILENAME,
    read_capture_epoch,
    read_capture_quality,
    write_capture_epoch,
    write_capture_quality,
)


def test_a_v1_sidecar_is_read_as_a_trailing_snapshot_with_unknown_losses(tmp_path: Path) -> None:
    (tmp_path / "capture_quality.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "verdict": "pass",
                "profile": "fuel-map",
                "observed_seconds": 60.0,
                "storage": {"dropped_records": 2491},
                "failures": [],
                "notes": [],
            }
        )
    )
    payload = read_capture_quality(tmp_path)
    assert payload is not None
    assert payload["scope"] == "trailing_snapshot"
    assert payload["verdict"] == "pass"
    assert payload["schema_version"] == 1
    assert payload["losses"]["recording"]["status"] == "unknown"
    assert payload["losses"]["recording"]["lost"] is None
    assert any("cumulative" in note for note in payload["notes"])


def test_a_v2_sidecar_round_trips_and_an_absent_one_is_none(tmp_path: Path) -> None:
    assert read_capture_quality(tmp_path) is None
    write_capture_quality(tmp_path, {"schema_version": 2, "scope": "stop", "verdict": "fail"})
    assert read_capture_quality(tmp_path) == {
        "schema_version": 2,
        "scope": "stop",
        "verdict": "fail",
    }
    (tmp_path / "capture_quality.json").write_text("not json")
    assert read_capture_quality(tmp_path) is None


def test_the_capture_epoch_sidecar_round_trips(tmp_path: Path) -> None:
    assert read_capture_epoch(tmp_path) is None
    payload = {
        "schema_version": 1,
        "generation": 4,
        "started_wall": 1.0,
        "loss_epoch": {
            "taken_at_monotonic": 10.0,
            "taken_at_wall": 1.0,
            "counters": {"storage.dropped_records": 2491, "can0.rx_dropped": None},
        },
    }
    path = write_capture_epoch(tmp_path, payload)
    assert path.name == CAPTURE_EPOCH_FILENAME
    assert read_capture_epoch(tmp_path) == payload
