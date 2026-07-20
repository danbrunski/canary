"""Tests for the dashboard REST endpoints (Task 5.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.events import AnomalyEvent
from src.config import CanaryConfig
from src.dashboard.api import (
    StatusSnapshot,
    build_status,
    create_app,
    echo_config,
    read_anomalies_since,
    read_latest_telemetry_rows,
)
from src.storage.parquet_writer import TELEMETRY_SCHEMA

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config_yaml(storage_path: Path) -> str:
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
    channel: vcan0
    bitrate: 500000
collection:
  obd_poll_rate_hz: 1
  storage_path: {storage_path}
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
    enabled: true
    n_estimators: 100
    contamination: 0.01
  lstm_autoencoder:
    enabled: true
    sequence_length: 60
    latent_dim: 16
    threshold_k: 3.0
dashboard:
  host: 127.0.0.1
  port: 8000
"""


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    storage = tmp_path / "storage"
    storage.mkdir()
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(storage))
    return CanaryConfig.from_yaml(cfg_path)


@pytest.fixture
def anomalies_dir(tmp_path: Path) -> Path:
    return tmp_path / "anomalies"


def _make_session_with_parquet(storage_root: Path, name: str, rows: int = 3) -> Path:
    """Drop a minimal Parquet chunk into ``storage_root/name/``."""
    session = storage_root / name
    session.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [
            {
                "timestamp": pa.scalar(
                    1_778_000_000_000_000 + i * 1_000_000,
                    type=pa.timestamp("us", tz="UTC"),
                ).as_py(),
                "source": "obd",
                "channel": "RPM",
                "value_number": float(700 + i),
                "value_text": None,
                "value_bool": None,
                "unit": "rpm",
                "payload": None,
                "metadata_json": None,
            }
            for i in range(rows)
        ],
        schema=TELEMETRY_SCHEMA,
    )
    pq.write_table(table, session / "telemetry-00000.parquet")
    return session


# ---------------------------------------------------------------------------
# build_status — unit
# ---------------------------------------------------------------------------


def test_build_status_no_sessions_no_models(config: CanaryConfig) -> None:
    snap = build_status(config)
    assert snap.latest_session is None
    assert snap.latest_session_path is None
    assert snap.model_versions == {}
    assert "Nissan Xterra" in snap.vehicle
    assert snap.free_mb > 0
    # No emitter passed → counters absent (None)
    assert snap.emitter_counters is None


def test_build_status_surfaces_emitter_counters(config: CanaryConfig, tmp_path: Path) -> None:
    """When the daemon passes the emitter, /status carries its counters."""
    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.event_logger import AnomalyEventLogger

    logger = AnomalyEventLogger(tmp_path / "anomalies")
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    # Hand-modify the counters to a non-zero state — emit_lstm would otherwise
    # require a result fixture, and the build_status path is what we're testing.
    emitter.counters.emitted = 4
    emitter.counters.suppressed_not_anomaly = 12
    emitter.counters.suppressed_dedupe = 1

    snap = build_status(config, emitter=emitter)
    assert snap.emitter_counters == {
        "emitted": 4,
        "suppressed_not_anomaly": 12,
        "suppressed_dedupe": 1,
        "suppressed_tuning": 0,
        "suppressed_persistence": 0,
        "skipped_vehicle_state": 0,
    }


def test_build_status_gps_and_trip_absent_without_tracker(config: CanaryConfig) -> None:
    snap = build_status(config)
    assert snap.gps is None
    assert snap.trip is None


def test_build_status_surfaces_gps_and_trip(config: CanaryConfig) -> None:
    """When the daemon wires a GPS tracker, /status carries fix + odometer."""
    from src.analysis.gps_tracker import GpsTracker
    from src.telemetry import TelemetryRecord

    tracker = GpsTracker()
    base = 1000.0
    tracker.observe(TelemetryRecord.gps(base, "fix_quality", 1, ""))
    tracker.observe(TelemetryRecord.gps(base, "num_satellites", 7, ""))
    tracker.observe(TelemetryRecord.gps(base, "speed", 36.0, "km/h"))
    tracker.observe(TelemetryRecord.gps(base + 1.0, "speed", 36.0, "km/h"))

    snap = build_status(config, gps_tracker=tracker)
    assert snap.gps is not None
    assert snap.gps["fix_quality"] == 1
    assert snap.gps["num_satellites"] == 7
    assert snap.trip is not None
    assert snap.trip["distance_km"] == pytest.approx(0.01, abs=1e-6)
    payload = snap.to_dict()
    assert payload["gps"]["fix_quality"] == 1
    assert payload["trip"]["distance_km"] == pytest.approx(0.01, abs=1e-6)


def test_read_bindings_for_display_returns_empty_when_file_missing(tmp_path: Path) -> None:
    from src.dashboard.api import read_bindings_for_display

    assert read_bindings_for_display(tmp_path / "missing.yaml") == {}


def test_read_bindings_for_display_filters_confidence_and_keys_by_channel(
    tmp_path: Path,
) -> None:
    """Display projection: keyed by `decoded:<NAME>`, low-confidence excluded."""
    from src.dashboard.api import read_bindings_for_display

    yaml = """
schema_version: 1
vehicle: {make: Nissan, model: Xterra, year: 2008}
signals:
  - name: COOLANT_TEMP
    module: ECM
    can_id: 0x551
    byte_start: 0
    byte_length: 1
    scale: 1.0
    offset: -40.0
    unit: degC
    confidence: confirmed
    plausible_min: -10
    plausible_max: 130
  - name: ENGINE_RPM
    module: ECM
    can_id: 0x1F9
    byte_start: 2
    byte_length: 2
    scale: 0.125
    offset: 0
    unit: rpm
    confidence: high
  - name: MAYBE_THIS
    module: UNKNOWN
    can_id: 0x300
    byte_start: 0
    byte_length: 1
    confidence: hypothesized
"""
    path = tmp_path / "bindings.yaml"
    path.write_text(yaml)
    out = read_bindings_for_display(path)

    # decoded:* keys; hypothesized excluded by default
    assert set(out.keys()) == {"decoded:COOLANT_TEMP", "decoded:ENGINE_RPM"}
    assert out["decoded:COOLANT_TEMP"] == {
        "name": "COOLANT_TEMP",
        "scale": 1.0,
        "offset": -40.0,
        "unit": "degC",
        "confidence": "confirmed",
        "plausible_min": -10.0,
        "plausible_max": 130.0,
    }
    assert out["decoded:ENGINE_RPM"]["scale"] == 0.125


def test_read_can_coverage_catalogs_signals_with_can_ids(tmp_path: Path) -> None:
    """CAN broadcast catalog: one row per signal carrying its source CAN ID,
    sorted by (can_id, name), with channel keys the frontend looks up live."""
    from src.dashboard.api import read_can_coverage

    yaml = """
schema_version: 1
vehicle: {make: Nissan, model: Xterra, year: 2008}
signals:
  - name: COOLANT_TEMP
    module: ECM
    can_id: 0x551
    byte_start: 0
    byte_length: 1
    unit: degC
    confidence: confirmed
  - name: ECM_RPM
    module: ECM
    can_id: 0x1F9
    byte_start: 2
    byte_length: 2
    scale: 0.125
    offset: 0
    unit: rpm
    confidence: high
"""
    path = tmp_path / "bindings.yaml"
    path.write_text(yaml)
    empty_dbc = tmp_path / "dbc"
    empty_dbc.mkdir()

    cov = read_can_coverage(path, dbc_dir=empty_dbc)

    assert cov["signal_count"] == 2
    assert cov["decoded_id_count"] == 2
    assert set(cov["decoded_ids_hex"]) == {"0x1F9", "0x551"}
    # sorted by (can_id, name): 0x1F9 (505) before 0x551 (1361)
    assert [s["can_id_hex"] for s in cov["signals"]] == ["0x1F9", "0x551"]
    rpm = next(s for s in cov["signals"] if s["name"] == "ECM_RPM")
    assert rpm["can_id_hex"] == "0x1F9"
    assert rpm["channel"] == "decoded:ECM_RPM"
    assert rpm["source"] == "decoded"
    assert rpm["unit"] == "rpm"


def test_bindings_endpoint_returns_display_payload(
    config: CanaryConfig, anomalies_dir: Path, tmp_path: Path
) -> None:
    yaml = """
schema_version: 1
signals:
  - name: RPM
    module: ECM
    can_id: 0x1F9
    byte_start: 2
    byte_length: 2
    scale: 0.125
    offset: 0
    unit: rpm
    confidence: confirmed
"""
    path = tmp_path / "bindings.yaml"
    path.write_text(yaml)
    app = create_app(config, anomalies_dir=anomalies_dir, bindings_path=path)
    client = TestClient(app)
    r = client.get("/bindings")
    assert r.status_code == 200
    assert r.json()["decoded:RPM"]["scale"] == 0.125


def test_bindings_endpoint_returns_empty_when_file_missing(
    config: CanaryConfig, anomalies_dir: Path, tmp_path: Path
) -> None:
    app = create_app(config, anomalies_dir=anomalies_dir, bindings_path=tmp_path / "absent.yaml")
    client = TestClient(app)
    r = client.get("/bindings")
    assert r.status_code == 200
    assert r.json() == {}


def test_dids_endpoint(config: CanaryConfig, anomalies_dir: Path, tmp_path: Path) -> None:
    yaml = """
schema_version: 1
vehicle: {make: Nissan, model: Xterra, year: 2008}
access:
  ecu_request_id: 0x7E0
  ecu_response_id: 0x7E8
  session: 0xC0
dids:
  - did: 0x1208
    name: INJECTOR_PULSE_WIDTH
    byte_start: 0
    byte_length: 2
    confidence: high
"""
    path = tmp_path / "dids.yaml"
    path.write_text(yaml)
    client = TestClient(create_app(config, anomalies_dir=anomalies_dir, dids_path=path))
    body = client.get("/dids").json()
    assert body["schema_version"] == 1
    assert body["dids"][0]["name"] == "INJECTOR_PULSE_WIDTH"

    missing = create_app(config, anomalies_dir=anomalies_dir, dids_path=tmp_path / "absent.yaml")
    r = TestClient(missing).get("/dids")
    assert r.status_code == 200
    assert r.json()["dids"] == []


def test_read_behavior_models_returns_empty_when_file_missing(tmp_path: Path) -> None:
    """A daemon brought up before any `refit_behavior_models.py` run should
    surface "no surfaces" cleanly, not 500. The dashboard renders this as
    "no behavior models refit yet."""
    from src.dashboard.api import read_behavior_models

    payload = read_behavior_models(tmp_path / "missing.yaml")
    assert payload == {"schema_version": None, "models": []}


def test_read_behavior_models_round_trips_yaml(tmp_path: Path) -> None:
    """Persisted behavior_models.yaml deserializes back through the loader
    cleanly and the JSON-mode dump is dashboard-ready."""
    from src.dashboard.api import read_behavior_models
    from src.decode.behavior_models import (
        Axis1D,
        BehaviorModel1D,
        BehaviorModels,
        PolynomialFit,
        dump_behavior_models,
    )

    models = BehaviorModels(
        schema_version=1,
        models=[
            BehaviorModel1D(
                name="coolant_warmup",
                kind="surface_1d",
                output="obd:COOLANT_TEMP",
                output_unit="degC",
                axis=Axis1D(
                    name="t",
                    channel="obd:RUN_TIME",
                    unit="s",
                    domain=(0.0, 600.0),
                ),
                fit=PolynomialFit(
                    kind="poly",
                    coefficients=[25.0, 0.15],
                    sample_count=12_450,
                    r_squared=0.97,
                    sessions=["2026-05-13T005522"],
                    fitted_at="2026-05-13T15:00:00+00:00",
                    training_data_hash="sha256:abc",
                ),
                version=2,
            )
        ],
    )
    path = tmp_path / "behavior_models.yaml"
    dump_behavior_models(models, path)
    payload = read_behavior_models(path)
    assert payload["schema_version"] == 1
    assert len(payload["models"]) == 1
    [model] = payload["models"]
    assert model["name"] == "coolant_warmup"
    assert model["version"] == 2
    assert model["fit"]["r_squared"] == 0.97


def test_behavior_models_endpoint_returns_empty_when_missing(
    config: CanaryConfig, anomalies_dir: Path, tmp_path: Path
) -> None:
    app = create_app(
        config,
        anomalies_dir=anomalies_dir,
        behavior_models_path=tmp_path / "absent.yaml",
    )
    client = TestClient(app)
    response = client.get("/models/behavior")
    assert response.status_code == 200
    assert response.json() == {"schema_version": None, "models": []}


def test_behavior_models_endpoint_returns_yaml_content(
    config: CanaryConfig, anomalies_dir: Path, tmp_path: Path
) -> None:
    """End-to-end: refit dumps yaml → endpoint surfaces it for the dashboard."""
    from src.decode.behavior_models import (
        Axis1D,
        BehaviorModel1D,
        BehaviorModels,
        PolynomialFit,
        dump_behavior_models,
    )

    behavior_path = tmp_path / "behavior_models.yaml"
    dump_behavior_models(
        BehaviorModels(
            schema_version=1,
            models=[
                BehaviorModel1D(
                    name="coolant_warmup",
                    kind="surface_1d",
                    output="obd:COOLANT_TEMP",
                    axis=Axis1D(name="t", channel="obd:RUN_TIME", domain=(0.0, 600.0)),
                    fit=PolynomialFit(
                        kind="poly",
                        coefficients=[25.0, 0.15],
                        sample_count=100,
                        r_squared=0.95,
                        fitted_at="now",
                    ),
                )
            ],
        ),
        behavior_path,
    )
    app = create_app(config, anomalies_dir=anomalies_dir, behavior_models_path=behavior_path)
    client = TestClient(app)
    response = client.get("/models/behavior")
    assert response.status_code == 200
    body = response.json()
    assert len(body["models"]) == 1
    assert body["models"][0]["name"] == "coolant_warmup"


def test_create_app_mounts_static_when_dist_exists(
    config: CanaryConfig, anomalies_dir: Path, tmp_path: Path
) -> None:
    """When the SPA has been built, GET / returns the index.html."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>canary-test</title>")
    app = create_app(config, anomalies_dir=anomalies_dir, static_dir=static_dir)
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "canary-test" in response.text


def test_create_app_skips_static_when_dist_missing(
    config: CanaryConfig, anomalies_dir: Path, tmp_path: Path
) -> None:
    """Until `pnpm build`, /  returns 404 — the API routes still work."""
    app = create_app(config, anomalies_dir=anomalies_dir, static_dir=tmp_path / "absent")
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 404
    # /status still works
    assert client.get("/status").status_code == 200


def test_create_app_dev_cors_allows_vite_origin(config: CanaryConfig, anomalies_dir: Path) -> None:
    """``enable_dev_cors=True`` permits the Vite dev origin so the frontend
    can call the backend directly from localhost:5173."""
    app = create_app(config, anomalies_dir=anomalies_dir, enable_dev_cors=True)
    client = TestClient(app)
    response = client.get("/status", headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_status_endpoint_includes_emitter_counters(config: CanaryConfig, tmp_path: Path) -> None:
    """End-to-end through TestClient: the /status response carries the counters dict."""
    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.event_logger import AnomalyEventLogger

    logger = AnomalyEventLogger(tmp_path / "anomalies-status")
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    emitter.counters.emitted = 2
    app = create_app(config, anomalies_dir=tmp_path / "anomalies-status", emitter=emitter)
    client = TestClient(app)
    response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["emitter_counters"]["emitted"] == 2


def test_build_status_with_session_and_model_manifest(config: CanaryConfig, tmp_path: Path) -> None:
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T120000")
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T130000")

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "isolation_forest.manifest.json").write_text(
        json.dumps({"version": "2026-05-13"})
    )

    snap = build_status(config, models_dir=models_dir)
    # newest session wins
    assert snap.latest_session == "2026-05-13T130000"
    assert snap.model_versions == {"isolation_forest": "2026-05-13"}


def test_status_snapshot_to_dict_rounds_free_mb() -> None:
    snap = StatusSnapshot(
        storage_path="/x",
        free_mb=1234.56789,
        latest_session=None,
        latest_session_path=None,
        model_versions={},
        vehicle="Test Car",
    )
    assert snap.to_dict()["free_mb"] == 1234.6


def test_build_status_ignores_unreadable_manifest(config: CanaryConfig, tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "broken.manifest.json").write_text("{not json")
    snap = build_status(config, models_dir=models_dir)
    assert snap.model_versions == {"broken": None}
    assert snap.model_provenance == {"broken": None}


# ---------------------------------------------------------------------------
# model_provenance — Task 6.5
# ---------------------------------------------------------------------------


def test_build_status_surfaces_loaded_model_provenance(
    config: CanaryConfig, tmp_path: Path
) -> None:
    models_dir = tmp_path / "models"
    if_dir = models_dir / "isolation_forest"
    if_dir.mkdir(parents=True)
    (if_dir / "model.manifest.json").write_text(
        json.dumps(
            {
                "artifact_format_version": 1,
                "model_type": "isolation_forest",
                "model_version": "isolation_forest_2026-07-19T000000Z",
                "created_at": "2026-07-19T00:00:00Z",
                "training_data_hash": "sha256:abc123",
                "training_sessions": ["2026-07-01T120000", "2026-07-02T080000"],
                "input_rows": 4096,
                "hyperparams": {},
                "schema": {},
            }
        )
    )

    snap = build_status(config, models_dir=models_dir)
    assert snap.model_provenance == {
        "isolation_forest": {
            "model_version": "isolation_forest_2026-07-19T000000Z",
            "created_at": "2026-07-19T00:00:00Z",
            "training_data_hash": "sha256:abc123",
            "training_sessions": ["2026-07-01T120000", "2026-07-02T080000"],
            "artifact_format_version": 1,
        }
    }
    # Also visible in the /status payload dict (what get_status mirrors).
    assert snap.to_dict()["model_provenance"] == snap.model_provenance


def test_build_status_model_provenance_none_without_models_dir(config: CanaryConfig) -> None:
    snap = build_status(config)
    assert snap.model_provenance is None


# ---------------------------------------------------------------------------
# read_anomalies_since — unit
# ---------------------------------------------------------------------------


def test_read_anomalies_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    assert read_anomalies_since(tmp_path / "nope") == []


def test_read_anomalies_returns_events_in_timestamp_order(anomalies_dir: Path) -> None:
    with AnomalyEventLogger(anomalies_dir) as logger:
        logger.log(_event("2026-05-13T12:00:00+00:00", score=1.0))
        logger.log(_event("2026-05-13T12:01:00+00:00", score=2.0))
        logger.log(_event("2026-05-12T23:59:00+00:00", score=0.5))  # prior day file

    events = read_anomalies_since(anomalies_dir)
    timestamps = [e["timestamp"] for e in events]
    assert timestamps == sorted(timestamps)


def test_read_anomalies_filters_by_since(anomalies_dir: Path) -> None:
    with AnomalyEventLogger(anomalies_dir) as logger:
        logger.log(_event("2026-05-13T12:00:00+00:00", score=1.0))
        logger.log(_event("2026-05-13T12:01:00+00:00", score=2.0))
        logger.log(_event("2026-05-13T12:02:00+00:00", score=3.0))

    after = read_anomalies_since(anomalies_dir, since="2026-05-13T12:00:30+00:00")
    assert [e["score"] for e in after] == [2.0, 3.0]


def test_read_anomalies_raises_on_bad_since(anomalies_dir: Path) -> None:
    anomalies_dir.mkdir()
    with pytest.raises(ValueError):
        read_anomalies_since(anomalies_dir, since="not-a-date")


# ---------------------------------------------------------------------------
# read_latest_telemetry_rows — unit
# ---------------------------------------------------------------------------


def test_telemetry_latest_returns_empty_when_no_session(config: CanaryConfig) -> None:
    assert read_latest_telemetry_rows(config.collection.storage_path) == []


def test_telemetry_latest_returns_newest_session_newest_chunk(config: CanaryConfig) -> None:
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T120000", rows=2)
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T130000", rows=3)
    rows = read_latest_telemetry_rows(config.collection.storage_path, n=10)
    assert len(rows) == 3
    assert all(r["source"] == "obd" for r in rows)
    assert all(r["channel"] == "RPM" for r in rows)
    # the helper collapses (value_number, value_text, value_bool) → "value"
    assert {"value", "unit", "timestamp"} <= set(rows[0])


def test_telemetry_latest_truncates_to_n(config: CanaryConfig) -> None:
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T120000", rows=10)
    rows = read_latest_telemetry_rows(config.collection.storage_path, n=3)
    assert len(rows) == 3
    # the *last* 3 rows are returned (tail)
    assert rows[0]["value"] == 707.0


def _make_session_with_chunks(
    storage_root: Path, name: str, n_chunks: int, rows_per_chunk: int = 3
) -> Path:
    """Write `n_chunks` Parquet chunks; row `value_number` == its global
    index so cross-chunk ordering is verifiable."""
    session = storage_root / name
    session.mkdir(parents=True, exist_ok=True)
    counter = 0
    for c in range(n_chunks):
        recs = []
        for _ in range(rows_per_chunk):
            recs.append(
                {
                    "timestamp": pa.scalar(
                        1_778_000_000_000_000 + counter * 1_000_000,
                        type=pa.timestamp("us", tz="UTC"),
                    ).as_py(),
                    "source": "obd",
                    "channel": "RPM",
                    "value_number": float(counter),
                    "value_text": None,
                    "value_bool": None,
                    "unit": "rpm",
                    "payload": None,
                    "metadata_json": None,
                }
            )
            counter += 1
        pq.write_table(
            pa.Table.from_pylist(recs, schema=TELEMETRY_SCHEMA),
            session / f"telemetry-{c:05d}.parquet",
        )
    return session


def test_telemetry_latest_spans_chunk_boundaries(config: CanaryConfig) -> None:
    # 4 chunks × 3 rows = 12; the last 7 must cross ≥2 chunk files, in order.
    _make_session_with_chunks(config.collection.storage_path, "2026-05-13T120000", 4, 3)
    rows = read_latest_telemetry_rows(config.collection.storage_path, n=7)
    assert [r["value"] for r in rows] == [5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0]


def test_telemetry_latest_n_over_total_returns_all_across_chunks(
    config: CanaryConfig,
) -> None:
    _make_session_with_chunks(config.collection.storage_path, "2026-05-13T120000", 4, 3)
    rows = read_latest_telemetry_rows(config.collection.storage_path, n=100)
    assert [r["value"] for r in rows] == [float(i) for i in range(12)]


# ---------------------------------------------------------------------------
# echo_config
# ---------------------------------------------------------------------------


def test_echo_config_round_trips(config: CanaryConfig) -> None:
    payload = echo_config(config)
    assert payload["vehicle"]["model"] == "Xterra"
    assert payload["collection"]["obd_poll_rate_hz"] == 1
    # JSON-safe — no Path objects
    json.dumps(payload)


# ---------------------------------------------------------------------------
# FastAPI integration — full route surface
# ---------------------------------------------------------------------------


def _client(config: CanaryConfig, anomalies_dir: Path, models_dir: Path | None = None):
    app = create_app(config, anomalies_dir=anomalies_dir, models_dir=models_dir)
    return TestClient(app)


def test_status_endpoint(config: CanaryConfig, anomalies_dir: Path) -> None:
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T120000")
    response = _client(config, anomalies_dir).get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["latest_session"] == "2026-05-13T120000"
    assert "Nissan Xterra" in body["vehicle"]


def test_config_endpoint(config: CanaryConfig, anomalies_dir: Path) -> None:
    response = _client(config, anomalies_dir).get("/config")
    assert response.status_code == 200
    assert response.json()["vehicle"]["model"] == "Xterra"


def test_anomalies_endpoint_empty(config: CanaryConfig, anomalies_dir: Path) -> None:
    response = _client(config, anomalies_dir).get("/anomalies")
    assert response.status_code == 200
    assert response.json() == {"events": [], "count": 0}


def test_anomalies_endpoint_returns_events(config: CanaryConfig, anomalies_dir: Path) -> None:
    with AnomalyEventLogger(anomalies_dir) as logger:
        logger.log(_event("2026-05-13T12:00:00+00:00", score=1.0))
        logger.log(_event("2026-05-13T12:01:00+00:00", score=2.0))

    response = _client(config, anomalies_dir).get(
        "/anomalies", params={"since": "2026-05-13T12:00:30+00:00"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["events"][0]["score"] == 2.0


def test_anomalies_endpoint_400_on_bad_since(config: CanaryConfig, anomalies_dir: Path) -> None:
    anomalies_dir.mkdir()
    response = _client(config, anomalies_dir).get("/anomalies", params={"since": "garbage"})
    assert response.status_code == 400
    assert "bad `since`" in response.json()["detail"]


def test_telemetry_latest_endpoint(config: CanaryConfig, anomalies_dir: Path) -> None:
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T120000", rows=4)
    response = _client(config, anomalies_dir).get("/telemetry/latest", params={"n": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert all("value" in row for row in body["rows"])


def test_telemetry_latest_rejects_silly_n(config: CanaryConfig, anomalies_dir: Path) -> None:
    response = _client(config, anomalies_dir).get("/telemetry/latest", params={"n": 0})
    assert response.status_code == 422  # FastAPI validation
    response = _client(config, anomalies_dir).get("/telemetry/latest", params={"n": 1_000_000})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# helper: build an event
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket streams (Task 5.4)
# ---------------------------------------------------------------------------


def test_ws_telemetry_pushes_published_messages(config: CanaryConfig, anomalies_dir: Path) -> None:
    from src.dashboard.broadcast import Broadcaster

    bus = Broadcaster()
    app = create_app(config, anomalies_dir=anomalies_dir, telemetry_broadcaster=bus)
    client = TestClient(app)
    with client.websocket_connect("/ws/telemetry") as ws:
        # Wait until our subscriber registered, then publish
        # (the TestClient is synchronous; the subscribe happens during accept()).
        import time

        for _ in range(50):
            if bus.subscriber_count == 1:
                break
            time.sleep(0.01)
        bus.publish({"timestamp": "now", "channel": "RPM", "value": 700})
        message = ws.receive_json()
        assert message == {"timestamp": "now", "channel": "RPM", "value": 700}


def test_ws_anomalies_pushes_published_events(config: CanaryConfig, anomalies_dir: Path) -> None:
    from src.dashboard.broadcast import Broadcaster

    bus = Broadcaster()
    app = create_app(config, anomalies_dir=anomalies_dir, anomalies_broadcaster=bus)
    client = TestClient(app)
    with client.websocket_connect("/ws/anomalies") as ws:
        import time

        for _ in range(50):
            if bus.subscriber_count == 1:
                break
            time.sleep(0.01)
        bus.publish({"timestamp": "now", "source": "lstm_autoencoder", "score": 5.0})
        message = ws.receive_json()
        assert message["source"] == "lstm_autoencoder"


def test_ws_scores_pushes_published_scores(config: CanaryConfig, anomalies_dir: Path) -> None:
    from src.dashboard.broadcast import Broadcaster

    bus = Broadcaster()
    app = create_app(config, anomalies_dir=anomalies_dir, scores_broadcaster=bus)
    client = TestClient(app)
    with client.websocket_connect("/ws/scores") as ws:
        import time

        for _ in range(50):
            if bus.subscriber_count == 1:
                break
            time.sleep(0.01)
        bus.publish(
            {
                "type": "score",
                "source": "isolation_forest",
                "score": 0.0,
                "threshold": None,
                "operating_mode": "idle",
                "model_version": "if_v1",
                "timestamp": "now",
                "missing_features": False,
            }
        )
        message = ws.receive_json()
        assert message["source"] == "isolation_forest"
        assert message["score"] == 0.0  # a normal 0 must come through, not be hidden


def test_ws_disconnect_removes_subscriber(config: CanaryConfig, anomalies_dir: Path) -> None:
    from src.dashboard.broadcast import Broadcaster

    bus = Broadcaster()
    app = create_app(config, anomalies_dir=anomalies_dir, telemetry_broadcaster=bus)
    client = TestClient(app)
    with client.websocket_connect("/ws/telemetry") as ws:
        import time

        for _ in range(50):
            if bus.subscriber_count == 1:
                break
            time.sleep(0.01)
        assert bus.subscriber_count == 1
        ws.close()
    # subscriber count drops after the handler finishes its `finally` block
    import time

    for _ in range(50):
        if bus.subscriber_count == 0:
            break
        time.sleep(0.01)
    assert bus.subscriber_count == 0


def test_ws_endpoint_works_without_explicit_broadcaster(
    config: CanaryConfig, anomalies_dir: Path
) -> None:
    """No broadcaster passed → app creates a default one; ws still accepts."""
    app = create_app(config, anomalies_dir=anomalies_dir)
    client = TestClient(app)
    with client.websocket_connect("/ws/telemetry") as ws:
        # Publish via app.state's auto-created broadcaster
        bus = app.state.telemetry_broadcaster
        import time

        for _ in range(50):
            if bus.subscriber_count == 1:
                break
            time.sleep(0.01)
        bus.publish({"ping": 1})
        assert ws.receive_json() == {"ping": 1}


# ---------------------------------------------------------------------------
# GET /obd/dtc (Task 1.5)
# ---------------------------------------------------------------------------


def test_get_obd_dtc_without_a_dtc_state_returns_the_empty_stale_shape(
    config: CanaryConfig, anomalies_dir: Path
) -> None:
    app = create_app(config, anomalies_dir=anomalies_dir)
    client = TestClient(app)
    response = client.get("/obd/dtc")
    assert response.status_code == 200
    body = response.json()
    assert body["captured_at"] is None
    assert body["stored"] == []
    assert body["stale"] is True


def test_get_obd_dtc_returns_the_cached_report_shape(
    config: CanaryConfig, anomalies_dir: Path
) -> None:
    from datetime import UTC, datetime, timedelta

    from src.control.dtc_state import DtcState
    from src.dashboard.schemas import DtcReportResponse
    from src.telemetry import TelemetryRecord

    dtc_state = DtcState()
    dtc_state.observe(
        TelemetryRecord.obd(1_778_000_000.0, "DTC_STORED", "P0420,C0510", ""),
        session_id="s1",
        session_dir=None,
        emitter=None,
    )
    dtc_state.observe(
        TelemetryRecord.obd(1_778_000_000.0, "MIL_STATUS", True, ""),
        session_id="s1",
        session_dir=None,
        emitter=None,
    )

    app = create_app(config, anomalies_dir=anomalies_dir, dtc_state=dtc_state)
    client = TestClient(app)
    response = client.get("/obd/dtc")
    assert response.status_code == 200
    body = response.json()

    validated = DtcReportResponse.model_validate(body)
    assert [c.code for c in validated.stored] == ["P0420", "C0510"]
    assert validated.mil_on is True
    assert validated.dtc_count == 2
    assert validated.captured_at is not None

    # Fresh capture -> not stale.
    assert body["stale"] is False
    # A snapshot taken long after captured_at is stale.
    later = dtc_state.snapshot(now=datetime.now(UTC) + timedelta(hours=1))
    assert later["stale"] is True


# ---------------------------------------------------------------------------
# GET /sessions/{id}/trip-summary (Task 1.5)
# ---------------------------------------------------------------------------


def test_get_session_trip_summary_returns_computed_shape(
    config: CanaryConfig, anomalies_dir: Path
) -> None:
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    session_id = "2026-07-19T120000"
    session = config.collection.storage_path / session_id
    records = [
        TelemetryRecord.obd(1_779_400_000.0 + i, "SPEED", 60.0, "km/h") for i in range(10)
    ] + [TelemetryRecord.obd(1_779_400_000.0 + i, "RPM", 2000.0, "rpm") for i in range(10)]
    write_records_to_parquet(records, session, max_rows=10_000)

    app = create_app(config, anomalies_dir=anomalies_dir)
    client = TestClient(app)
    response = client.get(f"/sessions/{session_id}/trip-summary")
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == session_id
    assert body["summary"]["distance_km"] > 0
    assert body["summary"]["avg_speed_kph"] is not None

    # Sidecar was persisted (Task 1.4), and a second request reads it back
    # unchanged rather than recomputing.
    assert (session / "trip_summary.json").exists()
    again = client.get(f"/sessions/{session_id}/trip-summary")
    assert again.json() == body


def test_get_session_trip_summary_404_for_missing_session(
    config: CanaryConfig, anomalies_dir: Path
) -> None:
    app = create_app(config, anomalies_dir=anomalies_dir)
    client = TestClient(app)
    response = client.get("/sessions/2099-01-01T000000/trip-summary")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /control/sessions/{id}/digest (Task 5.6; authenticated per the
# auth-consistency follow-up — the digest aggregates the vehicle ledger and
# per-session fingerprint z-scores, both otherwise only readable via
# authenticated /control/vehicle-ledger and /control/trends)
# ---------------------------------------------------------------------------


def test_get_session_digest_requires_auth(config: CanaryConfig, anomalies_dir: Path) -> None:
    app = create_app(config, anomalies_dir=anomalies_dir, control_token="secret")
    client = TestClient(app)
    response = client.get("/control/sessions/2026-07-19T120000/digest")
    assert response.status_code == 401


def test_get_session_digest_returns_computed_shape(
    config: CanaryConfig, anomalies_dir: Path
) -> None:
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    session_id = "2026-07-19T120000"
    session = config.collection.storage_path / session_id
    records = [
        TelemetryRecord.obd(1_779_400_000.0 + i, "SPEED", 60.0, "km/h") for i in range(10)
    ] + [TelemetryRecord.obd(1_779_400_000.0 + i, "RPM", 2000.0, "rpm") for i in range(10)]
    write_records_to_parquet(records, session, max_rows=10_000)

    app = create_app(config, anomalies_dir=anomalies_dir, control_token="secret")
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret"}
    response = client.get(f"/control/sessions/{session_id}/digest", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == session_id
    assert "duration_seconds" in body
    assert "mode_seconds" in body
    assert "anomaly_counts_by_source" in body
    assert "top_behavior_deviations" in body
    assert "fingerprint_top_movers" in body
    assert "new_dtcs" in body
    assert "new_ledger_events" in body

    # Sidecar was persisted (Task 5.5), and a second request reads it back
    # unchanged rather than recomputing.
    assert (session / "digest.json").exists()
    again = client.get(f"/control/sessions/{session_id}/digest", headers=headers)
    assert again.json() == body


def test_get_session_digest_404_for_missing_session(
    config: CanaryConfig, anomalies_dir: Path
) -> None:
    app = create_app(config, anomalies_dir=anomalies_dir, control_token="secret")
    client = TestClient(app)
    response = client.get(
        "/control/sessions/2099-01-01T000000/digest",
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 404


def test_get_session_digest_route_updates_the_catalog(tmp_path: Path) -> None:
    """Task 6.3: the on-demand digest route is a real sidecar-write site too
    (not just the trip-end hook), so it must thread ``catalog_db`` through
    just like the neighboring pin/unpin/classify routes do."""
    from src.storage.catalog import SessionCatalog
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    storage = tmp_path / "storage"
    storage.mkdir()
    catalog_db = tmp_path / "catalog.db"
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(storage) + f"daemon:\n  catalog_db: {catalog_db}\n")
    config = CanaryConfig.from_yaml(cfg_path)

    session_id = "2026-07-19T120000"
    session = storage / session_id
    records = [
        TelemetryRecord.obd(1_779_400_000.0 + i, "SPEED", 60.0, "km/h") for i in range(10)
    ] + [TelemetryRecord.obd(1_779_400_000.0 + i, "RPM", 2000.0, "rpm") for i in range(10)]
    write_records_to_parquet(records, session, max_rows=10_000)

    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    client = TestClient(app)
    response = client.get(
        f"/control/sessions/{session_id}/digest",
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200

    with SessionCatalog(catalog_db) as catalog:
        row = catalog.get_session(session_id)
    assert row is not None
    assert row.digest is not None
    assert row.digest["session_id"] == session_id


def _event(timestamp: str, *, score: float) -> AnomalyEvent:
    return AnomalyEvent(
        timestamp=timestamp,
        source="lstm_autoencoder",
        model_version="2026-05-13",
        score=score,
        threshold=1.0,
        affected_channels=("obd:RPM",),
        operating_mode="idle",
        session="2026-05-13T120000",
    )
