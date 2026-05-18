"""Best-effort version helpers for dashboard metadata endpoints."""

from __future__ import annotations

import subprocess


def _canary_version() -> str:
    """Best-effort package version for the OpenAPI doc."""
    try:
        from importlib.metadata import version

        return version("canary")
    except Exception:  # noqa: BLE001 — anything goes wrong, fall back
        return "0.0.0"


def _git_sha() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


__all__ = ["_canary_version", "_git_sha"]
