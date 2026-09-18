"""Multi-PID Mode 01 batching — roadmap/multi-pid-polling.md Phase 1.

The behaviours worth pinning are the ones that make a batch *safe* to trust.
A batched read that silently misattributes values is far worse than no
batching at all: every downstream feature, surface and model would be fed
plausible numbers off the wrong channel.
"""

from __future__ import annotations

import can
import pytest

from src.collection import tx_gate
from src.collection.can_obd_client import BatchedMode01Response, CanObdClient
from src.collection.isotp import (
    MAX_BATCHED_PIDS,
    build_batched_request_payload,
    split_batched_response,
)

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


class _ScriptedBus:
    """Replays a canned ISO-TP response to whatever is sent."""

    def __init__(self, frames: list[bytes]) -> None:
        self.sent: list[can.Message] = []
        self._frames = list(frames)

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.sent.append(msg)

    def recv(self, timeout: float | None = None) -> can.Message | None:
        if not self._frames:
            return None
        return can.Message(arbitration_id=0x7E8, data=self._frames.pop(0), is_extended_id=False)

    def shutdown(self) -> None:  # pragma: no cover - interface completeness
        pass


def test_client_batch_reassembles_a_multi_frame_response() -> None:
    """A 6-PID response is 15 bytes, so it arrives as first-frame plus
    consecutive frames and exercises the flow-control path."""
    # 41 0C 0000 0D 00 04 00 11 13 0E FF 10 0076  == 15 bytes
    first = bytes([0x10, 0x0F, 0x41, 0x0C, 0x00, 0x00, 0x0D, 0x00])
    cf1 = bytes([0x21, 0x04, 0x00, 0x11, 0x13, 0x0E, 0xFF, 0x10])
    cf2 = bytes([0x22, 0x00, 0x76, 0x00, 0x00, 0x00, 0x00, 0x00])
    bus = _ScriptedBus([first, cf1, cf2])
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
    bus = _ScriptedBus([bytes([0x03, 0x7F, 0x01, 0x12, 0x00, 0x00, 0x00, 0x00])])
    client = CanObdClient(bus, timeout_seconds=0.3)  # type: ignore[arg-type]

    assert client.query_mode_01_batch([0x0C, 0x0D], byte_length_for=width_for) is None


def test_client_batch_returns_none_on_timeout() -> None:
    client = CanObdClient(_ScriptedBus([]), timeout_seconds=0.1)  # type: ignore[arg-type]

    assert client.query_mode_01_batch([0x0C], byte_length_for=width_for) is None


def test_client_batch_transmits_nothing_in_passive_mode() -> None:
    """Passive mode is a safety gate; batching must not sneak past it."""
    bus = _ScriptedBus([])
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
import time  # noqa: E402

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


class _BatchingEcu:
    """Answers Mode 01 requests, single or batched, from a fixed value table."""

    #: pid -> payload the fake ECU returns. 0x2F is deliberately absent so
    #: the "ECU omits an unsupported PID" path gets exercised.
    #: 0x05/0x0F/0x10 give more than the six-PID ceiling, so a selection can
    #: span two requests — the case a capture round has to hold together.
    VALUES = {
        0x0C: b"\x0b\xb8",
        0x0D: b"\x32",
        0x04: b"\x40",
        0x11: b"\x19",
        0x0E: b"\x50",
        0x05: b"\x5a",
        0x0F: b"\x46",
        0x10: b"\x01\x00",
    }

    def __init__(self, *, answer_batches: bool = True) -> None:
        self.sent: list[can.Message] = []
        self._pending: list[bytes] = []
        self.answer_batches = answer_batches
        self.batch_requests = 0

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.sent.append(msg)
        data = bytes(msg.data)
        length, service = data[0], data[1]
        if service != 0x01:
            return
        pids = list(data[2 : 1 + length])
        if len(pids) > 1:
            self.batch_requests += 1
            if not self.answer_batches:
                return  # silence, exactly like an ECU that dislikes batching
        body = bytearray([0x41])
        for pid in pids:
            if pid in self.VALUES:
                body += bytes([pid]) + self.VALUES[pid]
        self._pending.extend(_isotp_frames(bytes(body)))

    def recv(self, timeout: float | None = None) -> can.Message | None:
        if not self._pending:
            return None
        return can.Message(arbitration_id=0x7E8, data=self._pending.pop(0), is_extended_id=False)

    def shutdown(self) -> None:  # pragma: no cover - interface completeness
        pass


def _isotp_frames(body: bytes) -> list[bytes]:
    """Wrap a service body in single- or multi-frame ISO-TP."""
    if len(body) <= 7:
        return [bytes([len(body)]) + body + bytes(7 - len(body))]
    frames = [bytes([0x10 | (len(body) >> 8), len(body) & 0xFF]) + body[:6]]
    rest, seq = body[6:], 1
    while rest:
        chunk, rest = rest[:7], rest[7:]
        frames.append(bytes([0x20 | (seq & 0x0F)]) + chunk + bytes(7 - len(chunk)))
        seq += 1
    return frames


def _spec(**kw) -> CanObdSpec:
    base = {
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
    requests = build_poll_requests(_BatchingEcu(), spec)  # type: ignore[arg-type]
    states = _initial_request_states(requests, spec, 0.0)
    for state in states:
        state.next_due_at = 0.0
    leader = next(i for i, r in enumerate(requests) if r.channel == "RPM")

    chosen = select_batch_companions(requests, states, 1.0, leader_index=leader, max_pids=6)

    tiers = {requests[i].poll_tier for i in chosen}
    assert tiers == {"fast"}
    assert "FUEL_LEVEL" not in {requests[i].channel for i in chosen}


def test_companion_selection_is_a_noop_when_batching_disabled() -> None:
    spec = _spec(pids=("RPM", "SPEED"))
    requests = build_poll_requests(_BatchingEcu(), spec)  # type: ignore[arg-type]
    states = _initial_request_states(requests, spec, 0.0)

    assert select_batch_companions(requests, states, 1.0, leader_index=0, max_pids=1) == [0]


def test_only_plain_mode01_pids_are_batchable() -> None:
    spec = _spec(pids=("RPM",), dids=("1225",))
    requests = build_poll_requests(_BatchingEcu(), spec)  # type: ignore[arg-type]

    by_kind = {r.kind: r for r in requests}
    assert is_batchable(by_kind["pid"]) is True
    assert is_batchable(by_kind["did"]) is False


def test_loop_batches_and_emits_one_reading_per_channel() -> None:
    """The payoff: fewer round trips, same per-channel readings downstream."""
    bus = _BatchingEcu()
    readings: list = []
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS"), batch_max_pids=6),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=time.monotonic() + 0.5,
    )

    assert bus.batch_requests > 0, "expected batched requests"
    channels = {r.pid for r in readings}
    assert {"RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS"} <= channels
    by_channel = {r.pid: r.value for r in readings}
    assert by_channel["RPM"] == 750.0  # 0x0BB8 / 4
    assert by_channel["SPEED"] == 50.0  # 0x32


def test_batching_beats_single_pid_on_round_trips() -> None:
    """Same channels, same wall clock — count the requests each way."""
    pids = ("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS")

    def run(batch_max: int) -> tuple[int, int]:
        bus = _BatchingEcu()
        got: list = []
        run_can_obd_loop(
            bus,  # type: ignore[arg-type]
            _spec(pids=pids, batch_max_pids=batch_max),
            on_frame=lambda _m: None,
            on_reading=got.append,
            stop_event=threading.Event(),
            deadline_monotonic=time.monotonic() + 0.4,
        )
        return len(bus.sent), len(got)

    single_requests, single_readings = run(1)
    batch_requests, batch_readings = run(6)

    assert batch_readings > 0 and single_readings > 0
    # Readings per request is the whole point.
    assert (batch_readings / batch_requests) > (single_readings / single_requests)


def test_loop_falls_back_when_the_ecu_refuses_batches() -> None:
    """An ECU that ignores multi-PID requests must not stall the session. The
    loop gives up after a few failures and keeps polling single-PID -- and
    crucially must NOT back off the healthy channels on the way there."""
    bus = _BatchingEcu(answer_batches=False)
    readings: list = []
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "SPEED", "ENGINE_LOAD"), batch_max_pids=6),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=time.monotonic() + 0.8,
    )

    assert bus.batch_requests > 0, "should have tried batching"
    assert {r.pid for r in readings} >= {"RPM", "SPEED", "ENGINE_LOAD"}, (
        "must recover to single-PID polling and still produce every channel"
    )


class _SizeLimitedEcu(_BatchingEcu):
    """Answers batches only up to ``max_accepted`` PIDs, ignoring larger ones.

    Models the realistic middle case the step-down exists for: an ECU that
    supports batching but not the full J1979 six.
    """

    def __init__(self, max_accepted: int) -> None:
        super().__init__()
        self.max_accepted = max_accepted
        self.rejected_sizes: list[int] = []

    def send(self, msg, timeout=None):  # type: ignore[no-untyped-def]
        data = bytes(msg.data)
        length, service = data[0], data[1]
        pids = list(data[2 : 1 + length])
        if service == 0x01 and len(pids) > self.max_accepted:
            self.sent.append(msg)
            self.batch_requests += 1
            self.rejected_sizes.append(len(pids))
            return  # silence
        super().send(msg, timeout)


def test_batch_size_steps_down_instead_of_giving_up() -> None:
    """An ECU that takes 4 PIDs but refuses 6 should end up batching at 4, not
    fall back to single-PID. 'Supports batching' is not binary."""
    from src.collection.can_obd_collector import _next_batch_size

    assert [_next_batch_size(n) for n in (6, 4, 2)] == [4, 2, 1]

    bus = _SizeLimitedEcu(max_accepted=4)
    readings: list = []
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(
            pids=("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS", "TIMING_ADVANCE"),
            batch_max_pids=6,
        ),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=time.monotonic() + 0.8,
    )

    assert bus.rejected_sizes, "should have tried a too-large batch first"
    assert max(bus.rejected_sizes) > 4
    # Having narrowed, it must still be batching (not single-PID) and still
    # producing every channel.
    assert {r.pid for r in readings} >= {"RPM", "SPEED", "ENGINE_LOAD"}


def test_healthy_channels_are_not_backed_off_by_a_failed_batch() -> None:
    """The subtle one. A whole-batch failure says nothing about any individual
    PID, so crediting six misses would wrongly suppress six healthy channels.
    After the fallback, every channel must still be polling."""
    bus = _BatchingEcu(answer_batches=False)
    readings: list = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "SPEED", "ENGINE_LOAD"), batch_max_pids=6),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=time.monotonic() + 0.8,
    )

    assert stats.batch_failures > 0
    counts = {c: sum(1 for r in readings if r.pid == c) for c in ("RPM", "SPEED", "ENGINE_LOAD")}
    assert all(n > 0 for n in counts.values()), f"a channel went silent: {counts}"


def test_every_pid_in_a_batch_lands_on_one_timestamp() -> None:
    """A batch is one ECU sampling operation, so it is one instant.

    Each PID used to take its own clock read while the shared payload was
    decoded, scattering a single measurement across several timestamps — which
    is what forced consumers to re-group readings by proximity instead of
    reading a batch as one row.
    """
    bus = _BatchingEcu()
    readings: list = []
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS"), batch_max_pids=6),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=time.monotonic() + 0.5,
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


def test_a_capture_round_puts_every_channel_on_one_row() -> None:
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
    readings: list = []
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
            _BatchingEcu(),  # type: ignore[arg-type]
            _spec(pids=channels, batch_max_pids=6),
            on_frame=lambda _m: None,
            on_reading=readings.append,
            stop_event=threading.Event(),
            deadline_monotonic=time.monotonic() + 0.5,
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
    requests = build_poll_requests(_BatchingEcu(), spec)  # type: ignore[arg-type]
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
    )
    assert [requests[i].channel for i in chosen] == ["RPM", "MAF", "SPEED"]
    # Without a round, the historic rule holds: only requests due *now* ride.
    assert select_batch_companions(
        requests, states, 1.0, leader_index=index["RPM"], max_pids=3
    ) == [index["RPM"]]


def test_batch_readings_share_receipt_time_and_round_but_not_a_claim_of_atomicity() -> None:
    """Every PID in one batched response carries the same ``acquired_at`` —
    the payload's receipt — and the same round. That is all it says."""
    bus = _BatchingEcu()
    readings: list = []
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS"), batch_max_pids=6),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=time.monotonic() + 0.3,
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


class _SleepyEcu(_BatchingEcu):
    """Silent to every request until ``wake_after`` sends, then a healthy
    batching ECU — the Pi booting with the key off, then the engine starting."""

    def __init__(self, wake_after: int) -> None:
        super().__init__()
        self.wake_after = wake_after
        self.sends = 0
        self.awake_batch_sizes: list[int] = []

    def send(self, msg, timeout=None):  # type: ignore[no-untyped-def]
        self.sends += 1
        data = bytes(msg.data)
        pids = list(data[2 : 1 + data[0]])
        if self.sends <= self.wake_after:
            self.sent.append(msg)
            if len(pids) > 1:
                self.batch_requests += 1
            return
        if data[1] == 0x01 and len(pids) > 1:
            self.awake_batch_sizes.append(len(pids))
        super().send(msg, timeout)


class _NrcBatchEcu(_BatchingEcu):
    """Answers singles; refuses every batch with a negative response."""

    def send(self, msg, timeout=None):  # type: ignore[no-untyped-def]
        data = bytes(msg.data)
        pids = list(data[2 : 1 + data[0]])
        if data[1] == 0x01 and len(pids) > 1:
            self.sent.append(msg)
            self.batch_requests += 1
            self._pending.append(bytes([0x03, 0x7F, 0x01, 0x12, 0, 0, 0, 0]))
            return
        super().send(msg, timeout)


def _mode01_sizes(bus: _BatchingEcu) -> list[int]:
    return [len(bytes(m.data)[2 : 1 + m.data[0]]) for m in bus.sent if bytes(m.data)[1] == 0x01]


def test_batch_silence_from_a_sleeping_ecu_does_not_narrow() -> None:
    """Key off at boot: every request times out. That silence must not cost the
    drive its batching — before the fix it narrowed 6 -> 4 -> 2 -> off."""
    bus = _SleepyEcu(wake_after=3 * MAX_CONSECUTIVE_BATCH_FAILURES * 2 + 2)
    readings: list = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=_SIX_FAST, batch_max_pids=6, request_timeout_seconds=0.02),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=time.monotonic() + 1.2,
    )

    assert stats.batch_failures >= 3 * MAX_CONSECUTIVE_BATCH_FAILURES
    assert stats.batch_failures_ecu_silent == stats.batch_failures
    assert stats.batch_size_narrowings == 0
    assert bus.awake_batch_sizes, "batching must survive the ECU waking up"
    assert max(bus.awake_batch_sizes) == 6, "and at the full configured size"
    assert {r.pid for r in readings} >= set(_SIX_FAST)


def test_an_awake_ecu_that_ignores_batches_still_disables_batching() -> None:
    """The protection the gate must keep: singles answer, batches are silent,
    so each silent batch is bracketed by proof of life and counts."""
    bus = _BatchingEcu(answer_batches=False)
    readings: list = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=_SIX_FAST, batch_max_pids=6, request_timeout_seconds=0.02),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=time.monotonic() + 1.0,
    )

    assert stats.batch_size_narrowings == 3  # 6 -> 4 -> 2 -> off
    assert stats.batch_failures - stats.batch_failures_ecu_silent == (
        3 * MAX_CONSECUTIVE_BATCH_FAILURES
    )
    assert _mode01_sizes(bus)[-5:] == [1] * 5, "must have fallen back to single-PID"
    assert {r.pid for r in readings} >= set(_SIX_FAST)


def test_an_ecu_that_refuses_batches_with_an_nrc_narrows_regardless() -> None:
    """A negative response is an answer from an awake ECU, so it is evidence
    about batching even with no other contact in between."""
    bus = _NrcBatchEcu()
    readings: list = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=_SIX_FAST, batch_max_pids=6, request_timeout_seconds=0.02),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=time.monotonic() + 0.6,
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


class _BaroEcu(_BatchingEcu):
    """The fuel-pw-research-2 members that desynced: baro answers with the one
    byte J1979 gives it, the way the Xterra ECM does."""

    VALUES = {**_BatchingEcu.VALUES, 0x33: b"\x51", 0x44: b"\x80\x00"}

    def __init__(self) -> None:
        super().__init__()
        self.batch_orders: list[tuple[int, ...]] = []

    def send(self, msg, timeout=None):  # type: ignore[no-untyped-def]
        data = bytes(msg.data)
        pids = tuple(data[2 : 1 + data[0]])
        if data[1] == 0x01 and len(pids) > 1:
            self.batch_orders.append(pids)
        super().send(msg, timeout)


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
    channels: tuple[str, ...], leader: str, expected_order: tuple[int, ...]
) -> None:
    """PID 0x33 was registered 2 bytes wide; J1979 and the ECM send 1. In a
    batch the walk then swallowed the next PID's id byte and desynced, and
    every member after baro timed out (0x44: 37/228 live). Baro is slow tier,
    so only a capture round (``ignore_tier``) ever batched it with fast PIDs —
    which is why no test here saw it."""
    from src.collection import tuning_profile
    from src.collection.capture_profiles import CaptureProfile

    bus = _BaroEcu()
    readings: list = []
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
            deadline_monotonic=time.monotonic() + 0.5,
        )
    finally:
        tuning_profile.set_active(False, source="test")

    assert bus.batch_orders, "the round never batched — the regression is not exercised"
    assert bus.batch_orders[0] == expected_order, bus.batch_orders[0]
    assert stats.batch_desyncs == 0, "the walk lost the payload boundary after baro"
    assert set(bus.batch_orders) == {expected_order}, bus.batch_orders
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
