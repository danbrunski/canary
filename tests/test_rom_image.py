"""W08.2 / W08.4 / W08.5: describe, table values and compare, against known answers.

Synthetic images with the real layout always run; the operator's decoded
files (outside Git) are used when present and skipped otherwise.
"""

from __future__ import annotations

import struct

import pytest

from src.analysis.rom_image import (
    ImageFormatError,
    block_stats,
    changed_ranges,
    checksum_values,
    compare_images,
    describe,
    load_image,
    tables_payload,
)
from src.signals.afr import load_afr_calibration
from src.vehicle.tune_revisions import load_tune_revisions
from tests.rom_fixtures import (
    ECU_READ_20261004_SHA256,
    IMAGE_SIZE,
    OPERATOR_BACKUP_DIR,
    PROFILE,
    encrypted_backup_lookalike,
    operator_image,
    osiris_container,
    synthetic_image,
    target_raw,
    with_checksums,
    xterra_map,
)

#: Decoding a real container takes about a second; these run where the
#: operator's files are (``-m slow``, or a bare ``pytest``) and skip elsewhere.
operator_rom = pytest.mark.slow


def _registry():  # type: ignore[no-untyped-def]
    return load_tune_revisions(PROFILE / "tune_revisions.yaml")


def _operator(name: str) -> bytes:
    image = operator_image(name)
    if image is None:
        pytest.skip(f"operator ROM {name} is not on this machine")
    return image


# -- format detection --------------------------------------------------------


def test_plain_image_and_osirisrom_both_load_to_the_same_plain_image() -> None:
    rom_map = xterra_map()
    image = synthetic_image()
    plain = load_image(image, rom_map)
    assert (plain.format, plain.container_sha256, plain.image) == ("plain", None, image)
    container = osiris_container(image)
    osiris = load_image(container, rom_map)
    assert osiris.format == "osirisrom"
    assert osiris.image == image
    assert osiris.container_sha256 is not None and len(osiris.container_sha256) == 64


def test_uprev_encrypted_backup_is_refused_by_name() -> None:
    with pytest.raises(ImageFormatError, match="UpRev encrypted ROM backup"):
        load_image(encrypted_backup_lookalike(), xterra_map())


def test_operator_encrypted_backups_are_refused_by_name() -> None:
    backups = sorted(OPERATOR_BACKUP_DIR.glob("*.bin")) if OPERATOR_BACKUP_DIR.exists() else []
    if not backups:
        pytest.skip("operator ROM backups are not on this machine")
    for path in backups:
        with pytest.raises(ImageFormatError, match="cannot decode"):
            load_image(path.read_bytes(), xterra_map())


@pytest.mark.parametrize("size", [100, IMAGE_SIZE - 1, IMAGE_SIZE + 1, IMAGE_SIZE + 16])
def test_anything_else_is_refused_with_its_size(size: int) -> None:
    with pytest.raises(ImageFormatError, match=f"{size:,} bytes"):
        load_image(bytes(size), xterra_map())


def test_a_broken_osirisrom_is_refused_by_name() -> None:
    container = bytearray(osiris_container(synthetic_image()))
    container[200] ^= 0xFF
    with pytest.raises(ImageFormatError, match="not a readable .OsirisROM"):
        load_image(bytes(container), xterra_map())


# -- describe ----------------------------------------------------------------


def test_describe_synthetic_image() -> None:
    image = synthetic_image()
    result = describe(image, xterra_map(), _registry())
    assert result["calibration_id"] == "1ZS44A00000"
    assert result["size"] == IMAGE_SIZE
    assert result["checksum"]["ok"] is True
    assert result["checksum"]["stored"] == result["checksum"]["computed"]
    assert result["revision_match"] == {
        "revision": None,
        "status": "unrecognised",
        "reference": None,
    }
    assert len(result["blocks"]) == 256
    assert result["blocks"][0x12]["class"] == "erased"  # 0x012000
    assert result["blocks"][0x12]["ff_pct"] == 100.0
    assert result["blocks"][0x30]["class"] == "dense"  # seeded noise
    assert result["map"]["calibration_id"] == "1ZS44A00000"
    assert len(result["map"]["erase_sectors"]) == 16
    assert result["map_slots"]["count"] == 5
    assert result["map_reason"] is None
    names = {r["name"] for r in result["regions"]}
    assert {"Boot vector table", "Map 1: Fuel Compensation (span)", "UpRev map slots"} <= names
    fuel = [t for t in result["tables"] if t["kind"] == "fuel_targets" and t["slot"] == 1]
    assert fuel[0]["offset"] == 0xE0220 and fuel[0]["confidence"] == "verified"


def test_a_flipped_byte_fails_the_checksum_and_is_reported_not_refused() -> None:
    image = bytearray(synthetic_image())
    image[0x0A0000] ^= 0x01
    result = describe(bytes(image), xterra_map(), _registry())
    assert result["checksum"]["ok"] is False
    assert "checksum mismatch" in result["checksum"]["reason"]
    assert result["map"] is not None  # still fully described


def test_another_calibration_gets_identity_and_blocks_but_no_map() -> None:
    image = synthetic_image(calibration_id=b"1ZS04C00000")
    result = describe(image, xterra_map(), _registry())
    assert result["calibration_id"] == "1ZS04C00000"
    assert result["map"] is None
    assert result["map_reason"] == "no map definition for calibration 1ZS04C00000"
    assert result["regions"] == [] and result["tables"] == [] and result["map_slots"] is None
    assert result["checksum"] is None
    assert len(result["blocks"]) == 256 and len(result["sha256"]) == 64


def test_vectorised_checksum_equals_the_editor_definition() -> None:
    rom_map = xterra_map()
    for image in (synthetic_image(), synthetic_image(seed=3, checksums=False)):
        assert checksum_values(image, rom_map.checksums) == rom_map.checksums.values(image)
    broken = bytearray(synthetic_image())
    struct.pack_into(">I", broken, 0x20008, 0)
    with pytest.raises(ValueError, match="Unsupported ROM checksum style"):
        checksum_values(bytes(broken), rom_map.checksums)


def test_block_classes() -> None:
    image = bytearray(b"\xff" * (4 * 4096))
    image[4096 : 4096 + 100] = bytes(100)  # mostly erased
    image[2 * 4096 : 3 * 4096] = bytes(range(256)) * 16  # 8 bits/byte
    image[3 * 4096 : 4 * 4096] = bytes([0, 1, 2, 3]) * 1024  # 2 bits/byte
    classes = [b["class"] for b in block_stats(bytes(image))]
    assert classes == ["erased", "sparse", "dense", "data"]


@operator_rom
def test_known_answer_20260920_and_the_ecu_read() -> None:
    image = _operator("r20260920")
    result = describe(image, xterra_map(), _registry())
    assert result["sha256"] == ECU_READ_20261004_SHA256
    assert result["crc32"] == "95b993c5"
    assert result["checksum"]["ok"] is True
    assert result["revision_match"]["revision"] == "ZS44A-20260920"
    assert result["revision_match"]["status"] == "matched"


@operator_rom
def test_known_answer_k106_and_the_uprev_base() -> None:
    k106 = describe(_operator("k106"), xterra_map(), _registry())
    assert k106["revision_match"]["revision"] == "ZS44A-k106"
    assert k106["checksum"]["ok"] is True
    base = describe(_operator("base"), xterra_map(), _registry())
    assert base["revision_match"] == {
        "revision": None,
        "status": "reference",
        "reference": "ZS44A-uprev-base",
    }
    assert base["checksum"]["ok"] is True


# -- table values (W08.4) ----------------------------------------------------


def _table(payload: dict, table_id: str, slot: int | None) -> dict:  # type: ignore[type-arg]
    (table,) = [t for t in payload["tables"] if t["id"] == table_id and t["slot"] == slot]
    return table


def _assert_fuel_targets_equal_calibration(image: bytes, revision: str) -> None:
    payload = tables_payload(image, xterra_map())
    fuel = _table(payload, "fuel_targets", 1)
    calibration = load_afr_calibration(PROFILE / "afr_calibration.yaml")
    (target,) = [t for t in calibration.target_maps if t.revision == revision]
    assert fuel["raw"] == target.raw_values
    for got_row, want_row in zip(fuel["decoded"], target.afr, strict=True):
        assert got_row == pytest.approx(want_row, abs=1e-6)
    assert fuel["axes"]["rows"]["raw"] == target.raw_rpm_breakpoints
    assert fuel["axes"]["rows"]["decoded"] == pytest.approx(target.rpm_breakpoints)
    assert fuel["axes"]["cols"]["raw"] == target.raw_load_breakpoints
    assert fuel["axes"]["cols"]["decoded"] is None  # no BFS scale is declared
    assert fuel["confidence"] == "verified"


def test_tables_synthetic_fuel_targets_and_k() -> None:
    image = synthetic_image(k=27089)
    _assert_fuel_targets_equal_calibration(image, "ZS44A-20260920")
    payload = tables_payload(image, xterra_map())
    assert payload["map_slots"] == [1, 2, 3, 4, 5]
    for slot in range(1, 6):
        k = _table(payload, "k_constant", slot)
        assert k["raw"] == [[27089]] and k["decoded"] is None
        assert k["confidence"] == "inferred"
    assert _table(payload, "fuel_targets_maps_2_5", 3)["raw"] == target_raw("ZS44A-k106")


def test_tables_need_a_map() -> None:
    payload = tables_payload(synthetic_image(calibration_id=b"1ZS04C00000"), xterra_map())
    assert payload["tables"] == []
    assert payload["reason"] == "no map definition for calibration 1ZS04C00000"


@operator_rom
def test_tables_known_answer_20260920_fuel_targets_cell_for_cell() -> None:
    _assert_fuel_targets_equal_calibration(_operator("r20260920"), "ZS44A-20260920")


@operator_rom
def test_tables_known_answer_k() -> None:
    for name, expected in (("k106", 28715), ("r20260920", 28715), ("base", 27089)):
        payload = tables_payload(_operator(name), xterra_map())
        k = _table(payload, "k_constant", 1)
        assert k["raw"] == [[expected]], name
        assert k["confidence"] == "inferred"


# -- compare (W08.5) ---------------------------------------------------------


def test_changed_ranges_merge_within_a_16_byte_gap() -> None:
    a = bytes(256)
    b = bytearray(a)
    for i in (10, 26, 43, 100):  # 26-10 = 16 merges; 43-26 = 17 does not
        b[i] = 1
    assert changed_ranges(a, bytes(b)) == [(10, 26, 2), (43, 43, 1), (100, 100, 1)]


def test_compare_synthetic_labels_and_cells() -> None:
    rom_map = xterra_map()
    a = synthetic_image(k=27089)
    b = bytearray(a)
    b[0xE025C] = 0xA2  # Fuel Targets Map 1, row 7, col 4 (0xA3 in 20260920)
    for slot in range(2, 6):
        struct.pack_into(">H", b, 0xE0000 + (slot - 1) * 0x800 + 0x762, 28715)
    b = bytearray(with_checksums(bytes(b), rom_map))
    result = compare_images(a, bytes(b), rom_map)
    assert result["map_applied"] is True
    labels = [r["labels"] for r in result["ranges"]]
    assert any("Checksum sum word" in item for item in labels)
    fuel = next(r for r in result["ranges"] if "Fuel Targets (Map 1)" in r["labels"])
    (cell,) = fuel["cells"]
    assert (cell["row"], cell["col"], cell["old"], cell["new"]) == (7, 4, 0xA3, 0xA2)
    assert cell["old_decoded"] == pytest.approx(1881.6 / 0xA3)
    k_ranges = [r for r in result["ranges"] if any(lbl.startswith("K (") for lbl in r["labels"])]
    assert [r["cells"][0]["slot"] for r in k_ranges] == [2, 3, 4, 5]
    assert all(r["cells"][0]["new_decoded"] is None for r in k_ranges)


@operator_rom
def test_compare_known_answer_base_to_k106() -> None:
    result = compare_images(_operator("base"), _operator("k106"), xterra_map())
    assert result["changed_bytes"] == 248
    ranges = result["ranges"]
    assert len(ranges) == 9
    assert {"Checksum sum word", "Checksum XOR word"} <= set(ranges[0]["labels"])
    assert "Fuel Compensation rpm axis" in ranges[1]["labels"]
    assert "Map 1: Ignition timing (probable)" in ranges[2]["labels"]
    assert "Fuel Targets (Map 1)" in ranges[3]["labels"]
    assert len(ranges[3]["cells"]) == 10
    assert {"Map 1: Fuel Compensation (span)", "K (injector constant) (Map 1)"} <= set(
        ranges[4]["labels"]
    )
    for slot, item in zip(range(2, 6), ranges[5:], strict=True):
        assert f"K (injector constant) (Map {slot})" in item["labels"]
        assert (item["cells"][0]["old"], item["cells"][0]["new"]) == (27089, 28715)


@operator_rom
def test_compare_known_answer_k106_to_20260920() -> None:
    result = compare_images(_operator("k106"), _operator("r20260920"), xterra_map())
    assert result["changed_bytes"] == 3
    checksums, fuel = result["ranges"]
    assert {"Checksum sum word", "Checksum XOR word"} <= set(checksums["labels"])
    (cell,) = fuel["cells"]
    # Row 7, column 4 (zero-based): the 5200 rpm / BFS 18.20 cell.
    assert (cell["row"], cell["col"]) == (7, 4)
    assert round(cell["old_decoded"], 2) == 11.61
    assert round(cell["new_decoded"], 2) == 11.54
