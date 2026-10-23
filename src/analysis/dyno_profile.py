"""A vehicle's ``dyno.yaml``: the stored dyno facts, their loading and saving.

:class:`DynoProfile` holds the sections other modules own the schema of
(motion sources, inertia, squat, road load, noise floors) and refuses a
profile whose stored facts are unsound. Loading refuses a truncated file
rather than reading it as "no floor measured"; saving is atomic. The file
lives beside the vehicle's other profile data (:func:`dyno_profile_path`),
never in the checkout's runtime state.

Imports the stored-record schemas from their owners, never the estimators'
orchestration (:mod:`src.analysis.dyno_workbench`) or the ``dyno`` facade.
Moved verbatim out of ``dyno.py`` (R16).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import Field, model_validator

from src.analysis.dyno_comparison import FLOOR_MATCH_FIELDS, DynoNoiseFloor, Quantity, RunConditions
from src.analysis.dyno_contract import DynoRefusal, MotionSources, StrictModel
from src.analysis.dyno_driveline import InertiaData
from src.analysis.dyno_road_load import RoadLoadCoefficients
from src.analysis.dyno_squat import SquatCoefficient
from src.atomic_write import atomic_write_text
from src.yaml_load import load_yaml

if TYPE_CHECKING:
    from src.config import CanaryConfig

#: Per-vehicle dyno facts, in the profile data directory.
DYNO_FILENAME = "dyno.yaml"


class DynoProfile(StrictModel):
    """A vehicle's ``dyno.yaml``: inertia estimates and measured dyno facts."""

    schema_version: Literal[1] = 1
    motion_sources: MotionSources | None = None
    inertia: InertiaData | None = None
    squat: SquatCoefficient | None = None
    road_load: RoadLoadCoefficients | None = None
    noise_floors: list[DynoNoiseFloor] = Field(default_factory=list)

    @model_validator(mode="after")
    def _stored_facts_are_sound(self) -> DynoProfile:
        if self.road_load is not None and self.road_load.directions != "pair":
            raise ValueError(
                "road_load must come from a coast-down pair (both directions of one road); "
                "a single direction leaves grade and wind in A and B"
            )
        seen: set[tuple[Any, ...]] = set()
        for floor in self.noise_floors:
            key = (floor.quantity, *(getattr(floor.conditions, k) for k in FLOOR_MATCH_FIELDS))
            if key in seen:
                raise ValueError(f"two noise floors for the same regime {key}")
            seen.add(key)
        return self

    def noise_floor_for(
        self, quantity: Quantity, conditions: RunConditions
    ) -> DynoNoiseFloor | DynoRefusal:
        """The floor measured for this quantity, gear, road and induction."""
        for floor in self.noise_floors:
            if floor.matches(quantity, conditions):
                return floor
        if not self.noise_floors:
            return DynoRefusal(
                "noise_floor", "no_floor", "no dyno noise floor has been recorded for this vehicle"
            )
        regimes = sorted(
            f"{f.quantity} gear {f.conditions.gear} on {f.conditions.road!r} "
            f"({f.conditions.induction})"
            for f in self.noise_floors
        )
        return DynoRefusal(
            "noise_floor",
            "conditions_mismatch",
            f"no floor for {quantity} gear {conditions.gear} on {conditions.road!r} "
            f"({conditions.induction}); recorded: {regimes}",
        )


def load_dyno_profile(path: Path) -> DynoProfile | None:
    """Load a vehicle's dyno facts, or ``None`` when it has no ``dyno.yaml``.

    A file that exists but is empty, is not a mapping, or does not validate
    raises, naming the file: a truncated ``dyno.yaml`` must not read as "no
    floor measured", which is a different answer.
    """
    if not path.exists():
        return None
    raw = load_yaml(path.read_text())
    if not isinstance(raw, dict):
        kind = "empty" if raw is None else f"a {type(raw).__name__}, not a mapping"
        raise ValueError(f"{path} is {kind}; a dyno profile needs at least schema_version")
    return DynoProfile.model_validate(raw)


def save_dyno_profile(path: Path, profile: DynoProfile) -> None:
    atomic_write_text(
        path, yaml.safe_dump(profile.model_dump(mode="json"), sort_keys=False), mkdir=True
    )


def dyno_profile_path(config: CanaryConfig) -> Path:
    """The active vehicle's ``dyno.yaml``, beside its other profile data."""
    from src.vehicle.profiles import profile_dir

    return profile_dir(config) / DYNO_FILENAME
