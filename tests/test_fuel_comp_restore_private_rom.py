"""B08.6 desk part on the operator's real images (skipped when absent).

Kept apart from ``tests/test_fuel_comp_restore.py`` so nothing there passes
*because* an image was absent. The two files are the registry's ZS44A-20260920
and ZS44A-20261004 (``tests.rom_fixtures.OPERATOR_ROMS``), resolved by plain
sha256; the points are the committed ram-study slice's observed byte reads.

The written rows are the prediction of record's (``plant_interventions.yaml``).
The two low-load rows are what this test claims the images reproduce; the
high-load row has no observed point on this drive (see
``test_the_slice_has_no_observed_point_in_the_high_load_row``), so it stays
``no_points`` here whatever the images hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.analysis.channels import read_channels
from src.analysis.fuel_comp_inputs import load_fuel_comp_declaration
from src.analysis.fuel_comp_restore import check_comparable, observed_points, reproduce
from src.analysis.fuel_comp_rom import FuelCompRomRefusalError, RevisionImageResolver
from src.analysis.plant_validation import load_interventions, resolve_boundary
from src.analysis.rom_map import load_rom_map
from src.vehicle.tune_revisions import load_tune_revisions
from tests.rom_fixtures import OPERATOR_ROMS, PROFILE

SLICE = Path("tests/fixtures/fuel_comp/_capture-ram-study-2026-10-04T204557")


def test_the_real_images_reproduce_the_written_low_load_rows() -> None:
    declaration = load_fuel_comp_declaration(PROFILE)
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    case = load_interventions(PROFILE).case("fuel-comp-restore-2026-10-04")
    resolver = RevisionImageResolver(
        registry,
        load_rom_map(PROFILE),
        declaration.rom,
        rom_dirs=sorted({OPERATOR_ROMS[k].parent for k in ("r20260920", "r20261004")}),
        slot=declaration.program.map_slot,
    )
    try:
        old = resolver.resolve(case.base_revision)
        new = resolver.resolve(case.candidate_revision)
    except FuelCompRomRefusalError as exc:
        if exc.reason == "image_not_found":
            pytest.skip(f"private ROMs not on this machine: {exc.message}")
        raise
    check_comparable(old, new)  # same load-byte chain, K and slot
    assert old.load_axis_raw[0] == new.load_axis_raw[0] == 16
    assert (old.load_axis_raw[-1], new.load_axis_raw[-1]) == (144, 255)
    assert old.rpm_axis_raw == new.rpm_axis_raw
    long = read_channels(
        SLICE, [case.load_input, declaration.inputs.rpm.channel], acquired_time=True
    )
    points = observed_points(
        long,
        session=SLICE.name,
        byte_spec=case.load_input,
        declaration=declaration,
        max_gap_s=declaration.known_answer.max_gap_s,
    )
    result = reproduce(
        case,
        resolve_boundary(case, registry),
        old,
        new,
        points,
        rpm_per_count=declaration.inputs.rpm.rpm_per_count,
    ).as_dict()
    summary = [(b["as_written"], b["change_pct_range"], b["verdict"]) for b in result["bands"]]
    assert [b["verdict"] for b in result["bands"]] == [
        "reproduced",
        "reproduced",
        "no_points",
    ], summary
