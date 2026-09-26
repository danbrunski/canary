#!/usr/bin/env python3
"""Resolve sessions' IMU frames and write ``imu_frame.json`` (H02.4).

For each session: raw ``imu:ACCEL_X/Y/Z`` → ``imu:ACCEL_LON/LAT`` (gravity
removed) and ``imu:SF_LON/LAT/VERT`` (specific force, gravity kept), steering
by the best speed the session has (``0x2D1`` broadcast, then Mode 01, then
GPS) and GPS course. The sidecar records the frame of record: the rotation,
the quiescent windows its pitch reference came from, and the residual pitch
offset it leaves for the dyno to measure. A refusal is recorded and printed
with its cause. See ``src/preprocessing/imu_session.py``.

Laptop only: pull the session first (AGENTS.md: no analysis on the Pi while
it collects). Captures are named directly (``_capture-tuning-...``).

Usage:

    .venv/bin/python scripts/resolve_imu_frame.py ~/canary-data/2026-09-27T101500
    .venv/bin/python scripts/resolve_imu_frame.py <session> --replace
    .venv/bin/python scripts/resolve_imu_frame.py <session> --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

try:
    from _cli import add_log_level_arg
except ImportError:  # loaded as `scripts.resolve_imu_frame`
    from scripts._cli import add_log_level_arg  # type: ignore[no-redef]

from src.log import configure_logging
from src.preprocessing.imu_session import (
    DEFAULT_QUIESCENT_SECONDS,
    ImuFrameOfRecordError,
    ImuFrameRefusal,
    resolve_session_imu_frame,
)

log = logging.getLogger(__name__)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="resolve_imu_frame",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("sessions", nargs="+", type=Path, help="Session directories.")
    parser.add_argument(
        "--quiescent-seconds",
        type=float,
        default=DEFAULT_QUIESCENT_SECONDS,
        help="How long speed and course must hold steady to count as quiescent.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace a resolved frame whose reference differs (invalidates any dyno "
        "pitch offset measured against the old one).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Resolve but write nothing.")
    add_log_level_arg(parser)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.log_level)
    missing = [str(path) for path in args.sessions if not path.is_dir()]
    if missing:
        log.error("not a session directory: %s", ", ".join(missing))
        return 1
    errors = 0
    for session_dir in args.sessions:
        try:
            result = resolve_session_imu_frame(
                session_dir,
                quiescent_seconds=args.quiescent_seconds,
                write=not args.dry_run,
                replace=args.replace,
            )
        except ImuFrameOfRecordError as exc:
            log.error("%s: %s", session_dir.name, exc)
            errors += 1
            continue
        if isinstance(result, ImuFrameRefusal):
            log.warning("%s: refused (%s): %s", session_dir.name, result.kind, result.reason)
            continue
        sf = result.sidecar.specific_force
        inputs = result.sidecar.inputs
        assert sf is not None and inputs is not None
        reference = sf.pitch_reference
        log.info(
            "%s: resolved reference %s from %d quiescent window(s), %.0f s, "
            "%.0f %% stationary; speed %s; |g| %.4f; tilt from chip Z %.2f deg",
            session_dir.name,
            sf.reference_id,
            len(reference.windows),
            reference.quiescent_seconds,
            100.0 * reference.stationary_fraction,
            inputs.speed_source,
            reference.gravity_magnitude_g,
            reference.tilt_from_body_z_deg,
        )
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
