"""Offline tests for the CANary-HAT K-line edge decoder.

``hardware/canary-hat/firmware/kline_decode.py`` turns a passive edge capture
(``kline_edges.py``) into bytes. It was validated on a real UpRev session
(1,462 frames, every checksum good); these tests pin the behaviour that result
rests on, with synthetic edges whose answer is known by construction.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

DECODER = Path(__file__).resolve().parents[1] / "hardware/canary-hat/firmware/kline_decode.py"
BIT_NS = 1e9 / 10400


@pytest.fixture(scope="module")
def kd() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kline_decode", DECODER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _uart_levels(data: bytes) -> list[int]:
    """8N1 bit levels for ``data``: start 0, LSB first, stop 1."""
    bits: list[int] = []
    for byte in data:
        bits += [0] + [(byte >> i) & 1 for i in range(8)] + [1]
    return bits


def _edges(segments: list[tuple[int, float]], t0: int = 1_000_000) -> list[tuple[int, int]]:
    """Edges for consecutive (level, duration_ns) segments; the line idles high."""
    edges: list[tuple[int, int]] = []
    t = float(t0)
    level = 1
    for seg_level, duration in segments:
        if seg_level != level:
            edges.append((round(t), seg_level))
            level = seg_level
        t += duration
    if level != 1:
        edges.append((round(t), 1))
    return edges


def _frame_segments(data: bytes, gap_ns: float = 0.0) -> list[tuple[int, float]]:
    segs = [(lvl, BIT_NS) for lvl in _uart_levels(data)]
    return segs + ([(1, gap_ns)] if gap_ns else [])


def test_decodes_a_wakeup_pulse_then_bytes(kd: ModuleType) -> None:
    start_comm = bytes.fromhex("8110FC810E")  # UpRev's StartCommunication
    segs = [(1, 5e6), (0, 25e6), (1, 25e6)] + _frame_segments(start_comm, 1e6)
    records = list(kd.decode(_edges(segs), BIT_NS, long_low_ns=12 * BIT_NS))

    lows = [r for r in records if r[0] == "low"]
    data = [r for r in records if r[0] == "byte"]
    assert len(lows) == 1
    assert lows[0][2] == pytest.approx(25e6, abs=1)
    assert bytes(r[2] for r in data) == start_comm
    assert all(r[3] for r in data)  # every stop bit high


def test_zero_bytes_are_data_not_wakeup_pulses(kd: ModuleType) -> None:
    # 0x00 holds the line low for 9 bit times: the longest legal low in data.
    payload = bytes.fromhex("0400000000")
    records = list(kd.decode(_edges(_frame_segments(payload, 1e6)), BIT_NS, 12 * BIT_NS))

    assert [r[0] for r in records] == ["byte"] * len(payload)
    assert bytes(r[2] for r in records) == payload


def test_bit_time_is_measured_from_single_bit_pulses(kd: ModuleType) -> None:
    segs = _frame_segments(bytes([0x55] * 40), 2e6)  # 0x55 alternates every bit
    pulses = kd.pulses(_edges(segs))

    assert kd.bit_time(pulses) == pytest.approx(BIT_NS, rel=0.01)


def test_a_bad_stop_bit_is_flagged(kd: ModuleType) -> None:
    levels = _uart_levels(b"\x81")
    levels[-1] = 0  # stop bit low: a framing error
    segs = [(lvl, BIT_NS) for lvl in levels] + [(1, 1e6)]
    records = list(kd.decode(_edges(segs), BIT_NS, 12 * BIT_NS))

    assert records[0][0] == "byte"
    assert records[0][3] is False
