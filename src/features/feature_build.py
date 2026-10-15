"""The offline feature-frame builder and the training-disposition screen.

Corpus-level orchestration over the owners: per-session resampling and
workers, the shared transform, and the cache (``feature_cache`` owns the
key and version).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CanaryConfig, require_profile_scoped
from src.decode.bindings import load_bindings
from src.decode.dbc import DEFAULT_DBC_DIR, load_dbc_dir
from src.decode.dids import load_did_map
from src.features.feature_cache import _feature_frame_cache_metadata, _write_feature_frame_cache
from src.features.feature_paths import DEFAULT_BINDINGS_PATH, DEFAULT_DIDS_PATH
from src.features.feature_transform import featurize_wide_frame, segment_or_constant
from src.features.feature_workers import resample_sessions
from src.features.session_resampling import decode_session_to_wide, load_decode_inputs
from src.preprocessing.drives import stamp_physical_drives
from src.preprocessing.quality_gate import apply_quality_gate
from src.preprocessing.resampler import resample_config_for, resample_wide

log = logging.getLogger(__name__)


def build_feature_frame(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    behavior_models_path: Path | None = None,
    dbc_dir: Path = DEFAULT_DBC_DIR,
    dids_path: Path = DEFAULT_DIDS_PATH,
    cache_dir: Path | None = None,
    force_rebuild: bool = False,
    max_workers: int | None = None,
) -> pd.DataFrame:
    """Run the full preprocessing pipeline over ``session_dirs`` into a feature frame.

    ``read_session`` → ``decode_can_rows`` (bindings.yaml, if present) →
    ``decode_can_rows_dbc`` (any ``.dbc`` files under ``dbc_dir``, if present —
    Task 1.8 interop hook) → ``decode_did_rows`` (``dids_path``, if present) →
    ``to_wide(coerce_numeric=True)`` → ``resample_wide`` → operating-mode
    segment (or constant ``"all"`` mode) → ``append_deviation_columns``
    (behavior models, if present — Task 4.E.7) → ``compute_features``.

    Every catalog path defaults to the repo's own file (:mod:`src.paths`), never
    the working directory, and the cache key hashes exactly the files loaded
    (R01.2): a build started outside the repo root used to drop every ``did:``
    feature while its cache key hashed a different constant.

    The deviation columns land *before* ``compute_features`` so they pick up
    rolling mean/std/roc derivatives just like raw / decoded channels. The
    ``ZScoreScaler`` then normalizes them through ``trainable_feature_columns``
    without changes to either module.

    P02.12 — the behavior models are the scoped ``config.daemon.behavior_models``,
    the file the daemon serves. The default was the repo's unscoped
    ``data/behavior_models.yaml``, which does not exist, and no trainer passed
    a path, so trainer frames carried no ``deviation:*`` columns while the
    served frames did. ``behavior_models_path`` overrides it only for a caller
    whose subject is another artifact (``refit_behavior_models --out``).
    """
    # Before the cache: a hit would otherwise hand back a frame an unscoped
    # config could never have built correctly (P02.11).
    require_profile_scoped(config, reader="build_feature_frame")
    if behavior_models_path is None:
        behavior_models_path = config.daemon.behavior_models
    session_dirs = [Path(session_dir) for session_dir in session_dirs]
    cache_path: Path | None = None
    cache_metadata: dict[str, Any] | None = None
    if cache_dir is not None:
        cache_metadata = _feature_frame_cache_metadata(
            session_dirs,
            config,
            bindings_path=bindings_path,
            behavior_models_path=behavior_models_path,
            dbc_dir=dbc_dir,
            dids_path=dids_path,
        )
        cache_path = Path(cache_dir) / f"{cache_metadata['cache_key']}.pkl"
        if not force_rebuild:
            cached = _load_cached(cache_path)
            if cached is not None:
                return _with_physical_drives(cached, session_dirs)

    inputs = load_decode_inputs(
        bindings_path=bindings_path,
        behavior_models_path=behavior_models_path,
        dbc_dir=dbc_dir,
        dids_path=dids_path,
    )
    resampled = resample_sessions(
        session_dirs, inputs, resample_config_for(config), max_workers=max_workers
    )
    features = featurize_wide_frame(
        resampled,
        config,
        behavior_models=inputs.behavior_models,
        did_map=inputs.did_map,
        bindings=inputs.bindings,
    ).features
    if cache_path is not None and cache_metadata is not None:
        _write_feature_frame_cache(cache_path, features, cache_metadata)
    return _with_physical_drives(features, session_dirs)


def _load_cached(cache_path: Path) -> pd.DataFrame | None:
    """The cached feature frame at ``cache_path``, or ``None`` (absent or
    unreadable: a corrupt cache rebuilds, it never fails a retrain)."""
    if not cache_path.exists():
        return None
    try:
        cached = pd.read_pickle(cache_path)
        if not isinstance(cached, pd.DataFrame):
            raise TypeError(f"cached object is {type(cached).__name__}, not DataFrame")
    except Exception as exc:  # noqa: BLE001 - cache corruption should not fail retrain
        log.warning(
            "feature frame cache unreadable; rebuilding",
            extra={"cache": str(cache_path), "error": str(exc)},
        )
        return None
    log.info(
        "loaded feature frame cache",
        extra={"cache": str(cache_path), "rows": len(cached), "columns": len(cached.columns)},
    )
    return cached


def _with_physical_drives(features: pd.DataFrame, session_dirs: Sequence[Path]) -> pd.DataFrame:
    """Label every row with its physical drive (``src.preprocessing.drives``).

    Stamped after the cache, never into it: which drive a row belongs to
    depends on the recordings beside it (captures bridge drives, quarantine
    moves recordings away), not only on the sessions this frame was built from.
    Every trainer's drive floor, validation split and count reads this label.
    """
    if features.empty or "session" not in features.columns:
        return features
    return stamp_physical_drives(features, session_dirs)


def disposition_screen_frames(
    sessions: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
) -> tuple[list[pd.DataFrame], dict[str, int]]:
    """Decode, quality-gate, resample and regime-gate every session for the
    training-disposition screen (Task 1.4).

    Returns the per-session regime-gated frames (ready for
    ``training_disposition.classify_dispositions``) and the corpus-wide
    per-channel reject counts accumulated across every session's quality gate.
    Reuses :func:`decode_session_to_wide`, the exact decode steps the trainers
    run, so the screen sees precisely what training would see; the resample
    uses the training recipe, gap rules included (D03.27). One unreadable
    session is logged and skipped. Used by ``scripts/disposition_report.py``,
    ``scripts/ledger.py`` (golden baselines) and ``scripts/retrain.py``'s
    disposition summary: this was ``disposition_report._scan_sessions``,
    imported by the other two across scripts (R06.4).
    """
    bindings = load_bindings(bindings_path) if bindings_path.exists() else None
    dbc_database = load_dbc_dir(DEFAULT_DBC_DIR)
    did_map = load_did_map(DEFAULT_DIDS_PATH) if DEFAULT_DIDS_PATH.exists() else None
    resample_config = resample_config_for(config)

    framed_parts: list[pd.DataFrame] = []
    total_rejects: dict[str, int] = {}
    for session_dir in sessions:
        try:
            wide = decode_session_to_wide(Path(session_dir), bindings, dbc_database, did_map)
        except Exception as exc:  # noqa: BLE001 — one bad session must not kill the scan
            log.warning(
                "skipping unreadable session",
                extra={"session": str(session_dir), "error": str(exc)},
            )
            continue
        gated, quality = apply_quality_gate(wide)
        for channel, count in quality.reject_counts.items():
            total_rejects[channel] = total_rejects.get(channel, 0) + count
        resampled = resample_wide(gated, config=resample_config)
        framed, _fell_back = segment_or_constant(resampled, config)
        framed_parts.append(framed)

    return framed_parts, total_rejects
