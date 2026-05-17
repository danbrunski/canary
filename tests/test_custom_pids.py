"""Tests for custom PIDs + the sandboxed formula evaluator (Task 6.8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.collection.custom_pids import (
    CustomPid,
    CustomPidSet,
    FormulaError,
    custom_pids_to_torque_csv,
    evaluate_custom_pid,
    evaluate_formula,
    load_custom_pids,
    save_custom_pids,
    torque_csv_to_custom_pids,
)

# ---------------------------------------------------------------------------
# Formula evaluation
# ---------------------------------------------------------------------------


def test_basic_byte_formula() -> None:
    # Classic RPM formula: (A*256 + B) / 4
    assert evaluate_formula("(A*256 + B) / 4", bytes([0x0B, 0xB8])) == 750.0


def test_bytes_length_and_higher_bytes() -> None:
    assert evaluate_formula("bytes", b"\x01\x02\x03") == 3.0
    assert evaluate_formula("C + D", bytes([1, 2, 3, 4])) == 7.0


def test_signed_helper_and_functions() -> None:
    assert evaluate_formula("signed(A, 8)", bytes([0xFF])) == -1.0
    assert evaluate_formula("max(A, B)", bytes([3, 9])) == 9.0
    assert evaluate_formula("abs(signed(A,8))", bytes([0xFE])) == 2.0


def test_ternary_and_compare() -> None:
    assert evaluate_formula("A if A > 10 else 0", bytes([5])) == 0.0
    assert evaluate_formula("A if A > 10 else 0", bytes([20])) == 20.0


def test_short_payload_returns_none_not_error() -> None:
    # References B but payload has only one byte.
    assert evaluate_formula("A*256 + B", bytes([0x10])) is None


def test_divide_by_zero_returns_none() -> None:
    assert evaluate_formula("A / (B - B)", bytes([10, 5])) is None


@pytest.mark.parametrize(
    "evil",
    [
        "__import__('os').system('echo hi')",
        "().__class__.__bases__",
        "A.__class__",
        "[x for x in range(3)]",
        "(lambda: 1)()",
        "open('/etc/passwd')",
        "A; B",
        "exec('1')",
    ],
)
def test_unsafe_formulas_are_rejected(evil: str) -> None:
    with pytest.raises(FormulaError):
        evaluate_formula(evil, b"\x01\x02")


# ---------------------------------------------------------------------------
# CustomPid model + read-only guard
# ---------------------------------------------------------------------------


def test_write_mode_rejected() -> None:
    with pytest.raises(ValueError, match="not a read-only service"):
        CustomPid(name="X", display_name="X", mode=0x04, pid=0x00, formula="A")


def test_read_only_modes_accepted() -> None:
    for mode in (0x01, 0x09, 0x22):
        CustomPid(name=f"M{mode}", display_name="x", mode=mode, pid=0x10, formula="A")


def test_bad_formula_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        CustomPid(name="X", display_name="X", mode=0x22, pid=0x1234, formula="A.__class__")


def test_custom_pid_set_rejects_duplicate_names() -> None:
    a = CustomPid(name="DUP", display_name="a", mode=0x01, pid=1, formula="A")
    b = CustomPid(name="DUP", display_name="b", mode=0x01, pid=2, formula="B")
    with pytest.raises(ValueError, match="duplicate"):
        CustomPidSet(custom_pids=[a, b])


def test_evaluate_custom_pid_returns_value_unit() -> None:
    pid = CustomPid(
        name="BOOST",
        display_name="Boost",
        mode=0x22,
        pid=0x1234,
        formula="A - 101",
        unit="kPa",
        minimum=-100,
        maximum=200,
    )
    # In-range
    assert evaluate_custom_pid(pid, bytes([150])) == (49.0, "kPa")
    # Out-of-range is still returned (advisory bounds, not a filter).
    assert evaluate_custom_pid(pid, bytes([255])) == (154.0, "kPa")
    # Short payload → None
    assert evaluate_custom_pid(pid, b"") is None


# ---------------------------------------------------------------------------
# YAML persistence
# ---------------------------------------------------------------------------


def test_yaml_round_trip(tmp_path: Path) -> None:
    pid = CustomPid(
        name="OIL_PRESS",
        display_name="Oil Pressure",
        mode=0x22,
        pid=0x0F40,
        formula="(A*256+B)/100",
        unit="psi",
        header=0x7E0,
        poll_tier="fast",
    )
    path = tmp_path / "custom_pids.yaml"
    save_custom_pids(CustomPidSet(custom_pids=[pid]), path)
    loaded = load_custom_pids(path)
    assert loaded.schema_version == 1
    assert loaded.custom_pids[0].name == "OIL_PRESS"
    assert loaded.custom_pids[0].header == 0x7E0
    assert load_custom_pids(tmp_path / "missing.yaml").custom_pids == []


# ---------------------------------------------------------------------------
# Torque CSV interop
# ---------------------------------------------------------------------------


def test_torque_csv_import_accept_skip_and_warn() -> None:
    csv_text = (
        "Name,ShortName,ModeAndPID,Equation,Min Value,Max Value,Units,Header\n"
        "Oil Temp,OILT,221154,A-40,-40,150,C,7E0\n"
        "Trans Temp,TFT,01051,A-40,,,C,\n"  # bad ModeAndPID (odd length)
        "Evil,EV,220001,A.__class__,,,,\n"  # unsafe formula
        "Clear,CLR,0400,A,,,,\n"  # write mode 0x04 — must be rejected
    )
    accepted, warnings = torque_csv_to_custom_pids(csv_text)
    assert [p.name for p in accepted] == ["OILT"]
    assert accepted[0].mode == 0x22 and accepted[0].pid == 0x1154
    assert accepted[0].header == 0x7E0
    assert len(warnings) == 3
    assert any("0x04" in w or "read-only" in w for w in warnings)


def test_torque_csv_export_round_trips() -> None:
    pid = CustomPid(
        name="RPM2",
        display_name="Engine RPM",
        mode=0x01,
        pid=0x0C,
        formula="(A*256+B)/4",
        unit="rpm",
    )
    csv_text = custom_pids_to_torque_csv([pid])
    accepted, warnings = torque_csv_to_custom_pids(csv_text)
    assert not warnings
    assert accepted[0].formula == "(A*256+B)/4"
    assert accepted[0].mode == 0x01
    assert accepted[0].pid == 0x0C
