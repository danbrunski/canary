"""Authenticated calibration history and RAM verification routes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import can
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.analysis.memory_verification import plan_restamp, restamp, verify_channel
from src.analysis.tune_revisions import TuneRevision
from src.collection import tuning_profile
from src.collection.can_collector import bus_factory
from src.collection.can_obd_client import CanObdClient
from src.config import CanaryConfig
from src.control.calibration import (
    active_registry,
    append_flash,
    calibration_payload,
    memory_channels_payload,
    record_verify_results,
    seed_operational_memory,
)
from src.dashboard.security import (
    control_auth_dependency,
)
from src.decode.dids import load_did_map


class VerifyRequest(BaseModel):
    channels: list[str] = Field(default_factory=list)
    samples: int = Field(default=20, ge=1, le=100)
    record_revision: str | None = None
    carry_reason: str | None = None


class FlashRequest(BaseModel):
    revision: str = Field(min_length=1)
    flashed_at: datetime
    calibration_id: str | None = None
    k: float | None = None
    rom_file: str | None = None
    rom_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    notes: str = ""


def register_calibration_routes(
    app: FastAPI,
    *,
    config: CanaryConfig,
    dids_path: Path,
    resolved_control_token: str | None,
) -> None:
    authorize = control_auth_dependency(resolved_control_token)

    @app.get("/control/calibration", dependencies=[Depends(authorize)])
    def get_calibration() -> dict[str, Any]:
        return calibration_payload(config)

    @app.get("/control/memory-channels", dependencies=[Depends(authorize)])
    def get_memory_channels() -> dict[str, Any]:
        return memory_channels_payload(config, dids_path)

    @app.post("/control/calibration", dependencies=[Depends(authorize)], status_code=201)
    def post_calibration(body: FlashRequest) -> dict[str, Any]:
        try:
            append_flash(config, TuneRevision.model_validate(body.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return calibration_payload(config)

    @app.post("/control/memory-channels/verify", dependencies=[Depends(authorize)])
    def post_verify(body: VerifyRequest) -> dict[str, Any]:
        if tuning_profile.is_active():
            name = tuning_profile.active_profile().name
            raise HTTPException(
                status_code=409,
                detail=f"memory verification is deferred while {name!r} is recording",
            )
        catalog_path = seed_operational_memory(config, dids_path)
        did_map = load_did_map(catalog_path)
        wanted = {name.upper() for name in body.channels}
        channels = [
            channel for channel in did_map.memory if not wanted or channel.name.upper() in wanted
        ]
        missing = wanted - {channel.name.upper() for channel in channels}
        if missing:
            raise HTTPException(404, f"unknown memory channels: {', '.join(sorted(missing))}")
        access = did_map.access
        if access is None:
            raise HTTPException(409, "memory catalog has no diagnostic-session access block")
        try:
            bus = bus_factory(config.hardware.can)
        except (OSError, can.CanError) as exc:
            raise HTTPException(503, f"could not open diagnostic bus: {exc}") from exc
        try:
            client = CanObdClient(
                bus,
                request_id=access.ecu_request_id,
                response_id_min=access.ecu_response_id,
                response_id_max=access.ecu_response_id,
            )
            verdicts = [
                verify_channel(
                    client,
                    channel,
                    samples=body.samples,
                    request_id=access.ecu_request_id,
                    session=access.session,
                )
                for channel in channels
            ]
            if access.exit_session is not None:
                client.enter_diagnostic_session(
                    access.exit_session,
                    request_id=access.ecu_request_id,
                )
        finally:
            bus.shutdown()
        record_verify_results(config, {verdict.channel: verdict.outcome for verdict in verdicts})

        notes: list[str] = []
        changed = 0
        if body.record_revision is not None:
            registry = active_registry(config)
            if registry is None or not any(
                revision.revision == body.record_revision for revision in registry.revisions
            ):
                raise HTTPException(409, "record_revision is not present in flash history")
            stamps, notes = plan_restamp(
                channels,
                verdicts,
                carry_reason=body.carry_reason,
            )
            changed = restamp(catalog_path, stamps, body.record_revision)
        counts = {
            outcome: sum(verdict.outcome == outcome for verdict in verdicts)
            for outcome in ("pass", "fail", "unverifiable")
        }
        return {
            "verdicts": [verdict.to_dict() for verdict in verdicts],
            "counts": counts,
            "recorded": changed,
            "notes": notes,
            "catalog": str(catalog_path),
        }


__all__ = ["register_calibration_routes"]
