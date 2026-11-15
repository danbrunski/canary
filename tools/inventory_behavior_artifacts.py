#!/usr/bin/env python3
"""Inventory the behavior artifact set, its policy and its dependents (B03.1).

Offline and read-only: reads the profile-scoped ``behavior_models.yaml``, the
``behavior_policy.yaml`` / ``behavior_validation.yaml`` / tune registry /
accepted baselines beside it, and every ``model.manifest.json`` under the
scoped models dir. It never contacts the Pi or the daemon, so the deployed
identity is ``unknown`` unless you pass a ``GET /models/behavior`` payload you
saved yourself (``--deployed-snapshot``) — read ``/status.vehicle_state``
first when you take one.

Usage:
  tools/inventory_behavior_artifacts.py --config canary.dev.example.yaml \\
      --profile nissan-xterra-2008
  tools/inventory_behavior_artifacts.py --config canary.dev.example.yaml \\
      --profile nissan-xterra-2008 --deployed-snapshot /tmp/models_behavior.json --json
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

try:
    from _cli import add_config_profile_args, load_scoped  # type: ignore[import-not-found]
except ImportError:  # loaded as `tools.inventory_behavior_artifacts`: repo root on path
    from tools._cli import add_config_profile_args, load_scoped


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="inventory_behavior_artifacts",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_config_profile_args(parser)
    parser.add_argument(
        "--deployed-snapshot",
        type=Path,
        default=None,
        help="Saved GET /models/behavior JSON from the deployed daemon.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the full JSON inventory.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    from src.analysis.behavior_inventory import build_inventory

    config = load_scoped(args)
    slug = config.scoped_profile
    snapshot = json.loads(args.deployed_snapshot.read_text()) if args.deployed_snapshot else None
    inventory = build_inventory(
        config, config_path=args.config, profile_slug=slug, deployed_snapshot=snapshot
    )
    if args.json:
        print(json.dumps(inventory, indent=2, sort_keys=True))
        return 0

    artifact = inventory["local_artifact"]
    print(f"artifact {artifact['path']}  {artifact['sha256']}")
    print(f"policy   {inventory['policy']['path']}  {inventory['policy']['sha256']}")
    print(f"calibration in force: {inventory['calibration']['current_revision']}")
    for model in artifact["models"]:
        identity = model["artifact"]
        status = model["serving_status"]
        scope = model["validation_scope"]
        print(
            f"  {identity['name']:32s} v{identity['version']:<3} {status['lifecycle']:12s} "
            f"uses={','.join(status['permitted_uses']):28s} evidence={scope['verdict']}"
        )
    for entry in inventory["policy"]["unmatched_entries"]:
        print(f"  policy entry matches no artifact: {entry}")
    print(f"accepted-baseline overrides: {len(inventory['accepted_baseline_overrides'])}")
    for manifest in inventory["dependent_manifests"]:
        if "error" in manifest:
            print(f"  ERROR {manifest['path']}: {manifest['error']}")
            continue
        consumed = manifest["behavior_models_consumed"] or ["none"]
        print(f"  {manifest['path']}: deviation columns from {', '.join(consumed)}")
        for column, reason in manifest["behavior_feature_conflicts"].items():
            print(f"    WARN {column}: {reason}")
    deployed = inventory["deployed"]
    print(
        f"deployed: {deployed['state']}"
        + (f" — {deployed['reason']}" if "reason" in deployed else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
