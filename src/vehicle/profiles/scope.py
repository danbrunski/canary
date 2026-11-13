"""Where a profile's data lives: config scoping and the profile data locations.

``scope_config`` nests every vehicle-specific path under the profile's slug;
``profile_data_paths`` finds what a profile left behind from the same field
lists. ``profile_dir``, ``source_registry_path`` and ``operational_dir`` answer
"which file is this vehicle's?" for the tracked and the machine-owned copy.
Imports :mod:`src.vehicle.profiles.model` and nothing else in the package.
"""

from __future__ import annotations

from pathlib import Path

from src.config import CanaryConfig
from src.vehicle.profiles.model import VehicleProfile

# ---------------------------------------------------------------------------
# Config scoping
# ---------------------------------------------------------------------------


#: Every ``daemon`` directory field :func:`scope_config` nests under the slug,
#: and every ``daemon`` file field. One list for scoping and for finding what a
#: profile left behind (:func:`profile_data_paths`), so a newly scoped path
#: cannot be scoped and then missed by the delete's "data stays here" report.
#: ``calibration_dir`` (:func:`_calibration_base`) and ``collection.storage_path``
#: are scoped on their own beside these.
SCOPED_DAEMON_DIRS: tuple[str, ...] = ("models_dir", "anomalies_dir", "jobs_dir", "rom_library_dir")
SCOPED_DAEMON_FILES: tuple[str, ...] = (
    "audit_log",
    "feedback_log",
    "vehicle_ledger",
    "reference_epoch",
    "training_ledger",
    "tuning_config",
    "tuning_audit",
    "trip_state",
    "trend_alert_state",
    "behavior_models",
    "accepted_baselines",
    "custom_pids",
    "capture_profiles",
    "capture_profile_overrides",
    "afr_calibration",
    "kline_profile",
    "catalog_db",
    "memory_ledger",
    "retention_footer_ledger",
)


def _scope_dir(path: Path, slug: str) -> Path:
    """Directory field ``X/Y`` → ``X/Y/<slug>``."""
    return path / slug


def _scope_file(path: Path, slug: str) -> Path:
    """File field ``X/Y/f.ext`` → ``X/Y/<slug>/f.ext``."""
    return path.parent / slug / path.name


def scope_config(config: CanaryConfig, profile: VehicleProfile) -> CanaryConfig:
    """Return a copy of ``config`` with every vehicle-specific path nested under
    the profile slug, and the ``vehicle:`` block reflecting the profile.

    ``profiles.registry_path`` is deliberately *not* scoped — it is the global
    index that maps VIN to slug. The slug is recorded on the result as
    ``config.scoped_profile`` (P02.11), which the vehicle-data readers
    (featurization's ``afr_calibration``, the files beside it) require
    (:func:`src.config.require_profile_scoped`).

    Idempotent for the same profile: scoping an already-scoped config returns
    it unchanged instead of nesting ``<slug>/<slug>/``. Re-scoping to a
    *different* profile raises — its paths are no longer the base paths.
    """
    slug = profile.slug
    if config.scoped_profile is not None:
        if config.scoped_profile == slug:
            return config
        raise ValueError(
            f"config is already scoped to profile {config.scoped_profile!r}; "
            f"cannot re-scope it to {slug!r} (scope the unscoped config instead)"
        )

    collection = config.collection.model_copy(
        update={"storage_path": _scope_dir(config.collection.storage_path, slug)}
    )
    scoped_paths: dict[str, Path] = {
        name: _scope_dir(getattr(config.daemon, name), slug) for name in SCOPED_DAEMON_DIRS
    }
    scoped_paths.update(
        {name: _scope_file(getattr(config.daemon, name), slug) for name in SCOPED_DAEMON_FILES}
    )
    scoped_paths["calibration_dir"] = _scope_dir(_calibration_base(config), slug)
    daemon = config.daemon.model_copy(update=scoped_paths)
    vehicle = config.vehicle.model_copy(
        update={
            "make": profile.make,
            "model": profile.model,
            "year": profile.year if profile.year is not None else 0,
            "vin": profile.vin,
            "parameters": profile.parameters,
        }
    )
    scoped = config.model_copy(
        update={"collection": collection, "daemon": daemon, "vehicle": vehicle}
    )
    scoped._scoped_profile = slug
    scoped._scope_source = profile
    return scoped


def rescope_like(config: CanaryConfig, reference: CanaryConfig) -> CanaryConfig:
    """``config`` (freshly loaded, unscoped) scoped with the very profile
    ``reference`` was scoped with: its slug *and* the registry vehicle
    parameters ``resolve_active_profile`` applied, never a re-derivation.

    The daemon's config hot-reload goes through this (P02.11): a bare
    ``from_yaml`` swapped in an unscoped config and every live tick refused.
    ``reference`` unscoped (profiles off) returns ``config`` as loaded.
    """
    profile = reference._scope_source
    if profile is None:
        return config
    if not isinstance(profile, VehicleProfile):
        raise TypeError(f"scoped config carries {type(profile).__name__}, not a VehicleProfile")
    return scope_config(config, profile)


def profile_data_paths(config: CanaryConfig, slug: str) -> list[Path]:
    """The existing directories that hold ``slug``'s data, found from ``config``.

    ``config`` is scoped to one profile, so each scoped directory ``X/<active>``
    has its sibling ``X/<slug>`` and each scoped file ``X/Y/<active>/f`` its
    sibling directory ``X/Y/<slug>``. The same field lists :func:`scope_config`
    scopes by (:data:`SCOPED_DAEMON_DIRS`, :data:`SCOPED_DAEMON_FILES`,
    ``calibration_dir``, ``collection.storage_path``), so a path cannot be
    scoped without being found here. Includes the tracked ``data/<slug>/``
    that ``capture_profiles`` and the like live in. Nothing here is ever
    deleted or moved by a profile delete. Empty for an unscoped config
    (profiles disabled), where there are no per-vehicle directories.
    """
    if config.scoped_profile is None:
        return []
    daemon = config.daemon
    scoped_dirs = [
        config.collection.storage_path,
        *(getattr(daemon, name) for name in SCOPED_DAEMON_DIRS),
        _calibration_base(config),
    ]
    scoped_files = [getattr(daemon, name) for name in SCOPED_DAEMON_FILES]
    siblings = [path.parent / slug for path in scoped_dirs]
    siblings += [path.parent.parent / slug for path in scoped_files]
    return [path for path in dict.fromkeys(siblings) if path.exists()]


# ---------------------------------------------------------------------------
# Profile data locations (R05.1)
# ---------------------------------------------------------------------------
#
# Below ``control`` so analysis can resolve the same files the control plane
# writes. ``src.control.calibration`` re-exports these; there is one answer to
# "which flash registry is live?".


def profile_dir(config: CanaryConfig) -> Path:
    """The vehicle's tracked profile-data directory (beside ``afr_calibration``)."""
    return config.daemon.afr_calibration.parent


def source_registry_path(config: CanaryConfig) -> Path:
    """The tracked flash registry shipped with the profile data."""
    return profile_dir(config) / "tune_revisions.yaml"


def _calibration_base(config: CanaryConfig) -> Path:
    """``daemon.calibration_dir`` when set (YAML or ``scope_config``), else beside ``jobs_dir``.

    Resolved at call time rather than by a validator so a ``model_copy`` that
    moves ``jobs_dir`` (tests, ``rebase_state_paths``) moves this with it.
    """
    if "calibration_dir" in config.daemon.model_fields_set:
        return config.daemon.calibration_dir
    return config.daemon.jobs_dir.parent / "calibration"


def operational_dir(config: CanaryConfig) -> Path:
    """Mutable calibration state outside tracked profile data, one per vehicle.

    For a scoped config this is ``<control>/calibration/<slug>`` (R05.15). It
    used to be ``jobs_dir.parent / "calibration"``, which for a scoped
    ``jobs_dir`` (``<control>/jobs/<slug>``) is one directory inside ``jobs/``
    shared by every profile: a second vehicle's dashboard-recorded flash would
    have landed in, and been served from, the first vehicle's registry.
    """
    return _calibration_base(config)


def operational_registry_path(config: CanaryConfig) -> Path:
    """Where ``append_flash`` records flashes made from the dashboard."""
    return operational_dir(config) / "tune_revisions.yaml"
