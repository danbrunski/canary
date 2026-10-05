"""H04.4: no committee names a channel nothing delivers.

Found 2026-10-03: the longitudinal-acceleration committee named
``imu:ACCEL_LON``, which the IMU decimation drops before resampling (offline)
and the live frame never emits, so a relation configured on it would have
compared nothing, silently. Every committee channel now has a declared
producer (``committees.CHANNEL_PRODUCERS``), checked at import and by the
evaluator; a ``pending`` one cannot be configured in ``committees.yaml``. The
tests below hold each *delivered* claim to the code that delivers it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from src.collection.gps_collector import reading_to_records
from src.collection.gps_nmea import GpsReading
from src.collection.imu_collector import ACCEL_CHANNELS, GYRO_CHANNELS
from src.collection.pid_decoders import PID_DECODERS
from src.decode.bindings import load_bindings
from src.preprocessing.imu_decimation import IMU_TICK_COLUMNS
from src.preprocessing.imu_live import LiveImuFrame
from src.signals.committees import (
    ALTITUDE_COMMITTEE,
    CHANNEL_PRODUCERS,
    COMMITTEES,
    IMU_ACCEL_LON_MEAN,
    LONGITUDINAL_ACCEL_COMMITTEE,
    MEMBER_TRANSFORMS,
    ChannelProducer,
    CommitteeDeclarationError,
    CommitteeEvaluator,
    CommitteeProfile,
    SensorCommittee,
    check_committee_producers,
)
from src.trainability import source_exclusion_reason

REPO = Path(__file__).resolve().parents[1]


def _committee_channels() -> set[str]:
    return {
        channel
        for committee in COMMITTEES
        for channel in (*committee.members, *(g.channel for g in committee.gates))
    }


def test_every_declared_committee_channel_has_a_producer() -> None:
    check_committee_producers()  # also ran at import
    assert _committee_channels() <= set(CHANNEL_PRODUCERS)


def test_the_imu_member_is_the_signed_tick_channel_not_the_dropped_raw_one() -> None:
    assert IMU_ACCEL_LON_MEAN == "imu:ACCEL_LON_MEAN"
    assert IMU_ACCEL_LON_MEAN in LONGITUDINAL_ACCEL_COMMITTEE.members
    assert "imu:ACCEL_LON" not in _committee_channels()
    assert CHANNEL_PRODUCERS[IMU_ACCEL_LON_MEAN].kind == "imu_tick"
    # H01.1: the new channel is an external sensor's, never a feature.
    assert source_exclusion_reason(IMU_ACCEL_LON_MEAN) is not None


def test_a_committee_naming_an_undeclared_channel_is_refused_by_name() -> None:
    bad = SensorCommittee(
        name="LON_ACCEL_SOURCE_DIVERGENCE",
        quantity="longitudinal acceleration (g)",
        members=("imu:ACCEL_LON", "derived:GPS_SPEED_ACCEL_G"),
    )
    with pytest.raises(CommitteeDeclarationError, match=r"reads imu:ACCEL_LON, which has no"):
        check_committee_producers([bad])
    # The evaluator — what loads a profile, live and offline — refuses it too.
    with pytest.raises(CommitteeDeclarationError, match="imu:ACCEL_LON"):
        CommitteeEvaluator(None, committees=[bad])


def test_a_gate_without_a_producer_is_refused() -> None:
    from src.signals.committees import MemberGate

    gated = SensorCommittee(
        name="X",
        quantity="speed",
        members=("obd:SPEED", "gps:speed"),
        gates=(MemberGate("gps:pdop", max_value=2.0),),
    )
    with pytest.raises(CommitteeDeclarationError, match="X reads gps:pdop"):
        check_committee_producers([gated])


def test_a_member_transform_claim_must_be_computed_here() -> None:
    committee = SensorCommittee(
        name="X", quantity="g", members=("derived:NOT_COMPUTED", "gps:speed")
    )
    producers = {
        **CHANNEL_PRODUCERS,
        "derived:NOT_COMPUTED": ChannelProducer("member_transform", "nobody"),
    }
    with pytest.raises(CommitteeDeclarationError, match="MEMBER_TRANSFORMS does not compute"):
        check_committee_producers([committee], producers)


def _profile(name: str, reference: str, member: str, conditions: list[dict[str, Any]]) -> Any:
    return {
        "schema_version": 1,
        "committees": {
            name: {
                "reference": reference,
                "relations": [{"member": member, "ratio": 1.0, "evidence": "test"}],
                "tolerance": {
                    "kind": "absolute",
                    "max_departure": 1.0,
                    "sustain_seconds": 10,
                    "min_verdicts": 5,
                    "max_verdict_gap_seconds": 3,
                    "conditions": conditions,
                    "evidence": "test",
                },
            }
        },
    }


def test_a_profile_cannot_configure_a_relation_on_a_pending_member() -> None:
    """``baro:altitude`` is declared (the committee loads) but nothing delivers
    it, so a relation on it would compare nothing: refused, naming it."""
    data = _profile(ALTITUDE_COMMITTEE.name, "gps:altitude", "baro:altitude", [])
    with pytest.raises(ValueError, match=r"baro:altitude is not delivered by anything yet"):
        CommitteeProfile.model_validate(data)


def test_a_profile_cannot_condition_on_an_undelivered_channel() -> None:
    conditions = [{"channel": "derived:GPS_COURSE_RATE", "max_abs": 1.0, "reason": "straight"}]
    data = _profile(
        LONGITUDINAL_ACCEL_COMMITTEE.name,
        "derived:GPS_SPEED_ACCEL_G",
        IMU_ACCEL_LON_MEAN,
        conditions,
    )
    with pytest.raises(ValueError, match="derived:GPS_COURSE_RATE is not delivered"):
        CommitteeProfile.model_validate(data)


def test_the_vehicle_profile_configures_only_delivered_channels() -> None:
    from src.signals.committees import load_committee_profile

    load_committee_profile(REPO / "data" / "nissan-xterra-2008" / "committees.yaml")


# ---------------------------------------------------------------------------
# Each delivered claim, held to its producer
# ---------------------------------------------------------------------------


def _gps_channels() -> set[str]:
    reading = GpsReading(
        has_fix=True,
        speed_kmh=50.0,
        heading_deg=90.0,
        latitude=40.0,
        longitude=-105.0,
        altitude_m=1600.0,
        fix_quality=1,
        num_satellites=10,
        hdop=0.8,
    )
    return {f"gps:{r.channel}" for r in reading_to_records(reading, 0.0)}


def _live_imu_channels() -> set[str]:
    """What LiveImuFrame emits over three stop-and-go cycles (the sign needs
    more than one pull's votes), with a known mount."""
    t0 = datetime(2026, 10, 5, tzinfo=UTC)
    frame = LiveImuFrame()
    emitted: set[str] = set()
    hz = 50

    def profile(t: float) -> tuple[float, float]:  # (kph, a_g)
        if t < 10.0:
            return 0.0, 0.0
        c = (t - 10.0) % 20.0
        if c < 4.0:
            return 50.0 * c / 4.0, 50.0 / 3.6 / 4.0 / 9.80665
        if c < 10.0:
            return 50.0, 0.0
        if c < 14.0:
            return 50.0 - 50.0 * (c - 10.0) / 4.0, -50.0 / 3.6 / 4.0 / 9.80665
        return 0.0, 0.0

    for i in range(70 * hz):
        t = i / hz
        at = t0 + timedelta(seconds=t)
        kph, a_g = profile(t)
        if i % hz == 0:
            frame.observe_input("gps:speed", kph, at)
            frame.observe_input("gps:heading", 90.0, at)
            values, _ = frame.take_tick()
            emitted |= set(values)
        for axis, value in zip(("ACCEL_X", "ACCEL_Y", "ACCEL_Z"), (a_g, 0.0, 1.0), strict=True):
            frame.observe_imu(axis, value, at)
    return emitted


def test_each_delivered_producer_claim_holds() -> None:
    pids = {name for name, _decoder in PID_DECODERS.values()}
    bindings = {s.name for s in load_bindings(REPO / "data" / "bindings.yaml").signals}
    gps = _gps_channels()
    imu_raw = {f"imu:{c}" for c in (*ACCEL_CHANNELS, *GYRO_CHANNELS)}
    live_imu = _live_imu_channels()
    checked = 0
    for channel, producer in CHANNEL_PRODUCERS.items():
        source, _, name = channel.partition(":")
        if producer.kind == "obd_pid":
            assert source == "obd" and name in pids, channel
        elif producer.kind == "can_binding":
            assert source == "decoded" and name in bindings, channel
        elif producer.kind == "gps_collector":
            assert channel in gps, channel
        elif producer.kind == "imu_collector":
            assert channel in imu_raw, channel
        elif producer.kind == "imu_tick":
            assert channel in IMU_TICK_COLUMNS, channel
            assert channel in live_imu, f"{channel}: LiveImuFrame never emitted it"
        elif producer.kind == "member_transform":
            assert channel in MEMBER_TRANSFORMS, channel
        else:
            assert producer.kind == "pending"
            continue
        checked += 1
    assert checked >= 10
    # The live frame emits every per-tick column the offline decimation does.
    assert set(IMU_TICK_COLUMNS) <= live_imu


def _lon_evaluator() -> CommitteeEvaluator:
    data = _profile(
        LONGITUDINAL_ACCEL_COMMITTEE.name, "derived:GPS_SPEED_ACCEL_G", IMU_ACCEL_LON_MEAN, []
    )
    return CommitteeEvaluator(CommitteeProfile.model_validate(data))


def _lon_entry(evaluator: CommitteeEvaluator) -> dict[str, Any]:
    entry: dict[str, Any] = evaluator.snapshot()["committees"][LONGITUDINAL_ACCEL_COMMITTEE.name]
    return entry


def test_a_missing_imu_member_names_which_kind_of_empty_live() -> None:
    """The live IMU frame's own step goes into the verdict and the snapshot
    (``/status``): *which* kind of empty, not a list of possible causes."""
    frame = LiveImuFrame()
    evaluator = _lon_evaluator()
    now = datetime(2026, 10, 5, tzinfo=UTC)
    evaluator.producer_status = {IMU_ACCEL_LON_MEAN: frame.lon_mean_absence(now)}
    evaluator.observe({"derived:GPS_SPEED_ACCEL_G": 0.2}, 1.0)
    entry = _lon_entry(evaluator)
    assert (entry["state"], entry["reason"]) == ("no_verdict", "member_missing")
    assert entry["detail"] == "imu:ACCEL_LON_MEAN has no value (live IMU frame no_imu_samples)"
    assert evaluator.snapshot()["producer_status"] == {
        IMU_ACCEL_LON_MEAN: "live IMU frame no_imu_samples"
    }
    # A NaN reads the same as absent, never as a 0 g agreement.
    evaluator.observe({"derived:GPS_SPEED_ACCEL_G": 0.2, IMU_ACCEL_LON_MEAN: np.nan}, 2.0)
    assert _lon_entry(evaluator)["reason"] == "member_missing"


def test_a_missing_imu_member_names_the_offline_frame_outcome() -> None:
    """Offline the resampled frame's ``attrs["imu_frame"]`` (what
    ``resample_and_segment`` records) is the status."""
    import pandas as pd

    from src.preprocessing.imu_stream import IMU_FRAME_ATTR
    from src.signals.committees import IMU_FRAME_ATTR as COMMITTEE_IMU_FRAME_ATTR
    from src.signals.committees import evaluate_committee_frame

    assert COMMITTEE_IMU_FRAME_ATTR == IMU_FRAME_ATTR
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-10-05", periods=3, freq="1s", tz="UTC"),
            "derived:GPS_SPEED_ACCEL_G": [0.1, 0.2, 0.1],
        }
    )
    frame.attrs[IMU_FRAME_ATTR] = {
        "2026-10-05T000000": {"status": "unresolved", "reason": "no imu_frame.json"}
    }
    profile = _lon_evaluator().profile
    evaluate_committee_frame(frame, profile)
    from src.signals.committees import offline_producer_status

    status = offline_producer_status(frame)
    assert status == {IMU_ACCEL_LON_MEAN: "offline IMU frame unresolved: no imu_frame.json"}
    evaluator = _lon_evaluator()
    evaluator.producer_status = status
    evaluator.observe({"derived:GPS_SPEED_ACCEL_G": 0.2}, 1.0)
    assert "offline IMU frame unresolved" in _lon_entry(evaluator)["detail"]


def test_a_missing_mode01_member_is_worded_neutrally() -> None:
    """A missing PID value does not say whether it was unpolled, passive, or
    an ECM that did not answer: the text says exactly that."""
    absent = CHANNEL_PRODUCERS["obd:SPEED"].absent
    assert "not polled" in absent and "passive" in absent and "ECM silent" in absent
    assert CHANNEL_PRODUCERS["obd:BAROMETRIC_PRESSURE"].absent == absent


def test_a_body_frame_gyro_cannot_be_configured_as_yaw_rate() -> None:
    """``imu:GYRO_Z`` is the board's Z rate; on a tilted mount it mixes in roll
    and pitch rate. Pending until a frame-rotated yaw-rate channel exists."""
    from src.signals.committees import HEADING_YAW_COMMITTEE

    assert CHANNEL_PRODUCERS["imu:GYRO_Z"].kind == "pending"
    data = _profile(HEADING_YAW_COMMITTEE.name, "derived:GPS_COURSE_RATE", "imu:GYRO_Z", [])
    with pytest.raises(ValueError, match="imu:GYRO_Z is not delivered by anything yet"):
        CommitteeProfile.model_validate(data)
