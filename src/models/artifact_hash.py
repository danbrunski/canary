"""Shared artifact-file sha256 helper (Task 6.6).

One hash function used on both ends of the deploy-skew check: the training
ledger hashes the artifact at train time (:mod:`src.models.training_ledger`);
``scripts/verify_artifacts.py`` recomputes it locally/remotely at compare
time. Keeping it in one place means "sha256 of the artifact" means the same
thing everywhere instead of two subtly different implementations drifting.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = ["sha256_file"]


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
