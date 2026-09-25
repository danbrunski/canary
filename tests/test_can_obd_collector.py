"""Tests for the Canable-native OBD streaming collector."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import can
import pytest

from src.can_ids import parse_standard_can_id
from src.collection.can_obd_collector import (
    AUTO_BASELINE_PID_NAMES,
    MAX_RESPONSE_PENDING_EXTENSIONS,
    CanObdSpec,
    DidSessionSpec,
    ObdSchedulerMetrics,
    _PollRequest,
    _query_with_tap,
    _response_to_reading,
    build_poll_requests,
    parse_did_token,
    parse_pid_token,
    record_can_obd,
    resolve_did_session_spec,
    run_can_obd_loop,
)
from src.collection.custom_pids import CustomPid
from src.collection.isotp import DEFAULT_REQUEST_ID, ObdResponse
from src.collection.obd_collector import JsonlReadingWriter
from src.collection.obd_registry import MODE01_REGISTRY
from src.collection.obd_scheduler import (
    AUTO_MISS_BACKOFF_SECONDS,
    CAPTURE_MISS_BACKOFF_SECONDS,
    FAST_EXIT_RETRY_MIN_SECONDS,
    MAX_MISS_BACKOFF_SECONDS,
    CanObdLoopStats,
    ForcePollTrigger,
    FreezeFrameTrigger,
    SessionControlResult,
    _PollRequestState,
    _select_next_due_request,
    _update_response_state,
)
from src.collection.reconnect import ReconnectPolicy
from src.config import CanBusConfig
from src.session import make_session_dir
from tests.capture_round_fakes import FakeClock, install_clock
from tests.waits import StopWhen, hang_guard_deadline


class _StaticDiscovery:
    def query_supported_mode_01_pids(self) -> tuple[int, ...]:
        return (0x0C, 0x05, 0x99)  # RPM + coolant + unsupported-by-decoder


class _FakeBus:
    def __init__(self) -> None:
        self.sent: list[can.Message] = []
        self._responses: list[can.Message] = []

    def send(self, msg: can.Message) -> None:
        self.sent.append(msg)
        data = bytes(msg.data)
        if msg.arbitration_id == 0x7DF and data[:3] == bytes([0x02, 0x01, 0x0C]):
            self._responses.append(
                can.Message(
                    arbitration_id=0x7E8,
                    data=bytes([0x04, 0x41, 0x0C, 0x0A, 0xF0, 0x00, 0x00, 0x00]),
                    is_extended_id=False,
                )
            )
        elif msg.arbitration_id == 0x7E0 and data[:4] == bytes([0x03, 0x22, 0x11, 0x01]):
            self._responses.append(
                can.Message(
                    arbitration_id=0x7E8,
                    data=bytes([0x05, 0x62, 0x11, 0x01, 0x12, 0x34, 0x00, 0x00]),
                    is_extended_id=False,
                )
            )

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        if self._responses:
            return self._responses.pop(0)
        if timeout:
            time.sleep(min(timeout, 0.005))
        return None


class _NegativeThenPositiveBus:
    def __init__(self) -> None:
        self.sent: list[can.Message] = []
        self._responses: list[can.Message] = []

    def send(self, msg: can.Message) -> None:
        self.sent.append(msg)
        data = bytes(msg.data)
        if msg.arbitration_id == 0x7DF and data[:3] == bytes([0x02, 0x01, 0x04]):
            self._responses.append(
                can.Message(
                    arbitration_id=0x7E8,
                    data=bytes([0x03, 0x7F, 0x01, 0x12, 0x00, 0x00, 0x00, 0x00]),
                    is_extended_id=False,
                )
            )
        elif msg.arbitration_id == 0x7DF and data[:3] == bytes([0x02, 0x01, 0x0C]):
            self._responses.append(
                can.Message(
                    arbitration_id=0x7E8,
                    data=bytes([0x04, 0x41, 0x0C, 0x0A, 0xF0, 0x00, 0x00, 0x00]),
                    is_extended_id=False,
                )
            )

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        if self._responses:
            return self._responses.pop(0)
        if timeout:
            time.sleep(min(timeout, 0.005))
        return None


class _TieredPidBus:
    def __init__(self) -> None:
        self.sent: list[can.Message] = []
        self._responses: list[can.Message] = []

    def send(self, msg: can.Message) -> None:
        self.sent.append(msg)
        data = bytes(msg.data)
        if msg.arbitration_id != 0x7DF or data[:2] != bytes([0x02, 0x01]):
            return
        pid = data[2]
        if pid == 0x0C:  # RPM, fast
            payload = bytes([0x04, 0x41, pid, 0x0A, 0xF0, 0x00, 0x00, 0x00])
        elif pid == 0x2F:  # FUEL_LEVEL, slow
            payload = bytes([0x03, 0x41, pid, 0x80, 0x00, 0x00, 0x00, 0x00])
        else:
            return
        self._responses.append(
            can.Message(arbitration_id=0x7E8, data=payload, is_extended_id=False)
        )

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        if self._responses:
            return self._responses.pop(0)
        if timeout:
            time.sleep(min(timeout, 0.001))
        return None


class _DtcCapableBus:
    """Answers Mode 01 PID 0x01 (MIL/count) plus Mode 03/07/0A (Task 1.3)."""

    def __init__(self) -> None:
        self.sent: list[can.Message] = []
        self._responses: list[can.Message] = []

    def send(self, msg: can.Message) -> None:
        self.sent.append(msg)
        data = bytes(msg.data)
        if msg.arbitration_id != 0x7DF:
            return
        if data[:3] == bytes([0x02, 0x01, 0x01]):
            # MIL on (bit 7) + count=3 in bits 0-6 -> 0x83.
            payload = bytes([0x03, 0x41, 0x01, 0x83, 0x00, 0x00, 0x00, 0x00])
        elif data[:2] == bytes([0x01, 0x03]):
            # Stored: P0420, C0510.
            payload = bytes([0x06, 0x43, 0x02, 0x04, 0x20, 0x45, 0x10, 0x00])
        elif data[:2] == bytes([0x01, 0x07]):
            # Pending: P0123.
            payload = bytes([0x04, 0x47, 0x01, 0x01, 0x23, 0x00, 0x00, 0x00])
        elif data[:2] == bytes([0x01, 0x0A]):
            # Permanent: none.
            payload = bytes([0x02, 0x4A, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        else:
            return
        self._responses.append(
            can.Message(arbitration_id=0x7E8, data=payload, is_extended_id=False)
        )

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        if self._responses:
            return self._responses.pop(0)
        if timeout:
            time.sleep(min(timeout, 0.005))
        return None


class _FreezeFrameCapableBus:
    """Answers Mode 02 (Task 3.3) for the five freeze-frame PIDs, frame #0."""

    #: pid -> (length_byte, formula_bytes) for a `43 <pid> 00 <formula...>` reply.
    _RESPONSES: dict[int, bytes] = {
        0x0C: bytes([0x0A, 0xF0]),  # RPM: (0x0AF0)/4 = 700.0
        0x0D: bytes([0x50]),  # SPEED: 80 km/h
        0x05: bytes([0x5A]),  # COOLANT_TEMP: 90 - 40 = 50 C
        0x04: bytes([0x80]),  # ENGINE_LOAD: 128*100/255
        0x11: bytes([0x40]),  # THROTTLE_POS: 64*100/255
    }

    def __init__(self) -> None:
        self.sent: list[can.Message] = []
        self._responses: list[can.Message] = []

    def send(self, msg: can.Message) -> None:
        self.sent.append(msg)
        data = bytes(msg.data)
        if msg.arbitration_id != 0x7DF or data[:2] != bytes([0x02, 0x02]):
            return
        pid = data[2]
        formula = self._RESPONSES.get(pid)
        if formula is None:
            return
        body = bytes([0x42, pid, 0x00]) + formula
        payload = bytes([len(body)]) + body
        payload = payload + bytes(8 - len(payload))
        self._responses.append(
            can.Message(arbitration_id=0x7E8, data=payload, is_extended_id=False)
        )

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        if self._responses:
            return self._responses.pop(0)
        if timeout:
            time.sleep(min(timeout, 0.005))
        return None


class _SilentBus:
    """Never answers anything — models a stale/no_data bus."""

    def __init__(self) -> None:
        self.sent: list[can.Message] = []

    def send(self, msg: can.Message) -> None:
        self.sent.append(msg)

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        if timeout:
            time.sleep(min(timeout, 0.005))
        return None


class _AlwaysNegativeBus:
    """Always answers with a formal `7F 01 31` refusal — never silent."""

    def __init__(self) -> None:
        self.sent: list[can.Message] = []

    def send(self, msg: can.Message) -> None:
        self.sent.append(msg)

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        return can.Message(
            arbitration_id=0x7E8,
            data=bytes([0x03, 0x7F, 0x01, 0x31, 0x00, 0x00, 0x00, 0x00]),
            is_extended_id=False,
        )


def test_query_with_tap_negative_response_is_distinct_from_timeout() -> None:
    """A formal 7F refusal must be visible to the caller, not just `None` —
    the exact conflation `_query_with_tap` used to have with a true timeout."""
    request = _PollRequest(
        kind="pid", service=0x01, identifier=0x0C, request_id=DEFAULT_REQUEST_ID, channel="RPM"
    )
    stats = CanObdLoopStats()
    result = _query_with_tap(
        _AlwaysNegativeBus(),  # type: ignore[arg-type]
        request,
        timeout_seconds=0.2,
        on_frame=lambda _msg: None,
        stop_event=threading.Event(),
        stats=stats,
        deadline_monotonic=time.monotonic() + 0.5,
    )
    assert result.response is None
    assert result.negative_response_code == 0x31


def test_query_with_tap_true_timeout_has_no_negative_response_code() -> None:
    request = _PollRequest(
        kind="pid", service=0x01, identifier=0x0C, request_id=DEFAULT_REQUEST_ID, channel="RPM"
    )
    stats = CanObdLoopStats()
    result = _query_with_tap(
        _SilentBus(),  # type: ignore[arg-type]
        request,
        timeout_seconds=0.05,
        on_frame=lambda _msg: None,
        stop_event=threading.Event(),
        stats=stats,
        deadline_monotonic=time.monotonic() + 0.5,
    )
    assert result.response is None
    assert result.negative_response_code is None


def test_query_with_tap_mode06_keeps_monitor_id_on_wire() -> None:
    """Mode 06 requires a MID; MID 00 must serialize as ``02 06 00``."""
    bus = _SilentBus()
    request = _PollRequest(
        kind="service",
        service=0x06,
        identifier=0x00,
        request_id=DEFAULT_REQUEST_ID,
        channel="MONITOR_RESULTS",
        poll_tier="dtc",
    )

    _query_with_tap(
        bus,  # type: ignore[arg-type]
        request,
        timeout_seconds=0.01,
        on_frame=lambda _msg: None,
        stop_event=threading.Event(),
        stats=CanObdLoopStats(),
        deadline_monotonic=time.monotonic() + 0.1,
    )

    assert bytes(bus.sent[0].data) == bytes([0x02, 0x06, 0x00, 0, 0, 0, 0, 0])


def test_query_with_tap_dtc_services_remain_identifierless() -> None:
    """Mode 03/07/0A are still one-byte service requests, unlike Mode 06."""
    bus = _SilentBus()
    request = _PollRequest(
        kind="dtc",
        service=0x03,
        identifier=0x00,
        request_id=DEFAULT_REQUEST_ID,
        channel="DTC_STORED",
        poll_tier="dtc",
    )

    _query_with_tap(
        bus,  # type: ignore[arg-type]
        request,
        timeout_seconds=0.01,
        on_frame=lambda _msg: None,
        stop_event=threading.Event(),
        stats=CanObdLoopStats(),
        deadline_monotonic=time.monotonic() + 0.1,
    )

    assert bytes(bus.sent[0].data) == bytes([0x01, 0x03, 0, 0, 0, 0, 0, 0])


def test_mode06_discovery_retries_after_successful_obd_contact(caplog) -> None:  # type: ignore[no-untyped-def]
    """Mode 06 support is silent at startup, then answers once the ECU is in
    contact. Discovery is loop-driven (C06.4): the silent probe parks the
    walk, the first Mode 01 answer re-arms it, and the next probe installs
    the MIDs — every probe on the bus, none through a side channel."""
    caplog.set_level("INFO")

    class _Bus(_SilentBus):
        def __init__(self) -> None:
            super().__init__()
            self.responses: list[can.Message] = []
            self.mode01_answered = False
            self.support_probes = 0

        def send(self, msg: can.Message) -> None:
            super().send(msg)
            data = bytes(msg.data)
            if data[:3] == bytes([0x02, 0x01, 0x0C]):
                self.mode01_answered = True
                self.responses.append(
                    can.Message(
                        arbitration_id=0x7E8,
                        data=bytes([0x04, 0x41, 0x0C, 0x0A, 0xF0, 0, 0, 0]),
                        is_extended_id=False,
                    )
                )
            elif data[:3] == bytes([0x02, 0x06, 0x00]):
                self.support_probes += 1
                if self.mode01_answered:
                    bits = 1 << 31  # MID 0x01 claimed, no continuation
                    self.responses.append(
                        can.Message(
                            arbitration_id=0x7E8,
                            data=bytes([0x06, 0x46, 0x00, *bits.to_bytes(4, "big"), 0]),
                            is_extended_id=False,
                        )
                    )
            elif data[:3] == bytes([0x02, 0x06, 0x01]):
                self.responses.append(
                    can.Message(
                        arbitration_id=0x7E8,
                        data=bytes([0x03, 0x46, 0x01, 0x0C, 0, 0, 0, 0]),
                        is_extended_id=False,
                    )
                )

        def recv(self, timeout: float = 0.0) -> can.Message | None:
            if self.responses:
                return self.responses.pop(0)
            return super().recv(timeout)

    bus = _Bus()
    stop = threading.Event()
    readings: list[Any] = []

    def on_reading(reading: Any) -> None:
        readings.append(reading)
        if reading.pid == "MONITOR_RESULTS":
            stop.set()

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=100.0,
        pids=("RPM",),
        request_timeout_seconds=0.05,
        dtc_poll_interval_seconds=0.0,
        service_poll_interval_seconds=300.0,
        service_mids="auto",
    )
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=on_reading,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 2.0,
    )

    assert bus.support_probes == 2, "the silent probe must be retried once contact is made"
    assert any(bytes(message.data)[:3] == bytes([0x02, 0x06, 0x01]) for message in bus.sent)
    assert any(reading.pid == "MONITOR_RESULTS" for reading in readings)
    assert stats.discovery_requests_sent == 2
    assert stats.discovery_timeouts == 1 and stats.discovery_responses == 1
    assert "mode06 support discovery: group 0x00 silent" in caplog.text
    assert "Mode 06 discovery complete" in caplog.text


def test_parse_pid_token_accepts_auto_names_and_hex() -> None:
    assert parse_pid_token("auto") is None
    assert parse_pid_token("RPM") == 0x0C
    assert parse_pid_token("0x0c") == 0x0C
    assert parse_pid_token("0C") == 0x0C
    with pytest.raises(ValueError, match="unknown"):
        parse_pid_token("NOPE")


def test_parse_did_and_can_id() -> None:
    assert parse_did_token("1101") == 0x1101
    assert parse_did_token("ECM_BASE_FUEL_SCHEDULE_CANDIDATE") == 0x1209
    assert parse_standard_can_id("7E0") == 0x7E0
    assert parse_standard_can_id("0x7e1") == 0x7E1
    with pytest.raises(ValueError, match="4-character"):
        parse_did_token("110")


def test_dids_reaching_one_identifier_are_polled_once() -> None:
    """A name and its alias are one register, not two channels.

    `ECM_MAF_GPS` kept its retired `ECM_BASE_FUEL_SCHEDULE_CANDIDATE` name as a
    catalogue alias, so both could appear in a DID list and the scheduler issued
    a request for each. The 2026-08-10 all-DID capture polled 0x1209 at 29.4 Hz
    while every other channel got 14.7 — a seventh of the round spent on a
    duplicate whose rows are indistinguishable from the original's, because the
    channel name is derived from the identifier. Mode 01 already deduplicated
    this way; DIDs did not.
    """
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=(),
        # Two catalogue names for one register, then the same DID twice by hex.
        dids=("ECM_MAF_GPS", "ECM_BASE_FUEL_SCHEDULE_CANDIDATE", "1208", "1208"),
    )
    requests = build_poll_requests(_StaticDiscovery(), spec)
    did_channels = [r.channel for r in requests if r.kind == "did"]

    assert did_channels == ["DID_1209", "DID_1208"]


def test_build_poll_requests_combines_auto_explicit_and_dids() -> None:
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=("auto", "SPEED"),
        dids=("1101",),
    )
    requests = build_poll_requests(_StaticDiscovery(), spec)
    channels = [r.channel for r in requests]
    assert channels[:2] == ["RPM", "COOLANT_TEMP"]
    assert "SPEED" in channels
    # DID requests are appended after PIDs, DTC requests (Task 1.3) after that.
    assert "DID_1101" in channels
    assert channels.index("DID_1101") < channels.index("DTC_STORED")


def test_build_poll_requests_assigns_registry_poll_tiers() -> None:
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=("RPM", "COOLANT_TEMP", "FUEL_LEVEL"),
    )

    tiers = {r.channel: r.poll_tier for r in build_poll_requests(_StaticDiscovery(), spec)}

    assert tiers["RPM"] == "fast"
    assert tiers["COOLANT_TEMP"] == "medium"
    assert tiers["FUEL_LEVEL"] == "slow"


def test_build_poll_requests_channel_and_tier_come_from_the_same_registry_row() -> None:
    """Task 1.1: the scheduler used to read the channel name from
    ``PID_DECODERS`` but the poll tier from ``MODE01_REGISTRY`` — two
    separately-keyed tables. Both must now trace back to the same
    ``MODE01_REGISTRY`` row for every produced pid request."""
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=("auto",),
    )
    requests = [r for r in build_poll_requests(_StaticDiscovery(), spec) if r.kind == "pid"]
    assert requests  # sanity: the auto set isn't empty
    for request in requests:
        definition = MODE01_REGISTRY[request.identifier]
        assert request.channel == definition.channel
        assert request.poll_tier == definition.poll_tier


def test_auto_poll_requests_probe_full_decoded_pid_catalog_when_discovery_is_sparse() -> None:
    class _SparseDiscovery:
        def query_supported_mode_01_pids(self) -> tuple[int, ...]:
            return (0x2E, 0x2F)

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=("auto",),
    )

    channels = [r.channel for r in build_poll_requests(_SparseDiscovery(), spec)]

    assert "EVAPORATIVE_PURGE" in channels
    assert "FUEL_LEVEL" in channels
    for name in AUTO_BASELINE_PID_NAMES:
        assert name in channels
    assert len(channels) >= 24


def test_monitor_status_pid_is_in_the_default_auto_poll_set() -> None:
    """Task 1.2: PID 0x01 (MIL/DTC count) rides the default auto poll set."""
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=("auto",),
    )
    requests = {r.channel: r for r in build_poll_requests(_StaticDiscovery(), spec)}
    assert requests["MONITOR_STATUS"].poll_tier == "slow"


# ---------------------------------------------------------------------------
# Priority lane + never-answered backoff
# ---------------------------------------------------------------------------


def test_build_poll_requests_marks_mode_critical_channels_priority() -> None:
    """RPM and throttle hold the lane; SPEED gave it up on 2026-08-07.

    The lane costs the other tiers a share of a contended bus, so a channel
    earns it only if nothing cheaper can keep the segmenter fed. SPEED cannot
    clear that bar any more: the segmenter coalesces it onto `gps:speed`,
    which is already on the wire and costs no request slot. Throttle has no
    passive fallback and is required by event/operating-mode classification.
    """
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=("RPM", "SPEED", "THROTTLE_POS", "COOLANT_TEMP"),
    )

    priority = {r.channel: r.priority for r in build_poll_requests(_StaticDiscovery(), spec)}

    assert priority["RPM"] is True
    assert priority["THROTTLE_POS"] is True
    assert priority["SPEED"] is False
    assert priority["COOLANT_TEMP"] is False


def test_build_poll_requests_priority_channels_are_configurable() -> None:
    can_config = CanBusConfig(interface="virtual", channel="test", bitrate=500000)
    pids = ("RPM", "COOLANT_TEMP")

    opted_out = build_poll_requests(
        _StaticDiscovery(),
        CanObdSpec(can=can_config, pids=pids, priority_channels=()),
    )
    reassigned = build_poll_requests(
        _StaticDiscovery(),
        CanObdSpec(can=can_config, pids=pids, priority_channels=("coolant_temp",)),
    )

    assert not any(r.priority for r in opted_out)
    assert {r.channel for r in reassigned if r.priority} == {"COOLANT_TEMP"}


def test_active_profile_reassigns_the_priority_lane_used_for_selection() -> None:
    from src.collection import tuning_profile
    from src.collection.capture_profiles import CaptureProfile

    requests = build_poll_requests(
        _StaticDiscovery(),
        CanObdSpec(
            can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
            pids=("RPM", "COOLANT_TEMP"),
        ),
    )
    states = [
        _PollRequestState(
            next_due_at=10.0 if request.channel == "COOLANT_TEMP" else 0.0,
            suppressed_until=(float("inf") if request.kind != "pid" else 0.0),
        )
        for request in requests
    ]
    tuning_profile.activate(
        CaptureProfile(
            name="coolant-priority",
            mode01={"RPM": 1.0, "COOLANT_TEMP": 1.0},
            priority_channels=("COOLANT_TEMP",),
            storage_prefix="_capture-coolant-priority",
        ),
        source="test",
    )
    try:
        selected = _select_next_due_request(requests, states, 20.0)
        assert selected is not None
        assert selected[1].channel == "COOLANT_TEMP"
    finally:
        tuning_profile.set_active(False, source="test")


def test_priority_request_wins_a_contended_slot_over_a_more_overdue_one() -> None:
    """The fix for operating-mode flapping: when the plan asks for more than
    the bus can serve every request is permanently overdue, so plain
    most-overdue-first degrades RPM along with everything else and the
    segmenter loses the signal every mode gates on."""
    now = 1_000.0
    requests = [
        _PollRequest(
            kind="pid",
            service=0x01,
            identifier=0x2F,
            request_id=DEFAULT_REQUEST_ID,
            channel="FUEL_LEVEL",
            poll_tier="slow",
        ),
        _PollRequest(
            kind="pid",
            service=0x01,
            identifier=0x0C,
            request_id=DEFAULT_REQUEST_ID,
            channel="RPM",
            poll_tier="fast",
            priority=True,
        ),
    ]
    states = [
        _PollRequestState(next_due_at=now - 60.0),  # far more overdue
        _PollRequestState(next_due_at=now - 0.5),
    ]

    selection = _select_next_due_request(requests, states, now)

    assert selection is not None
    assert selection[1].channel == "RPM"


def test_priority_request_does_not_starve_others_before_it_is_due() -> None:
    now = 1_000.0
    requests = [
        _PollRequest(
            kind="pid",
            service=0x01,
            identifier=0x2F,
            request_id=DEFAULT_REQUEST_ID,
            channel="FUEL_LEVEL",
            poll_tier="slow",
        ),
        _PollRequest(
            kind="pid",
            service=0x01,
            identifier=0x0C,
            request_id=DEFAULT_REQUEST_ID,
            channel="RPM",
            poll_tier="fast",
            priority=True,
        ),
    ]
    states = [
        _PollRequestState(next_due_at=now - 1.0),
        _PollRequestState(next_due_at=now + 0.5),  # not due yet
    ]

    selection = _select_next_due_request(requests, states, now)

    assert selection is not None
    assert selection[1].channel == "FUEL_LEVEL"


def test_miss_backoff_escalates_only_for_requests_that_never_answered() -> None:
    """Unsupported PIDs from the ``auto`` catalog probe are permanent, so a
    flat 30 s retry spends bus time forever on channels that will never
    answer. A channel that *has* answered keeps the flat backoff so a bus
    dropout still recovers promptly."""
    never_answered = _PollRequestState()
    for _ in range(3):
        _update_response_state(never_answered, emitted=False, now=0.0)
    assert never_answered.suppressed_until == AUTO_MISS_BACKOFF_SECONDS * 4

    answered_before = _PollRequestState(last_response_at=5.0)
    for _ in range(3):
        _update_response_state(answered_before, emitted=False, now=0.0)
    assert answered_before.suppressed_until == AUTO_MISS_BACKOFF_SECONDS


def test_focused_capture_retries_a_transient_miss_without_a_30_second_hole() -> None:
    """A single miss in today's 15 Hz DID capture cost about 450 samples.

    Capture channels are known responders, so retry promptly; consecutive
    misses still escalate to the normal ceiling instead of hammering a DID
    that became unavailable mid-capture.
    """
    state = _PollRequestState(last_response_at=5.0)

    delays: list[float] = []
    for _ in range(7):
        _update_response_state(state, emitted=False, now=0.0, focused_capture=True)
        delays.append(state.suppressed_until)

    assert delays == [
        CAPTURE_MISS_BACKOFF_SECONDS,
        2.0,
        4.0,
        8.0,
        16.0,
        AUTO_MISS_BACKOFF_SECONDS,
        AUTO_MISS_BACKOFF_SECONDS,
    ]

    _update_response_state(state, emitted=True, now=0.0, focused_capture=True)
    assert state.missed_responses == 0
    assert state.suppressed_until == 0.0


def test_miss_backoff_is_capped_and_resets_on_a_response() -> None:
    state = _PollRequestState()
    for _ in range(40):
        _update_response_state(state, emitted=False, now=0.0)
    assert state.suppressed_until == MAX_MISS_BACKOFF_SECONDS

    _update_response_state(state, emitted=True, now=0.0)
    assert state.suppressed_until == 0.0
    assert state.missed_responses == 0


# ---------------------------------------------------------------------------
# Periodic DTC poll (Task 1.3)
# ---------------------------------------------------------------------------


def test_build_poll_requests_adds_dtc_kind_for_services_03_07_0a() -> None:
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=(),
        dtc_poll_interval_seconds=300.0,
    )
    dtc_requests = [r for r in build_poll_requests(_StaticDiscovery(), spec) if r.kind == "dtc"]
    assert {(r.service, r.channel) for r in dtc_requests} == {
        (0x03, "DTC_STORED"),
        (0x07, "DTC_PENDING"),
        (0x0A, "DTC_PERMANENT"),
    }


def test_build_poll_requests_dtc_poll_disabled_when_interval_non_positive() -> None:
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=(),
        dtc_poll_interval_seconds=0.0,
    )
    assert not [r for r in build_poll_requests(_StaticDiscovery(), spec) if r.kind == "dtc"]


def test_dtc_poll_reads_codes_and_mil_with_engine_off_koeo() -> None:
    """Ignition-only (KOEO): the ECM answers OBD with RPM effectively 0/absent
    — nothing in the DTC poll depends on RPM, so it reads exactly like it
    would with the engine running."""
    bus = _DtcCapableBus()
    stop = threading.Event()
    readings: list[Any] = []

    def on_reading(reading: Any) -> None:
        readings.append(reading)
        seen = {r.pid for r in readings}
        if {"DTC_STORED", "DTC_PENDING", "DTC_PERMANENT", "MIL_STATUS"} <= seen:
            stop.set()

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=20.0,
        pids=("MONITOR_STATUS",),
        dtc_poll_interval_seconds=0.01,
        request_timeout_seconds=0.1,
    )

    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=on_reading,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 2.0,
    )

    by_channel = {r.pid: r for r in readings}
    assert by_channel["DTC_STORED"].value == "P0420,C0510"
    assert by_channel["DTC_PENDING"].value == "P0123"
    assert by_channel["DTC_PERMANENT"].value == ""
    assert by_channel["MIL_STATUS"].value is True
    assert by_channel["MONITOR_STATUS"].value == 3.0


def test_build_poll_requests_adds_service_kind_for_mode06() -> None:
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=(),
        service_poll_interval_seconds=300.0,
    )
    service_requests = [
        r for r in build_poll_requests(_StaticDiscovery(), spec) if r.kind == "service"
    ]
    assert {(r.service, r.channel) for r in service_requests} == {(0x06, "MONITOR_RESULTS")}
    assert service_requests[0].poll_tier == "dtc"


def test_build_poll_requests_uses_discovered_mode06_mids() -> None:
    class _Mode06Discovery(_StaticDiscovery):
        def query_supported_mode_06_mids(self) -> tuple[int, ...]:
            return (0x01, 0xA2, 0xA3)

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=(),
        service_poll_interval_seconds=300.0,
        service_mids="auto",
    )

    service_requests = [
        request
        for request in build_poll_requests(_Mode06Discovery(), spec)
        if request.kind == "service"
    ]
    assert [request.identifier for request in service_requests] == [0x01, 0xA2, 0xA3]


def test_build_poll_requests_service_poll_disabled_when_interval_non_positive() -> None:
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=(),
        service_poll_interval_seconds=0.0,
    )
    assert not [r for r in build_poll_requests(_StaticDiscovery(), spec) if r.kind == "service"]


def test_response_to_reading_service_kind_emits_hex_payload_and_misfire_counts() -> None:
    """Task 3.1 — Mode 06 is a multi-record sweep, doesn't fit the scalar
    TelemetryValue contract, so it's shipped as hex (like the `did` kind)
    for MonitorState to decode downstream. Task 3.2 — a per-cylinder
    misfire-count MID also lands as its own numeric reading so it flows
    through storage into the fingerprint-visible wide frame."""
    request = _PollRequest(
        kind="service",
        service=0x06,
        identifier=0xA2,
        request_id=DEFAULT_REQUEST_ID,
        channel="MONITOR_RESULTS",
        poll_tier="dtc",
    )
    response_payload = bytes([0x0C, 0x24, 0x00, 0x03, 0x00, 0x00, 0x00, 0xFF])
    payload = bytes([0xA2]) + response_payload
    response = ObdResponse(service=0x46, pid=0xA2, payload=response_payload, responder_id=0x7E8)
    readings = _response_to_reading(request, response)
    assert len(readings) == 2
    by_channel = {r.pid: r for r in readings}
    assert by_channel["MONITOR_RESULTS"].value == payload.hex()
    assert by_channel["MISFIRE_COUNT_CYL1"].value == 3.0


def test_build_poll_requests_freeze_frame_disabled_without_a_trigger() -> None:
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=(),
    )
    assert not [
        r for r in build_poll_requests(_StaticDiscovery(), spec) if r.kind == "freeze_frame"
    ]


def test_build_poll_requests_adds_freeze_frame_kind_when_triggered() -> None:
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=(),
        freeze_frame_trigger=FreezeFrameTrigger(),
    )
    ff_requests = [
        r for r in build_poll_requests(_StaticDiscovery(), spec) if r.kind == "freeze_frame"
    ]
    assert {r.channel for r in ff_requests} == {
        "FREEZE_FRAME_RPM",
        "FREEZE_FRAME_SPEED",
        "FREEZE_FRAME_COOLANT_TEMP",
        "FREEZE_FRAME_ENGINE_LOAD",
        "FREEZE_FRAME_THROTTLE_POS",
    }
    assert all(r.service == 0x02 and r.poll_tier == "on_demand" for r in ff_requests)


def test_response_to_reading_freeze_frame_strips_the_frame_number_byte() -> None:
    """Task 3.3 — Mode 02's response has a leading frame-number byte the
    Mode 01 formula bytes don't; the decoder must see only the formula."""
    request = _PollRequest(
        kind="freeze_frame",
        service=0x02,
        identifier=0x0C,  # RPM
        request_id=DEFAULT_REQUEST_ID,
        channel="FREEZE_FRAME_RPM",
        poll_tier="on_demand",
    )
    # frame#=0x00, then the RPM formula bytes (0x0A, 0xF0 -> 700.0 rpm).
    payload = bytes([0x00, 0x0A, 0xF0])
    response = ObdResponse(service=0x02, pid=0x0C, payload=payload, responder_id=0x7E8)
    readings = _response_to_reading(request, response)
    assert len(readings) == 1
    assert readings[0].pid == "FREEZE_FRAME_RPM"
    assert readings[0].value == pytest.approx(700.0)


class _ClockedFreezeFrameBus(_FreezeFrameCapableBus):
    """:class:`_FreezeFrameCapableBus` on a :class:`FakeClock` (T01.8)."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        if self._responses:
            return self._responses.pop(0)
        if timeout:
            self.clock.advance(timeout)
        return None


def test_freeze_frame_trigger_is_consumed_once_then_reads_go_dormant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 3.3 — the trigger fires exactly one Mode 02 sweep; it doesn't
    re-poll every tick afterward (on_demand rate is 0).

    On a fake clock (T01.8): the sweep starts after the unanswered RPM read's
    0.1 s timeout, and the claim is about the rest of the second, so both the
    sweep and the dormant window after it must fit. On a real clock a loaded
    machine spent that window for it."""
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _ClockedFreezeFrameBus(clock)
    stop = threading.Event()
    readings: list[Any] = []
    trigger = FreezeFrameTrigger()
    trigger.request()

    def on_reading(reading: Any) -> None:
        readings.append(reading)

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=20.0,
        pids=("RPM",),
        request_timeout_seconds=0.1,
        freeze_frame_trigger=trigger,
    )

    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=on_reading,
        stop_event=stop,
        deadline_monotonic=clock.monotonic() + 1.0,
    )

    ff_readings = [r for r in readings if r.pid.startswith("FREEZE_FRAME_")]
    channels = [r.pid for r in ff_readings]
    # Exactly one reading per freeze-frame PID — no repeats even though the
    # loop ran for a full second at 20 Hz.
    assert sorted(channels) == sorted(set(channels))
    assert "FREEZE_FRAME_RPM" in channels


def test_force_poll_trigger_take_is_atomic_and_normalizes_case() -> None:
    trigger = ForcePollTrigger()
    assert trigger.take() == frozenset()  # nothing pending yet

    trigger.request(["rpm", " Dtc_Stored "])
    assert trigger.take() == frozenset({"RPM", "DTC_STORED"})
    assert trigger.take() == frozenset()  # consumed — not sticky

    trigger.request(["a"])
    trigger.request(["b"])  # a second request before take() merges, doesn't overwrite
    assert trigger.take() == frozenset({"A", "B"})


class _ClockedDtcBus(_DtcCapableBus):
    """:class:`_DtcCapableBus` on a :class:`FakeClock`: a receive with nothing
    due costs its timeout in simulated time instead of sleeping."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        if self._responses:
            return self._responses.pop(0)
        if timeout:
            self.clock.advance(timeout)
        return None


def test_force_poll_trigger_forces_a_channel_due_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trigger_poll: DTC_STORED normally waits its turn; forcing it should
    produce a reading well before it would ever fire on its own.

    On a fake clock (T01.3). The forced read lands at 0.31 s, after three
    0.1 s support-probe timeouts; unforced, the first DTC_STORED read comes
    at ~4.9 s (measured 2026-09-23). The test used to give it 0.5 s of *wall*
    clock, which a loaded machine spent before the probes were done."""
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    bus = _ClockedDtcBus(clock)
    sink: StopWhen[Any] = StopWhen(lambda got: any(r.pid == "DTC_STORED" for r in got))
    trigger = ForcePollTrigger()
    trigger.request(["dtc_stored"])  # lowercase on purpose — matching is case-insensitive

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=20.0,
        pids=(),
        request_timeout_seconds=0.1,
        dtc_poll_interval_seconds=300.0,  # would never fire naturally in this window
        service_poll_interval_seconds=0,  # unrelated to this test; keep the loop fast
        force_poll_trigger=trigger,
    )

    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=sink,
        stop_event=sink.stop,
        deadline_monotonic=clock.monotonic() + 0.5,
    )

    assert sink.met, "the forced DTC_STORED read did not happen inside 0.5 s"


def test_force_poll_trigger_ignores_unknown_channel_names() -> None:
    """An unmatched channel name is a silent no-op, not an error."""
    bus = _DtcCapableBus()
    stop = threading.Event()
    trigger = ForcePollTrigger()
    trigger.request(["not_a_real_channel"])

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=20.0,
        pids=(),
        request_timeout_seconds=0.05,
        dtc_poll_interval_seconds=300.0,
        service_poll_interval_seconds=0,
        force_poll_trigger=trigger,
    )

    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=lambda _r: None,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 0.2,
    )
    # No crash, no spurious channel appearing — this test's assertion is
    # simply that the loop above completed cleanly.


def test_dtc_poll_skips_cleanly_when_bus_never_answers() -> None:
    """Stale/no_data: the DTC requests just back off like any other request
    — no crash, no readings, no spurious extra polling."""
    bus = _SilentBus()
    stop = threading.Event()

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=20.0,
        pids=("RPM",),
        dtc_poll_interval_seconds=0.01,
        request_timeout_seconds=0.02,
    )

    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=lambda _r: None,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 0.5,
    )

    assert stats.readings_emitted == 0
    assert stats.request_timeouts > 0
    # True bus silence (no frame at all), not a formal ECU refusal.
    assert stats.negative_responses == 0
    dtc_service_bytes = {bytes(msg.data)[:2] for msg in bus.sent}
    assert bytes([0x01, 0x03]) in dtc_service_bytes


def test_run_loop_emits_can_frames_pid_readings_and_did_readings() -> None:
    bus = _FakeBus()
    stop = threading.Event()
    frames: list[can.Message] = []
    readings: list[Any] = []

    def on_reading(reading: Any) -> None:
        readings.append(reading)
        if {r.pid for r in readings} >= {"RPM", "DID_1101"}:
            stop.set()

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=20.0,
        pids=("RPM",),
        dids=("1101",),
        request_timeout_seconds=0.05,
    )

    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=frames.append,
        on_reading=on_reading,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 1.0,
    )

    by_pid = {reading.pid: reading for reading in readings}
    assert by_pid["RPM"].value == 700.0
    assert by_pid["DID_1101"].value == "1234"
    assert len(frames) >= 2
    assert stats.requests_sent >= 2
    assert stats.readings_emitted >= 2


def test_negative_pid_response_does_not_block_following_pids() -> None:
    """ENGINE_LOAD is refused (``7F 01 12``); the RPM read *after* that refusal
    is what proves the refusal did not block the loop.

    T01.2: this used to stop on "RPM read and two requests sent", which the
    first RPM read (priority lane, one request sent) never met, so the test
    only ever ended at its 0.4 s deadline, inside the 1 s timeout of the
    Mode 03 poll that followed the refusal. A loop slower than 0.4 s to its
    first read failed it. It now stops on the RPM read that follows the
    refusal, and the DTC/Mode 06 polls (unanswered here, 1 s timeouts each)
    are off so nothing but the two PIDs is on the bus."""
    bus = _NegativeThenPositiveBus()
    engine_load_request = bytes([0x02, 0x01, 0x04])

    def _refusal_sent() -> bool:
        return any(bytes(msg.data)[:3] == engine_load_request for msg in bus.sent)

    sink: StopWhen[Any] = StopWhen(lambda got: got[-1].pid == "RPM" and _refusal_sent())
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=100.0,
        pids=("ENGINE_LOAD", "RPM"),
        request_timeout_seconds=1.0,
        dtc_poll_interval_seconds=0,
        service_poll_interval_seconds=0,
    )

    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=sink,
        stop_event=sink.stop,
        deadline_monotonic=hang_guard_deadline(),
    )

    assert sink.met, "no RPM reading followed ENGINE_LOAD's refusal"
    assert "ENGINE_LOAD" not in {reading.pid for reading in sink.items}
    # The refusal was read as a refusal, not waited out as a timeout.
    assert stats.negative_responses == 1
    assert stats.request_timeouts == 0
    assert stats.requests_sent >= 2


def test_scheduler_polls_fast_tier_more_often_than_slow_tier() -> None:
    bus = _TieredPidBus()
    stop = threading.Event()
    readings: list[Any] = []

    def on_reading(reading: Any) -> None:
        readings.append(reading)
        counts = {pid: sum(1 for r in readings if r.pid == pid) for pid in {"RPM", "FUEL_LEVEL"}}
        if counts["RPM"] >= 3 and counts["FUEL_LEVEL"] >= 1:
            stop.set()

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=8.0,
        pids=("RPM", "FUEL_LEVEL"),
        request_timeout_seconds=0.02,
    )

    # Stops on its condition; the deadline only turns a hang into a failure
    # (T01.8: the condition came at 0.25 s, after four unanswered DTC/Mode 06
    # polls, against a 1.0 s wall-clock deadline).
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=on_reading,
        stop_event=stop,
        deadline_monotonic=hang_guard_deadline(),
    )

    sent_pids = [bytes(msg.data)[2] for msg in bus.sent if bytes(msg.data)[:2] == b"\x02\x01"]
    assert sent_pids.count(0x0C) > sent_pids.count(0x2F)
    assert stats.readings_emitted == len(readings)


def test_scheduler_metrics_report_rates_and_backoff_state() -> None:
    bus = _NegativeThenPositiveBus()
    stop = threading.Event()
    snapshots: list[ObdSchedulerMetrics] = []

    def on_reading(reading: Any) -> None:
        if reading.pid == "RPM" and len(bus.sent) >= 2:
            stop.set()

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=50.0,
        pids=("ENGINE_LOAD", "RPM"),
        request_timeout_seconds=0.05,
    )

    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=on_reading,
        stop_event=stop,
        # Stops on its condition (T01.8: reached at 0.06 s, after a timeout,
        # against a 0.4 s wall-clock deadline).
        deadline_monotonic=hang_guard_deadline(),
        on_scheduler_metrics=snapshots.append,
        scheduler_metrics_interval_seconds=0.01,
    )

    assert stats.scheduler_snapshots >= 1
    assert stats.negative_responses >= 1
    latest = snapshots[-1].to_dict()
    assert latest["bus_budget_requests_per_second"] > spec.poll_rate_hz
    assert latest["response_rate"] is not None
    by_channel = {row["channel"]: row for row in latest["requests"]}
    # ENGINE_LOAD always gets a formal 7F 01 12 refusal from
    # `_NegativeThenPositiveBus`, never a true timeout — that must land in
    # `negative_responses`, not `timeouts` (the bug this counter fixes).
    assert by_channel["ENGINE_LOAD"]["backed_off"] is True
    assert by_channel["ENGINE_LOAD"]["negative_responses"] >= 1
    assert by_channel["ENGINE_LOAD"]["timeouts"] == 0
    assert (
        by_channel["ENGINE_LOAD"]["last_negative_response_code"] == "0x12 (subFunctionNotSupported)"
    )
    assert by_channel["RPM"]["responses"] >= 1
    assert latest["negative_responses"] >= 1


# --- Custom PIDs (Task 2.6) --------------------------------------------------


def test_build_poll_requests_includes_custom_pids() -> None:
    custom = CustomPid(
        name="OIL_PRESS",
        display_name="Oil Pressure",
        mode=0x22,
        pid=0x1140,
        formula="(A*256+B)/100",
        unit="psi",
        header=0x7E0,
        poll_tier="slow",
    )
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=(),
        custom_pids=(custom,),
    )
    requests = [r for r in build_poll_requests(_StaticDiscovery(), spec) if r.kind == "custom"]
    assert len(requests) == 1
    request = requests[0]
    assert request.channel == "custom:OIL_PRESS"
    assert request.service == 0x22
    assert request.identifier == 0x1140
    assert request.request_id == 0x7E0
    assert request.poll_tier == "slow"
    assert request.custom_pid is custom


def test_build_poll_requests_custom_pid_defaults_request_id_by_mode() -> None:
    # No explicit `header`: Mode 22 defaults to the DID request id, Mode 01
    # defaults to the Mode-01 broadcast id — same convention as `did`/`pid`.
    mode22 = CustomPid(name="A", display_name="A", mode=0x22, pid=0x1, formula="A")
    mode01 = CustomPid(name="B", display_name="B", mode=0x01, pid=0x1, formula="A")
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=(),
        did_request_id=0x7E1,
        custom_pids=(mode22, mode01),
    )
    by_name = {
        r.custom_pid.name: r
        for r in build_poll_requests(_StaticDiscovery(), spec)
        if r.kind == "custom"
    }
    assert by_name["A"].request_id == 0x7E1
    assert by_name["B"].request_id == 0x7DF


class _CustomPidBus:
    """Answers a Mode 0x22 custom-PID request for DID 0x1140."""

    def __init__(self) -> None:
        self.sent: list[can.Message] = []
        self._responses: list[can.Message] = []

    def send(self, msg: can.Message) -> None:
        self.sent.append(msg)
        data = bytes(msg.data)
        if msg.arbitration_id == 0x7E0 and data[:4] == bytes([0x03, 0x22, 0x11, 0x40]):
            self._responses.append(
                can.Message(
                    arbitration_id=0x7E8,
                    data=bytes([0x05, 0x62, 0x11, 0x40, 0x03, 0xE8, 0x00, 0x00]),
                    is_extended_id=False,
                )
            )

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        if self._responses:
            return self._responses.pop(0)
        if timeout:
            time.sleep(min(timeout, 0.005))
        return None


def test_custom_pid_response_decodes_through_evaluator_into_custom_channel() -> None:
    bus = _CustomPidBus()
    stop = threading.Event()
    readings: list[Any] = []

    def on_reading(reading: Any) -> None:
        readings.append(reading)
        stop.set()

    custom = CustomPid(
        name="OIL_PRESS",
        display_name="Oil Pressure",
        mode=0x22,
        pid=0x1140,
        formula="(A*256+B)/100",
        unit="psi",
        header=0x7E0,
    )
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=20.0,
        pids=("RPM",),  # explicit, non-empty — avoids the "auto" full-catalog probe
        custom_pids=(custom,),
        request_timeout_seconds=0.2,
    )

    # Stops on the first reading (T01.8: reached at 0.20 s, after the
    # unanswered RPM read's 0.2 s timeout, against a 1.0 s wall-clock deadline).
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=on_reading,
        stop_event=stop,
        deadline_monotonic=hang_guard_deadline(),
    )

    assert len(readings) == 1
    reading = readings[0]
    assert reading.pid == "custom:OIL_PRESS"
    assert reading.value == 10.0
    assert reading.unit == "psi"


# --- Mode 22 session windows (Task 6.15 / risk R8) --------------------------


class _SessionGatedBus:
    """Models the 2008 Nissan ECM's Mode-22 gating.

    * ``22 xxxx`` outside the 0xC0 session → ``7F 22 80`` (refused);
    * ``10 C0`` → ``50 C0`` and the session holds for ``session_ttl`` seconds
      (refreshed by re-sending ``10 C0``);
    * Mode 01 goes silent while the session is held (mutual exclusion).
    """

    def __init__(self, session_ttl: float = 0.08) -> None:
        # ttl defaults below the tests' lapse_seconds (0.1) so the session
        # expires during the lapse, like the real ECM's S3 timeout does.
        self.sent: list[can.Message] = []
        self._responses: list[can.Message] = []
        self._session_until = 0.0
        self._ttl = session_ttl

    def _in_session(self) -> bool:
        return time.monotonic() < self._session_until

    def send(self, msg: can.Message) -> None:
        self.sent.append(msg)
        data = bytes(msg.data)
        if msg.arbitration_id == 0x7E0 and data[:3] == bytes([0x02, 0x10, 0xC0]):
            self._session_until = time.monotonic() + self._ttl
            self._responses.append(
                can.Message(
                    arbitration_id=0x7E8,
                    data=bytes([0x02, 0x50, 0xC0, 0x00, 0x00, 0x00, 0x00, 0x00]),
                    is_extended_id=False,
                )
            )
        elif msg.arbitration_id == 0x7DF and data[:3] == bytes([0x02, 0x01, 0x0C]):
            if not self._in_session():
                self._responses.append(
                    can.Message(
                        arbitration_id=0x7E8,
                        data=bytes([0x04, 0x41, 0x0C, 0x0A, 0xF0, 0x00, 0x00, 0x00]),
                        is_extended_id=False,
                    )
                )
        elif msg.arbitration_id == 0x7E0 and data[:4] == bytes([0x03, 0x22, 0x11, 0x01]):
            if self._in_session():
                self._responses.append(
                    can.Message(
                        arbitration_id=0x7E8,
                        data=bytes([0x05, 0x62, 0x11, 0x01, 0x12, 0x34, 0x00, 0x00]),
                        is_extended_id=False,
                    )
                )
            else:
                self._responses.append(
                    can.Message(
                        arbitration_id=0x7E8,
                        data=bytes([0x03, 0x7F, 0x22, 0x80, 0x00, 0x00, 0x00, 0x00]),
                        is_extended_id=False,
                    )
                )

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        if self._responses:
            return self._responses.pop(0)
        if timeout:
            time.sleep(min(timeout, 0.002))
        return None


def _window_spec(**overrides: float) -> CanObdSpec:
    session = DidSessionSpec(
        session=0xC0,
        keepalive_seconds=float(overrides.pop("keepalive_seconds", 0.15)),
        window_interval_seconds=float(overrides.pop("window_interval_seconds", 0.25)),
        window_max_seconds=float(overrides.pop("window_max_seconds", 0.5)),
        lapse_seconds=float(overrides.pop("lapse_seconds", 0.1)),
    )
    assert not overrides, f"unused overrides: {overrides}"
    return CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=20.0,
        pids=("RPM",),
        dids=("1101",),
        request_timeout_seconds=0.05,
        did_session=session,
    )


def test_did_session_windows_read_gated_dids_and_resume_mode_01() -> None:
    """On a session-gated ECM, DIDs are read inside 10 C0 windows and Mode 01
    polling resumes after the lapse — neither starves the other."""
    bus = _SessionGatedBus()
    stop = threading.Event()
    readings: list[Any] = []

    def on_reading(reading: Any) -> None:
        readings.append(reading)
        pids = [r.pid for r in readings]
        # Done once we've seen a DID reading AND a Mode 01 reading after it.
        if "DID_1101" in pids and "RPM" in pids[pids.index("DID_1101") :]:
            stop.set()

    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _window_spec(),
        on_frame=lambda _msg: None,
        on_reading=on_reading,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 5.0,
    )

    pids = [r.pid for r in readings]
    assert "DID_1101" in pids, "gated DID was never read through a session window"
    did_at = pids.index("DID_1101")
    assert "RPM" in pids[:did_at], "Mode 01 should poll before the first window"
    assert "RPM" in pids[did_at:], "Mode 01 should resume after the window lapses"
    by_pid = {r.pid: r for r in readings}
    assert by_pid["DID_1101"].value == "1234"
    assert stats.session_controls_sent >= 1
    # The 22 request must only ever follow a 10 C0 on the wire.
    wire = [bytes(m.data)[:2] for m in bus.sent]
    first_did = wire.index(bytes([0x03, 0x22]))
    assert bytes([0x02, 0x10]) in wire[:first_did]


def test_did_requests_stay_quiet_outside_windows() -> None:
    """Between windows no 22/10 frames are sent — Mode 01 runs undisturbed."""
    bus = _SessionGatedBus()
    stop = threading.Event()
    readings: list[Any] = []

    spec = _window_spec(window_interval_seconds=10.0)  # first window far away

    def on_reading(reading: Any) -> None:
        readings.append(reading)
        if sum(1 for r in readings if r.pid == "RPM") >= 3:
            stop.set()

    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=on_reading,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 2.0,
    )

    services = {bytes(m.data)[1] for m in bus.sent}
    assert 0x01 in services
    assert 0x22 not in services, "DID polled outside a session window"
    assert 0x10 not in services, "session opened before the configured interval"


def test_failed_session_entry_skips_window_and_keeps_mode_01_alive() -> None:
    """An ECM that refuses 10 C0 must not wedge the loop or starve Mode 01."""

    class _RefusesSessionBus(_SessionGatedBus):
        def send(self, msg: can.Message) -> None:
            data = bytes(msg.data)
            if msg.arbitration_id == 0x7E0 and data[:2] == bytes([0x02, 0x10]):
                self.sent.append(msg)
                self._responses.append(
                    can.Message(
                        arbitration_id=0x7E8,
                        data=bytes([0x03, 0x7F, 0x10, 0x11, 0x00, 0x00, 0x00, 0x00]),
                        is_extended_id=False,
                    )
                )
                return
            super().send(msg)

    bus = _RefusesSessionBus()
    stop = threading.Event()
    readings: list[Any] = []
    snapshots: list[ObdSchedulerMetrics] = []

    def on_reading(reading: Any) -> None:
        readings.append(reading)

    def until_failure_seen(metrics: ObdSchedulerMetrics) -> None:
        snapshots.append(metrics)
        profile = metrics.did_profile
        if profile is not None and profile["session_failures"] >= 1:
            # Wait for Mode 01 proof-of-life after the failed window attempt.
            if any(r.pid == "RPM" for r in readings[-3:]):
                stop.set()

    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _window_spec(),
        on_frame=lambda _msg: None,
        on_reading=on_reading,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 5.0,
        on_scheduler_metrics=until_failure_seen,
        scheduler_metrics_interval_seconds=0.05,
    )

    profile = snapshots[-1].to_dict()["did_profile"]
    assert profile["session_failures"] >= 1
    assert profile["phase"] == "mode01"
    services = {bytes(m.data)[1] for m in bus.sent}
    assert 0x22 not in services, "DID must not be polled when the session never opened"
    assert any(r.pid == "RPM" for r in readings)


def test_resolve_did_session_spec_reads_dids_yaml_access_block() -> None:
    """`auto` pulls session 0xC0 + 2 s keep-alive from data/dids.yaml."""
    from src.config import CollectionConfig

    collection = CollectionConfig(
        obd_dids=("1101",),
        obd_poll_rate_hz=1.0,
        storage_path=Path("/tmp/canary-test"),
        session_retention_days=1,
        storage_warning_mb=100,
    )
    spec = resolve_did_session_spec(collection)
    assert spec is not None
    assert spec.session == 0xC0
    assert spec.keepalive_seconds == 2.0
    assert spec.window_interval_seconds == 60.0

    no_dids = collection.model_copy(update={"obd_dids": ()})
    assert resolve_did_session_spec(no_dids) is None

    forced_off = collection.model_copy(update={"obd_did_session": None})
    assert resolve_did_session_spec(forced_off) is None


# --- record_can_obd reconnect resilience -----------------------------------
#
# The Canable-OBD recorder path used by `scripts/record.py --obd-source
# canable` for the drive backlog must survive an in-vehicle USB
# re-enumeration the same way the CAN-only record() path does. Before the
# fix it had no reconnect at all, so a single OSError ended the drive
# silently mid-capture. These mirror the test_can_collector reconnect cases.

_SPEC = CanObdSpec(
    can=CanBusConfig(interface="virtual", channel="recon", bitrate=500000),
    poll_rate_hz=50.0,
    pids=("RPM",),  # explicit → no supported-PID walk, deterministic
    request_timeout_seconds=0.05,
)


class _ReconnectableObdBus:
    """Serves one CAN frame then raises OSError on recv, simulating a USB
    drop. Class counters let tests confirm the factory built fresh buses."""

    instances_built = 0
    instances_shutdown = 0

    def __init__(self, good_frames: int = 1) -> None:
        type(self).instances_built += 1
        self.calls = 0
        self.budget = good_frames

    def send(self, msg: can.Message) -> None:  # read-only OBD request; ignore
        pass

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        self.calls += 1
        if self.calls > self.budget:
            raise OSError(5, "Input/output error")
        return can.Message(
            arbitration_id=0x100 + self.calls,
            data=bytes([self.calls & 0xFF] * 4),
            is_extended_id=False,
            timestamp=float(self.calls),
        )

    def shutdown(self) -> None:
        type(self).instances_shutdown += 1


def test_record_can_obd_auto_reconnects_on_bus_failure(tmp_path: Path) -> None:
    """OSError from a USB disconnect rebuilds the bus via factory instead of
    ending the recording; frames.log stays one continuous file."""
    _ReconnectableObdBus.instances_built = 0
    _ReconnectableObdBus.instances_shutdown = 0

    paths = make_session_dir(tmp_path, datetime.now(UTC))
    writer = JsonlReadingWriter(paths.pids)

    def factory(_spec: CanBusConfig) -> _ReconnectableObdBus:
        return _ReconnectableObdBus(good_frames=1)

    result = record_can_obd(
        factory(_SPEC.can),  # type: ignore[arg-type]
        paths,
        spec=_SPEC,
        vehicle=None,
        duration=0.4,
        obd_writer=writer,
        factory=factory,  # type: ignore[arg-type]
        reconnect_policy=ReconnectPolicy(
            initial_delay_seconds=0.01,
            max_delay_seconds=0.01,
        ),
    )
    writer.close()

    assert result.reconnects >= 1, "factory should have been called for reconnect"
    assert result.stopped_by == "deadline"
    # Frames written across rebuilds land in one continuous file (data
    # integrity preserved even though the per-segment tally undercounts).
    assert paths.frames.exists()
    assert paths.frames.stat().st_size > 0
    # Failed buses shut down — no USB handle leaks.
    assert _ReconnectableObdBus.instances_shutdown >= result.reconnects

    manifest = json.loads(paths.manifest.read_text())
    assert manifest["reconnects"] == result.reconnects
    assert manifest["stopped_by"] == "deadline"


def test_record_can_obd_bails_after_max_consecutive_reconnect_failures(
    tmp_path: Path,
) -> None:
    """A persistently broken adapter must stop with reconnect_exhausted
    rather than spin forever (the wedged-USB / brown-out failure mode)."""
    paths = make_session_dir(tmp_path, datetime.now(UTC))
    writer = JsonlReadingWriter(paths.pids)

    factory_calls = 0

    def failing_factory(_spec: CanBusConfig) -> _ReconnectableObdBus:
        nonlocal factory_calls
        factory_calls += 1
        raise RuntimeError(f"adapter wedged (attempt {factory_calls})")

    result = record_can_obd(
        _ReconnectableObdBus(good_frames=1),  # type: ignore[arg-type]
        paths,
        spec=_SPEC,
        vehicle=None,
        duration=10.0,  # generous; we expect to bail well before this
        obd_writer=writer,
        factory=failing_factory,  # type: ignore[arg-type]
        reconnect_policy=ReconnectPolicy(
            initial_delay_seconds=0.005,
            max_delay_seconds=0.005,
        ),
        max_reconnects=3,
    )
    writer.close()

    assert result.stopped_by == "reconnect_exhausted"
    assert factory_calls == 3  # exactly max_reconnects attempts
    manifest = json.loads(paths.manifest.read_text())
    assert manifest["stopped_by"] == "reconnect_exhausted"
    assert "adapter wedged" in manifest["error"]


class _GoodObdBus:
    """Hands out broadcast frames forever; never fails."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, msg: can.Message) -> None:
        pass

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        self.calls += 1
        return can.Message(arbitration_id=0x100, data=b"\x01\x02", is_extended_id=False)

    def shutdown(self) -> None:
        pass


def test_record_can_obd_stops_on_a_full_disk(tmp_path: Path) -> None:
    """R02.6: the frames writer runs inside ``on_frame``, so its OSError left
    the loop and was taken for a bus fault — the bus was rebuilt (which
    succeeds and resets the counters) and retried until the deadline, never
    reporting ``disk_error``. A full disk ends the run on the first failure."""
    import errno
    import unittest.mock as mock

    paths = make_session_dir(tmp_path, datetime.now(UTC))
    writer = JsonlReadingWriter(paths.pids)
    factory_calls = 0

    def factory(_spec: CanBusConfig) -> _GoodObdBus:
        nonlocal factory_calls
        factory_calls += 1
        return _GoodObdBus()

    class FullDisk:
        def __init__(self, _path: str) -> None:
            self.writes = 0

        def __enter__(self) -> FullDisk:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def on_message_received(self, _msg: can.Message) -> None:
            self.writes += 1
            if self.writes > 3:
                raise OSError(errno.ENOSPC, "No space left on device")

    with mock.patch.object(can.io, "CanutilsLogWriter", FullDisk):
        result = record_can_obd(
            _GoodObdBus(),  # type: ignore[arg-type]
            paths,
            spec=_SPEC,
            vehicle=None,
            duration=2.0,
            obd_writer=writer,
            factory=factory,  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(initial_delay_seconds=0.01, max_delay_seconds=0.01),
        )
    writer.close()

    assert result.stopped_by == "disk_error"
    assert result.dropped_writes == 1
    assert result.reconnects == 0 and factory_calls == 0, "a full disk is not a bus fault"
    manifest = json.loads(paths.manifest.read_text())
    assert manifest["stopped_by"] == "disk_error"
    assert manifest["dropped_writes"] == 1
    assert "No space left on device" in manifest["error"]


def test_record_can_obd_without_factory_propagates(tmp_path: Path) -> None:
    """Backward-compat: with no factory/policy a bus failure propagates as
    before (the daemon path's long-standing behavior), and the manifest is
    still written with stopped_by='error'."""
    paths = make_session_dir(tmp_path, datetime.now(UTC))
    writer = JsonlReadingWriter(paths.pids)

    with pytest.raises(OSError):
        record_can_obd(
            _ReconnectableObdBus(good_frames=1),  # type: ignore[arg-type]
            paths,
            spec=_SPEC,
            vehicle=None,
            duration=0.4,
            obd_writer=writer,
        )
    writer.close()

    manifest = json.loads(paths.manifest.read_text())
    assert manifest["stopped_by"] == "error"
    assert manifest["reconnects"] == 0


def test_record_can_obd_reraises_config_value_error(tmp_path: Path) -> None:
    """A config error (unknown PID) must propagate even with reconnect
    enabled — it is not a transient bus fault to retry forever."""
    paths = make_session_dir(tmp_path, datetime.now(UTC))
    writer = JsonlReadingWriter(paths.pids)

    bad_spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="recon", bitrate=500000),
        pids=("NOPE",),
    )

    with pytest.raises(ValueError, match="unknown"):
        record_can_obd(
            _ReconnectableObdBus(good_frames=1),  # type: ignore[arg-type]
            paths,
            spec=bad_spec,
            vehicle=None,
            duration=0.4,
            obd_writer=writer,
            factory=lambda _s: _ReconnectableObdBus(),  # type: ignore[arg-type,return-value]
            reconnect_policy=ReconnectPolicy(initial_delay_seconds=0.01, max_delay_seconds=0.01),
        )
    writer.close()


def _controller(**overrides: object) -> tuple[Any, list[Any]]:
    """A window controller holding one DID, plus its request states."""
    from src.collection.obd_scheduler import DidWindowController, _PollRequestState

    spec = DidSessionSpec(
        session=0xC0,
        keepalive_seconds=2.0,
        window_interval_seconds=60.0,
        window_max_seconds=10.0,
        lapse_seconds=5.0,
        **overrides,  # type: ignore[arg-type]
    )
    states = [_PollRequestState(), _PollRequestState()]
    return DidWindowController(spec, [1], now=0.0), states


def test_fast_exit_hands_mode_01_back_without_waiting_out_the_session() -> None:
    """The lapse exists so the session expires; an accepted `10 81` ends it now."""
    controller, states = _controller(exit_session=0x81, exit_grace_seconds=0.05)
    controller.on_session_response(result=SessionControlResult(ok=True), states=states, now=100.0)
    assert controller.phase == "window"

    # Window closes; the exit is now owed.
    controller.advance(states, now=100.0 + 10.0)
    assert controller.phase == "lapse"
    assert controller.session_exit_due() is True

    controller.on_exit_response(result=SessionControlResult(ok=True), now=110.0)
    assert controller.fast_exits == 1
    assert controller.session_exit_due() is False
    # Mode 01 resumes after the grace, not after the 5 s lapse.
    controller.advance(states, now=110.06)
    assert controller.phase == "mode01"


def test_a_transient_exit_failure_is_retried_on_the_next_window() -> None:
    """One silence must cost one window, not the rest of the drive.

    The real ECM answered 139 of 140 fast exits on 2026-08-07. The 140th
    failed, and because any failure latched the fallback off, Mode 01 went to
    zero for the remainder of the capture.
    """
    controller, states = _controller(exit_session=0x81)
    controller.on_session_response(result=SessionControlResult(ok=True), states=states, now=100.0)
    controller.advance(states, now=110.0)
    controller.on_exit_response(result=SessionControlResult(ok=False), now=110.0)

    assert controller.fast_exit_failures == 1
    # This window still waits the full lapse out — nothing else to hand back to.
    controller.advance(states, now=110.06)
    assert controller.phase == "lapse"
    controller.advance(states, now=115.01)
    assert controller.phase == "mode01"

    # ...but the next window tries again.
    assert controller.fast_exit_available(now=115.01) is True
    controller.on_session_response(result=SessionControlResult(ok=True), states=states, now=200.0)
    controller.advance(states, now=210.0)
    assert controller.session_exit_due() is True
    assert controller.snapshot(now=210.0)["fast_exit_supported"] is True


@pytest.mark.parametrize(
    ("nrc", "label"),
    [
        (None, "no response"),
        (0x21, "busyRepeatRequest"),
        (0x22, "conditionsNotCorrect"),
        (0x78, "responsePending"),
    ],
)
def test_a_busy_or_silent_ecu_never_disables_the_fast_exit(nrc: int | None, label: str) -> None:
    """Only "never" disables a capability. Everything else is a retry."""
    controller, _states = _controller(exit_session=0x81)
    for tick in range(10):
        controller.on_exit_response(
            result=SessionControlResult(ok=False, negative_response_code=nrc),
            now=100.0 + tick,
        )
    assert controller.fast_exit_available(now=200.0) is True, label
    assert controller.snapshot(now=200.0)["fast_exit_supported"] is True


def test_two_permanent_refusals_disable_the_fast_exit_and_then_re_probe() -> None:
    """A genuine `subFunctionNotSupported` disables it — provisionally."""
    controller, _states = _controller(exit_session=0x81)
    refusal = SessionControlResult(ok=False, negative_response_code=0x12)

    controller.on_exit_response(result=refusal, now=100.0)
    assert controller.fast_exit_available(now=100.0) is True  # one is not proof

    controller.on_exit_response(result=refusal, now=101.0)
    assert controller.fast_exit_available(now=101.0) is False
    snapshot = controller.snapshot(now=101.0)
    assert snapshot["fast_exit_supported"] is False
    assert snapshot["fast_exit_retry_in_seconds"] == FAST_EXIT_RETRY_MIN_SECONDS
    assert snapshot["last_exit_nrc_name"] == "subFunctionNotSupported"

    # Self-healing: the disable expires on its own, mid-drive, no restart.
    assert controller.fast_exit_available(now=101.0 + FAST_EXIT_RETRY_MIN_SECONDS + 0.1) is True


def test_repeated_refusals_back_off_but_a_success_clears_everything() -> None:
    controller, _states = _controller(exit_session=0x81)
    refusal = SessionControlResult(ok=False, negative_response_code=0x11)

    controller.on_exit_response(result=refusal, now=100.0)
    controller.on_exit_response(result=refusal, now=101.0)
    assert controller.snapshot(now=101.0)["fast_exit_retry_in_seconds"] == pytest.approx(
        FAST_EXIT_RETRY_MIN_SECONDS, abs=0.1
    )

    # The re-probe fails too, so the next wait doubles rather than giving up.
    retry_at = 101.0 + FAST_EXIT_RETRY_MIN_SECONDS
    controller.on_exit_response(result=refusal, now=retry_at)
    assert controller.snapshot(now=retry_at)["fast_exit_retry_in_seconds"] == pytest.approx(
        FAST_EXIT_RETRY_MIN_SECONDS * 2, abs=0.1
    )

    controller.on_exit_response(result=SessionControlResult(ok=True), now=retry_at + 100.0)
    assert controller.fast_exit_available(now=retry_at + 100.0) is True
    recovered = controller.snapshot(now=retry_at + 100.0)
    assert recovered["fast_exit_retry_in_seconds"] == 0.0
    assert recovered["last_exit_nrc_name"] is None


class _BusyThenAnsweringSessionBus:
    """Answers session control with `0x78 responsePending` before the real reply.

    ISO 14229's "received you, still working" — the shape of an ECU under
    load, which is exactly when a 90 req/s hybrid capture asks it to switch
    sessions.
    """

    def __init__(self, pending_frames: int) -> None:
        self.sent: list[can.Message] = []
        self._responses: list[can.Message] = []
        self._pending_frames = pending_frames

    def send(self, msg: can.Message) -> None:
        self.sent.append(msg)
        data = bytes(msg.data)
        if data[:2] != bytes([0x02, 0x10]):
            return
        for _ in range(self._pending_frames):
            self._responses.append(
                can.Message(
                    arbitration_id=0x7E8,
                    data=bytes([0x03, 0x7F, 0x10, 0x78, 0x00, 0x00, 0x00, 0x00]),
                    is_extended_id=False,
                )
            )
        self._responses.append(
            can.Message(
                arbitration_id=0x7E8,
                data=bytes([0x02, 0x50, data[2], 0x00, 0x00, 0x00, 0x00, 0x00]),
                is_extended_id=False,
            )
        )

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        if self._responses:
            return self._responses.pop(0)
        if timeout:
            time.sleep(min(timeout, 0.005))
        return None


def _send_exit(bus: object, stats: CanObdLoopStats) -> SessionControlResult:
    from src.collection.can_obd_collector import _send_session_control

    return _send_session_control(
        bus,  # type: ignore[arg-type]
        DidSessionSpec(session=0xC0, exit_session=0x81),
        sub_function=0x81,
        request_id=0x7E0,
        timeout_seconds=0.2,
        on_frame=lambda _msg: None,
        stop_event=threading.Event(),
        stats=stats,
        deadline_monotonic=None,
    )


def test_a_busy_ecu_gets_its_answer_through_instead_of_being_called_unsupported() -> None:
    """`0x78` must extend the wait, not score a failure.

    This is the trigger end of the 2026-08-07 blackout: the answer was on its
    way, and reading the "please wait" as a refusal threw it away.
    """
    stats = CanObdLoopStats()
    result = _send_exit(_BusyThenAnsweringSessionBus(pending_frames=2), stats)

    assert result.ok is True
    assert result.negative_response_code is None
    assert stats.negative_responses == 0
    assert stats.request_timeouts == 0


def test_an_endlessly_busy_ecu_still_gives_up_and_says_why() -> None:
    """The wait is bounded — but the reason is reported, not silently dropped."""
    stats = CanObdLoopStats()
    result = _send_exit(
        _BusyThenAnsweringSessionBus(pending_frames=MAX_RESPONSE_PENDING_EXTENSIONS + 1),
        stats,
    )

    assert result.ok is False
    assert result.negative_response_code == 0x78
    assert result.permanently_refused is False  # so it will be retried
    # Session control used to bypass the counters entirely.
    assert stats.negative_responses == 1


def test_a_failed_session_control_is_visible_in_the_scheduler_counters() -> None:
    """A silent ECU on session control used to leave no trace anywhere."""
    stats = CanObdLoopStats()
    result = _send_exit(_SilentBus(), stats)

    assert result.ok is False
    assert result.negative_response_code is None
    assert stats.request_timeouts == 1


def test_a_lapse_always_leaves_mode_01_a_share_of_the_bus() -> None:
    """Regression: `window_interval` below `lapse_seconds` starved Mode 01 to zero.

    The next window was scheduled `interval` after the window *closed*, so once
    a 5 s lapse expired it was already ~4.75 s overdue and reopened before a
    single Mode 01 request could be issued — permanently, not transiently.
    """
    from src.collection.obd_scheduler import MODE01_MIN_SHARE_SECONDS, DidWindowController

    spec = DidSessionSpec(
        session=0xC0,
        keepalive_seconds=2.0,
        window_interval_seconds=0.25,  # the hybrid profile's cadence
        window_max_seconds=10.0,
        lapse_seconds=5.0,  # ...twenty times longer than the interval
    )
    states = [_PollRequestState(), _PollRequestState()]
    controller = DidWindowController(spec, [1], now=0.0)

    controller.on_session_response(result=SessionControlResult(ok=True), states=states, now=100.0)
    controller.on_request_attempted(1)
    controller.advance(states, now=100.01)
    assert controller.phase == "lapse"

    controller.advance(states, now=105.02)
    assert controller.phase == "mode01"
    assert controller.session_control_due(states, now=105.02) is False
    assert controller.session_control_due(states, now=105.03 + MODE01_MIN_SHARE_SECONDS) is True


def test_no_exit_configured_keeps_the_historic_lapse() -> None:
    controller, states = _controller()
    controller.on_session_response(result=SessionControlResult(ok=True), states=states, now=100.0)
    controller.advance(states, now=110.0)
    assert controller.session_exit_due() is False
    assert controller.phase == "lapse"


def test_capture_profile_sets_the_did_window_cadence() -> None:
    """DID cadence is a sampling choice, so a profile can retune it live."""
    from src.collection import tuning_profile
    from src.collection.capture_profiles import CaptureProfile

    controller, states = _controller(exit_session=0x81, exit_grace_seconds=0.05)
    assert controller.snapshot(now=0.0)["window_interval_seconds"] == 60.0
    try:
        tuning_profile.activate(
            CaptureProfile(
                name="fast-dids",
                mode01={"RPM": 1.0},
                mode22=("ECM_RPM",),
                did_interval_seconds=0.5,
                corpus_eligible=False,
                storage_prefix="_capture-fast-dids",
            ),
            source="test",
        )
        assert controller.snapshot(now=0.0)["window_interval_seconds"] == 0.5

        # A 0.5 s cadence is only reachable because the fast exit ends the
        # blackout; the 5 s lapse alone would swallow it whole.
        controller.on_session_response(
            result=SessionControlResult(ok=True), states=states, now=100.0
        )
        controller.on_request_attempted(1)  # the window's only DID was read
        controller.advance(states, now=100.2)
        controller.on_exit_response(result=SessionControlResult(ok=True), now=100.2)
        controller.advance(states, now=100.26)
        assert controller.phase == "mode01"
        assert controller.session_control_due(states, now=100.7) is True
    finally:
        tuning_profile.set_active(False, source="test")


def test_single_pid_readings_still_stamp_themselves() -> None:
    """Only the batch path shares a stamp; the single-PID path is unchanged."""
    from src.collection.can_obd_collector import _response_to_reading
    from src.collection.isotp import ObdResponse

    request = _PollRequest(
        kind="pid", service=0x01, identifier=0x0C, request_id=0x7DF, channel="RPM"
    )
    before = time.time()
    readings = _response_to_reading(
        request,
        ObdResponse(service=0x41, pid=0x0C, payload=bytes([0x0B, 0xEA]), responder_id=0x7E8),
    )
    assert readings and before <= readings[0].timestamp <= time.time()


def test_query_with_tap_records_the_frame_arrival_and_its_clock() -> None:
    """C02.7: a sane bus-driver stamp is the acquisition clock; an adapter
    reporting 0.0 falls back to userspace time and says so."""
    from src.collection.can_obd_collector import KERNEL_STAMP_MAX_SKEW_SECONDS

    class _StampedBus(_FakeBus):
        def __init__(self, stamp: float) -> None:
            super().__init__()
            self._stamp = stamp

        def recv(self, timeout: float = 0.0) -> can.Message | None:
            msg = super().recv(timeout)
            if msg is not None:
                msg.timestamp = self._stamp
            return msg

    request = _PollRequest(
        kind="pid", service=0x01, identifier=0x0C, request_id=0x7DF, channel="RPM"
    )
    kernel_stamp = time.time() - 0.010
    for stamp, expected in ((kernel_stamp, "kernel"), (0.0, "userspace")):
        result = _query_with_tap(
            _StampedBus(stamp),  # type: ignore[arg-type]
            request,
            timeout_seconds=0.2,
            on_frame=lambda _m: None,
            stop_event=threading.Event(),
            stats=CanObdLoopStats(),
            deadline_monotonic=None,
        )
        assert result.response is not None and result.arrival is not None
        assert result.arrival.clock == expected
        assert result.response_interval_s is None, "single frame: no reassembly span"
        if expected == "kernel":
            assert result.arrival.at == kernel_stamp
        else:
            assert abs(result.arrival.at - time.time()) < KERNEL_STAMP_MAX_SKEW_SECONDS


def test_select_next_due_request_honours_round_membership_and_the_tuple() -> None:
    """Inside a round only members are candidates, a member may go before its
    own due instant, and the required tuple outranks everything but the
    priority lane."""
    requests = [
        _PollRequest(kind="pid", service=0x01, identifier=0x04, request_id=0x7DF, channel="LOAD"),
        _PollRequest(kind="pid", service=0x01, identifier=0x10, request_id=0x7DF, channel="MAF"),
        _PollRequest(
            kind="pid",
            service=0x01,
            identifier=0x0C,
            request_id=0x7DF,
            channel="RPM",
            priority=True,
        ),
    ]
    states = [
        _PollRequestState(next_due_at=0.0),
        _PollRequestState(next_due_at=2.0),  # not due yet, but in the window
        _PollRequestState(next_due_at=3.0),
    ]
    assert _select_next_due_request(requests, states, 1.0) is not None
    assert _select_next_due_request(requests, states, 1.0)[0] == 0  # type: ignore[index]
    # Round members {0, 1}: the tuple member MAF goes first despite its later due.
    assert _select_next_due_request(requests, states, 1.0, among={0, 1}, preferred={1})[0] == 1  # type: ignore[index]
    # The priority lane still wins over the tuple when it is a member.
    assert _select_next_due_request(requests, states, 1.0, among={0, 1, 2}, preferred={1})[0] == 2  # type: ignore[index]
    assert _select_next_due_request(requests, states, 1.0, among=set()) is None


def test_a_capture_armed_mid_window_does_not_wait_out_the_old_interval() -> None:
    """A profile change mid-window must not defer DIDs by the old interval.

    2026-09-22: a `tuning` capture asking for a window every 0.5 s recorded
    its first Mode 22 row 27.6 s in, because the change landed while a
    `default` window was open and the controller kept counting `default`'s
    ~50 s interval. The lapse (the ECU's session expiring) is real and is
    still honoured; the stale interval is not.
    """
    from src.collection.obd_scheduler import SessionControlResult

    for phase in ("window", "lapse"):
        controller, states = _controller()
        now = 100.0
        controller.on_session_response(result=SessionControlResult(ok=True), states=states, now=now)
        assert controller.phase == "window"
        if phase == "lapse":
            now += 10.0
            controller.advance(states, now=now)
            assert controller.phase == "lapse"

        controller.on_generation_change(now, profile="tuning", members={1: "DID_1208"})

        elapsed = 0.0
        while elapsed < 120.0 and not controller.session_control_due(states, now + elapsed):
            elapsed += 0.1
            controller.advance(states, now=now + elapsed)
        # The 5 s lapse, not the 60 s window interval.
        assert elapsed <= 6.0, f"{phase}: first window {elapsed:.1f}s after the change"


def test_record_can_obd_stops_when_the_readings_file_hits_a_full_disk(tmp_path: Path) -> None:
    """R02.6, the readings half: ``pids.jsonl`` flushes every line, so on a
    full disk it is usually the first write to fail — from inside the poll
    loop, where a bare ``OSError`` was taken for a bus fault and rebuilt."""
    import errno

    paths = make_session_dir(tmp_path, datetime.now(UTC))
    factory_calls = 0

    def factory(_spec: CanBusConfig) -> _ReconnectableObdBus:
        nonlocal factory_calls
        factory_calls += 1
        return _ReconnectableObdBus(good_frames=10_000)

    class FullDiskReadings:
        def write(self, _reading: object) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

    class AnsweringBus:
        """Answers every RPM request so a reading reaches the writer."""

        def __init__(self) -> None:
            self.pending: list[can.Message] = []

        def send(self, msg: can.Message, timeout: float | None = None) -> None:
            if bytes(msg.data)[1] == 0x01:
                self.pending.append(
                    can.Message(
                        arbitration_id=0x7E8,
                        data=bytes([0x04, 0x41, 0x0C, 0x0B, 0xB8, 0, 0, 0]),
                        is_extended_id=False,
                    )
                )

        def recv(self, timeout: float = 0.0) -> can.Message | None:
            return self.pending.pop(0) if self.pending else None

        def shutdown(self) -> None:
            pass

    result = record_can_obd(
        AnsweringBus(),  # type: ignore[arg-type]
        paths,
        spec=_SPEC,
        vehicle=None,
        duration=2.0,
        obd_writer=FullDiskReadings(),  # type: ignore[arg-type]
        factory=factory,  # type: ignore[arg-type]
        reconnect_policy=ReconnectPolicy(initial_delay_seconds=0.01, max_delay_seconds=0.01),
    )

    assert result.stopped_by == "disk_error"
    assert result.dropped_writes == 1
    assert result.reconnects == 0 and factory_calls == 0, "a full disk is not a bus fault"
    manifest = json.loads(paths.manifest.read_text())
    assert manifest["stopped_by"] == "disk_error"
    assert "No space left on device" in manifest["error"]
