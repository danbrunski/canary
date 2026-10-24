#!/usr/bin/env python3
"""Build or refresh the derived session frames on the laptop (P01.2).

Every offline consumer (retrain's trainers, ``evaluate_detectors``) reads a
session's 1 Hz frame through the derived-frame store and builds it lazily on
first use; this command does it up front after a pull, so the first training
or evaluation run does not pay for it::

    .venv/bin/python scripts/derive_sessions.py --config canary.example.yaml \\
        --sessions-root ~/canary-data/sessions/nissan-xterra-2008 \\
        --sessions-root ~/canary-data/sessions-0920 --since 2026-08-03

A fresh frame is left alone, a frame whose decode catalogs changed has only
the affected channels re-derived, and a frame whose chunk files, grid,
gate limits or code version changed is rebuilt. Each session prints which of
those happened and why. Never run on the Pi: the store refuses there.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

try:
    from _cli import (  # type: ignore[import-not-found]
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        load_config,
    )
except ImportError:  # loaded as `scripts.derive_sessions`: repo root on path
    from scripts._cli import (
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        load_config,
    )

from src.features.session_resampling import default_decode_inputs
from src.log import configure_logging
from src.preprocessing.derived_store import DEFAULT_DERIVED_DIR, DerivedStore
from src.preprocessing.resampler import resample_config_for
from src.storage.sessions import recorded_sessions, session_started_at


def _utc_date(text: str) -> datetime:
    stamp = datetime.fromisoformat(text)
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_common_args(parser)
    add_bindings_arg(parser)
    add_log_level_arg(parser, default="WARNING")
    parser.add_argument(
        "sessions",
        nargs="*",
        type=Path,
        help="Session directories (in addition to any found under --sessions-root).",
    )
    parser.add_argument("--sessions-root", type=Path, action="append", default=[])
    parser.add_argument(
        "--since", type=_utc_date, default=None, help="Only sessions starting at/after this."
    )
    parser.add_argument("--derived-dir", type=Path, default=DEFAULT_DERIVED_DIR)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report each session's state without building anything.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = load_config(args)
    resample = resample_config_for(config)
    decoders = default_decode_inputs(args.bindings)
    store = DerivedStore(args.derived_dir)

    sessions = [*args.sessions, *recorded_sessions(args.sessions_root)]
    if args.since is not None:
        sessions = [
            s for s in sessions if (started := session_started_at(s)) and started >= args.since
        ]
    if not sessions:
        print("no sessions to derive", file=sys.stderr)
        return 1

    failed = 0
    started_all = time.monotonic()
    for session in sessions:
        if args.check:
            reason = store.status(session, decoders, resample)
            print(f"{session.name}: {'fresh' if reason is None else reason}")
            continue
        try:
            _, report = store.load(session, decoders, resample)
        except Exception as exc:  # noqa: BLE001 - one bad session is named, not fatal
            failed += 1
            print(f"{session.name}: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        detail = f" ({', '.join(report.rederived_units)})" if report.rederived_units else ""
        print(
            f"{session.name}: {report.status} in {report.seconds:.1f}s -- {report.reason}{detail}"
        )
    if not args.check:
        print(
            f"{len(sessions) - failed} of {len(sessions)} sessions derived in "
            f"{time.monotonic() - started_all:.0f}s under {args.derived_dir}"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
