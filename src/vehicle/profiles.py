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
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from src.atomic_write import atomic_write_text
from src.config import CanaryConfig, VehicleParameters

if TYPE_CHECKING:
    from src.vehicle.tune_revisions import TuneRevisionRegistry

log = logging.getLogger(__name__)

REGISTRY_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Profile model + registry I/O
# ---------------------------------------------------------------------------


@dataclass
class VehicleProfile:
    """One vehicle's identity. ``slug`` is the stable path key; ``name`` is the
    editable display label (blank until the operator sets it)."""

    slug: str
    vin: str
    name: str = ""
    make: str = ""
    model: str = ""
    year: int | None = None
    parameters: VehicleParameters = field(default_factory=VehicleParameters)
    #: Mode 09 InfoType 0x04/0x06 (Task 3.4) — last-seen ECU calibration ID /
    #: verification number for this profile. Blank until the first successful
    #: probe; a *changed* calibration ID for an already-registered profile is
    #: an ECU reflash (see ``resolve_active_profile``'s corpus-boundary hook).
    calibration_id: str = ""
    cvn: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "vin": self.vin,
            "name": self.name,
            "make": self.make,
            "model": self.model,
            "year": self.year,
            "parameters": self.parameters.model_dump(),
            "calibration_id": self.calibration_id,
            "cvn": self.cvn,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VehicleProfile:
        params = data.get("parameters") or {}
        return cls(
            slug=str(data["slug"]),
            vin=str(data.get("vin", "")),
            name=str(data.get("name", "")),
            make=str(data.get("make", "")),
            model=str(data.get("model", "")),
            year=data.get("year"),
            parameters=VehicleParameters(**params),
            calibration_id=str(data.get("calibration_id", "")),
            cvn=str(data.get("cvn", "")),
        )


def slugify(text: str) -> str:
    """Lowercase, hyphenate, and strip to a filesystem-safe slug.

    Never returns a leading ``_`` (reserved for ``_archive`` / ``_replay-*``,
    which training discovery and the retention sweep skip — see
    ``src/preprocessing/loader.py``) and never empty.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = slug.lstrip("_") or "vehicle"
    return slug


def profile_module_stem(slug: str) -> str:
    """A Python module name for a profile's per-vehicle code (R05.4, R05.7).

    ``-`` becomes ``_``; a slug that is still not an identifier (a VIN-derived
    slug starting with a digit) gets a ``vehicle_`` prefix. One rule, so a
    profile's recipe and signal modules are found under the same name.
    """
    stem = slug.replace("-", "_")
    return stem if stem.isidentifier() else f"vehicle_{stem}"


def _dedupe_slug(slug: str, taken: set[str]) -> str:
    if slug not in taken:
        return slug
    i = 2
    while f"{slug}-{i}" in taken:
        i += 1
    return f"{slug}-{i}"


def load_registry(path: Path) -> list[VehicleProfile]:
    """Load the VIN → profile registry. A missing file is an empty registry."""
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    entries = raw.get("profiles", []) if isinstance(raw, dict) else []
    return [VehicleProfile.from_dict(entry) for entry in entries]


def save_registry(path: Path, profiles: list[VehicleProfile]) -> None:
    """Persist the registry (atomic temp-file + replace)."""
    payload = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "profiles": [p.to_dict() for p in profiles],
    }
    atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False), mkdir=True)


def update_profile_name(path: Path, slug: str, name: str) -> VehicleProfile:
    """Set a profile's display name in the registry; return the updated profile.

    Raises ``KeyError`` if no profile has that slug. Renaming touches only the
    ``name`` field — the slug and every path stays put.
    """
    profiles = load_registry(path)
    for profile in profiles:
        if profile.slug == slug:
            profile.name = name.strip()
            save_registry(path, profiles)
            return profile
    raise KeyError(slug)


# ---------------------------------------------------------------------------
# VIN probe result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VehicleInfoProbe:
    """Result of the startup Mode 09 probe (Task 3.4 extends the VIN-only read).

    The probe itself transmits, so it lives in
    :func:`src.collection.vehicle_probe.probe_vehicle_info`; resolution takes
    it from its caller as a :data:`VehicleInfoProber` (R09.5).
    """

    vin: str | None
    calibration_id: str | None
    cvn: str | None


#: How resolution reads the vehicle: the caller's Mode 09 probe
#: (:func:`src.collection.vehicle_probe.probe_vehicle_info` on the daemon).
VehicleInfoProber = Callable[[CanaryConfig], VehicleInfoProbe]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _default_profile(config: CanaryConfig, *, slug: str | None = None) -> VehicleProfile:
    """The fallback profile built from the ``vehicle:`` block — used when no
    VIN can be read, or as the target of a ``--profile`` override."""
    veh = config.vehicle
    derived = slugify(f"{veh.make}-{veh.model}-{veh.year}")
    name = f"{veh.year} {veh.make} {veh.model}".strip()
    return VehicleProfile(
        slug=slug or derived,
        vin=veh.vin,
        name=name,
        make=veh.make,
        model=veh.model,
        year=veh.year,
        parameters=veh.parameters,
    )


def _new_profile_for_vin(vin: str, *, taken: set[str]) -> VehicleProfile:
    """A freshly-seen vehicle: stable slug from the VIN, blank display name for
    the operator to fill in from the dashboard."""
    return VehicleProfile(slug=_dedupe_slug(slugify(vin), taken), vin=vin, name="")


def _maybe_persist(
    config: CanaryConfig,
    profiles: list[VehicleProfile],
    profile: VehicleProfile,
) -> None:
    """Add ``profile`` to the registry if its slug is new, so the active vehicle
    is always listable/nameable from the dashboard. No-op when
    ``profiles.auto_register`` is off or the slug already exists."""
    if not config.profiles.auto_register:
        return
    if any(p.slug == profile.slug for p in profiles):
        return
    profiles.append(profile)
    save_registry(config.profiles.registry_path, profiles)
    log.warning(
        "registered vehicle profile %r (vin=%s) — set a display name in the "
        "dashboard Profiles panel",
        profile.slug,
        profile.vin or "unknown",
    )


def _apply_calibration_probe(
    config: CanaryConfig,
    profiles: list[VehicleProfile],
    profile: VehicleProfile,
    probe: VehicleInfoProbe,
) -> None:
    """Update ``profile``'s stored calibration ID/CVN from a fresh Mode 09
    probe (Task 3.4), persisting the registry when either changed.

    A changed calibration ID for an *already-registered* profile (i.e. one
    that had a non-blank stored value already) is an ECU reflash — logged as
    a ``corpus_boundary`` vehicle-ledger event, the same mechanism
    ``eval-longitudinal-and-dtc.md``'s Task 3.5 corpus-boundary integration
    already reads (``VehicleLedgerStore.latest_boundary`` / the
    ``--since-boundary`` corpus filter). A brand-new profile's first
    calibration read is just a baseline, not a boundary.
    """
    if not probe.calibration_id and not probe.cvn:
        return  # nothing read this probe; keep whatever was last stored
    previous_calibration_id = profile.calibration_id
    reflashed = bool(
        previous_calibration_id
        and probe.calibration_id
        and probe.calibration_id != previous_calibration_id
    )
    if probe.calibration_id:
        profile.calibration_id = probe.calibration_id
    if probe.cvn:
        profile.cvn = probe.cvn
    save_registry(config.profiles.registry_path, profiles)
    if reflashed:
        _record_calibration_boundary(config, profile, previous_calibration_id)


def _record_calibration_boundary(
    config: CanaryConfig,
    profile: VehicleProfile,
    previous_calibration_id: str,
) -> None:
    """Append a ``corpus_boundary`` vehicle-ledger event for an ECU reflash
    (Task 3.4). Scoped straight to this profile's ledger path via
    ``_scope_file`` rather than a full ``scope_config`` call — this runs
    from inside ``resolve_active_profile``, before its caller has scoped
    ``config`` (the profile isn't resolved yet at that point).
    """
    from src.vehicle.vehicle_ledger import VehicleLedgerStore

    ledger = VehicleLedgerStore(_scope_file(config.daemon.vehicle_ledger, profile.slug))
    ledger.record(
        occurred_at=datetime.now(UTC).date(),
        kind="mod",
        title=f"ECU calibration changed: {previous_calibration_id} -> {profile.calibration_id}",
        details=f"cvn={profile.cvn or 'unknown'}",
        corpus_boundary=True,
    )
    log.warning(
        "ECU calibration ID changed (reflash detected) — corpus-boundary ledger entry appended",
        extra={
            "slug": profile.slug,
            "previous_calibration_id": previous_calibration_id,
            "calibration_id": profile.calibration_id,
        },
    )


def _resolve_active_profile(
    config: CanaryConfig,
    *,
    probe: VehicleInfoProber,
    override_slug: str | None = None,
) -> VehicleProfile:
    """Determine which vehicle this is and return its profile.

    * ``override_slug`` (CLI ``--profile``) → that registry entry, or a
      synthetic default under that slug; the VIN probe is skipped.
    * else read the VIN with ``probe``; a registry hit returns that profile;
      an unknown VIN is registered with a blank display name for the operator
      to fill in.
    * no VIN (disabled / virtual / probe failed) → the default profile.

    The resolved profile is persisted to the registry (when ``auto_register``)
    if not already present, so the dashboard can always show and rename it.
    A registered profile's calibration ID/CVN (Task 3.4) is refreshed from the
    same probe; a changed calibration ID is logged as a corpus-boundary event.
    """
    profiles = load_registry(config.profiles.registry_path)
    by_slug = {p.slug: p for p in profiles}
    by_vin = {p.vin: p for p in profiles if p.vin}

    if override_slug is not None:
        if override_slug in by_slug:
            return by_slug[override_slug]
        profile = _default_profile(config, slug=override_slug)
        _maybe_persist(config, profiles, profile)
        return profile

    if not config.profiles.enabled:
        return _default_profile(config)

    vin_probe = probe(config)
    vin = vin_probe.vin
    if vin is None:
        profile = _default_profile(config)
        _maybe_persist(config, profiles, profile)
        return profile

    if vin in by_vin:
        profile = by_vin[vin]
    else:
        profile = _new_profile_for_vin(vin, taken=set(by_slug))
        _maybe_persist(config, profiles, profile)

    # Only touch calibration bookkeeping for a profile that's actually in the
    # persisted registry — `_maybe_persist` above is a no-op when
    # `auto_register` is off, and an unregistered profile has no registry row
    # to update (mirrors that gate rather than silently rewriting the file).
    if any(p.slug == profile.slug for p in profiles):
        _apply_calibration_probe(config, profiles, profile, vin_probe)
    return profile


def resolve_active_profile(
    config: CanaryConfig,
    *,
    probe: VehicleInfoProber,
    override_slug: str | None = None,
) -> VehicleProfile:
    """Determine which vehicle this is and return its profile (the daemon's path).

    See :func:`_resolve_active_profile`. Also migrates, once, the pre-R05.15
    shared calibration directory into the resolved profile's own when the
    profile owns it (:func:`migrate_legacy_calibration`). A migration failure
    is logged, never fatal: the read-only fallback still finds the files.
    """
    profile = _resolve_active_profile(config, probe=probe, override_slug=override_slug)
    try:
        migrate_legacy_calibration(scope_config(config, profile))
    except (OSError, ValueError) as exc:
        log.warning("legacy calibration migration failed: %s", exc)
    return profile


def resolve_profile_for_tooling(config: CanaryConfig, slug: str | None) -> VehicleProfile:
    """Resolve a profile for offline tools (refit, migration) WITHOUT a VIN probe
    and WITHOUT mutating the registry (so a dry-run stays read-only).

    ``--profile <slug>`` selects a registry entry (or a synthetic default under
    that slug); otherwise the ``vehicle:`` block's default profile is used. A
    config already scoped (P02.11) resolves to the profile it was scoped to, so
    re-resolving downstream of an entry point's ``scope_config`` is a no-op.
    """
    slug = slug if slug is not None else config.scoped_profile
    if slug is not None:
        for profile in load_registry(config.profiles.registry_path):
            if profile.slug == slug:
                return profile
        return _default_profile(config, slug=slug)
    return _default_profile(config)


# ---------------------------------------------------------------------------
# Config scoping
# ---------------------------------------------------------------------------


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
    daemon = config.daemon.model_copy(
        update={
            "models_dir": _scope_dir(config.daemon.models_dir, slug),
            "anomalies_dir": _scope_dir(config.daemon.anomalies_dir, slug),
            "jobs_dir": _scope_dir(config.daemon.jobs_dir, slug),
            "audit_log": _scope_file(config.daemon.audit_log, slug),
            "feedback_log": _scope_file(config.daemon.feedback_log, slug),
            "vehicle_ledger": _scope_file(config.daemon.vehicle_ledger, slug),
            "reference_epoch": _scope_file(config.daemon.reference_epoch, slug),
            "training_ledger": _scope_file(config.daemon.training_ledger, slug),
            "tuning_config": _scope_file(config.daemon.tuning_config, slug),
            "tuning_audit": _scope_file(config.daemon.tuning_audit, slug),
            "trip_state": _scope_file(config.daemon.trip_state, slug),
            "trend_alert_state": _scope_file(config.daemon.trend_alert_state, slug),
            "behavior_models": _scope_file(config.daemon.behavior_models, slug),
            "accepted_baselines": _scope_file(config.daemon.accepted_baselines, slug),
            "custom_pids": _scope_file(config.daemon.custom_pids, slug),
            "capture_profiles": _scope_file(config.daemon.capture_profiles, slug),
            "afr_calibration": _scope_file(config.daemon.afr_calibration, slug),
            "kline_profile": _scope_file(config.daemon.kline_profile, slug),
            "catalog_db": _scope_file(config.daemon.catalog_db, slug),
            "calibration_dir": _scope_dir(_calibration_base(config), slug),
        }
    )
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
    registered = [profile.slug for profile in load_registry(config.profiles.registry_path)]
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


def operational_registry_path(config: CanaryConfig) -> Path:
    """Where ``append_flash`` records flashes made from the dashboard."""
    return operational_dir(config) / "tune_revisions.yaml"


def active_registry_path(config: CanaryConfig) -> Path:
    """The operational registry file once one exists, else the tracked one.

    A *file identity*, not the flash history: the operational copy lacks any
    revision hand-added to the tracked file after it was seeded (R05.17).
    Read the history with :func:`active_registry`, which merges both; cite
    the files it read with :func:`active_registry_sources`.
    """
    override = operational_file(config, "tune_revisions.yaml")
    return override if override is not None else source_registry_path(config)


def active_registry_sources(config: CanaryConfig) -> tuple[Path, ...]:
    """The existing registry files :func:`active_registry` reads, operational first."""
    tracked = source_registry_path(config)
    operational = operational_file(config, "tune_revisions.yaml")
    paths = [p for p in (operational, tracked) if p is not None and p.exists()]
    return tuple(dict.fromkeys(paths))


def active_registry(config: CanaryConfig) -> TuneRevisionRegistry | None:
    """The vehicle's flash history: the operational registry merged with the tracked one.

    R05.17: once ``append_flash`` created the operational copy it used to win
    outright, so a revision committed to the tracked
    ``data/<profile>/tune_revisions.yaml`` afterwards (the hand-edit workflow)
    was invisible to every reader, while making the tracked file the only
    source would have dropped the dashboard-recorded flashes. Both are read;
    tracked revisions the operational copy lacks are merged in by
    ``flashed_at``, and one name recorded as two different flashes raises
    :class:`src.vehicle.tune_revisions.RegistryConflictError` naming both
    files. ``None`` when neither file exists.
    """
    from src.vehicle.tune_revisions import load_tune_revisions, merge_tune_registries

    tracked_path = source_registry_path(config)
    operational_path = operational_file(config, "tune_revisions.yaml")
    tracked = load_tune_revisions(tracked_path)
    if operational_path is None or operational_path == tracked_path:
        return tracked
    return merge_tune_registries(
        load_tune_revisions(operational_path), operational_path, tracked, tracked_path
    )
