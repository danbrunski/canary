"""tools/pull_report.py — pull detection and RPM-source preference."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.analysis.pulls import Pull, RpmSeries, detect_pulls
from tools import pull_report as tool


def _series(seconds: np.ndarray, rpm: np.ndarray) -> RpmSeries:
    return RpmSeries("test", seconds, rpm, pd.Timestamp("2026-07-28T16:46:57Z"), low_rate=False)


def _detect(series: RpmSeries, **overrides: float) -> list[Pull]:
    kwargs: dict[str, Any] = {
        "grid_ms": 100,
        "min_drdt": 120.0,
        "min_duration": 1.5,
        "min_span": 500.0,
        "band": 250,
    }
    kwargs.update(overrides)
    return detect_pulls(series, **kwargs)


def test_detects_single_clean_pull() -> None:
    # 10 s idle, then 2000->5000 rpm over 6 s (500 rpm/s), then steady.
    t = np.arange(0, 30, 0.01)
    rpm = np.piecewise(
        t,
        [t < 10, (t >= 10) & (t < 16), t >= 16],
        [2000.0, lambda x: 2000 + 500 * (x - 10), 5000.0],
    )
    pulls = _detect(_series(t, rpm))
    assert len(pulls) == 1
    (pull,) = pulls
    assert pull.rpm_end - pull.rpm_start > 2500
    assert not pull.dip
    # Torque proxy should recover the true slope in every interior band.
    interior = {b: v for b, v in pull.band_drdt.items() if 2500 <= b <= 4250}
    assert interior
    for value in interior.values():
        assert value == pytest.approx(500, rel=0.15)


def test_short_blip_and_steady_state_are_not_pulls() -> None:
    t = np.arange(0, 20, 0.01)
    rpm = np.full_like(t, 2000.0)
    rpm[(t >= 5) & (t < 5.5)] = 2000 + 400 * (t[(t >= 5) & (t < 5.5)] - 5)  # 0.5 s blip
    assert _detect(_series(t, rpm)) == []


def test_mid_pull_lift_sets_dip_flag() -> None:
    # Ramp with a momentary slope collapse in the middle (shift/lift).
    t = np.arange(0, 12, 0.01)
    rpm = 2000 + 400 * t
    hole = (t >= 5) & (t < 5.3)
    rpm[hole] = rpm[hole][0]  # flat spot
    rpm[t >= 5.3] -= 400 * 0.3
    pulls = _detect(_series(t, rpm))
    assert len(pulls) == 1
    assert pulls[0].dip


def test_rpm_source_prefers_decoded_over_obd(tmp_path: Path) -> None:
    ts = pd.date_range("2026-07-28T16:46:57Z", periods=5, freq="10ms")
    frame = pd.DataFrame(
        {
            "timestamp": list(ts) + [ts[0]],
            "source": ["decoded"] * 5 + ["obd"],
            "channel": ["ECM_RPM"] * 5 + ["RPM"],
            "value_number": [8 * 3000.0] * 5 + [3000.0],
        }
    )
    bindings = tmp_path / "bindings.yaml"
    bindings.write_text(
        "schema_version: 1\n"
        "signals:\n"
        "  - name: ECM_RPM\n"
        "    module: ECM\n"
        "    can_id: 505\n"
        "    byte_start: 2\n"
        "    byte_length: 2\n"
        "    scale: 0.125\n"
        "    unit: rpm\n"
        "    confidence: confirmed\n"
    )
    series = tool._rpm_series(frame, bindings)
    assert series.label.startswith("decoded:ECM_RPM")
    assert series.rpm[0] == pytest.approx(3000.0)  # scaled, not raw counts
    assert not series.low_rate


def test_rpm_source_falls_back_to_obd_with_caveat(tmp_path: Path) -> None:
    ts = pd.date_range("2026-07-28T16:46:57Z", periods=3, freq="1s")
    frame = pd.DataFrame(
        {
            "timestamp": ts,
            "source": "obd",
            "channel": "RPM",
            "value_number": [800.0, 900.0, 1000.0],
        }
    )
    series = tool._rpm_series(frame, tmp_path / "absent.yaml")
    assert series.low_rate
