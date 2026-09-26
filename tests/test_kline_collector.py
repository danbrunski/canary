"""K-line collector: idle until a capture profile asks; owns the module's
session exactly while it reads; ends cleanly; refuses a plan it cannot read;
names every row by its module (K01.1, K01.5)."""

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
from src.collection.kline.sessions import MODULE_SESSIONS
from src.collection.kline.transport import EchoMismatchError, KlineTimeoutError, RxChunk
from src.collection.kline_collector import LOCAL_ID, KlineCollector, plan_records
from src.collection.reconnect import ReconnectPolicy
from src.config import KlineConfig
from src.decode.dids import load_did_map
from src.telemetry import TelemetryRecord
from tests.builders import abs_scan_tool_frames, kline_wire

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
        self.targets: list[int | None] = []
        # Only the first transport built in a test fails, however many follow.
        self.fail_start = fail_start if not FakeTransport.instances else 0
        FakeTransport.instances.append(self)

    def open(self) -> None:
        self.calls.append("open")

    def close(self) -> None:
        self.calls.append("close")

    def start_communication(self, target: int | None = None) -> kp.StartCommunicationReply:
        self.calls.append("start")
        self.targets.append(target)
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
        plan,
        bytes([LOCAL_ID]) + b"\x02\xc8\x40\x00",
        started=10.0,
        finished=10.02,
        module="ECM",
        address=0x10,
    )
    assert [(r.source, r.channel, r.value, r.payload) for r in records] == [
        ("kline", "ECM:1201", "02c8", b"\x02\xc8"),
        ("kline", "ECM:MEM_ECM_TARGET_EQUIV_RATIO", "4000", b"\x40\x00"),
    ]
    assert records[0].timestamp.timestamp() == pytest.approx(10.01)
    assert records[0].metadata["acquired_clock"] == "userspace"
    assert records[0].metadata["response_interval_s"] == pytest.approx(0.02)
    assert records[0].metadata["instrument"] == "command"
    assert (records[0].metadata["module"], records[0].metadata["module_address"]) == ("ECM", 0x10)


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
    assert first.channel == "ECM:1201"
    assert MODULE_SESSIONS.owner(0x10) == "kline"
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
    assert transport.targets == [0x10]  # a bare entry is the ECM
    assert tx_gate.ecm_session_owner() == "can_diagnostics"
    assert MODULE_SESSIONS.owner(0x10) == "none"


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
    did_1103 = [r for r in records if r.channel == "ECM:1103"]
    # A live value: it flickered 0x99/0x9A within UpRev's own session.
    assert did_1103 and {r.value for r in did_1103} == {"99", "9a"}
    assert {r.metadata["module"] for r in did_1103} == {"ECM"}
    # Every value row names its module; no bare DID_ survives (K01.1).
    assert not [r for r in records if r.channel.startswith("DID_")]
    assert collector.foreign_frames == 12 + 723


async def test_another_tester_owns_the_session_while_it_talks() -> None:
    collector = _sniffer([()])
    await _sniff_until_drained(collector, asyncio.Queue())
    assert tx_gate.ecm_session_owner() == "foreign"
    assert tx_gate.is_passive()  # CAN diagnostics stepped aside
    tx_gate.check_expiry(now=float("inf"))  # the daemon polls this; time passes
    assert tx_gate.ecm_session_owner() == "can_diagnostics"


async def test_status_names_the_foreign_tester_and_when_it_was_last_heard() -> None:
    """H06.4 enter / hold / release, with who held it: UpRev is tester 0xFC
    talking to the ECM (0x10)."""
    assert tx_gate.snapshot()["foreign_tester"] is None
    before = time.time()
    collector = _sniffer([()])
    await _sniff_until_drained(collector, asyncio.Queue())
    snap = tx_gate.snapshot()
    assert snap["ecm_session_owner"] == "foreign"  # enter
    heard = snap["foreign_tester"]
    assert (heard["tester_address"], heard["module_address"]) == ("FC", "10")
    assert heard["seconds_since_heard"] < 5.0
    from datetime import datetime

    assert datetime.fromisoformat(heard["last_heard_at"]).timestamp() >= before - 1.0
    assert tx_gate.check_expiry() is False  # hold: inside foreign_hold_seconds
    assert tx_gate.ecm_session_owner() == "foreign"
    tx_gate.check_expiry(now=float("inf"))  # release
    released = tx_gate.snapshot()
    assert released["ecm_session_owner"] == "can_diagnostics"
    assert released["foreign_tester"]["tester_address"] == "FC"  # still explained


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


# --------------------------------------------------------------------------
# K01.5 — an active reader for a module other than the ECM
# --------------------------------------------------------------------------

ABS_VALUES = {0x1102: b"\x00", 0x1202: b"\x00\xf5", 0x1303: b"\xa2\xfe"}


class FakeAbs(FakeTransport):
    """Answers like the ABS at 0x28: one ``22`` per DID, ``3E`` -> ``7E``."""

    values: dict[int, bytes] = ABS_VALUES
    refuse: dict[int, int] = {}

    def start_communication(self, target: int | None = None) -> kp.StartCommunicationReply:
        self.calls.append("start")
        self.targets.append(target)
        return kp.StartCommunicationReply(0xFC, 0x28, b"\x6d\x8f")

    def request(self, payload: bytes) -> bytes:
        kp.check_request(payload)
        self.calls.append(f"request {payload.hex()}")
        if payload[0] == kp.SID_READ_DATA_BY_COMMON_ID:
            did = int.from_bytes(payload[1:3], "big")
            if did in self.refuse:
                raise kp.NegativeResponseError(payload[0], self.refuse[did])
            return payload[1:3] + self.values[did]
        raise AssertionError(payload.hex())

    def tester_present(self) -> None:
        self.calls.append("tester_present")


def _abs_collector(
    entries: list[tuple[str, ...]],
    *,
    transport: type[FakeTransport] = FakeAbs,
    profile: Path = PROFILE,
    poll_interval: float = 0.01,
) -> KlineCollector:
    FakeTransport.instances.clear()
    return KlineCollector(
        KlineConfig(enabled=True, poll_interval_seconds=poll_interval, sniff=False),
        profile,
        DIDS,
        entries=lambda: entries[0],
        transport_factory=lambda profile, mode: transport(profile),  # type: ignore[arg-type,return-value]
        reconnect_policy=FAST,
    )


async def _records_until(
    collector: KlineCollector, count: int, stop: asyncio.Event
) -> tuple[asyncio.Task[None], list[TelemetryRecord]]:
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    task = asyncio.create_task(collector.run(queue, stop))
    records = [await asyncio.wait_for(queue.get(), timeout=5) for _ in range(count)]
    return task, records


async def test_an_abs_read_polls_each_did_by_common_id_and_leaves_can_alone() -> None:
    entries = [("ABS:ENGINE_SPEED", "ABS:STOP_LAMP_SWITCH", "ABS:OFF_SWITCH", "ABS:1102")]
    collector = _abs_collector(entries)
    stop = asyncio.Event()
    task, records = await _records_until(collector, 3, stop)
    # The ABS shares the wire with the ECM's K-line, not the ECM's session:
    # CAN diagnostics keep polling while it reads.
    assert tx_gate.ecm_session_owner() == "can_diagnostics"
    assert not tx_gate.is_passive()
    assert MODULE_SESSIONS.owner(0x28) == "kline"
    stop.set()
    await task
    assert [(r.channel, r.value) for r in records] == [
        ("ABS:1202", "00f5"),
        ("ABS:1303", "a2fe"),  # two flags of one word are one read
        ("ABS:1102", "00"),
    ]
    assert {(r.metadata["module"], r.metadata["module_address"]) for r in records} == {
        ("ABS", 0x28)
    }
    (transport,) = FakeTransport.instances
    assert transport.targets == [0x28]
    assert transport.calls[:5] == [
        "open",
        "start",
        "request 2212020401",
        "request 2213030401",
        "request 2211020401",
    ]
    assert transport.calls[-2:] == ["stop", "close"]
    assert MODULE_SESSIONS.owner(0x28) == "none"


async def test_passive_mode_starts_nothing_on_any_module() -> None:
    tx_gate.set_passive(True, source="test")
    for entries in ([ENTRIES], [("ABS:1202",)]):
        collector = _abs_collector(entries)
        stop = asyncio.Event()
        task = asyncio.create_task(collector.run(asyncio.Queue(), stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task
        assert FakeTransport.instances == []


class ArmsPassiveOnThirdRead(FakeAbs):
    """The operator arms passive while the third ``22`` is on the wire."""

    def request(self, payload: bytes) -> bytes:
        reply = super().request(payload)
        if sum(c.startswith("request 22") for c in self.calls) == 3:
            tx_gate.set_passive(True, source="test")
            self.calls.append("ARMED")
        return reply

    def stop_communication(self) -> None:  # pragma: no cover - must not be called
        self.calls.append("stop")


async def test_arming_passive_mid_session_sends_nothing_more() -> None:
    """Passive hands the one K-line wire to another tool: after the arm, not a
    read and not even StopCommunication (a late 82 could garble its wake-up);
    the module times the session out at P3max."""
    collector = _abs_collector([("ABS:1202",)], transport=ArmsPassiveOnThirdRead)
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(asyncio.Queue(), stop))
    for _ in range(300):
        if FakeTransport.instances and FakeTransport.instances[0].calls[-1:] == ["close"]:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    stop.set()
    await task
    (transport,) = FakeTransport.instances  # and it did not start again
    after = transport.calls[transport.calls.index("ARMED") + 1 :]
    assert after == ["close"]
    assert MODULE_SESSIONS.owner(0x28) == "none"


async def test_arming_passive_after_the_plan_resolves_sends_nothing() -> None:
    """The window between ``_wants_command`` and the wake-up: armed there, the
    session never opens the transceiver, let alone sends ``81``."""
    collector = _abs_collector([("ABS:1202",)])
    prepare = collector._prepare

    def arm_after_prepare(entries: tuple[str, ...]) -> object:
        prepared = prepare(entries)
        tx_gate.set_passive(True, source="test")
        return prepared

    collector._prepare = arm_after_prepare  # type: ignore[assignment,method-assign]
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(asyncio.Queue(), stop))
    await asyncio.sleep(0.1)
    stop.set()
    await task
    assert FakeTransport.instances == []
    assert MODULE_SESSIONS.owner(0x28) == "none"


async def test_another_tester_on_any_module_blocks_every_start() -> None:
    MODULE_SESSIONS.claim(0x28, "foreign", ttl_seconds=60.0)
    collector = _collector([ENTRIES])  # the ECM: a different module, one wire
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(asyncio.Queue(), stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert FakeTransport.instances == []
    assert tx_gate.ecm_session_owner() == "can_diagnostics"


class Garbled(FakeTransport):
    def start_communication(self, target: int | None = None) -> kp.StartCommunicationReply:
        self.calls.append("start")
        raise EchoMismatchError("echo differs: another tester is transmitting")


async def test_a_garbled_echo_hands_the_line_to_the_other_tester() -> None:
    collector = _abs_collector([("ABS:1202",)], transport=Garbled)
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(asyncio.Queue(), stop))
    for _ in range(200):
        if FakeTransport.instances and FakeTransport.instances[0].calls[-1:] == ["close"]:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert MODULE_SESSIONS.owner(None) == "foreign"
    assert len(FakeTransport.instances) == 1  # waits for them, does not retry into them


class WrongWidth(FakeAbs):
    values = {**ABS_VALUES, 0x1102: b"\x00\x00"}


class RefusesDid(FakeAbs):
    refuse = {0x1102: 0x12}


@pytest.mark.parametrize(
    ("transport", "named"),
    [(WrongWidth, "answered 2 byte"), (RefusesDid, "refused DID 0x1102")],
)
async def test_a_reply_the_profile_does_not_record_refuses_the_plan(
    transport: type[FakeTransport], named: str, caplog: pytest.LogCaptureFixture
) -> None:
    collector = _abs_collector([("ABS:1102",)], transport=transport)
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(asyncio.Queue(), stop))
    for _ in range(200):
        if FakeTransport.instances and FakeTransport.instances[0].calls[-1:] == ["close"]:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert len(FakeTransport.instances) == 1  # refused, not retried
    refusals = [r for r in caplog.records if "not retrying" in r.getMessage()]
    assert len(refusals) == 1 and named in refusals[0].getMessage()
    assert MODULE_SESSIONS.owner(0x28) == "none"


def _profile_with_keepalive(tmp_path: Path, seconds: float) -> Path:
    import yaml

    doc = yaml.safe_load(PROFILE.read_text())
    doc["modules"][0x28]["reader"]["tester_present_seconds"] = seconds
    out = tmp_path / "kline.yaml"
    out.write_text(yaml.safe_dump(doc))
    return out


async def test_tester_present_keeps_a_paused_session_open(tmp_path: Path) -> None:
    collector = _abs_collector(
        [("ABS:1202",)], profile=_profile_with_keepalive(tmp_path, 0.01), poll_interval=0.05
    )
    stop = asyncio.Event()
    task, _ = await _records_until(collector, 2, stop)
    stop.set()
    await task
    (transport,) = FakeTransport.instances
    reads = [i for i, call in enumerate(transport.calls) if call.startswith("request 22")]
    between = transport.calls[reads[0] + 1 : reads[1]]
    assert between.count("tester_present") >= 2  # a 50 ms pause, kept every 10 ms
    assert collector.keepalives >= 2


async def test_a_pause_past_p3max_without_tester_present_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ECM was never seen to answer 3E on K-line, so a pause longer than
    P3max (5 s) would lose the session between polls: refused before sending."""
    collector = _abs_collector([ENTRIES], transport=FakeTransport, poll_interval=6.0)
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(asyncio.Queue(), stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert FakeTransport.instances == []
    assert any("P3max" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# K01.1 — sniffed rows carry their module
# --------------------------------------------------------------------------


def _abs_sniffer(entries: list[tuple[str, ...]]) -> KlineCollector:
    FakeSniffTransport.instances.clear()
    stream = kline_wire(abs_scan_tool_frames(cycles=3))
    return KlineCollector(
        KlineConfig(enabled=True, foreign_hold_seconds=10.0),
        PROFILE,
        DIDS,
        entries=lambda: entries[0],
        transport_factory=lambda profile, mode: FakeSniffTransport(stream, mode),  # type: ignore[arg-type,return-value]
        reconnect_policy=FAST,
    )


async def test_a_scan_tool_on_the_abs_is_named_and_leaves_can_polling_alone() -> None:
    collector = _abs_sniffer([()])
    records = await _sniff_until_drained(collector, asyncio.Queue())
    values = {(r.channel, r.value) for r in records if r.channel != "FRAME"}
    # The ABS's 0x1210 (yaw) and the ECM's 0x1210 (TPS2) are different rows.
    assert ("ABS:1210", "ffff") in values and ("ECM:1210", "001a") in values
    # Read before any StartCommunication: unknown, not guessed.
    assert ("unknown:1202", "00f5") in values
    assert ("ABS:1202", "0000") in values
    frames = [r for r in records if r.channel == "FRAME"]
    assert {r.metadata["module"] for r in frames} == {"unknown", "ABS", "ECM"}
    assert MODULE_SESSIONS.owner(0x28) == "foreign"
    # Its last frames were the ECM's, so CAN steps aside for that hold too.
    assert tx_gate.ecm_session_owner() == "foreign"
    assert tx_gate.snapshot()["foreign_tester"]["module_address"] == "10"


def test_another_modules_local_ids_are_laid_out_only_by_its_own_widths() -> None:
    from src.collection.kline.profile import load_kline_profile
    from src.collection.kline_collector import module_did_widths

    profile = load_kline_profile(PROFILE)
    ecm_widths = {0x1201: 2}
    assert module_did_widths(profile, 0x10, ecm_widths)[0x1201] == 2
    abs_widths = module_did_widths(profile, 0x28, ecm_widths)
    assert 0x1201 not in abs_widths and abs_widths[0x1202] == 2
    assert module_did_widths(profile, None, ecm_widths) == {}


# --------------------------------------------------------------------------
# H05.8 — capture quality learns the achieved K-line rate
# --------------------------------------------------------------------------


async def _ledger_after(collector: KlineCollector, records: int) -> dict[str, object]:
    from src.collection import capture_lifetime

    capture_lifetime._reset_for_tests()
    capture_lifetime.begin(time.monotonic(), profile="kline-target", generation=1)
    stop = asyncio.Event()
    task, _ = await _records_until(collector, records, stop)
    stop.set()
    await task
    snap = capture_lifetime.snapshot(time.monotonic())
    capture_lifetime._reset_for_tests()
    assert snap is not None
    return snap.to_dict()


async def test_each_profile_entry_is_observed_for_capture_quality() -> None:
    from src.collection.capture_quality import evaluate_capture_quality

    lifetime = await _ledger_after(_collector([ENTRIES]), 20)
    channels = lifetime["channels"]
    assert isinstance(channels, dict)
    # Keyed by the entry as the profile wrote it, never by the stored channel.
    assert set(channels) == {"kline:0x1201", "kline:ECM_TARGET_EQUIV_RATIO"}
    assert all(row["observations"] >= 10 for row in channels.values())
    quality = evaluate_capture_quality(
        profile={"name": "kline-target", "mode01": {}, "kline": list(ENTRIES)},
        lifetime=lifetime,
    )
    rows = {row.channel: row for row in quality.channels}
    assert set(rows) == set(channels)
    assert all(row.cadence["hz"] and row.cadence["hz"] > 0 for row in rows.values())
    assert {row.status for row in rows.values()} == {"ok"}


async def test_two_flags_of_one_abs_word_are_both_observed() -> None:
    entries = ("ABS:STOP_LAMP_SWITCH", "ABS:OFF_SWITCH", "ABS:ENGINE_SPEED")
    lifetime = await _ledger_after(_abs_collector([entries]), 6)
    channels = lifetime["channels"]
    assert isinstance(channels, dict)
    assert set(channels) == {f"kline:{entry}" for entry in entries}


async def test_no_capture_armed_means_no_ledger_rows() -> None:
    from src.collection import capture_lifetime

    capture_lifetime._reset_for_tests()
    stop = asyncio.Event()
    task, _ = await _records_until(_collector([ENTRIES]), 4, stop)
    stop.set()
    await task
    assert capture_lifetime.snapshot(time.monotonic()) is None
