"""D03.64: scoring a capture reads the rows it scores, and a child that cannot fit says so.

On 2026-11-10 the daemon's boot-time finalize of a 108-minute ``tuning``
capture (80 % raw CAN frames) read every row back with the CAN decoded on
read: the spawn child reached 6.19 GB, the memory cgroup's OOM killer took it,
and ``OOMPolicy=stop`` failed the whole unit. These tests pin the three halves
of the fix:

* the score reads only ``obd`` rows and only the columns it uses -- and the two
  figures that are whole-session counts (``rows_on_disk``, the ``ended_at``
  fallback) are still whole-session counts;
* the child caps its own address space, and running out of it is a named
  :class:`CaptureQualityUnavailableError`, never a dead service;
* the boot path survives that refusal.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import multiprocessing
import resource
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from src.analysis import capture_coverage
from src.analysis.capture_coverage import (
    _acquisition_seconds,
    _requests_for,
    channel_lifetimes_from_rows,
    count_persisted_tuples,
    member_channels,
    persisted_quality_inputs,
    recompute_capture_quality,
)
from src.analysis.channels import read_acquisition_frame
from src.analysis.digest import derived_address_space_bytes
from src.collection.capture_quality import effective_max_tuple_skew_s
from src.config import CanaryConfig
from src.control import capture_finalize, child_pool, trip_end
from src.control.capture_finalize import (
    CaptureQualityUnavailableError,
    finalize_interrupted_capture,
    recompute_quality_off_loop,
)
from src.preprocessing import session_reader
from src.storage.capture_manifest import read_capture_manifest
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord
from tests.builders import CAPTURE_T0, capture_obd_record, capture_rounds, write_capture_session
from tests.waits import settle, wait_until

pytestmark = pytest.mark.usefixtures("no_trip_end_digest_child")

_MIB = 1024 * 1024
_STARTED = CAPTURE_T0


def _quality_dict(session: Path, **kwargs: Any) -> dict[str, Any]:
    return recompute_capture_quality(session, **kwargs).to_dict()


# ---------------------------------------------------------------------------
# a session that is mostly not what the score reads
# ---------------------------------------------------------------------------


def _mixed_records(*, tail: str) -> list[TelemetryRecord]:
    """Forty ``A``/``B`` rounds, with every other kind of row beside them.

    ``tail`` is what the session's *last* row is: ``"obd"`` (the final round),
    ``"can"`` (a broadcast frame after it), or ``"kline"`` (an exchange stored
    after it whose ``acquired_at`` is earlier than the stored stamp, so the
    last row on the acquisition clock is not the last on the stored one).
    """
    records = list(capture_rounds(40))  # obd rows, T0 .. T0 + 9.753
    records += [
        TelemetryRecord.can(CAPTURE_T0 + k * 0.025, 0x100 + k % 7, bytes([k % 251]) * 8)
        for k in range(400)  # raw frames the score never looks at
    ]
    records += [
        TelemetryRecord.gps(CAPTURE_T0 + k * 0.5, "speed", float(k), "km/h") for k in range(20)
    ]
    records += [
        TelemetryRecord.from_epoch(
            CAPTURE_T0 + k * 0.002,
            source="sensor",
            channel="FUEL_PRESSURE",
            value=350.0,
            unit="kPa",
            payload=b"CH0:2126\t2785\n" * 5,
            metadata={"adc_counts": 2129.0, "adc_input": 0},
        )
        for k in range(300)
    ]
    # Rows recorded before 2026-09-26 stored decoded values beside the frames.
    records += [
        TelemetryRecord.decoded(datetime.fromtimestamp(CAPTURE_T0 + k, tz=UTC), "RPM_B", 800.0)
        for k in range(10)
    ]
    if tail == "can":
        records.append(TelemetryRecord.can(CAPTURE_T0 + 11.2, 0x101, b"\x01" * 8))
    elif tail == "kline":
        records.append(TelemetryRecord.can(CAPTURE_T0 + 11.2, 0x101, b"\x01" * 8))
        records.append(
            TelemetryRecord.from_epoch(
                CAPTURE_T0 + 12.0,
                source="kline",
                channel="ABS:1202",
                value=1.0,
                metadata={"acquired_at": CAPTURE_T0 + 11.5},
            )
        )
    return records


def _profile() -> dict[str, Any]:
    return {
        "mode01": {"A": 1.0, "B": 1.0},
        "mode22": [],
        "target_rate_hz": 4.0,
        "required_tuple": ["A", "B"],
        "min_achieved_rate_hz": 3.0,
        "round_timestamps": True,
        "max_tuple_skew_s": 0.125,
        "name": "fuel-map",
    }


def _whole_session_inputs(
    session: Path, *, ended_at: float | None, generation_boundary: int | None = None
) -> dict[str, Any]:
    """``persisted_quality_inputs`` as it was before D03.64: every row of the
    session read, with the CAN decoded on read, and the score taken from that
    frame. The reference the restricted read must agree with."""
    profile = _profile()
    frame = read_acquisition_frame(session)
    ended_source = "given"
    if ended_at is None:
        ended_source = "last_row"
        ended_at = float(_acquisition_seconds(frame).max())
    requests = _requests_for(profile, frame)
    return {
        "requests": requests,
        "lifetime_channels": channel_lifetimes_from_rows(
            frame,
            [str(row["channel"]) for row in requests],
            started_at=_STARTED,
            ended_at=ended_at,
        ),
        "tuples": count_persisted_tuples(
            frame,
            member_channels(profile),
            max_skew_s=effective_max_tuple_skew_s(profile),
            started_at=_STARTED,
            ended_at=ended_at,
            generation_boundary=generation_boundary,
        ),
        "rows_on_disk": int(len(frame)),
        "ended_at": ended_at,
        "ended_at_source": ended_source,
    }


@pytest.mark.parametrize("tail", ["obd", "can", "kline"])
@pytest.mark.parametrize("given_end", [True, False])
def test_the_scored_read_gives_the_whole_reads_inputs(
    tmp_path: Path, tail: str, given_end: bool
) -> None:
    """Identity: restricting the read changes nothing the score is made of.

    Everything but ``rows_on_disk`` (which was a count of a frame carrying
    rows decoded on read, see below) equals what the whole-session read gives,
    including the interval's end when none was recorded -- the last row of
    *any* source, on the acquisition clock.
    """
    session = write_capture_session(tmp_path, _mixed_records(tail=tail))
    ended_at = CAPTURE_T0 + 60 if given_end else None
    expected = _whole_session_inputs(session, ended_at=ended_at, generation_boundary=3)
    actual = persisted_quality_inputs(
        session,
        profile=_profile(),
        started_at=_STARTED,
        ended_at=ended_at,
        generation_boundary=3,
    )
    expected.pop("rows_on_disk")
    actual.pop("rows_on_disk")
    assert actual == expected
    if not given_end:
        last = {"obd": CAPTURE_T0 + 39 * 0.25 + 0.003, "can": CAPTURE_T0 + 11.2}
        assert actual["ended_at"] == pytest.approx(last.get(tail, CAPTURE_T0 + 11.5))
        assert actual["ended_at_source"] == "last_row"
    assert actual["tuples"]["complete"] == 40


def test_the_last_row_is_read_on_the_acquisition_clock_not_the_stored_one(
    tmp_path: Path,
) -> None:
    """A K-line exchange stored at T0+12 acquired at T0+11.5 ends the capture
    at 11.5: the unread rows' end is asked of the same clock as the read ones."""
    session = write_capture_session(tmp_path, _mixed_records(tail="kline"))
    assert capture_coverage.last_row_seconds_outside_scored(session) == pytest.approx(
        CAPTURE_T0 + 11.5
    )
    assert _quality_dict(session)["interval"]["elapsed_s"] == pytest.approx(11.5)


def test_rows_on_disk_counts_the_stored_rows_not_the_rows_decoded_on_read(
    tmp_path: Path,
) -> None:
    """``rows_on_disk`` is compared with the writer's ``rows_written``: it is
    every persisted row, from the chunks' footers -- including a stored
    ``decoded`` row, which a read drops and re-derives."""
    records = _mixed_records(tail="can")
    session = write_capture_session(tmp_path, records)
    inputs = persisted_quality_inputs(
        session, profile=_profile(), started_at=_STARTED, ended_at=CAPTURE_T0 + 60
    )
    assert inputs["rows_on_disk"] == len(records)
    quality = _quality_dict(session, ended_at=CAPTURE_T0 + 60)
    assert quality["persistence"]["rows_on_disk"] == len(records)


def test_a_session_with_no_scored_rows_still_ends_where_its_last_row_does(
    tmp_path: Path,
) -> None:
    """All-CAN: no ``obd`` row to score, an empty frame, and the interval's end
    still comes from the rows that were not read."""
    records = [TelemetryRecord.can(CAPTURE_T0 + k * 0.1, 0x100, b"\x00" * 8) for k in range(50)]
    session = write_capture_session(tmp_path, records)
    inputs = persisted_quality_inputs(
        session, profile=_profile(), started_at=_STARTED, ended_at=None
    )
    assert inputs["ended_at"] == pytest.approx(CAPTURE_T0 + 4.9)
    assert inputs["rows_on_disk"] == 50
    assert inputs["tuples"]["source"] == "no_round_evidence"


def test_a_session_that_is_not_readable_says_no_rows_not_a_failure(tmp_path: Path) -> None:
    session = write_capture_session(tmp_path, [capture_obd_record(CAPTURE_T0, "A", round_id=1)])
    for chunk in session.glob("telemetry-*.parquet"):
        chunk.write_bytes(b"")
    inputs = persisted_quality_inputs(
        session, profile=_profile(), started_at=_STARTED, ended_at=None
    )
    assert inputs["tuples"] == {"source": "unavailable", "reason": "no telemetry rows persisted"}
    assert inputs["rows_on_disk"] == 0
    assert inputs["ended_at_source"] == "no_rows"


# ---------------------------------------------------------------------------
# the regression: CAN rows are never loaded
# ---------------------------------------------------------------------------


class _ReadSpy:
    """What every ``pd.read_parquet`` of a session returned: its sources and
    columns."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reads: list[tuple[set[str], set[str]]] = []
        real = pd.read_parquet

        def spy(path: Any, *args: Any, **kwargs: Any) -> pd.DataFrame:
            frame = real(path, *args, **kwargs)
            sources = {str(s) for s in frame["source"].unique()} if "source" in frame else set()
            self.reads.append((sources, set(frame.columns)))
            return frame

        monkeypatch.setattr(pd, "read_parquet", spy)
        self.decoded = 0
        real_decode = session_reader.decode_on_read  # type: ignore[attr-defined]

        def counting_decode(*args: Any, **kwargs: Any) -> Any:
            self.decoded += 1
            return real_decode(*args, **kwargs)

        monkeypatch.setattr(session_reader, "decode_on_read", counting_decode)

    @property
    def sources(self) -> set[str]:
        return set().union(*(sources for sources, _ in self.reads)) if self.reads else set()

    @property
    def columns(self) -> set[str]:
        return set().union(*(columns for _, columns in self.reads)) if self.reads else set()


@pytest.mark.parametrize("given_end", [True, False])
def test_scoring_a_capture_never_loads_its_can_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given_end: bool
) -> None:
    """The 2026-11-10 failure: 80 % of the rows were raw CAN frames, all read
    and decoded to score 5 % of them. No parquet read the score makes may
    return a row of any other source, a ``payload``, or decode a frame."""
    session = write_capture_session(tmp_path, _mixed_records(tail="kline"))
    spy = _ReadSpy(monkeypatch)

    # Teeth: the spy does see the CAN rows when the whole session is read.
    read_acquisition_frame(session)
    assert {"can", "sensor", "gps"} <= spy.sources
    assert "payload" in spy.columns
    assert spy.decoded > 0
    spy.reads.clear()
    spy.decoded = 0

    quality = recompute_capture_quality(session, ended_at=CAPTURE_T0 + 60 if given_end else None)

    assert quality.tuples["complete"] == 40
    assert spy.reads, "the score read no rows at all"
    assert spy.sources == {"obd"}
    assert "payload" not in spy.columns and "value_number" not in spy.columns
    assert spy.decoded == 0, "the score decoded frames on read"


def test_the_score_reads_through_the_one_restricted_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every caller of the score (stop, live, boot, the route, the validation
    evidence) reaches ``persisted_quality_inputs``; its only read of the
    session's rows is :func:`read_scored_rows`, so none can forget the filter."""
    session = write_capture_session(tmp_path, _mixed_records(tail="obd"))
    asked: list[dict[str, Any]] = []
    real = capture_coverage.read_acquisition_frame  # type: ignore[attr-defined]

    def record(path: Any, **kwargs: Any) -> pd.DataFrame:
        asked.append(kwargs)
        return real(path, **kwargs)

    monkeypatch.setattr(capture_coverage, "read_acquisition_frame", record)
    persisted_quality_inputs(session, profile=_profile(), started_at=_STARTED, ended_at=None)
    # The unscored rows' last instant is asked of a frame of its own, never of
    # the session: the one read of the session is the restricted one.
    reads_of_the_session = [k for k in asked if k.get("filters") is not None]
    assert reads_of_the_session == [
        {
            "decoded": "as_recorded",
            "filters": [("source", "==", "obd")],
            "columns": capture_coverage.SCORED_COLUMNS,
        }
    ]


def test_an_acquisition_frame_refuses_filters_it_cannot_apply(tmp_path: Path) -> None:
    """``filters``/``columns`` narrow a session directory's read; handed with a
    frame that is already read they would be silently ignored."""
    frame = pd.DataFrame({"timestamp": [], "source": [], "metadata_json": []})
    with pytest.raises(ValueError, match="already read"):
        read_acquisition_frame(frame, filters=[("source", "==", "obd")])
    with pytest.raises(ValueError, match="already read"):
        read_acquisition_frame(frame, columns=["timestamp"])


# ---------------------------------------------------------------------------
# the child caps its own address space
# ---------------------------------------------------------------------------


def _spawn_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))


def _score_and_report_limit(
    session_dir: Path, kwargs: dict[str, Any], *, address_space_bytes: int | None
) -> tuple[dict[str, Any], int]:
    """Run in a real child: the real entry point, then what the OS says."""
    quality = capture_finalize.recompute_under_limit(
        session_dir, kwargs, address_space_bytes=address_space_bytes
    )
    return quality.to_dict(), resource.getrlimit(resource.RLIMIT_AS)[0]


async def test_the_real_child_scores_under_its_own_cap_and_the_parent_is_untouched(
    tmp_path: Path,
) -> None:
    session = write_capture_session(tmp_path, capture_rounds(240))
    before = resource.getrlimit(resource.RLIMIT_AS)
    cap = derived_address_space_bytes()
    expected = await asyncio.to_thread(_quality_dict, session, ended_at=CAPTURE_T0 + 60)
    call = functools.partial(
        _score_and_report_limit,
        session,
        _recompute_kwargs(ended_at=CAPTURE_T0 + 60),
        address_space_bytes=cap,
    )
    with _spawn_pool() as pool:
        scored, soft = await asyncio.get_running_loop().run_in_executor(pool, call)
    assert soft <= cap and soft != resource.RLIM_INFINITY, "the child's cap is not in force"
    assert scored == expected
    assert resource.getrlimit(resource.RLIMIT_AS) == before, "the parent's own limit moved"


def _recompute_kwargs(**given: Any) -> dict[str, Any]:
    """The keyword arguments ``recompute_quality_off_loop`` hands its child."""
    return {
        "lifetime": None,
        "losses": None,
        "persistence": None,
        "scheduler": None,
        "scope": "recompute",
        "ended_at": None,
        "batching_start": None,
        **given,
    }


def _vm_size_bytes() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmSize:"):
            return int(line.split()[1]) * 1024
    raise AssertionError("no VmSize in /proc/self/status")


def _outgrow_the_cap(
    session_dir: Path, kwargs: dict[str, Any], *, address_space_bytes: int | None
) -> Any:
    """Run in a real child: take the cap the parent chose, tighten it to a
    little above what this child already maps, and allocate past it -- a real
    ``MemoryError`` from a real ``RLIMIT_AS``, with no scored session needed."""
    assert address_space_bytes == derived_address_space_bytes(), address_space_bytes
    assert address_space_bytes is not None
    child_pool.limit_address_space(min(address_space_bytes, _vm_size_bytes() + 256 * _MIB))
    held = bytearray(1024 * _MIB)
    raise AssertionError(f"the cap let {len(held)} bytes through")


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs /proc")
async def test_a_child_that_runs_out_of_its_cap_is_a_named_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel must never be the one that ends it. The ``MemoryError`` the
    cap raises in the child crosses the process boundary and comes back as
    :class:`CaptureQualityUnavailableError`, naming the cap."""
    monkeypatch.setattr(capture_finalize, "recompute_under_limit", _outgrow_the_cap)
    session = write_capture_session(tmp_path, capture_rounds(8))
    with pytest.raises(CaptureQualityUnavailableError) as raised:
        await recompute_quality_off_loop(session)
    message = str(raised.value)
    assert f"{derived_address_space_bytes() // _MIB} MiB address-space cap" in message
    assert session.name in message and "ran out of memory" in message
    # The caller is intact: the next recompute answers.
    monkeypatch.undo()
    again = await recompute_quality_off_loop(session, ended_at=CAPTURE_T0 + 2)
    assert again.scope == "recompute"


def _report_the_cap(
    session_dir: Path, kwargs: dict[str, Any], *, address_space_bytes: int | None
) -> Any:
    """Run in a real child: say what cap the parent handed it."""
    return address_space_bytes


async def test_only_a_child_process_is_ever_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``RLIMIT_AS`` is per process: a thread pool standing in for the child
    *is* the test worker (or the daemon), and a cap put on it never comes off
    -- it took ``torch`` out of the xdist worker that ran the first draft of
    this change. So the cap is handed only to a child process: the single-use
    one this call spawns (the derived default, or what was asked for), an
    injected process pool only when asked, and a thread pool never."""
    given: list[int | None] = []
    before = resource.getrlimit(resource.RLIMIT_AS)

    def record(
        session_dir: Path, kwargs: dict[str, Any], *, address_space_bytes: int | None = None
    ) -> Any:
        given.append(address_space_bytes)
        return SimpleNamespace(to_dict=lambda: {}, passed=True, verdict="pass", failures=[])

    monkeypatch.setattr(capture_finalize, "recompute_under_limit", record)
    session = write_capture_session(tmp_path, capture_rounds(8))
    with ThreadPoolExecutor(max_workers=1) as pool:
        await recompute_quality_off_loop(session, executor=pool)
        await recompute_quality_off_loop(session, executor=pool, address_space_bytes=1234 * _MIB)
    monkeypatch.setattr(child_pool, "spawn_pool", lambda: ThreadPoolExecutor(max_workers=1))
    await recompute_quality_off_loop(session)
    await recompute_quality_off_loop(session, address_space_bytes=1234 * _MIB)
    assert given == [None, None, None, None], "a thread pool was handed a cap"
    assert resource.getrlimit(resource.RLIMIT_AS) == before, "this process was capped"

    monkeypatch.setattr(capture_finalize, "recompute_under_limit", _report_the_cap)
    monkeypatch.setattr(child_pool, "spawn_pool", _spawn_pool)
    # ``_report_the_cap`` stands in for the score and returns the cap it was handed.
    default_cap: Any = await recompute_quality_off_loop(session)
    asked_cap: Any = await recompute_quality_off_loop(session, address_space_bytes=1234 * _MIB)
    assert default_cap == derived_address_space_bytes()
    assert asked_cap == 1234 * _MIB
    with _spawn_pool() as injected:
        no_ask: Any = await recompute_quality_off_loop(session, executor=injected)
        asked: Any = await recompute_quality_off_loop(
            session, executor=injected, address_space_bytes=999 * _MIB
        )
    assert no_ask is None
    assert asked == 999 * _MIB


def test_the_digest_and_the_quality_child_share_one_limiter() -> None:
    """``trip_end.limit_address_space`` (patched by the digest's tests) is the
    ``child_pool`` function the quality child calls, not a second copy."""
    assert trip_end.limit_address_space is child_pool.limit_address_space


def test_the_child_entry_caps_before_it_reads_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    monkeypatch.setattr(child_pool, "limit_address_space", lambda n: order.append(f"limit:{n}"))
    real = capture_coverage.recompute_capture_quality

    def spy(session_dir: Path, **kwargs: Any) -> Any:
        order.append("score")
        return real(session_dir, **kwargs)

    monkeypatch.setattr(capture_coverage, "recompute_capture_quality", spy)
    session = write_capture_session(tmp_path, capture_rounds(8))
    capture_finalize.recompute_under_limit(
        session, _recompute_kwargs(ended_at=CAPTURE_T0 + 2), address_space_bytes=777 * _MIB
    )
    assert order == [f"limit:{777 * _MIB}", "score"]


# ---------------------------------------------------------------------------
# the boot path survives a refusal
# ---------------------------------------------------------------------------


class _RefusingExecutor(Executor):
    """A pool whose child ran out of memory: the future fails with the
    ``MemoryError`` the cap raised."""

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        future.set_exception(MemoryError("Unable to allocate 21.9 MiB"))
        return future


async def test_finalizing_an_interrupted_capture_survives_a_refusal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session = write_capture_session(tmp_path, capture_rounds(8))
    with caplog.at_level(logging.ERROR, logger="src.control.capture_finalize"):
        payload = await finalize_interrupted_capture(session, executor=_RefusingExecutor())
    assert payload is None
    assert not (session / "capture_quality.json").exists()
    logged = "\n".join(record.getMessage() + (record.exc_text or "") for record in caplog.records)
    assert "failed to finalize interrupted capture" in logged
    assert "ran out of memory" in caplog.text


async def test_finalizing_an_interrupted_capture_writes_the_sidecar(tmp_path: Path) -> None:
    session = write_capture_session(tmp_path, capture_rounds(240))
    with ThreadPoolExecutor(max_workers=1) as pool:
        payload = await finalize_interrupted_capture(session, executor=pool)
    assert payload is not None and payload["tuples"]["complete"] == 240
    assert json.loads((session / "capture_quality.json").read_text()) == json.loads(
        json.dumps(payload)
    )


async def test_the_daemon_boots_past_an_interrupted_capture_it_could_not_score(
    daemon_config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D03.64 on the boot path itself: ``run_daemon`` finalizes the capture the
    last process left armed before it starts any collector. A child refused by
    its own cap must leave the daemon starting -- 2026-11-10 it left it dead."""
    from src.daemon import build_components, runtime

    async def refused(session_dir: Path, **_: Any) -> Any:
        raise CaptureQualityUnavailableError(
            f"capture quality recompute for {session_dir.name} ran out of memory under "
            "its 4096 MiB address-space cap"
        )

    monkeypatch.setattr(capture_finalize, "recompute_quality_off_loop", refused)
    storage = daemon_config.collection.storage_path
    interrupted = write_capture_session(storage, capture_rounds(8))
    components = build_components(daemon_config)
    stop = asyncio.Event()
    writer = ParquetTelemetryWriter(storage)

    async def run() -> None:
        await runtime.run_daemon(
            daemon_config,
            stop,
            collectors=[],
            writer=writer,
            components=components,
            enable_live_inference=False,
        )

    task = asyncio.create_task(run())
    try:
        await wait_until(
            lambda: (
                components.health_monitor.startup.snapshot()["stages"]["collector_tasks_created"]
                is not None
            ),
            what="the daemon starting its collectors after the refused boot-time score",
        )
    finally:
        stop.set()
        await settle(task)
    manifest = read_capture_manifest(interrupted)
    assert manifest is not None and manifest.end_reason == "interrupted"
    assert not (interrupted / "capture_quality.json").exists()
