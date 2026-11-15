"""``src.atomic_write``: the one tmp + rename (+ fsync) writer (R23.13)."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from src.atomic_write import (
    atomic_write_bytes,
    atomic_write_text,
    fsync_dir,
    publish_atomic,
    temp_path,
)
from src.paths import REPO_ROOT

SRC = REPO_ROOT / "src"


def _record_fsyncs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ``os.fsync`` in the helper to log what each fd points at, in order."""
    events: list[str] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        target = Path(os.readlink(f"/proc/self/fd/{fd}"))
        events.append("dir" if target.is_dir() else f"file:{target.name}")
        real_fsync(fd)

    real_replace = Path.replace

    def replace(self: Path, target: str | os.PathLike[str]) -> Path:
        events.append("replace")
        return real_replace(self, target)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(Path, "replace", replace)
    return events


def test_text_write_is_atomic_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("old")
    atomic_write_text(target, "new ✓")
    assert target.read_text(encoding="utf-8") == "new ✓"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


def test_default_temp_name_is_fixed_and_unique_is_hidden_and_distinct(tmp_path: Path) -> None:
    target = tmp_path / "x.bin"
    assert temp_path(target) == tmp_path / "x.bin.tmp"
    assert temp_path(target, hidden=True) == tmp_path / ".x.bin.tmp"
    first, second = temp_path(target, unique=True), temp_path(target, unique=True)
    assert first != second
    for name in (first.name, second.name):
        assert name.startswith(f".x.bin.{os.getpid()}.")
        assert name.endswith(".tmp")
    assert first.parent == tmp_path


def test_durable_write_fsyncs_file_before_rename_and_directory_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _record_fsyncs(monkeypatch)
    atomic_write_bytes(tmp_path / "a.bin", b"\x00\x01", durable=True)
    assert events == ["file:a.bin.tmp", "replace", "dir"]
    assert (tmp_path / "a.bin").read_bytes() == b"\x00\x01"


def test_non_durable_write_never_fsyncs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = _record_fsyncs(monkeypatch)
    atomic_write_bytes(tmp_path / "a.bin", b"x", unique_tmp=True)
    assert events == ["replace"]


def test_publish_atomic_durable_orders_fsyncs_around_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp = temp_path(tmp_path / "chunk.parquet")
    tmp.write_bytes(b"PAR1")
    events = _record_fsyncs(monkeypatch)
    publish_atomic(tmp, tmp_path / "chunk.parquet")
    assert events == ["file:chunk.parquet.tmp", "replace", "dir"]
    assert not tmp.exists()


def test_fsync_failure_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(fd: int) -> None:
        raise OSError(5, "EIO")

    monkeypatch.setattr(os, "fsync", broken)
    atomic_write_text(tmp_path / "a.txt", "kept", durable=True)
    fsync_dir(tmp_path)
    assert (tmp_path / "a.txt").read_text() == "kept"
    assert "fsync failed" in caplog.text


def test_strict_fsync_raises_removes_the_temp_and_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "devices.json"
    target.write_text("old")

    def broken(fd: int) -> None:
        raise OSError(5, "EIO")

    monkeypatch.setattr(os, "fsync", broken)
    for kwargs in ({}, {"hidden_tmp": True}, {"unique_tmp": True}):
        with pytest.raises(OSError, match="EIO"):
            atomic_write_text(target, "new", durable=True, strict_fsync=True, **kwargs)
        assert target.read_text() == "old"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["devices.json"]
    with pytest.raises(OSError, match="EIO"):
        fsync_dir(tmp_path, strict_fsync=True)
    tmp = temp_path(target)
    tmp.write_text("x")
    with pytest.raises(OSError, match="EIO"):
        publish_atomic(tmp, target, strict_fsync=True)
    assert target.read_text() == "old"


def test_strict_fsync_without_durable_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="durable"):
        atomic_write_bytes(tmp_path / "a", b"x", strict_fsync=True)


def test_mode_is_applied_to_the_temp_file_before_any_byte_lands(tmp_path: Path) -> None:
    target = tmp_path / "secret"
    # A leftover temp with loose bits must not lend them to the new file.
    stale = temp_path(target)
    stale.write_text("stale")
    stale.chmod(0o644)
    atomic_write_text(target, "psk", mode=0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text() == "psk"


def test_failed_write_removes_its_temp_and_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.json"
    target.write_text("old")

    def boom(self: Path, target: str | os.PathLike[str]) -> Path:
        raise OSError("rename refused")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="rename refused"):
        atomic_write_text(target, "new", unique_tmp=True)
    assert target.read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.json"]


def test_mkdir_creates_the_parent(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "er" / "a.bin"
    atomic_write_bytes(target, b"1", mkdir=True, durable=True)
    assert target.read_bytes() == b"1"


def test_concurrent_unique_writers_never_share_a_temp(tmp_path: Path) -> None:
    target = tmp_path / "img.bin"
    errors: list[BaseException] = []

    def write(n: int) -> None:
        try:
            for _ in range(20):
                atomic_write_bytes(target, bytes([n]) * 4096, unique_tmp=True)
        except BaseException as exc:  # noqa: BLE001 - collected for the assert
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    data = target.read_bytes()
    assert len(data) == 4096 and len(set(data)) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["img.bin"]


def test_os_fsync_lives_only_in_atomic_write() -> None:
    """R23.13: four files each carried their own fsync sequence. One now does."""
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "os.fsync(" in path.read_text(encoding="utf-8") and path.name != "atomic_write.py"
    ]
    assert offenders == []
