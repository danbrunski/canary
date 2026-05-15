#!/usr/bin/env python3
"""Walk through a tag-protocol step-by-step, hitting Enter to tag each event.

Designed for in-vehicle use where typing `scripts/tag.py "label"` for every
event is impractical — driver-seat ergonomics. Run this in one terminal
alongside `scripts/record.py` in another. The script auto-finds the latest
session and writes each tag with the correct label as soon as you press
Enter.

Usage:
  scripts/guided_session.py                  # uses the 'rev' protocol
  scripts/guided_session.py --protocol rev
  scripts/guided_session.py --protocol nt510 # NT510 active-test session
  scripts/guided_session.py --session data/sessions/<timestamp>

At each step, press Enter to tag and advance, or type `s` (then Enter) to skip
the step without writing a tag — useful when a protocol lists an action your
tool/vehicle doesn't have. Quit with Ctrl-C at any time; tags written so far
stay in tags.jsonl.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.collection.tags import find_latest_session, write_tag


@dataclass(frozen=True)
class Step:
    """One labeled event in a protocol.

    `advisory_duration_s` is just shown to the user as a hint — the script
    doesn't enforce timing. The user controls pace by when they press Enter.
    """

    label: str
    description: str
    advisory_duration_s: int | None = None


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

# Rev protocol: seated, parked, engine off → cold idle → warm idle → blips
# (2k/3k/4k) with idle bracketing → 2500 sustain → engine off again. Each
# step is one tag; the implicit window for each is [tag time, next tag time]
# so build_windows produces clean per-phase segments.
PROTOCOL_REV: list[Step] = [
    Step("key_on_engine_off", "Key ON, engine OFF. Just sit.", 30),
    Step("engine_start", "Crank the engine now. Hit Enter the moment it cranks.", None),
    Step("idle_cold", "Engine running, settling. Just idle.", 60),
    Step("idle_warm", "Engine warmer now. Keep idling.", 90),
    Step("blip_2k_start", "About to blip to ~2000 RPM. Hit Enter just BEFORE you blip.", None),
    Step("blip_2k_end", "Blip done. Hit Enter the moment you released the throttle.", None),
    Step("idle_recover_a", "Let it settle at idle.", 15),
    Step("blip_3k_start", "About to blip to ~3000 RPM.", None),
    Step("blip_3k_end", "Throttle released.", None),
    Step("idle_recover_b", "Idle again.", 15),
    Step("sustain_2500_start", "About to hold steady ~2500 RPM for ~15s.", None),
    Step("sustain_2500_end", "Releasing throttle.", None),
    Step("idle_recover_c", "Idle again.", 15),
    Step("blip_4k_start", "About to blip to ~4000 RPM.", None),
    Step("blip_4k_end", "Throttle released.", None),
    Step("idle_recover_d", "Final idle.", 15),
    Step("engine_off_key_on", "Kill engine, leave key ON.", 30),
]

# NT510-active protocol: drive the Foxwell NT510 (or similar) through its
# read-only inventory + ABS live data + ECU/ABS actuator tests, tagging each
# action so the byte-vs-tag differential can find the signal(s) each test
# moves. Many steps are tool/vehicle-specific — type `s` + Enter to skip any
# your NT510 doesn't offer. Run record.py CAN-only (no --obd-port) so there's
# no ISO-TP contention between the NT510 and the ELM327.
PROTOCOL_NT510: list[Step] = [
    Step("nt510_module_scan_start", "NT510: start the all-systems / module scan.", None),
    Step("nt510_module_scan_end", "Module scan done — note which modules answered.", None),
    Step(
        "engine_start",
        "Start the engine (skip with 's' if it's already running). Enter once it idles. "
        "Needed for the steering test — the Xterra's power steering is engine-driven.",
        None,
    ),
    Step("idle_settle", "Let it idle and settle.", 30),
    Step("nt510_abs_livedata_start", "NT510: open ABS (chassis) live data / data stream.", None),
    Step("steer_full_left", "Steering wheel FULL LEFT, hold a moment.", 5),
    Step("steer_full_right", "Now FULL RIGHT, hold.", 5),
    Step("steer_center", "Back to CENTER (wheels straight).", 3),
    Step("brake_press", "Brake pedal: a couple of firm presses, then a light one.", 10),
    Step("brake_release", "Foot fully off the brake.", 3),
    Step("nt510_abs_livedata_end", "NT510: close the ABS live-data screen.", None),
    Step("nt510_fan_test_on", "NT510: cooling-fan actuator test — fan ON.", None),
    Step("nt510_fan_test_off", "Cooling-fan test OFF / finished.", None),
    Step(
        "nt510_idle_air_up",
        "NT510: idle-air / target-idle test — command idle RPM UP, hold.",
        10,
    ),
    Step("nt510_idle_air_restore", "Restore normal idle.", 5),
    Step("nt510_evap_purge_on", "NT510: EVAP purge-solenoid (canister purge) test — ON.", 8),
    Step("nt510_evap_purge_off", "EVAP purge test OFF.", None),
    Step(
        "nt510_power_balance_start",
        "NT510: power-balance / injector-cutoff test — START (it cycles the cylinders).",
        None,
    ),
    Step("nt510_power_balance_end", "Power-balance test finished.", None),
    Step(
        "nt510_fuel_pump_cut",
        "NT510: fuel-pump relay test — CUT the pump (engine stalls in a few s). "
        "Skip with 's' if you'd rather not stall it.",
        None,
    ),
    Step("nt510_fuel_pump_restore", "Pump restored — restart the engine if it stalled.", None),
    Step(
        "nt510_abs_actuator_start",
        "NT510: ABS actuator / pump-motor activation test — START.",
        None,
    ),
    Step("nt510_abs_actuator_end", "ABS actuator test finished.", None),
    Step("nt510_session_end", "Back the NT510 out to the main menu. Done — stop record.py.", None),
]

# Drive protocol: a full operating-envelope capture for ML training + behavior-
# model fitting (see docs/data-collection-plan.md). It's meant to be walked
# loosely — the route decides which steps fire; type `s` + Enter to skip any
# that don't apply, and just re-hit a step's label by narrating if a phase
# recurs (e.g. several stops at lights). Hit Enter at the *transition* into each
# phase; the window for a phase is [its tag, the next tag].
PROTOCOL_DRIVE: list[Step] = [
    Step("cold_start", "Engine just started — COLD. Enter at startup. (skip 's' if warm)", None),
    Step("idle_warm", "Idling, warming up. Sit until coolant is at operating temp.", 300),
    Step("pulling_away", "About to drive off — Enter as you start moving.", None),
    Step("city_stop_go", "City / surface streets — stop-and-go, low-to-moderate load.", 600),
    Step("stopped_engine_on", "Stopped (light/sign), engine running, foot on brake.", 20),
    Step("hard_accel", "A firm acceleration from a stop.", None),
    Step("steady_cruise_light", "Steady cruise, light load (~30–45 mph, flat).", 60),
    Step("highway_onramp", "Merging onto the highway — sustained acceleration.", None),
    Step("highway_cruise", "Highway cruise, ~65–75 mph, steady throttle.", 900),
    Step("coasting_dfco", "Coasting down, foot FULLY off the throttle (fuel-cutoff).", 15),
    Step("wot_pull", "Wide-open-throttle pull through a gear — where safe & legal.", None),
    Step("uphill_load", "Sustained uphill / loaded cruise. (skip 's' if no hill)", 30),
    Step("four_lo", "4WD low engaged, crawling off-road. (skip 's' if not off-roading)", 60),
    Step("ac_on", "A/C turned ON. (skip 's' if not testing A/C)", 30),
    Step("ac_off", "A/C turned OFF.", None),
    Step("returning_idle", "Back to idle — parked, in neutral, engine running.", 20),
    Step("engine_off_key_on", "Engine OFF, key left ON. Done — stop record.py.", 15),
]

# Binding-targeted drive: focused on the high-variance UNBOUND bytes flagged in
# the corpus report (0x285:7, 0x551:1, 0x2A0:2, 0x2DE:4-5, 0x2A5 pairs, 0x794
# pair). Each step isolates ONE physical variable so the byte-vs-tag
# differential can attribute each unbound byte to a single cause. Order matters:
# brake-then-release, then HVAC sweep, THEN motion — so the brake/HVAC bytes
# get a clean stationary window before vehicle-speed-correlated noise enters.
# Pair with `record.py --vehicle-state driving` in another terminal; do NOT
# pass --obd-port for this protocol — keep the ISO-TP bus quiet so we can see
# the broadcast bytes change in response to physical actions, not OBD polls.
PROTOCOL_BINDING: list[Step] = [
    Step("key_on_engine_off", "Key ON, engine OFF. Sit. (Baseline before crank.)", 5),
    Step("engine_start", "Crank the engine. Enter the moment it cranks.", None),
    Step(
        "idle_park_neutral_brake",
        "Parked, in neutral, foot FIRMLY on the brake. Sit and breathe.",
        10,
    ),
    Step(
        "brake_release",
        "Foot fully OFF the brake (still parked, neutral). Hold.",
        10,
    ),
    Step(
        "brake_press_pump",
        "A few firm brake PRESSES, then one light one. Foot back off to finish.",
        10,
    ),
    Step("hvac_off_baseline", "All HVAC OFF: fan OFF, A/C OFF, vents shut. Sit.", 10),
    Step("hvac_fan_low", "Fan ON, LOWEST speed. A/C still off.", 10),
    Step("hvac_fan_high", "Fan to MAXIMUM. A/C still off.", 10),
    Step("hvac_ac_on", "A/C compressor ON, fan still high.", 15),
    Step("hvac_ac_off_fan_off", "A/C OFF, fan OFF — back to silence.", 10),
    Step("pulling_away", "About to drive off. Enter as the wheels start moving.", None),
    Step("accel_to_25", "Smooth accel from 0 → ~25 mph. Enter as you start.", None),
    Step("steady_25_flat", "Holding steady ~25 mph, flat road, light throttle.", 60),
    Step(
        "coast_dfco",
        "Foot FULLY off the throttle, decelerating (still rolling — no brake yet).",
        10,
    ),
    Step("brake_to_stop", "Now on the brake, coming to a full stop.", None),
    Step("accel_to_40", "From the stop, FIRMER accel up to ~40 mph.", None),
    Step("steady_40", "Holding ~40 mph.", 30),
    Step("right_turn_sharp", "A sharp RIGHT turn — Enter as you start turning the wheel.", None),
    Step("straight_recover_a", "Going straight again — wheel centered.", 10),
    Step("left_turn_sharp", "A sharp LEFT turn.", None),
    Step("straight_recover_b", "Going straight again.", 10),
    Step(
        "bumpy_section",
        "Driving over rough/bumpy pavement (skip 's' if no such road nearby).",
        15,
    ),
    Step(
        "uphill_section",
        "Sustained uphill climb, steady throttle (skip 's' if no hill).",
        30,
    ),
    Step("final_idle", "Back to parked, in neutral, idling.", 15),
    Step("engine_off_key_on", "Kill engine, leave key ON. Done — stop record.py.", 5),
]

PROTOCOLS: dict[str, list[Step]] = {
    "rev": PROTOCOL_REV,
    "nt510": PROTOCOL_NT510,
    "drive": PROTOCOL_DRIVE,
    "binding": PROTOCOL_BINDING,
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _format_header(step_num: int, total: int, label: str) -> str:
    bar = "=" * 78
    title = f" GUIDED PROTOCOL                                   Step {step_num}/{total}"
    return f"\n{bar}\n{title}\n{bar}\n\n  Next:  {label}"


_SKIP_TOKENS = {"s", "skip"}


def _format_prompt(step: Step) -> str:
    lines = [f"\n  {step.description}"]
    if step.advisory_duration_s is not None:
        lines.append(f"  (Suggested ~{step.advisory_duration_s}s before advancing.)")
    lines.append("\n  >>> Enter to tag and advance, or 's' + Enter to skip: ")
    return "\n".join(lines)


def run_step(
    step: Step,
    step_num: int,
    total: int,
    session: Path,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    write_tag_fn: Callable[..., dict[str, Any]] = write_tag,
) -> dict[str, Any] | None:
    """Display one step, await Enter (or a skip), write the tag.

    Returns the tag record, or ``None`` if the user typed ``s``/``skip`` to
    skip the step without writing a tag.
    """
    output_fn(_format_header(step_num, total, step.label))
    response = input_fn(_format_prompt(step))
    if response.strip().lower() in _SKIP_TOKENS:
        output_fn(f"  ↷ skipped '{step.label}' (no tag written)")
        return None
    record = write_tag_fn(session, step.label, kind="event")
    ts = record["t"]
    output_fn(
        f"  ✓ Tagged '{step.label}' at "
        f"{time.strftime('%H:%M:%S', time.localtime(ts))}.{int((ts % 1) * 1000):03d}"
    )
    return record


def run_protocol(
    steps: list[Step],
    session: Path,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    write_tag_fn: Callable[..., dict[str, Any]] = write_tag,
) -> list[dict[str, Any]]:
    """Walk every step in order, returning the list of tag records written.

    Skipped steps (the user typed ``s``) contribute no record.
    """
    written: list[dict[str, Any]] = []
    total = len(steps)
    for i, step in enumerate(steps, start=1):
        record = run_step(
            step,
            i,
            total,
            session,
            input_fn=input_fn,
            output_fn=output_fn,
            write_tag_fn=write_tag_fn,
        )
        if record is not None:
            written.append(record)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="guided_session",
        description=(
            "Walk through a tag protocol step-by-step; hit Enter to fire each "
            "tag while you do the action. Pair with `scripts/record.py` "
            "running in another terminal."
        ),
    )
    parser.add_argument(
        "--protocol",
        default="rev",
        choices=sorted(PROTOCOLS.keys()),
        help="Which protocol to run.",
    )
    parser.add_argument(
        "--session",
        type=Path,
        default=None,
        help="Session directory. Defaults to the latest under --sessions-root.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path("data/sessions"),
        help="Where to look for the latest session. Default: ./data/sessions.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    if args.session is not None:
        session = args.session
    else:
        try:
            session = find_latest_session(args.sessions_root)
        except (FileNotFoundError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            print(
                "Start `scripts/record.py` first; this script tags into its session.",
                file=sys.stderr,
            )
            return 1

    steps = PROTOCOLS[args.protocol]
    print(f"Tagging into session: {session}")
    print(f"Protocol: {args.protocol} ({len(steps)} steps)")
    print(
        "\nMake sure `scripts/record.py` is already running.\n"
        "Press Ctrl-C at any time to stop (tags so far are preserved)."
    )

    try:
        run_protocol(steps, session)
    except KeyboardInterrupt:
        print("\n\nInterrupted. Tags written so far are saved.")
        return 130
    except FileNotFoundError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1

    print("\n" + "=" * 78)
    print(" Protocol complete. Stop record.py when ready.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
