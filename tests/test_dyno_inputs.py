"""Speed, acceleration and specific force on one time base (H07.3).

Synthetic channels are generated from a known motion (speed, acceleration,
grade) at each instrument's own rate, resolution and lag; the producer must
recover the motion, keep ``f − a`` equal to the grade term however fast the
acceleration changes, apply the profile's speed ratio, and refuse each broken
input with its named cause.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from src.analysis.dyno import (
    DYNO_FILENAME,
    DynoRefusal,
    Leg,
    MotionSamples,
    MotionSources,
    PitchOffset,
    SpeedSource,
    estimate_pitch_offset,
    load_dyno_profile,
)
from src.analysis.dyno_inputs import (
    DynoInputs,
    ImuSpecificForce,
    ImuUnavailable,
    build_motion_inputs,
    matched_kernels,
    read_motion_inputs,
)
from src.preprocessing.imu_session import (
    ImuFrameInputs,
    ImuFrameSidecar,
    LinearAccelFrame,
    PitchReference,
    SpecificForceFrame,
    reference_id,
    write_imu_frame_sidecar,
)
from src.signals.committees import STANDARD_GRAVITY as G
from src.signals.committees import load_committee_profile
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

REPO = Path(__file__).resolve().parents[1]

T0 = pd.Timestamp("2026-09-26T12:00:00Z")
START = T0 + pd.Timedelta(seconds=2)
END = T0 + pd.Timedelta(seconds=22)
#: The synthetic speed channel reads this fraction of true road speed.
RATIO = 0.932
#: GPS speed arrives this late (acquisition clock), as the committee saw.
GPS_LAG_S = 1.5
SPEED = "decoded:TEST_SPEED"
RPM = "decoded:TEST_RPM"
OVERALL_RATIO = 1.0 * 4.08  # 5th: keeps 0x1F9 counts in 16 bits
TIRE_RADIUS = 2.609 / (2 * math.pi)

SOURCES = MotionSources(
    speed=[SpeedSource(channel=SPEED, ratio_to_gps=RATIO, evidence="synthetic")],
    rpm=[RPM],
)

Profile = Callable[[np.ndarray], np.ndarray]


def _v(t: np.ndarray) -> np.ndarray:
    return 20.0 + 1.2 * t + 0.8 * np.sin(2 * math.pi * t / 8.0)


def _a(t: np.ndarray) -> np.ndarray:
    return 1.2 + 0.8 * (2 * math.pi / 8.0) * np.cos(2 * math.pi * t / 8.0)


def _grade(t: np.ndarray) -> np.ndarray:
    return 0.01 * np.sin(2 * math.pi * t / 20.0)


def _series(t: np.ndarray, values: np.ndarray) -> pd.Series:
    return pd.Series(values, index=T0 + pd.to_timedelta(t, unit="s"))


def _times(rng: np.random.Generator, hz: float, jitter_s: float = 0.0) -> np.ndarray:
    t = np.arange(-15.0, 35.0, 1.0 / hz)
    return np.sort(t + (rng.uniform(-jitter_s, jitter_s, len(t)) if jitter_s else 0.0))


def _channels(
    seed: int = 0,
    *,
    v: Profile = _v,
    a: Profile = _a,
    gps_hz: float = 1.0,
    satellites: float = 10.0,
    hdop: float = 0.8,
) -> tuple[dict[str, pd.Series], ImuSpecificForce]:
    """Each instrument at its own rate: speed at 100 Hz in 0.1 km/h steps
    reading RATIO of true, GPS late by GPS_LAG_S, the IMU at 208 Hz."""
    rng = np.random.default_rng(seed)
    t_can = _times(rng, 100.0, jitter_s=0.0005)
    t_gps = _times(rng, gps_hz)
    t_imu = _times(rng, 208.0, jitter_s=0.0002)
    kmh = np.round(v(t_can) * RATIO * 3.6, 1)
    gps = v(t_gps - GPS_LAG_S) * 3.6 + rng.normal(0.0, 0.1, len(t_gps))
    sf = a(t_imu) + G * np.sin(np.arctan(_grade(t_imu))) + rng.normal(0.0, 0.05, len(t_imu))
    rpm = v(t_can) * OVERALL_RATIO / TIRE_RADIUS * 60 / (2 * math.pi)
    channels = {
        SPEED: _series(t_can, kmh),
        RPM: _series(t_can, rpm),
        "gps:speed": _series(t_gps, gps),
        "gps:num_satellites": _series(t_gps, np.full(len(t_gps), satellites)),
        "gps:hdop": _series(t_gps, np.full(len(t_gps), hdop)),
        "gps:altitude": _series(t_gps, 300.0 + 0.5 * t_gps),
    }
    return channels, ImuSpecificForce(series=_series(t_imu, sf), reference_id="ref-1")


def _built(result: DynoInputs | DynoRefusal) -> DynoInputs:
    assert isinstance(result, DynoInputs), result
    return result


def _refused(result: object, kind: str) -> DynoRefusal:
    assert isinstance(result, DynoRefusal), result
    assert result.kind == kind, result
    assert result.reason
    return result


def _build(
    channels: dict[str, pd.Series], imu: object, **kwargs: object
) -> DynoInputs | DynoRefusal:
    options: dict[str, Any] = {"start": START, "end": END, "sources": SOURCES, "imu": imu}
    options.update(kwargs)
    return build_motion_inputs(channels, **options)


# ---------------------------------------------------------------------------
# Known answers
# ---------------------------------------------------------------------------


def test_speed_and_acceleration_recover_a_known_profile() -> None:
    channels, imu = _channels()
    out = _built(_build(channels, imu))
    t = out.t_s
    assert t[0] == 0.0 and t[-1] == pytest.approx(20.0)
    np.testing.assert_allclose(np.diff(t), 0.05)
    true_t = t + 2.0
    # Absolute speed: the 0.932 under-read removed, 0.1 km/h steps averaged.
    assert np.max(np.abs(out.speed_mps - _v(true_t))) < 0.02
    assert np.max(np.abs(out.accel_mps2 - _a(true_t))) < 0.03
    assert out.speed_source == SPEED and out.speed_ratio_to_gps == RATIO
    assert out.rpm is not None and out.rpm_source == RPM
    np.testing.assert_allclose(
        out.rpm, _v(true_t) * OVERALL_RATIO / TIRE_RADIUS * 60 / (2 * math.pi), rtol=2e-3
    )
    assert out.altitude_m is not None
    np.testing.assert_allclose(out.altitude_m, 300.0 + 0.5 * true_t, atol=1e-6)
    # GPS is received 1.5 s late; the check finds the lag, and at it the
    # scaled speed agrees with GPS.
    assert out.gps_check.status == "checked"
    assert out.gps_check.lag_s == pytest.approx(GPS_LAG_S, abs=0.1)
    assert out.gps_check.ratio == pytest.approx(1.0, abs=0.005)
    assert out.gps_quality.epochs == 21
    assert out.gps_quality.worst == {"gps:num_satellites": 10.0, "gps:hdop": 0.8}
    motion = out.motion()
    assert isinstance(motion, MotionSamples)
    assert motion.imu_reference_id == "ref-1"


def _launch_v(t: np.ndarray) -> np.ndarray:
    # Cruise, then 2.5 m/s^2 switched on over ~0.1 s at t = 10 s.
    return 20.0 + 2.5 * 0.05 * np.logaddexp(0.0, (t - 10.0) / 0.05)


def _launch_a(t: np.ndarray) -> np.ndarray:
    return 2.5 / (1.0 + np.exp(-(t - 10.0) / 0.05))


def test_specific_force_and_acceleration_share_one_filter() -> None:
    # A launch far faster than the 0.5 s smoothing: the smoothed acceleration
    # misses the edge by ~0.2 m/s^2, but f is smoothed by the same kernel, so
    # f - a is still the grade term alone. A different filter on either (or
    # none on f) puts the edge into f - a, where every dyno fit reads it as
    # pitch or force.
    channels, imu = _channels(v=_launch_v, a=_launch_a)
    out = _built(_build(channels, imu))
    assert out.sf_lon_mps2 is not None
    true_t = out.t_s + 2.0
    assert np.max(np.abs(out.accel_mps2 - _launch_a(true_t))) > 0.15  # the edge is smoothed
    static = out.sf_lon_mps2 - out.accel_mps2
    grade_term = G * np.sin(np.arctan(_grade(true_t)))
    assert np.max(np.abs(static - grade_term)) < 0.05


def _from_rest_v(t: np.ndarray) -> np.ndarray:
    # Stopped until t = 8 s, then 2.5 m/s^2: a first-gear launch from rest.
    return np.where(t < 8.0, 0.0, 2.5 * (t - 8.0))


def _from_rest_a(t: np.ndarray) -> np.ndarray:
    return np.where(t < 8.0, 0.0, 2.5)


def test_a_launch_from_rest_reads_no_negative_speed_and_reaches_the_pitch_offset() -> None:
    # M01.7: the quadratic smoother undershot to -0.022 m/s at the standstill
    # corner, and estimate_pitch_offset refuses a negative speed, so one
    # launch from rest among a gear's pulls refused that gear's offset. The
    # reader clamps speed at zero; acceleration comes from the raw speed.
    legs = []
    for seed, heading in ((1, "northbound"), (2, "southbound")):
        channels, imu = _channels(seed, v=_from_rest_v, a=_from_rest_a)
        out = _built(_build(channels, imu))
        assert out.speed_mps.min() == 0.0
        true_t = out.t_s + 2.0
        stopped = true_t < 7.5
        assert np.all(out.speed_mps[stopped] == 0.0)
        assert np.max(np.abs(out.accel_mps2[stopped])) < 0.05
        assert np.median(out.accel_mps2[true_t > 9.0]) == pytest.approx(2.5, abs=0.05)
        motion = out.motion()
        assert isinstance(motion, MotionSamples), motion
        legs.append(Leg(heading, motion))
    offset = estimate_pitch_offset(legs, squat=None)
    assert isinstance(offset, PitchOffset), offset
    # 1/2 · 2.5 · 14^2 = 245 m from the launch to the window's end.
    for heading in ("northbound", "southbound"):
        assert offset.heading_distance_m[heading] == pytest.approx(245.0, rel=0.01)


def test_rpm_is_smoothed_by_the_same_kernel_as_speed() -> None:
    # rpm made exactly proportional to the recorded speed samples (same
    # instants, same 0.1 km/h steps) across a launch: after H07.3 the two must
    # still be proportional to rounding, which holds only if the same kernel
    # smooths both (rpm is the axis every power point is binned on, and speed
    # sets the road load and P = F·v at that point).
    channels, imu = _channels(v=_launch_v, a=_launch_a)
    per_kmh = 97.0
    channels[RPM] = channels[SPEED] * per_kmh
    out = _built(_build(channels, imu))
    assert out.rpm is not None
    np.testing.assert_allclose(out.rpm, per_kmh * out.speed_mps * 3.6 * RATIO, rtol=1e-9)


def test_matched_kernels_are_exact_on_a_quadratic_and_share_one_smoothing() -> None:
    smooth, derivative = matched_kernels(5, 0.05)
    assert len(smooth) == len(derivative) == 13
    x = np.arange(13) * 0.05
    y = 3.0 + 2.0 * x - 4.0 * x**2
    assert float(smooth @ y) == pytest.approx(y[6])
    assert float(derivative @ y) == pytest.approx(2.0 - 8.0 * x[6])
    # The derivative kernel is the smoothing applied to a central difference:
    # differentiating then smoothing equals the derivative kernel exactly.
    rng = np.random.default_rng(0)
    v = rng.normal(size=40)
    central = (v[2:] - v[:-2]) / (2 * 0.05)
    np.testing.assert_allclose(
        np.correlate(v, derivative, "valid"), np.correlate(central, smooth[1:-1], "valid")
    )


# ---------------------------------------------------------------------------
# Profile ratio
# ---------------------------------------------------------------------------


def test_a_missing_speed_correction_is_refused_and_names_the_ratio_the_data_implies() -> None:
    channels, imu = _channels()
    uncorrected = MotionSources(
        speed=[SpeedSource(channel=SPEED, ratio_to_gps=1.0, evidence="forgot")], rpm=[RPM]
    )
    refusal = _refused(_build(channels, imu, sources=uncorrected), "speed_scale_mismatch")
    assert "0.93" in refusal.reason and SPEED in refusal.reason


def test_the_scale_check_is_a_gross_error_check_not_a_calibration() -> None:
    # 0x6F7's ratio (0.9203) used for a 0.932 channel is a 1.3 % error: under
    # the 3 % tolerance, so it passes and reads 1.3 % fast. The ratio has to be
    # the channel's own, measured (dyno.yaml motion_sources).
    channels, imu = _channels()
    borrowed = MotionSources(
        speed=[SpeedSource(channel=SPEED, ratio_to_gps=0.9203, evidence="0x6F7's")], rpm=[RPM]
    )
    out = _built(_build(channels, imu, sources=borrowed))
    assert out.gps_check.ratio == pytest.approx(0.9203 / RATIO, abs=0.005)
    assert np.median(out.speed_mps / _v(out.t_s + 2.0)) == pytest.approx(RATIO / 0.9203, abs=1e-3)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_gps_below_the_h04_quality_gates_is_refused_by_name() -> None:
    channels, imu = _channels()
    few = channels["gps:num_satellites"].copy()
    few.iloc[20] = 6.0  # t = 5 s, inside the window
    refusal = _refused(_build({**channels, "gps:num_satellites": few}, imu), "gps_quality")
    assert "gps:num_satellites 6 < 7" in refusal.reason
    wide = channels["gps:hdop"].copy()
    wide.iloc[25] = 2.5
    refusal = _refused(_build({**channels, "gps:hdop": wide}, imu), "gps_quality")
    assert "gps:hdop 2.5 > 2" in refusal.reason
    # A gate channel that was never recorded fails closed.
    without = {k: s for k, s in channels.items() if k != "gps:hdop"}
    refusal = _refused(_build(without, imu), "gps_quality")
    assert "gps:hdop was not recorded" in refusal.reason
    # An epoch outside the window does not count against it.
    early = channels["gps:num_satellites"].copy()
    early.iloc[5] = 3.0  # t = -10 s
    _built(_build({**channels, "gps:num_satellites": early}, imu))
    no_gps = {k: s for k, s in channels.items() if k != "gps:speed"}
    _refused(_build(no_gps, imu), "inputs_missing")


def test_a_missing_imu_frame_degrades_by_name() -> None:
    channels, _ = _channels()
    cause = ImuUnavailable("imu_frame_missing", "no imu_frame.json: run resolve_imu_frame.py")
    out = _built(_build(channels, cause))
    assert out.sf_lon_mps2 is None and out.imu_reference_id is None
    assert out.imu_unavailable == cause
    # Speed and acceleration are still delivered...
    assert np.max(np.abs(out.speed_mps - _v(out.t_s + 2.0))) < 0.02
    # ...but the samples every dyno fit needs are refused, naming the cause.
    refusal = _refused(out.motion(), "imu_unavailable")
    assert "imu_frame_missing" in refusal.reason and "resolve_imu_frame" in refusal.reason


def test_speed_sources_are_taken_in_profile_order_and_coarse_ones_are_named() -> None:
    channels, imu = _channels()
    listed = MotionSources(
        speed=[
            SpeedSource(channel="decoded:ABSENT", ratio_to_gps=0.9, evidence="x"),
            SpeedSource(channel=SPEED, ratio_to_gps=RATIO, evidence="y"),
        ],
        rpm=[RPM],
    )
    assert _built(_build(channels, imu, sources=listed)).speed_source == SPEED
    # A ~0.7 Hz Mode 01 speed and 1 Hz GPS cannot be differentiated over 0.5 s.
    rng = np.random.default_rng(1)
    t_obd = _times(rng, 0.7)
    slow = {k: s for k, s in channels.items() if k != SPEED}
    slow["obd:SPEED"] = _series(t_obd, np.round(_v(t_obd) * RATIO * 3.6))
    obd_only = MotionSources(
        speed=[SpeedSource(channel="obd:SPEED", ratio_to_gps=RATIO, evidence="z")], rpm=[RPM]
    )
    refusal = _refused(_build(slow, imu, sources=obd_only), "speed_too_coarse")
    assert "obd:SPEED (0.70 Hz)" in refusal.reason and "gps:speed (1.00 Hz)" in refusal.reason
    # A 10 Hz GPS is fine enough, and is its own reference.
    fast_gps, imu10 = _channels(gps_hz=10.0)
    del fast_gps[SPEED]
    out = _built(_build(fast_gps, imu10, sources=MotionSources(rpm=[RPM])))
    assert out.speed_source == "gps:speed" and out.gps_check.status == "reference"


def test_a_hole_in_a_selected_channel_is_refused_not_bridged() -> None:
    channels, imu = _channels()

    def holed(series: pd.Series, lo: float, hi: float) -> pd.Series:
        seconds = (series.index - T0) / pd.Timedelta(seconds=1)  # type: ignore[operator]
        holed_series: pd.Series = series[(seconds < lo) | (seconds > hi)]
        return holed_series

    refusal = _refused(
        _build({**channels, SPEED: holed(channels[SPEED], 9.0, 9.4)}, imu), "inputs_gap"
    )
    assert SPEED in refusal.reason and "hole" in refusal.reason
    broken = ImuSpecificForce(holed(imu.series, 12.0, 12.3), imu.reference_id)
    refusal = _refused(_build(channels, broken), "inputs_gap")
    assert "imu:SF_LON" in refusal.reason
    refusal = _refused(
        _build({**channels, RPM: holed(channels[RPM], 15.0, 15.5)}, imu), "inputs_gap"
    )
    assert RPM in refusal.reason
    # Speed that stops before the window (plus its smoothing margin) ends.
    refusal = _refused(
        _build({**channels, SPEED: holed(channels[SPEED], 21.0, 99.0)}, imu), "inputs_gap"
    )
    assert "ends" in refusal.reason
    _refused(_build(channels, imu, start=END, end=START), "invalid_input")


# ---------------------------------------------------------------------------
# Profile data
# ---------------------------------------------------------------------------


def test_motion_sources_schema_refuses_gps_and_duplicates() -> None:
    with pytest.raises(ValidationError, match="reference"):
        MotionSources(speed=[SpeedSource(channel="gps:speed", ratio_to_gps=1.0, evidence="x")])
    with pytest.raises(ValidationError, match="twice"):
        MotionSources(rpm=["obd:RPM", "obd:RPM"])
    with pytest.raises(ValidationError):
        SpeedSource(channel="obd:SPEED", ratio_to_gps=0.92, evidence="")


def test_committed_xterra_speed_ratios_are_measured_and_agree_with_committees() -> None:
    profile = load_dyno_profile(REPO / "data" / "nissan-xterra-2008" / DYNO_FILENAME)
    assert profile is not None and profile.motion_sources is not None
    speed = {s.channel: s for s in profile.motion_sources.speed}
    # 0x2D1 is its own measurement, not the 0x6F7/PID figure it was once
    # assumed to share (0.932 against 0.9203).
    assert list(speed)[0] == "decoded:VEHICLE_SPEED_2D1"
    assert speed["decoded:VEHICLE_SPEED_2D1"].ratio_to_gps == pytest.approx(0.932, abs=0.002)
    # A channel both files state has one ratio: the committee's and the
    # dyno's cannot drift apart.
    committees = load_committee_profile(REPO / "data" / "nissan-xterra-2008" / "committees.yaml")
    relations = committees.committees["SPEED_SOURCE_DIVERGENCE"]
    assert relations.reference == "gps:speed"
    shared = [r for r in relations.relations if r.member in speed]
    assert shared
    for relation in shared:
        assert speed[relation.member].ratio_to_gps == relation.ratio, relation.member
    assert profile.motion_sources.rpm[0] == "decoded:ECM_RPM"


# ---------------------------------------------------------------------------
# Session I/O
# ---------------------------------------------------------------------------


def _identity_sidecar() -> ImuFrameSidecar:
    rotation = np.eye(3)
    return ImuFrameSidecar(
        status="resolved",
        resolved_at="2026-09-26T12:00:00+00:00",
        inputs=ImuFrameInputs(
            imu_samples=1,
            imu_rate_hz=208.0,
            first_sample="2026-09-26T12:00:00+00:00",
            last_sample="2026-09-26T12:00:40+00:00",
            speed_source="decoded:VEHICLE_SPEED_2D1",
            heading_source="gps:heading",
            quiescent_seconds=2.0,
            quiescent_window_samples=416,
            quiescent_lookahead_seconds=1.5,
            quiescent_lookahead_samples=312,
        ),
        specific_force=SpecificForceFrame(
            reference_id=reference_id(rotation),
            rotation_body_to_vehicle=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            yaw_offset_rad=0.0,
            sign_flipped=False,
            sign_votes=(10, 0),
            pitch_reference=PitchReference(
                gravity_body_g=(0.0, 0.0, 1.0),
                gravity_magnitude_g=1.0,
                tilt_from_body_z_deg=0.0,
                quiescent_samples=100,
                quiescent_seconds=2.0,
                stationary_fraction=0.0,
                windows=[],
                window_angle_spread_deg=0.0,
            ),
        ),
        linear_accel=LinearAccelFrame(yaw_offset_rad=0.0, sign_flipped=False, sign_votes=(10, 0)),
    )


def _session(tmp_path: Path) -> Path:
    """A stored session: raw 0x2D1 and 0x1F9 frames, GPS epochs, raw IMU in g."""
    rng = np.random.default_rng(7)
    epoch = T0.timestamp()
    records: list[TelemetryRecord] = []
    for t in _times(rng, 100.0, jitter_s=0.0005):
        raw = int(round(_v(np.array([t]))[0] * RATIO * 3.6 / 0.1))
        records.append(
            TelemetryRecord.can(epoch + t, 0x2D1, bytes([0, raw & 0xFF, raw >> 8, 0, 0, 0, 0, 0]))
        )
        rpm = _v(np.array([t]))[0] * OVERALL_RATIO / TIRE_RADIUS * 60 / (2 * math.pi)
        counts = int(round(rpm * 8))
        records.append(
            TelemetryRecord.can(
                epoch + t, 0x1F9, bytes([0, 0, counts >> 8, counts & 0xFF, 0, 0, 0, 0])
            )
        )
    for t in _times(rng, 1.0):
        stamp = epoch + t
        records.append(
            TelemetryRecord.gps(
                stamp, "speed", float(_v(np.array([t - GPS_LAG_S]))[0] * 3.6), "km/h"
            )
        )
        records.append(TelemetryRecord.gps(stamp, "num_satellites", 10, ""))
        records.append(TelemetryRecord.gps(stamp, "hdop", 0.8, ""))
        records.append(TelemetryRecord.gps(stamp, "altitude", 300.0, "m"))
    for t in _times(rng, 208.0):
        sf_g = float(_a(np.array([t]))[0] + G * math.sin(math.atan(_grade(np.array([t]))[0]))) / G
        records.append(TelemetryRecord.imu(epoch + t, "ACCEL_X", sf_g, "g"))
        records.append(TelemetryRecord.imu(epoch + t, "ACCEL_Y", 0.0, "g"))
        records.append(TelemetryRecord.imu(epoch + t, "ACCEL_Z", 1.0, "g"))
    session = tmp_path / "2026-09-26T120000"
    write_records_to_parquet(records, session, max_rows=20_000)
    return session


def test_read_motion_inputs_decodes_0x2d1_on_read_and_uses_the_frame_of_record(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    sources = MotionSources(
        speed=[SpeedSource(channel="decoded:VEHICLE_SPEED_2D1", ratio_to_gps=RATIO, evidence="t")],
        rpm=["decoded:ECM_RPM"],
    )
    # No imu_frame.json yet: delivered without specific force, and says why.
    bare = _built(read_motion_inputs(session, start=START, end=END, sources=sources))
    assert bare.imu_unavailable is not None and bare.imu_unavailable.kind == "imu_frame_missing"
    assert bare.speed_source == "decoded:VEHICLE_SPEED_2D1"
    assert np.max(np.abs(bare.speed_mps - _v(bare.t_s + 2.0))) < 0.02
    assert bare.rpm is not None and bare.rpm_source == "decoded:ECM_RPM"

    sidecar = _identity_sidecar()
    write_imu_frame_sidecar(session, sidecar)
    out = _built(read_motion_inputs(session, start=START, end=END, sources=sources))
    assert out.sf_lon_mps2 is not None
    assert sidecar.specific_force is not None
    assert out.imu_reference_id == sidecar.specific_force.reference_id
    static = out.sf_lon_mps2 - out.accel_mps2
    np.testing.assert_allclose(static, G * np.sin(np.arctan(_grade(out.t_s + 2.0))), atol=0.03)
    motion = out.motion()
    assert isinstance(motion, MotionSamples)
    assert motion.imu_reference_id == sidecar.specific_force.reference_id
