"""R23.1: the chunk is built column by column, and is the table it always was.

``records_to_table`` replaced ``pa.Table.from_pylist([r.parquet_row() ...])`` in
the publish thread (a dict per row, then a column taken out of every dict). A
stored chunk is the contract: these tests hold the new build to the old one
over every source, to the exact ``metadata_json`` text, the schema footer and the
nullability, and hold the metadata memo to the one way it could be wrong (two
metadata dicts equal as Python keys but not as JSON).
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src import telemetry
from src.storage.broadcast_storage import CanStorage
from src.storage.parquet_writer import (
    TELEMETRY_SCHEMA,
    ParquetTelemetryWriter,
    chunk_schema,
    records_to_table,
)
from src.telemetry import (
    METADATA_JSON_CACHE_SIZE,
    TelemetryRecord,
    metadata_json,
    split_value,
)

_T0 = datetime(2026, 11, 12, 3, 42, 49, 553_000, tzinfo=UTC)


def _at(i: int) -> datetime:
    return _T0 + timedelta(milliseconds=7 * i)


def _mixed_batch(*, nan_value: bool = False) -> list[TelemetryRecord]:
    """One record of every shape a chunk holds, then the same again so a memo
    has something to hit: CAN (data, error and remote frames), Mode 01 rows,
    DID rows (hex text), memory rows (payload, float metadata), sensor, GPS,
    IMU, derived, K-line and UpRev rows; a bool, an int, a float, text and no
    value; metadata that is empty, int-only, float-bearing and mixed. A NaN
    value is opt-in: Arrow's ``equals`` never calls two NaN equal, so only the
    byte comparison of a written chunk can take it."""
    stamp = _T0.timestamp()
    once: list[TelemetryRecord] = [
        TelemetryRecord.can(stamp, 0x2A0, b"\x00\x01\x02\x03\x04\x05\x06\x07"),
        TelemetryRecord.can(stamp, 0x4, b"\x20\x00", frame_kind="error"),
        TelemetryRecord.can(stamp, 0x7E8, b"", frame_kind="remote"),
        TelemetryRecord.obd(stamp, "RPM", 720, "rpm", payload=b"\x0b\x40"),
        TelemetryRecord.obd(stamp, "COOLANT_TEMP", 88.5, "degC", metadata={"round_id": 3}),
        TelemetryRecord.obd(
            stamp,
            "DID_1208",
            "1586",
            "",
            metadata={
                "acquired_at": 1794454969.362885,
                "acquired_clock": "kernel",
                "generation": 2,
                "responder_id": 2024,
                "round_id": 0,
                "ts_schema": 2,
            },
        ),
        TelemetryRecord.obd(
            stamp,
            "mem:ECM_TARGET_EQUIV_RATIO",
            1.0,
            "phi",
            payload=bytes(range(18)),
            metadata={
                "acquired_at": 1794454969.5526338,
                "response_interval_s": 0.005127668380737305,
            },
        ),
        TelemetryRecord.obd(stamp, "MIL_ON", True, ""),
        TelemetryRecord.obd(stamp, "FUEL_STATUS", None, ""),
        TelemetryRecord.from_epoch(
            stamp,
            source="sensor",
            channel="FUEL_PRESSURE",
            value=41.25,
            unit="psi",
            metadata={"acquired_at": 1794454969.7, "sample_index": 11},
        ),
        TelemetryRecord.gps(stamp, "speed", 17.2, "km/h"),
        TelemetryRecord.gps(stamp, "fix_quality", 1, ""),
        TelemetryRecord.imu(stamp, "ACCEL_X", -0.0123, "g"),
        TelemetryRecord.derived(_T0, "FUEL_RATE", 3.4, "L/h"),
        TelemetryRecord.from_epoch(
            stamp, source="kline", channel="kline:ECM:MEM_X", value=5, metadata={"module": "ECM"}
        ),
        TelemetryRecord.uprev(stamp, "TIMING_ADVANCE", 12.0, "deg"),
        # The memo's hazard: metadata equal as a Python key, different as JSON.
        TelemetryRecord.obd(stamp, "A", 1, "", metadata={"n": 1}),
        TelemetryRecord.obd(stamp, "B", 1, "", metadata={"n": 1.0}),
        TelemetryRecord.obd(stamp, "C", 1, "", metadata={"n": True}),
        TelemetryRecord.obd(stamp, "D", 1, "", metadata={"n": None}),
        TelemetryRecord.obd(stamp, "E", 1, "", metadata={"n": "1"}),
        # Insertion order must not reach the text; nor an unhashable value.
        TelemetryRecord.obd(stamp, "F", 1, "", metadata={"b": 2, "a": 1, "c": "x"}),
        TelemetryRecord.obd(stamp, "G", 1, "", metadata={"c": "x", "a": 1, "b": 2}),
        TelemetryRecord.obd(stamp, "H", 1, "", metadata={"seq": [1, 2], "a": 1}),  # type: ignore[dict-item]
        TelemetryRecord.obd(stamp, "I", 1, "", metadata={"x": math.nan}),
        TelemetryRecord.obd(stamp, "J", 1, "", metadata={"x": float("inf")}),
        TelemetryRecord.obd(stamp, "K", math.nan if nan_value else 0.5, ""),
        TelemetryRecord.obd(stamp, "L", 10**15, ""),
        TelemetryRecord.obd(stamp, "M", "naïve ✓", "", metadata={"note": "naïve ✓"}),
    ]
    again = [
        TelemetryRecord(
            timestamp=_at(i),
            source=r.source,
            channel=r.channel,
            value=r.value,
            unit=r.unit,
            payload=r.payload,
            metadata=dict(r.metadata),
        )
        for i, r in enumerate(once, start=1)
    ]
    return once + again


def _old_table(records: list[TelemetryRecord], schema: Any) -> Any:
    return pa.Table.from_pylist([r.parquet_row() for r in records], schema=schema)


def test_the_column_build_is_the_from_pylist_table_for_a_mixed_batch() -> None:
    records = _mixed_batch()
    new = records_to_table(records, TELEMETRY_SCHEMA)
    old = _old_table(records, TELEMETRY_SCHEMA)

    assert new.equals(old, check_metadata=True)
    assert new.schema.equals(old.schema, check_metadata=True)
    assert new.schema.names == TELEMETRY_SCHEMA.names
    assert [f.nullable for f in new.schema] == [f.nullable for f in TELEMETRY_SCHEMA]
    assert new.to_pylist() == old.to_pylist()


def test_metadata_json_is_the_sort_keys_dumps_to_the_byte() -> None:
    """The oracle is ``json.dumps`` itself, not ``parquet_row``."""
    records = _mixed_batch()
    new = records_to_table(records, TELEMETRY_SCHEMA)
    expected = [json.dumps(r.metadata, sort_keys=True) for r in records]

    assert new.column("metadata_json").to_pylist() == expected
    assert [r.parquet_row()["metadata_json"] for r in records] == expected
    assert [metadata_json(r.metadata) for r in records] == expected


def test_the_schema_footer_survives_the_column_build() -> None:
    records = _mixed_batch()
    schema = chunk_schema(CanStorage.change_only(5.0), records)
    assert schema.metadata, "a chunk holding a CAN frame carries the storage footer"

    new = records_to_table(records, schema)

    assert new.schema.metadata == schema.metadata
    assert new.equals(_old_table(records, schema), check_metadata=True)


def test_an_empty_batch_is_an_empty_table_of_the_schema() -> None:
    new = records_to_table([], TELEMETRY_SCHEMA)
    assert new.num_rows == 0
    assert new.equals(_old_table([], TELEMETRY_SCHEMA), check_metadata=True)


def test_a_schema_column_the_build_does_not_make_is_null_filled() -> None:
    """``from_pylist`` fills a key a row lacks with null; so does this."""
    records = _mixed_batch()[:5]
    schema = TELEMETRY_SCHEMA.append(pa.field("extra", pa.string()))

    new = records_to_table(records, schema)

    assert new.column("extra").to_pylist() == [None] * 5
    assert new.equals(_old_table(records, schema), check_metadata=True)


def test_a_chunk_written_to_disk_reads_back_as_the_old_chunk(tmp_path: Path) -> None:
    records = _mixed_batch(nan_value=True)
    schema = chunk_schema(CanStorage.change_only(5.0), records)
    new_path, old_path = tmp_path / "new.parquet", tmp_path / "old.parquet"

    pq.write_table(records_to_table(records, schema), new_path, compression="snappy")  # type: ignore[no-untyped-call]
    pq.write_table(_old_table(records, schema), old_path, compression="snappy")  # type: ignore[no-untyped-call]

    assert new_path.read_bytes() == old_path.read_bytes()


@pytest.mark.parametrize(
    "value",
    [True, False, 0, 1, -1, 2**62, 1.5, float("nan"), -0.0, "text", "", None],
)
def test_split_value_sets_exactly_the_columns_parquet_row_always_set(value: Any) -> None:
    number, text, flag = split_value(value)
    row = TelemetryRecord(timestamp=_T0, source="obd", channel="X", value=value).parquet_row()
    assert (row["value_number"], row["value_text"], row["value_bool"]) == (number, text, flag) or (
        isinstance(value, float) and math.isnan(value)
    )
    assert sum(part is not None for part in (number, text, flag)) == (value is not None)
    if isinstance(value, bool):
        assert flag is value
    elif isinstance(value, (int, float)):
        assert number is not None and isinstance(number, float)


def test_the_metadata_memo_never_hands_a_row_another_rows_text() -> None:
    """``True == 1 == 1.0`` as a key; their JSON is ``true``, ``1`` and ``1.0``.
    Whatever order they arrive in, each gets its own."""
    metadata_json.__globals__["_memoized_metadata_json"].cache_clear()
    shapes: list[dict[str, Any]] = [
        {"n": 1},
        {"n": 1.0},
        {"n": True},
        {"n": 1},
        {"n": True},
        {"n": 1.0},
        {"n": None},
        {"n": "1"},
    ]
    for _ in range(2):
        for shape in shapes:
            assert metadata_json(shape) == json.dumps(shape, sort_keys=True)
    assert metadata_json({"n": 1}) == '{"n": 1}'
    assert metadata_json({"n": 1.0}) == '{"n": 1.0}'
    assert metadata_json({"n": True}) == '{"n": true}'


def test_the_metadata_memo_hits_on_repeated_can_metadata_and_is_bounded() -> None:
    memo = metadata_json.__globals__["_memoized_metadata_json"]
    memo.cache_clear()
    frames = [
        TelemetryRecord.can(1_794_000_000.0 + i, 0x100 + i % 5, b"\x00" * 8) for i in range(50)
    ]

    for frame in frames:
        metadata_json(frame.metadata)

    info = memo.cache_info()
    assert info.misses == 5 and info.hits == 45
    assert info.maxsize == METADATA_JSON_CACHE_SIZE
    # Metadata that carries a float is unique per row: it never enters the memo.
    metadata_json({"acquired_at": 1.5, "round_id": 1})
    assert memo.cache_info().currsize == 5


def test_the_memo_stays_within_its_bound_under_unique_integer_metadata() -> None:
    memo = metadata_json.__globals__["_memoized_metadata_json"]
    memo.cache_clear()
    for n in range(METADATA_JSON_CACHE_SIZE + 500):
        assert metadata_json({"seq": n}) == f'{{"seq": {n}}}'
    assert memo.cache_info().currsize == METADATA_JSON_CACHE_SIZE


def test_an_unhashable_metadata_value_is_serialized_not_an_error() -> None:
    value: dict[str, Any] = {"seq": [1, 2, 3], "a": {"b": 1}}
    assert metadata_json(value) == json.dumps(value, sort_keys=True)


def test_parquet_row_and_the_column_build_share_one_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both call :func:`src.telemetry.metadata_json` and
    :func:`src.telemetry.split_value`, so a row stored singly and a row stored in
    a chunk cannot be spelled differently."""
    metadata_seen: list[object] = []
    value_seen: list[object] = []
    real_metadata, real_split = telemetry.metadata_json, telemetry.split_value

    def metadata_spy(metadata: Any) -> str:
        metadata_seen.append(metadata)
        return real_metadata(metadata)

    def split_spy(value: Any) -> Any:
        value_seen.append(value)
        return real_split(value)

    for module in ("src.telemetry", "src.storage.parquet_writer"):
        monkeypatch.setattr(f"{module}.metadata_json", metadata_spy)
        monkeypatch.setattr(f"{module}.split_value", split_spy)
    record = TelemetryRecord.can(1_794_000_000.0, 0x2A0, b"\x00\x01")

    record.parquet_row()
    assert (len(metadata_seen), len(value_seen)) == (1, 1)
    records_to_table([record, record], TELEMETRY_SCHEMA)
    assert (len(metadata_seen), len(value_seen)) == (3, 3)


# -- write_many: the periodic publish once per batch -----------------------


class _Clock:
    def __init__(self) -> None:
        self.now = _T0
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return self.now


def _writer(
    tmp_path: Path, *, max_rows: int = 10_000, clock: _Clock | None = None
) -> ParquetTelemetryWriter:
    return ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 11, 12, 3, 0, tzinfo=UTC),
        max_rows=max_rows,
        flush_interval_seconds=5.0,
        clock=clock,
    )


def _obd_batch(n: int, *, start: int = 0) -> list[TelemetryRecord]:
    base = _T0.timestamp()
    return [TelemetryRecord.obd(base + 0.01 * (start + i), "RPM", 700 + i, "rpm") for i in range(n)]


def test_write_many_publishes_once_per_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _writer(tmp_path)
    calls = 0
    real = writer.publish_due

    def counting() -> Path | None:
        nonlocal calls
        calls += 1
        return real()

    monkeypatch.setattr(writer, "publish_due", counting)

    writer.write_many(_obd_batch(500))
    assert calls == 1
    assert writer.rows_written == 500
    assert len(writer.last_accepted) == 500


def test_write_many_reads_the_clock_once_per_batch_not_per_record(tmp_path: Path) -> None:
    clock = _Clock()
    writer = _writer(tmp_path, clock=clock)
    before = clock.reads

    writer.write_many(_obd_batch(500))

    assert clock.reads - before <= 2, (
        "the interval check is per batch (it was one clock read a row)"
    )


def test_write_many_still_cuts_a_chunk_at_max_rows_inside_a_long_batch(tmp_path: Path) -> None:
    writer = _writer(tmp_path, max_rows=100)

    files = writer.write_many(_obd_batch(250))

    assert [pq.read_table(f).num_rows for f in files] == [100, 100]  # type: ignore[no-untyped-call]
    assert len(writer._buffer) == 50
    assert writer.rows_written == 250


def test_write_many_chunks_like_a_record_at_a_time_writer(tmp_path: Path) -> None:
    batch = _obd_batch(250)
    one_at_a_time = _writer(tmp_path / "single", max_rows=100)
    for record in batch:
        one_at_a_time.write(record)
    one_at_a_time.close()
    together = _writer(tmp_path / "batch", max_rows=100)
    together.write_many(batch)
    together.close()

    def rows(writer: ParquetTelemetryWriter) -> list[list[dict[str, Any]]]:
        return [pq.read_table(f).to_pylist() for f in writer.files_written]  # type: ignore[no-untyped-call]

    assert rows(together) == rows(one_at_a_time)


def test_write_many_flushes_an_aged_buffer_at_the_end_of_the_batch(tmp_path: Path) -> None:
    clock = _Clock()
    writer = _writer(tmp_path, clock=clock)
    writer.write_many(_obd_batch(10))
    assert not writer.files_written

    clock.now = _T0 + timedelta(seconds=6)
    files = writer.write_many(_obd_batch(10, start=10))

    assert len(files) == 1
    assert pq.read_table(files[0]).num_rows == 20  # type: ignore[no-untyped-call]


def test_a_batch_withheld_by_the_arming_gate_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Closed:
        def observe(self, record: TelemetryRecord) -> bool:
            return False

        def reset_for_new_session(self) -> None:
            return None

    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 11, 12, 3, 0, tzinfo=UTC),
        max_rows=2,
        arming_gate=Closed(),
    )

    ticks = 0
    real = writer.publish_due

    def counting() -> Path | None:
        nonlocal ticks
        ticks += 1
        return real()

    monkeypatch.setattr(writer, "publish_due", counting)

    assert writer.write_many(_obd_batch(10)) == []
    assert writer.write_many([]) == []
    assert writer.last_accepted == []
    assert writer.rows_written == 0
    assert ticks == 0
