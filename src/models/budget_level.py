"""A02.3 — the sequence detector's alert level, chosen from the alert budget.

Calibration cuts each band at a quantile of per-block maxima on the
calibration sessions. A 0.999 quantile over a few dozen blocks is those
sessions' maximum, and beating the maximum of ~36 samples gives about three
alert episodes an hour on a drive identical to calibration: the project's
budget of ``V1_ALERTS_PER_HOUR_BUDGET`` (1 alert per engine-hour) could not be
met by construction (:data:`BUDGET_LEVEL_INCIDENT`).

This module sets the *level* instead. The served verdict is

    global_score > band(mode)  OR  family_max > family_band

which is exactly ``severity > 1`` for the served severity
``max(global_score / band(mode), family_max / family_band)``
(:func:`served_severity`). :func:`choose_budget_level` picks the lowest level
``L`` whose persistence-gated alert episodes per engine-hour on held-out drives
meet the budget (``evaluation.alert_budget_threshold``, over per-block maxima),
and :func:`apply_budget_level` multiplies every verdict band (global, per-mode,
``family_max_score``) by ``L``. The served verdict then *is* ``severity > L``:
no serving code changes, the evidence ranking (per-channel and per-family
normalizers) is untouched, and the manifest records the budget, the drives, the
level and the achieved rate.

Two refusals, both measured constraints rather than notes:

* **Never on what the artifact trained or calibrated on.** In-sample
  calibration inflated the sequence detector's false-alarm rate to ~1.0 on
  unseen drives. The sessions are read from the scored frame itself and checked
  against the artifact's own manifest, so a caller cannot label them away
  (:class:`BudgetHoldoutSeenError`).
* **Never on exposure that cannot resolve the budget.** Below
  ``1 / budget`` engine-hours, "within budget" cannot be told from "zero
  alerts", and the level is only "above every held-out score"
  (:class:`BudgetHoldoutTooSmallError`, naming the hours and the sessions).

Applying a level twice would compound it, so a calibration that already
carries one is refused (:func:`apply_budget_level`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.config import CanaryConfig
from src.models.benchmark_gate import load_sequence_artifact
from src.models.evaluation import (
    PersistenceSpec,
    ScoreTrace,
    alert_budget_threshold,
    engine_running,
)
from src.models.evaluation_detectors import (
    BatchedSequenceScorer,
    SequenceScores,
    suppressed_for,
)
from src.models.false_positive_benchmark import V1_ALERTS_PER_HOUR_BUDGET
from src.models.inference import BUDGET_CADENCE_KEY
from src.models.sequence_autoencoder_training import (
    TrainedSequenceAutoencoder,
    save_sequence_autoencoder_model,
)
from src.models.sequence_calibration import SequenceThresholdCalibration
from src.preprocessing.drives import parent_session

#: The measurement every refusal here names.
BUDGET_LEVEL_INCIDENT = (
    "A02.3: a 0.999 quantile over ~36 calibration blocks is that session's maximum, "
    "~3 alert episodes per engine-hour on a drive identical to calibration, so the "
    "1-per-engine-hour budget could not be met by construction; in-sample calibration "
    "inflated the sequence detector's false-alarm rate to ~1.0 on unseen drives"
)

#: The manifest key (inside ``thresholds``) the record lives under.
ALERT_BUDGET_KEY = "alert_budget"


class BudgetHoldoutError(ValueError):
    """The held-out drives cannot set an alert level."""


class BudgetHoldoutSeenError(BudgetHoldoutError):
    """A budget session is one the artifact trained or calibrated on."""


class BudgetHoldoutTooSmallError(BudgetHoldoutError):
    """The held-out exposure cannot resolve the budget."""


def served_severity(scores: SequenceScores) -> np.ndarray[Any, np.dtype[np.float64]]:
    """Per row, the served verdict as one number: ``> 1`` exactly when served
    flags it (global score over its mode band, or the worst family over the
    family band). NaN before a window, as the verdict is ``None`` there."""
    global_score = np.asarray(scores.global_score, dtype=np.float64)
    band = np.asarray(scores.global_threshold, dtype=np.float64)
    severity = np.full(global_score.shape, np.nan)
    banded = np.isfinite(global_score) & np.isfinite(band) & (band > 0)
    severity[banded] = global_score[banded] / band[banded]
    if scores.family_threshold is not None and scores.family_threshold > 0:
        family = np.asarray(scores.family_max, dtype=np.float64) / scores.family_threshold
        family_known = np.isfinite(family)
        severity = np.where(
            family_known & np.isfinite(severity), np.maximum(severity, family), severity
        )
        severity = np.where(family_known & ~np.isfinite(severity), family, severity)
    return severity


@dataclass(frozen=True)
class BudgetLevel:
    """The level the verdict bands are scaled by, and what it rests on."""

    level: float
    budget_per_engine_hour: float
    #: Parent recording directories the level was chosen on.
    sessions: tuple[str, ...]
    engine_hours: float
    episodes: int
    achieved_per_engine_hour: float
    #: Independent (non-overlapping) blocks the candidate levels came from.
    blocks: int
    block_rows: int
    persistence: Mapping[str, Any]
    note: str
    #: A06.6 — the forward cadence the traces were scored at: serving at any
    #: other refuses (``require_budget_cadence``).
    forward_every_n_ticks: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": True,
            "level": self.level,
            "budget_per_engine_hour": self.budget_per_engine_hour,
            "holdout_sessions": list(self.sessions),
            "holdout_role": "evaluation",
            "engine_hours": self.engine_hours,
            "alert_episodes": self.episodes,
            "achieved_per_engine_hour": self.achieved_per_engine_hour,
            "blocks": self.blocks,
            "block_rows": self.block_rows,
            "persistence": dict(self.persistence),
            BUDGET_CADENCE_KEY: self.forward_every_n_ticks,
            "rule": "every verdict band (global, per-mode, family_max_score) x level; "
            "served verdict == served severity > level",
            "in_sample_to": "holdout_sessions: the achieved rate is measured on the drives "
            "the level was chosen on, never a held-out number",
            "note": self.note,
        }


def refuse_seen_sessions(sessions: Sequence[str], *, manifest: Any) -> None:
    """Refuse any budget session the artifact trained or calibrated on.

    Compared by recording directory, so a ``#segment`` piece of a training
    recording is caught too.
    """
    seen: dict[str, str] = {}
    for role in ("training_sessions", "calibration_sessions"):
        for name in getattr(manifest, role, ()) or ():
            seen.setdefault(parent_session(str(name)), role.removesuffix("_sessions"))
    clashes = sorted(
        {
            f"{parent_session(s)} ({seen[parent_session(s)]})"
            for s in sessions
            if parent_session(s) in seen
        }
    )
    if clashes:
        raise BudgetHoldoutSeenError(
            "refusing to set the alert level on sessions the artifact "
            f"trained or calibrated on: {', '.join(clashes)}. The level must come from "
            f"held-out evaluation drives. Refused because of {BUDGET_LEVEL_INCIDENT}."
        )


def score_budget_traces(
    trained: Any,
    frame: pd.DataFrame,
    config: CanaryConfig,
    *,
    batch_size: int = 512,
) -> dict[str, ScoreTrace]:
    """The served severity of ``trained`` on each session piece of ``frame``
    (a ``build_feature_frame`` output), with its engine-running rows.

    Scored with the serve-time channel suppression and the served rule
    (``BatchedSequenceScorer``), keyed by the frame's own session column. Only
    the windows serving forward-passes at ``models.sequence_forward_every_n_ticks``
    carry a severity (A06.6); the rest have none, as live, so the persistence
    gate and the episodes see the served schedule.
    """
    if frame.empty:
        return {}
    if SESSION_COLUMN not in frame.columns:
        raise BudgetHoldoutError(
            f"the budget frame has no {SESSION_COLUMN!r} column, so the sessions it rests "
            "on cannot be checked against the artifact's training and calibration"
        )
    scorer = BatchedSequenceScorer(
        trained, suppress_channels=suppressed_for(trained, config), batch_size=batch_size
    )
    traces: dict[str, ScoreTrace] = {}
    for name, group in frame.groupby(SESSION_COLUMN, sort=False):
        rows = group.reset_index(drop=True)
        scored = scorer.score(rows).at_cadence(config.models.sequence_forward_every_n_ticks)
        traces[str(name)] = ScoreTrace(
            pd.to_datetime(rows[TIMESTAMP_COLUMN], utc=True).reset_index(drop=True),
            served_severity(scored),
            engine_running(rows),
        )
    return traces


def choose_budget_level(
    traces: Mapping[str, ScoreTrace],
    *,
    manifest: Any,
    persistence: PersistenceSpec,
    block_rows: int,
    budget_per_engine_hour: float = V1_ALERTS_PER_HOUR_BUDGET,
    forward_every_n_ticks: int = 1,
) -> BudgetLevel:
    """The lowest level whose alert episodes on ``traces`` meet the budget.

    ``traces`` are keyed by session piece (as :func:`score_budget_traces`
    returns them); every key is checked against ``manifest``'s training and
    calibration sessions first. Refuses an exposure that cannot resolve the
    budget, and one with no score at all, naming the hours and the sessions.
    ``forward_every_n_ticks`` is the cadence ``traces`` were scored at
    (:func:`score_budget_traces`); it is recorded with the level.
    """
    if budget_per_engine_hour <= 0:
        raise ValueError("the alert budget must be positive")
    sessions = tuple(sorted({parent_session(name) for name in traces}))
    refuse_seen_sessions(list(traces), manifest=manifest)
    needed = 1.0 / budget_per_engine_hour
    if not traces:
        raise BudgetHoldoutTooSmallError(
            "no evaluation drives to set the alert level from: 0 engine-h, "
            f"{needed:g} engine-h are needed to resolve {budget_per_engine_hour:g} alert(s) "
            f"per engine-hour. Refused because of {BUDGET_LEVEL_INCIDENT}."
        )
    result = alert_budget_threshold(
        list(traces.values()),
        persistence=persistence,
        block_seconds=float(block_rows),
        budget_per_engine_hour=budget_per_engine_hour,
    )
    if result.threshold is None or not result.resolvable:
        raise BudgetHoldoutTooSmallError(
            f"the evaluation holdout ({', '.join(sessions)}) holds {result.engine_hours:.2f} "
            f"engine-h; {needed:g} engine-h are needed to tell {budget_per_engine_hour:g} "
            f"alert(s) per engine-hour from zero ({result.note}). Hold out more driving "
            f"(--evaluation-sessions). Refused because of {BUDGET_LEVEL_INCIDENT}."
        )
    assert result.achieved_per_engine_hour is not None
    episodes = int(round(result.achieved_per_engine_hour * result.engine_hours))
    return BudgetLevel(
        level=float(result.threshold),
        budget_per_engine_hour=budget_per_engine_hour,
        sessions=sessions,
        engine_hours=float(result.engine_hours),
        episodes=episodes,
        achieved_per_engine_hour=float(result.achieved_per_engine_hour),
        blocks=int(result.blocks),
        block_rows=int(block_rows),
        persistence={
            "required": persistence.required,
            "window": persistence.window,
            "window_seconds": persistence.window_seconds,
        },
        note=result.note,
        forward_every_n_ticks=int(forward_every_n_ticks),
    )


def apply_budget_level(
    calibration: SequenceThresholdCalibration, level: BudgetLevel
) -> SequenceThresholdCalibration:
    """``calibration`` with every verdict band multiplied by ``level.level`` and
    the record stamped. Refuses a calibration that already carries a level
    (scaling twice would compound it) and a non-positive level."""
    if calibration.alert_budget is not None:
        raise ValueError(
            "this calibration already carries an alert-budget level "
            f"({dict(calibration.alert_budget).get('level')!r}); applying another would "
            "compound it. Recalibrate from the trained model instead."
        )
    if not (np.isfinite(level.level) and level.level > 0):
        raise BudgetHoldoutError(
            f"alert level {level.level!r} is not a positive number: the held-out served "
            "severity never rose above zero, so it cannot scale a band"
        )
    factor = level.level

    def scaled(distribution: Any) -> Any:
        return replace(distribution, threshold=float(distribution.threshold) * factor)

    pre = {
        "global": calibration.global_error.threshold,
        "per_mode": (
            None
            if calibration.per_mode_errors is None
            else {m: d.threshold for m, d in calibration.per_mode_errors.items()}
        ),
        "family_max": (
            None if calibration.family_max_score is None else calibration.family_max_score.threshold
        ),
    }
    return replace(
        calibration,
        global_error=scaled(calibration.global_error),
        per_mode_errors=(
            None
            if calibration.per_mode_errors is None
            else {mode: scaled(d) for mode, d in calibration.per_mode_errors.items()}
        ),
        family_max_score=(
            None if calibration.family_max_score is None else scaled(calibration.family_max_score)
        ),
        alert_budget={**level.to_dict(), "bands_before_level": pre},
    )


def _rewrite_thresholds(trained: Any, artifact_path: Path, thresholds: Mapping[str, Any]) -> None:
    """Save ``trained`` back to ``artifact_path`` with ``thresholds``. The
    binary embeds its manifest, so both the checkpoint and the sidecar are
    rewritten (``write_artifact``: binary first, sidecar last)."""
    manifest = replace(trained.manifest, thresholds=dict(thresholds))
    save_sequence_autoencoder_model(
        TrainedSequenceAutoencoder(model=trained.model, manifest=manifest), artifact_path
    )


def _calibration_of(trained: Any, artifact_path: Path) -> SequenceThresholdCalibration:
    if trained.manifest.thresholds is None:
        raise BudgetHoldoutError(
            f"{artifact_path}: the artifact has no calibrated thresholds to set a level on"
        )
    return SequenceThresholdCalibration.from_dict(trained.manifest.thresholds)


def set_artifact_budget_level(
    artifact_path: Path,
    frame: pd.DataFrame,
    config: CanaryConfig,
    *,
    persistence: PersistenceSpec,
    budget_per_engine_hour: float = V1_ALERTS_PER_HOUR_BUDGET,
) -> BudgetLevel:
    """Choose the alert level of the sequence artifact at ``artifact_path`` on
    ``frame`` (the evaluation holdout's ``build_feature_frame`` output) and
    write it into the artifact. Raises :class:`BudgetHoldoutError` (the artifact
    untouched) when the holdout is seen or too small."""
    trained = load_sequence_artifact(artifact_path)
    calibration = _calibration_of(trained, artifact_path)
    if SESSION_COLUMN in frame.columns:
        # Before the scoring pass: a seen session is refused without one.
        refuse_seen_sessions(
            [str(name) for name in frame[SESSION_COLUMN].dropna().unique()],
            manifest=trained.manifest,
        )
    level = choose_budget_level(
        score_budget_traces(trained, frame, config),
        manifest=trained.manifest,
        persistence=persistence,
        block_rows=int(trained.manifest.schema["sequence_length"]),
        budget_per_engine_hour=budget_per_engine_hour,
        forward_every_n_ticks=config.models.sequence_forward_every_n_ticks,
    )
    _rewrite_thresholds(trained, artifact_path, apply_budget_level(calibration, level).to_dict())
    return level


def stamp_unbudgeted(artifact_path: Path, record: Mapping[str, Any]) -> None:
    """Write an :func:`unbudgeted_record` into the artifact's thresholds."""
    trained = load_sequence_artifact(artifact_path)
    calibration = _calibration_of(trained, artifact_path)
    if calibration.alert_budget is not None:
        raise ValueError(f"{artifact_path}: the thresholds already carry an alert-budget record")
    _rewrite_thresholds(
        trained, artifact_path, replace(calibration, alert_budget=dict(record)).to_dict()
    )


def budget_level_sessions(manifest: Any) -> tuple[str, ...]:
    """The sessions ``manifest``'s alert level was chosen on (A02.3), from
    ``thresholds.alert_budget``; empty when no level was applied. Readers that
    need held-out drives treat these as seen."""
    thresholds = getattr(manifest, "thresholds", None)
    if not isinstance(thresholds, Mapping):
        return ()
    record = thresholds.get(ALERT_BUDGET_KEY)
    if not isinstance(record, Mapping) or not record.get("applied"):
        return ()
    return tuple(str(name) for name in record.get("holdout_sessions") or ())


def unbudgeted_record(*, reason: str, override_reason: str) -> dict[str, Any]:
    """The stamp of an artifact published without a budget level, by an
    explicit operator override (``--accept-unbudgeted-thresholds``)."""
    if not override_reason.strip():
        raise ValueError("publishing without an alert-budget level needs a stated reason")
    return {
        "applied": False,
        "refusal": reason,
        "override_reason": override_reason,
        "incident": BUDGET_LEVEL_INCIDENT,
    }


__all__ = [
    "ALERT_BUDGET_KEY",
    "BUDGET_LEVEL_INCIDENT",
    "BudgetHoldoutError",
    "BudgetHoldoutSeenError",
    "BudgetHoldoutTooSmallError",
    "BudgetLevel",
    "apply_budget_level",
    "budget_level_sessions",
    "choose_budget_level",
    "refuse_seen_sessions",
    "score_budget_traces",
    "served_severity",
    "set_artifact_budget_level",
    "stamp_unbudgeted",
    "unbudgeted_record",
]
