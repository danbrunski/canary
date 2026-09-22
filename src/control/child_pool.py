"""Single-use spawn pools for whole-session work kept off the event loop.

The trip-end digest, capture-quality recompute and workbench analyses each run
one computation in a fresh ``spawn`` child so a multi-gigabyte session never
shares the collector's heap. Every path that does not produce a result must
also end that child: a timed-out or cancelled computation would otherwise keep
a core and its frames until it finished on its own.

SIGTERM alone is not a guarantee. It can land while the child is still
importing pandas and take seconds to act, or be ignored outright if the
disposition was inherited as ``SIG_IGN``. So the discard sends SIGTERM and a
background reaper escalates to SIGKILL after a grace period. The reaper is a
daemon thread; the event loop never waits on a child.
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
from concurrent.futures import Executor, ProcessPoolExecutor
from typing import Any

log = logging.getLogger(__name__)

#: Seconds a terminated child gets to exit before it is killed.
TERMINATE_GRACE_SECONDS = 5.0


def spawn_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))


def discard_pool(
    pool: Executor, *, terminate: bool, grace: float = TERMINATE_GRACE_SECONDS
) -> None:
    """Release a single-use pool without waiting on the loop for its child.

    With ``terminate`` the child is sent SIGTERM now and SIGKILL if it is still
    alive ``grace`` seconds later.
    """
    processes: list[Any] = []
    if terminate and isinstance(pool, ProcessPoolExecutor):
        # No public way to reach the workers before 3.14's terminate_workers().
        processes = list((pool._processes or {}).values())
    pool.shutdown(wait=False, cancel_futures=True)
    for proc in processes:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001 — already gone is the goal
            log.debug("pool child already exited", exc_info=True)
            continue
        threading.Thread(
            target=_reap, args=(proc, grace), name="child-pool-reaper", daemon=True
        ).start()


def _reap(proc: Any, grace: float) -> None:
    try:
        proc.join(grace)
        if proc.is_alive():
            log.warning("pool child pid %s ignored SIGTERM for %.0fs; killing", proc.pid, grace)
            proc.kill()
            proc.join()
    except Exception:  # noqa: BLE001 — already gone is the goal
        log.debug("pool child reap failed", exc_info=True)


__all__ = ["TERMINATE_GRACE_SECONDS", "discard_pool", "spawn_pool"]
