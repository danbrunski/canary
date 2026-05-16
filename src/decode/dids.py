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

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Strict):
    """One piece of evidence backing a DID identification."""

    session: str
    method: str
    finding: str


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
    keepalive_seconds: float | None = None
    notes: str | None = None


class DidEntry(_Strict):
    """An identified Mode 22 DID: where its value sits in the response
    payload + how to decode it.

    `scale`/`offset` are `None` for DIDs whose byte position is known but
    whose engineering scaling isn't pinned to ground truth yet (e.g. the
    injector-PW scale constant still needs a steady-state multi-point).
    """

    did: int = Field(ge=0, le=0xFFFF)
    name: str
    byte_start: int = Field(ge=0, le=63)
    byte_length: int = Field(ge=1, le=8)
    byte_order: Literal["big", "little"] = "big"
    scale: float | None = None
    offset: float | None = None
    unit: str = "unknown"
    confidence: Literal["confirmed", "high", "medium", "low", "hypothesized"]
    plausible_min: float | None = None
    plausible_max: float | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    notes: str | None = None


class DidMap(_Strict):
    """Top-level dids.yaml document."""

    schema_version: int
    vehicle: dict[str, Any] = Field(default_factory=dict)
    access: DidAccess
    dids: list[DidEntry]


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
