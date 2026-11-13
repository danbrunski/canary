"""The ECU backup's wire protocol: one read of the recipe's range (R29.13).

:class:`MemoryReader` is everything :class:`src.control.rom_backup.RomBackupJob`
puts on the bus. It sends only DiagnosticSessionControl (``0x10``) and
ReadMemoryByAddress (``0x23``), through
:meth:`src.collection.can_obd_client.CanObdClient.query_raw`, whose probe-only
allowlist cannot build a write. It holds no lifecycle: the job claims the ECM
session, opens and closes the bus, and decides (``midread_problem``) whether
the read may go on; the reader paces the requests, retries a block, renews the
claim every block, and counts what happened in :class:`ReadCounters`.

The reader also owns the frame tap: the ECM's own broadcasts (is it still
powered?) and frames on the request ids (another tester on CAN).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import can

from src.analysis.rom_extent import early_extent
from src.collection import tx_gate
from src.collection.can_obd_client import CanObdClient, RawReply
from src.collection.isotp import DEFAULT_REQUEST_ID, nrc_name
from src.control.rom_backup_recipe import ROM_BACKUP_FILE, BackupRecipe

log = logging.getLogger("src.control.rom_backup")

#: The ECM-session holder name, and the private-traffic owner.
HOLDER: tx_gate.EcmSessionHolder = "rom_backup"
#: DiagnosticSessionControl and ReadMemoryByAddress: the only two services the
#: reader requests.
SESSION_SERVICE = 0x10
MEMORY_READ_SERVICE = 0x23
#: The claim's TTL, renewed every block: a reader wedged in bus I/O cannot keep
#: the poll loop paused for longer than this.
CLAIM_TTL_SECONDS = 30.0


class ReadAbortedError(Exception):
    """End the read as failed, with a reason."""


class ReadCancelledError(Exception):
    """The operator or a daemon shutdown cancelled the read."""


@dataclass
class ReadCounters:
    """What the read put on the bus, reported in the job state and its summary."""

    #: Requests sent (a send error or a gate suppression is not one).
    requests: int = 0
    #: Attempts after the first, for a session step or a block.
    retries: int = 0
    #: Requests that got neither a reply nor a negative response in time.
    timeouts: int = 0
    #: Frames the bus refused (a full TX queue, bus-off).
    send_errors: int = 0
    #: Negative responses by code (``"0x31"``), in the order first seen.
    nrcs: dict[str, int] = field(default_factory=dict)


class MidreadCheck(Protocol):
    """Why the read must stop now, or ``None``; raises
    :class:`ReadCancelledError` on a cancel."""

    def __call__(self, *, check_vehicle: bool) -> str | None: ...


@dataclass(frozen=True)
class _Failure:
    """One failed request: what to report, and whether the ECM said nothing."""

    cause: str
    #: Sent, and neither a reply nor a negative response came back in time.
    no_answer: bool


class MemoryReader:
    """One read of ``recipe``'s range, on a bus the caller opened.

    ``midread_problem`` is the job's re-check (cancel, passive, another
    tester, capture, vehicle state); ``update`` publishes progress fields to
    the job state; ``cancel`` ends a pacing pause early.
    """

    def __init__(
        self,
        recipe: BackupRecipe,
        *,
        midread_problem: MidreadCheck,
        update: Callable[..., None],
        cancel: threading.Event,
        listen_seconds: float,
        recheck_seconds: float,
    ) -> None:
        self.recipe = recipe
        self.counters = ReadCounters()
        self._midread_problem = midread_problem
        self._update = update
        self._cancel = cancel
        self._listen_seconds = listen_seconds
        self._recheck_seconds = recheck_seconds
        #: A frame on one of these is another tester: this socket never hears
        #: its own transmissions.
        self.watched_ids = frozenset({recipe.request_id, DEFAULT_REQUEST_ID})
        self.ecm_ids = frozenset(recipe.ecm_broadcast_ids)
        #: The first request id another tester was heard on, or ``None``.
        self.foreign_can: int | None = None
        self.ecm_heard_at: float | None = None

    # -- the bus ------------------------------------------------------------

    def tap(self, message: can.Message) -> None:
        """Every frame the read's own bus receives: the ECM still broadcasting,
        or another tester on CAN?

        This socket never hears its own transmissions, and the daemon's poll
        loop sends nothing while the claim stands, so a frame on a request id
        is another tester.
        """
        arbitration_id = int(message.arbitration_id)
        if arbitration_id in self.ecm_ids:
            self.ecm_heard_at = time.monotonic()
        elif self.foreign_can is None and arbitration_id in self.watched_ids:
            self.foreign_can = arbitration_id

    def attach(self, bus: can.BusABC) -> CanObdClient:
        """Filter ``bus`` to the frames the read listens to, and the client on it."""
        recipe = self.recipe
        try:
            bus.set_filters(
                [
                    {"can_id": i, "can_mask": 0x7FF, "extended": False}
                    for i in sorted({recipe.response_id, *self.watched_ids, *self.ecm_ids})
                ]
            )
        except (NotImplementedError, can.CanError, OSError):
            log.debug("CAN filters unavailable on this interface; filtering in software")
        return CanObdClient(
            bus,
            request_id=recipe.request_id,
            response_id_min=recipe.response_id,
            response_id_max=recipe.response_id,
            timeout_seconds=recipe.pacing.request_timeout_seconds,
            on_frame=self.tap,
            parse_error_owner="rom-backup",
            tx_holder=HOLDER,
        )

    def listen(self, bus: can.BusABC) -> None:
        """Before the first request: is another tester already talking on CAN?"""
        deadline = time.monotonic() + self._listen_seconds
        while time.monotonic() < deadline and self.foreign_can is None:
            try:
                message = bus.recv(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            except (ValueError, IndexError):
                continue
            if message is not None:
                self.tap(message)

    def _send(self, client: CanObdClient, service: int, body: bytes) -> RawReply:
        counters = self.counters
        reply = client.query_raw(service, body, request_id=self.recipe.request_id)
        if not reply.sent and reply.send_error is not None:
            # The bus refused the frame (a full TX queue, bus-off): a failed
            # attempt, retried like a timeout and named as what it is.
            counters.send_errors += 1
            self._update(send_errors=counters.send_errors)
            return reply
        if not reply.sent:
            owner = tx_gate.ecm_session_owner()
            cause = (
                "passive mode is armed"
                if tx_gate.passive_armed()
                else f"the ECM session is held by {owner}"
            )
            raise ReadAbortedError(f"transmit suppressed by the gate: {cause}")
        counters.requests += 1
        if reply.negative_response_code is not None:
            code = f"0x{reply.negative_response_code:02X}"
            counters.nrcs[code] = counters.nrcs.get(code, 0) + 1
        elif not reply.positive:
            counters.timeouts += 1
        self._update(
            requests=counters.requests,
            timeouts=counters.timeouts,
            negative_responses=dict(counters.nrcs),
        )
        return reply

    def _retried(self) -> None:
        self.counters.retries += 1
        self._update(retries=self.counters.retries)

    # -- failures -----------------------------------------------------------

    @staticmethod
    def _cause(what: str, reply: RawReply, timeout: float) -> _Failure:
        if reply.send_error is not None:
            return _Failure(f"{what}: CAN send failed ({reply.send_error})", no_answer=False)
        if reply.negative_response_code is not None:
            code = reply.negative_response_code
            return _Failure(
                f"{what}: negative response 0x{code:02X} ({nrc_name(code)})", no_answer=False
            )
        return _Failure(f"{what}: no response within {timeout:g} s", no_answer=True)

    def _explain(self, detail: str, last: _Failure | None, timeout: float) -> str:
        """``detail``, with what the ECM's broadcasts say about a silent failure.

        Nothing heard on any of them for a whole request timeout is an ECM
        that is off, not one ignoring the request: at least one broadcasts
        faster than that.
        """
        if last is None or not last.no_answer or not self.ecm_ids:
            return detail
        names = ", ".join(f"0x{i:03X}" for i in sorted(self.ecm_ids))
        heard = self.ecm_heard_at
        if heard is None:
            return (
                f"{detail}; the ECM's broadcasts ({names}) were never heard during the read, so "
                "a key-off cannot be told apart from an ECM that stopped answering"
            )
        quiet = time.monotonic() - heard
        if quiet >= timeout:
            return (
                f"the ECM went silent (key off?): none of its broadcasts ({names}) heard for "
                f"{quiet:.1f} s; {detail}"
            )
        return (
            f"{detail}; the ECM was still broadcasting (heard {quiet:.2f} s before this "
            "failure), so it is powered but not answering"
        )

    # -- the session --------------------------------------------------------

    def _session_step(self, client: CanObdClient, sub: int) -> _Failure | None:
        reply = self._send(client, SESSION_SERVICE, bytes([sub]))
        if reply.positive:
            return None
        return self._cause(
            f"session 0x{sub:02X}", reply, self.recipe.pacing.request_timeout_seconds
        )

    def enter_session(self, client: CanObdClient, sub: int) -> None:
        """One ``enter_once`` sub-function, retried like a block."""
        recipe = self.recipe
        last: _Failure | None = None
        for attempt in range(recipe.pacing.retries_per_block + 1):
            if attempt:
                self._retried()
            problem = self._midread_problem(check_vehicle=False)
            if problem:
                raise ReadAbortedError(problem)
            last = self._session_step(client, sub)
            if last is None:
                return
        detail = f"could not enter the session: {last.cause if last else None}"
        raise ReadAbortedError(self._explain(detail, last, recipe.pacing.request_timeout_seconds))

    def exit_session(self, bus: can.BusABC) -> None:
        """Best effort, one try each: the claim is released whatever happens."""
        recipe = self.recipe
        if not recipe.session.exit:
            return
        client = CanObdClient(
            bus,
            request_id=recipe.request_id,
            response_id_min=recipe.response_id,
            response_id_max=recipe.response_id,
            timeout_seconds=recipe.pacing.request_timeout_seconds,
            parse_error_owner="rom-backup",
            tx_holder=HOLDER,
        )
        for sub in recipe.session.exit:
            try:
                reply = client.query_raw(
                    SESSION_SERVICE, bytes([sub]), request_id=recipe.request_id
                )
            except Exception:  # noqa: BLE001 - exit is best effort
                log.exception("ROM backup: session exit 0x%02X failed", sub)
                continue
            if not reply.positive:
                log.warning(
                    "ROM backup: session exit 0x%02X not acknowledged (sent=%s, nrc=%s, "
                    "send_error=%s)",
                    sub,
                    reply.sent,
                    reply.negative_response_code,
                    reply.send_error,
                )

    # -- the read -----------------------------------------------------------

    def _read_block(self, client: CanObdClient, address: int, length: int) -> bytes:
        recipe = self.recipe
        last: _Failure | None = None
        timeout = recipe.pacing.request_timeout_seconds
        for attempt in range(recipe.pacing.retries_per_block + 1):
            if attempt:
                self._retried()
                problem = self._midread_problem(check_vehicle=False)
                if problem:
                    raise ReadAbortedError(problem)
            last = None
            for sub in recipe.session.before_each_read:
                last = self._session_step(client, sub)
                if last is not None:
                    break
            if last is not None:
                continue
            body = address.to_bytes(4, "big") + length.to_bytes(2, "big")
            reply = self._send(client, MEMORY_READ_SERVICE, body)
            if reply.payload is not None and len(reply.payload) == length:
                return reply.payload
            if reply.payload is not None:
                last = _Failure(
                    f"read: {len(reply.payload)} bytes back for a {length}-byte read",
                    no_answer=False,
                )
            else:
                last = self._cause("read", reply, timeout)
        detail = (
            f"read at 0x{address:06X} ({length} bytes) failed after "
            f"{recipe.pacing.retries_per_block + 1} attempts; last: {last.cause if last else None}"
        )
        raise ReadAbortedError(self._explain(detail, last, timeout))

    def read_all(self, client: CanObdClient, started: float) -> bytearray:
        """Every block of the range, in address order; the image."""
        recipe = self.recipe
        blocks = recipe.read.blocks()
        image = bytearray(recipe.read.size)
        next_check = 0.0
        last_cycle = 0.0
        done_bytes = 0
        # The vector tables are read once their bytes are in (minutes before
        # the end): an image that points past the range stops the read there.
        extent_pending = recipe.extent is not None
        for index, (address, length) in enumerate(blocks):
            now = time.monotonic()
            problem = self._midread_problem(check_vehicle=now >= next_check)
            if now >= next_check:
                next_check = now + self._recheck_seconds
            if problem:
                raise ReadAbortedError(problem)
            tx_gate.claim_ecm_session(HOLDER, ttl_seconds=CLAIM_TTL_SECONDS)
            wait = last_cycle + recipe.pacing.min_cycle_seconds - time.monotonic()
            if wait > 0:
                self._cancel.wait(wait)  # a cancel or shutdown ends the pause
            last_cycle = time.monotonic()
            data = self._read_block(client, address, length)
            offset = address - recipe.read.start
            image[offset : offset + length] = data
            done_bytes += length
            done = index + 1
            if extent_pending and recipe.extent is not None:
                if done_bytes >= recipe.extent.tables_end:
                    extent_pending = False
                    _check_extent_early(image, recipe, done, len(blocks))
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed > 0 else 0.0
            self._update(
                blocks_done=done,
                bytes=done_bytes,
                elapsed_s=round(elapsed, 2),
                eta_s=round((len(blocks) - done) / rate, 1) if rate > 0 else None,
            )
        return image

    def reread(self, client: CanObdClient, image: bytearray) -> list[str]:
        """Re-read blocks spread across the image; the addresses that differ."""
        recipe = self.recipe
        blocks = recipe.read.blocks()
        count = min(recipe.verify.reread_blocks, len(blocks))
        if count == 1:
            picks = [0]
        else:
            picks = sorted({round(i * (len(blocks) - 1) / (count - 1)) for i in range(count)})
        mismatches = []
        for number, index in enumerate(picks, start=1):
            problem = self._midread_problem(check_vehicle=True)
            if problem:
                raise ReadAbortedError(problem)
            tx_gate.claim_ecm_session(HOLDER, ttl_seconds=CLAIM_TTL_SECONDS)
            address, length = blocks[index]
            data = self._read_block(client, address, length)
            offset = address - recipe.read.start
            if bytes(image[offset : offset + length]) != data:
                mismatches.append(f"0x{address:06X}")
            self._update(phase=f"verifying ({number}/{len(picks)})")
        return mismatches


def _check_extent_early(image: bytearray, recipe: BackupRecipe, done: int, total: int) -> None:
    """Abort when the vector tables already read point past the range.

    Offline: the bytes are the ones just read. A read of the rest could
    only produce an image known to be short, so it stops here and says
    what the recipe needs.
    """
    spec = recipe.extent
    if spec is None:
        return
    reading = early_extent(bytes(image[: spec.tables_end]), spec, image_size=len(image))
    if reading.verdict != "larger":
        return
    raise ReadAbortedError(
        f"stopped at block {done:,} of {total:,}: {reading.summary()}. The recipe reads "
        f"0x{recipe.read.start:06X}-0x{recipe.read.end:06X}; this ECM needs a "
        f"{ROM_BACKUP_FILE} in its own profile with the flash's real size. Nothing was stored"
    )


def counters_summary(counters: ReadCounters) -> dict[str, Any]:
    """The counters as the job's summary and ``read_meta`` carry them."""
    return {
        "requests": counters.requests,
        "retries": counters.retries,
        "timeouts": counters.timeouts,
        "send_errors": counters.send_errors,
        "negative_responses": dict(counters.nrcs),
    }


__all__ = [
    "CLAIM_TTL_SECONDS",
    "HOLDER",
    "MEMORY_READ_SERVICE",
    "SESSION_SERVICE",
    "MemoryReader",
    "MidreadCheck",
    "ReadAbortedError",
    "ReadCancelledError",
    "ReadCounters",
    "counters_summary",
]
