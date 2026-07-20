"""Pydantic request models for dashboard control routes."""

from __future__ import annotations

from datetime import date
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


class VehicleEventRequest(_ControlRequest):
    """New vehicle maintenance/intervention ledger entry (Task 3.3)."""

    occurred_at: date
    kind: str
    title: str
    details: str = ""
    odometer_km: float | None = None
    affected_channels: list[str] = Field(default_factory=list)
    corpus_boundary: bool = False


class ProfileNameUpdateRequest(_ControlRequest):
    """Set a vehicle profile's editable display name (blank is allowed — it
    clears the name back to unset)."""

    name: str = Field(..., max_length=255)


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


class UpsKeepaliveRequest(_ControlRequest):
    """Set (or cancel, with 0) the UPS watchdog keep-alive hold — the
    operator's "keep alive N min" override for a bench transition longer than
    the watchdog's grace window. See scripts/ups_shutdown_watch.py."""

    minutes: float = Field(..., ge=0.0, le=720.0)


class DtcCodeEntry(BaseModel):
    """One described DTC — matches ``DtcDescription.to_dict()``."""

    model_config = ConfigDict(extra="forbid")

    code: str
    description: str
    system: str
    generic: bool
    curated: bool


class DtcReportResponse(BaseModel):
    """``GET /obd/dtc`` (Task 1.5) — the cached ``DtcReport`` plus staleness."""

    model_config = ConfigDict(extra="forbid")

    captured_at: str | None
    stored: list[DtcCodeEntry]
    pending: list[DtcCodeEntry]
    permanent: list[DtcCodeEntry]
    mil_on: bool | None
    dtc_count: int
    stale: bool


__all__ = [
    "CaptureStartRequest",
    "CaptureStopRequest",
    "CaptureTagRequest",
    "DtcCodeEntry",
    "DtcReportResponse",
    "FeedbackRequest",
    "JobSubmitRequest",
    "ModelUploadRequest",
    "ReplayStartRequest",
    "SessionMetadataRequest",
    "SessionPinRequest",
    "TuningRevertRequest",
    "TuningUpdateRequest",
    "UpsKeepaliveRequest",
]
