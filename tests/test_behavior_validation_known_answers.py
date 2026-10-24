"""B02.2 — known answers and counterexamples on committed real-data slices.

The slices under ``tests/fixtures/behavior_validation/{historical,corrected}``
are the evaluated columns of real sessions, written by
``scripts/evaluate_behavior_models.py --export-rows`` through the shared
preprocessing chain; ``evidence_*.yaml`` pins the source sessions by content
hash. Expectations are the numbers the 2026-09-21 review measured with its
temporary evaluator, re-derived here by the repo evaluator — the known answer
comes first, then the counterexamples. Nothing here labels a September
exceedance a false positive: those drives are unlabelled.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis.behavior_validation import (
    EventPolicy,
    apply_criteria,
    evaluate_rows,
    fold_metrics,
    load_validation_document,
)
from src.analysis.refit import _check_publish_gate, default_recipes, legacy_reproduction_spec
from src.behavior.behavior_models import (
    STATUS_LEGACY_NO_CONTRACT,
    STATUS_NO_HELD_OUT_VALIDATION,
    BehaviorModel1D,
    BehaviorModel2D,
    load_behavior_models,
    serving_status,
)

FIXTURES = Path("tests/fixtures/behavior_validation")
# The review's known answers belong to the artifact it measured: the set
# published until the 2026-09-23 current-calibration refresh, pinned here as a
# fixture so a promotion cannot silently re-point them (B03.9).
ARTIFACT = load_behavior_models(
    Path(__file__).parent / "fixtures/behavior_validation/behavior_models_2026-09-22.yaml"
)
CRITERIA = load_validation_document(Path("data/nissan-xterra-2008/behavior_validation.yaml"))
CATALOG = {recipe.name: recipe for recipe in default_recipes("nissan-xterra-2008")}

J26 = "2026-07-26T225058"
S17 = "_capture-tuning-hybrid-research-2026-09-17T184821"
S18 = "2026-09-18T134345"
S21 = "_capture-fuel-target-research-2026-09-21T195844"


def _rows(kind: str, *sessions: str) -> pd.DataFrame:
    return pd.concat(
        [pd.read_parquet(FIXTURES / kind / f"{session}.parquet") for session in sessions],
        ignore_index=True,
    )


def _model(name: str) -> BehaviorModel1D | BehaviorModel2D:
    model = ARTIFACT.by_name(name)
    assert model is not None
    return model


def _metrics(name: str, frame: pd.DataFrame):  # type: ignore[no-untyped-def]
    model = _model(name)
    spec = legacy_reproduction_spec(CATALOG[name], model)[0] if model.eligibility is None else None
    # A historical reproduction: the review measured these slices, captures
    # included, before the default-profile rule (B05.3) -- the same opt-out
    # evaluate_behavior_models.py --mode historical takes.
    ev = evaluate_rows(model, frame, spec=spec, profile_scope="every_recording")
    return fold_metrics(model, ev, drive_of_piece={}, policy=EventPolicy()), ev


@pytest.mark.parametrize(
    "name",
    [
        "air_charge_vs_rpm_throttle_did",
        "warm_idle_rpm_baseline",
        "timing_advance_vs_rpm_load",
        "timing_advance_vs_rpm_iat",
        "fuel_trim_vs_rpm_load",
        "air_mass_per_cycle_vs_rpm_load",
        "ve_vs_rpm_load",
    ],
)
def test_known_answer_persisted_holdouts_reproduce_exactly(name: str) -> None:
    """The repo evaluator lands on the artifacts' own persisted held-out numbers."""
    model = _model(name)
    fit = model.fit
    frame = _rows("historical", *fit.calibration_sessions)
    metrics, _ = _metrics(name, frame)
    assert metrics.evaluated == fit.validation_sample_count
    for got, want in (
        (metrics.coverage, fit.validation_coverage),
        (metrics.r_squared, fit.validation_r_squared),
        (metrics.warning_row_rate, fit.validation_warning_rate),
        (metrics.critical_row_rate, fit.validation_critical_rate),
    ):
        assert got == pytest.approx(want, abs=1e-6)


def test_the_july_holdout_is_thin_and_not_independent() -> None:
    """47-79 covered rows on one held-out session — the review's finding."""
    counts = {
        name: _model(name).fit.validation_sample_count
        for name in ("timing_advance_vs_rpm_load", "fuel_trim_vs_rpm_load", "ve_vs_rpm_load")
    }
    assert counts == {
        "timing_advance_vs_rpm_load": 57,
        "fuel_trim_vs_rpm_load": 47,
        "ve_vs_rpm_load": 76,
    }
    # One held-out *session*, not several independent drives.
    assert _model("timing_advance_vs_rpm_load").fit.calibration_sessions == [J26]


def test_legacy_catalyst_evidence_is_absent() -> None:
    model = _model("cat_warmup")
    assert model.fit.validation_sample_count == 0 and not model.fit.calibration_sessions
    assert model.fit.r_squared is not None and model.fit.r_squared > 0.8  # training only
    # Publication sanity may read training R²; trust never does.
    assert _check_publish_gate(CATALOG["cat_warmup"], model) is None
    reasons = serving_status(model).reasons
    assert STATUS_LEGACY_NO_CONTRACT in reasons and STATUS_NO_HELD_OUT_VALIDATION in reasons


def test_off_map_cooler_iat_is_refused_not_graded() -> None:
    """Timing/IAT: cool September intake air sits entirely below the fitted domain."""
    frame = _rows("corrected", S17, S18)
    metrics, ev = _metrics("timing_advance_vs_rpm_iat", frame)
    assert metrics.evaluated == 0
    assert metrics.refusals["off_map"] == metrics.eligible > 0
    iat = ev.y[ev.fresh]
    iat_model = _model("timing_advance_vs_rpm_iat")
    assert isinstance(iat_model, BehaviorModel2D)
    domain_low = iat_model.axes[1].bin_edges[0]
    assert np.nanmax(iat) < domain_low  # 13-22 degC vs a 29 degC floor


def test_broad_band_poor_predictor_fails_its_criteria() -> None:
    frame = _rows("historical", J26)
    metrics, _ = _metrics("timing_advance_vs_rpm_iat", frame)
    assert metrics.r_squared is not None and metrics.r_squared < 0
    assert metrics.mae == pytest.approx(8.80, abs=0.01)
    assert metrics.warning_half_width_median == pytest.approx(31.3, abs=0.1)
    assert CRITERIA is not None
    criteria = CRITERIA.recipes["timing_advance_vs_rpm_iat"]
    verdict, failures = apply_criteria(criteria, metrics, [], qualifying_test_drives=2)
    assert verdict == "failed"
    assert any(f.startswith("warning_half_width_median=31.") for f in failures)


@pytest.mark.parametrize(
    ("name", "session", "n", "r_squared", "mae"),
    [
        ("air_mass_per_cycle_vs_rpm_load", S17, 1040, 0.736, None),
        ("air_mass_per_cycle_vs_rpm_load", S21, 949, 0.756, None),
        ("air_charge_vs_rpm_throttle_did", S21, 1011, 0.781, 2029.5),
        ("timing_advance_vs_rpm_load", S17, 1084, 0.328, 4.80),
        ("timing_advance_vs_rpm_load", S21, 1009, 0.279, 6.71),
        ("timing_advance_vs_rpm_load", S18, 261, 0.714, None),
        ("fuel_trim_vs_rpm_load", S18, 142, -3.948, 6.51),
        ("ve_vs_rpm_load", S18, 114, 0.661, 4.71),
    ],
)
def test_september_measurements_reproduce_the_review(
    name: str, session: str, n: int, r_squared: float, mae: float | None
) -> None:
    metrics, _ = _metrics(name, _rows("corrected", session))
    assert metrics.evaluated == n
    assert metrics.r_squared == pytest.approx(r_squared, abs=0.001)
    if mae is not None:
        assert metrics.mae == pytest.approx(mae, abs=0.01)


def test_useful_constant_idle_baseline_meets_its_accuracy_criteria() -> None:
    """Negative R² is not a reason to reject a constant robust baseline."""
    metrics, _ = _metrics("warm_idle_rpm_baseline", _rows("corrected", S18))
    assert metrics.evaluated == 154
    assert metrics.mae == pytest.approx(13.64, abs=0.01)
    assert metrics.r_squared is not None and metrics.r_squared < 0
    assert metrics.warning_row_rate == 0 and metrics.critical_row_rate == 0
    assert CRITERIA is not None
    criteria = CRITERIA.recipes["warm_idle_rpm_baseline"]
    verdict, failures = apply_criteria(criteria, metrics, [], qualifying_test_drives=1)
    # Accurate enough; what it lacks is independent evidence, and says so.
    assert verdict == "insufficient_evidence"
    assert failures == [
        "test_drives=1<2 (each >= 30 evaluated rows)",
        "control_hours=0.04<0.5 (0 event(s) is not a rate)",
    ]


def test_catalyst_on_a_real_cold_start_is_not_trustworthy() -> None:
    """The legacy catalyst curve against S18's genuine cold start, under the
    catalog recipe's cold-start contract: n=34, R² -0.737, MAE 55 degC, 25
    warnings + 1 critical (76.5 % per-row exceedance — not a false-positive
    rate; the drive is unlabelled)."""
    metrics, _ = _metrics("cat_warmup", _rows("corrected", S18))
    assert metrics.evaluated == 34
    assert metrics.r_squared == pytest.approx(-0.737, abs=0.001)
    assert metrics.mae == pytest.approx(55.0, abs=0.05)
    assert round((metrics.warning_row_rate or 0) * 34) == 25
    assert round((metrics.critical_row_rate or 0) * 34) == 1
