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

import asyncio
import logging
import multiprocessing
import resource
import threading
from concurrent.futures import Executor, ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Seconds a terminated child gets to exit before it is killed.
TERMINATE_GRACE_SECONDS = 5.0

#: The second brain's idle-time child (``src.control.brain_jobs``).
BRAIN_CHILD = "brain"
#: Longest a yielded brain child can take to be gone: the reaper's SIGTERM
#: grace, then SIGKILL and join. The digest waits at most this long for it.
CHILD_EXIT_BOUND_SECONDS = TERMINATE_GRACE_SECONDS + 1.0


def spawn_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))


class AddressSpaceCapRefusedError(RuntimeError):
    """:func:`limit_address_space` was asked to cap a process that is not a
    disposable ``multiprocessing`` child."""


def limit_address_space(nbytes: int) -> tuple[int, int]:
    """Cap this process's address space at ``nbytes`` and return the limit.

    Never *raises* the limit: an existing hard limit (a cgroup, a systemd
    ``LimitAS=``) stays the ceiling. Returns what ``getrlimit`` reports
    afterwards so a caller — or a test running this in a real child — can
    assert the limit is actually in force rather than trusting the call.

    ``RLIMIT_AS`` bounds *virtual* address space, which is deliberately
    stricter than RSS: pandas/pyarrow map more than they resident, so this
    refuses somewhat before the kernel would have killed anything. Refusing
    early with a named reason is the trade being made.

    The cap is per *process* and permanent, so it may only land on a
    disposable ``multiprocessing`` child. Anywhere else (the daemon itself, or
    a test that runs a child's work in a thread) it raises
    :class:`AddressSpaceCapRefusedError` and changes nothing. On 2026-11-12 a
    test that handed the digest a thread pool capped its pytest worker; every
    later thread that worker started died in its bootstrap, and the push gate
    hung at 99 % for an hour on an unrelated test. A test that runs a child's
    entry in-process patches the limiter out, as the digest tests do.
    """
    if multiprocessing.parent_process() is None:
        raise AddressSpaceCapRefusedError(
            f"refusing to cap this process's address space at {nbytes} bytes: it is not a "
            "multiprocessing child, and RLIMIT_AS would stay on it for good (a capped "
            "process cannot start threads). Run the work in a spawn child, or patch the "
            "limiter out in a test that runs the child's entry in-process."
        )
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    target = nbytes if hard == resource.RLIM_INFINITY else min(nbytes, hard)
    if soft != resource.RLIM_INFINITY:
        target = min(target, soft)
    resource.setrlimit(resource.RLIMIT_AS, (target, hard))
    return resource.getrlimit(resource.RLIMIT_AS)


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


# -- in-flight registry -------------------------------------------------------
#
# The digest and the workbench share one memory budget with the brain's
# idle-time jobs: at most one analysis child at a time. They live in different
# modules and layers above this one, so the registry is here, below all three.
# A child counts as alive from claim until its *process* has exited — a
# terminated child still holds its memory until the reaper has seen it go.


@dataclass
class _Slot:
    running: bool = True
    processes: list[Any] = field(default_factory=list)
    yield_requested: bool = False


_SLOTS: dict[str, _Slot] = {}
_SLOTS_LOCK = threading.Lock()


def claim_child(name: str) -> None:
    """Mark ``name``'s child as starting (alive until released *and* exited)."""
    with _SLOTS_LOCK:
        _SLOTS[name] = _Slot()


def attach_child(name: str, pool: Executor) -> None:
    """Record the processes of ``pool`` (a spawn pool) under ``name``. Call
    right after submitting: ``ProcessPoolExecutor`` spawns on submit."""
    if not isinstance(pool, ProcessPoolExecutor):
        return
    with _SLOTS_LOCK:
        slot = _SLOTS.get(name)
        if slot is not None:
            slot.processes.extend((pool._processes or {}).values())


def release_child(name: str) -> None:
    """The computation is over; the slot stays alive until its processes exit."""
    with _SLOTS_LOCK:
        slot = _SLOTS.get(name)
        if slot is not None:
            slot.running = False
            slot.yield_requested = False


def child_alive(name: str) -> bool:
    """Whether ``name``'s child is computing or its process has not exited yet."""
    with _SLOTS_LOCK:
        slot = _SLOTS.get(name)
        if slot is None:
            return False
        alive = []
        for proc in slot.processes:
            try:
                if proc.is_alive():
                    alive.append(proc)
            except Exception:  # noqa: BLE001 - a reaped handle is gone
                continue
        slot.processes = alive
        if not slot.running and not alive:
            del _SLOTS[name]
            return False
        return True


def request_yield(name: str) -> None:
    """Ask ``name``'s child to stop for higher-priority work (it polls this)."""
    with _SLOTS_LOCK:
        slot = _SLOTS.get(name)
        if slot is not None and slot.running:
            slot.yield_requested = True


def yield_requested(name: str) -> bool:
    with _SLOTS_LOCK:
        slot = _SLOTS.get(name)
        return slot is not None and slot.yield_requested


async def wait_for_child_exit(name: str, timeout: float = CHILD_EXIT_BOUND_SECONDS) -> bool:
    """Ask ``name``'s child to yield and wait, without blocking the loop, until
    its process is gone; ``False`` if it is still alive after ``timeout``."""
    if not child_alive(name):
        return True
    request_yield(name)
    deadline = asyncio.get_running_loop().time() + timeout
    while child_alive(name):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


__all__ = [
    "BRAIN_CHILD",
    "CHILD_EXIT_BOUND_SECONDS",
    "TERMINATE_GRACE_SECONDS",
    "attach_child",
    "child_alive",
    "claim_child",
    "discard_pool",
    "release_child",
    "request_yield",
    "spawn_pool",
    "wait_for_child_exit",
    "yield_requested",
]
