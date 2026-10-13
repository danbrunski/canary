#!/usr/bin/env python3
"""Which CANary-HAT revision this Pi carries, and how that revision is wired.

The facts are ``hat_revisions.json`` beside this file; the daemon reads the same
table (``src/collection/hat_revision.py``). Standard library only, so it runs
with the Pi's system python3, from the checkout or from a copy of this
directory:

    python3 hat_rev.py                  # the fitted revision's name, e.g. 2.1
    python3 hat_rev.py gnss_tty         # one fact: /dev/ttyAMA4
    python3 hat_rev.py --shell          # HAT_REV=2.1 HAT_GNSS_TTY=... for eval
    python3 hat_rev.py --rev 2.0 ...    # a named revision, whatever is fitted

The firmware publishes the ID EEPROM under ``/proc/device-tree/hat``
(``CANARY_HAT_DIR`` overrides it; ``CANARY_HAT_REV`` names a revision outright).
A Pi with no CANary-HAT EEPROM gets the table's ``unidentified`` revision,
rev 2.0. Exit 2: an unknown revision name, or a CANary-HAT EEPROM reporting a
product_ver the table does not list (a checkout older than the board).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TABLE = HERE / "hat_revisions.json"
PRODUCT_PREFIX = "CANary-HAT"


class HatRevisionError(Exception):
    """No such revision, or an EEPROM naming one this table does not know."""


def load_table(path: Path = TABLE) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        table: dict[str, Any] = json.load(f)
    return table


def by_name(name: str, table: dict[str, Any] | None = None) -> dict[str, Any]:
    table = table or load_table()
    for rev in table["revisions"]:
        if rev["name"] == name:
            return dict(rev)
    known = ", ".join(r["name"] for r in table["revisions"])
    raise HatRevisionError(f"no CANary-HAT revision {name!r} (known: {known})")


def by_product_ver(product_ver: str, table: dict[str, Any] | None = None) -> dict[str, Any]:
    table = table or load_table()
    try:
        want = int(product_ver, 16)
    except ValueError:
        raise HatRevisionError(f"unreadable product_ver {product_ver!r}") from None
    for rev in table["revisions"]:
        if int(rev["product_ver"], 16) == want:
            return dict(rev)
    raise HatRevisionError(
        f"the CANary-HAT EEPROM reports product_ver {product_ver}, which "
        f"{TABLE.name} does not list: update the checkout, or name the board "
        "with --rev / CANARY_HAT_REV"
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").replace("\0", "").strip()
    except OSError:
        return ""


def detect(
    rev: str | None = None,
    *,
    hat_dir: Path | None = None,
    table: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """``(revision, how)``; ``how`` is ``named``, ``eeprom`` or ``unidentified``."""
    table = table or load_table()
    rev = rev or os.environ.get("CANARY_HAT_REV") or None
    if rev:
        return by_name(rev, table), "named"
    hat_dir = hat_dir or Path(os.environ.get("CANARY_HAT_DIR", "/proc/device-tree/hat"))
    product = _read(hat_dir / "product")
    product_ver = _read(hat_dir / "product_ver")
    if product.startswith(PRODUCT_PREFIX) and product_ver:
        return by_product_ver(product_ver, table), "eeprom"
    return by_name(table["unidentified"], table), "unidentified"


def shell_lines(rev: dict[str, Any], how: str) -> list[str]:
    """``HAT_<FIELD>=value`` lines for a shell ``eval``: lists comma-joined,
    booleans 1/0, the revision name as ``HAT_REV``."""
    lines = [f"HAT_REV={shlex.quote(rev['name'])}", f"HAT_HOW={shlex.quote(how)}"]
    for key, value in rev.items():
        if key == "name":
            continue
        if isinstance(value, bool):
            text = "1" if value else "0"
        elif isinstance(value, list):
            text = ",".join(str(v) for v in value)
        else:
            text = str(value)
        lines.append(f"HAT_{key.upper()}={shlex.quote(text)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("field", nargs="?", help="one fact to print (default: the revision name)")
    parser.add_argument("--rev", help="a revision by name (2.0, 2.1), ignoring the EEPROM")
    parser.add_argument(
        "--product-ver",
        help="a revision by EEPROM product_ver (0x0201); empty means no EEPROM said",
    )
    parser.add_argument("--shell", action="store_true", help="every fact as HAT_*= lines")
    args = parser.parse_args(argv)
    try:
        if args.product_ver is not None and args.product_ver.strip():
            rev, how = by_product_ver(args.product_ver.strip()), "eeprom"
        elif args.product_ver is not None:
            rev, how = by_name(load_table()["unidentified"]), "unidentified"
        else:
            rev, how = detect(args.rev)
    except HatRevisionError as e:
        print(f"hat_rev: {e}", file=sys.stderr)
        return 2
    if args.shell:
        print("\n".join(shell_lines(rev, how)))
    elif args.field:
        if args.field not in rev:
            print(f"hat_rev: no field {args.field!r} (have: {', '.join(rev)})", file=sys.stderr)
            return 2
        value = rev[args.field]
        print(",".join(map(str, value)) if isinstance(value, list) else value)
    else:
        print(rev["name"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
