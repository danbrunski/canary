"""Loading the sessions an evaluation scores (P01.2).

Which recordings are discovered (:data:`EVALUATION_SESSION_KINDS`) and how
their resampled frames are read back from, refreshed in, or built into the
derived-frame store, one process per session when asked. A session that fails
to load is a named skip, not a crash. Extracted from
``scripts/evaluate_detectors.py`` (R15.4); that script re-exports these names.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from src.config import CanaryConfig
from src.features.session_resampling import default_decode_inputs, load_resampled_session
from src.preprocessing.derived_store import DerivedStore
from src.preprocessing.resampler import resample_config_for
from src.session import RECORDED_SESSION_KINDS, SessionKind

log = logging.getLogger(__name__)


#: Every recording under ``--sessions-root``, captures and legacy ``_tuning-*``
#: included: they are kept for *drive grouping* only, a sibling recording
#: bridging its neighbours into one physical drive. Whether their rows are
#: scored is :func:`~src.storage.corpus_eligibility.default_profile_exclusion`'s call
#: (R04.6): tuning captures are not. ``_archive`` (quarantine) and ``_replay-*``
#: (re-emitted copies) are not drives.
EVALUATION_SESSION_KINDS = RECORDED_SESSION_KINDS | {SessionKind.TUNING}


def _load_one(
    args: tuple[Path, str | None, CanaryConfig],
) -> tuple[str, pd.DataFrame | None, str | None]:
    # The config parsed once by the parent, not re-read here per session:
    # re-reading it made a mid-run edit of the YAML reach the sessions loaded
    # after it (2026-09-26: a key added under ``collection`` while a run was
    # loading failed 49 sessions on the loaded schema, and dropped 9 drives).
    session, derived_dir, config = args
    try:
        store = DerivedStore(Path(derived_dir)) if derived_dir is not None else None
        return session.name, load_resampled_session(session, config, store=store), None
    except Exception as exc:  # noqa: BLE001 - one bad session is a skip, not a crash
        return session.name, None, f"{type(exc).__name__}: {exc}"


def load_resampled_sessions(
    sessions: Sequence[Path],
    config: CanaryConfig,
    *,
    derived_dir: Path | None,
    workers: int,
    cache_only: bool = False,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Resampled frames by session name, through the derived-frame store (P01.2).

    Each session's reduced frame is read back from ``derived_dir``, refreshed
    for the decode units whose catalog entry changed, or built and kept
    (:class:`~src.preprocessing.derived_store.DerivedStore`); a stale frame is
    detected by hash and never served. ``derived_dir=None`` reduces in memory
    and keeps nothing. ``cache_only`` skips (and names) every session whose
    derived frame is missing or stale instead of deriving it.
    """
    jobs = [
        (session, str(derived_dir) if derived_dir is not None else None, config)
        for session in sessions
    ]
    frames: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    if cache_only:
        store = DerivedStore(derived_dir) if derived_dir is not None else None
        decoders = default_decode_inputs()
        resample = resample_config_for(config)
        ready = []
        for job in jobs:
            reason = (
                "no --derived-dir" if store is None else store.status(job[0], decoders, resample)
            )
            if reason is None:
                ready.append(job)
            else:
                errors[job[0].name] = f"not derived yet (--cache-only): {reason}"
        jobs = ready
    results: list[tuple[str, pd.DataFrame | None, str | None]] = []
    if workers > 1 and len(jobs) > 1:
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(_load_one, jobs))
        except BrokenExecutor as exc:
            # A worker killed for memory breaks the pool. Finished sessions
            # are already derived, so the serial pass reads them back.
            log.warning("parallel session decode aborted (%s); finishing serially", exc)
            results = []
    if not results:
        results = [_load_one(job) for job in jobs]
    for name, frame, error in results:
        if frame is None:
            errors[name] = error or "unreadable"
        elif frame.empty:
            errors[name] = "no rows after decode"
        else:
            frames[name] = frame
    return frames, errors
