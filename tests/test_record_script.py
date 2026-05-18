"""Tests for the recording CLI lifecycle."""

from __future__ import annotations

import signal
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
    """An enabled OBD capture with zero readings is schema-incomplete."""
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

    assert rc == 1
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

    def fake_record(*args: Any, **_k: Any) -> None:
        # Stand in for the blocking CAN recorder: hold until OBD is up, then return.
        assert obd_started.wait(timeout=5.0)
        args[1].pids.write_text('{"timestamp": 1.0, "channel": "RPM", "value": 700}\n')

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

    rc = record_script.main(
        [
            "--config",
            "canary.example.yaml",
            "--out-dir",
            str(tmp_path),
            "--obd-source",
            "off",
        ]
    )

    assert rc == 0
    assert bus.closed is True


def test_record_cli_restores_signal_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = FakeBus()
    original_sigint = signal.getsignal(signal.SIGINT)
    original_sigterm = signal.getsignal(signal.SIGTERM)

    monkeypatch.setattr(record_script, "bus_factory", lambda _spec: bus)
    monkeypatch.setattr(record_script, "record", lambda *_a, **_k: None)

    rc = record_script.main(
        [
            "--config",
            "canary.example.yaml",
            "--out-dir",
            str(tmp_path),
            "--obd-source",
            "off",
        ]
    )

    assert rc == 0
    assert signal.getsignal(signal.SIGINT) == original_sigint
    assert signal.getsignal(signal.SIGTERM) == original_sigterm


def test_record_cli_runs_canable_obd_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--obd-source canable should use the one-bus CAN+OBD recorder."""
    bus = FakeBus()
    writer = FakeWriter()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(record_script, "bus_factory", lambda _spec: bus)
    monkeypatch.setattr(record_script, "JsonlReadingWriter", lambda _path: writer)

    def fake_record_can_obd(_bus: Any, paths: Any, **kwargs: Any) -> None:
        captured["bus"] = _bus
        captured["spec"] = kwargs["spec"]
        captured["writer"] = kwargs["obd_writer"]
        paths.pids.write_text('{"timestamp": 1.0, "channel": "RPM", "value": 700}\n')

    monkeypatch.setattr(record_script, "record_can_obd", fake_record_can_obd)

    rc = record_script.main(
        [
            "--config",
            "canary.example.yaml",
            "--out-dir",
            str(tmp_path),
            "--obd-source",
            "canable",
            "--obd-pids",
            "RPM",
            "--obd-dids",
            "1101",
            "--obd-did-header",
            "7E0",
        ]
    )

    assert rc == 0
    assert captured["bus"] is bus
    assert captured["spec"].pids == ("RPM",)
    assert captured["spec"].dids == ("1101",)
    assert captured["spec"].did_request_id == 0x7E0
    assert captured["writer"] is writer
    assert bus.closed is True
    assert writer.closed is True


def test_record_cli_passes_dids_to_obd_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--obd-dids should propagate into the ObdSpec the factory sees."""
    bus = FakeBus()
    writer = FakeWriter()
    captured_spec: dict[str, Any] = {}

    class FakeObdConn:
        def start(self) -> None: ...
        def stop(self) -> None: ...
        def is_connected(self) -> bool:
            return True

    def fake_factory(spec: Any, _on_reading: Any) -> FakeObdConn:
        captured_spec["spec"] = spec
        return FakeObdConn()

    monkeypatch.setattr(record_script, "bus_factory", lambda _spec: bus)
    monkeypatch.setattr(record_script, "JsonlReadingWriter", lambda _path: writer)
    monkeypatch.setattr(record_script, "obd_factory", fake_factory)
    def fake_record(*args: Any, **_kwargs: Any) -> None:
        args[1].pids.write_text('{"timestamp": 1.0, "channel": "RPM", "value": 700}\n')

    monkeypatch.setattr(record_script, "record", fake_record)

    rc = record_script.main(
        [
            "--config",
            "canary.example.yaml",
            "--out-dir",
            str(tmp_path),
            "--obd-port",
            "/dev/null",
            "--obd-dids",
            "1101,F190",
            "--obd-did-header",
            "7E1",
        ]
    )
    assert rc == 0
    spec = captured_spec["spec"]
    assert spec.dids == ("1101", "F190")
    assert spec.did_header == "7E1"


def test_record_cli_rejects_malformed_did(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus()
    writer = FakeWriter()
    monkeypatch.setattr(record_script, "bus_factory", lambda _spec: bus)
    monkeypatch.setattr(record_script, "JsonlReadingWriter", lambda _path: writer)
    monkeypatch.setattr(record_script, "obd_factory", lambda _spec, _on: object())
    monkeypatch.setattr(record_script, "record", lambda *_a, **_k: None)

    with pytest.raises(ValueError, match="4-character hex DID"):
        record_script.main(
            [
                "--config",
                "canary.example.yaml",
                "--out-dir",
                str(tmp_path),
                "--obd-port",
                "/dev/null",
                "--obd-dids",
                "ZZZZ",
            ]
        )
