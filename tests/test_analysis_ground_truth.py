"""Regression suite: the analysis chain must reproduce what we already know.

Every finding this project retracted — a byte bound on matching means, a rate
computed against wall-clock, a session judged to be cycling from cumulative
counters — looked convincing in isolation, and none was checked against a case
with a known answer before being acted on.

This is that check. It runs the real chain (parquet -> binding decode ->
alignment -> comparison) over a committed slice of a real drive and asserts it
still lands on the answers three independent instruments already agree about.
A tool whose novel output you want to trust should be able to pass here first.

The expectations live in ``expectations.yaml``, so recording a new known fact
is editing data rather than writing another test. The fixture is real bytes
off the wire — synthesising one would only prove the chain agrees with the
assumptions used to build it, which is the circularity this exists to break.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from src.analysis.broadcast_discovery import credible_fits, sweep_session
from src.analysis.compare import PairingPolicy, compare_channels
from src.decode.bindings import load_bindings
from src.preprocessing.loader import read_session

BINDINGS_PATH = Path("data/bindings.yaml")
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "ground_truth"
EXPECTATIONS = yaml.safe_load((FIXTURE_ROOT / "expectations.yaml").read_text())
SESSION = FIXTURE_ROOT / EXPECTATIONS["fixture"]

#: Acquisition tolerance for pairing a ~1 Hz Mode 01 PID against the fixture's
#: 10:1-decimated broadcast frames. Measured on the slice (2026-09-06): frames
#: arrive every 102 ms median / 170 ms p95 / ~0.5-0.7 s max across decimation
#: gaps, PIDs every 1.11 s median. The nearest frame to a PID reading is
#: 26-31 ms away at the median, 50 ms at p95, 144 ms at the worst — so 250 ms
#: admits every honest nearest-frame pairing (100 ms left one wheel-speed and
#: one coolant reading unpaired) while staying under a quarter of the PID
#: cadence, so no reading can be paired to a frame nearer another reading.
POLICY = PairingPolicy.within("250ms")


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    return read_session(SESSION)


def _ids(cases: list[dict[str, Any]]) -> list[str]:
    return [f"{case['signal']}~{case['reference']}" for case in cases]


@pytest.mark.parametrize(
    "pin", EXPECTATIONS["catalog_pins"], ids=[p["name"] for p in EXPECTATIONS["catalog_pins"]]
)
def test_catalog_decode_parameters_are_pinned(pin: dict[str, Any]) -> None:
    """Catch a careless catalog edit exactly, where the empirical check cannot.

    ``slope`` is a ratio, so a +4% scale error shows up as only a -2.9% slope
    error — measured 2026-08-06, and it slipped a 3% tolerance. Tightening the
    tolerance far enough to catch it would make the empirical check fragile
    against honest window changes, so the exact values are pinned here instead
    and the two checks cover each other's gaps.
    """
    signal = next(s for s in load_bindings(BINDINGS_PATH).signals if s.name == pin["name"])

    assert signal.can_id == pin["can_id"]
    assert signal.byte_start == pin["byte_start"]
    assert signal.byte_length == pin["byte_length"]
    assert signal.byte_order == pin["byte_order"]
    assert signal.scale == pytest.approx(pin["scale"])
    assert (signal.offset or 0.0) == pytest.approx(pin["offset"])


@pytest.mark.parametrize(
    "case", EXPECTATIONS["unit_agreement"], ids=_ids(EXPECTATIONS["unit_agreement"])
)
def test_binding_agrees_with_its_independent_reference(
    case: dict[str, Any], frame: pd.DataFrame
) -> None:
    """A correct decode and its Mode 01 counterpart must agree 1:1.

    Both sides are in engineering units, so the assertion is the 1:1
    hypothesis itself — and it fails on a wrong byte offset, wrong
    endianness, wrong scale, wrong offset, or wrong unit without anyone
    having to write down what the right answer was. The PID is the answer.

    Two statistics carry it. The residual of ``b - a`` per pair
    (``difference_bias``/``difference_spread``) is the load-bearing one: it
    is in engineering units, a wrong scale or offset shifts it directly, and
    it does not degrade when a signal is quantised on a narrow range. The
    fitted slope is kept as a second, scale-free check; it is weak exactly
    where the residual is strong (see the coolant note in expectations.yaml).
    """
    result = compare_channels(frame, case["signal"], case["reference"], policy=POLICY)

    assert abs(result.difference_bias) <= case["max_residual_bias"], (
        f"{result.describe()} — mean b-a is {result.difference_bias:+.3f} in the "
        "reference's units; a correct decode sits at zero"
    )
    assert result.difference_spread <= case["max_residual_spread"], (
        f"{result.describe()} — b-a spread {result.difference_spread:.3f} exceeds "
        "what sampling offset and quantisation explain on this fixture"
    )
    assert result.r_squared >= case["min_r_squared"], result.describe()
    scale_error_pct = abs(result.slope - 1.0) * 100.0
    assert scale_error_pct <= case["max_scale_error_pct"], (
        f"{result.describe()} — decoded value is {scale_error_pct:.2f}% off its "
        f"reference; expected the two to agree 1:1"
    )
    assert abs(result.offset) <= case["max_offset"], result.describe()


@pytest.mark.parametrize(
    "case",
    EXPECTATIONS["byte_relationships"],
    ids=[
        f"0x{c['a']['can_id']:03X}:{c['a']['byte']}~0x{c['b']['can_id']:03X}:{c['b']['byte']}"
        for c in EXPECTATIONS["byte_relationships"]
    ],
)
def test_exact_byte_relationships_still_hold(case: dict[str, Any], frame: pd.DataFrame) -> None:
    """Relationships that held to sd = 0.000, so any drift is a real change.

    Deliberately below the decode layer: these compare raw wire bytes, so they
    keep working — and keep being meaningful — even if a scale or offset in the
    catalog is wrong.
    """

    def byte_frame(can_id: int, index: int, name: str) -> pd.DataFrame:
        channel = f"0x{can_id:03X}".upper()
        rows = frame[(frame["source"] == "can") & (frame["channel"].str.upper() == channel)]
        rows = rows[rows["payload"].map(lambda p: isinstance(p, (bytes, bytearray)))]
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(rows["timestamp"].to_numpy(), utc=True),
                name: [bytes(payload)[index] for payload in rows["payload"]],
            }
        ).sort_values("timestamp")

    # Nearest-frame pairing, not binning. Averaging within a bin would take the
    # mean of two *different* integers around a transition and produce a value
    # that was never on the wire — which is the aggregate substitution this
    # whole suite exists to catch, and it does show up here: binning at 1 s
    # turns an exact relationship into one with sd 0.076.
    joined = pd.merge_asof(
        byte_frame(case["a"]["can_id"], case["a"]["byte"], "a"),
        byte_frame(case["b"]["can_id"], case["b"]["byte"], "b"),
        on="timestamp",
        direction="nearest",
        tolerance=pd.Timedelta("100ms"),
    ).dropna()

    assert len(joined) > 100, "fixture no longer covers both frames"
    difference = joined["a"] - joined["b"]
    deviation = (difference - case["delta"]).abs()

    # Median rather than mean: the statistic must not be movable by the handful
    # of transition-edge samples the decimation introduces.
    assert difference.median() == case["delta"]
    assert deviation.max() <= case["max_deviation"], (
        f"bytes diverged by {deviation.max():.0f} counts — more than the one "
        "count a sampling offset at a transition can explain"
    )
    assert (difference == case["delta"]).mean() >= case["min_exact_fraction"]


def test_the_sweep_rediscovers_a_binding_it_was_not_told_about(frame: pd.DataFrame) -> None:
    """Discovery must find the known answer before its novel output is trusted.

    ``ECM_RPM`` at 0x1F9 bytes 2-3, scale 0.125, is in the catalog — but the
    sweep is never shown it. Handing it only raw frames and an rpm reference
    must rank that binding, at that scale, first. Without this, a sweep that
    had silently regressed would still emit a ranked, confident-looking list
    of new "findings".

    ``include_unresolved`` is on because this fixture is seven minutes long:
    the sub-window slopes spread 1.55%, just past the stability bar, so the
    honest verdict on a slice this short is "cannot settle it" rather than
    "stable". That is the check working, not failing — but the binding must
    never be called ``drifting``, which is asserted separately.
    """
    fits = sweep_session(frame, ["obd:RPM"], can_ids=[0x1F9], policy=POLICY, min_pairs=100)
    ranked = credible_fits(fits, min_r_squared=0.98, include_unresolved=True)

    assert ranked, "sweep found nothing on a frame carrying a confirmed binding"
    best = ranked[0]
    assert best.probe.byte_start == 2
    assert best.probe.byte_length == 2
    assert best.probe.byte_order == "big"
    # raw = rpm x 8, so recovering rpm from raw is x 0.125.
    assert best.slope == pytest.approx(0.125, rel=0.03)
    assert best.scale_verdict != "drifting", (
        f"the confirmed rpm binding was judged unstable: {best.describe()}"
    )


def test_the_sweep_does_not_manufacture_bindings_from_noise(frame: pd.DataFrame) -> None:
    """A reference unrelated to a frame must yield nothing credible.

    The counterpart to rediscovery: a sweep that finds the right answer but
    also invents wrong ones is not usable. Random noise shares no scale with
    any byte, so the stability check must clear the field.
    """
    rng = np.random.default_rng(20260806)
    noise_rows = frame[(frame["source"] == "obd") & (frame["channel"] == "RPM")].copy()
    noise_rows["channel"] = "SPEED"
    noise_rows["value_number"] = rng.uniform(0.0, 100.0, len(noise_rows))
    polluted = pd.concat([frame[frame["channel"] != "SPEED"], noise_rows], ignore_index=True)

    fits = sweep_session(polluted, ["obd:SPEED"], can_ids=[0x1F9], policy=POLICY, min_pairs=100)

    assert credible_fits(fits, min_r_squared=0.90) == []
