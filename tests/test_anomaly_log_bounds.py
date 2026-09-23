"""Bounded reads of the daily anomaly log (D03.28).

``GET /anomalies?since=14:20Z`` parsed all 37 daily files (169 MB) inside the
collecting daemon before applying ``since`` and stalled collection for ~30 s
mid-drive on 2026-09-23. These pin that a bound skips whole files by their
filename date — observed by recording which files are actually opened — and
that skipping never changes the answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from src.alerts import event_logger
from src.alerts.event_logger import (
    AnomalyEventLogger,
    event_log_file_date,
    event_log_path,
    read_events_dir,
)
from src.alerts.events import AnomalyEvent
from src.dashboard.readers import read_anomalies_since


def _event(timestamp: str, *, score: float = 1.0, source: str = "lstm_autoencoder") -> AnomalyEvent:
    return AnomalyEvent(
        timestamp=timestamp,
        source=source,  # type: ignore[arg-type]
        model_version="2026-09-23",
        score=score,
        operating_mode="city",
    )


def _write(path: Path, *events: AnomalyEvent) -> None:
    """Write events verbatim into ``path`` — bypasses the logger's own filing
    so a test can put an event in a file its timestamp would not choose."""
    path.write_text("".join(e.to_json_line() + "\n" for e in events))


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every file name the directory reader opens."""
    names: list[str] = []
    real = event_logger.read_events

    def spy(path: Path) -> Iterator[AnomalyEvent]:
        names.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(event_logger, "read_events", spy)
    return names


def _log_days(directory: Path) -> None:
    with AnomalyEventLogger(directory) as logger:
        logger.log(_event("2026-05-02T10:00:00Z", score=0.1))
        logger.log(_event("2026-09-19T00:17:00Z", score=0.2))
        logger.log(_event("2026-09-22T23:00:00Z", score=0.3))
        logger.log(_event("2026-09-23T14:00:00Z", score=0.4))
        logger.log(_event("2026-09-23T14:30:00Z", score=0.5))


def test_event_log_file_date_parses_only_dated_names() -> None:
    assert event_log_file_date(Path("anomalies-2026-09-19.jsonl")) == date(2026, 9, 19)
    assert event_log_file_date(Path("anomalies-backup.jsonl")) is None
    assert event_log_file_date(Path("anomalies-2026-09-19.old.jsonl")) is None
    assert event_log_file_date(Path("anomalies-2026-02-30.jsonl")) is None


def test_recent_since_never_opens_an_older_daily_file(tmp_path: Path, opened: list[str]) -> None:
    """The drive's request shape: a same-day ``since`` opens only that day and
    the one-day margin, never the history behind them."""
    _log_days(tmp_path)

    events = read_anomalies_since(tmp_path, since="2026-09-23T14:20:00Z")

    assert [e["score"] for e in events] == [0.5]
    assert sorted(opened) == ["anomalies-2026-09-22.jsonl", "anomalies-2026-09-23.jsonl"]


def test_recent_since_survives_an_unreadable_old_file(tmp_path: Path) -> None:
    """Belt and braces for the spy above: an old day that cannot be parsed
    at all (a directory where a file should be) is simply never touched."""
    _log_days(tmp_path)
    old = event_log_path(tmp_path, "2026-05-02")
    old.unlink()
    old.mkdir()  # opening it would raise IsADirectoryError

    events = read_anomalies_since(tmp_path, since="2026-09-23T14:20:00Z")
    assert [e["score"] for e in events] == [0.5]


def test_since_keeps_exactly_one_day_of_margin(tmp_path: Path, opened: list[str]) -> None:
    """A file dated one day before ``since`` is still read (an event can be
    filed a day off its timestamp); a file two days before is not."""
    _write(event_log_path(tmp_path, "2026-09-21"), _event("2026-09-23T00:40:00Z", score=21.0))
    _write(event_log_path(tmp_path, "2026-09-22"), _event("2026-09-23T00:30:00Z", score=22.0))
    _write(event_log_path(tmp_path, "2026-09-23"), _event("2026-09-23T01:00:00Z", score=23.0))

    events = read_events_dir(tmp_path, since=datetime(2026, 9, 23, 0, 0, tzinfo=UTC))

    assert [e.score for e in events] == [22.0, 23.0]
    assert "anomalies-2026-09-21.jsonl" not in opened
    assert sorted(opened) == ["anomalies-2026-09-22.jsonl", "anomalies-2026-09-23.jsonl"]


def test_since_still_filters_events_inside_kept_files(tmp_path: Path) -> None:
    """File skipping is only a pre-filter: inside a kept file the parsed-
    timestamp comparison is unchanged, including mixed ISO forms and dropping
    unparseable timestamps."""
    _write(
        event_log_path(tmp_path, "2026-09-23"),
        _event("2026-09-23T12:00:00Z", score=1.0),
        _event("2026-09-23 12:02:00+00:00", score=2.0),
        _event("not-a-date", score=3.0),
    )
    events = read_anomalies_since(tmp_path, since="2026-09-23T12:00:30Z")
    assert [e["score"] for e in events] == [2.0]


def test_limit_without_since_matches_full_read(tmp_path: Path, opened: list[str]) -> None:
    """Newest-first reading with an early stop returns exactly the newest N a
    full read would, for every N — and a small N does not open old days."""
    with AnomalyEventLogger(tmp_path) as logger:
        for day in range(1, 11):
            for hour in range(day % 3 + 1):  # 1-3 events a day, uneven
                logger.log(
                    _event(
                        f"2026-09-{day:02d}T{hour + 8:02d}:00:00Z",
                        score=day + hour / 10,
                        source="behavior" if hour % 2 else "lstm_autoencoder",
                    )
                )
    full = read_events_dir(tmp_path)
    assert len(full) > 10
    for n in range(1, len(full) + 3):
        assert read_events_dir(tmp_path, limit=n) == full[-n:], n

    def behavior(event: AnomalyEvent) -> bool:
        return event.source == "behavior"

    full_behavior = [e for e in full if behavior(e)]
    for n in range(1, len(full_behavior) + 2):
        assert read_events_dir(tmp_path, limit=n, keep=behavior) == full_behavior[-n:], n

    opened.clear()
    newest = read_anomalies_since(tmp_path, limit=2)
    assert [e["timestamp"] for e in newest] == [e.timestamp for e in full[-2:]]
    # 09-10 holds 2 events; the margin adds 09-09. Nothing older is opened.
    assert opened == ["anomalies-2026-09-10.jsonl", "anomalies-2026-09-09.jsonl"]


def test_limit_counts_matching_events_through_the_route_reader(tmp_path: Path) -> None:
    """``source`` narrows before ``limit`` counts, so a newer day full of
    other sources does not crowd out the one asked for."""
    with AnomalyEventLogger(tmp_path) as logger:
        logger.log(_event("2026-09-20T10:00:00Z", score=1.0, source="behavior"))
        for minute in range(5):
            logger.log(_event(f"2026-09-23T10:{minute:02d}:00Z", score=9.0))
    events = read_anomalies_since(tmp_path, source="behavior", limit=1)
    assert [e["score"] for e in events] == [1.0]


def test_unparseable_filename_is_always_read(tmp_path: Path, opened: list[str]) -> None:
    """A file whose name is not a date cannot be placed in time, so no bound
    may skip it; its events are still filtered by their own timestamps."""
    _log_days(tmp_path)
    _write(
        tmp_path / "anomalies-backup.jsonl",
        _event("2026-09-23T15:00:00Z", score=9.0),
        _event("2026-05-01T00:00:00Z", score=8.0),
    )

    since = read_anomalies_since(tmp_path, since="2026-09-23T14:20:00Z")
    assert [e["score"] for e in since] == [0.5, 9.0]
    assert "anomalies-backup.jsonl" in opened

    opened.clear()
    newest = read_anomalies_since(tmp_path, limit=1)
    assert [e["score"] for e in newest] == [9.0]
    assert "anomalies-backup.jsonl" in opened

    everything = read_events_dir(tmp_path)
    assert {e.score for e in everything} >= {8.0, 9.0}
