"""Minimal NMEA 0183 parser for the serial GPS collector.

Pure functions only — no I/O — so the sentence handling is exhaustively unit
testable without hardware. We decode just the three sentences that carry what
operating-mode detection and the dashboard need:

* **RMC** (recommended minimum): fix validity, speed-over-ground, course, and
  position. Emitted once per fix epoch, so the collector flushes a reading when
  it sees one.
* **GGA** (fix data): fix quality, satellites-in-use, HDOP, altitude.
* **VTG** (track/ground speed): speed in km/h directly (a redundant cross-check
  for RMC's knots).

Multi-constellation receivers prefix sentences with the talker that produced
them (``GP`` GPS, ``GL`` GLONASS, ``GN`` combined); we key off the last three
characters of the address field so every talker is handled identically.

``gps:speed`` is reported in **km/h** to match ``obd:SPEED`` so the two coalesce
in the operating-mode segmenter.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Knots → km/h. 1 nautical mile = 1.852 km exactly.
_KNOTS_TO_KMH = 1.852


@dataclass
class GpsReading:
    """Accumulated state for one fix epoch, merged across RMC/GGA/VTG."""

    has_fix: bool = False
    speed_kmh: float | None = None
    heading_deg: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None
    fix_quality: int | None = None
    num_satellites: int | None = None
    hdop: float | None = None


def nmea_checksum_ok(sentence: str) -> bool:
    """Validate the ``*HH`` XOR checksum of an NMEA sentence.

    Returns ``False`` for any sentence missing the ``$`` start, the ``*``
    delimiter, or a well-formed two-hex-digit checksum that matches the XOR of
    every character between them.
    """
    if not sentence.startswith("$") or "*" not in sentence:
        return False
    body, _, checksum = sentence[1:].partition("*")
    checksum = checksum.strip()
    if len(checksum) < 2:
        return False
    try:
        expected = int(checksum[:2], 16)
    except ValueError:
        return False
    actual = 0
    for char in body:
        actual ^= ord(char)
    return actual == expected


def sentence_type(sentence: str) -> str | None:
    """Return the 3-char sentence type (``RMC``/``GGA``/``VTG``/...) or None."""
    if not sentence.startswith("$"):
        return None
    address = sentence[1:].split(",", 1)[0]
    if len(address) < 3:
        return None
    return address[-3:].upper()


def update_reading(reading: GpsReading, sentence: str) -> GpsReading:
    """Apply one NMEA sentence to ``reading`` in place; return it.

    Sentences that fail the checksum or aren't one of RMC/GGA/VTG are ignored
    (the reading is returned unchanged), so a noisy line never corrupts state.
    """
    if not nmea_checksum_ok(sentence):
        return reading
    body = sentence[1:].partition("*")[0]
    fields = body.split(",")
    kind = fields[0][-3:].upper() if fields and len(fields[0]) >= 3 else ""
    args = fields[1:]
    if kind == "RMC":
        _apply_rmc(reading, args)
    elif kind == "GGA":
        _apply_gga(reading, args)
    elif kind == "VTG":
        _apply_vtg(reading, args)
    return reading


def _apply_rmc(reading: GpsReading, f: list[str]) -> None:
    # f: time, status, lat, N/S, lon, E/W, sog_knots, course, date, ...
    if len(f) < 8:
        return
    if f[1] == "A":
        reading.has_fix = True
    lat = _parse_latlon(_get(f, 2), _get(f, 3))
    lon = _parse_latlon(_get(f, 4), _get(f, 5))
    if lat is not None:
        reading.latitude = lat
    if lon is not None:
        reading.longitude = lon
    knots = _to_float(_get(f, 6))
    if knots is not None:
        reading.speed_kmh = knots * _KNOTS_TO_KMH
    course = _to_float(_get(f, 7))
    if course is not None:
        reading.heading_deg = course


def _apply_gga(reading: GpsReading, f: list[str]) -> None:
    # f: time, lat, N/S, lon, E/W, quality, num_sats, hdop, alt, alt_unit, ...
    if len(f) < 9:
        return
    quality = _to_int(_get(f, 5))
    if quality is not None:
        reading.fix_quality = quality
        if quality > 0:
            reading.has_fix = True
    sats = _to_int(_get(f, 6))
    if sats is not None:
        reading.num_satellites = sats
    hdop = _to_float(_get(f, 7))
    if hdop is not None:
        reading.hdop = hdop
    lat = _parse_latlon(_get(f, 1), _get(f, 2))
    lon = _parse_latlon(_get(f, 3), _get(f, 4))
    if lat is not None:
        reading.latitude = lat
    if lon is not None:
        reading.longitude = lon
    alt = _to_float(_get(f, 8))
    if alt is not None:
        reading.altitude_m = alt


def _apply_vtg(reading: GpsReading, f: list[str]) -> None:
    # f: course_true, T, course_mag, M, sog_knots, N, sog_kmh, K, ...
    if len(f) < 7:
        return
    kmh = _to_float(_get(f, 6))
    if kmh is not None:
        reading.speed_kmh = kmh
    course = _to_float(_get(f, 0))
    if course is not None:
        reading.heading_deg = course


def _parse_latlon(value: str, hemisphere: str) -> float | None:
    """Convert NMEA ``ddmm.mmmm`` + hemisphere into signed decimal degrees."""
    raw = _to_float(value)
    if raw is None or not hemisphere:
        return None
    degrees = int(raw // 100)
    minutes = raw - degrees * 100
    decimal = degrees + minutes / 60.0
    if hemisphere.upper() in ("S", "W"):
        decimal = -decimal
    return decimal


def _get(fields: list[str], index: int) -> str:
    return fields[index] if 0 <= index < len(fields) else ""


def _to_float(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


__all__ = [
    "GpsReading",
    "nmea_checksum_ok",
    "sentence_type",
    "update_reading",
]
