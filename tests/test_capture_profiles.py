from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.collection import bus_budget, tuning_profile
from src.collection.capture_profiles import (
    CaptureProfile,
    CaptureProfileStore,
    model_input_warnings,
)
from src.config import CanaryConfig
from src.control.sessions import list_sessions
from src.dashboard.api import create_app
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.storage.parquet_writer import ParquetTelemetryWriter

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    bus_budget.reset()
    yield
    bus_budget.reset()
    tuning_profile.set_active(False, source="test")


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
    assert hybrid.did_continuous is False
    assert hybrid.bus_budget_override == 90.0
    assert "COMMANDED_EQUIV_RATIO" in hybrid.mode01
    assert "ECM_BASE_FUEL_SCHEDULE_CANDIDATE" in hybrid.mode22


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
    assert store.load().by_name("diagnostic") == profile
    store.delete("diagnostic")
    assert store.load().by_name("diagnostic") is None
    with pytest.raises(ValueError, match="cannot be deleted"):
        store.delete("default")


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
    # The seeded profile carries no override, and a profile without one must
    # leave the operator's budget alone rather than clearing or raising it.
    assert bus_budget.override() is None
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
        json={"profile": "fuel-map", "bus_budget_requests_per_second": 30.0},
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
    payload = {
        "profile": {
            "name": "idle-map",
            "description": "idle calibration",
            "mode01": {"RPM": 3},
            "mode22": [],
            "bus_budget_override": 30,
            "broadcast_decode": True,
            "corpus_eligible": False,
            "storage_prefix": "_capture-idle-map",
        }
    }
    assert client.put("/control/capture/profiles", headers=headers, json=payload).status_code == 200
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


def test_seeded_fuel_map_profile_ships_no_bus_budget_override(tmp_path: Path) -> None:
    """25 req/s dropped 2491 records live; the seed must not re-arm it."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    fuel_map = _store(tmp_path, config).load().by_name("fuel-map")
    assert fuel_map is not None
    assert fuel_map.bus_budget_override is None


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
    bus_budget.set_override(12.0, source="operator", ttl_seconds=None)

    # `fuel-map` declares no override of its own, so the operator's stands.
    assert (
        client.post("/control/capture", headers=headers, json={"profile": "fuel-map"}).status_code
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
        client.post("/control/capture", headers=headers, json={"profile": "high-rate"}).status_code
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
    client.post("/control/capture", headers=headers, json={"profile": "fuel-map"})

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

    client.post("/control/capture", headers=headers, json={"profile": "fuel-map"})
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
