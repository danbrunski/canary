"""Filesystem defaults shared by dashboard readers and app assembly."""

from __future__ import annotations

from pathlib import Path

from src.paths import BINDINGS_YAML, DIDS_YAML, TEST_PROFILES_DIR

# Where the React SPA's built artifacts land. Anchored to this package so the
# daemon can run from systemd, Docker, or scripts without relying on cwd.
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static" / "dist"

# The repo-rooted catalog defaults live in ``src.paths`` (R01.2); these names
# re-export them for the dashboard's existing importers.
DEFAULT_BINDINGS_PATH = BINDINGS_YAML
DEFAULT_DIDS_PATH = DIDS_YAML
DEFAULT_TEST_PROFILES_DIR = TEST_PROFILES_DIR

__all__ = [
    "DEFAULT_BINDINGS_PATH",
    "DEFAULT_DIDS_PATH",
    "DEFAULT_STATIC_DIR",
    "DEFAULT_TEST_PROFILES_DIR",
]
