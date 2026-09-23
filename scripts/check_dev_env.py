"""Refuse to gate on a venv that has drifted from constraints-dev.txt.

CI installs exactly the versions in ``constraints-dev.txt``. A local gate run
on other versions can pass while CI fails (2026-09-19: pandas-stubs;
2026-09-23: mypy 2.x + numpy 2.5 stubs), so ``scripts/check.sh`` runs this
first. It compares only packages present on both sides: a constrained package
the venv lacks is not drift (CI-only extras), and a venv package the file does
not list (torch, CUDA wheels) is out of scope.

Usage: .venv/bin/python scripts/check_dev_env.py [constraints-file]
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def main(argv: list[str]) -> int:
    default = Path(__file__).resolve().parent.parent / "constraints-dev.txt"
    path = Path(argv[1]) if len(argv) > 1 else default
    if not path.exists():
        print(
            f"check_dev_env: {path} is missing; run scripts/lock_dev_constraints.sh",
            file=sys.stderr,
        )
        return 1
    wanted: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, version = line.split("==", 1)
            wanted[_norm(name)] = version.strip()
    installed = {_norm(d.metadata["Name"]): d.version for d in metadata.distributions()}
    drift = sorted(
        (name, have, wanted[name])
        for name, have in installed.items()
        if name in wanted and have != wanted[name]
    )
    if not drift:
        return 0
    print(
        f"✗ venv differs from {path.name} in {len(drift)} package(s)"
        " -- CI would run other versions:",
        file=sys.stderr,
    )
    for name, have, want in drift:
        print(f"    {name}: venv {have}, constraints {want}", file=sys.stderr)
    print(
        "  Match CI:   .venv/bin/pip install -e '.[dev]' -c constraints-dev.txt\n"
        "  Or upgrade on purpose: see scripts/lock_dev_constraints.sh",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
