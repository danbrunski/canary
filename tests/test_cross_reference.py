"""Tests for the Mode 22 ↔ Mode 01 cross-reference analyzer.

Synthesizes paired streams (frames.log + pids.jsonl) with a known scaling,
runs the full pipeline, and verifies the regression recovers the scaling.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.cross_reference import (
    cross_reference,
    linear_regression,
    load_pid_units,
    load_pids_jsonl,
    pair_by_timestamp,
)

# ---------------------------------------------------------------------------
# linear_regression
# ---------------------------------------------------------------------------


def test_linear_regression_recovers_known_slope_and_offset():
    # y = 2.5 * x + 10 with no noise
    pairs = [(float(x), 2.5 * x + 10) for x in range(0, 20)]
    slope, offset, r2 = linear_regression(pairs)
    assert abs(slope - 2.5) < 1e-9
    assert abs(offset - 10.0) < 1e-9
    assert abs(r2 - 1.0) < 1e-9


def test_linear_regression_handles_noise():
    import random

    rng = random.Random(42)
    pairs = [(float(x), 0.5 * x + 3 + rng.gauss(0, 0.1)) for x in range(0, 50)]
    slope, offset, r2 = linear_regression(pairs)
    assert abs(slope - 0.5) < 0.01
    assert abs(offset - 3.0) < 0.5
    assert r2 > 0.99


def test_linear_regression_constant_x_returns_zero():
    pairs = [(5.0, y) for y in range(0, 20)]
    slope, _, r2 = linear_regression(pairs)
    assert slope == 0.0
    assert r2 == 0.0


def test_linear_regression_constant_y_returns_zero_r2():
    pairs = [(float(x), 7.0) for x in range(0, 20)]
    _, _, r2 = linear_regression(pairs)
    assert r2 == 0.0


def test_linear_regression_too_few_points_returns_zero():
    assert linear_regression([(1.0, 2.0)]) == (0.0, 0.0, 0.0)
    assert linear_regression([]) == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# pair_by_timestamp
# ---------------------------------------------------------------------------


def test_pair_by_timestamp_basic_alignment():
    a = [(1.0, 100.0), (2.0, 200.0), (3.0, 300.0)]
    b = [(1.05, 1.0), (2.1, 2.0), (3.2, 3.0)]
    pairs = pair_by_timestamp(a, b, tolerance_seconds=0.5)
    assert pairs == [(100.0, 1.0), (200.0, 2.0), (300.0, 3.0)]


def test_pair_by_timestamp_drops_out_of_tolerance():
    a = [(1.0, 100.0), (5.0, 500.0)]
    b = [(1.1, 1.0), (10.0, 10.0)]  # second far from a
    pairs = pair_by_timestamp(a, b, tolerance_seconds=0.5)
    assert pairs == [(100.0, 1.0)]


def test_pair_by_timestamp_picks_nearest_when_multiple_in_window():
    a = [(2.0, 200.0)]
    b = [(1.8, 0.8), (2.1, 0.9), (2.4, 0.4)]
    pairs = pair_by_timestamp(a, b, tolerance_seconds=1.0)
    # Nearest to 2.0 is 2.1 (delta 0.1) over 1.8 (delta 0.2) and 2.4 (delta 0.4).
    assert pairs == [(200.0, 0.9)]


def test_pair_by_timestamp_does_not_reuse_pid_samples():
    a = [(1.0, 100.0), (1.2, 200.0)]
    b = [(1.05, 1.0)]
    pairs = pair_by_timestamp(a, b, tolerance_seconds=0.3)
    assert pairs == [(100.0, 1.0)]


def test_pair_by_timestamp_empty_inputs():
    assert pair_by_timestamp([], [(1.0, 2.0)]) == []
    assert pair_by_timestamp([(1.0, 2.0)], []) == []


def test_pid_jsonl_loaders_skip_malformed_lines(tmp_path: Path):
    path = tmp_path / "pids.jsonl"
    path.write_text(
        json.dumps({"t": 1.0, "pid": "RPM", "value": 700.0, "unit": "rpm"}) + "\n"
        '{"t": 2.0, "pid": "RPM", "value": \n'
        "{}\n" + json.dumps({"t": 3.0, "pid": "RPM", "value": 720.0, "unit": "rpm"}) + "\n"
    )

    assert load_pids_jsonl(path)["RPM"] == [(1.0, 700.0), (3.0, 720.0)]
    assert load_pid_units(path) == {"RPM": "rpm"}


# ---------------------------------------------------------------------------
# End-to-end: synthetic session → bindings recovered
# ---------------------------------------------------------------------------


def _write_candump_for_did_at_times(
    path: Path, can_id_req: int, can_id_resp: int, did: int, samples: list[tuple[float, int]]
) -> None:
    """Write candump frames simulating Mode 22 read/response pairs for a DID.

    Samples are (timestamp, raw_value). For each sample we emit the request
    and a 2-byte response.
    """
    lines = []
    for ts, val in samples:
        # request: 03 22 hi lo padding
        req_data = f"03 22 {did >> 8:02X} {did & 0xFF:02X} FF FF FF FF".replace(" ", "")
        lines.append(f"({ts:.6f}) vcan0 {can_id_req:03X}#{req_data} R")
        # response: 05 62 hi lo vh vl 00 00
        resp_data = (
            f"05 62 {did >> 8:02X} {did & 0xFF:02X} {val >> 8:02X} {val & 0xFF:02X} 00 00"
        ).replace(" ", "")
        # Response timestamp slightly after request to simulate ECM latency
        lines.append(f"({ts + 0.001:.6f}) vcan0 {can_id_resp:03X}#{resp_data} R")
    path.write_text("\n".join(lines) + "\n")


def _write_pids_jsonl(path: Path, samples: list[tuple[float, str, float, str]]) -> None:
    """samples = (timestamp, pid, value, unit)."""
    lines = []
    for t, pid, value, unit in samples:
        lines.append(json.dumps({"t": t, "pid": pid, "value": value, "unit": unit}))
    path.write_text("\n".join(lines) + "\n")


def test_cross_reference_recovers_battery_voltage_scaling(tmp_path: Path):
    """0x1204 raw / 16 = battery voltage. After regression we should get
    slope ≈ 1/16, offset ≈ 0, high r²."""
    session = tmp_path / "synth"
    session.mkdir()

    # 30 paired samples: raw rises linearly 200..240, voltage = raw/16
    raw_values = list(range(200, 240, 2))  # 20 samples
    timestamps = [10.0 + i * 0.5 for i in range(len(raw_values))]

    _write_candump_for_did_at_times(
        session / "frames.log",
        can_id_req=0x7E0,
        can_id_resp=0x7E8,
        did=0x1204,
        samples=list(zip(timestamps, raw_values, strict=True)),
    )
    pid_samples = [
        (t + 0.05, "CONTROL_MODULE_VOLTAGE", raw / 16.0, "V")
        for t, raw in zip(timestamps, raw_values, strict=True)
    ]
    _write_pids_jsonl(session / "pids.jsonl", pid_samples)

    bindings = cross_reference(session, min_pairs=10, min_r_squared=0.5)
    assert bindings, "expected at least one binding"
    top = bindings[0]
    assert top.did == 0x1204
    assert top.pid == "CONTROL_MODULE_VOLTAGE"
    assert top.r_squared > 0.99
    assert abs(top.slope - 1 / 16) < 1e-6
    assert abs(top.offset) < 1e-6
    assert top.pid_unit == "V"
    assert top.n_pairs >= 10


def test_cross_reference_errors_when_no_pids_jsonl(tmp_path: Path):
    """Helpful error when the OBD sidecar doesn't exist."""
    session = tmp_path / "synth-noobd"
    session.mkdir()
    (session / "frames.log").write_text("(1.0) vcan0 7E8#0562110305A30000\n")

    import pytest

    with pytest.raises(FileNotFoundError, match="no pids.jsonl"):
        cross_reference(session)


def test_cross_reference_skips_low_correlation_pairings(tmp_path: Path):
    """A DID whose values are uncorrelated with any PID should not bind."""
    session = tmp_path / "synth-noise"
    session.mkdir()

    # DID samples that increase linearly; PID samples that DON'T track them.
    timestamps = [10.0 + i * 0.5 for i in range(30)]
    raw_values = [200 + i for i in range(30)]
    _write_candump_for_did_at_times(
        session / "frames.log",
        can_id_req=0x7E0,
        can_id_resp=0x7E8,
        did=0x1234,
        samples=list(zip(timestamps, raw_values, strict=True)),
    )
    # Random-ish PID values that don't track raw_values
    pid_samples = [
        (t, "RPM", float((i * 13) % 50), "rpm")  # noise pattern
        for i, t in enumerate(timestamps)
    ]
    _write_pids_jsonl(session / "pids.jsonl", pid_samples)

    bindings = cross_reference(session, min_pairs=10, min_r_squared=0.8)
    # Either no bindings, or at least none for the noise pattern at high r²
    for b in bindings:
        assert b.r_squared < 0.8
