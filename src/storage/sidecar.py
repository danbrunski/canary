"""Per-session JSON sidecars: one reader, one writer, one telemetry preflight.

R01.5 (refactor review 2026-09-24). The "absent → ``None``; ``dict(json.loads
(...))``; except ``(OSError, JSONDecodeError)``" shape had been copied 23
times across 16 files, and only one copy checked that the JSON was an
*object*: every other one raised an uncaught ``ValueError``/``TypeError`` on
a sidecar holding valid non-object JSON (``[]``, ``"x"``, ``3``), the one
malformed case its ``except`` did not name. :func:`read_json_object` is that
check, once.

Leaf module on purpose: it imports nothing from ``src`` beyond
:mod:`src.atomic_write`, so ``analysis``, ``control`` and ``storage`` can
all import it at module level without a cycle.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.atomic_write import atomic_write_text

#: The chunk glob every session reader keys off. Mirrors
#: ``src.storage.session_files.PARQUET_PATTERN``; kept literal here so this
#: module stays a leaf (``session_files`` imports pandas and pyarrow).
TELEMETRY_GLOB = "telemetry-*.parquet"


def read_json_object(path: Path) -> dict[str, Any] | None:
    """The JSON object at ``path``, or ``None`` when absent or unusable.

    ``None`` for a missing file, an unreadable one, malformed JSON, and valid
    JSON that is not an object. A sidecar is always an object; anything else
    is as unusable as a truncated file and must not escape as an exception.
    """
    try:
        text = Path(path).read_text()
    except OSError:  # absent, a directory, unreadable
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def write_json_sidecar(
    path: Path,
    payload: Mapping[str, Any],
    *,
    default: Callable[[Any], Any] | None = None,
    mkdir: bool = False,
) -> Path:
    """Atomically write ``payload`` as indented, key-sorted JSON plus newline.

    The byte shape every session sidecar already had (``classification.json``,
    ``fingerprint.json``, ``dtc.json``, ``digest.json``, ...), over
    :func:`src.atomic_write.atomic_write_text`, so a reader never sees half a
    file.
    """
    text = json.dumps(dict(payload), indent=2, sort_keys=True, default=default) + "\n"
    atomic_write_text(Path(path), text, mkdir=mkdir)
    return Path(path)


def require_telemetry(session_dir: Path) -> list[Path]:
    """The session's telemetry chunks, sorted, or ``FileNotFoundError``.

    The preflight the fingerprint, trip summary and classifier each wrote
    for themselves: a session with no ``telemetry-*.parquet`` has nothing to
    compute over, and saying so is different from computing an empty answer.
    """
    chunks = sorted(Path(session_dir).glob(TELEMETRY_GLOB))
    if not chunks:
        raise FileNotFoundError(f"no telemetry parquet under {session_dir}")
    return chunks


__all__ = [
    "TELEMETRY_GLOB",
    "read_json_object",
    "require_telemetry",
    "write_json_sidecar",
]
