"""Tests for the broadcast-byte ↔ Mode 01 PID correlation analysis."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.analysis.broadcast_correlate import (
    ByteProbe,
    byte_timeseries,
    correlate_broadcast,
    probes_from_bindings,
)
from src.decode.bindings import Signal

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_session(tmp_path: Path, *, n: int = 200, with_pids: bool = True) -> Path:
    """Synthetic session: 0x123 byte 0 = rpm/32, byte 1 = constant; pids.jsonl has RPM."""
    session = tmp_path / "sess"
    session.mkdir()
    t0 = 1_778_000_000.0
    frame_lines: list[str] = []
    pid_lines: list[str] = []
    for i in range(n):
        t = t0 + i * 0.02  # 50 Hz frames
        rpm = 800 + i * 18  # ramps 800 → ~4400
        b0 = min(255, rpm // 32)
        noise = (i * 31) % 256  # scrambled "counter" byte — correlates with nothing
        frame_lines.append(f"({t:.6f}) vcan0 123#{b0:02X}07{noise:02X}00")
        if i % 4 == 0 and with_pids:  # PID readings (n/4 ≥ min_pairs for n=200)
            pid_lines.append(json.dumps({"t": t, "pid": "RPM", "value": float(rpm), "unit": "rpm"}))
            throttle = {"t": t, "pid": "THROTTLE_POS", "value": float(5 + (i % 50)), "unit": ""}
            pid_lines.append(json.dumps(throttle))
    (session / "frames.log").write_text("\n".join(frame_lines) + "\n")
    if with_pids:
        (session / "pids.jsonl").write_text("\n".join(pid_lines) + "\n")
    return session


def test_byte_timeseries_extracts_one_byte(tmp_path: Path) -> None:
    session = _write_session(tmp_path, n=10)
    series = byte_timeseries(session / "frames.log", ByteProbe(0x123, 0, 1))
    assert len(series) == 10
    # byte 0 = (800 + i*18)//32, monotonically non-decreasing
    values = [v for _, v in series]
    assert values == sorted(values)
    assert values[0] == 800 // 32


def test_byte_timeseries_ignores_short_frames_and_other_ids(tmp_path: Path) -> None:
    session = tmp_path / "s"
    session.mkdir()
    (session / "frames.log").write_text(
        "(1.0) vcan0 123#1122334455667788\n"
        "(1.1) vcan0 999#AABBCCDD\n"  # different id
        "(1.2) vcan0 123#11\n"  # too short for byte 3
        "(1.3) vcan0 123#1122334455667788\n"
    )
    series = byte_timeseries(session / "frames.log", ByteProbe(0x123, 3, 1))
    assert [v for _, v in series] == [0x44, 0x44]


def test_correlate_broadcast_finds_rpm_byte(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    [result] = correlate_broadcast(session, [ByteProbe(0x123, 0, 1, label="rpm_hi")])
    assert result.n_frames == 200
    assert result.raw_range is not None
    best = result.best
    assert best is not None
    assert best.pid == "RPM"
    assert best.r_squared > 0.95  # < 1.0 because byte 0 = floor(rpm/32) is quantized
    # PID = raw × slope + offset, raw ≈ rpm/32 → slope in the ballpark of 32
    assert 25 < best.slope < 40


def test_correlate_broadcast_reports_no_fit_for_a_counter_byte(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    # byte 2 is a 0..255 frame counter — no correlation with RPM/throttle
    [result] = correlate_broadcast(session, [ByteProbe(0x123, 2, 1)], min_r2=0.5)
    assert result.fits == ()


def test_correlate_broadcast_no_match_for_absent_id(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    [result] = correlate_broadcast(session, [ByteProbe(0xABC, 0, 1)])
    assert result.n_frames == 0
    assert result.raw_range is None
    assert result.fits == ()


def test_correlate_broadcast_requires_pids_jsonl(tmp_path: Path) -> None:
    session = _write_session(tmp_path, with_pids=False)
    with pytest.raises(FileNotFoundError, match="pids.jsonl"):
        correlate_broadcast(session, [ByteProbe(0x123, 0, 1)])


def test_byte_probe_from_signal_and_display() -> None:
    sig = Signal(
        name="ECM_RPM",
        module="ECM",
        can_id=0x1F9,
        byte_start=2,
        byte_length=2,
        byte_order="big",
        confidence="confirmed",
    )
    probe = ByteProbe.from_signal(sig)
    assert probe.can_id == 0x1F9
    assert probe.byte_length == 2
    assert "0x1F9" in probe.display and "bytes 2-3" in probe.display and "ECM_RPM" in probe.display


def test_probes_from_bindings_unpinned_only() -> None:
    probes = probes_from_bindings(REPO_ROOT / "data" / "bindings.yaml")
    labels = {p.label for p in probes}
    # ECM_RPM has a pinned scale → excluded; ECM_CALC_LOAD_A has scale: null → included
    assert "ECM_RPM" not in labels
    assert "ECM_CALC_LOAD_A" in labels
    all_probes = probes_from_bindings(REPO_ROOT / "data" / "bindings.yaml", unpinned_only=False)
    assert "ECM_RPM" in {p.label for p in all_probes}
    assert len(all_probes) > len(probes)
