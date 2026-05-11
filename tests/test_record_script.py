"""Tests for the recording CLI lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts import record as record_script


class FakeBus:
    def __init__(self) -> None:
        self.closed = False

    def shutdown(self) -> None:
        self.closed = True


class FakeWriter:
    def __init__(self) -> None:
        self.closed = False

    def write(self, _reading: object) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_record_cli_cleans_up_when_obd_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = FakeBus()
    writer = FakeWriter()

    monkeypatch.setattr(record_script, "bus_factory", lambda _spec: bus)
    monkeypatch.setattr(record_script, "JsonlReadingWriter", lambda _path: writer)

    def fail_obd_factory(*_args: Any, **_kwargs: Any) -> object:
        raise RuntimeError("obd failed")

    monkeypatch.setattr(record_script, "obd_factory", fail_obd_factory)

    with pytest.raises(RuntimeError, match="obd failed"):
        record_script.main(
            [
                "--config",
                "canary.example.yaml",
                "--out-dir",
                str(tmp_path),
                "--obd-port",
                "/dev/null",
            ]
        )

    assert bus.closed is True
    assert writer.closed is True
