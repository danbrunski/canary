"""Scoring drives in spawned worker processes (P02.5).

The evaluation scores independent drives, so with ``--workers > 1`` they run
in *spawned* (not forked) processes, largest first, and the parent merges the
results back in drive order: the report does not depend on the worker count.
Spawned, because the parent has already run torch (the parity check and the
untrained autoencoder's calibration) and a forked child of a process with a
live OpenMP pool can hang in its first parallel region.

What crosses the boundary is small and picklable: a :class:`ScorerSpec` (paths
and fitted baselines, never loaded artifacts) to build each worker's
:class:`~src.models.evaluation_scorer.DriveScorer`, and a
:class:`~src.models.evaluation_scorer.DriveTask` per drive. The worker entry
points (:func:`_init_scoring_worker`, :func:`_score_drive_task`) are module
level, so a spawned interpreter resolves them by name.

A worker re-hashes every key input before it loads anything and refuses if one
moved (:func:`~src.models.evaluation_identity.changed_key_inputs`); the parent
then scores in-process with what it loaded and commits nothing. Extracted from
``scripts/evaluate_detectors.py`` (R15.3); that script re-exports these names.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
from collections.abc import Callable, Sequence
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from src.behavior.behavior_models import load_behavior_models
from src.cgroup_memory import CGROUP_ROOT, memory_limit_status
from src.config import CanaryConfig
from src.decode.bindings import load_bindings
from src.decode.dids import load_did_map
from src.features.feature_paths import DEFAULT_DIDS_PATH
from src.features.feature_workers import available_memory_bytes as _available_memory_bytes
from src.features.session_resampling import default_decode_inputs
from src.models.benchmark_gate import load_sequence_artifact
from src.models.evaluation import PersistenceSpec
from src.models.evaluation_cache import ScoreCache
from src.models.evaluation_detectors import (
    DeployedStack,
    Detector,
    PlausibilityDetector,
    RawStreamDetector,
    ScoreModel,
    ThresholdedScore,
    UntrainedAutoencoder,
    suppressed_for,
)
from src.models.evaluation_identity import KeySources, changed_key_inputs
from src.models.evaluation_scorer import DriveResult, DriveScorer, DriveTask, SweepSpec
from src.models.isolation_forest import load_isolation_forest_model
from src.preprocessing.derived_store import on_the_pi

log = logging.getLogger(__name__)


#: Peak RSS of one scoring worker on A02.11's drives, measured 2026-09-27
#: (P02.5): 3,272 / 3,291 / 3,251 MB at 2 / 3 / 4 workers, set by the worker
#: that scores the drive holding ``2026-09-20T152758`` (8,570 rows, whose
#: injection sweep holds the longest arrived-sample stream). The pool is sized
#: so this many bytes per worker fit in :data:`SCORING_MEMORY_FRACTION` of
#: ``MemAvailable``, as if every worker peaked at once; the peaks do not
#: coincide (4 workers peaked at 9.40 GB for the whole tree, parent included).
SCORING_WORKER_PEAK_BYTES = 3_300_000_000


SCORING_MEMORY_FRACTION = 0.8


@dataclass
class ScorerSpec:
    """What a spawned scoring worker rebuilds its :class:`DriveScorer` from.

    Spawned, not forked: the parent has already run torch (the parity check
    and the untrained autoencoder's calibration), and a forked child of a
    process with a live OpenMP pool can hang in its first parallel region.
    Artifacts are reloaded from their paths; fitted baselines travel as
    fitted (the untrained autoencoder as ``None``, rebuilt from its seed).
    """

    #: Profile-scoped (P02.11): featurization refuses anything else.
    config: CanaryConfig
    if_path: Path | None
    sequence_path: Path | None
    baselines: list[tuple[ScoreModel | None, float]]
    untrained_seed: int
    plausibility: bool
    bindings_path: Path | None
    behavior_path: Path | None
    identities: dict[str, str]
    stream_paths: dict[tuple[str, str], Path]
    cache_root: Path | None
    cache_run: str
    key_sources: KeySources
    key_inputs: dict[str, str | None]
    code: str
    feature_key: str
    persistence: PersistenceSpec
    roles: dict[str, list[str]]
    sweep: SweepSpec
    threads: int


def build_worker_scorer(spec: ScorerSpec) -> DriveScorer:
    """The parent's detectors, rebuilt the way ``main`` builds them."""
    config = spec.config
    trained_if = load_isolation_forest_model(spec.if_path) if spec.if_path is not None else None
    trained_seq = (
        load_sequence_artifact(spec.sequence_path) if spec.sequence_path is not None else None
    )
    stack = DeployedStack(
        config,
        trained_if,
        trained_seq,
        suppress_channels=suppressed_for(trained_seq, config) if trained_seq else (),
    )
    detectors: list[Detector] = [stack]
    for model, threshold in spec.baselines:
        if model is None:
            assert trained_seq is not None
            model = UntrainedAutoencoder.like(
                trained_seq,
                seed=spec.untrained_seed,
                suppress_channels=suppressed_for(trained_seq, config),
            )
        detectors.append(ThresholdedScore(model, threshold))
    bindings = (
        load_bindings(spec.bindings_path)
        if spec.bindings_path is not None and spec.bindings_path.exists()
        else None
    )
    # The catalog load_resampled_session decoded the frames with.
    did_map = default_decode_inputs().did_map
    raw: list[RawStreamDetector] = []
    if spec.plausibility:
        raw.append(
            PlausibilityDetector(
                spec.config,
                bindings=bindings,
                did_map=load_did_map(DEFAULT_DIDS_PATH) if DEFAULT_DIDS_PATH.exists() else None,
            )
        )
    behavior = (
        load_behavior_models(spec.behavior_path)
        if spec.behavior_path is not None and spec.behavior_path.exists()
        else None
    )
    return DriveScorer(
        detectors=detectors,
        raw_detectors=raw,
        identities=spec.identities,
        stream_paths=spec.stream_paths,
        cache=ScoreCache(spec.cache_root, run=spec.cache_run),
        code=spec.code,
        feature_key=spec.feature_key,
        config=config,
        bindings=bindings,
        did_map=did_map,
        behavior=behavior,
        persistence=spec.persistence,
        roles=spec.roles,
        sweep=spec.sweep,
    )


_WORKER_SCORER: DriveScorer | None = None


#: Torch threads per scoring process (``--scoring-threads``). Fixed, never
#: derived from ``--workers``: the LSTM's float32 sums differ in the last
#: bits between thread counts (measured: 6 vs 4 threads moved scores by up to
#: 7.8e-07), so a count that followed the pool size would make the report
#: depend on it. One thread per process makes it depend on neither the
#: worker count nor the machine's core count; the pool supplies parallelism.
DEFAULT_SCORING_THREADS = 1


def pin_threads(threads: int) -> Callable[[], None]:
    """Hold this process's torch intra-op pool to ``threads`` (``0`` leaves
    torch's own default); returns what puts the previous count back. NumPy's
    BLAS pool is left alone on purpose: its results also move with the thread
    count, and every process then uses the same machine default, as the
    harness always did."""
    if threads <= 0:
        return lambda: None
    try:
        import torch
    except ImportError:  # torch-free installs score without the sequence model
        return lambda: None
    previous = torch.get_num_threads()
    torch.set_num_threads(threads)
    return lambda: torch.set_num_threads(previous)


def _init_scoring_worker(spec: ScorerSpec) -> None:
    global _WORKER_SCORER
    pin_threads(spec.threads)
    # This worker reloads the artifacts, catalogs and code from disk: if any
    # of them is not what the parent keyed this run on, its scores would be
    # staged under a key they do not match. Refuse; the parent then scores
    # in-process with what it loaded, and commits nothing (P02.5).
    changed = changed_key_inputs(spec.key_sources, spec.key_inputs)
    if changed:
        raise RuntimeError(
            "scoring worker refused: key inputs changed since the run started: "
            + ", ".join(changed)
        )
    _WORKER_SCORER = build_worker_scorer(spec)


def _score_drive_task(task: DriveTask) -> DriveResult:
    assert _WORKER_SCORER is not None, "scoring worker was not initialized"
    return _WORKER_SCORER.score_drive(task)


def _cgroup_memory_headroom(
    *, cgroup_root: Path | None = None, self_cgroup: Path | None = None
) -> int | None:
    """The least ``memory.max - memory.current`` over this process's cgroup
    and every parent group below the root (a parent's ceiling binds its
    children too), or ``None`` when no level sets a byte ceiling (``max``) or
    the files cannot be read. The group is found by the daemon's own parser
    (``memory_limit_status``, in :mod:`src.cgroup_memory` since R15)."""
    root = cgroup_root or CGROUP_ROOT
    status = memory_limit_status(
        cgroup_root=root, self_cgroup=self_cgroup or Path("/proc/self/cgroup")
    )
    if status["memory_max_source"] is None:
        return None
    group = Path(status["memory_max_source"]).parent
    headroom: int | None = None
    while group != root and group.is_relative_to(root):
        try:
            ceiling = (group / "memory.max").read_text().strip()
            if ceiling != "max":
                current = int((group / "memory.current").read_text())
                room = max(0, int(ceiling) - current)
                headroom = room if headroom is None else min(headroom, room)
        except (OSError, ValueError):
            pass
        group = group.parent
    return headroom


def scoring_workers(requested: int, tasks: int, *, threads: int = 1) -> int:
    """Workers for ``tasks`` drives: at most ``requested``, one per drive, no
    more than the cores hold at ``threads`` torch threads each, and as many
    as :data:`SCORING_WORKER_PEAK_BYTES` fit in the memory there is: the
    lesser of ``MemAvailable`` and the cgroup's headroom (the Pi's
    ``canary.service`` runs under ``MemoryMax``). On the Pi it is always one,
    in-process: a pool of 3.3 GB workers is no thing to fork beside the
    collector (AGENTS.md: no analysis on the Pi while it collects)."""
    if requested <= 1 or tasks <= 1 or on_the_pi():
        return 1
    requested = min(requested, max(1, (os.cpu_count() or 1) // max(1, threads)))
    limits = [m for m in (_available_memory_bytes(), _cgroup_memory_headroom()) if m is not None]
    available = min(limits) if limits else None
    by_memory = (
        requested
        if available is None
        else int(available * SCORING_MEMORY_FRACTION // SCORING_WORKER_PEAK_BYTES)
    )
    return max(1, min(requested, tasks, by_memory))


def score_drives(
    tasks: Sequence[DriveTask],
    *,
    workers: int,
    threads: int,
    local: Callable[[], DriveScorer],
    spec: Callable[[], ScorerSpec],
) -> list[DriveResult]:
    """Every drive's :class:`DriveResult`, in ``tasks`` order.

    Drives are independent, so with ``workers > 1`` they are scored in
    spawned processes, largest first; results are merged back in drive order,
    so the report does not depend on the worker count. A worker killed for
    memory breaks the pool: the drives are then scored in this process, and
    whatever the workers finished is read back from the score cache.
    """
    count = scoring_workers(workers, len(tasks), threads=threads)
    if count > 1:
        log.info("scoring %d drives on %d workers (%d threads each)", len(tasks), count, threads)
        order = sorted(
            range(len(tasks)),
            key=lambda i: -sum(len(frame) for frame in tasks[i].frames.values()),
        )
        results: dict[int, DriveResult] = {}
        try:
            with ProcessPoolExecutor(
                max_workers=count,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_init_scoring_worker,
                initargs=(spec(),),
            ) as pool:
                futures = {i: pool.submit(_score_drive_task, tasks[i]) for i in order}
                results = {i: future.result() for i, future in futures.items()}
        except BrokenExecutor as exc:
            log.warning("parallel scoring aborted (%s); finishing in this process", exc)
            results = {}
        if len(results) == len(tasks):
            return [results[i] for i in range(len(tasks))]
    scorer = local()
    return [scorer.score_drive(task) for task in tasks]
