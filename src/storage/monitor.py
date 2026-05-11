"""Storage capacity checks for the collection daemon."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

BYTES_PER_MIB = 1024 * 1024


def _existing_path(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def free_space_mb(path: Path) -> float:
    """Return free MiB for the filesystem containing path or its nearest parent."""
    usage = shutil.disk_usage(_existing_path(path))
    return usage.free / BYTES_PER_MIB


def warn_if_storage_low(path: Path, warning_mb: int) -> float:
    """Log a warning when the filesystem containing path is below the threshold."""
    free_mb = free_space_mb(path)
    if free_mb < warning_mb:
        log.warning(
            "storage space below warning threshold",
            extra={
                "path": str(path),
                "free_mb": round(free_mb, 1),
                "warning_mb": warning_mb,
            },
        )
    return free_mb
