"""Provenance stamps the behavior-surface fitters write on every fit (R05.14).

``surface_1d`` and ``surface_2d`` each carried a private copy of the
training-data hash and the fit timestamp; one home keeps a 1D and a 2D refit of
the same rows hashing the same way.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import numpy as np


def training_data_hash(*columns: np.ndarray) -> str:
    """Stable hash of the training rows, so a refit on the same data has the same hash."""
    digest = hashlib.sha256()
    for column in columns:
        digest.update(column.tobytes())
    return f"sha256:{digest.hexdigest()[:16]}"


def fitted_now() -> str:
    """The fit timestamp: UTC now, ISO 8601."""
    return datetime.now(UTC).isoformat()


__all__ = ["fitted_now", "training_data_hash"]
