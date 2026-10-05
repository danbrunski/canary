"""E03.2: detectable and distinguishable fuel-mixture hypotheses.

Known-answer and labelled synthetic controls over the vehicle's committed
``fault_hypotheses.yaml`` (v2). Synthetic observations are generated from a
signature and labelled as such in each test name; the known answer is the
WOT-lean pattern of 2026-09-21/23 (delivery lean at enrichment, no fuel
pressure logged).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from src.analysis.fault_model import (
    FAULT_MODEL_FILE,
    FaultHypothesis,
    FaultModel,
    FaultModelError,
    Feature,
    Observation,
    evaluate_observations,
    load_fault_model,
    observed_sign,
    score,
    structural_analysis,
)
from src.evidence import (
    Conditions,
    Evidence,
    MemoryLedger,
    Provenance,
    Quality,
    Quantity,
    Result,
    Retraction,
    SourceRef,
    VehicleIdentity,
)

MODEL_PATH = Path("data/nissan-xterra-2008") / FAULT_MODEL_FILE

#: The Mode 01 delivery channels plus trims, as a 2026-09 tuning capture logged.
MODE01 = frozenset(
    {
        "obd:COMMANDED_EQUIV_RATIO",
        "obd:O2_WR_LAMBDA_S1",
        "obd:RPM",
        "obd:FUEL_STATUS",
        "obd:ENGINE_LOAD",
        "obd:MAF",
        "obd:SHORT_FUEL_TRIM_1",
        "obd:LONG_FUEL_TRIM_1",
    }
)
WITH_PRESSURE = MODE01 | {"sensor:FUEL_PRESSURE"}
PRESSURE_FIRST = "Log fuel pressure through a full-throttle pull"


@pytest.fixture(scope="module")
def model() -> FaultModel:
    loaded, sha = load_fault_model(MODEL_PATH)
    assert len(sha) == 64
    return loaded


def _raw() -> dict[str, Any]:
    data = yaml.safe_load(MODEL_PATH.read_text())
    assert isinstance(data, dict)
    return data


def test_the_artifact_declares_the_three_starting_hypotheses(model: FaultModel) -> None:
    names = {h.name for h in model.modelled}
    assert {"maf_transfer_error", "fuel_delivery_sag", "fixed_quantity_error"} <= names
    assert model.version == 2
    assert any("Single fault" in text for text in model.assumptions)


def test_no_hypothesis_but_sag_claims_anything_about_pressure(model: FaultModel) -> None:
    """Whether the calibration absorbs droop is unknown, so only sag (the claim
    that uncorrected droop explains the lean) constrains pressure beyond
    "it does not rise with flow"."""
    for hypothesis in model.modelled:
        for residual in ("pressure_flow_loss", "fuel_pressure_droop"):
            for sign in hypothesis.signature[residual].values():
                if hypothesis.name == "fuel_delivery_sag" and residual == "pressure_flow_loss":
                    assert sign == "+"
                else:
                    assert sign in ("nonnegative", "any"), (hypothesis.name, residual)


def test_without_fuel_pressure_maf_error_and_delivery_sag_stay_unresolved(
    model: FaultModel,
) -> None:
    report = structural_analysis(model, MODE01)
    pair = report.pair("maf_transfer_error", "fuel_delivery_sag")
    assert pair.verdict != "distinguishable"
    assert pair in report.indistinguishable
    assert pair.measurements[0].startswith(PRESSURE_FIRST)
    absent = [s for s in pair.separators if s.feature.residual == "pressure_flow_loss"]
    assert absent and all(s.excluded and s.excluded.kind == "channel_absent" for s in absent)


def test_with_pressure_the_pair_is_only_possibly_separable(model: FaultModel) -> None:
    """Pressure can refute sag (a flow loss too small to explain the lean) but
    cannot refute a MAF error, which allows any non-negative droop."""
    report = structural_analysis(model, WITH_PRESSURE)
    pair = report.pair("maf_transfer_error", "fuel_delivery_sag")
    assert pair.verdict == "possibly"
    loss = next(s for s in pair.separators if s.feature.residual == "pressure_flow_loss")
    assert loss.excluded is None and not loss.guaranteed
    # The 2026-10-04 drive logged pressure but never reached enrichment.
    part_load = structural_analysis(model, WITH_PRESSURE, covered_regions={"high_airflow"})
    uncovered = part_load.pair("maf_transfer_error", "fuel_delivery_sag")
    assert any(
        s.excluded is not None and s.excluded.kind == "region_uncovered"
        for s in uncovered.separators
        if s.feature.residual == "pressure_flow_loss"
    )


def test_the_report_names_every_indistinguishable_pair(model: FaultModel) -> None:
    report = structural_analysis(model, WITH_PRESSURE)
    listed = {frozenset(p) for p in report.as_dict()["indistinguishable"]}
    assert frozenset({"maf_transfer_error", "lambda_sensor_bias"}) in listed
    pair = report.pair("maf_transfer_error", "lambda_sensor_bias")
    assert pair.verdict == "indistinguishable"
    assert any("wideband" in m.lower() for m in pair.measurements)


def test_the_throttle_fill_path_is_never_counted_as_a_separator(model: FaultModel) -> None:
    channels = WITH_PRESSURE | {"mem:ECM_FUEL_COMP_X_INPUT"}
    report = structural_analysis(model, channels)
    throttle = [e for e in report.excluded if e.feature.residual == "throttle_fill_vs_maf"]
    assert throttle and all(e.kind == "unqualified" for e in throttle)
    assert all("independent airflow sensor" in e.detail for e in throttle)
    for pair in report.pairs:
        assert all(f.residual != "throttle_fill_vs_maf" for f in pair.usable_separators)
    pair = report.pair("maf_transfer_error", "fuel_delivery_sag")
    assert any(s.feature.residual == "throttle_fill_vs_maf" for s in pair.separators)


def test_a_fixed_quantity_error_is_undetectable_on_qualified_residuals_today(
    model: FaultModel,
) -> None:
    report = structural_analysis(model, WITH_PRESSURE)
    # Only the unqualified trims are guaranteed to see it; a flow loss it
    # merely allows does not make it detectable.
    assert report.detectability["fixed_quantity_error"] == "possibly"
    assert report.detectability["fuel_delivery_sag"] == "detectable"
    assert report.pair("fixed_quantity_error", "maf_transfer_error").verdict == "distinguishable"


def _representative(allowed: frozenset[str]) -> str:
    for sign in ("+", "-", "0"):
        if sign in allowed:
            return sign
    raise AssertionError(allowed)


@pytest.mark.parametrize(
    "name",
    [
        "no_fault",
        "maf_transfer_error",
        "fuel_delivery_sag",
        "fixed_quantity_error",
        "lambda_sensor_bias",
    ],
)
def test_synthetic_control_each_signature_is_consistent_with_its_own_observations(
    model: FaultModel, name: str
) -> None:
    """SYNTHETIC: observations generated from the hypothesis's own signature."""
    hypothesis = model.hypothesis(name)
    observations = [
        Observation(
            f,
            _representative(hypothesis.allowed(f.residual, f.region)),  # type: ignore[arg-type]
            evidence=f"synthetic:{name}",
        )
        for f in model.features
    ]
    report = evaluate_observations(model, observations)
    assert name in report.consistent
    for other in model.modelled:
        if other.name == name:
            continue
        separable = any(
            not (other.allowed(f.residual, f.region) & hypothesis.allowed(f.residual, f.region))
            for f in model.features
        )
        if separable:
            assert other.name not in report.consistent, (name, other.name)


def test_synthetic_control_pressure_at_a_pull_refutes_sag_only_when_flow_holds(
    model: FaultModel,
) -> None:
    """SYNTHETIC: lean at enrichment, with steady and with collapsing pressure."""
    lean = Observation(Feature("delivery_error", "enrichment"), "+", evidence="synthetic:lean")
    steady = Observation(Feature("pressure_flow_loss", "enrichment"), "0", evidence="synthetic")
    falling = Observation(Feature("pressure_flow_loss", "enrichment"), "+", evidence="synthetic")
    held = evaluate_observations(model, [lean, steady])
    assert "fuel_delivery_sag" not in held.consistent
    assert "maf_transfer_error" in held.consistent
    # A large droop is consistent with sag, and still with a MAF error whose
    # calibration absorbs the droop: it does not resolve the pair.
    collapsed = evaluate_observations(model, [lean, falling])
    assert {"maf_transfer_error", "fuel_delivery_sag"} <= set(collapsed.consistent)


def test_closed_loop_droop_refutes_nothing(model: FaultModel) -> None:
    """The 2026-10-04 part-load droop: no hypothesis forbids it."""
    droop = Observation(Feature("fuel_pressure_droop", "high_airflow"), "+", evidence="ev")
    assert set(evaluate_observations(model, [droop]).consistent) == {h.name for h in model.modelled}


def test_known_answer_the_wot_lean_pattern_leaves_three_explanations(model: FaultModel) -> None:
    """The 2026-09-21/23 shape: lean at enrichment, no fuel pressure, trims unqualified."""
    observations = [
        Observation(Feature("delivery_error", "enrichment"), observed_sign(3.3, 2.5)),
        Observation(
            Feature("closed_loop_trim", "low_airflow"), None, unevaluable="unqualified: no floor"
        ),
        Observation(Feature("pressure_flow_loss", "enrichment"), None, unevaluable="missing_input"),
    ]
    report = evaluate_observations(model, observations)
    assert set(report.consistent) == {
        "maf_transfer_error",
        "fuel_delivery_sag",
        "lambda_sensor_bias",
    }
    fixed = report.of("fixed_quantity_error")
    assert not fixed.consistent and fixed.conflicts[0].feature.region == "enrichment"


def test_conflicting_observations_of_one_feature_are_refused(model: FaultModel) -> None:
    feature = Feature("delivery_error", "enrichment")
    with pytest.raises(FaultModelError, match="data conflict"):
        evaluate_observations(
            model, [Observation(feature, "+", "ev-a"), Observation(feature, "-", "ev-b")]
        )


def _ledger(tmp_path: Path) -> MemoryLedger:
    return MemoryLedger(tmp_path / "memory.jsonl", vehicle="truck")


def _evidence(ledger: MemoryLedger) -> str:
    stamp = datetime(2026, 10, 1, tzinfo=UTC)
    record = Evidence(
        vehicle=VehicleIdentity(profile="truck", vin=None, program_id=None, calibration=None),
        provenance=Provenance(
            producer="replay_frequency", producer_kind="tool", version="1", code_version="x"
        ),
        conditions=Conditions(regime="all conditions", vehicle_state=None),
        valid_from=stamp,
        subject="likelihood:support",
        sources=(
            SourceRef(
                session="2026-10-01T000000",
                window_start=stamp,
                window_end=stamp,
                channels=("obd:RPM",),
                n_samples=1,
            ),
        ),
        input_hashes={"x": "0" * 64},
        policy={"x": 1},
        values={"rate": Quantity(value=0.5, unit="1")},
        quality=Quality(n_observations=1, timestamp_basis=None),
        result=Result(status="evaluated"),
    )
    return ledger.append(record).id


def test_a_language_hypothesis_stays_unscored_until_modelled(
    model: FaultModel, tmp_path: Path
) -> None:
    extended = model.with_language_hypothesis(
        "exhaust_leak", "An exhaust leak ahead of the sensor reads lean"
    )
    assert structural_analysis(extended, MODE01).unscored == ("exhaust_leak",)
    assert evaluate_observations(extended, []).unscored == ("exhaust_leak",)
    with pytest.raises(FaultModelError):
        evaluate_observations(extended, []).of("exhaust_leak")
    refused = {r.hypothesis: r.reason for r in score(extended, _ledger(tmp_path)).refused}
    assert "unscored until modelled" in refused["exhaust_leak"]
    with pytest.raises(ValidationError, match="unscored until it is modelled"):
        FaultHypothesis(
            name="x",
            label="Generated",
            origin="language",
            signature={"delivery_error": {"enrichment": "+"}},
        )


def test_no_likelihood_without_evidence_that_resolves(model: FaultModel, tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    scores = score(model, ledger)
    assert scores.values == {}
    assert {r.hypothesis for r in scores.refused} == {h.name for h in model.hypotheses}
    data = _raw()
    name = data["hypotheses"][1]["name"]
    data["hypotheses"][1]["likelihood"] = {"value": 0.5, "evidence": [], "method": "guess"}
    with pytest.raises(ValidationError):
        FaultModel.model_validate(data)
    # An id that was never minted is refused, not trusted.
    data["hypotheses"][1]["likelihood"] = {
        "value": 0.5,
        "evidence": ["ev-invented"],
        "method": "replay frequency",
    }
    invented = score(FaultModel.model_validate(data), ledger)
    assert name not in invented.values
    assert "does not resolve" in {r.hypothesis: r.reason for r in invented.refused}[name]
    # A real one is accepted ...
    real = _evidence(ledger)
    data["hypotheses"][1]["likelihood"]["evidence"] = [real]
    backed = FaultModel.model_validate(data)
    assert score(backed, ledger).evidence[name] == (real,)
    # ... until it is retracted.
    stamp = datetime(2026, 10, 2, tzinfo=UTC)
    ledger.append(
        Retraction(
            vehicle=VehicleIdentity(profile="truck", vin=None, program_id=None, calibration=None),
            provenance=Provenance(producer="operator", producer_kind="operator", version="1"),
            valid_from=stamp,
            retracts=(real,),
            reason="test",
        )
    )
    assert name not in score(backed, ledger).values


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        (lambda d: d["hypotheses"][1]["signature"].pop("pressure_flow_loss"), "unstated"),
        (lambda d: d["residuals"][3].pop("unqualified_reason"), "says why"),
        (
            lambda d: d["residuals"][5].update(channel_sets=[["sensor:WIDEBAND"]]),
            "invented channel",
        ),
        (lambda d: d["hypotheses"][2].update(label="Sag of 3 psi"), "number"),
        (lambda d: d["regions"].append(d["regions"][0]), "duplicate region"),
    ],
)
def test_the_schema_refuses_implicit_or_invented_content(edit: Any, match: str) -> None:
    data = _raw()
    edit(data)
    with pytest.raises(ValidationError, match=match):
        FaultModel.model_validate(data)


def test_negative_control_a_sag_that_claims_no_flow_loss_is_inseparable(
    model: FaultModel,
) -> None:
    """The analysis can fail: let sag allow any non-negative flow loss, like
    the others, and fuel pressure no longer separates it from a MAF error."""
    data = _raw()
    sag = next(h for h in data["hypotheses"] if h["name"] == "fuel_delivery_sag")
    sag["signature"]["pressure_flow_loss"] = {"enrichment": "nonnegative"}
    edited = FaultModel.model_validate(data)
    pair = structural_analysis(edited, WITH_PRESSURE).pair(
        "maf_transfer_error", "fuel_delivery_sag"
    )
    assert all(s.feature.residual != "pressure_flow_loss" for s in pair.separators)
