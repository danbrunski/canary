"""K-line through the CANary-HAT: read the ECU when asked, listen otherwise.

Idle by default. While the active capture profile has a non-empty ``kline:``
list (``tuning_profile``), this collector owns the ECM's diagnostic session
(``tx_gate.claim_ecm_session("kline")``, so CAN diagnostic polling pauses),
wakes the ECM with the vehicle profile's wake-up (``daemon.kline_profile``),
defines the requested set as one local identifier and polls it, emitting one
``kline:`` record per channel per reply. When the profile's list changes it
starts a new session; when it empties, or the daemon stops, it ends the
session with StopCommunication and releases the ECM to CAN diagnostics.

Each record carries the raw value bytes (``payload``) and their hex
(``value``), exactly as CAN DID rows do, so the catalog decodes both the same
way; ``acquired_at`` is the midpoint of the exchange on the host clock.

Everything it sends goes through :class:`KlineTransport`, which enforces the
TLIN1027's order and the read-only allowlist: this module cannot write the
ECU's memory.

**Otherwise it listens** (``hardware.kline.sniff``, on by default): the
transceiver in sniff mode, pull-up off, unable to transmit. Every frame
another tester and the ECU exchange is recorded as ``kline:FRAME`` (raw
bytes, direction), values it can lay out from a tracked ``AC`` definition or
a ``22`` read become ``kline:DID_*``/``kline:RAM_*`` rows marked
``instrument: sniff``, and while that tester is active the ECM session is
theirs (``tx_gate`` owner ``foreign``, renewed on each of its frames and held
``foreign_hold_seconds`` after the last): CAN diagnostics step aside, and a
capture profile's K-line request waits rather than colliding with it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from src.collection import tuning_profile, tx_gate
from src.collection.kline import protocol as kp
from src.collection.kline.plan import KlinePlan, KlinePlanError, resolve_plan, split_reply
from src.collection.kline.profile import (
    KlineNotConfiguredError,
    KlineProfile,
    load_kline_profile,
)
from src.collection.kline.sniff import DefinitionTracker, Exchange, FrameDecoder, SniffedFrame
from src.collection.kline.transport import HatPins, KlineTransport, KlineTransportError, Mode
from src.collection.queueing import QueueDropCounter, put_nowait_or_drop
from src.collection.reconnect import ReconnectPolicy
from src.config import KlineConfig
from src.decode.dids import DidMap, load_did_map
from src.telemetry import KLINE_SOURCE, TelemetryRecord

log = logging.getLogger(__name__)

#: The local identifier CANary defines. UpRev uses 0x81-0x83; any free id
#: works, and a single one is enough for a plan that fits one frame.
LOCAL_ID = 0x81
#: How often an idle collector re-reads the active profile.
IDLE_CHECK_SECONDS = 0.5

#: How long one sniff read gathers bytes before decoding them.
SNIFF_BURST_SECONDS = 0.05

TransportFactory = Callable[[KlineProfile, Mode], KlineTransport]
EntriesSource = Callable[[], tuple[str, ...]]


def _active_entries() -> tuple[str, ...]:
    return tuning_profile.active_profile().kline


def plan_records(
    plan: KlinePlan, params: bytes, *, started: float, finished: float
) -> list[TelemetryRecord]:
    """One ``kline:`` record per planned channel from a ``61 lid`` reply."""
    values = kp.split_local_id_reply(LOCAL_ID, params, plan.descriptor_widths)
    stamp = (started + finished) / 2.0
    metadata: dict[str, str | float | None] = {
        "acquired_at": stamp,
        "acquired_clock": "userspace",
        "response_interval_s": finished - started,
    }
    return [
        TelemetryRecord.from_epoch(
            stamp,
            source=KLINE_SOURCE,
            channel=channel,
            value=raw.hex(),
            payload=raw,
            metadata=dict(metadata),
        )
        for channel, raw in split_reply(plan, values)
    ]


class KlineCollector:
    def __init__(
        self,
        config: KlineConfig,
        profile_path: Path,
        dids_path: Path,
        *,
        entries: EntriesSource = _active_entries,
        transport_factory: TransportFactory | None = None,
        reconnect_policy: ReconnectPolicy | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.profile_path = profile_path
        self.dids_path = dids_path
        self._entries = entries
        pins = HatPins(
            tty=config.tty,
            gpio_chip=config.gpio_chip,
            enable=config.enable_gpio,
            pullup=config.pullup_gpio,
        )
        self._factory = transport_factory or (
            lambda profile, mode: KlineTransport(profile, mode=mode, pins=pins)
        )
        self._policy = reconnect_policy or ReconnectPolicy(
            initial_delay_seconds=2.0, max_delay_seconds=30.0
        )
        self._clock = clock
        self._refused: tuple[str, ...] | None = None
        self._said: set[str] = set()
        self.drop_counter = QueueDropCounter("kline")
        self.sessions = 0
        self.replies = 0
        self.sniffed_frames = 0
        self.foreign_frames = 0

    def _say_once(self, key: str, message: str, *args: object) -> None:
        if key not in self._said:
            self._said.add(key)
            log.warning(message, *args)

    def _wants_command(self) -> tuple[str, ...] | None:
        """The requested channels, if a command session may start now."""
        entries = self._entries()
        if not entries or entries == self._refused:
            return None
        if tx_gate.ecm_session_owner() == "foreign":
            self._say_once(
                "foreign-wait",
                "K-line requested by the capture profile; waiting for another tester to finish",
            )
            return None
        self._said.discard("foreign-wait")
        return entries

    async def run(self, queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        delay = self._policy.initial_delay_seconds
        while not stop.is_set():
            entries = self._wants_command()
            if entries is None:
                if not self.config.sniff:
                    await _wait(stop, IDLE_CHECK_SECONDS)
                    continue
                try:
                    await self._sniff(queue, stop)
                    delay = self._policy.initial_delay_seconds
                except (KlineTransportError, OSError) as exc:
                    log.warning(
                        "K-line sniffing failed; retrying in %.0f s",
                        delay,
                        extra={"error": str(exc), "retry_seconds": delay},
                    )
                    await _wait(stop, delay)
                    delay = self._policy.next_delay(delay)
                continue
            prepared = self._prepare(entries)
            if prepared is None:
                self._refused = entries
                continue
            profile, plan = prepared
            try:
                await self._session(profile, plan, entries, queue, stop)
                delay = self._policy.initial_delay_seconds
            except (KlineTransportError, kp.KlineProtocolError, kp.NegativeResponseError) as exc:
                log.warning(
                    "K-line session failed; retrying in %.0f s",
                    delay,
                    extra={"error": str(exc), "retry_seconds": delay},
                )
                await _wait(stop, delay)
                delay = self._policy.next_delay(delay)

    def _prepare(self, entries: tuple[str, ...]) -> tuple[KlineProfile, KlinePlan] | None:
        """Load the vehicle profile and resolve the plan, or say why not (once)."""
        try:
            profile = load_kline_profile(self.profile_path)
            did_map: DidMap = load_did_map(self.dids_path)
            plan = resolve_plan(entries, did_map)
        except (KlineNotConfiguredError, KlinePlanError, ValueError) as exc:
            log.error(
                "K-line requested by the capture profile but cannot run: %s",
                exc,
                extra={"kline_entries": list(entries)},
            )
            return None
        return profile, plan

    async def _session(
        self,
        profile: KlineProfile,
        plan: KlinePlan,
        entries: tuple[str, ...],
        queue: asyncio.Queue[TelemetryRecord],
        stop: asyncio.Event,
    ) -> None:
        tx_gate.claim_ecm_session("kline")
        transport = self._factory(profile, Mode.COMMAND)
        try:
            await asyncio.to_thread(transport.open)
            try:
                reply = await asyncio.to_thread(transport.start_communication)
                self.sessions += 1
                log.info(
                    "K-line session started",
                    extra={"key_bytes": reply.key_bytes.hex(), "channels": list(entries)},
                )
                definition = kp.define_local_id(LOCAL_ID, plan.descriptors)
                acknowledged = await asyncio.to_thread(transport.request, definition)
                if acknowledged[:1] != bytes([LOCAL_ID]):
                    raise kp.KlineProtocolError(f"definition acknowledged as {acknowledged.hex()}")
                poll = kp.read_local_id(LOCAL_ID, profile.trailer())
                while not stop.is_set() and self._entries() == entries:
                    started = self._clock()
                    params = await asyncio.to_thread(transport.request, poll)
                    finished = self._clock()
                    self.replies += 1
                    for record in plan_records(plan, params, started=started, finished=finished):
                        put_nowait_or_drop(queue, record, on_drop=self.drop_counter)
                    if self.config.poll_interval_seconds:
                        await _wait(stop, self.config.poll_interval_seconds)
                try:
                    await asyncio.to_thread(transport.stop_communication)
                except (KlineTransportError, kp.KlineProtocolError, kp.NegativeResponseError):
                    pass  # the session ends either way; the ECM times it out
            finally:
                await asyncio.to_thread(transport.close)
        finally:
            tx_gate.release_ecm_session("kline")

    async def _sniff(self, queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        """Listen until stopped or until a command session may start."""
        try:
            profile = load_kline_profile(self.profile_path)
            did_map = load_did_map(self.dids_path)
        except (KlineNotConfiguredError, ValueError) as exc:
            self._say_once("sniff-unconfigured", "K-line sniffing unavailable: %s", exc)
            await _wait(stop, IDLE_CHECK_SECONDS * 10)
            return
        widths = {e.did: e.response_length for e in did_map.dids if e.response_length}
        decoder = FrameDecoder()
        tracker = DefinitionTracker(widths)
        pending: SniffedFrame | None = None
        transport = self._factory(profile, Mode.SNIFF)
        await asyncio.to_thread(transport.open)
        try:
            while not stop.is_set() and self._wants_command() is None:
                burst = await asyncio.to_thread(transport.read_burst, SNIFF_BURST_SECONDS)
                to_epoch = time.time() - time.monotonic()
                for at, byte in burst:
                    for frame in decoder.feed(at, byte):
                        self.sniffed_frames += 1
                        stamp = frame.finished + to_epoch
                        put_nowait_or_drop(
                            queue, frame_record(frame, stamp), on_drop=self.drop_counter
                        )
                        if frame.direction == "tester":
                            self.foreign_frames += 1
                            tx_gate.claim_ecm_session(
                                "foreign", ttl_seconds=self.config.foreign_hold_seconds
                            )
                            pending = frame
                            continue
                        if pending is None:
                            continue
                        exchange, pending = Exchange(pending, frame), None
                        for record in sniffed_value_records(exchange, tracker, stamp):
                            put_nowait_or_drop(queue, record, on_drop=self.drop_counter)
        finally:
            await asyncio.to_thread(transport.close)


def frame_record(frame: SniffedFrame, stamp: float) -> TelemetryRecord:
    metadata: dict[str, str | int | None] = {
        "instrument": "sniff",
        "direction": frame.direction,
        "kind": frame.kind,
    }
    if frame.source is not None:
        metadata["source_address"] = frame.source
        metadata["target_address"] = frame.target
    return TelemetryRecord.from_epoch(
        stamp,
        source=KLINE_SOURCE,
        channel="FRAME",
        value=frame.raw.hex(),
        payload=frame.raw,
        metadata=dict(metadata),
    )


def sniffed_value_records(
    exchange: Exchange, tracker: DefinitionTracker, stamp: float
) -> list[TelemetryRecord]:
    """Values another tester read: a tracked ``21`` set, or a ``22`` DID."""
    request, reply = exchange.request, exchange.reply
    if reply is None:
        return []
    pairs: list[tuple[str, bytes, dict[str, str | int | None]]] = []
    if request.payload[:1] == bytes([kp.SID_READ_DATA_BY_COMMON_ID]):
        if reply.payload[:1] == bytes([kp.SID_READ_DATA_BY_COMMON_ID + kp.POSITIVE_OFFSET]):
            did = int.from_bytes(request.payload[1:3], "big")
            try:
                value = kp.split_did_reply(did, reply.payload[1:])
            except kp.KlineProtocolError:
                return []
            pairs.append((f"DID_{did:04X}", value, {}))
    else:
        observed = tracker.observe(exchange)
        if observed is None or observed.unresolved is not None:
            return []
        for descriptor, value in observed.values:
            name = (
                f"DID_{descriptor.did:04X}"
                if isinstance(descriptor, kp.DidDescriptor)
                else f"RAM_{descriptor.address:08X}"
            )
            pairs.append((name, value, {"local_id": observed.local_id}))
    return [
        TelemetryRecord.from_epoch(
            stamp,
            source=KLINE_SOURCE,
            channel=name,
            value=value.hex(),
            payload=value,
            metadata={"instrument": "sniff", **extra},
        )
        for name, value, extra in pairs
    ]


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


__all__ = [
    "LOCAL_ID",
    "KlineCollector",
    "frame_record",
    "plan_records",
    "sniffed_value_records",
]
