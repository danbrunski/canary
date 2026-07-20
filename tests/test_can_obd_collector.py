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

from src.collection.can_collector import make_session_dir
from src.collection.can_obd_collector import (
    AUTO_BASELINE_PID_NAMES,
    CanObdSpec,
    DidSessionSpec,
    ObdSchedulerMetrics,
    build_poll_requests,
    parse_can_id,
    parse_did_token,
    parse_pid_token,
    record_can_obd,
    resolve_did_session_spec,
    run_can_obd_loop,
)
from src.collection.obd_collector import JsonlReadingWriter
from src.collection.obd_registry import MODE01_REGISTRY
from src.collection.reconnect import ReconnectPolicy
from src.config import CanBusConfig


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


def test_parse_pid_token_accepts_auto_names_and_hex() -> None:
    assert parse_pid_token("auto") is None
    assert parse_pid_token("RPM") == 0x0C
    assert parse_pid_token("0x0c") == 0x0C
    assert parse_pid_token("0C") == 0x0C
    with pytest.raises(ValueError, match="unknown"):
        parse_pid_token("NOPE")


def test_parse_did_and_can_id() -> None:
    assert parse_did_token("1101") == 0x1101
    assert parse_can_id("7E0") == 0x7E0
    assert parse_can_id("0x7e1") == 0x7E1
    with pytest.raises(ValueError, match="4-character"):
        parse_did_token("110")


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
    bus = _NegativeThenPositiveBus()
    stop = threading.Event()
    readings: list[Any] = []

    def on_reading(reading: Any) -> None:
        readings.append(reading)
        if reading.pid == "RPM":
            stop.set()

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=100.0,
        pids=("ENGINE_LOAD", "RPM"),
        request_timeout_seconds=1.0,
    )

    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=on_reading,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 0.4,
    )

    assert [reading.pid for reading in readings] == ["RPM"]
    assert len(bus.sent) >= 2
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

    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=on_reading,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 1.0,
    )

    sent_pids = [bytes(msg.data)[2] for msg in bus.sent if bytes(msg.data)[:2] == b"\x02\x01"]
    assert sent_pids.count(0x0C) > sent_pids.count(0x2F)
    assert stats.readings_emitted == len(readings)


def test_scheduler_metrics_report_rates_and_backoff_state() -> None:
    bus = _NegativeThenPositiveBus()
    stop = threading.Event()
    snapshots: list[ObdSchedulerMetrics] = []

    def on_reading(reading: Any) -> None:
        if reading.pid == "RPM":
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
        deadline_monotonic=time.monotonic() + 0.4,
        on_scheduler_metrics=snapshots.append,
        scheduler_metrics_interval_seconds=0.01,
    )

    assert stats.scheduler_snapshots >= 1
    latest = snapshots[-1].to_dict()
    assert latest["bus_budget_requests_per_second"] > spec.poll_rate_hz
    assert latest["response_rate"] is not None
    by_channel = {row["channel"]: row for row in latest["requests"]}
    assert by_channel["ENGINE_LOAD"]["backed_off"] is True
    assert by_channel["ENGINE_LOAD"]["timeouts"] >= 1
    assert by_channel["RPM"]["responses"] >= 1


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
