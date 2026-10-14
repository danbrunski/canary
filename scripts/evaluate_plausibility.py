#!/usr/bin/env python3
"""Evaluate the plausibility checks on stored drives (A05.8).

Runs the live evaluator (:mod:`src.plausibility`) over each session's
*arrived* samples — no resampling, no forward-fill — and reports, per drive:

* **clean** — pending and confirmed detections on the unmodified drive. The
  acceptance is zero *confirmed* on held-out drives (drives the vehicle's
  ``plausibility.yaml`` numbers were not measured on);
* **plumbing faults** — the 2026-09-20 benchmark's five kinds (spike, drift,
  dropout, stuck, scale) at ``synthetic_fault_eval._inject_one``'s
  magnitudes, on the named channels, ``--n-injections`` each from one seeded
  generator per (drive, channel) in the benchmark's kind order;
* **realistic faults** (``--realistic``) — A02.6's kinds swept over severity
  through ``anomaly_injection.inject_realistic_fault``, applied only to
  samples that arrived.

A detection counts for a fault only when it lands on a faulted channel inside
``[start, end + grace)`` and did not occur on the clean drive. An injection
that changed no sample (channel absent, or stuck at the value it already
held) is reported as a no-op, never as a miss.

    .venv/bin/python scripts/evaluate_plausibility.py --config canary.example.yaml \\
        --profile nissan-xterra-2008 --clean <held-out session dirs...> \\
        --fault-sessions <dirs...> --realistic --out data/analyses/<date>-plausibility.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from _cli import add_common_args, add_profile_arg, load_config
except ImportError:  # loaded as `scripts.evaluate_plausibility`
    from scripts._cli import add_common_args, add_profile_arg, load_config  # type: ignore[no-redef]

from src.plausibility.benchmark import (
    BENCHMARK_CHANNELS,
    DEFAULT_GRACE_SECONDS,
    BenchmarkContext,
    evaluate_stream,
    fault_hits,
    grade_hits,
    load_benchmark_context,
    load_benchmark_stream,
    restrict_to_groups,
)
from src.plausibility.evaluator import EvidenceRecord, PlausibilityEvaluator
from src.plausibility.offline import (
    PLUMBING_FAULT_KINDS,
    SessionStream,
    inject_plumbing_fault,
    inject_realistic_samples,
    run_evaluator,
)
from src.session import session_sort_key
from src.storage.sessions import session_started_at

DEFAULT_WINDOW_SECONDS = 240.0
DEFAULT_LEAD_SECONDS = 240.0


@dataclass(frozen=True)
class Job:
    session: str
    clean: bool
    fault_channels: tuple[str, ...]
    fault_kinds: tuple[str, ...]
    n_injections: int
    seed: int
    realistic: bool
    realistic_reps: int
    window_seconds: float
    lead_seconds: float
    grace_seconds: float
    cache_dir: str | None


_CONTEXT: BenchmarkContext | None = None


def _init(config_path: str, profile: str | None) -> None:
    global _CONTEXT
    # Every injected fault confirms by design; the live warning per
    # confirmation would bury the report.
    logging.getLogger("src.plausibility.evaluator").setLevel(logging.ERROR)
    _CONTEXT = load_benchmark_context(Path(config_path), profile)


def _record_brief(record: EvidenceRecord) -> dict[str, Any]:
    return {
        "state": record.state,
        "channel": record.channel,
        "check": record.check,
        "mode": record.operating_mode,
        "detected_at": record.detected_at,
        "text": record.text,
    }


def _context_frame(stream: SessionStream) -> pd.DataFrame:
    """1 Hz carried-forward frame for ``choose_fault_window``'s conditions."""
    from src.preprocessing.anomaly_injection import (
        FUEL_STATUS_CHANNEL,
        MAF_CHANNEL,
        RPM_CHANNEL,
        SPEED_CHANNEL,
    )

    wanted = {
        RPM_CHANNEL,
        SPEED_CHANNEL,
        FUEL_STATUS_CHANNEL,
        MAF_CHANNEL,
        "obd:COOLANT_TEMP",
        "obd:O2_WR_LAMBDA_S1",
    }
    part = stream.samples.loc[stream.samples["channel"].isin(wanted)]
    if part.empty:
        return pd.DataFrame({"timestamp": pd.to_datetime([], utc=True)})
    second = np.floor(part["t"].to_numpy())
    wide = part.assign(second=second).pivot_table(
        index="second", columns="channel", values="value", aggfunc="last"
    )
    grid = np.arange(wide.index.min(), wide.index.max() + 1.0)
    wide = wide.reindex(grid).ffill()
    wide = wide.reset_index(names="second")
    wide["timestamp"] = pd.to_datetime(wide["second"], unit="s", utc=True)
    return wide


def run_job(job: Job) -> dict[str, Any]:
    context = _CONTEXT
    assert context is not None
    session = Path(job.session)
    started = session_started_at(session)
    out: dict[str, Any] = {
        "session": str(session),
        "name": session.name,
        "started_at": started.isoformat() if started else None,
    }
    try:
        stream = load_benchmark_stream(session, context, job.cache_dir)
    except FileNotFoundError as exc:
        out["result"] = "withheld"
        out["reason"] = f"no telemetry: {exc}"
        return out
    span = stream.span
    if span is None:
        out["result"] = "withheld"
        out["reason"] = "no numeric samples"
        return out
    out["span_seconds"] = round(span[1] - span[0], 1)
    out["samples"] = len(stream.samples)
    clean_evaluator = PlausibilityEvaluator(context.catalog, session=session.name)
    clean_records = run_evaluator(clean_evaluator, stream)
    coverage = clean_evaluator.coverage_summary()
    out["coverage"] = coverage
    out["modes"] = {
        str(k): int(v) for k, v in pd.Series(stream.modes).value_counts(dropna=False).items()
    }
    confirmed = [r for r in clean_records if r.state == "confirmed"]
    pending = [r for r in clean_records if r.state == "pending"]
    if coverage["observed_channels_checked"] == 0:
        out["result"] = "skipped"
        out["reason"] = "no observed channel has any check"
    else:
        out["result"] = "flagged" if confirmed else "scored_quiet"
    out["clean"] = {
        "pending": len(pending),
        "confirmed": len(confirmed),
        "confirmed_records": [_record_brief(r) for r in confirmed],
        "pending_by_check": dict(
            pd.Series([r.check for r in pending], dtype=object).value_counts().items()
        ),
        "pending_records": [_record_brief(r) for r in pending[:40]],
    }
    clean_keys = {(r.channel, r.check, r.detected_at) for r in clean_records}

    faults: list[dict[str, Any]] = []
    for channel in job.fault_channels:
        if not bool((stream.samples["channel"] == channel).any()):
            for kind in job.fault_kinds:
                faults.append(
                    {
                        "channel": channel,
                        "kind": kind,
                        "result": "no-op",
                        "reason": "channel not observed on this drive",
                    }
                )
            continue
        rng = np.random.default_rng(job.seed)
        subset = restrict_to_groups(stream, [channel])
        for kind in job.fault_kinds:
            for run in range(job.n_injections):
                faulted, injection = inject_plumbing_fault(
                    subset, channel=channel, kind=kind, rng=rng
                )
                row: dict[str, Any] = {
                    "channel": channel,
                    "kind": kind,
                    "run": run,
                    "start": injection.start,
                    "end": injection.end,
                    "changed_samples": injection.changed,
                    "parameter": injection.parameter,
                }
                if injection.changed == 0:
                    row["result"] = "no-op"
                    row["reason"] = "the injection changed no arrived sample"
                    faults.append(row)
                    continue
                records = evaluate_stream(faulted, context, session.name)
                hits = fault_hits(
                    records,
                    clean_keys,
                    channels=[channel],
                    start=injection.start,
                    end=injection.end + job.grace_seconds,
                )
                row.update(grade_hits(hits, injection.start))
                row["result"] = "graded"
                faults.append(row)
    out["plumbing"] = faults

    realistic: list[dict[str, Any]] = []
    if job.realistic:
        from src.preprocessing.anomaly_injection import REALISTIC_FAULTS, choose_fault_window

        frame = _context_frame(stream)
        role_channels = [c for channels in context.roles.values() for c in channels]
        realistic_stream = restrict_to_groups(stream, [*role_channels, "obd:RPM"])
        for kind, spec in REALISTIC_FAULTS.items():
            for rep in range(job.realistic_reps):
                digest = hashlib.sha256(f"{job.seed}|{session.name}|{kind}|{rep}".encode())
                rng = np.random.default_rng(int(digest.hexdigest()[:8], 16))
                window = choose_fault_window(
                    frame,
                    kind=kind,
                    length_seconds=job.window_seconds,
                    lead_seconds=job.lead_seconds,
                    rng=rng,
                )
                if isinstance(window, str):
                    realistic.append(
                        {"kind": kind, "rep": rep, "result": "skipped", "reason": window}
                    )
                    continue
                start = window[0].timestamp()
                end = window[1].timestamp()
                for severity in spec.severities:
                    faulted, report = inject_realistic_samples(
                        realistic_stream,
                        kind=kind,
                        severity=severity,
                        start=start,
                        end=end,
                        roles=context.roles,
                        bindings=context.bindings,
                    )
                    row = {
                        "kind": kind,
                        "rep": rep,
                        "severity": severity,
                        "severity_unit": spec.severity_unit,
                        "perturbed": report["perturbed"],
                        "not_perturbed": report["not_perturbed"],
                        "changed_samples": report["changed_samples"],
                    }
                    if report["changed_samples"] == 0:
                        row["result"] = "no-op"
                        row["reason"] = "no role channel arrived in the window"
                        realistic.append(row)
                        continue
                    records = evaluate_stream(faulted, context, session.name)
                    hits = fault_hits(
                        records,
                        clean_keys,
                        channels=report["perturbed"],
                        start=start,
                        end=end + job.grace_seconds,
                    )
                    row.update(grade_hits(hits, start))
                    row["result"] = "graded"
                    realistic.append(row)
    out["realistic"] = realistic
    return out


def summarize(rows: Sequence[Mapping[str, Any]], clean_sessions: set[str]) -> dict[str, Any]:
    plumbing: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_channel: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        for fault in row.get("plumbing", []):
            cell = plumbing[fault["kind"]]
            chan = by_channel[f"{fault['channel']} {fault['kind']}"]
            if fault["result"] != "graded":
                cell["no_op"] += 1
                chan["no_op"] += 1
                continue
            for target in (cell, chan):
                target["graded"] += 1
                target["detected"] += int(fault["detected"])
                target["confirmed"] += int(fault["confirmed"])
    realistic: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        for fault in row.get("realistic", []):
            key = f"{fault['kind']} {fault.get('severity', '-')}"
            cell = realistic[key]
            if fault["result"] != "graded":
                cell[fault["result"]] += 1
                continue
            cell["graded"] += 1
            cell["detected"] += int(fault["detected"])
            cell["confirmed"] += int(fault["confirmed"])
    clean = [
        {
            "session": row["name"],
            "started_at": row.get("started_at"),
            "result": row.get("result"),
            "reason": row.get("reason"),
            "span_seconds": row.get("span_seconds"),
            "pending": row.get("clean", {}).get("pending"),
            "confirmed": row.get("clean", {}).get("confirmed"),
        }
        for row in rows
        if row["session"] in clean_sessions
    ]
    return {
        "plumbing_by_kind": {k: dict(v) for k, v in plumbing.items()},
        "plumbing_by_channel_kind": {k: dict(v) for k, v in sorted(by_channel.items())},
        "realistic": {k: dict(v) for k, v in realistic.items()},
        "clean_drives": clean,
        "clean_confirmed_total": sum(int(c["confirmed"] or 0) for c in clean),
        "clean_hours": round(sum(float(c["span_seconds"] or 0.0) for c in clean) / 3600.0, 3),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_common_args(parser)
    add_profile_arg(parser)
    parser.add_argument(
        "--clean",
        type=Path,
        nargs="*",
        default=[],
        help="Held-out drives scored clean (and for realistic faults).",
    )
    parser.add_argument(
        "--fault-sessions",
        type=Path,
        nargs="*",
        default=[],
        help="Drives the plumbing faults are injected into.",
    )
    parser.add_argument("--fault-channels", nargs="*", default=list(BENCHMARK_CHANNELS))
    parser.add_argument("--fault-kinds", nargs="*", default=list(PLUMBING_FAULT_KINDS))
    parser.add_argument("--n-injections", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--realistic", action="store_true")
    parser.add_argument("--realistic-reps", type=int, default=2)
    parser.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--lead-seconds", type=float, default=DEFAULT_LEAD_SECONDS)
    parser.add_argument("--grace-seconds", type=float, default=DEFAULT_GRACE_SECONDS)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    load_config(args)  # fail fast on a bad config before spawning workers

    clean = {str(p) for p in args.clean}
    faults = {str(p) for p in args.fault_sessions}
    sessions = sorted(
        clean | faults,
        key=lambda s: session_sort_key(Path(s)),
    )
    jobs = [
        Job(
            session=s,
            clean=s in clean,
            fault_channels=tuple(args.fault_channels) if s in faults else (),
            fault_kinds=tuple(args.fault_kinds),
            n_injections=args.n_injections,
            seed=args.seed,
            realistic=args.realistic and s in clean,
            realistic_reps=args.realistic_reps,
            window_seconds=args.window_seconds,
            lead_seconds=args.lead_seconds,
            grace_seconds=args.grace_seconds,
            cache_dir=str(args.cache_dir) if args.cache_dir else None,
        )
        for s in sessions
    ]
    rows: list[dict[str, Any]] = []

    def collect(row: dict[str, Any]) -> None:
        rows.append(row)
        clean_part = row.get("clean", {})
        print(
            f"{row['name']}: {row.get('result')} pending={clean_part.get('pending')} "
            f"confirmed={clean_part.get('confirmed')}",
            file=sys.stderr,
            flush=True,
        )

    if args.workers <= 1:
        _init(str(args.config), args.profile)
        for job in jobs:
            collect(run_job(job))
    else:
        with ProcessPoolExecutor(
            max_workers=args.workers,
            initializer=_init,
            initargs=(str(args.config), args.profile),
        ) as pool:
            for row in pool.map(run_job, jobs):
                collect(row)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "config": str(args.config),
        "profile": args.profile,
        "seed": args.seed,
        "n_injections": args.n_injections,
        "fault_channels": list(args.fault_channels),
        "fault_kinds": list(args.fault_kinds),
        "grace_seconds": args.grace_seconds,
        "summary": summarize(rows, clean),
        "drives": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, default=str) + "\n")
    print(json.dumps(report["summary"], indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
