#!/usr/bin/env python3
"""Audit every model artifact's input lineage against the catalog it was built on (B08.1).

Offline and read-only. For each behavior model (served, ``_prior``,
``_candidates``, the review fixture) and each IF/sequence manifest it traces
recipe inputs -> derived dependencies -> the source identity each name had in
the decode catalog committed when the artifact was built, and compares that
with what the name reads now (``src.analysis.artifact_lineage``). With
``--sessions-root`` it also re-decodes the stored raw samples the artifacts
trained on (Mode 01 payloads against the PID table; raw ``DID_<hex>`` replies
against the build-time and the current entry), scans session sidecars for a
stored name that now reads a different DID, and runs the road-speed negative
control through ``compare_channels``.

It writes nothing but ``--out``. Superseding what it finds affected is a B03
decision, not this tool's.

Usage:
  tools/audit_artifact_lineage.py --profile nissan-xterra-2008 \\
      --sessions-root ~/canary-data/sessions/nissan-xterra-2008 --sessions-root data/sessions \\
      --extra-models ~/canary-data/models-0920a \\
      --negative-control-session \\
          ~/canary-data/sessions/nissan-xterra-2008/_capture-rom-trace-2026-09-21T142926 \\
      --out data/analyses/2026-10-05-b08-1-artifact-lineage.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The CAN DID ids corrected on 2026-10-04 (c897a62). Raw replies for these are
#: always looked for, so their absence from an artifact's sessions is shown,
#: not assumed.
CORRECTED_CAN_DIDS: tuple[int, ...] = (0x1102, 0x1123, 0x1124, 0x1125, 0x1126)
SIDECAR_NAMES: tuple[str, ...] = ("capture_profile.json", "capture_quality.json")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="audit_artifact_lineage",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--profile", default="nissan-xterra-2008", help="Vehicle profile slug.")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--sessions-root",
        type=Path,
        action="append",
        default=[],
        help="Where stored sessions live (repeatable). Without one, nothing is re-decoded.",
    )
    parser.add_argument(
        "--extra-models",
        type=Path,
        action="append",
        default=[],
        help="Another directory to search for model.manifest.json (repeatable).",
    )
    parser.add_argument(
        "--negative-control-session",
        type=Path,
        default=None,
        help="A session holding raw DID 0x1102 replies and a wheel-speed reference.",
    )
    parser.add_argument("--out", type=Path, default=None, help="Write the JSON report here.")
    return parser.parse_args(argv)


def _rel(path: Path, repo: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo.resolve()))
    except ValueError:
        return str(path).replace(str(Path.home()), "~")


def discover(repo: Path, slug: str, extra_models: Sequence[Path]) -> list[Any]:
    from src.analysis.artifact_lineage import behavior_artifacts, manifest_artifact

    artifacts: list[Any] = []
    profile = repo / "data" / slug
    behavior_files = [
        profile / "behavior_models.yaml",
        *sorted(profile.glob("_prior/*/behavior_models.yaml")),
        *sorted(profile.glob("_candidates/*/behavior_models.yaml")),
        *sorted((repo / "tests/fixtures/behavior_validation").glob("behavior_models*.yaml")),
    ]
    for path in behavior_files:
        if path.is_file():
            artifacts.extend(behavior_artifacts(path, label=_rel(path, repo)))
    manifests = sorted((repo / "models").glob("**/model.manifest.json"))
    manifests += sorted((repo / "tests/fixtures/deployed_manifests").glob("**/model.manifest.json"))
    for root in extra_models:
        manifests += sorted(root.expanduser().glob("**/model.manifest.json"))
    for path in manifests:
        artifacts.append(manifest_artifact(path, label=_rel(path, repo)))
    return artifacts


def _derived_inputs() -> dict[str, tuple[tuple[str, ...], ...]]:
    """Required input groups declared now."""
    from src.signals.derived import DERIVED_CHANNEL_INPUTS

    return {name: tuple(map(tuple, groups)) for name, groups in DERIVED_CHANNEL_INPUTS.items()}


def _derived_optional_inputs() -> dict[str, tuple[str, ...]]:
    """Inputs read when present, declared now (compared and expanded like groups)."""
    from src.signals.derived import DERIVED_CHANNEL_OPTIONAL_INPUTS

    return {name: tuple(columns) for name, columns in DERIVED_CHANNEL_OPTIONAL_INPUTS.items()}


def _memory_names(lineage: Any, ctx: Any) -> dict[str, list[str]]:
    """RAM channel -> every stored name its rows may carry (renames included)."""
    from src.analysis.artifact_lineage import former_names

    out: dict[str, list[str]] = {}
    for leaf in lineage.leaves:
        if leaf.kind == "memory" and leaf.identity_key is not None:
            name = leaf.channel.split(":", 1)[1].removesuffix("_SENTINEL")
            former = [old for old, _ in former_names(name, "memory", ctx.renames)]
            out[name] = [f"mem:{n}" for n in (name, *former)]
    return out


def redecode_for(lineage: Any, ctx: Any, roots: Sequence[Path], cache: dict[str, Any]) -> Any:
    """Re-decode the stored samples of one artifact's sessions."""
    from src.analysis.artifact_lineage import (
        MemoryRecheck,
        find_session,
        recheck_memory,
        recheck_mode01,
        redecode_raw_dids,
        session_obd_rows,
    )
    from src.storage.sessions import session_started_at

    mode01 = sorted(
        {leaf.channel.split(":", 1)[1] for leaf in lineage.leaves if leaf.kind == "mode01"}
    )
    dids = {f"DID_{did:04X}" for did in CORRECTED_CAN_DIDS}
    dids |= {
        f"DID_{leaf.identity_key[1]:04X}"
        for leaf in lineage.leaves
        if leaf.identity_key is not None and leaf.identity_key[0] == "did"
    }
    memory = _memory_names(lineage, ctx)
    channels = sorted({*mode01, *dids, *(n for names in memory.values() for n in names)})
    memory_checks = {name: MemoryRecheck() for name in memory}
    build = ctx.history.at(lineage.artifact.built_at) if lineage.artifact.built_at else None
    found, missing = 0, []
    totals: Counter[str] = Counter()
    examples: list[str] = []
    raw: dict[str, Any] = {}
    for name in lineage.artifact.sessions:
        session = find_session(name, roots)
        if session is None:
            missing.append(name)
            continue
        found += 1
        key = f"{session}|{','.join(channels)}"
        if key not in cache:
            cache[key] = session_obd_rows(session, channels)
        rows = cache[key]
        check = recheck_mode01(rows)
        for field_name in ("rows", "with_payload", "agree", "disagree", "undecodable"):
            totals[field_name] += getattr(check, field_name)
        examples.extend(check.examples)
        started = session_started_at(name)
        recorded = ctx.history.at(started) if started is not None else None
        if recorded is not None:
            for ram, check_memory in memory_checks.items():
                check_memory.add(
                    recheck_memory(rows, ram, recorded, ctx.history.current, ctx.renames)
                )
        if build is not None:
            for channel, summary in redecode_raw_dids(rows, build, ctx.history.current).items():
                entry = raw.setdefault(
                    channel,
                    {
                        "build_entry": summary["build_entry"],
                        "current_entry": summary["current_entry"],
                        "replies": 0,
                        "identical_replies": 0,
                        "build_decoded": 0,
                        "current_decoded": 0,
                        "sessions": [],
                    },
                )
                entry["replies"] += summary["replies"]
                entry["identical_replies"] += summary["replies"] if summary["identical"] else 0
                entry["build_decoded"] += summary["build_decoded"]
                entry["current_decoded"] += summary["current_decoded"]
                entry["sessions"].append(name)
    return {
        "sessions_found": found,
        "sessions_missing": missing,
        "mode01_channels": mode01,
        "mode01_recheck": {**dict(totals), "examples": examples[:5]},
        "memory_recheck": {name: check.as_dict() for name, check in sorted(memory_checks.items())},
        "raw_did_replies_summary": {
            channel: {k: v for k, v in entry.items() if k != "sessions"}
            | {"sessions": len(entry["sessions"])}
            for channel, entry in sorted(raw.items())
        },
        "corrected_dids_in_sessions": {
            channel: entry["sessions"]
            for channel, entry in sorted(raw.items())
            if int(channel[4:], 16) in CORRECTED_CAN_DIDS
        },
    }


def corpus_scan(roots: Sequence[Path], ctx: Any) -> dict[str, Any]:
    """Every stored session: raw corrected-DID replies and stale sidecar labels."""
    from src.analysis.artifact_lineage import (
        redecode_raw_dids,
        session_obd_rows,
        stale_sidecar_labels,
    )
    from src.storage.sessions import session_started_at

    channels = [f"DID_{did:04X}" for did in CORRECTED_CAN_DIDS]
    sessions: list[Path] = []
    for root in roots:
        for base in (root, root / "_archive"):
            if base.is_dir():
                sessions += [
                    p for p in sorted(base.iterdir()) if p.is_dir() and p.name != "_archive"
                ]
    raw: dict[str, Any] = {}
    stale: list[dict[str, Any]] = []
    for session in sessions:
        for sidecar in SIDECAR_NAMES:
            path = session / sidecar
            if path.is_file():
                for hit in stale_sidecar_labels(json.loads(path.read_text()), ctx.history.current):
                    stale.append({"session": session.name, "file": sidecar, **hit})
        started = session_started_at(session.name)
        rows = session_obd_rows(session, channels)
        if not rows:
            continue
        build = ctx.history.at(started) if started else None
        if build is None:
            continue
        for channel, summary in redecode_raw_dids(rows, build, ctx.history.current).items():
            raw[f"{session.name}/{channel}"] = {"catalog_at_recording": build.label, **summary}
    return {
        "sessions_scanned": len(sessions),
        "raw_corrected_did_replies": raw,
        "stale_sidecar_labels": stale,
    }


def negative_control(session: Path) -> dict[str, Any]:
    """DID 0x1102 against road speed, through the one sanctioned comparison."""
    from src.analysis.compare import PairingPolicy, compare_channels

    out: dict[str, Any] = {"session": session.name}
    for reference in ("can:WHEEL_SPEED_6F7_A", "did:ECM_VEHICLE_SPEED"):
        try:
            result = compare_channels(
                session,
                "did:ECM_VEHICLE_SPEED_COARSE",
                reference,
                policy=PairingPolicy.within("250ms"),
            )
        except Exception as error:  # noqa: BLE001 - a refusal is a result here
            out[reference] = {"refused": f"{type(error).__name__}: {error}"}
            continue
        out[reference] = {
            "n_pairs": result.n_pairs,
            "slope": result.slope,
            "offset": result.offset,
            "r_squared": result.r_squared,
            "scale_verdict": result.scale_verdict,
            "difference_bias": result.difference_bias,
            "difference_spread": result.difference_spread,
        }
    return out


def set_rechecks(lineages: Sequence[Any], roots: Sequence[Path]) -> dict[str, Any]:
    """Mode 01 payload recheck once per behavior file, over its distinct sessions.

    Per-model figures count a session once per model that lists it; this is
    the de-duplicated figure for a whole artifact file.
    """
    from src.analysis.artifact_lineage import find_session, recheck_mode01, session_obd_rows

    by_path: dict[str, tuple[set[str], set[str]]] = {}
    for lineage in lineages:
        if lineage.artifact.kind != "behavior_model":
            continue
        sessions, channels = by_path.setdefault(lineage.artifact.path, (set(), set()))
        sessions.update(lineage.artifact.sessions)
        channels.update(
            leaf.channel.split(":", 1)[1] for leaf in lineage.leaves if leaf.kind == "mode01"
        )
    out: dict[str, Any] = {}
    for path, (sessions, channels) in sorted(by_path.items()):
        totals: Counter[str] = Counter()
        found = 0
        for name in sorted(sessions):
            session = find_session(name, roots)
            if session is None:
                continue
            found += 1
            check = recheck_mode01(session_obd_rows(session, sorted(channels)))
            for field_name in ("rows", "with_payload", "agree", "disagree", "undecodable"):
                totals[field_name] += getattr(check, field_name)
        out[path] = {
            "distinct_sessions": len(sessions),
            "sessions_found": found,
            "mode01_channels": sorted(channels),
            **dict(totals),
        }
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    sys.path.insert(0, str(REPO_ROOT))
    from src.analysis.artifact_lineage import (
        LineageContext,
        audit_artifact,
        correction_ledger,
        derived_definitions_loader,
        load_catalog_history,
        load_observation_aliases,
        load_renames,
        summarize,
        trim_qualification,
    )

    repo = args.repo.resolve()
    ctx = LineageContext(
        history=load_catalog_history(repo),
        renames=load_renames(repo / "tools" / "channel_renames"),
        aliases=load_observation_aliases(repo / "data" / args.profile / "observation_aliases.yaml"),
        derived_inputs=_derived_inputs(),
        derived_optional_inputs=_derived_optional_inputs(),
        derived_at=derived_definitions_loader(repo),
    )
    roots = [root.expanduser() for root in args.sessions_root]
    lineages = [audit_artifact(a, ctx) for a in discover(repo, args.profile, args.extra_models)]
    cache: dict[str, Any] = {}
    artifacts = []
    for lineage in lineages:
        record = lineage.as_dict()
        if roots:
            record["redecode"] = redecode_for(lineage, ctx, roots, cache)
        artifacts.append(record)
    report: dict[str, Any] = {
        "summary": summarize(lineages),
        "catalog_revisions": [s.label for s in ctx.history.snapshots],
        "correction_ledger": correction_ledger(ctx.history),
        "trim_qualification": {
            f"{label} claimed as {claim}": dict(
                zip(
                    ("qualifies", "why"),
                    trim_qualification(key, claim, ctx.aliases),
                    strict=True,
                )
            )
            for label, key, claim in (
                ("DID 0x1102", ("did", 0x1102), "fuel_trim_long_b1"),
                ("DID 0x1125", ("did", 0x1125), "fuel_trim_long_b2"),
                ("DID 0x1125", ("did", 0x1125), "fuel_trim_long_b1"),
                ("Mode 01 PID 0x07", ("mode01", 0x07), "fuel_trim_long_b1"),
                ("Mode 01 PID 0x09", ("mode01", 0x09), "fuel_trim_long_b1"),
                ("Mode 01 PID 0x0D", ("mode01", 0x0D), "fuel_trim_long_b1"),
            )
        },
        "artifacts": artifacts,
    }
    if roots:
        report["set_mode01_rechecks"] = set_rechecks(lineages, roots)
        report["corpus"] = corpus_scan(roots, ctx)
    if args.negative_control_session is not None:
        report["negative_control"] = negative_control(args.negative_control_session.expanduser())

    from src.jsonable import jsonable

    # No ``default``: a type ``jsonable`` does not know is refused, not stringified.
    text = json.dumps(jsonable(report), indent=2, sort_keys=True)
    if args.out is not None:
        args.out.write_text(text + "\n")
    print(f"verdicts: {report['summary']}")
    for record in artifacts:
        print(f"  {record['verdict']:12s} {record['path']} :: {record['name']}")
        for reason in record["reasons"]:
            if record["verdict"] != "unaffected":
                print(f"      {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
