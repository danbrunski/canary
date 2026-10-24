"""Multi-DID Mode 22 batching — the 2-byte-identifier sibling of
``test_multi_pid_batching.py``.

The reason to batch 0x22 is NOT throughput: an unbatched sweep already
manages ~270 DIDs/s on this bus. It is that DIDs in one request share a
single ECU sample instant, so their values can be compared with each other.
Sequentially polled DIDs cannot — they are rotation-sampled with an unknown
relative age.

That makes misattribution the failure that matters. A Mode 22 response is
NOT self-delimiting, so splitting it depends entirely on knowing each DID's
payload width; get one width wrong and every later DID in that response is
silently read off the wrong bytes. The tests below pin the refusals that
keep that impossible.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Any

import can
import pytest

from src.analysis.did_scan import BatchRead, sweep_dids_batched
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.isotp import (
    MAX_BATCHED_DIDS,
    build_batched_did_request_payload,
    split_batched_did_response,
)
from src.collection.obd_scheduler import CanObdSpec
from src.config import CanBusConfig
from tests.capture_round_fakes import FakeClock, install_clock

# Widths as MEASURED payload lengths, which is what the splitter needs.
WIDTHS = {0x1101: 1, 0x1219: 2, 0x1208: 2, 0x1100: 4, 0x1103: 1}


def width_for(did: int) -> int | None:
    return WIDTHS.get(did)


# ---------------------------------------------------------------------------
# building the request
# ---------------------------------------------------------------------------


def test_batched_did_request_matches_the_frame_uprev_was_observed_sending() -> None:
    """UpRev's tester sent ``05 22 11 03 04 01`` to this ECM on 2026-08-08 —
    service 0x22 carrying two DIDs. That capture is why we know the ECM
    accepts batched 0x22 at all, so the builder must reproduce it exactly."""
    payload = build_batched_did_request_payload(0x22, [0x1103, 0x0401])
    assert payload[:6] == bytes([0x05, 0x22, 0x11, 0x03, 0x04, 0x01])


def test_batched_did_request_is_always_padded_to_eight_bytes() -> None:
    """This ECM ignores short frames outright — no response, not even a
    negative one — which is indistinguishable from an unsupported service."""
    for count in range(1, MAX_BATCHED_DIDS + 1):
        payload = build_batched_did_request_payload(0x22, list(range(0x1100, 0x1100 + count)))
        assert len(payload) == 8


def test_batched_did_request_rejects_more_than_fits_one_frame() -> None:
    """1 PCI + 1 service + 3 x 2 DID bytes = 8. A fourth DID cannot fit."""
    assert MAX_BATCHED_DIDS == 3
    with pytest.raises(ValueError, match="at most"):
        build_batched_did_request_payload(0x22, [0x1101, 0x1102, 0x1103, 0x1104])


def test_batched_did_request_rejects_duplicates() -> None:
    """A duplicate makes the response ambiguous to walk and wastes a slot."""
    with pytest.raises(ValueError, match="duplicate"):
        build_batched_did_request_payload(0x22, [0x1101, 0x1101])


def test_batched_did_request_rejects_out_of_range_identifiers() -> None:
    with pytest.raises(ValueError, match="out of range"):
        build_batched_did_request_payload(0x22, [0x1_0000])


def test_batched_did_request_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one"):
        build_batched_did_request_payload(0x22, [])


# ---------------------------------------------------------------------------
# splitting the response
# ---------------------------------------------------------------------------


def test_split_walks_every_did_group() -> None:
    payload = bytes([0x11, 0x01, 0x8E, 0x12, 0x19, 0x02, 0xEE, 0x12, 0x08, 0x16, 0xAC])
    readings, clean = split_batched_did_response(payload, width_for)
    assert clean is True
    assert readings == {0x1101: b"\x8e", 0x1219: b"\x02\xee", 0x1208: b"\x16\xac"}


def test_split_stops_clean_false_on_an_unknown_width() -> None:
    """The whole point. An unknown width means the next boundary cannot be
    found, so everything after it would be read off the wrong bytes. What was
    walked before the unknown is still trustworthy and is returned; the flag
    is what tells the caller not to trust completeness."""
    payload = bytes([0x11, 0x01, 0x8E, 0x9F, 0x9F, 0x01, 0x02, 0x12, 0x19, 0x02, 0xEE])
    readings, clean = split_batched_did_response(payload, width_for)
    assert clean is False
    assert readings == {0x1101: b"\x8e"}
    assert 0x1219 not in readings


def test_split_stops_clean_false_on_a_truncated_payload() -> None:
    payload = bytes([0x11, 0x01, 0x8E, 0x12, 0x19, 0x02])  # 0x1219 wants 2, has 1
    readings, clean = split_batched_did_response(payload, width_for)
    assert clean is False
    assert readings == {0x1101: b"\x8e"}


def test_split_stops_clean_false_on_a_dangling_identifier_byte() -> None:
    """A single trailing byte cannot even form a DID."""
    payload = bytes([0x11, 0x01, 0x8E, 0x12])
    readings, clean = split_batched_did_response(payload, width_for)
    assert clean is False
    assert readings == {0x1101: b"\x8e"}


def test_split_keys_off_the_response_not_the_request() -> None:
    """An ECU may omit a DID it does not support rather than reject the whole
    batch. Matching positionally against the request would then misattribute
    every later value, so the walk reads identifiers out of the response."""
    payload = bytes([0x12, 0x19, 0x02, 0xEE, 0x11, 0x01, 0x8E])  # reversed, 0x1208 absent
    readings, clean = split_batched_did_response(payload, width_for)
    assert clean is True
    assert readings == {0x1219: b"\x02\xee", 0x1101: b"\x8e"}


# ---------------------------------------------------------------------------
# the sweep layer
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Records how each DID was actually read: batched or individually."""

    def __init__(self, values: dict[int, bytes]) -> None:
        self._values = values
        self.batched_groups: list[list[int]] = []
        self.single_reads: list[int] = []

    def read_did(self, did: int) -> bytes | None:
        self.single_reads.append(did)
        return self._values.get(did)

    def read_dids(
        self, dids: Sequence[int], byte_length_for: Callable[[int], int | None]
    ) -> BatchRead:
        self.batched_groups.append(list(dids))
        payload = b"".join(
            bytes([d >> 8, d & 0xFF]) + self._values[d] for d in dids if d in self._values
        )
        readings, clean = split_batched_did_response(payload, byte_length_for)
        return BatchRead(readings=readings, clean=clean)


def test_sweep_batches_known_widths_and_falls_back_for_unknown() -> None:
    """A mixed list must still be correct: unknown-width DIDs lose the
    same-instant guarantee but must not desync the ones that have it."""
    values = {0x1101: b"\x8e", 0x1219: b"\x02\xee", 0x1208: b"\x16\xac", 0x1999: b"\x01"}
    transport = _FakeTransport(values)
    snapshot = sweep_dids_batched(
        transport, [0x1101, 0x1219, 0x1208, 0x1999], width_for, batch_size=3
    )
    assert snapshot.values == values
    assert transport.batched_groups == [[0x1101, 0x1219, 0x1208]]
    assert transport.single_reads == [0x1999]  # unknown width -> individual


def test_sweep_rereads_individually_when_the_ecu_omits_a_did() -> None:
    """A gap in a batched response must be re-read, never inferred."""
    values = {0x1101: b"\x8e", 0x1208: b"\x16\xac"}
    transport = _FakeTransport(values)
    snapshot = sweep_dids_batched(transport, [0x1101, 0x1219, 0x1208], width_for, batch_size=3)
    assert snapshot.values == values
    assert 0x1219 in transport.single_reads


class _SingleOnlyTransport:
    """A bare Mode22Transport — no ``read_dids``, e.g. the ELM327 path."""

    def __init__(self, values: dict[int, bytes]) -> None:
        self._values = values
        self.single_reads: list[int] = []

    def read_did(self, did: int) -> bytes | None:
        self.single_reads.append(did)
        return self._values.get(did)


def test_sweep_degrades_to_single_reads_without_batch_support() -> None:
    values = {0x1101: b"\x8e", 0x1219: b"\x02\xee"}
    transport = _SingleOnlyTransport(values)
    snapshot = sweep_dids_batched(transport, [0x1101, 0x1219], width_for, batch_size=3)
    assert snapshot.values == values
    assert transport.single_reads == [0x1101, 0x1219]


def test_sweep_batch_size_is_capped_at_the_frame_ceiling() -> None:
    values = {0x1101: b"\x8e", 0x1219: b"\x02\xee", 0x1208: b"\x16\xac", 0x1103: b"\xa5"}
    transport = _FakeTransport(values)
    sweep_dids_batched(transport, list(values), width_for, batch_size=99)
    assert all(len(g) <= MAX_BATCHED_DIDS for g in transport.batched_groups)


def _frames(body: bytes) -> list[bytes]:
    if len(body) <= 7:
        return [bytes([len(body), *body]) + bytes(7 - len(body))]
    out = [bytes([0x10 | (len(body) >> 8), len(body) & 0xFF]) + body[:6]]
    rest, sequence = body[6:], 1
    while rest:
        chunk, rest = rest[:7], rest[7:]
        out.append(bytes([0x20 | sequence, *chunk]) + bytes(7 - len(chunk)))
        sequence += 1
    return out


class _SchedulerDidEcu:
    """Answers Mode 22 singles and batches (and RPM) instantly.

    With ``clock`` (a :class:`FakeClock` the test has installed), a receive
    with nothing pending costs its timeout in simulated time, so a run bounded
    by a ``clock.monotonic()`` deadline does not depend on machine load
    (T01.8).
    """

    values = {0x1101: b"\x8e", 0x1219: b"\x02\xee", 0x1208: b"\x16\xac"}

    def __init__(
        self,
        *,
        reject_batches: bool = False,
        responder_id: int = 0x7E8,
        clock: FakeClock | None = None,
    ) -> None:
        self.clock = clock
        self.channel_info = "test"
        self.reject_batches = reject_batches
        self.sent: list[can.Message] = []
        self.pending: list[bytes] = []
        self.did_batches = 0
        self.responder_id = responder_id

    def send(self, message: can.Message) -> None:
        self.sent.append(message)
        data = bytes(message.data)
        if data[1] == 0x30:  # ISO-TP flow control
            return
        if data[1] == 0x22:
            count = (data[0] - 1) // 2
            dids = [(data[i] << 8) | data[i + 1] for i in range(2, 2 + count * 2, 2)]
            if count > 1:
                self.did_batches += 1
                if self.reject_batches:
                    return
            body = b"\x62" + b"".join(
                bytes([did >> 8, did & 0xFF]) + self.values[did] for did in dids
            )
            self.pending.extend(_frames(body))
        elif data[1] == 0x01:  # keep the one explicit Mode-01 member healthy
            self.pending.extend(_frames(b"\x41\x0c\x0b\xb8"))

    def recv(self, timeout: float | None = None) -> can.Message | None:
        if not self.pending:
            if self.clock is not None and timeout:
                self.clock.advance(timeout)
            return None
        return can.Message(
            arbitration_id=self.responder_id, data=self.pending.pop(0), is_extended_id=False
        )


def _scheduler_spec(**overrides: Any) -> CanObdSpec:
    values: dict[str, Any] = {
        "can": CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        "poll_rate_hz": 30.0,
        "pids": ("RPM",),
        "dids": ("1101", "1219", "1208"),
        "request_timeout_seconds": 0.01,
        "did_batch_max_dids": 3,
        "did_response_widths": WIDTHS,
    }
    values.update(overrides)
    return CanObdSpec(**values)


def test_active_scheduler_batches_known_width_dids_with_full_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On a fake clock (T01.25): 0.2 s of wall clock saw no DID batch at all in
    # a parallel push-gate run at load average ~8.8 (`0 > 0`, 2026-10-12), the
    # shape T01.8 and T01.16 fixed in the tests below.
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _SchedulerDidEcu(clock=clock)
    readings: list[Any] = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _scheduler_spec(),
        on_frame=lambda _message: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 0.5,
    )
    assert bus.did_batches > 0
    assert stats.did_batched_requests > 0
    dids = [reading for reading in readings if reading.pid.startswith("DID_")]
    assert {reading.pid for reading in dids} == {"DID_1101", "DID_1219", "DID_1208"}
    assert all(reading.responder_id == 0x7E8 for reading in dids)
    assert all(reading.acquired_at is not None and reading.round_id is not None for reading in dids)


def test_known_answer_decode_matches_single_and_batched_scheduler_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On a fake clock (T01.16): 0.2 s of wall clock read no DID at all on a
    # slow CI runner (`assert {} == {...}`, 2026-10-03), as T01.8 found for
    # the test below.
    decoded: list[dict[str, float]] = []
    for batch_max in (1, 3):
        clock = FakeClock()
        install_clock(monkeypatch, clock)
        readings: list[Any] = []
        run_can_obd_loop(
            _SchedulerDidEcu(clock=clock),  # type: ignore[arg-type]
            _scheduler_spec(did_batch_max_dids=batch_max),
            on_frame=lambda _message: None,
            on_reading=readings.append,
            stop_event=threading.Event(),
            deadline_monotonic=clock.monotonic() + 0.5,
        )
        decoded.append(
            {
                reading.pid: reading.value
                for reading in readings
                if reading.pid in {"DID_1101", "DID_1219", "DID_1208"}
            }
        )

    assert decoded[0] == decoded[1]
    assert set(decoded[0]) == {"DID_1101", "DID_1219", "DID_1208"}


def test_rejected_did_batches_fall_back_without_backing_off_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On a fake clock (T01.8): the last DID's first single read lands at
    # 0.31 s, after the rejected batches and the DTC polls time out; 0.5 s of
    # wall clock was 1.6x that.
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _SchedulerDidEcu(reject_batches=True, clock=clock)
    readings: list[Any] = []
    snapshots: list[Any] = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _scheduler_spec(),
        on_frame=lambda _message: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 0.5,
        on_scheduler_metrics=snapshots.append,
        scheduler_metrics_interval_seconds=0.02,
    )
    assert stats.did_batch_failures >= 3
    assert stats.did_batch_size_narrowings == 1
    assert {reading.pid for reading in readings if reading.pid.startswith("DID_")} == {
        "DID_1101",
        "DID_1219",
        "DID_1208",
    }
    fallback = [
        snapshot.resolved_plan
        for snapshot in snapshots
        if snapshot.resolved_plan is not None and snapshot.resolved_plan["batch_max"]["mode22"] == 1
    ]
    assert fallback
    demand = fallback[-1]["diagnostic_requests_per_second"]
    assert demand["best_case_batched"] == demand["single_request_fallback"]


def test_physical_did_batch_rejects_a_different_ecu_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On a fake clock (T01.25): the same 0.15 s wall-clock exposure as the
    # batching test above; one stall before the first batch and nothing fails.
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _SchedulerDidEcu(responder_id=0x7E9, clock=clock)
    readings: list[Any] = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _scheduler_spec(request_timeout_seconds=0.005),
        on_frame=lambda _message: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 0.5,
    )
    assert stats.did_batch_failures > 0
    assert not any(reading.pid.startswith("DID_") for reading in readings)


class _SleepyDidEcu(_SchedulerDidEcu):
    """Silent to every request until ``wake_after`` sends (key off at boot)."""

    def __init__(self, wake_after: int, *, clock: FakeClock | None = None) -> None:
        super().__init__(clock=clock)
        self.wake_after = wake_after
        self.sends = 0
        self.awake_did_batches = 0

    def send(self, message: can.Message) -> None:
        self.sends += 1
        if self.sends <= self.wake_after:
            self.sent.append(message)
            return
        before = self.did_batches
        super().send(message)
        self.awake_did_batches += self.did_batches - before


def test_did_batch_silence_from_a_sleeping_ecu_does_not_disable_batching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On a fake clock (T01.8): 14 silent sends (nine timeouts) come before the
    # first awake DID batch at 0.44 s; 0.8 s of wall clock was 1.8x that.
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _SleepyDidEcu(wake_after=14, clock=clock)
    readings: list[Any] = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _scheduler_spec(),
        on_frame=lambda _message: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 0.8,
    )
    assert stats.did_batch_failures >= 3
    assert stats.did_batch_failures_ecu_silent == stats.did_batch_failures
    assert stats.did_batch_size_narrowings == 0
    assert bus.awake_did_batches > 0, "DID batching must survive the ECU waking up"
    assert {reading.pid for reading in readings if reading.pid.startswith("DID_")} == {
        "DID_1101",
        "DID_1219",
        "DID_1208",
    }
