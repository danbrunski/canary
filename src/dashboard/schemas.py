"""Pydantic request models for dashboard control routes."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.collection.capture_profiles import MAX_TARGET_RATE_HZ, CaptureProfile
from src.vehicle.vehicle_ledger import TyreRecord


class _ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TrainingRunApprovalRequest(_ControlRequest):
    """``POST /control/training-runs/{id}/approve`` (6.36); the companion app
    registers the same route with this body."""

    approved: bool = True
    notes: str | None = None


class SequenceDetectorRequest(_ControlRequest):
    #: Selector value for the backbone to activate (registry: lstm | tcn).
    backbone: Literal["lstm", "tcn"]


class CaptureStartRequest(_ControlRequest):
    purpose: str | None = None
    protocol: str | None = None
    vehicle_state: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class CaptureAutostartArmRequest(_ControlRequest):
    """Arm a capture the daemon starts itself once the vehicle qualifies."""

    profile: str = Field(..., min_length=1)
    #: Engineering °C. ``None`` starts as soon as the engine runs; the
    #: default keeps warm-up on ``default``, where the cold-start models
    #: learn it.
    coolant_min_c: float | None = Field(default=80.0, ge=-40.0, le=130.0)
    rate_hz: float | None = Field(default=None, gt=0, le=MAX_TARGET_RATE_HZ)
    bus_budget_requests_per_second: float | None = Field(default=None, ge=2.0, le=200.0)
    tune_revision: str | None = Field(default=None, max_length=255)
    start_after_ticks: int = Field(default=2, ge=1, le=60)
    stop_after_seconds: float = Field(default=10.0, ge=0.0, le=600.0)


class CaptureTagRequest(_ControlRequest):
    label: str
    kind: str = "event"
    timestamp: float | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class CaptureStopRequest(_ControlRequest):
    reason: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class CapturePauseRequest(_ControlRequest):
    """Pause/resume the running capture without closing its session."""

    paused: bool


class CaptureProfileStartRequest(_ControlRequest):
    profile: str = Field(..., min_length=1)
    tune_revision: str | None = Field(default=None, max_length=255)
    #: Override the profile's own ``target_rate_hz`` for this capture only.
    #: The saved profile is untouched, so a one-off fast pull cannot silently
    #: become the profile's new normal.
    rate_hz: float | None = Field(default=None, gt=0, le=MAX_TARGET_RATE_HZ)
    #: Applied as part of the same capture transition, never by a follow-up
    #: request from the MCP client.  ``None`` preserves the profile's own
    #: override (or the existing operator override when it has none).
    bus_budget_requests_per_second: float | None = Field(default=None, ge=2.0, le=200.0)
    #: A bounded capture override is normally a backstop against a forgotten
    #: stop.  Explicit null intentionally keeps it until capture teardown.
    bus_budget_ttl_seconds: float | None = Field(default=None, gt=0.0, le=21600.0)


class CaptureProfileSaveRequest(_ControlRequest):
    profile: CaptureProfile
    previous_name: str | None = None


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
    #: Correct an existing entry by re-stating it under the same id: the
    #: correction replaces it on disk (``VehicleLedgerStore.replace``), so a
    #: typo or wrong reading is gone rather than superseded. Omit for a new
    #: event; ``DELETE /control/vehicle-ledger/{id}`` removes one outright.
    event_id: str | None = None
    #: A tyre fact (gauge tread depth, rotation, replacement, pressure) the
    #: tyre-wear trend reads; validated by ``TyreRecord``.
    tyres: TyreRecord | None = None


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


class CanActivityRequest(_ControlRequest):
    """Capture N seconds of raw CAN traffic (the ``can_activity`` tool).

    Per-arbitration-id frame rate plus which byte positions actually
    changed — the RE workhorse for "press the brake, see what moved" (pairs
    with AGENTS.md's "tag physical actions" convention). Pure listen: no
    ``bus.send``, no interaction with passive mode. ``ids`` restricts the
    capture to those arbitration ids; omit for the whole bus."""

    seconds: float = Field(..., gt=0.0, le=30.0)
    ids: list[int] | None = None


class TriggerPollRequest(_ControlRequest):
    """Force specific channels due on the OBD scheduler's next tick (the
    ``trigger_poll`` tool), instead of waiting out their tier cadence — the
    DTC/Mode 06 poll sits at a 5-minute interval by default. Channel names
    match ``GET /status``'s ``obd_scheduler.requests[].channel`` (``RPM``,
    ``DTC_STORED``, ``MONITOR_RESULTS``, ``DID_1101``, ...), matched
    case-insensitively; an unknown name is silently never matched. This only
    nudges the scheduler — read the result back with the usual tools
    (``get_status``, ``get_dtc``, ``query_telemetry``, ...) a few seconds
    later."""

    channels: list[str] = Field(..., min_length=1)


class ObdRequestRequest(_ControlRequest):
    """One-shot OBD/UDS request (the ``obd_request`` tool).

    ``service`` accepts any service byte — read, write and actuation alike;
    nothing filters it here or in ``CanObdClient.query()``. ``header``
    overrides the default functional request id (``0x7DF``) for a
    physically-addressed request (e.g. ``0x7E0`` for the engine ECM, or
    another module's tester address)."""

    service: int = Field(..., ge=0, le=0xFF)
    identifier: int | None = Field(default=None, ge=0, le=0xFFFF)
    header: int | None = Field(default=None, ge=0, le=0x7FF)
    timeout_seconds: float | None = Field(default=None, gt=0.0, le=10.0)


class ClearCodesRequest(_ControlRequest):
    """``POST /control/diagnostics/clear_codes``: clear one module's stored
    codes, by its name in the vehicle's K-line profile (``SRS``)."""

    module: str = Field(min_length=1)


class PassiveModeRequest(_ControlRequest):
    """Arm/disarm the CAN transmit gate (passive, receive-only mode).

    Used when another tool owns a diagnostic session with a module — an ECU
    reprogramming run being the motivating case — and CANary's OBD polling
    must not put frames on the bus. See src/collection/tx_gate.py.
    ``ttl_seconds`` (only meaningful when arming) auto-disarms after that
    many seconds, so a forgotten disarm doesn't silently stop OBD polling
    forever — omit for the old "stays armed until manually disarmed"
    behavior."""

    enabled: bool
    ttl_seconds: float | None = Field(default=None, gt=0.0, le=3600.0)


class BusBudgetRequest(_ControlRequest):
    """Override the OBD request-rate budget for a bounded capture.

    The scheduler's default is a conservative 8 req/s against a measured
    667 req/s capability, which is why WOT work has had to borrow UpRev's
    K-line logger. Raise it for a pull, drop it after. Send
    ``requests_per_second: null`` to clear. See src/collection/bus_budget.py
    for why the ceiling is 200 rather than the measured maximum, and why the
    TTL defaults to on here when passive mode's does not."""

    #: ``None`` clears the override and returns to the configured default.
    requests_per_second: float | None = Field(default=None, ge=2.0, le=200.0)
    #: Omitted → the module's 600 s default. Explicit ``null`` opts out of
    #: auto-revert entirely, which is deliberate-only.
    #: Capped at 6 h rather than 1: a capture's teardown reverts the budget
    #: itself, so the TTL is a backstop against a forgotten override, not the
    #: mechanism. The old 1 h ceiling rejected a request mid-arming on a drive
    #: that was going to outlast it. Explicit ``null`` still opts out entirely.
    ttl_seconds: float | None = Field(default=600.0, gt=0.0, le=21600.0)


class TuningModeRequest(_ControlRequest):
    """Arm/disarm the focused high-rate tuning poll profile.

    Narrows the Mode 01 rotation to a small tuning set and suspends the
    Mode-22 DID windows, so each channel gets a far larger slice of the same
    bus budget. See src/collection/tuning_profile.py."""

    enabled: bool
    channels: list[str] | None = None
    #: Poll every selected channel at exactly this rate. Without it the
    #: relative rate model applies, which caps each channel at ``poll_rate_hz``
    #: (1 Hz) no matter how much bus budget is free — so a capture could not
    #: previously be asked for a rate at all. Raise the bus budget to match:
    #: this sets demand, the budget still gates issuance.
    rate_hz: float | None = Field(default=None, gt=0, le=MAX_TARGET_RATE_HZ)


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
    """Matches ``TripSummary.to_dict()`` (``src/signals/derived.py``)."""

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
