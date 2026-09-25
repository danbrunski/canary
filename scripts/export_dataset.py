#!/usr/bin/env python3
"""Export a traceable training dataset from recorded sessions (Task 6.20).

    .venv/bin/python scripts/export_dataset.py \\
        --session 2026-05-11T222447 --session 2026-05-13T004818 \\
        --format parquet --format feature_matrix \\
        --resample-hz 1 --purpose "lstm baseline" --out data/datasets

    .venv/bin/python scripts/export_dataset.py --mark-candidate 2026-05-11T222447 \\
        --purpose "clean highway baseline"

Every export carries a ``dataset_manifest.json`` (vehicle, config hash,
schema, sessions, tags, time range, quality warnings) so a copy taken off
the Pi stays reproducible and honest (Risk R12).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

try:
    from _cli import (
        add_common_args,
        add_log_level_arg,
        load_config,
    )
except ImportError:  # loaded as `scripts.export_dataset` — repo root on path, not scripts/
    from scripts._cli import (  # type: ignore[no-redef]
        add_common_args,
        add_log_level_arg,
        load_config,
    )

from src.datasets.export import (  # noqa: E402
    DatasetExportSpec,
    export_dataset,
    mark_training_candidate,
)
from src.log import configure_logging  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="export_dataset", description=__doc__)
    add_common_args(p)
    p.add_argument(
        "--sessions-root",
        type=Path,
        default=None,
        help="Override the session storage root (default: from config).",
    )
    p.add_argument("--session", dest="sessions", action="append", default=[], help="Session id.")
    p.add_argument("--channel", dest="channels", action="append", default=None)
    p.add_argument("--resample-hz", type=float, default=None)
    p.add_argument("--include-tag", dest="include_tags", action="append", default=None)
    p.add_argument("--exclude-tag", dest="exclude_tags", action="append", default=None)
    p.add_argument(
        "--format",
        dest="formats",
        action="append",
        default=None,
        choices=["csv", "jsonl", "parquet", "feature_matrix"],
    )
    p.add_argument("--purpose", default="")
    p.add_argument("--no-zip", action="store_true")
    p.add_argument("--out", type=Path, default=Path("data/datasets"))
    p.add_argument("--name", default=None)
    p.add_argument(
        "--mark-candidate",
        default=None,
        help="Flag a session as a training candidate and exit (no export).",
    )
    add_log_level_arg(p)
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = load_config(args)
    storage_root = args.sessions_root or config.collection.storage_path

    if args.mark_candidate is not None:
        meta = mark_training_candidate(storage_root / args.mark_candidate, purpose=args.purpose)
        print(f"marked {args.mark_candidate} training_candidate={meta['training_candidate']}")
        return 0

    if not args.sessions:
        print("error: at least one --session is required", file=sys.stderr)
        return 1

    spec = DatasetExportSpec(
        sessions=args.sessions,
        channels=args.channels,
        resample_hz=args.resample_hz,
        include_tags=args.include_tags,
        exclude_tags=args.exclude_tags,
        formats=args.formats or ["parquet"],
        purpose=args.purpose,
        zip=not args.no_zip,
    )
    result = export_dataset(spec, storage_root, config, args.out, name=args.name)
    print("=" * 72)
    print(f"DATASET EXPORT  →  {result.out_dir}")
    print("=" * 72)
    print(f"  rows:     {result.manifest.row_count}")
    print(f"  channels: {len(result.manifest.channel_schema)}")
    print(f"  sessions: {len(result.manifest.sessions)}")
    print(f"  files:    {', '.join(f.name for f in result.files)}")
    if result.zip_path is not None:
        print(f"  zip:      {result.zip_path}")
    if result.manifest.quality_warnings:
        print("  data-quality warnings:")
        for w in result.manifest.quality_warnings:
            print(f"    ⚠ {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
