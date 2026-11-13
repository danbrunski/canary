"""The pre-R05.15 shared calibration directory: who owns it, and its migration.

Before R05.15 every profile resolved ``jobs_dir.parent / "calibration"``. A
profile adopts that directory on evidence (:func:`legacy_adoption`) and copies it
into its own :func:`~src.vehicle.profiles.scope.operational_dir` once
(:func:`migrate_legacy_calibration`). :func:`operational_file` is here, not in
``scope`` with the other locations, because it falls back to the adopted legacy
copy and ``legacy`` imports ``scope``: the other direction would be a cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from src.atomic_write import atomic_write_text
from src.config import CanaryConfig
from src.vehicle.profiles.model import load_registry
from src.vehicle.profiles.scope import operational_dir, source_registry_path

log = logging.getLogger(__name__)


#: What the pre-R05.15 shared directory can hold: ``append_flash`` wrote the
#: registry; a dashboard memory *verify* wrote the other two, with no registry.
LEGACY_CALIBRATION_FILES = ("tune_revisions.yaml", "dids.yaml", "memory_verify.json")
#: Written into a profile's own ``operational_dir`` when it adopts the legacy
#: directory, so the adoption survives later edits to the tracked registry or a
#: second vehicle joining the registry.
MIGRATION_MARKER = "migrated_from.json"


@dataclass(frozen=True)
class LegacyAdoption:
    """Whether this profile owns the pre-R05.15 shared calibration directory."""

    #: The legacy directory, when it exists (``None``: nothing to adopt).
    path: Path | None
    adopted: bool
    #: Which rule decided, or why none applied.
    reason: str


def _legacy_calibration_dir(config: CanaryConfig) -> Path | None:
    legacy = config.daemon.jobs_dir.parent / "calibration"
    if legacy == operational_dir(config) or not legacy.is_dir():
        return None
    if not any((legacy / name).exists() for name in LEGACY_CALIBRATION_FILES):
        return None
    return legacy


def _read_marker(directory: Path) -> dict[str, Any] | None:
    import json

    try:
        data = json.loads((directory / MIGRATION_MARKER).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _registries_share_history(config: CanaryConfig, legacy_registry: Path) -> tuple[bool, str]:
    from src.vehicle.tune_revisions import load_tune_revisions

    try:
        source = load_tune_revisions(source_registry_path(config))
        shared = load_tune_revisions(legacy_registry)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return False, f"unreadable registry: {exc}"
    if source is None or not source.revisions:
        return False, "this profile has no tracked flash registry to match it against"
    if shared is None or not shared.revisions:
        return False, "the legacy registry is empty"
    first_ours = (source.revisions[0].revision, source.revisions[0].calibration_id)
    first_theirs = (shared.revisions[0].revision, shared.revisions[0].calibration_id)
    if first_ours != first_theirs:
        return False, (
            f"its first revision {first_theirs[0]!r} is not this profile's {first_ours[0]!r}"
        )
    return True, "its flash registry starts with this profile's tracked history"


def legacy_adoption(config: CanaryConfig) -> LegacyAdoption:
    """Does this (scoped) profile own the pre-R05.15 ``<jobs>/calibration``?

    Before R05.15 every profile resolved ``jobs_dir.parent / "calibration"``.
    Ownership is decided from evidence, first match wins:

    1. this profile already migrated it (its :data:`MIGRATION_MARKER` names it);
    2. its ``tune_revisions.yaml`` starts with the same revision as this
       profile's tracked registry (``append_flash`` seeded it from there; a
       flash later hand-added to the tracked file does not break the match);
    3. it holds no registry (only a memory *verify* ran) and this profile is
       the only one in ``profiles.yaml``.

    Anything else is not adopted, and :func:`migrate_legacy_calibration` and
    ``calibration_status`` say so rather than dropping it silently.
    """
    legacy = _legacy_calibration_dir(config)
    if legacy is None:
        return LegacyAdoption(None, False, "no pre-R05.15 calibration directory")
    marker = _read_marker(operational_dir(config))
    if marker is not None and marker.get("source") == str(legacy):
        return LegacyAdoption(legacy, True, "migrated")
    legacy_registry = legacy / "tune_revisions.yaml"
    if legacy_registry.exists():
        shares, why = _registries_share_history(config, legacy_registry)
        return LegacyAdoption(legacy, shares, why)
    slug = config.collection.storage_path.name
    registered = [profile.slug for profile in load_registry(config.profiles)]
    if registered == [slug]:
        return LegacyAdoption(
            legacy, True, "it has no flash registry and this is the only registered profile"
        )
    return LegacyAdoption(
        legacy,
        False,
        "it has no flash registry to prove an owner and profiles.yaml registers "
        f"{len(registered)} profiles",
    )


def legacy_operational_dir(config: CanaryConfig) -> Path | None:
    """The pre-R05.15 shared directory when :func:`legacy_adoption` gives it to this profile.

    Read-only fallback for offline tools, which never migrate (a dry run stays
    read-only); the daemon copies it over once, at profile resolution.
    """
    adoption = legacy_adoption(config)
    return adoption.path if adoption.adopted else None


def unadopted_legacy_calibration(config: CanaryConfig) -> str | None:
    """Why ``<jobs>/calibration`` exists but no profile has adopted it, else ``None``."""
    adoption = legacy_adoption(config)
    if adoption.path is None or adoption.adopted:
        return None
    base = operational_dir(config).parent
    for sibling in base.iterdir() if base.is_dir() else ():
        marker = _read_marker(sibling)
        if marker is not None and marker.get("source") == str(adoption.path):
            return None  # another vehicle's, already migrated there
    return f"{adoption.path} is not adopted by this profile: {adoption.reason}"


_warned_unadopted: set[str] = set()


def migrate_legacy_calibration(config: CanaryConfig) -> list[Path]:
    """Copy an adopted legacy calibration directory into this profile's own, once.

    Runs when the daemon resolves its profile, so the adoption is fixed
    before anyone edits the tracked registry or registers a second vehicle.
    Files already present in :func:`operational_dir` win; the legacy
    directory is left intact. An unadopted legacy directory is logged at
    WARNING. Returns the files written.
    """
    import json
    import shutil

    adoption = legacy_adoption(config)
    if adoption.path is None:
        return []
    if not adoption.adopted:
        reason = unadopted_legacy_calibration(config)
        if reason is not None and reason not in _warned_unadopted:
            _warned_unadopted.add(reason)
            log.warning("legacy calibration directory not adopted: %s", reason)
        return []
    own = operational_dir(config)
    if _read_marker(own) is not None:
        return []
    own.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for name in LEGACY_CALIBRATION_FILES:
        source, destination = adoption.path / name, own / name
        if source.exists() and not destination.exists():
            shutil.copyfile(source, destination)
            copied.append(destination)
    atomic_write_text(
        own / MIGRATION_MARKER,
        json.dumps(
            {
                "source": str(adoption.path),
                "rule": adoption.reason,
                "files": [path.name for path in copied],
                "at": datetime.now(UTC).isoformat(),
            },
            indent=2,
        ),
    )
    log.info(
        "migrated legacy calibration %s -> %s (%s): %s",
        adoption.path,
        own,
        adoption.reason,
        ", ".join(p.name for p in copied) or "nothing to copy",
    )
    return copied


def operational_file(config: CanaryConfig, name: str) -> Path | None:
    """The existing operational ``name``: this vehicle's own, else its legacy copy."""
    own = operational_dir(config) / name
    if own.exists():
        return own
    legacy = legacy_operational_dir(config)
    if legacy is not None and (legacy / name).exists():
        return legacy / name
    return None
