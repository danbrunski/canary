#!/usr/bin/env python3
"""Benchmark the LSTM autoencoder's false-positive rate on held-out clean drives.

Task 3.6: feed recorded *clean* Parquet sessions (held out from training)
through the same preprocessing path the model was trained on, score every
sliding window, and report:

  * the false-positive rate at the calibrated threshold (FPR = flagged / total),
  * the implied alerts-per-hour vs. the v1 budget (<= 1 alert / hour),
  * a sweep of ``k`` multipliers so a tighter/looser threshold can be chosen
    without retraining, and
  * which channels and sessions trip most often.

No hardware needed. Pair with ``scripts/replay.py`` (preprocessing sanity) and
``scripts/train_anomaly_model.py`` (Isolation Forest training).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.train_lstm import build_dataset
from src.config import CanaryConfig
from src.models.false_positive_benchmark import (
    FalsePositiveBenchmarkReport,
    benchmark_false_positives,
)
from src.models.lstm_autoencoder import MissingLSTMAutoencoderDependencyError
from src.models.lstm_training import load_lstm_autoencoder_model
from src.models.sequence_dataset import observation_mask_from_schema
from src.preprocessing.normalization import ZScoreScaler


def run_false_positive_benchmark(
    model_path: Path,
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    k_grid: Sequence[float] | None = None,
    stride: int = 1,
    batch_size: int = 64,
    device: str = "cpu",
) -> FalsePositiveBenchmarkReport:
    """Load a trained LSTM and benchmark its false-positive rate on the sessions.

    Routes through the SAME pipeline the trainer uses (``build_dataset``:
    bindings/DBC/DID decode → derived signals → deviation columns →
    features) and re-applies the manifest's z-score scaler, so windows are
    scored in the calibrated-threshold domain. The old path here skipped
    decode + scaling entirely, which either crashed on the model's
    ``decoded:*``/``dbc:*`` schema or scored raw OBD units against scaled
    thresholds.
    """
    if not session_dirs:
        raise ValueError("at least one held-out session directory is required")
    trained = load_lstm_autoencoder_model(model_path, device=device)
    scaler = (
        ZScoreScaler.from_dict(dict(trained.manifest.scaler))
        if trained.manifest.scaler is not None
        else None
    )
    dataset = build_dataset(
        [Path(path) for path in session_dirs],
        config,
        sequence_length=int(trained.manifest.schema["sequence_length"]),
        feature_columns=trained.feature_columns,
        stride=stride,
        scaler=scaler,
        observation_mask=observation_mask_from_schema(trained.manifest.schema)[0],
    )
    return benchmark_false_positives(
        trained,
        dataset,
        k_grid=k_grid,
        batch_size=batch_size,
        device=device,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fp_benchmark",
        description=(
            "Benchmark an LSTM autoencoder's false-positive rate on held-out clean "
            "Parquet sessions; reports FPR, alerts/hour, and a k-threshold sweep."
        ),
    )
    parser.add_argument(
        "--model",
        type=Path,
        required=True,
        help="Trained LSTM autoencoder checkpoint (written by save_lstm_autoencoder_model).",
    )
    parser.add_argument(
        "session_dir",
        type=Path,
        nargs="+",
        help="One or more held-out clean Parquet session directories.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("canary.yaml"),
        help="Path to canary.yaml (default: ./canary.yaml).",
    )
    parser.add_argument(
        "--k",
        type=float,
        nargs="+",
        default=None,
        metavar="K",
        help="Candidate threshold multipliers to sweep (default: 1 2 3 4 5 6 8 10).",
    )
    parser.add_argument(
        "--stride",
        type=int,
        default=1,
        help="Sliding-window stride over the held-out sessions (default: 1).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="Inference batch size (default: 64).",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="Torch device for inference (default: cpu).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON report.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = CanaryConfig.from_yaml(args.config)
        report = run_false_positive_benchmark(
            args.model,
            args.session_dir,
            config,
            k_grid=args.k,
            stride=args.stride,
            batch_size=args.batch_size,
            device=args.device,
        )
    except MissingLSTMAutoencoderDependencyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    indent = 2 if args.pretty else None
    print(json.dumps(report.to_dict(), indent=indent, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
