"""S01.5 — a retention tick is cheap, not just off the loop.

``a870887`` moved the hourly tick into a thread, but each tick still walked
every session tree twice (``_session_sizes`` and ``enforce_storage_cap``), a
third time per session to size husks, and opened every parquet in the corpus
to read its last four bytes: 62,091 files on the Pi on 2026-09-22. A session's
files do not change after it closes, so one whose footers were all read once
never needs them read again.
"""

from __future__ import annotations

import io
import logging
import os
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.daemon.retention import _sweep_once
from src.storage.retention import FooterLedger, scan_sessions

_OLD_SESSIONS = ("2026-05-10T000000", "2026-05-10T010000", "2026-05-10T020000")


def _write_complete(path: Path) -> None:
    path.write_bytes(b"PAR1" + b"\x00" * 64 + b"PAR1")


@pytest.fixture
def storage(tmp_path: Path) -> Path:
    root = tmp_path / "sessions"
    root.mkdir()
    for name in _OLD_SESSIONS:
        session = root / name
        session.mkdir()
        for i in range(4):
            _write_complete(session / f"telemetry-{i:05d}.parquet")
        (session / "session_meta.json").write_text("{}\n")
    # Not a session: never walked, never counted.
    archive = root / "_archive" / "2026-01-01T000000"
    archive.mkdir(parents=True)
    _write_complete(archive / "telemetry-00000.parquet")
    return root


class _IoSpy:
    """Counts parquet opens and directory listings made during a tick."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.parquet_opens: list[str] = []
        self.listings: Counter[str] = Counter()
        real_open = io.open
        real_scandir = os.scandir
        real_listdir = os.listdir

        def _open(file: Any, *args: Any, **kwargs: Any) -> Any:
            if str(file).endswith(".parquet"):
                self.parquet_opens.append(str(file))
            return real_open(file, *args, **kwargs)

        def _scandir(path: Any = ".") -> Iterator[os.DirEntry[str]]:
            self.listings[str(path)] += 1
            return real_scandir(path)

        def _listdir(path: Any = ".") -> list[str]:
            self.listings[str(path)] += 1
            return real_listdir(path)

        monkeypatch.setattr(io, "open", _open)
        monkeypatch.setattr("builtins.open", _open)
        monkeypatch.setattr(os, "scandir", _scandir)
        monkeypatch.setattr(os, "listdir", _listdir)

    def reset(self) -> None:
        self.parquet_opens.clear()
        self.listings.clear()


def _tick(storage: Path, footers: FooterLedger, **overrides: Any) -> None:
    kwargs: dict[str, Any] = {
        "active": storage / "2026-09-22T000000",
        "active_replay": None,
        "storage_max_mb": 1024,
        "storage_warning_mb": None,
        "replay_retention_days": 7,
        "catalog_db": None,
        "footers": footers,
    }
    kwargs.update(overrides)
    _sweep_once(storage, 36500, **kwargs)


def test_second_tick_over_an_unchanged_tree_opens_no_parquet(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    footers = FooterLedger()
    spy = _IoSpy(monkeypatch)

    _tick(storage, footers)
    # The first tick reads every footer: nothing has been verified yet.
    assert len(spy.parquet_opens) == 4 * len(_OLD_SESSIONS)

    spy.reset()
    _tick(storage, footers)
    assert spy.parquet_opens == []


def test_a_tick_lists_each_session_once(storage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One walk per tick feeds the sizes, the husk check and the byte cap."""
    spy = _IoSpy(monkeypatch)
    _tick(storage, FooterLedger())
    per_session = {name: spy.listings[str(storage / name)] for name in _OLD_SESSIONS}
    assert per_session == dict.fromkeys(_OLD_SESSIONS, 1)
    assert not [path for path in spy.listings if "_archive" in path]


def test_a_changed_session_is_verified_again(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    footers = FooterLedger()
    _tick(storage, footers)
    session = storage / _OLD_SESSIONS[0]
    truncated = session / "telemetry-00009.parquet"
    truncated.write_bytes(b"PAR1\x00\x00garbage-no-footer")

    spy = _IoSpy(monkeypatch)
    _tick(storage, footers)
    # Only the session that changed is re-read, and its truncated chunk goes.
    assert {Path(path).parent.name for path in spy.parquet_opens} == {_OLD_SESSIONS[0]}
    assert not truncated.exists()
    assert len(list(session.glob("*.parquet"))) == 4

    spy.reset()
    _tick(storage, footers)
    assert spy.parquet_opens == []


def test_tick_logs_its_duration_and_the_files_it_touched(
    storage: Path, caplog: pytest.LogCaptureFixture
) -> None:
    footers = FooterLedger()
    with caplog.at_level(logging.INFO, logger="src.daemon.retention"):
        _tick(storage, footers)
        _tick(storage, footers)
    ticks = [record for record in caplog.records if record.getMessage() == "retention tick"]
    assert len(ticks) == 2
    first, second = (record.__dict__ for record in ticks)
    # 4 parquet + 1 sidecar per session, the archive excluded.
    assert first["files_stat"] == second["files_stat"] == 5 * len(_OLD_SESSIONS)
    assert first["footers_read"] == 4 * len(_OLD_SESSIONS)
    assert second["footers_read"] == 0
    assert first["sessions"] == len(_OLD_SESSIONS)
    assert isinstance(first["duration_ms"], float)


def test_scan_sizes_match_a_full_walk(storage: Path) -> None:
    scans = scan_sessions(storage)
    assert set(scans) == {storage / name for name in _OLD_SESSIONS}
    for path, scan in scans.items():
        walked = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        assert scan.total_bytes == walked
        assert len(scan.parquet_sizes) == 4


def test_byte_cap_still_evicts_oldest_first_from_the_scan(storage: Path) -> None:
    big = storage / _OLD_SESSIONS[0] / "telemetry-00100.parquet"
    big.write_bytes(b"PAR1" + b"\x00" * (2 * 1024 * 1024) + b"PAR1")
    _tick(storage, FooterLedger(), storage_max_mb=1)
    assert not (storage / _OLD_SESSIONS[0]).exists()
    assert (storage / _OLD_SESSIONS[1]).exists()
