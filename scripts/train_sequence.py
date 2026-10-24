#!/usr/bin/env python3
"""Train either sequence-autoencoder anomaly detector from recorded sessions.

Task 2.1: the unified replacement for the near-duplicate ``scripts/train_lstm.py``
and ``scripts/train_tcn.py`` — same shared preprocessing pipeline (read
sessions → decode CAN via bindings.yaml → wide → resample → operating-mode
segment → rolling features), sliding windows from the clean-baseline
sessions, train + calibrate thresholds, write a Torch checkpoint + JSON
manifest — with the backbone selected by ``--backbone {lstm,tcn}`` instead of
by which script you ran.

The training contract itself (parked rows, validation drives, scaler,
unobserved/suppressed columns, feature set) is
``src.models.training_entry.train_sequence_detector`` (R06.4); this is its
CLI. ``scripts/train_lstm.py``/``scripts/train_tcn.py`` are shims over this
CLI with their backbone fixed, kept because ``src/control/jobs.py`` pins those
script paths.

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
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

try:
    from _cli import (  # type: ignore[import-not-found]
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        add_override_reason_arg,
        add_profile_arg,
        load_scoped_config,
        scoped_models_dir,
    )
except ImportError:  # loaded as `scripts.train_sequence` — repo root on path, not scripts/
    from scripts._cli import (
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        add_override_reason_arg,
        add_profile_arg,
        load_scoped_config,
        scoped_models_dir,
    )

from src.config import CanaryConfig
from src.features.feature_paths import DEFAULT_BINDINGS_PATH
from src.log import configure_logging
from src.models.evaluation import DriveHoldout, holdout_by_drive, nothing_left_to_train
from src.models.false_positive_benchmark import FalsePositiveBenchmarkReport
from src.models.sequence_autoencoder_training import TrainedSequenceAutoencoder
from src.models.sequence_dataset import DEFAULT_SEQUENCE_STRIDE
from src.models.sequence_feature_set import ALL_CHANNELS, FEATURE_SETS
from src.models.sequence_registry import SequenceTrainingBackend
from src.models.sequence_training import configure_torch_threads, default_training_device
from src.models.training_entry import (
    SEQUENCE_BACKBONES,
    benchmark_held_out,
    build_sequence_dataset,
    train_sequence_detector,
)
from src.preprocessing import discover_sessions

log = logging.getLogger(__name__)

#: Selectable backbones for this CLI's ``--backbone`` flag / ``bind_backbone``.
BACKBONES: dict[str, SequenceTrainingBackend] = SEQUENCE_BACKBONES

#: The training contract lives in ``src.models.training_entry`` (R06.4); these
#: are its names under this CLI's historical ones.
build_dataset = build_sequence_dataset
train = train_sequence_detector


def _config_block(config: CanaryConfig, backend: SequenceTrainingBackend) -> Any:
    return getattr(config.models, backend.config_attr)


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
    """Run the false-positive benchmark on held-out clean sessions and print a summary.

    Scores the served rule, the configured ``suppress_channels`` included
    (``training_entry.benchmark_held_out``, R04.1).
    """
    report = benchmark_held_out(
        backend,
        trained,
        session_dirs,
        config,
        bindings_path=bindings_path,
        stride=stride,
        device=device,
        k_grid=k_grid,
    )
    if report is None:
        log.warning("held-out sessions produced no sequence windows; skipping evaluation")
        return None
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
    add_bindings_arg(parser)
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
    add_override_reason_arg(
        parser,
        "--accept-thin-calibration",
        help="Publish even though the calibration sessions do not cover what the model "
        "will score (fewer than three drives, a scored mode without its own band, a "
        "channel never observed). Refused without this (A02.2); the reason and the "
        "violations are stamped into the manifest.",
    )
    parser.add_argument(
        "--feature-set",
        choices=FEATURE_SETS,
        default=ALL_CHANNELS,
        help="What the model conditions on and scores (A03.2/A03.3). all_channels "
        "(default, the deployed shape) scores every trainable column; "
        "engine_response conditions on driving inputs and scores only engine "
        "responses, one source per quantity, using the profile's sequence_roles.yaml. "
        "Recorded in the manifest's feature spec.",
    )
    parser.add_argument(
        "--evaluate-k",
        type=float,
        nargs="+",
        default=None,
        metavar="K",
        help="Candidate k multipliers for the benchmark sweep (default: 1 2 3 4 5 6 8 10).",
    )
    add_log_level_arg(parser)
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
    # Scoped once, here (P02.11): the feature frame and the vehicle's
    # sequence_roles.yaml are both read through the profile's data directory.
    config = load_scoped_config(args, args.profile)
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
    if holdout is not None and not sessions:
        print(f"error: {nothing_left_to_train(holdout)}", file=sys.stderr)
        return 2
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
            feature_set=args.feature_set,
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
    """Bind every backbone-parameterized function here to a fixed backbone,
    for the ``train_lstm.py``/``train_tcn.py`` shims."""
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
