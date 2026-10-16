"""E09.12: the prediction of record scored on the plant-free outputs.

BFS (DID 0x1208) and the final commanded phi (``mem:ECM_TARGET_EQUIV_RATIO``)
come from the ECU's decoded integer logic, not from the plant, so their test
does not wait for the plant to qualify. Each is tested per matched pair: a
post-change read of the output against the prediction's own design point in
the same regime (E07.4's matching, both ends of the two fill intervals within
the byte tolerance), observed change = post / pre - 1 against that point's
predicted interval from the draws, widened by the condition-matched floor of
pre-vs-pre pairs; E07.4's acceptance numbers decide.

The synthetic half shows the test passes a known change, fails a reversed
one, says ``insufficient_evidence`` with too few pairs, and refuses a changed
prediction artifact. The private half (``slow``; skips by name without the
artifact, the drives or the images) reproduces the real verdict and runs the
same test as a null control on pre-flash drives on the base image.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from src.analysis.ecu_application import load_application_model
from src.analysis.fuel_plant import FuelPlantRefusalError, load_b06, load_fuel_plant
from src.analysis.plant_validation import (
    Boundary,
    EcuAmendment,
    EcuOutputRules,
    EcuOutputVerdict,
    InterventionCase,
    InterventionFile,
    InterventionVerdict,
    PredictionArtifact,
    ecu_effect_test,
    ecu_output_reads,
    evaluate_ecu_outputs,
    load_interventions,
    post_chain_sha256,
    post_scope,
    resolve_boundary,
    trim_independence,
    verify_prediction_artifact,
)
from src.vehicle.tune_revisions import load_tune_revisions

PROFILE = Path("data/nissan-xterra-2008")
CASE_ID = "fuel-comp-restore-2026-10-04"
FIXTURE = Path("tests/fixtures/fuel_plant/_capture-tuning-2026-09-23T142926")
PRE = "_sim-ecu-pre-2026-10-01T000000"
POST = "_sim-ecu-post-2026-10-06T000000"


def _raw() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((PROFILE / "plant_interventions.yaml").read_text())
    return raw


def _case(
    *, synthetic: bool = True, post: list[str] | None = None, amend: bool = False
) -> InterventionCase:
    """The real case's rules on a labelled synthetic case, re-frozen for its
    own ``post`` list (``ecu_rules_sha256``); with ``amend``, the real
    version-2 amendment re-chained onto that block and re-frozen."""
    raw = copy.deepcopy(_raw()["cases"][0])
    raw["synthetic"] = synthetic
    if synthetic:
        raw["id"] = "synthetic-ecu-outputs"
    amendments = raw.pop("ecu_amendments")
    raw.pop("ecu_post_appended", None)
    ecu = raw.pop("ecu_outputs")
    ecu["post"] = [POST] if post is None else post
    ecu["frozen_rules"]["sha256"] = "0" * 63 + "1"
    base = InterventionCase.model_validate(raw)
    rules = EcuOutputRules.model_validate(ecu)
    ecu["frozen_rules"]["sha256"] = base.model_copy(
        update={"ecu_outputs": rules}
    ).ecu_rules_sha256()
    if not amend:
        return InterventionCase.model_validate({**raw, "ecu_outputs": ecu})
    previous = ecu["frozen_rules"]["sha256"]
    for amendment in amendments:
        amendment["amends"] = previous
        amendment["frozen_rules"]["sha256"] = "0" * 63 + "1"
        previous = EcuAmendment.model_validate(amendment).rules_sha256()
        amendment["frozen_rules"]["sha256"] = previous
    return InterventionCase.model_validate(
        {**raw, "ecu_outputs": ecu, "ecu_amendments": amendments}
    )


# -- synthetic known answers -----------------------------------------------------------


def _points(n: int, rng: np.random.Generator) -> pd.DataFrame:
    """Design points: idle-to-cruise rpm, fill intervals, one recording, reads
    40 s apart (so within-recording floor pairs keep the 30 s separation)."""
    rpm = np.repeat([800.0, 1600.0, 2400.0], n // 3)
    lo = np.repeat([30.0, 40.0, 50.0], n // 3)
    m = len(rpm)
    return pd.DataFrame(
        {
            "session": PRE,
            "t": 1_790_000_000.0 + 40.0 * np.arange(m),
            "regime": "closed_loop",
            "rpm": rpm,
            "maf_gps": 10.0,
            "load_lo": lo,
            "load_hi": lo + 40.0,
            "baro_kpa": 81.0,
            "iat_c": 25.0,
            "observed": 20_000.0 * (1.0 + rng.normal(0.0, 0.003, m)),
        }
    )


def _predicted(points: pd.DataFrame, change_pct: float, half_width: float = 0.3) -> pd.DataFrame:
    return points.assign(
        pred_lo=change_pct - half_width, pred_mid=change_pct, pred_hi=change_pct + half_width
    )


def _post_reads(
    points: pd.DataFrame, true_change_pct: float, rng: np.random.Generator, n: int | None = None
) -> pd.DataFrame:
    """Post-change reads at the design points' conditions, the output moved by
    a known ratio (plus the same per-read noise)."""
    src = points if n is None else points.iloc[:n]
    m = len(src)
    return pd.DataFrame(
        {
            "session": POST,
            "t": 1_790_500_000.0 + 40.0 * np.arange(m),
            "observed": 20_000.0
            * (1.0 + true_change_pct / 100.0)
            * (1.0 + rng.normal(0.0, 0.003, m)),
            "rpm": src["rpm"].to_numpy() * 1.01,
            "maf_gps": 10.0,
            "baro_kpa": 81.0,
            "iat_c": 26.0,
            "coolant_c": 90.0,
            "regime": "closed_loop",
            "load_lo": src["load_lo"].to_numpy() + 2.0,
            "load_hi": src["load_hi"].to_numpy() + 2.0,
            "state": "eligible",
        }
    )


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(12)


def test_a_candidate_that_moves_bfs_by_a_known_ratio_passes(rng: np.random.Generator) -> None:
    case = _case()
    pre = _predicted(_points(60, rng), 2.31)
    result = ecu_effect_test(pre, _post_reads(pre, 2.31, rng), case)
    assert result["outcome"] == "pass", result["reason"]
    assert result["n_pairs"] == 60 and result["n_signed"] == 60
    assert result["floor_pairs"] >= case.acceptance.min_floor_pairs
    # The floor is the pre-vs-pre noise, near the 0.3 % per-read scatter x sqrt(2).
    assert 0.2 < result["noise_floor_pct"] < 1.2
    assert result["observed_change_pct"]["median"] == pytest.approx(2.31, abs=0.2)
    assert result["direction_agreement"] == 1.0
    assert result["within_uncertainty"] >= case.acceptance.within_uncertainty


def test_a_reversed_prediction_fails(rng: np.random.Generator) -> None:
    case = _case()
    pre = _predicted(_points(60, rng), -2.31)
    result = ecu_effect_test(pre, _post_reads(pre, 2.31, rng), case)
    assert result["outcome"] == "fail"
    assert result["direction_agreement"] == 0.0
    assert result["within_uncertainty"] < case.acceptance.within_uncertainty


def test_too_few_pairs_is_insufficient_evidence_not_a_fail(rng: np.random.Generator) -> None:
    case = _case()
    pre = _predicted(_points(60, rng), 2.31)
    few = ecu_effect_test(pre, _post_reads(pre, 2.31, rng, n=case.acceptance.min_pairs - 1), case)
    assert few["outcome"] == "insufficient_evidence"
    assert few["reason"] == f"{case.acceptance.min_pairs - 1} pairs < {case.acceptance.min_pairs}"
    # Reads out of the matching (fill interval 8 counts off) form no pair.
    off = _post_reads(pre, 2.31, rng)
    off["load_hi"] = off["load_hi"] + 6.0
    unmatched = ecu_effect_test(pre, off, case)
    assert (unmatched["n_pairs"], unmatched["outcome"]) == (0, "insufficient_evidence")
    assert unmatched["empty"].startswith("unmatched")
    # No floor: the design points too close together to pair inside one recording.
    close = pre.assign(t=pre["t"].to_numpy() / 40.0)
    nofloor = ecu_effect_test(close, _post_reads(pre, 2.31, rng), case)
    assert nofloor["floor_pairs"] == 0
    assert nofloor["outcome"] == "insufficient_evidence"
    assert nofloor["reason"] == "condition-matched noise floor unavailable"
    # Each kind of empty is named apart.
    never = ecu_effect_test(pre, _post_reads(pre, 2.31, rng, n=0), case)
    assert never["empty"].startswith("never_captured")
    filtered = ecu_effect_test(pre, _post_reads(pre, 2.31, rng).assign(state="transient"), case)
    assert filtered["empty"].startswith("filtered")


def test_an_unsigned_prediction_is_judged_on_within_uncertainty_alone(
    rng: np.random.Generator,
) -> None:
    case = _case()
    pre = _predicted(_points(60, rng), 0.0, half_width=0.0)
    held = ecu_effect_test(pre, _post_reads(pre, 0.0, rng), case)
    assert held["outcome"] == "pass"
    assert held["direction_agreement"] is None and held["n_signed"] == 0
    assert held["reason"].startswith("direction_not_applicable")
    moved = ecu_effect_test(pre, _post_reads(pre, 3.0, rng), case)
    assert moved["outcome"] == "fail"


def test_pairs_are_one_to_one_and_counted_per_cell(rng: np.random.Generator) -> None:
    case = _case()
    pre = _predicted(_points(60, rng), 2.31)
    pre["cell"] = np.where(pre["rpm"] < 1000, "800x48", "1600x48")
    post = pd.concat([_post_reads(pre, 2.31, rng)] * 2, ignore_index=True)
    result = ecu_effect_test(pre, post, case)
    # 120 post reads, 60 design points: each point is used once.
    assert result["n_pairs"] == 60
    assert {k: v["n_pairs"] for k, v in result["cells"].items()} == {"1600x48": 40, "800x48": 20}


def _traces_hash(case: InterventionCase) -> dict[str, str]:
    assert case.ecu_outputs is not None
    traces = case.ecu_outputs.prediction.traces
    return {traces: case.ecu_outputs.prediction.files[traces]}


def _evaluate(case: InterventionCase, pre: pd.DataFrame, post: pd.DataFrame) -> dict[str, Any]:
    phi = post.assign(observed=1.0)
    return evaluate_ecu_outputs(
        case,
        _evaluate_points(pre),
        {"base_fuel_schedule": post, "target_phi": phi},
        artifact=_traces_hash(case),
    )


def _evaluate_points(pre: pd.DataFrame) -> pd.DataFrame:
    return pre.rename(
        columns={
            "observed": "observed:base_fuel_schedule",
            "pred_lo": "pred_lo:base_fuel_schedule",
            "pred_mid": "pred_mid:base_fuel_schedule",
            "pred_hi": "pred_hi:base_fuel_schedule",
        }
    ).assign(
        **{
            "observed:target_phi": 1.0,
            "pred_lo:target_phi": 0.0,
            "pred_mid:target_phi": 0.0,
            "pred_hi:target_phi": 0.0,
        }
    )


def test_the_verdict_is_sealed_and_cannot_stand_in_for_a_plant_verdict(
    rng: np.random.Generator,
) -> None:
    case = _case()
    pre = _predicted(_points(60, rng), 2.31)
    report = _evaluate(case, pre, _post_reads(pre, 2.31, rng))
    assert report["verdicts"] == {"base_fuel_schedule": "pass", "target_phi": "pass"}
    assert report["synthetic"] is True and "not evidence" in report["claim"]
    assert report["plant"].startswith("not used")
    record = report["verdict_record"]
    assert isinstance(record, EcuOutputVerdict) and record.verify()
    assert record.outcome("base_fuel_schedule") == "pass"
    edited = dataclasses.replace(
        record, outcomes=(("base_fuel_schedule", "pass", 999, 999), *record.outcomes[1:])
    )
    assert not edited.verify()
    # The same seal under the plant verdict's class does not verify: the body
    # names its kind.
    forged = InterventionVerdict(
        case=record.case,
        verdict="pass",
        synthetic=False,
        base_image_sha256=record.base_image_sha256,
        candidate_image_sha256=record.candidate_image_sha256,
        baseline_sha256="",
        plant_version="",
        qualified_regimes=(),
        seal=record.seal,
    )
    assert not forged.verify()


def test_recordings_are_declared_and_synthetic_never_mixes(rng: np.random.Generator) -> None:
    pre = _predicted(_points(60, rng), 2.31)
    post = _post_reads(pre, 2.31, rng)
    with pytest.raises(FuelPlantRefusalError) as undeclared:
        _evaluate(_case(post=["_sim-other-2026-10-06T000000"]), pre, post)
    assert undeclared.value.reason == "undeclared_recording"
    # A recorded case never scores _sim-* traces as observations.
    with pytest.raises(FuelPlantRefusalError) as observed:
        _evaluate(_case(synthetic=False), pre, post)
    assert observed.value.reason == "synthetic_as_observed"
    # A synthetic case never scores a recording.
    recorded = post.assign(session="2026-10-08T172943")
    with pytest.raises(FuelPlantRefusalError) as mixed:
        _evaluate(_case(post=["2026-10-08T172943"]), pre, recorded)
    assert mixed.value.reason == "synthetic_mismatch"
    # The traces' hash must be the one verify_prediction_artifact checked.
    case = _case()
    assert case.ecu_outputs is not None
    points = pre.rename(columns={"observed": "observed:base_fuel_schedule"})
    with pytest.raises(FuelPlantRefusalError) as unverified:
        evaluate_ecu_outputs(
            case, points, {"base_fuel_schedule": post, "target_phi": post}, artifact={}
        )
    assert unverified.value.reason == "prediction_not_verified"


# -- the artifact and the frozen rules ---------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_changed_prediction_artifact_refuses_by_name(tmp_path: Path) -> None:
    design, digest = "a" * 64, "b" * 64
    files = {
        "run/result.json": json.dumps(
            {"result_digest": digest, "cell_table": {"design_sha256": design}}
        ),
        "run/traces.parquet": "traces",
        "run/report.html": "<html>",
        "designs/d/design.json": json.dumps({"sha256": design}),
        "designs/d/design.parquet": "points",
    }
    for rel, body in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body)
    recorded = {rel: _sha(tmp_path / rel) for rel in files}

    def artifact(**over: Any) -> PredictionArtifact:
        fields: dict[str, Any] = {
            "artifact": "case",
            "recorded_in": "E08.7",
            "design_sha256": design,
            "result_digest": digest,
            "result": "run/result.json",
            "traces": "run/traces.parquet",
            "design": "designs/d/design.parquet",
            "design_record": "designs/d/design.json",
            "files": {k: recorded[k] for k in list(files)[:3]},
            "design_files": {k: recorded[k] for k in list(files)[3:]},
        }
        return PredictionArtifact.model_validate({**fields, **over})

    checked = verify_prediction_artifact(tmp_path, artifact())
    assert checked == recorded
    # A recorded digest that differs from the result's own.
    with pytest.raises(FuelPlantRefusalError) as wrong_digest:
        verify_prediction_artifact(tmp_path, artifact(result_digest="c" * 64))
    assert wrong_digest.value.reason == "prediction_artifact_changed"
    # One byte of the traces changed after the prediction was recorded.
    (tmp_path / "run/traces.parquet").write_text("traceS")
    with pytest.raises(FuelPlantRefusalError) as changed:
        verify_prediction_artifact(tmp_path, artifact())
    assert changed.value.reason == "prediction_artifact_changed"
    assert "run/traces.parquet" in str(changed.value)
    (tmp_path / "run/traces.parquet").unlink()
    with pytest.raises(FuelPlantRefusalError) as missing:
        verify_prediction_artifact(tmp_path, artifact())
    assert missing.value.reason == "prediction_artifact_missing"
    # A scored file must carry a recorded hash at all.
    with pytest.raises(ValidationError, match="recorded hash"):
        artifact(files={"run/result.json": recorded["run/result.json"]})


def test_the_real_cases_ecu_rules_are_frozen_and_name_e08_7s_hashes() -> None:
    case = load_interventions(PROFILE).case(CASE_ID)
    rules = case.ecu_outputs
    assert rules is not None
    # E08.7 item 5's recorded values, verbatim.
    assert rules.prediction.files == {
        "run/result.json": "0e1294711018511faa9ac4bc6cb258ccd35dacae2c9ae27093c9638aba8d34be",
        "run/report.html": "cd99ebcb3ca013d4e9ed5d557961ce50a974dff0cf8a997a24a45ee0c8bf1477",
        "run/traces.parquet": "92d35caa610b1f34b897fabc0398bc4eaf36f66b46307afe16b578505a772066",
        "run-200draws/result.json": (
            "71267e1633b467603bdce31cf6d09b63f5595e8f8c8d5d5690e5ca0e4ee57917"
        ),
        "run-200draws/report.html": (
            "5173dadd56ec584fbcd15f120e12c1a0565d296ea12fbc9ea99c2457c0c67cf4"
        ),
        "run-200draws/traces.parquet": (
            "f05b5c2d3bfa7e94bc370f051670f6cdee2ae0de90f712a545a616b00e4cc5c5"
        ),
    }
    assert rules.prediction.traces == "run-200draws/traces.parquet"
    assert rules.post == [
        "2026-10-08T172943",
        "2026-10-08T182550",
        "_capture-fuel-pulsation-2026-10-08T210012",
        "2026-10-10T153117",
        "2026-10-10T165941",
    ]
    # E07.4's own rules hash is untouched by the new block.
    assert case.frozen_rules is not None
    assert case.frozen_rules.sha256 == case.rules_sha256()
    assert case.rules_sha256().startswith("f522ff53")
    raw = _raw()
    for key, value in (
        ("interval_quantiles", [0.1, 0.9]),
        ("post", ["2026-10-08T172943"]),
    ):
        changed = copy.deepcopy(raw)
        changed["cases"][0]["ecu_outputs"][key] = value
        with pytest.raises(ValidationError, match="rules_changed"):
            InterventionFile.model_validate(changed)
    changed = copy.deepcopy(raw)
    changed["cases"][0]["ecu_outputs"]["prediction"]["files"]["run/traces.parquet"] = "d" * 64
    with pytest.raises(ValidationError, match="rules_changed"):
        InterventionFile.model_validate(changed)


def test_bfs_and_target_phi_read_no_trim_word_in_the_application_model() -> None:
    model, _ = load_application_model(PROFILE)
    found = trim_independence(model, ["base_fuel_schedule_word", "target_equiv_ratio"])
    assert found["trim_words"] == {
        "0xFFFF26B4": "short_term_trim_b1",
        "0xFFFF26B6": "short_term_trim_b2",
    }
    # Long-term trim has no RAM word in the model: said, not claimed checked.
    assert found["long_term_trim_in_model"] == []
    assert "base_fuel_schedule" in found["outputs"]["base_fuel_schedule_word"]["nodes"]
    assert "final_command_word" in found["outputs"]["target_equiv_ratio"]["nodes"]
    # A chain that read a trim word would refuse.
    broken = model.model_copy(deep=True)
    node = next(n for n in broken.nodes if n.id == "fuel_comp_product")
    node.reads.append(0xFFFF26B4)
    with pytest.raises(FuelPlantRefusalError) as reads_trim:
        trim_independence(broken, ["base_fuel_schedule_word"])
    assert reads_trim.value.reason == "output_reads_trim"


def test_a_post_read_is_built_as_a_design_point_and_names_each_state() -> None:
    profile, _ = load_fuel_plant(PROFILE, "fuel-mv-1")
    delay = load_b06(PROFILE, profile.delay)
    case = load_interventions(PROFILE).case(CASE_ID)
    assert case.ecu_outputs is not None
    bfs = case.ecu_outputs.outputs[0]
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    boundary = resolve_boundary(case, registry)
    no_fill = pd.DataFrame(
        {c: [] for c in ("route", "timestamp", "byte_lo", "byte_hi", "rpm_word_lo")}
    )
    # The committed slice is pre-flash: refused as a post-change recording.
    with pytest.raises(FuelPlantRefusalError) as wrong:
        ecu_output_reads(
            FIXTURE, bfs, profile, delay, no_fill, image_sha256="0" * 64, boundary=boundary
        )
    assert wrong.value.reason == "wrong_side"
    early = Boundary(datetime(2026, 9, 1, tzinfo=UTC), "exact", boundary.base, boundary.base)
    reads = ecu_output_reads(
        FIXTURE, bfs, profile, delay, no_fill, image_sha256="0" * 64, boundary=early
    )
    assert len(reads) > 0
    states = set(reads["state"])
    # No fill interval was offered, so no read is eligible, each named.
    assert "eligible" not in states
    assert states <= {"fill_byte_unpaired", "transient", "cold", "missing_input", "not_co_observed"}
    assert reads["regime"].isin(["closed_loop", "enrichment", "lean_target", "unknown"]).all()


def test_air_normalised_bfs_removes_an_airflow_difference_and_names_its_exclusions(
    rng: np.random.Generator,
) -> None:
    """Known answer: the post drive draws 8 % more air at the same rpm and
    fill interval, so raw BFS reads +8 % on top of the ROM's +2.31 %; BFS x
    rpm / MAF (K x FC / 128 at the steady fixed point) reads the ROM alone."""
    case = _case(amend=True)
    ceiling = float(case.ecu_amendment(2).bfs_ceiling_counts)
    pre = _predicted(_points(60, rng), 2.31)
    post = _post_reads(pre, 2.31, rng)
    # BFS = K x Qa / N x FC / 128: 8 % more air, and the post reads' rpm sit
    # 1 % above the design's (_post_reads), so N is 1 % larger.
    post["observed"] = post["observed"] * 1.08 / 1.01
    post["maf_gps"] = 10.8
    raw = ecu_effect_test(pre, post, case)
    assert raw["outcome"] == "fail"
    normalised = ecu_effect_test(pre, post, case, quantity="air_normalised", ceiling=ceiling)
    assert normalised["quantity"] == "air_normalised"
    assert normalised["outcome"] == "pass", normalised["reason"]
    assert normalised["observed_change_pct"]["median"] == pytest.approx(2.31, abs=0.2)
    # Clamped and airflow-less reads are excluded by name, never silently.
    post.loc[:2, "observed"] = ceiling
    post.loc[3:4, "maf_gps"] = np.nan
    named = ecu_effect_test(pre, post, case, quantity="air_normalised", ceiling=ceiling)
    assert named["exclusions"]["post_at_ceiling"] == 3
    assert named["exclusions"]["post_maf_unpaired"] == 2
    assert named["n_pairs"] == 55
    with pytest.raises(FuelPlantRefusalError) as unstated:
        ecu_effect_test(pre, post, case, quantity="air_normalised")
    assert unstated.value.reason == "ceiling_unstated"
    # Each verdict names the rules it was scored under.
    record = _evaluate(case, pre, _post_reads(pre, 2.31, rng))["verdict_record"]
    assert case.ecu_outputs is not None
    assert (record.rules_version, record.rules_sha256) == (
        1,
        case.ecu_outputs.frozen_rules.sha256,
    )
    amended = evaluate_ecu_outputs(
        case,
        _evaluate_points(pre),
        {"base_fuel_schedule": post, "target_phi": post.assign(observed=1.0)},
        artifact=_traces_hash(case),
        amendment=2,
    )
    assert amended["verdict_record"].rules_version == 2
    assert amended["verdict_record"].rules_sha256 == case.ecu_amendment(2).frozen_rules.sha256
    assert amended["effect_tests"]["base_fuel_schedule"]["quantity"] == "air_normalised"
    assert amended["effect_tests"]["target_phi"]["quantity"] == "raw"


def test_an_amendment_is_a_new_frozen_block_that_names_what_it_amends() -> None:
    case = load_interventions(PROFILE).case(CASE_ID)
    assert case.ecu_outputs is not None
    second = case.ecu_amendment(2)
    # The original block, its sha and E07.4's are untouched.
    assert case.ecu_rules_sha256().startswith("15eaa48b")
    assert case.ecu_outputs.frozen_rules.sha256 == case.ecu_rules_sha256()
    assert case.rules_sha256().startswith("f522ff53")
    assert second.amends == case.ecu_outputs.frozen_rules.sha256
    assert second.quantities == {"base_fuel_schedule": "air_normalised"}
    assert second.bfs_ceiling_counts == 255 << 8
    assert "no post-flash pair exists" in second.reason
    raw = _raw()
    for path, value in (
        (("quantities",), {"base_fuel_schedule": "raw"}),
        (("bfs_ceiling_counts",), 60000),
    ):
        changed = copy.deepcopy(raw)
        changed["cases"][0]["ecu_amendments"][0][path[0]] = value
        with pytest.raises(ValidationError, match="rules_changed"):
            InterventionFile.model_validate(changed)
    broken = copy.deepcopy(raw)
    broken["cases"][0]["ecu_amendments"][0]["amends"] = "e" * 64
    with pytest.raises(ValidationError, match="amendment_chain_broken"):
        InterventionFile.model_validate(broken)
    # Editing the original block is still refused: amend, never edit.
    edited = copy.deepcopy(raw)
    edited["cases"][0]["ecu_outputs"]["interval_quantiles"] = [0.1, 0.9]
    with pytest.raises(ValidationError, match="rules_changed"):
        InterventionFile.model_validate(edited)


def _appended(raw: dict[str, Any], names: list[str]) -> list[dict[str, Any]]:
    """Chained ``ecu_post_appended`` entries for ``names`` on the real case."""
    previous = raw["cases"][0]["ecu_outputs"]["frozen_rules"]["sha256"]
    out = []
    for name in names:
        previous = post_chain_sha256(previous, name, date(2026, 10, 20))
        out.append({"recording": name, "appended_on": "2026-10-20", "chain_sha256": previous})
    return out


def test_a_new_post_recording_is_appended_without_touching_any_frozen_rule() -> None:
    raw = _raw()
    new = ["2026-10-18T120000", "2026-10-19T080000"]
    appended = copy.deepcopy(raw)
    appended["cases"][0]["ecu_post_appended"] = _appended(raw, new)
    case = InterventionFile.model_validate(appended).case(CASE_ID)
    assert case.ecu_post_recordings() == [*FIVE, *new]
    # Every frozen hash is unchanged: the list is outside them.
    assert case.ecu_rules_sha256().startswith("15eaa48b")
    assert case.ecu_amendment(2).rules_sha256().startswith("c458d1a6")
    assert case.ecu_amendment(3).rules_sha256() == case.ecu_amendment(3).frozen_rules.sha256
    assert case.rules_sha256().startswith("f522ff53")
    # Reordered, edited or a middle entry removed: refused by name.
    for entries in (
        list(reversed(_appended(raw, new))),
        [_appended(raw, new)[1]],
        [{**_appended(raw, new)[0], "recording": "2026-10-18T130000"}],
    ):
        rewritten = copy.deepcopy(raw)
        rewritten["cases"][0]["ecu_post_appended"] = entries
        with pytest.raises(ValidationError, match="post_list_rewritten"):
            InterventionFile.model_validate(rewritten)
    # The declaration date is in the link: back-dating an entry is a rewrite.
    redated = copy.deepcopy(raw)
    redated["cases"][0]["ecu_post_appended"] = _appended(raw, new)
    redated["cases"][0]["ecu_post_appended"][0]["appended_on"] = "2026-10-17"
    with pytest.raises(ValidationError, match="post_list_rewritten"):
        InterventionFile.model_validate(redated)
    duplicate = copy.deepcopy(raw)
    duplicate["cases"][0]["ecu_post_appended"] = _appended(raw, [FIVE[0]])
    with pytest.raises(ValidationError, match="post_recording_duplicate"):
        InterventionFile.model_validate(duplicate)
    nameless = copy.deepcopy(raw)
    nameless["cases"][0]["ecu_post_appended"] = _appended(raw, ["drive-after-flash"])
    with pytest.raises(ValidationError, match="recording_time_unknown"):
        InterventionFile.model_validate(nameless)
    # The frozen five cannot be removed or reordered either.
    for five in (FIVE[1:], [FIVE[1], FIVE[0], *FIVE[2:]]):
        shrunk = copy.deepcopy(raw)
        shrunk["cases"][0]["ecu_outputs"]["post"] = five
        with pytest.raises(ValidationError, match="rules_changed"):
            InterventionFile.model_validate(shrunk)


def test_the_post_scope_scores_every_candidate_unless_named_and_only_after_the_flash(
    tmp_path: Path,
) -> None:
    raw = _raw()
    new = "2026-10-18T120000"
    appended = copy.deepcopy(raw)
    appended["cases"][0]["ecu_post_appended"] = _appended(raw, [new])
    case = InterventionFile.model_validate(appended).case(CASE_ID)
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    boundary = resolve_boundary(case, registry)
    for name in [*FIVE, new]:
        (tmp_path / name).mkdir()
    scope, unscored = post_scope(case, boundary, [*FIVE, new], tmp_path)
    assert (scope, unscored) == ([*FIVE, new], [])
    # Named: the five only, the new one reported, not refused.
    scope, unscored = post_scope(case, boundary, [*FIVE, new], tmp_path, recordings=FIVE)
    assert (scope, unscored) == (FIVE, [new])
    # A candidate pulled but not declared: the full scope refuses it.
    with pytest.raises(FuelPlantRefusalError) as undeclared:
        post_scope(load_interventions(PROFILE).case(CASE_ID), boundary, [*FIVE, new], tmp_path)
    assert undeclared.value.reason == "post_recording_undeclared"
    # An appended recording dated before the flash is refused.
    early = copy.deepcopy(raw)
    early["cases"][0]["ecu_post_appended"] = _appended(raw, ["2026-10-03T120000"])
    early_case = InterventionFile.model_validate(early).case(CASE_ID)
    (tmp_path / "2026-10-03T120000").mkdir()
    with pytest.raises(FuelPlantRefusalError) as before:
        post_scope(early_case, boundary, [*FIVE, "2026-10-03T120000"], tmp_path)
    assert before.value.reason == "post_recording_before_flash"


def test_an_appended_recording_is_scored_and_named_in_the_seal(rng: np.random.Generator) -> None:
    case = _case()
    raw = case.model_dump(mode="json")
    extra = "_sim-ecu-post-2026-10-07T000000"
    assert case.ecu_outputs is not None
    raw["ecu_post_appended"] = [
        {
            "recording": extra,
            "appended_on": "2026-10-20",
            "chain_sha256": post_chain_sha256(
                case.ecu_outputs.frozen_rules.sha256, extra, date(2026, 10, 20)
            ),
        }
    ]
    grown = InterventionCase.model_validate(raw)
    pre = _predicted(_points(60, rng), 2.31)
    post = pd.concat(
        [
            _post_reads(pre, 2.31, rng, n=30),
            _post_reads(pre, 2.31, rng).iloc[30:].assign(session=extra),
        ],
        ignore_index=True,
    )
    report = _evaluate(grown, pre, post)
    assert report["verdicts"]["base_fuel_schedule"] == "pass"
    record = report["verdict_record"]
    assert record.recordings == (POST, extra)
    assert (record.scope, record.candidates_not_scored) == ("all", ())
    # A subset verdict is sealed as one: relabelling its scope breaks the seal.
    assert not dataclasses.replace(record, scope="named").verify()
    assert not dataclasses.replace(record, candidates_not_scored=("x",)).verify()
    named = evaluate_ecu_outputs(
        grown,
        _evaluate_points(pre),
        {
            "base_fuel_schedule": post[post["session"] == POST],
            "target_phi": post[post["session"] == POST].assign(observed=1.0),
        },
        artifact=_traces_hash(grown),
        recordings=[POST],
        candidates_not_scored=[extra],
    )["verdict_record"]
    assert (named.scope, named.recordings, named.candidates_not_scored) == (
        "named",
        (POST,),
        (extra,),
    )
    assert named.verify()
    assert not dataclasses.replace(named, scope="all", candidates_not_scored=()).verify()
    with pytest.raises(FuelPlantRefusalError) as undeclared:
        _evaluate(case, pre, post)
    assert undeclared.value.reason == "undeclared_recording"


def test_v3_counts_only_pairs_the_prediction_resolves(rng: np.random.Generator) -> None:
    case = _case(amend=True)
    v3 = case.ecu_amendment(3)
    assert (v3.resolvable, v3.exclude_unknown_regime) == ("predicted_beyond_floor", True)
    kw: dict[str, Any] = {"resolvable": True, "exclude_unknown_regime": True}
    # (b) A known change beyond the floor still passes.
    pre = _predicted(_points(60, rng), 2.31)
    known = ecu_effect_test(pre, _post_reads(pre, 2.31, rng), case, **kw)
    assert known["outcome"] == "pass", known["reason"]
    assert known["n_pairs"] == 60 and known["exclusions"]["pre_unresolvable"] == 0
    # (c) The reversed prediction fails.
    reversed_ = _predicted(_points(60, rng), -2.31)
    assert (
        ecu_effect_test(reversed_, _post_reads(reversed_, 2.31, rng), case, **kw)["outcome"]
        == "fail"
    )
    # A prediction inside the floor cannot be tested there (version 2 let
    # such points carry the verdict: the real control is the slow test).
    small = _predicted(_points(60, rng), 0.05, half_width=0.05)
    v3_small = ecu_effect_test(small, _post_reads(small, 0.0, rng), case, **kw)
    assert v3_small["outcome"] == "insufficient_evidence"
    assert v3_small["reason"].startswith("prediction_unresolvable")
    assert v3_small["empty"].startswith("prediction_unresolvable")
    assert (v3_small["n_pairs"], v3_small["pairs_before_resolvable"]) == (0, 60)
    assert v3_small["exclusions"]["pre_unresolvable"] == 60
    # Mixed: only the resolvable points are paired and counted.
    mixed = pd.concat([small.iloc[:30], pre.iloc[30:]], ignore_index=True)
    counted = ecu_effect_test(mixed, _post_reads(mixed, 2.31, rng), case, **kw)
    assert counted["n_pairs"] == 30 and counted["outcome"] == "pass"
    # unknown-regime reads are excluded by name on both sides.
    unknown = ecu_effect_test(
        pre.assign(regime="unknown"),
        _post_reads(pre, 2.31, rng).assign(regime="unknown"),
        case,
        **kw,
    )
    assert unknown["exclusions"]["pre_regime_unknown"] == 60
    assert unknown["exclusions"]["post_regime_unknown"] == 60
    assert unknown["n_pairs"] == 0
    # Version 2 still pairs unknown with unknown (match_pairs compares labels).
    assert (
        ecu_effect_test(
            pre.assign(regime="unknown"), _post_reads(pre, 2.31, rng).assign(regime="unknown"), case
        )["n_pairs"]
        == 60
    )


def test_the_artifact_is_verified_before_any_recording_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.analysis.plant_validation as pv

    def never(*_: Any, **__: Any) -> Any:
        raise AssertionError("a recording was touched before the artifact was verified")

    for name in ("candidate_recordings", "post_scope", "pre_observed", "fill_samples"):
        monkeypatch.setattr(pv, name, never)
    monkeypatch.setattr(pv, "ecu_output_reads", never)
    monkeypatch.setattr(pv, "read_channels", never)
    case = load_interventions(PROFILE).case(CASE_ID)
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    with pytest.raises(FuelPlantRefusalError) as missing:
        pv.score_ecu_outputs(
            case,
            resolve_boundary(case, registry),
            registry,
            profile_dir=PROFILE,
            sessions_root=tmp_path / "sessions",
            predictions_root=tmp_path / "predictions",
            resolver=never,
            fuel_type="gasoline",
        )
    assert missing.value.reason == "prediction_artifact_missing"


# -- private: the real verdict (slow; skips by name) --------------------------------------

SESSIONS_ROOT = Path.home() / "canary-data" / "sessions" / "nissan-xterra-2008"
PREDICTIONS_ROOT = Path.home() / "canary-data" / "sim" / "predictions"
#: Pre-flash drives on the base image used as a null control (no change).
#: The five post-flash recordings E08.7 lists, scored by name: a newly
#: pulled post-flash session does not change what these tests score.
FIVE = [
    "2026-10-08T172943",
    "2026-10-08T182550",
    "_capture-fuel-pulsation-2026-10-08T210012",
    "2026-10-10T153117",
    "2026-10-10T165941",
]
NULL_DRIVES = ("_capture-tuning-2026-09-22T235224", "_capture-tuning-2026-09-22T170459")


def _private_inputs() -> tuple[InterventionCase, Boundary, Any, Any]:
    from src.analysis.fuel_comp_inputs import load_fuel_comp_declaration
    from src.analysis.fuel_comp_rom import RevisionImageResolver
    from src.analysis.rom_map import load_rom_map
    from tests.rom_fixtures import OPERATOR_ROMS

    case = load_interventions(PROFILE).case(CASE_ID)
    assert case.ecu_outputs is not None
    artifact = PREDICTIONS_ROOT / case.ecu_outputs.prediction.artifact
    if not artifact.is_dir():
        pytest.skip(f"prediction_artifact_not_here: {artifact}")
    absent = [n for n in (*FIVE, *NULL_DRIVES) if not (SESSIONS_ROOT / n).is_dir()]
    if absent or not (SESSIONS_ROOT / "_capture-tuning-2026-09-23T142926").is_dir():
        pytest.skip(f"drives_not_here: {absent or ['_capture-tuning-2026-09-23T142926']}")
    images = [OPERATOR_ROMS[k] for k in ("r20260920", "r20261004")]
    if not all(p.is_file() for p in images):
        pytest.skip(f"image_not_found: {[str(p) for p in images if not p.is_file()]}")
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    decl = load_fuel_comp_declaration(PROFILE)
    resolver = RevisionImageResolver(
        registry,
        load_rom_map(PROFILE),
        decl.rom,
        rom_dirs=[images[0].parent],
        slot=decl.program.map_slot,
    )
    return case, resolve_boundary(case, registry), registry, resolver


@pytest.fixture(scope="module")
def real_score() -> tuple[dict[str, Any], pd.DataFrame, dict[str, pd.DataFrame]]:
    from src.analysis.plant_validation import score_ecu_outputs

    case, boundary, registry, resolver = _private_inputs()
    return score_ecu_outputs(
        case,
        boundary,
        registry,
        profile_dir=PROFILE,
        sessions_root=SESSIONS_ROOT,
        predictions_root=PREDICTIONS_ROOT,
        resolver=resolver,
        fuel_type="gasoline",
        recordings=FIVE,
    )


@pytest.mark.slow
def test_the_real_verdict_is_insufficient_evidence_for_both_outputs(
    real_score: tuple[dict[str, Any], pd.DataFrame, dict[str, pd.DataFrame]],
) -> None:
    report, points, reads = real_score
    case = load_interventions(PROFILE).case(CASE_ID)
    assert case.ecu_outputs is not None
    # The artifact was checked first, against E08.7's values.
    checked = report["prediction"]["hashes_checked"]
    assert {k: checked[k] for k in case.ecu_outputs.prediction.files} == (
        case.ecu_outputs.prediction.files
    )
    assert report["plant_version_of_design"] == "fuel-mv-1"
    assert len(points) == 298
    assert report["verdicts"] == {
        "base_fuel_schedule": "insufficient_evidence",
        "target_phi": "insufficient_evidence",
    }
    record = report["verdict_record"]
    assert record.verify() and record.synthetic is False
    assert (record.rules_version, record.rules_sha256[:8]) == (1, "15eaa48b")
    assert record.outcomes == (
        ("base_fuel_schedule", "insufficient_evidence", 0, 103),
        ("target_phi", "insufficient_evidence", 0, 57),
    )
    bfs = report["effect_tests"]["base_fuel_schedule"]
    assert bfs["quantity"] == "raw"
    # 33 BFS reads on the post-flash drives, all on the live profile's
    # once-a-minute DID poll, none within 0.3 s of an rpm or MAF read.
    assert bfs["post_reads"] == 33
    assert bfs["post_states"] == {"not_co_observed": 33}
    assert bfs["post_sessions"] == [
        "2026-10-08T172943",
        "2026-10-08T182550",
        "2026-10-10T153117",
        "2026-10-10T165941",
    ]
    assert bfs["empty"].startswith("filtered")
    assert bfs["exclusions"] == {
        "pre_observed_unpaired": 4,
        "pre_not_predicted": 1,
        "post_observed_unpaired": 0,
    }
    assert bfs["noise_floor_pct"] == pytest.approx(4.1106, abs=1e-3)
    assert bfs["reason"] == "0 pairs < 20"
    phi = report["effect_tests"]["target_phi"]
    assert phi["post_reads"] == 0
    assert phi["empty"].startswith("never_captured")
    assert (phi["floor_pairs"], phi["noise_floor_pct"]) == (57, 0.0)
    assert phi["exclusions"]["pre_observed_unpaired"] == 133
    assert reads["target_phi"].empty
    # Trims: neither chain reads a trim word.
    assert report["learned_state"]["outputs"]["base_fuel_schedule_word"]["trim_words_read"] == []
    assert report["learned_state"]["outputs"]["target_equiv_ratio"]["trim_words_read"] == []


@pytest.mark.slow
def test_a_null_control_fails_raw_bfs_and_passes_the_air_normalised_amendment(
    real_score: tuple[dict[str, Any], pd.DataFrame, dict[str, pd.DataFrame]],
) -> None:
    """Known answer: the same image on both sides, so the change is zero.

    Two pre-flash drives on ZS44A-20260920 stand in for "post", scored against
    the design points with a predicted change of exactly 0. Target phi holds
    (closed loop, 1.0 on both). Raw BFS does not (version 1): at the case's
    matching tolerances the same ROM moves by more than the within-recording
    floor across drives. BFS x rpm / MAF (amendment 2, frozen before this was
    re-run) passes on both. Measured, not asserted away."""
    from src.analysis.plant_validation import _output_frame, fill_samples

    _, points, _ = real_score
    case, boundary, registry, resolver = _private_inputs()
    assert case.ecu_outputs is not None
    profile, _ = load_fuel_plant(PROFILE, "fuel-mv-1")
    delay = load_b06(PROFILE, profile.delay)
    early = Boundary(datetime(2026, 9, 1, tzinfo=UTC), "exact", boundary.base, boundary.base)
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for name in NULL_DRIVES:
        fill = fill_samples(SESSIONS_ROOT / name, PROFILE, registry, resolver, fuel_type="gasoline")
        for spec in case.ecu_outputs.outputs:
            reads = ecu_output_reads(
                SESSIONS_ROOT / name,
                spec,
                profile,
                delay,
                fill,
                image_sha256=case.base_image_sha256,
                boundary=early,
            )
            pre = _output_frame(points, spec.id).assign(pred_lo=0.0, pred_mid=0.0, pred_hi=0.0)
            found[(name, spec.id)] = ecu_effect_test(pre, reads, case)
            if spec.id == "base_fuel_schedule":
                found[(name, "normalised")] = ecu_effect_test(
                    pre,
                    reads,
                    case,
                    quantity="air_normalised",
                    ceiling=float(case.ecu_amendment(2).bfs_ceiling_counts),
                )
    for name in NULL_DRIVES:
        assert found[(name, "target_phi")]["outcome"] == "pass"
        assert found[(name, "target_phi")]["observed_change_pct"]["median"] == 0.0
    late, early_drive = (found[(n, "base_fuel_schedule")] for n in NULL_DRIVES)
    assert (late["n_pairs"], late["outcome"]) == (114, "fail")
    assert late["observed_change_pct"]["median"] == pytest.approx(8.56, abs=0.01)
    assert late["within_uncertainty"] == pytest.approx(0.035, abs=0.001)
    assert (early_drive["n_pairs"], early_drive["outcome"]) == (93, "fail")
    assert early_drive["observed_change_pct"]["median"] == pytest.approx(-0.21, abs=0.01)
    assert early_drive["within_uncertainty"] == pytest.approx(0.731, abs=0.001)
    late_n, early_n = (found[(n, "normalised")] for n in NULL_DRIVES)
    for result in (late_n, early_n):
        assert result["outcome"] == "pass"
        assert result["noise_floor_pct"] == pytest.approx(2.4379, abs=1e-3)
        assert result["floor_pairs"] == 103
        assert {k: v for k, v in result["exclusions"].items() if "ceiling" in k or "maf" in k} == {
            "pre_at_ceiling": 0,
            "pre_maf_unpaired": 0,
            "post_at_ceiling": 0,
            "post_maf_unpaired": 0,
        }
    assert late_n["n_pairs"] == 114
    assert late_n["observed_change_pct"]["median"] == pytest.approx(-0.33, abs=0.01)
    assert late_n["within_uncertainty"] == pytest.approx(0.851, abs=0.001)
    assert early_n["n_pairs"] == 93
    assert early_n["observed_change_pct"]["median"] == pytest.approx(-0.23, abs=0.01)
    assert early_n["within_uncertainty"] == pytest.approx(0.860, abs=0.001)


@pytest.mark.slow
def test_the_amended_real_verdict_is_still_insufficient_evidence() -> None:
    """Pairing, not the quantity, is the limit on the post-flash drives."""
    from src.analysis.plant_validation import score_ecu_outputs

    case, boundary, registry, resolver = _private_inputs()
    report, points, _ = score_ecu_outputs(
        case,
        boundary,
        registry,
        profile_dir=PROFILE,
        sessions_root=SESSIONS_ROOT,
        predictions_root=PREDICTIONS_ROOT,
        resolver=resolver,
        fuel_type="gasoline",
        amendment=2,
        recordings=FIVE,
    )
    record = report["verdict_record"]
    assert record.verify()
    assert (record.rules_version, record.rules_sha256) == (
        2,
        case.ecu_amendment(2).frozen_rules.sha256,
    )
    assert record.outcomes == (
        ("base_fuel_schedule", "insufficient_evidence", 0, 103),
        ("target_phi", "insufficient_evidence", 0, 57),
    )
    bfs = report["effect_tests"]["base_fuel_schedule"]
    assert bfs["quantity"] == "air_normalised"
    assert bfs["post_states"] == {"not_co_observed": 33}
    assert bfs["noise_floor_pct"] == pytest.approx(2.4379, abs=1e-3)
    assert bfs["exclusions"]["pre_at_ceiling"] == 0
    assert int(points["draws_at_ceiling:base_fuel_schedule"].sum()) == 0
    assert report["effect_tests"]["target_phi"]["quantity"] == "raw"


@pytest.mark.slow
def test_v3_does_not_pass_a_no_change_drive_against_the_real_prediction(
    real_score: tuple[dict[str, Any], pd.DataFrame, dict[str, pd.DataFrame]],
) -> None:
    """Control (a): the two pre-flash drives on the base image, scored against
    the REAL prediction. Version 2 passes 235224 (direction 0.762 on 21 signed
    pairs, within 0.904; 73 of its 114 pairs in cell 800x48); version 3 gives
    ``insufficient_evidence`` named ``prediction_unresolvable`` on both."""
    from src.analysis.plant_validation import _output_frame, fill_samples

    _, points, _ = real_score
    case, boundary, registry, resolver = _private_inputs()
    assert case.ecu_outputs is not None
    profile, _ = load_fuel_plant(PROFILE, "fuel-mv-1")
    delay = load_b06(PROFILE, profile.delay)
    early = Boundary(datetime(2026, 9, 1, tzinfo=UTC), "exact", boundary.base, boundary.base)
    spec = case.ecu_outputs.outputs[0]
    pre = _output_frame(points, spec.id)
    ceiling = float(case.ecu_amendment(3).bfs_ceiling_counts)
    found: dict[tuple[str, int], dict[str, Any]] = {}
    for name in NULL_DRIVES:
        fill = fill_samples(SESSIONS_ROOT / name, PROFILE, registry, resolver, fuel_type="gasoline")
        reads = ecu_output_reads(
            SESSIONS_ROOT / name,
            spec,
            profile,
            delay,
            fill,
            image_sha256=case.base_image_sha256,
            boundary=early,
        )
        found[(name, 2)] = ecu_effect_test(
            pre, reads, case, quantity="air_normalised", ceiling=ceiling
        )
        found[(name, 3)] = ecu_effect_test(
            pre,
            reads,
            case,
            quantity="air_normalised",
            ceiling=ceiling,
            resolvable=True,
            exclude_unknown_regime=True,
        )
    v2 = found[(NULL_DRIVES[0], 2)]
    assert (v2["outcome"], v2["n_pairs"], v2["n_signed"]) == ("pass", 114, 21)
    assert v2["direction_agreement"] == pytest.approx(0.762, abs=0.001)
    assert v2["within_uncertainty"] == pytest.approx(0.904, abs=0.001)
    assert v2["cells"]["800x48"]["n_pairs"] == 73
    assert found[(NULL_DRIVES[1], 2)]["outcome"] == "fail"
    for name, before in zip(NULL_DRIVES, (106, 90), strict=True):
        v3 = found[(name, 3)]
        assert v3["outcome"] == "insufficient_evidence"
        assert v3["reason"].startswith("prediction_unresolvable")
        assert (v3["n_pairs"], v3["pairs_before_resolvable"]) == (0, before)
        assert v3["floor_pairs"] == 100
        assert v3["noise_floor_pct"] == pytest.approx(2.4399, abs=1e-3)
        # Every predicted point in a named regime with a BFS read is inside the floor.
        assert v3["exclusions"]["pre_unresolvable"] == 274
        assert v3["exclusions"]["pre_regime_unknown"] == 20


@pytest.mark.slow
def test_the_v3_real_verdict_is_still_insufficient_evidence() -> None:
    from src.analysis.plant_validation import score_ecu_outputs

    case, boundary, registry, resolver = _private_inputs()
    report, _, _ = score_ecu_outputs(
        case,
        boundary,
        registry,
        profile_dir=PROFILE,
        sessions_root=SESSIONS_ROOT,
        predictions_root=PREDICTIONS_ROOT,
        resolver=resolver,
        fuel_type="gasoline",
        amendment=3,
        recordings=FIVE,
    )
    record = report["verdict_record"]
    assert record.verify() and record.recordings == tuple(FIVE)
    assert record.scope == "named"
    assert (record.rules_version, record.rules_sha256) == (
        3,
        case.ecu_amendment(3).frozen_rules.sha256,
    )
    assert record.outcomes == (
        ("base_fuel_schedule", "insufficient_evidence", 0, 100),
        ("target_phi", "insufficient_evidence", 0, 54),
    )
    bfs = report["effect_tests"]["base_fuel_schedule"]
    assert bfs["post_states"] == {"not_co_observed": 33}
    assert bfs["reason"] == "0 pairs < 20"
    assert report["effect_tests"]["target_phi"]["empty"].startswith("never_captured")
