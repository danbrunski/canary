"""Pydantic request models for dashboard control routes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SequenceDetectorRequest(_ControlRequest):
    #: Selector value for the backbone to activate (registry: lstm | tcn).
    backbone: Literal["lstm", "tcn"]


class CaptureStartRequest(_ControlRequest):
    purpose: str | None = None
    protocol: str | None = None
    vehicle_state: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class CaptureTagRequest(_ControlRequest):
    label: str
    kind: str = "event"
    timestamp: float | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class CaptureStopRequest(_ControlRequest):
    reason: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class JobSubmitRequest(_ControlRequest):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)


class SessionMetadataRequest(_ControlRequest):
    metadata: dict[str, Any] = Field(default_factory=dict)
    merge: bool = True


class SessionPinRequest(_ControlRequest):
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelUploadRequest(_ControlRequest):
    artifact_b64: str
    manifest: dict[str, Any]


class FeedbackRequest(_ControlRequest):
    """Operator verdict on one anomaly event."""

    event: dict[str, Any]
    label: str
    reason: str | None = None
    driving_context: dict[str, Any] = Field(default_factory=dict)
    labeled_by: str | None = None


class TuningUpdateRequest(_ControlRequest):
    """Replace the tuning config. ``version`` is auto-bumped."""

    config: dict[str, Any]
    change: str = "tuning update"
    by: str | None = None


class TuningRevertRequest(_ControlRequest):
    version: int
    by: str | None = None


class ReplayStartRequest(_ControlRequest):
    """Start a replay of a stored session into the live capture pipeline."""

    session_id: str = Field(..., min_length=1)
    speed: float = Field(default=1.0, gt=0.0, le=100.0)
    loop: bool = False


__all__ = [
    "CaptureStartRequest",
    "CaptureStopRequest",
    "CaptureTagRequest",
    "FeedbackRequest",
    "JobSubmitRequest",
    "ModelUploadRequest",
    "ReplayStartRequest",
    "SessionMetadataRequest",
    "SessionPinRequest",
    "TuningRevertRequest",
    "TuningUpdateRequest",
]
