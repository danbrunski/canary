"""H04.4: the signed IMU longitudinal channel, ``imu:ACCEL_LON_MEAN``.

The longitudinal-acceleration committee's IMU member. Every other IMU
aggregate is a magnitude; this one keeps the sign, so it can be compared with
a speed derivative. One aggregation (the per-tick mean of the resolved
``imu:ACCEL_LON``): offline in ``imu_decimation`` / ``imu_decimation.decimate_imu_long``
on the sidecar's frame, live in ``LiveImuFrame`` with its own running sign
evidence. Absent — never 0 — wherever the frame cannot say which way is
forward.
"""

from __future__ import annotations

import math
import shutil
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest

from src.config import CanaryConfig
from src.features.live import LiveFeatureSource
from src.preprocessing.imu_decimation import (
    ACCEL_LON_COLUMN,
    ACCEL_LON_MAX_COLUMN,
    ACCEL_LON_MEAN_COLUMN,
    decimate_imu_long,
    decimate_imu_wide,
)
from src.preprocessing.imu_live import SIGN_ALPHA, SIGN_RHO_G2, LiveImuFrame, sign_log_evidence
from src.preprocessing.imu_session import ImuSessionFrame, resolve_session_imu_frame
from src.preprocessing.loader import resample_and_segment
from src.signals.committees import (
    GPS_SPEED_ACCEL_G,
    IMU_ACCEL_LON_MEAN,
    LONGITUDINAL_ACCEL_COMMITTEE,
    STANDARD_GRAVITY,
    CommitteeEvaluator,
    CommitteeProfile,
    evaluate_committee_frame,
    physical_committee_point,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.test_imu_stream import DRIVE, NOW, T0, Segment, records, write_session
from tests.test_imu_stream import config as config  # noqa: F401 - fixture

HZ = 50
START = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
PULL_G = 50.0 / 3.6 / 4.0 / STANDARD_GRAVITY
BRAKE_G = -50.0 / 3.6 / 5.0 / STANDARD_GRAVITY


def _rot_z(deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _tilt() -> np.ndarray:
    c, s = math.cos(math.radians(5.0)), math.sin(math.radians(5.0))
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


#: Rest, then 20 s stop-and-go cycles from t = 10 s: a 4 s pull to 50,
#: 6 s cruise, a 5 s brake to rest, 5 s at rest.
CYCLE_S = 20.0


def _profile(t: float) -> tuple[float, float]:
    """(kph, a_g) at ``t``."""
    if t < 10.0:
        return 0.0, 0.0
    c = (t - 10.0) % CYCLE_S
    if c < 4.0:
        return 50.0 * c / 4.0, PULL_G
    if c < 10.0:
        return 50.0, 0.0
    if c < 15.0:
        return 50.0 - 50.0 * (c - 10.0) / 5.0, BRAKE_G
    return 0.0, 0.0


def _run_live(
    mount_yaw_deg: float, seconds: float = 70.0, *, speed_until: float = math.inf
) -> tuple[dict[int, dict[str, float]], list[str]]:
    """Tick ``LiveImuFrame`` at 1 Hz; ``{second: values}`` and each tick's status.
    Speed readings stop at ``speed_until``; course carries on."""
    mount = _rot_z(mount_yaw_deg) @ _tilt()
    rng = np.random.default_rng(1)
    frame = LiveImuFrame()
    out: dict[int, dict[str, float]] = {}
    statuses: list[str] = []
    for i in range(round(seconds * HZ)):
        t = i / HZ
        at = START + timedelta(seconds=t)
        kph, a_g = _profile(t)
        if i % HZ == 0:
            if i:
                values, _ = frame.take_tick()
                out[i // HZ - 1] = values
                statuses.append(frame.status)
            if t < speed_until:
                frame.observe_input("obd:SPEED", float(round(kph)), at)
            frame.observe_input("gps:heading", 90.0, at)
        body = mount @ np.array([a_g, 0.0, 1.0]) + rng.normal(0.0, 0.003, 3)
        for axis, value in zip(("ACCEL_X", "ACCEL_Y", "ACCEL_Z"), body, strict=True):
            frame.observe_imu(axis, float(value), at)
    return out, statuses


@pytest.mark.parametrize("mount_yaw_deg", [35.0, 215.0])
def test_live_signs_a_known_pull_and_brake_whichever_way_the_board_faces(
    mount_yaw_deg: float,
) -> None:
    """The board's yaw at 35 deg and 215 deg puts its X axis forward and
    backward; the speed derivative's votes sign both the same way. The first
    pull alone is too little evidence (4 votes < ``sign_min_votes``), so the
    signed mean appears only once a brake has voted too, and then every tick
    carries the right sign."""
    ticks, statuses = _run_live(mount_yaw_deg)
    # At rest and through the first pull: no signed value at all.
    assert all(ACCEL_LON_MEAN_COLUMN not in ticks[s] for s in range(0, 14))
    assert statuses[13] == "sign_unresolved"
    # The third cycle (from 50 s): pull, cruise, brake.
    for second in (52, 53):
        assert ticks[second][ACCEL_LON_MEAN_COLUMN] == pytest.approx(PULL_G, abs=0.01)
    for second in (56, 57, 58):
        assert abs(ticks[second][ACCEL_LON_MEAN_COLUMN]) < 0.01
    for second in (61, 62, 63):
        assert ticks[second][ACCEL_LON_MEAN_COLUMN] == pytest.approx(BRAKE_G, abs=0.01)
    assert statuses[-1] == "resolving"
    # Never wrong-signed, on any tick it was emitted.
    for second, values in ticks.items():
        if ACCEL_LON_MEAN_COLUMN in values and abs(_profile(second + 0.5)[1]) > 0.1:
            assert np.sign(values[ACCEL_LON_MEAN_COLUMN]) == np.sign(_profile(second + 0.5)[1])


def test_no_speed_change_leaves_the_sign_unresolved_and_the_mean_absent() -> None:
    """Speed goes silent at rest, before the pull: course still finds the
    straight-line axis (yaw), but no speed change votes for which end is
    forward. The magnitudes flow, the signed mean does not, and the status
    names the missing step."""
    ticks, statuses = _run_live(35.0, speed_until=9.5)
    for second in (61, 62, 63):  # a brake, long after the first pull resolved the yaw
        # 0.02 g: with speed silent, gravity is never refined past the rest.
        assert ticks[second][ACCEL_LON_MAX_COLUMN] == pytest.approx(-BRAKE_G, abs=0.02)
    assert all(ACCEL_LON_MEAN_COLUMN not in values for values in ticks.values())
    assert statuses[-1] == "sign_unresolved"


def _cruise_then_pull(seed: int) -> tuple[int, int]:
    """The verifier's case (2026-10-05): rest, 60 s of cruise at 50 km/h with
    road noise (0.05 g per IMU axis) and speed jitter (0.6 km/h, read through
    the 1 km/h PID), then a 4 s 0.15 g pull. ``(wrong-signed ticks, signed
    ticks)`` over the whole run."""
    rng = np.random.default_rng(seed)
    frame = LiveImuFrame()
    wrong = signed = 0
    accel = 0.0
    for i in range(80 * HZ):
        t = i / HZ
        at = START + timedelta(seconds=t)
        if t < 10.0:
            kph, a_g = 0.0, 0.0
        elif t < 70.0:
            kph, a_g = 50.0 + rng.normal(0.0, 0.6), 0.0
        else:
            kph, a_g = 50.0 + (t - 70.0) * 0.15 * STANDARD_GRAVITY * 3.6, 0.15
        if i % HZ == 0:
            if i:
                values, _ = frame.take_tick()
                if ACCEL_LON_MEAN_COLUMN in values:
                    signed += 1
                    wrong += accel > 0.1 and values[ACCEL_LON_MEAN_COLUMN] < 0.0
            accel = a_g
            frame.observe_input("obd:SPEED", float(round(kph)), at)
            frame.observe_input("gps:heading", 90.0, at)
        body = np.array([a_g, 0.0, 1.0]) + rng.normal(0.0, 0.05 if t >= 10.0 else 0.002, 3)
        for axis, value in zip(("ACCEL_X", "ACCEL_Y", "ACCEL_Z"), body, strict=True):
            frame.observe_imu(axis, float(value), at)
    return wrong, signed


@pytest.mark.slow  # 2-7 s serial: whole-drive simulations
def test_cruise_jitter_never_signs_a_pull_backwards() -> None:
    """With the first vote deciding, 18 of 40 seeds signed the pull wrong.
    Gated votes (quantisation, rate, interval-mean ``h``) plus the sequential
    sign test: no wrong-signed tick on any of the same 40 seeds. A lone 4 s
    pull is also too little evidence, so nothing is signed at all — fail
    closed."""
    results = [_cruise_then_pull(seed) for seed in range(40)]
    assert sum(wrong for wrong, _ in results) == 0
    assert sum(signed for _, signed in results) == 0  # measured: never resolves


def _simulate_votes(
    drives: int, votes: int, p_agree: float, seed: int
) -> tuple[int, int, list[int]]:
    """Many drives at the vote level, through :func:`sign_log_evidence` — the
    function ``LiveImuFrame`` decides by. Each vote: ``h̄`` of 0.05-0.4 g
    along a per-drive axis with 20 deg of scatter; its sign agrees with that
    axis with probability ``p_agree``. Returns (drives that ever resolved,
    drives that resolved the wrong way, the vote index each resolved at)."""
    rng = np.random.default_rng(seed)
    threshold = -math.log(SIGN_ALPHA)
    resolved = wrong = 0
    at: list[int] = []
    for start in range(0, drives, 1000):
        n = min(1000, drives - start)
        axis = rng.uniform(0.0, math.pi, (n, 1))
        angle = axis + rng.normal(0.0, math.radians(20.0), (n, votes))
        size = rng.uniform(0.05, 0.4, (n, votes))
        sign = np.where(rng.random((n, votes)) < p_agree, 1.0, -1.0)
        h1, h2 = size * np.cos(angle), size * np.sin(angle)
        f1, f2 = np.cumsum(sign * h1, axis=1), np.cumsum(sign * h2, axis=1)
        evidence = sign_log_evidence(
            f1,
            f2,
            np.cumsum(h1 * h1, axis=1),
            np.cumsum(h1 * h2, axis=1),
            np.cumsum(h2 * h2, axis=1),
            SIGN_RHO_G2,
        )
        crossed = evidence >= threshold
        ever = crossed.any(axis=1)
        first = crossed.argmax(axis=1)
        rows = np.arange(n)
        along = f1[rows, first] * np.cos(axis[:, 0]) + f2[rows, first] * np.sin(axis[:, 0])
        resolved += int(ever.sum())
        wrong += int((ever & (along < 0.0)).sum())
        at += [int(i) for i in first[ever]]
    return resolved, wrong, at


def test_random_sign_votes_resolve_within_the_declared_rate() -> None:
    """The sequential test is checked after every vote, all drive long; its
    error rate must hold under that. 10,000 drives of 500 fair-coin votes
    (well past the ~90 a real sign needs): measured 2026-10-05, 3 drives
    resolved (rate 3e-4; 1 of 10,000 with 1,000 votes each) against the
    declared alpha = 1e-3 — Ville's bound is conservative. Asserted with
    three binomial standard deviations of slack over alpha."""
    resolved, _wrong, _at = _simulate_votes(10_000, 500, 0.5, seed=0)
    assert resolved / 10_000 <= SIGN_ALPHA + 3.0 * math.sqrt(SIGN_ALPHA / 10_000)


def test_consistent_votes_resolve_the_right_way() -> None:
    """The other side: with 75 % of votes agreeing (the offline sign split
    on the real-timing slice was 86 / 27), every drive resolves, none the
    wrong way, at a median of 91 votes (measured 2026-10-05)."""
    resolved, wrong, at = _simulate_votes(1_000, 500, 0.75, seed=1)
    assert (resolved, wrong) == (1_000, 0)
    assert float(np.median(at)) < 150


def _raw_lon_frame() -> pd.DataFrame:
    rows = []
    for i in range(300):  # 3 s at 100 Hz, then a 1 s hole, then 1 s more
        rows.append(
            {
                "session": "s",
                "timestamp": START + timedelta(seconds=i / 100.0),
                ACCEL_LON_COLUMN: 0.2 if i < 100 else -0.3 if i < 200 else 0.05 * math.sin(i),
            }
        )
    for i in range(400, 500):
        rows.append(
            {
                "session": "s",
                "timestamp": START + timedelta(seconds=i / 100.0),
                ACCEL_LON_COLUMN: 0.1,
            }
        )
    rows.append({"session": "s", "timestamp": START + timedelta(seconds=3.5), "obd:RPM": 900.0})
    return pd.DataFrame(rows).sort_values("timestamp", kind="stable").reset_index(drop=True)


def test_offline_wide_and_long_decimation_agree_and_keep_the_sign() -> None:
    wide = decimate_imu_wide(_raw_lon_frame(), hz=1.0)
    by_tick = wide.groupby(wide["timestamp"].dt.floor("1s"))[ACCEL_LON_MEAN_COLUMN].first()
    assert by_tick.iloc[0] == pytest.approx(0.2)
    assert by_tick.iloc[1] == pytest.approx(-0.3)
    # The tick holding only an OBD row has no IMU sample: NaN, never 0.
    assert math.isnan(by_tick.loc[pd.Timestamp(START + timedelta(seconds=3))])
    assert by_tick.iloc[-1] == pytest.approx(0.1)

    long = _raw_lon_frame().melt(
        id_vars=["session", "timestamp"], var_name="source_channel", value_name="value_number"
    )
    long = long.dropna(subset=["value_number"]).reset_index(drop=True)
    decimated = decimate_imu_long(long, hz=1.0)
    means = decimated.loc[decimated["source_channel"] == ACCEL_LON_MEAN_COLUMN]
    long_by_tick = means.groupby(means["timestamp"].dt.floor("1s"))["value_number"].first()
    expected = by_tick.dropna()
    np.testing.assert_allclose(long_by_tick.to_numpy(), expected.to_numpy(), rtol=0, atol=1e-12)
    assert list(long_by_tick.index) == list(expected.index)


# ---------------------------------------------------------------------------
# Live vs offline, through the real producers, to the committee
# ---------------------------------------------------------------------------

#: A test-only relation so the committee runs: NOT a measured tolerance (the
#: real one waits for the H02.6 drive and lives in committees.yaml).
_TEST_PROFILE = CommitteeProfile.model_validate(
    {
        "schema_version": 1,
        "committees": {
            LONGITUDINAL_ACCEL_COMMITTEE.name: {
                "reference": GPS_SPEED_ACCEL_G,
                "relations": [{"member": IMU_ACCEL_LON_MEAN, "ratio": 1.0, "evidence": "test"}],
                "tolerance": {
                    "kind": "absolute",
                    "max_departure": 0.5,
                    "sustain_seconds": 10,
                    "min_verdicts": 5,
                    "max_verdict_gap_seconds": 3,
                    "conditions": [],
                    "evidence": "test only",
                },
            }
        },
    }
)


#: ``DRIVE`` twice, the second lap turned on from where the first ended
#: (course 0 deg) after a stop: one lap gives the live sign test too few
#: votes to resolve before the drive is over.
TWO_LAPS: list[Segment] = [
    *DRIVE,
    Segment(4.0, 40.0, 0.0, heading0=0.0, heading1=0.0),  # stop between laps
    *(
        replace(seg, heading0=(seg.heading0 - 90.0) % 360.0, heading1=(seg.heading1 - 90.0) % 360.0)
        for seg in DRIVE
    ),
]


def _truth_g() -> dict[float, float]:
    """The drive's injected longitudinal acceleration, per whole second."""
    out: dict[float, float] = {}
    t = 0.0
    for seg in TWO_LAPS:
        a_g = (seg.kph1 - seg.kph0) / 3.6 / seg.seconds / STANDARD_GRAVITY
        start, stop = t, t + seg.seconds
        for second in range(math.ceil(start), math.floor(stop)):
            if second + 1 <= stop:
                out[float(second)] = a_g
        t = stop
    return out


#: The IMU goes silent here (the cruise after the turn); everything else
#: carries on to the end of the drive.
IMU_SILENT_AT = 140.0


def _live_points(config: CanaryConfig, seconds: float) -> dict[float, dict[str, object]]:
    """``{t: operating point}`` from LiveFeatureSource, the tick at ``t + 1``
    labelled ``t`` (the offline row ``t`` is the bin ``[t, t + 1)``)."""
    source = LiveFeatureSource(config=config)
    stream = sorted(records(TWO_LAPS), key=lambda r: r.timestamp)
    silent = T0 + timedelta(seconds=IMU_SILENT_AT)
    out: dict[float, dict[str, object]] = {}
    i = 0
    for second in range(1, int(seconds) + 2):
        now = T0 + timedelta(seconds=second)
        while i < len(stream) and stream[i].timestamp < now:
            if not (stream[i].source == "imu" and stream[i].timestamp >= silent):
                source.observe(stream[i])
            i += 1
        source.tick(now)
        out[float(second - 1)] = dict(source.latest_operating_point or {})
    return out


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, float) and not math.isnan(value) else None


@pytest.mark.slow  # 2-7 s serial: whole-drive simulations
def test_live_and_offline_deliver_the_same_signed_channel_to_the_committee(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """Synthetic drive: two laps of ``tests/test_imu_stream.DRIVE`` (mount
    yaw 140 deg, 6 / -4 deg tilt, 4 mg noise; per lap a 0.38 g pull, a
    0.45 g brake, a 0.23 g pull, a 0.35 g turn; a stop between), the IMU
    silent from 140 s (the second lap's last cruise).

    * The offline value_number chain (sidecar → stream → decimation →
      resampler) and the live feature source (``LiveImuFrame`` → operating
      point) both carry ``imu:ACCEL_LON_MEAN``, signed. Measured 2026-10-05:
      on the 74 whole seconds both have before the IMU goes silent, offline
      is within 1.6 mg of the injected acceleration, live within 1.5 mg, and
      the two within 1.2 mg. Live signs nothing until its sequential sign
      test clears (from t = 55 s, the first lap's second pull); offline has
      every tick (session-wide fit).
    * The committee reaches verdicts on the member on both paths.
    * The IMU going silent mid-drive turns the live verdict into a named
      no-verdict (``stale``, then ``member_missing``), never a 0 g reading.
    """
    session = write_session(tmp_path, segments=TWO_LAPS)
    offline = resample_and_segment([session], config)
    assert IMU_ACCEL_LON_MEAN in offline.columns
    offline_by_t = {
        (t - pd.Timestamp(T0)).total_seconds(): _number(v)
        for t, v in zip(offline["timestamp"], offline[IMU_ACCEL_LON_MEAN], strict=True)
    }
    seconds = sum(seg.seconds for seg in TWO_LAPS)
    live = _live_points(config, seconds)
    live_by_t = {t: _number(point.get(IMU_ACCEL_LON_MEAN)) for t, point in live.items()}
    truth = _truth_g()

    both = [
        t
        for t in sorted(truth)
        if t < IMU_SILENT_AT and offline_by_t.get(t) is not None and live_by_t.get(t) is not None
    ]
    assert len(both) >= 70  # 74 measured
    assert all(offline_by_t.get(t) is not None for t in truth)  # offline: every tick
    assert max(t for t in truth if t < IMU_SILENT_AT and live_by_t.get(t) is None) <= 54.0

    def gap(a: Mapping[float, float | None], b: Mapping[float, float | None]) -> float:
        return max(abs(float(a[t] or 0.0) - float(b[t] or 0.0)) for t in both)

    assert gap(offline_by_t, truth) < 0.002
    assert gap(live_by_t, truth) < 0.002
    assert gap(live_by_t, offline_by_t) < 0.002
    # Signed: a brake reads backward and a pull forward, on both paths.
    assert min(truth[t] for t in both) < -0.4 and max(truth[t] for t in both) > 0.2

    # The member reaches the committee, offline and live.
    counts = evaluate_committee_frame(offline, _TEST_PROFILE).counts
    assert counts[LONGITUDINAL_ACCEL_COMMITTEE.name].get("agree", 0) >= 40, counts
    evaluator = CommitteeEvaluator(_TEST_PROFILE)
    states: dict[float, str | None] = {}
    for t, point in sorted(live.items()):
        if not point:
            continue
        evaluator.observe(physical_committee_point(point, None), T0.timestamp() + t + 1)
        entry = evaluator.snapshot()["committees"][LONGITUDINAL_ACCEL_COMMITTEE.name]
        states[t] = entry["state"] if entry["state"] != "no_verdict" else entry["reason"]
    before = [states[t] for t in both if t in states]
    assert before == ["agree"] * len(both), before

    # Silent IMU: named, never a verdict on a carried value.
    after = [state for t, state in states.items() if t >= IMU_SILENT_AT]
    assert after and not {"agree", "disagree"} & set(after), after
    assert after[0] == "stale" and set(after[1:]) == {"member_missing"}, after


# ---------------------------------------------------------------------------
# Real drive timing: the 08-03 ground-truth slice with an IMU synthesised on it
# ---------------------------------------------------------------------------

DRIVE_SLICE = Path(__file__).resolve().parent / "fixtures" / "ground_truth" / "drive-slice"
SLICE_MOUNT = _rot_z(140.0) @ _tilt()


def _slice_with_imu(root: Path) -> tuple[Path, pd.Series, pd.DataFrame, list[TelemetryRecord]]:
    """The committed 7-minute 2026-08-03 slice (real Mode 01 speed, real GPS
    speed and course, at their real rates and jitter) plus a 50 Hz IMU made
    from that GPS speed and course through a known mount. No stored session
    holds IMU rows yet (the HAT IMU has not recorded a drive: H02.6), so the
    IMU is the one synthetic part. Returns the session, the injected
    longitudinal acceleration per whole second, the speed/course inputs and
    the IMU records."""
    from src.preprocessing.loader import read_session

    inputs = read_session(DRIVE_SLICE)
    inputs = inputs.loc[
        inputs["source_channel"].isin(["gps:speed", "gps:heading", "obd:SPEED"]),
        ["timestamp", "source_channel", "value_number"],
    ]
    gps_speed = inputs.loc[inputs["source_channel"] == "gps:speed"].sort_values("timestamp")
    course = inputs.loc[inputs["source_channel"] == "gps:heading"].sort_values("timestamp")
    t0 = gps_speed["timestamp"].min().ceil("1s")
    stamps = pd.date_range(t0, gps_speed["timestamp"].max().floor("1s"), freq="20ms")
    seconds = (stamps - t0).total_seconds().to_numpy()

    def at_imu(frame: pd.DataFrame, *, angle: bool = False) -> np.ndarray:
        """The channel on the IMU grid, from its per-second last reading: GPS
        epochs arrive in bursts (here 38.8 -> 41.6 km/h 21 ms apart, a 13 g
        step if differentiated as delivered), so the motion the IMU feels is
        built from whole seconds, while both paths still read the raw rows."""
        values = frame.set_index("timestamp")["value_number"].astype(float)
        if angle:
            values = pd.Series(np.unwrap(np.radians(values.to_numpy())), index=values.index)
        per_second = values.resample("1s").last().dropna()
        own = (per_second.index - t0).total_seconds().to_numpy()
        return cast(np.ndarray, np.interp(seconds, own, per_second.to_numpy()))

    speed_ms = at_imu(gps_speed) / 3.6
    lon_g = np.gradient(speed_ms, seconds) / STANDARD_GRAVITY
    course_rad = at_imu(course, angle=True)
    lat_g = -speed_ms * np.gradient(course_rad, seconds) / STANDARD_GRAVITY  # ISO: left +
    # GPS course is noise near rest: no lateral below 10 km/h.
    lat_g = np.where(speed_ms * 3.6 >= 10.0, lat_g, 0.0)
    rng = np.random.default_rng(0)
    body = (SLICE_MOUNT @ np.vstack([lon_g, lat_g, np.ones_like(lon_g)])).T
    body += rng.normal(0.0, 0.004, body.shape)
    imu = [
        TelemetryRecord.imu(stamp.timestamp(), axis, float(value), "g")
        for stamp, row in zip(stamps, body, strict=True)
        for axis, value in zip(("ACCEL_X", "ACCEL_Y", "ACCEL_Z"), row, strict=True)
    ]
    session = root / "2026-08-03T231900"
    session.mkdir()
    shutil.copy(DRIVE_SLICE / "telemetry-00000.parquet", session / "telemetry-00000.parquet")
    write_records_to_parquet(imu, root / "imu", max_rows=10**6)
    for k, part in enumerate(sorted((root / "imu").glob("telemetry-*.parquet")), start=1):
        shutil.copy(part, session / f"telemetry-{k:05d}.parquet")
    truth = pd.Series(lon_g, index=stamps).groupby(stamps.floor("1s")).mean()
    return session, truth, inputs, imu


def _live_on_records(inputs: pd.DataFrame, imu: list[TelemetryRecord]) -> dict[pd.Timestamp, float]:
    """``LiveImuFrame`` fed the slice's inputs and the IMU in time order,
    ticked at each whole second: ``{bin start: imu:ACCEL_LON_MEAN}``."""
    stream: list[tuple[float, int, str, float]] = [
        (row.timestamp.timestamp(), 0, str(row.source_channel), float(row.value_number))
        for row in cast(Any, inputs).itertuples()
    ]
    stream += [(r.timestamp.timestamp(), 1, r.channel, float(r.value)) for r in imu]  # type: ignore[arg-type]
    stream.sort()
    frame = LiveImuFrame()
    out: dict[pd.Timestamp, float] = {}
    tick = math.floor(stream[0][0]) + 1
    for stamp, is_imu, channel, value in stream:
        while stamp >= tick:
            values, _ = frame.take_tick()
            if ACCEL_LON_MEAN_COLUMN in values:
                out[pd.Timestamp(tick - 1, unit="s", tz="UTC")] = values[ACCEL_LON_MEAN_COLUMN]
            tick += 1
        at = datetime.fromtimestamp(stamp, tz=UTC)
        if is_imu:
            frame.observe_imu(channel, value, at)
        else:
            frame.observe_input(channel, value, at)
    assert frame.status == "resolving"
    return out


@pytest.mark.slow  # 2-7 s serial: whole-drive simulations
def test_live_and_offline_on_real_drive_timing(tmp_path: Path, config: CanaryConfig) -> None:
    """Parity on real input timing (1 Hz-ish Mode 01 speed, 2 Hz GPS, real
    turns). The IMU is built from per-second GPS speed and course (no
    lateral below 10 km/h), so its motion is smooth while both paths read the
    raw, bursty rows. Measured 2026-10-05 over the 239 whole seconds both
    paths carry of 419 (live signs nothing for its first 179 s: the
    sequential sign test, alpha 1e-3, needs the evidence of several pulls):

    * **Sign:** right on all 30 seconds with |a| >= 0.1 g, on both paths.
    * **Agreement:** |live - offline| median 0.032 g, p95 0.097 g, max
      0.145 g; corr(live, offline) 0.81; corr with the injected acceleration
      0.92 live, 0.78 offline. Not the synthetic drive's ~1 mg. Re-run
      without lateral acceleration, the offline yaw lands on the mount
      (-0.700 rad, against -0.102 with the turns: the turns pull the
      session-wide fit 34 deg), yet p95 vs truth is still 86 mg offline and
      59 mg live: the gravity low-pass absorbs slow acceleration on both
      paths. So the H04.4 tolerance must be measured on the path that will
      run it (live), on the H02.6 drive.

    These bounds are the measurement, not a specification.
    """
    session, truth, inputs, imu = _slice_with_imu(tmp_path)
    assert isinstance(resolve_session_imu_frame(session, now=NOW), ImuSessionFrame)
    offline = resample_and_segment([session], config)
    assert offline.attrs["imu_frame"][session.name]["status"] == "applied"
    offline_by_t = dict(zip(offline["timestamp"], offline[IMU_ACCEL_LON_MEAN], strict=True))
    live = _live_on_records(inputs, imu)

    rows = pd.DataFrame(
        {
            "truth": truth,
            "offline": [offline_by_t.get(t, math.nan) for t in truth.index],
            "live": [live.get(t, math.nan) for t in truth.index],
        },
        index=truth.index,
    ).dropna()
    assert len(rows) >= 230  # 239 measured
    moving = rows.loc[rows["truth"].abs() >= 0.1]
    assert len(moving) >= 28  # 30 measured
    for path in ("offline", "live"):
        assert (np.sign(moving[path]) == np.sign(moving["truth"])).all(), path
    parity = (rows["live"] - rows["offline"]).abs()
    assert parity.quantile(0.95) < 0.11
    assert rows["live"].corr(rows["offline"]) > 0.75
    assert rows["offline"].corr(rows["truth"]) > 0.7
    assert rows["live"].corr(rows["truth"]) > 0.88
