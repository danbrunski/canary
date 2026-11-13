"""The ECU backup's read recipe: schema and loader (W08.7, split out in R29.13).

The recipe is vehicle data (``rom_backup.yaml``, the profile's own or its
platform's: :func:`src.vehicle.platforms.vehicle_data_file`): the request and
response ids, the DiagnosticSessionControl sequence, the address range and
block size, the pacing, and the capture that established them as provenance.
:mod:`src.control.rom_backup` runs it; this module only validates it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.rom_extent import ExtentSpec
from src.analysis.rom_map import RomMap
from src.collection.isotp import DEFAULT_REQUEST_ID
from src.vehicle.platforms import VehicleDataFile
from src.yaml_load import load_yaml

#: File name of the recipe in a vehicle profile or platform directory.
ROM_BACKUP_FILE = "rom_backup.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecipeSession(_Strict):
    """DiagnosticSessionControl sub-functions, in order."""

    enter_once: list[int] = Field(default_factory=list)
    before_each_read: list[int] = Field(default_factory=list)
    #: Sent after the read, on every exit path that entered a session: back to
    #: the session CANary's normal polling expects.
    exit: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bytes(self) -> RecipeSession:
        for value in (*self.enter_once, *self.before_each_read, *self.exit):
            if not 0 <= value <= 0xFF:
                raise ValueError(f"session sub-function 0x{value:X} is not a byte")
        return self


class RecipeRead(_Strict):
    start: int = Field(ge=0)
    #: Inclusive.
    end: int = Field(ge=0)
    #: Bytes per ReadMemoryByAddress; the last read is shorter when the range
    #: is not a multiple.
    block_size: int = Field(ge=1, le=0xFFE)

    @model_validator(mode="after")
    def _range(self) -> RecipeRead:
        if self.end < self.start or self.end > 0xFFFFFFFF:
            raise ValueError("read range must run start..end within 32-bit addresses")
        return self

    @property
    def size(self) -> int:
        """Bytes in the image: the range, which starts at 0
        (:func:`load_backup_recipe`)."""
        return self.end + 1 - self.start

    def blocks(self) -> list[tuple[int, int]]:
        """``(address, length)`` for every read, in address order."""
        return [
            (address, min(self.block_size, self.end + 1 - address))
            for address in range(self.start, self.end + 1, self.block_size)
        ]


class RecipePacing(_Strict):
    min_cycle_seconds: float = Field(ge=0)
    request_timeout_seconds: float = Field(gt=0, le=10)
    retries_per_block: int = Field(ge=0, le=10)


class RecipeVerify(_Strict):
    reread_blocks: int = Field(ge=0)


class RecipeProvenance(_Strict):
    capture: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class BackupRecipe(_Strict):
    schema_version: Literal[1]
    request_id: int = Field(ge=0, le=0x7FF)
    response_id: int = Field(ge=0, le=0x7FF)
    session: RecipeSession
    read: RecipeRead
    pacing: RecipePacing
    verify: RecipeVerify
    provenance: RecipeProvenance
    #: Frames the ECM broadcasts by itself whenever it is powered, at least
    #: one of them faster than ``pacing.request_timeout_seconds``. Empty: a
    #: timeout is reported without saying whether the ECM went silent.
    ecm_broadcast_ids: list[int] = Field(default_factory=list)
    #: What the image says about its own size, read offline from the bytes
    #: (:mod:`src.analysis.rom_extent`). ``None``: the size goes unchecked.
    extent: ExtentSpec | None = None

    @model_validator(mode="after")
    def _broadcast_ids(self) -> BackupRecipe:
        diagnostic = {self.request_id, self.response_id, DEFAULT_REQUEST_ID}
        for value in self.ecm_broadcast_ids:
            if not 0 <= value <= 0x7FF:
                raise ValueError(f"ecm_broadcast_ids 0x{value:X} is not an 11-bit CAN id")
            if value in diagnostic:
                raise ValueError(
                    f"ecm_broadcast_ids 0x{value:03X} is a diagnostic id; list frames the ECM "
                    "broadcasts by itself"
                )
        return self


def load_backup_recipe(path: Path, rom_map: RomMap | None) -> BackupRecipe:
    """The recipe at ``path``, refused when it would not produce a whole image.

    The range must start at 0, so that image offsets are flash addresses. With
    a map it must also cover the map's image exactly; without one, the range is
    the image.
    """
    recipe = BackupRecipe.model_validate(load_yaml(path.read_text()))
    span = recipe.read.size
    if rom_map is not None and (recipe.read.start != 0 or span != rom_map.image_size):
        raise ValueError(
            f"{ROM_BACKUP_FILE} reads 0x{recipe.read.start:06X}-0x{recipe.read.end:06X} "
            f"({span:,} bytes); the image is 0x000000-0x{rom_map.image_size - 1:06X}"
        )
    if recipe.read.start != 0:
        raise ValueError(
            f"{ROM_BACKUP_FILE} reads from 0x{recipe.read.start:06X}; an image starts at "
            "0x000000, so that its offsets are flash addresses"
        )
    extent = recipe.extent
    if extent is not None and extent.tables_end > span:
        raise ValueError(
            f"{ROM_BACKUP_FILE} extent: a vector table runs to 0x{extent.tables_end:06X}, past "
            f"the {span:,} bytes the recipe reads"
        )
    if extent is not None and extent.ram_from <= recipe.read.end:
        raise ValueError(
            f"{ROM_BACKUP_FILE} extent: ram_from 0x{extent.ram_from:08X} is inside the read "
            f"range, which ends at 0x{recipe.read.end:06X}"
        )
    return recipe


def recipe_extent(recipe_file: Callable[[], VehicleDataFile]) -> ExtentSpec | None:
    """The vehicle recipe's ``extent``, or ``None`` when no recipe loads.

    For the library, which reads every image's extent: a missing or broken
    recipe is the backup card's refusal to name, not a reason a listing fails.
    """
    try:
        return load_backup_recipe(recipe_file().path, None).extent
    except (FileNotFoundError, ValueError, yaml.YAMLError):
        return None


__all__ = [
    "ROM_BACKUP_FILE",
    "BackupRecipe",
    "RecipePacing",
    "RecipeProvenance",
    "RecipeRead",
    "RecipeSession",
    "RecipeVerify",
    "load_backup_recipe",
    "recipe_extent",
]
