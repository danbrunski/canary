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
for RPM — not actuation, consistent with the read-only invariant.

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
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.config import CanaryConfig, VehicleParameters

log = logging.getLogger(__name__)

REGISTRY_SCHEMA_VERSION = 1

# VINs are 17 alphanumeric characters (ISO 3779; excludes I/O/Q). Real ECU
# replies occasionally pad or truncate, so accept a forgiving length band and
# reject obviously-bogus reads rather than registering a junk profile.
_MIN_VIN_LEN = 11
_MAX_VIN_LEN = 17


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "vin": self.vin,
            "name": self.name,
            "make": self.make,
            "model": self.model,
            "year": self.year,
            "parameters": self.parameters.model_dump(),
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
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "profiles": [p.to_dict() for p in profiles],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False))
    tmp.replace(path)


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
# VIN probe
# ---------------------------------------------------------------------------


def _normalize_vin(vin: str) -> str | None:
    cleaned = "".join(ch for ch in vin if ch.isalnum()).upper()
    if _MIN_VIN_LEN <= len(cleaned) <= _MAX_VIN_LEN:
        return cleaned
    return None


def read_vin(config: CanaryConfig) -> str | None:
    """One-shot VIN read at startup. ``None`` on any failure — never raises.

    No-op (returns ``None``) when OBD is disabled, the bus is ``virtual``, or
    the source is not the CAN-native path. ELM327 VIN is out of scope for v1;
    those configs fall back to the default profile. Opens its own short-lived
    bus and closes it before the main collectors start.
    """
    coll = config.collection
    if coll.obd_source != "canable" or config.hardware.can.interface == "virtual":
        return None

    # Imported lazily so this module (and config) stay importable without the
    # python-can collector deps present.
    from src.collection.can_collector import bus_factory
    from src.collection.can_obd_client import CanObdClient
    from src.collection.obd_registry import decode_vin

    try:
        bus = bus_factory(config.hardware.can)
    except Exception:
        log.warning("VIN probe: failed to open CAN bus", exc_info=True)
        return None

    try:
        client = CanObdClient(bus, timeout_seconds=config.profiles.vin_probe_timeout_seconds)
        response = client.query_mode_09(0x02)
        if response is None:
            log.info("VIN probe: no Mode 09 response")
            return None
        decoded = decode_vin(response.payload)
        if decoded is None:
            return None
        vin = _normalize_vin(decoded[0])
        if vin is None:
            log.warning("VIN probe: implausible VIN payload %r", decoded[0])
        return vin
    except Exception:
        log.warning("VIN probe failed", exc_info=True)
        return None
    finally:
        with suppress(Exception):
            bus.shutdown()


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


def resolve_active_profile(
    config: CanaryConfig,
    *,
    override_slug: str | None = None,
) -> VehicleProfile:
    """Determine which vehicle this is and return its profile.

    * ``override_slug`` (CLI ``--profile``) → that registry entry, or a
      synthetic default under that slug; the VIN probe is skipped.
    * else read the VIN; a registry hit returns that profile; an unknown VIN is
      registered with a blank display name for the operator to fill in.
    * no VIN (disabled / virtual / probe failed) → the default profile.

    The resolved profile is persisted to the registry (when ``auto_register``)
    if not already present, so the dashboard can always show and rename it.
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

    vin = read_vin(config)
    if vin is None:
        profile = _default_profile(config)
        _maybe_persist(config, profiles, profile)
        return profile

    if vin in by_vin:
        return by_vin[vin]

    profile = _new_profile_for_vin(vin, taken=set(by_slug))
    _maybe_persist(config, profiles, profile)
    return profile


def resolve_profile_for_tooling(config: CanaryConfig, slug: str | None) -> VehicleProfile:
    """Resolve a profile for offline tools (refit, migration) WITHOUT a VIN probe
    and WITHOUT mutating the registry (so a dry-run stays read-only).

    ``--profile <slug>`` selects a registry entry (or a synthetic default under
    that slug); otherwise the ``vehicle:`` block's default profile is used.
    """
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
    index that maps VIN to slug. The slug is recoverable downstream as
    ``config.collection.storage_path.name``.
    """
    slug = profile.slug

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
    return config.model_copy(
        update={"collection": collection, "daemon": daemon, "vehicle": vehicle}
    )
