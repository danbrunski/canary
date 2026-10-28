"""Which map reads a library image: the active profile's on a Pi, the one that
names the image's calibration on a desk or a companion (daemon.rom_map_lookup).

A desk scoped to the 2008 opens the 2013's ROM; the 2013's own map has to read
it, and that profile's flash history and typical values go with it. A Pi reads
only its own truck's ROMs and keeps the scoped map.
"""

from __future__ import annotations

import shutil
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.analysis.rom_image import checksum_values, read_calibration_id, sha256_hex
from src.analysis.rom_map import ROM_MAP_FILE, RomMap, load_rom_map
from src.config import CanaryConfig
from src.control.rom_library import RomLibrary, RomMapLookupError
from src.dashboard.routes.rom_editor import register_rom_editor_routes
from src.dashboard.routes.rom_library import register_rom_library_routes
from src.paths import DATA_DIR
from src.yaml_load import load_yaml
from tests.configs import isolated_config
from tests.rom_fixtures import IMAGE_SIZE, PROFILE, synthetic_image

AUTH = {"Authorization": "Bearer secret"}
OCTET = {**AUTH, "Content-Type": "application/octet-stream"}
PROFILE_2013 = DATA_DIR / "nissan-xterra-2013"
CALIBRATION_2013 = "19CF2B00000"


def image_2013(calibration_id: str = CALIBRATION_2013) -> bytes:
    """A 1 MiB image the 2013's map applies to, with valid checksum words."""
    rom_map = load_rom_map(PROFILE_2013)
    image = bytearray(b"\x80" * IMAGE_SIZE)
    location = rom_map.calibration_id
    image[location.offset : location.offset + location.length] = calibration_id.encode()
    for offset, value in rom_map.checksums.markers.items():
        struct.pack_into(">I", image, offset, value)
    total, xor = checksum_values(bytes(image), rom_map.checksums)
    struct.pack_into(">I", image, rom_map.checksums.sum_offset, total)
    struct.pack_into(">I", image, rom_map.checksums.xor_offset, xor)
    return bytes(image)


def library(tmp_path: Path, lookup: str, *, profiles_root: Path | None = None) -> RomLibrary:
    return RomLibrary(
        tmp_path / "library",
        profile_dir=PROFILE if profiles_root is None else profiles_root / PROFILE.name,
        registry=lambda: None,
        map_lookup=lookup,  # type: ignore[arg-type]
        profiles_root=profiles_root,
    )


# -- the tracked data ----------------------------------------------------------------------------


def test_no_calibration_is_in_two_tracked_maps() -> None:
    """The desk's lookup refuses a calibration two maps claim; the tracked
    profiles never give it one to refuse."""
    seen: dict[str, Path] = {}
    for path in sorted(DATA_DIR.glob(f"*/{ROM_MAP_FILE}")):
        for calibration in load_yaml(path.read_text())["calibration_ids"]:
            assert calibration not in seen, f"{calibration} in {seen[calibration]} and {path}"
            seen[calibration] = path
    assert {"1ZS44A00000", CALIBRATION_2013} <= set(seen)


# -- RomLibrary.map_for -------------------------------------------------------------------------


def test_a_pi_reads_every_image_with_its_own_map(tmp_path: Path) -> None:
    """``active_profile``: the 2013's ROM on the 2008's Pi is unmapped, as before."""
    lib = library(tmp_path, "active_profile")
    match = lib.map_for(image_2013())
    assert match.profile_dir == PROFILE
    assert match.calibration_id == CALIBRATION_2013
    assert match.reason == f"no map definition for calibration {CALIBRATION_2013}"
    assert [d for d, _ in lib.profile_maps()] == [PROFILE]


def test_a_desk_reads_each_image_with_the_map_that_names_its_calibration(
    tmp_path: Path,
) -> None:
    lib = library(tmp_path, "calibration_id")
    other = lib.map_for(image_2013())
    assert (other.profile_dir, other.reason) == (PROFILE_2013, None)
    assert isinstance(other.rom_map, RomMap) and other.rom_map.calibration_ids == [CALIBRATION_2013]
    own = lib.map_for(synthetic_image())
    assert (own.profile_dir, own.calibration_id, own.reason) == (PROFILE, "1ZS44A00000", None)


def test_a_calibration_no_map_names_stays_with_the_active_profile(tmp_path: Path) -> None:
    lib = library(tmp_path, "calibration_id")
    match = lib.map_for(image_2013(calibration_id="1ZZZZZ00000"))
    assert match.profile_dir == PROFILE
    assert match.reason == "no map definition for calibration 1ZZZZZ00000"


def test_two_maps_naming_one_calibration_is_refused_by_both_files(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / PROFILE.name).mkdir(parents=True)
    for name in ("xterra-2013-a", "xterra-2013-b"):
        (root / name).mkdir()
        shutil.copy(PROFILE_2013 / ROM_MAP_FILE, root / name / ROM_MAP_FILE)
    (root / "_archive").mkdir()
    shutil.copy(PROFILE_2013 / ROM_MAP_FILE, root / "_archive" / ROM_MAP_FILE)
    lib = library(tmp_path, "calibration_id", profiles_root=root)
    with pytest.raises(RomMapLookupError) as refused:
        lib.map_for(image_2013())
    message = str(refused.value)
    assert "xterra-2013-a/rom_map.yaml" in message and "xterra-2013-b/rom_map.yaml" in message
    assert "_archive" not in message


def test_the_flash_history_is_the_image_profiles(tmp_path: Path) -> None:
    """Another profile's tracked tune_revisions.yaml names its image; the
    active vehicle's registry (here empty) is not asked."""
    root = tmp_path / "data"
    (root / PROFILE.name).mkdir(parents=True)
    other = root / "xterra-2013"
    other.mkdir()
    shutil.copy(PROFILE_2013 / ROM_MAP_FILE, other / ROM_MAP_FILE)
    image = image_2013()
    (other / "tune_revisions.yaml").write_text(
        "schema_version: 1\nrevisions:\n  - revision: 2013-stock\n"
        f"    calibration_id: {CALIBRATION_2013}\n    flashed_at: 2013-01-01T00:00:00Z\n"
        f"    image_sha256: {sha256_hex(image)}\n"
    )
    lib = library(tmp_path, "calibration_id", profiles_root=root)
    entry, _ = lib.add_image(image, source="upload_plain", filename="2013.bin")
    assert entry["map_profile"] == "xterra-2013" and entry["map_reason"] is None
    assert entry["checksum"]["ok"] is True
    assert (entry["revision_match"]["status"], entry["revision_match"]["revision"]) == (
        "matched",
        "2013-stock",
    )
    assert lib.revision_match(entry["sha256"])["revision"] == "2013-stock"


# -- config -------------------------------------------------------------------------------------


def test_companion_mode_looks_maps_up_by_calibration_unless_told_otherwise(
    tmp_path: Path,
) -> None:
    assert isolated_config(tmp_path).rom_map_lookup == "active_profile"
    companion = isolated_config(tmp_path, companion={"enabled": True})
    assert companion.rom_map_lookup == "calibration_id"
    # A round trip keeps it unset, so it still follows the mode.
    again = CanaryConfig.model_validate(companion.model_dump(mode="python"))
    assert again.rom_map_lookup == "calibration_id"
    pinned = isolated_config(
        tmp_path, companion={"enabled": True}, daemon={"rom_map_lookup": "active_profile"}
    )
    assert pinned.rom_map_lookup == "active_profile"


# -- the routes ---------------------------------------------------------------------------------


def _client(tmp_path: Path, lookup: str) -> TestClient:
    config: CanaryConfig = isolated_config(
        tmp_path,
        daemon={"afr_calibration": PROFILE / "afr_calibration.yaml", "rom_map_lookup": lookup},
    )
    app = FastAPI()
    register_rom_library_routes(app, config=config, resolved_control_token="secret")
    register_rom_editor_routes(app, config=config, resolved_control_token="secret")
    return TestClient(app)


@pytest.fixture
def desk(tmp_path: Path) -> Iterator[TestClient]:
    with _client(tmp_path, "calibration_id") as client:
        yield client


def _upload(client: TestClient, image: bytes) -> dict[str, Any]:
    """The stored entry."""
    response = client.post(
        "/control/rom/library", params={"filename": "rom.bin"}, content=image, headers=OCTET
    )
    assert response.status_code == 200, response.text
    entry: dict[str, Any] = response.json()
    return entry


def test_a_desk_opens_the_2013_rom_with_the_2013_map(desk: TestClient) -> None:
    entry = _upload(desk, image_2013())
    sha = entry["sha256"]
    assert entry["map_profile"] == "nissan-xterra-2013" and entry["map_reason"] is None
    assert entry["calibration_id"] == CALIBRATION_2013 and entry["checksum"]["ok"] is True
    tables = desk.get(f"/control/rom/library/{sha}/tables", headers=AUTH).json()
    assert tables["reason"] is None
    assert {t["id"] for t in tables["tables"]} == {t.id for t in load_rom_map(PROFILE_2013).tables}
    edit = {"table": "fuel_compensation", "cells": [[0, 0]], "add_raw": 1}
    preview = desk.post(
        f"/control/rom/library/{sha}/edits/preview",
        json={"label": "2013 FC +1", "edits": [edit]},
        headers=AUTH,
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert [(c["slot"], c["old"], c["new"]) for c in body["provenance"]["cells"]] == [
        (None, 0x80, 0x81)
    ]
    assert body["sim_base"]["reason"] == "sim_kit_undeclared"
    assert body["export"]["reason"] == "table_not_verified"
    # Its typical values are the 2013's, which declares none; the active
    # vehicle's are still there without the image.
    typical = desk.get("/control/rom/editor/typical-values", params={"sha": sha}, headers=AUTH)
    assert typical.status_code == 409
    assert typical.json()["detail"]["error"] == "typical_values_undeclared"
    own = desk.get("/control/rom/editor/typical-values", headers=AUTH)
    assert own.status_code == 200 and own.json()["profile"] == PROFILE.name


def test_the_pi_still_refuses_another_vehicles_rom_its_map_does_not_describe(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, "active_profile") as pi:
        entry = _upload(pi, image_2013())
        assert entry["map_profile"] is None
        assert entry["map_reason"] == f"no map definition for calibration {CALIBRATION_2013}"
        tables = pi.get(f"/control/rom/library/{entry['sha256']}/tables", headers=AUTH).json()
        assert tables["tables"] == [] and tables["reason"] == entry["map_reason"]
        assert read_calibration_id(image_2013(), load_rom_map(PROFILE)) == CALIBRATION_2013
