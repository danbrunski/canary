"""D03.29 — the trip-end edge does not depend on sky view.

On 2026-09-23 the GPS reported ``fix_quality`` 0 on all 3,278 samples of a
27-minute drive. The collector withholds ``gps:speed`` without a fix and the
tracker read motion from ``gps:speed`` alone, so no trip started, none ended,
and neither ``digest.json`` nor ``digest_skipped.json`` was written — while
road speed sat on the bus the whole time. These tests pin:

* vehicle speed (Mode 01 PID 0x0D, or the binding ``bindings.yaml`` declares
  ``quantity: road_speed``) drives the edge, with GPS as the fallback;
* the road-speed source is data, not a CAN id in shared code, and the schema
  refuses a declaration that could not be read as km/h;
* a drive nobody could see end leaves ``no_speed_source`` on disk;
* a session with wheel speed and no GPS fix ends a trip and runs the digest.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.analysis.digest import REASON_NO_SPEED_SOURCE, write_digest
from src.analysis.gps_tracker import (
    MODE01_SPEED_CHANNELS,
    ROAD_SPEED_QUANTITY,
    GpsTracker,
    TripEnd,
)
from src.control.trip_end import handle_trip_end_off_loop, record_trip_end_unobservable
from src.daemon.drain import drain_queue_with_broadcast
from src.dashboard.paths import DEFAULT_BINDINGS_PATH
from src.decode.bindings import Bindings, load_bindings
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.storage.session_products import read_digest, read_digest_skipped
from src.telemetry import TelemetryRecord
from tests.builders import RICH_SESSION_END, digest_config, write_rich_session
from tests.fakes import slow_digest

#: A test vehicle's road-speed frame — deliberately NOT the Xterra's 0x6F7, so
#: nothing here can pass by accident against a hard-coded id.
_ROAD_ID = 0x3A0
_WINDOW = 10.0  # trip-end window for the tests, seconds


def _bindings(**overrides: Any) -> Bindings:
    signal = {
        "name": "TEST_ROAD_SPEED",
        "module": "ABS",
        "can_id": _ROAD_ID,
        "byte_start": 0,
        "byte_length": 2,
        "byte_order": "big",
        "scale": 0.0625,
        "offset": 0.0,
        "unit": "km/h",
        "confidence": "high",
        "plausible_min": 0.0,
        "plausible_max": 250.0,
        "quantity": "road_speed",
    }
    signal.update(overrides)
    return Bindings.model_validate({"schema_version": 1, "signals": [signal]})


def _wheel(epoch: float, kmh: float, can_id: int = _ROAD_ID) -> TelemetryRecord:
    raw = round(kmh / 0.0625)
    return TelemetryRecord.can(epoch, can_id, raw.to_bytes(2, "big") + bytes(6))


def _no_fix(epoch: float) -> TelemetryRecord:
    return TelemetryRecord.gps(epoch, "fix_quality", 0, "")


def _tracker(**kwargs: Any) -> GpsTracker:
    return GpsTracker(trip_end_stationary_seconds=_WINDOW, bindings=_bindings(), **kwargs)


def _feed(tracker: GpsTracker, records: Iterator[TelemetryRecord]) -> list[TripEnd]:
    """Feed through the sink filter, as the daemon does; collect the edges."""
    ends = []
    for record in records:
        if tracker.accepts(record):
            end = tracker.observe(record)
            if end is not None:
                ends.append(end)
    return ends


def _drive_without_fix(t0: float = 1_790_000_000.0) -> Iterator[TelemetryRecord]:
    """Moving 10 s, stopped 3 s, then key-off: wheel frames stop, the GPS keeps
    reporting fix health with no fix — the 2026-09-23 shape."""
    for i in range(40):  # 0.5 s steps: 20 s of frames
        epoch = t0 + i * 0.5
        yield _wheel(epoch, 40.0 if epoch < t0 + 10.0 else 0.0)
        if i % 2 == 0:
            yield _no_fix(epoch)
    for second in range(20, 40):  # key off: GPS fix-health rows only
        yield _no_fix(t0 + second)


# --- the source is data, and the data is readable --------------------------


def test_mode01_speed_channel_matches_the_pid_0x0d_decoder() -> None:
    from src.collection.can_obd_client import PID_DECODERS

    assert PID_DECODERS[0x0D][0] in MODE01_SPEED_CHANNELS


def test_the_repo_declares_exactly_one_road_speed_binding_with_a_pinned_scale() -> None:
    bindings = load_bindings(DEFAULT_BINDINGS_PATH)
    declared = [s for s in bindings.signals if s.quantity == ROAD_SPEED_QUANTITY]
    assert len(declared) == 1
    road = declared[0]
    assert road.scale is not None and road.unit == "km/h"
    assert bindings.quantity_binding(ROAD_SPEED_QUANTITY) is road


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"scale": None}, "no pinned scale"),
        ({"unit": "raw"}, "is not 'km/h'"),
    ],
)
def test_a_road_speed_declaration_that_cannot_read_km_h_is_refused(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _bindings(**overrides)


def test_two_bindings_cannot_both_be_the_road_speed() -> None:
    base = _bindings().signals[0].model_dump()
    twin = {**base, "name": "OTHER_ROAD_SPEED", "can_id": 0x3A1}
    with pytest.raises(ValidationError, match="more than one binding declares"):
        Bindings.model_validate({"schema_version": 1, "signals": [base, twin]})


# --- the edge -------------------------------------------------------------


def test_wheel_speed_with_no_gps_fix_ends_the_trip() -> None:
    ends = _feed(_tracker(), _drive_without_fix())
    assert len(ends) == 1
    assert ends[0].observed is True
    assert ends[0].speed_source == "can:TEST_ROAD_SPEED"


def test_the_same_drive_ends_nothing_without_a_declared_road_speed() -> None:
    """The regression shape: GPS-only reading of the same drive sees no trip."""
    tracker = GpsTracker(trip_end_stationary_seconds=_WINDOW)
    assert _feed(tracker, _drive_without_fix()) == []


def test_mode01_vehicle_speed_alone_ends_the_trip() -> None:
    tracker = GpsTracker(trip_end_stationary_seconds=_WINDOW)
    t0 = 1_790_000_000.0
    records = [TelemetryRecord.obd(t0, "SPEED", 50.0, "km/h")]
    records += [TelemetryRecord.obd(t0 + s, "SPEED", 0.0, "km/h") for s in range(1, 14)]
    ends = _feed(tracker, iter(records))
    assert [(e.observed, e.speed_source) for e in ends] == [(True, "obd:SPEED")]


def test_fresh_vehicle_speed_outranks_gps() -> None:
    """Parked with a jittery fix: GPS reading motion must not restart a trip
    the vehicle's own speed says is over."""
    tracker = _tracker()
    t0 = 1_790_000_000.0
    tracker.observe(_wheel(t0, 40.0))
    tracker.observe(_wheel(t0 + 1, 0.0))
    ends = []
    for second in range(2, 15):
        for record in (
            TelemetryRecord.gps(t0 + second, "speed", 12.0, "km/h"),
            _wheel(t0 + second, 0.0),
        ):
            end = tracker.observe(record)
            if end is not None:
                ends.append(end)
    assert [e.speed_source for e in ends] == ["can:TEST_ROAD_SPEED"]


def test_gps_still_ends_a_trip_when_there_is_no_vehicle_speed() -> None:
    tracker = _tracker()
    t0 = 1_790_000_000.0
    tracker.observe(TelemetryRecord.gps(t0, "speed", 40.0, "km/h"))
    ends = [
        tracker.observe(TelemetryRecord.gps(t0 + s, "speed", 0.0, "km/h")) for s in range(1, 13)
    ]
    fired = [e for e in ends if e is not None]
    assert [(e.observed, e.speed_source) for e in fired] == [(True, "gps:speed")]


def test_speed_lost_while_moving_ends_the_trip_as_unobservable() -> None:
    tracker = _tracker()
    t0 = 1_790_000_000.0
    tracker.observe(_wheel(t0, 60.0))
    ends = [tracker.observe(_no_fix(t0 + s)) for s in range(1, 45)]
    fired = [e for e in ends if e is not None]
    assert len(fired) == 1
    assert fired[0].observed is False
    assert "no usable speed source" in fired[0].detail
    assert "can:TEST_ROAD_SPEED" in fired[0].detail


def test_vehicle_speed_never_reaches_the_gps_odometer() -> None:
    """The Xterra's ECU/ABS speeds read ~8% under ground truth: fine for an
    edge, wrong for distance."""
    tracker = _tracker()
    for i in range(20):
        tracker.observe(_wheel(1_790_000_000.0 + i, 80.0))
    assert tracker.trip_status()["distance_km"] == 0.0


def test_the_sink_filter_takes_only_trip_inputs_and_decimates_road_speed() -> None:
    tracker = _tracker()
    t0 = 1_790_000_000.0
    assert tracker.accepts(_no_fix(t0))
    assert tracker.accepts(TelemetryRecord.obd(t0, "SPEED", 0.0, "km/h"))
    assert not tracker.accepts(TelemetryRecord.obd(t0, "RPM", 700.0, "rpm"))
    assert not tracker.accepts(_wheel(t0, 10.0, can_id=0x100))
    # 100 Hz road-speed frames reach the sink at 2 Hz.
    accepted = sum(tracker.accepts(_wheel(t0 + i * 0.01, 10.0)) for i in range(100))
    assert accepted == 2


# --- the session that could not be seen -----------------------------------


def test_a_session_blind_for_the_window_closes_as_unobservable() -> None:
    tracker = GpsTracker(trip_end_stationary_seconds=_WINDOW)
    for s in range(30):
        tracker.observe(_no_fix(1_790_000_000.0 + s))
    # A fix arrives once parked — the 2026-09-23 order of events.
    tracker.observe(TelemetryRecord.gps(1_790_000_031.0, "speed", 0.0, "km/h"))
    closed = tracker.close_session()
    assert closed is not None and closed.observed is False
    assert "gps fix_quality: 0 of 30 rows with a fix" in closed.detail
    assert "no binding declares quantity: road_speed" in closed.detail
    assert tracker.close_session() is None, "evidence resets with the session"


def test_a_session_whose_trip_ended_or_that_was_watched_closes_quietly() -> None:
    tracker = _tracker()
    _feed(tracker, _drive_without_fix())
    assert tracker.close_session() is None  # the edge fired in it

    watched = _tracker()
    for s in range(30):  # parked, speed readable the whole time
        watched.observe(_wheel(1_790_000_000.0 + s, 0.0))
    assert watched.close_session() is None


def test_record_trip_end_unobservable_writes_the_refusal_once(tmp_path: Path) -> None:
    session = tmp_path / "2026-09-23T142900"
    session.mkdir()
    config = digest_config(tmp_path)
    written = record_trip_end_unobservable(session, config, "no usable speed source")
    assert written is not None
    sidecar = read_digest_skipped(session)
    assert sidecar is not None
    assert sidecar["reason"] == REASON_NO_SPEED_SOURCE
    assert sidecar["detail"] == "no usable speed source"
    # An answer already on disk is never replaced.
    assert record_trip_end_unobservable(session, config, "second") is None
    assert read_digest_skipped(session)["detail"] == "no usable speed source"  # type: ignore[index]


def test_record_trip_end_unobservable_never_shadows_a_digest_or_a_replay(tmp_path: Path) -> None:
    digested = tmp_path / "2026-09-23T142900"
    digested.mkdir()
    write_digest(digested, slow_digest(0.0, [])(digested, digest_config(tmp_path)))
    assert record_trip_end_unobservable(digested, digest_config(tmp_path), "x") is None
    assert read_digest_skipped(digested) is None

    replay = tmp_path / "_replay-2026-09-23T142900"
    replay.mkdir()
    assert record_trip_end_unobservable(replay, digest_config(tmp_path), "x") is None


# --- through the daemon's drain -------------------------------------------


async def _drain(
    writer: ParquetTelemetryWriter,
    tracker: GpsTracker,
    *phases: list[TelemetryRecord],
    settle: float = 0.2,
    **hooks: Any,
) -> None:
    """Run the real drain; each phase is drained before the next is queued, so
    a rotation the storage sink performs is visible to the trip sink's next
    record (the sinks are independent tasks)."""
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(
        drain_queue_with_broadcast(
            queue,
            writer,
            stop,
            gps_tracker=tracker,
            active_session_path=lambda: writer.paths.root,
            **hooks,
        )
    )
    for phase in phases:
        for record in phase:
            await queue.put(record)
        await asyncio.sleep(settle)
    stop.set()
    await task


def _no_process_wide_rlimit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A thread-pool digest would apply the child's ``RLIMIT_AS`` to the whole
    test process (a thread is not a child) and starve every later test in the
    worker — the same guard ``test_trip_end_digest`` uses."""
    from src.control import trip_end

    monkeypatch.setattr(trip_end, "limit_address_space", lambda n: (n, n))


async def test_a_session_with_wheel_speed_and_no_gps_fix_ends_a_trip_and_runs_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The D03.29 acceptance: recorded through the real drain and writer, the
    trip ends on wheel speed alone and the trip-end digest lands."""
    writer = ParquetTelemetryWriter(
        tmp_path / "storage", session_started_at=datetime(2026, 9, 23, 14, 29, tzinfo=UTC)
    )
    t0 = datetime(2026, 9, 23, 14, 29, tzinfo=UTC).timestamp()
    ended: list[Path] = []
    unobservable: list[Path] = []
    await _drain(
        writer,
        _tracker(),
        list(_drive_without_fix(t0)),
        on_trip_ended=ended.append,
        on_trip_unobservable=lambda path, _end: unobservable.append(path),
    )
    writer.close()
    assert ended == [writer.paths.root]
    assert unobservable == []

    config = digest_config(tmp_path)
    _no_process_wide_rlimit(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        digest = await handle_trip_end_off_loop(
            ended[0], config, now=datetime(2026, 9, 23, 14, 35, tzinfo=UTC), executor=pool
        )
    assert digest is not None
    assert read_digest(ended[0]) is not None
    assert read_digest_skipped(ended[0]) is None


async def test_a_rotated_session_nobody_could_see_reports_no_speed_source(
    tmp_path: Path,
) -> None:
    writer = ParquetTelemetryWriter(
        tmp_path / "storage", session_started_at=datetime(2026, 9, 23, 14, 29, tzinfo=UTC)
    )
    t0 = datetime(2026, 9, 23, 14, 29, tzinfo=UTC).timestamp()
    first = writer.paths.root
    calls: list[tuple[Path, TripEnd]] = []
    await _drain(
        writer,
        GpsTracker(trip_end_stationary_seconds=_WINDOW),
        [_no_fix(t0 + s) for s in range(30)],
        # A key cycle: engine run time resets, the writer rotates the session.
        [
            TelemetryRecord.obd(t0 + 30, "RUN_TIME", 900.0, "s"),
            TelemetryRecord.obd(t0 + 31, "RUN_TIME", 1.0, "s"),
        ],
        [_no_fix(t0 + 32 + s) for s in range(3)],
        on_trip_unobservable=lambda path, end: calls.append((path, end)),
    )
    writer.close()
    assert writer.paths.root != first, "the fixture must actually rotate"
    assert [path for path, _ in calls] == [first]
    assert calls[0][1].observed is False


# --- the edge lands on the session the stop was seen in --------------------

#: Far enough ahead that the sink's wall-clock tick can never reach the end of
#: the window: these tests exercise the rotation path alone.
_FUTURE = datetime(2031, 1, 1, tzinfo=UTC).timestamp()


def _stop_then_silence(t0: float) -> list[TelemetryRecord]:
    """Moving 5 s, stopped 2 s, then nothing at all — a GPS-less install, or
    a receiver that emits no rows, after key-off."""
    return [_wheel(t0 + i * 0.5, 40.0 if i < 10 else 0.0) for i in range(15)]


def test_a_stopped_trip_ends_on_the_session_its_stop_was_seen_in() -> None:
    first, second = Path("2031-01-01T000000"), Path("2031-01-01T010000")
    tracker = _tracker()
    tracker.enter_session(first)
    for record in _stop_then_silence(_FUTURE):
        assert tracker.observe(record) is None
    assert tracker.close_session() is None, "a rotation does not end the trip"
    tracker.enter_session(second)
    end = tracker.observe(_wheel(_FUTURE + 3600, 0.0))
    assert end is not None and end.observed is True
    assert end.session == first
    assert end.sessions == (first,)
    assert end.stopped_epoch == pytest.approx(_FUTURE + 5.0)


def test_close_session_leaves_a_moving_trip_open() -> None:
    tracker = _tracker()
    tracker.observe(_wheel(_FUTURE, 40.0))
    assert tracker.close_session() is None
    tracker.observe(_wheel(_FUTURE + 1, 0.0))
    end = tracker.observe(_wheel(_FUTURE + 12, 0.0))
    assert end is not None and end.observed


def test_tick_ends_a_stopped_trip_through_silence() -> None:
    tracker = _tracker()
    for record in _stop_then_silence(_FUTURE):
        tracker.observe(record)
    assert tracker.tick(_FUTURE + 8) is None  # 3 s after the stop
    end = tracker.tick(_FUTURE + 16)  # 11 s after the stop
    assert end is not None and end.observed is True
    assert tracker.tick(_FUTURE + 100) is None, "one edge per trip"


def test_a_replay_is_isolated_from_the_live_trip() -> None:
    tracker = _tracker()
    tracker.observe(_wheel(_FUTURE, 0.0))  # live: parked, no trip
    tracker.suspend()
    tracker.observe(_wheel(1_700_000_000.0, 80.0))  # replay: mid-drive
    assert tracker.tick(_FUTURE + 10_000) is None, "no wall-clock tick in a replay"
    tracker.resume()
    # The replayed "moving" state is gone: live silence ends nothing.
    ends = [tracker.observe(_no_fix(_FUTURE + s)) for s in range(1, 60)]
    assert [e for e in ends if e is not None] == []


async def test_a_stop_then_silence_is_digested_on_its_own_session_not_the_next(
    tmp_path: Path,
) -> None:
    """Verifier repro: road-speed stop, no further records, a key cycle, then a
    0 km/h frame in the new session. The edge used to fire on that frame and
    digest the NEW session; the finished drive got nothing."""
    writer = ParquetTelemetryWriter(
        tmp_path / "storage", session_started_at=datetime.fromtimestamp(_FUTURE, tz=UTC)
    )
    first = writer.paths.root
    ended: list[Path] = []
    await _drain(
        writer,
        _tracker(),
        _stop_then_silence(_FUTURE),
        [
            TelemetryRecord.obd(_FUTURE + 60, "RUN_TIME", 900.0, "s"),
            TelemetryRecord.obd(_FUTURE + 61, "RUN_TIME", 1.0, "s"),
        ],
        [_wheel(_FUTURE + 62, 0.0)],
        on_trip_ended=ended.append,
    )
    writer.close()
    assert writer.paths.root != first, "the fixture must actually rotate"
    assert ended == [first]


class _Sink:
    """``run_gps_sink`` driven directly: a settable session path and a fake
    monotonic clock, so idle ticks are deterministic."""

    def __init__(self, tracker: GpsTracker, session: Path) -> None:
        from src.daemon.drain import run_gps_sink

        self.tracker = tracker
        self.session = session
        self.now = 0.0
        self.ended: list[Path | None] = []
        self.ends: list[TripEnd] = []
        self.unobservable: list[tuple[Path, TripEnd]] = []
        self.queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(
            run_gps_sink(
                self.queue,
                tracker,
                self.stop,
                poll_interval=0.02,
                active_session_path=lambda: self.session,
                on_trip_ended=lambda path, end: (self.ended.append(path), self.ends.append(end)),
                on_trip_unobservable=lambda path, end: self.unobservable.append((path, end)),
                clock=lambda: self.now,
            )
        )

    async def put(self, *records: TelemetryRecord) -> None:
        for record in records:
            await self.queue.put(record)
        await self.queue.join()

    async def idle(self, seconds: float, *, steps: int = 4) -> None:
        """Let the queue idle while the monotonic clock advances ``seconds``."""
        for _ in range(steps):
            self.now += seconds / steps
            await asyncio.sleep(0.06)

    async def close(self) -> None:
        self.stop.set()
        await self.task


async def test_key_off_silence_ends_the_trip_on_the_idle_tick() -> None:
    """No record after the stop and no rotation: the idle tick ends the trip,
    on the session the stop was seen in."""
    session = Path("2031-01-01T000000")
    sink = _Sink(_tracker(), session)
    await sink.put(*_stop_then_silence(_FUTURE))
    await sink.idle(3.0)
    assert sink.ended == [], "3 s after the stop is inside the window"
    await sink.idle(10.0)
    await sink.close()
    assert sink.ended == [session]


async def test_a_record_clock_far_from_the_wall_clock_cannot_end_a_trip_by_tick() -> None:
    """The tick runs on record time + monotonic elapsed, never ``time.time()``:
    a wall-clock step (or records stamped long ago) must not end or split a
    trip. Stamped in 1970, the pre-fix wall-clock tick ended this at once."""
    sink = _Sink(_tracker(), Path("2031-01-01T000000"))
    await sink.put(_wheel(1_000.0, 40.0), _wheel(1_001.0, 0.0))
    await sink.idle(2.0)
    await sink.close()
    assert sink.ended == [] and sink.unobservable == []


async def test_a_quiet_session_that_never_moved_is_not_reported_as_blind() -> None:
    """Verifier repro D: 0 km/h for 60 s (driveway idle), an hour of idle
    ticks, then a rotation. No trip happened, and that is what it says."""
    first, second = Path("2031-01-01T000000"), Path("2031-01-01T010000")
    sink = _Sink(_tracker(), first)
    await sink.put(*[_wheel(_FUTURE + i * 0.5, 0.0) for i in range(120)])
    await sink.idle(3600.0)
    sink.session = second
    await sink.put(_wheel(_FUTURE + 3700, 0.0))
    await sink.close()
    assert sink.ended == []
    assert sink.unobservable == []


def test_close_session_after_ticks_on_a_parked_session_is_none() -> None:
    tracker = _tracker()
    for i in range(120):
        tracker.observe(_wheel(_FUTURE + i * 0.5, 0.0))
    for minutes in range(1, 61):
        assert tracker.tick(_FUTURE + 60 * minutes) is None
    assert tracker.close_session() is None


def test_silence_after_motion_is_still_blind_at_rotation() -> None:
    """The stretch that *does* count: last seen moving, then nothing."""
    tracker = GpsTracker(trip_end_stationary_seconds=_WINDOW)
    tracker.observe(TelemetryRecord.obd(_FUTURE, "SPEED", 50.0, "km/h"))
    for s in range(1, 30):
        tracker.observe(_no_fix(_FUTURE + s))
    closed = tracker.close_session()
    assert closed is not None and closed.observed is False


async def test_the_sink_reports_nothing_for_a_replay_and_resumes_the_live_trip() -> None:
    live, replay, after = (
        Path("2031-01-01T000000"),
        Path("_replay-abc"),
        Path("2031-01-01T010000"),
    )
    tracker = _tracker()
    sink = _Sink(tracker, live)
    await sink.put(_wheel(_FUTURE, 0.0))  # live, parked
    sink.session = replay
    # A replayed drive that ends in a stop, then is cut off mid-drive.
    await sink.put(*_stop_then_silence(1_700_000_000.0), _wheel(1_700_000_020.0, 0.0))
    await sink.put(_wheel(1_700_000_030.0, 90.0))
    await sink.idle(600.0)
    sink.session = after
    await sink.put(*[_no_fix(_FUTURE + s) for s in range(1, 60)])
    await sink.idle(600.0)
    await sink.close()
    assert sink.ended == [], "the replayed stop is not a live trip end"
    assert sink.unobservable == [], "the replay's 'moving' does not leak into the live session"
    assert not tracker.suspended


# --- a trip spans sessions; the digest names them --------------------------

_CAPTURE = Path("_capture-tuning-2031-01-01T000000")


async def test_a_capture_stopped_after_the_stop_digests_the_capture_not_the_parked_session() -> (
    None
):
    """canary-17, 2026-09-23: moving in the tuning capture, stop at 16:52:30,
    the capture stops and the writer rotates to a parked ``default`` session
    at 16:53:16, then parked. The edge fired 300 s after the stop and
    digested the 257 s parked session; the drive in the capture got nothing."""
    parked = Path("2031-01-01T000046")
    sink = _Sink(_tracker(), _CAPTURE)
    moving = [_wheel(_FUTURE + i * 0.5, 40.0) for i in range(60)]  # 30 s moving
    await sink.put(*moving, _wheel(_FUTURE + 30, 0.0), _wheel(_FUTURE + 35, 0.0))
    sink.session = parked  # the capture stopped; rotation while stationary
    await sink.put(*[_wheel(_FUTURE + 46 + s, 0.0) for s in range(3)])
    await sink.idle(20.0)  # parked past the 10 s window
    await sink.close()
    assert sink.ended == [_CAPTURE], "the drive's session, never the parked one"
    assert sink.ends[0].sessions == (_CAPTURE,)
    assert sink.unobservable == []


async def test_a_trip_spanning_a_capture_boundary_names_every_session() -> None:
    """A capture stopped mid-drive: the trip ends in the next session, and the
    end names both — the capture is not silently dropped."""
    after = Path("2031-01-01T000500")
    sink = _Sink(_tracker(), _CAPTURE)
    await sink.put(*[_wheel(_FUTURE + i, 50.0) for i in range(10)])
    sink.session = after  # capture stopped while moving
    await sink.put(*[_wheel(_FUTURE + 10 + i, 50.0) for i in range(10)])
    await sink.put(*[_wheel(_FUTURE + 20 + i, 0.0) for i in range(15)])
    await sink.close()
    assert sink.ended == [after]
    assert sink.ends[0].sessions == (_CAPTURE, after)
    assert sink.ends[0].started_epoch == pytest.approx(_FUTURE)


def test_the_trip_block_orders_sessions_by_start_time_not_name() -> None:
    """``_capture-…`` sorts after ``2031-…`` as a string; by time it is first."""
    from src.control.trip_end import trip_block

    earlier_live = Path("2030-12-31T235000")
    after = Path("2031-01-01T000500")
    end = TripEnd(
        observed=True,
        speed_source="can:TEST_ROAD_SPEED",
        detail="stationary for 300 s",
        session=after,
        sessions=(_CAPTURE, after, earlier_live),
        started_epoch=_FUTURE - 600,
        stopped_epoch=_FUTURE + 400,
    )
    block = trip_block(end, after)
    assert block["sessions"] == [earlier_live.name, _CAPTURE.name, after.name]
    assert block["end_session"] == after.name
    assert block["coverage"] == "end_session_only"
    assert block["started_at"] is not None and block["stopped_at"] is not None


async def test_a_capture_session_digests_with_its_trip_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``handle_trip_end_off_loop`` accepts a ``_capture-*`` directory ("which
    session is live" includes captures) and writes the trip block into it."""

    capture = write_rich_session(tmp_path, name="_capture-tuning-2026-05-20T180000")
    parked = tmp_path / "2026-05-20T181000"
    end = TripEnd(
        observed=True,
        speed_source="can:TEST_ROAD_SPEED",
        detail="stationary for 300 s on can:TEST_ROAD_SPEED",
        session=capture,
        sessions=(capture,),
        started_epoch=RICH_SESSION_END.timestamp() - 300,
        stopped_epoch=RICH_SESSION_END.timestamp() - 60,
    )
    _no_process_wide_rlimit(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        digest = await handle_trip_end_off_loop(
            capture, digest_config(tmp_path), trip=end, now=RICH_SESSION_END, executor=pool
        )
    assert digest is not None
    written = read_digest(capture)
    assert written is not None
    assert written["trip"]["end_session"] == capture.name
    assert written["trip"]["sessions"] == [capture.name]
    assert written["trip"]["coverage"] == "whole_trip"
    assert not parked.exists()


def _components(tracker: GpsTracker) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(gps_tracker=tracker)


def test_the_shutdown_fallback_digests_the_open_trip_not_the_active_session() -> None:
    from src.daemon.runtime import shutdown_trip_target

    parked = Path("2031-01-01T000046")
    tracker = _tracker()
    tracker.enter_session(_CAPTURE)
    tracker.observe(_wheel(_FUTURE, 40.0))
    tracker.observe(_wheel(_FUTURE + 5, 0.0))
    tracker.close_session()
    tracker.enter_session(parked)
    tracker.observe(_wheel(_FUTURE + 6, 0.0))
    target, trip = shutdown_trip_target(_components(tracker), parked)
    assert target == _CAPTURE
    assert trip is not None and trip.sessions == (_CAPTURE,)


def test_the_shutdown_fallback_skips_a_parked_session_the_trip_never_touched() -> None:
    from src.daemon.runtime import shutdown_trip_target

    parked = Path("2031-01-01T000046")
    tracker = _tracker()
    tracker.enter_session(parked)
    for s in range(30):
        tracker.observe(_wheel(_FUTURE + s, 0.0))
    assert shutdown_trip_target(_components(tracker), parked) == (None, None)


def test_the_shutdown_fallback_keeps_digesting_what_it_cannot_read() -> None:
    from src.daemon.runtime import shutdown_trip_target

    active = Path("2031-01-01T000046")
    tracker = _tracker()
    tracker.enter_session(active)
    tracker.observe(_no_fix(_FUTURE))  # no usable speed at all
    assert shutdown_trip_target(_components(tracker), active) == (active, None)


def _trip_ended_by_tick_then_parked(parked: Path) -> GpsTracker:
    """Road-speed trip in the capture, edge fired by the idle tick, then a
    rotation into a parked session (the round-3 no-fix probe)."""
    tracker = _tracker()
    tracker.enter_session(_CAPTURE)
    for record in _stop_then_silence(_FUTURE):
        tracker.observe(record)
    end = tracker.tick(_FUTURE + 60)
    assert end is not None and end.observed and end.session == _CAPTURE
    assert tracker.close_session() is None
    tracker.enter_session(parked)
    return tracker


def test_the_shutdown_fallback_skips_a_parked_session_with_no_fix_after_the_trip_ended() -> None:
    from src.daemon.runtime import shutdown_trip_target

    parked = Path("2031-01-01T000200")
    tracker = _trip_ended_by_tick_then_parked(parked)
    for s in range(120):
        tracker.observe(_no_fix(_FUTURE + 120 + s))  # key off; GPS without a fix
    assert shutdown_trip_target(_components(tracker), parked) == (None, None)


def test_the_shutdown_fallback_skips_a_silent_parked_session_after_the_trip_ended() -> None:
    from src.daemon.runtime import shutdown_trip_target

    parked = Path("2031-01-01T000200")
    tracker = _trip_ended_by_tick_then_parked(parked)  # and then nothing at all
    assert shutdown_trip_target(_components(tracker), parked) == (None, None)


def test_a_key_off_parked_session_is_no_trip_not_a_blind_one() -> None:
    """Speed has been readable since start and the parked session never
    produced a usable reading: the key was off. Closing it (rotation or
    shutdown) files no ``no_speed_source`` refusal -- that would be every
    key-off -- while a never-readable install still does."""
    parked = Path("2031-01-01T000200")
    tracker = _trip_ended_by_tick_then_parked(parked)
    for s in range(0, 900, 5):
        tracker.observe(_no_fix(_FUTURE + 120 + s))  # 15 min parked, GPS without a fix
    assert tracker.close_session() is None


def test_the_shutdown_fallback_still_digests_a_session_that_moved() -> None:
    from src.daemon.runtime import shutdown_trip_target

    later = Path("2031-01-01T000200")
    tracker = _trip_ended_by_tick_then_parked(later)
    tracker.observe(_wheel(_FUTURE + 130, 40.0))
    target, trip = shutdown_trip_target(_components(tracker), later)
    assert target == later and trip is not None, "an open trip digests on its own session"


def test_replayed_speed_does_not_make_an_install_readable() -> None:
    from src.daemon.runtime import shutdown_trip_target

    active = Path("2031-01-01T000046")
    tracker = _tracker()
    tracker.enter_session(active)
    tracker.suspend()
    tracker.observe(_wheel(1_700_000_000.0, 0.0))
    tracker.resume()
    tracker.observe(_no_fix(_FUTURE))
    assert shutdown_trip_target(_components(tracker), active) == (active, None)
