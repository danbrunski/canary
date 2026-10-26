"""The ROM library, image compare and the ECU backup job (W08.3-W08.5, W08.7).

Every route requires the control token, like ``/control/rom/inspect``. The
library stores plain images per vehicle under ``daemon.rom_library_dir``
(outside the checkout); the backup job reads the ECU's flash with the
vehicle's recipe (its profile's, else its platform's) and stores the result as
``source: ecu_read``, whether or not the vehicle has a ROM map yet. Nothing
here writes the ECU.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from src.collection import tuning_profile
from src.collection.can_bus import bus_factory
from src.config import CanaryConfig
from src.control.capture import CaptureController
from src.control.rom_backup import (
    ROM_BACKUP_FILE,
    BackupRefusedError,
    RomBackupJob,
    VehicleReading,
    recipe_extent,
)
from src.control.rom_library import (
    MAX_ROM_UPLOAD,
    RomLibrary,
    RomLibraryError,
    RomNotFoundError,
    valid_sha,
)
from src.dashboard.routes.rom_maps import library_trace_context
from src.dashboard.security import control_auth_dependency
from src.dashboard.status import derive_vehicle_state_from_storage
from src.paths import DIDS_YAML
from src.vehicle.platforms import VehicleDataFile, config_data_file
from src.vehicle.profiles import active_registry
from src.vehicle.profiles import profile_dir as vehicle_profile_dir
from src.vehicle.tune_revisions import TuneRevisionRegistry
from src.vehicle_state import (
    FRESHNESS_THRESHOLD_SECONDS,
    LiveStateSource,
    ReplayStatusSource,
    derive_from_inputs,
    running_replay_id,
)


class RomLabelRequest(BaseModel):
    """``PATCH /control/rom/library/{sha}``: the label is the only editable field."""

    model_config = ConfigDict(extra="forbid")
    label: str | None


def vehicle_reading(
    config: CanaryConfig,
    *,
    live_source: LiveStateSource | None,
    replay_controller: ReplayStatusSource | None,
) -> VehicleReading:
    """The backup job's view of ``vehicle_state``, from the same classifier.

    The live source gives the broadcast engine speed the job watches while
    OBD polling is paused; without one (live inference off) the state comes
    from storage and the watch is unavailable, which refuses the start.
    """
    now = datetime.now(UTC)
    if live_source is None:
        state = derive_vehicle_state_from_storage(
            config.collection.storage_path,
            replay_controller=replay_controller,  # type: ignore[arg-type]
        )
        return VehicleReading(
            state=state.state,
            rpm=state.rpm,
            broadcast_rpm=None,
            obd_freshness_seconds=state.obd_freshness_seconds,
            watch_unavailable="no live telemetry source; live inference is not running",
        )
    inputs = live_source.vehicle_state_inputs(now)
    state = derive_from_inputs(inputs, running_replay_id(replay_controller), now)
    can_age = inputs.get("can_freshness_seconds")
    can_fresh = can_age is not None and can_age <= FRESHNESS_THRESHOLD_SECONDS
    broadcast = inputs.get("broadcast_rpm") if can_fresh else None
    return VehicleReading(
        state=state.state,
        rpm=state.rpm,
        broadcast_rpm=broadcast,
        obd_freshness_seconds=state.obd_freshness_seconds,
        watch_unavailable=None
        if broadcast is not None
        else "broadcast engine speed absent or stale",
    )


def capture_activity(capture_controller: CaptureController | None) -> str | None:
    """An active profile capture or capture window, named; ``None`` when idle.

    The same fact ``scripts/restart_canary.sh`` refuses on
    (``/status.capture_profile.active``), plus labelled capture windows.
    """
    if tuning_profile.is_active():
        return f"capture profile {tuning_profile.active_profile_name()!r} is active"
    if capture_controller is not None:
        current = capture_controller.current_capture()
        if current is not None:
            return f"capture window {current.purpose or current.capture_id!r} is active"
    return None


def _download_name(entry: dict[str, Any]) -> str:
    stem = entry.get("label") or entry.get("filename") or entry.get("calibration_id") or "rom"
    stem = re.sub(r"\.(bin|osirisrom)$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or "rom"
    return f"{stem}-{entry['sha256'][:8]}.bin"


def register_rom_library_routes(
    app: FastAPI,
    *,
    config: CanaryConfig,
    resolved_control_token: str | None,
    live_source: Any = None,
    replay_controller: Any = None,
    capture_controller: CaptureController | None = None,
    publish_control_event: Any = None,
    dids_path: Path = DIDS_YAML,
) -> RomBackupJob:
    """Register the library, compare and backup routes; returns the job."""
    authorize = control_auth_dependency(resolved_control_token)
    profile = vehicle_profile_dir(config)

    def registry() -> TuneRevisionRegistry | None:
        # May raise (a conflicting or malformed flash history): the library
        # reports that as revision_match status "unknown" with the reason.
        return active_registry(config)

    def recipe_file() -> VehicleDataFile:
        return config_data_file(config, ROM_BACKUP_FILE)

    library = RomLibrary(
        config.daemon.rom_library_dir,
        profile_dir=profile,
        registry=registry,
        extent_spec=lambda: recipe_extent(recipe_file),
    )
    job = RomBackupJob(
        recipe_file=recipe_file,
        library=library,
        open_bus=lambda: bus_factory(config.hardware.can),
        vehicle=lambda: vehicle_reading(
            config, live_source=live_source, replay_controller=replay_controller
        ),
        capture_active=lambda: capture_activity(capture_controller),
        ecu_calibration_id=lambda: tuning_profile.calibration_snapshot()["ecu_calibration_id"],
        obd_source=config.collection.obd_source,
        publish=publish_control_event,
    )
    app.state.rom_library = library
    app.state.rom_backup_job = job

    # Daemon shutdown (or restart) mid-read: cancel the job so it sends the
    # recipe's exit sequence, releases the ECM and records its end event
    # instead of vanishing with the process. Composed around whatever
    # lifespan the app already has.
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan(lifespan_app: Any) -> AsyncIterator[Any]:
        async with previous_lifespan(lifespan_app) as state:
            try:
                yield state
            finally:
                await run_in_threadpool(job.shutdown)

    app.router.lifespan_context = _lifespan

    def _sha(sha: str) -> str:
        if not valid_sha(sha):
            raise HTTPException(404, f"no ROM library entry {sha!r}")
        return sha

    async def _call(fn: Any, *args: Any) -> Any:
        try:
            return await run_in_threadpool(fn, *args)
        except RomNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                409, f"ROM map definition unavailable for this vehicle: {exc}"
            ) from exc
        except (RomLibraryError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/control/rom/library", dependencies=[Depends(authorize)])
    async def post_rom_library(
        request: Request, filename: str = Query(min_length=1, max_length=255)
    ) -> dict[str, Any]:
        """Octet-stream upload: a plain image or an ``.OsirisROM``. Refusals
        name the cause (422); over 2 MiB is 413."""
        if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
            raise HTTPException(415, "Send the ROM file as application/octet-stream")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_ROM_UPLOAD:
                raise HTTPException(413, "ROM exceeds the 2 MiB upload limit")
            body.extend(chunk)
        entry, created = await _call(library.add_upload, bytes(body), filename)
        return {**entry, "created": created}

    @app.get("/control/rom/library", dependencies=[Depends(authorize)])
    async def get_rom_library() -> dict[str, Any]:
        entries = await _call(library.entries)
        return {"entries": entries, "count": len(entries)}

    @app.get("/control/rom/library/{sha}", dependencies=[Depends(authorize)])
    async def get_rom_library_entry(sha: str) -> dict[str, Any]:
        result: dict[str, Any] = await _call(library.describe, _sha(sha))
        return result

    @app.get("/control/rom/library/{sha}/download", dependencies=[Depends(authorize)])
    async def get_rom_library_download(sha: str) -> Response:
        image = await _call(library.image, _sha(sha))
        entry = await _call(library.entry, sha)
        return Response(
            content=image,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{_download_name(entry)}"'},
        )

    @app.get("/control/rom/library/{sha}/tables", dependencies=[Depends(authorize)])
    async def get_rom_library_tables(sha: str) -> dict[str, Any]:
        result: dict[str, Any] = await _call(library.tables, _sha(sha))
        return result

    @app.get("/control/rom/library/{sha}/trace", dependencies=[Depends(authorize)])
    async def get_rom_library_trace(sha: str) -> dict[str, Any]:
        """The live overlay's inputs for one image (ROM Editor): the vehicle's
        trace definitions per table kind, the live providers, and whether this
        image is the revision recorded as running now. Read-only; the image's
        cells and axes are ``GET .../tables``. 409 when the vehicle has no
        ``rom_viewer.yaml``."""
        _sha(sha)
        await _call(library.entry, sha)  # 404 for an image not in the library
        result: dict[str, Any] = await _call(
            lambda: library_trace_context(profile, dids_path, sha=sha, revisions=registry())
        )
        return result

    @app.patch("/control/rom/library/{sha}", dependencies=[Depends(authorize)])
    async def patch_rom_library_entry(sha: str, request: RomLabelRequest) -> dict[str, Any]:
        result: dict[str, Any] = await _call(library.set_label, _sha(sha), request.label)
        return result

    @app.delete("/control/rom/library/{sha}", dependencies=[Depends(authorize)])
    async def delete_rom_library_entry(sha: str) -> dict[str, Any]:
        await _call(library.delete, _sha(sha))
        return {"deleted": sha}

    @app.get("/control/rom/compare", dependencies=[Depends(authorize)])
    async def get_rom_compare(
        a: str = Query(min_length=64, max_length=64), b: str = Query(min_length=64, max_length=64)
    ) -> dict[str, Any]:
        result: dict[str, Any] = await _call(library.compare, _sha(a), _sha(b))
        return result

    @app.get("/control/rom/backup", dependencies=[Depends(authorize)])
    async def get_rom_backup() -> dict[str, Any]:
        result: dict[str, Any] = await run_in_threadpool(job.state)
        return result

    @app.post("/control/rom/backup", status_code=202, dependencies=[Depends(authorize)])
    async def post_rom_backup() -> dict[str, Any]:
        """Start the read. 409 names every precondition that refused it."""
        try:
            result: dict[str, Any] = await run_in_threadpool(job.start)
        except BackupRefusedError as exc:
            raise HTTPException(409, str(exc)) from exc
        return result

    @app.post("/control/rom/backup/cancel", dependencies=[Depends(authorize)])
    async def post_rom_backup_cancel() -> dict[str, Any]:
        try:
            result: dict[str, Any] = await run_in_threadpool(job.cancel)
        except BackupRefusedError as exc:
            raise HTTPException(409, str(exc)) from exc
        return result

    return job


__all__ = [
    "RomLabelRequest",
    "capture_activity",
    "register_rom_library_routes",
    "vehicle_reading",
]
