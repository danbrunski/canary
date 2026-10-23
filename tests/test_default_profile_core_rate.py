"""The Xterra ``default`` core Mode 01 set achieves at least 1 Hz (D03.34).

Operator, 2026-09-26: at least 1 Hz *achieved*, not just planned. The core
fast set is RPM, SPEED, MAF, ENGINE_LOAD, THROTTLE_POS, TIMING_ADVANCE,
O2_WR_LAMBDA_S1, O2_B1S2, O2_B2S2 and CONTROL_MODULE_VOLTAGE, plus bank 2's
O2_WR_LAMBDA_S5, which polls at the bank-1 rate.

The 2026-09-20 drive's rates, 0.78/s for RPM and 0.47/s for TIMING_ADVANCE,
are sample counts divided by the whole 2.4 h session. That span includes
18 minutes before OBD polling started. Over the OBD span the median RPM
interval was 1.00 s. The losses the real scheduler owns are pinned here, each
on the fake clock against the real loop:

- **The never-answered back-off outlived key-on-engine-off.** The ECM left
  TIMING_ADVANCE out of its answers for 35 minutes of KOEO. The back-off
  doubled to 15 minutes, so the channel stayed silent 9.9-10.9 minutes past
  the engine start: 593 s from the first running RPM row, and the start fell
  inside the 61.7 s gap before it. This fake clock gives 10.6 minutes after
  the same 35-minute KOEO. An engine start now re-probes every backed-off
  channel (``rearm_on_engine_start``), once per real engine-off
  (``EngineStartLatch``).
- **A plan of exactly 1 Hz cannot achieve 1 Hz.** Mode 01 is silent while a
  Mode 22 window holds the session, and nothing gives those samples back:
  0.985 Hz here at weight 1.0, 0.965-0.975 Hz on the truck on 2026-09-23.
  The profile plans the core set at 1.5 Hz.
- **Due times were anchored on the request instant.** This was not a cause
  of the shortfall at weight 1.0, where every interval is a whole number of
  1 s rounds. It is what keeps medium and slow at plan once the core set
  goes to 1.5: a round member issued early moved its own phase forward every
  cycle, so a slower request's interval collapsed to whole round periods
  (medium 0.372 Hz instead of 0.333 at weight 1.5). Due times now advance on
  their own grid (``_next_request_due_at``).
"""

from __future__ import annotations

import dataclasses
import itertools
import threading
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import pytest

from src.collection import tuning_profile
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.capture_profiles import CaptureProfile, CaptureProfileCatalog
from src.collection.obd_collector import ObdReading
from src.collection.obd_registry import MODE01_REGISTRY
from src.collection.obd_scheduler import (
    ENGINE_OFF_HOLD_SECONDS,
    MAX_MISS_BACKOFF_SECONDS,
    CanObdSpec,
    EngineStartLatch,
    PollRequestState,
    _next_request_due_at,
    build_poll_requests,
    did_alias_table,
    profile_did_union,
)
from src.config import CanBusConfig, CollectionConfig
from src.yaml_load import load_yaml
from tests.capture_round_fakes import FakeClock, ScriptedEcu, install_clock, support_bits

CATALOG = Path("data/nissan-xterra-2008/capture_profiles.yaml")
CAN = CanBusConfig(interface="virtual", channel="core-rate", bitrate=500000)

#: The operator's core fast set, plus bank 2's wide-band sensor at the bank-1
#: rate (operator, 2026-09-26).
CORE = (
    "RPM",
    "SPEED",
    "MAF",
    "ENGINE_LOAD",
    "THROTTLE_POS",
    "TIMING_ADVANCE",
    "O2_WR_LAMBDA_S1",
    "O2_WR_LAMBDA_S5",
    "O2_B1S2",
    "O2_B2S2",
    "CONTROL_MODULE_VOLTAGE",
)
#: The Pi's configured DIDs (``canary.pi.example.yaml``).
PI_DIDS = (
    "ECM_AIR_CHARGE",
    "ECM_AF_SENSOR_V_B1",
    "ECM_AF_SENSOR_V_B2",
    "ECM_THROTTLE_POS_SENSOR_1",
    "ECM_RPM",
    "ECM_THROTTLE_POS_SENSOR_2",
    "ECM_EVAP_PURGE_CMD",
)
#: Request-to-response latency. The truck sustained 116.8 req/s on
#: 2026-09-10, under 8.6 ms a request loop included.
TRUCK_LATENCY_S = 0.010


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    install_clock(monkeypatch, fake)
    return fake


def _catalog() -> CaptureProfileCatalog:
    return CaptureProfileCatalog.model_validate(load_yaml(CATALOG.read_text()))


def _pid(channel: str) -> int:
    (pid,) = [pid for pid, d in MODE01_REGISTRY.items() if d.channel == channel]
    return pid


def _payloads(channels: Iterable[str], **overrides: bytes) -> dict[int, bytes]:
    """A plausible answer for every channel, at its registry width."""
    values = {
        _pid(name): bytes([0x40]) * MODE01_REGISTRY[_pid(name)].byte_length for name in channels
    }
    values[0x0C] = b"\x0b\xb8"  # 750 rpm: the engine is running
    for name, payload in overrides.items():
        values[_pid(name)] = payload
    return values


def _run(
    clock: FakeClock, bus: ScriptedEcu, spec: CanObdSpec, seconds: float
) -> tuple[list[ObdReading], float]:
    """Run the real loop; returns the readings and the wall instant it started."""
    readings: list[ObdReading] = []
    wall_start = clock.time()
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + seconds,
    )
    return readings, wall_start


def _small_spec(*pids: str, batch_max_pids: int = 6) -> CanObdSpec:
    """Mode 01 only, batching on (unless asked), no DTC or monitor polls."""
    return CanObdSpec(
        can=CAN,
        poll_rate_hz=1.0,
        pids=pids,
        capture_profiles_enabled=True,
        batch_max_pids=batch_max_pids,
        priority_channels=(),
        dtc_poll_interval_seconds=0.0,
        service_poll_interval_seconds=0.0,
        request_timeout_seconds=1.0,
    )


def _default(mode01: dict[str, float]) -> CaptureProfile:
    return CaptureProfile(name="default", mode01=mode01, corpus_eligible=True)


def _requested(bus: ScriptedEcu, pid: int, *, before: float | None = None) -> int:
    """Mode 01 requests that carried ``pid``, optionally sent before ``before``."""
    count = 0
    for at, msg in bus.sent:
        data = bytes(msg.data)
        if before is not None and at >= before:
            continue
        if len(data) >= 3 and data[1] == 0x01 and data[0] < 0x10 and pid in data[2 : 1 + data[0]]:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Due times advance on their own grid
# ---------------------------------------------------------------------------


def test_due_time_advances_from_the_due_time_not_the_request_instant(clock: FakeClock) -> None:
    """Early (a round member), late and far-late requests keep their grid.

    Anchored on the request instant, the early one came due at 12.5 (every
    cycle 0.5 s early), the late one at 13.4 (every cycle 0.4 s late), and
    the far-late one at 20.0 instead of the next grid point.
    """
    spec = _small_spec("COOLANT_TEMP")
    tuning_profile.configure_default(_default({"COOLANT_TEMP": 1.0}))
    (request,) = [r for r in build_poll_requests(None, spec) if r.channel == "COOLANT_TEMP"]
    assert request.poll_tier == "medium"  # 3.0 s at poll_rate_hz 1

    base = clock.monotonic()

    def next_due(issued: float) -> float:
        clock.advance(base + issued - clock.monotonic())
        state = PollRequestState(next_due_at=base + 10.0, last_requested_at=clock.monotonic())
        return _next_request_due_at(request, state, spec) - base

    assert next_due(9.5) == pytest.approx(13.0)  # issued 0.5 s early
    assert next_due(10.4) == pytest.approx(13.0)  # issued 0.4 s late
    assert next_due(17.0) == pytest.approx(19.0)  # whole periods skipped


def test_a_slower_request_keeps_its_rate_under_a_faster_round(clock: FakeClock) -> None:
    """Medium at 3.0 s under a 0.8 s round (RPM at weight 1.25) stays 1/3 Hz.

    Anchored on the request instant it ran every 2.4 s, three round periods:
    100 reads in 240 s instead of 80. The capture rounds issue a member up to
    one period early, and that advance recurred every cycle.
    """
    tuning_profile.configure_default(_default({"RPM": 1.25, "COOLANT_TEMP": 1.0}))
    bus = ScriptedEcu(clock, latency=TRUCK_LATENCY_S, values=_payloads(("RPM", "COOLANT_TEMP")))
    readings, _ = _run(clock, bus, _small_spec("RPM", "COOLANT_TEMP"), 240.0)
    counts = Counter(r.pid for r in readings)
    assert counts["RPM"] == pytest.approx(300, abs=1)
    assert counts["COOLANT_TEMP"] == pytest.approx(80, abs=1)


# ---------------------------------------------------------------------------
# An engine start re-probes what key-on-engine-off backed off
# ---------------------------------------------------------------------------


def test_engine_start_re_probes_a_channel_the_ecm_withheld_at_koeo(clock: FakeClock) -> None:
    """The 2026-09-20 TIMING_ADVANCE gap, on a 300 s KOEO.

    The ECM leaves 0x0E out of every batched answer until the engine runs.
    The never-answered back-off doubles (30, 60, 120, 240 s), so the old loop
    next asked 450 s in: 150 s after the engine started. Now the first RPM
    reading at or above the running floor re-probes it.
    """
    koeo_s = 300.0
    channels = ("RPM", "SPEED", "TIMING_ADVANCE")
    tuning_profile.configure_default(_default(dict.fromkeys(channels, 1.0)))
    values = _payloads(channels, RPM=b"\x00\x00")
    timing = values.pop(0x0E)
    bus = ScriptedEcu(clock, latency=TRUCK_LATENCY_S, values=values)
    engine_start = clock.monotonic() + koeo_s

    def start_engine(_msg: object) -> None:
        if clock.monotonic() >= engine_start and 0x0E not in bus.values:
            bus.values[0x0E] = timing
            bus.values[0x0C] = b"\x0b\xb8"

    bus.on_send.append(start_engine)
    readings, wall_start = _run(clock, bus, _small_spec(*channels), koeo_s + 30.0)

    stamps = [r.timestamp - wall_start for r in readings if r.pid == "TIMING_ADVANCE"]
    first = min(stamps, default=float("inf"))
    assert first - koeo_s < 3.0, f"TIMING_ADVANCE first read {first - koeo_s:.1f} s after start"
    # The back-off still held while the engine was off: 0, 30, 90 and 210 s.
    assert _requested(bus, 0x0E, before=engine_start) == 4


@pytest.mark.parametrize("batch_max_pids", [6, 1])
def test_rpm_flapping_across_the_running_floor_is_not_an_engine_start(
    clock: FakeClock, batch_max_pids: int
) -> None:
    """RPM alternating 390/410 never reads 0, so the engine never went off.

    When every upward crossing of 400 rpm counted as a start, the silent
    0x28 was re-probed on every other read: 150 probes in 300 s instead of
    the back-off's 4, and at a batch size of 1 those probes cut RPM from
    297 reads to 200 (verifier, 2026-09-26).
    """
    channels = ("RPM", "SPEED", "O2_WR_LAMBDA_S5")
    tuning_profile.configure_default(_default(dict.fromkeys(channels, 1.0)))
    bus = ScriptedEcu(
        clock, latency=TRUCK_LATENCY_S, values=_payloads(channels), silent_pids=(0x28,)
    )
    flap = iter(b"\x06\x18\x06\x68" * 10_000)  # 390 rpm, 410 rpm, ...

    def next_rpm(msg: object) -> None:
        data = bytes(msg.data)  # type: ignore[attr-defined]
        if len(data) >= 3 and data[1] == 0x01 and 0x0C in data[2 : 1 + data[0]]:
            bus.values[0x0C] = bytes([next(flap), next(flap)])

    bus.on_send.append(next_rpm)
    readings, _ = _run(clock, bus, _small_spec(*channels, batch_max_pids=batch_max_pids), 300.0)
    assert _requested(bus, 0x28) == 4  # 0, 30, 90 and 210 s: the back-off alone
    assert Counter(r.pid for r in readings)["RPM"] >= 297


def test_the_engine_start_latch_needs_a_sustained_engine_off() -> None:
    """Off is RPM 0 (``ignition_only``) for ``ENGINE_OFF_HOLD_SECONDS``."""
    latch = EngineStartLatch()
    assert not latch.observe(750.0, 0.0)  # a loop that starts running
    assert not latch.observe(0.0, 1.0)
    assert not latch.observe(0.0, 1.0 + ENGINE_OFF_HOLD_SECONDS - 0.1)
    assert not latch.observe(750.0, 32.0)  # a stall shorter than the hold
    assert not latch.observe(200.0, 40.0)  # cranking is neither off nor running
    assert not latch.observe(0.0, 41.0)
    assert not latch.observe(250.0, 41.0 + ENGINE_OFF_HOLD_SECONDS)
    assert not latch.observe(0.0, 41.0 + ENGINE_OFF_HOLD_SECONDS)
    assert latch.observe(750.0, 80.0)
    assert not latch.observe(750.0, 81.0)  # one start per engine-off


def test_a_listed_pid_the_ecm_never_answers_costs_a_probe_per_back_off(
    clock: FakeClock,
) -> None:
    """PID 0x28 is untested on the truck. If it never answers, an explicit
    list entry gets the same doubling back-off as an AUTO probe: five probes
    in 500 s (0, 30, 90, 210 and 450 s), doubling to one per 15 minutes."""
    channels = ("RPM", "O2_WR_LAMBDA_S1", "O2_WR_LAMBDA_S5")
    tuning_profile.configure_default(_default(dict.fromkeys(channels, 1.5)))
    bus = ScriptedEcu(
        clock, latency=TRUCK_LATENCY_S, values=_payloads(channels), silent_pids=(0x28,)
    )
    readings, _ = _run(clock, bus, _small_spec(*channels), 500.0)
    assert _requested(bus, 0x28) == 5
    assert not any(r.pid == "O2_WR_LAMBDA_S5" for r in readings)
    assert Counter(r.pid for r in readings)["O2_WR_LAMBDA_S1"] == pytest.approx(750, abs=2)
    assert MAX_MISS_BACKOFF_SECONDS == 900.0


# ---------------------------------------------------------------------------
# A priority PID that drops out is re-asked every 10 s (A05.8)
# ---------------------------------------------------------------------------


def _request_times(bus: ScriptedEcu, pid: int, *, after: float) -> list[float]:
    """Monotonic instants of Mode 01 requests carrying ``pid`` at or after ``after``."""
    out: list[float] = []
    for at, msg in bus.sent:
        data = bytes(msg.data)
        if at < after:
            continue
        if len(data) >= 3 and data[1] == 0x01 and data[0] < 0x10 and pid in data[2 : 1 + data[0]]:
            out.append(at)
    return out


@pytest.mark.parametrize("batch_max_pids", [6, 1])
def test_a_priority_pid_that_goes_silent_is_re_asked_every_ten_seconds(
    clock: FakeClock, batch_max_pids: int
) -> None:
    """Operator decision 2026-10-13: the priority PIDs' miss back-off is 10 s.

    THROTTLE_POS (priority) and SPEED (not) both answer for 60 s, then both
    go silent while RPM keeps answering. THROTTLE_POS is re-asked every
    ~10 s, SPEED every ~30 s. Under the old flat back-off THROTTLE_POS also
    waited 30 s, which is what put plausibility's fast tier at 33 s (dropout
    threshold 99 s).
    """
    channels = ("RPM", "SPEED", "THROTTLE_POS")
    tuning_profile.configure_default(_default(dict.fromkeys(channels, 1.0)))
    bus = ScriptedEcu(clock, latency=TRUCK_LATENCY_S, values=_payloads(channels))
    silent_from = clock.monotonic() + 60.0

    def drop_out(_msg: object) -> None:
        if clock.monotonic() >= silent_from and not bus.silent_pids:
            bus.silent_pids = frozenset({_pid("SPEED"), _pid("THROTTLE_POS")})

    bus.on_send.append(drop_out)
    spec = dataclasses.replace(
        _small_spec(*channels, batch_max_pids=batch_max_pids),
        priority_channels=("THROTTLE_POS",),
    )
    readings, _ = _run(clock, bus, spec, 240.0)

    def gaps(channel: str) -> list[float]:
        times = _request_times(bus, _pid(channel), after=silent_from + 1.0)
        return [b - a for a, b in itertools.pairwise(times)]

    throttle, speed = gaps("THROTTLE_POS"), gaps("SPEED")
    # Batched: 10 s + the 1 s round = 11 s; single: + the 1 s timeout = 12 s.
    assert len(throttle) >= 13, throttle
    assert all(10.0 <= gap <= 12.5 for gap in throttle), throttle
    # SPEED keeps the ordinary 30 s back-off: 31-32 s.
    assert len(speed) >= 4, speed
    assert all(30.0 <= gap <= 32.5 for gap in speed), speed
    # RPM keeps flowing; unbatched, each silent probe costs RPM its 1 s timeout.
    lost = 0 if batch_max_pids > 1 else len(throttle) + len(speed) + 2
    assert Counter(r.pid for r in readings)["RPM"] >= 236 - lost


# ---------------------------------------------------------------------------
# The committed Xterra default, end to end
# ---------------------------------------------------------------------------


def _xterra_run(
    clock: FakeClock, profile: CaptureProfile, seconds: float, warmup: float
) -> dict[str, tuple[float, float]]:
    """``{channel: (achieved Hz, longest interval s)}`` over ``[warmup, seconds)``.

    The daemon's shape: the whole saved catalog is the request universe and
    ``default`` gates it, under the Pi's collection settings (1 Hz base rate,
    batches of six, 1.0 s timeout, its seven DIDs in session windows). The
    Pi polls ``auto``; the explicit list is the same selection without the
    discovery walk. The ECM is session-gated like the Xterra's: Mode 01 is
    silent while a Mode 22 session is held.
    """
    catalog = _catalog()
    catalog = CaptureProfileCatalog(
        profiles=[profile if p.name == "default" else p for p in catalog.profiles]
    )
    collection = CollectionConfig(
        obd_source="canable",
        obd_pids=tuple(profile.mode01),
        obd_dids=PI_DIDS,
        obd_batch_max_pids=6,
        obd_did_batch_max_dids=1,
        obd_request_timeout_seconds=1.0,
        obd_poll_rate_hz=1,
        storage_path=Path("unused"),
        session_retention_days=30,
        storage_warning_mb=500,
    )
    # The ECU calibration-id read (service 0x21) is answered on the truck and
    # then refreshed rarely; the scripted ECM does not implement 0x21, and a
    # silent read would retry on its short timer and cost a 1 s timeout each.
    spec = dataclasses.replace(
        CanObdSpec.from_config(collection, CAN, catalog=catalog.profiles), calibration_id=None
    )
    tuning_profile.configure_default(profile)
    tuning_profile.configure_did_aliases(
        did_alias_table(profile_did_union(collection, catalog.profiles))
    )
    bus = ScriptedEcu(
        clock,
        latency=TRUCK_LATENCY_S,
        session_gated=True,
        values=_payloads(profile.mode01),
        mode06_support={0x00: support_bits(0x01)},
        mode06_values={0x01: bytes([0x01, 0x0A, 0, 0, 0, 0, 0xFF, 0xFF])},
        dtc_responses={0x03: b"\x00", 0x07: b"\x00", 0x0A: b"\x00"},
    )
    readings, wall_start = _run(clock, bus, spec, seconds)
    stamps: dict[str, list[float]] = {}
    for reading in readings:
        stamps.setdefault(reading.pid, []).append(reading.timestamp - wall_start)
    window = seconds - warmup
    out: dict[str, tuple[float, float]] = {}
    for channel, times in stamps.items():
        inside = [t for t in times if t >= warmup]
        gaps = [later - earlier for earlier, later in zip(inside, inside[1:], strict=False)]
        out[channel] = (len(inside) / window, max(gaps, default=float("inf")))
    return out


@pytest.mark.slow
def test_the_xterra_default_core_set_achieves_one_hz_and_nothing_else_slows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten simulated minutes with nine Mode 22 windows (plus the warm-up's).

    Achieved means samples inside a fixed window over the window's length.
    The window is set by the simulation, never by the first or last sample.
    Every core channel must reach 1.0 Hz, with no gap as long as 2 s. Every
    other channel must match what the same profile gets with the core set at
    weight 1.0 (the committed 2026-09-26 rate).
    """
    committed = next(p for p in _catalog().profiles if p.name == "default")
    baseline = committed.model_copy(
        update={"mode01": {k: (1.0 if k in CORE else v) for k, v in committed.mode01.items()}}
    )
    seconds, warmup = 600.0, 60.0
    runs = {}
    for label, profile in (("baseline", baseline), ("committed", committed)):
        clock = FakeClock()
        install_clock(monkeypatch, clock)
        runs[label] = _xterra_run(clock, profile, seconds, warmup)
    achieved = runs["committed"]

    short = {c: achieved.get(c) for c in CORE if achieved.get(c, (0.0, 0.0))[0] < 1.0}
    assert not short, f"core channels under 1 Hz: {short}"
    long_gaps = {c: achieved[c][1] for c in CORE if achieved[c][1] >= 2.0}
    assert not long_gaps, f"core channels with a gap of 2 s or more: {long_gaps}"

    others = set(runs["baseline"]) - set(CORE)
    assert {"COOLANT_TEMP", "FUEL_LEVEL", "DID_1226", "DTC_STORED"} <= others
    window = seconds - warmup
    slower = {
        c: (runs["baseline"][c][0], achieved.get(c, (0.0, 0.0))[0])
        for c in others
        # One sample of slack: a phase shift can move one read across the edge.
        if achieved.get(c, (0.0, 0.0))[0] * window < runs["baseline"][c][0] * window - 1
    }
    assert not slower, f"(baseline Hz, committed Hz) for channels that slowed: {slower}"
