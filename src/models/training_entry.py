"""The training entries every detector retrain goes through (R06.4).

``train_sequence_detector`` and ``train_isolation_forest`` hold the training
contract: the sequence path drops parked rows, chooses the validation drives
before the scaler is fit, drops unobserved and suppressed columns and resolves
the feature set; the IF path selects its columns, withholds demoted behavior
deviations and fits per mode. They used to be the ``train`` functions of
``scripts/train_sequence.py`` and ``scripts/train_anomaly_model.py``, which
``scripts/retrain.py`` and ``scripts/sequence_bakeoff.py`` imported as
libraries through a sibling import between scripts. The scripts are now thin
CLIs over this module.

``benchmark_held_out`` is the one way a CLI runs the false-positive benchmark
on a trained sequence model: it scores with the serving config's
``suppress_channels`` for that backbone (R04.1), so no caller can benchmark a
detector the daemon does not serve.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.behavior.behavior_decoder import withheld_learned_feature_columns
from src.behavior.behavior_models import load_behavior_models
from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN, is_flag_column
from src.config import CanaryConfig
from src.features.feature_build import build_feature_frame
from src.features.feature_paths import DEFAULT_BINDINGS_PATH
from src.features.feature_policy import FeaturePolicy, resolve_feature_policy
from src.features.feature_selection import (
    COVERAGE_FLOOR_SCHEMA_KEY,
    expand_suppress_channels,
    isolation_forest_feature_selection,
    sequence_feature_columns,
    sequence_feature_selection,
)
from src.features.feature_spec import align_features_to_schema, build_feature_spec
from src.models.evaluation import refuse_non_default_profile_sessions
from src.models.false_positive_benchmark import (
    FalsePositiveBenchmarkReport,
    benchmark_false_positives,
)
from src.models.inference import TRAINING_SUPPRESS_SCHEMA_KEY, training_suppress_record
from src.models.isolation_forest import (
    TrainedIsolationForest,
    default_manifest_path,
    fit_isolation_forest,
)
from src.models.sequence_autoencoder_training import (
    TrainedSequenceAutoencoder,
    default_sequence_manifest_path,
    train_sequence_autoencoder,
)
from src.models.sequence_dataset import (
    DEFAULT_SEQUENCE_STRIDE,
    DEFAULT_TRAINING_MASK_RULE,
    SequenceWindowDataset,
    observation_mask_from_schema,
)
from src.models.sequence_feature_set import (
    ALL_CHANNELS,
    FEATURE_SET_SCHEMA_KEY,
    load_vehicle_sequence_roles,
    resolve_feature_set,
    sequence_roles_path,
)
from src.models.sequence_protocol import TrainedSequenceModel
from src.models.sequence_registry import (
    LSTM_TRAINING_BACKEND,
    TCN_TRAINING_BACKEND,
    SequenceTrainingBackend,
)
from src.models.sequence_training import (
    ValidationChoice,
    choose_validation_sessions,
    session_envelopes,
    tail_block_validation_start,
)
from src.preprocessing.drives import DRIVE_COLUMN, drop_rows_without_drive
from src.preprocessing.normalization import ZScoreScaler

log = logging.getLogger(__name__)

#: Manifest ``schema`` key naming the columns dropped because they were
#: observed only in the validation sessions (no training statistics).
UNOBSERVED_IN_TRAINING_SCHEMA_KEY = "unobserved_in_training"

#: Manifest ``schema`` key: training-frame rows dropped because no vehicle
#: activity lay near them (parked time, ``src.preprocessing.drives``).
ROWS_WITHOUT_DRIVE_SCHEMA_KEY = "rows_without_physical_drive"

#: Selectable sequence backbones by CLI/config selector.
SEQUENCE_BACKBONES: dict[str, SequenceTrainingBackend] = {
    "lstm": LSTM_TRAINING_BACKEND,
    "tcn": TCN_TRAINING_BACKEND,
}


def _config_block(config: CanaryConfig, backend: SequenceTrainingBackend) -> Any:
    return getattr(config.models, backend.config_attr)


def build_sequence_dataset(
    backend: SequenceTrainingBackend,
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    feature_frame: pd.DataFrame | None = None,
    feature_cache_dir: Path | None = None,
    rebuild_feature_cache: bool = False,
    sequence_length: int | None = None,
    feature_columns: Sequence[str] | None = None,
    stride: int = DEFAULT_SEQUENCE_STRIDE,
    scaler: ZScoreScaler | None = None,
    observation_mask: str | None = None,
) -> SequenceWindowDataset:
    """Build a sliding-window dataset from recorded sessions via the shared pipeline.

    ``observation_mask`` (A03.1) is what the per-cell mask means. Building for
    a trained model's schema (``feature_columns`` given) requires the model's
    own rule (``observation_mask_from_schema``), so an artifact is never
    scored with a mask it was not trained on; a fresh training build defaults
    to :data:`DEFAULT_TRAINING_MASK_RULE`.

    When ``feature_columns`` is given (e.g. a trained model's schema), they must
    all be present and numeric; otherwise the trainable numeric columns are
    discovered automatically.

    When ``scaler`` is given, the feature columns are z-scored before window
    construction so the model trains on a sane numeric scale (raw OBD values
    range from coolant ~30 to RPM ~5000, which makes MAE/MSE loss meaningless
    without normalization).
    """
    features = (
        feature_frame
        if feature_frame is not None
        else build_feature_frame(
            session_dirs,
            config,
            bindings_path=bindings_path,
            cache_dir=feature_cache_dir,
            force_rebuild=rebuild_feature_cache,
        )
    )
    columns = (
        list(feature_columns) if feature_columns is not None else sequence_feature_columns(features)
    )
    if not columns:
        raise ValueError(
            "no trainable feature columns — the sessions produced no numeric "
            "signal columns. Did you convert them to Parquet, and is "
            f"{bindings_path} present so CAN payloads get decoded?"
        )
    length = (
        _config_block(config, backend).sequence_length
        if sequence_length is None
        else sequence_length
    )
    # A held-out drive may lack a whole sensor family the model trained with
    # (e.g. no GPS fix). Add those columns as all-NaN so they're scored as
    # masked-missing rather than crashing the scaler / window slice.
    if feature_columns is not None:
        if observation_mask is None:
            raise ValueError(
                "build_dataset for an existing schema needs that model's observation_mask "
                "(sequence_dataset.observation_mask_from_schema(manifest.schema)[0])"
            )
        features = align_features_to_schema(features, columns)
    rule = DEFAULT_TRAINING_MASK_RULE if observation_mask is None else observation_mask
    # A03.8 — the envelope is measured on raw values, before any scaling.
    envelopes = session_envelopes(
        features, interval_seconds=1.0 / float(config.preprocessing.resample_hz)
    )
    if scaler is not None:
        # Keep only columns sequence construction/metadata needs and scale the
        # numeric matrix as float32. ZScoreScaler.transform copies the entire
        # wide feature frame and commonly doubles multi-GB corpus RSS. The
        # scaling itself is serve's (R27.5: ``apply_block``, in place and in
        # bounded float64 chunks), so a training window and the window serve
        # scores from the same rows are the same float32 values.
        structural = [
            column
            # DRIVE_COLUMN: the calibration drive floor counts physical drives.
            for column in (SESSION_COLUMN, TIMESTAMP_COLUMN, MODE_COLUMN, DRIVE_COLUMN)
            if column in features.columns
        ]
        # Both flag families: ``__gap`` and (A03.1) ``__arrived`` drive the mask.
        flags = [column for column in features.columns if is_flag_column(column)]
        keep = list(dict.fromkeys([*structural, *columns, *flags]))
        compact = features.loc[:, keep].copy()
        values = compact.loc[:, columns].to_numpy(dtype="float32", copy=True)
        compact.loc[:, columns] = scaler.apply_block(values, columns, out=values)
        features = compact
    return SequenceWindowDataset(
        features,
        sequence_length=length,
        stride=stride,
        feature_columns=columns,
        observation_mask=rule,
        session_envelopes=envelopes,
    )


def train_sequence_detector(
    backend: SequenceTrainingBackend,
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    out_dir: Path,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    feature_frame: pd.DataFrame | None = None,
    feature_cache_dir: Path | None = None,
    rebuild_feature_cache: bool = False,
    sequence_length: int | None = None,
    stride: int | None = None,
    device: str = "cpu",
    model_version: str | None = None,
    calibration_sessions: Sequence[Path] | None = None,
    calibration_frame: pd.DataFrame | None = None,
    feature_policy: FeaturePolicy | None = None,
    calibration_override: str | None = None,
    evaluation_sessions: Sequence[Path] | None = None,
    feature_set: str = ALL_CHANNELS,
) -> TrainedSequenceAutoencoder:
    """Build windows, train + calibrate ``backend``'s autoencoder, write artifact + manifest.

    When ``calibration_sessions`` is given (held out from ``session_dirs``), the
    reconstruction-error thresholds are calibrated on those *unseen* clean
    drives — out-of-sample. In-sample calibration on the training windows
    underestimates real cross-session error and floods alerts on unseen drives;
    a held-out calibration set produces thresholds that actually reflect what
    fresh drives look like. Falls back to in-sample when omitted.

    ``calibration_frame`` is the already-built feature frame of those held-out
    sessions: ``scripts/retrain.py`` builds it once and hands the *same* frame
    to the IF and to this trainer, so both detectors calibrate on one pooled
    multi-session holdout. When omitted it is built here.

    A calibration that does not cover what the model will score (fewer than
    three physical drives, a scored mode without its own band, a channel never
    observed) is refused before training (A02.2); ``calibration_override`` is
    the operator's stated reason to publish anyway, stamped into the manifest.
    ``evaluation_sessions`` (A02.1) — the held-out sessions the retrain gate
    scores, disjoint from calibration — are recorded in the manifest too.

    ``feature_set`` (A03.2/A03.3) picks what the model conditions on and what
    it scores; the default :data:`ALL_CHANNELS` is the deployed shape. The
    resolution is recorded in the feature spec.

    The validation drives are chosen by the stated rule before the scaler is
    fit (A03.5), and the scaler is fit on the remaining training rows only, so
    validation statistics never leak into the scaling (A03.4).

    Non-default-profile recordings are refused, for training and calibration
    alike (R03.12; :func:`~src.models.evaluation.refuse_non_default_profile_sessions`).
    """
    refuse_non_default_profile_sessions(session_dirs, purpose="train the sequence detector")
    refuse_non_default_profile_sessions(
        calibration_sessions or (), purpose="calibrate the sequence detector"
    )
    policy = (
        feature_policy
        if feature_policy is not None
        else resolve_feature_policy(config, bindings_path=bindings_path)
    )
    # Build the raw feature frame once so the scaler and the dataset see
    # exactly the same source rows. The scaler is fit on the trainable
    # numeric columns; the dataset then z-scores via that scaler before
    # windowing. Inference re-applies the same scaler (loaded from the
    # manifest) before calling SequenceInferenceEngine.score_window.
    features = (
        feature_frame
        if feature_frame is not None
        else build_feature_frame(
            session_dirs,
            config,
            bindings_path=bindings_path,
            cache_dir=feature_cache_dir,
            force_rebuild=rebuild_feature_cache,
        )
    )
    # Parked rows (no vehicle activity near them) neither train nor validate:
    # a sequence model of normal driving learns nothing from a key-off truck,
    # and early stopping judged on one measures nothing. Applied here, the one
    # entry every sequence retrain goes through, so no caller can forget it.
    features, rows_without_drive = drop_rows_without_drive(features)
    if rows_without_drive:
        log.info(
            "dropped %d row(s) with no physical drive (parked) from sequence training",
            rows_without_drive,
        )
    if features.empty:
        raise ValueError("every training row is parked: no row lies in a physical drive")
    # D03.34 — the detector coverage floor (shared with the IF): a channel
    # too sparse for the 1 Hz grid is not an input; the exclusions are
    # recorded from this same selection.
    selection = sequence_feature_selection(features, policy=policy)
    columns = list(selection.columns)
    if not columns:
        raise ValueError(
            "no trainable feature columns — the sessions produced no numeric "
            "signal columns. Did you convert them to Parquet, and is "
            f"{bindings_path} present so CAN payloads get decoded?"
        )
    config_block = _config_block(config, backend)
    # A03.4/A03.5: fix the validation drives first; the scaler is fit on the
    # rows that train, and validation rows never move it.
    length = config_block.sequence_length if sequence_length is None else int(sequence_length)
    training_stride = (
        int(getattr(config_block, "training_stride", DEFAULT_SEQUENCE_STRIDE))
        if stride is None
        else stride
    )
    validation_choice = choose_validation_sessions(
        features,
        sequence_length=length,
        validation_fraction=float(config_block.validation_fraction),
        random_state=int(config_block.random_state),
        stride=training_stride,
    )
    scaler_rows = _rows_outside(features, validation_choice)
    if not validation_choice.sessions:
        # No eligible drive: validation is the tail block of the windows
        # (A03.5's stated fallback). Fit the scaler on the rows before it.
        tail = tail_block_validation_start(
            features,
            sequence_length=length,
            stride=training_stride,
            validation_fraction=float(config_block.validation_fraction),
        )
        if tail is not None:
            tail_session, tail_start = tail
            names = scaler_rows[SESSION_COLUMN].astype(str)
            stamps = pd.to_datetime(scaler_rows[TIMESTAMP_COLUMN], utc=True)
            before = (names < tail_session) | ((names == tail_session) & (stamps < tail_start))
            scaler_rows = scaler_rows.loc[before.to_numpy()]
    # A channel observed only in the validation drives has no training
    # statistics: the scaler cannot centre it and the encoder never learns it.
    # It is not a feature of this model, and the schema says so. (2026-09-23:
    # `mem:ECM_ENGINE_SPEED_RAM` appeared only in the validation drive of the
    # post-flash corpus and the scaler fit raised on it.)
    unobserved_in_training = [c for c in columns if not scaler_rows[c].notna().any()]
    if unobserved_in_training:
        log.warning(
            "dropping %d column(s) observed only in the validation sessions",
            len(unobserved_in_training),
            extra={
                "columns": unobserved_in_training,
                "validation_sessions": list(validation_choice.sessions),
            },
        )
        columns = [c for c in columns if c not in set(unobserved_in_training)]
        if not columns:
            raise ValueError("every trainable column was observed only in the validation sessions")
    # Exclude config-suppressed channels (and their rolling-derivative siblings)
    # at train time so the manifest's ``feature_columns`` reflects what the
    # model actually scores on. The serve path also masks these at runtime —
    # belt + suspenders, and lets the operator add a noise channel mid-cycle
    # without an immediate retrain.
    suppressed = expand_suppress_channels(tuple(config_block.suppress_channels), columns)
    if suppressed:
        log.info(
            "excluding suppressed channels from %s training schema",
            backend.model_type,
            extra={"suppressed": list(suppressed)},
        )
        columns = [c for c in columns if c not in set(suppressed)]
        if not columns:
            raise ValueError("suppress_channels excludes every trainable column")
    resolution = resolve_feature_set(
        feature_set,
        columns,
        vehicle_roles=(
            load_vehicle_sequence_roles(sequence_roles_path(config))
            if feature_set != ALL_CHANNELS
            else None
        ),
    )
    columns = list(resolution.feature_columns)
    # GRU-D (and the TCN's delta-decay channels) handle missing values via the
    # observed-mask, so keep EVERY row and let masking ignore the gaps.
    # Dropping rows with any NaN here (as the IF path does — the IF can't
    # mask) would discard every session missing a channel — e.g. all the
    # no-GPS historical idle sessions, whose gps:speed is NaN throughout —
    # collapsing the corpus from ~11k windows to just the few
    # fully-instrumented drives (~3.5k) and starving the autoencoder. Fully
    # blank rows are already removed upstream (``_drop_fully_stale_rows``). The
    # scaler is fit skipna, so per-column means/stds use each channel's real
    # observations regardless of other channels' gaps.
    #
    scaler = ZScoreScaler.fit(scaler_rows, columns=columns)
    scaler_fit_sessions = sorted(scaler_rows[SESSION_COLUMN].astype(str).unique())
    scaler_fit_until = {
        str(name): pd.Timestamp(stamp).isoformat()
        for name, stamp in pd.to_datetime(scaler_rows[TIMESTAMP_COLUMN], utc=True)
        .groupby(scaler_rows[SESSION_COLUMN].astype(str))
        .max()
        .items()
    }

    dataset = build_sequence_dataset(
        backend,
        session_dirs,
        config,
        bindings_path=bindings_path,
        feature_frame=features,
        feature_cache_dir=feature_cache_dir,
        rebuild_feature_cache=rebuild_feature_cache,
        sequence_length=sequence_length,
        stride=training_stride,
        feature_columns=columns,
        scaler=scaler,
        observation_mask=DEFAULT_TRAINING_MASK_RULE,
    )
    if len(dataset) == 0:
        raise ValueError(
            "no sequence windows available — sessions are shorter than "
            f"sequence_length, or every window contained a gap/missing feature "
            f"({len(dataset.feature_columns)} feature columns considered)"
        )
    calibration_ds: SequenceWindowDataset | None = None
    if calibration_sessions or calibration_frame is not None:
        calibration_ds = build_sequence_dataset(
            backend,
            calibration_sessions or (),
            config,
            bindings_path=bindings_path,
            feature_frame=calibration_frame,
            feature_cache_dir=feature_cache_dir,
            rebuild_feature_cache=rebuild_feature_cache,
            sequence_length=sequence_length,
            stride=int(getattr(config_block, "calibration_stride", DEFAULT_SEQUENCE_STRIDE)),
            feature_columns=columns,
            scaler=scaler,
            observation_mask=dataset.observation_mask,
        )
        if len(calibration_ds) == 0:
            raise ValueError(
                "calibration sessions produced no windows — they're shorter "
                "than sequence_length, or had no usable feature data"
            )
    else:
        log.warning(
            "no --calibration-sessions given; falling back to in-sample "
            "threshold calibration (cross-session FPR likely inflated)"
        )

    feature_spec = build_feature_spec(
        dataset.frame,
        dataset.feature_columns,
        config=config,
        sequence_length=dataset.sequence_length,
        sequence_stride=dataset.stride,
        policy=policy,
        # B03.4 — record the behavior artifact set the frame was built
        # against (the sequence trainer consumes no deviation column, so
        # this pins "none", not "unknown").
        behavior_models=(
            load_behavior_models(config.daemon.behavior_models)
            if config.daemon.behavior_models.exists()
            else None
        ),
    )
    # A01.5 — the suppress list this schema was cut with, so the daemon can
    # name a serve-time list that differs (its thresholds would then count
    # channels serving leaves out, or the other way round).
    feature_spec[TRAINING_SUPPRESS_SCHEMA_KEY] = training_suppress_record(
        tuple(config_block.suppress_channels), suppressed
    )
    # A03.2/A03.3 — what this model conditions on and what it scores.
    feature_spec[FEATURE_SET_SCHEMA_KEY] = dict(resolution.record)
    feature_spec[UNOBSERVED_IN_TRAINING_SCHEMA_KEY] = sorted(unobserved_in_training)
    feature_spec[COVERAGE_FLOOR_SCHEMA_KEY] = selection.coverage_record()
    feature_spec[ROWS_WITHOUT_DRIVE_SCHEMA_KEY] = rows_without_drive
    trained = train_sequence_autoencoder(
        backend,
        dataset,
        config_block,
        device=device,
        model_version=model_version,
        trained_at=datetime.now(UTC),
        calibration_dataset=calibration_ds,
        feature_spec=feature_spec,
        scaler=scaler,
        calibration_sessions_requested=(
            [Path(session).name for session in calibration_sessions]
            if calibration_sessions
            else None
        ),
        # The documented fallback of ``train_sequence.py`` (see its
        # --calibration-sessions help): passing no calibration sessions is a
        # deliberate dev/manual-run path, not the accidental bypass 3.1
        # targets in ``train_sequence_autoencoder``. Unchanged by the move out
        # of the script (R06.4).
        allow_in_sample_calibration=calibration_ds is None,
        calibration_override=calibration_override,
        validation_choice=validation_choice,
        scaler_fit_sessions=scaler_fit_sessions,
        scaler_fit_until=scaler_fit_until,
    )
    if evaluation_sessions:
        trained = replace(
            trained,
            manifest=replace(
                trained.manifest,
                evaluation_sessions=tuple(Path(s).name for s in evaluation_sessions),
            ),
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / "model.pt"
    manifest_path = default_sequence_manifest_path(artifact_path)
    trained.save(artifact_path, manifest_path)
    log.info(
        "%s trained and saved",
        backend.model_type,
        extra={
            "artifact": str(artifact_path),
            "manifest": str(manifest_path),
            "feature_columns": len(dataset.feature_columns),
            "windows": len(dataset),
            "sessions": [str(s) for s in session_dirs],
        },
    )
    return trained


def _rows_outside(features: pd.DataFrame, choice: ValidationChoice) -> pd.DataFrame:
    """The feature rows of sessions outside the validation choice (A03.4)."""
    if not choice.sessions:
        return features
    held = set(choice.sessions)
    return features.loc[~features[SESSION_COLUMN].astype(str).isin(held)]


def served_suppress_channels(config: CanaryConfig, model_type: str) -> tuple[str, ...]:
    """``models.<model_type>.suppress_channels``: what the daemon keeps out of the score."""
    block = getattr(config.models, model_type, None)
    return tuple(getattr(block, "suppress_channels", ()) or ())


def benchmark_held_out(
    backend: SequenceTrainingBackend,
    trained: TrainedSequenceModel,
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    stride: int = DEFAULT_SEQUENCE_STRIDE,
    batch_size: int = 64,
    device: str = "cpu",
    k_grid: Sequence[float] | None = None,
) -> FalsePositiveBenchmarkReport | None:
    """False-positive benchmark of ``trained`` on held-out clean sessions, as served.

    The held-out frame is z-scored with the model's own scaler, so
    reconstruction error lands in the calibrated-threshold domain (the serve
    path scales every window the same way), and windows are built with the
    model's own observation mask. The score leaves out the serving config's
    ``suppress_channels`` for this backbone (R04.1): ``train_sequence.py
    --evaluate`` and ``sequence_bakeoff.py`` used to benchmark without it, so
    their FPR counted channels the daemon never scores. ``None`` when the
    sessions produce no windows.
    """
    scaler = (
        ZScoreScaler.from_dict(dict(trained.manifest.scaler))
        if trained.manifest.scaler is not None
        else None
    )
    dataset = build_sequence_dataset(
        backend,
        session_dirs,
        config,
        bindings_path=bindings_path,
        sequence_length=int(trained.manifest.schema["sequence_length"]),
        feature_columns=trained.feature_columns,
        stride=stride,
        scaler=scaler,
        observation_mask=observation_mask_from_schema(trained.manifest.schema)[0],
    )
    if len(dataset) == 0:
        return None
    return benchmark_false_positives(
        trained,
        dataset,
        k_grid=k_grid,
        batch_size=batch_size,
        device=device,
        suppress_channels=served_suppress_channels(config, backend.model_type),
    )


def train_isolation_forest(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    out_dir: Path,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    feature_frame: pd.DataFrame | None = None,
    feature_cache_dir: Path | None = None,
    rebuild_feature_cache: bool = False,
    calibration_sessions: Sequence[Path] | None = None,
    calibration_frame: pd.DataFrame | None = None,
    feature_policy: FeaturePolicy | None = None,
    evaluation_sessions: Sequence[Path] | None = None,
    quarantined_cells: Mapping[str, pd.Index] | None = None,
    calibration_policy: Mapping[str, Any] | None = None,
    calibration_override: str | None = None,
) -> TrainedIsolationForest:
    """Build features, fit the per-mode IF + scaler, write artifact + manifest.

    ``calibration_frame`` is the already-built feature frame of the held-out
    sessions (``scripts/retrain.py`` builds it once for every detector); when
    omitted it is built here from ``calibration_sessions``.
    ``evaluation_sessions`` (A02.1) are the held-out sessions the retrain gate
    scores — disjoint from calibration — recorded in the manifest.
    ``quarantined_cells`` / ``calibration_policy`` / ``calibration_override``
    pass through to :func:`fit_isolation_forest` (A04.3, A04.4).

    Non-default-profile recordings are refused, for training and calibration
    alike (R03.12; :func:`~src.models.evaluation.refuse_non_default_profile_sessions`).
    """
    refuse_non_default_profile_sessions(session_dirs, purpose="train the Isolation Forest")
    refuse_non_default_profile_sessions(
        calibration_sessions or (), purpose="calibrate the Isolation Forest"
    )
    policy = (
        feature_policy
        if feature_policy is not None
        else resolve_feature_policy(config, bindings_path=bindings_path)
    )
    features = (
        feature_frame
        if feature_frame is not None
        else build_feature_frame(
            session_dirs,
            config,
            bindings_path=bindings_path,
            cache_dir=feature_cache_dir,
            force_rebuild=rebuild_feature_cache,
        )
    )
    # The detector coverage floor (MIN_DETECTOR_COVERAGE, shared with the
    # sequence trainer since D03.34) plus the IF's own row-coverage requirement: a
    # per-row detector cannot score a row with any trained column missing.
    selection = isolation_forest_feature_selection(features, policy=policy)
    feature_columns = list(selection.columns)
    # B03.2 — a deviation column whose behavior model withholds
    # ``learned_feature`` (demoted/quarantined/exploratory in the profile's
    # behavior_policy.yaml) is not selected for a *new* model. The column is
    # still produced for existing manifests; the refusal is recorded on the
    # spec so the artifact answers why it is absent.
    behavior_models_path = config.daemon.behavior_models
    behavior_models = (
        load_behavior_models(behavior_models_path) if behavior_models_path.exists() else None
    )
    withheld = withheld_learned_feature_columns(feature_columns, behavior_models)
    if withheld:
        log.info(
            "behavior deviation columns withheld from IF training",
            extra={"columns": sorted(withheld)},
        )
        feature_columns = [column for column in feature_columns if column not in withheld]
    if not feature_columns:
        raise ValueError(
            "no trainable feature columns — the sessions produced no numeric "
            "signal columns. Did you convert them to Parquet, and is "
            "data/bindings.yaml present so CAN payloads get decoded?"
        )

    trained = fit_isolation_forest(
        features,
        config.models.isolation_forest,
        feature_columns=feature_columns,
        feature_spec={
            **build_feature_spec(
                features,
                feature_columns,
                config=config,
                policy=policy,
                # B03.4 — pin which behavior artifact produced each consumed
                # deviation column, so a later refit is caught by the
                # producibility check instead of fed to this model.
                behavior_models=behavior_models,
            ),
            "behavior_learned_feature_withheld": dict(sorted(withheld.items())),
            COVERAGE_FLOOR_SCHEMA_KEY: selection.coverage_record(),
        },
        fitted_at=datetime.now(UTC),
        calibration_frame=(
            calibration_frame
            if calibration_frame is not None or not calibration_sessions
            else build_feature_frame(
                calibration_sessions,
                config,
                bindings_path=bindings_path,
                cache_dir=feature_cache_dir,
                force_rebuild=rebuild_feature_cache,
            )
        ),
        quarantined_cells=quarantined_cells,
        calibration_policy=calibration_policy,
        calibration_override=calibration_override,
    )

    if evaluation_sessions:
        trained = replace(
            trained,
            manifest=replace(
                trained.manifest,
                evaluation_sessions=tuple(Path(s).name for s in evaluation_sessions),
            ),
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / "model.joblib"
    manifest_path = default_manifest_path(artifact_path)
    trained.save(artifact_path, manifest_path)
    log.info(
        "model trained and saved",
        extra={
            "artifact": str(artifact_path),
            "manifest": str(manifest_path),
            "feature_columns": len(feature_columns),
            "sessions": [str(s) for s in session_dirs],
        },
    )
    return trained


__all__ = [
    "ROWS_WITHOUT_DRIVE_SCHEMA_KEY",
    "SEQUENCE_BACKBONES",
    "UNOBSERVED_IN_TRAINING_SCHEMA_KEY",
    "benchmark_held_out",
    "build_sequence_dataset",
    "served_suppress_channels",
    "train_isolation_forest",
    "train_sequence_detector",
]
