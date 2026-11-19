#!/usr/bin/env python3
"""Does an ECU image hold the whole flash? Read each file's own pointers.

Offline only: reads image files and a backup recipe, prints what each image's
vector tables and call targets say about its size
(:mod:`src.analysis.rom_extent`), and writes nothing. Nothing here talks to the
ECU. Plain images (an ECU read, a library download) and ``.OsirisROM`` files
(decoded first) are both accepted.

Usage::

    .venv/bin/python -m tools.rom_extent --config canary.yaml --profile <slug> IMAGE [IMAGE ...]
    .venv/bin/python -m tools.rom_extent --recipe data/<slug>/rom_backup.yaml IMAGE
    .venv/bin/python -m tools.rom_extent --config canary.yaml --json IMAGE

The recipe's ``extent`` section names the vector tables to read. ``--recipe``
names a file; otherwise ``--profile`` (or ``--config``'s profile) finds the
vehicle's ``rom_backup.yaml``, its own or its generation's platform one
(``data/platforms/<id>/``, chosen by the config's make, model and year). There
is no default vehicle. Exit status 0 when every file was read (whatever
its verdict), 1 when a file could not be read, 2 when the recipe has no
``extent``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from src.analysis.osiris import SIGNATURE, Container
from src.analysis.rom_extent import ExtentSpec, read_extent
from src.control.rom_backup import load_backup_recipe

try:
    from _cli import (  # type: ignore[import-not-found]
        ProfileDataError,
        add_profile_dir_args,
        load_profile_data,
    )
except ImportError:  # loaded as `tools.<name>`: repo root on path
    from tools._cli import ProfileDataError, add_profile_dir_args, load_profile_data

RECIPE_FILE = "rom_backup.yaml"


def plain_image(data: bytes) -> bytes:
    """The plain image: an ``.OsirisROM`` decoded, anything else as it is."""
    return Container.parse(data).decoded() if data.startswith(SIGNATURE) else data


def read_file(path: Path, spec: ExtentSpec) -> dict[str, Any]:
    image = plain_image(path.read_bytes())
    reading = read_extent(image, spec)
    return {
        "file": str(path),
        "sha256": hashlib.sha256(image).hexdigest(),
        "size": len(image),
        **reading.to_dict(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("images", nargs="+", type=Path, metavar="IMAGE")
    add_profile_dir_args(parser, sessions_flag=None)
    parser.add_argument(
        "--recipe",
        type=Path,
        default=None,
        help="a rom_backup.yaml with an extent section (else the --profile vehicle's, "
        "found through its platform)",
    )
    parser.add_argument("--json", action="store_true", help="one JSON object per file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    recipe = args.recipe
    if recipe is None:
        try:
            recipe = load_profile_data(args, require_directory=False).file(RECIPE_FILE)
        except ProfileDataError as exc:
            raise SystemExit(f"refused: {exc}") from exc
    spec = load_backup_recipe(recipe, None).extent
    if spec is None:
        print(f"{recipe} has no extent section: nothing names a vector table to read")
        return 2
    status = 0
    for path in args.images:
        try:
            result = read_file(path, spec)
        except (OSError, ValueError) as exc:
            print(f"{path}: could not read: {exc}", file=sys.stderr)
            status = 1
            continue
        if args.json:
            print(json.dumps(result))
            continue
        print(f"{path}")
        print(f"  sha256  {result['sha256']}")
        print(f"  size    {result['size']:,} bytes")
        print(f"  verdict {result['verdict']}: {result['summary']}")
    return status


if __name__ == "__main__":
    sys.exit(main())
