"""Tests for the DID-map validator tool (hermetic — no session data needed)."""

from __future__ import annotations

import json
from pathlib import Path

from tools.decode_dids import main

_MAP = """\
schema_version: 1
vehicle: {make: Nissan}
access: {ecu_request_id: 0x7E0, ecu_response_id: 0x7E8, session: 0xC0}
dids:
  - did: 0x1101
    name: ECM_COOLANT_TEMP
    byte_start: 0
    byte_length: 1
    scale: 1.0
    offset: -40.0
    unit: degC
    confidence: confirmed
    plausible_min: -40.0
    plausible_max: 130.0
  - did: 0x9999
    name: UNSEEN
    byte_start: 0
    byte_length: 1
    confidence: low
"""


def _write(tmp: Path, didmap: str, snap: dict[str, str]) -> tuple[Path, Path]:
    m = tmp / "dids.yaml"
    m.write_text(didmap)
    s = tmp / "dids_idle_c0.json"
    s.write_text(json.dumps({"session": "10C0", "state": "idle", "values": snap}))
    return m, s


def test_plausible_snapshot_returns_zero(tmp_path: Path) -> None:
    m, _ = _write(tmp_path, _MAP, {"0x1101": "65", "0x1234": "ab"})  # 0x65=101 → 61°C ok
    assert main([str(tmp_path), "--dids", str(m)]) == 0


def test_implausible_decode_flagged(tmp_path: Path) -> None:
    # raw 0x00 → 0-40 = -40 °C is the engine-off sentinel: in-bounds (== min).
    # raw 0xFF → 255-40 = 215 °C is well outside plausible_max 130 → exit 2.
    m, _ = _write(tmp_path, _MAP, {"0x1101": "ff"})
    assert main([str(tmp_path), "--dids", str(m)]) == 2


def test_no_snapshots_returns_one(tmp_path: Path) -> None:
    m = tmp_path / "dids.yaml"
    m.write_text(_MAP)
    assert main([str(tmp_path), "--dids", str(m)]) == 1  # no dids_*.json present


def test_confidence_filter(tmp_path: Path) -> None:
    m, _ = _write(tmp_path, _MAP, {"0x1101": "ff"})
    # Excluding 'confirmed' drops the only implausible entry → clean exit 0.
    assert main([str(tmp_path), "--dids", str(m), "--confidence", "low"]) == 0
