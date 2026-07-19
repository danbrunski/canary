"""Tests for streaming LSTM inference helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from src.config import CanaryConfig
from src.models.inference import SequenceInferenceEngine, SequenceWindowBuffer
from src.models.lstm_training import train_lstm_autoencoder
from src.models.sequence_dataset import SequenceWindowDataset
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN

pytestmark = pytest.mark.slow  # heavy ML training/bake-off; see pyproject test tiers

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _torch():
    return pytest.importorskip("torch")


def _config(**updates):
    base = CanaryConfig.from_yaml(EXAMPLE_YAML).models.lstm_autoencoder
    defaults = {
        "sequence_length": 4,
        "hidden_size": 6,
        "num_layers": 1,
        "latent_dim": 2,
        "batch_size": 4,
        "learning_rate": 0.01,
        "max_epochs": 1,
        "validation_fraction": 0.0,
        "early_stopping_patience": 2,
        "random_state": 9,
        "threshold_k": 100.0,
    }
    defaults.update(updates)
    return base.model_copy(update=defaults)


def _frame(rows: int = 12) -> pd.DataFrame:
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["s1"] * rows,
            TIMESTAMP_COLUMN: pd.date_range("2026-05-12T00:00:00Z", periods=rows, freq="s"),
            "rpm": [700.0 + (idx % 3) for idx in range(rows)],
            "speed": [float(idx % 4) for idx in range(rows)],
        }
    )


def _dataset(sequence_length: int = 4) -> SequenceWindowDataset:
    return SequenceWindowDataset(
        _frame(),
        sequence_length=sequence_length,
        feature_columns=["rpm", "speed"],
    )


def _trained(**config_updates):
    _torch()
    dataset = _dataset()
    return train_lstm_autoencoder(
        dataset,
        _config(**config_updates),
        trained_at="2026-05-12T12:00:00Z",
        model_version="inference-test",
    )


def test_sequence_buffer_scores_only_after_full_window() -> None:
    _torch()
    engine = SequenceInferenceEngine(_trained(), time_fn=_fake_clock())
    frame = _frame()

    assert engine.observe(frame.iloc[0]) is None
    assert engine.observe(frame.iloc[1]) is None
    assert engine.observe(frame.iloc[2]) is None
    result = engine.observe(frame.iloc[3])

    assert result is not None
    assert result.model_version == "inference-test"
    assert result.session == "s1"
    assert result.window_start_timestamp == "2026-05-12T00:00:00Z"
    assert result.window_end_timestamp == "2026-05-12T00:00:03Z"
    assert set(result.channel_scores) == {"rpm", "speed"}
    assert set(result.channel_thresholds) == {"rpm", "speed"}
    assert result.latency_ms == pytest.approx(100.0)


def test_inference_result_to_dict_is_json_safe() -> None:
    engine = SequenceInferenceEngine(_trained(), time_fn=_fake_clock())
    result = None
    for _, row in _frame().iloc[:4].iterrows():
        result = engine.observe(row)

    assert result is not None
    payload = result.to_dict()
    assert payload["model_version"] == "inference-test"
    assert isinstance(payload["channel_scores"], dict)
    assert isinstance(payload["anomalous_channels"], list)
    # Phase 7.2a evidence keys are JSON-safe (top channels + series + overlays).
    assert isinstance(payload["top_channels"], list)
    assert isinstance(payload["reconstruction_series"], dict)
    assert isinstance(payload["reconstruction_overlays"], dict)


def test_score_window_populates_top_channel_evidence(tmp_path: Path) -> None:
    """7.2a: ``score_window`` emits compact per-channel evidence — top-K
    channels by ratio, the per-timestep reconstruction error series for
    each, and an actual-vs-reconstructed overlay so the dashboard can
    render the divergence without round-tripping through the model."""
    _torch()
    engine = SequenceInferenceEngine(_trained(), time_fn=_fake_clock())
    result = None
    for _, row in _frame().iloc[:4].iterrows():
        result = engine.observe(row)
    assert result is not None
    # Only two feature columns ⇒ the top-K is the full set, ordered by
    # ratio. Larger K than feature count is fine — the slice is bounded.
    assert set(result.top_channels) == {"rpm", "speed"}
    assert set(result.reconstruction_series) == {"rpm", "speed"}
    for series in result.reconstruction_series.values():
        assert len(series) == engine.sequence_length
    assert set(result.reconstruction_overlays) == {"rpm", "speed"}
    for overlay in result.reconstruction_overlays.values():
        assert set(overlay) == {"actual", "reconstructed"}
        assert len(overlay["actual"]) == engine.sequence_length
        assert len(overlay["reconstructed"]) == engine.sequence_length


def test_inference_flags_anomaly_against_low_thresholds() -> None:
    trained = _trained(threshold_k=100.0)
    thresholds = dict(trained.manifest.thresholds or {})
    thresholds["global_error"] = dict(thresholds["global_error"])
    thresholds["global_error"]["threshold"] = 0.0
    thresholds["channel_errors"] = {
        channel: {**distribution, "threshold": 0.0}
        for channel, distribution in dict(thresholds["channel_errors"]).items()
    }
    trained = replace(trained, manifest=replace(trained.manifest, thresholds=thresholds))
    engine = SequenceInferenceEngine(trained, time_fn=_fake_clock())

    result = None
    for _, row in _frame().iloc[:4].iterrows():
        result = engine.observe(row)

    assert result is not None
    assert result.is_anomaly is True
    assert result.global_score > result.global_threshold
    assert result.anomalous_channels


def test_inference_engine_requires_calibrated_thresholds() -> None:
    _torch()  # skip cleanly when torch is unavailable (CI without the `ml` extra)
    trained = train_lstm_autoencoder(
        _dataset(),
        _config(),
        trained_at="2026-05-12T12:00:00Z",
        calibrate_thresholds=False,
    )

    with pytest.raises(ValueError, match="calibrated thresholds"):
        SequenceInferenceEngine(trained)


def test_inference_scores_when_a_channel_is_never_observed() -> None:
    """A whole channel absent for an entire drive (e.g. no GPS fix) is masked,
    and the detector still scores on the channels it has — no NaN, no stall."""
    import math

    engine = SequenceInferenceEngine(_trained(), time_fn=_fake_clock())
    result = None
    for i in range(4):
        # 'speed' never appears; only 'rpm' is present on every tick.
        result = engine.observe(
            {
                SESSION_COLUMN: "s1",
                TIMESTAMP_COLUMN: f"2026-05-12T00:00:0{i}Z",
                "rpm": 700.0 + (i % 3),
            }
        )
    assert result is not None
    assert math.isfinite(result.global_score)
    assert math.isfinite(result.channel_scores["rpm"])
    # The never-observed channel contributes no reconstruction error.
    assert result.channel_scores["speed"] == 0.0


def test_sequence_buffer_masks_missing_or_non_finite_features() -> None:
    """An absent or non-finite feature is masked (unobserved), not a reason to
    drop the row — so a drive lacking a whole sensor family (e.g. no GPS fix)
    still scores on the channels it has, mirroring how the model saw GPS-less
    sessions at training time."""
    import numpy as np

    buffer = SequenceWindowBuffer(feature_columns=("rpm", "speed"), sequence_length=2)

    # Row 1: 'speed' is absent entirely. Accepted; 'speed' masked unobserved.
    assert (
        buffer.append({SESSION_COLUMN: "s1", TIMESTAMP_COLUMN: "2026-05-12T00:00:00Z", "rpm": 1.0})
        is False
    )
    assert buffer.ready is False
    # Row 2: 'speed' present but non-finite. Also masked, and the window fills.
    assert (
        buffer.append(
            {
                SESSION_COLUMN: "s1",
                TIMESTAMP_COLUMN: "2026-05-12T00:00:01Z",
                "rpm": 1.0,
                "speed": float("nan"),
            }
        )
        is True
    )
    assert buffer.ready is True
    mask = buffer.mask_array()
    assert mask.shape == (2, 2)
    assert np.array_equal(mask[:, 0], np.ones(2, dtype=mask.dtype))  # rpm observed
    assert np.array_equal(mask[:, 1], np.zeros(2, dtype=mask.dtype))  # speed masked


def test_sequence_buffer_resets_on_fully_unobserved_row() -> None:
    """A tick with no finite feature at all is a genuine gap → restart the
    window (the only remaining reset besides a session change)."""
    buffer = SequenceWindowBuffer(feature_columns=("rpm", "speed"), sequence_length=2)

    full = {SESSION_COLUMN: "s1", "rpm": 1.0, "speed": 2.0}
    assert buffer.append({TIMESTAMP_COLUMN: "2026-05-12T00:00:00Z", **full}) is False
    # Fully unobserved tick clears the partial fill.
    assert (
        buffer.append(
            {
                SESSION_COLUMN: "s1",
                TIMESTAMP_COLUMN: "2026-05-12T00:00:01Z",
                "rpm": float("nan"),
                "speed": float("nan"),
            }
        )
        is False
    )
    assert buffer.ready is False
    # Cleared: it now takes two fresh rows to become ready again.
    base = {"rpm": 1.0, "speed": 2.0}
    assert (
        buffer.append({SESSION_COLUMN: "s1", TIMESTAMP_COLUMN: "2026-05-12T00:00:02Z", **base})
        is False
    )
    assert (
        buffer.append({SESSION_COLUMN: "s1", TIMESTAMP_COLUMN: "2026-05-12T00:00:03Z", **base})
        is True
    )


def test_sequence_buffer_derives_mask_and_delta_from_gap_flags() -> None:
    """The GRU-D serve buffer derives the observed-mask from ``¬<channel>__gap``
    and accumulates Δt across ticks (reset on observed, +interval on stale),
    mirroring how the offline dataset builds mask/Δt."""
    import numpy as np

    buffer = SequenceWindowBuffer(feature_columns=("rpm", "speed"), sequence_length=3)
    # rpm: fresh, fresh, stale.  speed: no gap column ⇒ always observed.
    rows = [
        {
            SESSION_COLUMN: "s",
            TIMESTAMP_COLUMN: "2026-05-12T00:00:00Z",
            "rpm": 1.0,
            "speed": 2.0,
            "rpm__gap": False,
        },
        {
            SESSION_COLUMN: "s",
            TIMESTAMP_COLUMN: "2026-05-12T00:00:01Z",
            "rpm": 1.0,
            "speed": 2.0,
            "rpm__gap": False,
        },
        {
            SESSION_COLUMN: "s",
            TIMESTAMP_COLUMN: "2026-05-12T00:00:02Z",
            "rpm": 1.0,
            "speed": 2.0,
            "rpm__gap": True,
        },
    ]
    for row in rows:
        ready = buffer.append(row)
    assert ready is True

    mask = buffer.mask_array()
    delta = buffer.delta_array()
    # rpm (col 0): observed, observed, stale.
    assert mask[:, 0].tolist() == [1.0, 1.0, 0.0]
    # speed (col 1): always observed (no gap column present).
    assert mask[:, 1].tolist() == [1.0, 1.0, 1.0]
    # rpm Δt: 0 while fresh, then 1 s on the stale tick.
    assert delta[:, 0].tolist() == [0.0, 0.0, 1.0]
    assert delta[:, 1].tolist() == [0.0, 0.0, 0.0]
    assert np.asarray(buffer.window_array()).shape == (3, 2)


def test_sequence_buffer_resets_on_session_change() -> None:
    buffer = SequenceWindowBuffer(feature_columns=("rpm", "speed"), sequence_length=2)
    row = {
        TIMESTAMP_COLUMN: "2026-05-12T00:00:00Z",
        "rpm": 1.0,
        "speed": 2.0,
    }

    assert buffer.append({**row, SESSION_COLUMN: "s1"}) is False
    assert buffer.append({**row, SESSION_COLUMN: "s2"}) is False
    assert buffer.append({**row, SESSION_COLUMN: "s2"}) is True
    start, end, session = buffer.metadata()
    assert start == "2026-05-12T00:00:00Z"
    assert end == "2026-05-12T00:00:00Z"
    assert session == "s2"


def test_score_window_applies_scaler_before_forward_pass() -> None:
    """Inference must apply the manifest's scaler so reconstruction errors
    are in the same units the threshold was calibrated against.

    Pins the May 2026 LSTM bug — degenerate fit because training saw raw OBD
    values, threshold calibrated on raw-value reconstruction errors, but
    inference at the time did not scale and produced score=88 against
    threshold=171,895. New code scales identically at train and inference.
    """
    import numpy as np

    trained = _trained()
    assert trained.manifest.scaler is not None
    engine = SequenceInferenceEngine(trained)
    # Materialized scaler attributes exist and are populated for the
    # feature columns in order.
    assert engine._scaler_means is not None
    assert engine._scaler_stds is not None
    assert engine._scaler_means.shape == (len(engine.feature_columns),)
    assert engine._scaler_stds.shape == (len(engine.feature_columns),)
    # Per-column means roughly match the training data (mean ≈ 700 for rpm).
    assert 698.0 < float(engine._scaler_means[0]) < 702.0
    # Per-column std is non-zero and used as the divisor (not floored to 1).
    assert engine._scaler_stds[0] > 0.0

    # End-to-end: feeding raw values (close to the training distribution)
    # produces a small reconstruction error.
    raw_window = np.array([[700.0, 1.0]] * engine.sequence_length, dtype=np.float32)
    result = engine.score_window(
        raw_window,
        timestamp="2026-05-20T00:00:00Z",
        session="s",
        window_start_timestamp="2026-05-20T00:00:00Z",
        window_end_timestamp="2026-05-20T00:00:01Z",
    )
    # On a window inside the training distribution, the global reconstruction
    # error should be in the same single-digit / fractional range as the
    # calibrated threshold — not the four-digit raw-value scale the
    # May 2026 artifact produced.
    assert result.global_score < 100.0


def test_score_window_masked_score_ignores_stale_cells() -> None:
    """The serve-time global score is a masked mean — a stale (mask=0) cell with
    a wildly off value must not move the score, matching the masked thresholds
    the model was calibrated against."""
    import numpy as np

    engine = SequenceInferenceEngine(_trained())
    window = np.array([[700.0, 1.0]] * engine.sequence_length, dtype=np.float32)
    all_fresh = np.ones_like(window)
    delta = np.zeros_like(window)
    baseline = engine.score_window(
        window,
        mask=all_fresh,
        delta=delta,
        timestamp="t",
        session="s",
        window_start_timestamp="t0",
        window_end_timestamp="t1",
    )

    # Corrupt one cell's value but mark it stale (mask=0): the masked score must
    # be unchanged because the corrupted cell doesn't contribute.
    corrupted = window.copy()
    corrupted[0, 0] = 1e6
    partial_mask = all_fresh.copy()
    partial_mask[0, 0] = 0.0
    masked = engine.score_window(
        corrupted,
        mask=partial_mask,
        delta=delta,
        timestamp="t",
        session="s",
        window_start_timestamp="t0",
        window_end_timestamp="t1",
    )
    # The stale corrupted cell is excluded; the masked global stays small.
    assert masked.global_score < 100.0
    assert masked.channel_scores["rpm"] < baseline.channel_scores["rpm"] + 1.0


def test_score_window_validates_shape() -> None:
    import numpy as np

    engine = SequenceInferenceEngine(_trained())

    with pytest.raises(ValueError, match="window shape"):
        engine.score_window(
            np.zeros((3, 2), dtype="float32"),
            timestamp="t",
            session="s",
            window_start_timestamp="t0",
            window_end_timestamp="t1",
        )


def _fake_clock():
    values = iter([1.0, 1.1, 2.0, 2.1, 3.0, 3.1])
    return lambda: next(values)


def test_sequence_inference_suppress_channels_silences_alert_from_noise_channel() -> None:
    """A channel listed in ``suppress_channels`` has its mask forced to 0 for
    the whole window. Its reconstruction error then no longer contributes to
    the masked global score, so a wildly OOD value in a *suppressed* channel
    can't fire an alert — exactly the knob we need for fuel-level/HVAC noise."""
    import math

    import numpy as np

    _torch()
    trained = _trained()
    seq_len = trained.manifest.schema["sequence_length"]

    # rpm fits the training distribution (~700); speed is wildly OOD.
    # Without suppression the speed channel drives a large reconstruction
    # error → the masked global score blows past the calibrated threshold.
    window = np.zeros((seq_len, 2), dtype=np.float32)
    window[:, 0] = 700.0  # rpm — normal
    window[:, 1] = 9999.0  # speed — extreme OOD value

    common = dict(
        timestamp="2026-05-12T12:00:00Z",
        session="s1",
        window_start_timestamp="2026-05-12T12:00:00Z",
        window_end_timestamp="2026-05-12T12:00:03Z",
    )

    plain = SequenceInferenceEngine(_trained())
    r_plain = plain.score_window(window, **common)

    suppressed = SequenceInferenceEngine(_trained(), suppress_channels=("speed",))
    r_supp = suppressed.score_window(window, **common)

    # Sanity: both scored a finite global score, same shape of result.
    assert math.isfinite(r_plain.global_score)
    assert math.isfinite(r_supp.global_score)
    # The suppressed channel contributes zero to both the per-channel
    # evidence (score=0, not in anomalous_channels) and the global score.
    assert r_supp.channel_scores["speed"] == 0.0
    assert "speed" not in r_supp.anomalous_channels
    # The OOD perturbation that fired the unsuppressed engine no longer fires
    # the suppressed engine — same window, same model, just the mask knob.
    assert r_plain.global_score > r_supp.global_score
    assert r_plain.is_anomaly is True
    assert r_supp.is_anomaly is False


def test_sequence_inference_suppress_channels_silently_ignores_unknown() -> None:
    """An entry that doesn't match any trained feature column is a no-op,
    not a hard failure — typo-resistance and renamed-channel resilience.
    Engine still constructs and scores cleanly."""
    _torch()
    engine = SequenceInferenceEngine(_trained(), suppress_channels=("not_a_real_channel",))
    assert engine.suppressed_channels == ()
