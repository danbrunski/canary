#!/usr/bin/env python3
"""Sustained end-to-end validation on the synthetic bus (roadmap task C10).

Runs the scenario table in ``src/validation/scenarios.py`` through the real
collector, fanout, writer and inference loop, writes one versioned JSON
report per scenario, and can diff two report directories (a *before* tree and
an *after* tree) into markdown.

Usage::

    # after (this tree)
    .venv/bin/python scripts/sustained_validation.py run --label after \\
        --out data/analyses/sustained-validation/after --sessions-root data/_validation

    # before: run the same command from a read-only worktree of the pre-change
    # commit with this script and src/validation/ copied in (see
    # docs/sustained-validation.md), pointing --out/--sessions-root at absolute
    # paths in the main tree.

    .venv/bin/python scripts/sustained_validation.py summarize \\
        --before data/analyses/sustained-validation/before \\
        --after data/analyses/sustained-validation/after

Raw sessions land under ``--sessions-root`` (``_``-prefixed by default so the
training corpus never discovers them); nothing under it is committed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

try:
    from _cli import _REPO_ROOT
except ImportError:
    from scripts._cli import _REPO_ROOT  # type: ignore[no-redef]

from src.validation.harness import harness_config, run_scenario_clean
from src.validation.report import build_report, summarize_markdown, write_report
from src.validation.scenarios import SCENARIOS, scenario
from src.validation.sweep import run_budget_sweep, summarize_sweep

log = logging.getLogger("sustained_validation")

DEFAULT_OUT = Path("data/analyses/sustained-validation")
DEFAULT_SESSIONS_ROOT = Path("data/_validation")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one or every scenario and write JSON reports")
    run.add_argument("--label", required=True, help="before | after (or any tag)")
    run.add_argument("--out", type=Path, default=None, help="report directory")
    run.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS_ROOT)
    run.add_argument("--scratch", type=Path, default=None, help="control/state scratch dir")
    run.add_argument(
        "--scenario",
        action="append",
        default=None,
        help="scenario name (repeatable); default: all",
    )
    run.add_argument("--no-inference", action="store_true", help="skip the live inference loop")
    run.add_argument(
        "--models-dir",
        type=Path,
        default=None,
        help=(
            "detector artifact root (profile-scoped like the daemon). Only manifests are "
            "committed under models/, so a workstation run installs the declared IF stub "
            "unless this points at real artifacts; the Pi needs no override."
        ),
    )

    sweep = sub.add_parser(
        "sweep",
        help="walk candidate bus budgets under one fixed workload, stopping at the first "
        "failed contract (C10.12)",
    )
    sweep.add_argument("--label", required=True, help="run tag")
    sweep.add_argument("--scenario", default="fuel-map", help="scenario to hold fixed")
    sweep.add_argument(
        "--budgets",
        required=True,
        help="comma-separated candidate budgets in req/s, walked in the order given",
    )
    sweep.add_argument("--out", type=Path, default=None, help="write JSON + markdown here")
    sweep.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS_ROOT)
    sweep.add_argument("--scratch", type=Path, default=None)
    sweep.add_argument("--no-inference", action="store_true")

    summarize = sub.add_parser("summarize", help="diff two report directories into markdown")
    summarize.add_argument("--before", type=Path, required=True)
    summarize.add_argument("--after", type=Path, required=True)
    summarize.add_argument("--out", type=Path, default=None, help="write markdown here")

    sub.add_parser("list", help="list scenario names")
    return parser.parse_args(argv)


async def _run_all(args: argparse.Namespace) -> int:
    out = args.out if args.out is not None else DEFAULT_OUT / args.label
    scratch = (
        args.scratch
        if args.scratch is not None
        else Path(tempfile.mkdtemp(prefix=f"c10-{args.label}-"))
    )
    names = args.scenario or [s.name for s in SCENARIOS]
    failures = 0
    for name in names:
        spec = scenario(name)
        started = time.monotonic()
        print(f"[{args.label}] {name}: running {spec.duration_s:.0f}s ...", flush=True)
        try:
            artifacts = await run_scenario_clean(
                spec,
                label=args.label,
                sessions_root=args.sessions_root,
                scratch=scratch / name,
                config=(
                    None
                    if args.models_dir is None
                    else harness_config(scratch / name, models_dir=args.models_dir)
                ),
                enable_inference=not args.no_inference,
            )
            report = build_report(artifacts)
        except Exception:  # noqa: BLE001 — one scenario must not hide the rest
            log.exception("scenario %s failed", name)
            failures += 1
            continue
        path = write_report(report, out / f"{name}.json")
        headline = report["headline"]
        print(
            f"[{args.label}] {name}: {time.monotonic() - started:.1f}s wall; "
            f"limiting={headline['limiting_stage']} "
            f"sequences={headline['sequence_reconciliation']} "
            f"verdict={headline['quality_verdict']} "
            f"failures={headline['quality_failures']} -> {path}",
            flush=True,
        )
    return 1 if failures else 0


async def _run_sweep(args: argparse.Namespace) -> int:
    budgets = [float(token) for token in args.budgets.split(",") if token.strip()]
    if not budgets:
        print("no budgets given", file=sys.stderr)
        return 2
    scratch = (
        args.scratch
        if args.scratch is not None
        else Path(tempfile.mkdtemp(prefix=f"c10-sweep-{args.label}-"))
    )
    spec = scenario(args.scenario)
    print(
        f"[{args.label}] sweeping {args.scenario} over {budgets} req/s "
        f"({spec.duration_s:.0f}s each) ...",
        flush=True,
    )
    result = await run_budget_sweep(
        spec,
        budgets,
        label=args.label,
        sessions_root=args.sessions_root,
        scratch=scratch,
        enable_inference=not args.no_inference,
    )
    text = summarize_sweep(result)
    print(text)
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"{args.scenario}-sweep.json").write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
        )
        (args.out / f"{args.scenario}-sweep.md").write_text(text + "\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    args = parse_args(argv)
    if args.command == "list":
        for spec in SCENARIOS:
            print(f"{spec.name:28s} {spec.kind:8s} {spec.duration_s:5.0f}s  {spec.table_row}")
        return 0
    if args.command == "summarize":
        text = summarize_markdown(args.before, args.after)
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text)
        else:
            print(text)
        return 0
    assert _REPO_ROOT.exists()
    if args.command == "sweep":
        return asyncio.run(_run_sweep(args))
    return asyncio.run(_run_all(args))


if __name__ == "__main__":
    raise SystemExit(main())
