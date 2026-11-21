"""Which CANary-HAT revision is fitted, and where it wires what the daemon drives.

Rev 2.1 moved pins (``docs/CANary-HAT-Rev2.1.md`` §13.5.13): the K-line
commander pull-up went from GPIO24 to GPIO21, and the 12 V sense moved from the
ADS1015 to the ADS8688A, whose sensor supply GPIO19 switches. Both revisions
are in service, so nothing here assumes one. The facts are
``hardware/canary-hat-firmware/hat_revisions.json``, which
the board's bring-up scripts read too (``hat_rev.py`` beside it).

The fitted revision is, in order:

1. the one ``hardware.hat_revision`` names;
2. the one the HAT's ID EEPROM reports (``/proc/device-tree/hat``);
3. with no CANary-HAT EEPROM, the one whose own ADC answers a known-answer
   probe (:mod:`src.collection.hat_probe`): rev 2.0's ADS1015 on I2C1, rev
   2.1's ADS8688A on SPI0 CE1. A new board arrives with a blank EEPROM, so this is how the
   first boot of one is told apart;
4. otherwise none, and nothing that drives a HAT pin starts.

Nothing guesses a pin map. Until 2026-11-20 a Pi with no CANary-HAT EEPROM got
rev 2.0's, and on the first rev 2.1 board (blank, with CANary-PWR stacked) that
put the K-line pull-up on GPIO24, which is the power supervisor's reset, looked
for an ADS1015 that is not there every 10-20 s, and read ``no_data`` where
``bench`` was true. Only a log line said so.

Detection runs once per process (:func:`fitted_hat`): the EEPROM and the
overlay cannot change without a reboot, and the probe must not run beside the
collector that owns the ADC. Its verdict, with what it found wrong, is
``/status.hat`` (:func:`hat_status`).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from src.collection.hat_probe import ProbeResult, probe_adcs
from src.collection.kline.transport import HatPins
from src.config import (
    HardwareConfig,
    HatRevisionSetting,
    KlineConfig,
    PowerBoardSetting,
)
from src.paths import HAT_REVISIONS_JSON

log = logging.getLogger(__name__)

#: Where the Pi firmware publishes the HAT's ID EEPROM.
HAT_DIR = Path("/proc/device-tree/hat")
#: Where the board UARTs appear, and the kernel's PPS devices.
DEV_DIR = Path("/dev")
PPS_DIR = Path("/sys/class/pps")
PRODUCT_PREFIX = "CANary-HAT"
#: The name both overlays give their TIMEPULSE ``pps-gpio`` device.
PPS_NAME_PREFIX = "canary-pps"

#: How a revision was named; ``none`` when none could be.
RevisionSource = Literal["config", "eeprom", "probed"]
HatSource = Literal["config", "eeprom", "probed", "none"]

#: GPIOs CANary-PWR owns on the Pi's header (docs/CANary-PWR-Spec.md §P6):
#: nothing the daemon drives may use them. Rev 2.0's K-line pull-up is GPIO24,
#: which is why PWR does not support rev 2.0 (§P8).
POWER_BOARD_GPIOS: dict[str, dict[int, str]] = {
    "none": {},
    "1.0": {24: "PI_NRST, the supervisor's reset", 27: "PI_BOOT0, the supervisor's BOOT0"},
}


class HatRevisionError(ValueError):
    """No revision can be named: an EEPROM reporting one the table does not
    list, or no CANary-HAT at all."""


@dataclass(frozen=True)
class HatRevision:
    name: str
    product_ver: int
    #: The device-tree overlay the board's EEPROM names (``dt_blob``).
    overlay: str
    #: The GNSS UART that overlay creates.
    gnss_tty: str
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
    #: How it was chosen: ``config``, ``eeprom`` or ``probed``.
    source: RevisionSource


@dataclass(frozen=True)
class FittedHat:
    """What is on the header, how that was decided, and what is wrong with it."""

    #: ``None``: no CANary-HAT could be named, and no HAT pin is driven.
    revision: HatRevision | None
    source: HatSource
    #: The ID EEPROM's product string; ``None`` when the firmware published none.
    eeprom_product: str | None
    #: What the ADC probe found; ``None`` when the config or the EEPROM decided.
    probe: str | None
    #: Whether the kernel shows this revision's overlay (its GNSS UART and the
    #: CANary PPS device); ``None`` with no revision.
    overlay_loaded: bool | None
    problems: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        rev = self.revision
        return {
            "revision": rev.name if rev else None,
            "source": self.source,
            "eeprom_product": self.eeprom_product,
            "probe": self.probe,
            "overlay": rev.overlay if rev else None,
            "overlay_loaded": self.overlay_loaded,
            "problems": list(self.problems),
        }


def load_hat_revisions(path: Path = HAT_REVISIONS_JSON) -> dict[str, dict[str, Any]]:
    """``{name: row}`` from the revision table."""
    table = json.loads(path.read_text(encoding="utf-8"))
    return {row["name"]: row for row in table["revisions"]}


def _revision(row: Mapping[str, Any], source: RevisionSource) -> HatRevision:
    return HatRevision(
        name=row["name"],
        product_ver=int(row["product_ver"], 16),
        overlay=row["overlay"],
        gnss_tty=row["gnss_tty"],
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


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def _pps_registered(pps_dir: Path) -> bool:
    return any(_read(name).startswith(PPS_NAME_PREFIX) for name in pps_dir.glob("pps*/name"))


def _overlay_problems(
    rev: HatRevision, rows: Mapping[str, Mapping[str, Any]], dev_dir: Path, pps_dir: Path
) -> tuple[bool, list[str]]:
    """Whether the kernel shows ``rev``'s overlay, and what is wrong if not."""
    tty = dev_dir / Path(rev.gnss_tty).name
    if tty.exists() and _pps_registered(pps_dir):
        return True, []
    others = [
        row
        for name, row in rows.items()
        if name != rev.name and (dev_dir / Path(row["gnss_tty"]).name).exists()
    ]
    if others and _pps_registered(pps_dir):
        other = others[0]
        return False, [
            f"the kernel has rev {other['name']}'s overlay ({other['gnss_tty']}), not rev "
            f"{rev.name}'s ({rev.gnss_tty}): the overlay loaded is not this board's"
        ]
    return False, [
        f"rev {rev.name}'s overlay {rev.overlay} is not loaded (no {rev.gnss_tty} or "
        f"{PPS_NAME_PREFIX} device): the HAT's CAN, GNSS and PPS are absent until its ID "
        f"EEPROM names the overlay (program_hat.sh --rev {rev.name}) or config.txt loads it"
    ]


def detect_hat(
    setting: HatRevisionSetting,
    *,
    hat_dir: Path = HAT_DIR,
    table_path: Path = HAT_REVISIONS_JSON,
    probe: Callable[[Mapping[str, Mapping[str, Any]]], ProbeResult] = probe_adcs,
    dev_dir: Path = DEV_DIR,
    pps_dir: Path = PPS_DIR,
) -> FittedHat:
    """What is fitted (module docstring, steps 1-4). Uncached: the daemon calls
    :func:`fitted_hat`."""
    rows = load_hat_revisions(table_path)
    product = _read(hat_dir / "product")
    product_ver = _read(hat_dir / "product_ver")
    eeprom_product = product or None
    problems: list[str] = []
    eeprom_row: Mapping[str, Any] | None = None
    if product.startswith(PRODUCT_PREFIX) and product_ver:
        try:
            want = int(product_ver, 16)
        except ValueError:
            want = -1
        eeprom_row = next((r for r in rows.values() if int(r["product_ver"], 16) == want), None)
        if eeprom_row is None and setting == "auto":
            return FittedHat(
                None,
                "eeprom",
                eeprom_product,
                None,
                None,
                (
                    f"{product!r} reports product_ver {product_ver}, which {table_path.name} "
                    "does not list: update the checkout, or set hardware.hat_revision",
                ),
            )

    probe_seen: str | None = None
    if setting != "auto":
        rev = _revision(rows[setting], "config")
        if eeprom_row is not None and eeprom_row["name"] != setting:
            problems.append(
                f"hardware.hat_revision names rev {setting}, but the ID EEPROM reports rev "
                f"{eeprom_row['name']}; the config wins"
            )
    elif eeprom_row is not None:
        rev = _revision(eeprom_row, "eeprom")
    else:
        found, probe_seen = probe(rows)
        held = f"the ID EEPROM names {product!r}" if product else "no CANary-HAT ID EEPROM"
        if len(found) != 1:
            if found:
                problems.append(
                    f"{held}, and the ADCs of revs {', '.join(found)} all answered: set "
                    "hardware.hat_revision"
                )
            return FittedHat(None, "none", eeprom_product, probe_seen, None, tuple(problems))
        rev = _revision(rows[found[0]], "probed")
        problems.append(
            f"{held} (a new board's is blank): rev {rev.name} was identified by its ADC. "
            f"Program the EEPROM with program_hat.sh --rev {rev.name}"
        )
    loaded, overlay = _overlay_problems(rev, rows, dev_dir, pps_dir)
    problems.extend(overlay)
    return FittedHat(rev, rev.source, eeprom_product, probe_seen, loaded, tuple(problems))


_fitted: dict[str, FittedHat] = {}
_fitted_lock = threading.Lock()


def fitted_hat(setting: HatRevisionSetting) -> FittedHat:
    """:func:`detect_hat` once per process and setting, logged once."""
    with _fitted_lock:
        hat = _fitted.get(setting)
        if hat is None:
            hat = detect_hat(setting)
            _fitted[setting] = hat
            if hat.revision is None:
                log.info(
                    "no CANary-HAT identified (%s); nothing drives a HAT pin",
                    hat.probe or hat.eeprom_product or "no ID EEPROM",
                )
            else:
                log.info("CANary-HAT rev %s (%s)", hat.revision.name, hat.source)
            for problem in hat.problems:
                log.warning("CANary-HAT: %s", problem)
        return hat


def forget_fitted_hat() -> None:
    """Drop :func:`fitted_hat`'s verdicts (tests)."""
    with _fitted_lock:
        _fitted.clear()


def resolve_hat_revision(
    setting: HatRevisionSetting,
    *,
    hat_dir: Path = HAT_DIR,
    table_path: Path = HAT_REVISIONS_JSON,
    probe: Callable[[Mapping[str, Mapping[str, Any]]], ProbeResult] = probe_adcs,
) -> HatRevision:
    """The revision :func:`detect_hat` names, or :class:`HatRevisionError`.
    Uncached, for one-shot tools (the ADC bring-up CLI) and tests."""
    hat = detect_hat(setting, hat_dir=hat_dir, table_path=table_path, probe=probe)
    if hat.revision is None:
        raise HatRevisionError(
            "; ".join(hat.problems)
            or f"no CANary-HAT identified ({hat.probe or 'no ID EEPROM'}): "
            "set hardware.hat_revision"
        )
    return hat.revision


def kline_pins(config: KlineConfig, revision: HatRevision | None) -> HatPins | None:
    """The transceiver's lines: the revision's, unless the config pins them.
    ``None`` when neither names both."""
    enable = config.enable_gpio
    pullup = config.pullup_gpio
    if revision is not None:
        enable = revision.kline_enable_gpio if enable is None else enable
        pullup = revision.kline_pullup_gpio if pullup is None else pullup
    if enable is None or pullup is None:
        return None
    return HatPins(tty=config.tty, gpio_chip=config.gpio_chip, enable=enable, pullup=pullup)


def power_board_conflicts(board: PowerBoardSetting, gpios: Mapping[str, int | None]) -> list[str]:
    """``"<what> GPIO<n> is <PWR net>"`` for each of ``gpios`` the power board owns."""
    owned = POWER_BOARD_GPIOS[board]
    return [
        f"{what} GPIO{gpio} is CANary-PWR {board}'s {owned[gpio]}"
        for what, gpio in gpios.items()
        if gpio is not None and gpio in owned
    ]


def _needs_a_hat(hardware: HardwareConfig) -> list[str]:
    wanted = {
        "hardware.kline": hardware.kline.enabled,
        "hardware.supply_voltage": hardware.supply_voltage.enabled,
        "hardware.hat_adc": hardware.hat_adc.enabled,
    }
    return [name for name, on in wanted.items() if on]


def hat_status(hardware: HardwareConfig) -> dict[str, Any]:
    """``/status.hat``: :func:`fitted_hat`'s verdict, the power board, and every
    problem with the pair as configured."""
    hat = fitted_hat(hardware.hat_revision)
    out = hat.to_dict()
    problems: list[str] = out["problems"]
    rev = hat.revision
    if rev is None:
        wanted = _needs_a_hat(hardware)
        if wanted:
            problems.append(
                f"{', '.join(wanted)} enabled, but no CANary-HAT was identified: they are off"
            )
    board = hardware.power_board
    if rev is not None:
        pins = kline_pins(hardware.kline, rev)
        problems.extend(
            power_board_conflicts(
                board,
                {
                    "the K-line EN": pins.enable if pins else None,
                    "the K-line pull-up": pins.pullup if pins else None,
                    "SENS_PWR_EN": rev.sensor_power_gpio,
                },
            )
        )
    out["power_board"] = None if board == "none" else board
    return out


#: The names ``hardware.hat_revision`` accepts besides ``auto``.
SETTING_NAMES = tuple(name for name in get_args(HatRevisionSetting) if name != "auto")

__all__ = [
    "HAT_DIR",
    "POWER_BOARD_GPIOS",
    "SETTING_NAMES",
    "FittedHat",
    "HatRevision",
    "HatRevisionError",
    "detect_hat",
    "fitted_hat",
    "forget_fitted_hat",
    "hat_status",
    "kline_pins",
    "load_hat_revisions",
    "power_board_conflicts",
    "resolve_hat_revision",
]
