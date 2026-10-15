#!/usr/bin/env python3
"""Train the Isolation Forest anomaly detector from recorded sessions.

The Phase 2 capstone: runs the full preprocessing pipeline (read sessions →
decode CAN signals via bindings.yaml → wide → resample → operating-mode
segment → rolling features), fits a per-operating-mode Isolation Forest plus
its z-score scaler, and writes a joblib artifact + JSON manifest.

Without `--evaluate` it just trains and saves. With `--evaluate <session>`
it additionally splices synthetic anomalies into a held-out session and
prints per-mode precision / recall / score distribution at the *served*
calibrated threshold, with withheld rows counted apart (A04.9). It no longer
recommends `contamination`: that knob only sets sklearn's `offset_`, which no
served decision reads. Held-out, drive-grouped evaluation is
`scripts/evaluate_detectors.py`.

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
from pathlib import Path

import pandas as pd

try:
    from _cli import (
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        add_profile_arg,
        load_scoped_config,
        scoped_models_dir,
    )
except ImportError:  # loaded as `scripts.train_anomaly_model` — repo root on path, not scripts/
    from scripts._cli import (  # type: ignore[no-redef]
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        add_profile_arg,
        load_scoped_config,
        scoped_models_dir,
    )

from src.config import CanaryConfig
from src.decode.bindings import load_bindings
from src.features.feature_paths import DEFAULT_BINDINGS_PATH
from src.features.feature_transform import configured_behavior_models, featurize_wide_frame
from src.log import configure_logging
from src.models.isolation_forest import (
    MissingModelDependencyError,
    TrainedIsolationForest,
)
from src.models.training_entry import train_isolation_forest
from src.preprocessing import (
    AnomalyInjector,
    decode_can_rows,
    discover_sessions,
    read_session,
    resample_wide,
    to_wide,
)
from src.preprocessing.resampler import resample_config_for
from src.telemetry import DECODED_SOURCE, source_of

log = logging.getLogger(__name__)


#: The IF training entry is ``src.models.training_entry`` (R06.4); this CLI's
#: historical name for it.
train = train_isolation_forest


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
    # One training-recipe config for both the resample and the dropout's
    # hold ceiling below, so the two cannot disagree (D03.27).
    resample_config = resample_config_for(config)
    resampled = resample_wide(
        wide,
        config=resample_config,
    )

    # Choose a numeric channel to perturb — a decoded signal if one's present.
    channel = spike_channel
    if channel is None:
        candidates = [
            c
            for c in resampled.columns
            if source_of(str(c)) == DECODED_SOURCE and bool(resampled[c].notna().any())
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
    # The recipe's one class rule (R04.7), broadcast class included (P02.10).
    max_gap_seconds = resample_config.max_gap_seconds_for(channel)
    inj = AnomalyInjector(resampled)
    inj.spike(channel=channel, start=t_a, end=t_b, value=spike_value)
    inj.dropout(channel=channel, start=t_c, end=t_d, max_gap_seconds=max_gap_seconds)

    # Same shared wide→features builder the trainer/serve paths use, so the
    # injected-fault evaluation scores the model on byte-identical schema (7.1).
    # read_session -> decode_can_rows decodes no Mode 22 DID: no DID catalog
    # describes this frame (P02.14).
    features = featurize_wide_frame(
        inj.df, config, behavior_models=configured_behavior_models(config), did_map=None
    ).features
    labels = inj.labels()
    # featurize_wide_frame may reorder by session/timestamp; realign labels.
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
        f"  overall  rows={report.rows}  withheld={report.withheld_rows} "
        f"({report.withheld_positive_rows} labelled)  "
        f"precision={_fmt(o.precision)}  recall={_fmt(o.recall)}  f1={_fmt(o.f1)}  "
        f"fpr={_fmt(o.false_positive_rate)}"
    )
    for mode, mode_eval in sorted(report.modes.items()):
        m = mode_eval.metrics
        sd = mode_eval.score_distribution
        print(
            f"  mode={mode:<12}  scored={m.rows:<6} withheld={mode_eval.withheld_rows:<5} "
            f"precision={_fmt(m.precision)}  recall={_fmt(m.recall)}  f1={_fmt(m.f1)}"
        )
        print(
            f"      threshold={_fmt(mode_eval.threshold)}  score dist: "
            f"min={_fmt(sd.minimum)} p50={_fmt(sd.median)} max={_fmt(sd.maximum)}"
        )


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
    add_common_args(parser)
    add_profile_arg(parser)
    add_bindings_arg(parser)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory for model.joblib + model.manifest.json. "
        "Default: the (profile-scoped) <models_dir>/isolation_forest from --config.",
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
    add_log_level_arg(parser)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    # Scoped once, here (P02.11): training and the --evaluate injection both
    # featurize with the vehicle's afr_calibration, as the daemon does.
    config = load_scoped_config(args, args.profile)
    args.out = args.out or scoped_models_dir(config, args.profile) / "isolation_forest"

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
