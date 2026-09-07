"""Replay a recorded session as a live producer (simulation backend).

Lets the daemon run with no hardware by re-emitting a previously captured
session's telemetry onto the same ``asyncio.Queue`` the real CAN/OBD
collectors feed. Downstream is identical to a real drive: the drain task
writes Parquet, the live source decodes raw CAN via ``bindings.yaml`` into
``decoded:*`` channels, and everything fans out to ``/ws/telemetry`` — so
the dashboard shows real, previously-collected signals moving.

Pacing reproduces the recording's own inter-row timing (scaled by
``replay_speed``), drift-corrected so it can't run away; a long capture
gap is clamped so the stream stays lively, and it loops when the session
is exhausted. Timestamps are rebased to wall-clock at emit time so
data-freshness reads "now". Chunk files are read one at a time so a
multi-hundred-MB session replays in bounded memory.

``metadata_json`` rides along (C02.6). The acquisition clock inside it,
``acquired_at``, is shifted by the same offset as the row's timestamp so the
replayed row stays internally consistent (presentation stamp and acquisition
instant keep their original separation); ``generation`` / ``round_id`` are
carried untouched, since they identify the *source* capture's rounds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd

from src.collection.collectors import CollectorStarter
from src.config import CanaryConfig
from src.preprocessing.loader import PARQUET_PATTERN
from src.telemetry import MetadataValue, TelemetryRecord, TelemetrySource, TelemetryValue

log = logging.getLogger(__name__)

#: Don't sleep longer than this between rows even if the recording paused
#: (USB drop, ignition-off gap) — keeps the replayed dashboard moving.
_MAX_GAP_SECONDS = 0.5


def _present(value: Any) -> bool:
    """False for ``None``, ``NaN`` and pandas ``NA`` alike.

    Nullable Parquet columns come back as ``pd.NA`` rather than ``float('nan')``;
    the old ``isinstance(x, float)`` guard let ``NA`` through to ``bool(NA)``,
    which raises ("boolean value of NA is ambiguous") on any session whose
    ``value_bool`` column is nullable — the ground-truth drive slice included.
    """
    if value is None:
        return False
    return not (pd.api.types.is_scalar(value) and bool(pd.isna(value)))


def _value(row: pd.Series) -> float | str | bool | None:
    num = row.get("value_number")
    if _present(num):
        return float(num)  # type: ignore[arg-type]
    text = row.get("value_text")
    if _present(text):
        return str(text)
    b = row.get("value_bool")
    if _present(b):
        return bool(b)
    return None


def _payload(row: pd.Series) -> bytes | None:
    p = row.get("payload")
    if isinstance(p, (bytes, bytearray)):
        return bytes(p)
    return None


def _metadata(row: pd.Series) -> dict[str, MetadataValue]:
    raw = row.get("metadata_json")
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def rebase_metadata(metadata: dict[str, MetadataValue], shift_s: float) -> dict[str, MetadataValue]:
    """Shift ``acquired_at`` by ``shift_s`` (the row's own timestamp rebase)."""
    acquired = metadata.get("acquired_at")
    if not isinstance(acquired, (int, float)) or isinstance(acquired, bool):
        return metadata
    return {**metadata, "acquired_at": float(acquired) + shift_s}


def _iter_rows(session_dir: Path) -> Iterator[tuple[float, dict[str, object]]]:
    """Yield ``(original_epoch, record-fields)`` in timestamp order, chunk by
    chunk (bounded memory). The record is built at emit time so its
    timestamp is wall-clock-fresh."""
    for chunk in sorted(session_dir.glob(PARQUET_PATTERN)):
        try:
            df = pd.read_parquet(chunk)
        except (OSError, ValueError):
            continue
        if df.empty:
            continue
        df = df.sort_values("timestamp", kind="stable")
        ts = pd.to_datetime(df["timestamp"], utc=True)
        for (_, row), t in zip(df.iterrows(), ts, strict=True):
            unit = row.get("unit")
            yield (
                float(t.timestamp()),
                {
                    "source": str(row["source"]),
                    "channel": str(row["channel"]),
                    "value": _value(row),
                    "unit": "" if unit is None or pd.isna(unit) else str(unit),
                    "payload": _payload(row),
                    "metadata": _metadata(row),
                },
            )


async def replay_session(
    queue: asyncio.Queue[TelemetryRecord],
    session_dir: Path,
    stop_event: asyncio.Event,
    *,
    speed: float = 1.0,
    loop: bool = True,
) -> None:
    """Replay ``session_dir`` onto ``queue`` until ``stop_event`` is set.

    Best-effort: a missing/empty session logs and idles rather than
    crashing the daemon (the rest of the dashboard still works).
    """
    if not session_dir.exists():
        log.warning("replay session not found: %s", session_dir)
        await stop_event.wait()
        return
    speed = speed if speed > 0 else 1.0

    while not stop_event.is_set():
        emitted = 0
        first_orig: float | None = None
        wall_start = time.monotonic()
        for orig_epoch, fields in _iter_rows(session_dir):
            if stop_event.is_set():
                return
            if first_orig is None:
                first_orig = orig_epoch
            # Drift-corrected pacing: the i-th row is due at
            # wall_start + (recording-elapsed / speed), clamped per-row so a
            # capture pause can't stall the stream.
            target = wall_start + (orig_epoch - first_orig) / speed
            delay = min(_MAX_GAP_SECONDS, max(0.0, target - time.monotonic()))
            if delay > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    return  # stop fired during the sleep
                except TimeoutError:
                    pass
            emitted_at = time.time()
            record = TelemetryRecord(
                timestamp=datetime.fromtimestamp(emitted_at, tz=UTC),
                source=cast(TelemetrySource, fields["source"]),
                channel=str(fields["channel"]),
                value=cast(TelemetryValue, fields["value"]),
                unit=str(fields["unit"]),
                payload=cast("bytes | None", fields["payload"]),
                metadata=rebase_metadata(
                    cast("dict[str, MetadataValue]", fields["metadata"]),
                    emitted_at - orig_epoch,
                ),
            )
            try:
                queue.put_nowait(record)
                emitted += 1
            except asyncio.QueueFull:
                # Drain is behind; drop rather than block the replay clock —
                # same "freshest wins" policy the broadcaster uses.
                pass
        log.info("replay pass complete", extra={"session": session_dir.name, "rows": emitted})
        if not loop or emitted == 0:
            await stop_event.wait()
            return


def make_replay_collector(config: CanaryConfig) -> CollectorStarter:
    """Return a ``starter(queue, stop_event)`` matching ``_default_collectors``
    so ``run_daemon`` swaps real collectors for replay with no other wiring."""
    session = Path(config.collection.replay_session or "")
    speed = config.collection.replay_speed
    looping = config.collection.replay_loop

    async def starter(queue: asyncio.Queue[TelemetryRecord], stop_event: asyncio.Event) -> None:
        log.info(
            "replay collector starting",
            extra={"session": str(session), "speed": speed, "loop": looping},
        )
        await replay_session(queue, session, stop_event, speed=speed, loop=looping)

    return starter
