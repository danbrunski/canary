#!/usr/bin/env python3
"""Corpus-wide detector benchmark (Phase 2B, Tasks 2.5/2.6).

Grids (held-out clean sessions x fault kinds x target channels) through the
synthetic-fault harness (`src.models.synthetic_fault_eval.evaluate_detectors`,
2.3/2.4) and rolls the per-cell reports up into one per-detector x
per-fault-class table: recall, an estimated precision, median time-to-detect,
and the clean-corpus alerts-per-hour rate. Prints the table and writes the
full report (aggregate + every per-cell detail) as JSON.

`--baseline <report.json>` (2.6) diffs this run's table against a previous
one, cell by cell, and flags any metric that regressed beyond `--tolerance`
(`diff_reports`); the pre-deploy gate `scripts/retrain.py` runs after itself.

Usage:
  scripts/detector_benchmark.py --sessions-root data/sessions
  scripts/detector_benchmark.py data/sessions/a data/sessions/b \
      --if-model models/isolation_forest/model.joblib
  scripts/detector_benchmark.py --channels obd:RPM obd:SPEED --fault-kinds spike dropout
  scripts/detector_benchmark.py --baseline data/analyses/benchmark-20260701-000000.json

Requires a trained Isolation Forest artifact (`--if-model`, default
`<models_dir>/<isolation_forest_artifact>` from config); the sequence
detector is optional and skipped gracefully when the configured backend
isn't enabled/available (mirrors `evaluate_detectors`'s own
`trained_sequence=None` handling).

Known limitation: `evaluate_detectors` (2.3/2.4) always calls
`featurize_wide_frame(resampled, config)` with no `behavior_models` — an IF
artifact trained with behavior-model deviation (`deviation:*`) feature
columns can't be schema-matched here (that's a gap in the already-completed
2.3/2.4 harness, not something this CLI re-opens). Bindings-decoded
(`decoded:*`) CAN channels work fine; only a deviation-column-trained
artifact needs a dedicated non-deviation IF artifact for this benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pandas as pd

try:
    from _cli import add_common_args, load_config
except ImportError:  # loaded as `scripts.detector_benchmark` — repo root on path, not scripts/
    from scripts._cli import add_common_args, load_config  # type: ignore[no-redef]

from src.config import CanaryConfig
from src.decode.bindings import load_bindings
from src.log import configure_logging
from src.models.isolation_forest import TrainedIsolationForest, load_isolation_forest_model
from src.models.sequence_protocol import TrainedSequenceModel
from src.models.sequence_registry import resolve_sequence_backend
from src.models.synthetic_fault_eval import DEFAULT_FAULT_KINDS, evaluate_detectors
from src.preprocessing import AnomalyKind, decode_can_rows, discover_sessions, read_session, to_wide
from src.preprocessing.feature_pipeline import DEFAULT_BINDINGS_PATH
from src.preprocessing.loader import numeric_value_columns
from src.preprocessing.resampler import (
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
    ResampleConfig,
    resample_wide,
)

log = logging.getLogger(__name__)

#: A default spread of engine-telemetry channels this repo's OBD capture
#: reliably produces (see `src/analysis/events/*.py`, `src/analysis/derived.py`)
#: — enough variety (a fast RPM-derived signal, a slow-changing temperature,
#: a driver-input signal) to exercise the detectors without hardcoding this
#: vehicle's full PID set.
DEFAULT_TARGET_CHANNELS: tuple[str, ...] = (
    "obd:RPM",
    "obd:SPEED",
    "obd:COOLANT_TEMP",
    "obd:THROTTLE_POS",
    "obd:ENGINE_LOAD",
)

#: Regression tolerance (Task 2.6), a single global relative-change fraction
#: applied to every metric. WHY 0.15 and not tighter: the grid is small (a
#: handful of sessions x channels, `n_injections` runs per cell) so a per-cell
#: `recall` can swing by a whole injection flipping detected/undetected —
#: on an 8-run cell that's 12.5 pts of "recall" with zero real behavior
#: change. A tolerance below that floor would flag grid noise as regression
#: on every run; 0.15 clears it with margin while still catching an actual
#: broken detector (whose recall/precision typically collapses by much more
#: than one flipped run, or whose alerts-per-hour-clean — pooled over full
#: corpus exposure hours, not a handful of runs, so it's less noisy per-run —
#: jumps well past a 15% bump from an actual false-alarm flood).
DEFAULT_TOLERANCE = 0.15

#: Which direction is "worse" for each rollup metric a regression compare
#: cares about — `precision` can be `None` (no denominator) and is skipped
#: rather than treated as -inf/+inf.
_REGRESSION_DIRECTION: dict[str, str] = {
    "recall": "down",
    "precision": "down",
    "median_time_to_detect_seconds": "up",
    "alerts_per_hour_clean": "up",
}


@dataclass(frozen=True)
class GridCell:
    """One (session, channel) grid unit — its `evaluate_detectors` report
    covers every requested fault kind for that channel in one call."""

    session: str
    channel: str
    seed: int
    report: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "channel": self.channel,
            "seed": self.seed,
            "report": self.report,
        }


def cell_seed(base_seed: int, session: str, channel: str) -> int:
    """Deterministic per-(session, channel) seed derived from `--seed`.

    `hash()` is salted per-process (`PYTHONHASHSEED`) and would make two runs
    with the same `--seed` diverge; SHA-256 of the identity string doesn't.
    """
    digest = hashlib.sha256(f"{base_seed}:{session}:{channel}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def select_target_channels(resampled: pd.DataFrame, candidates: Sequence[str]) -> list[str]:
    """Candidates actually usable in this session's frame.

    `AnomalyInjector` raises `KeyError` on a channel absent from the frame,
    and every injection kind needs at least one real (non-NaN) value to
    perturb or hold — so a candidate missing or all-NaN in this session is
    silently skipped rather than crashing the whole grid.
    """
    return [c for c in candidates if c in resampled.columns and resampled[c].notna().any()]


def load_resampled_session(
    session_dir: Path,
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
) -> pd.DataFrame:
    """Read + decode + wide-pivot + resample one session (no segmentation —
    2.3/2.4's `evaluate_detectors` calls `featurize_wide_frame`, which
    segments internally, so doing it here too would be redundant work).

    CAN-signal decode (`decoded:*` columns, same as `train_anomaly_model.py`)
    runs when `bindings_path` exists so an IF artifact trained on decoded
    channels schema-matches; skipped (not an error) when absent, matching
    `train_anomaly_model.py`'s own optional-bindings handling."""
    long = read_session(session_dir)
    if bindings_path.exists():
        long = decode_can_rows(long, load_bindings(bindings_path))
    wide = to_wide(long)
    value_columns = numeric_value_columns(wide)
    if not value_columns:
        return pd.DataFrame(
            {
                SESSION_COLUMN: wide[SESSION_COLUMN].copy()
                if SESSION_COLUMN in wide.columns
                else pd.Series(dtype="object"),
                TIMESTAMP_COLUMN: pd.to_datetime(wide[TIMESTAMP_COLUMN], utc=True)
                if TIMESTAMP_COLUMN in wide.columns
                else pd.Series(dtype="datetime64[ns, UTC]"),
            }
        )
    return resample_wide(
        wide,
        config=ResampleConfig(
            hz=config.preprocessing.resample_hz,
            slow_channel_max_gap_seconds=config.preprocessing.slow_channel_max_gap_seconds,
        ),
        value_columns=value_columns,
    )


def run_grid(
    sessions: Sequence[tuple[str, pd.DataFrame]],
    config: CanaryConfig,
    trained_if: TrainedIsolationForest,
    *,
    fault_kinds: Sequence[AnomalyKind] = DEFAULT_FAULT_KINDS,
    channels: Sequence[str] = DEFAULT_TARGET_CHANNELS,
    n_injections: int = 5,
    seed: int = 42,
    trained_sequence: TrainedSequenceModel | None = None,
) -> list[GridCell]:
    """Run `evaluate_detectors` over every (session, usable channel) cell.

    One call per cell covers every fault kind for that channel (the harness
    already loops fault kinds internally) plus that cell's clean/nominal
    pass — nothing here re-implements either.
    """
    cells: list[GridCell] = []
    for session_name, resampled in sessions:
        usable = select_target_channels(resampled, channels)
        if not usable:
            log.warning("session %s: no requested channel usable; skipping", session_name)
            continue
        for channel in usable:
            seed_for_cell = cell_seed(seed, session_name, channel)
            try:
                report = evaluate_detectors(
                    resampled,
                    config,
                    trained_if,
                    fault_channel=channel,
                    fault_kinds=fault_kinds,
                    n_injections=n_injections,
                    seed=seed_for_cell,
                    trained_sequence=trained_sequence,
                )
            except KeyError as e:
                # The clean/nominal pass (channel-independent) is scored
                # first inside evaluate_detectors, so a schema mismatch
                # between this session's captured channels and the IF
                # artifact's trained feature columns (e.g. a session with no
                # GPS fix against a model trained with a GPS committee
                # feature) fails identically for every channel — skip the
                # rest of this session rather than repeat the same error.
                log.warning(
                    "session %s: IF model feature schema mismatch (%s); "
                    "skipping remaining channels for this session",
                    session_name,
                    e,
                )
                break
            cells.append(
                GridCell(session=session_name, channel=channel, seed=seed_for_cell, report=report)
            )
    return cells


def _nominal_by_session(cells: Sequence[GridCell]) -> dict[str, dict[str, Any]]:
    """One `nominal_fp` block per session.

    `evaluate_detectors`'s clean/nominal pass scores the unmodified frame —
    identical across every channel of the same session — so keeping only the
    first cell's block per session avoids double-counting the same clean
    hours once per channel.
    """
    by_session: dict[str, dict[str, Any]] = {}
    for cell in cells:
        by_session.setdefault(cell.session, cell.report["nominal_fp"])
    return by_session


def aggregate_cells(cells: Sequence[GridCell]) -> dict[str, Any]:
    """Roll the per-(session, channel) grid up into one per-detector x
    per-fault-class table.

    - ``recall`` pools raw `detected`/`runs` across every cell for that
      (detector, fault_kind) — a session/channel-weighted average, not a
      mean of per-cell rates, so a channel with more injections carries
      proportionally more weight.
    - ``alerts_per_hour_clean`` pools each detector's clean-pass alerts and
      hours once per SESSION (see `_nominal_by_session`) into one corpus-wide
      rate; it's fault-kind-agnostic (a detector's baseline FP rate doesn't
      depend on which fault it's being graded against) so the same number
      repeats across that detector's fault-kind rows.
    - ``precision`` has no labeled negatives to count directly — the harness
      only injects positives. It's estimated instead: the detector's clean
      corpus alert rate predicts how many false alarms it would raise, absent
      the fault, over the same total exposure time (`n_injections` full-length
      scoring passes per cell) the fault kind's grid cells covered.
      `TP / (TP + FP_estimate)` is the closest analogue to precision this
      harness can honestly report — not a substitute for a labeled
      precision/recall curve over real negatives.
    """
    nominal_by_session = _nominal_by_session(cells)
    detector_names: set[str] = set()
    for nominal in nominal_by_session.values():
        detector_names.update(nominal)

    alerts_per_hour: dict[str, float] = {}
    nominal_totals: dict[str, dict[str, float]] = {}
    for name in sorted(detector_names):
        total_alerts = sum(n[name]["alerts"] for n in nominal_by_session.values() if name in n)
        total_hours = sum(n[name]["hours"] for n in nominal_by_session.values() if name in n)
        rate = (total_alerts / total_hours) if total_hours else 0.0
        alerts_per_hour[name] = rate
        nominal_totals[name] = {
            "alerts": float(total_alerts),
            "hours": float(total_hours),
            "per_hour": rate,
        }

    pooled: dict[tuple[str, str], dict[str, Any]] = {}
    for cell in cells:
        for metric in cell.report["per_fault"]:
            key = (metric["detector"], metric["fault_kind"])
            bucket = pooled.setdefault(
                key, {"runs": 0, "detected": 0, "latencies": [], "exposure_hours": 0.0}
            )
            bucket["runs"] += metric["runs"]
            bucket["detected"] += metric["detected"]
            bucket["latencies"].extend(metric["latencies_seconds"])
            hours = cell.report["nominal_fp"].get(metric["detector"], {}).get("hours", 0.0)
            bucket["exposure_hours"] += hours * metric["runs"]

    table: list[dict[str, Any]] = []
    for (detector, fault_kind), bucket in sorted(pooled.items()):
        runs = bucket["runs"]
        detected = bucket["detected"]
        recall = (detected / runs) if runs else 0.0
        per_hour = alerts_per_hour.get(detector, 0.0)
        fp_estimate = per_hour * bucket["exposure_hours"]
        denom = detected + fp_estimate
        precision = (detected / denom) if denom > 0 else None
        latencies = bucket["latencies"]
        median_latency = float(statistics.median(latencies)) if latencies else None
        table.append(
            {
                "detector": detector,
                "fault_kind": fault_kind,
                "runs": runs,
                "detected": detected,
                "recall": recall,
                "precision": precision,
                "median_time_to_detect_seconds": median_latency,
                "alerts_per_hour_clean": per_hour,
            }
        )

    return {
        "sessions": sorted(nominal_by_session),
        "channels": sorted({cell.channel for cell in cells}),
        "fault_kinds": sorted({fault_kind for _, fault_kind in pooled}),
        "nominal_fp": nominal_totals,
        "table": table,
    }


def build_report(
    cells: Sequence[GridCell],
    *,
    seed: int,
    n_injections: int,
    generated_at: str,
) -> dict[str, Any]:
    """The full JSON-safe report: aggregate table + every per-cell detail
    (kept so `--baseline` regression compare — 2.6, via `diff_reports` — has
    the raw per-session/channel numbers, not just the rolled-up table)."""
    aggregate = aggregate_cells(cells)
    return {
        "generated_at": generated_at,
        "seed": seed,
        "n_injections": n_injections,
        "grid_cells": len(cells),
        **aggregate,
        "cells": [cell.to_dict() for cell in cells],
    }


def print_report(report: dict[str, Any]) -> None:
    sessions = ", ".join(report["sessions"]) or "(none)"
    print("=" * 88)
    print(f"DETECTOR BENCHMARK  seed={report['seed']}  sessions: {sessions}")
    print("=" * 88)
    if not report["table"]:
        print("  (no grid cells — no sessions, or no requested channel usable in any of them)")
        return
    header = (
        f"  {'detector':<16} {'fault':<8} {'runs':>5} {'recall':>7} "
        f"{'precision':>10} {'med.t2d(s)':>11} {'alerts/hr':>10}"
    )
    print(header)
    for row in report["table"]:
        precision = "n/a" if row["precision"] is None else f"{row['precision']:.3f}"
        latency = (
            "n/a"
            if row["median_time_to_detect_seconds"] is None
            else f"{row['median_time_to_detect_seconds']:.1f}"
        )
        print(
            f"  {row['detector']:<16} {row['fault_kind']:<8} {row['runs']:>5} "
            f"{row['recall']:>7.3f} {precision:>10} {latency:>11} "
            f"{row['alerts_per_hour_clean']:>10.3f}"
        )


def diff_reports(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Per-(detector, fault_kind) regression compare (Task 2.6).

    Only cells present in BOTH reports' ``table`` are graded against
    ``tolerance`` — a cell that only exists in one report (a detector/channel
    dropped or added between runs) is recorded as ``added``/``removed`` for
    visibility but can't have "regressed", there being nothing to compare.
    """
    baseline_by_key = {(row["detector"], row["fault_kind"]): row for row in baseline["table"]}
    current_by_key = {(row["detector"], row["fault_kind"]): row for row in current["table"]}

    cells: list[dict[str, Any]] = []
    any_regressed = False
    for detector, fault_kind in sorted(set(baseline_by_key) | set(current_by_key)):
        base_row = baseline_by_key.get((detector, fault_kind))
        cur_row = current_by_key.get((detector, fault_kind))
        if base_row is None:
            cells.append(
                {"detector": detector, "fault_kind": fault_kind, "status": "added", "metrics": {}}
            )
            continue
        if cur_row is None:
            cells.append(
                {
                    "detector": detector,
                    "fault_kind": fault_kind,
                    "status": "removed",
                    "metrics": {},
                }
            )
            continue

        metrics: dict[str, Any] = {}
        for metric_name, direction in _REGRESSION_DIRECTION.items():
            base_value = base_row.get(metric_name)
            cur_value = cur_row.get(metric_name)
            regressed = False
            if base_value is not None and cur_value is not None:
                if direction == "down":
                    regressed = cur_value < base_value * (1 - tolerance)
                else:
                    regressed = cur_value > base_value * (1 + tolerance)
            metrics[metric_name] = {
                "baseline": base_value,
                "current": cur_value,
                "regressed": regressed,
            }
            any_regressed = any_regressed or regressed
        cells.append(
            {
                "detector": detector,
                "fault_kind": fault_kind,
                "status": "compared",
                "metrics": metrics,
            }
        )

    return {"tolerance": tolerance, "regressed": any_regressed, "cells": cells}


def print_regression_diff(diff: dict[str, Any]) -> None:
    print("=" * 88)
    print(f"REGRESSION COMPARE  tolerance={diff['tolerance']:.0%}")
    print("=" * 88)
    if not diff["cells"]:
        print("  (no baseline cells to compare)")
        return
    any_flagged = False
    for cell in diff["cells"]:
        label = f"{cell['detector']:<16} {cell['fault_kind']:<8}"
        if cell["status"] != "compared":
            only_in = "current" if cell["status"] == "added" else "baseline"
            print(f"  {label}  ({cell['status']}: only in {only_in} run)")
            continue
        flagged = [name for name, m in cell["metrics"].items() if m["regressed"]]
        if not flagged:
            continue
        any_flagged = True
        print(f"  REGRESSED  {label}")
        for metric_name in flagged:
            m = cell["metrics"][metric_name]
            print(f"    {metric_name}: {m['baseline']:.4g} -> {m['current']:.4g}")
    if not any_flagged:
        print("  no regressions beyond tolerance")


def _load_trained_sequence(
    config: CanaryConfig,
    override: Path | None,
    *,
    skip: bool,
) -> TrainedSequenceModel | None:
    """The configured `sequence_detector` backend's trained model, or `None`
    when disabled/unavailable/skipped — `evaluate_detectors` already handles
    `trained_sequence=None` gracefully (IF/fused only), same as no manifest
    configured in the daemon."""
    if skip:
        return None
    backend = resolve_sequence_backend(config)
    if backend is None:
        if override is not None:
            log.warning(
                "--sequence-model given but no sequence_detector backend is "
                "enabled in config; skipping sequence coverage"
            )
        return None
    missing = backend.missing_requirements()
    if missing:
        log.warning(
            "sequence backend %s missing requirements %s; skipping", backend.selector, missing
        )
        return None
    path = override if override is not None else backend.artifact_path(config)
    if not path.exists():
        log.warning("sequence-detector artifact not found at %s; skipping", path)
        return None
    return backend.load_model(path)


def _resolve_sessions(args: argparse.Namespace) -> list[Path]:
    if args.sessions:
        return [Path(p) for p in args.sessions]
    return discover_sessions(args.sessions_root)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="detector_benchmark",
        description=(
            "Grid a synthetic-fault benchmark (session x fault kind x target "
            "channel) over the recorded corpus and report per-detector x "
            "per-fault-class recall / precision / time-to-detect / clean "
            "alerts-per-hour (Phase 2B, 2.5)."
        ),
    )
    parser.add_argument(
        "sessions",
        nargs="*",
        type=Path,
        help="Session directories to benchmark. Default: all discovered under --sessions-root.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path("data/sessions"),
        help="Where to discover sessions when none are given explicitly.",
    )
    add_common_args(parser)
    parser.add_argument(
        "--bindings",
        type=Path,
        default=DEFAULT_BINDINGS_PATH,
        help="Path to bindings.yaml for CAN-signal decode (default data/bindings.yaml; "
        "skipped if absent).",
    )
    parser.add_argument(
        "--if-model",
        type=Path,
        default=None,
        help="Isolation Forest artifact path (default: <models_dir>/<isolation_forest_artifact>).",
    )
    parser.add_argument(
        "--sequence-model",
        type=Path,
        default=None,
        help="Sequence-detector artifact path override (default: the configured "
        "models.sequence_detector backend's own artifact path).",
    )
    parser.add_argument(
        "--no-sequence",
        action="store_true",
        help="Skip sequence-detector coverage even if configured/available.",
    )
    parser.add_argument(
        "--fault-kinds",
        nargs="+",
        choices=sorted(get_args(AnomalyKind)),
        default=list(DEFAULT_FAULT_KINDS),
        metavar="KIND",
    )
    parser.add_argument(
        "--channels",
        nargs="+",
        default=list(DEFAULT_TARGET_CHANNELS),
        metavar="CHANNEL",
        help="Target channels to inject faults into (skipped per-session if absent/all-NaN).",
    )
    parser.add_argument("--n-injections", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="A previous run's JSON report (Task 2.6). Diffs this run's per-detector "
        "x per-fault-class table against it and flags any metric that regressed "
        "beyond --tolerance; nonzero exit if any cell is flagged.",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        # argparse runs `help % params` for every action (for %(default)s
        # etc.), so a literal "%" here must be doubled or it's parsed as a
        # (broken) format spec.
        help=f"Relative regression tolerance for --baseline compare (default "
        f"{DEFAULT_TOLERANCE * 100:.0f}%%; see DEFAULT_TOLERANCE for the rationale).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="JSON output path (default: data/analyses/benchmark-<timestamp>.json).",
    )
    parser.add_argument(
        "--timestamp",
        default=None,
        help="Filesystem-safe timestamp for the default --out name and the report's "
        "generated_at field (default: now, UTC, YYYYMMDD-HHMMSS).",
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
    config = load_config(args)

    session_dirs = _resolve_sessions(args)
    if not session_dirs:
        print(f"no sessions found under {args.sessions_root} (or none given); nothing to do")
        # Still an empty-but-valid report, per the "no corpus yet" case.
        session_dirs = []

    if_model_path = (
        args.if_model
        if args.if_model is not None
        else config.daemon.models_dir / config.daemon.isolation_forest_artifact
    )
    try:
        trained_if = load_isolation_forest_model(if_model_path)
    except FileNotFoundError:
        print(f"error: no Isolation Forest artifact at {if_model_path}", file=sys.stderr)
        return 1

    trained_sequence = _load_trained_sequence(config, args.sequence_model, skip=args.no_sequence)

    sessions: list[tuple[str, pd.DataFrame]] = []
    for session_dir in session_dirs:
        try:
            resampled = load_resampled_session(session_dir, config, bindings_path=args.bindings)
        except FileNotFoundError as e:
            log.warning("skipping unreadable session %s: %s", session_dir, e)
            continue
        sessions.append((session_dir.name, resampled))

    cells = run_grid(
        sessions,
        config,
        trained_if,
        fault_kinds=args.fault_kinds,
        channels=args.channels,
        n_injections=args.n_injections,
        seed=args.seed,
        trained_sequence=trained_sequence,
    )

    timestamp = args.timestamp or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    report = build_report(
        cells,
        seed=args.seed,
        n_injections=args.n_injections,
        generated_at=timestamp,
    )
    print_report(report)

    regression_diff: dict[str, Any] | None = None
    if args.baseline is not None:
        try:
            baseline_report = json.loads(args.baseline.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: cannot read baseline report {args.baseline}: {e}", file=sys.stderr)
            return 2
        regression_diff = diff_reports(baseline_report, report, tolerance=args.tolerance)
        report["regressions"] = regression_diff
        print_regression_diff(regression_diff)

    out_path = args.out or Path("data/analyses") / f"benchmark-{timestamp}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out_path}")

    if regression_diff is not None and regression_diff["regressed"]:
        print("error: regression(s) detected vs baseline", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
