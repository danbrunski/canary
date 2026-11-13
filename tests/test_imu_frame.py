"""Tests for gravity/heading/sign frame resolution (Phase 2a.4)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.decode.bindings import load_bindings
from src.preprocessing.imu_frame import (
    GRAVITY_MAX_SAMPLE_SECONDS,
    GRAVITY_START,
    GRAVITY_TIME_CONSTANT_SECONDS,
    QUIESCENT_LOOKAHEAD_SECONDS,
    SPEED_SOURCES,
    estimate_gravity,
    gravity_low_pass_alpha,
    gravity_step,
    low_pass_gravity,
    quiescent_mask,
    read_yaw_offset,
    resolve_frame,
    resolve_longitudinal_sign,
    resolve_yaw_offset,
    sample_intervals_seconds,
    select_speed,
    straight_mask,
    write_yaw_offset,
)


def _ts(seconds: float) -> datetime:
    return datetime(2026, 6, 1, 12, 0, tzinfo=UTC) + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Individual building blocks
# ---------------------------------------------------------------------------


def test_quiescent_mask_true_only_when_speed_and_heading_are_steady() -> None:
    n = 40
    speed = pd.Series([60.0] * 20 + list(np.linspace(60, 90, 20)))
    heading = pd.Series([90.0] * n)
    mask = quiescent_mask(speed, heading, window=5)
    # Steady cruise should be quiescent once the rolling window has filled;
    # the accelerating tail should not be.
    assert bool(mask.iloc[10])
    assert not bool(mask.iloc[-1])


def test_quiescent_mask_without_heading_falls_back_to_speed_only() -> None:
    speed = pd.Series([60.0] * 20)
    mask = quiescent_mask(speed, None, window=5)
    assert bool(mask.iloc[-1])


def test_straight_mask_excludes_a_turn_but_not_an_accel_event() -> None:
    heading = pd.Series([90.0] * 15 + list(np.linspace(90, 180, 15)))
    mask = straight_mask(heading, window=5)
    assert bool(mask.iloc[10])
    assert not bool(mask.iloc[-1])


def test_estimate_gravity_converges_to_tilted_vector_during_quiescence() -> None:
    n = 200
    tilt = math.radians(8.0)
    gravity_true = np.array([math.sin(tilt), 0.0, math.cos(tilt)])
    accel = pd.DataFrame(
        {
            "x": gravity_true[0],
            "y": gravity_true[1],
            "z": gravity_true[2],
        },
        index=range(n),
    )
    mask = pd.Series(True, index=accel.index)
    gravity = estimate_gravity(accel, mask, pd.Series([_ts(i * 0.01) for i in range(n)]))
    assert gravity.iloc[-1]["x"] == pytest.approx(gravity_true[0], abs=1e-3)
    assert gravity.iloc[-1]["z"] == pytest.approx(gravity_true[2], abs=1e-3)


def test_estimate_gravity_holds_last_value_on_non_quiescent_rows() -> None:
    accel = pd.DataFrame({"x": [0.0] * 50 + [5.0] * 10, "y": 0.0, "z": [1.0] * 50 + [0.0] * 10})
    mask = pd.Series([True] * 50 + [False] * 10)
    gravity = estimate_gravity(accel, mask, pd.Series([_ts(i * 0.01) for i in range(60)]))
    # Held at the last quiescent estimate, not contaminated by the
    # non-quiescent spike.
    assert gravity.iloc[-1]["x"] == pytest.approx(0.0, abs=1e-6)
    assert gravity.iloc[-1]["z"] == pytest.approx(1.0, abs=1e-6)


def test_resolve_yaw_offset_recovers_known_axis_mod_pi() -> None:
    true_theta = math.radians(25.0)
    n = 100
    # Points lying along the true_theta line, alternating sign (accel vs.
    # brake both lie on the same axis) — exactly what the double-angle
    # formula is built to resolve mod pi.
    magnitudes = np.array([0.4 if i % 2 == 0 else -0.3 for i in range(n)])
    h1 = magnitudes * math.cos(true_theta)
    h2 = magnitudes * math.sin(true_theta)
    mask = pd.Series(True, index=range(n))
    offset = resolve_yaw_offset(pd.Series(h1), pd.Series(h2), mask)
    assert offset is not None
    # mod pi: recovered offset is true_theta or true_theta + pi.
    diff = (offset - true_theta) % math.pi
    assert min(diff, math.pi - diff) == pytest.approx(0.0, abs=1e-6)


def test_resolve_yaw_offset_none_when_nothing_usable() -> None:
    h1 = pd.Series([0.001] * 10)
    h2 = pd.Series([0.001] * 10)
    mask = pd.Series([False] * 10)
    assert resolve_yaw_offset(h1, h2, mask) is None


def test_resolve_longitudinal_sign_flips_when_anti_correlated_with_obd() -> None:
    timestamps = pd.Series([_ts(i) for i in range(10)])
    speed = pd.Series(np.linspace(0, 90, 10))  # accelerating: positive d(speed)/dt
    lon = pd.Series([-0.3] * 10)  # IMU reports negative during an accel event
    corrected, flipped = resolve_longitudinal_sign(lon, speed, timestamps)
    assert flipped is True
    assert (corrected > 0).all()


def test_resolve_longitudinal_sign_keeps_sign_when_correlated() -> None:
    timestamps = pd.Series([_ts(i) for i in range(10)])
    speed = pd.Series(np.linspace(0, 90, 10))
    lon = pd.Series([0.3] * 10)
    corrected, flipped = resolve_longitudinal_sign(lon, speed, timestamps)
    assert flipped is False
    assert (corrected == 0.3).all()


# ---------------------------------------------------------------------------
# Full resolve_frame: known tilt + known maneuvers
# ---------------------------------------------------------------------------


def _build_synthetic_session(
    *, pitch_deg: float, yaw_deg: float, hz: float = 10.0
) -> tuple[pd.DataFrame, float]:
    """A synthetic session with a known mount pitch + yaw and four labeled
    segments: quiescent cruise, straight accel, straight brake, and a
    constant-speed turn — enough to exercise gravity, heading, and sign
    resolution against ground truth we chose ourselves."""
    pitch = math.radians(pitch_deg)
    yaw = math.radians(yaw_deg)

    gravity_body = np.array([math.sin(pitch), 0.0, math.cos(pitch)])
    # Vehicle-forward/lateral axes as seen in body coordinates: the pitch
    # tilts the whole rigid mount (so "forward" picks up the same pitch
    # rotation gravity did); the yaw then additionally rotates the mount's
    # horizontal axes relative to the vehicle's actual forward/lateral.
    u = np.array([math.cos(pitch), 0.0, -math.sin(pitch)])  # pitch-tilted forward-ish
    v = np.array([0.0, 1.0, 0.0])  # untouched by a pure pitch
    e_lon_body = math.cos(yaw) * u + math.sin(yaw) * v
    e_lat_body = -math.sin(yaw) * u + math.cos(yaw) * v

    dt = 1.0 / hz
    segments = [
        # (duration_s, speed_start, speed_end, heading_start, heading_end, lon_g, lat_g)
        (5.0, 60.0, 60.0, 90.0, 90.0, 0.0, 0.0),  # quiescent cruise
        (5.0, 60.0, 90.0, 90.0, 90.0, 0.30, 0.0),  # straight hard accel
        (5.0, 90.0, 90.0, 90.0, 90.0, 0.0, 0.0),  # quiescent cruise
        (5.0, 90.0, 60.0, 90.0, 90.0, -0.35, 0.0),  # straight hard brake
        (5.0, 60.0, 60.0, 90.0, 180.0, 0.0, 0.25),  # constant-speed turn
    ]

    rows: list[dict[str, object]] = []
    t = 0.0
    for duration, speed0, speed1, heading0, heading1, lon_g, lat_g in segments:
        n = int(duration * hz)
        speeds = np.linspace(speed0, speed1, n)
        headings = np.linspace(heading0, heading1, n)
        for i in range(n):
            linear_body = lon_g * e_lon_body + lat_g * e_lat_body
            accel_body = gravity_body + linear_body
            rows.append(
                {
                    "timestamp": _ts(t),
                    "imu:ACCEL_X": accel_body[0],
                    "imu:ACCEL_Y": accel_body[1],
                    "imu:ACCEL_Z": accel_body[2],
                    "obd:SPEED": speeds[i],
                    "gps:heading": headings[i],
                    "_true_lon": lon_g,
                    "_true_lat": lat_g,
                }
            )
            t += dt

    return pd.DataFrame(rows), yaw


#: The production masks' look-ahead at the synthetic sessions' 10 Hz. A
#: trailing mask marks the first second of each pull quiescent and feeds
#: it to the gravity low-pass (``QUIESCENT_LOOKAHEAD_SECONDS``); with the
#: bias-corrected low-pass (M01.13) 5 s of cruise no longer outweighs it,
#: so the pull read 0.249 g of 0.30 that way. With it: 0.300 exactly.
_LOOKAHEAD_10HZ = round(QUIESCENT_LOOKAHEAD_SECONDS * 10.0)


def test_resolve_frame_recovers_known_tilt_and_maneuvers() -> None:
    frame, true_yaw = _build_synthetic_session(pitch_deg=8.0, yaw_deg=20.0)

    result = resolve_frame(frame, quiescent_window=10, quiescent_lookahead=_LOOKAHEAD_10HZ)

    assert result.yaw_offset_rad is not None
    diff = (result.yaw_offset_rad - true_yaw) % math.pi
    assert min(diff, math.pi - diff) < math.radians(2.0)

    lon = result.linear_accel_lon
    lat = result.linear_accel_lat
    true_lon = frame["_true_lon"]
    true_lat = frame["_true_lat"]

    # Sample well inside each labeled segment (away from transition edges)
    # and compare recovered vs. ground truth.
    accel_rows = frame.index[(true_lon > 0.2)]
    brake_rows = frame.index[(true_lon < -0.2)]
    turn_rows = frame.index[(true_lat > 0.2)]
    quiescent_rows = frame.index[(true_lon == 0.0) & (true_lat == 0.0)]

    assert lon.loc[accel_rows].mean() == pytest.approx(0.30, abs=0.05)
    assert lon.loc[brake_rows].mean() == pytest.approx(-0.35, abs=0.05)
    assert lat.loc[turn_rows].mean() == pytest.approx(0.25, abs=0.05)
    assert lon.loc[quiescent_rows].abs().mean() < 0.05
    assert lat.loc[quiescent_rows].abs().mean() < 0.05


def test_resolve_frame_bno055_bypass_shares_yaw_and_sign_code() -> None:
    """gravity_removed=True (BNO055 path) skips the low-pass and reads
    imu:ACCEL_X/Y as already-fused horizontal components directly, but
    still runs the same heading + sign resolution as the raw-chip path."""
    true_yaw = math.radians(15.0)
    n = 100
    heading = pd.Series([90.0] * n)
    speed = pd.Series(np.linspace(0, 90, n))  # accelerating throughout
    # Fused horizontal accel already gravity-removed, expressed along an
    # unknown (to the code) body-horizontal axis rotated by true_yaw from
    # vehicle-forward.
    lon_true = 0.4
    h1 = lon_true * math.cos(true_yaw)
    h2 = lon_true * math.sin(true_yaw)
    frame = pd.DataFrame(
        {
            "timestamp": [_ts(i * 0.1) for i in range(n)],
            "imu:ACCEL_X": h1,
            "imu:ACCEL_Y": h2,
            "obd:SPEED": speed,
            "gps:heading": heading,
        }
    )

    result = resolve_frame(frame, gravity_removed=True, quiescent_window=10)

    assert result.yaw_offset_rad is not None
    diff = (result.yaw_offset_rad - true_yaw) % math.pi
    assert min(diff, math.pi - diff) < math.radians(2.0)
    assert result.linear_accel_lon.iloc[-1] == pytest.approx(lon_true, abs=0.05)


def test_resolve_frame_reports_unresolved_heading_without_gps() -> None:
    frame, _ = _build_synthetic_session(pitch_deg=0.0, yaw_deg=0.0)
    frame = frame.drop(columns=["gps:heading"])
    result = resolve_frame(frame, quiescent_window=10)
    assert result.yaw_offset_rad is None


def test_resolve_frame_accepts_a_persisted_yaw_offset_override() -> None:
    frame, true_yaw = _build_synthetic_session(pitch_deg=5.0, yaw_deg=10.0)
    result = resolve_frame(frame, quiescent_window=10, yaw_offset_rad=true_yaw)
    assert result.yaw_offset_rad == true_yaw


# ---------------------------------------------------------------------------
# Yaw offset persistence
# ---------------------------------------------------------------------------


def test_write_and_read_yaw_offset_roundtrip(tmp_path: Path) -> None:
    write_yaw_offset(tmp_path, 0.4321)
    assert read_yaw_offset(tmp_path) == pytest.approx(0.4321)


def test_read_yaw_offset_missing_returns_none(tmp_path: Path) -> None:
    assert read_yaw_offset(tmp_path) is None


# ---------------------------------------------------------------------------
# Speed source (H02.4)
# ---------------------------------------------------------------------------


def test_select_speed_prefers_0x2d1_then_obd_then_gps_and_scales_counts() -> None:
    assert SPEED_SOURCES == ("decoded:VEHICLE_SPEED_2D1", "obd:SPEED", "gps:speed")
    bindings = load_bindings(Path("data/bindings.yaml"))
    frame = pd.DataFrame(
        {
            "decoded:VEHICLE_SPEED_2D1": [600.0, np.nan],
            "obd:SPEED": [55.0, 56.0],
            "gps:speed": [61.0, 62.0],
        }
    )
    selected = select_speed(frame, bindings=bindings)
    assert selected is not None
    speed, source = selected
    assert source == "decoded:VEHICLE_SPEED_2D1"
    assert speed.iloc[0] == pytest.approx(60.0)  # 0.1 km/h per count, never raw

    selected = select_speed(frame.drop(columns=["decoded:VEHICLE_SPEED_2D1"]))
    assert selected is not None and selected[1] == "obd:SPEED"
    selected = select_speed(frame[["gps:speed"]])
    assert selected is not None and selected[1] == "gps:speed"
    assert select_speed(pd.DataFrame({"obd:SPEED": [np.nan]})) is None


def test_resolve_frame_without_obd_speed_steers_by_gps() -> None:
    """The quiescent mask no longer needs obd:SPEED (tuning captures)."""
    frame, true_yaw = _build_synthetic_session(pitch_deg=8.0, yaw_deg=20.0)
    frame = frame.rename(columns={"obd:SPEED": "gps:speed"})
    result = resolve_frame(frame, quiescent_window=10, quiescent_lookahead=_LOOKAHEAD_10HZ)
    assert result.yaw_offset_rad is not None
    diff = (result.yaw_offset_rad - true_yaw) % math.pi
    assert min(diff, math.pi - diff) < math.radians(2.0)
    accel_rows = frame.index[frame["_true_lon"] > 0.2]
    assert result.linear_accel_lon.loc[accel_rows].mean() == pytest.approx(0.30, abs=0.05)


def test_a_sign_flip_keeps_lateral_left_positive() -> None:
    """LAT = up x LON: when the longitudinal sign flips, lateral flips with it."""
    frame, _ = _build_synthetic_session(pitch_deg=5.0, yaw_deg=200.0)
    result = resolve_frame(frame, quiescent_window=10, quiescent_lookahead=_LOOKAHEAD_10HZ)
    assert result.sign_flipped is True
    turn_rows = frame.index[frame["_true_lat"] > 0.2]
    accel_rows = frame.index[frame["_true_lon"] > 0.2]
    assert result.linear_accel_lat.loc[turn_rows].mean() == pytest.approx(0.25, abs=0.05)
    assert result.linear_accel_lon.loc[accel_rows].mean() == pytest.approx(0.30, abs=0.05)


def test_quiescent_mask_lookahead_excludes_rows_before_a_speed_change() -> None:
    speed = pd.Series([0.0] * 20 + [10.0] * 10)
    trailing = quiescent_mask(speed, None, window=5)
    ahead = quiescent_mask(speed, None, window=5, lookahead=3)
    assert bool(trailing.iloc[19]) and not bool(ahead.iloc[19])
    assert not bool(ahead.iloc[17]) and bool(ahead.iloc[16])


# ---------------------------------------------------------------------------
# M01.13: the gravity low-pass has a time constant in seconds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rate_hz", [12.5, 104.0, 208.0])
def test_the_gravity_time_constant_is_the_same_at_every_imu_rate(rate_hz: float) -> None:
    """``α = 1 − exp(−Δt/τ)``: the implied time constant ``−Δt / ln(1 − α)``
    is :data:`GRAVITY_TIME_CONSTANT_SECONDS` at each rate the LSM6DSOX runs
    at. The old per-sample ``α = 0.02`` gave 3.96 s, 0.48 s and 0.24 s here."""
    dt = 1.0 / rate_hz
    alpha = gravity_low_pass_alpha(dt)
    assert -dt / math.log1p(-alpha) == pytest.approx(GRAVITY_TIME_CONSTANT_SECONDS, rel=1e-12)


def test_a_sample_after_a_gap_moves_the_estimate_by_one_sample_at_most() -> None:
    """An IMU dropout's first sample observed its own period, not the gap: it
    is capped at :data:`GRAVITY_MAX_SAMPLE_SECONDS` rather than taking
    ``α → 1`` and replacing the estimate with one vibrating sample. A
    non-positive interval moves nothing; a missing one (the session's first
    sample) counts as the cap."""
    capped = gravity_low_pass_alpha(GRAVITY_MAX_SAMPLE_SECONDS)
    assert gravity_low_pass_alpha(40.0) == capped < 0.011
    assert gravity_low_pass_alpha(math.nan) == capped
    assert gravity_low_pass_alpha(0.0) == 0.0
    assert gravity_low_pass_alpha(-0.01) == 0.0


def test_offline_and_live_low_pass_steps_are_bit_identical() -> None:
    """The offline loop (``low_pass_gravity`` over ``sample_intervals_seconds``
    of the stored instants) and the live frame (``gravity_step`` with
    ``timedelta.total_seconds()`` between consecutive samples) compute the
    same bits from the same samples: irregular microsecond stamps, a gap."""
    rng = np.random.default_rng(3)
    steps_us = rng.integers(9_000, 10_500, 400)
    steps_us[150] = 2_500_000  # a dropout
    stamps = [_ts(0.0) + timedelta(microseconds=int(us)) for us in np.cumsum(steps_us)]
    samples = rng.normal([0.1, -0.05, 1.0], 0.03, (len(stamps), 3))

    offline, final = low_pass_gravity(samples, sample_intervals_seconds(pd.Series(stamps)))

    state = GRAVITY_START
    for i, row in enumerate(samples.tolist()):
        interval = math.nan if i == 0 else (stamps[i] - stamps[i - 1]).total_seconds()
        state = gravity_step(state, (row[0], row[1], row[2]), gravity_low_pass_alpha(interval))
        assert offline[i].tolist() == list(state.estimate)
    assert final == state
    assert offline[0].tolist() == samples[0].tolist()  # the first sample, exactly


def test_the_low_pass_is_a_mean_until_its_weight_fills() -> None:
    """Bias-corrected: before a few τ of quiescent samples the estimate is
    their time-weighted mean, not the first sample held for τ, so one noisy
    seed sample does not bias the first seconds (at τ = 10 s it would still
    carry 37 % of the estimate 10 s later). Two equal-interval samples average
    to about their midpoint; the weight is ``1 − exp(−T/τ)``."""
    out, state = low_pass_gravity(
        np.array([[0.0, 0.0, 0.9], [0.0, 0.0, 1.1]]), np.array([0.01, 0.01])
    )
    assert out[1][2] == pytest.approx(1.0, abs=1e-4)
    rng = np.random.default_rng(5)
    n = 100 * 30  # 30 s at 100 Hz
    noisy = rng.normal([0.0, 0.0, 1.0], 0.05, (n, 3))
    out, state = low_pass_gravity(noisy, np.full(n, 0.01))
    assert state.weight == pytest.approx(-math.expm1(-30.0 / GRAVITY_TIME_CONSTANT_SECONDS))
    # One second in: the mean of 100 samples (σ 5 mg), not the seed (σ 50 mg).
    assert abs(out[99][2] - noisy[:100, 2].mean()) < 1e-3


def _gentle_pull_session(rate_hz: float) -> pd.DataFrame:
    """Level road, flat mount tilt (8° pitch, 20° yaw): cruise, one firm
    straight pull (yaw and sign), cruise, then a 10 s 0.035 g pull — gentle
    enough that its speed (1.24 km/h/s) passes the quiescence gate the whole
    way — and cruise again."""
    pitch, yaw = math.radians(8.0), math.radians(20.0)
    gravity_body = np.array([math.sin(pitch), 0.0, math.cos(pitch)])
    u = np.array([math.cos(pitch), 0.0, -math.sin(pitch)])
    v = np.array([0.0, 1.0, 0.0])
    e_lon = math.cos(yaw) * u + math.sin(yaw) * v
    segments = [(30.0, 0.0), (5.0, 0.25), (30.0, 0.0), (10.0, 0.035), (10.0, 0.0)]
    rows: list[dict[str, object]] = []
    kph, step = 50.0, round(1e6 / rate_hz)
    i = 0
    for seconds, lon_g in segments:
        for _ in range(round(seconds * rate_hz)):
            body = gravity_body + lon_g * e_lon
            rows.append(
                {
                    "timestamp": _ts(0.0) + timedelta(microseconds=i * step),
                    "imu:ACCEL_X": body[0],
                    "imu:ACCEL_Y": body[1],
                    "imu:ACCEL_Z": body[2],
                    "gps:speed": kph,
                    "gps:heading": 90.0,
                    "_true_lon": lon_g,
                }
            )
            kph += lon_g * 9.80665 * 3.6 / rate_hz
            i += 1
    return pd.DataFrame(rows)


def test_a_sustained_gentle_pull_reads_the_same_at_every_imu_rate() -> None:
    """M01.13's defect, end to end through ``resolve_frame``: a 10 s 0.035 g
    pull passes the quiescence gate, so the low-pass absorbs it at its time
    constant. With τ in seconds the pull's last second reads the same at
    12.5, 104 and 208 Hz, and keeps ``exp(−t/τ)`` of the pull (about a third at
    τ = 10 s once the gate's window has filled); at α = 0.02 per sample it read
    under 1 % of the pull at 104 and 208 Hz."""
    kept: list[float] = []
    for rate_hz in (12.5, 104.0, 208.0):
        frame = _gentle_pull_session(rate_hz)
        result = resolve_frame(
            frame,
            quiescent_window=round(2.0 * rate_hz),
            quiescent_lookahead=round(QUIESCENT_LOOKAHEAD_SECONDS * rate_hz),
        )
        assert result.yaw_offset_rad is not None and not result.sign_flipped
        pull = frame.index[frame["_true_lon"] == 0.035]
        last_second = pull[-round(rate_hz) :]
        kept.append(float(result.linear_accel_lon.loc[last_second].mean()) / 0.035)
    assert max(kept) - min(kept) < 0.03, kept
    assert all(0.3 < k < 0.6 for k in kept), kept


def test_a_non_finite_sample_is_skipped_not_carried() -> None:
    """One NaN axis on a quiescent row leaves the estimate where it was and the
    filter keeps tracking (the ``ewm`` it replaced skipped NaNs too); it never
    freezes that axis for the rest of the session. Live and offline skip it the
    same way (``gravity_step`` returns the state itself), and a NaN first sample
    yields no estimate rather than ``GRAVITY_START``'s zeros."""
    n = 300
    stamps = pd.Series([_ts(i * 0.01) for i in range(n)])
    accel = pd.DataFrame({"x": [0.1] * 150 + [0.2] * 150, "y": 0.0, "z": 1.0})
    accel.loc[100, "x"] = math.nan
    accel.loc[0, "y"] = math.nan
    mask = pd.Series(True, index=accel.index)
    gravity = estimate_gravity(accel, mask, stamps)
    assert gravity.notna().all().all()
    assert float(gravity["x"].iloc[0]) == pytest.approx(0.1)  # back-filled, not 0
    assert float(gravity["x"].iloc[101]) == pytest.approx(0.1)
    rise = -math.expm1(-149 * 0.01 / GRAVITY_TIME_CONSTANT_SECONDS)
    assert float(gravity["x"].iloc[-1]) > 0.1 + 0.05 * rise  # still tracking the step

    values = accel.to_numpy()
    offline, _ = low_pass_gravity(values, sample_intervals_seconds(stamps))
    state = GRAVITY_START
    for i, row in enumerate(values.tolist()):
        interval = math.nan if i == 0 else (stamps[i] - stamps[i - 1]).total_seconds()
        stepped = gravity_step(state, (row[0], row[1], row[2]), gravity_low_pass_alpha(interval))
        assert (stepped is state) == (i in (0, 100))
        state = stepped
        if i:
            assert offline[i].tolist() == list(state.estimate)
    assert np.isnan(offline[0]).all()
