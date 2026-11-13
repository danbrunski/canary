"""Shared artifact-file sha256 helper (Task 6.6).

One hash function used on both ends of the deploy-skew check: the training
ledger hashes the artifact at train time (:mod:`src.models.training_ledger`);
``scripts/verify_artifacts.py`` recomputes it locally/remotely at compare
time. Keeping it in one place means "sha256 of the artifact" means the same
thing everywhere instead of two subtly different implementations drifting.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["sha256_bytes", "sha256_file", "sha256_file_or_none", "sha256_json"]


def sha256_bytes(data: bytes) -> str:
    """sha256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_json(
    payload: Any, *, compact: bool = True, default: Callable[[Any], Any] | None = None
) -> str:
    """sha256 hex digest of ``payload`` as sorted-key JSON, UTF-8 encoded.

    The hash behind cache keys, ledger ids, evidence stamps and scenario ids
    (R24.5): thirty-odd copies of ``hashlib.sha256(json.dumps(...))``, in
    flavors that look alike and are not. Every stored hash depends on its
    flavor, so the two things that differ are parameters rather than copies:

    - ``compact`` picks the separators: ``(",", ":")`` when true,
      ``json.dumps``'s own ``(", ", ": ")`` when false.
    - ``default`` is ``json.dumps(default=)``, for payloads holding values
      JSON cannot encode (``str`` is the usual one).

    ``ensure_ascii`` and ``allow_nan`` are left at ``json.dumps``'s defaults;
    a site that needs another variant keeps its own encoder (``SimDesign``
    refuses NaN) rather than growing a flag here. A caller must keep passing
    the flavor its stored hashes were made with: changing either parameter
    changes every digest.
    """
    separators = (",", ":") if compact else None
    text = json.dumps(payload, sort_keys=True, separators=separators, default=default)
    return sha256_bytes(text.encode())


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Streaming sha256 hex digest of ``path``'s contents.

    Streamed rather than a single ``read_bytes()`` — LSTM/TCN artifacts can
    run into the tens of MB and this keeps memory flat regardless of size.
    """
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file_or_none(path: Path | None) -> str | None:
    """:func:`sha256_file`, or ``None`` when ``path`` is ``None``, not a regular
    file, or unreadable.

    The "hash it if it is there" form six callers had each written for
    themselves (R01.6), one of which read the whole file into memory.
    """
    if path is None or not Path(path).is_file():
        return None
    try:
        return sha256_file(path)
    except OSError:
        return None
