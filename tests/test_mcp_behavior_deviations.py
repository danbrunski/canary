"""Tests for the get_behavior_deviations MCP tool (task 3.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.mcp_server.tools import behavior as behavior_tool
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord


def _cfg_yaml(storage: Path, behavior_models: Path) -> str:
    return f"""
vehicle: {{make: Nissan, model: Xterra, year: 2008, vin: ""}}
hardware:
  elm327_port: /dev/ttyUSB0
  can: {{interface: virtual, channel: vcan0, bitrate: 500000}}
collection:
  obd_poll_rate_hz: 1
  storage_path: {storage}
  session_retention_days: 30
  storage_warning_mb: 500
preprocessing:
  resample_hz: 1
  rolling_window_seconds: 30
  operating_modes: {{idle: {{rpm_max: 900, speed_max: 0}}}}
models:
  isolation_forest: {{enabled: true, n_estimators: 100, contamination: 0.01}}
  lstm_autoencoder: {{enabled: false, sequence_length: 60, latent_dim: 16, threshold_k: 3.0}}
dashboard: {{host: 127.0.0.1, port: 8000}}
daemon: {{behavior_models: {behavior_models}}}
"""


def _behavior_models_yaml() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "models": [
            {
                "name": "timing_vs_rpm_load",
                "kind": "surface_2d",
                "output": "obd:TIMING_ADVANCE",
                "axes": [
                    {"name": "rpm", "channel": "obd:RPM", "bin_edges": [0.0, 1000.0, 2000.0]},
                    {"name": "load", "channel": "obd:ENGINE_LOAD", "bin_edges": [0.0, 50.0, 100.0]},
                ],
                "fit": {
                    "kind": "grid_binned_mean",
                    "cells": [
                        {"mean": 10.0, "std": 1.0, "count": 20},
                        {"mean": 15.0, "std": 1.0, "count": 20},
                        {"mean": 20.0, "std": 1.0, "count": 20},
                        {"mean": 25.0, "std": 1.0, "count": 20},
                    ],
                    "sample_count": 80,
                    "fitted_at": "2026-01-01T00:00:00Z",
                },
                "deviation": {"kind": "absolute", "warning_band": 3.0, "critical_band": 8.0},
            }
        ],
    }


def _write_env(tmp_path: Path, *, session_id: str = "2026-07-19T000000") -> Path:
    storage = tmp_path / "data"
    session_dir = storage / "nissan-xterra-2008" / session_id
    records = []
    for i in range(10):
        t = 1_780_000_000.0 + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 500.0, "rpm"),
                TelemetryRecord.obd(t, "ENGINE_LOAD", 25.0, "%"),
                TelemetryRecord.obd(t, "TIMING_ADVANCE", 15.0, "degBTDC"),
            ]
        )
    write_records_to_parquet(records, session_dir, max_rows=10_000)

    cfg_path = tmp_path / "canary.yaml"
    # The config names the behavior-models file (R01.2: the default is the
    # repo's own data/behavior_models.yaml, never the working directory's);
    # scope_config nests it under the profile slug.
    cfg_path.write_text(_cfg_yaml(storage, tmp_path / "data" / "behavior_models.yaml"))
    bm_path = tmp_path / "data" / "nissan-xterra-2008" / "behavior_models.yaml"
    bm_path.parent.mkdir(parents=True, exist_ok=True)
    bm_path.write_text(yaml.safe_dump(_behavior_models_yaml()))
    return cfg_path


def test_get_behavior_deviations_returns_surfaces(tmp_path: Path) -> None:
    cfg_path = _write_env(tmp_path)
    out = behavior_tool.get_behavior_deviations(cfg_path, session="2026-07-19T000000")
    assert out["ok"] is True
    assert out["n_rows"] == 10
    (surface,) = out["surfaces"]
    assert surface["name"] == "timing_vs_rpm_load"
    assert surface["mean_deviation"] == 5.0


def test_get_behavior_deviations_mode_filter_excludes_ungated_surface(tmp_path: Path) -> None:
    cfg_path = _write_env(tmp_path)
    out = behavior_tool.get_behavior_deviations(
        cfg_path, session="2026-07-19T000000", mode="warmup"
    )
    assert out["ok"] is True
    assert out["surfaces"] == []


def test_get_behavior_deviations_unknown_session_returns_ok_false(tmp_path: Path) -> None:
    cfg_path = _write_env(tmp_path)
    out = behavior_tool.get_behavior_deviations(cfg_path, session="2099-01-01T000000")
    assert out["ok"] is False
    assert "not found" in out["error"]


def test_get_behavior_deviations_missing_behavior_models_returns_ok_false(tmp_path: Path) -> None:
    cfg_path = _write_env(tmp_path)
    (tmp_path / "data" / "nissan-xterra-2008" / "behavior_models.yaml").unlink()
    out = behavior_tool.get_behavior_deviations(cfg_path, session="2026-07-19T000000")
    assert out["ok"] is False
    assert "behavior_models.yaml" in out["error"]
