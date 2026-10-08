"""B05.3 / B05.8 — bank-balance event persistence and the lambda bank balance.

The STFT bank-balance baselines failed B02 on ordinary closed-loop control
alone: a 30 s-smoothed balance drifts just past its band for 10-25 s, and the
daemon's 3-of-5 tick gate latches on every such run (2.86 events/h on the
2026-09-23 city test drives). A model may now carry its own persistence, a
two-sided CUSUM of the excess over its warning band whose limit is fitted on
the training fold only, applied identically by the daemon and the evaluator.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.analysis.behavior_validation import (
    PERSISTENCE_NO_TRAINING_EXCURSION,
    EventPolicy,
    RowEvaluation,
    band_excess_event_times,
    fit_band_excess_persistence,
    model_event_times,
)
from src.analysis.refit import (
    STFT_BANK_BALANCE_PERSISTENCE,
    TOTAL_TRIM_STEADY_RATES,
    Surface1DRecipe,
    attach_criteria,
    default_recipes,
    fit_recipe,
    recipe_contract_fingerprint,
)
from src.behavior.behavior_models import (
    BandExcessPersistence,
    DeviationConfig,
    FittedBandExcessPersistence,
    NoiseFloor,
    SyntheticFault,
    ValidationCriteria,
    contract_fingerprint,
)
from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column
from src.config import VehicleParameters
from src.models.fusion import BandExcessAccumulator, PersistenceGate
from src.paths import DATA_DIR
from src.signals.afr import (
    LAMBDA_BANK_BALANCE_AF_VOLTAGE,
    LAMBDA_BANK_BALANCE_WIDEBAND,
    AfrCalibration,
    NoFlashHistory,
    WidebandPairVerification,
    append_afr_channels,
)
from tests.afr_voltage_chain import baro_columns, voltage_chain

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
BALANCE = "derived:STFT_BANK_BALANCE"
RULE = BandExcessPersistence(reset_gap_seconds=30.0, basis="test: the signal's 30 s memory")


# ---------------------------------------------------------------------------
# The accumulator and the gate (the daemon's side)
# ---------------------------------------------------------------------------


def test_accumulator_is_a_two_sided_cusum_of_excess_over_the_band() -> None:
    acc = BandExcessAccumulator(reset_gap_seconds=30.0)
    assert acc.update(4.0, 3.0, 0.0) == pytest.approx(1.0)  # 1 past the band
    assert acc.update(4.0, 3.0, 1.0) == pytest.approx(2.0)
    assert acc.update(0.0, 3.0, 2.0) == pytest.approx(0.0)  # inside: floored at zero
    assert acc.update(-5.0, 3.0, 3.0) == pytest.approx(2.0)  # the low side
    # A gap longer than the signal's memory restarts both sums.
    assert acc.update(-3.5, 3.0, 40.0) == pytest.approx(0.5)


def test_gate_freezes_on_a_missing_verdict_and_needs_strictly_more_than_the_limit() -> None:
    gate = PersistenceGate(3, window=5, window_seconds=8.0)

    def tick(residual: float | None, now: float, firing: bool | None = True) -> bool:
        return gate.observe_band_excess(
            "behavior:m",
            residual,
            3.0,
            firing=firing,
            limit=4.0,
            reset_gap_seconds=30.0,
            now=now,
        )

    assert not tick(5.0, 0.0)  # excess 2
    assert not tick(None, 1.0, firing=None)  # refused: untouched, not a miss
    assert not tick(5.0, 2.0)  # 4 == limit: not yet
    assert tick(4.0, 3.0)  # 5 > 4
    assert gate.active("behavior:m")
    gate.reset()  # a replay boundary clears the accumulation too
    assert not gate.active("behavior:m")
    assert not tick(5.0, 4.0)


def test_a_reloaded_limit_restarts_the_accumulation() -> None:
    gate = PersistenceGate(3)
    common: dict[str, Any] = {"firing": True, "reset_gap_seconds": 30.0}
    assert gate.observe_band_excess("k", 9.0, 3.0, limit=5.0, now=0.0, **common)
    assert not gate.observe_band_excess("k", 3.5, 3.0, limit=50.0, now=1.0, **common)


def test_a_model_reloaded_back_to_the_m_of_n_gate_drops_its_stale_sums() -> None:
    gate = PersistenceGate(3)
    assert gate.observe_band_excess(
        "k", 9.0, 3.0, firing=True, limit=5.0, reset_gap_seconds=30.0, now=0.0
    )
    assert gate.active("k")
    assert not gate.observe("k", None, now=1.0)  # the M-of-N window decides now
    assert not gate.active("k")


# ---------------------------------------------------------------------------
# The evaluator and the training-fold fit
# ---------------------------------------------------------------------------


def _evaluation(residual: np.ndarray, *, width: float = 3.0, refused: Any = ()) -> RowEvaluation:
    n = len(residual)
    reasons = np.full(n, None, dtype=object)
    for i in refused:
        reasons[i] = "fuel_status_stale:obd:FUEL_STATUS"
    evaluated = pd.isna(reasons)
    severity = np.where(np.abs(residual) > width, "warning", "ok").astype(object)
    severity[~evaluated] = None
    return RowEvaluation(
        pieces=np.full(n, "2026-09-01T120000", dtype=object),
        seconds=np.arange(n, dtype=float),
        modes=np.full(n, "city", dtype=object),
        present=np.ones(n, dtype=bool),
        fresh=np.ones(n, dtype=bool),
        reasons=reasons,
        expected=np.where(evaluated, 0.0, np.nan),
        actual=residual.astype(float),
        severity=severity,
        warning_half_width=np.where(evaluated, width, np.nan),
        critical_half_width=np.where(evaluated, 2 * width, np.nan),
        x=residual.astype(float),
        y=np.full(n, np.nan),
    )


def _fitted(limit: float) -> FittedBandExcessPersistence:
    return FittedBandExcessPersistence(
        rule=RULE,
        limit=limit,
        training_drives=["drive-a"],
        training_rows=100,
        training_excursions=1,
    )


def test_an_ordinary_run_just_past_the_band_is_one_event_under_3_of_5_and_none_here() -> None:
    """The measured failure shape: 18 ticks 0.5 past the band."""
    residual = np.zeros(200)
    residual[50:68] = 3.5
    ev = _evaluation(residual)
    groups = np.zeros(200)
    policy = EventPolicy(required=3, window=5, window_seconds=8.0, dedupe_seconds=30.0)
    model = SimpleNamespace(event_persistence=None)
    assert len(model_event_times(model, ev, groups, policy)) == 1  # type: ignore[arg-type]
    model = SimpleNamespace(event_persistence=_fitted(12.0))
    assert model_event_times(model, ev, groups, policy) == []  # type: ignore[arg-type]
    # A shift of the declared size (5 on a 3 band) completes one in 7 ticks.
    residual[120:180] = 5.0
    events = model_event_times(model, _evaluation(residual), groups, policy)  # type: ignore[arg-type]
    assert [t for _, t in events] == [126.0, 156.0]  # then the 30 s dedupe


def test_refused_rows_freeze_the_accumulation_like_the_gate() -> None:
    residual = np.zeros(60)
    residual[10:30] = 5.0
    refused = range(12, 26)  # only 6 evaluated ticks of the shift
    events = band_excess_event_times(
        _evaluation(residual, refused=refused),
        np.zeros(60),
        _fitted(12.0),
        dedupe_seconds=30.0,
    )
    # 2 x 6 = 12 is not > 12: the refused ticks never counted as excess.
    assert events == []


def test_the_limit_is_the_largest_training_excess_and_none_is_a_named_refusal() -> None:
    residual = np.zeros(300)
    residual[20:38] = 3.5  # 18 x 0.5 = 9
    residual[100:110] = -4.0  # 10 x 1 = 10, the largest
    ev = _evaluation(residual)
    fitted = fit_band_excess_persistence(RULE, ev, np.full(300, "drive-a", dtype=object))
    assert isinstance(fitted, FittedBandExcessPersistence)
    assert fitted.limit == pytest.approx(10.0)
    assert fitted.training_excursions == 2
    assert fitted.training_drives == ["drive-a"]
    # The training fold itself never completes an event at its own limit.
    assert band_excess_event_times(ev, np.zeros(300), fitted, dedupe_seconds=30.0) == []

    quiet = fit_band_excess_persistence(
        RULE, _evaluation(np.zeros(300)), np.full(300, "drive-a", dtype=object)
    )
    assert isinstance(quiet, str) and quiet.startswith(PERSISTENCE_NO_TRAINING_EXCURSION)


def test_the_fingerprint_covers_the_rule_not_the_fitted_limit() -> None:
    recipes = {r.name: r for r in default_recipes("nissan-xterra-2008")}
    city = recipes["stft_bank_balance_city"]
    assert city.event_persistence == STFT_BANK_BALANCE_PERSISTENCE
    # A recipe without a rule fingerprints exactly as before the field existed.
    without = replace(city, event_persistence=None)
    assert recipe_contract_fingerprint(without) != recipe_contract_fingerprint(city)
    assert recipes["ltft_bank_balance_city"].event_persistence is None


# ---------------------------------------------------------------------------
# Fit through the refit: limit from the training fold only
# ---------------------------------------------------------------------------


def _balance_drive(day: int, *, excursion: float, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = 900
    balance = rng.normal(0.0, 0.5, n)
    balance[300:318] = excursion  # one ordinary run past the band per drive
    return pd.DataFrame(
        {
            TIMESTAMP_COLUMN: pd.date_range(
                T0 + timedelta(days=day), periods=n, freq="1s", tz="UTC"
            ),
            SESSION_COLUMN: (T0 + timedelta(days=day)).strftime("%Y-%m-%dT%H%M%S"),
            MODE_COLUMN: "city",
            "obd:RPM": 1500.0,
            "obd:FUEL_STATUS": 2.0,
            BALANCE: balance,
        }
    )


def _balance_recipe() -> Surface1DRecipe:
    return Surface1DRecipe(
        name="stft_bank_balance_city",
        output=BALANCE,
        output_unit="%",
        x_column=BALANCE,
        operating_mode="city",
        fit_method="robust_baseline",
        min_samples=150,
        min_sessions=3,
        min_r_squared=None,
        min_rpm=400.0,
        closed_loop_only=True,
        deviation=DeviationConfig(
            kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=0.8
        ),
        event_persistence=RULE,
        max_validation_warning_rate=1.0,
        max_validation_critical_rate=1.0,
        validation=ValidationCriteria(
            kind="constant_baseline",
            unit="%",
            noise_floor=NoiseFloor(value=0.78125, basis="trim LSB"),
            meaningful_deviation=5.0,
            meaningful_deviation_basis="test",
            min_validation_drives=2,
            min_eligible_observations=150,
            max_mae=2.0,
            max_abs_error_p95=3.75,
            max_control_events_per_hour=2.0,
            synthetic_faults=[
                SyntheticFault(magnitude=5.0, direction="positive", duration_seconds=120)
            ],
        ),
    )


def test_refit_fits_the_limit_on_training_drives_and_never_on_a_test_drive() -> None:
    frames = [
        _balance_drive(0, excursion=3.2, seed=0),
        _balance_drive(1, excursion=3.2, seed=1),
        # The two newest drives are the test fold; one holds a far larger run.
        _balance_drive(2, excursion=3.2, seed=2),
        _balance_drive(3, excursion=9.0, seed=3),
    ]
    result = fit_recipe(_balance_recipe(), pd.concat(frames, ignore_index=True))
    model = result.model
    assert model is not None, result.reason
    fitted = model.event_persistence
    assert fitted is not None and fitted.rule == RULE
    assert model.evidence is not None and model.evidence.split is not None
    train_drives = {drive.drive_id for drive in model.evidence.split.train}
    test_drives = {drive.drive_id for drive in model.evidence.split.test}
    assert set(fitted.training_drives) == train_drives
    assert not set(fitted.training_drives) & test_drives
    # The 9-point run on a test drive did not choose the limit, so it is an event.
    assert model.evidence is not None and model.evidence.test is not None
    assert model.evidence.test.events == 1
    # And the evidence is only valid for the declared rule.
    assert model.evidence.contract_fingerprint == contract_fingerprint(model)


def test_refit_refuses_a_rule_with_nothing_to_fit_on() -> None:
    frames = [_balance_drive(day, excursion=0.0, seed=day) for day in range(4)]
    result = fit_recipe(_balance_recipe(), pd.concat(frames, ignore_index=True))
    assert result.model is None and result.skipped
    assert PERSISTENCE_NO_TRAINING_EXCURSION in (result.reason or "")


# ---------------------------------------------------------------------------
# The live detector runs the evaluator's arithmetic
# ---------------------------------------------------------------------------


def test_live_detector_sustains_exactly_where_the_evaluator_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.daemon.detectors.behavior as behavior_detector
    from src.daemon.detectors.behavior import BehaviorDetector, _BehaviorResidual

    monkeypatch.setattr(behavior_detector, "_publish_score", lambda *a, **k: None)
    rng = np.random.default_rng(7)
    residual = rng.normal(0.0, 1.0, 400)
    residual[40:58] = 3.5  # ordinary
    residual[200:320] = 5.0 + rng.normal(0.0, 1.0, 120)  # a declared-size shift
    refused = set(range(230, 240))
    fitted = _fitted(12.0)
    emitted: list[float] = []
    components = SimpleNamespace(
        config=SimpleNamespace(
            models=SimpleNamespace(fusion=SimpleNamespace(behavior_warning_severity=0.5))
        ),
        persistence_gate=PersistenceGate(3, window=5, window_seconds=8.0),
        alert_tuner=None,
        scores_broadcaster=None,
        emitter=SimpleNamespace(
            counters=SimpleNamespace(suppressed_persistence=0),
            emit_behavior=lambda **kw: emitted.append(float(kw["timestamp"])),
        ),
    )
    fusion = SimpleNamespace(
        behavior_source=lambda name, severity, *, channel: SimpleNamespace(
            fired=severity != "ok", severity=1.0
        )
    )
    detector = BehaviorDetector(components, fusion, SimpleNamespace())  # type: ignore[arg-type]
    for i, r in enumerate(residual):
        tick = _BehaviorResidual(
            name="stft_bank_balance_city",
            kind="surface_1d",
            channel=BALANCE,
            actual=float(r),
            expected=0.0,
            deviation=float(r),
            severity="warning" if abs(r) > 3.0 else "ok",
            mode_match=True,
            operating_mode="city",
            timestamp=str(i),
            reason="fuel_status_stale:obd:FUEL_STATUS" if i in refused else None,
            event_persistence=fitted,
            warning_half_width=3.0,
        )
        ctx = SimpleNamespace(
            now=T0 + timedelta(seconds=i), timestamp_str=str(i), capture_profile_allows=True
        )
        detector.apply([tick], ctx)  # type: ignore[arg-type]
    offline = band_excess_event_times(
        _evaluation(residual, refused=sorted(refused)),
        np.zeros(len(residual)),
        fitted,
        dedupe_seconds=0.0,  # the emitter owns dedupe live; compare every sustained tick
    )
    assert emitted and emitted == [t for _, t in offline]
    assert min(emitted) >= 200.0  # the ordinary run never emitted


# ---------------------------------------------------------------------------
# B05.8 — lambda bank balance
# ---------------------------------------------------------------------------

#: These calibrations record no target map, so no row's revision is read.
NO_HISTORY = NoFlashHistory("test: the target AFR lookup is not exercised")
#: The voltage route with the profile's barometric gain (B06.7); the frames
#: below read baro inside the ECM's skip window, so the gain is exactly 1.
_CHAIN = voltage_chain(
    [
        {"voltage": 1.93, "afr": 14.25},
        {"voltage": 2.17, "afr": 14.72},
        {"voltage": 2.41, "afr": 15.65},
        {"voltage": 3.35, "afr": 21.86},
    ]
)


def _voltage_frame() -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-10-01", periods=5, freq="s", tz="UTC"),
            "did:ECM_AF_SENSOR_V_B1": [2.17, 2.41, 2.17, 3.30, np.nan],
            "did:ECM_AF_SENSOR_V_B2": [2.17, 2.17, 1.93, 2.17, 2.17],
        }
    )
    return baro_columns(frame)


def test_lambda_balance_is_taken_in_lambda_after_each_banks_own_transfer() -> None:
    vehicle = VehicleParameters()
    calibration = AfrCalibration.model_validate(_CHAIN)
    out = append_afr_channels(
        _voltage_frame(), vehicle, calibration, channel_units=None, flash_history=NO_HISTORY
    )
    balance = out[LAMBDA_BANK_BALANCE_AF_VOLTAGE]
    from src.signals.derived import fuel_properties

    afr_stoich = fuel_properties(vehicle).afr_stoich
    assert balance.iloc[0] == pytest.approx(0.0)
    # Equal voltage steps are unequal mixture steps: +0.24 V lean is +0.93
    # AFR, -0.24 V rich only -0.47 AFR.
    assert balance.iloc[1] == pytest.approx((15.65 - 14.72) / afr_stoich)
    assert balance.iloc[2] == pytest.approx((14.72 - 14.25) / afr_stoich)
    # A bank at the fuel-cut ceiling is no measurement, not a huge asymmetry.
    assert np.isnan(balance.iloc[3])
    assert np.isnan(balance.iloc[4])
    assert out[gap_column(LAMBDA_BANK_BALANCE_AF_VOLTAGE)].tolist() == [
        False,
        False,
        False,
        False,
        True,
    ]


def test_the_wideband_pair_is_no_channel_until_the_bank_2_pid_is_verified() -> None:
    frame = pd.DataFrame({"obd:O2_WR_LAMBDA_S1": [1.00, 1.02], "obd:O2_WR_LAMBDA_S5": [1.00, 0.99]})
    vehicle = VehicleParameters()
    unverified = append_afr_channels(
        frame,
        vehicle,
        AfrCalibration.model_validate(_CHAIN),
        channel_units=None,
        flash_history=NO_HISTORY,
    )
    assert LAMBDA_BANK_BALANCE_WIDEBAND not in unverified
    verified = AfrCalibration.model_validate(_CHAIN).model_copy(
        update={
            "wideband_bank_balance": WidebandPairVerification(
                session="2026-10-01T120000",
                comparison="compare_channels obd:O2_WR_LAMBDA_S5 -> did:ECM_AF_SENSOR_V_B2",
                n_pairs=120,
                ratio_median=1.001,
                ratio_spread=0.004,
                verified_on="2026-10-01",  # type: ignore[arg-type]
                basis="test",
            )
        }
    )
    out = append_afr_channels(
        frame, vehicle, verified, channel_units=None, flash_history=NO_HISTORY
    )
    assert out[LAMBDA_BANK_BALANCE_WIDEBAND].tolist() == pytest.approx([0.0, 0.03])


def test_afr_bank_balance_recipe_is_lambda_closed_loop_steady_with_declared_criteria() -> None:
    from src.analysis.behavior_validation import load_validation_document

    document = load_validation_document(
        DATA_DIR / "nissan-xterra-2008" / "behavior_validation.yaml"
    )
    recipes = {r.name: r for r in attach_criteria(default_recipes("nissan-xterra-2008"), document)}
    recipe = recipes["afr_bank_balance_robust"]
    assert recipe.output == LAMBDA_BANK_BALANCE_AF_VOLTAGE
    assert recipe.output_unit == "lambda"
    eligibility = recipe.serving_eligibility()
    assert eligibility.fuel_control is not None
    assert eligibility.fuel_control.allowed_states == [2]
    assert eligibility.excluded_operating_modes == ["warmup"]
    assert eligibility.engine_state.state == "engine_running"
    assert dict(recipe.max_abs_rate_of_change) == dict(TOTAL_TRIM_STEADY_RATES)
    assert recipe.event_persistence is not None
    criteria = recipe.validation
    assert criteria is not None and criteria.unit == recipe.output_unit
    assert criteria.meaningful_deviation > criteria.noise_floor.value


# ---------------------------------------------------------------------------
# Default-profile evidence only (R03.12; operator 2026-09-24/25)
# ---------------------------------------------------------------------------


def _write_capture(root: Any, name: str, *, profile: str, corpus_eligible: bool) -> Any:
    import json

    path = root / name
    path.mkdir()
    (path / "capture_profile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": profile,
                "started_at": "2026-09-01T00:00:00Z",
                "profile_snapshot": {"corpus_eligible": corpus_eligible},
            }
        )
    )
    return path


def test_tuning_capture_rows_are_refused_by_name_in_training_and_held_out(tmp_path: Any) -> None:
    from src.analysis.behavior_validation import (
        PROFILE_EXCLUSION_COLUMN,
        profile_refusals,
        stamp_profile_exclusions,
    )

    frames = [_balance_drive(day, excursion=3.2, seed=day) for day in range(4)]
    # Each drive gets a tuning capture rotated in mid-drive (same outing).
    captures = []
    for day, frame in enumerate(frames):
        stamp = (T0 + timedelta(days=day, seconds=900)).strftime("%Y-%m-%dT%H%M%S")
        name = f"_capture-tuning-{stamp}"
        capture = frame.copy()
        capture[TIMESTAMP_COLUMN] = capture[TIMESTAMP_COLUMN] + pd.Timedelta(seconds=900)
        capture[SESSION_COLUMN] = name
        capture[BALANCE] = capture[BALANCE] + 9.0  # would dominate any fit it reached
        captures.append(capture)
        _write_capture(tmp_path, name, profile="tuning", corpus_eligible=False)
    corpus = pd.concat([*frames, *captures], ignore_index=True)
    sessions = [tmp_path / name for name in corpus[SESSION_COLUMN].unique()]

    # Unstamped: the recipe refuses by name instead of silently counting them.
    unverified = fit_recipe(_balance_recipe(), corpus)
    assert unverified.model is None and "profile_unverified" in (unverified.reason or "")
    with pytest.raises(ValueError, match="profile_unverified"):
        profile_refusals(corpus)

    stamped = stamp_profile_exclusions(corpus, sessions)
    capture_rows = stamped[SESSION_COLUMN].str.startswith("_capture-")
    assert set(stamped.loc[capture_rows, PROFILE_EXCLUSION_COLUMN]) == {
        "profile_excluded:not_default_profile"
    }
    assert stamped.loc[~capture_rows, PROFILE_EXCLUSION_COLUMN].isna().all()

    result = fit_recipe(_balance_recipe(), stamped)
    model = result.model or result.candidate
    assert model is not None, result.reason
    assert not any(piece.startswith("_capture-") for piece in model.fit.sessions)
    assert abs(model.fit.median) < 1.0  # type: ignore[union-attr]  # the +9 rows never trained
    assert model.evidence is not None and model.evidence.test is not None
    test = model.evidence.test
    assert test.refusals.get("profile_excluded:not_default_profile", 0) > 0
    # Identical to fitting without the captures at all: they contribute nothing.
    alone = fit_recipe(_balance_recipe(), pd.concat(frames, ignore_index=True))
    assert alone.model is not None
    assert model.fit.training_data_hash == alone.model.fit.training_data_hash
    assert test.evaluated == alone.model.evidence.test.evaluated  # type: ignore[union-attr]


def test_a_capture_with_no_directory_is_refused_not_assumed_eligible(tmp_path: Any) -> None:
    from src.analysis.behavior_validation import PROFILE_EXCLUSION_COLUMN, stamp_profile_exclusions

    frame = pd.DataFrame(
        {SESSION_COLUMN: ["2026-09-01T120000", "_capture-default-2026-09-01T121000"]}
    )
    eligible = _write_capture(
        tmp_path, "_capture-default-2026-09-01T121000", profile="default", corpus_eligible=True
    )
    assert stamp_profile_exclusions(frame, [eligible])[PROFILE_EXCLUSION_COLUMN].isna().all()
    missing = stamp_profile_exclusions(frame, [])
    assert missing[PROFILE_EXCLUSION_COLUMN].tolist() == [
        None,
        "profile_excluded:no_session_directory",
    ]


def test_refit_cli_grants_nothing_to_a_non_default_capture(tmp_path: Any) -> None:
    import scripts.refit_behavior_models as refit_cli

    tuning = _write_capture(
        tmp_path, "_capture-tuning-2026-09-01T120000", profile="tuning", corpus_eligible=False
    )
    default = _write_capture(
        tmp_path, "_capture-default-2026-09-01T130000", profile="default", corpus_eligible=True
    )
    grants = refit_cli._capture_recipe_allowlists(
        [tuning, default], explicit_recipe_grants={"stft_bank_balance_city"}
    )
    assert grants == {
        "_capture-tuning-2026-09-01T120000": set(),
        "_capture-default-2026-09-01T130000": {"stft_bank_balance_city"},
    }


def test_evaluate_rows_refuses_an_unstamped_frame_holding_a_capture(tmp_path: Any) -> None:
    """The guard is in the shared evaluator, not its callers: an unstamped
    frame with a capture piece raises instead of being graded; a stamped one
    refuses the capture's rows by name; only the historical opt-out grades them."""
    from src.analysis.behavior_validation import (
        UnverifiedProfileError,
        evaluate_rows,
        stamp_profile_exclusions,
    )

    frames = [_balance_drive(day, excursion=3.2, seed=day) for day in range(4)]
    result = fit_recipe(_balance_recipe(), pd.concat(frames, ignore_index=True))
    model = result.model or result.candidate
    assert model is not None, result.reason
    name = "_capture-tuning-2026-09-10T120000"
    capture = _balance_drive(9, excursion=0.0, seed=9).assign(**{SESSION_COLUMN: name})
    _write_capture(tmp_path, name, profile="tuning", corpus_eligible=False)
    unstamped = pd.concat([frames[0], capture], ignore_index=True)

    with pytest.raises(UnverifiedProfileError, match="profile_unverified.*_capture-tuning"):
        evaluate_rows(model, unstamped)

    sessions = [tmp_path / frames[0][SESSION_COLUMN].iloc[0], tmp_path / name]
    stamped = evaluate_rows(model, stamp_profile_exclusions(unstamped, sessions))
    is_capture = np.asarray(stamped.pieces == name)
    assert not stamped.evaluated[is_capture].any()
    assert set(stamped.reasons[is_capture]) == {"profile_excluded:not_default_profile"}
    assert stamped.evaluated[~is_capture].any()

    historical = evaluate_rows(model, unstamped, profile_scope="every_recording")
    assert historical.evaluated[is_capture].any()
