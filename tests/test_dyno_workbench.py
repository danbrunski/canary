"""H07.9 — the driving dyno on the Analysis page.

The physics is pinned in ``test_dyno_power.py``; these pin what the page adds:
a pending coefficient refuses every run by name instead of being defaulted,
every window found is listed with its gear, heading or refusal, a computed
run carries its conditions, flags and uncertainty, and an empty answer says
which kind of empty it is. The route runs through the workbench child and
defers while a capture records.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.analysis import dyno_workbench
from src.analysis.channels import read_channels
from src.analysis.dyno import DynoRefusal, load_dyno_profile
from src.analysis.dyno_comparison import (
    TIRE_PRESSURE_MAX_KPA,
    TIRE_PRESSURE_MIN_KPA,
    RunConditions,
)
from src.analysis.dyno_pulls import FoundPull
from src.analysis.dyno_workbench import DynoRequest, dyno_runs, prerequisites
from src.analysis.tuning_frame import WotCriterion
from src.analysis.workbench import WorkbenchInputs
from src.collection import tuning_profile
from src.config import MASS_NOT_SET, CanaryConfig, VehicleParameters
from src.control import child_pool, workbench_jobs
from src.dashboard.routes import workbench as workbench_routes
from src.paths import REPO_ROOT
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry
from tests.test_dyno_power import (
    MOUNT_OFFSET_DEG,
    PAIR,
    PARAMS,
    SQUAT_DEG_PER_G,
    WHEEL_MASS,
    A,
    B,
    C,
    torque,
)

XTERRA = Path("data/nissan-xterra-2008")
WOT = WotCriterion(
    unit="V",
    closed_value=0.62,
    wide_open_value=4.12,
    threshold_value=4.00,
    reached_stop_tolerance=0.25,
    rpm_floor=2500.0,
)
T0 = pd.Timestamp("2026-09-26T12:00:00Z")
REGISTRY = TuneRevisionRegistry(
    revisions=[
        TuneRevision(revision="synthetic-rev", flashed_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC))
    ]
)
REQUEST = DynoRequest(road="synthetic road", payload_kg=127.0, induction="na")
#: The synthetic test vehicle, with its mass stated as the generator's.
VEHICLE = PARAMS.model_copy(
    update={"mass_kg": 2311.0 - 127.0, "mass_evidence": "synthetic: the generator's mass"}
)


def _providers(tmp: Path) -> Any:
    from src.analysis.tuning_frame import load_provider_registry

    path = tmp / "tuning_providers.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "providers": [{"role": "operator_demand", "spec": "obd:THROTTLE_POS"}],
                "wot_criterion": WOT.model_dump(),
            }
        )
    )
    return load_provider_registry(path)


def _inputs(profile_data: Path, *, providers: Any) -> WorkbenchInputs:
    return WorkbenchInputs(providers=providers, registry=REGISTRY, profile_data=profile_data)


def _profile(tmp: Path, *, measured: bool) -> Path:
    """A dyno.yaml: the synthetic vehicle's inertia, and (``measured``) a road
    load and squat recorded from the generator's own known answers."""
    data: dict[str, Any] = {
        "schema_version": 1,
        "motion_sources": {"speed": [], "rpm": ["obd:RPM"]},
        "inertia": {
            "road_wheels": 4,
            "wheel_shape_factor": 0.7,
            "driveline_output_kg_m2": 1.0,
            "engine_side_kg_m2": 0.18,
            "evidence": "synthetic",
        },
    }
    if measured:
        radius = 2.609 / (2 * math.pi)
        wheel_side = 4 * 0.7 * WHEEL_MASS * radius**2 + 1.0
        fit = {
            "a_n": A + C * 4.0,
            "b_n_per_mps": B,
            "c_n_per_mps2": C,
            "a_se_n": 0.0,
            "b_se_n_per_mps": 0.0,
            "c_se_n_per_mps2": 0.0,
            "residual_sd_n": 0.0,
            "n_samples": 100,
            "speed_min_mps": 6.0,
            "speed_max_mps": 31.0,
            "static_pitch_deg": MOUNT_OFFSET_DEG,
        }
        data["road_load"] = {
            **{k: fit[k] for k in ("a_n", "b_n_per_mps", "c_n_per_mps2")},
            "a_se_n": 0.0,
            "b_se_n_per_mps": 0.0,
            "c_se_n_per_mps2": 0.0,
            "directions": "pair",
            "per_direction": {"northbound": fit, "southbound": fit},
            "residual_sd_n": 0.0,
            "test_mass_kg": 2311.0,
            "neutral_effective_mass_kg": 2311.0 + wheel_side / radius**2,
            "inertia": "estimated",
            "squat_corrected": True,
            "imu_pitch_offset_deg": MOUNT_OFFSET_DEG,
            "conditions": {
                "road": "synthetic road",
                "date": "2026-09-26",
                "sessions": ["synthetic"],
                "payload_kg": 127.0,
                "tire_pressure_front_kpa": 234.0,
                "tire_pressure_rear_kpa": 234.0,
                "ambient_temp_c": 22.0,
                "baro_kpa": 80.0,
            },
        }
        data["squat"] = {
            "deg_per_g": SQUAT_DEG_PER_G,
            "offset_deg": MOUNT_OFFSET_DEG,
            "residual_sd_deg": 0.01,
            "n_samples": 500,
            "accel_span_g": 0.2,
            "grade": 0.0,
            "max_segment_grade": 0.001,
            "grade_leak_deg_per_g": 0.0,
            "grade_leak_se_deg_per_g": 0.01,
            "altitude_noise": [
                {
                    "white_var_m2": 0.1,
                    "correlated_var_m2": 1.0,
                    "tau_s": 10.0,
                    "reference_s": 300.0,
                    "n_reference": 300,
                }
            ],
            "conditions": {
                "payload_kg": 127.0,
                "tire_pressure_front_kpa": 234.0,
                "tire_pressure_rear_kpa": 234.0,
                "road": "synthetic road",
                "date": "2026-09-26",
                "sessions": ["synthetic"],
            },
        }
    (tmp / "dyno.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return tmp


def _session(root: Path, *, with_conditions: bool = True) -> Path:
    """A stored session holding the run conditions over the pulls' windows."""
    records = [TelemetryRecord.obd(T0.timestamp() + s, "RPM", 3000.0, "rpm") for s in range(60)]
    if with_conditions:
        for s in range(60):
            t = T0.timestamp() + s
            records += [
                TelemetryRecord.obd(t, "BAROMETRIC_PRESSURE", 80.0, "kPa"),
                TelemetryRecord.obd(t, "INTAKE_TEMP", 25.0, "degC"),
                TelemetryRecord.obd(t, "AMBIANT_AIR_TEMP", 21.0, "degC"),
            ]
    session = root / "2026-09-26T120000"
    write_records_to_parquet(records, session, max_rows=100_000)
    return session


def _found(course: dict[str, float] | None = None) -> list[FoundPull]:
    """The generator's pull pair as find_power_pulls returns them, plus a lift."""
    course = course or {"northbound": 2.0, "southbound": 182.0}
    out = []
    for index, pull in enumerate(PAIR):
        start = T0 + pd.Timedelta(seconds=5 + 20 * index)
        out.append(
            FoundPull(
                start=start,
                end=start + pd.Timedelta(seconds=10),
                rpm_start=2450.0,
                rpm_end=5500.0,
                course_deg=course[pull.heading],
                pull=pull,
                refusal=None,
            )
        )
    lift = DynoRefusal("find_power_pulls", "lift_or_shift", "the rpm slope collapsed mid-pull")
    out.append(
        FoundPull(
            start=T0 + pd.Timedelta(seconds=50),
            end=T0 + pd.Timedelta(seconds=55),
            rpm_start=2000.0,
            rpm_end=3000.0,
            course_deg=90.0,
            pull=None,
            refusal=lift,
        )
    )
    return out


@pytest.fixture
def found_pulls(monkeypatch: pytest.MonkeyPatch) -> list[list[FoundPull]]:
    """What find_power_pulls returns, one list per session read (in order)."""
    queue: list[list[FoundPull]] = []
    monkeypatch.setattr(dyno_workbench, "find_power_pulls", lambda *a, **k: queue.pop(0))
    return queue


def _facts(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["fact"]: row for row in rows}


# --------------------------------------------------------------------------- prerequisites


def test_the_committed_xterra_profile_owes_road_load_and_squat_to_h07_12() -> None:
    """The truck's road load and squat are not measured yet (H07.12): both
    block power, each names the item owing it, and nothing reads as recorded."""
    from src.analysis.dyno import Driveline
    from src.vehicle.profiles import read_registry_file

    (xterra,) = [p for p in read_registry_file(Path("profiles.yaml")) if p.slug == XTERRA.name]
    profile = load_dyno_profile(XTERRA / "dyno.yaml")
    assert profile is not None
    driveline = Driveline.from_vehicle(xterra.parameters, profile.inertia)
    facts = _facts(prerequisites(profile, driveline, xterra.parameters, WOT, "did:X"))
    for fact, item in (("road_load", "H07.5"), ("squat", "H07.4")):
        assert facts[fact]["status"] == "pending"
        assert facts[fact]["blocks_power"] is True
        assert item in facts[fact]["owed_by"] and "H07.12" in facts[fact]["owed_by"]
    assert facts["inertia"]["status"] == "estimated"
    assert facts["noise_floor"]["status"] == "pending"
    # The mass is quoted with its own evidence, which says it is an estimate.
    assert facts["mass"]["status"] == "stated"
    assert "not a scale ticket" in facts["mass"]["detail"]


def test_a_mass_without_evidence_blocks_power_rather_than_using_the_default() -> None:
    """``mass_kg`` has no default (M01.20; it was a 1,900 kg truck's): unset, the
    dyno refuses by name. A mass without its evidence is refused too: nothing
    says how it was arrived at."""
    profile = load_dyno_profile(XTERRA / "dyno.yaml")
    assert VehicleParameters().mass_kg is None
    rows = _facts(prerequisites(profile, None, VehicleParameters(), WOT, "x"))
    assert rows["mass"]["status"] == "missing" and rows["mass"]["blocks_power"] is True
    assert rows["mass"]["detail"].startswith(MASS_NOT_SET)
    stated = _facts(prerequisites(profile, None, VehicleParameters(mass_kg=2000.0), WOT, "x"))
    assert stated["mass"]["status"] == "missing" and stated["mass"]["blocks_power"] is True
    assert "no mass_evidence" in stated["mass"]["detail"]
    assert _facts(prerequisites(None, None, VehicleParameters(), WOT, "x")).keys() == {
        "dyno_profile"
    }


# --------------------------------------------------------------------------- runs


def test_pending_coefficients_refuse_every_run_and_still_list_the_pulls(
    tmp_path: Path, found_pulls: list[list[FoundPull]]
) -> None:
    profile = _profile(tmp_path, measured=False)
    session = _session(tmp_path / "storage")
    found_pulls.append(_found())

    result = dyno_runs(
        [session],
        inputs=_inputs(profile, providers=_providers(tmp_path)),
        vehicle=VEHICLE,
        request=REQUEST,
    )

    json.dumps(result)  # the route returns it as is
    assert result["available"] is False
    assert result["unavailable_kind"] == "coefficients_pending"
    (run,) = result["runs"]
    assert run["kind"] == "coefficients_pending" and run["gear"] == 3
    assert "road_load" in run["reason"] and "squat" in run["reason"]
    assert "H07.12" in run["reason"]
    assert "curve" not in run
    (listed,) = result["sessions"]
    assert listed["n_windows"] == 3 and listed["n_pulls"] == 2
    windows = listed["windows"]
    assert [w["heading"] for w in windows] == ["course-000", "course-180", None]
    assert [w["gear"] for w in windows] == [3, 3, None]
    assert windows[2]["refusal"]["kind"] == "lift_or_shift"


def test_recorded_coefficients_give_a_curve_with_conditions_flags_and_uncertainty(
    tmp_path: Path, found_pulls: list[list[FoundPull]]
) -> None:
    profile = _profile(tmp_path, measured=True)
    session = _session(tmp_path / "storage")
    found_pulls.append(_found())

    result = dyno_runs(
        [session],
        inputs=_inputs(profile, providers=_providers(tmp_path)),
        vehicle=VEHICLE,
        request=REQUEST,
    )

    json.dumps(result)
    assert result["available"] is True and result["unavailable_kind"] is None
    (run,) = result["runs"]
    assert run["available"] is True and run["direction"] == "pair" and run["n_pulls"] == 2
    conditions = run["conditions"]
    assert conditions["tune_revision"] == "synthetic-rev"
    assert conditions["baro_kpa"] == 80.0 and conditions["intake_temp_c"] == 25.0
    assert conditions["ambient_temp_c"] == 21.0  # from the session, not stated
    assert conditions["road"] == "synthetic road" and conditions["payload_kg"] == 127.0
    assert conditions["sessions"] == [session.name]
    flags = run["flags"]
    assert flags["inertia"] == "estimated" and flags["squat_corrected"] is True
    assert "own pulls" in flags["imu_pitch_offset_source"]
    # The offset is estimated from the pulls themselves, on a graded road
    # whose two legs cover different stretches: near, not equal to, 0.3 deg.
    assert abs(flags["imu_pitch_offset_deg"] - MOUNT_OFFSET_DEG) < 0.1
    # M01.8: the road load's C moved into the run's air (80 kPa both, ambient
    # 22 C coasted against 21 C run), and what the run did not state is named.
    air = flags["road_load_air"]
    assert air["fit_baro_kpa"] == 80.0 and air["run_baro_kpa"] == 80.0
    assert air["density_ratio"] == pytest.approx(295.15 / 294.15)
    assert air["c_applied_n_per_mps2"] == pytest.approx(C * 295.15 / 294.15)
    assert "ambient_temp_c" in air["temperature_source"]
    assert [(m["field"], m["status"]) for m in flags["road_load_mismatches"]] == [
        ("tire_pressure_front_kpa", "unstated"),
        ("tire_pressure_rear_kpa", "unstated"),
    ]
    # M01.10: the GPS check of each pull, and the power band it implies.
    scale = flags["speed_scale"]
    assert len(scale["residuals"]) == 2 and "squared" in scale["meaning"]
    assert flags["squat_slope"].startswith("apparent squat")
    rpm = pd.Series(run["curve"]["rpm"])
    estimate = pd.Series(run["curve"]["engine_torque_nm_estimate"])
    error = (estimate / torque(rpm.to_numpy()) - 1.0).abs()
    assert error.max() < 0.03, error.max()
    floor = run["uncertainty"]["noise_floor"]["wheel_power_kw"]
    assert floor["available"] is False and floor["kind"] == "no_floor"
    spread = run["uncertainty"]["heading_spread"]
    assert len(spread["wheel_power_kw"]) == len(rpm)
    assert "not a noise floor" in spread["meaning"]
    assert set(run["per_heading"]) == {"course-000", "course-180"}


def _tire_run(tmp_path: Path, found_pulls: list[list[FoundPull]], **tires: float) -> dict[str, Any]:
    profile = _profile(tmp_path, measured=True)
    session = _session(tmp_path / "storage")
    found_pulls.append(_found())
    result = dyno_runs(
        [session],
        inputs=_inputs(profile, providers=_providers(tmp_path)),
        vehicle=VEHICLE,
        request=DynoRequest(road="synthetic road", payload_kg=127.0, induction="na", **tires),
    )
    (run,) = result["runs"]
    assert run["available"] is True
    return result


def test_the_requests_tire_pressures_reach_the_run_conditions_and_match_the_road_load_m01_8(
    tmp_path: Path, found_pulls: list[list[FoundPull]]
) -> None:
    # The road load in _profile was coasted at 234 kPa front and rear.
    result = _tire_run(
        tmp_path, found_pulls, tire_pressure_front_kpa=234.0, tire_pressure_rear_kpa=234.0
    )
    (run,) = result["runs"]
    assert run["conditions"]["tire_pressure_front_kpa"] == 234.0
    assert run["conditions"]["tire_pressure_rear_kpa"] == 234.0
    assert result["request"]["tire_pressure_front_kpa"] == 234.0
    assert result["request"]["tire_pressure_rear_kpa"] == 234.0
    # Stated and equal to the record's: nothing differs, nothing is unstated.
    assert run["flags"]["road_load_mismatches"] == []


def test_a_tire_pressure_that_differs_from_the_road_loads_is_named_and_the_other_is_not_m01_8(
    tmp_path: Path, found_pulls: list[list[FoundPull]]
) -> None:
    result = _tire_run(
        tmp_path, found_pulls, tire_pressure_front_kpa=220.0, tire_pressure_rear_kpa=234.0
    )
    (run,) = result["runs"]
    (named,) = run["flags"]["road_load_mismatches"]
    assert (named["field"], named["status"]) == ("tire_pressure_front_kpa", "differs")
    assert named["run"] == 220.0 and named["road_load"] == 234.0
    # 34 psi is 234.4 kPa: the same pressure as 234 to a gauge, so no mismatch;
    # 241 kPa (35 psi) is a different one.
    (tmp_path / "gauge").mkdir()
    near = _tire_run(
        tmp_path / "gauge",
        found_pulls,
        tire_pressure_front_kpa=34.0 * 6.894757293168361,
        tire_pressure_rear_kpa=234.42,
    )
    assert near["runs"][0]["flags"]["road_load_mismatches"] == []
    (tmp_path / "psi").mkdir()
    far = _tire_run(
        tmp_path / "psi", found_pulls, tire_pressure_front_kpa=241.0, tire_pressure_rear_kpa=234.0
    )
    (far_named,) = far["runs"][0]["flags"]["road_load_mismatches"]
    assert (far_named["field"], far_named["status"]) == ("tire_pressure_front_kpa", "differs")
    assert "3.5 kPa" in far_named["detail"]
    # One axle stated leaves the other unstated, not defaulted to the first.
    (tmp_path / "front").mkdir()
    only_front = _tire_run(tmp_path / "front", found_pulls, tire_pressure_front_kpa=234.0)
    (unstated,) = only_front["runs"][0]["flags"]["road_load_mismatches"]
    assert (unstated["field"], unstated["status"]) == ("tire_pressure_rear_kpa", "unstated")
    assert only_front["runs"][0]["conditions"]["tire_pressure_rear_kpa"] is None


@pytest.mark.parametrize(
    "kpa",
    [
        pytest.param(35.0, id="35 psi typed as kPa"),
        pytest.param(2.4, id="2.4 bar typed as kPa"),
        pytest.param(79.9, id="just under the floor"),
        pytest.param(1662.0, id="241 kPa converted as psi"),
        pytest.param(float("nan"), id="not a number"),
    ],
)
def test_a_tire_pressure_outside_the_range_is_refused_naming_the_range_m01_8(kpa: float) -> None:
    lo, hi = f"{TIRE_PRESSURE_MIN_KPA:g}", f"{TIRE_PRESSURE_MAX_KPA:g}"
    for field in ("tire_pressure_front_kpa", "tire_pressure_rear_kpa"):
        with pytest.raises(ValueError, match=rf"{field}.*{lo}-{hi} kPa.*psi or bar"):
            DynoRequest(**{field: kpa})  # type: ignore[arg-type]
        with pytest.raises(ValueError, match=rf"{lo}-{hi} kPa"):
            _run_conditions_with(**{field: kpa})
    # The edges of the range are accepted, and so is "not stated".
    DynoRequest(tire_pressure_front_kpa=TIRE_PRESSURE_MIN_KPA, tire_pressure_rear_kpa=600.0)
    DynoRequest()


def test_a_trail_pressure_is_accepted_and_a_psi_slip_is_not_m01_8() -> None:
    # 12 psi, aired down for a trail, is 82.7 kPa and a real pressure.
    trail = 12.0 * 6.894757293168361
    assert 82.7 < trail < 82.8
    DynoRequest(tire_pressure_front_kpa=trail, tire_pressure_rear_kpa=trail)
    assert _run_conditions_with(tire_pressure_front_kpa=trail).tire_pressure_front_kpa == trail
    # Every whole psi figure from 12 to 79 typed as kPa is under the floor.
    for psi in range(12, 80):
        with pytest.raises(ValueError, match="psi or bar"):
            DynoRequest(tire_pressure_front_kpa=float(psi))


def _run_conditions_with(**tires: float) -> RunConditions:
    return RunConditions(
        gear=3,
        direction="pair",
        tune_revision="r",
        induction="na",
        baro_kpa=80.0,
        intake_temp_c=25.0,
        ambient_temp_c=21.0,
        road="synthetic road",
        date=dt.date(2026, 9, 26),
        payload_kg=127.0,
        sessions=["s"],
        **tires,
    )


def test_the_schema_states_the_tire_pressure_range_m01_8() -> None:
    props = RunConditions.model_json_schema()["properties"]
    for field in ("tire_pressure_front_kpa", "tire_pressure_rear_kpa"):
        assert props[field]["minimum"] == TIRE_PRESSURE_MIN_KPA
        assert props[field]["maximum"] == TIRE_PRESSURE_MAX_KPA
    assert _run_conditions_with().tire_pressure_front_kpa is None


def test_a_run_without_its_stated_conditions_is_refused_naming_each(
    tmp_path: Path, found_pulls: list[list[FoundPull]]
) -> None:
    profile = _profile(tmp_path, measured=True)
    session = _session(tmp_path / "storage", with_conditions=False)
    found_pulls.append(_found())

    result = dyno_runs(
        [session],
        inputs=_inputs(profile, providers=_providers(tmp_path)),
        vehicle=VEHICLE,
        request=DynoRequest(),
    )

    assert result["unavailable_kind"] == "every_run_refused"
    (run,) = result["runs"]
    assert run["kind"] == "inputs_missing"
    for named in ("barometric", "intake", "ambient", "road", "payload_kg", "induction"):
        assert named in run["reason"], named


def test_the_kinds_of_empty_are_kept_apart(
    tmp_path: Path, found_pulls: list[list[FoundPull]]
) -> None:
    providers = _providers(tmp_path)
    session = _session(tmp_path / "storage")

    unconfigured = dyno_runs(
        [session],
        inputs=_inputs(tmp_path / "nowhere", providers=providers),
        vehicle=VEHICLE,
        request=REQUEST,
    )
    assert unconfigured["unavailable_kind"] == "not_configured"
    assert "dyno.yaml" in unconfigured["reason"]

    profile = _profile(tmp_path, measured=True)
    found_pulls.append([])
    none = dyno_runs(
        [session], inputs=_inputs(profile, providers=providers), vehicle=VEHICLE, request=REQUEST
    )
    assert none["unavailable_kind"] == "no_pull_found"
    assert none["runs"] == [] and none["sessions"][0]["n_windows"] == 0

    found_pulls.append(_found()[2:])  # the lift alone
    refused = dyno_runs(
        [session], inputs=_inputs(profile, providers=providers), vehicle=VEHICLE, request=REQUEST
    )
    assert refused["unavailable_kind"] == "every_pull_refused"


def test_a_vehicle_without_a_wot_criterion_blocks_power(
    tmp_path: Path, found_pulls: list[list[FoundPull]]
) -> None:
    profile = _profile(tmp_path, measured=True)
    session = _session(tmp_path / "storage")
    found_pulls.append(_found())
    result = dyno_runs(
        [session], inputs=_inputs(profile, providers=None), vehicle=VEHICLE, request=REQUEST
    )
    assert _facts(result["prerequisites"])["wot_criterion"]["status"] == "missing"
    # A profile to fix, not a coefficient the truck owes.
    assert result["unavailable_kind"] == "prerequisites_missing"
    (run,) = result["runs"]
    assert run["kind"] == "prerequisites_missing"
    assert "wot_criterion" in run["reason"] and "no wot_criterion" in run["reason"]


@pytest.mark.parametrize("case", ["mass_evidence", "transformed_demand"])
def test_missing_configuration_is_not_reported_as_pending_coefficients(
    tmp_path: Path, found_pulls: list[list[FoundPull]], case: str
) -> None:
    """Only owed measurements are ``coefficients_pending``; absent or unusable
    profile configuration is ``prerequisites_missing``, even beside a
    pending coefficient."""
    from src.analysis.tuning_frame import load_provider_registry

    profile = _profile(tmp_path, measured=False)  # road load and squat pending too
    session = _session(tmp_path / "storage")
    found_pulls.append(_found())
    vehicle, providers = VEHICLE, _providers(tmp_path)
    if case == "mass_evidence":
        vehicle = VEHICLE.model_copy(update={"mass_evidence": None})
        fact, words = "mass", "no mass_evidence"
    else:
        path = tmp_path / "transformed.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "providers": [
                        {
                            "role": "operator_demand",
                            "spec": "obd:THROTTLE_POS",
                            "transform": "scale",
                            "factor": 2.0,
                            "unit": "V",
                        }
                    ],
                    "wot_criterion": WOT.model_dump(),
                }
            )
        )
        providers = load_provider_registry(path)
        fact, words = "wot_criterion", "applies 'scale'"
    result = dyno_runs(
        [session], inputs=_inputs(profile, providers=providers), vehicle=vehicle, request=REQUEST
    )
    row = _facts(result["prerequisites"])[fact]
    assert row["status"] == "missing" and words in row["detail"]
    assert result["unavailable_kind"] == "prerequisites_missing"
    assert result["runs"][0]["kind"] == "prerequisites_missing"
    assert words in result["reason"]


def test_each_session_is_read_whole_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """H07.9 performance: one whole-session read per session (rpm, course,
    demand, baro, IAT, ambient together), shared by the pull detector, the run
    conditions and the conditions block. The real ``find_power_pulls`` runs."""
    from src.analysis import dyno_pulls, workbench

    profile = _profile(tmp_path, measured=True)
    storage = tmp_path / "storage"
    sessions = [_session(storage)]
    calls: list[list[str]] = []
    real = read_channels

    def counted(session: Any, specs: list[str], **kwargs: Any) -> Any:
        calls.append(list(specs))
        return real(session, specs, **kwargs)

    monkeypatch.setattr(dyno_workbench, "read_channels", counted)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a second whole-session read")

    monkeypatch.setattr(dyno_pulls, "read_channels", forbidden)
    monkeypatch.setattr(workbench, "_read_optional", forbidden)

    result = dyno_runs(
        sessions,
        inputs=_inputs(profile, providers=_providers(tmp_path)),
        vehicle=VEHICLE,
        request=REQUEST,
    )

    assert len(calls) == 1
    assert set(calls[0]) == {
        "obd:RPM",
        "gps:heading",
        "obd:THROTTLE_POS",
        "obd:BAROMETRIC_PRESSURE",
        "obd:INTAKE_TEMP",
        "obd:AMBIANT_AIR_TEMP",
    }
    assert result["conditions"]["baro"]["min_kpa"] == 80.0
    assert result["unavailable_kind"] == "no_pull_found"  # steady rpm: no rise


def test_the_dyno_budget_is_sized_from_the_measured_rate() -> None:
    from src.analysis.dyno_workbench import (
        DYNO_LAPTOP_ROWS_PER_S,
        DYNO_PI_SLOWDOWN,
        dyno_over_budget,
        dyno_row_budget,
    )

    budget = dyno_row_budget(900.0)
    assert budget == int(900.0 * 0.5 * DYNO_LAPTOP_ROWS_PER_S / DYNO_PI_SLOWDOWN)
    assert dyno_over_budget({"a": budget, "b": None}, 900.0) is None
    reason = dyno_over_budget({"a": budget, "b": 1}, 900.0)
    assert reason is not None and "on the Pi" in reason and "laptop" in reason


# --------------------------------------------------------------------------- route

AUTH = {"Authorization": "Bearer secret"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The workbench routes on the example config, a thread standing in for
    the spawn child (the spawn path is test_workbench_analysis's)."""
    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    storage = tmp_path / "storage"
    storage.mkdir()
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(update={"storage_path": storage}),
            "daemon": config.daemon.model_copy(update={"jobs_dir": tmp_path / "jobs"}),
        }
    )
    app = FastAPI()
    workbench_routes.register_workbench_routes(
        app,
        config=config,
        bindings_path=Path("data/bindings.yaml"),
        dids_path=Path("data/dids.yaml"),
        resolved_control_token="secret",
    )
    monkeypatch.setattr(child_pool, "spawn_pool", lambda: ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(workbench_jobs, "limit_address_space", lambda nbytes: (nbytes, nbytes))
    with TestClient(app) as test_client:
        test_client.config = config  # type: ignore[attr-defined]
        yield test_client


def test_dyno_route_runs_through_the_child_and_needs_auth(
    client: TestClient,
) -> None:
    storage = client.config.collection.storage_path  # type: ignore[attr-defined]
    _session(storage)
    assert client.get("/analysis/dyno?sessions=2026-09-26T120000").status_code == 401
    response = client.get(
        "/analysis/dyno?sessions=2026-09-26T120000&road=r&payload_kg=100&induction=na",
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["selection"]["sessions"] == ["2026-09-26T120000"]
    assert body["request"] == {
        "road": "r",
        "payload_kg": 100.0,
        "induction": "na",
        "ambient_temp_c": None,
        "allow_single_direction": False,
        "tire_pressure_front_kpa": None,
        "tire_pressure_rear_kpa": None,
    }
    # The example config's vehicle has no dyno profile, or no pull in this
    # session; either way the page is told which kind of empty it is.
    assert body["available"] is False
    assert body["unavailable_kind"] in {"not_configured", "no_pull_found"}
    assert body["reason"]


def test_dyno_route_defers_while_a_capture_records(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = client.config.collection.storage_path  # type: ignore[attr-defined]
    _session(storage)
    monkeypatch.setattr(tuning_profile, "is_active", lambda: True)
    monkeypatch.setattr(
        tuning_profile,
        "active_profile",
        lambda: type("P", (), {"name": "tuning"})(),
    )
    response = client.get("/analysis/dyno?sessions=2026-09-26T120000", headers=AUTH)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "analysis_deferred"


def test_dyno_route_refuses_a_pool_that_cannot_finish_in_time(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = client.config.collection.storage_path  # type: ignore[attr-defined]
    _session(storage)
    monkeypatch.setattr(workbench_jobs, "count_session_rows", lambda path: 10**9)
    # Within the per-session budget (disabled here): the pool's total is what
    # the dyno's own check refuses.
    monkeypatch.setattr(workbench_jobs, "digest_row_budget", lambda config: 0)
    spawned: list[Any] = []
    monkeypatch.setattr(child_pool, "spawn_pool", lambda: spawned.append(1))
    response = client.get("/analysis/dyno?sessions=2026-09-26T120000", headers=AUTH)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "dyno_over_budget"
    assert "1,000,000,000 stored rows" in detail["message"]
    assert spawned == []


def test_dyno_route_refuses_an_unknown_induction(
    client: TestClient,
) -> None:
    response = client.get("/analysis/dyno?sessions=x&induction=turbo", headers=AUTH)
    assert response.status_code == 422


def test_dyno_route_passes_the_tire_pressures_into_the_request_m01_8(
    client: TestClient,
) -> None:
    storage = client.config.collection.storage_path  # type: ignore[attr-defined]
    _session(storage)
    response = client.get(
        "/analysis/dyno?sessions=2026-09-26T120000&road=r&payload_kg=100&induction=na"
        "&tire_pressure_front_kpa=241.3&tire_pressure_rear_kpa=227.5",
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    request = response.json()["request"]
    assert request["tire_pressure_front_kpa"] == 241.3
    assert request["tire_pressure_rear_kpa"] == 227.5


@pytest.mark.parametrize("query", ["tire_pressure_front_kpa=35", "tire_pressure_rear_kpa=2.4"])
def test_dyno_route_refuses_a_tire_pressure_that_is_probably_psi_or_bar_m01_8(
    client: TestClient, query: str
) -> None:
    response = client.get(f"/analysis/dyno?sessions=x&{query}", headers=AUTH)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert (
        f"{TIRE_PRESSURE_MIN_KPA:g}-{TIRE_PRESSURE_MAX_KPA:g} kPa" in detail
        and "psi or bar" in detail
    )
    assert query.split("=")[0] in detail


def test_the_forms_psi_range_is_the_daemons_kpa_range_m01_8() -> None:
    """``util/pressure.ts`` mirrors the Python range so the form can refuse in psi;
    nothing but this test keeps the two copies the same."""
    source = (REPO_ROOT / "src/dashboard/static/src/util/pressure.ts").read_text()
    match = re.search(
        r"TIRE_PRESSURE_RANGE_KPA\s*=\s*\{\s*min:\s*([0-9.]+),\s*max:\s*([0-9.]+)\s*\}", source
    )
    assert match, "TIRE_PRESSURE_RANGE_KPA not found in pressure.ts"
    assert (float(match.group(1)), float(match.group(2))) == (
        TIRE_PRESSURE_MIN_KPA,
        TIRE_PRESSURE_MAX_KPA,
    )
