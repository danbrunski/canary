"""Authenticated calibration history and RAM verification routes."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import can
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.analysis.memory_verification import (
    VerifyAbortedError,
    plan_restamp,
    restamp,
    verify_channel,
)
from src.collection import tuning_profile, tx_gate
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
from src.vehicle.tune_revisions import TuneRevision

#: The ECM-session claim the verifier transmits under (see ``tx_gate``).
VERIFY_HOLDER: tx_gate.EcmSessionHolder = "memory_verify"
#: Renewed before every sample, so a wedged verify cannot hold the ECM past
#: it and a long channel cannot outlive it.
VERIFY_CLAIM_TTL_SECONDS = 60.0
#: After claiming and before the first 10 C0: let an exchange the poll loop
#: already had in flight finish (the ROM backup's pre-read listen, 0.5 s).
VERIFY_SETTLE_SECONDS = 0.5
#: One verify at a time: the refusal check and the claim are otherwise
#: check-then-act, and two requests would both pass the check.
_VERIFY_LOCK = threading.Lock()


def _settle(bus: Any, seconds: float) -> None:
    """Drain the bus for `seconds` (frames are discarded: the claim is held)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            bus.recv(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
        except (ValueError, IndexError, OSError, can.CanError):
            continue


def keep_verify_claim() -> None:
    """Before every sample: stop if the ECM is no longer ours alone, else renew.

    ``memory_verify`` ranks below every other holder, so it owns the session
    exactly when its claim is the only one. Another claim (a foreign tester,
    the K-line reader, the ROM backup), passive armed, or the claim having
    lapsed all stop the verify with the claimant named.
    """
    if tx_gate.passive_armed():
        raise VerifyAbortedError("memory verification stopped: passive mode was armed mid-verify")
    owner = tx_gate.ecm_session_owner()
    if owner == "can_diagnostics":
        raise VerifyAbortedError(
            "memory verification stopped: its ECM-session claim lapsed mid-verify"
        )
    if owner != VERIFY_HOLDER:
        raise VerifyAbortedError(
            f"memory verification stopped: the ECM diagnostic session was taken by {owner!r}"
        )
    tx_gate.claim_ecm_session(VERIFY_HOLDER, ttl_seconds=VERIFY_CLAIM_TTL_SECONDS)


def verify_refusal() -> str | None:
    """Why the verifier may not take the ECM session now, or ``None``.

    The verifier enters 0xC0 and, for a Mode 01 relation, exits to 0x81 on
    every sample. Run beside the poll loop, the two would cut across each
    other's sessions on the same ECM (a 0x23 read refused mid-window, a Mode
    22 window ended early), and a correct address would read as failed. So
    it claims the session the way the ROM backup does, which pauses the
    loop's Mode 01, Mode 22 and RAM polling, and refuses by name when it
    cannot: passive armed, or the session already held by someone else.
    """
    if tx_gate.passive_armed():
        return (
            "memory verification refused: passive mode is armed, so another tool "
            "owns the bus; disarm it first"
        )
    if tx_gate.claim_held(VERIFY_HOLDER):
        return "memory verification refused: another verification holds the ECM session"
    owner = tx_gate.ecm_session_owner()
    if owner != "can_diagnostics":
        return f"memory verification refused: the ECM diagnostic session is held by {owner!r}"
    return None


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
    #: sha256 of the plain 1:1 flash image (W08.2). Without it a ROM Library
    #: image or ECU read of this flash cannot be matched to its revision.
    image_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
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
        if not _VERIFY_LOCK.acquire(blocking=False):
            raise HTTPException(
                409, "memory verification refused: another verification is already running"
            )
        try:
            return _verify(body)
        finally:
            _VERIFY_LOCK.release()

    def _verify(body: VerifyRequest) -> dict[str, Any]:
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
        refusal = verify_refusal()
        if refusal is not None:
            raise HTTPException(409, refusal)
        tx_gate.claim_ecm_session(VERIFY_HOLDER, ttl_seconds=VERIFY_CLAIM_TTL_SECONDS)
        try:
            try:
                bus = bus_factory(config.hardware.can)
            except (OSError, can.CanError) as exc:
                raise HTTPException(503, f"could not open diagnostic bus: {exc}") from exc
            client: CanObdClient | None = None
            try:
                client = CanObdClient(
                    bus,
                    request_id=access.ecu_request_id,
                    response_id_min=access.ecu_response_id,
                    response_id_max=access.ecu_response_id,
                    tx_holder=VERIFY_HOLDER,
                )
                _settle(bus, VERIFY_SETTLE_SECONDS)
                if access.session is not None and not any(
                    client.enter_diagnostic_session(
                        access.session, request_id=access.ecu_request_id
                    )
                    for _attempt in range(2)  # one retry; any() stops at the first yes
                ):
                    raise HTTPException(
                        503,
                        f"session_entry_refused: session 0x{access.session:02X} was not "
                        "entered after a retry; nothing was verified or recorded",
                    )
                verdicts = []
                for channel in channels:
                    verdicts.append(
                        verify_channel(
                            client,
                            channel,
                            catalog=did_map.memory,
                            samples=body.samples,
                            request_id=access.ecu_request_id,
                            session=access.session,
                            exit_session=access.exit_session,
                            exit_grace_seconds=access.exit_grace_seconds,
                            before_sample=keep_verify_claim,
                        )
                    )
            except VerifyAbortedError as exc:
                # Nothing is recorded: a channel cut off by another tester has
                # not been shown to fail, and the ones before it are re-run.
                raise HTTPException(409, f"{exc}; nothing was recorded") from exc
            finally:
                if client is not None and access.exit_session is not None:
                    try:
                        client.enter_diagnostic_session(
                            access.exit_session,
                            request_id=access.ecu_request_id,
                        )
                    except (OSError, can.CanError):
                        pass
                bus.shutdown()
        finally:
            tx_gate.release_ecm_session(VERIFY_HOLDER)
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
