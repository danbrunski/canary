"""Opening a CAN bus, and the frame primitives every receive path shares (R02.12).

``bus_factory`` lived in ``can_collector``, the passive sniffer, whose
docstring says it never calls ``bus.send()`` — while most of its fifteen
importers transmit (the OBD control routes, calibration, the probe tools,
``preflight``). Whether a bus is ever written is the caller's business, not
the constructor's, so construction lives here. ``can_collector`` re-exports
``bus_factory`` and ``frame_to_record`` for existing callers.

Also here: :func:`receive_frame`, the one place a frame is taken off a bus
(slcan parse errors become counted, dropped frames — R02.1), and
:func:`frame_to_record`, stamped by the shared acquisition-time policy
(R02.4).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import can

from src.collection.frame_time import frame_arrival
from src.collection.queueing import flush_dropped_can_frame_log, log_dropped_can_frame
from src.config import CanBusConfig
from src.telemetry import CanFrameKind, TelemetryRecord

FrameTap = Callable[[can.Message], None]
ParseErrorTap = Callable[[Exception], None]

#: A driver-level parse failure: python-can's slcan parser raises these on a
#: corrupt serial line. One malformed frame, never a configuration error.
FRAME_PARSE_ERRORS: tuple[type[Exception], ...] = (ValueError, IndexError)

#: Parse-error log throttle identity for a receiver that does not name one.
DEFAULT_PARSE_ERROR_OWNER = "can-obd-client"


def bus_kwargs(interface: str, channel: str, bitrate: int) -> dict[str, Any]:
    """``can.Bus`` keyword arguments; ``bitrate`` is omitted for ``virtual``.

    The virtual backend rejects ``bitrate`` as unknown. One rule for the
    vehicle bus and the add-on sensor bus.
    """
    kwargs: dict[str, Any] = {"interface": interface, "channel": channel}
    if interface != "virtual":
        kwargs["bitrate"] = bitrate
    return kwargs


def bus_factory(spec: CanBusConfig) -> can.BusABC:
    """Construct a python-can Bus from config.

    Opening a bus says nothing about transmitting on it. The passive sniffer
    (``can_collector``) never calls ``bus.send()``; the OBD paths transmit
    through ``tx_gate``. Hardware bus-monitoring (no ACK either) is
    adapter-dependent: python-can's slcan ``listen_only=True`` sends the ``L``
    open command, but the knockoff "CANable2" CAN-FD adapter used in dev
    returns zero frames in that mode (its firmware doesn't honor ``L``), so
    the bus is opened normally (``O``). SocketCAN's listen-only is configured
    out-of-band via ``ip link set ... listen-only on`` (see
    docs/can-setup.md).
    """
    return can.Bus(**bus_kwargs(spec.interface, spec.channel, spec.bitrate))


def frame_to_record(msg: can.Message) -> TelemetryRecord:
    """Convert a python-can message into the shared telemetry schema.

    Stamped by :func:`~src.collection.frame_time.frame_arrival`, the policy
    the OBD path uses, so a broadcast row and an OBD reading from the same
    instant carry the same clock even when the driver stamp is stale (R02.4).

    Error and remote frames are tagged (``frame_kind``, P02.10): they keep
    their id spelling (``CAN_ERR_CRTL`` is ``0x4``), and change-only storage
    keeps every one, because each is an event (an RX overflow, a TX warning),
    not a repeating value.
    """
    kind: CanFrameKind | None = (
        "error" if msg.is_error_frame else "remote" if msg.is_remote_frame else None
    )
    return TelemetryRecord.can(
        frame_arrival(msg).at,
        int(msg.arbitration_id),
        bytes(msg.data),
        frame_kind=kind,
    )


def receive_frame(
    bus: can.BusABC,
    *,
    timeout: float,
    on_frame: FrameTap | None,
    on_parse_error: ParseErrorTap | None = None,
    parse_error_owner: str = DEFAULT_PARSE_ERROR_OWNER,
) -> can.Message | None:
    """The one place a frame is taken off an OBD-owning bus.

    Whatever ``bus.recv`` returns is handed to ``on_frame`` first — before
    any caller looks at its arbitration id — so raw capture sees every frame
    exactly once regardless of which request, drain or probe was waiting.

    A driver parse error (:data:`FRAME_PARSE_ERRORS`) is a dropped frame, not
    an exception: it is reported to ``on_parse_error`` (the receiver's exact
    counter), logged through the throttled ``log_dropped_can_frame`` under
    ``parse_error_owner``, and ``None`` is returned. Letting it propagate
    ended the collector (R02.1): a ``ValueError`` raised inside a discovery
    probe reached ``run_with_reconnect(reraise=(ValueError,))``, which treats
    it as a configuration error. Every receiver goes through here, so none
    has to remember that.
    """
    try:
        msg = bus.recv(timeout=timeout)
    except FRAME_PARSE_ERRORS as exc:
        if on_parse_error is not None:
            on_parse_error(exc)
        log_dropped_can_frame(exc, owner=parse_error_owner)
        return None
    if msg is None:
        return None
    flush_dropped_can_frame_log(owner=parse_error_owner)
    if on_frame is not None:
        on_frame(msg)
    return msg


__all__ = [
    "DEFAULT_PARSE_ERROR_OWNER",
    "FRAME_PARSE_ERRORS",
    "FrameTap",
    "ParseErrorTap",
    "bus_factory",
    "bus_kwargs",
    "frame_to_record",
    "receive_frame",
]
