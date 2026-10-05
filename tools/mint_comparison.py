#!/usr/bin/env python3
"""Compare two channels in a stored session and record the result as evidence.

E01.2: runs the sanctioned comparison (``src.analysis.compare``) through
``src.analysis.compare_evidence.mint_channel_comparison``, which writes one
evidence record to the vehicle's memory ledger (``daemon.memory_ledger``,
``control/<profile>/memory.jsonl`` under ``state_dir``) and returns its id.
Cite that id from an investigation or card; the numbers are rendered from the
record. A refusal (input never captured, too few pairs, a constant signal) is
recorded as unevaluable evidence and printed as such.

The tool states its own conditions (``all conditions``: it filters nothing)
and resolves the calibration from the session's capture manifest or the
vehicle's tune-revision registry. Program identity is unknown unless
``--program-id`` states it.

Reads a whole session: run it on the laptop, or on the Pi only while nothing
is recording (AGENTS.md, "No analysis on the Pi while it collects").

Usage:
  tools/mint_comparison.py --config canary.dev.example.yaml \\
      --profile nissan-xterra-2008 --session 2026-09-20T180000 \\
      --tolerance 50ms obd:RPM can:ECM_RPM
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mint_comparison",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("a", help="Reference channel spec, e.g. obd:RPM.")
    parser.add_argument("b", help="Compared channel spec, e.g. can:ECM_RPM.")
    parser.add_argument("--config", type=Path, required=True, help="canary.yaml to scope by.")
    parser.add_argument("--profile", default=None, help="Vehicle profile slug.")
    parser.add_argument("--session", required=True, help="Session id under the storage path.")
    parser.add_argument(
        "--tolerance",
        required=True,
        help="Largest acquisition-time separation of a pair, e.g. 50ms. No default: "
        "the tolerance is part of the claim.",
    )
    parser.add_argument("--min-pairs", type=int, default=None, help="Minimum aligned pairs.")
    parser.add_argument(
        "--program-id", default=None, help="ECU program identity, when known (else unknown)."
    )
    parser.add_argument("--json", action="store_true", help="Print the stored record as JSON.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    sys.path.insert(0, str(REPO_ROOT))
    from src.analysis.compare import MIN_PAIRS, PairingPolicy
    from src.analysis.compare_evidence import mint_channel_comparison
    from src.config import CanaryConfig
    from src.evidence import Evidence, memory_ledger_for, record_to_dict
    from src.storage.sessions import resolve_session
    from src.vehicle.profiles import active_registry, resolve_profile_for_tooling, scope_config

    base = CanaryConfig.from_yaml(args.config)
    config = scope_config(base, resolve_profile_for_tooling(base, args.profile))
    session = resolve_session(config.collection.storage_path, args.session)
    ledger = memory_ledger_for(config)
    evidence_id = mint_channel_comparison(
        ledger,
        session,
        args.a,
        args.b,
        policy=PairingPolicy.within(args.tolerance),
        vin=config.vehicle.vin or None,
        program_id=args.program_id,
        tune_registry=active_registry(config),
        min_pairs=args.min_pairs if args.min_pairs is not None else MIN_PAIRS,
    )
    record = ledger.resolve_evidence(evidence_id).record
    assert isinstance(record, Evidence)
    if args.json:
        print(json.dumps(record_to_dict(record), indent=2, sort_keys=True))
        return 0
    print(f"evidence {record.id}  ({ledger.path})")
    print(f"  subject     {record.subject}")
    print(f"  calibration {record.vehicle.calibration or 'unknown'}")
    print(f"  conditions  {record.conditions.regime}")
    if record.supersedes:
        print(f"  supersedes  {', '.join(record.supersedes)}")
    if record.result.status == "unevaluable":
        print(f"  unevaluable ({record.result.reason}): {record.result.detail}")
        return 0
    for name, quantity in record.values.items():
        print(f"  {name:<18}{quantity.value:.6g} {quantity.unit}")
    for name, label in record.labels.items():
        print(f"  {name:<18}{label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
