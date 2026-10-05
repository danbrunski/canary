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
import json
import logging
import os
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.daemon.retention import _sweep_once
from src.storage.retention import FOOTER_LEDGER_VERSION, FooterLedger, scan_sessions

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


# --- the ledger survives a restart (S01.5, 2026-10-05) -----------------------
#
# The in-memory ledger made every tick after the first cheap, but the first
# tick after each start still read every footer: 70.5 s after the 2026-10-04
# cold boot, during a drive whose daemon missed 23 of 2,271 inference ticks.


def _restart_tick(storage: Path, ledger: Path, **overrides: Any) -> FooterLedger:
    """One tick of a freshly started daemon: the ledger as loaded from disk."""
    footers = FooterLedger.load(ledger, storage)
    _tick(storage, footers, **overrides)
    return footers


def test_a_restart_reads_no_footer_of_a_verified_closed_session(
    storage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "state" / "control" / "truck" / "retention_footers.json"
    _restart_tick(storage, ledger)
    assert ledger.exists()

    spy = _IoSpy(monkeypatch)
    _restart_tick(storage, ledger)
    assert spy.parquet_opens == []


def test_an_unchanged_tick_does_not_rewrite_the_ledger(storage: Path, tmp_path: Path) -> None:
    ledger = tmp_path / "retention_footers.json"
    footers = _restart_tick(storage, ledger)
    # The ledger is written by tmp-file + rename, so a rewrite changes the inode.
    inode = ledger.stat().st_ino
    _tick(storage, footers)
    assert ledger.stat().st_ino == inode


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "",
        "[1, 2]",
        '{"version": 2, "sessions_root": "ROOT", "sessions": ["2026-05-10T000000"]}',
    ],
    ids=["corrupt", "empty", "not-a-mapping", "bad-sessions"],
)
def test_an_unusable_ledger_means_every_footer_is_read(
    storage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    ledger = tmp_path / "retention_footers.json"
    ledger.write_text(content.replace("ROOT", str(storage)))
    spy = _IoSpy(monkeypatch)
    _restart_tick(storage, ledger)
    assert len(spy.parquet_opens) == 4 * len(_OLD_SESSIONS)
    # ... and is replaced by a good one.
    spy.reset()
    _restart_tick(storage, ledger)
    assert spy.parquet_opens == []


@pytest.mark.parametrize(
    ("key", "value"),
    [("version", FOOTER_LEDGER_VERSION - 1), ("sessions_root", "/elsewhere/sessions")],
    ids=["old-version", "other-root"],
)
def test_a_ledger_for_another_version_or_root_is_not_trusted(
    storage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: Any
) -> None:
    """A real ledger whose fingerprints all still match: only the version or
    the sessions root differs, and that alone must send every footer to be read."""
    ledger = tmp_path / "retention_footers.json"
    _restart_tick(storage, ledger)
    document = json.loads(ledger.read_text())
    assert set(document["sessions"]) == set(_OLD_SESSIONS)
    document[key] = value
    ledger.write_text(json.dumps(document))

    spy = _IoSpy(monkeypatch)
    _restart_tick(storage, ledger)
    assert len(spy.parquet_opens) == 4 * len(_OLD_SESSIONS)


def test_a_ledger_key_that_is_not_a_session_name_is_ignored(storage: Path, tmp_path: Path) -> None:
    ledger = tmp_path / "retention_footers.json"
    ledger.write_text(
        json.dumps(
            {
                "version": FOOTER_LEDGER_VERSION,
                "sessions_root": str(storage),
                "sessions": {"_archive": "x", "../2026-05-10T000000": "x"},
            }
        )
    )
    assert FooterLedger.load(ledger, storage)._verified == {}


def test_a_ledger_that_cannot_be_written_never_fails_a_tick(
    storage: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    with caplog.at_level(logging.INFO):
        _restart_tick(storage, blocker / "retention_footers.json")
    assert any(r.getMessage().startswith("footer ledger not saved") for r in caplog.records)
    assert any(r.getMessage() == "retention tick" for r in caplog.records)


@pytest.mark.parametrize("change", ["rewritten", "replaced", "touched", "added", "removed"])
def test_a_session_changed_while_the_daemon_was_down_is_read_again(
    storage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    ledger = tmp_path / "retention_footers.json"
    _restart_tick(storage, ledger)
    session = storage / _OLD_SESSIONS[1]
    chunk = session / "telemetry-00002.parquet"
    if change == "rewritten":  # same size, the footer gone, mtime moved on
        stat = chunk.stat()
        chunk.write_bytes(b"PAR1" + b"\x00" * 64 + b"XXXX")
        os.utime(chunk, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    elif change == "replaced":  # renamed into place: same bytes, mtime, size; new inode
        stat = chunk.stat()
        staged = session / "staged.tmp"
        staged.write_bytes(chunk.read_bytes())
        os.utime(staged, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        staged.replace(chunk)
    elif change == "touched":
        stat = chunk.stat()
        os.utime(chunk, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    elif change == "added":
        _write_complete(session / "telemetry-00099.parquet")
    else:
        chunk.unlink()

    spy = _IoSpy(monkeypatch)
    _restart_tick(storage, ledger)
    assert {Path(path).parent.name for path in spy.parquet_opens} == {_OLD_SESSIONS[1]}
    if change == "rewritten":
        assert not chunk.exists()  # the truncated chunk is still caught


def test_the_open_session_is_never_persisted(
    storage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session being recorded is never written to the ledger, so a later
    start never trusts a previous process's word about a session that was
    still growing."""
    ledger = tmp_path / "retention_footers.json"
    active = storage / _OLD_SESSIONS[2]
    footers = FooterLedger.load(ledger, storage)
    # Even had something marked it verified, it is not saved.
    footers.mark_verified(active, "anything")
    _tick(storage, footers, active=active)
    saved = json.loads(ledger.read_text())["sessions"]
    assert set(saved) == set(_OLD_SESSIONS[:2])

    # Next start, recording elsewhere: the previously open session is read.
    spy = _IoSpy(monkeypatch)
    _restart_tick(storage, ledger)
    assert {Path(path).parent.name for path in spy.parquet_opens} == {_OLD_SESSIONS[2]}


def _build_tree(root: Path) -> Path:
    """Identical, deterministic trees for the parity test."""
    sessions = root / "sessions"
    sessions.mkdir(parents=True)
    for day in range(1, 7):
        session = sessions / f"2026-05-{day:02d}T000000"
        session.mkdir()
        for i in range(3):
            path = session / f"telemetry-{i:05d}.parquet"
            path.write_bytes(b"PAR1" + b"\x00" * (300 * 1024) + b"PAR1")
            os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000 + i))
        (session / "session_meta.json").write_text("{}\n")
    return sessions


def _damage(sessions: Path) -> None:
    """The same damage to either tree, after the cached one was primed."""
    # A chunk killed mid-write, renamed into a verified session.
    (sessions / "2026-05-03T000000" / "telemetry-00007.parquet").write_bytes(b"PAR1-cut")
    # A session reduced to a husk: only zero-byte or truncated chunks.
    husk = sessions / "2026-05-04T000000"
    for path in husk.iterdir():
        path.unlink()
    (husk / "telemetry-00000.parquet").write_bytes(b"")
    (husk / "telemetry-00001.parquet").write_bytes(b"PAR1xx")
    # A new, complete chunk in another verified session.
    (sessions / "2026-05-05T000000" / "telemetry-00009.parquet").write_bytes(
        b"PAR1" + b"\x00" * 64 + b"PAR1"
    )


def _surviving(sessions: Path) -> set[str]:
    return {str(path.relative_to(sessions)) for path in sessions.rglob("*") if path.is_file()}


@pytest.mark.parametrize("storage_max_mb", [None, 4])
def test_cached_and_uncached_sweeps_take_the_same_actions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, storage_max_mb: int | None
) -> None:
    """The ledger saves reads, never decisions: from a primed ledger and from
    none, the same tree loses the same chunks and sessions."""
    cached = _build_tree(tmp_path / "cached")
    uncached = _build_tree(tmp_path / "uncached")
    ledger = tmp_path / "retention_footers.json"
    _restart_tick(cached, ledger, storage_max_mb=None)  # prime the ledger
    assert _surviving(cached) == _surviving(uncached)
    _damage(cached)
    _damage(uncached)

    def _actions(sessions: Path, footers: FooterLedger) -> list[tuple[str, Any]]:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="src.daemon.retention"):
            _tick(sessions, footers, storage_max_mb=storage_max_mb)
        acted = []
        for record in caplog.records:
            message = record.getMessage()
            if message == "retention tick":
                continue
            fields = {
                key: [Path(p).name for p in value] if key == "sessions" else value
                for key, value in record.__dict__.items()
                if key in {"count", "removed_sessions", "removed_zero_byte_files", "sessions"}
            }
            acted.append((message, sorted(fields.items())))
        return acted

    cached_actions = _actions(cached, FooterLedger.load(ledger, cached))
    uncached_actions = _actions(uncached, FooterLedger())
    assert cached_actions == uncached_actions
    assert cached_actions  # the damage did call for action
    assert _surviving(cached) == _surviving(uncached)
    assert not (cached / "2026-05-04T000000").exists()
    assert not (cached / "2026-05-03T000000" / "telemetry-00007.parquet").exists()
