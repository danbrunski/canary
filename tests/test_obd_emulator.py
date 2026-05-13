"""Integration tests for the OBD collector against ELM327-emulator (Task 1.5).

These exercise the *real* python-obd Async loop talking to a *real*
ELM327-emulator instance over a pty — the same path the daemon takes,
just routed at a virtual port. Catches regressions the unit-test fakes
in ``test_obd_collector.py`` would miss:

* whether ``obd_factory`` actually opens a connection,
* whether the supported-set probing surfaces real PIDs,
* whether ``supervise_obd_capture`` produces ``ObdReading`` records that
  hit the callback,
* whether unsupported PIDs warn-and-skip instead of crashing.

Skipped cleanly when ``ELM327-emulator`` isn't installed (CI without the
``[emulator]`` extra), so this file never breaks the suite for the
common dev path.

The emulator runs in a thread inside its own context manager; tests
shut it down deterministically so a flaky setup doesn't leak ptys
across runs.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.collection.obd_collector import (
    ObdReading,
    ObdSpec,
    obd_factory,
    resolve_pids,
    supervise_obd_capture,
)
from src.collection.reconnect import ReconnectPolicy

elm = pytest.importorskip(
    "elm",
    reason="ELM327-emulator not installed; run "
    "`.venv/bin/pip install --no-build-isolation 'canary[emulator]'`",
)


# ---------------------------------------------------------------------------
# Fixture: a live emulator on a pty, torn down deterministically
# ---------------------------------------------------------------------------


@pytest.fixture
def emulator_pty() -> Iterator[str]:
    """Spin up ELM327-emulator on a pty, yield the device path."""
    with elm.Elm(batch_mode=True) as e:
        port = e.get_pty()
        # The pty is open and the emulator thread is running; python-obd
        # can connect immediately. The Elm context manager handles cleanup.
        assert port, "emulator did not open a pty"
        yield port


# ---------------------------------------------------------------------------
# obd_factory
# ---------------------------------------------------------------------------


def test_obd_factory_connects_to_emulator(emulator_pty: str) -> None:
    """The factory builds a live ``obd.Async`` against the emulator."""
    readings: list[ObdReading] = []
    spec = ObdSpec(port=emulator_pty, pids=("RPM",))
    conn = obd_factory(spec, on_reading=readings.append)
    try:
        assert conn.is_connected()
    finally:
        conn.stop()


def test_obd_factory_reports_supported_pids_from_emulator(emulator_pty: str) -> None:
    """``supports()`` against the emulator should return real booleans, not crash."""
    import obd

    readings: list[ObdReading] = []
    spec = ObdSpec(port=emulator_pty, pids=("RPM", "SPEED", "COOLANT_TEMP"))
    conn = obd_factory(spec, on_reading=readings.append)
    try:
        for cmd in resolve_pids(spec.pids):
            assert isinstance(conn.supports(cmd), bool)
        # RPM is universally supported in the emulator's default ECU profile
        assert conn.supports(obd.commands.RPM)
    finally:
        conn.stop()


# ---------------------------------------------------------------------------
# Watch + read: end-to-end through the callback
# ---------------------------------------------------------------------------


def test_obd_factory_drives_callback_with_real_pid_readings(emulator_pty: str) -> None:
    """The factory wires watch() → callback so the same path that feeds
    ``JsonlReadingWriter`` / ``queue_reading_callback`` actually produces data."""
    readings: list[ObdReading] = []
    spec = ObdSpec(port=emulator_pty, pids=("RPM", "SPEED"))
    conn = obd_factory(spec, on_reading=readings.append)
    try:
        conn.start()
        # The emulator's default scenario is "idle car" — RPM ≈ 620, SPEED 0.
        # Give the watch loop a couple of seconds to land at least one of each.
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if {r.pid for r in readings} >= {"RPM", "SPEED"}:
                break
            time.sleep(0.05)
    finally:
        conn.stop()

    pids_seen = {r.pid for r in readings}
    assert "RPM" in pids_seen, f"expected RPM reading, got {pids_seen}"
    assert "SPEED" in pids_seen, f"expected SPEED reading, got {pids_seen}"
    rpms = [r.value for r in readings if r.pid == "RPM"]
    assert all(isinstance(v, float) for v in rpms)
    # Idle RPM in the emulator's default profile sits ~620; allow a wide band
    # in case the emulator config changes.
    assert all(0 <= v <= 8000 for v in rpms), f"out-of-range RPMs: {rpms}"


def test_unsupported_pid_warns_and_skips_does_not_crash(emulator_pty: str) -> None:
    """A PID the ECU doesn't claim to support should be silently dropped, per FR-01.

    The behavioral contract: ``obd_factory`` returns a live connection,
    only the supported PIDs end up in the watch loop, and the unsupported
    ones never produce readings (no crash, no callback call). The emulator's
    default profile doesn't claim ``FUEL_RATE`` / ``EVAPORATIVE_PURGE``;
    if the profile changes to claim them, swap to a different proprietary
    PID and flag the change.
    """
    import logging

    readings: list[ObdReading] = []
    spec = ObdSpec(port=emulator_pty, pids=("RPM", "FUEL_RATE", "EVAPORATIVE_PURGE"))

    # Hook the obd_collector logger directly — pytest's caplog can be
    # interfered with by python-obd's import-time logging setup, so we
    # attach our own handler.
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    logger = logging.getLogger("src.collection.obd_collector")
    handler = _Capture(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        conn = obd_factory(spec, on_reading=readings.append)
    finally:
        logger.removeHandler(handler)
    try:
        conn.start()
        # Let the watch loop run; only RPM should produce readings since the
        # other two PIDs were skipped at factory time.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if any(r.pid == "RPM" for r in readings):
                break
            time.sleep(0.05)
    finally:
        conn.stop()

    pids_seen = {r.pid for r in readings}
    assert "RPM" in pids_seen, "supported PID still gets watched"
    assert "FUEL_RATE" not in pids_seen, "unsupported PID must not produce readings"
    assert "EVAPORATIVE_PURGE" not in pids_seen, "unsupported PID must not produce readings"
    # And a warning landed for each unsupported PID we asked for.
    unsupported_warnings = [r for r in captured if "not supported" in r.getMessage()]
    assert len(unsupported_warnings) >= 1, "expected at least one unsupported-PID warning"
    warned_pids = {getattr(r, "pid", None) for r in unsupported_warnings}
    assert warned_pids.intersection({"FUEL_RATE", "EVAPORATIVE_PURGE"})


# ---------------------------------------------------------------------------
# supervise_obd_capture: the daemon's recorded-session path
# ---------------------------------------------------------------------------


def test_supervise_obd_capture_lands_readings_in_callback(emulator_pty: str) -> None:
    """The supervised path runs in a thread; verify it produces real readings."""
    readings: list[ObdReading] = []
    stop = threading.Event()
    spec = ObdSpec(port=emulator_pty, pids=("RPM", "COOLANT_TEMP"))

    thread = threading.Thread(
        target=supervise_obd_capture,
        kwargs={
            "spec": spec,
            "on_reading": readings.append,
            "stop_event": stop,
            "factory": obd_factory,
            "reconnect_policy": ReconnectPolicy(initial_delay_seconds=0.1, max_delay_seconds=0.5),
            "health_poll_seconds": 0.2,
        },
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if {r.pid for r in readings} >= {"RPM", "COOLANT_TEMP"}:
                break
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "supervisor thread did not exit cleanly"

    pids = {r.pid for r in readings}
    assert "RPM" in pids
    assert "COOLANT_TEMP" in pids
    # COOLANT_TEMP in the emulator's idle profile is ~80 °C; allow a wide band.
    coolant = [r.value for r in readings if r.pid == "COOLANT_TEMP"]
    assert all(-40 <= v <= 215 for v in coolant), f"coolant out of range: {coolant}"


# ---------------------------------------------------------------------------
# JSONL writer end-to-end: the same path tools/log_to_parquet.py reads
# ---------------------------------------------------------------------------


def test_emulator_readings_round_trip_through_jsonl(emulator_pty: str, tmp_path: Path) -> None:
    """The supervisor's callback target is JsonlReadingWriter in production.
    Verify the readings serialize cleanly into pids.jsonl."""
    import json

    from src.collection.obd_collector import JsonlReadingWriter

    path = tmp_path / "pids.jsonl"
    writer = JsonlReadingWriter(path)
    stop = threading.Event()
    spec = ObdSpec(port=emulator_pty, pids=("RPM",))

    thread = threading.Thread(
        target=supervise_obd_capture,
        kwargs={
            "spec": spec,
            "on_reading": writer.write,
            "stop_event": stop,
            "factory": obd_factory,
            "reconnect_policy": None,  # single-shot — emulator stays up the whole test
        },
        daemon=True,
    )
    thread.start()
    try:
        # Wait for at least 3 lines on disk
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if path.exists() and sum(1 for _ in path.open()) >= 3:
                break
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(timeout=5.0)
        writer.close()

    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(lines) >= 3
    assert all(set(d) >= {"t", "pid", "value", "unit"} for d in lines)
    assert any(d["pid"] == "RPM" for d in lines)


# ---------------------------------------------------------------------------
# obd_factory ignores Mode 04 (CLEAR_DTC) — read-only enforcement against
# a real adapter, not just unit-test fakes.
# ---------------------------------------------------------------------------


def test_obd_factory_refuses_clear_dtc_against_real_adapter(emulator_pty: str) -> None:
    """The read-only guard (1.4) blocks writes before opening the connection."""
    from src.collection.obd_collector import WriteCommandRejectedError

    with pytest.raises(WriteCommandRejectedError, match="CLEAR_DTC"):
        obd_factory(
            ObdSpec(port=emulator_pty, pids=("RPM", "CLEAR_DTC")),
            on_reading=lambda _r: None,
        )


# ---------------------------------------------------------------------------
# Stable across two back-to-back factory builds (no fixture leak)
# ---------------------------------------------------------------------------


def test_two_factory_builds_in_one_test_do_not_interfere(emulator_pty: str) -> None:
    """Build, connect, stop, build again — the second should still work.

    Catches a class of bugs where a stale watch list or unclosed serial
    breaks the second connection.
    """
    readings_a: list[ObdReading] = []
    readings_b: list[ObdReading] = []

    conn_a = obd_factory(ObdSpec(port=emulator_pty, pids=("RPM",)), readings_a.append)
    try:
        conn_a.start()
        time.sleep(0.5)
    finally:
        conn_a.stop()

    conn_b: Any = obd_factory(ObdSpec(port=emulator_pty, pids=("SPEED",)), readings_b.append)
    try:
        conn_b.start()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not readings_b:
            time.sleep(0.05)
    finally:
        conn_b.stop()

    assert readings_a, "first connection should have produced readings"
    assert readings_b, "second connection should have produced readings"
