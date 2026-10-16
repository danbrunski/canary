"""The ``collection:`` section: OBD polling, storage, replay, recording
arming and idle shutdown (R21).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from src.config.base import (
    DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS,
    DEFAULT_PRIORITY_CHANNELS,
    _Strict,
    validate_broadcast_stale_timeout,
)

#: Named ``src.config`` (not ``__name__``, ``src.config.collection``) so a record
#: from the retired-key warning keeps the logger name it had in the single file.
log = logging.getLogger("src.config")


class RecordArmingConfig(_Strict):
    """Operator-intent gate deferring full-rate recording until the vehicle
    looks active (Task 8.6, R14 in ``docs/implementation-tasks.md``).

    **Ships disabled** — a hard default, matching the other config-gated
    background features in this file: current continuous-logging behavior
    (Done-Definition v1 #2) is unchanged byte-for-byte until an operator
    opts in. See ``docs/storage-lifecycle-fix.md`` for the arm/disarm
    design (why disarm only happens at a session-rotation boundary, and why
    pre-arm data is dropped rather than buffered).
    """

    enabled: bool = False
    #: Sources whose mere presence proves the vehicle bus is connected and
    #: talking — the deciding factor for recording. GPS/IMU/derived chatter
    #: whenever the Pi has power, so they never count. Set ``[]`` to gate on
    #: ``arm_channels`` values alone (e.g. a vehicle whose bus stays awake
    #: while parked).
    arm_sources: tuple[str, ...] = ("can", "obd")
    #: ``TelemetryRecord.channel`` names (unprefixed — e.g. ``"RPM"``) that
    #: can arm the gate when ``arm_sources`` doesn't already cover it.
    arm_channels: tuple[str, ...] = ("RPM",)
    #: A configured channel's numeric value must exceed this to arm.
    arm_threshold: float = 0.0
    #: Disarm after this many seconds of records with no arm-channel evidence
    #: (key out, adapter unplugged) — otherwise a post-drive parked night with
    #: the Pi on the UPS stays armed and records GPS chatter until the *next*
    #: key-on rotates the session. Generous enough that a stall or a long
    #: light never trips it (an idling engine keeps emitting RPM > 0), and
    #: cranking RPM re-arms instantly regardless. ``null`` restores the v1
    #: rotation-only disarm.
    disarm_after_seconds: float | None = 300.0


class IdleShutdownConfig(_Strict):
    """Escalate a sustained parked state to a system shutdown.

    The software half of ``docs/CANary-HAT-Circuits.md`` §3.7.6: the rig has no
    ignition sense, so the alternative to unplugging it between drives is to
    infer engine-off from bus activity and power down. See
    ``src.daemon.idle_shutdown`` for the inhibits and why each exists.

    **Ships disabled.** A box that powers itself off unattended is not
    something to enable by surprise, and the same hard default applies to
    every other config-gated background feature in this file.

    Not to be confused with ``scripts/ups_shutdown_watch.py``, which is a
    *safety* watchdog for actual input-power loss and runs standalone so it
    survives a hung daemon. This is an idle timeout and lives in the daemon
    because that is where the vehicle-state classifier lives.
    """

    enabled: bool = False
    #: Vehicle states counting as parked/disconnected. ``ignition_only`` is
    #: deliberately absent — KOEO is an operator at the wheel running
    #: diagnostics — and so is ``unknown``: if the classifier could not
    #: decide, neither should this.
    idle_states: tuple[str, ...] = ("engine_off", "parked", "no_data")
    #: Continuous seconds in an idle state before powering off. Generous by
    #: default: the cost of waiting is a few minutes of idle draw, the cost
    #: of firing early is a shutdown mid-drive.
    idle_after_seconds: float = 1800.0
    #: Command used to power down. ``systemctl suspend`` is the obvious
    #: alternative, but the Pi 5 has no meaningful suspend state, so
    #: poweroff is the only one that actually saves anything.
    poweroff_command: tuple[str, ...] = ("systemctl", "poweroff")
    #: Honour the operator hold that ``ups_shutdown_watch.py`` reads. A hold
    #: respected by only one of the two shutdown paths is worse than none.
    respect_keepalive: bool = True
    keepalive_file: str = "/run/canary/ups_keepalive"
    #: The ``ups-shutdown`` watchdog's JSON heartbeat. Its ``input_lost``
    #: field is what distinguishes a parked vehicle (input present, shut
    #: down) from a bench Pi on USB-C with the barrel jack out (input lost,
    #: stay up). Read as a file — never GPIO6 directly, which the watchdog
    #: owns and a second ``lgpio`` claimant collides with.
    status_file: str = "/run/canary/ups_status"
    #: Log the decision instead of acting on it. The way to try this on a
    #: live rig without risking a surprise poweroff.
    dry_run: bool = False


class CollectionConfig(_Strict):
    obd_source: Literal["canable", "elm327", "disabled"] = "canable"
    #: Hold OBD polling (and the startup VIN probe) while the vehicle is
    #: parked: a battery on the port (``hardware.supply_voltage``) and no frame
    #: from any module for ``vehicle_power.ASLEEP_SECONDS``. Polling a sleeping
    #: bus only fills the transmit queue and may wake modules and drain the
    #: battery (operator, 2026-10-05). It resumes when a module talks again.
    #: Only for a vehicle whose OBD port carries broadcast traffic whenever
    #: the ignition is on (the Xterra). A gatewayed port is silent until asked,
    #: and this would never poll it, hence off by default.
    obd_sleeps_with_bus: bool = False
    obd_pids: tuple[str, ...] = ("auto",)
    obd_dids: tuple[str, ...] = ()
    obd_did_request_id: int = 0x7E0
    #: Mode-22 gating session (UDS 0x10 sub-function). ``"auto"`` (default)
    #: reads it from data/dids.yaml's access block; ``null`` forces bare DID
    #: polling (ECUs that answer 0x22 in the default session); an int
    #: overrides the map. When a session gates Mode 22 it is mutually
    #: exclusive with Mode 01 on the bus, so DIDs are polled in short
    #: scheduled windows (see the obd_did_window_* knobs below).
    obd_did_session: int | Literal["auto"] | None = "auto"
    #: ``null`` → keep-alive cadence from dids.yaml access block (or 2.0 s).
    #: PIDs per Mode-01 request. SAE J1979 allows six; the collector narrows
    #: 6 -> 4 -> 2 -> off on repeated failures, so a vehicle that refuses
    #: batching degrades on its own rather than needing this tuned by hand.
    #: Batched PIDs share one request *and one timestamp*, which is what makes
    #: a capture read as synchronous rows instead of interleaved samples.
    obd_batch_max_pids: int = Field(default=6, ge=1, le=6)
    #: Maximum DIDs per Mode-22 request. Independent from Mode 01 capability;
    #: 1 is off. Only DIDs with provenance-backed full response widths batch.
    obd_did_batch_max_dids: int = Field(default=1, ge=1, le=3)
    obd_did_session_keepalive_seconds: float | None = None
    #: How often a DID window opens. Each window reads every active DID once,
    #: so this is the effective per-DID sample interval.
    obd_did_window_interval_seconds: float = 60.0
    #: Start-to-start period of one Mode 01 + Mode 22 cycle (C04.3). When
    #: set, windows open at ``anchor + k * cycle`` and the interval above is
    #: ignored; ``null`` (default) keeps the end-to-start interval policy.
    #: A capture profile's ``did_cycle_seconds`` overrides either.
    obd_did_cycle_seconds: float | None = None
    #: Hard cap on one window's duration (it normally closes much sooner,
    #: as soon as every active DID has been attempted once).
    obd_did_window_max_seconds: float = 10.0
    #: Quiet time after a window so the ECM's diagnostic session expires
    #: (S3 timeout) before Mode 01 polling resumes — resuming earlier would
    #: time the first Mode 01 polls out into their miss backoff (30 s; 10 s
    #: for the priority channels).
    obd_did_session_lapse_seconds: float = 5.0
    obd_request_timeout_seconds: float = 1.0
    obd_poll_rate_hz: float
    #: Channels that hold their tier rate when the request plan asks for more
    #: than the bus can serve; everything else degrades around them. Defaults
    #: to the signals ``preprocessing.operating_modes`` gates on — starve
    #: those and the segmenter can't classify a row at all. Empty disables the
    #: priority lane (uniform degradation, the pre-1.11 behavior).
    obd_priority_channels: tuple[str, ...] = DEFAULT_PRIORITY_CHANNELS
    #: Maximum cadence for exploratory support probes. Discovery still obeys
    #: the global bus budget, but it cannot consume every otherwise-free slot
    #: while confirmed collection work is sparse.
    obd_discovery_budget_requests_per_second: float = Field(default=32.0, gt=0)
    #: Cadence (s) of the Mode 03/07/0A stored/pending/permanent DTC poll
    #: (Task 1.3) on the Canable-native path. DTCs change on a minutes
    #: scale, not seconds, so this is its own knob rather than a
    #: `PollTier` scaled off `obd_poll_rate_hz`. ``<= 0`` disables it.
    obd_dtc_poll_interval_seconds: float = 300.0
    #: Cadence (s) of the Mode 06 on-board monitor poll (Task 3.1) — misfire
    #: counts + monitor margins, same "own knob, not a `PollTier`" cadence
    #: model as ``obd_dtc_poll_interval_seconds`` above. ``<= 0`` disables it.
    obd_monitor_poll_interval_seconds: float = 300.0
    #: Mode 06 monitor IDs to poll. ``"auto"`` walks the standard supported-MID
    #: bitmaps once when the collector connects; an explicit tuple skips
    #: discovery for ECUs with a known fixed set.
    obd_monitor_mids: Literal["auto"] | tuple[int, ...] = "auto"
    storage_path: Path
    session_retention_days: int
    storage_warning_mb: int
    #: Byte-bounded ring buffer. When set, the daemon's retention sweeper
    #: evicts oldest unpinned sessions until total session bytes fall under
    #: this cap (pinned + active dirs are never evicted). ``None`` disables
    #: the cap (age-only retention) — the safe default for configs predating
    #: Phase 8.2.
    storage_max_mb: int | None = None
    reconnect_initial_delay_seconds: float = 10.0
    reconnect_max_delay_seconds: float = 60.0
    queue_max_size: int = 10_000
    queue_warning_fraction: float = 0.8
    #: P02.10 (operator 2026-09-26): a broadcast CAN frame is stored only when
    #: its payload differs from its id's previous frame, and an unchanged id is
    #: re-written once per this timeout (one second before it: derived, not a
    #: second knob), so its value never lapses. The same number is the 1 Hz
    #: resampler's stale class for broadcast channels (``can:``/``decoded:``/
    #: ``dbc:``) and the per-sample readers' as-of validity, so storage and
    #: every reader agree on it. Under 2 s is refused
    #: (:func:`validate_broadcast_stale_timeout`, above);
    #: it must also stay under ``preprocessing.session_gap_seconds``
    #: (:class:`CanaryConfig` checks). Changing it changes the models'
    #: broadcast ``__gap``/``__arrived`` features: a feature-spec change.
    broadcast_stale_timeout_seconds: float = DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS
    #: Simulation backend: when set, the daemon replays this recorded
    #: session (dir path or id under storage) onto the telemetry queue
    #: *instead of* the real CAN/OBD collectors — no hardware needed, the
    #: dashboard shows previously-collected signals. Empty = normal capture.
    replay_session: str | None = None
    replay_speed: float = 1.0
    replay_loop: bool = True
    #: Age in days at which ``_replay-*`` session directories are evicted by
    #: the retention sweeper. Independent from ``session_retention_days``
    #: because replay output is test data that the operator may want to
    #: keep visible briefly but not forever. Set to a large value to keep
    #: replays around indefinitely; the ``_`` prefix already excludes them
    #: from training discovery and from the live-session listing.
    replay_retention_days: int = 7
    #: Operator-intent recording gate (Task 8.6). Defaults off.
    record_arming: RecordArmingConfig = RecordArmingConfig()
    idle_shutdown: IdleShutdownConfig = IdleShutdownConfig()

    @field_validator("broadcast_stale_timeout_seconds")
    @classmethod
    def _broadcast_stale_timeout_floor(cls, value: float) -> float:
        return validate_broadcast_stale_timeout(value)

    @model_validator(mode="before")
    @classmethod
    def _retired_persist_decoded_confidence(cls, data: Any) -> Any:
        """``persist_decoded_confidence`` is retired (P02.8, operator 2026-09-26).

        It chose which bindings' ``decoded`` rows were stored beside the raw
        frames. Nothing stores them now, whatever it says: the session readers
        decode the frames on read, with ``confirmed`` bindings plus any marked
        ``persist: true`` in ``bindings.yaml``
        (:func:`src.decode.bindings.on_read_signals`), the only value this key
        ever shipped with. A config that still sets it loads (the Pi's
        ``/etc/canary/canary.yaml`` does), and says so; a value other than
        ``[confirmed]`` is named, because nothing honours it any more.
        """
        if not isinstance(data, dict) or "persist_decoded_confidence" not in data:
            return data
        data = dict(data)
        value = data.pop("persist_decoded_confidence")
        levels = sorted(str(v) for v in value) if isinstance(value, (list, tuple)) else value
        if levels == ["confirmed"]:
            log.warning(
                "collection.persist_decoded_confidence is retired and ignored (P02.8): "
                "decoded rows are no longer stored; readers decode confirmed and "
                "persist: true bindings on read. Delete the key from this config."
            )
        else:
            log.warning(
                "collection.persist_decoded_confidence is retired and ignored (P02.8), and "
                "this config's value %r is NOT honoured: decoded rows are no longer stored, "
                "and readers decode confirmed bindings plus those marked persist: true in "
                "bindings.yaml on read. Mark a binding persist: true to add it; delete the key.",
                value,
            )
        return data
