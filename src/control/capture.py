"""Capture-window control for the headless daemon.

The daemon owns the CAN adapter and writes telemetry continuously. A remote
"capture" is therefore a labeled window on the current daemon session, not a
separate recorder process. The controller writes durable tags into the active
session so later analysis can recover what happened even if the API process
restarts.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.collection.tags import write_tag
from src.storage.sessions import write_session_metadata


class CaptureConflictError(RuntimeError):
    """Raised when a capture command conflicts with the current state."""


class NoActiveCaptureError(RuntimeError):
    """Raised when a command requires an active capture window."""


def no_active_capture_detail(outcome: str) -> str:
    """Why a capture command found nothing, and what to do about it.

    Phrased once, here, because the interesting part is never "no active
    capture" — it is *which kind of empty*. The window lives only in this
    process's memory, so a daemon restart silently ends it while telemetry
    recording carries on; that is what happened on 2026-09-17, when a deploy
    restart dropped the drive's third window and a later tag had nothing to
    attach to. Every caller (both control routes, the MCP write tools, the
    CLI) surfaces this string, so the explanation cannot be attached at one
    call site and missed at another.
    """
    return (
        f"no active capture window, so {outcome}. The window is held in daemon "
        "memory only: a daemon restart (deploy, crash, power cycle) ends it "
        "while telemetry recording continues, and it is not resumed. Check "
        "GET /control/captures/current and open a new window with "
        "POST /control/captures."
    )


@dataclass(frozen=True)
class CaptureState:
    """In-memory state for the active remote capture window."""

    capture_id: str
    session_id: str
    session_path: Path
    started_at: str
    purpose: str | None = None
    protocol: str | None = None
    vehicle_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "session_id": self.session_id,
            "session_path": str(self.session_path),
            "started_at": self.started_at,
            "purpose": self.purpose,
            "protocol": self.protocol,
            "vehicle_state": self.vehicle_state,
        }


class CaptureController:
    """Remote capture-window commands backed by `tags.jsonl` side effects."""

    def __init__(self, current_session_path: Callable[[], Path | None]) -> None:
        self._current_session_path = current_session_path
        self._active: CaptureState | None = None
        self._lock = threading.Lock()

    @property
    def active(self) -> CaptureState | None:
        with self._lock:
            return self._active

    def start_capture(
        self,
        *,
        purpose: str | None = None,
        protocol: str | None = None,
        vehicle_state: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> CaptureState:
        """Start a labeled capture window on the daemon's current session."""
        with self._lock:
            if self._active is not None:
                raise CaptureConflictError("a capture is already active")
            session = self._require_session_path()
            state = CaptureState(
                capture_id=uuid4().hex,
                session_id=session.name,
                session_path=session,
                started_at=datetime.now(UTC).isoformat(),
                purpose=purpose,
                protocol=protocol,
                vehicle_state=vehicle_state,
            )
            tag_fields: dict[str, Any] = {
                "capture_id": state.capture_id,
                "session_id": state.session_id,
            }
            if purpose is not None:
                tag_fields["purpose"] = purpose
            if protocol is not None:
                tag_fields["protocol"] = protocol
            if vehicle_state is not None:
                tag_fields["vehicle_state"] = vehicle_state
            tag_fields.update(dict(fields or {}))
            write_tag(session, "capture_start", kind="capture", **tag_fields)
            write_session_metadata(
                session,
                {
                    "active_capture_id": state.capture_id,
                    "capture_started_at": state.started_at,
                    "purpose": purpose,
                    "protocol": protocol,
                    "vehicle_state": vehicle_state,
                    **dict(fields or {}),
                },
            )
            self._active = state
            return state

    def current_capture(self) -> CaptureState | None:
        """Return the active capture window, if any."""
        with self._lock:
            return self._active

    def add_tag(
        self,
        label: str,
        *,
        kind: str = "event",
        timestamp: float | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a tag to the active capture's session."""
        with self._lock:
            if self._active is None:
                raise NoActiveCaptureError(no_active_capture_detail("the tag was not recorded"))
            tag_fields = {
                "capture_id": self._active.capture_id,
                "session_id": self._active.session_id,
                **dict(fields or {}),
            }
            return write_tag(
                self._active.session_path,
                label,
                kind=kind,
                timestamp=timestamp,
                **tag_fields,
            )

    def stop_capture(
        self,
        *,
        reason: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> CaptureState:
        """Stop the active capture window and write a durable stop tag."""
        with self._lock:
            if self._active is None:
                raise NoActiveCaptureError(no_active_capture_detail("nothing was stopped"))
            state = self._active
            tag_fields: dict[str, Any] = {
                "capture_id": state.capture_id,
                "session_id": state.session_id,
            }
            if reason is not None:
                tag_fields["reason"] = reason
            tag_fields.update(dict(fields or {}))
            write_tag(state.session_path, "capture_stop", kind="capture", **tag_fields)
            write_session_metadata(
                state.session_path,
                {
                    "active_capture_id": None,
                    "last_capture_id": state.capture_id,
                    "capture_stopped_at": datetime.now(UTC).isoformat(),
                    "stop_reason": reason,
                    **dict(fields or {}),
                },
            )
            self._active = None
            return state

    def _require_session_path(self) -> Path:
        session = self._current_session_path()
        if session is None:
            raise RuntimeError("daemon has no current session")
        session.mkdir(parents=True, exist_ok=True)
        return session


__all__ = [
    "CaptureConflictError",
    "CaptureController",
    "CaptureState",
    "NoActiveCaptureError",
    "no_active_capture_detail",
]
