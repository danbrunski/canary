"""Parallel per-session feature builds: worker sizing, spawn entry points and
the serial fallback.

``_worker_process_session`` and ``_init_feature_worker`` are top-level here so
a ``spawn`` worker can import them by qualified name.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
from collections.abc import Sequence
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd

from src.features.session_resampling import (
    BuildDecodeInputs,
    decode_session_to_resampled,
    drop_fully_stale_rows,
)
from src.preprocessing.resampler import ResampleConfig

log = logging.getLogger(__name__)


#: Worker-process state for the parallel feature build. Populated once per
#: process via :func:`_init_feature_worker` so the heavy decoder objects
#: (bindings, DBC database, DID map) are sent across the pool boundary one time
#: per worker — not pickled per submitted session.
_FEATURE_WORKER_STATE: dict[str, Any] = {}


def _init_feature_worker(
    bindings: Any,
    dbc_database: Any,
    did_map: Any,
    resample_config: ResampleConfig,
) -> None:
    _FEATURE_WORKER_STATE["bindings"] = bindings
    _FEATURE_WORKER_STATE["dbc"] = dbc_database
    _FEATURE_WORKER_STATE["dids"] = did_map
    _FEATURE_WORKER_STATE["resample"] = resample_config


def _worker_process_session(
    session_dir: Path,
) -> tuple[str, pd.DataFrame | None, str | None]:
    """ProcessPool task body. Returns ``(session_name, frame_or_none, error_or_none)``
    so a single bad session is reported as a skip rather than killing the pool."""
    state = _FEATURE_WORKER_STATE
    try:
        frame = decode_session_to_resampled(
            session_dir,
            state["bindings"],
            state["dbc"],
            state["dids"],
            state["resample"],
        )
        return session_dir.name, frame, None
    except Exception as exc:  # noqa: BLE001 — one bad session must not kill the corpus
        return session_dir.name, None, f"{type(exc).__name__}: {exc}"


#: Skip the process pool below this session count: per-session work is short
#: enough that a fork + IPC round-trip wipes out the parallel win. Above this,
#: the pool consistently beats serial wall time on our corpus.
_PARALLEL_SESSION_THRESHOLD = 3

#: Hard ceiling on auto-resolved workers, before the memory budget below trims
#: it further. Callers can pass an explicit ``max_workers`` to override both.
_PARALLEL_WORKER_CAP = 4

#: Share of ``MemAvailable`` the pool may claim. Deliberately well under half:
#: the parent process is concurrently accumulating every returned session frame
#: in ``resampled_parts`` (a full-corpus retrain parent has been observed at
#: 8 GB RSS), and it, not the workers, holds the final concatenated frame.
_PARALLEL_MEMORY_FRACTION = 0.35

#: Resident floor for one ``spawn`` worker before it touches data: a fresh
#: interpreter re-importing pandas/pyarrow/torch-free decode stack measures
#: ~130-200 MB on the dev laptop.
_WORKER_BASELINE_BYTES = 250 * 1024 * 1024

#: Peak working set per byte of session parquet, for the decode → long →
#: wide-pivot → resample chain. Measured on this corpus (2026-07-26,
#: ``resample_hz: 1``): 28.5 MB on disk → 487 MB peak (17×), 45.6 MB → 1360 MB
#: (30×). Rounded up to 40× because the expansion scales with a session's
#: *channel count and wall-clock span*, not just its byte count, and an
#: underestimate is what gets a worker OOM-killed.
_WORKER_BYTES_PER_DISK_BYTE = 40


def available_memory_bytes() -> int | None:
    """Best-effort ``MemAvailable``, or ``None`` if the platform won't say.

    ``MemAvailable`` (not ``MemFree``) is the right number: it accounts for
    reclaimable page cache, which is most of what a just-finished parquet read
    leaves behind.
    """
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:  # non-Linux fallback: free pages only, no reclaimable cache
        return int(os.sysconf("SC_AVPHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, AttributeError):
        return None


def _largest_session_bytes(session_dirs: Sequence[Path]) -> int:
    """On-disk parquet bytes of the biggest session in the batch.

    Sizing off the max rather than the mean because ``pool.map`` hands sessions
    out in order: several of the largest drives can be in flight simultaneously,
    and it only takes one worker over the line to get the pool OOM-killed.
    """
    largest = 0
    for session_dir in session_dirs:
        try:
            total = sum(path.stat().st_size for path in session_dir.glob("*.parquet"))
        except OSError:
            continue
        largest = max(largest, total)
    return largest


def _resolve_worker_count(session_dirs: Sequence[Path], max_workers: int | None) -> int:
    """Pick a worker count that fits in RAM.

    A fixed cap is wrong on both ends: it OOM-kills a wide corpus on a loaded
    box (4 workers × multi-GB frames on a 15 GB laptop) and leaves cores idle on
    a small one. Derive it from what's actually free and what one worker is
    actually going to hold, and log the arithmetic so a surprising choice is
    inspectable rather than mysterious.
    """
    if max_workers is None:
        configured = os.environ.get("CANARY_FEATURE_WORKERS")
        if configured:
            try:
                max_workers = int(configured)
            except ValueError:
                log.warning("ignoring invalid CANARY_FEATURE_WORKERS=%r", configured)
    if max_workers is not None:
        return max(1, int(max_workers))

    ceiling = min(len(session_dirs), os.cpu_count() or 1, _PARALLEL_WORKER_CAP)
    if ceiling <= 1:
        return max(1, ceiling)

    available = available_memory_bytes()
    if available is None:
        return ceiling

    per_worker = _WORKER_BASELINE_BYTES + _largest_session_bytes(session_dirs) * (
        _WORKER_BYTES_PER_DISK_BYTE
    )
    budget = int(available * _PARALLEL_MEMORY_FRACTION)
    affordable = max(1, budget // per_worker)
    workers = min(ceiling, affordable)
    log.info(
        "sized parallel feature build to available memory",
        extra={
            "workers": workers,
            "cpu_ceiling": ceiling,
            "available_mb": round(available / 1024 / 1024),
            "budget_mb": round(budget / 1024 / 1024),
            "per_worker_mb": round(per_worker / 1024 / 1024),
        },
    )
    return workers


def resample_sessions(
    session_dirs: Sequence[Path],
    inputs: BuildDecodeInputs,
    resample_config: ResampleConfig,
    *,
    max_workers: int | None,
) -> pd.DataFrame:
    """Decode → pivot → resample one session at a time, then concatenate.

    ``resample_wide`` and ``compute_features`` already group by session (no
    cross-session bleed), so per-session-then-concat is identical to
    concat-then-resample — but peak memory is one session's raw bus-rate pivot
    instead of all of them concatenated, which OOM-kills the Pi on a full
    corpus.

    Sessions are independent, so for >= ``_PARALLEL_SESSION_THRESHOLD`` inputs
    the per-session pipeline fans out across a ``ProcessPoolExecutor`` (one
    process per session, capped by CPU count and by the memory budget in
    ``_resolve_worker_count``). ``max_workers=1`` forces the serial path — kept
    exactly as it was for back-compat with tests and the Pi cold-start
    fall-back, where multiprocessing overhead would lose. An unreadable session
    is skipped and named; no readable session at all raises.
    """
    resampled_parts: list[pd.DataFrame] = []
    skipped: list[str] = []

    effective_workers = _resolve_worker_count(session_dirs, max_workers)
    parallel = effective_workers > 1 and len(session_dirs) >= _PARALLEL_SESSION_THRESHOLD

    def _build_serial() -> None:
        for session_dir in session_dirs:
            try:
                resampled_parts.append(
                    decode_session_to_resampled(
                        session_dir,
                        inputs.bindings,
                        inputs.dbc_database,
                        inputs.did_map,
                        resample_config,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — one bad session must not kill the corpus
                skipped.append(session_dir.name)
                log.warning(
                    "skipping unreadable session",
                    extra={"session": str(session_dir), "error": f"{type(exc).__name__}: {exc}"},
                )

    if parallel:
        log.info(
            "building feature frame in parallel",
            extra={"sessions": len(session_dirs), "workers": effective_workers},
        )
        try:
            with ProcessPoolExecutor(
                max_workers=effective_workers,
                initializer=_init_feature_worker,
                initargs=(inputs.bindings, inputs.dbc_database, inputs.did_map, resample_config),
                # ``spawn`` re-imports the codebase per worker rather than
                # forking the parent's address space, which avoids the classic
                # fork-with-threads deadlock (numpy / torch keep background
                # threads alive). The extra ~0.5-1 s of worker startup is
                # rounded off by even a single-session feature build.
                mp_context=multiprocessing.get_context("spawn"),
            ) as pool:
                # ``map`` preserves order, so the concatenated frame is
                # identical to the serial run — no ordering drift between runs.
                for name, frame, err in pool.map(_worker_process_session, session_dirs):
                    if frame is not None:
                        resampled_parts.append(frame)
                    else:
                        skipped.append(name)
                        log.warning(
                            "skipping unreadable session",
                            extra={"session": name, "error": err},
                        )
        except BrokenExecutor as exc:
            # OOM-kill, segfault, or a worker dying mid-task all surface as
            # BrokenExecutor. Don't fail the retrain — fall back to the
            # serial path that's been in production all along. Partial
            # results from the pool are dropped (we can't trust the ordering
            # or completeness) and we restart fresh, serially.
            log.warning(
                "parallel feature build aborted (worker died); falling back to serial",
                extra={
                    "sessions": len(session_dirs),
                    "workers": effective_workers,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            resampled_parts.clear()
            skipped.clear()
            _build_serial()
    else:
        _build_serial()
    if not resampled_parts:
        raise ValueError(
            f"no readable sessions among the {len(session_dirs)} given "
            f"(all skipped: {', '.join(skipped) or 'none'})"
        )
    if skipped:
        log.warning(
            "feature frame built from %d/%d sessions; %d skipped: %s",
            len(resampled_parts),
            len(session_dirs),
            len(skipped),
            ", ".join(skipped),
        )
    resampled = pd.concat(resampled_parts, ignore_index=True)
    return drop_fully_stale_rows(resampled)
