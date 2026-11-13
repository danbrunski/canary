"""One K-line module session, however it is asked for (R23.10).

The polling session (``KlineCollector._session``) and a queued diagnostic
function (``_run_action``) both claim the module, open the transceiver, wake
the module, run, stop it (swallowing a refused ``82``), close the transceiver,
record CANary's own wake-ups, and release. These tests pin that lifecycle for
both entry points, including the paths where it fails half way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.collection import tx_gate
from src.collection.kline import protocol as kp
from src.collection.kline.actions import KlineAction, KlineActionResult
from src.collection.kline.profile import KlineProfile
from src.collection.kline.sessions import MODULE_SESSIONS
from src.collection.kline.transport import KlineTimeoutError, WakePulse
from src.collection.kline_collector import KlineCollector
from src.config import KlineConfig
from src.telemetry import TelemetryRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
DIDS = REPO_ROOT / "data/dids.yaml"
PROFILE = REPO_ROOT / "data/nissan-xterra-2008/kline.yaml"
ENTRIES = ("0x1201", "ECM_TARGET_EQUIV_RATIO")
ECM = 0x10
SRS = 0x58


@pytest.fixture(autouse=True)
def _clean_gate() -> Iterator[None]:
    tx_gate.reset_ecm_session_claims()
    tx_gate.set_passive(False, source="test")
    yield
    tx_gate.reset_ecm_session_claims()
    tx_gate.set_passive(False, source="test")


class Transport:
    """Records its calls; fails where told; reports one wake-up."""

    def __init__(
        self,
        profile: KlineProfile,
        *,
        fail_start: bool = False,
        fail_stop: bool = False,
        pulse_target: int = ECM,
    ) -> None:
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.pulse_target = pulse_target
        self.calls: list[str] = []
        #: Who held the lines while the transport was open.
        self.held: list[tuple[str, str]] = []

    def _holders(self) -> tuple[str, str]:
        return (tx_gate.ecm_session_owner(), MODULE_SESSIONS.owner(self.pulse_target))

    def open(self) -> None:
        self.calls.append("open")
        self.held.append(self._holders())

    def close(self) -> None:
        self.calls.append("close")

    def start_communication(self, target: int | None = None) -> kp.StartCommunicationReply:
        self.calls.append("start")
        if self.fail_start:
            raise KlineTimeoutError("no StartCommunication reply")
        return kp.StartCommunicationReply(0xFC, 0x10, b"\x5d\x8f")

    def request(self, payload: bytes) -> bytes:
        self.calls.append(f"request {payload[:2].hex()}")
        if payload[0] == kp.SID_DEFINE_LOCAL_ID:
            return bytes([payload[1]])
        if payload[0] == kp.SID_READ_DATA_BY_LOCAL_ID:
            return bytes([payload[1]]) + b"\x02\xc8" + b"\x40\x00"
        return b""

    def stop_communication(self) -> None:
        self.calls.append("stop")
        if self.fail_stop:
            raise KlineTimeoutError("no StopCommunication reply")

    def take_wake_pulses(self) -> list[WakePulse]:
        return [
            WakePulse(
                name="fast",
                wanted_low_ms=25.0,
                wanted_high_ms=25.0,
                fall=1.0,
                release=1.025,
                end=1.05,
                readback=b"\x00",
                char_s=0.001,
                target=self.pulse_target,
                answered=not self.fail_start,
            )
        ]


def _collector(transports: list[Transport], **kwargs: bool) -> KlineCollector:
    def factory(profile: KlineProfile, mode: object) -> Transport:
        transport = Transport(profile, **kwargs)
        transports.append(transport)
        return transport

    return KlineCollector(
        KlineConfig(enabled=True, poll_interval_seconds=0.01, sniff=False),
        PROFILE,
        DIDS,
        entries=lambda: ENTRIES,
        transport_factory=factory,  # type: ignore[arg-type]
    )


def _action() -> KlineAction:
    return KlineAction(kind="clear_codes", module="ECM", address=ECM, request=b"\x14")


async def _poll(collector: KlineCollector, queue: asyncio.Queue[TelemetryRecord]) -> None:
    prepared = collector._prepare(ENTRIES)
    assert prepared is not None
    profile, plan = prepared
    # One poll round, then stop: the stop event is set after the first read.
    stop = asyncio.Event()
    stop.set()
    await collector._session(profile, plan, ENTRIES, queue, stop)


async def _act(
    collector: KlineCollector, queue: asyncio.Queue[TelemetryRecord]
) -> KlineActionResult:
    action = _action()
    await collector._run_action(action, queue)
    return action.future.result()


def _wake_rows(queue: asyncio.Queue[TelemetryRecord]) -> list[TelemetryRecord]:
    rows = [queue.get_nowait() for _ in range(queue.qsize())]
    return [r for r in rows if r.channel.endswith("BREAK")]


def _released() -> None:
    assert MODULE_SESSIONS.owner(ECM) == "none"
    assert tx_gate.ecm_session_owner() == "can_diagnostics"


async def test_a_polling_session_holds_the_module_while_open_and_releases_after() -> None:
    transports: list[Transport] = []
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    collector = _collector(transports)
    await _poll(collector, queue)
    (transport,) = transports
    assert transport.held == [("kline", "kline")]
    assert transport.calls[0:2] == ["open", "start"]
    assert transport.calls[-2:] == ["stop", "close"]
    assert collector.sessions == 1
    assert len(_wake_rows(queue)) == 1
    _released()


async def test_a_polling_session_that_cannot_wake_the_module_still_closes_and_releases() -> None:
    transports: list[Transport] = []
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    collector = _collector(transports, fail_start=True)
    with pytest.raises(KlineTimeoutError):
        await _poll(collector, queue)
    (transport,) = transports
    assert transport.calls == ["open", "start", "close"]  # no stop for a session never begun
    assert collector.sessions == 0
    assert len(_wake_rows(queue)) == 1  # the failed wake-up is still recorded
    _released()


async def test_a_polling_session_ignores_a_refused_stop() -> None:
    transports: list[Transport] = []
    collector = _collector(transports, fail_stop=True)
    await _poll(collector, asyncio.Queue())  # does not raise
    assert transports[0].calls[-2:] == ["stop", "close"]
    _released()


async def test_an_action_holds_the_module_while_open_and_releases_after() -> None:
    transports: list[Transport] = []
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    collector = _collector(transports)
    result = await _act(collector, queue)
    (transport,) = transports
    assert result.ok
    assert transport.held == [("kline", "kline")]
    assert transport.calls == ["open", "start", "request 14", "stop", "close"]
    assert collector.actions_run == 1
    assert len(_wake_rows(queue)) == 1
    _released()


async def test_an_action_that_cannot_wake_the_module_reports_it_and_releases() -> None:
    transports: list[Transport] = []
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    collector = _collector(transports, fail_start=True)
    result = await _act(collector, queue)
    (transport,) = transports
    assert not result.ok and result.detail.startswith("K-line failed")
    assert transport.calls == ["open", "start", "close"]
    assert collector.actions_run == 0
    assert len(_wake_rows(queue)) == 1
    _released()


async def test_an_action_ignores_a_refused_stop() -> None:
    transports: list[Transport] = []
    collector = _collector(transports, fail_stop=True)
    result = await _act(collector, asyncio.Queue())
    assert result.ok  # the function ran; the module times the session out
    assert transports[0].calls[-2:] == ["stop", "close"]
    _released()


async def test_an_action_without_a_queue_still_closes_and_releases() -> None:
    transports: list[Transport] = []
    collector = _collector(transports)
    action = _action()
    await collector._run_action(action)
    assert action.future.result().ok
    assert transports[0].calls[-1] == "close"
    _released()


class _ArmsPassiveDuringRequest(Transport):
    def request(self, payload: bytes) -> bytes:
        tx_gate.set_passive(True, source="test")
        return super().request(payload)


async def test_an_action_sends_no_stop_once_passive_is_armed() -> None:
    """The polling session already did not (R23.10 gave the action the same
    rule: a late 82 could garble the tool that now has the wire)."""
    transports: list[Transport] = []

    def factory(profile: KlineProfile, mode: object) -> Transport:
        transport = _ArmsPassiveDuringRequest(profile)
        transports.append(transport)
        return transport

    collector = KlineCollector(
        KlineConfig(enabled=True, poll_interval_seconds=0.01, sniff=False),
        PROFILE,
        DIDS,
        entries=lambda: ENTRIES,
        transport_factory=factory,  # type: ignore[arg-type]
    )
    result = await _act(collector, asyncio.Queue())
    assert result.ok
    assert transports[0].calls == ["open", "start", "request 14", "close"]
    _released()
