"""Tests for the OBD-II parallel-capture collector.

Real ELM327 testing happens at the bench. These tests cover everything
testable without hardware:
  * ObdReading serialization
  * JsonlReadingWriter file output
  * Unknown-PID validation
  * The python-obd `OBDResponse → callback` adapter (with a fake response)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.collection.obd_collector import (
    JsonlReadingWriter,
    ObdReading,
    ObdSpec,
    _make_callback,
    obd_factory,
    resolve_pids,
)

# ---------------------------------------------------------------------------
# Reading + writer
# ---------------------------------------------------------------------------


def test_obd_reading_serializes_to_json_line():
    r = ObdReading(timestamp=1700000000.5, pid="RPM", value=752.0, unit="rpm")
    parsed = json.loads(r.to_json_line())
    assert parsed == {"t": 1700000000.5, "pid": "RPM", "value": 752.0, "unit": "rpm"}


def test_obd_reading_handles_string_value():
    """VIN and similar PIDs return strings, not floats."""
    r = ObdReading(timestamp=1.0, pid="VIN", value="JN8AR05Y...", unit="")
    assert json.loads(r.to_json_line())["value"] == "JN8AR05Y..."


def test_jsonl_writer_appends_one_line_per_reading(tmp_path: Path):
    path = tmp_path / "pids.jsonl"
    w = JsonlReadingWriter(path)
    try:
        w.write(ObdReading(1.0, "RPM", 800.0, "rpm"))
        w.write(ObdReading(1.5, "COOLANT_TEMP", 95.0, "°C"))
    finally:
        w.close()

    lines = [line for line in path.read_text().splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["pid"] == "RPM"
    assert json.loads(lines[1])["pid"] == "COOLANT_TEMP"


def test_jsonl_writer_creates_parent_dirs(tmp_path: Path):
    path = tmp_path / "nested" / "deep" / "pids.jsonl"
    w = JsonlReadingWriter(path)
    try:
        w.write(ObdReading(1.0, "RPM", 800.0, "rpm"))
    finally:
        w.close()
    assert path.exists()


def test_jsonl_writer_flushes_per_write(tmp_path: Path):
    """A crash mid-session should lose at most the last in-flight line."""
    path = tmp_path / "pids.jsonl"
    w = JsonlReadingWriter(path)
    try:
        w.write(ObdReading(1.0, "RPM", 800.0, "rpm"))
        # Without close(), can we still read what was written?
        assert path.read_text().strip(), "writer should flush per write, not buffer"
    finally:
        w.close()


# ---------------------------------------------------------------------------
# resolve_pids
# ---------------------------------------------------------------------------


def test_resolve_pids_known_commands():
    cmds = resolve_pids(("RPM", "COOLANT_TEMP", "MAF"))
    assert len(cmds) == 3
    # python-obd commands have a `name` attr
    assert all(hasattr(c, "name") for c in cmds)


def test_resolve_pids_unknown_command_raises_with_helpful_message():
    with pytest.raises(ValueError, match="unknown OBD command 'RMP'"):
        resolve_pids(("RPM", "RMP"))  # typo in second item


# ---------------------------------------------------------------------------
# _make_callback: pint Quantity → ObdReading
# ---------------------------------------------------------------------------


@dataclass
class _FakeQuantity:
    """Stand-in for pint.Quantity — only the attrs python-obd uses."""

    magnitude: float
    units: Any


@dataclass
class _FakeResponse:
    """Stand-in for obd.OBDResponse with the methods our adapter touches."""

    value: Any
    null: bool = False

    def is_null(self) -> bool:
        return self.null


def test_callback_unwraps_pint_quantity_into_value_and_unit():
    captured: list[ObdReading] = []
    cb = _make_callback("RPM", captured.append)
    cb(_FakeResponse(value=_FakeQuantity(magnitude=752.0, units="rpm")))
    assert len(captured) == 1
    r = captured[0]
    assert r.pid == "RPM"
    assert r.value == 752.0
    assert r.unit == "rpm"
    assert r.timestamp > 0


def test_callback_drops_dimensionless_unit_string():
    """python-obd uses 'dimensionless' for unitless PIDs (e.g. THROTTLE_POS %)
    but we'd rather store an empty string than that token."""
    captured: list[ObdReading] = []
    cb = _make_callback("ENGINE_LOAD", captured.append)
    cb(_FakeResponse(value=_FakeQuantity(magnitude=42.0, units="dimensionless")))
    assert captured[0].unit == ""


def test_callback_skips_null_responses():
    captured: list[ObdReading] = []
    cb = _make_callback("RPM", captured.append)
    cb(_FakeResponse(value=None, null=True))
    cb(None)  # also None response (rare but possible)
    assert captured == []


def test_callback_passes_through_string_values():
    """VIN-style responses come back as strings, not Quantities."""
    captured: list[ObdReading] = []
    cb = _make_callback("VIN", captured.append)
    cb(_FakeResponse(value="JN8AR05Y8XW123456"))
    assert captured[0].value == "JN8AR05Y8XW123456"
    assert captured[0].unit == ""


# ---------------------------------------------------------------------------
# obd_factory: validation runs before connection (so typos fail loud)
# ---------------------------------------------------------------------------


def test_obd_factory_rejects_unknown_pid_before_opening_connection():
    """Don't burn a connect attempt on a session that's going to fail anyway."""
    spec = ObdSpec(port="/dev/ttyUSB0", pids=("RPM", "BOGUS_PID"))
    with patch("src.collection.obd_collector.obd.Async") as fake_async:
        with pytest.raises(ValueError, match="unknown OBD command 'BOGUS_PID'"):
            obd_factory(spec, on_reading=lambda _r: None)
        fake_async.assert_not_called()


def test_obd_factory_raises_when_adapter_does_not_connect():
    spec = ObdSpec(port="/dev/ttyUSB-fake", pids=("RPM",))
    with patch("src.collection.obd_collector.obd.Async") as fake_async:
        instance = fake_async.return_value
        instance.is_connected.return_value = False
        with pytest.raises(RuntimeError, match="did not connect"):
            obd_factory(spec, on_reading=lambda _r: None)


def test_obd_factory_skips_unsupported_pids_and_watches_supported():
    spec = ObdSpec(port="/dev/ttyUSB0", pids=("RPM", "MAF"))
    with patch("src.collection.obd_collector.obd.Async") as fake_async:
        instance = fake_async.return_value
        instance.is_connected.return_value = True
        # Pretend ECU supports RPM but not MAF
        instance.supports.side_effect = lambda cmd: cmd.name == "RPM"

        conn = obd_factory(spec, on_reading=lambda _r: None)

    # Only one watch call (RPM), MAF was skipped.
    assert instance.watch.call_count == 1
    watched_cmd = instance.watch.call_args.args[0]
    assert watched_cmd.name == "RPM"
    assert conn is instance
