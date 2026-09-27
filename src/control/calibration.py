"""Calibration history and RAM-address evidence for the control plane."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from src.atomic_write import atomic_write_text
from src.collection import tuning_profile
from src.config import CanaryConfig
from src.decode.dids import DidMap, MemoryChannel, load_did_map
from src.vehicle.profiles import (
    active_registry,
    active_registry_path,
    active_registry_sources,
    legacy_operational_dir,
    operational_dir,
    operational_file,
    operational_registry_path,
    profile_dir,
    source_registry_path,
    unadopted_legacy_calibration,
)
from src.vehicle.tune_revisions import (
    RegistryConflictError,
    TuneRevision,
    TuneRevisionRegistry,
    calibration_matches,
)


def operational_memory_path(config: CanaryConfig) -> Path:
    return operational_dir(config) / "dids.yaml"


def _seed(source: Path, destination: Path) -> Path:
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return destination


def active_memory_path(config: CanaryConfig, source: Path) -> Path:
    override = operational_file(config, "dids.yaml")
    return override if override is not None else source


def calibration_status(config: CanaryConfig) -> dict[str, Any]:
    """The running-calibration facts ``/status`` and ``/control/calibration`` share.

    One function, so the two cannot disagree about whether the ECU matches
    the flash history. The registry is re-read each call: an appended flash
    or a pulled registry edit shows up without a restart.
    """
    conflict: str | None = None
    try:
        registry = active_registry(config)
    except RegistryConflictError as exc:
        # R05.17: the two registries disagree about one flash. Which revision
        # is running is then unknown; say so on /status rather than failing it.
        registry, conflict = None, str(exc)
    running = None if registry is None else registry.at(datetime.now(UTC))
    ecu = tuning_profile.calibration_snapshot()
    calibration_id = ecu["ecu_calibration_id"]
    registry_id = None if running is None else running.calibration_id
    matches = calibration_matches(calibration_id, registry_id)
    changed = bool(ecu["ecu_calibration_changed"])
    reason: str | None = None
    if conflict is not None:
        reason = f"flash history refused: {conflict}"
    elif matches is False:
        reason = (
            f"ECU reports {calibration_id}, flash history's running revision "
            f"{running.revision if running is not None else '?'} records {registry_id}"
        )
    elif changed:
        reason = "ECU calibration id changed during this daemon lifetime"
    return {
        "running_revision": None if running is None else running.revision,
        "registry_calibration_id": registry_id,
        "ecu_calibration_id": calibration_id,
        "ecu_calibration_read_at": ecu["ecu_calibration_read_at"],
        "calibration_matches_registry": matches,
        "calibration_changed": changed,
        # W02.3: a reflash the registry has not heard about, or one seen
        # happen underneath the running daemon. Loud by design.
        "calibration_alert": reason is not None,
        "calibration_alert_reason": reason,
        # R05.15: the pre-R05.15 shared calibration directory exists but no
        # profile owns it, so its flashes / verified RAM catalog are not read.
        "legacy_calibration_unadopted": unadopted_legacy_calibration(config),
    }


def calibration_payload(config: CanaryConfig) -> dict[str, Any]:
    try:
        registry = active_registry(config)
    except RegistryConflictError:
        registry = None  # calibration_status names the conflict as the alert
    return {
        **calibration_status(config),
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
    try:
        registry = active_registry(config)
    except RegistryConflictError:
        registry = None  # calibration_status names the conflict as the alert
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
    # Start from what readers see now (R05.17): the operational copy — this
    # vehicle's own, or the legacy shared one it owns (R05.15) — merged with
    # the tracked registry, so a revision hand-added to the tracked file is
    # both a duplicate here and written through, not orphaned.
    destination = operational_registry_path(config)
    registry = active_registry(config) or TuneRevisionRegistry()
    if any(item.revision == revision.revision for item in registry.revisions):
        raise ValueError(f"revision {revision.revision!r} already exists")
    updated = TuneRevisionRegistry(revisions=[*registry.revisions, revision])
    atomic_write_text(
        destination,
        yaml.safe_dump(updated.model_dump(mode="json"), sort_keys=False),
        mkdir=True,
    )
    tuning_profile.configure_running_tune_revision(
        revision.revision, calibration_id=revision.calibration_id
    )
    return updated


def last_verify_path(config: CanaryConfig) -> Path:
    return operational_dir(config) / "memory_verify.json"


def record_verify_results(config: CanaryConfig, verdicts: dict[str, str]) -> None:
    """Persist the last known-answer outcome per channel (``pass``/``fail``/...).

    Merged over the previous record, so verifying one window does not turn
    every other channel back into "never ran".
    """
    previous = _read_verify_results(config)  # includes an adopted legacy record
    stamp = datetime.now(UTC).isoformat()
    merged = {**previous, **{name: {"outcome": out, "at": stamp} for name, out in verdicts.items()}}
    atomic_write_text(last_verify_path(config), json.dumps(merged, indent=2), mkdir=True)


def _read_verify_results(config: CanaryConfig) -> dict[str, dict[str, str]]:
    path = operational_file(config, "memory_verify.json")
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(name): entry
        for name, entry in data.items()
        if isinstance(entry, dict) and isinstance(entry.get("outcome"), str)
    }


def memory_verify_summary(config: CanaryConfig, source: Path) -> dict[str, int]:
    """Catalogued channels by their last known-answer verify: passed/failed/never ran.

    ``unverifiable`` channels declare no check; they count as neither passed
    nor failed, and are reported on their own so "never ran" means exactly
    that.
    """
    did_map = load_did_map(active_memory_path(config, source))
    results = _read_verify_results(config)
    counts = {"total": 0, "passed": 0, "failed": 0, "unverifiable": 0, "never_ran": 0}
    for channel in did_map.memory:
        counts["total"] += 1
        outcome = results.get(channel.name, {}).get("outcome")
        if outcome == "pass":
            counts["passed"] += 1
        elif outcome == "fail":
            counts["failed"] += 1
        elif outcome == "unverifiable" or channel.known_answer is None:
            counts["unverifiable"] += 1
        else:
            counts["never_ran"] += 1
    return counts


def seed_operational_memory(config: CanaryConfig, source: Path) -> Path:
    destination = operational_memory_path(config)
    _seed(active_memory_path(config, source), destination)
    # Validate the copy before callers are allowed to edit it.
    DidMap.model_validate(yaml.safe_load(destination.read_text()))
    return destination


__all__ = [
    "active_memory_path",
    "active_registry",
    "active_registry_path",
    "active_registry_sources",
    "append_flash",
    "calibration_payload",
    "calibration_status",
    "last_verify_path",
    "legacy_operational_dir",
    "memory_verify_summary",
    "record_verify_results",
    "memory_channel_state",
    "memory_channels_payload",
    "operational_dir",
    "operational_file",
    "operational_memory_path",
    "operational_registry_path",
    "profile_dir",
    "seed_operational_memory",
    "source_registry_path",
    "unadopted_legacy_calibration",
]
