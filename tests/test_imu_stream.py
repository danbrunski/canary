"""The offline IMU frame on the ``value_number`` chain (H02.4, ``imu_stream``).

A synthetic drive with a mount we chose, written as a session with small
chunks so the part-by-part reader cuts it in many places. Its frame is
resolved once (``imu_frame.json``); every reader of the session then sees
``imu:ACCEL_LON/LAT`` rebuilt from that sidecar part by part, equal to the
whole-session frame. Axes are ISO 8855; the chip sees ``M @ (a, a_lat, 1)``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import CanaryConfig
from src.preprocessing.derived import grid_interval_ns, reduce_numeric_session
from src.preprocessing.derived_store import DerivedStore, use_store
from src.preprocessing.imu_session import (
    IMU_FRAME_FILENAME,
    ImuSessionFrame,
    read_imu_frame_sidecar,
    resolve_session_imu_frame,
    write_imu_frame_sidecar,
)
from src.preprocessing.imu_stream import (
    IMU_FRAME_ATTR,
    ImuFrameStaleError,
    LinearAccelStream,
    iter_framed_numeric_parts,
    plan_imu_frame,
)
from src.preprocessing.live import LiveFeatureSource
from src.preprocessing.loader import (
    iter_session_numeric_parts,
    resample_and_segment,
)
from src.preprocessing.resampler import resample_config_for
from src.signals.committees import STANDARD_GRAVITY
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import example_config

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
IMU_HZ = 50
NOW = datetime(2026, 9, 26, 13, 0, tzinfo=UTC)
LON, LAT = "imu:ACCEL_LON", "imu:ACCEL_LAT"
LON_MAX, LAT_MAX = "imu:ACCEL_LON_MAX", "imu:ACCEL_LAT_MAX"
ACCEL_MODES = ("launch", "hard_brake", "cornering")


def _rot(axis: str, deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    if axis == "x":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == "y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


MOUNT = _rot("z", 140.0) @ _rot("y", 6.0) @ _rot("x", -4.0)


@dataclass(frozen=True)
class Segment:
    seconds: float
    kph0: float
    kph1: float
    heading0: float = 90.0
    heading1: float = 90.0
    lat_g: float = 0.0
    imu: bool = True


# Stop, a 0.38 g pull from rest, on to highway, a 0.45 g brake to a stop, a
# pull, then a 0.35 g left turn at constant speed.
DRIVE = [
    Segment(15.0, 0.0, 0.0),
    Segment(4.5, 0.0, 60.0),
    Segment(4.0, 60.0, 100.0),
    Segment(10.0, 100.0, 100.0),
    Segment(6.3, 100.0, 0.0),
    Segment(10.0, 0.0, 0.0),
    Segment(5.0, 0.0, 40.0),
    Segment(8.0, 40.0, 40.0, heading0=90.0, heading1=0.0, lat_g=0.35),
    Segment(10.0, 40.0, 40.0, heading0=0.0, heading1=0.0),
]


def _windows(segments: list[Segment]) -> dict[str, tuple[float, float]]:
    """Seconds from T0 of the pull from rest, the brake and the turn."""
    t = 0.0
    out: dict[str, tuple[float, float]] = {}
    for seg in segments:
        if seg.kph0 == 0.0 and seg.kph1 > 50.0:
            out["launch"] = (t, t + seg.seconds)
        elif seg.kph1 == 0.0 and seg.kph0 > 0.0:
            out["hard_brake"] = (t, t + seg.seconds)
        elif seg.lat_g > 0.3:
            out["cornering"] = (t, t + seg.seconds)
        t += seg.seconds
    return out


def records(
    segments: list[Segment] = DRIVE,
    *,
    imu: bool = True,
    speeds: tuple[str, ...] = ("obd", "gps"),
) -> list[TelemetryRecord]:
    """IMU at 50 Hz (g); OBD speed/rpm/coolant/throttle and GPS speed/course
    at 1 Hz, so the live segmenter has every input it requires. ``speeds``
    picks the speed sources: ``obd`` (Mode 01), ``gps``, and ``2d1`` (raw
    ``0x2D1`` frames at 10 Hz, bytes 1-2 little-endian, 0.1 km/h a count)."""
    rng = np.random.default_rng(0)
    out: list[TelemetryRecord] = []
    t = 0.0
    for seg in segments:
        n = round(seg.seconds * IMU_HZ)
        a_g = (seg.kph1 - seg.kph0) / 3.6 / seg.seconds / STANDARD_GRAVITY
        for i in range(n):
            frac = i / n
            kph = seg.kph0 + (seg.kph1 - seg.kph0) * frac
            heading = (seg.heading0 + (seg.heading1 - seg.heading0) * frac) % 360.0
            stamp = (T0 + timedelta(seconds=t)).timestamp()
            if "2d1" in speeds and round(t * IMU_HZ) % (IMU_HZ // 10) == 0:
                counts = round(kph * 10.0).to_bytes(2, "little")
                out.append(TelemetryRecord.can(stamp, 0x2D1, b"\x00" + counts + bytes(5)))
            if round(t * IMU_HZ) % IMU_HZ == 0:
                if "obd" in speeds:
                    out.append(TelemetryRecord.obd(stamp, "SPEED", float(round(kph)), "km/h"))
                out.append(TelemetryRecord.obd(stamp, "RPM", 1500.0, "rpm"))
                out.append(TelemetryRecord.obd(stamp, "COOLANT_TEMP", 85.0, "C"))
                out.append(TelemetryRecord.obd(stamp, "THROTTLE_POS", 20.0, "%"))
                if "gps" in speeds:
                    out.append(TelemetryRecord.gps(stamp, "speed", kph, "km/h"))
                out.append(TelemetryRecord.gps(stamp, "heading", heading, "deg"))
            if imu and seg.imu:
                body = MOUNT @ np.array([a_g, seg.lat_g, 1.0]) + rng.normal(0.0, 0.004, 3)
                for axis, value in zip(("ACCEL_X", "ACCEL_Y", "ACCEL_Z"), body, strict=True):
                    out.append(TelemetryRecord.imu(stamp, axis, float(value), "g"))
            t += 1.0 / IMU_HZ
    return out


def write_session(
    root: Path,
    name: str = "2026-09-26T120000",
    *,
    segments: list[Segment] = DRIVE,
    imu: bool = True,
    resolve: bool = True,
    speeds: tuple[str, ...] = ("obd", "gps"),
) -> Path:
    session = root / name
    write_records_to_parquet(records(segments, imu=imu, speeds=speeds), session, max_rows=400)
    if resolve:
        assert isinstance(resolve_session_imu_frame(session, now=NOW), ImuSessionFrame)
    return session


@pytest.fixture
def config() -> CanaryConfig:
    """The example config with the accel-gated modes placed ahead of the speed
    bands (the example lists them after ``city``/``highway``, which claim every
    moving row first)."""
    data = example_config(Path("canary.example.yaml")).model_dump()
    data["preprocessing"]["operating_modes"] = {
        "launch": {"speed_max": 30, "accel_lon_min": 0.3},
        "hard_brake": {"speed_min": 1, "accel_lon_min": 0.4},
        "cornering": {"accel_lat_min": 0.3},
        "stopped": {"speed_max": 0},
        "city": {"speed_min": 1, "speed_max": 80},
        "highway": {"speed_min": 80},
    }
    return CanaryConfig.model_validate(data)


def _stream_rows(session: Path, target_rows: int) -> tuple[list[pd.DataFrame], pd.DataFrame]:
    parts = list(iter_framed_numeric_parts(session, target_rows=target_rows, align_ns=10**9))
    return parts, pd.concat(parts, ignore_index=True)


# ---------------------------------------------------------------------------
# Part-by-part equality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target_rows", [150, 1_000, 10**9])
def test_stream_equals_the_whole_session_frame(tmp_path: Path, target_rows: int) -> None:
    """ACCEL_LON/LAT rebuilt part by part from the sidecar are the whole-session
    resolution's, bit for bit, however the session is cut, and every other
    row passes through once, whole ticks at a time."""
    session = write_session(tmp_path)
    reference = resolve_session_imu_frame(session, write=False, now=NOW)
    assert isinstance(reference, ImuSessionFrame)
    expected = reference.rows(session.name)
    expected = expected[expected["source_channel"].isin([LON, LAT])]

    parts, rows = _stream_rows(session, target_rows)
    if target_rows == 150:
        assert len(parts) > 10  # many part edges, several inside manoeuvres
    got = rows[rows["source_channel"].isin([LON, LAT])]
    for column in (LON, LAT):
        want = expected[expected["source_channel"] == column]
        have = got[got["source_channel"] == column].sort_values("timestamp", kind="stable")
        assert len(have) == len(want) == 3_640
        np.testing.assert_array_equal(
            pd.DatetimeIndex(have["timestamp"]).as_unit("ns"),
            pd.DatetimeIndex(want["timestamp"]).as_unit("ns"),
        )
        np.testing.assert_array_equal(have["value_number"].to_numpy(), want["value_number"])

    # Every other row exactly once.
    raw = pd.concat(
        iter_session_numeric_parts(session, target_rows=target_rows, align_ns=10**9),
        ignore_index=True,
    )
    others = rows[~rows["source_channel"].isin([LON, LAT])]
    key = ["timestamp", "source_channel", "value_number"]
    pd.testing.assert_frame_equal(
        others.sort_values(key).reset_index(drop=True)[key],
        raw.sort_values(key).reset_index(drop=True)[key],
        check_exact=True,
    )
    # No tick is split between two emitted parts.
    ticks = [set(pd.DatetimeIndex(p["timestamp"]).floor("1s")) for p in parts]
    for i, a in enumerate(ticks):
        for b in ticks[i + 1 :]:
            assert not a & b


def test_an_imu_dropout_holds_only_the_look_ahead(tmp_path: Path) -> None:
    """The IMU goes silent for 40 s while OBD/GPS carry on. The rows still
    waiting for their look-ahead are held, not the whole dropout, and the
    frame still equals the whole-session one."""
    segments = [*DRIVE[:4], Segment(40.0, 100.0, 100.0, imu=False), *DRIVE[4:]]
    session = write_session(tmp_path, segments=segments)
    reference = resolve_session_imu_frame(session, write=False, now=NOW)
    assert isinstance(reference, ImuSessionFrame)

    stream = LinearAccelStream(plan_imu_frame(session), align_ns=10**9)
    emitted: list[pd.DataFrame] = []
    for part in iter_session_numeric_parts(session, target_rows=150, align_ns=10**9):
        emitted.extend(stream.feed(part))
    emitted.extend(stream.finish())
    rows = pd.concat(emitted, ignore_index=True)
    have = rows[rows["source_channel"] == LON].sort_values("timestamp", kind="stable")
    np.testing.assert_array_equal(have["value_number"].to_numpy(), reference.frame[LON].to_numpy())
    # ~1.5 s of look-ahead plus the ticks they touch, never the 40 s dropout
    # (40 s of OBD + GPS alone is 240 rows).
    lookahead_rows = round(1.5 * IMU_HZ) * 3
    assert stream.peak_held_rows <= lookahead_rows + 4 * (3 * IMU_HZ + 6)


@pytest.mark.parametrize("speeds", [("2d1",), ("2d1", "obd", "gps")])
def test_0x2d1_speed_is_decoded_from_the_stored_frames(
    tmp_path: Path, config: CanaryConfig, speeds: tuple[str, ...]
) -> None:
    """``VEHICLE_SPEED_2D1`` is a ``high`` binding, outside the loader's
    on-read set, so a decode on read alone never yields it. Both IMU readers
    decode it from the stored raw frames: the resolver steers by it (first in
    ``SPEED_SOURCES``, and here the only speed: no Mode 01, no GPS speed) and
    records it, and the part-by-part stream reads the same rows and drops them
    after, so the frame gains nothing but the IMU channels."""
    from src.decode.on_read import on_read_catalog

    session = write_session(tmp_path, speeds=speeds)
    sidecar = read_imu_frame_sidecar(session)
    assert sidecar is not None and sidecar.inputs is not None
    assert sidecar.inputs.speed_source == "decoded:VEHICLE_SPEED_2D1"

    reference = resolve_session_imu_frame(session, write=False, now=NOW)
    assert isinstance(reference, ImuSessionFrame)
    _, rows = _stream_rows(session, 150)
    have = rows[rows["source_channel"] == LON].sort_values("timestamp", kind="stable")
    np.testing.assert_array_equal(have["value_number"].to_numpy(), reference.frame[LON].to_numpy())

    frame = resample_and_segment([session], config)
    assert frame.attrs[IMU_FRAME_ATTR][session.name]["status"] == "applied"
    assert "speed decoded:VEHICLE_SPEED_2D1" in frame.attrs[IMU_FRAME_ATTR][session.name]["reason"]
    assert LON_MAX in frame.columns
    catalog = on_read_catalog()
    on_read = {signal.name for signal in catalog.signals} if catalog is not None else set()
    if "VEHICLE_SPEED_2D1" not in on_read:
        assert "decoded:VEHICLE_SPEED_2D1" not in frame.columns


# ---------------------------------------------------------------------------
# Readers: resample_and_segment, the store, autotag
# ---------------------------------------------------------------------------


def _seconds(frame: pd.DataFrame, mode: str) -> set[float]:
    rows = frame[frame["operating_mode"] == mode]
    return {(t - pd.Timestamp(T0)).total_seconds() for t in rows["timestamp"]}


def test_resample_and_segment_sees_the_imu_and_accel_modes_fire(
    tmp_path: Path, config: CanaryConfig
) -> None:
    session = write_session(tmp_path)
    frame = resample_and_segment([session], config)
    assert {LON_MAX, LAT_MAX, "imu:ACCEL_LON_RMS", "imu:JERK_MAX"} <= set(frame.columns)
    assert LON not in frame.columns and LAT not in frame.columns
    use = frame.attrs[IMU_FRAME_ATTR][session.name]
    assert use["status"] == "applied" and use["reference_id"]

    # |LON| RMS over ticks wholly inside each manoeuvre is its acceleration
    # (the MAX adds the 4 mg noise's peaks).
    rms = frame.set_index("timestamp")["imu:ACCEL_LON_RMS"]
    windows = _windows(DRIVE)
    for mode, g in (
        ("launch", 60.0 / 3.6 / 4.5 / STANDARD_GRAVITY),
        ("hard_brake", 100.0 / 3.6 / 6.3 / STANDARD_GRAVITY),
    ):
        start, stop = windows[mode]
        inside = rms[
            (rms.index >= T0 + timedelta(seconds=math.ceil(start)))
            & (rms.index < T0 + timedelta(seconds=math.floor(stop) - 1))
        ]
        assert len(inside) >= 3
        np.testing.assert_allclose(inside.to_numpy(), g, atol=0.003)
    assert frame[LON_MAX].max() < 0.48

    dwell = config.preprocessing.mode_dwell_ticks
    for mode in ACCEL_MODES:
        seen = _seconds(frame, mode)
        start, stop = windows[mode]
        assert len(seen) >= 3, mode
        # Inside the manoeuvre, held at most the dwell past it.
        assert all(start - 1.0 <= s <= stop + dwell for s in seen), (mode, sorted(seen))


def test_store_and_no_store_give_the_same_frame(tmp_path: Path, config: CanaryConfig) -> None:
    session = write_session(tmp_path)
    bare = resample_and_segment([session], config)
    resample = resample_config_for(config)
    store = DerivedStore(tmp_path / "derived")
    with use_store(tmp_path / "derived"):
        built = resample_and_segment([session], config)
        _, report = store.load_numeric(session, resample)
        assert report.status == "reused"
        reused = resample_and_segment([session], config)
    pd.testing.assert_frame_equal(built, bare, check_exact=True)
    pd.testing.assert_frame_equal(reused, bare, check_exact=True)
    assert built.attrs[IMU_FRAME_ATTR] == bare.attrs[IMU_FRAME_ATTR]

    # Removing the sidecar is a change of input: the stored frame rebuilds,
    # naming it, and loses the IMU channels.
    (session / IMU_FRAME_FILENAME).unlink()
    reduced, report = store.load_numeric(session, resample)
    assert report.status == "built"
    assert report.reason == "the session's IMU frame (imu_frame.json) changed"
    assert LON_MAX not in reduced.channels


def test_the_value_number_reduction_is_the_same_with_one_part_or_many(tmp_path: Path) -> None:
    session = write_session(tmp_path)
    resample = resample_config_for(example_config(Path("canary.example.yaml")))
    grid_interval_ns(resample)
    one = reduce_numeric_session(session, resample, part_rows=10**9)
    many = reduce_numeric_session(session, resample, part_rows=150)
    assert LON_MAX in one.channels
    # Per-part reductions keep each part's bin extremes; the cells the
    # resampler reads (and so the frame) are the same.
    from src.preprocessing.derived import resample_numeric_reduced

    pd.testing.assert_frame_equal(
        resample_numeric_reduced(one, resample),
        resample_numeric_reduced(many, resample),
        check_exact=True,
    )


def test_autotag_tags_hard_accel_hard_brake_and_cornering_offline(
    tmp_path: Path, config: CanaryConfig, caplog: pytest.LogCaptureFixture
) -> None:
    import scripts.autotag as autotag

    session = write_session(tmp_path)
    assert autotag.autotag_session(session, config) > 0
    labels = [
        json.loads(line)["label"] for line in (session / "tags.jsonl").read_text().splitlines()
    ]
    for label in ("hard_accel", "hard_brake", "cornering"):
        assert label in labels, labels

    # The same drive, never resolved: no IMU tags, and a warning naming why.
    unresolved = write_session(tmp_path, "2026-09-26T130000", resolve=False)
    with caplog.at_level("WARNING"):
        autotag.autotag_session(unresolved, config)
    written = (
        (unresolved / "tags.jsonl").read_text() if (unresolved / "tags.jsonl").exists() else ""
    )
    assert "hard_accel" not in written and "cornering" not in written
    assert "IMU tags dormant (unresolved)" in caplog.text
    assert "resolve_imu_frame.py" in caplog.text


# ---------------------------------------------------------------------------
# Named behaviour without an applicable sidecar
# ---------------------------------------------------------------------------


def test_each_kind_of_absent_imu_frame_is_named(tmp_path: Path, config: CanaryConfig) -> None:
    unresolved = write_session(tmp_path, "2026-09-26T120000", resolve=False)
    no_imu = write_session(tmp_path, "2026-09-26T130000", imu=False, resolve=False)
    refused = write_session(tmp_path, "2026-09-26T140000", resolve=False)
    stale = write_session(tmp_path, "2026-09-26T150000")

    # A refusal on record (here: pretend the heading was missing).
    from src.preprocessing.imu_session import ImuFrameRefusal, refused_sidecar

    write_imu_frame_sidecar(
        refused, refused_sidecar(ImuFrameRefusal("no_heading", "no gps:heading"), now=NOW)
    )
    # A resolved sidecar written before the low-pass initial state was recorded.
    raw = json.loads((stale / IMU_FRAME_FILENAME).read_text())
    del raw["linear_accel"]["initial_gravity_body_g"]
    del raw["linear_accel"]["first_quiescent_sample"]
    (stale / IMU_FRAME_FILENAME).write_text(json.dumps(raw))
    assert read_imu_frame_sidecar(stale) is not None  # still readable (the dyno reads it)

    frame = resample_and_segment([unresolved, no_imu, refused, stale], config)
    uses = frame.attrs[IMU_FRAME_ATTR]
    assert {name: use["status"] for name, use in uses.items()} == {
        unresolved.name: "unresolved",
        no_imu.name: "no_imu_rows",
        refused.name: "refused",
        stale.name: "stale",
    }
    assert "scripts/resolve_imu_frame.py" in uses[unresolved.name]["reason"]
    assert "no_heading" in uses[refused.name]["reason"]
    assert "no --replace needed" in uses[stale.name]["reason"]
    # None of them carries a frame; the raw axes are still there.
    assert LON_MAX not in frame.columns
    assert "imu:ACCEL_X" in frame.columns


def test_a_stale_sidecar_backfills_with_the_same_reference(tmp_path: Path) -> None:
    """A sidecar written before 2026-09-26 steered by ``obd:SPEED`` even on a
    session with 0x2D1 frames: the resolver then read through the on-read set,
    which lacks ``VEHICLE_SPEED_2D1``. Re-resolving it without ``replace``
    keeps that speed source, so the reference is the same and only the
    low-pass's initial state is added. A resolve with ``replace`` (or with no
    sidecar) takes 0x2D1, which moves the reference: the pin is load-bearing."""
    from src.preprocessing.imu_session import read_imu_inputs, resolve_imu_frame

    speeds = ("2d1", "obd", "gps")
    session = write_session(tmp_path, speeds=speeds, resolve=False)
    # What the old reader returned: no 0x2D1 speed.
    old_inputs = {
        column: series
        for column, series in read_imu_inputs(session).items()
        if column != "decoded:VEHICLE_SPEED_2D1"
    }
    old = resolve_imu_frame(old_inputs, now=NOW)
    assert isinstance(old, ImuSessionFrame)
    raw = old.sidecar.model_dump(mode="json")
    del raw["linear_accel"]["initial_gravity_body_g"]
    del raw["linear_accel"]["first_quiescent_sample"]
    (session / IMU_FRAME_FILENAME).write_text(json.dumps(raw))
    old_sidecar = read_imu_frame_sidecar(session)
    assert old_sidecar is not None and old_sidecar.specific_force is not None
    assert old_sidecar.inputs is not None and old_sidecar.inputs.speed_source == "obd:SPEED"
    assert plan_imu_frame(session).status == "stale"
    assert "no --replace needed" in plan_imu_frame(session).reason

    again = resolve_session_imu_frame(session, now=NOW)  # no replace
    assert isinstance(again, ImuSessionFrame)
    new_sidecar = read_imu_frame_sidecar(session)
    assert new_sidecar is not None and new_sidecar.specific_force is not None
    assert new_sidecar.specific_force.reference_id == old_sidecar.specific_force.reference_id
    assert new_sidecar.inputs is not None and new_sidecar.inputs.speed_source == "obd:SPEED"
    assert new_sidecar.linear_accel is not None
    assert new_sidecar.linear_accel.initial_gravity_body_g is not None
    assert plan_imu_frame(session).status == "applied"

    best = resolve_session_imu_frame(session, write=False, replace=True, now=NOW)
    assert isinstance(best, ImuSessionFrame)
    assert best.sidecar.inputs is not None
    assert best.sidecar.inputs.speed_source == "decoded:VEHICLE_SPEED_2D1"
    assert best.sidecar.specific_force is not None
    assert best.sidecar.specific_force.reference_id != old_sidecar.specific_force.reference_id
    fresh = write_session(tmp_path, "2026-09-26T130000", speeds=speeds, resolve=False)
    first = resolve_session_imu_frame(fresh, write=False, now=NOW)
    assert isinstance(first, ImuSessionFrame) and first.sidecar.inputs is not None
    assert first.sidecar.inputs.speed_source == "decoded:VEHICLE_SPEED_2D1"


def test_a_sidecar_from_other_rows_is_refused(tmp_path: Path, config: CanaryConfig) -> None:
    """A sidecar that does not describe the session's IMU rows (here: resolved
    from a shorter recording of the same drive) raises rather than emit a
    frame from another fit."""
    session = write_session(tmp_path, "2026-09-26T120000", resolve=False)
    other = write_session(tmp_path, "2026-09-26T130000", segments=DRIVE[:-1])
    (session / IMU_FRAME_FILENAME).write_text((other / IMU_FRAME_FILENAME).read_text())
    with pytest.raises(ImuFrameStaleError, match="resolve_imu_frame.py"):
        resample_and_segment([session], config)


# ---------------------------------------------------------------------------
# Live vs offline on the same drive
# ---------------------------------------------------------------------------


def _live_modes(config: CanaryConfig, seconds: float) -> dict[float, tuple[str | None, float]]:
    """``{t: (operating_mode, imu:ACCEL_LON_MAX)}`` from LiveFeatureSource, the
    tick at ``t + 1`` labelled ``t`` (the offline row ``t`` is the bin
    ``[t, t + 1)``)."""
    source = LiveFeatureSource(config=config)
    stream = sorted(records(), key=lambda r: r.timestamp)
    out: dict[float, tuple[str | None, float]] = {}
    i = 0
    for second in range(1, int(seconds) + 2):
        now = T0 + timedelta(seconds=second)
        while i < len(stream) and stream[i].timestamp < now:
            source.observe(stream[i])
            i += 1
        source.tick(now)
        point = source.latest_operating_point or {}
        value = point.get(LON_MAX)
        out[float(second - 1)] = (
            point.get("operating_mode"),
            float(value) if isinstance(value, float) else math.nan,
        )
    return out


def test_live_and_offline_agree_except_where_live_cannot_see_accel(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """Measured on one synthetic drive (H02.4 evidence):

    * ``imu:ACCEL_LON_MAX`` agrees within 2 mg on every tick where both paths
      have it; live has none for its first ticks of the first pull (its yaw is
      decided ``QUIESCENT_LOOKAHEAD_SECONDS`` late), offline has it from the
      first sample (session-wide fit).
    * ``operating_mode`` agrees on every tick the offline segmenter does not
      put in an accel-gated mode (or hold there by dwell). On those it
      differs: the live segmenter's signal map
      (``feature_pipeline.SEGMENTER_SIGNAL_COLUMNS``) carries no
      ``accel_lon``/``accel_lat``, so live never enters ``launch`` /
      ``hard_brake`` / ``cornering`` even with the channels on its operating
      point.
    """
    session = write_session(tmp_path)
    offline = resample_and_segment([session], config)
    seconds = sum(seg.seconds for seg in DRIVE)
    live = _live_modes(config, seconds)

    offline_modes = {
        (t - pd.Timestamp(T0)).total_seconds(): (m, v)
        for t, m, v in zip(
            offline["timestamp"], offline["operating_mode"], offline[LON_MAX], strict=True
        )
    }
    common = sorted(set(offline_modes) & set(live))
    assert len(common) >= 70

    assert not any(math.isnan(offline_modes[t][1]) for t in common)
    live_missing = [t for t in common if math.isnan(live[t][1])]
    launch_start = _windows(DRIVE)["launch"][0]
    assert max(live_missing) <= launch_start + 2  # only until the first pull decides yaw
    both = [t for t in common if not math.isnan(live[t][1])]
    diffs = [abs(live[t][1] - offline_modes[t][1]) for t in both]
    assert max(diffs) < 0.002

    accel_ticks = {t for t in common if offline_modes[t][0] in ACCEL_MODES}
    disagree = {t for t in common if offline_modes[t][0] != live[t][0]}
    assert disagree <= accel_ticks, sorted(disagree - accel_ticks)
    agree = len(common) - len(disagree)
    # Recorded in H02.4: 54 of 73 ticks agree; all 19 disagreements are accel-mode ticks.
    assert (agree, len(common), len(accel_ticks)) == (54, 73, 19)


# ---------------------------------------------------------------------------
# H01.1: the training chain never sees the frame
# ---------------------------------------------------------------------------


def test_the_training_chain_is_the_same_with_or_without_the_sidecar(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """The detectors' feature frame (``build_feature_frame``) does not read
    through the ``value_number`` chain, so resolving a session's IMU frame
    changes nothing in it, and no ``imu:`` column is trainable either way."""
    from src.preprocessing.feature_pipeline import build_feature_frame, trainable_feature_columns

    session = write_session(tmp_path)
    with_frame = build_feature_frame([session], config, max_workers=1)
    (session / IMU_FRAME_FILENAME).unlink()
    without = build_feature_frame([session], config, max_workers=1)
    pd.testing.assert_frame_equal(with_frame, without, check_exact=True)
    assert not any(c.startswith("imu:ACCEL_LON") for c in with_frame.columns)
    assert not [c for c in trainable_feature_columns(with_frame) if c.startswith("imu:")]
