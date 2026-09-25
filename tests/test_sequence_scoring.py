"""R04.2 / R04.11 / R04.12: the served sequence-scoring rule, written once.

The kernel is numpy-only, so these run without torch; the engine, the batched
scorer and the false-positive benchmark calling it are covered by their own
files (``test_lstm_inference.py``, ``test_evaluation_detectors.py``,
``test_false_positive_benchmark.py``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from src.models.sequence_calibration import (
    ReconstructionErrorDistribution,
    SequenceThresholdCalibration,
)
from src.models.sequence_scoring import (
    ScoreExclusions,
    SequenceScoringRule,
    family_index,
    score_exclusions,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
COLUMNS = ("obd:RPM", "obd:RPM__mean", "obd:SPEED", "obd:SPEED__roc", "obd:MAF")


def _dist(threshold: float) -> ReconstructionErrorDistribution:
    return ReconstructionErrorDistribution(
        count=50,
        mean=threshold / 2,
        std=threshold / 10,
        minimum=0.0,
        maximum=threshold,
        p50=threshold / 2,
        p90=threshold,
        p95=threshold,
        p99=threshold,
        threshold=threshold,
    )


def _calibration(*, families: bool = True) -> SequenceThresholdCalibration:
    return SequenceThresholdCalibration(
        threshold_k=3.0,
        calibrated_at="2026-09-25T00:00:00Z",
        training_data_hash="sha256:x",
        window_count=100,
        global_error=_dist(1.0),
        channel_errors={
            "obd:RPM": _dist(0.5),
            "obd:RPM__mean": _dist(0.5),
            "obd:SPEED": _dist(0.5),
            "obd:SPEED__roc": _dist(0.5),
            # Never observed in calibration (A02.12).
            "obd:MAF": _dist(0.0),
        },
        per_mode_errors={"idle": _dist(0.25)},
        family_errors=(
            {"obd:RPM": _dist(2.0), "obd:SPEED": _dist(4.0), "obd:MAF": _dist(0.0)}
            if families
            else None
        ),
        family_max_score=_dist(1.5) if families else None,
    )


def test_family_index_groups_rolling_siblings_under_their_channel() -> None:
    assert family_index(COLUMNS) == {"obd:RPM": [0, 1], "obd:SPEED": [2, 3], "obd:MAF": [4]}


def test_threshold_for_mode_falls_back_to_the_global_band() -> None:
    calibration = _calibration()
    assert calibration.threshold_for_mode("idle") == 0.25
    assert calibration.threshold_for_mode("highway") == 1.0
    assert calibration.threshold_for_mode(None) == 1.0


def test_score_batch_applies_the_served_rule() -> None:
    rule = SequenceScoringRule(_calibration(), COLUMNS)
    t = 4
    error = np.zeros((3, t, len(COLUMNS)), dtype=np.float32)
    mask = np.ones_like(error)
    # Window 0: every scored cell 0.5 -> global 0.5, over the idle band only.
    error[0, :, :4] = 0.5
    # Window 1: one RPM sibling at 4.0 -> RPM family 4.0 / 2.0 = 2.0 > 1.5.
    error[1, :, 1] = 4.0
    # Window 2: the uncalibrated channel is huge but excluded from the mask.
    error[2, :, 4] = 100.0
    keep = ScoreExclusions(suppressed=(), uncalibrated=("obd:MAF",), context=()).keep_mask(COLUMNS)
    scores = rule.score_batch(error, mask * keep, ["idle", "highway", None])

    assert scores.global_score == pytest.approx([0.5, 1.0, 0.0])
    assert scores.global_threshold == pytest.approx([0.25, 1.0, 1.0])
    assert list(scores.global_anomaly) == [True, False, False]
    # The uncalibrated family has no band, so it is not scored at all.
    assert scores.family_names == ("obd:RPM", "obd:SPEED")
    assert scores.family_scores[1] == pytest.approx([2.0, 0.0])
    assert list(scores.family_anomaly) == [False, True, False]
    assert list(scores.verdict) == [True, True, False]
    assert scores.channel[2, 4] == 0.0


def test_the_family_path_is_off_without_a_family_calibration() -> None:
    rule = SequenceScoringRule(_calibration(families=False), COLUMNS)
    error = np.full((1, 2, len(COLUMNS)), 9.0)
    scores = rule.score_batch(error, np.ones_like(error), [None])
    assert scores.family_threshold is None
    assert scores.family_names == ()
    assert np.isnan(scores.family_max[0])
    assert not scores.family_anomaly[0]
    ablated = SequenceScoringRule(_calibration(), COLUMNS, use_family_path=False)
    assert ablated.score_batch(error, np.ones_like(error), [None]).family_threshold is None


def test_score_exclusions_resolves_suppress_and_uncalibrated() -> None:
    exclusions = score_exclusions(COLUMNS, _calibration(), {}, ("obd:SPEED", "not-a-column"))
    assert exclusions.suppressed == ("obd:SPEED", "obd:SPEED__roc")
    assert exclusions.uncalibrated == ("obd:MAF",)
    assert exclusions.context == ()
    assert list(exclusions.keep_mask(COLUMNS)) == [1.0, 1.0, 0.0, 0.0, 0.0]


def test_score_batch_refuses_mismatched_shapes() -> None:
    rule = SequenceScoringRule(_calibration(), COLUMNS)
    error = np.zeros((2, 3, len(COLUMNS)))
    with pytest.raises(ValueError, match="one mode per window"):
        rule.score_batch(error, np.ones_like(error), [None])
    with pytest.raises(ValueError, match=r"\(B, T, F\)"):
        rule.score_batch(error, np.ones((2, 3, 1)), [None, None])


def test_serve_imports_nothing_from_the_training_module() -> None:
    """R04.11: the engine's threshold format and scoring rule have torch-free,
    training-free homes; serve pulls in neither the trainer nor torch."""
    code = (
        "import sys\n"
        "import src.models.inference, src.models.sequence_scoring\n"
        "import src.models.sequence_feature_set, src.preprocessing.feature_pipeline\n"
        "import src.models.isolation_forest, src.models.sequence_registry\n"
        "import src.daemon.inference, src.daemon.inference_worker, src.daemon.health\n"
        "import src.control.sequence_detector\n"
        "print('src.models.sequence_training' in sys.modules, 'torch' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.split() == ["False", "False"]


def test_both_backbones_share_one_reconstruction_implementation() -> None:
    """R04.12: one masked-reconstruction implementation for both backbones."""
    pytest.importorskip("torch")
    from src.models.lstm_autoencoder import LSTMAutoencoder
    from src.models.sequence_module import MaskedReconstructionMixin
    from src.models.tcn import TCNAutoencoder

    for cls in (LSTMAutoencoder, TCNAutoencoder):
        assert issubclass(cls, MaskedReconstructionMixin)
        for name in ("forward", "reconstruction_error", "_validate_inputs"):
            assert getattr(cls, name) is getattr(MaskedReconstructionMixin, name)
