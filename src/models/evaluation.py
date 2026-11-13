"""Drive-grouped detector evaluation: one yardstick for every detector (A02.4).

Before this there was no number in the repo for how well a detector finds a
fault on a drive it has not seen. The retrain gate scored false alarms on the
sessions its thresholds were cut from, and the fault benchmark pooled
per-second counts across drives. This module is the protocol every later
detector decision (A05, A06) is judged on:

* **Physical drives, not sessions.** Sessions that overlap or follow each
  other within :data:`DRIVE_GAP_SECONDS` (a daemon restart, a capture window
  opening mid-drive) are one drive (:func:`group_into_drives`), ordered by
  parsed start time (``session_started_at``), never by directory name.
* **Eras.** A drive belongs to the tune revision and hardware-intervention
  interval it was recorded in (:class:`Era`); a drive spanning a boundary is
  skipped, and a fold never mixes eras (:class:`Fold`).
* **Held out means unseen.** :class:`HoldoutSplit` refuses a session in both
  the calibration and the evaluation side, and a physical drive split across
  them.
* **Event-level metrics at the deployed threshold.** Alert *episodes* after
  the production persistence gate (:func:`alert_episodes`), false alarms per
  engine-hour, event recall and time to detect. No point-adjust: a detection
  is an episode the fault itself caused (:func:`fault_attributable_detection`).
  :func:`vus_pr` is the only threshold-free number, and only for comparisons.
* **Uncertainty over drives.** :func:`bootstrap_ratio_ci` resamples whole
  drives, never correlated seconds.
* **Thresholds from the alert budget** (A02.3): :func:`alert_budget_threshold`
  picks the lowest threshold whose alert episodes on the given drives stay
  within ``V1_ALERTS_PER_HOUR_BUDGET`` per engine-hour, from per-block maxima
  (independent windows), and says when the exposure cannot resolve the budget.

Every empty result says which kind of empty it is: *withheld* (the detector
had no verdict), *skipped* (the drive or window could not carry the test),
*scored and quiet*, or *flagged*.

The heavy parts — loading sessions, the detectors themselves — live in
:mod:`src.models.evaluation_detectors` and ``scripts/evaluate_detectors.py``;
this module is pure (numpy/pandas) so its rules are unit-testable.

The code lives in four owner modules and this one re-exports every name, so
``from src.models.evaluation import ...`` keeps working (R27.7):

* :mod:`src.models.evaluation_corpus` — sessions, drives, eras, holdouts, and
  :func:`refuse_non_default_profile_sessions`;
* :mod:`src.models.evaluation_alerts` — the persistence gate and alert episodes;
* :mod:`src.models.evaluation_metrics` — engine-hours, detection grading, the
  alert-budget threshold, the drive bootstrap and :func:`vus_pr`;
* :mod:`src.models.evaluation_shift` — the single-quantity shift known answer.

Here stay the reporting strata and :class:`DriveDetectorResult`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.models.evaluation_alerts import (
    MAX_ENGINE_STEP_SECONDS,
    Episode,
    PersistenceSpec,
    alert_episodes,
    sustained_alerts,
)
from src.models.evaluation_corpus import (
    EVALUATION_HOLDOUT_DRIVES,
    MIN_CALIBRATION_HOLDOUT_BYTES,
    MIN_CALIBRATION_HOLDOUT_SESSIONS,
    MIN_HOLDOUT_MODE_SECONDS,
    Drive,
    DriveHoldout,
    Era,
    EraResolver,
    Fold,
    HoldoutSplit,
    NonDefaultProfileTrainingError,
    drives_of_sessions,
    era_resolver,
    group_into_drives,
    grouped_folds,
    holdout_by_drive,
    nothing_left_to_train,
    pick_calibration_holdouts,
    pick_evaluation_holdouts,
    refuse_non_default_profile_sessions,
    session_holdout_health,
    session_mode_coverage,
    session_parquet_bytes,
    session_span,
    span_from_name,
)
from src.models.evaluation_metrics import (
    BudgetThreshold,
    DetectionOutcome,
    DetectionStatus,
    RatioEstimate,
    ScoreTrace,
    alert_budget_threshold,
    block_maxima,
    bootstrap_ratio_ci,
    engine_hours,
    engine_running,
    fault_attributable_detection,
    vus_pr,
)
from src.models.evaluation_shift import (
    SHIFT_CONTROL,
    SHIFT_CONTROL_MIN_FLAG_RATE,
    SHIFT_PASS_FLAG_RATE,
    SHIFT_PASS_SIGMA,
    SHIFT_SIGMAS,
    ShiftOutcome,
    ShiftReport,
    VerdictScorer,
    mode_spreads,
    shift_columns,
    single_quantity_shift,
)
from src.preprocessing.drives import DRIVE_GAP_SECONDS, SessionSpan

BARO_CHANNEL = "obd:BAROMETRIC_PRESSURE"
#: J1979 PID 0x46. Intake air temperature is *not* a stand-in: on this project
#: it tracks airflow (stagnation), not the weather.
AMBIENT_CHANNEL = "obd:AMBIENT_AIR_TEMP"

#: Reporting strata. Bands, not vehicle facts: home altitude differs per
#: vehicle, but a kPa is a kPa.
BARO_BANDS_KPA: tuple[tuple[float | None, float | None, str], ...] = (
    (None, 70.0, "<70 kPa"),
    (70.0, 77.0, "70-77 kPa"),
    (77.0, None, ">=77 kPa"),
)
AMBIENT_BANDS_C: tuple[tuple[float | None, float | None, str], ...] = (
    (None, 10.0, "<10 degC"),
    (10.0, 25.0, "10-25 degC"),
    (25.0, None, ">=25 degC"),
)
UNMEASURED = "unmeasured"


# ---------------------------------------------------------------------------
# Strata
# ---------------------------------------------------------------------------


def band_of(value: float | None, bands: Sequence[tuple[float | None, float | None, str]]) -> str:
    if value is None or not math.isfinite(value):
        return UNMEASURED
    for low, high, label in bands:
        if (low is None or value >= low) and (high is None or value < high):
            return label
    return UNMEASURED


def observed_median(frame: pd.DataFrame, column: str) -> float | None:
    """Median of a channel over rows where it was freshly observed (its
    ``__gap`` flag clear), so a forward-filled hold is not re-counted."""
    if column not in frame.columns:
        return None
    values = pd.to_numeric(frame[column], errors="coerce")
    gap = f"{column}__gap"
    if gap in frame.columns:
        values = values[~frame[gap].fillna(True).astype(bool)]
    values = values.dropna()
    return None if values.empty else float(values.median())


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


@dataclass
class DriveDetectorResult:
    """One detector on one drive's clean rows."""

    rows: int = 0
    scored_rows: int = 0
    flagged_rows: int = 0
    episodes: int = 0
    #: Episodes that start inside a labelled interval, by label kind — reported
    #: beside, never inside, the false-alarm count.
    labelled_episodes: dict[str, int] = field(default_factory=dict)

    @property
    def withheld_rows(self) -> int:
        return self.rows - self.scored_rows

    @property
    def false_alarm_episodes(self) -> int:
        return self.episodes - sum(self.labelled_episodes.values())

    def to_dict(self, hours: float) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "scored_rows": self.scored_rows,
            "withheld_rows": self.withheld_rows,
            "flagged_rows": self.flagged_rows,
            "episodes": self.episodes,
            "labelled_episodes": dict(self.labelled_episodes),
            "false_alarm_episodes": self.false_alarm_episodes,
            "false_alarms_per_engine_hour": (
                None if hours <= 0 else self.false_alarm_episodes / hours
            ),
        }


__all__ = [
    "AMBIENT_BANDS_C",
    "AMBIENT_CHANNEL",
    "BARO_BANDS_KPA",
    "BARO_CHANNEL",
    "DRIVE_GAP_SECONDS",
    "EVALUATION_HOLDOUT_DRIVES",
    "MAX_ENGINE_STEP_SECONDS",
    "MIN_CALIBRATION_HOLDOUT_BYTES",
    "MIN_CALIBRATION_HOLDOUT_SESSIONS",
    "MIN_HOLDOUT_MODE_SECONDS",
    "SHIFT_CONTROL",
    "SHIFT_CONTROL_MIN_FLAG_RATE",
    "SHIFT_PASS_FLAG_RATE",
    "SHIFT_PASS_SIGMA",
    "SHIFT_SIGMAS",
    "UNMEASURED",
    "BudgetThreshold",
    "DetectionOutcome",
    "DetectionStatus",
    "Drive",
    "DriveDetectorResult",
    "DriveHoldout",
    "Episode",
    "Era",
    "EraResolver",
    "Fold",
    "HoldoutSplit",
    "NonDefaultProfileTrainingError",
    "PersistenceSpec",
    "RatioEstimate",
    "ScoreTrace",
    "SessionSpan",
    "ShiftOutcome",
    "ShiftReport",
    "VerdictScorer",
    "alert_budget_threshold",
    "alert_episodes",
    "band_of",
    "block_maxima",
    "bootstrap_ratio_ci",
    "drives_of_sessions",
    "engine_hours",
    "engine_running",
    "era_resolver",
    "fault_attributable_detection",
    "group_into_drives",
    "grouped_folds",
    "holdout_by_drive",
    "mode_spreads",
    "nothing_left_to_train",
    "observed_median",
    "pick_calibration_holdouts",
    "pick_evaluation_holdouts",
    "refuse_non_default_profile_sessions",
    "session_holdout_health",
    "session_mode_coverage",
    "session_parquet_bytes",
    "session_span",
    "shift_columns",
    "single_quantity_shift",
    "span_from_name",
    "sustained_alerts",
    "vus_pr",
]
