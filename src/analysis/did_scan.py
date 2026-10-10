"""Mode 22 DID scanner — which DIDs answer, and which carry runtime-varying
data vs. fixed calibration constants (Task 4.E.5).

The transport is just "send ReadDataByIdentifier for a 16-bit DID, get the data
bytes back"; the response parsing and the constant-vs-varying classification —
the bits worth getting right — are transport-agnostic and unit-tested. A serial
ELM327 implementation is provided (`Elm327Mode22Transport`).

This scanner sends only UDS service 0x22 (ReadDataByIdentifier). It does not
invoke 0x2E (WriteDataByIdentifier), 0x2F
(InputOutputControlByIdentifier), or security access. That is the scanner's
scope, not a restriction on CANary's general diagnostic request path.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

# Pure protocol helpers — stdlib-only, no python-can, so importing them at
# runtime does not couple this analysis module to the CAN stack (which is why
# CanObdClient below stays behind TYPE_CHECKING and duck-typed).
from src.collection.isotp import MAX_BATCHED_DIDS, nrc_name, split_batched_did_response
from src.decode.dids import without_side_effect_dids

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


class BatchedMode22Transport(Mode22Transport, Protocol):
    """A transport that can also read several DIDs in ONE request.

    Declared so the batched contract is a type, not a convention: ``read_dids``
    must report a refusal (:class:`BatchRead.rejected`) rather than return an
    empty reading, because "the ECU refused every batch" and "the ECU batches
    fine" are otherwise indistinguishable in the result.
    """

    def read_dids(
        self, dids: Sequence[int], byte_length_for: Callable[[int], int | None]
    ) -> BatchRead: ...


@dataclass(frozen=True)
class BatchRead:
    """One multi-DID request's outcome, as a transport's ``read_dids`` reports it.

    ``rejected`` separates "the ECU refused the whole request" (``7F 22 <NRC>``,
    or nothing answered) from "the ECU answered but left a DID out". Both leave
    DIDs missing from ``readings``, and both get re-read individually — but only
    the first says anything about whether this ECU batches at all. Folding them
    together is how a sweep in which every batch was refused came back looking
    exactly like a batched one.
    """

    readings: dict[int, bytes]
    #: False when the payload walk stopped early (unknown width, truncation).
    clean: bool
    #: The whole request was refused (NRC) or went unanswered.
    rejected: bool = False
    #: The NRC byte when ``rejected`` by a negative response; None on silence.
    negative_response_code: int | None = None


@dataclass(frozen=True)
class DidSnapshot:
    """One sweep: DID → raw data bytes, for the DIDs that answered positively.

    The ``batch_*`` fields say how the values were actually read. They are
    ``None`` only on snapshots that predate them (not recorded), never on a
    sweep this module produced:

    * ``batch_requested`` — DIDs per request asked for (after the frame cap).
    * ``batch_effective`` — DIDs per request the sweep ended up using: equal to
      ``batch_requested`` when batching held, 1 when it was abandoned (the ECU
      rejected a batch whose DIDs then answered singly), the transport could
      not batch, or no DID had a known width.
    * ``batch_rejections`` — batched requests refused outright (NRC/silence).
    * ``batch_nrc`` — the NRC of the most recent rejection, if it had one.
    * ``batched_reads`` — values that came from an accepted batched response
      (the ones that actually share a sample instant).

    ``skipped`` holds the DIDs in range that were never requested, with why:
    a catalogued read that changes ECU state (``DidEntry.side_effect``). A
    skipped DID is absent from ``values`` for that reason, not for silence.
    """

    values: Mapping[int, bytes]
    captured_at: float  # time.time()
    label: str = ""
    batch_requested: int | None = None
    batch_effective: int | None = None
    batch_rejections: int | None = None
    batch_nrc: int | None = None
    batched_reads: int | None = None
    skipped: Mapping[int, str] = field(default_factory=dict)

    @property
    def dids(self) -> frozenset[int]:
        return frozenset(self.values)

    @property
    def batch_unsupported(self) -> bool:
        """Batching was asked for and abandoned because the ECU refused it."""
        return (
            self.batch_requested is not None
            and self.batch_effective is not None
            and self.batch_effective < self.batch_requested
            and bool(self.batch_rejections)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "captured_at": self.captured_at,
            "values": {f"0x{did:04X}": data.hex() for did, data in sorted(self.values.items())},
            "batch_requested": self.batch_requested,
            "batch_effective": self.batch_effective,
            "batch_rejections": self.batch_rejections,
            "batch_nrc": None if self.batch_nrc is None else f"0x{self.batch_nrc:02X}",
            "batch_nrc_name": None if self.batch_nrc is None else nrc_name(self.batch_nrc),
            "batched_reads": self.batched_reads,
            "skipped": {f"0x{did:04X}": why for did, why in sorted(self.skipped.items())},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DidSnapshot:
        """Inverse of :meth:`to_dict`. Snapshots written before the ``batch_*``
        fields existed load with them as ``None`` (not recorded) rather than
        as a claim that the sweep was unbatched."""

        def _opt_int(key: str) -> int | None:
            raw = data.get(key)
            if raw is None:
                return None
            if isinstance(raw, str):
                return int(raw, 16)
            return int(raw)

        return cls(
            values={
                int(did_hex, 16): bytes.fromhex(raw_hex)
                for did_hex, raw_hex in (data.get("values") or {}).items()
            },
            captured_at=float(data["captured_at"]),
            label=str(data.get("label", "")),
            batch_requested=_opt_int("batch_requested"),
            batch_effective=_opt_int("batch_effective"),
            batch_rejections=_opt_int("batch_rejections"),
            batch_nrc=_opt_int("batch_nrc"),
            batched_reads=_opt_int("batched_reads"),
            skipped={
                int(did_hex, 16): str(why) for did_hex, why in (data.get("skipped") or {}).items()
            },
        )


#: The JSONL record discriminator every bus-tool log uses (``tools/did_log``
#: headers included). ``can_memory_log`` wrote ``"type"`` until R06.1;
#: :func:`record_kind` still reads those files.
RECORD_KEY = "kind"


def record_kind(row: Mapping[str, Any]) -> str | None:
    """A JSONL record's discriminator; reads pre-R06.1 ``"type"`` logs too."""
    kind = row.get(RECORD_KEY, row.get("type"))
    return kind if isinstance(kind, str) else None


def load_widths(path: Path) -> dict[int, int]:
    """Measured payload widths from a :class:`DidSnapshot` file or a did_log JSONL header.

    Only a measurement counts: a catalog ``byte_length`` is the decoded value's
    width inside the payload, not the payload's own length. Lives beside the
    snapshot it reads (R06.1); ``tools/_bus_tool`` re-exports it.
    """
    text = path.read_text()
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError:
        first = next((line for line in text.splitlines() if line.strip()), "")
        raw = json.loads(first)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: not a did_sweep snapshot or did_log header")
    if record_kind(raw) == "header" and isinstance(raw.get("widths"), dict):
        return {int(key, 16): int(width) for key, width in raw["widths"].items()}
    if isinstance(raw.get("values"), dict):
        return {int(key, 16): len(hexvalue) // 2 for key, hexvalue in raw["values"].items()}
    raise ValueError(f"{path}: not a did_sweep snapshot or did_log header")


def sweep_dids(
    transport: Mode22Transport,
    dids: Iterable[int],
    *,
    delay_seconds: float = 0.0,
    label: str = "",
    on_progress: Callable[[int, bytes | None], None] | None = None,
) -> DidSnapshot:
    """Read every DID in ``dids``; keep the ones that answered positively.

    A DID whose read changes ECU state is not requested; the snapshot's
    ``skipped`` names it.
    """
    values: dict[int, bytes] = {}
    readable, skipped = without_side_effect_dids(dids)
    for did in readable:
        data = transport.read_did(did)
        if on_progress is not None:
            on_progress(did, data)
        if data is not None:
            values[did] = data
        if delay_seconds > 0:
            time.sleep(delay_seconds)
    return DidSnapshot(
        values=dict(values),
        captured_at=time.time(),
        label=label,
        batch_requested=1,
        batch_effective=1,
        batch_rejections=0,
        batched_reads=0,
        skipped=skipped,
    )


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

    ``transport`` needs to be a :class:`BatchedMode22Transport` (i.e. have a
    ``read_dids`` returning :class:`BatchRead`) to batch at all; a plain
    :class:`Mode22Transport` degrades to single reads throughout.

    A batch the ECU *rejects* outright (NRC, or silence) is counted separately
    from a DID the ECU merely omitted. When a rejected group's DIDs then answer
    individually, the ECU has shown it does not accept multi-DID requests
    (the 2008 Nissan ECM answers ``7F 22 12``): the sweep stops batching for
    the rest of the run instead of paying a refused request per group, and the
    snapshot records ``batch_effective=1`` with the NRC. A sweep in which no
    value shares a sample instant cannot come back looking like one where they
    all do.
    """
    batch_size = max(1, min(batch_size, MAX_BATCHED_DIDS))
    read_dids = getattr(transport, "read_dids", None)
    values: dict[int, bytes] = {}
    rejections = 0
    last_nrc: int | None = None
    batched_reads = 0

    def _single(did: int) -> bytes | None:
        data = transport.read_did(did)
        if on_progress is not None:
            on_progress(did, data)
        if data is not None:
            values[did] = data
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        return data

    def _snapshot(effective: int) -> DidSnapshot:
        return DidSnapshot(
            values=dict(values),
            captured_at=time.time(),
            label=label,
            batch_requested=batch_size,
            batch_effective=effective,
            batch_rejections=rejections,
            batch_nrc=last_nrc,
            batched_reads=batched_reads,
            skipped=skipped,
        )

    requested, skipped = without_side_effect_dids(dids)
    batchable = [d for d in requested if (byte_length_for(d) or 0) > 0]
    unknown = [d for d in requested if (byte_length_for(d) or 0) <= 0]

    if read_dids is None or batch_size == 1 or not batchable:
        for did in requested:
            _single(did)
        return _snapshot(1)

    batching = True
    for start in range(0, len(batchable), batch_size):
        group = batchable[start : start + batch_size]
        if not batching:
            for did in group:
                _single(did)
            continue
        result: BatchRead = read_dids(group, byte_length_for)
        if result.rejected:
            rejections += 1
            if result.negative_response_code is not None:
                last_nrc = result.negative_response_code
            answered = [did for did in group if _single(did) is not None]
            if answered:
                # The same DIDs answer one at a time but not together: this
                # ECU does not batch. Every further batch would be refused too.
                batching = False
            continue
        for did in group:
            data = result.readings.get(did)
            if data is None:
                # Either the ECU omitted it or the walk desynced before
                # reaching it — re-read individually rather than guess.
                _single(did)
                continue
            values[did] = data
            batched_reads += 1
            if on_progress is not None:
                on_progress(did, data)
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    for did in unknown:
        _single(did)

    return _snapshot(batch_size if batching else 1)


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

    This transport invokes only ``query_mode_22`` (0x22) and, when a session
    is requested, ``enter_diagnostic_session`` (0x10 session control).
    ``client`` is
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
    ) -> BatchRead:
        """Read up to ``MAX_BATCHED_DIDS`` DIDs in one same-instant request.

        ``clean`` is False if the response could not be walked to the end,
        which means a width was unknown or the payload was short. Callers must
        fall back to single-DID reads for anything missing rather than treat a
        partial walk as complete. ``rejected`` (with the NRC when there was
        one) means the ECU refused or ignored the request as a whole — not
        that it left a DID out.
        """
        self._ensure_session()
        reply = self._client.query_mode_22_batch(dids, request_id=self._request_id)
        if reply.response is None:
            return BatchRead(
                readings={},
                clean=False,
                rejected=True,
                negative_response_code=reply.negative_response_code,
            )
        readings, clean = split_batched_did_response(reply.response.payload, byte_length_for)
        return BatchRead(readings=readings, clean=clean)
