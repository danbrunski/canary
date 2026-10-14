"""The derived session layer (P01.2/P01.3): stored once, versioned, per channel.

Every frame read back from a store must equal the in-memory chain exactly,
whatever mix of reused and re-derived channels produced it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from src.decode.bindings import Bindings, load_bindings
from src.decode.dbc import DEFAULT_DBC_DIR, load_dbc_dir
from src.decode.dids import DidMap, load_did_map
from src.features import feature_pipeline
from src.features.feature_pipeline import resample_long_session, resample_session_bounded
from src.paths import BINDINGS_YAML, DATA_DIR, DIDS_YAML
from src.preprocessing import derived_store
from src.preprocessing.derived_store import (
    DEFAULT_DERIVED_DIR,
    DERIVED_DIR_ENV,
    DecodeInputs,
    DerivedStore,
    active_store,
)
from src.preprocessing.loader import read_session
from src.preprocessing.resampler import ResampleConfig

DRIVE_SLICE = Path("tests/fixtures/ground_truth/drive-slice/telemetry-00000.parquet")
DID_HEX = "1206"


def _session(root: Path) -> Path:
    """The ground-truth slice as three chunks, plus a chunk of Mode 22 reads."""
    table = pq.read_table(DRIVE_SLICE)
    table = table.take(pc.sort_indices(table, sort_keys=[("timestamp", "ascending")]))
    session = root / "2026-08-03T231900"
    session.mkdir(parents=True)
    bounds = [int(b) for b in np.linspace(0, table.num_rows, 4)]
    for i in range(3):
        pq.write_table(
            table.slice(bounds[i], bounds[i + 1] - bounds[i]),
            session / f"telemetry-{i:05d}.parquet",
        )
    obd = table.filter(pc.equal(table["channel"], "RPM"))
    n = obd.num_rows
    payloads = [f"{(200 + 7 * i) % 900:04X}" for i in range(n)]
    reads = obd.set_column(
        obd.schema.get_field_index("channel"), "channel", pa.array([f"DID_{DID_HEX}"] * n)
    )
    reads = reads.set_column(
        reads.schema.get_field_index("value_number"),
        "value_number",
        pa.nulls(n, type=pa.float64()),
    )
    reads = reads.set_column(
        reads.schema.get_field_index("value_text"), "value_text", pa.array(payloads)
    )
    pq.write_table(reads, session / "telemetry-00003.parquet")
    # K-line reads name their module in the channel (``<module>:<hex>``), so a
    # channel may itself contain the source separator.
    kline = obd.set_column(obd.schema.get_field_index("source"), "source", pa.array(["kline"] * n))
    kline = kline.set_column(
        kline.schema.get_field_index("channel"), "channel", pa.array(["ABS:1210"] * n)
    )
    pq.write_table(kline, session / "telemetry-00004.parquet")
    return session


def _catalogs() -> DecodeInputs:
    return DecodeInputs(
        bindings=load_bindings(BINDINGS_YAML),
        dbc=load_dbc_dir(DEFAULT_DBC_DIR),
        did_map=load_did_map(DIDS_YAML),
    )


def _expected(session: Path, decoders: DecodeInputs) -> pd.DataFrame:
    # As recorded, like the store's chain (the loader's decode on read would
    # add the repo catalog's on-read signals beside ``decoders``', P02.8).
    return resample_long_session(
        read_session(session, decoded="as_recorded"),
        decoders.bindings,
        decoders.dbc,
        decoders.did_map,
        ResampleConfig(),
        session_name=session.name,
    )


def _through(store: DerivedStore, session: Path, decoders: DecodeInputs) -> pd.DataFrame:
    return resample_session_bounded(
        session,
        decoders.bindings,
        decoders.dbc,
        decoders.did_map,
        ResampleConfig(),
        store=store,
    )


@pytest.fixture
def reduce_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every ``reduce_session`` call (what was decoded, and from which rows)."""
    calls: list[dict[str, Any]] = []
    real = derived_store.reduce_session

    def spy(session_dir: Path, *decoders: Any, **kwargs: Any) -> Any:
        bindings, dbc, did_map, _resample = decoders
        calls.append({"bindings": bindings, "dbc": dbc, "did_map": did_map, **kwargs})
        return real(session_dir, *decoders, **kwargs)

    monkeypatch.setattr(derived_store, "reduce_session", spy)
    return calls


def test_a_second_load_reads_the_stored_frame_and_decodes_nothing(
    tmp_path: Path, reduce_calls: list[dict[str, Any]]
) -> None:
    session = _session(tmp_path / "sessions")
    decoders = _catalogs()
    store = DerivedStore(tmp_path / "derived")

    first_reduced, first = store.load(session, decoders, ResampleConfig())
    second_reduced, second = store.load(session, decoders, ResampleConfig())

    assert (first.status, second.status) == ("built", "reused")
    assert len(reduce_calls) == 1  # the build; the reuse decoded nothing
    assert "did:ECM_INJECTOR_PULSE_1" in second_reduced.channels
    assert "kline:ABS:1210" in second_reduced.channels
    pd.testing.assert_frame_equal(
        _expected(session, decoders), _through(store, session, decoders), check_exact=True
    )
    pd.testing.assert_frame_equal(first_reduced.cells, second_reduced.cells, check_dtype=False)


def test_a_stale_frame_is_rebuilt_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path / "sessions")
    decoders = _catalogs()
    store = DerivedStore(tmp_path / "derived")
    store.load(session, decoders, ResampleConfig())

    extra = pq.read_table(session / "telemetry-00000.parquet").slice(0, 10)
    pq.write_table(extra, session / "telemetry-00009.parquet")
    assert store.status(session, decoders, ResampleConfig()) == "the session's chunk files changed"
    _, report = store.load(session, decoders, ResampleConfig())
    assert (report.status, report.reason) == ("built", "the session's chunk files changed")

    monkeypatch.setattr(derived_store, "DERIVED_CODE_VERSION", 999)
    _, report = store.load(session, decoders, ResampleConfig())
    assert report.status == "built"
    assert "code version" in report.reason
    pd.testing.assert_frame_equal(
        _expected(session, decoders), _through(store, session, decoders), check_exact=True
    )


def _cells_files(frame_dir: Path) -> list[str]:
    return sorted(p.name for p in frame_dir.glob("cells*.parquet"))


def test_racing_writers_never_serve_one_writers_meta_over_the_others_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writer B, with one binding edited, runs its whole load between writer
    A's cells rename and A's meta rename.

    With fixed file names this left A's meta over B's cells, and the next load
    served B's coolant channel as "every input hash matches" (P01
    verification, 2026-09-26). A meta now names its own write's cells, so the
    worst a race can leave is a frame whose file is gone, which is rebuilt.
    """
    session = _session(tmp_path / "sessions")
    v1 = _catalogs()
    v2 = DecodeInputs(
        bindings=_edited(v1.bindings, "ECM_COOLANT_TEMP", byte_start=1),
        dbc=v1.dbc,
        did_map=v1.did_map,
    )
    store = DerivedStore(tmp_path / "derived")
    real_replace = os.replace
    other: list[str] = []

    def replace(src: Any, dst: Any) -> None:
        if not other and str(dst).endswith("meta.json"):
            monkeypatch.setattr(derived_store.os, "replace", real_replace)
            _, report_b = DerivedStore(tmp_path / "derived").load(session, v2, ResampleConfig())
            other.append(report_b.status)
            monkeypatch.setattr(derived_store.os, "replace", replace)
        real_replace(src, dst)

    monkeypatch.setattr(derived_store.os, "replace", replace)
    store.load(session, v1, ResampleConfig())
    monkeypatch.setattr(derived_store.os, "replace", real_replace)
    assert other == ["built"]  # B really ran inside A's write

    _, report = store.load(session, v1, ResampleConfig())
    assert report.status == "built"
    assert "is gone (superseded by a concurrent write)" in report.reason
    pd.testing.assert_frame_equal(
        _expected(session, v1), _through(store, session, v1), check_exact=True
    )
    assert len(_cells_files(store.frame_dir(session))) == 1


def test_a_frame_whose_cells_file_is_gone_is_rebuilt_and_says_why(tmp_path: Path) -> None:
    session = _session(tmp_path / "sessions")
    decoders = _catalogs()
    store = DerivedStore(tmp_path / "derived")
    store.load(session, decoders, ResampleConfig())
    (cells,) = store.frame_dir(session).glob("cells-*.parquet")
    cells.unlink()

    stale = store.status(session, decoders, ResampleConfig())
    assert stale == f"cells file {cells.name} is gone (superseded by a concurrent write)"
    _, report = store.load(session, decoders, ResampleConfig())
    assert (report.status, report.reason) == ("built", stale)
    pd.testing.assert_frame_equal(
        _expected(session, decoders), _through(store, session, decoders), check_exact=True
    )


def test_a_version_1_frame_is_rebuilt_and_its_fixed_name_cells_deleted(tmp_path: Path) -> None:
    """Version 1 kept ``cells.parquet`` beside a meta that names no file. Such
    a pair may be one writer's meta over another's cells, so it is rebuilt,
    never read."""
    session = _session(tmp_path / "sessions")
    decoders = _catalogs()
    store = DerivedStore(tmp_path / "derived")
    store.load(session, decoders, ResampleConfig())
    frame_dir = store.frame_dir(session)
    meta_path = frame_dir / "meta.json"
    (cells,) = frame_dir.glob("cells-*.parquet")
    cells.rename(frame_dir / "cells.parquet")
    meta = json.loads(meta_path.read_text())
    del meta["cells"]
    meta_path.write_text(json.dumps({**meta, "code_version": 1}))

    _, report = store.load(session, decoders, ResampleConfig())

    assert report.status == "built" and "code version 1 != 2" in report.reason
    assert _cells_files(frame_dir) == [json.loads(meta_path.read_text())["cells"]]
    meta = json.loads(meta_path.read_text())
    del meta["cells"]
    meta_path.write_text(json.dumps(meta))
    assert store.status(session, decoders, ResampleConfig()) == "meta.json names no cells file"


def _edited(bindings: Bindings, name: str, **update: Any) -> Bindings:
    signals = [s.model_copy(update=update) if s.name == name else s for s in bindings.signals]
    return bindings.model_copy(update={"signals": signals})


def test_editing_one_binding_rederives_only_that_channel(
    tmp_path: Path, reduce_calls: list[dict[str, Any]]
) -> None:
    session = _session(tmp_path / "sessions")
    decoders = _catalogs()
    store = DerivedStore(tmp_path / "derived")
    store.load(session, decoders, ResampleConfig())
    reduce_calls.clear()

    edited = DecodeInputs(
        bindings=_edited(decoders.bindings, "ECM_COOLANT_TEMP", byte_start=1),
        dbc=decoders.dbc,
        did_map=decoders.did_map,
    )
    before = _through(store, session, decoders)
    reduced, report = store.load(session, edited, ResampleConfig())

    assert report.status == "refreshed"
    assert report.rederived_units == ["binding:ECM_COOLANT_TEMP"]
    assert report.reused_channels == len(reduced.channels) - 1
    # One filtered read, of that binding's CAN id and its stored rows only.
    (call,) = reduce_calls
    assert [s.name for s in call["bindings"].signals] == ["ECM_COOLANT_TEMP"]
    assert call["dbc"] is None and call["did_map"] is None
    assert {tuple(clause[0]) for clause in call["filters"]} == {
        ("source", "==", "decoded"),
        ("source", "==", "can"),
    }
    after = _through(store, session, edited)
    pd.testing.assert_frame_equal(_expected(session, edited), after, check_exact=True)
    moved = [c for c in after.columns if not after[c].equals(before[c])]
    assert moved and all("ECM_COOLANT_TEMP" in c for c in moved), moved


def test_adding_a_did_adds_its_column_and_touches_nothing_else(
    tmp_path: Path, reduce_calls: list[dict[str, Any]]
) -> None:
    session = _session(tmp_path / "sessions")
    full = _catalogs()
    did_map: DidMap = full.did_map
    without = DecodeInputs(
        bindings=full.bindings,
        dbc=full.dbc,
        did_map=did_map.model_copy(
            update={"dids": [d for d in did_map.dids if f"{d.did:04X}" != DID_HEX]}
        ),
    )
    store = DerivedStore(tmp_path / "derived")
    store.load(session, without, ResampleConfig())
    before = _through(store, session, without)
    assert "did:ECM_INJECTOR_PULSE_1" not in before.columns
    reduce_calls.clear()

    reduced, report = store.load(session, full, ResampleConfig())

    assert report.rederived_units == ["did:ECM_INJECTOR_PULSE_1"]
    assert len(reduce_calls) == 1
    after = _through(store, session, full)
    pd.testing.assert_frame_equal(_expected(session, full), after, check_exact=True)
    for column in before.columns:
        pd.testing.assert_series_equal(before[column], after[column], check_exact=True)


def test_removing_a_binding_drops_its_channel(tmp_path: Path) -> None:
    session = _session(tmp_path / "sessions")
    full = _catalogs()
    fewer = DecodeInputs(
        bindings=full.bindings.model_copy(
            update={"signals": [s for s in full.bindings.signals if s.name != "ECM_COOLANT_TEMP"]}
        ),
        dbc=full.dbc,
        did_map=full.did_map,
    )
    store = DerivedStore(tmp_path / "derived")
    store.load(session, full, ResampleConfig())
    reduced, report = store.load(session, fewer, ResampleConfig())

    assert report.status == "refreshed"
    assert "decoded:ECM_COOLANT_TEMP" not in reduced.channels
    pd.testing.assert_frame_equal(
        _expected(session, fewer), _through(store, session, fewer), check_exact=True
    )


def test_every_unit_rederived_alone_rebuilds_the_one_pass_frame(tmp_path: Path) -> None:
    """The decode units partition the channels: re-deriving each one from its
    own filtered read reproduces the single full pass exactly."""
    session = _session(tmp_path / "sessions")
    decoders = _catalogs()
    store = DerivedStore(tmp_path / "derived")
    full, _ = store.load(session, decoders, ResampleConfig())
    meta_path = store.frame_dir(session) / "meta.json"

    meta = json.loads(meta_path.read_text())
    for unit in meta["units"].values():
        unit["hash"] = "stale"
    meta_path.write_text(json.dumps(meta))

    rebuilt, report = store.load(session, decoders, ResampleConfig())

    assert report.status == "refreshed" and report.reused_channels == 0
    assert rebuilt.channels == full.channels
    pd.testing.assert_frame_equal(
        _expected(session, decoders), _through(store, session, decoders), check_exact=True
    )


def test_the_store_is_opt_in_gitignored_and_off_the_pi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(DERIVED_DIR_ENV, raising=False)
    assert active_store() is None  # the daemon / MCP / tests: reduce in memory
    monkeypatch.setenv(DERIVED_DIR_ENV, str(tmp_path / "d"))
    store = active_store()
    assert store is not None and store.root == tmp_path / "d"
    # data/cache/ is gitignored and no sessions root.
    assert DEFAULT_DERIVED_DIR.relative_to(DATA_DIR / "cache")
    assert "data/cache/" in Path(".gitignore").read_text().splitlines()

    real_read = Path.read_text

    def board(self: Path, *args: Any, **kwargs: Any) -> str:
        if str(self) == "/proc/device-tree/model":
            return "Raspberry Pi 5 Model B Rev 1.0\x00"
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", board)
    with pytest.raises(RuntimeError, match="never built on the Pi"):
        DerivedStore(tmp_path / "d").load(
            _session(tmp_path / "s"), DecodeInputs(), ResampleConfig()
        )


def test_trainers_read_through_the_store_a_script_selects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``retrain`` selects the store for its trainers (and their process pool)
    with ``use_store``; the chain the trainers run then builds into it, and
    the setting does not outlive the block."""
    import argparse

    from scripts.retrain import derived_dir_for
    from src.preprocessing.derived_store import use_store

    monkeypatch.delenv(DERIVED_DIR_ENV, raising=False)
    session = _session(tmp_path / "sessions")
    decoders = _catalogs()
    root = derived_dir_for(argparse.Namespace(feature_cache=tmp_path / "cache" / "feature_frames"))
    assert root == tmp_path / "cache" / "derived_sessions"
    assert derived_dir_for(argparse.Namespace(feature_cache=None)) is None

    with use_store(root):
        frame = feature_pipeline._decode_session_to_resampled(
            session, decoders.bindings, decoders.dbc, decoders.did_map, ResampleConfig()
        )
    assert active_store() is None
    assert list(root.glob("*/cells-*.parquet"))
    pd.testing.assert_frame_equal(_expected(session, decoders), frame, check_exact=True)


def test_a_did_id_declared_twice_is_refused_by_the_catalog_and_the_store(
    tmp_path: Path,
) -> None:
    """The decoder indexes DIDs by id (last entry wins); the store versions
    them per entry. Two entries for one id made a refreshed frame disagree
    with a rebuilt one, so both the catalog and the store refuse it."""
    from pydantic import ValidationError

    did_map = load_did_map(DIDS_YAML)
    twin = did_map.dids[0].model_copy(update={"name": "SHADOW", "aliases": []})
    with pytest.raises(ValidationError, match="declared twice"):
        DidMap.model_validate(
            {**did_map.model_dump(), "dids": [*did_map.model_dump()["dids"], twin.model_dump()]}
        )
    session = _session(tmp_path / "sessions")
    decoders = _catalogs()
    shadowed = DecodeInputs(
        bindings=decoders.bindings,
        dbc=decoders.dbc,
        did_map=did_map.model_copy(update={"dids": [*did_map.dids, twin]}),
    )
    store = DerivedStore(tmp_path / "derived")
    store.load(session, decoders, ResampleConfig())
    with pytest.raises(ValueError, match="twice"):
        store.load(session, shadowed, ResampleConfig())


def _imu_session(root: Path) -> Path:
    """OBD at ~2 Hz, raw IMU acceleration at ~50 Hz, a text channel and a
    power-off gap, written out of time order into many chunks."""
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    rng = np.random.default_rng(11)
    start = 1_780_000_000.0
    records: list[TelemetryRecord] = []
    for i in range(12_000):
        t = start + i * 0.02 + float(rng.uniform(0, 0.004))
        if i >= 8_000:
            t += 400.0
        records.append(
            TelemetryRecord.from_epoch(
                t, source="imu", channel="ACCEL_LON", value=float(rng.normal())
            )
        )
        records.append(
            TelemetryRecord.from_epoch(
                t, source="imu", channel="ACCEL_LAT", value=float(rng.normal())
            )
        )
        if i % 25 == 0:
            records.append(TelemetryRecord.obd(t, "RPM", float(700 + i % 900), "rpm"))
            records.append(
                TelemetryRecord.obd(t, "COOLANT_TEMP", 200.0 if i % 13 == 0 else 90.0, "degC")
            )
        if i % 400 == 0:
            records.append(TelemetryRecord.obd(t, "VIN", "5N1AN08W68C500000", ""))
    blocks = [records[k : k + 2_000] for k in range(0, len(records), 2_000)]
    order = [1, 0, *range(2, len(blocks))]
    session = root / "2026-06-01T000000"
    write_records_to_parquet([r for i in order for r in blocks[i]], session, max_rows=1_500)
    return session


def _whole_session_numeric(session: Path) -> pd.DataFrame:
    """The ``value_number`` rows of every chunk at once: the unbounded read the
    no-store chain made before P02.1, kept here as the reference the
    part-by-part chain must reproduce."""
    from src.decode.on_read import on_read_catalog
    from src.preprocessing.session_reader import _read_numeric_chunks
    from src.storage.session_files import PARQUET_PATTERN
    from src.telemetry import SOURCE_SEP

    frame = pd.concat(
        _read_numeric_chunks(sorted(session.glob(PARQUET_PATTERN)), on_read_catalog()),
        ignore_index=True,
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["session"] = session.name
    frame["source_channel"] = (
        frame["source"].astype(str) + SOURCE_SEP + frame["channel"].astype(str)
    )
    return frame[["session", "timestamp", "source_channel", "value_number"]]


def test_the_value_number_chain_reads_the_store_and_matches_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resample_and_segment(value="value_number")`` (digest, fingerprints,
    classification) gives the whole-session read's frame part by part, with
    or without the store, built or reused, and a tick-aligned part split
    leaves IMU decimation unchanged."""
    from src.config import CanaryConfig
    from src.preprocessing.derived import reduce_numeric_session, resample_numeric_reduced
    from src.preprocessing.derived_store import use_store
    from src.preprocessing.imu_decimation import decimate_imu_long
    from src.preprocessing.loader import resample_and_segment
    from src.preprocessing.resampler import resample_config_for, resample_long
    from src.preprocessing.segmenter import segment_operating_modes

    monkeypatch.delenv(DERIVED_DIR_ENV, raising=False)
    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    resample = resample_config_for(config)
    session = _imu_session(tmp_path / "sessions")

    raw = resample_long(
        decimate_imu_long(_whole_session_numeric(session), hz=resample.hz), config=resample
    )
    assert {"imu:ACCEL_LON_RMS", "imu:JERK_MAX", "imu:ACCEL_LAT_MAX"} <= set(raw.columns)
    for part_rows in (700, 5_000):
        parts = resample_numeric_reduced(
            reduce_numeric_session(session, resample, part_rows=part_rows), resample
        )
        pd.testing.assert_frame_equal(raw, parts, check_exact=True)

    expected = resample_and_segment([session], config, value="value_number")
    pd.testing.assert_frame_equal(
        segment_operating_modes(
            raw,
            config.preprocessing.operating_modes,
            mode_dwell_ticks=config.preprocessing.mode_dwell_ticks,
        ),
        expected,
        check_exact=True,
    )
    with use_store(tmp_path / "derived"):
        built = resample_and_segment([session], config, value="value_number")
        reused = resample_and_segment([session], config, value="value_number")
    store = DerivedStore(tmp_path / "derived")
    (numeric_cells,) = (store.frame_dir(session) / "numeric").glob("cells-*.parquet")
    _, report = store.load_numeric(session, resample)
    assert report.status == "reused"
    pd.testing.assert_frame_equal(expected, built, check_exact=True)
    pd.testing.assert_frame_equal(expected, reused, check_exact=True)
    # The numeric frame is published the same way: a meta naming a missing
    # file is a rebuild that says why, never a read of whatever is there.
    numeric_cells.unlink()
    _, report = store.load_numeric(session, resample)
    assert report.status == "built" and numeric_cells.name in report.reason


def test_without_a_store_the_value_number_chain_reads_one_part_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P02.1: with no store (the Pi's digest and classification children, and
    every laptop caller that does not activate one) the chain still reads the
    session part by part, so its memory is one part plus the 1 Hz cells, never
    the whole session. The store only adds reuse."""
    from src.config import CanaryConfig
    from src.preprocessing import derived, imu_stream, session_pipeline, session_reader
    from src.storage.session_files import count_session_rows

    monkeypatch.delenv(DERIVED_DIR_ENV, raising=False)
    # The part size is read by name in each module that cuts or batches parts.
    for owner in (session_reader, derived, imu_stream):
        monkeypatch.setattr(owner, "SESSION_PART_ROWS", 3_000)
    session = _imu_session(tmp_path / "sessions")
    total = count_session_rows(session)
    assert total is not None
    reads: list[int] = []
    real = session_reader._read_numeric_chunks

    def counted(paths: Any, catalog: Any) -> list[pd.DataFrame]:
        frames = real(paths, catalog)
        reads.append(sum(len(frame) for frame in frames))
        return frames

    monkeypatch.setattr(session_reader, "_read_numeric_chunks", counted)
    frame = session_pipeline.resample_and_segment(
        [session], CanaryConfig.from_yaml(Path("canary.example.yaml")), value="value_number"
    )
    assert not frame.empty
    assert sum(reads) == total
    assert len(reads) > 2 and max(reads) < total // 2


def test_the_store_is_never_active_on_the_pi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon's digest and workbench children run these readers on the
    Pi; even with the variable set, they keep the no-store path there."""
    monkeypatch.setenv(DERIVED_DIR_ENV, str(tmp_path / "d"))
    monkeypatch.setattr(derived_store, "on_the_pi", lambda: True)
    assert active_store() is None
