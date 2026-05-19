"""Tests for telemetry Parquet storage."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from src.storage import parquet_writer
from src.storage.parquet_writer import (
    ParquetTelemetryWriter,
    delete_expired_sessions,
    emergency_evict_for_free_space,
    enforce_storage_cap,
)
from src.telemetry import TelemetryRecord


def test_writer_flushes_records_to_readable_parquet(tmp_path: Path) -> None:
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
        max_rows=2,
    )

    first = writer.write(TelemetryRecord.obd(1_778_000_000.0, "RPM", 720.0, "rpm"))
    second = writer.write(TelemetryRecord.can(1_778_000_000.1, 0x123, b"\x01\x02"))

    assert first is None
    assert second is not None
    assert second == tmp_path / "2026-05-11T120000" / "telemetry-00000.parquet"

    table = pq.read_table(second)
    rows = table.to_pylist()
    assert len(rows) == 2
    assert rows[0]["source"] == "obd"
    assert rows[0]["channel"] == "RPM"
    assert rows[0]["value_number"] == 720.0
    assert rows[1]["source"] == "can"
    assert rows[1]["payload"] == b"\x01\x02"
    assert not list(tmp_path.glob("*/*.tmp"))


def test_close_flushes_partial_buffer(tmp_path: Path) -> None:
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 5, 11, 12, 1, tzinfo=UTC),
        max_rows=10,
    )
    writer.write(TelemetryRecord.obd(1_778_000_000.0, "SPEED", 30.0, "km/h"))
    path = writer.close()
    assert path is not None
    assert path.exists()


def test_time_based_flush(tmp_path: Path) -> None:
    now = datetime(2026, 5, 11, 12, 2, tzinfo=UTC)

    def clock() -> datetime:
        return now

    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=now,
        max_rows=10,
        flush_interval_seconds=1.0,
        clock=clock,
    )
    writer.write(TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm"))

    now = now + timedelta(seconds=2)
    flushed = writer.write(TelemetryRecord.obd(1_778_000_001.0, "RPM", 701.0, "rpm"))
    assert flushed is not None


def test_writer_rotates_session_when_engine_run_time_resets(tmp_path: Path) -> None:
    first_started = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    second_started = datetime(2026, 5, 11, 12, 10, tzinfo=UTC)
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=first_started,
        max_rows=10,
    )

    writer.write(TelemetryRecord.obd(first_started.timestamp(), "RUN_TIME", 120.0, "s"))
    writer.write(
        TelemetryRecord.obd((first_started + timedelta(seconds=1)).timestamp(), "RPM", 700.0, "rpm")
    )
    writer.write(TelemetryRecord.obd(second_started.timestamp(), "RUN_TIME", 1.0, "s"))
    writer.close()

    files = sorted(tmp_path.glob("*/telemetry-*.parquet"))
    assert files == [
        tmp_path / "2026-05-11T120000" / "telemetry-00000.parquet",
        tmp_path / "2026-05-11T121000" / "telemetry-00000.parquet",
    ]
    first_rows = pq.read_table(files[0]).to_pylist()
    second_rows = pq.read_table(files[1]).to_pylist()
    assert [row["channel"] for row in first_rows] == ["RUN_TIME", "RPM"]
    assert [row["channel"] for row in second_rows] == ["RUN_TIME"]


def test_failed_atomic_write_does_not_publish_final_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 5, 11, 12, 3, tzinfo=UTC),
    )
    writer.write(TelemetryRecord.obd(1_778_000_000.0, "RPM", 720.0, "rpm"))

    def fail_write_table(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("disk disappeared")

    with monkeypatch.context() as patch:
        patch.setattr(parquet_writer._PQ, "write_table", fail_write_table)
        with pytest.raises(RuntimeError, match="disk disappeared"):
            writer.flush()

    assert not list(tmp_path.glob("*/telemetry-*.parquet"))
    path = writer.flush()
    assert path is not None
    assert pq.read_table(path).to_pylist()[0]["channel"] == "RPM"


def test_crash_mid_flush_preserves_prior_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 1.14: a mid-flush crash must leave previously-flushed chunks
    readable and not expose half-written ``.tmp`` files as session data.

    Scenario:
      1. Successful flush #1 produces ``telemetry-00000.parquet``.
      2. Flush #2 crashes after the .tmp file is partially written.
      3. On 'restart', ``discover_sessions`` + ``read_session`` only sees
         the intact chunk; the corrupt .tmp is invisible to the glob.
    """
    from src.preprocessing.loader import discover_sessions, read_session

    session_started = datetime(2026, 5, 11, 12, 4, tzinfo=UTC)
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=session_started,
        max_rows=2,
    )

    # Flush #1: 2 records → triggers the max_rows flush.
    writer.write(TelemetryRecord.obd(1_778_000_000.0, "RPM", 720.0, "rpm"))
    first_flush = writer.write(TelemetryRecord.obd(1_778_000_000.1, "RPM", 721.0, "rpm"))
    assert first_flush is not None
    assert first_flush.exists()

    # Flush #2: queue records and then make the next write_table simulate
    # a partial write by truncating the .tmp file mid-stream and raising.
    writer.write(TelemetryRecord.obd(1_778_000_000.2, "RPM", 722.0, "rpm"))

    def corrupting_write_table(table: object, where: object, **_kwargs: object) -> None:
        # Simulate a partial pyarrow write: produce *some* bytes, then crash.
        Path(str(where)).write_bytes(b"PAR1\x00\x00\x00\x00")  # truncated Parquet
        raise RuntimeError("kernel oops")

    with monkeypatch.context() as patch:
        patch.setattr(parquet_writer._PQ, "write_table", corrupting_write_table)
        with pytest.raises(RuntimeError, match="kernel oops"):
            writer.write(TelemetryRecord.obd(1_778_000_000.3, "RPM", 723.0, "rpm"))

    session_dir = first_flush.parent
    # The intact chunk is still readable — its footer is fine.
    intact_rows = pq.read_table(first_flush).to_pylist()
    assert len(intact_rows) == 2
    assert [r["channel"] for r in intact_rows] == ["RPM", "RPM"]

    # The corrupt .tmp lingers but isn't picked up by the discovery glob
    # (which matches telemetry-*.parquet, not telemetry-*.parquet.tmp).
    leftover_tmp = list(session_dir.glob("*.tmp"))
    assert leftover_tmp, "test setup error: corrupting write didn't leave a .tmp"

    # 'Restart' = a fresh process discovering sessions on this directory.
    discovered = discover_sessions(tmp_path)
    assert discovered == [session_dir]
    long_frame = read_session(session_dir)
    # Exactly the 2 records from flush #1; the would-be flush #2 records are
    # gone (in-memory buffer, lost on crash, which is fine for this contract).
    assert len(long_frame) == 2
    assert list(long_frame["channel"]) == ["RPM", "RPM"]


def test_crash_recovery_with_fresh_writer_appends_new_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a crash, a brand-new writer pointed at the same root must not
    overwrite the surviving chunk — sessions live in dated directories, so
    the new writer naturally gets a fresh dir and the old data is preserved.

    Mirrors the operator path: daemon crashes, systemd restarts it, we
    don't want the next session to clobber the previous one's last good chunk.
    """
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 5, 11, 12, 5, tzinfo=UTC),
        max_rows=2,
    )
    writer.write(TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm"))
    survived = writer.write(TelemetryRecord.obd(1_778_000_000.1, "RPM", 701.0, "rpm"))
    assert survived is not None

    # Simulate a 'crash' by not closing the writer.
    del writer

    # New process / restart:
    fresh = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 5, 11, 12, 6, tzinfo=UTC),
        max_rows=2,
    )
    fresh.write(TelemetryRecord.obd(1_778_000_002.0, "RPM", 800.0, "rpm"))
    fresh.write(TelemetryRecord.obd(1_778_000_002.1, "RPM", 801.0, "rpm"))
    fresh.close()

    # Both session dirs exist; the old one is untouched.
    sessions = sorted(d for d in tmp_path.iterdir() if d.is_dir())
    assert len(sessions) == 2
    assert pq.read_table(survived).to_pylist()[0]["value_number"] == 700.0


def test_delete_expired_sessions_removes_only_old_timestamped_dirs(tmp_path: Path) -> None:
    old = tmp_path / "2026-05-01T000000"
    keep = tmp_path / "2026-05-10T000000"
    scratch = tmp_path / "scratch"
    old.mkdir()
    keep.mkdir()
    scratch.mkdir()

    deleted = delete_expired_sessions(
        tmp_path,
        retention_days=5,
        now=datetime(2026, 5, 11, tzinfo=UTC),
    )
    assert deleted == [old]
    assert not old.exists()
    assert keep.exists()
    assert scratch.exists()


def test_delete_expired_sessions_keeps_pinned_old_sessions(tmp_path: Path) -> None:
    old = tmp_path / "2026-05-01T000000"
    old.mkdir()
    (old / ".canary-pin").write_text('{"pinned": true}\n')

    deleted = delete_expired_sessions(
        tmp_path,
        retention_days=5,
        now=datetime(2026, 5, 11, tzinfo=UTC),
    )

    assert deleted == []
    assert old.exists()


def test_delete_expired_sessions_keeps_protected_old_sessions(tmp_path: Path) -> None:
    old = tmp_path / "2026-05-01T000000"
    protected = tmp_path / "2026-05-02T000000"
    old.mkdir()
    protected.mkdir()

    deleted = delete_expired_sessions(
        tmp_path,
        retention_days=5,
        now=datetime(2026, 5, 11, tzinfo=UTC),
        protect={protected},
    )

    assert deleted == [old]
    assert not old.exists()
    assert protected.exists()


def test_delete_expired_sessions_rejects_negative_retention(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="retention_days"):
        delete_expired_sessions(tmp_path, retention_days=-1)


# ---------------------------------------------------------------------------
# enforce_storage_cap — the byte-bounded ring buffer (Phase 8.2)
# ---------------------------------------------------------------------------


def _make_session(root: Path, name: str, mib: int) -> Path:
    session = root / name
    session.mkdir()
    (session / "telemetry-00000.parquet").write_bytes(b"\0" * (mib * 1024 * 1024))
    return session


def test_enforce_storage_cap_evicts_oldest_first_until_under_cap(tmp_path: Path) -> None:
    oldest = _make_session(tmp_path, "2026-05-01T000000", 1)
    middle = _make_session(tmp_path, "2026-05-02T000000", 1)
    newest = _make_session(tmp_path, "2026-05-03T000000", 1)

    deleted = enforce_storage_cap(tmp_path, max_mb=2)

    assert deleted == [oldest]
    assert not oldest.exists()
    assert middle.exists()
    assert newest.exists()


def test_enforce_storage_cap_noop_when_under_cap(tmp_path: Path) -> None:
    a = _make_session(tmp_path, "2026-05-01T000000", 1)
    b = _make_session(tmp_path, "2026-05-02T000000", 1)

    assert enforce_storage_cap(tmp_path, max_mb=10) == []
    assert a.exists()
    assert b.exists()


def test_enforce_storage_cap_never_evicts_pinned_or_protected(tmp_path: Path) -> None:
    pinned = _make_session(tmp_path, "2026-05-01T000000", 1)
    (pinned / ".canary-pin").write_text('{"pinned": true}\n')
    protected = _make_session(tmp_path, "2026-05-02T000000", 1)
    evictable = _make_session(tmp_path, "2026-05-03T000000", 1)

    deleted = enforce_storage_cap(tmp_path, max_mb=1, protect={protected})

    # Cap is 1 MiB and 3 MiB is present, but only the unpinned, unprotected
    # dir may go — staying over cap is correct when nothing else is evictable.
    assert deleted == [evictable]
    assert pinned.exists()
    assert protected.exists()
    assert not evictable.exists()


def test_enforce_storage_cap_stops_when_only_pinned_remain(tmp_path: Path) -> None:
    a = _make_session(tmp_path, "2026-05-01T000000", 1)
    b = _make_session(tmp_path, "2026-05-02T000000", 1)
    for session in (a, b):
        (session / ".canary-pin").write_text("{}\n")

    assert enforce_storage_cap(tmp_path, max_mb=1) == []
    assert a.exists()
    assert b.exists()


def test_enforce_storage_cap_ignores_non_session_entries(tmp_path: Path) -> None:
    session = _make_session(tmp_path, "2026-05-03T000000", 1)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "blob.bin").write_bytes(b"\0" * (5 * 1024 * 1024))

    # scratch is not a timestamp dir: not measured, not evictable. The only
    # session is under cap, so nothing is removed.
    assert enforce_storage_cap(tmp_path, max_mb=2) == []
    assert session.exists()
    assert scratch.exists()


def test_enforce_storage_cap_rejects_non_positive_cap(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_mb"):
        enforce_storage_cap(tmp_path, max_mb=0)
    with pytest.raises(ValueError, match="max_mb"):
        enforce_storage_cap(tmp_path, max_mb=-1)


def test_enforce_storage_cap_missing_root_is_noop(tmp_path: Path) -> None:
    assert enforce_storage_cap(tmp_path / "nope", max_mb=1) == []


# ---------------------------------------------------------------------------
# emergency_evict_for_free_space — the acting low-space guard (Phase 8.3)
# ---------------------------------------------------------------------------


def _free_by_dir_count(
    root: Path, by_count: dict[int, float], default: float
) -> Callable[[Path], float]:
    """A free-space probe whose result depends on how many dirs remain.

    Models the real filesystem: deleting a session dir raises free space, so
    the guard must re-probe after every delete (not assume a fixed value).
    """

    def probe(_path: Path) -> float:
        remaining = sum(1 for child in root.iterdir() if child.is_dir())
        return by_count.get(remaining, default)

    return probe


def test_emergency_evict_noop_when_space_healthy(tmp_path: Path) -> None:
    a = tmp_path / "2026-05-01T000000"
    b = tmp_path / "2026-05-02T000000"
    a.mkdir()
    b.mkdir()

    deleted = emergency_evict_for_free_space(
        tmp_path, warning_mb=500, free_space_mb=lambda _p: 1000.0
    )

    assert deleted == []
    assert a.exists()
    assert b.exists()


def test_emergency_evict_oldest_first_until_recovered(tmp_path: Path) -> None:
    oldest = tmp_path / "2026-05-01T000000"
    second = tmp_path / "2026-05-02T000000"
    third = tmp_path / "2026-05-03T000000"
    newest = tmp_path / "2026-05-04T000000"
    for session in (oldest, second, third, newest):
        session.mkdir()

    # 4 dirs => 100 MiB free, 3 => 300, 2 => 600 (recovered past the 500 cap).
    probe = _free_by_dir_count(tmp_path, {4: 100.0, 3: 300.0, 2: 600.0}, 9999.0)

    deleted = emergency_evict_for_free_space(tmp_path, warning_mb=500, free_space_mb=probe)

    assert deleted == [oldest, second]  # stopped as soon as free >= warning
    assert not oldest.exists()
    assert not second.exists()
    assert third.exists()
    assert newest.exists()


def test_emergency_evict_skips_pinned_and_protected(tmp_path: Path) -> None:
    oldest = tmp_path / "2026-05-01T000000"
    pinned = tmp_path / "2026-05-02T000000"
    protected = tmp_path / "2026-05-03T000000"
    newest = tmp_path / "2026-05-04T000000"
    for session in (oldest, pinned, protected, newest):
        session.mkdir()
    (pinned / ".canary-pin").write_text("{}\n")

    # Never recovers: the guard is as aggressive as it can be but must still
    # never touch pinned or protected (active) dirs.
    deleted = emergency_evict_for_free_space(
        tmp_path,
        warning_mb=500,
        protect={protected},
        free_space_mb=lambda _p: 0.0,
    )

    assert deleted == [oldest, newest]
    assert pinned.exists()
    assert protected.exists()


def test_emergency_evict_ignores_non_session_entries(tmp_path: Path) -> None:
    session = tmp_path / "2026-05-01T000000"
    session.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "blob.bin").write_bytes(b"\0")

    deleted = emergency_evict_for_free_space(tmp_path, warning_mb=500, free_space_mb=lambda _p: 0.0)

    assert deleted == [session]
    assert scratch.exists()


def test_emergency_evict_rejects_negative_warning(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="warning_mb"):
        emergency_evict_for_free_space(tmp_path, warning_mb=-1)


def test_emergency_evict_missing_root_is_noop(tmp_path: Path) -> None:
    assert emergency_evict_for_free_space(tmp_path / "nope", warning_mb=500) == []
