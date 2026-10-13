"""B08.6 desk part: the October 4 Fuel Comp restore's written multiplier changes,
reproduced at input points (:mod:`src.analysis.fuel_comp_restore`).

No ROM bytes are committed. Known answers run on synthetic images built like
:func:`tests.rom_fixtures.load_byte_image` (both with the same load-byte chain
and K, as the real pair has), resolved through the real
:class:`~src.analysis.fuel_comp_rom.RevisionImageResolver` under a registry
whose two revisions carry the synthetic hashes. The input points are the
committed ram-study slice (``tests/fixtures/fuel_comp/_capture-ram-study-
2026-10-04T204557``), the only capture that logs the load byte. The real images
are exercised by ``tests/test_fuel_comp_restore_private_rom.py``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis.channels import read_channels
from src.analysis.fuel_comp_inputs import FuelCompDeclaration, load_fuel_comp_declaration
from src.analysis.fuel_comp_restore import (
    InputPoint,
    RestoreReproductionRefusalError,
    classify,
    multiplier_change,
    observed_points,
    points_from_samples,
    reproduce,
    written_tolerance,
)
from src.analysis.fuel_comp_rom import FuelCompCalibration, RevisionImageResolver
from src.analysis.plant_validation import (
    Boundary,
    InterventionCase,
    PredictedBand,
    load_interventions,
    resolve_boundary,
)
from src.analysis.rom_map import load_rom_map
from src.decode.ecm_lookup import lookup_2d
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry
from tests.rom_fixtures import PROFILE, load_byte_image, with_checksums, xterra_map

SLICE = Path("tests/fixtures/fuel_comp/_capture-ram-study-2026-10-04T204557")
CASE = "fuel-comp-restore-2026-10-04"

RPM_AXIS = tuple(range(8, 129, 8))  # 0xCACD raw, x 4 in the word domain (400..6,400 rpm)
LOAD_AXIS_OLD = (*range(16, 129, 8), 144)  # 20260920: compressed 16..144
LOAD_AXIS_NEW = (*range(16, 241, 16), 255)  # 20261004: restored 16..255


def old_cell(r: int, c: int) -> int:
    return 128 + c


def new_cell(r: int, c: int) -> int:
    """Asymmetric in row and column, so a transposed read differs."""
    return 128 + c + 2 * r


def _image(load_axis: tuple[int, ...], cell: object) -> bytes:
    image = bytearray(load_byte_image())
    image[0xCACD : 0xCACD + 16] = bytes(RPM_AXIS)
    image[0xCB29 : 0xCB29 + 16] = bytes(load_axis)
    rom_map = xterra_map()
    slots = rom_map.map_slots
    assert slots is not None
    base = slots.slot_base(1)
    image[base + 0x660 : base + 0x760] = bytes(
        cell(r, c)  # type: ignore[operator]
        for r in range(16)
        for c in range(16)
    )
    return with_checksums(bytes(image), rom_map)


@pytest.fixture(scope="module")
def declaration() -> FuelCompDeclaration:
    return load_fuel_comp_declaration(PROFILE)


@pytest.fixture(scope="module")
def real_case() -> InterventionCase:
    return load_interventions(PROFILE).case(CASE)


def _synthetic(
    tmp_path: Path,
    real_case: InterventionCase,
    declaration: FuelCompDeclaration,
    old_image: bytes,
    new_image: bytes,
) -> tuple[InterventionCase, Boundary, FuelCompCalibration, FuelCompCalibration]:
    """The real case with the synthetic hashes, a registry that agrees, and
    both calibrations through the real resolver."""
    paths = []
    for name, image in (("old.bin", old_image), ("new.bin", new_image)):
        path = tmp_path / name
        path.write_bytes(image)
        paths.append(path)
    shas = [hashlib.sha256(i).hexdigest() for i in (old_image, new_image)]
    registry = TuneRevisionRegistry(
        revisions=[
            TuneRevision(
                revision=real_case.base_revision,
                flashed_at=datetime(2026, 9, 21, 3, 8, 45, tzinfo=UTC),
                flashed_at_precision="exact",
                image_sha256=shas[0],
            ),
            TuneRevision(
                revision=real_case.candidate_revision,
                flashed_at=datetime(2026, 10, 4, 23, 2, 33, 183902, tzinfo=UTC),
                flashed_at_precision="exact",
                image_sha256=shas[1],
            ),
        ]
    )
    case = real_case.model_copy(
        update={"base_image_sha256": shas[0], "candidate_image_sha256": shas[1]}
    )
    resolver = RevisionImageResolver(
        registry,
        load_rom_map(PROFILE),
        declaration.rom,
        image_files=paths,
        slot=declaration.program.map_slot,
    )
    return (
        case,
        resolve_boundary(case, registry),
        resolver.resolve(case.base_revision),
        resolver.resolve(case.candidate_revision),
    )


@pytest.fixture
def pair(
    tmp_path: Path, real_case: InterventionCase, declaration: FuelCompDeclaration
) -> tuple[InterventionCase, Boundary, FuelCompCalibration, FuelCompCalibration]:
    return _synthetic(
        tmp_path,
        real_case,
        declaration,
        _image(LOAD_AXIS_OLD, old_cell),
        _image(LOAD_AXIS_NEW, new_cell),
    )


@pytest.fixture(scope="module")
def slice_points(real_case: InterventionCase, declaration: FuelCompDeclaration) -> list[InputPoint]:
    long = read_channels(
        SLICE, [real_case.load_input, declaration.inputs.rpm.channel], acquired_time=True
    )
    return observed_points(
        long,
        session=SLICE.name,
        byte_spec=real_case.load_input,
        declaration=declaration,
        max_gap_s=declaration.known_answer.max_gap_s,
    )


def _point(byte: tuple[int, int], word: tuple[int, int], t: float = 1.79e9) -> InputPoint:
    return InputPoint("s", "observed", t, byte[0], byte[1], word[0], word[1], None)


# -- known answers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("byte", "word", "fc_old", "fc_new", "change_pct"),
    [
        # Row 0, first column: unchanged.
        ((16, 16), (32, 32), (128, 128), (128, 128), (0.0, 0.0)),
        # 144 is the old axis's last column (143) and the new axis's ninth (136).
        ((144, 144), (32, 32), (143, 143), (136, 136), (-4.8951, -4.8951)),
        # Past 144 the old column clamps; the new axis keeps going (col 12, row 4).
        ((208, 208), (160, 160), (143, 143), (148, 148), (3.4965, 3.4965)),
        # rpm known only between two reads: rows 0..1 at the first column.
        ((16, 16), (32, 64), (128, 128), (128, 130), (0.0, 1.5625)),
        # A byte interval across both axes' 144 breakpoint and the old clamp.
        ((128, 160), (32, 32), (142, 143), (135, 137), (-4.9296, -4.1958)),
    ],
)
def test_the_change_is_each_image_on_its_own_axes(
    pair: tuple[InterventionCase, Boundary, FuelCompCalibration, FuelCompCalibration],
    byte: tuple[int, int],
    word: tuple[int, int],
    fc_old: tuple[int, int],
    fc_new: tuple[int, int],
    change_pct: tuple[float, float],
) -> None:
    _case, _boundary, old, new = pair
    assert old.load_axis_raw == LOAD_AXIS_OLD and new.load_axis_raw == LOAD_AXIS_NEW
    got = multiplier_change(old, new, _point(byte, word))
    assert (got.fc_old, got.fc_new) == (fc_old, fc_new)
    assert got.change_pct == pytest.approx(change_pct, abs=1e-4)
    assert got.old_load_clamped == (byte[1] > 144)
    assert got.new_load_clamped is False


def test_new_cells_on_the_old_axis_or_transposed_are_different_answers(
    pair: tuple[InterventionCase, Boundary, FuelCompCalibration, FuelCompCalibration],
) -> None:
    """The restore changed the axis and the cells: either alone is wrong."""
    _case, _boundary, old, new = pair
    point = _point((144, 144), (32, 32))
    right = multiplier_change(old, new, point).change_pct
    cells_only = dataclasses.replace(new, load_axis_raw=old.load_axis_raw)
    assert multiplier_change(old, cells_only, point).change_pct == (0.0, 0.0) != right
    transposed = dataclasses.replace(
        new, fuel_comp=tuple(tuple(row) for row in zip(*new.fuel_comp, strict=True))
    )
    at = _point((208, 208), (160, 160))
    assert multiplier_change(old, transposed, at).change_pct != pytest.approx(
        multiplier_change(old, new, at).change_pct
    )


def test_interval_extremes_are_the_brute_force_extremes(
    tmp_path: Path, real_case: InterventionCase, declaration: FuelCompDeclaration
) -> None:
    """Random cells: the corner grid finds what every whole (word, byte) finds."""
    rng = np.random.default_rng(4)
    cells = [rng.integers(110, 150, (16, 16)) for _ in range(2)]
    _case, _boundary, old, new = _synthetic(
        tmp_path,
        real_case,
        declaration,
        _image(LOAD_AXIS_OLD, lambda r, c: int(cells[0][r][c])),
        _image(LOAD_AXIS_NEW, lambda r, c: int(cells[1][r][c])),
    )
    for _ in range(12):
        b0 = int(rng.integers(0, 230))
        w0 = int(rng.integers(20, 500))
        b1, w1 = b0 + int(rng.integers(0, 25)), w0 + int(rng.integers(0, 40))
        got = multiplier_change(old, new, _point((b0, b1), (w0, w1)))
        brute = [
            100.0
            * (
                lookup_2d(new.fuel_comp, new.rpm_breakpoints, new.load_axis_raw, w, b).value
                / lookup_2d(old.fuel_comp, old.rpm_breakpoints, old.load_axis_raw, w, b).value
                - 1.0
            )
            for w, b in itertools.product(range(w0, w1 + 1), range(b0, b1 + 1))
        ]
        # Q16 truncation can move an interior value by one count (< 1 % of 110).
        assert got.change_pct[0] <= min(brute) + 0.95
        assert got.change_pct[1] >= max(brute) - 0.95
        assert min(brute) <= got.change_pct[0] and got.change_pct[1] <= max(brute)


# -- the committed ram-study slice ---------------------------------------------------------


def test_every_byte_read_on_the_slice_is_placed_or_named(slice_points: list[InputPoint]) -> None:
    """192 reads; the byte sits 0.25-2.3 s from any PID 0x0C read, so each is
    bracketed (``known_answer.max_gap_s`` = 2.5 s) rather than paired."""
    assert len(slice_points) == 192
    assert Counter(p.reason for p in slice_points) == {
        None: 167,
        "unbracketed:after": 14,
        "unbracketed:before": 11,
    }
    placed = [p for p in slice_points if p.reason is None]
    assert all(p.byte_lo == p.byte_hi for p in placed)
    assert all(len(p.rpm_read_times) == 2 for p in placed)
    assert all(p.rpm_read_times[0] <= p.t <= p.rpm_read_times[1] for p in placed)


def test_the_slice_has_no_observed_point_in_the_high_load_row(
    slice_points: list[InputPoint], real_case: InterventionCase, declaration: FuelCompDeclaration
) -> None:
    """The written "+3 to +5.5 %" row (byte >= 144 at 4,000-5,600 rpm) holds no
    observed point: every bracketed byte >= 144 is below 3,800 rpm. Its
    figures are not reproducible from observed coordinates on this drive."""
    bands = real_case.prediction_of_record.bands
    rpc = declaration.inputs.rpm.rpm_per_count
    placed = [p for p in slice_points if p.reason is None]
    got = Counter(classify(p, bands, rpc) for p in placed)
    assert got == {(0, None): 77, (1, None): 51, (None, "no_predicted_row"): 39}
    high = [p for p in placed if p.byte_lo is not None and p.byte_lo >= 144]
    assert len(high) == 39
    assert max(p.word_hi * rpc for p in high if p.word_hi is not None) == 3787.5
    # The row's "45 % of samples": 89 of 192 byte reads are below 64 (46.4 %).
    assert sum(1 for p in slice_points if p.byte_lo is not None and p.byte_lo < 64) == 89


def test_constant_multipliers_give_each_kind_of_verdict(
    tmp_path: Path,
    real_case: InterventionCase,
    declaration: FuelCompDeclaration,
    slice_points: list[InputPoint],
) -> None:
    """200 -> 198 everywhere is exactly -1 %: inside "about -1 %", outside
    "-0.2 to -0.5 %", and nothing at all in the high row."""
    case, boundary, old, new = _synthetic(
        tmp_path,
        real_case,
        declaration,
        _image(LOAD_AXIS_OLD, lambda r, c: 200),
        _image(LOAD_AXIS_NEW, lambda r, c: 198),
    )
    result = reproduce(
        case,
        boundary,
        old,
        new,
        slice_points,
        rpm_per_count=declaration.inputs.rpm.rpm_per_count,
    ).as_dict()
    verdicts = [(b["n_points"], b["verdict"]) for b in result["bands"]]
    assert verdicts == [(77, "reproduced"), (51, "contradicted"), (0, "no_points")]
    assert result["bands"][0]["change_pct_range"] == pytest.approx([-1.0, -1.0])
    assert result["bands"][1]["window_pct"] == pytest.approx([-0.55, -0.15])
    assert result["bands"][2]["change_pct_range"] is None
    assert result["unplaced"] == {"unbracketed:after": 14, "unbracketed:before": 11}
    assert result["no_claim"]["by_reason"] == {"no_predicted_row": 39}
    assert result["no_claim"]["changed"] == 39
    assert [s["reads"] for s in result["load_share"]] == [89, 56, 47]
    assert result["routes"] == {"observed": 192}
    assert result["assumptions"]


# -- refusals and edges --------------------------------------------------------------------


def test_the_images_must_be_the_cases_and_compare_the_same_input(
    pair: tuple[InterventionCase, Boundary, FuelCompCalibration, FuelCompCalibration],
) -> None:
    case, boundary, old, new = pair
    rpc = 12.5
    with pytest.raises(RestoreReproductionRefusalError) as caught:
        reproduce(case, boundary, new, old, [], rpm_per_count=rpc)
    assert caught.value.reason == "image_mismatch:base"
    for changed, reason in (
        (dataclasses.replace(new, k=new.k + 1), "k_changed"),
        (dataclasses.replace(new, slot=2), "slot_mismatch"),
        (
            dataclasses.replace(
                new,
                curves=dataclasses.replace(new.curves, idle_air_cap=new.curves.idle_air_cap + 1),
            ),
            "load_input_chain_changed",
        ),
    ):
        with pytest.raises(RestoreReproductionRefusalError) as caught:
            reproduce(case, boundary, old, changed, [], rpm_per_count=rpc)
        assert caught.value.reason == reason


def test_points_outside_the_base_revision_are_not_placed(
    pair: tuple[InterventionCase, Boundary, FuelCompCalibration, FuelCompCalibration],
) -> None:
    case, boundary, old, new = pair
    flash = boundary.at.timestamp()
    points = [
        _point((40, 40), (64, 64), t=flash - 1.0),
        _point((40, 40), (64, 64), t=flash),
        _point((40, 40), (64, 64), t=boundary.base.flashed_at.timestamp() - 1.0),
    ]
    result = reproduce(case, boundary, old, new, points, rpm_per_count=12.5)
    assert result.unplaced == {"after_flash": 1, "before_base_revision": 1}
    assert result.bands[0].changes[0].point.t == flash - 1.0


def test_the_tolerance_is_the_written_precision(real_case: InterventionCase) -> None:
    bands = real_case.prediction_of_record.bands
    assert [written_tolerance(b) for b in bands] == [(0.5, 0.5), (0.05, 0.05), (0.5, 0.05)]
    unstated = PredictedBand(load_input=(0, 64), fuel_change_pct=(-1.2, -1.2), as_written="| x |")
    with pytest.raises(RestoreReproductionRefusalError) as caught:
        written_tolerance(unstated)
    assert caught.value.reason == "precision_unstated"


def test_a_point_across_a_band_edge_belongs_to_neither(real_case: InterventionCase) -> None:
    bands = real_case.prediction_of_record.bands
    assert classify(_point((60, 70), (64, 64)), bands, 12.5) == (None, "straddles_band_edge")
    # byte 200, rpm 3,900-4,100: across the high row's 4,000 rpm edge.
    assert classify(_point((200, 200), (312, 328)), bands, 12.5) == (None, "straddles_band_edge")
    assert classify(_point((200, 200), (328, 400)), bands, 12.5) == (2, None)
    assert classify(_point((200, 200), (100, 120)), bands, 12.5) == (None, "no_predicted_row")


def test_observed_points_name_what_the_session_lacks(declaration: FuelCompDeclaration) -> None:
    t = pd.Timestamp("2026-10-04T21:00:00Z")
    rows = [
        {
            "timestamp": t,
            "spec": "mem:ECM_FUEL_COMP_X_INPUT",
            "value": 100.0,
            "unit": "counts",
            "timestamp_basis": "acquired",
        },
        {
            "timestamp": t + pd.Timedelta(seconds=1),
            "spec": "mem:ECM_FUEL_COMP_X_INPUT",
            "value": 101.0,
            "unit": "counts",
            "timestamp_basis": "round",
        },
    ]
    long = pd.DataFrame(rows)
    kwargs = {"session": "s", "declaration": declaration, "max_gap_s": 2.5}
    got = observed_points(long, byte_spec="mem:ECM_FUEL_COMP_X_INPUT", **kwargs)  # type: ignore[arg-type]
    assert [p.reason for p in got] == [
        "not_captured:obd:RPM",
        "clock_unacquired:mem:ECM_FUEL_COMP_X_INPUT",
    ]
    with pytest.raises(RestoreReproductionRefusalError) as caught:
        observed_points(long, byte_spec="mem:NOT_READ", **kwargs)  # type: ignore[arg-type]
    assert caught.value.reason == "not_captured:mem:NOT_READ"


def test_reconstructed_samples_become_interval_points() -> None:
    frame = pd.DataFrame(
        {
            "session": ["s", "s"],
            "route": ["open_loop", "closed_loop"],
            "timestamp": pd.to_datetime(["2026-10-04T21:00:00Z", "2026-10-04T21:00:01Z"]),
            "state": ["eligible", "gated"],
            "reason": [None, "steadiness"],
            "byte_lo": [207.0, np.nan],
            "byte_hi": [212.0, np.nan],
            "rpm_word_lo": [330.0, np.nan],
            "rpm_word_hi": [340.0, np.nan],
        }
    )
    first, second = points_from_samples(frame)
    assert (first.route, first.reason) == ("reconstructed:open_loop", None)
    assert (first.byte_lo, first.byte_hi, first.word_lo, first.word_hi) == (207, 212, 330, 340)
    assert second.reason == "gated:steadiness" and second.byte_lo is None
