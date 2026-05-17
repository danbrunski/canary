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


class CaptureConflictError(RuntimeError):
    """Raised when a capture command conflicts with the current state."""


class NoActiveCaptureError(RuntimeError):
    """Raised when a command requires an active capture window."""


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
                raise NoActiveCaptureError("no active capture")
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
                raise NoActiveCaptureError("no active capture")
            state = self._active
            tag_fields: dict[str, Any] = {
                "capture_id": state.capture_id,
                "session_id": state.session_id,
            }
            if reason is not None:
                tag_fields["reason"] = reason
            tag_fields.update(dict(fields or {}))
            write_tag(state.session_path, "capture_stop", kind="capture", **tag_fields)
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
]
