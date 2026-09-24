"""A vehicle's K-line facts, as data: addresses, wake-up, framing, timing.

Loaded from the active profile's ``kline.yaml`` (``daemon.kline_profile``,
scoped per vehicle by ``src.profiles.scope_config``). Nothing here has a
vehicle-specific default: the next vehicle may be a 5-baud ISO 9141 ECU, and a
guessed tester address reads exactly like dead hardware (the Xterra ignores
``0xF1`` and answers ``0xFC``). An absent file means "K-line not configured",
said as such, not a fallback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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

    @model_validator(mode="after")
    def _distinct_addresses(self) -> KlineProfile:
        if self.ecm_address == self.tester_address:
            raise ValueError("ecm_address and tester_address must differ")
        return self

    def trailer(self) -> bytes:
        return bytes(self.request_trailer)


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
    "KlineNotConfiguredError",
    "KlineProfile",
    "KnownAnswer",
    "Timing",
    "Wakeup",
    "load_kline_profile",
]
