"""Replay a recorded session as a live producer (simulation backend).

Lets the daemon run with no hardware by re-emitting a previously captured
session's telemetry onto the same ``asyncio.Queue`` the real CAN/OBD
collectors feed. Downstream is identical to a real drive: the drain task
writes Parquet (raw frames, never ``decoded`` rows: P02.8), the live source
decodes raw CAN via ``bindings.yaml`` into ``decoded:*`` channels, and
everything fans out to ``/ws/telemetry`` — so the dashboard shows real,
previously-collected signals moving.

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

**Only captured sources are re-emitted** (D02.9,
``data/analyses/2026-09-17-d029-replay-amplification.md``). The stored
``decoded``/``derived`` rows (:data:`src.telemetry.RE_DERIVED_SOURCES`) are
regenerated downstream: ``decoded`` by the live source from the raw ``can``
frames (and, for anything reading the replay's output, by the loader's decode
on read), ``derived`` by the inference tick. Re-emitting them left the
detectors reading whichever ``decoded:<channel>`` copy landed last — the
stored one or the one freshly decoded from the frame, which differ exactly
when ``bindings.yaml`` changed since the recording. Until P02.8 (2026-09-26)
it also wrote every decoded signal to Parquet twice (measured 1.985x); no
``decoded`` row is stored now, whatever reaches the storage sink.

The one session shape where skipping would lose data is a recording from
before raw-payload persistence landed (2026-07-29): its ``can`` rows carry no
payload, so nothing downstream can re-derive anything. That is decided once
per session by :func:`probe_rederivability` before the first row is emitted,
reported in the log and in ``GET /control/replay``
(``rederivable``/``rederivable_reason``), and never switched silently. Such a
replay emits its stored ``decoded`` rows to the live pipeline, and its output
still stores none (P02.8); the source keeps its own. No session on the laptop
has this shape (every one probed 2026-09-26 carries payloads).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd

from src.collection.collectors import CollectorStarter
from src.collection.reconnect import wait_for_stop_or_timeout
from src.config import CanaryConfig
from src.storage.broadcast_storage import UnfaithfulReplayError, refuse_unfaithful_replay
from src.storage.session_files import PARQUET_PATTERN, count_session_rows, session_time_span_seconds
from src.telemetry import (
    CAN_SOURCE,
    RE_DERIVED_SOURCES,
    MetadataValue,
    TelemetryRecord,
    TelemetrySource,
    TelemetryValue,
)

log = logging.getLogger(__name__)

#: Don't sleep longer than this between rows even if the recording paused
#: (USB drop, ignition-off gap) — keeps the replayed dashboard moving.
_MAX_GAP_SECONDS = 0.5

#: Leading chunks sampled by :func:`probe_rederivability`. A chunk holds tens
#: of thousands of rows, so one is already decisive for any session with CAN
#: traffic; a few cover a session whose first chunk is OBD/GPS-only without
#: reading the whole recording.
_PAYLOAD_PROBE_CHUNKS = 3


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


@dataclass(frozen=True)
class Rederivability:
    """Whether a source session's raw rows can regenerate its re-derived ones.

    Carries the skip set rather than letting callers assemble one, so the only
    way to obtain "which sources may replay drop" is to have asked this
    question about this session first.
    """

    rederivable: bool
    reason: str
    chunks_sampled: int
    can_rows_sampled: int

    @property
    def skip_sources(self) -> frozenset[TelemetrySource]:
        """Sources a replay of this session must not re-emit."""
        return RE_DERIVED_SOURCES if self.rederivable else frozenset()


def _has_can_payload(df: pd.DataFrame) -> tuple[int, bool]:
    """``(can_row_count, any_payload)`` for one chunk's ``source``/``payload``."""
    if "source" not in df.columns:
        return 0, False
    can = df[df["source"].astype(str) == CAN_SOURCE]
    if can.empty:
        return 0, False
    if "payload" not in can.columns:
        return len(can), False
    return len(can), any(_payload_present(p) for p in can["payload"])


def _payload_present(value: Any) -> bool:
    """A usable raw frame: bytes with at least one byte in them.

    ``FrameDecoder.decode`` / ``LiveFeatureSource.decode_for_telemetry`` both
    return nothing for an empty payload, so ``b""`` is not re-derivable.
    """
    return isinstance(value, (bytes, bytearray)) and len(value) > 0


def probe_rederivability(
    session_dir: Path, *, chunk_limit: int = _PAYLOAD_PROBE_CHUNKS
) -> Rederivability:
    """Decide, once per session, whether replay may skip re-derived sources.

    Reads only the ``source``/``payload`` columns of the first few chunks
    (footer-projected, never the whole session). ``rederivable`` is True only
    when raw CAN payloads are actually present, because that is the input the
    storage decoder and the live feature source both re-derive ``decoded:*``
    from; ``derived:*`` rides the same decision since the inference tick needs
    those same features. When it is False the stored rows are the only copy
    that exists and replay keeps emitting them.
    """
    chunks = sorted(session_dir.glob(PARQUET_PATTERN))
    if not chunks:
        return Rederivability(
            rederivable=False,
            reason=f"no {PARQUET_PATTERN} chunks in the source session",
            chunks_sampled=0,
            can_rows_sampled=0,
        )
    sampled = 0
    can_rows = 0
    for chunk in chunks[:chunk_limit]:
        try:
            df = pd.read_parquet(chunk, columns=["source", "payload"])
        except (OSError, ValueError):
            # Missing column (pre-payload schema) or an unreadable tail chunk:
            # fall back to the full read and let _has_can_payload judge it.
            try:
                df = pd.read_parquet(chunk)
            except (OSError, ValueError):
                continue
        sampled += 1
        chunk_can_rows, has_payload = _has_can_payload(df)
        can_rows += chunk_can_rows
        if has_payload:
            return Rederivability(
                rederivable=True,
                reason=(
                    f"can rows carry raw payloads (sampled {sampled} chunk(s), "
                    f"{can_rows} can rows); decoded/derived are re-derived downstream"
                ),
                chunks_sampled=sampled,
                can_rows_sampled=can_rows,
            )
    if can_rows == 0:
        return Rederivability(
            rederivable=False,
            reason=(
                f"no can rows in the first {sampled} chunk(s); nothing downstream "
                "can re-derive decoded/derived rows"
            ),
            chunks_sampled=sampled,
            can_rows_sampled=0,
        )
    return Rederivability(
        rederivable=False,
        reason=(
            f"can rows carry no payload ({can_rows} sampled over {sampled} chunk(s)); "
            "source predates raw-payload persistence (2026-07-29), so stored "
            "decoded/derived rows are the only copy"
        ),
        chunks_sampled=sampled,
        can_rows_sampled=can_rows,
    )


@dataclass
class ReplayEmissionStats:
    """What a replay actually put on the queue, counted as it goes (D02.9).

    Mutated in place by :func:`replay_session` so ``GET /control/replay`` can
    report real numbers. The absence of exactly these counters is what let
    D02.9 infer a row count from chunk counts and land on a wrong premise, so
    :func:`replay_session` requires one rather than defaulting it away.
    """

    #: Rows in the source session (Parquet footers, no data read); ``None``
    #: when nothing was readable — "unknown", not zero.
    source_rows_total: int | None = None
    #: Rows handed to the queue (cumulative across loop passes).
    rows_emitted: int = 0
    #: Rows filtered at the emit path because they are re-derived downstream.
    rows_skipped: int = 0
    #: Rows dropped because the drain was behind (queue full) — never silent.
    rows_dropped: int = 0
    #: Completed passes over the source (``loop=True`` re-emits).
    passes: int = 0
    rederivable: bool | None = None
    rederivable_reason: str | None = None
    skipped_sources: tuple[str, ...] = field(default_factory=tuple)
    #: Recorded span of the source, first to last stored timestamp (Parquet
    #: footers only); ``None`` when unknown. The denominator for progress.
    source_span_s: float | None = None
    #: Recording time covered by the current pass so far: the latest emitted
    #: row's original timestamp minus the pass's first. Resets per pass.
    source_time_emitted_s: float = 0.0
    #: ``time.monotonic()`` when the current pass started; ``None`` before the
    #: first row. Wall elapsed is computed from it at status time.
    pass_started_monotonic: float | None = None

    def wall_elapsed_s(self, now: float | None = None) -> float | None:
        """Wall-clock seconds the current pass has been running."""
        if self.pass_started_monotonic is None:
            return None
        return max(0.0, (time.monotonic() if now is None else now) - self.pass_started_monotonic)

    def effective_speed(self, now: float | None = None) -> float | None:
        """Recording seconds replayed per wall second in the current pass.

        The requested ``speed`` is a multiplier on the *sleeps*, and every
        sleep is clamped at ``_MAX_GAP_SECONDS``, so a recording with pauses
        replays faster than requested — 18.9x on one real session, at a
        reported ``speed: 1.0`` (D02.9). This is the number that was missing.
        ``None`` until at least a second of wall time has passed, because a
        ratio over milliseconds is noise dressed as a speed.
        """
        elapsed = self.wall_elapsed_s(now)
        if elapsed is None or elapsed < 1.0:
            return None
        return self.source_time_emitted_s / elapsed

    def source_time_progress(self) -> float | None:
        """Fraction of the source's recorded span covered by the current pass."""
        if self.source_span_s is None or self.source_span_s <= 0:
            return None
        return min(1.0, self.source_time_emitted_s / self.source_span_s)

    def pass_reconciled(self) -> bool | None:
        """After ≥1 complete pass: does emitted + skipped + dropped equal
        ``passes × source_rows_total``? ``None`` until a pass has completed or
        while the source row count is unknown. A ``False`` here is a real
        accounting gap, not a wall-time-versus-rows inference.
        """
        if self.passes < 1 or self.source_rows_total is None:
            return None
        return (
            self.rows_emitted + self.rows_skipped + self.rows_dropped
            == self.passes * self.source_rows_total
        )


def _iter_rows(
    session_dir: Path,
    *,
    skip_sources: frozenset[TelemetrySource],
    stats: ReplayEmissionStats | None = None,
) -> Iterator[tuple[float, dict[str, object]]]:
    """Yield ``(original_epoch, record-fields)`` in timestamp order, chunk by
    chunk (bounded memory). The record is built at emit time so its
    timestamp is wall-clock-fresh.

    ``skip_sources`` is required (and normally comes from
    :attr:`Rederivability.skip_sources`) so a caller cannot re-emit rows the
    pipeline re-derives without having decided that it is safe to.
    """
    for chunk in sorted(session_dir.glob(PARQUET_PATTERN)):
        try:
            df = pd.read_parquet(chunk)
        except (OSError, ValueError):
            continue
        if df.empty:
            continue
        if skip_sources:
            keep = ~df["source"].astype(str).isin(skip_sources)
            if stats is not None:
                stats.rows_skipped += int((~keep).sum())
            df = df[keep]
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
    stats: ReplayEmissionStats,
    broadcast_stale_timeout_seconds: float,
) -> None:
    """Replay ``session_dir`` onto ``queue`` until ``stop_event`` is set.

    Best-effort: a missing/empty session logs and idles rather than
    crashing the daemon (the rest of the dashboard still works).

    ``stats`` is required and mutated in place: every replay reports how many
    rows it read, emitted, skipped and dropped (D02.9).

    ``broadcast_stale_timeout_seconds`` is the daemon's
    ``collection.broadcast_stale_timeout_seconds``, required so no replay path
    can skip P02.10's refusals: a change-only session replayed slower than
    real time, or recorded with a longer timeout than this, raises
    :class:`~src.storage.broadcast_storage.UnfaithfulReplayError` before a
    row is emitted.
    """
    if not session_dir.exists():
        log.warning("replay session not found: %s", session_dir)
        await stop_event.wait()
        return
    speed = speed if speed > 0 else 1.0
    # P02.10: slower than real time, or recorded with a longer stale timeout
    # than the daemon's, a change-only session's re-writes arrive past the
    # timeout the live pipeline holds a value for. Raised before a row.
    refuse_unfaithful_replay(
        session_dir, speed, stale_timeout_seconds=broadcast_stale_timeout_seconds
    )

    # Decide once, before the first row: per-row guessing is what would let a
    # pre-2026-07-29 session silently lose its only copy of decoded signals.
    probe = probe_rederivability(session_dir)
    stats.rederivable = probe.rederivable
    stats.rederivable_reason = probe.reason
    stats.skipped_sources = tuple(sorted(probe.skip_sources))
    stats.source_rows_total = count_session_rows(session_dir)
    stats.source_span_s = session_time_span_seconds(session_dir)
    log.info(
        "replay source probed",
        extra={
            "session": session_dir.name,
            "rederivable": probe.rederivable,
            "reason": probe.reason,
            "skipped_sources": ",".join(stats.skipped_sources),
            "source_rows": stats.source_rows_total,
            "source_span_s": stats.source_span_s,
        },
    )

    while not stop_event.is_set():
        emitted = 0
        first_orig: float | None = None
        wall_start = time.monotonic()
        stats.pass_started_monotonic = wall_start
        stats.source_time_emitted_s = 0.0
        for orig_epoch, fields in _iter_rows(
            session_dir, skip_sources=probe.skip_sources, stats=stats
        ):
            if stop_event.is_set():
                return
            if first_orig is None:
                first_orig = orig_epoch
            stats.source_time_emitted_s = max(0.0, orig_epoch - first_orig)
            # Drift-corrected pacing: the i-th row is due at
            # wall_start + (recording-elapsed / speed), clamped per-row so a
            # capture pause can't stall the stream.
            target = wall_start + (orig_epoch - first_orig) / speed
            delay = min(_MAX_GAP_SECONDS, max(0.0, target - time.monotonic()))
            if delay > 0 and await wait_for_stop_or_timeout(stop_event, delay):
                return  # stop fired during the sleep
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
                stats.rows_emitted += 1
            except asyncio.QueueFull:
                # Drain is behind; drop rather than block the replay clock —
                # same "freshest wins" policy the broadcaster uses. Counted,
                # so "fewer rows out than in" is never a mystery.
                stats.rows_dropped += 1
        stats.passes += 1
        log.info(
            "replay pass complete",
            extra={
                "session": session_dir.name,
                "rows": emitted,
                "skipped": stats.rows_skipped,
                "dropped": stats.rows_dropped,
                "source_rows": stats.source_rows_total,
                "pass": stats.passes,
                "reconciled": stats.pass_reconciled(),
                "source_time_s": round(stats.source_time_emitted_s, 3),
                "wall_s": round(time.monotonic() - wall_start, 3),
                "effective_speed": stats.effective_speed(),
                "requested_speed": speed,
            },
        )
        if not loop or emitted == 0:
            await stop_event.wait()
            return


def make_replay_collector(config: CanaryConfig) -> CollectorStarter:
    """Return a ``starter(queue, stop_event)`` matching ``_default_collectors``
    so ``run_daemon`` swaps real collectors for replay with no other wiring."""
    session = Path(config.collection.replay_session or "")
    speed = config.collection.replay_speed
    looping = config.collection.replay_loop
    stale_timeout = config.collection.broadcast_stale_timeout_seconds

    async def starter(queue: asyncio.Queue[TelemetryRecord], stop_event: asyncio.Event) -> None:
        log.info(
            "replay collector starting",
            extra={"session": str(session), "speed": speed, "loop": looping},
        )
        # The YAML-knob path has no control-plane status to publish into, so
        # the counters live and die with the log lines replay_session writes.
        try:
            await replay_session(
                queue,
                session,
                stop_event,
                speed=speed,
                loop=looping,
                stats=ReplayEmissionStats(),
                broadcast_stale_timeout_seconds=stale_timeout,
            )
        except UnfaithfulReplayError:
            # Refused by name, like a missing session: log and idle.
            log.exception("replay refused")
            await stop_event.wait()

    return starter


__all__ = [
    "ReplayEmissionStats",
    "Rederivability",
    "make_replay_collector",
    "probe_rederivability",
    "rebase_metadata",
    "replay_session",
]
