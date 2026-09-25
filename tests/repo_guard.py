"""R07.2 backstop: no test writes into ``<repo>/data`` or ``<repo>/models``.

``DaemonConfig`` defaults 19 writable paths to the repo tree, and scripts
default their outputs there too. On 2026-09-24 ``data/control/trip_odometer.json``
and ``catalog.db`` were found rewritten by a test run (``data/control`` is
gitignored, so nothing showed it), and a stubbed ``retrain.main`` had earlier
grown the real training ledger to 4,252 rows. Remembering to pass a tmp path is
the fix every caller must remember; this makes the write fail instead.

Two layers, both wired from ``tests/conftest.py``:

- :class:`RepoTreeGuard` is a :func:`sys.addaudithook` hook. It refuses (raises
  :class:`RepoTreeWriteError`) an ``open`` for writing, a rename/replace, a
  remove, a mkdir, a truncate or a SQLite connect whose target lies under a
  guarded root, and records it so the test fails even if the code under test
  swallows the exception.
- :func:`snapshot` / :func:`diff_snapshots` compare the files under
  ``data/control`` and ``models`` plus every tracked or untracked-unignored
  file under ``data`` and ``models`` before and after the run. That catches
  what the hook cannot see: a child process, or a native writer (pyarrow
  writes Parquet without Python's ``open``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
GUARDED_ROOTS: tuple[Path, ...] = (REPO / "data", REPO / "models")
_ROOT_STRINGS = tuple(str(root) for root in GUARDED_ROOTS)
_ROOT_PREFIXES = tuple(root + os.sep for root in _ROOT_STRINGS)

#: Test node-id prefixes whose repo-tree writes are let through (and still
#: listed at the end of the run). Each entry names the file, the date and the
#: roadmap item that must delete it; an allowance is a debt, not a policy.
ALLOWANCES: dict[str, str] = {}

_EVENTS = frozenset(
    {
        "open",
        "os.rename",
        "os.remove",
        "os.rmdir",
        "os.mkdir",
        "os.truncate",
        "os.symlink",
        "os.link",
        "shutil.rmtree",
        "shutil.copyfile",
        "sqlite3.connect",
    }
)
_WRITE_MODE_CHARS = frozenset("wax+")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
#: Control state and model artifacts are gitignored, so the snapshot walks them.
_WALKED = (REPO / "data" / "control", REPO / "models")


class RepoTreeWriteError(PermissionError):
    """A test tried to write into the repo's ``data/`` or ``models/`` tree."""


def _as_path(value: Any) -> str | None:
    if isinstance(value, int) or value is None:
        return None
    try:
        raw = os.fspath(value)
    except TypeError:
        return None
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    return raw


def _guarded(raw: str | None) -> str | None:
    if not raw:
        return None
    absolute = os.path.abspath(raw)
    if absolute in _ROOT_STRINGS or absolute.startswith(_ROOT_PREFIXES):
        return absolute
    return None


def _sqlite_target(database: Any) -> str | None:
    raw = _as_path(database)
    if raw is None or raw == ":memory:" or raw == "":
        return None
    if raw.startswith("file:"):
        path, _, query = raw[len("file:") :].partition("?")
        if "mode=ro" in query or "mode=memory" in query:
            return None
        return path
    return raw


def _targets(event: str, args: tuple[Any, ...]) -> list[str]:
    """The paths ``event`` would write, before the guarded-root check."""
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        writes = (
            any(char in _WRITE_MODE_CHARS for char in mode)
            if isinstance(mode, str)
            else isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
        )
        raw = _as_path(path) if writes else None
        return [raw] if raw else []
    if event == "sqlite3.connect":
        raw = _sqlite_target(args[0] if args else None)
        return [raw] if raw else []
    if event in {"os.rename", "os.symlink", "os.link"}:
        return [raw for raw in (_as_path(args[0]), _as_path(args[1])) if raw]
    if event == "shutil.copyfile":
        raw = _as_path(args[1])
        return [raw] if raw else []
    raw = _as_path(args[0]) if args else None
    # A mkdir of an existing directory (or a remove of a missing file) is
    # refused too, although it changes nothing here: whether it exists
    # depends on the checkout. ``data/control`` is gitignored, so it exists in
    # a working tree and not in CI's clean checkout, and a guard that let the
    # no-op through would pass locally and fail there.
    return [] if raw is None else [raw]


class RepoTreeGuard:
    """The audit hook plus the per-test bookkeeping ``conftest`` reads."""

    def __init__(self) -> None:
        self.current_test: str | None = None
        #: node id (``None`` outside a test) -> refused writes.
        self.violations: dict[str | None, list[str]] = {}
        #: node id -> writes let through by :data:`ALLOWANCES`.
        self.allowed: dict[str, list[str]] = {}
        self._expected: list[list[str]] = []
        self._busy = False
        self._installed = False

    def install(self) -> None:
        if not self._installed:
            sys.addaudithook(self.audit)
            self._installed = True

    def allowance_for(self, nodeid: str | None) -> str | None:
        if nodeid is None:
            return None
        for prefix, reason in ALLOWANCES.items():
            if nodeid.startswith(prefix):
                return reason
        return None

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if event not in _EVENTS or self._busy:
            return
        self._busy = True
        try:
            hits = [hit for raw in _targets(event, args) if (hit := _guarded(raw))]
        finally:
            self._busy = False
        if not hits:
            return
        what = f"{event} {', '.join(hits)}"
        if self._expected:
            self._expected[-1].append(what)
        elif self.allowance_for(self.current_test) is not None:
            self.allowed.setdefault(self.current_test or "", []).append(what)
            return
        else:
            self.violations.setdefault(self.current_test, []).append(what)
        raise RepoTreeWriteError(
            f"test wrote into the repo tree ({what}); point the path at tmp_path "
            "(tests/configs.py isolated_config, retrain_argv) -- R07.2"
        )

    @contextmanager
    def expect(self) -> Iterator[list[str]]:
        """Refuse writes inside the block without failing the test.

        For tests that pin the guard itself; yields the refused writes.
        """
        seen: list[str] = []
        self._expected.append(seen)
        try:
            yield seen
        finally:
            self._expected.pop()


GUARD = RepoTreeGuard()


def _git_files(*args: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", *args, "--", "data", "models"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [name for name in out.split("\0") if name]


def snapshot() -> dict[str, tuple[int, int]]:
    """``relative path -> (mtime_ns, size)`` of every file the guard watches."""
    names = set(_git_files()) | set(_git_files("--others", "--exclude-standard"))
    for root in _WALKED:
        if root.is_dir():
            names.update(
                path.relative_to(REPO).as_posix() for path in root.rglob("*") if path.is_file()
            )
    state: dict[str, tuple[int, int]] = {}
    for name in names:
        try:
            info = (REPO / name).stat()
        except OSError:
            continue
        state[name] = (info.st_mtime_ns, info.st_size)
    return state


def _modified_tracked() -> set[str]:
    """Tracked files under ``data/``/``models/`` that differ from the index."""
    return set(_git_files("--modified"))


@dataclass(frozen=True)
class RunBaseline:
    """What the tree looked like when the run started."""

    files: dict[str, tuple[int, int]]
    #: Tracked and unmodified at the start: nobody had them open for editing.
    tracked_clean: frozenset[str]


def baseline() -> RunBaseline:
    return RunBaseline(
        files=snapshot(),
        tracked_clean=frozenset(set(_git_files()) - _modified_tracked()),
    )


def _attributable(name: str, start: RunBaseline) -> bool:
    """Whether a change to ``name`` during the run can only be the run's.

    ``data/control/**`` is daemon state no other session edits on this laptop,
    and a tracked file that was clean at the start had no edit in progress.
    Everything else -- a file another session already had modified, a new
    untracked file under ``data/analyses``, a gitignored model binary -- may
    be a concurrent session's work (the K-line session edits
    ``data/<vehicle>/kline.yaml``; analysis agents write ``data/analyses``),
    so it is reported, not failed. The audit hook still refuses every
    in-process write to any of them; this diff only covers child processes
    and native writers.
    """
    return name.startswith("data/control/") or name in start.tracked_clean


def classify_changes(start: RunBaseline) -> tuple[list[str], list[str]]:
    """``(failures, warnings)``: the run's changes, and changes that may be
    another session's."""
    after = snapshot()
    failures: list[str] = []
    warnings: list[str] = []
    for change in diff_snapshots(start.files, after):
        name = change.split(maxsplit=1)[1]
        (failures if _attributable(name, start) else warnings).append(change)
    return failures, warnings


def diff_snapshots(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[str]:
    """Human-readable changes between two :func:`snapshot` results."""
    changes = [f"created  {name}" for name in sorted(after.keys() - before.keys())]
    changes += [f"deleted  {name}" for name in sorted(before.keys() - after.keys())]
    changes += [
        f"modified {name}"
        for name in sorted(before.keys() & after.keys())
        if before[name] != after[name]
    ]
    return changes
