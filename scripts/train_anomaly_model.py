#!/usr/bin/env python3
"""Train the Isolation Forest anomaly detector from recorded sessions.

The Phase 2 capstone: runs the full preprocessing pipeline (read sessions →
decode CAN signals via bindings.yaml → wide → resample → operating-mode
segment → rolling features), fits a per-operating-mode Isolation Forest plus
its z-score scaler, and writes a joblib artifact + JSON manifest.

Without `--evaluate` it just trains and saves. With `--evaluate <session>`
it additionally splices synthetic anomalies into a held-out session and
prints per-mode precision / recall / score-distribution so `contamination`
can be tuned.

Usage:
  scripts/train_anomaly_model.py --sessions-root data/sessions --out models/if
  scripts/train_anomaly_model.py data/sessions/2026-05-11T222447 --out models/if
  scripts/train_anomaly_model.py data/sessions/* --out models/if \
      --evaluate data/sessions/2026-05-11T222447

Sessions must contain Parquet chunks (telemetry-*.parquet) — convert
candump captures first with `tools/log_to_parquet.py`.
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
from src.decode.bindings import load_bindings
from src.log import configure_logging
from src.models.isolation_forest import (
    MissingModelDependencyError,
    TrainedIsolationForest,
    default_manifest_path,
    fit_isolation_forest,
)
from src.preprocessing import (
    AnomalyInjector,
    FeatureConfig,
    ResampleConfig,
    compute_features,
    decode_can_rows,
    discover_sessions,
    read_session,
    resample_wide,
    to_wide,
)
from src.preprocessing.feature_pipeline import (
    DEFAULT_BINDINGS_PATH,
    build_feature_frame,
    build_feature_spec,
)
from src.preprocessing.feature_pipeline import (
    segment_or_constant as _segment_or_constant,
)
from src.preprocessing.feature_pipeline import (
    trainable_feature_columns as _trainable_feature_columns,
)

log = logging.getLogger(__name__)


def train(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    out_dir: Path,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
) -> TrainedIsolationForest:
    """Build features, fit the per-mode IF + scaler, write artifact + manifest."""
    features = build_feature_frame(session_dirs, config, bindings_path=bindings_path)
    feature_columns = _trainable_feature_columns(features)
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
        feature_spec=build_feature_spec(features, feature_columns, config=config),
        fitted_at=datetime.now(UTC),
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


def evaluate(
    trained: TrainedIsolationForest,
    session_dir: Path,
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    spike_channel: str | None = None,
) -> None:
    """Splice synthetic anomalies into `session_dir` and print per-mode metrics."""
    from src.models.isolation_forest_evaluation import evaluate_isolation_forest

    bindings = load_bindings(bindings_path) if bindings_path.exists() else None
    long = read_session(session_dir)
    if bindings is not None:
        long = decode_can_rows(long, bindings)
    wide = to_wide(long, coerce_numeric=True)
    resampled = resample_wide(wide, config=ResampleConfig(hz=config.preprocessing.resample_hz))

    # Choose a numeric channel to perturb — a decoded signal if one's present.
    channel = spike_channel
    if channel is None:
        candidates = [
            c
            for c in resampled.columns
            if str(c).startswith("decoded:") and bool(resampled[c].notna().any())
        ]
        if not candidates:
            log.warning("no decoded channel available to inject; skipping evaluation")
            return
        channel = candidates[0]
    log.info("injecting synthetic anomalies", extra={"channel": channel})

    timestamps = pd.to_datetime(resampled["timestamp"], utc=True).sort_values()
    if len(timestamps) < 30:
        log.warning("session too short to evaluate; skipping")
        return
    # A short spike about a third of the way in, plus a dropout two-thirds in.
    t_a, t_b = timestamps.iloc[len(timestamps) // 3], timestamps.iloc[len(timestamps) // 3 + 10]
    t_c, t_d = (
        timestamps.iloc[2 * len(timestamps) // 3],
        timestamps.iloc[2 * len(timestamps) // 3 + 10],
    )
    spike_value = float(resampled[channel].dropna().max() * 5 + 1000)
    inj = AnomalyInjector(resampled)
    inj.spike(channel=channel, start=t_a, end=t_b, value=spike_value)
    inj.dropout(channel=channel, start=t_c, end=t_d)

    seg = _segment_or_constant(inj.df, config)
    features = compute_features(
        seg,
        config=FeatureConfig(window_seconds=config.preprocessing.rolling_window_seconds),
    )
    labels = inj.labels()
    # compute_features may reorder by session/timestamp; realign labels.
    labels = labels.reindex(features.index, fill_value=False)

    evaluation = evaluate_isolation_forest(trained, features, labels, windows=inj.windows)
    report = evaluation.report

    def _fmt(value: float | None, places: int = 3) -> str:
        return "n/a" if value is None else f"{value:.{places}f}"

    print("=" * 78)
    print(f"EVALUATION  {session_dir}  (injected spike + dropout on {channel})")
    print("=" * 78)
    o = report.overall
    print(
        f"  overall  rows={report.rows}  "
        f"precision={_fmt(o.precision)}  recall={_fmt(o.recall)}  f1={_fmt(o.f1)}  "
        f"fpr={_fmt(o.false_positive_rate)}"
    )
    for mode, mode_eval in sorted(report.modes.items()):
        m = mode_eval.metrics
        sd = mode_eval.score_distribution
        print(
            f"  mode={mode:<12}  rows={m.rows:<6}  "
            f"precision={_fmt(m.precision)}  recall={_fmt(m.recall)}  f1={_fmt(m.f1)}"
        )
        rec = mode_eval.contamination
        line = (
            f"      score dist: min={_fmt(sd.minimum)} p50={_fmt(sd.median)} max={_fmt(sd.maximum)}"
        )
        if rec.recommended_contamination is not None:
            line += f"  → recommend contamination ≈ {rec.recommended_contamination:.4f}"
        else:
            line += f"  ({rec.reason})"
        print(line)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="train_anomaly_model",
        description=(
            "Train the Isolation Forest anomaly detector from recorded Parquet "
            "sessions. Runs the full preprocessing pipeline (incl. CAN-signal "
            "decode via bindings.yaml), fits per-mode IF + scaler, writes a "
            "joblib artifact + JSON manifest."
        ),
    )
    parser.add_argument(
        "sessions",
        nargs="*",
        type=Path,
        help="Session directories. If none given, all sessions under --sessions-root.",
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
        default=Path("models/isolation_forest"),
        help="Output directory for model.joblib + model.manifest.json.",
    )
    parser.add_argument(
        "--evaluate",
        type=Path,
        default=None,
        help="A session to splice synthetic anomalies into and report metrics on.",
    )
    parser.add_argument(
        "--evaluate-channel",
        default=None,
        help="Channel to inject anomalies into during --evaluate (default: first decoded:*).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = CanaryConfig.from_yaml(args.config)

    sessions: list[Path] = list(args.sessions)
    if not sessions:
        sessions = discover_sessions(args.sessions_root)
    if not sessions:
        print(
            f"error: no sessions given and none found under {args.sessions_root}. "
            "Convert candump captures with tools/log_to_parquet.py first.",
            file=sys.stderr,
        )
        return 1

    try:
        trained = train(sessions, config, args.out, bindings_path=args.bindings)
    except MissingModelDependencyError as e:
        print(f"error: {e}", file=sys.stderr)
        print("Install the ML extras: pip install -e '.[ml]'", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"Trained Isolation Forest saved to {args.out}/")
    print(f"  feature columns: {len(trained.feature_columns)}")
    print(f"  operating modes: {sorted(trained.models.keys())}")

    if args.evaluate is not None:
        try:
            evaluate(
                trained,
                args.evaluate,
                config,
                bindings_path=args.bindings,
                spike_channel=args.evaluate_channel,
            )
        except (FileNotFoundError, ValueError) as e:
            print(f"error during evaluation: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
