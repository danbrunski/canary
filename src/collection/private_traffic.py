"""Arbitration ids whose frames are not telemetry, for a window of time (W08.7).

The ECU backup reads the whole flash over one request/response id pair: about
sixteen thousand ReadMemoryByAddress exchanges, each a dozen frames. On
SocketCAN every socket on the interface hears every frame, including the ones
another socket in this process sent, so the daemon's own raw capture would
store all of that exchange as broadcast telemetry: several hundred thousand
rows of a flash dump in the session that trains the detectors.

A sender that owns such an exchange *holds* its ids from :func:`hold` to
:func:`release`, which records a window ``[start, end]`` on the wall clock.
The raw-capture taps (``can_obd_collector`` and ``can_collector.CanReader``)
ask :func:`exclude` about every frame, and it decides by the frame's **own
acquisition time** (:func:`src.collection.frame_time.frame_arrival`, the clock
the row would be stored under), not by when the tap gets round to it. A frame
received inside the window is private however late it is processed: a tap
that drains its queue after the hold ended no longer leaks the tail of the
exchange (the ``10 81`` exit and its ``50 81`` reply leaked that way in ~15 %
of runs before this). Deciding at processing time was the bug; the timestamp
is the fact.

Closed windows are kept for :data:`RETAIN_SECONDS`, and a held frame's driver
stamp is believed up to that age, so a tap stalled for up to a minute still
matches them (beyond that the decision falls back to processing time; the
limit is documented on the constant). The hold is process-wide state like
:mod:`src.collection.tx_gate`, so no capture path has to be told about the
backup.

Reads are lock-free (one tuple swapped atomically under the lock): the taps
call :func:`exclude` for every frame, ~1,600 a second, and with no window the
check is one truthiness test.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

import can

from src.collection.frame_time import KERNEL_STAMP_MAX_SKEW_SECONDS, frame_arrival

#: How long a closed window keeps matching late-processed frames, and how old
#: a frame's driver stamp may be and still be believed for the decision.
#: Generous: a tap a whole minute behind is a stalled collector, not a timing
#: detail. The limit: a tap stalled longer than this judges a held frame by
#: its processing time (the stamp is then indistinguishable from a stale
#: post-suspend one), so a frame acquired inside a window could leak.
RETAIN_SECONDS = 60.0


@dataclass(frozen=True)
class _Window:
    owner: str
    ids: frozenset[int]
    start: float
    #: ``math.inf`` while the hold stands.
    end: float

    def covers(self, arbitration_id: int, at: float) -> bool:
        return arbitration_id in self.ids and self.start <= at <= self.end


_lock = threading.Lock()
#: Every open window and every closed one still retained. Replaced, never
#: mutated, so readers need no lock.
_windows: tuple[_Window, ...] = ()
_dropped: dict[str, int] = {}


def _prune_locked(now: float) -> tuple[_Window, ...]:
    return tuple(w for w in _windows if w.end == math.inf or now - w.end <= RETAIN_SECONDS)


def hold(owner: str, arbitration_ids: Iterable[int], *, now: float | None = None) -> None:
    """Open ``owner``'s window over ``arbitration_ids`` from ``now``."""
    global _windows
    at = time.time() if now is None else now
    ids = frozenset(int(i) for i in arbitration_ids)
    with _lock:
        kept = tuple(w for w in _prune_locked(at) if not (w.owner == owner and w.end == math.inf))
        _windows = (*kept, _Window(owner=owner, ids=ids, start=at, end=math.inf))
        _dropped[owner] = 0


def release(owner: str, *, now: float | None = None) -> int:
    """Close ``owner``'s window at ``now``. Returns how many frames it has kept
    out of telemetry so far (late frames counted afterwards are in
    :func:`dropped`)."""
    global _windows
    at = time.time() if now is None else now
    with _lock:
        _windows = tuple(
            _Window(owner=w.owner, ids=w.ids, start=w.start, end=at)
            if w.owner == owner and w.end == math.inf
            else w
            for w in _prune_locked(at)
        )
        return _dropped.get(owner, 0)


def is_private(arbitration_id: int, at: float) -> bool:
    """True when a frame on ``arbitration_id`` acquired at ``at`` (wall clock)
    falls inside a hold window."""
    windows = _windows
    return bool(windows) and any(w.covers(arbitration_id, at) for w in windows)


def _acquired_at(message: can.Message, now: float) -> float:
    """The frame's own stamp, believed up to :data:`RETAIN_SECONDS` old.

    :func:`frame_arrival` stops trusting a stamp 5 s from the wall clock (a
    stale SocketCAN stamp after a suspend, an adapter with no clock), which is
    right for the clock a row is stored under but would let a tap stalled more
    than 5 s leak a held frame. For the privacy decision only, a stamp up to
    the retention period in the past still stands; anything older, in the
    future or absent falls back to the shared policy.
    """
    stamp = getattr(message, "timestamp", None)
    if (
        isinstance(stamp, (int, float))
        and -KERNEL_STAMP_MAX_SKEW_SECONDS <= now - float(stamp) <= RETAIN_SECONDS
    ):
        return float(stamp)
    return frame_arrival(message, now=now).at


def exclude(message: can.Message, *, now: float | None = None) -> bool:
    """For a raw-capture tap: True (and counted) when ``message`` must not be
    stored, judged by its own acquisition time. ``now`` is the wall clock
    (``time.time()`` when omitted; tests pass a stalled tap's)."""
    windows = _windows
    if not windows:
        return False
    arbitration_id = int(message.arbitration_id)
    if not any(arbitration_id in w.ids for w in windows):
        return False
    at = _acquired_at(message, time.time() if now is None else now)
    for window in windows:
        if window.covers(arbitration_id, at):
            with _lock:
                _dropped[window.owner] = _dropped.get(window.owner, 0) + 1
            return True
    return False


def dropped(owner: str) -> int:
    with _lock:
        return _dropped.get(owner, 0)


def snapshot() -> dict[str, list[str]]:
    """Ids of every *open* hold per owner, as hex, for status and tests."""
    windows = _windows
    return {w.owner: [f"0x{i:03X}" for i in sorted(w.ids)] for w in windows if w.end == math.inf}


def reset() -> None:
    """Drop every window (test setup)."""
    global _windows
    with _lock:
        _windows = ()
        _dropped.clear()


__all__ = [
    "RETAIN_SECONDS",
    "dropped",
    "exclude",
    "hold",
    "is_private",
    "release",
    "reset",
    "snapshot",
]
