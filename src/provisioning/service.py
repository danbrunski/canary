"""What ``python -m src.provisioning run`` keeps alive (roadmap N5.1).

Two jobs share one event loop: the BLE provisioning server (:mod:`.bluez`)
and the network-control socket (:mod:`.netsock`). They are independent --
a Pi with no Bluetooth adapter, a blocked radio, or no BlueZ at all still
needs its dashboard to manage WiFi -- so a failure in one is logged and
retried, and never ends the process or stops the other.

Before the socket existed a BLE failure exited non-zero and systemd's
``Restart=on-failure`` tried again five seconds later. The BLE job keeps that
cadence in-process, with a back-off for a Pi that will never have an adapter,
and re-runs ``rfkill unblock bluetooth`` first, as the unit's ``ExecStartPre``
does on a restart.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from src.provisioning.netsock import (
    DEFAULT_JOIN_TIMEOUT_S,
    NetworkControlServer,
    NetworkOps,
)
from src.provisioning.network import run_command

log = logging.getLogger(__name__)

RETRY_FIRST_S = 5.0
RETRY_MAX_S = 60.0
#: A job that ran this long before failing was working; its next failure
#: starts the back-off over.
HEALTHY_AFTER_S = 60.0
_RFKILL = "/usr/sbin/rfkill"


def unblock_bluetooth() -> None:
    """``rfkill unblock bluetooth``; a missing binary or a refusal is not fatal."""
    result = run_command([_RFKILL, "unblock", "bluetooth"], timeout=5)
    if result.returncode not in (0, 127):
        log.warning("rfkill unblock bluetooth failed (%d): %s", result.returncode, result.stderr)


async def supervise(
    name: str,
    job: Callable[[], Awaitable[None]],
    *,
    retry_first_s: float = RETRY_FIRST_S,
    retry_max_s: float = RETRY_MAX_S,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run ``job`` forever, retrying with a growing delay when it fails or returns."""
    delay = retry_first_s
    while True:
        started = clock()
        try:
            await job()
            log.warning("%s stopped; restarting in %.0f s", name, delay)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one job's failure must not end the other
            log.exception("%s failed; retrying in %.0f s", name, delay)
        if clock() - started >= HEALTHY_AFTER_S:
            delay = retry_first_s
        await asyncio.sleep(delay)
        delay = min(delay * 2, retry_max_s)


async def run_service(
    ops: NetworkOps,
    ble: Callable[[], Awaitable[None]],
    *,
    socket_path: Path | None = None,
    join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
    retry_first_s: float = RETRY_FIRST_S,
    retry_max_s: float = RETRY_MAX_S,
) -> None:
    """Serve the control socket and BLE together until cancelled."""
    server = NetworkControlServer(ops, path=socket_path, join_timeout_s=join_timeout_s)

    async def bluetooth() -> None:
        await asyncio.to_thread(unblock_bluetooth)
        await ble()

    # return_exceptions: on cancel, wait for both jobs to finish unwinding (the
    # socket file is removed there) instead of returning at the first to stop.
    await asyncio.gather(
        supervise("network control socket", server.serve, retry_first_s=1.0, retry_max_s=30.0),
        supervise(
            "BLE provisioning", bluetooth, retry_first_s=retry_first_s, retry_max_s=retry_max_s
        ),
        return_exceptions=True,
    )


def cancel_on_signals(task: asyncio.Task[None]) -> None:
    """SIGTERM/SIGINT cancel ``task``, so the socket file is removed on the way out."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
