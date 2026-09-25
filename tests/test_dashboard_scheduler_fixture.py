"""The dashboard's windowed-DID rate fixture is what the backend resolves (R08.6).

The capture runner's "requested" column and TelemetryPanel's freshness chips
both show a windowed DID at the window cadence it is actually polled at
(operator decision 2026-09-25, R00). The dashboard's own test
(``src/dashboard/static/src/lib/scheduler.test.ts``) pins that rate against a
committed slice of ``/status.health.obd_scheduler``. This test rebuilds the
slice from the live policy -- the same ``_resolved_polling_plan`` and
``_request_rate_hz`` the daemon reports, activated the way
``test_capture_profile_preview_parity`` activates a profile -- so a backend
change to the rate policy fails here instead of leaving the dashboard pinned
to a stale number.

Regenerate after a deliberate policy change with
``.venv/bin/python -m tests.test_dashboard_scheduler_fixture``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.collection import bus_budget, tuning_profile
from src.collection.capture_profiles import CaptureProfile, seed_catalog
from src.collection.obd_scheduler import (
    CanObdSpec,
    _resolved_polling_plan,
    build_poll_requests,
    did_alias_table,
    preview_discovery_active,
    profile_did_union,
)
from src.collection.obd_scheduler.cadence import _request_rate_hz
from src.config import CanaryConfig, CollectionConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "src/dashboard/static/src/test/fixtures/windowedDidScheduler.json"
EXAMPLE = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml")

#: The channels kept in the slice: one windowed DID and one Mode 01 PID.
KEPT = {"DID_1208", "RPM"}


def _scenarios() -> list[tuple[str, CollectionConfig, CaptureProfile]]:
    collection = EXAMPLE.collection
    return [
        (
            # A configured cycle beats the profile's interval (the R02.3
            # precedence), and a 0.5 weight skips every other window. The
            # status still reports the 2 s *interval*, so 1 / interval is not
            # the cadence.
            "config-cycle-beats-profile-interval",
            collection.model_copy(update={"obd_did_cycle_seconds": 1.5}),
            CaptureProfile(
                name="windowed-interval",
                mode01={"RPM": 1.0},
                mode22=("ECM_AIR_CHARGE",),
                mode22_rate_weights={"ECM_AIR_CHARGE": 0.5},
                did_interval_seconds=2.0,
                storage_prefix="_capture-windowed-interval",
            ),
        ),
        (
            # A profile cycle with weight 1: one read per 0.5 s window, while
            # the reported interval is the config's 60 s.
            "profile-cycle-every-window",
            collection,
            CaptureProfile(
                name="windowed-cycle",
                mode01={"RPM": 1.0},
                mode22=("ECM_AIR_CHARGE",),
                did_cycle_seconds=0.5,
                storage_prefix="_capture-windowed-cycle",
            ),
        ),
    ]


def _slice(collection: CollectionConfig, profile: CaptureProfile) -> dict[str, Any]:
    catalog = [*seed_catalog(collection).profiles, profile]
    spec = CanObdSpec.from_config(collection, EXAMPLE.hardware.can, catalog=catalog)
    assert spec.did_session is not None
    tuning_profile.configure_did_aliases(did_alias_table(profile_did_union(collection, catalog)))
    tuning_profile.activate(profile, source="test")
    requests = build_poll_requests(None, spec)
    plan = _resolved_polling_plan(
        requests,
        spec,
        pid_batch_max=spec.batch_max_pids,
        did_batch_max=spec.did_batch_max_dids,
        discovery_active=preview_discovery_active(profile),
    )
    view = tuning_profile.view()
    rows = [
        {
            "kind": request.kind,
            "channel": request.channel,
            "profile_channel": view.profile_channel_for(request.channel),
            # As `_scheduler_metrics` reports it.
            "configured_rate_hz": round(_request_rate_hz(request, spec), 4),
            "backed_off": False,
            "last_response_age_seconds": None,
        }
        for request in requests
        if request.channel in KEPT and view.request_eligible(request)
    ]
    cycle = view.did_cycle_seconds(spec.did_session)
    return {
        "requests": rows,
        # As `DidWindowController.snapshot` reports it: the interval and the
        # cycle separately, never the period that governs.
        "did_profile": {
            "window_interval_seconds": view.did_window_interval(spec.did_session),
            "cycle": {
                "timing_policy": "cycle" if cycle is not None else "interval",
                "cycle_seconds": cycle,
            },
        },
        "resolved_plan": {
            "members": [m for m in plan["members"] if m["channel"] in KEPT],
        },
        "window_period_seconds": view.did_window_period(spec.did_session),
    }


def build_fixture() -> dict[str, Any]:
    try:
        return {name: _slice(c, p) for name, c, p in _scenarios()}
    finally:
        bus_budget.reset()
        tuning_profile.configure_did_aliases({})
        tuning_profile.set_active(False, source="test")


@pytest.fixture
def restore_runtime() -> Iterator[None]:
    yield
    bus_budget.reset()
    tuning_profile.configure_did_aliases({})
    tuning_profile.set_active(False, source="test")


def test_dashboard_fixture_matches_the_live_policy(restore_runtime: None) -> None:
    committed = json.loads(FIXTURE.read_text())
    assert build_fixture() == committed, (
        "the backend's windowed-DID rate policy changed; regenerate the dashboard "
        "fixture (see the module docstring) and re-run scheduler.test.ts"
    )


def test_the_fixture_exercises_the_drift() -> None:
    """Each scenario must be one where ``1 / window_interval_seconds`` is wrong."""
    for name, scenario in json.loads(FIXTURE.read_text()).items():
        did = next(r for r in scenario["requests"] if r["kind"] == "did")
        naive = 1 / scenario["did_profile"]["window_interval_seconds"]
        assert abs(did["configured_rate_hz"] - naive) > 0.01, name


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(build_fixture(), indent=2) + "\n")
    print(f"wrote {FIXTURE}")
