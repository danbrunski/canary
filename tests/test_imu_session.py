"""Per-session IMU frame resolution: both families, refusals, sidecar (H02.4).

Every scenario is synthetic with a known answer: a mount rotation we chose, a
road grade we chose, manoeuvres we chose. The vehicle frame is ISO 8855 (x
forward, y left, z up); the chip sees ``M @ f_vehicle`` for specific force
``f_vehicle = (a + sin θ, a_lat, cos θ)`` in g.
"""

from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis.dyno import (
    DynoRefusal,
    Leg,
    MotionSamples,
    PitchOffset,
    correct_specific_force,
    estimate_pitch_offset,
)
from src.preprocessing.imu_frame import QUIESCENT_LOOKAHEAD_SECONDS
from src.preprocessing.imu_session import (
    IMU_FRAME_FILENAME,
    RESIDUAL_PITCH_OFFSET,
    SF_LAT_COLUMN,
    SF_LON_COLUMN,
    SF_VERT_COLUMN,
    ImuFrameOfRecordError,
    ImuFrameRefusal,
    ImuSessionFrame,
    channels_from_long,
    read_imu_frame_sidecar,
    refused_sidecar,
    resolve_imu_frame,
    resolve_session_imu_frame,
    specific_force_from_sidecar,
    write_imu_frame_sidecar,
)
from src.signals.committees import STANDARD_GRAVITY
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from src.trainability import SOURCE_NOT_TRAINABLE, source_exclusion_reason

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
IMU_HZ = 50.0
NOW = datetime(2026, 9, 26, 13, 0, tzinfo=UTC)


def _rot(axis: str, deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    if axis == "x":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == "y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def mount(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """Body ← vehicle rotation for a mount rolled, pitched and yawed (deg)."""
    return _rot("z", yaw) @ _rot("y", pitch) @ _rot("x", roll)


@dataclass(frozen=True)
class Segment:
    seconds: float
    kph0: float
    kph1: float
    heading0: float = 90.0
    heading1: float = 90.0
    grade: float = 0.0  # rise over run, uphill in the direction of travel
    lat_g: float = 0.0  # left positive


@dataclass(frozen=True)
class Truth:
    frame: pd.DataFrame  # t, kph, heading, a_g, grade_rad, lat_g, accel body xyz
    mount: np.ndarray


def drive(
    segments: list[Segment],
    *,
    mount_matrix: np.ndarray,
    noise_g: float = 0.0,
    seed: int = 0,
) -> Truth:
    rng = np.random.default_rng(seed)
    rows = []
    t = 0.0
    dt = 1.0 / IMU_HZ
    for seg in segments:
        n = round(seg.seconds * IMU_HZ)
        a_g = (seg.kph1 - seg.kph0) / 3.6 / seg.seconds / STANDARD_GRAVITY
        theta = math.atan(seg.grade)
        for i in range(n):
            frac = i / n
            f_vehicle = np.array([a_g + math.sin(theta), seg.lat_g, math.cos(theta)])
            body = mount_matrix @ f_vehicle + rng.normal(0.0, noise_g, 3)
            rows.append(
                {
                    "t": t,
                    "kph": seg.kph0 + (seg.kph1 - seg.kph0) * frac,
                    "heading": (seg.heading0 + (seg.heading1 - seg.heading0) * frac) % 360.0,
                    "a_g": a_g,
                    "grade_rad": theta,
                    "lat_g": seg.lat_g,
                    "x": body[0],
                    "y": body[1],
                    "z": body[2],
                }
            )
            t += dt
    return Truth(frame=pd.DataFrame(rows), mount=mount_matrix)


def channels(
    truth: Truth,
    *,
    speed: tuple[str, ...] = ("obd:SPEED", "gps:speed"),
    heading: bool = True,
    imu: bool = True,
) -> dict[str, pd.Series]:
    """The recorded channels: IMU at full rate, OBD/GPS at 1 Hz, 0x2D1 at 10 Hz
    as raw counts (0.1 km/h per count)."""
    f = truth.frame
    index = pd.DatetimeIndex([T0 + timedelta(seconds=s) for s in f["t"]])
    out: dict[str, pd.Series] = {}
    if imu:
        for axis in "xyz":
            out[f"imu:ACCEL_{axis.upper()}"] = pd.Series(f[axis].to_numpy(), index=index)
    one_hz = (f["t"] * IMU_HZ).round().astype(int) % round(IMU_HZ) == 0
    ten_hz = (f["t"] * IMU_HZ).round().astype(int) % round(IMU_HZ / 10) == 0
    if "obd:SPEED" in speed:
        out["obd:SPEED"] = pd.Series(f.loc[one_hz, "kph"].round().to_numpy(), index=index[one_hz])
    if "gps:speed" in speed:
        out["gps:speed"] = pd.Series(f.loc[one_hz, "kph"].to_numpy(), index=index[one_hz])
    if "decoded:VEHICLE_SPEED_2D1" in speed:
        counts = (f.loc[ten_hz, "kph"] * 10.0).round().to_numpy()
        out["decoded:VEHICLE_SPEED_2D1"] = pd.Series(counts, index=index[ten_hz])
    if heading:
        out["gps:heading"] = pd.Series(f.loc[one_hz, "heading"].to_numpy(), index=index[one_hz])
    return out


def resolved(result: ImuSessionFrame | ImuFrameRefusal) -> ImuSessionFrame:
    assert isinstance(result, ImuSessionFrame), result
    return result


def rows_where(truth: Truth, mask: pd.Series, *, trim_s: float = 1.0) -> np.ndarray:
    """Positions inside ``mask`` runs, ``trim_s`` away from each run's edges."""
    values = mask.to_numpy(dtype=bool)
    trim = round(trim_s * IMU_HZ)
    keep = np.zeros_like(values)
    padded = np.concatenate([[False], values, [False]]).astype(np.int8)
    edges = np.flatnonzero(np.diff(padded))
    for start, stop in zip(edges[::2], edges[1::2], strict=True):
        keep[start + trim : stop - trim] = True
    return np.flatnonzero(keep)


# A drive with every manoeuvre: stop, pull, cruise, brake, stop, pull, left turn.
MANOEUVRES = [
    Segment(12.0, 0.0, 0.0),
    Segment(6.0, 0.0, 60.0),  # +0.283 g
    Segment(10.0, 60.0, 60.0),
    Segment(5.0, 60.0, 0.0),  # -0.340 g
    Segment(8.0, 0.0, 0.0),
    Segment(5.0, 0.0, 40.0),
    Segment(6.0, 40.0, 40.0),
    Segment(8.0, 40.0, 40.0, heading0=90.0, heading1=0.0, lat_g=0.25),  # left turn
    Segment(8.0, 40.0, 40.0, heading0=0.0, heading1=0.0),
]


# ---------------------------------------------------------------------------
# Known tilt + known manoeuvres: both families
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("yaw_deg", [25.0, 205.0])
def test_known_tilt_and_manoeuvres_resolve_both_families(yaw_deg: float) -> None:
    """A mount rolled 6°, pitched −8° and yawed 25° (or 205°: the axis fit
    lands backwards and the sign must flip) on flat ground. SF_* equal the
    vehicle-frame specific force; ACCEL_* equal it without gravity; LAT is
    left-positive either way."""
    truth = drive(MANOEUVRES, mount_matrix=mount(6.0, -8.0, yaw_deg), noise_g=0.005)
    out = resolved(resolve_imu_frame(channels(truth), now=NOW, change_only_timeout_seconds=None))
    f, frame = truth.frame, out.frame

    pull = rows_where(truth, f["a_g"] > 0.2)
    brake = rows_where(truth, f["a_g"] < -0.2)
    turn = rows_where(truth, f["lat_g"] > 0.2)
    still = rows_where(truth, (f["a_g"] == 0) & (f["lat_g"] == 0))

    for column in (SF_LON_COLUMN, "imu:ACCEL_LON"):
        for rows in (pull, brake):
            expected = f["a_g"].iloc[rows].mean()
            assert frame[column].iloc[rows].mean() == pytest.approx(expected, abs=0.01)
        assert abs(frame[column].iloc[still].mean()) < 0.01
    for column in (SF_LAT_COLUMN, "imu:ACCEL_LAT"):
        assert frame[column].iloc[turn].mean() == pytest.approx(0.25, abs=0.01)
        assert abs(frame[column].iloc[pull].mean()) < 0.01
    assert frame[SF_VERT_COLUMN].iloc[still].mean() == pytest.approx(1.0, abs=0.005)
    assert frame[SF_VERT_COLUMN].iloc[turn].mean() == pytest.approx(1.0, abs=0.005)

    sf = out.sidecar.specific_force
    assert sf is not None
    assert sf.sign_flipped is (yaw_deg > 180.0)
    linear = out.sidecar.linear_accel
    assert linear is not None and linear.sign_flipped is (yaw_deg > 180.0)
    # The recorded rotation is the mount's inverse: rows = vehicle axes in body.
    rotation = np.array(sf.rotation_body_to_vehicle)
    np.testing.assert_allclose(rotation, truth.mount.T, atol=0.01)


def test_quiescent_lookahead_keeps_a_pull_from_rest_out_of_the_reference() -> None:
    """GPS speed is 1 Hz: for up to a second after a pull starts the recorded
    speed still reads 0. A trailing window counts that second as quiescent
    and tilts the reference by the pull; the look-ahead does not."""
    truth = drive(MANOEUVRES, mount_matrix=mount(0.0, 0.0, 0.0))
    out = resolved(
        resolve_imu_frame(
            channels(truth, speed=("gps:speed",)), now=NOW, change_only_timeout_seconds=None
        )
    )
    sf = out.sidecar.specific_force
    assert sf is not None
    reference = np.array(sf.pitch_reference.gravity_body_g)
    tilt = math.degrees(math.atan2(reference[0], reference[2]))
    assert abs(tilt) < 0.01  # the flat reference's pitch, exactly 0 without leaks
    assert out.sidecar.inputs is not None
    assert out.sidecar.inputs.quiescent_lookahead_seconds == QUIESCENT_LOOKAHEAD_SECONDS


# ---------------------------------------------------------------------------
# Known grade
# ---------------------------------------------------------------------------


def test_sf_lon_on_a_known_grade_is_a_plus_g_sin_grade() -> None:
    """Reference on flat ground (every quiescent window is at rest before the
    hill); the pull is made on a 4 % grade. SF_LON = a + sin θ within 2 mg."""
    grade = 0.04
    segments = [
        Segment(15.0, 0.0, 0.0),
        Segment(6.0, 0.0, 30.0),  # straight pull on the flat: resolves yaw and sign
        Segment(10.0, 30.0, 70.0, grade=grade),  # still pulling, now uphill
        Segment(6.0, 70.0, 20.0, grade=grade),
    ]
    truth = drive(segments, mount_matrix=mount(3.0, 5.0, 40.0), noise_g=0.003)
    out = resolved(resolve_imu_frame(channels(truth), now=NOW, change_only_timeout_seconds=None))
    f = truth.frame
    uphill_pull = rows_where(truth, (f["grade_rad"] > 0) & (f["a_g"] > 0))
    uphill_brake = rows_where(truth, (f["grade_rad"] > 0) & (f["a_g"] < 0))
    theta = math.atan(grade)
    for rows in (uphill_pull, uphill_brake):
        expected = (f["a_g"].iloc[rows] + math.sin(theta)).mean()
        assert out.frame[SF_LON_COLUMN].iloc[rows].mean() == pytest.approx(expected, abs=0.002)
    sf = out.sidecar.specific_force
    assert sf is not None
    windows = sf.pitch_reference.windows
    assert windows and all(w.mean_speed_kph == 0.0 for w in windows)
    assert sf.pitch_reference.stationary_fraction == 1.0


def test_accel_lon_excludes_the_grade_after_a_steady_climb() -> None:
    """A steady climb (quiescent) lets the low-pass take the grade out:
    ACCEL_LON of the pull on the hill is the pull alone, while SF_LON keeps
    the grade less the reference's own mean pitch (recorded)."""
    grade = 0.05
    segments = [
        Segment(10.0, 0.0, 0.0),
        Segment(5.0, 0.0, 40.0),
        Segment(20.0, 40.0, 40.0, grade=grade),  # steady climb: quiescent
        Segment(5.0, 40.0, 80.0, grade=grade),  # pull on the hill, +0.227 g
        Segment(5.0, 80.0, 80.0, grade=grade),
    ]
    truth = drive(segments, mount_matrix=mount(-4.0, 7.0, 130.0), noise_g=0.003)
    out = resolved(resolve_imu_frame(channels(truth), now=NOW, change_only_timeout_seconds=None))
    f = truth.frame
    pull = rows_where(truth, (f["grade_rad"] > 0) & (f["a_g"] > 0))
    a = f["a_g"].iloc[pull].mean()
    assert out.frame["imu:ACCEL_LON"].iloc[pull].mean() == pytest.approx(a, abs=0.01)

    # SF_LON = a·cos θr + sin(θ − θr), θr = the reference's mean pitch, which
    # the sidecar's gravity vector gives back through the known mount.
    sf = out.sidecar.specific_force
    assert sf is not None
    up_vehicle = truth.mount.T @ np.array(sf.pitch_reference.gravity_body_g)
    theta_r = math.atan2(up_vehicle[0], up_vehicle[2])
    theta = math.atan(grade)
    assert 0.3 * theta < theta_r < 0.8 * theta  # the climb is part of the reference
    expected = a * math.cos(theta_r) + math.sin(theta - theta_r)
    assert out.frame[SF_LON_COLUMN].iloc[pull].mean() == pytest.approx(expected, abs=0.002)
    assert sf.pitch_reference.pitch_offset_removed is False
    assert sf.pitch_reference.window_angle_spread_deg > 1.0  # the flat and the hill differ


def test_the_recorded_reference_composes_with_the_dyno_pitch_offset() -> None:
    """The dyno measures the residual pitch from a direction pair and removes
    it; nothing in the frame removed it first. Here the reference includes a
    steady climb, so SF_LON carries μ = −θr; estimate_pitch_offset recovers it
    and correct_specific_force leaves a + g·sin θ on both legs. The climb is
    longer than the descent, so the reference's mean pitch is not zero."""
    grade = 0.03
    segments = [
        Segment(10.0, 0.0, 0.0),
        Segment(5.0, 0.0, 50.0),
        Segment(40.0, 50.0, 50.0, heading0=90.0, heading1=90.0, grade=grade),  # up
        Segment(10.0, 50.0, 30.0, heading0=90.0, heading1=270.0),  # turn round
        Segment(20.0, 30.0, 30.0, heading0=270.0, heading1=270.0, grade=-grade),  # down
    ]
    truth = drive(segments, mount_matrix=mount(2.0, -6.0, 75.0))
    out = resolved(resolve_imu_frame(channels(truth), now=NOW, change_only_timeout_seconds=None))
    sf_block = out.sidecar.specific_force
    assert sf_block is not None
    f = truth.frame

    def leg(heading: str, mask: pd.Series) -> Leg:
        rows = rows_where(truth, mask, trim_s=2.5)
        return Leg(
            heading,
            MotionSamples(
                t_s=f["t"].iloc[rows].to_numpy(),
                speed_mps=f["kph"].iloc[rows].to_numpy() / 3.6,
                accel_mps2=f["a_g"].iloc[rows].to_numpy() * STANDARD_GRAVITY,
                sf_lon_mps2=out.frame[SF_LON_COLUMN].iloc[rows].to_numpy() * STANDARD_GRAVITY,
                imu_reference_id=sf_block.reference_id,
            ),
        )

    legs = [leg("eastbound", f["grade_rad"] > 0), leg("westbound", f["grade_rad"] < 0)]
    offset = estimate_pitch_offset(legs, squat=None)
    assert isinstance(offset, PitchOffset), offset
    assert offset.reference_id == sf_block.reference_id

    up_vehicle = truth.mount.T @ np.array(sf_block.pitch_reference.gravity_body_g)
    theta_r_deg = math.degrees(math.atan2(up_vehicle[0], up_vehicle[2]))
    assert theta_r_deg > 0.3  # a real offset, not a vacuous zero
    assert offset.deg == pytest.approx(-theta_r_deg, abs=0.01)
    for one, sign in zip(legs, (1.0, -1.0), strict=True):
        corrected = correct_specific_force(one.samples, squat=None, pitch_offset=offset)
        assert not isinstance(corrected, DynoRefusal), corrected
        expected = one.samples.accel_mps2 + STANDARD_GRAVITY * math.sin(sign * math.atan(grade))
        np.testing.assert_allclose(corrected, expected, atol=0.005)
    # The offset belongs to this reference: samples from another frame are refused.
    other = dataclasses.replace(legs[0].samples, imu_reference_id="0123456789abcdef")
    refused = correct_specific_force(other, squat=None, pitch_offset=offset)
    assert isinstance(refused, DynoRefusal) and refused.kind == "frame_mismatch"


# ---------------------------------------------------------------------------
# Speed sources
# ---------------------------------------------------------------------------


def test_a_session_without_obd_speed_resolves_from_0x2d1() -> None:
    """The ``tuning`` profile polls no obd:SPEED; the 0x2D1 broadcast (raw
    counts, scaled through the binding) steers instead."""
    truth = drive(MANOEUVRES, mount_matrix=mount(4.0, 3.0, 60.0), noise_g=0.005)
    recorded = channels(truth, speed=("decoded:VEHICLE_SPEED_2D1",))
    assert "obd:SPEED" not in recorded
    out = resolved(resolve_imu_frame(recorded, now=NOW, change_only_timeout_seconds=None))
    assert out.sidecar.inputs is not None
    assert out.sidecar.inputs.speed_source == "decoded:VEHICLE_SPEED_2D1"
    pull = rows_where(truth, truth.frame["a_g"] > 0.2)
    expected = truth.frame["a_g"].iloc[pull].mean()
    assert out.frame[SF_LON_COLUMN].iloc[pull].mean() == pytest.approx(expected, abs=0.01)


def test_a_session_without_obd_speed_resolves_from_gps() -> None:
    truth = drive(MANOEUVRES, mount_matrix=mount(4.0, 3.0, 60.0), noise_g=0.005)
    out = resolved(
        resolve_imu_frame(
            channels(truth, speed=("gps:speed",)), now=NOW, change_only_timeout_seconds=None
        )
    )
    assert out.sidecar.inputs is not None
    assert out.sidecar.inputs.speed_source == "gps:speed"


def test_0x2d1_outranks_obd_and_gps() -> None:
    truth = drive(MANOEUVRES, mount_matrix=mount(0.0, 0.0, 0.0))
    speeds = ("decoded:VEHICLE_SPEED_2D1", "obd:SPEED", "gps:speed")
    out = resolved(
        resolve_imu_frame(channels(truth, speed=speeds), now=NOW, change_only_timeout_seconds=None)
    )
    assert out.sidecar.inputs is not None
    assert out.sidecar.inputs.speed_source == "decoded:VEHICLE_SPEED_2D1"


# ---------------------------------------------------------------------------
# Refusals name their cause
# ---------------------------------------------------------------------------


def _refusal(result: ImuSessionFrame | ImuFrameRefusal) -> ImuFrameRefusal:
    assert isinstance(result, ImuFrameRefusal), result
    return result


def test_refusals_name_their_cause() -> None:
    truth = drive(MANOEUVRES, mount_matrix=mount(0.0, 0.0, 0.0))

    no_imu = _refusal(
        resolve_imu_frame(channels(truth, imu=False), change_only_timeout_seconds=None)
    )
    assert no_imu.kind == "imu_rows_missing" and "imu:ACCEL_X" in no_imu.reason

    no_speed = _refusal(
        resolve_imu_frame(channels(truth, speed=()), change_only_timeout_seconds=None)
    )
    assert no_speed.kind == "no_speed_source"
    assert "decoded:VEHICLE_SPEED_2D1" in no_speed.reason and "gps:speed" in no_speed.reason

    no_heading = _refusal(
        resolve_imu_frame(channels(truth, heading=False), change_only_timeout_seconds=None)
    )
    assert no_heading.kind == "no_heading" and "gps:heading" in no_heading.reason

    always_moving = drive(
        [Segment(10.0, 0.0, 50.0), Segment(10.0, 50.0, 0.0)] * 2,
        mount_matrix=mount(0.0, 0.0, 0.0),
    )
    no_rest = _refusal(resolve_imu_frame(channels(always_moving), change_only_timeout_seconds=None))
    assert no_rest.kind == "no_quiescent_period" and "obd:SPEED" in no_rest.reason

    never_straight_accel = drive(
        [Segment(20.0, 50.0, 50.0), Segment(10.0, 50.0, 50.0, heading1=180.0, lat_g=0.2)],
        mount_matrix=mount(0.0, 0.0, 0.0),
    )
    no_yaw = _refusal(
        resolve_imu_frame(channels(never_straight_accel), change_only_timeout_seconds=None)
    )
    assert no_yaw.kind == "yaw_unresolved" and "straight" in no_yaw.reason

    in_m_per_s2 = channels(truth)
    for axis in "XYZ":
        in_m_per_s2[f"imu:ACCEL_{axis}"] = in_m_per_s2[f"imu:ACCEL_{axis}"] * STANDARD_GRAVITY
    wrong_unit = _refusal(resolve_imu_frame(in_m_per_s2, change_only_timeout_seconds=None))
    assert wrong_unit.kind == "gravity_implausible" and "9.8" in wrong_unit.reason


def test_sign_unresolved_when_speed_never_changes_with_the_accel() -> None:
    """The IMU sees a straight 0.2 g pull the speed never shows (a frozen
    speed channel): the axis resolves, which end is forward cannot."""
    truth = drive(
        [Segment(10.0, 0.0, 0.0), Segment(5.0, 0.0, 35.0), Segment(10.0, 35.0, 35.0)],
        mount_matrix=mount(0.0, 0.0, 30.0),
    )
    recorded = channels(truth, speed=("gps:speed",))
    recorded["gps:speed"] = recorded["gps:speed"] * 0.0 + 20.0
    refusal = _refusal(resolve_imu_frame(recorded, change_only_timeout_seconds=None))
    assert refusal.kind == "sign_unresolved" and "gps:speed" in refusal.reason


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


def _write_session(session_dir: Path, recorded: dict[str, pd.Series]) -> None:
    records = []
    units = {"imu": "g", "obd": "km/h", "gps": "km/h"}
    for column, series in recorded.items():
        source, channel = column.split(":", 1)
        for ts, value in series.items():
            stamp = pd.Timestamp(ts).timestamp()
            unit = units.get(source, "")
            if source == "imu":
                records.append(TelemetryRecord.imu(stamp, channel, float(value), unit))
            elif source == "obd":
                records.append(TelemetryRecord.obd(stamp, channel, float(value), unit))
            elif source == "gps":
                records.append(TelemetryRecord.gps(stamp, channel, float(value), unit))
    session_dir.mkdir(parents=True, exist_ok=True)
    write_records_to_parquet(records, session_dir)


def test_session_caller_writes_a_sidecar_that_reproduces_sf(tmp_path: Path) -> None:
    truth = drive(MANOEUVRES, mount_matrix=mount(5.0, -3.0, 110.0), noise_g=0.004)
    session = tmp_path / "2026-09-26T120000"
    _write_session(session, channels(truth))

    out = resolved(resolve_session_imu_frame(session, now=NOW))
    sidecar = read_imu_frame_sidecar(session)
    assert sidecar == out.sidecar
    raw = json.loads((session / IMU_FRAME_FILENAME).read_text())
    assert raw["status"] == "resolved"
    reference = raw["specific_force"]["pitch_reference"]
    assert reference["pitch_offset_removed"] is False
    assert reference["residual_pitch_offset"] == RESIDUAL_PITCH_OFFSET
    assert reference["windows"][0]["samples"] > 0

    # SF_* recomputed from stored raw rows + the recorded rotation are the
    # resolver's SF_* exactly: the sidecar is the frame of record.
    raw_accel = channels(truth)
    accel = pd.DataFrame({c: raw_accel[c] for c in ("imu:ACCEL_X", "imu:ACCEL_Y", "imu:ACCEL_Z")})
    again = specific_force_from_sidecar(accel.reset_index(drop=True), sidecar)
    np.testing.assert_allclose(
        again.to_numpy(), out.frame[[SF_LON_COLUMN, SF_LAT_COLUMN, SF_VERT_COLUMN]].to_numpy()
    )

    long_rows = out.rows(session.name)
    assert set(long_rows["channel"]) == {"ACCEL_LON", "ACCEL_LAT", "SF_LON", "SF_LAT", "SF_VERT"}
    assert (long_rows["source"] == "imu").all() and (long_rows["unit"] == "g").all()


def test_a_refusal_is_written_and_names_its_cause(tmp_path: Path) -> None:
    truth = drive(MANOEUVRES, mount_matrix=mount(0.0, 0.0, 0.0))
    session = tmp_path / "2026-09-26T120000"
    _write_session(session, channels(truth, heading=False))
    refusal = _refusal(resolve_session_imu_frame(session, now=NOW))
    sidecar = read_imu_frame_sidecar(session)
    assert sidecar is not None and sidecar.status == "refused"
    assert sidecar.refusal is not None and sidecar.refusal.kind == refusal.kind == "no_heading"
    with pytest.raises(ValueError, match="no_heading"):
        specific_force_from_sidecar(pd.DataFrame(), sidecar)

    empty = tmp_path / "2026-09-26T130000"
    empty.mkdir()
    assert _refusal(resolve_session_imu_frame(empty, now=NOW)).kind == "no_telemetry"


def test_a_resolved_frame_is_not_silently_replaced(tmp_path: Path) -> None:
    """A dyno pitch offset is valid for one reference; a re-resolution that
    changes it (another quiescent window here) must be asked for."""
    truth = drive(MANOEUVRES, mount_matrix=mount(5.0, -3.0, 110.0), noise_g=0.004)
    session = tmp_path / "2026-09-26T120000"
    _write_session(session, channels(truth))
    first = resolved(resolve_session_imu_frame(session, now=NOW))
    # Same inputs, same reference: an idempotent rewrite.
    resolved(resolve_session_imu_frame(session, now=NOW))
    with pytest.raises(ImuFrameOfRecordError, match="replace=True"):
        resolve_session_imu_frame(session, quiescent_seconds=4.0, now=NOW)
    second = resolved(
        resolve_session_imu_frame(session, quiescent_seconds=4.0, replace=True, now=NOW)
    )
    assert first.sidecar.specific_force is not None and second.sidecar.specific_force is not None
    assert first.sidecar.specific_force.reference_id != second.sidecar.specific_force.reference_id
    # Nor replaced by a refusal: "resolved to nothing" would hide the frame.
    refusal = _refusal(
        resolve_imu_frame(channels(truth, heading=False), change_only_timeout_seconds=None)
    )
    with pytest.raises(ImuFrameOfRecordError, match="refusal"):
        write_imu_frame_sidecar(session, refused_sidecar(refusal, now=NOW))
    write_imu_frame_sidecar(session, refused_sidecar(refusal, now=NOW), replace=True)
    # A refusal is replaced freely.
    resolved(resolve_session_imu_frame(session, now=NOW))


def test_channels_from_long_reads_read_session_rows(tmp_path: Path) -> None:
    from src.preprocessing.loader import read_session

    truth = drive(MANOEUVRES[:2], mount_matrix=mount(0.0, 0.0, 0.0))
    session = tmp_path / "2026-09-26T120000"
    recorded = channels(truth)
    _write_session(session, recorded)
    back = channels_from_long(read_session(session))
    for column, series in recorded.items():
        np.testing.assert_allclose(back[column].to_numpy(), series.to_numpy(), rtol=1e-6)


# ---------------------------------------------------------------------------
# H01.1: neither family can become a detector feature
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    [
        "imu:SF_LON",
        "imu:SF_LAT",
        "imu:SF_VERT",
        "imu:ACCEL_LON",
        "imu:ACCEL_LAT",
        "imu:ACCEL_LON_MAX",
        "imu:ACCEL_LAT_MAX",
        "imu:SF_LON__mean",
    ],
)
def test_frame_channels_never_train(column: str) -> None:
    reason = source_exclusion_reason(column)
    assert reason is not None and reason.startswith(SOURCE_NOT_TRAINABLE)


def test_cli_resolves_records_refusals_and_guards_the_frame(tmp_path: Path) -> None:
    import scripts.resolve_imu_frame as cli

    truth = drive(MANOEUVRES, mount_matrix=mount(5.0, -3.0, 110.0), noise_g=0.004)
    good = tmp_path / "2026-09-26T120000"
    _write_session(good, channels(truth, speed=("gps:speed",)))
    bad = tmp_path / "_capture-tuning-2026-09-26T130000"
    _write_session(bad, channels(truth, heading=False))

    assert cli.main([str(good), str(bad)]) == 0
    good_sidecar = read_imu_frame_sidecar(good)
    bad_sidecar = read_imu_frame_sidecar(bad)
    assert good_sidecar is not None and good_sidecar.status == "resolved"
    assert bad_sidecar is not None and bad_sidecar.refusal is not None
    assert bad_sidecar.refusal.kind == "no_heading"

    assert cli.main([str(good), "--quiescent-seconds", "4"]) == 2  # frame of record
    assert cli.main([str(good), "--quiescent-seconds", "4", "--replace"]) == 0
    assert cli.main([str(tmp_path / "missing")]) == 1
