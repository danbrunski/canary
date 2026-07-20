"""Tests for OBD coverage report + DTC catalog (Tasks 6.3 / 6.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from src.analysis.obd_coverage import build_coverage_report, channels_to_pids
from src.config import CanaryConfig
from src.dashboard.api import create_app
from src.decode.dtc_catalog import build_dtc_report, describe_dtc
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def test_channels_to_pids_handles_prefixes_and_unknowns() -> None:
    pids = channels_to_pids({"obd:RPM", "SPEED", "decoded:ECM_RPM", "derived:FUEL_RATE_LPH"})
    assert 0x0C in pids  # RPM
    assert 0x0D in pids  # SPEED
    # decoded:/derived: are not public Mode 01 — ignored.
    assert len(pids) == 2


def test_build_coverage_report_classifies_honestly() -> None:
    # ECU supports RPM (decoded), monitor-status-this-drive-cycle (0x41,
    # intentionally undecoded), and nothing else.
    report = build_coverage_report({0x0C, 0x41})
    assert report.supported_count == 2
    assert report.decoded_count >= 1
    assert report.supported_not_decoded_count == 1
    assert "MONITOR_STATUS_DRIVE_CYCLE" in report.undecoded_supported
    by_pid = {r.pid: r for r in report.rows}
    assert by_pid[0x0C].status == "decoded"
    assert by_pid[0x0C].responded_recently is True
    assert by_pid[0x41].status == "supported_not_decoded"
    assert by_pid[0x05].status == "unsupported"  # coolant not in supported set


def test_coverage_backed_off_and_responded_split() -> None:
    report = build_coverage_report(
        {0x0C, 0x0D},
        responded_pids={0x0C},
        backed_off_pids={0x0D},
    )
    by_pid = {r.pid: r for r in report.rows}
    assert by_pid[0x0C].responded_recently is True
    assert by_pid[0x0D].responded_recently is False
    assert by_pid[0x0D].backed_off is True
    assert report.responded_count == 1
    assert report.backed_off_count == 1


# ---------------------------------------------------------------------------
# DTC catalog
# ---------------------------------------------------------------------------


def test_describe_dtc_curated_and_structural() -> None:
    cat = describe_dtc("P0420")
    assert "Catalyst" in cat.description
    assert cat.system == "Powertrain"
    assert cat.generic is True
    assert cat.curated is True

    # Unknown manufacturer-specific code → structural fallback, still useful.
    unknown = describe_dtc("P1abc")
    assert unknown.curated is False
    assert unknown.generic is False  # second char '1' = manufacturer
    assert "manufacturer-specific" in unknown.description
    assert "P" == unknown.code[0]

    assert describe_dtc("U0100").system == "Network / Communication"


def test_build_dtc_report() -> None:
    report = build_dtc_report(
        stored=["P0420", "P0301"],
        pending=["P0455"],
        permanent=[],
        mil_on=True,
    )
    assert report.dtc_count == 2  # stored only
    assert report.mil_on is True
    assert {d["code"] for d in report.stored} == {"P0420", "P0301"}
    assert report.pending[0]["code"] == "P0455"
    assert report.permanent == []
    # Timestamped + JSON-safe.
    datetime.fromisoformat(report.captured_at)
    assert isinstance(report.to_dict(), dict)


# ---------------------------------------------------------------------------
# /obd/coverage endpoint
# ---------------------------------------------------------------------------

_CONFIG_YAML = """
vehicle: {{make: Nissan, model: Xterra, year: 2008}}
hardware:
  elm327_port: /dev/ttyUSB0
  can: {{interface: virtual, channel: vcan0, bitrate: 500000}}
collection:
  obd_poll_rate_hz: 1.0
  storage_path: {storage}
  session_retention_days: 30
  storage_warning_mb: 500
preprocessing:
  resample_hz: 1.0
  rolling_window_seconds: 5
  operating_modes: {{}}
models:
  isolation_forest: {{enabled: true, n_estimators: 100, contamination: 0.01}}
  lstm_autoencoder: {{enabled: false, sequence_length: 30, latent_dim: 8, threshold_k: 3.0}}
dashboard: {{host: 127.0.0.1, port: 8000}}
"""


def test_obd_coverage_endpoint_infers_from_telemetry(tmp_path: Path) -> None:
    storage = tmp_path / "sessions"
    session = storage / "2026-05-17T120000"
    session.mkdir(parents=True)
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    write_records_to_parquet(
        [
            TelemetryRecord(timestamp=base, source="obd", channel="RPM", value=800),
            TelemetryRecord(timestamp=base, source="obd", channel="COOLANT_TEMP", value=80),
            TelemetryRecord(timestamp=base, source="can", channel="0x1F9", value=1),
        ],
        session,
    )
    cfg = tmp_path / "canary.yaml"
    cfg.write_text(_CONFIG_YAML.format(storage=storage))
    config = CanaryConfig.from_yaml(cfg)
    client = TestClient(create_app(config, anomalies_dir=tmp_path / "anomalies"))
    body = client.get("/obd/coverage").json()
    assert body["supported_count"] == 2  # RPM + COOLANT_TEMP (CAN row ignored)
    by_pid = {r["pid"]: r for r in body["rows"]}
    assert by_pid[0x0C]["status"] == "decoded"
    assert by_pid[0x05]["status"] == "decoded"


def test_obd_coverage_endpoint_empty_when_no_sessions(tmp_path: Path) -> None:
    storage = tmp_path / "sessions"
    storage.mkdir()
    cfg = tmp_path / "canary.yaml"
    cfg.write_text(_CONFIG_YAML.format(storage=storage))
    config = CanaryConfig.from_yaml(cfg)
    client = TestClient(create_app(config, anomalies_dir=tmp_path / "anomalies"))
    body = client.get("/obd/coverage").json()
    assert body["supported_count"] == 0
    assert body["decoded_count"] == 0
    assert len(body["rows"]) > 0  # registry rows still listed (all unsupported)
    assert body["custom_pids"] == []


def test_obd_coverage_endpoint_lists_custom_pids(tmp_path: Path) -> None:
    """Task 2.8a: custom PIDs (not Mode-01 registry PIDs) still surface in
    `/obd/coverage` for the Data Sources panel, separate from the honest
    J1979 supported/decoded counts."""
    from src.collection.custom_pids import CustomPid, CustomPidSet, save_custom_pids

    storage = tmp_path / "sessions"
    storage.mkdir()
    custom_pids_path = tmp_path / "custom_pids.yaml"
    save_custom_pids(
        CustomPidSet(
            custom_pids=[
                CustomPid(
                    name="OIL_PRESS",
                    display_name="Oil pressure",
                    mode=0x22,
                    pid=0x1234,
                    formula="A * 0.5",
                    unit="psi",
                )
            ]
        ),
        custom_pids_path,
    )
    cfg = tmp_path / "canary.yaml"
    cfg.write_text(
        _CONFIG_YAML.format(storage=storage) + f"daemon:\n  custom_pids: {custom_pids_path}\n"
    )
    config = CanaryConfig.from_yaml(cfg)
    client = TestClient(create_app(config, anomalies_dir=tmp_path / "anomalies"))
    body = client.get("/obd/coverage").json()
    assert body["custom_pids"] == [
        {
            "name": "OIL_PRESS",
            "display_name": "Oil pressure",
            "channel": "custom:OIL_PRESS",
            "unit": "psi",
            "category": "custom",
        }
    ]
