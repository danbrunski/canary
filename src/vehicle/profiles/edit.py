"""Editing, deleting and restoring a profile, and the evidence rules behind them.

A dashboard edit lands in the registry overlay; a delete is a tombstone (a
tracked profile) or a dropped overlay row (a machine-only one); a restore lifts
the tombstone. ``ProfileResolution`` and its process-wide record live here
because the rules read it (the VIN read at startup, the ``vehicle:`` block's
slug) and :mod:`src.vehicle.profiles.resolve`, which writes it, imports this
module, not the other way round.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.config import CanaryConfig, ProfilesConfig, VehicleConfig, VehicleParameters
from src.vehicle.profiles.model import (
    PARAMETER_EVIDENCE,
    VehicleProfile,
    _default_profile,
    _registry_entries,
    deleted_slugs,
    load_registry,
    parameter_unit,
    read_registry_file,
    save_registry,
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
    quoted against idle. Clearing the gearing needs no evidence; the model's
    own validator stops requiring it then.
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
