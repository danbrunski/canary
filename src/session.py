"""Recording-session filesystem conventions and tag windows."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Any

FRAMES_FILENAME = "frames.log"
MANIFEST_FILENAME = "manifest.json"
PIDS_FILENAME = "pids.jsonl"
TAGS_FILENAME = "tags.jsonl"


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


def make_session_dir(out_root: Path, started_at: datetime) -> SessionPaths:
    """Create `out_root/<UTC-timestamp>/` and return the file layout.

    Timestamp uses `T` separator and no colons so the path is safe on every
    filesystem we care about (tmpfs, ext4, exFAT, NTFS via WSL).
    """
    ts = started_at.strftime("%Y-%m-%dT%H%M%S")
    root = out_root / ts
    root.mkdir(parents=True, exist_ok=False)
    return session_paths(root)


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
