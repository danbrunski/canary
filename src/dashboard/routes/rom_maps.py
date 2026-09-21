"""Bounded, authenticated, in-memory ROM inspection; no upload persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from src.analysis.rom_targets import TargetEditor, sha256
from src.analysis.tune_revisions import load_tune_revisions
from src.analysis.tuning_frame import load_provider_registry
from src.config import CanaryConfig
from src.dashboard.security import _client_is_loopback, _require_control_auth

MAX_ROM_UPLOAD = 2 * 1024 * 1024


class TableInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["fuel_targets", "fuel_compensation"]
    title: str
    x_label: str
    x_unit: str
    y_label: str
    y_unit: str
    unit: str


class ViewerDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    map_slots: list[int] = Field(min_length=1)
    target_definition: str
    tables: list[TableInfo]
    warnings: list[str] = Field(default_factory=list)


def inspect_rom(data: bytes, profile_dir: Path) -> dict[str, Any]:
    if not data or len(data) > MAX_ROM_UPLOAD:
        raise ValueError("ROM upload is empty or exceeds 2 MiB")
    definition = ViewerDefinition.model_validate(
        yaml.safe_load((profile_dir / "rom_viewer.yaml").read_text())
    )
    editor = TargetEditor.from_bytes(data, profile_dir / definition.target_definition)
    target = editor.export()
    tables = []
    for slot in definition.map_slots:
        for info in definition.tables:
            available = info.kind == "fuel_targets" and slot == editor.definition.map_slot
            tables.append(
                {
                    **info.model_dump(),
                    "slot": slot,
                    "status": "available" if available else "tbd",
                    "reason": None
                    if available
                    else "TBD — complete table location and axes not verified.",
                    "x_axis": target["bfs_ms"] if available else [],
                    "y_axis": target["rpm"] if available else [],
                    "values": target["afr"] if available else [],
                    "raw": target["raw"] if available else [],
                }
            )
    providers = load_provider_registry(profile_dir / "tuning_providers.yaml")
    live = {}
    for role in ("rpm", "base_fuel_schedule", "commanded_afr"):
        provider = next((p for p in providers.providers if p.role == role), None)
        live[role] = (
            provider.model_dump() if provider and provider.confidence == "confirmed" else None
        )
    revisions = load_tune_revisions(profile_dir / "tune_revisions.yaml")
    current = revisions.at(datetime.now(UTC)) if revisions else None
    return {
        "source_sha256": sha256(data),
        "revision": target["revision"],
        "recorded_revision": current.revision if current else None,
        "matches_recorded_revision": bool(current and current.rom_sha256 == sha256(data)),
        "map_slots": definition.map_slots,
        "tables": tables,
        "live": live,
        "warnings": definition.warnings,
        "afr_stoich": editor.definition.afr_numerator / 128,
    }


def register_rom_routes(
    app: FastAPI, *, config: CanaryConfig, resolved_control_token: str | None
) -> None:
    def authorize(request: Request, authorization: str | None = Header(default=None)) -> None:
        _require_control_auth(
            resolved_control_token, authorization, client_is_loopback=_client_is_loopback(request)
        )

    @app.post("/control/rom/inspect", dependencies=[Depends(authorize)])
    async def post_rom_inspect(request: Request) -> dict[str, Any]:
        """Raw octet-stream upload. Decode only; never select a tune or flash."""
        if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
            raise HTTPException(415, "Send an OsirisROM file as application/octet-stream")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_ROM_UPLOAD:
                raise HTTPException(413, "ROM exceeds 2 MiB limit")
            body.extend(chunk)
        try:
            return await run_in_threadpool(
                inspect_rom, bytes(body), config.daemon.afr_calibration.parent
            )
        except FileNotFoundError as exc:
            raise HTTPException(409, "ROM map definitions unavailable for this vehicle") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
