"""Backbone-agnostic training, calibration, and persistence for sequence autoencoders.

Task 2.1 unification: :mod:`src.models.lstm_training` and
:mod:`src.models.tcn_training` were ~90% identical clones — same manifest
shape, same save/load scaffolding, same coercion helpers, same training-loop
call — with exactly two genuine backbone-specific pieces:

* Architecture wiring (``build_model``/``reconstruct_model`` on
  :class:`~src.models.sequence_registry.SequenceTrainingBackend`) — the actual
  kwargs differ (``hidden_size``/``num_layers`` vs.
  ``channels``/``kernel_size``/``num_levels``), so this stays backbone-owned.
* LSTM's ``strict=False`` decay-key load tolerance
  (``SequenceTrainingBackend.tolerated_missing_state_keys``) — the one real
  divergence the refactoring doc names.

Everything else — including the "calibrated-without-scaler" load guard the
doc also named as LSTM-specific, which turned out to already be
byte-identical on both backbones by the time this task landed — is shared and
lives here once. ``lstm_training.py``/``tcn_training.py`` are now thin
backbone-bound façades over this module.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from src.models.manifest import (
    manifest_path_for,
    read_artifact,
    validate_model_feature_spec,
    write_artifact,
)
from src.models.sequence_dataset import (
    OBSERVATION_MASK_SCHEMA_KEY,
    SequenceWindowDataset,
    observation_mask_record,
)
from src.models.sequence_feature_set import context_columns_from_schema
from src.models.sequence_registry import (
    MIN_CALIBRATION_SESSIONS,
    SequenceAutoencoderManifest,
    SequenceTrainingBackend,
    sequence_training_backend_for_model_type,
)
from src.models.sequence_training import (
    MIN_CALIBRATION_SAMPLES,
    SequenceThresholdCalibration,
    ValidationChoice,
    ValidationSplit,
    calibrate_sequence_thresholds,
    coerce_timestamp,
    cpu_state_dict,
    dataloader_kwargs_for_device,
    envelope_coverage_violations,
    evaluate_loss,
    max_window_timestamp,
    merge_envelopes,
    min_window_timestamp,
    observed_block_counts,
    train_one_epoch,
    training_data_hash,
    training_sessions,
    validate_dataset_observations,
    validation_split,
    window_blocks,
    window_modes,
)
from src.models.sequence_training import EpochLoss as _EpochLoss
from src.preprocessing.feature_pipeline import build_feature_spec
from src.preprocessing.normalization import ZScoreScaler
from src.timefmt import format_timestamp as _format_timestamp

log = logging.getLogger(__name__)

ARTIFACT_FORMAT_VERSION = 1


class CalibrationCoverageError(ValueError):
    """The calibration cannot stand behind the thresholds this model would serve.

    Raised before training (A02.2, A02.12), so a thin holdout costs seconds,
    not the hours of a training run. ``violations`` names each gap.
    """

    def __init__(self, violations: Sequence[str]) -> None:
        self.violations = tuple(violations)
        super().__init__(
            "refusing to calibrate thresholds that do not cover what they will score: "
            + "; ".join(self.violations)
            + ". Widen the calibration holdout, or pass calibration_override='<reason>' "
            "to publish anyway (the reason and these violations are stamped into the "
            "manifest's calibration_holdout)."
        )


def _calibration_drive_count(calibration: SequenceWindowDataset, sessions: Sequence[str]) -> int:
    """Physical drives the calibration windows come from
    (:func:`src.analysis.drives.count_drives` over the rows of the windowed
    pieces: the stamped drive where the frame carries one)."""
    from src.analysis.drives import count_drives
    from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN

    frame = calibration.frame
    if SESSION_COLUMN not in frame.columns or TIMESTAMP_COLUMN not in frame.columns:
        return len(sessions)
    rows = frame.loc[frame[SESSION_COLUMN].astype(str).isin(set(sessions))]
    return count_drives(rows, session_column=SESSION_COLUMN, timestamp_column=TIMESTAMP_COLUMN)


def calibration_coverage_violations(
    training: SequenceWindowDataset,
    calibration: SequenceWindowDataset,
    *,
    held_out: bool,
    training_envelope: Mapping[str, Any] | None = None,
    calibration_envelope: Mapping[str, Any] | None = None,
    context_columns: Sequence[str] = (),
) -> list[str]:
    """What a calibration fails to cover, before anything is trained.

    * fewer than :data:`MIN_CALIBRATION_SESSIONS` *physical drives*
      (:func:`src.analysis.drives.count_drives`) — held-out only;
    * an operating mode the model will score (at least
      :data:`MIN_CALIBRATION_SAMPLES` independent training windows) that the
      calibration gives no band of its own — held-out only;
    * a channel the model is trained to score that is never observed in any
      calibration window (context inputs, A03.2, are not scored);
    * (A03.8) a scored mode whose training envelope — baro, IAT, speed — the
      calibration's range in that mode spans less than
      ``sequence_training.MIN_ENVELOPE_COVERAGE`` of — held-out only, when the
      training envelope was recorded.

    The 2026-09-20 artifact failed all three: one session, city and warmup
    only, and two injector-pulse channels with no calibration observations.
    """
    violations: list[str] = []
    if held_out:
        windowed = sorted({window.session for window in calibration.windows})
        drives = _calibration_drive_count(calibration, windowed)
        if drives < MIN_CALIBRATION_SESSIONS:
            violations.append(
                f"calibration rests on {drives} physical drive(s) "
                f"({', '.join(windowed)}); {MIN_CALIBRATION_SESSIONS} are required"
            )
        trained_modes = _modes_with_blocks(training)
        calibrated_modes = _modes_with_blocks(calibration)
        uncovered = sorted(trained_modes - calibrated_modes)
        if uncovered:
            violations.append(
                f"operating mode(s) {uncovered} are scored by the model but have fewer than "
                f"{MIN_CALIBRATION_SAMPLES} independent calibration windows "
                f"(calibration covers {sorted(calibrated_modes) or 'none'})"
            )
        if training_envelope is not None:
            if calibration_envelope is None:
                violations.append(
                    "calibration envelope not recorded, so its coverage of the training "
                    "envelope cannot be checked (build the calibration dataset with "
                    "session_envelopes)"
                )
            else:
                violations.extend(
                    envelope_coverage_violations(
                        training_envelope, calibration_envelope, modes=sorted(trained_modes)
                    )
                )
    context = set(context_columns)
    unobserved = sorted(
        c for c, n in observed_block_counts(calibration).items() if n == 0 and c not in context
    )
    if unobserved:
        violations.append(f"channel(s) never observed in calibration: {unobserved}")
    return violations


def _modes_with_blocks(dataset: SequenceWindowDataset) -> set[str]:
    blocks = window_blocks(dataset)
    return {
        mode
        for mode, indexes in window_modes(dataset).items()
        if len(set(blocks[indexes].tolist())) >= MIN_CALIBRATION_SAMPLES
    }


class SequenceAutoencoderTrainingConfigLike(Protocol):
    """Config surface every sequence-autoencoder backbone's training needs.

    Backbone-specific architecture knobs (``hidden_size``/``num_layers`` for
    LSTM; ``channels``/``kernel_size``/``num_levels`` for TCN) aren't listed
    here — only each backend's own ``build_model`` closure reads those.
    """

    enabled: bool
    sequence_length: int
    batch_size: int
    learning_rate: float
    max_epochs: int
    validation_fraction: float
    early_stopping_patience: int
    min_delta: float
    random_state: int
    threshold_k: float


@dataclass(frozen=True)
class TrainedSequenceAutoencoder:
    """A trained sequence autoencoder (either backbone) plus its manifest."""

    model: Any
    manifest: SequenceAutoencoderManifest

    @property
    def feature_columns(self) -> tuple[str, ...]:
        return tuple(str(column) for column in self.manifest.schema["feature_columns"])

    def save(self, artifact_path: Path, manifest_path: Path | None = None) -> None:
        """Write the Torch checkpoint and JSON manifest."""
        save_sequence_autoencoder_model(self, artifact_path, manifest_path=manifest_path)


def train_sequence_autoencoder(
    backend: SequenceTrainingBackend,
    dataset: SequenceWindowDataset,
    config: SequenceAutoencoderTrainingConfigLike,
    *,
    device: str = "cpu",
    dtype: str = "float32",
    model_version: str | None = None,
    trained_at: datetime | pd.Timestamp | str | None = None,
    calibrate_thresholds: bool = True,
    calibration_dataset: SequenceWindowDataset | None = None,
    feature_spec: Mapping[str, Any] | None = None,
    scaler: ZScoreScaler | None = None,
    allow_in_sample_calibration: bool = False,
    calibration_sessions_requested: Sequence[str] | None = None,
    calibration_override: str | None = None,
    validation_choice: ValidationChoice | None = None,
    scaler_fit_sessions: Sequence[str] | None = None,
    scaler_fit_until: Mapping[str, str] | None = None,
) -> TrainedSequenceAutoencoder:
    """Train ``backend``'s autoencoder on clean sequence windows.

    The caller is responsible for passing only clean-baseline sessions into the
    ``SequenceWindowDataset``. The loop uses masked-MSE loss, an optional
    validation split, and early stopping on validation loss when validation
    rows exist — identical between backbones (:mod:`src.models.sequence_training`).

    ``calibration_dataset`` (when given) is a *held-out* clean session, scaled
    with the same scaler, that the reconstruction-error thresholds are
    calibrated on instead of the training windows — out-of-sample calibration
    avoids the cross-session false-alarm flood that in-sample thresholds cause.

    Task 3.1: omitting ``calibration_dataset`` now raises unless the caller
    explicitly opts in with ``allow_in_sample_calibration=True`` — in-sample
    calibration was the exact mechanism behind an earlier FPR≈1.0 flood, and
    it should never be a silent default. The escape hatch stamps
    ``calibration="in_sample"`` into the manifest so a load-time consumer
    (``ModelManager``) can warn on it.

    A02.2/A02.12: before training, the calibration is checked against what the
    model will score (:func:`calibration_coverage_violations`) and a gap raises
    :class:`CalibrationCoverageError` — it used to publish with a WARNING.
    ``calibration_override`` (the operator's reason, required non-empty)
    publishes anyway and stamps the reason and every violation into
    ``calibration_holdout["override"]``.

    Phases (R04.10): :func:`_preflight` (refusals), :func:`_plan_training`
    (validation split and envelopes), :func:`_setup_execution` (model, loaders,
    AMP, compile), :func:`_run_epochs` (the loop with early stopping),
    :func:`_build_sequence_manifest`, then :func:`_calibrate_and_stamp`. Torch
    stays lazy: the helpers receive the modules they need.
    """
    preflight = _preflight(
        backend,
        dataset,
        config,
        calibrate_thresholds=calibrate_thresholds,
        calibration_dataset=calibration_dataset,
        allow_in_sample_calibration=allow_in_sample_calibration,
        feature_spec=feature_spec,
    )
    plan = _plan_training(
        backend,
        dataset,
        config,
        preflight,
        calibrate_thresholds=calibrate_thresholds,
        calibration_dataset=calibration_dataset,
        scaler=scaler,
        calibration_override=calibration_override,
        validation_choice=validation_choice,
        scaler_fit_sessions=scaler_fit_sessions,
        scaler_fit_until=scaler_fit_until,
    )
    torch, _nn, optim, data = _torch_training_modules(backend)
    execution = _setup_execution(
        backend,
        dataset,
        config,
        plan,
        context_columns=preflight.context_columns,
        device=device,
        dtype=dtype,
        torch=torch,
        optim=optim,
        data=data,
    )
    outcome = _run_epochs(execution, config)
    if outcome.best_state is None:
        raise RuntimeError(f"{backend.model_type} training did not produce a checkpoint")
    model = execution.model
    model.load_state_dict(outcome.best_state)
    model.to(execution.device)
    model.eval()

    created_at = _format_timestamp(coerce_timestamp(trained_at))
    manifest = _build_sequence_manifest(
        backend,
        dataset,
        config,
        plan,
        execution,
        outcome,
        created_at=created_at,
        model_version=model_version,
        feature_spec=feature_spec,
        scaler=scaler,
        dtype=dtype,
    )
    trained = TrainedSequenceAutoencoder(model=model, manifest=manifest)
    if not calibrate_thresholds:
        return trained
    return _calibrate_and_stamp(
        backend,
        trained,
        dataset,
        config,
        preflight,
        plan,
        calibration_dataset=calibration_dataset,
        calibration_sessions_requested=calibration_sessions_requested,
        created_at=created_at,
        device=device,
    )


@dataclass(frozen=True)
class _Preflight:
    """What the refusals resolved: calibration provenance, the windows the
    thresholds come from, the context columns kept out of the loss, and the
    observation floors both datasets are held to."""

    calibration_provenance: str | None
    calibration_source: SequenceWindowDataset
    context_columns: tuple[str, ...]
    minimum_window_fraction: float
    minimum_usable_fraction: float


def _preflight(
    backend: SequenceTrainingBackend,
    dataset: SequenceWindowDataset,
    config: SequenceAutoencoderTrainingConfigLike,
    *,
    calibrate_thresholds: bool,
    calibration_dataset: SequenceWindowDataset | None,
    allow_in_sample_calibration: bool,
    feature_spec: Mapping[str, Any] | None,
) -> _Preflight:
    """Phase 1: refuse what cannot train — in-sample calibration without the
    opt-in, a mask mismatch between training and calibration, unknown context
    columns, unusable observation coverage."""
    _validate_training_config(backend, config)
    if len(dataset) == 0:
        raise ValueError(f"no sequence windows available to train {backend.model_type}")
    if calibrate_thresholds and calibration_dataset is None and not allow_in_sample_calibration:
        raise ValueError(
            f"train_{backend.selector}_autoencoder: no calibration_dataset given. In-sample "
            "threshold calibration underestimates cross-session reconstruction "
            "error and can flood alerts on unseen drives — pass a held-out "
            "calibration_dataset, or explicitly opt in with "
            "allow_in_sample_calibration=True."
        )
    calibration_provenance: str | None
    if not calibrate_thresholds:
        calibration_provenance = None
    elif calibration_dataset is not None:
        calibration_provenance = "held_out"
    else:
        calibration_provenance = "in_sample"

    minimum_window_fraction = float(getattr(config, "min_window_observed_fraction", 0.01))
    minimum_usable_fraction = float(getattr(config, "min_usable_window_fraction", 0.9))
    validate_dataset_observations(
        dataset,
        minimum_window_fraction=minimum_window_fraction,
        minimum_usable_fraction=minimum_usable_fraction,
        context="training dataset",
    )
    calibration_source = calibration_dataset if calibration_dataset is not None else dataset
    # A03.1 — thresholds are only comparable to serving scores when the
    # calibration windows carry the mask the model was trained on.
    if calibration_source.observation_mask != dataset.observation_mask:
        raise ValueError(
            f"calibration dataset uses observation mask {calibration_source.observation_mask!r} "
            f"but the training dataset uses {dataset.observation_mask!r}"
        )
    # A03.2 — context inputs recorded in the feature spec condition the encoder
    # and are kept out of the loss, the thresholds and the score.
    context_columns = context_columns_from_schema(feature_spec or {})
    unknown_context = sorted(set(context_columns) - set(dataset.feature_columns))
    if unknown_context:
        raise ValueError(f"feature spec context columns not in the dataset: {unknown_context}")
    return _Preflight(
        calibration_provenance=calibration_provenance,
        calibration_source=calibration_source,
        context_columns=tuple(context_columns),
        minimum_window_fraction=minimum_window_fraction,
        minimum_usable_fraction=minimum_usable_fraction,
    )


@dataclass(frozen=True)
class _TrainingPlan:
    """The validation split and everything recorded about it, fixed before
    anything trains."""

    split: ValidationSplit
    validation_record: dict[str, Any] | None
    train_session_names: list[str]
    training_envelope: Any
    calibration_envelope: Any
    override_record: dict[str, Any] | None
    calibration_health: dict[str, Any] | None


def _plan_training(
    backend: SequenceTrainingBackend,
    dataset: SequenceWindowDataset,
    config: SequenceAutoencoderTrainingConfigLike,
    preflight: _Preflight,
    *,
    calibrate_thresholds: bool,
    calibration_dataset: SequenceWindowDataset | None,
    scaler: ZScoreScaler | None,
    calibration_override: str | None,
    validation_choice: ValidationChoice | None,
    scaler_fit_sessions: Sequence[str] | None,
    scaler_fit_until: Mapping[str, str] | None,
) -> _TrainingPlan:
    """Phase 2: the validation split (A03.5), the scaler-leak check (A03.4),
    the envelopes (A03.8) and the calibration coverage gate (A02.2/A02.12)."""
    # A03.5 — validation drives by the stated rule, fixed before anything
    # trains; A03.4 — the scaler the caller fit must not have seen them.
    split = validation_split(
        dataset,
        validation_fraction=config.validation_fraction,
        random_state=config.random_state,
        choice=validation_choice,
    )
    validation_record = None if split.record is None else dict(split.record)
    train_session_names = sorted({dataset.windows[i].session for i in split.train_indexes})
    if validation_record is not None:
        if scaler_fit_sessions is not None:
            # ``scaler_fit_until``: the last timestamp the scaler read from each
            # session. A validation window that starts after it did not move
            # the scaler, which is how a tail-block fallback (validation and
            # training in one drive) still honours A03.4.
            until = {str(k): pd.Timestamp(v) for k, v in (scaler_fit_until or {}).items()}
            leaked = sorted(
                {
                    dataset.windows[i].session
                    for i in split.validation_indexes
                    if dataset.windows[i].session in {str(x) for x in scaler_fit_sessions}
                    and (
                        dataset.windows[i].session not in until
                        or pd.Timestamp(dataset.windows[i].start_timestamp)
                        <= until[dataset.windows[i].session]
                    )
                }
            )
            if leaked:
                raise ValueError(
                    f"the scaler was fit on validation session(s) {leaked}: fit it on the "
                    "training sessions only (A03.4)"
                )
            validation_record["scaler_fit_excludes_validation"] = True
        else:
            validation_record["scaler_fit_excludes_validation"] = scaler is None
    # A03.8 — the envelope of what the model is fit on (validation drives excluded).
    training_envelope = (
        merge_envelopes(dataset.session_envelopes, train_session_names)
        if dataset.session_envelopes is not None
        else None
    )
    calibration_envelope = (
        merge_envelopes(calibration_dataset.session_envelopes)
        if calibration_dataset is not None and calibration_dataset.session_envelopes is not None
        else None
    )
    override_record: dict[str, Any] | None = None
    if calibrate_thresholds:
        violations = calibration_coverage_violations(
            dataset,
            preflight.calibration_source,
            held_out=calibration_dataset is not None,
            training_envelope=training_envelope,
            calibration_envelope=calibration_envelope,
            context_columns=preflight.context_columns,
        )
        if violations:
            if calibration_override is None or not calibration_override.strip():
                raise CalibrationCoverageError(violations)
            log.warning(
                "%s calibration published under operator override: %s",
                backend.model_type,
                "; ".join(violations),
                extra={"override_reason": calibration_override},
            )
            override_record = {"reason": calibration_override.strip(), "violations": violations}
    calibration_health = (
        validate_dataset_observations(
            preflight.calibration_source,
            minimum_window_fraction=preflight.minimum_window_fraction,
            minimum_usable_fraction=preflight.minimum_usable_fraction,
            context="calibration dataset",
        )
        if calibrate_thresholds
        else None
    )
    return _TrainingPlan(
        split=split,
        validation_record=validation_record,
        train_session_names=train_session_names,
        training_envelope=training_envelope,
        calibration_envelope=calibration_envelope,
        override_record=override_record,
        calibration_health=calibration_health,
    )


@dataclass
class _Execution:
    """The model and everything that drives it through the epochs."""

    torch: Any
    device: Any
    model: Any
    execution_model: Any
    optimizer: Any
    train_loader: Any
    validation_loader: Any
    train_count: int
    validation_count: int
    loss_weights: Any
    amp_dtype: Any
    grad_scaler: Any
    loader_kwargs: dict[str, Any]


def _setup_execution(
    backend: SequenceTrainingBackend,
    dataset: SequenceWindowDataset,
    config: SequenceAutoencoderTrainingConfigLike,
    plan: _TrainingPlan,
    *,
    context_columns: Sequence[str],
    device: str,
    dtype: str,
    torch: Any,
    optim: Any,
    data: Any,
) -> _Execution:
    """Phase 3: seed, model, loaders, loss weights, optimizer, AMP, compile —
    in the order the RNG has always been consumed, so a fixed seed trains the
    same weights."""
    torch.manual_seed(config.random_state)

    device_obj = torch.device(device)
    model = backend.build_model(
        config,
        input_size=len(dataset.feature_columns),
        device=device,
        dtype=dtype,
    )
    train_subset = data.Subset(dataset, plan.split.train_indexes)
    validation_subset = (
        data.Subset(dataset, plan.split.validation_indexes)
        if plan.split.validation_indexes
        else None
    )
    loss_weights = None
    if context_columns:
        context_set = set(context_columns)
        loss_weights = torch.tensor(
            [0.0 if column in context_set else 1.0 for column in dataset.feature_columns],
            device=device_obj,
        )
    loader_kwargs = dataloader_kwargs_for_device(
        device, num_workers=getattr(config, "dataloader_workers", None)
    )
    train_loader = data.DataLoader(
        train_subset,
        batch_size=config.batch_size,
        shuffle=True,
        generator=torch.Generator().manual_seed(config.random_state),
        **loader_kwargs,
    )
    validation_loader = (
        None
        if validation_subset is None
        else data.DataLoader(
            validation_subset,
            batch_size=config.batch_size,
            shuffle=False,
            **loader_kwargs,
        )
    )

    optimizer = optim.Adam(model.parameters(), lr=config.learning_rate)
    amp_dtype = None
    grad_scaler = None
    if str(device_obj).startswith("cuda") and getattr(config, "mixed_precision", False):
        amp_dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        if amp_dtype == torch.float16:
            grad_scaler = torch.amp.GradScaler("cuda")
    execution_model = model
    if getattr(config, "compile_model", False) and hasattr(torch, "compile"):
        execution_model = torch.compile(model)
    return _Execution(
        torch=torch,
        device=device_obj,
        model=model,
        execution_model=execution_model,
        optimizer=optimizer,
        train_loader=train_loader,
        validation_loader=validation_loader,
        train_count=len(train_subset),
        validation_count=0 if validation_subset is None else len(validation_subset),
        loss_weights=loss_weights,
        amp_dtype=amp_dtype,
        grad_scaler=grad_scaler,
        loader_kwargs=loader_kwargs,
    )


@dataclass(frozen=True)
class _FitOutcome:
    """The best checkpoint the epoch loop kept, and how it got there."""

    best_epoch: int
    best_train_loss: float
    best_validation_loss: float | None
    best_state: dict[str, Any] | None
    history: tuple[_EpochLoss, ...]
    early_stopped: bool


def _run_epochs(
    execution: _Execution, config: SequenceAutoencoderTrainingConfigLike
) -> _FitOutcome:
    """Phase 4: train until ``max_epochs`` or ``early_stopping_patience``
    epochs without a ``min_delta`` improvement on the monitored loss
    (validation when there is any, training otherwise)."""
    best_epoch = 0
    best_train_loss = float("inf")
    best_validation_loss: float | None = None
    best_monitor_loss = float("inf")
    best_state: dict[str, Any] | None = None
    epochs_without_improvement = 0
    history: list[_EpochLoss] = []
    early_stopped = False

    for epoch in range(1, config.max_epochs + 1):
        train_loss = train_one_epoch(
            execution.execution_model,
            execution.train_loader,
            optimizer=execution.optimizer,
            device=execution.device,
            amp_dtype=execution.amp_dtype,
            grad_scaler=execution.grad_scaler,
            loss_weights=execution.loss_weights,
        )
        validation_loss = (
            None
            if execution.validation_loader is None
            else evaluate_loss(
                execution.execution_model,
                execution.validation_loader,
                device=execution.device,
                amp_dtype=execution.amp_dtype,
                loss_weights=execution.loss_weights,
            )
        )
        history.append(
            _EpochLoss(epoch=epoch, train_loss=train_loss, validation_loss=validation_loss)
        )

        monitor_loss = train_loss if validation_loss is None else validation_loss
        if monitor_loss < best_monitor_loss - config.min_delta:
            best_epoch = epoch
            best_train_loss = train_loss
            best_validation_loss = validation_loss
            best_monitor_loss = monitor_loss
            best_state = cpu_state_dict(execution.model)
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1
            if epochs_without_improvement >= config.early_stopping_patience:
                early_stopped = True
                break
    return _FitOutcome(
        best_epoch=best_epoch,
        best_train_loss=best_train_loss,
        best_validation_loss=best_validation_loss,
        best_state=best_state,
        history=tuple(history),
        early_stopped=early_stopped,
    )


def _build_sequence_manifest(
    backend: SequenceTrainingBackend,
    dataset: SequenceWindowDataset,
    config: SequenceAutoencoderTrainingConfigLike,
    plan: _TrainingPlan,
    execution: _Execution,
    outcome: _FitOutcome,
    *,
    created_at: str,
    model_version: str | None,
    feature_spec: Mapping[str, Any] | None,
    scaler: ZScoreScaler | None,
    dtype: str,
) -> SequenceAutoencoderManifest:
    """Phase 5: the uncalibrated manifest."""
    version = model_version or f"{backend.model_type}_{created_at.replace(':', '')}"
    schema = (
        dict(feature_spec)
        if feature_spec is not None
        else build_feature_spec(
            dataset.frame,
            dataset.feature_columns,
            sequence_length=dataset.sequence_length,
            sequence_stride=dataset.stride,
        )
    )
    validate_model_feature_spec(schema, context=f"{backend.model_type} training feature spec")
    schema["sequence_length"] = dataset.sequence_length
    # A03.1 — the mask these windows carried; serving reads it back.
    schema[OBSERVATION_MASK_SCHEMA_KEY] = observation_mask_record(
        dataset.observation_mask, dataset.freshness_fallback_columns
    )
    return SequenceAutoencoderManifest(
        artifact_format_version=ARTIFACT_FORMAT_VERSION,
        model_type=backend.model_type,
        model_version=version,
        created_at=created_at,
        training_data_hash=training_data_hash(dataset),
        training_start_timestamp=min_window_timestamp(dataset),
        training_end_timestamp=max_window_timestamp(dataset),
        training_sessions=training_sessions(dataset),
        input_rows=len(dataset.frame),
        input_windows=len(dataset),
        train_windows=execution.train_count,
        validation_windows=execution.validation_count,
        best_epoch=outcome.best_epoch,
        best_train_loss=outcome.best_train_loss,
        best_validation_loss=outcome.best_validation_loss,
        stopped_epoch=outcome.history[-1].epoch,
        early_stopped=outcome.early_stopped,
        architecture=execution.model.architecture.to_dict(),
        hyperparams={
            "batch_size": config.batch_size,
            "learning_rate": config.learning_rate,
            "max_epochs": config.max_epochs,
            "validation_fraction": config.validation_fraction,
            "early_stopping_patience": config.early_stopping_patience,
            "min_delta": config.min_delta,
            "random_state": config.random_state,
            "loss": "masked_mse",
            "optimizer": "adam",
            "device": str(execution.device),
            "dtype": dtype,
            "mixed_precision": execution.amp_dtype is not None,
            "compiled": execution.execution_model is not execution.model,
            "dataloader_workers": execution.loader_kwargs["num_workers"],
        },
        schema=schema,
        history=outcome.history,
        scaler=_resolve_manifest_scaler(scaler, dataset, plan.train_session_names).to_dict(),
        validation_holdout=plan.validation_record,
        training_envelope=plan.training_envelope,
        calibration_envelope=plan.calibration_envelope,
    )


def _calibrate_and_stamp(
    backend: SequenceTrainingBackend,
    trained: TrainedSequenceAutoencoder,
    dataset: SequenceWindowDataset,
    config: SequenceAutoencoderTrainingConfigLike,
    preflight: _Preflight,
    plan: _TrainingPlan,
    *,
    calibration_dataset: SequenceWindowDataset | None,
    calibration_sessions_requested: Sequence[str] | None,
    created_at: str,
    device: str,
) -> TrainedSequenceAutoencoder:
    """Phase 6: thresholds, then the calibration provenance stamped on."""
    calibrated = calibrate_sequence_autoencoder_thresholds(
        trained,
        calibration_dataset if calibration_dataset is not None else dataset,
        threshold_k=config.threshold_k,
        batch_size=config.batch_size,
        device=device,
        calibrated_at=created_at,
        threshold_quantile=getattr(config, "threshold_quantile", None),
        training_observed_blocks=observed_block_counts(dataset),
        context_columns=preflight.context_columns,
    )
    used_sessions = training_sessions(preflight.calibration_source)
    holdout = (
        _calibration_holdout_report(
            backend, used=used_sessions, requested=calibration_sessions_requested
        )
        if calibration_dataset is not None
        else None
    )
    if plan.override_record is not None:
        holdout = {**(holdout or {}), "override": plan.override_record}
    stamped = replace(
        calibrated.manifest,
        calibration=preflight.calibration_provenance,
        calibration_sessions=used_sessions,
        calibration_health=plan.calibration_health,
        calibration_holdout=holdout,
    )
    return TrainedSequenceAutoencoder(model=calibrated.model, manifest=stamped)


def _calibration_holdout_report(
    backend: SequenceTrainingBackend,
    *,
    used: Sequence[str],
    requested: Sequence[str] | None,
) -> dict[str, Any]:
    """How many held-out sessions the thresholds actually rest on — and say so
    when it is too few, in the manifest and at WARNING, instead of a quiet
    one-session calibration that reads like any other ``held_out`` artifact."""
    requested_names = list(used) if requested is None else [str(name) for name in requested]
    without_windows = sorted(set(requested_names) - set(used))
    below_minimum = len(used) < MIN_CALIBRATION_SESSIONS
    if below_minimum or without_windows:
        log.warning(
            "%s thresholds calibrated on %d held-out session(s) (%d recommended)%s",
            backend.model_type,
            len(used),
            MIN_CALIBRATION_SESSIONS,
            f"; requested sessions that produced no windows: {without_windows}"
            if without_windows
            else "",
            extra={"calibration_sessions": list(used), "requested": requested_names},
        )
    return {
        "sessions_requested": requested_names,
        "sessions_used": list(used),
        "sessions_without_windows": without_windows,
        "recommended_minimum_sessions": MIN_CALIBRATION_SESSIONS,
        "below_recommended_minimum": below_minimum,
    }


def calibrate_sequence_autoencoder_thresholds(
    trained: TrainedSequenceAutoencoder,
    dataset: SequenceWindowDataset,
    *,
    threshold_k: float,
    batch_size: int = 64,
    device: str = "cpu",
    calibrated_at: datetime | pd.Timestamp | str | None = None,
    threshold_quantile: float | None = None,
    training_observed_blocks: Mapping[str, int] | None = None,
    context_columns: Sequence[str] = (),
) -> TrainedSequenceAutoencoder:
    """Attach masked reconstruction-error thresholds to a trained bundle.

    Thin wrapper over :func:`~src.models.sequence_training.calibrate_sequence_thresholds`
    (already shared between both backbones) that writes the calibration into
    the unified manifest.
    """
    calibration = calibrate_sequence_thresholds(
        trained.model,
        dataset,
        feature_columns=trained.feature_columns,
        threshold_k=threshold_k,
        batch_size=batch_size,
        device=device,
        calibrated_at=calibrated_at,
        threshold_quantile=threshold_quantile,
        training_observed_blocks=training_observed_blocks,
        context_columns=context_columns,
    )
    manifest = replace(trained.manifest, thresholds=calibration.to_dict())
    return TrainedSequenceAutoencoder(model=trained.model, manifest=manifest)


def save_sequence_autoencoder_model(
    trained: TrainedSequenceAutoencoder,
    artifact_path: Path,
    *,
    manifest_path: Path | None = None,
) -> None:
    """Persist a trained checkpoint and manifest (either backbone)."""
    backend = sequence_training_backend_for_model_type(trained.manifest.model_type)
    torch = _torch_module(backend)
    manifest = trained.manifest.to_dict()
    payload = {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "model_type": trained.manifest.model_type,
        "state_dict": cpu_state_dict(trained.model),
        "manifest": manifest,
    }
    write_artifact(
        artifact_path,
        lambda path: torch.save(payload, path),
        manifest,
        manifest_path=manifest_path,
    )


def load_sequence_autoencoder_model(
    backend: SequenceTrainingBackend,
    artifact_path: Path,
    *,
    device: str = "cpu",
    dtype: str = "float32",
) -> TrainedSequenceAutoencoder:
    """Load a checkpoint written by ``save_sequence_autoencoder_model``."""
    torch = _torch_module(backend)
    payload = read_artifact(
        artifact_path,
        lambda path: torch.load(path, map_location=device, weights_only=False),
        model_type=backend.model_type,
        expected_format_version=ARTIFACT_FORMAT_VERSION,
    )

    manifest = SequenceAutoencoderManifest.from_dict(payload["manifest"])
    feature_columns = validate_model_feature_spec(
        manifest.schema,
        context=f"{backend.model_type} manifest {artifact_path}",
    )
    _validate_loaded_manifest(backend, manifest, feature_columns=feature_columns)
    model = backend.reconstruct_model(manifest.architecture)

    if backend.tolerated_missing_state_keys:
        # Some checkpoints predate a since-added parameter (LSTM: the GRU-D
        # decay_input_target). Loading strict=False and tolerating exactly the
        # documented key set lets those old artifacts still load; anything
        # else missing or unexpected is a real mismatch and must still fail
        # loudly, so this is never a blanket strict=False.
        missing, unexpected = model.load_state_dict(payload["state_dict"], strict=False)
        if unexpected or not set(missing) <= backend.tolerated_missing_state_keys:
            raise RuntimeError(
                f"{backend.model_type} checkpoint state dict mismatch: "
                f"missing={sorted(missing)} unexpected={sorted(unexpected)}"
            )
    else:
        model.load_state_dict(payload["state_dict"])
    model.to(device=torch.device(device), dtype=getattr(torch, dtype))
    model.eval()
    return TrainedSequenceAutoencoder(model=model, manifest=manifest)


def default_sequence_manifest_path(artifact_path: Path) -> Path:
    """Return the conventional JSON manifest path for a checkpoint."""
    return manifest_path_for(artifact_path)


def _validate_loaded_manifest(
    backend: SequenceTrainingBackend,
    manifest: SequenceAutoencoderManifest,
    *,
    feature_columns: tuple[str, ...],
) -> None:
    architecture = manifest.architecture
    input_size = _required_int(
        architecture, "input_size", context=f"{backend.model_type} architecture"
    )
    if input_size != len(feature_columns):
        raise ValueError(
            f"{backend.model_type} architecture input_size does not match manifest "
            f"feature_columns: input_size={input_size}, feature_columns={len(feature_columns)}"
        )

    architecture_sequence_length = _required_int(
        architecture,
        "sequence_length",
        context=f"{backend.model_type} architecture",
    )
    schema_sequence_length = _required_int(
        manifest.schema,
        "sequence_length",
        context=f"{backend.model_type} feature schema",
    )
    if architecture_sequence_length != schema_sequence_length:
        raise ValueError(
            f"{backend.model_type} architecture sequence_length does not match manifest "
            f"schema: architecture={architecture_sequence_length}, schema={schema_sequence_length}"
        )

    if manifest.thresholds is not None:
        _validate_thresholds(backend, manifest.thresholds, feature_columns=feature_columns)
        # The threshold is computed on the model's reconstruction error
        # distribution during calibration. If training applied a scaler,
        # those errors are in *scaled* units; if it didn't, they're in raw
        # OBD/CAN units. Inference must match — and the only way it knows
        # which is which is via the saved scaler. A calibrated manifest
        # without a scaler is the May 2026 degenerate-fit artifact shape:
        # threshold of 171,895 against live scores of 87, completely
        # unworkable. Refuse it loudly at load. Shared by both backbones —
        # despite the refactoring doc naming this LSTM-specific, the TCN
        # trainer already carried a byte-identical guard by the time this
        # task landed.
        if manifest.scaler is None:
            raise ValueError(
                f"{backend.model_type} manifest has calibrated thresholds but no scaler — "
                "the threshold scale won't match inference. Retrain on "
                "current code so the artifact carries a scaler dict."
            )
        _validate_scaler(backend, manifest.scaler, feature_columns=feature_columns)


def _resolve_manifest_scaler(
    scaler: ZScoreScaler | None,
    dataset: SequenceWindowDataset,
    train_sessions: Sequence[str] | None = None,
) -> ZScoreScaler:
    """Pick the scaler to embed in the manifest.

    Production callers (``scripts/train_sequence.py``) fit a scaler on the raw
    feature frame, apply it, then pass the fitted scaler in — that one
    represents raw → scaled and is what inference applies to incoming
    windows. Test callers don't bother and pass the dataset's frame
    pre-scaled; for those, we fit a fresh scaler on whatever the dataset
    actually contains so the manifest stays self-consistent (means/stds
    match the model's expected input distribution). The post-load guard
    requires *some* scaler to exist, so this branch ensures we never write
    an artifact without one.
    """
    if scaler is not None:
        return scaler
    frame = dataset.frame
    if train_sessions:
        # A03.4: validation rows never move the scaler.
        frame = frame.loc[frame["session"].astype(str).isin(set(train_sessions))]
    return ZScoreScaler.fit(frame, columns=dataset.feature_columns)


def _validate_scaler(
    backend: SequenceTrainingBackend,
    scaler: Mapping[str, Any],
    *,
    feature_columns: tuple[str, ...],
) -> None:
    """Round-trip the scaler dict + ensure every feature column has stats."""
    try:
        loaded = ZScoreScaler.from_dict(dict(scaler))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {backend.model_type} scaler in manifest: {exc}") from exc
    missing = [column for column in feature_columns if column not in loaded.means]
    if missing:
        raise ValueError(
            f"{backend.model_type} scaler missing means for: " + ", ".join(sorted(missing))
        )


def _validate_thresholds(
    backend: SequenceTrainingBackend,
    thresholds: Mapping[str, Any],
    *,
    feature_columns: tuple[str, ...],
) -> None:
    try:
        calibration = SequenceThresholdCalibration.from_dict(thresholds)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {backend.model_type} thresholds in manifest: {exc}") from exc

    if not math.isfinite(calibration.global_error.threshold):
        raise ValueError(f"{backend.model_type} thresholds global_error.threshold must be finite")
    missing = [column for column in feature_columns if column not in calibration.channel_errors]
    if missing:
        raise ValueError(
            f"{backend.model_type} thresholds missing channel_errors for: " + ", ".join(missing)
        )
    for column in feature_columns:
        threshold = calibration.channel_errors[column].threshold
        if not math.isfinite(threshold):
            raise ValueError(f"{backend.model_type} threshold for {column!r} must be finite")


def _required_int(data: Mapping[str, Any], key: str, *, context: str) -> int:
    try:
        return int(data[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{context} missing integer {key!r}") from exc


def _validate_training_config(
    backend: SequenceTrainingBackend,
    config: SequenceAutoencoderTrainingConfigLike,
) -> None:
    if not config.enabled:
        raise ValueError(f"{backend.model_type} training requested but config is disabled")
    if config.batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if config.learning_rate <= 0:
        raise ValueError("learning_rate must be positive")
    if config.max_epochs <= 0:
        raise ValueError("max_epochs must be positive")
    if not 0.0 <= config.validation_fraction < 1.0:
        raise ValueError("validation_fraction must be >= 0 and < 1")
    if config.early_stopping_patience <= 0:
        raise ValueError("early_stopping_patience must be positive")
    if config.min_delta < 0:
        raise ValueError("min_delta must be non-negative")
    if config.threshold_k < 0:
        raise ValueError("threshold_k must be non-negative")


def _torch_training_modules(backend: SequenceTrainingBackend) -> tuple[Any, Any, Any, Any]:
    try:
        import torch
        from torch import nn, optim
        from torch.utils import data
    except ImportError as exc:
        raise backend.missing_dependency_error_cls()(
            "torch is required to train sequence detectors; run scripts/install_deps.py "
            "or install the 'ml' optional dependency"
        ) from exc
    return torch, nn, optim, data


def _torch_module(backend: SequenceTrainingBackend) -> Any:
    try:
        import torch
    except ImportError as exc:
        raise backend.missing_dependency_error_cls()(
            f"torch is required for {backend.model_type} checkpoints"
        ) from exc
    return torch


__all__ = [
    "ARTIFACT_FORMAT_VERSION",
    "CalibrationCoverageError",
    "SequenceAutoencoderTrainingConfigLike",
    "TrainedSequenceAutoencoder",
    "calibrate_sequence_autoencoder_thresholds",
    "calibration_coverage_violations",
    "default_sequence_manifest_path",
    "load_sequence_autoencoder_model",
    "save_sequence_autoencoder_model",
    "train_sequence_autoencoder",
]
