"""Tests for the typed SAE J1979 registry (Task 6.1).

Every decoded PID is exercised against a canned raw response so the
formula table can't drift; coverage classification is checked against the
four honest outcomes (decoded / supported-not-decoded / unsupported /
unknown).
"""

from __future__ import annotations

import pytest

from src.collection.can_obd_client import PID_DECODERS
from src.collection.obd_registry import (
    MODE01_REGISTRY,
    MODE09_REGISTRY,
    SERVICE_REGISTRY,
    assert_pid_tables_consistent,
    classify_pid,
    decode_cvn,
    decode_fuel_rate,
    decode_injection_timing,
    decode_minutes,
    decode_torque_percent,
    decode_vin,
    decode_wide_lambda,
    lookup,
    misfire_counts_from_results,
    misfire_cylinder_number,
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


def test_assert_pid_tables_consistent_passes_on_real_tables() -> None:
    """The startup assertion (Task 1.1) is a no-op against the real tables."""
    assert_pid_tables_consistent()


def test_assert_pid_tables_consistent_catches_name_drift() -> None:
    """A deliberately mismatched channel name must be caught, not silently
    accepted."""
    broken = dict(PID_DECODERS)
    pid, (_name, decoder) = next(iter(broken.items()))
    broken[pid] = ("SOME_OTHER_CHANNEL_NAME", decoder)
    with pytest.raises(ValueError, match="channel-name drift"):
        assert_pid_tables_consistent(pid_decoders=broken)


def test_assert_pid_tables_consistent_catches_decoder_drift() -> None:
    """A deliberately mismatched decoder function must be caught."""
    pid, (name, _decoder) = next(iter(PID_DECODERS.items()))
    broken = dict(PID_DECODERS)
    broken[pid] = (name, lambda data: None)
    with pytest.raises(ValueError, match="decoder drift"):
        assert_pid_tables_consistent(pid_decoders=broken)


def test_assert_pid_tables_consistent_catches_missing_registry_entry() -> None:
    """A PID_DECODERS entry with no matching MODE01_REGISTRY row must be
    caught, not silently skipped."""
    stripped = dict(MODE01_REGISTRY)
    pid = next(iter(PID_DECODERS))
    del stripped[pid]
    with pytest.raises(ValueError, match="MODE01_REGISTRY does not"):
        assert_pid_tables_consistent(mode01_registry=stripped)


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


def test_wideband_lambda_pids_polled_at_fast_tier() -> None:
    """Task 2.1: 0x24/0x25/0x34/0x35 are now polled (via PID_DECODERS) at the
    fast tier, and decode via the shared ``decode_wide_lambda`` formula."""
    for pid in (0x24, 0x25, 0x34, 0x35):
        assert pid in PID_DECODERS, f"PID 0x{pid:02X} should be in PID_DECODERS"
        entry = MODE01_REGISTRY[pid]
        assert entry.poll_tier == "fast"
        assert entry.decoder is decode_wide_lambda
        # AB=0xA000 → 40960/32768 = 1.25 λ (lean); CD (voltage/current) ignored.
        assert entry.decoder(bytes([0xA0, 0x00, 0x00, 0x00])) == pytest.approx((1.25, "ratio"))
    # The remaining 0x26-0x2B / 0x36-0x3B wideband PIDs stay medium-tier
    # (documented but not actively polled — Task 2.2 probes the rest).
    for pid in (0x26, 0x27, 0x28, 0x29, 0x2A, 0x2B, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x3B):
        assert pid not in PID_DECODERS
        assert MODE01_REGISTRY[pid].poll_tier == "medium"


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
# Task 3.1 — per-cylinder misfire counts
# ---------------------------------------------------------------------------


def test_misfire_cylinder_number_maps_reserved_mid_range() -> None:
    assert misfire_cylinder_number(0xA2) == 1
    assert misfire_cylinder_number(0xAC) == 11
    # Composite/engine-wide misfire data (MID 0xA1) and any other monitor
    # (catalyst, EVAP, ...) are not per-cylinder.
    assert misfire_cylinder_number(0x01) is None
    assert misfire_cylinder_number(0xA1) is None
    assert misfire_cylinder_number(0xAD) is None


def test_misfire_counts_from_results_filters_to_cylinder_mids() -> None:
    cyl1 = bytes([0xA2, 0x0C, 0x24, 0x00, 0x03, 0x00, 0x00, 0x00, 0x00])
    cyl2 = bytes([0xA3, 0x0C, 0x24, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    ewma = bytes([0xA2, 0x0B, 0x24, 0x00, 0x09, 0x00, 0x00, 0x00, 0x00])
    composite = bytes([0x01, 0x01, 0x10, 0x00, 0x07, 0x00, 0x00, 0x00, 0x00])
    results = parse_mode06_response(cyl1 + cyl2 + ewma + composite)
    counts = misfire_counts_from_results(results)
    assert counts == {1: 3, 2: 0}


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


def test_decode_cvn_strips_leading_message_count_byte() -> None:
    """Regression: the Xterra returned `01 0B4B7C60` (one CVN) on 2026-07-26 and
    this decoded as "010B4B7C" — a leading 01 invented, the trailing 60 dropped.
    A CVN exists to be compared exactly, so a wrong-but-plausible value is worse
    than no value."""
    assert decode_cvn(bytes.fromhex("010b4b7c60")) == ("0B4B7C60", "hex")


def test_decode_cvn_multiple_cvns_with_count_byte() -> None:
    assert decode_cvn(bytes.fromhex("020b4b7c60aabbccdd")) == ("0B4B7C60 AABBCCDD", "hex")


def test_decode_cvn_bare_four_bytes_unchanged() -> None:
    """No count byte (len % 4 == 0) must decode exactly as before the fix."""
    assert decode_cvn(bytes([0xAB, 0xCD, 0xEF, 0x01])) == ("ABCDEF01", "hex")
