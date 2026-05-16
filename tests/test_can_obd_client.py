"""Tests for the Canable-native ISO 15765-4 OBD-II client (Phase 3)."""

from __future__ import annotations

import threading
import time

import can
import pytest

from src.collection.can_obd_client import (
    DEFAULT_REQUEST_ID,
    DEFAULT_RESPONSE_ID_MIN,
    PID_DECODERS,
    CanObdClient,
    decode_baro,
    decode_catalyst_temp,
    decode_commanded_lambda,
    decode_control_module_voltage,
    decode_evap_vapor_pressure_signed,
    decode_maf,
    decode_o2_voltage,
    decode_percent_a_over_255,
    decode_percent_b_minus_128,
    decode_rpm,
    decode_run_time,
    decode_speed,
    decode_temp_minus_40,
    decode_timing_advance,
    parse_dtc_payload,
    read_pid,
    read_pids,
)

# ---------------------------------------------------------------------------
# Pure decoder tests — fast, no bus
# ---------------------------------------------------------------------------


def test_decode_rpm() -> None:
    # 0x1840 / 4 = 1552
    assert decode_rpm(bytes([0x18, 0x40])) == (1552.0, "rpm")
    # Idle ~700 → raw 2800 = 0x0AF0
    assert decode_rpm(bytes([0x0A, 0xF0])) == (700.0, "rpm")


def test_decode_speed() -> None:
    assert decode_speed(bytes([0x50])) == (80.0, "kph")


def test_decode_temp_minus_40() -> None:
    # Coolant raw 0x69 (105) → 65 °C
    assert decode_temp_minus_40(bytes([0x69])) == (65.0, "degC")
    # Engine-off sentinel raw 0x00 → −40 °C (the decoder doesn't know
    # this is a sentinel; the plausible-bounds check above bindings does)
    assert decode_temp_minus_40(bytes([0x00])) == (-40.0, "degC")


def test_decode_percent_a_over_255() -> None:
    assert decode_percent_a_over_255(bytes([0xFF])) == pytest.approx((100.0, "%"))
    assert decode_percent_a_over_255(bytes([0x00])) == (0.0, "%")
    assert decode_percent_a_over_255(bytes([0x80])) == pytest.approx((50.196, "%"), abs=0.01)


def test_decode_percent_b_minus_128() -> None:
    """Fuel trim: (A−128) × 100/128. 128 → 0%, 0 → −100%, 255 → +99.2%."""
    assert decode_percent_b_minus_128(bytes([0x80])) == (0.0, "%")
    assert decode_percent_b_minus_128(bytes([0x00])) == (-100.0, "%")


def test_decode_maf() -> None:
    # 0x07D0 / 100 = 20 g/s
    assert decode_maf(bytes([0x07, 0xD0])) == (20.0, "g/s")


def test_decode_timing_advance() -> None:
    # raw 128 → 128/2 − 64 = 0°BTDC, raw 192 → 96−64 = 32°
    assert decode_timing_advance(bytes([0x80])) == (0.0, "degBTDC")
    assert decode_timing_advance(bytes([0xC0])) == (32.0, "degBTDC")


def test_decode_o2_voltage() -> None:
    # raw 0xA0 (160) / 200 = 0.8V (typical lean threshold)
    assert decode_o2_voltage(bytes([0xA0])) == (0.8, "V")


def test_decode_control_module_voltage() -> None:
    # 0x36B0 / 1000 = 14.000 V
    assert decode_control_module_voltage(bytes([0x36, 0xB0])) == (14.0, "V")


def test_decode_run_time() -> None:
    assert decode_run_time(bytes([0x01, 0x00])) == (256.0, "s")


def test_decode_baro() -> None:
    assert decode_baro(bytes([0x65])) == (101.0, "kPa")


def test_decode_commanded_lambda() -> None:
    # 0x8000 / 32768 = 1.0 stoich
    assert decode_commanded_lambda(bytes([0x80, 0x00])) == (1.0, "ratio")


def test_decode_evap_vapor_pressure_signed() -> None:
    # +100 raw → 100/4 = 25 Pa
    assert decode_evap_vapor_pressure_signed(bytes([0x00, 0x64])) == (25.0, "Pa")
    # −100 raw → −25 Pa (signed)
    assert decode_evap_vapor_pressure_signed(bytes([0xFF, 0x9C])) == (-25.0, "Pa")


def test_decode_catalyst_temp() -> None:
    # raw 0x2EE0 / 10 − 40 = 1200 − 40 = 1160°C; sanity in range
    assert decode_catalyst_temp(bytes([0x2E, 0xE0])) == (1160.0, "degC")


def test_decoders_handle_short_payloads_gracefully() -> None:
    """Every decoder must return None on too-few bytes, never raise."""
    for _pid, (name, decoder) in PID_DECODERS.items():
        assert decoder(b"") is None, f"{name} should reject empty payload"
        # A single byte is enough for some single-byte decoders; only fail
        # the multi-byte ones on len=1.
        if decoder in (decode_rpm, decode_maf, decode_run_time, decode_commanded_lambda):
            assert decoder(b"\x01") is None, f"{name} should require 2 bytes"


# ---------------------------------------------------------------------------
# DTC parsing
# ---------------------------------------------------------------------------


def test_parse_dtc_payload_single_dtc() -> None:
    # count=1, P0123 = 0x01 0x23 with system bits 00 (P), first digit 0
    assert parse_dtc_payload(bytes([0x01, 0x01, 0x23])) == ["P0123"]


def test_parse_dtc_payload_multiple_dtcs() -> None:
    # count=2: P0420 (0x04 0x20) + C0510 (0x45 0x10)
    raw = bytes([0x02, 0x04, 0x20, 0x45, 0x10])
    assert parse_dtc_payload(raw) == ["P0420", "C0510"]


def test_parse_dtc_payload_drops_null_filler() -> None:
    # count=3 with 2 real DTCs + 1 null filler
    raw = bytes([0x03, 0x04, 0x20, 0x00, 0x00, 0x45, 0x10])
    assert parse_dtc_payload(raw) == ["P0420", "C0510"]


def test_parse_dtc_payload_empty() -> None:
    assert parse_dtc_payload(b"") == []
    assert parse_dtc_payload(bytes([0x00])) == []  # count=0


def test_parse_dtc_payload_recognizes_all_systems() -> None:
    # P=00, C=01, B=10, U=11 in high two bits of byte 0
    raw = bytes([0x04, 0x01, 0x23, 0x41, 0x23, 0x81, 0x23, 0xC1, 0x23])
    assert parse_dtc_payload(raw) == ["P0123", "C0123", "B0123", "U0123"]


# ---------------------------------------------------------------------------
# Client integration — fake ECU on a virtual bus
# ---------------------------------------------------------------------------


def _make_pair():
    """Two `Bus` ends on the same virtual channel for request/response."""
    tester = can.interface.Bus(interface="virtual", channel="canary-test")
    ecu = can.interface.Bus(interface="virtual", channel="canary-test")
    return tester, ecu


def _single_frame_reply(
    *, responder_id: int, service: int, pid: int | None, body: bytes
) -> can.Message:
    """Construct a single-frame ISO-TP reply with the given service payload."""
    if pid is None:
        payload = bytes([service]) + body
    elif 0 <= pid <= 0xFF:
        payload = bytes([service, pid]) + body
    else:
        payload = bytes([service, (pid >> 8) & 0xFF, pid & 0xFF]) + body
    if len(payload) > 7:
        raise ValueError("payload too big for single-frame; use multi-frame helper")
    pci = len(payload)  # single-frame PCI byte = length, high nibble = 0
    frame = bytes([pci]) + payload
    frame += bytes(8 - len(frame))
    return can.Message(arbitration_id=responder_id, data=frame, is_extended_id=False)


def _fake_ecu_loop(
    ecu_bus: can.BusABC,
    responses: dict[tuple[int, int | None], bytes],
    *,
    responder_id: int = DEFAULT_RESPONSE_ID_MIN,
    stop: threading.Event,
) -> None:
    """Tiny ECU stub: listen for OBD requests, answer with canned bytes."""
    while not stop.is_set():
        frame = ecu_bus.recv(timeout=0.1)
        if frame is None:
            continue
        if frame.arbitration_id != DEFAULT_REQUEST_ID:
            continue
        data = bytes(frame.data)
        pci_high = (data[0] >> 4) & 0x0F
        if pci_high != 0:
            continue  # only single-frame requests
        length = data[0] & 0x0F
        body = data[1 : 1 + length]
        if not body:
            continue
        service = body[0]
        pid = body[1] if len(body) > 1 and service in (0x01, 0x09) else None
        key = (service, pid)
        canned = responses.get(key)
        if canned is None:
            continue
        ecu_bus.send(
            _single_frame_reply(
                responder_id=responder_id,
                service=0x40 | service,
                pid=pid,
                body=canned,
            )
        )


def test_query_mode_01_single_frame_round_trip() -> None:
    tester, ecu = _make_pair()
    stop = threading.Event()
    # RPM=1552 (0x1840), coolant=65°C (0x69), speed=80 km/h (0x50)
    responses = {
        (0x01, 0x0C): bytes([0x18, 0x40]),
        (0x01, 0x05): bytes([0x69]),
        (0x01, 0x0D): bytes([0x50]),
    }
    t = threading.Thread(
        target=_fake_ecu_loop, args=(ecu, responses), kwargs={"stop": stop}, daemon=True
    )
    t.start()
    try:
        client = CanObdClient(tester, timeout_seconds=2.0)
        rpm = read_pid(client, 0x0C)
        coolant = read_pid(client, 0x05)
        speed = read_pid(client, 0x0D)
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()

    assert rpm is not None and rpm.name == "RPM" and rpm.value == 1552.0
    assert coolant is not None and coolant.name == "COOLANT_TEMP" and coolant.value == 65.0
    assert speed is not None and speed.name == "SPEED" and speed.value == 80.0


def test_query_returns_none_on_timeout() -> None:
    tester, _ecu = _make_pair()
    try:
        # No ECU stub running — the request should send but no response arrives.
        client = CanObdClient(tester, timeout_seconds=0.2)
        assert client.query_mode_01(0x0C) is None
    finally:
        tester.shutdown()
        _ecu.shutdown()


def test_query_rejects_wrong_pid_echo() -> None:
    """A response with the wrong PID echo must be ignored (different request)."""
    tester, ecu = _make_pair()
    stop = threading.Event()
    # Only respond to PID 0x05, not 0x0C — so a 0x0C query times out.
    responses = {(0x01, 0x05): bytes([0x69])}
    t = threading.Thread(
        target=_fake_ecu_loop, args=(ecu, responses), kwargs={"stop": stop}, daemon=True
    )
    t.start()
    try:
        client = CanObdClient(tester, timeout_seconds=0.5)
        assert client.query_mode_01(0x0C) is None
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()


def test_discover_supported_pids_walks_bitmasks() -> None:
    """Walk PID 0x00 → 0x20 → … decoding the bitmasks the ECU returns."""
    tester, ecu = _make_pair()
    stop = threading.Event()
    # PID 0x00 bitmask: bits set for 0x01 (PID supported), 0x05 (coolant),
    # 0x0C (RPM), 0x0D (speed). MSB-first: bit 31=PID 0x01, bit 27=0x05, bit 20=0x0C, bit 19=0x0D.
    bits = (
        (1 << 31)  # PID 0x01
        | (1 << 27)  # PID 0x05
        | (1 << 20)  # PID 0x0C
        | (1 << 19)  # PID 0x0D
        # bit 0 (continuation marker) clear → walk stops after this bitmask
    )
    responses = {(0x01, 0x00): bits.to_bytes(4, byteorder="big")}
    t = threading.Thread(
        target=_fake_ecu_loop, args=(ecu, responses), kwargs={"stop": stop}, daemon=True
    )
    t.start()
    try:
        client = CanObdClient(tester, timeout_seconds=1.0)
        pids = client.query_supported_mode_01_pids()
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()
    assert pids == (0x01, 0x05, 0x0C, 0x0D)


def test_discover_supported_pids_continues_past_continuation_bit() -> None:
    """When PID 0x00's bitmask has the low bit set, walker continues to 0x20."""
    tester, ecu = _make_pair()
    stop = threading.Event()
    # 0x00 reports continuation + PID 0x0C; 0x20 reports PID 0x21 only.
    bits_a = (1 << 20) | 0x01  # PID 0x0C + continuation
    bits_b = 1 << 31  # PID 0x21, no continuation
    responses = {
        (0x01, 0x00): bits_a.to_bytes(4, "big"),
        (0x01, 0x20): bits_b.to_bytes(4, "big"),
    }
    t = threading.Thread(
        target=_fake_ecu_loop, args=(ecu, responses), kwargs={"stop": stop}, daemon=True
    )
    t.start()
    try:
        client = CanObdClient(tester, timeout_seconds=1.0)
        pids = client.query_supported_mode_01_pids()
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()
    assert pids == (0x0C, 0x21)


def test_read_pids_sequential() -> None:
    """``read_pids`` queries each PID in order and returns decoded results."""
    tester, ecu = _make_pair()
    stop = threading.Event()
    responses = {
        (0x01, 0x0C): bytes([0x18, 0x40]),
        (0x01, 0x05): bytes([0x69]),
    }
    t = threading.Thread(
        target=_fake_ecu_loop, args=(ecu, responses), kwargs={"stop": stop}, daemon=True
    )
    t.start()
    try:
        client = CanObdClient(tester, timeout_seconds=1.0)
        out = read_pids(client, [0x0C, 0x05, 0x0D])  # 0x0D won't answer
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()
    assert [d.name for d in out] == ["RPM", "COOLANT_TEMP"]


def test_query_mode_03_single_frame() -> None:
    """Mode 03 (DTC list) — single-frame response with two codes."""
    tester, ecu = _make_pair()
    stop = threading.Event()
    # Service 0x03, payload = count=2 then P0420 + C0510
    responses = {(0x03, None): bytes([0x02, 0x04, 0x20, 0x45, 0x10])}
    t = threading.Thread(
        target=_fake_ecu_loop, args=(ecu, responses), kwargs={"stop": stop}, daemon=True
    )
    t.start()
    try:
        client = CanObdClient(tester, timeout_seconds=1.0)
        resp = client.query_mode_03()
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()
    assert resp is not None
    assert parse_dtc_payload(resp.payload) == ["P0420", "C0510"]


def test_query_mode_22_single_frame_with_physical_request_id() -> None:
    """Mode 22 uses a 16-bit DID echo and usually a physical tester ID."""
    tester, ecu = _make_pair()
    stop = threading.Event()

    def _did_responder() -> None:
        while not stop.is_set():
            frame = ecu.recv(timeout=0.1)
            if frame is None:
                continue
            if frame.arbitration_id != 0x7E0:
                continue
            data = bytes(frame.data)
            if data[:4] != bytes([0x03, 0x22, 0x11, 0x01]):
                continue
            ecu.send(
                _single_frame_reply(
                    responder_id=DEFAULT_RESPONSE_ID_MIN,
                    service=0x62,
                    pid=0x1101,
                    body=bytes([0x12, 0x34]),
                )
            )
            return

    t = threading.Thread(target=_did_responder, daemon=True)
    t.start()
    try:
        client = CanObdClient(tester, timeout_seconds=1.0)
        resp = client.query_mode_22(0x1101, request_id=0x7E0)
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()

    assert resp is not None
    assert resp.service == 0x62
    assert resp.pid == 0x1101
    assert resp.payload == bytes([0x12, 0x34])


def test_query_handles_isotp_multi_frame_reassembly() -> None:
    """Mode 09 VIN: ISO-TP first frame + consecutive frames + flow control."""
    tester, ecu = _make_pair()
    stop = threading.Event()
    vin_bytes = b"JN8AR05Y58W123456"  # 17 ascii chars

    def _vin_responder() -> None:
        # Wait for the Mode 09 PID 0x02 request
        while not stop.is_set():
            frame = ecu.recv(timeout=0.1)
            if frame is None:
                continue
            if frame.arbitration_id != DEFAULT_REQUEST_ID:
                continue
            data = bytes(frame.data)
            if data[:3] != bytes([0x02, 0x09, 0x02]):
                continue
            # Build the response: service 0x49, PID 0x02, count=1, then VIN.
            # Total response payload (without ISO-TP framing): 49 02 01 + 17 = 20 bytes
            response_body = bytes([0x49, 0x02, 0x01]) + vin_bytes
            total_len = len(response_body)
            # First frame: PCI = 0x1L LL (4 bits + 8 bits length); 6 data bytes
            first = bytes([0x10 | (total_len >> 8), total_len & 0xFF]) + response_body[:6]
            ecu.send(
                can.Message(
                    arbitration_id=DEFAULT_RESPONSE_ID_MIN,
                    data=first,
                    is_extended_id=False,
                )
            )
            # Wait for flow control from the tester (0x7E0 = 0x7E8 − 8)
            t_end = time.monotonic() + 0.5
            saw_fc = False
            while time.monotonic() < t_end:
                fc = ecu.recv(timeout=0.05)
                if fc is None:
                    continue
                if fc.arbitration_id == DEFAULT_RESPONSE_ID_MIN - 8 and bytes(fc.data)[0] == 0x30:
                    saw_fc = True
                    break
            if not saw_fc:
                return
            # Consecutive frames: sequence 1, 2, 3 — 7 bytes each
            remaining = response_body[6:]
            seq = 1
            while remaining:
                chunk = remaining[:7]
                remaining = remaining[7:]
                frame_data = bytes([0x20 | (seq & 0x0F)]) + chunk
                frame_data += bytes(8 - len(frame_data))
                ecu.send(
                    can.Message(
                        arbitration_id=DEFAULT_RESPONSE_ID_MIN,
                        data=frame_data,
                        is_extended_id=False,
                    )
                )
                seq += 1
            return

    t = threading.Thread(target=_vin_responder, daemon=True)
    t.start()
    try:
        client = CanObdClient(tester, timeout_seconds=2.0)
        resp = client.query_mode_09(0x02)
    finally:
        stop.set()
        t.join(timeout=2.0)
        tester.shutdown()
        ecu.shutdown()
    assert resp is not None
    # payload = count byte + VIN (response_body's `49 02` header is stripped
    # by _extract_service_payload because we passed pid=0x02 echo).
    assert resp.payload == bytes([0x01]) + vin_bytes
