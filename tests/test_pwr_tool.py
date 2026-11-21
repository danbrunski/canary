"""The CANary-PWR tool against its firmware's contract (hardware/canary-pwr-firmware).

The C compiler checks the firmware's register struct against src/regs.h; these
tests check that pwr_tool.py reads the same header, decodes it in the right
units, and refuses what must be refused before the Pi touches the MCU's
option bytes or flash. The option-byte dumps are the first board's, read
through its bootloader before and after nBOOT_SEL was cleared (2026-11-20).
"""

from __future__ import annotations

import argparse
import importlib.util
import struct
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from src.paths import REPO_ROOT

FIRMWARE = REPO_ROOT / "hardware" / "canary-pwr-firmware"
MODULE_NAME = "canary_pwr_tool_under_test"

FIRST_BOARD_OPTIONS_BEFORE = (
    "fffffeaa 00000155 ffffffff 00000000 00ff0000 ff00ffff ff00ff3f 00ff00c0 "
    "ff00ff3f 00ff00c0 ffffffff 00000000 ffff0000 0000ffff ffffffff ffffffff "
    "ffffffff ffffffff ffffffff ffffffff ffffffff ffffffff ffffffff ffffffff "
    "ffffffff ffffffff ffffffff ffffffff fffeff80 0001007f ffffffff ffffffff"
)
FIRST_BOARD_OPTIONS_AFTER = (
    "fefffeaa 01000155 0000007f ffffff80 00000000 ffffffff 0000001f ffffffe0 "
    "0000001f ffffffe0 0000007f ffffff80 00000000 ffffffff ffffffff ffffffff "
    "ffffffff ffffffff ffffffff ffffffff ffffffff ffffffff ffffffff ffffffff "
    "ffffffff ffffffff ffffffff ffffffff 00000000 ffffffff ffffffff ffffffff"
)


def _words(text: str) -> bytes:
    return b"".join(struct.pack("<I", int(word, 16)) for word in text.split())


@pytest.fixture(scope="module")
def tool() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, FIRMWARE / "pwr_tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module  # dataclasses resolve annotations through it
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(MODULE_NAME, None)


def _pack(tool: ModuleType, regmap: Any, **values: Any) -> bytes:
    raw = bytearray(regmap.size)
    defaults: dict[str, Any] = {"magic": b"CPWR", "ltc": [0] * regmap.consts["PWR_LTC_COUNT"]}
    for field in regmap.fields:
        value = values.get(field.name, defaults.get(field.name, 0))
        if isinstance(value, list):
            struct.pack_into("<" + field.fmt, raw, field.offset, *value)
        else:
            struct.pack_into("<" + field.fmt, raw, field.offset, value)
    return bytes(raw)


def test_the_register_map_is_the_firmwares(tool: ModuleType) -> None:
    regmap = tool.load_register_map()
    assert regmap.size == 0x68
    assert regmap.consts["PWR_I2C_ADDR"] == 0x2E
    by_name = {field.name: field for field in regmap.fields}
    assert by_name["magic"].fmt == "4s"
    assert by_name["ina_shunt"].fmt == "h"
    assert by_name["ina_samples"].offset % 4 == 0
    assert by_name["ltc"].fmt == f"{regmap.consts['PWR_LTC_COUNT']}H"
    assert len(regmap.consts["PWR_LTC_REGS"]) == regmap.consts["PWR_LTC_COUNT"]


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("PWR_REG_UPTIME_MS      0x0C", "PWR_REG_UPTIME_MS      0x0E", "gap or overlap|aligned"),
        ("0x22 /* s16", "0x22 /* int16", "must start with a type"),
        (
            "#define PWR_REG_SIZE           0x68",
            "#define PWR_REG_SIZE           0x6A",
            "PWR_REG_SIZE",
        ),
        ("0x29, 0x2A }", "0x29, 0x2B }", "without a name"),
    ],
)
def test_a_header_the_struct_could_not_match_is_refused(
    tool: ModuleType, tmp_path: Path, old: str, new: str, message: str
) -> None:
    text = (FIRMWARE / "src" / "regs.h").read_text()
    assert old in text
    header = tmp_path / "regs.h"
    header.write_text(text.replace(old, new))
    with pytest.raises(ValueError, match=message):
        tool.load_register_map(header)


def test_registers_decode_to_engineering_units(tool: ModuleType) -> None:
    regmap = tool.load_register_map()
    ltc_regs = regmap.consts["PWR_LTC_REGS"]
    ltc = [0] * len(ltc_regs)
    ltc[ltc_regs.index(0x20)] = 13600  # meas_vcap1, 183.5 uV/LSB
    ltc[ltc_regs.index(0x2A)] = 10536  # meas_dtemp, 0.028 x - 251.4
    ltc[ltc_regs.index(0x28)] = 1600  # meas_iin, 1.983 uV / 8 mOhm
    ltc[ltc_regs.index(0x1A)] = 3  # num_caps: 0..3 means 1..4
    ltc[ltc_regs.index(0x1B)] = 0b10_0010_0101  # stepdown, cv, cappg, ci
    raw = _pack(
        tool,
        regmap,
        fw_major=0,
        fw_minor=1,
        board_major=1,
        ina_bus=4056,
        ina_bus_min=4048,
        ina_bus_max=4082,
        ina_shunt=1318,
        ina_shunt_max=1954,
        ina_samples=184,
        vrefint_cal=1655,
        adc_vrefint=1505,
        adc_vin=1975,
        adc_vin_min=1972,
        adc_vin_max=1980,
        adc_pi3v3=4095,
        inputs=1 << regmap.consts["PWR_IN_BUCK_PG"],
        devices=0b01,
        ltc=ltc,
    )
    summary = tool.summarize(regmap.decode(raw), regmap)
    assert summary["firmware"] == "0.1"
    assert summary["pi_5v"]["volts"] == pytest.approx(5.070)
    assert summary["pi_5v"]["amps"] == pytest.approx(0.659)
    assert summary["pi_5v"]["max_amps"] == pytest.approx(0.977)
    vdda = 3.0 * 1655 / 1505
    assert summary["mcu_adc"]["vdda"] == pytest.approx(vdda)
    assert summary["mcu_adc"]["vin"] == pytest.approx(1975 / 4095 * vdda * 1150 / 150)
    assert summary["ltc3350"]["meas_vcap1"] == pytest.approx(2.4956, abs=1e-4)
    assert summary["ltc3350"]["meas_dtemp"] == pytest.approx(43.608, abs=1e-3)
    assert summary["ltc3350"]["meas_iin"] == pytest.approx(0.3966, abs=1e-4)
    assert summary["ltc3350"]["num_caps"] == 4
    assert summary["ltc3350"]["chrg_status_bits"] == ["stepdown", "cv", "cappg", "ci"]
    assert summary["inputs"]["buck_pg"] is True
    assert summary["inputs"]["pi_5v_off"] is False
    assert summary["devices"] == {"ina226": True, "ltc3350": False}
    assert "Pi 5 V: 5.070 V" in tool.render(summary)


def test_untouched_trackers_report_nothing_rather_than_sentinels(tool: ModuleType) -> None:
    regmap = tool.load_register_map()
    raw = _pack(tool, regmap, ina_bus_min=0xFFFF, ina_shunt_max=-32768, adc_vin_min=0xFFFF)
    summary = tool.summarize(regmap.decode(raw), regmap)
    assert summary["pi_5v"]["min_volts"] is None
    assert summary["pi_5v"]["max_amps"] is None
    assert summary["mcu_adc"]["vin_min"] is None
    assert summary["mcu_adc"]["vdda"] is None


def test_boot_sel_changes_one_bit_and_never_touches_protection(tool: ModuleType) -> None:
    assert tool.boot_sel_plan(0xFFFFFEAA) == 0xFEFFFEAA
    decoded = tool.decode_optr(0xFEFFFEAA)
    assert decoded["rdp_level"] == 0 and decoded["boot0_pin_honoured"]
    for rdp in (0xBB, 0xCC):
        with pytest.raises(tool.ToolError, match="readout protection"):
            tool.boot_sel_plan(0xFFFFFE00 | rdp)
    with pytest.raises(tool.ToolError, match="already 0"):
        tool.boot_sel_plan(0xFEFFFEAA)
    assert tool.decode_optr(0xFFFFFECC)["rdp_level"] == 2


def test_the_first_boards_option_bytes_kept_every_setting_but_nboot_sel(tool: ModuleType) -> None:
    before = tool.option_settings(_words(FIRST_BOARD_OPTIONS_BEFORE))
    after = tool.option_settings(_words(FIRST_BOARD_OPTIONS_AFTER))
    assert tool.option_changes(before, after) == {"OPTR": (0xFFFFFEAA, 0xFEFFFEAA)}
    assert after["SECR"] == 0  # no securable area, BOOT_LOCK clear
    assert after["WRP1AR"] == 0x1F  # start 31 > end 0: no write protection


def test_an_option_word_its_complement_contradicts_is_refused(tool: ModuleType) -> None:
    area = bytearray(_words(FIRST_BOARD_OPTIONS_AFTER))
    area[0x18] ^= 0x01  # WRP1AR
    with pytest.raises(tool.ToolError, match="WRP1AR"):
        tool.option_settings(bytes(area))


def _image(tool: ModuleType, *, sp: int = 0x20002000, entry: int = 0x0800010D) -> bytes:
    image = bytearray(b"\xff" * 0x400)
    struct.pack_into("<II", image, 0, sp, entry)
    image[tool.FW_TAG_OFFSET : tool.FW_TAG_OFFSET + 16] = tool.FW_TAG.ljust(16, b"\0")
    return bytes(image)


def test_only_a_canary_pwr_firmware_image_may_be_flashed(tool: ModuleType) -> None:
    tool.check_firmware_image(_image(tool))
    with pytest.raises(tool.ToolError, match="reset vector"):
        tool.check_firmware_image(_image(tool, entry=0x20001045))  # a RAM helper
    with pytest.raises(tool.ToolError, match="Thumb"):
        tool.check_firmware_image(_image(tool, entry=0x0800010C))
    with pytest.raises(tool.ToolError, match="stack pointer"):
        tool.check_firmware_image(_image(tool, sp=0x08000000))
    untagged = bytearray(_image(tool))
    untagged[tool.FW_TAG_OFFSET] = 0
    with pytest.raises(tool.ToolError, match="tag"):
        tool.check_firmware_image(bytes(untagged))
    with pytest.raises(tool.ToolError, match="flash is"):
        tool.check_firmware_image(_image(tool) + b"\xff" * tool.FLASH_SIZE)


def test_nothing_resets_the_mcu_during_a_capture(tool: ModuleType) -> None:
    def capturing() -> dict[str, Any]:
        return {"capture_profile": {"active": True, "name": "tuning", "session_dir": "_capture-x"}}

    with pytest.raises(tool.ToolError, match="capture 'tuning' is recording"):
        tool.refuse_while_capturing(False, fetch=capturing)
    tool.refuse_while_capturing(True, fetch=capturing)
    tool.refuse_while_capturing(False, fetch=lambda: None)
    tool.refuse_while_capturing(False, fetch=lambda: {"capture_profile": {"active": False}})


def test_the_ram_helper_layout_comes_from_its_header(tool: ModuleType) -> None:
    layout = tool.load_helper_layout()
    assert layout.load == 0x20001000
    assert layout.status_size == 4 * layout.consts["HELPER_STATUS_WORDS"]
    assert layout.status + layout.status_size <= 0x20002000 - 256  # room for its stack
    assert layout.step_name(layout.consts["HELPER_STEP_LAUNCHING"]) == "launching"
    words = list(range(layout.status_size // 4))
    decoded = layout.decode(struct.pack(f"<{len(words)}I", *words))
    assert decoded["magic"] == 0 and decoded["optr_idle"] == layout.words["optr_idle"]


def _bench_board(tool: ModuleType, options: bytes, flash: bytes = b"\xff" * 8) -> Any:
    """A Board whose pins, bootloader and memory are a script, not hardware."""

    class Bench(tool.Board):  # type: ignore[misc,name-defined]
        def __init__(self) -> None:
            super().__init__(tool.load_register_map())
            self.calls: list[tuple[str, ...]] = []

        def _pinctrl(self, gpio: int, *args: str) -> None:
            self.calls.append(("pinctrl", str(gpio), *args))

        def reset_pulse(self) -> None:
            self.calls.append(("reset",))

        def bootloader_info(self) -> str:
            return "Device ID    : 0x0466"

        def read_memory(self, address: int, size: int) -> bytes:
            return (options if address == tool.OPTION_BYTES else flash)[:size]

        def stm32flash(self, *args: str, check: bool = True) -> str:
            self.calls.append(("stm32flash", *args))
            return ""

    return Bench()


def test_flash_refuses_while_boot0_would_be_ignored(tool: ModuleType, tmp_path: Path) -> None:
    image = tmp_path / "pwr.bin"
    image.write_bytes(_image(tool))
    board = _bench_board(tool, _words(FIRST_BOARD_OPTIONS_BEFORE))
    args = argparse.Namespace(image=str(image), without_boot0=False)
    with pytest.raises(tool.ToolError, match="boot-sel"):
        tool.cmd_flash(board, args)
    assert not [call for call in board.calls if call[0] == "stm32flash" and "-w" in call]


def test_boot_sel_refuses_a_protected_chip_before_loading_anything(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool, "BUILD", tmp_path)
    (tmp_path / "ram-boot-sel.bin").write_bytes(b"\0" * 8)
    area = bytearray(_words(FIRST_BOARD_OPTIONS_BEFORE))
    struct.pack_into("<II", area, 0, 0xFFFFFEBB, 0x00000144)  # RDP level 1
    board = _bench_board(tool, bytes(area))
    with pytest.raises(tool.ToolError, match="readout protection"):
        tool.cmd_boot_sel(board, argparse.Namespace(yes=True))
    assert not [call for call in board.calls if call[0] == "stm32flash"]
