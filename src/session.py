"""Recording-session filesystem conventions and tag windows."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from json import JSONDecodeError
from pathlib import Path
from typing import Any

FRAMES_FILENAME = "frames.log"
MANIFEST_FILENAME = "manifest.json"
PIDS_FILENAME = "pids.jsonl"
TAGS_FILENAME = "tags.jsonl"
#: The DTC poll's session sidecar: written by ``src.control.dtc_state``, read
#: by the digest and the session catalog.
DTC_SIDECAR_FILENAME = "dtc.json"

# Written by record.py at startup, removed on clean exit. Gives an
# out-of-band controller (scripts/stop_recording.py, a supervisor) a stable
# handle on the live recorder so it never has to guess the PID via `pgrep`
# — which is fragile when the process runs under a shell/eval wrapper or
# when more than one session is on disk. Not part of SessionPaths on
# purpose: it's a transient runtime artifact, not session data, and adding
# a field to the frozen dataclass would ripple through every consumer.
PID_FILENAME = "recorder.pid"


@dataclass(frozen=True)
class SessionPaths:
    """File layout for a single recording session."""

    root: Path
    frames: Path
    manifest: Path
    pids: Path
    tags: Path


@dataclass(frozen=True)
class TagWindow:
    """A [start, end) interval defined by a start tag and the next tag."""

    label: str
    start: float
    end: float


def session_paths(root: Path) -> SessionPaths:
    """Return the conventional file layout for an existing session directory."""
    return SessionPaths(
        root=root,
        frames=root / FRAMES_FILENAME,
        manifest=root / MANIFEST_FILENAME,
        pids=root / PIDS_FILENAME,
        tags=root / TAGS_FILENAME,
    )


#: The UTC stamp every session directory name carries. ``T`` separator and
#: no colons so the path is safe on every filesystem we care about (tmpfs,
#: ext4, exFAT, NTFS via WSL). The only copy of this format in ``src``,
#: ``scripts`` and ``tools`` (R01.1): format with
#: :func:`format_session_timestamp`, parse with :func:`classify_session_name`.
SESSION_TIMESTAMP_FORMAT = "%Y-%m-%dT%H%M%S"
_STAMP = r"(\d{4}-\d{2}-\d{2}T\d{6})"


class SessionKind(StrEnum):
    """What a directory under the storage root is, decided by its name alone.

    - ``live``: a plain session the writer rotates into (``2026-08-05T142720``).
    - ``capture``: a profile-owned capture (``_capture-<slug>-<stamp>``).
    - ``tuning``: legacy tuning mode (``_tuning-<stamp>``,
      ``ParquetTelemetryWriter.enter_tuning_mode``).
    - ``replay``: runtime replay output (``_replay-<stamp>``). Re-emitted rows,
      never this vehicle's own recording.
    """

    LIVE = "live"
    CAPTURE = "capture"
    TUNING = "tuning"
    REPLAY = "replay"


_KIND_PATTERNS: tuple[tuple[SessionKind, re.Pattern[str]], ...] = (
    (SessionKind.LIVE, re.compile(rf"^{_STAMP}$")),
    (SessionKind.CAPTURE, re.compile(rf"^_capture-[a-z0-9-]+-{_STAMP}$")),
    (SessionKind.TUNING, re.compile(rf"^_tuning-{_STAMP}$")),
    (SessionKind.REPLAY, re.compile(rf"^_replay-{_STAMP}$")),
)

#: Every kind a name can classify as. "What is live right now?" answers over
#: all of them (:func:`src.storage.sessions.latest_session`).
ALL_SESSION_KINDS: frozenset[SessionKind] = frozenset(SessionKind)
#: This vehicle's own recordings that HTTP reads resolve and post-hoc sidecars
#: are computed over (``is_session_dir_name``). ``_tuning-*`` counts (operator
#: decision 2026-09-25, R01.1): it is this vehicle's own recording. It stays
#: out of training through the ``_`` prefix (``discover_sessions``) and out of
#: detection through R03.12's profile gate — not through this set.
RECORDED_SESSION_KINDS: frozenset[SessionKind] = frozenset(
    {SessionKind.LIVE, SessionKind.CAPTURE, SessionKind.TUNING}
)
#: Plain live sessions only: the corpus-shaped directories the classifier,
#: trends and retention walk.
LIVE_SESSION_KINDS: frozenset[SessionKind] = frozenset({SessionKind.LIVE})


@dataclass(frozen=True)
class SessionName:
    """A parsed session directory name."""

    kind: SessionKind
    started_at: datetime


def format_session_timestamp(started_at: datetime) -> str:
    """The directory stamp for ``started_at`` (formatted as given; pass UTC)."""
    return started_at.strftime(SESSION_TIMESTAMP_FORMAT)


def parse_session_timestamp(stamp: str) -> datetime | None:
    """Parse a bare ``YYYY-MM-DDTHHMMSS`` stamp as UTC, or ``None``."""
    try:
        return datetime.strptime(stamp, SESSION_TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def classify_session_name(name: str) -> SessionName | None:
    """The one parser for session directory names: kind and UTC start time.

    ``None`` for anything that is not a session (``_archive``, scratch
    directories, a stamp that is not a real date). Ordering sessions any
    other way (by directory name, by mtime) is a bug waiting for a capture
    prefix to expose it: ``_capture-`` sorts before ``_tuning-`` whatever the
    dates.
    """
    for kind, pattern in _KIND_PATTERNS:
        match = pattern.match(name)
        if match is None:
            continue
        started = parse_session_timestamp(match.group(1))
        return None if started is None else SessionName(kind=kind, started_at=started)
    return None


def session_kind(name: str) -> SessionKind | None:
    """The :class:`SessionKind` of a directory name, or ``None``."""
    parsed = classify_session_name(name)
    return None if parsed is None else parsed.kind


#: Simulator output (E07): a name under this prefix is a synthetic scenario's
#: trace, never a recording. The leading ``_`` keeps it out of training
#: discovery; plant identification/evaluation and the live memory ledger refuse
#: it by name (:func:`is_synthetic_session`).
SYNTHETIC_SESSION_PREFIX = "_sim-"


def is_synthetic_session(name: str) -> bool:
    """Whether ``name`` labels simulator output rather than a recording."""
    return name.startswith(SYNTHETIC_SESSION_PREFIX)


_NEVER = datetime.min.replace(tzinfo=UTC)


def session_sort_key(session: str | Path) -> tuple[datetime, str]:
    """Chronological sort key, total over unparseable names.

    Parsed start time first, then the name as a tie-break. A name that does
    not parse sorts before every session (``datetime.min``) instead of raising
    ``TypeError`` beside parseable ones.
    """
    name = session.name if isinstance(session, Path) else session
    parsed = classify_session_name(name)
    return (parsed.started_at if parsed is not None else _NEVER, name)


def iter_sessions(root: Path, *, kinds: Iterable[SessionKind]) -> list[Path]:
    """Session directories under ``root`` of the given ``kinds``, oldest first.

    Ordered by parsed start time, never by name. ``kinds`` has no default on
    purpose: "which sessions?" is a different question for training, live
    reads and sidecars, and each caller states its answer.
    """
    wanted = frozenset(kinds)
    if not root.is_dir():
        return []
    found = [
        child
        for child in root.iterdir()
        if (parsed := classify_session_name(child.name)) is not None
        and parsed.kind in wanted
        and child.is_dir()
    ]
    return sorted(found, key=session_sort_key)


def dir_size(path: Path) -> int:
    """Total bytes of the regular files under ``path``; unreadable files count 0."""
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def make_session_dir(out_root: Path, started_at: datetime) -> SessionPaths:
    """Create `out_root/<UTC-timestamp>/` and return the file layout.

    Refuses (``FileExistsError``) when that second's directory exists: reusing
    it would mix two recordings. A writer that must open a session whatever
    the clock says uses :func:`make_unique_session_dir`.
    """
    ts = format_session_timestamp(started_at)
    root = out_root / ts
    root.mkdir(parents=True, exist_ok=False)
    return session_paths(root)


#: How many seconds :func:`make_unique_session_dir` bumps before giving up.
UNIQUE_SESSION_DIR_ATTEMPTS = 60


def make_unique_session_dir(out_root: Path, started_at: datetime) -> SessionPaths:
    """Create a fresh session directory at ``started_at``, or the first free
    second after it.

    Session directories are named to the second, so a daemon restart, a
    record-gap or run-time rotation, or a capture ending inside the same
    wall-clock second as the previous session's start found its name taken
    and ``make_session_dir`` raised; on 2026-09-25 that killed a restarted
    daemon before any collector ran (T01.11). The bumped name is still a
    plain, parseable stamp, and it sorts after the directory it avoided under
    :func:`session_sort_key`. Each attempt is an exclusive ``mkdir``, so two
    writers racing for one second cannot both get it.
    """
    attempt = started_at
    for _ in range(UNIQUE_SESSION_DIR_ATTEMPTS):
        try:
            return make_session_dir(out_root, attempt)
        except FileExistsError:
            attempt += timedelta(seconds=1)
    raise FileExistsError(
        f"no free session directory under {out_root} within {UNIQUE_SESSION_DIR_ATTEMPTS} s "
        f"of {format_session_timestamp(started_at)}"
    )


_isolated_id_lock = threading.Lock()
_last_isolated_id: datetime | None = None


def issue_isolated_id(now: datetime | None = None) -> str:
    """A stamp for an isolated directory (``_capture-<profile>-<id>``,
    ``_tuning-<id>``, ``_replay-<id>``) that this process has never issued.

    The ids were ``format_session_timestamp(now)``, so two captures started in
    one wall-clock second named the same directory, and the second silently
    overwrote the first's ``telemetry-00000.parquet`` and sidecar (D03.32).
    Each call now returns ``now`` or, when that second is already issued, the
    second after the last one issued: still a plain, parseable stamp, still in
    start order under :func:`session_sort_key`. A directory left by an earlier
    process in the same second is the writer's to refuse
    (``ParquetTelemetryWriter`` will not enter a non-empty isolated directory).
    """
    global _last_isolated_id
    wanted = (datetime.now(UTC) if now is None else now).astimezone(UTC).replace(microsecond=0)
    with _isolated_id_lock:
        if _last_isolated_id is not None and wanted <= _last_isolated_id:
            wanted = _last_isolated_id + timedelta(seconds=1)
        _last_isolated_id = wanted
    return format_session_timestamp(wanted)


def load_tags(path: Path) -> list[dict[str, Any]]:
    """Load JSONL tags from `path`, returning an empty list when absent.

    Applies corrections recorded via `kind="invalidate"` tags: each invalidate
    record names a target tag (by `target_label`, optionally narrowed by
    `target_t`); matching event tags are dropped, and the invalidate records
    themselves are filtered out of the result. This lets a user post-hoc say
    "the AC didn't actually turn on when I tagged hvac_ac_on" without losing
    history — tags.jsonl stays append-only.

    The intended driver: a tag fires but the physical action didn't happen at
    that moment (e.g. AC failed to engage, sharp turn aborted because of
    traffic). `scripts/correct_tag.py` appends an invalidate; subsequent
    analyzer runs see the corrected window list.
    """
    if not path.exists():
        return []
    raw: list[dict[str, Any]] = []
    with path.open("r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except JSONDecodeError:
                continue
            if isinstance(record, dict):
                try:
                    float(record["t"])
                    str(record["label"])
                except (KeyError, TypeError, ValueError):
                    continue
                raw.append(record)
    return _apply_invalidations(raw)


def _apply_invalidations(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter event tags suppressed by invalidate records; drop the records too.

    An invalidate record looks like::

        {"t": ..., "kind": "invalidate", "target_label": "hvac_ac_on",
         "target_t": <orig_t or omitted>, "reason": "..."}

    If `target_t` is present, only the event tag with that exact (label, t)
    is dropped. If absent, every event tag matching `target_label` is dropped.
    Invalidate records lacking `target_label` are ignored (defensive — we
    won't silently drop everything because of a malformed correction).
    """
    invalidate_all_by_label: set[str] = set()
    invalidate_by_label_and_t: set[tuple[str, float]] = set()
    for record in raw:
        if record.get("kind") != "invalidate":
            continue
        target_label = record.get("target_label")
        if not isinstance(target_label, str):
            continue
        target_t = record.get("target_t")
        if target_t is None:
            invalidate_all_by_label.add(target_label)
        else:
            try:
                invalidate_by_label_and_t.add((target_label, float(target_t)))
            except (TypeError, ValueError):
                continue

    kept: list[dict[str, Any]] = []
    for record in raw:
        if record.get("kind") == "invalidate":
            continue  # corrections are metadata, not windows
        label = str(record["label"])
        if label in invalidate_all_by_label:
            continue
        if (label, float(record["t"])) in invalidate_by_label_and_t:
            continue
        kept.append(record)
    return kept


def build_windows(
    tags: Sequence[Mapping[str, Any]],
    session_end: float,
    *,
    session_start: float | None = None,
) -> list[TagWindow]:
    """Each tag opens a window that closes when the next tag fires."""
    ordered = sorted(tags, key=lambda tag: float(tag["t"]))
    windows: list[TagWindow] = []
    if not ordered:
        return windows

    if session_start is not None and session_start < float(ordered[0]["t"]):
        windows.append(
            TagWindow(label="(pre-tags)", start=session_start, end=float(ordered[0]["t"]))
        )

    previous_start: float | None = None
    for i, tag in enumerate(ordered):
        start = float(tag["t"])
        if previous_start is not None and start <= previous_start:
            raise ValueError("tag timestamps must be strictly increasing")
        previous_start = start
        end = float(ordered[i + 1]["t"]) if i + 1 < len(ordered) else session_end
        windows.append(TagWindow(label=str(tag["label"]), start=start, end=end))
    return windows
