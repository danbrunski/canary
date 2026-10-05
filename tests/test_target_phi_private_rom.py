"""E02.2 / E02.3 private-ROM checks: the twin's table from the real image.

Kept apart from ``tests/test_target_phi.py`` on purpose (E02.3): those
committed telemetry and lookup-vector regressions must never pass *because*
a ROM was present, and these must never pass *because* it was absent. Every
test here skips with a named reason when the operator's ZS44A-20260920 image
is not on this machine — a skip, never a pass.

The image goes through the W08 ROM library (stored in a temporary library,
fetched back by its plain-image sha256), never read straight off disk into
the twin, so the path exercised is the one a deployment uses. No ROM bytes
are committed; nothing is written under the repo.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.analysis.target_phi import (
    TwinRefusalError,
    evaluate_session,
    load_twin_definition,
    resolve_table,
)
from src.control.rom_library import RomLibrary
from src.vehicle.tune_revisions import load_tune_revisions
from tests.rom_fixtures import PROFILE, operator_image
from tests.test_target_phi import EXPECT, IMAGE_SHA, _table, _viewer, fixture_frame

#: The 2026-10-04 flash read, byte-identical to the decoded
#: ZS44A-Tuned-20260920.OsirisROM (docs/skunkworks/uprev-backup-20261004.md).
ECU_READ = Path.home() / "ecu-re" / "roms" / "ZS44A-20260920.ecuread.bin"


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> RomLibrary:
    image = ECU_READ.read_bytes() if ECU_READ.exists() else operator_image("r20260920")
    if image is None:
        pytest.skip(
            "private ROM ZS44A-20260920 (plain sha256 a46baa24...be50) is not on this "
            "machine: the image-backed checks did not run"
        )
    if hashlib.sha256(image).hexdigest() != IMAGE_SHA:
        pytest.skip("the local ZS44A-20260920 file is not the expected plain image")
    root = tmp_path_factory.mktemp("rom_library")
    lib = RomLibrary(
        root,
        profile_dir=PROFILE,
        registry=lambda: load_tune_revisions(PROFILE / "tune_revisions.yaml"),
    )
    lib.add_image(image, source="upload_plain", filename="ZS44A-20260920.bin")
    return lib


def test_the_session_revision_resolves_to_the_library_image(library: RomLibrary) -> None:
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    revision = registry.for_session(EXPECT["source_session"])
    assert revision is not None and revision.revision == EXPECT["calibration"]["revision"]
    assert revision.image_sha256 == IMAGE_SHA
    assert library.revision_match(IMAGE_SHA)["revision"] == revision.revision


def test_slot_1_from_the_image_equals_the_committed_table(library: RomLibrary) -> None:
    definition, _ = load_twin_definition(PROFILE)
    image = library.image(IMAGE_SHA)
    table = resolve_table(
        image, library.rom_map(), definition, viewer=_viewer(), expected_sha256=IMAGE_SHA, slot=1
    )
    committed = _table(definition)
    assert table.cells == committed.cells
    assert table.row_breakpoints == committed.row_breakpoints
    assert table.column_breakpoints == committed.column_breakpoints
    # The X relation's scaler, and the final-command constants the twin models.
    assert table.load_point_scaler == EXPECT["calibration"]["load_point_scaler"]
    assert table.final_command_constants_checked
    assert table.revision == EXPECT["calibration"]["revision"]


def test_the_image_refuses_an_unverified_slot_and_another_hash(library: RomLibrary) -> None:
    definition, _ = load_twin_definition(PROFILE)
    image = library.image(IMAGE_SHA)
    with pytest.raises(TwinRefusalError) as refused:
        resolve_table(
            image,
            library.rom_map(),
            definition,
            viewer=_viewer(),
            expected_sha256=IMAGE_SHA,
            slot=2,
        )
    assert refused.value.reason == "table_unverified"
    with pytest.raises(TwinRefusalError) as refused:
        resolve_table(
            image, library.rom_map(), definition, viewer=_viewer(), expected_sha256="0" * 64, slot=1
        )
    assert refused.value.reason == "image_hash_mismatch"


def test_the_fixture_accounts_identically_from_the_image(library: RomLibrary) -> None:
    definition, sha = load_twin_definition(PROFILE)
    table = resolve_table(
        library.image(IMAGE_SHA),
        library.rom_map(),
        definition,
        viewer=_viewer(),
        expected_sha256=IMAGE_SHA,
        slot=1,
    )
    result = evaluate_session(fixture_frame(), definition, table, definition_sha256=sha)
    assert result["accounting"]["states"] == EXPECT["accounting"]["states"]
    assert result["provenance"]["table_source"] == "rom_library"
    # Checked against the image, so only the two vehicle-side blockers remain.
    assert result["qualification_blockers"] == [
        "final_command_path_unverified",
        "ram_copy_unverified",
    ]
