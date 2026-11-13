"""A value computed from files, reused until one of them changes (R29.9).

``GET /status`` is polled, and several of its blocks are read from files that
change a few times a day: the model manifests, the flash registry, the DID
catalog and its last verify. Re-reading them on every poll cost more than the
rest of the payload together. :class:`StampedCache` keys each value on the
identity of the files it was computed from, ``(path, mtime_ns, size)`` --
:func:`src.yaml_load.load_yaml_model`'s key, extended from one file to a set --
so an edited, added or removed file is a different key and the next call
recomputes.

**The key is not enough for a file written just now.** The kernel stamps a
write from a coarse clock, so a same-size rewrite within one tick keeps the
old ``st_mtime_ns``: the key matches and the old value would be served
(``yaml_load`` measured 1987 of 2000 back-to-back rewrites unchanged). The
same "racily clean" rule applies here: a value computed within
:data:`RACY_WINDOW_NS` of its newest file's mtime is not trusted and is
recomputed on the next call, until it has settled. A cache that can serve a
stale model manifest after ``canaryctl models upload`` would be a bug, not a
trade-off, so this is the one rule a caller cannot opt out of.

Only the *stamps* are read on a hit: what a caller memoizes is the parse and
the validation, never the listing that finds the files.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Hashable, Sequence
from pathlib import Path
from typing import Generic, NamedTuple, TypeVar

from src.yaml_load import RACY_WINDOW_NS

T = TypeVar("T")


class FileStamp(NamedTuple):
    """One file's identity: where it is, when it was last written, how big."""

    path: str
    mtime_ns: int
    size: int


def file_stamp(path: Path) -> FileStamp | None:
    """``path``'s stamp, or ``None`` when it does not exist (or cannot be read).

    ``None`` is a stamp too: a file appearing, or disappearing, changes the key.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return FileStamp(str(path), stat.st_mtime_ns, stat.st_size)


#: One entry: the stamps the value was computed under, the value, and the
#: ``time.time_ns()`` it was computed at (what the racy window is measured from).
_Entry = tuple[tuple[FileStamp | None, ...], object, int]


class StampedCache(Generic[T]):
    """Values keyed by a caller's ``scope`` and validated by file stamps.

    ``scope`` names *what* is cached (a models directory, a pair of paths);
    ``stamps`` say *which version of its files*. Bounded to ``max_entries``
    scopes, oldest evicted, so a test run that builds a hundred temporary
    directories cannot grow it without limit. Thread-safe: ``/status`` runs in
    FastAPI's worker pool. ``compute`` runs outside the lock, so two threads
    that miss together both compute (the same answer) and the later write wins.
    """

    def __init__(self, max_entries: int = 32) -> None:
        self._max = max_entries
        self._entries: dict[Hashable, _Entry] = {}
        self._lock = threading.Lock()

    def get(
        self,
        scope: Hashable,
        stamps: Sequence[FileStamp | None],
        compute: Callable[[], T],
    ) -> T:
        """The value for ``scope``, recomputed when ``stamps`` differ or are racily new.

        ``stamps`` must be taken *before* the call, and ``compute`` must read
        the same files: a file that changes between the two leaves a key older
        than the value, which the next call sees as a difference and fixes.
        """
        key = tuple(stamps)
        with self._lock:
            entry = self._entries.get(scope)
        if entry is not None and entry[0] == key and _settled(key, entry[2]):
            value: T = entry[1]  # type: ignore[assignment]
            return value
        value = compute()
        with self._lock:
            if scope not in self._entries and len(self._entries) >= self._max:
                self._entries.pop(next(iter(self._entries)))
            self._entries[scope] = (key, value, time.time_ns())
        return value

    def clear(self) -> None:
        """Forget everything (a test that rewrites a file in place and cannot wait)."""
        with self._lock:
            self._entries.clear()


def _settled(stamps: tuple[FileStamp | None, ...], cached_at_ns: int) -> bool:
    """Whether the value was computed long enough after its newest file was written."""
    newest = max((stamp.mtime_ns for stamp in stamps if stamp is not None), default=None)
    return newest is None or cached_at_ns - newest >= RACY_WINDOW_NS


__all__ = ["FileStamp", "StampedCache", "file_stamp"]
