"""E07.4: validate the fuel plant from telemetry and across an actual intervention.

The real case (B08.6's October 4 Fuel Comp restore) is checked against the
revision registry and its preserved prediction, and is ``pending`` by name: the
frozen plant failed its own held-out bounds and no post-change recording
exists. The whole path then runs on a **labelled synthetic plant** that does
qualify, generated from known physics, to show that it passes when the effect
is there, fails when it is reversed, and refuses a plant fitted per ROM.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from src.analysis.fuel_plant import (
    PLANT_FILE,
    B06Rules,
    BaselineParameterSet,
    FuelPlantProfile,
    FuelPlantRefusalError,
    PlantInputs,
    SpentRecord,
    load_b06,
    load_fuel_plant,
    plant_qualification,
    predict,
    resolve_parameters,
)
from src.analysis.fuel_plant_eval import evaluate, identify
from src.analysis.plant_validation import (
    Boundary,
    InterventionCase,
    InterventionFile,
    build_case_samples,
    evaluate_intervention,
    inventory_session,
    load_interventions,
    match_pairs,
    prediction_order,
    resolve_boundary,
    split_by_boundary,
    verify_prediction_of_record,
)
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry, load_tune_revisions

PROFILE = Path("data/nissan-xterra-2008")
FIXTURE = Path("tests/fixtures/fuel_plant/_capture-tuning-2026-09-23T142926")
CASE_ID = "fuel-comp-restore-2026-10-04"
FLASH = datetime(2026, 10, 4, 23, 2, 33, 183902, tzinfo=UTC)


@pytest.fixture(scope="module")
def real() -> tuple[FuelPlantProfile, B06Rules, InterventionCase, Boundary]:
    profile, _ = load_fuel_plant(PROFILE)
    delay = load_b06(PROFILE, profile.delay)
    case = load_interventions(PROFILE).case(CASE_ID)
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    return profile, delay, case, resolve_boundary(case, registry)


# -- the real case --------------------------------------------------------------------


def test_the_case_is_the_registrys_flash_and_its_prediction_came_first(
    real: tuple[FuelPlantProfile, B06Rules, InterventionCase, Boundary],
) -> None:
    _, _, case, boundary = real
    assert boundary.at == FLASH
    assert boundary.precision == "exact"
    assert (boundary.base.revision, boundary.candidate.revision) == (
        "ZS44A-20260920",
        "ZS44A-20261004",
    )
    # Committed 23:01:15Z, 78 s before the flash: prior, by the commit's own time.
    assert prediction_order(case, boundary) == "prior"
    assert (FLASH - case.prediction_of_record.committed_at).total_seconds() == pytest.approx(
        78.18, abs=0.01
    )
    # Not on any ledger, and the record says why rather than being re-dated.
    assert case.prediction_of_record.ledger.status == "not_recorded"
    # An image the registry does not name is refused, never trusted.
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    wrong = case.model_copy(update={"candidate_image_sha256": "0" * 64})
    with pytest.raises(FuelPlantRefusalError) as err:
        resolve_boundary(wrong, registry)
    assert err.value.reason == "image_mismatch"


def test_the_prediction_of_record_is_preserved_as_committed(
    real: tuple[FuelPlantProfile, B06Rules, InterventionCase, Boundary],
) -> None:
    _, _, case, _ = real
    record = case.prediction_of_record
    try:
        problems = verify_prediction_of_record(record)
    except FuelPlantRefusalError as exc:
        pytest.skip(f"recording commit not in this clone ({exc.reason})")
    assert problems == []
    # A reworded band, a moved time or another text are each caught.
    edited = record.model_copy(
        update={
            "bands": [
                record.bands[0].model_copy(update={"as_written": "| < 64 | any | about −2 % |"}),
                *record.bands[1:],
            ],
            "committed_at": datetime(2026, 10, 4, 22, 0, tzinfo=UTC),
            "text_sha256": "f" * 64,
        }
    )
    assert len(verify_prediction_of_record(edited)) == 3


def test_the_real_case_is_pending_because_the_plant_failed_its_bounds(
    real: tuple[FuelPlantProfile, B06Rules, InterventionCase, Boundary],
) -> None:
    """The committed held-out slice through the same frozen plant: it reproduces
    E07.3's numbers there, and the intervention verdict is pending by name."""
    profile, delay, case, boundary = real
    qualification = plant_qualification(profile)
    assert not qualification.qualified
    assert qualification.reason == (
        "plant_not_qualified: baseline na-stock-2026-10-05.1 failed its predeclared held-out bounds"
    )
    frame = build_case_samples(FIXTURE, profile, delay, case)
    assert frame["load_input"].isna().all()  # the tuning captures never logged the fill byte
    report, samples = evaluate_intervention(
        case, boundary, profile, delay, [frame], sessions_root=FIXTURE.parent
    )
    assert report["verdict"] == "pending"
    assert [p.split(":")[0] for p in report["pending"]] == [
        "plant_not_qualified",
        "no_post_change_data",
    ]
    assert report["effect_tests"] == {}
    steady = report["baseline_reproduction"]["steady"]
    assert (steady["n_samples"], steady["n_regions"]) == (152, 38)
    assert steady["median_pct"] == pytest.approx(1.90, abs=0.01)
    pre = report["baseline_reproduction"]
    # Steady, transient and missing are reported apart, plus the latency bracket.
    assert pre["transient"]["n_samples"] > 0
    assert set(pre["missing"]) == {"missing_input", "not_co_observed"}
    assert set(pre["observation_latency_median_shift_pct"]) == {
        "lambda_observation_delay=lo",
        "lambda_observation_delay=hi",
    }
    assert set(samples["side"]) == {"pre"}
    assert {"lambda_pred", "err_pct", "state"} <= set(samples.columns)


def test_qualification_is_read_from_the_recorded_verdicts(
    real: tuple[FuelPlantProfile, B06Rules, InterventionCase, Boundary],
) -> None:
    profile, *_ = real
    raw: dict[str, Any] = yaml.safe_load((PROFILE / PLANT_FILE).read_text())
    for verdict, expect in (
        ("pass", ("closed_loop", "enrichment")),
        ("pass_closed_loop_only", ("closed_loop",)),
    ):
        for rec in raw["spent_held_out"]:
            rec["verdict"] = verdict
        q = plant_qualification(FuelPlantProfile.model_validate(raw))
        assert q.qualified and q.regimes == expect
    # One held-out recording never scored under this baseline: not qualified.
    raw["spent_held_out"] = raw["spent_held_out"][1:]
    q = plant_qualification(FuelPlantProfile.model_validate(raw))
    assert not q.qualified and q.reason is not None and "were not scored" in q.reason


def test_the_inventory_reports_acquisition_clocks_and_the_revision(
    real: tuple[FuelPlantProfile, B06Rules, InterventionCase, Boundary],
) -> None:
    profile, _, case, _ = real
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    row = inventory_session(
        FIXTURE, profile, extra={"load_input": case.load_input}, registry=registry
    )
    assert row["revision"] == "ZS44A-20260920"
    pulse = row["channels"][profile.specs.pulse]
    assert pulse["n"] > 0 and pulse["median_dt_s"] == pytest.approx(0.5, abs=0.01)
    assert row["groups"]["load_input"]["present"] == []
    assert row["groups"]["fuel_pressure"]["present"] == []
    assert row["plant_replay_ready"]


# -- a labelled synthetic plant that qualifies ------------------------------------------

S_TRUE = 0.0502
T0 = datetime(2030, 1, 1, tzinfo=UTC).timestamp()
SYN_FLASH = datetime(2030, 1, 2, tzinfo=UTC)
IDENT = ["_sim-ident-a", "_sim-ident-b"]
HELD = ["_sim-held-a", "_sim-held-b"]
PRE = ["_sim-pre-a", "_sim-pre-b"]
POST = ["_sim-post-a"]


def _synthetic_profile() -> FuelPlantProfile:
    raw: dict[str, Any] = yaml.safe_load((PROFILE / PLANT_FILE).read_text())
    raw.update(synthetic=True, plant_version="synthetic-mv-1", baseline=None, spent_held_out=[])
    raw["split"].update(identification=IDENT, held_out=HELD)
    return FuelPlantProfile.model_validate(raw)


Change = Callable[[np.ndarray, np.ndarray], np.ndarray]


def band_middle(fill: np.ndarray, rpm: np.ndarray) -> np.ndarray:
    """The prediction of record's band midpoints: the true change, when it is right."""
    out = np.where(fill < 64, -1.0, np.where(fill < 144, -0.35, 0.0))
    return np.where((fill >= 144) & (rpm >= 4000) & (rpm <= 5600), 4.25, out)


def _frame(
    session: str,
    profile: FuelPlantProfile,
    *,
    t0: float,
    seed: int,
    post: bool = False,
    change: Change | None = None,
    ltft_post: float = 0.0,
) -> pd.DataFrame:
    """Steady reads from known physics at s = S_TRUE: closed-loop regions at
    low fill (the trims hold lambda at 1) and enrichment regions at WOT
    (lambda 0.85 before the change). Before the change LTFT is +2 %; after it,
    ``ltft_post``. ``change`` gives the true percent change in the ECM's base
    fuel per read (the ROM effect); trims add, and LTFT applies in open loop."""
    rng = np.random.default_rng(seed)
    rows: list[dict[str, Any]] = []
    regions = [("closed_loop", 1500 + 60 * k, 20 + 4 * k, 8 + k) for k in range(26)] + [
        ("enrichment", 4100 + 120 * k, 160 + 5 * k, 90 + 4 * k) for k in range(12)
    ]
    for r, (regime, rpm, fill, maf) in enumerate(regions):
        for n in range(4):
            rows.append(
                dict(
                    session=session,
                    t=t0 + 30 * r + 0.5 * n,
                    rpm=rpm * (1 + rng.normal(0, 0.003)),
                    maf_gps=maf * (1 + rng.normal(0, 0.003)),
                    baro_kpa=81.0,
                    iat_c=25.0,
                    rail_kpa=np.nan,
                    load_input=float(fill),
                    regime=regime,
                    region_id=f"{session}#{r}",
                    state="eligible",
                    ecu_dead_time_ms=0.6,
                    dead_time_source="battery_voltage",
                    rail_measured=False,
                    coolant_c=88.0,
                )
            )
    frame = pd.DataFrame(rows)
    params = resolve_parameters(profile, _delay(), for_identification=True)
    one = PlantInputs(
        pulse_ms=frame["ecu_dead_time_ms"].to_numpy() + 1.0,
        ecu_dead_time_ms=frame["ecu_dead_time_ms"].to_numpy(),
        rpm=frame["rpm"].to_numpy(),
        maf_gps=frame["maf_gps"].to_numpy(),
        baro_kpa=frame["baro_kpa"].to_numpy(),
        iat_c=frame["iat_c"].to_numpy(),
        rail_kpa=frame["rail_kpa"].to_numpy(),
    )
    # lambda at 1 ms of effective open time under the true scale.
    lam_per_ms = predict(one, params, profile.geometry).lambda_delivered / S_TRUE
    closed = (frame["regime"] == "closed_loop").to_numpy()
    ltft_pre = 2.0
    stft_pre = np.where(closed, rng.normal(1.0, 0.2, len(frame)), 0.0)
    te_closed = lam_per_ms / 1.0
    base_closed = te_closed / (1 + (stft_pre + ltft_pre) / 100)
    te_open = lam_per_ms / 0.85
    base_open = te_open / (1 + ltft_pre / 100)
    if not post:
        te = np.where(closed, te_closed, te_open)
        stft, ltft = stft_pre, ltft_pre
    else:
        fills = frame["load_input"].to_numpy(dtype=float)
        speeds = frame["rpm"].to_numpy(dtype=float)
        true_change = np.zeros(len(frame)) if change is None else change(fills, speeds)
        factor = 1 + true_change / 100
        stft = np.where(closed, 100 * (te_closed / (base_closed * factor) - 1) - ltft_post, 0.0)
        te = np.where(closed, te_closed, base_open * factor * (1 + ltft_post / 100))
        ltft = ltft_post
    frame["pulse_ms"] = frame["ecu_dead_time_ms"] + te
    frame["stft_pct"] = stft
    frame["ltft_pct"] = ltft
    lam = lam_per_ms / te * (1 + rng.normal(0, 0.004, len(frame)))
    frame["lambda_obs"] = frame["lambda_obs_lo"] = frame["lambda_obs_hi"] = lam
    return frame


def _delay() -> B06Rules:
    profile, _ = load_fuel_plant(PROFILE)
    return load_b06(PROFILE, profile.delay)


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("sim")
    delay = _delay()
    profile = _synthetic_profile()
    fit = identify(
        [_frame(n, profile, t0=T0 + 3600 * k, seed=k) for k, n in enumerate(IDENT)],
        profile,
        delay,
        sessions_root=root,
    )
    assert fit["value"] == pytest.approx(S_TRUE, rel=0.01)
    baseline = BaselineParameterSet(
        id="synthetic-1",
        frozen=True,
        frozen_on=date(2030, 1, 1),
        plant_version=profile.plant_version,
        hardware_configuration=profile.hardware_configuration,
        fitted_on=IDENT,
        revisions=["SYN-A"],
        values={"stoich_air_per_open_ms": fit["value"]},
        intervals={"stoich_air_per_open_ms": fit["interval"]},
        method="synthetic",
    )
    profile = profile.model_copy(update={"baseline": baseline})
    held = evaluate(
        [_frame(n, profile, t0=T0 + 7200 + 3600 * k, seed=10 + k) for k, n in enumerate(HELD)],
        profile,
        delay,
        sessions_root=root,
    )
    assert held["verdict"] == "pass"
    spent = [
        SpentRecord(**{k: v for k, v in r.items()}, scored_on=date(2030, 1, 1))
        for r in held["spent_records"]
    ]
    profile = profile.model_copy(update={"spent_held_out": spent})
    real_case = load_interventions(PROFILE).case(CASE_ID)
    case = real_case.model_copy(
        update={
            "id": "synthetic-restore",
            "synthetic": True,
            "base_revision": "SYN-A",
            "candidate_revision": "SYN-B",
            "base_image_sha256": "a" * 64,
            "candidate_image_sha256": "b" * 64,
            "split": real_case.split.model_copy(update={"pre": PRE, "post": POST}),
        }
    )
    registry = TuneRevisionRegistry(
        revisions=[
            TuneRevision(
                revision="SYN-A",
                flashed_at=datetime(2029, 1, 1, tzinfo=UTC),
                flashed_at_precision="exact",
                image_sha256="a" * 64,
            ),
            TuneRevision(
                revision="SYN-B",
                flashed_at=SYN_FLASH,
                flashed_at_precision="exact",
                image_sha256="b" * 64,
            ),
        ]
    )
    pre = [_frame(n, profile, t0=T0 + 20000 + 3600 * k, seed=20 + k) for k, n in enumerate(PRE)]
    return {
        "root": root,
        "delay": delay,
        "profile": profile,
        "case": case,
        "boundary": resolve_boundary(case, registry),
        "pre": pre,
    }


def _post(
    syn: dict[str, Any], change: Change | None = band_middle, ltft: float = 0.0
) -> pd.DataFrame:
    """Post-change reads after the flash: the true change per read, LTFT as stated."""
    t0 = SYN_FLASH.timestamp() + 600
    return _frame(POST[0], syn["profile"], t0=t0, seed=99, post=True, change=change, ltft_post=ltft)


def _run(syn: dict[str, Any], post: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    return evaluate_intervention(
        syn["case"],
        syn["boundary"],
        syn["profile"],
        syn["delay"],
        [*syn["pre"], post],
        sessions_root=syn["root"],
    )


def test_a_qualified_synthetic_plant_passes_a_correctly_predicted_change(
    synthetic: dict[str, Any],
) -> None:
    """The ROM changes fuel by each band's midpoint (+4.25 % at WOT, -1 % and
    -0.35 % at part load) and the flash resets LTFT from +2 % to 0."""
    syn = synthetic
    assert plant_qualification(syn["profile"]).qualified
    report, samples = _run(syn, _post(syn))
    assert report["synthetic"] is True
    assert "not evidence about the vehicle" in report["claim"]
    assert report["pending"] == []
    lam = report["effect_tests"]["lambda_open_loop"]
    assert lam["outcome"] == "pass", lam
    assert lam["observed_fuel_change_pct"]["median"] == pytest.approx(4.25, abs=0.5)
    assert lam["noise_floor_pct"] < 1.5
    # One-to-one: 48 WOT reads per pre recording give 48 floor pairs, never the
    # 96 that counting each mirror pair twice would.
    assert lam["floor_pairs"] == 48
    trim = report["effect_tests"]["trim_closed_loop"]
    assert trim["outcome"] == "pass", trim
    # Most of the STFT move is the LTFT reset, not the ROM: both are reported.
    assert (
        trim["predicted_stft_change_pct"]["median"]
        > trim["predicted_from_rom_change_only_pct"]["median"]
    )
    assert report["post_change_replay"]["verdict"] == "pass"
    assert report["verdict"] == "pass"
    sealed = report["verdict_record"]
    assert sealed.verify() and sealed.verdict == "pass" and sealed.synthetic
    assert dataclasses.replace(sealed, synthetic=False).verify() is False
    assert set(samples["side"]) == {"pre", "post"}
    assert set(samples.loc[samples["side"] == "post", "ltft_state"]) == {"reset_holds"}


def test_a_reversed_change_fails_and_relearning_reads_are_not_tested(
    synthetic: dict[str, Any],
) -> None:
    syn = synthetic
    report, _ = _run(syn, _post(syn, change=lambda f, r: -band_middle(f, r)))
    assert report["effect_tests"]["lambda_open_loop"]["outcome"] == "fail"
    assert report["verdict"] == "fail"
    # LTFT already relearned to +3 %: the declared reset no longer holds there,
    # so those reads are excluded by name, never fed into a prediction.
    report, _ = _run(syn, _post(syn, ltft=3.0))
    for test in report["effect_tests"].values():
        assert test["n_pairs"] == 0
        assert test["exclusions"]["ltft_relearning"] == test["post_reads"]
        assert test["outcome"] == "insufficient_evidence"
    assert report["verdict"] == "insufficient_evidence"


def test_a_plant_fitted_per_rom_cannot_count(synthetic: dict[str, Any]) -> None:
    syn = synthetic
    profile: FuelPlantProfile = syn["profile"]
    assert profile.baseline is not None
    post = _post(syn)
    frames = [*syn["pre"], post]

    def run(p: FuelPlantProfile) -> str:
        with pytest.raises(FuelPlantRefusalError) as err:
            evaluate_intervention(
                syn["case"], syn["boundary"], p, syn["delay"], frames, sessions_root=syn["root"]
            )
        return err.value.reason

    on_candidate = profile.model_copy(
        update={"baseline": profile.baseline.model_copy(update={"revisions": ["SYN-B"]})}
    )
    assert run(on_candidate) == "plant_fitted_on_candidate_rom"
    refit = profile.model_copy(
        update={"baseline": profile.baseline.model_copy(update={"fitted_on": [*IDENT, POST[0]]})}
    )
    assert run(refit) == "refit_across_intervention"
    unfrozen = profile.model_copy(
        update={"baseline": profile.baseline.model_copy(update={"frozen": False})}
    )
    assert run(unfrozen) == "baseline_not_frozen"
    # Identified on the base revision *and* another: not this case's base alone.
    mixed = profile.model_copy(
        update={"baseline": profile.baseline.model_copy(update={"revisions": ["SYN-0", "SYN-A"]})}
    )
    assert run(mixed) == "baseline_revision_mismatch"
    # A fitted recording whose name has no start time cannot be shown to
    # precede the flash (simulator traces are exempt: they carry no clock name).
    nameless = profile.model_copy(
        update={"baseline": profile.baseline.model_copy(update={"fitted_on": [*IDENT, "drive"]})}
    )
    assert run(nameless) == "recording_time_unknown"


def test_the_split_is_by_sample_time_and_declared_first(synthetic: dict[str, Any]) -> None:
    syn = synthetic
    post = _post(syn)
    straddle = pd.concat([syn["pre"][0], post.assign(session=PRE[0])], ignore_index=True)
    with pytest.raises(FuelPlantRefusalError) as err:
        split_by_boundary([straddle], syn["boundary"])
    assert err.value.reason == "straddles_intervention"
    stray = post.assign(session="_sim-post-undeclared")
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate_intervention(
            syn["case"],
            syn["boundary"],
            syn["profile"],
            syn["delay"],
            [*syn["pre"], stray],
            sessions_root=syn["root"],
        )
    assert err.value.reason == "undeclared_recording"
    # A declared post-change recording with reads before the flash is on the wrong side.
    early = syn["pre"][0].assign(session=POST[0])
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate_intervention(
            syn["case"],
            syn["boundary"],
            syn["profile"],
            syn["delay"],
            [syn["pre"][1], early],
            sessions_root=syn["root"],
        )
    assert err.value.reason == "wrong_side"


def test_synthetic_and_recorded_never_mix(
    synthetic: dict[str, Any],
    real: tuple[FuelPlantProfile, B06Rules, InterventionCase, Boundary],
) -> None:
    profile, delay, case, boundary = real
    syn = synthetic
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate_intervention(
            syn["case"], syn["boundary"], profile, delay, syn["pre"], sessions_root=syn["root"]
        )
    assert err.value.reason == "synthetic_mismatch"
    # A real plant is never identified or scored on simulator output.
    sim = syn["pre"][0].assign(session=profile.split.identification[0].replace("_capture", "_sim"))
    with pytest.raises(FuelPlantRefusalError) as err:
        identify([sim], profile, delay, sessions_root=syn["root"])
    assert err.value.reason == "synthetic_as_observed"
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate([sim], profile, delay, sessions_root=syn["root"])
    assert err.value.reason == "synthetic_as_observed"
    recorded = syn["pre"][0].assign(session=HELD[0].replace("_sim-", "_capture-x-"))
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate([recorded], syn["profile"], syn["delay"], sessions_root=syn["root"])
    assert err.value.reason == "synthetic_plant_on_recording"


# -- rules frozen before the first post-change drive -------------------------------------


def _case_raw() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((PROFILE / "plant_interventions.yaml").read_text())
    return raw


def test_the_scoring_rules_cannot_change_after_a_post_change_drive_is_listed() -> None:
    raw = _case_raw()
    case = raw["cases"][0]
    assert case["floor"]["declared_on"] == date(2026, 10, 5)
    # As committed: the frozen hash matches the rules.
    InterventionFile.model_validate(raw)
    # The first post-change drive is listed: still loads, the rules unchanged.
    case["split"]["post"] = ["_capture-tuning-2026-10-06T120000"]
    InterventionFile.model_validate(raw)
    # Any scoring rule changed after that refuses to load.
    for path, value in (
        (("floor", "min_separation_s"), 10.0),
        (("acceptance", "min_pairs"), 5),
        (("matching", "load_input_abs"), 8),
        (("learned_state", "relearn_tolerance_pct"), 3.0),
    ):
        changed = copy.deepcopy(raw)
        changed["cases"][0][path[0]][path[1]] = value
        with pytest.raises(ValidationError, match="rules_changed"):
            InterventionFile.model_validate(changed)
    # So do the pre-change recordings and the load input with its pairing.
    stated: tuple[tuple[str, object], ...] = (
        ("load_input", "mem:ECM_FUEL_TARGET_MAP_X_TRACE"),
        ("load_input_pair_tolerance_s", 0.5),
    )
    for key, stated_value in stated:
        changed = copy.deepcopy(raw)
        changed["cases"][0][key] = stated_value
        with pytest.raises(ValidationError, match="rules_changed"):
            InterventionFile.model_validate(changed)
    changed = copy.deepcopy(raw)
    changed["cases"][0]["split"]["pre"] = changed["cases"][0]["split"]["pre"][:-1]
    with pytest.raises(ValidationError, match="rules_changed"):
        InterventionFile.model_validate(changed)
    # A post-change recording whose name has no start time cannot be ordered
    # against the freeze.
    nameless = copy.deepcopy(raw)
    nameless["cases"][0]["split"]["post"] = ["drive-after-flash"]
    with pytest.raises(ValidationError, match="recording_time_unknown"):
        InterventionFile.model_validate(nameless)
    # Re-freezing after the drive is refused too: the hash is rewritten, but
    # the recording started before the new frozen_at.
    refrozen = copy.deepcopy(raw)
    refrozen["cases"][0]["floor"]["min_separation_s"] = 10.0
    model = InterventionCase.model_validate(
        {**refrozen["cases"][0], "frozen_rules": None, "split": {**case["split"], "post": []}}
    )
    refrozen["cases"][0]["frozen_rules"] = {
        "sha256": model.rules_sha256(),
        "frozen_at": datetime(2026, 10, 7, tzinfo=UTC),
    }
    with pytest.raises(ValidationError, match="rules_frozen_after_post"):
        InterventionFile.model_validate(refrozen)
    # And a post-change drive cannot be listed without frozen rules at all.
    unfrozen = copy.deepcopy(raw)
    unfrozen["cases"][0]["frozen_rules"] = None
    with pytest.raises(ValidationError, match="rules_not_frozen"):
        InterventionFile.model_validate(unfrozen)


def test_floor_pairs_inside_one_recording_keep_the_declared_separation(
    real: tuple[FuelPlantProfile, B06Rules, InterventionCase, Boundary],
) -> None:
    _, _, case, _ = real
    sep = case.floor.min_separation_s
    assert sep == 30.0
    reads = pd.DataFrame(
        {
            "session": ["_capture-x-2026-10-04T000000"] * 3,
            "t": [0.0, 1.0, 1.0 + sep],
            "rpm": [3000.0] * 3,
            "load_input": [100.0] * 3,
            "baro_kpa": [81.0] * 3,
            "iat_c": [25.0] * 3,
            "regime": ["closed_loop"] * 3,
        }
    )
    i, j = match_pairs(reads, reads, case.matching, floor=case.floor)
    pairs = {tuple(sorted((int(a), int(b)))) for a, b in zip(i, j, strict=True)}
    # Reads 0 and 1 are one second apart: never a floor pair with each other.
    assert (0, 1) not in pairs
    assert pairs and all(abs(reads.t[a] - reads.t[b]) >= sep for a, b in pairs)
