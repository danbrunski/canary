"""K-line through the CANary-HAT: read a module when asked, listen otherwise.

Idle by default. While the active capture profile has a non-empty ``kline:``
list (``tuning_profile``) naming one module (``ABS:1202``; a bare entry is the
ECM), this collector opens a session with that module: it wakes the line with
the vehicle profile's wake-up (``daemon.kline_profile``), sends
StartCommunication to the module's address and reads what was asked —

- **the ECM**: the set as one ``AC`` local identifier, polled with ``21``, the
  definition refused beforehand if its reply would pass the ECM's
  ``max_local_id_reply_bytes``. It owns the ECM's diagnostic session for the
  duration (``tx_gate.claim_ecm_session("kline")``, so CAN diagnostic polling
  pauses);
- **any other module**: one ``22 hh ll`` per DID, each reply checked against
  the width the profile recorded. CAN is not paused: the ABS shares the wire
  with the ECM's K-line, not its session.

It emits one ``kline:<module>:<item>`` record per value per reply
(``src.collection.kline.names``). TesterPresent (``3E``) keeps a paused
session open where the module was seen to answer it. When the profile's list
changes it starts a new session; when it empties, when passive mode is armed,
or when the daemon stops, it ends the session with StopCommunication and
releases the claims (``src.collection.kline.sessions``).

Each record carries the raw value bytes (``payload``) and their hex
(``value``); ``acquired_at`` is the midpoint of the exchange on the host
clock. Decoding to engineering units is the profile's bindings, not the
collector's.

Everything it sends goes through :class:`KlineTransport`, which enforces the
TLIN1027's order and the read-only allowlist: this module cannot write any
module's memory, and cannot send the IOControl a scan tool uses to calibrate.

**It starts nothing** while passive mode is armed (``tx_gate.passive_armed``:
the operator armed it for another tool) or while another tester is on the
line (any module's ``foreign`` claim) — K-line is one wire.

**Otherwise it listens** (``hardware.kline.sniff``, on by default): the
transceiver in sniff mode, pull-up off, unable to transmit. Every frame
another tester and a module exchange is recorded as ``kline:FRAME`` (raw
bytes, direction, module), values it can lay out from a tracked ``AC``
definition or a ``22`` read become ``kline:<module>:<hex>`` /
``kline:<module>:RAM_*`` rows marked ``instrument: sniff``, and while that
tester is active the module it talks to is theirs (``foreign``, renewed on
each of its frames and held ``foreign_hold_seconds`` after the last). When
that module is the ECM, or unknown, the ECM's CAN session is theirs too
(``tx_gate`` owner ``foreign``) and CAN diagnostics step aside; a scan tool
on the ABS leaves CAN polling alone.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from queue import Empty, SimpleQueue

from src.collection import capture_lifetime, tuning_profile, tx_gate
from src.collection.kline import protocol as kp
from src.collection.kline.actions import KLINE_ACTIONS, KlineAction, KlineActions
from src.collection.kline.names import FRAME_CHANNEL, capture_key, did_channel, ram_channel
from src.collection.kline.plan import (
    KlineCatalog,
    KlinePlan,
    KlinePlanError,
    ModulePlan,
    split_reply,
)
from src.collection.kline.profile import (
    KlineNotConfiguredError,
    KlineProfile,
    load_kline_profile,
)
from src.collection.kline.sessions import MODULE_SESSIONS
from src.collection.kline.sniff import DefinitionTracker, Exchange, FrameDecoder, SniffedFrame
from src.collection.kline.transport import (
    BusBusyError,
    EchoMismatchError,
    HatPins,
    KlineTransport,
    KlineTransportError,
    Mode,
    PassiveArmedError,
    RxChunk,
)
from src.collection.queueing import QueueDropCounter, put_nowait_or_drop
from src.collection.reconnect import ReconnectPolicy, wait_for_stop_or_timeout
from src.config import KlineConfig
from src.decode.dids import load_did_map
from src.telemetry import KLINE_SOURCE, TelemetryRecord

log = logging.getLogger(__name__)

#: The local identifier CANary defines. UpRev uses 0x81-0x83; any free id
#: works, and a single one is enough for a plan that fits one frame.
LOCAL_ID = 0x81
#: How often an idle collector re-reads the active profile.
IDLE_CHECK_SECONDS = 0.5

#: How often the event loop decodes what the sniff reader has gathered. Only
#: latency: the reader stamps bytes as it reads them, on its own thread.
SNIFF_DRAIN_SECONDS = 0.05

TransportFactory = Callable[[KlineProfile, Mode], KlineTransport]
EntriesSource = Callable[[], tuple[str, ...]]


def _active_entries() -> tuple[str, ...]:
    return tuning_profile.active_profile().kline


def _command_metadata(
    started: float, finished: float, module: str, address: int
) -> tuple[float, dict[str, str | int | float | None]]:
    """The exchange's midpoint and the metadata every commanded row carries."""
    stamp = (started + finished) / 2.0
    return stamp, {
        "acquired_at": stamp,
        "acquired_clock": "userspace",
        "response_interval_s": finished - started,
        "instrument": "command",
        "module": module,
        "module_address": address,
    }


def plan_records(
    plan: KlinePlan,
    params: bytes,
    *,
    started: float,
    finished: float,
    module: str,
    address: int,
) -> list[TelemetryRecord]:
    """One ``kline:<module>:<item>`` record per planned channel from a
    ``61 lid`` reply."""
    values = kp.split_local_id_reply(LOCAL_ID, params, plan.descriptor_widths)
    stamp, metadata = _command_metadata(started, finished, module, address)
    return [
        TelemetryRecord.from_epoch(
            stamp,
            source=KLINE_SOURCE,
            channel=f"{module}:{channel}",
            value=raw.hex(),
            payload=raw,
            metadata=dict(metadata),
        )
        for channel, raw in split_reply(plan, values)
    ]


def did_record(
    did: int, value: bytes, *, started: float, finished: float, module: str, address: int
) -> TelemetryRecord:
    """The record for one ``22`` read CANary made itself."""
    stamp, metadata = _command_metadata(started, finished, module, address)
    return TelemetryRecord.from_epoch(
        stamp,
        source=KLINE_SOURCE,
        channel=did_channel(module, did),
        value=value.hex(),
        payload=value,
        metadata=metadata,
    )


def _observe_for_capture(
    by_channel: Mapping[str, tuple[str, ...]], records: Iterable[TelemetryRecord]
) -> None:
    """Feed the capture ledger one observation per profile entry delivered.

    Keyed by the entry (:func:`~src.collection.kline.names.capture_key`), so
    capture quality reports the achieved K-line rate of what the profile
    asked for (H05.8). A no-op while no capture is armed.
    """
    now = time.monotonic()
    for record in records:
        for entry in by_channel.get(record.channel, ()):
            capture_lifetime.observe(capture_key(entry), now)


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
        actions: KlineActions = KLINE_ACTIONS,
    ) -> None:
        self.config = config
        self._actions = actions
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
        self.keepalives = 0
        self.sniffed_frames = 0
        self.foreign_frames = 0
        self.actions_run = 0

    def _say_once(self, key: str, message: str, *args: object) -> None:
        if key not in self._said:
            self._said.add(key)
            log.warning(message, *args)

    def _wants_command(self) -> tuple[str, ...] | None:
        """The requested channels, if a command session may start now."""
        entries = self._entries()
        if not entries or entries == self._refused:
            return None
        if tx_gate.passive_armed():
            self._say_once(
                "passive-wait",
                "K-line requested by the capture profile; passive mode is armed, so it waits",
            )
            return None
        self._said.discard("passive-wait")
        if MODULE_SESSIONS.foreign_active() or tx_gate.ecm_session_owner() == "foreign":
            self._say_once(
                "foreign-wait",
                "K-line requested by the capture profile; waiting for another tester to finish",
            )
            return None
        self._said.discard("foreign-wait")
        return entries

    @staticmethod
    def _line_is_theirs() -> bool:
        """Passive armed, or another tester on the line (any module)."""
        return (
            tx_gate.passive_armed()
            or MODULE_SESSIONS.foreign_active()
            or tx_gate.ecm_session_owner() == "foreign"
        )

    def _may_continue(self, entries: tuple[str, ...], stop: asyncio.Event) -> bool:
        """Whether an open command session keeps going. Passive armed mid-session
        ends it: the operator is handing the line to another tool. A queued
        diagnostic function ends it too; the next pass runs the function and
        then reopens the session."""
        return (
            not stop.is_set()
            and self._entries() == entries
            and not tx_gate.passive_armed()
            and not self._actions.pending()
        )

    async def run(self, queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        self._actions.attach()
        try:
            await self._run(queue, stop)
        finally:
            self._actions.detach()

    async def _run(self, queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        delay = self._policy.initial_delay_seconds
        while not stop.is_set():
            action = self._actions.take()
            if action is not None:
                try:
                    await self._run_action(action)
                finally:
                    self._actions.done(action)
                continue
            entries = self._wants_command()
            if entries is None:
                if not self.config.sniff:
                    await wait_for_stop_or_timeout(stop, IDLE_CHECK_SECONDS)
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
                    await wait_for_stop_or_timeout(stop, delay)
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
            except PassiveArmedError:
                # Armed mid-exchange: the transport refused the next byte.
                # Not a failure and not a reason to back off; the next pass
                # waits in _wants_command until the operator disarms.
                log.info("K-line session ended: passive mode armed")
            except KlinePlanError as exc:
                # The module refused what the plan asked, or answered in a shape
                # the profile does not record: asking again will not change it.
                log.error(
                    "K-line read refused by the module; not retrying this plan: %s",
                    exc,
                    extra={"kline_entries": list(entries)},
                )
                self._refused = entries
            except (KlineTransportError, kp.KlineProtocolError, kp.NegativeResponseError) as exc:
                if isinstance(exc, EchoMismatchError | BusBusyError):
                    # Another tester is on the line: it is theirs until it
                    # has been quiet as long as the sniffer would wait.
                    MODULE_SESSIONS.claim(
                        None, "foreign", ttl_seconds=self.config.foreign_hold_seconds
                    )
                log.warning(
                    "K-line session failed; retrying in %.0f s",
                    delay,
                    extra={"error": str(exc), "retry_seconds": delay},
                )
                await wait_for_stop_or_timeout(stop, delay)
                delay = self._policy.next_delay(delay)

    def _prepare(self, entries: tuple[str, ...]) -> tuple[KlineProfile, ModulePlan] | None:
        """Load the vehicle profile and resolve the plan, or say why not (once).

        The resolution is :meth:`KlineCatalog.resolve`, the same one a
        capture-profile save runs (H05.8)."""
        catalog = KlineCatalog(self.profile_path, self.dids_path, self.config.poll_interval_seconds)
        try:
            profile, plan = catalog.resolve(entries)
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
        plan: ModulePlan,
        entries: tuple[str, ...],
        queue: asyncio.Queue[TelemetryRecord],
        stop: asyncio.Event,
    ) -> None:
        if self._line_is_theirs():
            # Armed (or another tester heard) since _wants_command: not one
            # byte, not even the transceiver's enable.
            return
        is_ecm = plan.address == profile.ecm_address
        MODULE_SESSIONS.claim(plan.address, "kline")
        if is_ecm:
            tx_gate.claim_ecm_session("kline")
        transport = self._factory(profile, Mode.COMMAND)
        try:
            await asyncio.to_thread(transport.open)
            try:
                reply = await asyncio.to_thread(transport.start_communication, plan.address)
                self.sessions += 1
                log.info(
                    "K-line session started",
                    extra={
                        "module": plan.label,
                        "key_bytes": reply.key_bytes.hex(),
                        "channels": list(entries),
                    },
                )
                if plan.local is not None:
                    await self._poll_local_id(transport, profile, plan, entries, queue, stop)
                else:
                    await self._poll_common_ids(transport, profile, plan, entries, queue, stop)
                if not tx_gate.passive_armed():
                    # Armed passive hands the wire to another tool: a late 82
                    # could garble its wake-up, so the module times out at
                    # P3max instead (the transport refuses it anyway).
                    try:
                        await asyncio.to_thread(transport.stop_communication)
                    except (KlineTransportError, kp.KlineProtocolError, kp.NegativeResponseError):
                        pass  # the session ends either way; the module times it out
            finally:
                await asyncio.to_thread(transport.close)
        finally:
            MODULE_SESSIONS.release(plan.address, "kline")
            if is_ecm:
                tx_gate.release_ecm_session("kline")

    async def _run_action(self, action: KlineAction) -> None:
        """One diagnostic function in a session of its own: StartCommunication,
        the request, StopCommunication. Always finishes ``action``'s future."""
        started = time.time()
        if self._line_is_theirs():
            action.finish(
                ok=False,
                started=started,
                detail="refused: passive mode is armed or another tester holds the line",
            )
            return
        try:
            profile = load_kline_profile(self.profile_path)
        except (KlineNotConfiguredError, ValueError) as exc:
            action.finish(ok=False, started=started, detail=f"refused: {exc}")
            return
        is_ecm = action.address == profile.ecm_address
        MODULE_SESSIONS.claim(action.address, "kline")
        if is_ecm:
            tx_gate.claim_ecm_session("kline")
        transport = self._factory(profile, Mode.COMMAND)
        try:
            await asyncio.to_thread(transport.open)
            try:
                await asyncio.to_thread(transport.start_communication, action.address)
                params = await asyncio.to_thread(transport.request, action.request)
                try:
                    await asyncio.to_thread(transport.stop_communication)
                except (KlineTransportError, kp.KlineProtocolError, kp.NegativeResponseError):
                    pass  # the function is done; the module times the session out
            finally:
                await asyncio.to_thread(transport.close)
        except kp.NegativeResponseError as exc:
            action.finish(ok=False, started=started, detail=f"{action.module} refused: {exc}")
        except (KlineTransportError, kp.KlineProtocolError) as exc:
            action.finish(ok=False, started=started, detail=f"K-line failed: {exc}")
        else:
            self.actions_run += 1
            reply = bytes([action.request[0] + kp.POSITIVE_OFFSET]) + params
            action.finish(ok=True, started=started, reply=reply, detail="positive response")
        finally:
            MODULE_SESSIONS.release(action.address, "kline")
            if is_ecm:
                tx_gate.release_ecm_session("kline")
        result = action.future.result()
        log.info(
            "K-line %s on %s: %s",
            action.kind,
            action.module,
            result.detail,
            extra={"kline_action": result.to_dict()},
        )

    async def _poll_local_id(
        self,
        transport: KlineTransport,
        profile: KlineProfile,
        plan: ModulePlan,
        entries: tuple[str, ...],
        queue: asyncio.Queue[TelemetryRecord],
        stop: asyncio.Event,
    ) -> None:
        assert plan.local is not None
        definition = kp.define_local_id(LOCAL_ID, plan.local.descriptors)
        acknowledged = await asyncio.to_thread(transport.request, definition)
        if acknowledged[:1] != bytes([LOCAL_ID]):
            raise kp.KlineProtocolError(f"definition acknowledged as {acknowledged.hex()}")
        poll = kp.read_local_id(LOCAL_ID, profile.trailer())
        by_channel = plan.entries_by_channel()
        while self._may_continue(entries, stop):
            started = self._clock()
            params = await asyncio.to_thread(transport.request, poll)
            finished = self._clock()
            self.replies += 1
            records = plan_records(
                plan.local,
                params,
                started=started,
                finished=finished,
                module=plan.label,
                address=plan.address,
            )
            for record in records:
                put_nowait_or_drop(queue, record, on_drop=self.drop_counter)
            _observe_for_capture(by_channel, records)
            await self._pause(transport, plan, entries, stop)

    async def _poll_common_ids(
        self,
        transport: KlineTransport,
        profile: KlineProfile,
        plan: ModulePlan,
        entries: tuple[str, ...],
        queue: asyncio.Queue[TelemetryRecord],
        stop: asyncio.Event,
    ) -> None:
        by_channel = plan.entries_by_channel()
        while self._may_continue(entries, stop):
            for entry in plan.reads:
                if not self._may_continue(entries, stop):
                    return
                request = kp.read_did(entry.did, profile.trailer())
                started = self._clock()
                try:
                    params = await asyncio.to_thread(transport.request, request)
                except kp.NegativeResponseError as exc:
                    if exc.code == 0x21:  # busy-repeatRequest: transient
                        raise
                    raise KlinePlanError(
                        f"{plan.label} refused DID 0x{entry.did:04X}: {exc}"
                    ) from exc
                finished = self._clock()
                value = kp.split_did_reply(entry.did, params)
                if len(value) != entry.width:
                    raise KlinePlanError(
                        f"{plan.label} DID 0x{entry.did:04X} answered {len(value)} byte(s); "
                        f"the profile records {entry.width}"
                    )
                self.replies += 1
                record = did_record(
                    entry.did,
                    value,
                    started=started,
                    finished=finished,
                    module=plan.label,
                    address=plan.address,
                )
                put_nowait_or_drop(queue, record, on_drop=self.drop_counter)
                _observe_for_capture(by_channel, (record,))
            await self._pause(transport, plan, entries, stop)

    async def _pause(
        self,
        transport: KlineTransport,
        plan: ModulePlan,
        entries: tuple[str, ...],
        stop: asyncio.Event,
    ) -> None:
        """The configured pause between reads, kept alive with TesterPresent
        where the module answers it."""
        remaining = self.config.poll_interval_seconds
        keepalive = plan.tester_present_seconds
        while remaining > 0 and self._may_continue(entries, stop):
            step = remaining if keepalive is None else min(remaining, keepalive)
            await wait_for_stop_or_timeout(stop, step)
            remaining -= step
            if keepalive is not None and remaining > 0 and self._may_continue(entries, stop):
                await asyncio.to_thread(transport.tester_present)
                self.keepalives += 1

    async def _sniff(self, queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        """Listen until stopped or until a command session may start."""
        try:
            profile = load_kline_profile(self.profile_path)
            did_map = load_did_map(self.dids_path)
        except (KlineNotConfiguredError, ValueError) as exc:
            self._say_once("sniff-unconfigured", "K-line sniffing unavailable: %s", exc)
            await wait_for_stop_or_timeout(stop, IDLE_CHECK_SECONDS * 10)
            return
        ecm_widths = {e.did: e.response_length for e in did_map.dids if e.response_length}
        decoder = FrameDecoder()
        recorder = SniffRecorder(profile, ecm_widths)
        transport = self._factory(profile, Mode.SNIFF)
        await asyncio.to_thread(transport.open)
        # The reader owns the port on a thread of its own, so a busy event loop
        # or a saturated executor delays decoding, never reading. It used to
        # read in 50 ms bursts through asyncio.to_thread, and each ~350 ms gap
        # between bursts cost a frame (2026-09-24).
        chunks: SimpleQueue[RxChunk] = SimpleQueue()
        halt = threading.Event()
        failures: list[BaseException] = []

        def read() -> None:
            try:
                transport.listen(halt.is_set, chunks.put)
            except BaseException as exc:  # handed to the loop below
                failures.append(exc)

        reader = threading.Thread(target=read, name="kline-sniff", daemon=True)
        reader.start()
        log.info(
            "K-line sniffing (listening only, pull-up off)",
            extra={"tty": self.config.tty, "baud": profile.baud},
        )
        try:
            while (
                not stop.is_set() and self._wants_command() is None and not self._actions.pending()
            ):
                await wait_for_stop_or_timeout(stop, SNIFF_DRAIN_SECONDS)
                to_epoch = time.time() - time.monotonic()
                while True:
                    try:
                        chunk = chunks.get_nowait()
                    except Empty:
                        break
                    for frame in decoder.feed_chunk(chunk):
                        self.sniffed_frames += 1
                        if frame.direction == "tester":
                            self.foreign_frames += 1
                            hold = self.config.foreign_hold_seconds
                            MODULE_SESSIONS.claim(frame.module, "foreign", ttl_seconds=hold)
                            tx_gate.note_foreign_tester(
                                tester_address=frame.tester, module_address=frame.module
                            )
                            if frame.module in (None, profile.ecm_address):
                                tx_gate.claim_ecm_session("foreign", ttl_seconds=hold)
                        for record in recorder.observe(frame, frame.finished + to_epoch):
                            put_nowait_or_drop(queue, record, on_drop=self.drop_counter)
                if failures:
                    raise failures[0]
        finally:
            halt.set()
            await asyncio.to_thread(reader.join)
            await asyncio.to_thread(transport.close)


class SniffRecorder:
    """Another tester's traffic, frame by frame, as the rows it is stored as.

    Every frame becomes a ``kline:FRAME`` row stamped with its module's label;
    a reply paired with the request before it also becomes value rows
    (:func:`sniffed_value_records`), laid out by the definitions that module's
    own session made. Pure: the claims another tester's frames make are the
    collector's.
    """

    def __init__(self, profile: KlineProfile, ecm_widths: Mapping[int, int]) -> None:
        self.profile = profile
        self._ecm_widths = dict(ecm_widths)
        # Local ids belong to the module that defined them, and so do the DID
        # widths that lay out their replies: the ECM's catalog is the ECM's.
        self._trackers: dict[int | None, DefinitionTracker] = {}
        self._pending: SniffedFrame | None = None

    def observe(self, frame: SniffedFrame, stamp: float) -> list[TelemetryRecord]:
        label = self.profile.module_label(frame.module)
        out = [frame_record(frame, stamp, label)]
        if frame.direction == "tester":
            self._pending = frame
            return out
        if self._pending is None:
            return out
        exchange, self._pending = Exchange(self._pending, frame), None
        tracker = self._trackers.get(frame.module)
        if tracker is None:
            widths = module_did_widths(self.profile, frame.module, self._ecm_widths)
            tracker = self._trackers[frame.module] = DefinitionTracker(widths)
        out += sniffed_value_records(exchange, tracker, stamp, label)
        return out


def module_did_widths(
    profile: KlineProfile, address: int | None, ecm_widths: Mapping[int, int]
) -> dict[int, int]:
    """The DID reply widths that lay out ``address``'s local-id replies.

    The ECM's are its catalog's (``dids.yaml``) plus any its K-line profile
    entry records; another module's are only what its profile entry records;
    an unknown module (no StartCommunication seen) has none, so its ``21``
    replies stay raw frames rather than being laid out by a guess.
    """
    if address is None:
        return {}
    module = profile.modules.get(address)
    recorded = {d.did: d.width for d in module.dids} if module is not None else {}
    if address == profile.ecm_address:
        return {**ecm_widths, **recorded}
    return recorded


def frame_record(frame: SniffedFrame, stamp: float, module: str) -> TelemetryRecord:
    """``module`` is the profile's label for ``frame.module``
    (:meth:`~src.collection.kline.profile.KlineProfile.module_label`)."""
    metadata: dict[str, str | int | float | None] = {
        "instrument": "sniff",
        "direction": frame.direction,
        "kind": frame.kind,
        "timing_uncertainty_s": round(frame.timing_uncertainty_s, 6),
        "module": module,
    }
    if frame.module is not None:
        metadata["module_address"] = frame.module
    if frame.source is not None:
        metadata["source_address"] = frame.source
        metadata["target_address"] = frame.target
    return TelemetryRecord.from_epoch(
        stamp,
        source=KLINE_SOURCE,
        channel=FRAME_CHANNEL,
        value=frame.raw.hex(),
        payload=frame.raw,
        metadata=dict(metadata),
    )


def sniffed_value_records(
    exchange: Exchange, tracker: DefinitionTracker, stamp: float, module: str
) -> list[TelemetryRecord]:
    """Values another tester read: a tracked ``21`` set, or a ``22`` DID,
    named ``<module>:<item>`` for the module whose session it was."""
    request, reply = exchange.request, exchange.reply
    if reply is None:
        return []
    pairs: list[tuple[str, bytes, dict[str, str | int | float | None]]] = []
    if request.payload[:1] == bytes([kp.SID_READ_DATA_BY_COMMON_ID]):
        if reply.payload[:1] == bytes([kp.SID_READ_DATA_BY_COMMON_ID + kp.POSITIVE_OFFSET]):
            did = int.from_bytes(request.payload[1:3], "big")
            try:
                value = kp.split_did_reply(did, reply.payload[1:])
            except kp.KlineProtocolError:
                return []
            pairs.append((did_channel(module, did), value, {}))
    else:
        observed = tracker.observe(exchange)
        if observed is None or observed.unresolved is not None:
            return []
        for descriptor, value in observed.values:
            name = (
                did_channel(module, descriptor.did)
                if isinstance(descriptor, kp.DidDescriptor)
                else ram_channel(module, descriptor.address)
            )
            pairs.append((name, value, {"local_id": observed.local_id}))
    return [
        TelemetryRecord.from_epoch(
            stamp,
            source=KLINE_SOURCE,
            channel=name,
            value=value.hex(),
            payload=value,
            metadata={
                "instrument": "sniff",
                "timing_uncertainty_s": round(reply.timing_uncertainty_s, 6),
                "module": module,
                **extra,
            },
        )
        for name, value, extra in pairs
    ]


__all__ = [
    "LOCAL_ID",
    "KlineCollector",
    "SniffRecorder",
    "did_record",
    "frame_record",
    "module_did_widths",
    "plan_records",
    "sniffed_value_records",
]
