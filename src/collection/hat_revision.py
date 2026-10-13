"""Which CANary-HAT revision is fitted, and where it wires what the daemon drives.

Rev 2.1 moved pins (``docs/CANary-HAT-Rev2.1.md`` §13.5.13): the K-line
commander pull-up went from GPIO24 to GPIO21, and the 12 V sense moved from the
ADS1015 to the ADS8688A, whose sensor supply GPIO19 switches. Both revisions
are in service, so nothing here assumes one. The facts are
``hardware/canary-hat-firmware/hat_revisions.json``, which
the board's bring-up scripts read too (``hat_rev.py`` beside it). The fitted
revision is the one the HAT's ID EEPROM reports, unless
``hardware.hat_revision`` names one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from src.collection.kline.transport import HatPins
from src.config import HatRevisionSetting, KlineConfig
from src.paths import HAT_REVISIONS_JSON

log = logging.getLogger(__name__)

#: Where the Pi firmware publishes the HAT's ID EEPROM.
HAT_DIR = Path("/proc/device-tree/hat")
PRODUCT_PREFIX = "CANary-HAT"


class HatRevisionError(ValueError):
    """An EEPROM reporting a revision the table does not list."""


@dataclass(frozen=True)
class HatRevision:
    name: str
    product_ver: int
    kline_enable_gpio: int
    kline_pullup_gpio: int
    #: The 12 V sense's converter: ``ads1015`` (rev 2.0, I2C, what
    #: :mod:`src.collection.supply_voltage_collector` reads) or ``ads8688a``
    #: (rev 2.1, SPI, :mod:`src.collection.hat_adc_collector`).
    supply_adc: str
    #: The ADS8688A's spidev node; ``None`` on a board without one.
    adc_spi_device: str | None
    #: ``SENS_PWR_EN``, the 5 V sensor supply's enable; ``None`` where there is none.
    sensor_power_gpio: int | None
    #: How it was chosen: ``config``, ``eeprom`` or ``unidentified``.
    source: str


def load_hat_revisions(path: Path = HAT_REVISIONS_JSON) -> tuple[dict[str, dict[str, Any]], str]:
    """``({name: row}, unidentified_name)`` from the revision table."""
    table = json.loads(path.read_text(encoding="utf-8"))
    rows = {row["name"]: row for row in table["revisions"]}
    return rows, table["unidentified"]


def _revision(row: dict[str, Any], source: str) -> HatRevision:
    return HatRevision(
        name=row["name"],
        product_ver=int(row["product_ver"], 16),
        kline_enable_gpio=row["kline_enable_gpio"],
        kline_pullup_gpio=row["kline_pullup_gpio"],
        supply_adc=row["supply_adc"],
        adc_spi_device=row["adc_spi_device"],
        sensor_power_gpio=row["sensor_power_gpio"],
        source=source,
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").replace("\0", "").strip()
    except OSError:
        return ""


def resolve_hat_revision(
    setting: HatRevisionSetting,
    *,
    hat_dir: Path = HAT_DIR,
    table_path: Path = HAT_REVISIONS_JSON,
) -> HatRevision:
    """The revision ``setting`` names, or for ``auto`` the one the EEPROM
    reports. A Pi with no CANary-HAT EEPROM gets the table's ``unidentified``
    revision (rev 2.0, the only one that has run without one); an EEPROM
    reporting an unlisted product_ver is refused, since guessing would drive
    another revision's pins."""
    rows, unidentified = load_hat_revisions(table_path)
    if setting != "auto":
        return _revision(rows[setting], "config")
    product = _read(hat_dir / "product")
    product_ver = _read(hat_dir / "product_ver")
    if product.startswith(PRODUCT_PREFIX) and product_ver:
        try:
            want = int(product_ver, 16)
        except ValueError:
            want = -1
        for row in rows.values():
            if int(row["product_ver"], 16) == want:
                return _revision(row, "eeprom")
        raise HatRevisionError(
            f"{product!r} reports product_ver {product_ver}, which {table_path.name} does not "
            "list: update the checkout, or set hardware.hat_revision"
        )
    log.warning(
        "no CANary-HAT ID EEPROM in %s; using the rev %s pin map (set hardware.hat_revision "
        "to name the board)",
        hat_dir,
        unidentified,
    )
    return _revision(rows[unidentified], "unidentified")


def kline_pins(config: KlineConfig, revision: HatRevision) -> HatPins:
    """The transceiver's lines: the revision's, unless the config pins them."""
    return HatPins(
        tty=config.tty,
        gpio_chip=config.gpio_chip,
        enable=config.enable_gpio if config.enable_gpio is not None else revision.kline_enable_gpio,
        pullup=config.pullup_gpio if config.pullup_gpio is not None else revision.kline_pullup_gpio,
    )


#: The names ``hardware.hat_revision`` accepts besides ``auto``.
SETTING_NAMES = tuple(name for name in get_args(HatRevisionSetting) if name != "auto")

__all__ = [
    "HAT_DIR",
    "SETTING_NAMES",
    "HatRevision",
    "HatRevisionError",
    "kline_pins",
    "load_hat_revisions",
    "resolve_hat_revision",
]
