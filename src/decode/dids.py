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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class Evidence(_Strict):
    """One piece of evidence backing a DID identification.

    Evidence that establishes a *number* must also say when the number holds.
    A calibration measured at steady state does not apply during a transient,
    and one measured in closed loop does not apply under enrichment — quoting
    a constant outside its conditions is the shape of every retracted finding
    in this project's history (AGENTS.md, "never compare aggregates"). So
    ``constant`` cannot be recorded without ``conditions``: the validator
    below makes the unsafe entry unwritable rather than merely discouraged.
    """

    session: str
    method: str
    finding: str
    #: The quantitative result, when this evidence establishes one.
    constant: float | None = None
    #: Units of ``constant``, e.g. ``"counts per gram of fuel"``.
    constant_unit: str | None = None
    #: The operating envelope the constant was measured in, and outside which
    #: it must not be quoted.
    conditions: str | None = None

    @model_validator(mode="after")
    def _constant_requires_conditions_and_unit(self) -> Evidence:
        if self.constant is None:
            return self
        missing = [
            name
            for name, value in (
                ("conditions", self.conditions),
                ("constant_unit", self.constant_unit),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"evidence carrying `constant` must also set {' and '.join(missing)} "
                "— a bare number invites being quoted against data it was never "
                "measured on"
            )
        return self


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


class DidEntry(_Strict):
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
    * ``linear_did`` — ``word * word_scale`` and ``did_raw * did_scale`` are
      the same quantity in engineering units, within ``tolerance`` (absolute)
      or ``relative_tolerance``. For a RAM value that is a differently-scaled
      or quantized view of a DID — a map-axis rpm trace in 50-rpm steps
      against a 12.5-rpm DID.
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
        return self


class MemoryChannel(_Strict):
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
        if (self.sentinel_raw is None) != (self.sentinel_meaning is None):
            raise ValueError(
                f"{self.name}: sentinel_raw and sentinel_meaning must be set together "
                "-- a sentinel without a stated meaning decodes as a measurement"
            )
        return self


def decode_memory_raw(channel: MemoryChannel, payload: bytes) -> int | None:
    """Raw integer for `channel` from a service-0x23 response payload.

    ``None`` when the payload is too short for the declared slice.
    """
    end = channel.byte_start + channel.byte_length
    if len(payload) < end:
        return None
    return int.from_bytes(payload[channel.byte_start : end], byteorder=channel.byte_order)


def apply_memory_scaling(channel: MemoryChannel, raw: int) -> float | None:
    """Apply `scale`/`offset`, or `None` for a sentinel or unpinned scale.

    A sentinel is not a small value — it is the absence of one. Returning
    ``None`` keeps it out of every mean, comparison and model input rather
    than contributing a zero.
    """
    if channel.sentinel_raw is not None and raw == channel.sentinel_raw:
        return None
    if channel.scale is None:
        return None
    offset = channel.offset if channel.offset is not None else 0.0
    return raw * channel.scale + offset


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
    start = offset + channel.byte_start
    end = start + channel.byte_length
    if len(payload) < end:
        return None
    return int.from_bytes(payload[start:end], byteorder=channel.byte_order)


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
        reference = did_raw * known.did_scale
        allowed = max(known.tolerance or 0.0, (known.relative_tolerance or 0.0) * abs(reference))
        return abs(word * known.word_scale - reference) <= allowed
    if known.relation == "reciprocal_did":
        assert known.constant is not None and known.tolerance is not None
        return word > 0 and abs(known.constant / word - did_raw) <= known.tolerance
    raise ValueError(f"unhandled known-answer relation {known.relation!r}")


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
        seen: dict[str, str] = {}
        for entry in self.dids:
            for name in (entry.name, *entry.aliases):
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
        return self

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
    """Load and validate dids.yaml. Raises ValidationError on schema mismatch."""
    raw = yaml.safe_load(path.read_text())
    return DidMap.model_validate(raw)


def decode_did_raw(entry: DidEntry, payload: bytes) -> int | None:
    """Extract the raw integer for `entry` from a Mode-22 response payload.

    `payload` is the data after the `62 HH LL` echo (what
    `CanObdClient.query_mode_22().payload` yields). Returns `None` when the
    payload is too short for the declared byte range.
    """
    end = entry.byte_start + entry.byte_length
    if len(payload) < end:
        return None
    return int.from_bytes(payload[entry.byte_start : end], byteorder=entry.byte_order)


def apply_scaling(entry: DidEntry, raw: int) -> float | None:
    """Apply `scale`/`offset`; `None` when scaling isn't pinned yet."""
    if entry.scale is None:
        return None
    offset = entry.offset if entry.offset is not None else 0.0
    return raw * entry.scale + offset


def is_plausible(entry: DidEntry, decoded: float) -> bool | None:
    """Whether `decoded` is within `entry`'s plausible range.

    `True`/`False` when at least one bound is set; `None` when neither is
    (no check). Mirrors `bindings.is_plausible` so the DID validator can
    flag misapplied scalings the same way the broadcast one does.
    """
    if entry.plausible_min is None and entry.plausible_max is None:
        return None
    if entry.plausible_min is not None and decoded < entry.plausible_min:
        return False
    if entry.plausible_max is not None and decoded > entry.plausible_max:
        return False
    return True


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
