"""Calibration history and RAM-address evidence for the control plane."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from src.analysis.tune_revisions import TuneRevision, TuneRevisionRegistry, load_tune_revisions
from src.atomic_write import atomic_write_text
from src.collection import tuning_profile
from src.config import CanaryConfig
from src.decode.dids import DidMap, MemoryChannel, load_did_map


def profile_dir(config: CanaryConfig) -> Path:
    return config.daemon.afr_calibration.parent


def source_registry_path(config: CanaryConfig) -> Path:
    return profile_dir(config) / "tune_revisions.yaml"


def operational_dir(config: CanaryConfig) -> Path:
    """Mutable state outside tracked profile data, like capture-profile state."""
    return config.daemon.jobs_dir.parent / "calibration"


def operational_registry_path(config: CanaryConfig) -> Path:
    return operational_dir(config) / "tune_revisions.yaml"


def operational_memory_path(config: CanaryConfig) -> Path:
    return operational_dir(config) / "dids.yaml"


def _seed(source: Path, destination: Path) -> Path:
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return destination


def active_registry_path(config: CanaryConfig) -> Path:
    override = operational_registry_path(config)
    return override if override.exists() else source_registry_path(config)


def active_memory_path(config: CanaryConfig, source: Path) -> Path:
    override = operational_memory_path(config)
    return override if override.exists() else source


def calibration_payload(config: CanaryConfig) -> dict[str, Any]:
    registry = load_tune_revisions(active_registry_path(config))
    running = None if registry is None else registry.at(datetime.now(UTC))
    ecu = tuning_profile.calibration_snapshot()
    calibration_id = ecu["ecu_calibration_id"]
    matches = (
        None
        if calibration_id is None or running is None or running.calibration_id is None
        else calibration_id == running.calibration_id
    )
    return {
        "running_revision": None if running is None else running.revision,
        "registry_calibration_id": None if running is None else running.calibration_id,
        "ecu_calibration_id": calibration_id,
        "ecu_calibration_read_at": ecu["ecu_calibration_read_at"],
        "calibration_matches_registry": matches,
        "calibration_changed": ecu["ecu_calibration_changed"],
        "history": []
        if registry is None
        else [revision.model_dump(mode="json") for revision in reversed(registry.revisions)],
        "storage": str(operational_registry_path(config)),
    }


def memory_channel_state(channel: MemoryChannel, running_revision: str | None) -> str:
    if channel.revision_carried_reason:
        return "carried"
    if channel.known_answer is None:
        return "unverifiable"
    if running_revision is not None and channel.verified_tune_revision == running_revision:
        return "verified"
    return "checked earlier"


def memory_channels_payload(config: CanaryConfig, source: Path) -> dict[str, Any]:
    path = active_memory_path(config, source)
    did_map = load_did_map(path)
    registry = load_tune_revisions(active_registry_path(config))
    running = None if registry is None else registry.at(datetime.now(UTC))
    revision = None if running is None else running.revision
    channels = []
    for channel in did_map.memory:
        channels.append(
            {
                "name": channel.name,
                "address": f"0x{channel.address:08X}",
                "window": channel.window,
                "unit": channel.unit,
                "verified_tune_revision": channel.verified_tune_revision,
                "revision_carried_reason": channel.revision_carried_reason,
                "known_answer": None
                if channel.known_answer is None
                else channel.known_answer.model_dump(mode="json"),
                "state": memory_channel_state(channel, revision),
            }
        )
    return {"running_revision": revision, "channels": channels, "count": len(channels)}


def append_flash(config: CanaryConfig, revision: TuneRevision) -> TuneRevisionRegistry:
    source = source_registry_path(config)
    destination = operational_registry_path(config)
    if source.exists():
        _seed(source, destination)
    registry = load_tune_revisions(destination) or TuneRevisionRegistry()
    if any(item.revision == revision.revision for item in registry.revisions):
        raise ValueError(f"revision {revision.revision!r} already exists")
    updated = TuneRevisionRegistry(revisions=[*registry.revisions, revision])
    atomic_write_text(
        destination,
        yaml.safe_dump(updated.model_dump(mode="json"), sort_keys=False),
        mkdir=True,
    )
    tuning_profile.configure_running_tune_revision(revision.revision)
    return updated


def seed_operational_memory(config: CanaryConfig, source: Path) -> Path:
    destination = operational_memory_path(config)
    _seed(source, destination)
    # Validate the copy before callers are allowed to edit it.
    DidMap.model_validate(yaml.safe_load(destination.read_text()))
    return destination


__all__ = [
    "active_memory_path",
    "active_registry_path",
    "append_flash",
    "calibration_payload",
    "memory_channel_state",
    "memory_channels_payload",
    "operational_memory_path",
    "operational_registry_path",
    "seed_operational_memory",
]
