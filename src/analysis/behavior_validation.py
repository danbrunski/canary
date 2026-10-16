"""Reproducible behavior-model validation (B02).

One evaluator for every question the refit and the offline review ask of a
behavior model: which rows could it speak for, how wrong was it there in
physical units, would it have alerted, could it have seen a fault of the size
the operator cares about, and on how many *independent* drives was that
measured.

The pieces, each used by both ``src.analysis.refit`` (publication) and
``scripts/evaluate_behavior_models.py`` (the reproducible report):

* **Drive grouping** (B02.3). A physical drive is split across directories —
  a capture rotates the writer into ``_capture-…`` and back — and the
  resampler splits a directory at gaps into ``#segment-NNN`` pieces. Pieces
  are grouped by parent directory, then adjacent directories are joined when
  the gap between them is at most ``join_gap_seconds``. Order comes from
  parsed start times (``src.storage.sessions.session_started_at``) or the
  frame's own timestamps — never from directory names, where a capture prefix
  sorts a later drive "earlier".
* **Grouped splits.** Newest drives form the test fold, the next ones the
  calibration fold, the rest train. No drive's pieces may cross folds; an
  explicit assignment that tries is refused (:class:`LeakedDriveError`).
* **Row evaluation** through the single B01 eligibility evaluator, with
  denominators kept apart: rows, input-present, fresh, eligible (passes the
  serving contract), evaluated (on-map with an actual). An ineligible row is
  never an ``ok`` measurement.
* **Metrics in physical units** — MAE, signed bias, absolute-error quantiles,
  R² only as a descriptor, warning/critical band half-widths — plus event
  counts after the daemon's own persistence gate and emitter dedupe, reported
  separately from per-row band exceedance. Drive-level uncertainty: per-drive
  MAE spread, and a drive-resampled bootstrap once three drives exist
  (adjacent seconds are never treated as independent).
* **Skill and sensitivity** (B02.6): a training-fitted simple baseline scored
  on the same rows, and explicitly synthetic step faults of declared
  magnitude/duration run through the same gate. Synthetic detection is never
  called fault recall.
* **Criteria** come from the vehicle profile's ``behavior_validation.yaml``;
  :func:`apply_criteria` turns metrics into ``passed`` / ``failed`` /
  ``insufficient_evidence`` with every failure named, and
  :func:`src.behavior.behavior_models.serving_status` is the only consumer of
  that verdict.

Compatibility facade (R20). The pieces above now live in modules that depend
one way (``tests/test_behavior_validation_boundaries.py`` holds the map):

=================================== ======================================================
``behavior_validation_schema``      the validation document, evaluator version, identity
``behavior_validation_inputs``      default-profile stamps, per-read axis pairing
``behavior_validation_frame``       the bounded offline evaluation frame
``behavior_validation_evaluation``  ``RowEvaluation``, ``evaluate_rows``, event projection
``behavior_validation_metrics``     fold metrics, baselines, bootstrap, strata
``behavior_validation_sensitivity`` synthetic faults and transient controls
``behavior_validation_criteria``    ``apply_criteria``
``behavior_validation_evidence``    ``ValidationContext``, ``build_evidence``, carry-over
``splits`` (R05.13)                 drive grouping and grouped splits
=================================== ======================================================

This file defines nothing but its logger: every other name is the owner's
object, kept so the old import path keeps working. Library code, the refit and
the evaluation CLI import the owners; a monkeypatch must target the owner whose
function reads the name.
"""

from __future__ import annotations

import logging

from src.analysis.behavior_validation_criteria import apply_criteria as apply_criteria
from src.analysis.behavior_validation_evaluation import (
    PERSISTENCE_NO_TRAINING_EXCURSION as PERSISTENCE_NO_TRAINING_EXCURSION,
)
from src.analysis.behavior_validation_evaluation import EventPolicy as EventPolicy
from src.analysis.behavior_validation_evaluation import RowEvaluation as RowEvaluation
from src.analysis.behavior_validation_evaluation import _seconds as _seconds
from src.analysis.behavior_validation_evaluation import _verdicts as _verdicts
from src.analysis.behavior_validation_evaluation import (
    axis_separation_summary as axis_separation_summary,
)
from src.analysis.behavior_validation_evaluation import (
    band_excess_event_times as band_excess_event_times,
)
from src.analysis.behavior_validation_evaluation import count_events as count_events
from src.analysis.behavior_validation_evaluation import evaluate_rows as evaluate_rows
from src.analysis.behavior_validation_evaluation import event_times as event_times
from src.analysis.behavior_validation_evaluation import (
    fit_band_excess_persistence as fit_band_excess_persistence,
)
from src.analysis.behavior_validation_evaluation import model_event_times as model_event_times
from src.analysis.behavior_validation_evidence import ValidationContext as ValidationContext
from src.analysis.behavior_validation_evidence import build_evidence as build_evidence
from src.analysis.behavior_validation_evidence import carry_over_flags as carry_over_flags
from src.analysis.behavior_validation_frame import build_evaluation_frame as build_evaluation_frame
from src.analysis.behavior_validation_frame import (
    evaluation_decoded_signals as evaluation_decoded_signals,
)
from src.analysis.behavior_validation_inputs import _PAIRABLE_BASES as _PAIRABLE_BASES
from src.analysis.behavior_validation_inputs import _PROFILE_SCOPED_KINDS as _PROFILE_SCOPED_KINDS
from src.analysis.behavior_validation_inputs import AXIS_PAIRING_COLUMN as AXIS_PAIRING_COLUMN
from src.analysis.behavior_validation_inputs import (
    AXIS_PAIRING_POLICY_COLUMN as AXIS_PAIRING_POLICY_COLUMN,
)
from src.analysis.behavior_validation_inputs import AXIS_SEPARATION_PREFIX as AXIS_SEPARATION_PREFIX
from src.analysis.behavior_validation_inputs import PROFILE_EXCLUDED as PROFILE_EXCLUDED
from src.analysis.behavior_validation_inputs import (
    PROFILE_EXCLUSION_COLUMN as PROFILE_EXCLUSION_COLUMN,
)
from src.analysis.behavior_validation_inputs import PROFILE_UNVERIFIED as PROFILE_UNVERIFIED
from src.analysis.behavior_validation_inputs import (
    AxisPairingRefusedError as AxisPairingRefusedError,
)
from src.analysis.behavior_validation_inputs import ProfileScope as ProfileScope
from src.analysis.behavior_validation_inputs import SessionReads as SessionReads
from src.analysis.behavior_validation_inputs import UnverifiedProfileError as UnverifiedProfileError
from src.analysis.behavior_validation_inputs import _model_axes as _model_axes
from src.analysis.behavior_validation_inputs import _pair_session_reads as _pair_session_reads
from src.analysis.behavior_validation_inputs import axis_pairing_bounds as axis_pairing_bounds
from src.analysis.behavior_validation_inputs import axis_separation_column as axis_separation_column
from src.analysis.behavior_validation_inputs import graded_frame as graded_frame
from src.analysis.behavior_validation_inputs import per_read_frame as per_read_frame
from src.analysis.behavior_validation_inputs import profile_exclusions as profile_exclusions
from src.analysis.behavior_validation_inputs import profile_refusals as profile_refusals
from src.analysis.behavior_validation_inputs import read_model_reads as read_model_reads
from src.analysis.behavior_validation_inputs import (
    require_axis_pairing_covers as require_axis_pairing_covers,
)
from src.analysis.behavior_validation_inputs import (
    stamp_profile_exclusions as stamp_profile_exclusions,
)
from src.analysis.behavior_validation_metrics import ENVELOPE_MIN_ROWS as ENVELOPE_MIN_ROWS
from src.analysis.behavior_validation_metrics import MIN_BOOTSTRAP_DRIVES as MIN_BOOTSTRAP_DRIVES
from src.analysis.behavior_validation_metrics import BaselineKind as BaselineKind
from src.analysis.behavior_validation_metrics import _bin_labels as _bin_labels
from src.analysis.behavior_validation_metrics import _drive_bootstrap as _drive_bootstrap
from src.analysis.behavior_validation_metrics import _evaluated_hours as _evaluated_hours
from src.analysis.behavior_validation_metrics import _median_or_none as _median_or_none
from src.analysis.behavior_validation_metrics import _r_squared as _r_squared
from src.analysis.behavior_validation_metrics import baseline_predictions as baseline_predictions
from src.analysis.behavior_validation_metrics import breakdown as breakdown
from src.analysis.behavior_validation_metrics import breakdown_by_labels as breakdown_by_labels
from src.analysis.behavior_validation_metrics import envelope_coverage as envelope_coverage
from src.analysis.behavior_validation_metrics import fit_baseline as fit_baseline
from src.analysis.behavior_validation_metrics import fold_metrics as fold_metrics
from src.analysis.behavior_validation_metrics import (
    qualifying_drive_count as qualifying_drive_count,
)
from src.analysis.behavior_validation_metrics import regime_labels as regime_labels
from src.analysis.behavior_validation_schema import EVALUATOR_VERSION as EVALUATOR_VERSION
from src.analysis.behavior_validation_schema import MODE_LABEL_FIX_AT as MODE_LABEL_FIX_AT
from src.analysis.behavior_validation_schema import MODE_LABEL_FIX_COMMIT as MODE_LABEL_FIX_COMMIT
from src.analysis.behavior_validation_schema import VALIDATION_FILENAME as VALIDATION_FILENAME
from src.analysis.behavior_validation_schema import (
    BehaviorValidationDocument as BehaviorValidationDocument,
)
from src.analysis.behavior_validation_schema import ProvenanceResolution as ProvenanceResolution
from src.analysis.behavior_validation_schema import RegimeAxis as RegimeAxis
from src.analysis.behavior_validation_schema import ReportRegimes as ReportRegimes
from src.analysis.behavior_validation_schema import SteadyStateRegime as SteadyStateRegime
from src.analysis.behavior_validation_schema import _Strict as _Strict
from src.analysis.behavior_validation_schema import decoder_identity as decoder_identity
from src.analysis.behavior_validation_schema import file_identity as file_identity
from src.analysis.behavior_validation_schema import file_sha256 as file_sha256
from src.analysis.behavior_validation_schema import (
    load_validation_document as load_validation_document,
)
from src.analysis.behavior_validation_schema import (
    mode_labels_predate_fix as mode_labels_predate_fix,
)
from src.analysis.behavior_validation_schema import (
    session_content_identity as session_content_identity,
)
from src.analysis.behavior_validation_schema import (
    validation_document_path as validation_document_path,
)
from src.analysis.behavior_validation_sensitivity import (
    _first_evaluated_seconds as _first_evaluated_seconds,
)
from src.analysis.behavior_validation_sensitivity import (
    _first_event_per_window as _first_event_per_window,
)
from src.analysis.behavior_validation_sensitivity import _windows as _windows
from src.analysis.behavior_validation_sensitivity import _windows_with_event as _windows_with_event
from src.analysis.behavior_validation_sensitivity import (
    synthetic_sensitivity as synthetic_sensitivity,
)
from src.analysis.behavior_validation_sensitivity import transient_controls as transient_controls
from src.analysis.splits import DEFAULT_JOIN_GAP_SECONDS as DEFAULT_JOIN_GAP_SECONDS
from src.analysis.splits import Drive as Drive
from src.analysis.splits import DriveSplit as DriveSplit
from src.analysis.splits import LeakedDriveError as LeakedDriveError
from src.analysis.splits import PieceRanges as PieceRanges
from src.analysis.splits import UnorderedSessionsError as UnorderedSessionsError
from src.analysis.splits import chronological_split as chronological_split
from src.analysis.splits import explicit_split as explicit_split
from src.analysis.splits import group_drives as group_drives
from src.analysis.splits import parent_session as parent_session
from src.analysis.splits import piece_time_ranges as piece_time_ranges
from src.analysis.splits import training_leaks as training_leaks

log = logging.getLogger(__name__)

__all__ = [
    "AXIS_PAIRING_COLUMN",
    "AXIS_PAIRING_POLICY_COLUMN",
    "AXIS_SEPARATION_PREFIX",
    "AxisPairingRefusedError",
    "axis_pairing_bounds",
    "axis_separation_column",
    "axis_separation_summary",
    "graded_frame",
    "per_read_frame",
    "read_model_reads",
    "SessionReads",
    "require_axis_pairing_covers",
    "DEFAULT_JOIN_GAP_SECONDS",
    "EVALUATOR_VERSION",
    "PROFILE_EXCLUDED",
    "PROFILE_EXCLUSION_COLUMN",
    "PROFILE_UNVERIFIED",
    "UnverifiedProfileError",
    "BehaviorValidationDocument",
    "Drive",
    "DriveSplit",
    "EventPolicy",
    "LeakedDriveError",
    "ProvenanceResolution",
    "RowEvaluation",
    "UnorderedSessionsError",
    "ValidationContext",
    "ReportRegimes",
    "apply_criteria",
    "band_excess_event_times",
    "breakdown_by_labels",
    "build_evaluation_frame",
    "build_evidence",
    "carry_over_flags",
    "chronological_split",
    "count_events",
    "decoder_identity",
    "event_times",
    "evaluate_rows",
    "explicit_split",
    "fit_band_excess_persistence",
    "fit_baseline",
    "fold_metrics",
    "group_drives",
    "load_validation_document",
    "model_event_times",
    "parent_session",
    "PieceRanges",
    "piece_time_ranges",
    "profile_exclusions",
    "profile_refusals",
    "regime_labels",
    "session_content_identity",
    "stamp_profile_exclusions",
    "synthetic_sensitivity",
    "training_leaks",
    "transient_controls",
    "validation_document_path",
]
