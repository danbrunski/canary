"""Behavior-validation evidence assembly (R20; B02.4/B02.7).

The orchestration boundary of the family: :func:`build_evidence` grades the
untouched test drives (:mod:`.behavior_validation_inputs`,
:mod:`.behavior_validation_evaluation`), measures them
(:mod:`.behavior_validation_metrics`, :mod:`.behavior_validation_sensitivity`)
and turns the numbers into a verdict (:mod:`.behavior_validation_criteria`);
:func:`carry_over_flags` says why a prior kept on disk may no longer be
trusted. Nothing else of the family imports this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd

from src.analysis.behavior_validation_criteria import apply_criteria
from src.analysis.behavior_validation_evaluation import EventPolicy, RowEvaluation, evaluate_rows
from src.analysis.behavior_validation_inputs import SessionReads, graded_frame, profile_refusals
from src.analysis.behavior_validation_metrics import breakdown, fold_metrics, qualifying_drive_count
from src.analysis.behavior_validation_schema import EVALUATOR_VERSION, BehaviorValidationDocument
from src.analysis.behavior_validation_sensitivity import synthetic_sensitivity, transient_controls
from src.analysis.splits import DEFAULT_JOIN_GAP_SECONDS, DriveSplit
from src.behavior.behavior_eligibility import ServingSpec
from src.behavior.behavior_models import (
    BehaviorModel1D,
    BehaviorModel2D,
    ValidationCriteria,
    ValidationEvidence,
    contract_fingerprint,
)
from src.config import CanaryConfig

# ---------------------------------------------------------------------------
# Evidence assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationContext:
    """What a publication route knows beyond the frame (B02.4/B02.7)."""

    policy: EventPolicy = field(default_factory=EventPolicy)
    decoder_identity: Mapping[str, str] = field(default_factory=dict)
    mode_taxonomy: tuple[str, ...] = ()
    criteria_source: str | None = None
    join_gap_seconds: float = DEFAULT_JOIN_GAP_SECONDS
    provenance: Mapping[str, Sequence[str]] = field(default_factory=dict)
    #: Differences between the config's operating modes and the approved
    #: taxonomy (:meth:`BehaviorValidationDocument.taxonomy_problems`). Any
    #: entry keeps the evidence from passing: it was not gathered under the
    #: operator-approved segmentation.
    taxonomy_problems: tuple[str, ...] = ()
    #: B04.9 — the session directories the frame was built from. A model
    #: whose criteria declare ``axis_pairing`` is graded per read from them,
    #: and refused (``axis_pairing_no_reads``) without them.
    reads: SessionReads = field(default_factory=SessionReads)

    @classmethod
    def for_run(
        cls,
        config: CanaryConfig,
        document: BehaviorValidationDocument | None,
        *,
        decoder: Mapping[str, str],
        criteria_source: str | None,
        provenance: Mapping[str, Sequence[str]] | None = None,
        reads: SessionReads | None = None,
    ) -> ValidationContext:
        modes = tuple(config.preprocessing.operating_modes)
        return cls(
            policy=EventPolicy.from_config(config, document),
            decoder_identity=dict(decoder),
            mode_taxonomy=modes,
            criteria_source=criteria_source,
            join_gap_seconds=(
                document.drive_join_gap_seconds
                if document is not None
                else DEFAULT_JOIN_GAP_SECONDS
            ),
            provenance=dict(provenance or {}),
            taxonomy_problems=tuple(document.taxonomy_problems(modes)) if document else (),
            reads=reads if reads is not None else SessionReads(),
        )


def build_evidence(
    model: BehaviorModel1D | BehaviorModel2D,
    *,
    split: DriveSplit,
    test_frame: pd.DataFrame,
    calibration_frame: pd.DataFrame | None,
    criteria: ValidationCriteria | None,
    baseline: Mapping[str, float],
    context: ValidationContext,
    tune_revision: str | None,
    spec: ServingSpec | None = None,
) -> tuple[ValidationEvidence, RowEvaluation]:
    """Score the untouched test drives and turn the numbers into a verdict.

    Refuses (:class:`UnverifiedProfileError`) a test or calibration frame
    holding capture/tuning pieces without a profile verdict: evidence is
    counted on default-profile rows only.

    ``test_frame`` and ``calibration_frame`` are grid frames. A model whose
    ``criteria`` declare ``axis_pairing`` (B04.9) is graded on
    :func:`graded_frame` instead, one row per output read re-read from
    ``context.reads`` — the evaluator's rule — and refused
    (:class:`AxisPairingRefusedError`, ``axis_pairing_no_reads``) where a
    fold's session directory is missing. ``baseline`` is fitted by the caller.
    """
    profile_refusals(test_frame)
    if calibration_frame is not None:
        profile_refusals(calibration_frame)
    policy = context.policy
    pairing = criteria.axis_pairing if criteria is not None else None
    test_frame = graded_frame(
        model, test_frame, pairing, context.reads, interval_seconds=policy.interval_seconds
    )
    if calibration_frame is not None and not calibration_frame.empty:
        calibration_frame = graded_frame(
            model,
            calibration_frame,
            pairing,
            context.reads,
            interval_seconds=policy.interval_seconds,
        )
    drive_of_piece = {
        piece: drive.drive_id
        for drive in (*split.train, *split.calibration, *split.test)
        for piece in drive.pieces
    }
    test_ev = evaluate_rows(model, test_frame, spec=spec, interval_seconds=policy.interval_seconds)
    test = fold_metrics(
        model, test_ev, drive_of_piece=drive_of_piece, policy=policy, baseline=baseline
    )
    calibration = None
    if calibration_frame is not None and not calibration_frame.empty:
        cal_ev = evaluate_rows(
            model, calibration_frame, spec=spec, interval_seconds=policy.interval_seconds
        )
        calibration = fold_metrics(
            model, cal_ev, drive_of_piece=drive_of_piece, policy=policy, baseline=baseline
        )
    sensitivity = (
        synthetic_sensitivity(model, test_frame, test_ev, criteria, policy=policy, spec=spec)
        if criteria is not None and test_ev.evaluated.any()
        else []
    )
    transients = (
        transient_controls(model, test_frame, test_ev, criteria, policy=policy, spec=spec)
        if criteria is not None and test_ev.evaluated.any()
        else []
    )
    minimum = criteria.min_observations_per_drive if criteria is not None else 1
    verdict, failures = apply_criteria(
        criteria,
        test,
        sensitivity,
        qualifying_test_drives=qualifying_drive_count(test_ev, drive_of_piece, minimum),
        transients=transients,
    )
    if context.taxonomy_problems:
        failures = [*failures, *(f"mode_taxonomy:{p}" for p in context.taxonomy_problems)]
        if verdict == "passed":
            verdict = "insufficient_evidence"
    evidence = ValidationEvidence(
        evaluator_version=EVALUATOR_VERSION,
        evaluated_at=datetime.now(UTC).isoformat(),
        verdict=verdict,
        failures=failures,
        criteria=criteria,
        criteria_source=context.criteria_source,
        contract_fingerprint=contract_fingerprint(model),
        decoder_identity=dict(context.decoder_identity),
        mode_taxonomy=list(context.mode_taxonomy),
        tune_revision=tune_revision,
        split=split.record(),
        baseline=dict(baseline),
        calibration=calibration,
        test=test,
        per_drive=breakdown(
            model,
            test_ev,
            by="drive",
            drive_of_piece=drive_of_piece,
            policy=policy,
            baseline=baseline,
        ),
        per_regime=breakdown(
            model,
            test_ev,
            by="regime",
            drive_of_piece=drive_of_piece,
            policy=policy,
            baseline=baseline,
        ),
        sensitivity=sensitivity,
        transient_controls=transients,
        provenance={key: list(values) for key, values in context.provenance.items()},
    )
    return evidence, test_ev


# ---------------------------------------------------------------------------
# Carry-over (B02.7)
# ---------------------------------------------------------------------------


def carry_over_flags(
    prior: BehaviorModel1D | BehaviorModel2D,
    *,
    recipe_contract: str | None,
    criteria: ValidationCriteria | None,
    context: ValidationContext,
) -> list[str]:
    """Why a prior kept on disk may no longer be trusted, without rewriting it.

    Compared against the evidence the prior was published with: a changed
    recipe contract, decoder catalog, mode taxonomy or declared criteria each
    means that evidence no longer describes what would be served.
    """
    flags: list[str] = []
    if recipe_contract is not None and recipe_contract != contract_fingerprint(prior):
        flags.append("contract_changed:recipe")
    evidence = prior.evidence
    if evidence is None:
        return flags
    for key, value in sorted(context.decoder_identity.items()):
        recorded = evidence.decoder_identity.get(key)
        if recorded is not None and recorded != value:
            flags.append(f"decoder_changed:{key}")
    if (
        context.mode_taxonomy
        and evidence.mode_taxonomy
        and sorted(context.mode_taxonomy) != sorted(evidence.mode_taxonomy)
    ):
        flags.append("context_changed:mode_taxonomy")
    if criteria is not None and evidence.criteria is not None and criteria != evidence.criteria:
        flags.append("criteria_changed")
    return flags
