"""Tests for scripts/timing_compare.py.

The behaviours worth pinning are the ones that stop the tool crying wolf. It
is used to decide whether to advance ignition timing further, so a false
"took cleanly" is the dangerous failure and a false "RETARD" trains the
operator to ignore it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "timing_compare", REPO_ROOT / "scripts" / "timing_compare.py"
)
assert _spec and _spec.loader
timing_compare = importlib.util.module_from_spec(_spec)
sys.modules["timing_compare"] = timing_compare
_spec.loader.exec_module(timing_compare)


def _run(label: str, rows: list[tuple[float, float, float]], air_temp: float = 40.0):
    """rows: (timing, rpm, load)."""
    df = pd.DataFrame(rows, columns=["timing", "rpm", "load"])
    df["air_temp"] = air_temp
    return timing_compare.Run(label, df, "test")


def _cell(timing: float, n: int = 20, rpm: float = 1200.0, load: float = 20.0):
    return [(timing, rpm, load)] * n


def test_load_floor_drops_decel_cells() -> None:
    """Below the floor, decel/overrun swings timing for non-calibration
    reasons — measured sd of 4-8 deg. Those rows must not reach a verdict."""
    df = pd.DataFrame(
        [(20.0, 1200.0, 5.0), (20.0, 1200.0, 40.0)], columns=["timing", "rpm", "load"]
    )
    df["air_temp"] = 40.0
    kept = timing_compare._cells(df, timing_compare.DEFAULT_LOAD_FLOOR)
    assert len(kept) == 1
    assert kept.iloc[0]["load"] == 40.0


def test_practical_floor_suppresses_trivially_small_deltas(capsys) -> None:
    """The regression this exists for: with large n the standard error shrinks
    until a 1.0 deg difference clears any sigma threshold, so two logs of the
    SAME calibration light up as retard everywhere. The tuning increment is
    1-2 deg, so anything under the floor is not attributable to a change."""
    before = _run("b", _cell(20.0, n=500))
    after = _run("a", _cell(19.0, n=500))  # -1.0 deg, far below the 1.5 floor

    timing_compare.compare(before, after, load_floor=15.0, sigma=2.0, iat_tol=8.0, min_delta=1.5)
    out = capsys.readouterr().out
    assert "no change" in out
    assert "RETARD" not in out


def test_real_retard_is_flagged(capsys) -> None:
    before = _run("b", _cell(20.0, n=50))
    after = _run("a", _cell(14.0, n=50))

    timing_compare.compare(before, after, load_floor=15.0, sigma=2.0, iat_tol=8.0, min_delta=1.5)
    out = capsys.readouterr().out
    assert "RETARD" in out
    assert "candidate knock limit" in out


def test_advance_that_took_cleanly_is_not_flagged_as_retard(capsys) -> None:
    before = _run("b", _cell(14.0, n=50))
    after = _run("a", _cell(20.0, n=50))

    timing_compare.compare(before, after, load_floor=15.0, sigma=2.0, iat_tol=8.0, min_delta=1.5)
    out = capsys.readouterr().out
    assert "took cleanly" in out
    assert "RETARD" not in out


def test_thin_cells_get_no_verdict(capsys) -> None:
    """n=2 yields a standard deviation but not a trustworthy one. CANary polls
    TIMING_ADVANCE at 0.06-0.26 Hz and routinely gives n=1-9 per cell."""
    before = _run("b", _cell(20.0, n=3))
    after = _run("a", _cell(5.0, n=3))

    timing_compare.compare(before, after, load_floor=15.0, sigma=2.0, iat_tol=8.0, min_delta=1.5)
    out = capsys.readouterr().out
    assert "insufficient samples" in out
    assert "RETARD" not in out


def test_iat_gap_marks_cells_not_comparable(capsys) -> None:
    """Knock margin is IAT-dependent, so a before/after across a temperature
    gap is not a valid comparison even when the delta looks real."""
    before = _run("b", _cell(20.0, n=50), air_temp=30.0)
    after = _run("a", _cell(14.0, n=50), air_temp=60.0)

    timing_compare.compare(before, after, load_floor=15.0, sigma=2.0, iat_tol=8.0, min_delta=1.5)
    out = capsys.readouterr().out
    assert "NOT COMPARABLE" in out


def test_uprev_loader_handles_missing_optional_columns(tmp_path: Path) -> None:
    """The 2026-07-26 16:53 log has no INTAKE AIR TMP column at all. Absent
    optional columns must materialise as NaN, not explode."""
    csv = tmp_path / "log.csv"
    csv.write_text("Time,IGN TIMING (BTDC),ENGINE RPM (rpm),CAL/LD VALUE (%)\n0,17,800,25\n")

    run = timing_compare.load_uprev(csv, "x")

    assert "air_temp" in run.frame.columns
    assert run.frame["air_temp"].isna().all()
    assert run.frame.iloc[0]["timing"] == 17.0


def test_uprev_loader_converts_fahrenheit_to_celsius(tmp_path: Path) -> None:
    """UpRev logs IAT in F; CANary stores C. Comparing them unconverted would
    make every cross-source comparison look like a huge temperature gap."""
    csv = tmp_path / "log.csv"
    csv.write_text(
        "Time,IGN TIMING (BTDC),ENGINE RPM (rpm),CAL/LD VALUE (%),INTAKE AIR TMP\n"
        "0,17,800,25,194.0\n"
    )

    run = timing_compare.load_uprev(csv, "x")

    assert run.frame.iloc[0]["air_temp"] == pytest.approx(90.0, abs=0.1)


def test_uprev_loader_rejects_a_log_without_timing(tmp_path: Path) -> None:
    csv = tmp_path / "bad.csv"
    csv.write_text("Time,ENGINE RPM (rpm)\n0,800\n")

    with pytest.raises(SystemExit, match="IGN TIMING"):
        timing_compare.load_uprev(csv, "x")
