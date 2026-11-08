"""The ``hardware:`` section: the CAN bus, GPS, IMU, ADCs and K-line (R21)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from src.config.base import _Strict
from src.lsm6dsox_settings import ImuSettings
from src.paths import DATA_DIR


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

    Phase 2a/2b (``docs/roadmap/archive/imu-gps-integration.md``). Optional and
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
    """The CANary-HAT's 12 V sense: the protected ``+12V`` rail through a
    resistor divider. Recorded as ``sensor:SUPPLY_VOLTAGE``.

    Unlike ``obd:CONTROL_MODULE_VOLTAGE`` it needs no awake ECM, so it reads
    key-off and parked. Disabled by default (no HAT, no ADC). An
    external-sensor channel: never trains or scores the anomaly detectors.

    The fields below are rev 2.0's ADS1015. On rev 2.1 the same rail is the
    ADS8688A's CH0, read by the ``hardware.hat_adc`` collector with the divider
    from its board map; there only ``enabled`` and ``rate_hz`` apply.
    """

    enabled: bool = False
    i2c_bus: int = 1
    i2c_address: int = 0x48
    #: ADS1015 input the divider feeds (single-ended). HAT rev 2.0: AIN0.
    input_channel: int = Field(default=0, ge=0, le=3)
    #: Rail volts per volt at the ADC pin. HAT rev 2.0: R11 100 k over R12
    #: 22 k, so (100 + 22) / 22. A per-board fact, kept here, not in code.
    divider_ratio: float = Field(default=122.0 / 22.0, gt=1.0)
    #: Readings recorded per second. The rail moves slowly except while cranking.
    rate_hz: float = Field(default=10.0, gt=0.0, le=200.0)


class HatAdcConfig(_Strict):
    """The CANary-HAT rev 2.1's ADC (ADS8688A, U8): the fuel-pressure gauge
    and the two generic sensor ports. ``src.collection.hat_adc_collector``.

    Disabled by default; rev 2.1 only (rev 2.0 has no such ADC). The channel
    map and every calibration number are data (``board``). The same chip also
    carries rev 2.1's 12 V sense, which ``hardware.supply_voltage`` switches.
    Recorded under ``sensor:``, so never trains or scores the detectors.

    **Rates.** The chip shares SPI0 with the vehicle CAN controller, so it is
    read in short bursts, one bus message each (about 75 µs for five inputs).
    The fast channels (the sensors and the voltage ports) are stored at
    ``record_hz``, each record the mean of ``burst_hz / record_hz`` bursts: the
    USB ADC path stores fuel pressure the same way, 100 Hz averaged from about
    500, so injector pulsation is not aliased into it. Supply rails and the
    resistive ports move slowly and are stored at ``slow_record_hz``.
    """

    enabled: bool = False
    #: ``None``: the revision table's (``/dev/spidev0.1`` on rev 2.1).
    spi_device: str | None = None
    #: The ADS8688A allows 17 MHz.
    spi_speed_hz: int = Field(default=8_000_000, gt=0, le=17_000_000)
    #: Bursts per second, every mapped input in each. At 500 the chip holds
    #: the bus a few per cent of the time; measure CAN frame loss with it
    #: idle and running at bring-up (docs/CANary-HAT-Rev2.1.md §13.5.3).
    burst_hz: float = Field(default=500.0, gt=0.0, le=2000.0)
    record_hz: float = Field(default=100.0, gt=0.0)
    slow_record_hz: float = Field(default=10.0, gt=0.0)
    #: Drive the sensor supply's enable (``SENS_PWR_EN``, GPIO19 on rev 2.1)
    #: high while collecting, low on stop. Off, the ports read the supply as
    #: low and record that instead of ratios.
    sensor_power: bool = True
    gpio_chip: str = "/dev/gpiochip0"
    board: Path = DATA_DIR / "hat_adc_rev2_1.yaml"

    @model_validator(mode="after")
    def _rates_nest(self) -> Self:
        if not self.slow_record_hz <= self.record_hz <= self.burst_hz:
            raise ValueError(
                "hardware.hat_adc: need slow_record_hz <= record_hz <= burst_hz, got "
                f"{self.slow_record_hz} / {self.record_hz} / {self.burst_hz}"
            )
        return self


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
    #: TLIN1027 EN and the commander pull-up FET gate, as line offsets on
    #: ``gpio_chip``. ``None`` (the default) takes them from the HAT revision
    #: (``hardware.hat_revision``): EN is GPIO26 on both, the pull-up GPIO24 on
    #: rev 2.0 and GPIO21 on rev 2.1. Set them only for a board the revision
    #: table does not describe.
    enable_gpio: int | None = None
    pullup_gpio: int | None = None
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


#: Which CANary-HAT revision is fitted. ``auto`` reads the HAT's ID EEPROM; a
#: name overrides it. The names are the rows of
#: ``hardware/canary-hat-firmware/hat_revisions.json`` (a test keeps them equal).
HatRevisionSetting = Literal["auto", "2.0", "2.1"]


class HardwareConfig(_Strict):
    elm327_port: str
    #: Rev 2.0 and rev 2.1 wire the K-line pull-up and the 12 V sense
    #: differently (``src.collection.hat_revision``).
    hat_revision: HatRevisionSetting = "auto"
    can: CanBusConfig
    gps: GpsConfig = GpsConfig()
    imu: ImuConfig = ImuConfig()
    sensor_can: SensorCanConfig = SensorCanConfig()
    supply_voltage: SupplyVoltageConfig = SupplyVoltageConfig()
    hat_adc: HatAdcConfig = HatAdcConfig()
    usb_adc: UsbAdcConfig = UsbAdcConfig()
    kline: KlineConfig = KlineConfig()
