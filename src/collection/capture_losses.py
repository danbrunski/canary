"""Loss-counter epochs for a capture (roadmap task C03.5).

Every loss counter the daemon keeps is **cumulative since process start**:
``telemetry_sinks.<sink>.dropped_records``, the C07 ``can_receive`` handoff
counters, the collector's own handoff (C03.6, ``obd_collect``) and the
kernel's ``can0`` receive drops. ``health.py`` used to hand the raw storage
counter to the capture evaluator, which described it as loss *during this
capture* — so a 2491-record incident from an earlier drive failed every later
capture in the same daemon process, and a capture that lost rows itself could
hide behind a clean final minute.

The fix is an epoch: snapshot every counter when the capture arms, snapshot
again when it stops, and reconcile the pair. Each counter ends up in exactly
one of three states, and only the first is a claim about the capture:

* ``verified`` — both readings present, ``stop >= start``; ``delta`` is the
  loss attributable to this capture (possibly zero);
* ``reset`` — the counter went *backwards* (a collector restarted, a monitor
  was rebuilt); the loss inside the capture is unknowable from the pair;
* ``unknown`` — one side is missing (no can0 interface, the sink never
  reported, a historic sidecar without epochs). ``delta`` is ``None``, never
  zero: an absent counter is not evidence of no loss.

Counters are grouped by *scope* so dashboard-only drops (``broadcast``,
``live`` — the freshest state wins there by design) are reported beside, and
never mixed into, recording loss (``storage``, ``can_receive``,
``obd_collect``, ``ingest``, ``can0``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Counter scope by sink. Anything not listed is treated as ``recording`` —
#: the conservative reading for a counter nobody has classified.
SINK_SCOPES: dict[str, str] = {
    "storage": "recording",
    "can_receive": "recording",
    "obd_collect": "recording",
    "ingest": "recording",
    "can0": "recording",
    "broadcast": "dashboard",
    "live": "dashboard",
    "gps": "derived",
    "dtc": "derived",
    "monitor": "derived",
}

RECORDING = "recording"
DASHBOARD = "dashboard"
DERIVED = "derived"
#: CAN controller hardware overflow (MCP2515 FIFO): frames lost before the
#: kernel saw them. Tracks bus burstiness, not CANary's own throughput, and is
#: separate from socket loss (``can0.rx_dropped``, still ``recording``). Its
#: effect on a capture's channels is measured directly by their cadence and
#: coverage, so the verdict reports it rather than failing on it.
CONTROLLER = "controller"
_CONTROLLER_COUNTERS = frozenset({"can0.rx_over_errors"})

VERIFIED = "verified"
RESET = "reset"
UNKNOWN = "unknown"

#: Per-sink counter fields that mean "a record did not get through".
LOSS_FIELDS: tuple[str, ...] = ("dropped_records", "errors")
#: Kernel counters read from ``/sys/class/net/can0/statistics``.
CAN0_LOSS_FIELDS: tuple[str, ...] = ("rx_dropped", "rx_over_errors")


def scope_for(counter: str) -> str:
    if counter in _CONTROLLER_COUNTERS:
        return CONTROLLER
    sink = counter.split(".", 1)[0]
    return SINK_SCOPES.get(sink, RECORDING)


@dataclass(frozen=True)
class LossEpoch:
    """Every loss counter at one instant. ``None`` = unavailable then."""

    taken_at_monotonic: float
    taken_at_wall: float
    counters: Mapping[str, int | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "taken_at_monotonic": round(self.taken_at_monotonic, 3),
            "taken_at_wall": round(self.taken_at_wall, 3),
            "counters": dict(self.counters),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> LossEpoch | None:
        counters = payload.get("counters")
        if not isinstance(counters, Mapping):
            return None
        return cls(
            taken_at_monotonic=float(payload.get("taken_at_monotonic") or 0.0),
            taken_at_wall=float(payload.get("taken_at_wall") or 0.0),
            counters={str(k): (None if v is None else int(v)) for k, v in counters.items()},
        )


@dataclass(frozen=True)
class CounterDelta:
    """One counter reconciled across a capture."""

    counter: str
    scope: str
    start: int | None
    stop: int | None
    delta: int | None
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "start": self.start,
            "stop": self.stop,
            "delta": self.delta,
            "status": self.status,
        }


def reconcile_losses(start: LossEpoch | None, stop: LossEpoch | None) -> dict[str, CounterDelta]:
    """Pair two epochs counter by counter. Missing side → ``unknown``."""
    names: set[str] = set()
    if start is not None:
        names |= set(start.counters)
    if stop is not None:
        names |= set(stop.counters)
    out: dict[str, CounterDelta] = {}
    for name in sorted(names):
        n0 = None if start is None else start.counters.get(name)
        n1 = None if stop is None else stop.counters.get(name)
        out[name] = _reconcile_one(name, n0, n1)
    return out


def _reconcile_one(name: str, n0: int | None, n1: int | None) -> CounterDelta:
    scope = scope_for(name)
    if n0 is None or n1 is None:
        return CounterDelta(name, scope, n0, n1, None, UNKNOWN)
    if n1 < n0:
        return CounterDelta(name, scope, n0, n1, None, RESET)
    return CounterDelta(name, scope, n0, n1, n1 - n0, VERIFIED)


def summarize_losses(deltas: Mapping[str, CounterDelta]) -> dict[str, Any]:
    """Group reconciled counters by scope with a per-scope verdict.

    ``lost`` is the verified loss inside the scope, or ``None`` when any
    counter in it is unverified — a partial sum would read as a total.
    """
    scopes: dict[str, dict[str, Any]] = {}
    for name, delta in deltas.items():
        block = scopes.setdefault(
            delta.scope, {"counters": {}, "lost": 0, "unverified": [], "status": VERIFIED}
        )
        block["counters"][name] = delta.to_dict()
        if delta.status == VERIFIED:
            block["lost"] += int(delta.delta or 0)
        else:
            block["unverified"].append(name)
    for block in scopes.values():
        if block["unverified"]:
            block["status"] = UNKNOWN
            block["lost"] = None
    for scope in (RECORDING, DASHBOARD):
        scopes.setdefault(
            scope, {"counters": {}, "lost": None, "unverified": [], "status": UNKNOWN}
        )
    return scopes


def unknown_losses() -> dict[str, Any]:
    """The summary for a capture with no epochs at all (historic sidecars)."""
    return summarize_losses({})


__all__ = [
    "CAN0_LOSS_FIELDS",
    "CONTROLLER",
    "DASHBOARD",
    "DERIVED",
    "LOSS_FIELDS",
    "RECORDING",
    "RESET",
    "SINK_SCOPES",
    "UNKNOWN",
    "VERIFIED",
    "CounterDelta",
    "LossEpoch",
    "reconcile_losses",
    "scope_for",
    "summarize_losses",
    "unknown_losses",
]
