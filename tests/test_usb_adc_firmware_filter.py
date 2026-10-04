"""The USB ADC firmware's decimation filter (firmware/usb-adc/filter.h),
compiled for the host and driven with known signals: unit DC gain, the
designed passband gain, stopband tones that do not alias into the output,
and one output per R1 x R2 conversions. Skips where no C compiler is found."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware" / "usb-adc"
# firmware/usb-adc is not a package (its name has a hyphen): load the design
# script by path.
_SPEC = importlib.util.spec_from_file_location("design_fir", FIRMWARE / "design_fir.py")
assert _SPEC is not None and _SPEC.loader is not None
design_fir = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(design_fir)

CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
pytestmark = pytest.mark.skipif(CC is None, reason="no host C compiler")


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Path:
    exe = tmp_path_factory.mktemp("filter") / "host_filter"
    assert CC is not None
    subprocess.run(
        [
            CC,
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(FIRMWARE),
            str(FIRMWARE / "host_filter.c"),
            "-o",
            str(exe),
        ],
        check=True,
    )
    return exe


def _run(exe: Path, conversions: np.ndarray) -> np.ndarray:
    data = np.clip(np.round(conversions), 0, 4095).astype("<u2").tobytes()
    out = subprocess.run([str(exe)], input=data, capture_output=True, check=True).stdout
    return np.array([int(v) for v in out.split()], dtype=float)


def _tone(
    freq_hz: float, seconds: float = 2.0, mean: float = 2000.0, amp: float = 400.0
) -> np.ndarray:
    n = int(seconds * design_fir.FS_ADC)
    t = np.arange(n) / float(design_fir.FS_ADC)
    tone: np.ndarray = mean + amp * np.sin(2 * np.pi * freq_hz * t)
    return tone


def _amplitude(y: np.ndarray, freq_hz: float) -> float:
    """Amplitude of the ``freq_hz`` component of an output-rate series."""
    y = y[len(y) // 4 :] - y.mean()
    t = np.arange(len(y)) / design_fir.FS_OUT
    basis = np.exp(-2j * np.pi * freq_hz * t)
    return float(2 * abs(np.dot(y, basis)) / len(y))


def test_the_header_is_what_the_design_script_writes() -> None:
    assert design_fir.render(design_fir.design()) == (FIRMWARE / "fir_taps.h").read_text()


def test_dc_passes_unchanged_at_one_output_per_r1_r2_conversions(harness: Path) -> None:
    conversions = np.full(design_fir.R1 * design_fir.R2 * 1000, 2114.0)
    out = _run(harness, conversions)
    # Nothing until the delay line has filled, then one per R1 x R2.
    assert len(out) == 1000 - (design_fir.NUM_TAPS // design_fir.R2) + 1
    assert set(out) == {2114.0}


@pytest.mark.parametrize("freq_hz", [20.0, 80.0, 150.0, 180.0])
def test_passband_tones_keep_the_designed_gain(harness: Path, freq_hz: float) -> None:
    out = _run(harness, _tone(freq_hz))
    taps = design_fir.design() / (1 << design_fir.Q)
    from scipy import signal  # type: ignore[import-untyped]

    _, fir = signal.freqz(taps, worN=[freq_hz], fs=design_fir.FS_STAGE1)
    x = np.pi * freq_hz * design_fir.R1 / design_fir.FS_ADC
    boxcar = np.sin(x) / (design_fir.R1 * np.sin(np.pi * freq_hz / design_fir.FS_ADC))
    expected = 400.0 * abs(fir[0]) * boxcar
    assert _amplitude(out, freq_hz) == pytest.approx(expected, rel=0.01)


@pytest.mark.parametrize("freq_hz", [320.0, 400.0, 700.0])
def test_stopband_tones_do_not_alias_into_the_output(harness: Path, freq_hz: float) -> None:
    out = _run(harness, _tone(freq_hz))
    alias = abs(freq_hz - design_fir.FS_OUT)  # where it would fold to
    # 400 counts in; what is left is the 12-bit rounding (< 1 count).
    assert _amplitude(out, alias) < 0.5
    assert np.ptp(out[len(out) // 4 :]) <= 2.0
