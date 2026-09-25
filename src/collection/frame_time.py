"""When a received CAN frame was acquired: the one policy (roadmap R02.4).

Every receive path stamps its rows here — broadcast frames
(``can_collector.frame_to_record``), OBD responses (``can_obd_collector``)
and the add-on sensor bus (``sensor_can_collector.frame_to_records``). They
used to hold three policies, so after a suspend-stale SocketCAN stamp a
broadcast row and an OBD reading from the same instant were stamped minutes
apart, and same-instant pairing (``compare_channels``) silently paired the
wrong samples.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import can

from src.collection.obd_reading import AcquiredClock

#: A bus-driver frame stamp further than this from ``time.time()`` is not
#: trusted as the acquisition clock (an adapter with no clock reports 0.0;
#: a stale SocketCAN stamp after a suspend can be minutes off). The
#: userspace receipt time is used instead (C02.7).
KERNEL_STAMP_MAX_SKEW_SECONDS = 5.0


@dataclass(frozen=True)
class FrameArrival:
    """When a frame reached the collector, and which clock says so."""

    at: float
    clock: AcquiredClock


def frame_arrival(msg: can.Message, *, now: float | None = None) -> FrameArrival:
    """Acquisition instant for ``msg`` (C02.7).

    Prefers the bus driver's own stamp (``Message.timestamp`` — the kernel
    ``SO_TIMESTAMP`` on SocketCAN, the driver's receive-time read on slcan)
    over a ``time.time()`` taken after handling, provided it agrees with the
    wall clock to within :data:`KERNEL_STAMP_MAX_SKEW_SECONDS`. Adapters with
    no clock report 0.0 and fall back to userspace time. ``now`` is the
    caller's wall-clock read (``time.time()`` when omitted).
    """
    if now is None:
        now = time.time()
    stamp = getattr(msg, "timestamp", None)
    if isinstance(stamp, (int, float)) and abs(now - float(stamp)) <= KERNEL_STAMP_MAX_SKEW_SECONDS:
        return FrameArrival(at=float(stamp), clock="kernel")
    return FrameArrival(at=now, clock="userspace")


__all__ = ["KERNEL_STAMP_MAX_SKEW_SECONDS", "FrameArrival", "frame_arrival"]
