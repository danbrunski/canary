"""A06.6 — the sequence detector's forward pass every n ticks.

A novelty screen does not need 1 Hz, and the sequence forward pass is the
largest inference stage (a median 344 ms per tick on the Pi, 2026-09-24). The
window is state advanced by every row, so "score every n ticks" means "append
every tick, forward-pass every n". These tests pin what that must not change:

- on a scored tick the result is exactly the every-tick engine's;
- an unscored tick is a :class:`SequenceCadenceSkip` — no score, threshold or
  verdict — and never a carried-over result; the daemon publishes nothing for
  it, leaves the persistence gate as it is, and counts it as skipped;
- the offline schedule (``SequenceScores.at_cadence``, the A02.3 budget traces)
  is the live engine's own rule, so the alert level is chosen on the windows
  serving scores, records the cadence, and an artifact chosen at another
  cadence refuses to serve;
- the config declares the cadence and refuses one the persistence gate could
  never latch at.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.config import CanaryConfig
from src.daemon.detectors import sequence as sequence_mod
from src.daemon.inference_metrics import InferenceTimingRecorder
from src.models import budget_level as budget_mod
from src.models.budget_level import choose_budget_level, score_budget_traces
from src.models.evaluation import PersistenceSpec, ScoreTrace
from src.models.evaluation_detectors import SequenceScores
from src.models.inference import (
    BUDGET_CADENCE_KEY,
    ForwardCadence,
    SequenceCadenceMismatchError,
    SequenceCadenceSkip,
    budget_cadence,
    require_budget_cadence,
    served_windows,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = (
    "canary.example.yaml",
    "canary.pi.example.yaml",
    "canary.dev.example.yaml",
    "canary.sim.example.yaml",
)


def _example(**models: Any) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load((REPO_ROOT / "canary.example.yaml").read_text())
    data["models"].update(models)
    return data


# --- the rule ------------------------------------------------------------------


def test_the_first_full_window_is_scored_then_every_nth() -> None:
    cadence = ForwardCadence(3)
    steps = [cadence.step(ready) for ready in [False, False, True, True, True, True, True, True]]
    assert steps == [False, False, True, False, False, True, False, False]
    assert cadence.ticks_since_forward == 2


def test_a_tick_without_a_window_restarts_the_count() -> None:
    """After a gap, a session change or a reset the window refills, and its
    first full window is scored at once rather than inheriting the old phase."""
    cadence = ForwardCadence(3)
    assert [cadence.step(r) for r in [True, True, False, True, True, True, True]] == [
        True,
        False,
        False,
        True,
        False,
        False,
        True,
    ]
    cadence.step(True)
    cadence.reset()
    assert cadence.ticks_since_forward is None
    assert cadence.step(True) is True


def test_cadence_one_scores_every_window() -> None:
    pattern = [False, True, True, False, True, True, True]
    np.testing.assert_array_equal(served_windows(pattern, 1), pattern)


@pytest.mark.parametrize("bad", [0, -2])
def test_a_cadence_below_one_tick_is_refused(bad: int) -> None:
    with pytest.raises(ValueError, match="forward cadence must be >= 1"):
        ForwardCadence(bad)


def test_served_windows_is_the_engines_rule() -> None:
    rng = np.random.default_rng(3)
    pattern = rng.random(500) > 0.1
    for every_n in (1, 2, 3, 5):
        cadence = ForwardCadence(every_n)
        expected = [cadence.step(bool(h)) for h in pattern]
        np.testing.assert_array_equal(served_windows(pattern, every_n), expected)


# --- the offline schedule ----------------------------------------------------------


def _scores(n: int, gap_at: int | None = None) -> SequenceScores:
    has = np.ones(n, dtype=bool)
    has[:2] = False
    if gap_at is not None:
        has[gap_at : gap_at + 2] = False
    score = np.where(has, np.linspace(0.1, 2.0, n), np.nan)
    verdict = np.array(
        [bool(s > 1.0) if h else None for s, h in zip(score, has, strict=True)], dtype=object
    )
    return SequenceScores(
        global_score=score,
        global_threshold=np.where(has, 1.0, np.nan),
        family_max=np.where(has, score / 2, np.nan),
        family_threshold=1.5,
        is_anomaly=verdict,
        is_anomaly_global=verdict.copy(),
    )


def test_scores_at_cadence_keep_scored_windows_exactly_and_blank_the_rest() -> None:
    full = _scores(20, gap_at=9)
    served = full.at_cadence(3)
    keep = served_windows([v is not None for v in full.is_anomaly], 3)
    assert keep.sum() < sum(v is not None for v in full.is_anomaly)
    # Scored windows: identical to every-tick scoring.
    np.testing.assert_array_equal(served.global_score[keep], full.global_score[keep])
    np.testing.assert_array_equal(served.family_max[keep], full.family_max[keep])
    assert list(served.is_anomaly[keep]) == list(full.is_anomaly[keep])
    # Unscored ticks: no score and no verdict, never the previous one.
    assert np.isnan(served.global_score[~keep]).all()
    assert np.isnan(served.global_threshold[~keep]).all()
    assert all(v is None for v in served.is_anomaly[~keep])
    assert all(v is None for v in served.is_anomaly_global[~keep])
    # The refilled window after the gap is scored at once.
    assert keep[11]
    assert full.at_cadence(1) is full


# --- the A02.3 budget level --------------------------------------------------------


class _FakeScorer:
    """``BatchedSequenceScorer`` standing in: a known severity per row."""

    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    def score(self, rows: pd.DataFrame) -> SequenceScores:
        return _scores(len(rows))


def _budget_frame(rows: int = 40) -> pd.DataFrame:
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["2026-10-04T194342"] * rows,
            TIMESTAMP_COLUMN: pd.date_range("2026-10-04T19:43:42Z", periods=rows, freq="1s"),
            "obd:RPM": [1500.0] * rows,
        }
    )


def test_budget_traces_carry_a_severity_only_on_the_windows_serving_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_mod, "BatchedSequenceScorer", _FakeScorer)
    monkeypatch.setattr(budget_mod, "suppressed_for", lambda *_a, **_k: ())
    trained = SimpleNamespace(feature_columns=("obd:RPM",), manifest=SimpleNamespace())
    every = CanaryConfig.model_validate(_example())
    third = CanaryConfig.model_validate(_example(sequence_forward_every_n_ticks=3))
    frame = _budget_frame()

    full = score_budget_traces(trained, frame, every)["2026-10-04T194342"]
    served = score_budget_traces(trained, frame, third)["2026-10-04T194342"]

    keep = served_windows(np.isfinite(full.scores), 3)
    np.testing.assert_array_equal(np.isfinite(served.scores), keep)
    np.testing.assert_array_equal(served.scores[keep], full.scores[keep])
    assert np.isfinite(full.scores).sum() == 38
    assert keep.sum() == 13


def test_the_level_records_the_cadence_it_was_chosen_at() -> None:
    n = int(1.1 * 3600)
    rng = np.random.default_rng(1)
    severity = rng.uniform(0.2, 0.9, n)
    severity[::3] = np.nan  # what a cadence-3 trace leaves unscored
    trace = ScoreTrace(
        pd.Series(pd.date_range("2026-10-04T19:43:42Z", periods=n, freq="1s")),
        severity,
        np.ones(n, dtype=bool),
    )
    level = choose_budget_level(
        {"2026-10-04T194342": trace},
        manifest=SimpleNamespace(training_sessions=(), calibration_sessions=()),
        persistence=PersistenceSpec(required=3, window=5, window_seconds=8.0),
        block_rows=30,
        forward_every_n_ticks=3,
    )
    record = level.to_dict()
    assert record[BUDGET_CADENCE_KEY] == 3
    assert budget_cadence(record) == 3


def test_serving_refuses_a_level_chosen_at_another_cadence() -> None:
    chosen_at_three = {"applied": True, "level": 1.4, BUDGET_CADENCE_KEY: 3}
    require_budget_cadence(chosen_at_three, 3)
    with pytest.raises(SequenceCadenceMismatchError, match="every 3 tick"):
        require_budget_cadence(chosen_at_three, 1)
    # A level recorded before the cadence existed was chosen on every window.
    legacy = {"applied": True, "level": 1.4}
    require_budget_cadence(legacy, 1)
    with pytest.raises(SequenceCadenceMismatchError, match="every 1 tick"):
        require_budget_cadence(legacy, 2)
    # No level applied (none, or an operator-stamped unbudgeted artifact):
    # nothing to keep consistent.
    require_budget_cadence(None, 4)
    require_budget_cadence({"applied": False, "override_reason": "x"}, 4)


def test_the_daemon_build_path_refuses_a_mismatched_artifact_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sequence_mod, "_CADENCE_REFUSALS_LOGGED", set())
    loaded = SimpleNamespace(
        serving_version="lstm_autoencoder_cadence-test",
        artifact=SimpleNamespace(
            manifest=SimpleNamespace(
                thresholds={"alert_budget": {"applied": True, BUDGET_CADENCE_KEY: 1}}
            )
        ),
    )
    with caplog.at_level(logging.ERROR, logger=sequence_mod.log.name):
        first = sequence_mod._build_sequence_engine(loaded, forward_every_n_ticks=3)
        again = sequence_mod._build_sequence_engine(loaded, forward_every_n_ticks=3)
    assert first is None and again is None
    refusals = [r for r in caplog.records if "not served" in r.getMessage()]
    assert len(refusals) == 1
    assert "A06.6" in refusals[0].getMessage()


def test_both_build_paths_pass_the_configured_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parent's engine and the scoring child's are built from the same
    config: both get ``models.sequence_forward_every_n_ticks``."""
    from src.daemon.inference_worker import _WorkerState
    from src.models import inference as inference_models

    built: list[int] = []

    class _Engine:
        def __init__(self, _artifact: Any, **kwargs: Any) -> None:
            built.append(kwargs["forward_every_n_ticks"])

    monkeypatch.setattr(inference_models, "SequenceInferenceEngine", _Engine)
    config = CanaryConfig.model_validate(_example(sequence_forward_every_n_ticks=2))
    loaded = SimpleNamespace(
        serving_version="v1",
        artifact=SimpleNamespace(manifest=SimpleNamespace(thresholds={}, model_type="x")),
    )
    components = SimpleNamespace(config=config)
    sequence_mod._build_sequence_engine(
        loaded, forward_every_n_ticks=sequence_mod._forward_cadence_for(components)
    )

    class _Manager:
        current = loaded

        def poll(self) -> None:
            return None

    _WorkerState({"lstm_autoencoder": _Manager()}, config)
    assert built == [2, 2]


# --- the daemon side -----------------------------------------------------------------


def test_a_skipped_tick_publishes_nothing_and_leaves_the_gate_alone() -> None:
    published: list[Any] = []
    observed: list[Any] = []
    emitted: list[Any] = []
    components = SimpleNamespace(
        scores_broadcaster=SimpleNamespace(publish=published.append),
        persistence_gate=SimpleNamespace(observe=lambda *a, **k: observed.append(a)),
        emitter=SimpleNamespace(emit_lstm=lambda *a, **k: emitted.append(a), counters=None),
        alert_tuner=None,
        model_managers={},
        sequence_model_type=None,
    )
    detector = sequence_mod.SequenceDetector(components)  # type: ignore[arg-type]
    skip = SequenceCadenceSkip(
        model_version="v1",
        timestamp="2026-10-04T19:43:50Z",
        session="2026-10-04T194342",
        forward_every_n_ticks=3,
        ticks_since_forward=1,
    )
    assert detector.apply(skip, SimpleNamespace()) == []  # type: ignore[arg-type]
    assert published == [] and observed == [] and emitted == []
    payload = skip.to_dict()
    assert payload["scored"] is False
    assert not {"global_score", "global_threshold", "is_anomaly"} & set(payload)


def test_the_recorder_counts_scored_and_skipped_windows_apart() -> None:
    recorder = InferenceTimingRecorder()
    assert recorder.snapshot().to_dict()["sequence_cadence"] is None
    for scored in (True, False, False, True, False, False):
        recorder.note_sequence_cadence(scored=scored, every_n=3)
    assert recorder.snapshot().to_dict()["sequence_cadence"] == {
        "forward_every_n_ticks": 3,
        "scored": 2,
        "cadence_skipped": 4,
    }


# --- config ---------------------------------------------------------------------------


@pytest.mark.parametrize("example", EXAMPLES)
def test_the_shipped_configs_load_at_every_tick(example: str) -> None:
    config = CanaryConfig.from_yaml(REPO_ROOT / example)
    assert config.models.sequence_forward_every_n_ticks == 1


@pytest.mark.parametrize("example", ["canary.example.yaml", "canary.pi.example.yaml"])
def test_the_example_configs_declare_the_cadence(example: str) -> None:
    import yaml

    models = yaml.safe_load((REPO_ROOT / example).read_text())["models"]
    assert models["sequence_forward_every_n_ticks"] == 1


def test_a_cadence_the_persistence_gate_can_latch_at_is_accepted() -> None:
    # 3 hits inside 8 s at 1 Hz: a forward pass every 3 s spans 6 s.
    config = CanaryConfig.model_validate(_example(sequence_forward_every_n_ticks=3))
    assert config.models.sequence_forward_every_n_ticks == 3


def test_a_cadence_the_persistence_gate_could_never_latch_at_is_refused() -> None:
    # Every 4 s, 3 hits span 8 s: the oldest is evicted as the third arrives.
    with pytest.raises(ValidationError, match="could never latch"):
        CanaryConfig.model_validate(_example(sequence_forward_every_n_ticks=4))


def test_no_wall_clock_bound_means_any_cadence_can_latch() -> None:
    data = _example(sequence_forward_every_n_ticks=10)
    data.setdefault("alerts", {})["persistence_window_seconds"] = None
    assert CanaryConfig.model_validate(data).models.sequence_forward_every_n_ticks == 10


def test_a_cadence_below_one_is_refused_by_the_config() -> None:
    with pytest.raises(ValidationError):
        CanaryConfig.model_validate(_example(sequence_forward_every_n_ticks=0))


# --- the engine (torch) ---------------------------------------------------------------

FEATURES = ("obd:RPM", "obd:SPEED")


def _band(threshold: float) -> dict[str, float]:
    return {
        "count": 100,
        "mean": 0.1,
        "std": 0.1,
        "minimum": 0.0,
        "maximum": 1.0,
        "p50": 0.1,
        "p90": 0.2,
        "p95": 0.3,
        "p99": 0.4,
        "threshold": threshold,
    }


class _WindowMeanModel:
    """Reconstructs every step as the running mean of the window so far, so a
    score depends on the whole window's content and order: a window advanced
    differently under the cadence would score differently."""

    def __init__(self, torch: Any) -> None:
        self._torch = torch

    def to(self, *_a: object, **_k: object) -> _WindowMeanModel:
        return self

    def eval(self) -> _WindowMeanModel:
        return self

    def forward(self, value: Any, mask: Any, delta: Any) -> Any:
        steps = self._torch.arange(1, value.shape[1] + 1, dtype=value.dtype).view(1, -1, 1)
        return value.cumsum(dim=1) / steps


def _stub_trained(torch: Any) -> Any:
    manifest = SimpleNamespace(
        model_type="lstm_autoencoder",
        model_version="lstm_autoencoder_cadence-stub",
        scaler=None,
        thresholds={
            "threshold_k": 3.0,
            "calibrated_at": "2026-10-05T00:00:00Z",
            "training_data_hash": "stub",
            "window_count": 100,
            "global_error": _band(40.0),
            "channel_errors": {column: _band(40.0) for column in FEATURES},
        },
        schema={"sequence_length": 4, "feature_columns": list(FEATURES)},
        training_envelope=None,
    )
    return SimpleNamespace(
        model=_WindowMeanModel(torch), feature_columns=FEATURES, manifest=manifest
    )


def _drive_rows(rows: int = 40) -> pd.DataFrame:
    stamps = list(pd.date_range("2026-10-04T19:43:42Z", periods=rows, freq="1s"))
    # A 30 s hole at row 17: the window clears and refills.
    stamps = stamps[:17] + [s + pd.Timedelta(seconds=30) for s in stamps[17:]]
    rng = np.random.default_rng(5)
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["2026-10-04T194342"] * rows,
            TIMESTAMP_COLUMN: stamps,
            "obd:RPM": 800.0 + 600.0 * rng.random(rows),
            "obd:SPEED": 40.0 * rng.random(rows),
        }
    )


@pytest.mark.parametrize("every_n", [2, 3, 5])
def test_scored_ticks_match_every_tick_scoring_and_the_rest_are_marked(every_n: int) -> None:
    torch = pytest.importorskip("torch")
    from src.models.inference import SequenceInferenceEngine, SequenceInferenceResult

    every = SequenceInferenceEngine(_stub_trained(torch))
    cadenced = SequenceInferenceEngine(_stub_trained(torch), forward_every_n_ticks=every_n)
    frame = _drive_rows()
    windows: list[bool] = []
    scored = skipped = 0
    for index in range(len(frame)):
        a = every.observe(frame.iloc[index])
        b = cadenced.observe(frame.iloc[index])
        windows.append(a is not None)
        if isinstance(b, SequenceInferenceResult):
            scored += 1
            assert isinstance(a, SequenceInferenceResult)
            left, right = a.to_dict(), b.to_dict()
            left.pop("latency_ms")
            right.pop("latency_ms")
            assert left == right, f"row {index}"
        elif isinstance(b, SequenceCadenceSkip):
            skipped += 1
            assert isinstance(a, SequenceInferenceResult), f"row {index}: skip without a window"
            assert b.timestamp == a.timestamp
            assert b.forward_every_n_ticks == every_n
            assert 1 <= b.ticks_since_forward < every_n
        else:
            assert a is None, f"row {index}: the cadence lost a window"
    expected = served_windows(windows, every_n)
    assert scored == int(expected.sum())
    assert scored + skipped == sum(windows)
    assert skipped > 0


def test_a_reset_restarts_the_cadence_with_the_window() -> None:
    torch = pytest.importorskip("torch")
    from src.models.inference import SequenceInferenceEngine, SequenceInferenceResult

    engine = SequenceInferenceEngine(_stub_trained(torch), forward_every_n_ticks=3)
    frame = _drive_rows()
    results = [engine.observe(frame.iloc[i]) for i in range(5)]
    assert isinstance(results[3], SequenceInferenceResult)
    assert isinstance(results[4], SequenceCadenceSkip)
    engine.reset()
    refilled = [engine.observe(frame.iloc[i]) for i in range(5, 9)]
    assert refilled[:3] == [None, None, None]
    assert isinstance(refilled[3], SequenceInferenceResult)


def _installed_sequence_artifact() -> Path | None:
    for path in sorted((REPO_ROOT / "models").glob("*/lstm_autoencoder/model.pt")):
        if not path.parent.parent.name.startswith("_"):
            return path
    return None


@pytest.mark.slow
def test_a_real_drive_scores_identically_on_the_ticks_the_cadence_scores(
    tmp_path: Path,
) -> None:
    """The known answer on real data: the committed drive slice through the
    locally installed sequence artifact, every tick and every third. Model
    binaries are not in git, so CI skips this; the laptop runs it."""
    pytest.importorskip("torch")
    artifact = _installed_sequence_artifact()
    if artifact is None:
        pytest.skip("no sequence artifact installed under models/<vehicle>/")
    from src.features.feature_pipeline import align_features_to_schema, build_feature_frame
    from src.models.benchmark_gate import load_sequence_artifact
    from src.models.evaluation_detectors import BatchedSequenceScorer, suppressed_for
    from src.models.inference import SequenceInferenceEngine, SequenceInferenceResult
    from src.vehicle.profiles import resolve_profile_for_tooling, scope_config

    config = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml")
    config = scope_config(config, resolve_profile_for_tooling(config, None))
    trained = load_sequence_artifact(artifact)
    features = build_feature_frame(
        [REPO_ROOT / "tests/fixtures/ground_truth/drive-slice"], config, max_workers=1
    )
    frame = align_features_to_schema(features, list(trained.feature_columns))
    suppress = suppressed_for(trained, config)
    every = SequenceInferenceEngine(trained, suppress_channels=suppress)
    cadenced = SequenceInferenceEngine(
        load_sequence_artifact(artifact), suppress_channels=suppress, forward_every_n_ticks=3
    )
    schedule: list[bool] = []
    scored = 0
    for index in range(len(frame)):
        a = every.observe(frame.iloc[index])
        b = cadenced.observe(frame.iloc[index])
        schedule.append(isinstance(b, SequenceInferenceResult))
        if isinstance(b, SequenceInferenceResult):
            scored += 1
            assert isinstance(a, SequenceInferenceResult)
            assert b.global_score == a.global_score
            assert b.is_anomaly == a.is_anomaly
            assert dict(b.family_scores) == dict(a.family_scores)
            assert b.envelope == a.envelope
        elif isinstance(b, SequenceCadenceSkip):
            assert isinstance(a, SequenceInferenceResult)
            assert b.envelope == a.envelope
    assert scored > 50, f"only {scored} scored windows on the slice"
    # The offline schedule (A02.3 budget traces, at_cadence) is the served one.
    batched = BatchedSequenceScorer(trained, suppress_channels=suppress).score(frame)
    assert [v is not None for v in batched.at_cadence(3).is_anomaly] == schedule


@pytest.mark.slow
@pytest.mark.parametrize("child", [True, False], ids=["scoring-child", "in-process"])
async def test_the_live_pipeline_scores_every_third_window_and_counts_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: bool
) -> None:
    """End to end through ``LiveInferencePipeline``, in the scoring child (every
    message pickled both ways) and in process: windows still advance every
    tick, a third are forward-passed, the rest are counted as skipped and still
    reach the envelope meter, and A01.7's span summary counts scored windows
    only."""
    pytest.importorskip("torch")
    import threading

    from src.daemon import inference as inference_mod
    from tests import test_live_sequence_ordering as ordering

    config = ordering._config(tmp_path)
    config = config.model_copy(
        update={"models": config.models.model_copy(update={"sequence_forward_every_n_ticks": 3})}
    )
    trained = ordering._trained()
    release = threading.Semaphore(0)
    worker = None
    if child:
        worker, _pipe = ordering._child_worker(config, trained, release)
    else:
        compute = inference_mod._compute_tick

        def held_compute(request: Any) -> Any:
            assert release.acquire(timeout=5.0)
            return compute(request)

        monkeypatch.setattr(inference_mod, "_compute_tick", held_compute)
    pipeline, source, recorder = ordering._pipeline(config, trained, worker)
    try:
        await ordering._drive(pipeline, source, release)
    finally:
        release.release(ordering.TICKS)
        await pipeline.close(timeout_seconds=1.0)

    snapshot = recorder.snapshot().to_dict()
    windows = ordering.TICKS - ordering.SEQUENCE_LENGTH + 1  # 10
    scored = int(served_windows([True] * windows, 3).sum())  # 4
    assert snapshot["sequence_cadence"] == {
        "forward_every_n_ticks": 3,
        "scored": scored,
        "cadence_skipped": windows - scored,
    }
    assert snapshot["sequence_windows"]["scored"] == scored
    assert snapshot["sequence_envelope"]["windows"] == windows


# --- what grades the detector: the A02 harness, the A06.5 gate, the role gate ---------


def _cadence_config(every_n: int) -> CanaryConfig:
    return CanaryConfig.model_validate(_example(sequence_forward_every_n_ticks=every_n))


def test_the_harness_grades_the_sequence_detector_on_the_served_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DeployedStack`` (A02's harness and the A06.5 gate's evaluation) scores
    the sequence detector as serving does at the configured cadence."""
    from src.models import evaluation_detectors as ed

    monkeypatch.setattr(ed, "BatchedSequenceScorer", _FakeScorer)
    frame = _budget_frame(20)
    every = ed.DeployedStack(_cadence_config(1), None, SimpleNamespace())  # type: ignore[arg-type]
    third = ed.DeployedStack(_cadence_config(3), None, SimpleNamespace())  # type: ignore[arg-type]

    full = every.score(frame)["sequence"]
    served = third.score(frame)["sequence"]
    keep = served_windows([v is not None for v in full.verdict], 3)
    assert [v is not None for v in served.verdict] == list(keep)
    assert list(served.verdict[keep]) == list(full.verdict[keep])
    assert keep.sum() < sum(v is not None for v in full.verdict)


def test_the_score_cache_never_reuses_a_sequence_entry_across_cadences() -> None:
    pytest.importorskip("sklearn")
    from src.models.evaluation_identity import sequence_identity

    one = sequence_identity("artifact-digest", _cadence_config(1))
    three = sequence_identity("artifact-digest", _cadence_config(3))
    assert one != three
    assert one == sequence_identity("artifact-digest", _cadence_config(1))


def _budget_report(cadence: int | None) -> dict[str, Any]:
    names = ("isolation_forest", "sequence", "fused")
    report: dict[str, Any] = {
        "detectors": list(names),
        "roles": dict.fromkeys(names, "alert"),
        "drives": [
            {
                "drive": f"d{i}",
                "cross_era": False,
                "engine_hours": 2.0,
                "detectors": {n: {"false_alarm_episodes": 0} for n in names},
            }
            for i in range(3)
        ],
        "false_alarms": {n: {"false_alarms_per_engine_hour": {"high": 0.0}} for n in names},
        "artifacts": {},
    }
    if cadence is not None:
        report["sequence_forward_every_n_ticks"] = cadence
    return report


ALERT_ROLES = {"isolation_forest": "alert", "lstm_autoencoder": "alert"}
PUBLISHED = ["isolation_forest", "lstm_autoencoder"]


def test_the_alert_budget_fails_sequence_and_fused_on_a_report_at_another_cadence() -> None:
    from src.models.alert_budget import check_alert_budget

    matched = check_alert_budget(
        _budget_report(3),
        roles_by_source=ALERT_ROLES,
        published_sources=PUBLISHED,
        forward_every_n_ticks=3,
    )
    assert matched.violations == ()
    mismatched = check_alert_budget(
        _budget_report(3),
        roles_by_source=ALERT_ROLES,
        published_sources=PUBLISHED,
        forward_every_n_ticks=1,
    )
    failed = {d.detector: d for d in mismatched.detectors if not d.passed}
    assert set(failed) == {"sequence", "fused"}
    assert "A06.6" in failed["sequence"].reason
    assert "every 3 tick" in failed["fused"].reason
    # A report from before the cadence existed scored every window.
    legacy = check_alert_budget(
        _budget_report(None),
        roles_by_source=ALERT_ROLES,
        published_sources=PUBLISHED,
        forward_every_n_ticks=2,
    )
    assert {d.detector for d in legacy.detectors if not d.passed} == {"sequence", "fused"}


def _write_verdict(tmp_path: Path, *, cadence: int, graded: tuple[str, ...]) -> Path:
    from src.models.alert_budget import (
        check_alert_budget,
        file_sha256,
        verdict_record,
        write_verdict,
    )

    artifact = tmp_path / "model.pt"
    artifact.write_bytes(b"weights")
    report = _budget_report(cadence)
    report["detectors"] = list(graded)
    check = check_alert_budget(
        report,
        roles_by_source=ALERT_ROLES,
        published_sources=PUBLISHED if "sequence" in graded else ["isolation_forest"],
        forward_every_n_ticks=cadence,
    )
    record = verdict_record(
        check, source="lstm_autoencoder", artifact_path=artifact, sha256=file_sha256(artifact)
    )
    assert record["sequence_forward_every_n_ticks"] == cadence
    assert record["passed"] is True
    write_verdict(artifact, record)
    return artifact


def test_the_role_gate_demotes_a_sequence_verdict_measured_at_another_cadence(
    tmp_path: Path,
) -> None:
    from src.models.role_gate import resolve_role

    artifact = _write_verdict(tmp_path, cadence=3, graded=("isolation_forest", "sequence"))
    served = resolve_role(
        "lstm_autoencoder", "alert", artifact_path=artifact, forward_every_n_ticks=3
    )
    assert served.effective == "alert"
    other = resolve_role(
        "lstm_autoencoder", "alert", artifact_path=artifact, forward_every_n_ticks=1
    )
    assert other.effective == "evidence"
    assert "A06.6" in other.reason and "every 3 tick" in other.reason


def test_the_role_gate_demotes_any_verdict_that_graded_the_fused_verdict(
    tmp_path: Path,
) -> None:
    from src.models.role_gate import resolve_role

    fused = _write_verdict(tmp_path, cadence=1, graded=("isolation_forest", "fused"))
    resolution = resolve_role(
        "isolation_forest", "alert", artifact_path=fused, forward_every_n_ticks=2
    )
    assert resolution.effective == "evidence"
    assert "A06.6" in resolution.reason
    # An IF verdict that graded nothing cadence-bound is not touched.
    (tmp_path / "if").mkdir()
    alone = _write_verdict(tmp_path / "if", cadence=1, graded=("isolation_forest",))
    assert (
        resolve_role(
            "isolation_forest", "alert", artifact_path=alone, forward_every_n_ticks=2
        ).effective
        == "alert"
    )


def test_the_daemon_role_gate_is_built_with_the_configured_cadence(tmp_path: Path) -> None:
    from src.models.role_gate import DetectorRoleGate

    artifact = _write_verdict(tmp_path, cadence=1, graded=("isolation_forest", "sequence"))
    loaded = SimpleNamespace(artifact_path=artifact, serving_version="v1")
    gate = DetectorRoleGate(
        {"lstm_autoencoder": "alert"}, loaded_for=lambda _s: loaded, forward_every_n_ticks=3
    )
    assert gate["lstm_autoencoder"] == "evidence"
    assert (
        DetectorRoleGate(
            {"lstm_autoencoder": "alert"}, loaded_for=lambda _s: loaded, forward_every_n_ticks=1
        )["lstm_autoencoder"]
        == "alert"
    )


def test_the_synthetic_fault_harness_builds_its_engine_at_the_configured_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.models import synthetic_fault_eval as sfe

    class _StopError(Exception):
        pass

    def _engine(_trained: Any, **kwargs: Any) -> Any:
        raise _StopError(kwargs)

    monkeypatch.setattr(sfe, "SequenceInferenceEngine", _engine)
    with pytest.raises(_StopError) as stopped:
        sfe.evaluate_detectors(
            _budget_frame(5),
            _cadence_config(3),
            SimpleNamespace(score_scales={}),  # type: ignore[arg-type]
            fault_channel="obd:RPM",
            did_map=None,
            trained_sequence=SimpleNamespace(),  # type: ignore[arg-type]
        )
    assert stopped.value.args[0] == {"forward_every_n_ticks": 3}


def test_a_cadence_refusal_reaches_status_health(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.daemon.health import HealthMonitor

    monkeypatch.setattr(sequence_mod, "_CADENCE_REFUSALS_LOGGED", set())
    health = HealthMonitor()
    loaded = SimpleNamespace(
        serving_version="v1",
        artifact=SimpleNamespace(
            manifest=SimpleNamespace(
                thresholds={"alert_budget": {"applied": True, BUDGET_CADENCE_KEY: 1}}
            )
        ),
    )
    components = SimpleNamespace(
        config=_cadence_config(3),
        health_monitor=health,
        model_managers={"lstm_autoencoder": SimpleNamespace(current=loaded)},
        sequence_model_type="lstm_autoencoder",
    )
    detector = sequence_mod.SequenceDetector(components)  # type: ignore[arg-type]
    ctx = SimpleNamespace(scoring_allowed=True)
    assert detector.prepare(object(), ctx) is None  # type: ignore[arg-type]
    refusal = health.snapshot().get("sequence_cadence_refusal")
    assert refusal is not None and "A06.6" in refusal


def test_build_components_gives_the_role_gate_the_configured_cadence(tmp_path: Path) -> None:
    from src.daemon import build_components
    from tests.configs import isolate

    config = isolate(_cadence_config(3), tmp_path)
    config.daemon.models_dir = tmp_path / "models"
    config.daemon.anomalies_dir = tmp_path / "anomalies"
    components = build_components(config, load_models=False)
    assert components.role_gate._forward_every_n_ticks == 3  # type: ignore[union-attr]


def _gate_report(cadence: int | None, episodes: dict[str, int]) -> dict[str, Any]:
    from src.models.benchmark_gate import PROTOCOL_NAME, PROTOCOL_VERSION

    report: dict[str, Any] = {
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
        "detectors": ["isolation_forest", "sequence", "fused"],
        "drives": [
            {
                "drive": name,
                "sessions": [name],
                "engine_hours": 1.0,
                "detectors": {d: {"false_alarm_episodes": n} for d, n in episodes.items()},
            }
            for name in ("d1", "d2")
        ],
        "recall": {},
    }
    if cadence is not None:
        report["sequence_forward_every_n_ticks"] = cadence
    return report


def test_the_benchmark_gate_never_diffs_cadence_bound_detectors_across_cadences() -> None:
    """A report at n = 3 against one at n = 1: the sequence and fused cells are
    incomparable (A06.6), and the IF, which the cadence does not touch, is
    still diffed and its regression still caught."""
    from src.models.benchmark_gate import diff_reports, incomparable_detectors

    before = _gate_report(1, {"isolation_forest": 0, "sequence": 0, "fused": 0})
    after = _gate_report(3, {"isolation_forest": 4, "sequence": 9, "fused": 9})

    reasons = incomparable_detectors(before, after)
    assert {"sequence", "sequence_global", "fused"} <= set(reasons)
    assert "isolation_forest" not in reasons
    assert "A06.6" in reasons["sequence"] and "every 1 vs every 3" in reasons["sequence"]
    diff = diff_reports(before, after)
    status = {c["detector"]: c for c in diff["cells"]}
    assert (status["sequence"]["status"], status["sequence"]["regressed"]) == (
        "incomparable",
        False,
    )
    assert status["fused"]["status"] == "incomparable"
    assert status["isolation_forest"]["status"] == "compared"
    assert diff["regressed"] is True  # the IF went from 0 to 4 per engine-hour

    # A report written before the cadence existed scored every window: n = 1.
    legacy = _gate_report(None, {"isolation_forest": 0, "sequence": 0, "fused": 0})
    assert incomparable_detectors(legacy, before) == {}
    worse = _gate_report(None, {"isolation_forest": 0, "sequence": 9, "fused": 0})
    sequence_cell = next(
        c for c in diff_reports(before, worse)["cells"] if c["detector"] == "sequence"
    )
    assert (sequence_cell["status"], sequence_cell["regressed"]) == ("compared", True)
