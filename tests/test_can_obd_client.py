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
    decode_absolute_load,
    decode_baro,
    decode_catalyst_temp,
    decode_commanded_lambda,
    decode_control_module_voltage,
    decode_evap_vapor_pressure_signed,
    decode_maf,
    decode_mil_on,
    decode_monitor_status,
    decode_o2_voltage,
    decode_percent_a_over_255,
    decode_percent_b_minus_128,
    decode_rpm,
    decode_run_time,
    decode_speed,
    decode_temp_minus_40,
    decode_timing_advance,
    decode_wide_lambda,
    parse_dtc_payload,
    read_pid,
    read_pids,
)
from src.collection.isotp import split_batched_did_response
from src.validation.scripted_ecu import FakeClock, ScriptedEcu, single_frame_reply, support_bits

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


def test_decode_timing_advance_rejects_unavailable_sentinel() -> None:
    """Raw 0xFF is "not available", not 63.5°BTDC.

    This ECU returns 0xFF whenever the engine is not running — measured at
    63.5 in 1012/1012 samples at RPM==0 and 0/683 while running. Decoding it
    as a number put a constant into the isolation forest's training corpus
    (session 2026-07-22T154954 is 100% sentinel).
    """
    assert decode_timing_advance(bytes([0xFF])) is None
    # The neighbouring value is still a legitimate (if extreme) reading.
    assert decode_timing_advance(bytes([0xFE])) == (63.0, "degBTDC")


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


def test_decode_wide_lambda() -> None:
    # AB=0x8000 → 32768/32768 = 1.0 stoich; CD bytes (voltage/current) unused
    assert decode_wide_lambda(bytes([0x80, 0x00, 0x12, 0x34])) == (1.0, "ratio")
    # AB=0x6000 → 24576/32768 = 0.75 (rich)
    assert decode_wide_lambda(bytes([0x60, 0x00])) == pytest.approx((0.75, "ratio"))


def test_decode_absolute_load() -> None:
    """Task 2.3: 2-byte percent, ``(A*256+B)*100/255`` — can exceed 100%
    (single-byte ``decode_percent_a_over_255`` cannot, the bug this fixes)."""
    assert decode_absolute_load(bytes([0x00, 0xFF])) == pytest.approx((100.0, "%"))
    assert decode_absolute_load(bytes([0x00, 0x00])) == (0.0, "%")
    # raw 0x0190 (400) * 100/255 ≈ 156.9% — above 100%, a real boosted-engine
    # value the old single-byte decode (max 255 → 100%) would have wrapped.
    assert decode_absolute_load(bytes([0x01, 0x90])) == pytest.approx((156.86, "%"), abs=0.01)


def test_pid_decoders_matches_pre_refactor_golden_shape() -> None:
    """Task 1.1: MODE01_REGISTRY's channel names are now generated from
    PID_DECODERS (not independently hand-typed) — pin PID_DECODERS's exact
    pre-refactor keys/names/decoders so nothing upstream can silently add,
    drop, rename, or re-decoder a PID out from under that derivation."""
    golden = {
        0x01: ("MONITOR_STATUS", decode_monitor_status),
        0x03: "FUEL_STATUS",
        0x04: "ENGINE_LOAD",
        0x05: ("COOLANT_TEMP", decode_temp_minus_40),
        0x06: "SHORT_FUEL_TRIM_1",
        0x07: "LONG_FUEL_TRIM_1",
        0x08: "SHORT_FUEL_TRIM_2",
        0x09: "LONG_FUEL_TRIM_2",
        0x0B: "INTAKE_PRESSURE",
        0x0C: ("RPM", decode_rpm),
        0x0D: ("SPEED", decode_speed),
        0x0E: ("TIMING_ADVANCE", decode_timing_advance),
        0x0F: "INTAKE_TEMP",
        0x10: ("MAF", decode_maf),
        0x11: "THROTTLE_POS",
        0x14: ("O2_B1S1", decode_o2_voltage),
        0x15: ("O2_B1S2", decode_o2_voltage),
        0x18: ("O2_B2S1", decode_o2_voltage),
        0x19: ("O2_B2S2", decode_o2_voltage),
        0x1F: ("RUN_TIME", decode_run_time),
        0x21: "DISTANCE_W_MIL",
        0x24: ("O2_WR_LAMBDA_S1", decode_wide_lambda),
        0x25: ("O2_WR_LAMBDA_S2", decode_wide_lambda),
        # Bank 2 upstream under the PID 0x13 layout (D03.34, 2026-09-26).
        0x28: ("O2_WR_LAMBDA_S5", decode_wide_lambda),
        0x2C: "COMMANDED_EGR",
        0x2D: "EGR_ERROR",
        0x2E: "EVAPORATIVE_PURGE",
        0x2F: "FUEL_LEVEL",
        0x30: "WARMUPS_SINCE_DTC_CLEAR",
        0x31: "DISTANCE_SINCE_DTC_CLEAR",
        0x32: ("EVAP_VAPOR_PRESSURE", decode_evap_vapor_pressure_signed),
        0x33: ("BAROMETRIC_PRESSURE", decode_baro),
        0x34: ("O2_WR_CURRENT_S1", decode_wide_lambda),
        0x35: ("O2_WR_CURRENT_S2", decode_wide_lambda),
        0x3C: ("CATALYST_TEMP_B1S1", decode_catalyst_temp),
        0x3D: ("CATALYST_TEMP_B2S1", decode_catalyst_temp),
        0x3E: ("CATALYST_TEMP_B1S2", decode_catalyst_temp),
        0x3F: ("CATALYST_TEMP_B2S2", decode_catalyst_temp),
        0x42: ("CONTROL_MODULE_VOLTAGE", decode_control_module_voltage),
        0x43: ("ABSOLUTE_LOAD", decode_absolute_load),
        0x44: ("COMMANDED_EQUIV_RATIO", decode_commanded_lambda),
        0x45: "RELATIVE_THROTTLE_POS",
        0x46: "AMBIANT_AIR_TEMP",
        0x47: "ABS_THROTTLE_POS_B",
        0x49: "ACCEL_PEDAL_POS_D",
        0x4A: "ACCEL_PEDAL_POS_E",
        0x4C: "COMMANDED_THROTTLE_ACTUATOR",
        0x5C: "OIL_TEMP",
    }
    golden_names = {
        pid: (entry if isinstance(entry, str) else entry[0]) for pid, entry in golden.items()
    }
    assert set(PID_DECODERS) == set(golden_names)
    for pid, name in golden_names.items():
        assert PID_DECODERS[pid][0] == name, f"PID 0x{pid:02X} channel name drifted"
    # Decoder identity for the entries where the golden data pins a specific
    # function (percent/temp-family PIDs share formulas, so only functions
    # unique to one PID are pinned above; the rest are covered by
    # test_decoders_handle_short_payloads_gracefully iterating every entry).
    for pid, entry in golden.items():
        if isinstance(entry, tuple):
            assert PID_DECODERS[pid][1] is entry[1], f"PID 0x{pid:02X} decoder drifted"


def test_decoders_handle_short_payloads_gracefully() -> None:
    """Every decoder must return None on too-few bytes, never raise."""
    for _pid, (name, decoder) in PID_DECODERS.items():
        assert decoder(b"") is None, f"{name} should reject empty payload"
        # A single byte is enough for some single-byte decoders; only fail
        # the multi-byte ones on len=1.
        if decoder in (
            decode_rpm,
            decode_maf,
            decode_run_time,
            decode_commanded_lambda,
            decode_wide_lambda,
            decode_absolute_load,
        ):
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
# MIL / monitor-status decoding (Task 1.2/1.4)
# ---------------------------------------------------------------------------


def test_decode_monitor_status_masks_off_mil_bit() -> None:
    # bit 7 set (MIL on) + count=3 in bits 0-6 -> count-only channel is 3.
    assert decode_monitor_status(bytes([0x83, 0x00, 0x00, 0x00])) == (3.0, "count")


def test_decode_mil_on_reads_bit_seven() -> None:
    assert decode_mil_on(bytes([0x83, 0x00, 0x00, 0x00])) is True
    assert decode_mil_on(bytes([0x03, 0x00, 0x00, 0x00])) is False
    assert decode_mil_on(b"") is None


# ---------------------------------------------------------------------------
# Client integration — fake ECU on a virtual bus
# ---------------------------------------------------------------------------


def _make_pair() -> tuple[can.BusABC, can.BusABC]:
    """Two `Bus` ends on the same virtual channel for request/response."""
    tester = can.interface.Bus(interface="virtual", channel="canary-test")
    ecu = can.interface.Bus(interface="virtual", channel="canary-test")
    return tester, ecu


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
        pid = body[1] if len(body) > 1 and service in (0x01, 0x06, 0x09) else None
        key = (service, pid)
        canned = responses.get(key)
        if canned is None:
            continue
        ecu_bus.send(
            single_frame_reply(
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


def test_negative_response_returns_none_without_waiting_full_timeout() -> None:
    """A 7F <service> NRC must short-circuit, not burn the whole timeout.

    Critical for DID sweeps: an absent DID otherwise costs one full
    timeout each (hours over a wide range).
    """
    tester, ecu = _make_pair()
    stop = threading.Event()

    def ecu_loop() -> None:
        while not stop.is_set():
            f = ecu.recv(timeout=0.1)
            if f is None:
                continue
            d = bytes(f.data)
            if len(d) >= 4 and d[1] == 0x22:  # Mode 22 request → negative
                ecu.send(
                    can.Message(
                        arbitration_id=0x7E8,
                        data=bytes([0x03, 0x7F, 0x22, 0x31, 0, 0, 0, 0]),
                        is_extended_id=False,
                    )
                )

    t = threading.Thread(target=ecu_loop, daemon=True)
    t.start()
    try:
        client = CanObdClient(
            tester,
            request_id=0x7E0,
            response_id_min=0x7E8,
            response_id_max=0x7E8,
            timeout_seconds=5.0,
        )
        t0 = time.monotonic()
        assert client.query_mode_22(0x1101) is None
        assert time.monotonic() - t0 < 1.0  # returned on the NRC, not at 5 s
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()


def test_enter_diagnostic_session_positive_and_negative() -> None:
    tester, ecu = _make_pair()
    stop = threading.Event()

    def ecu_loop() -> None:
        while not stop.is_set():
            f = ecu.recv(timeout=0.1)
            if f is None:
                continue
            d = bytes(f.data)
            if len(d) >= 3 and d[1] == 0x10:  # DiagnosticSessionControl
                if d[2] == 0xC0:
                    body = [0x02, 0x50, 0xC0]
                else:
                    body = [0x03, 0x7F, 0x10, 0x12]  # subFunctionNotSupported
                ecu.send(
                    can.Message(
                        arbitration_id=0x7E8,
                        data=bytes(body + [0] * (8 - len(body))),
                        is_extended_id=False,
                    )
                )

    t = threading.Thread(target=ecu_loop, daemon=True)
    t.start()
    try:
        c = CanObdClient(
            tester,
            request_id=0x7E0,
            response_id_min=0x7E8,
            response_id_max=0x7E8,
            timeout_seconds=2.0,
        )
        assert c.enter_diagnostic_session(0xC0) is True
        assert c.enter_diagnostic_session(0x03) is False
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()


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


def test_discover_supported_mode06_mids_walks_bitmasks() -> None:
    tester, ecu = _make_pair()
    stop = threading.Event()
    # MID 00 reports 01 plus continuation to 20; MID 20 reports 21 and stops.
    responses = {
        (0x06, 0x00): ((1 << 31) | 0x01).to_bytes(4, "big"),
        (0x06, 0x20): (1 << 31).to_bytes(4, "big"),
    }
    thread = threading.Thread(
        target=_fake_ecu_loop, args=(ecu, responses), kwargs={"stop": stop}, daemon=True
    )
    thread.start()
    try:
        client = CanObdClient(tester, timeout_seconds=1.0)
        mids = client.query_supported_mode_06_mids()
    finally:
        stop.set()
        thread.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()
    assert mids == (0x01, 0x21)


def test_discover_supported_pids_merges_multiple_responders() -> None:
    """Functional supported-PID probes must not trust only the first ECU."""
    clock = FakeClock()
    bus = ScriptedEcu(
        clock,
        # Engine: RPM + SPEED in group 0x00, silent on 0x20.
        mode01_support={0x00: support_bits(0x0C, 0x0D)},
        # Transmission: a continuation to 0x20 only, then EVAP purge + fuel level.
        support_responders={
            0x7E9: {0x00: support_bits(more=True), 0x20: support_bits(0x2E, 0x2F, base=0x20)}
        },
    )
    client = CanObdClient(bus, timeout_seconds=0.2, time_fn=clock.monotonic)  # type: ignore[arg-type]

    assert client.query_supported_mode_01_pids() == (0x0C, 0x0D, 0x2E, 0x2F)
    assert [bytes(msg.data)[2] for msg in bus.messages] == [0x00, 0x20]


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


def test_query_sends_any_service_byte() -> None:
    """`query()` passes the caller's service byte straight through — no
    allowlist. Mode 04 (CLEAR_DTC, a write) is the case that used to be
    refused, so assert the frame actually reaches the bus."""
    tester, ecu = _make_pair()
    try:
        client = CanObdClient(tester, timeout_seconds=0.2)
        assert client.query(service=0x04, identifier=None) is None  # nothing answers
        sent = ecu.recv(timeout=0.5)
        assert sent is not None
        assert sent.arbitration_id == DEFAULT_REQUEST_ID
        assert sent.data[0] == 0x01  # single-frame PCI: one payload byte
        assert sent.data[1] == 0x04  # ...and that byte is the service
    finally:
        tester.shutdown()
        ecu.shutdown()


def test_query_generic_matches_dedicated_mode_03_method() -> None:
    """The generic escape hatch round-trips a DTC-shaped (no-identifier)
    service identically to the dedicated `query_mode_03` method."""
    tester, ecu = _make_pair()
    stop = threading.Event()
    responses = {(0x03, None): bytes([0x01, 0x04, 0x20])}
    t = threading.Thread(
        target=_fake_ecu_loop, args=(ecu, responses), kwargs={"stop": stop}, daemon=True
    )
    t.start()
    try:
        client = CanObdClient(tester, timeout_seconds=1.0)
        resp = client.query(service=0x03, identifier=None)
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()
    assert resp is not None
    assert parse_dtc_payload(resp.payload) == ["P0420"]


def test_query_mode_07_single_frame() -> None:
    """Mode 07 (pending DTC list, Task 1.1) — same wire shape as Mode 03."""
    tester, ecu = _make_pair()
    stop = threading.Event()
    responses = {(0x07, None): bytes([0x01, 0x01, 0x23])}
    t = threading.Thread(
        target=_fake_ecu_loop, args=(ecu, responses), kwargs={"stop": stop}, daemon=True
    )
    t.start()
    try:
        client = CanObdClient(tester, timeout_seconds=1.0)
        resp = client.query_mode_07()
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()
    assert resp is not None
    assert parse_dtc_payload(resp.payload) == ["P0123"]


def test_query_mode_0a_single_frame_drops_null_filler() -> None:
    """Mode 0A (permanent DTC list, Task 1.1) — null-filler payload case."""
    tester, ecu = _make_pair()
    stop = threading.Event()
    # count=2: null filler + P0420 (single-frame body is capped at 7 bytes,
    # so this stays within that while still exercising the filler drop).
    responses = {(0x0A, None): bytes([0x02, 0x00, 0x00, 0x04, 0x20])}
    t = threading.Thread(
        target=_fake_ecu_loop, args=(ecu, responses), kwargs={"stop": stop}, daemon=True
    )
    t.start()
    try:
        client = CanObdClient(tester, timeout_seconds=1.0)
        resp = client.query_mode_0A()
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()
    assert resp is not None
    assert parse_dtc_payload(resp.payload) == ["P0420"]


def test_query_mode_22_batch_reports_the_rejection_nrc() -> None:
    """An ECM that refuses multi-DID reads must be distinguishable from silence.

    The 2008 Nissan ECM answers ``7F 22 12`` to ``05 22 12 01 12 03`` while
    answering each DID singly. Collapsing that into ``None`` is what let a
    "batched" sweep report the same DIDs it would have read one at a time.
    """
    tester, ecu = _make_pair()
    stop = threading.Event()

    def _rejecting_ecu() -> None:
        while not stop.is_set():
            frame = ecu.recv(timeout=0.1)
            if frame is None:
                continue
            data = bytes(frame.data)
            if frame.arbitration_id != 0x7E0 or data[1] != 0x22:
                continue
            ecu.send(
                can.Message(
                    arbitration_id=DEFAULT_RESPONSE_ID_MIN,
                    data=bytes([0x03, 0x7F, 0x22, 0x12, 0x00, 0x00, 0x00, 0x00]),
                    is_extended_id=False,
                )
            )
            return

    t = threading.Thread(target=_rejecting_ecu, daemon=True)
    t.start()
    try:
        client = CanObdClient(tester, timeout_seconds=1.0)
        reply = client.query_mode_22_batch([0x1201, 0x1203], request_id=0x7E0)
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()

    assert reply.response is None
    assert reply.sent is True
    assert reply.negative_response_code == 0x12


def test_query_mode_22_batch_silence_has_no_nrc() -> None:
    """Nothing answered is a different empty from a refusal — and says so."""
    tester, ecu = _make_pair()
    try:
        client = CanObdClient(tester, timeout_seconds=0.05)
        reply = client.query_mode_22_batch([0x1201, 0x1203], request_id=0x7E0)
    finally:
        tester.shutdown()
        ecu.shutdown()
    assert reply.response is None
    assert reply.negative_response_code is None
    assert reply.sent is True


def test_query_mode_22_batch_splits_a_positive_response() -> None:
    tester, ecu = _make_pair()
    stop = threading.Event()

    def _batching_ecu() -> None:
        while not stop.is_set():
            frame = ecu.recv(timeout=0.1)
            if frame is None:
                continue
            data = bytes(frame.data)
            if frame.arbitration_id != 0x7E0 or data[1] != 0x22:
                continue
            body = bytes([0x62, 0x12, 0x01, 0x0A, 0x12, 0x03, 0xBB])
            ecu.send(
                can.Message(
                    arbitration_id=DEFAULT_RESPONSE_ID_MIN,
                    data=bytes([len(body), *body]),
                    is_extended_id=False,
                )
            )
            return

    t = threading.Thread(target=_batching_ecu, daemon=True)
    t.start()
    try:
        client = CanObdClient(tester, timeout_seconds=1.0)
        reply = client.query_mode_22_batch([0x1201, 0x1203], request_id=0x7E0)
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()

    assert reply.negative_response_code is None
    assert reply.response is not None
    readings, clean = split_batched_did_response(reply.response.payload, {0x1201: 1, 0x1203: 1}.get)
    assert clean is True
    assert readings == {0x1201: b"\x0a", 0x1203: b"\xbb"}


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
                single_frame_reply(
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
    # by isotp.extract_service_payload because we passed pid=0x02 echo).
    assert resp.payload == bytes([0x01]) + vin_bytes


def test_query_or_nrc_tells_a_refusal_from_silence() -> None:
    """``(None, nrc)`` is the ECU refusing; ``(None, None)`` is nothing on the bus.

    Measured 2026-09-22 KOEO: ``21 10`` outside session 0xC0 → ``7F 21 80``
    (serviceNotSupportedInActiveDiagnosticSession). ``query()`` keeps
    returning ``None`` for both, as its polling callers expect.
    """
    tester, ecu = _make_pair()
    stop = threading.Event()

    def ecu_loop() -> None:
        while not stop.is_set():
            f = ecu.recv(timeout=0.1)
            if f is None:
                continue
            d = bytes(f.data)
            if len(d) >= 3 and d[1] == 0x21:  # local-identifier read in the wrong session
                ecu.send(
                    can.Message(
                        arbitration_id=0x7E8,
                        data=bytes([0x03, 0x7F, 0x21, 0x80, 0, 0, 0, 0]),
                        is_extended_id=False,
                    )
                )
            # anything else: silence

    t = threading.Thread(target=ecu_loop, daemon=True)
    t.start()
    try:
        client = CanObdClient(
            tester,
            request_id=0x7E0,
            response_id_min=0x7E8,
            response_id_max=0x7E8,
            timeout_seconds=2.0,
        )
        t0 = time.monotonic()
        assert client.query_or_nrc(service=0x21, identifier=0x10) == (None, 0x80)
        assert time.monotonic() - t0 < 1.0  # returned on the NRC, not at 2 s
        assert client.query_or_nrc(service=0x3E, identifier=None) == (None, None)
        assert client.query(service=0x21, identifier=0x10) is None
    finally:
        stop.set()
        t.join(timeout=1.0)
        tester.shutdown()
        ecu.shutdown()


# ---------------------------------------------------------------------------
# R02.5 — the response policy lives in the client, not in each caller
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _ScriptedBus:
    """Answers each request with scripted ``(delay, responder, frame)`` replies
    on a fake clock, so timing assertions are exact."""

    def __init__(self, clock: _Clock, replies: list[tuple[float, int, bytes]]) -> None:
        self.clock = clock
        self.script = replies
        self.pending: list[tuple[float, int, bytes]] = []
        self.sent: list[can.Message] = []

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.sent.append(msg)
        if bytes(msg.data)[0] >> 4 == 0x3:  # our flow control
            return
        now = self.clock.now
        self.pending = sorted(
            [(now + delay, responder, frame) for delay, responder, frame in self.script],
            key=lambda item: item[0],
        )

    def recv(self, timeout: float | None = None) -> can.Message | None:
        wait = float(timeout or 0.0)
        if self.pending and self.pending[0][0] <= self.clock.now + wait:
            due, responder, frame = self.pending.pop(0)
            self.clock.now = max(self.clock.now, due)
            return can.Message(arbitration_id=responder, data=frame, is_extended_id=False)
        self.clock.now += wait
        return None


def _frame(*body: int) -> bytes:
    data = bytes([len(body), *body])
    return data + bytes(8 - len(data))


def test_physical_request_ignores_another_modules_reply() -> None:
    """``obd_request`` to 0x7E0 accepted any 0x7E8-0x7EF reply, so a 0x7E9
    answer to the same DID was attached to the engine ECU."""
    clock = _Clock()
    bus = _ScriptedBus(
        clock,
        [
            (0.005, 0x7E9, _frame(0x62, 0x11, 0x01, 0xAA, 0xBB)),
            (0.010, 0x7E8, _frame(0x62, 0x11, 0x01, 0x12, 0x34)),
        ],
    )
    client = CanObdClient(bus, timeout_seconds=0.2, time_fn=clock)  # type: ignore[arg-type]
    response = client.query(service=0x22, identifier=0x1101, request_id=0x7E0)
    assert response is not None
    assert response.responder_id == 0x7E8
    assert response.payload == bytes([0x12, 0x34])


def test_physical_request_with_only_a_foreign_reply_is_silence() -> None:
    clock = _Clock()
    bus = _ScriptedBus(clock, [(0.005, 0x7E9, _frame(0x62, 0x11, 0x01, 0xAA, 0xBB))])
    client = CanObdClient(bus, timeout_seconds=0.2, time_fn=clock)  # type: ignore[arg-type]
    assert client.query_or_nrc(service=0x22, identifier=0x1101, request_id=0x7E0) == (None, None)


def test_client_honours_response_pending() -> None:
    """``7F 22 78`` is the ECU saying the answer is coming (ISO 14229 P2*).
    The client waited out its original deadline and returned nothing."""
    clock = _Clock()
    bus = _ScriptedBus(
        clock,
        [
            (0.010, 0x7E8, _frame(0x7F, 0x22, 0x78)),
            (0.350, 0x7E8, _frame(0x62, 0x11, 0x01, 0x12, 0x34)),
        ],
    )
    client = CanObdClient(bus, timeout_seconds=0.2, time_fn=clock)  # type: ignore[arg-type]
    response = client.query_mode_22(0x1101, request_id=0x7E0)
    assert response is not None
    assert response.payload == bytes([0x12, 0x34])


def test_client_response_pending_is_bounded() -> None:
    """An ECU that answers 0x78 forever ends as NRC 0x78, not a hung request."""
    clock = _Clock()
    bus = _ScriptedBus(
        clock, [(0.01 + 0.1 * i, 0x7E8, _frame(0x7F, 0x22, 0x78)) for i in range(40)]
    )
    client = CanObdClient(bus, timeout_seconds=0.2, time_fn=clock)  # type: ignore[arg-type]
    started = clock.now
    assert client.query_or_nrc(service=0x22, identifier=0x1101, request_id=0x7E0) == (None, 0x78)
    assert clock.now - started < 0.2 + 5 * 0.5 + 0.01


def test_a_declared_response_id_is_the_pin_not_request_plus_8() -> None:
    """A profile's request/response pair is vehicle data (``dids.yaml``
    ``ecu_request_id``/``ecu_response_id``, as the calibration route builds
    it). A module answering 0x745 on 0x765 must be heard, and a 0x74D reply
    (request + 8) must not be taken for it."""
    clock = _Clock()
    bus = _ScriptedBus(
        clock,
        [
            (0.005, 0x74D, _frame(0x62, 0x11, 0x01, 0xAA, 0xBB)),
            (0.010, 0x765, _frame(0x62, 0x11, 0x01, 0x12, 0x34)),
        ],
    )
    client = CanObdClient(
        bus,  # type: ignore[arg-type]
        request_id=0x745,
        response_id_min=0x765,
        response_id_max=0x765,
        timeout_seconds=0.2,
        time_fn=clock,
    )
    response = client.query_mode_22(0x1101)
    assert response is not None
    assert response.responder_id == 0x765
    assert response.payload == bytes([0x12, 0x34])
