"""Filesystem defaults shared by dashboard readers and app assembly."""

from __future__ import annotations

from pathlib import Path

# Where the React SPA's built artifacts land. Anchored to this package so the
# daemon can run from systemd, Docker, or scripts without relying on cwd.
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static" / "dist"

# Repo root, anchored to this file (``src/dashboard/paths.py`` -> up 3). These
# catalog files are version-controlled and ship in the clone.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Project hand-edited CAN signal bindings used by dashboard display readers.
DEFAULT_BINDINGS_PATH = _REPO_ROOT / "data" / "bindings.yaml"

# Canonical Nissan Mode-22 DID catalog.
DEFAULT_DIDS_PATH = _REPO_ROOT / "data" / "dids.yaml"

# Scanned directory of YAML test-session profiles for the Test panel.
DEFAULT_TEST_PROFILES_DIR = _REPO_ROOT / "data" / "test_profiles"

__all__ = [
    "DEFAULT_BINDINGS_PATH",
    "DEFAULT_DIDS_PATH",
    "DEFAULT_STATIC_DIR",
    "DEFAULT_TEST_PROFILES_DIR",
]
