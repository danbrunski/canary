"""The profile's schema, its names, and the registry file it lives in.

``VehicleProfile`` and the parameter table the dashboard edits; ``slugify`` and
the other naming rules; and the registry's file I/O: the tracked file, this
machine's overlay merged over it by slug, and the atomic write back
(:func:`load_registry`, :func:`save_registry`). Everything else in the package
builds on this module and nothing here imports another profiles module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.atomic_write import atomic_write_text
from src.config import CanaryConfig, ProfilesConfig, VehicleParameters
from src.yaml_load import load_yaml

REGISTRY_SCHEMA_VERSION = 1

#: Vehicle facts and the evidence field that must say how each was arrived at.
#: A fact and its evidence form one *unit*: an edit to the fact needs new
#: evidence (:func:`update_profile`), and the overlay stores and reverts the
#: unit whole (:func:`_parameter_overrides`), so evidence never describes a
#: number from the other side of the merge (operator, 2026-10-10). Tire size
#: is deliberately not in a unit: tires get swapped, and a new size must save
#: without rewriting the gearing's evidence (operator, 2026-11-07). The drive's
#: own GPS measurement (:mod:`src.analysis.rolling_circumference`) checks it.
PARAMETER_EVIDENCE: dict[str, tuple[str, ...]] = {
    "mass_evidence": ("mass_kg",),
    "driveline_evidence": ("final_drive", "gearbox_ratios"),
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
    "induction": {"label": "Induction", "unit": "", "kind": "choice"},
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
    raw = load_yaml(path.read_text()) or {}
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
    raw = load_yaml(path.read_text()) or {}
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
