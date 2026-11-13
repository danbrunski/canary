"""The ROM backup job's and the VIN read's view of ``vehicle_state`` (R29.7).

:func:`vehicle_reading` lived in ``dashboard/routes/rom_library.py`` and
``dashboard/routes/control/system.py`` imported it: one route module serving
another. It belongs here, beside :class:`~src.control.rom_backup.VehicleReading`
and the classifier (:mod:`src.vehicle_state`).

**What stays in the dashboard.** The no-live-source fallback classifies the
newest session's stored rows, and reading those is
:func:`src.dashboard.status.derive_vehicle_state_from_storage`, a dashboard
function this layer may not import (``tests/test_layering.py``). It arrives as
``storage_state``, a required argument, so a caller cannot forget the fallback
and silently get none; the dashboard binds it once, as
:func:`src.dashboard.readers.read_vehicle_reading`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from src.control.rom_backup import VehicleReading
from src.vehicle_state import (
    LiveStateSource,
    ReplayStatusSource,
    VehicleState,
    derive_from_live_source,
)


def vehicle_reading(
    *,
    live_source: LiveStateSource | None,
    replay_controller: ReplayStatusSource | None,
    storage_state: Callable[[], VehicleState],
) -> VehicleReading:
    """The backup job's view of ``vehicle_state``, from the same classifier.

    The live source gives the broadcast engine speed the job watches while
    OBD polling is paused; without one (live inference off) the state comes
    from ``storage_state`` and the watch is unavailable, which refuses the start.
    """
    now = datetime.now(UTC)
    if live_source is None:
        state = storage_state()
        return VehicleReading(
            state=state.state,
            rpm=state.rpm,
            broadcast_rpm=None,
            obd_freshness_seconds=state.obd_freshness_seconds,
            watch_unavailable="no live telemetry source; live inference is not running",
        )
    state = derive_from_live_source(live_source, replay_controller=replay_controller, now=now)
    # Fresh-gated by the classifier: ``None`` when absent or CAN is stale.
    broadcast = state.broadcast_rpm
    return VehicleReading(
        state=state.state,
        rpm=state.rpm,
        broadcast_rpm=broadcast,
        obd_freshness_seconds=state.obd_freshness_seconds,
        watch_unavailable=None
        if broadcast is not None
        else "broadcast engine speed absent or stale",
    )


__all__ = ["vehicle_reading"]
