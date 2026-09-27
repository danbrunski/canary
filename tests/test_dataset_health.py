"""Tests for dataset health analysis (Task 6.23)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import scripts.dataset_health as cli
from src.config import CanaryConfig
from src.dashboard.api import create_app
from src.datasets.health import analyze_corpus, analyze_session
from src.preprocessing.loader import discover_sessions
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from src.vehicle.vehicle_ledger import VehicleLedgerStore
from tests.configs import write_isolated_config


def _make_session(
    storage: Path,
    session_id: str,
    *,
    seconds: int = 70,
    channels: tuple[str, ...] = ("RPM", "SPEED"),
) -> Path:
    sdir = storage / session_id
    sdir.mkdir(parents=True)
    base = datetime(2026, 5, 11, 22, 24, 47, tzinfo=UTC)
    recs: list[TelemetryRecord] = []
    for i in range(seconds):
        ts = base + timedelta(seconds=i)
        for ch in channels:
            recs.append(
                TelemetryRecord(timestamp=ts, source="obd", channel=ch, value=float(i + 1), unit="")
            )
    write_records_to_parquet(recs, sdir)
    return sdir


def _config(tmp_path: Path) -> tuple[CanaryConfig, Path]:
    storage = tmp_path / "sessions"
    storage.mkdir()
    # The CLI tests read tmp_path/canary.yaml, so the config is written too.
    return CanaryConfig.from_yaml(write_isolated_config(tmp_path, storage=storage)), storage


# ---------------------------------------------------------------------------
# analyze_session
# ---------------------------------------------------------------------------


def test_analyze_session_basic(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    sdir = _make_session(storage, "2026-05-11T222447", seconds=70)
    h = analyze_session(sdir, config)
    assert h.row_count == 70
    assert h.duration_seconds == 69.0
    assert "obd:RPM" in h.channels and "obd:SPEED" in h.channels
    assert h.mode_coverage == {"all": 70}  # no segmenter inputs → single mode
    assert h.value_ranges["speed"] == {"min": 1.0, "max": 70.0}
    assert h.warnings == []  # 70s, both key channels → clean


def test_analyze_session_flags_short_and_missing_key(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    sdir = _make_session(storage, "2026-05-11T222447", seconds=20, channels=("RPM",))
    h = analyze_session(sdir, config)
    assert any("short capture" in w for w in h.warnings)
    assert any("missing key channel obd:SPEED" in w for w in h.warnings)


def test_analyze_session_empty_is_safe(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    sdir = storage / "2026-05-11T222447"
    sdir.mkdir(parents=True)
    h = analyze_session(sdir, config)
    assert h.row_count == 0
    assert any("no telemetry" in w for w in h.warnings)


def test_parse_error_ratio_and_pinned(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    sdir = _make_session(storage, "2026-05-11T222447")
    (sdir / "manifest.json").write_text(json.dumps({"frames_recorded": 100, "parse_errors": 250}))
    (sdir / "control.json").write_text(json.dumps({"pinned": True, "training_candidate": True}))
    h = analyze_session(sdir, config)
    assert h.parse_error_ratio == 2.5
    assert h.pinned is True and h.training_candidate is True
    assert any("parse_errors exceed frames" in w for w in h.warnings)


# ---------------------------------------------------------------------------
# analyze_corpus + readiness
# ---------------------------------------------------------------------------


def test_corpus_not_ready_too_thin(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-05-11T222447", seconds=70)
    report = analyze_corpus(discover_sessions(storage), config)
    assert report.readiness.ready is False
    assert any("usable sessions" in r for r in report.readiness.reasons)


def test_corpus_ready_when_thick_and_clean(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    for i in range(3):
        _make_session(storage, f"2026-05-1{i + 1}T222447", seconds=70)
    report = analyze_corpus(discover_sessions(storage), config, min_sessions=3, min_total_rows=100)
    assert report.readiness.ready is True
    assert report.readiness.reasons == []
    assert report.total_rows == 210
    assert report.channel_availability["obd:RPM"] == 1.0
    assert report.mode_coverage == {"all": 210}


def test_corpus_not_ready_missing_key_channel(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    for i in range(3):
        _make_session(storage, f"2026-05-1{i + 1}T222447", seconds=70, channels=("RPM",))
    report = analyze_corpus(discover_sessions(storage), config, min_total_rows=10)
    assert report.readiness.ready is False
    assert any("obd:SPEED absent" in r for r in report.readiness.reasons)


# ---------------------------------------------------------------------------
# corpus_boundary bisection note (Task 3.5)
# ---------------------------------------------------------------------------


def test_corpus_boundary_bisecting_the_corpus_adds_a_warning(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-05-01T000000", seconds=70)  # before boundary
    _make_session(storage, "2026-06-15T000000", seconds=70)  # after boundary
    report = analyze_corpus(discover_sessions(storage), config, corpus_boundary=date(2026, 6, 1))
    assert any("bisected" in w for w in report.warnings)


def test_corpus_boundary_before_every_session_is_not_a_bisection(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-06-10T000000", seconds=70)
    _make_session(storage, "2026-06-15T000000", seconds=70)
    report = analyze_corpus(discover_sessions(storage), config, corpus_boundary=date(2026, 1, 1))
    assert not any("bisected" in w for w in report.warnings)


def test_no_boundary_means_no_bisection_note(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-05-01T000000", seconds=70)
    _make_session(storage, "2026-06-15T000000", seconds=70)
    report = analyze_corpus(discover_sessions(storage), config)
    assert not any("bisected" in w for w in report.warnings)


def test_dataset_health_endpoint_surfaces_bisection_note(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-05-01T000000", seconds=70)
    _make_session(storage, "2026-06-15T000000", seconds=70)
    ledger = VehicleLedgerStore(tmp_path / "vehicle_ledger.jsonl")
    ledger.record(
        occurred_at=date(2026, 6, 1), kind="mod", title="Intake swap", corpus_boundary=True
    )
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        vehicle_ledger_store=ledger,
    )
    client = TestClient(app)
    body = client.get("/control/datasets/health", headers={"Authorization": "Bearer secret"}).json()
    assert any("bisected" in w for w in body["warnings"])


# ---------------------------------------------------------------------------
# API + CLI
# ---------------------------------------------------------------------------


def test_dataset_health_endpoint(tmp_path: Path) -> None:
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-05-11T222447", seconds=70)
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    client = TestClient(app)
    assert client.get("/control/datasets/health").status_code == 401
    body = client.get("/control/datasets/health", headers={"Authorization": "Bearer secret"}).json()
    assert body["total_rows"] == 70
    assert "readiness" in body and body["readiness"]["ready"] is False


def test_cli_text_and_json(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config, storage = _config(tmp_path)
    _make_session(storage, "2026-05-11T222447", seconds=70)
    cfg_path = tmp_path / "canary.yaml"
    rc = cli.main(["--config", str(cfg_path)])
    assert rc == 2  # not ready (one session)
    assert "DATASET HEALTH" in capsys.readouterr().out
    rc = cli.main(["--config", str(cfg_path), "--json"])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_rows"] == 70


def test_cli_surfaces_bisection_note(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    storage = tmp_path / "sessions"
    storage.mkdir()
    ledger_path = tmp_path / "vehicle_ledger.jsonl"
    cfg_path = write_isolated_config(
        tmp_path, storage=storage, daemon={"vehicle_ledger": ledger_path}
    )
    config = CanaryConfig.from_yaml(cfg_path)
    _make_session(storage, "2026-05-01T000000", seconds=70)
    _make_session(storage, "2026-06-15T000000", seconds=70)
    VehicleLedgerStore(ledger_path).record(
        occurred_at=date(2026, 6, 1), kind="mod", title="Intake swap", corpus_boundary=True
    )
    assert config.daemon.vehicle_ledger == ledger_path  # sanity: override took
    rc = cli.main(["--config", str(cfg_path), "--min-sessions", "1"])
    assert rc in (0, 2)
    assert "bisected" in capsys.readouterr().out
