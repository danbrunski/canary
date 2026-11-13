"""The catalog reads: behavior surfaces, bindings, the DID catalog, test profiles, OBD and CAN
coverage and the cached DTC and monitor state.

Public (no token) and read-only, like the rest of
:func:`~src.dashboard.routes.public.register_public_routes`. Most re-read their file on every
request so a hand edit shows up without a restart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query

from src.config import CanaryConfig
from src.control.dtc_state import DtcState
from src.control.monitor_state import MonitorState
from src.daemon.behavior_live import BehaviorLiveState
from src.dashboard.readers import (
    polled_did_names,
    read_behavior_models,
    read_bindings_for_display,
    read_can_coverage,
    read_obd_coverage,
)


def register_catalog_read_routes(
    app: FastAPI,
    *,
    config: CanaryConfig,
    behavior_models_path: Path,
    bindings_path: Path,
    dids_path: Path,
    test_profiles_dir: Path,
    behavior_live: BehaviorLiveState | None,
    dtc_state: DtcState | None = None,
    monitor_state: MonitorState | None = None,
) -> None:
    @app.get("/models/behavior")
    def get_behavior_models() -> dict[str, Any]:
        # Re-read on every request so a `scripts/refit_behavior_models.py`
        # run lands in the dashboard without a daemon restart. Cheap — the
        # file is < 100 KB even with a full surface catalog.
        return read_behavior_models(behavior_models_path)

    @app.get("/models/behavior/live")
    def get_behavior_live(
        series: bool = Query(default=True),
    ) -> dict[str, Any]:
        # Live "you are here" overlay for Models▸Behavior: per-model
        # expected/actual/deviation/severity + a short server-side trail,
        # fed by the daemon's inference loop. Empty (not an error) when no
        # daemon is wired — the static surfaces still render standalone.
        # series=false drops the rolling trail (the bulk of the payload) for
        # callers that only need the latest snapshot.
        if behavior_live is None:
            return {"observed": 0, "timestamp": None, "operating_mode": None, "models": {}}
        return behavior_live.snapshot(include_series=series)

    @app.get("/bindings")
    def get_bindings() -> dict[str, Any]:
        # Re-read per request so a hand-edit to bindings.yaml (during RE)
        # surfaces in the dashboard immediately. The file is small.
        return read_bindings_for_display(bindings_path)

    @app.get("/dids")
    def get_dids() -> dict[str, Any]:
        # Read-only Nissan Mode-22 DID catalog for the 6.16 workbench.
        # Re-read per request like /bindings so a hand-edit during RE shows
        # up immediately; empty doc when the file is absent.
        from src.decode.dids import load_did_map

        if not dids_path.exists():
            return {"schema_version": None, "vehicle": {}, "access": None, "dids": [], "polled": []}
        did_map = load_did_map(dids_path)
        payload = did_map.model_dump(mode="json")
        # Which catalogued DIDs the daemon actually polls (`obd_dids`). The
        # catalog is the whole RE record, so most entries are characterized
        # but not polled — without this the dashboard can't tell "not asked
        # for" from "asked for, no answer", and reads every unpolled entry
        # as a failure.
        payload["polled"] = polled_did_names(did_map, config.collection.obd_dids)
        return payload

    @app.get("/test-profiles")
    def get_test_profiles() -> dict[str, Any]:
        # Read-only catalog for the Test panel, scanned per request so a
        # dropped-in / hand-edited profile YAML appears without a daemon
        # restart. Unauthenticated like /dids + /bindings (no secrets;
        # listing profiles must work before the control token is entered).
        from src.control.test_profiles import load_test_profiles

        return load_test_profiles(test_profiles_dir).model_dump(mode="json")

    @app.get("/obd/coverage")
    def get_obd_coverage() -> dict[str, Any]:
        # Read-only honesty surface (Task 6.3 / Risk R7): which public PIDs
        # the ECU supports, which CANary decodes, which are undecoded gaps.
        # Also lists custom PIDs (Task 2.8a) — no secrets in `custom_pids.yaml`,
        # same public/unauthenticated posture as the DID catalog below.
        return read_obd_coverage(
            config.collection.storage_path,
            custom_pids_path=config.daemon.custom_pids,
        )

    @app.get("/can/coverage")
    def get_can_coverage() -> dict[str, Any]:
        # Static catalog of decodable broadcast-CAN signals (bindings + DBC)
        # with their source CAN IDs. The CAN tab overlays live values and
        # discovered/undecoded IDs from the telemetry stream client-side.
        return read_can_coverage(bindings_path)

    @app.get("/obd/dtc")
    def get_obd_dtc() -> dict[str, Any]:
        # Task 1.5: the cached DtcReport (Task 1.4) the periodic Mode
        # 03/07/0A poll (Task 1.3) keeps current — stored/pending/permanent
        # codes, MIL state, and staleness. Empty-but-shaped when no poll has
        # landed yet (daemon just started, or DTC polling is disabled).
        if dtc_state is None:
            return {
                "captured_at": None,
                "stored": [],
                "pending": [],
                "permanent": [],
                "mil_on": None,
                "dtc_count": 0,
                "freeze_frame": None,
                "stale": True,
            }
        return dtc_state.snapshot()

    @app.get("/obd/monitors")
    def get_obd_monitors() -> dict[str, Any]:
        # Task 3.1: the cached MonitorState (Mode 06 on-board monitor
        # results — misfire counts + monitor value/min/max margins) the
        # periodic Mode 06 poll keeps current. Empty-but-shaped when no poll
        # has landed yet, same convention as /obd/dtc above.
        if monitor_state is None:
            return {
                "captured_at": None,
                "misfire_counts": {},
                "results": [],
                "stale": True,
            }
        return monitor_state.snapshot()
