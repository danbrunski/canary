"""The CANary-HAT revision table, and everything that must agree with it.

Rev 2.0 and rev 2.1 wire the GNSS UART, the PPS line and the K-line pull-up to
different GPIOs, and both boards are in service. One table
(``hardware/canary-hat-firmware/hat_revisions.json``) holds the facts; these
tests pin the overlays, the EEPROM settings, the config setting and both of the
table's readers (the daemon's and the bring-up scripts') to it.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from src.collection.hat_revision import (
    SETTING_NAMES,
    HatRevisionError,
    kline_pins,
    resolve_hat_revision,
)
from src.config import KlineConfig
from src.paths import HAT_REVISIONS_JSON

FIRMWARE = HAT_REVISIONS_JSON.parent
TABLE: dict[str, Any] = json.loads(HAT_REVISIONS_JSON.read_text(encoding="utf-8"))
ROWS = {row["name"]: row for row in TABLE["revisions"]}
#: The X1202 UPS's power-loss line, held by scripts/ups_shutdown_watch.py.
X1202_PLD_GPIO = 6


def _hat_rev() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hat_rev", FIRMWARE / "hat_rev.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _eeprom(tmp_path: Path, product: str, product_ver: str) -> Path:
    hat = tmp_path / "hat"
    hat.mkdir()
    # The firmware's strings are NUL-terminated.
    (hat / "product").write_text(product + "\0")
    (hat / "product_ver").write_text(product_ver + "\0")
    return hat


def _code(dts: str) -> list[str]:
    """The overlay's non-comment, non-blank lines."""
    lines = (re.sub(r"//.*", "", line).strip() for line in dts.splitlines())
    return [line for line in lines if line]


# --------------------------------------------------------------------------
# The table against the files it names
# --------------------------------------------------------------------------


def test_the_config_setting_names_exactly_the_table_rows() -> None:
    assert sorted(SETTING_NAMES) == sorted(ROWS)
    assert TABLE["unidentified"] in ROWS


@pytest.mark.parametrize("name", sorted(ROWS))
def test_each_revision_eeprom_names_its_own_overlay_and_product_ver(name: str) -> None:
    row = ROWS[name]
    settings = (FIRMWARE / row["eeprom_settings"]).read_text(encoding="utf-8")
    assert re.search(rf'^dt_blob "{re.escape(row["overlay"])}"$', settings, re.M)
    ver = re.search(r"^product_ver (0x[0-9a-fA-F]+)$", settings, re.M)
    assert ver and int(ver.group(1), 16) == int(row["product_ver"], 16)
    assert re.search(rf'^product "CANary-HAT rev {re.escape(name)}"$', settings, re.M)


@pytest.mark.parametrize("name", sorted(ROWS))
def test_each_overlay_wires_the_gnss_and_pps_its_row_says(name: str) -> None:
    row = ROWS[name]
    dts = (FIRMWARE / f"{row['overlay']}-overlay.dts").read_text(encoding="utf-8")
    assert f"target = <&{row['gnss_uart']}>;" in dts
    assert f"pinctrl-0 = <&{row['gnss_uart']}_pins>;" in dts
    pps = row["pps_gpio"]
    assert re.search(rf"canary_pps_pins \{{\s*brcm,pins = <{pps}>;", dts)
    assert f"gpios = <&gpio {pps} 0>;" in dts
    # The node name is what /dev/pps-canary's udev rule matches, on every board.
    assert "canary_pps: canary-pps {" in dts


def test_the_overlays_differ_only_in_the_gnss_uart_and_pps_pin() -> None:
    """Rev 2.0's overlay is proven on boards; rev 2.1's is the same code with
    the moved pins substituted, so it compiles and binds the same way."""
    old, new = ROWS["2.0"], ROWS["2.1"]
    expected = (FIRMWARE / f"{old['overlay']}-overlay.dts").read_text(encoding="utf-8")
    for a, b in (
        (f"&{old['gnss_uart']}", f"&{new['gnss_uart']}"),
        (f"<{old['pps_gpio']}>", f"<{new['pps_gpio']}>"),
        (f"&gpio {old['pps_gpio']} 0", f"&gpio {new['pps_gpio']} 0"),
    ):
        expected = expected.replace(a, b)
    actual = (FIRMWARE / f"{new['overlay']}-overlay.dts").read_text(encoding="utf-8")
    assert _code(actual) == _code(expected)


def test_every_overlay_source_has_a_row() -> None:
    sources = {p.name.removesuffix("-overlay.dts") for p in FIRMWARE.glob("*-overlay.dts")}
    assert sources == {row["overlay"] for row in ROWS.values()}


@pytest.mark.parametrize("name", sorted(ROWS))
def test_no_revision_drives_the_x1202_power_loss_line(name: str) -> None:
    row = ROWS[name]
    used = {row["pps_gpio"], row["kline_enable_gpio"], row["kline_pullup_gpio"], *row["gnss_gpios"]}
    assert X1202_PLD_GPIO not in used


# --------------------------------------------------------------------------
# The daemon's reader
# --------------------------------------------------------------------------


def test_the_eeprom_picks_the_revision(tmp_path: Path) -> None:
    rev = resolve_hat_revision("auto", hat_dir=_eeprom(tmp_path, "CANary-HAT rev 2.1", "0x0201"))
    assert (rev.name, rev.source, rev.kline_pullup_gpio, rev.supply_adc) == (
        "2.1",
        "eeprom",
        21,
        "ads8688a",
    )


def test_a_rev_2_0_eeprom_keeps_the_rev_2_0_pins(tmp_path: Path) -> None:
    rev = resolve_hat_revision("auto", hat_dir=_eeprom(tmp_path, "CANary-HAT rev 2.0", "0x0200"))
    assert (rev.name, rev.kline_enable_gpio, rev.kline_pullup_gpio, rev.supply_adc) == (
        "2.0",
        26,
        24,
        "ads1015",
    )


def test_no_eeprom_is_the_unidentified_revision(tmp_path: Path) -> None:
    rev = resolve_hat_revision("auto", hat_dir=tmp_path / "absent")
    assert (rev.name, rev.source) == (TABLE["unidentified"], "unidentified")


def test_another_vendors_hat_is_not_read_as_a_canary_hat(tmp_path: Path) -> None:
    rev = resolve_hat_revision("auto", hat_dir=_eeprom(tmp_path, "Some Other HAT", "0x0201"))
    assert rev.source == "unidentified"


def test_an_unlisted_canary_hat_revision_is_refused(tmp_path: Path) -> None:
    with pytest.raises(HatRevisionError, match="0x0300"):
        resolve_hat_revision("auto", hat_dir=_eeprom(tmp_path, "CANary-HAT rev 3.0", "0x0300"))


def test_the_config_setting_beats_the_eeprom(tmp_path: Path) -> None:
    rev = resolve_hat_revision("2.0", hat_dir=_eeprom(tmp_path, "CANary-HAT rev 2.1", "0x0201"))
    assert (rev.name, rev.source) == ("2.0", "config")


def test_kline_pins_come_from_the_revision_unless_the_config_pins_them() -> None:
    rev21 = resolve_hat_revision("2.1")
    assert kline_pins(KlineConfig(), rev21).pullup == 21
    assert kline_pins(KlineConfig(), resolve_hat_revision("2.0")).pullup == 24
    pinned = kline_pins(KlineConfig(enable_gpio=5, pullup_gpio=7), rev21)
    assert (pinned.enable, pinned.pullup) == (5, 7)


# --------------------------------------------------------------------------
# The bring-up scripts' reader agrees with the daemon's
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("product", "product_ver"),
    [
        ("CANary-HAT rev 2.0", "0x0200"),
        ("CANary-HAT rev 2.1", "0x0201"),
        ("Some Other HAT", "0x0201"),
    ],
)
def test_hat_rev_py_reads_the_eeprom_as_the_daemon_does(
    tmp_path: Path, product: str, product_ver: str
) -> None:
    hat = _eeprom(tmp_path, product, product_ver)
    script, how = _hat_rev().detect(hat_dir=hat)
    daemon = resolve_hat_revision("auto", hat_dir=hat)
    assert script["name"] == daemon.name
    assert how == daemon.source
    assert (script["kline_enable_gpio"], script["kline_pullup_gpio"]) == (
        daemon.kline_enable_gpio,
        daemon.kline_pullup_gpio,
    )


def test_hat_rev_py_refuses_an_unlisted_revision(tmp_path: Path) -> None:
    hat_rev = _hat_rev()
    with pytest.raises(hat_rev.HatRevisionError):
        hat_rev.detect(hat_dir=_eeprom(tmp_path, "CANary-HAT rev 3.0", "0x0300"))


def test_hat_rev_py_shell_lines_name_the_gnss_tty_per_revision(
    capsys: pytest.CaptureFixture[str],
) -> None:
    hat_rev = _hat_rev()
    for name, tty in (("2.0", "/dev/ttyAMA2"), ("2.1", "/dev/ttyAMA4")):
        assert hat_rev.main(["--rev", name, "--shell"]) == 0
        out = capsys.readouterr().out.splitlines()
        assert f"HAT_REV={name}" in out
        assert f"HAT_GNSS_TTY={tty}" in out
    # install_pi.sh passes the EEPROM's product_ver, empty when there is none.
    assert hat_rev.main(["--product-ver", "", "gnss_tty"]) == 0
    assert capsys.readouterr().out.strip() == ROWS[TABLE["unidentified"]]["gnss_tty"]
