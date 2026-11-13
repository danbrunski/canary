"""Append-only JSONL sink for anomaly events, rotated daily (Task 5.2).

Mirrors how the OBD/tag sidecars work — durable per line (flushed every write,
so a crash loses at most one in-flight event) and trivially tailable. Files are
named ``anomalies-YYYY-MM-DD.jsonl`` under the configured directory; "daily
rotation" is just a new file when the UTC date rolls over (no rename, so it
stays crash-safe). The dashboard's ``GET /anomalies?since=...`` reads across the
matching files — bounded by filename date, because the history is unbounded
(169 MB on the Pi by 2026-09-23) and the reader runs inside the collecting
daemon (D03.28).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO

from src.alerts.events import AnomalyEvent
from src.timefmt import as_utc, parse_iso_utc

FILENAME_PREFIX = "anomalies-"
FILENAME_SUFFIX = ".jsonl"

# A daily file's name is the UTC date of the events in it, but an event whose
# timestamp carries a non-UTC offset, or failed to parse and was filed under
# "now", can sit a day off. Bounded reads keep one extra day either side of
# the boundary rather than trusting it (D03.28).
FILE_DATE_MARGIN = timedelta(days=1)


def _event_date(event: AnomalyEvent) -> str:
    """The UTC date (YYYY-MM-DD) the event's file should live in."""
    if event.timestamp:
        try:
            ts = datetime.fromisoformat(event.timestamp)
            ts = as_utc(ts)
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
        self._base_directory = Path(directory)
        self.directory = self._base_directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._date: str | None = None
        self._fp: IO[str] | None = None
        self._replay_path: Path | None = None

    @property
    def current_path(self) -> Path | None:
        if self._replay_path is not None:
            return self._replay_path
        return None if self._date is None else event_log_path(self.directory, self._date)

    def log(self, event: AnomalyEvent) -> Path:
        """Append one event; return the file it landed in."""
        if self._replay_path is not None:
            if self._fp is None:
                self._fp = self._replay_path.open("a")
            self._fp.write(event.to_json_line() + "\n")
            self._fp.flush()
            return self._replay_path
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

    def enter_replay_mode(self, replay_id: str) -> Path:
        """Redirect appends to ``<base_dir>/_replay/<replay_id>.jsonl``.

        One file per replay (no daily rotation) so the run is reviewable as
        a unit. The ``_replay/`` subdir is excluded from
        :func:`read_events_dir`'s glob, so feedback/refit jobs that read
        ``<anomalies_dir>/anomalies-*.jsonl`` never see replay events.
        """
        if self._fp is not None:
            self._fp.close()
            self._fp = None
        self._date = None
        replay_dir = self._base_directory / "_replay"
        replay_dir.mkdir(parents=True, exist_ok=True)
        self._replay_path = replay_dir / f"{replay_id}.jsonl"
        return self._replay_path

    def exit_replay_mode(self) -> None:
        """Resume per-day appends under the base directory."""
        if self._fp is not None:
            self._fp.close()
            self._fp = None
        self._replay_path = None
        self._date = None

    def close(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None
            self._date = None
            self._replay_path = None

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


def event_log_file_date(path: Path) -> date | None:
    """The UTC date in an ``anomalies-YYYY-MM-DD.jsonl`` name, else ``None``.

    ``None`` means "cannot be placed in time": bounded reads still open such a
    file rather than silently dropping whatever it holds.
    """
    name = Path(path).name
    if not (name.startswith(FILENAME_PREFIX) and name.endswith(FILENAME_SUFFIX)):
        return None
    stem = name[len(FILENAME_PREFIX) : -len(FILENAME_SUFFIX)]
    try:
        return datetime.strptime(stem, "%Y-%m-%d").date()
    except ValueError:
        return None


def read_events_dir(
    directory: Path,
    *,
    since: datetime | None = None,
    limit: int | None = None,
    keep: Callable[[AnomalyEvent], bool] | None = None,
) -> list[AnomalyEvent]:
    """Read ``anomalies-*.jsonl`` under ``directory``, sorted by timestamp.

    With no arguments every file is read. The bounds exist because the log is
    unbounded history and this runs inside the collecting daemon: one
    ``GET /anomalies?since=`` that parsed all 37 files (169 MB) stalled
    collection for ~30 s on 2026-09-23 (D03.28). Both bounds skip whole files
    by their filename date; a file whose name is not a date is always read.

    ``since``
        Keep events whose *parsed* timestamp is ``>= since`` (naive = UTC;
        unparseable timestamps are dropped). Daily files dated more than
        :data:`FILE_DATE_MARGIN` before ``since``'s UTC date are never opened.
    ``keep``
        Extra per-event predicate, applied before ``limit`` counts, so the
        newest N are the newest N *matching* events.
    ``limit``
        Return only the newest ``limit`` events (still oldest-first). Daily
        files are read newest-first and reading stops once ``limit`` events
        are in hand and one further day of margin has been read.
    """
    since_utc: datetime | None = None
    oldest_file_date: date | None = None
    if since is not None:
        since_utc = as_utc(since)
        oldest_file_date = since_utc.date() - FILE_DATE_MARGIN

    def accept(event: AnomalyEvent) -> bool:
        if since_utc is not None:
            try:
                if parse_iso_utc(event.timestamp) < since_utc:
                    return False
            except ValueError:
                return False
        return keep is None or keep(event)

    dated: list[tuple[date, Path]] = []
    undated: list[Path] = []
    for path in sorted(Path(directory).glob(f"{FILENAME_PREFIX}*{FILENAME_SUFFIX}")):
        file_date = event_log_file_date(path)
        if file_date is None:
            undated.append(path)
        elif oldest_file_date is None or file_date >= oldest_file_date:
            dated.append((file_date, path))

    events: list[AnomalyEvent] = []
    for path in undated:
        events.extend(e for e in read_events(path) if accept(e))

    if limit is not None and limit > 0:
        # Newest day first. Only dated-file events count toward the stop: an
        # undated file's events could be from any time, so they can never
        # stand in for a day not yet read.
        dated.sort(key=lambda item: item[0], reverse=True)
        from_dated = 0
        filled_on: date | None = None
        for file_date, path in dated:
            if filled_on is not None and file_date < filled_on - FILE_DATE_MARGIN:
                break
            batch = [e for e in read_events(path) if accept(e)]
            events.extend(batch)
            from_dated += len(batch)
            if filled_on is None and from_dated >= limit:
                filled_on = file_date
    else:
        for _file_date, path in dated:
            events.extend(e for e in read_events(path) if accept(e))

    events.sort(key=lambda e: e.timestamp)
    if limit is not None and limit > 0:
        events = events[-limit:]
    return events
