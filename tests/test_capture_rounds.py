"""Explicit capture rounds and preserved acquisition clocks (roadmap task C02).

Everything here runs on a fake clock and a scripted ECU
(:mod:`tests.capture_round_fakes`), so timing assertions are exact rather
than "probably within a few ms on a quiet laptop".

The headline counterexample (C02.1): two channels at 10 Hz on an 8 req/s
budget, batching off. Before the fix the loop kept one round stamp for as
long as work stayed due — measured here as **24 readings on one stamp
spanning 2.875 s** (15 readings / 2.875 s with a refused batch and single-PID
fallback). Now every stamp covers at most one round period.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pandas as pd
import pytest

from src.analysis.channels import (
    ROUND_ID_COLUMN,
    TIMESTAMP_BASIS_COLUMN,
    read_acquisition_frame,
    read_channels,
    round_skew,
)
from src.analysis.compare import compare_channels
from src.collection import capture_generation, tuning_profile, tx_gate
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.capture_profiles import CaptureProfile
from src.collection.obd_collector import TS_SCHEMA_VERSION, ObdReading, reading_to_record
from src.collection.obd_scheduler import (
    CanObdLoopStats,
    CanObdSpec,
    FreezeFrameTrigger,
    RoundScheduler,
    _scheduler_metrics,
    build_poll_requests,
    round_period_seconds,
)
from src.config import CanBusConfig
from src.preprocessing.loader import read_session, to_wide
from src.preprocessing.resampler import ResampleConfig, resample_wide
from src.signals.pairing import ComparisonRefusedError, PairingPolicy
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.capture_round_fakes import (
    FakeClock,
    ScriptedEcu,
    group_by_stamp,
    install_clock,
    stamp_span_seconds,
)

BINDINGS = Path("data/bindings.yaml")
DIDS = Path("data/dids.yaml")


def _spec(**overrides: object) -> CanObdSpec:
    base: dict[str, object] = {
        "can": CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        "poll_rate_hz": 1.0,  # -> 8 req/s bus budget
        "pids": ("RPM", "SPEED"),
        "batch_max_pids": 1,
        "priority_channels": (),
        "dtc_poll_interval_seconds": 0.0,
        "service_poll_interval_seconds": 0.0,
        "request_timeout_seconds": 0.2,
    }
    base.update(overrides)
    return CanObdSpec(**base)  # type: ignore[arg-type]


def _profile(name: str, channels: dict[str, float], **overrides: object) -> CaptureProfile:
    fields: dict[str, object] = {
        "name": name,
        "mode01": channels,
        "round_timestamps": True,
        "corpus_eligible": False,
        "storage_prefix": f"_capture-{name}",
    }
    fields.update(overrides)
    return CaptureProfile(**fields)  # type: ignore[arg-type]


def _run(
    clock: FakeClock, bus: ScriptedEcu, spec: CanObdSpec, *, seconds: float
) -> tuple[list[ObdReading], CanObdLoopStats]:
    readings: list[ObdReading] = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + seconds,
    )
    return readings, stats


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    install_clock(monkeypatch, fake)
    return fake


@pytest.fixture
def profile_reset() -> object:
    yield None
    tuning_profile.set_active(False, source="test")


def _by_round(readings: list[ObdReading]) -> dict[tuple[int, int], list[ObdReading]]:
    rounds: dict[tuple[int, int], list[ObdReading]] = {}
    for reading in readings:
        assert reading.generation is not None and reading.round_id is not None
        rounds.setdefault((reading.generation, reading.round_id), []).append(reading)
    return rounds


# ---------------------------------------------------------------------------
# C02.1 / C02.10 — the overloaded-round counterexample, both variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("batching", ["off", "refused"])
def test_overloaded_capture_stamps_are_bounded_by_one_round(
    clock: FakeClock, profile_reset: object, batching: str
) -> None:
    """Before: 24 readings on one stamp over 2.875 s (15 with the refused
    batch). After: no stamp spans more than a 100 ms round, the shortfall is
    counted as incomplete rounds, and no channel is attempted twice in one."""
    bus = ScriptedEcu(clock, batch_nrc=0x12 if batching == "refused" else None)
    spec = _spec(batch_max_pids=6 if batching == "refused" else 1)
    tuning_profile.activate(
        _profile("two-at-ten", {"RPM": 1.0, "SPEED": 1.0}, target_rate_hz=10.0), source="test"
    )
    readings, stats = _run(clock, bus, spec, seconds=3.0)

    grouped = group_by_stamp(readings)
    widest = max(grouped.values(), key=len)
    assert len(widest) < 8 and stamp_span_seconds(widest) < 0.8, "the counterexample is back"
    assert all(stamp_span_seconds(group) <= 0.1 for group in grouped.values())
    assert len(grouped) >= 15, f"only {len(grouped)} distinct round stamps in 3 s"
    # The budget serves one of two members per 100 ms round: the shortfall is
    # explicit, not hidden in a shared stamp.
    assert stats.rounds_incomplete > 0
    assert stats.rounds_started == stats.rounds_complete + stats.rounds_incomplete
    for members in _by_round(readings).values():
        channels = [reading.pid for reading in members]
        assert len(channels) == len(set(channels)), "a channel was read twice in one round"
    if batching == "refused":
        assert bus.batch_requests > 0 and stats.batch_failures > 0


def test_every_reading_carries_exactly_one_generation_and_round(
    clock: FakeClock, profile_reset: object
) -> None:
    """C02.14: one (generation, round) per reading; the stamp is the round's
    scheduled start; the acquisition instant is always kept alongside."""
    bus = ScriptedEcu(clock)
    tuning_profile.activate(_profile("pair", {"RPM": 1.0, "SPEED": 1.0}, target_rate_hz=4.0))
    readings, _stats = _run(clock, bus, _spec(), seconds=2.0)

    assert readings
    for reading in readings:
        assert reading.generation == capture_generation.current()
        assert reading.round_id is not None
        assert reading.acquired_at is not None
        assert reading.acquired_at >= reading.timestamp
        assert reading.acquired_at - reading.timestamp < 0.25
    stamps = {reading.timestamp for reading in readings}
    ids = {reading.round_id for reading in readings}
    assert len(stamps) == len(ids), "each round has one stamp and each stamp one round"


# ---------------------------------------------------------------------------
# C02.2 / C02.4 — round state and deadlines
# ---------------------------------------------------------------------------


def test_round_state_ends_on_completion_or_deadline_and_counts_skips() -> None:
    stats = CanObdLoopStats()
    rounds = RoundScheduler(generation=7, stats=stats)

    first = rounds.open(100.0, 5_000.0, period=0.1, members=(0, 1))
    assert (first.generation, first.round_id) == (7, 0)
    assert (first.scheduled_start, first.deadline, first.wall_start) == (100.0, 100.1, 5_000.0)
    assert rounds.expire(100.05) is None, "not yet"
    rounds.on_attempt(0)
    rounds.on_observed(0, 1, 5_000.004)
    assert rounds.current is not None and not rounds.current.complete
    closed = rounds.expire(100.1)
    assert closed is not None and closed.attempted == {0} and not closed.complete
    assert (stats.rounds_started, stats.rounds_complete, stats.rounds_incomplete) == (1, 0, 1)

    # Falling behind: opening at 100.35 skips rounds k=1 and k=2 outright.
    assert rounds.next_open_at(100.35, 0.1) == 100.35
    third = rounds.open(100.35, 5_000.35, period=0.1, members=(0,))
    assert third.round_id == 1 and third.scheduled_start == pytest.approx(100.3)
    assert third.wall_start == pytest.approx(5_000.3)
    assert stats.rounds_skipped == 2
    rounds.on_attempt(0)
    rounds.on_observed(0, 1, 5_000.36)
    assert rounds.current is not None and rounds.current.complete
    rounds.close(100.36)
    assert stats.rounds_complete == 1
    # A completed round never re-opens inside its own slot.
    assert rounds.next_open_at(100.36, 0.1) == pytest.approx(100.4)

    # The frozen state is replaced, never mutated in place.
    assert first.attempted == frozenset() and first.observations == {}
    with pytest.raises(AttributeError):
        first.attempted = frozenset({1})  # type: ignore[misc]


def test_new_generation_restarts_round_numbering() -> None:
    stats = CanObdLoopStats()
    rounds = RoundScheduler(generation=1, stats=stats)
    rounds.open(0.0, 0.0, period=1.0, members=(0,))
    rounds.reset(2, 0.5)
    assert rounds.current is None and stats.rounds_incomplete == 1
    reopened = rounds.open(0.5, 0.5, period=1.0, members=(0,))
    assert (reopened.generation, reopened.round_id) == (2, 0)
    assert stats.rounds_skipped == 0, "a reset re-anchors; it is not a missed deadline"


# ---------------------------------------------------------------------------
# C02.3 / C02.11 — mixed-rate membership and continuously due DID work
# ---------------------------------------------------------------------------


def test_context_channel_joins_every_fourth_round(clock: FakeClock, profile_reset: object) -> None:
    """4 Hz fast / 1 Hz context: the period is the fast cadence; the context
    channel is a member of every fourth round and is never attempted twice."""
    bus = ScriptedEcu(clock)
    tuning_profile.activate(
        _profile("fast-context", {"RPM": 1.0, "COOLANT_TEMP": 0.25}, target_rate_hz=4.0)
    )
    spec = _spec(pids=("RPM", "COOLANT_TEMP"), poll_rate_hz=4.0)  # 32 req/s: budget is not binding
    requests = build_poll_requests(bus, spec)  # type: ignore[arg-type]
    states = [type("S", (), {"suppressed_until": 0.0, "next_due_at": 0.0})() for _ in requests]
    assert round_period_seconds(requests, states, spec, 0.0) == pytest.approx(0.25)  # type: ignore[arg-type]

    readings, stats = _run(clock, bus, spec, seconds=4.0)

    rpm_rounds = sorted({r.round_id for r in readings if r.pid == "RPM" and r.round_id is not None})
    coolant_rounds = sorted(
        {r.round_id for r in readings if r.pid == "COOLANT_TEMP" and r.round_id is not None}
    )
    assert len(rpm_rounds) >= 14 and rpm_rounds == list(range(rpm_rounds[0], rpm_rounds[-1] + 1))
    assert 3 <= len(coolant_rounds) <= 5
    gaps = [b - a for a, b in pairwise(coolant_rounds)]
    assert all(gap == 4 for gap in gaps), f"context cadence drifted: rounds {coolant_rounds}"
    assert stats.rounds_incomplete == 0
    for members in _by_round(readings).values():
        assert len({r.pid for r in members}) == len(members)


def test_continuously_due_did_work_at_15hz_forms_complete_rounds(
    clock: FakeClock, profile_reset: object
) -> None:
    """Every DID is due in every 66.7 ms round; with the budget able to serve
    them the rounds complete and stamps advance one period at a time."""
    bus = ScriptedEcu(clock)
    tuning_profile.activate(
        CaptureProfile(
            name="did-fast",
            mode01={},
            mode01_enabled=False,
            mode22=("1101", "1102"),
            target_rate_hz=15.0,
            round_timestamps=True,
            corpus_eligible=False,
            storage_prefix="_capture-did-fast",
        )
    )
    # RPM is listed but the profile disables Mode 01, so only the DIDs are
    # eligible; an empty PID list would mean auto-discovery instead.
    spec = _spec(pids=("RPM",), dids=("1101", "1102"), poll_rate_hz=4.0)  # 32 req/s budget
    readings, stats = _run(clock, bus, spec, seconds=2.0)

    assert {r.pid for r in readings} == {"DID_1101", "DID_1102"}
    rounds = _by_round(readings)
    assert len(rounds) >= 28, f"expected ~30 rounds in 2 s at 15 Hz, got {len(rounds)}"
    # The first rounds absorb the startup stagger; from then on both DIDs are
    # members of every round.
    settled = [members for (_g, round_id), members in rounds.items() if round_id >= 2]
    assert settled and all(
        {r.pid for r in members} == {"DID_1101", "DID_1102"} for members in settled
    )
    assert stats.rounds_incomplete == 0 and stats.rounds_skipped == 0
    stamps = sorted({r.timestamp for r in readings})
    steps = [b - a for a, b in pairwise(stamps)]
    assert all(step == pytest.approx(1 / 15, abs=1e-6) for step in steps)


# ---------------------------------------------------------------------------
# C02.5 / C02.11 — batches inside a round and measured skew
# ---------------------------------------------------------------------------


def test_six_pid_batch_plus_extra_members_share_a_round_with_distinct_arrivals(
    clock: FakeClock, profile_reset: object
) -> None:
    channels = (
        "RPM",
        "SPEED",
        "ENGINE_LOAD",
        "THROTTLE_POS",
        "COOLANT_TEMP",
        "INTAKE_TEMP",
        "TIMING_ADVANCE",
        "MAF",
    )
    bus = ScriptedEcu(clock)
    tuning_profile.activate(_profile("eight", dict.fromkeys(channels, 1.0), target_rate_hz=2.0))
    readings, stats = _run(clock, bus, _spec(pids=channels, batch_max_pids=6), seconds=2.0)

    assert bus.batch_requests > 0
    rounds = _by_round(readings)
    full = [members for members in rounds.values() if {r.pid for r in members} >= set(channels)]
    assert full, "no round carried all eight channels"
    for members in full:
        stamps = {r.timestamp for r in members}
        arrivals = {r.acquired_at for r in members}
        assert len(stamps) == 1, "one round, one presentation stamp"
        assert len(arrivals) == 2, "two batches, two receipt instants — retained, not merged"
        skew = max(arrivals) - min(arrivals)  # type: ignore[type-var]
        assert 0 < skew < 0.5, "measured, positive, and inside the round period"
    assert stats.round_skew_max_s > 0
    assert stats.round_skew_max_s == pytest.approx(
        max(stamp_span_seconds(members) for members in full), abs=1e-9
    )


def test_required_tuple_members_are_batched_first(clock: FakeClock, profile_reset: object) -> None:
    """With more members than one batch holds, the profile's required tuple
    travels in the first request rather than wherever list order put it."""
    channels = ("COOLANT_TEMP", "INTAKE_TEMP", "TIMING_ADVANCE", "MAF", "RPM", "SPEED")
    bus = ScriptedEcu(clock)
    tuning_profile.activate(
        _profile(
            "tuple-first",
            dict.fromkeys(channels, 1.0),
            target_rate_hz=2.0,
            required_tuple=("RPM", "SPEED", "MAF"),
        )
    )
    readings, _ = _run(clock, bus, _spec(pids=channels, batch_max_pids=4), seconds=2.0)

    assert bus.batch_requests > 0
    rounds = _by_round(readings)
    with_tuple = [
        members
        for (_g, round_id), members in rounds.items()
        if round_id >= 1 and {r.pid for r in members} >= {"RPM", "SPEED", "MAF"}
    ]
    assert len(with_tuple) >= 2
    for members in with_tuple:
        tuple_arrivals = {r.acquired_at for r in members if r.pid in ("RPM", "SPEED", "MAF")}
        assert len(tuple_arrivals) == 1, "the tuple must come back in one response"
        # Six members, four per batch: two responses, the tuple whole in one.
        assert len({r.acquired_at for r in members}) == 2


# ---------------------------------------------------------------------------
# C02.10 — timeouts, refusals, partial batches stay bounded
# ---------------------------------------------------------------------------


def test_timeouts_refusals_and_partial_batches_keep_rounds_bounded(
    clock: FakeClock, profile_reset: object
) -> None:
    channels = ("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS")
    # SPEED never answers (timeout, then miss backoff); ENGINE_LOAD is refused
    # single-PID and omitted from batches (partial batch).
    bus = ScriptedEcu(clock, silent_pids={0x0D}, nrc_pids={0x04})
    tuning_profile.activate(_profile("faulty", dict.fromkeys(channels, 1.0), target_rate_hz=5.0))
    readings, stats = _run(clock, bus, _spec(pids=channels, batch_max_pids=6), seconds=3.0)

    assert stats.rounds_started > 0
    assert stats.rounds_started == stats.rounds_complete + stats.rounds_incomplete
    assert stats.request_timeouts > 0
    assert {r.pid for r in readings} >= {"RPM", "THROTTLE_POS"}
    assert "SPEED" not in {r.pid for r in readings}
    for members in _by_round(readings).values():
        assert stamp_span_seconds(members) <= 0.2
        assert len({r.pid for r in members}) == len(members)
    # The silent channel is retried only when it is due again, never twice in
    # one round: per-round request count for 0x0D is at most one.
    per_second: dict[int, int] = {}
    for sent_at, msg in bus.sent:
        data = bytes(msg.data)
        if data[1] == 0x01 and 0x0D in data[2 : 1 + data[0]]:
            per_second[int(sent_at)] = per_second.get(int(sent_at), 0) + 1
    assert per_second and max(per_second.values()) <= 5


# ---------------------------------------------------------------------------
# C02.7 — frame arrival clocks and multi-frame policy
# ---------------------------------------------------------------------------


def test_kernel_frame_stamp_is_preferred_and_userspace_is_the_fallback(clock: FakeClock) -> None:
    stamped, _ = _run(clock, ScriptedEcu(clock), _spec(), seconds=0.5)
    unstamped, _ = _run(clock, ScriptedEcu(clock, stamp_frames=False), _spec(), seconds=0.5)
    assert stamped and unstamped
    assert {r.acquired_clock for r in stamped} == {"kernel"}
    assert {r.acquired_clock for r in unstamped} == {"userspace"}
    # Outside a round-stamping profile the presentation stamp IS the arrival.
    assert all(r.timestamp == r.acquired_at for r in stamped + unstamped)


def test_multiframe_response_uses_first_frame_and_keeps_the_interval(clock: FakeClock) -> None:
    channels = ("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS", "TIMING_ADVANCE", "MAF")
    bus = ScriptedEcu(clock, frame_gap=0.002)
    readings, _ = _run(clock, bus, _spec(pids=channels, batch_max_pids=6), seconds=0.5)
    batched = [r for r in readings if r.response_interval_s is not None]
    assert batched, "a six-PID batch is a multi-frame response"
    # first frame + N consecutive frames -> completion is N gaps later; the
    # full six-PID response (15 bytes) needs two consecutive frames.
    intervals = {round(r.response_interval_s / bus.frame_gap) for r in batched}  # type: ignore[operator]
    assert intervals <= {1, 2} and 2 in intervals
    first_send = bus.sent[0][0]
    assert min(r.acquired_at for r in batched) == pytest.approx(  # type: ignore[type-var]
        clock.time() - (clock.monotonic() - first_send) + bus.latency, abs=1e-6
    )


# ---------------------------------------------------------------------------
# C02.8 — generation resets
# ---------------------------------------------------------------------------


def test_in_flight_request_keeps_the_generation_it_was_issued_in(
    clock: FakeClock, profile_reset: object
) -> None:
    bus = ScriptedEcu(clock)
    tuning_profile.activate(_profile("gen", {"RPM": 1.0, "SPEED": 1.0}, target_rate_hz=4.0))
    started = capture_generation.current()

    def bump_once(_msg: object) -> None:
        if capture_generation.current() == started + 1:
            capture_generation.bump("test:mid-flight")

    bus.on_send.append(bump_once)
    readings, _ = _run(clock, bus, _spec(), seconds=1.0)

    loop_generation = started + 1  # the loop start bumped once
    assert readings[0].generation == loop_generation
    assert readings[0].round_id == 0
    later = [r for r in readings[1:]]
    assert later and all(r.generation == loop_generation + 1 for r in later)
    assert later[0].round_id == 0, "numbering restarts under the new generation"


def test_profile_pause_passive_and_replay_transitions_bump_the_generation(
    profile_reset: object,
) -> None:
    before = capture_generation.current()
    tuning_profile.activate(_profile("bump", {"RPM": 1.0}))
    assert capture_generation.current() == before + 1
    tuning_profile.set_paused(True)
    tuning_profile.set_paused(True)  # unchanged: no new generation
    tuning_profile.set_paused(False)
    assert capture_generation.current() == before + 3
    try:
        tx_gate.set_passive(True, source="test")
        tx_gate.set_passive(True, source="test")
    finally:
        tx_gate.set_passive(False, source="test")
    assert capture_generation.current() == before + 5


# ---------------------------------------------------------------------------
# C02.6 / C02.12 — the persisted contract, through the real writer and readers
# ---------------------------------------------------------------------------


def test_round_trip_through_parquet_and_readers(
    clock: FakeClock, profile_reset: object, tmp_path: Path
) -> None:
    bus = ScriptedEcu(clock)
    tuning_profile.activate(
        _profile("persist", {"RPM": 1.0, "SPEED": 1.0, "MAF": 1.0}, target_rate_hz=4.0)
    )
    readings, _ = _run(
        clock, bus, _spec(pids=("RPM", "SPEED", "MAF"), batch_max_pids=2), seconds=2.0
    )
    session = tmp_path / "2026-09-07T000000"
    write_records_to_parquet([reading_to_record(r) for r in readings], session)

    raw = read_acquisition_frame(session)
    assert set(raw[TIMESTAMP_BASIS_COLUMN]) == {"acquired"}
    assert raw[ROUND_ID_COLUMN].notna().all()
    metadata = [json.loads(m) for m in raw["metadata_json"]]
    assert {m["ts_schema"] for m in metadata} == {TS_SCHEMA_VERSION}
    assert {m["acquired_clock"] for m in metadata} == {"kernel"}

    long = read_channels(
        session,
        ["obd:RPM", "obd:SPEED"],
        acquired_time=True,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert set(long[TIMESTAMP_BASIS_COLUMN]) == {"acquired"}
    # The reader's clock is the arrival, not the round stamp.
    arrivals = sorted(r.acquired_at for r in readings if r.pid == "RPM")  # type: ignore[type-var]
    got = sorted(long.loc[long["spec"] == "obd:RPM", "timestamp"].map(pd.Timestamp.timestamp))
    assert got == pytest.approx(arrivals, abs=1e-5)

    skew = round_skew(raw)
    assert len(skew) == len({(r.generation, r.round_id) for r in readings})
    assert (skew["skew_s"] >= 0).all() and skew["skew_s"].max() > 0
    assert skew["skew_s"].max() == pytest.approx(
        max(stamp_span_seconds(g) for g in group_by_stamp(readings).values()), abs=1e-6
    )


def test_legacy_rows_keep_their_documented_basis(tmp_path: Path) -> None:
    """Old files are read as before: no acquisition evidence is invented."""
    t0 = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    session = tmp_path / "2026-08-05T120000"
    write_records_to_parquet(
        [
            TelemetryRecord(timestamp=t0, source="obd", channel="RPM", value=1000.0),
            TelemetryRecord(
                timestamp=t0,
                source="obd",
                channel="SPEED",
                value=10.0,
                metadata={"acquired_at": t0.timestamp() + 0.02},
            ),
            # A round-tagged row without acquired_at is a round stamp even
            # with no manifest to say so.
            TelemetryRecord(
                timestamp=t0,
                source="obd",
                channel="MAF",
                value=3.0,
                metadata={"generation": 1, "round_id": 4},
            ),
        ],
        session,
    )
    raw = read_acquisition_frame(session)
    basis = dict(zip(raw["channel"], raw[TIMESTAMP_BASIS_COLUMN], strict=True))
    assert basis == {"RPM": "stored", "SPEED": "acquired", "MAF": "round"}
    assert round_skew(raw).empty, "no acquired_at beside the round id -> no skew evidence"


def test_a_shared_round_stamp_cannot_collapse_distinct_acquisitions(tmp_path: Path) -> None:
    """Pairing runs on acquired_at. Two channels stamped with the same round
    start but acquired 30 ms apart pair at 50 ms and refuse at 10 ms; the
    stamp alone would have called them simultaneous."""
    t0 = datetime(2026, 9, 7, 0, 0, tzinfo=UTC).timestamp()
    records: list[TelemetryRecord] = []
    for k in range(40):
        stamp = t0 + k * 0.25
        for channel, value, offset in (
            ("RPM", 800.0 + 25 * k, 0.004),
            ("MAF", 2.0 + 0.1 * k, 0.034),
        ):
            records.append(
                TelemetryRecord.from_epoch(
                    stamp,
                    source="obd",
                    channel=channel,
                    value=value,
                    metadata={
                        "ts_schema": 2,
                        "acquired_at": stamp + offset,
                        "acquired_clock": "kernel",
                        "generation": 1,
                        "round_id": k,
                    },
                )
            )
    session = tmp_path / "2026-09-07T000000"
    write_records_to_parquet(records, session)

    paired = compare_channels(
        session,
        "obd:RPM",
        "obd:MAF",
        policy=PairingPolicy.within("50ms"),
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert paired.n_pairs == 40
    assert paired.timestamp_basis_a == "acquired" == paired.timestamp_basis_b
    with pytest.raises(ComparisonRefusedError):
        compare_channels(
            session,
            "obd:RPM",
            "obd:MAF",
            policy=PairingPolicy.within("10ms"),
            bindings_path=BINDINGS,
            dids_path=DIDS,
        )


# ---------------------------------------------------------------------------
# C02.9 / C02.13 — consumers of the presentation stamp
# ---------------------------------------------------------------------------


def test_resampler_and_gap_flags_see_the_presentation_stamp_unchanged(tmp_path: Path) -> None:
    """Round-stamped rows and plain rows with the same stamps resample and
    gap-flag identically: the preprocessing pivot reads ``timestamp`` only."""
    t0 = datetime(2026, 9, 7, 0, 0, tzinfo=UTC).timestamp()

    def rows(with_rounds: bool) -> list[TelemetryRecord]:
        out: list[TelemetryRecord] = []
        for k in range(20):
            if 8 <= k < 13:
                continue  # a 5 s hole -> gap flags
            stamp = t0 + k
            for channel, value in (("RPM", 900.0 + k), ("SPEED", 10.0 + k)):
                metadata = (
                    {
                        "ts_schema": 2,
                        "acquired_at": stamp + 0.05,
                        "acquired_clock": "kernel",
                        "generation": 1,
                        "round_id": k,
                    }
                    if with_rounds
                    else None
                )
                out.append(
                    TelemetryRecord.from_epoch(
                        stamp, source="obd", channel=channel, value=value, metadata=metadata
                    )
                )
        return out

    frames = []
    for name, with_rounds in (("2026-09-07T000000", True), ("2026-09-07T010000", False)):
        session = tmp_path / name
        write_records_to_parquet(rows(with_rounds), session)
        wide = to_wide(read_session(session))
        resampled = resample_wide(wide, config=ResampleConfig(hz=1.0))
        frames.append(resampled.drop(columns=["session"]).reset_index(drop=True))
    pd.testing.assert_frame_equal(frames[0], frames[1])
    assert frames[0]["obd:RPM__gap"].any(), "the hole must still be flagged"


def test_scheduler_metrics_expose_round_accounting(clock: FakeClock) -> None:
    bus = ScriptedEcu(clock)
    spec = _spec()
    requests = build_poll_requests(bus, spec)  # type: ignore[arg-type]
    stats = CanObdLoopStats(rounds_started=3, rounds_complete=2, rounds_incomplete=1)
    rounds = RoundScheduler(generation=9, stats=stats)
    snapshot = rounds.snapshot(clock.monotonic())
    from src.collection.obd_scheduler import _initial_request_states

    metrics = _scheduler_metrics(
        requests, _initial_request_states(requests, spec, 0.0), spec, stats, 0.0, rounds=snapshot
    )
    payload = metrics.to_dict()["rounds"]
    assert payload == {
        "generation": 9,
        "period_s": None,
        "started": 3,
        "complete": 2,
        "incomplete": 1,
        "skipped": 0,
        "skew_max_s": 0.0,
        "last_skew_s": 0.0,
        "open": None,
    }


# ---------------------------------------------------------------------------
# verifier finding — only on-demand (rate 0) work active must not spin
# ---------------------------------------------------------------------------


def test_on_demand_only_work_keeps_receiving_and_issues_freeze_frames(
    clock: FakeClock,
) -> None:
    """Every finite-rate PID backed off + a triggered freeze frame.

    ``round_period_seconds`` is ``None`` (nothing has a cadence) while the
    freeze-frame requests still count as active. The first cut returned
    ``(None, None)`` there and the loop spun without a single ``recv`` —
    one core pegged and the broadcast tap starved for the whole backoff. The
    Mode 02 requests must still go out and the bus must keep being received.
    """
    bus = ScriptedEcu(clock, silent_pids=(0x0C, 0x0D))
    trigger = FreezeFrameTrigger()
    spec = _spec(freeze_frame_trigger=trigger, request_timeout_seconds=0.1)
    mode01_seen = 0

    def fire_after_both_pids_backed_off(msg: object) -> None:
        # Each silent PID backs off after its first miss; once both have been
        # asked, every finite-rate request is suppressed and only the
        # (rate-0) freeze frames can be active when the trigger fires.
        nonlocal mode01_seen
        data = bytes(msg.data)  # type: ignore[attr-defined]
        if len(data) > 1 and data[1] == 0x01:
            mode01_seen += 1
            if mode01_seen == 2:
                trigger.request()

    bus.on_send.append(fire_after_both_pids_backed_off)
    readings: list[ObdReading] = []
    stop = threading.Event()
    result: dict[str, object] = {}
    seconds = 12.0
    deadline = clock.monotonic() + seconds

    def run() -> None:
        result["stats"] = run_can_obd_loop(
            bus,  # type: ignore[arg-type]
            spec,
            on_frame=lambda _m: None,
            on_reading=readings.append,
            stop_event=stop,
            deadline_monotonic=deadline,
        )

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=10.0)
    spun = worker.is_alive()
    stop.set()
    worker.join(timeout=5.0)

    assert not spun, "loop never advanced the fake clock: it spun without receiving"
    assert clock.monotonic() >= deadline, "loop did not run out its deadline on the fake clock"
    mode02_sends = [msg for _t, msg in bus.sent if len(msg.data) > 1 and msg.data[1] == 0x02]
    assert mode02_sends, "triggered freeze frames were never requested"
    # Both PIDs missed once and backed off; the remaining ~11 s must have been
    # spent receiving, not spinning: the fake bus advances the clock only
    # inside recv(), so reaching the deadline proves it was called throughout.
    mode01_sends = [msg for _t, msg in bus.sent if len(msg.data) > 1 and msg.data[1] == 0x01]
    assert 0 < len(mode01_sends) <= 4
    assert readings == []
