"""No capture profile changes what another one records (E09.1 verification, 2026-10-16).

The request plan is the union of every saved profile, so a profile switch needs
no restart, and the active profile gates it. Three things let a profile nobody
had armed reach the one that was:

- a memory read was planned over the union of every profile's members of its
  window, so ``twin`` widened ``tuning``'s HI DET read from one byte to three
  and ``tuning`` recorded ``mem:ECM_PEDAL_ENRICH_FLAGS``;
- the loop staggered its requests' first due times by plan *index*, so a
  ``twin`` window ``tuning`` never reads moved ``tuning``'s first DTC poll a
  slot, its Mode 01 round grid against its window grid, and RPM from 440 to
  410 reads in 120 s on the fake clock;
- the window controller counted members no profile in force selects as due,
  and opened windows that read nothing.

So the pin is the whole observable behaviour: the tracked catalog with and
without each other profile, and with a synthetic extra that widens the active
profile's windows, adds one it does not read and a Mode 01 PID it does not
poll, must leave ``default``'s and ``tuning``'s sent frames (with their fake
clock times), decoded channels and resolved plans identical — armed before the
loop starts, and armed by a switch mid-run.
"""

from __future__ import annotations

import dataclasses
import threading
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from src.collection import bus_budget, tuning_profile
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.capture_profiles import CaptureProfile, CaptureProfileCatalog
from src.collection.obd_collector import ObdReading
from src.collection.obd_registry import MODE01_REGISTRY
from src.collection.obd_scheduler import (
    CanObdSpec,
    ObdSchedulerMetrics,
    did_alias_table,
    narrow_memory_reads,
    profile_did_union,
)
from src.config import CanBusConfig, CollectionConfig
from src.decode.dids import (
    MEMORY_SOURCE,
    memory_channels,
    plan_memory_reads,
    select_memory_channels,
)
from src.yaml_load import load_yaml
from tests.capture_round_fakes import FakeClock, ScriptedEcu, install_clock
from tests.test_memory_window_burst import XTERRA_MODE06

CATALOG_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "nissan-xterra-2008" / "capture_profiles.yaml"
)
TRACKED = CaptureProfileCatalog.model_validate(load_yaml(CATALOG_PATH.read_text()))
DEFAULT = TRACKED.by_name("default")
TUNING = TRACKED.by_name("tuning")
assert DEFAULT is not None and TUNING is not None

#: Not in `default` or `tuning`: a PID neither polls.
EXTRA_PID = "ABSOLUTE_LOAD"
#: Members `tuning` does not select of windows it reads (flags_2841: the
#: pedal-enrichment and transient flags around its HI DET byte), and a
#: window it does not read at all (uprev_state).
EXTRA_MEMORY = (
    "ECM_FUEL_TRANSIENT_FLAGS",
    "ECM_PEDAL_ENRICH_FLAGS",
    "ECM_UPREV_MAGIC",
    "ECM_ACTIVE_MAP",
)


def _synthetic() -> CaptureProfile:
    return CaptureProfile.model_validate(
        {
            "name": "zz-synthetic",
            "mode01": {EXTRA_PID: 1.0, "RPM": 1.0},
            "mode22": ("ECM_MAF_GPS",),
            "memory": (*TUNING.memory, *EXTRA_MEMORY),
            "did_cycle_seconds": 0.25,
            "target_rate_hz": 8.0,
            "storage_prefix": "_capture-zz-synthetic",
        }
    )


def _catalogs(target: CaptureProfile) -> dict[str, tuple[CaptureProfile, ...]]:
    """The reference (`default` and the target alone), the reference plus each
    other tracked profile, and every tracked profile plus the synthetic one."""
    base = (DEFAULT,) if target.name == DEFAULT.name else (DEFAULT, target)
    others = [p for p in TRACKED.profiles if p.name not in {DEFAULT.name, target.name}]
    variants = {"reference": base}
    for other in others:
        variants[f"+{other.name}"] = (*base, other)
    variants["+all+synthetic"] = (*base, *others, _synthetic())
    return variants


def _collection() -> CollectionConfig:
    """The Pi's collection settings for the Xterra (as the burst test's)."""
    return CollectionConfig(
        obd_source="canable",
        obd_pids=tuple(DEFAULT.mode01),
        obd_dids=tuple(DEFAULT.mode22),
        obd_batch_max_pids=6,
        obd_did_batch_max_dids=1,
        obd_request_timeout_seconds=1.0,
        obd_poll_rate_hz=1,
        storage_path=Path("unused"),
        session_retention_days=30,
        storage_warning_mb=500,
    )


def _run(
    catalog: Sequence[CaptureProfile],
    active: CaptureProfile,
    seconds: float,
    *,
    switch_at: float | None,
    restart_at: float | None = None,
) -> dict[str, Any]:
    """Everything a profile's run shows, on a fresh fake clock: the requests
    sent and when, the decoded channels, and the last resolved plan.

    ``restart_at`` ends the loop there and starts a new one on the same bus
    and clock, as a collector reconnect does, with the capture still armed."""
    collection = _collection()
    with pytest.MonkeyPatch.context() as patch:
        clock = FakeClock()
        install_clock(patch, clock)
        spec = dataclasses.replace(
            CanObdSpec.from_config(
                collection,
                CanBusConfig(interface="virtual", channel="independence", bitrate=500000),
                catalog=catalog,
            ),
            calibration_id=None,
        )
        tuning_profile.configure_default(DEFAULT)
        tuning_profile.configure_did_aliases(
            did_alias_table(profile_did_union(collection, catalog))
        )

        def arm() -> None:
            tuning_profile.activate(active, source="test")
            if active.bus_budget_override is not None:
                bus_budget.set_override(active.bus_budget_override, source="test", ttl_seconds=None)

        tuning_profile.activate(DEFAULT, source="test")
        values = {pid: bytes([0x40]) * d.byte_length for pid, d in MODE01_REGISTRY.items()}
        values[0x0C] = b"\x0b\xb8"
        bus = ScriptedEcu(
            clock,
            latency=0.010,
            session_gated=True,
            values=values,
            dtc_responses={0x03: b"\x00", 0x07: b"\x00", 0x0A: b"\x00"},
            **XTERRA_MODE06,
        )
        start = clock.monotonic()
        if switch_at is None:
            arm()
        else:
            armed: list[bool] = []

            def switch(_msg: object) -> None:
                if not armed and clock.monotonic() - start >= switch_at:
                    armed.append(True)
                    arm()

            bus.on_send.append(switch)
        readings: list[ObdReading] = []
        metrics: list[ObdSchedulerMetrics] = []
        deadlines = [start + seconds]
        if restart_at is not None:
            deadlines.insert(0, start + restart_at)
        try:
            for deadline in deadlines:
                run_can_obd_loop(
                    bus,  # type: ignore[arg-type]
                    spec,
                    on_frame=lambda _m: None,
                    on_reading=readings.append,
                    stop_event=threading.Event(),
                    deadline_monotonic=deadline,
                    on_scheduler_metrics=metrics.append,
                    scheduler_metrics_interval_seconds=5.0,
                )
        finally:
            bus_budget.reset()
            tuning_profile.set_active(False, source="test")
            tuning_profile.configure_did_aliases({})
    assert metrics
    last = metrics[-1]
    did_profile = last.did_profile or {}
    return {
        "sent": [(round(at - start, 9), bytes(msg.data).hex()) for at, msg in bus.sent],
        "readings": Counter(r.pid for r in readings),
        "plan": last.resolved_plan,
        "cycle_plan": (did_profile.get("cycle") or {}).get("plan"),
    }


SCENARIOS = {
    # `default` armed at loop start: its first 60 s window and a second.
    ("default", None): 125.0,
    # `tuning` armed at loop start (a collector restart inside a capture) ...
    ("tuning", None): 30.0,
    # ... and by a switch mid-run, which replans every selected member.
    ("tuning", 10.0): 40.0,
}


@pytest.mark.parametrize(("name", "switch_at"), list(SCENARIOS), ids=lambda v: str(v))
def test_no_other_profile_changes_what_the_active_one_records(
    name: str, switch_at: float | None
) -> None:
    target = TRACKED.by_name(name)
    assert target is not None
    seconds = SCENARIOS[(name, switch_at)]
    runs = {
        label: _run(catalog, target, seconds, switch_at=switch_at)
        for label, catalog in _catalogs(target).items()
    }
    reference = runs.pop("reference")
    assert reference["sent"] and reference["readings"]
    for label, run in runs.items():
        for key in ("readings", "plan", "cycle_plan"):
            assert run[key] == reference[key], (label, key)
        # The frames last: on a mismatch, name the first one that moved.
        first = next(
            (
                (i, a, b)
                for i, (a, b) in enumerate(zip(reference["sent"], run["sent"], strict=False))
                if a != b
            ),
            None,
        )
        assert first is None and len(run["sent"]) == len(reference["sent"]), (label, first)


def test_a_wider_window_elsewhere_is_not_read_or_decoded_under_tuning() -> None:
    """The flags_2841 read `tuning` sends is its HI DET byte alone, whatever
    else the catalog's profiles select around it, and nothing it did not name
    is decoded."""
    run = _run((DEFAULT, TUNING, _synthetic()), TUNING, 10.0, switch_at=None)
    hi_det = next(c for c in memory_channels() if c.name == "ECM_HIGH_DET_FLAGS")
    address = (hi_det.address & 0xFFFFFFFF).to_bytes(4, "big").hex()
    reads = [d for _at, d in run["sent"] if d[2:4] == "23" and d[4:12] == address]
    assert reads and all(d[12:16] == "0001" for d in reads), reads
    decoded = {
        pid.split(":", 1)[1] for pid in run["readings"] if pid.startswith(f"{MEMORY_SOURCE}:")
    }
    assert decoded == {name.upper() for name in TUNING.memory}
    assert EXTRA_PID not in run["readings"]


def test_narrowing_cuts_a_window_to_the_selection_and_keeps_it_whole_otherwise() -> None:
    from src.collection.obd_scheduler import build_poll_requests

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="narrow", bitrate=500000),
        pids=("RPM",),
        memory=select_memory_channels(("ECM_HIGH_DET_FLAGS", "ECM_PEDAL_ENRICH_FLAGS")),
        dtc_poll_interval_seconds=0.0,
        service_poll_interval_seconds=0.0,
    )
    (planned,) = [r for r in build_poll_requests(None, spec) if r.kind == "memory"]
    assert planned.memory_read is not None and planned.memory_read.length == 3
    (narrowed,) = [r for r in narrow_memory_reads([planned], ("ECM_HIGH_DET_FLAGS",))]
    assert narrowed.memory_read is not None and narrowed.memory_window == planned.memory_read
    assert [c.name for c in narrowed.memory_read.channels] == ["ECM_HIGH_DET_FLAGS"]
    assert narrowed.memory_read.length == 1
    # Back to the whole window: a selection touching none of it, and no gate.
    (untouched,) = narrow_memory_reads([narrowed], ("ECM_RPM",))
    (ungated,) = narrow_memory_reads([narrowed], None)
    assert untouched.memory_read == ungated.memory_read == planned.memory_read


def test_a_collector_restart_inside_a_capture_resumes_it_within_one_cycle() -> None:
    """A bus reconnect builds a new loop with the capture still armed. Its
    first window was due ``obd_did_window_interval_seconds`` later (60 s on
    the Pi) whatever the profile's cycle, and the loop's own generation bump
    hid it from the generation-change path that would have corrected it: a
    minute of DIDs and memory lost per reconnect. Now the restart's first
    window opens within one cycle and reads every member `tuning` selects."""
    restart_at = 7.3
    run = _run((DEFAULT, TUNING), TUNING, restart_at + 2.0, switch_at=None, restart_at=restart_at)
    cycle = TUNING.did_cycle_seconds
    assert cycle is not None
    after = [(at - restart_at, d) for at, d in run["sent"] if at >= restart_at]
    # The reconnect's exit first (C04.7), then the first window's entry.
    entries = [at for at, d in after if d.startswith("0210c0")]
    assert after[0][1].startswith("021081") and entries and entries[0] < cycle, after[:3]
    window_end = next(at for at, d in after if d.startswith("021081") and at > entries[0])
    in_window = {d[4:8] for at, d in after if entries[0] < at < window_end and d[2:4] == "22"}
    wanted = {
        channel.removeprefix("DID_").lower() for channel in did_alias_table(TUNING.mode22).values()
    }
    assert in_window == wanted
    reads = {d[4:12] for at, d in after if entries[0] < at < window_end and d[2:4] == "23"}
    planned = {
        (read.address & 0xFFFFFFFF).to_bytes(4, "big").hex()
        for read in plan_memory_reads(select_memory_channels(TUNING.memory))
    }
    assert reads == planned


def _memory_profile(name: str, memory: tuple[str, ...], **update: object) -> CaptureProfile:
    fields: dict[str, object] = {
        "name": name,
        "mode01": {"RPM": 1.0},
        "mode22": (),
        "memory": memory,
        "did_cycle_seconds": 0.5,
        "target_rate_hz": 2.0,
        "storage_prefix": f"_capture-{name}",
    }
    fields.update(update)
    return CaptureProfile.model_validate(fields)


def _read_order(run: dict[str, Any]) -> list[str]:
    """Addresses of the first window's service-0x23 reads, in send order."""
    entry = next(i for i, (_at, d) in enumerate(run["sent"]) if d.startswith("0210c0"))
    order: list[str] = []
    for _at, data in run["sent"][entry + 1 :]:
        if data.startswith("021081"):
            break
        if data[2:4] == "23":
            order.append(data[4:12])
    return order


def test_another_profile_cannot_reorder_the_active_ones_window_reads() -> None:
    """``ECM_KNOCK_RETARD_DEG`` sits late in the fuel_target window, after the
    spark window's first member. Planned by first *selected* member, a profile
    reading it and the spark window read spark first, until any other profile
    selected fuel_target's first member, which put fuel_target first: two reads
    due together go in plan order, so one profile reordered another's."""
    reader = _memory_profile("zz-reader", ("ECM_KNOCK_RETARD_DEG", "ECM_IGNITION_TIMING_RAM"))
    other = _memory_profile("zz-other", ("ECM_TARGET_EQUIV_RATIO",))
    alone = _run((DEFAULT, reader), reader, 3.0, switch_at=None)
    beside = _run((DEFAULT, reader, other), reader, 3.0, switch_at=None)
    assert len(_read_order(alone)) == 2
    assert _read_order(beside) == _read_order(alone)


def test_a_member_no_profile_in_force_selects_never_opens_a_window() -> None:
    """A profile whose one DID is due every other 0.25 s cycle opens a window
    every other cycle. The plan also holds every catalogued DID, which nothing
    requests, so their first due time stays in the past: counted as due, they
    opened a window on every boundary, an entry and an exit reading nothing."""
    sparse = _memory_profile(
        "zz-sparse",
        (),
        mode22=("ECM_RPM",),
        mode22_rate_weights={"ECM_RPM": 0.5},
        did_cycle_seconds=0.25,
        target_rate_hz=4.0,
        bus_budget_override=90.0,
        expected_total_demand_hz=12.0,
        capacity_evidence="test: one PID at 4 Hz, one DID at 2 Hz, 8 session requests/s",
    )
    run = _run((DEFAULT, sparse), sparse, 10.0, switch_at=None)
    entries = [at for at, d in run["sent"] if d.startswith("0210c0")]
    reads = [at for at, d in run["sent"] if d[2:4] == "22"]
    assert len(entries) >= 15
    assert len(entries) == len(reads), (len(entries), len(reads))
