"""A vehicle's K-line facts, as data: addresses, wake-up, framing, timing.

Loaded from the active profile's ``kline.yaml`` (``daemon.kline_profile``,
scoped per vehicle by ``src.vehicle.profiles.scope_config``). Nothing here has a
vehicle-specific default: the next vehicle may be a 5-baud ISO 9141 ECU, and a
guessed tester address reads exactly like dead hardware (the Xterra ignores
``0xF1`` and answers ``0xFC``). An absent file means "K-line not configured",
said as such, not a fallback.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.collection.kline import protocol as kp


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Wakeup(_Strict):
    """One wake-up pattern: the line held low, then released, before the
    first StartCommunication byte. ``evidence`` says where it was measured."""

    name: str
    low_ms: float = Field(gt=0.0, le=2000.0)
    high_ms: float = Field(gt=0.0, le=2000.0)
    evidence: str = Field(min_length=1)


class Timing(_Strict):
    """ISO 14230 inter-message times, milliseconds."""

    #: Longest the ECU may take to start replying.
    p2_max_ms: float = Field(gt=0.0)
    #: Least the tester waits after a reply before its next request.
    p3_min_ms: float = Field(gt=0.0)
    #: Longest the tester may stay silent before the ECU drops the session.
    p3_max_ms: float = Field(gt=0.0)
    #: Gap between the tester's own request bytes.
    p4_ms: float = Field(ge=0.0)


class KnownAnswer(_Strict):
    """A read whose reply shape is known, checked before trusting anything new."""

    request: str  # hex payload, e.g. "22 11 03 04 01"
    reply_prefix: str  # hex, e.g. "62 11 03"
    reply_length: int = Field(gt=0)  # payload bytes
    evidence: str = Field(min_length=1)

    @field_validator("request", "reply_prefix")
    @classmethod
    def _hex(cls, value: str) -> str:
        bytes.fromhex(value)
        return value

    def request_bytes(self) -> bytes:
        return bytes.fromhex(self.request)

    def reply_prefix_bytes(self) -> bytes:
        return bytes.fromhex(self.reply_prefix)


#: A module's profile name: an identifier, never two hex digits (that is how an
#: *unnamed* address is written, so ``AB`` would read as address 0xAB).
_MODULE_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_HEX_ADDRESS = re.compile(r"^[0-9A-F]{2}$")
#: The module label of a frame seen before any StartCommunication (a capture
#: that attached mid-session): not guessed, said.
UNKNOWN_MODULE = "unknown"


def _check_identifier(value: str, what: str) -> str:
    if not _MODULE_NAME.fullmatch(value):
        raise ValueError(f"{what} {value!r} must be an upper-case identifier")
    return value


class Flag(_Strict):
    """One bit of a flag word, bound by an operator action seen to flip it."""

    name: str
    mask: int = Field(gt=0, le=0xFFFFFFFF)
    meaning: str = Field(min_length=1)
    status: Literal["confirmed", "provisional"]
    evidence: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _check_identifier(value, "flag name")

    @field_validator("mask")
    @classmethod
    def _one_bit(cls, value: int) -> int:
        if value & (value - 1):
            raise ValueError(f"flag mask 0x{value:x} must be a single bit")
        return value


class ModuleDid(_Strict):
    """A DID one module answers, with its reply width and, when a known answer
    backs it, a binding: a value (``raw * scale + offset``) or named flags.

    The width is what the module was seen to return; it is a fact without a
    meaning, which is why an unbound DID may still be listed and read. A DID
    whose meaning is still a hypothesis says so in ``note`` and has no name.
    """

    did: int = Field(ge=0, le=0xFFFF)
    width: int = Field(ge=1, le=4)
    evidence: str = Field(min_length=1)
    note: str | None = None
    name: str | None = None
    unit: str | None = None
    scale: float | None = None
    offset: float = 0.0
    signed: bool = False
    status: Literal["confirmed", "provisional"] | None = None
    known_answer: str | None = None
    flags: tuple[Flag, ...] = ()

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else _check_identifier(value, "DID name")

    @model_validator(mode="after")
    def _binding_is_whole(self) -> ModuleDid:
        value_fields = {
            "name": self.name,
            "unit": self.unit,
            "scale": self.scale,
            "status": self.status,
            "known_answer": self.known_answer,
        }
        given = {k for k, v in value_fields.items() if v is not None}
        if given and given != set(value_fields):
            missing = sorted(set(value_fields) - given)
            raise ValueError(
                f"DID 0x{self.did:04X}: a value binding needs every one of "
                f"{sorted(value_fields)}; missing {missing}"
            )
        if given and self.flags:
            raise ValueError(f"DID 0x{self.did:04X} is bound as a value and as flags")
        for flag in self.flags:
            if flag.mask >= 1 << (8 * self.width):
                raise ValueError(
                    f"DID 0x{self.did:04X}: flag {flag.name} mask 0x{flag.mask:x} is wider "
                    f"than its {self.width}-byte reply"
                )
        return self

    @property
    def bound(self) -> bool:
        return self.name is not None or bool(self.flags)

    def raw(self, value: bytes) -> int:
        if len(value) != self.width:
            raise ValueError(
                f"DID 0x{self.did:04X} is {self.width} byte(s); the reply carried {len(value)}"
            )
        return int.from_bytes(value, "big", signed=self.signed)

    def decode(self, value: bytes) -> dict[str, float | bool]:
        """Engineering values of this DID's bindings; empty when it has none."""
        raw = self.raw(value)
        if self.scale is not None and self.name is not None:
            return {self.name: raw * self.scale + self.offset}
        return {flag.name: bool(raw & flag.mask) for flag in self.flags}


class ModuleReader(_Strict):
    """How CANary may read a module itself (K01.5). Absent: it never does."""

    #: ``common_id``: one ``22 hh ll`` per DID (how the scan tool read ABS).
    #: ``local_id``: define one ``AC`` local id and poll ``21`` (the ECM).
    read_service: Literal["common_id", "local_id"]
    #: Longest ``21`` reply (data bytes after ``61 lid``) the module accepted;
    #: a longer definition is refused before it is sent. Required for
    #: ``local_id``: the ECM answered ``7F AC 12`` to one past its limit.
    max_local_id_reply_bytes: int | None = Field(default=None, ge=1, le=253)
    #: Send TesterPresent (``3E``) when the tester would otherwise be silent
    #: this long. ``None``: the module was never seen to answer ``3E``, so it
    #: is not sent, and polling alone must keep the session inside P3max.
    tester_present_seconds: float | None = Field(default=None, gt=0.0)
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _local_id_has_a_limit(self) -> ModuleReader:
        if self.read_service == "local_id" and self.max_local_id_reply_bytes is None:
            raise ValueError("a local_id reader needs max_local_id_reply_bytes")
        return self


class ClearCodes(_Strict):
    """How a module's stored codes are cleared: the request a scan tool was
    seen to send it. Absent: CANary never clears that module's codes."""

    #: The whole request, service byte first (``[0x14]`` for the airbag
    #: module: a bare ClearDiagnosticInformation, no group bytes).
    request: tuple[int, ...] = Field(min_length=1)
    evidence: str = Field(min_length=1)

    @field_validator("request")
    @classmethod
    def _is_a_clear(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value[0] != 0x14:
            raise ValueError(
                f"clear_codes request starts 0x{value[0]:02x}; ClearDiagnosticInformation is 0x14"
            )
        if any(not 0 <= b <= 0xFF for b in value):
            raise ValueError("clear_codes request bytes must be 0x00-0xFF")
        return value


CodeStatus = Literal["current", "past"]


class CodeRead(_Strict):
    """One request of a module's code read and which codes its reply holds."""

    #: The whole request, service byte first (``[0xA2, 0x01]``).
    request: tuple[int, ...] = Field(min_length=1)
    #: Which codes the reply lists: present now, or stored from before.
    status: CodeStatus
    #: ``confirmed``: a known answer showed it; ``inferred``: read off what a
    #: scan tool displayed beside one observation, and said so to the operator.
    status_confidence: Literal["confirmed", "inferred"]
    evidence: str = Field(min_length=1)

    @field_validator("request")
    @classmethod
    def _is_a_code_read(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(not 0 <= b <= 0xFF for b in value):
            raise ValueError("read_codes request bytes must be 0x00-0xFF")
        if value[0] not in kp.CODE_READ_SERVICES:
            known = ", ".join(f"0x{s:02x}" for s in sorted(kp.CODE_READ_SERVICES))
            raise ValueError(
                f"read_codes request starts 0x{value[0]:02x}; the code-read services whose "
                f"reply layout is known are {known}"
            )
        return value

    def request_bytes(self) -> bytes:
        return bytes(self.request)


class CodeReadKnownAnswer(_Strict):
    """A reply seen on the wire and the codes a scan tool displayed for it:
    the decode is checked against it when the profile loads."""

    #: The reply payload, service byte first (``"E2 01 90 23"``).
    reply: str
    codes: tuple[str, ...]
    evidence: str = Field(min_length=1)

    @field_validator("reply")
    @classmethod
    def _hex(cls, value: str) -> str:
        bytes.fromhex(value)
        return value


class ReadCodes(_Strict):
    """How a module's stored codes are read: the requests a scan tool was seen
    to send it, in order. Absent: CANary never reads that module's codes."""

    requests: tuple[CodeRead, ...] = Field(min_length=1)
    #: ``count_then_words``: ``<n> [hi lo]×n`` after the service byte.
    reply_format: Literal["count_then_words"]
    #: ``sae_j2012``: top two bits the letter (P C B U), then four hex digits.
    code_word: Literal["sae_j2012"]
    known_answer: CodeReadKnownAnswer
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> ReadCodes:
        requests = [r.request for r in self.requests]
        if len(set(requests)) != len(requests):
            raise ValueError("read_codes lists a request twice")
        statuses = [r.status for r in self.requests]
        if len(set(statuses)) != len(statuses):
            raise ValueError("read_codes gives two requests the same status")
        reply = bytes.fromhex(self.known_answer.reply)
        services = {r.request[0] for r in self.requests}
        if not reply or reply[0] - kp.POSITIVE_OFFSET not in services:
            raise ValueError(
                f"read_codes known answer {self.known_answer.reply!r} does not answer "
                "any of its requests"
            )
        decoded = decode_codes(reply[1:])
        if tuple(code for code, _word in decoded) != self.known_answer.codes:
            raise ValueError(
                f"read_codes known answer decodes to {[c for c, _ in decoded]}, "
                f"not {list(self.known_answer.codes)}"
            )
        return self


def decode_codes(params: bytes) -> tuple[tuple[str, int], ...]:
    """``(code, word)`` for each code word in a ``count_then_words`` reply's
    parameters (after its service byte), SAE J2012 letters."""
    return tuple((kp.decode_code_word(word), word) for word in kp.split_code_reply(params))


class KlineModule(_Strict):
    """One module on this vehicle's K-line, by its KWP2000 address."""

    name: str
    evidence: str = Field(min_length=1)
    #: The key bytes its StartCommunication reply carried.
    key_bytes: tuple[int, int] | None = None
    reader: ModuleReader | None = None
    read_codes: ReadCodes | None = None
    clear_codes: ClearCodes | None = None
    dids: tuple[ModuleDid, ...] = ()

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        _check_identifier(value, "module name")
        if _HEX_ADDRESS.fullmatch(value):
            raise ValueError(
                f"module name {value!r} reads as a hex address; unnamed addresses are "
                "written that way"
            )
        return value

    @model_validator(mode="after")
    def _unique(self) -> KlineModule:
        dids = [d.did for d in self.dids]
        if len(set(dids)) != len(dids):
            raise ValueError(f"module {self.name}: a DID is listed twice")
        names = [n for d in self.dids for n in ([d.name] if d.name else [f.name for f in d.flags])]
        if len(set(names)) != len(names):
            raise ValueError(f"module {self.name}: a binding name is used twice")
        return self

    def did(self, did: int) -> ModuleDid | None:
        return next((d for d in self.dids if d.did == did), None)

    def resolve(self, token: str) -> tuple[ModuleDid, str | None]:
        """A DID by hex (``1202``/``0x1202``), value name or flag name; the
        second item is the binding name the token asked for, if any."""
        wanted = token.strip().upper()
        hex_part = wanted.removeprefix("0X")
        if len(hex_part) == 4 and all(c in "0123456789ABCDEF" for c in hex_part):
            entry = self.did(int(hex_part, 16))
            if entry is None:
                raise LookupError(
                    f"{self.name} DID 0x{hex_part} has no recorded width in the K-line "
                    "profile; read it once (sniff a scan tool) to learn it"
                )
            return entry, None
        for entry in self.dids:
            if entry.name == wanted:
                return entry, wanted
            for flag in entry.flags:
                if flag.name == wanted:
                    return entry, wanted
        raise LookupError(f"{self.name} has no DID or binding named {token!r}")


class KlineProfile(_Strict):
    schema_version: Literal[1]
    baud: int = Field(gt=0, le=20_000)  # the TLIN1027 transmits up to 20 kbps
    ecm_address: int = Field(ge=0, le=0xFF)
    tester_address: int = Field(ge=0, le=0xFF)
    #: Tried in order until one gets a StartCommunication reply.
    wakeups: tuple[Wakeup, ...] = Field(min_length=1)
    key_bytes: tuple[int, int]
    #: How frames look after StartCommunication.
    frame_format: Literal["length_byte", "addressed"]
    #: Bytes appended to ``21``/``22`` requests. The Xterra's are UpRev's
    #: ``04 01``, whose meaning is unresolved (H05.9): data, not code, so a
    #: finding changes a file.
    request_trailer: tuple[int, ...] = ()
    timing: Timing
    known_answers: tuple[KnownAnswer, ...] = Field(min_length=1)
    evidence: str = Field(min_length=1)
    #: The modules seen on this K-line, by KWP2000 address. Names channels
    #: (``kline:ABS:1303``); an address with no entry is written in hex.
    modules: Mapping[int, KlineModule] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _distinct_addresses(self) -> KlineProfile:
        if self.ecm_address == self.tester_address:
            raise ValueError("ecm_address and tester_address must differ")
        for address in self.modules:
            if not 0 <= address <= 0xFF:
                raise ValueError(f"module address 0x{address:x} is not one byte")
        if self.tester_address in self.modules:
            raise ValueError("the tester address is not a module")
        names = [module.name for module in self.modules.values()]
        if len(set(names)) != len(names):
            raise ValueError("two modules share a name")
        return self

    def trailer(self) -> bytes:
        return bytes(self.request_trailer)

    def module_label(self, address: int | None) -> str:
        """The profile's name for ``address``; its hex when unnamed; and
        :data:`UNKNOWN_MODULE` when no StartCommunication said which it was."""
        if address is None:
            return UNKNOWN_MODULE
        module = self.modules.get(address)
        return module.name if module is not None else f"{address:02X}"

    def module_address(self, label: str) -> int:
        """Inverse of :meth:`module_label`: a profile name or two hex digits."""
        wanted = label.strip().upper()
        for address, module in self.modules.items():
            if module.name == wanted:
                return address
        if _HEX_ADDRESS.fullmatch(wanted):
            return int(wanted, 16)
        raise LookupError(f"no K-line module named {label!r} in this vehicle's profile")


class KlineNotConfiguredError(LookupError):
    """The active vehicle profile has no ``kline.yaml``."""


def load_kline_profile(path: Path) -> KlineProfile:
    if not path.exists():
        raise KlineNotConfiguredError(
            f"K-line not configured for this vehicle: {path} does not exist"
        )
    raw = yaml.safe_load(path.read_text())
    return KlineProfile.model_validate(raw)


__all__ = [
    "UNKNOWN_MODULE",
    "ClearCodes",
    "CodeRead",
    "CodeReadKnownAnswer",
    "CodeStatus",
    "Flag",
    "KlineModule",
    "KlineNotConfiguredError",
    "KlineProfile",
    "KnownAnswer",
    "ModuleDid",
    "ModuleReader",
    "ReadCodes",
    "Timing",
    "Wakeup",
    "decode_codes",
    "load_kline_profile",
]
