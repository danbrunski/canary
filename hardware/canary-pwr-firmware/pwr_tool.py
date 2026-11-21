#!/usr/bin/env python3
"""CANary-PWR supervisor from the Pi: status, bootloader, option bytes, flashing.

Standard library only, run from this directory's checkout on the Pi (it reads
src/regs.h and ram/helper.h, so the register map and the RAM helper's layout
have one copy each). The STM32 I2C bootloader protocol (AN4221) is spoken by
stm32flash; this script drives the two reflash lines around it (PWR spec §P6):

    GPIO27  PI_BOOT0  high = the MCU starts its ST bootloader at the next reset
    GPIO24  PI_NRST   high = the MCU is held in reset (through a BSS138)

The Pi stays powered throughout: in reset or in the bootloader the MCU's pins
float and the pull-downs keep the 5 V buck on (§P7 default-on).

    python3 pwr_tool.py status              read the running firmware
    python3 pwr_tool.py info                bootloader, option bytes, flash state
    python3 pwr_tool.py probe-pins          prove GPIO27 -> BOOT0 and GPIO24 -> NRST
    python3 pwr_tool.py boot-sel --yes      clear nBOOT_SEL (once per board)
    python3 pwr_tool.py flash [IMAGE]       write build/pwr.bin, verify, start it
    python3 pwr_tool.py check-reentry       both ways back into the bootloader
    python3 pwr_tool.py run | bootloader    leave it running either side

Anything that resets the MCU refuses while a capture is recording, like
scripts/restart_canary.sh (--force-capture overrides).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REGS_H = HERE / "src" / "regs.h"
HELPER_H = HERE / "ram" / "helper.h"
BUILD = HERE / "build"

I2C_BUS = "/dev/i2c-1"
I2C_SLAVE = 0x0703  # linux/i2c-dev.h
BOOTLOADER_ADDR = 0x56  # AN2606 §47
GPIO_BOOT0 = 27
GPIO_NRST = 24
STATUS_URL = "http://localhost:8000/status"

FLASH_BASE = 0x08000000
FLASH_SIZE = 64 * 1024
FLASH_PAGE = 2048
RAM_BASE = 0x20000000
RAM_TOP = 0x20002000
OPTION_BYTES = 0x1FFF7800
OPTION_BYTES_SIZE = 0x80
FW_TAG_OFFSET = 0xC0  # stm32g031k8.ld puts .fw_tag right after the 48 vectors
FW_TAG = b"CANary-PWR fw"

OPTR_NBOOT_SEL = 1 << 24
OPTR_NBOOT1 = 1 << 25
OPTR_NBOOT0 = 1 << 26
OPTR_IWDG_SW = 1 << 16
RDP_LEVEL0 = 0xAA
RDP_LEVEL2 = 0xCC  # permanent: nothing here may ever write it

# The FLASH option registers in the option-byte area, each stored as a value
# word and its complement, with the bits the STM32G031 implements (masks from
# ST's stm32g031xx.h). OPTSTRT rewrites every word from the registers, which
# read reserved bits as 0, so a factory 0xFF00FF3F becomes 0x0000001F with
# the same settings (seen on the first board, 2026-11-20): compare these bits.
OPTION_REGISTERS: dict[int, tuple[str, int]] = {
    0x00: ("OPTR", 0xFFFFFFFF),
    0x08: ("PCROP1ASR", 0x0000007F),
    0x10: ("PCROP1AER", 0x8000007F),
    0x18: ("WRP1AR", 0x001F001F),
    0x20: ("WRP1BR", 0x001F001F),
    0x28: ("PCROP1BSR", 0x0000007F),
    0x30: ("PCROP1BER", 0x0000007F),
    0x70: ("SECR", 0x0001003F),
}

RESET_FLAGS = {  # RCC_CSR
    25: "option-byte load",
    26: "pin",
    27: "power-on/brown-out",
    28: "software",
    29: "independent watchdog",
    30: "window watchdog",
    31: "low-power",
}

# Board facts behind the scales (CANary-PWR rev 1.0 schematic, PWR spec §P4/§P5).
INA_BUS_LSB_V = 1.25e-3  # INA226 datasheet
INA_SHUNT_LSB_V = 2.5e-6
R28_SHUNT_OHM = 0.005
VIN_DIVIDER = (1_000_000 + 150_000) / 150_000  # R3 / R4
VREFINT_CAL_VDDA = 3.0
ADC_FULL_SCALE = 4095
RSNS_OHM = 0.008  # RSNSI and RSNSC

# LTC3350 (datasheet Rev D, register map p. 32 and descriptions p. 33-38).
LTC_NAMES = {
    0x05: "vcapfb_dac",
    0x06: "vshunt",
    0x17: "ctl_reg",
    0x1A: "num_caps",
    0x1B: "chrg_status",
    0x1C: "mon_status",
    0x1D: "alarm_reg",
    0x1E: "meas_cap",
    0x1F: "meas_esr",
    0x20: "meas_vcap1",
    0x21: "meas_vcap2",
    0x22: "meas_vcap3",
    0x23: "meas_vcap4",
    0x24: "meas_gpi",
    0x25: "meas_vin",
    0x26: "meas_vcap",
    0x27: "meas_vout",
    0x28: "meas_iin",
    0x29: "meas_ichg",
    0x2A: "meas_dtemp",
}
_CELL_V = 183.5e-6
LTC_SCALES: dict[str, tuple[float, float, str]] = {
    "vshunt": (_CELL_V, 0.0, "V"),
    "meas_vcap1": (_CELL_V, 0.0, "V"),
    "meas_vcap2": (_CELL_V, 0.0, "V"),
    "meas_vcap3": (_CELL_V, 0.0, "V"),
    "meas_vcap4": (_CELL_V, 0.0, "V"),
    "meas_gpi": (_CELL_V, 0.0, "V"),
    "meas_vin": (2.21e-3, 0.0, "V"),
    "meas_vcap": (1.476e-3, 0.0, "V"),
    "meas_vout": (2.21e-3, 0.0, "V"),
    "meas_iin": (1.983e-6 / RSNS_OHM, 0.0, "A"),
    "meas_ichg": (1.983e-6 / RSNS_OHM, 0.0, "A"),
    "meas_dtemp": (0.028, -251.4, "degC"),
}
CHRG_STATUS_BITS = {
    0: "stepdown",
    1: "stepup",
    2: "cv",
    3: "uvlo",
    4: "input_ilim",
    5: "cappg",
    6: "shnt",
    7: "bal",
    8: "dis",
    9: "ci",
    11: "pfo",
}
MON_STATUS_BITS = {
    0: "capesr_active",
    1: "capesr_scheduled",
    2: "capesr_pending",
    3: "cap_done",
    4: "esr_done",
    5: "cap_failed",
    6: "esr_failed",
    8: "power_failed",
    9: "power_returned",
}


class ToolError(Exception):
    """A refusal or failure to report to the operator, without a traceback."""


# ---- headers -----------------------------------------------------------------

_DEFINE = re.compile(r"#define\s+(\w+)\s+(.*?)\s*(?:/\*(.*?)\*/)?\s*$")


def _define_value(raw: str) -> Any:
    if raw.startswith("{"):
        return tuple(int(x, 0) for x in raw.strip("{} ").split(",") if x.strip())
    if raw.startswith('"'):
        return raw.strip('"')
    try:
        return int(raw.rstrip("uU"), 0)
    except ValueError:
        return raw


def parse_defines(path: Path) -> dict[str, tuple[Any, str]]:
    """Every valued ``#define NAME value /* comment */`` in a C header."""
    defines: dict[str, tuple[Any, str]] = {}
    for line in path.read_text().replace("\\\n", " ").splitlines():
        match = _DEFINE.match(line.strip())
        if match is None or not match.group(2):
            continue
        defines[match.group(1)] = (_define_value(match.group(2)), (match.group(3) or "").strip())
    return defines


_TYPES = {"char4": "4s", "u8": "B", "u16": "H", "s16": "h", "u32": "I"}
_NOT_FIELDS = {"PWR_REG_SIZE", "PWR_REG_COMMAND"}


@dataclass(frozen=True)
class Field:
    name: str
    offset: int
    fmt: str

    @property
    def size(self) -> int:
        return struct.calcsize("<" + self.fmt)

    @property
    def align(self) -> int:
        element = self.fmt[-1]
        return 1 if element in "sB" else struct.calcsize("<" + element)


@dataclass(frozen=True)
class RegisterMap:
    consts: dict[str, Any]
    fields: tuple[Field, ...]
    size: int

    def decode(self, raw: bytes) -> dict[str, Any]:
        if len(raw) < self.size:
            raise ToolError(f"register read returned {len(raw)} bytes, expected {self.size}")
        values: dict[str, Any] = {}
        for field in self.fields:
            unpacked = struct.unpack_from("<" + field.fmt, raw, field.offset)
            if field.fmt.endswith("s"):
                values[field.name] = unpacked[0].decode("ascii", "replace")
            elif len(unpacked) == 1:
                values[field.name] = unpacked[0]
            else:
                values[field.name] = list(unpacked)
        return values

    def bits(self, prefix: str, value: int) -> dict[str, bool]:
        return {
            name.removeprefix(prefix).lower(): bool(value >> bit & 1)
            for name, bit in self.consts.items()
            if name.startswith(prefix) and isinstance(bit, int)
        }


def _field_format(token: str, consts: dict[str, Any], name: str) -> str:
    array = re.fullmatch(r"(\w+)\[(\w+)\]", token)
    if array is not None and array.group(1) in _TYPES:
        count = consts.get(array.group(2))
        if not isinstance(count, int):
            raise ValueError(f"{name}: array length {array.group(2)} is not defined")
        return f"{count}{_TYPES[array.group(1)]}"
    if token in _TYPES:
        return _TYPES[token]
    raise ValueError(
        f"{name}: its comment must start with a type ({', '.join(_TYPES)}), got {token!r}"
    )


def load_register_map(path: Path = REGS_H) -> RegisterMap:
    """regs.h as fields, refusing a map the firmware's struct could not match."""
    defines = parse_defines(path)
    consts = {name: value for name, (value, _) in defines.items()}
    fields = []
    for name, (offset, comment) in defines.items():
        if not name.startswith("PWR_REG_") or name in _NOT_FIELDS:
            continue
        token = comment.split()[0] if comment else ""
        fields.append(
            Field(name.removeprefix("PWR_REG_").lower(), offset, _field_format(token, consts, name))
        )
    fields.sort(key=lambda f: f.offset)
    size = consts.get("PWR_REG_SIZE")
    if not isinstance(size, int):
        raise ValueError("PWR_REG_SIZE is not defined")
    end = 0
    for field in fields:
        if field.offset != end:
            raise ValueError(
                f"{field.name} at 0x{field.offset:02X}: expected 0x{end:02X} (gap or overlap)"
            )
        if field.offset % field.align:
            raise ValueError(
                f"{field.name} at 0x{field.offset:02X} is not {field.align}-byte aligned"
            )
        end = field.offset + field.size
    if end != size:
        raise ValueError(f"fields end at 0x{end:02X}, PWR_REG_SIZE is 0x{size:02X}")
    ltc = consts.get("PWR_LTC_REGS")
    if not isinstance(ltc, tuple) or len(ltc) != consts.get("PWR_LTC_COUNT"):
        raise ValueError("PWR_LTC_REGS must list PWR_LTC_COUNT registers")
    unnamed = [reg for reg in ltc if reg not in LTC_NAMES]
    if unnamed:
        raise ValueError(f"LTC3350 registers without a name here: {unnamed}")
    return RegisterMap(consts, tuple(fields), size)


@dataclass(frozen=True)
class HelperLayout:
    consts: dict[str, Any]
    words: dict[str, int]
    steps: dict[int, str]

    @property
    def load(self) -> int:
        return int(self.consts["HELPER_LOAD"])

    @property
    def status(self) -> int:
        return int(self.consts["HELPER_STATUS"])

    @property
    def status_size(self) -> int:
        return 4 * int(self.consts["HELPER_STATUS_WORDS"])

    def decode(self, raw: bytes) -> dict[str, int]:
        words = struct.unpack_from(f"<{self.status_size // 4}I", raw)
        return {name: words[index] for name, index in self.words.items()}

    def step_name(self, code: int) -> str:
        return self.steps.get(code, f"0x{code:02X}")


def load_helper_layout(path: Path = HELPER_H) -> HelperLayout:
    consts = {name: value for name, (value, _) in parse_defines(path).items()}
    words = {
        name.removeprefix("HS_").lower(): value
        for name, value in consts.items()
        if name.startswith("HS_") and isinstance(value, int)
    }
    steps = {
        value: name.removeprefix("HELPER_STEP_").lower()
        for name, value in consts.items()
        if name.startswith("HELPER_STEP_") and isinstance(value, int)
    }
    count = consts.get("HELPER_STATUS_WORDS")
    if sorted(words.values()) != list(range(count if isinstance(count, int) else -1)):
        raise ValueError("HS_* must number the status words 0..HELPER_STATUS_WORDS-1")
    return HelperLayout(consts, words, steps)


# ---- decoding ----------------------------------------------------------------


def decode_optr(optr: int) -> dict[str, Any]:
    rdp = optr & 0xFF
    return {
        "optr": f"0x{optr:08X}",
        "rdp": f"0x{rdp:02X}",
        "rdp_level": 0 if rdp == RDP_LEVEL0 else 2 if rdp == RDP_LEVEL2 else 1,
        "nboot_sel": int(bool(optr & OPTR_NBOOT_SEL)),
        "nboot1": int(bool(optr & OPTR_NBOOT1)),
        "nboot0": int(bool(optr & OPTR_NBOOT0)),
        "iwdg_sw": int(bool(optr & OPTR_IWDG_SW)),
        "boot0_pin_honoured": not optr & OPTR_NBOOT_SEL,
    }


def option_settings(area: bytes) -> dict[str, int]:
    """Each option register's implemented bits, refusing a word its complement contradicts."""
    settings: dict[str, int] = {}
    for offset, (name, mask) in OPTION_REGISTERS.items():
        value, complement = struct.unpack_from("<II", area, offset)
        if value ^ complement != 0xFFFFFFFF:
            raise ToolError(
                f"option bytes: {name} reads 0x{value:08X} with complement 0x{complement:08X}"
            )
        settings[name] = value & mask
    return settings


def option_changes(before: dict[str, int], after: dict[str, int]) -> dict[str, tuple[int, int]]:
    return {name: (before[name], after[name]) for name in before if before[name] != after[name]}


def boot_sel_plan(optr: int) -> int:
    """The only change boot-sel makes: nBOOT_SEL 1 -> 0, RDP held at level 0."""
    if optr & 0xFF != RDP_LEVEL0:
        raise ToolError(
            f"readout protection is 0x{optr & 0xFF:02X}, not 0xAA (level 0): "
            "refusing to touch the option bytes"
        )
    if not optr & OPTR_NBOOT_SEL:
        raise ToolError("nBOOT_SEL is already 0: the BOOT0 pin is honoured")
    return optr & ~OPTR_NBOOT_SEL


def check_firmware_image(image: bytes) -> None:
    """Refuse anything but this firmware, linked for this part's flash."""
    if not 0 < len(image) <= FLASH_SIZE:
        raise ToolError(f"image is {len(image)} bytes; flash is {FLASH_SIZE}")
    sp, entry = struct.unpack_from("<II", image)
    if not RAM_BASE < sp <= RAM_TOP:
        raise ToolError(f"initial stack pointer 0x{sp:08X} is not in SRAM")
    if not (FLASH_BASE <= entry < FLASH_BASE + len(image) and entry & 1):
        raise ToolError(f"reset vector 0x{entry:08X} is not a Thumb address inside the image")
    if image[FW_TAG_OFFSET : FW_TAG_OFFSET + len(FW_TAG)] != FW_TAG:
        raise ToolError(
            f"no {FW_TAG.decode()!r} tag at offset 0x{FW_TAG_OFFSET:X}: "
            "not a CANary-PWR firmware image"
        )


def _set_bits(value: int, names: dict[int, str]) -> list[str]:
    return [name for bit, name in names.items() if value >> bit & 1]


def summarize(values: dict[str, Any], regmap: RegisterMap) -> dict[str, Any]:
    """Raw registers to engineering units. Each scale is cited where defined."""
    vrefint = values["adc_vrefint"]
    vdda = VREFINT_CAL_VDDA * values["vrefint_cal"] / vrefint if vrefint else None

    def adc_volts(raw: int, gain: float = 1.0) -> float | None:
        return None if vdda is None else raw / ADC_FULL_SCALE * vdda * gain

    sampled = values["ina_samples"] > 0
    ltc_regs = regmap.consts["PWR_LTC_REGS"]
    ltc_raw = {LTC_NAMES[reg]: raw for reg, raw in zip(ltc_regs, values["ltc"], strict=True)}
    ltc: dict[str, Any] = {}
    for name, raw in ltc_raw.items():
        if name in LTC_SCALES:
            lsb, offset, _unit = LTC_SCALES[name]
            ltc[name] = round(raw * lsb + offset, 4)
        else:
            ltc[name] = raw
    ltc["num_caps"] = ltc_raw["num_caps"] + 1
    ltc["chrg_status_bits"] = _set_bits(ltc_raw["chrg_status"], CHRG_STATUS_BITS)
    ltc["mon_status_bits"] = _set_bits(ltc_raw["mon_status"], MON_STATUS_BITS)
    return {
        "firmware": f"{values['fw_major']}.{values['fw_minor']}",
        "board": f"{values['board_major']}.{values['board_minor']}",
        "build_id": f"{values['build_id']:08x}",
        "uptime_s": values["uptime_ms"] / 1000,
        "loop_hz": values["loop_hz"],
        "reset_causes": [
            name for bit, name in RESET_FLAGS.items() if values["reset_flags"] >> bit & 1
        ],
        "option_bytes": decode_optr(values["optr"]),
        "devices": regmap.bits("PWR_DEV_", values["devices"]),
        "private_bus_errors": values["bus_errors"],
        "inputs": regmap.bits("PWR_IN_", values["inputs"]),
        "button_presses": values["button_presses"],
        "pi_5v": {
            "volts": values["ina_bus"] * INA_BUS_LSB_V,
            "min_volts": values["ina_bus_min"] * INA_BUS_LSB_V if sampled else None,
            "max_volts": values["ina_bus_max"] * INA_BUS_LSB_V if sampled else None,
            "amps": values["ina_shunt"] * INA_SHUNT_LSB_V / R28_SHUNT_OHM,
            "max_amps": values["ina_shunt_max"] * INA_SHUNT_LSB_V / R28_SHUNT_OHM
            if sampled
            else None,
            "samples": values["ina_samples"],
            "ina226_id": f"0x{values['ina_id']:04X}",
        },
        "mcu_adc": {
            "vdda": vdda,
            "vin": adc_volts(values["adc_vin"], VIN_DIVIDER),
            "vin_min": adc_volts(values["adc_vin_min"], VIN_DIVIDER)
            if values["adc_vin_min"] <= ADC_FULL_SCALE
            else None,
            "vin_max": adc_volts(values["adc_vin_max"], VIN_DIVIDER),
            "pi_3v3": adc_volts(values["adc_pi3v3"]),
        },
        "ltc3350": ltc,
        "raw": values,
    }


def _fmt(value: float | None, unit: str, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f} {unit}"


def render(summary: dict[str, Any]) -> str:
    pi, adc = summary["pi_5v"], summary["mcu_adc"]
    ltc, ob = summary["ltc3350"], summary["option_bytes"]
    devices = ", ".join(
        f"{name} {'ok' if ok else 'NOT ANSWERING'}" for name, ok in summary["devices"].items()
    )
    inputs = " ".join(f"{name}={int(level)}" for name, level in summary["inputs"].items())
    cells = " ".join(f"{ltc[f'meas_vcap{i}']:.3f}" for i in range(1, 5))
    boot0 = "honoured" if ob["boot0_pin_honoured"] else "IGNORED"
    resets = ", ".join(summary["reset_causes"]) or "none recorded"
    charger = " ".join(ltc["chrg_status_bits"]) or "-"
    monitor = " ".join(ltc["mon_status_bits"]) or "-"
    lines = [
        f"CANary-PWR firmware {summary['firmware']} (build {summary['build_id']}), "
        f"board {summary['board']}, up {summary['uptime_s']:.1f} s, "
        f"main loop {summary['loop_hz']} Hz",
        f"last reset: {resets}; OPTR {ob['optr']} (RDP level {ob['rdp_level']}, BOOT0 pin {boot0})",
        f"private bus: {devices}; {summary['private_bus_errors']} failed transfers",
        f"Pi 5 V: {_fmt(pi['volts'], 'V')} (min {_fmt(pi['min_volts'], 'V')}, "
        f"max {_fmt(pi['max_volts'], 'V')}, {pi['samples']} samples); "
        f"{_fmt(pi['amps'], 'A')} (peak {_fmt(pi['max_amps'], 'A')})",
        f"MCU ADC: VIN {_fmt(adc['vin'], 'V', 2)} (min {_fmt(adc['vin_min'], 'V', 2)}, "
        f"max {_fmt(adc['vin_max'], 'V', 2)}), Pi 3V3 {_fmt(adc['pi_3v3'], 'V', 2)}, "
        f"VDDA {_fmt(adc['vdda'], 'V', 3)}",
        f"LTC3350: VIN {ltc['meas_vin']:.2f} V, VOUT {ltc['meas_vout']:.2f} V, "
        f"VCAP {ltc['meas_vcap']:.3f} V [cells {cells}], IIN {ltc['meas_iin']:.3f} A, "
        f"ICHG {ltc['meas_ichg']:.3f} A, die {ltc['meas_dtemp']:.1f} °C, "
        f"{ltc['num_caps']} caps configured",
        f"         charger: {charger}; monitor: {monitor}; "
        f"alarms 0x{ltc['alarm_reg']:04X}; vshunt {ltc['vshunt']:.3f} V",
        f"inputs: {inputs}; button presses {summary['button_presses']}",
    ]
    return "\n".join(lines)


# ---- the board -----------------------------------------------------------------

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class Board:
    """The Pi's view of the supervisor: two GPIOs, I2C1 and stm32flash."""

    def __init__(
        self, regmap: RegisterMap, bus: str = I2C_BUS, run: Runner = subprocess.run
    ) -> None:
        self.regmap = regmap
        self.bus = bus
        self.run = run
        self.addr = int(regmap.consts["PWR_I2C_ADDR"])
        self.pins_touched = False

    # GPIO
    def _pinctrl(self, gpio: int, *args: str) -> None:
        self.pins_touched = True
        self.run(["pinctrl", "set", str(gpio), *args], check=True, capture_output=True, text=True)

    def boot0(self, high: bool) -> None:
        self._pinctrl(GPIO_BOOT0, "op", "dh" if high else "dl")

    def reset_pulse(self) -> None:
        self._pinctrl(GPIO_NRST, "op", "dh")
        time.sleep(0.02)
        self._pinctrl(GPIO_NRST, "op", "dl")
        time.sleep(0.2)

    def release(self) -> None:
        """Both lines back to inputs; their pull-downs hold BOOT0 low and NRST free."""
        if self.pins_touched:
            self._pinctrl(GPIO_NRST, "ip", "pd")
            self._pinctrl(GPIO_BOOT0, "ip", "pd")

    # the bootloader, through stm32flash
    def stm32flash(self, *args: str, check: bool = True) -> str:
        cmd = ["stm32flash", "-a", f"0x{BOOTLOADER_ADDR:02x}", *args, self.bus]
        proc = self.run(cmd, capture_output=True, text=True)
        out = (proc.stdout or "") + (proc.stderr or "")
        if check and proc.returncode != 0:
            raise ToolError(f"{' '.join(cmd)} failed (exit {proc.returncode}):\n{out.strip()}")
        return out

    def bootloader_info(self) -> str | None:
        out = self.stm32flash(check=False)
        return out if "Device ID" in out else None

    def read_memory(self, address: int, size: int) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "read.bin"
            self.stm32flash("-r", str(path), "-S", f"0x{address:08x}:{size}")
            data = path.read_bytes()
        if len(data) != size:
            raise ToolError(f"read {len(data)} of {size} bytes at 0x{address:08X}")
        return data

    def run_from_ram(self, image: Path, load: int) -> None:
        # stm32flash erases nothing for a RAM address (main.c: no_erase).
        self.stm32flash("-w", str(image), "-v", "-S", f"0x{load:08x}", "-g", f"0x{load:08x}")

    # the firmware, on I2C1
    def _i2c(self, action: Callable[[int], bytes | None]) -> bytes | None:
        try:
            fd = os.open(self.bus, os.O_RDWR)
        except OSError as exc:
            raise ToolError(f"cannot open {self.bus}: {exc}") from exc
        try:
            fcntl.ioctl(fd, I2C_SLAVE, self.addr)
            return action(fd)
        except OSError:
            return None
        finally:
            os.close(fd)

    def app_read(self, reg: int, size: int) -> bytes | None:
        def read(fd: int) -> bytes | None:
            os.write(fd, bytes([reg]))
            data = os.read(fd, size)
            return data if len(data) == size else None

        return self._i2c(read)

    def app_write(self, data: bytes) -> bool:
        return self._i2c(lambda fd: b"ok" if os.write(fd, data) == len(data) else None) is not None

    def app_registers(self) -> dict[str, Any] | None:
        raw = self.app_read(0, self.regmap.size)
        if raw is None:
            return None
        values = self.regmap.decode(raw)
        return values if values["magic"] == "CPWR" else None

    def wait_app(self, seconds: float = 1.5) -> dict[str, Any] | None:
        deadline = time.monotonic() + seconds
        while True:
            values = self.app_registers()
            if values is not None or time.monotonic() > deadline:
                return values
            time.sleep(0.1)


# ---- steps -------------------------------------------------------------------------


def fetch_daemon_status(url: str = STATUS_URL) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            loaded = json.load(response)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def refuse_while_capturing(
    force: bool, fetch: Callable[[], dict[str, Any] | None] = fetch_daemon_status
) -> None:
    status = fetch()
    if status is None:
        return  # no daemon answering: nothing is recording through it
    profile = status.get("capture_profile") or {}
    if not profile.get("active"):
        return
    name = profile.get("name") or "unknown"
    what = f"capture '{name}' is recording ({profile.get('session_dir') or '?'})"
    if force:
        print(f"WARNING: --force-capture given; {what}", file=sys.stderr)
        return
    raise ToolError(f"refusing: {what}. Stop it first, or pass --force-capture.")


def enter_bootloader_pins(board: Board) -> str:
    board.boot0(True)
    board.reset_pulse()
    info = board.bootloader_info()
    if info is None:
        raise ToolError("held BOOT0 high and pulsed NRST, but no bootloader answers at 0x56")
    return info


def read_options(board: Board) -> dict[str, int]:
    return option_settings(board.read_memory(OPTION_BYTES, OPTION_BYTES_SIZE))


def flash_is_blank(board: Board) -> bool:
    return board.read_memory(FLASH_BASE, 8) == b"\xff" * 8


def start_app(board: Board) -> tuple[str, dict[str, Any]]:
    """Reset into the firmware, or Go to it from the bootloader.

    A reset with BOOT0 low lands in the bootloader again while the flash-empty
    flag from the last option-byte load is still set (AN2606 pattern 11); the
    firmware clears it, so this is needed only the first time.
    """
    board.boot0(False)
    board.reset_pulse()
    values = board.wait_app()
    if values is not None:
        return "reset", values
    if board.bootloader_info() is None:
        raise ToolError(
            "after a reset neither the firmware (0x2E) nor the bootloader (0x56) answers"
        )
    if flash_is_blank(board):
        raise ToolError(
            "the flash is blank: nothing to run (flash an image first); the bootloader is up"
        )
    board.stm32flash("-g", f"0x{FLASH_BASE:08x}")
    values = board.wait_app()
    if values is None:
        raise ToolError(
            "the bootloader started the image, but the firmware does not answer at 0x2E"
        )
    return "bootloader Go", values


def helper_image(name: str) -> Path:
    path = BUILD / name
    if not path.exists():
        raise ToolError(f"{path} is missing: run `make` in {HERE}")
    return path


# ---- commands ----------------------------------------------------------------------


def cmd_status(board: Board, args: argparse.Namespace) -> int:
    values = board.app_registers()
    if values is None:
        raise ToolError(
            "no CANary-PWR firmware answers at 0x2E (in the bootloader? `run` starts it)"
        )
    summary = summarize(values, board.regmap)
    print(json.dumps(summary, indent=2, default=str) if args.json else render(summary))
    return 0


def cmd_clear(board: Board, args: argparse.Namespace) -> int:
    consts = board.regmap.consts
    if not board.app_write(bytes([consts["PWR_REG_COMMAND"], consts["PWR_CMD_CLEAR_TRACKERS"]])):
        raise ToolError("no CANary-PWR firmware answers at 0x2E")
    print("trackers cleared")
    return 0


def cmd_info(board: Board, args: argparse.Namespace) -> int:
    info = enter_bootloader_pins(board)
    for line in info.splitlines():
        if re.match(r"(Version|Option RAM|Device ID|- )", line.strip()):
            print(line.rstrip())
    options = read_options(board)
    for key, value in decode_optr(options["OPTR"]).items():
        print(f"  {key}: {value}")
    print(
        "  " + ", ".join(f"{name} 0x{value:X}" for name, value in options.items() if name != "OPTR")
    )
    blank = flash_is_blank(board)
    print(f"flash: {'blank' if blank else 'programmed'}")
    if args.stay or blank:
        print("left in the bootloader")
    else:
        how, _ = start_app(board)
        print(f"firmware running again ({how})")
    return 0


def cmd_probe_pins(board: Board, args: argparse.Namespace) -> int:
    layout = load_helper_layout()
    image = helper_image("ram-pins.bin")
    enter_bootloader_pins(board)
    blank = flash_is_blank(board)
    board.run_from_ram(image, layout.load)
    time.sleep(0.2)
    gone = board.bootloader_info() is None
    toggles = args.toggles
    for i in range(toggles):  # starts high, so an even count ends high
        board.boot0(i % 2 == 1)
        time.sleep(0.25)
    if args.button:
        print(f"press and release SW1 now ({args.button:.0f} s)...", flush=True)
        time.sleep(args.button)
    board.boot0(True)
    board.reset_pulse()
    back = board.bootloader_info() is not None
    if not back:
        raise ToolError("NRST pulse did not bring the bootloader back (GPIO24 -> Q8 -> NRST?)")
    status = layout.decode(board.read_memory(layout.status, layout.status_size))
    ok_magic = status["magic"] == layout.consts["HELPER_MAGIC"]
    checks = {
        "helper ran from SRAM (bootloader gone while it ran)": gone and ok_magic,
        "GPIO24 resets the MCU (bootloader back after the pulse)": back,
        f"GPIO27 reaches BOOT0 ({status['pa14_edges']} of {toggles} edges seen)": status[
            "pa14_edges"
        ]
        == toggles
        and status["pa14_high"] > 0
        and status["pa14_low"] > 0,
    }
    if args.button:
        checks[f"SW1 reaches PA5 ({status['pa5_low']} low samples)"] = status["pa5_low"] > 0
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
    print(
        f"      helper step {layout.step_name(status['step'])}, {status['count']} samples; "
        f"GPIOA IDR 0x{status['idr_a']:04X} (pins the helper leaves analog read 0)"
    )
    if not blank:
        how, _ = start_app(board)
        print(f"firmware running again ({how})")
    return 0 if all(checks.values()) else 1


def cmd_boot_sel(board: Board, args: argparse.Namespace) -> int:
    layout = load_helper_layout()
    image = helper_image("ram-boot-sel.bin")
    enter_bootloader_pins(board)
    options = read_options(board)
    before = options["OPTR"]
    print(f"now:     OPTR 0x{before:08X}  {decode_optr(before)}")
    if not before & OPTR_NBOOT_SEL:
        print("nBOOT_SEL is already 0: the BOOT0 pin is honoured; nothing to do")
        return 0
    want = boot_sel_plan(before)
    print(f"planned: OPTR 0x{want:08X}  (nBOOT_SEL 1 -> 0; RDP stays 0xAA; nothing else changes)")
    if not args.yes:
        print("dry run: pass --yes to program it")
        return 0
    board.run_from_ram(image, layout.load)
    time.sleep(1.0)  # OPTSTRT, then OBL_LAUNCH resets the MCU into the bootloader (BOOT0 held high)
    if board.bootloader_info() is None:
        board.reset_pulse()
        if board.bootloader_info() is None:
            raise ToolError("after programming, no bootloader answers at 0x56 even after a reset")
    options_after = read_options(board)
    after = options_after["OPTR"]
    status = layout.decode(board.read_memory(layout.status, layout.status_size))
    print(
        f"helper:  step {layout.step_name(status['step'])}, "
        f"OPTR before 0x{status['optr_before']:08X}, "
        f"read after the write 0x{status['optr_staged']:08X} / idle 0x{status['optr_idle']:08X}, "
        f"FLASH_SR 0x{status['flash_sr_before']:08X} -> 0x{status['flash_sr']:08X}, "
        f"FLASH_CR 0x{status['flash_cr_before']:08X} -> 0x{status['flash_cr']:08X}"
    )
    print(f"after:   OPTR 0x{after:08X}  {decode_optr(after)}")
    if after != want:
        raise ToolError(f"OPTR reads 0x{after:08X}, expected 0x{want:08X}")
    unexpected = {
        name: change
        for name, change in option_changes(options, options_after).items()
        if name != "OPTR"
    }
    if unexpected:
        listed = ", ".join(
            f"{name} 0x{old:X} -> 0x{new:X}" for name, (old, new) in unexpected.items()
        )
        raise ToolError(f"option registers besides OPTR changed: {listed}")
    print("PASS  every other option register is unchanged")
    print("PASS  nBOOT_SEL is 0: the MCU now follows the BOOT0 pin (GPIO27) at every reset")
    return 0


def cmd_flash(board: Board, args: argparse.Namespace) -> int:
    path = Path(args.image) if args.image else BUILD / "pwr.bin"
    image = path.read_bytes()
    check_firmware_image(image)
    enter_bootloader_pins(board)
    optr = read_options(board)["OPTR"]
    if optr & OPTR_NBOOT_SEL and not args.without_boot0:
        raise ToolError(
            "nBOOT_SEL is 1, so once the flash holds code the MCU ignores BOOT0, and the only "
            "way back into the bootloader is the firmware's own command (or SWD). "
            "Run `boot-sel --yes` first."
        )
    length = -(-len(image) // FLASH_PAGE) * FLASH_PAGE
    print(
        f"writing {path} ({len(image)} bytes, {length // FLASH_PAGE} pages) and verifying...",
        flush=True,
    )
    board.stm32flash("-w", str(path), "-v", "-S", f"0x{FLASH_BASE:08x}:{length}")
    how, values = start_app(board)
    summary = summarize(values, board.regmap)
    print(f"firmware {summary['firmware']} build {summary['build_id']} running ({how})")
    print(render(summary))
    return 0


def cmd_check_reentry(board: Board, args: argparse.Namespace) -> int:
    if board.app_registers() is None:
        raise ToolError("the firmware is not running: `run` or `flash` first")
    consts = board.regmap.consts
    enter_bootloader_pins(board)
    pins_ok = board.app_registers() is None
    start_app(board)
    sent = board.app_write(
        bytes([consts["PWR_REG_COMMAND"], consts["PWR_CMD_BOOTLOADER"]])
        + consts["PWR_CMD_BOOTLOADER_KEY"].encode()
    )
    time.sleep(0.3)
    command_ok = sent and board.bootloader_info() is not None and board.app_registers() is None
    how, _ = start_app(board)
    print(f"{'PASS' if pins_ok else 'FAIL'}  GPIO27 high + GPIO24 pulse -> bootloader")
    print(
        f"{'PASS' if command_ok else 'FAIL'}  firmware command "
        f"0x{consts['PWR_CMD_BOOTLOADER']:02X} 'LDR' -> bootloader"
    )
    print(f"      firmware running again ({how})")
    return 0 if pins_ok and command_ok else 1


def cmd_run(board: Board, args: argparse.Namespace) -> int:
    how, values = start_app(board)
    print(f"firmware {values['fw_major']}.{values['fw_minor']} running ({how})")
    return 0


def cmd_bootloader(board: Board, args: argparse.Namespace) -> int:
    if args.via == "command":
        consts = board.regmap.consts
        if not board.app_write(
            bytes([consts["PWR_REG_COMMAND"], consts["PWR_CMD_BOOTLOADER"]])
            + consts["PWR_CMD_BOOTLOADER_KEY"].encode()
        ):
            raise ToolError("no CANary-PWR firmware answers at 0x2E to take the command")
        time.sleep(0.3)
        info = board.bootloader_info()
        if info is None:
            raise ToolError("the firmware took the command, but no bootloader answers at 0x56")
    else:
        enter_bootloader_pins(board)
    print("in the bootloader at 0x56; `run` leaves it")
    return 0


RESETS = {"info", "probe-pins", "boot-sel", "flash", "check-reentry", "run", "bootloader"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bus", default=I2C_BUS)
    parser.add_argument(
        "--force-capture", action="store_true", help="reset the MCU even while a capture records"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="read and decode the running firmware's registers")
    status.add_argument("--json", action="store_true")
    sub.add_parser("clear", help="clear the min/max trackers and the button count")
    info = sub.add_parser("info", help="bootloader identity, option bytes, flash state")
    info.add_argument("--stay", action="store_true", help="stay in the bootloader afterwards")
    probe = sub.add_parser("probe-pins", help="RAM helper: prove the BOOT0 and NRST lines")
    probe.add_argument("--toggles", type=int, default=6, help="BOOT0 edges to send (even)")
    probe.add_argument(
        "--button", type=float, default=0.0, metavar="SECONDS", help="also wait for SW1"
    )
    boot_sel = sub.add_parser(
        "boot-sel", help="clear nBOOT_SEL so the MCU honours BOOT0 (once per board)"
    )
    boot_sel.add_argument("--yes", action="store_true", help="program it (otherwise a dry run)")
    flash = sub.add_parser("flash", help="write, verify and start a firmware image")
    flash.add_argument("image", nargs="?", help="default: build/pwr.bin")
    flash.add_argument(
        "--without-boot0", action="store_true", help="flash although nBOOT_SEL is still 1"
    )
    sub.add_parser("check-reentry", help="enter the bootloader by pins and by command, then run")
    sub.add_parser("run", help="reset into the firmware")
    boot = sub.add_parser("bootloader", help="enter the bootloader and stay there")
    boot.add_argument("--via", choices=("pins", "command"), default="pins")
    args = parser.parse_args(argv)
    if args.command == "probe-pins" and args.toggles % 2:
        parser.error("--toggles must be even, so BOOT0 ends high")

    board = Board(load_register_map(), bus=args.bus)
    commands: dict[str, Callable[[Board, argparse.Namespace], int]] = {
        "status": cmd_status,
        "clear": cmd_clear,
        "info": cmd_info,
        "probe-pins": cmd_probe_pins,
        "boot-sel": cmd_boot_sel,
        "flash": cmd_flash,
        "check-reentry": cmd_check_reentry,
        "run": cmd_run,
        "bootloader": cmd_bootloader,
    }
    try:
        if args.command in RESETS:
            refuse_while_capturing(args.force_capture)
        return commands[args.command](board, args)
    except ToolError as exc:
        print(f"pwr_tool: {exc}", file=sys.stderr)
        return 1
    finally:
        board.release()


if __name__ == "__main__":
    sys.exit(main())
