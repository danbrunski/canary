#!/usr/bin/env python3
"""CLI for the tuning gates — delivery error, A/B comparison, noise floor.

The analyses this wraps refuse the unsafe forms that produced three retracted
findings in the 2026-08 campaign (closed-loop dilution, DFCO counted as
enrichment, band-composition bias, gridding on calculated load). See
``src/analysis/tuning.py``; this script is argument parsing and text tables.

Usage:

    .venv/bin/python scripts/tuning_report.py delivery-error \\
        --session _tuning-2026-08-05T142720

    .venv/bin/python scripts/tuning_report.py compare \\
        --session A --against B --value obd:TIMING_ADVANCE

    .venv/bin/python scripts/tuning_report.py null-run \\
        --session A --against B --value obd:TIMING_ADVANCE --channel timing_advance_deg
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

try:
    from _cli import add_common_args, load_config  # type: ignore[import-not-found]
except ImportError:  # loaded as `scripts.tuning_report`
    from scripts._cli import add_common_args, load_config

from src.analysis.noise_floor import load_noise_floor, record_null_run, save_noise_floor
from src.analysis.tuning import GateError, compare_sessions, delivery_error
from src.analysis.tuning_context import TuningContext
from src.config import CanaryConfig
from src.vehicle.profiles import resolve_profile_for_tooling, scope_config


def _scoped(args: argparse.Namespace) -> CanaryConfig:
    config = load_config(args)
    profile = resolve_profile_for_tooling(config, getattr(args, "profile", None))
    return scope_config(config, profile)


def _session_dir(config: CanaryConfig, session: str) -> Path:
    path = Path(session)
    return path if path.exists() else Path(config.collection.storage_path) / session


def _run_delivery_error(args: argparse.Namespace) -> int:
    config = _scoped(args)
    context = TuningContext.from_config(config)
    if context.calibration is None:
        raise GateError(f"no afr_calibration.yaml found ({config.daemon.afr_calibration})")
    if context.rpm_bands is None:
        raise GateError("no mode01_wot.rpm_bands in this vehicle's tuning_providers.yaml")
    result = delivery_error(
        _session_dir(config, args.session),
        declaration=context.lambda_delivery,
        calibration=context.calibration,
        measured_spec=args.measured,
        min_load=context.resolve_min_load(args.min_load),
        revisions=context.registry,
        mode01_exclusion_note=context.mode01_exclusion_note,
        rpm_bands=context.rpm_bands,
    )
    print(f"delivery error — measured from {result.measured_spec}")
    pairing = result.pairing
    print(
        f"  paired by acquisition time within {pairing['max_separation_s'] * 1000:g} ms: "
        f"target {pairing['target_source']}, measured {pairing['measured_source']}"
    )
    if not pairing["transport_delay_modelled"]:
        print("  transport delay not modelled on this route (pairs as acquired)")
    sensitivity = result.delay_sensitivity
    if sensitivity is not None:
        ends = ", ".join(
            f"{row['delay_s']:g} s: n={row['n']} "
            + ("-" if row["band_averaged_pct"] is None else f"{row['band_averaged_pct']:+.2f} %")
            for row in sensitivity["at"]
        )
        print(f"  across the delay bracket: {ends}")
        if not sensitivity["stable"]:
            print(f"  UNSTABLE: {sensitivity['reason']}")
    if result.n == 0:
        print("  no samples survived the gates:")
        for gate, count in result.gates["filtered"].items():
            print(f"    {gate}: -{count}")
        return 1
    print(result.bands.round(3).to_string(index=False))
    print(
        f"\n  band-averaged {result.band_averaged_pct:+.2f} %   pooled {result.pooled_pct:+.2f} %"
    )
    print(f"  n={result.n}, rpm {result.rpm_window[0]:.0f}-{result.rpm_window[1]:.0f}")
    print(f"  gates: started {result.gates['started']}", end="")
    for gate, count in result.gates["filtered"].items():
        print(f", {gate} -{count}", end="")
    print()
    if result.pooled_pct and abs(result.pooled_pct - result.band_averaged_pct) > 1.0:
        print(
            "  NOTE: pooled and band-averaged disagree by >1 point, which means rpm\n"
            "        coverage is uneven. Trust the band-averaged figure."
        )
    return 0


def _run_compare(args: argparse.Namespace) -> int:
    config = _scoped(args)
    # R05.1: the profile's floor, as every other reader resolves it — this CLI
    # used to look beside the session store instead.
    context = TuningContext.from_config(config)
    floor = context.noise_floor
    if floor is None and args.floor_channel:
        floor = load_noise_floor(Path(args.floor_file)) if args.floor_file else None
    result = compare_sessions(
        _session_dir(config, args.session),
        _session_dir(config, args.against),
        value=args.value,
        axes=tuple(args.axes),
        unsafe_axes=args.unsafe_axes,
        noise_floor=floor if args.floor_channel else None,
        floor_channel=args.floor_channel,
        conditions=args.conditions,
        revisions=context.registry,
    )
    sd = (
        floor.sd_for(args.floor_channel)
        if (floor and args.floor_channel and result.floor_refusal is None)
        else None
    )
    print(f"{args.value}: {args.session} -> {args.against}, gridded on {', '.join(result.axes)}")
    if result.n_cells == 0:
        print("  no cells matched in both sessions at the minimum sample count")
        return 1
    print(result.cells.round(3).to_string(index=False))
    print(f"\n  mean delta {result.mean_delta:+.3f} over {result.n_cells} cells")
    if result.floor_refusal is not None:
        print(f"  noise floor refused: {result.floor_refusal['reason']}")
    elif sd is None:
        print("  noise floor: unavailable — deltas carry no significance verdict")
    else:
        print(
            f"  noise floor: sd {sd:.3f} measured under [{result.floor_conditions}]; "
            "cells under 2 sigma are not distinguishable. A floor is only valid for the "
            "conditions it was measured under."
        )
    return 0


def _run_null_run(args: argparse.Namespace) -> int:
    """Measure a floor from two same-tune captures and persist it."""
    config = _scoped(args)
    context = TuningContext.from_config(config)
    result = compare_sessions(
        _session_dir(config, args.session),
        _session_dir(config, args.against),
        value=args.value,
        axes=tuple(args.axes),
        unsafe_axes=args.unsafe_axes,
        revisions=context.registry,
    )
    if result.n_cells < 2:
        print(f"only {result.n_cells} matched cell(s) — need at least 2 to have a spread")
        return 1
    floor = record_null_run(
        {args.channel: result.cells["delta"]},
        sessions=(args.session, args.against),
        units={args.channel: args.unit},
        conditions=args.conditions,
        note=args.note,
    )
    out = Path(args.out)
    save_noise_floor(out, floor)
    entry = floor.channels[args.channel]
    print(f"wrote {out}")
    print(
        f"  {args.channel}: sd {entry.sd:.3f} {entry.unit}, mean {entry.mean:+.3f}, "
        f"{entry.n_cells} cells"
    )
    print("  a floor is only valid for the conditions it was measured under")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser)
    parser.add_argument("--profile", default=None, help="Vehicle profile slug.")
    sub = parser.add_subparsers(dest="command", required=True)

    de = sub.add_parser("delivery-error", help="Measured-vs-commanded AFR at enrichment.")
    de.add_argument("--session", required=True)
    de.add_argument(
        "--measured",
        default="obd:O2_WR_LAMBDA_S1",
        help="Measured-AFR source. The ECM lambda and the A/F voltage paths disagree "
        "by ~3.8%% at WOT, so this is explicit rather than defaulted silently.",
    )
    de.add_argument(
        "--min-load",
        type=float,
        default=None,
        help="Engine-load floor, %%. Default: the profile's mode01_wot.load_floor_pct.",
    )
    de.set_defaults(func=_run_delivery_error)

    cmp_ = sub.add_parser("compare", help="A/B two sessions on a matched grid.")
    cmp_.add_argument("--session", required=True)
    cmp_.add_argument("--against", required=True)
    cmp_.add_argument("--value", required=True)
    cmp_.add_argument("--axes", nargs="+", default=["obd:RPM", "obd:MAF"])
    cmp_.add_argument("--floor-channel", default=None, help="Noise-floor channel to quote.")
    cmp_.add_argument("--floor-file", default=None)
    cmp_.add_argument(
        "--conditions",
        default=None,
        help="Regime this comparison was made under; required with --floor-channel. "
        "The floor is refused unless it was measured under exactly these conditions.",
    )
    cmp_.add_argument("--unsafe-axes", action="store_true", help="Override the axis gate.")
    cmp_.set_defaults(func=_run_compare)

    nr = sub.add_parser("null-run", help="Measure a noise floor from two same-tune captures.")
    nr.add_argument("--session", required=True)
    nr.add_argument("--against", required=True)
    nr.add_argument("--value", required=True)
    nr.add_argument("--channel", required=True, help="Floor key, e.g. timing_advance_deg.")
    nr.add_argument("--unit", default="unknown")
    nr.add_argument(
        "--conditions",
        required=True,
        help=(
            "Operating regime this floor was measured under, e.g. "
            "'WOT, throttle>=85, 3000-5000 rpm'. Required: the same null pair gives "
            "0.48 deg of spark spread at WOT and 7.7 deg across all conditions, so a "
            "floor without its regime is not interpretable."
        ),
    )
    nr.add_argument("--axes", nargs="+", default=["obd:RPM", "obd:MAF"])
    nr.add_argument("--out", required=True)
    nr.add_argument("--note", default="")
    nr.add_argument("--unsafe-axes", action="store_true")
    nr.set_defaults(func=_run_null_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except GateError as exc:
        print(f"gate refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
