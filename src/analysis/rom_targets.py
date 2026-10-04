"""Hash-bound offline Fuel Targets edits; vehicle facts belong in profile data."""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.analysis.osiris import Container
from src.signals.afr import TargetAfrMap, load_afr_calibration


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Checksums(Strict):
    algorithm: Literal["be32_sum_xor"]
    start: int
    excluded: list[int]
    sum_offset: int
    xor_offset: int
    markers: dict[int, int]

    def values(self, rom: bytes) -> tuple[int, int]:
        offsets = [self.start, self.sum_offset, self.xor_offset, *self.excluded, *self.markers]
        if len(rom) % 4 or any(o < 0 or o % 4 or o + 4 > len(rom) for o in offsets):
            raise ValueError("Invalid checksum word offsets")
        if self.sum_offset == self.xor_offset or not {self.sum_offset, self.xor_offset}.issubset(
            self.excluded
        ):
            raise ValueError("Checksum words must be distinct and excluded")
        for offset, expected in self.markers.items():
            if struct.unpack_from(">I", rom, offset)[0] != expected:
                raise ValueError("Unsupported ROM checksum style")
        total = xor = 0
        for offset in range(self.start, len(rom), 4):
            if offset not in self.excluded:
                value = struct.unpack_from(">I", rom, offset)[0]
                total = (total + value) & 0xFFFFFFFF
                xor ^= value
        return total, xor

    def verify(self, rom: bytes) -> None:
        total, xor = self.values(rom)
        if (total, xor) != (
            struct.unpack_from(">I", rom, self.sum_offset)[0],
            struct.unpack_from(">I", rom, self.xor_offset)[0],
        ):
            raise ValueError("Internal ROM checksum mismatch")


class Definition(Strict):
    schema_version: Literal[1]
    table: Literal["fuel_targets"]
    map_slot: int = Field(ge=1)
    image_size: int = Field(gt=0)
    table_offset: int = Field(ge=0)
    rpm_axis_offset: int = Field(ge=0)
    load_axis_offset: int = Field(ge=0)
    afr_numerator: float = Field(gt=0)
    calibration_file: str
    checksums: Checksums


class CellEdit(Strict):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    rpm: float
    bfs_ms: float
    old_raw: int = Field(ge=1, le=255)
    new_raw: int = Field(ge=1, le=255)
    rationale: str = Field(min_length=1)


class Patch(Strict):
    schema_version: Literal[1]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    table: Literal["fuel_targets"]
    map_slot: int = Field(ge=1)
    changes: list[CellEdit] = Field(min_length=1)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TargetEditor:
    def __init__(self, source: bytes, definition: Definition, target: TargetAfrMap):
        self.source = source
        self.definition = definition
        self.target = target
        provenance = target.provenance
        if provenance is None or provenance.rom_sha256 != sha256(source):
            raise ValueError("Unrecognized source hash; independently verified provenance required")
        if provenance.map_slot != definition.map_slot:
            raise ValueError("Map slot mismatch")
        self.container = Container.parse(source)
        self.rom = self.container.decoded()
        if len(self.rom) != definition.image_size:
            raise ValueError("Unsupported ROM size")
        definition.checksums.verify(self.rom)
        if target.raw_values is None or any(
            not 1 <= x <= 255 for r in target.raw_values for x in r
        ):
            raise ValueError("Complete nonzero raw target table required")
        sequences = [
            (definition.table_offset, [v for row in target.raw_values for v in row]),
            (definition.rpm_axis_offset, target.raw_rpm_breakpoints),
            (definition.load_axis_offset, target.raw_load_breakpoints),
        ]
        spans = []
        for offset, sequence in sequences:
            if sequence is None:
                raise ValueError("Raw axes required")
            expected = bytes(sequence)
            if self.rom[offset : offset + len(expected)] != expected:
                raise ValueError(f"Table/axis bytes differ from verified reference at {offset:#x}")
            spans.append(set(range(offset, offset + len(expected))))
        checksum_bytes = {
            o + i
            for o in (definition.checksums.sum_offset, definition.checksums.xor_offset)
            for i in range(4)
        }
        if any(spans[i] & spans[j] for i in range(3) for j in range(i)) or any(
            span & checksum_bytes for span in spans
        ):
            raise ValueError("Table, axes and checksum spans must not overlap")
        for raw_row, afr_row in zip(target.raw_values, target.afr, strict=True):
            for raw, afr in zip(raw_row, afr_row, strict=True):
                if abs(definition.afr_numerator / raw - afr) > 0.000001:
                    raise ValueError("AFR scaling disagrees with verified reference")

    @classmethod
    def load(cls, source: Path, definition_path: Path) -> TargetEditor:
        return cls.from_bytes(source.read_bytes(), definition_path)

    @classmethod
    def from_bytes(cls, data: bytes, definition_path: Path) -> TargetEditor:
        """Inspect an upload without persisting proprietary ROM bytes."""
        definition = Definition.model_validate(yaml.safe_load(definition_path.read_text()))
        calibration = load_afr_calibration(definition_path.parent / definition.calibration_file)
        matches = [
            t
            for t in calibration.target_maps
            if t.provenance
            and t.provenance.rom_sha256 == sha256(data)
            and t.provenance.map_slot == definition.map_slot
        ]
        if len(matches) != 1:
            raise ValueError("Source hash/map slot must match exactly one verified calibration")
        return cls(data, definition, matches[0])

    def export(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source_sha256": sha256(self.source),
            "revision": self.target.revision,
            "table": self.definition.table,
            "map_slot": self.definition.map_slot,
            "row_axis": "rpm",
            "column_axis": "bfs_ms",
            "rpm": self.target.rpm_breakpoints,
            "bfs_ms": self.target.load_breakpoints,
            # The axis bytes as the ROM stores them, verified against the
            # image in __init__; what a raw trace mapping lands on (W05.1).
            "rpm_raw": self.target.raw_rpm_breakpoints,
            "bfs_raw": self.target.raw_load_breakpoints,
            "raw": self.target.raw_values,
            "afr": self.target.afr,
            "afr_formula": f"{self.definition.afr_numerator} / raw",
        }

    def apply(self, patch: Patch) -> tuple[bytes, dict[str, Any]]:
        if patch.source_sha256 != sha256(self.source):
            raise ValueError("Patch source hash mismatch")
        if patch.table != self.definition.table or patch.map_slot != self.definition.map_slot:
            raise ValueError("Patch table/map slot mismatch")
        rom = bytearray(self.rom)
        seen: set[int] = set()
        changes = []
        for edit in patch.changes:
            if edit.row >= len(self.target.rpm_breakpoints) or edit.column >= len(
                self.target.load_breakpoints
            ):
                raise ValueError("Cell index outside table")
            if (edit.rpm, edit.bfs_ms) != (
                self.target.rpm_breakpoints[edit.row],
                self.target.load_breakpoints[edit.column],
            ):
                raise ValueError("Cell axis labels do not match row/column")
            offset = (
                self.definition.table_offset
                + edit.row * len(self.target.load_breakpoints)
                + edit.column
            )
            if offset in seen:
                raise ValueError("Duplicate cell edit")
            seen.add(offset)
            if rom[offset] != edit.old_raw:
                raise ValueError("Expected old cell value mismatch")
            if edit.old_raw == edit.new_raw:
                raise ValueError("No-op cell edit")
            rom[offset] = edit.new_raw
            changes.append(
                {
                    **edit.model_dump(),
                    "decoded_offset": hex(offset),
                    "old_afr": self.definition.afr_numerator / edit.old_raw,
                    "new_afr": self.definition.afr_numerator / edit.new_raw,
                }
            )
        checksums = self.definition.checksums
        total, xor = checksums.values(bytes(rom))
        struct.pack_into(">I", rom, checksums.sum_offset, total)
        struct.pack_into(">I", rom, checksums.xor_offset, xor)
        checksums.verify(bytes(rom))
        allowed = seen | {
            o + i for o in (checksums.sum_offset, checksums.xor_offset) for i in range(4)
        }
        differences = [i for i, (a, b) in enumerate(zip(self.rom, rom, strict=True)) if a != b]
        if not set(differences) <= allowed:
            raise ValueError("Unexpected decoded ROM changes")
        candidate = self.container.with_rom(bytes(rom)).serialize()
        reparsed = Container.parse(candidate)
        if reparsed.decoded() != bytes(rom) or reparsed.serialize() != candidate:
            raise ValueError("Candidate verification failed")
        if reparsed.prefix != self.container.prefix or [
            s for s in reparsed.sections if s.label != b"ROMData "
        ] != [s for s in self.container.sections if s.label != b"ROMData "]:
            raise ValueError("Unexpected non-ROM container change")
        return candidate, {
            "schema_version": 1,
            "source_sha256": sha256(self.source),
            "candidate_sha256": sha256(candidate),
            "table": patch.table,
            "map_slot": patch.map_slot,
            "changes": changes,
            "decoded_changed_offsets": [hex(i) for i in differences],
            "checksums": {"sum": f"{total:08x}", "xor": f"{xor:08x}"},
            "validation": "CRC, internal checksums, roundtrip, permitted-byte diff passed",
            "review": "Offline candidate only; reopen in UpRev and review before any use",
        }
