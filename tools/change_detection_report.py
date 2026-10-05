#!/usr/bin/env python3
"""Independent-drive report for change detection and the adaptive candidate.

E03.3/E03.5: reads stored sessions under ``--sessions-dir`` (or a sample
fixture written by ``--dump-fixture``), gates and pairs the residual declared
in the vehicle's ``change_detection.yaml``, freezes a reference on the older
drives of one calibration (train, then calibration fold), and reports on the
newest drives it never saw: coverage, exceedances, events per detector, and
detection latency for each declared *synthetic* shift. ``--candidate`` also
replays every drive through the monitor and the Kalman candidate together.
The policy freeze and the results go to the vehicle's *persistent* memory
ledger — ``--config`` (``daemon.memory_ledger`` of the scoped vehicle) or an
explicit ``--ledger`` path. There is no scratch default: a freeze on a
throwaway ledger could be redone with another policy on the same test drives
(``freeze_policy`` refuses that only on a ledger that remembers). Without
either, the tool reports the preparation and refuses to monitor test drives.

Reads whole sessions: run it on the laptop, or on the Pi only while nothing
is recording (AGENTS.md, "No analysis on the Pi while it collects").

Usage:
  .venv/bin/python tools/change_detection_report.py \\
      --sessions-dir ~/canary-data/sessions/nissan-xterra-2008 --candidate
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.analysis.change_evaluation import (  # noqa: E402
    CHANGE_RESIDUALS_FILE,
    assemble_drives,
    evaluate,
    freeze_policy,
    load_change_residuals,
    mint_change_report,
    prepare,
    read_session_samples,
    replay_candidate,
    samples_from_frame,
    samples_to_frame,
)
from src.evidence.ledger import MemoryLedger  # noqa: E402
from src.evidence.records import VehicleIdentity  # noqa: E402
from src.models.change_detection import blocks_of  # noqa: E402
from src.paths import DATA_DIR  # noqa: E402
from src.vehicle.tune_revisions import load_tune_revisions  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", default="nissan-xterra-2008")
    parser.add_argument("--sessions-dir", type=Path, default=None)
    parser.add_argument("--from-fixture", type=Path, default=None, help="samples.csv + meta.json")
    parser.add_argument("--dump-fixture", type=Path, default=None)
    parser.add_argument("--residual", default="stft_bank_balance")
    parser.add_argument(
        "--calibration",
        default=None,
        help="Tune revision the reference is fitted on (default: the newest drive's).",
    )
    parser.add_argument("--candidate", action="store_true")
    parser.add_argument("--ledger", type=Path, default=None, help="Persistent memory ledger.")
    parser.add_argument(
        "--config", type=Path, default=None, help="canary.yaml: use its vehicle memory ledger."
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    import pandas as pd

    profile_dir = DATA_DIR / args.profile
    residuals, residuals_sha = load_change_residuals(profile_dir / CHANGE_RESIDUALS_FILE)
    definition = residuals.residual(args.residual)
    if args.from_fixture is not None:
        frame = pd.read_csv(args.from_fixture / "samples.csv")
        meta = json.loads((args.from_fixture / "meta.json").read_text())
        sessions = samples_from_frame(frame, meta)
    elif args.sessions_dir is not None:
        registry = load_tune_revisions(profile_dir / "tune_revisions.yaml")
        dirs = sorted(
            p
            for p in Path(args.sessions_dir).expanduser().iterdir()
            if p.is_dir() and not p.name.startswith("_archive") and any(p.glob("telemetry-*"))
        )
        sessions = [read_session_samples(d, definition, registry=registry) for d in dirs]
    else:
        print("--sessions-dir or --from-fixture is required", file=sys.stderr)
        return 2
    calibration = args.calibration
    if calibration is None:
        dated = [s for s in sessions if s.exclusion is None and len(s.t) and s.first is not None]
        calibration = max(dated, key=lambda s: s.first).calibration if dated else None  # type: ignore[arg-type,return-value]
    if args.dump_fixture is not None:
        args.dump_fixture.mkdir(parents=True, exist_ok=True)
        keep = [s for s in sessions if s.calibration == calibration or s.exclusion is not None]
        frame, meta = samples_to_frame(keep)
        # Acquisition seconds need millisecond precision; values do not.
        frame = frame.assign(
            t=frame["t"].map("{:.3f}".format),
            y=frame["y"].map("{:.5g}".format),
            condition=frame["condition"].map("{:.5g}".format),
        )
        frame.to_csv(args.dump_fixture / "samples.csv", index=False)
        (args.dump_fixture / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True))
    drives, excluded = assemble_drives(sessions, calibration=calibration)
    prep = prepare(
        drives,
        definition,
        calibration=calibration,
        excluded_sessions=excluded,
        residuals_sha256=residuals_sha,
    )
    if prep.reference is None:
        print(json.dumps({"refusal": prep.refusal, "tuning": [vars(r) for r in prep.tuning]}))
        return 1
    vehicle = VehicleIdentity(
        profile=args.profile, vin=None, program_id=None, calibration=calibration
    )
    if args.ledger is not None:
        ledger = MemoryLedger(args.ledger, vehicle=args.profile)
    elif args.config is not None:
        from src.config import CanaryConfig
        from src.evidence.ledger import memory_ledger_for
        from src.vehicle.profiles import resolve_profile_for_tooling, scope_config

        base = CanaryConfig.from_yaml(args.config)
        ledger = memory_ledger_for(
            scope_config(base, resolve_profile_for_tooling(base, args.profile))
        )
    else:
        print(
            json.dumps(
                {
                    "refused": "no persistent memory ledger (--config or --ledger): the policy "
                    "freeze must be on a ledger that remembers it",
                    "selected_policy": prep.selected,
                    "folds": prep.folds,
                }
            )
        )
        return 2
    path = ledger.path
    with tempfile.TemporaryDirectory() as scratch:
        frozen = freeze_policy(ledger, prep, vehicle=vehicle)
        report = evaluate(prep, ledger=ledger, frozen_record=frozen)
        print(json.dumps(report.summary(), indent=None if args.json else 1, default=str))
        ids = mint_change_report(ledger, report, vehicle=vehicle)
        print(json.dumps({"minted": {k: len(v) for k, v in ids.items()}, "ledger": str(path)}))
        if args.candidate:
            ordered = [
                (d.drive_id, blocks_of(prep.policy, d.drive_id, d.samples(definition))[0])
                for d in drives
            ]
            # A replay of the whole history must not inherit the test-run
            # alarms just minted: it gets its own scratch ledger.
            replay_ledger = MemoryLedger(Path(scratch) / "replay.jsonl", vehicle=args.profile)
            traces, candidate = replay_candidate(
                ordered,
                prep.reference,
                definition.candidate,
                ledger=replay_ledger,
                calibration=calibration,
                channels=definition.channels,
            )
            for trace in traces:
                print(
                    json.dumps(
                        {
                            "drive": trace.drive,
                            "frozen_at_start": len(trace.frozen_at_start),
                            "outcomes": trace.outcomes,
                            "block_alarms": trace.block_alarms,
                            "drive_alarms": trace.drive_alarms,
                            "divergence_alarms": trace.divergence_alarms,
                            "theta": [round(v, 3) for v in trace.theta_after],
                            "divergence": round(trace.divergence, 3),
                            "drive_u": trace.drive_u,
                            "ref_res": trace.reference_residual,
                            "cand_res": trace.candidate_residual,
                        },
                        default=str,
                    )
                )
            print(json.dumps({"identifiability": candidate.identifiability()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
