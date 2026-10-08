"""Which Fuel Comp bytes a recorded revision ran, from a hash-checked image (N02.1).

N02 places every logged sample on the Fuel Comp cells that were active when
it was logged. That needs the revision's own table, its rpm axis ``0xCACD``
and load axis ``0xCB29`` (k106 and 20260920 compressed the load axis to
16..144; 20261004 restored 16..255), K, and every value the load byte's
chain reads (:mod:`src.decode.fuel_comp_load`). Those are bytes in a private
ROM file outside the repository, so this module:

- finds the file for a revision in **operator-supplied directories** (a CLI
  argument, or :data:`ROM_DIRS_ENV`), by the registry's ``rom_file`` name, or
  among explicitly named image files by hash -- it knows no path itself;
- decodes an ``.OsirisROM`` or takes a plain image (:func:`src.analysis.
  rom_image.load_image`), and **refuses unless the plain image's sha256 is the
  registry's ``image_sha256``** for that revision;
- refuses an image of another program: the calibration ID must be one the
  vehicle's ``rom_map.yaml`` describes and every program literal it declares
  must hold the decoded value;
- reads every table by its ``rom_map.yaml`` id, refusing one that is missing
  or not ``verified``.

Every refusal is a :class:`FuelCompRomRefusalError` whose ``reason`` names it:
``revision_unknown``, ``no_surviving_image`` (no plain-image hash recorded:
ZS04C-stock, osiris-initial, k105), ``image_not_found``,
``image_hash_mismatch``, ``image_unreadable``, ``wrong_program``,
``table_missing``, ``table_unverified`` and ``curves_not_monotone`` (the
image's curves would make the load byte non-monotone, so no interval from it
could be trusted). Nothing here writes an image.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.analysis.rom_image import (
    ImageFormatError,
    load_image,
    map_applies,
    read_calibration_id,
    sha256_hex,
    table_values,
)
from src.analysis.rom_map import RomMap, check_program_constants, read_program_constant
from src.decode.fuel_comp_load import LoadInputCurves, NonMonotoneCurvesError
from src.vehicle.tune_revisions import TuneRevisionRegistry

#: Environment variable naming the directories ROM files are searched in,
#: separated like ``PATH`` (``os.pathsep``). Read only by
#: :func:`rom_dirs_from_env`; nothing here has a built-in default.
ROM_DIRS_ENV = "CANARY_ROM_DIRS"


class FuelCompRomRefusalError(ValueError):
    """The revision's Fuel Comp bytes cannot be read; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FuelCompRomTables(_Strict):
    """Which ``rom_map.yaml`` entries hold what the reconstruction reads.

    Vehicle profile data (``fuel_comp_fit.yaml`` ``rom:``): the ids name
    entries in the same profile's ``rom_map.yaml``, which carry the offsets
    and the code evidence; nothing here restates an address.
    """

    #: ``kind`` of the Fuel Comp slot table; its row and column axes are the
    #: table's own ``axes`` in ``rom_map.yaml``.
    fuel_comp_kind: str = Field(min_length=1)
    #: The rpm search (``0x027194``) compares the rpm word with axis byte x
    #: this; the load search (``0x027114``) compares the byte directly.
    rpm_axis_compare_multiplier: int = Field(ge=1)
    k_table: str = Field(min_length=1)
    area_curve: str = Field(min_length=1)
    fill_low: str = Field(min_length=1)
    fill_high: str = Field(min_length=1)
    rpm_multiplier: str = Field(min_length=1)
    idle_air_cap: str = Field(min_length=1)
    limp_home_area: str = Field(min_length=1)
    purge_gain: str = Field(min_length=1)
    #: A ``program_constants`` id, not a table.
    reciprocal_numerator: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


@dataclass(frozen=True)
class FuelCompCalibration:
    """One revision's Fuel Comp bytes and the load byte's inputs, as read."""

    #: The revision (or reference image) name these bytes belong to.
    name: str
    image_sha256: str
    #: Where the image was read, for the record.
    source_path: Path
    image_format: str
    slot: int
    #: 16 x 16 raw cells, rows = rpm, columns = load (128 = 1.0).
    fuel_comp: tuple[tuple[int, ...], ...]
    rpm_axis_raw: tuple[int, ...]
    load_axis_raw: tuple[int, ...]
    #: The rpm axis in the rpm word's domain (byte x the compare multiplier).
    rpm_breakpoints: tuple[int, ...]
    #: K in the slot, raw.
    k: int
    #: Calibration byte ``0x82FE``: ``w139E = purge_raw x gain >> 7``.
    purge_gain: int
    curves: LoadInputCurves
    #: ``rom_map.yaml`` ids, for the record.
    table_ids: Mapping[str, str]

    def provenance(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "image_sha256": self.image_sha256,
            "source_file": self.source_path.name,
            "image_format": self.image_format,
            "slot": self.slot,
            "tables": dict(self.table_ids),
            "rpm_axis_raw": list(self.rpm_axis_raw),
            "load_axis_raw": list(self.load_axis_raw),
            "k": self.k,
        }


def rom_dirs_from_env(environ: Mapping[str, str] | None = None) -> list[Path]:
    """The directories named by :data:`ROM_DIRS_ENV` (empty when unset)."""
    raw = (os.environ if environ is None else environ).get(ROM_DIRS_ENV, "")
    return [Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()]


class RevisionImageResolver:
    """Revision name to :class:`FuelCompCalibration`, hash-checked and cached.

    ``rom_dirs`` are searched for each revision's registry ``rom_file`` (as a
    relative path, then by its file name); ``image_files`` are hashed and
    matched by plain-image sha256 whatever their name (an ECU read saved
    under its own name). Both come from the caller.
    """

    def __init__(
        self,
        registry: TuneRevisionRegistry,
        rom_map: RomMap,
        tables: FuelCompRomTables,
        *,
        rom_dirs: Sequence[Path] = (),
        image_files: Sequence[Path] = (),
        slot: int,
    ) -> None:
        self._registry = registry
        self._rom_map = rom_map
        self._tables = tables
        self._dirs = [Path(d) for d in rom_dirs]
        self._files = [Path(f) for f in image_files]
        self._slot = slot
        self._hashed: dict[Path, tuple[str, bytes, str] | None] = {}
        self._resolved: dict[str, FuelCompCalibration | FuelCompRomRefusalError] = {}

    @property
    def searched(self) -> list[str]:
        return [str(d) for d in self._dirs] + [str(f) for f in self._files]

    def resolve(self, name: str) -> FuelCompCalibration:
        """The named revision's (or reference image's) bytes, or a refusal."""
        cached = self._resolved.get(name)
        if cached is None:
            try:
                cached = self._resolve(name)
            except FuelCompRomRefusalError as exc:
                cached = exc
            self._resolved[name] = cached
        if isinstance(cached, FuelCompRomRefusalError):
            raise cached
        return cached

    def image(self, name: str) -> bytes:
        """The named revision's plain image, after :meth:`resolve` has
        hash-checked it, for readers of other tables (N02.6's Target AFR and
        timing axes). Read-only: nothing here writes an image."""
        calibration = self.resolve(name)
        loaded = self._load(calibration.source_path)
        if loaded is None or loaded[0] != calibration.image_sha256:
            raise FuelCompRomRefusalError(
                "image_unreadable", f"{calibration.source_path} changed since it was resolved"
            )
        return loaded[1]

    def _expected(self, name: str) -> tuple[str, str | None]:
        """``(image_sha256, rom_file)`` the registry records for ``name``."""
        revision = next((r for r in self._registry.revisions if r.revision == name), None)
        if revision is not None:
            if revision.image_sha256 is None:
                raise FuelCompRomRefusalError(
                    "no_surviving_image",
                    f"{name} has no image_sha256 in the tune registry (rom_file "
                    f"{revision.rom_file or 'not recorded'}): no surviving image of it is "
                    "known, so its Fuel Comp cells and axes cannot be read and its samples "
                    "cannot be placed",
                )
            return revision.image_sha256, revision.rom_file
        reference = next((r for r in self._registry.reference_images if r.name == name), None)
        if reference is not None:
            return reference.image_sha256, reference.rom_file
        raise FuelCompRomRefusalError(
            "revision_unknown", f"{name!r} is neither a revision nor a reference image"
        )

    def _load(self, path: Path) -> tuple[str, bytes, str] | None:
        """``(plain sha256, plain image, format)`` of ``path``, cached."""
        if path not in self._hashed:
            try:
                loaded = load_image(path.read_bytes(), self._rom_map)
            except ImageFormatError as exc:
                raise FuelCompRomRefusalError("image_unreadable", f"{path}: {exc}") from exc
            except OSError:
                self._hashed[path] = None
                return None
            self._hashed[path] = (sha256_hex(loaded.image), loaded.image, loaded.format)
        return self._hashed[path]

    def _find(self, name: str, expected: str, rom_file: str | None) -> tuple[Path, bytes, str]:
        wrong: list[tuple[Path, str]] = []
        if rom_file:
            relative = Path(rom_file)
            for directory in self._dirs:
                for candidate in dict.fromkeys([directory / relative, directory / relative.name]):
                    if not candidate.is_file():
                        continue
                    loaded = self._load(candidate)
                    if loaded is None:
                        continue
                    digest, image, fmt = loaded
                    if digest == expected:
                        return candidate, image, fmt
                    wrong.append((candidate, digest))
        for candidate in self._files:
            loaded = self._load(candidate)
            if loaded is not None and loaded[0] == expected:
                return candidate, loaded[1], loaded[2]
        if wrong:
            path, digest = wrong[0]
            raise FuelCompRomRefusalError(
                "image_hash_mismatch",
                f"{path} decodes to plain image {digest}, not {name}'s recorded "
                f"image_sha256 {expected}: it is some other image under that name",
            )
        raise FuelCompRomRefusalError(
            "image_not_found",
            f"no image of {name} (rom_file {rom_file or 'not recorded'}, plain sha256 "
            f"{expected[:12]}...) in {self.searched or 'no directories'}: pass the "
            f"directory holding it, or set {ROM_DIRS_ENV}",
        )

    def _resolve(self, name: str) -> FuelCompCalibration:
        expected, rom_file = self._expected(name)
        path, image, fmt = self._find(name, expected, rom_file)
        rom_map = self._rom_map
        reason = map_applies(read_calibration_id(image, rom_map), rom_map)
        if reason is not None:
            raise FuelCompRomRefusalError("wrong_program", f"{name}: {reason}")
        problems = check_program_constants(image, rom_map)
        if problems:
            raise FuelCompRomRefusalError(
                "wrong_program",
                f"{name} is not the program the load byte was decoded on: " + "; ".join(problems),
            )
        spec = self._tables
        values = {(t["id"], t["slot"]): t for t in table_values(image, rom_map)}

        def entry(table_id: str, slot: int | None) -> dict[str, Any]:
            found = values.get((table_id, slot))
            if found is None:
                raise FuelCompRomRefusalError(
                    "table_missing",
                    f"rom_map.yaml places no {table_id!r}"
                    + ("" if slot is None else f" in slot {slot}"),
                )
            if found["confidence"] != "verified":
                raise FuelCompRomRefusalError(
                    "table_unverified", f"{table_id} is {found['confidence']}, not verified"
                )
            return found

        def flat(table_id: str, slot: int | None = None) -> tuple[int, ...]:
            return tuple(int(v) for row in entry(table_id, slot)["raw"] for v in row)

        fuel_comp_tables = [
            t
            for (table_id, slot), t in values.items()
            if t["kind"] == spec.fuel_comp_kind and slot == self._slot
        ]
        if len(fuel_comp_tables) != 1:
            raise FuelCompRomRefusalError(
                "table_missing",
                f"expected one {spec.fuel_comp_kind!r} table in slot {self._slot}, "
                f"found {len(fuel_comp_tables)}",
            )
        fuel_comp = entry(fuel_comp_tables[0]["id"], self._slot)
        axes: dict[str, tuple[int, ...]] = {}
        axis_ids: dict[str, str] = {}
        for role, key in (("rows", "rpm"), ("cols", "load")):
            payload = fuel_comp["axes"][role]
            if payload is None:
                raise FuelCompRomRefusalError(
                    "table_missing", f"{fuel_comp['id']} declares no {role} axis"
                )
            if payload["confidence"] != "verified":
                raise FuelCompRomRefusalError(
                    "table_unverified", f"{payload['id']} is {payload['confidence']}"
                )
            axes[key] = tuple(int(v) for v in payload["raw"])
            axis_ids[f"{key}_axis"] = payload["id"]
        area = entry(spec.area_curve, None)
        area_axis = area["axes"]["cols"]
        if area_axis is None or area_axis["confidence"] != "verified":
            raise FuelCompRomRefusalError(
                "table_unverified", f"{spec.area_curve} has no verified axis"
            )
        try:
            constant = rom_map.program_constant(spec.reciprocal_numerator)
        except KeyError as exc:
            raise FuelCompRomRefusalError(
                "table_missing",
                f"rom_map.yaml declares no program constant {spec.reciprocal_numerator!r}",
            ) from exc
        if constant.confidence != "verified":
            raise FuelCompRomRefusalError(
                "table_unverified", f"{constant.id} is {constant.confidence}, not verified"
            )
        try:
            curves = LoadInputCurves(
                area_breakpoints=tuple(int(v) for v in area_axis["raw"]),
                area_values=flat(spec.area_curve),
                fill_low=flat(spec.fill_low),
                fill_high=flat(spec.fill_high),
                rpm_multiplier=flat(spec.rpm_multiplier)[0],
                reciprocal_numerator=read_program_constant(image, constant),
                idle_air_cap=flat(spec.idle_air_cap)[0],
                limp_home_area=flat(spec.limp_home_area)[0],
            )
        except NonMonotoneCurvesError as exc:
            raise FuelCompRomRefusalError(
                "curves_not_monotone",
                f"{name}: {exc}; the byte interval would not be bounded by its ends, so "
                "no sample of this image can be placed",
            ) from exc
        return FuelCompCalibration(
            name=name,
            image_sha256=expected,
            source_path=path,
            image_format=fmt,
            slot=self._slot,
            fuel_comp=tuple(tuple(int(v) for v in row) for row in fuel_comp["raw"]),
            rpm_axis_raw=axes["rpm"],
            load_axis_raw=axes["load"],
            rpm_breakpoints=tuple(v * spec.rpm_axis_compare_multiplier for v in axes["rpm"]),
            k=flat(spec.k_table, self._slot)[0],
            purge_gain=flat(spec.purge_gain)[0],
            curves=curves,
            table_ids={
                "fuel_comp": fuel_comp["id"],
                **axis_ids,
                "k": spec.k_table,
                "area_curve": spec.area_curve,
                "area_axis": area_axis["id"],
                "fill_low": spec.fill_low,
                "fill_high": spec.fill_high,
                "rpm_multiplier": spec.rpm_multiplier,
                "idle_air_cap": spec.idle_air_cap,
                "limp_home_area": spec.limp_home_area,
                "purge_gain": spec.purge_gain,
                "reciprocal_numerator": constant.id,
            },
        )


__all__ = [
    "ROM_DIRS_ENV",
    "FuelCompCalibration",
    "FuelCompRomRefusalError",
    "FuelCompRomTables",
    "RevisionImageResolver",
    "rom_dirs_from_env",
]
