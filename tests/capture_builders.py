"""Capture-request fixtures: memory-window profiles and specs, scheduler tuples and the
Xterra catalog (R30.2).

Moved out of the ``tests/test_*.py`` files that provided them, bodies unchanged; private helpers
got public names."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

import can

from src.collection import tuning_profile
from src.collection.can_obd_loop import run_can_obd_loop
from src.collection.capture_lifetime import ChannelAccumulator
from src.collection.capture_profiles import CaptureProfile
from src.collection.obd_collector import ObdReading
from src.collection.obd_scheduler import CanObdSpec, DidSessionSpec, did_alias_table
from src.config import CanBusConfig
from src.decode.dids import select_memory_channels
from src.paths import REPO_ROOT
from src.storage.capture_losses import LossEpoch, reconcile_losses, summarize_losses
from tests.capture_round_fakes import FakeClock, ScriptedEcu, support_bits

#: ``poll_rate_hz`` 1 derives the 8 req/s the Pi serves at.
BUDGET_RPS = 8.0


SLOT = 1.0 / BUDGET_RPS


LATENCY = 0.010


#: The coordinate tuple plus final timing: the spark window (timing + rpm
#: word), the fuel_axis window (BFS-X) and flags_2841 (HI DET): three reads.
MEMORY = (
    "ECM_IGNITION_TIMING_RAM",
    "ECM_ENGINE_SPEED_RAM",
    "ECM_FUEL_TARGET_MAP_X_TRACE",
    "ECM_HIGH_DET_FLAGS",
)


DIDS = ("ECM_RPM", "ECM_AIR_CHARGE", "ECM_IGNITION_TIMING")


MODE01 = ("RPM", "ENGINE_LOAD", "TIMING_ADVANCE")


CYCLE_S = 4.0


def window_profile(**update: object) -> CaptureProfile:
    fields: dict[str, object] = {
        "name": "burst",
        "mode01": dict.fromkeys(MODE01, 1.0),
        "mode22": DIDS,
        "memory": MEMORY,
        "did_cycle_seconds": CYCLE_S,
        "storage_prefix": "_capture-burst",
    }
    fields.update(update)
    return CaptureProfile.model_validate(fields)


def window_spec(*, window_max_seconds: float = 10.0, keepalive_seconds: float = 2.0) -> CanObdSpec:
    return CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="burst", bitrate=500000),
        poll_rate_hz=1.0,
        pids=MODE01,
        dids=DIDS,
        batch_max_pids=6,
        priority_channels=(),
        dtc_poll_interval_seconds=0.0,
        service_poll_interval_seconds=0.0,
        request_timeout_seconds=1.0,
        capture_profiles_enabled=True,
        memory=select_memory_channels(MEMORY),
        did_session=DidSessionSpec(
            session=0xC0,
            exit_session=0x81,
            exit_grace_seconds=0.05,
            keepalive_seconds=keepalive_seconds,
            window_interval_seconds=CYCLE_S,
            window_max_seconds=window_max_seconds,
            lapse_seconds=5.0,
        ),
    )


#: Every ScriptedEcu a test built, newest last (for its lapse count).
RUNS: list[ScriptedEcu] = []


def window_run(
    clock: FakeClock,
    profile: CaptureProfile,
    seconds: float,
    *,
    ecu: dict[str, Any] | None = None,
    unseen_memory: bool = False,
    **spec: float,
) -> tuple[list[tuple[float, bytes]], list[ObdReading]]:
    """Requests sent (flow control left out) and readings, on the fake clock.

    ``unseen_memory``: every 0x23 request is lost before the ECM: no answer,
    and the session's S3 timer is not refreshed by it."""
    tuning_profile.configure_did_aliases(did_alias_table(DIDS))
    tuning_profile.activate(profile, source="test")
    bus = ScriptedEcu(clock, latency=LATENCY, session_gated=True, **(ecu or {}))
    if unseen_memory:
        deliver = bus.send

        def send(msg: can.Message, timeout: float | None = None) -> None:
            if len(msg.data) >= 2 and msg.data[1] == 0x23:
                bus.sent.append((clock.monotonic(), msg))
                return
            deliver(msg, timeout)

        bus.send = send  # type: ignore[method-assign]
    RUNS.append(bus)
    readings: list[ObdReading] = []
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        window_spec(**spec),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + seconds,
    )
    # Requests only: a flow-control frame is the tail of the read before it.
    requests = [(at, bytes(msg.data)) for at, msg in bus.sent if msg.data[0] >> 4 != 0x3]
    return requests, readings


XTERRA_CATALOG = REPO_ROOT / "data" / "nissan-xterra-2008" / "capture_profiles.yaml"


#: The 23 Mode 06 MIDs the Xterra ECM answers, from walking its support
#: groups 0x00-0xA0 (data/analyses/2026-07-28T0330-ecu-address-space.md
#: "Mode 06"; the C06.12 hardware walk found the same 23 in 6 probes,
#: docs/diagnostic-discovery.md). Each answers one test record (TID, UAS,
#: value, min, max), the shape `test_default_profile_core_rate` uses.
XTERRA_MODE06_MIDS = (
    0x01, 0x02, 0x05, 0x06, 0x21, 0x22, 0x39, 0x3B, 0x3C, 0x3D, 0x41, 0x42,
    0x45, 0x46, 0x81, 0x82, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7,
)  # fmt: skip


XTERRA_MODE06: dict[str, Any] = {
    "mode06_support": {
        group: support_bits(
            *(mid for mid in XTERRA_MODE06_MIDS if group < mid <= group + 0x20),
            base=group,
            more=group < 0xA0,
        )
        for group in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0)
    },
    "mode06_values": {
        mid: bytes([0x01, 0x0A, 0, 0, 0, 0, 0xFF, 0xFF]) for mid in XTERRA_MODE06_MIDS
    },
}


#: Evidence methods that are reads of the address on the running ECM, as
#: opposed to firmware analysis. ``live_read`` is the method from 2026-10-08
#: on; the rest are the earlier, differently named live reads that the
#: channels already in profiles carry (each checked against its finding:
#: every one reads the RAM address through service 0x23 on the truck).
LIVE_READ_METHODS = frozenset(
    {
        "live_read",
        "live_drive_check",
        "ram_sweep_against_confirmed_did",
        "independent_reproduction",
        "duplicate_register_survey",
        "same_reply_transform_vs_did_1201",
        "same_reply_piecewise_identity_vs_did_0x1134",
        "can_transport_parity",
        "fuel_cut_coincidence",
        # A chain reproduction on a live capture is a read too: the address
        # was read through service 0x23 on the truck, and its value checked
        # against an independent computation from other channels (and wrong
        # variants of that computation shown to miss), which is a known answer.
        "chain_reproduction",
    }
)


TUPLE = ["RPM", "ENGINE_LOAD", "FUEL_STATUS", "COMMANDED_EQUIV_RATIO", "O2_WR_LAMBDA_S1"]


def quality_profile(**overrides: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "fuel-map",
        "mode01": {name: 1.0 for name in TUPLE} | {"COOLANT_TEMP": 0.25},
        "target_rate_hz": 4.0,
        "required_tuple": list(TUPLE),
        "min_achieved_rate_hz": 3.0,
        "round_timestamps": True,
    }
    base.update(overrides)
    return base


def quality_scheduler(
    channels: list[str], *, trailing_hz: dict[str, float] | None = None
) -> dict[str, Any]:
    return {
        "capture_observed_seconds": 60.0,
        "achieved_hz_capture": dict(trailing_hz or {}),
        "capture_max_gap_seconds": {},
        "requests": [{"channel": c, "kind": "pid", "capture_selected": True} for c in channels],
        "batching": {"max_pids": 6, "mean_pids_per_batch": 5.2},
    }


def quality_tuples(
    complete: int,
    elapsed: float = 300.0,
    *,
    windows: list[tuple[float, float, float]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    acc = ChannelAccumulator("__tuple__")
    if windows is None:
        for i in range(complete):
            acc.observe(i * 0.25)
    else:
        for start, stop, period in windows:
            t = start
            while t <= stop + 1e-9:
                acc.observe(t)
                t += period
    block = {
        "source": "persisted_rows",
        "attempted": complete,
        "complete": complete,
        "persisted": complete,
        "incomplete": 0,
        "skew_rejected": 0,
        "member_misses": dict.fromkeys(TUPLE, 0),
        "skew": {"p95_s": 0.01, "max_s": 0.02},
        "lifetime": acc.snapshot(elapsed).to_dict(),
    }
    block.update(overrides)
    return block


def _epoch(counters: dict[str, int | None], at: float = 0.0) -> LossEpoch:
    return LossEpoch(taken_at_monotonic=at, taken_at_wall=at, counters=counters)


def verified_losses(
    start: Mapping[str, int | None], stop: Mapping[str, int | None]
) -> dict[str, Any]:
    return summarize_losses(reconcile_losses(_epoch(dict(start)), _epoch(dict(stop), 300.0)))


CLEAN: dict[str, int | None] = {
    "storage.dropped_records": 0,
    "storage.errors": 0,
    "can0.rx_dropped": 0,
}
