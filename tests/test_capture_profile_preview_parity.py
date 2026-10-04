"""The capture-profile preview is the live plan, asked early (roadmap R02.3).

``CaptureProfileStore.resolved_plan`` said it was "the same identifier-level
plan used by live metrics" and was a 142-line re-implementation that
disagreed with it: per-DID window precedence (*profile cycle → profile
interval → config cycle* against live's *profile cycle → config cycle →
profile interval*), a DID-rate cap applied without a Mode-22 session, and no
``round_timestamps`` uniform cadence. Here the live side is built the way the
daemon builds it — the whole saved catalog as the request universe, the alias
table configured, the candidate activated, its budget armed as the capture
route arms it — and the plan the scheduler reports is compared with the
preview's, over every starter profile and a small config matrix.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.collection import bus_budget, tuning_profile
from src.collection.capture_profiles import (
    CaptureProfile,
    CaptureProfileCatalog,
    CaptureProfileSources,
    CaptureProfileStore,
    seed_catalog,
)
from src.collection.obd_scheduler import (
    CanObdSpec,
    DidSessionSpec,
    _resolved_polling_plan,
    build_poll_requests,
    did_alias_table,
    preview_discovery_active,
    profile_did_union,
)
from src.config import CanaryConfig, CollectionConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml").collection


def _configs() -> dict[str, CollectionConfig]:
    return {
        "example": EXAMPLE,
        # No DIDs configured: no Mode-22 session, so DIDs are polled bare.
        "no_did_session": EXAMPLE.model_copy(update={"obd_dids": ()}),
        # A configured cycle beside a profile interval: the precedence case.
        "config_cycle": EXAMPLE.model_copy(update={"obd_did_cycle_seconds": 1.5}),
        "explicit_pids": EXAMPLE.model_copy(
            update={"obd_pids": ("RPM", "SPEED", "COOLANT_TEMP", "THROTTLE_POS")}
        ),
    }


def _extra_profiles() -> list[CaptureProfile]:
    """Shapes the starters do not cover: a windowed DID interval, uniform
    cadence without a target rate, and a memory channel in a window."""
    return [
        CaptureProfile(
            name="windowed-interval",
            mode01={"RPM": 1.0, "SPEED": 0.5},
            mode22=("ECM_AIR_CHARGE",),
            mode22_rate_weights={"ECM_AIR_CHARGE": 0.5},
            did_interval_seconds=2.0,
            storage_prefix="_capture-windowed-interval",
        ),
        CaptureProfile(
            name="uniform-rounds",
            mode01={"RPM": 1.0, "COOLANT_TEMP": 0.25},
            round_timestamps=True,
            storage_prefix="_capture-uniform-rounds",
        ),
        CaptureProfile(
            name="memory-window",
            mode01={"RPM": 1.0},
            memory=("ECM_TARGET_EQUIV_RATIO",),
            did_cycle_seconds=0.5,
            storage_prefix="_capture-memory-window",
        ),
    ]


def _catalog(collection: CollectionConfig) -> CaptureProfileCatalog:
    seeded = seed_catalog(collection)
    return CaptureProfileCatalog(profiles=[*seeded.profiles, *_extra_profiles()])


def _cases() -> list[Any]:
    cases = []
    for config_name, collection in _configs().items():
        for profile in _catalog(collection).profiles:
            variants = [profile]
            if not profile.round_timestamps and profile.name != "default":
                variants.append(
                    profile.model_copy(update={"round_timestamps": True, "name": profile.name})
                )
            for variant in variants:
                label = f"{config_name}-{variant.name}-rt{int(variant.round_timestamps)}"
                cases.append(pytest.param(collection, variant, id=label))
    return cases


@pytest.fixture
def restore_runtime() -> Iterator[None]:
    yield
    bus_budget.reset()
    tuning_profile.configure_did_aliases({})
    tuning_profile.set_active(False, source="test")


def _live_plan(collection: CollectionConfig, profile: CaptureProfile) -> dict[str, Any]:
    catalog = _catalog(collection)
    catalog = CaptureProfileCatalog(
        profiles=[profile if p.name == profile.name else p for p in catalog.profiles]
    )
    spec = CanObdSpec.from_config(collection, EXAMPLE_CAN, catalog=catalog.profiles)
    tuning_profile.configure_did_aliases(
        did_alias_table(profile_did_union(collection, catalog.profiles))
    )
    tuning_profile.activate(profile, source="test")
    if profile.bus_budget_override is not None:
        bus_budget.set_override(profile.bus_budget_override, source="test", ttl_seconds=None)
    # What `_scheduler_metrics` passes: the active profile's view and the
    # live effective budget (both defaulted). Batch sizes and discovery are
    # runtime inputs; they are held at the preview's values so the comparison
    # is of the policy, which is what drifted.
    return _resolved_polling_plan(
        build_poll_requests(None, spec),
        spec,
        pid_batch_max=spec.batch_max_pids,
        did_batch_max=spec.did_batch_max_dids,
        discovery_active=preview_discovery_active(profile),
    )


EXAMPLE_CAN = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml").hardware.can


@pytest.mark.parametrize(("collection", "profile"), _cases())
def test_preview_equals_the_live_plan(
    collection: CollectionConfig,
    profile: CaptureProfile,
    tmp_path: Path,
    restore_runtime: None,
) -> None:
    store = CaptureProfileStore(
        CaptureProfileSources(tmp_path / "profiles.yaml", tmp_path / "overrides.yaml"),
        collection,
        tmp_path / "bm.yaml",
    )
    preview = store.resolved_plan(profile)
    live = _live_plan(collection, profile)
    assert preview == live


def test_the_matrix_exercises_what_drifted() -> None:
    """Guard the matrix itself: it must contain a windowed DID member, a
    session-less config, a round-timestamped profile and a memory read."""
    collections = _configs()
    assert collections["no_did_session"].obd_dids == ()
    profiles = {p.name: p for p in _catalog(EXAMPLE).profiles}
    assert {"default", "fuel-map", "tuning-did"} <= set(profiles)
    assert any(p.round_timestamps for p in profiles.values())
    assert profiles["windowed-interval"].did_interval_seconds is not None
    assert profiles["memory-window"].memory


def test_one_window_period_precedence() -> None:
    """Profile cycle, then config cycle, then profile interval, then config
    interval. The old preview put the profile interval before the config cycle."""
    session = DidSessionSpec(session=0xC0, window_interval_seconds=60.0, cycle_seconds=1.5)
    bare = CaptureProfile(name="p", storage_prefix="_capture-p")
    assert bare.did_window_period(session) == 1.5
    interval = bare.model_copy(update={"did_interval_seconds": 2.0})
    assert interval.did_window_period(session) == 1.5
    assert interval.did_window_period(DidSessionSpec(session=0xC0)) == 2.0
    cycle = bare.model_copy(update={"did_cycle_seconds": 0.25})
    assert cycle.did_window_period(session) == 0.25
    assert bare.did_window_period(DidSessionSpec(session=0xC0)) == 60.0


def test_dashboard_preview_includes_the_daemons_custom_pids(tmp_path: Path) -> None:
    """The app's store is built with ``daemon.custom_pids``: a vehicle's custom
    PID appears in the capture-profile preview, as it does in the live plan."""
    from fastapi.testclient import TestClient

    from src.dashboard.api import create_app

    custom = tmp_path / "custom_pids.yaml"
    custom.write_text(
        "schema_version: 1\n"
        "custom_pids:\n"
        "  - name: OIL_PRESS\n"
        "    display_name: Oil Pressure\n"
        "    mode: 34\n"
        "    pid: 4416\n"
        "    formula: (A*256+B)/100\n"
        "    unit: psi\n"
    )
    config = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml")
    config = config.model_copy(
        update={
            "daemon": config.daemon.model_copy(
                update={
                    "custom_pids": custom,
                    "capture_profiles": tmp_path / "capture_profiles.yaml",
                    "capture_profile_overrides": tmp_path / "capture_profile_overrides.yaml",
                }
            )
        }
    )
    client = TestClient(
        create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    )
    listed = client.get("/control/capture/profiles", headers={"Authorization": "Bearer secret"})
    assert listed.status_code == 200
    members = listed.json()["resolved_plans"]["default"]["members"]
    assert any(member["channel"] == "custom:OIL_PRESS" for member in members)
