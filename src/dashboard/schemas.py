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
    #: Task S.3 (llm-assist-and-compute-roadmap.md, case 5): the enriched
    #: label — a hypothesis string and/or one clarifying question/answer
    #: pair — attached from the review flow alongside the verdict.
    #: Operator-authored today; a future LLM producer would fill the same
    #: fields. See ``AnomalyFeedback`` for the question/answer pairing rule.
    hypothesis: str | None = None
    clarifying_question: str | None = None
    clarifying_answer: str | None = None


class VehicleEventRequest(_ControlRequest):
    """New vehicle maintenance/intervention ledger entry (Task 3.3)."""

    occurred_at: date
    kind: str
    title: str
    details: str = ""
    odometer_km: float | None = None
    affected_channels: list[str] = Field(default_factory=list)
    corpus_boundary: bool = False
    #: Correct an existing entry by re-stating it under the same id. The
    #: ledger is append-only and ``VehicleLedgerStore.records`` keeps the
    #: latest line per id, so a correction supersedes without losing the
    #: original — but nothing exposed that, and fixing a typo'd entry meant
    #: hand-appending JSON over ssh. Omit for a new event.
    event_id: str | None = None


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


class PassiveModeRequest(_ControlRequest):
    """Arm/disarm the CAN transmit gate (passive, receive-only mode).

    Used when another tool owns a diagnostic session with a module — an ECU
    reprogramming run being the motivating case — and CANary's OBD polling
    must not put frames on the bus. See src/collection/tx_gate.py."""

    enabled: bool


class TuningModeRequest(_ControlRequest):
    """Arm/disarm the focused high-rate tuning poll profile.

    Narrows the Mode 01 rotation to a small tuning set and suspends the
    Mode-22 DID windows, so each channel gets a far larger slice of the same
    bus budget. See src/collection/tuning_profile.py."""

    enabled: bool
    channels: list[str] | None = None


class CustomPidCreateRequest(_ControlRequest):
    """Define one new custom PID (Task 2.8c) — same fields as
    :class:`src.collection.custom_pids.CustomPid`; validated (read-only mode,
    safe formula) by that model at route-handling time."""

    name: str
    display_name: str
    mode: int = Field(ge=0, le=0xFF)
    pid: int = Field(ge=0, le=0xFFFF)
    formula: str
    unit: str = ""
    category: str = "custom"
    minimum: float | None = None
    maximum: float | None = None
    poll_tier: Literal["fast", "medium", "slow", "on_demand"] = "medium"
    dashboard_hint: Literal["gauge", "graph", "text", "indicator", "list"] = "graph"
    header: int | None = Field(default=None, ge=0, le=0x1FFFFFFF)


class CustomPidCsvImportRequest(_ControlRequest):
    """A Torque Pro custom-PID CSV export, pasted or uploaded as text."""

    csv_text: str = Field(..., min_length=1)


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
    #: Mode 02 freeze-frame reading (Task 3.3) — channel name -> decoded
    #: value, or ``None`` when no stored DTC has triggered a read yet.
    freeze_frame: dict[str, Any] | None = None
    stale: bool


class TripSummaryFields(BaseModel):
    """Matches ``TripSummary.to_dict()`` (``src/analysis/derived.py``)."""

    model_config = ConfigDict(extra="forbid")

    duration_s: float
    distance_km: float | None
    fuel_used_l: float | None
    avg_economy_l_per_100km: float | None
    avg_economy_mpg: float | None
    co2_kg: float | None
    idle_time_s: float | None
    idle_fraction: float | None
    max_speed_kph: float | None
    avg_speed_kph: float | None
    zero_to_60_mph_s: float | None
    quarter_mile_s: float | None
    quarter_mile_trap_kph: float | None


class TripSummaryResponse(BaseModel):
    """``GET /sessions/{id}/trip-summary`` (Task 1.5) — the ``trip_summary.json``
    sidecar (Task 1.4), computed on demand from the stored session."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    computed_at: str
    session_id: str
    summary: TripSummaryFields


__all__ = [
    "CaptureStartRequest",
    "CaptureStopRequest",
    "CaptureTagRequest",
    "CustomPidCreateRequest",
    "CustomPidCsvImportRequest",
    "DtcCodeEntry",
    "DtcReportResponse",
    "FeedbackRequest",
    "JobSubmitRequest",
    "ModelUploadRequest",
    "ReplayStartRequest",
    "SessionMetadataRequest",
    "SessionPinRequest",
    "TripSummaryFields",
    "TripSummaryResponse",
    "TuningModeRequest",
    "TuningRevertRequest",
    "TuningUpdateRequest",
    "UpsKeepaliveRequest",
]
