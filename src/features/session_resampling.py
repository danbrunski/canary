"""Offline loading of one session: decode, quality gate and resample.

The per-session chain every offline consumer shares (trainers, the
evaluation loader, the session-deviation analysis), the decode catalogs it
needs and the all-stale-row filter. Reader ownership is
``src.preprocessing`` (R13); this module only chooses what to ask of it.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.behavior.behavior_models import BehaviorModels, load_behavior_models
from src.columns import GAP_SUFFIX
from src.config import CanaryConfig
from src.decode.bindings import Bindings, load_bindings
from src.decode.dbc import DEFAULT_DBC_DIR, load_dbc_dir
from src.decode.dids import load_did_map
from src.features.feature_paths import DEFAULT_BINDINGS_PATH, DEFAULT_DIDS_PATH
from src.preprocessing.derived import resample_reduced
from src.preprocessing.derived_store import DecodeInputs, DerivedStore, active_store
from src.preprocessing.quality_gate import apply_quality_gate
from src.preprocessing.resampler import ResampleConfig, resample_config_for, resample_wide
from src.preprocessing.session_decode import decode_long_frame, reduce_session
from src.preprocessing.session_reader import SESSION_PART_ROWS, read_session
from src.preprocessing.wide_frame import to_wide
from src.storage.broadcast_storage import change_only_timeout, refuse_shorter_stored_timeout

log = logging.getLogger(__name__)


def decode_session_to_wide(
    session_dir: Path,
    bindings: Any,
    dbc_database: Any,
    did_map: Any,
) -> pd.DataFrame:
    """parquet → decode → wide pivot, split out of
    :func:`decode_session_to_resampled` (Task 1.2) so the quality gate's
    reject counts stay inspectable per-session (``scripts/disposition_report.py``)
    without duplicating the decode steps.
    """
    long = decode_long_frame(
        read_session(session_dir, decoded="as_recorded"), bindings, dbc_database, did_map
    )
    return to_wide(long, coerce_numeric=True)


def decode_session_to_resampled(
    session_dir: Path,
    bindings: Any,
    dbc_database: Any,
    did_map: Any,
    resample_config: ResampleConfig,
) -> pd.DataFrame:
    """The per-session pipeline: parquet → decode → wide → reject gate → resample.

    Top-level (not nested) so :class:`ProcessPoolExecutor` can pickle it. The
    serial path also calls it directly, so there's exactly one implementation
    of the per-session work.

    The quality gate (Task 1.2) runs on the wide frame, before
    ``resample_wide`` — a physically-impossible cell must be NaN'd before the
    resampler's ffill/``__gap`` bookkeeping ever sees it, so a reject is
    indistinguishable from any other gap to every downstream consumer.

    Runs :func:`resample_session_bounded`, so training, the evaluation loader
    and the session-deviation analysis share one bounded chain.
    """
    return resample_session_bounded(
        session_dir,
        bindings,
        dbc_database,
        did_map,
        resample_config,
    )


def resample_session_bounded(
    session_dir: Path,
    bindings: Any,
    dbc_database: Any,
    did_map: Any,
    resample_config: ResampleConfig,
    *,
    part_rows: int = SESSION_PART_ROWS,
    store: DerivedStore | None = None,
    channels: Collection[str] | None = None,
) -> pd.DataFrame:
    """decode → wide → reject gate → resample for one session, in bounded memory.

    The one reader of a session's 1 Hz frame for every offline consumer
    (P01.2): the trainers (via :func:`decode_session_to_resampled`),
    ``evaluate_detectors`` (via :func:`load_resampled_session`) and the
    session-deviation analysis. Identical output to
    ``resample_long_session(read_session(session_dir, decoded="as_recorded"), ...)``
    (``tests/test_bounded_session_resample.py`` asserts it exactly), but it
    never pivots at raw resolution: :func:`reduce_session` keeps only the cells
    the resampler reads, and :func:`src.preprocessing.derived.resample_reduced`
    builds the wide frame from those. Reading a 2 h, 17.9M-row drive whole cost
    ~12 GB before a single value was decoded; the A02.11 baseline was
    OOM-killed at 15 GB on it (2026-09-25).

    ``store`` (default: :func:`~src.preprocessing.derived_store.active_store`,
    which is ``None`` unless the process asked for one) reads the reduced
    frame back, or builds and keeps it; without one nothing is written.
    ``channels`` projects the frame to those value columns.
    """
    if store is None:
        store = active_store()
    if store is not None:
        reduced, _load = store.load(
            session_dir,
            DecodeInputs(bindings=bindings, dbc=dbc_database, did_map=did_map),
            resample_config,
            part_rows=part_rows,
        )
    else:
        reduced = reduce_session(
            session_dir, bindings, dbc_database, did_map, resample_config, part_rows=part_rows
        )
    return resample_reduced(reduced, resample_config, channels=channels)


def resample_long_session(
    long: pd.DataFrame,
    bindings: Any,
    dbc_database: Any,
    did_map: Any,
    resample_config: ResampleConfig,
    *,
    session_name: str,
) -> pd.DataFrame:
    """decode → wide → reject gate → resample for one session's long frame.

    Takes the long frame instead of reading it, so a filtered reader (the
    behavior-validation evaluator reads only the CAN ids its decoded channels
    need) runs the identical decode/gate/resample chain rather than a copy of
    it. It holds the whole decoded session at once; a caller that reads a
    whole session directory uses :func:`resample_session_bounded`, whose
    output is identical and whose memory is not.
    """
    # P02.10: the decode drops ``attrs``, so the recorded timeout is checked here.
    refuse_shorter_stored_timeout(
        change_only_timeout(long), resample_config.broadcast_max_gap_seconds, what=session_name
    )
    wide = to_wide(decode_long_frame(long, bindings, dbc_database, did_map), coerce_numeric=True)
    gated, quality = apply_quality_gate(wide)
    if quality.total_rejected:
        log.info(
            "quality gate rejected cells in session",
            extra={"session": session_name, "reject_counts": quality.reject_counts},
        )
    return resample_wide(gated, config=resample_config)


@dataclass(frozen=True)
class BuildDecodeInputs:
    """The catalogs a feature build decodes with; ``None`` where the file is absent."""

    bindings: Bindings | None
    behavior_models: BehaviorModels | None
    dbc_database: Any
    did_map: Any


def load_decode_inputs(
    *, bindings_path: Path, behavior_models_path: Path, dbc_dir: Path, dids_path: Path
) -> BuildDecodeInputs:
    return BuildDecodeInputs(
        bindings=load_bindings(bindings_path) if bindings_path.exists() else None,
        behavior_models=(
            load_behavior_models(behavior_models_path) if behavior_models_path.exists() else None
        ),
        dbc_database=load_dbc_dir(dbc_dir),
        did_map=load_did_map(dids_path) if dids_path.exists() else None,
    )


def drop_fully_stale_rows(resampled: pd.DataFrame) -> pd.DataFrame:
    """Drop resampled rows where *every* gap-flagged channel is stale.

    The resampler emits a row per second across a session's whole span and
    forward-fills values across data gaps, flagging each fast channel's
    staleness in a ``<channel>__gap`` column. A row with *all* gap flags set had
    no fresh observation that second — it's a synthesized fill (e.g. a session
    left open across a long power-off gap), not telemetry, and training on it
    poisons the affected mode (a 12 h gap forward-fills into tens of thousands
    of identical "warmup" rows). Rows with only *some* channels stale (a slow
    PID between updates) are real observations and kept. ``LiveFeatureSource.tick``
    (src/features/live.py) applies the same all-gap-flags-set rule per tick
    and returns ``None`` instead of emitting a row — dropping here moves training
    rows toward "seconds actually observed", which is what serve scores.
    """
    gap_cols = [c for c in resampled.columns if str(c).endswith(GAP_SUFFIX)]
    if not gap_cols or resampled.empty:
        return resampled
    fully_stale = resampled[gap_cols].fillna(True).all(axis=1)
    dropped = int(fully_stale.sum())
    if dropped:
        log.info(
            "dropped fully-stale (gap-filled) rows from training frame",
            extra={"dropped": dropped, "total": len(resampled)},
        )
    return resampled.loc[~fully_stale].reset_index(drop=True)


def load_resampled_session(
    session_dir: Path,
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    store: DerivedStore | None = None,
) -> pd.DataFrame:
    """Read + decode + wide-pivot + quality-gate + resample one session (no
    segmentation: ``featurize_wide_frame`` segments internally).

    The same per-session chain the trainers run (:func:`resample_session_bounded`):
    bindings, ``.dbc`` files and the DID map all decode, and the reject gate
    NaNs impossible cells before the resampler sees them. The benchmark copy
    this replaced decoded bindings only, with no DID decode and no quality
    gate, so it scored the sequence detector on frames with every ``did:``
    channel absent: rows the artifact never saw that way in training or
    serving (A02). A missing bindings file or DID map is skipped, not an
    error, as in training. Used by ``scripts/evaluate_detectors.py`` (and so
    by retrain's benchmark and alert-budget gates), which passes its derived
    ``store``.
    """
    decoders = default_decode_inputs(bindings_path)
    resampled = resample_session_bounded(
        session_dir,
        decoders.bindings,
        decoders.dbc,
        decoders.did_map,
        resample_config_for(config),
        store=store,
    )
    return drop_fully_stale_rows(resampled)


def default_decode_inputs(bindings_path: Path = DEFAULT_BINDINGS_PATH) -> DecodeInputs:
    """The catalogs :func:`load_resampled_session` decodes with: bindings,
    ``.dbc`` files and the DID map, each skipped when absent (as in training).

    Loaded once per process for the same file bytes (P02.5): parsing the DBC
    alone cost ~0.3 s, and the evaluation harness asked once per session.
    Keyed on the files' contents, so an edited catalog is reloaded.
    """
    stamp = _catalog_stamp(bindings_path, *sorted(DEFAULT_DBC_DIR.glob("*.dbc")), DEFAULT_DIDS_PATH)
    cached = _DECODE_INPUTS_MEMO.get(stamp)
    if cached is None:
        cached = DecodeInputs(
            bindings=load_bindings(bindings_path) if bindings_path.exists() else None,
            dbc=load_dbc_dir(DEFAULT_DBC_DIR),
            did_map=load_did_map(DEFAULT_DIDS_PATH) if DEFAULT_DIDS_PATH.exists() else None,
        )
        _DECODE_INPUTS_MEMO.clear()
        _DECODE_INPUTS_MEMO[stamp] = cached
    return cached


#: The last catalogs :func:`default_decode_inputs` loaded, by file contents.
_DECODE_INPUTS_MEMO: dict[tuple[tuple[str, str | None], ...], DecodeInputs] = {}


def _catalog_stamp(*paths: Path) -> tuple[tuple[str, str | None], ...]:
    """Each path with its content hash (``None`` when absent)."""
    stamp: list[tuple[str, str | None]] = []
    for path in paths:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        stamp.append((str(path.resolve()), digest))
    return tuple(stamp)


#: Pre-B02 private name, kept for existing importers.
_drop_fully_stale_rows = drop_fully_stale_rows
