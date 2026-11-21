"""Tell a CANary-HAT revision by its own ADC, for a board whose ID EEPROM is blank.

A new board arrives with nothing in its EEPROM, so the firmware names no
revision. Each revision's 12 V sense is a different converter on a different
bus, and each answers a read-only known answer at power-up that no idle bus
and no other part gives:

- rev 2.0, ADS1015 on I2C1 0x48: Lo_thresh 0x8000 and Hi_thresh 0x7FFF
  (SBAS473 register map). Nothing in CANary writes the comparator thresholds.
- rev 2.1, ADS8688A on SPI0 CE1: AUTO_SEQ_EN 0xFF and CH7's range 0x00
  (SBAS582 Table 9). The driver writes neither (CH5-CH7 are grounded,
  docs/CANary-HAT-Rev2.1.md §13.5.13), and a test holds it to that. An idle
  bus reads the same byte from both.

Standard library only (with its siblings ``i2c_dev``, ``spi_dev`` and
``ads8688a``, which are too), so ``hardware/canary-hat-firmware/hat_rev.py``
runs this same code with the Pi's system python3. The daemon's reader is
:mod:`src.collection.hat_revision`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from src.collection.ads8688a import read_frame, reg_range
from src.collection.i2c_dev import LinuxI2cBus, RegisterBus
from src.collection.spi_dev import SPI_MODE_1, LinuxSpiBus, SpiBus

#: Rev 2.0's 12 V ADC; ``SupplyVoltageConfig``'s defaults (a test keeps them equal).
ADS1015_BUS = 1
ADS1015_ADDRESS = 0x48
ADS1015_THRESHOLDS = {0x02: b"\x80\x00", 0x03: b"\x7f\xff"}
ADS8688A_AUTO_SEQ_EN = 0x01
ADS8688A_PROBE_CHANNEL = 7
ADS8688A_ANSWERS = {ADS8688A_AUTO_SEQ_EN: 0xFF, reg_range(ADS8688A_PROBE_CHANNEL): 0x00}
PROBE_SPI_HZ = 1_000_000

I2cFactory = Callable[[int, int], RegisterBus]
SpiFactory = Callable[[str], SpiBus]
#: ``(revisions whose ADC answered, what each probe saw)``.
ProbeResult = tuple[tuple[str, ...], str]


def _linux_i2c(bus: int, address: int) -> RegisterBus:
    return LinuxI2cBus(bus, address)


def _linux_spi(device: str) -> SpiBus:
    return LinuxSpiBus(device, mode=SPI_MODE_1, speed_hz=PROBE_SPI_HZ)


def _why(exc: OSError) -> str:
    return exc.strerror or str(exc)


def ads1015_answers(i2c: I2cFactory = _linux_i2c) -> tuple[bool, str]:
    """``(answered, what was seen)`` for rev 2.0's 12 V ADC. Reads only."""
    where = f"ADS1015 on I2C{ADS1015_BUS} {ADS1015_ADDRESS:#04x}"
    try:
        bus = i2c(ADS1015_BUS, ADS1015_ADDRESS)
    except OSError as exc:
        return False, f"{where}: {_why(exc)}"
    try:
        seen = {reg: bus.read(reg, 2) for reg in ADS1015_THRESHOLDS}
    except OSError as exc:
        return False, f"{where}: no answer ({_why(exc)})"
    finally:
        bus.close()
    if seen == ADS1015_THRESHOLDS:
        return True, f"{where} answered"
    got = ", ".join(f"{reg:#04x}={value.hex()}" for reg, value in seen.items())
    return False, f"{where}: a device answered, not an ADS1015 ({got})"


def ads8688a_answers(device: str, spi: SpiFactory = _linux_spi) -> tuple[bool, str]:
    """``(answered, what was seen)`` for rev 2.1's ADC. Reads only."""
    where = f"ADS8688A on {device}"
    try:
        bus = spi(device)
    except OSError as exc:
        return False, f"{where}: {_why(exc)}"
    try:
        responses = bus.transfer([read_frame(reg) for reg in ADS8688A_ANSWERS])
    except OSError as exc:
        return False, f"{where}: {_why(exc)}"
    finally:
        bus.close()
    seen = {reg: r[2] for reg, r in zip(ADS8688A_ANSWERS, responses, strict=True)}
    if seen == ADS8688A_ANSWERS:
        return True, f"{where} answered"
    got = ", ".join(f"{reg:#04x}={value:#04x}" for reg, value in seen.items())
    return False, f"{where}: no ADS8688A ({got})"


def probe_adcs(
    rows: Mapping[str, Mapping[str, Any]],
    *,
    i2c: I2cFactory = _linux_i2c,
    spi: SpiFactory = _linux_spi,
) -> ProbeResult:
    """Probe each revision's ADC (the table's ``supply_adc``). Reads only."""
    found: list[str] = []
    seen: list[str] = []
    for name, row in rows.items():
        if row["supply_adc"] == "ads1015":
            answered, what = ads1015_answers(i2c)
        elif row["supply_adc"] == "ads8688a" and row["adc_spi_device"]:
            answered, what = ads8688a_answers(row["adc_spi_device"], spi)
        else:
            continue
        seen.append(what)
        if answered:
            found.append(name)
    return tuple(found), "; ".join(seen)


__all__ = [
    "ADS1015_ADDRESS",
    "ADS1015_BUS",
    "ADS1015_THRESHOLDS",
    "ADS8688A_ANSWERS",
    "ADS8688A_PROBE_CHANNEL",
    "ProbeResult",
    "ads1015_answers",
    "ads8688a_answers",
    "probe_adcs",
]
