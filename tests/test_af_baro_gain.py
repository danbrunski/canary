"""Known answer for the voltage route's barometric gain (B06.7).

PID 0x24 and DID 0x1225 are one A/F sensor: PID 0x24 is the ECM's lambda
computed from the DID word after a barometric gain about stoich, its A/F table,
a learned rear-O2 offset and an rpm x BFS offset (afr_calibration.yaml
``baro_gain`` / ``voltage_residual_sources``). So the gain-applied voltage route
must agree with PID 0x24 to within what it still leaves out, and the gain-1
route must not -- it reads lean, more so the richer the mixture.

Data: the committed 2026-09-21T223734 tuning slice (PID 0x24 and DID 0x1225)
plus its PID 0x33 rows (``tests/fixtures/af_baro_gain``), 80-81 kPa.

Method, per sample (never an aggregate of either side):

* pairs: :func:`src.analysis.compare.align_pairs`, PID 0x24 against the DID on
  the acquisition clock within the declaration's 0.25 s
  (median 83 ms apart on this capture);
* steady: the PID 0x24 reads immediately before and after the DID read span at
  most the enrichment rule's dwell (0.6 s) and differ by at most its step
  tolerance (0.01 lambda), so lambda barely moved across the pair;
* per pair, ``d = AFR_route - AFR_PID``; per band the median of ``d``.

The tolerance comes from the declared terms, not from the data's fit:

* **learned rear-O2 offset** (+/-1 AFR, unobservable): it is a constant added
  in AFR, and the gain is the identity at stoich (x2 = 880), so it is measured
  where the gain contributes nothing -- the stoich band (PID lambda 0.985-1.000)
  -- and differenced out: ``delta = median(d_band) - median(d_stoich)``. Its
  +/-1 AFR clamp is 2-3x the gain's own effect and cannot be the tolerance;
* **rpm x BFS offset**: the table range for the bank, as a spread between two
  cells (bank 1: 0.088 AFR);
* **transfer reproduction**: none since 2026-10-07 -- the transfer is table
  0xE644 itself (a ``rom_table`` transfer, refused on load if it differs);
* **k resolution**: PID 0x33 is floored to 1 kPa (+/-0.5 kPa about the
  midpoint used) and k is a byte (one count, rounding undecoded), mapped to AFR
  at the band's richest edge;
* **pairing skew and read noise**: the 99 % bootstrap half-width of ``delta``
  (seeded). A steady pair bounds one pair's skew at 0.5 % lambda; a median of
  many is bounded by its sampling interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis.compare import align_pairs
from src.analysis.lambda_delivery import load_lambda_delivery
from src.preprocessing.loader import read_session
from src.signals.afr import AfrCalibration, BaroSamples, load_afr_calibration, voltage_to_afr
from src.signals.pairing import PairingPolicy, to_seconds

PROFILE = Path("data/nissan-xterra-2008")
SLICE = Path("tests/fixtures/lambda_delivery/_capture-tuning-2026-09-21T223734")
BARO_SLICE = Path("tests/fixtures/af_baro_gain/_capture-tuning-2026-09-21T223734")
STOICH = 14.7
#: The gain is the identity at x2 = 880; the reference band sits rich of the
#: transfer's 2.2 V knot, where the curve reproduces table 0xE644 to 0.04 %.
STOICH_BAND = (0.985, 1.000)
RICH_BANDS = ((0.80, 0.85), (0.85, 0.90))
BOOTSTRAP = 2000
#: Fewest steady pairs a band needs before its median is asserted on.
MIN_PAIRS = 5
SESSIONS = Path.home() / "canary-data"


@pytest.fixture(scope="module")
def calibration() -> AfrCalibration:
    return load_afr_calibration(PROFILE / "afr_calibration.yaml")


def _session(*dirs: Path) -> pd.DataFrame:
    rows = pd.concat(
        [read_session(d, filters=[("source", "==", "obd")]) for d in dirs], ignore_index=True
    )
    return rows.sort_values("timestamp", kind="stable").reset_index(drop=True)


@dataclass(frozen=True)
class Pairs:
    pid_lambda: np.ndarray
    route_afr: np.ndarray
    gain1_afr: np.ndarray
    #: The route at half the ECM's gain correction (1 + (g - 1) / 2): what the
    #: per-sample ratio test must be able to tell from the real gain.
    half_gain_afr: np.ndarray
    baro_kpa: np.ndarray


def steady_pairs(
    session: pd.DataFrame | Path, calibration: AfrCalibration, *, bank: int = 1
) -> Pairs:
    declaration = load_lambda_delivery(PROFILE / "lambda_delivery.yaml")
    rule = declaration.eligibility.regimes.enrichment
    pid = declaration.measured(f"ecm_lambda_pid_0x{24 if bank == 1 else 28}").spec
    did = declaration.measured(f"af_voltage_b{bank}").spec
    assert declaration.sensor_of(f"af_voltage_b{bank}") == declaration.sensor_of(
        f"ecm_lambda_pid_0x{24 if bank == 1 else 28}"
    )
    tolerance = pd.Timedelta(seconds=declaration.acquisition.max_separation_s)
    aligned = align_pairs(session, pid, did, policy=PairingPolicy.within(tolerance))
    pairs = aligned.pairs
    from src.analysis.channels import read_channels

    long = read_channels(session, [pid, calibration.baro_gain.baro_channel], acquired_time=True)  # type: ignore[union-attr]
    pid_rows = long[long["spec"] == pid].sort_values("timestamp", kind="stable")
    pid_t = to_seconds(pid_rows["timestamp"])
    pid_v = pd.to_numeric(pid_rows["value"], errors="coerce").to_numpy(dtype=float)
    did_t = to_seconds(pairs["timestamp_b"])
    after = np.clip(np.searchsorted(pid_t, did_t, side="left"), 1, len(pid_t) - 1)
    before = after - 1
    steady = (
        (pid_t[before] <= did_t)
        & (pid_t[after] >= did_t)
        & (pid_t[after] - pid_t[before] <= rule.steady_target_dwell_s)
        & (np.abs(pid_v[after] - pid_v[before]) <= rule.target_step_tolerance)
        & (pairs["a"].to_numpy(dtype=float) < 1.9)
    )
    pairs = pairs[steady]
    baro_long = long[long["spec"] == calibration.baro_gain.baro_channel]  # type: ignore[union-attr]
    baro = BaroSamples.from_long(to_seconds(pairs["timestamp_b"]), baro_long)
    volts = pairs["b"].reset_index(drop=True)
    route = voltage_to_afr(volts, calibration, baro=baro)
    assert calibration.voltage_transfer is not None
    keep = route.afr.notna().to_numpy()
    return Pairs(
        pid_lambda=pairs["a"].to_numpy(dtype=float)[keep],
        route_afr=route.afr.to_numpy()[keep],
        gain1_afr=calibration.voltage_transfer.curve_afr(volts.to_numpy(dtype=float))[keep],
        half_gain_afr=_half_gain(volts.to_numpy(dtype=float), route.gain.to_numpy(), calibration)[
            keep
        ],
        baro_kpa=baro.kpa[keep],
    )


def _half_gain(volts: np.ndarray, gain: np.ndarray, calibration: AfrCalibration) -> np.ndarray:
    transfer, table = calibration.voltage_transfer, calibration.baro_gain
    assert transfer is not None and table is not None and transfer.volts_per_count is not None
    per_count, stoich = transfer.volts_per_count, float(table.stoich_x2_counts)
    half = 1.0 + (gain - 1.0) / 2.0
    corrected = np.maximum(0.0, stoich + (volts / per_count - stoich) * half) * per_count
    return transfer.curve_afr(np.nan_to_num(corrected))


def ratio_median_pct(pairs: Pairs, afr: np.ndarray, band: tuple[float, float]) -> float:
    """Median over the band's pairs of ``route / PID - 1``, percent (per sample)."""
    rich = _band(pairs, *band)
    return float(np.median(afr[rich] / (pairs.pid_lambda[rich] * STOICH) - 1.0) * 100.0)


def _band(pairs: Pairs, low: float, high: float) -> np.ndarray:
    return (pairs.pid_lambda >= low) & (pairs.pid_lambda < high)


@dataclass(frozen=True)
class BandResult:
    n: int
    delta_route: float
    delta_gain1: float
    predicted_gain1: float
    low: float
    high: float
    terms: dict[str, float]


def band_result(
    pairs: Pairs, calibration: AfrCalibration, band: tuple[float, float], *, bank: int = 1
) -> BandResult:
    transfer, gain = calibration.voltage_transfer, calibration.baro_gain
    assert transfer is not None and gain is not None
    stoich = _band(pairs, *STOICH_BAND)
    rich = _band(pairs, *band)
    pid_afr = pairs.pid_lambda * STOICH
    d_route = pairs.route_afr - pid_afr
    d_gain1 = pairs.gain1_afr - pid_afr
    delta = float(np.median(d_route[rich]) - np.median(d_route[stoich]))
    delta_gain1 = float(np.median(d_gain1[rich]) - np.median(d_gain1[stoich]))
    predicted = float(
        np.median((pairs.gain1_afr - pairs.route_afr)[rich])
        - np.median((pairs.gain1_afr - pairs.route_afr)[stoich])
    )
    # rpm x BFS offset: PID carries +cell, the route does not; differencing
    # leaves the spread between two cells of the bank's table.
    low_cell, high_cell = calibration.residual_source("rpm_bfs_offset").bounds_afr[bank]
    spread = high_cell - low_cell
    # Transfer reproduction: none. Since 2026-10-07 the transfer IS table
    # 0xE644 (a rom_table transfer, checked equal to it on load).
    provenance = transfer.provenance
    assert provenance is not None and provenance.method == "rom_table"
    per_count = provenance.volts_per_count
    assert per_count is not None
    x2 = np.arange(0.0, 1961.0)
    ecm = transfer.curve_afr(x2 * per_count)
    # k resolution at the band's richest edge: PID floor +/- half a kPa through
    # the table's local slope, plus the byte's own count.
    kpa = float(np.median(pairs.baro_kpa))
    k = float(gain.k_at(np.array([kpa]))[0])
    dk_dkpa = float(gain.k_at(np.array([kpa + 0.5]))[0] - gain.k_at(np.array([kpa - 0.5]))[0])
    dk = gain.k_resolution + abs(dk_dkpa) * gain.pid_resolution_kpa / 2.0
    x2_edge = np.interp(band[0] * STOICH, ecm, x2)
    slope = float(np.interp(x2_edge + 1, x2, ecm) - np.interp(x2_edge, x2, ecm))
    k_term = abs(slope) * abs(gain.stoich_x2_counts - x2_edge) * dk / k
    # Sampling (pairing skew and read noise): 99 % bootstrap of delta.
    rng = np.random.default_rng(0)
    rich_d, stoich_d = d_route[rich], d_route[stoich]
    boots = np.array(
        [
            np.median(rng.choice(rich_d, len(rich_d)))
            - np.median(rng.choice(stoich_d, len(stoich_d)))
            for _ in range(BOOTSTRAP)
        ]
    )
    skew = float(max(np.quantile(boots, 0.995) - delta, delta - np.quantile(boots, 0.005)))
    terms = {
        "rpm_bfs_spread": spread,
        "k_resolution": k_term,
        "sampling": skew,
    }
    high = spread + k_term + skew
    low = -spread - k_term - skew
    return BandResult(int(rich.sum()), delta, delta_gain1, predicted, low, high, terms)


@pytest.fixture(scope="module")
def fixture_pairs(calibration: AfrCalibration) -> Pairs:
    return steady_pairs(_session(SLICE, BARO_SLICE), calibration)


def test_the_fixture_reads_the_altitude_the_gain_is_for(fixture_pairs: Pairs) -> None:
    assert set(np.unique(fixture_pairs.baro_kpa)) <= {80.0, 81.0}
    assert int(_band(fixture_pairs, *STOICH_BAND).sum()) == 434


@pytest.mark.parametrize("band", RICH_BANDS, ids=lambda b: f"{b[0]:.2f}-{b[1]:.2f}")
def test_the_gain_applied_route_agrees_with_pid_0x24_within_the_declared_terms(
    fixture_pairs: Pairs, calibration: AfrCalibration, band: tuple[float, float]
) -> None:
    result = band_result(fixture_pairs, calibration, band)
    assert result.n >= MIN_PAIRS
    assert result.low <= result.delta_route <= result.high, result


@pytest.mark.parametrize("band", RICH_BANDS, ids=lambda b: f"{b[0]:.2f}-{b[1]:.2f}")
def test_negative_control_the_gain_1_route_disagrees_by_the_predicted_amount(
    fixture_pairs: Pairs, calibration: AfrCalibration, band: tuple[float, float]
) -> None:
    """Without the gain the route reads lean, outside the same tolerance, by
    about what the firmware arithmetic predicts. A missing gain fails here."""
    result = band_result(fixture_pairs, calibration, band)
    assert result.delta_gain1 > result.high, result
    assert result.predicted_gain1 + result.low <= result.delta_gain1
    assert result.delta_gain1 <= result.predicted_gain1 + result.high


def test_the_gain_1_bias_grows_richer_of_stoich(
    fixture_pairs: Pairs, calibration: AfrCalibration
) -> None:
    near = band_result(fixture_pairs, calibration, (0.95, 0.985)).delta_gain1
    mid = band_result(fixture_pairs, calibration, (0.85, 0.90)).delta_gain1
    rich = band_result(fixture_pairs, calibration, (0.80, 0.85)).delta_gain1
    assert 0 < near < mid < rich


def ratio_bounds_pct(
    pairs: Pairs, calibration: AfrCalibration, band: tuple[float, float], *, bank: int = 1
) -> tuple[float, float]:
    """Where the gain-applied route's ratio median may sit, percent.

    Not differenced, so it carries the rear-O2 offset at its value measured
    at stoich on this drive (where the gain contributes nothing) and the rpm x
    BFS offset at its one-sided table bounds (PID carries ``+cell``), over the
    band's richest AFR, plus the 99 % bootstrap of the band's median.
    """
    stoich = _band(pairs, *STOICH_BAND)
    rear = abs(ratio_median_pct(pairs, pairs.route_afr, STOICH_BAND)) if stoich.any() else 0.0
    low_cell, high_cell = calibration.residual_source("rpm_bfs_offset").bounds_afr[bank]
    afr = band[0] * STOICH
    rich = _band(pairs, *band)
    ratios = (pairs.route_afr[rich] / (pairs.pid_lambda[rich] * STOICH) - 1.0) * 100.0
    rng = np.random.default_rng(0)
    boots = np.array([np.median(rng.choice(ratios, len(ratios))) for _ in range(BOOTSTRAP)])
    centre = float(np.median(ratios))
    sampling = float(max(np.quantile(boots, 0.995) - centre, centre - np.quantile(boots, 0.005)))
    return (
        -100.0 * high_cell / afr - rear - sampling,
        -100.0 * low_cell / afr + rear + sampling,
    )


@pytest.mark.parametrize("band", RICH_BANDS, ids=lambda b: f"{b[0]:.2f}-{b[1]:.2f}")
def test_the_per_sample_ratio_separates_a_half_size_gain(
    fixture_pairs: Pairs, calibration: AfrCalibration, band: tuple[float, float]
) -> None:
    """The differenced test above catches a missing gain but not a half-size
    one (half the gain moves delta by ~0.10 AFR at 0.85-0.90, inside its
    0.14 AFR tolerance). The undifferenced per-sample ratio median does: the
    gain-applied route sits within its bounds, half the gain and no gain do not."""
    low, high = ratio_bounds_pct(fixture_pairs, calibration, band)
    applied = ratio_median_pct(fixture_pairs, fixture_pairs.route_afr, band)
    half = ratio_median_pct(fixture_pairs, fixture_pairs.half_gain_afr, band)
    gain1 = ratio_median_pct(fixture_pairs, fixture_pairs.gain1_afr, band)
    assert low <= applied <= high, (low, applied, high)
    assert half > high and gain1 > half, (high, half, gain1)


def test_the_known_answer_numbers(fixture_pairs: Pairs, calibration: AfrCalibration) -> None:
    """Pinned so a change to the route or the data shows up as a number.

    AFR, band median minus stoich-band median (PID lambda 0.985-1.000, 434
    pairs), on table 0xE644 since 2026-10-07. Tolerance on this slice:
    0.80-0.85 +/-0.214, 0.85-0.90 +/-0.138. Per-sample ratio medians (route /
    PID - 1): gain applied +0.01 % / +0.04 %, half gain +1.31 % / +0.98 %,
    gain 1 +2.63 % / +1.75 %."""
    rich = band_result(fixture_pairs, calibration, (0.80, 0.85))
    mid = band_result(fixture_pairs, calibration, (0.85, 0.90))
    assert (rich.n, mid.n) == (8, 139)
    assert rich.delta_route == pytest.approx(-0.0025, abs=0.001)
    assert mid.delta_route == pytest.approx(0.0022, abs=0.001)
    assert rich.delta_gain1 == pytest.approx(0.3027, abs=0.001)
    assert mid.delta_gain1 == pytest.approx(0.2064, abs=0.001)
    medians = {
        name: [ratio_median_pct(fixture_pairs, afr, band) for band in RICH_BANDS]
        for name, afr in (
            ("applied", fixture_pairs.route_afr),
            ("half", fixture_pairs.half_gain_afr),
            ("gain1", fixture_pairs.gain1_afr),
        )
    }
    assert medians["applied"] == pytest.approx([0.01, 0.044], abs=0.01)
    assert medians["half"] == pytest.approx([1.307, 0.975], abs=0.01)
    assert medians["gain1"] == pytest.approx([2.629, 1.752], abs=0.01)


#: Sessions at a different baro, or not in the fixture: the gain must hold there.
OTHER_SESSIONS = {
    "held_out_same_altitude": SESSIONS
    / "sessions"
    / "nissan-xterra-2008"
    / "_capture-tuning-2026-09-23T142926",
    "09-17_76kPa": SESSIONS / "sessions-0917" / "_capture-tuning-hybrid-research-2026-09-17T191712",
    "09-18_75-79kPa": SESSIONS / "sessions-0918" / "2026-09-18T213818",
}


@pytest.mark.slow
@pytest.mark.parametrize("name", list(OTHER_SESSIONS))
def test_the_gain_holds_on_other_sessions(name: str, calibration: AfrCalibration) -> None:
    path = OTHER_SESSIONS[name]
    if not path.exists():
        pytest.skip(f"{path} is not on this machine")
    session = _session(path)
    if not (session["channel"] == "BAROMETRIC_PRESSURE").any():
        pytest.skip(f"{path.name} logged no PID 0x33: the voltage route has no AFR there")
    pairs = steady_pairs(session, calibration)
    if not len(pairs.pid_lambda):
        pytest.skip(f"{path.name}: no steady PID 0x24 / DID 0x1225 pair with baro as of it")
    usable = [
        band
        for band in RICH_BANDS
        if _band(pairs, *band).sum() >= MIN_PAIRS and _band(pairs, *STOICH_BAND).sum() >= MIN_PAIRS
    ]
    if not usable:
        pytest.skip(
            f"{path.name}: fewer than {MIN_PAIRS} steady pairs in every rich band "
            f"(baro {np.nanmin(pairs.baro_kpa):.0f}-{np.nanmax(pairs.baro_kpa):.0f} kPa)"
        )
    for band in usable:
        result = band_result(pairs, calibration, band)
        assert result.low <= result.delta_route <= result.high, (band, result)
        assert result.delta_gain1 > result.high, (band, result)
