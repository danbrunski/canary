"""One 12 V reading before the collectors start, from whichever ADC the HAT has.

The startup VIN probe asks once, so it can skip transmitting into an empty
OBD port on the bench (:mod:`src.collection.vehicle_probe`). Rev 2.0 reads the
rail through an ADS1015 (:mod:`src.collection.supply_voltage_collector`),
rev 2.1 through the ADS8688A's CH0 (:mod:`src.collection.hat_adc_collector`).
"""

from __future__ import annotations

import logging

from src.collection import hat_adc_collector, supply_voltage_collector
from src.collection.hat_revision import HatRevisionError, resolve_hat_revision
from src.config import HardwareConfig

log = logging.getLogger(__name__)


def read_supply_once(hardware: HardwareConfig) -> float | None:
    """The rail in volts, or ``None``: no 12 V sense enabled, or no reading."""
    supply = hardware.supply_voltage
    if not supply.enabled:
        # No revision lookup for a Pi without the sense: it would log a
        # missing ID EEPROM at every start.
        return supply_voltage_collector.read_supply_once(supply)
    try:
        revision = resolve_hat_revision(hardware.hat_revision)
    except HatRevisionError:
        log.warning("12 V sense not read at startup", exc_info=True)
        return None
    if revision.supply_adc == "ads8688a":
        return hat_adc_collector.read_supply_once(hardware.hat_adc, revision)
    return supply_voltage_collector.read_supply_once(supply)


__all__ = ["read_supply_once"]
