#!/usr/bin/env python3
"""Train either sequence-autoencoder anomaly detector from recorded sessions.

Task 2.1: the unified replacement for the near-duplicate ``scripts/train_lstm.py``
and ``scripts/train_tcn.py`` — same shared preprocessing pipeline (read
sessions → decode CAN via bindings.yaml → wide → resample → operating-mode
segment → rolling features), sliding windows from the clean-baseline
sessions, train + calibrate thresholds, write a Torch checkpoint + JSON
manifest — with the backbone selected by ``--backbone {lstm,tcn}`` instead of
by which script you ran.

``scripts/train_lstm.py``/``scripts/train_tcn.py`` are now ~5-line shims over
this module with their backbone fixed — kept because ``scripts/retrain.py``
and ``src/control/jobs.py``'s job allow-list still expect those exact module
names/functions (``train``, ``build_dataset``, ``evaluate``, ``main``).

Without ``--evaluate`` it trains and saves. With ``--evaluate <session>...`` it
runs the false-positive benchmark (see ``scripts/fp_benchmark.py``) on held-out
clean sessions and prints the FPR, implied alerts-per-hour vs. the v1 budget
(≤ 1 alert / hour), and a sweep of ``k`` threshold multipliers.

Usage:
  scripts/train_sequence.py --backbone lstm --sessions-root data/sessions
  scripts/train_sequence.py --backbone tcn data/sessions/2026-05-11T222447 \
      --evaluate data/sessions/holdout_drive

``--out`` defaults to the profile-scoped ``models/<slug>/<model_type>`` from
``--config`` (override with ``--out``/``--profile``).

Sessions must contain Parquet chunks (telemetry-*.parquet) — convert candump
captures first with ``tools/log_to_parquet.py``. Requires the ``ml`` extra
(torch); run ``scripts/install_deps.py``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pandas as pd
from _cli import add_common_args, add_profile_arg, load_config, scoped_models_dir

from src.config import CanaryConfig
from src.decode.behavior_models import load_behavior_models
from src.log import configure_logging
from src.models.evaluation import DriveHoldout, holdout_by_drive
from src.models.false_positive_benchmark import (
    FalsePositiveBenchmarkReport,
    benchmark_false_positives,
)
from src.models.inference import TRAINING_SUPPRESS_SCHEMA_KEY, training_suppress_record
from src.models.sequence_autoencoder_training import (
    TrainedSequenceAutoencoder,
    default_sequence_manifest_path,
    train_sequence_autoencoder,
)
from src.models.sequence_dataset import DEFAULT_SEQUENCE_STRIDE, SequenceWindowDataset
from src.models.sequence_registry import (
    LSTM_TRAINING_BACKEND,
    TCN_TRAINING_BACKEND,
    SequenceTrainingBackend,
)
from src.models.sequence_training import configure_torch_threads, default_training_device
from src.preprocessing import discover_sessions
from src.preprocessing.feature_pipeline import (
    DEFAULT_BINDINGS_PATH,
    FeaturePolicy,
    align_features_to_schema,
    build_feature_frame,
    build_feature_spec,
    expand_suppress_channels,
    resolve_feature_policy,
    sequence_feature_columns,
)
from src.preprocessing.normalization import ZScoreScaler
from src.preprocessing.resampler import GAP_SUFFIX, SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN

log = logging.getLogger(__name__)

#: Selectable backbones for this CLI's ``--backbone`` flag / ``bind_backbone``.
BACKBONES: dict[str, SequenceTrainingBackend] = {
    "lstm": LSTM_TRAINING_BACKEND,
    "tcn": TCN_TRAINING_BACKEND,
}


def _config_block(config: CanaryConfig, backend: SequenceTrainingBackend) -> Any:
    return getattr(config.models, backend.config_attr)


def build_dataset(
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
) -> SequenceWindowDataset:
    """Build a sliding-window dataset from recorded sessions via the shared pipeline.

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
        features = align_features_to_schema(features, columns)
    if scaler is not None:
        # Keep only columns sequence construction/metadata needs and scale the
        # numeric matrix as float32. ZScoreScaler.transform copies the entire
        # wide feature frame and commonly doubles multi-GB corpus RSS.
        structural = [
            column
            for column in (SESSION_COLUMN, TIMESTAMP_COLUMN, MODE_COLUMN)
            if column in features.columns
        ]
        gaps = [column for column in features.columns if str(column).endswith(GAP_SUFFIX)]
        keep = list(dict.fromkeys([*structural, *columns, *gaps]))
        compact = features.loc[:, keep].copy()
        values = compact.loc[:, columns].to_numpy(dtype="float32", copy=True)
        for index, column in enumerate(columns):
            denom = scaler.stds[column]
            if denom < scaler.std_floor:
                denom = 1.0
            values[:, index] = (values[:, index] - scaler.means[column]) / denom
        compact.loc[:, columns] = values
        features = compact
    return SequenceWindowDataset(
        features,
        sequence_length=length,
        stride=stride,
        feature_columns=columns,
    )


def train(
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
) -> TrainedSequenceAutoencoder:
    """Build windows, train + calibrate ``backend``'s autoencoder, write artifact + manifest.

    When ``calibration_sessions`` is given (held out from ``session_dirs``), the
    reconstruction-error thresholds are calibrated on those *unseen* clean
    drives — out-of-sample. In-sample calibration on the training windows
    underestimates real cross-session error and floods alerts on unseen drives;
    a held-out calibration set produces thresholds that actually reflect what
    fresh drives look like. Falls back to in-sample when omitted.

    ``calibration_frame`` is the already-built feature frame of those held-out
    sessions — ``scripts/retrain.py`` builds it once and hands the *same* frame
    to the IF and to this trainer, so both detectors calibrate on one pooled
    multi-session holdout. When omitted it is built here.

    A calibration that does not cover what the model will score (fewer than
    three physical drives, a scored mode without its own band, a channel never
    observed) is refused before training (A02.2); ``calibration_override`` is
    the operator's stated reason to publish anyway, stamped into the manifest.
    ``evaluation_sessions`` (A02.1) — the held-out sessions the retrain gate
    scores, disjoint from calibration — are recorded in the manifest too.
    """
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
    columns = sequence_feature_columns(features, policy=policy)
    if not columns:
        raise ValueError(
            "no trainable feature columns — the sessions produced no numeric "
            "signal columns. Did you convert them to Parquet, and is "
            f"{bindings_path} present so CAN payloads get decoded?"
        )
    config_block = _config_block(config, backend)
    training_stride = (
        int(getattr(config_block, "training_stride", DEFAULT_SEQUENCE_STRIDE))
        if stride is None
        else stride
    )
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
    scaler = ZScoreScaler.fit(features, columns=columns)

    dataset = build_dataset(
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
    )
    if len(dataset) == 0:
        raise ValueError(
            "no sequence windows available — sessions are shorter than "
            f"sequence_length, or every window contained a gap/missing feature "
            f"({len(dataset.feature_columns)} feature columns considered)"
        )
    calibration_ds: SequenceWindowDataset | None = None
    if calibration_sessions or calibration_frame is not None:
        calibration_ds = build_dataset(
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
        # This CLI's documented fallback (see the --calibration-sessions
        # help text): omitting the flag is a deliberate dev/manual-run path,
        # not the accidental-bypass 3.1 targets at the library level.
        allow_in_sample_calibration=calibration_ds is None,
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


def evaluate(
    backend: SequenceTrainingBackend,
    trained: TrainedSequenceAutoencoder,
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    stride: int = DEFAULT_SEQUENCE_STRIDE,
    device: str = "cpu",
    k_grid: Sequence[float] | None = None,
) -> FalsePositiveBenchmarkReport | None:
    """Run the false-positive benchmark on held-out clean sessions and print a summary."""
    # Z-score the held-out frame with the model's own scaler so reconstruction
    # error lands in the same domain as the calibrated thresholds — the live
    # serve path scales every window the same way before scoring.
    scaler = (
        ZScoreScaler.from_dict(dict(trained.manifest.scaler))
        if trained.manifest.scaler is not None
        else None
    )
    dataset = build_dataset(
        backend,
        session_dirs,
        config,
        bindings_path=bindings_path,
        sequence_length=int(trained.manifest.schema["sequence_length"]),
        feature_columns=trained.feature_columns,
        stride=stride,
        scaler=scaler,
    )
    if len(dataset) == 0:
        log.warning("held-out sessions produced no sequence windows; skipping evaluation")
        return None
    report = benchmark_false_positives(trained, dataset, k_grid=k_grid, device=device)
    _print_evaluation(report, session_dirs)
    return report


def _print_evaluation(
    report: FalsePositiveBenchmarkReport,
    session_dirs: Sequence[Path],
) -> None:
    names = ", ".join(Path(p).name for p in session_dirs)
    print("=" * 78)
    print(f"FALSE-POSITIVE BENCHMARK  (held-out clean sessions: {names})")
    print("=" * 78)
    print(
        f"  model: {report.model_version}   windows: {report.window_count}   "
        f"sequence_length: {report.sequence_length}   feature_columns: "
        f"{report.feature_column_count}"
    )
    budget = "YES" if report.meets_v1_budget else "NO"
    print(
        f"  calibrated k={report.threshold_k:g}  →  FPR={report.false_positive_rate:.4f}  "
        f"({report.flagged_windows}/{report.window_count})   "
        f"alerts/hour={report.alerts_per_hour:.2f}   v1 budget (≤1/h): {budget}"
    )
    print(
        f"  global threshold={report.global_threshold:.6g}   global exceedances="
        f"{report.global_exceedances} (FPR={report.global_false_positive_rate:.4f})"
    )
    if report.channel_summaries:
        print("  top channels by false positives:")
        for summary in report.channel_summaries[:8]:
            print(
                f"    {summary.channel:<28} {summary.exceedances:>5}  "
                f"FPR={summary.false_positive_rate:.4f}"
            )
    print("  k sweep (threshold = mean + k·σ):")
    for point in report.k_sweep:
        flag = "YES" if point.meets_v1_budget else "NO"
        print(
            f"    k={point.k:>5g}  FPR={point.false_positive_rate:.4f}  "
            f"alerts/h={point.alerts_per_hour:.2f}  budget={flag}"
        )
    meeting = [point.k for point in report.k_sweep if point.meets_v1_budget]
    if meeting:
        print(
            f"  → recommend k ≈ {min(meeting):g} (smallest swept k meeting the "
            "≤1 alert/hour budget)"
        )
    else:
        print(
            "  → no swept k meets the ≤1 alert/hour budget; capture more clean "
            "baseline or widen --evaluate-k"
        )


def _build_arg_parser(backend: SequenceTrainingBackend, *, prog: str) -> argparse.ArgumentParser:
    label = backend.selector.upper()
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            f"Train the {label} autoencoder anomaly detector from recorded Parquet "
            "sessions. Runs the shared preprocessing pipeline, builds sliding "
            "windows, trains + calibrates thresholds, writes a Torch checkpoint "
            "+ JSON manifest. Optionally runs the false-positive benchmark on "
            "held-out clean sessions."
        ),
    )
    parser.add_argument(
        "sessions",
        nargs="*",
        type=Path,
        help="Training session directories. If none given, all sessions under "
        "--sessions-root (minus any --evaluate sessions).",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path("data/sessions"),
        help="Where to discover sessions when none are given explicitly.",
    )
    add_common_args(parser)
    add_profile_arg(parser)
    parser.add_argument(
        "--bindings",
        type=Path,
        default=DEFAULT_BINDINGS_PATH,
        help="Path to bindings.yaml for CAN-signal decode (default data/bindings.yaml).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory for model.pt + model.manifest.json. "
        f"Default: the (profile-scoped) <models_dir>/{backend.model_type} from --config.",
    )
    parser.add_argument(
        "--sequence-length",
        type=int,
        default=None,
        help="Override the sliding-window length from canary.yaml.",
    )
    parser.add_argument(
        "--stride",
        type=int,
        default=None,
        help="Override the configured training-window stride.",
    )
    parser.add_argument(
        "--device",
        default=default_training_device(),
        help="Torch device for training (default: cuda when available, else cpu).",
    )
    parser.add_argument(
        "--model-version",
        default=None,
        help="Explicit model version label (default: derived from the timestamp).",
    )
    parser.add_argument(
        "--evaluate",
        nargs="+",
        type=Path,
        default=None,
        metavar="SESSION",
        help="Held-out clean session(s) to run the false-positive benchmark on.",
    )
    parser.add_argument(
        "--calibration-sessions",
        nargs="+",
        type=Path,
        default=None,
        metavar="SESSION",
        help="Held-out clean session(s) for *out-of-sample* threshold calibration. "
        "These must be excluded from the training sessions. Without this flag "
        "thresholds are calibrated on the training windows themselves, which "
        "underestimates real cross-session error and inflates false positives.",
    )
    parser.add_argument(
        "--accept-thin-calibration",
        default=None,
        metavar="REASON",
        help="Publish even though the calibration sessions do not cover what the model "
        "will score (fewer than three drives, a scored mode without its own band, a "
        "channel never observed). Refused without this (A02.2); the reason and the "
        "violations are stamped into the manifest.",
    )
    parser.add_argument(
        "--evaluate-k",
        type=float,
        nargs="+",
        default=None,
        metavar="K",
        help="Candidate k multipliers for the benchmark sweep (default: 1 2 3 4 5 6 8 10).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def parse_args(
    backend: SequenceTrainingBackend, argv: Sequence[str] | None = None
) -> argparse.Namespace:
    parser = _build_arg_parser(backend, prog=f"train_{backend.selector}")
    return parser.parse_args(argv)


def _resolve_holdout(args: argparse.Namespace) -> DriveHoldout | None:
    """Training, calibration and evaluation sessions, split by physical drive.

    ``--calibration-sessions`` and ``--evaluate`` take the rest of their
    drives with them (``holdout_by_drive``), a drive on both sides is refused,
    and neither side trains. ``None`` when there are no sessions at all.
    """
    pool = list(args.sessions) if args.sessions else discover_sessions(args.sessions_root)
    if not pool:
        return None
    return holdout_by_drive(
        pool,
        calibration=list(args.calibration_sessions or ()),
        evaluation=list(args.evaluate or ()),
    )


def _resolve_training_sessions(args: argparse.Namespace) -> list[Path] | None:
    """Pick the training sessions, excluding the --evaluate and
    --calibration-sessions drives. ``None`` when nothing is left to train on —
    never every session, which would train on the held-out drives."""
    holdout = _resolve_holdout(args)
    if holdout is None:
        return None
    return list(holdout.training) or None


def _run(backend: SequenceTrainingBackend, args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    configure_torch_threads()
    config = load_config(args)
    args.out = args.out or scoped_models_dir(config, args.profile) / backend.model_type

    try:
        holdout = _resolve_holdout(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sessions = list(holdout.training) if holdout is not None else []
    if holdout is not None:
        args.calibration_sessions = list(holdout.calibration) or None
        args.evaluate = list(holdout.evaluation) or None
    if not sessions:
        print(
            f"error: no sessions given and none found under {args.sessions_root}. "
            "Convert candump captures with tools/log_to_parquet.py first.",
            file=sys.stderr,
        )
        return 1

    try:
        trained = train(
            backend,
            sessions,
            config,
            args.out,
            bindings_path=args.bindings,
            sequence_length=args.sequence_length,
            stride=args.stride,
            device=args.device,
            calibration_sessions=args.calibration_sessions,
            model_version=args.model_version,
            calibration_override=args.accept_thin_calibration,
        )
    except backend.missing_dependency_error_cls() as e:
        print(f"error: {e}", file=sys.stderr)
        print("Install the ML extras: pip install -e '.[ml]'", file=sys.stderr)
        return 1
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    calibration = trained.manifest.thresholds or {}
    sequence_length = trained.manifest.schema["sequence_length"]
    label = backend.selector.upper()
    print(f"Trained {label} autoencoder saved to {args.out}/")
    print(f"  feature columns: {len(trained.feature_columns)}")
    print(f"  windows: {trained.manifest.input_windows}  sequence_length: {sequence_length}")
    print(
        f"  best epoch: {trained.manifest.best_epoch}  early stopped: "
        f"{trained.manifest.early_stopped}  threshold_k: {calibration.get('threshold_k')}"
    )

    if args.evaluate:
        try:
            evaluate(
                backend,
                trained,
                args.evaluate,
                config,
                bindings_path=args.bindings,
                stride=(
                    args.stride
                    if args.stride is not None
                    else int(
                        getattr(
                            _config_block(config, backend),
                            "calibration_stride",
                            DEFAULT_SEQUENCE_STRIDE,
                        )
                    )
                ),
                device=args.device,
                k_grid=args.evaluate_k,
            )
        except (FileNotFoundError, KeyError, ValueError) as e:
            print(f"error during evaluation: {e}", file=sys.stderr)
            return 1
    return 0


def _split_backbone_argv(argv: Sequence[str] | None) -> tuple[SequenceTrainingBackend, list[str]]:
    """Pull ``--backbone`` out of argv, returning the backend + the rest."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--backbone", choices=sorted(BACKBONES), required=True)
    known, remaining = pre.parse_known_args(argv)
    return BACKBONES[known.backbone], remaining


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    backend, remaining = _split_backbone_argv(raw_argv)
    args = parse_args(backend, remaining)
    return _run(backend, args)


@dataclass(frozen=True)
class BoundTrainer:
    """The per-backbone callables ``scripts/train_lstm.py``/``train_tcn.py`` re-export."""

    build_dataset: Any
    train: Any
    evaluate: Any
    parse_args: Any
    main: Any


def bind_backbone(name: str) -> BoundTrainer:
    """Bind every backbone-parameterized function here to a fixed backbone.

    Used by the ``train_lstm.py``/``train_tcn.py`` shims so callers that
    import those modules directly (``scripts/retrain.py``,
    ``scripts/sequence_bakeoff.py``) see the exact same
    ``train``/``build_dataset``/``evaluate``/``main`` call signatures they did
    before Task 2.1 — just routed through this shared implementation.
    """
    backend = BACKBONES[name]
    return BoundTrainer(
        build_dataset=partial(build_dataset, backend),
        train=partial(train, backend),
        evaluate=partial(evaluate, backend),
        parse_args=partial(parse_args, backend),
        main=lambda argv=None: _run(backend, parse_args(backend, argv)),
    )


if __name__ == "__main__":
    sys.exit(main())
