"""Dashboard status snapshot assembly."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.alerts.emitter import AnomalyEventEmitter
from src.config import CanaryConfig
from src.models.health import HealthMonitor
from src.preprocessing.loader import discover_sessions


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
    #: "is the anomaly path firing?".
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
    """Read the version field from a model manifest JSON, or ``None``."""
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


__all__ = ["StatusSnapshot", "build_status"]
