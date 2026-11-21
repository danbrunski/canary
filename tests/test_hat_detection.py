"""Which CANary-HAT is on the header when its ID EEPROM cannot say (2026-11-20).

The first rev 2.1 board arrived blank, with CANary-PWR stacked. The daemon took
the missing EEPROM to mean rev 2.0: the K-line pull-up went to GPIO24, which is
PWR's supervisor reset, it retried an ADS1015 that is not fitted every 10-20 s,
and the vehicle state read ``no_data`` where ``bench`` was true. Now a blank
EEPROM is answered by the board's own ADC, nothing answering means no HAT, and
``/status.hat`` says which happened and what is wrong.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from src.collection import hat_revision
from src.collection.ads8688a import RANGES, Ads8688a
from src.collection.collectors import _hat_adc_collectors, _kline_collectors
from src.collection.hat_adc_collector import Plan
from src.collection.hat_probe import (
    ADS1015_ADDRESS,
    ADS1015_BUS,
    ADS8688A_ANSWERS,
    ADS8688A_PROBE_CHANNEL,
    ads1015_answers,
    ads8688a_answers,
    probe_adcs,
)
from src.collection.hat_revision import (
    FittedHat,
    HatRevisionError,
    detect_hat,
    fitted_hat,
    hat_status,
    kline_pins,
    load_hat_revisions,
    resolve_hat_revision,
)
from src.collection.reconnect import ReconnectPolicy
from src.config import CanaryConfig, KlineConfig, SupplyVoltageConfig
from src.decode.hat_adc_board import load_hat_adc_board
from src.paths import REPO_ROOT

ROWS = load_hat_revisions()
EXAMPLE = REPO_ROOT / "canary.example.yaml"


def _eeprom(tmp_path: Path, product: str, product_ver: str) -> Path:
    hat = tmp_path / "hat"
    hat.mkdir()
    (hat / "product").write_text(product + "\0")
    (hat / "product_ver").write_text(product_ver + "\0")
    return hat


def _answers(*names: str) -> Any:
    def probe(rows: Mapping[str, Mapping[str, Any]]) -> tuple[tuple[str, ...], str]:
        assert set(rows) == set(ROWS)
        return names, "fake probe"

    return probe


def _kernel(tmp_path: Path, tty: str | None, pps: bool) -> tuple[Path, Path]:
    """A ``/dev`` with ``tty`` and a ``/sys/class/pps`` with or without CANary's."""
    dev, pps_dir = tmp_path / "dev", tmp_path / "pps"
    dev.mkdir()
    (pps_dir / "pps0").mkdir(parents=True)
    (pps_dir / "pps0" / "name").write_text("ptp0\n")
    if tty:
        (dev / Path(tty).name).touch()
    if pps:
        (pps_dir / "pps1").mkdir()
        (pps_dir / "pps1" / "name").write_text("canary-pps.-1\n")
    return dev, pps_dir


# --------------------------------------------------------------------------
# The probe: read-only known answers, and each can fail
# --------------------------------------------------------------------------


class FakeI2c:
    def __init__(self, registers: Mapping[int, bytes] | None) -> None:
        self.registers = registers
        self.closed = False

    def read(self, register: int, length: int) -> bytes:
        if self.registers is None:
            raise OSError(121, "Remote I/O error")
        return self.registers[register][:length]

    def write(self, register: int, value: int) -> None:
        raise AssertionError("the probe reads only")

    def write_block(self, register: int, data: bytes) -> None:
        raise AssertionError("the probe reads only")

    def close(self) -> None:
        self.closed = True


class FakeSpi:
    """Program registers behind ADS8688A frames; ``reads_only`` for the probe."""

    def __init__(
        self,
        registers: Mapping[int, int] | None = None,
        *,
        idle: int = 0x00,
        reads_only: bool = True,
    ) -> None:
        self.registers = dict(registers) if registers is not None else None
        self.idle = idle
        self.reads_only = reads_only
        self.frames: list[bytes] = []

    def transfer(self, frames: Sequence[bytes]) -> list[bytes]:
        self.frames.extend(frames)
        out = []
        for frame in frames:
            if len(frame) != 3:  # a command frame
                out.append(bytes(len(frame)))
                continue
            register, write = frame[0] >> 1, frame[0] & 1
            assert not (write and self.reads_only), "the probe reads only"
            if self.registers is None:
                value = self.idle
            elif write:
                self.registers[register] = value = frame[1]
            else:
                value = self.registers[register]
            out.append(bytes([self.idle, self.idle, value]))
        return out

    def close(self) -> None:
        pass


ADS1015_POWER_UP = {0x02: b"\x80\x00", 0x03: b"\x7f\xff"}


def test_an_ads1015_is_known_by_its_power_up_thresholds() -> None:
    bus = FakeI2c(ADS1015_POWER_UP)
    assert ads1015_answers(lambda b, a: bus)[0]
    assert bus.closed


@pytest.mark.parametrize(
    "registers",
    [
        None,  # nothing at 0x48
        {0x02: b"\x4b\x00", 0x03: b"\x50\x00"},  # an LM75's THYST/TOS at 0x48
        {0x02: b"\xff\xff", 0x03: b"\xff\xff"},
    ],
)
def test_nothing_else_at_0x48_reads_as_an_ads1015(registers: Mapping[int, bytes] | None) -> None:
    assert not ads1015_answers(lambda b, a: FakeI2c(registers))[0]


def test_a_missing_i2c_bus_is_no_ads1015() -> None:
    def absent(bus: int, address: int) -> FakeI2c:
        raise FileNotFoundError(2, "No such file or directory")

    answered, seen = ads1015_answers(absent)
    assert not answered and "No such file" in seen


def test_an_ads8688a_is_known_by_its_power_up_registers() -> None:
    assert ads8688a_answers("/dev/spidev0.1", lambda d: FakeSpi(dict(ADS8688A_ANSWERS)))[0]


@pytest.mark.parametrize("idle", [0x00, 0xFF])
def test_an_idle_spi_bus_is_no_ads8688a(idle: int) -> None:
    """The relation can fail: an idle MISO reads one byte from every register."""
    assert not ads8688a_answers("/dev/spidev0.1", lambda d: FakeSpi(idle=idle))[0]


def test_the_probe_names_each_revision_whose_adc_answered() -> None:
    i2c = lambda b, a: FakeI2c(None)  # noqa: E731
    spi = lambda d: FakeSpi(dict(ADS8688A_ANSWERS))  # noqa: E731
    found, seen = probe_adcs(ROWS, i2c=i2c, spi=spi)
    assert found == ("2.1",)
    assert "ADS8688A on /dev/spidev0.1 answered" in seen
    found, _ = probe_adcs(ROWS, i2c=lambda b, a: FakeI2c(ADS1015_POWER_UP), spi=lambda d: FakeSpi())
    assert found == ("2.0",)


def test_the_probe_uses_the_12v_sense_defaults() -> None:
    sense = SupplyVoltageConfig()
    assert (ADS1015_BUS, ADS1015_ADDRESS) == (sense.i2c_bus, sense.i2c_address)


def test_the_ads8688a_driver_never_writes_the_probed_registers() -> None:
    """The probe's known answer holds only while the driver leaves AUTO_SEQ_EN
    and CH7's range at their power-up values."""
    board = load_hat_adc_board(CanaryConfig.from_yaml(EXAMPLE).hardware.hat_adc.board)
    inputs = Plan(supply=True, sensors=True).inputs(board)
    assert ADS8688A_PROBE_CHANNEL not in inputs
    bus = FakeSpi({reg: 0 for reg in range(0x40)}, reads_only=False)
    Ads8688a(bus).configure(dict.fromkeys(inputs, RANGES[board.range]))
    written = {frame[0] >> 1 for frame in bus.frames if len(frame) == 3 and frame[0] & 1}
    assert written and not written & set(ADS8688A_ANSWERS)


def test_the_probe_runs_on_the_standard_library_alone() -> None:
    """hat_rev.py runs it with the Pi's system python3, before any venv: with
    no site-packages (``-S``) the import must still succeed, and load only its
    siblings."""
    code = (
        "import json, sys; import src.collection.hat_probe; "
        "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] == 'src')))"
    )
    out = subprocess.run(
        [sys.executable, "-S", "-E", "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    siblings = {"ads8688a", "hat_probe", "i2c_dev", "spi_dev"}
    allowed = {"src", "src.collection"} | {f"src.collection.{name}" for name in siblings}
    assert set(json.loads(out)) <= allowed


# --------------------------------------------------------------------------
# Detection: config, EEPROM, probe, none
# --------------------------------------------------------------------------


def test_a_blank_eeprom_is_answered_by_the_boards_adc(tmp_path: Path) -> None:
    dev, pps = _kernel(tmp_path, ROWS["2.1"]["gnss_tty"], pps=True)
    hat = detect_hat(
        "auto", hat_dir=tmp_path / "absent", probe=_answers("2.1"), dev_dir=dev, pps_dir=pps
    )
    assert hat.revision is not None
    assert (hat.revision.name, hat.source, hat.probe) == ("2.1", "probed", "fake probe")
    assert hat.revision.kline_pullup_gpio == 21
    assert any("program_hat.sh --rev 2.1" in p for p in hat.problems)


def test_no_eeprom_and_no_adc_is_no_hat_not_rev_2_0(tmp_path: Path) -> None:
    hat = detect_hat("auto", hat_dir=tmp_path / "absent", probe=_answers())
    assert (hat.revision, hat.source) == (None, "none")
    with pytest.raises(HatRevisionError, match="no CANary-HAT identified"):
        resolve_hat_revision("auto", hat_dir=tmp_path / "absent", probe=_answers())


def test_another_vendors_hat_is_probed_too(tmp_path: Path) -> None:
    hat = detect_hat(
        "auto", hat_dir=_eeprom(tmp_path, "Some Other HAT", "0x0201"), probe=_answers()
    )
    assert (hat.revision, hat.source, hat.eeprom_product) == (None, "none", "Some Other HAT")


def test_two_answering_adcs_name_no_revision(tmp_path: Path) -> None:
    hat = detect_hat("auto", hat_dir=tmp_path / "absent", probe=_answers("2.0", "2.1"))
    assert hat.revision is None
    assert any("set hardware.hat_revision" in p for p in hat.problems)


def test_an_eeprom_is_believed_without_probing(tmp_path: Path) -> None:
    def never(rows: Mapping[str, Mapping[str, Any]]) -> tuple[tuple[str, ...], str]:
        raise AssertionError("an EEPROM that names a revision is not second-guessed")

    dev, pps = _kernel(tmp_path, ROWS["2.0"]["gnss_tty"], pps=True)
    hat = detect_hat(
        "auto",
        hat_dir=_eeprom(tmp_path, "CANary-HAT rev 2.0", "0x0200"),
        probe=never,
        dev_dir=dev,
        pps_dir=pps,
    )
    assert hat.revision is not None
    assert (hat.revision.name, hat.source, hat.overlay_loaded, hat.problems) == (
        "2.0",
        "eeprom",
        True,
        (),
    )


def test_an_unlisted_eeprom_names_no_revision(tmp_path: Path) -> None:
    hat = detect_hat("auto", hat_dir=_eeprom(tmp_path, "CANary-HAT rev 3.0", "0x0300"))
    assert (hat.revision, hat.source) == (None, "eeprom")
    assert "0x0300" in hat.problems[0]


def test_a_config_that_contradicts_the_eeprom_wins_and_says_so(tmp_path: Path) -> None:
    hat = detect_hat("2.0", hat_dir=_eeprom(tmp_path, "CANary-HAT rev 2.1", "0x0201"))
    assert hat.revision is not None and (hat.revision.name, hat.source) == ("2.0", "config")
    assert any("the ID EEPROM reports rev 2.1" in p for p in hat.problems)


# --------------------------------------------------------------------------
# The overlay the kernel shows
# --------------------------------------------------------------------------


def test_a_missing_overlay_is_a_problem(tmp_path: Path) -> None:
    dev, pps = _kernel(tmp_path, None, pps=False)
    hat = detect_hat(
        "auto",
        hat_dir=_eeprom(tmp_path, "CANary-HAT rev 2.1", "0x0201"),
        dev_dir=dev,
        pps_dir=pps,
    )
    assert hat.overlay_loaded is False
    assert any("canary-hat-r21 is not loaded" in p for p in hat.problems)


def test_the_other_revisions_overlay_is_named(tmp_path: Path) -> None:
    dev, pps = _kernel(tmp_path, ROWS["2.0"]["gnss_tty"], pps=True)
    hat = detect_hat(
        "auto",
        hat_dir=_eeprom(tmp_path, "CANary-HAT rev 2.1", "0x0201"),
        dev_dir=dev,
        pps_dir=pps,
    )
    assert hat.overlay_loaded is False
    assert any("rev 2.0's overlay" in p for p in hat.problems)


# --------------------------------------------------------------------------
# Once per process; what the collectors and /status do with it
# --------------------------------------------------------------------------


def _fake_detect(monkeypatch: pytest.MonkeyPatch, hat: FittedHat) -> list[str]:
    calls: list[str] = []

    def detect(setting: str) -> FittedHat:
        calls.append(setting)
        return hat

    monkeypatch.setattr(hat_revision, "detect_hat", detect)
    return calls


def _no_hat() -> FittedHat:
    return FittedHat(None, "none", None, "nothing answered", None, ())


def _with(config: CanaryConfig, **hardware: Any) -> CanaryConfig:
    updates: dict[str, Any] = {}
    for key, value in hardware.items():
        current = getattr(config.hardware, key)
        updates[key] = current.model_copy(update=value) if isinstance(value, dict) else value
    return config.model_copy(update={"hardware": config.hardware.model_copy(update=updates)})


def test_detection_runs_once_and_logs_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _fake_detect(
        monkeypatch, FittedHat(None, "none", None, "seen", None, ("something wrong",))
    )
    with caplog.at_level(logging.WARNING, logger="src.collection.hat_revision"):
        assert fitted_hat("auto") is fitted_hat("auto")
    assert calls == ["auto"]
    assert [r.getMessage() for r in caplog.records].count("CANary-HAT: something wrong") == 1


def test_no_hat_starts_no_hat_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_detect(monkeypatch, _no_hat())
    config = _with(
        CanaryConfig.from_yaml(EXAMPLE),
        kline={"enabled": True},
        supply_voltage={"enabled": True},
        hat_adc={"enabled": True},
    )
    assert _kline_collectors(config) == []
    assert _hat_adc_collectors(config, ReconnectPolicy()) == []
    status = hat_status(config.hardware)
    assert status["revision"] is None
    assert any("hardware.kline" in p and "are off" in p for p in status["problems"])


def test_kline_pins_without_a_revision_need_both_pins_named() -> None:
    assert kline_pins(KlineConfig(), None) is None
    pins = kline_pins(KlineConfig(enable_gpio=5, pullup_gpio=7), None)
    assert pins is not None and (pins.enable, pins.pullup) == (5, 7)


def test_power_board_refuses_rev_2_0s_kline_pullup(monkeypatch: pytest.MonkeyPatch) -> None:
    """CANary-PWR's supervisor reset is GPIO24, rev 2.0's K-line pull-up (§P8)."""
    rev20 = hat_revision._revision(ROWS["2.0"], "config")
    _fake_detect(monkeypatch, FittedHat(rev20, "config", None, None, True, ()))
    config = _with(CanaryConfig.from_yaml(EXAMPLE), kline={"enabled": True}, power_board="1.0")
    assert _kline_collectors(config) == []
    status = hat_status(config.hardware)
    assert status["power_board"] == "1.0"
    assert any("GPIO24" in p and "reset" in p for p in status["problems"])


def test_power_board_and_rev_2_1_share_the_header(monkeypatch: pytest.MonkeyPatch) -> None:
    rev21 = hat_revision._revision(ROWS["2.1"], "eeprom")
    _fake_detect(monkeypatch, FittedHat(rev21, "eeprom", "CANary-HAT rev 2.1", None, True, ()))
    config = _with(CanaryConfig.from_yaml(EXAMPLE), kline={"enabled": True}, power_board="1.0")
    assert len(_kline_collectors(config)) == 1
    status = hat_status(config.hardware)
    assert status["problems"] == []
    assert (status["revision"], status["source"], status["overlay"]) == (
        "2.1",
        "eeprom",
        "canary-hat-r21",
    )
