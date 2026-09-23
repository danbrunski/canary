#!/usr/bin/env python3
"""LSTM-vs-TCN sequence-detector bake-off (Phase 9.7).

Trains both sequence-detector backbones on the *same* clean training sessions,
benchmarks both on the *same* held-out clean sessions, and prints a side-by-side
comparison so an operator can pick the production default. The gate (per the
plan): the TCN should match or beat the LSTM at the ≤1 alert/hour budget within
the Pi budget; otherwise the LSTM stays the default and the TCN ships as a
selectable alternative.

Beyond the aggregate false-positive rate, the report breaks the per-channel FPR
out by **rate class** — dense ~1 Hz CAN vs slow/gappy round-robin OBD/DID/GPS —
because GRU-D's masking advantage concentrates on the slow channels, so an
aggregate-only number can hide where the two backbones actually differ.

Usage:
  scripts/sequence_bakeoff.py --sessions-root data/sessions \
      --evaluate data/sessions/holdout_drive
  scripts/sequence_bakeoff.py data/sessions/train_* \
      --evaluate data/sessions/holdout_a data/sessions/holdout_b --json out.json

Requires the ``ml`` extra (torch); run ``scripts/install_deps.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Reuse the per-backbone training CLIs as importable libraries (same pattern as
# retrain.py); they expose identical train()/build_dataset() signatures.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import retrain  # noqa: E402 — reuse its auto-holdout picker (Task 3.3)
import train_lstm as lstm_trainer  # noqa: E402
import train_tcn as tcn_trainer  # noqa: E402
from _cli import add_common_args, load_config  # noqa: E402

from src.config import CanaryConfig  # noqa: E402
from src.log import configure_logging  # noqa: E402
from src.models.evaluation import holdout_by_drive  # noqa: E402
from src.models.false_positive_benchmark import (  # noqa: E402
    FalsePositiveBenchmarkReport,
    benchmark_false_positives,
)
from src.models.sequence_training import (  # noqa: E402
    configure_torch_threads,
    default_training_device,
)
from src.models.tcn import MissingTCNDependencyError  # noqa: E402
from src.preprocessing import discover_sessions  # noqa: E402
from src.preprocessing.feature_pipeline import DEFAULT_BINDINGS_PATH  # noqa: E402
from src.preprocessing.normalization import ZScoreScaler  # noqa: E402

log = logging.getLogger(__name__)

#: Channel source prefixes whose readings arrive on the slow/gappy round-robin
#: (OBD Mode 01/22 PIDs, named DIDs) or an intermittent fix (GPS). Everything
#: else (broadcast CAN ``decoded:``/``dbc:`` and ``derived:`` combinations) is
#: treated as dense ~1 Hz. This is the seam GRU-D masking targets.
SLOW_CHANNEL_PREFIXES = ("obd", "did", "gps")


@dataclass(frozen=True)
class _Backbone:
    """One selectable backbone: its model_type + the trainer CLI module."""

    key: str
    model_type: str
    trainer: object  # train_lstm or train_tcn module (duck-typed train/build_dataset)


_BACKBONES: dict[str, _Backbone] = {
    "lstm": _Backbone("lstm", "lstm_autoencoder", lstm_trainer),
    "tcn": _Backbone("tcn", "tcn_autoencoder", tcn_trainer),
}


def channel_rate_class(column: str) -> str:
    """Classify a feature column as ``"slow"`` (round-robin OBD/DID/GPS) or
    ``"dense"`` (everything else). Rolling-feature suffixes keep the source
    prefix, so ``obd:RPM__mean`` classifies the same as ``obd:RPM``."""
    source = column.split(":", 1)[0]
    return "slow" if source in SLOW_CHANNEL_PREFIXES else "dense"


@dataclass(frozen=True)
class RateClassFPR:
    """Mean per-channel false-positive rate for one rate class."""

    rate_class: str
    channel_count: int
    mean_false_positive_rate: float

    def to_dict(self) -> dict[str, object]:
        return {
            "rate_class": self.rate_class,
            "channel_count": self.channel_count,
            "mean_false_positive_rate": self.mean_false_positive_rate,
        }


@dataclass(frozen=True)
class BackboneResult:
    """One backbone's bake-off outcome."""

    key: str
    model_type: str
    model_version: str
    artifact_bytes: int
    window_count: int
    false_positive_rate: float
    alerts_per_hour: float
    meets_v1_budget: bool
    recommended_k: float | None
    rate_class_fpr: tuple[RateClassFPR, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "model_type": self.model_type,
            "model_version": self.model_version,
            "artifact_bytes": self.artifact_bytes,
            "window_count": self.window_count,
            "false_positive_rate": self.false_positive_rate,
            "alerts_per_hour": self.alerts_per_hour,
            "meets_v1_budget": self.meets_v1_budget,
            "recommended_k": self.recommended_k,
            "rate_class_fpr": [item.to_dict() for item in self.rate_class_fpr],
        }


def _rate_class_breakdown(report: FalsePositiveBenchmarkReport) -> tuple[RateClassFPR, ...]:
    buckets: dict[str, list[float]] = {"dense": [], "slow": []}
    for summary in report.channel_summaries:
        buckets[channel_rate_class(summary.channel)].append(summary.false_positive_rate)
    return tuple(
        RateClassFPR(
            rate_class=rate_class,
            channel_count=len(rates),
            mean_false_positive_rate=(sum(rates) / len(rates) if rates else 0.0),
        )
        for rate_class, rates in buckets.items()
    )


def _recommended_k(report: FalsePositiveBenchmarkReport) -> float | None:
    meeting = [point.k for point in report.k_sweep if point.meets_v1_budget]
    return min(meeting) if meeting else None


def run_backbone(
    backbone: _Backbone,
    train_sessions: Sequence[Path],
    eval_sessions: Sequence[Path],
    config: CanaryConfig,
    out_dir: Path,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    device: str = "cpu",
    k_grid: Sequence[float] | None = None,
    calibration_sessions: Sequence[Path] | None = None,
    calibration_override: str | None = None,
) -> BackboneResult:
    """Train one backbone, benchmark it on held-out sessions, summarize.

    ``calibration_sessions`` (held out from training, and distinct from the
    eval sessions) drives the trainer's out-of-sample threshold calibration
    so the bake-off's FPR reflects production firing on unseen drives instead
    of the in-sample memorization artifact.
    """
    trainer = backbone.trainer
    trained = trainer.train(  # type: ignore[attr-defined]
        train_sessions,
        config,
        out_dir,
        bindings_path=bindings_path,
        device=device,
        calibration_sessions=calibration_sessions,
        calibration_override=calibration_override,
    )
    artifact_path = out_dir / "model.pt"
    # Z-score the held-out frame with the freshly trained model's own scaler so
    # benchmark reconstruction error matches the calibrated-threshold domain
    # (same scaling the live serve path applies per window).
    scaler = (
        ZScoreScaler.from_dict(dict(trained.manifest.scaler))
        if trained.manifest.scaler is not None
        else None
    )
    eval_dataset = trainer.build_dataset(  # type: ignore[attr-defined]
        eval_sessions,
        config,
        bindings_path=bindings_path,
        sequence_length=int(trained.manifest.schema["sequence_length"]),
        feature_columns=trained.feature_columns,
        scaler=scaler,
    )
    report = benchmark_false_positives(trained, eval_dataset, k_grid=k_grid, device=device)
    return BackboneResult(
        key=backbone.key,
        model_type=backbone.model_type,
        model_version=report.model_version,
        artifact_bytes=artifact_path.stat().st_size if artifact_path.exists() else 0,
        window_count=report.window_count,
        false_positive_rate=report.false_positive_rate,
        alerts_per_hour=report.alerts_per_hour,
        meets_v1_budget=report.meets_v1_budget,
        recommended_k=_recommended_k(report),
        rate_class_fpr=_rate_class_breakdown(report),
    )


def run_bakeoff(
    train_sessions: Sequence[Path],
    eval_sessions: Sequence[Path],
    config: CanaryConfig,
    out_root: Path,
    *,
    backbones: Sequence[str] = ("lstm", "tcn"),
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    device: str = "cpu",
    k_grid: Sequence[float] | None = None,
    calibration_sessions: Sequence[Path] | None = None,
    calibration_override: str | None = None,
) -> list[BackboneResult]:
    """Train + benchmark each requested backbone on identical splits.

    The selected backbones are force-enabled in the in-memory config for the
    run — the bake-off is an offline experiment, so it trains both regardless of
    which one ``sequence_detector`` ships.

    ``calibration_sessions`` are passed through to each backbone's trainer so
    thresholds are calibrated on those *unseen* drives (out-of-sample) rather
    than the training windows. They must be distinct from ``eval_sessions`` so
    the FP benchmark stays a true held-out test.
    """
    results: list[BackboneResult] = []
    for key in backbones:
        backbone = _BACKBONES[key]
        # Force-enable so the trainer's _validate_config doesn't reject a
        # backbone that isn't the configured production default.
        getattr(config.models, backbone.model_type).enabled = True
        log.info("bake-off: training %s", backbone.model_type)
        results.append(
            run_backbone(
                backbone,
                train_sessions,
                eval_sessions,
                config,
                out_root / backbone.model_type,
                bindings_path=bindings_path,
                device=device,
                k_grid=k_grid,
                calibration_sessions=calibration_sessions,
                calibration_override=calibration_override,
            )
        )
    return results


def _print_bakeoff(results: Sequence[BackboneResult], eval_sessions: Sequence[Path]) -> None:
    names = ", ".join(Path(p).name for p in eval_sessions)
    print("=" * 78)
    print(f"SEQUENCE-DETECTOR BAKE-OFF  (held-out clean sessions: {names})")
    print("=" * 78)
    for result in results:
        budget = "YES" if result.meets_v1_budget else "NO"
        rec = "n/a" if result.recommended_k is None else f"{result.recommended_k:g}"
        print(f"  {result.model_type}  ({result.model_version})")
        print(
            f"    windows={result.window_count}  artifact={result.artifact_bytes / 1024:.1f} KiB  "
            f"calibrated FPR={result.false_positive_rate:.4f}  "
            f"alerts/h={result.alerts_per_hour:.2f}  ≤1/h budget: {budget}  "
            f"recommend k≈{rec}"
        )
        for item in result.rate_class_fpr:
            print(
                f"      {item.rate_class:<5} channels={item.channel_count:<3} "
                f"mean per-channel FPR={item.mean_false_positive_rate:.4f}"
            )
    print()
    print(_verdict(results))


def _verdict(results: Sequence[BackboneResult]) -> str:
    by_key = {r.key: r for r in results}
    if "lstm" not in by_key or "tcn" not in by_key:
        return "  (train both backbones for a head-to-head verdict)"
    lstm, tcn = by_key["lstm"], by_key["tcn"]
    # The gate is at the ≤1/hr budget: prefer the lower alert rate, but only the
    # TCN displacing the LSTM flips the default (ties keep the incumbent).
    if tcn.alerts_per_hour < lstm.alerts_per_hour and tcn.meets_v1_budget:
        verdict = "TCN beats LSTM on alert rate within budget — candidate new default"
    elif lstm.alerts_per_hour <= tcn.alerts_per_hour:
        verdict = "LSTM matches or beats TCN — keep LSTM as default; TCN stays selectable"
    else:
        verdict = "TCN lower but over budget — keep LSTM default"
    slow_l = next((c for c in lstm.rate_class_fpr if c.rate_class == "slow"), None)
    slow_t = next((c for c in tcn.rate_class_fpr if c.rate_class == "slow"), None)
    slow_note = ""
    if slow_l is not None and slow_t is not None:
        slow_note = (
            f"  slow-channel mean FPR: lstm={slow_l.mean_false_positive_rate:.4f} "
            f"tcn={slow_t.mean_false_positive_rate:.4f}"
        )
    return f"  → {verdict}\n{slow_note}" if slow_note else f"  → {verdict}"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sequence_bakeoff",
        description=(
            "Train the LSTM and TCN sequence detectors on the same sessions, "
            "benchmark both on the same held-out clean sessions, and print a "
            "side-by-side false-positive comparison with a dense-vs-slow "
            "channel-rate-class breakdown (Phase 9.7)."
        ),
    )
    parser.add_argument("sessions", nargs="*", type=Path, help="Training session directories.")
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path("data/sessions"),
        help="Where to discover training sessions when none given (minus --evaluate).",
    )
    parser.add_argument(
        "--evaluate",
        nargs="+",
        type=Path,
        required=True,
        metavar="SESSION",
        help="Held-out clean session(s) to benchmark both backbones on.",
    )
    parser.add_argument(
        "--calibration-sessions",
        nargs="+",
        type=Path,
        default=None,
        metavar="SESSION",
        help="Held-out clean session(s) for out-of-sample threshold calibration. "
        "Must be distinct from --evaluate (and absent from positional train "
        "sessions) so calibration and benchmark stay independent. Without this "
        "thresholds are calibrated on the training windows themselves, which "
        "inflates cross-session false positives on both backbones.",
    )
    parser.add_argument(
        "--accept-thin-calibration",
        default=None,
        metavar="REASON",
        help="Train even though the calibration sessions do not cover what the models "
        "will score (fewer than three drives, a scored mode without its own band, a "
        "channel never observed). Refused without this (A02.2); the reason is stamped "
        "into each manifest.",
    )
    add_common_args(parser)
    parser.add_argument("--bindings", type=Path, default=DEFAULT_BINDINGS_PATH)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("models/_bakeoff"),
        help="Scratch directory for the trained artifacts (per-backbone subdirs).",
    )
    parser.add_argument(
        "--backbones",
        nargs="+",
        choices=sorted(_BACKBONES),
        default=["lstm", "tcn"],
        help="Backbones to compare (default: both).",
    )
    parser.add_argument(
        "--device",
        default=default_training_device(),
        help="Torch device (default: cuda when available, else cpu).",
    )
    parser.add_argument(
        "--evaluate-k",
        type=float,
        nargs="+",
        default=None,
        metavar="K",
        help="Candidate k multipliers for the sweep (default: 1 2 3 4 5 6 8 10).",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Also write the full comparison as JSON to this path.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def _resolve_training_sessions(args: argparse.Namespace) -> list[Path] | None:
    """Training sessions without the --evaluate / --calibration-sessions
    drives (``holdout_by_drive``: siblings of a held-out session go with it,
    and a drive on both sides is refused with ``ValueError``)."""
    pool = list(args.sessions) if args.sessions else discover_sessions(args.sessions_root)
    if not pool:
        return None
    holdout = holdout_by_drive(
        pool,
        calibration=list(args.calibration_sessions or ()),
        evaluation=list(args.evaluate or ()),
    )
    args.calibration_sessions = list(holdout.calibration) or None
    args.evaluate = list(holdout.evaluation)
    return list(holdout.training) or None


def _auto_carve_calibration_sessions(sessions: Sequence[Path]) -> list[Path]:
    """Auto-hold-out calibration sessions from ``sessions`` (Task 3.3).

    Reuses ``retrain.py``'s auto-holdout picker (most-recent *substantial*
    sessions, chosen for operating-mode coverage) rather than inventing a new
    splitting strategy. Returns ``[]`` when the corpus is too small to spare a
    holdout (fewer than 3 sessions) — the caller must treat that as a hard
    stop, not a silent in-sample fallback.
    """
    if len(sessions) < 3:
        return []
    return retrain._pick_calibration_holdouts(sessions, max_holdouts=min(3, len(sessions) - 2))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    configure_torch_threads()
    config = load_config(args)

    log.info("bake-off device=%s", args.device)
    try:
        sessions = _resolve_training_sessions(args)
    except ValueError as exc:
        # Calibration and evaluation must be disjoint, by physical drive, so
        # the FP benchmark stays a true held-out test (no peeking).
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not sessions:
        print(
            f"error: no training sessions given and none found under {args.sessions_root}.",
            file=sys.stderr,
        )
        return 1

    # Task 3.3: a default (no --calibration-sessions) invocation used to
    # calibrate in-sample silently — the exact FPR≈1.0 flood mechanism. Auto-
    # carve a held-out split from the training set (same convention as
    # retrain.py's calibration_holdout); if the corpus is too small to spare
    # one, fail loudly instead of falling back to in-sample.
    calibration_sessions = args.calibration_sessions
    if not calibration_sessions:
        calibration_sessions = _auto_carve_calibration_sessions(sessions)
        if not calibration_sessions:
            print(
                "error: no --calibration-sessions given and none could be "
                "auto-carved from the training set (need at least 3 "
                "substantial sessions to spare one for out-of-sample "
                "calibration) — a default bake-off run must not silently "
                "calibrate in-sample. Pass --calibration-sessions explicitly.",
                file=sys.stderr,
            )
            return 1
        try:
            carved = holdout_by_drive(
                sessions, calibration=calibration_sessions, evaluation=list(args.evaluate)
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        calibration_sessions = list(carved.calibration)
        sessions = list(carved.training)
        log.info(
            "auto-carved calibration holdout sessions",
            extra={"calibration_sessions": [str(s) for s in calibration_sessions]},
        )

    try:
        results = run_bakeoff(
            sessions,
            args.evaluate,
            config,
            args.out,
            backbones=args.backbones,
            bindings_path=args.bindings,
            device=args.device,
            k_grid=args.evaluate_k,
            calibration_sessions=calibration_sessions,
            calibration_override=args.accept_thin_calibration,
        )
    except MissingTCNDependencyError as e:
        print(f"error: {e}", file=sys.stderr)
        print("Install the ML extras: pip install -e '.[ml]'", file=sys.stderr)
        return 1
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    _print_bakeoff(results, args.evaluate)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = {"results": [r.to_dict() for r in results]}
        args.json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
