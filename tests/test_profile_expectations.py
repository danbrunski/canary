"""R23.11: one parse of a profile snapshot's promises, shared by three readers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.analysis.capture_coverage import member_channels
from src.storage.capture_manifest import CaptureManifest
from src.storage.profile_expectations import ProfileExpectations

SNAPSHOT: dict[str, Any] = {
    "name": "t",
    "mode01": {"rpm": 1.0, "ENGINE_LOAD": 0.5},
    "mode22_rate_weights": {"ecm_maf_gps": 2},
    "required_tuple": ["rpm", "ECM_MAF_GPS"],
    "target_rate_hz": 4,
    "min_achieved_rate_hz": 1.5,
}


def test_from_snapshot_normalises_every_field() -> None:
    expect = ProfileExpectations.from_snapshot(SNAPSHOT)
    assert dict(expect.weights) == {"RPM": 1.0, "ENGINE_LOAD": 0.5, "ECM_MAF_GPS": 2.0}
    assert expect.target_rate_hz == 4.0
    assert expect.required_tuple == ("RPM", "ECM_MAF_GPS")
    assert expect.min_achieved_rate_hz == 1.5
    assert expect.mode01_names == {"RPM", "ENGINE_LOAD"}


def test_a_requested_rate_overrides_the_snapshot_target() -> None:
    assert ProfileExpectations.from_snapshot(SNAPSHOT, 10).target_rate_hz == 10.0
    assert ProfileExpectations.from_snapshot({}).target_rate_hz is None
    assert ProfileExpectations.from_snapshot({}).min_achieved_rate_hz is None


def test_a_malformed_tuple_reads_as_no_tuple() -> None:
    assert ProfileExpectations.from_snapshot({"required_tuple": "RPM"}).required_tuple == ()
    assert ProfileExpectations.from_snapshot({"required_tuple": ["rpm", " "]}).required_tuple == (
        "RPM",
    )


def test_the_manifest_and_coverage_read_the_same_tuple() -> None:
    expect = ProfileExpectations.from_snapshot(SNAPSHOT)
    manifest = CaptureManifest(
        profile="t",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        profile_snapshot=SNAPSHOT,
    )
    assert manifest.required_tuple == expect.required_tuple
    assert tuple(member_channels(SNAPSHOT)) == expect.required_tuple
