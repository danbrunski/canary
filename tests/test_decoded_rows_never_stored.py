"""P02.8: decoded rows are never stored, and every stored-data reader decodes
them on read (operator decision 2026-09-26).

The storage half is made impossible rather than documented: the storage
sink's subscription refuses a ``decoded`` record whatever a config key or a
capture profile says, and the daemon's Parquet writer refuses one outright.

The reader half runs each reader over a real pre-P02.8 slice
(``tests/fixtures/replay/decoded-slice``: 20 s of ``2026-09-10T142711`` whose
stored ``decoded`` rows are exactly its kept frames' decode), over a copy
with those rows removed (a session recorded after P02.8), and over a mixed
copy (one chunk each way: the corpus from now on). Each reader must return
the decoded channels from every copy, identically.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
import yaml

from src.telemetry import TelemetryRecord
from tests.configs import example_config

REPO = Path(__file__).resolve().parents[1]
SLICE = REPO / "tests" / "fixtures" / "replay" / "decoded-slice" / "telemetry-00000.parquet"
#: The fixture's source session, so every copy reads under the same name.
NAME = "2026-09-10T142711"
ON_READ = [
    "decoded:ECM_ACCEL_PEDAL_POS_V",
    "decoded:ECM_COOLANT_TEMP",
    "decoded:ECM_RPM",
    "decoded:ECM_THROTTLE_SENSOR_V",
    "decoded:WHEEL_SPEED_6F7_A",
    "decoded:WHEEL_SPEED_6F7_B",
    "decoded:WHEEL_SPEED_6F7_C",
]


def _write(root: Path, tables: list[pa.Table]) -> Path:
    session = root / NAME
    session.mkdir(parents=True)
    for index, table in enumerate(tables):
        pq.write_table(table, session / f"telemetry-{index:05d}.parquet")
    return session


def _not_decoded(table: pa.Table) -> pa.Table:
    return table.filter(pc.not_equal(table["source"], "decoded"))


@pytest.fixture
def copies(tmp_path: Path) -> dict[str, Path]:
    """``stored`` (as recorded before P02.8), ``stripped`` (no decoded rows) and
    ``mixed`` (a stored chunk, then a stripped one), one storage root each."""
    table = pq.read_table(SLICE)
    assert pc.sum(pc.equal(table["source"], "decoded")).as_py() > 0
    stamps = table["timestamp"]
    middle = pc.min(stamps).as_py() + (pc.max(stamps).as_py() - pc.min(stamps).as_py()) / 2
    early = table.filter(pc.less(stamps, pa.scalar(middle, stamps.type)))
    late = table.filter(pc.greater_equal(stamps, pa.scalar(middle, stamps.type)))
    return {
        "stored": _write(tmp_path / "stored", [table]),
        "stripped": _write(tmp_path / "stripped", [_not_decoded(table)]),
        "mixed": _write(tmp_path / "mixed", [early, _not_decoded(late)]),
    }


def _decoded_channels(columns: Any) -> list[str]:
    return sorted(str(c) for c in columns if str(c) in ON_READ)


# ---------------------------------------------------------------------------
# Storage: impossible
# ---------------------------------------------------------------------------


def test_the_storage_subscription_refuses_a_decoded_record() -> None:
    from src.daemon.drain import storable

    frame = TelemetryRecord.can(1_757_000_000.0, 0x1F9, bytes(8))
    decoded = TelemetryRecord.decoded(frame.timestamp, "ECM_RPM", 800.0, "rpm")
    assert storable(frame) is True
    assert storable(decoded) is False


def test_the_writer_refuses_a_decoded_record(tmp_path: Path) -> None:
    from src.storage.parquet_writer import ParquetTelemetryWriter

    writer = ParquetTelemetryWriter(tmp_path / "storage")
    frame = TelemetryRecord.can(1_757_000_000.0, 0x1F9, bytes(8))
    writer.write(frame)
    with pytest.raises(ValueError, match="P02.8"):
        writer.write(TelemetryRecord.decoded(frame.timestamp, "ECM_RPM", 800.0, "rpm"))


async def test_the_daemon_stores_no_decoded_row_whatever_config_or_profile_say(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The retired knobs still load (a Pi config and a saved catalog carry
    them) and turn nothing back on: the daemon's own drain wiring, fed CAN
    frames and the stored decoded rows a legacy replay emits, writes the
    frames and no decoded row, and the loader decodes the frames on read."""
    from src.collection import tuning_profile
    from src.collection.capture_profiles import CaptureProfile
    from src.daemon import build_components
    from src.daemon.drain import drain_queue_with_broadcast
    from src.daemon.runtime import drain_kwargs
    from src.preprocessing.loader import read_session
    from src.storage.parquet_writer import ParquetTelemetryWriter
    from tests.configs import daemon_test_config

    with caplog.at_level(logging.WARNING):
        config = daemon_test_config(
            tmp_path, collection={"persist_decoded_confidence": ["confirmed", "high"]}
        )
        profile = CaptureProfile.model_validate(
            {
                "name": "tuning",
                "mode01": {"RPM": 1.0},
                "storage_prefix": "_capture-tuning",
                "broadcast_decode": True,
            }
        )
    assert "persist_decoded_confidence is retired" in caplog.text
    assert not hasattr(config.collection, "persist_decoded_confidence")
    assert "broadcast_decode" not in profile.model_dump()

    components = build_components(config, load_models=False)
    writer = ParquetTelemetryWriter(tmp_path / "storage")
    kwargs = drain_kwargs(config, components, active_session_path=lambda: writer.paths.root)
    assert "storage_decode" not in kwargs
    tuning_profile.activate(profile, source="test")
    try:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        drain = asyncio.create_task(drain_queue_with_broadcast(queue, writer, stop, **kwargs))
        for i in range(20):
            counts = 6400 + i
            payload = bytes([0x20, 0x00, counts >> 8, counts & 0xFF, 0, 0, 0, 0])
            frame = TelemetryRecord.can(1_757_000_000.0 + i * 0.01, 0x1F9, payload)
            await queue.put(frame)
            await queue.put(TelemetryRecord.decoded(frame.timestamp, "ECM_RPM", 1.0, "rpm"))
        await queue.put(TelemetryRecord.obd(1_757_000_000.5, "RPM", 800.0, "rpm"))
        await queue.join()
        stop.set()
        await asyncio.wait_for(drain, timeout=10)
    finally:
        tuning_profile.set_active(False, source="test")

    sessions = sorted({path.parent for path in (tmp_path / "storage").rglob("*.parquet")})
    assert len(sessions) == 1
    stored = pd.concat(pd.read_parquet(path) for path in sorted(sessions[0].glob("*.parquet")))
    assert stored["source"].value_counts().to_dict() == {"can": 20, "obd": 1}
    read = read_session(sessions[0])
    rpm = read[(read["source"] == "decoded") & (read["channel"] == "ECM_RPM")]
    assert rpm["value_number"].tolist() == [6400.0 + i for i in range(20)]


# ---------------------------------------------------------------------------
# Readers: every one decodes on read, old sessions and new alike
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("copy", ["stripped", "mixed"])
def test_the_loader_decodes_on_read(copies: dict[str, Path], copy: str) -> None:
    from src.preprocessing.loader import iter_session_parts, read_session

    stored = read_session(copies["stored"]).drop(columns="session_path")
    other = read_session(copies[copy]).drop(columns="session_path")
    assert _decoded_channels(stored["source_channel"].unique()) == ON_READ
    pd.testing.assert_frame_equal(stored, other, check_exact=True)
    parts = pd.concat(iter_session_parts(copies[copy], target_rows=500), ignore_index=True)
    pd.testing.assert_frame_equal(stored, parts.drop(columns="session_path"), check_exact=True)
    # A stored row is never what a reader sees: as recorded, the stripped copy
    # has none, and the loader supplied them all.
    recorded = read_session(copies["stripped"], decoded="as_recorded")
    assert not (recorded["source"] == "decoded").any()


@pytest.mark.parametrize("copy", ["stripped", "mixed"])
def test_the_value_number_chain_decodes_on_read(copies: dict[str, Path], copy: str) -> None:
    """Digest, fingerprints, classification, the trip summary, labels, autotag
    and the sequence dataset all read ``resample_and_segment``; before P02.8
    its only ``decoded:`` inputs were stored rows."""
    from src.analysis.fingerprint import compute_fingerprint
    from src.preprocessing.loader import resample_and_segment

    config = example_config(REPO / "canary.example.yaml")
    stored = resample_and_segment([copies["stored"]], config)
    other = resample_and_segment([copies[copy]], config)
    assert _decoded_channels(stored.columns) == ON_READ
    pd.testing.assert_frame_equal(stored, other, check_exact=True)
    assert compute_fingerprint(copies["stored"], config) == compute_fingerprint(
        copies[copy], config
    )


def test_the_derived_numeric_frame_decodes_on_read_and_versions_by_the_catalog(
    copies: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store's ``value_number`` frame is shaped by the on-read catalog now,
    so an edit to one of its bindings rebuilds it and says why."""
    from src.decode import on_read
    from src.preprocessing.derived_store import DerivedStore, use_store
    from src.preprocessing.loader import resample_and_segment
    from src.preprocessing.resampler import resample_config_for

    config = example_config(REPO / "canary.example.yaml")
    resample = resample_config_for(config)
    in_memory = resample_and_segment([copies["stored"]], config)
    with use_store(tmp_path / "derived"):
        through_store = resample_and_segment([copies["stripped"]], config)
        reused = resample_and_segment([copies["stripped"]], config)
    assert _decoded_channels(through_store.columns) == ON_READ
    pd.testing.assert_frame_equal(in_memory, through_store, check_exact=True)
    pd.testing.assert_frame_equal(in_memory, reused, check_exact=True)
    store = DerivedStore(tmp_path / "derived")
    assert store.load_numeric(copies["stripped"], resample)[1].status == "reused"

    catalog = yaml.safe_load((REPO / "data" / "bindings.yaml").read_text())
    for signal in catalog["signals"]:
        if signal["name"] == "ECM_RPM":
            signal["byte_start"] = 0
    edited = tmp_path / "bindings.yaml"
    edited.write_text(yaml.safe_dump(catalog, sort_keys=False))
    monkeypatch.setattr(on_read, "ON_READ_BINDINGS_PATH", edited)
    _reduced, rebuilt = store.load_numeric(copies["stripped"], resample)
    assert rebuilt.status == "built"
    assert "on-read decode catalog changed" in rebuilt.reason


def test_a_binding_edit_reaches_old_and_new_sessions_alike(
    copies: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One semantics, the current catalog: a session's stale stored rows lose
    to the current decode exactly as a session that never stored them."""
    from src.decode import on_read
    from src.preprocessing.loader import read_session

    catalog = yaml.safe_load((REPO / "data" / "bindings.yaml").read_text())
    for signal in catalog["signals"]:
        if signal["name"] == "ECM_RPM":
            signal["byte_start"] = 0
    edited = tmp_path / "bindings.yaml"
    edited.write_text(yaml.safe_dump(catalog, sort_keys=False))
    before = read_session(copies["stored"])
    monkeypatch.setattr(on_read, "ON_READ_BINDINGS_PATH", edited)
    old, new = read_session(copies["stored"]), read_session(copies["stripped"])

    def rpm(frame: pd.DataFrame) -> list[float]:
        rows = frame[(frame["source"] == "decoded") & (frame["channel"] == "ECM_RPM")]
        return rows["value_number"].tolist()

    assert rpm(old) == rpm(new)
    assert rpm(old) != rpm(before)


@pytest.mark.parametrize("copy", ["stripped", "mixed"])
def test_read_channels_reads_decoded_specs(copies: dict[str, Path], copy: str) -> None:
    from src.analysis.channels import read_channels

    stored = read_channels(copies["stored"], ON_READ, acquired_time=True)
    other = read_channels(copies[copy], ON_READ, acquired_time=True)
    assert sorted(stored["spec"].unique()) == ON_READ
    pd.testing.assert_frame_equal(stored, other, check_exact=True)


def test_plausibility_streams_decode_every_decoded_sample(copies: dict[str, Path]) -> None:
    from src.decode.bindings import load_bindings
    from src.decode.dids import load_did_map
    from src.plausibility.limits import (
        PLAUSIBILITY_FILENAME,
        load_plausibility_profile,
        resolve_limits,
    )
    from src.plausibility.offline import load_stream

    config = example_config(REPO / "canary.example.yaml")
    bindings = load_bindings(REPO / "data" / "bindings.yaml")
    did_map = load_did_map(REPO / "data" / "dids.yaml")
    catalog = resolve_limits(
        load_plausibility_profile(REPO / "data" / "nissan-xterra-2008" / PLAUSIBILITY_FILENAME),
        bindings=bindings,
        did_map=did_map,
        operating_modes=config.preprocessing.operating_modes,
        priority_channels=config.collection.obd_priority_channels,
    )
    streams = {
        name: load_stream(copies[name], config, catalog, bindings=bindings, did_map=did_map)
        for name in ("stored", "stripped", "mixed")
    }
    decoded = streams["stored"].samples["channel"].str.startswith("decoded:")
    assert decoded.sum() > 0
    for name in ("stripped", "mixed"):
        pd.testing.assert_frame_equal(
            streams["stored"].samples, streams[name].samples, check_exact=True
        )
        assert list(streams["stored"].modes) == list(streams[name].modes)


@pytest.mark.parametrize("copy", ["stripped", "mixed"])
def test_drive_activity_counts_decoded_broadcast(copies: dict[str, Path], copy: str) -> None:
    import numpy as np

    from src.analysis import drives

    stored = drives.activity_seconds(copies["stored"])
    obd_only = drives._read_seconds(sorted(copies["stored"].glob("*.parquet")), ("obd",))
    assert obd_only is not None and stored.size > np.unique(obd_only).size
    assert np.array_equal(stored, drives.activity_seconds(copies[copy]))


@pytest.mark.parametrize("copy", ["stripped", "mixed"])
def test_the_dashboard_readers_and_vehicle_state(copies: dict[str, Path], copy: str) -> None:
    from datetime import UTC, datetime

    from src.dashboard.readers import read_latest_per_channel, read_latest_telemetry_rows
    from src.vehicle_state import derive_from_latest_rows

    def latest(session: Path) -> list[dict[str, Any]]:
        return read_latest_per_channel(session.parent, session_dir=session)

    stored = latest(copies["stored"])
    assert _decoded_channels(f"{r['source']}:{r['channel']}" for r in stored) == ON_READ
    assert stored == latest(copies[copy])
    now = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)
    assert derive_from_latest_rows(
        stored, replay_id=None, now=now, passive=False
    ) == derive_from_latest_rows(latest(copies[copy]), replay_id=None, now=now, passive=False)
    if copy == "stripped":  # one chunk: the newest-session tail reader
        assert read_latest_telemetry_rows(copies["stored"].parent, n=300) == (
            read_latest_telemetry_rows(copies[copy].parent, n=300)
        )


@pytest.mark.parametrize("copy", ["stripped", "mixed"])
def test_fetch_channel_rows_decodes_decoded_keys_in_the_query(
    copies: dict[str, Path], copy: str
) -> None:
    """Binned analysis and the session chart; a stored decoded row is never
    read (its source is excluded), so old and new sessions answer alike."""
    from src.storage.telemetry_query import TelemetryQuery

    def fetch(session: Path) -> list[dict[str, Any]]:
        query = TelemetryQuery(session.parent)
        try:
            return query.fetch_channel_rows([*ON_READ, "obd:RPM"], session=NAME)
        finally:
            query.close()

    stored = fetch(copies["stored"])
    assert sorted({row["channel_key"] for row in stored}) == sorted([*ON_READ, "obd:RPM"])

    def canonical(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        return sorted((r["timestamp"], r["channel_key"], r["value_number"]) for r in rows)

    assert canonical(stored) == canonical(fetch(copies[copy]))


def test_query_telemetry_decodes_on_read_and_names_the_kind_of_empty(
    copies: dict[str, Path],
) -> None:
    """Raw SQL sees the current catalog's decoded rows, whether or not the
    session stored any (P02.8 verification, 2026-09-26: a stripped session
    returned 0 decoded ECM_RPM rows against 76,600). An empty answer, rows or
    a zero aggregate, still says which kind of empty it is."""
    from src.mcp_server.tools.telemetry import query_telemetry
    from src.storage.telemetry_query import TelemetryQuery

    count = (
        "SELECT channel, count(*) AS n FROM telemetry WHERE source = 'decoded' "
        "GROUP BY channel ORDER BY channel"
    )
    answers = {}
    for name in ("stored", "stripped"):
        query = TelemetryQuery(copies[name].parent)
        try:
            answers[name] = query_telemetry(query, sql=count)
            if name == "stripped":
                none = query_telemetry(
                    query,
                    sql="SELECT count(*) AS n FROM telemetry "
                    "WHERE source = 'decoded' AND channel = 'NO_SUCH_BINDING'",
                )
                plain = query_telemetry(query, sql="SELECT count(*) AS n FROM telemetry")
        finally:
            query.close()
    assert answers["stripped"]["rows"] == answers["stored"]["rows"]
    assert {row["channel"] for row in answers["stripped"]["rows"]} >= {
        key.split(":", 1)[1] for key in ON_READ
    }
    assert "P02.8" in answers["stripped"]["note"]
    assert "empty_reason" not in answers["stripped"]
    assert none["rows"] == [{"n": 0}] and "empty_reason" in none
    assert "note" not in plain and "empty_reason" not in plain


def test_broadcast_discovery_and_the_pull_report_load_decoded_rows(
    copies: dict[str, Path],
) -> None:
    from src.analysis import broadcast_discovery
    from tools import pull_report

    loads = {name: broadcast_discovery._load_session(copies[name]) for name in copies}
    assert (loads["stored"]["source"] == "decoded").sum() > 0
    pd.testing.assert_frame_equal(loads["stored"], loads["stripped"], check_exact=True)
    frames = {name: pull_report._read_session_frame(copies[name]) for name in copies}
    assert (frames["stored"]["source"] == "decoded").sum() > 0
    for name in ("stripped", "mixed"):
        pd.testing.assert_frame_equal(
            frames["stored"].reset_index(drop=True),
            frames[name].reset_index(drop=True),
            check_exact=True,
        )


def test_a_saved_catalog_carrying_broadcast_decode_loads_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Saved catalogs on the Pi carry the retired field and are never
    re-seeded: they load, the field is dropped, and the load names it (once
    per process; the conftest reset makes each test a fresh process here)."""
    from src.collection.capture_profiles import load_capture_profiles

    config = example_config(REPO / "canary.example.yaml")
    saved = yaml.safe_load((REPO / "data" / "capture_profiles.yaml").read_text())
    for profile in saved["profiles"]:
        profile["broadcast_decode"] = True
    path = tmp_path / "capture_profiles.yaml"
    path.write_text(yaml.safe_dump(saved, sort_keys=False))
    with caplog.at_level(logging.WARNING, logger="src.collection.capture_profiles"):
        catalog = load_capture_profiles(path, config.collection)
        load_capture_profiles(path, config.collection)
    assert [p.name for p in catalog.profiles] == [p["name"] for p in saved["profiles"]]
    assert all("broadcast_decode" not in p.model_dump() for p in catalog.profiles)
    warnings = [r for r in caplog.records if "broadcast_decode is retired" in r.getMessage()]
    assert len(warnings) == 1
