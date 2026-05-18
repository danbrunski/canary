"""Read-only REST surface for the dashboard (Task 5.3).

Four endpoints, all read-only, all backed by files the daemon already
writes — no live producer wiring yet (the WebSocket layer in Task 5.4
will do that). The goal here is to unblock the frontend so the dashboard
can be built against a real backend without depending on Phase 5's full
inference loop landing first.

* ``GET /status`` — service health: storage path, free disk, the most
  recent session directory observed under ``collection.storage_path``,
  loaded model versions (best-effort: reads JSON model manifests if a
  models directory is configured), and the loaded config's vehicle line.
* ``GET /config`` — read-only echo of the loaded ``canary.yaml`` with
  internal-only paths surfaced. Useful for "what is this Pi actually
  running" diagnostics.
* ``GET /anomalies?since=...`` — reads the ``AnomalyEventLogger``'s
  ``anomalies-YYYY-MM-DD.jsonl`` files under a configured anomalies
  directory; filters by ``since`` ISO-8601 timestamp.
* ``GET /telemetry/latest?n=...`` — the most recent ``n`` rows from the
  newest Parquet chunk in the newest session, as a list of
  ``{timestamp, source, channel, value, unit}`` dicts. Pure file read;
  no daemon coupling.

The factory ``create_app(config, *, anomalies_dir, models_dir)`` keeps the
app stateless — every request re-reads from disk (cheap for v1; if the
anomalies file ever grows large enough to matter, the dashboard can ask
with a ``since=`` filter to bound the work).

Bind-host policy (Task 5.5) is enforced at the entry point that runs
``uvicorn.run`` — not here, since the app itself doesn't know where it's
mounted.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import read_events_dir
from src.alerts.feedback import (
    FEEDBACK_LABELS,
    FeedbackStore,
    annotate_events_with_feedback,
)
from src.alerts.tuning import (
    AlertTuner,
    TuningConfig,
    estimate_before_after,
    load_tuning_version,
    save_tuning_config,
)
from src.config import CanaryConfig
from src.control.audit import ControlAuditLogger
from src.control.capture import CaptureConflictError, CaptureController, NoActiveCaptureError
from src.control.jobs import JobController
from src.control.models import publish_model_artifact
from src.control.preflight import build_preflight
from src.control.sessions import (
    delete_session,
    list_sessions,
    pin_session,
    resolve_session,
    summarize_session,
    unpin_session,
    write_session_metadata,
    write_session_zip,
)
from src.dashboard.broadcast import Broadcaster
from src.decode.behavior_models import (
    DEFAULT_BEHAVIOR_MODELS_PATH,
    load_behavior_models,
)
from src.decode.bindings import load_bindings
from src.models.health import HealthMonitor
from src.models.training_ledger import TrainingLedger
from src.preprocessing.loader import (
    PARQUET_PATTERN,
    discover_sessions,
)

#: Where the React SPA's built artifacts land — relative to repo root in dev,
#: but the daemon resolves it via ``Path(__file__).parent / "static" / "dist"``
#: which works regardless of cwd.
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static" / "dist"

#: Repo root, anchored to this file (``src/dashboard/api.py`` → up 3) rather
#: than the process cwd. These catalog files are version-controlled and ship
#: in the clone, so the daemon must find them regardless of where it was
#: launched from — under systemd the unit happens to ``cd`` to the clone, but
#: a Docker ``WORKDIR`` or a direct ``serve_dashboard`` run would otherwise
#: silently empty *all three* catalogs. ``DEFAULT_STATIC_DIR`` already anchors
#: to ``__file__`` for the same reason; these now match.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where the project hand-edits CAN signal bindings live. The dashboard reads
#: a *display-oriented* projection so the frontend can render decoded channels
#: in engineering units. The full evidence trail / notes / confidence ladder
#: stays in the YAML and is irrelevant to the live view.
DEFAULT_BINDINGS_PATH = _REPO_ROOT / "data" / "bindings.yaml"

#: Canonical Nissan Mode-22 DID catalog (Task 6.16 workbench reads this).
DEFAULT_DIDS_PATH = _REPO_ROOT / "data" / "dids.yaml"

#: Scanned directory of YAML test-session profiles for the Test panel. A
#: user-editable catalog (drop a file, it appears) — not hardcoded in code.
DEFAULT_TEST_PROFILES_DIR = _REPO_ROOT / "data" / "test_profiles"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status helpers — pulled out so they're individually testable
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusSnapshot:
    """What ``GET /status`` returns. Stable shape independent of FastAPI."""

    storage_path: str
    free_mb: float
    latest_session: str | None
    latest_session_path: str | None
    model_versions: dict[str, str | None]
    vehicle: str
    #: Inference-health block (Task 6.27) — latency, freshness, missing-feature
    #: rate, model-swap outcomes, host load, drift — or ``None`` when the
    #: daemon hasn't wired a monitor in (tests / bare app).
    health: dict[str, Any] | None = None
    #: Emitter counters at ``/status`` request time — ``{"emitted": N,
    #: "suppressed_not_anomaly": N, "suppressed_dedupe": N}`` or ``None`` when
    #: the daemon hasn't wired an emitter in. The dashboard surfaces this as
    #: "is the anomaly path firing?" — emitted > 0 within a recent window is
    #: a strong "yes" signal; persistent zero with a recent session is the
    #: cue that something is wrong upstream.
    emitter_counters: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "storage_path": self.storage_path,
            "free_mb": round(self.free_mb, 1),
            "latest_session": self.latest_session,
            "latest_session_path": self.latest_session_path,
            "model_versions": self.model_versions,
            "vehicle": self.vehicle,
            "emitter_counters": self.emitter_counters,
            "health": self.health,
        }


def _free_mb(path: Path) -> float:
    """Free disk MB at ``path`` (or its first existing parent). 0 on failure."""
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return 0.0
    return usage.free / (1024 * 1024)


def _model_version(manifest_path: Path) -> str | None:
    """Read the version field from a model manifest JSON, or ``None``.

    Reads conservatively — the manifest format isn't strictly enforced
    here so that an in-development manifest doesn't break ``/status``.
    """
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    version = (
        data.get("model_version")
        or data.get("version")
        or data.get("training", {}).get("model_version")
    )
    return str(version) if version is not None else None


def _resolve_latest_session(storage_path: Path) -> Path | None:
    sessions = discover_sessions(storage_path)
    return sessions[-1] if sessions else None


def build_status(
    config: CanaryConfig,
    *,
    models_dir: Path | None = None,
    emitter: AnomalyEventEmitter | None = None,
    health_monitor: HealthMonitor | None = None,
) -> StatusSnapshot:
    """Assemble the status snapshot used by ``GET /status``."""
    storage = config.collection.storage_path
    latest = _resolve_latest_session(storage)
    model_versions: dict[str, str | None] = {}
    if models_dir is not None:
        # Artifacts live one dir down: <models_dir>/<name>/model.manifest.json
        # (matches daemon.isolation_forest_artifact / lstm_artifact). Recurse so
        # the nested layout is found; fall back to the flat
        # <models_dir>/<name>.manifest.json convention when the manifest sits
        # directly in models_dir.
        for manifest in sorted(models_dir.rglob("*.manifest.json")):
            name = (
                manifest.stem.removesuffix(".manifest")
                if manifest.parent == models_dir
                else manifest.parent.name
            )
            model_versions[name] = _model_version(manifest)
    return StatusSnapshot(
        storage_path=str(storage),
        free_mb=_free_mb(storage),
        latest_session=latest.name if latest else None,
        latest_session_path=str(latest) if latest else None,
        model_versions=model_versions,
        vehicle=f"{config.vehicle.year} {config.vehicle.make} {config.vehicle.model}".strip(),
        emitter_counters=emitter.counters.to_dict() if emitter is not None else None,
        health=health_monitor.snapshot() if health_monitor is not None else None,
    )


# ---------------------------------------------------------------------------
# Telemetry latest — newest Parquet chunk, last N rows
# ---------------------------------------------------------------------------


def read_latest_telemetry_rows(
    storage_path: Path,
    *,
    n: int = 100,
) -> list[dict[str, Any]]:
    """Return the last ``n`` rows of the newest session, spanning Parquet
    chunks as needed.

    Reads chunks newest-first and stops as soon as ``n`` rows are in hand,
    so latency stays bounded (``n`` is route-capped at 10_000) while the
    window can still be wide enough to surface a low-rate signal. The old
    single-newest-chunk read could not: at full bus rate a chunk spans a
    fraction of a second, so a 1 Hz OBD/decoded row would never appear in
    "latest N" no matter how large N was. Returns ``[]`` when there's no
    session/chunk on disk.
    """
    latest = _resolve_latest_session(storage_path)
    if latest is None:
        return []
    chunks = sorted(latest.glob(PARQUET_PATTERN))
    if not chunks:
        return []
    collected: list[pd.DataFrame] = []
    total = 0
    for chunk in reversed(chunks):  # newest-first; stop once we have >= n
        if chunk.stat().st_size == 0:
            continue
        df = pd.read_parquet(chunk)
        collected.append(df)
        total += len(df)
        if total >= n:
            break
    table = (
        collected[0]
        if len(collected) == 1
        else pd.concat(collected[::-1], ignore_index=True)  # back to chronological
    )
    if len(table) > n:
        table = table.tail(n)
    rows: list[dict[str, Any]] = []
    for record in table.to_dict(orient="records"):
        rows.append(_normalize_telemetry_row({str(k): v for k, v in record.items()}))
    return rows


_PER_CHANNEL_COLUMNS = [
    "timestamp",
    "source",
    "channel",
    "value_number",
    "value_text",
    "value_bool",
    "unit",
]


def read_latest_per_channel(
    storage_path: Path,
    *,
    window_seconds: float = 90.0,
    max_chunks: int = 300,
) -> list[dict[str, Any]]:
    """Return the most recent row for *every distinct channel* in the newest
    session.

    ``read_latest_telemetry_rows`` returns the last N rows globally, which the
    high-rate CAN bus dominates ~60:1 over the ~1 Hz OBD poll — a 120-row seed
    carries only 2–3 OBD channels, so the dashboard's gauges come up nearly
    empty until the WebSocket slowly backfills. This instead scans chunks
    newest-first until the accumulated span covers ``window_seconds`` (one full
    OBD poll cycle, with margin) and keeps the latest sample per
    ``(source, channel)`` — so every gauge renders immediately on load.

    Only the value columns are read (the binary ``payload`` is skipped), which
    keeps a ~90 s / ~150 k-row scan well under half a second. Returns ``[]``
    when there's no session/chunk on disk.
    """
    latest = _resolve_latest_session(storage_path)
    if latest is None:
        return []
    chunks = [c for c in sorted(latest.glob(PARQUET_PATTERN)) if c.stat().st_size > 0]
    if not chunks:
        return []
    collected: list[pd.DataFrame] = []
    for chunk in reversed(chunks[-max_chunks:]):  # newest-first
        collected.append(pd.read_parquet(chunk, columns=_PER_CHANNEL_COLUMNS))
        spanned = pd.concat(collected) if len(collected) > 1 else collected[0]
        ts = pd.to_datetime(spanned["timestamp"], utc=True)
        if (ts.max() - ts.min()).total_seconds() >= window_seconds:
            break
    table = pd.concat(collected[::-1], ignore_index=True)
    table["timestamp"] = pd.to_datetime(table["timestamp"], utc=True)
    table = table.sort_values("timestamp", kind="stable")
    latest_rows = table.groupby(["source", "channel"], as_index=False).last()
    rows: list[dict[str, Any]] = []
    for record in latest_rows.to_dict(orient="records"):
        rows.append(_normalize_telemetry_row({str(k): v for k, v in record.items()}))
    return rows


def read_obd_coverage(storage_path: Path) -> dict[str, Any]:
    """OBD capability/coverage (Task 6.3) derived from observed telemetry.

    The "supported" set is inferred honestly from which ``obd:`` channels
    actually appeared in the newest session — no live ECU query, so it works
    off-vehicle and without owning the diagnostic bus. Empty report when no
    session/chunk exists yet.
    """
    from src.analysis.obd_coverage import build_coverage_report, channels_to_pids

    latest = _resolve_latest_session(storage_path)
    observed: set[str] = set()
    if latest is not None:
        for chunk in sorted(latest.glob(PARQUET_PATTERN)):
            try:
                df = pd.read_parquet(chunk, columns=["source", "channel"])
            except (OSError, ValueError, KeyError):
                continue
            obd_rows = df[df["source"] == "obd"]
            observed.update(str(c) for c in obd_rows["channel"].unique())
    supported = channels_to_pids(observed)
    return build_coverage_report(supported).to_dict()


def _normalize_telemetry_row(record: dict[str, Any]) -> dict[str, Any]:
    """Collapse the (value_number, value_text, value_bool) trio into one ``value``
    so the dashboard gets a stable shape regardless of the underlying type."""
    value = record.get("value_number")
    if value is None or pd.isna(value):
        value = record.get("value_text")
    if value is None or (isinstance(value, float) and pd.isna(value)):
        value = record.get("value_bool")
    return {
        "timestamp": _iso(record.get("timestamp")),
        "source": record.get("source"),
        "channel": record.get("channel"),
        "value": None if (isinstance(value, float) and pd.isna(value)) else value,
        "unit": record.get("unit"),
    }


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[no-any-return]
    return str(value)


# ---------------------------------------------------------------------------
# Anomalies — filter the daily JSONL files by `since`
# ---------------------------------------------------------------------------


def read_anomalies_since(
    anomalies_dir: Path,
    *,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Read anomalies from ``anomalies_dir`` with optional ``since`` filter.

    ``since`` is parsed as an ISO-8601 string compared lexicographically against
    each event's ``timestamp`` — which is also persisted as ISO-8601, so the
    comparison is correct without a datetime parse. Invalid ``since`` strings
    raise ``ValueError`` so the FastAPI handler can turn them into 400s.
    """
    if not anomalies_dir.exists():
        return []
    events = read_events_dir(anomalies_dir)
    if since is not None:
        # Validate by parse-roundtrip; we don't need the result, just to fail
        # loudly on garbage.
        pd.Timestamp(since)
        events = [e for e in events if e.timestamp >= since]
    return [e.to_dict() for e in events]


# ---------------------------------------------------------------------------
# Config echo — convert the Pydantic model to JSON-safe dict
# ---------------------------------------------------------------------------


def echo_config(config: CanaryConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Behavior models — Task 4.E.9 dashboard feed
# ---------------------------------------------------------------------------


def read_bindings_for_display(
    path: Path = DEFAULT_BINDINGS_PATH,
    *,
    confidence_levels: tuple[str, ...] = ("confirmed", "high", "medium"),
) -> dict[str, Any]:
    """Surface the display-relevant subset of ``bindings.yaml`` for the frontend.

    The dashboard renders a ``decoded:<NAME>`` column's raw integer value as
    ``raw * scale + offset`` with the binding's unit; values outside
    ``plausible_min/max`` get color-flagged. Returns one entry per signal
    keyed by ``decoded:<NAME>`` so the frontend lookup is a flat
    ``bindings[channel_key]``.

    Low-confidence signals (``low`` / ``hypothesized``) are excluded by
    default — surfacing them in the UI risks treating a guess as a reading.
    Adjust ``confidence_levels`` if a parked-mode debug view ever wants
    them all.
    """
    if not path.exists():
        return {}
    bindings = load_bindings(path)
    out: dict[str, dict[str, Any]] = {}
    for signal in bindings.signals:
        if signal.confidence not in confidence_levels:
            continue
        out[f"decoded:{signal.name}"] = {
            "name": signal.name,
            "scale": signal.scale,
            "offset": signal.offset,
            "unit": signal.unit,
            "confidence": signal.confidence,
            "plausible_min": signal.plausible_min,
            "plausible_max": signal.plausible_max,
        }
    return out


def read_behavior_models(path: Path = DEFAULT_BEHAVIOR_MODELS_PATH) -> dict[str, Any]:
    """Return ``behavior_models.yaml`` as a JSON-safe payload for the dashboard.

    The dashboard's performance panel renders 1D fits (coolant warmup, idle
    RPM curve) directly and 2D fits (timing surface, fuel-trim surface) as
    heatmaps with per-cell counts surfacing operating-envelope coverage.

    Missing file → ``{"models": [], "schema_version": null}``; the dashboard
    handles that as "no surfaces refit yet" rather than an error.
    """
    if not path.exists():
        return {"schema_version": None, "models": []}
    return load_behavior_models(path).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Control-plane request models
# ---------------------------------------------------------------------------


class _ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
    """Operator verdict on one anomaly event (Task 6.24)."""

    event: dict[str, Any]
    label: str
    reason: str | None = None
    driving_context: dict[str, Any] = Field(default_factory=dict)
    labeled_by: str | None = None


class TuningUpdateRequest(_ControlRequest):
    """Replace the tuning config (Task 6.25). ``version`` is auto-bumped."""

    config: dict[str, Any]
    change: str = "tuning update"
    by: str | None = None


class TuningRevertRequest(_ControlRequest):
    version: int
    by: str | None = None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    config: CanaryConfig,
    *,
    anomalies_dir: Path,
    models_dir: Path | None = None,
    telemetry_broadcaster: Broadcaster | None = None,
    anomalies_broadcaster: Broadcaster | None = None,
    emitter: AnomalyEventEmitter | None = None,
    behavior_models_path: Path = DEFAULT_BEHAVIOR_MODELS_PATH,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    dids_path: Path = DEFAULT_DIDS_PATH,
    test_profiles_dir: Path = DEFAULT_TEST_PROFILES_DIR,
    static_dir: Path | None = None,
    enable_dev_cors: bool = False,
    capture_controller: CaptureController | None = None,
    job_controller: JobController | None = None,
    control_broadcaster: Broadcaster | None = None,
    audit_logger: ControlAuditLogger | None = None,
    control_token: str | None = None,
    feedback_store: FeedbackStore | None = None,
    alert_tuner: AlertTuner | None = None,
    health_monitor: HealthMonitor | None = None,
) -> FastAPI:
    """Build a stateless FastAPI app over the configured paths.

    Routes delegate to pure helpers so the same logic is unit-testable
    without spinning up the web stack. The broadcasters power the
    WebSocket streams (5.4); pass ``None`` and those endpoints will
    accept connections but never emit (useful in tests or when the
    daemon isn't producing yet).
    """
    app = FastAPI(title="CANary", version=_canary_version())
    if enable_dev_cors:
        # `pnpm dev` serves the SPA on :5173 and proxies the backend.
        # Add CORS so a developer can also hit the backend directly from
        # the browser without going through the Vite proxy. Production
        # serves the SPA from the same origin and doesn't need CORS.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    telemetry_bus = telemetry_broadcaster or Broadcaster()
    anomalies_bus = anomalies_broadcaster or Broadcaster()
    control_bus = control_broadcaster or Broadcaster()
    resolved_control_token = (
        os.environ.get(config.daemon.control_token_env) if control_token is None else control_token
    )
    # Stash them on app.state so producer threads / app extensions can reach
    # the same instances after construction.
    app.state.telemetry_broadcaster = telemetry_bus
    app.state.anomalies_broadcaster = anomalies_bus
    app.state.control_broadcaster = control_bus
    app.state.control_loop = None

    def publish_control_event(action: str, result: str, payload: dict[str, Any]) -> None:
        event = (
            audit_logger.record(action=action, result=result, payload=payload)
            if audit_logger is not None
            else {
                "action": action,
                "result": result,
                "payload": payload,
            }
        )
        _publish_on_app_loop(control_bus, event, getattr(app.state, "control_loop", None))

    @app.get("/status")
    def get_status() -> dict[str, Any]:
        return build_status(
            config,
            models_dir=models_dir,
            emitter=emitter,
            health_monitor=health_monitor,
        ).to_dict()

    @app.get("/config")
    def get_config() -> dict[str, Any]:
        return echo_config(config)

    @app.get("/anomalies")
    def get_anomalies(since: str | None = Query(default=None)) -> dict[str, Any]:
        try:
            events = read_anomalies_since(anomalies_dir, since=since)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"bad `since`: {exc}") from exc
        # Join the operator's verdict onto each event so the timeline can
        # show "you marked this" without a second round-trip (Task 6.24).
        if feedback_store is not None:
            events = annotate_events_with_feedback(events, feedback_store)
        return {"events": events, "count": len(events)}

    @app.get("/telemetry/latest")
    def get_telemetry_latest(
        n: int = Query(default=100, ge=1, le=10_000),
        per_channel: bool = Query(default=False),
    ) -> dict[str, Any]:
        rows = (
            read_latest_per_channel(config.collection.storage_path)
            if per_channel
            else read_latest_telemetry_rows(config.collection.storage_path, n=n)
        )
        return {"rows": rows, "count": len(rows)}

    @app.get("/models/behavior")
    def get_behavior_models() -> dict[str, Any]:
        # Re-read on every request so a `scripts/refit_behavior_models.py`
        # run lands in the dashboard without a daemon restart. Cheap — the
        # file is < 100 KB even with a full surface catalog.
        return read_behavior_models(behavior_models_path)

    @app.get("/bindings")
    def get_bindings() -> dict[str, Any]:
        # Re-read per request so a hand-edit to bindings.yaml (during RE)
        # surfaces in the dashboard immediately. The file is small.
        return read_bindings_for_display(bindings_path)

    @app.get("/dids")
    def get_dids() -> dict[str, Any]:
        # Read-only Nissan Mode-22 DID catalog for the 6.16 workbench.
        # Re-read per request like /bindings so a hand-edit during RE shows
        # up immediately; empty doc when the file is absent.
        from src.decode.dids import load_did_map

        if not dids_path.exists():
            return {"schema_version": None, "vehicle": {}, "access": None, "dids": []}
        return load_did_map(dids_path).model_dump(mode="json")

    @app.get("/test-profiles")
    def get_test_profiles() -> dict[str, Any]:
        # Read-only catalog for the Test panel, scanned per request so a
        # dropped-in / hand-edited profile YAML appears without a daemon
        # restart. Unauthenticated like /dids + /bindings (no secrets;
        # listing profiles must work before the control token is entered).
        from src.control.test_profiles import load_test_profiles

        return load_test_profiles(test_profiles_dir).model_dump(mode="json")

    @app.get("/obd/coverage")
    def get_obd_coverage() -> dict[str, Any]:
        # Read-only honesty surface (Task 6.3 / Risk R7): which public PIDs
        # the ECU supports, which CANary decodes, which are undecoded gaps.
        return read_obd_coverage(config.collection.storage_path)

    def require_control_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        _require_control_auth(
            resolved_control_token,
            authorization,
            client_is_loopback=_client_is_loopback(request),
        )

    def _controller() -> CaptureController:
        if capture_controller is None:
            raise HTTPException(status_code=503, detail="control API is not available")
        return capture_controller

    def _jobs() -> JobController:
        if job_controller is None:
            raise HTTPException(status_code=503, detail="job API is not available")
        return job_controller

    @app.post("/control/captures", status_code=201)
    def post_capture(
        request: CaptureStartRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        controller = _controller()
        try:
            capture = controller.start_capture(
                purpose=request.purpose,
                protocol=request.protocol,
                vehicle_state=request.vehicle_state,
                fields=request.fields,
            )
        except CaptureConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        publish_control_event("capture.start", "ok", capture.to_dict())
        return {"capture": capture.to_dict()}

    @app.get("/control/captures/current")
    def get_current_capture(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        controller = _controller()
        capture = controller.current_capture()
        return {"capture": None if capture is None else capture.to_dict()}

    @app.post("/control/captures/current/tags")
    def post_capture_tag(
        request: CaptureTagRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        controller = _controller()
        try:
            tag = controller.add_tag(
                request.label,
                kind=request.kind,
                timestamp=request.timestamp,
                fields=request.fields,
            )
        except NoActiveCaptureError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        capture = controller.current_capture()
        payload = {
            "tag": tag,
            "capture": None if capture is None else capture.to_dict(),
        }
        publish_control_event("capture.tag", "ok", payload)
        return payload

    @app.post("/control/captures/current/stop")
    def post_capture_stop(
        request: CaptureStopRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        controller = _controller()
        try:
            capture = controller.stop_capture(reason=request.reason, fields=request.fields)
        except NoActiveCaptureError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        publish_control_event("capture.stop", "ok", capture.to_dict())
        return {"capture": capture.to_dict()}

    @app.get("/control/sessions")
    def get_control_sessions(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        sessions = [session.to_dict() for session in list_sessions(config.collection.storage_path)]
        return {"sessions": sessions, "count": len(sessions)}

    @app.get("/control/sessions/{session_id}")
    def get_control_session(
        session_id: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"session": summarize_session(session).to_dict()}

    @app.post("/control/sessions/{session_id}/metadata")
    def post_control_session_metadata(
        session_id: str,
        request: SessionMetadataRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        metadata = write_session_metadata(session, request.metadata, merge=request.merge)
        payload = {"session_id": session_id, "metadata": metadata}
        publish_control_event("session.metadata", "ok", payload)
        return payload

    @app.post("/control/sessions/{session_id}/pin")
    def post_control_session_pin(
        session_id: str,
        request: SessionPinRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        metadata = pin_session(session, reason=request.reason)
        if request.metadata:
            metadata = write_session_metadata(session, request.metadata)
        payload = {"session": summarize_session(session).to_dict(), "metadata": metadata}
        publish_control_event("session.pin", "ok", payload)
        return payload

    @app.post("/control/sessions/{session_id}/unpin")
    def post_control_session_unpin(
        session_id: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        metadata = unpin_session(session)
        payload = {"session": summarize_session(session).to_dict(), "metadata": metadata}
        publish_control_event("session.unpin", "ok", payload)
        return payload

    @app.delete("/control/sessions/{session_id}")
    def delete_control_session(
        session_id: str,
        force: bool = Query(default=False),
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        active = capture_controller.current_capture() if capture_controller is not None else None
        if active is not None and active.session_id == session_id:
            raise HTTPException(status_code=409, detail="cannot delete the active capture session")
        try:
            delete_session(session, force=force)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        payload = {"session_id": session_id, "deleted": True, "force": force}
        publish_control_event("session.delete", "ok", payload)
        return payload

    @app.get("/control/sessions/{session_id}/export")
    def export_control_session(
        session_id: str,
        background_tasks: BackgroundTasks,
        _auth: None = Depends(require_control_auth),
    ) -> FileResponse:
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        tmp = tempfile.NamedTemporaryFile(
            prefix=f"canary-{session_id}-",
            suffix=".zip",
            delete=False,
        )
        tmp_path = Path(tmp.name)
        tmp.close()
        write_session_zip(session, tmp_path)
        background_tasks.add_task(_unlink_path, tmp_path)
        return FileResponse(
            tmp_path,
            media_type="application/zip",
            filename=f"{session_id}.zip",
        )

    @app.post("/control/jobs", status_code=201)
    def post_control_job(
        request: JobSubmitRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            job = _jobs().submit(request.kind, request.params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        publish_control_event("job.submit", "ok", job.to_dict())
        return {"job": job.to_dict()}

    @app.get("/control/jobs")
    def get_control_jobs(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        jobs = [job.to_dict() for job in _jobs().list_jobs()]
        return {"jobs": jobs, "count": len(jobs)}

    @app.get("/control/jobs/{job_id}/logs")
    def get_control_job_logs(
        job_id: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        logs = _jobs().logs(job_id)
        if logs is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        return {"job_id": job_id, "log": logs}

    @app.get("/control/jobs/{job_id}")
    def get_control_job(
        job_id: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        job = _jobs().get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        return {"job": job.to_dict()}

    @app.post("/control/jobs/{job_id}/cancel")
    def post_control_job_cancel(
        job_id: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        job = _jobs().cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        publish_control_event("job.cancel", "ok", job.to_dict())
        return {"job": job.to_dict()}

    @app.get("/control/preflight")
    def get_control_preflight(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        current_session = (
            capture_controller.active.session_path
            if capture_controller is not None and capture_controller.active is not None
            else _resolve_latest_session(config.collection.storage_path)
        )
        return build_preflight(
            config,
            control_token_configured=bool(resolved_control_token),
            current_session=current_session,
        )

    @app.get("/control/version")
    def get_control_version(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        return {
            "api_version": 1,
            "package_version": _canary_version(),
            "git_sha": _git_sha(),
            "config_token_env": config.daemon.control_token_env,
        }

    @app.post("/control/models/{model_type}/artifact")
    def post_control_model_artifact(
        model_type: str,
        request: ModelUploadRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            published = publish_model_artifact(
                config,
                model_type=model_type,
                artifact_b64=request.artifact_b64,
                manifest=request.manifest,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        publish_control_event("model.upload", "ok", published)
        return {"model": published}

    def _feedback() -> FeedbackStore:
        if feedback_store is None:
            raise HTTPException(status_code=503, detail="feedback API is not available")
        return feedback_store

    @app.get("/control/feedback")
    def get_feedback(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        store = _feedback()
        records = [fb.to_dict() for fb in store.labels().values()]
        return {"feedback": records, "count": len(records), "summary": store.summary()}

    @app.get("/control/training-runs")
    def get_training_runs(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        """Training-run history for the 6.22 dashboard (Task 6.21 ledger).

        Latest record per run (the append-only chain is collapsed); read
        per request so a fresh ``retrain.py`` run shows up without a daemon
        restart. Empty list when nothing's been recorded yet."""
        ledger = TrainingLedger(config.daemon.training_ledger)
        runs = [r.to_dict() for r in ledger.records()]
        return {"runs": runs, "count": len(runs)}

    @app.post("/control/feedback", status_code=201)
    def post_feedback(
        request: FeedbackRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        store = _feedback()
        if request.label not in FEEDBACK_LABELS:
            raise HTTPException(
                status_code=422,
                detail=f"label must be one of {sorted(FEEDBACK_LABELS)}",
            )
        fb = store.record(
            event=request.event,
            label=request.label,  # type: ignore[arg-type]  # checked above
            reason=request.reason,
            driving_context=request.driving_context,
            labeled_by=request.labeled_by,
        )
        publish_control_event("anomaly.feedback", "ok", fb.to_dict())
        return {"feedback": fb.to_dict()}

    def _tuner() -> AlertTuner:
        if alert_tuner is None:
            raise HTTPException(status_code=503, detail="tuning API is not available")
        return alert_tuner

    def _ensure_baseline_audited(tuner: AlertTuner) -> None:
        """Snapshot the pre-change config so the first change is revertible.

        The in-memory default (version 1) is never written until the operator
        makes a change; without this, you couldn't undo your *first* tuning
        edit. Idempotent — only writes if that version isn't already in the
        audit log.
        """
        current = tuner.config
        if load_tuning_version(current.version, config.daemon.tuning_audit) is None:
            save_tuning_config(
                current,
                config.daemon.tuning_config,
                audit_path=config.daemon.tuning_audit,
                change="baseline snapshot",
                by="system",
            )

    @app.get("/control/datasets/health")
    def get_dataset_health(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        """Corpus readiness + per-session coverage (Task 6.23)."""
        from src.datasets.health import analyze_corpus

        storage = config.collection.storage_path
        sessions = discover_sessions(storage) if storage.exists() else []
        return analyze_corpus(sessions, config).to_dict()

    @app.get("/control/tuning")
    def get_tuning(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        return {"tuning": _tuner().config.to_dict()}

    @app.post("/control/tuning")
    def post_tuning(
        request: TuningUpdateRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        tuner = _tuner()
        try:
            proposed = TuningConfig.model_validate(request.config)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid tuning config: {exc}") from exc
        _ensure_baseline_audited(tuner)
        # Monotonic version bump regardless of what the client sent — the
        # version is the audit handle, not client-controlled.
        proposed = proposed.model_copy(update={"version": tuner.config.version + 1})
        save_tuning_config(
            proposed,
            config.daemon.tuning_config,
            audit_path=config.daemon.tuning_audit,
            change=request.change,
            by=request.by,
        )
        # Mutate the live tuner in place so the change takes effect on the
        # very next inference tick — no daemon restart.
        tuner.config = proposed
        publish_control_event(
            "tuning.update", "ok", {"version": proposed.version, "change": request.change}
        )
        return {"tuning": proposed.to_dict()}

    @app.post("/control/tuning/estimate")
    def post_tuning_estimate(
        request: TuningUpdateRequest,
        since: str | None = Query(default=None),
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        """Before/after FP estimate of a proposed config over recent
        anomalies — run *before* committing (Risk R11)."""
        tuner = _tuner()
        try:
            proposed = TuningConfig.model_validate(request.config)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid tuning config: {exc}") from exc
        try:
            events = read_anomalies_since(anomalies_dir, since=since)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"bad `since`: {exc}") from exc
        return estimate_before_after(events, tuner.config, proposed)

    @app.post("/control/tuning/revert")
    def post_tuning_revert(
        request: TuningRevertRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        tuner = _tuner()
        snapshot = load_tuning_version(request.version, config.daemon.tuning_audit)
        if snapshot is None:
            raise HTTPException(
                status_code=404, detail=f"no tuning version {request.version} in audit log"
            )
        # Reverting is itself a new, audited version (never rewinds the log).
        reverted = snapshot.model_copy(update={"version": tuner.config.version + 1})
        save_tuning_config(
            reverted,
            config.daemon.tuning_config,
            audit_path=config.daemon.tuning_audit,
            change=f"revert to v{request.version}",
            by=request.by,
        )
        tuner.config = reverted
        publish_control_event(
            "tuning.revert", "ok", {"to_version": request.version, "new_version": reverted.version}
        )
        return {"tuning": reverted.to_dict()}

    @app.websocket("/ws/telemetry")
    async def ws_telemetry(websocket: WebSocket) -> None:
        await _run_websocket(websocket, telemetry_bus)

    @app.websocket("/ws/anomalies")
    async def ws_anomalies(websocket: WebSocket) -> None:
        await _run_websocket(websocket, anomalies_bus)

    @app.websocket("/ws/control")
    async def ws_control(websocket: WebSocket) -> None:
        token = websocket.query_params.get("token")
        if not resolved_control_token or token != resolved_control_token:
            await websocket.close(code=1008)
            return
        app.state.control_loop = asyncio.get_running_loop()
        await _run_websocket(websocket, control_bus)

    # Serve the built SPA from /, with SPA fall-through on 404 (the React
    # router could later own client-side paths like /diagnostics).
    # The mount is added last so the API routes above always win when their
    # paths overlap. ``html=True`` makes ``index.html`` the fallback for
    # any unmatched path, exactly what an SPA wants.
    chosen_static = static_dir if static_dir is not None else DEFAULT_STATIC_DIR
    if chosen_static.exists():
        app.mount("/", StaticFiles(directory=str(chosen_static), html=True), name="spa")
    else:
        log.info(
            "static dir not built; SPA not mounted",
            extra={"static_dir": str(chosen_static)},
        )

    return app


def _client_is_loopback(request: Request) -> bool:
    """True iff the request's *socket peer* is a loopback address.

    Uses the real ASGI client address, never ``X-Forwarded-For`` (spoofable),
    so this can't be forged by a remote LAN client. The Vite dev proxy runs on
    the dev box and connects from 127.0.0.1, so it counts as loopback too.
    """
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def _require_control_auth(
    control_token: str | None,
    authorization: str | None,
    *,
    client_is_loopback: bool = False,
) -> None:
    """Bearer-token guard for mutating control-plane routes.

    When no token is configured, control is *open to loopback only* (the
    dev/sim case: your own machine / the Vite proxy) and still refused for
    anyone else with 503. A configured token is always enforced, regardless
    of where the request came from — so the Pi (token via install_pi.sh)
    behaves exactly as before.
    """
    if control_token is None or control_token == "":
        if client_is_loopback:
            return
        raise HTTPException(status_code=503, detail="control API token is not configured")
    presented = ""
    if authorization is not None and authorization.startswith("Bearer "):
        presented = authorization[len("Bearer ") :]
    # Constant-time compare: a wrong token must not be recoverable by timing
    # the 401. Splitting the scheme off first keeps the comparison token-to-
    # token (compare_digest is only fully constant-time on equal-length input).
    if not secrets.compare_digest(presented, control_token):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _unlink_path(path: Path) -> None:
    path.unlink(missing_ok=True)


async def _run_websocket(websocket: WebSocket, bus: Broadcaster) -> None:
    """Hold one WebSocket open, pumping messages from ``bus`` until disconnect.

    The handler accepts, subscribes, then loops on ``subscription.get()``
    sending each message as JSON. ``WebSocketDisconnect`` (client closed)
    breaks the loop cleanly. Any other exception unsubscribes + re-raises
    — keeping the subscriber list correct under failures.
    """
    await websocket.accept()
    subscription = bus.subscribe()
    try:
        while True:
            message = await subscription.get()
            await websocket.send_json(message)
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(subscription)


def _publish_on_app_loop(
    bus: Broadcaster,
    event: dict[str, Any],
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    if loop is None:
        bus.publish(event)
        return
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is loop:
        bus.publish(event)
        return
    try:
        bus.publish_threadsafe(loop, event)
    except RuntimeError:
        bus.publish(event)


def _canary_version() -> str:
    """Best-effort package version for the OpenAPI doc."""
    try:
        from importlib.metadata import version

        return version("canary")
    except Exception:  # noqa: BLE001 — anything goes wrong, fall back
        return "0.0.0"


def _git_sha() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


__all__ = [
    "StatusSnapshot",
    "build_status",
    "create_app",
    "echo_config",
    "read_anomalies_since",
    "read_latest_telemetry_rows",
    "read_latest_per_channel",
]
