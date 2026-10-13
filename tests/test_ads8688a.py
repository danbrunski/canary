"""The ADS8688A driver and its SPI transport, against SBAS680's frame protocol."""

from __future__ import annotations

import ctypes
from typing import Any

import pytest

from src.collection import spi_dev
from src.collection.ads8688a import (
    RANGES,
    Ads8688a,
    Ads8688aError,
    cmd_manual,
    command_frame,
    read_frame,
    reg_range,
    write_frame,
)
from tests.ads8688a_fake import AbsentBus, FakeAds8688a

FIVE = RANGES["0-5.12"]

# --------------------------------------------------------------------------
# Encodings from the datasheet's tables
# --------------------------------------------------------------------------


def test_manual_channel_commands_are_table_6() -> None:
    assert [cmd_manual(c) for c in range(8)] == [
        0xC000,
        0xC400,
        0xC800,
        0xCC00,
        0xD000,
        0xD400,
        0xD800,
        0xDC00,
    ]
    with pytest.raises(ValueError):
        cmd_manual(8)


def test_range_registers_and_codes_are_table_9_and_15() -> None:
    assert [reg_range(c) for c in range(8)] == list(range(0x05, 0x0D))
    assert RANGES["0-5.12"].code == 0b0110
    assert RANGES["0-10.24"].code == 0b0101
    assert RANGES["+-10.24"].code == 0b0000


def test_register_frames_put_the_address_above_the_write_bit() -> None:
    assert write_frame(0x05, 0x06) == bytes([0x0B, 0x06, 0x00])
    assert read_frame(0x05) == bytes([0x0A, 0x00, 0x00])
    assert command_frame(0xC400) == bytes([0xC4, 0x00, 0x00, 0x00])


def test_codes_are_straight_binary_in_every_range() -> None:
    assert FIVE.lsb_volts == pytest.approx(78.125e-6)
    assert FIVE.volts(0) == 0.0
    assert FIVE.volts(0xFFFF) == pytest.approx(5.12 - 78.125e-6)
    bipolar = RANGES["+-10.24"]
    assert bipolar.volts(0x8000) == pytest.approx(0.0)
    assert bipolar.volts(0) == pytest.approx(-10.24)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_configure_resets_sets_ranges_and_powers_down_the_rest() -> None:
    fake = FakeAds8688a()
    adc = Ads8688a(fake)
    adc.configure(dict.fromkeys(range(5), FIVE))
    assert [fake.registers[reg_range(c)] for c in range(5)] == [0b0110] * 5
    assert fake.registers[0x02] == 0b1110_0000  # CH5-CH7 powered down
    assert len(fake.messages) == 1  # one bus message for the whole setup
    assert fake.messages[0][0] == command_frame(0x8500)  # RST first


def test_configure_refuses_a_device_that_does_not_read_back() -> None:
    with pytest.raises(Ads8688aError, match="register 0x05"):
        Ads8688a(AbsentBus()).configure({0: FIVE})


def test_verify_catches_a_device_that_reset_behind_our_back() -> None:
    fake = FakeAds8688a({0: 2.28})
    adc = Ads8688a(fake)
    adc.configure({0: FIVE})
    adc.verify()  # configured as asked
    fake.transfer([command_frame(0x8500)])  # a brown-out: back to +-10.24 V
    with pytest.raises(Ads8688aError, match="lost its configuration"):
        adc.verify()


def test_a_burst_after_a_reset_is_discarded_not_misscaled() -> None:
    """A reset puts every range back to +-10.24 V; the burst that follows would
    decode 2.28 V as 3.13 V. The burst's own register check catches it."""
    fake = FakeAds8688a({0: 2.28})
    adc = Ads8688a(fake)
    adc.configure({0: FIVE})
    assert FIVE.volts(adc.read([0]).codes[0]) == pytest.approx(2.28, abs=1e-3)
    fake.transfer([command_frame(0x8500)])  # a brown-out
    with pytest.raises(Ads8688aError, match="burst discarded"):
        adc.read([0])


def test_configure_needs_a_channel() -> None:
    with pytest.raises(ValueError):
        Ads8688a(FakeAds8688a()).configure({})


# --------------------------------------------------------------------------
# Bursts
# --------------------------------------------------------------------------


def test_a_burst_returns_each_channel_one_frame_late_and_in_one_message() -> None:
    fake = FakeAds8688a({0: 2.28, 1: 1.5, 3: 5.0, 4: 2.582})
    ticks = iter([100.0, 100.0002])
    adc = Ads8688a(fake, clock=lambda: next(ticks))
    adc.configure(dict.fromkeys(range(5), FIVE))
    burst = adc.read([0, 1, 3, 4])
    assert burst.stamp == pytest.approx(100.0001)
    for channel, volts in fake.inputs.items():
        assert FIVE.volts(burst.codes[channel]) == pytest.approx(volts, abs=FIVE.lsb_volts)
    message = fake.messages[-1]
    # Four channels, NO_OP to clock out the last, then a register to prove the
    # chip still holds its configuration.
    assert len(message) == 6
    assert message[4] == command_frame(0x0000)
    assert message[5] == read_frame(reg_range(0))


def test_consecutive_bursts_never_mix_up_channels() -> None:
    fake = FakeAds8688a({0: 1.0, 2: 2.0})
    adc = Ads8688a(fake)
    adc.configure({0: FIVE, 2: FIVE})
    for _ in range(3):
        burst = adc.read([2, 0])
        assert FIVE.volts(burst.codes[2]) == pytest.approx(2.0, abs=1e-4)
        assert FIVE.volts(burst.codes[0]) == pytest.approx(1.0, abs=1e-4)


def test_an_unmapped_channel_is_refused_not_read() -> None:
    fake = FakeAds8688a()
    adc = Ads8688a(fake)
    adc.configure(dict.fromkeys(range(5), FIVE))
    with pytest.raises(ValueError, match=r"\[5\] are not configured"):
        adc.read([0, 5])
    assert len(fake.messages) == 1  # nothing went on the bus


# --------------------------------------------------------------------------
# The Linux transport
# --------------------------------------------------------------------------


def test_ioctl_numbers_match_spidev_h() -> None:
    assert spi_dev.spi_ioc_message(1) == 0x40206B00
    assert spi_dev.SPI_IOC_WR_MODE == 0x40016B01
    assert spi_dev.SPI_IOC_WR_BITS_PER_WORD == 0x40016B03
    assert spi_dev.SPI_IOC_WR_MAX_SPEED_HZ == 0x40046B04


def test_transfer_deselects_between_frames_and_returns_what_was_clocked_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, Any]] = []

    def fake_ioctl(fd: int, request: int, arg: Any) -> None:
        calls.append((request, arg))
        if request == spi_dev.spi_ioc_message(len(arg)):
            for slot in arg:  # the device answers each byte with its complement
                tx = (ctypes.c_uint8 * slot.len).from_address(slot.tx_buf)
                rx = (ctypes.c_uint8 * slot.len).from_address(slot.rx_buf)
                for i in range(slot.len):
                    rx[i] = tx[i] ^ 0xFF

    monkeypatch.setattr(spi_dev.os, "open", lambda *_a, **_k: 99)
    monkeypatch.setattr(spi_dev.os, "close", lambda _fd: None)
    monkeypatch.setattr(spi_dev.fcntl, "ioctl", fake_ioctl)
    bus = spi_dev.LinuxSpiBus("/dev/spidev0.1", mode=spi_dev.SPI_MODE_1, speed_hz=8_000_000)
    assert [c[0] for c in calls] == [
        spi_dev.SPI_IOC_WR_MODE,
        spi_dev.SPI_IOC_WR_BITS_PER_WORD,
        spi_dev.SPI_IOC_WR_MAX_SPEED_HZ,
    ]
    out = bus.transfer([b"\x01\x02\x03\x04", b"\x10\x20\x30"])
    assert out == [b"\xfe\xfd\xfc\xfb", b"\xef\xdf\xcf"]
    message = calls[-1][1]
    assert [slot.cs_change for slot in message] == [1, 0]
    assert {slot.speed_hz for slot in message} == {8_000_000}
    bus.close()
    with pytest.raises(OSError):
        bus.transfer([b"\x00"])
