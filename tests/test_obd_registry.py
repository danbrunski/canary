"""Tests for the typed SAE J1979 registry (Task 6.1).

Every decoded PID is exercised against a canned raw response so the
formula table can't drift; coverage classification is checked against the
four honest outcomes (decoded / supported-not-decoded / unsupported /
unknown).
"""

from __future__ import annotations

from src.collection.can_obd_client import PID_DECODERS
from src.collection.obd_registry import (
    MODE01_REGISTRY,
    MODE09_REGISTRY,
    SERVICE_REGISTRY,
    classify_pid,
    decode_cvn,
    decode_fuel_rate,
    decode_injection_timing,
    decode_minutes,
    decode_torque_percent,
    decode_vin,
    decode_wide_lambda,
    lookup,
    mode01_support_group,
    parse_mode06_response,
)

# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------


def test_every_pid_decoders_entry_is_in_registry_with_same_name() -> None:
    """The metadata layer must not drift from the collector's decoder table."""
    for pid, (name, _decoder) in PID_DECODERS.items():
        assert pid in MODE01_REGISTRY, f"PID 0x{pid:02X} missing from registry"
        assert MODE01_REGISTRY[pid].channel == name


def test_registry_reuses_pid_decoders_callables_not_copies() -> None:
    """Decoders shared with PID_DECODERS must be the *same* object."""
    for pid, (_name, decoder) in PID_DECODERS.items():
        assert MODE01_REGISTRY[pid].decoder is decoder


def test_support_group_assignment() -> None:
    assert mode01_support_group(0x05) == 0x00
    assert mode01_support_group(0x20) == 0x00
    assert mode01_support_group(0x21) == 0x20
    assert mode01_support_group(0x42) == 0x40
    assert mode01_support_group(0x63) == 0x60
    assert MODE01_REGISTRY[0x0C].support_group == 0x00
    assert MODE01_REGISTRY[0x42].support_group == 0x40


def test_supported_not_decoded_entries_exist() -> None:
    """At least one real PID is intentionally undecoded (must not vanish)."""
    undecoded = [d for d in MODE01_REGISTRY.values() if d.decoder is None]
    assert undecoded
    assert 0x41 in MODE01_REGISTRY  # monitor status this drive cycle
    assert MODE01_REGISTRY[0x41].decoder is None


def test_poll_tiers_are_valid() -> None:
    valid = {"fast", "medium", "slow", "on_demand"}
    for d in MODE01_REGISTRY.values():
        assert d.poll_tier in valid
    # Fast powertrain channels are on the fast tier.
    for pid in (0x0C, 0x0D, 0x04, 0x11, 0x10):
        assert MODE01_REGISTRY[pid].poll_tier == "fast"


def test_monitor_status_pid_is_slow_tier_and_decoded() -> None:
    """Task 1.2: PID 0x01 (MIL/DTC count) rides the slow telemetry tier."""
    entry = MODE01_REGISTRY[0x01]
    assert entry.channel == "MONITOR_STATUS"
    assert entry.poll_tier == "slow"
    assert entry.decoder is not None
    assert entry.decoder(bytes([0x83, 0x00, 0x00, 0x00])) == (3.0, "count")


# ---------------------------------------------------------------------------
# New decoders (canned raw responses)
# ---------------------------------------------------------------------------


def test_decode_wide_lambda_stoich() -> None:
    # 0x8000 / 32768 = 1.0 λ; trailing voltage bytes ignored
    assert decode_wide_lambda(bytes([0x80, 0x00, 0x12, 0x34])) == (1.0, "ratio")


def test_decode_torque_percent_signed_offset() -> None:
    assert decode_torque_percent(bytes([125])) == (0.0, "%")
    assert decode_torque_percent(bytes([225])) == (100.0, "%")


def test_decode_minutes() -> None:
    assert decode_minutes(bytes([0x00, 0x3C])) == (60.0, "min")


def test_decode_injection_timing_zero_point() -> None:
    # (26880 - 26880) / 128 = 0 deg
    assert decode_injection_timing((26880).to_bytes(2, "big")) == (0.0, "deg")


def test_decode_fuel_rate() -> None:
    assert decode_fuel_rate(bytes([0x00, 0x14])) == (1.0, "L/h")


def test_decode_vin_strips_count_prefix_and_padding() -> None:
    vin = "1N6BA06A08N123456"
    raw = b"\x01" + vin.encode("ascii")
    assert decode_vin(raw) == (vin, "text")


def test_decode_cvn_hex_groups() -> None:
    assert decode_cvn(bytes([0xAB, 0xCD, 0xEF, 0x01])) == ("ABCDEF01", "hex")


def test_new_decoders_reject_short_payloads() -> None:
    for decoder in (
        decode_wide_lambda,
        decode_minutes,
        decode_injection_timing,
        decode_fuel_rate,
    ):
        assert decoder(b"") is None
        assert decoder(b"\x01") is None
    assert decode_torque_percent(b"") is None
    assert decode_vin(b"") is None
    assert decode_cvn(b"") is None


# ---------------------------------------------------------------------------
# Mode 06 monitor parsing
# ---------------------------------------------------------------------------


def test_parse_mode06_response_pass_and_fail() -> None:
    # Two 9-byte records: one passing, one failing.
    rec_pass = bytes([0x01, 0x0B, 0x10, 0x00, 0x32, 0x00, 0x00, 0x00, 0x64])
    rec_fail = bytes([0xA1, 0x0C, 0x10, 0x00, 0xFF, 0x00, 0x00, 0x00, 0x10])
    results = parse_mode06_response(rec_pass + rec_fail)
    assert len(results) == 2
    assert results[0].monitor_id == 0x01
    assert results[0].test_value == 0x32
    assert results[0].passed is True
    assert results[1].monitor_id == 0xA1
    assert results[1].passed is False


def test_parse_mode06_ignores_trailing_padding() -> None:
    rec = bytes([0x01, 0x0B, 0x10, 0x00, 0x32, 0x00, 0x00, 0x00, 0x64])
    assert len(parse_mode06_response(rec + b"\x00\x00")) == 1
    assert parse_mode06_response(b"") == []


# ---------------------------------------------------------------------------
# lookup + coverage classification
# ---------------------------------------------------------------------------


def test_lookup_mode02_reuses_mode01_formula() -> None:
    m1 = lookup(0x01, 0x0C)
    m2 = lookup(0x02, 0x0C)
    assert m1 is not None and m2 is not None
    assert m2.mode == 0x02
    assert m2.decoder is m1.decoder
    assert m2.channel == "RPM"


def test_lookup_mode09_and_services() -> None:
    assert lookup(0x09, 0x02) is MODE09_REGISTRY[0x02]
    assert lookup(0x09, 0xFF) is None
    assert lookup(0x99, 0x01) is None
    assert SERVICE_REGISTRY[0x03].name == "STORED_DTC"
    assert SERVICE_REGISTRY[0x07].on_demand is True


def test_classify_pid_four_outcomes() -> None:
    decoded = classify_pid(0x01, 0x0C, supported=True)
    assert decoded.status == "decoded"
    assert decoded.channel == "RPM"

    undecoded = classify_pid(0x01, 0x41, supported=True)
    assert undecoded.status == "supported_not_decoded"
    assert undecoded.channel == "MONITOR_STATUS_DRIVE_CYCLE"

    unsupported = classify_pid(0x01, 0x0C, supported=False)
    assert unsupported.status == "unsupported"

    unknown = classify_pid(0x01, 0xFE, supported=True)
    assert unknown.status == "unknown_pid"
    assert unknown.channel is None
