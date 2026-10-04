"""Configuration models for CANary.

Loads `canary.yaml` and validates it. Unknown keys raise rather than being
silently dropped, so typos in config don't become silent drift at runtime.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final, Literal, Self, get_args

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from src.lsm6dsox_settings import ImuSettings
from src.paths import ANALYSES_DIR, ANOMALIES_DIR, CONTROL_DIR, DATA_DIR

log = logging.getLogger(__name__)


# Broadcast CAN change-only storage (P02.10): the stale timeout's default and
# floor, and the re-write lead derived from it. Defined here, beside the
# ``collection.broadcast_stale_timeout_seconds`` field they validate (R09.2);
# ``src.storage.broadcast_storage`` states the rule that uses them.

#: The operator's default (2026-09-26).
DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS: Final = 10.0
#: A shorter timeout is refused: the re-write happens one second before the
#: timeout, so under two seconds an unchanged id would be re-written more
#: often than once a second and the filter would save nothing it can measure.
MIN_BROADCAST_STALE_TIMEOUT_SECONDS: Final = 2.0
#: How long before the timeout an unchanged id is re-written: derived from the
#: timeout, never configured. It covers ids whose frame period is under a
#: second (the re-write fires on the first frame at least ``timeout - 1`` s
#: after the last stored one, so the stored value is at most ``timeout - 1 +
#: period`` old). A slower id could lapse that way (a 7 s id at 9 + 7 s), so
#: :class:`~src.storage.broadcast_storage.CanChangeFilter` also keeps a frame
#: when the next one, one inter-arrival later, would find the stored value past
#: the timeout.
REWRITE_LEAD_SECONDS: Final = 1.0


def validate_broadcast_stale_timeout(seconds: float) -> float:
    """``seconds`` as a timeout, or ``ValueError`` naming why it cannot be one."""
    value = float(seconds)
    if not math.isfinite(value) or value < MIN_BROADCAST_STALE_TIMEOUT_SECONDS:
        raise ValueError(
            f"broadcast stale timeout {seconds!r} s is under the "
            f"{MIN_BROADCAST_STALE_TIMEOUT_SECONDS:g} s floor (P02.10, operator 2026-09-26): "
            f"an unchanged CAN id is re-written {REWRITE_LEAD_SECONDS:g} s before the "
            "timeout, so a shorter one would re-write it more than once a second"
        )
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


#: Default ``CollectionConfig.obd_priority_channels`` — the OBD channels that
#: keep their configured poll rate when the request plan oversubscribes the
#: bus. Lives here rather than in ``collection.obd_scheduler`` (which imports
#: it) only to keep the config default and the scheduler constant from
#: drifting apart. See the scheduler's ``DEFAULT_PRIORITY_CHANNELS`` re-export
#: for why this channel specifically.
DEFAULT_PRIORITY_CHANNELS: tuple[str, ...] = ("RPM", "THROTTLE_POS")


class VehicleParameters(_Strict):
    """Physical parameters needed for derived trip/fuel/performance channels
    (Task 6.6). All optional with defaults so existing ``canary.yaml`` files
    keep loading; ``VehicleConfig.model_dump()`` carries them into every
    session manifest, so a trip's calculations stay reproducible from the
    captured data alone.
    """

    #: Curb/test mass in kg — used for acceleration-derived power sanity.
    mass_kg: float = 1900.0
    #: How ``mass_kg`` was arrived at (a scale ticket, or an itemised estimate
    #: with its range), and what it includes. Occupants, cargo and fuel below
    #: full are *test conditions*, not vehicle facts: the dyno records them
    #: per run (H07).
    mass_evidence: str | None = None
    #: Mass of one road wheel with its tyre, kg: rotating mass, which the
    #: dyno's inertia term needs separately from the total (H07.6).
    wheel_tire_assembly_kg: float | None = Field(default=None, gt=0.0)
    #: The operator's usual load — driver and cargo — in kg. A default for a
    #: run's conditions; a run records what was actually aboard.
    usual_payload_kg: float | None = Field(default=None, ge=0.0)
    #: ``gasoline`` | ``diesel`` | ``e85`` — selects stoichiometric AFR, fuel
    #: density, and the CO2 emission factor.
    fuel_type: Literal["gasoline", "diesel", "e85"] = "gasoline"
    #: Engine displacement in litres (volumetric-efficiency fuel estimate).
    displacement_l: float = 4.0
    #: Volumetric efficiency (0–1.2) for the speed-density fallback when no
    #: MAF / fuel-rate PID is available.
    volumetric_efficiency: float = 0.85
    #: Driveline loss fraction (0–1) between crank and wheels for the
    #: estimated-power channel.
    drivetrain_loss_fraction: float = 0.15
    #: Brake thermal efficiency (0–1) for the fuel-energy power estimate.
    thermal_efficiency: float = 0.30
    #: Rolling tire diameter in metres (optional; speed cross-checks only).
    tire_diameter_m: float | None = None
    #: Driveline, as three separate unitless/SI facts (H07.1). They replace
    #: ``gear_ratios``, which was documented as gearbox × final drive but
    #: compared as rpm per km/h, with an example in the wrong units for a
    #: six-speed. Expected rpm per km/h is *computed* from these
    #: (:meth:`rpm_per_kph`), so the two readings cannot be confused again.
    #: Axle (final-drive) ratio.
    final_drive: float | None = Field(default=None, gt=0.0)
    #: Gearbox ratio of every forward gear, first gear first (e.g. 3.79 for
    #: first). All of them or none: a gear estimate against a partial table
    #: would name the wrong gear whenever the driver is in a missing one.
    gearbox_ratios: tuple[float, ...] = ()
    #: Loaded rolling circumference of the driven tyres, metres.
    tyre_loaded_circumference_m: float | None = Field(default=None, gt=0.0)
    #: Where the driveline numbers come from. Required once any is set.
    driveline_evidence: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _retired_gear_ratios(cls, data: Any) -> Any:
        if isinstance(data, dict) and "gear_ratios" in data:
            data = dict(data)
            if data.pop("gear_ratios"):
                raise ValueError(
                    "vehicle parameter gear_ratios is retired (it mixed up gearbox x final "
                    "drive with rpm per km/h): use final_drive, gearbox_ratios (one "
                    "unitless ratio per gear) and tyre_loaded_circumference_m"
                )
        return data

    @model_validator(mode="after")
    def _driveline_has_evidence(self) -> VehicleParameters:
        driveline = (self.final_drive, self.gearbox_ratios, self.tyre_loaded_circumference_m)
        if any(driveline) and not self.driveline_evidence:
            raise ValueError(
                "driveline facts need driveline_evidence saying how they were measured"
            )
        if any(ratio <= 0 for ratio in self.gearbox_ratios):
            raise ValueError("gearbox_ratios must be positive")
        return self

    def rpm_per_kph(self) -> tuple[float, ...]:
        """Engine rpm per km/h of true road speed in each gear, or ``()``
        unless the gearbox, final drive and tyre are all known."""
        if not (self.gearbox_ratios and self.final_drive and self.tyre_loaded_circumference_m):
            return ()
        wheel_rpm_per_kph = 1000.0 / 60.0 / self.tyre_loaded_circumference_m
        return tuple(g * self.final_drive * wheel_rpm_per_kph for g in self.gearbox_ratios)


class VehicleConfig(_Strict):
    make: str
    model: str
    year: int
    vin: str = ""
    parameters: VehicleParameters = VehicleParameters()


class CanBusConfig(_Strict):
    """How to construct the python-can `Bus`.

    `interface` is the python-can backend identifier:
      * `slcan`     — serial-attached adapter (Canable in slcan firmware, WSL dev)
      * `socketcan` — kernel SocketCAN netdev (Pi production)
      * `virtual`   — in-memory bus for tests and replay
    `channel` is the device path (`/dev/ttyACM0`) or netdev name (`can0`).
    `bitrate` is ignored by `virtual`.
    """

    interface: str
    channel: str
    bitrate: int


class GpsConfig(_Strict):
    """Serial NMEA GPS receiver for operating-mode detection.

    Optional and defaulted-disabled so configs without a GPS keep loading and
    behave exactly as before. When ``enabled``, the daemon runs a serial
    collector that parses NMEA 0183 (GGA/RMC/VTG) and emits ``gps:*`` telemetry
    records. ``gps:speed`` (km/h, matching ``obd:SPEED``) backs up the
    operating-mode segmenter's speed signal; no ``gps:`` channel is ever a
    model feature (H00 operator decision 1, enforced by ``src.trainability``).
    """

    enabled: bool = False
    #: Where fixes come from. ``serial`` reads NMEA off the receiver directly.
    #: ``gpsd`` reads gpsd's JSON socket instead, leaving the serial port to
    #: gpsd — which is what lets chrony discipline the clock from GPS with no
    #: network (see docs/time-sync.md). Both emit identical ``gps:*`` channels.
    #: Defaults to ``serial`` so existing configs and WSL dev are unaffected.
    source: Literal["serial", "gpsd"] = "serial"
    #: Serial device path (``source: serial`` only). Prefer the stable
    #: ``/dev/serial/by-id/...`` symlink over ``/dev/ttyUSB0``, which renumbers
    #: when USB devices re-enumerate.
    device: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    #: Serial read timeout (seconds). Also bounds how quickly the collector
    #: notices a stop request while waiting for the next NMEA line. Reused as
    #: the socket timeout under ``source: gpsd``.
    read_timeout_seconds: float = 2.0
    #: gpsd endpoint (``source: gpsd`` only). gpsd runs on the same host as the
    #: daemon; there is no reason to point this off-box.
    gpsd_host: str = "127.0.0.1"
    gpsd_port: int = 2947


class SensorCanConfig(_Strict):
    """A second, private CAN interface carrying add-on sensors (not the vehicle bus).

    Disabled by default: a config without the hardware keeps loading and
    behaves exactly as before. When enabled, the daemon listens on
    ``channel`` and decodes the frames catalogued in ``signals`` into
    ``sensor:*`` records. It never transmits. First use: an AEM X-Series
    fuel-pressure gauge (30-0301) — 500 kbit/s, 29-bit IDs, 20 Hz — ahead of
    the supercharger build (``docs/roadmap/supercharger-build.md``).

    The gauge has no internal termination: a bus of just the Pi and the gauge
    needs 120 ohm at each end (the Pi's HAT jumper, and one at the gauge).
    """

    enabled: bool = False
    interface: str = "socketcan"
    channel: str = "can1"
    bitrate: int = 500_000
    signals: Path = DATA_DIR / "sensor_can_signals.yaml"


class ImuConfig(_Strict):
    """3-axis IMU (accel + gyro) for motion-aware auto-tagging.

    Phase 2a/2b (``docs/roadmap/imu-gps-integration.md``). Optional and
    defaulted-disabled, same "configs without it keep loading and behave
    exactly as before" contract as ``GpsConfig``. Unlike ``GpsConfig`` there
    is no separate ``enabled`` bool: the doc's config sketch folds "no
    hardware" into the same ``source`` Literal that names which chip is
    wired, since which-chip and whether-any-chip are the same decision here.
    ``.enabled`` is provided as a property so call sites can gate the same
    way as ``gps.enabled`` without a Literal-vs-bool special case.
    """

    #: bno055 does gravity removal + orientation fusion on-chip (see
    #: ``src.preprocessing.imu_frame``'s module docstring); mpu6050/icm20948
    #: are raw accel+gyro requiring this codebase's own gravity low-pass.
    #: lsm6dsox is the CANary-HAT's IMU (raw accel+gyro, read through its FIFO
    #: by ``src.collection.imu_collector``) and the only chip with a collector.
    #: ``disabled`` (default) — no IMU wired, daemon startup skips it exactly
    #: like ``gps.enabled=False`` skips the GPS collector.
    source: Literal["bno055", "mpu6050", "icm20948", "lsm6dsox", "disabled"] = "disabled"
    i2c_bus: int = 1
    #: 7-bit I²C address (lsm6dsox only; the HAT straps SA0 high -> 0x6B).
    i2c_address: int = 0x6B
    #: Output data rate. For lsm6dsox it must be one of the chip's rates
    #: (12.5, 26, 52, 104, 208, 416, 833 Hz) — refused, not rounded. The
    #: recorded rate is a load decision, measured in H02.3.
    sample_rate_hz: float = 100.0
    #: Full-scale ranges (lsm6dsox only). ±8 g / ±2000 deg/s did not clip on
    #: a hand-shaken board (2.2 g, 244 deg/s peaks, 2026-09-23).
    accel_range_g: int = 8
    gyro_range_dps: int = 2000
    #: How often the collector drains the chip's FIFO. The FIFO holds ~2 s at
    #: 104 Hz, so this only trades latency against bus transactions.
    fifo_poll_seconds: float = Field(default=0.1, gt=0.0, le=1.0)
    #: ``auto`` resolves mount tilt dynamically via the gravity low-pass in
    #: ``imu_frame.py`` (the only correct option per the doc's physics
    #: section — a one-shot calibration is permanently biased by wherever
    #: the mount happened to sit at cal time). A fixed-quaternion string is
    #: accepted for a rigidly pre-surveyed mount but isn't consumed by any
    #: code yet. The BNO055 does this on-chip either way.
    mount_orientation: str = "auto"

    @property
    def enabled(self) -> bool:
        """Mirrors ``GpsConfig.enabled`` — ``False`` only when ``source == "disabled"``."""
        return self.source != "disabled"

    @model_validator(mode="after")
    def _chip_settings(self) -> ImuConfig:
        if self.source != "lsm6dsox":
            return self
        # Validate against the driver's own tables so the two cannot drift.
        try:
            ImuSettings(
                odr_hz=self.sample_rate_hz,
                accel_range_g=self.accel_range_g,
                gyro_range_dps=self.gyro_range_dps,
            )
        except ValueError as exc:
            raise ValueError(f"hardware.imu: {exc}") from exc
        return self


class SupplyVoltageConfig(_Strict):
    """The CANary-HAT's 12 V sense: an ADS1015 reading the protected ``+12V``
    rail through a resistor divider. Recorded as ``sensor:SUPPLY_VOLTAGE``.

    Unlike ``obd:CONTROL_MODULE_VOLTAGE`` it needs no awake ECM, so it reads
    key-off and parked. Disabled by default (no HAT, no ADC). An
    external-sensor channel: never trains or scores the anomaly detectors.
    """

    enabled: bool = False
    i2c_bus: int = 1
    i2c_address: int = 0x48
    #: ADS1015 input the divider feeds (single-ended). HAT rev 2.0: AIN0.
    input_channel: int = Field(default=0, ge=0, le=3)
    #: Rail volts per volt at the ADC pin. HAT rev 2.0: R11 100 k over R12
    #: 22 k, so (100 + 22) / 22. A per-board fact, kept here, not in code.
    divider_ratio: float = Field(default=122.0 / 22.0, gt=1.0)
    #: Conversions per second. The rail moves slowly except while cranking.
    rate_hz: float = Field(default=10.0, gt=0.0, le=200.0)


class UsbAdcConfig(_Strict):
    """A USB serial ADC module reading analog sensors (first: the AEM fuel gauge).

    The transport only; the channels and every calibration number are data
    (``sensors``). Disabled by default. Recorded as ``sensor:<name>``, an
    external-sensor channel: never trains or scores the anomaly detectors.
    """

    enabled: bool = False
    #: Name the module by its USB port (``/dev/serial/by-path/...``): a plain
    #: CH340 has no serial number, so ``by-id`` cannot tell two apart and
    #: ``/dev/ttyUSB*`` follows probe order.
    port: str = "/dev/ttyUSB0"
    baudrate: int = 115200
    #: One blocking read's limit; bounds shutdown latency.
    read_timeout_seconds: float = Field(default=1.0, gt=0.0, le=5.0)
    #: No parseable line for this long reopens the port. The stock firmware
    #: sends every 0.5 s, ``firmware/usb-adc`` every 10 ms.
    stall_seconds: float = Field(default=5.0, gt=0.0)
    sensors: Path = DATA_DIR / "usb_adc_sensors.yaml"


class KlineConfig(_Strict):
    """The CANary-HAT's K-line transceiver (TLIN1027 on uart0).

    ``enabled`` only says the hardware is there and may be used. When K-line
    is actually commanded is a capture profile's decision (its ``kline:``
    list); the vehicle's protocol facts are ``daemon.kline_profile``. The
    board overlay enables uart0 (``/dev/ttyAMA0``).
    """

    enabled: bool = False
    tty: str = "/dev/ttyAMA0"
    gpio_chip: str = "/dev/gpiochip0"
    #: TLIN1027 EN (HAT rev 2.0: GPIO26) and the commander pull-up FET gate
    #: (GPIO24), as line offsets on ``gpio_chip``.
    enable_gpio: int = 26
    pullup_gpio: int = 24
    #: Pause between polls of the defined set; 0 polls as fast as P3 allows.
    poll_interval_seconds: float = Field(default=0.0, ge=0.0, le=10.0)
    #: Listen whenever no capture profile asks for a session (pull-up off,
    #: never transmitting), decoding other testers — UpRev, a scan tool — and
    #: stepping CAN diagnostics aside while one is active (H06).
    sniff: bool = True
    #: How long after another tester's last frame the ECM session still
    #: counts as theirs. UpRev polls every ~0.1-0.3 s; its session times out
    #: after P3max (5 s).
    foreign_hold_seconds: float = Field(default=10.0, gt=0.0, le=300.0)


class HardwareConfig(_Strict):
    elm327_port: str
    can: CanBusConfig
    gps: GpsConfig = GpsConfig()
    imu: ImuConfig = ImuConfig()
    sensor_can: SensorCanConfig = SensorCanConfig()
    supply_voltage: SupplyVoltageConfig = SupplyVoltageConfig()
    usb_adc: UsbAdcConfig = UsbAdcConfig()
    kline: KlineConfig = KlineConfig()


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
    idle_states: tuple[str, ...] = ("engine_off", "no_data")
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
    #: time the first Mode 01 polls out into their 30 s miss backoff.
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


class OperatingModeSpec(_Strict):
    rpm_min: float | None = None
    rpm_max: float | None = None
    speed_min: float | None = None
    speed_max: float | None = None
    throttle_min: float | None = None
    throttle_max: float | None = None
    #: Coolant-temperature gates (°C). Lets the segmenter pull "cold engine
    #: warming up" out of the same RPM/speed band as warm steady-state idle —
    #: without this the behavior models fit one surface to both regimes and
    #: fire on every cold-start cycle. See ``warmup`` in
    #: ``canary.example.yaml`` for a representative threshold; the gating
    #: signal is ``obd:COOLANT_TEMP`` (degC).
    coolant_temp_min: float | None = None
    coolant_temp_max: float | None = None
    #: Recent-motion gates (km/h) on a derived ``recent_speed`` signal — the
    #: trailing max ground speed over ``idle_transient_lookback_seconds``. Lets
    #: the segmenter pull a *transient stop* (stopped now, but driving moments
    #: ago — a stop light) out of the same warm RPM/zero-speed band as a steady
    #: standing idle (driveway / pre-drive). Without it both collapse to one
    #: ``idle`` model that can't tell "stopped mid-drive" from "never moved".
    #: ``recent_speed`` is computed per session, so it never bleeds across
    #: sessions, and is ephemeral — it gates segmentation only, never a feature.
    recent_speed_min: float | None = None
    recent_speed_max: float | None = None
    #: Longitudinal/lateral accel gates (g) on the decimated
    #: ``imu:ACCEL_LON_MAX``/``imu:ACCEL_LAT_MAX`` channels (Phase 2a.3/2a.7,
    #: docs/roadmap/imu-gps-integration.md) — lets ``launch``/``hard_brake``/
    #: ``cornering`` modes gate on a transient IMU peak the same way other
    #: modes gate on RPM/speed/throttle. Inert (never required, never
    #: matched) until a real session actually has these columns — no IMU
    #: hardware exists yet, so every stock mode spec leaves these ``None``.
    accel_lon_min: float | None = None
    accel_lon_max: float | None = None
    accel_lat_min: float | None = None
    accel_lat_max: float | None = None


class PreprocessingConfig(_Strict):
    resample_hz: float
    rolling_window_seconds: int
    operating_modes: dict[str, OperatingModeSpec]
    #: Trailing window (s) for the segmenter's derived ``recent_speed`` signal
    #: (see ``OperatingModeSpec.recent_speed_*``). Sized to outlast a typical
    #: red light so a multi-minute stop stays ``idle_transient`` rather than
    #: decaying into standing ``idle``. The live ring buffer is grown to hold at
    #: least this many seconds so serve-time segmentation matches training.
    idle_transient_lookback_seconds: int = 180
    #: Skip per-mode segmentation entirely and fit/score a single model over a
    #: constant ``operating_mode = "all"``. Use when the corpus covers one
    #: regime, or when the OBD mode signals (RPM/SPEED/THROTTLE) are too sparse
    #: to segment reliably live — that sparsity otherwise produces train/serve
    #: skew where serving emits a mode the trainer never produced. See
    #: ``feature_pipeline.segment_or_constant``.
    single_operating_mode: bool = False
    #: Forward-fill ceiling (s) for slow channels (coolant/IAT/baro —
    #: ``resampler.DEFAULT_SLOW_CHANNEL_HINTS``) during resampling. Generous
    #: relative to fast channels' ``max_gap`` (interval × ``max_gap_factor``)
    #: because slow sensors legitimately update on the order of tens of
    #: seconds; this only catches a channel that's gone genuinely dead.
    #: Beyond the limit the resampled cell goes NaN and its ``__gap`` flag
    #: sets, same treatment fast channels already got.
    slow_channel_max_gap_seconds: float = 120.0
    #: A larger wall-clock discontinuity starts a new logical capture segment,
    #: even if both sides were accidentally written beneath one directory.
    #: Resampling across suspend/disconnect gaps manufactures thousands of
    #: all-stale rows and lets rolling features bridge unrelated drives.
    session_gap_seconds: float = Field(default=300.0, gt=0.0)
    #: Multiple of the resample interval a *fast* channel may go unobserved
    #: before its ffilled cell is NaN'd and its ``__gap`` flag sets.
    #:
    #: This is a claim about the achievable poll cadence, not the configured
    #: one. The default 2.0 (a 2 s ceiling at 1 Hz) assumes fast-tier PIDs
    #: really arrive at ~1 Hz; on a contended bus they do not — the 2026-07-26
    #: drive measured SPEED at 2.13 s and MAF at 4.46 s mean spacing against an
    #: 8 req/s budget carrying ~25 req/s of demand. Every cell past the ceiling
    #: becomes NaN, and because ``TrainedIsolationForest.score`` refuses any row
    #: with a NaN in any trained feature, that made the IF unable to score a
    #: single row (0 of 273 features reached 75% coverage; the Pi reported
    #: ``missing_feature_rate: 1.0``).
    #:
    #: Exposed as config (it was previously a hardcoded ``ResampleConfig``
    #: default) because it is preprocessing-parity-critical: it is recorded in
    #: the model manifest as ``max_gap_seconds`` and read by BOTH the offline
    #: resampler and ``LiveFeatureSource``, so train and serve move together.
    #: Changing it requires a retrain to take effect.
    #:
    #: The default stays at the historical 2.0 deliberately. Raising it was
    #: measured on 2026-07-26T164436 and does NOT rescue the IF: over 40 OBD
    #: channels, mean coverage went 11.7% (factor 2) → 20.9% (5) → 25.7% (10)
    #: → 30.0% (20), with **zero** channels reaching 50% at any setting. The
    #: shortfall is real poll cadence and Mode-01 suspension during DID
    #: windows, not an over-tight ceiling, so a looser ceiling only relabels
    #: stale data as fresh without making per-row scoring viable. Left tunable
    #: because it is a legitimate knob, not because raising it is the fix.
    max_gap_factor: float = 2.0
    #: Phase 5.2 — consecutive rows a candidate operating-mode label must hold
    #: before segmentation actually switches to it. ``1`` (default) is the
    #: legacy immediate-switch behavior. Without dwell, 1 Hz flapping at a
    #: mode boundary (e.g. city/highway right at the speed threshold)
    #: contaminates both models' training rows with a handful of mislabeled
    #: ticks each way. Mirrors ``AlertsConfig.min_consecutive_ticks``'s shape;
    #: see ``segmenter._apply_mode_dwell``.
    mode_dwell_ticks: int = Field(default=1, ge=1)


class IsolationForestConfig(_Strict):
    enabled: bool
    n_estimators: int
    contamination: float
    per_mode_contamination: dict[str, float] = Field(default_factory=dict)
    random_state: int = 42
    #: Phase 7.2 — fraction of each mode's clean training rows, time-ordered
    #: from the tail, held out (not fit on) and used purely to calibrate the
    #: per-mode score threshold. ``0`` disables calibration (legacy
    #: contamination-label decision only).
    calibration_holdout_fraction: float = Field(default=0.2, ge=0.0, lt=1.0)
    #: Target fraction of *nominal* held-out ticks the per-mode threshold is
    #: allowed to flag — the calibrated decision boundary is the
    #: ``1 - target_flag_rate`` quantile of held-out scores. This replaces the
    #: assumption that exactly ``contamination`` of nominal driving is anomalous.
    target_flag_rate: float = Field(default=0.001, gt=0.0, le=0.5)
    #: Task 2.2 — a mode with fewer than this many *fit* rows (post-holdout
    #: split) is skipped entirely rather than fitting a full forest on a
    #: handful of rows: no model persisted, listed in the manifest's
    #: ``skipped_modes`` instead.
    min_samples_per_mode: int = Field(default=100, ge=0)
    #: Parallel tree builders. A small fixed default avoids both the historical
    #: single-core fit and ``-1`` oversubscription on memory-constrained WSL.
    n_jobs: int = 4
    max_dropped_row_fraction: float = Field(default=0.75, ge=0.0, lt=1.0)
    allow_unknown_only: bool = False
    #: A04.4 — a mode model resting on fewer independent physical drives than
    #: this (training rows) is refused, and so is one whose threshold would
    #: rest on fewer calibration drives (``retrain.py --accept-thin-calibration
    #: REASON`` publishes it anyway and stamps the reason). The 2026-09-20
    #: city/highway models came from one day; idle/warmup thresholds from one
    #: session.
    min_training_drives_per_mode: int = Field(default=3, ge=0)
    min_calibration_drives_per_mode: int = Field(default=3, ge=0)
    #: A04.10 — extra seeds refit per mode to measure which calibration rows
    #: the threshold flags seed-to-seed (pairwise Jaccard, in the manifest).
    #: 0 disables the measurement.
    stability_seeds: int = Field(default=4, ge=0)

    @field_validator("n_jobs")
    @classmethod
    def _nonzero_n_jobs(cls, value: int) -> int:
        if value == 0:
            raise ValueError("n_jobs must not be zero")
        return value


class AlertsConfig(_Strict):
    """Emission policy shared by every detector (Phase 7.5; M-of-N 6.1).

    All defaulted so configs predating it keep loading.
    """

    #: A detector (IF / sequence / each behavior surface) must be over its
    #: threshold for at least this many ticks WITHIN the trailing
    #: ``persistence_window_ticks`` (M-of-N, 6.1) before it emits an event or
    #: counts as a firing fusion source. Real faults persist; a lone tick is
    #: the historical flood. 1 = legacy fire-on-first-tick behavior. Named
    #: ``min_consecutive_ticks`` from its pre-6.1 strict-N-consecutive
    #: shape; kept for config compatibility even though it's now the M in
    #: M-of-N (``persistence_window_ticks`` defaults to this same value,
    #: which reproduces the old strict-consecutive gate exactly).
    min_consecutive_ticks: int = Field(default=3, ge=1)
    #: N — the trailing window (in ticks) the M hits above are counted
    #: within. Must be >= min_consecutive_ticks. Default 5 against the
    #: default M=3 gives the roadmap's "3-of-5" shape out of the box —
    #: tolerates one fire-gap-fire flicker per window without weakening the
    #: 3 hits still required (flood protection unchanged). ``None`` falls
    #: back to ``min_consecutive_ticks`` itself (strict-consecutive, the
    #: exact pre-6.1 gate) for anyone who explicitly wants that shape back.
    persistence_window_ticks: int | None = Field(default=5, ge=1)
    #: T — wall-clock bound (seconds) on the same window: an entry older
    #: than this many seconds from "now" is evicted regardless of tick
    #: count, so a long data/replay gap can't let stale hits from before the
    #: gap count toward a streak once ticks resume. ``None`` disables the
    #: time bound (tick-count-only, the pre-6.1 shape).
    persistence_window_seconds: float | None = Field(default=8.0, gt=0.0)
    #: Per-(source, model) refractory window between emitted events. The old
    #: 1.0 s default was a no-op at the 1 Hz tick — a sustained deviation
    #: emitted ~3600 events/hour per source.
    min_seconds_between_events: float = Field(default=30.0, ge=0.0)

    @model_validator(mode="after")
    def _check_window(self) -> AlertsConfig:
        if (
            self.persistence_window_ticks is not None
            and self.persistence_window_ticks < self.min_consecutive_ticks
        ):
            raise ValueError(
                "persistence_window_ticks must be >= min_consecutive_ticks "
                f"({self.persistence_window_ticks} < {self.min_consecutive_ticks})"
            )
        return self


class FusionConfig(_Strict):
    """Phase 7.2 — per-tick fusion of IF + LSTM + behavior residuals into one
    agreement-aware calibrated severity. All defaulted so configs predating
    7.2 keep loading with fusion on.
    """

    enabled: bool = True
    #: Fused severity in [0, 1] at/above which a fused ``AnomalyEvent`` fires.
    severity_threshold: float = Field(default=0.5, gt=0.0, le=1.0)
    #: Added to the fused severity per *additional* agreeing source beyond the
    #: first (agreement-aware boost), before clamping to 1.0.
    agreement_boost: float = Field(default=0.25, ge=0.0, le=1.0)
    #: 6.2 (operator-approved decision, see OVERNIGHT-QUEUE.md "Fusion
    #: semantics"): the fused event is a real agreement gate, not
    #: OR-with-boost — ``is_anomaly`` requires at least this many
    #: *independent* firing sources (sibling surfaces sharing a ``group``
    #: still count as one), on top of ``severity_threshold``. A single
    #: source at any severity never sets the fused verdict alone; it still
    #: emits its own per-source event through the unrelated per-source path.
    min_agreement: int = Field(default=2, ge=1)
    #: Normalized severity a "warning" / "critical" behavior-deviation band
    #: contributes as a fusion source.
    behavior_warning_severity: float = Field(default=0.5, ge=0.0, le=1.0)
    behavior_critical_severity: float = Field(default=1.0, ge=0.0, le=1.0)


#: A06.5 — what a detector's events are allowed to do. ``alert``: the
#: historical behaviour (stored, shown as an active anomaly, eligible for an
#: ntfy push, and able to carry a fused event). ``evidence``: still scored and
#: stored, flagged ``role: evidence`` on the event record, and still counted
#: toward fusion agreement — but never pushed, never shown as an active
#: alert, and never the reason a fused event fires.
DetectorRole = Literal["alert", "evidence"]


class DetectorRolesConfig(_Strict):
    """A06.5 — per-detector role, the mechanism behind A06.1/A06.2.

    **Decided by the operator 2026-09-23** (A06.1, A06.2): both detectors
    are ``evidence``. The Isolation Forest scores and stores but never alerts
    on its own (A05's plausibility checks own gross single-channel faults);
    the sequence detector is a non-alerting novelty screen. The primary
    detector is the behavior models plus the deterministic layer (A06.4).
    Set a role to ``alert`` to restore the old behaviour for one detector.

    Enforced in two shared places, not by each detector: the
    ``AnomalyEventEmitter`` stamps the role on every event it stores and
    withholds pushes for ``evidence``, and ``FusionScorer`` lets an
    ``evidence`` source vote in agreement but requires an ``alert`` source
    before the fused verdict can fire. Behavior residuals and the
    deterministic sources (DTC, misfire, trend) are always ``alert``.
    """

    isolation_forest: DetectorRole = "evidence"
    #: The single sequence-detector slot, whichever backbone
    #: (``sequence_detector: lstm | tcn``) fills it — both publish the
    #: ``lstm_autoencoder`` event source.
    sequence: DetectorRole = "evidence"

    #: H04.2 sensor committees (``source: committee``). ``evidence`` until a
    #: committee has a confirmed real catch — the same bar the 2026-09-23
    #: decision set for the learned detectors; set ``alert`` to promote.
    #: Deterministic, so no artifact or alert-budget verdict applies: it is
    #: *not* in :meth:`by_source` (the artifact role gate would demote an
    #: ``alert`` with "no artifact loaded") but in :meth:`deterministic_by_source`.
    committee: DetectorRole = "evidence"

    def by_source(self) -> dict[str, DetectorRole]:
        """Role keyed by the ``AnomalyEvent.source`` / fusion-source name the
        detector publishes under — the only form the emitter and fusion need."""
        return {"isolation_forest": self.isolation_forest, "lstm_autoencoder": self.sequence}

    def deterministic_by_source(self) -> dict[str, DetectorRole]:
        """Roles of sources with no model artifact, applied as configured
        (never through the artifact role gate). Emitter-only: none of these is
        a fusion source."""
        return {"committee": self.committee}


class _SequenceTrainingConfig(_Strict):
    """What both sequence-detector backbones train, calibrate and score with.

    LSTM and TCN carried 17 identical copies of these fields (R03.10), so a
    new training knob could reach one backbone and not the other. Defaults
    are the TCN's; :class:`LSTMAutoencoderConfig` redeclares ``enabled``,
    ``sequence_length``, ``latent_dim`` and ``threshold_k`` as required.
    """

    enabled: bool = False
    sequence_length: int = 30
    latent_dim: int = 32
    dropout: float = 0.0
    batch_size: int = 64
    #: Overlapping training windows are highly redundant. Calibration remains
    #: dense by default so deployed thresholds retain one-tick resolution.
    training_stride: int = Field(default=5, ge=1)
    calibration_stride: int = Field(default=1, ge=1)
    #: ``None`` chooses a conservative platform default (zero on WSL). Worker
    #: processes can duplicate the full pandas-backed dataset.
    dataloader_workers: int | None = Field(default=None, ge=0)
    mixed_precision: bool = True
    compile_model: bool = False
    min_window_observed_fraction: float = Field(default=0.01, gt=0.0, le=1.0)
    min_usable_window_fraction: float = Field(default=0.9, gt=0.0, le=1.0)
    learning_rate: float = 0.001
    max_epochs: int = 100
    validation_fraction: float = 0.2
    early_stopping_patience: int = 10
    min_delta: float = 0.0
    random_state: int = 42
    threshold_k: float = 3.0
    #: When set (0–1), calibrate thresholds as this quantile of the
    #: reconstruction-error distribution instead of ``mean + k·σ``.
    #: Reconstruction errors are right-skewed and stride-1 windows heavily
    #: autocorrelated, so σ is a poor tail proxy; a quantile targets the
    #: alert budget directly (like the IF's ``target_flag_rate``). Task 3.2:
    #: defaults to 0.999 (the calibrated-FPR path) rather than ``None`` — the
    #: old ``None`` default silently meant ``mean + k·σ`` was every deploy's
    #: real threshold. Set explicitly to ``null`` in config to opt back into
    #: k·σ.
    threshold_quantile: float | None = Field(default=0.999, gt=0.0, lt=1.0)
    #: Feature columns to exclude from anomaly scoring. Each entry matches
    #: itself plus its rolling-derivative siblings (``__mean``, ``__std``,
    #: ``__roc``), so listing ``obd:FUEL_LEVEL`` silences all four. Applied
    #: at *train* time (excluded from ``feature_columns`` in the manifest,
    #: so the model never learns them) **and** at *serve* time (the live
    #: window's mask is forced to 0 at these indices, so a stale artifact
    #: that still includes them can't push the global score). Use for
    #: behavior signals — fuel level, HVAC blower, module voltage — that
    #: drift naturally over a drive without being a health signal.
    suppress_channels: tuple[str, ...] = ()


class LSTMAutoencoderConfig(_SequenceTrainingConfig):
    enabled: bool
    sequence_length: int
    latent_dim: int
    threshold_k: float
    hidden_size: int = 64
    num_layers: int = 1


class TCNAutoencoderConfig(_SequenceTrainingConfig):
    """Dilated causal TCN autoencoder — the alternate sequence-detector backbone.

    Fully defaulted (and ``enabled: false`` by default) so configs without a
    ``tcn_autoencoder`` block keep loading; selecting it requires both
    ``sequence_detector: tcn`` and ``enabled: true``.
    """

    # The training fields are ``_SequenceTrainingConfig``'s, shared with the
    # LSTM (R03.10). Not in the docstring: that is the JSON-schema description.
    channels: int = 64
    kernel_size: int = 3
    num_levels: int = 4
    #: Feed a bounded per-channel Δt decay as extra conv input channels (the
    #: GRU-D freshness gate). When false the TCN sees value+mask only.
    use_delta_decay: bool = True


class ModelsConfig(_Strict):
    isolation_forest: IsolationForestConfig
    lstm_autoencoder: LSTMAutoencoderConfig
    tcn_autoencoder: TCNAutoencoderConfig = Field(default_factory=TCNAutoencoderConfig)
    fusion: FusionConfig = Field(default_factory=FusionConfig)
    #: A06.5 — ``alert`` (default) or ``evidence`` per detector. The choice
    #: is the operator's (A06.1/A06.2); see :class:`DetectorRolesConfig`.
    roles: DetectorRolesConfig = Field(default_factory=DetectorRolesConfig)
    #: Which backbone fills the single sequence-detector slot. The selected
    #: backend's own ``enabled`` flag still gates whether it loads (so an
    #: existing ``lstm_autoencoder.enabled: false`` keeps the slot off, and
    #: ``tcn_autoencoder`` defaults to disabled). See
    #: ``src/models/sequence_registry.py``. 9.1 (operator-approved decision,
    #: see ``OVERNIGHT-QUEUE.md``): GRU-D LSTM is the shipped default — its
    #: GRU decoder reconstructs step-by-step from the latent, materially
    #: stronger temporal-shape modeling than the TCN decoder's single
    #: broadcast-latent reconstruction. TCN is documented as an NPU-export
    #: fallback, not an equal; no further bake-off gates this default.
    sequence_detector: Literal["lstm", "tcn"] = "lstm"
    #: Score IF + sequence in a child process rather than a worker thread.
    #: C08 moved scoring off the event loop into a thread, which is not the
    #: same as off the CPU: measured on the Pi 2026-09-09, the daemon missed
    #: 48 % of its 1 Hz ticks with 2.5 of 4 cores idle, because one Python
    #: process is GIL-bound to roughly one core and a slow forward pass
    #: starved the collector thread sharing it — collection stopped for ~100 s
    #: at a stretch.
    #:
    #: **Off by default, deliberately.** The child rebuilds its models from
    #: *this config's* artifact paths, so it only sees models that came from
    #: disk — a parent holding an injected or in-memory model (tests, and any
    #: caller that populates ``model_managers`` itself) would have the child
    #: score against something else, or nothing. Deployments where the models
    #: do come from disk opt in; nothing else changes behaviour. A host where
    #: the child cannot start falls back to in-process scoring, so this is a
    #: performance knob, never a correctness one.
    out_of_process_scoring: bool = False
    #: Build the live rolling feature row in one persistent spawned process.
    #: Unlike model scoring, this worker needs no artifact paths and is safe
    #: for every deployment: each request carries the exact config and
    #: behavior-model snapshot for that tick. Enabled by default so pandas'
    #: Python/GIL-heavy transform cannot starve collection or HTTP on the Pi.
    #: Set false for deterministic single-process debugging and unit fixtures.
    out_of_process_feature_build: bool = True


class DashboardConfig(_Strict):
    host: str
    port: int


class DaemonConfig(_Strict):
    """Where the long-running daemon reads / writes its sidecar artifacts.

    All fields are optional so existing ``canary.yaml`` files keep loading
    without edits; the defaults are sensible relative-to-cwd paths for the
    common WSL-dev case. Production deployments override them (the systemd
    unit will pin absolute paths under ``/var/lib/canary``).
    """

    anomalies_dir: Path = ANOMALIES_DIR
    models_dir: Path = Path("models")
    jobs_dir: Path = CONTROL_DIR / "jobs"
    audit_log: Path = CONTROL_DIR / "audit.jsonl"
    feedback_log: Path = CONTROL_DIR / "anomaly_feedback.jsonl"
    #: Vehicle maintenance / intervention ledger (Task 3.1,
    #: eval-longitudinal-and-dtc.md) — what physically happened to the
    #: truck, append-only, profile-scoped like the sibling paths below.
    vehicle_ledger: Path = CONTROL_DIR / "vehicle_ledger.jsonl"
    #: Frozen reference epoch for fingerprint drift trending (Task 4.3,
    #: eval-longitudinal-and-dtc.md) — per-stat mean/std/quantiles over a
    #: hand-picked set of healthy sessions. Written only by
    #: ``scripts/fingerprint.py freeze``; never auto-updated. Profile-scoped
    #: like the sibling ledger paths.
    reference_epoch: Path = CONTROL_DIR / "reference_epoch.json"
    training_ledger: Path = CONTROL_DIR / "training_runs.jsonl"
    tuning_config: Path = CONTROL_DIR / "tuning.yaml"
    tuning_audit: Path = CONTROL_DIR / "tuning_audit.jsonl"
    #: Persisted GPS trip odometer (cumulative distance + moving time). Survives
    #: daemon restarts so the System page shows lifetime "miles / time driven
    #: with the unit plugged in" rather than per-process totals.
    trip_state: Path = CONTROL_DIR / "trip_odometer.json"
    #: Task 4.6 idempotency marker — ``{stat: last_alerted_session_id}``. CUSUM
    #: state itself is recomputed fresh from the full fingerprint history on
    #: every evaluation (Task 4.4's design note: cheap at per-session
    #: frequency, avoids incremental-state staleness/corruption); this tiny
    #: file is the ONLY thing persisted, purely so a re-evaluation of the same
    #: already-alerted session never double-emits. Profile-scoped like the
    #: sibling ledger paths.
    trend_alert_state: Path = CONTROL_DIR / "trend_alert_state.json"
    #: Behavior-model surfaces (hand-edited / refit-written). A config field
    #: rather than a hardcoded path so it can be scoped per vehicle profile
    #: (see ``src/vehicle/profiles.py``). Rooted at the checkout like the sibling
    #: paths (R01.2), so a tool started from another directory reads the same
    #: file the daemon does.
    behavior_models: Path = DATA_DIR / "behavior_models.yaml"
    #: Directional, provenanced accepted-fault overrides (Task 1.3,
    #: online-training-strategy.md) — sibling to ``behavior_models`` above,
    #: same profile-scoping rationale (a second vehicle must never inherit
    #: this one's overrides). Optional: absent file ⇒ zero overrides
    #: (``load_accepted_baselines`` handles that gracefully).
    accepted_baselines: Path = DATA_DIR / "accepted_baselines.yaml"
    #: User-defined custom-PID formulas (Task 6.8, wired in by Task 2.6) —
    #: schema documented in ``custom_pids.example.yaml``. Optional: absent
    #: file ⇒ zero custom PIDs polled (``load_custom_pids`` handles that
    #: gracefully), same posture as ``behavior_models`` above.
    custom_pids: Path = DATA_DIR / "custom_pids.yaml"
    #: Operator-selectable collection modes, scoped per vehicle.
    capture_profiles: Path = DATA_DIR / "capture_profiles.yaml"
    #: Per-vehicle, per-tune AFR voltage transfer and target-table exports.
    afr_calibration: Path = DATA_DIR / "afr_calibration.yaml"
    #: Per-vehicle K-line facts (addresses, wake-up, framing, timing) for the
    #: CANary-HAT's native K-line path; absent means K-line is not configured.
    kline_profile: Path = DATA_DIR / "kline.yaml"
    #: Session catalog (Task 6.1, eval-longitudinal-and-dtc.md) — SQLite index
    #: over session sidecars. Rebuildable from disk at any time
    #: (``scripts/catalog.py rebuild``); never a second source of truth.
    #: Profile-scoped like the sibling ledger paths.
    catalog_db: Path = CONTROL_DIR / "catalog.db"
    #: Operational calibration state written from the dashboard: the flash
    #: registry ``append_flash`` extends, the verified RAM catalog and the last
    #: memory-verify outcomes. Left unset in YAML it resolves beside
    #: ``jobs_dir`` (``src.vehicle.profiles.operational_dir``), so a deployment that
    #: pins ``jobs_dir`` under ``/data/canary`` keeps this there too; the
    #: default below is where that lands for the default ``jobs_dir``.
    #: ``scope_config`` always sets it, per vehicle (R05.15: it used to be one
    #: directory inside ``jobs/`` shared by every profile).
    calibration_dir: Path = CONTROL_DIR / "calibration"
    #: Capture profiles saved or deleted on this machine, layered over the
    #: tracked ``capture_profiles`` catalog by name (S03.4). Operational state:
    #: the tracked catalog is never written at runtime.
    capture_profile_overrides: Path = CONTROL_DIR / "capture_profile_overrides.yaml"
    #: Root for everything the daemon writes at runtime (S03). When set, every
    #: path in :data:`DAEMON_STATE_LAYOUT` that the YAML does not set itself
    #: resolves under it, so nothing the Pi writes lands in the git checkout
    #: (where ``canary-update`` refuses tracked edits). ``None`` keeps the
    #: checkout-rooted defaults the laptop uses. The Pi sets ``/data/canary``.
    state_dir: Path | None = None
    #: Ceiling on the stored rows a whole-session *raw* read in the digest
    #: child may attempt (D03.6): a capture's tuning section (``read_channels``
    #: with no row filter) and the workbench jobs. Since P02.1 the rest of the
    #: digest reads part by part and is bounded by
    #: ``digest_span_budget_seconds`` instead. A session over the budget gets a
    #: ``digest_skipped.json`` naming the count and the budget instead of an
    #: attempt; ``0`` disables the check.
    #:
    #: ``None`` (the default) **derives** the budget from the child's
    #: address-space ceiling and the measured per-row peak, rather than
    #: stating a second number that can drift out of step with the first. A
    #: hand-picked 4 M sat against a 4 GiB ceiling that only covered ~2 M rows,
    #: so sessions in between passed the budget, spawned a child and died of
    #: ``MemoryError`` — safe, but it reported a memory anomaly where the
    #: honest answer was "this is simply larger than what fits".
    digest_row_budget: int | None = Field(default=None, ge=0)
    #: Ceiling, in seconds of recorded span (Parquet footer timestamps), on
    #: the session a digest or a ``classify`` job may attempt (P02.1). Their
    #: ``value_number`` chain holds one part of raw rows at a time plus the
    #: session's reduced cells and 1 Hz frames, so its peak follows the span,
    #: not the rows. ``None`` derives it from the child's address-space ceiling
    #: (``src.analysis.digest.digest_span_budget_seconds``: ~13.7 h for a
    #: 4 GiB child); ``0`` disables the check (the ceiling still applies).
    digest_span_budget_seconds: float | None = Field(default=None, ge=0)
    #: Hard ``RLIMIT_AS`` ceiling, in MiB, applied inside the digest child
    #: process before it touches any data. This is the backstop that holds
    #: even if the row budget is raised or the memory cost regresses: an
    #: over-budget digest then raises ``MemoryError`` *in the child*, which
    #: the parent reports, instead of the kernel picking an OOM victim (twice
    #: that victim was the daemon). ``None`` derives it from physical RAM —
    #: half of total, clamped to [1 GiB, 4 GiB] — which on the 8 GB Pi gives
    #: a 4 GiB child that still fits, beside the daemon's normal 150-650 MB,
    #: inside the unit's ``MemoryMax=6G``; the rlimit is the *catchable* half
    #: of that pair, since a cgroup kill is not an exception anyone can name.
    digest_address_space_mb: int | None = Field(default=None, gt=0)
    isolation_forest_artifact: str = "isolation_forest/model.joblib"
    lstm_artifact: str = "lstm_autoencoder/model.pt"
    tcn_artifact: str = "tcn_autoencoder/model.pt"
    model_poll_seconds: float = 30.0
    retention_sweep_seconds: float = 3600.0
    broadcaster_queue_size: int = 100
    control_token_env: str = "CANARY_CONTROL_TOKEN"

    @model_validator(mode="after")
    def _root_state_paths(self) -> Self:
        """Re-root every unset runtime path under ``state_dir`` (S03.1)."""
        if self.state_dir is None:
            return self
        for name, relative in DAEMON_STATE_LAYOUT.items():
            if name not in self.model_fields_set:
                setattr(self, name, self.state_dir / relative)
        return self


#: Where each runtime-written :class:`DaemonConfig` path goes under
#: ``state_dir`` (S03.1), before ``scope_config`` nests it per vehicle.
#: Trained artifacts sit with the models (operator decision 2026-10-04: they
#: are Pi-owned), control-plane state under ``control/``.
DAEMON_STATE_LAYOUT: dict[str, Path] = {
    "anomalies_dir": Path("anomalies"),
    "models_dir": Path("models"),
    "behavior_models": Path("models/behavior_models.yaml"),
    "jobs_dir": Path("control/jobs"),
    "audit_log": Path("control/audit.jsonl"),
    "feedback_log": Path("control/anomaly_feedback.jsonl"),
    "vehicle_ledger": Path("control/vehicle_ledger.jsonl"),
    "reference_epoch": Path("control/reference_epoch.json"),
    "training_ledger": Path("control/training_runs.jsonl"),
    "tuning_config": Path("control/tuning.yaml"),
    "tuning_audit": Path("control/tuning_audit.jsonl"),
    "trip_state": Path("control/trip_odometer.json"),
    "trend_alert_state": Path("control/trend_alert_state.json"),
    "catalog_db": Path("control/catalog.db"),
    "custom_pids": Path("control/custom_pids.yaml"),
    "capture_profile_overrides": Path("control/capture_profile_overrides.yaml"),
}
#: :class:`DaemonConfig` paths that name version-controlled data the daemon
#: only reads. Every ``Path`` field is in exactly one of these two tables or
#: is ``state_dir``/``calibration_dir`` (``tests/test_state_dir.py``), so a new
#: field cannot default into the checkout unclassified.
DAEMON_TRACKED_PATHS: frozenset[str] = frozenset(
    {"accepted_baselines", "afr_calibration", "capture_profiles", "kline_profile"}
)


class TrendsConfig(_Strict):
    """Task 4.4 trend-test parameters (``src.analysis.trends``). All defaulted
    to the textbook values so configs predating this task keep loading."""

    #: Tabular-CUSUM slack ``k``, in reference-std units — half the shift
    #: size worth detecting.
    cusum_k: float = Field(default=0.5, ge=0.0)
    #: Tabular-CUSUM decision interval ``h``, in reference-std units — the
    #: alarm threshold. 4-5 sigma is the usual textbook range.
    cusum_h: float = Field(default=5.0, gt=0.0)
    #: Mann-Kendall two-tailed significance level.
    mann_kendall_alpha: float = Field(default=0.05, gt=0.0, lt=1.0)


class NotificationsConfig(_Strict):
    """Push notifications via ntfy (Phase 5, eval-longitudinal-and-dtc.md).

    Ships disabled with no real topic baked in (operator-approved decision,
    see OVERNIGHT-QUEUE.md Q12) — a made-up ``topic`` would either silently
    no-op against ntfy.sh or, worse, land on a stranger's public topic.
    Turning this on for the operator's actual ntfy instance is an explicit
    morning task, not something to flip during unattended work. All fields
    default so configs predating this section keep loading with
    notifications off.
    """

    enabled: bool = False
    #: Base ntfy server URL (self-hosted or the public ``ntfy.sh``).
    ntfy_url: str = "https://ntfy.sh"
    #: ntfy topic to POST to. Blank until the operator picks a real one.
    topic: str = ""
    #: Minimum ``AnomalyEvent.score`` (same larger-is-worse scale each source
    #: already uses) that reaches a push (5.3's critical-alert hook).
    min_severity: float = Field(default=0.8, ge=0.0)
    #: Hard cap on pushes per recording session — a flapping detector must
    #: never turn into a phone full of notifications.
    max_pushes_per_session: int = Field(default=3, ge=0)
    #: End-of-drive digest push (5.5) — independent toggle from the
    #: per-event critical-alert hook above.
    digest_enabled: bool = False


class OnlineTrainingConfig(_Strict):
    """Config-gated scheduled retrain (Task 3.2, online-training-strategy.md).

    **Ships disabled** — a hard pre-made decision (queue Round-2): enabling
    unattended scheduled retrains against the real corpus is an explicit
    operator action, never an unattended default. When enabled, the daemon's
    ``run_online_training_scheduler`` background task (mirrors
    ``run_retention_sweeper``'s asyncio-task-on-a-cadence shape) submits a
    ``retrain_all`` job through the existing ``JobController`` every
    ``interval_seconds`` — the same subprocess-isolated mechanism the
    dashboard's manual "Retrain" button already uses, so a scheduled retrain
    never runs on the daemon's own event loop or blocks real-time capture.
    The job is windowed to the trailing ``window_days`` of sessions
    (``--since``, Task 3.1) — a genuine sliding window, not a fixed corpus —
    and the scheduler never adds ``--no-calibration-holdout``: a window too
    small to spare a calibration holdout is refused by ``retrain.py`` itself
    (Task 3.1) and logged as a skip, never degraded to in-sample calibration.
    """

    enabled: bool = False
    #: Seconds between scheduled-retrain attempts.
    interval_seconds: float = Field(default=86400.0, gt=0.0)
    #: Rolling-window length in days passed to ``retrain.py --since``.
    window_days: int = Field(default=30, gt=0)
    #: How often the scheduler re-checks a submitted job's status while
    #: waiting for it to finish (a cheap in-memory dict lookup — this is not
    #: additional training, just how promptly the loop notices completion).
    poll_interval_seconds: float = Field(default=5.0, gt=0.0)


class NonvolatileWriteConfig(_Strict):
    """Interlocks for the future ECU-programming path (W07).

    These defaults intentionally refuse uncertain or marginal supply/state
    conditions. No programming transport exists yet; the config is consumed
    by the mandatory authorization gate that must precede one.
    """

    authorization_ttl_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
    max_signal_age_seconds: float = Field(default=5.0, gt=0.0, le=10.0)
    stationary_speed_max_kph: float = Field(default=0.5, ge=0.0, le=2.0)
    min_battery_voltage: float = Field(default=12.0, gt=0.0)
    min_ups_soc_percent: float = Field(default=40.0, ge=0.0, le=100.0)
    ups_runtime_margin_seconds: float = Field(default=120.0, ge=0.0)


class TuningReadinessConfig(_Strict):
    """Floors for the ``/status.tuning_readiness`` chips (W02.4).

    Status only: nothing refuses an action on these. The flash interlocks
    keep their own floors in ``nonvolatile_write``.
    """

    min_ups_soc_percent: float = Field(default=40.0, ge=0.0, le=100.0)


class ProfilesConfig(_Strict):
    """Multi-vehicle profile resolution (see ``src/vehicle/profiles.py``).

    When the Pi is plugged into a vehicle, the daemon reads the VIN
    (OBD-II Mode 09 PID 0x02) at startup and resolves it to a profile slug,
    then scopes every vehicle-specific path under ``<base>/<slug>/`` so two
    vehicles never share a training corpus or model set. All fields default so
    configs predating profiles keep loading; with the probe disabled or no VIN
    available the ``vehicle:`` block is used as the fallback/default profile.
    """

    enabled: bool = True
    #: VIN → profile registry (a daemon-managed *data* file, not canary.yaml).
    #: Not scoped per vehicle — it is the index that maps VIN to slug.
    registry_path: Path = Path("profiles.yaml")
    #: What this machine learned about its vehicles (display names, VINs seen,
    #: calibration ID / CVN), merged over ``registry_path`` by slug (S03.2).
    #: The tracked registry holds the laptop-authored vehicle parameters and is
    #: never written at runtime. Under ``daemon.state_dir`` when that is set.
    registry_overlay_path: Path = CONTROL_DIR / "profiles.yaml"
    #: Persist a freshly-seen VIN to the registry automatically (with a blank
    #: display name for the operator to fill in via the dashboard).
    auto_register: bool = True
    #: How long the startup VIN probe waits for the ECU's Mode 09 response.
    vin_probe_timeout_seconds: float = 2.0


class AutoTagConfig(_Strict):
    """Thresholds for the motion-aware auto-tagger (Phase 2a.5/2a.6,
    docs/roadmap/imu-gps-integration.md). One taxonomy, one config block:
    both the offline pass (``scripts/autotag.py``) and the live daemon hook
    evaluate the exact same thresholds, so tuning them (as real drives
    accumulate) never needs a redeploy — same philosophy as the behavior
    models' deviation bands.

    ``live_enabled`` is a **hard pre-made decision** (Round-2
    OVERNIGHT-QUEUE): ships ``False`` until Phase 2b hardware validation,
    independent of whether the offline pass is useful today (it is, for the
    GPS-only ``stopped``/``highway_cruise`` tags against existing sessions).
    """

    live_enabled: bool = False
    #: g — decimated ``imu:ACCEL_LON_MAX`` threshold, disambiguated into
    #: accel vs. brake by ``d(obd:SPEED)/dt``'s sign (IMU + OBD, no GPS).
    hard_accel_g: float = 0.35
    hard_brake_g: float = 0.40
    #: g — decimated ``imu:ACCEL_LAT_MAX`` threshold for ``cornering``.
    cornering_g: float = 0.30
    #: km/h — a launch's starting speed must be at/below this ("near-stop").
    launch_speed_max_kph: float = 5.0
    #: seconds a qualifying hard_accel must sustain from near-stop to count
    #: as ``launch`` rather than an ordinary hard_accel.
    launch_sustain_seconds: float = 1.5
    #: km/h — ``gps:speed`` at/below this counts as ``stopped`` (GPS-only,
    #: bus-independent — works even with no OBD/CAN fix at all).
    stopped_speed_max_kph: float = 3.0
    #: km/h — ``gps:speed`` at/above this, sustained, is a highway_cruise
    #: candidate.
    highway_speed_min_kph: float = 90.0
    #: seconds ``gps:speed`` must hold >= ``highway_speed_min_kph`` before
    #: ``highway_cruise`` tags.
    highway_sustain_seconds: float = 20.0
    #: g² — max variance of lon/lat accel allowed during the sustained-speed
    #: window (keeps a fast, bumpy/swervy stretch from reading as "cruise").
    #: Only applied when IMU accel columns are present; ignored otherwise.
    highway_max_accel_variance_g2: float = 0.02


class LLMAssistConfig(_Strict):
    """Feature flag for the optional LLM-assist tier (Task S.4,
    docs/roadmap/llm-assist-and-compute-roadmap.md). Default ``False``: no
    producer exists yet (the roadmap's Sequencing step 1 is seams-only), and
    nothing on the capture path reads this block — it's read only by the
    dashboard's read-only analysis routes deciding whether to surface
    ``src.analysis.llm_assist`` cards or the disabled/absent state.
    """

    enabled: bool = False
    #: Where a future producer appends cards and the dashboard endpoint
    #: reads them from (``src.analysis.llm_assist.store.LlmAssistCardStore``).
    cards_path: Path = ANALYSES_DIR / "llm_assist_cards.jsonl"


class PlausibilityConfig(_Strict):
    """Deterministic plausibility checks in the live tick (A05.9).

    Off by default: the per-tick cost has not been measured on the Pi
    against the missed-tick budget yet. When on, the vehicle's
    ``plausibility.yaml`` (profile data directory) is required — without it
    the evaluator is withheld and ``/status`` says why, rather than running on
    guessed tier intervals and confirmation policy. Detections are evidence
    records in ``<anomalies_dir>/plausibility-evidence.jsonl`` (replays:
    ``<anomalies_dir>/_replay/<id>.plausibility-evidence.jsonl``).
    """

    enabled: bool = False


class CommitteesConfig(_Strict):
    """Sensor committees in the live tick (H04.2).

    On by default: with no ``committees.yaml`` in the profile data directory
    every committee reports ``not_configured`` and nothing flags, so the file
    — not this switch — is what arms a vehicle. ``enabled: false`` removes the
    evaluator entirely and ``/status`` says so.
    """

    enabled: bool = True


class CanaryConfig(_Strict):
    vehicle: VehicleConfig
    hardware: HardwareConfig
    collection: CollectionConfig
    preprocessing: PreprocessingConfig
    models: ModelsConfig
    dashboard: DashboardConfig
    daemon: DaemonConfig = DaemonConfig()
    alerts: AlertsConfig = AlertsConfig()
    profiles: ProfilesConfig = ProfilesConfig()
    trends: TrendsConfig = TrendsConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    online_training: OnlineTrainingConfig = OnlineTrainingConfig()
    nonvolatile_write: NonvolatileWriteConfig = NonvolatileWriteConfig()
    tuning_readiness: TuningReadinessConfig = TuningReadinessConfig()
    autotag: AutoTagConfig = AutoTagConfig()
    llm_assist: LLMAssistConfig = LLMAssistConfig()
    plausibility: PlausibilityConfig = PlausibilityConfig()
    committees: CommitteesConfig = CommitteesConfig()

    #: The profile slug :func:`src.vehicle.profiles.scope_config` nested this config's
    #: vehicle paths under; ``None`` for a config as loaded. Private, so no YAML
    #: can claim it: the one writer is ``scope_config``. It survives
    #: ``model_copy`` and pickling (the scoring child gets the parent's object).
    _scoped_profile: str | None = PrivateAttr(default=None)
    #: The ``VehicleProfile`` itself (opaque here: ``profiles`` sits above this
    #: module), so a freshly loaded YAML can be re-scoped exactly as this config
    #: was -- same slug, same registry vehicle parameters
    #: (:func:`src.vehicle.profiles.rescope_like`, the config hot-reload).
    _scope_source: Any = PrivateAttr(default=None)

    @property
    def scoped_profile(self) -> str | None:
        """The profile slug this config is scoped to, or ``None`` (unscoped)."""
        return self._scoped_profile

    @model_validator(mode="after")
    def _root_state_paths(self) -> Self:
        """The non-daemon runtime paths follow ``daemon.state_dir`` too (S03.1)."""
        state = self.daemon.state_dir
        if state is None:
            return self
        if "registry_overlay_path" not in self.profiles.model_fields_set:
            self.profiles = self.profiles.model_copy(
                update={"registry_overlay_path": state / "control" / "profiles.yaml"}
            )
        if "cards_path" not in self.llm_assist.model_fields_set:
            self.llm_assist = self.llm_assist.model_copy(
                update={"cards_path": state / "analyses" / "llm_assist_cards.jsonl"}
            )
        return self

    @model_validator(mode="after")
    def _broadcast_timeout_inside_session_gap(self) -> Self:
        """An unchanged CAN id is re-written only once per broadcast stale
        timeout (P02.10). A timeout at or past ``session_gap_seconds`` would
        let the gap between two stored frames of a quiet, CAN-only session
        reach the resampler's segment split, cutting one recording into
        segments that are not real discontinuities."""
        timeout = self.collection.broadcast_stale_timeout_seconds
        gap = self.preprocessing.session_gap_seconds
        if timeout >= gap:
            raise ValueError(
                f"collection.broadcast_stale_timeout_seconds ({timeout:g} s) must be under "
                f"preprocessing.session_gap_seconds ({gap:g} s): an unchanged CAN id is "
                "re-written only once per timeout (P02.10), so a longer one would split "
                "quiet recordings into segments at the heartbeat gaps"
            )
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> Self:
        with path.open("r") as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)


class UnscopedConfigError(ValueError):
    """A vehicle-data reader was handed a config ``scope_config`` never scoped.

    With profiles enabled, every vehicle-specific path (``afr_calibration`` and
    the files beside it) exists only under ``<base>/<slug>/``; the unscoped path
    names a file that is absent, and every reader treats absent as "this
    vehicle has none" -- silently.
    """


def require_profile_scoped(config: CanaryConfig, *, reader: str) -> None:
    """Refuse ``config`` for ``reader`` unless it is profile-scoped (P02.11).

    Unscoped is correct only with ``profiles.enabled: false``, where the base
    paths are the vehicle's. The incident this names: the daemon scopes its
    config in ``main`` and so applied the vehicle's ``afr_calibration.yaml``
    while the trainers and the evaluation harness featurized through the
    unscoped config, where ``data/afr_calibration.yaml`` does not exist -- every
    offline feature frame was built without the calibration the served one had.
    """
    if config.profiles.enabled and config.scoped_profile is None:
        raise UnscopedConfigError(
            f"{reader} needs a profile-scoped config (src.vehicle.profiles.scope_config): with "
            "profiles enabled the vehicle's data lives under <base>/<slug>/, and "
            f"{config.daemon.afr_calibration} is the unscoped path. The trainers and "
            "the evaluation harness once featurized through it without the vehicle's "
            "afr_calibration.yaml while the daemon applied it (P02.11). Scope the "
            "config once at the entry point (resolve_profile_for_tooling + "
            "scope_config), or disable profiles."
        )


def _daemon_path_fields() -> tuple[str, ...]:
    """Every ``Path`` or ``Path | None`` :class:`DaemonConfig` field, read off
    the model.

    Any other annotation that contains ``Path`` (``list[Path]``, a dict of
    them, ...) is refused rather than skipped: :func:`rebase_state_paths`
    would otherwise leave those paths pointing wherever they were, silently.
    """
    fields: list[str] = []
    for name, field in DaemonConfig.model_fields.items():
        annotation = field.annotation
        if annotation is Path:
            fields.append(name)
            continue
        args = get_args(annotation)
        if args and set(args) == {Path, type(None)}:
            fields.append(name)
        elif _mentions_path(annotation):
            raise TypeError(
                f"DaemonConfig.{name}: {annotation!r} holds paths rebase_state_paths "
                "cannot rebase; use Path or Path | None, or teach it this shape"
            )
    return tuple(fields)


def _mentions_path(annotation: Any) -> bool:
    return annotation is Path or any(_mentions_path(arg) for arg in get_args(annotation))


def rebase_state_paths(
    config: CanaryConfig, root: Path, *, keep: Iterable[str] = ()
) -> CanaryConfig:
    """``config`` with every daemon file/directory path moved under ``root``.

    The field list is derived from :class:`DaemonConfig`'s ``Path``-typed
    fields, so a path added there later is rebased without anyone remembering
    to list it (R07.2: a hand-kept 12-entry copy in the validation harness had
    already missed ``capture_profiles``). A relative path keeps its shape under
    ``root`` (``data/control/audit.jsonl`` -> ``root/data/control/audit.jsonl``);
    an absolute one is re-rooted the same way, so two fields never collide.

    ``keep`` names fields left where they are -- read-only inputs such as
    ``behavior_models`` that the caller wants to read from the real tree. A
    name that is not a ``Path`` field of :class:`DaemonConfig` is refused, so
    a typo cannot silently leave a writable path pointing at the repo.
    """
    fields = _daemon_path_fields()
    kept = frozenset(keep)
    unknown = kept - set(fields)
    if unknown:
        raise ValueError(
            f"rebase_state_paths: {sorted(unknown)} are not Path fields of DaemonConfig "
            f"(known: {sorted(fields)})"
        )
    update: dict[str, Path] = {}
    for name in fields:
        if name in kept:
            continue
        path: Path | None = getattr(config.daemon, name)
        if path is None:
            continue
        update[name] = root / (path.relative_to(path.anchor) if path.is_absolute() else path)
    return config.model_copy(update={"daemon": config.daemon.model_copy(update=update)})
