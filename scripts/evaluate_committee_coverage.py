#!/usr/bin/env python3
"""Which of A05.8's plumbing faults each detector owns and catches.

Operator decision 2026-10-13 (A05.8): **the sensor committees own RPM/THROTTLE
faults at idle and stuck-at-cruise; plausibility keeps single-channel range and
slew.** "100 % of plumbing faults" is restated per detector: plausibility is
judged on the faults inside its single-channel scope, the committees on the
cross-channel cases. This script grades both on the same injections.

Every fault is the one ``scripts/evaluate_plausibility.py`` injects — same
seed, same per-(drive, channel) generator, same kind order, so the same
windows and magnitudes (``src.plausibility.offline.inject_plumbing_fault`` on
arrived samples); both scripts share :mod:`src.plausibility.benchmark`. For
each graded fault it reports:

* **owner** under the decision (:func:`fault_owner`):

  - ``plausibility`` — a spike/scale/drift whose faulted values leave the
    channel's resolved range or slew envelope; a stuck fault on a channel with
    a stuck rule; a dropout on a channel whose freshness is checked;
  - ``committee`` — a spike/scale/drift on ``obd:RPM`` / ``obd:THROTTLE_POS``
    that stays inside that envelope (the "at idle" misses: physically
    possible values), and a stuck fault on a channel steady cruise holds
    (``obd:RPM``, ``obd:SPEED``; A05.3 removed their stuck rules);
  - ``unassigned`` — neither: the decision gives it to no detector (for
    example an in-envelope drift on SPEED, or a dropout of a PID freshness
    does not watch). Reported, never folded into either denominator.

  The envelope ignores mode gating, so it is generous to plausibility: a
  fault outside the range in a mode where the range check is off still
  counts as plausibility's.
* **plausibility** — confirmed on the faulted channel inside
  ``[start, end + grace)`` and absent on the clean drive (the A05.8 grading).
* **committee** — a :class:`~src.signals.committees.CommitteeFlag` naming the
  faulted channel as member or reference, detected inside the same interval
  and absent on the clean drive. Committees run offline exactly as replay
  runs them: :func:`~src.signals.committees.evaluate_committee_frame` over
  ``resample_and_segment``'s 1 Hz frame, with the fault applied to the cells
  where the channel arrived (dropout: those cells un-arrived). A channel no
  committee declares is ``uncovered`` — it is never fed to a committee, so a
  zero there is structural, not measured.

    .venv/bin/python scripts/evaluate_committee_coverage.py --config canary.example.yaml \\
        --profile nissan-xterra-2008 --sessions <dirs...> --out <report.json>
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from _cli import add_common_args, add_profile_arg, load_config  # type: ignore[import-not-found]
except ImportError:  # loaded as `scripts.evaluate_committee_coverage`
    from scripts._cli import add_common_args, add_profile_arg, load_config

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
from src.plausibility.evaluator import PlausibilityEvaluator
from src.plausibility.limits import ChannelLimits
from src.plausibility.offline import (
    PLUMBING_FAULT_KINDS,
    Injection,
    SessionStream,
    inject_plumbing_fault,
    run_evaluator,
)
from src.signals.committees import (
    COMMITTEES,
    CommitteeFlag,
    CommitteeProfile,
    evaluate_committee_frame,
    resolve_committee_profile,
)

#: Operator decision 2026-10-13: in-envelope spike/scale/drift on these
#: channels (the RPM / THROTTLE_POS misses at idle) belong to the committees.
COMMITTEE_OWNED_IN_ENVELOPE: tuple[str, ...] = ("obd:RPM", "obd:THROTTLE_POS")
#: Operator decision 2026-10-13: stuck-at-cruise belongs to the committees.
#: Steady cruise holds these exactly, so A05.3 gave them no stuck rule.
COMMITTEE_OWNED_STUCK: tuple[str, ...] = ("obd:RPM", "obd:SPEED")
#: The fix-health gate's channels, filled only by ``--assume-gps-fix`` and
#: only when a session carries neither (the committed ground-truth slice was
#: cut without them).
GPS_FIX_COLUMNS = {"gps:num_satellites": 10.0, "gps:hdop": 0.9}


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def envelope_breaches(
    stream: SessionStream, injection: Injection, limits: ChannelLimits
) -> list[str]:
    """Which single-channel envelopes the faulted values leave: ``range``
    (a value past a resolved bound) and/or ``slew`` (a step between
    consecutive arrived samples, one of them in the window, past the slew
    limit). Mode gating is ignored."""
    samples = stream.samples
    channel = samples.loc[samples["channel"] == injection.channel, ["t", "value"]]
    if channel.empty:
        return []
    times = channel["t"].to_numpy(dtype="float64")
    values = channel["value"].to_numpy(dtype="float64")
    inside = (times >= injection.start) & (times < injection.end)
    breaches: list[str] = []
    window_values = values[inside]
    low = limits.minimum.value if limits.minimum is not None else -np.inf
    high = limits.maximum.value if limits.maximum is not None else np.inf
    if bool(((window_values < low) | (window_values > high)).any()):
        breaches.append("range")
    slew = limits.slew
    if slew is not None and len(values) > 1:
        dt = np.maximum(np.diff(times), slew.update_interval_seconds)
        step = np.abs(np.diff(values))
        touches = inside[1:] | inside[:-1]
        allowed = slew.per_second * dt + slew.allowance
        if bool(((step > allowed) & touches).any()):
            breaches.append("slew")
    return breaches


def fault_owner(kind: str, limits: ChannelLimits, breaches: Sequence[str]) -> tuple[str, str]:
    """``(owner, why)`` for one fault under the 2026-10-13 decision."""
    channel = limits.channel
    if kind == "dropout":
        if limits.freshness_threshold_seconds is not None:
            return "plausibility", "freshness checked"
        return "unassigned", f"freshness {limits.coverage()['freshness']}"
    if kind == "stuck":
        if limits.stuck is not None:
            return "plausibility", "channel has a stuck rule"
        if channel in COMMITTEE_OWNED_STUCK:
            return "committee", "stuck-at-cruise (no stuck rule: cruise holds it)"
        return "unassigned", "no stuck rule, not a cruise-held channel"
    if breaches:
        return "plausibility", f"leaves the {'/'.join(breaches)} envelope"
    if channel in COMMITTEE_OWNED_IN_ENVELOPE:
        return "committee", "inside the range and slew envelope"
    return "unassigned", "inside the range and slew envelope"


# ---------------------------------------------------------------------------
# Committees on the resampled frame
# ---------------------------------------------------------------------------


def covering_committees(channel: str, profile: CommitteeProfile | None) -> dict[str, list[str]]:
    """``{"declared": [...], "configured": [...]}``: committees naming
    ``channel`` as a member, and those whose profile relation reads it."""
    declared = [c.name for c in COMMITTEES if channel in c.members]
    configured: list[str] = []
    if profile is not None:
        for name in declared:
            spec = profile.committees.get(name)
            if spec is None:
                continue
            if spec.reference == channel or any(r.member == channel for r in spec.relations):
                configured.append(name)
    return {"declared": declared, "configured": configured}


def fault_frame(frame: pd.DataFrame, injection: Injection) -> pd.DataFrame:
    """``frame`` with ``injection`` applied to the cells where its channel
    arrived inside the window — the arrived-sample fault on the 1 Hz grid."""
    channel = injection.channel
    arrived_column = f"{channel}__arrived"
    if channel not in frame.columns or arrived_column not in frame.columns:
        return frame
    out = frame.copy()
    t = pd.to_datetime(out["timestamp"], utc=True).map(pd.Timestamp.timestamp).to_numpy()
    cells = (
        (t >= injection.start)
        & (t < injection.end)
        & out[arrived_column].fillna(False).to_numpy(dtype=bool)
    )
    values = out[channel].to_numpy(dtype="float64").copy()
    parameter = injection.parameter
    if injection.kind == "dropout":
        values[cells] = np.nan
        arrived = out[arrived_column].to_numpy(dtype=bool).copy()
        arrived[cells] = False
        out[arrived_column] = arrived
    elif parameter is None:
        return frame
    elif injection.kind in ("spike", "stuck"):
        values[cells] = parameter
    elif injection.kind == "scale":
        values[cells] = values[cells] * parameter
    elif injection.kind == "drift":
        span = injection.end - injection.start
        values[cells] = values[cells] + np.clip((t[cells] - injection.start) / span, 0, 1) * (
            parameter
        )
    else:
        raise ValueError(f"unsupported plumbing fault kind: {injection.kind!r}")
    out[channel] = values
    return out


def _flag_key(flag: CommitteeFlag) -> tuple[str, str, float]:
    return (flag.committee, flag.member, round(flag.detected_at, 3))


def _flag_brief(flag: CommitteeFlag) -> dict[str, Any]:
    return {
        "committee": flag.committee,
        "member": flag.member,
        "detected_at": datetime.fromtimestamp(flag.detected_at, UTC).isoformat(),
        "departure": round(flag.departure, 4),
        "text": flag.text,
    }


# ---------------------------------------------------------------------------
# One session
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    fault_channels: tuple[str, ...]
    fault_kinds: tuple[str, ...]
    n_injections: int
    seed: int
    grace_seconds: float
    assume_gps_fix: bool


def _session_frame(session: Path, context: BenchmarkContext, assume_gps_fix: bool) -> pd.DataFrame:
    from src.preprocessing.loader import resample_and_segment

    frame = resample_and_segment([session], context.config)
    if assume_gps_fix and not any(c in frame.columns for c in GPS_FIX_COLUMNS):
        frame = frame.assign(**GPS_FIX_COLUMNS)
    return frame


def evaluate_session(
    session: Path,
    context: BenchmarkContext,
    committee_profile: CommitteeProfile | None,
    options: Options,
) -> dict[str, Any]:
    """Both detectors over every plumbing fault on one session."""
    stream = load_benchmark_stream(session, context, None)
    if stream.span is None:
        return {"session": str(session), "result": "withheld", "reason": "no numeric samples"}
    clean_records = run_evaluator(
        PlausibilityEvaluator(context.catalog, session=session.name), stream
    )
    clean_keys: set[tuple[str, str, str]] = {
        (r.channel, r.check, r.detected_at) for r in clean_records
    }
    frame = _session_frame(session, context, options.assume_gps_fix)
    committee_clean = evaluate_committee_frame(
        frame, committee_profile, bindings=context.bindings, session=session.name
    )
    clean_flags = {_flag_key(f) for f in committee_clean.flags}
    rows: list[dict[str, Any]] = []
    for channel in options.fault_channels:
        limits = context.catalog.get(channel)
        coverage = covering_committees(channel, committee_profile)
        rng = np.random.default_rng(options.seed)
        subset = restrict_to_groups(stream, [channel])
        if subset.span is None:
            continue
        for kind in options.fault_kinds:
            for run in range(options.n_injections):
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
                    rows.append(row)
                    continue
                breaches = envelope_breaches(faulted, injection, limits)
                owner, why = fault_owner(kind, limits, breaches)
                end = injection.end + options.grace_seconds
                records = evaluate_stream(faulted, context, session.name)
                hits = fault_hits(
                    records, clean_keys, channels=[channel], start=injection.start, end=end
                )
                row.update(
                    result="graded",
                    owner=owner,
                    owner_reason=why,
                    envelope_breaches=breaches,
                    plausibility=grade_hits(hits, injection.start),
                    committee=_committee_grade(
                        frame, injection, committee_profile, context, coverage, clean_flags, end
                    ),
                )
                rows.append(row)
    return {
        "session": str(session),
        "name": session.name,
        "span_seconds": round(stream.span[1] - stream.span[0], 1),
        "result": "graded",
        "committee_clean_flags": [_flag_brief(f) for f in committee_clean.flags],
        "committee_clean_counts": committee_clean.counts,
        "faults": rows,
    }


def _committee_grade(
    frame: pd.DataFrame,
    injection: Injection,
    profile: CommitteeProfile | None,
    context: BenchmarkContext,
    coverage: Mapping[str, list[str]],
    clean_flags: set[tuple[str, str, float]],
    end: float,
) -> dict[str, Any]:
    if not coverage["declared"]:
        return {"result": "uncovered", "reason": f"no committee declares {injection.channel}"}
    if not coverage["configured"]:
        return {
            "result": "uncovered",
            "reason": f"{coverage['declared']} declare it but none is configured for it",
        }
    result = evaluate_committee_frame(
        fault_frame(frame, injection), profile, bindings=context.bindings
    )
    hits = [
        f
        for f in result.flags
        if injection.channel in (f.member, f.reference)
        and injection.start <= f.detected_at < end
        and _flag_key(f) not in clean_flags
    ]
    counts = {name: result.counts.get(name, {}) for name in coverage["configured"]}
    return {
        "result": "graded",
        "caught": bool(hits),
        "latency_seconds": (
            round(min(f.detected_at for f in hits) - injection.start, 3) if hits else None
        ),
        "flags": [_flag_brief(f) for f in hits],
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Summary and CLI
# ---------------------------------------------------------------------------


def summarize(sessions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per owner and per ``channel kind``: graded, each detector's catches."""

    def cell() -> dict[str, int]:
        return defaultdict(int)

    by_owner: dict[str, dict[str, int]] = defaultdict(cell)
    by_channel: dict[str, dict[str, int]] = defaultdict(cell)
    for session in sessions:
        for row in session.get("faults", []):
            key = f"{row['channel']} {row['kind']}"
            if row["result"] != "graded":
                by_channel[key]["no_op"] += 1
                continue
            committee = row["committee"]
            caught_p = bool(row["plausibility"]["confirmed"])
            caught_c = committee["result"] == "graded" and bool(committee["caught"])
            for target in (by_owner[f"{row['owner']} {row['kind']}"], by_channel[key]):
                target["graded"] += 1
                target["plausibility_confirmed"] += int(caught_p)
                target["committee_caught"] += int(caught_c)
                target["either"] += int(caught_p or caught_c)
                target["committee_uncovered"] += int(committee["result"] == "uncovered")
            by_channel[key][f"owner_{row['owner']}"] += 1
    return {
        "by_owner_kind": {k: dict(v) for k, v in sorted(by_owner.items())},
        "by_channel_kind": {k: dict(v) for k, v in sorted(by_channel.items())},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_common_args(parser)
    add_profile_arg(parser)
    parser.add_argument("--sessions", type=Path, nargs="+", required=True)
    parser.add_argument("--fault-channels", nargs="*", default=list(BENCHMARK_CHANNELS))
    parser.add_argument("--fault-kinds", nargs="*", default=list(PLUMBING_FAULT_KINDS))
    parser.add_argument("--n-injections", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--grace-seconds", type=float, default=DEFAULT_GRACE_SECONDS)
    parser.add_argument(
        "--assume-gps-fix",
        action="store_true",
        help="Fill gps:num_satellites/hdop with a good fix when a session has "
        "neither column (the committed ground-truth slice). Recorded in the report.",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    load_config(args)  # fail fast on a bad config
    logging.getLogger("src.plausibility.evaluator").setLevel(logging.ERROR)
    context = load_benchmark_context(Path(args.config), args.profile)
    committee_profile, committee_path = resolve_committee_profile(context.config)
    options = Options(
        fault_channels=tuple(args.fault_channels),
        fault_kinds=tuple(args.fault_kinds),
        n_injections=args.n_injections,
        seed=args.seed,
        grace_seconds=args.grace_seconds,
        assume_gps_fix=args.assume_gps_fix,
    )
    sessions = []
    for session in args.sessions:
        result = evaluate_session(session, context, committee_profile, options)
        print(f"{session.name}: {result['result']}", file=sys.stderr, flush=True)
        sessions.append(result)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "decision": (
            "operator 2026-10-13: committees own RPM/THROTTLE faults at idle and "
            "stuck-at-cruise; plausibility keeps single-channel range/slew"
        ),
        "config": str(args.config),
        "profile": args.profile,
        "committee_profile": committee_path,
        "seed": args.seed,
        "n_injections": args.n_injections,
        "grace_seconds": args.grace_seconds,
        "assume_gps_fix": args.assume_gps_fix,
        "summary": summarize(sessions),
        "sessions": sessions,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, default=str) + "\n")
    print(json.dumps(report["summary"], indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
