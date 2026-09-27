"""B02 — reproducible behavior validation and publication gates.

The actual failures from the 2026-09-21 review, each pinned: a broad-band
surface that resolves nothing, a model with no held-out evidence, drive
fragments leaking across folds, prefix-sorted chronology, a constant idle
baseline that must still be able to pass, and every publication route
(fresh fit, partial refit, carry-over, load/hot reload) agreeing on trust.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.analysis.behavior_validation import (
    PROFILE_EXCLUSION_COLUMN,
    BehaviorValidationDocument,
    Drive,
    EventPolicy,
    LeakedDriveError,
    UnorderedSessionsError,
    ValidationContext,
    apply_criteria,
    carry_over_flags,
    chronological_split,
    count_events,
    evaluate_rows,
    explicit_split,
    fold_metrics,
    group_drives,
    mode_labels_predate_fix,
    piece_time_ranges,
    training_leaks,
)
from src.analysis.refit import (
    Surface1DRecipe,
    Surface2DRecipe,
    _check_publish_gate,
    attach_criteria,
    default_recipes,
    fit_recipe,
    refit_all,
)
from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN
from src.decode.behavior_models import (
    STATUS_NO_HELD_OUT_VALIDATION,
    Axis1D,
    BehaviorModel1D,
    BehaviorModels,
    DeviationConfig,
    FoldMetrics,
    LookupTableFit,
    NoiseFloor,
    SyntheticFault,
    ValidationCriteria,
    dump_behavior_models,
    load_behavior_models,
    serving_status,
)
from tests.configs import example_config

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _criteria(**overrides: Any) -> ValidationCriteria:
    fields: dict[str, Any] = {
        "kind": "predictive_surface",
        "unit": "deg",
        "noise_floor": NoiseFloor(value=0.5, basis="test LSB"),
        "meaningful_deviation": 5.0,
        "meaningful_deviation_basis": "test",
        "min_validation_drives": 2,
        "min_eligible_observations": 100,
        "min_envelope_coverage": 0.0,
        "min_skill_vs_baseline": 0.3,
        "synthetic_faults": [
            SyntheticFault(magnitude=6.0, direction="negative", duration_seconds=60)
        ],
    }
    fields.update(overrides)
    return ValidationCriteria(**fields)


def _drive(
    name: str, start: datetime, *, n: int = 400, seed: int = 0, signal: str = "timing"
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rpm = rng.uniform(800, 4000, n)
    load = rng.uniform(10, 90, n)
    if signal == "timing":
        # A real relationship: timing tracks rpm and load, small noise.
        output = 10.0 + 0.004 * rpm - 0.1 * load + rng.normal(0.0, 0.4, n)
    else:
        # No relationship at all: broad noise independent of both axes.
        output = rng.normal(20.0, 8.0, n)
    return pd.DataFrame(
        {
            TIMESTAMP_COLUMN: pd.date_range(start, periods=n, freq="1s", tz="UTC"),
            SESSION_COLUMN: name,
            MODE_COLUMN: "city",
            "obd:RPM": rpm,
            "obd:ENGINE_LOAD": load,
            "obd:TIMING_ADVANCE": output,
        }
    )


def _corpus(names: list[str], **kwargs: Any) -> pd.DataFrame:
    return pd.concat(
        [
            _drive(name, T0 + timedelta(days=day), seed=day, **kwargs)
            for day, name in enumerate(names)
        ],
        ignore_index=True,
    )


def _timing_recipe(**overrides: Any) -> Surface2DRecipe:
    fields: dict[str, Any] = {
        "name": "timing_test",
        "output": "obd:TIMING_ADVANCE",
        "x_column": "obd:RPM",
        "y_column": "obd:ENGINE_LOAD",
        "x_edges": (800.0, 1600.0, 2400.0, 3200.0, 4000.0),
        "y_edges": (10.0, 30.0, 50.0, 70.0, 90.0),
        "min_samples": 200,
        "min_cell_count": 5,
        "min_rpm": 400.0,
        "max_validation_warning_rate": 1.0,
        "max_validation_critical_rate": 1.0,
        "deviation": DeviationConfig(
            kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=0.5
        ),
        "validation": _criteria(),
    }
    fields.update(overrides)
    return Surface2DRecipe(**fields)


# ---------------------------------------------------------------------------
# B02.3 — grouped, chronological splits
# ---------------------------------------------------------------------------


def test_prefix_sorted_chronology_is_refused() -> None:
    """String order puts ``2026-…`` before ``_capture-…``; parsed time does not."""
    names = [
        "2026-08-01T120000",
        "_capture-zzz-2026-08-02T120000",
        "2026-08-03T120000",
        "_capture-aaa-2026-08-04T120000",
    ]
    assert sorted(names)[-1] == "_capture-zzz-2026-08-02T120000"  # the old holdout
    frame = pd.concat(
        [_drive(name, T0 + timedelta(days=day), seed=day) for day, name in enumerate(names)],
        ignore_index=True,
    )
    # The captures here are verified default-profile evidence (B05.3 stamp).
    frame[PROFILE_EXCLUSION_COLUMN] = None
    result = fit_recipe(_timing_recipe(), frame)
    model = result.model or result.candidate
    assert model is not None, result.reason
    assert model.fit.calibration_sessions == [
        "2026-08-03T120000",
        "_capture-aaa-2026-08-04T120000",
    ]
    assert model.fit.sessions == ["2026-08-01T120000", "_capture-zzz-2026-08-02T120000"]


def test_drive_fragments_never_cross_folds() -> None:
    """``#segment`` pieces and a capture rotated into mid-drive are one drive."""
    start = T0
    ranges = {
        "2026-08-01T120000#segment-001": (start, start + timedelta(minutes=20)),
        "2026-08-01T120000#segment-002": (
            start + timedelta(minutes=50),
            start + timedelta(minutes=60),
        ),
        # Capture armed 2 s after the plain session's last row.
        "_capture-fuel-2026-08-01T130002": (
            start + timedelta(minutes=60, seconds=2),
            start + timedelta(minutes=80),
        ),
        "2026-08-02T090000": (start + timedelta(hours=21), start + timedelta(hours=22)),
    }
    drives = group_drives(ranges, join_gap_seconds=600)
    assert [d.pieces for d in drives] == [
        (
            "2026-08-01T120000#segment-001",
            "2026-08-01T120000#segment-002",
            "_capture-fuel-2026-08-01T130002",
        ),
        ("2026-08-02T090000",),
    ]
    split = chronological_split(drives, n_test=1)
    assert split.pieces("test") == {"2026-08-02T090000"}
    assert len(split.pieces("train")) == 3
    # An explicit assignment that splits one drive is refused, naming it.
    with pytest.raises(LeakedDriveError, match="may not cross folds"):
        explicit_split(
            drives,
            {
                "2026-08-01T120000#segment-001": "train",
                "_capture-fuel-2026-08-01T130002": "test",
            },
        )
    # A model trained on one fragment cannot be evaluated on its drive.
    assert training_leaks(drives, ["2026-08-01T120000#segment-002"]) == {
        "2026-08-01T120000": [
            "2026-08-01T120000#segment-001",
            "2026-08-01T120000#segment-002",
        ]
    }


def test_fit_recipe_keeps_segments_of_one_drive_in_one_fold() -> None:
    frames = []
    for day in range(3):
        drive = _drive(f"2026-08-0{day + 1}T120000", T0 + timedelta(days=day), seed=day)
        if day == 2:
            half = len(drive) // 2
            drive.loc[: half - 1, SESSION_COLUMN] = "2026-08-03T120000#segment-001"
            drive.loc[half:, SESSION_COLUMN] = "2026-08-03T120000#segment-002"
        frames.append(drive)
    recipe = _timing_recipe(validation=_criteria(min_validation_drives=1))
    result = fit_recipe(recipe, pd.concat(frames, ignore_index=True))
    model = result.model or result.candidate
    assert model is not None, result.reason
    assert model.fit.calibration_sessions == [
        "2026-08-03T120000#segment-001",
        "2026-08-03T120000#segment-002",
    ]
    assert not any("2026-08-03" in s for s in model.fit.sessions)


def test_unordered_sessions_refuse_a_split() -> None:
    frame = _corpus(["a", "b", "c"]).drop(columns=[TIMESTAMP_COLUMN])
    result = fit_recipe(_timing_recipe(), frame)
    assert result.skipped
    assert "cannot order sessions" in result.reason
    with pytest.raises(UnorderedSessionsError):
        group_drives(piece_time_ranges(frame))


def test_prior_edges_fitted_on_a_test_drive_are_refused() -> None:
    names = ["2026-08-01T120000", "2026-08-02T120000", "2026-08-03T120000", "2026-08-04T120000"]
    frame = _corpus(names)
    auto = _timing_recipe(x_edges=None, y_edges=None, auto_bin_count=3)
    prior = fit_recipe(auto, frame)
    prior_model = prior.model or prior.candidate
    assert prior_model is not None
    # The prior trained on every drive; its edges now include today's test drives.
    widened = prior_model.model_copy(
        update={"fit": prior_model.fit.model_copy(update={"sessions": names})}
    )
    result = fit_recipe(auto, frame, prior_model=widened)
    assert result.skipped and "re-grid" in result.reason
    regridded = fit_recipe(auto, frame, prior_model=widened, regrid=True)
    assert (regridded.model or regridded.candidate) is not None


# ---------------------------------------------------------------------------
# B02.5 / B02.6 — denominators, skill, sensitivity
# ---------------------------------------------------------------------------


def test_broad_band_useless_surface_is_refused() -> None:
    names = [f"2026-08-0{d + 1}T120000" for d in range(5)]
    result = fit_recipe(_timing_recipe(), _corpus(names, signal="noise"))
    model = result.model or result.candidate
    assert model is not None and model.evidence is not None
    evidence = model.evidence
    assert evidence.verdict == "failed"
    failed = " ".join(evidence.failures)
    assert "skill_vs_baseline" in failed
    assert "warning_half_width_median" in failed  # broad bands resolve nothing
    status = serving_status(model)
    assert not status.alerting
    assert any(reason.startswith("validation_failed:") for reason in status.reasons)
    # Numbers are in physical units with drive-level uncertainty.
    test = evidence.test
    assert test is not None and test.drives == 2 and test.mae is not None and test.mae > 5
    assert test.warning_half_width_median is not None and test.warning_half_width_median > 5.0


def test_useful_surface_passes_and_alerts() -> None:
    names = [f"2026-08-0{d + 1}T120000" for d in range(5)]
    result = fit_recipe(_timing_recipe(), _corpus(names))
    assert result.model is not None, result.reason
    evidence = result.model.evidence
    assert evidence is not None and evidence.verdict == "passed", evidence and evidence.failures
    assert evidence.test is not None and evidence.test.skill_vs_baseline is not None
    assert evidence.test.skill_vs_baseline > 0.3
    assert [case.synthetic for case in evidence.sensitivity] == [True]
    assert evidence.sensitivity[0].detection_fraction == pytest.approx(1.0)
    assert set(evidence.per_drive) == {d.drive_id for d in _drives_of(evidence.split.test)}
    assert serving_status(result.model).alerting


def _drives_of(records: Any) -> list[Any]:
    return list(records)


def test_constant_idle_baseline_can_pass_its_own_criteria() -> None:
    frames = []
    for day in range(4):
        rng = np.random.default_rng(day)
        n = 300
        frames.append(
            pd.DataFrame(
                {
                    TIMESTAMP_COLUMN: pd.date_range(
                        T0 + timedelta(days=day), periods=n, freq="1s", tz="UTC"
                    ),
                    SESSION_COLUMN: f"2026-08-0{day + 1}T120000",
                    MODE_COLUMN: "idle",
                    "obd:RPM": 762.5 + rng.normal(0.0, 10.0, n),
                }
            )
        )
    recipe = Surface1DRecipe(
        name="warm_idle_rpm_baseline",
        output="obd:RPM",
        x_column="obd:RPM",
        operating_mode="idle",
        min_rpm=400.0,
        fit_method="robust_baseline",
        min_r_squared=None,
        min_samples=200,
        min_sessions=3,
        deviation=DeviationConfig(
            kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=25.0
        ),
        validation=_criteria(
            kind="constant_baseline",
            unit="rpm",
            noise_floor=NoiseFloor(value=25.0, basis="accessory steps"),
            meaningful_deviation=100.0,
            min_skill_vs_baseline=None,
            max_mae=40.0,
            max_abs_error_p95=75.0,
            min_eligible_observations=150,
            synthetic_faults=[SyntheticFault(magnitude=150.0, duration_seconds=60)],
        ),
    )
    result = fit_recipe(recipe, pd.concat(frames, ignore_index=True))
    assert result.model is not None, result.reason
    evidence = result.model.evidence
    assert evidence is not None and evidence.verdict == "passed", evidence and evidence.failures
    assert evidence.test is not None
    # Negative/undefined R² is not a reason to refuse a constant baseline.
    assert evidence.test.r_squared is None or evidence.test.r_squared < 0.1
    assert serving_status(result.model).alerting


def test_no_prediction_is_not_a_valid_observation() -> None:
    """B01's distinction survives into every denominator."""
    names = [f"2026-08-0{d + 1}T120000" for d in range(3)]
    result = fit_recipe(
        _timing_recipe(validation=_criteria(min_validation_drives=1)), _corpus(names)
    )
    model = result.model or result.candidate
    assert model is not None
    probe = _drive("2026-08-09T120000", T0 + timedelta(days=9), n=60, seed=9)
    probe.loc[:9, "obd:RPM"] = 9000.0  # off-map
    probe.loc[10:19, "obd:RPM"] = 0.0  # engine not running
    probe.loc[20:29, "obd:TIMING_ADVANCE"] = np.nan  # actual missing
    ev = evaluate_rows(model, probe)
    metrics = fold_metrics(model, ev, drive_of_piece={}, policy=EventPolicy())
    assert metrics.rows == 60
    assert metrics.evaluated == 30
    assert metrics.eligible == 40  # off-map rows passed the contract
    assert metrics.coverage == pytest.approx(0.75)
    assert metrics.refusals["off_map"] == 10
    assert metrics.refusals["engine_not_running"] == 10
    assert metrics.refusals["actual_missing:obd:TIMING_ADVANCE"] == 10
    assert metrics.input_present == 50


def test_evaluated_hours_is_wall_clock_exposure_not_a_row_count() -> None:
    """B02.5: an event rate's denominator is the time the detector was watching.

    A slowly polled output is evaluated in short bursts far apart (the
    catalyst PID on its 1/15 Hz tier: 1 s pairs ~17 s apart); counting rows
    would shrink the exposure and inflate events/h.
    """
    names = [f"2026-08-0{d + 1}T120000" for d in range(3)]
    result = fit_recipe(
        _timing_recipe(validation=_criteria(min_validation_drives=1)), _corpus(names)
    )
    model = result.model or result.candidate
    assert model is not None
    probe = _drive("2026-08-09T120000", T0 + timedelta(days=9), n=60, seed=9)
    dense = fold_metrics(
        model, evaluate_rows(model, probe), drive_of_piece={}, policy=EventPolicy()
    )
    assert dense.evaluated == 60
    assert dense.evaluated_hours == pytest.approx(60 / 3600)  # contiguous 1 Hz: unchanged
    sparse = probe.iloc[[0, 1, 18, 19, 36, 37]].reset_index(drop=True)
    metrics = fold_metrics(
        model, evaluate_rows(model, sparse), drive_of_piece={}, policy=EventPolicy()
    )
    assert metrics.evaluated == 6
    assert metrics.evaluated_hours == pytest.approx(38 / 3600)  # first-to-last + one tick, not 6 s
    # Two drives sum their own spans; a lone evaluated row counts one tick.
    two = pd.concat(
        [sparse, _drive("2026-08-10T120000", T0 + timedelta(days=10), n=1, seed=10)],
        ignore_index=True,
    )
    metrics = fold_metrics(
        model, evaluate_rows(model, two), drive_of_piece={}, policy=EventPolicy()
    )
    assert metrics.drives == 2
    assert metrics.evaluated_hours == pytest.approx(39 / 3600)


def test_legacy_artifact_is_reproduced_with_its_own_gates_not_a_later_recipe_gate() -> None:
    """B07.6 gave warm idle a steady-rpm gate on 2026-09-23. The artifact
    fitted before it was fitted and validated without that gate, so its
    reproduction keeps the artifact's own (empty) gates and says so;
    re-applying the new gate refused every committed held-out row."""
    from src.analysis.refit import legacy_reproduction_spec

    recipe = {r.name: r for r in default_recipes("nissan-xterra-2008")}["warm_idle_rpm_baseline"]
    assert [c.channel for c in recipe.rate_conditions()] == ["obd:RPM__roc"]
    legacy = load_behavior_models(
        Path(__file__).parent / "fixtures/behavior_validation/behavior_models_2026-09-22.yaml"
    ).by_name("warm_idle_rpm_baseline")
    assert legacy is not None and legacy.eligibility is None and legacy.conditions == []
    spec, note = legacy_reproduction_spec(recipe, legacy)
    assert spec.conditions == ()
    assert spec.eligibility is not None and spec.eligibility.local_ranges == []
    assert note is not None and "own fitted gates" in note and "local ranges" in note
    # The recipe's other row gates (mode, engine state) still select the rows.
    assert spec.operating_mode == "idle"


def test_parked_hours_inside_one_drive_are_not_exposure() -> None:
    """EVALUATOR_VERSION 3: a gap longer than the drive join gap ends a watch.

    2026-09-23T032755 kept recording a parked truck for 11 h, so
    ``group_drives`` joined the evening drive before it and the morning drive
    after it into one group, and the first-to-last span counted the parked
    hours as time the detector watched: 6 warm-idle events over "17.4 h".
    """
    names = [f"2026-08-0{d + 1}T120000" for d in range(3)]
    result = fit_recipe(
        _timing_recipe(validation=_criteria(min_validation_drives=1)), _corpus(names)
    )
    model = result.model or result.candidate
    assert model is not None
    evening = _drive("2026-08-09T120000", T0 + timedelta(days=9), n=60, seed=9)
    morning = _drive("2026-08-09T230000", T0 + timedelta(days=9, hours=11), n=60, seed=10)
    one_group = pd.concat([evening, morning], ignore_index=True)
    joined = {"2026-08-09T120000": "drive", "2026-08-09T230000": "drive"}
    policy = EventPolicy()
    assert policy.exposure_break_seconds == 600.0  # the profile's drive_join_gap_seconds
    metrics = fold_metrics(
        model, evaluate_rows(model, one_group), drive_of_piece=joined, policy=policy
    )
    assert metrics.drives == 1
    assert metrics.evaluated_hours == pytest.approx(120 / 3600)  # two watches, not 11 h
    # Gaps inside the join gap are still watched time (sparse polling).
    sparse = evening.iloc[[0, 1, 18, 19, 36, 37]].reset_index(drop=True)
    metrics = fold_metrics(model, evaluate_rows(model, sparse), drive_of_piece={}, policy=policy)
    assert metrics.evaluated_hours == pytest.approx(38 / 3600)
    # The break is the profile's join gap, carried by the policy.
    document = BehaviorValidationDocument.model_validate(
        {"approved_operating_modes": ["idle"], "drive_join_gap_seconds": 60000.0}
    )

    config = example_config(Path("canary.example.yaml"))
    wide = EventPolicy.from_config(config, document)
    assert wide.exposure_break_seconds == 60000.0
    metrics = fold_metrics(
        model, evaluate_rows(model, one_group), drive_of_piece=joined, policy=wide
    )
    assert metrics.evaluated_hours == pytest.approx((11 * 3600 + 60) / 3600)


def test_row_exceedance_is_not_an_event() -> None:
    policy = EventPolicy(required=3, window=5, window_seconds=8.0, dedupe_seconds=30.0)
    seconds = np.arange(120, dtype=float)
    groups = np.zeros(120)
    isolated = np.full(120, False, dtype=object)
    isolated[::10] = True  # 12 single-tick exceedances
    assert count_events(isolated, seconds, groups, policy) == 0
    sustained = np.full(120, False, dtype=object)
    sustained[10:80] = True  # one long deviation
    assert count_events(sustained, seconds, groups, policy) == 3  # dedupe every 30 s
    refused = sustained.copy()
    refused[11:13] = None  # missing verdicts freeze, never count as misses
    assert count_events(refused, seconds, groups, policy) == 3


def test_apply_criteria_names_missing_evidence() -> None:
    criteria = _criteria(min_validation_drives=3, min_eligible_observations=500)
    metrics = FoldMetrics(
        drives=1,
        rows=200,
        input_present=200,
        fresh=200,
        eligible=150,
        evaluated=140,
        coverage=140 / 150,
        mae=1.0,
        warning_half_width_median=2.0,
        skill_vs_baseline=0.6,
    )
    verdict, failures = apply_criteria(criteria, metrics, [], qualifying_test_drives=1)
    assert verdict == "insufficient_evidence"
    assert failures == [
        "test_drives=1<3 (each >= 30 evaluated rows)",
        "eligible=150<500",
    ]
    assert apply_criteria(None, metrics, [], qualifying_test_drives=1) == (
        "insufficient_evidence",
        ["no_declared_criteria"],
    )


def test_criteria_are_profile_data_and_validated() -> None:
    doc = BehaviorValidationDocument.model_validate(
        {
            "approved_operating_modes": ["idle", "city", "highway"],
            "retired_operating_modes": {"suburban": "city"},
            "recipes": {"timing_test": _criteria().model_dump()},
        }
    )
    assert doc.taxonomy_problems(["idle", "city", "suburban", "highway"]) == [
        "retired_mode_present:suburban->city"
    ]
    attached = attach_criteria([_timing_recipe(validation=None)], doc)
    assert attached[0].validation == doc.recipes["timing_test"]
    with pytest.raises(ValueError, match="noise floor"):
        _criteria(meaningful_deviation=0.4)
    with pytest.raises(ValueError, match="min_skill_vs_baseline"):
        _criteria(min_skill_vs_baseline=None)
    with pytest.raises(ValueError, match="synthetic fault"):
        _criteria(synthetic_faults=[])


def test_shipped_profile_criteria_load_and_cover_the_shipped_models() -> None:
    from src.analysis.behavior_validation import load_validation_document

    doc = load_validation_document(Path("data/nissan-xterra-2008/behavior_validation.yaml"))
    assert doc is not None
    shipped = load_behavior_models(Path("data/nissan-xterra-2008/behavior_models.yaml"))
    assert {m.name for m in shipped.models} <= set(doc.recipes)
    assert set(doc.recipes) <= {r.name for r in default_recipes("nissan-xterra-2008")}
    assert "suburban" in doc.retired_operating_modes


def test_unapproved_taxonomy_cannot_pass() -> None:
    names = [f"2026-08-0{d + 1}T120000" for d in range(5)]
    context = ValidationContext(taxonomy_problems=("retired_mode_present:suburban->city",))
    result = fit_recipe(_timing_recipe(), _corpus(names), validation_context=context)
    model = result.model or result.candidate
    assert model is not None and model.evidence is not None
    assert model.evidence.verdict == "insufficient_evidence"
    assert "mode_taxonomy:retired_mode_present:suburban->city" in model.evidence.failures
    assert not serving_status(model).alerting


def test_mode_label_sidecars_before_the_fix_are_stale() -> None:
    assert mode_labels_predate_fix("2026-09-18T12:00:00Z")
    assert not mode_labels_predate_fix("2026-09-21T12:00:00Z")
    assert mode_labels_predate_fix("not a time")


# ---------------------------------------------------------------------------
# B02.7 — every publication route agrees
# ---------------------------------------------------------------------------


def _legacy_catalyst() -> BehaviorModel1D:
    """The cat_warmup shape: training R² 0.87, zero held-out rows."""
    return BehaviorModel1D(
        name="cat_warmup",
        kind="surface_1d",
        output="obd:CATALYST_TEMP_B1S1",
        axis=Axis1D(name="t", channel="obd:RUN_TIME", domain=(0.0, 600.0)),
        operating_mode="warmup",
        fit=LookupTableFit(
            kind="lookup_table",
            x_knots=[0.0, 600.0],
            y_knots=[20.0, 500.0],
            sample_count=1027,
            r_squared=0.8729,
            sessions=[f"2026-05-18T0{hour}0000" for hour in range(1, 8)],
            fitted_at="2026-05-25T15:17:44+00:00",
        ),
        deviation=DeviationConfig(kind="absolute", warning_band=30.0, critical_band=80.0),
    )


def test_missing_holdout_never_falls_back_to_training_r_squared() -> None:
    legacy = _legacy_catalyst()
    recipe = next(r for r in default_recipes("nissan-xterra-2008") if r.name == "cat_warmup")
    # Publication sanity may read training R² ...
    assert _check_publish_gate(recipe, legacy) is None
    # ... but alerting never does.
    assert STATUS_NO_HELD_OUT_VALIDATION in serving_status(legacy).reasons
    # Carry-over keeps it on disk and still untrusted.
    models, _ = refit_all(
        [],
        pd.DataFrame(),
        existing=BehaviorModels(schema_version=1, models=[legacy]),
        catalog=[recipe],
    )
    assert models.models and not serving_status(models.models[0]).alerting
    # A fresh recipe with criteria but a single drive publishes no evidence.
    one_drive = _corpus(["2026-08-01T120000"])
    single = fit_recipe(_timing_recipe(), one_drive)
    assert single.model is not None and single.model.evidence is None
    assert serving_status(single.model).reasons == (STATUS_NO_HELD_OUT_VALIDATION,)


def _passed_model() -> tuple[Surface2DRecipe, Any, pd.DataFrame]:
    names = [f"2026-08-0{d + 1}T120000" for d in range(5)]
    frame = _corpus(names)
    recipe = _timing_recipe()
    context = ValidationContext(
        decoder_identity={"bindings": "sha256:a", "dids": "sha256:b"},
        mode_taxonomy=("idle", "city"),
    )
    result = fit_recipe(recipe, frame, validation_context=context)
    assert result.model is not None and serving_status(result.model).alerting
    return recipe, result.model, frame


def test_partial_refit_carry_over_flags_changed_decoder_contract_and_criteria() -> None:
    recipe, model, _frame = _passed_model()
    existing = BehaviorModels(schema_version=2, models=[model])
    other = Surface1DRecipe(name="other", output="y", x_column="x", min_samples=10_000)
    same = ValidationContext(
        decoder_identity={"bindings": "sha256:a", "dids": "sha256:b"},
        mode_taxonomy=("idle", "city"),
    )
    kept, _ = refit_all(
        [other],
        pd.DataFrame({"x": [1.0], "y": [1.0]}),
        existing=existing,
        catalog=[recipe, other],
        validation_context=same,
    )
    assert serving_status(kept.models[0]).alerting
    assert kept.models[0].evidence == model.evidence

    changed = ValidationContext(
        decoder_identity={"bindings": "sha256:CHANGED", "dids": "sha256:b"},
        mode_taxonomy=("idle", "city", "suburban"),
    )
    flagged, _ = refit_all(
        [other],
        pd.DataFrame({"x": [1.0], "y": [1.0]}),
        existing=existing,
        catalog=[recipe, other],
        validation_context=changed,
    )
    prior = flagged.models[0]
    assert prior.publication_flags == ["decoder_changed:bindings", "context_changed:mode_taxonomy"]
    assert prior.evidence == model.evidence  # historical evidence preserved, not rewritten
    assert not serving_status(prior).alerting

    stricter = _timing_recipe(
        closed_loop_only=True, validation=_criteria(min_skill_vs_baseline=0.5)
    )
    assert carry_over_flags(
        model,
        recipe_contract=__import__(
            "src.analysis.refit", fromlist=["x"]
        ).recipe_contract_fingerprint(stricter),
        criteria=stricter.validation,
        context=same,
    ) == ["contract_changed:recipe", "criteria_changed"]


def test_identical_rows_with_a_changed_decoder_bump_the_version() -> None:
    recipe, model, frame = _passed_model()
    changed = ValidationContext(
        decoder_identity={"bindings": "sha256:CHANGED", "dids": "sha256:b"},
        mode_taxonomy=("idle", "city"),
    )
    refit = fit_recipe(
        recipe,
        frame,
        prior_model=model,
        next_version_for={recipe.name: 2},
        validation_context=changed,
    )
    assert refit.model is not None
    assert refit.model.fit.training_data_hash == model.fit.training_data_hash
    assert refit.model.version == 2


def test_load_and_hot_reload_agree_with_the_fit(tmp_path: Path) -> None:
    from src.daemon import poll_behavior_models
    from src.preprocessing.live import LiveFeatureSource

    _recipe, model, _frame = _passed_model()
    path = tmp_path / "behavior_models.yaml"
    dump_behavior_models(BehaviorModels(schema_version=2, models=[model]), path)
    loaded = load_behavior_models(path)
    assert loaded.models[0] == model
    assert serving_status(loaded.models[0]).alerting

    config = example_config(Path("canary.example.yaml"))
    source = LiveFeatureSource(config=config, behavior_models=loaded)
    flagged = model.model_copy(update={"publication_flags": ["decoder_changed:bindings"]})

    async def _reload() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(poll_behavior_models(path, source, stop, interval_seconds=0.05))
        try:
            await asyncio.sleep(0.1)
            dump_behavior_models(BehaviorModels(schema_version=2, models=[flagged]), path)
            while (
                source.behavior_models is None
                or not source.behavior_models.models[0].publication_flags
            ):
                await asyncio.sleep(0.05)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(asyncio.wait_for(_reload(), timeout=5.0))
    reloaded = source.behavior_models
    assert reloaded is not None
    assert serving_status(reloaded.models[0]).reasons == ("decoder_changed:bindings",)


def test_drive_split_records_every_fold() -> None:
    drives = [
        Drive(
            f"d{i}",
            (f"2026-08-0{i + 1}T120000",),
            T0 + timedelta(days=i),
            T0 + timedelta(days=i, hours=1),
        )
        for i in range(5)
    ]
    split = chronological_split(drives, n_test=2, n_calibration=1)
    record = split.record()
    assert [d.drive_id for d in record.train] == ["d0", "d1"]
    assert [d.drive_id for d in record.calibration] == ["d2"]
    assert [d.drive_id for d in record.test] == ["d3", "d4"]


def test_first_runtime_answer_after_the_first_running_rows_places_the_start() -> None:
    """RPM and RUN_TIME are separate polls; the 2026-09-18 cold start had five
    running rows before the first RUN_TIME (20 s). That reading is evidence."""
    from src.preprocessing.session_context import EngineStartTracker, StartContextSpec

    spec = StartContextSpec("obd:RPM", "obd:RUN_TIME", "obd:COOLANT_TEMP", 400.0, 60.0)
    tracker = EngineStartTracker(spec)
    assert tracker.update(1550.0, None, None, 0.0) == (
        0.0,
        pytest.approx(float("nan"), nan_ok=True),
    )
    assert tracker.update(1550.0, 20.0, 16.0, 5.0) == (1.0, 16.0)
    # A mid-drive join still cannot manufacture a start.
    late = EngineStartTracker(spec)
    late.update(1550.0, None, None, 0.0)
    assert late.update(1550.0, 600.0, 85.0, 5.0)[0] == 0.0
