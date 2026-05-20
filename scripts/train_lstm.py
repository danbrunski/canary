#!/usr/bin/env python3
"""Train the LSTM autoencoder anomaly detector from recorded sessions.

The Phase 3 companion to ``scripts/train_anomaly_model.py``: runs the shared
preprocessing pipeline (read sessions → decode CAN via bindings.yaml → wide →
resample → operating-mode segment → rolling features), builds fixed-length
sliding windows from the clean-baseline sessions, trains the LSTM autoencoder
(MAE loss, early stopping, then calibrates reconstruction-error thresholds),
and writes a Torch checkpoint + JSON manifest.

Without ``--evaluate`` it trains and saves. With ``--evaluate <session>...`` it
runs the false-positive benchmark (see ``scripts/fp_benchmark.py``) on held-out
clean sessions and prints the FPR, implied alerts-per-hour vs. the v1 budget
(≤ 1 alert / hour), and a sweep of ``k`` threshold multipliers.

Usage:
  scripts/train_lstm.py --sessions-root data/sessions --out models/lstm
  scripts/train_lstm.py data/sessions/2026-05-11T222447 --out models/lstm
  scripts/train_lstm.py data/sessions/train_* --out models/lstm \
      --evaluate data/sessions/holdout_drive

Sessions must contain Parquet chunks (telemetry-*.parquet) — convert candump
captures first with ``tools/log_to_parquet.py``. Requires the ``ml`` extra
(torch); run ``scripts/install_deps.py``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from src.config import CanaryConfig
from src.log import configure_logging
from src.models.false_positive_benchmark import (
    FalsePositiveBenchmarkReport,
    benchmark_false_positives,
)
from src.models.lstm_autoencoder import MissingLSTMAutoencoderDependencyError
from src.models.lstm_training import (
    TrainedLSTMAutoencoder,
    default_lstm_manifest_path,
    train_lstm_autoencoder,
)
from src.models.sequence_dataset import DEFAULT_SEQUENCE_STRIDE, SequenceWindowDataset
from src.preprocessing import discover_sessions
from src.preprocessing.feature_pipeline import (
    DEFAULT_BINDINGS_PATH,
    build_feature_frame,
    build_feature_spec,
    sequence_feature_columns,
)
from src.preprocessing.normalization import ZScoreScaler

log = logging.getLogger(__name__)


def build_dataset(
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
    construction so the LSTM trains on a sane numeric scale (raw OBD values
    range from coolant ~30 to RPM ~5000, which makes MAE loss meaningless
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
        config.models.lstm_autoencoder.sequence_length
        if sequence_length is None
        else sequence_length
    )
    if scaler is not None:
        features = scaler.transform(features)
    return SequenceWindowDataset(
        features,
        sequence_length=length,
        stride=stride,
        feature_columns=columns,
    )


def train(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    out_dir: Path,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    feature_frame: pd.DataFrame | None = None,
    feature_cache_dir: Path | None = None,
    rebuild_feature_cache: bool = False,
    sequence_length: int | None = None,
    stride: int = DEFAULT_SEQUENCE_STRIDE,
    device: str = "cpu",
    model_version: str | None = None,
) -> TrainedLSTMAutoencoder:
    """Build windows, train + calibrate the LSTM autoencoder, write artifact + manifest."""
    # Build the raw feature frame once so the scaler and the dataset see
    # exactly the same source rows. The scaler is fit on the trainable
    # numeric columns; the dataset then z-scores via that scaler before
    # windowing. Inference re-applies the same scaler (loaded from the
    # manifest) before calling LSTMInferenceEngine.score_window.
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
    columns = sequence_feature_columns(features)
    if not columns:
        raise ValueError(
            "no trainable feature columns — the sessions produced no numeric "
            "signal columns. Did you convert them to Parquet, and is "
            f"{bindings_path} present so CAN payloads get decoded?"
        )
    # Drop rows where any of the chosen feature columns is NaN before fitting
    # the scaler. Otherwise per-column means/stds reflect spurious
    # imputed-zero rows the model can't really learn from, and the
    # threshold ends up calibrated against a fractured error distribution.
    # Mirrors IsolationForest training's missing-feature row drop.
    clean_features = features.dropna(subset=columns)
    scaler = ZScoreScaler.fit(clean_features, columns=columns)

    dataset = build_dataset(
        session_dirs,
        config,
        bindings_path=bindings_path,
        feature_frame=clean_features,
        feature_cache_dir=feature_cache_dir,
        rebuild_feature_cache=rebuild_feature_cache,
        sequence_length=sequence_length,
        stride=stride,
        feature_columns=columns,
        scaler=scaler,
    )
    if len(dataset) == 0:
        raise ValueError(
            "no sequence windows available — sessions are shorter than "
            f"sequence_length, or every window contained a gap/missing feature "
            f"({len(dataset.feature_columns)} feature columns considered)"
        )
    trained = train_lstm_autoencoder(
        dataset,
        config.models.lstm_autoencoder,
        device=device,
        model_version=model_version,
        trained_at=datetime.now(UTC),
        feature_spec=build_feature_spec(
            dataset.frame,
            dataset.feature_columns,
            config=config,
            sequence_length=dataset.sequence_length,
            sequence_stride=dataset.stride,
        ),
        scaler=scaler,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / "model.pt"
    manifest_path = default_lstm_manifest_path(artifact_path)
    trained.save(artifact_path, manifest_path)
    log.info(
        "LSTM autoencoder trained and saved",
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
    trained: TrainedLSTMAutoencoder,
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    stride: int = DEFAULT_SEQUENCE_STRIDE,
    device: str = "cpu",
    k_grid: Sequence[float] | None = None,
) -> FalsePositiveBenchmarkReport | None:
    """Run the false-positive benchmark on held-out clean sessions and print a summary."""
    dataset = build_dataset(
        session_dirs,
        config,
        bindings_path=bindings_path,
        sequence_length=int(trained.manifest.schema["sequence_length"]),
        feature_columns=trained.feature_columns,
        stride=stride,
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="train_lstm",
        description=(
            "Train the LSTM autoencoder anomaly detector from recorded Parquet "
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
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("canary.yaml"),
        help="Path to canary.yaml.",
    )
    parser.add_argument(
        "--bindings",
        type=Path,
        default=DEFAULT_BINDINGS_PATH,
        help="Path to bindings.yaml for CAN-signal decode (default data/bindings.yaml).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("models/lstm_autoencoder"),
        help="Output directory for model.pt + model.manifest.json.",
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
        default=DEFAULT_SEQUENCE_STRIDE,
        help=f"Sliding-window stride (default: {DEFAULT_SEQUENCE_STRIDE}).",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="Torch device for training/inference (default: cpu).",
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
    return parser.parse_args(argv)


def _resolve_training_sessions(args: argparse.Namespace) -> list[Path] | None:
    """Pick the training sessions, excluding any --evaluate sessions when discovered."""
    if args.sessions:
        return list(args.sessions)
    discovered = discover_sessions(args.sessions_root)
    if not discovered:
        return None
    if args.evaluate:
        held_out = {Path(p).resolve() for p in args.evaluate}
        kept = [s for s in discovered if Path(s).resolve() not in held_out]
        if kept:
            return kept
    return discovered or None


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = CanaryConfig.from_yaml(args.config)

    sessions = _resolve_training_sessions(args)
    if not sessions:
        print(
            f"error: no sessions given and none found under {args.sessions_root}. "
            "Convert candump captures with tools/log_to_parquet.py first.",
            file=sys.stderr,
        )
        return 1

    try:
        trained = train(
            sessions,
            config,
            args.out,
            bindings_path=args.bindings,
            sequence_length=args.sequence_length,
            stride=args.stride,
            device=args.device,
            model_version=args.model_version,
        )
    except MissingLSTMAutoencoderDependencyError as e:
        print(f"error: {e}", file=sys.stderr)
        print("Install the ML extras: pip install -e '.[ml]'", file=sys.stderr)
        return 1
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    calibration = trained.manifest.thresholds or {}
    sequence_length = trained.manifest.schema["sequence_length"]
    print(f"Trained LSTM autoencoder saved to {args.out}/")
    print(f"  feature columns: {len(trained.feature_columns)}")
    print(f"  windows: {trained.manifest.input_windows}  sequence_length: {sequence_length}")
    print(
        f"  best epoch: {trained.manifest.best_epoch}  early stopped: "
        f"{trained.manifest.early_stopped}  threshold_k: {calibration.get('threshold_k')}"
    )

    if args.evaluate:
        try:
            evaluate(
                trained,
                args.evaluate,
                config,
                bindings_path=args.bindings,
                stride=args.stride,
                device=args.device,
                k_grid=args.evaluate_k,
            )
        except (FileNotFoundError, KeyError, ValueError) as e:
            print(f"error during evaluation: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
