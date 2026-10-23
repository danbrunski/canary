#!/usr/bin/env python3
"""Does an ECU image hold the whole flash? Read each file's own pointers.

Offline only: reads image files and a backup recipe, prints what each image's
vector tables and call targets say about its size
(:mod:`src.analysis.rom_extent`), and writes nothing. Nothing here talks to the
ECU. Plain images (an ECU read, a library download) and ``.OsirisROM`` files
(decoded first) are both accepted.

Usage::

    .venv/bin/python -m tools.rom_extent IMAGE [IMAGE ...]
    .venv/bin/python -m tools.rom_extent --recipe data/<slug>/rom_backup.yaml IMAGE
    .venv/bin/python -m tools.rom_extent --json IMAGE

The recipe's ``extent`` section names the vector tables to read; the default is
the N50 Xterra platform's. Exit status 0 when every file was read (whatever
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
from src.paths import PLATFORMS_DIR

DEFAULT_RECIPE = PLATFORMS_DIR / "nissan-xterra-n50" / "rom_backup.yaml"


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
    parser.add_argument(
        "--recipe",
        type=Path,
        default=DEFAULT_RECIPE,
        help="a rom_backup.yaml with an extent section (default: the N50 Xterra platform's)",
    )
    parser.add_argument("--json", action="store_true", help="one JSON object per file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    spec = load_backup_recipe(args.recipe, None).extent
    if spec is None:
        print(f"{args.recipe} has no extent section: nothing names a vector table to read")
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
