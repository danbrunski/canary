"""Tests for tools/did_sweep.py — the did_sweep job's actual worker script."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import can
import pytest

from src.decode.dids import DidAccess
from tools import did_sweep


def _config_yaml(channel: str) -> str:
    return f"""
vehicle:
  make: Nissan
  model: Xterra
  year: 2008
  vin: ""
hardware:
  elm327_port: /dev/ttyUSB0
  can:
    interface: virtual
    channel: {channel}
    bitrate: 500000
collection:
  obd_poll_rate_hz: 1
  storage_path: {{storage}}
  session_retention_days: 30
  storage_warning_mb: 500
preprocessing:
  resample_hz: 1
  rolling_window_seconds: 30
  operating_modes:
    idle:
      rpm_max: 900
      speed_max: 0
models:
  isolation_forest:
    enabled: false
    n_estimators: 100
    contamination: 0.01
  lstm_autoencoder:
    enabled: false
    sequence_length: 60
    latent_dim: 16
    threshold_k: 3.0
dashboard:
  host: 127.0.0.1
  port: 8000
"""


def _write_config(tmp_path: Path, channel: str) -> Path:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(channel).format(storage=storage))
    return cfg_path


# ---------------------------------------------------------------------------
# _resolve_session
# ---------------------------------------------------------------------------


def test_resolve_session_none_disables_gating() -> None:
    assert did_sweep._resolve_session("none") is None


def test_resolve_session_explicit_hex() -> None:
    assert did_sweep._resolve_session("C0") == 0xC0


def test_resolve_session_auto_delegates_to_did_access(monkeypatch: pytest.MonkeyPatch) -> None:
    access = DidAccess(ecu_request_id=0x7E0, ecu_response_id=0x7E8, session=0xC0)
    monkeypatch.setattr(did_sweep, "did_access", lambda: access)
    assert did_sweep._resolve_session("auto") == 0xC0


def test_resolve_session_auto_with_no_access_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(did_sweep, "did_access", lambda: None)
    assert did_sweep._resolve_session("auto") is None


# ---------------------------------------------------------------------------
# main() — range/header validation, no bus needed
# ---------------------------------------------------------------------------


def test_main_rejects_inverted_range(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, "did-sweep-unused")
    rc = did_sweep.main(
        [
            "--config",
            str(cfg_path),
            "--start",
            "0x1200",
            "--end",
            "0x1100",
            "--label",
            "x",
            "--out",
            str(tmp_path / "out.json"),
        ]
    )
    assert rc == 1


def test_main_rejects_non_hex_header(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, "did-sweep-unused2")
    rc = did_sweep.main(
        [
            "--config",
            str(cfg_path),
            "--start",
            "0x1100",
            "--end",
            "0x1101",
            "--label",
            "x",
            "--header",
            "ZZZ",
            "--out",
            str(tmp_path / "out.json"),
        ]
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# main() — real virtual-bus sweep, no session gating
# ---------------------------------------------------------------------------


def _did_reply(*, did: int, responder_id: int, data: bytes) -> can.Message:
    payload = bytes([0x62, (did >> 8) & 0xFF, did & 0xFF]) + data
    frame = bytes([len(payload)]) + payload
    frame += bytes(8 - len(frame))
    return can.Message(arbitration_id=responder_id, data=frame, is_extended_id=False)


def _run_fake_ecu(channel: str, values: dict[int, bytes], *, stop: threading.Event) -> None:
    ecu = can.interface.Bus(interface="virtual", channel=channel)
    try:
        while not stop.is_set():
            frame = ecu.recv(timeout=0.1)
            if frame is None:
                continue
            data = bytes(frame.data)
            if data[1] != 0x22:
                continue
            did = (data[2] << 8) | data[3]
            value = values.get(did)
            if value is not None:
                ecu.send(_did_reply(did=did, responder_id=0x7E8, data=value))
    finally:
        ecu.shutdown()


def test_main_sweeps_and_writes_a_snapshot(tmp_path: Path) -> None:
    channel = "did-sweep-e2e"
    cfg_path = _write_config(tmp_path, channel)
    out_path = tmp_path / "snap.json"
    values = {0x1100: bytes([0x0A, 0x0B]), 0x1102: bytes([0xFF])}
    stop = threading.Event()
    t = threading.Thread(
        target=_run_fake_ecu, args=(channel, values), kwargs={"stop": stop}, daemon=True
    )
    t.start()
    try:
        rc = did_sweep.main(
            [
                "--config",
                str(cfg_path),
                "--start",
                "0x1100",
                "--end",
                "0x1102",
                "--label",
                "control",
                "--session",
                "none",
                "--read-timeout",
                "0.2",
                "--out",
                str(out_path),
            ]
        )
    finally:
        stop.set()
        t.join(timeout=1.0)

    assert rc == 0
    data = json.loads(out_path.read_text())
    assert data["label"] == "control"
    assert data["values"] == {"0x1100": "0a0b", "0x1102": "ff"}
    # 0x1101 never answered — absent, not a null/empty entry.
    assert "0x1101" not in data["values"]
