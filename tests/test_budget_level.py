"""A02.3 — the sequence detector's alert level, set from the alert budget on
held-out evaluation drives (``src.models.budget_level``) and wired into the
retrain (``scripts/retrain.py``)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest

from src.models.budget_level import (
    BudgetHoldoutSeenError,
    BudgetHoldoutTooSmallError,
    BudgetLevel,
    apply_budget_level,
    choose_budget_level,
    served_severity,
    unbudgeted_record,
)
from src.models.evaluation import (
    PersistenceSpec,
    ScoreTrace,
    alert_episodes,
    engine_hours,
    sustained_alerts,
)
from src.models.evaluation_detectors import SequenceScores
from src.models.sequence_calibration import (
    ReconstructionErrorDistribution,
    SequenceThresholdCalibration,
)
from src.models.sequence_scoring import SequenceScoringRule

GATE = PersistenceSpec(required=3, window=None, window_seconds=None)
COLUMNS = ("obd:RPM", "obd:RPM__mean", "obd:SPEED", "obd:SPEED__mean")


def _dist(threshold: float) -> ReconstructionErrorDistribution:
    return ReconstructionErrorDistribution(
        count=100,
        mean=threshold / 3,
        std=threshold / 6,
        minimum=0.0,
        maximum=threshold,
        p50=threshold / 3,
        p90=threshold / 2,
        p95=threshold * 0.7,
        p99=threshold * 0.9,
        threshold=threshold,
    )


def _calibration() -> SequenceThresholdCalibration:
    return SequenceThresholdCalibration(
        threshold_k=3.0,
        calibrated_at="2026-10-05T00:00:00Z",
        training_data_hash="abc",
        window_count=100,
        global_error=_dist(0.8),
        channel_errors={column: _dist(1.0) for column in COLUMNS},
        per_mode_errors={"idle": _dist(0.5), "city": _dist(1.1)},
        threshold_quantile=0.999,
        family_errors={"obd:RPM": _dist(1.2), "obd:SPEED": _dist(0.9)},
        family_max_score=_dist(1.4),
        block_rows=30,
        effective_window_count=100,
    )


def _sequence_scores(batch: Any) -> SequenceScores:
    verdict = batch.verdict
    return SequenceScores(
        global_score=batch.global_score,
        global_threshold=batch.global_threshold,
        family_max=batch.family_max,
        family_threshold=batch.family_threshold,
        is_anomaly=verdict.astype(object),
        is_anomaly_global=batch.global_anomaly.astype(object),
    )


def _level(value: float, sessions: tuple[str, ...] = ("2026-09-22T171544",)) -> BudgetLevel:
    return BudgetLevel(
        level=value,
        budget_per_engine_hour=1.0,
        sessions=sessions,
        engine_hours=2.0,
        episodes=1,
        achieved_per_engine_hour=0.5,
        blocks=240,
        block_rows=30,
        persistence={"required": 3, "window": None, "window_seconds": None},
        note="1 episode(s) in 2.00 engine-h at the chosen threshold",
    )


@pytest.mark.parametrize("family_path", [True, False])
def test_scaled_bands_make_the_served_verdict_exactly_severity_over_the_level(
    family_path: bool,
) -> None:
    """The known answer the whole design rests on: the served rule (global OR
    family) is ``severity > 1``, and after every verdict band is multiplied by
    ``L`` the same served rule is ``severity > L`` — window for window."""
    rng = np.random.default_rng(7)
    error = rng.gamma(2.0, 0.4, size=(400, 5, len(COLUMNS)))
    mask = (rng.random(error.shape) > 0.1).astype(np.float64)
    modes = list(rng.choice(cast(Any, ["idle", "city", "highway", None]), size=400))
    before = SequenceScoringRule(_calibration(), COLUMNS, use_family_path=family_path)
    batch = before.score_batch(error, mask, modes)
    severity = served_severity(_sequence_scores(batch))
    np.testing.assert_array_equal(severity > 1.0, batch.verdict)

    for level in (0.6, 1.0, 1.37, 2.5):
        after = SequenceScoringRule(
            apply_budget_level(_calibration(), _level(level)),
            COLUMNS,
            use_family_path=family_path,
        )
        np.testing.assert_array_equal(
            after.score_batch(error, mask, modes).verdict, severity > level
        )


def test_applying_a_level_scales_only_the_verdict_bands_and_records_it() -> None:
    calibration = _calibration()
    scaled = apply_budget_level(calibration, _level(1.5))
    assert scaled.global_error.threshold == pytest.approx(1.2)
    assert scaled.per_mode_errors is not None
    assert scaled.per_mode_errors["idle"].threshold == pytest.approx(0.75)
    assert scaled.family_max_score is not None
    assert scaled.family_max_score.threshold == pytest.approx(2.1)
    # The evidence normalizers are untouched.
    assert scaled.channel_errors == calibration.channel_errors
    assert scaled.family_errors == calibration.family_errors
    record = scaled.alert_budget
    assert record is not None
    assert record["applied"] is True
    assert record["level"] == 1.5
    assert record["budget_per_engine_hour"] == 1.0
    assert record["holdout_sessions"] == ["2026-09-22T171544"]
    assert record["achieved_per_engine_hour"] == 0.5
    assert record["bands_before_level"]["global"] == 0.8
    # It survives the manifest round trip.
    again = SequenceThresholdCalibration.from_dict(scaled.to_dict())
    assert again.alert_budget == record
    assert again.global_error.threshold == pytest.approx(1.2)
    # Twice would compound it: refused.
    with pytest.raises(ValueError, match="already carries an alert-budget level"):
        apply_budget_level(scaled, _level(1.5))


def _trace(hours: float, *, excursions: int, seed: int, start: str) -> ScoreTrace:
    """1 Hz served severity: quiet noise under 1 with ``excursions`` sustained
    15 s rises of graded size spread across the drive."""
    rng = np.random.default_rng(seed)
    n = int(hours * 3600)
    severity = rng.uniform(0.2, 0.9, n)
    for index, at in enumerate(np.linspace(600, n - 600, excursions, dtype=int)):
        severity[at : at + 15] = 1.5 + 0.25 * index
    stamps = pd.Series(pd.date_range(start, periods=n, freq="1s"))
    return ScoreTrace(stamps, severity, np.ones(n, dtype=bool))


def _manifest(training: tuple[str, ...] = (), calibration: tuple[str, ...] = ()) -> Any:
    return SimpleNamespace(training_sessions=training, calibration_sessions=calibration)


def _episodes(trace: ScoreTrace, level: float) -> int:
    verdicts = [None if not np.isfinite(s) else bool(s > level) for s in trace.scores]
    alerting = sustained_alerts(trace.timestamps, verdicts, GATE)
    return len(alert_episodes(trace.timestamps, alerting, verdicts=verdicts))


def test_the_chosen_level_meets_the_budget_on_the_holdout_and_no_lower_level_does() -> None:
    traces = {
        "2026-09-22T171544": _trace(0.8, excursions=4, seed=1, start="2026-09-22T17:15:44Z"),
        "2026-09-23T080000#segment-001": _trace(
            0.5, excursions=3, seed=2, start="2026-09-23T08:00:00Z"
        ),
    }
    level = choose_budget_level(
        traces, manifest=_manifest(("2026-09-01T120000",)), persistence=GATE, block_rows=30
    )
    hours = sum(engine_hours(t.timestamps, t.running) for t in traces.values())
    episodes = sum(_episodes(t, level.level) for t in traces.values())
    assert episodes / hours <= 1.0
    assert episodes == level.episodes
    assert level.achieved_per_engine_hour == pytest.approx(episodes / hours)
    # The lowest such level: just under it, the budget is exceeded.
    below = sum(_episodes(t, level.level - 1e-6) for t in traces.values())
    assert below / hours > 1.0
    # It resolves the excursions rather than sitting above every score.
    assert level.level < max(float(np.max(t.scores)) for t in traces.values())
    assert level.sessions == ("2026-09-22T171544", "2026-09-23T080000")
    assert level.engine_hours == pytest.approx(hours)


@pytest.mark.parametrize(
    ("training", "calibration", "named"),
    [
        (("2026-09-22T171544",), (), "2026-09-22T171544 (training)"),
        # A segment piece of a calibration recording is the same recording.
        ((), ("2026-09-22T171544#segment-002",), "2026-09-22T171544 (calibration)"),
    ],
)
def test_a_session_the_artifact_trained_or_calibrated_on_is_refused(
    training: tuple[str, ...], calibration: tuple[str, ...], named: str
) -> None:
    traces = {
        "2026-09-22T171544#segment-001": _trace(
            2.0, excursions=3, seed=3, start="2026-09-22T17:15:44Z"
        )
    }
    with pytest.raises(BudgetHoldoutSeenError, match="trained or calibrated on") as info:
        choose_budget_level(
            traces,
            manifest=_manifest(training, calibration),
            persistence=GATE,
            block_rows=30,
        )
    assert named in str(info.value)
    assert "A02.3" in str(info.value)


def test_a_holdout_too_short_to_resolve_the_budget_is_refused_and_named() -> None:
    short = {"2026-09-22T171544": _trace(0.5, excursions=2, seed=4, start="2026-09-22T17:15:44Z")}
    with pytest.raises(BudgetHoldoutTooSmallError) as info:
        choose_budget_level(short, manifest=_manifest(), persistence=GATE, block_rows=30)
    message = str(info.value)
    assert "2026-09-22T171544" in message
    assert "0.50 engine-h" in message
    assert "1 engine-h are needed" in message
    with pytest.raises(BudgetHoldoutTooSmallError, match="no evaluation drives"):
        choose_budget_level({}, manifest=_manifest(), persistence=GATE, block_rows=30)
    # A drive the engine never ran on is no exposure at all.
    off = _trace(2.0, excursions=2, seed=5, start="2026-09-22T17:15:44Z")
    parked = ScoreTrace(off.timestamps, off.scores, np.zeros(len(off.scores), dtype=bool))
    with pytest.raises(BudgetHoldoutTooSmallError, match="0.00 engine-h"):
        choose_budget_level(
            {"2026-09-22T171544": parked}, manifest=_manifest(), persistence=GATE, block_rows=30
        )


def test_an_unbudgeted_override_needs_a_reason() -> None:
    with pytest.raises(ValueError, match="stated reason"):
        unbudgeted_record(reason="too small", override_reason="  ")
    record = unbudgeted_record(reason="too small", override_reason="bench, operator")
    assert record["applied"] is False
    assert record["override_reason"] == "bench, operator"


# ---------------------------------------------------------------------------
# The served engine on a real (tiny) artifact — torch only
# ---------------------------------------------------------------------------


def _drive(name: str, *, hours: float, seed: int) -> pd.DataFrame:
    from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN

    rng = np.random.default_rng(seed)
    n = int(hours * 3600)
    rpm = rng.normal(1500, 300, n)
    # Rare sustained excursions the level has to clear.
    for at in np.linspace(900, n - 900, 5, dtype=int):
        rpm[at : at + 20] += 2500
    return pd.DataFrame(
        {
            SESSION_COLUMN: [name] * n,
            TIMESTAMP_COLUMN: pd.date_range("2026-09-22T17:15:44Z", periods=n, freq="1s"),
            "obd:RPM": rpm,
            "obd:SPEED": rng.normal(50, 10, n),
            "obd:THROTTLE_POS": rng.normal(20, 4, n),
            "obd:COOLANT_TEMP": np.full(n, 85.0),
        }
    )


@pytest.mark.ml_training
def test_the_served_engine_meets_the_budget_on_the_holdout_after_the_level(
    tmp_path: Path,
) -> None:
    """End to end on a saved artifact: the level is written into the binary
    and its sidecar, and the *served* rule on the reloaded artifact then
    meets the budget on the holdout. A training session and a too-short
    holdout are refused with the artifact left byte-for-byte as it was."""
    pytest.importorskip("torch")
    from src.artifact_hash import sha256_file
    from src.decode.dids import load_repo_did_map
    from src.features.feature_pipeline import featurize_wide_frame, sequence_feature_columns
    from src.models.benchmark_gate import load_sequence_artifact
    from src.models.budget_level import set_artifact_budget_level
    from src.models.evaluation import engine_running
    from src.models.evaluation_detectors import BatchedSequenceScorer
    from src.models.lstm_training import save_lstm_autoencoder_model, train_lstm_autoencoder
    from src.models.manifest import manifest_path_for
    from src.models.sequence_dataset import SequenceWindowDataset
    from src.preprocessing.normalization import ZScoreScaler
    from tests.configs import example_config

    config = example_config(Path(__file__).resolve().parents[1] / "canary.example.yaml")
    # 4 alerts per engine-hour needs only 0.25 engine-h to resolve: the same
    # rule as the declared 1/h on a quarter of the rows (test runtime).
    budget = 4.0

    did_map = load_repo_did_map()

    def featurize(frame: pd.DataFrame) -> pd.DataFrame:
        return featurize_wide_frame(frame, config, behavior_models=None, did_map=did_map).features

    base = featurize(_drive("2026-09-01T120000", hours=0.1, seed=0))
    columns = sequence_feature_columns(base)
    scaler = ZScoreScaler.fit(base, columns=columns)
    trained = train_lstm_autoencoder(
        SequenceWindowDataset(scaler.transform(base), sequence_length=4, feature_columns=columns),
        config.models.lstm_autoencoder.model_copy(
            update={
                "sequence_length": 4,
                "hidden_size": 6,
                "num_layers": 1,
                "latent_dim": 2,
                "batch_size": 64,
                "max_epochs": 1,
                "validation_fraction": 0.0,
                "random_state": 3,
            }
        ),
        trained_at="2026-09-01T12:00:00Z",
        allow_in_sample_calibration=True,
        scaler=scaler,
    )
    artifact = tmp_path / "model.pt"
    save_lstm_autoencoder_model(trained, artifact)
    assert "2026-09-01T120000" in trained.manifest.training_sessions
    untouched = sha256_file(artifact)

    def level_on(frame: pd.DataFrame) -> BudgetLevel:
        return set_artifact_budget_level(
            artifact, frame, config, persistence=GATE, budget_per_engine_hour=budget
        )

    seen = featurize(_drive("2026-09-01T120000", hours=0.05, seed=1))
    with pytest.raises(BudgetHoldoutSeenError, match="2026-09-01T120000 \\(training\\)"):
        level_on(seen)
    short = featurize(_drive("2026-09-22T171544", hours=0.1, seed=2))
    with pytest.raises(BudgetHoldoutTooSmallError, match="2026-09-22T171544"):
        level_on(short)
    assert sha256_file(artifact) == untouched

    holdout = featurize(_drive("2026-09-22T171544", hours=0.3, seed=3))
    level = level_on(holdout)

    sidecar = json.loads(manifest_path_for(artifact).read_text())
    record = sidecar["thresholds"]["alert_budget"]
    assert record["holdout_sessions"] == ["2026-09-22T171544"]
    assert record["budget_per_engine_hour"] == budget
    assert record["level"] == level.level
    reloaded = load_sequence_artifact(artifact)
    assert reloaded.manifest.thresholds is not None
    assert reloaded.manifest.thresholds["alert_budget"] == record  # the binary agrees

    scored = BatchedSequenceScorer(reloaded).score(holdout)
    stamps = pd.to_datetime(holdout["timestamp"], utc=True).reset_index(drop=True)
    alerting = sustained_alerts(stamps, list(scored.is_anomaly), GATE)
    episodes = len(alert_episodes(stamps, alerting, verdicts=list(scored.is_anomaly)))
    hours = engine_hours(stamps, engine_running(holdout))
    assert episodes == level.episodes
    assert episodes / hours <= budget
    assert record["achieved_per_engine_hour"] == pytest.approx(episodes / hours)


# ---------------------------------------------------------------------------
# The retrain wiring (torch-free: the level itself is stubbed)
# ---------------------------------------------------------------------------


def _span(session: Path, start: str) -> None:
    session.mkdir(parents=True)
    stamps = pd.date_range(start, periods=2, freq=pd.Timedelta(minutes=30), tz="UTC")
    pd.DataFrame({"timestamp": stamps, "value_number": [0.0, 1.0]}).to_parquet(
        session / "telemetry-00000.parquet"
    )


def _retrain_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from scripts import _cli, retrain
    from src.models import training_entry

    root = tmp_path / "sessions"
    names = ["2026-09-20T120000", "2026-09-21T120000", "2026-09-22T120000", "2026-09-23T120000"]
    for day, name in enumerate(names):
        _span(root / name, f"2026-09-{20 + day}T12:00:00")
    sessions = [root / name for name in names]
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: list(sessions))
    frames: list[list[str]] = []

    def _frame(session_dirs: Any, *_a: object, **_k: object) -> Any:
        frames.append([Path(s).name for s in session_dirs])
        return SimpleNamespace(names=[Path(s).name for s in session_dirs])

    monkeypatch.setattr(retrain, "build_feature_frame", _frame)
    monkeypatch.setattr(retrain, "_run_benchmark_gate", lambda *a, **k: (None, None))

    def _if_train(_sessions: object, _config: object, out_dir: Path, **_k: object) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "model.joblib").write_bytes(b"new-if")
        (out_dir / "model.manifest.json").write_text("{}")

    seq_trained_into: list[Path] = []

    def _seq_train(_backend: object, _s: object, _c: object, out_dir: Path, **_k: object) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "model.pt").write_bytes(b"new-seq")
        (out_dir / "model.manifest.json").write_text(json.dumps({"thresholds": {}}))
        seq_trained_into.append(out_dir)

    monkeypatch.setattr(training_entry, "train_isolation_forest", _if_train)
    monkeypatch.setattr(training_entry, "train_sequence_detector", _seq_train)
    served = tmp_path / "models" / "lstm_autoencoder"
    served.mkdir(parents=True)
    (served / "model.pt").write_bytes(b"old-seq")
    return {
        "root": root,
        "sessions": sessions,
        "frames": frames,
        "seq_trained_into": seq_trained_into,
        "served": served,
    }


def _retrain_argv(tmp_path: Path, setup: dict[str, Any], *extra: str) -> list[str]:
    return [
        "--config",
        str(Path(__file__).resolve().parents[1] / "canary.example.yaml"),
        "--sessions-root",
        str(setup["root"]),
        "--all",
        "--no-feedback-policy",
        "--no-ledger",
        "--skip-disposition-summary",
        "--skip-quarantine-exclusion",
        "--calibration-sessions",
        str(setup["sessions"][2]),
        "--if-out",
        str(tmp_path / "models" / "isolation_forest"),
        "--sequence-out",
        str(setup["served"]),
        "--analyses-dir",
        str(tmp_path / "analyses"),
        "--feature-cache",
        str(tmp_path / "feature-cache"),
        *extra,
    ]


def test_retrain_sets_the_level_from_the_evaluation_holdout_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sequence artifact trains into a staging dir the daemon never reads,
    gets its level from the evaluation drives only, and only then reaches the
    served directory."""
    from scripts import retrain

    setup = _retrain_setup(tmp_path, monkeypatch)
    calls: list[dict[str, Any]] = []

    def _set_level(artifact: Path, frame: Any, _config: object, **kwargs: Any) -> BudgetLevel:
        calls.append({"artifact": artifact, "frame": frame.names, **kwargs})
        assert artifact.parent.name == "_staging-lstm_autoencoder"
        assert not (setup["served"] / "model.pt").read_bytes() == b"new-seq"
        artifact.write_bytes(b"new-seq-levelled")
        return _level(1.3, sessions=tuple(frame.names))

    monkeypatch.setattr(retrain, "set_artifact_budget_level", _set_level)

    rc = retrain.main(
        _retrain_argv(tmp_path, setup, "--evaluation-sessions", str(setup["sessions"][3]))
    )

    assert rc == 0
    assert setup["seq_trained_into"] == [tmp_path / "models" / "_staging-lstm_autoencoder"]
    (call,) = calls
    assert call["frame"] == ["2026-09-23T120000"]  # the evaluation drive, nothing else
    assert isinstance(call["persistence"], PersistenceSpec)
    assert (setup["served"] / "model.pt").read_bytes() == b"new-seq-levelled"
    assert not (tmp_path / "models" / "_staging-lstm_autoencoder").exists()
    assert not list((tmp_path / "models").glob("_refused-*"))


def test_retrain_sets_aside_a_sequence_artifact_with_no_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No evaluation drives left: no level, so the artifact is never served.
    The served directory keeps what it had; the IF still publishes."""
    from scripts import retrain

    setup = _retrain_setup(tmp_path, monkeypatch)

    def _must_not_run(*_a: object, **_k: object) -> BudgetLevel:
        raise AssertionError("no evaluation holdout: nothing to set a level on")

    monkeypatch.setattr(retrain, "set_artifact_budget_level", _must_not_run)

    rc = retrain.main(_retrain_argv(tmp_path, setup, "--no-calibration-holdout"))

    assert rc == 0  # partial success: the IF artifact landed
    assert (setup["served"] / "model.pt").read_bytes() == b"old-seq"
    (refused,) = (tmp_path / "models").glob("_refused-unbudgeted-*-lstm_autoencoder")
    assert (refused / "model.pt").read_bytes() == b"new-seq"
    assert not (tmp_path / "models" / "_staging-lstm_autoencoder").exists()
    assert (tmp_path / "models" / "isolation_forest" / "model.joblib").read_bytes() == b"new-if"
    out = capsys.readouterr().out
    assert "alert level refused" in out
    assert "no evaluation holdout" in out


def test_retrain_sets_aside_a_holdout_too_small_and_publishes_only_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import retrain

    setup = _retrain_setup(tmp_path, monkeypatch)

    def _too_small(*_a: object, **_k: object) -> BudgetLevel:
        raise BudgetHoldoutTooSmallError("the evaluation holdout (x) holds 0.40 engine-h")

    monkeypatch.setattr(retrain, "set_artifact_budget_level", _too_small)
    argv = _retrain_argv(tmp_path, setup, "--evaluation-sessions", str(setup["sessions"][3]))

    assert retrain.main(argv) == 0
    assert (setup["served"] / "model.pt").read_bytes() == b"old-seq"
    assert len(list((tmp_path / "models").glob("_refused-unbudgeted-*"))) == 1

    with pytest.raises(SystemExit):
        retrain.parse_args([*argv, "--accept-unbudgeted-thresholds", "  "])
    stamped: list[tuple[Path, dict[str, Any]]] = []
    monkeypatch.setattr(
        retrain, "stamp_unbudgeted", lambda artifact, record: stamped.append((artifact, record))
    )
    reason = "bench drive, operator 2026-10-05"

    assert retrain.main([*argv, "--accept-unbudgeted-thresholds", reason]) == 0

    ((artifact, record),) = stamped
    assert artifact.parent.name == "_staging-lstm_autoencoder"
    assert record["applied"] is False
    assert record["override_reason"] == reason
    assert "0.40 engine-h" in record["refusal"]
    assert (setup["served"] / "model.pt").read_bytes() == b"new-seq"


# ---------------------------------------------------------------------------
# The level's drives are seen (operator decision 2026-10-05, option b)
# ---------------------------------------------------------------------------


def test_the_drives_a_level_was_chosen_on_are_recorded_as_seen() -> None:
    from src.models.evaluation_heldout import record_seen

    levelled = apply_budget_level(_calibration(), _level(1.2, sessions=("2026-09-22T171544",)))
    manifest = SimpleNamespace(
        training_sessions=["2026-09-01T120000"],
        calibration_sessions=["2026-09-10T120000"],
        thresholds=levelled.to_dict(),
    )
    seen: dict[str, dict[str, str]] = {}
    record_seen(seen, "sequence", manifest)
    assert seen == {
        "2026-09-01T120000": {"sequence": "training"},
        "2026-09-10T120000": {"sequence": "calibration"},
        "2026-09-22T171544": {"sequence": "budget_level"},
    }
    # An override stamp names no drives the level rests on: nothing more is seen.
    overridden = SimpleNamespace(
        training_sessions=[],
        calibration_sessions=[],
        thresholds={"alert_budget": unbudgeted_record(reason="x", override_reason="bench")},
    )
    unseen: dict[str, dict[str, str]] = {}
    record_seen(unseen, "sequence", overridden)
    assert unseen == {}


@pytest.mark.ml_training
def test_the_levels_drives_still_score_the_if_and_refuse_an_alert_role_sequence_gate(
    tmp_path: Path,
) -> None:
    """The in-sample verdict the 2026-10-05 decision closes, per detector: on
    the drive the level was chosen on the harness still scores the IF, but
    leaves the sequence detectors out, so A06.5 has no held-out engine time
    for an alert-role sequence detector and refuses it unless overridden."""
    pytest.importorskip("torch")
    pytest.importorskip("sklearn")
    from dataclasses import replace as dc_replace

    from scripts.evaluate_detectors import main as evaluate_main
    from src.decode.dids import load_repo_did_map
    from src.features.feature_pipeline import (
        featurize_wide_frame,
        load_resampled_session,
        sequence_feature_columns,
    )
    from src.models.alert_budget import check_alert_budget
    from src.models.benchmark_gate import gate_view
    from src.models.isolation_forest import default_manifest_path, fit_isolation_forest
    from src.models.lstm_training import save_lstm_autoencoder_model, train_lstm_autoencoder
    from src.models.sequence_dataset import SequenceWindowDataset
    from src.preprocessing.normalization import ZScoreScaler
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord
    from tests.builders import IFConfig
    from tests.configs import example_config

    example = Path(__file__).resolve().parents[1] / "canary.example.yaml"
    config = example_config(example)
    # The harness counts alarms by the publishing config's roles: both alert.
    roles = "  roles:\n    isolation_forest: evidence\n    sequence: evidence\n"
    assert roles in example.read_text()
    alert_yaml = tmp_path / "canary.yaml"
    alert_roles = "  roles:\n    isolation_forest: alert\n    sequence: alert\n"
    alert_yaml.write_text(example.read_text().replace(roles, alert_roles))
    root = tmp_path / "sessions"

    def write(name: str, start: float, seed: int) -> Path:
        rng = np.random.default_rng(seed)
        records = []
        for i in range(120):
            records.append(
                TelemetryRecord.obd(start + i, "RPM", float(rng.normal(1500.0, 50.0)), "rpm")
            )
            records.append(
                TelemetryRecord.obd(start + i, "SPEED", float(rng.normal(30.0, 3.0)), "km/h")
            )
        write_records_to_parquet(records, root / name, max_rows=1000)
        return root / name

    trained_on = write("2026-05-12T000000", 1_778_544_000.0, 1)
    levelled_on = write("2026-05-13T000000", 1_778_630_400.0, 2)
    did_map = load_repo_did_map()
    features = featurize_wide_frame(
        load_resampled_session(trained_on, config), config, behavior_models=None, did_map=did_map
    ).features
    forest = fit_isolation_forest(
        features,
        cast(Any, IFConfig(n_estimators=20)),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T00:00:00Z",
    )
    if_artifact = tmp_path / "models" / "model.joblib"
    forest.save(if_artifact, default_manifest_path(if_artifact))
    columns = sequence_feature_columns(features)
    scaler = ZScoreScaler.fit(features, columns=columns)
    trained = train_lstm_autoencoder(
        SequenceWindowDataset(
            scaler.transform(features), sequence_length=4, feature_columns=columns
        ),
        config.models.lstm_autoencoder.model_copy(
            update={
                "sequence_length": 4,
                "hidden_size": 6,
                "num_layers": 1,
                "latent_dim": 2,
                "batch_size": 16,
                "max_epochs": 1,
                "validation_fraction": 0.0,
                "random_state": 3,
            }
        ),
        trained_at="2026-05-12T00:00:00Z",
        allow_in_sample_calibration=True,
        scaler=scaler,
    )
    assert trained.manifest.thresholds is not None
    levelled = apply_budget_level(
        SequenceThresholdCalibration.from_dict(trained.manifest.thresholds),
        _level(1.0, sessions=(levelled_on.name,)),
    )
    trained = dc_replace(
        trained, manifest=dc_replace(trained.manifest, thresholds=levelled.to_dict())
    )
    seq_artifact = tmp_path / "models" / "lstm" / "model.pt"
    save_lstm_autoencoder_model(trained, seq_artifact)

    out = tmp_path / "report.json"
    rc = evaluate_main(
        [
            str(levelled_on),
            "--config",
            str(alert_yaml),
            "--profile",
            "nissan-xterra-2008",
            "--sessions-root",
            str(root),
            "--if-model",
            str(if_artifact),
            "--sequence-model",
            str(seq_artifact),
            "--baselines",
            "--faults",
            "bank_imbalance",
            "--labels",
            str(tmp_path / "no-labels.yaml"),
            "--tune-revisions",
            str(tmp_path / "no-revisions.yaml"),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--derived-dir",
            str(tmp_path / "derived"),
            "--workers",
            "1",
            "--out",
            str(out),
        ]
    )
    report = json.loads(out.read_text()) if out.exists() else None
    assert report is not None, f"harness wrote no report (exit {rc})"
    # The drive is still evaluated: the IF (which never saw it) is scored
    # there; only the sequence detectors are in-sample and left out.
    (drive,) = report["drives"]
    assert drive["sessions"] == [levelled_on.name]
    assert "isolation_forest" in drive["detectors"]
    assert drive["detectors"]["isolation_forest"]["scored_rows"] > 0
    for name in ("sequence", "sequence_global", "fused"):
        assert name not in drive["detectors"]
        assert f"sequence budget_level on {levelled_on.name}" in drive["in_sample"][name]
    assert report["skipped_drives"] == []
    # The benchmark a levelled retrain runs is not empty: the IF is graded.
    view = gate_view(report)
    assert view.nominal_fp["isolation_forest"]["drives"] == 1
    assert view.nominal_fp["sequence"]["drives"] == 0

    alert = {"isolation_forest": "evidence", "lstm_autoencoder": "alert"}
    check = check_alert_budget(
        report, roles_by_source=alert, published_sources=["lstm_autoencoder"]
    )
    assert check.refused
    assert any(
        v.startswith("sequence: not measured: no held-out engine time") for v in check.violations
    ), check.violations
    overridden = check_alert_budget(
        report,
        roles_by_source=alert,
        published_sources=["lstm_autoencoder"],
        override_reason="operator, bench",
    )
    assert not overridden.refused
    # The IF on the same report is measured (held to its numbers, not refused
    # as unmeasured): the level's drive is held out for it.
    if_check = check_alert_budget(
        report,
        roles_by_source={"isolation_forest": "alert", "lstm_autoencoder": "evidence"},
        published_sources=["isolation_forest"],
    )
    (if_verdict,) = [d for d in if_check.detectors if d.detector == "isolation_forest"]
    assert not if_verdict.reason.startswith("not measured")
    assert if_verdict.engine_hours is not None and if_verdict.engine_hours > 0


def test_only_a_budget_level_keeps_a_seen_drive_and_only_for_its_detectors() -> None:
    from src.models.evaluation_heldout import in_sample_detectors

    level_only = {"2026-09-22T171544": {"sequence": "budget_level"}}
    assert in_sample_detectors(level_only) == {
        name: "sequence budget_level on 2026-09-22T171544"
        for name in ("sequence", "sequence_global", "fused")
    }
    # Training or calibration by any artifact still skips the drive outright.
    mixed = {"a": {"sequence": "budget_level"}, "b": {"sequence": "training"}}
    assert in_sample_detectors(mixed) is None
    assert in_sample_detectors({"a": {"isolation_forest": "calibration"}}) is None


def test_in_sample_results_leave_every_summary_of_their_drive() -> None:
    from src.models.evaluation_records import DriveRecord, SessionScore, drop_in_sample

    stamps = pd.Series(pd.date_range("2026-09-22T17:15:44Z", periods=3, freq="1s"))
    verdicts = np.array([False] * 3, dtype=object)
    clean = SessionScore(stamps, verdicts, np.zeros(3), np.zeros(3, dtype=bool))
    scores = {"s": {"sequence": clean, "isolation_forest": clean}}
    record = DriveRecord(
        SimpleNamespace(drive_id="drive-x"),  # type: ignore[arg-type]
        ["s"],
        detectors={"sequence": {"rows": 3}, "isolation_forest": {"rows": 3}},
    )
    outcomes = [{"detector": "sequence"}, {"detector": "isolation_forest"}]
    slices = [{"detector": "sequence"}, {"detector": "isolation_forest"}]
    shift: dict[str, Any] = {"detectors": {"sequence": {}, "isolation_forest": {}}}
    drop_in_sample(
        {"sequence": "sequence budget_level on s"},
        sessions=["s"],
        scores=scores,
        record=record,
        outcomes=outcomes,
        slices=slices,
        shift=shift,
    )
    assert set(scores["s"]) == {"isolation_forest"}
    assert set(record.detectors) == {"isolation_forest"}
    assert record.in_sample == {"sequence": "sequence budget_level on s"}
    assert outcomes == [{"detector": "isolation_forest"}]
    assert slices == [{"detector": "isolation_forest"}]
    assert set(shift["detectors"]) == {"isolation_forest"}


def _gate_report(drives: list[dict[str, Any]]) -> dict[str, Any]:
    from src.models.benchmark_gate import PROTOCOL_NAME, PROTOCOL_VERSION

    return {
        "protocol": {
            "name": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "seed": 0,
            "faults": [],
            "injections_per_drive": 0,
            "window_seconds": 30,
            "lead_seconds": 10,
            "grace_seconds": 5,
            "persistence": {},
        },
        "detectors": ["isolation_forest", "sequence"],
        "drives": drives,
        "recall": {},
    }


def _gate_drive(
    name: str, episodes: dict[str, int], in_sample: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "drive": name,
        "sessions": [name],
        "engine_hours": 1.0,
        "detectors": {d: {"false_alarm_episodes": n} for d, n in episodes.items()},
        "in_sample": {d: "sequence budget_level on " + name for d in in_sample},
    }


def test_reports_with_different_per_detector_drives_never_diff_that_detector() -> None:
    """Before a level the sequence detector was graded on both drives; after
    it, on one (the other chose its level). Its cells are named incomparable
    rather than diffed, and the IF, on the same drives in both, is still
    compared and its regression still caught."""
    from src.models.benchmark_gate import (
        diff_reports,
        incomparable_detectors,
        regression_summary,
    )

    before = _gate_report(
        [
            _gate_drive("d1", {"isolation_forest": 0, "sequence": 9}),
            _gate_drive("d2", {"isolation_forest": 0, "sequence": 9}),
        ]
    )
    after = _gate_report(
        [
            _gate_drive("d1", {"isolation_forest": 4, "sequence": 0}),
            _gate_drive("d2", {"isolation_forest": 4}, in_sample=("sequence",)),
        ]
    )
    reasons = incomparable_detectors(before, after)
    assert set(reasons) == {"sequence"}
    assert "in-sample on ['d2']" in reasons["sequence"]
    diff = diff_reports(before, after)
    assert diff["comparable"] is True
    (seq_cell,) = [c for c in diff["cells"] if c["detector"] == "sequence"]
    assert (seq_cell["status"], seq_cell["regressed"]) == ("incomparable", False)
    (if_cell,) = [c for c in diff["cells"] if c["detector"] == "isolation_forest"]
    assert if_cell["status"] == "compared"
    assert diff["regressed"] is True  # the IF went from 0 to 4 per engine-hour
    assert any(line.startswith("sequence: not compared") for line in regression_summary(diff))
    # A detector that simply vanished (no in-sample drive) is still `missing`.
    gone = _gate_report(
        [_gate_drive("d1", {"isolation_forest": 0}), _gate_drive("d2", {"isolation_forest": 0})]
    )
    assert "sequence" not in incomparable_detectors(before, gone)
    (missing,) = [c for c in diff_reports(before, gone)["cells"] if c["detector"] == "sequence"]
    assert (missing["status"], missing["regressed"]) == ("missing", True)
