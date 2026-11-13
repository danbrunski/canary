"""Multiple-vehicle profiles: identify the vehicle by VIN, partition its data.

CANary's whole unsupervised stack — training corpus, out-of-sample threshold
calibration, behavior-model surfaces, sensor committees, σ-floors — learns *one
vehicle's* normal. Move the Pi to a different vehicle and its telemetry would
contaminate the same corpus and be scored by the same models. So each vehicle
gets a fully separate tree under a stable ``slug`` directory.

The vehicle's stable unique identifier is its **VIN**, read at startup via
OBD-II Mode 09 PID 0x02 on functional addressing (``0x7DF`` → ``0x7E8``). That
is standard OBD-II and does *not* need the UDS ``C0`` session that gates the
Mode 22 DIDs, so the probe never clashes with Mode 01 polling. Reading it is
request/response polling — the same class of operation the daemon already does
for RPM. It is an ordinary active diagnostic request. The read transmits, so
it lives in :mod:`src.collection.vehicle_probe`, and the caller hands it to
``resolve_active_profile`` (R09.5).

Two identifiers per profile:

* ``slug`` — the stable, filesystem-safe directory key. Derived once at
  creation and **never** changed (renaming must not move data).
* ``name`` — the user-facing display name, blank when a vehicle is first
  auto-registered and editable later from the dashboard.

The registry (``profiles.yaml``, a daemon-managed *data* file separate from
``canary.yaml``) maps VIN → profile. At startup ``resolve_active_profile``
reads the VIN, looks it up (auto-registering an unknown one with a blank name),
and ``scope_config`` rewrites every vehicle-specific path under the slug so all
downstream code — collectors, writer, model managers, retention, replay — keeps
working unchanged on the scoped config.

The module became a package (R26.4). Each part imports only the ones above it:

* :mod:`~src.vehicle.profiles.model` -- ``VehicleProfile``, the parameter table,
  naming (``slugify``) and the registry file I/O (tracked file + machine overlay);
* :mod:`~src.vehicle.profiles.edit` -- edit / revert / delete / restore with their
  evidence rules, and the process's record of how it resolved (``last_resolution``);
* :mod:`~src.vehicle.profiles.scope` -- ``scope_config``, ``profile_data_paths``
  and the profile data locations (``profile_dir``, ``operational_dir``);
* :mod:`~src.vehicle.profiles.legacy` -- the pre-R05.15 shared calibration
  directory: adoption, migration and ``operational_file``;
* :mod:`~src.vehicle.profiles.resolve` -- ``resolve_active_profile`` from the VIN
  probe, and ``resolve_profile_for_tooling``.

This ``__init__`` re-exports every public name the single module had, so an
import site does not move. Patch the owner, not the package: a name set on
``src.vehicle.profiles`` is the re-export, and code in the submodules looks the
name up in its own module (``tests/test_profiles_split.py`` refuses it).

The flash history (``active_registry``, ``active_flash_history`` and the two
registry-path helpers) lives in :mod:`src.vehicle.flash_history`, beside
``tune_revisions``; this package serves those four names lazily, because
``flash_history`` imports the submodules above and imports pandas.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.vehicle.profiles.edit import (
    EDITABLE_FIELDS,
    MAX_TEXT_LENGTH,
    REVERTABLE_FIELDS,
    YEAR_RANGE,
    ActiveVinMismatchError,
    DuplicateVinError,
    EvidenceRequiredError,
    NoTrackedProfileError,
    ProfileDeleteRefusedError,
    ProfileDeletion,
    ProfileEdit,
    ProfileEditError,
    ProfileResolution,
    delete_profile,
    delete_refusal,
    deleted_profiles,
    last_resolution,
    pending_restart,
    profile_overrides,
    record_resolution,
    restore_profile,
    revert_profile,
    update_profile,
    update_profile_name,
    vehicle_block_slug,
)
from src.vehicle.profiles.legacy import (
    LEGACY_CALIBRATION_FILES,
    MIGRATION_MARKER,
    LegacyAdoption,
    legacy_adoption,
    legacy_operational_dir,
    migrate_legacy_calibration,
    operational_file,
    unadopted_legacy_calibration,
)
from src.vehicle.profiles.model import (
    PARAMETER_EVIDENCE,
    PARAMETER_FIELDS,
    REGISTRY_SCHEMA_VERSION,
    VehicleProfile,
    deleted_slugs,
    load_registry,
    parameter_schema,
    parameter_unit,
    profile_module_stem,
    read_registry_file,
    save_registry,
    slugify,
)
from src.vehicle.profiles.resolve import (
    SEEDED_FROM_PROFILE,
    VehicleInfoProbe,
    VehicleInfoProber,
    resolve_active_profile,
    resolve_profile_for_tooling,
    seed_machine_owned_files,
)
from src.vehicle.profiles.scope import (
    SCOPED_DAEMON_DIRS,
    SCOPED_DAEMON_FILES,
    operational_dir,
    operational_registry_path,
    profile_data_paths,
    profile_dir,
    rescope_like,
    scope_config,
    source_registry_path,
)

if TYPE_CHECKING:
    from src.vehicle.flash_history import (
        active_flash_history,
        active_registry,
        active_registry_path,
        active_registry_sources,
    )

#: The names served from :mod:`src.vehicle.flash_history` (see the module docstring).
_FLASH_HISTORY_NAMES = (
    "active_flash_history",
    "active_registry",
    "active_registry_path",
    "active_registry_sources",
)


def __getattr__(name: str) -> Any:
    """The flash-history names, resolved on first use (PEP 562).

    ``flash_history`` imports ``profiles.legacy`` and ``profiles.scope``, which
    run this ``__init__`` first; importing it from here at module level would
    make whichever of the two is imported first fail on the other.
    """
    if name in _FLASH_HISTORY_NAMES:
        from src.vehicle import flash_history

        return getattr(flash_history, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "EDITABLE_FIELDS",
    "LEGACY_CALIBRATION_FILES",
    "MAX_TEXT_LENGTH",
    "MIGRATION_MARKER",
    "PARAMETER_EVIDENCE",
    "PARAMETER_FIELDS",
    "REGISTRY_SCHEMA_VERSION",
    "REVERTABLE_FIELDS",
    "SCOPED_DAEMON_DIRS",
    "SCOPED_DAEMON_FILES",
    "SEEDED_FROM_PROFILE",
    "YEAR_RANGE",
    "ActiveVinMismatchError",
    "DuplicateVinError",
    "EvidenceRequiredError",
    "LegacyAdoption",
    "NoTrackedProfileError",
    "ProfileDeleteRefusedError",
    "ProfileDeletion",
    "ProfileEdit",
    "ProfileEditError",
    "ProfileResolution",
    "VehicleInfoProbe",
    "VehicleInfoProber",
    "VehicleProfile",
    "active_flash_history",
    "active_registry",
    "active_registry_path",
    "active_registry_sources",
    "delete_profile",
    "delete_refusal",
    "deleted_profiles",
    "deleted_slugs",
    "last_resolution",
    "legacy_adoption",
    "legacy_operational_dir",
    "load_registry",
    "migrate_legacy_calibration",
    "operational_dir",
    "operational_file",
    "operational_registry_path",
    "parameter_schema",
    "parameter_unit",
    "pending_restart",
    "profile_data_paths",
    "profile_dir",
    "profile_module_stem",
    "profile_overrides",
    "read_registry_file",
    "record_resolution",
    "rescope_like",
    "resolve_active_profile",
    "resolve_profile_for_tooling",
    "restore_profile",
    "revert_profile",
    "save_registry",
    "scope_config",
    "seed_machine_owned_files",
    "slugify",
    "source_registry_path",
    "unadopted_legacy_calibration",
    "update_profile",
    "update_profile_name",
    "vehicle_block_slug",
]
