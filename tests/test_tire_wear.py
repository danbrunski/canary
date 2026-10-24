"""Tire wear from GPS: the per-drive rolling circumference and its trend."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.analysis.rolling_circumference import (
    METHOD_VERSION,
    MIN_PAIRS,
    measure_rolling_circumference,
)
from src.config import CanaryConfig
from src.control.tires import tire_status
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from src.vehicle.vehicle_ledger import TireRecord, VehicleLedgerStore

GEARS = (4.368, 2.518, 1.743, 1.283, 1.0, 0.769)
FD = 4.08
T0 = 1_790_000_000.0


def _rpm(kph: float, gear: int, circumference: float) -> float:
    return kph * 1000.0 / 60.0 / circumference * GEARS[gear - 1] * FD


def _drive(tmp_path: Path, name: str, *, circumference: float, seconds: int = 240) -> Path:
    """A drive: steady 5th-gear cruise, a clutch-in coast and a shift to 4th."""
    records: list[TelemetryRecord] = []
    for i in range(seconds):
        t = T0 + i
        if i < seconds // 2:  # steady cruise at 90 km/h in 5th
            kph, rpm = 90.0, _rpm(90.0, 5, circumference)
        elif i < seconds // 2 + 30:  # coasting, clutch in: rpm falls to idle
            kph, rpm = 90.0 - 0.5 * (i - seconds // 2), 800.0
        else:  # steady 4th at 70 km/h
            kph, rpm = 70.0, _rpm(70.0, 4, circumference)
        records.append(TelemetryRecord.obd(t, "RPM", rpm, "rpm"))
        records.append(TelemetryRecord.gps(t, "speed", kph, "km/h"))
    session = tmp_path / name
    write_records_to_parquet(records, session, max_rows=100_000)
    return session


def _measure(session: Path, **kwargs: float) -> dict:  # type: ignore[type-arg]
    return measure_rolling_circumference(
        session,
        rpm_channels=["decoded:ECM_RPM", "obd:RPM"],
        gearbox_ratios=GEARS,
        final_drive=FD,
        **kwargs,
    ).to_dict()


def test_a_drive_gives_its_tires_rolling_circumference(tmp_path: Path) -> None:
    result = _measure(_drive(tmp_path, "2026-10-01T120000", circumference=2.559), prior_m=2.6)
    assert result["available"] is True
    assert result["median_m"] == pytest.approx(2.559, rel=1e-4)
    # The first listed rpm source is absent here; the next one is used.
    assert result["rpm_channel"] == "obd:RPM"
    # Cruise in 5th and 4th both count; the clutch-in coast never does.
    assert set(result["gears"]) == {"4", "5"}
    assert result["n_pairs"] >= MIN_PAIRS
    assert result["speed_kph"][0] >= 40.0


def test_too_little_steady_cruise_is_refused_by_name(tmp_path: Path) -> None:
    result = _measure(_drive(tmp_path, "2026-10-01T120000", circumference=2.559, seconds=40))
    assert result["available"] is False
    assert result["reason"] == "too_few_steady_pairs"


def test_a_tire_far_from_the_prior_matches_no_gear(tmp_path: Path) -> None:
    """A different tire size is refused, never fitted to the wrong gear."""
    result = _measure(_drive(tmp_path, "2026-10-01T120000", circumference=2.2), prior_m=2.559)
    assert result["available"] is False
    assert "within 5% of a gear at 2.559 m" in result["detail"]


def test_a_gauge_reading_needs_a_depth() -> None:
    with pytest.raises(ValidationError, match="tread_depth_mm"):
        TireRecord(action="tread_measured")
    assert TireRecord(action="rotated").tread_depth_mm is None


def _measured(storage: Path, session: str, circumference: float) -> None:
    session_dir = storage / session
    session_dir.mkdir(parents=True)
    block = {
        "available": True,
        "method_version": METHOD_VERSION,
        "rpm_channel": "obd:RPM",
        "median_m": circumference,
        "n_pairs": 500,
        "se_pct": 0.03,
    }
    (session_dir / "rolling_circumference.json").write_text(json.dumps(block))


def _ledger(tmp_path: Path) -> VehicleLedgerStore:
    return VehicleLedgerStore(tmp_path / "ledger.jsonl")


MM = 2 * math.pi / 1000.0  # metres of circumference per mm of tread


def test_without_a_gauge_reading_there_is_a_circumference_and_no_depth(tmp_path: Path) -> None:
    storage = tmp_path / "sessions"
    for day in range(1, 6):
        _measured(storage, f"2026-10-0{day}T120000", 2.559)
    status = tire_status(storage, _ledger(tmp_path))
    assert status["current"]["circumference_m"] == pytest.approx(2.559)
    assert status["estimate"] is None
    assert "no gauge reading" in status["caveats"][0]


def test_a_gauge_reading_turns_circumference_into_tread_depth(tmp_path: Path) -> None:
    storage = tmp_path / "sessions"
    # Five drives around the 10 mm gauge reading, then the tire loses 1.5 mm.
    for day in range(1, 6):
        _measured(storage, f"2026-10-0{day}T120000", 2.559)
    for day in range(10, 20):
        _measured(storage, f"2026-10-{day}T120000", 2.559 - 1.5 * MM)
    ledger = _ledger(tmp_path)
    ledger.record(
        occurred_at=date(2026, 10, 3),
        kind="maintenance",
        title="tread gauge",
        tires=TireRecord(action="tread_measured", tread_depth_mm=10.0),
    )
    status = tire_status(storage, ledger)
    assert status["baseline"]["status"] == "fixed"
    assert status["estimate"]["worn_since_baseline_mm"] == pytest.approx(1.5, abs=0.01)
    assert status["estimate"]["tread_depth_mm"] == pytest.approx(8.5, abs=0.01)
    assert status["estimate"]["uncertainty_mm"] > 0


def test_before_any_drive_since_the_reading_the_estimate_is_the_reading(
    tmp_path: Path,
) -> None:
    """The latest drives are the ones that fixed the baseline: their spread is
    not wear (2026-10-04, a reading 13 days after the last drive read 0.23 mm
    "worn")."""
    storage = tmp_path / "sessions"
    spread = (2.556, 2.561, 2.558, 2.563, 2.555, 2.560, 2.557, 2.559, 2.562)
    for day, c in enumerate(spread, start=1):
        _measured(storage, f"2026-09-0{day}T120000", c)
    ledger = _ledger(tmp_path)
    ledger.record(
        occurred_at=date(2026, 9, 20),
        kind="maintenance",
        title="gauge",
        tires=TireRecord(action="tread_measured", tread_depth_mm=12.0),
    )
    status = tire_status(storage, ledger)
    assert status["baseline"]["status"] == "fixed"
    assert status["estimate"]["tread_depth_mm"] == 12.0
    assert status["estimate"]["worn_since_baseline_mm"] == 0.0
    assert status["estimate"]["drives_since_reading"] == 0
    assert any("no measured drive since the gauge reading" in c for c in status["caveats"])


def test_a_rotation_restarts_the_series_and_retires_the_old_reading(tmp_path: Path) -> None:
    storage = tmp_path / "sessions"
    _measured(storage, "2026-10-01T120000", 2.540)
    _measured(storage, "2026-10-12T120000", 2.559)
    ledger = _ledger(tmp_path)
    ledger.record(
        occurred_at=date(2026, 10, 1),
        kind="maintenance",
        title="gauge",
        tires=TireRecord(action="tread_measured", tread_depth_mm=10.0),
    )
    ledger.record(
        occurred_at=date(2026, 10, 10),
        kind="maintenance",
        title="rotation",
        tires=TireRecord(action="rotated"),
    )
    status = tire_status(storage, ledger)
    assert status["series_start"] == "2026-10-10"
    assert [d["session"] for d in status["drives"]] == ["2026-10-12T120000"]
    assert status["estimate"] is None


def test_a_pressure_change_since_the_baseline_is_named(tmp_path: Path) -> None:
    storage = tmp_path / "sessions"
    for day in range(1, 8):
        _measured(storage, f"2026-10-0{day}T120000", 2.559)
    ledger = _ledger(tmp_path)
    ledger.record(
        occurred_at=date(2026, 10, 2),
        kind="maintenance",
        title="gauge",
        tires=TireRecord(action="tread_measured", tread_depth_mm=10.0),
    )
    ledger.record(
        occurred_at=date(2026, 10, 5),
        kind="maintenance",
        title="aired up",
        tires=TireRecord(action="pressure_changed", pressure_psi=40.0),
    )
    status = tire_status(storage, ledger)
    assert status["estimate"] is not None
    assert any("pressure changed" in c for c in status["caveats"])


def test_the_tires_route_serves_the_trend(tmp_path: Path) -> None:
    from src.dashboard.api import create_app
    from tests.configs import isolated_config

    config = isolated_config(tmp_path)
    for day in range(1, 4):
        _measured(config.collection.storage_path, f"2026-10-0{day}T120000", 2.559)
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        vehicle_ledger_store=_ledger(tmp_path),
    )
    response = TestClient(app).get("/control/tires", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["drives"]) == 3
    assert body["current"]["circumference_m"] == pytest.approx(2.559)


def test_the_backfill_measures_sessions_without_a_digest_and_copies_old_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drives the digest refused are the long ones: the backfill measures
    every session, and a 2026-10-03 digest field is copied, not re-measured."""
    from scripts import backfill_rolling_circumference as backfill
    from src.storage.session_products import read_rolling_circumference

    storage = tmp_path / "sessions"
    bare = _drive(storage, "2026-10-01T120000", circumference=2.559)
    held = storage / "2026-10-02T120000"
    held.mkdir()
    old = {"available": True, "method_version": METHOD_VERSION, "median_m": 2.56}
    (held / "digest.json").write_text(json.dumps({"rolling_circumference": old}))
    (storage / "_replay-2026-10-01T120000").mkdir()
    measured: list[str] = []

    def _measure_session(session_dir: Path, _config: object) -> dict:  # type: ignore[type-arg]
        measured.append(session_dir.name)
        return {"available": True, "method_version": METHOD_VERSION, "median_m": 2.559}

    class _Config:
        class collection:  # noqa: N801
            storage_path = storage

    monkeypatch.setattr(CanaryConfig, "from_yaml", staticmethod(lambda _p: _Config))
    monkeypatch.setattr(backfill, "resolve_profile_for_tooling", lambda *_a: None)
    monkeypatch.setattr(backfill, "scope_config", lambda base, _p: base)
    monkeypatch.setattr(backfill, "measure_session", _measure_session)

    assert backfill.main(["--config", "unused.yaml"]) == 0
    assert measured == [bare.name]
    assert read_rolling_circumference(bare)["median_m"] == 2.559  # type: ignore[index]
    assert read_rolling_circumference(held) == old
    assert read_rolling_circumference(storage / "_replay-2026-10-01T120000") is None
    # A second run finds both current and measures nothing.
    assert backfill.main(["--config", "unused.yaml"]) == 0
    assert measured == [bare.name]
