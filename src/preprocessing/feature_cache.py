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

from src.artifact_hash import sha256_file
from src.config import CanaryConfig, require_profile_scoped
from src.decode.dbc import DEFAULT_DBC_DIR
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.storage.session_files import PARQUET_PATTERN
from src.vehicle.profiles import active_registry_sources

log = logging.getLogger(__name__)

#: Repo-rooted (R01.2): the cache key must hash the file the build loads, not
#: whatever ``data/dids.yaml`` happens to sit under the working directory.
DEFAULT_BINDINGS_PATH = BINDINGS_YAML
DEFAULT_DIDS_PATH = DIDS_YAML
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
#: 4 — operating-mode segmenter scales its ``decoded:`` fallbacks (raw binding
#:     counts) into engineering units before the thresholds see them. Cached
#:     frames carry the 2026-08-06..09-18 labels, where broadcast-labelled rows
#:     read 8x rpm / 16x km/h / degC + 40: on 2026-08-06T162657 (21% of rows on
#:     the rpm fallback, 23% on speed) 15.9% of labels change — 500 rows move
#:     highway -> city and 91 unknown -> idle_transient.
#: 5 — A03.1: the resampler (and derived channels) emit per-channel
#:     ``__arrived`` flags. A cached frame without them would train a
#:     sequence model whose "arrived" mask falls back to freshness everywhere.
#: 6 — P02.10: broadcast channels (``can:``/``decoded:``/``dbc:``) take the
#:     broadcast stale class (``collection.broadcast_stale_timeout_seconds``,
#:     10 s) instead of the fast 2 s one, and their ``__arrived`` flag reads "a
#:     stored row within that timeout". Cached frames carry the old flags.
#: 7 — P02.11: featurization reads the vehicle's ``afr_calibration.yaml`` and
#:     parameters through the profile-scoped config. Trainer frames were built
#:     through the unscoped one, without the calibration (no ``TARGET_AFR`` /
#:     ``AFR_ERROR_B*``) and with the ``vehicle:`` block's parameters; the key
#:     now hashes both, and frames cached before it are not reused.
#: 8 — P02.12: the behavior models default to the scoped
#:     ``daemon.behavior_models``. Trainer frames were built against the absent
#:     repo ``data/behavior_models.yaml``, with no ``deviation:*`` columns.
#:     P02.13: ``derived:TARGET_AFR`` / ``AFR_ERROR_B*`` are no longer looked up
#:     with ``obd:ENGINE_LOAD``, so cached frames carry values the code no
#:     longer produces.
#: 9 — P02.14: ``derived:BASE_FUEL_SCHEDULE`` (DID 0x1208 in ms) is the
#:     Xterra's verified load axis, so frames gain it and, where ``obd:RPM`` is
#:     present, ``derived:TARGET_AFR`` / ``AFR_ERROR_B*``. The vehicle module
#:     that declares it is not a hashed input, so the version carries it.
#: 10 — P02.15 / P02.16: ``derived:TARGET_AFR`` clamps a load below the map's
#:     first breakpoint where the map declares it, and each row's map is the
#:     revision flashed at its timestamp, not ``active_revision``. The key now
#:     hashes the flash-history files too (``tune_revisions``).
FEATURE_FRAME_CACHE_VERSION = 10


def build_feature_frame_cache_key(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    behavior_models_path: Path | None = None,
    dbc_dir: Path = DEFAULT_DBC_DIR,
    dids_path: Path = DEFAULT_DIDS_PATH,
) -> str:
    """Return the deterministic cache key for a feature-frame build.

    ``behavior_models_path`` defaults, as :func:`build_feature_frame`'s does,
    to the scoped ``config.daemon.behavior_models`` (P02.12).
    """
    require_profile_scoped(config, reader="feature-frame cache key")
    return str(
        _feature_frame_cache_metadata(
            [Path(session_dir) for session_dir in session_dirs],
            config,
            bindings_path=bindings_path,
            behavior_models_path=(
                config.daemon.behavior_models
                if behavior_models_path is None
                else behavior_models_path
            ),
            dbc_dir=dbc_dir,
            dids_path=dids_path,
        )["cache_key"]
    )


def _feature_frame_cache_metadata(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path,
    behavior_models_path: Path,
    dbc_dir: Path,
    dids_path: Path = DEFAULT_DIDS_PATH,
) -> dict[str, Any]:
    # The key hashes the calibration the build reads, which only the scoped
    # config names (P02.11).
    require_profile_scoped(config, reader="feature-frame cache key")
    payload: dict[str, Any] = {
        "cache_version": FEATURE_FRAME_CACHE_VERSION,
        "sessions": [_session_cache_entry(session_dir) for session_dir in session_dirs],
        "preprocessing": config.preprocessing.model_dump(mode="json"),
        # P02.10: the broadcast stale class lives beside the storage setting it
        # must equal, outside ``preprocessing``, so it is hashed by name.
        "broadcast_stale_timeout_seconds": config.collection.broadcast_stale_timeout_seconds,
        "bindings": _file_cache_entry(bindings_path),
        # P02.12: the file the build actually reads, which by default is the
        # scoped ``daemon.behavior_models``.
        "behavior_models": _file_cache_entry(behavior_models_path),
        "dbc": _directory_cache_entry(dbc_dir, pattern="*.dbc"),
        # Hash the DID map so editing dids.yaml (new DID, fixed scale)
        # invalidates cached frames that decoded did:* columns.
        # ``dids_path`` must be the file the build actually loads.
        "dids": _file_cache_entry(dids_path),
        # P02.11: the derived channels read these two. The calibration is
        # hashed at the scoped path, so a frame built without it (or with a
        # since-edited one) is never served.
        "afr_calibration": _file_cache_entry(config.daemon.afr_calibration),
        # P02.16: each row's target map is the revision these files say was
        # flashed when it was recorded.
        "tune_revisions": [_file_cache_entry(path) for path in active_registry_sources(config)],
        "vehicle_parameters": config.vehicle.parameters.model_dump(mode="json"),
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
        "sha256": sha256_file(path),
    }


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
