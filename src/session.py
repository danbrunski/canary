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
    """Load JSONL tags from `path`, returning an empty list when absent."""
    if not path.exists():
        return []
    tags: list[dict[str, Any]] = []
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
                tags.append(record)
    return tags


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
