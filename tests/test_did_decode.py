"""Tests for ``decode_did_rows`` + live-source DID decode.

Pins the missing-link fix: raw Mode-22 ``DID_<hex>`` poll readings (hex
string value) get decoded into named, scaled ``did:<NAME>`` numeric
columns. Without this the DID-dependent behavior recipes are dead code —
the column they target never exists.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from src.config import CanaryConfig
from src.decode.dids import (
    DID_SOURCE,
    DidAccess,
    DidEntry,
    DidMap,
    decode_did_rows,
)
from src.preprocessing.live import LiveFeatureSource
from src.telemetry import TelemetryRecord
from tests.configs import example_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _did_map() -> DidMap:
    return DidMap(
        schema_version=1,
        access=DidAccess(ecu_request_id=0x7E0, ecu_response_id=0x7E8),
        dids=[
            DidEntry(
                did=0x1225,
                name="ECM_AF_SENSOR_V_B1",
                byte_start=0,
                byte_length=2,
                byte_order="big",
                scale=0.005,
                offset=0.0,
                unit="V",
                confidence="high",
            ),
            DidEntry(
                did=0x1208,
                name="ECM_AIR_CHARGE",
                byte_start=0,
                byte_length=2,
                byte_order="big",
                scale=None,  # not pinned — exercises the raw-fallback path
                offset=None,
                unit="unknown",
                confidence="high",
            ),
        ],
    )


def _long_with_did(channel: str, payload_hex: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-05-21T00:00:00Z"], utc=True),
            "source": ["obd"],
            "channel": [channel],
            "value_number": [None],
            "value_text": [payload_hex],
            "value_bool": [None],
            "unit": [""],
            "payload": [None],
            "metadata_json": [None],
            "session": ["s"],
        }
    )


# --------------------------------------------------------------------------- #
# decode_did_rows (offline path)
# --------------------------------------------------------------------------- #


def test_decode_scaled_did() -> None:
    # 2.15 V → raw 430 (0x01AE) at scale 0.005.
    frame = _long_with_did("DID_1225", "01ae")
    out = decode_did_rows(frame, _did_map())
    did = out[out["source"] == DID_SOURCE]
    assert len(did) == 1
    assert did.iloc[0]["channel"] == "ECM_AF_SENSOR_V_B1"
    assert abs(did.iloc[0]["value_number"] - 2.15) < 1e-9
    assert did.iloc[0]["unit"] == "V"


def test_decode_unpinned_scale_falls_back_to_raw() -> None:
    """ECM_AIR_CHARGE has scale=None — emit the raw integer, unit='raw'."""
    frame = _long_with_did("DID_1208", "1362")  # 0x1362 = 4962
    out = decode_did_rows(frame, _did_map())
    did = out[out["source"] == DID_SOURCE]
    assert did.iloc[0]["channel"] == "ECM_AIR_CHARGE"
    assert did.iloc[0]["value_number"] == 4962.0
    assert did.iloc[0]["unit"] == "raw"


def test_a_legacy_kline_did_row_is_not_decoded_against_the_ecm_catalog() -> None:
    """K01.1: ``kline:DID_<hex>`` rows (pre-2026-09-24) name no module. The
    scan tool's ABS 0x1210 (yaw) is not the ECM's 0x1210, so decoding one
    against ``dids.yaml`` invents a reading. Offline, live and REST all refuse."""
    frame = _long_with_did("DID_1225", "01ae").assign(source="kline")
    out = decode_did_rows(frame, _did_map())
    assert (out["source"] == DID_SOURCE).sum() == 0
    source = LiveFeatureSource(config=_config(), did_map=_did_map())
    record = TelemetryRecord.from_epoch(0.0, source="kline", channel="DID_1225", value="01ae")
    assert source.decode_did_for_telemetry(record) is None


def test_unknown_did_hex_is_skipped() -> None:
    frame = _long_with_did("DID_9999", "0102")
    out = decode_did_rows(frame, _did_map())
    assert (out["source"] == DID_SOURCE).sum() == 0


def test_short_payload_is_skipped() -> None:
    """1 byte for a 2-byte DID → decode_did_raw returns None, row skipped."""
    frame = _long_with_did("DID_1225", "01")
    out = decode_did_rows(frame, _did_map())
    assert (out["source"] == DID_SOURCE).sum() == 0


def test_non_did_rows_pass_through_untouched() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-05-21T00:00:00Z"], utc=True),
            "source": ["obd"],
            "channel": ["RPM"],
            "value_number": [800.0],
            "value_text": [None],
            "value_bool": [None],
            "unit": ["rpm"],
            "payload": [None],
            "metadata_json": [None],
            "session": ["s"],
        }
    )
    out = decode_did_rows(frame, _did_map())
    assert len(out) == 1
    assert (out["source"] == DID_SOURCE).sum() == 0


def test_empty_frame_returns_unchanged() -> None:
    out = decode_did_rows(pd.DataFrame(), _did_map())
    assert out.empty


# --------------------------------------------------------------------------- #
# LiveFeatureSource DID decode (live path)
# --------------------------------------------------------------------------- #


def _config() -> CanaryConfig:
    return example_config(EXAMPLE_YAML)


def test_live_source_decodes_did_to_named_column() -> None:
    source = LiveFeatureSource(config=_config(), did_map=_did_map())
    now = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
    # Raw DID reading: obd source, channel DID_1225, hex value.
    source.observe(TelemetryRecord.obd(now.timestamp(), "DID_1225", "01ae", ""))
    row = source.tick(now)
    assert row is not None
    assert "did:ECM_AF_SENSOR_V_B1" in row.columns
    # float32 storage in the live frame → loosen tolerance.
    assert abs(float(row["did:ECM_AF_SENSOR_V_B1"].iloc[0]) - 2.15) < 1e-5


def test_live_source_ignores_did_when_no_map() -> None:
    """Without a DID map, a raw DID reading is just an undecodable obd value
    that ``_to_float`` drops — no crash, no column."""
    source = LiveFeatureSource(config=_config(), did_map=None)
    now = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
    source.observe(TelemetryRecord.obd(now.timestamp(), "DID_1225", "01ae", ""))
    source.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 800.0, "rpm"))
    row = source.tick(now)
    assert row is not None
    assert not any(c.startswith("did:") for c in row.columns)


def test_live_source_unpinned_scale_raw_fallback() -> None:
    source = LiveFeatureSource(config=_config(), did_map=_did_map())
    now = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
    source.observe(TelemetryRecord.obd(now.timestamp(), "DID_1208", "1362", ""))
    row = source.tick(now)
    assert row is not None
    assert float(row["did:ECM_AIR_CHARGE"].iloc[0]) == 4962.0


# --------------------------------------------------------------------------- #
# Live-stream publishing (dashboard path)
# --------------------------------------------------------------------------- #


def test_decode_did_for_telemetry_returns_engineering_units() -> None:
    source = LiveFeatureSource(config=_config(), did_map=_did_map())
    now = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
    decoded = source.decode_did_for_telemetry(
        TelemetryRecord.obd(now.timestamp(), "DID_1225", "01ae", "")
    )
    assert decoded is not None
    assert decoded.entry.name == "ECM_AF_SENSOR_V_B1"
    assert abs(decoded.value - 2.15) < 1e-9
    assert decoded.unit == "V"


def test_decode_did_for_telemetry_declines_non_did_records() -> None:
    """Only raw DID polls decode — ordinary PIDs and unknown ids pass."""
    source = LiveFeatureSource(config=_config(), did_map=_did_map())
    now = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
    assert (
        source.decode_did_for_telemetry(TelemetryRecord.obd(now.timestamp(), "RPM", 800.0, "rpm"))
        is None
    )
    assert (
        source.decode_did_for_telemetry(
            TelemetryRecord.obd(now.timestamp(), "DID_9999", "01ae", "")
        )
        is None
    )
    # Too short for the declared 2-byte range.
    assert (
        source.decode_did_for_telemetry(TelemetryRecord.obd(now.timestamp(), "DID_1225", "01", ""))
        is None
    )


async def test_broadcast_sink_publishes_decoded_did_rows() -> None:
    """The dashboard keys channels by ``<source>:<channel>``, so a raw
    ``obd:DID_1225`` poll is useless to it — the decoded ``did:<NAME>`` row
    published alongside is what renders a gauge."""
    from src.daemon.drain import run_telemetry_broadcast_sink
    from src.dashboard.broadcast import Broadcaster

    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    bus = Broadcaster()
    sub = bus.subscribe()
    source = LiveFeatureSource(config=_config(), did_map=_did_map())

    await queue.put(TelemetryRecord.obd(1_778_000_000.0, "DID_1225", "01ae", ""))
    task = asyncio.create_task(
        run_telemetry_broadcast_sink(queue, bus, stop, live_source=source, poll_interval=0.01)
    )
    await asyncio.sleep(0.1)
    stop.set()
    await task

    published = []
    while not sub.queue.empty():
        published.append(sub.queue.get_nowait())

    # The raw poll still goes out unchanged — storage and the RE tooling
    # both speak it, and dropping it would be a silent format change.
    raw = [r for r in published if r["channel"] == "DID_1225"]
    assert raw and raw[0]["source"] == "obd" and raw[0]["value"] == "01ae"

    decoded = [r for r in published if r["source"] == DID_SOURCE]
    assert len(decoded) == 1
    assert decoded[0]["channel"] == "ECM_AF_SENSOR_V_B1"
    assert abs(decoded[0]["value"] - 2.15) < 1e-9
    assert decoded[0]["unit"] == "V"
    assert decoded[0]["timestamp"] == raw[0]["timestamp"]


async def test_broadcast_sink_without_did_map_publishes_raw_only() -> None:
    from src.daemon.drain import run_telemetry_broadcast_sink
    from src.dashboard.broadcast import Broadcaster

    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    bus = Broadcaster()
    sub = bus.subscribe()
    source = LiveFeatureSource(config=_config(), did_map=None)

    await queue.put(TelemetryRecord.obd(1_778_000_000.0, "DID_1225", "01ae", ""))
    task = asyncio.create_task(
        run_telemetry_broadcast_sink(queue, bus, stop, live_source=source, poll_interval=0.01)
    )
    await asyncio.sleep(0.1)
    stop.set()
    await task

    published = []
    while not sub.queue.empty():
        published.append(sub.queue.get_nowait())
    assert len(published) == 1
    assert published[0]["source"] == "obd"


async def test_broadcast_sink_skips_decode_without_subscribers() -> None:
    """A closed dashboard must add no decode work to the collection path."""
    from src.daemon.drain import run_telemetry_broadcast_sink
    from src.dashboard.broadcast import Broadcaster

    class _DecodeSpy:
        def decode_for_telemetry(self, _record: TelemetryRecord) -> list[TelemetryRecord]:
            raise AssertionError("CAN decode ran with no dashboard subscriber")

        def decode_did_for_telemetry(self, _record: TelemetryRecord) -> None:
            raise AssertionError("DID decode ran with no dashboard subscriber")

    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    await queue.put(TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm"))
    stop.set()

    await run_telemetry_broadcast_sink(
        queue,
        Broadcaster(),
        stop,
        live_source=_DecodeSpy(),  # type: ignore[arg-type]
        poll_interval=0.01,
    )
    assert queue.empty()
