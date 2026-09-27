#!/usr/bin/env python3
"""Rebuild / inspect the session catalog (Task 6.3, eval-longitudinal-and-dtc.md).

The catalog (``src/storage/catalog.py``) is a rebuildable SQLite index over
session sidecars — normally kept current incrementally, wherever a sidecar is
written (classification, fingerprint, dtc, digest, pin/unpin) or a session is
deleted (retention). This CLI covers the two things that hook can't:

* ``rebuild`` — re-index every session directory on disk from its sidecars.
  This is the corruption-recovery story: delete/move aside a broken
  ``catalog.db`` and rebuild — sidecars are the source of truth, never the
  catalog itself.
* ``status`` — row count, oldest/newest session, disposition breakdown.

    .venv/bin/python scripts/catalog.py rebuild
    .venv/bin/python scripts/catalog.py status
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

try:
    from _cli import (
        add_common_args,
        add_log_level_arg,
        load_config,
    )
except ImportError:  # loaded as `scripts.catalog` — repo root on path, not scripts/
    from scripts._cli import (  # type: ignore[no-redef]
        add_common_args,
        add_log_level_arg,
        load_config,
    )

from src.config import CanaryConfig  # noqa: E402
from src.log import configure_logging  # noqa: E402
from src.storage.catalog import SessionCatalog, rebuild_catalog  # noqa: E402
from src.storage.session_files import has_telemetry  # noqa: E402
from src.vehicle.profiles import resolve_profile_for_tooling, scope_config  # noqa: E402

log = logging.getLogger(__name__)

#: Everything the catalog considers a "session" to index: normal live-session
#: dirs, underscore-prefixed sibling trees at the top level (``_replay-*``,
#: ``_tuning-*``), and one level into ``_archive/`` for quarantined sessions
#: nested inside it. Anything else (no telemetry parquet at all — ``scratch``,
#: stray files) is skipped. Deliberately broader than
#: ``src.preprocessing.loader.discover_sessions`` — that function excludes
#: underscore-prefixed dirs for training; the catalog indexes everything on
#: disk so consumers with an explicit-name escape hatch (``parquet_query``)
#: can still resolve them.
_ARCHIVE_DIRNAME = "_archive"


def _discover_catalog_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    dirs: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name == _ARCHIVE_DIRNAME:
            dirs.extend(
                sub for sub in sorted(child.iterdir()) if sub.is_dir() and has_telemetry(sub)
            )
            continue
        if has_telemetry(child):
            dirs.append(child)
    return dirs


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="catalog",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common_args(parser)
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Vehicle profile slug to scope paths to. Default: the vehicle: block's profile.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=None,
        help="Storage root to scan. Defaults to (profile-scoped) collection.storage_path.",
    )
    parser.add_argument(
        "--catalog-db",
        type=Path,
        default=None,
        help="Override catalog DB path. Defaults to (profile-scoped) daemon.catalog_db.",
    )
    add_log_level_arg(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("rebuild", help="Re-index every session directory on disk from its sidecars.")
    sub.add_parser("status", help="Print row count, session span, disposition breakdown.")
    return parser.parse_args(argv)


def _scoped_config(args: argparse.Namespace) -> CanaryConfig:
    config = load_config(args)
    if not config.profiles.enabled:
        return config
    return scope_config(config, resolve_profile_for_tooling(config, args.profile))


def _run_rebuild(sessions_root: Path, catalog: SessionCatalog) -> int:
    session_dirs = _discover_catalog_dirs(sessions_root)
    if not session_dirs:
        log.warning("no session directories with telemetry found under %s", sessions_root)
        return 0
    count = rebuild_catalog(catalog, session_dirs)
    log.info(
        "rebuilt catalog: indexed %d/%d session(s) -> %s", count, len(session_dirs), catalog.db_path
    )
    return 0 if count == len(session_dirs) else 2


def _run_status(catalog: SessionCatalog) -> int:
    rows = catalog.list_sessions()
    log.info("catalog=%s sessions=%d", catalog.db_path, len(rows))
    if not rows:
        return 0
    log.info("oldest=%s newest=%s", rows[0].session_id, rows[-1].session_id)
    by_disposition: dict[str, int] = {}
    for row in rows:
        key = row.disposition or "unclassified"
        by_disposition[key] = by_disposition.get(key, 0) + 1
    for disposition, count in sorted(by_disposition.items()):
        log.info("  %-14s %d", disposition, count)
    pinned = sum(1 for row in rows if row.pinned)
    log.info("pinned=%d", pinned)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.log_level)

    config = _scoped_config(args)
    sessions_root = args.sessions_root or config.collection.storage_path
    catalog_db = args.catalog_db or config.daemon.catalog_db

    with SessionCatalog(catalog_db) as catalog:
        if args.command == "rebuild":
            return _run_rebuild(sessions_root, catalog)
        if args.command == "status":
            return _run_status(catalog)
    return 1


if __name__ == "__main__":
    sys.exit(main())
