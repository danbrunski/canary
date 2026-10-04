"""Fuel-rail pulsation in a full-rate USB ADC capture, and the gauge step check.

Input: a session recorded with the ``fuel-pulsation`` capture profile
(``analog_full_rate``), so every v2 sample (firmware/usb-adc: 503.9 Hz,
anti-alias filtered to 180 Hz) is stored with its sequence number
``adc_seq``. Spectra are computed **by sample index**, never from timestamps:
the stamps are fitted, but USB delivers lines in bursts and a spectrum must
not depend on that.

Two questions, answered per steady-rpm band (windows whose rpm stays within
:data:`RPM_STEADY_FRACTION`):

1. **What is the pulsation?** The strongest lines of the pressure spectrum,
   each in Hz and in engine orders (cycles per crank revolution). A line that
   is the same *order* in every band is the engine's.
2. **Does the gauge step?** The AEM gauge regenerates its analog output at a
   rate of its own. If that is below the chain's ~180 Hz passband, the output
   is a staircase, and the energy of the sample-to-sample changes (``diff²``)
   peaks at the update rate in every band, whatever the rpm. So a line in the
   ``diff²`` spectrum at the same *Hz* across bands well apart in rpm is the
   step rate (or an alias of it). Pulsation puts its ``diff²`` lines at twice
   its own frequency, which moves with rpm.

An empty answer says which kind of empty it is (AGENTS.md): no steady band,
one band only (fixed Hz and fixed order cannot be told apart), or several
bands and no fixed line (no steps the chain can resolve: the update rate is
above ~180 Hz, or the output is smooth).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

#: firmware/usb-adc v2's output rate, nominal; the measured one is the slope
#: of the stamps over the sequence numbers.
NOMINAL_RATE_HZ = 503.905
WINDOW_SAMPLES = 2048
SEGMENT_SAMPLES = 1024
RPM_STEADY_FRACTION = 0.04
RPM_BAND_WIDTH = 300.0
#: A peak must stand this far above the spectrum's median to count.
PEAK_FACTOR = 8.0
TOP_PEAKS = 6
FIXED_LINE_TOLERANCE_HZ = 1.5
#: Two bands must be at least this far apart in rpm for a line common to both
#: to say "fixed frequency" rather than "the same order at nearly the same rpm".
MIN_RPM_SEPARATION = 600.0


class PulsationRefusedError(ValueError):
    """The session cannot answer the question, and why."""


@dataclass(frozen=True)
class Peak:
    hz: float
    order: float
    #: Root-mean-square amplitude of the line, in the channel's unit.
    rms: float


@dataclass
class Band:
    rpm: float
    windows: int
    pressure_peaks: list[Peak] = field(default_factory=list)
    change_peaks: list[Peak] = field(default_factory=list)


@dataclass
class PulsationReport:
    rate_hz: float
    samples: int
    bands: list[Band]
    #: Frequencies (Hz) the change-energy spectrum shows in bands far apart
    #: in rpm, with the rpm of every band they were seen in.
    step_lines: list[tuple[float, list[float]]]
    step_verdict: str


def full_rate_series(frame: pd.DataFrame, channel: str = "FUEL_PRESSURE") -> pd.DataFrame:
    """``seq``, ``value`` and ``timestamp`` of every stored sample, by sequence.

    Refuses a session stored as means (``adc_samples``) or by v1 firmware (no
    ``adc_seq``): neither has the samples this needs."""
    rows = frame[(frame["source"] == "sensor") & (frame["channel"] == channel)]
    if rows.empty:
        raise PulsationRefusedError(f"no sensor:{channel} rows in this session")
    meta = [json.loads(m) if isinstance(m, str) else {} for m in rows["metadata_json"]]
    if any("adc_samples" in m for m in meta):
        raise PulsationRefusedError(
            f"sensor:{channel} was stored as means of several samples; record the capture "
            "with the fuel-pulsation profile (analog_full_rate) to keep every sample"
        )
    seqs = [m.get("adc_seq") for m in meta]
    if any(s is None for s in seqs):
        raise PulsationRefusedError(
            f"sensor:{channel} has no adc_seq: recorded with the v1 firmware (100 Hz), "
            "whose samples cannot be placed by index"
        )
    out = pd.DataFrame(
        {
            "seq": np.asarray(seqs, dtype=np.int64),
            "value": rows["value"].astype(float).to_numpy(),
            "timestamp": rows["timestamp"].to_numpy(),
        }
    )
    return out.sort_values("seq").drop_duplicates("seq").reset_index(drop=True)


def measured_rate_hz(series: pd.DataFrame) -> float:
    """The output rate: sequence numbers per second of fitted stamp."""
    if len(series) < 2:
        return NOMINAL_RATE_HZ
    seconds = (series["timestamp"].iloc[-1] - series["timestamp"].iloc[0]) / pd.Timedelta(seconds=1)
    span = int(series["seq"].iloc[-1] - series["seq"].iloc[0])
    return span / seconds if seconds > 0 else NOMINAL_RATE_HZ


def _welch(x: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    """One-sided power spectral density, Hann segments with half overlap."""
    window = np.hanning(SEGMENT_SAMPLES)
    scale = fs * float(np.sum(window**2))
    step = SEGMENT_SAMPLES // 2
    spectra = []
    for start in range(0, len(x) - SEGMENT_SAMPLES + 1, step):
        seg = x[start : start + SEGMENT_SAMPLES]
        spectrum = np.abs(np.fft.rfft((seg - seg.mean()) * window)) ** 2 / scale
        spectrum[1:-1] *= 2
        spectra.append(spectrum)
    freqs = np.fft.rfftfreq(SEGMENT_SAMPLES, 1 / fs)
    return freqs, np.mean(spectra, axis=0)


def _peaks(freqs: np.ndarray, psd: np.ndarray, rpm: float, low_hz: float = 5.0) -> list[Peak]:
    """The strongest local maxima standing PEAK_FACTOR above the median."""
    df = float(freqs[1] - freqs[0])
    floor = float(np.median(psd[freqs > low_hz]))
    found = []
    for i in range(1, len(psd) - 1):
        if freqs[i] < low_hz or not (psd[i] > psd[i - 1] and psd[i] >= psd[i + 1]):
            continue
        if psd[i] < PEAK_FACTOR * floor:
            continue
        # The PSD integrates to power, and Hann spreads a line over +-2 bins.
        power = float(np.sum(psd[max(i - 2, 0) : i + 3]) * df)
        found.append(Peak(hz=float(freqs[i]), order=float(freqs[i] / (rpm / 60.0)), rms=power**0.5))
    found.sort(key=lambda p: p.rms, reverse=True)
    return found[:TOP_PEAKS]


def _rpm_by_seq(series: pd.DataFrame, rpm: pd.DataFrame) -> np.ndarray:
    """rpm at each sample: the latest rpm reading at or before its stamp."""
    if rpm.empty:
        return np.full(len(series), np.nan)
    rpm = rpm.sort_values("timestamp")
    joined = pd.merge_asof(
        series[["timestamp"]].reset_index(),
        rpm[["timestamp", "value"]].rename(columns={"value": "rpm"}),
        on="timestamp",
        direction="backward",
        tolerance=pd.Timedelta(seconds=1),
    ).sort_values("index")
    return joined["rpm"].to_numpy(dtype=float)


def analyse(series: pd.DataFrame, rpm: pd.DataFrame) -> PulsationReport:
    """Bands of steady rpm, each with its pressure and change-energy lines,
    and the step verdict."""
    fs = measured_rate_hz(series)
    values = series["value"].to_numpy(dtype=float)
    seqs = series["seq"].to_numpy()
    rpm_at = _rpm_by_seq(series, rpm)
    # Windows never span a gap in the sequence: a gap breaks the index time base.
    breaks = np.flatnonzero(np.diff(seqs) != 1) + 1
    by_band: dict[float, list[tuple[np.ndarray, float]]] = {}
    for run in np.split(np.arange(len(seqs)), breaks):
        for start in range(0, len(run) - WINDOW_SAMPLES + 1, WINDOW_SAMPLES // 2):
            idx = run[start : start + WINDOW_SAMPLES]
            r = rpm_at[idx]
            if np.isnan(r).any() or r.mean() < 300:
                continue
            if (r.max() - r.min()) > RPM_STEADY_FRACTION * r.mean():
                continue
            band = round(r.mean() / RPM_BAND_WIDTH) * RPM_BAND_WIDTH
            by_band.setdefault(band, []).append((values[idx], float(r.mean())))

    bands = []
    for band_rpm in sorted(by_band):
        windows = by_band[band_rpm]
        mean_rpm = float(np.mean([w[1] for w in windows]))
        pressure = [_welch(w[0], fs) for w in windows]
        change = [_welch(np.diff(w[0]) ** 2, fs) for w in windows]
        freqs = pressure[0][0]
        bands.append(
            Band(
                rpm=mean_rpm,
                windows=len(windows),
                pressure_peaks=_peaks(freqs, np.mean([p[1] for p in pressure], axis=0), mean_rpm),
                change_peaks=_peaks(
                    change[0][0], np.mean([c[1] for c in change], axis=0), mean_rpm
                ),
            )
        )
    step_lines, verdict = _step_verdict(bands)
    return PulsationReport(
        rate_hz=fs, samples=len(series), bands=bands, step_lines=step_lines, step_verdict=verdict
    )


#: A four-stroke's events fall on half-orders; a change-energy line within
#: this fraction of one is the engine's, not the gauge's, in that band.
ORDER_TOLERANCE = 0.02


def _on_engine_order(peak: Peak) -> bool:
    nearest = round(peak.order * 2) / 2
    return nearest > 0 and abs(peak.order - nearest) <= ORDER_TOLERANCE * nearest


def _step_verdict(bands: list[Band]) -> tuple[list[tuple[float, list[float]]], str]:
    if not bands:
        return [], (
            "no answer: no window of steady rpm (within "
            f"{RPM_STEADY_FRACTION:.0%} for {WINDOW_SAMPLES} samples); hold rpm steady"
        )
    if len(bands) < 2 or bands[-1].rpm - bands[0].rpm < MIN_RPM_SEPARATION:
        return [], (
            "no answer: steady rpm in one band only, so a line fixed in Hz cannot be told "
            f"from an engine order; hold two speeds at least {MIN_RPM_SEPARATION:.0f} rpm apart"
        )
    # Each line: its frequency, and every band it was seen in with whether it
    # sat on an engine half-order there.
    lines: list[tuple[float, list[tuple[float, bool]]]] = []
    for band in bands:
        for peak in band.change_peaks:
            sighting = (band.rpm, _on_engine_order(peak))
            for i, (hz, seen) in enumerate(lines):
                if abs(hz - peak.hz) <= FIXED_LINE_TOLERANCE_HZ:
                    seen.append(sighting)
                    lines[i] = ((hz * (len(seen) - 1) + peak.hz) / len(seen), seen)
                    break
            else:
                lines.append((peak.hz, [sighting]))
    # Fixed in Hz across bands far apart in rpm, and not an engine order in at
    # least one of them: in commensurate bands a step line can sit on an order
    # by coincidence, but not in every band at once.
    fixed = [
        (round(hz, 1), [rpm for rpm, _ in seen])
        for hz, seen in lines
        if len(seen) >= 2
        and max(r for r, _ in seen) - min(r for r, _ in seen) >= MIN_RPM_SEPARATION
        and not all(on_order for _, on_order in seen)
    ]
    if fixed:
        # The update rate shows in the most bands; the rest are its images
        # (a held output mixes the pulsation with the update rate).
        fixed.sort(key=lambda line: len(line[1]), reverse=True)
        head_hz, head_seen = fixed[0]
        others = ", ".join(f"{hz:g}" for hz, _ in fixed[1:])
        return fixed, (
            f"steps: the change energy peaks at {head_hz:g} Hz in {len(head_seen)} bands "
            f"{MIN_RPM_SEPARATION:.0f}+ rpm apart, the signature of an output updated at "
            "that rate (or an alias of it)"
            + (f"; also {others} Hz, likely its images" if others else "")
            + ". Pulsation near or above it is the gauge's staircase, not the rail."
        )
    return [], (
        f"no steps resolved across {len(bands)} bands: the gauge updates faster than the "
        "chain's ~180 Hz passband, or its output is smooth"
    )


def format_report(report: PulsationReport) -> str:
    lines = [
        f"{report.samples} samples at {report.rate_hz:.3f} Hz (measured)",
        f"step check: {report.step_verdict}",
    ]
    for band in report.bands:
        lines.append(f"\n{band.rpm:.0f} rpm ({band.windows} windows)")
        for peak in band.pressure_peaks:
            lines.append(
                f"  pressure {peak.hz:7.1f} Hz  order {peak.order:5.2f}  {peak.rms:.3f} rms"
            )
        for peak in band.change_peaks:
            lines.append(f"  change   {peak.hz:7.1f} Hz  order {peak.order:5.2f}")
    return "\n".join(lines)


__all__ = [
    "NOMINAL_RATE_HZ",
    "Band",
    "Peak",
    "PulsationRefusedError",
    "PulsationReport",
    "analyse",
    "format_report",
    "full_rate_series",
    "measured_rate_hz",
]
