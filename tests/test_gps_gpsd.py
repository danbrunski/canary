"""Tests for the gpsd GPS backend.

The load-bearing property here is *parity* with the serial NMEA path: the same
drive should produce the same ``gps:*`` channels regardless of which backend is
configured. Several tests below pin the mappings where gpsd's representation
differs from NMEA's and a direct copy would silently change behavior.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.collection.gps_gpsd import (
    GpsdReader,
    GpsdSpec,
    collect_gpsd,
    collect_gpsd_from_config,
    parse_gpsd_line,
    update_reading_gpsd,
)
from src.collection.gps_nmea import GpsReading
from src.collection.reconnect import ReconnectPolicy
from src.telemetry import TelemetryRecord


def _tpv(**fields: object) -> dict[str, object]:
    return {"class": "TPV", "device": "/dev/ttyUSB0", **fields}


def _sky(**fields: object) -> dict[str, object]:
    return {"class": "SKY", "device": "/dev/ttyUSB0", **fields}


class _FakeReader:
    """Serves canned lines, then blocks-as-idle (empty reads) like a quiet gpsd."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = [line.encode() for line in lines]
        self.closed = False

    def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------
# Field mapping
# --------------------------------------------------------------------------


def test_tpv_maps_position_speed_and_heading() -> None:
    reading = GpsReading()
    update_reading_gpsd(
        reading,
        _tpv(mode=3, lat=39.9, lon=-105.2, altMSL=1600.0, speed=22.2222, track=90.0),
    )
    assert reading.has_fix is True
    assert reading.latitude == pytest.approx(39.9)
    assert reading.longitude == pytest.approx(-105.2)
    assert reading.altitude_m == pytest.approx(1600.0)
    assert reading.heading_deg == pytest.approx(90.0)
    # gpsd reports m/s; gps:speed is km/h so it can be compared with obd:SPEED.
    assert reading.speed_kmh == pytest.approx(80.0, rel=1e-3)


def test_tpv_no_fix_mode_maps_to_zero_fix_quality() -> None:
    """gpsd mode=1 means *no fix* — it must not read as a fix.

    ``gps_tracker`` derives ``has_fix`` from ``fix_quality > 0`` (the GGA
    convention). Passing gpsd's ``mode`` through unchanged would make an
    unfixed receiver display a fix on the dashboard.
    """
    for mode in (0, 1):
        reading = GpsReading()
        update_reading_gpsd(reading, _tpv(mode=mode))
        assert reading.has_fix is False
        assert reading.fix_quality == 0


def test_tpv_2d_and_3d_modes_map_to_positive_fix_quality() -> None:
    for mode in (2, 3):
        reading = GpsReading()
        update_reading_gpsd(reading, _tpv(mode=mode))
        assert reading.has_fix is True
        assert reading.fix_quality is not None and reading.fix_quality > 0


def test_tpv_falls_back_to_legacy_alt_field() -> None:
    """gpsd < 3.20 emits ``alt`` rather than ``altMSL``."""
    reading = GpsReading()
    update_reading_gpsd(reading, _tpv(mode=3, alt=1500.0))
    assert reading.altitude_m == pytest.approx(1500.0)


def test_tpv_prefers_msl_altitude_over_legacy_alt() -> None:
    # GGA reports MSL, so the serial path's altitude is MSL — match it.
    reading = GpsReading()
    update_reading_gpsd(reading, _tpv(mode=3, alt=1620.0, altMSL=1600.0))
    assert reading.altitude_m == pytest.approx(1600.0)


def test_sky_uses_satellites_used_not_seen() -> None:
    """``num_satellites`` must mean *used in fix*, matching GGA.

    The committee gate and speed-trust check are tuned against used-count; a
    seen-count (``nSat``) runs higher on a marginal fix and would re-open the
    parked-truck speed-jitter leak those gates close.
    """
    reading = GpsReading()
    update_reading_gpsd(reading, _sky(nSat=14, uSat=6, hdop=1.4))
    assert reading.num_satellites == 6
    assert reading.hdop == pytest.approx(1.4)


def test_sky_counts_used_flags_when_usat_absent() -> None:
    reading = GpsReading()
    update_reading_gpsd(
        reading,
        _sky(satellites=[{"used": True}, {"used": True}, {"used": False}]),
    )
    assert reading.num_satellites == 2


def test_missing_fields_become_none_not_zero() -> None:
    """A TPV without velocity must not assert a bogus 0 km/h 'stopped'."""
    reading = GpsReading()
    update_reading_gpsd(reading, _tpv(mode=3, lat=39.9, lon=-105.2))
    assert reading.speed_kmh is None
    assert reading.heading_deg is None
    assert reading.altitude_m is None


# --------------------------------------------------------------------------
# Stream parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    ["", "   ", "not json", '{"no_class": 1}', "[1,2,3]", '{"class": 7}'],
)
def test_parse_gpsd_line_rejects_junk(line: str) -> None:
    assert parse_gpsd_line(line) is None


def test_parse_gpsd_line_accepts_a_report() -> None:
    assert parse_gpsd_line('{"class":"TPV","mode":3}\n') == {"class": "TPV", "mode": 3}


# --------------------------------------------------------------------------
# Read loop
# --------------------------------------------------------------------------


async def _drain(reader: _FakeReader) -> list[TelemetryRecord]:
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()

    async def run() -> None:
        await collect_gpsd(reader, queue, stop, now_fn=lambda: 1000.0)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    records: list[TelemetryRecord] = []
    while not queue.empty():
        records.append(queue.get_nowait())
    return records


@pytest.mark.asyncio
async def test_collect_gpsd_flushes_one_record_set_per_tpv() -> None:
    reader = _FakeReader(
        [
            json.dumps(_sky(uSat=9, hdop=0.9)) + "\n",
            json.dumps(_tpv(mode=3, lat=39.9, lon=-105.2, speed=22.2222, track=90.0)) + "\n",
        ]
    )
    records = await _drain(reader)
    channels = {record.channel for record in records}
    assert channels == {
        "fix_quality",
        "num_satellites",
        "hdop",
        "speed",
        "heading",
        "latitude",
        "longitude",
    }
    # One TPV → one fix epoch: every record shares a single timestamp.
    assert len({record.timestamp for record in records}) == 1


@pytest.mark.asyncio
async def test_fix_health_persists_across_tpv_epochs() -> None:
    """SKY arrives less often than TPV; sats/hdop must not blank in between.

    They gate whether ``gps:speed`` is emitted at all, so dropping them on a
    TPV-only epoch would intermittently withhold speed while driving.
    """
    reader = _FakeReader(
        [
            json.dumps(_sky(uSat=9, hdop=0.9)) + "\n",
            json.dumps(_tpv(mode=3, lat=39.9, lon=-105.2, speed=20.0)) + "\n",
            json.dumps(_tpv(mode=3, lat=39.91, lon=-105.21, speed=21.0)) + "\n",
        ]
    )
    records = await _drain(reader)
    second_epoch = [r for r in records if r.channel == "num_satellites"]
    assert len(second_epoch) == 2, "both epochs should carry satellite health"
    assert all(record.value == 9 for record in second_epoch)
    # And speed survived the gate on both epochs.
    assert len([r for r in records if r.channel == "speed"]) == 2


@pytest.mark.asyncio
async def test_marginal_fix_withholds_speed_same_as_serial_path() -> None:
    """Parity check on the shared speed-trust gate (5 sats @ hdop 2.5)."""
    reader = _FakeReader(
        [
            json.dumps(_sky(uSat=5, hdop=2.5)) + "\n",
            json.dumps(_tpv(mode=3, lat=39.9, lon=-105.2, speed=2.5)) + "\n",
        ]
    )
    records = await _drain(reader)
    channels = {record.channel for record in records}
    assert "speed" not in channels
    assert {"num_satellites", "hdop", "latitude"} <= channels


@pytest.mark.asyncio
async def test_collect_gpsd_ignores_non_report_traffic() -> None:
    reader = _FakeReader(
        [
            '{"class":"VERSION","release":"3.22"}\n',
            "garbage\n",
            json.dumps(_tpv(mode=1)) + "\n",
        ]
    )
    records = await _drain(reader)
    # mode=1 is no-fix: health only, no position/speed.
    assert {record.channel for record in records} == {"fix_quality"}
    assert records[0].value == 0


# --------------------------------------------------------------------------
# Socket reader + reconnect
# --------------------------------------------------------------------------


class _FakeSocket:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    def recv(self, _size: int) -> bytes:
        if not self._chunks:
            raise TimeoutError
        chunk = self._chunks.pop(0)
        if chunk is None:  # pragma: no cover — defensive
            raise TimeoutError
        return chunk

    def close(self) -> None:
        self.closed = True


def test_reader_reassembles_lines_split_across_recv() -> None:
    reader = GpsdReader(_FakeSocket([b'{"class":"T', b'PV"}\n{"class":"SKY"}\n']))  # type: ignore[arg-type]
    assert reader.readline() == b'{"class":"TPV"}\n'
    assert reader.readline() == b'{"class":"SKY"}\n'


def test_reader_preserves_partial_line_across_timeout() -> None:
    """A read timeout mid-line must not corrupt the next line.

    The timeout is what lets the collector notice a stop request between
    fixes, so it fires routinely — losing the partial buffer would drop a
    fix roughly every time the receiver went quiet.
    """
    sock = _FakeSocket([b'{"class":"TP'])
    reader = GpsdReader(sock)  # type: ignore[arg-type]
    assert reader.readline() == b""  # timed out mid-line
    sock._chunks.append(b'V","mode":3}\n')
    assert reader.readline() == b'{"class":"TPV","mode":3}\n'


def test_reader_raises_when_gpsd_closes() -> None:
    reader = GpsdReader(_FakeSocket([b""]))  # type: ignore[arg-type]
    with pytest.raises(ConnectionError):
        reader.readline()


@pytest.mark.asyncio
async def test_collect_gpsd_from_config_survives_gpsd_being_down() -> None:
    """gpsd may not be up yet when canary starts; keep retrying, don't crash."""
    attempts = 0

    def boom(spec: GpsdSpec) -> _FakeReader:
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError("gpsd not listening")

    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(
        collect_gpsd_from_config(
            GpsdSpec(),
            queue,
            stop,
            factory=boom,
            reconnect_policy=ReconnectPolicy(initial_delay_seconds=0.01, max_delay_seconds=0.01),
        )
    )
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert attempts > 1


@pytest.mark.asyncio
async def test_collect_gpsd_from_config_closes_reader_on_stop() -> None:
    reader = _FakeReader([])
    stop = asyncio.Event()
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    task = asyncio.create_task(
        collect_gpsd_from_config(GpsdSpec(), queue, stop, factory=lambda spec: reader)
    )
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert reader.closed is True
