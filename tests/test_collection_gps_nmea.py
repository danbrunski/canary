"""Tests for the NMEA 0183 parser used by the GPS collector."""

from __future__ import annotations

import pytest

from src.collection.gps_nmea import (
    GpsReading,
    nmea_checksum_ok,
    sentence_type,
    update_reading,
)


def _nmea(body: str) -> str:
    """Wrap an NMEA body (between ``$`` and ``*``) with a valid XOR checksum."""
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"${body}*{checksum:02X}"


# A captured real fix-less sentence (valid checksum from the device).
_REAL_NOFIX_RMC = "$GNRMC,181042.095,V,,,,,0.00,0.00,240526,,,N*56"


def test_checksum_accepts_real_device_sentence() -> None:
    assert nmea_checksum_ok(_REAL_NOFIX_RMC)


def test_checksum_rejects_tampered_payload() -> None:
    tampered = _REAL_NOFIX_RMC.replace("181042", "181043")
    assert not nmea_checksum_ok(tampered)


@pytest.mark.parametrize(
    "bad",
    [
        "GNRMC,181042.095,V*56",  # no leading $
        "$GNRMC,181042.095,V",  # no checksum delimiter
        "$GNRMC,foo*ZZ",  # non-hex checksum
        "$GNRMC,foo*5",  # truncated checksum
    ],
)
def test_checksum_rejects_malformed(bad: str) -> None:
    assert not nmea_checksum_ok(bad)


@pytest.mark.parametrize(
    "sentence,expected",
    [
        ("$GNRMC,...", "RMC"),
        ("$GPGGA,...", "GGA"),
        ("$GLGSV,...", "GSV"),
        ("not-nmea", None),
    ],
)
def test_sentence_type(sentence: str, expected: str | None) -> None:
    assert sentence_type(sentence) == expected


def test_rmc_with_fix_sets_speed_heading_and_position() -> None:
    sentence = _nmea("GNRMC,181042.00,A,3953.0000,N,10515.0000,W,54.00,123.4,240526,,,A")
    reading = update_reading(GpsReading(), sentence)
    assert reading.has_fix is True
    # 54 knots × 1.852 = 100.008 km/h
    assert reading.speed_kmh == pytest.approx(100.008, abs=1e-3)
    assert reading.heading_deg == pytest.approx(123.4)
    assert reading.latitude == pytest.approx(39.8833, abs=1e-3)
    assert reading.longitude == pytest.approx(-105.25, abs=1e-3)


def test_rmc_void_status_leaves_no_fix() -> None:
    reading = update_reading(GpsReading(), _REAL_NOFIX_RMC)
    assert reading.has_fix is False


def test_gga_populates_fix_quality_satellites_hdop_altitude() -> None:
    sentence = _nmea("GNGGA,181043.00,3953.0000,N,10515.0000,W,1,07,1.2,1600.5,M,,M,,")
    reading = update_reading(GpsReading(), sentence)
    assert reading.fix_quality == 1
    assert reading.has_fix is True
    assert reading.num_satellites == 7
    assert reading.hdop == pytest.approx(1.2)
    assert reading.altitude_m == pytest.approx(1600.5)


def test_gga_quality_zero_is_no_fix() -> None:
    sentence = _nmea("GNGGA,181043.00,,,,,0,00,,,M,,M,,")
    reading = update_reading(GpsReading(), sentence)
    assert reading.fix_quality == 0
    assert reading.num_satellites == 0
    assert reading.has_fix is False


def test_vtg_sets_speed_in_kmh_directly() -> None:
    sentence = _nmea("GNVTG,123.4,T,,M,54.00,N,100.0,K,A")
    reading = update_reading(GpsReading(), sentence)
    assert reading.speed_kmh == pytest.approx(100.0)
    assert reading.heading_deg == pytest.approx(123.4)


def test_update_reading_merges_gga_then_rmc() -> None:
    reading = GpsReading()
    reading = update_reading(
        reading, _nmea("GNGGA,181043.00,3953.0000,N,10515.0000,W,1,07,1.2,1600.5,M,,M,,")
    )
    reading = update_reading(
        reading, _nmea("GNRMC,181043.00,A,3953.0000,N,10515.0000,W,54.00,123.4,240526,,,A")
    )
    # GGA-only fields survive the later RMC merge.
    assert reading.num_satellites == 7
    assert reading.altitude_m == pytest.approx(1600.5)
    assert reading.speed_kmh == pytest.approx(100.008, abs=1e-3)


def test_southern_western_hemispheres_are_negative() -> None:
    sentence = _nmea("GNRMC,181042.00,A,3953.0000,S,10515.0000,E,0,0,240526,,,A")
    reading = update_reading(GpsReading(), sentence)
    assert reading.latitude == pytest.approx(-39.8833, abs=1e-3)
    assert reading.longitude == pytest.approx(105.25, abs=1e-3)


def test_bad_checksum_sentence_is_ignored() -> None:
    reading = GpsReading()
    before = (reading.has_fix, reading.speed_kmh)
    reading = update_reading(reading, "$GNRMC,181042.00,A,3953.0,N,10515.0,W,54.0,1,2,,,A*00")
    assert (reading.has_fix, reading.speed_kmh) == before
