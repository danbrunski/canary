"""GPX/KML export of a captured session's GPS track (Task 5.6a,
engine-analysis-and-mapping.md).

The GPS collector already logs ``gps:latitude``/``gps:longitude``/
``gps:altitude`` (:mod:`src.collection.gps_collector`) and one full fix's
channels share an exact timestamp (all built from the same
``GpsReading`` in :func:`~src.collection.gps_collector.reading_to_records`),
so — unlike the cross-source alignment Tasks 5.1/5.3 need — an exact-match
pivot is the *correct* join here, not an approximation. GPX and KML are both
small, well-specified XML formats; hand-writing them avoids a new dependency
for what's really just a template with escaped text nodes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import pandas as pd

from src.preprocessing.loader import read_session
from src.telemetry import GPS_SOURCE


@dataclass(frozen=True)
class TrackPoint:
    timestamp: pd.Timestamp
    latitude: float
    longitude: float
    altitude: float | None


def read_gps_track(session_dir: Path) -> list[TrackPoint]:
    """The session's captured GPS fixes, oldest-first.

    Rows missing a latitude or longitude are dropped (a fix mid-acquisition
    reports altitude/speed before position resolves) — a track point without
    coordinates isn't a track point. Altitude is carried when present,
    ``None`` otherwise (never fabricated as ``0``).
    """
    # Only the GPS rows are read (P02.2): the rest is ~100k CAN frames a minute.
    # And only the columns the pivot uses: ``value`` is built from the three.
    gps = read_session(
        session_dir,
        filters=[("source", "==", GPS_SOURCE)],
        columns=["value_number", "value_text", "value_bool"],
    )
    if gps.empty:
        return []
    pivoted = gps.pivot_table(
        index="timestamp",
        columns="channel",
        values="value",
        aggfunc="last",
    ).sort_index()
    if "latitude" not in pivoted.columns or "longitude" not in pivoted.columns:
        return []
    lat = pd.to_numeric(pivoted["latitude"], errors="coerce")
    lon = pd.to_numeric(pivoted["longitude"], errors="coerce")
    alt = (
        pd.to_numeric(pivoted["altitude"], errors="coerce")
        if "altitude" in pivoted.columns
        else pd.Series(index=pivoted.index, dtype=float)
    )
    points: list[TrackPoint] = []
    for ts, la, lo, al in zip(pivoted.index, lat, lon, alt, strict=True):
        if pd.isna(la) or pd.isna(lo):
            continue
        points.append(
            TrackPoint(
                timestamp=pd.Timestamp(ts),
                latitude=float(la),
                longitude=float(lo),
                altitude=None if pd.isna(al) else float(al),
            )
        )
    return points


def to_gpx(track: list[TrackPoint], *, name: str) -> str:
    """Minimal single-track-segment GPX 1.1 document."""
    points = "\n".join(
        f'      <trkpt lat="{p.latitude:.7f}" lon="{p.longitude:.7f}">\n'
        + (f"        <ele>{p.altitude:.2f}</ele>\n" if p.altitude is not None else "")
        + f"        <time>{p.timestamp.isoformat()}</time>\n"
        + "      </trkpt>"
        for p in track
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="CANary" xmlns="http://www.topografix.com/GPX/1/1">\n'
        f"  <trk>\n    <name>{escape(name)}</name>\n    <trkseg>\n{points}\n"
        "    </trkseg>\n  </trk>\n</gpx>\n"
    )


def to_kml(track: list[TrackPoint], *, name: str) -> str:
    """Minimal single-``LineString`` KML document (Google Earth/Maps)."""
    # KML coordinate order is lon,lat[,alt] — the opposite of GPX's lat/lon
    # attribute order, per the OGC spec; easy to transpose by accident.
    coords = " ".join(
        f"{p.longitude:.7f},{p.latitude:.7f},{p.altitude:.2f}"
        if p.altitude is not None
        else f"{p.longitude:.7f},{p.latitude:.7f},0"
        for p in track
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        f"  <Document>\n    <name>{escape(name)}</name>\n    <Placemark>\n"
        f"      <name>{escape(name)}</name>\n      <LineString>\n"
        "        <altitudeMode>clampToGround</altitudeMode>\n"
        f"        <coordinates>{coords}</coordinates>\n"
        "      </LineString>\n    </Placemark>\n  </Document>\n</kml>\n"
    )


__all__ = ["TrackPoint", "read_gps_track", "to_gpx", "to_kml"]
