"""Bounded, authenticated, in-memory ROM inspection; no upload persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from src.analysis.ecu_application import coverage_report, load_application_model
from src.analysis.rom_targets import Definition, TargetEditor, sha256
from src.analysis.rom_trace import (
    load_viewer_definition,
    trace_payload,
)
from src.analysis.tuning_frame import load_provider_registry
from src.config import CanaryConfig
from src.control.rom_library import MAX_ROM_UPLOAD
from src.dashboard.security import (
    control_auth_dependency,
)
from src.paths import DIDS_YAML
from src.vehicle.profiles import active_registry
from src.vehicle.profiles import profile_dir as vehicle_profile_dir
from src.vehicle.tune_revisions import TuneRevisionRegistry
from src.yaml_load import load_yaml


async def read_rom_upload(request: Request, *, wrong_type: str, too_large: str) -> bytes:
    """The body of an ``application/octet-stream`` ROM upload, size-capped.

    The one copy of the block every ROM upload route needs: 415 (``wrong_type``)
    for any other content type, 413 (``too_large``) the moment the streamed body
    passes :data:`src.control.rom_library.MAX_ROM_UPLOAD`, before the rest is
    buffered. The messages stay each route's own.
    """
    if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
        raise HTTPException(415, wrong_type)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_ROM_UPLOAD:
            raise HTTPException(413, too_large)
        body.extend(chunk)
    return bytes(body)


def inspect_rom(
    data: bytes,
    profile_dir: Path,
    dids_path: Path = DIDS_YAML,
    *,
    revisions: TuneRevisionRegistry | None,
) -> dict[str, Any]:
    """Decode one uploaded ROM against the vehicle's map definitions.

    ``revisions`` is required, not derived from ``profile_dir``: the live
    flash history is :func:`src.vehicle.profiles.active_registry` — the operational
    copy merged with the tracked file — and the tracked file alone would miss
    a flash recorded from the dashboard (R05.1, R05.17).
    """
    if not data or len(data) > MAX_ROM_UPLOAD:
        raise ValueError("ROM upload is empty or exceeds 2 MiB")
    definition = load_viewer_definition(profile_dir)
    editor = TargetEditor.from_bytes(data, profile_dir / definition.target_definition)
    target = editor.export()
    tables = []
    for slot in definition.map_slots:
        for info in definition.tables:
            available = info.kind == "fuel_targets" and slot == editor.definition.map_slot
            tables.append(
                {
                    **info.model_dump(exclude={"trace"}),
                    # W05.1: the ECM's own coordinates into this table and
                    # whether their mapping onto its axes is established.
                    "trace": trace_payload(info.trace, dids_path) if available else None,
                    "slot": slot,
                    "status": "available" if available else "tbd",
                    "reason": None
                    if available
                    else "TBD — complete table location and axes not verified.",
                    "x_axis": target["bfs_ms"] if available else [],
                    "y_axis": target["rpm"] if available else [],
                    # The ROM's axis bytes, which a `raw` trace axis lands on.
                    "x_axis_raw": target["bfs_raw"] if available else [],
                    "y_axis_raw": target["rpm_raw"] if available else [],
                    "values": target["afr"] if available else [],
                    "raw": target["raw"] if available else [],
                }
            )
    current = revisions.at(datetime.now(UTC)) if revisions else None
    return {
        "source_sha256": sha256(data),
        "revision": target["revision"],
        "recorded_revision": current.revision if current else None,
        "matches_recorded_revision": bool(current and current.rom_sha256 == sha256(data)),
        "map_slots": definition.map_slots,
        "tables": tables,
        "live": live_providers(profile_dir),
        "warnings": definition.warnings,
        # M01.3: the AFR the ECM states at lambda = 1 (its tables' numerator),
        # not the fuel's stoich. ``afr_stoich`` is the same number under its
        # old, misleading name, kept for a dashboard bundle built before it.
        "afr_scale": editor.definition.afr_numerator / 128,
        "afr_stoich": editor.definition.afr_numerator / 128,
    }


def live_providers(profile_dir: Path) -> dict[str, Any]:
    """The live-channel providers the map lookup reads, by role. A role whose
    provider is not ``confirmed`` is ``None``: the page then has no input for
    it and says so, rather than reading an unconfirmed channel."""
    providers = load_provider_registry(profile_dir / "tuning_providers.yaml")
    live: dict[str, Any] = {}
    for role in ("rpm", "base_fuel_schedule", "commanded_afr"):
        provider = next((p for p in providers.providers if p.role == role), None)
        live[role] = (
            provider.model_dump() if provider and provider.confidence == "confirmed" else None
        )
    return live


def library_trace_context(
    profile_dir: Path,
    dids_path: Path = DIDS_YAML,
    *,
    sha: str,
    revisions: TuneRevisionRegistry | None,
) -> dict[str, Any]:
    """What the ROM Editor's live overlay needs beyond a library image's own
    tables: the vehicle's trace definitions, the live providers, and whether
    the image is the revision recorded as running now.

    Everything here comes from the vehicle profile and the flash history; the
    image's cells and axes come from ``GET .../tables``. It is the part of
    :func:`inspect_rom` that does not depend on a ``.OsirisROM`` container, so
    the editor reads it for a plain library image (``image_sha256`` is what the
    flash history names an image by). The overlay is only meaningful on the
    revision running now, and the page refuses it otherwise; this states which
    that is.
    """
    definition = load_viewer_definition(profile_dir)
    targets = Definition.model_validate(
        load_yaml((profile_dir / definition.target_definition).read_text())
    )
    current = revisions.at(datetime.now(UTC)) if revisions else None
    return {
        "sha256": sha,
        # The slot the trace definitions apply to: the one the target
        # definition (and so the ECM's active-slot check) is written for.
        "map_slot": targets.map_slot,
        # M01.3: the ECM's AFR at lambda = 1, not the fuel's stoich;
        # ``afr_stoich`` is the old name for the same number (older bundles).
        "afr_scale": targets.afr_numerator / 128,
        "afr_stoich": targets.afr_numerator / 128,
        "recorded_revision": current.revision if current else None,
        "matches_recorded_revision": bool(current and current.image_sha256 == sha),
        "live": live_providers(profile_dir),
        "warnings": definition.warnings,
        "tables": [
            {**info.model_dump(exclude={"trace"}), "trace": trace_payload(info.trace, dids_path)}
            for info in definition.tables
        ],
    }


def application_model_report(
    profile_dir: Path, *, revisions: TuneRevisionRegistry | None
) -> dict[str, Any]:
    """E05.1: the vehicle's ECM application model as the dashboard reads it --
    which outputs are qualified or conditional, what the twin implements,
    observes and assumes on each path, the blockers, and the logic still
    uncovered -- against the image recorded as running now."""
    model, model_sha = load_application_model(profile_dir)
    current = revisions.at(datetime.now(UTC)) if revisions else None
    image = current.image_sha256 if current else None
    return coverage_report(model, model_sha, image_sha256=image)


def register_rom_routes(
    app: FastAPI,
    *,
    config: CanaryConfig,
    resolved_control_token: str | None,
    dids_path: Path = DIDS_YAML,
) -> None:
    authorize = control_auth_dependency(resolved_control_token)

    @app.get("/control/ecu/application-model", dependencies=[Depends(authorize)])
    def get_application_model() -> dict[str, Any]:
        """Read-only coverage of the ECM application twin (E05.1)."""
        try:
            return application_model_report(
                vehicle_profile_dir(config), revisions=active_registry(config)
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, "No ECU application model for this vehicle") from exc

    @app.post("/control/rom/inspect", dependencies=[Depends(authorize)])
    async def post_rom_inspect(request: Request) -> dict[str, Any]:
        """Raw octet-stream upload. Decode only; never select a tune or flash."""
        body = await read_rom_upload(
            request,
            wrong_type="Send an OsirisROM file as application/octet-stream",
            too_large="ROM exceeds 2 MiB limit",
        )
        try:
            return await run_in_threadpool(
                inspect_rom,
                body,
                vehicle_profile_dir(config),
                dids_path,
                revisions=active_registry(config),
            )
        except FileNotFoundError as exc:
            raise HTTPException(409, "ROM map definitions unavailable for this vehicle") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
