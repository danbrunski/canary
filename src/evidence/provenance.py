"""Provenance helpers for tools that mint evidence (E01.2).

Stdlib only. ``code_version`` hashes the producing modules' own source bytes,
so it is the same on the Pi and the laptop for the same code and needs no git
checkout (the Pi's tree may be a bundle merge; a git sha says nothing about
uncommitted edits either).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import ModuleType
from typing import Any

from src.artifact_hash import sha256_json

__all__ = ["code_version", "hash_json", "hash_session_files"]


def code_version(*modules: ModuleType) -> str:
    """sha256 over the named modules' source files (name + bytes), in order."""
    if not modules:
        raise ValueError("code_version needs the modules that produced the values")
    digest = hashlib.sha256()
    for module in modules:
        source = getattr(module, "__file__", None)
        if source is None:
            raise ValueError(f"{module.__name__} has no source file to version")
        digest.update(module.__name__.encode())
        digest.update(hashlib.sha256(Path(source).read_bytes()).digest())
    return digest.hexdigest()


def hash_json(payload: Any) -> str:
    """sha256 of a JSON-able payload in canonical form."""
    return sha256_json(payload, default=str)


def hash_session_files(fingerprint: dict[str, Any]) -> str:
    """sha256 of a session's chunk list by name and size.

    Takes ``src.storage.session_files.session_files_fingerprint`` output but
    drops the resolved path and mtimes: a session pulled to the laptop by tar
    is the same input as on the Pi, wherever it lives.
    """
    files = [{"name": item["name"], "size": item["size"]} for item in fingerprint.get("files", [])]
    return hash_json({"name": fingerprint.get("name"), "files": files})
