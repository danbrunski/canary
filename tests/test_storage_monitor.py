"""Tests for storage capacity checks."""

from __future__ import annotations

import logging
from collections import namedtuple
from pathlib import Path

from src.storage import monitor


def test_free_space_uses_nearest_existing_parent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    usage = namedtuple("usage", "total used free")

    def fake_disk_usage(path: Path) -> object:
        assert path == tmp_path
        return usage(total=100, used=90, free=10 * monitor.BYTES_PER_MIB)

    monkeypatch.setattr(monitor.shutil, "disk_usage", fake_disk_usage)

    assert monitor.free_space_mb(tmp_path / "missing" / "child") == 10


def test_warn_if_storage_low_logs_threshold(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        monitor.shutil,
        "disk_usage",
        lambda _path: usage(total=100, used=90, free=10 * monitor.BYTES_PER_MIB),
    )

    caplog.set_level(logging.WARNING)
    free_mb = monitor.warn_if_storage_low(tmp_path, warning_mb=500)

    assert free_mb == 10
    assert "storage space below warning threshold" in caplog.text
