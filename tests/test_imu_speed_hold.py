"""The IMU frame holds a change-only broadcast speed (P02.10).

Change-only CAN storage keeps a frame only when its payload changes, and
re-writes an unchanged id ``timeout - 1`` s after its last stored frame. A
speed frame with no rolling counter (the Xterra's ``0x2D1`` has one; another
vehicle's may not) is then stored once per re-write on a steady leg. The IMU
frame used to age every input out after ``INPUT_MAX_AGE_SECONDS`` (3 s), so
such a leg had no quiescent window: no gravity reference at rest or cruise.

The rule (``imu_frame.input_max_age_seconds``): a broadcast speed stored
change-only stands for its value until the stale timeout, never longer, as
the resampler's broadcast class does. Offline it follows the session's
recorded storage (the chunks' ``canary.can_storage`` footer), live the
filter the live source runs. Storage that keeps every frame is untouched.

The drive is ``test_imu_stream``'s, with ``0x2D1`` the only speed: raw frames
at 10 Hz, bytes 1-2 little-endian counts and every other byte zero, so no
counter.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import CanaryConfig
from src.decode.bindings import Bindings, load_bindings
from src.features.live import LiveFeatureSource
from src.preprocessing.imu_frame import INPUT_MAX_AGE_SECONDS, input_max_age_seconds
from src.preprocessing.imu_live import LiveImuFrame
from src.preprocessing.imu_session import (
    IMU_FRAME_FILENAME,
    ImuSessionFrame,
    read_imu_frame_sidecar,
    read_imu_inputs,
    resolve_imu_frame,
    resolve_session_imu_frame,
)
from src.preprocessing.imu_stream import iter_framed_numeric_parts, plan_imu_frame
from src.storage.broadcast_storage import CanChangeFilter, CanStorage, session_can_storage
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import example_config
from tests.test_imu_stream import DRIVE, MOUNT, NOW, T0, records

TIMEOUT_S = 10.0
SPEED = "decoded:VEHICLE_SPEED_2D1"
LON = "imu:ACCEL_LON"
NAME = "2026-09-26T120000"
IMU_HZ = 50


@pytest.fixture(scope="module")
def bindings() -> Bindings:
    return load_bindings(Path("data/bindings.yaml"))


def _drive_records(*, speed_gap: tuple[float, float] | None = None) -> list[TelemetryRecord]:
    """The drive with ``0x2D1`` the only speed; ``speed_gap`` (seconds from
    T0) is a stretch with no ``0x2D1`` frame at all: the module went quiet."""
    out = records(speeds=("2d1",))
    if speed_gap is None:
        return out
    lo, hi = (T0 + timedelta(seconds=s) for s in speed_gap)
    return [r for r in out if not (r.source == "can" and lo <= r.timestamp < hi)]


def _session(root: Path, storage: CanStorage | None, **kwargs: object) -> Path:
    session = root / NAME
    write_records_to_parquet(
        _drive_records(**kwargs),  # type: ignore[arg-type]
        session,
        max_rows=400,
        can_storage=storage,
    )
    return session


def _resolved(result: object) -> ImuSessionFrame:
    assert isinstance(result, ImuSessionFrame), result
    return result


def _quiescent_samples(frame: ImuSessionFrame) -> int:
    assert frame.sidecar.specific_force is not None
    return frame.sidecar.specific_force.pitch_reference.quiescent_samples


def _speed_rows(session: Path) -> int:
    return len(read_imu_inputs(session)[SPEED])


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_only_a_change_only_broadcast_input_is_held() -> None:
    assert input_max_age_seconds(SPEED, None) == INPUT_MAX_AGE_SECONDS
    assert input_max_age_seconds(SPEED, TIMEOUT_S) == TIMEOUT_S
    assert input_max_age_seconds("can:0x2D1", TIMEOUT_S) == TIMEOUT_S
    # Never shorter than the every-frame age, at the 2 s floor.
    assert input_max_age_seconds(SPEED, 2.0) == INPUT_MAX_AGE_SECONDS
    for polled in ("obd:SPEED", "gps:speed", "gps:heading"):
        assert input_max_age_seconds(polled, TIMEOUT_S) == INPUT_MAX_AGE_SECONDS
    with pytest.raises(ValueError, match="P02.10"):
        input_max_age_seconds(SPEED, 1.0)


# ---------------------------------------------------------------------------
# Offline: the session resolver and its part-by-part rebuild
# ---------------------------------------------------------------------------


def test_a_counterless_speed_stored_change_only_keeps_its_quiescent_windows(
    tmp_path: Path,
) -> None:
    """The same drive stored every frame and change-only: the change-only
    session holds ``0x2D1`` for its recorded timeout, so its frame is the
    every-frame one exactly (same quiescent windows, same reference, same
    ``ACCEL_LON/LAT``), and the part-by-part rebuild reads the age from the
    sidecar. Held for only 3 s, the three steady legs lose their quiescent
    windows (1,708 samples -> 6, the edge of a cruise)."""
    every = _session(tmp_path / "every", None)
    change = _session(tmp_path / "change", CanStorage.change_only(TIMEOUT_S))
    assert session_can_storage(change).stale_timeout_seconds == TIMEOUT_S
    assert _speed_rows(change) < _speed_rows(every) / 2  # the steady legs dropped out

    full = _resolved(resolve_session_imu_frame(every, now=NOW))
    held = _resolved(resolve_session_imu_frame(change, now=NOW))
    assert full.sidecar.inputs is not None and held.sidecar.inputs is not None
    assert held.sidecar.inputs.speed_source == SPEED
    assert held.sidecar.inputs.speed_max_age_seconds == TIMEOUT_S
    assert full.sidecar.inputs.speed_max_age_seconds is None
    assert _quiescent_samples(held) == _quiescent_samples(full) > 0
    assert full.sidecar.specific_force is not None and held.sidecar.specific_force is not None
    assert held.sidecar.specific_force.reference_id == full.sidecar.specific_force.reference_id
    pd.testing.assert_frame_equal(held.frame, full.frame, check_exact=True)

    # The rebuild on the value_number chain holds it the same way.
    parts = list(iter_framed_numeric_parts(change, target_rows=150, align_ns=10**9))
    rows = pd.concat(parts, ignore_index=True)
    have = rows[rows["source_channel"] == LON].sort_values("timestamp", kind="stable")
    np.testing.assert_array_equal(have["value_number"].to_numpy(), full.frame[LON].to_numpy())

    # Without the hold: the 3 s age the resolver had before.
    unheld = _resolved(
        resolve_imu_frame(read_imu_inputs(change), change_only_timeout_seconds=None, now=NOW)
    )
    assert _quiescent_samples(full) == 1_708
    assert _quiescent_samples(unheld) == 6
    assert unheld.sidecar.specific_force is not None
    assert len(unheld.sidecar.specific_force.pitch_reference.windows) == 1


def test_every_frame_storage_is_unchanged(tmp_path: Path) -> None:
    """A session that stores every frame (no footer, or the every-frame
    profile's) gets the 3 s age, records no speed age, and keys its stored
    frames as a sidecar written before the field existed. A 6 s ``0x2D1``
    dropout at rest still ages out after 3 s there: the hold is the
    change-only storage's, not a longer age for everyone."""
    gap = (3.0, 9.0)
    old = _session(tmp_path / "old", None, speed_gap=gap)
    profile = _session(tmp_path / "profile", CanStorage.every_frame(), speed_gap=gap)
    assert session_can_storage(profile).stale_timeout_seconds is None

    for session in (old, profile):
        frame = _resolved(resolve_session_imu_frame(session, now=NOW))
        assert frame.sidecar.inputs is not None
        assert frame.sidecar.inputs.speed_max_age_seconds is None
        assert frame.sidecar.inputs.speed_max_age == INPUT_MAX_AGE_SECONDS
        as_before = resolve_imu_frame(
            read_imu_inputs(session), change_only_timeout_seconds=None, now=NOW
        )
        pd.testing.assert_frame_equal(_resolved(as_before).frame, frame.frame, check_exact=True)
        # Samples 3 s or more into the dropout steer by nothing.
        assert frame.sidecar.specific_force is not None
        windows = frame.sidecar.specific_force.pitch_reference.windows
        aged = (
            T0 + timedelta(seconds=gap[0] + INPUT_MAX_AGE_SECONDS),
            T0 + timedelta(seconds=gap[1]),
        )
        for window in windows:
            start, end = pd.Timestamp(window.start), pd.Timestamp(window.end)
            assert end < pd.Timestamp(aged[0]) or start >= pd.Timestamp(aged[1])

        # The stream's key is the pre-field key: no stored frame goes stale.
        plan = plan_imu_frame(session)
        raw = json.loads((session / IMU_FRAME_FILENAME).read_text())
        del raw["inputs"]["speed_max_age_seconds"]
        (session / IMU_FRAME_FILENAME).write_text(json.dumps(raw))
        sidecar = read_imu_frame_sidecar(session)
        assert sidecar is not None and sidecar.inputs is not None
        assert sidecar.inputs.speed_max_age_seconds is None
        assert plan_imu_frame(session).key() == plan.key()


def test_a_real_gap_longer_than_the_timeout_still_ages_out(tmp_path: Path) -> None:
    """``0x2D1`` stops for 20 s in the opening rest leg (the IMU carries on).
    Its last stored row is held for the timeout and no longer: no quiescent
    window reaches past that row plus 10 s before the frames resume. Stored
    every frame, the same dropout ages out 3 s after the last frame."""
    gap = (2.0, 22.0)
    segments = DRIVE.copy()
    segments[0] = type(DRIVE[0])(25.0, 0.0, 0.0)  # a longer opening rest
    change = tmp_path / "change" / NAME
    stream = records(segments, speeds=("2d1",))
    lo, hi = (T0 + timedelta(seconds=s) for s in gap)
    kept = [r for r in stream if not (r.source == "can" and lo <= r.timestamp < hi)]
    write_records_to_parquet(
        kept, change, max_rows=400, can_storage=CanStorage.change_only(TIMEOUT_S)
    )
    speed = read_imu_inputs(change)[SPEED]
    last = pd.Timestamp(speed.index[speed.index < pd.Timestamp(lo)][-1])
    assert last < pd.Timestamp(lo)
    resumed = pd.Timestamp(speed.index[speed.index >= pd.Timestamp(lo)][0])
    assert resumed >= pd.Timestamp(hi)

    frame = _resolved(resolve_session_imu_frame(change, now=NOW))
    assert frame.sidecar.specific_force is not None
    windows = frame.sidecar.specific_force.pitch_reference.windows
    expired = last + pd.Timedelta(seconds=TIMEOUT_S)
    for window in windows:
        start, end = pd.Timestamp(window.start), pd.Timestamp(window.end)
        assert end <= expired or start >= resumed, (start, end, expired, resumed)
    # The hold did reach into the gap: the rest before it stayed quiescent
    # past the 3 s an every-frame age would allow.
    first = windows[0]
    assert pd.Timestamp(first.end) > last + pd.Timedelta(seconds=INPUT_MAX_AGE_SECONDS)


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


def _filtered_speed_stream(
    seconds: float, *, kph: float = 0.0, stop_at: float | None = None
) -> list[tuple[float, float]]:
    """``(t, counts)`` of a counterless 100 Hz ``0x2D1`` at a constant speed,
    through the storage writer's own change filter: what the live source
    forwards to the IMU frame. ``stop_at``: the module goes quiet."""
    keep = CanChangeFilter(TIMEOUT_S)
    payload = b"\x00" + round(kph * 10.0).to_bytes(2, "little") + bytes(5)
    start = T0.timestamp()
    out = []
    for i in range(round(seconds * 100)):
        t = i / 100.0
        if stop_at is not None and t >= stop_at:
            break
        if keep.keep("0x2D1", payload, start + t):
            out.append((t, kph * 10.0))
    return out


def _feed(
    frame: LiveImuFrame,
    speed: list[tuple[float, float]],
    seconds: float,
    *,
    start: float = 0.0,
    up: np.ndarray = MOUNT[:, 2],
) -> None:
    """IMU at 50 Hz with the vehicle at rest (``up`` in the body frame), and
    the speed readings, both in time order, in ``[start, seconds)``."""
    readings = [(t, v) for t, v in speed if start <= t < seconds]
    k = 0
    for i in range(round(start * IMU_HZ), round(seconds * IMU_HZ)):
        t = i / IMU_HZ
        while k < len(readings) and readings[k][0] <= t:
            frame.observe_input(SPEED, readings[k][1], T0 + timedelta(seconds=readings[k][0]))
            k += 1
        stamp = T0 + timedelta(seconds=t)
        for axis, value in zip(("ACCEL_X", "ACCEL_Y", "ACCEL_Z"), up, strict=True):
            frame.observe_imu(axis, float(value), stamp)


def test_live_holds_a_counterless_change_only_speed(bindings: Bindings) -> None:
    """20 s at rest: the filtered stream carries the first frame and two
    re-writes. Held for the timeout, the frame finds its gravity reference;
    with no hold (every reading assumed to arrive) it never does."""
    speed = _filtered_speed_stream(20.0)
    assert [t for t, _ in speed] == [0.0, 9.0, 18.0]
    held = LiveImuFrame(bindings=bindings, broadcast_stale_timeout_s=TIMEOUT_S)
    _feed(held, speed, 20.0)
    assert held.status == "yaw_unresolved"  # gravity formed; no heading, no yaw
    assert held._gravity is not None
    np.testing.assert_allclose(held._gravity, MOUNT[:, 2], atol=1e-9)

    unheld = LiveImuFrame(bindings=bindings)
    _feed(unheld, speed, 20.0)
    assert unheld.status == "no_quiescent_period"


def test_live_holds_no_longer_than_the_timeout(bindings: Bindings) -> None:
    """The module stops after 12 s at rest (last stored frame at 9 s). The
    IMU then sees a different "up" from 9 + 10 s on: had the held speed
    outlived the timeout those samples would pull gravity toward it."""
    speed = _filtered_speed_stream(40.0, stop_at=12.0)
    assert [t for t, _ in speed] == [0.0, 9.0]
    frame = LiveImuFrame(bindings=bindings, broadcast_stale_timeout_s=TIMEOUT_S)
    expired = 9.0 + TIMEOUT_S
    _feed(frame, speed, expired + 0.02)
    tilted = MOUNT @ np.array([0.25, 0.0, 1.0])
    _feed(frame, speed, 40.0, start=expired + 0.02, up=tilted)
    assert frame._gravity is not None
    np.testing.assert_allclose(frame._gravity, MOUNT[:, 2], atol=1e-9)


def _dense_drive(frame: LiveImuFrame) -> list[tuple[dict[str, float], datetime | None]]:
    """``test_imu_live``'s drive with a 10 Hz decoded speed carrying a count of
    jitter (never more than 0.1 s between readings), ticked every second."""
    from tests.test_imu_live import _profile

    ticks = []
    for i in range(30 * IMU_HZ):
        t = i / IMU_HZ
        stamp = T0 + timedelta(seconds=t)
        kph, heading, a_g, lat_g = _profile(t)
        if i % 5 == 0:
            frame.observe_input(SPEED, round(kph * 10.0) + (i // 5) % 2, stamp)
        if i % IMU_HZ == 0:
            frame.observe_input("gps:heading", heading % 360.0, stamp)
        body = MOUNT @ np.array([a_g, lat_g, 1.0])
        for axis, value in zip(("ACCEL_X", "ACCEL_Y", "ACCEL_Z"), body, strict=True):
            frame.observe_imu(axis, float(value), stamp)
        if i % IMU_HZ == IMU_HZ - 1:
            ticks.append(frame.take_tick())
    return ticks


def test_live_dense_speed_is_unchanged_by_the_hold(bindings: Bindings) -> None:
    """A speed whose readings come closer together than the re-emission period
    (every frame changing, or stored every frame) is never re-emitted, and
    nothing ages differently inside 3 s: every tick is identical with and
    without the hold."""
    held = LiveImuFrame(bindings=bindings, broadcast_stale_timeout_s=TIMEOUT_S)
    plain = LiveImuFrame(bindings=bindings)
    assert _dense_drive(held) == _dense_drive(plain)
    assert held.status == plain.status
    assert all(entry[3] for entry in held._speeds[SPEED].history)  # nothing re-emitted


def test_the_live_source_holds_with_its_own_filter_timeout(bindings: Bindings) -> None:
    """End to end: raw counterless ``0x2D1`` frames at 100 Hz into
    ``LiveFeatureSource``, whose change filter keeps three of 2,000; its IMU
    frame holds them for the configured timeout and finds gravity at rest."""
    config: CanaryConfig = example_config(Path("canary.example.yaml"))
    source = LiveFeatureSource(config=config, bindings=bindings)
    timeout = config.collection.broadcast_stale_timeout_seconds
    assert source.imu_frame.broadcast_stale_timeout_s == timeout
    payload = b"\x00" + bytes(2) + bytes(5)
    for i in range(20 * 100):
        t = T0 + timedelta(seconds=i / 100.0)
        source.observe(TelemetryRecord.can(t.timestamp(), 0x2D1, payload))
        if i % 2 == 0:
            for axis, value in zip(("ACCEL_X", "ACCEL_Y", "ACCEL_Z"), MOUNT[:, 2], strict=True):
                source.observe(TelemetryRecord.imu(t.timestamp(), axis, float(value), "g"))
    assert source._can_filter.kept == 3
    assert source.imu_frame.status == "yaw_unresolved"
