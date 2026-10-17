"""A session window's memory reads go last and back to back (B08.4, operator 2026-10-08).

B08.4's timing-lookup coordinate is three service-0x23 reads (the rpm word,
BFS-X and the HI DET latch; hundreds of bytes apart, past the ECM's 63-byte
read). Paced one budget slot apart at the 8 req/s serving budget they spanned
0.5 s, and 0.75-1.3 s once Mode 01 rpm/load/timing joined, against the
lookup's 0.25 s skew. The approved fix (option b) keeps the window's request
count and span and changes only where its memory reads sit: last, back to back,
with the exit straight after, so Mode 01 resumes on the slot it always had.

Every assertion reads the fake clock's sent frames or the readings'
acquisition times, never a count over wall-clock.
"""

from __future__ import annotations

import dataclasses
import threading
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import can
import pytest
import yaml

from src.collection import bus_budget, can_obd_loop, tuning_profile, tx_gate
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.capture_profiles import CaptureProfile, CaptureProfileCatalog
from src.collection.obd_collector import ObdReading
from src.collection.obd_registry import MODE01_REGISTRY
from src.collection.obd_scheduler import (
    CanObdSpec,
    DidSessionSpec,
    did_alias_table,
    preview_polling_plan,
    profile_did_union,
)
from src.config import CanBusConfig, CollectionConfig
from src.decode.dids import MEMORY_SOURCE, select_memory_channels
from tests.capture_round_fakes import FakeClock, ScriptedEcu, install_clock, support_bits

#: ``poll_rate_hz`` 1 derives the 8 req/s the Pi serves at.
BUDGET_RPS = 8.0
SLOT = 1.0 / BUDGET_RPS
LATENCY = 0.010
#: The coordinate tuple plus final timing: the spark window (timing + rpm
#: word), the fuel_axis window (BFS-X) and flags_2841 (HI DET): three reads.
MEMORY = (
    "ECM_IGNITION_TIMING_RAM",
    "ECM_ENGINE_SPEED_RAM",
    "ECM_FUEL_TARGET_MAP_X_TRACE",
    "ECM_HIGH_DET_FLAGS",
)
DIDS = ("ECM_RPM", "ECM_AIR_CHARGE", "ECM_IGNITION_TIMING")
MODE01 = ("RPM", "ENGINE_LOAD", "TIMING_ADVANCE")
LOOKUP_SKEW_S = 0.25
CYCLE_S = 4.0
ENTRY = bytes.fromhex("0210c0")
EXIT = bytes.fromhex("021081")


def _profile(**update: object) -> CaptureProfile:
    fields: dict[str, object] = {
        "name": "burst",
        "mode01": dict.fromkeys(MODE01, 1.0),
        "mode22": DIDS,
        "memory": MEMORY,
        "did_cycle_seconds": CYCLE_S,
        "storage_prefix": "_capture-burst",
    }
    fields.update(update)
    return CaptureProfile.model_validate(fields)


def _spec(*, window_max_seconds: float = 10.0, keepalive_seconds: float = 2.0) -> CanObdSpec:
    return CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="burst", bitrate=500000),
        poll_rate_hz=1.0,
        pids=MODE01,
        dids=DIDS,
        batch_max_pids=6,
        priority_channels=(),
        dtc_poll_interval_seconds=0.0,
        service_poll_interval_seconds=0.0,
        request_timeout_seconds=1.0,
        capture_profiles_enabled=True,
        memory=select_memory_channels(MEMORY),
        did_session=DidSessionSpec(
            session=0xC0,
            exit_session=0x81,
            exit_grace_seconds=0.05,
            keepalive_seconds=keepalive_seconds,
            window_interval_seconds=CYCLE_S,
            window_max_seconds=window_max_seconds,
            lapse_seconds=5.0,
        ),
    )


#: Every ScriptedEcu a test built, newest last (for its lapse count).
_RUNS: list[ScriptedEcu] = []


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock(monotonic=1000.0)
    install_clock(monkeypatch, fake)
    return fake


@pytest.fixture(autouse=True)
def runtime_reset() -> Iterable[None]:
    yield None
    tuning_profile.set_paused(False)
    tuning_profile.configure_did_aliases({})
    tx_gate.set_passive(False, source="test")
    tuning_profile.set_active(False, source="test")


def _run(
    clock: FakeClock,
    profile: CaptureProfile,
    seconds: float,
    *,
    ecu: dict[str, Any] | None = None,
    unseen_memory: bool = False,
    **spec: float,
) -> tuple[list[tuple[float, bytes]], list[ObdReading]]:
    """Requests sent (flow control left out) and readings, on the fake clock.

    ``unseen_memory``: every 0x23 request is lost before the ECM: no answer,
    and the session's S3 timer is not refreshed by it."""
    tuning_profile.configure_did_aliases(did_alias_table(DIDS))
    tuning_profile.activate(profile, source="test")
    bus = ScriptedEcu(clock, latency=LATENCY, session_gated=True, **(ecu or {}))
    if unseen_memory:
        deliver = bus.send

        def send(msg: can.Message, timeout: float | None = None) -> None:
            if len(msg.data) >= 2 and msg.data[1] == 0x23:
                bus.sent.append((clock.monotonic(), msg))
                return
            deliver(msg, timeout)

        bus.send = send  # type: ignore[method-assign]
    _RUNS.append(bus)
    readings: list[ObdReading] = []
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(**spec),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + seconds,
    )
    # Requests only: a flow-control frame is the tail of the read before it.
    requests = [(at, bytes(msg.data)) for at, msg in bus.sent if msg.data[0] >> 4 != 0x3]
    return requests, readings


def _service(data: bytes) -> int:
    return data[1]


def _windows(
    requests: list[tuple[float, bytes]],
) -> list[tuple[list[tuple[float, bytes]], float | None]]:
    """Each complete window's requests from its entry to its exit inclusive,
    and the time of the first Mode 01 request after the exit."""
    out = []
    start: int | None = None
    for i, (_at, data) in enumerate(requests):
        if data.startswith(ENTRY) and start is None:
            start = i
        elif data.startswith(EXIT) and start is not None:
            after = next((at for at, d in requests[i + 1 :] if _service(d) == 0x01), None)
            out.append((requests[start : i + 1], after))
            start = None
    return out


def test_memory_reads_go_last_back_to_back_and_the_exit_follows(clock: FakeClock) -> None:
    requests, _readings = _run(clock, _profile(), seconds=30.0)
    windows = _windows(requests)
    assert len(windows) >= 5
    for window, mode01_after in windows:
        services = [_service(d) for _at, d in window]
        memory = [at for at, d in window if _service(d) == 0x23]
        dids = [at for at, d in window if _service(d) == 0x22]
        assert len(memory) == 3 and len(dids) == len(DIDS), services
        # Last: every DID before the first memory read.
        assert max(dids) < min(memory)
        # Back to back: one read's turnaround apart, never a budget slot.
        gaps = [b - a for a, b in zip(memory, memory[1:], strict=False)]
        assert max(gaps) < SLOT / 4, gaps
        exit_at = window[-1][0]
        assert exit_at - memory[-1] < SLOT / 4
        # The window's N requests still take the N - 1 slots pacing gave them,
        # so the window's average rate is the budget's and the Mode 01 request
        # after it waits as long as it did when every read was paced.
        assert mode01_after is not None
        span = mode01_after - window[0][0]
        assert span >= (len(window) - 1) * SLOT - 1e-9, (len(window), span)


def test_the_coordinate_and_its_mode01_leg_meet_the_lookup_skew_at_the_serving_budget(
    clock: FakeClock,
) -> None:
    """Acquisition times, paired per window: the three coordinate reads, and
    the nearest RPM / ENGINE_LOAD / TIMING_ADVANCE reads joined to them."""
    _requests, readings = _run(clock, _profile(), seconds=30.0)
    stamps: dict[str, list[float]] = {}
    for reading in readings:
        at = reading.acquired_at if reading.acquired_at is not None else reading.timestamp
        stamps.setdefault(reading.pid, []).append(float(at))
    tuple_channels = [
        f"{MEMORY_SOURCE}:{name}"
        for name in ("ECM_ENGINE_SPEED_RAM", "ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_HIGH_DET_FLAGS")
    ]
    anchors = stamps[tuple_channels[0]]
    assert len(anchors) >= 5
    for anchor in anchors:
        reads = [min(stamps[c], key=lambda t, a=anchor: abs(t - a)) for c in tuple_channels]
        lo, hi = min(reads), max(reads)
        assert hi - lo <= LOOKUP_SKEW_S / 5, reads
        joined = [min(stamps[p], key=lambda t, a=anchor: abs(t - a)) for p in MODE01]
        assert max(hi, *joined) - min(lo, *joined) <= LOOKUP_SKEW_S, (reads, joined)
        # Final timing rides the rpm word's read: the same acquisition.
        timing = min(
            stamps[f"{MEMORY_SOURCE}:ECM_IGNITION_TIMING_RAM"], key=lambda t: abs(t - anchor)
        )
        assert timing == anchor


def test_a_profile_without_memory_reads_is_paced_as_before(clock: FakeClock) -> None:
    requests, _readings = _run(clock, _profile(memory=()), seconds=30.0)
    dids = [at for at, d in requests if _service(d) == 0x22]
    assert len(dids) >= 5 * len(DIDS)
    gaps = [b - a for a, b in zip(dids, dids[1:], strict=False)]
    assert min(gaps) >= SLOT - 1e-9


def test_a_continuous_window_paces_its_memory_reads_as_before(clock: FakeClock) -> None:
    """A continuous window never closes, so it has no exit to sit against."""
    profile = _profile(mode01={}, mode01_enabled=False, did_continuous=True, did_cycle_seconds=None)
    requests, _readings = _run(clock, profile, seconds=10.0)
    memory = [at for at, d in requests if _service(d) == 0x23]
    assert len(memory) >= 6
    gaps = [b - a for a, b in zip(memory, memory[1:], strict=False)]
    assert min(gaps) >= SLOT - 1e-9


def test_a_window_whose_deadline_falls_in_the_wait_reads_its_memory_at_once(
    clock: FakeClock,
) -> None:
    """Three DIDs and three prepaid slots need 0.75 s past the entry; a
    0.6 s window would close first and lose every memory read. It reads them
    at once instead, back to back, and is charged per read as before."""
    requests, _readings = _run(clock, _profile(), seconds=30.0, window_max_seconds=0.6)
    windows = _windows(requests)
    assert len(windows) >= 5
    for window, _mode01_after in windows:
        memory = [at for at, d in window if _service(d) == 0x23]
        assert len(memory) == 3
        gaps = [b - a for a, b in zip(memory, memory[1:], strict=False)]
        assert max(gaps) < SLOT / 4


def test_a_keepalive_due_inside_the_burst_waits_for_it(clock: FakeClock) -> None:
    """On the 2026-10-04 ram-study profile a keep-alive came due while the
    burst ran: sent there, it split the reads and spent a slot nothing had
    paid for, delaying the exit by one. Due after pacing's last read slot,
    it is not sent at all, as pacing did not send it: the exit comes first."""
    requests, _readings = _run(clock, _profile(), seconds=6.0)
    window, _after = _windows(requests)[0]
    entry = window[0][0]
    first_read = min(at for at, d in window if _service(d) == 0x23)
    tuning_profile.set_active(False, source="test")

    # The same run again, on a fresh clock, with the keep-alive due 5 ms
    # after the first window's first memory read.
    with pytest.MonkeyPatch.context() as patch:
        fresh = FakeClock(monotonic=1000.0)
        install_clock(patch, fresh)
        due_inside = first_read - entry + 0.005
        requests, _readings = _run(fresh, _profile(), seconds=30.0, keepalive_seconds=due_inside)
    windows = _windows(requests)
    assert len(windows) >= 5
    for window, _mode01_after in windows:
        memory = [i for i, (_at, d) in enumerate(window) if _service(d) == 0x23]
        inside = [d for _at, d in window[memory[0] : memory[-1] + 1] if _service(d) == 0x10]
        assert not inside, [d.hex() for _at, d in window]
        times = [window[i][0] for i in memory]
        assert max(b - a for a, b in zip(times, times[1:], strict=False)) < SLOT / 4


def test_moving_the_reads_to_the_end_does_not_move_the_next_window(clock: FakeClock) -> None:
    """Under the end-to-start interval the next window is due an interval
    after the window closes. Closed by its burst, the window would close up
    to a paced read span later than pacing closed it, and every later window
    with it (0.18 s per 60 s window for the B08 additions to ``default``).
    It is scheduled from the paced close instead, which is at least one
    slot's wait before the exit."""
    requests, _readings = _run(clock, _profile(did_cycle_seconds=None), seconds=30.0)
    entries = [at for at, d in requests if d.startswith(ENTRY)]
    exits = [at for at, d in requests if d.startswith(EXIT)][1:]  # the first is the reconnect exit
    assert len(entries) >= 5
    for exit_at, next_entry in zip(exits, entries[1:], strict=False):
        assert next_entry - exit_at <= CYCLE_S - SLOT / 2, (exit_at, next_entry)


def _span_ok(window: list[tuple[float, bytes]], mode01_after: float | None) -> float:
    """Entry to the next Mode 01 request: N paced requests take between N-1
    and N slots (the gap after the entry varies with the slot grid). A wait
    paid twice adds the whole wait, several slots."""
    assert mode01_after is not None
    span = mode01_after - window[0][0]
    assert (len(window) - 1) * SLOT - 1e-9 <= span <= len(window) * SLOT, (
        len(window),
        span,
    )
    return span


def test_a_keepalive_in_the_wait_is_charged_once(clock: FakeClock) -> None:
    """A keep-alive due while the window waits for its burst spends its own
    slot, and the burst is charged from its plan: the idle wait is not paid a
    second time. Charged from the clock the keep-alive had advanced, the
    window grew by the whole wait (`tuning` at 8 req/s: 1.875-2.375 s spans
    became 3.145 s and 294 windows 210 in 900 s)."""
    requests, _readings = _run(clock, _profile(), seconds=30.0)
    reference = _windows(requests)
    tuning_profile.set_active(False, source="test")
    with pytest.MonkeyPatch.context() as patch:
        fresh = FakeClock(monotonic=1000.0)
        install_clock(patch, fresh)
        # Due 0.5 s after the entry: after the DIDs, before the last read slot.
        requests, _readings = _run(fresh, _profile(), seconds=30.0, keepalive_seconds=0.5)
    windows = _windows(requests)
    assert len(windows) == len(reference) >= 5
    for (window, after), (paced, paced_after) in zip(windows, reference, strict=True):
        assert sum(1 for _at, d in window if _service(d) == 0x10) == 3  # entry, keep-alive, exit
        assert len(window) == len(paced) + 1
        _span_ok(window, after)
        _span_ok(paced, paced_after)
        memory = [at for at, d in window if _service(d) == 0x23]
        assert max(b - a for a, b in zip(memory, memory[1:], strict=False)) < SLOT / 4


def test_unanswered_memory_reads_never_hold_the_keepalive_past_s3(clock: FakeClock) -> None:
    """Three reads lost before the ECM time out at 1.0 s each: a 3 s burst.
    They may not refresh S3 (whether an unanswered 0x23 does is not
    established), so a keep-alive held for the burst let the session lapse
    and Mode 01 fell into the 30 s blackout. It goes as soon as the burst has
    run past its paced end instead."""
    profile = _profile()
    requests, _readings = _run(
        clock,
        profile,
        seconds=40.0,
        ecu={"s3_seconds": 3.0, "lapse_blackout_seconds": 30.0},
        unseen_memory=True,
    )
    bus = _RUNS[-1]
    assert bus.lapses == 0
    windows = _windows(requests)
    assert len(windows) >= 4
    for window, _after in windows:
        session = [at for at, d in window if _service(d) == 0x10]
        # A keep-alive cannot interrupt a request in flight, so the bound is
        # the keep-alive interval plus one request timeout (1.0 s), as with
        # pacing; held for the whole burst it was 3 timeouts.
        assert max(b - a for a, b in zip(session, session[1:], strict=False)) <= 2.0 + 1.0
    rpm = [at for at, d in requests if _service(d) == 0x01]
    assert max(b - a for a, b in zip(rpm, rpm[1:], strict=False)) < 10.0


# ---------------------------------------------------------------------------
# The Xterra `tuning` profile at its own 90 req/s: rates are pacing's
# ---------------------------------------------------------------------------

XTERRA_CATALOG = (
    Path(__file__).resolve().parents[1] / "data" / "nissan-xterra-2008" / ("capture_profiles.yaml")
)


#: The 23 Mode 06 MIDs the Xterra ECM answers, from walking its support
#: groups 0x00-0xA0 (data/analyses/2026-07-28T0330-ecu-address-space.md
#: "Mode 06"; the C06.12 hardware walk found the same 23 in 6 probes,
#: docs/diagnostic-discovery.md). Each answers one test record (TID, UAS,
#: value, min, max), the shape `test_default_profile_core_rate` uses.
XTERRA_MODE06_MIDS = (
    0x01, 0x02, 0x05, 0x06, 0x21, 0x22, 0x39, 0x3B, 0x3C, 0x3D, 0x41, 0x42,
    0x45, 0x46, 0x81, 0x82, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7,
)  # fmt: skip
XTERRA_MODE06: dict[str, Any] = {
    "mode06_support": {
        group: support_bits(
            *(mid for mid in XTERRA_MODE06_MIDS if group < mid <= group + 0x20),
            base=group,
            more=group < 0xA0,
        )
        for group in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0)
    },
    "mode06_values": {
        mid: bytes([0x01, 0x0A, 0, 0, 0, 0, 0xFF, 0xFF]) for mid in XTERRA_MODE06_MIDS
    },
}


def _tuning_counts(monkeypatch: pytest.MonkeyPatch, seconds: float, *, paced: bool) -> Counter[str]:
    """Readings per channel for the tracked `tuning` profile at its 90 req/s
    override, under the Pi's collection settings. ``paced`` turns the burst
    off (``_memory_burst`` answers ``None``): HEAD's scheduler."""
    if paced:
        monkeypatch.setattr(can_obd_loop._CanObdLoop, "_memory_burst", lambda *_a: None)
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    catalog = CaptureProfileCatalog.model_validate(yaml.safe_load(XTERRA_CATALOG.read_text()))
    default = catalog.by_name("default")
    tuning = catalog.by_name("tuning")
    assert default is not None and tuning is not None and tuning.memory
    collection = CollectionConfig(
        obd_source="canable",
        obd_pids=tuple(default.mode01),
        obd_dids=tuple(default.mode22),
        obd_batch_max_pids=6,
        obd_did_batch_max_dids=1,
        obd_request_timeout_seconds=1.0,
        obd_poll_rate_hz=1,
        storage_path=Path("unused"),
        session_retention_days=30,
        storage_warning_mb=500,
    )
    spec = dataclasses.replace(
        CanObdSpec.from_config(
            collection,
            CanBusConfig(interface="virtual", channel="tuning", bitrate=500000),
            catalog=catalog.profiles,
        ),
        calibration_id=None,
    )
    tuning_profile.configure_default(default)
    tuning_profile.configure_did_aliases(
        did_alias_table(profile_did_union(collection, catalog.profiles))
    )
    tuning_profile.activate(tuning, source="test")
    assert tuning.bus_budget_override == 90.0
    bus_budget.set_override(tuning.bus_budget_override, source="test", ttl_seconds=None)
    values = {pid: bytes([0x40]) * d.byte_length for pid, d in MODE01_REGISTRY.items()}
    values[0x0C] = b"\x0b\xb8"
    # The ECM answers Mode 06 as the truck does (:data:`XTERRA_MODE06`) and the
    # DTC polls with no codes. Left silent, each support probe waited out its
    # 1 s timeout every discovery cycle and starved the loop: `tuning` read RPM
    # 253 times in 300 s, and DIDs 185, where the paced plan gives 1,049 and 600.
    bus = ScriptedEcu(
        clock,
        latency=LATENCY,
        session_gated=True,
        values=values,
        dtc_responses={0x03: b"\x00", 0x07: b"\x00", 0x0A: b"\x00"},
        **XTERRA_MODE06,
    )
    readings: list[ObdReading] = []
    try:
        run_can_obd_loop(
            bus,  # type: ignore[arg-type]
            spec,
            on_frame=lambda _m: None,
            on_reading=readings.append,
            stop_event=threading.Event(),
            deadline_monotonic=clock.monotonic() + seconds,
        )
    finally:
        bus_budget.reset()
        tuning_profile.set_active(False, source="test")
    return Counter(r.pid for r in readings)


#: Run length, and how far a channel's count may sit from its planned rate:
#: the one read a run's first or last partial window adds or drops, as a
#: fraction of a 0.5 Hz channel's 150 reads.
TUNING_SECONDS = 300.0
RATE_TOLERANCE = 0.01


def _planned_hz() -> dict[str, float]:
    """Planned reads/s per decoded channel, from the capture preview: the
    scheduler's own plan for `tuning`, not a figure written here."""
    catalog = CaptureProfileCatalog.model_validate(yaml.safe_load(XTERRA_CATALOG.read_text()))
    default, tuning = catalog.by_name("default"), catalog.by_name("tuning")
    assert default is not None and tuning is not None
    collection = CollectionConfig(
        obd_source="canable",
        obd_pids=tuple(default.mode01),
        obd_dids=tuple(default.mode22),
        obd_batch_max_pids=6,
        obd_poll_rate_hz=1,
        storage_path=Path("unused"),
        session_retention_days=30,
        storage_warning_mb=500,
    )
    planned: dict[str, float] = {}
    for member in preview_polling_plan(collection, tuning)["members"]:
        if member["kind"] not in {"pid", "did", "memory"}:
            continue
        for channel in member["decoded_channels"] or [member["channel"]]:
            planned[channel] = float(member["rate_hz"])
    return planned


@pytest.mark.slow
def test_tuning_at_90_req_s_keeps_every_channel_at_its_paced_rate() -> None:
    """Verification (2026-10-08) found commanded phi and FUEL_STATUS 5.5 %
    low in `tuning` over 900 s at 90 req/s (2,752 -> 2,614), with the core
    Mode 01 channels high. The cause was where the window's exit landed: the
    burst sent it when the burst ended rather than when pacing sent it, and
    at 90 req/s pacing runs on each read's turnaround (a multi-frame read
    takes longer than the 11 ms slot), not on slots. Mode 01 resumed at a
    different point of the 0.5 s cycle, so a core-only round took the place
    of the second batch that carries 0x44 and 0x03. The burst now ends where
    pacing's last read ended (each read planned at the longer of its slot
    and its measured turnaround), and the exit goes there. Every channel
    keeps pacing's count over 300 s, give or take the one read a run's last
    partial window may add or drop.

    Both runs are held to the plan, not only to each other (E09.1
    verification, 2026-10-16: with Mode 06 unanswered both were starved
    alike, and agreed). Every DID and memory channel, and every Mode 01 PID
    planned at 2 Hz or less, runs at its planned rate. A Mode 01 PID planned
    at 4 Hz runs at 2 Hz or more and never above its plan: Mode 01 and Mode 22
    are exclusive here, the window (nine DIDs, eight or nine memory reads)
    and its exit grace take 0.28 s of each 0.5 s cycle, and two uniform
    0.25 s rounds do not fit in the 0.22 s left. One does (2.0 Hz: 0x44 and FUEL_STATUS ride
    its later batches); the core batch also gets the round falling just before
    the next entry three cycles in four (3.5 Hz). Which of the two a profile
    gets is a scheduling choice not yet made (E09.1 report).
    """
    with pytest.MonkeyPatch.context() as patch:
        paced = _tuning_counts(patch, TUNING_SECONDS, paced=True)
    with pytest.MonkeyPatch.context() as patch:
        burst = _tuning_counts(patch, TUNING_SECONDS, paced=False)
    off = {
        channel: (paced[channel], burst[channel])
        for channel in set(paced) | set(burst)
        if abs(paced[channel] - burst[channel]) > 1
    }
    assert not off, off
    one_round_per_cycle_hz = 2.0
    for channel, planned in _planned_hz().items():
        for label, counts in (("paced", paced), ("burst", burst)):
            hz = counts[channel] / TUNING_SECONDS
            in_window = channel.startswith(("DID_", f"{MEMORY_SOURCE}:"))
            if in_window or planned <= one_round_per_cycle_hz:
                assert hz == pytest.approx(planned, rel=RATE_TOLERANCE), (label, channel, hz)
            else:
                lo = one_round_per_cycle_hz * (1 - RATE_TOLERANCE)
                assert lo <= hz <= planned * (1 + RATE_TOLERANCE), (label, channel, hz)


def _switch_rates(
    switch_at: float, *, budget: float, latency: float, span: float = 40.0
) -> dict[str, float]:
    """Reads/s per channel for `tuning` armed by a profile switch ``switch_at``
    seconds into a `default` loop, the way the Pi starts a capture.

    Measured from each channel's own reads over ``span`` seconds starting 2 s
    after the switch, as (reads - 1) / (last - first): a window that skips a
    read moves it, where the edges of the span do not."""
    catalog = CaptureProfileCatalog.model_validate(yaml.safe_load(XTERRA_CATALOG.read_text()))
    default, tuning = catalog.by_name("default"), catalog.by_name("tuning")
    assert default is not None and tuning is not None
    collection = CollectionConfig(
        obd_source="canable",
        obd_pids=tuple(default.mode01),
        obd_dids=tuple(default.mode22),
        obd_batch_max_pids=6,
        obd_did_batch_max_dids=1,
        obd_request_timeout_seconds=1.0,
        obd_poll_rate_hz=1,
        storage_path=Path("unused"),
        session_retention_days=30,
        storage_warning_mb=500,
    )
    with pytest.MonkeyPatch.context() as patch:
        clock = FakeClock()
        install_clock(patch, clock)
        spec = dataclasses.replace(
            CanObdSpec.from_config(
                collection,
                CanBusConfig(interface="virtual", channel="switch", bitrate=500000),
                catalog=catalog.profiles,
            ),
            calibration_id=None,
        )
        tuning_profile.configure_default(default)
        tuning_profile.configure_did_aliases(
            did_alias_table(profile_did_union(collection, catalog.profiles))
        )
        tuning_profile.activate(default, source="test")
        values = {pid: bytes([0x40]) * d.byte_length for pid, d in MODE01_REGISTRY.items()}
        values[0x0C] = b"\x0b\xb8"
        bus = ScriptedEcu(
            clock,
            latency=latency,
            session_gated=True,
            values=values,
            dtc_responses={0x03: b"\x00", 0x07: b"\x00", 0x0A: b"\x00"},
            **XTERRA_MODE06,
        )
        start, wall = clock.monotonic(), clock.time()
        armed: list[float] = []

        def switch(_msg: can.Message) -> None:
            if not armed and clock.monotonic() - start >= switch_at:
                armed.append(clock.time() - wall)
                tuning_profile.activate(tuning, source="test")
                bus_budget.set_override(budget, source="test", ttl_seconds=None)

        bus.on_send.append(switch)
        readings: list[ObdReading] = []
        try:
            run_can_obd_loop(
                bus,  # type: ignore[arg-type]
                spec,
                on_frame=lambda _m: None,
                on_reading=readings.append,
                stop_event=threading.Event(),
                deadline_monotonic=start + switch_at + span + 3.0,
            )
        finally:
            bus_budget.reset()
            tuning_profile.set_active(False, source="test")
    lo = armed[0] + 2.0
    stamps: dict[str, list[float]] = {}
    for reading in readings:
        at = float(reading.acquired_at or reading.timestamp) - wall
        if lo <= at < lo + span:
            stamps.setdefault(reading.pid, []).append(at)
    return {
        channel: (len(times) - 1) / (max(times) - min(times))
        for channel, times in stamps.items()
        if len(times) > 1
    }


#: Switch instants across one 0.5 s `tuning` cycle (E09.1 verification,
#: 2026-10-16): DIDs fell to half rate for the whole run when a member's grid
#: phase was carried over from before the switch and landed just after the
#: window entry, which only some instants of the cycle produce.
SWITCH_SCAN = tuple(round(10.0 + 0.02 * i, 2) for i in range(26))


#: A Mode 01 round lands anywhere in the Mode 01 share of a 0.5 s cycle, so a
#: rate taken between a channel's first and last read over the 40 s span is
#: good to one share in 40 s, about 1.3 % (HEAD's 1 Hz context PIDs read
#: 1.012-1.013 there). Window members sit on the window grid and hold 1 %.
SWITCH_MODE01_TOLERANCE = 0.02

#: (budget req/s, answer latency s, switch instant s).
SWITCH_CASES = (
    *((90.0, LATENCY, at) for at in SWITCH_SCAN),
    *((116.0, LATENCY, at) for at in SWITCH_SCAN[::5]),
    *((90.0, 0.0015, at) for at in SWITCH_SCAN[::5]),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("budget", "latency", "switch_at"),
    SWITCH_CASES,
    ids=[f"{b:g}rps-{lat * 1000:g}ms-at{at:.2f}" for b, lat, at in SWITCH_CASES],
)
def test_a_capture_started_by_a_switch_runs_at_its_plan_wherever_the_switch_falls(
    budget: float, latency: float, switch_at: float
) -> None:
    """Every DID and memory channel within 1 % of its plan at every switch
    instant, a Mode 01 PID planned at 2 Hz or less within 2 %, and a 4 Hz Mode
    01 PID between one round per cycle (2 Hz) and its plan, for the reason the
    test above gives. At 116 req/s (the measured ceiling, 2026-09-10) and at
    1.5 ms answers as well as the 10 ms the other tests use.

    On the fake clock, with the ECM answering Mode 06 as the truck does, the
    previous scheduler (495936b0) failed here because a window member's due
    time from before the switch kept its grid phase: at 10 ms answers two
    DIDs ran at exactly half rate for the whole run; at 1.5 ms they ran at
    0.75-0.87 of plan (90 req/s) or passed (116 req/s). With Mode 06 refused
    it passed. Members now restart on the switch's grid
    (``replan_for_generation``). No stored tuning capture ran post-D03.34
    scheduler code, so whether the truck saw this is unconfirmed."""
    rates = _switch_rates(switch_at, budget=budget, latency=latency)
    off: dict[str, float] = {}
    for channel, hz in _planned_hz().items():
        got = rates.get(channel, 0.0)
        in_window = channel.startswith(("DID_", f"{MEMORY_SOURCE}:"))
        if in_window:
            ok = got == pytest.approx(hz, rel=RATE_TOLERANCE)
        elif hz <= 2.0:
            ok = got == pytest.approx(hz, rel=SWITCH_MODE01_TOLERANCE)
        else:
            ok = 2.0 * (1 - RATE_TOLERANCE) <= got <= hz * (1 + RATE_TOLERANCE)
        if not ok:
            off[channel] = round(got / hz, 3)
    assert not off, off
