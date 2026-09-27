"""The evaluation harness's score cache (P02.5).

``scripts/evaluate_detectors.py`` scores every detector on the same rows: each
held-out session, each injection window's clean slice, each injected slice,
and the A04.1 shift slice. Each detector's output on each of those *units* is
kept here, so a re-run rescores only what changed:

- the **component key** names the detector: its artifact's sha256 (and its
  manifest's, where the calibration lives), or a fitted baseline's parameters
  and threshold;
- the **unit key** names the rows: the session's resampled frame by content
  (the derived-frame key), the slice bounds and the injection spec, and the
  featurization inputs;
- both carry the :func:`code_version`, a hash of every source file under
  ``src/`` plus the harness itself and the numeric libraries' versions, so an
  edited transform cannot be served a score it did not produce.

A changed artifact changes only its own component key (and the fused
detector's, which consumes it); every other detector reads its scores back.
Entries are pickles of exactly what the scorer returned, so a cached verdict
is the same object type (``bool`` vs ``numpy.bool_``) as a fresh one and the
report is byte-identical either way. A cache, never a source of truth:
gitignored, laptop-only (P01's rules), deleted without loss.

**A run writes nothing until it has proved its key.** Scoring takes minutes,
and another session can edit ``src/``, a catalog or an artifact meanwhile; a
score computed from the new bytes must never land under the old key (or the
reverse). So entries are staged under ``.staging/<run>/`` and moved into place
by :meth:`ScoreCache.commit` only after the harness has re-read every key
input and found it unchanged; otherwise :meth:`ScoreCache.discard` drops them.

**Bounded.** One A02.11 run is ~46 MB of entries, and each code edit or new
artifact starts a fresh set. :meth:`ScoreCache.prune` keeps the store under
:data:`SCORE_CACHE_MAX_BYTES` by deleting the least recently used entries
(a hit refreshes an entry's mtime), never one the current run read or wrote.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import platform
import shutil
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path
from typing import Any

import pandas as pd

from src.paths import REPO_ROOT
from src.preprocessing.derived_store import refuse_on_the_pi

log = logging.getLogger(__name__)

#: Bumped when an entry's layout changes (the code version covers logic).
SCORE_CACHE_VERSION = 1

#: Libraries whose version can move a score by a last bit.
_VERSIONED_LIBRARIES = ("numpy", "pandas", "scikit-learn", "scipy", "torch")

#: Upper bound on the store (P02.5). About twenty A02.11-sized entry sets: a
#: day of edits and artifact swaps, a rounding error beside the 1.7 GB stream
#: cache next to it.
SCORE_CACHE_MAX_BYTES = 1024**3

#: Where a run's entries wait for :meth:`ScoreCache.commit`.
STAGING_DIR = ".staging"

#: A staging directory this old belongs to a run that died before committing.
STALE_STAGING_SECONDS = 24 * 3600


def digest(*parts: Any) -> str:
    """A stable sha256 over JSON-able ``parts`` (``str()`` for the rest)."""
    text = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def code_version(extra_files: Iterable[Path] = ()) -> str:
    """Every ``src/**/*.py`` file's path and bytes, ``extra_files``, the
    numeric libraries' versions and :data:`SCORE_CACHE_VERSION`, hashed.

    Deliberately the whole package, not a hand-kept list of the modules a
    score depends on: a list is a thing to forget to edit, and a stale score
    is the one failure a cache must not have. Any source edit costs a
    rescore, nothing more.
    """
    hasher = hashlib.sha256(f"score-cache-v{SCORE_CACHE_VERSION}".encode())
    files = sorted((REPO_ROOT / "src").rglob("*.py")) + sorted(Path(p) for p in extra_files)
    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        resolved = path.resolve()
        shown = resolved.relative_to(REPO_ROOT) if resolved.is_relative_to(REPO_ROOT) else resolved
        hasher.update(str(shown).encode())
        hasher.update(hashlib.sha256(data).digest())
    hasher.update(f"python={platform.python_version()}".encode())
    for name in _VERSIONED_LIBRARIES:
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = "absent"
        hasher.update(f"{name}={version}".encode())
    return hasher.hexdigest()


def frame_key(frame: pd.DataFrame) -> str:
    """A resampled frame's content key: column names, dtypes and every value.

    Content, not provenance: two sessions that resample to the same rows share
    their scores, and a re-derived frame that moved by one cell does not.
    """
    hasher = hashlib.sha256()
    hasher.update(json.dumps([str(c) for c in frame.columns]).encode())
    hasher.update(json.dumps([str(t) for t in frame.dtypes]).encode())
    hasher.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return hasher.hexdigest()


class ScoreCache:
    """Pickled scorer outputs under ``root/<component>/<unit>.pkl``.

    ``root=None`` is a cache that never hits and keeps nothing. ``run`` names
    this run's staging directory; the harness passes its own to every scoring
    worker, so a worker's entries are staged beside the parent's and a drive a
    broken pool finished is read back in-process. A write is atomic (temp
    file + rename) and staged; :meth:`get` reads the store, then the staging
    directory. ``hits``/``misses`` count lookups by component name.
    """

    def __init__(self, root: Path | None, *, run: str | None = None) -> None:
        if root is not None:
            refuse_on_the_pi()
        self.root = root
        self.run = run or uuid.uuid4().hex
        self.hits: Counter[str] = Counter()
        self.misses: Counter[str] = Counter()

    @property
    def staging(self) -> Path | None:
        return None if self.root is None else self.root / STAGING_DIR / self.run

    @staticmethod
    def _relative(component: str, unit: str) -> Path:
        return Path(component) / unit[:2] / f"{unit}.pkl"

    @staticmethod
    def _name(component: str) -> str:
        return component.rsplit("-", 1)[0]

    def get(self, component: str, unit: str) -> Any | None:
        """The entry, or ``None`` (a miss). An unreadable entry is a miss; a
        hit on the store refreshes the entry's mtime (see :meth:`prune`)."""
        if self.root is None or self.staging is None:
            self.misses[self._name(component)] += 1
            return None
        relative = self._relative(component, unit)
        for path, stored in ((self.root / relative, True), (self.staging / relative, False)):
            try:
                with path.open("rb") as handle:
                    value = pickle.load(handle)
            except FileNotFoundError:
                continue
            except (OSError, pickle.UnpicklingError, EOFError, AttributeError) as exc:
                log.warning("unreadable score-cache entry %s (%s); rescoring", path, exc)
                continue
            if stored:
                try:
                    os.utime(path)
                except OSError:
                    pass
            self.hits[self._name(component)] += 1
            return value
        self.misses[self._name(component)] += 1
        return None

    def put(self, component: str, unit: str, value: Any) -> None:
        if self.staging is None:
            return
        path = self.staging / self._relative(component, unit)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def commit(self) -> int:
        """Move this run's staged entries into the store; returns how many."""
        if self.root is None or self.staging is None or not self.staging.exists():
            return 0
        moved = 0
        for staged in sorted(self.staging.rglob("*.pkl")):
            target = self.root / staged.relative_to(self.staging)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
            moved += 1
        shutil.rmtree(self.staging, ignore_errors=True)
        return moved

    def discard(self) -> None:
        """Drop this run's staged entries: the store is left as it was."""
        if self.staging is not None:
            shutil.rmtree(self.staging, ignore_errors=True)

    def prune(self, *, max_bytes: int = SCORE_CACHE_MAX_BYTES, keep_since: float) -> int:
        """Delete least-recently-used entries until the store fits ``max_bytes``.

        An entry whose mtime is at or after ``keep_since`` (the run's start) was
        read or written by this run and is never deleted, so a run's own
        entries survive even when they alone exceed the cap. Staging
        directories older than :data:`STALE_STAGING_SECONDS` (runs that died
        before committing) go first. Returns the entries deleted.
        """
        if self.root is None or not self.root.exists():
            return 0
        staging_root = self.root / STAGING_DIR
        if staging_root.exists():
            cutoff = time.time() - STALE_STAGING_SECONDS
            for stale in staging_root.iterdir():
                try:
                    if stale.stat().st_mtime < cutoff:
                        shutil.rmtree(stale, ignore_errors=True)
                except OSError:
                    continue
        entries: list[tuple[float, int, Path]] = []
        total = 0
        for path in self.root.rglob("*.pkl"):
            if STAGING_DIR in path.relative_to(self.root).parts:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            total += stat.st_size
            entries.append((stat.st_mtime, stat.st_size, path))
        deleted = 0
        for mtime, size, path in sorted(entries):
            if total <= max_bytes:
                break
            if mtime >= keep_since:
                continue
            path.unlink(missing_ok=True)
            total -= size
            deleted += 1
        if deleted:
            log.info("score cache pruned %d entries to %.0f MB", deleted, total / 1e6)
        return deleted

    def counts(self) -> dict[str, dict[str, int]]:
        """``{component name: {"hits": n, "misses": n}}``."""
        names = sorted(set(self.hits) | set(self.misses))
        return {n: {"hits": self.hits[n], "misses": self.misses[n]} for n in names}


__all__ = [
    "SCORE_CACHE_MAX_BYTES",
    "SCORE_CACHE_VERSION",
    "ScoreCache",
    "code_version",
    "digest",
    "frame_key",
]
