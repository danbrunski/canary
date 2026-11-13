"""``src.models.evaluation`` re-exports its owners' objects (R27.7).

The evaluation protocol lives in four modules (corpus, alerts, metrics, shift);
``evaluation`` is the facade the trainers, scripts and older tests import
from. A re-export that quietly became a copy would let a test patch one object
while the code under test used another, so each name must be the owner's own.
"""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest

PACKAGE = "src.models"

#: Re-exported name -> owner module (within ``src.models``).
REEXPORTS = {
    # corpus
    "Drive": "evaluation_corpus",
    "DriveHoldout": "evaluation_corpus",
    "EVALUATION_HOLDOUT_DRIVES": "evaluation_corpus",
    "Era": "evaluation_corpus",
    "EraResolver": "evaluation_corpus",
    "Fold": "evaluation_corpus",
    "HoldoutSplit": "evaluation_corpus",
    "MIN_CALIBRATION_HOLDOUT_BYTES": "evaluation_corpus",
    "MIN_CALIBRATION_HOLDOUT_SESSIONS": "evaluation_corpus",
    "MIN_HOLDOUT_MODE_SECONDS": "evaluation_corpus",
    "NonDefaultProfileTrainingError": "evaluation_corpus",
    "drives_of_sessions": "evaluation_corpus",
    "era_resolver": "evaluation_corpus",
    "group_into_drives": "evaluation_corpus",
    "grouped_folds": "evaluation_corpus",
    "holdout_by_drive": "evaluation_corpus",
    "nothing_left_to_train": "evaluation_corpus",
    "pick_calibration_holdouts": "evaluation_corpus",
    "pick_evaluation_holdouts": "evaluation_corpus",
    "refuse_non_default_profile_sessions": "evaluation_corpus",
    "session_holdout_health": "evaluation_corpus",
    "session_mode_coverage": "evaluation_corpus",
    "session_parquet_bytes": "evaluation_corpus",
    "session_span": "evaluation_corpus",
    "span_from_name": "evaluation_corpus",
    # alerts
    "MAX_ENGINE_STEP_SECONDS": "evaluation_alerts",
    "Episode": "evaluation_alerts",
    "PersistenceSpec": "evaluation_alerts",
    "alert_episodes": "evaluation_alerts",
    "sustained_alerts": "evaluation_alerts",
    # metrics
    "BudgetThreshold": "evaluation_metrics",
    "DetectionOutcome": "evaluation_metrics",
    "DetectionStatus": "evaluation_metrics",
    "RatioEstimate": "evaluation_metrics",
    "ScoreTrace": "evaluation_metrics",
    "alert_budget_threshold": "evaluation_metrics",
    "block_maxima": "evaluation_metrics",
    "bootstrap_ratio_ci": "evaluation_metrics",
    "engine_hours": "evaluation_metrics",
    "engine_running": "evaluation_metrics",
    "fault_attributable_detection": "evaluation_metrics",
    "vus_pr": "evaluation_metrics",
    # shift
    "SHIFT_CONTROL": "evaluation_shift",
    "SHIFT_CONTROL_MIN_FLAG_RATE": "evaluation_shift",
    "SHIFT_PASS_FLAG_RATE": "evaluation_shift",
    "SHIFT_PASS_SIGMA": "evaluation_shift",
    "SHIFT_SIGMAS": "evaluation_shift",
    "ShiftOutcome": "evaluation_shift",
    "ShiftReport": "evaluation_shift",
    "VerdictScorer": "evaluation_shift",
    "mode_spreads": "evaluation_shift",
    "shift_columns": "evaluation_shift",
    "single_quantity_shift": "evaluation_shift",
    # the preprocessing layer's drive types, re-exported since A02.4
    "DRIVE_GAP_SECONDS": "preprocessing.drives",
    "SessionSpan": "preprocessing.drives",
}

#: Defined in ``evaluation`` itself (reporting strata and its result type).
OWNED_HERE = {
    "AMBIENT_BANDS_C",
    "AMBIENT_CHANNEL",
    "BARO_BANDS_KPA",
    "BARO_CHANNEL",
    "UNMEASURED",
    "DriveDetectorResult",
    "band_of",
    "observed_median",
}


def _owner_module(owner: str) -> ModuleType:
    return importlib.import_module(
        f"src.{owner}" if owner.startswith("preprocessing") else f"{PACKAGE}.{owner}"
    )


@pytest.mark.parametrize(("name", "owner"), sorted(REEXPORTS.items()))
def test_evaluation_names_are_the_owners_objects(name: str, owner: str) -> None:
    facade = importlib.import_module(f"{PACKAGE}.evaluation")
    module = _owner_module(owner)
    assert getattr(facade, name) is getattr(module, name)
    assert getattr(module, name) is not None


def test_evaluation_all_names_every_export() -> None:
    facade = importlib.import_module(f"{PACKAGE}.evaluation")
    assert set(facade.__all__) == set(REEXPORTS) | OWNED_HERE
    for name in OWNED_HERE:
        obj = getattr(facade, name)
        if isinstance(obj, type) or callable(obj):
            assert obj.__module__ == f"{PACKAGE}.evaluation"


def test_moved_classes_report_their_owner_module() -> None:
    """A pickle names its class by ``__module__``; pin where each moved class lives."""
    for name, owner in REEXPORTS.items():
        facade = importlib.import_module(f"{PACKAGE}.evaluation")
        obj = getattr(facade, name)
        if isinstance(obj, type) and owner.startswith("evaluation"):
            assert obj.__module__ == f"{PACKAGE}.{owner}"
