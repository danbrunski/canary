"""Where capture profiles live and how they layer (R23.12).

The tracked catalog, this machine's overrides, :func:`layer_catalog`, load and
save, and the seed a vehicle starts from. Split out of ``capture_profiles``,
which re-exports every public name here. The schema is
``capture_profile_model``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import Field, model_validator

from src.atomic_write import atomic_write_text
from src.collection.capture_profile_model import (
    DEFAULT_PROFILE_NAME,
    SCHEMA_VERSION,
    CaptureProfile,
    CaptureProfileCatalog,
    _Strict,
    profile_digest,
)
from src.config import CollectionConfig, DaemonConfig
from src.yaml_load import load_yaml

# Named, not ``__name__``: see ``capture_profile_model``.
log = logging.getLogger("src.collection.capture_profiles")

#: The catalog a vehicle starts from when it has no tracked one. Only
#: ``default`` is built in code, because it mirrors the running collection
#: config; every other profile is data. It stands in, in memory, for the missing
#: tracked catalog; an operator's edits and deletions are this machine's
#: overrides layered over it (S03.4), so they are final until reverted.
STARTER_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "capture_profiles.yaml"


def default_profile(collection: CollectionConfig) -> CaptureProfile:
    """The reserved ``default`` profile; it mirrors the active config exactly."""
    default_channels = {name: 1.0 for name in collection.obd_pids}
    return CaptureProfile(
        name=DEFAULT_PROFILE_NAME,
        description="Normal telemetry and anomaly-detection collection",
        notes=(
            "ECM_COOLANT_TEMP is present so an ordinary drive settles the "
            "contested DID 0x1101 offset, which no capture on hand can: the "
            "two recorded offsets differ by exactly 10 degC, and separating "
            "them needs this DID and Mode 01 PID 0x05 in ONE session. Only "
            "the default profile can do that \u2014 the tuning profile "
            "disables Mode 01. Verified 2026-08-06 that 0x1101 was in the "
            "scheduler plan but never requested, because the DID window "
            "polls this list, not the plan: four consecutive windows sent 0 "
            "requests for it while ECM_RPM got one each."
        ),
        mode01=default_channels,
        mode22=tuple(collection.obd_dids),
        corpus_eligible=True,
    )


def seed_catalog(
    collection: CollectionConfig, starter_path: Path = STARTER_CATALOG_PATH
) -> CaptureProfileCatalog:
    """A new vehicle's catalog: ``default`` from config plus the starter profiles.

    The starters are data (``STARTER_CATALOG_PATH``), not code. A missing or
    unreadable starter file yields ``default`` alone rather than failing: a
    catalog with only the serving profile is complete, just unfurnished.
    """
    profiles = [default_profile(collection)]
    if starter_path.exists():
        try:
            starter = CaptureProfileCatalog.model_validate(load_yaml(starter_path.read_text()))
        except (OSError, ValueError, yaml.YAMLError):
            log.exception(
                "starter capture catalog %s is unreadable; seeding default only", starter_path
            )
        else:
            profiles.extend(p for p in starter.profiles if p.name != DEFAULT_PROFILE_NAME)
    return CaptureProfileCatalog(profiles=profiles)


@dataclass(frozen=True)
class CaptureProfileSources:
    """Where a vehicle's capture profiles come from (S03.4).

    Two files, layered by profile name (operator decision 2026-10-04):

    - ``catalog``: the tracked catalog, laptop-authored and never written at
      runtime. A vehicle without one starts from :func:`seed_catalog`, in
      memory.
    - ``overrides``: what this machine saved or deleted from the dashboard.
      An override replaces the tracked profile of the same name; a deletion
      hides it. Every profile not overridden keeps following the repo. ``None``
      reads the tracked catalog alone (a tool with no config).
    """

    catalog: Path
    overrides: Path | None

    @classmethod
    def of(cls, daemon: DaemonConfig) -> CaptureProfileSources:
        return cls(daemon.capture_profiles, daemon.capture_profile_overrides)


class CaptureProfileOverrides(_Strict):
    """This machine's layer over the tracked catalog (S03.4)."""

    schema_version: int = SCHEMA_VERSION
    #: Profiles saved here: each replaces the tracked one of its name, or adds one.
    profiles: list[CaptureProfile] = Field(default_factory=list)
    #: Tracked profiles deleted here.
    deleted: list[str] = Field(default_factory=list)
    #: The tracked profile each override replaced, as it was when saved
    #: (:func:`profile_digest`), so a later repo change to it is reported.
    base_digests: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> CaptureProfileOverrides:
        names = [profile.name for profile in self.profiles]
        if len(names) != len(set(names)):
            raise ValueError("capture profile override names must be unique")
        if DEFAULT_PROFILE_NAME in self.deleted:
            raise ValueError("the default capture profile cannot be deleted")
        if set(names) & set(self.deleted):
            raise ValueError("a capture profile cannot be both overridden and deleted")
        return self


def save_capture_profiles(path: Path, catalog: CaptureProfileCatalog) -> None:
    """Write a whole catalog: authoring tools only, never the daemon (S03.4)."""
    payload = catalog.model_dump(mode="json")
    atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False), mkdir=True)


def _read_catalog(path: Path) -> CaptureProfileCatalog | None:
    if not path.exists():
        return None
    return CaptureProfileCatalog.model_validate(load_yaml(path.read_text()))


def load_overrides(path: Path | None) -> CaptureProfileOverrides:
    if path is None or not path.exists():
        return CaptureProfileOverrides()
    return CaptureProfileOverrides.model_validate(load_yaml(path.read_text()) or {})


def save_overrides(path: Path, overrides: CaptureProfileOverrides) -> None:
    payload = overrides.model_dump(mode="json")
    atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False), mkdir=True)


#: ``(name, digest now in the repo)`` already reported as shadowed, so a
#: catalog read on every API call reports each change once per process.
_SHADOW_WARNED: set[tuple[str, str]] = set()


def layer_catalog(
    base: CaptureProfileCatalog, overrides: CaptureProfileOverrides
) -> CaptureProfileCatalog:
    """``base`` with ``overrides`` applied by name; tracked order kept, additions last."""
    replaced = {profile.name: profile for profile in overrides.profiles}
    deleted = set(overrides.deleted)
    profiles: list[CaptureProfile] = []
    for profile in base.profiles:
        if profile.name in deleted:
            continue
        override = replaced.pop(profile.name, None)
        if override is None:
            profiles.append(profile)
            continue
        current = profile_digest(profile)
        saved = overrides.base_digests.get(profile.name)
        if saved != current and (profile.name, current) not in _SHADOW_WARNED:
            _SHADOW_WARNED.add((profile.name, current))
            log.warning(
                "capture profile %r is overridden on this machine, and the repo's version "
                "has changed since; the override still wins (revert it to take the repo's)",
                profile.name,
                extra={"profile": profile.name},
            )
        profiles.append(override)
    profiles.extend(sorted(replaced.values(), key=lambda item: item.name))
    return CaptureProfileCatalog(profiles=profiles)


def load_capture_profiles(
    sources: CaptureProfileSources, collection: CollectionConfig
) -> CaptureProfileCatalog:
    """A vehicle's effective catalog: the tracked one (or the seed) under this
    machine's overrides. Never writes anything.

    The tracked catalog used to double as the saved one, and the daemon wrote
    it on the Pi, which then blocked ``git pull``/``canary-update`` (the last
    time, 2026-10-04, for two empty ``kline: []``). Nothing appends, upgrades or
    restores a profile behind the operator's back: a deletion here stays
    deleted, and only a revert brings the repo's version back.
    """
    base = _read_catalog(sources.catalog) or seed_catalog(collection)
    return layer_catalog(base, load_overrides(sources.overrides))


def _reset_for_tests() -> None:
    """Forget which shadowed overrides were already reported."""
    _SHADOW_WARNED.clear()
