"""N02.1: revision -> Fuel Comp bytes, from a hash-checked image, refusing by name.

The synthetic tests build a 1 MiB image with the vehicle's layout
(``tests/rom_fixtures.py``) and invented, rising curves -- no ROM values --
and a registry naming its plain sha256. The private-ROM tests at the end read
the operator's UpRev files and skip, naming the file, when they are absent: a
missing ROM is a skip, never a pass.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.analysis.fuel_comp_inputs import load_fuel_comp_declaration
from src.analysis.fuel_comp_rom import (
    ROM_DIRS_ENV,
    FuelCompRomRefusalError,
    RevisionImageResolver,
    rom_dirs_from_env,
)
from src.analysis.rom_map import load_rom_map
from src.vehicle.tune_revisions import (
    ReferenceImage,
    TuneRevision,
    TuneRevisionRegistry,
    load_tune_revisions,
)
from tests.rom_fixtures import OPERATOR_ROMS, PROFILE, load_byte_image, osiris_container

T0 = datetime(2026, 8, 1, tzinfo=UTC)


def _image(*, literal: int = 0x00DC1D00, calibration_id: bytes = b"1ZS44A00000") -> bytes:
    return load_byte_image(literal=literal, calibration_id=calibration_id)


def _registry(image: bytes, *, name: str = "synthetic-a") -> TuneRevisionRegistry:
    return TuneRevisionRegistry(
        revisions=[
            TuneRevision(revision="no-image", flashed_at=T0, rom_file="lost"),
            TuneRevision(
                revision=name,
                flashed_at=datetime(2026, 8, 2, tzinfo=UTC),
                rom_file=f"{name}.OsirisROM",
                image_sha256=hashlib.sha256(image).hexdigest(),
            ),
        ],
        reference_images=[
            ReferenceImage(
                name="reference",
                image_sha256=hashlib.sha256(image[::-1]).hexdigest(),
                rom_file="sub/reference.bin",
            )
        ],
    )


def _resolver(registry: TuneRevisionRegistry, **kwargs: object) -> RevisionImageResolver:
    declaration = load_fuel_comp_declaration(PROFILE)
    return RevisionImageResolver(
        registry,
        load_rom_map(PROFILE),
        declaration.rom,
        slot=declaration.program.map_slot,
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_revision_resolves_from_its_osiris_file_with_every_table(tmp_path: Path) -> None:
    image = _image()
    (tmp_path / "synthetic-a.OsirisROM").write_bytes(osiris_container(image))
    calibration = _resolver(_registry(image), rom_dirs=[tmp_path]).resolve("synthetic-a")
    assert calibration.image_sha256 == hashlib.sha256(image).hexdigest()
    assert calibration.image_format == "osirisrom"
    assert calibration.load_axis_raw == (*range(0x10, 0x88, 8), 0x90)
    assert calibration.rpm_breakpoints == tuple(4 * b for b in calibration.rpm_axis_raw)
    assert len(calibration.fuel_comp) == 16 and all(len(r) == 16 for r in calibration.fuel_comp)
    assert calibration.fuel_comp[0][0] == image[0xE0660]
    assert calibration.k == 28715  # synthetic_image's slot K
    assert calibration.purge_gain == 128
    curves = calibration.curves
    assert curves.area_breakpoints[:3] == (0, 100, 200)
    assert (curves.rpm_multiplier, curves.idle_air_cap, curves.limp_home_area) == (150, 1000, 700)
    assert curves.reciprocal_numerator == 0x00DC1D00
    assert calibration.table_ids["fuel_comp"] == "fuel_compensation"
    assert calibration.provenance()["source_file"] == "synthetic-a.OsirisROM"


def test_a_plain_image_named_anything_resolves_by_hash(tmp_path: Path) -> None:
    image = _image()
    path = tmp_path / "an-ecu-read.bin"
    path.write_bytes(image)
    calibration = _resolver(_registry(image), image_files=[path]).resolve("synthetic-a")
    assert calibration.image_format == "plain"
    assert calibration.source_path == path


def test_a_reference_image_resolves_by_its_relative_rom_file(tmp_path: Path) -> None:
    image = _image()
    reversed_image = image[::-1]
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "reference.bin").write_bytes(reversed_image)
    with pytest.raises(FuelCompRomRefusalError) as caught:
        _resolver(_registry(image), rom_dirs=[tmp_path]).resolve("reference")
    # Found by its relative path and hash-matched; refused only because the
    # reversed bytes are not this program.
    assert caught.value.reason == "wrong_program"


def test_a_file_under_the_right_name_with_the_wrong_bytes_is_refused(tmp_path: Path) -> None:
    image = _image()
    other = bytearray(image)
    other[0xE0660] ^= 0xFF  # one Fuel Comp cell
    (tmp_path / "synthetic-a.OsirisROM").write_bytes(osiris_container(bytes(other)))
    with pytest.raises(FuelCompRomRefusalError) as caught:
        _resolver(_registry(image), rom_dirs=[tmp_path]).resolve("synthetic-a")
    assert caught.value.reason == "image_hash_mismatch"
    assert "not synthetic-a's recorded image_sha256" in caught.value.message


def test_a_missing_file_is_refused_naming_where_it_looked(tmp_path: Path) -> None:
    image = _image()
    with pytest.raises(FuelCompRomRefusalError) as caught:
        _resolver(_registry(image), rom_dirs=[tmp_path]).resolve("synthetic-a")
    assert caught.value.reason == "image_not_found"
    assert str(tmp_path) in caught.value.message and ROM_DIRS_ENV in caught.value.message


def test_a_revision_without_a_recorded_image_is_refused_by_name(tmp_path: Path) -> None:
    image = _image()
    resolver = _resolver(_registry(image), rom_dirs=[tmp_path])
    with pytest.raises(FuelCompRomRefusalError) as caught:
        resolver.resolve("no-image")
    assert caught.value.reason == "no_surviving_image"
    assert "no-image" in caught.value.message
    with pytest.raises(FuelCompRomRefusalError) as caught:
        resolver.resolve("never-recorded")
    assert caught.value.reason == "revision_unknown"


@pytest.mark.parametrize(
    ("image_kwargs", "match"),
    [
        ({"literal": 0x00DC1D01}, "fill_reciprocal_numerator"),
        ({"calibration_id": b"1ZS04C00000"}, "no map definition for calibration 1ZS04C00000"),
    ],
    ids=["program-literal", "calibration-id"],
)
def test_another_program_is_refused(
    tmp_path: Path, image_kwargs: dict[str, object], match: str
) -> None:
    image = _image(**image_kwargs)  # type: ignore[arg-type]
    (tmp_path / "synthetic-a.OsirisROM").write_bytes(osiris_container(image))
    with pytest.raises(FuelCompRomRefusalError) as caught:
        _resolver(_registry(image), rom_dirs=[tmp_path]).resolve("synthetic-a")
    assert caught.value.reason == "wrong_program"
    assert match in caught.value.message


def test_a_falling_curve_in_the_image_is_refused_not_used(tmp_path: Path) -> None:
    image = load_byte_image(fill_low=tuple(30000 - 1000 * i for i in range(16)))
    path = tmp_path / "x.bin"
    path.write_bytes(image)
    with pytest.raises(FuelCompRomRefusalError) as caught:
        _resolver(_registry(image), image_files=[path]).resolve("synthetic-a")
    assert caught.value.reason == "curves_not_monotone"
    assert "fill_low falls" in caught.value.message


def test_an_unreadable_file_under_the_right_name_is_refused(tmp_path: Path) -> None:
    image = _image()
    (tmp_path / "synthetic-a.OsirisROM").write_bytes(b"not an image at all")
    with pytest.raises(FuelCompRomRefusalError) as caught:
        _resolver(_registry(image), rom_dirs=[tmp_path]).resolve("synthetic-a")
    assert caught.value.reason == "image_unreadable"
    assert "unrecognised file of 19 bytes" in caught.value.message


@pytest.mark.parametrize(
    ("field", "value"),
    [("area_curve", "no_such_table"), ("fuel_comp_kind", "no_such_kind")],
)
def test_a_table_the_map_does_not_place_is_refused(tmp_path: Path, field: str, value: str) -> None:
    image = _image()
    (tmp_path / "synthetic-a.OsirisROM").write_bytes(osiris_container(image))
    declaration = load_fuel_comp_declaration(PROFILE)
    resolver = RevisionImageResolver(
        _registry(image),
        load_rom_map(PROFILE),
        declaration.rom.model_copy(update={field: value}),
        rom_dirs=[tmp_path],
        slot=declaration.program.map_slot,
    )
    with pytest.raises(FuelCompRomRefusalError) as caught:
        resolver.resolve("synthetic-a")
    assert caught.value.reason == "table_missing"
    assert value in caught.value.message


@pytest.mark.parametrize("table_id", ["fill_curve_high", "fuel_comp_flow_axis"])
def test_a_table_the_map_does_not_verify_is_refused(tmp_path: Path, table_id: str) -> None:
    image = _image()
    (tmp_path / "synthetic-a.OsirisROM").write_bytes(osiris_container(image))
    rom_map = load_rom_map(PROFILE)
    tables = [
        t.model_copy(update={"confidence": "inferred"}) if t.id == table_id else t
        for t in rom_map.tables
    ]
    declaration = load_fuel_comp_declaration(PROFILE)
    resolver = RevisionImageResolver(
        _registry(image),
        rom_map.model_copy(update={"tables": tables}),
        declaration.rom,
        rom_dirs=[tmp_path],
        slot=declaration.program.map_slot,
    )
    with pytest.raises(FuelCompRomRefusalError) as caught:
        resolver.resolve("synthetic-a")
    assert caught.value.reason == "table_unverified"
    assert table_id in caught.value.message


def test_refusals_are_cached_and_raised_again(tmp_path: Path) -> None:
    image = _image()
    resolver = _resolver(_registry(image), rom_dirs=[tmp_path])
    for _ in range(2):
        with pytest.raises(FuelCompRomRefusalError):
            resolver.resolve("synthetic-a")


def test_rom_dirs_come_only_from_the_caller_or_the_environment() -> None:
    assert rom_dirs_from_env({}) == []
    import os

    joined = os.pathsep.join(["/a", "", "/b c"])
    assert rom_dirs_from_env({ROM_DIRS_ENV: joined}) == [Path("/a"), Path("/b c")]


# -- the operator's images (private; skip by name) ---------------------------------


def _operator_resolver() -> RevisionImageResolver:
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    dirs = sorted({OPERATOR_ROMS[name].parent for name in ("k106", "r20260920", "r20261004")})
    return _resolver(registry, rom_dirs=dirs)


@pytest.mark.parametrize(
    ("revision", "fixture", "load_axis_ends"),
    [
        ("ZS44A-k106", "k106", (16, 144)),
        ("ZS44A-20260920", "r20260920", (16, 144)),
        ("ZS44A-20261004", "r20261004", (16, 255)),
    ],
)
def test_the_recorded_revisions_resolve_from_the_uprev_files(
    revision: str, fixture: str, load_axis_ends: tuple[int, int]
) -> None:
    if not OPERATOR_ROMS[fixture].exists():
        pytest.skip(f"private ROM {OPERATOR_ROMS[fixture]} is not on this machine: not checked")
    calibration = _operator_resolver().resolve(revision)
    axis = calibration.load_axis_raw
    assert (axis[0], axis[-1]) == load_axis_ends
    assert all(b > a for a, b in zip(axis, axis[1:], strict=False))
    assert calibration.rpm_axis_raw[0] * 50 == 400  # 0xCACD: 400..6,400 rpm
    assert calibration.k == 28715  # K 1.06 in all three (rom_map.yaml k_constant)
    assert calibration.curves.reciprocal_numerator == 0x00DC1D00


@pytest.mark.parametrize("revision", ["ZS44A-k105", "ZS44A-osiris-initial", "ZS04C-stock"])
def test_revisions_with_no_surviving_image_refuse_by_name(revision: str) -> None:
    with pytest.raises(FuelCompRomRefusalError) as caught:
        _operator_resolver().resolve(revision)
    assert caught.value.reason == "no_surviving_image"
    assert revision in caught.value.message
