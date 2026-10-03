"""A04 — Isolation Forest defects: attribution, imputation, provenance,
refusals at load, episode-counted calibration, seed agreement."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("joblib")
pytest.importorskip("sklearn")

from src.columns import (  # noqa: E402
    MODE_COLUMN,  # noqa: E402
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
)
from src.models.feature_spec_health import (  # noqa: E402
    load_audit_snapshot,
    per_mode_blindness,
    reset_blindness_tracking,
)
from src.models.isolation_forest import (  # noqa: E402
    MISSING_FEATURES_COLUMN,
    MODEL_MODE_COLUMN,
    SCORE_COLUMN,
    SKIP_TOO_FEW_CALIBRATION_DRIVES,
    SKIP_TOO_FEW_TRAINING_DRIVES,
    ModeTrainingSummary,
    exceedance_episodes,
    fit_isolation_forest,
    load_isolation_forest_model,
)


@dataclass(frozen=True)
class _Config:
    enabled: bool = True
    n_estimators: int = 60
    contamination: float = 0.05
    per_mode_contamination: dict[str, float] = field(default_factory=dict)
    random_state: int = 42
    calibration_holdout_fraction: float = 0.2
    target_flag_rate: float = 0.02
    max_dropped_row_fraction: float = 0.75
    n_jobs: int = 1


N_FEATURES = 24


def _drive_frame(
    *,
    sessions: dict[str, str],
    rows_per_session: int = 120,
    modes: tuple[str, ...] = ("idle", "city"),
    seed: int = 3,
) -> pd.DataFrame:
    """``sessions`` maps a session name to its start time; each session holds
    ``rows_per_session`` 1 Hz rows split evenly across ``modes``."""
    rng = np.random.default_rng(seed)
    parts = []
    for name, start in sessions.items():
        per_mode = rows_per_session // len(modes)
        labels = [m for m in modes for _ in range(per_mode)]
        n = len(labels)
        frame = pd.DataFrame(
            {
                SESSION_COLUMN: [name] * n,
                TIMESTAMP_COLUMN: pd.date_range(start, periods=n, freq="1s"),
                MODE_COLUMN: labels,
            }
        )
        offset = np.array([0.0 if m == modes[0] else 50.0 for m in labels])
        for index in range(N_FEATURES):
            frame[f"obd:F{index:02d}"] = offset + rng.normal(index, 1.0 + index / 10, n)
        parts.append(frame)
    return pd.concat(parts, ignore_index=True)


FEATURES = [f"obd:F{i:02d}" for i in range(N_FEATURES)]
THREE_DRIVES = {
    "2026-09-01T100000": "2026-09-01T10:00:00Z",
    "2026-09-02T100000": "2026-09-02T10:00:00Z",
    "2026-09-03T100000": "2026-09-03T10:00:00Z",
}


# ---------------------------------------------------------------- A04.2


def test_contributors_are_the_scoring_models_own_features_ranked_by_score_effect() -> None:
    """A04.2: occlusion over the scoring mode's own features. A column only the
    *other* mode trained on, however extreme, is never reported; the shifted
    feature of the scoring model ranks first.

    Four features, not 24: with many features a single-quantity shift barely
    moves an IF score at all (A04.1), which is the blindness, not attribution.
    """
    frame = _drive_frame(sessions=THREE_DRIVES)
    idle = frame[MODE_COLUMN] == "idle"
    frame["deviation:idle_only"] = np.nan
    frame.loc[idle, "deviation:idle_only"] = np.random.default_rng(1).normal(
        1.0, 0.1, int(idle.sum())
    )
    columns = [*FEATURES[:4], "deviation:idle_only"]
    trained = fit_isolation_forest(frame, _Config(), feature_columns=columns)
    city = set(trained.mode_feature_columns["city"])
    assert "deviation:idle_only" not in city
    assert "deviation:idle_only" in trained.mode_feature_columns["idle"]

    row = frame[~idle].iloc[[10]].copy()
    row["obd:F02"] = row["obd:F02"] + 15.0  # ~12 sigma
    row["deviation:idle_only"] = 1e6  # |z| ranking over the union put this first
    contributions = trained.feature_contributions(row, top_k=5)

    assert contributions, "a shifted row must have positive contributors"
    assert set(contributions) <= city
    assert next(iter(contributions)) == "obd:F02"
    assert all(value > 0 for value in contributions.values())


# ---------------------------------------------------------------- A04.7


def test_a_tolerated_missing_cell_is_imputed_to_the_modes_own_median() -> None:
    frame = _drive_frame(sessions=THREE_DRIVES)
    trained = fit_isolation_forest(frame, _Config(), feature_columns=FEATURES)
    medians = trained.manifest.mode_feature_medians
    assert medians["idle"]["obd:F00"] == pytest.approx(0.0, abs=0.5)
    assert medians["city"]["obd:F00"] == pytest.approx(50.0, abs=0.5)

    row = frame[frame[MODE_COLUMN] == "idle"].iloc[[5]].copy()
    missing = row.copy()
    missing["obd:F00"] = np.nan  # 1 of 24 features: inside the 5 % tolerance
    at_median = row.copy()
    at_median["obd:F00"] = medians["idle"]["obd:F00"]
    scored = trained.score(pd.concat([missing, at_median], ignore_index=True))
    assert not scored[MISSING_FEATURES_COLUMN].any()
    assert scored[SCORE_COLUMN].iloc[0] == pytest.approx(scored[SCORE_COLUMN].iloc[1])
    # The pooled mean (~25) is far outside idle's spread; imputing it is not neutral.
    pooled = row.copy()
    pooled["obd:F00"] = trained.scaler.means["obd:F00"]
    assert trained.score(pooled)[SCORE_COLUMN].iloc[0] > scored[SCORE_COLUMN].iloc[0]


# ---------------------------------------------------------------- A04.8 / A04.6


def _with_unknown_model(trained):  # type: ignore[no-untyped-def]
    city = next(m for m in trained.manifest.modes if m.mode == "city")
    schema = dict(trained.manifest.schema)
    schema["mode_feature_columns"] = {
        **schema["mode_feature_columns"],
        "unknown": list(schema["mode_feature_columns"]["city"]),
    }
    manifest = replace(
        trained.manifest,
        modes=(*trained.manifest.modes, replace(city, mode="unknown")),
        schema=schema,
    )
    return replace(
        trained, models={**trained.models, "unknown": trained.models["city"]}, manifest=manifest
    )


def test_an_artifact_serving_an_unknown_mode_model_is_refused_at_load(tmp_path: Path) -> None:
    """A04.8: the 2026-09-20 artifact served a model for 'unknown'. Loading
    such an artifact refuses that model; its rows are withheld, not scored."""
    reset_blindness_tracking()
    frame = _drive_frame(sessions=THREE_DRIVES)
    trained = _with_unknown_model(fit_isolation_forest(frame, _Config(), feature_columns=FEATURES))
    trained.save(tmp_path / "model.joblib")

    loaded = load_isolation_forest_model(tmp_path / "model.joblib")
    assert "unknown" not in loaded.models
    assert "unknown" in loaded.refused_modes
    assert "unknown" not in loaded.score_thresholds

    row = frame[frame[MODE_COLUMN] == "city"].iloc[[0]].copy()
    row[MODE_COLUMN] = "unknown"
    scored = loaded.score(row)
    assert bool(scored[MISSING_FEATURES_COLUMN].iloc[0])
    assert scored[MODEL_MODE_COLUMN].iloc[0] == "unknown"
    assert "unknown" in load_audit_snapshot()["isolation_forest"]["refused_modes"]


class _Policy:
    def exclusion_reason(self, base: str) -> str | None:
        return "binding opted out (model_feature: false)" if base == "obd:F03" else None


def test_feature_policy_is_checked_at_load_and_violations_are_named(tmp_path: Path) -> None:
    reset_blindness_tracking()
    frame = _drive_frame(sessions=THREE_DRIVES)
    frame["obd:F03__mean"] = frame["obd:F03"]
    trained = fit_isolation_forest(frame, _Config(), feature_columns=[*FEATURES, "obd:F03__mean"])
    trained.save(tmp_path / "model.joblib")

    assert load_isolation_forest_model(tmp_path / "model.joblib").policy_violations == {}
    loaded = load_isolation_forest_model(tmp_path / "model.joblib", feature_policy=_Policy())
    assert set(loaded.policy_violations) == {"obd:F03", "obd:F03__mean"}
    audit = load_audit_snapshot()["isolation_forest"]
    assert audit["policy_checked"] is True
    assert audit["policy_violation_count"] == 2
    assert "mode_feature_columns" not in audit


# ---------------------------------------------------------------- A04.4


@dataclass(frozen=True)
class _DriveFloorConfig(_Config):
    min_calibration_drives_per_mode: int = 3
    min_training_drives_per_mode: int = 0


def test_a_mode_calibrated_on_too_few_drives_is_refused_unless_overridden() -> None:
    train = _drive_frame(sessions=THREE_DRIVES)
    calibration = _drive_frame(
        sessions={"2026-09-05T100000": "2026-09-05T10:00:00Z"}, rows_per_session=200, seed=9
    )
    with pytest.raises(ValueError, match=SKIP_TOO_FEW_CALIBRATION_DRIVES):
        fit_isolation_forest(
            train, _DriveFloorConfig(), feature_columns=FEATURES, calibration_frame=calibration
        )

    trained = fit_isolation_forest(
        train,
        _DriveFloorConfig(),
        feature_columns=FEATURES,
        calibration_frame=calibration,
        calibration_override="bench test",
    )
    for mode in trained.manifest.modes:
        assert mode.calibration_drives == 1
        assert mode.calibration_override == "bench test"
        assert mode.calibration_sessions == ("2026-09-05T100000",)
        assert mode.training_drives == 3
        assert mode.calibration_start_timestamp is not None
        assert ModeTrainingSummary.from_dict(mode.to_dict()) == mode
    assert trained.manifest.calibration["override"] == "bench test"


def test_a_mode_trained_on_too_few_drives_is_refused_and_named() -> None:
    frame = _drive_frame(sessions=dict(list(THREE_DRIVES.items())[:1]), rows_per_session=400)

    @dataclass(frozen=True)
    class _TwoDrives(_Config):
        min_training_drives_per_mode: int = 2

    with pytest.raises(ValueError, match=SKIP_TOO_FEW_TRAINING_DRIVES):
        fit_isolation_forest(frame, _TwoDrives(), feature_columns=FEATURES)


# ---------------------------------------------------------------- A04.3


def test_rows_lost_to_quarantine_are_counted_per_mode() -> None:
    frame = _drive_frame(sessions=THREE_DRIVES)
    idle = frame.index[frame[MODE_COLUMN] == "idle"]
    quarantined = idle[:30]
    frame.loc[quarantined, "obd:F01"] = np.nan
    trained = fit_isolation_forest(
        frame,
        _Config(),
        feature_columns=FEATURES,
        quarantined_cells={"obd:F01": quarantined},
    )
    (idle_summary,) = [m for m in trained.manifest.modes if m.mode == "idle"]
    accounting = idle_summary.row_accounting
    assert accounting["mode_rows"] == len(idle)
    assert accounting["lost_to_quarantine"] == 30
    assert accounting["complete_without_quarantine"] == len(idle)
    assert accounting["complete_rows"] == len(idle) - 30
    assert accounting["fit_rows"] + accounting["calibration_rows"] == accounting["complete_rows"]


# ---------------------------------------------------------------- A04.5


def test_a_feature_one_polling_profile_never_produces_is_not_selected() -> None:
    """A04.5: pooled coverage says 'present'; three of four sessions never
    produce it. It must not join the mode, and the manifest names what each
    mode needs."""
    sessions = {**THREE_DRIVES, "2026-09-04T100000": "2026-09-04T10:00:00Z"}
    frame = _drive_frame(sessions=sessions, rows_per_session=120)
    big = _drive_frame(
        sessions={"2026-09-04T120000": "2026-09-04T12:00:00Z"}, rows_per_session=1200, seed=5
    )
    big["derived:BANK_BALANCE"] = np.random.default_rng(2).normal(0, 1, len(big))
    frame = pd.concat([frame, big], ignore_index=True)
    assert frame["derived:BANK_BALANCE"].notna().mean() > 0.5  # pooled coverage passes

    trained = fit_isolation_forest(
        frame, _Config(), feature_columns=[*FEATURES, "derived:BANK_BALANCE"]
    )
    for mode, columns in trained.mode_feature_columns.items():
        assert "derived:BANK_BALANCE" not in columns, mode
    for summary in trained.manifest.modes:
        assert set(summary.required_channels) == set(FEATURES)


def test_structural_blindness_is_counted_per_mode() -> None:
    by_mode = per_mode_blindness(
        ["a", "a__mean", "b"],
        {"city": ["a", "a__mean", "b", "c"], "idle": [f"x{i}" for i in range(40)] + ["b"]},
    )
    assert by_mode["city"]["scores_nothing"] is True  # 3/4 of city is gone
    assert by_mode["idle"]["absent_count"] == 1
    assert by_mode["idle"]["scores_nothing"] is False  # 1/41 < 5 %


# ---------------------------------------------------------------- A04.11


def test_adjacent_exceedances_are_one_episode() -> None:
    stamps = pd.date_range("2026-09-01", periods=10, freq="1s")
    flags = [True, True, True, False, False, False, False, False, True, True]
    assert exceedance_episodes(flags, stamps, gap_seconds=3.0) == 2
    assert exceedance_episodes(flags, stamps, gap_seconds=30.0) == 1
    assert exceedance_episodes(flags, stamps, ["a"] * 5 + ["b"] * 5, gap_seconds=30.0) == 2
    assert exceedance_episodes([False] * 10, stamps) == 0


def test_calibration_counts_clustered_exceedances_not_rows() -> None:
    """A04.11: with 1 Hz rows, 2 % of a 1,200-row holdout is 24 rows but only a
    few 30 s episodes; the threshold is lowered until 10 episodes stand behind
    it, and the manifest says how many it rests on."""

    @dataclass(frozen=True)
    class _Big(_Config):
        calibration_holdout_fraction: float = 0.5

    frame = _drive_frame(sessions=THREE_DRIVES, rows_per_session=800, modes=("idle",))
    trained = fit_isolation_forest(frame, _Big(), feature_columns=FEATURES)
    (mode,) = trained.manifest.modes
    assert mode.exceedance_episodes is not None
    assert mode.exceedance_episodes >= 10 or mode.target_flag_rate == pytest.approx(0.05, abs=0.01)
    assert mode.achieved_episodes_per_hour is not None and mode.achieved_episodes_per_hour > 0
    assert trained.manifest.calibration["episode_gap_seconds"] == 30.0


# ---------------------------------------------------------------- A04.12 / A04.10


@dataclass(frozen=True)
class _StableConfig(_Config):
    stability_seeds: int = 2


def test_the_calibration_actually_used_and_seed_agreement_are_recorded() -> None:
    train = _drive_frame(sessions=THREE_DRIVES)
    calibration = _drive_frame(
        sessions={
            "2026-09-06T100000": "2026-09-06T10:00:00Z",
            "2026-09-07T100000": "2026-09-07T10:00:00Z",
        },
        rows_per_session=200,
        seed=11,
    )
    trained = fit_isolation_forest(
        train,
        _StableConfig(),
        feature_columns=FEATURES,
        calibration_frame=calibration,
        calibration_policy={"feedback_labels": {"applied": True, "excluded_rows": 0}},
    )
    hyper = trained.manifest.hyperparams
    assert hyper["calibration_holdout_fraction"] == 0.0  # external sessions replaced the tail
    assert hyper["configured_calibration_holdout_fraction"] == 0.2
    assert trained.manifest.calibration["source"] == "external_sessions"
    assert trained.manifest.calibration["policy"]["feedback_labels"]["applied"] is True
    for mode in trained.manifest.modes:
        agreement = mode.seed_agreement
        assert agreement is not None
        assert agreement["seeds"] == 3
        assert 0.0 <= agreement["min_jaccard"] <= agreement["mean_jaccard"] <= 1.0


def test_seed_stability_sweep_reports_agreement_per_ensemble_size() -> None:
    from src.models.isolation_forest import seed_stability_sweep

    rng = np.random.default_rng(0)
    fit_x = rng.normal(0, 1, (300, 8))
    eval_x = rng.normal(0, 1, (400, 8))
    sweep = seed_stability_sweep(fit_x, eval_x, n_estimators=(20, 400), seeds=(0, 1, 2))
    assert set(sweep) == {20, 400}
    for block in sweep.values():
        assert block["seeds"] == 3
        assert block["flagged_per_seed"] == 4
        assert 0.0 <= block["min_jaccard"] <= block["mean_jaccard"] <= 1.0
    # More trees, steadier choice of which rows are flagged.
    assert sweep[400]["mean_jaccard"] >= sweep[20]["mean_jaccard"]


def test_seed_stability_sweep_passes_model_params_to_every_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A04.10: the same sweep measures ``max_samples`` / ``max_features``."""
    from src.models import isolation_forest as module

    seen: list[dict[str, object]] = []
    real = module._isolation_forest_cls()

    def recording(**kwargs: object) -> object:
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(module, "_isolation_forest_cls", lambda: recording)
    rng = np.random.default_rng(0)
    module.seed_stability_sweep(
        rng.normal(0, 1, (200, 6)),
        rng.normal(0, 1, (100, 6)),
        n_estimators=(10,),
        seeds=(0, 1),
        params={"max_samples": 64, "max_features": 0.5},
    )
    assert len(seen) == 2
    assert all(k["max_samples"] == 64 and k["max_features"] == 0.5 for k in seen)


def test_the_daemon_loader_checks_the_feature_policy_against_its_config(tmp_path: Path) -> None:
    """A04.6: the daemon's IF loader resolves the same policy training uses
    (bindings catalog + serving capture profile) and names violations."""
    from src.config import CanaryConfig
    from src.daemon.components import _load_isolation_forest

    reset_blindness_tracking()
    frame = _drive_frame(sessions=THREE_DRIVES)
    frame["decoded:NOT_A_REAL_BINDING"] = frame["obd:F01"] * 2.0
    trained = fit_isolation_forest(
        frame, _Config(), feature_columns=[*FEATURES, "decoded:NOT_A_REAL_BINDING"]
    )
    trained.save(tmp_path / "model.joblib")
    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    loaded = _load_isolation_forest(tmp_path / "model.joblib", config=config)
    assert loaded.policy_violations == {"decoded:NOT_A_REAL_BINDING": "not in the bindings catalog"}
    assert load_audit_snapshot()["isolation_forest"]["policy_violation_count"] == 1


def test_structural_blindness_verdict_is_per_mode_for_a_loaded_if(tmp_path: Path) -> None:
    """A04.5: once the loaded IF registered its mode feature sets, the
    blindness verdict says which modes are blind instead of dividing by the
    union."""
    from src.features.feature_pipeline import FeatureSpecReconciliation
    from src.models.feature_spec_health import observe_reconciliation

    reset_blindness_tracking()
    frame = _drive_frame(sessions=THREE_DRIVES)
    frame["obd:CITY_ONLY"] = np.where(frame[MODE_COLUMN] == "city", 1.0, np.nan)
    frame.loc[frame[MODE_COLUMN] == "city", "obd:CITY_ONLY"] += np.linspace(0, 1, 180)
    trained = fit_isolation_forest(frame, _Config(), feature_columns=[*FEATURES, "obd:CITY_ONLY"])
    trained.save(tmp_path / "model.joblib")
    loaded = load_isolation_forest_model(tmp_path / "model.joblib")
    absent = ("obd:F00", "obd:F01", "obd:CITY_ONLY")
    report = FeatureSpecReconciliation(
        row_count=1,
        expected_feature_columns=tuple(loaded.feature_columns),
        missing_feature_columns=absent,
        extra_feature_columns=(),
        filled_feature_columns=absent,
        coerced_feature_columns=(),
        output_columns=tuple(loaded.feature_columns),
    )
    verdict = None
    for step in range(12):
        verdict = observe_reconciliation(
            "isolation_forest", loaded.manifest.model_version, report, now=float(step * 15)
        ).verdict
    assert verdict is not None
    assert verdict["by_mode"]["city"]["absent_count"] == 3
    assert verdict["by_mode"]["idle"]["absent_count"] == 2
    assert verdict["blind_modes"] == ["city", "idle"]  # 3/25 and 2/24 both clear 5 %
    assert verdict["scores_nothing"] is True


def test_refusing_unknown_never_turns_the_remaining_model_global(tmp_path: Path) -> None:
    """A04.8 verifier finding: a saved {city, unknown} artifact, once unknown is
    refused, holds one model. That must not make city a global fallback: an
    unknown row and a koeo row (a mode no model was fit for) stay withheld."""
    reset_blindness_tracking()
    frame = _drive_frame(sessions=THREE_DRIVES, modes=("city",))
    trained = _with_unknown_model(fit_isolation_forest(frame, _Config(), feature_columns=FEATURES))
    assert set(trained.models) == {"city", "unknown"}
    trained.save(tmp_path / "model.joblib")

    loaded = load_isolation_forest_model(tmp_path / "model.joblib")
    assert set(loaded.models) == {"city"}
    rows = frame.iloc[[0, 1, 2]].copy()
    rows[MODE_COLUMN] = ["unknown", "koeo", "city"]
    scored = loaded.score(rows)
    assert scored[MISSING_FEATURES_COLUMN].tolist() == [True, True, False]
    assert np.isinf(scored[SCORE_COLUMN].iloc[0]) and np.isinf(scored[SCORE_COLUMN].iloc[1])
    assert np.isfinite(scored[SCORE_COLUMN].iloc[2])
    assert loaded.feature_contributions(rows.iloc[[0]]) == {}
    assert loaded.feature_contributions(rows.iloc[[1]]) == {}


def test_the_occlusion_reference_says_mode_median_or_pooled_mean() -> None:
    """A04.2: on an artifact without recorded medians (the 2026-09-20 one),
    occlusion replaces features with the pooled scaler mean, and says so."""
    frame = _drive_frame(sessions=THREE_DRIVES)
    trained = fit_isolation_forest(frame, _Config(), feature_columns=FEATURES)
    assert trained.contribution_reference("idle") == "mode_median"
    assert trained.contribution_reference("koeo") is None
    legacy = replace(trained, manifest=replace(trained.manifest, mode_feature_medians={}))
    assert legacy.contribution_reference("idle") == "pooled_mean"
    row = frame[frame[MODE_COLUMN] == "idle"].iloc[[3]].copy()
    row["obd:F00"] = row["obd:F00"] + 30.0
    # Same model, different reference: the attribution can differ.
    assert set(legacy.feature_contributions(row)) <= set(legacy.mode_feature_columns["idle"])


def _reconciliation(expected: tuple[str, ...], absent: tuple[str, ...]):  # type: ignore[no-untyped-def]
    from src.features.feature_pipeline import FeatureSpecReconciliation

    return FeatureSpecReconciliation(
        row_count=1,
        expected_feature_columns=expected,
        missing_feature_columns=absent,
        extra_feature_columns=(),
        filled_feature_columns=absent,
        coerced_feature_columns=(),
        output_columns=expected,
    )


def test_blindness_transitions_and_messages_follow_the_per_mode_verdict() -> None:
    """A04.5 verifier finding: the transition and the log text must use the
    per-mode verdict. City blind while idle still scores is a WARNING naming
    city — not an "unsatisfiable" ERROR, nor a quiet line from the union's
    absent share — and a change in which modes are blind is a transition."""
    from src.models.feature_spec_health import (
        describe_structural_verdict,
        observe_reconciliation,
        record_load_audit,
    )

    city = tuple(f"c{i}" for i in range(20))
    idle = tuple(f"i{i}" for i in range(20))
    expected = city + idle

    def run(version: str, absent_sequence: list[tuple[str, ...]]) -> list[Any]:
        reset_blindness_tracking()
        record_load_audit(
            "isolation_forest",
            model_version=version,
            refused_modes={},
            policy_violations={},
            policy_checked=False,
            mode_feature_columns={"city": city, "idle": idle},
        )
        return [
            observe_reconciliation(
                "isolation_forest", version, _reconciliation(expected, absent), now=step * 15.0
            )
            for step, absent in enumerate(absent_sequence)
        ]

    # 3 of city's 20 absent (15 %): city is blind, idle is not. 3 of the
    # 40-column union is 7.5 % > 5 %, which the union rule called
    # "unsatisfiable" — the self-contradictory ERROR the verifier found.
    updates = run("v1", [city[:3]] * 12)
    assert [u.transition for u in updates if u.transition] == ["established"]
    verdict = updates[-1].verdict
    assert verdict is not None
    assert verdict["blind_modes"] == ["city"] and verdict["scores_nothing"] is False
    level, message = describe_structural_verdict(verdict, "established")
    assert level == "warning"
    assert "mode model(s) city withhold every row" in message
    assert "city 3/20 (15.0%)" in message and "idle 0/20 (0.0%)" in message
    assert "unsatisfiable" not in message

    # Both blind: an ERROR. Then idle's columns are seen once (no longer
    # structural): which modes are blind changed, so that is a transition,
    # even though the union's absent share moved only from 10 % to 5 %.
    both = (*city[:2], *idle[:2])
    updates = run("v2", [both] * 12 + [city[:2]])
    transitions = [u.transition for u in updates if u.transition]
    assert transitions == ["established", "changed"]
    established = next(u for u in updates if u.transition == "established").verdict
    assert established is not None and established["scores_nothing"] is True
    level, message = describe_structural_verdict(established, "established")
    assert level == "error" and "every mode model is blind" in message
    changed = updates[-1].verdict
    assert changed is not None
    assert changed["blind_modes"] == ["city"] and changed["scores_nothing"] is False


def test_default_feature_discovery_never_picks_an_external_sensor() -> None:
    """H01.1: with no column list, the fitter's own discovery applies the
    source gate — an IMU/GPS/sensor column in the frame never trains."""
    frame = _drive_frame(sessions=THREE_DRIVES)
    rng = np.random.default_rng(3)
    for column in ("imu:ACCEL_X", "gps:speed", "sensor:FUEL_PRESSURE"):
        frame[column] = rng.normal(size=len(frame))
    trained = fit_isolation_forest(frame, _Config())
    assert set(trained.feature_columns) == set(FEATURES)
