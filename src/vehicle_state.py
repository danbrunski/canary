"""One vehicle-state classifier (R03.1).

``VehicleState`` used to be derived three times: ``dashboard.status`` from the
newest session's Parquet, ``dashboard.status`` again from the live feature
source, and ``daemon.inference`` for the scoring gate. The inference copy's
docstring said it mirrored the others, but it had lost the broadcast-RPM
fallback: during an all-DID capture (Mode 01 dark by the ECU's design)
``/status`` said ``engine_running`` while the inference gate said
``engine_off``. The two status copies disagreed with each other too: the live
one required CAN freshness before trusting the broadcast rpm and the Parquet
one did not.

The fallback now lives in :func:`classify_vehicle_state`'s signature. Every
caller hands over the OBD rpm *and* the broadcast rpm (``broadcast_rpm`` is a
required keyword), and the classifier decides which to use, so no caller can
skip it. The derivations below only gather inputs.

A leaf module: it imports nothing from ``dashboard``, ``daemon`` or
``control``, so the inference gate and the non-volatile-write gate can use it
without pulling in the status payload assembly.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from src.decode.engineering import physical_value
from src.operating import ENGINE_RUNNING_MIN_RPM
from src.timefmt import parse_iso_utc

VehicleStateName = Literal[
    "engine_running",
    "ignition_only",
    "engine_off",
    "no_data",
    "replay",
    "unknown",
]

#: A channel that's been quiet longer than this is treated as "not flowing"
#: for the vehicle-state classification. OBD polls at ~1 Hz so 10 s is well
#: past the slowest realistic refresh.
FRESHNESS_THRESHOLD_SECONDS = 10.0

#: Below this RPM (with OBD fresh) we call it "ignition_only / KOEO" rather
#: than "engine running"; the one definition is ``src.operating`` (R01.7).
RPM_RUNNING_THRESHOLD = ENGINE_RUNNING_MIN_RPM

#: The decoded broadcast channel carrying engine speed. Stored as raw counts
#: (``decoded:`` rows are raw by design); scaled through the binding.
BROADCAST_RPM_CHANNEL = "ECM_RPM"


@dataclass(frozen=True)
class VehicleState:
    """Derived operational state — what's the truck doing right now?

    Surfaced in ``GET /status.vehicle_state`` so dashboards / agents stop
    guessing whether "no anomalies" means the detectors are broken vs. the
    engine just isn't running. The class is also documented in
    ``AGENTS.md`` because the live-vs-engine-off-vs-replay confusion has
    been a recurring source of wrong conclusions.
    """

    state: VehicleStateName
    rpm: float | None = None
    coolant_temp: float | None = None
    run_time: float | None = None
    obd_freshness_seconds: float | None = None
    can_freshness_seconds: float | None = None
    replay_id: str | None = None
    #: Wall clock the freshness numbers above were measured against, stamped
    #: by the ``derive_*`` functions. Deriving from Parquet is the one part of
    #: ``/status`` that gets reused across requests (:class:`VehicleStateCache`)
    #: — carrying the derivation instant *inside* the state means no consumer
    #: can mistake a reused reading for a fresh one, whichever view or
    #: endpoint handed it over. ``None`` only for a state built by calling
    #: :func:`classify_vehicle_state` directly.
    derived_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        def _round(value: float | None) -> float | None:
            return None if value is None else round(value, 2)

        return {
            "state": self.state,
            "rpm": _round(self.rpm),
            "coolant_temp": _round(self.coolant_temp),
            "run_time": _round(self.run_time),
            "obd_freshness_seconds": _round(self.obd_freshness_seconds),
            "can_freshness_seconds": _round(self.can_freshness_seconds),
            "replay_id": self.replay_id,
            "derived_at": self.derived_at,
        }


def _fresh(age: float | None, threshold: float) -> bool:
    return age is not None and age <= threshold


def classify_vehicle_state(
    *,
    obd_rpm: float | None,
    broadcast_rpm: float | None,
    coolant_temp: float | None,
    run_time: float | None,
    obd_freshness_seconds: float | None,
    can_freshness_seconds: float | None,
    replay_id: str | None = None,
    freshness_threshold_seconds: float = FRESHNESS_THRESHOLD_SECONDS,
    rpm_running_threshold: float = RPM_RUNNING_THRESHOLD,
) -> VehicleState:
    """Classify the live data source from a few derived signals.

    Pure function so the rule table is testable without spinning up the
    daemon.

    **Which rpm.** ``obd_rpm`` is the Mode 01 reading, ``broadcast_rpm`` the
    decoded broadcast one, already in rpm. The OBD value is used while OBD is
    fresh. Otherwise the broadcast value replaces it, **provided CAN is
    fresh**: during an all-DID capture the ECM stops answering Mode 01 *by
    design* (session 0xC0 and Mode 01 are mutually exclusive on this ECM), so
    "OBD stale" no longer implies a powered-down ECM, and a live broadcast rpm
    is direct evidence either way. A stale OBD rpm is no more informative
    than a missing one (observed 2026-08-07: a second scan tool took the
    tester session, and a 27 s stale 750 rpm held while the broadcast read
    781). A stale broadcast rpm is no better, which is why the fallback needs
    CAN freshness — the rule the live derivation used; the Parquet one did
    not (R03.1, recorded in the checklist). ``broadcast_rpm`` is required so
    that no caller can skip the fallback; pass ``None`` when there is none.

    Precedence:

    1. ``replay`` overrides everything when a replay is running, even if
       OBD/CAN telemetry is also flowing — the replayed records ARE the
       OBD/CAN telemetry from the agent's perspective.
    2. If neither OBD nor CAN have refreshed within
       ``freshness_threshold_seconds``, ``no_data`` (Pi probably
       disconnected or daemon idle).
    3. RPM at or above ``rpm_running_threshold``, from fresh OBD or a fresh
       broadcast → ``engine_running``.
    4. OBD fresh but RPM missing or 0 → ``ignition_only`` (key on, engine
       off — OBD responds, RUN_TIME is 0).
    5. OBD stale but CAN fresh → ``engine_off`` (plugged into the bus
       but the ECM isn't responding to OBD, typical of full power-down).
    6. Anything else → ``unknown`` (caller should still trust the raw
       fields).
    """
    obd_fresh = _fresh(obd_freshness_seconds, freshness_threshold_seconds)
    can_fresh = _fresh(can_freshness_seconds, freshness_threshold_seconds)

    rpm = obd_rpm
    rpm_from_broadcast = False
    if not (obd_fresh and obd_rpm is not None) and broadcast_rpm is not None and can_fresh:
        rpm = broadcast_rpm
        rpm_from_broadcast = True

    if replay_id is not None:
        return VehicleState(
            state="replay",
            rpm=rpm,
            coolant_temp=coolant_temp,
            run_time=run_time,
            obd_freshness_seconds=obd_freshness_seconds,
            can_freshness_seconds=can_freshness_seconds,
            replay_id=replay_id,
        )

    engine_evidence_fresh = obd_fresh or rpm_from_broadcast

    if not obd_fresh and not can_fresh:
        state: VehicleStateName = "no_data"
    elif engine_evidence_fresh and rpm is not None and rpm >= rpm_running_threshold:
        state = "engine_running"
    elif obd_fresh and (rpm is None or rpm == 0):
        state = "ignition_only"
    elif can_fresh and not obd_fresh:
        state = "engine_off"
    else:
        state = "unknown"

    return VehicleState(
        state=state,
        rpm=rpm,
        coolant_temp=coolant_temp,
        run_time=run_time,
        obd_freshness_seconds=obd_freshness_seconds,
        can_freshness_seconds=can_freshness_seconds,
    )


class _ReplayStatusLike(Protocol):
    @property
    def running(self) -> bool: ...

    @property
    def replay_id(self) -> str | None: ...


class ReplayStatusSource(Protocol):
    """What the derivations read from a replay controller."""

    def status(self) -> _ReplayStatusLike: ...


def running_replay_id(replay_controller: ReplayStatusSource | None) -> str | None:
    """The in-flight replay's id, or ``None`` when no replay is running."""
    if replay_controller is None:
        return None
    replay_state = replay_controller.status()
    return replay_state.replay_id if replay_state.running else None


def derive_from_inputs(
    inputs: Mapping[str, float | None], replay_id: str | None, now: datetime
) -> VehicleState:
    """Classify from ``LiveFeatureSource.vehicle_state_inputs`` (no I/O).

    The one in-memory derivation, used by both ``/status`` (when a live
    source is wired) and the inference gate. ``broadcast_rpm`` is already in
    rpm there — ``vehicle_state_inputs`` scales the raw counts.
    """
    return replace(
        classify_vehicle_state(
            obd_rpm=inputs.get("rpm"),
            broadcast_rpm=inputs.get("broadcast_rpm"),
            coolant_temp=inputs.get("coolant_temp"),
            run_time=inputs.get("run_time"),
            obd_freshness_seconds=inputs.get("obd_freshness_seconds"),
            can_freshness_seconds=inputs.get("can_freshness_seconds"),
            replay_id=replay_id,
        ),
        derived_at=now.isoformat(),
    )


class LiveStateSource(Protocol):
    def vehicle_state_inputs(self, now: datetime) -> Mapping[str, float | None]: ...


def derive_from_live_source(
    live_source: LiveStateSource,
    *,
    replay_controller: ReplayStatusSource | None,
    now: datetime | None = None,
) -> VehicleState:
    """:func:`derive_from_inputs` over a live feature source and a replay
    controller — what every in-memory caller actually has in hand."""
    now_dt = datetime.now(UTC) if now is None else now
    return derive_from_inputs(
        live_source.vehicle_state_inputs(now_dt), running_replay_id(replay_controller), now_dt
    )


def _row_timestamp(raw: object) -> datetime | None:
    if isinstance(raw, str):
        try:
            return parse_iso_utc(raw)
        except ValueError:
            return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    return None


def derive_from_latest_rows(
    rows: Iterable[Mapping[str, Any]], *, replay_id: str | None, now: datetime
) -> VehicleState:
    """Classify from a session's latest-per-channel rows (the Parquet variant).

    ``rows`` is what ``dashboard.readers.read_latest_per_channel`` returns:
    one ``{source, channel, value, timestamp}`` per channel. The read stays
    with the caller, so this module does no I/O.

    ``decoded`` rows count as CAN liveness: broadcast decode never stops, even
    when Mode 01 is dark — which it is *by ECU design* during an all-DID
    capture. ``decoded:ECM_RPM`` is stored in raw counts and scaled with the
    binding through the shared accessor (never a local constant).
    """
    obd_rpm: float | None = None
    decoded_rpm: float | None = None
    coolant: float | None = None
    run_time: float | None = None
    obd_latest_ts: datetime | None = None
    can_latest_ts: datetime | None = None

    for row in rows:
        source = row.get("source")
        channel = row.get("channel")
        value = row.get("value")
        ts = _row_timestamp(row.get("timestamp"))
        if source == "obd":
            if ts is not None and (obd_latest_ts is None or ts > obd_latest_ts):
                obd_latest_ts = ts
            if isinstance(value, (int, float)):
                if channel == "RPM":
                    obd_rpm = float(value)
                elif channel == "COOLANT_TEMP":
                    coolant = float(value)
                elif channel == "RUN_TIME":
                    run_time = float(value)
        elif source in ("can", "decoded"):
            if ts is not None and (can_latest_ts is None or ts > can_latest_ts):
                can_latest_ts = ts
            if (
                source == "decoded"
                and channel == BROADCAST_RPM_CHANNEL
                and isinstance(value, (int, float))
            ):
                decoded_rpm = float(value)

    obd_freshness = (now - obd_latest_ts).total_seconds() if obd_latest_ts is not None else None
    can_freshness = (now - can_latest_ts).total_seconds() if can_latest_ts is not None else None
    broadcast_rpm = (
        None
        if decoded_rpm is None
        else physical_value(decoded_rpm, f"decoded:{BROADCAST_RPM_CHANNEL}")
    )
    return replace(
        classify_vehicle_state(
            obd_rpm=obd_rpm,
            broadcast_rpm=broadcast_rpm,
            coolant_temp=coolant,
            run_time=run_time,
            obd_freshness_seconds=obd_freshness,
            can_freshness_seconds=can_freshness,
            replay_id=replay_id,
        ),
        derived_at=now.isoformat(),
    )


#: How long one derived :class:`VehicleState` may be reused across ``/status``
#: requests. Deriving it reads the newest session's Parquet chunks and is
#: essentially the entire cost of the payload — measured 2026-09-17 on a
#: 486-chunk session: 390 ms of a 400 ms build on the dev laptop, and the Pi
#: served the full payload in 1.3–2.6 s. Deliberately far below the
#: classifier's own :data:`FRESHNESS_THRESHOLD_SECONDS` (10 s), so reuse cannot
#: change a verdict that a single slow request would not have changed anyway,
#: and ``VehicleState.derived_at`` reports the age regardless.
VEHICLE_STATE_TTL_SECONDS = 2.0


class VehicleStateCache:
    """One derived vehicle state, shared by every ``/status`` view.

    Owned by the route registration, so no caller has to remember to pass it
    and both views necessarily read the same object: the compact poller and
    the full payload cannot report different vehicle states. Repeat polls pay
    one Parquet read per :data:`VEHICLE_STATE_TTL_SECONDS` instead of one per
    request, which is what makes a 1 Hz watchdog poll affordable on the Pi.

    Not used by the replay guard (``POST /control/replay``), which is a
    one-shot decision rather than a poll and reads fresh.
    """

    def __init__(self, ttl_seconds: float = VEHICLE_STATE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entry: tuple[float, VehicleState] | None = None
        self._lock = threading.Lock()

    def get(self, derive: Callable[[], VehicleState]) -> VehicleState:
        """The cached state, or ``derive()`` when it has aged past the TTL."""
        entry = self._entry
        if entry is not None and time.monotonic() - entry[0] < self._ttl:
            return entry[1]
        # Derivation reads and combines recent Parquet chunks.  Keep it under
        # the refresh lock so simultaneous compact/full status requests after
        # expiry share one disk scan rather than all starting the same costly
        # work.  Recheck after acquiring because another request may have
        # refreshed while this thread waited.
        with self._lock:
            entry = self._entry
            if entry is not None and time.monotonic() - entry[0] < self._ttl:
                return entry[1]
            state = derive()
            self._entry = (time.monotonic(), state)
        return state


__all__ = [
    "BROADCAST_RPM_CHANNEL",
    "FRESHNESS_THRESHOLD_SECONDS",
    "RPM_RUNNING_THRESHOLD",
    "VEHICLE_STATE_TTL_SECONDS",
    "VehicleState",
    "VehicleStateCache",
    "VehicleStateName",
    "classify_vehicle_state",
    "derive_from_inputs",
    "derive_from_latest_rows",
    "derive_from_live_source",
    "running_replay_id",
]
