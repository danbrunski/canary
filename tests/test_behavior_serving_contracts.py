"""B01 — behavior fit/serve contracts, end to end.

Covers the failure the 2026-09-21 behavior review reproduced (a steady-state
gated model that could never evaluate through the normal pipeline because
behavior evaluation ran before the ``__roc`` columns it requires existed),
the shared eligibility evaluator (engine state, explicit fuel-status
membership, cold-start session context, rate gates), legacy-artifact
migration, and fit → serialize → load parity across the batch, live and
replay paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.analysis.refit import Surface1DRecipe, fit_recipe, refit_all
from src.config import CanaryConfig
from src.daemon import _observe_behavior
from src.daemon.inference import BehaviorDetector, _BehaviorResidual
from src.dashboard.behavior_live import BehaviorLiveState
from src.decode.behavior_decoder import deviation, explain_expected_column
from src.decode.behavior_eligibility import point_eligibility, required_channel_groups
from src.decode.behavior_models import (
    CURRENT_SCHEMA_VERSION,
    STATUS_LEGACY_NO_CONTRACT,
    STATUS_NO_HELD_OUT_VALIDATION,
    Axis1D,
    BehaviorModel1D,
    BehaviorModels,
    ColdStartRequirement,
    DeviationConfig,
    EngineStateRequirement,
    FuelControlRequirement,
    PolynomialFit,
    RateOfChangeCondition,
    ServingEligibility,
    dump_behavior_models,
    load_behavior_models,
    serving_status,
)
from src.preprocessing.feature_pipeline import featurize_wide_frame
from src.preprocessing.live import LiveFeatureSource, build_prepared_live_tick
from src.preprocessing.resampler import SESSION_COLUMN
from src.preprocessing.session_context import (
    EngineStartTracker,
    StartContextSpec,
    append_start_context,
    start_context_columns,
)
from src.telemetry import TelemetryRecord

T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def config() -> CanaryConfig:
    return CanaryConfig.from_yaml(Path("canary.example.yaml"))


def _steady_model(**overrides: Any) -> BehaviorModel1D:
    """Timing expected at 10 deg whenever RPM and speed are steady."""
    fields: dict[str, Any] = {
        "name": "steady_timing",
        "kind": "surface_1d",
        "output": "obd:TIMING_ADVANCE",
        "axis": Axis1D(name="rpm", channel="obd:RPM", domain=(400.0, 7000.0)),
        "conditions": [
            RateOfChangeCondition(channel="obd:RPM__roc", max_abs_per_second=20.0),
            RateOfChangeCondition(channel="obd:SPEED__roc", max_abs_per_second=2.0),
        ],
        "eligibility": ServingEligibility(
            engine_state=EngineStateRequirement(state="engine_running")
        ),
        "fit": PolynomialFit(
            kind="poly", coefficients=[10.0], sample_count=100, fitted_at="2026-09-21T00:00:00Z"
        ),
        "deviation": DeviationConfig(kind="absolute", warning_band=5.0, critical_band=15.0),
    }
    fields.update(overrides)
    return BehaviorModel1D(**fields)


def _live_tick(source: LiveFeatureSource, now: datetime, values: dict[str, float]) -> None:
    for channel, value in values.items():
        source.observe(TelemetryRecord.obd(now.timestamp(), channel, value, ""))
    prepared = source.prepare_tick(now)
    assert prepared is not None
    source.apply_built_tick(prepared, build_prepared_live_tick(prepared))


def _live_result(source: LiveFeatureSource) -> Any:
    point = source.latest_operating_point
    assert point is not None
    model = source.behavior_models.models[0]  # type: ignore[union-attr]
    return deviation(model, point.get("obd:TIMING_ADVANCE"), point)


# ---------------------------------------------------------------------------
# B01.1 — the complete failure path, through the real transform
# ---------------------------------------------------------------------------


def _constant_rpm_wide(seconds: int = 40) -> pd.DataFrame:
    stamps = pd.date_range(T0, periods=seconds, freq="1s", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": stamps,
            SESSION_COLUMN: "b01-constant-rpm",
            "obd:RPM": 1500.0,
            "obd:SPEED": 40.0,
            "obd:TIMING_ADVANCE": 22.0,
            "obd:RPM__gap": False,
            "obd:SPEED__gap": False,
            "obd:TIMING_ADVANCE__gap": False,
        }
    )


def test_b01_1_batch_deviation_exists_after_required_history(config: CanaryConfig) -> None:
    models = BehaviorModels(schema_version=CURRENT_SCHEMA_VERSION, models=[_steady_model()])
    result = featurize_wide_frame(_constant_rpm_wide(), config, behavior_models=models)

    frame = result.operating_frame
    # The derivative the gate needs exists *before* behavior evaluation.
    assert "obd:RPM__roc" in frame.columns
    deviations = frame["deviation:steady_timing"].to_numpy()
    assert np.isnan(deviations[:30]).all()  # rolling horizon not yet filled
    assert deviations[30:] == pytest.approx(12.0)  # 22 - 10
    assert not frame["deviation:steady_timing__gap"].iloc[-1]
    # The rolling features downstream see the same deviation column.
    assert result.features["deviation:steady_timing"].iloc[-1] == pytest.approx(12.0)

    _, reasons = explain_expected_column(frame, models.models[0])
    assert reasons[0] == "rate_insufficient_history:obd:RPM"
    assert reasons[-1] is None


def test_b01_1_live_observation_exists_after_required_history(config: CanaryConfig) -> None:
    models = BehaviorModels(schema_version=CURRENT_SCHEMA_VERSION, models=[_steady_model()])
    source = LiveFeatureSource(config=config, behavior_models=models)
    values = {"RPM": 1500.0, "SPEED": 40.0, "TIMING_ADVANCE": 22.0}

    _live_tick(source, T0, values)
    first = _live_result(source)
    assert first.expected is None
    assert first.reason == "rate_insufficient_history:obd:RPM"

    for second in range(1, 40):
        _live_tick(source, T0 + timedelta(seconds=second), values)
    result = _live_result(source)
    assert result.reason is None
    assert result.expected == pytest.approx(10.0)
    assert result.deviation == pytest.approx(12.0)
    assert result.severity == "warning"

    # And the daemon's live overlay sees it too (not only the decoder).
    state = BehaviorLiveState()
    residuals = _observe_behavior(source, state)
    snapshot = state.snapshot(include_series=False)["models"]["steady_timing"]
    assert snapshot["expected"] == pytest.approx(10.0)
    assert snapshot["reason"] is None
    assert residuals[0].reason is None


def test_b01_1_stale_dependency_and_excess_rate_refuse(config: CanaryConfig) -> None:
    models = BehaviorModels(schema_version=CURRENT_SCHEMA_VERSION, models=[_steady_model()])
    source = LiveFeatureSource(config=config, behavior_models=models)
    for second in range(40):
        _live_tick(
            source,
            T0 + timedelta(seconds=second),
            {"RPM": 1500.0, "SPEED": 40.0, "TIMING_ADVANCE": 22.0},
        )
    assert _live_result(source).reason is None

    # SPEED stops arriving: past the fast-channel max_gap it goes stale.
    for second in range(40, 45):
        _live_tick(source, T0 + timedelta(seconds=second), {"RPM": 1500.0, "TIMING_ADVANCE": 22.0})
    stale = _live_result(source)
    assert stale.expected is None
    assert stale.reason == "rate_stale:obd:SPEED"

    # Speed returns, then RPM ramps at 100 rpm/s: over a 30 s horizon the
    # rate clears the 20 rpm/s gate.
    for second in range(45, 80):
        _live_tick(
            source,
            T0 + timedelta(seconds=second),
            {"RPM": 1500.0, "SPEED": 40.0, "TIMING_ADVANCE": 22.0},
        )
    assert _live_result(source).reason is None
    for step, second in enumerate(range(80, 90), start=1):
        _live_tick(
            source,
            T0 + timedelta(seconds=second),
            {"RPM": 1500.0 + 100.0 * step, "SPEED": 40.0, "TIMING_ADVANCE": 22.0},
        )
    assert _live_result(source).reason == "rate_exceeded:obd:RPM"


# ---------------------------------------------------------------------------
# B01.3/B01.4 — one evaluator: engine state, fuel membership, context
# ---------------------------------------------------------------------------


def _fuel_model() -> BehaviorModel1D:
    return _steady_model(
        conditions=[],
        eligibility=ServingEligibility(
            engine_state=EngineStateRequirement(state="engine_running"),
            fuel_control=FuelControlRequirement(allowed_states=[0x02]),
        ),
    )


@pytest.mark.parametrize(
    ("point", "reason"),
    [
        ({"obd:FUEL_STATUS": 2.0}, None),
        ({"obd:FUEL_STATUS": 4.0}, "fuel_status_excluded:open_loop_load_or_fuel_cut"),
        ({"obd:FUEL_STATUS": 1.0}, "fuel_status_excluded:open_loop_insufficient_temperature"),
        # Closed loop *with a feedback fault* is not membership in closed loop.
        ({"obd:FUEL_STATUS": 16.0}, "fuel_status_excluded:closed_loop_feedback_fault"),
        ({"obd:FUEL_STATUS": 3.0}, "fuel_status_excluded:invalid(3)"),
        ({}, "fuel_status_missing:obd:FUEL_STATUS"),
        (
            {"obd:FUEL_STATUS": float("nan"), "obd:FUEL_STATUS__gap": True},
            "fuel_status_stale:obd:FUEL_STATUS",
        ),
    ],
)
def test_fuel_status_membership_is_explicit(point: dict[str, Any], reason: str | None) -> None:
    base = {"obd:RPM": 1500.0, "obd:TIMING_ADVANCE": 20.0}
    assert point_eligibility({**base, **point}, _fuel_model()) == reason


def test_engine_state_running_vs_koeo_compatible() -> None:
    running = _steady_model(conditions=[])
    assert point_eligibility({"obd:RPM": 0.0}, running) == "engine_not_running"
    assert point_eligibility({}, running) == "engine_state_unknown:obd:RPM"
    assert (
        point_eligibility({"obd:RPM": float("nan"), "obd:RPM__gap": True}, running)
        == "engine_state_stale:obd:RPM"
    )
    # A KOEO-compatible contract (redundant TPS tracks) is not globally
    # forced to engine-running.
    tps = BehaviorModel1D(
        name="tps",
        kind="surface_1d",
        output="did:ECM_THROTTLE_POS_SENSOR_1",
        axis=Axis1D(name="tps2", channel="did:ECM_THROTTLE_POS_SENSOR_2", domain=(0.0, 5.0)),
        eligibility=ServingEligibility(engine_state=EngineStateRequirement(state="any")),
        fit=PolynomialFit(
            kind="poly", coefficients=[0.0, 1.0], sample_count=10, fitted_at="2026-09-21"
        ),
    )
    assert point_eligibility({"obd:RPM": 0.0, "did:ECM_THROTTLE_POS_SENSOR_2": 0.7}, tps) is None


def test_required_channel_groups_cover_the_whole_contract() -> None:
    model = _fuel_model().model_copy(
        update={
            "conditions": [RateOfChangeCondition(channel="obd:SPEED__roc", max_abs_per_second=2)]
        }
    )
    groups = required_channel_groups(model)
    assert ("obd:FUEL_STATUS",) in groups
    assert ("obd:SPEED",) in groups
    assert ("obd:RPM",) in groups


# ---------------------------------------------------------------------------
# B01.5 — session context from recorded evidence only
# ---------------------------------------------------------------------------

_COLD = ColdStartRequirement(max_initial_coolant_c=40.0)
_SPEC = StartContextSpec.from_requirement(_COLD)


def _cold_model() -> BehaviorModel1D:
    return BehaviorModel1D(
        name="coolant_warmup",
        kind="surface_1d",
        output="obd:COOLANT_TEMP",
        axis=Axis1D(name="t", channel="obd:RUN_TIME", domain=(0.0, 900.0)),
        eligibility=ServingEligibility(
            engine_state=EngineStateRequirement(state="engine_running"), cold_start=_COLD
        ),
        fit=PolynomialFit(
            kind="poly", coefficients=[20.0, 0.1], sample_count=100, fitted_at="2026-09-21"
        ),
        deviation=DeviationConfig(kind="absolute", warning_band=8.0, critical_band=22.0),
    )


def _drive(
    session: str,
    *,
    initial_coolant: float,
    koeo_seconds: int = 5,
    run_seconds: int = 60,
    first_runtime: float = 0.0,
) -> pd.DataFrame:
    rows = []
    t = 0
    for _ in range(koeo_seconds):
        rows.append((t, 0.0, 0.0, initial_coolant))
        t += 1
    for i in range(run_seconds):
        rows.append((t, 800.0, first_runtime + i, initial_coolant + 0.1 * i))
        t += 1
    frame = pd.DataFrame(rows, columns=["t", "obd:RPM", "obd:RUN_TIME", "obd:COOLANT_TEMP"])
    frame["timestamp"] = pd.Timestamp(T0) + pd.to_timedelta(frame.pop("t"), unit="s")
    frame[SESSION_COLUMN] = session
    return frame


def test_cold_start_evidence_warm_restart_and_mid_drive() -> None:
    cold = append_start_context(_drive("cold", initial_coolant=20.0), [_SPEC])
    warm = append_start_context(_drive("warm", initial_coolant=85.0), [_SPEC])
    # A daemon that starts mid-drive: no stopped rows, run-time already 10 min.
    mid = append_start_context(
        _drive("mid", initial_coolant=20.0, koeo_seconds=0, first_runtime=600.0), [_SPEC]
    )
    model = _cold_model()
    last = lambda frame: frame.iloc[-1].to_dict()  # noqa: E731

    assert point_eligibility(last(cold), model) is None
    assert point_eligibility(last(warm), model) == "cold_start_initial_coolant_above_limit"
    # Unknown initial temperature is never read as cold.
    assert point_eligibility(last(mid), model) == "cold_start_no_start_evidence"
    # KOEO rows before the start are refused by engine state, not context.
    assert point_eligibility(cold.iloc[0].to_dict(), model) == "engine_not_running"


def test_start_tracker_runtime_reset_and_session_boundary() -> None:
    tracker = EngineStartTracker(_SPEC)
    # Mid-drive join: no evidence.
    assert tracker.update(1500.0, 900.0, 80.0, 0.0) == (0.0, pytest.approx(np.nan, nan_ok=True))
    # Run-time clock goes backwards: an unseen restart the new clock places.
    observed, coolant = tracker.update(800.0, 3.0, 25.0, 1.0)
    assert observed == 1.0 and coolant == 25.0
    tracker.reset()  # session / replay boundary
    assert tracker.update(800.0, 120.0, 25.0, 2.0)[0] == 0.0


def test_start_context_batch_equals_live(config: CanaryConfig) -> None:
    drive = _drive("cold", initial_coolant=20.0)
    batch = append_start_context(drive, [_SPEC])
    models = BehaviorModels(schema_version=CURRENT_SCHEMA_VERSION, models=[_cold_model()])
    source = LiveFeatureSource(config=config, behavior_models=models)
    observed_col, coolant_col = start_context_columns(_COLD)
    for _, row in drive.iterrows():
        now = row["timestamp"].to_pydatetime()
        _live_tick(
            source,
            now,
            {
                "RPM": row["obd:RPM"],
                "RUN_TIME": row["obd:RUN_TIME"],
                "COOLANT_TEMP": row["obd:COOLANT_TEMP"],
            },
        )
        point = source.latest_operating_point
        assert point is not None
        expected = batch.loc[batch["timestamp"] == row["timestamp"]].iloc[0]
        assert point[observed_col] == expected[observed_col]
        assert (point[coolant_col] == expected[coolant_col]) or (
            np.isnan(point[coolant_col]) and np.isnan(expected[coolant_col])
        )
    # Replay entry/exit resets the evidence.
    source.reset()
    _live_tick(
        source, T0 + timedelta(hours=1), {"RPM": 800.0, "RUN_TIME": 900.0, "COOLANT_TEMP": 20.0}
    )
    assert source.latest_operating_point is not None
    assert source.latest_operating_point[observed_col] == 0.0


# ---------------------------------------------------------------------------
# B01.4 — fitting uses the same contract it persists
# ---------------------------------------------------------------------------


def test_fit_recipe_persists_fuel_and_cold_start_contract() -> None:
    frames = []
    for index, initial in enumerate((18.0, 22.0, 85.0)):
        drive = _drive(f"s{index}", initial_coolant=initial, run_seconds=200)
        drive["obd:FUEL_STATUS"] = np.where(np.arange(len(drive)) % 10 == 0, 4.0, 2.0)
        frames.append(drive)
    frame = pd.concat(frames, ignore_index=True)
    recipe = Surface1DRecipe(
        name="coolant_warmup",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        degree=1,
        min_samples=50,
        min_r_squared=None,
        min_rpm=400.0,
        closed_loop_only=True,
        max_initial_coolant_c=40.0,
    )
    result = fit_recipe(recipe, frame)
    assert result.model is not None, result.reason
    model = result.model
    assert model.eligibility is not None
    assert model.eligibility.fuel_control is not None
    assert model.eligibility.fuel_control.allowed_states == [2]
    assert model.eligibility.cold_start is not None
    assert model.eligibility.cold_start.max_initial_coolant_c == 40.0
    # Only the two cold drives' closed-loop running rows trained.
    assert model.fit.sessions == ["s0", "s1"]
    assert model.fit.sample_count == 2 * 180


# ---------------------------------------------------------------------------
# B01.6 — migration without blessing legacy artifacts
# ---------------------------------------------------------------------------


def test_legacy_artifact_loads_diagnostic_only(tmp_path: Path) -> None:
    path = tmp_path / "behavior_models.yaml"
    legacy = _steady_model(conditions=[], eligibility=None)
    dump_behavior_models(BehaviorModels(schema_version=1, models=[legacy]), path)

    loaded = load_behavior_models(path)
    status = serving_status(loaded.models[0])
    assert status.state == "diagnostic_only"
    assert STATUS_LEGACY_NO_CONTRACT in status.reasons
    assert STATUS_NO_HELD_OUT_VALIDATION in status.reasons
    # The legacy gates still evaluate (existing deviation features keep
    # their meaning) — only alerting is withheld.
    assert deviation(loaded.models[0], 22.0, {"obd:RPM": 1500.0}).deviation == 12.0


def test_contract_round_trips_and_unsupported_schema_rejected(tmp_path: Path) -> None:
    path = tmp_path / "behavior_models.yaml"
    model = _fuel_model().model_copy(
        update={
            "fit": _fuel_model().fit.model_copy(
                update={"validation_sample_count": 50, "calibration_sessions": ["held-out"]}
            )
        }
    )
    dump_behavior_models(
        BehaviorModels(schema_version=CURRENT_SCHEMA_VERSION, models=[model]), path
    )
    loaded = load_behavior_models(path)
    assert loaded.models[0] == model
    assert serving_status(loaded.models[0]).alerting

    path.write_text(path.read_text().replace("schema_version: 2", "schema_version: 3"))
    with pytest.raises(ValueError, match="schema_version 3"):
        load_behavior_models(path)


def test_refit_carry_over_does_not_bless_legacy_prior() -> None:
    legacy = _steady_model(name="legacy", conditions=[], eligibility=None)
    recipe = Surface1DRecipe(name="legacy", output="y", x_column="x", min_samples=10_000)
    frame = pd.DataFrame({"x": np.arange(20.0), "y": np.arange(20.0)})
    models, results = refit_all(
        [recipe], frame, existing=BehaviorModels(schema_version=1, models=[legacy])
    )
    assert results[0].skipped
    assert models.schema_version == CURRENT_SCHEMA_VERSION
    assert models.models[0].eligibility is None
    assert not serving_status(models.models[0]).alerting


def test_diagnostic_only_model_is_shown_but_never_alerts(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Overlay: the legacy model is graded and labelled.
    legacy = _steady_model(conditions=[], eligibility=None)
    source = LiveFeatureSource(
        config=config, behavior_models=BehaviorModels(schema_version=1, models=[legacy])
    )
    source.latest_operating_point = {"obd:RPM": 1500.0, "obd:TIMING_ADVANCE": 40.0}
    state = BehaviorLiveState()
    residuals = _observe_behavior(source, state)
    entry = state.snapshot(include_series=False)["models"]["steady_timing"]
    assert entry["severity"] == "critical"
    assert entry["status"] == "diagnostic_only"
    assert STATUS_LEGACY_NO_CONTRACT in entry["status_reasons"]
    assert residuals[0].alerting is False

    # Detector: the diagnostic-only residual never reaches fusion or emit.
    import src.daemon.inference as inference

    monkeypatch.setattr(inference, "gate_and_collect", lambda *a, **k: True)
    monkeypatch.setattr(inference, "_publish_score", lambda *a, **k: None)
    emitted: list[str] = []
    sources_seen: list[str] = []
    components = SimpleNamespace(
        config=SimpleNamespace(
            models=SimpleNamespace(fusion=SimpleNamespace(behavior_warning_severity=0.5))
        ),
        persistence_gate=None,
        alert_tuner=None,
        scores_broadcaster=None,
        emitter=SimpleNamespace(
            counters=None, emit_behavior=lambda **kw: emitted.append(kw["model"])
        ),
    )

    def behavior_source(name: str, severity: str, *, group: str) -> Any:
        sources_seen.append(name)
        return SimpleNamespace(fired=severity != "ok", severity=1.0)

    detector = BehaviorDetector(
        components,  # type: ignore[arg-type]
        SimpleNamespace(behavior_source=behavior_source),  # type: ignore[arg-type]
        source,
    )
    active = _BehaviorResidual(
        name="active",
        kind="surface_1d",
        channel="obd:TIMING_ADVANCE",
        actual=40.0,
        expected=10.0,
        deviation=30.0,
        severity="critical",
        mode_match=True,
        operating_mode=None,
        timestamp="2026-09-21T12:00:00Z",
    )
    refused = _BehaviorResidual(**{**active.__dict__, "name": "refused", "reason": "off_map"})
    diagnostic = _BehaviorResidual(**{**residuals[0].__dict__})
    ctx = SimpleNamespace(now=T0, timestamp_str="2026-09-21T12:00:00Z")
    fused = detector.apply([active, refused, diagnostic], ctx)  # type: ignore[arg-type]
    assert emitted == ["active"]
    assert len(fused) == 1
    assert "behavior:steady_timing" not in sources_seen


# ---------------------------------------------------------------------------
# B01.7 — fit, serialize, load, then identical results batch / live / replay
# ---------------------------------------------------------------------------


def _parity_drive(session: str, seed: int, *, initial_coolant: float = 20.0) -> pd.DataFrame:
    """A 1 Hz drive: KOEO, idle, steady cruise bands with ramps between, a
    decel fuel cut, and a gap in the speed channel."""
    rng = np.random.default_rng(seed)
    segments = [(0.0, 5), (800.0, 50)]
    rpm: list[float] = []
    for target, seconds in segments:
        rpm.extend([target] * seconds)
    for target in (2000.0, 1200.0):
        start = rpm[-1]
        rpm.extend(np.linspace(start, target, 10).tolist())
        rpm.extend([target] * 45)
    rpm_arr = np.asarray(rpm)
    n = len(rpm_arr)
    running = rpm_arr >= 400.0
    runtime = np.where(running, np.cumsum(running) - 1.0, 0.0)
    fuel = np.where(np.gradient(rpm_arr) < -20.0, 4.0, 2.0)
    fuel[~running] = 0.0
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range(T0, periods=n, freq="1s", tz="UTC"),
            SESSION_COLUMN: session,
            "obd:RPM": rpm_arr,
            "obd:SPEED": np.where(running, rpm_arr / 40.0, 0.0),
            "obd:FUEL_STATUS": fuel,
            "obd:RUN_TIME": runtime,
            "obd:COOLANT_TEMP": initial_coolant + 0.15 * runtime,
            "obd:TIMING_ADVANCE": np.where(
                running, 8.0 + 0.006 * rpm_arr + rng.normal(0.0, 0.4, n), 60.0
            ),
        }
    )
    return frame


def _with_gap_flags(frame: pd.DataFrame, channels: list[str]) -> pd.DataFrame:
    out = frame.copy()
    for channel in channels:
        out[f"{channel}__gap"] = out[channel].isna()
    return out


_CHANNELS = [
    "obd:RPM",
    "obd:SPEED",
    "obd:FUEL_STATUS",
    "obd:RUN_TIME",
    "obd:COOLANT_TEMP",
    "obd:TIMING_ADVANCE",
]


def _fitted_models(config: CanaryConfig, tmp_path: Path) -> BehaviorModels:
    drives = [_parity_drive(f"train-{i}", seed=i) for i in range(3)]
    wide = pd.concat([_with_gap_flags(d, _CHANNELS) for d in drives], ignore_index=True)
    features = featurize_wide_frame(wide, config).features
    recipes = [
        Surface1DRecipe(
            name="steady_timing_vs_rpm",
            output="obd:TIMING_ADVANCE",
            x_column="obd:RPM",
            degree=1,
            min_samples=60,
            min_sessions=2,
            min_session_samples=30,
            min_r_squared=None,
            min_rpm=400.0,
            closed_loop_only=True,
            max_abs_rate_of_change={"obd:RPM": 20.0},
            deviation=DeviationConfig(kind="absolute", warning_band=1.0, critical_band=3.0),
        ),
        Surface1DRecipe(
            name="coolant_warmup",
            output="obd:COOLANT_TEMP",
            x_column="obd:RUN_TIME",
            degree=1,
            min_samples=100,
            min_sessions=2,
            min_session_samples=30,
            min_r_squared=None,
            min_rpm=400.0,
            max_initial_coolant_c=40.0,
            deviation=DeviationConfig(kind="absolute", warning_band=0.5, critical_band=2.0),
        ),
    ]
    fitted, results = refit_all(recipes, features)
    assert all(result.fitted for result in results), [r.reason for r in results]
    path = tmp_path / "behavior_models.yaml"
    dump_behavior_models(fitted, path)
    loaded = load_behavior_models(path)
    assert loaded == fitted
    return loaded


def _batch_results(
    config: CanaryConfig, models: BehaviorModels, drive: pd.DataFrame
) -> list[dict[str, tuple[Any, ...]]]:
    framed = featurize_wide_frame(
        _with_gap_flags(drive, _CHANNELS), config, behavior_models=models
    ).operating_frame
    out: list[dict[str, tuple[Any, ...]]] = []
    for row in framed.to_dict(orient="records"):
        per_model: dict[str, tuple[Any, ...]] = {}
        for model in models.models:
            result = deviation(model, row.get(model.output), row)
            per_model[model.name] = (
                result.expected,
                result.deviation,
                result.severity,
                result.reason,
            )
        out.append(per_model)
    # The vectorized column path agrees with the scalar one row for row.
    for model in models.models:
        expected, reasons = explain_expected_column(framed, model)
        for index, per_model in enumerate(out):
            scalar_expected, _, _, scalar_reason = per_model[model.name]
            if scalar_expected is None:
                assert np.isnan(expected[index])
                assert reasons[index] == scalar_reason
            else:
                assert expected[index] == scalar_expected
    return out


def _live_results(
    source: LiveFeatureSource, drive: pd.DataFrame
) -> list[dict[str, tuple[Any, ...]]]:
    out: list[dict[str, tuple[Any, ...]]] = []
    for row in drive.to_dict(orient="records"):
        now = row["timestamp"].to_pydatetime()
        values = {
            channel.split(":", 1)[1]: row[channel]
            for channel in _CHANNELS
            if not pd.isna(row[channel])
        }
        _live_tick(source, now, values)
        residuals = _observe_behavior(source, BehaviorLiveState())
        out.append(
            {
                residual.name: (
                    residual.expected,
                    residual.deviation,
                    residual.severity,
                    residual.reason,
                )
                for residual in residuals
            }
        )
    return out


def _assert_same(a: list[dict[str, tuple[Any, ...]]], b: list[dict[str, tuple[Any, ...]]]) -> None:
    assert len(a) == len(b)
    for index, (left, right) in enumerate(zip(a, b, strict=True)):
        assert left.keys() == right.keys()
        for name in left:
            le, ld, ls, lr = left[name]
            re_, rd, rs, rr = right[name]
            assert (lr, ls) == (rr, rs), (index, name, left[name], right[name])
            assert (le is None) == (re_ is None), (index, name)
            assert (ld is None) == (rd is None), (index, name)
            if le is not None:
                assert le == pytest.approx(re_, rel=1e-6), (index, name)
            if ld is not None:
                assert ld == pytest.approx(rd, rel=1e-6, abs=1e-6), (index, name)


def test_b01_7_fit_serialize_load_parity_batch_live_replay(
    config: CanaryConfig, tmp_path: Path
) -> None:
    models = _fitted_models(config, tmp_path)
    assert all(serving_status(model).alerting for model in models.models)

    # Evaluation drives: a cold start with a speed dropout, and a warm restart.
    cold = _parity_drive("eval-cold", seed=11)
    cold.loc[100:104, "obd:SPEED"] = np.nan
    warm = _parity_drive("eval-warm", seed=12, initial_coolant=85.0)
    # A mid-drive join: the same cold drive, first 80 s (run-time > 60 s) unseen.
    mid = cold.iloc[80:].reset_index(drop=True)

    batches = {}
    for label, drive in (("cold", cold), ("warm", warm), ("mid", mid)):
        batches[label] = batch = _batch_results(config, models, drive)
        live = _live_results(LiveFeatureSource(config=config, behavior_models=models), drive)
        _assert_same(batch, live)

    # Replay: the live source first serves an unrelated drive, then the
    # replay boundary resets it and the recording is re-fed.
    replay_source = LiveFeatureSource(config=config, behavior_models=models)
    _live_results(replay_source, _parity_drive("unrelated", seed=99).iloc[:40])
    replay_source.reset()
    _assert_same(batches["cold"], _live_results(replay_source, cold))

    # The drives exercise both outcomes, and refusals name their condition.
    reasons = {result[3] for rows in batches.values() for row in rows for result in row.values()}
    for expected_reason in (
        None,
        "engine_not_running",
        "rate_insufficient_history:obd:RPM",
        "rate_exceeded:obd:RPM",
        "fuel_status_excluded:open_loop_load_or_fuel_cut",
        "cold_start_initial_coolant_above_limit",
        "cold_start_no_start_evidence",
    ):
        assert expected_reason in reasons, (expected_reason, sorted(map(str, reasons)))


def test_append_deviation_columns_builds_prerequisites_without_mutating_input() -> None:
    """B01.2 — the prerequisite context is built inside the shared decoder,
    so a direct caller (fingerprinting, analysis) cannot forget it."""
    from src.decode.behavior_decoder import append_deviation_columns

    wide = _constant_rpm_wide()
    before = list(wide.columns)
    models = BehaviorModels(schema_version=CURRENT_SCHEMA_VERSION, models=[_steady_model()])
    out = append_deviation_columns(wide, models, rate_window_seconds=30)
    assert list(wide.columns) == before
    assert out["deviation:steady_timing"].iloc[-1] == pytest.approx(12.0)
    legacy = BehaviorModels(
        schema_version=1, models=[_steady_model(conditions=[], eligibility=None)]
    )
    append_deviation_columns(wide, legacy)
    assert list(wide.columns) == before
