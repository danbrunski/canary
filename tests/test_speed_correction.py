"""The trip path's speed correction and its loader (M01.2)."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from src.analysis.dyno_profile import DYNO_FILENAME, load_dyno_profile
from src.paths import DATA_DIR
from src.signals.speed_correction import TRIP_SPEED_CHANNEL, SpeedCorrection
from src.vehicle.speed_correction import DYNO_FILENAME as LOADER_DYNO_FILENAME
from src.vehicle.speed_correction import load_trip_speed_correction

DATA = DATA_DIR


def test_road_speed_divides_by_the_measured_ratio() -> None:
    correction = SpeedCorrection.measured(0.9203, "test")
    assert correction.road_speed(100.0) == pytest.approx(108.660219)
    series = correction.road_speed(pd.Series([0.0, 92.03]))
    assert series.tolist() == pytest.approx([0.0, 100.0])
    assert "/ 0.9203" in correction.basis


def test_an_unmeasured_vehicle_integrates_as_reported_and_says_so() -> None:
    correction = SpeedCorrection.unmeasured("no dyno.yaml")
    assert correction.road_speed(100.0) == 100.0
    assert correction.basis.startswith(f"{TRIP_SPEED_CHANNEL} as reported, uncorrected")
    assert "no dyno.yaml" in correction.basis


@pytest.mark.parametrize("ratio", [0.5, 1.5, 9.203, 0.0])
def test_a_ratio_outside_the_dyno_bounds_is_refused(ratio: float) -> None:
    with pytest.raises(ValueError, match="outside"):
        SpeedCorrection.measured(ratio, "test")


def test_a_correction_must_name_its_source() -> None:
    with pytest.raises(ValueError, match="where its ratio came from"):
        SpeedCorrection.unmeasured("  ")


def test_the_loader_reads_the_file_the_dyno_reads() -> None:
    assert LOADER_DYNO_FILENAME == DYNO_FILENAME


@pytest.mark.parametrize(
    "profile", sorted(p.parent.name for p in DATA.glob(f"*/{DYNO_FILENAME}")), ids=str
)
def test_every_tracked_profile_reads_the_ratio_its_dyno_schema_validates(profile: str) -> None:
    """This layer cannot import ``MotionSources``; this pins that the vehicle
    layer's reader and the dyno's schema give the same ratio for every
    tracked ``dyno.yaml``."""
    dyno = load_dyno_profile(DATA / profile / DYNO_FILENAME)
    assert dyno is not None
    sources = dyno.motion_sources.speed if dyno.motion_sources is not None else []
    expected = next((s.ratio_to_gps for s in sources if s.channel == TRIP_SPEED_CHANNEL), None)
    assert load_trip_speed_correction(DATA / profile).ratio_to_gps == expected


def test_the_xterra_trip_ratio_is_its_measured_obd_speed_ratio() -> None:
    correction = load_trip_speed_correction(DATA / "nissan-xterra-2008")
    assert correction.ratio_to_gps == 0.9203


def test_a_profile_with_no_dyno_file_is_unmeasured(tmp_path: Path) -> None:
    correction = load_trip_speed_correction(tmp_path)
    assert correction.ratio_to_gps is None
    assert f"no {DYNO_FILENAME}" in correction.basis


def test_a_dyno_file_without_the_trip_channel_says_which_kind_of_unmeasured(
    tmp_path: Path,
) -> None:
    (tmp_path / DYNO_FILENAME).write_text(
        "schema_version: 1\nmotion_sources:\n  speed:\n"
        "    - channel: decoded:VEHICLE_SPEED_2D1\n      ratio_to_gps: 0.932\n"
        "      evidence: test\n"
    )
    correction = load_trip_speed_correction(tmp_path)
    assert correction.ratio_to_gps is None
    assert f"records no ratio_to_gps for {TRIP_SPEED_CHANNEL}" in correction.basis


def test_a_malformed_dyno_file_raises_rather_than_reading_as_unmeasured(tmp_path: Path) -> None:
    (tmp_path / DYNO_FILENAME).write_text(
        "schema_version: 1\nmotion_sources:\n  speed:\n    - channel: obd:SPEED\n"
    )
    with pytest.raises(ValueError, match="numeric ratio_to_gps"):
        load_trip_speed_correction(tmp_path)


def test_an_edited_dyno_file_applies_without_a_restart(tmp_path: Path) -> None:
    path = tmp_path / DYNO_FILENAME
    body = (
        "schema_version: 1\nmotion_sources:\n  speed:\n    - channel: obd:SPEED\n"
        "      ratio_to_gps: {}\n      evidence: test\n"
    )
    path.write_text(body.format(0.92))
    assert load_trip_speed_correction(tmp_path).ratio_to_gps == 0.92
    path.write_text(body.format(0.95))
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert load_trip_speed_correction(tmp_path).ratio_to_gps == 0.95
