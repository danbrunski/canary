"""Read-only diagnostic session for the UpRev/Cipher K-line interface.

The startup sequence is intentionally literal: every request was captured from
UpRev ROM Editor and independently reproduced against the development vehicle
on 2026-09-20.  The apparent no-op/negative probes are currently required to
reach the state in which ordinary ``0x22`` reads succeed, so removing one is an
experiment rather than a cleanup.

**This sequence is the development vehicle's, not a platform one.** It was
captured on the 2008 Nissan Xterra's ECM behind UpRev's Cipher cable: the
7E0/7E8 route, the 0x85 and 0xC0 sessions, the calibration/identity reads and
the 0x1100-0x1300 support groups are that ECM's. Another vehicle needs its own
capture, which belongs in its vehicle profile (``data/<vehicle>/``) rather
than as an edit here. :data:`CAPTURED_ON` names where it was proven.

Only diagnostic-session and read operations live here.  There are no ECU
memory-write, download, transfer, erase, or reflash payloads.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.collection.uprev_protocol import STREAM_DATA_PREFIX
from src.collection.uprev_usb import UpRevUsbError, UpRevUsbTransport

#: The one vehicle this literal startup sequence is proven on (see the module
#: docstring): Xterra-specific, not a platform default.
CAPTURED_ON = "2008 Nissan Xterra (development vehicle), UpRev Cipher K-line, 2026-09-20"

_CABLE_INFO = bytes.fromhex("08")
_CABLE_SYNC = bytes.fromhex("06")
_CABLE_CONFIG = bytes.fromhex("32")
_CABLE_RESET = bytes.fromhex("04")
_ECU_ROUTE = bytes.fromhex("0307e007e8")
_STOP = bytes.fromhex("0110")

_SESSION_FB = bytes.fromhex("020010fb")
_SESSION_85 = bytes.fromhex("02001085")
_SESSION_C0 = bytes.fromhex("020010c0")

_SUPPORT_GROUPS = (0x1100, 0x1120, 0x1140, 0x1160, 0x1200, 0x1220, 0x1240, 0x1300)


class UpRevSessionError(UpRevUsbError):
    """The cable answered, but its diagnostic session was not usable."""


class UpRevNegativeResponseError(UpRevSessionError):
    """A K-line request received a negative diagnostic response."""

    def __init__(self, service: int, code: int, payload: bytes) -> None:
        self.service = service
        self.code = code
        self.payload = payload
        super().__init__(f"negative response to 0x{service:02x}: NRC 0x{code:02x}")

    def __reduce__(self) -> tuple[object, ...]:
        # Rebuilt from its constructor's arguments, so it survives a process
        # boundary (pickle); tests/test_exceptions_pickle.py holds every class to it.
        return (type(self), (self.service, self.code, self.payload), self.__dict__)


@dataclass(frozen=True)
class UpRevBootstrapResult:
    """Identity/support evidence retained from one successful bootstrap."""

    cable_info: bytes
    cable_config: bytes
    support_blocks: dict[int, bytes]
    a101_identity: bytes
    a103_identity: bytes
    stop_partial_replies: int
    identity_failures: tuple[str, ...]


def build_did_request(identifier: int) -> bytes:
    """Build the captured checksummed single-DID read request."""
    if not 0 <= identifier <= 0xFFFF:
        raise ValueError(f"DID out of range: 0x{identifier:x}")
    return bytes.fromhex("020022") + identifier.to_bytes(2, "big") + bytes.fromhex("0401")


def supported_dids(support_blocks: dict[int, bytes]) -> tuple[int, ...]:
    """Expand the capture-proven 32-bit support masks into concrete DIDs.

    As in SAE support bitmaps, bit 32 names the next support-group boundary,
    not a data item.  It is excluded even when set.
    """
    identifiers: list[int] = []
    for base, mask in sorted(support_blocks.items()):
        if len(mask) != 4:
            raise UpRevSessionError(f"support block 0x{base:04x} has {len(mask)} bytes, expected 4")
        bits = int.from_bytes(mask, "big")
        identifiers.extend(base + offset for offset in range(1, 32) if bits & (1 << (32 - offset)))
    return tuple(identifiers)


def _negative_response(payload: bytes) -> UpRevNegativeResponseError | None:
    if len(payload) >= 5 and payload[:3] == bytes.fromhex("66037f"):
        return UpRevNegativeResponseError(payload[3], payload[4], payload)
    return None


def _is_stale_reply(payload: bytes, identifier: int) -> bool:
    """Whether ``payload`` is a complete reply to some *earlier* request.

    The cable can leave a finished stream reply or a previous DID reply queued
    ahead of the reply just requested; the hardware reproduced this on
    2026-09-20 after a bulk-write timeout. Those two shapes are recognisable —
    a stream reply carries the ``61 81`` data prefix, and a value read echoes
    ``62 <identifier>`` — so they can be drained. Anything else, malformed
    replies included, belongs to this request and must still surface.
    """
    if len(payload) < 5 or payload[0] != 0x66:
        return False
    if payload[2:4] == STREAM_DATA_PREFIX:
        return True
    return payload[2] == 0x62 and payload[3:5] != identifier.to_bytes(2, "big")


def parse_did_response(payload: bytes, identifier: int, *, checksummed: bool = True) -> bytes:
    """Validate a captured-style ``0x22`` reply and return raw DID bytes.

    ROM Editor's support-group requests omit the trailing ``04 01`` used by
    ordinary value reads; their replies consequently omit the inner checksum.
    The caller must state which captured request shape it sent rather than
    making a response-length heuristic silently accept either one.
    """
    negative = _negative_response(payload)
    if negative is not None:
        raise negative
    if len(payload) < 7 or payload[0] != 0x66:
        raise UpRevSessionError(f"not an UpRev diagnostic response: {payload.hex()}")
    inner_length = payload[1]
    overhead = 3 if checksummed else 2
    if len(payload) != inner_length + overhead:
        raise UpRevSessionError(
            f"DID response length mismatch: declared {inner_length}, got {len(payload)} bytes"
        )
    expected_prefix = b"\x62" + identifier.to_bytes(2, "big")
    if payload[2:5] != expected_prefix:
        raise UpRevSessionError(
            f"DID response identifier mismatch: expected {expected_prefix.hex()}, "
            f"got {payload[2:5].hex()}"
        )
    if checksummed:
        checksum = sum(payload[1:-1]) & 0xFF
        if checksum != payload[-1]:
            raise UpRevSessionError(
                f"DID response checksum mismatch: calculated 0x{checksum:02x}, "
                f"received 0x{payload[-1]:02x}"
            )
        return payload[5:-1]
    return payload[5:]


class UpRevDiagnosticSession:
    """Maintain the captured read-only K-line state on one open transport."""

    def __init__(self, transport: UpRevUsbTransport) -> None:
        self.transport = transport
        self.bootstrap_result: UpRevBootstrapResult | None = None
        self.stop_partial_replies = 0

    def _exchange(self, payload: bytes, timeout_seconds: float = 3.0) -> bytes:
        return self.transport.exchange(payload, timeout_seconds=timeout_seconds).payload

    def _expect(self, payload: bytes, expected: bytes, *, label: str) -> None:
        actual = self._exchange(payload)
        if actual != expected:
            raise UpRevSessionError(f"{label} returned {actual.hex()}, expected {expected.hex()}")

    def _stop(self) -> None:
        """Run the captured stop/lapse operation and consume both replies."""
        self.transport.send(_STOP)
        replies: list[bytes] = []
        # Both replies shared one capture timestamp, but the live cable has
        # produced either, both, or neither during road tests.  The identity
        # reads after each stop are the actual usability gate; missing cable
        # status here is recorded rather than confused with a failed ECU read.
        for _ in range(2):
            try:
                replies.append(self.transport.receive(timeout_seconds=2.5).payload)
            except TimeoutError:
                break
        expected = {b"\x06", bytes.fromhex("7f01")}
        unexpected = [reply for reply in replies if reply not in expected]
        if unexpected:
            raise UpRevSessionError(
                f"stop returned {[reply.hex() for reply in replies]}; expected 06/7f01"
            )
        if set(replies) != expected:
            self.stop_partial_replies += 1
        # Every captured stop transition left roughly 0.5 s after the terminal
        # reply before the next command.  The A101/A103 identity reads become
        # intermittent without this settle interval, especially while moving.
        time.sleep(0.5)

    def _expect_prefix(self, payload: bytes, prefix: bytes, *, label: str) -> bytes:
        actual = self._exchange(payload)
        if not actual.startswith(prefix):
            raise UpRevSessionError(
                f"{label} returned {actual.hex()}, expected prefix {prefix.hex()}"
            )
        return actual

    def _expect_prefix_with_retry(
        self,
        payload: bytes,
        prefix: bytes,
        *,
        label: str,
        attempts: int = 3,
    ) -> bytes:
        """Retry a read when the cable reports its transient ``7f04`` state."""
        last: bytes | None = None
        for attempt in range(attempts):
            self.transport.send(payload)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    last = self.transport.receive(
                        timeout_seconds=max(0.001, deadline - time.monotonic())
                    ).payload
                except TimeoutError:
                    last = None
                    break
                if last.startswith(prefix):
                    return last
                # A stop acknowledgement/terminal status can arrive after the
                # bounded drain in _stop.  It is asynchronous cable state, not
                # the response to the identity read just sent.
                if last in (b"\x06", bytes.fromhex("7f01")):
                    continue
                break
            if last not in (None, bytes.fromhex("7f04")):
                break
            if attempt + 1 < attempts:
                time.sleep(0.5)
        rendered = "timeout" if last is None else last.hex()
        raise UpRevSessionError(
            f"{label} returned {rendered} after {attempts} attempts, expected prefix {prefix.hex()}"
        )

    def _cold_link_start(self, cable_info: bytes) -> None:
        """Run ROM Editor's captured cold-start link negotiation.

        A shorter route/session sequence works only while state from a prior
        open survives in the cable.  Replaying this prefix makes reconnects
        self-contained; the two identity reads are known-answer gates before
        the later capability bootstrap.
        """
        self._expect(_ECU_ROUTE, b"\x06", label="initial ECU route")
        self._expect(_SESSION_85, bytes.fromhex("66025085"), label="85 session")
        self._stop()

        self._expect(_CABLE_SYNC, b"\x06", label="cold-start sync")
        self._exchange(_CABLE_CONFIG)
        if self._exchange(_CABLE_INFO) != cable_info:
            raise UpRevSessionError("cable identity changed during cold-start negotiation")
        self._expect(_CABLE_RESET, b"\x06", label="cold-start reset")
        self._expect(_ECU_ROUTE, b"\x06", label="cold-start ECU route")
        self._expect(bytes.fromhex("8a"), b"\x06", label="link command")
        self._expect(
            bytes.fromhex("8a07420400000000000000"),
            b"\x06",
            label="link configuration",
        )
        # The capture returned 06 02 50 c0 ... after the link-start delay; a
        # live reconnect has also returned cable status 7f04.  Both are cable
        # state, and the known calibration/identity reads below decide whether
        # the actual diagnostic link is usable.
        self._exchange(bytes.fromhex("8907426600000000000000"), timeout_seconds=4.0)

        self._expect(_CABLE_RESET, b"\x06", label="post-link reset")
        self._expect(_ECU_ROUTE, b"\x06", label="post-link ECU route")
        self._expect(_SESSION_C0, bytes.fromhex("660250c0"), label="post-link C0 session")
        self._expect_prefix(bytes.fromhex("02002110"), bytes.fromhex("660d6110"), label="cal ID")
        self._expect_prefix(
            bytes.fromhex("02002181"), bytes.fromhex("66156181"), label="ECU identity"
        )
        self._expect(_CABLE_RESET, b"\x06", label="post-identity reset")

        self._expect(_CABLE_SYNC, b"\x06", label="second sync")
        self._exchange(_CABLE_CONFIG)
        if self._exchange(_CABLE_INFO) != cable_info:
            raise UpRevSessionError("cable identity changed during second negotiation")
        self._expect(_ECU_ROUTE, b"\x06", label="second ECU route")
        self._expect(_SESSION_85, bytes.fromhex("66025085"), label="second 85 session")
        self._expect(_CABLE_RESET, b"\x06", label="second reset")
        self._expect(_ECU_ROUTE, b"\x06", label="second post-reset route")
        self._expect(_SESSION_C0, bytes.fromhex("660250c0"), label="second C0 session")
        self._expect_prefix(
            bytes.fromhex("02002110"), bytes.fromhex("660d6110"), label="second cal ID"
        )
        self._expect_prefix(
            bytes.fromhex("02002181"), bytes.fromhex("66156181"), label="second ECU identity"
        )
        self._expect(_CABLE_RESET, b"\x06", label="capability reset")

    def bootstrap(self) -> UpRevBootstrapResult:
        """Reproduce the capture-proven sequence that enables ``0x22`` reads."""
        try:
            self.transport.cable_ping()
        except (TimeoutError, UpRevUsbError):
            # Failed/aborted experiments can leave the cable MCU in a command
            # state where ping reports 7f04 or a stale dynamic-stream reply.
            # The captured local reset restores ping even when reset itself
            # reports 7f03.
            try:
                self._exchange(_CABLE_RESET)
            except TimeoutError:
                pass
            self.transport.purge_rx()
            self.transport.cable_ping()
        cable_info = self._exchange(_CABLE_INFO)
        self._expect(_CABLE_SYNC, b"\x06", label="cable sync")
        cable_config = self._exchange(_CABLE_CONFIG)
        repeated_info = self._exchange(_CABLE_INFO)
        if repeated_info != cable_info:
            raise UpRevSessionError("cable identity changed during bootstrap")
        self._cold_link_start(cable_info)
        self._expect(_ECU_ROUTE, b"\x06", label="capability ECU route")

        self._expect(_SESSION_FB, bytes.fromhex("660250fb"), label="FB session")
        self._exchange(bytes.fromhex("020023a100"))
        self._exchange(bytes.fromhex("020023a200"))
        self._exchange(bytes.fromhex("0200a100"))
        self._exchange(bytes.fromhex("0200a200"))

        support_blocks: dict[int, bytes] = {}
        for base in _SUPPORT_GROUPS:
            self._expect(_SESSION_C0, bytes.fromhex("660250c0"), label="C0 session")
            payload = self._exchange(bytes.fromhex("020022") + base.to_bytes(2, "big"))
            support_blocks[base] = parse_did_response(payload, base, checksummed=False)

        self._expect(_CABLE_RESET, b"\x06", label="cable reset")
        self._stop()
        self._exchange(bytes.fromhex("0200a100"))
        self._exchange(bytes.fromhex("0200a200"))
        self._stop()
        identity_failures: list[str] = []
        try:
            a101_identity = self._expect_prefix_with_retry(
                bytes.fromhex("0200a10100000000"),
                bytes.fromhex("6621e1"),
                label="A101 identity",
            )
        except UpRevSessionError as exc:
            a101_identity = b""
            identity_failures.append(str(exc))
        self._stop()
        try:
            a103_identity = self._expect_prefix_with_retry(
                bytes.fromhex("0200a10300"), bytes.fromhex("6621e1"), label="A103 identity"
            )
        except UpRevSessionError as exc:
            a103_identity = b""
            identity_failures.append(str(exc))

        # Reproduce a known answer before trusting any novel DID.  The value
        # varies, but this request was observed continuously in both captures.
        self.read_did(0x1103)

        result = UpRevBootstrapResult(
            cable_info=cable_info,
            cable_config=cable_config,
            support_blocks=support_blocks,
            a101_identity=a101_identity,
            a103_identity=a103_identity,
            stop_partial_replies=self.stop_partial_replies,
            identity_failures=tuple(identity_failures),
        )
        self.bootstrap_result = result
        return result

    def read_did(self, identifier: int) -> bytes:
        """Read one DID through the capture-proven K-line request shape."""
        request = build_did_request(identifier)
        last: bytes | None = None
        for attempt in range(3):
            self.transport.send(request)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    last = self.transport.receive(
                        timeout_seconds=max(0.001, deadline - time.monotonic())
                    ).payload
                except TimeoutError:
                    last = None
                    break
                if last in (b"\x06", bytes.fromhex("7f01")):
                    continue
                if last == bytes.fromhex("7f04"):
                    break
                if _is_stale_reply(last, identifier):
                    continue
                return parse_did_response(last, identifier)
            if attempt < 2:
                time.sleep(0.05)
        rendered = "timeout" if last is None else last.hex()
        raise UpRevSessionError(f"DID 0x{identifier:04x} returned {rendered} after 3 attempts")
