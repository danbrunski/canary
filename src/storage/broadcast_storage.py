"""Change-only storage of broadcast CAN frames (P02.10, operator 2026-09-26).

Most of what the recorder stored repeated itself: on the 2026-09-20 drive
41 % of CAN frames carried exactly their id's previous payload, and six ids
never changed at all. The operator decided:

- a CAN frame is stored only when its payload differs from that id's previous
  frame;
- an unchanged id is written again once per **broadcast stale timeout**, so
  its value never lapses. The timeout is one config setting
  (``collection.broadcast_stale_timeout_seconds``, default
  :data:`DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS`); the re-write happens
  :data:`REWRITE_LEAD_SECONDS` before it, derived rather than configured, and
  a timeout under :data:`MIN_BROADCAST_STALE_TIMEOUT_SECONDS` is refused;
- there are no "stopped" rows;
- sub-second frame timing and per-id frame counts need a capture profile that
  stores every frame (``CaptureProfile.store_every_frame``).

This module is the one statement of that rule, used by everything that must
agree on it:

- :class:`CanChangeFilter` decides, frame by frame, what is kept. The storage
  writer runs one per session (every id's first frame in a session is kept, so
  a session stands alone), and the live feature source runs one over the same
  stream, so live, stored and replayed features see the same frames. The
  filter is idempotent: a change-only stream filtered again is unchanged, which
  is why a replay of a new session stores what it read.
- :class:`CanStorage` is the mode, recorded in every chunk's parquet footer
  (:data:`FOOTER_KEY`). A chunk without the key predates P02.10 and stores
  every frame, so old sessions read exactly as they always did.
- :func:`session_can_storage` reads it back per chunk, and
  :func:`refuse_change_only` is the refusal every reader that counts stored
  frames or computes rates from them calls: in a change-only span a stored
  frame count is not the bus's.

What a change-only span means to a reader: a broadcast channel's value at an
instant is the **last stored row before it, valid for the stale timeout**
(an unchanged id is re-written before that), not the nearest row. The 1 Hz
resampler gives broadcast channels that stale class
(:meth:`src.preprocessing.resampler.ResampleConfig.max_gap_for`), and the
per-sample readers apply it inside the reader
(:mod:`src.analysis.channels`, :mod:`src.analysis.compare`).
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

import numpy as np

from src.can_ids import parse_can_channel
from src.telemetry import (
    CAN_FRAME_KIND_KEY,
    CAN_SOURCE,
    DBC_SOURCE,
    DECODED_SOURCE,
    SOURCE_SEP,
    TelemetryRecord,
)

log = logging.getLogger(__name__)

#: The operator's default (2026-09-26).
DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS: Final = 10.0
#: A shorter timeout is refused: the re-write happens one second before the
#: timeout, so under two seconds an unchanged id would be re-written more
#: often than once a second and the filter would save nothing it can measure.
MIN_BROADCAST_STALE_TIMEOUT_SECONDS: Final = 2.0
#: How long before the timeout an unchanged id is re-written: derived from the
#: timeout, never configured. It covers ids whose frame period is under a
#: second (the re-write fires on the first frame at least ``timeout - 1`` s
#: after the last stored one, so the stored value is at most ``timeout - 1 +
#: period`` old). A slower id could lapse that way (a 7 s id at 9 + 7 s), so
#: :class:`CanChangeFilter` also keeps a frame when the next one, one
#: inter-arrival later, would find the stored value past the timeout.
REWRITE_LEAD_SECONDS: Final = 1.0

#: Parquet footer (schema metadata) key carrying a chunk's :class:`CanStorage`.
FOOTER_KEY: Final = b"canary.can_storage"
#: ``DataFrame.attrs`` key the session readers set: the session's
#: :class:`SessionCanStorage` summary as a plain dict (see
#: :func:`frame_attrs`), so a reader handed a frame rather than a path still
#: knows which broadcast rows are change-only.
FRAME_ATTR: Final = "can_storage"

CanStorageMode = Literal["every_frame", "change_only"]

#: Wide-column namespaces decoded from broadcast frames. Their channels are
#: change-only wherever the frames are.
BROADCAST_NAMESPACES: Final = (CAN_SOURCE, DECODED_SOURCE, DBC_SOURCE)
_BROADCAST_PREFIXES: Final = tuple(f"{ns}{SOURCE_SEP}" for ns in BROADCAST_NAMESPACES)

#: The capture profile starter that stores every frame, named in refusals.
EVERY_FRAME_PROFILE: Final = "broadcast-every-frame"


def is_diagnostic_can_id(channel: str) -> bool:
    """Whether a CAN channel is ISO 15765-4 diagnostic traffic (OBD/UDS
    requests and responses): 11-bit ``0x7DF`` and ``0x7E0``-``0x7EF``, 29-bit
    ``0x18DB33F1`` and ``0x18DAxxxx``. Each frame is a message (a request, a
    response segment, flow control), never a repeating value: two identical
    requests are two requests, and an ISO-TP transfer sniffed from another
    tester needs every segment. Change-only storage keeps every one (P02.10).
    These are the legislated ranges, the same on any vehicle; a vehicle's
    other diagnostic ids are not listed.
    """
    can_id = parse_can_channel(channel)
    if can_id is None:
        return False
    return (
        can_id == 0x7DF
        or 0x7E0 <= can_id <= 0x7EF
        or can_id == 0x18DB33F1
        or (can_id >> 16) == 0x18DA
    )


def is_broadcast_channel(channel: str) -> bool:
    """Whether a ``<source>:<channel>`` key is decoded from broadcast frames."""
    return str(channel).startswith(_BROADCAST_PREFIXES)


def validate_broadcast_stale_timeout(seconds: float) -> float:
    """``seconds`` as a timeout, or ``ValueError`` naming why it cannot be one."""
    value = float(seconds)
    if not np.isfinite(value) or value < MIN_BROADCAST_STALE_TIMEOUT_SECONDS:
        raise ValueError(
            f"broadcast stale timeout {seconds!r} s is under the "
            f"{MIN_BROADCAST_STALE_TIMEOUT_SECONDS:g} s floor (P02.10, operator 2026-09-26): "
            f"an unchanged CAN id is re-written {REWRITE_LEAD_SECONDS:g} s before the "
            "timeout, so a shorter one would re-write it more than once a second"
        )
    return value


def rewrite_after_seconds(stale_timeout_seconds: float) -> float:
    """Seconds after its last stored frame an unchanged id is written again."""
    return validate_broadcast_stale_timeout(stale_timeout_seconds) - REWRITE_LEAD_SECONDS


@dataclass(frozen=True)
class CanStorage:
    """How one chunk (or one session's writer) stores CAN frames.

    ``recorded`` is ``False`` for a chunk whose footer carries no mode: it was
    written before P02.10 and stores every frame.
    """

    mode: CanStorageMode
    stale_timeout_seconds: float | None = None
    recorded: bool = True

    def __post_init__(self) -> None:
        if self.mode == "change_only":
            if self.stale_timeout_seconds is None:
                raise ValueError("change-only CAN storage needs its stale timeout")
            validate_broadcast_stale_timeout(self.stale_timeout_seconds)
        elif self.mode != "every_frame":
            raise ValueError(f"unknown CAN storage mode {self.mode!r}")

    @classmethod
    def change_only(cls, stale_timeout_seconds: float) -> CanStorage:
        return cls("change_only", validate_broadcast_stale_timeout(stale_timeout_seconds))

    @classmethod
    def every_frame(cls) -> CanStorage:
        return cls("every_frame")

    @property
    def is_change_only(self) -> bool:
        return self.mode == "change_only"

    @property
    def rewrite_after_seconds(self) -> float | None:
        if self.stale_timeout_seconds is None or not self.is_change_only:
            return None
        return rewrite_after_seconds(self.stale_timeout_seconds)

    def footer(self) -> dict[bytes, bytes]:
        """The parquet schema metadata recording this mode."""
        payload: dict[str, Any] = {"version": 1, "mode": self.mode}
        if self.is_change_only:
            payload["stale_timeout_seconds"] = self.stale_timeout_seconds
            payload["rewrite_after_seconds"] = self.rewrite_after_seconds
        return {FOOTER_KEY: json.dumps(payload, sort_keys=True).encode()}

    @classmethod
    def from_footer(cls, metadata: Mapping[bytes, bytes] | None) -> CanStorage:
        """The mode a chunk's schema metadata records; every-frame when absent.

        A key that is present but unreadable is refused rather than read as
        every-frame: counting a change-only chunk's frames as the bus's is the
        exact mistake the key exists to prevent.
        """
        raw = (metadata or {}).get(FOOTER_KEY)
        if raw is None:
            return cls("every_frame", recorded=False)
        try:
            payload = json.loads(raw.decode())
            return cls(
                mode=payload["mode"],
                stale_timeout_seconds=payload.get("stale_timeout_seconds"),
            )
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"unreadable CAN storage footer {raw!r}: cannot tell whether this chunk "
                "stores every frame or only changes (P02.10)"
            ) from exc


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------


@dataclass
class CanChangeFilter:
    """Per-id change filter: last payload and last write per CAN channel.

    :meth:`keep` answers for one frame and updates the state as if the answer
    were acted on; a caller that does not store a kept frame must not ask.
    Non-CAN records are always kept and touch no state, and so are error and
    remote frames (``frame_kind``: each is an event, e.g. an RX overflow,
    never a repeating value) and ISO 15765-4 diagnostic frames
    (:func:`is_diagnostic_can_id`: each is a message). Frames are judged in arrival order; a frame
    stamped *before* its id's last write (a clock step) is kept and restarts
    that id's heartbeat, so a backwards step can never silence an id.

    The re-write: an unchanged frame is kept once its stored value is
    ``timeout - 1`` s old, or earlier when the id is slow or irregular enough
    that its next frame (its longest recent inter-arrival later) would find
    it at or past the timeout, so the stored value does not lapse while the
    id transmits at its recent cadence.
    """

    stale_timeout_seconds: float = DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS
    _rewrite_after: float = field(init=False)
    _last: dict[str, tuple[bytes, float]] = field(init=False, default_factory=dict)
    #: Each id's newest frame time, kept or not, and its recent inter-arrivals
    #: (at most :data:`_RECENT_INTERARRIVALS`, each at most the timeout: a
    #: longer gap is a silence, not a period).
    _seen: dict[str, float] = field(init=False, default_factory=dict)
    _recent: dict[str, deque[float]] = field(init=False, default_factory=dict)
    kept: int = field(init=False, default=0)
    suppressed: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._rewrite_after = rewrite_after_seconds(self.stale_timeout_seconds)

    @property
    def rewrite_after_seconds(self) -> float:
        return self._rewrite_after

    @property
    def storage(self) -> CanStorage:
        return CanStorage.change_only(self.stale_timeout_seconds)

    def reset(self) -> None:
        """Forget every id: the next frame of each is kept (a new session)."""
        self._last = {}
        self._seen = {}
        self._recent = {}

    def keep(
        self,
        channel: str,
        payload: bytes | bytearray | None,
        seconds: float,
        *,
        event: bool = False,
    ) -> bool:
        """Whether a CAN frame of ``channel`` at ``seconds`` (epoch) is kept.

        ``event`` marks an error or remote frame, and a diagnostic id
        (:func:`is_diagnostic_can_id`) is one too: always kept, no state.
        """
        if payload is None or event or is_diagnostic_can_id(channel):
            # Nothing to compare, or not a value: keep it, leave the state alone.
            self.kept += 1
            return True
        data = bytes(payload)
        previous = self._last.get(channel)
        seen = self._seen.get(channel)
        self._seen[channel] = seconds
        recent = self._recent.get(channel)
        if recent is None:
            recent = self._recent[channel] = deque(maxlen=_RECENT_INTERARRIVALS)
        if seen is not None and seen <= seconds and seconds - seen <= self.stale_timeout_seconds:
            recent.append(seconds - seen)
        # The next frame is expected within the id's longest recent gap: an
        # irregular id (a tester's requests) is judged by its slowest cadence.
        interarrival = max(recent) if recent else 0.0
        age = seconds - previous[1] if previous is not None else 0.0
        if (
            previous is None
            or previous[0] != data
            or age >= self._rewrite_after
            or age + interarrival >= self.stale_timeout_seconds
            or seconds < previous[1]
        ):
            self._last[channel] = (data, seconds)
            self.kept += 1
            return True
        self.suppressed += 1
        return False

    def keep_record(self, record: TelemetryRecord) -> bool:
        """:meth:`keep` for a telemetry record; non-CAN records are kept."""
        if record.source != CAN_SOURCE:
            return True
        return self.keep(
            record.channel,
            record.payload,
            _epoch_seconds(record.timestamp),
            event=record.metadata.get(CAN_FRAME_KIND_KEY) is not None,
        )


#: How many of an id's recent inter-arrivals predict its next frame. Enough
#: to span an irregular id's bursts; few enough that one dropout on a 100 Hz
#: id is forgotten in a tenth of a second.
_RECENT_INTERARRIVALS = 16


def _epoch_seconds(moment: datetime) -> float:
    return moment.timestamp()


def change_only_mask(
    sources: Sequence[Any],
    channels: Sequence[Any],
    payloads: Sequence[Any],
    seconds: Sequence[float] | np.ndarray,
    *,
    stale_timeout_seconds: float = DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS,
    change_filter: CanChangeFilter | None = None,
    events: Sequence[bool] | None = None,
) -> np.ndarray:
    """Which rows (in the order given, which must be arrival order) the writer
    would keep: :class:`CanChangeFilter` applied row by row.

    For simulating change-only storage on a session stored every frame (the
    P02.10 parity evidence). Pass ``change_filter`` to carry state across
    chunks of one session. ``events`` marks error/remote frames (kept).
    """
    active = change_filter or CanChangeFilter(stale_timeout_seconds)
    stamps = np.asarray(seconds, dtype=np.float64)
    flags = [False] * len(stamps) if events is None else list(events)
    out = np.ones(len(stamps), dtype=bool)
    rows = zip(sources, channels, payloads, flags, strict=True)
    for i, (source, channel, payload, event) in enumerate(rows):
        if source == CAN_SOURCE:
            out[i] = active.keep(str(channel), payload, float(stamps[i]), event=bool(event))
    return out


def _frame_events(metadata_json: Sequence[Any]) -> list[bool]:
    """Which stored rows are tagged error/remote frames (``frame_kind``)."""
    marker = f'"{CAN_FRAME_KIND_KEY}"'
    return [isinstance(raw, str) and marker in raw for raw in metadata_json]


# ---------------------------------------------------------------------------
# Reading the recorded mode back
# ---------------------------------------------------------------------------


@lru_cache(maxsize=16_384)
def _chunk_storage_cached(path: str, size: int, mtime_ns: int) -> CanStorage:
    import pyarrow.parquet as pq

    metadata = pq.read_schema(path).metadata  # type: ignore[no-untyped-call, unused-ignore]
    return CanStorage.from_footer(metadata)


def chunk_can_storage(path: Path) -> CanStorage | None:
    """The mode one chunk's footer records; ``None`` for an unreadable chunk
    (a torn tail the loaders skip anyway)."""
    try:
        stat = path.stat()
        if stat.st_size == 0:
            return None
        return _chunk_storage_cached(str(path), stat.st_size, stat.st_mtime_ns)
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 - unreadable footer: the loaders skip the chunk too
        return None


@dataclass(frozen=True)
class SessionCanStorage:
    """Every readable chunk's mode, in chunk-name order."""

    session: str
    chunks: tuple[tuple[str, CanStorage], ...]

    @property
    def change_only_chunks(self) -> tuple[str, ...]:
        return tuple(name for name, storage in self.chunks if storage.is_change_only)

    @property
    def any_change_only(self) -> bool:
        return bool(self.change_only_chunks)

    @property
    def stale_timeout_seconds(self) -> float | None:
        """The longest timeout any change-only chunk was written with."""
        timeouts = [
            storage.stale_timeout_seconds
            for _name, storage in self.chunks
            if storage.is_change_only and storage.stale_timeout_seconds is not None
        ]
        return max(timeouts) if timeouts else None

    def attrs(self) -> dict[str, Any]:
        """The plain-dict form the session readers put on ``DataFrame.attrs``."""
        return {
            "change_only": self.any_change_only,
            "stale_timeout_seconds": self.stale_timeout_seconds,
            "change_only_chunks": len(self.change_only_chunks),
            "chunks": len(self.chunks),
        }


def session_can_storage(
    session_dir: Path, chunks: Iterable[Path] | None = None
) -> SessionCanStorage:
    """The CAN storage mode of every readable chunk of ``session_dir``."""
    paths = (
        sorted(chunks) if chunks is not None else sorted(session_dir.glob("telemetry-*.parquet"))
    )
    found: list[tuple[str, CanStorage]] = []
    for path in paths:
        storage = chunk_can_storage(path)
        if storage is not None:
            found.append((path.name, storage))
    return SessionCanStorage(session=session_dir.name, chunks=tuple(found))


def frame_attrs(storage: SessionCanStorage | Iterable[SessionCanStorage]) -> dict[str, Any]:
    """:attr:`FRAME_ATTR`'s value for a frame read from one or more sessions."""
    sessions = [storage] if isinstance(storage, SessionCanStorage) else list(storage)
    timeouts = [s.stale_timeout_seconds for s in sessions if s.stale_timeout_seconds is not None]
    return {
        "change_only": any(s.any_change_only for s in sessions),
        "stale_timeout_seconds": max(timeouts) if timeouts else None,
        "change_only_sessions": sorted(s.session for s in sessions if s.any_change_only),
    }


def change_only_timeout(frame_or_attrs: Any) -> float | None:
    """The stale timeout of a frame's change-only broadcast rows, from the
    loader's :attr:`FRAME_ATTR`; ``None`` when every frame was stored (or the
    frame did not come from a session reader)."""
    attrs = getattr(frame_or_attrs, "attrs", frame_or_attrs)
    info = attrs.get(FRAME_ATTR) if isinstance(attrs, Mapping) else None
    if not isinstance(info, Mapping) or not info.get("change_only"):
        return None
    timeout = info.get("stale_timeout_seconds")
    return float(timeout) if timeout is not None else DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS


def held_series(series: Any, *, validity_s: float, period_s: float | None = None) -> Any:
    """A change-only broadcast channel's samples with its unchanged frames put
    back (P02.10), for a reader that treats samples as measurements at their
    instants (interpolation, per-step means, hole checks).

    ``series`` is numeric, indexed by UTC acquisition time. Each stored row is
    repeated every ``period_s`` after itself until the next stored row, and at
    most ``validity_s`` after it: in change-only storage the bus carried that
    value at its frame period until the next change, and an unchanged id is
    re-written before the timeout, so no row for that long means the frames
    stopped. ``period_s`` defaults to the channel's own frame period, a low
    percentile of its stored-row gaps (a channel that changes frame to frame
    stores rows one period apart); pass a ceiling (a reader's grid step) when
    the channel may never have changed in the read, which leaves no short
    gap to estimate it from. A series with fewer than two rows is returned
    as is.
    """
    import pandas as pd

    clean = series.dropna().sort_index(kind="stable")
    if len(clean) < 2:
        return clean
    stamps = pd.DatetimeIndex(pd.to_datetime(clean.index, utc=True))
    t = np.asarray((stamps - stamps[0]) / pd.Timedelta(seconds=1), dtype=np.float64)
    gaps = np.diff(t)
    positive = gaps[gaps > 0]
    estimate = float(np.percentile(positive, 5)) if len(positive) >= 20 else np.inf
    period = min(estimate, period_s) if period_s is not None else estimate
    if not np.isfinite(period) or period <= 0:
        return clean
    until = np.minimum(np.append(gaps, np.inf), validity_s)
    # Repeats strictly before the next stored row: j * period < gap, j >= 1.
    counts = np.maximum(np.ceil(until / period - 1e-9).astype(np.int64) - 1, 0)
    counts[np.append(gaps, np.inf) > validity_s] = np.floor(validity_s / period).astype(np.int64)
    if not counts.any():
        return clean
    base = np.repeat(np.arange(len(t)), counts)
    offsets = np.concatenate([np.arange(1, n + 1) for n in counts if n]) * period
    extra_t = t[base] + offsets
    values = clean.to_numpy()
    held = pd.Series(
        np.concatenate([values, values[base]]),
        index=stamps[0] + pd.to_timedelta(np.concatenate([t, extra_t]), unit="s"),
        name=clean.name,
    )
    return held.sort_index(kind="stable")


class ChangeOnlyStorageError(ValueError):
    """A reader that counts stored CAN frames (or computes rates from them)
    was handed a span that stores them change-only.

    Its own type so callers can branch on it, never on the message.
    """

    def __init__(self, message: str, *, reader: str, sessions: Sequence[str]) -> None:
        super().__init__(message)
        self.reader = reader
        self.sessions = tuple(sessions)


def change_only_refusal(reader: str, sessions: Sequence[str], timeout: float | None) -> str:
    rewrite = (
        f"{timeout - REWRITE_LEAD_SECONDS:g} s"
        if timeout is not None
        else "the stale timeout minus one second"
    )
    return (
        f"{reader}: {', '.join(sessions)} stores CAN frames change-only (P02.10, operator "
        "2026-09-26): a frame is stored only when its payload changes, and an unchanged id "
        f"is re-written once every {rewrite}. Stored frame counts, frame rates and "
        "inter-arrival times are therefore not the bus's. Record with a capture profile "
        f"that sets store_every_frame (the starter profile '{EVERY_FRAME_PROFILE}') to "
        "count frames."
    )


def refuse_change_only(
    source: Path | Any | Iterable[Path],
    *,
    reader: str,
) -> None:
    """Raise :class:`ChangeOnlyStorageError` when ``source`` holds a change-only
    span: a session directory, several, or a frame a session reader returned
    (whose :attr:`FRAME_ATTR` says so)."""
    if isinstance(source, Path):
        storages = [session_can_storage(source)]
    elif hasattr(source, "attrs") and not isinstance(source, (list, tuple)):
        timeout = change_only_timeout(source)
        if timeout is None:
            return
        info = source.attrs.get(FRAME_ATTR, {})
        sessions = info.get("change_only_sessions") or ["this frame"]
        raise ChangeOnlyStorageError(
            change_only_refusal(reader, sessions, timeout), reader=reader, sessions=sessions
        )
    else:
        storages = [session_can_storage(Path(p)) for p in source]
    offending = [s for s in storages if s.any_change_only]
    if offending:
        names = [s.session for s in offending]
        timeouts = [s.stale_timeout_seconds for s in offending if s.stale_timeout_seconds]
        raise ChangeOnlyStorageError(
            change_only_refusal(reader, names, max(timeouts) if timeouts else None),
            reader=reader,
            sessions=names,
        )


def change_only_copy(
    session_dir: Path,
    out_dir: Path,
    *,
    stale_timeout_seconds: float = DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS,
) -> tuple[int, int]:
    """Write a change-only copy of a session stored every frame (P02.10).

    Each chunk is filtered by one :class:`CanChangeFilter` carried across the
    session's chunks in name order (the writer's arrival order), and written
    under the same name into ``out_dir`` with the change-only footer. The
    source is only read: a historic session is never rewritten, so ``out_dir``
    must be a new or empty directory elsewhere. For the parity evidence and
    tests that compare a session with what the recorder would now store.

    Returns ``(rows read, rows written)``.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    source, target = Path(session_dir).resolve(), Path(out_dir).resolve()
    if source == target:
        raise ValueError(f"refusing to rewrite {session_dir} in place: write the copy elsewhere")
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"refusing to write a change-only copy into non-empty {out_dir}")
    already = session_can_storage(Path(session_dir))
    if already.any_change_only:
        raise ValueError(f"{session_dir} already stores CAN frames change-only")
    target.mkdir(parents=True, exist_ok=True)
    change_filter = CanChangeFilter(stale_timeout_seconds)
    footer = change_filter.storage.footer()
    read = written = 0
    for chunk in sorted(Path(session_dir).glob("telemetry-*.parquet")):
        if chunk.stat().st_size == 0:
            continue
        try:
            table = pq.read_table(chunk)  # type: ignore[no-untyped-call, unused-ignore]
        except Exception:  # noqa: BLE001 - a torn chunk the loaders skip too
            continue
        read += table.num_rows
        if "payload" in table.schema.names:
            stamps = table["timestamp"].cast(pa.timestamp("us", tz="UTC"))
            seconds = stamps.cast(pa.int64()).to_numpy() / 1e6
            mask = change_only_mask(
                table["source"].to_pylist(),
                table["channel"].to_pylist(),
                table["payload"].to_pylist(),
                seconds,
                change_filter=change_filter,
                events=(
                    _frame_events(table["metadata_json"].to_pylist())
                    if "metadata_json" in table.schema.names
                    else None
                ),
            )
            table = table.filter(pa.array(mask))
        written += table.num_rows
        metadata = dict(table.schema.metadata or {})
        if "source" in table.schema.names and CAN_SOURCE in set(table["source"].to_pylist()):
            metadata.update(footer)  # a chunk without CAN frames says nothing
        pq.write_table(  # type: ignore[no-untyped-call, unused-ignore]
            table.replace_schema_metadata(metadata), target / chunk.name
        )
    return read, written


class UnfaithfulReplayError(ValueError):
    """A replay of a change-only session that the live pipeline would read
    differently from the recording (P02.10). Refused before a row is emitted;
    the control plane answers HTTP 422, the YAML collector logs and idles."""


class SlowReplayError(UnfaithfulReplayError):
    """A replay slower than real time of a session with change-only spans."""


def refuse_slow_replay(session_dir: Path, speed: float) -> None:
    """Refuse to replay a change-only session slower than real time (P02.10).

    A replay re-stamps every row at its emission time, so at ``speed`` < 1 an
    unchanged id's re-write (every ``timeout - 1`` s recorded) arrives
    ``(timeout - 1) / speed`` s after the previous one: the live pipeline reads
    a transmitting id as stale between re-writes, and the ``_replay-*`` output
    stores gaps longer than the timeout its footer records. Real time or
    faster keeps every gap within the timeout. (A replay that falls behind
    real time through load stretches gaps the same way; its
    ``effective_speed`` says when.)
    """
    if speed >= 1.0:
        return
    storage = session_can_storage(Path(session_dir))
    if storage.any_change_only:
        timeout = storage.stale_timeout_seconds or DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS
        raise SlowReplayError(
            f"refusing to replay {Path(session_dir).name} at {speed:g}x: it stores CAN frames "
            "change-only (P02.10), an unchanged id re-written once every "
            f"{timeout - REWRITE_LEAD_SECONDS:g} s, "
            "so slower than real time those re-writes arrive past the stale timeout and a "
            "transmitting id reads stale. Replay at speed >= 1."
        )


class BroadcastTimeoutMismatchError(ValueError):
    """A session's change-only chunks were written with a longer broadcast
    stale timeout than the reader's stale class: its unchanged ids were
    re-written further apart than the reader would hold a value, so every
    quiet id would read stale between re-writes."""


class ReplayTimeoutMismatchError(UnfaithfulReplayError, BroadcastTimeoutMismatchError):
    """A replay of a session whose change-only chunks were written with a
    longer broadcast stale timeout than the daemon's current setting."""


def refuse_unfaithful_replay(
    session_dir: Path, speed: float, *, stale_timeout_seconds: float
) -> None:
    """Every P02.10 refusal of a replay, in one call for every replay path
    (:class:`src.control.replay_control.ReplayController` and
    :func:`src.collection.replay.replay_session`).

    - slower than real time (:func:`refuse_slow_replay`);
    - recorded with a longer stale timeout than ``stale_timeout_seconds``, the
      daemon's ``collection.broadcast_stale_timeout_seconds``: the recording
      re-wrote unchanged ids ``recorded - 1`` s apart, so the live pipeline
      (whose broadcast class and filter use the daemon's timeout) would read
      every quiet id stale between re-writes, and the ``_replay-*`` output
      would carry gaps longer than the timeout its own footer records. This
      is the replay form of :func:`refuse_shorter_read_timeout`.
    """
    refuse_slow_replay(session_dir, speed)
    recorded = session_can_storage(Path(session_dir)).stale_timeout_seconds
    if recorded is not None and recorded > stale_timeout_seconds:
        raise ReplayTimeoutMismatchError(
            f"refusing to replay {Path(session_dir).name}: it stores CAN frames change-only "
            f"(P02.10) with a {recorded:g} s broadcast stale timeout (unchanged ids re-written "
            f"every {recorded - REWRITE_LEAD_SECONDS:g} s), but this daemon's "
            f"collection.broadcast_stale_timeout_seconds is {stale_timeout_seconds:g} s: the "
            "live pipeline would read every quiet id stale between re-writes, and the replay's "
            "output would store gaps longer than the timeout its footer records. Set the "
            f"timeout to at least {recorded:g} s to replay it."
        )


def refuse_shorter_read_timeout(session_dir: Path, read_timeout_seconds: float) -> None:
    """Refuse to read ``session_dir`` with a broadcast stale class shorter than
    the timeout its change-only chunks were written with (P02.10)."""
    refuse_shorter_stored_timeout(
        session_can_storage(session_dir).stale_timeout_seconds,
        read_timeout_seconds,
        what=session_dir.name,
    )


def refuse_shorter_stored_timeout(
    recorded: float | None, read_timeout_seconds: float, *, what: str
) -> None:
    """:func:`refuse_shorter_read_timeout` for data that already carries its
    recorded timeout (a reduced session, a frame's ``attrs``)."""
    if recorded is not None and recorded > read_timeout_seconds:
        rewrite = recorded - REWRITE_LEAD_SECONDS
        raise BroadcastTimeoutMismatchError(
            f"{what} stores CAN frames change-only with a {recorded:g} s broadcast "
            f"stale timeout (unchanged ids re-written every {rewrite:g} s), "
            f"but this read's broadcast stale class is {read_timeout_seconds:g} s "
            "(collection.broadcast_stale_timeout_seconds): every quiet id would read stale "
            f"between re-writes. Read it with a timeout of at least {recorded:g} s."
        )


__all__ = [
    "BROADCAST_NAMESPACES",
    "DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS",
    "EVERY_FRAME_PROFILE",
    "FOOTER_KEY",
    "FRAME_ATTR",
    "MIN_BROADCAST_STALE_TIMEOUT_SECONDS",
    "REWRITE_LEAD_SECONDS",
    "BroadcastTimeoutMismatchError",
    "CanChangeFilter",
    "CanStorage",
    "ChangeOnlyStorageError",
    "ReplayTimeoutMismatchError",
    "SessionCanStorage",
    "SlowReplayError",
    "UnfaithfulReplayError",
    "change_only_copy",
    "change_only_mask",
    "change_only_refusal",
    "change_only_timeout",
    "chunk_can_storage",
    "frame_attrs",
    "held_series",
    "is_broadcast_channel",
    "is_diagnostic_can_id",
    "refuse_change_only",
    "refuse_shorter_read_timeout",
    "refuse_slow_replay",
    "refuse_shorter_stored_timeout",
    "refuse_unfaithful_replay",
    "rewrite_after_seconds",
    "session_can_storage",
    "validate_broadcast_stale_timeout",
]
