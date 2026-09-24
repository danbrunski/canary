"""LSM6DSOX driver: configuration writes, FIFO decoding, sample timing.

Synthetic FIFO words give answers known by construction; a real three-batch
FIFO dump from the CANary-HAT at rest (``tests/fixtures/lsm6dsox``) checks the
decoder against the chip's own bytes.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from src.collection import lsm6dsox as lsm
from src.collection.lsm6dsox import FifoDecoder, ImuSettings, Lsm6dsox, Lsm6dsoxError

FIXTURE = Path(__file__).parent / "fixtures" / "lsm6dsox" / "fifo_at_rest.json"
SETTINGS = ImuSettings(odr_hz=104.0, accel_range_g=8, gyro_range_dps=2000)


def _word(tag: int, counter: int, data: bytes) -> bytes:
    assert len(data) == 6
    return bytes([tag << 3 | (counter & 0x3) << 1]) + data


def _vec(x: int, y: int, z: int) -> bytes:
    return b"".join(v.to_bytes(2, "little", signed=True) for v in (x, y, z))


def _stamp(counter: int, ticks: int) -> bytes:
    return _word(lsm.TAG_TIMESTAMP, counter, ticks.to_bytes(4, "little") + b"\0\0")


def _instants(
    first_counter: int,
    n: int,
    *,
    stamp_every: int = 8,
    ticks0: int = 1000,
    ticks_per: float = 384.0,
) -> bytes:
    """``n`` instants of gyro+accel, with a timestamp word every ``stamp_every``."""
    out = b""
    for i in range(n):
        counter = (first_counter + i) % 4
        if i % stamp_every == 0:
            out += _stamp(counter, round(ticks0 + i * ticks_per))
        out += _word(lsm.TAG_GYRO, counter, _vec(100 + i, 0, -100))
        out += _word(lsm.TAG_ACCEL, counter, _vec(0, 0, 4098))  # 4098 * 0.244 mg = 1.0 g
    return out


def _identity(ticks: float) -> float:
    return ticks / 40_000.0


def test_settings_refuse_rates_and_ranges_the_chip_cannot_do() -> None:
    with pytest.raises(ValueError, match="ODR 100"):
        ImuSettings(odr_hz=100.0, accel_range_g=8, gyro_range_dps=2000)
    with pytest.raises(ValueError, match="accel range 6"):
        ImuSettings(odr_hz=104.0, accel_range_g=6, gyro_range_dps=2000)
    with pytest.raises(ValueError, match="gyro range 300"):
        ImuSettings(odr_hz=104.0, accel_range_g=8, gyro_range_dps=300)


def test_decoder_groups_words_by_tag_counter_and_scales_units() -> None:
    decoder = FifoDecoder(SETTINGS)
    batch = decoder.decode(_instants(0, 5), to_host=_identity)
    samples = batch.samples + decoder.flush(_identity)

    assert batch.undated == 0 and batch.temperatures == []
    assert len(samples) == 5
    assert samples[0].accel_g == pytest.approx((0.0, 0.0, 4098 * 0.244e-3))
    assert samples[0].gyro_dps == pytest.approx((100 * 70e-3, 0.0, -100 * 70e-3))
    assert samples[4].gyro_dps is not None
    assert samples[4].gyro_dps[0] == pytest.approx(104 * 70e-3)


def test_instants_between_timestamps_are_spaced_by_ticks_per_instant() -> None:
    decoder = FifoDecoder(SETTINGS)
    # One timestamp word, so the spacing is the nominal one: 40000 / 104.
    samples = decoder.decode(_instants(0, 6), to_host=_identity).samples
    samples += decoder.flush(_identity)

    gaps = [b.timestamp - a.timestamp for a, b in zip(samples, samples[1:], strict=False)]
    assert gaps == pytest.approx([(40_000 / 104) / 40_000.0] * 5)
    assert samples[0].timestamp == pytest.approx(1000 / 40_000.0)


def test_ticks_per_instant_is_learned_from_consecutive_timestamps() -> None:
    # The chip's real ODR is not exactly nominal: 104 Hz nominal runs at
    # ~104.17 Hz, i.e. 384 ticks, not 40000/104 = 384.6.
    decoder = FifoDecoder(SETTINGS)
    assert decoder.ticks_per_instant == pytest.approx(40_000 / 104)
    decoder.decode(_instants(0, 17, ticks_per=384.0), to_host=_identity)
    assert decoder.ticks_per_instant == pytest.approx(384.0)


def test_a_wild_timestamp_does_not_retune_the_spacing() -> None:
    decoder = FifoDecoder(SETTINGS)
    words = _instants(0, 8, ticks_per=384.0) + _stamp(0, 1000 + 16 * 384)  # 16, not 8
    decoder.decode(words, to_host=_identity)
    assert decoder.ticks_per_instant == pytest.approx(40_000 / 104)


def test_an_instant_split_across_two_reads_decodes_the_same() -> None:
    words = _instants(0, 10)
    whole = FifoDecoder(SETTINGS)
    expected = whole.decode(words, to_host=_identity).samples + whole.flush(_identity)

    split = FifoDecoder(SETTINGS)
    cut = 7 * 4  # after stamp+gyro+accel of instant 0 and the gyro of instant 1
    got = split.decode(words[:cut], to_host=_identity).samples
    got += split.decode(words[cut:], to_host=_identity).samples + split.flush(_identity)
    assert got == expected


def test_instants_before_the_first_timestamp_are_counted_not_guessed() -> None:
    decoder = FifoDecoder(SETTINGS)
    undated_words = _word(lsm.TAG_GYRO, 3, _vec(1, 2, 3)) + _word(lsm.TAG_ACCEL, 3, _vec(1, 2, 3))
    batch = decoder.decode(undated_words + _instants(0, 3), to_host=_identity)
    samples = batch.samples + decoder.flush(_identity)
    assert batch.undated == 1
    assert len(samples) == 3


def test_temperature_word_is_25c_plus_256_lsb_per_degree() -> None:
    decoder = FifoDecoder(SETTINGS)
    words = _instants(0, 1) + _word(
        lsm.TAG_TEMP, 1, (512).to_bytes(2, "little", signed=True) + b"\0" * 4
    )
    temps = decoder.decode(words, to_host=_identity).temperatures
    assert [t.celsius for t in temps] == [pytest.approx(27.0)]


def test_a_partial_word_is_an_error() -> None:
    with pytest.raises(Lsm6dsoxError, match="whole words"):
        FifoDecoder(SETTINGS).decode(b"\x00" * 8, to_host=_identity)


class FakeBus:
    """Registers as a dict, a FIFO as a byte string, and a log of writes."""

    def __init__(self, fifo: bytes = b"", *, who: int = 0x6C, status2: int = 0) -> None:
        self.regs: dict[int, int] = {lsm.WHO_AM_I: who, lsm.CTRL3_C: 0, lsm.INTERNAL_FREQ_FINE: 0}
        self.fifo = fifo
        self.status2 = status2
        self.timestamp = 5000
        self.writes: list[tuple[int, int]] = []
        self.closed = False

    def read(self, register: int, length: int) -> bytes:
        if register == lsm.FIFO_STATUS1:
            words = len(self.fifo) // 7
            return bytes([words & 0xFF, (words >> 8) & 0x3 | self.status2])
        if register == lsm.TIMESTAMP0:
            return self.timestamp.to_bytes(4, "little")
        if register == lsm.FIFO_DATA_OUT_TAG:
            out, self.fifo = self.fifo[:length], self.fifo[length:]
            return out
        return bytes([self.regs.get(register + i, 0) for i in range(length)])

    def write(self, register: int, value: int) -> None:
        self.writes.append((register, value))
        if register == lsm.CTRL3_C:
            value &= ~lsm.CTRL3_SW_RESET  # reset completes immediately
        self.regs[register] = value

    def close(self) -> None:
        self.closed = True


def test_start_refuses_a_device_that_is_not_an_lsm6dsox() -> None:
    with pytest.raises(Lsm6dsoxError, match="WHO_AM_I 0x6a"):
        Lsm6dsox(FakeBus(who=0x6A), SETTINGS).start()


def test_start_writes_the_configuration_measured_on_the_hat() -> None:
    bus = FakeBus()
    Lsm6dsox(bus, SETTINGS).start()
    config = dict(bus.writes)
    # The same values the 2026-09-23 bring-up used on the real chip.
    assert config[lsm.CTRL1_XL] == 0x4C
    assert config[lsm.CTRL2_G] == 0x4C
    assert config[lsm.FIFO_CTRL3] == 0x44
    assert config[lsm.FIFO_CTRL4] == 0x96
    assert config[lsm.CTRL3_C] == lsm.CTRL3_BDU_IF_INC
    assert config[lsm.CTRL10_C] == lsm.CTRL10_TIMESTAMP_EN
    assert bus.writes[0] == (lsm.CTRL3_C, lsm.CTRL3_SW_RESET)


def test_read_fifo_maps_chip_ticks_to_host_time() -> None:
    bus = FakeBus(_instants(0, 3, ticks0=4000, ticks_per=384.0))
    bus.timestamp = 5000  # 1000 ticks = 25 ms after the first instant
    device = Lsm6dsox(bus, SETTINGS, clock=lambda: 100.0)
    device.start()
    batch = device.read_fifo()
    assert batch.words == 7 and not batch.overrun
    # start() arms the power-up settling window: all three instants fall in it.
    assert batch.samples == [] and batch.settling == 2


def test_samples_after_the_settling_window_map_chip_ticks_to_host_time() -> None:
    settle = int(lsm.SETTLE_SECONDS * 40_000)
    bus = FakeBus(_instants(0, 40, ticks0=4000, ticks_per=384.0))
    bus.timestamp = 4000 + 40 * 384 + 1000  # read after the last instant
    device = Lsm6dsox(bus, SETTINGS, clock=lambda: 100.0)
    device.start()
    batch = device.read_fifo()
    kept_ticks = [4000 + i * 384 for i in range(40) if i * 384 > settle][:-1]
    assert batch.settling == 40 - 1 - len(kept_ticks)
    assert [s.timestamp for s in batch.samples] == pytest.approx(
        [100.0 - (bus.timestamp - t) / 40_000.0 for t in kept_ticks]
    )


def test_an_overrun_is_reported_and_resets_the_timeline() -> None:
    bus = FakeBus(_instants(0, 2), status2=lsm.STATUS2_OVR_LATCHED)
    device = Lsm6dsox(bus, SETTINGS)
    device.start()
    assert device.read_fifo().overrun


def test_stop_bypasses_the_fifo_and_powers_both_sensors_down() -> None:
    bus = FakeBus()
    Lsm6dsox(bus, SETTINGS).stop()
    assert bus.writes == [(lsm.FIFO_CTRL4, 0), (lsm.CTRL1_XL, 0), (lsm.CTRL2_G, 0)]


def test_real_fifo_capture_decodes_to_one_g_at_rest() -> None:
    fixture = json.loads(FIXTURE.read_text())
    settings = ImuSettings(**fixture["settings"])
    decoder = FifoDecoder(settings)
    decoder.power_up()  # the capture began at power-up, as the collector's does
    per_second = 40_000.0 * (1.0 + 0.0015 * fixture["internal_freq_fine"])
    samples = []
    settled = 0
    for batch in fixture["batches"]:
        now_ticks = int.from_bytes(bytes.fromhex(batch["timestamp_reg"]), "little")
        host_now = (batch["host_before"] + batch["host_after"]) / 2

        def to_host(ticks: float, now_ticks: int = now_ticks, host_now: float = host_now) -> float:
            return host_now - ((now_ticks - ticks) % (1 << 32)) / per_second

        part = decoder.decode(bytes.fromhex(batch["fifo"]), to_host=to_host)
        assert part.undated == 0
        settled += part.settling
        samples.extend(part.samples)

    assert len(samples) > 50
    magnitudes = [math.dist((0, 0, 0), s.accel_g) for s in samples if s.accel_g]
    assert min(magnitudes) > 0.95 and max(magnitudes) < 1.05
    gyro_peak = max(max(abs(v) for v in s.gyro_dps) for s in samples if s.gyro_dps)
    assert gyro_peak < 5.0
    # 104 Hz nominal: the learned spacing and the stamped gaps agree to 1 %.
    gaps = sorted(b.timestamp - a.timestamp for a, b in zip(samples, samples[1:], strict=False))
    assert gaps[len(gaps) // 2] == pytest.approx(1 / 104.0, rel=0.01)
    assert all(gap > 0 for gap in gaps)
    # The chip really runs at 104.17 Hz: 384 timestamp ticks per instant.
    assert decoder.ticks_per_instant == pytest.approx(384.0, abs=0.5)
    assert settled == 1 + 4000 // 384  # instants 0..10 fall inside 100 ms
