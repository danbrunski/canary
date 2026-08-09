"""Mode 22 DID scanner — which DIDs answer, and which carry runtime-varying
data vs. fixed calibration constants (Task 4.E.5).

The transport is just "send ReadDataByIdentifier for a 16-bit DID, get the data
bytes back"; the response parsing and the constant-vs-varying classification —
the bits worth getting right — are transport-agnostic and unit-tested. A serial
ELM327 implementation is provided (`Elm327Mode22Transport`).

READ-ONLY: only UDS service 0x22 (ReadDataByIdentifier) is ever sent. No 0x2E
(WriteDataByIdentifier), no 0x2F (InputOutputControlByIdentifier), no security
access — and the Canable, if attached, must be in its sniffing role only.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

# Pure protocol helpers — stdlib-only, no python-can, so importing them at
# runtime does not couple this analysis module to the CAN stack (which is why
# CanObdClient below stays behind TYPE_CHECKING and duck-typed).
from src.collection.isotp import MAX_BATCHED_DIDS, split_batched_did_response

if TYPE_CHECKING:
    from src.collection.can_obd_client import CanObdClient


class Mode22Transport(Protocol):
    """Reads one DID at a time over some link to the ECU."""

    def read_did(self, did: int) -> bytes | None:
        """Return the data bytes after the ``62 HH LL`` echo, or ``None`` if the
        DID didn't answer positively (negative response, timeout, garbage)."""
        ...


def parse_mode22_response(did: int, raw: bytes) -> bytes | None:
    """Pull the data payload out of a raw Mode 22 response for ``did``.

    * positive ``62 <DID_hi> <DID_lo> <data...>`` → returns ``<data...>``
    * negative ``7F 22 <NRC>`` → ``None``
    * anything shorter / mismatched / not ``0x62`` → ``None``
    """
    if len(raw) < 3 or raw[0] != 0x62:
        return None
    if raw[1] != (did >> 8) & 0xFF or raw[2] != did & 0xFF:
        return None
    return bytes(raw[3:])


@dataclass(frozen=True)
class DidSnapshot:
    """One sweep: DID → raw data bytes, for the DIDs that answered positively."""

    values: Mapping[int, bytes]
    captured_at: float  # time.time()
    label: str = ""

    @property
    def dids(self) -> frozenset[int]:
        return frozenset(self.values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "captured_at": self.captured_at,
            "values": {f"0x{did:04X}": data.hex() for did, data in sorted(self.values.items())},
        }


def sweep_dids(
    transport: Mode22Transport,
    dids: Iterable[int],
    *,
    delay_seconds: float = 0.0,
    label: str = "",
    on_progress: Callable[[int, bytes | None], None] | None = None,
) -> DidSnapshot:
    """Read every DID in ``dids``; keep the ones that answered positively."""
    values: dict[int, bytes] = {}
    for did in dids:
        data = transport.read_did(did)
        if on_progress is not None:
            on_progress(did, data)
        if data is not None:
            values[did] = data
        if delay_seconds > 0:
            time.sleep(delay_seconds)
    return DidSnapshot(values=dict(values), captured_at=time.time(), label=label)


def sweep_dids_batched(
    transport: Mode22Transport,
    dids: Iterable[int],
    byte_length_for: Callable[[int], int | None],
    *,
    batch_size: int = MAX_BATCHED_DIDS,
    delay_seconds: float = 0.0,
    label: str = "",
    on_progress: Callable[[int, bytes | None], None] | None = None,
) -> DidSnapshot:
    """Read ``dids`` using same-instant batched requests where possible.

    Only DIDs with a KNOWN width can be batched — a multi-DID response is not
    self-delimiting, so an unknown width makes the rest of that response
    unsplittable. Those fall back to individual reads, which is why this is
    safe to point at a mixed list: nothing is silently misattributed, it just
    loses the same-instant guarantee for the unknown ones.

    ``transport`` needs a ``read_dids`` method to batch at all; a plain
    :class:`Mode22Transport` degrades to single reads throughout.
    """
    batch_size = max(1, min(batch_size, MAX_BATCHED_DIDS))
    read_dids = getattr(transport, "read_dids", None)
    values: dict[int, bytes] = {}

    def _single(did: int) -> None:
        data = transport.read_did(did)
        if on_progress is not None:
            on_progress(did, data)
        if data is not None:
            values[did] = data
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    requested = list(dids)
    batchable = [d for d in requested if (byte_length_for(d) or 0) > 0]
    unknown = [d for d in requested if (byte_length_for(d) or 0) <= 0]

    if read_dids is None or batch_size == 1:
        for did in requested:
            _single(did)
        return DidSnapshot(values=dict(values), captured_at=time.time(), label=label)

    for start in range(0, len(batchable), batch_size):
        group = batchable[start : start + batch_size]
        readings, clean = read_dids(group, byte_length_for)
        for did in group:
            data = readings.get(did)
            if data is None:
                # Either the ECU omitted it or the walk desynced before
                # reaching it — re-read individually rather than guess.
                _single(did)
                continue
            values[did] = data
            if on_progress is not None:
                on_progress(did, data)
        del clean  # every gap is already re-read individually above
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    for did in unknown:
        _single(did)

    return DidSnapshot(values=dict(values), captured_at=time.time(), label=label)


@dataclass(frozen=True)
class DidClassification:
    """Comparing ≥2 snapshots: which DIDs vary, which are constant, which flicker."""

    varying: frozenset[int]  # answered in every snapshot AND the data changed
    constant: frozenset[int]  # answered in every snapshot AND never changed
    intermittent: frozenset[int]  # answered in some snapshots but not all

    @property
    def all_seen(self) -> frozenset[int]:
        return self.varying | self.constant | self.intermittent


def classify_dids(snapshots: Sequence[DidSnapshot]) -> DidClassification:
    """Split DIDs into varying / constant / intermittent across the snapshots.

    Run at least two snapshots at *different* engine states (e.g. one at idle,
    one while revving / moving) so "varying" actually means "tracks something."
    """
    if len(snapshots) < 2:
        raise ValueError("need at least 2 snapshots to classify constant vs varying")
    all_dids: set[int] = set()
    for snapshot in snapshots:
        all_dids |= set(snapshot.dids)
    varying: set[int] = set()
    constant: set[int] = set()
    intermittent: set[int] = set()
    for did in all_dids:
        seen = [snapshot.values[did] for snapshot in snapshots if did in snapshot.values]
        if len(seen) < len(snapshots):
            intermittent.add(did)
        elif len(set(seen)) > 1:
            varying.add(did)
        else:
            constant.add(did)
    return DidClassification(frozenset(varying), frozenset(constant), frozenset(intermittent))


# ---------------------------------------------------------------------------
# ELM327 serial transport
# ---------------------------------------------------------------------------

_FRAME_MARKER_PREFIXES = tuple(f"{i}:" for i in range(16))  # "0:", "1:", ... ISO-TP multi-frame
_ERROR_TOKENS = ("NO DATA", "STOPPED", "BUFFER FULL", "CAN ERROR", "BUS", "UNABLE", "?", "ERR")


def _extract_hex_bytes(text: str) -> bytes:
    """Pull the response bytes out of an ELM327 reply.

    Handles single-line (``62 F1 90 ...``), the multi-frame ``0: .. / 1: ..``
    layout (drops the leading length-byte line and the ``N:`` markers), the
    echoed prompt, and stray whitespace. Returns ``b""`` on error tokens.
    """
    cleaned = text.replace("\r", "\n").replace(">", "")
    if any(tok in cleaned.upper() for tok in _ERROR_TOKENS):
        return b""
    out = bytearray()
    for line in cleaned.split("\n"):
        token = line.strip()
        if not token:
            continue
        if token[:2] in _FRAME_MARKER_PREFIXES:
            token = token[2:].strip()  # strip the "N:" frame marker
        # a bare length byte line (3 hex digits, e.g. "014") precedes multi-frame
        # data — but it could also legitimately be 1.5 bytes; treat <2-char and
        # 3-char no-space tokens as length headers and skip them.
        compact = token.replace(" ", "")
        if len(compact) in (1, 3) and all(c in "0123456789ABCDEFabcdef" for c in compact):
            continue
        for pair in token.split():
            if len(pair) == 2 and all(c in "0123456789ABCDEFabcdef" for c in pair):
                out.append(int(pair, 16))
            elif len(pair) % 2 == 0 and all(c in "0123456789ABCDEFabcdef" for c in pair):
                out.extend(bytes.fromhex(pair))
    return bytes(out)


class Elm327Mode22Transport:
    """``Mode22Transport`` over an open pyserial-style port talking to an ELM327.

    On construction: disables echo/linefeeds, sets the protocol to ISO 15765-4
    CAN 11/500 (protocol 6), and points the request header at the engine ECU
    (7E0). It never sends anything but ``AT*`` setup commands and ``22 ....``
    reads. ``serial_port`` only needs ``write(bytes)`` and ``read(n) -> bytes``.
    """

    def __init__(
        self,
        serial_port: Any,
        *,
        header: str = "7E0",
        read_timeout_seconds: float = 1.0,
        sleep_after_open: float = 0.3,
    ) -> None:
        self._ser = serial_port
        self._read_timeout = read_timeout_seconds
        time.sleep(sleep_after_open)
        self._drain()
        for setup in ("ATZ", "ATE0", "ATL0", "ATS0", "ATSP6", "ATCAF1", f"ATSH{header}"):
            self._command(setup, timeout=2.0)

    def read_did(self, did: int) -> bytes | None:
        raw = _extract_hex_bytes(self._command(f"22{did:04X}"))
        return parse_mode22_response(did, raw)

    def _drain(self) -> None:
        try:
            self._ser.read(4096)
        except Exception:  # noqa: BLE001 — best-effort flush
            pass

    def _command(self, cmd: str, *, timeout: float | None = None) -> str:
        self._ser.write((cmd + "\r").encode("ascii"))
        deadline = time.time() + (self._read_timeout if timeout is None else timeout)
        buf = bytearray()
        while time.time() < deadline:
            chunk = self._ser.read(256)
            if chunk:
                buf.extend(chunk)
                if b">" in buf:
                    break
            else:
                time.sleep(0.01)
        return bytes(buf).decode("ascii", errors="replace")


# ---------------------------------------------------------------------------
# python-can transport (CanObdClient-backed)
# ---------------------------------------------------------------------------


class CanableMode22Transport:
    """``Mode22Transport`` backed by ``CanObdClient`` over a python-can bus.

    The bus-native sibling of ``Elm327Mode22Transport``: it issues UDS
    service 0x22 reads through ``CanObdClient.query_mode_22`` on the same
    CAN interface used for broadcast sniffing, so no separate serial
    adapter is needed. Mode 22 is physically addressed, so ``request_id``
    (default 0x7E0 = engine ECU) is passed through on every read, letting
    one client be retargeted at different modules (0x7E1 trans, 0x7E2 ABS,
    …). ``query_mode_22`` already returns the bytes *after* the
    ``62 HH LL`` echo as ``.payload`` (and ``None`` for a negative ``7F``
    response or a timeout), which is exactly the ``Mode22Transport``
    contract — so the bridge is a one-liner.

    Manufacturer DID gating: some ECMs refuse ``22`` outside a non-default
    diagnostic session (the 2008 Nissan ECM gates it behind sub-function
    ``0xC0``). Pass ``session=0xC0`` and this transport enters it before
    reads and re-enters it every ``keepalive_seconds`` (the session lapses
    after a few seconds and TesterPresent is itself rejected on that ECM,
    so periodic ``10 <sf>`` is the keep-alive). With ``session=None`` it
    behaves as a plain pass-through (ECUs that don't gate 0x22).

    Read-only by construction: only ``query_mode_22`` (0x22) and, when a
    session is requested, ``enter_diagnostic_session`` (0x10 session
    control — no memory write, no actuation) are invoked. ``client`` is
    duck-typed so this analysis module stays import-decoupled from the
    collection layer at runtime.
    """

    def __init__(
        self,
        client: CanObdClient,
        *,
        request_id: int = 0x7E0,
        session: int | None = None,
        keepalive_seconds: float = 2.0,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._request_id = request_id
        self._session = session
        self._keepalive = keepalive_seconds
        self._time_fn = time_fn
        self._last_primed: float | None = None

    def _ensure_session(self) -> None:
        if self._session is None:
            return
        now = self._time_fn()
        if self._last_primed is not None and now - self._last_primed < self._keepalive:
            return
        # Best-effort: a failed re-prime just means the next read returns
        # None (negative response) and we retry the prime on the read after.
        if self._client.enter_diagnostic_session(self._session, request_id=self._request_id):
            self._last_primed = now

    def read_did(self, did: int) -> bytes | None:
        self._ensure_session()
        response = self._client.query_mode_22(did, request_id=self._request_id)
        return None if response is None else response.payload

    def read_dids(
        self, dids: Sequence[int], byte_length_for: Callable[[int], int | None]
    ) -> tuple[dict[int, bytes], bool]:
        """Read up to ``MAX_BATCHED_DIDS`` DIDs in one same-instant request.

        Returns ``(readings, clean)`` — ``clean`` is False if the response
        could not be walked to the end, which means a width was unknown or
        the payload was short. Callers must fall back to single-DID reads for
        anything missing rather than treat a partial walk as complete.
        """
        self._ensure_session()
        response = self._client.query_mode_22_batch(dids, request_id=self._request_id)
        if response is None:
            return {}, False
        return split_batched_did_response(response.payload, byte_length_for)
