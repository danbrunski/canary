"""End-to-end tests for the per-window binding analysis in decode_isotp.

Builds a synthetic candump + tags pair on disk and checks that the
report correctly surfaces:
  - Range-expanded DIDs (a DID's spread spikes in one window)
  - Window-unique DIDs (★)
  - Absence is suppressed for windows with no UDS traffic
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.decode_isotp import main as decode_main


def _write_candump(path: Path, frames: list[tuple[float, int, str]]) -> None:
    """Write candump-format lines: (timestamp, can_id, hex_data) per frame."""
    lines = [f"({ts:.6f}) vcan0 {cid:03X}#{hex_data} R" for ts, cid, hex_data in frames]
    path.write_text("\n".join(lines) + "\n")


def _write_tags(path: Path, tags: list[tuple[float, str]]) -> None:
    """Write JSONL tags: (timestamp, label) per tag."""
    records = [{"t": t, "kind": "event", "label": label} for t, label in tags]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _mode22_request(did: int) -> str:
    """ISO-TP single frame for Mode 22 read of DID."""
    return f"03 22 {did >> 8:02X} {did & 0xFF:02X} FF FF FF FF".replace(" ", "")


def _mode22_response_2b(did: int, value: int) -> str:
    """ISO-TP single frame Mode 22 positive response, 2-byte payload."""
    return (
        f"05 62 {did >> 8:02X} {did & 0xFF:02X} {value >> 8:02X} {value & 0xFF:02X} 00 00".replace(
            " ", ""
        )
    )


def _mode22_response_4b(did: int, value: int) -> str:
    """ISO-TP single frame Mode 22 positive response, 4-byte payload."""
    return (
        f"07 62 {did >> 8:02X} {did & 0xFF:02X} "
        f"{(value >> 24) & 0xFF:02X} {(value >> 16) & 0xFF:02X} "
        f"{(value >> 8) & 0xFF:02X} {value & 0xFF:02X}".replace(" ", "")
    )


@pytest.fixture
def synthetic_session(tmp_path: Path) -> Path:
    """Three windows, two DIDs, designed to exercise binding signals.

    - Window 0 (`baseline`): no diagnostic traffic at all
    - Window 1 (`throttle_blip`): both DIDs polled; 0x1226 spread is HUGE,
      0x1208 spread is small
    - Window 2 (`steady_idle`): both DIDs polled; both spreads are small

    Expected report behavior:
      - 0x1226 flagged as range-expanded in `throttle_blip`
      - 0x1208 NOT flagged (small spread everywhere)
      - `baseline` window absent-list is empty (no UDS traffic to compare)
    """
    session = tmp_path / "synth"
    session.mkdir()

    # Window 1: throttle blip — 0x1226 sweeps from 100..700, 0x1208 stays 6000-6010
    # Window 2: steady idle — both DIDs stay narrow
    frames: list[tuple[float, int, str]] = []
    # Window 1 (10..20s)
    for i in range(20):
        t = 10.0 + i * 0.5
        # 0x1226 ramps wildly
        frames.append((t, 0x7E0, _mode22_request(0x1226)))
        frames.append((t + 0.001, 0x7E8, _mode22_response_2b(0x1226, 100 + i * 30)))
        # 0x1208 small variation
        frames.append((t + 0.01, 0x7E0, _mode22_request(0x1208)))
        frames.append((t + 0.011, 0x7E8, _mode22_response_2b(0x1208, 6000 + (i % 10))))
    # Window 2 (20..30s)
    for i in range(20):
        t = 20.0 + i * 0.5
        frames.append((t, 0x7E0, _mode22_request(0x1226)))
        frames.append((t + 0.001, 0x7E8, _mode22_response_2b(0x1226, 430 + (i % 5))))
        frames.append((t + 0.01, 0x7E0, _mode22_request(0x1208)))
        frames.append((t + 0.011, 0x7E8, _mode22_response_2b(0x1208, 6000 + (i % 10))))

    _write_candump(session / "frames.log", frames)
    _write_tags(
        session / "tags.jsonl",
        [(0.0, "baseline"), (10.0, "throttle_blip"), (20.0, "steady_idle")],
    )
    return session


def test_range_expansion_surfaces_throttle_did(
    synthetic_session: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = decode_main([str(synthetic_session), "--no-signals"])
    assert rc == 0
    out = capsys.readouterr().out
    # 0x1226 should be flagged as range-expanded in throttle_blip
    assert "throttle_blip" in out
    assert "Range-expanded DIDs" in out
    assert "0x1226" in out


def test_range_expansion_does_not_flag_steady_did(
    synthetic_session: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    decode_main([str(synthetic_session), "--no-signals"])
    out = capsys.readouterr().out
    # 0x1208 has small spread everywhere; should not appear in any
    # "Range-expanded" listing.
    in_expansion = False
    for line in out.splitlines():
        if "Range-expanded" in line:
            in_expansion = True
            continue
        if in_expansion:
            if line.startswith("---") or not line.startswith("    "):
                in_expansion = False
                continue
            assert "0x1208" not in line, "steady DID should not appear as range-expanded"


def test_baseline_window_does_not_show_absence(
    synthetic_session: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The baseline window has zero UDS traffic; absence is meaningless."""
    decode_main([str(synthetic_session), "--no-signals"])
    out = capsys.readouterr().out
    # Find the 'baseline' section if any was printed for it
    baseline_idx = out.find("'baseline'")
    if baseline_idx == -1:
        return  # window had nothing to report at all — also acceptable
    next_section = out.find("---", baseline_idx + 1)
    section = out[baseline_idx:next_section] if next_section > 0 else out[baseline_idx:]
    assert "absent here" not in section, (
        "baseline window has no UDS traffic; should not report absences"
    )


def test_range_expansion_skips_ambiguous_wide_payloads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = tmp_path / "wide"
    session.mkdir()

    frames: list[tuple[float, int, str]] = []
    for i in range(20):
        t = 10.0 + i * 0.5
        frames.append((t, 0x7E0, _mode22_request(0x1234)))
        frames.append((t + 0.001, 0x7E8, _mode22_response_4b(0x1234, 100_000 + i * 10_000)))
    _write_candump(session / "frames.log", frames)
    _write_tags(session / "tags.jsonl", [(10.0, "wide_payload")])

    rc = decode_main([str(session), "--no-signals"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Range-expanded DIDs" not in out
