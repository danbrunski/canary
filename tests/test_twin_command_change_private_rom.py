"""E02.6 private-ROM checks: what-ifs over the operator's real images.

Kept apart from ``tests/test_twin_command_change.py`` (committed vectors) on
purpose, as E02.3 asks: these never pass *because* an image was absent. Each
test skips with a named reason when an image is not on this machine. Images go
through a temporary W08 :class:`~src.control.rom_library.RomLibrary` and are
fetched back by plain-image sha256, as a deployment would; no ROM bytes are
committed and nothing is written under the repo.

- ZS44A-20260920 -> ZS44A-20261004 (the Fuel Comp restore): Target AFR is
  byte-identical, so the command is unchanged at equal lookup inputs, and the
  changed Fuel Comp load axis and Map 1 cells are named as not reproduced.
- ZS44A-k106 -> ZS44A-20260920: exactly the one Fuel Targets cell the
  committed cell edit set names, with nothing else changed but checksums.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.analysis.ecu_application import load_application_model
from src.analysis.rom_targets import Patch
from src.analysis.target_phi import ResolvedTable, load_twin_definition, resolve_table
from src.analysis.twin_command_change import (
    change_from_cell_edits,
    change_from_images,
    predict_command_change,
    record_command_change,
)
from src.control.rom_library import RomLibrary
from src.evidence import MemoryLedger, Prediction
from src.vehicle.tune_revisions import load_tune_revisions
from tests.rom_fixtures import PROFILE, operator_image
from tests.test_target_phi import _viewer
from tests.test_twin_command_change import PATCH, SLUG, _vehicle

#: Revision -> (operator file key, plain-image sha256 from tune_revisions.yaml).
IMAGES = {
    "ZS44A-k106": ("k106", "e01cc0cb45d5beb879f82931be3054c79b7be3955220b3d93045deaabd23f18e"),
    "ZS44A-20260920": (
        "r20260920",
        "a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50",
    ),
    "ZS44A-20261004": (
        "r20261004",
        "5587f7e1e6dbd0fcb9ffe0c857cc0cafebbe11bf306636c0f3265b903254d253",
    ),
}


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> RomLibrary:
    lib = RomLibrary(
        tmp_path_factory.mktemp("rom_library"),
        profile_dir=PROFILE,
        registry=lambda: load_tune_revisions(PROFILE / "tune_revisions.yaml"),
    )
    for revision, (key, sha) in IMAGES.items():
        image = operator_image(key)
        if image is not None and hashlib.sha256(image).hexdigest() == sha:
            lib.add_image(image, source="upload_plain", filename=f"{revision}.bin")
    return lib


def _image(library: RomLibrary, revision: str) -> bytes:
    _key, sha = IMAGES[revision]
    try:
        return library.image(sha)
    except Exception:  # RomNotFoundError: absent or not the expected bytes
        pytest.skip(
            f"private ROM {revision} (plain sha256 {sha[:8]}...) is not on this machine: "
            "the image-backed what-if did not run"
        )


def _table(library: RomLibrary, revision: str) -> tuple[bytes, ResolvedTable]:
    image = _image(library, revision)
    definition, _ = load_twin_definition(PROFILE)
    return image, resolve_table(
        image,
        library.rom_map(),
        definition,
        viewer=_viewer(),
        application_model=load_application_model(PROFILE),
        expected_sha256=IMAGES[revision][1],
        slot=1,
    )


def test_the_fuel_comp_restore_predicts_no_command_change_and_names_what_it_changed(
    library: RomLibrary, tmp_path: Path
) -> None:
    base_image, base = _table(library, "ZS44A-20260920")
    target_image, target = _table(library, "ZS44A-20261004")
    definition, _ = load_twin_definition(PROFILE)
    change = change_from_images(
        base, target, base_image=base_image, target_image=target_image, rom_map=library.rom_map()
    )
    assert change.changed_cells() == []
    assert [(r.region.key(), r.direction) for r in predict_command_change(change, definition)] == [
        ("ordinary:r0-7:c0-7", "no_change"),
        ("override:r0-7", "no_change"),
    ]
    joined = " | ".join(change.not_reproduced)
    assert "Fuel Compensation load axis" in joined and "Fuel Compensation (Map 1)" in joined
    assert "Fuel Targets" not in joined
    ledger = MemoryLedger(tmp_path / "memory.jsonl", vehicle=SLUG)
    ids = record_command_change(
        ledger,
        change,
        definition,
        vehicle=_vehicle(base),
        now=datetime(2026, 10, 4, 22, tzinfo=UTC),
    )
    for pid in ids:
        record = ledger.get(pid)
        assert isinstance(record, Prediction) and record.what_if is not None
        assert record.direction == "no_change"
        assert record.what_if.not_reproduced == change.not_reproduced
        assert record.what_if.target_image_sha256 == IMAGES["ZS44A-20261004"][1]


def test_the_k106_to_20260920_images_change_exactly_the_cell_the_edit_set_names(
    library: RomLibrary,
) -> None:
    base_image, base = _table(library, "ZS44A-k106")
    target_image, target = _table(library, "ZS44A-20260920")
    definition, _ = load_twin_definition(PROFILE)
    by_images = change_from_images(
        base, target, base_image=base_image, target_image=target_image, rom_map=library.rom_map()
    )
    assert by_images.not_reproduced == ()
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    by_edits = change_from_cell_edits(
        base, Patch.model_validate_json(PATCH.read_text()), registry=registry, definition=definition
    )
    assert by_images.target.cells == by_edits.target.cells
    (only,) = predict_command_change(by_images, definition)
    assert (only.region.key(), only.direction) == ("ordinary:r7-7:c4-4", "increase")
    assert only.magnitude is not None and only.magnitude.value == 1 / 128
