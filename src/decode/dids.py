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
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field

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

    by_hex = {f"{entry.did:04X}": entry for entry in did_map.dids}
    if not by_hex:
        return long_frame

    has_session_path = "session_path" in long_frame.columns
    records: list[dict[str, Any]] = []
    for _, row in raw.iterrows():
        channel = str(row["channel"])
        hex_id = channel[len(_RAW_DID_CHANNEL_PREFIX) :].upper()
        entry = by_hex.get(hex_id)
        if entry is None:
            continue
        payload_hex = row.get("value_text")
        if not isinstance(payload_hex, str) or not payload_hex:
            continue
        try:
            payload = bytes.fromhex(payload_hex)
        except ValueError:
            continue
        raw_int = decode_did_raw(entry, payload)
        if raw_int is None:
            continue
        scaled = apply_scaling(entry, raw_int)
        # Fall back to the raw count when the scale constant isn't pinned.
        numeric = float(scaled) if scaled is not None else float(raw_int)
        records.append(
            {
                "timestamp": row["timestamp"],
                "source": DID_SOURCE,
                "channel": entry.name,
                "value_number": numeric,
                "value_text": None,
                "value_bool": None,
                "unit": entry.unit if scaled is not None else "raw",
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
