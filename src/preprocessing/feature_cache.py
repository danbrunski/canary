"""Feature-frame cache keys and cache writes for retraining."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CanaryConfig
from src.decode.behavior_models import DEFAULT_BEHAVIOR_MODELS_PATH
from src.decode.dbc import DEFAULT_DBC_DIR
from src.preprocessing.loader import PARQUET_PATTERN

log = logging.getLogger(__name__)

DEFAULT_BINDINGS_PATH = Path("data/bindings.yaml")
DEFAULT_DIDS_PATH = Path("data/dids.yaml")
#: Bump whenever a *code* change alters the feature frame for unchanged inputs.
#: The rest of the cache key hashes inputs only (session file stats, config,
#: bindings/dbc/dids), so without this a preprocessing fix would silently serve
#: pre-fix frames forever.
#:
#: 2 — resampler tz fix: sessions whose trailing-only channels used to raise
#:     "Cannot subtract tz-naive and tz-aware" were dropped from the corpus
#:     entirely, so cached frames are missing them.
#: 3 — ``RUN_TIME`` reclassified slow (``DEFAULT_SLOW_CHANNEL_HINTS``): its
#:     ``__gap`` flag was set on 82.5% of rows under the fast 2 s ceiling, which
#:     vetoed every cold-start warmup recipe. Cached frames carry the old flags,
#:     so reusing them would silently keep the veto in place.
FEATURE_FRAME_CACHE_VERSION = 3


def build_feature_frame_cache_key(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    behavior_models_path: Path = DEFAULT_BEHAVIOR_MODELS_PATH,
    dbc_dir: Path = DEFAULT_DBC_DIR,
) -> str:
    """Return the deterministic cache key for a feature-frame build."""
    return str(
        _feature_frame_cache_metadata(
            [Path(session_dir) for session_dir in session_dirs],
            config,
            bindings_path=bindings_path,
            behavior_models_path=behavior_models_path,
            dbc_dir=dbc_dir,
        )["cache_key"]
    )


def _feature_frame_cache_metadata(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path,
    behavior_models_path: Path,
    dbc_dir: Path,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cache_version": FEATURE_FRAME_CACHE_VERSION,
        "sessions": [_session_cache_entry(session_dir) for session_dir in session_dirs],
        "preprocessing": config.preprocessing.model_dump(mode="json"),
        "bindings": _file_cache_entry(bindings_path),
        "behavior_models": _file_cache_entry(behavior_models_path),
        "dbc": _directory_cache_entry(dbc_dir, pattern="*.dbc"),
        # Hash the DID map so editing dids.yaml (new DID, fixed scale)
        # invalidates cached frames that decoded did:* columns.
        "dids": _file_cache_entry(DEFAULT_DIDS_PATH),
    }
    return {**payload, "cache_key": _stable_hash(payload)}


def _session_cache_entry(session_dir: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for file in sorted(session_dir.glob(PARQUET_PATTERN)):
        try:
            stat = file.stat()
        except OSError:
            continue
        files.append(
            {
                "name": file.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return {
        "path": _stable_path(session_dir),
        "name": session_dir.name,
        "files": files,
    }


def _directory_cache_entry(directory: Path, *, pattern: str) -> dict[str, Any]:
    if not directory.exists():
        return {"path": _stable_path(directory), "exists": False, "files": []}
    files = [_file_cache_entry(file) for file in sorted(directory.glob(pattern)) if file.is_file()]
    return {"path": _stable_path(directory), "exists": True, "files": files}


def _file_cache_entry(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"path": _stable_path(path), "exists": False}
    stat = path.stat()
    return {
        "path": _stable_path(path),
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _file_sha256(path),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_path(path: Path) -> str:
    return str(path.expanduser().resolve())


def _stable_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_feature_frame_cache(
    cache_path: Path,
    features: pd.DataFrame,
    metadata: Mapping[str, Any],
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
    meta_path = cache_path.with_suffix(".json")
    meta_tmp_path = meta_path.with_name(f".{meta_path.name}.{os.getpid()}.tmp")
    try:
        features.to_pickle(tmp_path)
        tmp_path.replace(cache_path)
        sidecar = {
            **dict(metadata),
            "rows": int(len(features)),
            "columns": [str(column) for column in features.columns],
        }
        meta_tmp_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n")
        meta_tmp_path.replace(meta_path)
        log.info(
            "wrote feature frame cache",
            extra={
                "cache": str(cache_path),
                "rows": len(features),
                "columns": len(features.columns),
            },
        )
    except Exception as exc:  # noqa: BLE001 - cache write failures should not fail training
        for path in (tmp_path, meta_tmp_path):
            try:
                path.unlink()
            except OSError as cleanup_exc:
                log.debug(
                    "failed to remove incomplete feature-frame cache file",
                    extra={"path": str(path), "error": str(cleanup_exc)},
                )
        log.warning(
            "feature frame cache write failed; continuing without cache",
            extra={"cache": str(cache_path), "error": str(exc)},
        )


__all__ = [
    "FEATURE_FRAME_CACHE_VERSION",
    "build_feature_frame_cache_key",
    "_feature_frame_cache_metadata",
    "_write_feature_frame_cache",
]
