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
from collections.abc import Mapping
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
    #: Former names this DID has carried. Capture profiles reference DIDs BY
    #: NAME and `parse_did_token` raises on an unknown one, so a rename would
    #: otherwise break every saved profile still using the old name —
    #: including vehicle-scoped profiles on the Pi that this repo cannot see.
    #: Renaming an entry means moving its old name here, not deleting it.
    aliases: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    #: Readings of this DID that were CONSIDERED AND REJECTED, with why.
    #: A plausible-but-wrong identification gets re-derived by the next person
    #: unless the refutation is recorded as data — the same reason
    #: bindings.yaml carries `absent_signals`. Same shape as `evidence`; the
    #: decoder ignores both.
    evidence_against: list[Evidence] = Field(default_factory=list)
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


def decode_raw_did_reading(
    by_hex: Mapping[str, DidEntry], channel: str, payload_hex: object
) -> DecodedDid | None:
    """Decode one raw ``DID_<hex>`` reading, or ``None`` if it isn't one.

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
    if not channel.startswith(_RAW_DID_CHANNEL_PREFIX):
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
    ``did:ECM_INJECTOR_PW_B1`` numeric columns (recipes target them, IF/LSTM
    learn their residual distribution, the gauge shows them in engineering
    units).

    When a DID's ``scale`` isn't pinned in ``dids.yaml`` (e.g.
    ``ECM_INJECTOR_PW_B1`` whose constant is still outstanding), the **raw
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
    if raw.empty:
        return long_frame

    by_hex = did_entries_by_hex(did_map)
    if not by_hex:
        return long_frame

    has_session_path = "session_path" in long_frame.columns
    records: list[dict[str, Any]] = []
    for _, row in raw.iterrows():
        hit = decode_raw_did_reading(by_hex, str(row["channel"]), row.get("value_text"))
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
