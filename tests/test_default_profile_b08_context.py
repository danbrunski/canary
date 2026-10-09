"""What stops the B08 firmware-context windows going into ``default`` (B08.4).

Operator decision 2026-10-08 (B08.4 recommendation a): add the timing and
fuel-context memory windows, coolant DID 0x1101 and the map-identity window to
the Xterra ``default`` profile, after a bus-budget check, so B08.3/B08.4
evidence can come from corpus-eligible drives. The check found two things the
profile data cannot fix, and each is a refusal here rather than a note:

- **The deployed sequence model learned two of those channels.** The Pi's LSTM
  (``2026-09-24T035125``, ``tests/fixtures/deployed_manifests``) carries
  ``obd:mem:ECM_ENGINE_SPEED_RAM`` and ``obd:mem:ECM_IGNITION_TIMING_RAM``,
  scaled on one idle count (mean 775 rpm, std 12.5; 16 degBTDC, std 1). They
  are NaN at serve today because ``default`` reads no RAM. Polled by
  ``default``, every read would reach that model as a 100-sigma input. The
  feature policy now refuses memory channels at training
  (``feature_pipeline.MEMORY_CHANNEL_EXCLUSION``); an artifact that already
  carries them keeps being served them until it is replaced.
- **The coordinate tuple could not meet its 0.25 s skew at the serving budget.**
  The rpm word, BFS-X and the HI DET latch are three service-0x23 reads (their
  addresses are hundreds of bytes apart, past the ECM's 63-byte ceiling), and
  ``default`` paced every request at the 8 req/s budget its 1 Hz poll rate
  derives: 0.5 s for the memory tuple, 0.75 s and more with Mode 01. The
  operator approved option (b) on 2026-10-08: a window's memory reads now go
  last, back to back, with the session exit straight after
  (``can_obd_loop._CanObdLoop._memory_burst``), so the tuple and its Mode 01
  leg meet the skew at the serving budget. Skew is measured on the fake clock
  from each read's acquisition time, never count over wall-clock.
"""

from __future__ import annotations

import bisect
import dataclasses
import json
import threading
from collections.abc import Iterable
from pathlib import Path

import pytest
import yaml

from src.collection import bus_budget, tuning_profile
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.capture_profiles import CaptureProfile, CaptureProfileCatalog
from src.collection.obd_collector import ObdReading
from src.collection.obd_registry import MODE01_REGISTRY
from src.collection.obd_scheduler import CanObdSpec, did_alias_table, profile_did_union
from src.config import CanBusConfig, CollectionConfig
from src.decode.dids import MEMORY_SOURCE, plan_memory_reads, select_memory_channels
from src.features.feature_pipeline import MEMORY_FEATURE_PREFIX
from tests.capture_round_fakes import FakeClock, ScriptedEcu, install_clock, support_bits

REPO = Path(__file__).resolve().parents[1]
DEPLOYED = REPO / "tests" / "fixtures" / "deployed_manifests"
SLUG = "nissan-xterra-2008"
CATALOG = REPO / "data" / SLUG / "capture_profiles.yaml"
CONTEXT = REPO / "data" / SLUG / "operating_context.yaml"
CAN = CanBusConfig(interface="virtual", channel="b08-context", bitrate=500000)
#: Request-to-response latency; the truck sustained 116.8 req/s on 2026-09-10.
TRUCK_LATENCY_S = 0.010
#: The Mode 01 reads B08.4 pairs the coordinate with (the existing timing
#: surface's target and axes).
MODE01_LEG = ("RPM", "ENGINE_LOAD", "TIMING_ADVANCE")
#: What the approval adds, by window: spark (timing + rpm word), fuel_axis
#: (BFS-X), fuel_comp_x (fill), flags_2841 (HI DET + pedal), uprev_state (map
#: identity).
B08_MEMORY = (
    "ECM_IGNITION_TIMING_RAM",
    "ECM_ENGINE_SPEED_RAM",
    "ECM_FUEL_TARGET_MAP_X_TRACE",
    "ECM_FUEL_COMP_X_INPUT",
    "ECM_HIGH_DET_FLAGS",
    "ECM_PEDAL_ENRICH_FLAGS",
    "ECM_UPREV_MAP_SELECTOR",
    "ECM_UPREV_MAGIC",
    "ECM_ACTIVE_MAP",
)


def _catalog() -> CaptureProfileCatalog:
    return CaptureProfileCatalog.model_validate(yaml.safe_load(CATALOG.read_text()))


def _default() -> CaptureProfile:
    profile = _catalog().by_name("default")
    assert profile is not None
    return profile


def _learned_memory_channels(manifest: Path) -> set[str]:
    """Memory channel names a manifest's feature columns carry, any expansion."""
    columns = json.loads(manifest.read_text())["schema"]["feature_columns"]
    return {
        str(column).removeprefix(MEMORY_FEATURE_PREFIX).split("__", 1)[0]
        for column in columns
        if str(column).startswith(MEMORY_FEATURE_PREFIX)
    }


def _coordinate_tuple() -> tuple[tuple[str, ...], float]:
    """The timing-trim lookup's rows, columns and selector channels, and its
    tuple skew limit, from the data that declares them."""
    context = yaml.safe_load(CONTEXT.read_text())
    timing = context["timing_trim"]
    inputs = context["inputs"]
    names = (timing["rows"]["input"], timing["columns"]["input"], timing["select_input"])
    channels = tuple(str(inputs[name]["channel"]).removeprefix("mem:") for name in names)
    return channels, float(timing["max_acquisition_skew_s"])


# ---------------------------------------------------------------------------
# A deployed detector's learned memory channel may not be polled by default
# ---------------------------------------------------------------------------


def test_default_polls_no_memory_channel_a_deployed_detector_learned() -> None:
    manifests = sorted((DEPLOYED / SLUG).glob("*/model.manifest.json"))
    assert manifests, "no deployed manifest fixtures"
    selected = set(_default().memory)
    for manifest in manifests:
        clash = selected & _learned_memory_channels(manifest)
        assert not clash, (
            f"default polls {sorted(clash)}, which the deployed "
            f"{manifest.parent.name} learned as features: polled, each read reaches "
            "it as a value it never saw at serve (B08.4, 2026-10-08). Replace the "
            "artifact without them and refresh tests/fixtures/deployed_manifests first."
        )


def test_the_deployed_lstm_carries_the_spark_window_so_the_guard_can_fail() -> None:
    """The control: the approval's spark window is exactly what the Pi's LSTM
    learned, so adding it to ``default`` today fails the guard above."""
    learned = _learned_memory_channels(DEPLOYED / SLUG / "lstm_autoencoder/model.manifest.json")
    assert learned == {"ECM_ENGINE_SPEED_RAM", "ECM_IGNITION_TIMING_RAM"}
    assert learned <= set(B08_MEMORY)


# ---------------------------------------------------------------------------
# The coordinate tuple is three reads, so its skew is set by request pacing
# ---------------------------------------------------------------------------


def test_the_coordinate_tuple_needs_three_separate_memory_reads() -> None:
    channels, _skew = _coordinate_tuple()
    assert channels == ("ECM_ENGINE_SPEED_RAM", "ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_HIGH_DET_FLAGS")
    reads = plan_memory_reads(select_memory_channels(channels))
    assert len(reads) == 3
    # Even spanning all three is far past the 63-byte single-read ceiling.
    span = max(r.address + r.length for r in reads) - min(r.address for r in reads)
    assert span > 63


def _payloads(channels: Iterable[str]) -> dict[int, bytes]:
    by_channel = {d.channel: (pid, d.byte_length) for pid, d in MODE01_REGISTRY.items()}
    values = {by_channel[name][0]: bytes([0x40]) * by_channel[name][1] for name in channels}
    values[0x0C] = b"\x0b\xb8"  # 750 rpm: the engine is running
    return values


def _acquired(reading: ObdReading) -> float:
    return float(reading.acquired_at if reading.acquired_at is not None else reading.timestamp)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    profile: CaptureProfile,
    seconds: float,
    budget_rps: float | None = None,
) -> dict[str, list[float]]:
    """The daemon's shape (``test_default_profile_core_rate._xterra_run``): the
    tracked catalog with ``profile`` as ``default``, the Pi's collection
    settings, a session-gated ECM. Returns acquisition times per channel."""
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    catalog = CaptureProfileCatalog(
        profiles=[profile if p.name == "default" else p for p in _catalog().profiles]
    )
    collection = CollectionConfig(
        obd_source="canable",
        obd_pids=tuple(profile.mode01),
        obd_dids=tuple(profile.mode22),
        obd_batch_max_pids=6,
        obd_did_batch_max_dids=1,
        obd_request_timeout_seconds=1.0,
        obd_poll_rate_hz=1,
        storage_path=Path("unused"),
        session_retention_days=30,
        storage_warning_mb=500,
    )
    spec = dataclasses.replace(
        CanObdSpec.from_config(collection, CAN, catalog=catalog.profiles), calibration_id=None
    )
    tuning_profile.configure_default(profile)
    tuning_profile.configure_did_aliases(
        did_alias_table(profile_did_union(collection, catalog.profiles))
    )
    if budget_rps is not None:
        bus_budget.set_override(budget_rps, source="test", ttl_seconds=None)
    bus = ScriptedEcu(
        clock,
        latency=TRUCK_LATENCY_S,
        session_gated=True,
        values=_payloads(profile.mode01),
        mode06_support={0x00: support_bits(0x01)},
        mode06_values={0x01: bytes([0x01, 0x0A, 0, 0, 0, 0, 0xFF, 0xFF])},
        dtc_responses={0x03: b"\x00", 0x07: b"\x00", 0x0A: b"\x00"},
    )
    readings: list[ObdReading] = []
    try:
        run_can_obd_loop(
            bus,  # type: ignore[arg-type]
            spec,
            on_frame=lambda _msg: None,
            on_reading=readings.append,
            stop_event=threading.Event(),
            deadline_monotonic=clock.monotonic() + seconds,
        )
    finally:
        bus_budget.reset()
    stamps: dict[str, list[float]] = {}
    for reading in readings:
        stamps.setdefault(reading.pid, []).append(_acquired(reading))
    return {channel: sorted(times) for channel, times in stamps.items()}


def _tuple_spreads(
    stamps: dict[str, list[float]], channels: tuple[str, ...]
) -> list[tuple[float, float]]:
    """Per coordinate read: (spread of the memory tuple, spread once the
    nearest RPM/ENGINE_LOAD/TIMING_ADVANCE reads join it), each from paired
    acquisition times. Every member is the read nearest the rpm word's."""
    members = [f"{MEMORY_SOURCE}:{name}" for name in channels]
    out = []
    for anchor in stamps[members[0]]:
        reads = [min(stamps[m], key=lambda t, a=anchor: abs(t - a)) for m in members]
        lo, hi = min(reads), max(reads)
        joined_lo, joined_hi = lo, hi
        for pid in MODE01_LEG:
            times = stamps[pid]
            at = bisect.bisect_left(times, lo)
            sides = [times[i] for i in (at - 1, at) if 0 <= i < len(times)]
            nearest = min(sides, key=lambda t: max(hi, t) - min(lo, t))
            joined_lo, joined_hi = min(joined_lo, nearest), max(joined_hi, nearest)
        out.append((hi - lo, joined_hi - joined_lo))
    return out


def _with_b08(profile: CaptureProfile) -> CaptureProfile:
    return CaptureProfile.model_validate(
        {
            **profile.model_dump(),
            "mode22": (*profile.mode22, "ECM_COOLANT_TEMP"),
            "memory": B08_MEMORY,
        }
    )


@pytest.mark.slow
def test_the_b08_additions_meet_the_lookup_skew_at_the_serving_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two windows of the approved additions at the 8 req/s the 1 Hz poll
    rate derives, and the same at 40 req/s. Paced, the tuple spanned 0.5 s
    (0.75 s and more with Mode 01) at 8 req/s; with the window's memory reads
    back to back before the exit it fits the lookup's skew at either budget,
    Mode 01 rpm/load/timing included."""
    channels, skew = _coordinate_tuple()
    candidate = _with_b08(_default())

    for budget in (None, 40.0):
        spreads = _tuple_spreads(_run(monkeypatch, candidate, 130.0, budget_rps=budget), channels)
        assert len(spreads) == 2
        # Five reads back to back, never a budget slot (0.125 s) between two.
        assert all(memory < 0.125 for memory, _joined in spreads), spreads
        assert all(joined <= skew for _memory, joined in spreads), spreads


@pytest.mark.slow
def test_a_default_that_reads_the_coordinate_tuple_meets_the_lookup_skew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard on the committed profile: once ``default`` selects the
    coordinate tuple, every achieved tuple, Mode 01 leg included, sits inside
    the timing-trim lookup's skew. Until then there is nothing to measure."""
    channels, skew = _coordinate_tuple()
    profile = _default()
    if not set(channels) <= set(profile.memory):
        assert not set(channels) & set(profile.memory), (
            "default reads part of the coordinate tuple: all of it or none"
        )
        return
    spreads = _tuple_spreads(_run(monkeypatch, profile, 130.0), channels)
    assert spreads
    assert all(joined <= skew for _memory, joined in spreads), spreads


@pytest.fixture(autouse=True)
def _runtime_reset() -> Iterable[None]:
    yield None
    tuning_profile.configure_did_aliases({})
    bus_budget.reset()
