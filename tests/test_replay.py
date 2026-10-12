"""Tests for the session-replay simulation producer."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.collection.replay import (
    ReplayEmissionStats,
    make_replay_collector,
    probe_rederivability,
    replay_session,
)
from src.config import CanaryConfig
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import RE_DERIVED_SOURCES, REPLAY_SOURCE_AT_KEY, TelemetryRecord


def _session(tmp_path: Path) -> Path:
    sdir = tmp_path / "2026-05-11T222447"
    sdir.mkdir(parents=True)
    base = datetime(2026, 5, 11, 22, 24, 47, tzinfo=UTC)
    recs = [
        TelemetryRecord(timestamp=base, source="can", channel="0x1F9", payload=b"\x00\x01\x02\x03"),
        TelemetryRecord(
            timestamp=base + timedelta(seconds=0.01),
            source="obd",
            channel="RPM",
            value=812.0,
            unit="rpm",
        ),
        TelemetryRecord(
            timestamp=base + timedelta(seconds=0.02),
            source="can",
            channel="0x551",
            payload=b"\x5a",
        ),
    ]
    write_records_to_parquet(recs, sdir)
    return sdir


async def _drain(
    sdir: Path,
    *,
    loop: bool = False,
    expect: int = 3,
    stats: ReplayEmissionStats | None = None,
) -> list[TelemetryRecord]:
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    # speed huge ⇒ no real sleeping; collect then stop.
    task = asyncio.create_task(
        replay_session(
            queue,
            sdir,
            stop,
            speed=10_000.0,
            loop=loop,
            stats=stats if stats is not None else ReplayEmissionStats(),
            broadcast_stale_timeout_seconds=10.0,
            on_finish="idle",
        )
    )
    got: list[TelemetryRecord] = []
    try:
        for _ in range(expect):
            got.append(await asyncio.wait_for(queue.get(), timeout=2.0))
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
    return got


def test_replay_emits_records_in_order_with_payload_and_value(tmp_path: Path) -> None:
    got = asyncio.run(_drain(_session(tmp_path)))
    assert [(r.source, r.channel) for r in got] == [
        ("can", "0x1F9"),
        ("obd", "RPM"),
        ("can", "0x551"),
    ]
    assert got[0].payload == b"\x00\x01\x02\x03"
    assert got[1].value == 812.0 and got[1].unit == "rpm"
    assert got[2].payload == b"\x5a"
    # Timestamps are rebased to ~now, not the 2026-05-11 recording time.
    assert (datetime.now(UTC) - got[0].timestamp).total_seconds() < 30


def test_replay_missing_session_idles_then_stops(tmp_path: Path) -> None:
    async def run() -> None:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        task = asyncio.create_task(
            replay_session(
                queue,
                tmp_path / "nope",
                stop,
                loop=False,
                stats=ReplayEmissionStats(),
                broadcast_stale_timeout_seconds=10.0,
                on_finish="idle",
            )
        )
        await asyncio.sleep(0.05)
        assert queue.empty()
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(run())


def test_on_finish_return_ends_a_non_looping_replay_without_a_stop(tmp_path: Path) -> None:
    """2026-10-05: a non-looping control-plane replay never ended, because the
    pass idled on ``stop_event`` like a collector. ``on_finish="return"``
    returns once the pass is done, with no stop ever set."""
    sdir = _session(tmp_path)
    stats = ReplayEmissionStats()

    async def run() -> asyncio.Event:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        await asyncio.wait_for(
            replay_session(
                queue,
                sdir,
                stop,
                speed=10_000.0,
                loop=False,
                stats=stats,
                broadcast_stale_timeout_seconds=10.0,
                on_finish="return",
            ),
            timeout=2.0,
        )
        assert queue.qsize() == 3
        return stop

    stop = asyncio.run(run())
    assert not stop.is_set()
    assert stats.passes == 1 and stats.rows_emitted == 3
    assert stats.pass_reconciled() is True


def test_on_finish_idle_waits_for_stop_after_a_non_looping_pass(tmp_path: Path) -> None:
    """The collector path keeps idling: a returning collector looks dead."""
    sdir = _session(tmp_path)

    async def run() -> None:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        stats = ReplayEmissionStats()
        task = asyncio.create_task(
            replay_session(
                queue,
                sdir,
                stop,
                speed=10_000.0,
                loop=False,
                stats=stats,
                broadcast_stale_timeout_seconds=10.0,
                on_finish="idle",
            )
        )
        for _ in range(200):
            if stats.passes == 1:
                break
            await asyncio.sleep(0.01)
        assert stats.passes == 1
        await asyncio.sleep(0.05)
        assert not task.done()
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(run())


def test_on_finish_return_on_a_missing_session_returns_at_once(tmp_path: Path) -> None:
    async def run() -> None:
        await asyncio.wait_for(
            replay_session(
                asyncio.Queue(),
                tmp_path / "nope",
                asyncio.Event(),
                loop=False,
                stats=ReplayEmissionStats(),
                broadcast_stale_timeout_seconds=10.0,
                on_finish="return",
            ),
            timeout=2.0,
        )

    asyncio.run(run())


def _large_session(tmp_path: Path, rows: int = 20_000) -> Path:
    base = datetime(2026, 5, 18, 12, tzinfo=UTC)
    sdir = tmp_path / "2026-05-18T120000"
    write_records_to_parquet(
        [
            TelemetryRecord(
                timestamp=base + timedelta(milliseconds=i),
                source="obd",
                channel="RPM",
                value=float(i),
                unit="rpm",
            )
            for i in range(rows)
        ],
        sdir,
    )
    return sdir


@pytest.mark.parametrize("slow_read_s", [0.0, 0.3])
def test_a_fast_pass_never_holds_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slow_read_s: float
) -> None:
    """At 10,000x every row is past due, so none sleeps; without an explicit
    yield the pass held the event loop for its whole length and a looping
    replay starved every other coroutine. The source probes and each chunk's
    read/sort/convert also ran on the loop (~0.25 s and ~0.2 s per 10k-row
    chunk); they now run in a thread, so even a 0.3 s chunk read leaves no
    gap of that size. A heartbeat must keep beating, and the rows must come
    out exactly as the synchronous reader yields them."""
    import time as _time

    from src.collection import replay as replay_mod
    from src.collection.replay import _iter_rows

    sdir = _large_session(tmp_path)
    chunks = len(list(sdir.glob("*.parquet")))
    assert chunks >= 2  # the prefetch path is exercised
    expected = [(epoch, f["value"]) for epoch, f in _iter_rows(sdir, skip_sources=frozenset())]
    real_read = replay_mod._read_chunk
    import threading

    loop_thread = threading.get_ident()
    read_threads: list[int] = []

    def slow_read(chunk: Path, skip: frozenset[str]) -> object:
        read_threads.append(threading.get_ident())
        _time.sleep(slow_read_s)
        return real_read(chunk, skip)  # type: ignore[arg-type]

    monkeypatch.setattr(replay_mod, "_read_chunk", slow_read)
    # The yield comes after a *time* budget, so rows per turn scale with CPU
    # speed: at the production 5 ms a fast laptop emits ~2,400 rows a turn and
    # fails the 2,000-row bound with the yield working (T01.24). A 1 ms budget
    # keeps the bound 4x clear of that; the 5 ms value itself is judged by
    # test_rows_already_due_yield_the_loop_every_few_milliseconds.
    monkeypatch.setattr(replay_mod, "_MAX_HOLD_SECONDS", 0.001)
    stats = ReplayEmissionStats()

    async def run() -> tuple[float, list[int], list[TelemetryRecord]]:
        done = asyncio.Event()
        per_turn: list[int] = []

        async def heartbeat() -> None:
            last = stats.rows_emitted
            while not done.is_set():
                await asyncio.sleep(0)
                now = stats.rows_emitted
                per_turn.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)  # the heartbeat is running before the replay
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        started = _time.monotonic()
        await replay_session(
            queue,
            sdir,
            asyncio.Event(),
            speed=10_000.0,
            loop=False,
            stats=stats,
            broadcast_stale_timeout_seconds=10.0,
            on_finish="return",
        )
        wall = _time.monotonic() - started
        done.set()
        await beat
        return wall, per_turn, [queue.get_nowait() for _ in range(queue.qsize())]

    _wall, per_turn, got = asyncio.run(run())
    assert stats.rows_emitted == 20_000 and stats.passes == 1
    assert stats.pass_reconciled() is True
    assert [r.value for r in got] == [value for _, value in expected]
    # No wall-clock premise check: a held pass would put all 20,000 rows in one
    # turn, ten times the bound below, on any machine. (`wall > 0.5` measured
    # 0.41 s on a fast laptop with nothing wrong: T01.24.)
    # Judged by rows per heartbeat turn, not wall time, so machine load cannot
    # decide it (T01.22: a 0.23 s preemption gap failed a 0.15 s bound at load
    # average 13). Before the fix the whole pass was one turn.
    assert len(per_turn) > 50
    assert max(per_turn) < 20_000 // 10, max(per_turn)
    # The chunk reads run off the event loop's thread, so even a slow read
    # cannot stall it.
    assert read_threads and loop_thread not in read_threads


def test_rows_already_due_yield_the_loop_every_few_milliseconds(tmp_path: Path) -> None:
    """The per-row yield on its own terms: one 60,000-row chunk, so the read
    and conversion are a single threaded call and almost all event-loop time
    is the emit phase. At 10,000x every row is past due and none sleeps;
    without ``_MAX_HOLD_SECONDS`` the emit phase is one unbroken stretch
    (hundreds of ms), with it the heartbeat never waits much past 5 ms.

    Judged in rows, not wall time, so machine load cannot decide it: the
    rows emitted between two heartbeat turns. Without the yield the whole
    60,000-row chunk goes out in one turn; with it each turn sees a few ms of
    emission. (A wall-clock bound of 40 ms failed at load average 30 on
    2026-10-05 while the yield was working: T01.21.)"""

    base = datetime(2026, 5, 18, 12, tzinfo=UTC)
    sdir = tmp_path / "2026-05-18T120000"
    write_records_to_parquet(
        [
            TelemetryRecord(
                timestamp=base + timedelta(milliseconds=i),
                source="obd",
                channel="RPM",
                value=float(i),
                unit="rpm",
            )
            for i in range(60_000)
        ],
        sdir,
        max_rows=60_000,
    )
    assert len(list(sdir.glob("*.parquet"))) == 1
    stats = ReplayEmissionStats()

    async def run() -> list[int]:
        done = asyncio.Event()
        bursts: list[int] = []

        async def heartbeat() -> None:
            rows_then = stats.rows_emitted
            while not done.is_set():
                await asyncio.sleep(0)
                rows_now = stats.rows_emitted
                if rows_now > rows_then:
                    bursts.append(rows_now - rows_then)
                rows_then = rows_now

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        await replay_session(
            asyncio.Queue(),
            sdir,
            asyncio.Event(),
            speed=10_000.0,
            loop=False,
            stats=stats,
            broadcast_stale_timeout_seconds=10.0,
            on_finish="return",
        )
        done.set()
        await beat
        return bursts

    bursts = asyncio.run(run())
    assert stats.rows_emitted == 60_000
    # Emission is broken into many turns, none carrying a large share of the
    # chunk; unyielded, one turn carries all 60,000 rows.
    assert len(bursts) > 10, bursts
    assert max(bursts) < 20_000, max(bursts)


def test_on_finish_has_no_default() -> None:
    """Required, so no caller can inherit the wrong end-of-pass behaviour."""
    import inspect

    param = inspect.signature(replay_session).parameters["on_finish"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_make_replay_collector_matches_starter_signature(tmp_path: Path) -> None:
    sdir = _session(tmp_path)
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(
        f"""
vehicle: {{make: Nissan, model: Xterra, year: 2008}}
hardware:
  elm327_port: /dev/null
  can: {{interface: virtual, channel: sim, bitrate: 500000}}
collection:
  obd_source: disabled
  obd_poll_rate_hz: 1
  storage_path: {tmp_path / "sim"}
  session_retention_days: 7
  storage_warning_mb: 500
  replay_session: {sdir}
  replay_speed: 9000
  replay_loop: false
preprocessing: {{resample_hz: 1, rolling_window_seconds: 5, operating_modes: {{}}}}
models:
  isolation_forest: {{enabled: false, n_estimators: 100, contamination: 0.01}}
  lstm_autoencoder: {{enabled: false, sequence_length: 30, latent_dim: 8, threshold_k: 3.0}}
dashboard: {{host: 127.0.0.1, port: 8000}}
"""
    )
    config = CanaryConfig.from_yaml(cfg_path)
    starter = make_replay_collector(config)

    async def run() -> None:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        task = asyncio.create_task(starter(queue, stop))
        first = await asyncio.wait_for(queue.get(), timeout=2.0)
        assert first.channel == "0x1F9"
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(run())


def test_replay_carries_acquisition_metadata_rebased_with_the_row(tmp_path: Path) -> None:
    """C02.9: ``metadata_json`` survives replay. ``acquired_at`` shifts by the
    same offset as the row's rebased timestamp so their separation is kept;
    round identity is carried as recorded (it names the *source* rounds)."""
    sdir = tmp_path / "2026-05-11T222447"
    base = datetime(2026, 5, 11, 22, 24, 47, tzinfo=UTC)
    write_records_to_parquet(
        [
            TelemetryRecord(
                timestamp=base,
                source="obd",
                channel="RPM",
                value=812.0,
                metadata={
                    "ts_schema": 2,
                    "acquired_at": base.timestamp() + 0.031,
                    "acquired_clock": "kernel",
                    "generation": 3,
                    "round_id": 17,
                },
            ),
            TelemetryRecord(timestamp=base + timedelta(seconds=0.01), source="obd", channel="MAF"),
            TelemetryRecord(
                timestamp=base + timedelta(seconds=0.02), source="can", channel="0x1F9"
            ),
        ],
        sdir,
    )
    got = asyncio.run(_drain(sdir))
    rpm = got[0]
    assert rpm.metadata["generation"] == 3 and rpm.metadata["round_id"] == 17
    assert rpm.metadata["ts_schema"] == 2 and rpm.metadata["acquired_clock"] == "kernel"
    acquired = float(rpm.metadata["acquired_at"])  # type: ignore[arg-type]
    assert acquired - rpm.timestamp.timestamp() == pytest.approx(0.031, abs=1e-3)
    # P02.16: every replayed record also carries the instant it was recorded,
    # and nothing else is added.
    recorded = [base, base + timedelta(seconds=0.01), base + timedelta(seconds=0.02)]
    for record, at in zip(got, recorded, strict=True):
        assert record.metadata[REPLAY_SOURCE_AT_KEY] == pytest.approx(at.timestamp())
    assert got[1].metadata == {REPLAY_SOURCE_AT_KEY: pytest.approx(recorded[1].timestamp())}
    assert set(got[2].metadata) == {REPLAY_SOURCE_AT_KEY}


def test_iter_rows_handles_nullable_bool_columns_in_the_ground_truth_slice() -> None:
    """Nullable ``value_bool`` reads back as ``pd.NA``; ``bool(NA)`` raises.
    The committed drive slice is exactly such a file."""
    from src.collection.replay import _iter_rows

    # skip nothing: this test is about value decoding, not the emit filter.
    rows = list(
        _iter_rows(Path("tests/fixtures/ground_truth/drive-slice"), skip_sources=frozenset())
    )
    assert len(rows) > 100
    assert all(isinstance(fields["metadata"], dict) for _epoch, fields in rows)
    assert any(fields["value"] is not None for _epoch, fields in rows)


# --- D02.9: re-derived sources must not be re-emitted -----------------------


def _mixed_session(tmp_path: Path, *, can_payload: bytes | None) -> Path:
    """A session with both captured and re-derived rows.

    ``can_payload=None`` reproduces a pre-2026-07-29 recording: the raw frame
    bytes were never stored, so nothing downstream can re-derive anything.
    """
    sdir = tmp_path / "2026-05-11T222447"
    base = datetime(2026, 5, 11, 22, 24, 47, tzinfo=UTC)
    recs = [
        TelemetryRecord(timestamp=base, source="can", channel="0x1F9", payload=can_payload),
        TelemetryRecord(
            timestamp=base + timedelta(seconds=0.01),
            source="obd",
            channel="RPM",
            value=812.0,
            unit="rpm",
        ),
        TelemetryRecord.decoded(base + timedelta(seconds=0.02), "ECM_RPM", 812.0, "rpm"),
        TelemetryRecord.derived(base + timedelta(seconds=0.03), "FUEL_RATE_LPH", 1.25, "L/h"),
        TelemetryRecord(
            timestamp=base + timedelta(seconds=0.04),
            source="can",
            channel="0x551",
            payload=can_payload,
        ),
    ]
    write_records_to_parquet(recs, sdir)
    return sdir


def test_probe_reports_payload_bearing_session_as_rederivable(tmp_path: Path) -> None:
    probe = probe_rederivability(_mixed_session(tmp_path, can_payload=b"\x00\x01\x02\x03"))
    assert probe.rederivable is True
    assert probe.skip_sources == RE_DERIVED_SOURCES
    assert "payload" in probe.reason


def test_probe_reports_payloadless_session_as_not_rederivable(tmp_path: Path) -> None:
    probe = probe_rederivability(_mixed_session(tmp_path, can_payload=None))
    assert probe.rederivable is False
    assert probe.skip_sources == frozenset()
    assert probe.can_rows_sampled == 2
    assert "2026-07-29" in probe.reason


def test_replay_skips_rederived_rows_and_counts_them(tmp_path: Path) -> None:
    """A payload-bearing session: only the captured rows go on the queue, the
    emitted CAN rows keep their payloads (so storage/live can re-derive), and
    the skipped rows are reported rather than vanishing."""
    stats = ReplayEmissionStats()
    sdir = _mixed_session(tmp_path, can_payload=b"\x00\x01\x02\x03")
    got = asyncio.run(_drain(sdir, expect=3, stats=stats))
    assert [(r.source, r.channel) for r in got] == [
        ("can", "0x1F9"),
        ("obd", "RPM"),
        ("can", "0x551"),
    ]
    assert got[0].payload == b"\x00\x01\x02\x03"
    assert got[2].payload == b"\x00\x01\x02\x03"
    assert stats.source_rows_total == 5
    assert stats.rows_emitted == 3
    assert stats.rows_skipped == 2
    assert stats.rows_dropped == 0
    assert stats.rederivable is True
    assert stats.skipped_sources == ("decoded", "derived")


def _gapped_session(tmp_path: Path, gap_seconds: float) -> Path:
    """Three rows with one long recorded pause in the middle."""
    sdir = tmp_path / "2026-05-11T222447"
    sdir.mkdir(parents=True)
    base = datetime(2026, 5, 11, 22, 24, 47, tzinfo=UTC)
    recs = [
        TelemetryRecord(timestamp=base, source="obd", channel="RPM", value=800.0, unit="rpm"),
        TelemetryRecord(
            timestamp=base + timedelta(seconds=gap_seconds),
            source="obd",
            channel="RPM",
            value=810.0,
            unit="rpm",
        ),
        TelemetryRecord(
            timestamp=base + timedelta(seconds=gap_seconds + 0.1),
            source="obd",
            channel="RPM",
            value=820.0,
            unit="rpm",
        ),
    ]
    write_records_to_parquet(recs, sdir)
    return sdir


def test_a_clamped_gap_is_reported_as_effective_speed_not_hidden_behind_speed(
    tmp_path: Path,
) -> None:
    """D02.9 — ``speed`` is the requested multiplier; the recording outruns it.

    ``_MAX_GAP_SECONDS`` clamps every sleep, so a 600 s recorded pause costs
    at most 0.5 s of wall time at ``speed=1.0``. The stats now carry the
    source time actually covered, the wall time spent, the ratio, and the
    fraction of the source span, instead of leaving a poller to divide rows
    by elapsed clock and land on a wrong premise.
    """
    stats = ReplayEmissionStats()
    sdir = _gapped_session(tmp_path, gap_seconds=600.0)

    async def run() -> list[TelemetryRecord]:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        task = asyncio.create_task(
            replay_session(
                queue,
                sdir,
                stop,
                speed=1.0,
                loop=False,
                stats=stats,
                broadcast_stale_timeout_seconds=10.0,
                on_finish="idle",
            )
        )
        got: list[TelemetryRecord] = []
        try:
            for _ in range(3):
                got.append(await asyncio.wait_for(queue.get(), timeout=5.0))
            for _ in range(200):
                if stats.passes == 1:
                    break
                await asyncio.sleep(0.01)
            # Long enough for effective_speed to be a number, not noise.
            await asyncio.sleep(1.05)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)
        return got

    got = asyncio.run(run())
    assert [r.value for r in got] == [800.0, 810.0, 820.0]
    assert stats.source_span_s == pytest.approx(600.1, abs=0.01)
    assert stats.source_time_emitted_s == pytest.approx(600.1, abs=0.01)
    assert stats.source_time_progress() == pytest.approx(1.0)
    wall = stats.wall_elapsed_s()
    assert wall is not None and wall < 3.0, "the 600 s pause was clamped, not slept"
    effective = stats.effective_speed()
    assert effective is not None and effective > 100.0, (
        f"effective speed {effective} should dwarf the requested 1.0"
    )
    assert stats.passes == 1
    assert stats.pass_reconciled() is True


def test_pass_reconciliation_counts_rows_not_wall_time(tmp_path: Path) -> None:
    """A completed pass must account for every source row exactly once per
    pass — emitted, skipped or dropped — with no elapsed-time inference."""
    stats = ReplayEmissionStats()
    sdir = _mixed_session(tmp_path, can_payload=b"\x00\x01\x02\x03")
    assert stats.pass_reconciled() is None, "nothing to reconcile before a pass"
    asyncio.run(_drain(sdir, expect=3, stats=stats))
    assert stats.passes == 1
    assert stats.rows_emitted + stats.rows_skipped + stats.rows_dropped == stats.source_rows_total
    assert stats.pass_reconciled() is True
    # A row that went missing would be a real gap, and would say so.
    stats.rows_emitted -= 1
    assert stats.pass_reconciled() is False


def test_replay_keeps_rederived_rows_when_the_source_has_no_payloads(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression guard for the one case where skipping loses data.

    A pre-payload-persistence recording has no raw frames to re-derive from,
    so its stored ``decoded``/``derived`` rows are the only copy and must
    still be emitted — and the status must say why. If someone later
    "simplifies" the skip to unconditional, this test fails.
    """
    stats = ReplayEmissionStats()
    sdir = _mixed_session(tmp_path, can_payload=None)
    with caplog.at_level(logging.INFO, logger="src.collection.replay"):
        got = asyncio.run(_drain(sdir, expect=5, stats=stats))
    probed = [r for r in caplog.records if r.getMessage() == "replay source probed"]
    assert len(probed) == 1, "the behaviour switch is announced exactly once per replay"
    assert probed[0].rederivable is False  # type: ignore[attr-defined]
    assert [(r.source, r.channel) for r in got] == [
        ("can", "0x1F9"),
        ("obd", "RPM"),
        ("decoded", "ECM_RPM"),
        ("derived", "FUEL_RATE_LPH"),
        ("can", "0x551"),
    ]
    assert stats.source_rows_total == 5
    assert stats.rows_emitted == 5
    assert stats.rows_skipped == 0
    assert stats.rederivable is False
    assert stats.skipped_sources == ()
    assert stats.rederivable_reason is not None
    assert "2026-07-29" in stats.rederivable_reason
