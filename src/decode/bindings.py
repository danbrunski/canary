"""Signal bindings loader + decoder.

Reads `data/bindings.yaml` — the project's canonical record of "byte position
X in CAN ID Y means Z" — into typed Signal objects, then provides the two
functions the daemon (and analyzers) need to actually use them at runtime:

  * `decode_raw(signal, data)` — pull the raw integer out of a frame
  * `apply_scaling(signal, raw)` — turn it into an engineering value

The two are separate because some signals (motion candidates, hypothesized
entries) have no scaling yet; the raw extraction still works, the scaling
returns None until we pin scale/offset against ground truth.

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
    """One piece of evidence backing a signal binding."""

    session: str
    method: str
    finding: str


class Signal(_Strict):
    """A signal binding: where in a CAN frame the value lives + how to decode it.

    `byte_order` is "big" for MSB-first (the OBD-II/UDS convention used across
    most of Nissan's broadcast frames we've seen so far). Set explicitly to
    "little" if a signal turns out to be little-endian.

    `scale` and `offset` are `None` for signals where we know the byte
    position but haven't yet pinned the engineering scaling — `apply_scaling`
    returns None in that case. Promoting a signal to known scale/offset is
    typically done after ELM327 Mode 01 cross-reference.
    """

    name: str
    module: str
    can_id: int = Field(ge=0, le=0x1FFFFFFF)  # accommodates 29-bit extended IDs
    byte_start: int = Field(ge=0, le=63)
    byte_length: int = Field(ge=1, le=8)
    byte_order: Literal["big", "little"] = "big"
    scale: float | None = None
    offset: float | None = None
    unit: str = "unknown"
    confidence: Literal["confirmed", "high", "medium", "low", "hypothesized"]
    evidence: list[Evidence] = Field(default_factory=list)
    notes: str | None = None


class Bindings(_Strict):
    """Top-level bindings.yaml document."""

    schema_version: int
    vehicle: dict[str, Any] = Field(default_factory=dict)
    bus: dict[str, Any] | None = None
    signals: list[Signal]


def load_bindings(path: Path) -> Bindings:
    """Load and validate a bindings.yaml file. Raises ValidationError on schema mismatch."""
    raw = yaml.safe_load(path.read_text())
    return Bindings.model_validate(raw)


def decode_raw(signal: Signal, data: bytes) -> int | None:
    """Extract the raw integer value of `signal` from one frame's data bytes.

    Returns `None` when the frame is too short to contain the signal — that
    happens when an ID's DLC varies across frames, or when a signal's byte
    range overshoots the canonical DLC for its CAN ID.
    """
    end = signal.byte_start + signal.byte_length
    if len(data) < end:
        return None
    chunk = data[signal.byte_start:end]
    return int.from_bytes(chunk, byteorder=signal.byte_order)


def apply_scaling(signal: Signal, raw: int) -> float | None:
    """Apply `scale` and `offset` to a raw value.

    Returns `None` when `scale` is unset — the binding's scaling hasn't been
    pinned to ground truth yet, so we can't emit an engineering value. The
    raw extraction is still useful for variability tracking, just not for
    "this signal reads 23.5 °C".
    """
    if signal.scale is None:
        return None
    offset = signal.offset if signal.offset is not None else 0.0
    return raw * signal.scale + offset
