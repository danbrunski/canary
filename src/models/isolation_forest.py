"""Isolation Forest baseline model for Phase 2 anomaly detection.

The trainer consumes the output of the preprocessing pipeline:

    resampled wide frame -> operating-mode labels -> rolling features

It fits one sklearn ``IsolationForest`` per operating mode, persists the
normalizer parameters into a manifest, and exposes a small scoring API for
replay/evaluation code. Scores are oriented so larger values are more
anomalous; sklearn's labels are preserved as ``1`` normal / ``-1`` anomaly.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from src.coerce import optional_str as _optional_str
from src.columns import (
    MODE_COLUMN,
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
    UNKNOWN_MODE,
    base_channel,
    is_flag_column,
)
from src.dataframe_utils import require_columns as _require_columns
from src.models.manifest import (
    manifest_path_for,
    read_artifact,
    validate_model_feature_spec,
    write_artifact,
)
from src.models.sequence_calibration import coerce_timestamp
from src.preprocessing.feature_pipeline import build_feature_spec
from src.preprocessing.normalization import ZScoreScaler
from src.timefmt import format_timestamp as _format_timestamp
from src.trainability import source_exclusion_reason

log = logging.getLogger(__name__)

ARTIFACT_FORMAT_VERSION = 1
MODEL_TYPE = "isolation_forest"
DEFAULT_RANDOM_STATE = 42
MODEL_MODE_COLUMN = "isolation_forest_model_mode"
SCORE_COLUMN = "isolation_forest_score"
LABEL_COLUMN = "isolation_forest_label"
MISSING_FEATURES_COLUMN = "isolation_forest_missing_features"
#: Fraction of the chosen model's trained features that were NaN on this row.
#: Recorded for every scored row (not just withheld ones) so partial
#: degradation is observable instead of collapsing to a single bool.
MISSING_FEATURE_FRACTION_COLUMN = "isolation_forest_missing_feature_fraction"
#: How much per-row missingness ``score`` will impute through rather than
#: withhold. Above this the row is withheld (flagged, score=inf) and the
#: daemon's fail-safe suppresses the emit.
#:
#: Requiring a *fully* populated row — the original rule — is unsatisfiable on
#: real telemetry: OBD PIDs arrive round-robin at 0.1-0.5 Hz against a 1 Hz
#: resample grid, so on the 2026-07-26 corpus (26,843 rows) exactly 0 rows had
#: every trained feature present. The IF therefore self-suppressed on 100% of
#: ticks, which is the ``missing_feature_rate: 1.0`` observed on the Pi.
#:
#: Missing cells are imputed to the scoring mode's own training median
#: (``IsolationForestManifest.mode_feature_medians``, A04.7). They used to be
#: imputed to 0 in the scaler's z-space, i.e. the mean *pooled over every
#: mode*, which the 2026-09-20 artifact placed a median 9.4 mode-standard-
#: deviations from idle's own centre: imputing 5 % of features raised the mean
#: score in 3 of 4 modes. Imputation does not push a verdict toward "normal";
#: a per-mode median merely moves it least. Artifacts written before the
#: medians were recorded still impute the pooled mean (there is nothing else
#: to impute from); the next retrain replaces them.
#: Imputing at all is only honest for a small minority of the vector, hence a
#: low ceiling:
#: measured against the coverage-filtered feature set, 14.5% of rows need no
#: imputation at all and 48.4% fall within 5%. Raising this much further would
#: start scoring rows that are mostly imputed mean, i.e. blind in the opposite
#: direction (always "normal") — the failure mode this ceiling exists to avoid.
MAX_MISSING_FEATURE_FRACTION = 0.05
#: Phase 7.2 — per-mode calibrated decision boundary + verdict. ``score``
#: stays the raw larger-is-worse value; these add the held-out-calibrated
#: threshold and the boolean it produces. ``threshold`` is ``NaN`` and
#: ``anomaly`` is ``NA`` for a mode with no calibrated threshold (callers
#: then fall back to the legacy contamination label).
CALIBRATED_THRESHOLD_COLUMN = "isolation_forest_calibrated_threshold"
CALIBRATED_ANOMALY_COLUMN = "isolation_forest_calibrated_anomaly"

#: Below this many held-out rows a per-mode calibration is not trustworthy;
#: the mode falls back to the legacy contamination-label decision.
_MIN_CALIBRATION_SAMPLES = 10

#: A feature must be non-NaN in at least this fraction of a mode's rows to join
#: that mode's feature set. Excludes foreign-mode deviation surfaces that leak a
#: few rows across a boundary via the rolling window (which would otherwise empty
#: the mode in the ``dropna``). The populated/leaked split is wide (~0.95 vs
#: ~0.03), so the exact cutoff isn't delicate.
_MIN_MODE_FEATURE_COVERAGE = 0.5


SKIP_TOO_FEW_FIT_ROWS = "too_few_fit_rows"

#: ``unknown`` is not an operating regime, it is the segmenter saying it could
#: not tell — stale inputs, a mode boundary, rows no rule matched. A forest fit
#: on that bucket models "whatever was left over": its population changes with
#: every segmenter fix, its holdout has produced degenerate thresholds before,
#: and the rows are withheld at serve anyway. So no model is fit for it;
#: ``score()`` already withholds rows whose mode has no model. The one
#: exception is a corpus that is *entirely* ``unknown`` with
#: ``allow_unknown_only=true`` — the operator's explicit "I have no
#: segmentation, fit one global model" opt-in, which routes every row through
#: that single model.
SKIP_UNCLASSIFIED_MODE = "unclassified_operating_mode"

#: Held-out exceedances a calibrated quantile must rest on. The threshold is
#: the ``1 - rate`` quantile of ``n`` holdout scores, i.e. it is set by the top
#: ``rate * n`` rows. With fewer than ~10 of them the quantile is decided by
#: one or two rows: at ``n = 154`` a "0.1 %" target is 0.15 of a row, the
#: threshold is simply the holdout maximum, and the achieved rate can only be
#: 0 or 1/154 = 0.65 %. The 2026-09-20 manifest claimed ``target_flag_rate:
#: 0.001`` on holdouts of 91-328 rows. Below this count the target is not
#: estimable; calibration coarsens it (:func:`calibration_flag_rate`) and the
#: manifest records requested vs. used.
MIN_CALIBRATION_EXCEEDANCES = 10

#: Ceiling on how far resolvability may *raise* the calibrated rate. ``10 / n``
#: is unbounded as ``n`` shrinks — a 16-row holdout "resolves" 62 %, and a
#: threshold at the nominal median is not a detector. Past a 5 % per-tick
#: nominal flag rate the persistence gate downstream is carrying the whole
#: decision, so a holdout that cannot resolve even this is not rescued by
#: coarsening further: calibration proceeds at ``max(requested, this)`` and the
#: manifest says the rate used is itself below what the holdout resolves
#: (``resolvable_flag_rate`` > ``target_flag_rate``). An operator-requested
#: rate above the ceiling is honoured as asked.
MAX_RESOLVABILITY_FLAG_RATE = 0.05

#: A04.11 — exceedances closer than this (same session) are one episode. Rows
#: are 1 Hz but every rolling feature spans ``rolling_window_seconds``, so
#: neighbouring exceedances are one excursion seen repeatedly, not independent
#: evidence. On ``134345`` the warmup "top 1 %" was 4 rows forming 1 episode.
#: The spec's own ``preprocessing.rolling_window_seconds`` overrides this.
DEFAULT_EXCEEDANCE_EPISODE_GAP_SECONDS = 30.0

#: A04.4 — a mode's threshold must rest on this many independent physical
#: drives (``src.preprocessing.drives.count_drives``), per mode, not only globally: the
#: 2026-09-20 idle and warmup thresholds came entirely from one session at
#: 71-73 kPa against a training median of 79. Same floor as the sequence
#: detector's (``sequence_registry.MIN_CALIBRATION_SESSIONS``).
MIN_CALIBRATION_DRIVES_PER_MODE = 3

SKIP_TOO_FEW_TRAINING_DRIVES = "too_few_training_drives"
SKIP_TOO_FEW_CALIBRATION_DRIVES = "too_few_calibration_drives"

#: A04.5 — a feature joins a mode only when it is populated in at least this
#: fraction of the mode's training *sessions* (each at the per-mode coverage
#: floor). Pooled coverage hides a whole polling profile that never produces
#: the column: on ``134345`` the IF withheld 99.7 % of city rows because
#: ``derived:STFT_BANK_BALANCE``/``TOTAL_TRIM`` were NaN under its polling
#: although no spec column was absent.
MIN_MODE_SESSION_COVERAGE = 0.75

#: A04.8 — why a loaded mode model is not served.
REFUSED_UNCLASSIFIED_MODEL = (
    "model for the unclassified operating mode: 'unknown' is the segmenter saying it "
    "could not tell, not a regime; its rows are withheld (A04.8)"
)


def resolvable_flag_rate(holdout_rows: int) -> float:
    """Finest flag rate ``holdout_rows`` can estimate: ``10 / n`` (at most 1)."""
    if holdout_rows <= 0:
        return 1.0
    return min(1.0, MIN_CALIBRATION_EXCEEDANCES / holdout_rows)


def calibration_flag_rate(requested: float, holdout_rows: int) -> float:
    """The rate a threshold is calibrated at: ``requested``, coarsened to what
    ``holdout_rows`` can resolve, but never coarsened past
    :data:`MAX_RESOLVABILITY_FLAG_RATE`."""
    return max(requested, min(resolvable_flag_rate(holdout_rows), MAX_RESOLVABILITY_FLAG_RATE))


class IsolationForestConfigLike(Protocol):
    """Subset of ``src.config.IsolationForestConfig`` needed by the trainer."""

    enabled: bool
    n_estimators: int
    contamination: float
    random_state: int
    calibration_holdout_fraction: float
    target_flag_rate: float
    min_samples_per_mode: int
    n_jobs: int


class MissingModelDependencyError(ImportError):
    """Raised when sklearn/joblib is unavailable in the current environment."""


@dataclass(frozen=True)
class ModeTrainingSummary:
    """Manifest record for one per-mode model."""

    mode: str
    n_samples: int
    contamination: float
    sessions: tuple[str, ...]
    start_timestamp: str | None
    end_timestamp: str | None
    #: Phase 7.2 held-out calibration. ``score_threshold`` is ``None`` when
    #: the mode fell back to the legacy contamination-label decision (too
    #: little holdout, or calibration disabled). All optional so manifests
    #: written before 7.2 still load (the 7.3 model-load gate stays happy).
    score_threshold: float | None = None
    #: Spread (std) of the held-out nominal scores — the scale 7.2 fusion
    #: sizes a fired IF source's severity against (the negated
    #: decision-function threshold is a poor scale; see ``models.fusion``).
    score_scale: float | None = None
    holdout_samples: int = 0
    #: The flag rate the threshold was *actually* calibrated at — equal to
    #: ``requested_flag_rate`` only when the holdout could resolve it (see
    #: :func:`resolvable_flag_rate`). Name kept so pre-existing manifests and
    #: readers keep their meaning: "the rate this threshold stands for".
    target_flag_rate: float | None = None
    achieved_flag_rate: float | None = None
    #: What config asked for (``isolation_forest.target_flag_rate``).
    requested_flag_rate: float | None = None
    #: ``True`` when ``holdout_samples`` could not resolve the requested rate,
    #: so ``target_flag_rate`` is a coarser one. ``None`` on manifests written
    #: before this was recorded / uncalibrated modes.
    target_unresolvable: bool | None = None
    #: ``10 / holdout_samples`` — the finest rate this holdout can estimate.
    #: When it exceeds ``target_flag_rate`` the holdout was too small to
    #: resolve even the rate used (see :data:`MAX_RESOLVABILITY_FLAG_RATE`):
    #: that mode's threshold rests on fewer than 10 held-out exceedances.
    resolvable_flag_rate: float | None = None
    #: A04.4 — independent physical drives among this mode's training rows
    #: (``sessions``) and its calibration rows, and which sessions and span
    #: the threshold was cut from. ``None``/empty on older manifests.
    training_drives: int | None = None
    calibration_sessions: tuple[str, ...] = ()
    calibration_start_timestamp: str | None = None
    calibration_end_timestamp: str | None = None
    calibration_drives: int | None = None
    #: Why a threshold resting on fewer than
    #: :data:`MIN_CALIBRATION_DRIVES_PER_MODE` drives was published anyway
    #: (``retrain.py --accept-thin-calibration``). ``None`` = not overridden.
    calibration_override: str | None = None
    #: A04.11 — exceedance *episodes* at the threshold (clustered, not rows),
    #: and the episode rate per hour of calibration rows.
    exceedance_episodes: int | None = None
    achieved_episodes_per_hour: float | None = None
    #: A04.3 — rows at each step from "labelled this mode" to "fit on":
    #: ``mode_rows``, ``complete_without_quarantine`` (what completeness would
    #: have been had no cell been quarantined), ``complete_rows``,
    #: ``lost_to_quarantine``, ``fit_rows``, ``calibration_rows``.
    row_accounting: Mapping[str, int] = field(default_factory=dict)
    #: A04.5 — the base channels this mode's features are computed from: a
    #: polling profile that does not produce all of them blinds the mode.
    required_channels: tuple[str, ...] = ()
    #: A04.10 — seed-to-seed agreement of the flagged calibration rows.
    seed_agreement: Mapping[str, Any] | None = None

    @property
    def calibrated(self) -> bool:
        return self.score_threshold is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "training_drives": self.training_drives,
            "calibration_sessions": list(self.calibration_sessions),
            "calibration_start_timestamp": self.calibration_start_timestamp,
            "calibration_end_timestamp": self.calibration_end_timestamp,
            "calibration_drives": self.calibration_drives,
            "calibration_override": self.calibration_override,
            "exceedance_episodes": self.exceedance_episodes,
            "achieved_episodes_per_hour": self.achieved_episodes_per_hour,
            "row_accounting": dict(self.row_accounting),
            "required_channels": list(self.required_channels),
            "seed_agreement": None if self.seed_agreement is None else dict(self.seed_agreement),
            "mode": self.mode,
            "n_samples": self.n_samples,
            "contamination": self.contamination,
            "sessions": list(self.sessions),
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "score_threshold": self.score_threshold,
            "score_scale": self.score_scale,
            "holdout_samples": self.holdout_samples,
            "target_flag_rate": self.target_flag_rate,
            "achieved_flag_rate": self.achieved_flag_rate,
            "requested_flag_rate": self.requested_flag_rate,
            "target_unresolvable": self.target_unresolvable,
            "resolvable_flag_rate": self.resolvable_flag_rate,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModeTrainingSummary:
        threshold = data.get("score_threshold")
        scale = data.get("score_scale")
        target = data.get("target_flag_rate")
        achieved = data.get("achieved_flag_rate")
        requested = data.get("requested_flag_rate")
        unresolvable = data.get("target_unresolvable")
        resolvable = data.get("resolvable_flag_rate")
        training_drives = data.get("training_drives")
        calibration_drives = data.get("calibration_drives")
        episodes = data.get("exceedance_episodes")
        episode_rate = data.get("achieved_episodes_per_hour")
        agreement = data.get("seed_agreement")
        return cls(
            training_drives=None if training_drives is None else int(training_drives),
            calibration_sessions=tuple(str(s) for s in data.get("calibration_sessions", []) or []),
            calibration_start_timestamp=_optional_str(data.get("calibration_start_timestamp")),
            calibration_end_timestamp=_optional_str(data.get("calibration_end_timestamp")),
            calibration_drives=None if calibration_drives is None else int(calibration_drives),
            calibration_override=_optional_str(data.get("calibration_override")),
            exceedance_episodes=None if episodes is None else int(episodes),
            achieved_episodes_per_hour=None if episode_rate is None else float(episode_rate),
            row_accounting={
                str(k): int(v) for k, v in dict(data.get("row_accounting") or {}).items()
            },
            required_channels=tuple(str(c) for c in data.get("required_channels", []) or []),
            seed_agreement=None if agreement is None else dict(agreement),
            mode=str(data["mode"]),
            n_samples=int(data["n_samples"]),
            contamination=float(data["contamination"]),
            sessions=tuple(str(session) for session in data["sessions"]),
            start_timestamp=_optional_str(data.get("start_timestamp")),
            end_timestamp=_optional_str(data.get("end_timestamp")),
            score_threshold=None if threshold is None else float(threshold),
            score_scale=None if scale is None else float(scale),
            holdout_samples=int(data.get("holdout_samples", 0)),
            target_flag_rate=None if target is None else float(target),
            achieved_flag_rate=None if achieved is None else float(achieved),
            requested_flag_rate=None if requested is None else float(requested),
            target_unresolvable=None if unresolvable is None else bool(unresolvable),
            resolvable_flag_rate=None if resolvable is None else float(resolvable),
        )


@dataclass(frozen=True)
class SkippedModeSummary:
    """Manifest record for a mode training skipped for having too few fit
    rows (Task 2.2) — no model was fit or persisted for it. Distinct from a
    mode absent for lack of any usable feature coverage: this one had rows
    and a feature set, just not enough of either to trust a 200-tree forest."""

    mode: str
    fit_rows: int
    min_samples_required: int
    #: Why no model was fit. ``too_few_fit_rows`` (Task 2.2, and the meaning of
    #: every manifest written before this field) or
    #: ``unclassified_operating_mode`` — see :data:`SKIP_UNCLASSIFIED_MODE`;
    #: for that one ``fit_rows`` is the rows the bucket held, not a shortfall.
    reason: str = SKIP_TOO_FEW_FIT_ROWS
    #: Human-readable specifics (e.g. "1 calibration drive(s); 3 required").
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "fit_rows": self.fit_rows,
            "min_samples_required": self.min_samples_required,
            "reason": self.reason,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SkippedModeSummary:
        return cls(
            mode=str(data["mode"]),
            fit_rows=int(data["fit_rows"]),
            min_samples_required=int(data["min_samples_required"]),
            reason=str(data.get("reason", SKIP_TOO_FEW_FIT_ROWS)),
            detail=_optional_str(data.get("detail")),
        )


@dataclass(frozen=True)
class IsolationForestManifest:
    """JSON-safe metadata for a trained Isolation Forest artifact."""

    artifact_format_version: int
    model_type: str
    model_version: str
    created_at: str
    training_data_hash: str
    training_start_timestamp: str | None
    training_end_timestamp: str | None
    training_sessions: tuple[str, ...]
    input_rows: int
    complete_rows: int
    dropped_missing_feature_rows: int
    hyperparams: Mapping[str, Any]
    scaler: Mapping[str, Any]
    schema: Mapping[str, Any]
    modes: tuple[ModeTrainingSummary, ...]
    #: Modes with a usable feature set but too few fit rows to trust a
    #: trained forest (Task 2.2) — no model was persisted for them. Empty
    #: for manifests written before 2.2, or when every mode cleared the floor.
    skipped_modes: tuple[SkippedModeSummary, ...] = ()
    calibration_sessions: tuple[str, ...] = ()
    #: A02.1 — held-out sessions the post-retrain gate scored, disjoint from
    #: ``calibration_sessions`` (the gate used to score the calibration ones).
    evaluation_sessions: tuple[str, ...] = ()
    #: A04.7 — each mode's training median per feature, in raw units, the
    #: value a tolerated missing cell is imputed to. Empty on older artifacts
    #: (they impute the pooled scaler mean).
    mode_feature_medians: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    #: A04.12 — the calibration actually used, not the configured knobs:
    #: ``source`` (``external_sessions`` / ``internal_tail`` / ``none``), the
    #: effective ``holdout_fraction`` (0 when external sessions replaced the
    #: tail split), and the policies the calibration rows went through (A04.3).
    calibration: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode_feature_medians": {
                mode: dict(values) for mode, values in self.mode_feature_medians.items()
            },
            "calibration": dict(self.calibration),
            "artifact_format_version": self.artifact_format_version,
            "model_type": self.model_type,
            "model_version": self.model_version,
            "created_at": self.created_at,
            "training_data_hash": self.training_data_hash,
            "training_start_timestamp": self.training_start_timestamp,
            "training_end_timestamp": self.training_end_timestamp,
            "training_sessions": list(self.training_sessions),
            "input_rows": self.input_rows,
            "complete_rows": self.complete_rows,
            "dropped_missing_feature_rows": self.dropped_missing_feature_rows,
            "hyperparams": dict(self.hyperparams),
            "scaler": dict(self.scaler),
            "schema": dict(self.schema),
            "modes": [mode.to_dict() for mode in self.modes],
            "skipped_modes": [mode.to_dict() for mode in self.skipped_modes],
            "calibration_sessions": list(self.calibration_sessions),
            "evaluation_sessions": list(self.evaluation_sessions),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> IsolationForestManifest:
        return cls(
            artifact_format_version=int(data["artifact_format_version"]),
            model_type=str(data["model_type"]),
            model_version=str(data["model_version"]),
            created_at=str(data["created_at"]),
            training_data_hash=str(data["training_data_hash"]),
            training_start_timestamp=_optional_str(data.get("training_start_timestamp")),
            training_end_timestamp=_optional_str(data.get("training_end_timestamp")),
            training_sessions=tuple(str(session) for session in data["training_sessions"]),
            input_rows=int(data["input_rows"]),
            complete_rows=int(data["complete_rows"]),
            dropped_missing_feature_rows=int(data["dropped_missing_feature_rows"]),
            hyperparams=dict(data["hyperparams"]),
            scaler=dict(data["scaler"]),
            schema=dict(data["schema"]),
            modes=tuple(ModeTrainingSummary.from_dict(mode) for mode in data["modes"]),
            skipped_modes=tuple(
                SkippedModeSummary.from_dict(mode) for mode in data.get("skipped_modes", [])
            ),
            calibration_sessions=tuple(str(x) for x in data.get("calibration_sessions", [])),
            evaluation_sessions=tuple(str(x) for x in data.get("evaluation_sessions", [])),
            mode_feature_medians={
                str(mode): {str(c): float(v) for c, v in dict(values).items()}
                for mode, values in dict(data.get("mode_feature_medians") or {}).items()
            },
            calibration=dict(data.get("calibration") or {}),
        )


@dataclass(frozen=True)
class TrainedIsolationForest:
    """A fitted per-mode Isolation Forest bundle."""

    models: Mapping[str, Any]
    scaler: ZScoreScaler
    manifest: IsolationForestManifest
    #: A04.8 — mode models present in the artifact but refused at load, with
    #: why. Their rows are withheld like any unmodeled mode's.
    refused_modes: Mapping[str, str] = field(default_factory=dict)
    #: A04.6 — ``{feature: reason}`` for trained features the feature policy
    #: (``FeaturePolicy``) forbids, found at load. Named on ``/status``; the
    #: forest cannot un-learn them, the next retrain drops them.
    policy_violations: Mapping[str, str] = field(default_factory=dict)

    @property
    def feature_columns(self) -> tuple[str, ...]:
        return tuple(str(col) for col in self.manifest.schema["feature_columns"])

    @property
    def mode_column(self) -> str:
        return str(self.manifest.schema["mode_column"])

    @property
    def mode_feature_columns(self) -> dict[str, tuple[str, ...]]:
        """Per-mode feature subsets — each mode's model omits deviation
        surfaces gated to other modes (all-NaN in this mode). Empty for legacy
        artifacts, where callers fall back to the global ``feature_columns``."""
        raw = self.manifest.schema.get("mode_feature_columns", {})
        return {str(mode): tuple(str(c) for c in cols) for mode, cols in raw.items()}

    @property
    def _fallback_key(self) -> str | None:
        """The model every mode routes through, or ``None``.

        Only an artifact *saved* with a single model is a global model (the
        ``allow_unknown_only`` opt-in, or a one-mode corpus). Deciding this on
        the models left after a load-time refusal (A04.8) turned a saved
        ``{city, unknown}`` artifact into a "global" city model that scored
        ``unknown`` and ``koeo`` rows it was never trained on.
        """
        if self.refused_modes or len(self.models) != 1:
            return None
        return next(iter(self.models))

    def _features_for(self, model_key: str) -> tuple[str, ...]:
        """Feature columns the ``model_key`` model was trained on (its mode's
        subset), falling back to the global set for legacy artifacts."""
        return self.mode_feature_columns.get(model_key, self.feature_columns)

    @property
    def score_thresholds(self) -> dict[str, float]:
        """Per-mode held-out-calibrated decision boundaries (7.2).

        Keyed by the model's mode name (the same key ``MODEL_MODE_COLUMN``
        records, so a single-model fallback route still resolves). Modes that
        fell back to the legacy contamination label are absent.
        """
        return {
            mode.mode: mode.score_threshold
            for mode in self.manifest.modes
            if mode.score_threshold is not None and mode.mode in self.models
        }

    @property
    def score_scales(self) -> dict[str, float]:
        """Per-mode held-out score spreads (7.2 fusion magnitude scale).

        Keyed like :attr:`score_thresholds`; modes with a degenerate (zero)
        spread are absent, so fusion falls back to the unscaled fired
        severity for them.
        """
        return {
            mode.mode: mode.score_scale
            for mode in self.manifest.modes
            if mode.score_scale is not None and mode.mode in self.models
        }

    def _imputation_values(self, model_key: str, feats: Sequence[str]) -> np.ndarray[Any, Any]:
        """z-space value each of ``feats`` is imputed to for ``model_key`` (A04.7):
        the mode's own training median, or 0 (the pooled scaler mean) on an
        artifact that did not record medians."""
        return _scaled_medians(
            self.scaler, feats, self.manifest.mode_feature_medians.get(model_key, {})
        )

    def _mode_matrix(
        self, scaled: pd.DataFrame, model_key: str, feats: Sequence[str]
    ) -> np.ndarray[Any, np.dtype[np.float32]]:
        """The rows ``model_key`` scores, tolerated NaNs imputed (A04.7)."""
        return _impute_scaled(
            scaled.loc[:, list(feats)].to_numpy(dtype=np.float64),
            self._imputation_values(model_key, feats),
        )

    def contribution_reference(self, mode: object) -> str | None:
        """What :meth:`feature_contributions` occludes toward for ``mode``:
        ``"mode_median"`` when the artifact recorded that mode's medians
        (A04.7), ``"pooled_mean"`` (the scaler mean over every mode) on an
        artifact that did not — the 2026-09-20 one. ``None`` when no model
        serves the mode."""
        mode_key = str(mode)
        model_key = mode_key if mode_key in self.models else self._fallback_key
        if model_key is None:
            return None
        return "mode_median" if self.manifest.mode_feature_medians.get(model_key) else "pooled_mean"

    def feature_contributions(self, frame: pd.DataFrame, *, top_k: int = 5) -> dict[str, float]:
        """Which of the *scoring model's own* features raised this row's score (A04.2).

        Occlusion: each feature of the mode model that scored the row is, in
        turn, replaced by the imputation value — that mode's median, or the
        pooled scaler mean on an artifact that recorded no medians (see
        :meth:`contribution_reference`) — and all
        variants are scored in one batched ``decision_function`` call. A
        feature's contribution is how much its occlusion *lowers* the score;
        only positive contributions are returned, largest first.

        The previous |z| ranking over the 295-column union named a feature the
        scoring model did not even have on 60 of 62 city rows, and within the
        mode's own features matched the occlusion top-1 on only 0-7 of ~61.
        Returns ``{}`` for a withheld row or a mode with no served model.
        """
        _require_columns(frame, [self.mode_column, *self.feature_columns])
        row = frame.iloc[[0]]
        mode_value = row[self.mode_column].iloc[0]
        mode_key = str(mode_value)
        fallback_key = self._fallback_key
        model_key = mode_key if mode_key in self.models else fallback_key
        if model_key is None:
            return {}
        feats = list(self._features_for(model_key))
        if not feats:
            return {}
        scaled = self.scaler.transform(row)
        if float(scaled[feats].isna().mean(axis=1).iloc[0]) > MAX_MISSING_FEATURE_FRACTION:
            return {}
        base = self._mode_matrix(scaled, model_key, feats)
        variants = np.repeat(base, len(feats), axis=0)
        fill = self._imputation_values(model_key, feats).astype(np.float32)
        diagonal = np.arange(len(feats))
        variants[diagonal, diagonal] = fill
        model = self.models[model_key]
        scores = -model.decision_function(np.vstack([base, variants]))
        effect = float(scores[0]) - scores[1:]
        order = np.argsort(-effect, kind="stable")
        return {
            feats[int(index)]: float(effect[int(index)])
            for index in order[:top_k]
            if effect[int(index)] > 0.0
        }

    def score(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Append Isolation Forest scores and labels to ``frame``.

        Rows with missing feature values cannot be evaluated by sklearn, so
        they are explicitly labeled anomalous. That makes sensor dropouts
        visible to the evaluator while preserving sklearn's normal/anomaly
        label convention for complete rows.
        """
        _require_columns(frame, [self.mode_column, *self.feature_columns])

        # A row whose operating mode has no per-mode model used to crash the
        # whole call. That blanked the live detector whenever the segmenter
        # emitted a mode the trainer never produced (sparse OBD → train/serve
        # skew). Degrade instead: a single-model bundle routes every mode
        # through that one model; otherwise the unmodeled rows are withheld
        # (flagged, not scored) so the daemon's missing-feature fail-safe
        # suppresses the emit while health still records the degraded state —
        # symmetric with the LSTM "wait for a clean window" contract.
        fallback_key = self._fallback_key

        scaled = self.scaler.transform(frame)
        out = frame.copy()
        out[SCORE_COLUMN] = np.nan
        out[LABEL_COLUMN] = pd.Series(pd.NA, index=out.index, dtype="Int64")
        out[MODEL_MODE_COLUMN] = pd.Series(pd.NA, index=out.index, dtype="object")
        out[MISSING_FEATURES_COLUMN] = False
        out[MISSING_FEATURE_FRACTION_COLUMN] = np.nan

        for mode, group in scaled.groupby(self.mode_column, sort=True, dropna=False):
            mode_key = str(mode)
            indexes = group.index
            model_key = mode_key if mode_key in self.models else fallback_key
            # Check missing features against the feature set the chosen model was
            # actually trained on (its mode's subset), not the global union — a
            # deviation surface gated to a different mode is NaN here by design
            # and must not count as a sensor-dropout that suppresses the verdict.
            feats = (
                list(self._features_for(model_key))
                if model_key is not None
                else list(self.feature_columns)
            )
            # Tolerate a *little* missingness instead of demanding a fully
            # populated row. Requiring zero NaN made the detector unable to
            # score at all on real telemetry: OBD arrives round-robin at
            # 0.1-0.5 Hz against a 1 Hz grid, so on the 2026-07-26 corpus
            # 0 of 26,843 rows had every trained feature present, which is
            # exactly the ``missing_feature_rate: 1.0`` the Pi reported. With
            # the coverage-filtered feature set, 14.5% of rows are already
            # complete and 48.4% are within this tolerance — the difference
            # between a detector that never fires and one that works.
            missing_fraction = group[feats].isna().mean(axis=1)
            withheld = missing_fraction > MAX_MISSING_FEATURE_FRACTION
            out.loc[indexes, MISSING_FEATURE_FRACTION_COLUMN] = missing_fraction

            if withheld.any():
                missing_indexes = withheld[withheld].index
                out.loc[missing_indexes, SCORE_COLUMN] = np.inf
                out.loc[missing_indexes, LABEL_COLUMN] = -1
                out.loc[missing_indexes, MODEL_MODE_COLUMN] = mode_key
                out.loc[missing_indexes, MISSING_FEATURES_COLUMN] = True

            complete_indexes = indexes.difference(withheld[withheld].index)
            if complete_indexes.empty:
                continue

            if model_key is None:
                # No model for this mode and no single-model fallback to route
                # through. Withhold rather than crash the tick: flag the rows
                # so the daemon suppresses the emit but health still sees them.
                out.loc[complete_indexes, SCORE_COLUMN] = np.inf
                out.loc[complete_indexes, LABEL_COLUMN] = -1
                out.loc[complete_indexes, MODEL_MODE_COLUMN] = mode_key
                out.loc[complete_indexes, MISSING_FEATURES_COLUMN] = True
                continue

            # Impute the tolerated NaNs to the mode's own median (A04.7; the
            # pooled mean on artifacts that predate it — see
            # MAX_MISSING_FEATURE_FRACTION). sklearn cannot consume NaN at
            # all, so without this the "tolerated" rows would raise.
            x = self._mode_matrix(scaled.loc[complete_indexes], model_key, feats)
            model = self.models[model_key]
            # sklearn's decision_function is positive for inliers and
            # negative for outliers. Negating gives a more natural
            # "higher means worse" anomaly score. ``MODEL_MODE_COLUMN`` records
            # the model actually used, so a fallback route is observable.
            out.loc[complete_indexes, SCORE_COLUMN] = -model.decision_function(x)
            out.loc[complete_indexes, LABEL_COLUMN] = model.predict(x).astype(int)
            out.loc[complete_indexes, MODEL_MODE_COLUMN] = model_key

        # Phase 7.2 calibrated decision. Map each row to the threshold of the
        # model actually used (MODEL_MODE_COLUMN, so the single-model fallback
        # resolves), then verdict = score >= threshold. inf scores (missing /
        # unmodeled) stay anomalous. A mode with no calibrated threshold gets
        # NaN/NA here and the emitter falls back to the legacy label.
        thresholds = self.score_thresholds
        if thresholds:
            thr = out[MODEL_MODE_COLUMN].map(thresholds).astype("float64")
        else:
            thr = pd.Series(np.nan, index=out.index, dtype="float64")
        out[CALIBRATED_THRESHOLD_COLUMN] = thr
        known = thr.notna()
        anomaly = pd.Series(pd.NA, index=out.index, dtype="boolean")
        if bool(known.any()):
            anomaly.loc[known] = out.loc[known, SCORE_COLUMN].to_numpy(dtype="float64") >= thr.loc[
                known
            ].to_numpy(dtype="float64")
        out[CALIBRATED_ANOMALY_COLUMN] = anomaly

        return out

    def save(self, artifact_path: Path, manifest_path: Path | None = None) -> None:
        """Write the joblib artifact and its JSON manifest."""
        save_isolation_forest_model(self, artifact_path, manifest_path=manifest_path)


@dataclass(frozen=True)
class _ModeFitPlan:
    """One mode's feature set + time-ordered fit/calibration-holdout split
    (Task 2.4), computed against the *raw* frame before the scaler is fit —
    NaN positions are unaffected by scaling, so this can run first and hand
    the scaler exactly the rows models will train on."""

    feats: list[str]
    complete_index: pd.Index
    fit_index: pd.Index
    holdout_index: pd.Index
    n_holdout: int
    calibrate: bool
    #: Rows of the external calibration frame this mode calibrates on
    #: (``None`` = internal tail holdout, or no calibration).
    external_index: pd.Index | None = None
    training_drives: int | None = None
    calibration_drives: int | None = None
    calibration_override: str | None = None
    row_accounting: Mapping[str, int] = field(default_factory=dict)


def exceedance_episodes(
    flags: np.ndarray[Any, Any] | Sequence[bool],
    timestamps: pd.Series | Sequence[Any],
    sessions: pd.Series | Sequence[Any] | None = None,
    *,
    gap_seconds: float = DEFAULT_EXCEEDANCE_EPISODE_GAP_SECONDS,
) -> int:
    """Clustered exceedances (A04.11): flagged rows no more than
    ``gap_seconds`` apart, within one session, are one episode.

    A 1 Hz row carrying 30 s rolling features is not an independent draw;
    counting rows as exceedances let four adjacent warmup rows pass for a
    calibrated top 1 %.
    """
    mask = np.asarray(flags, dtype=bool)
    if not mask.any():
        return 0
    stamps = pd.to_datetime(pd.Series(list(timestamps)), utc=True)[mask]
    names = (
        pd.Series(["" for _ in range(int(mask.sum()))])
        if sessions is None
        else pd.Series(list(sessions)).astype(str)[mask]
    )
    ordered = pd.DataFrame({"session": names.to_numpy(), "t": stamps.to_numpy()}).sort_values(
        ["session", "t"], kind="stable"
    )
    step = ordered["t"].diff().dt.total_seconds().to_numpy()
    new_session = (ordered["session"] != ordered["session"].shift()).to_numpy()
    starts = new_session | ~(step <= gap_seconds)
    return int(starts.sum())


def _drive_count(rows: pd.DataFrame, *, timestamp_column: str, session_column: str) -> int:
    """Independent physical drives among ``rows`` (:func:`src.preprocessing.drives.count_drives`:
    the frame's stamped physical drive, captures and in-recording pauses
    included; rows from one drive either side of a capture count once)."""
    from src.preprocessing.drives import count_drives

    return count_drives(rows, session_column=session_column, timestamp_column=timestamp_column)


def _session_covered_features(
    group: pd.DataFrame, candidates: Sequence[str], *, session_column: str
) -> list[str]:
    """Candidates populated in at least :data:`MIN_MODE_SESSION_COVERAGE` of the
    mode's sessions (A04.5). Sessions with fewer than
    :data:`_MIN_CALIBRATION_SAMPLES` rows in the mode do not vote, unless no
    session has that many."""
    if not candidates or session_column not in group.columns:
        return list(candidates)
    counts = group[session_column].astype(str).value_counts()
    voting = counts[counts >= _MIN_CALIBRATION_SAMPLES].index
    if len(voting) == 0:
        voting = counts.index
    rows = group[group[session_column].astype(str).isin(voting)]
    coverage = rows[list(candidates)].notna().groupby(rows[session_column].astype(str)).mean()
    share = (coverage >= _MIN_MODE_FEATURE_COVERAGE).mean(axis=0)
    return [c for c in candidates if float(share[c]) >= MIN_MODE_SESSION_COVERAGE]


def _calibration_split(
    frame: pd.DataFrame,
    complete_index: pd.Index,
    *,
    timestamp_column: str,
    holdout_fraction: float,
) -> tuple[pd.Index, pd.Index, int, bool]:
    """Task 3.5: the per-mode calibration split, extracted out of
    ``fit_isolation_forest``'s per-mode loop (Phase 7.2).

    Time-orders one mode's complete rows and reserves the tail as a
    held-out calibration set the model never trains on, so the per-mode
    score threshold is set on genuinely unseen nominal data rather than the
    training distribution. Falls back to fitting on all rows (legacy
    contamination label, no calibration) when there isn't enough holdout.
    Returns ``(fit_index, holdout_index, n_holdout, calibrate)``.
    """
    ordered_index = (
        pd.to_datetime(frame.loc[complete_index, timestamp_column], utc=True)
        .sort_values(kind="stable")
        .index
    )
    n = len(ordered_index)
    n_holdout = int(np.floor(n * holdout_fraction)) if holdout_fraction > 0 else 0
    calibrate = (n - n_holdout) >= 1 and n_holdout >= _MIN_CALIBRATION_SAMPLES
    if calibrate:
        return ordered_index[: n - n_holdout], ordered_index[n - n_holdout :], n_holdout, True
    return ordered_index, ordered_index[:0], 0, False


@dataclass(frozen=True)
class _Calibration:
    threshold: float
    scale: float | None
    #: Row rate the threshold stands for (``k / n`` of the calibration rows).
    used_flag_rate: float
    achieved_flag_rate: float
    episodes: int
    episodes_per_hour: float | None
    #: Clustered exceedances at the *requested* rate fell short of
    #: :data:`MIN_CALIBRATION_EXCEEDANCES` (or its row count did).
    unresolvable: bool
    scores: np.ndarray[Any, Any]


def _calibrate_holdout_threshold(
    model: Any,
    holdout_x: np.ndarray[Any, Any],
    *,
    requested_flag_rate: float,
    timestamps: pd.Series,
    sessions: pd.Series,
    gap_seconds: float,
) -> _Calibration:
    """The per-mode threshold, resting on clustered exceedances (A04.11).

    Scores the never-trained-on calibration rows and cuts the threshold at
    the requested rate, first coarsened to what the row count resolves
    (:func:`calibration_flag_rate`). If the exceedances there form fewer than
    :data:`MIN_CALIBRATION_EXCEEDANCES` *episodes*
    (:func:`exceedance_episodes`), the threshold is lowered one row at a time
    until they do — never past :data:`MAX_RESOLVABILITY_FLAG_RATE` (or the
    requested rate, if the operator asked for more). ``scale`` is the
    calibration score spread (``None`` when degenerate).
    """
    scores = -model.decision_function(holdout_x)
    n = int(scores.size)
    stamps = pd.to_datetime(timestamps, utc=True).reset_index(drop=True)
    names = sessions.astype(str).reset_index(drop=True)

    def episodes_at(threshold: float) -> int:
        return exceedance_episodes(scores >= threshold, stamps, names, gap_seconds=gap_seconds)

    requested_threshold = float(np.quantile(scores, 1.0 - requested_flag_rate))
    unresolvable = (
        requested_flag_rate < resolvable_flag_rate(n)
        or episodes_at(requested_threshold) < MIN_CALIBRATION_EXCEEDANCES
    )
    rate = calibration_flag_rate(requested_flag_rate, n)
    threshold = float(np.quantile(scores, 1.0 - rate))
    episodes = episodes_at(threshold)
    coarsened = False
    if episodes < MIN_CALIBRATION_EXCEEDANCES:
        # Lower the threshold one more flagged row at a time, up to the
        # ceiling rate, until the exceedances form enough episodes.
        ceiling = max(requested_flag_rate, MAX_RESOLVABILITY_FLAG_RATE)
        ordered = np.sort(scores)[::-1]
        start = int(np.sum(scores >= threshold)) + 1
        stop = int(np.floor(ceiling * n))
        for k in range(start, stop + 1):
            candidate = float(ordered[k - 1])
            found = episodes_at(candidate)
            threshold, episodes, coarsened = candidate, found, True
            if found >= MIN_CALIBRATION_EXCEEDANCES:
                break
    flagged = scores >= threshold
    achieved = float(np.mean(flagged)) if n else 0.0
    seconds = (
        float(
            pd.DataFrame({"s": names, "t": stamps})
            .groupby("s")["t"]
            .agg(lambda t: (t.max() - t.min()).total_seconds() + 1.0)
            .sum()
        )
        if n
        else 0.0
    )
    spread = float(np.std(scores))
    return _Calibration(
        threshold=threshold,
        scale=spread if spread > 0.0 else None,
        used_flag_rate=achieved if coarsened else rate,
        achieved_flag_rate=achieved,
        episodes=episodes,
        episodes_per_hour=None if seconds <= 0 else episodes / (seconds / 3600.0),
        unresolvable=unresolvable,
        scores=scores,
    )


def _seed_agreement(
    isolation_forest_cls: Any,
    x: np.ndarray[Any, Any],
    holdout_x: np.ndarray[Any, Any],
    *,
    primary_scores: np.ndarray[Any, Any],
    flag_rate: float,
    seeds: Sequence[int],
    n_estimators: int,
    contamination: float,
    n_jobs: int,
) -> dict[str, Any]:
    """A04.10 — refit on the same rows with other seeds, flag the same share
    of the calibration rows, and report pairwise Jaccard of the flagged sets.

    With 200 trees the 2026-09-20 highway rows flagged 147-214 rows across
    seeds 0-3 (Jaccard 0.68): which rows a threshold catches was partly a
    coin toss, so the manifest now says how much.
    """
    flagged: list[set[int]] = []

    def top(scores: np.ndarray[Any, Any]) -> set[int]:
        k = max(1, int(round(flag_rate * scores.size)))
        return set(np.argsort(-scores, kind="stable")[:k].tolist())

    flagged.append(top(primary_scores))
    for seed in seeds:
        model = isolation_forest_cls(
            n_estimators=n_estimators,
            contamination=contamination,
            random_state=seed,
            n_jobs=n_jobs,
        )
        model.fit(x)
        flagged.append(top(-model.decision_function(holdout_x)))
    pairs = [
        len(a & b) / len(a | b) for i, a in enumerate(flagged) for b in flagged[i + 1 :] if a | b
    ]
    return {
        "seeds": len(flagged),
        "n_estimators": n_estimators,
        "flag_rate": flag_rate,
        "mean_jaccard": float(np.mean(pairs)) if pairs else None,
        "min_jaccard": float(np.min(pairs)) if pairs else None,
    }


def _jointly_complete_feature_set(
    training_group: pd.DataFrame,
    candidates: Sequence[str],
    *,
    minimum_complete_fraction: float,
    calibration_group: pd.DataFrame | None,
    minimum_calibration_rows: int,
) -> list[str]:
    """Largest useful greedy subset that remains jointly scoreable.

    Per-column coverage is not enough for a row-wise estimator: ten columns
    that are each 80% populated can have almost no rows where all ten coexist.
    Add the densest columns first and retain a candidate only while the joint
    complete-row floor remains satisfied.  When an external holdout exists,
    apply the same check there so training cannot manufacture a schema that
    its own calibration sessions cannot score.
    """
    if not candidates:
        return []
    calibration = calibration_group

    def _coverage(column: str) -> float:
        training = float(training_group[column].notna().mean())
        if calibration is None:
            return training
        held_out = (
            float(calibration[column].notna().mean())
            if column in calibration.columns and len(calibration)
            else 0.0
        )
        return min(training, held_out)

    # Python's sort is stable: equal-coverage columns retain the caller's
    # schema order, preserving deterministic sklearn results for dense data.
    ordered = sorted(candidates, key=_coverage, reverse=True)
    selected: list[str] = []
    for column in ordered:
        proposed = [*selected, column]
        train_complete = int(training_group[proposed].notna().all(axis=1).sum())
        if train_complete / len(training_group) < minimum_complete_fraction:
            continue
        if calibration is not None:
            if column not in calibration.columns:
                continue
            available = [name for name in proposed if name in calibration.columns]
            if len(available) != len(proposed):
                continue
            calibration_complete = int(calibration[proposed].notna().all(axis=1).sum())
            if calibration_complete < minimum_calibration_rows:
                continue
        selected.append(column)
    return selected


def fit_isolation_forest(
    frame: pd.DataFrame,
    config: IsolationForestConfigLike,
    *,
    feature_columns: Iterable[str] | None = None,
    feature_spec: Mapping[str, Any] | None = None,
    mode_column: str = MODE_COLUMN,
    timestamp_column: str = TIMESTAMP_COLUMN,
    session_column: str = SESSION_COLUMN,
    model_version: str | None = None,
    fitted_at: datetime | pd.Timestamp | str | None = None,
    random_state: int | None = None,
    calibration_frame: pd.DataFrame | None = None,
    quarantined_cells: Mapping[str, pd.Index] | None = None,
    calibration_policy: Mapping[str, Any] | None = None,
    calibration_override: str | None = None,
) -> TrainedIsolationForest:
    """Fit one Isolation Forest per operating mode.

    The scaler and models are fit on the same complete rows: any row missing
    one or more selected feature values is dropped from training, and the
    manifest records, per mode, how many rows each step cost
    (``row_accounting``, A04.3) — ``quarantined_cells`` (``{column: index}``,
    from ``retrain.py``'s quarantine screen) lets it say how many of those a
    quarantined cell removed. Modes are skipped, with the reason recorded, when
    they have too few fit rows (Task 2.2), rest on fewer independent training
    drives than ``min_training_drives_per_mode``, or would be calibrated on
    fewer than ``min_calibration_drives_per_mode`` drives (A04.4) — the last
    unless ``calibration_override`` names why that is accepted.
    ``calibration_policy`` is what the calibration rows went through (feedback
    labels, quarantine), recorded in the manifest.

    Phases (R04.9): :func:`_resolve_fit_settings` validates and resolves the
    spec; :func:`_plan_modes` decides each mode's features and split against
    the raw frame; :func:`_fit_scaler` fits the scaler on exactly the rows the
    forests train on; :func:`_fit_mode` fits, calibrates and summarizes one
    mode; :func:`_build_manifest` applies the dropped-fraction gate and writes
    the manifest.
    """
    settings = _resolve_fit_settings(
        frame,
        config,
        feature_columns=feature_columns,
        feature_spec=feature_spec,
        mode_column=mode_column,
        timestamp_column=timestamp_column,
        session_column=session_column,
        random_state=random_state,
        external_calibration=calibration_frame is not None,
    )
    mode_plans, skipped_modes = _plan_modes(
        frame,
        config,
        settings,
        calibration_frame=calibration_frame,
        quarantined_cells=quarantined_cells,
        calibration_override=calibration_override,
    )
    scaler = _fit_scaler(frame, mode_plans, settings.selected_features)
    isolation_forest_cls = _isolation_forest_cls()
    models: dict[str, Any] = {}
    mode_feature_medians: dict[str, dict[str, float]] = {}
    modes: list[ModeTrainingSummary] = []
    for mode, plan in mode_plans.items():
        models[mode], mode_feature_medians[mode], summary = _fit_mode(
            frame,
            mode,
            plan,
            scaler,
            config,
            settings,
            calibration_frame=calibration_frame,
            isolation_forest_cls=isolation_forest_cls,
        )
        modes.append(summary)
    manifest = _build_manifest(
        frame,
        config,
        settings,
        mode_plans=mode_plans,
        modes=modes,
        skipped_modes=skipped_modes,
        mode_feature_medians=mode_feature_medians,
        scaler=scaler,
        calibration_frame=calibration_frame,
        calibration_policy=calibration_policy,
        calibration_override=calibration_override,
        model_version=model_version,
        fitted_at=fitted_at,
    )
    return TrainedIsolationForest(models=models, scaler=scaler, manifest=manifest)


@dataclass(frozen=True)
class _FitSettings:
    """The validated, resolved inputs every phase of the IF fit reads."""

    selected_features: tuple[str, ...]
    schema: dict[str, Any]
    mode_column: str
    timestamp_column: str
    session_column: str
    seed: int
    holdout_fraction: float
    effective_holdout_fraction: float
    target_flag_rate: float
    min_samples_per_mode: int
    min_training_drives: int
    min_calibration_drives: int
    stability_seeds: int
    episode_gap: float
    minimum_complete_fraction: float
    n_jobs: int


def _resolve_fit_settings(
    frame: pd.DataFrame,
    config: IsolationForestConfigLike,
    *,
    feature_columns: Iterable[str] | None,
    feature_spec: Mapping[str, Any] | None,
    mode_column: str,
    timestamp_column: str,
    session_column: str,
    random_state: int | None,
    external_calibration: bool,
) -> _FitSettings:
    """Phase 1: validate the config and frame, resolve features and the spec."""
    _validate_config(config)
    _require_columns(frame, [mode_column, timestamp_column, session_column])
    selected_features = _resolve_feature_columns(frame, feature_columns, mode_column)
    _require_columns(frame, selected_features)
    schema = (
        dict(feature_spec)
        if feature_spec is not None
        else build_feature_spec(frame, selected_features)
    )
    validate_model_feature_spec(schema, context="IsolationForest training feature spec")
    holdout_fraction, target_flag_rate = _calibration_params(config)
    return _FitSettings(
        selected_features=selected_features,
        schema=schema,
        mode_column=mode_column,
        timestamp_column=timestamp_column,
        session_column=session_column,
        seed=config.random_state if random_state is None else random_state,
        holdout_fraction=holdout_fraction,
        effective_holdout_fraction=0.0 if external_calibration else holdout_fraction,
        target_flag_rate=target_flag_rate,
        min_samples_per_mode=_min_samples_per_mode(config),
        min_training_drives=int(getattr(config, "min_training_drives_per_mode", 0)),
        min_calibration_drives=int(getattr(config, "min_calibration_drives_per_mode", 0)),
        stability_seeds=int(getattr(config, "stability_seeds", 0)),
        episode_gap=_episode_gap_seconds(schema),
        minimum_complete_fraction=1.0 - float(getattr(config, "max_dropped_row_fraction", 1.0)),
        n_jobs=int(getattr(config, "n_jobs", 1)),
    )


def _plan_modes(
    frame: pd.DataFrame,
    config: IsolationForestConfigLike,
    settings: _FitSettings,
    *,
    calibration_frame: pd.DataFrame | None,
    quarantined_cells: Mapping[str, pd.Index] | None,
    calibration_override: str | None,
) -> tuple[dict[str, _ModeFitPlan], list[SkippedModeSummary]]:
    """Phase 2: each mode's feature subset and time-ordered fit/holdout split.

    Runs against the *raw* (unscaled) frame. NaN positions don't depend on
    scaling, so this can run before the scaler exists — which is exactly what
    Task 2.4 needs: the scaler is fit on the union of these ``fit_index`` rows
    only, not the calibration holdout tail (the previous leak) and not modes
    2.2 goes on to skip. Raises when no mode earns a model.
    """
    mode_column = settings.mode_column
    mode_plans: dict[str, _ModeFitPlan] = {}
    skipped_modes: list[SkippedModeSummary] = []
    present_modes = {str(value) for value in frame[mode_column].unique()}
    unknown_is_the_whole_corpus = (
        # ``is not False``: config-likes predating the knob keep their old
        # behaviour; the real config defaults it to False.
        present_modes == {UNKNOWN_MODE} and getattr(config, "allow_unknown_only", None) is not False
    )
    for raw_mode, group in frame.groupby(mode_column, sort=True, dropna=False):
        mode = str(raw_mode)
        if len(group) == 0:
            continue
        if mode == UNKNOWN_MODE and not unknown_is_the_whole_corpus:
            # See SKIP_UNCLASSIFIED_MODE: never a regime, never a model.
            skipped_modes.append(
                SkippedModeSummary(
                    mode=mode,
                    fit_rows=len(group),
                    min_samples_required=settings.min_samples_per_mode,
                    reason=SKIP_UNCLASSIFIED_MODE,
                )
            )
            log.info(
                "not fitting an IsolationForest for operating_mode=%r (%d rows): "
                "unclassified rows are not a regime",
                mode,
                len(group),
            )
            continue
        outcome = _plan_mode(
            frame,
            mode,
            group,
            settings,
            calibration_frame=calibration_frame,
            quarantined_cells=quarantined_cells,
            calibration_override=calibration_override,
        )
        if isinstance(outcome, SkippedModeSummary):
            skipped_modes.append(outcome)
        elif outcome is not None:
            mode_plans[mode] = outcome

    if not mode_plans and present_modes == {UNKNOWN_MODE}:
        raise ValueError(
            "IsolationForest would train only operating_mode='unknown'; fix segmentation "
            "coverage or explicitly set allow_unknown_only=true"
        )
    if not mode_plans:
        reasons = sorted({f"{s.mode}: {s.reason}" for s in skipped_modes})
        raise ValueError(
            "no per-mode IsolationForest models were trained"
            + (f" (skipped {', '.join(reasons)})" if reasons else "")
        )
    return mode_plans, skipped_modes


def _plan_mode(
    frame: pd.DataFrame,
    mode: str,
    group: pd.DataFrame,
    settings: _FitSettings,
    *,
    calibration_frame: pd.DataFrame | None,
    quarantined_cells: Mapping[str, pd.Index] | None,
    calibration_override: str | None,
) -> _ModeFitPlan | SkippedModeSummary | None:
    """One mode's plan, the recorded reason it is skipped, or ``None`` when it
    silently has nothing to fit (no jointly complete features or rows)."""
    mode_column = settings.mode_column
    timestamp_column = settings.timestamp_column
    session_column = settings.session_column
    min_samples_per_mode = settings.min_samples_per_mode
    # Per-mode feature set: only the columns densely populated in this
    # mode. A deviation surface gated to a *different* operating_mode is
    # all-NaN here, so it's excluded — rather than dropping every row of
    # this mode (the global dropna that silently blanked the detector,
    # because each mode-gated surface is NaN outside its own mode).
    # ``.any()`` is too permissive, though: a foreign-mode deviation leaks
    # into a handful of this mode's rows through the rolling window at a
    # mode boundary (its 30 s mean/std carries a few rows past the
    # transition). That sparse non-NaN qualified the column, then the
    # ``dropna`` below emptied the whole mode on it (e.g. warmup deviations,
    # ~3 % populated in idle, zeroed the idle model entirely). Require a real
    # coverage fraction so only genuinely-present features count — and
    # (A04.5) require it in most of the mode's *sessions*, so a column one
    # polling profile never produces cannot blind the mode wherever that
    # profile runs.
    eligible_feats = _session_covered_features(
        group,
        [
            c
            for c in settings.selected_features
            if float(group[c].notna().mean()) >= _MIN_MODE_FEATURE_COVERAGE
        ],
        session_column=session_column,
    )
    calibration_group = None
    if calibration_frame is not None and mode_column in calibration_frame.columns:
        calibration_group = calibration_frame[calibration_frame[mode_column].astype(str) == mode]
    mode_feats = _jointly_complete_feature_set(
        group,
        eligible_feats,
        minimum_complete_fraction=settings.minimum_complete_fraction,
        calibration_group=calibration_group,
        minimum_calibration_rows=_MIN_CALIBRATION_SAMPLES,
    )
    if not mode_feats:
        return None
    mode_complete = group.dropna(subset=mode_feats)
    if mode_complete.empty:
        return None
    row_accounting = _row_accounting(group, mode_feats, len(mode_complete), quarantined_cells)
    if row_accounting.get("lost_to_quarantine", 0):
        log.warning(
            "IsolationForest mode %r: quarantined cells removed %d of %d otherwise-complete "
            "training rows (a row with any NaN feature is not fit on)",
            mode,
            row_accounting["lost_to_quarantine"],
            row_accounting["complete_without_quarantine"],
            extra={"mode": mode, "row_accounting": dict(row_accounting)},
        )

    fit_index, holdout_index, n_holdout, calibrate = _calibration_split(
        frame,
        mode_complete.index,
        timestamp_column=timestamp_column,
        holdout_fraction=settings.effective_holdout_fraction,
    )

    if len(fit_index) == 0:
        return None

    # Task 2.2: a forest fit on a handful of rows is a meaningless model
    # that still emits with full sklearn-default confidence. Skip it
    # (no model persisted) instead, and record why in the manifest.
    if len(fit_index) < min_samples_per_mode:
        return SkippedModeSummary(
            mode=mode,
            fit_rows=len(fit_index),
            min_samples_required=min_samples_per_mode,
        )

    # A04.4: per-mode provenance, enforced. Joint-completeness selection
    # keeps only the sessions that polled every selected channel, so a
    # mode can quietly narrow to one day's drives while the header still
    # names 19 sessions.
    training_drives = _drive_count(
        frame.loc[fit_index],
        timestamp_column=timestamp_column,
        session_column=session_column,
    )
    if training_drives < settings.min_training_drives:
        log.warning(
            "IsolationForest mode %r refused: fit rows come from %d physical drive(s), "
            "min_training_drives_per_mode is %d",
            mode,
            training_drives,
            settings.min_training_drives,
        )
        return SkippedModeSummary(
            mode=mode,
            fit_rows=len(fit_index),
            min_samples_required=min_samples_per_mode,
            reason=SKIP_TOO_FEW_TRAINING_DRIVES,
            detail=f"{training_drives} drive(s); {settings.min_training_drives} required",
        )

    external_index: pd.Index | None = None
    if calibration_frame is not None:
        candidate = (
            calibration_group if calibration_group is not None else calibration_frame.iloc[0:0]
        )
        # Calibration must mirror serve-time scoring. Production tolerates
        # a small missing-feature fraction and imputes those cells;
        # calibrating only on fully complete rows made the threshold too
        # optimistic for the majority round-robin OBD rows.
        if len(candidate):
            missing_fraction = candidate[mode_feats].isna().mean(axis=1)
            external_index = candidate.index[
                (missing_fraction <= MAX_MISSING_FEATURE_FRACTION).to_numpy()
            ]
        else:
            external_index = candidate.index
        if len(external_index) < _MIN_CALIBRATION_SAMPLES:
            raise ValueError(
                f"held-out calibration session has only {len(external_index)} scoreable "
                f"rows for mode {mode!r}; need at least {_MIN_CALIBRATION_SAMPLES}"
            )
        calibrate = True
    calibration_drives: int | None = None
    override: str | None = None
    if calibrate:
        if external_index is not None and calibration_frame is not None:
            rows = calibration_frame.loc[external_index]
        else:
            rows = frame.loc[holdout_index]
        calibration_drives = (
            _drive_count(rows, timestamp_column=timestamp_column, session_column=session_column)
            if session_column in rows.columns
            else 0
        )
        if calibration_drives < settings.min_calibration_drives:
            if not calibration_override:
                log.warning(
                    "IsolationForest mode %r refused: its threshold would rest on %d "
                    "physical drive(s); %d are required per mode "
                    "(--accept-thin-calibration REASON overrides)",
                    mode,
                    calibration_drives,
                    settings.min_calibration_drives,
                )
                return SkippedModeSummary(
                    mode=mode,
                    fit_rows=len(fit_index),
                    min_samples_required=min_samples_per_mode,
                    reason=SKIP_TOO_FEW_CALIBRATION_DRIVES,
                    detail=(
                        f"{calibration_drives} calibration drive(s); "
                        f"{settings.min_calibration_drives} required"
                    ),
                )
            override = calibration_override
            log.warning(
                "IsolationForest mode %r calibrates on %d drive(s) (< %d); published anyway: %s",
                mode,
                calibration_drives,
                settings.min_calibration_drives,
                calibration_override,
            )

    return _ModeFitPlan(
        feats=mode_feats,
        complete_index=mode_complete.index,
        fit_index=fit_index,
        holdout_index=holdout_index,
        n_holdout=n_holdout,
        calibrate=calibrate,
        external_index=external_index,
        training_drives=training_drives,
        calibration_drives=calibration_drives,
        calibration_override=override,
        row_accounting={
            **row_accounting,
            "fit_rows": len(fit_index),
            "calibration_rows": (
                len(external_index) if external_index is not None else int(n_holdout)
            ),
        },
    )


def _fit_scaler(
    frame: pd.DataFrame, mode_plans: Mapping[str, _ModeFitPlan], selected_features: Sequence[str]
) -> ZScoreScaler:
    """Phase 3 (Task 2.4): the scaler, fit on exactly the rows the forests
    train on — every kept mode's fit rows, none of their calibration-holdout
    rows, and none of a skipped mode's rows.

    Still NaN-aware per column, so a deviation surface gated to one mode gets
    its stats from that mode's own fit rows. A feature column exclusive to a
    mode 2.2's floor skips entirely (e.g. a ``deviation:*`` surface mode-gated
    to that one mode — see ``feature_pipeline``'s mode-gated-feature
    docstrings) is NaN across the *whole* surviving-mode row union: no
    surviving mode's ``plan.feats`` lists it, so it never has real values
    outside the now-skipped mode. Fitting over ``selected_features``
    unconditionally used to crash on that column (``ZScoreScaler.fit`` rejects
    all-NaN columns) even though no surviving model ever reads it. Fit only on
    columns some surviving mode actually uses, then backfill identity
    placeholder stats (mean 0 / std 1) for the rest — dead parameters no model
    consults, kept only so ``scaler.columns`` still matches the manifest's full
    ``feature_columns`` schema.
    """
    scaler_fit_rows = sorted({idx for plan in mode_plans.values() for idx in plan.fit_index})
    used_columns = [
        column
        for column in selected_features
        if any(column in plan.feats for plan in mode_plans.values())
    ]
    scaler = ZScoreScaler.fit(frame.loc[scaler_fit_rows], columns=used_columns)
    unused_columns = [column for column in selected_features if column not in used_columns]
    if unused_columns:
        scaler = ZScoreScaler(
            means={**scaler.means, **dict.fromkeys(unused_columns, 0.0)},
            stds={**scaler.stds, **dict.fromkeys(unused_columns, 1.0)},
            columns=tuple(selected_features),
            std_floor=scaler.std_floor,
        )
    return scaler


def _fit_mode(
    frame: pd.DataFrame,
    mode: str,
    plan: _ModeFitPlan,
    scaler: ZScoreScaler,
    config: IsolationForestConfigLike,
    settings: _FitSettings,
    *,
    calibration_frame: pd.DataFrame | None,
    isolation_forest_cls: Any,
) -> tuple[Any, dict[str, float], ModeTrainingSummary]:
    """Phase 4: one mode's forest, its imputation medians, holdout
    calibration, optional seed agreement, and its manifest summary."""
    timestamp_column = settings.timestamp_column
    session_column = settings.session_column
    target_flag_rate = settings.target_flag_rate
    contamination = _contamination_for_mode(config, mode)
    x = _scaled_array(frame.loc[plan.fit_index, plan.feats], scaler, plan.feats)
    model = isolation_forest_cls(
        n_estimators=config.n_estimators,
        contamination=contamination,
        random_state=settings.seed,
        n_jobs=settings.n_jobs,
    )
    model.fit(x)
    # A04.7: the value a tolerated missing cell is imputed to at serve.
    medians = frame.loc[plan.fit_index, plan.feats].median()
    feature_medians = {c: float(medians[c]) for c in plan.feats}

    calibration_rows: pd.DataFrame | None = None
    if plan.calibrate:
        calibration_rows = (
            calibration_frame.loc[plan.external_index]
            if plan.external_index is not None and calibration_frame is not None
            else frame.loc[plan.holdout_index]
        )
    calibration_count = 0 if calibration_rows is None else len(calibration_rows)
    resolvable = resolvable_flag_rate(calibration_count)
    calibration: _Calibration | None = None
    agreement: dict[str, Any] | None = None
    if calibration_rows is not None:
        holdout_x = _impute_scaled(
            _scaled_array(calibration_rows.loc[:, plan.feats], scaler, plan.feats),
            _scaled_medians(scaler, plan.feats, feature_medians),
        )
        calibration = _calibrate_holdout_threshold(
            model,
            holdout_x,
            requested_flag_rate=target_flag_rate,
            timestamps=calibration_rows[timestamp_column],
            sessions=(
                calibration_rows[session_column]
                if session_column in calibration_rows.columns
                else pd.Series([""] * len(calibration_rows))
            ),
            gap_seconds=settings.episode_gap,
        )
        if calibration.unresolvable:
            log.warning(
                "IsolationForest mode %r: target_flag_rate %.4g is not resolvable on %d "
                "held-out rows (finest estimable ~%.4g = %d/n, and %d clustered "
                "exceedance episode(s) are needed); calibrating at %.4g with %d episode(s)%s",
                mode,
                target_flag_rate,
                calibration_count,
                resolvable,
                MIN_CALIBRATION_EXCEEDANCES,
                MIN_CALIBRATION_EXCEEDANCES,
                calibration.used_flag_rate,
                calibration.episodes,
                " — still fewer episodes than required; it needs more held-out drives"
                if calibration.episodes < MIN_CALIBRATION_EXCEEDANCES
                else "",
                extra={
                    "mode": mode,
                    "holdout_samples": calibration_count,
                    "requested_flag_rate": target_flag_rate,
                    "used_flag_rate": calibration.used_flag_rate,
                    "resolvable_flag_rate": resolvable,
                    "exceedance_episodes": calibration.episodes,
                },
            )
        if settings.stability_seeds > 0:
            agreement = _seed_agreement(
                isolation_forest_cls,
                x,
                holdout_x,
                primary_scores=calibration.scores,
                flag_rate=calibration.achieved_flag_rate,
                seeds=[settings.seed + offset for offset in range(1, settings.stability_seeds + 1)],
                n_estimators=config.n_estimators,
                contamination=contamination,
                n_jobs=settings.n_jobs,
            )

    calibrated_on = (
        calibration_rows
        if calibration_rows is not None and session_column in calibration_rows.columns
        else None
    )
    summary = _mode_summary(
        mode=mode,
        contamination=contamination,
        frame=frame.loc[plan.complete_index],
        session_column=session_column,
        timestamp_column=timestamp_column,
        score_threshold=None if calibration is None else calibration.threshold,
        score_scale=None if calibration is None else calibration.scale,
        holdout_samples=calibration_count,
        target_flag_rate=None if calibration is None else calibration.used_flag_rate,
        achieved_flag_rate=None if calibration is None else calibration.achieved_flag_rate,
        requested_flag_rate=None if calibration is None else target_flag_rate,
        target_unresolvable=None if calibration is None else calibration.unresolvable,
        resolvable_flag_rate=None if calibration is None else resolvable,
        training_drives=plan.training_drives,
        calibration_sessions=(
            () if calibrated_on is None else _unique_strings(calibrated_on[session_column])
        ),
        calibration_start_timestamp=(
            None if calibrated_on is None else _min_timestamp(calibrated_on[timestamp_column])
        ),
        calibration_end_timestamp=(
            None if calibrated_on is None else _max_timestamp(calibrated_on[timestamp_column])
        ),
        calibration_drives=plan.calibration_drives,
        calibration_override=plan.calibration_override,
        exceedance_episodes=None if calibration is None else calibration.episodes,
        achieved_episodes_per_hour=(None if calibration is None else calibration.episodes_per_hour),
        row_accounting=plan.row_accounting,
        required_channels=tuple(dict.fromkeys(base_channel(c) for c in plan.feats)),
        seed_agreement=agreement,
    )
    return model, feature_medians, summary


def _build_manifest(
    frame: pd.DataFrame,
    config: IsolationForestConfigLike,
    settings: _FitSettings,
    *,
    mode_plans: Mapping[str, _ModeFitPlan],
    modes: Sequence[ModeTrainingSummary],
    skipped_modes: Sequence[SkippedModeSummary],
    mode_feature_medians: Mapping[str, dict[str, float]],
    scaler: ZScoreScaler,
    calibration_frame: pd.DataFrame | None,
    calibration_policy: Mapping[str, Any] | None,
    calibration_override: str | None,
    model_version: str | None,
    fitted_at: datetime | pd.Timestamp | str | None,
) -> IsolationForestManifest:
    """Phase 5: the dropped-fraction gate, then the manifest."""
    mode_column = settings.mode_column
    session_column = settings.session_column
    timestamp_column = settings.timestamp_column
    # The rows actually used across all per-mode models (disjoint by mode) —
    # for the manifest's data hash, span, sessions, and complete-row count.
    used_index = [idx for plan in mode_plans.values() for idx in plan.complete_index.tolist()]
    complete = frame.loc[sorted(set(used_index))]
    # Missing-row safety is evaluated only within modes that earned a model.
    # Rows belonging to a mode below ``min_samples_per_mode`` are deliberately
    # represented by ``skipped_modes``; counting them again as feature-
    # missing rows conflates evidence coverage with joint schema completeness.
    modeled_rows = frame[mode_column].astype(str).isin(mode_plans)
    modeled_row_count = int(modeled_rows.sum())
    dropped_fraction = (
        (modeled_row_count - len(complete)) / modeled_row_count if modeled_row_count else 1.0
    )
    maximum_dropped = float(getattr(config, "max_dropped_row_fraction", 1.0))
    if dropped_fraction > maximum_dropped:
        raise ValueError(
            f"IsolationForest dropped {dropped_fraction:.1%} of input rows; maximum is "
            f"{maximum_dropped:.1%}"
        )

    calibration_source = (
        "external_sessions"
        if calibration_frame is not None
        else ("internal_tail" if any(p.calibrate for p in mode_plans.values()) else "none")
    )
    created_at = _format_timestamp(coerce_timestamp(fitted_at))
    version = model_version or f"isolation_forest_{created_at.replace(':', '')}"
    return IsolationForestManifest(
        artifact_format_version=ARTIFACT_FORMAT_VERSION,
        model_type=MODEL_TYPE,
        model_version=version,
        created_at=created_at,
        training_data_hash=_training_data_hash(
            complete,
            feature_columns=settings.selected_features,
            mode_column=mode_column,
            session_column=session_column,
            timestamp_column=timestamp_column,
        ),
        training_start_timestamp=_min_timestamp(complete[timestamp_column]),
        training_end_timestamp=_max_timestamp(complete[timestamp_column]),
        training_sessions=_unique_strings(complete[session_column]),
        input_rows=len(frame),
        complete_rows=len(complete),
        dropped_missing_feature_rows=len(frame) - len(complete),
        hyperparams={
            "n_estimators": config.n_estimators,
            "contamination": config.contamination,
            "per_mode_contamination": _per_mode_contamination(config),
            "random_state": settings.seed,
            # A04.12: the fraction actually held out (0 when external
            # calibration sessions replaced the tail split), beside the knob.
            "calibration_holdout_fraction": settings.effective_holdout_fraction,
            "configured_calibration_holdout_fraction": settings.holdout_fraction,
            "target_flag_rate": settings.target_flag_rate,
            "min_samples_per_mode": settings.min_samples_per_mode,
            "min_training_drives_per_mode": settings.min_training_drives,
            "min_calibration_drives_per_mode": settings.min_calibration_drives,
            "stability_seeds": settings.stability_seeds,
            "n_jobs": settings.n_jobs,
        },
        scaler=scaler.to_dict(),
        schema={
            **settings.schema,
            "mode_column": mode_column,
            "mode_feature_columns": {mode: plan.feats for mode, plan in mode_plans.items()},
            "timestamp_column": timestamp_column,
            "session_column": session_column,
            "score_column": SCORE_COLUMN,
            "label_column": LABEL_COLUMN,
        },
        modes=tuple(modes),
        skipped_modes=tuple(skipped_modes),
        calibration_sessions=(
            ()
            if calibration_frame is None or session_column not in calibration_frame.columns
            else _unique_strings(calibration_frame[session_column])
        ),
        mode_feature_medians=dict(mode_feature_medians),
        calibration={
            "source": calibration_source,
            "holdout_fraction": settings.effective_holdout_fraction,
            "configured_holdout_fraction": settings.holdout_fraction,
            "episode_gap_seconds": settings.episode_gap,
            "min_exceedance_episodes": MIN_CALIBRATION_EXCEEDANCES,
            "min_calibration_drives_per_mode": settings.min_calibration_drives,
            "override": calibration_override,
            "policy": dict(calibration_policy)
            if calibration_policy is not None
            else {"note": "not recorded by the caller"},
        },
    )


def _episode_gap_seconds(schema: Mapping[str, Any]) -> float:
    """The rolling window the spec's features were built with (A04.11)."""
    preprocessing = schema.get("preprocessing")
    if isinstance(preprocessing, Mapping):
        window = preprocessing.get("rolling_window_seconds")
        if window is not None:
            return float(window)
    return DEFAULT_EXCEEDANCE_EPISODE_GAP_SECONDS


def _row_accounting(
    group: pd.DataFrame,
    feats: Sequence[str],
    complete_rows: int,
    quarantined_cells: Mapping[str, pd.Index] | None,
) -> dict[str, int]:
    """Rows a mode keeps at each step (A04.3). A quarantined cell is NaN, and a
    row with any NaN feature is not fit on, so 4.5 % of cells once cost 71.6 %
    of rows; this makes that loss a number in the manifest."""
    present = group[list(feats)].notna()
    if quarantined_cells:
        for column in feats:
            cells = quarantined_cells.get(column)
            if cells is not None and len(cells):
                present[column] |= group.index.isin(cells)
    without_quarantine = int(present.all(axis=1).sum())
    return {
        "mode_rows": len(group),
        "complete_without_quarantine": without_quarantine,
        "complete_rows": complete_rows,
        "lost_to_quarantine": max(0, without_quarantine - complete_rows),
    }


def _scaled_medians(
    scaler: ZScoreScaler, columns: Sequence[str], medians: Mapping[str, float]
) -> np.ndarray[Any, np.dtype[np.float64]]:
    """``medians`` in the scaler's z-space; 0 (the pooled mean) where absent."""
    out = np.zeros(len(columns), dtype=np.float64)
    for index, column in enumerate(columns):
        median = medians.get(column)
        if median is None or not np.isfinite(median):
            continue
        out[index] = (median - scaler.means[column]) / scaler.denominator(column)
    return out


def _impute_scaled(
    values: np.ndarray[Any, Any], fill: np.ndarray[Any, Any]
) -> np.ndarray[Any, np.dtype[np.float32]]:
    out = np.asarray(values, dtype=np.float64).copy()
    missing = ~np.isfinite(out)
    if missing.any():
        out[missing] = np.broadcast_to(fill, out.shape)[missing]
    return out.astype(np.float32)


def _scaled_array(
    frame: pd.DataFrame, scaler: ZScoreScaler, columns: Sequence[str]
) -> np.ndarray[Any, np.dtype[np.float32]]:
    """Scale only the matrix a mode consumes, avoiding a full-frame copy."""
    values = frame.loc[:, list(columns)].to_numpy(dtype=np.float32, copy=True)
    means, denoms = scaler.vectors(columns)
    # float32 arithmetic, as the forests were always fit: the fit matrix is the
    # dominant training allocation. Serve scales in float64 (``transform``);
    # both use the scaler's one denominator rule (R04.15).
    values -= means.astype(np.float32)
    values /= denoms.astype(np.float32)
    return values


def save_isolation_forest_model(
    trained: TrainedIsolationForest,
    artifact_path: Path,
    *,
    manifest_path: Path | None = None,
) -> None:
    """Persist a trained model bundle with joblib and a JSON manifest."""
    joblib = _joblib_module()
    manifest = trained.manifest.to_dict()
    payload = {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "model_type": MODEL_TYPE,
        "models": dict(trained.models),
        "scaler": trained.scaler.to_dict(),
        "manifest": manifest,
    }
    write_artifact(
        artifact_path,
        lambda path: joblib.dump(payload, path),
        manifest,
        manifest_path=manifest_path,
    )


def load_isolation_forest_model(
    artifact_path: Path, *, feature_policy: Any | None = None
) -> TrainedIsolationForest:
    """Load a model previously written by ``save_isolation_forest_model``.

    Two load-time refusals (A04.6/A04.8), both recorded on the returned bundle
    and published to ``/status`` (``feature_spec_health.load_audit_snapshot``):

    * a model for the unclassified ``unknown`` mode is not served when the
      artifact has any other model (its rows are withheld instead). The
      2026-09-20 artifact carried one over 20 context features, thresholded
      on 91 rows, and scoring routed to it;
    * with a ``feature_policy`` (``feature_pipeline.FeaturePolicy``), every
      trained feature the policy forbids is named in ``policy_violations``.
      The forest cannot un-learn them; they stop training at the next retrain.
    """
    joblib = _joblib_module()
    payload = read_artifact(
        artifact_path,
        joblib.load,
        model_type=MODEL_TYPE,
        expected_format_version=ARTIFACT_FORMAT_VERSION,
    )

    manifest = IsolationForestManifest.from_dict(payload["manifest"])
    scaler = ZScoreScaler.from_dict(payload["scaler"])
    feature_columns = validate_model_feature_spec(
        manifest.schema,
        context=f"IsolationForest manifest {artifact_path}",
    )
    _validate_loaded_artifact(
        payload=payload,
        manifest=manifest,
        scaler=scaler,
        feature_columns=feature_columns,
    )
    models = dict(payload["models"])
    refused = refused_mode_models(models)
    for mode, reason in refused.items():
        models.pop(mode)
        log.warning(
            "IsolationForest %s: not serving the %r mode model: %s",
            artifact_path,
            mode,
            reason,
        )
    if not any(mode.calibrated and mode.mode in models for mode in manifest.modes):
        raise ValueError(
            "IsolationForest artifact has no servable calibrated mode model once refused "
            f"modes {sorted(refused)} are removed"
        )
    violations = (
        feature_policy_violations(feature_columns, feature_policy)
        if feature_policy is not None
        else {}
    )
    if violations:
        log.error(
            "IsolationForest %s was trained on %d feature(s) its feature policy forbids; "
            "they stay in this artifact until the next retrain (see /status "
            "detectors.isolation_forest.load_audit)",
            artifact_path,
            len(violations),
            extra={"policy_violations": dict(sorted(violations.items()))},
        )
    trained = TrainedIsolationForest(
        models=models,
        scaler=scaler,
        manifest=manifest,
        refused_modes=refused,
        policy_violations=violations,
    )
    from src.models.feature_spec_health import record_load_audit

    record_load_audit(
        MODEL_TYPE,
        model_version=manifest.model_version,
        refused_modes=refused,
        policy_violations=violations,
        policy_checked=feature_policy is not None,
        mode_feature_columns={mode: trained._features_for(mode) for mode in models},
    )
    return trained


def refused_mode_models(models: Mapping[str, Any]) -> dict[str, str]:
    """``{mode: reason}`` for mode models an artifact must not serve (A04.8).

    ``unknown`` is refused whenever another model exists; a lone ``unknown``
    model is the operator's explicit ``allow_unknown_only`` global model.
    """
    names = {str(mode) for mode in models}
    if UNKNOWN_MODE in names and len(names) > 1:
        return {UNKNOWN_MODE: REFUSED_UNCLASSIFIED_MODEL}
    return {}


def feature_policy_violations(feature_columns: Sequence[str], policy: Any) -> dict[str, str]:
    """``{feature: reason}`` for trained features ``policy`` forbids (A04.6).

    The policy (``FeaturePolicy.exclusion_reason``) was enforced only at
    training (``a4a58a4``); 83 of the 2026-09-20 artifact's 295 features came
    from ``model_feature: false`` bindings and 44 from ``hypothesized`` ones.
    """
    out: dict[str, str] = {}
    for column in feature_columns:
        reason = policy.exclusion_reason(base_channel(str(column)))
        if reason is not None:
            out[str(column)] = str(reason)
    return out


def default_manifest_path(artifact_path: Path) -> Path:
    """Return the conventional JSON manifest path for an artifact."""
    return manifest_path_for(artifact_path)


def _validate_config(config: IsolationForestConfigLike) -> None:
    if not config.enabled:
        raise ValueError("isolation forest training requested but config is disabled")
    if config.n_estimators <= 0:
        raise ValueError("n_estimators must be positive")
    _validate_contamination(config.contamination, label="contamination")
    for mode, contamination in _per_mode_contamination(config).items():
        _validate_contamination(contamination, label=f"per_mode_contamination[{mode!r}]")
    holdout_fraction, target_flag_rate = _calibration_params(config)
    if not 0.0 <= holdout_fraction < 1.0:
        raise ValueError("calibration_holdout_fraction must be in [0, 1)")
    if not 0.0 < target_flag_rate <= 0.5:
        raise ValueError("target_flag_rate must be in (0, 0.5]")
    if _min_samples_per_mode(config) < 0:
        raise ValueError("min_samples_per_mode must be non-negative")


def _validate_contamination(value: float, *, label: str) -> None:
    if not 0.0 < value <= 0.5:
        raise ValueError(f"{label} must be in (0, 0.5]")


def _calibration_params(config: IsolationForestConfigLike) -> tuple[float, float]:
    """Read the Phase 7.2 calibration knobs defensively.

    Configs predating 7.2 (and the lightweight test doubles) don't carry
    these attributes; absent ⇒ calibration disabled (``holdout_fraction``
    ``0.0``), which preserves the exact legacy fit-on-all behavior.
    """
    holdout_fraction = float(getattr(config, "calibration_holdout_fraction", 0.0))
    target_flag_rate = float(getattr(config, "target_flag_rate", 0.001))
    return holdout_fraction, target_flag_rate


def _min_samples_per_mode(config: IsolationForestConfigLike) -> int:
    """Read the Task 2.2 fit-row floor defensively — same pattern as
    :func:`_calibration_params`. Configs/test doubles predating 2.2 don't
    carry ``min_samples_per_mode``; absence means "no floor" (``0``),
    preserving the exact legacy behavior of training on however few rows a
    mode has. ``src.config.IsolationForestConfig`` defaults it to 100.
    """
    return int(getattr(config, "min_samples_per_mode", 0))


def _validate_loaded_artifact(
    *,
    payload: Mapping[str, Any],
    manifest: IsolationForestManifest,
    scaler: ZScoreScaler,
    feature_columns: tuple[str, ...],
) -> None:
    scaler_columns = tuple(str(column) for column in scaler.columns)
    if scaler_columns != feature_columns:
        raise ValueError(
            "IsolationForest scaler columns do not match manifest feature_columns: "
            f"scaler={list(scaler_columns)!r}, manifest={list(feature_columns)!r}"
        )

    manifest_modes = tuple(mode.mode for mode in manifest.modes)
    if not manifest_modes:
        raise ValueError("IsolationForest manifest contains no trained modes")
    # Phase 7.2 introduced per-mode calibrated thresholds. An artifact whose
    # ``modes`` carry no ``score_threshold`` is pre-7.2 (or training broke
    # silently). Fusion treats null thresholds as "never fire", so the
    # detector loads cleanly but is functionally a no-op. Catch that here
    # rather than discovering it in production.
    if not any(mode.calibrated for mode in manifest.modes):
        raise ValueError(
            "IsolationForest manifest has no calibrated score_threshold on any "
            "mode — likely a pre-Phase-7.2 artifact, or calibration was disabled "
            "at training time. Retrain on current code (calibration_holdout_fraction"
            " > 0 in canary.yaml) before deploying."
        )
    model_modes = {str(mode) for mode in dict(payload["models"])}
    missing_modes = [mode for mode in manifest_modes if mode not in model_modes]
    if missing_modes:
        raise ValueError(
            "IsolationForest artifact missing models for manifest modes: "
            + ", ".join(missing_modes)
        )


def _per_mode_contamination(config: IsolationForestConfigLike) -> dict[str, float]:
    overrides = getattr(config, "per_mode_contamination", {})
    return {str(mode): float(value) for mode, value in dict(overrides).items()}


def _contamination_for_mode(config: IsolationForestConfigLike, mode: str) -> float:
    return _per_mode_contamination(config).get(mode, config.contamination)


def _resolve_feature_columns(
    frame: pd.DataFrame,
    override: Iterable[str] | None,
    mode_column: str,
) -> tuple[str, ...]:
    if override is not None:
        columns = tuple(str(column) for column in override)
        _require_columns(frame, columns)
        for column in columns:
            if not pd.api.types.is_numeric_dtype(frame[column]):
                raise TypeError(
                    f"column {column!r} is not numeric; IsolationForest needs numeric features"
                )
        return columns

    skip = {SESSION_COLUMN, TIMESTAMP_COLUMN, MODE_COLUMN, mode_column}
    discovered: list[str] = []
    for raw in frame.columns:
        column = str(raw)
        if column in skip:
            continue
        if is_flag_column(column):  # ``__gap`` / ``__arrived`` bookkeeping
            continue
        if not pd.api.types.is_numeric_dtype(frame[column]):
            continue
        # H01.1 source gate: discovery never picks an external sensor.
        if source_exclusion_reason(column) is not None:
            continue
        discovered.append(column)
    if not discovered:
        raise ValueError("no numeric feature columns available for IsolationForest")
    return tuple(discovered)


def _mode_summary(
    *,
    mode: str,
    contamination: float,
    frame: pd.DataFrame,
    session_column: str,
    timestamp_column: str,
    **extra: Any,
) -> ModeTrainingSummary:
    """Training-row provenance from ``frame`` plus every calibration / A04
    field passed through ``extra`` (``ModeTrainingSummary``'s own names)."""
    return ModeTrainingSummary(
        mode=mode,
        n_samples=len(frame),
        contamination=contamination,
        sessions=_unique_strings(frame[session_column]),
        start_timestamp=_min_timestamp(frame[timestamp_column]),
        end_timestamp=_max_timestamp(frame[timestamp_column]),
        **extra,
    )


def _training_data_hash(
    frame: pd.DataFrame,
    *,
    feature_columns: Sequence[str],
    mode_column: str,
    session_column: str,
    timestamp_column: str,
) -> str:
    columns = [session_column, timestamp_column, mode_column, *feature_columns]
    normalized = frame[columns].copy()
    normalized[timestamp_column] = pd.to_datetime(normalized[timestamp_column], utc=True).astype(
        "int64"
    )
    normalized = normalized.sort_values(columns, kind="stable").reset_index(drop=True)

    digest = hashlib.sha256()
    digest.update(json.dumps({"columns": columns}, sort_keys=True).encode("utf-8"))
    row_hashes = pd.util.hash_pandas_object(normalized, index=False).to_numpy(dtype=np.uint64)
    digest.update(row_hashes.tobytes())
    return f"sha256:{digest.hexdigest()}"


def _unique_strings(series: pd.Series) -> tuple[str, ...]:
    return tuple(sorted(str(value) for value in series.dropna().unique()))


def _min_timestamp(series: pd.Series) -> str | None:
    timestamps = pd.to_datetime(series, utc=True)
    if timestamps.empty:
        return None
    return _format_timestamp(timestamps.min())


def _max_timestamp(series: pd.Series) -> str | None:
    timestamps = pd.to_datetime(series, utc=True)
    if timestamps.empty:
        return None
    return _format_timestamp(timestamps.max())


def _isolation_forest_cls() -> Any:
    try:
        from sklearn.ensemble import IsolationForest
    except ImportError as exc:
        raise MissingModelDependencyError(
            "scikit-learn is required to train IsolationForest models"
        ) from exc
    return IsolationForest


def _joblib_module() -> Any:
    try:
        import joblib
    except ImportError as exc:
        raise MissingModelDependencyError(
            "joblib is required to save/load IsolationForest artifacts"
        ) from exc
    return joblib


def served_verdicts(scored: pd.DataFrame) -> pd.Series:
    """Per-row verdict exactly as the daemon serves it (A04.9): ``True`` when
    the row is at/over its mode's calibrated threshold, ``False`` otherwise —
    including a mode with no calibrated threshold, which is non-emitting at
    serve (Task 2.3) — and ``None`` for a withheld row (missing features or no
    served model), which the daemon's fail-safe suppresses: a withheld row is
    neither a detection nor a quiet tick.

    sklearn's ``contamination`` label (``LABEL_COLUMN``) is not consulted: it
    only sets ``offset_`` and no served decision reads it.
    """
    missing = scored[MISSING_FEATURES_COLUMN].astype(bool)
    verdict = scored[CALIBRATED_ANOMALY_COLUMN].astype("boolean").fillna(False).astype(object)
    verdict.loc[missing] = None
    return verdict


def seed_stability_sweep(
    fit_x: np.ndarray[Any, Any],
    eval_x: np.ndarray[Any, Any],
    *,
    n_estimators: Sequence[int],
    seeds: Sequence[int] = (0, 1, 2, 3),
    flag_rate: float = 0.01,
    n_jobs: int = 1,
) -> dict[int, dict[str, Any]]:
    """A04.10 — for each ensemble size, refit on the same rows with each seed,
    flag the top ``flag_rate`` of ``eval_x`` (held-out rows), and report the
    pairwise Jaccard of the flagged sets. The ensemble size is chosen where
    this stops improving; the 2026-09-20 highway rows gave 0.68 at 200 trees
    and 0.86 at 1,000."""
    cls = _isolation_forest_cls()
    out: dict[int, dict[str, Any]] = {}
    k = max(1, int(round(flag_rate * len(eval_x))))
    for size in n_estimators:
        flagged: list[set[int]] = []
        for seed in seeds:
            model = cls(n_estimators=size, random_state=seed, n_jobs=n_jobs)
            model.fit(fit_x)
            scores = -model.decision_function(eval_x)
            flagged.append(set(np.argsort(-scores, kind="stable")[:k].tolist()))
        pairs = [len(a & b) / len(a | b) for i, a in enumerate(flagged) for b in flagged[i + 1 :]]
        out[int(size)] = {
            "seeds": len(seeds),
            "flagged_per_seed": k,
            "mean_jaccard": float(np.mean(pairs)) if pairs else None,
            "min_jaccard": float(np.min(pairs)) if pairs else None,
        }
    return out
