"""B08.2 private-ROM checks: the context resolves both calibration images
through the W08 library.

The old image (``ZS44A-20260920``, a46baa24...be50: the compressed Fuel Comp
load axis) and the restored one (``ZS44A-20261004``, 5587f7e1...d253: load axis
16..255) are stored in a temporary library and fetched back by plain-image
sha256, the path a deployment uses. Every test skips with a named reason when
the operator's files are not on this machine -- a skip, never a pass. No ROM
bytes are committed; nothing is written under the repo.
"""

from __future__ import annotations

import hashlib

import pytest

from src.analysis.ecu_application import load_application_model
from src.analysis.operating_context import ResolvedContext, load_context_definition, resolve_context
from src.control.rom_library import RomLibrary
from src.vehicle.tune_revisions import load_tune_revisions
from tests.rom_fixtures import PROFILE, operator_image

IMAGES = {
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
def resolved(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ResolvedContext]:
    library = RomLibrary(
        tmp_path_factory.mktemp("rom_library"),
        profile_dir=PROFILE,
        registry=lambda: load_tune_revisions(PROFILE / "tune_revisions.yaml"),
    )
    definition, _ = load_context_definition(PROFILE)
    model = load_application_model(PROFILE)
    out = {}
    for revision, (name, sha) in IMAGES.items():
        image = operator_image(name)
        if image is None:
            pytest.skip(f"private ROM {revision} ({sha[:8]}...) is not on this machine")
        if hashlib.sha256(image).hexdigest() != sha:
            pytest.skip(f"the local {revision} file is not the expected plain image")
        library.add_image(image, source="upload_plain", filename=f"{revision}.bin")
        assert library.revision_match(sha)["revision"] == revision
        out[revision] = resolve_context(
            library.image(sha),
            library.rom_map(),
            definition,
            application_model=model,
            expected_sha256=sha,
            slot=1,
        )
    return out


def test_both_images_resolve_with_the_corrected_axes(resolved: dict[str, ResolvedContext]) -> None:
    for revision, context in resolved.items():
        assert context.revision == revision
        fuel = context.fuel_comp
        assert (fuel.row_axis_id, fuel.column_axis_id) == (
            "fuel_comp_engine_speed_axis",
            "fuel_comp_flow_axis",
        )
        # 0xCACD: 8..128 x 50 = 400..6,400 rpm, unchanged by either flash.
        assert fuel.row_axis_raw[0] == 8 and fuel.row_axis_raw[-1] == 128
        assert context.trim_a.row_axis_id == "timing_trim_rpm_axis"
        assert context.trim_a.column_axis_id == "timing_trim_load_axis"


def test_the_restore_moved_the_load_axis_back_to_255(resolved: dict[str, ResolvedContext]) -> None:
    old = resolved["ZS44A-20260920"].fuel_comp.column_axis_raw
    new = resolved["ZS44A-20261004"].fuel_comp.column_axis_raw
    assert (old[0], old[-1]) == (16, 144)
    assert (new[0], new[-1]) == (16, 255)
    # Timing-trim bytes were not edited by the restore.
    assert resolved["ZS44A-20260920"].trim_a.cells == resolved["ZS44A-20261004"].trim_a.cells
    assert resolved["ZS44A-20260920"].trim_b.cells == resolved["ZS44A-20261004"].trim_b.cells


def test_the_two_saturations_on_the_real_calibration(resolved: dict[str, ResolvedContext]) -> None:
    """224-count fill ceiling; k = 158 clamps k x word at word 415 (5,187.5 rpm)."""
    for context in resolved.values():
        assert context.fill_ceiling == 224
        assert context.rpm_multiplier == 158
        assert context.reciprocal_saturation_word == 415
        assert context.reciprocal_saturation_word * 12.5 == pytest.approx(5187.5)


def test_slot_1_cells_differ_between_the_two_calibrations(
    resolved: dict[str, ResolvedContext],
) -> None:
    """The restore rewrote slot-1 Fuel Comp cells to base: same layout, other bytes."""
    old = resolved["ZS44A-20260920"].fuel_comp.cells
    new = resolved["ZS44A-20261004"].fuel_comp.cells
    assert len(old) == len(new) == 16 and all(len(r) == 16 for r in (*old, *new))
    assert old != new
