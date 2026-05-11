#!/usr/bin/env python3
"""Extract CONSULT-III Data Monitor parameter names from Xterra FSM PDFs.

Runs the per-PDF page ranges below through src.analysis.fsm and writes
data/fsm/signals.json — a per-module list of named parameters that
CONSULT-III tools can display. Names are the FSM canonical names; binding
them to Mode 22 DIDs happens at runtime via tagged-capture sessions.

Usage:
  .venv/bin/python tools/extract_fsm_signals.py
  .venv/bin/python tools/extract_fsm_signals.py --validate    # dry-run, list only
  .venv/bin/python tools/extract_fsm_signals.py --fsm-dir xterra_fsm/

To extend to a new module: add a SOURCES entry. Page ranges were verified
manually against the February 2010 revision of the 2008 Xterra FSM; if you
have a different revision, expect ranges to shift by ±1.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from src.analysis.fsm import extract_pdf_section_with_specs


class FsmSource(TypedDict, total=False):
    module: str
    file: str
    pages: list[int]
    max_x: float
    platform_note: str
    tested_on_canary_truck: bool
    fsm_section: str


# Per-source page ranges. Each entry is one CONSULT-III data-monitor table
# from one FSM PDF. `tested_on_canary_truck` flags whether we've validated
# this module's signals against actual bus traffic on the user's M/T Xterra.
# `max_x` overrides the leftmost-column cutoff when a table's name column is
# wider than the default (e.g. when names carry inline unit annotations).
SOURCES: list[FsmSource] = [
    {
        "module": "ECM",
        "file": "ec.pdf",
        "pages": [431, 432, 433],
        "platform_note": "VQ40DE engine",
        "tested_on_canary_truck": True,
        "fsm_section": "EC-431, CONSULT-III Reference Value in Data Monitor Mode",
    },
    {
        "module": "ABS",
        "file": "brc.pdf",
        "pages": [83, 84, 85, 86, 87],
        "platform_note": (
            "ABS Actuator and Electric Unit (Control Unit). The BRC FSM "
            "duplicates this Data Monitor table at BRC-202 for the without-VDC "
            "variant; both lists are byte-identical, so only the BRC-83 copy "
            "is extracted here."
        ),
        "tested_on_canary_truck": False,
        "fsm_section": "BRC-83, Reference Value",
    },
    {
        "module": "TRANSFER_4WD",
        "file": "dln.pdf",
        "pages": [17, 18],
        "max_x": 200.0,  # name column carries inline [unit] annotations
        "platform_note": "ALL MODE AWD/4WD Transfer Control Unit",
        "tested_on_canary_truck": False,
        "fsm_section": "DLN-17, DATA MONITOR MODE Display Item List",
    },
    {
        "module": "DIFF_LOCK",
        "file": "dln.pdf",
        "pages": [248],
        "max_x": 200.0,
        "platform_note": (
            "Rear Differential Lock Control Unit (Off-Road / Pro-4X trim only). "
            "Skip if your Xterra doesn't have the locking rear diff option."
        ),
        "tested_on_canary_truck": False,
        "fsm_section": "DLN-248, DATA MONITOR Display Item List",
    },
    {
        "module": "BCM",
        "file": "bcs.pdf",
        "pages": [37, 38, 39],
        "platform_note": "Body Control Module",
        "tested_on_canary_truck": False,
        "fsm_section": "BCS-37, Reference Value",
    },
    {
        "module": "CLUSTER",
        "file": "mwi.pdf",
        "pages": [57, 58, 59],
        "platform_note": (
            "Combination Meter. Note: the cluster's CONSULT-III Data Monitor "
            "screen exposes the BCM signals it consumes over CAN, so this list "
            "intentionally overlaps with BCM. Useful as a cross-reference."
        ),
        "tested_on_canary_truck": False,
        "fsm_section": "MWI-57, Reference Value",
    },
    {
        "module": "IPDM_ER",
        "file": "pcs.pdf",
        "pages": [20, 21, 22],
        "platform_note": (
            "Intelligent Power Distribution Module — Engine Room. The smart "
            "fuse/relay box; sits between BCM logical requests and physical "
            "relays. Useful for body anomaly detection (load-vs-request "
            "comparison)."
        ),
        "tested_on_canary_truck": False,
        "fsm_section": "PCS-20, Reference Value (IPDM E/R)",
    },
    {
        "module": "TCM_5AT",
        "file": "tm.pdf",
        "pages": [106, 107, 108, 109],
        "max_x": 200.0,  # names extend past the default cutoff (carry inline units)
        "platform_note": (
            "5AT: RE5R05A automatic transmission. NOT present on M/T builds. "
            "Included for users with the AT variant; not validated on this build."
        ),
        "tested_on_canary_truck": False,
        "fsm_section": "TM-106, Monitor Item Selection (CONSULT-III)",
    },
]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="extract_fsm_signals",
        description="Extract CONSULT-III parameter names from Xterra FSM PDFs.",
    )
    parser.add_argument(
        "--fsm-dir",
        type=Path,
        default=Path("xterra_fsm"),
        help="Directory containing the FSM PDFs (default: ./xterra_fsm).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/fsm/signals.json"),
        help="Output path for the signals JSON.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Print extraction summary without writing the JSON file.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    fsm_dir: Path = args.fsm_dir
    if not fsm_dir.is_dir():
        print(f"error: FSM directory not found: {fsm_dir}", file=sys.stderr)
        return 1

    modules: list[dict[str, Any]] = []
    total_params = 0
    print(f"extracting from {fsm_dir}/ ...")
    for src in SOURCES:
        path = fsm_dir / src["file"]
        if not path.exists():
            print(f"  SKIP {src['module']:14s} — missing {path}")
            continue
        max_x = src.get("max_x", 135.0)
        params = extract_pdf_section_with_specs(path, src["pages"], max_x=max_x)
        total_params += len(params)
        with_ranges = sum(1 for p in params if p["expected_ranges"])
        page_range = f"pp.{src['pages'][0]}-{src['pages'][-1]}"
        print(
            f"  {src['module']:14s}  {len(params):4d} params  "
            f"({with_ranges:3d} w/ numeric ranges)  ({src['file']} {page_range})"
        )
        modules.append(
            {
                "module": src["module"],
                "source_file": src["file"],
                "fsm_section": src["fsm_section"],
                "pages": src["pages"],
                "platform_note": src["platform_note"],
                "tested_on_canary_truck": src["tested_on_canary_truck"],
                "param_count": len(params),
                "params": params,
            }
        )

    output: dict[str, Any] = {
        "vehicle": "2008 Nissan Xterra",
        "fsm_revision": "February 2010",
        "extracted_at": datetime.now(UTC).isoformat(),
        "module_count": len(modules),
        "param_count": total_params,
        "modules": modules,
    }

    if args.validate:
        print(f"\n[validate] {total_params} params across {len(modules)} modules.")
        print("[validate] no file written.")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2))
    print(f"\nwrote {args.out} ({total_params} params, {len(modules)} modules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
