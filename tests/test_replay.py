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
from src.telemetry import RE_DERIVED_SOURCES, TelemetryRecord


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
            replay_session(queue, tmp_path / "nope", stop, loop=False, stats=ReplayEmissionStats())
        )
        await asyncio.sleep(0.05)
        assert queue.empty()
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(run())


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
    assert got[1].metadata == {} and got[2].metadata == {}


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
            replay_session(queue, sdir, stop, speed=1.0, loop=False, stats=stats)
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
