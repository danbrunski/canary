"""The bounded session loader must reproduce the whole-session chain exactly.

``resample_session_bounded`` (the trainers' and ``evaluate_detectors``'
per-session chain) reads a session in time-disjoint parts and keeps only the
rows the resampler's output depends on, so a 2 h drive no longer needs ~15 GB
(A02.11 was OOM-killed on one). Every test here compares against
``resample_long_session(read_session(...))`` -- the unbounded chain it
replaced -- with ``check_exact=True``: a memory fix may not move a single cell.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from src.decode.bindings import load_bindings
from src.decode.dbc import DEFAULT_DBC_DIR, load_dbc_dir
from src.decode.dids import load_did_map
from src.features.feature_pipeline import resample_long_session, resample_session_bounded
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.preprocessing import session_decode
from src.preprocessing.derived import grid_interval_ns, reduce_long_frames, resample_reduced
from src.preprocessing.loader import iter_session_parts, read_session, to_wide
from src.preprocessing.quality_gate import apply_quality_gate
from src.preprocessing.resampler import ResampleConfig
from src.telemetry import TelemetryRecord

DRIVE_SLICE = Path("tests/fixtures/ground_truth/drive-slice/telemetry-00000.parquet")


def _unbounded(session: Path, bindings: object, dbc: object, dids: object) -> pd.DataFrame:
    # As recorded, like the chain under test: it recomputes every binding with
    # the catalog it is given, where the loader's decode on read would add the
    # repo catalog's own (P02.8).
    return resample_long_session(
        read_session(session, decoded="as_recorded"),
        bindings,
        dbc,
        dids,
        ResampleConfig(),
        session_name=session.name,
    )


def _split_into_chunks(source: Path, out: Path, *, chunks: int, overlapping: set[int]) -> Path:
    """Rewrite one parquet file as ``chunks`` time-ordered files, swapping rows
    across each seam in ``overlapping`` so those neighbours' time ranges
    overlap (the writer's flush order is not a time order), then add a 0-byte
    tail chunk (a crash mid-write), which every reader skips."""
    table = pq.read_table(source)  # type: ignore[no-untyped-call]
    order = pc.sort_indices(table, sort_keys=[("timestamp", "ascending")])  # type: ignore[attr-defined]
    table = table.take(order)
    out.mkdir(parents=True)
    bounds = [int(b) for b in np.linspace(0, table.num_rows, chunks + 1)]
    members: list[list[int]] = [list(range(bounds[i], bounds[i + 1])) for i in range(chunks)]
    for seam in overlapping:  # between chunk seam-1 and chunk seam
        left, right = members[seam - 1], members[seam]
        members[seam - 1] = left[:-30] + right[:30]
        members[seam] = left[-30:] + right[30:]
    for i, rows in enumerate(members):
        pq.write_table(  # type: ignore[no-untyped-call]
            table.take(pa.array(sorted(rows))), out / f"telemetry-{i:05d}.parquet"
        )
    (out / f"telemetry-{chunks:05d}.parquet").write_bytes(b"")
    return out


def test_real_drive_slice_matches_the_whole_session_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real bus data, real bindings/DBC/DID decode, many overlapping chunks."""
    session = _split_into_chunks(
        DRIVE_SLICE, tmp_path / "2026-08-03T231900", chunks=9, overlapping={2, 5, 6}
    )
    bindings = load_bindings(BINDINGS_YAML)
    dbc = load_dbc_dir(DEFAULT_DBC_DIR)
    dids = load_did_map(DIDS_YAML)

    parts: list[int] = []
    real = iter_session_parts

    def spy(
        session_dir: Path, *, target_rows: int, filters: object = None, decoded: str = "on_read"
    ) -> object:
        for part in real(
            session_dir,
            target_rows=target_rows,
            filters=filters,
            decoded=decoded,  # type: ignore[arg-type]
        ):
            parts.append(len(part))
            yield part

    monkeypatch.setattr("src.preprocessing.session_decode.iter_session_parts", spy)
    bounded = resample_session_bounded(
        session, bindings, dbc, dids, ResampleConfig(), part_rows=2_000
    )

    assert len(parts) >= 3, parts  # overlapping chunks merged, still several parts
    pd.testing.assert_frame_equal(
        _unbounded(session, bindings, dbc, dids), bounded, check_exact=True
    )


def test_iter_session_parts_concatenate_to_read_session(tmp_path: Path) -> None:
    session = _split_into_chunks(
        DRIVE_SLICE, tmp_path / "2026-08-03T231900", chunks=7, overlapping={3}
    )
    parts = list(iter_session_parts(session, target_rows=1_500))

    assert len(parts) > 1
    for earlier, later in zip(parts, parts[1:], strict=False):
        assert earlier["timestamp"].max() < later["timestamp"].min()
    pd.testing.assert_frame_equal(
        read_session(session),
        pd.concat(parts, ignore_index=True),
        check_exact=True,
    )


def _synthetic_session(root: Path) -> Path:
    """OBD channels covering the cases the part-by-part coercion and gate must
    get right, written out of time order into many small chunks."""
    from src.storage.parquet_writer import write_records_to_parquet

    start = 1_780_000_000.0
    rng = np.random.default_rng(7)
    records: list[TelemetryRecord] = []
    for i in range(3_000):
        # 20 rows a second (slot = row within its 1 s bin), so a bin holds
        # many rows and the support-row cut drops most of them. Slot 0 sits
        # exactly on a grid point. A long power-off gap splits a segment.
        slot = i % 20
        t = start + i * 0.05 + (0.0 if slot == 0 else float(rng.uniform(0, 0.004)))
        if i >= 2_000:
            t += 400.0  # past session_gap_seconds: a second logical segment
        records.append(TelemetryRecord.obd(t, "RPM", float(700 + (i * 37) % 900), "rpm"))
        if slot in (0, 5, 15):
            records.append(TelemetryRecord.obd(t, "SPEED", float(i % 60), "km/h"))
        if slot == 3:
            # Numeric strings only: text rows, but numeric for the session.
            records.append(TelemetryRecord.obd(t, "DID_1206", f"{i % 100:04d}", ""))
        # Gate-limited, numeric only: 150.000001 is out of range as float64
        # but rounds onto the 150.0 limit in float32 (the session dtype), so
        # it passes; 200 fails either way.
        coolant = 150.000001 if slot == 10 else 200.0 if i % 13 == 0 else 90.0 + (i % 5)
        records.append(TelemetryRecord.obd(t, "COOLANT_TEMP", coolant, "degC"))
        # The next two stop at slot 10, their bin's last reading, which no
        # other channel's first or last lands on: whether it survives the gate
        # decides the bin's value, and only the right dtype's gate gets it.
        if slot <= 10:
            # Gate-limited with one text row late: all-numeric in early parts
            # but object for the session, so float64 rejects 100.000001.
            throttle: float | str = 100.000001 if slot == 10 else 20.0 + i % 7
            if i == 2_402:
                throttle = "NO DATA"
            records.append(TelemetryRecord.obd(t, "THROTTLE_POS", throttle, "%"))
            # Gate-limited, numeric strings: text rows but float32 for the
            # session, so "100.000001" passes.
            load = "100.000001" if slot == 10 else f"{i % 90}.5"
            records.append(TelemetryRecord.obd(t, "ENGINE_LOAD", load, "%"))
        else:
            # Rows that exist but carry nothing: the bin's last *row* is not
            # its last *reading*, so the edge above survives only as a reading.
            records.append(TelemetryRecord.obd(t, "THROTTLE_POS", None, "%"))
            records.append(TelemetryRecord.obd(t, "ENGINE_LOAD", None, "%"))
        if i % 97 == 0:
            records.append(TelemetryRecord.obd(t, "VIN", "5N1AN08W68C500000", ""))
            records.append(TelemetryRecord.obd(t, "MIL", i % 2 == 0, ""))
    # Blocks of records flushed out of time order: chunk time ranges overlap.
    blocks = [records[k : k + 900] for k in range(0, len(records), 900)]
    order = [1, 0, 2, 4, 3, 5, 6, 8, 7, *range(9, len(blocks))]
    shuffled = [record for idx in order for record in blocks[idx]]
    session = root / "2026-06-01T000000"
    write_records_to_parquet(shuffled, session, max_rows=400)
    return session


def test_synthetic_session_text_channels_and_gate_dtypes_match(tmp_path: Path) -> None:
    session = _synthetic_session(tmp_path)
    expected = _unbounded(session, None, None, None)
    # The cases above are live: each channel's dtype and reject count on the
    # whole-session wide frame is the one the comments promise.
    wide = to_wide(read_session(session))
    _, quality = apply_quality_gate(wide)
    assert quality.reject_counts["obd:COOLANT_TEMP"] == (wide["obd:COOLANT_TEMP"] == 200.0).sum()
    assert (
        quality.reject_counts["obd:THROTTLE_POS"]
        == (wide["obd:THROTTLE_POS"] == 100.000001).sum()
        > 0
    )
    assert "obd:ENGINE_LOAD" not in quality.reject_counts
    assert (wide["obd:ENGINE_LOAD"] == np.float32(100.000001)).any()
    assert not pd.api.types.is_numeric_dtype(expected["obd:VIN"])
    assert not pd.api.types.is_numeric_dtype(expected["obd:THROTTLE_POS"])
    assert expected["obd:ENGINE_LOAD"].dtype == np.float32
    assert expected["obd:DID_1206"].dtype == np.float32
    for part_rows in (500, 3_000, 10_000_000):
        bounded = resample_session_bounded(
            session, None, None, None, ResampleConfig(), part_rows=part_rows
        )
        pd.testing.assert_frame_equal(expected, bounded, check_exact=True)


def test_the_float32_gate_edge_is_exercised(tmp_path: Path) -> None:
    """Guard for the test above: 150.000001 really does pass the gate as
    float32 and fail it as float64, so the dtype bookkeeping matters."""
    wide = pd.DataFrame(
        {
            "session": "s",
            "timestamp": pd.Timestamp("2026-06-01", tz="UTC"),
            "obd:COOLANT_TEMP": [150.000001],
        }
    )
    as_object = wide.astype({"obd:COOLANT_TEMP": object})
    as_float = wide.astype({"obd:COOLANT_TEMP": "float32"})
    assert apply_quality_gate(as_object)[1].total_rejected == 1
    assert apply_quality_gate(as_float)[1].total_rejected == 0


def _synthetic_long(hz_seed: int) -> pd.DataFrame:
    """A decoded long frame with the awkward cases, built directly (no parquet)."""
    rng = np.random.default_rng(hz_seed)
    n = 6_000
    base = pd.Timestamp("2026-06-01T23:58:00", tz="UTC")  # crosses midnight
    offsets = np.cumsum(rng.exponential(0.01, n))
    offsets[2500:] += 700.0  # bridged below by empty rows: one segment
    offsets[4500:] += 400.0  # past session_gap_seconds: a segment split
    offsets[::97] = np.floor(offsets[::97])  # stamps exactly on grid points
    stamps = base + pd.to_timedelta(offsets, unit="s")
    channel = rng.choice(["RPM", "RPM", "RPM", "INTAKE_TEMP", "MIXED", "BURST", "SPEED"], n)
    value = pd.Series(rng.normal(50, 20, n), dtype=object)
    value[rng.random(n) < 0.2] = np.nan  # rows that exist but carry nothing
    mixed = channel == "MIXED"
    value[mixed & (rng.random(n) < 0.3)] = "text"
    burst = channel == "BURST"
    value[burst & (np.arange(n) > 400)] = np.nan  # a channel that dies
    value[channel == "INTAKE_TEMP"] = 300.0  # over the gate's 250 limit...
    value[(channel == "INTAKE_TEMP") & (rng.random(n) < 0.5)] = 30.0  # ...half the time
    long = pd.DataFrame(
        {
            "session": "2026-06-01T235800",
            "timestamp": stamps,
            "source": "obd",
            "channel": channel,
            "value": value,
        }
    )
    # Empty rows only: 3 s before the first reading (they set where the grid
    # starts) and every 250 s across the 700 s gap (they keep it one segment).
    first, gap_start = stamps[0], stamps[2499]
    empty_at = [first - pd.Timedelta(seconds=s) for s in (3.0, 1.5)]
    empty_at += [gap_start + pd.Timedelta(seconds=s) for s in (250.0, 500.0)]
    empty = pd.DataFrame(
        {
            "session": "2026-06-01T235800",
            "timestamp": empty_at,
            "source": "obd",
            "channel": "SPEED",
            "value": pd.Series([np.nan] * len(empty_at), dtype=object),
        }
    )
    long = pd.concat([empty, long], ignore_index=True)
    # Same-instant duplicates of one channel: the pivot keeps the last non-null.
    dupes = long.iloc[::50].copy()
    dupes["value"] = [np.nan if i % 2 else 7.0 for i in range(len(dupes))]
    long = pd.concat([long, dupes], ignore_index=True)
    long["source_channel"] = long["source"] + ":" + long["channel"]
    return session_decode.decode_long_frame(long, None, None, None)


@pytest.mark.parametrize("hz", [1.0, 2.0, 0.5, 0.25])
def test_reduced_cells_rebuild_the_resampled_frame(hz: float) -> None:
    """``resample_reduced`` over the kept cells equals the whole-frame chain."""
    long = _synthetic_long(int(hz * 100))
    config = ResampleConfig(hz=hz)
    expected = resample_long_session(long, None, None, None, config, session_name="s")
    reduced = reduce_long_frames(
        "2026-06-01T235800",
        [long.iloc[:3000], long.iloc[3000:]],
        grid_interval_ns(config),
        can_storage=None,
    )

    assert len(reduced.cells) < len(long) / 3  # it actually reduced
    assert "obd:MIXED" in reduced.non_numeric
    pd.testing.assert_frame_equal(expected, resample_reduced(reduced, config), check_exact=True)


def test_a_projected_frame_is_the_full_frame_cut_to_those_channels() -> None:
    long = _synthetic_long(3)
    config = ResampleConfig()
    reduced = reduce_long_frames("s", [long], grid_interval_ns(config), can_storage=None)
    full = resample_reduced(reduced, config)
    wanted = ["obd:BURST", "obd:SPEED"]
    columns = [
        "session",
        "timestamp",
        *wanted,
        *(f"{c}__gap" for c in wanted),
        *(f"{c}__arrived" for c in wanted),
    ]
    pd.testing.assert_frame_equal(
        full[columns], resample_reduced(reduced, config, channels=wanted), check_exact=True
    )


def test_refusals_name_their_cause() -> None:
    long = _synthetic_long(4)
    reduced = reduce_long_frames("s", [long], grid_interval_ns(ResampleConfig()), can_storage=None)
    with pytest.raises(KeyError, match="obd:NOPE"):
        resample_reduced(reduced, ResampleConfig(), channels=["obd:NOPE"])
    with pytest.raises(ValueError, match="re-derive"):
        resample_reduced(reduced, ResampleConfig(hz=2.0))
    with pytest.raises(ValueError, match="does not divide a day"):
        grid_interval_ns(ResampleConfig(hz=1 / 7))
    with pytest.raises(ValueError, match="shorter than the"):
        grid_interval_ns(ResampleConfig(hz=0.25, session_gap_seconds=1.0))
    with pytest.raises(ValueError, match="shorter than the"):
        resample_reduced(reduced, ResampleConfig(session_gap_seconds=0.5))
    grid_interval_ns(ResampleConfig(hz=0.25, session_gap_seconds=4.0))  # equal is exact
