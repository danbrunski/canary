"""The served sequence-detector scoring rule, written once (R04.2).

A window's verdict is:

1. the **masked global MAE** over the *scored* channels -- every channel minus
   the configured ``suppress_channels`` (A01.5), the channels calibration never
   observed (A02.12) and an ``engine_response`` artifact's context inputs
   (A03.2) -- against the window's **operating-mode band**, falling back to the
   global band when the mode has none;
2. OR the **worst family score**: per physical signal family (a channel and its
   rolling siblings, :func:`family_index`), the largest per-channel masked MAE
   divided by the family's calibrated band, against the calibrated
   ``family_max_score`` band.

Serve (``SequenceInferenceEngine.score_window``, one window), the evaluation
harness (``BatchedSequenceScorer``, many windows) and the false-positive
benchmark all call :meth:`SequenceScoringRule.score_batch`, and calibration
builds its family distributions over the same :func:`family_index`, so the
four can no longer disagree about what the daemon runs. Pure numpy: nothing
here imports torch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from src.columns import base_channel

if TYPE_CHECKING:
    # Type-only: calibration groups families with :func:`family_index`.
    from src.models.sequence_calibration import SequenceThresholdCalibration

_EPS = float(np.finfo(np.float64).eps)


def family_index(columns: Sequence[str]) -> dict[str, list[int]]:
    """Column indexes by physical signal family, in column order.

    The family of a column is its base channel: ``obd:RPM``, ``obd:RPM__mean``
    and ``obd:RPM__roc`` are one family. Calibration and every scorer group by
    this function, so a family key cannot differ between the two.
    """
    families: dict[str, list[int]] = {}
    for index, column in enumerate(columns):
        families.setdefault(base_channel(column), []).append(index)
    return families


@dataclass(frozen=True)
class ScoreExclusions:
    """The columns an artifact keeps out of the score, by reason.

    The encoder still receives every column; these are zeroed only in the
    score mask, the family path and the evidence.
    """

    #: Configured ``suppress_channels``, expanded to rolling siblings (A01.5).
    suppressed: tuple[str, ...]
    #: Never (or too rarely) observed in calibration: threshold not positive
    #: (A02.12).
    uncalibrated: tuple[str, ...]
    #: ``engine_response`` context inputs (A03.2): conditioned on, never scored.
    context: tuple[str, ...]

    @property
    def columns(self) -> frozenset[str]:
        return frozenset(self.suppressed) | frozenset(self.uncalibrated) | frozenset(self.context)

    def keep_mask(self, feature_columns: Sequence[str]) -> np.ndarray[Any, np.dtype[np.float32]]:
        """``(F,)`` float32: 1 for a scored column, 0 for an excluded one."""
        excluded = self.columns
        return np.array([column not in excluded for column in feature_columns], dtype=np.float32)


def score_exclusions(
    feature_columns: Sequence[str],
    calibration: SequenceThresholdCalibration,
    schema: Mapping[str, Any],
    suppress_channels: Sequence[str] = (),
) -> ScoreExclusions:
    """Resolve which of an artifact's columns the served rule scores.

    ``suppress_channels`` are base names or columns as configured; names that
    match no column drop out (a typo is a no-op, not a serve crash).
    """
    # Lazy: the feature pipeline pulls in decode and analysis modules.
    from src.models.sequence_feature_set import context_columns_from_schema
    from src.preprocessing.feature_pipeline import expand_suppress_channels

    columns = tuple(feature_columns)
    present = set(columns)
    suppressed = expand_suppress_channels(tuple(suppress_channels), columns)
    context = tuple(c for c in context_columns_from_schema(schema) if c in present)
    context_set = set(context)
    uncalibrated = tuple(
        column
        for column in columns
        if column not in context_set and not calibration.channel_errors[column].threshold > 0
    )
    return ScoreExclusions(suppressed=suppressed, uncalibrated=uncalibrated, context=context)


@dataclass(frozen=True)
class BatchScores:
    """The served rule's outputs for ``B`` windows."""

    #: ``(B,)`` masked MAE over the scored cells (0 where none was scored).
    global_score: np.ndarray[Any, np.dtype[np.float64]]
    #: ``(B,)`` the band each window's global score is compared against.
    global_threshold: np.ndarray[Any, np.dtype[np.float64]]
    #: ``(B, F)`` per-channel masked MAE (0 for a channel with no scored cell).
    channel: np.ndarray[Any, np.dtype[np.float64]]
    #: Families with a positive calibrated band, in :func:`family_index` order;
    #: empty when the family path is off.
    family_names: tuple[str, ...]
    #: ``(B, len(family_names))`` worst member score / the family band.
    family_scores: np.ndarray[Any, np.dtype[np.float64]]
    #: ``(B,)`` the worst family score, NaN when the family path is off.
    family_max: np.ndarray[Any, np.dtype[np.float64]]
    #: The calibrated ``family_max_score`` band, ``None`` when the path is off.
    family_threshold: float | None
    global_anomaly: np.ndarray[Any, np.dtype[np.bool_]]
    family_anomaly: np.ndarray[Any, np.dtype[np.bool_]]

    @property
    def verdict(self) -> np.ndarray[Any, np.dtype[np.bool_]]:
        """``(B,)`` the served verdict: global OR family."""
        return self.global_anomaly | self.family_anomaly


class SequenceScoringRule:
    """The served verdict rule over one artifact's calibration.

    Built once per artifact; :meth:`score_batch` is numpy over ``(B, T, F)``
    and costs tens of microseconds for the single window serve scores per tick.
    ``use_family_path=False`` is the global-only ablation (A02.3).
    """

    def __init__(
        self,
        calibration: SequenceThresholdCalibration,
        feature_columns: Sequence[str],
        *,
        use_family_path: bool = True,
    ) -> None:
        self.calibration = calibration
        self.feature_columns = tuple(feature_columns)
        self.families = family_index(self.feature_columns)
        family_errors = calibration.family_errors or {}
        self.family_threshold: float | None = (
            calibration.family_max_score.threshold
            if use_family_path and calibration.family_errors and calibration.family_max_score
            else None
        )
        # A family never observed in calibration (A02.12) has no band to compare.
        scored = [
            (family, np.asarray(indexes, dtype=np.intp), max(distribution.threshold, _EPS))
            for family, indexes in self.families.items()
            if (distribution := family_errors.get(family)) is not None
            and distribution.threshold > 0
        ]
        self._family_names = tuple(name for name, _, _ in scored)
        self._family_bands = np.array([band for _, _, band in scored], dtype=np.float64)
        # Members laid end to end, one run per family, so the per-family max is
        # a single ``np.maximum.reduceat`` rather than a Python loop per family.
        self._family_order = (
            np.concatenate([indexes for _, indexes, _ in scored])
            if scored
            else np.zeros(0, dtype=np.intp)
        )
        self._family_starts = np.cumsum([0] + [len(indexes) for _, indexes, _ in scored[:-1]])

    def score_batch(
        self,
        error: np.ndarray[Any, Any],
        score_mask: np.ndarray[Any, Any],
        modes: Sequence[str | None],
    ) -> BatchScores:
        """Score ``B`` windows.

        ``error`` is the absolute reconstruction error ``(B, T, F)``;
        ``score_mask`` is the observation mask with every excluded column
        zeroed (:meth:`ScoreExclusions.keep_mask`); ``modes`` is each window's
        last-row operating mode (``None`` -> the global band).
        """
        if error.shape != score_mask.shape or error.ndim != 3:
            raise ValueError("error and score_mask must both be (B, T, F)")
        if len(modes) != error.shape[0]:
            raise ValueError("one mode per window is required")
        masked = np.asarray(error * score_mask, dtype=np.float64)
        mask = np.asarray(score_mask, dtype=np.float64)
        cells = mask.sum(axis=(1, 2))
        global_score = np.divide(
            masked.sum(axis=(1, 2)), cells, out=np.zeros_like(cells), where=cells > 0
        )
        counts = mask.sum(axis=1)
        channel = np.divide(masked.sum(axis=1), counts, out=np.zeros_like(counts), where=counts > 0)
        global_threshold = np.array(
            [self.calibration.threshold_for_mode(mode) for mode in modes], dtype=np.float64
        )
        global_anomaly = global_score > global_threshold
        batch = error.shape[0]
        family_names: tuple[str, ...] = ()
        family_scores = np.zeros((batch, 0), dtype=np.float64)
        family_max = np.full(batch, np.nan)
        family_anomaly = np.zeros(batch, dtype=bool)
        if self.family_threshold is not None:
            family_names = self._family_names
            if family_names:
                worst_member = np.maximum.reduceat(
                    channel[:, self._family_order], self._family_starts, axis=1
                )
                family_scores = worst_member / self._family_bands
                family_max = family_scores.max(axis=1)
                family_anomaly = family_max > self.family_threshold
        return BatchScores(
            global_score=global_score,
            global_threshold=global_threshold,
            channel=channel,
            family_names=family_names,
            family_scores=family_scores,
            family_max=family_max,
            family_threshold=self.family_threshold,
            global_anomaly=global_anomaly,
            family_anomaly=family_anomaly,
        )


__all__ = [
    "BatchScores",
    "ScoreExclusions",
    "SequenceScoringRule",
    "family_index",
    "score_exclusions",
]
