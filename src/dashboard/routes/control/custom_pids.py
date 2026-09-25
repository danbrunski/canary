"""Custom PIDs (Task 2.8c): CRUD and Torque CSV import over ``data/custom_pids.yaml``.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from src.collection.custom_pids import (
    CustomPid,
    CustomPidSet,
    load_custom_pids,
    save_custom_pids,
    torque_csv_to_custom_pids,
)
from src.dashboard.routes.control.context import ControlContext
from src.dashboard.schemas import (
    CustomPidCreateRequest,
    CustomPidCsvImportRequest,
)


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    config = ctx.config
    publish_control_event = ctx.publish_control_event

    @router.get("/control/custom-pids")
    def get_custom_pids() -> dict[str, Any]:
        pid_set = load_custom_pids(config.daemon.custom_pids)
        return {
            "custom_pids": [p.model_dump(mode="json") for p in pid_set.custom_pids],
            "count": len(pid_set.custom_pids),
        }

    @router.post("/control/custom-pids", status_code=201)
    def post_custom_pids(
        request: CustomPidCreateRequest,
    ) -> dict[str, Any]:
        pid_set = load_custom_pids(config.daemon.custom_pids)
        try:
            new_pid = CustomPid(**request.model_dump())
            updated = CustomPidSet(custom_pids=[*pid_set.custom_pids, new_pid])
        except ValueError as exc:
            # Covers both CustomPid's own validators (read-only mode, safe
            # formula) and CustomPidSet's duplicate-name check.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        save_custom_pids(updated, config.daemon.custom_pids)
        publish_control_event("custom_pid.add", "ok", {"name": new_pid.name})
        return {"custom_pid": new_pid.model_dump(mode="json")}

    @router.delete("/control/custom-pids/{name}")
    def delete_custom_pid(
        name: str,
    ) -> dict[str, Any]:
        pid_set = load_custom_pids(config.daemon.custom_pids)
        remaining = [p for p in pid_set.custom_pids if p.name != name]
        if len(remaining) == len(pid_set.custom_pids):
            raise HTTPException(status_code=404, detail=f"no custom PID named {name!r}")
        save_custom_pids(CustomPidSet(custom_pids=remaining), config.daemon.custom_pids)
        publish_control_event("custom_pid.delete", "ok", {"name": name})
        return {"name": name, "deleted": True}

    @router.post("/control/custom-pids/import")
    def post_custom_pids_import(
        request: CustomPidCsvImportRequest,
    ) -> dict[str, Any]:
        """Torque Pro custom-PID CSV import. A row that names a write mode,
        has a bad formula, or duplicates an already-defined name is skipped
        with a warning — the rest of the import still lands."""
        accepted, warnings = torque_csv_to_custom_pids(request.csv_text)
        pid_set = load_custom_pids(config.daemon.custom_pids)
        existing_names = {p.name for p in pid_set.custom_pids}
        added: list[CustomPid] = []
        for candidate in accepted:
            if candidate.name in existing_names:
                warnings.append(f"{candidate.name!r} already exists; skipped")
                continue
            added.append(candidate)
            existing_names.add(candidate.name)
        if added:
            save_custom_pids(
                CustomPidSet(custom_pids=[*pid_set.custom_pids, *added]),
                config.daemon.custom_pids,
            )
            publish_control_event(
                "custom_pid.import",
                "ok",
                {"added": [p.name for p in added], "warnings": warnings},
            )
        return {
            "added": [p.model_dump(mode="json") for p in added],
            "added_count": len(added),
            "warnings": warnings,
        }

    return router
