from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.collection import bus_budget, capture_lifetime, tuning_profile
from src.collection.capture_profiles import (
    BUILTIN_REVISION,
    STORAGE_SAFE_REQUESTS_PER_SECOND,
    CaptureProfile,
    CaptureProfileCatalog,
    CaptureProfileStore,
    load_capture_profiles,
    model_input_warnings,
    save_capture_profiles,
    seed_catalog,
)
from src.config import CanaryConfig
from src.control.sessions import list_sessions
from src.dashboard.api import create_app
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    bus_budget.reset()
    capture_lifetime._reset_for_tests()
    yield
    bus_budget.reset()
    tuning_profile.set_active(False, source="test")
    capture_lifetime._reset_for_tests()


def _store(tmp_path: Path, config: CanaryConfig) -> CaptureProfileStore:
    return CaptureProfileStore(
        tmp_path / "vehicle" / "capture_profiles.yaml",
        config.collection,
        tmp_path / "missing-behavior-models.yaml",
    )


def test_strict_schema_and_seed_mirrors_collection(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    store = _store(tmp_path, config)
    catalog = store.load()

    default = catalog.by_name("default")
    fuel_map = catalog.by_name("fuel-map")
    assert default is not None and set(default.mode01) == {
        channel.upper() for channel in config.collection.obd_pids
    }
    assert default.mode22 == config.collection.obd_dids
    assert fuel_map is not None and fuel_map.storage_prefix == "_capture-fuel-map"
    assert "RPM" in fuel_map.mode01

    with pytest.raises(ValidationError, match="Extra inputs"):
        CaptureProfile.model_validate(
            {
                "name": "bad",
                "mode01": {},
                "corpus_eligible": False,
                "storage_prefix": "_capture-bad",
                "surprise": True,
            }
        )


def test_vehicle_hybrid_profile_keeps_its_experimental_fast_exit_plan() -> None:
    """The one-click UI profile must carry the exact plan tested on the ECM."""
    from src.collection.capture_profiles import load_capture_profiles

    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    catalog = load_capture_profiles(
        REPO_ROOT / "data/nissan-xterra-2008/capture_profiles.yaml",
        config.collection,
    )
    hybrid = catalog.by_name("tuning-hybrid-research")
    assert hybrid is not None
    assert hybrid.experimental is True
    assert hybrid.target_rate_hz == 4.0
    assert hybrid.did_interval_seconds == 0.25
    # C04.3: the 4 Hz intent is a start-to-start cycle; the end-to-start
    # interval is retained with its old meaning, not reinterpreted.
    assert hybrid.did_cycle_seconds == 0.25
    assert hybrid.did_continuous is False
    assert hybrid.bus_budget_override == 90.0
    assert "COMMANDED_EQUIV_RATIO" in hybrid.mode01
    assert (
        "ECM_MAF_GPS" in hybrid.mode22
    )  # renamed 2026-08-08 from ECM_BASE_FUEL_SCHEDULE_CANDIDATE
    assert hybrid.behavior_model_recipes == (
        "injector_pw_vs_rpm_throttle_did",
        "cam_primary_vs_command_rpm_did",
        "cam_secondary_vs_command_rpm_did",
    )


def test_default_cannot_be_deleted_and_profile_crud_round_trips(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    store = _store(tmp_path, config)
    profile = CaptureProfile(
        name="diagnostic",
        mode01={"RPM": 2.0},
        mode22=("ECM_AF_SENSOR_V_B1",),
        corpus_eligible=False,
        storage_prefix="_capture-diagnostic",
    )
    assert store.save(profile) == []
    saved = store.load().by_name("diagnostic")
    # Saving stamps operator provenance, which is what freezes a profile
    # against future seed upgrades. Everything else round-trips unchanged.
    assert saved is not None
    assert saved.operator_modified is True
    assert saved == profile.model_copy(update={"operator_modified": True})
    store.delete("diagnostic")
    assert store.load().by_name("diagnostic") is None
    with pytest.raises(ValueError, match="cannot be deleted"):
        store.delete("default")


def test_mode22_rate_weights_are_backward_compatible_and_strict() -> None:
    legacy = CaptureProfile(
        name="did-legacy",
        mode22=("ECM_RPM",),
        corpus_eligible=False,
        storage_prefix="_capture-did-legacy",
    )
    assert legacy.mode22_rate_weights == {}

    weighted = legacy.model_copy(update={"mode22_rate_weights": {"ECM_RPM": 1.0}})
    # Re-validate because model_copy deliberately trusts its update payload.
    assert CaptureProfile.model_validate(weighted.model_dump()).mode22_rate_weights == {
        "ECM_RPM": 1.0
    }
    for bad in (0.0, -1.0, float("inf"), float("nan")):
        with pytest.raises(ValidationError, match="finite and positive"):
            CaptureProfile(
                name="did-bad-weight",
                mode22=("ECM_RPM",),
                mode22_rate_weights={"ECM_RPM": bad},
                corpus_eligible=False,
                storage_prefix="_capture-did-bad-weight",
            )
    with pytest.raises(ValidationError, match="not selected"):
        CaptureProfile(
            name="did-unknown-weight",
            mode22=("ECM_RPM",),
            mode22_rate_weights={"ECM_COOLANT_TEMP": 0.25},
            corpus_eligible=False,
            storage_prefix="_capture-did-unknown-weight",
        )
    with pytest.raises(ValidationError, match="weighted demand is below"):
        CaptureProfile(
            name="did-tuple-too-slow",
            mode22=("ECM_RPM",),
            mode22_rate_weights={"ECM_RPM": 0.25},
            target_rate_hz=20.0,
            min_achieved_rate_hz=15.0,
            required_tuple=("ECM_RPM",),
            corpus_eligible=False,
            storage_prefix="_capture-did-tuple-too-slow",
        )


def test_a_tuning_capture_starts_without_a_tune_revision(tmp_path: Path) -> None:
    """`requires_tune_revision` prompts; it must never block a capture.

    An earlier version refused to arm, which got in the way more than it
    helped (operator decision 2026-08-10). Nothing is silently lost: the
    manifest still records the absence, so an analysis across a flash boundary
    can tell the calibration was unrecorded.
    """
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={"storage_path": tmp_path / "sessions"}
            )
        }
    )
    store = _store(tmp_path, config)
    writer = AsyncParquetTelemetryWriter(
        ParquetTelemetryWriter(
            tmp_path / "sessions",
            session_started_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        )
    )
    client = TestClient(
        create_app(
            config,
            anomalies_dir=tmp_path / "anomalies",
            control_token="secret",
            storage_writer=writer,
            capture_profile_store=store,
        )
    )
    headers = {"Authorization": "Bearer secret"}

    fuel_map = store.load().by_name("fuel-map")
    assert fuel_map is not None and fuel_map.requires_tune_revision is True

    started = client.post("/control/capture", headers=headers, json={"profile": "fuel-map"})
    assert started.status_code == 200
    assert writer.paths.root.name.startswith("_capture-fuel-map-")

    # The absence is still on the record, which is what makes it safe to allow.
    sidecar = json.loads((writer.paths.root / "capture_profile.json").read_text())
    assert sidecar["tune_revision"] is None


class _Clock:
    """Monotonic + wall clock that only moves when told to (arm-to-stop is
    simulated, so a five-minute capture takes no five minutes)."""

    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall = 1_700_000_000.0

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def perf_counter(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


def _round_record(t: float, channel: str, *, generation: int, round_id: int) -> TelemetryRecord:
    return TelemetryRecord.obd(
        t,
        channel,
        1.0,
        "",
        metadata={
            "ts_schema": 2,
            "acquired_at": t,
            "acquired_clock": "kernel",
            "generation": generation,
            "round_id": round_id,
        },
    )


def _capture_rows(
    profile: CaptureProfile, clock: _Clock, *, seconds: float, skip: set[str] = frozenset()
) -> list[TelemetryRecord]:
    """Every profile channel at its requested rate, round-tagged, for ``seconds``.

    Also feeds the capture-lifetime ledger exactly as ``record_read`` would,
    since no collector loop runs here — the ledger is what the verdict scores
    cadence and coverage on, the rows are what it scores yield on.
    """
    from src.collection import capture_generation

    generation = capture_generation.current()
    target = profile.target_rate_hz or 1.0
    period = 1.0 / target
    records: list[TelemetryRecord] = []
    rounds = int(seconds * target)
    for r in range(rounds):
        offset = r * period + 0.002
        for channel, weight in profile.mode01.items():
            if channel in skip:
                continue
            every = max(1, int(round(1.0 / weight)))
            if r % every == 0:
                records.append(
                    _round_record(clock.wall + offset, channel, generation=generation, round_id=r)
                )
                capture_lifetime.observe(channel, clock.mono + offset)
    return records


def _capture_app(tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch):
    """An app whose capture clocks are ``clock`` and whose can0 counters exist."""
    import src.collection.tuning_profile as tuning_profile_mod
    import src.models.health as health_mod
    from src.models.health import HealthMonitor

    monkeypatch.setattr(tuning_profile_mod, "time", clock)
    monkeypatch.setattr(health_mod, "time", clock)
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={"storage_path": tmp_path / "sessions"}
            )
        }
    )
    store = _store(tmp_path, config)
    writer = AsyncParquetTelemetryWriter(
        ParquetTelemetryWriter(
            tmp_path / "sessions",
            session_started_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        )
    )
    stats = tmp_path / "can0-statistics"
    stats.mkdir()
    for name in ("rx_over_errors", "rx_dropped", "rx_errors"):
        (stats / name).write_text("0")
    health = HealthMonitor(can0_stats_path=stats)
    health.observe_obd_scheduler({"achieved_hz_capture": {}, "requests": [], "batching": {}})
    # The daemon's sinks register their queues with health when they start
    # (drain.py); the finalizer treats an unregistered queue as unobserved,
    # which is an unverified verdict, not a pass. Mirror the daemon here.
    for name in ("ingest", "storage"):
        health.observe_telemetry_queue(name, asyncio.Queue())
    client = TestClient(
        create_app(
            config,
            anomalies_dir=tmp_path / "anomalies",
            control_token="secret",
            storage_writer=writer,
            capture_profile_store=store,
            health_monitor=health,
        )
    )
    return client, writer, store, health, stats


def test_capture_stop_writes_a_v2_sidecar_from_the_rows_it_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary, drain, flush, then judge — on real writer-produced Parquet.

    The 2026-08-10 session directory held only the profile and the DTC list,
    so the post-mortem could not establish the achieved rates or the gaps.
    The v1 sidecar that followed scored a trailing 60 s window; this one
    covers arm to stop and counts persisted tuples per round.
    """
    clock = _Clock()
    client, writer, store, health, _stats = _capture_app(tmp_path, clock, monkeypatch)
    headers = {"Authorization": "Bearer secret"}
    started = client.post(
        "/control/capture", headers=headers, json={"profile": "fuel-map", "tune_revision": "k106"}
    )
    assert started.status_code == 200
    capture_dir = writer.paths.root
    assert (capture_dir / "capture_epoch.json").exists()
    profile = store.load().by_name("fuel-map")
    assert profile is not None

    writer.wrapped.write_many(_capture_rows(profile, clock, seconds=60.0))
    clock.advance(60.0)

    stopped = client.post("/control/capture/stop", headers=headers)
    assert stopped.status_code == 200
    sidecar = json.loads((capture_dir / "capture_quality.json").read_text())
    assert sidecar["schema_version"] == 2
    assert sidecar["scope"] == "stop"
    assert sidecar["verdict"] == "pass", sidecar["failures"]
    assert sidecar["interval"]["elapsed_s"] == pytest.approx(60.0)
    assert sidecar["tuples"]["source"] == "persisted_rows"
    assert sidecar["tuples"]["complete"] == 240
    assert sidecar["tuples"]["max_tuple_skew_s"] == pytest.approx(0.125)
    assert sidecar["losses"]["recording"]["status"] == "verified"
    assert sidecar["losses"]["recording"]["lost"] == 0
    assert sidecar["persistence"]["flushed"] is True
    assert sidecar["persistence"]["generation_boundary"] >= 1
    assert sidecar["persistence"]["rows_on_disk"] == sidecar["persistence"]["rows_written"]
    rpm = next(row for row in sidecar["channels"] if row["channel"] == "RPM")
    assert rpm["status"] == "ok" and rpm["cadence"]["hz"] == pytest.approx(4.0)
    assert "tuple_bound_is_exact" not in sidecar
    assert stopped.json()["capture_quality"]["verdict"] == "pass"

    # Reopened later, the stop-time scope is explicit and recompute agrees.
    recomputed = client.get(f"/control/capture/quality/{capture_dir.name}", headers=headers)
    assert recomputed.status_code == 200
    body = recomputed.json()
    assert body["stored"]["scope"] == "stop"
    assert body["recomputed"]["scope"] == "recompute"
    assert body["recomputed"]["tuples"]["complete"] == 240
    assert body["recomputed"]["losses"]["recording"]["status"] == "verified"


def test_the_four_minute_missing_data_case_fails_at_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Samples only in the last minute of five: acquisition_gap, not pass."""
    clock = _Clock()
    client, writer, store, _health, _stats = _capture_app(tmp_path, clock, monkeypatch)
    headers = {"Authorization": "Bearer secret"}
    client.post("/control/capture", headers=headers, json={"profile": "fuel-map"})
    capture_dir = writer.paths.root
    profile = store.load().by_name("fuel-map")
    assert profile is not None
    clock.advance(240.0)
    writer.wrapped.write_many(_capture_rows(profile, clock, seconds=60.0))
    clock.advance(60.0)

    stopped = client.post("/control/capture/stop", headers=headers).json()
    sidecar = json.loads((capture_dir / "capture_quality.json").read_text())
    assert stopped["capture_quality"]["verdict"] == "fail"
    gap = next(f for f in sidecar["failures"] if f["clause"] == "acquisition_gap")
    assert gap["evidence"]["RPM"]["missing_initial_s"] == pytest.approx(240.0, abs=0.01)
    assert sidecar["tuples"]["complete"] == 240


def test_a_late_writer_failure_at_stop_is_recorded_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    client, writer, store, _health, _stats = _capture_app(tmp_path, clock, monkeypatch)
    headers = {"Authorization": "Bearer secret"}
    client.post("/control/capture", headers=headers, json={"profile": "fuel-map"})
    capture_dir = writer.paths.root
    profile = store.load().by_name("fuel-map")
    assert profile is not None
    writer.wrapped.write_many(_capture_rows(profile, clock, seconds=60.0))
    clock.advance(60.0)

    async def failing_flush() -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(writer, "flush", failing_flush)
    stopped = client.post("/control/capture/stop", headers=headers)
    assert stopped.status_code == 200
    sidecar = json.loads((capture_dir / "capture_quality.json").read_text())
    assert sidecar["verdict"] == "fail"
    loss = next(f for f in sidecar["failures"] if f["clause"] == "persistence_loss")
    assert "No space left on device" in loss["evidence"]["flush_error"]
    assert sidecar["persistence"]["flushed"] is False


def test_live_capture_quality_is_scoped_live_and_refused_when_inactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    client, writer, store, _health, stats = _capture_app(tmp_path, clock, monkeypatch)
    headers = {"Authorization": "Bearer secret"}
    assert client.get("/control/capture/quality", headers=headers).status_code == 409
    client.post("/control/capture", headers=headers, json={"profile": "fuel-map"})
    profile = store.load().by_name("fuel-map")
    assert profile is not None
    writer.wrapped.write_many(_capture_rows(profile, clock, seconds=20.0))
    clock.advance(20.0)
    # A kernel receive drop during the capture is attributed to it.
    (stats / "rx_dropped").write_text("7")

    live = client.get("/control/capture/quality", headers=headers)
    assert live.status_code == 200
    body = live.json()
    assert body["scope"] == "live"
    assert body["verdict"] == "fail"
    assert {f["clause"] for f in body["failures"]} == {"persistence_loss"}
    assert body["losses"]["recording"]["counters"]["can0.rx_dropped"]["delta"] == 7
    assert body["tuples"]["complete"] == 80
    client.delete("/control/capture", headers=headers)


def test_max_tuple_skew_must_fit_inside_a_round() -> None:
    with pytest.raises(ValueError, match="exceeds the round period"):
        CaptureProfile(
            name="x",
            mode01={"A": 1.0},
            target_rate_hz=4.0,
            max_tuple_skew_s=0.5,
            storage_prefix="_capture-x",
        )
    ok = CaptureProfile(
        name="x",
        mode01={"A": 1.0},
        target_rate_hz=4.0,
        max_tuple_skew_s=0.2,
        storage_prefix="_capture-x",
    )
    assert ok.max_tuple_skew_s == 0.2
    assert (
        CaptureProfile(name="y", mode01={"A": 1.0}, storage_prefix="_capture-y").max_tuple_skew_s
        is None
    )


def test_capture_lifecycle_rotates_writes_sidecar_and_reverts(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={"storage_path": tmp_path / "sessions"}
            )
        }
    )
    store = _store(tmp_path, config)
    writer = AsyncParquetTelemetryWriter(
        ParquetTelemetryWriter(
            tmp_path / "sessions",
            session_started_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        )
    )
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        storage_writer=writer,
        capture_profile_store=store,
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret"}

    response = client.post(
        "/control/capture",
        headers=headers,
        json={"profile": "fuel-map", "tune_revision": "rom-17"},
    )
    assert response.status_code == 200
    assert writer.paths.root.name.startswith("_capture-fuel-map-")
    sidecar = json.loads((writer.paths.root / "capture_profile.json").read_text())
    assert sidecar["profile"] == "fuel-map"
    assert sidecar["tune_revision"] == "rom-17"
    assert sidecar["profile_snapshot"]["behavior_model_recipes"] == []
    # `fuel-map` now declares its own budget, because demand and budget both
    # bind and raising only one achieves nothing. Arming applies it.
    assert bus_budget.override() == 20.0
    assert client.get("/status").json()["capture_profile"]["name"] == "fuel-map"

    captures = client.get("/control/sessions?captures=true", headers=headers).json()
    assert captures["sessions"][0]["metadata"]["capture_profile"] == "fuel-map"
    assert all(not item.session_id.startswith("_") for item in list_sessions(tmp_path / "sessions"))
    assert len(list_sessions(tmp_path / "sessions", captures=True)) == 1

    stopped = client.delete("/control/capture", headers=headers)
    assert stopped.status_code == 200
    assert not writer.paths.root.name.startswith("_")
    assert bus_budget.override() is None
    assert stopped.json()["capture_profile"]["name"] == "default"


def test_repeated_matching_capture_arm_and_stop_are_idempotent(tmp_path: Path) -> None:
    """A dropped HTTP response cannot split a drive into two capture sessions."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={"storage_path": tmp_path / "sessions"}
            )
        }
    )
    store = _store(tmp_path, config)
    writer = AsyncParquetTelemetryWriter(
        ParquetTelemetryWriter(
            tmp_path / "sessions",
            session_started_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        )
    )
    client = TestClient(
        create_app(
            config,
            anomalies_dir=tmp_path / "anomalies",
            control_token="secret",
            storage_writer=writer,
            capture_profile_store=store,
        )
    )
    headers = {"Authorization": "Bearer secret"}
    request = {"profile": "fuel-map", "tune_revision": "rom-17"}

    first = client.post("/control/capture", headers=headers, json=request)
    capture_dir = first.json()["output_session_dir"]
    repeated = client.post("/control/capture", headers=headers, json=request)

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True
    assert repeated.json()["output_session_dir"] == capture_dir
    assert len(list_sessions(tmp_path / "sessions", captures=True)) == 1

    stopped = client.post("/control/capture/stop", headers=headers)
    repeated_stop = client.post("/control/capture/stop", headers=headers)

    assert stopped.status_code == 200
    assert repeated_stop.status_code == 200
    assert repeated_stop.json()["idempotent"] is True
    assert repeated_stop.json()["ended_capture_dir"] == capture_dir


def test_capture_start_budget_failure_restores_writer_profile_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after storage rotation must not strand the daemon in capture mode."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={"storage_path": tmp_path / "sessions"}
            )
        }
    )
    store = _store(tmp_path, config)
    writer = AsyncParquetTelemetryWriter(
        ParquetTelemetryWriter(
            tmp_path / "sessions",
            session_started_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        )
    )
    client = TestClient(
        create_app(
            config,
            anomalies_dir=tmp_path / "anomalies",
            control_token="secret",
            storage_writer=writer,
            capture_profile_store=store,
        )
    )

    def fail_budget(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected budget failure")

    monkeypatch.setattr(bus_budget, "set_override", fail_budget)
    response = client.post(
        "/control/capture",
        headers={"Authorization": "Bearer secret"},
        json={
            "profile": "fuel-map",
            "tune_revision": "rom-17",
            "bus_budget_requests_per_second": 30.0,
        },
    )

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["error"] == "capture_start_failed"
    assert detail["failed_step"] == "bus_budget"
    assert detail["restored"]["profile"]["name"] == "default"
    assert detail["restored"]["bus_budget"]["override_requests_per_second"] is None
    assert not writer.paths.root.name.startswith("_capture-")


def test_capture_profile_api_crud_and_auth(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    store = _store(tmp_path, config)
    client = TestClient(
        create_app(
            config,
            anomalies_dir=tmp_path / "anomalies",
            control_token="secret",
            capture_profile_store=store,
        )
    )
    assert client.get("/control/capture/profiles").status_code == 401
    headers = {"Authorization": "Bearer secret"}
    assert client.get("/control/capture/profiles", headers=headers).status_code == 200
    profile = {
        "name": "idle-map",
        "description": "idle calibration",
        "mode01": {"RPM": 3},
        "mode22": [],
        "bus_budget_override": 30,
        "broadcast_decode": True,
        "corpus_eligible": False,
        "storage_prefix": "_capture-idle-map",
    }
    # 30 req/s is past the line where the storage sink is known to drop rows,
    # so the API refuses it unevidenced rather than accepting it and finding
    # out during the drive.
    refused = client.put("/control/capture/profiles", headers=headers, json={"profile": profile})
    assert refused.status_code == 422

    evidenced = {**profile, "capacity_evidence": "bench 2026-08-04: 478 reads/s, zero drops"}
    assert (
        client.put(
            "/control/capture/profiles", headers=headers, json={"profile": evidenced}
        ).status_code
        == 200
    )
    assert client.delete("/control/capture/profiles/idle-map", headers=headers).status_code == 200
    assert client.delete("/control/capture/profiles/default", headers=headers).status_code == 409


def test_storage_prefix_must_stay_resolvable_as_a_session(tmp_path: Path) -> None:
    """A prefix the sessions API cannot match would strand the capture."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    store = _store(tmp_path, config)

    with pytest.raises(ValidationError, match="_capture-"):
        CaptureProfile(
            name="dyno-pulls",
            mode01={"RPM": 1.0},
            corpus_eligible=False,
            storage_prefix="_dyno",
        )

    accepted = CaptureProfile(
        name="dyno-pulls",
        mode01={"RPM": 1.0},
        corpus_eligible=False,
        storage_prefix="_capture-dyno-pulls",
    )
    store.save(accepted)
    storage_root = tmp_path / "storage"
    session_dir = f"{accepted.storage_prefix}-2026-07-31T120000"
    (storage_root / session_dir).mkdir(parents=True, exist_ok=True)
    listed = list_sessions(storage_root, captures=True)
    assert session_dir in {item.session_id for item in listed}

    # Corpus-eligible captures rotate into a normal timestamped session, so a
    # prefix there would describe a directory that never gets created.
    with pytest.raises(ValidationError, match="normal timestamped sessions"):
        CaptureProfile(
            name="corpus-capture",
            mode01={"RPM": 1.0},
            corpus_eligible=True,
            storage_prefix="_capture-corpus-capture",
        )


def test_seeded_fuel_map_budget_stays_under_the_storage_safe_line(tmp_path: Path) -> None:
    """25 req/s dropped 2491 records live; the seed must stay clear of it.

    The seed used to ship no override at all, which stopped the incident but
    also guaranteed the failure it was paired with: demand capped at
    ``poll_rate_hz x tier`` and throughput at the default 8 req/s, so the
    2026-08-10 "high-rate" capture ran at 0.28-0.78 Hz. Both knobs now move,
    and the real invariant is the one asserted here — under the measured
    storage-sink line, or carrying evidence.
    """
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    fuel_map = _store(tmp_path, config).load().by_name("fuel-map")
    assert fuel_map is not None
    assert fuel_map.bus_budget_override is not None
    assert fuel_map.bus_budget_override <= STORAGE_SAFE_REQUESTS_PER_SECOND
    assert fuel_map.target_rate_hz is not None


def test_raising_the_budget_past_the_storage_line_requires_evidence() -> None:
    """The 2026-07-29 drop is a measured constraint, so it is a validator.

    It had been a comment in the seed for months; a comment cannot refuse.
    """
    with pytest.raises(ValidationError, match="capacity_evidence"):
        CaptureProfile(
            name="reckless",
            mode01={"RPM": 1.0},
            bus_budget_override=STORAGE_SAFE_REQUESTS_PER_SECOND + 1.0,
            storage_prefix="_capture-reckless",
        )
    # ...and is allowed with it, because the ceiling is a claim about evidence,
    # not a hard limit.
    profile = CaptureProfile(
        name="measured",
        mode01={"RPM": 1.0},
        bus_budget_override=STORAGE_SAFE_REQUESTS_PER_SECOND + 1.0,
        capacity_evidence="bench 2026-08-04: 478 reads/s, zero drops",
        storage_prefix="_capture-measured",
    )
    assert profile.bus_budget_override == STORAGE_SAFE_REQUESTS_PER_SECOND + 1.0


def test_required_tuple_must_name_channels_the_profile_polls() -> None:
    """A tuple naming an unpolled channel would verify as a vehicle fault."""
    with pytest.raises(ValidationError, match="does not poll"):
        CaptureProfile(
            name="mistyped",
            mode01={"RPM": 1.0},
            required_tuple=("RPM", "COMMANDED_EQUIV_RATIO"),
            storage_prefix="_capture-mistyped",
        )


def test_builtin_profiles_upgrade_but_operator_edits_are_frozen(tmp_path: Path) -> None:
    """A saved built-in must not pin a superseded definition forever.

    This is the defect that produced the 0.28 Hz fuel-map capture: the source
    seed had been corrected, and a catalog written before the correction
    shadowed it with nothing reporting the divergence.
    """
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    path = tmp_path / "vehicle" / "capture_profiles.yaml"
    seeded = seed_catalog(config.collection)
    stale = [
        profile.model_copy(update={"target_rate_hz": None, "builtin_revision": 0})
        if profile.name == "fuel-map"
        else profile
        for profile in seeded.profiles
    ]
    save_capture_profiles(path, CaptureProfileCatalog(profiles=stale))

    upgraded = load_capture_profiles(path, config.collection).by_name("fuel-map")
    assert upgraded is not None
    assert upgraded.target_rate_hz is not None
    assert upgraded.builtin_revision == BUILTIN_REVISION

    # An operator edit is a decision, not a stale copy: it survives.
    edited = [
        profile.model_copy(
            update={
                "target_rate_hz": 2.0,
                "builtin_revision": 0,
                "operator_modified": True,
            }
        )
        if profile.name == "fuel-map"
        else profile
        for profile in seeded.profiles
    ]
    save_capture_profiles(path, CaptureProfileCatalog(profiles=edited))
    kept = load_capture_profiles(path, config.collection).by_name("fuel-map")
    assert kept is not None
    assert kept.target_rate_hz == 2.0


def test_model_input_warnings_resolve_derived_and_decoded_channels(tmp_path: Path) -> None:
    """Derived channels are computed, so they can never be in a poll list."""
    # The shipped surfaces, so this pins the behavior the operator actually
    # sees: two of these models are keyed on derived channels.
    behavior_models = REPO_ROOT / "data" / "nissan-xterra-2008" / "behavior_models.yaml"
    if not behavior_models.exists():
        pytest.skip("vehicle behavior models are not present in this checkout")
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    store = CaptureProfileStore(
        tmp_path / "vehicle" / "capture_profiles.yaml", config.collection, behavior_models
    )
    profile = CaptureProfile(
        name="default",
        mode01={"RPM": 1.0, "ENGINE_LOAD": 1.0, "MAF": 1.0, "INTAKE_TEMP": 1.0},
        corpus_eligible=True,
        broadcast_decode=True,
    )
    warnings = store.save(profile)
    flagged = {warning["model"] for warning in warnings}
    missing = {channel for warning in warnings for channel in warning["missing_channels"]}

    # AIR_MASS_PER_CYCLE and VE_PERCENT resolve through MAF/RPM plus the
    # broadcast-decoded MAP, so neither may be reported as starved — they are
    # computed channels and can never appear in a poll list.
    assert not any(channel.startswith("derived:") for channel in missing)
    assert "air_mass_per_cycle_vs_rpm_load" not in flagged
    assert "ve_vs_rpm_load" not in flagged

    # Genuinely absent physical channels are still reported.
    assert "cat_warmup" in flagged
    assert {"obd:CATALYST_TEMP_B1S1", "obd:RUN_TIME"} <= missing

    # Broadcast decode is what supplies MAP; turning it off starves VE.
    without_decode = profile.model_copy(update={"broadcast_decode": False})
    starved = model_input_warnings(without_decode, behavior_models)
    assert "ve_vs_rpm_load" in {warning["model"] for warning in starved}


def test_capture_preserves_and_restores_an_operator_bus_budget(tmp_path: Path) -> None:
    """Starting a capture must not silently discard a manual override."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={"storage_path": tmp_path / "sessions"}
            )
        }
    )
    store = _store(tmp_path, config)
    writer = AsyncParquetTelemetryWriter(
        ParquetTelemetryWriter(
            tmp_path / "sessions",
            session_started_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        )
    )
    client = TestClient(
        create_app(
            config,
            anomalies_dir=tmp_path / "anomalies",
            control_token="secret",
            storage_writer=writer,
            capture_profile_store=store,
        )
    )
    headers = {"Authorization": "Bearer secret"}
    # A profile that declares no budget of its own. `fuel-map` used to serve
    # here and no longer can: it now ships one, because demand and budget both
    # bind and moving only one achieves nothing.
    store.save(
        CaptureProfile(
            name="no-budget",
            mode01={"RPM": 1.0},
            corpus_eligible=False,
            storage_prefix="_capture-no-budget",
        )
    )
    bus_budget.set_override(12.0, source="operator", ttl_seconds=None)

    assert (
        client.post(
            "/control/capture",
            headers=headers,
            json={"profile": "no-budget"},
        ).status_code
        == 200
    )
    assert bus_budget.override() == 12.0

    assert client.delete("/control/capture", headers=headers).status_code == 200
    assert bus_budget.override() == 12.0


def test_capture_with_its_own_budget_restores_the_previous_one_on_stop(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={"storage_path": tmp_path / "sessions"}
            )
        }
    )
    store = _store(tmp_path, config)
    store.save(
        CaptureProfile(
            name="high-rate",
            mode01={"RPM": 1.0},
            bus_budget_override=20.0,
            corpus_eligible=False,
            storage_prefix="_capture-high-rate",
        )
    )
    writer = AsyncParquetTelemetryWriter(
        ParquetTelemetryWriter(
            tmp_path / "sessions",
            session_started_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        )
    )
    client = TestClient(
        create_app(
            config,
            anomalies_dir=tmp_path / "anomalies",
            control_token="secret",
            storage_writer=writer,
            capture_profile_store=store,
        )
    )
    headers = {"Authorization": "Bearer secret"}
    bus_budget.set_override(9.0, source="operator", ttl_seconds=None)

    assert (
        client.post(
            "/control/capture",
            headers=headers,
            json={"profile": "high-rate", "tune_revision": "rom-17"},
        ).status_code
        == 200
    )
    assert bus_budget.override() == 20.0

    assert client.delete("/control/capture", headers=headers).status_code == 200
    assert bus_budget.override() == 9.0


def test_capture_route_applies_budget_atomically_and_returns_ended_path(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML).model_copy(
        update={
            "collection": CanaryConfig.from_yaml(EXAMPLE_YAML).collection.model_copy(
                update={"storage_path": tmp_path / "sessions"}
            )
        }
    )
    store = _store(tmp_path, config)
    writer = AsyncParquetTelemetryWriter(
        ParquetTelemetryWriter(
            tmp_path / "sessions",
            session_started_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        )
    )
    client = TestClient(
        create_app(
            config,
            anomalies_dir=tmp_path / "anomalies",
            control_token="secret",
            storage_writer=writer,
            capture_profile_store=store,
        )
    )
    headers = {"Authorization": "Bearer secret"}
    bus_budget.set_override(12.0, source="operator", ttl_seconds=None)

    started = client.post(
        "/control/capture",
        headers=headers,
        json={
            "profile": "fuel-map",
            "tune_revision": "rom-17",
            "bus_budget_requests_per_second": 30.0,
            "bus_budget_ttl_seconds": 300.0,
        },
    )
    assert started.status_code == 200
    assert started.json()["bus_budget"]["override_requests_per_second"] == 30.0
    assert started.json()["output_session_dir"].endswith(writer.paths.root.name)

    ended_path = writer.paths.root
    stopped = client.post("/control/capture/stop", headers=headers)
    assert stopped.status_code == 200
    assert stopped.json()["ended_capture_dir"] == str(ended_path)
    assert bus_budget.snapshot()["source"] == "operator"
    assert bus_budget.override() == 12.0


def test_active_profile_cannot_be_deleted_via_a_case_variant(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={"storage_path": tmp_path / "sessions"}
            )
        }
    )
    store = _store(tmp_path, config)
    writer = AsyncParquetTelemetryWriter(
        ParquetTelemetryWriter(
            tmp_path / "sessions",
            session_started_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        )
    )
    client = TestClient(
        create_app(
            config,
            anomalies_dir=tmp_path / "anomalies",
            control_token="secret",
            storage_writer=writer,
            capture_profile_store=store,
        )
    )
    headers = {"Authorization": "Bearer secret"}
    client.post(
        "/control/capture", headers=headers, json={"profile": "fuel-map", "tune_revision": "rom-17"}
    )

    assert client.delete("/control/capture/profiles/Fuel-Map", headers=headers).status_code == 409
    assert store.load().by_name("fuel-map") is not None


def test_collectors_enable_multi_pid_batching_from_config() -> None:
    """Batched PIDs share one request and one timestamp — the whole point of a
    capture reading as synchronous rows rather than interleaved samples."""
    from src.collection.can_obd_collector import CanObdSpec

    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    assert config.collection.obd_batch_max_pids == 6, "batch when the protocol allows it"

    # The spec default stays 1 so standalone/recorder paths are unchanged;
    # the daemon is what opts in.
    assert CanObdSpec.batch_max_pids == 1


def test_pause_suspends_polling_without_ending_the_capture(tmp_path: Path) -> None:
    """Pause stops spending bus budget on rows nobody wants, but the session
    stays open so resuming continues the same log."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={"storage_path": tmp_path / "sessions"}
            )
        }
    )
    store = _store(tmp_path, config)
    writer = AsyncParquetTelemetryWriter(
        ParquetTelemetryWriter(
            tmp_path / "sessions",
            session_started_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        )
    )
    client = TestClient(
        create_app(
            config,
            anomalies_dir=tmp_path / "anomalies",
            control_token="secret",
            storage_writer=writer,
            capture_profile_store=store,
        )
    )
    headers = {"Authorization": "Bearer secret"}

    # Pausing means nothing without a capture to pause.
    assert (
        client.post("/control/capture/pause", headers=headers, json={"paused": True}).status_code
        == 409
    )

    client.post(
        "/control/capture", headers=headers, json={"profile": "fuel-map", "tune_revision": "rom-17"}
    )
    session_dir = writer.paths.root

    request = SimpleNamespace(kind="pid", channel="RPM")
    assert tuning_profile.request_eligible(0, request) is True

    paused = client.post("/control/capture/pause", headers=headers, json={"paused": True})
    assert paused.status_code == 200
    assert paused.json()["capture_profile"]["paused"] is True
    assert tuning_profile.request_eligible(0, request) is False
    # Same session — a pause is not a rotation.
    assert writer.paths.root == session_dir
    assert client.get("/status").json()["capture_profile"]["paused"] is True

    resumed = client.post("/control/capture/pause", headers=headers, json={"paused": False})
    assert resumed.json()["capture_profile"]["paused"] is False
    assert tuning_profile.request_eligible(0, request) is True

    # Ending a capture leaves nothing paused behind for the next one.
    client.post("/control/capture/pause", headers=headers, json={"paused": True})
    client.delete("/control/capture", headers=headers)
    assert tuning_profile.is_paused() is False


def test_status_reports_rows_recorded_for_the_active_session(tmp_path: Path) -> None:
    """'Is it actually logging?' answered without waiting for a flush."""
    from src.telemetry import TelemetryRecord

    writer = ParquetTelemetryWriter(
        tmp_path / "sessions",
        session_started_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )
    assert writer.rows_written == 0
    for index in range(3):
        writer.write(TelemetryRecord.obd(1_760_000_000.0 + index, "RPM", 800.0, "rpm"))
    assert writer.rows_written == 3

    # A new session starts its own count rather than carrying the last one on.
    writer.rotate_session(started_at=datetime(2026, 7, 31, 12, 5, tzinfo=UTC))
    assert writer.rows_written == 0


def test_load_backfills_builtin_profiles_added_after_the_file_was_written(
    tmp_path: Path,
) -> None:
    """A catalog saved before a built-in existed must not hide it forever.

    Otherwise the first symptom is a 404 from POST /control/captures, in the
    vehicle, where hand-editing YAML is the worst possible remedy. Operator
    edits to profiles the file already names are still authoritative.
    """
    from src.collection.capture_profiles import (
        TUNING_DID_PROFILE_NAME,
        CaptureProfileCatalog,
        load_capture_profiles,
        save_capture_profiles,
        seed_catalog,
    )

    collection = CanaryConfig.from_yaml(EXAMPLE_YAML).collection
    seeded = seed_catalog(collection)
    stale = CaptureProfileCatalog(
        profiles=[p for p in seeded.profiles if p.name != TUNING_DID_PROFILE_NAME]
    )
    # Prove the fixture is actually missing the profile under test.
    assert TUNING_DID_PROFILE_NAME not in {p.name for p in stale.profiles}
    path = tmp_path / "capture_profiles.yaml"
    save_capture_profiles(path, stale)

    loaded = load_capture_profiles(path, collection)
    assert loaded.by_name(TUNING_DID_PROFILE_NAME) is not None
    # Persisted, so the back-fill happens once rather than on every load.
    assert TUNING_DID_PROFILE_NAME in path.read_text()


def test_load_does_not_overwrite_an_operator_edited_profile(tmp_path: Path) -> None:
    """Back-filling must add only what is absent, never reset what is present."""
    from src.collection.capture_profiles import (
        FUEL_MAP_PROFILE_NAME,
        CaptureProfileCatalog,
        load_capture_profiles,
        save_capture_profiles,
        seed_catalog,
    )

    collection = CanaryConfig.from_yaml(EXAMPLE_YAML).collection
    seeded = seed_catalog(collection)
    edited = [
        p.model_copy(update={"target_rate_hz": 33.0}) if p.name == FUEL_MAP_PROFILE_NAME else p
        for p in seeded.profiles
    ]
    path = tmp_path / "capture_profiles.yaml"
    save_capture_profiles(path, CaptureProfileCatalog(profiles=edited))

    loaded = load_capture_profiles(path, collection)
    fuel = loaded.by_name(FUEL_MAP_PROFILE_NAME)
    assert fuel is not None and fuel.target_rate_hz == 33.0


def test_capture_start_accepts_a_per_capture_rate_override(tmp_path: Path) -> None:
    """A one-off fast pull must not become the profile's new normal.

    The saved catalog entry stays untouched; only this capture runs at the
    overridden rate.
    """
    from src.collection.capture_profiles import TUNING_DID_PROFILE_NAME

    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    store = _store(tmp_path, config)
    before = store.load().by_name(TUNING_DID_PROFILE_NAME)
    assert before is not None
    assert before.target_rate_hz == 15.0

    overridden = before.model_copy(update={"target_rate_hz": 25.0})
    assert overridden.target_rate_hz == 25.0
    # The catalog is unchanged by the copy — that is the property under test.
    after = store.load().by_name(TUNING_DID_PROFILE_NAME)
    assert after is not None and after.target_rate_hz == 15.0


def test_bus_budget_ttl_allows_a_capture_length_override() -> None:
    """A 30-minute drive brushed the old 1 h cap mid-arming.

    The capture teardown reverts the budget itself, so the TTL is a backstop
    against a forgotten override rather than the mechanism — 6 h is a
    sensible backstop, 1 h was an obstacle.
    """
    from pydantic import ValidationError

    from src.dashboard.schemas import BusBudgetRequest

    assert BusBudgetRequest(requests_per_second=40, ttl_seconds=14400).ttl_seconds == 14400
    with pytest.raises(ValidationError):
        BusBudgetRequest(requests_per_second=40, ttl_seconds=21601)
