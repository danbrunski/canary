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

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import read_events_dir
from src.config import CanaryConfig
from src.dashboard.broadcast import Broadcaster
from src.preprocessing.loader import (
    PARQUET_PATTERN,
    discover_sessions,
)

#: Where the React SPA's built artifacts land — relative to repo root in dev,
#: but the daemon resolves it via ``Path(__file__).parent / "static" / "dist"``
#: which works regardless of cwd.
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static" / "dist"

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
    version = data.get("version") or data.get("training", {}).get("model_version")
    return str(version) if version is not None else None


def _resolve_latest_session(storage_path: Path) -> Path | None:
    sessions = discover_sessions(storage_path)
    return sessions[-1] if sessions else None


def build_status(
    config: CanaryConfig,
    *,
    models_dir: Path | None = None,
    emitter: AnomalyEventEmitter | None = None,
) -> StatusSnapshot:
    """Assemble the status snapshot used by ``GET /status``."""
    storage = config.collection.storage_path
    latest = _resolve_latest_session(storage)
    model_versions: dict[str, str | None] = {}
    if models_dir is not None:
        # Convention: <models_dir>/<name>.manifest.json. Both IF and LSTM use it.
        for manifest in sorted(models_dir.glob("*.manifest.json")):
            model_versions[manifest.stem.removesuffix(".manifest")] = _model_version(manifest)
    return StatusSnapshot(
        storage_path=str(storage),
        free_mb=_free_mb(storage),
        latest_session=latest.name if latest else None,
        latest_session_path=str(latest) if latest else None,
        model_versions=model_versions,
        vehicle=f"{config.vehicle.year} {config.vehicle.make} {config.vehicle.model}".strip(),
        emitter_counters=emitter.counters.to_dict() if emitter is not None else None,
    )


# ---------------------------------------------------------------------------
# Telemetry latest — newest Parquet chunk, last N rows
# ---------------------------------------------------------------------------


def read_latest_telemetry_rows(
    storage_path: Path,
    *,
    n: int = 100,
) -> list[dict[str, Any]]:
    """Return the last ``n`` rows from the newest chunk of the newest session.

    Returns an empty list when there's no session on disk. Reads only the
    newest chunk to keep latency bounded — cross-chunk pagination is a
    future feature; the dashboard's live feed will use the WebSocket path.
    """
    latest = _resolve_latest_session(storage_path)
    if latest is None:
        return []
    chunks = sorted(latest.glob(PARQUET_PATTERN))
    if not chunks:
        return []
    table = pd.read_parquet(chunks[-1])
    if len(table) > n:
        table = table.tail(n)
    rows: list[dict[str, Any]] = []
    for record in table.to_dict(orient="records"):
        rows.append(_normalize_telemetry_row({str(k): v for k, v in record.items()}))
    return rows


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
    static_dir: Path | None = None,
    enable_dev_cors: bool = False,
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
        # `npm run dev` serves the SPA on :5173 and proxies the backend.
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
    # Stash them on app.state so producer threads / app extensions can reach
    # the same instances after construction.
    app.state.telemetry_broadcaster = telemetry_bus
    app.state.anomalies_broadcaster = anomalies_bus

    @app.get("/status")
    def get_status() -> dict[str, Any]:
        return build_status(config, models_dir=models_dir, emitter=emitter).to_dict()

    @app.get("/config")
    def get_config() -> dict[str, Any]:
        return echo_config(config)

    @app.get("/anomalies")
    def get_anomalies(since: str | None = Query(default=None)) -> dict[str, Any]:
        try:
            events = read_anomalies_since(anomalies_dir, since=since)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"bad `since`: {exc}") from exc
        return {"events": events, "count": len(events)}

    @app.get("/telemetry/latest")
    def get_telemetry_latest(
        n: int = Query(default=100, ge=1, le=10_000),
    ) -> dict[str, Any]:
        rows = read_latest_telemetry_rows(config.collection.storage_path, n=n)
        return {"rows": rows, "count": len(rows)}

    @app.websocket("/ws/telemetry")
    async def ws_telemetry(websocket: WebSocket) -> None:
        await _run_websocket(websocket, telemetry_bus)

    @app.websocket("/ws/anomalies")
    async def ws_anomalies(websocket: WebSocket) -> None:
        await _run_websocket(websocket, anomalies_bus)

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


def _canary_version() -> str:
    """Best-effort package version for the OpenAPI doc."""
    try:
        from importlib.metadata import version

        return version("canary")
    except Exception:  # noqa: BLE001 — anything goes wrong, fall back
        return "0.0.0"


__all__ = [
    "StatusSnapshot",
    "build_status",
    "create_app",
    "echo_config",
    "read_anomalies_since",
    "read_latest_telemetry_rows",
]
