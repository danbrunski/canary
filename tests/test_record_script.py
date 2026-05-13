"""Tests for the recording CLI lifecycle."""

from __future__ import annotations

import threading
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


def test_record_cli_continues_when_obd_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ELM327 that won't connect must not abort the CAN capture or crash."""
    bus = FakeBus()
    writer = FakeWriter()

    monkeypatch.setattr(record_script, "bus_factory", lambda _spec: bus)
    monkeypatch.setattr(record_script, "JsonlReadingWriter", lambda _path: writer)
    monkeypatch.setattr(record_script, "record", lambda *_a, **_k: None)

    def fail_obd_factory(*_args: Any, **_kwargs: Any) -> object:
        raise RuntimeError("obd failed")

    monkeypatch.setattr(record_script, "obd_factory", fail_obd_factory)

    rc = record_script.main(
        [
            "--config",
            "canary.example.yaml",
            "--out-dir",
            str(tmp_path),
            "--obd-port",
            "/dev/null",
            "--no-reconnect",  # single best-effort OBD attempt, then give up
        ]
    )

    assert rc == 0
    assert bus.closed is True
    assert writer.closed is True


def test_record_cli_runs_supervised_obd_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --obd-port, OBD runs in a supervised thread alongside the CAN recorder."""
    bus = FakeBus()
    writer = FakeWriter()
    obd_started = threading.Event()

    class FakeObdConn:
        def start(self) -> None:
            obd_started.set()

        def stop(self) -> None:
            pass

        def is_connected(self) -> bool:
            return True

    monkeypatch.setattr(record_script, "bus_factory", lambda _spec: bus)
    monkeypatch.setattr(record_script, "JsonlReadingWriter", lambda _path: writer)
    monkeypatch.setattr(record_script, "obd_factory", lambda _spec, _on_reading: FakeObdConn())

    def fake_record(*_a: Any, **_k: Any) -> None:
        # Stand in for the blocking CAN recorder: hold until OBD is up, then return.
        assert obd_started.wait(timeout=5.0)

    monkeypatch.setattr(record_script, "record", fake_record)

    rc = record_script.main(
        ["--config", "canary.example.yaml", "--out-dir", str(tmp_path), "--obd-port", "/dev/null"]
    )

    assert rc == 0
    assert obd_started.is_set()
    assert bus.closed is True
    assert writer.closed is True


def test_record_cli_cleans_up_without_obd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus()
    monkeypatch.setattr(record_script, "bus_factory", lambda _spec: bus)
    monkeypatch.setattr(record_script, "record", lambda *_a, **_k: None)

    rc = record_script.main(["--config", "canary.example.yaml", "--out-dir", str(tmp_path)])

    assert rc == 0
    assert bus.closed is True
