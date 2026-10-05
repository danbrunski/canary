"""The target-phi twin on the live tick (E04.1) and its parity with the stored
evaluator (E04.2).

One evaluator: the live stream (:mod:`src.analysis.target_phi_stream`) calls
the stored evaluator's per-candidate judgement over a bounded window, so the
same accepted reads must give the same records -- tuples, gates, states,
predictions, differences, qualified residuals -- and the same counts.
These tests hold it to that on the committed October 4 slice and on synthetic
sessions that reach every named state, under changed arrival order, other
tick cadences, late reads, gaps, restarts, sentinels, mode transitions and
identity changes; and hold the daemon adapter to its runtime contract: the
record path only appends, a stalled worker costs counted reads and never a
backlog, and nothing is written anywhere.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Executor, Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from src.analysis.target_phi import ResolvedTable, TwinRefusalError
from src.control.twin_identity import IdentitySegment, TwinIdentity
from src.daemon.inference_metrics import InferenceTimingRecorder
from src.daemon.twin_live import (
    STAGE_EVAL,
    STAGE_IDENTITY,
    STAGE_TICK,
    LiveTwin,
    _InlineExecutor,
    build_live_twin,
)
from src.decode.dids import load_did_map
from src.features.live import LiveFeatureSource
from src.paths import DIDS_YAML
from src.telemetry import TelemetryRecord
from src.vehicle.profiles import VehicleProfile, scope_config
from tests.configs import isolated_config
from tests.test_brain_jobs import SLUG
from tests.test_lambda_delivery_live import _records
from tests.test_target_phi import (
    EXPECT,
    IMAGE_SHA,
    NAMED_STATES,
    _definition,
    _evaluate,
    _hysteresis_table,
    _session,
    _table,
    fixture_frame,
)

CATALOG = load_did_map(DIDS_YAML)
REVISION = EXPECT["calibration"]["revision"]
IDENTITY = TwinIdentity(REVISION, IMAGE_SHA, "registry", False, "known", "program")


class FakeResolver:
    """The shared resolver's contract with injected segments and tables.

    ``segments`` maps the asked instant to segments; ``table`` maps an
    identity to its default-slot table (or raises its refusal). Every other
    slot refuses ``table_unverified``, as the real profile's slots 2-5 do."""

    def __init__(
        self,
        table: ResolvedTable | None = None,
        *,
        segments: Any = None,
        table_for: Any = None,
    ) -> None:
        self.definition, self.definition_sha256 = _definition()
        chosen = table or _table(self.definition)
        self._segments = segments or (lambda _at: [IdentitySegment(None, None, IDENTITY)])
        self._table_for = table_for or (lambda _identity: chosen)
        self.asked: list[float | None] = []

    def segments(self, session: Path | None, *, first_read: float | None) -> list[Any]:
        self.asked.append(first_read)
        return list(self._segments(first_read))

    def table(self, identity: TwinIdentity, slot: int) -> ResolvedTable:
        if slot != self.definition.map_identity.default_slot.slot:
            raise TwinRefusalError("table_unverified", f"slot {slot} is inferred")
        return self._table_for(identity)


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    return fixture_frame()


@pytest.fixture(scope="module")
def stored(frame: pd.DataFrame) -> dict[str, Any]:
    return _evaluate(frame)


def _twin(
    table: ResolvedTable | None = None,
    *,
    resolver: FakeResolver | None = None,
    recorder: InferenceTimingRecorder | None = None,
    executor: Executor | None = None,
    session: str = "s",
    capacity: int | None = None,
    recheck_s: float = 60.0,
    by_segment: dict[int, list[dict[str, Any]]] | None = None,
) -> tuple[LiveTwin, list[dict[str, Any]]]:
    """A live twin on a fake resolver, preloaded; returns it and the list its
    default-slot records land in (``by_segment`` splits them per segment)."""
    chosen = resolver or FakeResolver(table)
    out: list[dict[str, Any]] = []

    def sink(segment: int, slot: int, records: list[dict[str, Any]]) -> None:
        if slot == chosen.definition.map_identity.default_slot.slot:
            out.extend(records)
            if by_segment is not None:
                by_segment.setdefault(segment, []).extend(records)

    kwargs: dict[str, Any] = {}
    if capacity is not None:
        kwargs["intake_capacity"] = capacity
    twin = LiveTwin(
        resolver=lambda: chosen,
        recorder=recorder,
        did_map=CATALOG,
        catalog=CATALOG,
        session=session,
        executor=executor or _InlineExecutor(),
        identity_recheck_s=recheck_s,
        on_records=sink,
        **kwargs,
    )
    twin.tick(session=session, vehicle_state=None)  # the worker preloads
    return twin, out


def _drive(
    twin: LiveTwin,
    records: Iterator[TelemetryRecord] | list[TelemetryRecord],
    *,
    tick_s: float = 1.0,
    session: str = "s",
    flush: bool = True,
) -> None:
    """Records into the intake, one tick per ``tick_s`` of stored time."""
    next_tick: datetime | None = None
    for record in records:
        if next_tick is None:
            next_tick = record.timestamp + timedelta(seconds=tick_s)
        while record.timestamp >= next_tick:
            twin.tick(session=session, vehicle_state="engine_running")
            next_tick += timedelta(seconds=tick_s)
        twin.intake.observe(record)
    twin.tick(session=session, vehicle_state="engine_running")
    if flush:
        twin.flush(session=session)


def _same_as_stored(live: list[dict[str, Any]], stored: dict[str, Any], twin: LiveTwin) -> None:
    assert live == stored["records"]
    block = twin.snapshot()
    accounting = {k: v for k, v in stored["accounting"].items() if k != "input_pairing"}
    assert block["accounting"] == accounting
    assert block["applicability"]["coverage"] == stored["coverage"]
    assert block["applicability"]["capture_requirement"] == stored["capture_requirement"]
    assert block["applicability"]["qualification_blockers"] == stored["qualification_blockers"]
    assert block["provenance"] == stored["provenance"]
    paired = {name: v["paired"] for name, v in stored["accounting"]["input_pairing"].items()}
    assert block["stream"]["paired"] == paired


# -- E04.2: parity on the October 4 slice ------------------------------------------


def test_the_live_path_gives_the_stored_records_on_october_4(
    tmp_path: Path, frame: pd.DataFrame, stored: dict[str, Any]
) -> None:
    """Records through a real LiveFeatureSource's observer seam, one tick per
    data second: the 174 records the stored evaluator mints, field for field."""
    twin, live = _twin()
    source = LiveFeatureSource(config=isolated_config(tmp_path, profiles={"enabled": False}))
    twin.attach(source)
    next_tick: datetime | None = None
    for record in _records(frame):
        if next_tick is None:
            next_tick = record.timestamp + timedelta(seconds=1)
        while record.timestamp >= next_tick:
            twin.tick(session="s", vehicle_state="engine_running")
            next_tick += timedelta(seconds=1)
        source.observe(record)
    twin.flush(session="s")
    assert len(live) == EXPECT["accounting"]["candidates"] == 174
    _same_as_stored(live, stored, twin)
    block = twin.snapshot()
    assert block["stream"]["late_reads"] == dict.fromkeys(block["stream"]["late_reads"], 0)
    assert block["intake"]["dropped"] == 0 and block["intake"]["observe_failures"] == 0
    assert block["result"] == "no_eligible_samples"
    assert block["applicability"]["qualified"] is False


@pytest.mark.parametrize("tick_s", [0.25, 5.0])
def test_the_tick_cadence_changes_nothing(
    frame: pd.DataFrame, stored: dict[str, Any], tick_s: float
) -> None:
    twin, live = _twin()
    _drive(twin, _records(frame), tick_s=tick_s)
    _same_as_stored(live, stored, twin)


def test_reads_arriving_out_of_order_within_the_settle_margin_change_nothing(
    frame: pd.DataFrame, stored: dict[str, Any]
) -> None:
    """Shuffle arrival inside 1.5 s blocks (the settle margin is 2 s); stamps
    are unchanged, so the decided records are."""
    records = list(_records(frame))
    rng = random.Random(4)
    shuffled: list[TelemetryRecord] = []
    block: list[TelemetryRecord] = []
    start = records[0].timestamp
    for record in records:
        if (record.timestamp - start).total_seconds() >= 1.5:
            rng.shuffle(block)
            shuffled += block
            block, start = [], record.timestamp
        block.append(record)
    rng.shuffle(block)
    shuffled += block
    assert shuffled != records
    twin, live = _twin()
    # Tick by arrival, not by stamp: a shuffled stream is not monotonic.
    for i, record in enumerate(shuffled):
        twin.intake.observe(record)
        if i % 50 == 0:
            twin.tick(session="s", vehicle_state="engine_running")
    twin.flush(session="s")
    _same_as_stored(live, stored, twin)
    assert sum(twin.snapshot()["stream"]["late_reads"].values()) == 0


def test_a_late_read_is_counted_and_dropped_never_folded_in(frame: pd.DataFrame) -> None:
    """Hold back a stretch of rpm-word and flag reads until long after the
    horizon passed them: they are counted late, and the records are exactly
    the stored evaluator's over the reads the stream accepted."""
    records = list(_records(frame))
    start = records[0].timestamp
    held = [
        r
        for r in records
        if r.channel in ("mem:ECM_ENGINE_SPEED_RAM", "mem:ECM_PEDAL_ENRICH_FLAGS")
        and timedelta(minutes=5) <= r.timestamp - start <= timedelta(minutes=8)
    ]
    assert held
    held_ids = {id(r) for r in held}
    on_time = [r for r in records if id(r) not in held_ids]
    twin, live = _twin()
    _drive(twin, on_time, flush=False)
    for record in held:
        twin.intake.observe(record)
    twin.tick(session="s", vehicle_state="engine_running")
    twin.flush(session="s")
    late = twin.snapshot()["stream"]["late_reads"]
    assert late["mem:ECM_ENGINE_SPEED_RAM"] + late["mem:ECM_PEDAL_ENRICH_FLAGS"] == len(held)
    accepted = frame[
        ~(
            frame["channel"].isin(["mem:ECM_ENGINE_SPEED_RAM", "mem:ECM_PEDAL_ENRICH_FLAGS"])
            & (frame["timestamp"] - frame["timestamp"].iloc[0] >= pd.Timedelta(minutes=5))
            & (frame["timestamp"] - frame["timestamp"].iloc[0] <= pd.Timedelta(minutes=8))
        )
    ].reset_index(drop=True)
    _same_as_stored(live, _evaluate(accepted), twin)


def test_a_gap_is_named_on_the_samples_inside_it_never_bridged(frame: pd.DataFrame) -> None:
    """Every identity read missing for four minutes: the samples there are
    ``map_identity_unbracketed`` on both paths -- no carried-forward slot."""
    seconds = (frame["timestamp"] - frame["timestamp"].iloc[0]).dt.total_seconds()
    identity = frame["channel"].isin(
        ["mem:ECM_UPREV_MAGIC", "mem:ECM_ACTIVE_MAP", "mem:ECM_UPREV_MAP_SELECTOR"]
    )
    gapped = frame[~(identity & (seconds >= 300) & (seconds <= 540))].reset_index(drop=True)
    stored = _evaluate(gapped)
    twin, live = _twin()
    _drive(twin, _records(gapped))
    _same_as_stored(live, stored, twin)
    inside = [
        r
        for r in live
        if 312
        <= (pd.Timestamp(r["sample"]["acquired_at"]) - frame["timestamp"].iloc[0]).total_seconds()
        <= 528
    ]
    assert inside
    assert all("map_identity_unbracketed" in r["reasons"] for r in inside)
    assert all(r["predicted_word"] is None for r in inside)


def test_a_restart_starts_empty_and_names_what_it_cannot_know(frame: pd.DataFrame) -> None:
    """A twin started half way through equals the stored evaluator over the
    reads after its start: nothing before it is invented, and its block says
    transient state before its first read is unknown."""
    half = (
        frame["timestamp"].iloc[0] + (frame["timestamp"].iloc[-1] - frame["timestamp"].iloc[0]) / 2
    )
    after = frame[frame["timestamp"] >= half].reset_index(drop=True)
    twin, live = _twin()
    _drive(twin, _records(after))
    _same_as_stored(live, _evaluate(after), twin)
    whole = {r["sample"]["acquired_at"]: r for r in _evaluate(frame)["records"]}
    first = live[0]
    assert first["sample"]["acquired_at"] in whole
    # The first sample's before-side identity read lay before the restart.
    assert whole[first["sample"]["acquired_at"]]["map_identity"]["before"] is not None
    assert first["map_identity"]["before"] is None
    assert first["reason"] == "map_identity_unbracketed"
    transient = twin.snapshot()["transient"]
    assert (transient["state"], transient["reason"]) == ("unknown_before", "not_reconstructible")


# -- E04.2: every named state, sentinels and mode transitions -----------------------


@pytest.mark.parametrize(
    ("kwargs", "edit", "evaluate", "state", "reason"),
    NAMED_STATES,
    ids=[f"{state}-{reason}" for _, _, _, state, reason in NAMED_STATES],
)
def test_every_named_state_is_decided_the_same_live(
    kwargs: dict[str, Any], edit: Any, evaluate: dict[str, Any], state: str, reason: str
) -> None:
    session = _session(**kwargs)
    if edit is not None:
        session = edit(session)
    evaluate = dict(evaluate)
    table = _hysteresis_table() if evaluate.pop("table", None) == "hysteresis" else None
    twin, live = _twin(table)
    if evaluate.get("session_complete") is False:
        # A capture still recording: the sample is pending until reads past
        # its decision horizon arrive, and counted as such.
        _drive(twin, _records(session), flush=False)
        assert live == []
        assert twin.snapshot()["stream"]["pending"] == 1
        return
    _drive(twin, _records(session))
    _same_as_stored(live, _evaluate(session, table=table), twin)
    (record,) = live
    assert (record["state"], record["reason"]) == (state, reason)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"word": None},
        {"fuel_status_after": 2.0},
        {"flag": 0, "load": 100},
        {"word": 19073},
    ],
    ids=["eligible-override", "sentinel", "mode-transition", "eligible-ordinary", "miss"],
)
def test_eligible_sentinel_and_mode_transition_samples_match(kwargs: dict[str, Any]) -> None:
    session = _session(**kwargs)
    twin, live = _twin()
    _drive(twin, _records(session))
    stored = _evaluate(session)
    _same_as_stored(live, stored, twin)
    if not kwargs or "word" in kwargs and kwargs["word"]:
        (record,) = live
        assert record["state"] == "eligible" and record["predicted_word"] == 19072


def test_several_samples_compete_for_reads_exactly_as_stored() -> None:
    """Two samples 0.1 s apart share one load read: one pairs, the other goes
    stale, on both paths, whichever tick decides each."""
    session = _session(extra_load_at=None)
    second = _session(t=30.1, load_at=None)
    both = (
        pd.concat([session, second[second["channel"] == "mem:ECM_TARGET_EQUIV_RATIO"]])
        .sort_values("timestamp", kind="stable")
        .reset_index(drop=True)
    )
    twin, live = _twin()
    _drive(twin, _records(both), tick_s=0.05)
    _same_as_stored(live, _evaluate(both), twin)
    assert sorted(r["reason"] or "" for r in live) == ["", "input_stale:load"]


# -- E04.2: identity changes and replay isolation -----------------------------------


CHANGE = pd.Timestamp("2026-10-04T21:00:38Z")
LATER = TwinIdentity("ZS44A-later", "e" * 64, "registry", False, "known", "program")


def _crossing_session() -> pd.DataFrame:
    """A 30 s sample on one calibration and an 80 s sample on the next."""
    first = _session(end=100.0)
    later = _session(t=80.0, end=100.0)
    tuple_rows = later[
        later["channel"].str.startswith("mem:")
        & ~later["channel"].isin(
            ["mem:ECM_UPREV_MAGIC", "mem:ECM_ACTIVE_MAP", "mem:ECM_UPREV_MAP_SELECTOR"]
        )
    ]
    return (
        pd.concat([first, tuple_rows])
        .sort_values("timestamp", kind="stable")
        .reset_index(drop=True)
    )


def test_a_flash_inside_a_session_is_two_segments_on_both_paths() -> None:
    """The flash history puts a flash at 38 s: the live path routes each read
    to the segment its own time falls in, flushes the first when the clock
    passes the flash, and decides each exactly as the stored evaluator's
    windowed evaluation of that segment, each on its own table."""
    session = _crossing_session()
    hysteresis = _hysteresis_table()
    definition, _sha = _definition()
    change = CHANGE.timestamp()
    resolver = FakeResolver(
        segments=lambda _at: [
            IdentitySegment(None, change, IDENTITY),
            IdentitySegment(change, None, LATER),
        ],
        table_for=lambda identity: hysteresis if identity == LATER else _table(definition),
    )
    segments: dict[int, list[dict[str, Any]]] = {}
    twin, _live = _twin(resolver=resolver, by_segment=segments)
    _drive(twin, _records(session))
    before = _evaluate(session, window=(None, change))
    after = _evaluate(session, table=hysteresis, window=(change, None))
    assert segments[0] == before["records"] and len(segments[0]) == 1
    assert segments[1] == after["records"] and len(segments[1]) == 1
    assert segments[1][0]["provenance"]["revision"] == hysteresis.revision
    block = twin.snapshot()
    assert [s["calibration"] for s in block["segments"]] == [REVISION, "ZS44A-later"]
    assert block["segments"][0]["finished"] is True
    assert block["identity"]["calibration"] == "ZS44A-later"


def test_a_flash_recorded_mid_session_is_picked_up_at_the_default_recheck(
    frame: pd.DataFrame,
) -> None:
    """The flash history changes while a session runs (a flash recorded
    after the session started). At the default 60 s recheck of data time the
    in-force segment is flushed and the rest of the session continues on the
    new calibration; past decisions are not re-made."""
    start = frame["timestamp"].iloc[0].timestamp()
    switch = start + 600.0
    hysteresis = _hysteresis_table()
    definition, _sha = _definition()
    resolver = FakeResolver(
        segments=lambda at: [
            IdentitySegment(None, None, LATER if at is not None and at >= switch else IDENTITY)
        ],
        table_for=lambda identity: hysteresis if identity == LATER else _table(definition),
    )
    segments: dict[int, list[dict[str, Any]]] = {}
    twin, live = _twin(resolver=resolver, by_segment=segments)
    _drive(twin, _records(frame))
    block = twin.snapshot()
    (change,) = [i for i in block["invalidations"] if i["kind"] == "calibration_changed"]
    asked = [a for a in resolver.asked if a is not None]
    switched_at = next(a for a in asked if a >= switch)
    assert switch <= switched_at < switch + 60.0 + 2.0  # within one recheck (+ a tick)
    assert len(live) == 174  # every sample decided exactly once, across the switch
    assert all(
        r["provenance"]["revision"] == hysteresis.revision
        for r in segments[1]
        if r.get("provenance")
    )
    assert change["flushed_pending"] >= 0


def test_a_program_the_table_loader_refuses_is_withheld_by_name() -> None:
    """The table loader is the only refusal on both paths: an image of
    another program refuses ``wrong_program`` for its segment."""
    session = _crossing_session()
    change = CHANGE.timestamp()
    other = TwinIdentity(REVISION, "f" * 64, "registry", False, "other_program", None)
    definition, _sha = _definition()

    def table_for(identity: TwinIdentity) -> ResolvedTable:
        if identity.program_status != "known":
            raise TwinRefusalError("wrong_program", "another program")
        return _table(definition)

    resolver = FakeResolver(
        segments=lambda _at: [
            IdentitySegment(None, change, IDENTITY),
            IdentitySegment(change, None, other),
        ],
        table_for=table_for,
    )
    segments: dict[int, list[dict[str, Any]]] = {}
    twin, _live = _twin(resolver=resolver, by_segment=segments)
    _drive(twin, _records(session))
    assert segments[0] == _evaluate(session, window=(None, change))["records"]
    assert 1 not in segments
    block = twin.snapshot()
    assert block["segments"][1]["slots"]["1"]["refusal"]["reason"] == "wrong_program"


def test_a_replay_is_its_own_session_and_writes_nothing(tmp_path: Path) -> None:
    """The twin under a replay is scoped to ``_replay-<id>``, says so, and
    starts a fresh stream when the replay ends; it never writes a file."""
    replay = SimpleNamespace(running=True, replay_id="r1", source_session_id=None)
    components = SimpleNamespace(
        replay_controller=SimpleNamespace(status=lambda: replay),
        storage_writer=None,
        config=SimpleNamespace(collection=SimpleNamespace(storage_path=tmp_path)),
    )
    twin, live = _twin(session="live")
    for record in _records(_session()):
        twin.intake.observe(record)
    twin.tick_live(components, "replay")  # type: ignore[arg-type]
    block = twin.snapshot()
    assert block["session"] == "_replay-r1" and block["replay"] is True
    assert block["writes"].startswith("nothing")
    assert [i["kind"] for i in block["invalidations"]] == ["session_changed"]
    replay.running = False
    twin.tick_live(components, "engine_off")  # type: ignore[arg-type]
    assert twin.snapshot()["session"] == "live"
    assert twin.snapshot()["result"] == "inputs_not_observed"
    assert twin.snapshot()["segments"] == []
    assert list(tmp_path.iterdir()) == []


# -- E04.1: the runtime contract ---------------------------------------------------------


class _StalledExecutor(Executor):
    """A worker that never finishes: every submitted future stays pending."""

    def __init__(self) -> None:
        self.submitted = 0

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
        self.submitted += 1
        return Future()


def test_a_stalled_worker_costs_counted_reads_never_a_backlog() -> None:
    stalled = _StalledExecutor()
    recorder = InferenceTimingRecorder()
    twin, _live = _twin(executor=_InlineExecutor(), recorder=recorder, capacity=200)
    twin._executor = stalled  # the worker stalls after the preload
    records = list(_records(fixture_frame()))[:2000]
    started = time.perf_counter()
    for i, record in enumerate(records):
        twin.intake.observe(record)
        if i % 100 == 0:
            twin.tick(session="s", vehicle_state="engine_running")
    elapsed = time.perf_counter() - started
    block = twin.snapshot()
    intake = block["intake"]
    assert stalled.submitted == 1  # one hand-off in flight; ticks never queue work
    assert intake["busy_ticks"] == 19
    assert intake["waiting"] == 200 == intake["capacity"]
    assert intake["dropped"] > 0
    assert elapsed < 2.0


def test_the_tick_never_waits_for_the_worker() -> None:
    """A resolver whose preload blocks until released: the ticks that hand it
    over return at once and publish ``starting``, then
    ``inputs_not_observed`` until the first read."""
    from concurrent.futures import ThreadPoolExecutor

    release = threading.Event()

    def slow() -> FakeResolver:
        release.wait(5.0)
        return FakeResolver()

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        twin = LiveTwin(resolver=slow, recorder=None, did_map=CATALOG, executor=executor)
        started = time.perf_counter()
        twin.tick(session="s", vehicle_state=None)
        twin.tick(session="s", vehicle_state=None)
        assert time.perf_counter() - started < 0.2
        assert twin.snapshot()["result"] == "starting"
        assert twin.snapshot()["intake"]["busy_ticks"] == 1
        release.set()
        deadline = time.monotonic() + 5.0
        while twin.snapshot()["result"] == "starting" and time.monotonic() < deadline:
            time.sleep(0.01)
            twin.tick(session="s", vehicle_state=None)
        assert twin.snapshot()["result"] == "inputs_not_observed"
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_status_carries_the_block_and_both_stage_timings(frame: pd.DataFrame) -> None:
    recorder = InferenceTimingRecorder()
    twin, _live = _twin(recorder=recorder)
    _drive(twin, list(_records(frame))[:1500], flush=False)
    timing = recorder.snapshot().to_dict()
    block = timing["twin"]
    assert block["role"] == "ecu_command_prediction" and block["alerts"] is False
    assert {STAGE_TICK, STAGE_EVAL, STAGE_IDENTITY} <= set(timing["stages"])
    assert block["identity"]["calibration"] == REVISION and block["identity"]["slot"] == 1
    assert block["stream"]["decision_horizon_s"] == pytest.approx(10.25)
    assert block["vehicle_state"] == "engine_running"
    assert len(block["recent"]) <= 120


def test_a_load_byte_stored_under_another_decode_withholds_the_stream() -> None:
    session = _session()
    load = session["channel"] == "mem:ECM_FUEL_TARGET_MAP_X_TRACE"
    session.loc[load, "value_number"] = session.loc[load, "value_number"] + 0.1
    twin, live = _twin()
    _drive(twin, _records(session))
    block = twin.snapshot()
    assert (block["result"], block["reason"]) == ("withheld", "wrong_units")
    assert live == []


def test_it_is_off_by_default_and_says_why(tmp_path: Path) -> None:
    recorder = InferenceTimingRecorder()
    components = SimpleNamespace(config=isolated_config(tmp_path))
    source = SimpleNamespace(did_map=None, record_observers=[])
    assert build_live_twin(components, source, recorder) is None  # type: ignore[arg-type]
    assert source.record_observers == []
    block = recorder.snapshot().to_dict()["twin"]
    assert (block["enabled"], block["result"]) == (False, "disabled")
    assert "E04.3" in block["reason"]


def _replayed(session: Path, *, speed: float = 20.0, seed: int = 7) -> list[TelemetryRecord]:
    """``session`` as ``collection.replay`` emits it: its own row iterator and
    record builder, emit times paced at ``speed`` with millisecond jitter, so
    every row's ``acquired_at`` is rebased by its own (jittered) offset."""
    from src.collection.replay import _iter_rows, replay_record

    rng = random.Random(seed)
    base = time.time()
    first: float | None = None
    out = []
    for orig, fields in _iter_rows(session, skip_sources=frozenset()):
        first = orig if first is None else first
        emitted = base + (orig - first) / speed + rng.uniform(0.0, 0.005)
        out.append(replay_record(fields, orig, emitted))
    return out


def test_a_replay_is_resolved_paired_and_decided_as_the_stored_brain_job(
    tmp_path: Path, frame: pd.DataFrame, stored: dict[str, Any]
) -> None:
    """The pre-flash October 4 capture, replayed through ``collection.replay``'s
    rebasing into a twin built on the shared resolver: the identity is the one
    the session's own first sample falls in (ZS44A-20260920, before the 23:02
    flash -- not the calibration in force at emit time), every read is paired
    on its recorded instant, and the records and counts are the stored brain
    job's."""
    import asyncio

    from src.control.brain_jobs import BrainJobStore, canonical_params
    from src.control.twin_identity import TwinResolver
    from tests.test_brain_jobs import SESSION, scheduler, scoped_config

    config = scoped_config(tmp_path)
    session = config.collection.storage_path / SESSION
    session.mkdir(parents=True)
    frame.to_parquet(session / "telemetry-00000.parquet")
    (session / "digest_skipped.json").write_text('{"reason": "test"}')
    params = canonical_params(config, "twin_session_evaluation", {"session": SESSION})
    BrainJobStore.for_config(config).submit("twin_session_evaluation", params)
    job = asyncio.run(scheduler(config).step())
    assert job is not None and job.state == "completed" and job.result is not None
    result = BrainJobStore.for_config(config).read_result(job.job_id)
    assert result is not None
    (evaluation,) = [e for e in result["evaluations"] if e["slot"] == 1]

    out: list[dict[str, Any]] = []
    recorder = InferenceTimingRecorder()
    twin = LiveTwin(
        resolver=lambda: TwinResolver(config),
        recorder=recorder,
        did_map=CATALOG,
        catalog=CATALOG,
        session="live",
        executor=_InlineExecutor(),
        on_records=lambda _seg, slot, records: out.extend(records) if slot == 1 else None,
    )
    twin.tick(session="_replay-r1", vehicle_state="replay", session_dir=session)
    replayed = _replayed(session)
    # The rebase really moved the clock: the emit-time acquired_at is "now".
    assert abs(float(replayed[0].metadata["acquired_at"]) - time.time()) < 3600
    next_tick: datetime | None = None
    for record in replayed:
        if next_tick is None:
            next_tick = record.timestamp + timedelta(seconds=1)
        while record.timestamp >= next_tick:
            twin.tick(session="_replay-r1", vehicle_state="replay", session_dir=session)
            next_tick += timedelta(seconds=1)
        twin.intake.observe(record)
    twin.flush(session="_replay-r1", session_dir=session)
    block = twin.snapshot()
    assert block["replay"] is True
    assert block["identity"]["calibration"] == result["calibration"] == "ZS44A-20260920"
    assert block["identity"]["source"] == result["calibration_source"]
    assert block["identity"]["program_id"] == result["program_id"]
    assert [s["calibration"] for s in block["segments"]] == [
        s["calibration"] for s in result["segments"]
    ]
    assert out == stored["records"]
    assert block["accounting"] == {
        k: v for k, v in evaluation["accounting"].items() if k != "input_pairing"
    }
    assert block["applicability"]["qualification_blockers"] == evaluation["qualification_blockers"]
    refused = {str(r["slot"]): r["reason"] for r in result["refusals"]}
    for slot, reason in refused.items():
        assert block["segments"][0]["slots"][slot]["refusal"]["reason"] == reason
    assert sum(block["stream"]["late_reads"].values()) == 0
    # Nothing written by the twin: the job wrote its own state, the twin none.
    assert block["writes"].startswith("nothing")


def test_build_live_twin_uses_the_shared_resolver(tmp_path: Path, frame: pd.DataFrame) -> None:
    """Built as the daemon builds it: off the loop, on the real profile."""
    base = isolated_config(tmp_path, keep=("afr_calibration",), twin_live={"enabled": True})
    config = scope_config(base, VehicleProfile(slug=SLUG, vin="", name="Xterra"))
    recorder = InferenceTimingRecorder()
    source = LiveFeatureSource(config=isolated_config(tmp_path, profiles={"enabled": False}))
    twin = build_live_twin(SimpleNamespace(config=config), source, recorder)  # type: ignore[arg-type]
    assert twin is not None
    try:
        twin.tick(session="s", vehicle_state=None)
        deadline = time.monotonic() + 30.0
        while twin.snapshot()["result"] == "starting" and time.monotonic() < deadline:
            time.sleep(0.02)
            twin.tick(session="s", vehicle_state=None)
        for record in list(_records(frame))[:400]:
            source.observe(record)
        twin.flush(session="s")
        block = twin.snapshot()
        assert block["identity"]["calibration"] == "ZS44A-20260920"
        assert block["identity"]["program_status"] == "known"
    finally:
        twin.close()
    assert not (config.daemon.memory_ledger.parent / "brain").exists()


def test_the_twin_is_wired_into_the_inference_pipeline() -> None:
    import inspect

    from src.daemon import inference

    source = inspect.getsource(inference.LiveInferencePipeline)
    assert "build_live_twin(components, source, recorder)" in source
    assert "self._twin.tick_live(" in source
    assert "self._twin.close()" in source


def test_identity_is_resolved_at_the_datas_own_time() -> None:
    """Identity is asked at the data's acquisition time (a replayed drive's
    own time), never the wall clock: first at the first read, then once per
    recheck interval of data time."""
    resolver = FakeResolver()
    twin, _live = _twin(resolver=resolver, recheck_s=10.0)
    _drive(twin, _records(_session()), flush=False)
    start = datetime(2026, 10, 4, 21, 0, tzinfo=UTC).timestamp()
    assert resolver.asked[0] is not None and start <= resolver.asked[0] < start + 1.0
    assert 5 <= len(resolver.asked) <= 7
    assert all(start <= a <= start + 61 for a in resolver.asked if a is not None)


def test_the_brain_status_route_and_mcp_serve_the_published_live_block(
    tmp_path: Path, frame: pd.DataFrame
) -> None:
    """``/control/brain/status`` carries the block the tick published to
    ``/status`` (one block, two places), and MCP passes the route through."""
    import asyncio

    from fastapi.testclient import TestClient

    from src.dashboard.api import create_app
    from src.mcp_server.tools import brain as brain_tool
    from tests.test_brain_routes import AUTH, TOKEN, _mcp_client

    config = scope_config(
        isolated_config(tmp_path, keep=("afr_calibration",)),
        VehicleProfile(slug=SLUG, vin="", name="Xterra"),
    )
    recorder = InferenceTimingRecorder()
    twin, _live = _twin(recorder=recorder)
    _drive(twin, list(_records(frame))[:1500], flush=False)
    app = create_app(
        config, anomalies_dir=tmp_path / "an", control_token=TOKEN, inference_timing=recorder
    )
    response = TestClient(app).get("/control/brain/status", headers=AUTH)
    assert response.status_code == 200, response.text
    live = response.json()["live_twin"]
    assert live == recorder.snapshot().to_dict()["twin"]
    assert live["latest_prediction"] is None or "predicted_phi" in live["latest_prediction"]
    assert live["recent"] and live["accounting"]["candidates"] > 0
    via_mcp = asyncio.run(brain_tool.get_brain_status(_mcp_client(app)))
    assert via_mcp["live_twin"] == live


@pytest.mark.parametrize("tick_s", [0.25, 1.0, 5.0])
def test_a_coolant_source_that_first_appears_mid_session_is_causal_on_both_paths(
    tick_s: float,
) -> None:
    """The DID coolant (the declared first choice) starts at 25 s, reading
    60 degC; Mode 01 reads 90 degC throughout. The sample at 10 s is judged on
    Mode 01 and the one at 40 s on the DID (cold), on the stored path and on
    the live path at every tick rate: the source is chosen per sample from
    reads at or before it (or in its gate bracket), never from the future."""
    from tests.test_target_phi import _row

    early = _session(t=10.0, end=60.0)
    late = _session(t=40.0, end=60.0)
    tuple_rows = late[
        late["channel"].str.startswith("mem:")
        & ~late["channel"].isin(
            ["mem:ECM_UPREV_MAGIC", "mem:ECM_ACTIVE_MAP", "mem:ECM_UPREV_MAP_SELECTOR"]
        )
    ]
    did = pd.DataFrame([_row(25.0 + k, "DID_1101", None, text="6E") for k in range(36)])
    session = (
        pd.concat([early, tuple_rows, did])
        .sort_values("timestamp", kind="stable")
        .reset_index(drop=True)
    )
    stored = _evaluate(session)
    sources = [r["gates"]["coolant"]["spec"] for r in stored["records"]]
    assert sources == ["obd:COOLANT_TEMP", "did:ECM_COOLANT_TEMP"]
    assert [r["reason"] for r in stored["records"]][1] == "cold"
    assert stored["coolant_sources"] == ["did:ECM_COOLANT_TEMP", "obd:COOLANT_TEMP"]
    twin, live = _twin()
    _drive(twin, _records(session), tick_s=tick_s)
    _same_as_stored(live, stored, twin)
    assert twin.snapshot()["applicability"]["coolant_sources"] == stored["coolant_sources"]


def test_an_input_first_read_mid_session_is_not_captured_before_it_on_both_paths() -> None:
    """``input_not_captured`` is asked per sample (a read at or before
    ``t + horizon``): the pedal flag first read at 45 s is not captured for
    the 10 s sample, and merely stale (not paired) for a later one."""
    early = _session(t=10.0, end=60.0, flag_at=40.0)
    stored = _evaluate(early)
    (record,) = stored["records"]
    assert "input_not_captured:flag" in record["reasons"]
    twin, live = _twin()
    _drive(twin, _records(early), tick_s=0.5)
    _same_as_stored(live, stored, twin)


def test_the_shared_resolver_splits_at_a_flash_inside_the_span_only(tmp_path: Path) -> None:
    """The real flash history: the 2026-10-04 23:02 flash is a segment bound
    for data spanning it, and none for data that ended before it (the stored
    path bounds a finished session by its last sample; a live stream does
    not, and crosses the bound when its data does). The ROM is hashed at
    most once per image."""
    from src.control.twin_identity import TwinResolver
    from tests.test_brain_jobs import scoped_config

    resolver = TwinResolver(scoped_config(tmp_path))
    before = datetime(2026, 10, 4, 20, 45, tzinfo=UTC).timestamp()
    after = datetime(2026, 10, 5, 1, 0, tzinfo=UTC).timestamp()
    ended = resolver.segments(None, first_read=before, until=before + 1800)
    assert [s.identity.calibration for s in ended] == ["ZS44A-20260920"]
    spanning = resolver.segments(None, first_read=before, until=after)
    assert [s.identity.calibration for s in spanning] == ["ZS44A-20260920", "ZS44A-20261004"]
    assert spanning[0].end == spanning[1].start
    assert all(s.identity.program_status == "known" for s in spanning)
    open_ended = resolver.segments(None, first_read=before)
    assert [s.identity.calibration for s in open_ended] == [
        s.identity.calibration for s in spanning
    ]
    assert len(resolver._programs) == 2  # one program lookup per image
