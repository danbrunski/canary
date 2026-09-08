from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar

from src.collection.capture_profiles import CaptureProfile
from src.session import SessionPaths
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

_T = TypeVar("_T")


class AsyncParquetTelemetryWriter:
    """Async, serialized facade over :class:`ParquetTelemetryWriter`.

    Parquet writes and flushes can spend meaningful time in PyArrow/fsync. The
    daemon uses this adapter so those calls run in a worker thread, while replay
    mode transitions share the same lock and cannot race a write in progress.
    """

    def __init__(self, writer: ParquetTelemetryWriter) -> None:
        self._writer = writer
        self._lock = asyncio.Lock()

    @property
    def wrapped(self) -> ParquetTelemetryWriter:
        return self._writer

    @property
    def paths(self) -> SessionPaths:
        return self._writer.paths

    @property
    def files_written(self) -> list[Path]:
        return self._writer.files_written

    @property
    def session_paths(self) -> list[SessionPaths]:
        return self._writer.session_paths

    async def write(self, record: TelemetryRecord) -> Path | None:
        return await self._call(self._writer.write, record)

    async def write_many(self, records: Iterable[TelemetryRecord]) -> list[Path]:
        """Persist several records under one lock/thread hop (one tick's worth
        of derived channels, e.g.) instead of one ``write`` call each."""
        return await self._call(self._writer.write_many, list(records))

    async def flush(self) -> Path | None:
        return await self._call(self._writer.flush)

    async def close(self) -> Path | None:
        return await self._call(self._writer.close)

    async def flush_due(self) -> bool:
        async with self._lock:
            return bool(self._writer.flush_due())

    async def enter_replay_mode(self, replay_id: str) -> Path | None:
        return await self._call(self._writer.enter_replay_mode, replay_id)

    async def exit_replay_mode(self) -> Path | None:
        return await self._call(self._writer.exit_replay_mode)

    async def enter_tuning_mode(self, tuning_id: str) -> Path | None:
        return await self._call(self._writer.enter_tuning_mode, tuning_id)

    async def exit_tuning_mode(self) -> Path | None:
        return await self._call(self._writer.exit_tuning_mode)

    async def enter_capture_mode(
        self,
        profile: CaptureProfile,
        capture_id: str,
        tune_revision: str | None = None,
        resolved_polling_plan: dict[str, Any] | None = None,
    ) -> Path | None:
        return await self._call(
            self._writer.enter_capture_mode,
            profile,
            capture_id,
            tune_revision,
            resolved_polling_plan,
        )

    @property
    def rows_written(self) -> int:
        """Rows accepted into the current session (see ParquetTelemetryWriter)."""
        return self._writer.rows_written

    async def exit_capture_mode(self) -> Path | None:
        return await self._call(self._writer.exit_capture_mode)

    async def _call(self, func: Callable[..., _T], *args: object) -> _T:
        async with self._lock:
            return await asyncio.to_thread(func, *args)


def ensure_async_writer(
    writer: ParquetTelemetryWriter | AsyncParquetTelemetryWriter,
) -> AsyncParquetTelemetryWriter:
    if isinstance(writer, AsyncParquetTelemetryWriter):
        return writer
    return AsyncParquetTelemetryWriter(writer)


__all__ = ["AsyncParquetTelemetryWriter", "ensure_async_writer"]
