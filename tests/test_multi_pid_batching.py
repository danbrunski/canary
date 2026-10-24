"""Multi-PID Mode 01 batching — roadmap/archive/multi-pid-polling.md Phase 1.

The behaviours worth pinning are the ones that make a batch *safe* to trust.
A batched read that silently misattributes values is far worse than no
batching at all: every downstream feature, surface and model would be fed
plausible numbers off the wrong channel.
"""

from __future__ import annotations

from typing import Any

import can
import pytest

from src.collection import tx_gate
from src.collection.can_obd_client import BatchedMode01Response, CanObdClient
from src.collection.isotp import (
    MAX_BATCHED_PIDS,
    build_batched_request_payload,
    split_batched_response,
)
from tests.fakes import CannedResponseBus

# Widths for the PIDs used below, matching the SAE J1979 payload sizes.
WIDTHS = {0x0C: 2, 0x0D: 1, 0x04: 1, 0x11: 1, 0x0E: 1, 0x10: 2, 0x05: 1}


def width_for(pid: int) -> int | None:
    return WIDTHS.get(pid)


# ---------------------------------------------------------------------------
# 1.1 — building the request
# ---------------------------------------------------------------------------


def test_batched_request_matches_the_frame_the_ecu_answered() -> None:
    """Byte-for-byte the request verified against the truck on 2026-07-26."""
    payload = build_batched_request_payload(0x01, [0x0C, 0x0D, 0x04, 0x11, 0x0E, 0x10])

    assert payload.hex().upper() == "07010C0D04110E10"
    assert len(payload) == 8


def test_batched_request_is_always_padded_to_eight_bytes() -> None:
    """Not cosmetic: this ECM ignores short frames with no response at all,
    which is indistinguishable from an unsupported service."""
    for count in range(1, MAX_BATCHED_PIDS + 1):
        payload = build_batched_request_payload(0x01, list(range(0x01, 0x01 + count)))
        assert len(payload) == 8, f"{count} PIDs produced {len(payload)} bytes"


def test_batched_request_rejects_more_than_the_j1979_ceiling() -> None:
    with pytest.raises(ValueError, match="at most 6"):
        build_batched_request_payload(0x01, [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07])


def test_batched_request_rejects_duplicates() -> None:
    """A duplicate makes the response ambiguous to walk and wastes a slot."""
    with pytest.raises(ValueError, match="duplicate"):
        build_batched_request_payload(0x01, [0x0C, 0x0D, 0x0C])


def test_batched_request_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one"):
        build_batched_request_payload(0x01, [])


# ---------------------------------------------------------------------------
# 1.2 — splitting the response
# ---------------------------------------------------------------------------


def test_split_reproduces_the_real_truck_response() -> None:
    """The exact payload the ECM returned at KOEO, minus the 41 echo.

    Note 0x0E comes back as FF — the engine-off 'not available' sentinel,
    which is a nice incidental confirmation that batching sees the same
    values the single-PID path does.
    """
    body = bytes.fromhex("0C00000D00040011130EFF100076")

    readings, clean = split_batched_response(body, width_for)

    assert clean is True
    assert readings == {
        0x0C: b"\x00\x00",
        0x0D: b"\x00",
        0x04: b"\x00",
        0x11: b"\x13",
        0x0E: b"\xff",
        0x10: b"\x00\x76",
    }


def test_split_handles_an_omitted_pid_without_desyncing() -> None:
    """Phase 1.3. An ECU may omit an unsupported PID rather than rejecting the
    whole batch. Because the walk keys off the PIDs *present*, the surviving
    values stay correctly attributed — a positional match against the request
    would have shifted every later value onto the wrong channel."""
    # Asked for 0C, 0D, 04 — the ECU answered without 0D.
    body = bytes.fromhex("0C0BB80419")

    readings, clean = split_batched_response(body, width_for)

    assert clean is True
    assert readings == {0x0C: b"\x0b\xb8", 0x04: b"\x19"}
    assert 0x0D not in readings


def test_split_stops_cleanly_on_an_unknown_pid() -> None:
    """An unknown PID has unknown width, so the next boundary is unknowable.
    Stopping is the only safe move; everything before it is still good."""
    body = bytes.fromhex("0C0BB8AA000419")

    readings, clean = split_batched_response(body, width_for)

    assert clean is False
    assert readings == {0x0C: b"\x0b\xb8"}


def test_split_stops_on_a_truncated_payload() -> None:
    """0x0C claims two bytes but only one is present."""
    body = bytes.fromhex("0C0B")

    readings, clean = split_batched_response(body, width_for)

    assert clean is False
    assert readings == {}


def test_split_of_empty_payload_is_clean_and_empty() -> None:
    assert split_batched_response(b"", width_for) == ({}, True)


# ---------------------------------------------------------------------------
# 1.5 — the client method
# ---------------------------------------------------------------------------


def test_client_batch_reassembles_a_multi_frame_response() -> None:
    """A 6-PID response is 15 bytes, so it arrives as first-frame plus
    consecutive frames and exercises the flow-control path."""
    # 41 0C 0000 0D 00 04 00 11 13 0E FF 10 0076  == 15 bytes
    first = bytes([0x10, 0x0F, 0x41, 0x0C, 0x00, 0x00, 0x0D, 0x00])
    cf1 = bytes([0x21, 0x04, 0x00, 0x11, 0x13, 0x0E, 0xFF, 0x10])
    cf2 = bytes([0x22, 0x00, 0x76, 0x00, 0x00, 0x00, 0x00, 0x00])
    bus = CannedResponseBus([first, cf1, cf2])
    client = CanObdClient(bus, timeout_seconds=0.5)  # type: ignore[arg-type]

    got = client.query_mode_01_batch(
        [0x0C, 0x0D, 0x04, 0x11, 0x0E, 0x10], byte_length_for=width_for
    )

    assert isinstance(got, BatchedMode01Response)
    assert got.clean is True
    assert got.readings[0x0C] == b"\x00\x00"
    assert got.readings[0x10] == b"\x00\x76"
    # request frame, then the flow-control CTS
    assert bus.sent[0].data.hex().upper() == "07010C0D04110E10"


def test_client_batch_returns_none_on_negative_response() -> None:
    """Phase 1.4. A 7F 01 <nrc> applies to the whole batch. The caller must be
    able to tell that apart from a successful read, so it falls back rather
    than crediting six channels that never answered."""
    bus = CannedResponseBus([bytes([0x03, 0x7F, 0x01, 0x12, 0x00, 0x00, 0x00, 0x00])])
    client = CanObdClient(bus, timeout_seconds=0.3)  # type: ignore[arg-type]

    assert client.query_mode_01_batch([0x0C, 0x0D], byte_length_for=width_for) is None


def test_client_batch_returns_none_on_timeout() -> None:
    client = CanObdClient(CannedResponseBus([]), timeout_seconds=0.1)  # type: ignore[arg-type]

    assert client.query_mode_01_batch([0x0C], byte_length_for=width_for) is None


def test_client_batch_transmits_nothing_in_passive_mode() -> None:
    """Passive mode is a safety gate; batching must not sneak past it."""
    bus = CannedResponseBus([])
    client = CanObdClient(bus, timeout_seconds=0.1)  # type: ignore[arg-type]
    tx_gate.set_passive(True, source="test")
    try:
        assert client.query_mode_01_batch([0x0C], byte_length_for=width_for) is None
        assert bus.sent == []
    finally:
        tx_gate.set_passive(False, source="test")


# ---------------------------------------------------------------------------
# Phase 2 — scheduler integration
# ---------------------------------------------------------------------------

import threading  # noqa: E402

from src.collection.can_obd_collector import (  # noqa: E402
    MAX_CONSECUTIVE_BATCH_FAILURES,
    run_can_obd_loop,
)
from src.collection.obd_scheduler import (  # noqa: E402
    CanObdSpec,
    _initial_request_states,
    build_poll_requests,
    is_batchable,
    select_batch_companions,
)
from src.config import CanBusConfig  # noqa: E402
from tests.capture_round_fakes import (  # noqa: E402
    BATCH_REFUSAL_WINDOW_SECONDS,  # noqa: E402
    DEFAULT_VALUES,
    FakeClock,
    ScriptedEcu,
    install_clock,
)
from tests.waits import StopWhen  # noqa: E402

#: Mode 01 payloads the batching tests answer with: ``DEFAULT_VALUES`` without
#: the fuel trims. 0x2F is absent, so the "ECU omits an unsupported PID" path
#: gets exercised; 0x05/0x0F/0x10 give more than the six-PID ceiling, so a
#: selection can span two requests — the case a capture round has to hold
#: together.
BATCHING_VALUES = {pid: v for pid, v in DEFAULT_VALUES.items() if pid not in (0x06, 0x07)}
#: The fuel-pw-research-2 members that desynced: baro answers with the one
#: byte J1979 gives it, the way the Xterra ECM does.
_BARO_VALUES = {**BATCHING_VALUES, 0x33: b"\x51", 0x44: b"\x80\x00"}
#: ``build_poll_requests`` asks discovery only for ``auto``; these specs name PIDs.
_NO_DISCOVERY = object()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """A :class:`FakeClock` bound as every collection module's ``time`` (T01.8)."""
    fake = FakeClock()
    install_clock(monkeypatch, fake)
    return fake


def _batching_ecu(clock: FakeClock, **kwargs: Any) -> ScriptedEcu:
    """Answers Mode 01, single or batched, from :data:`BATCHING_VALUES`."""
    kwargs.setdefault("values", BATCHING_VALUES)
    return ScriptedEcu(clock, **kwargs)


def _mode01_sizes(bus: ScriptedEcu) -> list[int]:
    return [len(pids) for pids in bus.mode01_requests()]


def _batch_orders(bus: ScriptedEcu) -> list[tuple[int, ...]]:
    return [pids for pids in bus.mode01_requests() if len(pids) > 1]


def _single_pids(bus: ScriptedEcu) -> list[int]:
    return [pids[0] for pids in bus.mode01_requests() if len(pids) == 1]


def _sleepy_ecu(clock: FakeClock, wake_after: int) -> ScriptedEcu:
    """Silent to every request until ``wake_after`` sends, then a healthy
    batching ECU — the Pi booting with the key off, then the engine starting."""
    ecu = _batching_ecu(clock, silent=True)

    def wake(_msg: can.Message) -> None:
        if len(ecu.sent) > wake_after:  # this send is already recorded
            ecu.silent = False

    ecu.on_send.append(wake)
    return ecu


def _one_bank_ecu(clock: FakeClock, *, silent_after_desync: bool = False) -> ScriptedEcu:
    """An ECU answering 0x06 with two bytes (bank 1, bank 3 — the layout J1979
    gives 0x55-0x58) where the registry says one. Any batch holding 0x06
    desyncs on it: the walk lands on the second byte as if it were a PID.

    ``silent_after_desync``: the key goes off right after the ECU answered the
    desyncing batch.
    """
    ecu = _batching_ecu(clock, values={**_BARO_VALUES, 0x06: b"\x90\x80"})
    answered_the_desync = False

    def key_off(msg: can.Message) -> None:
        nonlocal answered_the_desync
        if answered_the_desync:
            ecu.silent = True
        data = bytes(msg.data)
        pids = data[2 : 1 + data[0]] if data[0] < 0x10 and data[1] == 0x01 else b""
        answered_the_desync = answered_the_desync or (len(pids) > 1 and 0x06 in pids)

    if silent_after_desync:
        ecu.on_send.append(key_off)
    return ecu


def _spec(**kw: Any) -> CanObdSpec:
    base: dict[str, Any] = {
        "can": CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        "poll_rate_hz": 50.0,
        "request_timeout_seconds": 0.05,
    }
    base.update(kw)
    return CanObdSpec(**base)


def test_companion_selection_respects_poll_tier() -> None:
    """A slow-tier PID in a fast-tier batch would either be dragged to fast
    cadence or hold the fast one back. Both waste the budget it is buying."""
    spec = _spec(pids=("RPM", "FUEL_LEVEL"))  # RPM is fast, FUEL_LEVEL slow
    requests = build_poll_requests(_NO_DISCOVERY, spec)  # type: ignore[arg-type]
    states = _initial_request_states(requests, spec, 0.0)
    for state in states:
        state.next_due_at = 0.0
    leader = next(i for i, r in enumerate(requests) if r.channel == "RPM")

    chosen = select_batch_companions(
        requests, states, 1.0, leader_index=leader, max_pids=6, widths=None
    )

    tiers = {requests[i].poll_tier for i in chosen}
    assert tiers == {"fast"}
    assert "FUEL_LEVEL" not in {requests[i].channel for i in chosen}


def test_companion_selection_is_a_noop_when_batching_disabled() -> None:
    spec = _spec(pids=("RPM", "SPEED"))
    requests = build_poll_requests(_NO_DISCOVERY, spec)  # type: ignore[arg-type]
    states = _initial_request_states(requests, spec, 0.0)

    assert select_batch_companions(
        requests, states, 1.0, leader_index=0, max_pids=1, widths=None
    ) == [0]


def test_active_profile_batch_groups_partition_companions() -> None:
    from src.collection import tuning_profile
    from src.collection.capture_profiles import CaptureProfile

    channels = ("RPM", "COMMANDED_EQUIV_RATIO", "ENGINE_LOAD", "O2_WR_LAMBDA_S1", "MAF")
    spec = _spec(pids=channels)
    requests = build_poll_requests(_NO_DISCOVERY, spec)  # type: ignore[arg-type]
    states = _initial_request_states(requests, spec, 0.0)
    for state in states:
        state.next_due_at = 0.0
    index = {request.channel: i for i, request in enumerate(requests)}
    tuning_profile.activate(
        CaptureProfile(
            name="split",
            mode01=dict.fromkeys(channels, 1.0),
            mode01_batch_groups=(
                ("RPM", "COMMANDED_EQUIV_RATIO", "ENGINE_LOAD"),
                ("O2_WR_LAMBDA_S1", "MAF"),
            ),
            corpus_eligible=False,
            storage_prefix="_capture-split",
        ),
        source="test",
    )
    try:
        command_batch = select_batch_companions(
            requests,
            states,
            1.0,
            leader_index=index["COMMANDED_EQUIV_RATIO"],
            max_pids=6,
            widths=None,
        )
        lambda_batch = select_batch_companions(
            requests,
            states,
            1.0,
            leader_index=index["O2_WR_LAMBDA_S1"],
            max_pids=6,
            widths=None,
        )
    finally:
        tuning_profile.set_active(False, source="test")

    # 0x44 is medium-tier, so the ordinary tier guard keeps it single even
    # though its explicit partition also contains fast context PIDs.
    assert {requests[i].channel for i in command_batch} == {"COMMANDED_EQUIV_RATIO"}
    assert {requests[i].channel for i in lambda_batch} == {"O2_WR_LAMBDA_S1", "MAF"}


def test_only_plain_mode01_pids_are_batchable() -> None:
    spec = _spec(pids=("RPM",), dids=("1225",))
    requests = build_poll_requests(_NO_DISCOVERY, spec)  # type: ignore[arg-type]

    by_kind = {r.kind: r for r in requests}
    assert is_batchable(by_kind["pid"]) is True
    assert is_batchable(by_kind["did"]) is False


def test_loop_batches_and_emits_one_reading_per_channel(clock: FakeClock) -> None:
    """The payoff: fewer round trips, same per-channel readings downstream."""
    bus = _batching_ecu(clock)
    readings: list[Any] = []
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS"), batch_max_pids=6),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 0.5,
    )

    assert bus.batch_requests > 0, "expected batched requests"
    channels = {r.pid for r in readings}
    assert {"RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS"} <= channels
    by_channel = {r.pid: r.value for r in readings}
    assert by_channel["RPM"] == 750.0  # 0x0BB8 / 4
    assert by_channel["SPEED"] == 50.0  # 0x32


def test_batching_beats_single_pid_on_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same channels, same clock — count the requests each way.

    The same *simulated* 0.4 s each way (T01.8): the two runs used to get
    0.4 s of wall clock each, so a load spike during one of them compared a
    starved run with a healthy one, and the unanswered DTC polls (a fixed
    0.05 s each, whatever the load) weighed more in the starved one.
    """
    pids = ("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS")
    clock = FakeClock()
    install_clock(monkeypatch, clock)

    def run(batch_max: int) -> tuple[int, int]:
        bus = _batching_ecu(clock)
        got: list[Any] = []
        run_can_obd_loop(
            bus,  # type: ignore[arg-type]
            _spec(pids=pids, batch_max_pids=batch_max),
            on_frame=lambda _m: None,
            on_reading=got.append,
            stop_event=threading.Event(),
            deadline_monotonic=clock.monotonic() + 0.4,
        )
        return len(bus.sent), len(got)

    single_requests, single_readings = run(1)
    batch_requests, batch_readings = run(6)

    assert batch_readings > 0 and single_readings > 0
    # Readings per request is the whole point.
    assert (batch_readings / batch_requests) > (single_readings / single_requests)


def _run_against_a_batch_refusing_ecu(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    """RPM/SPEED/ENGINE_LOAD against an ECU that ignores every batch, on a fake clock."""
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    ecu = ScriptedEcu(clock, answer_batches=False)
    readings: list[Any] = []
    stats = run_can_obd_loop(
        ecu,  # type: ignore[arg-type]
        _spec(pids=("RPM", "SPEED", "ENGINE_LOAD"), batch_max_pids=6),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + BATCH_REFUSAL_WINDOW_SECONDS,
    )
    return ecu, readings, stats


def test_loop_falls_back_when_the_ecu_refuses_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ECU that ignores multi-PID requests must not stall the session. The
    loop gives up after a few failures and keeps polling single-PID -- and
    crucially must NOT back off the healthy channels on the way there."""
    bus, readings, _stats = _run_against_a_batch_refusing_ecu(monkeypatch)

    assert bus.batch_requests > 0, "should have tried batching"
    assert {r.pid for r in readings} >= {"RPM", "SPEED", "ENGINE_LOAD"}, (
        "must recover to single-PID polling and still produce every channel"
    )


def test_batch_size_steps_down_instead_of_giving_up(clock: FakeClock) -> None:
    """An ECU that takes 4 PIDs but refuses 6 should end up batching at 4, not
    fall back to single-PID. 'Supports batching' is not binary."""
    from src.collection.can_obd_collector import _next_batch_size

    assert [_next_batch_size(n) for n in (6, 4, 2)] == [4, 2, 1]

    bus = _batching_ecu(clock, max_batch_pids=4)  # batches, just not the J1979 six
    # Stops once every asserted channel has been read (T01): a 0.8 s wall-clock
    # deadline had to cover three batch timeouts before the step-down.
    sink: StopWhen[Any] = StopWhen(
        lambda got: {r.pid for r in got} >= {"RPM", "SPEED", "ENGINE_LOAD"}
    )
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(
            pids=("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS", "TIMING_ADVANCE"),
            batch_max_pids=6,
        ),
        on_frame=lambda _m: None,
        on_reading=sink,
        stop_event=sink.stop,
        deadline_monotonic=clock.monotonic() + 30.0,
    )
    readings = sink.items

    assert bus.rejected_batch_sizes, "should have tried a too-large batch first"
    assert max(bus.rejected_batch_sizes) > 4
    # Having narrowed, it must still be batching (not single-PID) and still
    # producing every channel.
    assert {r.pid for r in readings} >= {"RPM", "SPEED", "ENGINE_LOAD"}


def test_healthy_channels_are_not_backed_off_by_a_failed_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subtle one. A whole-batch failure says nothing about any individual
    PID, so crediting six misses would wrongly suppress six healthy channels.
    After the fallback, every channel must still be polling."""
    _bus, readings, stats = _run_against_a_batch_refusing_ecu(monkeypatch)

    assert stats.batch_failures > 0
    counts = {c: sum(1 for r in readings if r.pid == c) for c in ("RPM", "SPEED", "ENGINE_LOAD")}
    assert all(n > 0 for n in counts.values()), f"a channel went silent: {counts}"


def test_every_pid_in_a_batch_lands_on_one_timestamp(clock: FakeClock) -> None:
    """A batch is one ECU sampling operation, so it is one instant.

    Each PID used to take its own clock read while the shared payload was
    decoded, scattering a single measurement across several timestamps — which
    is what forced consumers to re-group readings by proximity instead of
    reading a batch as one row.
    """
    bus = _batching_ecu(clock)
    readings: list[Any] = []
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS"), batch_max_pids=6),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 0.5,
    )

    assert bus.batch_requests > 0, "expected batched requests"
    by_stamp: dict[float, set[str]] = {}
    for reading in readings:
        by_stamp.setdefault(reading.timestamp, set()).add(reading.pid)
    # Each batch round put all four channels on a single timestamp.
    assert any(
        {"RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS"} <= channels
        for channels in by_stamp.values()
    ), f"no timestamp carried the whole batch: {by_stamp}"


def test_a_capture_round_puts_every_channel_on_one_row(clock: FakeClock) -> None:
    """More channels than the 6-PID ceiling still reads as a single sample.

    capture-profiles.md 5.b: the operator's unit is a round, not a request.
    Eight channels span two batches ~ms apart, and without a round stamp they
    land on two timestamps — a "row" the consumer has to reassemble by
    proximity.
    """
    from src.collection import tuning_profile
    from src.collection.capture_profiles import CaptureProfile

    channels = (
        "RPM",
        "SPEED",
        "ENGINE_LOAD",
        "THROTTLE_POS",
        "COOLANT_TEMP",
        "INTAKE_TEMP",
        "TIMING_ADVANCE",
        "MAF",
    )
    readings: list[Any] = []
    try:
        tuning_profile.activate(
            CaptureProfile(
                name="rows",
                mode01=dict.fromkeys(channels, 1.0),
                round_timestamps=True,
                corpus_eligible=False,
                storage_prefix="_capture-rows",
            ),
            source="test",
        )
        run_can_obd_loop(
            _batching_ecu(clock),  # type: ignore[arg-type]
            _spec(pids=channels, batch_max_pids=6),
            on_frame=lambda _m: None,
            on_reading=readings.append,
            stop_event=threading.Event(),
            deadline_monotonic=clock.monotonic() + 0.5,
        )
    finally:
        tuning_profile.set_active(False, source="test")

    by_stamp: dict[float, set[str]] = {}
    for reading in readings:
        by_stamp.setdefault(reading.timestamp, set()).add(reading.pid)
    assert any(set(channels) <= got for got in by_stamp.values()), (
        f"no single timestamp carried the whole round: "
        f"{ {k: sorted(v) for k, v in by_stamp.items()} }"
    )

    # The scheduling stamp must not erase when the value actually arrived.
    stamped = [r for r in readings if r.acquired_at is not None]
    assert stamped, "round-stamped readings should retain their arrival instant"
    assert any(r.acquired_at != r.timestamp for r in stamped)


# ---------------------------------------------------------------------------
# C02.5 — companions are chosen from the open round's members
# ---------------------------------------------------------------------------


def test_companions_come_from_the_rounds_members_and_the_tuple_fills_first() -> None:
    """A member is due inside the round's window by construction, so it may
    ride the leader's batch before its own due instant; a non-member may not,
    however due it is. The required tuple takes the batch's slots first."""
    pids = ("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS", "TIMING_ADVANCE", "MAF")
    spec = _spec(pids=pids)
    requests = build_poll_requests(_NO_DISCOVERY, spec)  # type: ignore[arg-type]
    states = _initial_request_states(requests, spec, 0.0)
    index = {r.channel: i for i, r in enumerate(requests)}
    for state in states:
        state.next_due_at = 5.0  # due later in the window, not now
    states[index["RPM"]].next_due_at = 0.0

    members = frozenset(index[c] for c in ("RPM", "SPEED", "ENGINE_LOAD", "MAF"))
    chosen = select_batch_companions(
        requests,
        states,
        1.0,
        leader_index=index["RPM"],
        max_pids=3,
        members=members,
        preferred=frozenset({index["MAF"]}),
        widths=None,
    )
    assert [requests[i].channel for i in chosen] == ["RPM", "MAF", "SPEED"]
    # Without a round, the historic rule holds: only requests due *now* ride.
    assert select_batch_companions(
        requests, states, 1.0, leader_index=index["RPM"], max_pids=3, widths=None
    ) == [index["RPM"]]


def test_batch_readings_share_receipt_time_and_round_but_not_a_claim_of_atomicity(
    clock: FakeClock,
) -> None:
    """Every PID in one batched response carries the same ``acquired_at`` —
    the payload's receipt — and the same round. That is all it says."""
    bus = _batching_ecu(clock)
    readings: list[Any] = []
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS"), batch_max_pids=6),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 0.3,
    )
    assert bus.batch_requests > 0
    by_arrival: dict[float, set[str]] = {}
    for reading in readings:
        assert reading.acquired_at is not None and reading.round_id is not None
        assert reading.timestamp == reading.acquired_at, "no round stamp outside a capture profile"
        by_arrival.setdefault(reading.acquired_at, set()).add(reading.pid)
    assert any(
        {"RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS"} <= pids for pids in by_arrival.values()
    )


# ---------------------------------------------------------------------------
# A sleeping ECU is not evidence about batching (2026-09-18)
# ---------------------------------------------------------------------------

_SIX_FAST = ("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS", "TIMING_ADVANCE", "MAF")


def test_batch_silence_from_a_sleeping_ecu_does_not_narrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Key off at boot: every request times out. That silence must not cost the
    drive its batching — before the fix it narrowed 6 -> 4 -> 2 -> off.

    On a fake clock (T01.8): the ECU wakes after 20 sends, 14 of them timeouts,
    so the first awake batch lands at 0.41 s; 1.2 s of wall clock was 3x that.
    """
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    wake_after = 3 * MAX_CONSECUTIVE_BATCH_FAILURES * 2 + 2
    bus = _sleepy_ecu(clock, wake_after)
    readings: list[Any] = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=_SIX_FAST, batch_max_pids=6, request_timeout_seconds=0.02),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 1.2,
    )

    assert stats.batch_failures >= 3 * MAX_CONSECUTIVE_BATCH_FAILURES
    assert stats.batch_failures_ecu_silent == stats.batch_failures
    assert stats.batch_size_narrowings == 0
    awake_batch_sizes = [len(pids) for pids in bus.mode01_requests()[wake_after:] if len(pids) > 1]
    assert awake_batch_sizes, "batching must survive the ECU waking up"
    assert max(awake_batch_sizes) == 6, "and at the full configured size"
    assert {r.pid for r in readings} >= set(_SIX_FAST)


def test_an_awake_ecu_that_ignores_batches_still_disables_batching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The protection the gate must keep: singles answer, batches are silent,
    so each silent batch is bracketed by proof of life and counts.

    On a fake clock (T01.8): nine whole-batch timeouts come before the last
    channel's first single read at 0.35 s; 1.0 s of wall clock was 2.9x that.
    """
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _batching_ecu(clock, answer_batches=False)
    readings: list[Any] = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=_SIX_FAST, batch_max_pids=6, request_timeout_seconds=0.02),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 1.0,
    )

    assert stats.batch_size_narrowings == 3  # 6 -> 4 -> 2 -> off
    assert stats.batch_failures - stats.batch_failures_ecu_silent == (
        3 * MAX_CONSECUTIVE_BATCH_FAILURES
    )
    assert _mode01_sizes(bus)[-5:] == [1] * 5, "must have fallen back to single-PID"
    assert {r.pid for r in readings} >= set(_SIX_FAST)


def test_an_ecu_that_refuses_batches_with_an_nrc_narrows_regardless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative response is an answer from an awake ECU, so it is evidence
    about batching even with no other contact in between.

    On a fake clock (T01.8): four unanswered DTC/Mode 06 polls come before the
    first single reads at 0.12-0.13 s; 0.6 s of wall clock was 4.6x that.
    """
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _batching_ecu(clock, batch_nrc=0x12)
    readings: list[Any] = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=_SIX_FAST, batch_max_pids=6, request_timeout_seconds=0.02),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 0.6,
    )

    assert stats.batch_size_narrowings == 3
    assert stats.batch_failures == 3 * MAX_CONSECUTIVE_BATCH_FAILURES
    assert stats.batch_failures_ecu_silent == 0
    sizes = [s for s in _mode01_sizes(bus) if s > 1]
    assert sizes[:3] == [6, 6, 6], "no single probe needed between NRC refusals"
    assert _mode01_sizes(bus)[-5:] == [1] * 5
    assert {r.pid for r in readings} >= set(_SIX_FAST)


# ---------------------------------------------------------------------------
# Registry widths are what the batch walk trusts (2026-09-18, batch044)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channels", "leader", "expected_order"),
    # The priority lane picks the leader; companions follow request order.
    [
        (
            ("RPM", "COMMANDED_EQUIV_RATIO", "BAROMETRIC_PRESSURE"),
            "BAROMETRIC_PRESSURE",
            (0x33, 0x0C, 0x44),
        ),
        (("BAROMETRIC_PRESSURE", "COMMANDED_EQUIV_RATIO", "RPM"), "RPM", (0x0C, 0x33, 0x44)),
        (("RPM", "COMMANDED_EQUIV_RATIO", "BAROMETRIC_PRESSURE"), "RPM", (0x0C, 0x44, 0x33)),
    ],
    ids=["baro-first", "baro-middle", "baro-last"],
)
def test_a_capture_round_with_baro_walks_every_member(
    channels: tuple[str, ...], leader: str, expected_order: tuple[int, ...], clock: FakeClock
) -> None:
    """PID 0x33 was registered 2 bytes wide; J1979 and the ECM send 1. In a
    batch the walk then swallowed the next PID's id byte and desynced, and
    every member after baro timed out (0x44: 37/228 live). Baro is slow tier,
    so only a capture round (``ignore_tier``) ever batched it with fast PIDs —
    which is why no test here saw it."""
    from src.collection import tuning_profile
    from src.collection.capture_profiles import CaptureProfile

    bus = _batching_ecu(clock, values=_BARO_VALUES)
    readings: list[Any] = []
    try:
        tuning_profile.activate(
            CaptureProfile(
                name="baro",
                mode01=dict.fromkeys(channels, 1.0),
                priority_channels=(leader,),
                round_timestamps=True,
                corpus_eligible=False,
                storage_prefix="_capture-baro",
            ),
            source="test",
        )
        stats = run_can_obd_loop(
            bus,  # type: ignore[arg-type]
            _spec(pids=channels, batch_max_pids=6),
            on_frame=lambda _m: None,
            on_reading=readings.append,
            stop_event=threading.Event(),
            deadline_monotonic=clock.monotonic() + 0.5,
        )
    finally:
        tuning_profile.set_active(False, source="test")

    assert _batch_orders(bus), "the round never batched — the regression is not exercised"
    assert _batch_orders(bus)[0] == expected_order, _batch_orders(bus)[0]
    assert stats.batch_desyncs == 0, "the walk lost the payload boundary after baro"
    assert set(_batch_orders(bus)) == {expected_order}, _batch_orders(bus)
    by_channel: dict[str, list[float]] = {}
    for reading in readings:
        by_channel.setdefault(reading.pid, []).append(reading.value)
    assert set(channels) <= set(by_channel), f"a member was lost: {sorted(by_channel)}"
    assert set(by_channel["BAROMETRIC_PRESSURE"]) == {81.0}
    assert set(by_channel["COMMANDED_EQUIV_RATIO"]) == {1.0}  # 0x8000 / 32768
    assert set(by_channel["RPM"]) == {750.0}
    # Every batch put all three members on one row.
    by_stamp: dict[float, set[str]] = {}
    for reading in readings:
        by_stamp.setdefault(reading.timestamp, set()).add(reading.pid)
    assert any(set(channels) <= got for got in by_stamp.values())


# ---------------------------------------------------------------------------
# Measured-width guard: a vehicle that contradicts the registry (batch044 #4)
# ---------------------------------------------------------------------------


def _run_capture(bus: ScriptedEcu, channels: tuple[str, ...], seconds: float):  # type: ignore[no-untyped-def]
    """Run ``bus`` for ``seconds`` of its fake clock under a capture round.

    Simulated time (T01.8): the mismatch tests need three unanswered DTC polls,
    the desync, the single re-measure and a later batch before they can pass
    (~0.08 s), which a loaded machine could spend a good part of a 1.0 s wall
    window on.
    """
    assert bus.clock is not None, "_run_capture runs on the bus's fake clock"
    from src.collection import tuning_profile
    from src.collection.capture_profiles import CaptureProfile

    readings: list[Any] = []
    try:
        tuning_profile.activate(
            CaptureProfile(
                name="onebank",
                mode01=dict.fromkeys(channels, 1.0),
                priority_channels=("RPM",),
                round_timestamps=True,
                corpus_eligible=False,
                storage_prefix="_capture-onebank",
            ),
            source="test",
        )
        spec = _spec(pids=channels, batch_max_pids=6, request_timeout_seconds=0.02)
        stats = run_can_obd_loop(
            bus,  # type: ignore[arg-type]
            spec,
            on_frame=lambda _m: None,
            on_reading=readings.append,
            stop_event=threading.Event(),
            deadline_monotonic=bus.clock.monotonic() + seconds,
        )
    finally:
        tuning_profile.set_active(False, source="test")
    return spec, stats, readings


@pytest.mark.parametrize(
    "channels",
    [
        ("RPM", "SHORT_FUEL_TRIM_1", "SPEED", "ENGINE_LOAD"),  # a member follows the stray byte
        ("RPM", "SPEED", "ENGINE_LOAD", "SHORT_FUEL_TRIM_1"),  # the stray byte is the tail
    ],
    ids=["mismatch-middle", "mismatch-last"],
)
def test_a_pid_whose_measured_width_contradicts_the_registry_leaves_batches(
    channels: tuple[str, ...], caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One desync, then 0x06 is read single, measured at 2 bytes, and never
    batched again; the members the desync cost are re-read, not timed out."""
    import logging

    from src.collection.obd_scheduler import _batching_metrics

    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _one_bank_ecu(clock)
    with caplog.at_level(logging.WARNING, logger="src.collection.obd_scheduler"):
        spec, stats, readings = _run_capture(bus, channels, 1.0)

    holding = [i for i, order in enumerate(_batch_orders(bus)) if 0x06 in order]
    assert holding, f"0x06 never batched: {_batch_orders(bus)}"
    assert stats.batch_desyncs == 1, "the desync must not recur once 0x06 is measured"
    assert len(holding) == 1, f"0x06 batched again after the desync: {_batch_orders(bus)}"
    # The correct-width members keep batching after the desync.
    after = _batch_orders(bus)[holding[0] + 1 :]
    assert any(set(order) == {0x0C, 0x0D, 0x04} for order in after), _batch_orders(bus)
    assert 0x06 in _single_pids(bus)
    # The fake answers only Mode 01, so the DTC polls are the only timeouts.
    other_services = sum(1 for m in bus.messages if m.data[0] < 0x10 and m.data[1] != 0x01)
    assert stats.request_timeouts == other_services, "unparsed members were charged a timeout"
    by_channel: dict[str, set[float]] = {}
    for reading in readings:
        by_channel.setdefault(reading.pid, set()).add(reading.value)
    assert set(channels) <= set(by_channel), f"a member was lost: {sorted(by_channel)}"
    assert by_channel["RPM"] == {750.0} and by_channel["SPEED"] == {50.0}
    assert by_channel["SHORT_FUEL_TRIM_1"] == {12.5}, "a mis-sliced 0x06 value leaked"

    block = _batching_metrics(spec, stats, clock.monotonic())
    assert block is not None
    assert block["width_mismatches"] == {"0x06": {"registry": 1, "observed": 2}}
    assert block["desync_blame"] == {"0x06": 1}
    assert block["measured_widths"]["0x06"] == 2
    assert block["remeasure_pending"] == []
    warnings = [r for r in caplog.records if "0x06" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


def test_a_desync_followed_by_silence_does_not_storm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-measures ride each member's own next slot: an ECU that goes quiet
    after the desync sees no more than one request per member per round."""
    channels = ("RPM", "SHORT_FUEL_TRIM_1", "SPEED", "ENGINE_LOAD")
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _one_bank_ecu(clock, silent_after_desync=True)
    _spec_, stats, _readings = _run_capture(bus, channels, 0.8)

    assert stats.batch_desyncs == 1
    assert stats.rounds_started >= 2
    assert len(bus.sent) <= stats.rounds_started * len(channels)
    assert _single_pids(bus).count(0x06) <= stats.rounds_started
    # A silent single measures nothing: the re-measure stays owed.
    assert 0x06 in stats.mode01_widths.remeasure
    assert not stats.mode01_widths.mismatches


def test_a_matching_single_read_keeps_the_pid_batchable() -> None:
    from src.collection.obd_scheduler import Mode01WidthLedger

    ledger = Mode01WidthLedger()
    assert ledger.batchable(0x55), "registry width is trusted until contradicted"
    ledger.on_desync({0x0D}, suspect=0x55)
    assert not ledger.batchable(0x55) and not ledger.batchable(0x0D)
    ledger.observe_single(0x55, 2)
    ledger.observe_single(0x0D, 1)
    assert ledger.batchable(0x55) and ledger.batchable(0x0D)
    assert ledger.mismatches == {}
    ledger.observe_single(0x55, 1)
    assert not ledger.batchable(0x55)
    assert ledger.batch_width(0x55) is None


def test_repeated_blame_without_a_width_contradiction_stops_batching() -> None:
    from src.collection.obd_scheduler import MODE01_DESYNC_BLAME_LIMIT, Mode01WidthLedger

    ledger = Mode01WidthLedger()
    for _ in range(MODE01_DESYNC_BLAME_LIMIT):
        ledger.on_desync((), suspect=0x0C)
        ledger.observe_single(0x0C, 2)
    assert not ledger.batchable(0x0C)
    assert ledger.metrics()["desync_excluded"] == ["0x0C"]


def test_walk_blames_the_pid_before_a_stray_boundary_byte() -> None:
    from src.collection.isotp import walk_batched_response

    registry = {0x0C: 2, 0x55: 2, 0x0D: 1}
    # 0C 0B B8 | 55 90 | 0D 32 — 0x55 sliced as 2 bytes lands on 0x32.
    walk = walk_batched_response(
        bytes.fromhex("0C0BB855900D32"), registry.get, expected={0x0C, 0x55, 0x0D}
    )
    assert not walk.clean and walk.suspect == 0x55
    # Truncated tail: the PID at the break is blamed.
    walk = walk_batched_response(bytes.fromhex("0C0BB85590"), registry.get, expected={0x0C, 0x55})
    assert not walk.clean and walk.suspect == 0x55
    assert walk.readings == {0x0C: b"\x0b\xb8"}


def test_exit_snapshot_reports_the_narrowed_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """R02.9: the in-loop metrics passed the live batch sizes and the call at
    loop exit omitted them, so the final snapshot's ``resolved_plan`` reported
    the *configured* six after batching had been disabled."""
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _batching_ecu(clock, answer_batches=False)
    snapshots: list[Any] = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=_SIX_FAST, batch_max_pids=6, request_timeout_seconds=0.02),
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 1.0,
        on_scheduler_metrics=snapshots.append,
        scheduler_metrics_interval_seconds=10.0,
    )

    assert stats.batch_size_narrowings == 3  # 6 -> 4 -> 2 -> off
    final = snapshots[-1]
    assert final.resolved_plan["batch_max"]["mode01"] == 1
