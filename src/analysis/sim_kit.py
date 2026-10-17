"""The ROM-edit simulator's declared plant (E08.11): ``data/<vehicle>/sim_kit.yaml``.

The simulator runs its own plant version, declared as data, not the
contract's default: ``engine_sim_contract.yaml``'s ``plant_version`` is what
E07.4's intervention case and E07.6's paired runs name, and it does not move
when the simulator's choice does. The contract's ``conditional_rom_comparison``
mode lists the versions it admits, so a declaration naming any other is
refused when a scenario is checked (``plant_version_mismatch``).

This module only reads the declaration (it is imported by the design builders,
below the Monte Carlo). :func:`src.analysis.sim_montecarlo.load_sim_kit` is the
one loader that turns it into a kit and that plant's uncertainty declaration.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.analysis.sim_contract import SimulationRefusalError

SIM_KIT_FILE = "sim_kit.yaml"


class SimKitDeclaration(BaseModel):
    """Which frozen plant version the simulator runs, and each runnable
    version's uncertainty declaration (a file beside this one)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    plant_version: str = Field(min_length=1)
    #: Plant version -> its ``sim_uncertainty*.yaml`` file name.
    uncertainty: dict[str, str] = Field(min_length=1)
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _declared(self) -> SimKitDeclaration:
        if self.plant_version not in self.uncertainty:
            raise ValueError(
                f"the simulator's plant {self.plant_version} has no uncertainty declaration"
            )
        for version, name in self.uncertainty.items():
            if Path(name).name != name:
                raise ValueError(f"{version}: {name!r} is not a file name beside {SIM_KIT_FILE}")
        return self


@dataclass(frozen=True)
class LoadedSimKitDeclaration:
    declaration: SimKitDeclaration
    #: sha256 of the file's bytes (provenance).
    sha256: str
    path: Path


def load_sim_kit_declaration(profile_dir: Path) -> LoadedSimKitDeclaration:
    """``<profile_dir>/sim_kit.yaml``, refused by name when missing or invalid:
    a profile with no declaration has no simulator plant, and none is assumed."""
    path = Path(profile_dir) / SIM_KIT_FILE
    try:
        text = path.read_bytes()
    except FileNotFoundError as exc:
        raise SimulationRefusalError(
            "sim_kit_undeclared",
            f"{Path(profile_dir).name} has no {SIM_KIT_FILE}: the simulator's plant is declared, "
            "never defaulted to the contract's",
        ) from exc
    try:
        declaration = SimKitDeclaration.model_validate(yaml.safe_load(text))
    except ValidationError as exc:
        raise SimulationRefusalError(
            "sim_kit_invalid", f"{SIM_KIT_FILE}: {exc.error_count()} error(s): {exc}"
        ) from exc
    return LoadedSimKitDeclaration(declaration, hashlib.sha256(text).hexdigest(), path)


def sim_plant_version(profile_dir: Path) -> str:
    """The plant version the simulator runs on ``profile_dir`` (its designs'
    default too, so a design and the run that reads it agree)."""
    return load_sim_kit_declaration(profile_dir).declaration.plant_version


__all__ = [
    "SIM_KIT_FILE",
    "LoadedSimKitDeclaration",
    "SimKitDeclaration",
    "load_sim_kit_declaration",
    "sim_plant_version",
]
