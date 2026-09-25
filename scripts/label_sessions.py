#!/usr/bin/env python3
"""Run the post-hoc session classifier (modes + events + disposition).

Sister to ``scripts/classify_sessions.py``: that one quarantines
schema-broken / CAN-only sessions to ``_archive/``. This one runs the
mode segmenter + event-detector library over each well-formed live
session and writes a ``classification.json`` sidecar — the input
``refit_behavior_models.py`` reads to filter training data by mode +
event presence. It also computes the session's ``fingerprint.json``
(Task 4.1/4.2, eval-longitudinal-and-dtc.md) right after classification —
one post-session pass rather than a second corpus walk. Sessions that
already have a fingerprint but predate this hook are backfilled via
``scripts/fingerprint.py backfill``, not by this script.

Idempotent: a session whose sidecar already matches the current
``CLASSIFIER_VERSION`` is skipped unless ``--rebuild`` forces it.

Usage:

    .venv/bin/python scripts/label_sessions.py            # stale only
    .venv/bin/python scripts/label_sessions.py --session 2026-05-20T160000
    .venv/bin/python scripts/label_sessions.py --all --rebuild
    .venv/bin/python scripts/label_sessions.py --dry-run

One log line per session + an aggregate disposition tally at the end.
Per-session errors are logged but never fail the whole batch — the
operator wants a useful report even when one session is corrupt.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from _cli import add_common_args, discover_live_session_dirs, load_config

from src.analysis.fingerprint import compute_fingerprint, write_fingerprint
from src.analysis.session_classifier import (
    CLASSIFIER_VERSION,
    SessionClassification,
    classify_session,
    is_stale,
    read_classification,
    write_classification,
)
from src.control.trends import evaluate_and_alert_trends
from src.log import configure_logging
from src.storage.catalog import record_session_best_effort

log = logging.getLogger(__name__)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="label_sessions",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        help="Storage root to scan. Defaults to ``collection.storage_path`` from --config.",
    )
    parser.add_argument(
        "--session",
        action="append",
        default=[],
        help="One session id (e.g. ``2026-05-20T160000``). Repeatable. Overrides scanning.",
    )
    add_common_args(parser)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Label every session under --sessions-root (subject to --rebuild).",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force re-labeling even when classifier_version matches.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the classification but don't write the sidecar.",
    )
    parser.add_argument(
        "--catalog-db",
        type=Path,
        default=None,
        help=(
            "Also re-index each labeled session into the session catalog "
            "(Task 6.3) at this DB path. Omit to skip the catalog entirely "
            "and run ``scripts/catalog.py rebuild`` afterward instead."
        ),
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    return parser.parse_args(argv)


def _should_label(session_dir: Path, rebuild: bool) -> tuple[bool, SessionClassification | None]:
    existing = read_classification(session_dir)
    if rebuild:
        return True, existing
    return is_stale(existing), existing


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.log_level)

    config = load_config(args)
    sessions_root = args.sessions_root or config.collection.storage_path

    if args.session:
        targets = [sessions_root / name for name in args.session]
        missing = [path.name for path in targets if not path.is_dir()]
        if missing:
            log.error("session(s) not found under %s: %s", sessions_root, ", ".join(missing))
            return 1
    else:
        if not args.all:
            log.info(
                "no --session or --all given; labeling only sessions with stale sidecars "
                "(use --all to force scan)"
            )
        targets = discover_live_session_dirs(sessions_root)

    if not targets:
        log.warning("no sessions found at %s", sessions_root)
        return 0

    summary: dict[str, int] = {"ready": 0, "review": 0, "quarantine": 0, "skipped": 0, "error": 0}
    for session_dir in targets:
        should, existing = _should_label(session_dir, args.rebuild)
        if not should:
            log.info(
                "skip %s (already at classifier_version=%s)",
                session_dir.name,
                existing.classifier_version if existing else "?",
            )
            summary["skipped"] += 1
            continue
        try:
            classification = classify_session(session_dir, config)
        except FileNotFoundError as exc:
            log.warning("skip %s: %s", session_dir.name, exc)
            summary["error"] += 1
            continue
        except Exception:  # noqa: BLE001 — keep the batch moving
            log.exception("classify failed for %s", session_dir.name)
            summary["error"] += 1
            continue
        if not args.dry_run:
            write_classification(session_dir, classification)
            try:
                fingerprint_stats = compute_fingerprint(session_dir, config)
                write_fingerprint(session_dir, fingerprint_stats)
            except Exception:  # noqa: BLE001 — a fingerprint failure shouldn't block classification
                log.exception("fingerprint failed for %s", session_dir.name)
            else:
                # Trend evaluation + alerting (Task 4.4/4.6) rides the same
                # post-session pass, right after the fingerprint that feeds it.
                try:
                    evaluate_and_alert_trends(config, session_dir.name, sessions_root=sessions_root)
                except Exception:  # noqa: BLE001 — trend alerting shouldn't block the batch
                    log.exception("trend evaluation failed for %s", session_dir.name)
            record_session_best_effort(args.catalog_db, session_dir)
        summary[classification.disposition] += 1
        log.info(
            "%s: %s  duration=%ss  modes=%s  events=%s",
            session_dir.name,
            classification.disposition,
            int(classification.duration_seconds),
            sorted(classification.mode_seconds.keys()),
            sum(classification.event_counts.values()),
        )

    log.info(
        "done classifier_version=%s  ready=%d review=%d quarantine=%d skipped=%d error=%d",
        CLASSIFIER_VERSION,
        summary["ready"],
        summary["review"],
        summary["quarantine"],
        summary["skipped"],
        summary["error"],
    )
    return 0 if summary["error"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
