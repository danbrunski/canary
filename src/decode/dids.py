"""Mode 22 DID map loader + decoder.

Reads `data/dids.yaml` — the project's canonical record of identified
Nissan UDS Mode 22 (ReadDataByIdentifier) parameters — into typed
`DidEntry` objects. The Mode-22 counterpart of `bindings.py` (which
covers *broadcast* CAN bytes): DIDs are actively polled diagnostic
values, not passively-sniffed broadcast signals, so they get their own
file + access metadata (the gating diagnostic session, keep-alive).

`byte_start` / `byte_length` index into the Mode-22 *response payload* —
the bytes after the `62 <DID_hi> <DID_lo>` echo, i.e. exactly what
`CanObdClient.query_mode_22(...).payload` returns.

Strict pydantic schema so typos in the YAML fail loudly, not silently.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.decode import slices
from src.decode.memory_relations import (
    FlagEquivalenceRelation,
    MemoryArithmeticRelation,
    MemoryRelation,
)
from src.decode.slices import ByteSlice, LinearScale, PlausibleRange
from src.paths import DIDS_YAML
from src.yaml_load import load_yaml_model

log = logging.getLogger(__name__)

#: Wide-column ``source`` prefix for decoded DID signals. Like ``DBC_SOURCE``
#: in :mod:`src.decode.dbc`, this is deliberately NOT a
#: :data:`src.telemetry.TelemetrySource` member — it's a pseudo-source that
#: only exists as a column prefix (``did:<NAME>``) after decoding, so it
#: never collides with the raw ``obd:DID_<hex>`` poll rows it's derived from.
DID_SOURCE = "did"

#: Column prefix for service-0x23 channels (``mem:<NAME>``). Distinct from
#: ``did:`` on purpose: the two answer different questions about trust.
#: A DID number is stable across calibrations; a RAM address is only
#: valid for the ROM build it was found in, and a reader has to be able
#: to tell which kind of channel it is holding.
MEMORY_SOURCE = "mem"

#: Channel prefix the OBD scheduler / ELM327 path use for raw Mode-22 DID
#: poll responses (``DID_1208`` etc.), value carried as the hex payload
#: string in ``value_text``.
_RAW_DID_CHANNEL_PREFIX = "DID_"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


#: One evidence record for every catalog (R05.9): the DID catalog's refusal
#: (a ``constant`` never without its ``conditions`` and unit) now lives in
#: :class:`src.decode.slices.Evidence`, which bindings share.
Evidence = slices.Evidence


class CalibrationIdAccess(_Strict):
    """How to read the ECU's own calibration id (service ``0x21``).

    ``session`` is a measured vehicle fact, and deliberately has an
    ``unverified`` value: the id read was only ever sent inside the Mode-22
    session, so whether it also answers in the default session is unknown
    until a key-on/engine-off probe says so (W02.1). ``unverified`` and
    ``extended`` both ride the Mode-22 session window; only ``default``
    lets the collector read it bare at connect time.
    """

    local_identifier: int = Field(ge=0, le=0xFF)
    session: Literal["default", "extended", "unverified"]
    #: Where the ``session`` value comes from — a probe, a capture, or why it
    #: is still unverified. Required: an unexplained session fact is exactly
    #: the kind of thing nobody re-checks.
    evidence: str = Field(min_length=1)

    @property
    def needs_session(self) -> bool:
        return self.session != "default"


class DidAccess(_Strict):
    """How to reach Mode 22 on this vehicle's ECU.

    The 2008 Nissan ECM refuses service 0x22 in the default session
    (`7F 22 80`); it must be entered via UDS DiagnosticSessionControl
    sub-function `session` and kept alive by re-sending it every
    `keepalive_seconds` (TesterPresent 0x3E is itself rejected). The
    session is mutually exclusive with standard OBD Mode 01.
    """

    ecu_request_id: int = Field(ge=0, le=0x1FFFFFFF)
    ecu_response_id: int = Field(ge=0, le=0x1FFFFFFF)
    session: int | None = Field(default=None, ge=0, le=0xFF)
    #: Sub-function that returns the ECU to the default session, ending the
    #: Mode-01 blackout immediately instead of waiting out the S3 timeout.
    #: Absent means "no verified fast exit" — wait for the lapse.
    exit_session: int | None = Field(default=None, ge=0, le=0xFF)
    #: Settling time after an accepted ``exit_session`` before Mode 01 is
    #: polled again — a measured vehicle fact (C04.6), not a shared constant.
    #: Absent means the scheduler's default grace.
    exit_grace_seconds: float | None = Field(default=None, ge=0)
    keepalive_seconds: float | None = None
    #: Largest service-0x23 read this ECM answers, in bytes. A measured
    #: vehicle constraint, not a protocol limit: the 2026-09-20 sweep got
    #: positive responses at 32/33/40/48/56/60/62/63 and NRC 0x12 at 64
    #: while every interleaved DID kept answering. Required before a window
    #: can be declared, because an oversized window fails as a refusal at
    #: drive time rather than at load.
    max_memory_read_length: int | None = Field(default=None, ge=1)
    #: The calibration-id local-identifier read and the session it needs.
    #: Absent means this vehicle has no known calibration-id read.
    calibration_id: CalibrationIdAccess | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _calibration_id_session(self) -> DidAccess:
        if (
            self.calibration_id is not None
            and self.calibration_id.needs_session
            and self.session is None
        ):
            raise ValueError(
                "access.calibration_id needs a diagnostic session "
                f"({self.calibration_id.session!r}) but access.session is not set"
            )
        return self


class DidEntry(ByteSlice, LinearScale, PlausibleRange, _Strict):
    """An identified Mode 22 DID: where its value sits in the response
    payload + how to decode it.

    `scale`/`offset` are `None` for DIDs whose byte position is known but
    whose engineering scaling isn't pinned to ground truth yet (e.g. the
    air-charge register 0x1208 has a stable counts-per-gram ratio but no
    independent instrument pinning an engineering unit).
    """

    did: int = Field(ge=0, le=0xFFFF)
    name: str
    byte_start: int = Field(ge=0, le=63)
    byte_length: int = Field(ge=1, le=8)
    #: Full bytes after the ``62 <did>`` echo, measured from a single-DID
    #: response. This is deliberately distinct from ``byte_length``: the
    #: latter is only the decoded signal slice and cannot delimit a batched
    #: response when the payload carries additional bytes.
    response_length: int | None = Field(default=None, ge=1, le=64)
    response_length_source: str | None = None
    byte_order: Literal["big", "little"] = "big"
    #: Two's-complement slice (the shared :class:`~src.decode.slices.ByteSlice`
    #: field; see :attr:`MemoryChannel.signed`).
    signed: bool = False
    scale: float | None = None
    offset: float | None = None
    unit: str = "unknown"
    confidence: Literal["confirmed", "high", "medium", "low", "hypothesized"]
    plausible_min: float | None = None
    plausible_max: float | None = None
    #: Former names this DID has carried. Capture profiles reference DIDs BY
    #: NAME and `parse_did_token` raises on an unknown one, so a rename would
    #: otherwise break every saved profile still using the old name —
    #: including vehicle-scoped profiles on the Pi that this repo cannot see.
    #: A rename either moves its old name here or, when the old name was
    #: wrong and must not survive (the 2026-09-18 fuel relabel), migrates
    #: every stored artifact with ``tools/migrate_channel_names.py`` before
    #: the daemon restarts.
    aliases: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    #: Readings of this DID that were CONSIDERED AND REJECTED, with why.
    #: A plausible-but-wrong identification gets re-derived by the next person
    #: unless the refutation is recorded as data — the same reason
    #: bindings.yaml carries `absent_signals`. Same shape as `evidence`; the
    #: decoder ignores both.
    evidence_against: list[Evidence] = Field(default_factory=list)
    notes: str | None = None
    #: What answering this DID does to the ECU besides answering, with where
    #: that was shown. Set, the DID is never polled: capture profiles and the
    #: config's ``obd_dids`` refuse it, the profile inventory leaves it out and
    #: the sweep and logging tools skip it (:func:`side_effect_dids`). 0x120B
    #: cleared status bit 0 of every diagnostic item on each read, so it was
    #: catalogued as a read while it perturbed the knock-fault state CANary was
    #: observing (firmware, 2026-10-10). A one-off ``/control/obd_request``
    #: still reads it: that is an operator asking for it by number.
    side_effect: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _response_length_has_provenance(self) -> DidEntry:
        if (self.response_length is None) != (self.response_length_source is None):
            raise ValueError("response_length and response_length_source must be set together")
        if (
            self.response_length is not None
            and self.response_length < self.byte_start + self.byte_length
        ):
            raise ValueError("response_length cannot be shorter than the decoded byte slice")
        return self

    @model_validator(mode="after")
    def _kline_evidence_cannot_name_a_can_did(self) -> DidEntry:
        """Refuse an entry whose only support was observed on K-line.

        This catalog is polled over CAN (``access.ecu_request_id``). Until
        2026-10-04 DID 0x1102 was catalogued as long-term fuel trim B1 and
        0x1125 as LTFT B2, both from UpRev's K-line stream labels. The labels
        were UpRev's, not the ECM's: the firmware audit of 2026-10-05 found the
        ECM's K-line and CAN pointer tables send every DID they share to the
        same variable (except 0x1247, 0x1248 and 0x1315), so K-line 0x1102 is
        the speed byte too. K-line support for this catalog has in practice
        been a third-party tool's naming, and a CAN entry needs evidence read
        on CAN or from the firmware. K-line evidence may corroborate an entry
        or sit in ``evidence_against``; it cannot be the whole case for one.
        """
        transports = {item.transport for item in self.evidence}
        if "kline" in transports and "can" not in transports:
            raise ValueError(
                f"DID 0x{self.did:04X} ({self.name}): every supporting evidence entry "
                "that names a transport says `kline`, but this is the CAN catalog. "
                "K-line support has meant a third-party tool's labels, not the ECM's: "
                "UpRev named 0x1102 long-term trim B1, and the firmware sends 0x1102 "
                "to the speed byte on both transports (2026-10-04/05). Add CAN or "
                "firmware evidence (`transport: can`) or move the K-line finding to "
                "`evidence_against`."
            )
        return self


class MemoryKnownAnswer(_Strict):
    """A cross-check that proves a RAM address still holds what it claims.

    An address survives a reflash only by luck, and the failure is silent:
    the read still succeeds, the value is still in range, and the decode
    still produces a plausible number that is now measuring something else.
    A plausibility bound cannot catch that. A *structural* relation to a
    stable identifier can, because a moved address will not keep satisfying
    it — so the check is recorded here rather than left to whoever
    remembers, and :mod:`tools.verify_memory_channels` refuses to re-stamp a
    calibration without it passing.

    ``relation`` is an enumeration, not an expression: the arithmetic lives
    in code where it can be tested, and the catalog only selects which
    vehicle fact is being asserted.

    * ``clamped_high_byte`` — ``did_raw == max(floor, word >> 8)``. The 2008
      Nissan ECM exposes DID 0x1134 as exactly this view of the commanded
      equivalence-ratio word: the high byte, floored so it cannot represent
      the fuel-cut state.
    * ``equals_did`` — the RAM word and the DID are the same register. This
      is the relation for an address found by sweeping against a DID that
      already carries the quantity.
    * ``linear_did`` — ``word * word_scale`` and ``did_raw * did_scale +
      did_offset`` are the same quantity in engineering units, within
      ``tolerance`` (absolute) or ``relative_tolerance``. For a RAM value that
      is a differently-scaled or quantized view of a DID — a map-axis rpm
      trace in 50-rpm steps against a 12.5-rpm DID, or final ignition timing
      in degrees against DID 0x110A's ``110 - degrees`` encoding
      (``did_scale: -1``, ``did_offset: 110``).
    * ``reciprocal_did`` — ``constant / word`` equals ``did_raw`` within
      ``tolerance`` counts. For a period counter checked against a speed.

    ``min_agreement`` exists because exactness and acquisition skew are
    different things. The memory read and the cross-check DID are two
    requests about 1.5 ms apart, so a fast-moving quantity legitimately
    disagrees on the samples where it changed in between — ignition timing
    reproduces at 94-99.9% depending on how hard the engine is being
    revved. Demanding 100% would fail a correct binding for a reason that
    has nothing to do with the address.
    """

    did: int = Field(ge=0, le=0xFFFF)
    relation: Literal["clamped_high_byte", "equals_did", "linear_did", "reciprocal_did"]
    floor: int = Field(default=0, ge=0, le=0xFFFF)
    #: ``linear_did``: engineering units per RAM count / per DID count.
    word_scale: float | None = None
    did_scale: float | None = None
    #: ``linear_did``: engineering units added to ``did_raw * did_scale``, for
    #: a DID whose encoding carries an offset (``110 - timing``).
    did_offset: float | None = None
    #: ``reciprocal_did``: the numerator, in DID counts x RAM counts.
    constant: float | None = None
    #: Allowed disagreement — absolute (engineering units for ``linear_did``,
    #: DID counts for ``reciprocal_did``) and/or a fraction of the DID value.
    tolerance: float | None = Field(default=None, ge=0)
    relative_tolerance: float | None = Field(default=None, ge=0, le=1)
    #: Fraction of reads that must satisfy the relation. Below 1.0 only
    #: where acquisition skew makes exactness unreachable; see the class
    #: docstring.
    min_agreement: float = Field(default=1.0, gt=0.0, le=1.0)
    notes: str | None = None

    @model_validator(mode="after")
    def _relation_has_its_parameters(self) -> MemoryKnownAnswer:
        """A relation missing its numbers would silently accept anything."""
        if self.relation == "linear_did":
            if self.word_scale is None or self.did_scale is None:
                raise ValueError("linear_did requires word_scale and did_scale")
            if self.tolerance is None and self.relative_tolerance is None:
                raise ValueError("linear_did requires tolerance or relative_tolerance")
        if self.relation == "reciprocal_did":
            if self.constant is None or self.tolerance is None:
                raise ValueError("reciprocal_did requires constant and tolerance")
        if self.did_offset is not None and self.relation != "linear_did":
            # Every other relation ignores it, so it would read as a check
            # the verifier never makes.
            raise ValueError(f"did_offset applies only to linear_did, not {self.relation}")
        return self


class MemoryChannel(ByteSlice, LinearScale, PlausibleRange, _Strict):
    """A recurring ReadMemoryByAddress (service ``0x23``) channel.

    A DID is an identifier the ECU defines; a RAM address is a location in
    the ECU program's memory map. Calibration flashes on the same program
    leave it in place (operator, 2026-09-21), but another program is another
    map. That difference is why this is a separate model rather than a
    ``DidEntry`` with an ``address`` field:

    * ``verified_tune_revision`` is REQUIRED, so an address is never
      recorded without saying which calibration it was checked under. It is
      provenance, not a gate: the collector polls the channel whatever is
      running, and ``known_answer`` is what re-verifies the address.
    * ``sentinel_raw`` names a raw word that is a *state*, not a
      measurement. The Target AFR word is the case that forced it: zero
      means fuel cut, and scaling it as phi yields 0.0, which then divides
      into a target AFR of infinity. A sentinel decodes to ``None`` and is
      reported through ``sentinel_meaning``.

    ``byte_start``/``byte_length`` index into the service-0x23 response
    payload — the bytes after the ``63`` positive-response byte, which is
    what the assembler returns. Service 0x23 echoes no address, so the
    payload is exactly ``length`` bytes.
    """

    name: str
    #: Absolute address of the first byte read.
    address: int = Field(ge=0, le=0xFFFFFFFF)
    #: How many bytes to request. The ECM answers this exactly.
    length: int = Field(ge=1, le=64)
    byte_start: int = Field(default=0, ge=0, le=63)
    byte_length: int = Field(ge=1, le=8)
    byte_order: Literal["big", "little"] = "big"
    #: Two's-complement slice. The firmware keeps retard, learned offsets and
    #: final timing as s8/s16/s32: read unsigned, a 1-degree retard (s16
    #: 0xFF00) decodes as +255 degrees and every mean and comparison after it
    #: is wrong. ``sentinel_raw`` is then stated in the signed range.
    signed: bool = False
    scale: float | None = None
    offset: float | None = None
    unit: str = "unknown"
    confidence: Literal["confirmed", "high", "medium", "low", "hypothesized"]
    plausible_min: float | None = None
    plausible_max: float | None = None
    #: The calibration this address was verified against. Required: see the
    #: class docstring. ``"any"`` is deliberately NOT accepted — an address
    #: that survives a reflash has to be demonstrated, not assumed.
    verified_tune_revision: str = Field(min_length=1)
    #: Raw value that means "no measurement", with what it means.
    sentinel_raw: int | None = None
    sentinel_meaning: str | None = None
    #: How to prove this address still holds this quantity after a reflash.
    known_answer: MemoryKnownAnswer | None = None
    #: Relations to other RAM, to a bit pattern, or to a Mode 01 PID
    #: (:mod:`src.decode.memory_relations`) -- for the channels no DID
    #: mirrors. A relation re-verifies every channel it reads, so a channel
    #: can be checkable through a relation declared on another one; ask
    #: :func:`memory_checks` rather than reading this list.
    relations: list[MemoryRelation] = Field(default_factory=list)
    #: Set when ``verified_tune_revision`` was stamped WITHOUT a known-answer
    #: check, with the operator's reason. "Verified" and "carried across a
    #: flash on an argument" are different facts, and a stamp that hides the
    #: difference turns the second into the first the next time someone reads
    #: the catalog.
    revision_carried_reason: str | None = None
    #: Read this channel in one request with every other channel naming the
    #: same window.
    #:
    #: This is the only way to sample two quantities at the same instant on
    #: this ECM: it refuses batched Mode 22, so two DIDs are always two
    #: requests and always milliseconds apart. One 0x23 read spanning both
    #: addresses returns both from a single ECU operation. Named rather
    #: than inferred from address proximity, so "these must be same-instant"
    #: is a statement the catalog makes and the loader checks, not a
    #: coincidence of layout that a later edit could silently break.
    window: str | None = None
    aliases: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    evidence_against: list[Evidence] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> MemoryChannel:
        if self.byte_start + self.byte_length > self.length:
            raise ValueError(
                f"{self.name}: decoded slice "
                f"[{self.byte_start}:{self.byte_start + self.byte_length}] "
                f"does not fit in the {self.length}-byte read"
            )
        # Paired, and representable in the slice's own signedness: an s16
        # sentinel spelled 0xFB00 instead of -1280 could never match.
        slices.check_sentinel(
            self.name,
            self.sentinel_raw,
            self.sentinel_meaning,
            byte_length=self.byte_length,
            signed=self.signed,
        )
        for relation in self.relations:
            if isinstance(relation, MemoryArithmeticRelation) and self.name in relation.operands:
                raise ValueError(
                    f"{self.name}: relation {relation.label} names its own channel as an "
                    "operand; a word compared with itself always agrees"
                )
            if (
                isinstance(relation, FlagEquivalenceRelation)
                and self.name not in relation.channels()
            ):
                raise ValueError(
                    f"{self.name}: relation {relation.label} never reads {self.name}, so "
                    "it cannot vouch for this address; declare it on a channel it tests"
                )
        return self


def decode_memory_raw(channel: MemoryChannel, payload: bytes) -> int | None:
    """Raw integer for `channel` from a service-0x23 response payload.

    ``None`` when the payload is too short for the declared slice.
    """
    return channel.raw_from(payload)


def apply_memory_scaling(channel: MemoryChannel, raw: int) -> float | None:
    """Apply `scale`/`offset`, or `None` for a sentinel or unpinned scale.

    A sentinel is not a small value — it is the absence of one. Returning
    ``None`` keeps it out of every mean, comparison and model input rather
    than contributing a zero.
    """
    if is_memory_sentinel(channel, raw):
        return None
    return channel.scaled(raw)


def is_memory_sentinel(channel: MemoryChannel, raw: int) -> bool:
    """Whether `raw` is the channel's declared no-measurement state."""
    return channel.sentinel_raw is not None and raw == channel.sentinel_raw


@dataclass(frozen=True)
class MemoryRead:
    """One service-0x23 request and the channels it decodes.

    A single-channel entry is a window of one, so the collector has one code
    path rather than a special case that only the multi-field form exercises.
    ``offsets`` gives each channel's byte offset within the reply, because a
    channel's ``byte_start`` is relative to its own address, not to the start
    of the window it ended up in.
    """

    address: int
    length: int
    channels: tuple[MemoryChannel, ...]
    offsets: tuple[int, ...]

    @property
    def name(self) -> str:
        window = self.channels[0].window
        return window if window else self.channels[0].name


def plan_memory_reads(channels: Sequence[MemoryChannel]) -> list[MemoryRead]:
    """Group `channels` into the requests that will actually be sent.

    Channels naming the same ``window`` become one read spanning all of
    them; an unwindowed channel becomes a read of its own. Order is stable
    so a plan is reproducible.
    """
    groups: dict[str, list[MemoryChannel]] = {}
    order: list[str] = []
    for channel in channels:
        key = channel.window or f"\x00{channel.name}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(channel)

    reads: list[MemoryRead] = []
    for key in order:
        members = groups[key]
        start = min(c.address for c in members)
        end = max(c.address + c.length for c in members)
        reads.append(
            MemoryRead(
                address=start,
                length=end - start,
                channels=tuple(members),
                offsets=tuple(c.address - start for c in members),
            )
        )
    return reads


def decode_memory_window(channel: MemoryChannel, payload: bytes, offset: int) -> int | None:
    """Raw integer for `channel` from a window reply starting `offset` earlier."""
    return channel.raw_from(payload[offset:])


def check_known_answer(channel: MemoryChannel, word: int, did_raw: int) -> bool:
    """Whether `did_raw` is what `channel`'s declared relation predicts.

    ``word`` is the memory channel's raw value and ``did_raw`` the raw value
    of the cross-check DID, both read at the same instant.
    """
    known = channel.known_answer
    if known is None:
        raise ValueError(f"{channel.name} declares no known-answer check")
    if known.relation == "clamped_high_byte":
        return did_raw == max(known.floor, word >> 8)
    if known.relation == "equals_did":
        return did_raw == word
    if known.relation == "linear_did":
        assert known.word_scale is not None and known.did_scale is not None
        reference = did_raw * known.did_scale + (known.did_offset or 0.0)
        allowed = max(known.tolerance or 0.0, (known.relative_tolerance or 0.0) * abs(reference))
        return abs(word * known.word_scale - reference) <= allowed
    if known.relation == "reciprocal_did":
        assert known.constant is not None and known.tolerance is not None
        return word > 0 and abs(known.constant / word - did_raw) <= known.tolerance
    raise ValueError(f"unhandled known-answer relation {known.relation!r}")


def relation_channel_names(owner: MemoryChannel, relation: MemoryRelation) -> tuple[str, ...]:
    """Every memory channel `relation` (declared on `owner`) reads, owner first."""
    return tuple(dict.fromkeys((owner.name, *relation.channels())))


def memory_checks(
    channel: MemoryChannel, catalog: Sequence[MemoryChannel]
) -> list[tuple[MemoryChannel, MemoryRelation]]:
    """Every declared relation in `catalog` that reads `channel`.

    A relation verifies each channel it reads, not only the one it is
    declared on: ``0x1252 = trunc(0x124E/2) + trunc(0x1250/2)`` would fail if
    any of the three moved. So whether a channel is checkable is a question
    about the catalog, never about the channel's own entry.
    """
    return [
        (owner, relation)
        for owner in catalog
        for relation in owner.relations
        if channel.name in relation_channel_names(owner, relation)
    ]


def is_memory_checkable(channel: MemoryChannel, catalog: Sequence[MemoryChannel]) -> bool:
    """Whether anything can re-verify `channel`: a DID known answer or a relation."""
    return channel.known_answer is not None or bool(memory_checks(channel, catalog))


def _window_key(channel: MemoryChannel) -> str:
    return channel.window or f"\x00{channel.name}"


def relation_reads(
    subject: MemoryChannel,
    owner: MemoryChannel,
    relation: MemoryRelation,
    by_name: Mapping[str, MemoryChannel],
) -> list[tuple[int, int]]:
    """The service-0x23 reads (address, length) one sample of a check takes.

    Channels sharing a window come back in one reply, as the collector reads
    them. The read holding `subject` is widened by the subject's width on
    both sides, so the same reply also carries the neighbouring bytes the
    relation must fail on. The subject's read comes first.
    """
    groups: dict[str, list[MemoryChannel]] = {}
    for name in relation_channel_names(owner, relation):
        channel = by_name[name]
        groups.setdefault(_window_key(channel), []).append(channel)
    reads: list[tuple[int, int]] = []
    subject_key = _window_key(subject)
    for key in [subject_key, *(k for k in groups if k != subject_key)]:
        members = groups[key]
        start = min(c.address for c in members)
        end = max(c.address + c.length for c in members)
        if key == subject_key:
            span_start, span_length = neighbour_span(subject)
            start = min(start, span_start)
            end = max(end, span_start + span_length)
        reads.append((start, end - start))
    return reads


def neighbour_span(subject: MemoryChannel) -> tuple[int, int]:
    """The (address, length) read that carries `subject` and both neighbours.

    Widened by the subject's width on each side: a check is only evidence
    if it fails on the bytes beside the address, so every verifier read --
    a relation's or a DID known answer's -- takes them in the same reply.
    """
    start = subject.address - subject.byte_length
    end = subject.address + subject.length + subject.byte_length
    return start, end - start


class DidMap(_Strict):
    """Top-level dids.yaml document."""

    schema_version: int
    vehicle: dict[str, Any] = Field(default_factory=dict)
    access: DidAccess
    dids: list[DidEntry]
    #: Service-0x23 channels. They live in this file, not their own, because
    #: they are gated by the same ``access`` session and keep-alive as Mode
    #: 22 — splitting them would duplicate that and let the two drift.
    memory: list[MemoryChannel] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_channel_names(self) -> DidMap:
        """One namespace: a memory channel may not shadow a DID.

        Capture profiles and analysis specs reference channels by name, so
        two catalog entries sharing one name would resolve by whichever list
        was searched first.
        """
        # One entry per DID id: the decoder indexes the catalog by id
        # (``did_entries_by_hex``, last entry wins), so a second entry for an
        # id would silently replace the first. The derived-session store
        # versions DID channels per entry; a shadowed entry made a refreshed
        # frame disagree with a rebuilt one (P01 verification, 2026-09-26).
        by_id: dict[int, str] = {}
        for entry in self.dids:
            if entry.did in by_id:
                raise ValueError(
                    f"DID 0x{entry.did:04X} is declared twice ({by_id[entry.did]!r} and "
                    f"{entry.name!r}); the decoder keeps only the last, so merge them"
                )
            by_id[entry.did] = entry.name
        seen: dict[str, str] = {}
        for entry in self.dids:
            for name in (entry.name, *entry.aliases):
                if name in seen:
                    raise ValueError(
                        f"DID channel name {name!r} is declared twice; one name, one entry"
                    )
                seen[name] = "did"
        clashes = [
            c.name
            for c in self.memory
            if c.name in seen or c.name in {m.name for m in self.memory if m is not c}
        ]
        if clashes:
            raise ValueError(
                f"memory channel names collide with existing channels: {sorted(set(clashes))}"
            )
        self._check_windows()
        self._check_relations()
        return self

    def _check_relations(self) -> None:
        """A relation must resolve, be readable, and say how much skew it allows.

        Each refusal names the channel and relation. An unknown operand would
        make the verifier skip the check (or crash on the truck); a
        ``same_reply`` relation across windows would compare values read
        milliseconds apart as if simultaneous; an over-wide read comes back
        NRC 0x12 and loses the sample.
        """
        by_name = {c.name: c for c in self.memory}
        ceiling = self.access.max_memory_read_length
        for owner in self.memory:
            for relation in owner.relations:
                where = f"memory channel {owner.name}: relation {relation.label}"
                names = relation_channel_names(owner, relation)
                unknown = [name for name in names if name not in by_name]
                if unknown:
                    raise ValueError(f"{where} names unknown memory channel(s) {unknown}")
                windows = {_window_key(by_name[name]) for name in names}
                if isinstance(relation, MemoryArithmeticRelation):
                    shared = owner.window is not None and all(
                        by_name[name].window == owner.window for name in relation.operands
                    )
                    if relation.read == "same_reply" and not shared:
                        raise ValueError(
                            f"{where} is declared same_reply, but its operands are not all "
                            f"in {owner.name}'s window ({owner.window!r}), so they come back "
                            "in different replies; declare cross_read with a min_agreement"
                        )
                    if relation.read == "cross_read" and shared:
                        raise ValueError(
                            f"{where} is declared cross_read, but every operand shares the "
                            f"{owner.window!r} window and comes back in one reply; declare "
                            "same_reply"
                        )
                elif (
                    isinstance(relation, FlagEquivalenceRelation)
                    and len(windows) > 1
                    and "min_agreement" not in relation.model_fields_set
                ):
                    raise ValueError(
                        f"{where} reads {len(windows)} separate windows, so it must state "
                        "min_agreement: the flags are read in different requests"
                    )
                if ceiling is None:
                    raise ValueError(
                        f"{where} needs access.max_memory_read_length recorded to plan its reads"
                    )
                for name in names:
                    for address, length in relation_reads(by_name[name], owner, relation, by_name):
                        if length > ceiling:
                            raise ValueError(
                                f"{where}: verifying {name} reads {length} bytes at "
                                f"0x{address:08X} (neighbours included), over this ECM's "
                                f"{ceiling}-byte service-0x23 ceiling"
                            )

    def _check_windows(self) -> None:
        """A declared window must be readable, and must mean one thing.

        Both failures below are silent at drive time and obvious here. An
        oversized window comes back NRC 0x12 for the whole group, losing
        every channel in it at once. A window whose members disagree about
        the calibration would hand one reply to two different claims about
        what the ROM is.
        """
        ceiling = self.access.max_memory_read_length
        for read in plan_memory_reads(self.memory):
            members = read.channels
            if len(members) == 1 and members[0].window is None:
                continue
            if ceiling is None:
                raise ValueError(
                    f"window {read.name!r} needs access.max_memory_read_length "
                    "recorded before it can be declared readable"
                )
            if read.length > ceiling:
                raise ValueError(
                    f"window {read.name!r} spans {read.length} bytes, over this "
                    f"ECM's measured {ceiling}-byte service-0x23 ceiling"
                )
            revisions = {c.verified_tune_revision for c in members}
            if len(revisions) > 1:
                raise ValueError(
                    f"window {read.name!r} mixes calibrations {sorted(revisions)}; "
                    "one reply cannot be valid under two ROM builds"
                )


def load_did_map(path: Path) -> DidMap:
    """Load and validate dids.yaml. Raises ``YamlModelError`` (a ``ValueError``
    naming the file) on a parse or schema mismatch.

    Cached by the file's identity and stat (:func:`src.yaml_load.load_yaml_model`),
    so an edited file is re-read. Callers share the one model and none mutates
    it (checked over every caller of this and ``load_bindings``, R26.3); one
    that needs to should ``model_copy(deep=True)``.
    """
    return load_yaml_model(path, DidMap).model


# The repo catalog accessors (R01.2): the one cached loader of the checked-in
# data/dids.yaml and the lookups built on it. Repo-rooted (``src.paths``), so
# the working directory does not matter. ``src.collection.obd_scheduler``
# re-exports them for its callers.


@lru_cache(maxsize=1)
def load_repo_did_map() -> DidMap | None:
    """Parsed data/dids.yaml, or ``None`` when absent/unparseable.

    ``None`` keeps the collector running without DID metadata — callers fall
    back to hex-only token parsing and session-less polling. The one cached
    loader of the repo DID catalog (``control.obd_request`` kept a documented
    copy; R01.2 retired it).
    """
    if not DIDS_YAML.exists():
        return None
    try:
        return load_did_map(DIDS_YAML)
    except Exception:  # noqa: BLE001 — bad YAML shouldn't crash the collector
        return None


@lru_cache(maxsize=1)
def _did_ids_by_name() -> dict[str, int]:
    """Symbolic DID name → identifier from data/dids.yaml.

    Empty when the file is absent or unparseable — callers then accept only
    4-char hex, matching the pre-resolution behavior. Mirrors how
    ``parse_pid_token`` resolves PID names against ``_PID_IDS_BY_NAME``.
    """
    did_map = load_repo_did_map()
    if did_map is None:
        return {}
    # Aliases resolve too, so renaming a DID cannot break a saved capture
    # profile that still references its old name. Current names are inserted
    # last so they win any collision with a stale alias.
    resolved: dict[str, int] = {}
    for entry in did_map.dids:
        for alias in entry.aliases:
            resolved[alias] = entry.did
    for entry in did_map.dids:
        resolved[entry.name] = entry.did
    return resolved


def known_did_names() -> tuple[str, ...]:
    """Every DID name and alias the catalog resolves, side-effect DIDs included
    (naming one is fine; polling one is refused: :func:`refuse_side_effect_dids`)."""
    return tuple(_did_ids_by_name())


@lru_cache(maxsize=1)
def side_effect_dids() -> dict[int, str]:
    """DID id -> ``"<NAME>: <side effect>"`` for every entry that changes ECU
    state when read (:attr:`DidEntry.side_effect`). Empty without a catalog."""
    did_map = load_repo_did_map()
    if did_map is None:
        return {}
    return {
        entry.did: f"{entry.name}: {entry.side_effect}"
        for entry in did_map.dids
        if entry.side_effect
    }


def pollable_did_names() -> tuple[str, ...]:
    """The inventory a capture profile may poll: :func:`known_did_names`
    without the side-effect DIDs."""
    banned = side_effect_dids()
    by_name = _did_ids_by_name()
    return tuple(name for name in by_name if by_name[name] not in banned)


def refuse_side_effect_dids(dids: Iterable[int], *, where: str) -> None:
    """Raise naming every DID in ``dids`` that changes ECU state when read.

    The one check behind every path that turns a DID into a repeated request,
    so a profile, a config list or a tool cannot poll one by forgetting to ask.
    """
    banned = side_effect_dids()
    hits = sorted({did for did in dids if did in banned})
    if hits:
        named = "; ".join(f"0x{did:04X} ({banned[did]})" for did in hits)
        raise ValueError(
            f"{where} would poll a DID whose read changes ECU state, which is never "
            f"polled: {named}. Read it once by hand with /control/obd_request if it "
            "is needed."
        )


def did_ids_for_tokens(tokens: Iterable[str]) -> list[int]:
    """The DID ids that ``tokens`` (names, aliases or 4-digit hex) resolve to;
    a token the catalog does not know is left to the callers that refuse it."""
    by_name = _did_ids_by_name()
    ids: list[int] = []
    for token in tokens:
        text = token.strip()
        if len(text) == 4 and all(c in "0123456789abcdefABCDEF" for c in text):
            ids.append(int(text, 16))
        elif text in by_name:
            ids.append(by_name[text])
    return ids


def without_side_effect_dids(dids: Iterable[int]) -> tuple[list[int], dict[int, str]]:
    """``dids`` split into the ones a sweep may read and the skipped ones with why."""
    banned = side_effect_dids()
    kept: list[int] = []
    skipped: dict[int, str] = {}
    for did in dids:
        if did in banned:
            skipped[did] = banned[did]
        else:
            kept.append(did)
    return kept, skipped


def did_access() -> DidAccess | None:
    """The access block of data/dids.yaml (gating session, keep-alive)."""
    did_map = load_repo_did_map()
    return None if did_map is None else did_map.access


def memory_channels() -> tuple[MemoryChannel, ...]:
    """Every service-0x23 channel in the catalog, unfiltered.

    Selecting the ones a capture profile names is :func:`select_memory_channels`'s job.
    """
    did_map = load_repo_did_map()
    return () if did_map is None else tuple(did_map.memory)


def select_memory_channels(names: Iterable[str]) -> tuple[MemoryChannel, ...]:
    """The catalog's memory channels ``names`` selects, in catalog order.

    Raises ``ValueError`` naming every name the catalog does not hold, as
    ``parse_did_token`` does for a DID. Selecting by filtering the catalog
    instead dropped an unknown name without a word: a profile still naming a
    renamed channel ran with that channel silently unpolled. Renames keep no
    alias (2026-09-18 policy), so a stale name is migrated with
    ``tools/migrate_channel_names.py``, never resolved here.
    """
    wanted = tuple(dict.fromkeys(name.strip() for name in names))
    catalog = memory_channels()
    known = {channel.name for channel in catalog}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        raise ValueError(
            f"memory channel(s) not in the data/dids.yaml memory catalog: {', '.join(unknown)}"
            " (a renamed channel keeps no alias; migrate saved profiles with"
            " tools/migrate_channel_names.py)"
        )
    selected = set(wanted)
    return tuple(channel for channel in catalog if channel.name in selected)


def did_response_widths() -> tuple[dict[int, int], dict[int, str]]:
    """Provenance-backed full Mode-22 payload widths, keyed by DID."""
    did_map = load_repo_did_map()
    if did_map is None:
        return {}, {}
    widths = {
        entry.did: entry.response_length
        for entry in did_map.dids
        if entry.response_length is not None
    }
    sources = {
        entry.did: entry.response_length_source
        for entry in did_map.dids
        if entry.response_length_source is not None
    }
    return widths, sources


def decode_did_raw(entry: DidEntry, payload: bytes) -> int | None:
    """Extract the raw integer for `entry` from a Mode-22 response payload.

    `payload` is the data after the `62 HH LL` echo (what
    `CanObdClient.query_mode_22().payload` yields). Returns `None` when the
    payload is too short for the declared byte range.
    """
    return entry.raw_from(payload)


def apply_scaling(entry: DidEntry, raw: int) -> float | None:
    """Apply `scale`/`offset`; `None` when scaling isn't pinned yet."""
    return entry.scaled(raw)


def is_plausible(entry: DidEntry, decoded: float) -> bool | None:
    """Whether `decoded` is within `entry`'s plausible range.

    `True`/`False` when at least one bound is set; `None` when neither is
    (no check). Mirrors `bindings.is_plausible` so the DID validator can
    flag misapplied scalings the same way the broadcast one does.
    """
    return entry.plausible(decoded)


@dataclass(frozen=True)
class DecodedDid:
    """One raw ``DID_<hex>`` reading decoded against the catalog."""

    entry: DidEntry
    #: Scaled value, or the raw integer when ``scale`` isn't pinned yet.
    value: float
    #: ``entry.unit``, or ``"raw"`` when the value is an unscaled count.
    unit: str


def did_entries_by_hex(did_map: DidMap) -> dict[str, DidEntry]:
    """Index a catalog by uppercase 4-char hex id — the raw channel suffix."""
    return {f"{entry.did:04X}": entry for entry in did_map.dids}


#: Sources whose ``DID_<hex>`` rows are *not* this catalog's. ``kline`` rows of
#: that form predate K01.1 (2026-09-24) and name no module: a scan tool's ABS
#: 0x1210 (yaw rate) was stored exactly like the ECM's 0x1210 (TPS2). Newer
#: K-line rows are ``<module>:<hex>`` (``src.collection.kline.names``) and never
#: match the prefix at all.
FOREIGN_DID_SOURCES: frozenset[str] = frozenset({"kline"})


def decode_raw_did_reading(
    by_hex: Mapping[str, DidEntry], channel: str, payload_hex: object, *, source: str
) -> DecodedDid | None:
    """Decode one raw ``DID_<hex>`` reading, or ``None`` if it isn't one.

    ``source`` is required so no caller can decode a row without saying whose
    it is: a legacy ``kline:DID_<hex>`` row is refused (it may be any module's).

    The single home for the raw-reading decode policy, shared by the offline
    frame path (:func:`decode_did_rows`), the live feature source, and the
    dashboard's live/REST telemetry paths — so a DID reads the same number
    everywhere. ``None`` covers every "not decodable" case: a non-DID
    channel, an id absent from the catalog, a missing/malformed hex payload,
    and a payload too short for the entry's byte range.

    When ``scale`` isn't pinned in ``dids.yaml`` the **raw integer** is
    returned with unit ``"raw"`` — see :func:`decode_did_rows` for why that
    is useful rather than a failure.
    """
    if source in FOREIGN_DID_SOURCES or not channel.startswith(_RAW_DID_CHANNEL_PREFIX):
        return None
    entry = by_hex.get(channel[len(_RAW_DID_CHANNEL_PREFIX) :].upper())
    if entry is None:
        return None
    if not isinstance(payload_hex, str) or not payload_hex:
        return None
    try:
        payload = bytes.fromhex(payload_hex)
    except ValueError:
        return None
    raw_int = decode_did_raw(entry, payload)
    if raw_int is None:
        return None
    scaled = apply_scaling(entry, raw_int)
    if scaled is None:
        return DecodedDid(entry=entry, value=float(raw_int), unit="raw")
    return DecodedDid(entry=entry, value=float(scaled), unit=entry.unit)


def decode_did_rows(long_frame: pd.DataFrame, did_map: DidMap) -> pd.DataFrame:
    """Append ``did:<NAME>`` rows decoded from raw ``DID_<hex>`` poll readings.

    The Mode-22 counterpart of :func:`src.preprocessing.decode.decode_can_rows`.
    The OBD scheduler / ELM327 path emit a polled DID as an ``obd``-source
    row with ``channel="DID_<hex>"`` and the raw response payload as a hex
    string in ``value_text``. This decodes those against ``did_map`` into
    named, scaled signals so the rest of the pipeline sees
    ``did:ECM_AIR_CHARGE`` numeric columns (recipes target them, IF/LSTM
    learn their residual distribution, the gauge shows them in engineering
    units).

    When a DID's ``scale`` isn't pinned in ``dids.yaml`` (e.g.
    ``ECM_AIR_CHARGE``, whose engineering unit is still outstanding), the **raw
    integer** is emitted instead of a scaled value — anomaly detection
    works on deviation from baseline, so unscaled-but-consistent counts are
    perfectly usable; the absolute calibration only matters for human-facing
    units.

    Returns a new frame with the decoded rows appended + re-sorted. The
    input is not mutated. Empty / no-DID-rows / no-matching-DID inputs are
    returned unchanged.
    """
    if long_frame.empty or "channel" not in long_frame.columns:
        return long_frame

    raw = long_frame[long_frame["channel"].astype("string").str.startswith(_RAW_DID_CHANNEL_PREFIX)]
    if "source" in raw.columns:
        raw = raw[~raw["source"].isin(FOREIGN_DID_SOURCES)]
    if raw.empty:
        return long_frame

    by_hex = did_entries_by_hex(did_map)
    if not by_hex:
        return long_frame

    has_session_path = "session_path" in long_frame.columns
    records: list[dict[str, Any]] = []
    for _, row in raw.iterrows():
        hit = decode_raw_did_reading(
            by_hex, str(row["channel"]), row.get("value_text"), source=str(row.get("source", ""))
        )
        if hit is None:
            continue
        entry, numeric = hit.entry, hit.value
        records.append(
            {
                "timestamp": row["timestamp"],
                "source": DID_SOURCE,
                "channel": entry.name,
                "value_number": numeric,
                "value_text": None,
                "value_bool": None,
                "unit": hit.unit,
                "payload": None,
                "metadata_json": None,
                "session": row.get("session"),
                "session_path": row.get("session_path") if has_session_path else None,
                "source_channel": f"{DID_SOURCE}:{entry.name}",
                "value": numeric,
            }
        )

    if not records:
        return long_frame

    decoded = pd.DataFrame(records)
    # Align columns with the input so concat doesn't introduce NaN-only extras.
    for col in long_frame.columns:
        if col not in decoded.columns:
            decoded[col] = None
    decoded = decoded[list(long_frame.columns)]
    combined = pd.concat([long_frame, decoded], ignore_index=True)
    return combined.sort_values(["timestamp", "source", "channel"], kind="stable").reset_index(
        drop=True
    )
