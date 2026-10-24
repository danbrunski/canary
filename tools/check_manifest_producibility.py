#!/usr/bin/env python3
"""Check that deployed model manifests are still producible by the catalog.

D02.5: a binding retraction or DID rename is a feature-spec change. This reads
each ``model.manifest.json`` under the (profile-scoped) models dir and reports
every feature column that no current source — bindings.yaml, dids.yaml, the
DBC files, the Mode 01 registry, GPS, and the derived/deviation channels the
feature transform computes from them — can produce. Offline and read-only: it
never talks to the daemon or the bus, so it is safe to run on the Pi while it
collects (it is a few seconds of CPU).

Exit status: 0 when every manifest is producible, 1 when any column is not or a
consumed behavior-model column was produced by a different artifact than the one
serving now (B03.4: a refit changed its meaning), 2 when a manifest has no
feature spec. A manifest that predates the fingerprint record is reported as
unknown provenance — neither a pass nor a failure.

Usage:
  tools/check_manifest_producibility.py --config /etc/canary/canary.yaml
  tools/check_manifest_producibility.py --config canary.example.yaml \\
      --profile nissan-xterra-2008 --json
  tools/check_manifest_producibility.py --config canary.example.yaml \\
      --manifest models/nissan-xterra-2008/isolation_forest/model.manifest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_manifest_producibility",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, required=True, help="canary.yaml to scope by.")
    parser.add_argument("--profile", default=None, help="Vehicle profile slug.")
    parser.add_argument(
        "--manifest",
        type=Path,
        action="append",
        default=[],
        help="Manifest to check (repeatable). Default: every "
        "<models_dir>/**/model.manifest.json of the scoped config.",
    )
    parser.add_argument("--json", action="store_true", help="Emit a JSON report.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    sys.path.insert(0, str(REPO_ROOT))
    from src.behavior.behavior_models import load_behavior_models
    from src.config import CanaryConfig
    from src.models.spec_producibility import (
        behavior_feature_conflicts,
        behavior_fingerprint_mismatches,
        behavior_fingerprint_provenance,
        build_producible_catalog,
        manifest_behavior_fingerprints,
        manifest_feature_columns,
        summarize,
        unproducible_feature_columns,
    )
    from src.vehicle.profiles import resolve_profile_for_tooling, scope_config

    config = CanaryConfig.from_yaml(args.config)
    if config.profiles.enabled:
        config = scope_config(config, resolve_profile_for_tooling(config, args.profile))
    manifests = [p.expanduser() for p in args.manifest] or sorted(
        config.daemon.models_dir.glob("**/model.manifest.json")
    )
    if not manifests:
        print(f"no model.manifest.json under {config.daemon.models_dir}", file=sys.stderr)
        return 2

    catalog = build_producible_catalog(config)
    behavior_path = config.daemon.behavior_models
    behavior_models = load_behavior_models(behavior_path) if behavior_path.exists() else None
    report: dict[str, Any] = {}
    status = 0
    for path in manifests:
        try:
            manifest = json.loads(path.read_text())
            columns = manifest_feature_columns(manifest)
            recorded = manifest_behavior_fingerprints(manifest)
        except (OSError, ValueError) as exc:
            report[str(path)] = {"error": str(exc)}
            status = max(status, 2)
            continue
        unproducible = unproducible_feature_columns(columns, catalog)
        report[str(path)] = summarize(unproducible, columns)
        # B03.4 — still produced, but governed by a behavior demotion: a
        # warning (exit status unchanged), never a silent meaning change.
        report[str(path)]["behavior_feature_conflicts"] = behavior_feature_conflicts(
            columns, behavior_models
        )
        # B03.4 — which artifact produced each consumed deviation column. A
        # refit since training (``mismatch``) fails like a missing column: the
        # detector would be fed a column that no longer means what it learned.
        # No record (``unknown``) is reported, not judged.
        provenance = behavior_fingerprint_provenance(columns, recorded, behavior_models)
        report[str(path)]["behavior_fingerprint_provenance"] = provenance
        if unproducible or behavior_fingerprint_mismatches(provenance):
            status = max(status, 1)

    if args.json:
        print(json.dumps(report, indent=2))
        return status
    for path, entry in report.items():
        assert isinstance(entry, dict)
        if "error" in entry:
            print(f"ERROR {path}: {entry['error']}")
            continue
        count = entry["unproducible_count"]
        provenance = entry["behavior_fingerprint_provenance"]
        mismatched = behavior_fingerprint_mismatches(provenance)
        verdict = "OK   " if not count and not mismatched else "FAIL "
        refit = (
            f", {len(mismatched)} behavior artifact(s) refit since training" if mismatched else ""
        )
        print(
            f"{verdict}{path}: {count}/{entry['expected_count']} unproducible "
            f"({entry['unproducible_fraction']:.1%}){refit}"
        )
        for base, detail in entry["unproducible_by_base"].items():
            print(f"  {base}: {len(detail['columns'])} column(s) — {detail['reason']}")
        for column, reason in entry["behavior_feature_conflicts"].items():
            print(f"  WARN {column}: {reason}")
        for name, detail in provenance.items():
            label = {"mismatch": "FAIL", "unknown": "UNKNOWN", "match": "ok", "absent": "FAIL"}[
                detail["status"]
            ]
            print(f"  {label} behavior:{name}: {detail['reason']}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
