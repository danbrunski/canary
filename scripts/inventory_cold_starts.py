#!/usr/bin/env python3
"""Inventory the corpus's genuine cold starts (B07.2).

Scans stored sessions on the laptop and reports, per engine run, whether its
start was *observed* (the serving rule, ``EngineStartTracker``), what the
initial coolant/IAT/ambient were, whether the run-time clock was continuous,
whether the vehicle was stopped before it, and how much load/IAT spread the
warmup window carried. Sessions are then grouped into independent physical
drives by parsed start time.

Nothing is fitted or published: the output is a JSON report (and a printed
summary) naming what evidence exists, so the warmup decision rule
(``refit.choose_warmup_confounding_strategy``) can be re-run against a count
that is current and auditable instead of a July note.

Usage::

    .venv/bin/python scripts/inventory_cold_starts.py \\
        --root ~/canary-data --out /tmp/cold-starts.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.cold_start_inventory import (  # noqa: E402
    INVENTORY_CHANNELS,
    InventorySpec,
    SessionInventory,
    group_into_drives,
    per_second_view,
    runs_of_session,
    usable_context_runs,
)
from src.storage.sessions import session_started_at  # noqa: E402

#: Replays re-emit a stored drive: their "starts" are copies, not new
#: evidence, and counting them would inflate the inventory.
EXCLUDED_PREFIXES = ("_replay-",)


def find_sessions(root: Path) -> list[Path]:
    """Session directories under ``root``, one per session name.

    Deduplicated **per directory**, not per chunk: the same session is often
    present in several pulled directories, and weighing the copies once per
    *file* re-globbed both directories tens of thousands of times — a scan
    that read 17 MB and then spun at 100 % CPU for an hour (2026-09-22).
    """
    directories: dict[str, list[Path]] = {}
    for chunk in root.glob("**/telemetry-*.parquet"):
        directory = chunk.parent
        if directory.name.startswith(EXCLUDED_PREFIXES):
            continue
        copies = directories.setdefault(directory.name, [])
        if directory not in copies:
            copies.append(directory)
    chosen: list[Path] = []
    for name in sorted(directories):
        copies = directories[name]
        # Keep the copy with the most data rather than counting it twice.
        chosen.append(copies[0] if len(copies) == 1 else max(copies, key=_bytes))
    return chosen


def _bytes(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.glob("telemetry-*.parquet"))


def read_polled(session: Path, channels: Sequence[str]) -> pd.DataFrame:
    """The session's polled rows for ``channels`` only, as a long frame.

    DuckDB rather than a per-chunk pyarrow filter: a session directory holds
    hundreds of chunks whose CAN rows outnumber the polled ones 100:1, and
    filtering them row by row in Python took ~50 minutes over this corpus
    against ~3 minutes here.
    """
    import duckdb

    names = [channel.split(":", 1)[1] for channel in channels]
    pattern = str(session / "telemetry-*.parquet")
    placeholders = ", ".join("?" for _ in names)
    query = (
        "select timestamp, channel, value_number from "
        f"read_parquet(?, union_by_name=true) where source = 'obd' "
        f"and channel in ({placeholders}) order by timestamp"
    )
    try:
        frame = duckdb.connect().execute(query, [pattern, *names]).df()
    except Exception:  # an unreadable/empty session is "no polled rows"
        return pd.DataFrame(columns=["timestamp", "source_channel", "value_number"])
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "source_channel", "value_number"])
    frame["source_channel"] = "obd:" + frame["channel"].astype(str)
    return frame


def sidecar(session: Path) -> dict[str, Any]:
    path = session / "classification.json"
    if not path.exists():
        return {"cold_start": None, "classified_at": None, "stale": None}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {"cold_start": None, "classified_at": "unreadable", "stale": None}
    from src.analysis.behavior_validation import mode_labels_predate_fix

    classified_at = str(data.get("classified_at") or "")
    kinds = {event.get("kind") for event in data.get("events", [])}
    return {
        "cold_start": "cold_start" in kinds,
        "classified_at": classified_at or None,
        "stale": mode_labels_predate_fix(classified_at) if classified_at else None,
    }


def inventory_session(session: Path, spec: InventorySpec) -> SessionInventory:
    long = read_polled(session, INVENTORY_CHANNELS)
    present = set(long["source_channel"].unique()) if not long.empty else set()
    required = {spec.rpm_channel, spec.runtime_channel, spec.coolant_channel}
    missing = sorted(required - present)
    marks = sidecar(session)
    started = session_started_at(session.name)
    base = SessionInventory(
        session=session.name,
        path=str(session),
        is_capture=session.name.startswith("_"),
        started_at=started.isoformat() if started else None,
        rows=int(len(long)),
        missing_channels=missing,
        sidecar_cold_start=marks["cold_start"],
        sidecar_classified_at=marks["classified_at"],
        sidecar_mode_labels_stale=marks["stale"],
    )
    if long.empty:
        base.reason = "no polled rows: this recording carried no Mode 01 telemetry at all"
        return base
    if missing:
        base.reason = "missing cold-start contract channels: " + ", ".join(missing)
        return base
    view = per_second_view(long, INVENTORY_CHANNELS)
    base.runs = runs_of_session(view, spec)
    if not base.runs:
        base.reason = "no running rows: the engine never reached the running threshold"
    elif not base.cold_starts:
        kinds = sorted({run.classification for run in base.runs})
        base.reason = "engine ran but no start qualified as cold: " + ", ".join(kinds)
    return base


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-initial-coolant", type=float, default=None)
    args = parser.parse_args(argv)
    root = args.root.expanduser()
    spec = InventorySpec()
    if args.max_initial_coolant is not None:
        spec = InventorySpec(max_initial_coolant_c=args.max_initial_coolant)

    from src.analysis.refit import (
        WARMUP_CONTEXT_MIN_SPREAD,
        WARMUP_CONTEXT_SESSION_FLOOR,
        choose_warmup_confounding_strategy,
    )

    sessions = find_sessions(root)
    inventories = [inventory_session(session, spec) for session in sessions]
    drives = group_into_drives(inventories)
    usable = usable_context_runs(inventories, WARMUP_CONTEXT_MIN_SPREAD)
    capture_names = {inv.session for inv in inventories if inv.is_capture}
    cold_drives = [drive for drive in drives if drive["cold_starts"]]
    trainable_cold_drives = [
        drive
        for drive in cold_drives
        if any(piece not in capture_names for piece in drive["pieces"])
    ]
    usable_sessions = {name for name, _run in usable}
    usable_drives = [
        drive for drive in cold_drives if any(piece in usable_sessions for piece in drive["pieces"])
    ]
    decision = choose_warmup_confounding_strategy(len(usable_drives))
    report = {
        "report": "cold-start-inventory",
        "root": str(root),
        "spec": {
            "rpm_channel": spec.rpm_channel,
            "runtime_channel": spec.runtime_channel,
            "coolant_channel": spec.coolant_channel,
            "min_running_rpm": spec.min_running_rpm,
            "max_start_runtime_s": spec.max_start_runtime_s,
            "max_initial_coolant_c": spec.max_initial_coolant_c,
            "warm_coolant_c": spec.warm_coolant_c,
        },
        "context_rule": {
            "min_spread": dict(WARMUP_CONTEXT_MIN_SPREAD),
            "session_floor": WARMUP_CONTEXT_SESSION_FLOOR,
            "usable_cold_start_drives": len(usable_drives),
            "decision": decision,
        },
        "totals": {
            "sessions_scanned": len(inventories),
            "sessions_without_polled_rows": sum(1 for i in inventories if i.rows == 0),
            "sessions_missing_contract_channels": sum(1 for i in inventories if i.missing_channels),
            "sessions_with_engine_runs": sum(1 for i in inventories if i.runs),
            "sessions_with_cold_start": sum(1 for i in inventories if i.cold_starts),
            "engine_runs": sum(len(i.runs) for i in inventories),
            "cold_starts": sum(len(i.cold_starts) for i in inventories),
            "warm_restarts": sum(
                1 for i in inventories for r in i.runs if r.classification == "warm_restart"
            ),
            "unknown_initial_temperature": sum(
                1
                for i in inventories
                for r in i.runs
                if r.classification == "unknown_initial_temperature"
            ),
            "drives": len(drives),
            "drives_with_cold_start": len(cold_drives),
            "drives_with_cold_start_excluding_captures": len(trainable_cold_drives),
            "sidecar_cold_start_disagreements": sum(
                1
                for i in inventories
                if i.sidecar_cold_start is not None and i.sidecar_cold_start != bool(i.cold_starts)
            ),
        },
        "drives": drives,
        "sessions": [inventory.as_dict() for inventory in inventories],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps({k: report[k] for k in ("spec", "context_rule", "totals")}, indent=2))
    for drive in cold_drives:
        marker = " (capture)" if drive["is_capture"] else ""
        print(f"cold start drive {drive['drive_id']}{marker}: {len(drive['cold_starts'])} run(s)")
        for run in drive["cold_starts"]:
            print(
                f"    {run['session']} {run['started_at']} coolant={run['initial_coolant_c']} "
                f"iat={run['initial_iat_c']} ambient={run['ambient_c']} "
                f"load_spread={run['load_spread']} iat_spread={run['iat_spread']} "
                f"stationary_before={run['stationary_before_start']} "
                f"runtime_continuous={run['runtime_continuous']} cat_rows={run['catalyst_rows']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
