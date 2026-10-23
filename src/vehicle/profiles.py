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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from src.atomic_write import atomic_write_text
from src.config import CanaryConfig, ProfilesConfig, VehicleConfig, VehicleParameters

if TYPE_CHECKING:
    from src.signals.afr import FlashHistory
    from src.vehicle.tune_revisions import TuneRevisionRegistry

log = logging.getLogger(__name__)

REGISTRY_SCHEMA_VERSION = 1

#: Vehicle facts and the evidence field that must say how each was arrived at.
#: A fact and its evidence form one *unit*: an edit to the fact needs new
#: evidence (:func:`update_profile`), and the overlay stores and reverts the
#: unit whole (:func:`_parameter_overrides`), so evidence never describes a
#: number from the other side of the merge (operator, 2026-10-10).
PARAMETER_EVIDENCE: dict[str, tuple[str, ...]] = {
    "mass_evidence": ("mass_kg",),
    "driveline_evidence": ("final_drive", "gearbox_ratios", "tire_loaded_circumference_m"),
}

#: How the dashboard shows each :class:`~src.config.VehicleParameters` field:
#: label, unit (``""`` for unitless) and input kind. Every field must appear
#: (``tests/test_profile_edit.py``), so a new parameter cannot be invisible.
PARAMETER_FIELDS: dict[str, dict[str, str]] = {
    "mass_kg": {"label": "Vehicle mass", "unit": "kg", "kind": "number"},
    "mass_evidence": {"label": "Mass evidence", "unit": "", "kind": "evidence"},
    "wheel_tire_assembly_kg": {"label": "Wheel + tire assembly", "unit": "kg", "kind": "number"},
    "usual_payload_kg": {"label": "Usual payload", "unit": "kg", "kind": "number"},
    "fuel_type": {"label": "Fuel", "unit": "", "kind": "choice"},
    "displacement_l": {"label": "Displacement", "unit": "L", "kind": "number"},
    "volumetric_efficiency": {"label": "Volumetric efficiency", "unit": "", "kind": "number"},
    "drivetrain_loss_fraction": {"label": "Drivetrain loss", "unit": "", "kind": "number"},
    "thermal_efficiency": {"label": "Thermal efficiency", "unit": "", "kind": "number"},
    "tire_diameter_m": {"label": "Tire diameter", "unit": "m", "kind": "number"},
    "final_drive": {"label": "Final drive", "unit": "", "kind": "number"},
    "gearbox_ratios": {"label": "Gearbox ratios (1st first)", "unit": "", "kind": "ratios"},
    "tire_loaded_circumference_m": {
        "label": "Loaded tire circumference",
        "unit": "m",
        "kind": "number",
    },
    "driveline_evidence": {"label": "Driveline evidence", "unit": "", "kind": "evidence"},
}


def parameter_unit(name: str) -> tuple[str, ...]:
    """The parameters stored, merged and reverted together with ``name``."""
    for evidence, facts in PARAMETER_EVIDENCE.items():
        if name == evidence or name in facts:
            return (*facts, evidence)
    return (name,)


def parameter_schema() -> list[dict[str, Any]]:
    """Every vehicle parameter with its label, unit, kind, choices and evidence unit."""
    rows: list[dict[str, Any]] = []
    for name, info in VehicleParameters.model_fields.items():
        meta = PARAMETER_FIELDS[name]
        row: dict[str, Any] = {"name": name, **meta}
        args = getattr(info.annotation, "__args__", ())
        if meta["kind"] == "choice":
            row["choices"] = list(args)
        evidence = next((ev for ev, facts in PARAMETER_EVIDENCE.items() if name in facts), None)
        row["evidence_field"] = evidence
        row["evidence_for"] = list(PARAMETER_EVIDENCE.get(name, ()))
        rows.append(row)
    return rows


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


def _registry_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    entries = raw.get("profiles", []) if isinstance(raw, dict) else []
    return [_current_spelling(dict(entry)) for entry in entries]


def _current_spelling(entry: dict[str, Any]) -> dict[str, Any]:
    """One registry entry with pre-2026-10-11 parameter names renamed.

    Done per file, before the tracked file and the overlay merge: a tracked
    ``tyre_loaded_circumference_m`` beside an overlay's
    ``tire_loaded_circumference_m`` would otherwise reach
    :class:`~src.config.VehicleParameters` as both, which it refuses.
    """
    params = entry.get("parameters")
    if isinstance(params, dict) and "tyre_loaded_circumference_m" in params:
        params = dict(params)
        value = params.pop("tyre_loaded_circumference_m")
        params.setdefault("tire_loaded_circumference_m", value)
        entry["parameters"] = params
    return entry


def read_registry_file(path: Path) -> list[VehicleProfile]:
    """One registry file as written, with no overlay. A missing file is empty.

    For reading the tracked registry's own content (its vehicle parameters);
    the daemon's view of its vehicles is :func:`load_registry`.
    """
    return [VehicleProfile.from_dict(entry) for entry in _registry_entries(path)]


def _merge_entry(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """One slug's overlay entry over its tracked entry.

    Top-level fields replace; ``parameters`` merges field by field (the
    overlay stores only the parameters this machine changed, evidence units
    whole: :func:`_parameter_overrides`). Merging the block as one value let a
    single dashboard edit shadow every tracked parameter, so a later laptop
    edit to any other one stopped reaching the Pi (2026-10-10).
    """
    merged = {**base, **overlay}
    if isinstance(base.get("parameters"), dict) and isinstance(overlay.get("parameters"), dict):
        merged["parameters"] = {**base["parameters"], **overlay["parameters"]}
    return merged


def deleted_slugs(profiles: ProfilesConfig) -> list[str]:
    """The slugs this machine's overlay hides (its ``deleted:`` list), sorted.

    Only a *tracked* profile needs one: the tracked registry is never written at
    runtime, so "delete" there is a per-machine tombstone (:func:`delete_profile`).
    A machine-only profile is deleted by removing its overlay row.
    """
    path = profiles.registry_overlay_path
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    listed = raw.get("deleted", []) if isinstance(raw, dict) else []
    return sorted({str(slug) for slug in listed or []})


def load_registry(
    profiles: ProfilesConfig, *, include_deleted: bool = False
) -> list[VehicleProfile]:
    """The VIN → profile registry: the tracked file with this machine's overlay
    merged over it by slug (S03.2).

    The tracked ``registry_path`` holds the laptop-authored vehicle facts and is
    never written at runtime. ``registry_overlay_path`` holds what this machine
    learned or was told: per tracked slug, only the fields that differ (a
    display name, a VIN, the calibration ID / CVN, a vehicle parameter edited in
    the dashboard); a vehicle the tracked registry does not know, whole.
    ``parameters`` merges field by field (:func:`_merge_entry`), so a later
    laptop edit to a vehicle's parameters still takes effect here unless this
    machine has changed that same parameter, or another in its evidence unit.

    A tracked profile this machine deleted (:func:`deleted_slugs`) is left out
    unless ``include_deleted``. Only profile *resolution* and the VIN
    uniqueness check ask for it: they must see a hidden profile to restore it
    or refuse to duplicate its VIN, never to run on it.
    """
    merged = {entry["slug"]: entry for entry in _registry_entries(profiles.registry_path)}
    hidden = set() if include_deleted else set(deleted_slugs(profiles)) & merged.keys()
    for entry in _registry_entries(profiles.registry_overlay_path):
        slug = entry["slug"]
        merged[slug] = _merge_entry(merged.get(slug, {}), entry)
    return [VehicleProfile.from_dict(e) for slug, e in merged.items() if slug not in hidden]


def _parameter_overrides(base: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """The parameters ``current`` changes from ``base``, each evidence unit whole.

    A fact and the evidence for it travel as one unit (:data:`PARAMETER_EVIDENCE`):
    an overlay holding a dashboard-entered ``mass_kg`` with the *tracked*
    ``mass_evidence`` would pair the machine's number with whatever text the
    laptop commits next, describing a different mass.
    """
    changed = {name for name, value in current.items() if base.get(name) != value}
    keep: set[str] = set()
    for name in changed:
        keep.update(parameter_unit(name))
    return {name: value for name, value in current.items() if name in keep}


def save_registry(
    profiles: ProfilesConfig,
    entries: list[VehicleProfile],
    *,
    deleted: Iterable[str] | None = None,
) -> None:
    """Persist ``entries`` as the overlay over the tracked registry (atomic).

    Stores only what differs from the tracked registry (see
    :func:`load_registry`); the tracked file is never written.

    ``deleted`` is the overlay's tombstone list (:func:`deleted_slugs`). ``None``
    keeps the overlay's existing list: every rename, edit, revert, auto-register
    and calibration-probe save passes the *filtered* entry list, and none of
    them may un-delete a profile by omission. Only :func:`delete_profile` and
    :func:`restore_profile` pass a list.
    """
    tracked = {p.slug: p.to_dict() for p in read_registry_file(profiles.registry_path)}
    overlay: list[dict[str, Any]] = []
    for profile in entries:
        current = profile.to_dict()
        base = tracked.get(profile.slug)
        if base is None:
            overlay.append(current)
            continue
        changed: dict[str, Any] = {
            key: value
            for key, value in current.items()
            if key != "parameters" and base.get(key) != value
        }
        parameters = _parameter_overrides(base["parameters"], current["parameters"])
        if parameters:
            changed["parameters"] = parameters
        if changed:
            overlay.append({"slug": profile.slug, **changed})
    payload: dict[str, Any] = {"schema_version": REGISTRY_SCHEMA_VERSION, "profiles": overlay}
    kept = sorted({str(slug) for slug in (deleted_slugs(profiles) if deleted is None else deleted)})
    if kept:
        payload["deleted"] = kept
    atomic_write_text(
        profiles.registry_overlay_path, yaml.safe_dump(payload, sort_keys=False), mkdir=True
    )


def update_profile_name(profiles: ProfilesConfig, slug: str, name: str) -> VehicleProfile:
    """Set a profile's display name in the registry; return the updated profile.

    Raises ``KeyError`` if no profile has that slug. Renaming touches only the
    ``name`` field — the slug and every path stays put.
    """
    registry = load_registry(profiles)
    for profile in registry:
        if profile.slug == slug:
            profile.name = name.strip()
            save_registry(profiles, registry)
            return profile
    raise KeyError(slug)


# ---------------------------------------------------------------------------
# Dashboard editing (operator request 2026-10-10)
# ---------------------------------------------------------------------------
#
# An edit lands in the overlay like a rename. It reaches the daemon at its
# next start: ``resolve_active_profile`` + ``scope_config`` copy the profile
# into the config every component holds, and each session manifest records
# that copy. Nothing re-reads it mid-run, so a running session's derived
# channels and manifest keep the values it started with.

#: Top-level fields the dashboard edits besides ``parameters``.
EDITABLE_FIELDS: tuple[str, ...] = ("name", "make", "model", "year", "vin")
#: Revertable to the tracked value. ``vin``, ``calibration_id`` and ``cvn``
#: are what this machine read off the vehicle; the tracked file usually has
#: none, and reverting a VIN would unhook the vehicle at the next start.
REVERTABLE_FIELDS: tuple[str, ...] = ("name", "make", "model", "year")
#: Bounds on the identity fields (the year: the first production car to a
#: model year two ahead).
MAX_TEXT_LENGTH = 64
YEAR_RANGE = (1886, 2100)


class ProfileEditError(ValueError):
    """An edit refused; ``field`` names the field that refused it."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field

    def __reduce__(self) -> tuple[object, ...]:
        # Rebuilt from its constructor's arguments, so it survives a process
        # boundary (pickle); tests/test_exceptions_pickle.py holds every class to it.
        return (type(self), (self.field, str(self)), self.__dict__)


class EvidenceRequiredError(ProfileEditError):
    """A vehicle fact changed without new evidence for it."""


class DuplicateVinError(ProfileEditError):
    """The VIN already identifies another profile."""


class ActiveVinMismatchError(ProfileEditError):
    """The active profile's VIN changed away from the VIN read at startup."""


class NoTrackedProfileError(ProfileEditError):
    """A revert on a profile the tracked registry does not hold."""


class ProfileDeleteRefusedError(ProfileEditError):
    """A delete that would pull a profile out from under the daemon or recreate it.

    ``str(error)`` is the named reason; the listing shows it beside the
    disabled Delete button (:func:`delete_refusal`).
    """

    def __init__(self, slug: str, reason: str) -> None:
        super().__init__("slug", reason)
        self.slug = slug

    def __reduce__(self) -> tuple[object, ...]:
        return (type(self), (self.slug, str(self)), self.__dict__)


@dataclass(frozen=True)
class ProfileResolution:
    """How this process chose its active profile, for the edit rules and the UI.

    ``vin_read`` is the VIN the startup probe actually read off the vehicle,
    not the one the profile stores; ``None`` when nothing was read.
    """

    slug: str
    how: str
    vin_read: str | None
    #: The slug the ``vehicle:`` block of ``canary.yaml`` names, read before
    #: :func:`scope_config` overwrote that block with the profile's own. A scoped
    #: config can no longer say it, and :func:`delete_refusal` needs it. Not in
    #: :meth:`to_dict`: it is bookkeeping for the edit rules, not part of the answer
    #: to "how was this profile chosen".
    fallback_slug: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"slug": self.slug, "how": self.how, "vin_read": self.vin_read}


_resolution: ProfileResolution | None = None


def last_resolution() -> ProfileResolution | None:
    """What :func:`resolve_active_profile` decided in this process, if it ran."""
    return _resolution


def record_resolution(resolution: ProfileResolution | None) -> None:
    """Set (or, with ``None``, clear) the process's resolution record."""
    global _resolution
    _resolution = resolution


@dataclass(frozen=True)
class ProfileEdit:
    """A saved edit or revert: the merged profile and what changed."""

    profile: VehicleProfile
    #: Field names changed, ``parameters.<name>`` for parameters.
    changed: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    #: What the next start does with this profile's VIN, when it was edited.
    vin_resolution: str | None = None


def _find(registry: list[VehicleProfile], slug: str) -> VehicleProfile:
    for profile in registry:
        if profile.slug == slug:
            return profile
    raise KeyError(slug)


def _text(field_name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ProfileEditError(field_name, f"{field_name} must be text")
    text = value.strip()
    limit = 255 if field_name == "name" else MAX_TEXT_LENGTH
    if len(text) > limit:
        raise ProfileEditError(field_name, f"{field_name} is longer than {limit} characters")
    return text


def _year(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileEditError("year", "year must be a whole number")
    low, high = YEAR_RANGE
    if not low <= value <= high:
        raise ProfileEditError("year", f"year {value} is outside {low}..{high}")
    return int(value)


def _validated_parameters(merged: dict[str, Any]) -> VehicleParameters:
    from pydantic import ValidationError

    try:
        return VehicleParameters(**merged)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first["loc"]) or "parameters"
        raise ProfileEditError(
            f"parameters.{loc}" if first["loc"] else "parameters",
            f"parameters.{loc}: {first['msg']}" if first["loc"] else str(first["msg"]),
        ) from exc
    except ValueError as exc:  # a before-validator's plain refusal
        raise ProfileEditError("parameters", str(exc)) from exc


_EVIDENCE_EXAMPLES = {
    "mass_evidence": "scale ticket 2026-10-10, CAT scale, full fuel, no occupants",
    "driveline_evidence": "final drive from the axle tag; ratios from the gearbox manual",
}


def _evidence_key(text: str | None) -> str:
    """Evidence text as compared for "new": whitespace collapsed, case folded,
    so a trailing space or a capital letter is not new evidence."""
    return " ".join((text or "").split()).casefold()


def _require_evidence(before: VehicleParameters, after: VehicleParameters) -> None:
    """Refuse a changed vehicle fact that does not come with new evidence for it.

    A constraint, not advice: the dyno and the derived channels quote these
    numbers, and a figure nobody can trace is how the WOT noise floor was once
    quoted against idle. Clearing every driveline fact needs no evidence; the
    model's own validator stops requiring it then.
    """
    for evidence, facts in PARAMETER_EVIDENCE.items():
        moved = [name for name in facts if getattr(before, name) != getattr(after, name)]
        if not moved:
            continue
        if evidence == "driveline_evidence" and not any(getattr(after, f) for f in facts):
            continue
        text = _evidence_key(getattr(after, evidence))
        if text and text != _evidence_key(getattr(before, evidence)):
            continue
        changes = ", ".join(
            f"{name} {getattr(before, name)!r} -> {getattr(after, name)!r}" for name in moved
        )
        raise EvidenceRequiredError(
            evidence,
            f"{changes}: changing it needs a new {evidence} saying how the new value was "
            f"arrived at (for example {_EVIDENCE_EXAMPLES[evidence]!r})",
        )


def _vin_resolution_note(vin: str, slug: str, active_slug: str | None) -> str:
    """What the next daemon start does with ``vin`` on profile ``slug``."""
    resolution = last_resolution()
    read = resolution.vin_read if resolution is not None else None
    if not vin:
        return (
            "No VIN: the next start never selects this profile by VIN; it is used only "
            "as the fallback when no VIN can be read, if its slug is the vehicle: block's."
        )
    if read is not None and vin == read:
        if slug == active_slug:
            return f"The connected vehicle reported {vin} at this start: it stays active."
        return (
            f"The connected vehicle reported {vin} at this start: the next start selects "
            "this profile."
        )
    return (
        f"The next start selects this profile only if the vehicle reports {vin}. A vehicle "
        "reporting another VIN gets that VIN's profile, or a new blank one; no VIN read "
        "falls back to the vehicle: block's slug. Nothing changes until the daemon restarts."
    )


def _apply_vin(
    registry: list[VehicleProfile],
    profile: VehicleProfile,
    value: Any,
    *,
    active_slug: str | None,
    hidden: frozenset[str] = frozenset(),
) -> tuple[str, tuple[str, ...]]:
    """Validate a new VIN for ``profile``; the VIN and any warning.

    ``registry`` must include the profiles this machine hides: one VIN keys one
    profile, and the next start would restore a hidden holder (``hidden`` names
    them in the refusal).
    """
    from src.vehicle.vin import InvalidVinError, normalize_vin, validate_vin

    if not isinstance(value, str):
        raise ProfileEditError("vin", "vin must be text")
    warnings: tuple[str, ...] = ()
    vin = normalize_vin(value)
    if vin:
        try:
            check = validate_vin(vin)
        except InvalidVinError as exc:
            raise ProfileEditError("vin", str(exc)) from exc
        if check.warning:
            warnings = (check.warning,)
        holder = next((p for p in registry if p.vin == vin and p.slug != profile.slug), None)
        if holder is not None:
            raise DuplicateVinError(
                "vin",
                f"VIN {vin} already identifies profile {holder.slug!r}"
                f"{f' ({holder.name})' if holder.name else ''}"
                f"{' (hidden on this machine)' if holder.slug in hidden else ''}; "
                "one VIN keys one profile",
            )
    resolution = last_resolution()
    read = resolution.vin_read if resolution is not None else None
    if profile.slug == active_slug and read is not None and vin != read and vin != profile.vin:
        raise ActiveVinMismatchError(
            "vin",
            f"this daemon read VIN {read} off the connected vehicle at startup and resolved "
            f"it to this profile; giving the profile {vin or 'no VIN'} would make the next "
            f"start find no profile for {read} and register a new, empty one. Change the VIN "
            "of a profile that is not active, or connect the vehicle that carries it",
        )
    return vin, warnings


def update_profile(
    profiles: ProfilesConfig,
    slug: str,
    changes: dict[str, Any],
    *,
    active_slug: str | None = None,
) -> ProfileEdit:
    """Apply a dashboard edit to one profile and persist it to the overlay.

    ``changes`` holds only the fields being edited: any of
    :data:`EDITABLE_FIELDS`, and ``parameters`` as a partial mapping merged
    over the profile's current parameters and validated through
    :class:`~src.config.VehicleParameters` (an out-of-range value is refused
    naming the field). A changed vehicle fact needs new evidence
    (:func:`_require_evidence`); a VIN must be well formed, unique, and, on
    the active profile, the one read at startup (:func:`_apply_vin`).

    Raises ``KeyError`` for an unknown slug and :class:`ProfileEditError`
    (with ``field``) for a refused edit; nothing is written then.
    """
    unknown = sorted(set(changes) - {*EDITABLE_FIELDS, "parameters"})
    if unknown:
        raise ProfileEditError(unknown[0], f"not an editable profile field: {', '.join(unknown)}")
    registry = load_registry(profiles)
    profile = _find(registry, slug)
    before = profile.to_dict()
    warnings: tuple[str, ...] = ()
    vin_note: str | None = None

    for name in ("name", "make", "model"):
        if name in changes:
            setattr(profile, name, _text(name, changes[name]))
    if "year" in changes:
        profile.year = _year(changes["year"])
    if "vin" in changes:
        profile.vin, warnings = _apply_vin(
            load_registry(profiles, include_deleted=True),
            profile,
            changes["vin"],
            active_slug=active_slug,
            hidden=frozenset(_hidden_slugs(profiles)),
        )
        vin_note = _vin_resolution_note(profile.vin, slug, active_slug)
    if "parameters" in changes:
        edits = changes["parameters"]
        if not isinstance(edits, dict):
            raise ProfileEditError("parameters", "parameters must be a mapping of field to value")
        merged = {**profile.parameters.model_dump(), **edits}
        after = _validated_parameters(merged)
        _require_evidence(profile.parameters, after)
        profile.parameters = after

    current = profile.to_dict()
    changed = [k for k in EDITABLE_FIELDS if before[k] != current[k]]
    changed += [
        f"parameters.{k}"
        for k, v in current["parameters"].items()
        if before["parameters"].get(k) != v
    ]
    if changed:
        save_registry(profiles, registry)
    return ProfileEdit(
        profile=profile, changed=tuple(changed), warnings=warnings, vin_resolution=vin_note
    )


def profile_overrides(profiles: ProfilesConfig) -> dict[str, dict[str, Any]]:
    """Per slug: whether the tracked registry holds it, and which fields this
    machine's overlay sets over it (``parameters.<name>`` for parameters).

    A profile only this machine knows (``tracked: false``) is entirely its own;
    its ``fields`` list is empty because there is nothing to revert to.
    """
    tracked = {entry["slug"] for entry in _registry_entries(profiles.registry_path)}
    result: dict[str, dict[str, Any]] = {slug: {"tracked": True, "fields": []} for slug in tracked}
    for entry in _registry_entries(profiles.registry_overlay_path):
        slug = entry["slug"]
        if slug not in tracked:
            result[slug] = {"tracked": False, "fields": []}
            continue
        fields = [key for key in entry if key not in ("slug", "parameters")]
        parameters = entry.get("parameters")
        if isinstance(parameters, dict):
            fields += [f"parameters.{name}" for name in parameters]
        result[slug] = {"tracked": True, "fields": fields}
    return result


def revert_profile(
    profiles: ProfilesConfig, slug: str, fields: list[str] | None = None
) -> ProfileEdit:
    """Put ``fields`` of one profile back to the tracked registry's values.

    ``None`` reverts every override the dashboard can make (identity fields
    and parameters), keeping the VIN, calibration ID and CVN this machine read
    off the vehicle. A parameter reverts with its evidence unit, so a tracked
    number never ends up beside the machine's evidence text. Mirrors
    ``POST /control/capture/profiles/{name}/revert``.
    """
    base = next((p for p in read_registry_file(profiles.registry_path) if p.slug == slug), None)
    registry = load_registry(profiles)
    profile = _find(registry, slug)
    if base is None:
        raise NoTrackedProfileError(
            "slug",
            f"profile {slug!r} exists only on this machine; there is no tracked value to revert to",
        )
    if fields is None:
        overridden = profile_overrides(profiles).get(slug, {"fields": []})["fields"]
        fields = [f for f in overridden if f in REVERTABLE_FIELDS or f.startswith("parameters.")]
    before = profile.to_dict()
    parameters = profile.parameters.model_dump()
    tracked_parameters = base.parameters.model_dump()
    for name in fields:
        if name in REVERTABLE_FIELDS:
            setattr(profile, name, getattr(base, name))
        elif name.startswith("parameters.") and name[11:] in tracked_parameters:
            for member in parameter_unit(name[11:]):
                parameters[member] = tracked_parameters[member]
        else:
            raise ProfileEditError(
                name,
                f"{name} cannot be reverted: revert takes {', '.join(REVERTABLE_FIELDS)} and "
                "parameters.<name>; the VIN, calibration ID and CVN are what this machine "
                "read off the vehicle",
            )
    profile.parameters = _validated_parameters(parameters)
    current = profile.to_dict()
    changed = [k for k in EDITABLE_FIELDS if before[k] != current[k]]
    changed += [
        f"parameters.{k}"
        for k, v in current["parameters"].items()
        if before["parameters"].get(k) != v
    ]
    save_registry(profiles, registry)
    return ProfileEdit(profile=profile, changed=tuple(changed))


# ---------------------------------------------------------------------------
# Deleting a profile (operator request 2026-10-23)
# ---------------------------------------------------------------------------
#
# "Delete a profile from git" cannot write git: the tracked registry is never
# written at runtime (S03). A tracked profile is hidden on this machine by a
# tombstone in the overlay; a profile only this machine knows loses its overlay
# row. Recorded data (sessions, models, ledgers) is never deleted or moved:
# :func:`profile_data_paths` names where it stays.


@dataclass(frozen=True)
class ProfileDeletion:
    """A saved delete: the slug, and whether it was a tracked profile (hidden
    here, still in git) or one only this machine knew (gone from its registry)."""

    slug: str
    tracked: bool


def vehicle_block_slug(config: CanaryConfig) -> str:
    """The slug ``canary.yaml``'s ``vehicle:`` block names (the no-VIN fallback).

    Read from this process's resolution record when there is one: a scoped
    config's ``vehicle`` block is the active profile's own, so deriving it from
    that would name the active profile whatever the file says. Without a record
    (a process that never resolved) the config's block is all there is.
    """
    resolution = last_resolution()
    if resolution is not None and resolution.fallback_slug is not None:
        return resolution.fallback_slug
    return _default_profile(config).slug


def delete_refusal(slug: str, *, active_slug: str | None, fallback_slug: str | None) -> str | None:
    """Why ``slug`` cannot be deleted now, or ``None``.

    The active profile is what the running daemon's paths and manifests are
    built from. The ``vehicle:`` block's slug is what a start that reads no VIN
    falls back to (:func:`_registered_default`), registering it again if it is
    missing, so deleting it would only last until the next key-off start.
    """
    if slug == active_slug:
        return (
            "the daemon is running on it (the active profile); restart it on another "
            "profile first (connect that vehicle, or change canary.yaml's vehicle: block)"
        )
    if slug == fallback_slug:
        return (
            "canary.yaml's vehicle: block (make/model/year) names this profile, so a "
            "start that reads no VIN would recreate it; change the vehicle: block first"
        )
    return None


def delete_profile(
    profiles: ProfilesConfig,
    slug: str,
    *,
    active_slug: str | None,
    fallback_slug: str | None,
) -> ProfileDeletion:
    """Remove ``slug`` from this machine's registry (data is left where it is).

    A machine-only profile loses its overlay row. A tracked one loses its
    overlay row too (this machine's edits to it are dropped) and gains a
    tombstone, so it is hidden here while staying in git for other machines;
    :func:`restore_profile` brings back the tracked profile.

    Raises ``KeyError`` for a slug the registry does not show and
    :class:`ProfileDeleteRefusedError` (the reason) for the active profile and
    the ``vehicle:`` block's; nothing is written then.
    """
    registry = load_registry(profiles)
    _find(registry, slug)
    reason = delete_refusal(slug, active_slug=active_slug, fallback_slug=fallback_slug)
    if reason is not None:
        raise ProfileDeleteRefusedError(slug, reason)
    tracked = any(entry["slug"] == slug for entry in _registry_entries(profiles.registry_path))
    tombstones = [*deleted_slugs(profiles), slug] if tracked else None
    save_registry(profiles, [p for p in registry if p.slug != slug], deleted=tombstones)
    return ProfileDeletion(slug=slug, tracked=tracked)


def _hidden_slugs(profiles: ProfilesConfig) -> set[str]:
    """Tombstones that hide a profile the tracked registry holds."""
    tracked = {entry["slug"] for entry in _registry_entries(profiles.registry_path)}
    return set(deleted_slugs(profiles)) & tracked


def deleted_profiles(profiles: ProfilesConfig) -> list[VehicleProfile]:
    """The tracked profiles this machine currently hides, as the tracked file has them."""
    hidden = _hidden_slugs(profiles)
    return [p for p in load_registry(profiles, include_deleted=True) if p.slug in hidden]


def restore_profile(profiles: ProfilesConfig, slug: str) -> VehicleProfile:
    """Un-hide a deleted tracked profile; it returns exactly as the tracked file
    has it (the edits it had were dropped by :func:`delete_profile`).

    Raises ``KeyError`` when ``slug`` is not currently hidden.
    """
    if slug not in _hidden_slugs(profiles):
        raise KeyError(slug)
    save_registry(
        profiles,
        load_registry(profiles),
        deleted=[s for s in deleted_slugs(profiles) if s != slug],
    )
    return _find(load_registry(profiles), slug)


def pending_restart(in_use: VehicleConfig, profile: VehicleProfile) -> list[str]:
    """Fields where the registry's ``profile`` differs from what the daemon runs.

    ``in_use`` is the running config's ``vehicle`` block, copied from the
    profile at startup. Each field listed reaches the daemon (derived
    channels, the dyno, tire wear, new session manifests) only after a
    restart; offline tools that resolve the profile themselves already see
    the registry's value, so a non-empty list is two values in use at once.
    """
    differs: list[str] = []
    year = profile.year if profile.year is not None else 0
    for name, registry_value, running in (
        ("make", profile.make, in_use.make),
        ("model", profile.model, in_use.model),
        ("year", year, in_use.year),
        ("vin", profile.vin, in_use.vin),
    ):
        if registry_value != running:
            differs.append(name)
    stored = profile.parameters.model_dump()
    running_parameters = in_use.parameters.model_dump()
    differs += [f"parameters.{k}" for k, v in stored.items() if running_parameters.get(k) != v]
    return differs


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
    save_registry(config.profiles, profiles)
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
    before = (profile.calibration_id, profile.cvn)
    if probe.calibration_id:
        profile.calibration_id = probe.calibration_id
    if probe.cvn:
        profile.cvn = probe.cvn
    if (profile.calibration_id, profile.cvn) != before:
        save_registry(config.profiles, profiles)
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


def _restore_if_hidden(
    config: CanaryConfig,
    profiles: list[VehicleProfile],
    profile: VehicleProfile,
    *,
    how: str,
) -> None:
    """Un-delete ``profile`` when this machine hid it, and add it to ``profiles``.

    A start that resolves to a deleted profile (its VIN read, or the
    ``vehicle:`` block naming it, or ``--profile``) is the daemon running on it
    whatever the registry says, so the registry is made to say so. ``how`` is
    the log line's account of why.
    """
    if profile.slug not in _hidden_slugs(config.profiles):
        return
    restore_profile(config.profiles, profile.slug)
    profiles.append(profile)
    log.warning(
        "this start resolved to vehicle profile %r, which was deleted on this machine "
        "(%s), so it was restored",
        profile.slug,
        how,
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
    * no VIN (disabled / virtual / probe failed) → the default profile: the
      registry entry under the ``vehicle:`` block's slug, else one built from it.

    The resolved profile is persisted to the registry (when ``auto_register``)
    if not already present, so the dashboard can always show and rename it.
    A registered profile's calibration ID/CVN (Task 3.4) is refreshed from the
    same probe; a changed calibration ID is logged as a corpus-boundary event.

    Matching looks at every profile, including the tracked ones this machine
    deleted: resolution never runs on a hidden profile, so one that matches is
    restored (:func:`_restore_if_hidden`) rather than skipped, which would
    register a duplicate of the vehicle under a new slug.
    """
    registry = load_registry(config.profiles, include_deleted=True)
    hidden = _hidden_slugs(config.profiles)
    profiles = [p for p in registry if p.slug not in hidden]
    by_slug = {p.slug: p for p in registry}
    by_vin = {p.vin: p for p in registry if p.vin}
    fallback = _default_profile(config).slug

    if override_slug is not None:
        record_resolution(ProfileResolution(override_slug, "override", None, fallback))
        if override_slug in by_slug:
            profile = by_slug[override_slug]
            _restore_if_hidden(config, profiles, profile, how="the --profile override named it")
            return profile
        profile = _default_profile(config, slug=override_slug)
        _maybe_persist(config, profiles, profile)
        return profile

    if not config.profiles.enabled:
        profile = _default_profile(config)
        record_resolution(ProfileResolution(profile.slug, "profiles_disabled", None, fallback))
        return profile

    vin_probe = probe(config)
    vin = vin_probe.vin
    if vin is None:
        profile = _registered_default(config, by_slug)
        record_resolution(ProfileResolution(profile.slug, "no_vin_read", None, fallback))
        _restore_if_hidden(
            config, profiles, profile, how="no VIN was read and the vehicle: block names it"
        )
        _maybe_persist(config, profiles, profile)
        return profile

    if vin in by_vin:
        profile = by_vin[vin]
        record_resolution(ProfileResolution(profile.slug, "vin_matched", vin, fallback))
        _restore_if_hidden(config, profiles, profile, how=f"the vehicle reported its VIN {vin}")
    else:
        profile = _new_profile_for_vin(vin, taken=set(by_slug))
        record_resolution(ProfileResolution(profile.slug, "vin_registered", vin, fallback))
        _maybe_persist(config, profiles, profile)

    # Only touch calibration bookkeeping for a profile that's actually in the
    # persisted registry — `_maybe_persist` above is a no-op when
    # `auto_register` is off, and an unregistered profile has no registry row
    # to update (mirrors that gate rather than silently rewriting the file).
    if any(p.slug == profile.slug for p in profiles):
        _apply_calibration_probe(config, profiles, profile, vin_probe)
    return profile


def _registered_default(config: CanaryConfig, by_slug: dict[str, VehicleProfile]) -> VehicleProfile:
    """The fallback profile: the registry's entry under the ``vehicle:`` block's
    slug when there is one, else one built from that block.

    Until 2026-10-10 the fallback was always built from the block, so a start
    that read no VIN (key off, parked, bench) ran on ``canary.yaml``'s vehicle
    parameters, often the code defaults, while the dashboard showed the
    registry's: a dashboard edit had no effect, and the dyno added payload to a
    mass other than the one it displayed.
    """
    default = _default_profile(config)
    return by_slug.get(default.slug, default)


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
    scoped = scope_config(config, profile)
    try:
        migrate_legacy_calibration(scoped)
    except (OSError, ValueError) as exc:
        log.warning("legacy calibration migration failed: %s", exc)
    try:
        seed_machine_owned_files(scoped)
    except OSError as exc:
        log.warning("seeding machine-owned files failed: %s", exc)
    return profile


#: Files a machine owns once it has them, seeded from the tracked profile
#: directory the first time (S03.3). ``behavior_models.yaml``: the Pi's refit
#: job rewrites it, so it is a trained artifact, Pi-owned (operator decision
#: 2026-10-04); the tracked copy is only where a fresh install starts.
SEEDED_FROM_PROFILE: tuple[str, ...] = ("behavior_models",)


def seed_machine_owned_files(config: CanaryConfig) -> list[Path]:
    """Copy each :data:`SEEDED_FROM_PROFILE` file from the tracked profile
    directory to its configured location when that is somewhere else and empty.

    Never overwrites: after the first copy the machine's file is authoritative,
    and a laptop-trained replacement arrives the way models do (upload or
    ``scp``), not through git. Returns the files written.
    """
    written: list[Path] = []
    for name in SEEDED_FROM_PROFILE:
        target: Path = getattr(config.daemon, name)
        source = profile_dir(config) / target.name
        if target.exists() or not source.exists() or target.resolve() == source.resolve():
            continue
        atomic_write_text(target, source.read_text(), mkdir=True)
        log.info(
            "seeded %s from the tracked profile",
            name,
            extra={"source": str(source), "target": str(target)},
        )
        written.append(target)
    return written


def resolve_profile_for_tooling(config: CanaryConfig, slug: str | None) -> VehicleProfile:
    """Resolve a profile for offline tools (refit, migration) WITHOUT a VIN probe
    and WITHOUT mutating the registry (so a dry-run stays read-only).

    ``--profile <slug>`` selects a registry entry (or a synthetic default under
    that slug); otherwise the ``vehicle:`` block's default profile, as the
    registry holds it when it does (:func:`_registered_default`, the daemon's
    own fallback). A
    config already scoped (P02.11) resolves to the profile it was scoped to, so
    re-resolving downstream of an entry point's ``scope_config`` is a no-op.
    """
    # Hidden profiles are read too, never restored (a dry run stays read-only):
    # their data is still on disk, and the daemon restores one at the start
    # that resolves to it, so this is the profile that start would use.
    registry = load_registry(config.profiles, include_deleted=True)
    slug = slug if slug is not None else config.scoped_profile
    if slug is not None:
        for profile in registry:
            if profile.slug == slug:
                return profile
        return _default_profile(config, slug=slug)
    if not config.profiles.enabled:
        return _default_profile(config)
    return _registered_default(config, {p.slug: p for p in registry})


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


#: ``active_flash_history`` by the identity of the files it read. The live path
#: featurizes every tick, and parsing the registry costs ~2.5 ms on the laptop.
_flash_history_cache: dict[tuple[tuple[str, int], ...], Any] = {}


def active_flash_history(config: CanaryConfig) -> FlashHistory:
    """The flash history ``derived:TARGET_AFR`` resolves each row's revision
    from (P02.16): :func:`active_registry`, or a :class:`NoFlashHistory`
    naming why there is none.

    One function for the live tick and every offline frame, since both run
    ``feature_transform.append_derived_channels``. The live rows are stamped
    "now", so they resolve to the revision currently flashed, by the same rule
    (``TuneRevisionRegistry.at``) ``/control/calibration`` reports as
    ``running_revision``. Re-read when either registry file changes.
    """
    from src.signals.afr import NoFlashHistory
    from src.vehicle.tune_revisions import RegistryConflictError

    sources = active_registry_sources(config)
    try:
        key = tuple((str(path), path.stat().st_mtime_ns) for path in sources)
    except OSError:
        key = ()
    cached = _flash_history_cache.get(key) if key else None
    if cached is not None:
        return cast("FlashHistory", cached)
    history: FlashHistory
    try:
        registry = active_registry(config)
    except (RegistryConflictError, OSError, ValueError, yaml.YAMLError) as exc:
        history = NoFlashHistory(f"the vehicle's flash history could not be read: {exc}")
    else:
        missing = source_registry_path(config)
        history = (
            NoFlashHistory(f"no tune_revisions.yaml is recorded for this vehicle ({missing})")
            if registry is None
            else registry
        )
    if key:
        _flash_history_cache.clear()
        _flash_history_cache[key] = history
    return history
