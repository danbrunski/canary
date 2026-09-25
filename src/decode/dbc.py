"""Optional cantools DBC decode hook (Task 1.8).

The project's primary signal-decode path is ``data/bindings.yaml`` +
``src/decode/bindings.py`` — that's what carries confidence levels,
evidence trails, and plausibility bounds. This module is the optional
*reader* side: if someone hands you a community-built or factory DBC
for the same vehicle, drop it into ``data/dbc/`` and ``decode_can_rows_dbc``
will emit `source="dbc"` rows alongside the bindings-decoded ones, no
schema migration required (it just adds more named columns after ``to_wide``).

One-way: bindings.yaml stays source of truth; the DBC is consumed, never
written from here. The emitter side (4.D.2, ``tools/emit_dbc.py``) is
deferred as out-of-scope for v1.

Two failure modes worth distinguishing:

* DBC missing — caller's job (the preprocessing pipeline already handles
  "no bindings.yaml on disk" gracefully; we mirror that).
* DBC present but a payload doesn't match a known signal — silently
  skipped, like ``decode_can_rows`` does for unknown CAN IDs. cantools
  also raises on out-of-range / truncated payloads; we catch those so a
  single malformed frame doesn't kill an offline pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from src.can_ids import parse_can_channel
from src.paths import DBC_DIR
from src.telemetry import DBC_SOURCE, channel_key

log = logging.getLogger(__name__)

#: ``DBC_SOURCE`` (``"dbc"``) is the wide-column ``source`` prefix for
#: DBC-decoded values — re-exported from :mod:`src.telemetry` so ``to_wide``
#: produces ``dbc:<SIGNAL>`` without colliding with the bindings path's
#: ``decoded:<SIGNAL>``. Kept importable here for back-compat.
DEFAULT_DBC_DIR = DBC_DIR


def dbc_channel(signal_name: str) -> str:
    """Return the wide-column name a DBC-decoded signal occupies after ``to_wide``."""
    return channel_key(DBC_SOURCE, signal_name)


def load_dbc_database(path: Path) -> Any:
    """Load a single .dbc file.

    Returns a ``cantools.database.Database``. Raises ``FileNotFoundError``
    if the path doesn't exist (the caller should fall back to "no DBC")
    and ``cantools.database.errors.ParseError`` on malformed files.
    """
    import cantools  # local import keeps the import-time cost off the rest of the codebase

    if not path.exists():
        raise FileNotFoundError(f"DBC file not found: {path}")
    return cantools.database.load_file(str(path))


def load_dbc_dir(directory: Path = DEFAULT_DBC_DIR) -> Any | None:
    """Merge every ``.dbc`` under ``directory`` into one database, or ``None``.

    ``None`` when the directory is missing or holds no ``.dbc`` files — same
    "silent fallback to undecoded" contract ``decode_can_rows`` uses for a
    missing ``bindings.yaml``. Useful for the typical setup where you have
    one (or a few) hand-curated DBCs you want loaded together.
    """
    import cantools

    if not directory.exists():
        return None
    files = sorted(directory.glob("*.dbc"))
    if not files:
        return None
    database: Any = cantools.database.Database()
    for file in files:
        log.info("loading dbc file", extra={"path": str(file)})
        database.add_dbc_file(str(file))
    return database


def decode_can_rows_dbc(
    long_frame: pd.DataFrame,
    database: Any,
    *,
    source_label: str = DBC_SOURCE,
) -> pd.DataFrame:
    """Append DBC-decoded signal rows to a long-format telemetry frame.

    For every CAN row whose arbitration ID is a known DBC message, emit one
    new row per signal in that message (``source=source_label``,
    ``channel=signal_name``, ``value=numeric decoded value``). Unknown CAN
    IDs and rows whose payload is too short are silently skipped — same
    contract ``decode_can_rows`` uses for bindings.

    Pass-through: unknown frames stay in ``long_frame`` exactly as they
    arrived, since this function *appends* and doesn't mutate the input.
    """
    if long_frame.empty or "source" not in long_frame.columns:
        return long_frame

    can_rows = long_frame[long_frame["source"] == "can"]
    if can_rows.empty:
        return long_frame

    # Pre-build the "is this ID in the DBC?" lookup so groupby is cheap.
    known_ids: set[int] = {msg.frame_id for msg in database.messages}

    decoded_records: list[dict[str, Any]] = []
    for channel, group in can_rows.groupby("channel", sort=False):
        can_id = parse_can_channel(str(channel))
        if can_id is None or can_id not in known_ids:
            continue
        try:
            message = database.get_message_by_frame_id(can_id)
        except KeyError:
            continue
        units = {sig.name: (sig.unit or "") for sig in message.signals}
        for _, row in group.iterrows():
            payload = row.get("payload")
            if not isinstance(payload, (bytes, bytearray)):
                continue
            try:
                decoded = message.decode(bytes(payload), decode_choices=False)
            except Exception as exc:  # noqa: BLE001 — cantools raises a family of errors
                log.debug(
                    "dbc decode failed for one frame; skipping",
                    extra={"can_id": can_id, "error": str(exc)},
                )
                continue
            for signal_name, value in decoded.items():
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue  # named choice / enum signals — skip in numeric path
                decoded_records.append(
                    {
                        "timestamp": row["timestamp"],
                        "source": source_label,
                        "channel": signal_name,
                        "value_number": numeric,
                        "value_text": None,
                        "value_bool": None,
                        "unit": units.get(signal_name, ""),
                        "payload": None,
                        "metadata_json": None,
                        "session": row.get("session"),
                        "session_path": row.get("session_path"),
                        "source_channel": channel_key(source_label, signal_name),
                        "value": numeric,
                    }
                )

    if not decoded_records:
        return long_frame
    decoded_frame = pd.DataFrame.from_records(decoded_records)
    return (
        pd.concat([long_frame, decoded_frame], ignore_index=True)
        .sort_values(["timestamp", "source", "channel"], kind="stable")
        .reset_index(drop=True)
    )


__all__ = [
    "DBC_SOURCE",
    "DEFAULT_DBC_DIR",
    "dbc_channel",
    "decode_can_rows_dbc",
    "load_dbc_database",
    "load_dbc_dir",
]
