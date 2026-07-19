"""End-to-end smoke test for scripts/analyze_session.py (task 3.3)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.analyze_session import main
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord


def _cfg_yaml(storage: Path) -> str:
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
"""


def _behavior_models_yaml() -> dict:
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


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Path]:
    # The default profile for this vehicle block slugs to nissan-xterra-2008
    # (see src/profiles.py:_default_profile) — the session must live there.
    storage = tmp_path / "data"
    slug_dir = storage / "nissan-xterra-2008"
    session_id = "2026-07-19T000000"
    session_dir = slug_dir / session_id
    records = []
    for i in range(10):
        t = 1_780_000_000.0 + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 500.0, "rpm"),
                TelemetryRecord.obd(t, "ENGINE_LOAD", 25.0, "%"),
                TelemetryRecord.obd(t, "TIMING_ADVANCE", 15.0, "degBTDC"),  # expected 10 -> Δ=+5
            ]
        )
    write_records_to_parquet(records, session_dir, max_rows=10_000)

    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_cfg_yaml(storage))
    bm_path = tmp_path / "behavior_models.yaml"
    bm_path.write_text(yaml.safe_dump(_behavior_models_yaml()))

    return {"config": cfg_path, "behavior_models": bm_path, "session_id": session_id}


def test_cli_prints_deviation_table(env: dict[str, Path], capsys: pytest.CaptureFixture) -> None:
    exit_code = main(
        [
            "--config",
            str(env["config"]),
            "--session",
            str(env["session_id"]),
            "--behavior-models",
            str(env["behavior_models"]),
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "timing_vs_rpm_load" in out
    assert "flagged" in out  # warning_fraction > 0 for the injected +5 offset
    assert "5.000" in out  # mean_deviation


def test_cli_mode_filter_excludes_non_matching_surfaces(
    env: dict[str, Path], capsys: pytest.CaptureFixture
) -> None:
    exit_code = main(
        [
            "--config",
            str(env["config"]),
            "--session",
            str(env["session_id"]),
            "--behavior-models",
            str(env["behavior_models"]),
            "--mode",
            "warmup",  # the fixture's surface is ungated (operating_mode=None)
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "0/1 surfaces" in out
    assert "timing_vs_rpm_load" not in out


def test_cli_unknown_session_errors_cleanly(
    env: dict[str, Path], capsys: pytest.CaptureFixture
) -> None:
    exit_code = main(
        [
            "--config",
            str(env["config"]),
            "--session",
            "2099-01-01T000000",
            "--behavior-models",
            str(env["behavior_models"]),
        ]
    )
    assert exit_code == 1
    assert "not found" in capsys.readouterr().err
