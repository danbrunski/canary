"""Vehicle platforms: data shared by every vehicle of one generation.

A profile's data lives in ``data/<slug>/``. Some facts belong to a whole
generation rather than one vehicle: every 2005-2015 Xterra (N50) ECM hands out
its flash to the same requests, so a 2013 needs the recipe the 2008's capture
established, not a copy of it in ``data/nissan-xterra-2013/`` that drifts. A
platform is a directory ``data/platforms/<id>/``: a ``platform.yaml`` naming
the vehicles it covers (make, model, inclusive model years) and the shared
files beside it.

:func:`vehicle_data_file` is the one lookup. The profile's own file wins, so a
vehicle can always override its platform; otherwise the matching platform's.
When neither has the file, the error names where it looked and the make, model
and year the match saw, so a profile with a blank model reads as that rather
than as "no platform".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.config import CanaryConfig
from src.paths import PLATFORMS_DIR, REPO_ROOT
from src.vehicle.profiles import profile_dir

#: The definition file in each platform directory.
PLATFORM_FILE = "platform.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlatformVehicles(_Strict):
    """One make and model, over a span of model years."""

    make: str = Field(min_length=1)
    model: str = Field(min_length=1)
    #: Inclusive.
    years: tuple[int, int]

    @model_validator(mode="after")
    def _span(self) -> PlatformVehicles:
        if self.years[0] > self.years[1]:
            raise ValueError(f"years {self.years[0]}-{self.years[1]} run backwards")
        return self

    def covers(self, make: str, model: str, year: int | None) -> bool:
        return (
            year is not None
            and self.years[0] <= year <= self.years[1]
            and make.strip().casefold() == self.make.casefold()
            and model.strip().casefold() == self.model.casefold()
        )


class Platform(_Strict):
    schema_version: Literal[1]
    name: str = Field(min_length=1)
    vehicles: list[PlatformVehicles] = Field(min_length=1)
    #: Why these vehicles share the files here.
    evidence: str = Field(min_length=1)


@dataclass(frozen=True)
class ResolvedPlatform:
    id: str
    directory: Path
    definition: Platform


@dataclass(frozen=True)
class VehicleDataFile:
    """Where a vehicle data file was found."""

    path: Path
    #: ``None``: the profile's own file.
    platform: ResolvedPlatform | None

    def __str__(self) -> str:
        if self.platform is None:
            return _shown(self.path)
        return f"{_shown(self.path)} (platform {self.platform.definition.name})"


def _shown(path: Path) -> str:
    """``path`` relative to the checkout when it is inside it."""
    return str(path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path)


def load_platforms(platforms_dir: Path = PLATFORMS_DIR) -> list[ResolvedPlatform]:
    """Every ``<platforms_dir>/<id>/platform.yaml``, validated, in id order.

    ``ValueError`` naming the file when one is not valid YAML or not a valid
    definition.
    """
    if not platforms_dir.is_dir():
        return []
    platforms = []
    for path in sorted(platforms_dir.glob(f"*/{PLATFORM_FILE}")):
        try:
            definition = Platform.model_validate(yaml.safe_load(path.read_text()))
        except (yaml.YAMLError, ValidationError) as exc:
            raise ValueError(f"{_shown(path)} is invalid: {exc}") from exc
        platforms.append(ResolvedPlatform(path.parent.name, path.parent, definition))
    return platforms


def find_platform(
    make: str, model: str, year: int | None, *, platforms_dir: Path = PLATFORMS_DIR
) -> ResolvedPlatform | None:
    """The one platform covering this vehicle, or ``None``.

    Refuses (``ValueError``) a vehicle two platforms both cover: which one's
    files apply would depend on directory order.
    """
    matches = [
        platform
        for platform in load_platforms(platforms_dir)
        if any(v.covers(make, model, year) for v in platform.definition.vehicles)
    ]
    if len(matches) > 1:
        raise ValueError(
            f"{_vehicle(make, model, year)} is covered by more than one platform: "
            + ", ".join(p.id for p in matches)
        )
    return matches[0] if matches else None


def _vehicle(make: str, model: str, year: int | None) -> str:
    return (
        f"make {make.strip() or '(blank)'!r}, model {model.strip() or '(blank)'!r}, "
        f"year {year or '(unset)'}"
    )


def vehicle_data_file(
    profile: Path,
    name: str,
    *,
    make: str,
    model: str,
    year: int | None,
    platforms_dir: Path = PLATFORMS_DIR,
) -> VehicleDataFile:
    """``<profile>/<name>``, else the vehicle's platform's ``<name>``.

    Raises ``FileNotFoundError`` naming every place it looked, and
    ``ValueError`` for an invalid or ambiguous platform definition.
    """
    own = profile / name
    if own.exists():
        return VehicleDataFile(own, None)
    platform = find_platform(make, model, year, platforms_dir=platforms_dir)
    if platform is None:
        raise FileNotFoundError(
            f"{_shown(own)} does not exist, and no platform in {_shown(platforms_dir)} covers "
            f"{_vehicle(make, model, year)} (the profile's make, model and year choose it)"
        )
    shared = platform.directory / name
    if not shared.exists():
        raise FileNotFoundError(
            f"{_shown(own)} does not exist, and platform {platform.definition.name} has no {name}"
        )
    return VehicleDataFile(shared, platform)


def config_data_file(
    config: CanaryConfig, name: str, *, platforms_dir: Path = PLATFORMS_DIR
) -> VehicleDataFile:
    """:func:`vehicle_data_file` for the vehicle ``config`` is scoped to."""
    vehicle = config.vehicle
    return vehicle_data_file(
        profile_dir(config),
        name,
        make=vehicle.make,
        model=vehicle.model,
        year=vehicle.year or None,
        platforms_dir=platforms_dir,
    )


__all__ = [
    "PLATFORM_FILE",
    "Platform",
    "PlatformVehicles",
    "ResolvedPlatform",
    "VehicleDataFile",
    "config_data_file",
    "find_platform",
    "load_platforms",
    "vehicle_data_file",
]
