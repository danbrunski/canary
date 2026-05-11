#!/usr/bin/env python3
"""CLI for decoding ISO-TP / UDS traffic in a recorded session."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from src.analysis.diagnostic_report import (
    DecodedDiagnosticSession,
    load_decoded_diagnostic_session,
    report,
)
from src.analysis.diagnostics import DEFAULT_DIAG_IDS, parse_diag_ids
from src.analysis.signal_lookup import SignalCatalog, load_catalog

__all__ = [
    "DEFAULT_DIAG_IDS",
    "DecodedDiagnosticSession",
    "SignalCatalog",
    "load_catalog",
    "load_decoded_diagnostic_session",
    "main",
    "parse_args",
    "parse_diag_ids",
    "report",
]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="decode_isotp",
        description="Decode ISO-TP/UDS traffic in a recorded session, grouped by tag window.",
    )
    parser.add_argument("session_dir", type=Path, help="Path to data/sessions/<timestamp>/")
    parser.add_argument(
        "--diag-ids",
        default=None,
        help="Comma-separated hex IDs to treat as diagnostic (default: 7DF,7E0..7EF).",
    )
    parser.add_argument(
        "--signals",
        type=Path,
        default=Path("data/fsm/signals.json"),
        help=(
            "Path to FSM-extracted signal catalog (default: data/fsm/signals.json). "
            "Pass --no-signals to skip name suggestions entirely."
        ),
    )
    parser.add_argument(
        "--no-signals",
        action="store_true",
        help="Skip loading the FSM signal catalog (no name suggestions).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    diag_ids = parse_diag_ids(args.diag_ids) if args.diag_ids else set(DEFAULT_DIAG_IDS)

    catalog: SignalCatalog | None = None
    if not args.no_signals and args.signals.exists():
        catalog = load_catalog(args.signals)
    return report(args.session_dir, diag_ids, catalog)


if __name__ == "__main__":
    sys.exit(main())
