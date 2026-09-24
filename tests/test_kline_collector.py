"""K-line collector: idle until a capture profile asks; owns the ECM session
exactly while it reads; ends cleanly; refuses a plan it cannot read."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from src.collection import tx_gate
from src.collection.kline import protocol as kp
from src.collection.kline.plan import resolve_plan
from src.collection.kline.profile import KlineProfile
from src.collection.kline.transport import KlineTimeoutError, RxChunk
from src.collection.kline_collector import LOCAL_ID, KlineCollector, plan_records
from src.collection.reconnect import ReconnectPolicy
from src.config import KlineConfig
from src.decode.dids import load_did_map
from src.telemetry import TelemetryRecord

DIDS = Path("data/dids.yaml")
PROFILE = Path("data/nissan-xterra-2008/kline.yaml")
ENTRIES = ("0x1201", "ECM_TARGET_EQUIV_RATIO")
FAST = ReconnectPolicy(initial_delay_seconds=0.01, max_delay_seconds=0.01)


@pytest.fixture(autouse=True)
def _clean_gate() -> Iterator[None]:
    tx_gate.reset_ecm_session_claims()
    yield
    tx_gate.reset_ecm_session_claims()


class FakeTransport:
    """Answers like the Xterra: definitions acknowledged, polls answered with
    RPM 0x02C8 (712 rpm raw) and target equivalence ratio 0x4000."""

    instances: list[FakeTransport] = []

    def __init__(self, profile: KlineProfile, *, fail_start: int = 0) -> None:
        self.profile = profile
        self.calls: list[str] = []
        # Only the first transport built in a test fails, however many follow.
        self.fail_start = fail_start if not FakeTransport.instances else 0
        FakeTransport.instances.append(self)

    def open(self) -> None:
        self.calls.append("open")

    def close(self) -> None:
        self.calls.append("close")

    def start_communication(self) -> kp.StartCommunicationReply:
        self.calls.append("start")
        if self.fail_start:
            self.fail_start -= 1
            raise KlineTimeoutError("no StartCommunication reply")
        return kp.StartCommunicationReply(0xFC, 0x10, b"\x5d\x8f")

    def request(self, payload: bytes) -> bytes:
        kp.check_request(payload)
        self.calls.append(f"request {payload[:2].hex()}")
        if payload[0] == kp.SID_DEFINE_LOCAL_ID:
            return bytes([payload[1]])
        if payload[0] == kp.SID_READ_DATA_BY_LOCAL_ID:
            return bytes([payload[1]]) + b"\x02\xc8" + b"\x40\x00"
        raise AssertionError(payload.hex())

    def stop_communication(self) -> None:
        self.calls.append("stop")


def _collector(entries: list[tuple[str, ...]], **kwargs: object) -> KlineCollector:
    FakeTransport.instances.clear()
    return KlineCollector(
        KlineConfig(enabled=True, poll_interval_seconds=0.01, sniff=False),
        PROFILE,
        DIDS,
        entries=lambda: entries[0],
        transport_factory=lambda profile, mode: FakeTransport(profile, **kwargs),  # type: ignore[arg-type]
        reconnect_policy=FAST,
    )


def test_plan_records_split_one_reply_into_named_channels() -> None:
    plan = resolve_plan(ENTRIES, load_did_map(DIDS))
    records = plan_records(
        plan, bytes([LOCAL_ID]) + b"\x02\xc8\x40\x00", started=10.0, finished=10.02
    )
    assert [(r.source, r.channel, r.value, r.payload) for r in records] == [
        ("kline", "DID_1201", "02c8", b"\x02\xc8"),
        ("kline", "MEM_ECM_TARGET_EQUIV_RATIO", "4000", b"\x40\x00"),
    ]
    assert records[0].timestamp.timestamp() == pytest.approx(10.01)
    assert records[0].metadata["acquired_clock"] == "userspace"
    assert records[0].metadata["response_interval_s"] == pytest.approx(0.02)


async def test_idle_until_a_profile_asks_then_owns_the_session_while_reading() -> None:
    entries: list[tuple[str, ...]] = [()]
    collector = _collector(entries)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(queue, stop))
    await asyncio.sleep(0.05)
    assert FakeTransport.instances == []  # nothing asked: nothing opened
    assert tx_gate.ecm_session_owner() == "can_diagnostics"

    entries[0] = ENTRIES
    first = await asyncio.wait_for(queue.get(), timeout=5)
    assert first.channel == "DID_1201"
    assert tx_gate.ecm_session_owner() == "kline"
    assert tx_gate.is_passive()  # CAN diagnostics pause while K-line reads

    entries[0] = ()  # the capture ended
    for _ in range(200):
        if tx_gate.ecm_session_owner() == "can_diagnostics":
            break
        await asyncio.sleep(0.01)
    stop.set()
    await task
    (transport,) = FakeTransport.instances
    assert transport.calls[:4] == ["open", "start", "request ac81", "request 2181"]
    assert transport.calls[-2:] == ["stop", "close"]
    assert tx_gate.ecm_session_owner() == "can_diagnostics"


async def test_a_failed_start_is_retried_and_ownership_released_between() -> None:
    entries = [ENTRIES]
    collector = _collector(entries, fail_start=1)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(queue, stop))
    await asyncio.wait_for(queue.get(), timeout=5)
    stop.set()
    await task
    assert len(FakeTransport.instances) == 2
    failed = FakeTransport.instances[0]
    assert failed.calls == ["open", "start", "close"]
    assert collector.sessions == 1
    assert tx_gate.ecm_session_owner() == "can_diagnostics"


async def test_an_unreadable_plan_is_refused_once_and_never_transmits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entries = [("0x1117",)]  # no catalog width
    collector = _collector(entries)
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(asyncio.Queue(), stop))
    await asyncio.sleep(0.1)
    stop.set()
    await task
    assert FakeTransport.instances == []
    assert tx_gate.ecm_session_owner() == "can_diagnostics"
    refusals = [r for r in caplog.records if "cannot run" in r.getMessage()]
    assert len(refusals) == 1 and "0x1117" in refusals[0].getMessage()


async def test_a_vehicle_without_kline_facts_never_transmits(tmp_path: Path) -> None:
    FakeTransport.instances.clear()
    collector = KlineCollector(
        KlineConfig(enabled=True, sniff=False),
        tmp_path / "kline.yaml",
        DIDS,
        entries=lambda: ENTRIES,
        transport_factory=lambda profile, mode: FakeTransport(profile),  # type: ignore[arg-type,return-value]
        reconnect_policy=FAST,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(asyncio.Queue(), stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert FakeTransport.instances == []


async def test_losing_the_session_mid_poll_starts_a_new_one() -> None:
    entries = [ENTRIES]
    collector = _collector(entries)
    polls = {"n": 0}
    original = FakeTransport.request

    def flaky(self: FakeTransport, payload: bytes) -> bytes:
        if payload[0] == kp.SID_READ_DATA_BY_LOCAL_ID and len(FakeTransport.instances) == 1:
            polls["n"] += 1
            if polls["n"] == 3:
                raise KlineTimeoutError("ECM stopped answering")
        return original(self, payload)

    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(FakeTransport, "request", flaky)
        task = asyncio.create_task(collector.run(queue, stop))
        for _ in range(200):
            if collector.sessions >= 2:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await task
    assert collector.sessions >= 2
    assert FakeTransport.instances[0].calls[-1] == "close"
    assert tx_gate.ecm_session_owner() == "can_diagnostics"


async def test_cancelling_the_daemon_task_still_closes_and_releases() -> None:
    entries = [ENTRIES]
    collector = _collector(entries)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    task = asyncio.create_task(collector.run(queue, asyncio.Event()))
    await asyncio.wait_for(queue.get(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (transport,) = FakeTransport.instances
    assert transport.calls[-1] == "close"
    assert tx_gate.ecm_session_owner() == "can_diagnostics"


# --------------------------------------------------------------------------
# H06.3 / H06.4 — sniffing another tester
# --------------------------------------------------------------------------


def _uprev_stream() -> list[tuple[float, int]]:
    import gzip
    import json

    fixture = json.loads(
        gzip.open(Path("tests/fixtures/kline/uprev_session_bytes.json.gz"), "rt").read()
    )
    t = 1000.0
    out = []
    for byte, dt in zip(bytes.fromhex(fixture["bytes"]), fixture["dt_us"], strict=True):
        t += dt / 1e6
        out.append((t, byte))
    return out


class FakeSniffTransport:
    """Replays UpRev's real session through ``listen``, then goes quiet."""

    instances: list[tuple[FakeSniffTransport, object]] = []

    def __init__(self, stream: list[tuple[float, int]], mode: object) -> None:
        self.stream = deque(stream)
        self.mode = mode
        self.calls: list[str] = []
        FakeSniffTransport.instances.append((self, mode))

    def open(self) -> None:
        self.calls.append("open")

    def close(self) -> None:
        self.calls.append("close")

    def listen(self, until: Callable[[], bool], sink: Callable[[RxChunk], None]) -> None:
        """Byte by byte, each gap as the kernel timestamped it: exact timing."""
        last = self.stream[0][0] if self.stream else 0.0
        while self.stream and not until():
            at, byte = self.stream.popleft()
            sink(RxChunk(at=at, data=bytes([byte]), quiet_s=at - last, after=last, char_s=0.0))
            last = at
        while not until():
            time.sleep(0.001)

    def write(self, *_: object) -> None:  # pragma: no cover - must never be called
        raise AssertionError("sniff mode transmitted")


async def _sniff_until_drained(
    collector: KlineCollector, queue: asyncio.Queue[TelemetryRecord]
) -> list[TelemetryRecord]:
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(queue, stop))
    for _ in range(500):
        await asyncio.sleep(0.01)
        if FakeSniffTransport.instances and not FakeSniffTransport.instances[-1][0].stream:
            break
    await asyncio.sleep(0.05)
    stop.set()
    await task
    records = []
    while not queue.empty():
        records.append(queue.get_nowait())
    return records


def _sniffer(entries: list[tuple[str, ...]], hold: float = 10.0) -> KlineCollector:
    FakeSniffTransport.instances.clear()
    stream = _uprev_stream()
    return KlineCollector(
        KlineConfig(enabled=True, foreign_hold_seconds=hold),
        PROFILE,
        DIDS,
        entries=lambda: entries[0],
        transport_factory=lambda profile, mode: FakeSniffTransport(stream, mode),  # type: ignore[arg-type,return-value]
        reconnect_policy=FAST,
    )


async def test_idle_collector_sniffs_uprevs_session_frame_for_frame() -> None:
    from src.collection.kline.transport import Mode

    collector = _sniffer([()])
    records = await _sniff_until_drained(collector, asyncio.Queue())
    frames = [r for r in records if r.channel == "FRAME"]
    assert len(frames) == 1462
    assert {r.metadata["instrument"] for r in records} == {"sniff"}
    assert FakeSniffTransport.instances[0][1] is Mode.SNIFF
    # UpRev's ECU-check reads (22 11 03) decode; its logging set needs widths
    # the catalog lacks, so those stay raw frames.
    did_1103 = [r for r in records if r.channel == "DID_1103"]
    # A live value: it flickered 0x99/0x9A within UpRev's own session.
    assert did_1103 and {r.value for r in did_1103} == {"99", "9a"}
    assert collector.foreign_frames == 12 + 723


async def test_another_tester_owns_the_session_while_it_talks() -> None:
    collector = _sniffer([()])
    await _sniff_until_drained(collector, asyncio.Queue())
    assert tx_gate.ecm_session_owner() == "foreign"
    assert tx_gate.is_passive()  # CAN diagnostics stepped aside
    tx_gate.check_expiry(now=float("inf"))  # the daemon polls this; time passes
    assert tx_gate.ecm_session_owner() == "can_diagnostics"


async def test_a_capture_request_waits_for_the_foreign_tester() -> None:
    from src.collection.kline.transport import Mode

    entries: list[tuple[str, ...]] = [ENTRIES]
    tx_gate.claim_ecm_session("foreign", ttl_seconds=60.0)
    collector = _sniffer(entries)
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(asyncio.Queue(), stop))
    await asyncio.sleep(0.2)
    stop.set()
    await task
    modes = {mode for _, mode in FakeSniffTransport.instances}
    assert modes == {Mode.SNIFF}  # never switched to command while foreign


class BrokenReader(FakeSniffTransport):
    def listen(self, until: Callable[[], bool], sink: Callable[[RxChunk], None]) -> None:
        raise OSError("tty went away")


async def test_a_reader_thread_failure_reaches_the_retry_loop() -> None:
    """The reader runs on its own thread; its failure must not die there."""
    FakeSniffTransport.instances.clear()
    collector = KlineCollector(
        KlineConfig(enabled=True),
        PROFILE,
        DIDS,
        entries=lambda: (),
        transport_factory=lambda profile, mode: BrokenReader([], mode),  # type: ignore[arg-type,return-value]
        reconnect_policy=FAST,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(asyncio.Queue(), stop))
    for _ in range(500):  # a loaded host (the parallel gate) needs more than 0.3 s
        await asyncio.sleep(0.01)
        if len(FakeSniffTransport.instances) >= 2:
            break
    stop.set()
    await task
    assert len(FakeSniffTransport.instances) >= 2  # reopened after the failure
    assert all(t.calls[-1] == "close" for t, _ in FakeSniffTransport.instances)
