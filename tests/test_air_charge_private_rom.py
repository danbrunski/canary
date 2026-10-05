"""E05.2 private checks: steady-state BFS from independently observed inputs.

The calibration comes from the operator's ZS44A-20260920 image (the ECU read,
``~/ecu-re/roms``) and the drives from the laptop's session store
(``~/canary-data``); neither is committed, so each test skips by name when
its input is absent -- a skip, never a pass.

What is predicted is BFS (``0xFFFF103A``, DID 0x1208) at a steady state, from
the reported MAF word (DID 0x1209), the rpm word (PID 0x0C), K and the Fuel
Comp band, through :func:`src.analysis.air_charge_twin.predict_steady_bfs`.
Observed BFS and X are never inputs. The gates are the ones frozen before
scoring (``FROZEN_GATES``). Controls with a wrong neighbouring word or byte, or
the wrong calibration's K, must fail the same acceptance.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from src.analysis import air_charge_twin as twin
from src.analysis.ecu_application import ApplicationModelRefusalError, load_application_model
from src.analysis.rom_map import load_rom_map

REPO = Path(__file__).resolve().parents[1]
PROFILE = REPO / "data" / "nissan-xterra-2008"
ECU_READ = Path.home() / "ecu-re" / "roms" / "ZS44A-20260920.ecuread.bin"
IMAGE_SHA = "a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50"
SESSIONS = Path.home() / "canary-data" / "sessions" / "nissan-xterra-2008"
#: Every local capture under ZS44A-20260920 (flashed 2026-09-21T03:08Z,
#: replaced 2026-10-04T23:02Z) that polled DID 0x1208 with DID 0x1209 and
#: PID 0x0C: chosen by channel, before scoring.
DRIVES = (
    "_capture-fuel-target-research-2026-09-21T195844",
    "_capture-tuning-2026-09-21T223734",
    "_capture-tuning-2026-09-21T232153",
    "_capture-tuning-2026-09-22T170459",
    "_capture-tuning-2026-09-22T235224",
    "_capture-tuning-2026-09-23T142926",
    "_capture-tuning-2026-09-23T164007",
)
FIXTURES = (
    REPO / "tests/fixtures/ground_truth/target-phi-slice/telemetry-00000.parquet",
    REPO / "tests/fixtures/rom_trace/_capture-ram-study-2026-10-04T204557/telemetry-00000.parquet",
    REPO / "tests/fixtures/fuel_comp/_capture-ram-study-2026-10-04T204557/telemetry-00000.parquet",
)


@pytest.fixture(scope="module")
def image() -> bytes:
    if not ECU_READ.exists():
        pytest.skip("private ROM ZS44A-20260920 (a46baa24...be50) is not on this machine")
    data = ECU_READ.read_bytes()
    if hashlib.sha256(data).hexdigest() != IMAGE_SHA:
        pytest.skip("the local ZS44A-20260920 file is not the expected plain image")
    return data


@pytest.fixture(scope="module")
def calibration(image: bytes) -> twin.AirChargeCalibration:
    model, _ = load_application_model(PROFILE)
    return twin.AirChargeCalibration.from_image(
        image, model=model, rom_map=load_rom_map(PROFILE), slot=1, image_sha256=IMAGE_SHA
    )


def _read(paths: list[str]) -> pd.DataFrame:
    glob = ", ".join(f"'{p}'" for p in paths)
    return duckdb.sql(
        f"select * from read_parquet([{glob}], union_by_name=true) where channel in "
        "('DID_1208', 'DID_1209', 'RPM', 'MAF', 'mem:ECM_AIR_CHARGE_SLOT_103A', "
        "'mem:ECM_FUEL_COMP_X_INPUT')"
    ).df()


def test_the_calibration_is_read_after_the_programs_checks(
    image: bytes, calibration: twin.AirChargeCalibration
) -> None:
    assert (calibration.k, calibration.flow_multiplier, calibration.speed_multiplier) == (
        28715,
        226,
        3,
    )
    assert calibration.maf_scale == 149
    assert calibration.ceiling == (255,) * 16
    assert calibration.fuel_comp_rpm_axis[0] == 8 and calibration.fuel_comp_load_axis[0] == 16
    model, _ = load_application_model(PROFILE)
    other = bytearray(image)
    other[0x03C700] ^= 0x01  # code inside the Tp span
    with pytest.raises(ApplicationModelRefusalError) as refused:
        twin.AirChargeCalibration.from_image(
            bytes(other), model=model, rom_map=load_rom_map(PROFILE), slot=1, image_sha256="x"
        )
    assert refused.value.reason == "wrong_program"


@pytest.fixture(scope="module")
def drives(calibration: twin.AirChargeCalibration) -> twin.SteadyBfsReport:
    missing = [d for d in DRIVES if not (SESSIONS / d).is_dir()]
    if missing:
        pytest.skip(f"local sessions absent: {missing}")
    records: list[twin.BfsRecord] = []
    excluded: dict[str, int] = {}
    for drive in DRIVES:
        rows = _read([str(SESSIONS / drive / "telemetry-*.parquet")])
        report = twin.evaluate_steady_bfs(rows, calibration)
        records.extend(report.records)
        for reason, count in report.excluded.items():
            excluded[reason] = excluded.get(reason, 0) + count
    return twin.SteadyBfsReport(records=tuple(records), excluded=excluded)


@pytest.mark.slow
def test_steady_state_bfs_is_predicted_from_maf_rpm_k_and_fuel_comp(
    drives: twin.SteadyBfsReport,
) -> None:
    """Measured 2026-10-05: 458 of 14,843 BFS reads pass the gates; median
    ratio 0.9991, p10 0.9898, p90 1.0097; 98.5 % inside the exact interval
    (re-measured after the one-count airflow stall widened the interval)."""
    summary = drives.summary()
    assert drives.passes(), summary
    assert summary["scored"] >= 400
    assert summary["inside_interval"] >= 0.95
    # Scored under a stated assumption, never a check: no channel reads the
    # cranking, MAF-fault or subtracting-form flags.
    assert summary["regions_assumed"] == dict.fromkeys(twin.REGIONS_NOT_READ, summary["scored"])
    assert 0.995 <= summary["median_ratio"] <= 1.005
    # Every BFS read is accounted for: scored, or excluded by a named gate.
    assert set(drives.excluded) <= {
        "maf_unpaired",
        "rpm_unpaired",
        "below_min_rpm",
        "maf_window_sparse",
        "maf_not_steady",
        "rpm_not_steady",
    }


@pytest.mark.slow
def test_wrong_neighbouring_words_and_the_wrong_calibration_fail(
    image: bytes, calibration: twin.AirChargeCalibration, drives: twin.SteadyBfsReport
) -> None:
    """The same scored samples through each control. The bound is the
    acceptance's median band; each control lands far outside it."""
    k_neighbour = int.from_bytes(image[0xE0760:0xE0762], "big")  # slot +0x760: the scaler copy
    controls = {
        "k_from_slot_plus_0x760": replace(calibration, k=k_neighbour),
        "multiplier_from_0x89D3": replace(calibration, flow_multiplier=image[0x89D3]),
        "k_of_the_uprev_base": replace(calibration, k=27089),
        "report_taken_as_airflow": replace(calibration, maf_scale=255),
    }
    medians = {}
    for name, control in controls.items():
        ratios = sorted(
            r.observed
            / twin.predict_steady_bfs(
                control, maf_word=r.maf_word, rpm_word=r.rpm_word, load_byte=r.load_byte
            ).mid
            for r in drives.records
        )
        medians[name] = ratios[len(ratios) // 2]
    lo, hi = twin.FROZEN_GATES.median_ratio_band
    assert all(not lo <= m <= hi for m in medians.values()), medians
    assert medians["k_of_the_uprev_base"] == pytest.approx(28715 / 27089, abs=0.01)


def test_the_october_4_fixture_cannot_be_scored_and_says_why(
    calibration: twin.AirChargeCalibration,
) -> None:
    """The ram-study capture read BFS in a memory window every 5 s and polled
    PID 0x10 about every 2 s, never within 0.1 s of a BFS read: every one of
    the 194 reads is unpaired. An empty result about the inputs, not about
    the chain (capture recipe: E05.2)."""
    report = twin.evaluate_steady_bfs(
        _read([str(p) for p in FIXTURES]),
        calibration,
        bfs_channel="mem:ECM_AIR_CHARGE_SLOT_103A",
        maf_channel="MAF",
        load_channel="mem:ECM_FUEL_COMP_X_INPUT",
    )
    assert report.records == ()
    assert report.excluded == {"maf_unpaired": 194}
