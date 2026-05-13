"""Append-only JSONL sink for anomaly events, rotated daily (Task 5.2).

Mirrors how the OBD/tag sidecars work — durable per line (flushed every write,
so a crash loses at most one in-flight event) and trivially tailable. Files are
named ``anomalies-YYYY-MM-DD.jsonl`` under the configured directory; "daily
rotation" is just a new file when the UTC date rolls over (no rename, so it
stays crash-safe). The dashboard's ``GET /anomalies?since=...`` reads across the
matching files.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from src.alerts.events import AnomalyEvent

FILENAME_PREFIX = "anomalies-"
FILENAME_SUFFIX = ".jsonl"


def _event_date(event: AnomalyEvent) -> str:
    """The UTC date (YYYY-MM-DD) the event's file should live in."""
    if event.timestamp:
        try:
            ts = datetime.fromisoformat(event.timestamp)
            ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
            return ts.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.now(UTC).strftime("%Y-%m-%d")


def event_log_path(directory: Path, date: str) -> Path:
    """Path of the anomaly log file for a given ``YYYY-MM-DD`` date."""
    return Path(directory) / f"{FILENAME_PREFIX}{date}{FILENAME_SUFFIX}"


class AnomalyEventLogger:
    """Writes :class:`AnomalyEvent` records to per-day JSONL files.

    Keeps the current day's file handle open; rolls to a new file when an
    event's date differs from the open one. Not thread-safe — wrap with a lock
    if multiple producers share an instance.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._date: str | None = None
        self._fp: IO[str] | None = None

    @property
    def current_path(self) -> Path | None:
        return None if self._date is None else event_log_path(self.directory, self._date)

    def log(self, event: AnomalyEvent) -> Path:
        """Append one event; return the file it landed in."""
        date = _event_date(event)
        if date != self._date or self._fp is None:
            self._roll_to(date)
        assert self._fp is not None
        self._fp.write(event.to_json_line() + "\n")
        self._fp.flush()
        return event_log_path(self.directory, date)

    def _roll_to(self, date: str) -> None:
        if self._fp is not None:
            self._fp.close()
        self._fp = event_log_path(self.directory, date).open("a")
        self._date = date

    def close(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None
            self._date = None

    def __enter__(self) -> AnomalyEventLogger:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def read_events(path: Path) -> Iterator[AnomalyEvent]:
    """Yield events from one JSONL file (skipping blank/malformed lines)."""
    with Path(path).open() as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                yield AnomalyEvent.from_dict(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue


def read_events_dir(directory: Path) -> list[AnomalyEvent]:
    """Read every ``anomalies-*.jsonl`` under ``directory``, sorted by timestamp."""
    events: list[AnomalyEvent] = []
    for path in sorted(Path(directory).glob(f"{FILENAME_PREFIX}*{FILENAME_SUFFIX}")):
        events.extend(read_events(path))
    events.sort(key=lambda e: e.timestamp)
    return events
