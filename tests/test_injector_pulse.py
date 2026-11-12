"""E07.3: the ECM's injector pulse chain in its own integer arithmetic.

Synthetic tests pin the decoded formulas; the known answer — the per-sample
ratio ``(DID 0x1206 - Ts) / predicted`` near 1 (docs/ecu-firmware-analysis.md:
median 1.006, p10-p90 0.961-1.030 over 5,039 steady samples) — runs on a
committed slice of a held-out drive with the operator's image, and skips by
name when the image is not on this machine (no ROM bytes are committed).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from src.analysis.fuel_plant import FuelPlantRefusalError, load_b06, load_fuel_plant
from src.analysis.fuel_plant_eval import (
    build_samples,
    calibration_from_image,
    pulse_chain_ratios,
    summarize_ratios,
    without_pressure_correction,
)
from src.analysis.injector_pulse import (
    SHORT_PULSE_TABLES,
    TRIM_UNITY,
    PulseChainCalibration,
    ShortPulseCalibration,
    dead_time_counts,
    predict_pulse,
    pressure_correction_word,
    short_pulse_from_image,
    short_pulse_linearize,
    trim_word,
    vb_counts_from_volts,
)
from src.analysis.rom_map import load_rom_map
from tests.rom_fixtures import operator_image

PROFILE = Path("data/nissan-xterra-2008")
FIXTURE = Path("tests/fixtures/fuel_plant/_capture-tuning-2026-09-23T142926")
#: Plain-image sha256 of ZS44A-20260920 (tune_revisions.yaml).
R20260920_SHA = "a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50"

CAL = PulseChainCalibration(
    dead_time_base=47,
    dead_time_slope=92,
    dead_time_vref=175,
    rail_reference=175,
    flow_scaler=10601,
    vacuum_load_curve=tuple(range(0, 160, 10)),
    vacuum_rpm_curve=(0,) * 16,
)


def _ts(vb: int) -> int:
    return int(dead_time_counts([vb], base=47, slope=92, vref=175)[0])


def test_dead_time_is_linear_in_battery_voltage_with_an_arithmetic_shift() -> None:
    assert _ts(175) == 47  # 0.47 ms at 14.0 V
    assert _ts(150) == 47 + (25 * 92 >> 7)  # 12.0 V: 0.64 ms
    assert _ts(180) == 47 + ((-5 * 92) >> 7) == 43  # floors below 47 above 14 V
    assert _ts(0) == 47 + (175 * 92 >> 7)
    assert int(vb_counts_from_volts([14.0])[0]) == 175


def test_trims_add_and_zero_percent_is_unity() -> None:
    assert int(trim_word([0.0])[0]) == TRIM_UNITY
    assert int(trim_word([25.0])[0]) == (32 + 128) * 200
    assert int(trim_word([-12.5])[0]) == (128 - 16) * 200


def test_pressure_correction_is_unity_sqrt_times_the_flow_scaler_at_no_vacuum() -> None:
    at_zero = int(pressure_correction_word([0], CAL)[0])
    assert at_zero == (255 * 10601) >> 8
    words = pressure_correction_word(np.arange(0, 80 * 512, 512 * 10), CAL)
    assert np.all(np.diff(words) <= 0)  # more vacuum, more dP, shorter pulse
    # The lineariser approximates 256 sqrt(350 / (350 + vacuum)) to within a count.
    for vac in (0, 20, 45, 70):
        word = int(pressure_correction_word([vac * 512], CAL)[0])
        exact = min(255.0, 256 * (350 / (350 + vac)) ** 0.5) * 10601 / 256
        assert abs(word - exact) <= 1.5 * 10601 / 256


def test_a_neutral_pulse_scales_with_bfs_and_phi_and_carries_ts() -> None:
    kwargs = dict(
        stft_word=TRIM_UNITY,
        ltft_word=TRIM_UNITY,
        vb_counts=175,
        rpm=4000,
        baro_kpa=81,
        iat_c=25,
        # Flat vacuum curves: with the real ones more BFS means less estimated
        # vacuum, which is the point of the correction but not of this test.
        cal=PulseChainCalibration(47, 92, 175, 175, 10601, (100,) * 16, (0,) * 16),
    )
    one = predict_pulse(bfs=[20000], phi_q14=[16384], **kwargs)  # type: ignore[arg-type]
    rich = predict_pulse(bfs=[20000], phi_q14=[int(1.2 * 16384)], **kwargs)  # type: ignore[arg-type]
    double = predict_pulse(bfs=[40000], phi_q14=[16384], **kwargs)  # type: ignore[arg-type]
    assert int(one.dead_time[0]) == 47
    assert abs(rich.effective[0] / one.effective[0] - 1.2) < 0.01
    assert abs(double.effective[0] / one.effective[0] - 2.0) < 0.01


def test_an_image_whose_declared_words_differ_is_refused() -> None:
    profile, _ = load_fuel_plant(PROFILE)
    image = bytearray(1 << 20)
    with pytest.raises(FuelPlantRefusalError) as err:
        calibration_from_image(bytes(image), profile.pulse_chain)
    assert err.value.reason == "calibration_mismatch"


def _image() -> bytes:
    image = operator_image("r20260920")
    if image is None or hashlib.sha256(image).hexdigest() != R20260920_SHA:
        pytest.skip(
            "private ROM ZS44A-20260920 (plain sha256 a46baa24...) is not on this machine: "
            "the pulse-chain known answer did not run"
        )
    return image


def test_the_decoded_chain_reproduces_the_logged_pulse_per_sample() -> None:
    """Known answer: (DID 0x1206 - Ts) / predicted, steady fuelled reads.

    Full held-out + identification run (tools.fuel_plant_check chain, 6 tuning
    captures): n = 2,361, median 1.0084, p10-p90 0.958-1.025 against the
    firmware session's 1.006 / 0.961-1.030. On this committed slice:
    """
    profile, _ = load_fuel_plant(PROFILE)
    delay = load_b06(PROFILE, profile.delay)
    cal = calibration_from_image(_image(), profile.pulse_chain)
    frame = build_samples(FIXTURE, profile, delay)
    ratios = pulse_chain_ratios(frame, cal, profile.pulse_chain)
    stats = summarize_ratios(ratios["ratio"].to_numpy(dtype=float))
    assert stats["n"] == 218
    assert stats["median"] == pytest.approx(1.0084, abs=0.0005)
    assert 0.98 <= stats["p10"] and stats["p90"] <= 1.04
    # Ts came from the session's own fuel-cut reads (no PID 0x42 on the
    # tuning captures), and the vacuum estimate never falls below ~0.2 baro.
    assert set(ratios["dead_time_source"]) == {"fuel_cut_reads"}
    assert (ratios["vacuum_kpa"] <= ratios["baro_kpa"] * 0.81).all()


def test_removing_the_pressure_correction_fails_the_known_answer() -> None:
    """Negative control: the same slice without the ECM's rail-to-manifold
    correction must leave the band the real chain lands in, or the known
    answer would be a constant matching a constant.

    Full run (6 captures, n = 2,361): median 0.9364, p10-p90 0.891-0.968.
    What the known answer does NOT verify: Ts. On these captures Ts is the
    session's own fuel-cut pulse word, subtracted from the measured side, and
    the prediction is Ts-free; so it checks G x k, not the dead-time formula
    (checked only on 46 fuel-cut reads, +-2 counts) and not the injector's
    physical latency.
    """
    profile, _ = load_fuel_plant(PROFILE)
    delay = load_b06(PROFILE, profile.delay)
    cal = calibration_from_image(_image(), profile.pulse_chain)
    frame = build_samples(FIXTURE, profile, delay)
    real = summarize_ratios(
        pulse_chain_ratios(frame, cal, profile.pulse_chain)["ratio"].to_numpy(dtype=float)
    )
    control = summarize_ratios(
        pulse_chain_ratios(frame, without_pressure_correction(cal), profile.pulse_chain)[
            "ratio"
        ].to_numpy(dtype=float)
    )
    assert 0.98 <= real["median"] <= 1.04
    assert control["n"] == real["n"]
    assert not 0.98 <= control["median"] <= 1.04
    assert control["median"] < 0.96


# -- F01.4: the short-pulse linearisation (INJ_short_pulse_linearize 0x05B8B0) --

#: The ZS44A words (cal 0x9120-0x9126), equal on every image on the laptop.
SHORT = ShortPulseCalibration(gain_a=16358, gain_b=15846, offset_a=74, offset_b=959)


@pytest.mark.parametrize(
    ("x", "y"),
    [
        # Worked by hand: a = (x*16358 + 7400) >> 14, b = (x*15846 + 95900) >> 14.
        (0, 5),  # b = 95900 >> 14 = 5 (5.85 truncated); a = 0
        (100, 102),  # a = 1643200 >> 14 = 100, b = 1680500 >> 14 = 102
        (140, 141),  # b = 2314340 >> 14 = 141
        (150, 150),  # b = 2472800 >> 14 = 150: the lift has truncated away
        (177, 177),  # b = 2900642 >> 14 = 177, a = 2902766 >> 14 = 177
        (300, 300),  # both lines below x
        (0xFFFF, 0xFFFF),  # both below x; x itself is the max
    ],
)
def test_short_pulse_lifts_only_short_pulses(x: int, y: int) -> None:
    assert int(short_pulse_linearize([x], SHORT)[0]) == y


def test_short_pulse_never_shortens_and_caps_each_line_at_a_u16() -> None:
    x = np.arange(0, 0x10000)
    out = short_pulse_linearize(x, SHORT)
    assert np.all(out >= x)
    assert out.max() == 0xFFFF
    # The lift is confined below 1.78 ms (term B's crossing) and is at most 5.
    lift = out - x
    assert lift.max() == 5
    assert not np.any(lift[178:])
    # A steeper gain would exceed 0xFFFF: the cap holds the line at the word.
    steep = ShortPulseCalibration(gain_a=0xFFFF, gain_b=0, offset_a=0xFFFF, offset_b=0)
    assert int(short_pulse_linearize([0xFFFF], steep)[0]) == 0xFFFF


def test_short_pulse_refuses_a_word_outside_u16() -> None:
    with pytest.raises(ValueError, match="u16"):
        short_pulse_linearize([0x10000], SHORT)
    with pytest.raises(ValueError, match="gain_a"):
        ShortPulseCalibration(gain_a=-1, gain_b=0, offset_a=0, offset_b=0)


def test_short_pulse_words_are_read_by_rom_map_id() -> None:
    rom_map = load_rom_map(PROFILE)
    image = bytearray(rom_map.image_size)
    for table_id, value in zip(SHORT_PULSE_TABLES, (16358, 15846, 74, 959), strict=True):
        offset = rom_map.table(table_id).offset
        assert offset is not None
        image[offset : offset + 2] = value.to_bytes(2, "big")
    assert short_pulse_from_image(bytes(image), rom_map) == SHORT


def test_short_pulse_words_on_the_operator_image() -> None:
    assert short_pulse_from_image(_image(), load_rom_map(PROFILE)) == SHORT
