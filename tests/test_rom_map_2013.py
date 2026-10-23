"""The 2013 Xterra's ROM map (data/nissan-xterra-2013/rom_map.yaml).

A synthetic image pins what the map declares, so the shipped file is checked
on every machine; the truck's own ECU read, outside Git, is the known answer
and the test that uses it skips when it is absent.
"""

from __future__ import annotations

import hashlib
import struct

import numpy as np
import pytest

from src.analysis.rom_image import (
    checksum_values,
    checksum_verdict,
    map_applies,
    read_calibration_id,
    tables_payload,
)
from src.analysis.rom_map import RomMap, load_rom_map
from src.control.rom_backup import ROM_BACKUP_FILE, load_backup_recipe
from tests.rom_fixtures import IMAGE_SIZE, REPO, XTERRA_2013_READ, XTERRA_2013_SHA256

PROFILE_2013 = REPO / "data" / "nissan-xterra-2013"
CALIBRATION_ID = "19CF2B00000"
#: The 2008's Target AFR breakpoints (data/nissan-xterra-2008/rom_map.yaml).
BFS_2008 = bytes.fromhex("34445D646C6E7F80")
RPM_2008 = bytes.fromhex("1820283848505868")


@pytest.fixture(scope="module")
def rom_map() -> RomMap:
    return load_rom_map(PROFILE_2013)


def _synthetic(rom_map: RomMap, calibration_id: str = CALIBRATION_ID) -> bytes:
    """A 1 MiB image the map applies to, with valid checksum words."""
    image = bytearray(b"\x80" * IMAGE_SIZE)
    location = rom_map.calibration_id
    image[location.offset : location.offset + location.length] = calibration_id.encode()
    for offset, value in rom_map.checksums.markers.items():
        struct.pack_into(">I", image, offset, value)
    total, xor = checksum_values(bytes(image), rom_map.checksums)
    struct.pack_into(">I", image, rom_map.checksums.sum_offset, total)
    struct.pack_into(">I", image, rom_map.checksums.xor_offset, xor)
    return bytes(image)


def test_the_map_describes_one_stock_calibration_with_no_slots(rom_map: RomMap) -> None:
    assert rom_map.calibration_ids == [CALIBRATION_ID]
    assert rom_map.map_slots is None
    assert {table.kind for table in rom_map.tables if table.kind} == {
        "fuel_targets",
        "timing_trim_a",
        "timing_trim_b",
        "fuel_compensation",
        "load_point_scaler",
    }
    # Nothing here has met a known edit or a drive yet.
    assert {table.confidence for table in rom_map.tables} == {"inferred"}


def test_every_table_decodes_from_an_image_the_map_applies_to(rom_map: RomMap) -> None:
    image = _synthetic(rom_map)
    assert checksum_verdict(image, rom_map.checksums)["ok"]
    payload = tables_payload(image, rom_map)
    assert payload["reason"] is None
    assert {table["id"] for table in payload["tables"]} == {t.id for t in rom_map.tables}
    targets = next(t for t in payload["tables"] if t["id"] == "fuel_targets")
    assert len(targets["decoded"]) == 16 and targets["decoded"][0][0] == pytest.approx(14.7)


def test_the_2008_calibration_gets_no_tables_from_it(rom_map: RomMap) -> None:
    image = _synthetic(rom_map, calibration_id="1ZS44A00000")
    reason = map_applies(read_calibration_id(image, rom_map), rom_map)
    assert reason is not None and "1ZS44A00000" in reason
    assert tables_payload(image, rom_map)["tables"] == []


def test_the_2013_read_is_what_the_map_says(rom_map: RomMap) -> None:
    if not XTERRA_2013_READ.exists():
        pytest.skip(f"{XTERRA_2013_READ} is not on this machine")
    image = XTERRA_2013_READ.read_bytes()
    assert hashlib.sha256(image).hexdigest() == XTERRA_2013_SHA256
    assert read_calibration_id(image, rom_map) == CALIBRATION_ID
    assert checksum_verdict(image, rom_map.checksums)["ok"]
    data = np.frombuffer(image, dtype=np.uint8)

    def raw(table_id: str) -> np.ndarray:
        table = rom_map.table(table_id)
        assert table.offset is not None
        return data[table.offset : table.offset + table.size].reshape(table.rows, table.cols)

    # Target AFR: stoichiometric (128) to 11.0 AFR, on axes that contain the
    # 2008's breakpoints.
    targets = raw("fuel_targets")
    assert targets.min() == 128 and targets.max() == 171
    assert (raw("fuel_targets_high_det") == targets).all()
    assert raw("fuel_targets_bfs_axis").tobytes().endswith(BFS_2008)
    assert set(RPM_2008) <= set(raw("fuel_targets_rpm_axis").ravel().tolist())
    # Timing trims: the 2008's 143 flagged cells and its 43-68 masked surface.
    trim = raw("timing_trim_a")
    assert int((trim & 0x80 > 0).sum()) == 143
    assert (trim & 0x7F).min() == 43 and (trim & 0x7F).max() == 68
    assert (raw("timing_trim_b") == trim).all()
    assert raw("timing_trim_rpm_axis").ravel().tolist()[:4] == [0x08, 0x0C, 0x10, 0x18]
    fuel_comp = raw("fuel_compensation")
    assert fuel_comp.min() == 125 and fuel_comp.max() == 139
    assert raw("fuel_comp_flow_axis").ravel().tolist()[-1] == 0xFF
    scaler = rom_map.table("load_point_scaler")
    assert scaler.offset is not None
    assert struct.unpack_from(">H", image, scaler.offset)[0] == 44019


def test_the_shared_backup_recipe_reads_exactly_this_image(rom_map: RomMap) -> None:
    """With the map in place the backup checks the recipe's range against it."""
    recipe = load_backup_recipe(
        REPO / "data" / "platforms" / "nissan-xterra-n50" / ROM_BACKUP_FILE, rom_map
    )
    assert recipe.read.size == rom_map.image_size
