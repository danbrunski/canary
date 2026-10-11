"""A slow card's fsync stall must not stop telemetry being accepted.

2026-10-11, a second Pi on a cheap SD card: one chunk's fsync took 12.8 s,
the storage sink stopped draining while it waited, and its 10,000-record queue
dropped ~7,000 records a minute. The daemon's writer now publishes chunks on a
background thread (``publish_in_background``). These tests hold that write
open on purpose and check what keeps working while it is.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from src.daemon.drain import run_storage_sink
from src.daemon.health import HealthMonitor
from src.storage import parquet_writer
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

_REAL_WRITE = parquet_writer._write_table_atomic


class _StalledDisk:
    """``_write_table_atomic`` that waits for :meth:`release` before writing."""

    def __init__(self) -> None:
        self.released = threading.Event()
        self.started = threading.Event()
        self.calls = 0

    def __call__(self, table: Any, path: Path, *, compression: str) -> None:
        self.calls += 1
        self.started.set()
        assert self.released.wait(timeout=10), "test never released the stalled write"
        _REAL_WRITE(table, path, compression=compression)

    def release(self) -> None:
        self.released.set()


@pytest.fixture
def stalled_disk(monkeypatch: pytest.MonkeyPatch) -> Iterator[_StalledDisk]:
    disk = _StalledDisk()
    monkeypatch.setattr(parquet_writer, "_write_table_atomic", disk)
    yield disk
    disk.release()  # never leave a worker thread blocked behind a failed test


def _writer(tmp_path: Path, *, max_rows: int = 2) -> ParquetTelemetryWriter:
    return ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 10, 11, 23, 0, tzinfo=UTC),
        max_rows=max_rows,
        publish_in_background=True,
    )


def _rpm(i: int) -> TelemetryRecord:
    return TelemetryRecord.obd(1_791_760_000.0 + i, "RPM", float(i), "rpm")


def _stored_rpms(session: Path) -> list[float]:
    values: list[float] = []
    for chunk in sorted(session.glob("telemetry-*.parquet")):
        values.extend(row["value_number"] for row in pq.read_table(chunk).to_pylist())
    return values


def test_writes_keep_being_accepted_while_a_chunk_is_stalled(
    tmp_path: Path, stalled_disk: _StalledDisk
) -> None:
    writer = _writer(tmp_path)
    assert writer.write(_rpm(0)) is None
    assert writer.write(_rpm(1)) is None  # chunk 0 handed off, not yet durable
    assert stalled_disk.started.wait(timeout=5)

    # The disk is stuck; an inline writer would block in each of these.
    for i in range(2, 12):
        assert writer.write(_rpm(i)) is None
    assert writer.publish_stats.deferred > 0
    assert writer.files_written == []  # nothing is reported before it is durable

    stalled_disk.release()
    writer.flush()
    assert _stored_rpms(writer.paths.root) == [float(i) for i in range(12)]
    # Chunk 0 held its two rows; the rest waited behind it as one chunk.
    assert [p.name for p in writer.files_written] == [
        "telemetry-00000.parquet",
        "telemetry-00001.parquet",
    ]


def test_a_periodic_write_returns_a_chunk_only_once_it_is_durable(
    tmp_path: Path, stalled_disk: _StalledDisk
) -> None:
    writer = _writer(tmp_path)
    writer.write(_rpm(0))
    writer.write(_rpm(1))
    assert stalled_disk.started.wait(timeout=5)
    stalled_disk.release()
    finished = None
    for i in range(2, 200):
        finished = writer.write(_rpm(i)) or finished
        if finished is not None:
            break
    assert finished == writer.paths.root / "telemetry-00000.parquet"
    assert pq.read_table(finished).num_rows == 2
    writer.close()


def test_flush_waits_for_the_chunk_in_flight(tmp_path: Path, stalled_disk: _StalledDisk) -> None:
    writer = _writer(tmp_path)
    writer.write(_rpm(0))
    writer.write(_rpm(1))
    writer.write(_rpm(2))
    assert stalled_disk.started.wait(timeout=5)
    threading.Timer(0.2, stalled_disk.release).start()

    path = writer.flush()

    # Durable on return: both the stalled chunk and the rest are on disk.
    assert path == writer.paths.root / "telemetry-00001.parquet"
    assert _stored_rpms(writer.paths.root) == [0.0, 1.0, 2.0]
    assert writer.publish_stats.published == 1


def test_rotation_waits_so_no_chunk_lands_in_the_wrong_session(
    tmp_path: Path, stalled_disk: _StalledDisk
) -> None:
    writer = _writer(tmp_path)
    first_session = writer.paths.root
    writer.write(_rpm(0))
    writer.write(_rpm(1))
    writer.write(_rpm(2))
    threading.Timer(0.2, stalled_disk.release).start()

    writer.rotate_session(started_at=datetime(2026, 10, 11, 23, 30, tzinfo=UTC))
    writer.write(_rpm(3))
    writer.close()

    assert _stored_rpms(first_session) == [0.0, 1.0, 2.0]
    assert _stored_rpms(writer.paths.root) == [3.0]


def test_a_full_buffer_waits_for_the_disk(
    tmp_path: Path, stalled_disk: _StalledDisk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(parquet_writer, "BACKGROUND_BUFFER_CHUNKS", 3)
    writer = _writer(tmp_path)
    writer.write(_rpm(0))
    writer.write(_rpm(1))
    assert stalled_disk.started.wait(timeout=5)
    for i in range(2, 7):  # 5 rows buffered: still under the 6-row cap
        writer.write(_rpm(i))
    assert writer.publish_stats.waits == 0

    threading.Timer(0.2, stalled_disk.release).start()
    finished = writer.write(_rpm(7))  # the cap: block on chunk 0, then hand off

    assert finished == writer.paths.root / "telemetry-00000.parquet"
    assert writer.publish_stats.waits == 1
    writer.close()
    assert _stored_rpms(writer.paths.root) == [float(i) for i in range(8)]


def test_a_failed_background_write_is_counted_and_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def failing_once(table: Any, path: Path, *, compression: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(28, "No space left on device")
        _REAL_WRITE(table, path, compression=compression)

    monkeypatch.setattr(parquet_writer, "_write_table_atomic", failing_once)
    writer = _writer(tmp_path)
    writer.write(_rpm(0))
    writer.write(_rpm(1))  # chunk 0: fails in the background
    writer.write(_rpm(2))
    writer.flush()

    stats = writer.publish_stats.snapshot()
    assert stats["publish_errors"] == 1
    assert "No space left" in stats["last_publish_error"]
    # Nothing refused: the failed chunk's rows were retried, in order, ahead
    # of the row that arrived meanwhile.
    assert _stored_rpms(writer.paths.root) == [0.0, 1.0, 2.0]


def test_a_disk_that_keeps_failing_is_not_retried_on_every_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def always_failing(table: Any, path: Path, *, compression: str) -> None:
        nonlocal calls
        calls += 1
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(parquet_writer, "_write_table_atomic", always_failing)
    writer = _writer(tmp_path)  # flush interval 5 s: no retry inside this test
    writer.write(_rpm(0))
    writer.write(_rpm(1))
    deadline = time.monotonic() + 5
    while writer.publish_stats.errors == 0 and time.monotonic() < deadline:
        writer.write(_rpm(2))
    assert writer.publish_stats.errors == 1
    for i in range(500):
        writer.write(_rpm(3 + i))

    assert calls == 1
    assert writer.publish_stats.errors == 1


def test_the_inline_writer_is_unchanged(tmp_path: Path) -> None:
    writer = ParquetTelemetryWriter(
        tmp_path, session_started_at=datetime(2026, 10, 11, 23, 0, tzinfo=UTC), max_rows=2
    )
    writer.write(_rpm(0))
    path = writer.write(_rpm(1))
    assert path is not None and path.exists()  # durable before write() returned
    assert writer.publish_stats.snapshot()["background"] is False


def test_the_daemon_sink_keeps_draining_through_a_stall(
    tmp_path: Path, stalled_disk: _StalledDisk
) -> None:
    """The 2026-10-11 failure, end to end: records keep arriving while the
    disk is stuck, and the sink's small queue never fills."""
    writer = _writer(tmp_path, max_rows=50)
    health = HealthMonitor()

    async def run() -> int:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=100)
        stop = asyncio.Event()
        sink = asyncio.create_task(
            run_storage_sink(queue, writer, stop, poll_interval=0.01, health_monitor=health)
        )
        dropped = 0
        for i in range(1_000):  # 10x the queue, all while chunk 0 is stuck
            try:
                queue.put_nowait(_rpm(i))
            except asyncio.QueueFull:
                dropped += 1
            if i % 20 == 0:
                await asyncio.sleep(0.005)
        assert stalled_disk.calls == 1 and not stalled_disk.released.is_set()
        stalled_disk.release()
        stop.set()
        await sink
        return dropped

    dropped = asyncio.run(run())

    assert dropped == 0
    assert _stored_rpms(writer.paths.root) == [float(i) for i in range(1_000)]
    storage = health.snapshot()["telemetry_sinks"]["storage"]
    assert storage["background"] is True
    assert storage["publishes_deferred"] > 0
