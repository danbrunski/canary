"""N02.2: logged channels -> the load byte's raw inputs, and the known answer.

The known answer runs on a committed telemetry slice of the one capture that
logged ``0xFFFF118A`` (``tests/fixtures/fuel_comp/_capture-ram-study-
2026-10-04T204557``: the polled rows of the 17 channels N02 reads, written by
filtering the session's parquet on ``source == "obd"`` and those channels;
no ROM bytes). The chain's tables come from the private ZS44A-20260920 image
at test time, so those tests skip, naming it, when it is absent.
"""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from src.analysis.fuel_comp_inputs import (
    FUEL_COMP_FILE,
    FuelCompDeclaration,
    FuelCompInputError,
    KnownAnswerVariant,
    did_raw_counts,
    estimate_closed_position,
    known_answer,
    load_fuel_comp_declaration,
    negative_controls,
    purge_command_from_percent,
    rpm_word,
    throttle_counts_did,
    throttle_counts_pid,
)
from src.analysis.fuel_comp_rom import RevisionImageResolver
from src.analysis.rom_map import load_rom_map
from src.decode.dids import load_did_map
from src.decode.fuel_comp_load import Interval
from src.vehicle.tune_revisions import load_tune_revisions
from src.yaml_load import load_yaml
from tests.rom_fixtures import OPERATOR_ROMS, PROFILE

SLICE = Path("tests/fixtures/fuel_comp/_capture-ram-study-2026-10-04T204557")


@pytest.fixture(scope="module")
def declaration() -> FuelCompDeclaration:
    return load_fuel_comp_declaration(PROFILE)


def _shipped() -> dict[str, Any]:
    data = load_yaml((PROFILE / FUEL_COMP_FILE).read_text())
    assert isinstance(data, dict)
    return data


# -- the declaration ---------------------------------------------------------------


def test_the_shipped_declaration_loads_and_names_rom_map_entries(
    declaration: FuelCompDeclaration,
) -> None:
    rom_map = load_rom_map(PROFILE)
    rom = declaration.rom
    for table_id in (
        rom.k_table,
        rom.area_curve,
        rom.fill_low,
        rom.fill_high,
        rom.rpm_multiplier,
        rom.idle_air_cap,
        rom.limp_home_area,
        rom.purge_gain,
    ):
        rom_map.table(table_id)
    rom_map.program_constant(rom.reciprocal_numerator)
    assert [t.kind for t in rom_map.tables].count(rom.fuel_comp_kind) == 2
    assert declaration.inputs.idle_air.informed_by == [declaration.known_answer.session]


def test_an_unvalidated_did_route_must_name_its_closing_capture() -> None:
    data = _shipped()
    data["inputs"]["throttle_did"]["closing_capture"] = None
    with pytest.raises(ValidationError, match="name the capture that would close it"):
        FuelCompDeclaration.model_validate(data)


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        (("inputs", "idle_air", "nominal"), 300, "low <= nominal <= high"),
        (("inputs", "idle_air", "conditions"), "", "conditions"),
        (("samples", "gates", "baro", "max_kpa"), 70.0, "min_kpa must be below"),
        (("samples", "closed_loop", "trims"), {"stft_b1": "obd:X"}, "must name every trim"),
        (("pairing", "closed_loop", "max_separation_s"), 0, "greater than 0"),
        (("pairing", "open_loop", "evidence"), "", "evidence"),
        (("inputs", "rpm", "channel"), "nosuch:RPM", "unknown namespace"),
    ],
)
def test_a_measured_constraint_cannot_be_dropped_or_inverted(
    path: tuple[str, ...], value: object, match: str
) -> None:
    data = copy.deepcopy(_shipped())
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValidationError, match=match):
        FuelCompDeclaration.model_validate(data)


# -- logged values to raw words ----------------------------------------------------


def test_rpm_words_are_exact_or_refused(declaration: FuelCompDeclaration) -> None:
    assert rpm_word(787.5, declaration) == 63
    with pytest.raises(FuelCompInputError, match="whole engine-speed word"):
        rpm_word(790.0, declaration)


def test_pid_0x11_gives_a_count_interval_never_a_point(declaration: FuelCompDeclaration) -> None:
    # A = 8 (3.137 %): ceil(8 x 3.21) = 26 .. ceil(9 x 3.21) - 1 = 28.
    assert throttle_counts_pid(8 * 100 / 255, declaration) == Interval(26, 28)
    assert throttle_counts_pid(0.0, declaration) == Interval(0, 3)


def test_the_did_route_subtracts_the_closed_position_interval() -> None:
    assert throttle_counts_did(150, Interval(93, 102), 1023) == Interval(48, 57)
    assert throttle_counts_did(95, Interval(93, 102), 1023) == Interval(0, 2)  # clamps at 0


def test_did_counts_are_the_payload_integers_whatever_the_scale() -> None:
    catalog = load_did_map(Path("data/dids.yaml"))
    entry = next(e for e in catalog.dids if e.did == 0x120F)
    scale = float(entry.scale or 1.0)
    raw = did_raw_counts(np.array([123 * scale, 824 * scale]), "did:0x120F", catalog)
    assert raw.tolist() == [123, 824]
    with pytest.raises(FuelCompInputError, match="whole counts"):
        did_raw_counts(np.array([123.4 * scale]), "did:0x120F", catalog)


def test_every_purge_byte_round_trips_through_pid_0x2e(declaration: FuelCompDeclaration) -> None:
    for command in range(256):
        a = min(255, command * 1275 // 1000)
        found = purge_command_from_percent(a * 100 / 255, declaration)
        if a < 255:
            assert found == Interval.exactly(command), command
        else:
            # The handler clamps A at 255 from byte 200 up: every such byte.
            assert found == Interval(200, 255), command
    with pytest.raises(FuelCompInputError, match="not produced by any purge byte"):
        purge_command_from_percent(4 * 100 / 255, declaration)  # bytes 3, 4 give A 3, 5


def test_the_closed_position_is_the_warm_idle_mode_minus_the_opening(
    declaration: FuelCompDeclaration,
) -> None:
    times = np.arange(30.0)
    raw = np.array([125] * 20 + [126] * 5 + [300] * 5)
    rpm = np.where(times < 25, 750.0, 2500.0)
    coolant = np.full(30, 90.0)
    estimate = estimate_closed_position(times, raw, times, rpm, times, coolant, declaration)
    assert estimate.mode_raw == 125 and estimate.n_samples == 25
    assert estimate.interval == Interval(125 - 32, 125 - 23)
    assert not estimate.validated
    cold = estimate_closed_position(times, raw, times, rpm, times, coolant - 30, declaration)
    assert cold.interval is None and cold.reason is not None
    assert "closed_position_unestimated" in cold.reason


# -- the known answer (private ROM; committed telemetry) ---------------------------


@pytest.fixture(scope="module")
def calibration_20260920(declaration: FuelCompDeclaration):  # type: ignore[no-untyped-def]
    path = OPERATOR_ROMS["r20260920"]
    if not path.exists():
        pytest.skip(f"private ROM {path} (ZS44A-20260920) is not on this machine: not checked")
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    resolver = RevisionImageResolver(
        registry,
        load_rom_map(PROFILE),
        declaration.rom,
        rom_dirs=[path.parent],
        slot=declaration.program.map_slot,
    )
    return resolver.resolve("ZS44A-20260920")


def test_the_reconstruction_reproduces_the_logged_byte(
    calibration_20260920: Any, declaration: FuelCompDeclaration
) -> None:
    """canary-60's listing pass: 1.000 at idle (n = 38), 1.027 off idle (n = 22)."""
    result = known_answer(SLICE, calibration_20260920, declaration)
    assert result.passed, result.reasons
    idle, off_idle = result.subsets["idle"], result.subsets["off_idle"]
    assert idle["n"] == 38 and off_idle["n"] == 22
    assert idle["median_ratio"] == pytest.approx(1.000, abs=1e-3)
    assert (idle["p10_ratio"], idle["p90_ratio"]) == pytest.approx((0.960, 1.025), abs=1e-3)
    assert off_idle["median_ratio"] == pytest.approx(1.026, abs=1e-3)
    assert (off_idle["p10_ratio"], off_idle["p90_ratio"]) == pytest.approx((0.870, 1.148), abs=1e-3)
    # The interval (idle air over its declared range, purge unknown) holds
    # every idle byte. Off idle the inputs are read up to 2.5 s from the byte,
    # so half the bytes fall outside the 14-count interval: acquisition
    # separation, not the chain.
    assert idle["inside"] == 38
    assert off_idle["inside"] == 11


def test_the_known_answer_is_a_coarse_screen_not_proof_of_exact_arithmetic(
    calibration_20260920: Any, declaration: FuelCompDeclaration
) -> None:
    """What the data can resolve, pinned so nobody reads more into a pass:
    the 0xDC1D00 numerator scaled by 1.03 still passes; by 1.05 it fails.
    Exactness rests on the disassembly line-check, not on this test."""
    for factor, passes in ((1.03, True), (0.98, True), (1.05, False), (0.97, False)):
        curves = dataclasses.replace(
            calibration_20260920.curves,
            reciprocal_numerator=int(calibration_20260920.curves.reciprocal_numerator * factor),
        )
        result = known_answer(
            SLICE, dataclasses.replace(calibration_20260920, curves=curves), declaration
        )
        assert result.passed is passes, (factor, result.subsets)


@pytest.mark.parametrize("variant", negative_controls(96), ids=lambda v: v.name)
def test_every_negative_control_fails_the_known_answer(
    calibration_20260920: Any, declaration: FuelCompDeclaration, variant: KnownAnswerVariant
) -> None:
    result = known_answer(SLICE, calibration_20260920, declaration, variant=variant)
    assert not result.passed
    assert result.reasons
    expected_off_idle = {
        "rpm^0.5": 0.664,
        "rpm^1.5": 1.576,
        "2.55 counts per byte": 1.310,
        "4.0 counts per byte": 0.784,
        "0x120F uncorrected": 0.559,
    }
    assert result.subsets["off_idle"]["median_ratio"] == pytest.approx(
        expected_off_idle[variant.name], abs=1e-3
    )
