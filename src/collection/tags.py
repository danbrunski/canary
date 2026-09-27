"""Tag sidecar for recording sessions.

A tag is a labeled event (`{"t": <unix-ts>, "kind": ..., "label": ..., ...}`)
appended to `<session>/tags.jsonl` while a recording is in progress. Tags are
how the RE runner will mark phase boundaries ("baseline_start",
"action_start", "action_end") so the analyzer can later diff bus state across
labeled windows.

Timestamps use `time.time()` — the same wall clock python-can stamps
`frames.log` with — so tag timestamps align with frame timestamps directly,
no clock conversion needed.

Tags are written independently of the recorder process. That means:
  * the tagger and recorder don't need IPC;
  * a human can append tags from a second terminal during a live recording;
  * if no recording is running, tags still write — they just won't have
    matching frames.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from src.session import TAGS_FILENAME, SessionKind, iter_sessions

#: A tag records a physical action on the vehicle, so it belongs in a
#: recording of the vehicle: never in ``_replay-*`` output, whose rows are
#: re-emitted from another session.
TAGGABLE_SESSION_KINDS = frozenset({SessionKind.LIVE, SessionKind.CAPTURE, SessionKind.TUNING})


def write_tag(
    session_dir: Path,
    label: str,
    *,
    kind: str = "event",
    timestamp: float | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Append one tag to `<session_dir>/tags.jsonl` and return the record.

    `timestamp` defaults to `time.time()`. Pass an explicit value only when
    backfilling tags from an offline source — for live tagging the default
    is what you want (it's the same clock as `frames.log`).

    `kind` and `label` are protected by being named parameters; `t` would
    otherwise sneak through via ``**fields`` and silently overwrite the
    record's timestamp, so we reject it explicitly.
    """
    if "t" in fields:
        raise ValueError("reserved tag field 't' cannot be overridden; use timestamp= kwarg")

    record: dict[str, Any] = {
        "t": time.time() if timestamp is None else timestamp,
        "kind": kind,
        "label": label,
        **fields,
    }
    path = session_dir / TAGS_FILENAME
    # Open append-text and flush each line so tags are durable immediately —
    # if the recorder is killed, we don't lose the last few labels to a
    # buffered stdout.
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
    return record


def write_correction(
    session_dir: Path,
    target_label: str,
    *,
    target_t: float | None = None,
    reason: str | None = None,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Append a `kind="invalidate"` correction to a session's tags.jsonl.

    `target_label` names the original event tag's label. Pass `target_t` to
    invalidate just one occurrence (the most precise option); omit it to
    invalidate every tag with that label in the session (the easy option,
    appropriate when the protocol step never produced the intended action).
    `reason` is free-form annotation — surfaced by the analyzer when it
    explains which windows it dropped.

    The correction record itself never becomes a window — `load_tags` filters
    it out — but it stays in tags.jsonl so the audit trail is intact.
    """
    fields: dict[str, Any] = {"target_label": target_label}
    if target_t is not None:
        fields["target_t"] = float(target_t)
    if reason is not None:
        fields["reason"] = reason
    # label= is just for human readability when grepping tags.jsonl; the
    # analyzer keys off (kind, target_label, target_t).
    label = f"invalidate:{target_label}"
    return write_tag(
        session_dir,
        label,
        kind="invalidate",
        timestamp=timestamp,
        **fields,
    )


def find_latest_session(sessions_root: Path) -> Path:
    """Return the session being recorded now under `sessions_root`.

    Used by the `tag` CLI so the user doesn't have to copy-paste the
    timestamped path while a recording is in progress. "Which session is
    live?" includes captures (R01.1): the previous plain-name-only
    ``max(name)`` tagged the *previous* session for the whole of a
    ``_capture-*`` recording. Newest by parsed start time, over
    :data:`TAGGABLE_SESSION_KINDS`.

    Not :func:`src.storage.sessions.latest_session`: that one requires a
    flushed telemetry chunk, which a ``record.py`` session (``frames.log``)
    never has and a just-started daemon session does not have yet, and a tag
    written in those first seconds belongs in the new session.
    """
    if not sessions_root.is_dir():
        raise FileNotFoundError(f"sessions root does not exist: {sessions_root}")
    candidates = iter_sessions(sessions_root, kinds=TAGGABLE_SESSION_KINDS)
    if not candidates:
        raise FileNotFoundError(f"no session directories in {sessions_root}")
    return candidates[-1]
