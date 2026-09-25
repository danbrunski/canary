#!/usr/bin/env python3
"""One-time migration: nest existing single-vehicle data under a profile slug.

CANary used to keep one vehicle's data in a flat tree. With multiple vehicle
profiles, every vehicle-specific path moves under ``<base>/<slug>/`` (see
``src/profiles.py``). This script moves the *existing* data into that layout
for one profile (the ``vehicle:`` block's default, or ``--slug``) and registers
it in ``profiles.yaml`` so the daemon's VIN-resolution finds it.

Dry-run by default — it prints the planned moves and writes nothing. Pass
``--apply`` to execute. Idempotent: a move whose destination already exists, or
whose source is missing, is skipped.

Usage:

    .venv/bin/python scripts/migrate_to_profiles.py                 # preview
    .venv/bin/python scripts/migrate_to_profiles.py --apply
    .venv/bin/python scripts/migrate_to_profiles.py --name "2008 Xterra" \\
        --vin JN8AZ18U18W000000 --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from _cli import (
    add_common_args,
    add_log_level_arg,
    load_config,
)

from src.config import CanaryConfig
from src.log import configure_logging
from src.profiles import (
    VehicleProfile,
    load_registry,
    resolve_profile_for_tooling,
    save_registry,
    scope_config,
)
from src.session import SessionKind, session_kind

# Session directories are ``YYYY-MM-DDTHHMMSS`` (UTC). The ``_``-prefixed dirs
# (``_archive``, ``_replay-*``) stay at the top level — they're excluded from
# training discovery and aren't vehicle-partitioned in v1.


@dataclass(frozen=True)
class Move:
    src: Path
    dst: Path


def _plan(config: CanaryConfig, profile: VehicleProfile) -> list[Move]:
    slug = profile.slug
    scoped = scope_config(config, profile)
    moves: list[Move] = []

    # storage_path: move each session timestamp dir into <storage_path>/<slug>/.
    base = config.collection.storage_path
    scoped_sessions = scoped.collection.storage_path
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.name == slug or child.name.startswith("_") or not child.is_dir():
                continue
            if session_kind(child.name) is SessionKind.LIVE:
                moves.append(Move(child, scoped_sessions / child.name))

    # Directory fields whose *contents* move under the slug subdir.
    for unscoped, scoped_dir in (
        (config.daemon.models_dir, scoped.daemon.models_dir),
        (config.daemon.anomalies_dir, scoped.daemon.anomalies_dir),
        (config.daemon.jobs_dir, scoped.daemon.jobs_dir),
    ):
        if unscoped.is_dir():
            for child in sorted(unscoped.iterdir()):
                if child.name == slug or child.name.startswith("_"):
                    continue
                moves.append(Move(child, scoped_dir / child.name))

    # File fields move to their scoped location.
    for unscoped_file, scoped_file in (
        (config.daemon.behavior_models, scoped.daemon.behavior_models),
        (config.daemon.training_ledger, scoped.daemon.training_ledger),
        (config.daemon.feedback_log, scoped.daemon.feedback_log),
        (config.daemon.tuning_config, scoped.daemon.tuning_config),
        (config.daemon.tuning_audit, scoped.daemon.tuning_audit),
        (config.daemon.trip_state, scoped.daemon.trip_state),
        (config.daemon.audit_log, scoped.daemon.audit_log),
    ):
        if unscoped_file.is_file():
            moves.append(Move(unscoped_file, scoped_file))

    return moves


def _register(
    config: CanaryConfig, profile: VehicleProfile, name: str | None, vin: str | None
) -> None:
    if name is not None:
        profile.name = name
    if vin is not None:
        profile.vin = vin
    profiles = load_registry(config.profiles.registry_path)
    existing = next((p for p in profiles if p.slug == profile.slug), None)
    if existing is None:
        profiles.append(profile)
    else:
        if name is not None:
            existing.name = name
        if vin is not None:
            existing.vin = vin
    save_registry(config.profiles.registry_path, profiles)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="migrate_to_profiles",
        description="Nest existing single-vehicle data under a profile slug.",
    )
    add_common_args(parser)
    parser.add_argument(
        "--slug",
        type=str,
        default="",
        help="Target profile slug. Default: derived from the vehicle: block.",
    )
    parser.add_argument("--name", type=str, default=None, help="Display name to record.")
    parser.add_argument("--vin", type=str, default=None, help="VIN to record for this profile.")
    parser.add_argument(
        "--apply", action="store_true", help="Execute the moves (default: dry-run)."
    )
    add_log_level_arg(parser)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = load_config(args)
    profile = resolve_profile_for_tooling(config, args.slug or None)
    slug = profile.slug

    moves = _plan(config, profile)
    print(f"profile slug: {slug}")
    print(f"registry:     {config.profiles.registry_path}")
    if not moves:
        print("nothing to move (already migrated or no data on disk).")
    for move in moves:
        skip = " [skip: dst exists]" if move.dst.exists() else ""
        print(f"  MOVE: {move.src}  →  {move.dst}{skip}")

    if not args.apply:
        print("\ndry-run; pass --apply to execute and register the profile.")
        return 0

    applied = 0
    for move in moves:
        if move.dst.exists():
            continue
        move.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(move.src), str(move.dst))
        applied += 1
    _register(config, profile, args.name, args.vin)
    print(f"\napplied {applied} move(s); registered profile {slug!r}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
