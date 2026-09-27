#!/usr/bin/env python3
"""Job entry point for the ``replay_window`` job kind.

CLI shim around :func:`src.analysis.replay_window.replay_window`. Reads the
session, runs IF + LSTM scoring over the optional time window, looks up
historical anomaly events, and writes a structured JSON report to ``--out``.

Invoked by :class:`src.control.jobs.JobController` when an MCP client (or
operator) submits a ``replay_window`` job. The job result is the JSON file
at ``--out``; the MCP tool reads it when the job reaches a terminal state.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from src.analysis.replay_window import replay_window
from src.config import CanaryConfig
from src.storage.sessions import resolve_session


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="replay_window",
        description=(
            "Replay a session window through the IF + LSTM inference pipeline "
            "and diff against historically-emitted anomaly events."
        ),
    )
    parser.add_argument(
        "--session-id",
        required=True,
        help="Session directory name under collection.storage_path.",
    )
    parser.add_argument("--start", default=None, help="ISO-8601 window start (inclusive).")
    parser.add_argument("--end", default=None, help="ISO-8601 window end (inclusive).")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("canary.yaml"),
        help="Path to canary.yaml.",
    )
    parser.add_argument(
        "--if-model",
        type=Path,
        default=None,
        help="Isolation Forest artifact (default: <models_dir>/isolation_forest/model.joblib).",
    )
    parser.add_argument(
        "--lstm-model",
        type=Path,
        default=None,
        help="LSTM autoencoder artifact (default: <models_dir>/lstm_autoencoder).",
    )
    parser.add_argument(
        "--anomalies-dir",
        type=Path,
        default=None,
        help="Anomaly JSONL directory (default: from canary.yaml alerts.anomalies_dir).",
    )
    parser.add_argument("--max-rows", type=int, default=5_000)
    parser.add_argument("--max-windows", type=int, default=5_000)
    parser.add_argument("--out", type=Path, required=True, help="Output path for the JSON report.")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def _default_if_path(config: CanaryConfig) -> Path:
    return config.daemon.models_dir / "isolation_forest" / "model.joblib"


def _default_lstm_path(config: CanaryConfig) -> Path:
    return config.daemon.models_dir / "lstm_autoencoder"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = CanaryConfig.from_yaml(args.config)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"error loading config: {exc}", file=sys.stderr)
        return 1

    try:
        session_dir = resolve_session(config.collection.storage_path, args.session_id)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    start = _parse_iso(args.start) if args.start else None
    end = _parse_iso(args.end) if args.end else None
    if_model = args.if_model or _default_if_path(config)
    lstm_model = args.lstm_model or _default_lstm_path(config)
    anomalies_dir = args.anomalies_dir or config.alerts.anomalies_dir

    report = replay_window(
        session_dir=session_dir,
        config=config,
        start=start,
        end=end,
        if_model_path=if_model,
        lstm_model_path=lstm_model,
        anomalies_dir=anomalies_dir,
        max_rows=args.max_rows,
        max_windows=args.max_windows,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    indent = 2 if args.pretty else None
    args.out.write_text(json.dumps(report.to_dict(), indent=indent, sort_keys=True, default=str))
    print(f"wrote replay report → {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry
    sys.exit(main())
