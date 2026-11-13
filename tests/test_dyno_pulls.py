"""H07.7 — finding the dyno's pulls in a stored session.

The session holds an rpm trace and a GPS course; the per-window motion read
(:func:`read_motion_inputs`, tested on its own) is stood in for, so these
tests cover what this module adds: where the pulls are, which gear, which
way, and that every window found comes back as a pull or a named refusal.
"""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.analysis import dyno_pulls
from src.analysis.dyno import Driveline, DynoRefusal, MotionSamples, MotionSources, PowerPull
from src.analysis.dyno_pulls import find_power_pulls, label_headings
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

T0 = 1_790_000_000.0
DRIVELINE = Driveline(
    tire_radius_m=2.609 / (2 * math.pi),
    final_drive=4.08,
    gearbox_ratios=(4.368, 2.518, 1.743, 1.283, 1.0, 0.769),
    wheel_side_kg_m2=5.0,
    engine_side_kg_m2=0.18,
    efficiency=0.85,
    inertia="estimated",
)
SOURCES = MotionSources(speed=[], rpm=["obd:RPM"])


def _pull_rpm(start: float, *, dip: bool = False) -> list[tuple[float, float]]:
    """Idle, then 2,000 -> 5,000 rpm over 6 s at 20 Hz, then steady."""
    rows = []
    for i in range(int(30 * 20)):
        t = i / 20.0
        if t < 10:
            rpm = 2000.0
        elif t < 16:
            rpm = 2000.0 + 500.0 * (t - 10)
            if dip and t >= 12.5:
                # A part lift: the rise slows to 150 rpm/s for a second (still
                # rising, under 40 % of the pull's own slope), then resumes.
                rpm = 3250.0 + 150.0 * min(t - 12.5, 1.0) + 500.0 * max(t - 13.5, 0.0)
        else:
            rpm = 5000.0
        rows.append((start + t, rpm))
    return rows


def _session(tmp_path: Path, pulls: list[tuple[float, float, bool]]) -> Path:
    """One session; each pull is (start offset s, GPS course deg, dip)."""
    records = []
    for offset, course, dip in pulls:
        records += [
            TelemetryRecord.obd(t, "RPM", rpm, "rpm") for t, rpm in _pull_rpm(T0 + offset, dip=dip)
        ]
        records += [
            TelemetryRecord.gps(T0 + offset + s, "heading", course, "deg") for s in range(30)
        ]
    session = tmp_path / "2026-10-03T120000"
    write_records_to_parquet(records, session, max_rows=100_000)
    return session


def _inputs_in_gear(gear: int, *, motion: Any = None) -> Any:
    """A stand-in for one window's DynoInputs: rpm and speed consistent with ``gear``."""
    rpm = np.linspace(2000.0, 5000.0, 120)
    speed = rpm * 2 * math.pi * DRIVELINE.tire_radius_m / (60 * DRIVELINE.overall_ratio(gear))
    samples = MotionSamples(
        t_s=np.arange(120) / 20.0,
        speed_mps=speed,
        accel_mps2=np.full(120, 2.0),
        sf_lon_mps2=np.full(120, 2.0),
        rpm=rpm,
    )
    return SimpleNamespace(
        rpm=rpm,
        speed_mps=speed,
        demand=np.full(120, 99.0),
        motion=lambda: samples if motion is None else motion,
        gps_check=SimpleNamespace(speed_scale_residual=0.004),
    )


@pytest.fixture
def inputs_by_window(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Queue of what ``read_motion_inputs`` returns, one per window found."""
    queue: list[Any] = []
    monkeypatch.setattr(dyno_pulls, "read_motion_inputs", lambda *a, **k: queue.pop(0))
    return queue


def _find(session: Path) -> list[dyno_pulls.FoundPull]:
    return find_power_pulls(
        session, sources=SOURCES, driveline=DRIVELINE, demand_channel="obd:THROTTLE_POS"
    )


def test_each_rpm_rise_becomes_a_pull_in_its_gear_with_its_course(
    tmp_path: Path, inputs_by_window: list[Any]
) -> None:
    session = _session(tmp_path, [(0.0, 10.0, False), (60.0, 190.0, False)])
    inputs_by_window += [_inputs_in_gear(3), _inputs_in_gear(3)]

    found = _find(session)

    assert [f.refusal for f in found] == [None, None]
    assert [f.pull.gear for f in found if f.pull is not None] == [3, 3]
    # The window's GPS speed check travels with the pull into the power result (M01.10).
    assert [f.pull.speed_scale_residual for f in found if f.pull is not None] == [0.004, 0.004]
    assert [round(f.course_deg or 0) for f in found] == [10, 190]
    for item, offset in zip(found, (0.0, 60.0), strict=True):
        # The window brackets the rise (10-16 s into each pull's trace).
        assert abs(item.start.timestamp() - (T0 + offset + 10)) < 1.0
        assert abs(item.end.timestamp() - (T0 + offset + 16)) < 1.0
        assert item.rpm_end - item.rpm_start > 2500

    labelled = label_headings(found)
    assert [p.heading for p in labelled] == ["course-010", "course-190"]


def test_a_lift_mid_pull_is_refused_by_name_and_never_read(
    tmp_path: Path, inputs_by_window: list[Any]
) -> None:
    session = _session(tmp_path, [(0.0, 10.0, True)])
    found = _find(session)
    assert len(found) >= 1
    assert all(f.pull is None for f in found)
    assert {f.refusal.kind for f in found if f.refusal is not None} <= {"lift_or_shift"}
    assert inputs_by_window == []  # nothing was queued, and nothing was asked for


def test_a_window_between_gears_is_refused_not_rounded(
    tmp_path: Path, inputs_by_window: list[Any]
) -> None:
    session = _session(tmp_path, [(0.0, 10.0, False)])
    slipping = _inputs_in_gear(3)
    slipping.rpm = slipping.rpm * 1.12  # clutch slip: rpm 12 % above 3rd's line
    inputs_by_window.append(slipping)

    (found,) = _find(session)

    assert found.pull is None
    assert found.refusal is not None and found.refusal.kind == "gear_unresolved"
    assert "more than 4%" in found.refusal.reason


def test_a_motion_refusal_is_handed_on_unchanged(
    tmp_path: Path, inputs_by_window: list[Any]
) -> None:
    session = _session(tmp_path, [(0.0, 10.0, False)])
    no_imu = DynoRefusal("motion_inputs", "imu_unavailable", "no imu_frame.json")
    inputs_by_window.append(_inputs_in_gear(3, motion=no_imu))

    (found,) = _find(session)

    assert (found.pull, found.refusal) == (None, no_imu)


def test_headings_group_by_course_and_a_missing_course_is_its_own_direction() -> None:
    samples = MotionSamples(
        t_s=np.arange(3.0),
        speed_mps=np.ones(3),
        accel_mps2=np.zeros(3),
        sf_lon_mps2=np.zeros(3),
    )

    def item(course: float | None) -> dyno_pulls.FoundPull:
        pull = PowerPull(heading="unlabelled", gear=3, samples=samples, demand=None)
        return dyno_pulls.FoundPull(
            start=pd.Timestamp(0, tz="UTC"),
            end=pd.Timestamp(1, tz="UTC"),
            rpm_start=2000.0,
            rpm_end=5000.0,
            course_deg=course,
            pull=pull,
            refusal=None,
        )

    labels = [
        p.heading
        for p in label_headings([item(358.0), item(5.0), item(181.0), item(None), item(170.0)])
    ]
    # 358 and 5 are 7 degrees apart across north: one direction.
    assert labels == ["course-000", "course-000", "course-180", "course-unknown", "course-180"]


def test_a_session_with_no_rpm_has_no_pulls_and_a_vehicle_without_rpm_is_refused(
    tmp_path: Path,
) -> None:
    session = tmp_path / "2026-10-03T120000"
    write_records_to_parquet(
        [TelemetryRecord.gps(T0 + s, "heading", 10.0, "deg") for s in range(10)],
        session,
        max_rows=1_000,
    )
    assert _find(session) == []
    with pytest.raises(ValueError, match="no rpm channel"):
        find_power_pulls(
            session,
            sources=MotionSources(speed=[], rpm=[]),
            driveline=DRIVELINE,
            demand_channel=None,
        )


def test_a_part_throttle_rise_is_refused_before_its_motion_is_read(
    tmp_path: Path, inputs_by_window: list[Any]
) -> None:
    """H07.9: each motion read opens every chunk of the session, and most rpm
    rises in a drive are part throttle. With a WOT threshold, a window whose
    demand never reaches it is refused ``not_wot`` unread; one without any
    demand reading is refused ``inputs_missing``; a WOT window is read."""
    records = []
    for offset, course in ((0.0, 10.0), (60.0, 190.0), (120.0, 10.0)):
        records += [TelemetryRecord.obd(t, "RPM", rpm, "rpm") for t, rpm in _pull_rpm(T0 + offset)]
        records += [
            TelemetryRecord.gps(T0 + offset + s, "heading", course, "deg") for s in range(30)
        ]
    for offset, level in ((0.0, 3.5), (60.0, 4.1)):  # the third pull has no demand rows
        records += [
            TelemetryRecord.obd(T0 + offset + i / 10.0, "THROTTLE_POS", level, "V")
            for i in range(300)
        ]
    session = tmp_path / "2026-10-03T120000"
    write_records_to_parquet(records, session, max_rows=100_000)
    inputs_by_window.append(_inputs_in_gear(3))  # only the WOT window asks

    found = find_power_pulls(
        session,
        sources=SOURCES,
        driveline=DRIVELINE,
        demand_channel="obd:THROTTLE_POS",
        wot_threshold=4.0,
    )

    assert [None if f.refusal is None else f.refusal.kind for f in found] == [
        "not_wot",
        None,
        "inputs_missing",
    ]
    assert "peaks at 3.5" in found[0].refusal.reason  # type: ignore[union-attr]
    assert inputs_by_window == []


def test_a_caller_that_read_the_session_passes_it_and_nothing_reads_it_again(
    tmp_path: Path, inputs_by_window: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """H07.9: the workbench reads each session once; ``channels`` carries that
    read, and the detector does not read the whole session itself."""
    from src.analysis.channels import read_channels

    session = _session(tmp_path, [(0.0, 10.0, False)])
    channels = read_channels(session, dyno_pulls.session_reads(SOURCES, None), acquired_time=True)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("read the session again")

    monkeypatch.setattr(dyno_pulls, "read_channels", forbidden)
    inputs_by_window.append(_inputs_in_gear(3))
    (found,) = find_power_pulls(
        session,
        sources=SOURCES,
        driveline=DRIVELINE,
        demand_channel=None,
        channels=channels,
    )
    assert found.refusal is None and round(found.course_deg or 0) == 10


def test_a_session_without_gps_or_demand_reads_as_missing_not_a_crash(
    tmp_path: Path, inputs_by_window: list[Any]
) -> None:
    """The dyno page returned 500 on 2026-10-10: a session that never recorded
    a GPS course (or the demand channel) sliced an empty, RangeIndex series by
    timestamps. No course is ``course-unknown``; no demand is ``inputs_missing``."""
    records = [TelemetryRecord.obd(t, "RPM", rpm, "rpm") for t, rpm in _pull_rpm(T0)]
    session = tmp_path / "2026-10-10T120000"
    write_records_to_parquet(records, session, max_rows=100_000)
    inputs_by_window.append(_inputs_in_gear(3))

    (found,) = _find(session)
    assert found.refusal is None and found.course_deg is None
    assert [p.heading for p in label_headings([found])] == ["course-unknown"]

    (gated,) = find_power_pulls(
        session,
        sources=SOURCES,
        driveline=DRIVELINE,
        demand_channel="obd:THROTTLE_POS",
        wot_threshold=4.0,
    )
    assert gated.refusal is not None and gated.refusal.kind == "inputs_missing"
