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
from src.analysis.compare import (
    InputNotCapturedError,
    PairingPolicy,
    align_pairs,
    compare_channels,
)
from src.decode.bindings import load_bindings
from src.decode.dids import load_did_map
from src.preprocessing.loader import read_session
from src.storage.broadcast_storage import ChangeOnlyStorageError, change_only_copy

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


@pytest.fixture(scope="module")
def change_only_frame(tmp_path_factory: pytest.TempPathFactory) -> pd.DataFrame:
    """The same slice as the recorder stores it since P02.10: a CAN frame only
    when its payload changes, an unchanged id re-written once per timeout.
    Made by the real filter into a temporary copy; the fixture is only read."""
    copy = tmp_path_factory.mktemp("change-only") / SESSION.name
    read, written = change_only_copy(SESSION, copy)
    assert written < read
    return read_session(copy)


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
    "case", EXPECTATIONS["unit_agreement"], ids=_ids(EXPECTATIONS["unit_agreement"])
)
def test_the_one_to_one_check_holds_on_the_change_only_copy(
    case: dict[str, Any], frame: pd.DataFrame, change_only_frame: pd.DataFrame
) -> None:
    """P02.10: stored change-only, the same slice gives the same answer.

    Between changes the broadcast value is held inside the reader (the last
    stored row, valid for the stale timeout), so each PID reading still finds
    its broadcast partner: the same thresholds hold and the verdict is the one
    the full-rate slice gets.
    """
    full = compare_channels(frame, case["signal"], case["reference"], policy=POLICY)
    result = compare_channels(change_only_frame, case["signal"], case["reference"], policy=POLICY)

    assert result.scale_verdict == full.scale_verdict, result.describe()
    assert result.n_pairs >= full.n_pairs - 2, (result.describe(), full.describe())
    assert abs(result.difference_bias) <= case["max_residual_bias"], result.describe()
    assert result.difference_spread <= case["max_residual_spread"], result.describe()
    assert result.r_squared >= case["min_r_squared"], result.describe()
    assert abs(result.slope - 1.0) * 100.0 <= case["max_scale_error_pct"], result.describe()
    assert abs(result.offset) <= case["max_offset"], result.describe()


def test_the_sweep_refuses_the_change_only_copy(change_only_frame: pd.DataFrame) -> None:
    """Discovery pairs frames one-to-one; change-only frames are the changes,
    so it refuses by name rather than rank a biased sample (P02.10)."""
    with pytest.raises(ChangeOnlyStorageError, match="store_every_frame"):
        sweep_session(change_only_frame, ["obd:RPM"], can_ids=[0x1F9], policy=POLICY)


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


def test_segmenter_fallback_reproduces_mode01_labels() -> None:
    """Broadcast-only mode labels must match the Mode 01 labels they stand in for.

    The segmenter's ``decoded:`` fallback (rpm / wheel speed / coolant) is what
    labels every row where Mode 01 is stale. Those columns hold raw binding
    counts, so this runs the real training chain — parquet -> binding decode ->
    resample -> ``segment_or_constant`` — once as recorded and once with the
    Mode 01 inputs blanked, and requires the two to agree where the PIDs were
    fresh. The pre-fix fallback (counts read as engineering units) scored 0.38.
    """
    from src.columns import MODE_COLUMN
    from src.config import CanaryConfig
    from src.features.feature_pipeline import (
        _decode_session_to_resampled,
        segment_or_constant,
    )
    from src.preprocessing.resampler import ResampleConfig

    case = EXPECTATIONS["segmenter_fallback"]
    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    resampled = _decode_session_to_resampled(
        SESSION,
        load_bindings(BINDINGS_PATH),
        None,
        None,
        ResampleConfig(
            hz=config.preprocessing.resample_hz,
            max_gap_factor=config.preprocessing.max_gap_factor,
            slow_channel_max_gap_seconds=config.preprocessing.slow_channel_max_gap_seconds,
        ),
    )
    blanked = list(case["blanked"])
    reference, _ = segment_or_constant(resampled, config)
    broadcast_only = resampled.copy()
    broadcast_only[blanked] = np.nan
    fallback, fell_back = segment_or_constant(broadcast_only, config)

    assert not fell_back
    fresh = resampled[blanked].notna().all(axis=1)
    assert fresh.sum() >= case["min_fresh_rows"], "fixture no longer covers Mode 01"
    ref_labels = reference.loc[fresh, MODE_COLUMN]
    missing = set(case["required_reference_modes"]) - set(ref_labels)
    assert not missing, f"slice no longer exercises modes {sorted(missing)}"

    agreement = float((ref_labels == fallback.loc[fresh, MODE_COLUMN]).mean())
    assert agreement >= case["min_agreement"], (
        f"broadcast-only labels agree with Mode 01 on {agreement:.3f} of "
        f"{int(fresh.sum())} rows:\n"
        f"{pd.crosstab(ref_labels, fallback.loc[fresh, MODE_COLUMN])}"
    )


# ---------------------------------------------------------------------------
# Mode 22 DIDs on CAN (2026-10-04): 0x1102 is road speed, 0x1123-0x1126 are
# the trims, and none of their meanings may come from the K-line DID table.
# ---------------------------------------------------------------------------

DIDS_PATH = Path("data/dids.yaml")
DID_SESSION = FIXTURE_ROOT / EXPECTATIONS["did_fixture"]


@pytest.fixture(scope="module")
def did_frame() -> pd.DataFrame:
    return read_session(DID_SESSION)


def _did_ids(cases: list[dict[str, Any]]) -> list[str]:
    return [f"{case['signal']}~{case['reference']}" for case in cases]


def _per_sample_ratio(
    frame: pd.DataFrame, case: dict[str, Any], dids_path: Path = DIDS_PATH
) -> np.ndarray:
    """signal / reference at each paired instant, where the reference is fast
    enough that one quantisation step is not most of the reading."""
    pairs = align_pairs(
        frame, case["signal"], case["reference"], policy=POLICY, dids_path=dids_path
    ).pairs
    moving = pairs["b"] > case["ratio_min_reference"]
    return (pairs["a"][moving] / pairs["b"][moving]).to_numpy(dtype=float)


@pytest.mark.parametrize(
    "pin", EXPECTATIONS["did_pins"], ids=[f"0x{p['did']:04X}" for p in EXPECTATIONS["did_pins"]]
)
def test_did_catalog_entries_are_pinned_to_the_firmware(pin: dict[str, Any]) -> None:
    """The corrected CAN DIDs, exactly, and named for the PID that shares their RAM.

    The firmware's CAN pointer table sends each DID to the RAM address the CAN
    Mode 01 handler reads for ``mode01_pid``. The expected name is derived
    from that PID's own Mode 01 meaning rather than copied, so swapping banks
    (0x1125 as bank 2, the pre-2026-10-04 catalog) or short for long fails
    here even though no capture can pair a trim DID with its PID yet.
    """
    from src.collection.pid_decoders import PID_DECODERS

    entry = next(e for e in load_did_map(DIDS_PATH).dids if e.did == pin["did"])
    assert entry.name == pin["name"]
    assert entry.byte_start == pin["byte_start"]
    assert entry.byte_length == pin["byte_length"]
    assert entry.response_length == pin["byte_length"]
    assert entry.scale == pytest.approx(pin["scale"])
    assert (entry.offset or 0.0) == pytest.approx(pin["offset"])
    assert entry.unit == pin["unit"]

    pid_name = PID_DECODERS[pin["mode01_pid"]][0]
    if "FUEL_TRIM" in pid_name:
        # SHORT_FUEL_TRIM_1 -> ECM_SHORT_FUEL_TRIM_B1: kind and bank both
        # come from the PID, so neither can drift from the firmware record.
        kind, bank = pid_name.rsplit("_", 1)
        assert entry.name == f"ECM_{kind}_B{bank}", (
            f"0x{pin['did']:04X} reads the PID 0x{pin['mode01_pid']:02X} word "
            f"({pid_name}) but is catalogued as {entry.name}"
        )
    elif pid_name == "EVAPORATIVE_PURGE":
        # 0x1110 was intake VVT solenoid duty until 2026-10-04.
        assert "EVAP_PURGE" in entry.name and "VVT" not in entry.name
    else:
        assert pid_name == "SPEED"
        assert "SPEED" in entry.name
    # Every corrected entry rests on CAN evidence, never on K-line alone.
    assert "can" in {item.transport for item in entry.evidence}


def _pid_from_ram(raw: int, arithmetic: dict[str, int]) -> int:
    """PID byte A the ECM's CAN Mode 01 handler builds from the RAM byte."""
    return min(arithmetic["max"], raw * arithmetic["multiply"] // arithmetic["divide"])


_ARITHMETIC_PINS = [p for p in EXPECTATIONS["did_pins"] if "mode01_a" in p]


@pytest.mark.parametrize(
    "pin", _ARITHMETIC_PINS, ids=[f"0x{p['did']:04X}" for p in _ARITHMETIC_PINS]
)
def test_did_scale_reproduces_the_ecm_own_pid_conversion(pin: dict[str, Any]) -> None:
    """A DID and a PID that read the same RAM byte must decode to the same value.

    The ECM's own Mode 01 handler converts the byte to the PID's A byte
    (``mode01_a``), and the PID's SAE decoder turns A into engineering units.
    The catalog's decode of the raw byte has to land within one A step of that
    for every raw value the PID does not clamp. This is a firmware known
    answer: it needs no capture, so it holds even where Mode 01 and the DID
    session cannot be read together. The rival 0x1110 scale, 100/255 % per
    count, fails it, so it can tell the two apart (checked below).
    """
    from src.collection.pid_decoders import PID_DECODERS
    from src.decode.dids import apply_scaling, decode_did_raw

    entry = next(e for e in load_did_map(DIDS_PATH).dids if e.did == pin["did"])
    pid_decoder = PID_DECODERS[pin["mode01_pid"]][1]
    arithmetic = pin["mode01_a"]

    def pid_value(a: int) -> float:
        decoded = pid_decoder(bytes([a]))
        assert decoded is not None
        return decoded[0]

    one_a_step = pid_value(1) - pid_value(0)

    def disagreements(scale: float) -> list[int]:
        bad = []
        for raw in range(256):
            a = _pid_from_ram(raw, arithmetic)
            if a == arithmetic["max"]:
                continue  # the PID clamps; the DID does not
            assert decode_did_raw(entry, bytes([raw])) == raw
            did_value = raw * scale + (entry.offset or 0.0)
            if abs(did_value - pid_value(a)) > one_a_step:
                bad.append(raw)
        return bad

    assert entry.scale is not None
    assert disagreements(entry.scale) == []
    if pin["did"] == 0x1110:
        assert apply_scaling(entry, 100) == pytest.approx(50.0)
        # The other candidate D04.7 could not rule out from saturation alone.
        assert disagreements(100 / 255), "the 100/255 % reading should fail this check"


_BANK_PINS = EXPECTATIONS["did_bank_pins"]


def _bank_of_dtc(code: str) -> int:
    """The bank a DTC's SAE meaning names, e.g. P0011 -> 1."""
    import re

    from src.decode.dtc_catalog import describe_dtc

    description = describe_dtc(code)
    match = re.search(r"\(Bank (\d)\)", description.description)
    assert description.curated and match, f"{code} has no curated bank: {description}"
    return int(match.group(1))


@pytest.mark.parametrize("pin", _BANK_PINS, ids=[f"0x{p['did']:04X}" for p in _BANK_PINS])
def test_vtc_did_banks_are_pinned_to_the_firmware(pin: dict[str, Any]) -> None:
    """The intake VTC DIDs, exactly, and named for the bank their DTC reports.

    D03.37: the firmware's DTC item table puts controller index 0 on P0011 /
    P0075 and index 1 on P0021 / P0081. The expected bank suffix is derived
    from the DTC's own SAE meaning rather than copied, so the pre-2026-10-05
    reading ("0x1135 probably bank 2") or any swap fails here. The entry's CAN
    evidence must cite the pointer-table target and the RAM byte it copies,
    which ties the recorded addresses to the catalog's own case.
    """
    entry = next(e for e in load_did_map(DIDS_PATH).dids if e.did == pin["did"])
    assert entry.name == pin["name"]
    assert entry.byte_start == pin["byte_start"]
    assert entry.byte_length == pin["byte_length"]
    assert entry.response_length == pin["byte_length"]
    assert entry.scale == pytest.approx(pin["scale"])
    assert (entry.offset or 0.0) == pytest.approx(pin["offset"])
    assert entry.unit == pin["unit"]

    bank = _bank_of_dtc(pin["bank_dtc"])
    assert entry.name.endswith(f"_B{bank}"), (
        f"0x{pin['did']:04X} belongs to the controller that reports {pin['bank_dtc']} "
        f"(bank {bank}) but is catalogued as {entry.name}"
    )
    can_findings = [item.finding for item in entry.evidence if item.transport == "can"]
    for address in (pin["ram"], pin["snapshot_of"]):
        assert any(f"0x{address:08X}" in finding for finding in can_findings), (
            f"0x{pin['did']:04X}: no CAN evidence cites 0x{address:08X}"
        )


def test_vtc_controller_indexes_are_one_bank_each() -> None:
    """Angle and duty of one controller index share a bank, and the two
    indexes are the two banks, so a half-applied swap fails too."""
    banks: dict[int, set[int]] = {}
    for pin in _BANK_PINS:
        banks.setdefault(pin["vtc_index"], set()).add(_bank_of_dtc(pin["bank_dtc"]))
    assert banks == {0: {1}, 1: {2}}


def _timer_duty_pct(raw: int, pwm: dict[str, int]) -> float:
    """On-time the ECM loads into timer channel 7 for one duty byte."""
    compare = raw * pwm["compare_multiply"] // pwm["compare_divide"]
    return 100.0 * compare / pwm["period"]


_PWM_PINS = [p for p in _BANK_PINS if "pwm" in p]


@pytest.mark.parametrize("pin", _PWM_PINS, ids=[f"0x{p['did']:04X}" for p in _PWM_PINS])
def test_vtc_duty_scale_reproduces_the_timer_compare(pin: dict[str, Any]) -> None:
    """The duty DID's decode is the PWM on-time the output stage produces.

    0xC66C4 loads the timer compare with raw x 33 x 39 / 80 against a 4,125-
    count period. The catalog's decode has to land within one timer count of
    that for every byte value. A firmware known answer, no capture needed. The
    rival readings fail it: x0.5 % (the 0x1110 convention, impossible here as
    raw reaches 255) and x100/255 % (a byte read as a plain percentage).
    """
    from src.decode.dids import decode_did_raw

    entry = next(e for e in load_did_map(DIDS_PATH).dids if e.did == pin["did"])
    pwm = pin["pwm"]
    one_timer_count = 100.0 / pwm["period"]

    def disagreements(scale: float) -> list[int]:
        bad = []
        for raw in range(256):
            assert decode_did_raw(entry, bytes([raw])) == raw
            did_value = raw * scale + (entry.offset or 0.0)
            if abs(did_value - _timer_duty_pct(raw, pwm)) > one_timer_count:
                bad.append(raw)
        return bad

    assert entry.scale is not None
    assert disagreements(entry.scale) == []
    assert _timer_duty_pct(255, pwm) == pytest.approx(99.44, abs=0.01)
    assert disagreements(0.5), "the x0.5 % reading should fail this check"
    assert disagreements(100 / 255), "the 100/255 % reading should fail this check"


@pytest.mark.parametrize(
    "case", EXPECTATIONS["did_agreement"], ids=_did_ids(EXPECTATIONS["did_agreement"])
)
def test_did_agrees_with_its_independent_reference(
    case: dict[str, Any], did_frame: pd.DataFrame
) -> None:
    """A CAN DID decode and a road-speed reference must agree 1:1, pair by pair.

    The same 1:1 hypothesis as the broadcast checks, plus the per-sample ratio
    (AGENTS.md: read the ratio even when the verdict says stable).
    """
    result = compare_channels(did_frame, case["signal"], case["reference"], policy=POLICY)

    assert abs(result.difference_bias) <= case["max_residual_bias"], (
        f"{result.describe()} — mean b-a is {result.difference_bias:+.3f} km/h; "
        "a correct decode sits within its quantisation of zero"
    )
    assert result.difference_spread <= case["max_residual_spread"], result.describe()
    assert result.r_squared >= case["min_r_squared"], result.describe()
    assert abs(result.slope - 1.0) * 100.0 <= case["max_scale_error_pct"], result.describe()
    assert abs(result.offset) <= case["max_offset"], result.describe()
    assert result.scale_verdict == "stable", result.describe()

    ratio = _per_sample_ratio(did_frame, case)
    assert len(ratio) >= 30, "fixture no longer moves fast enough for a ratio"
    median = float(np.median(ratio))
    assert abs(median - 1.0) * 100.0 <= case["max_ratio_error_pct"], (
        f"per-sample ratio median {median:.4f} (p10 {np.percentile(ratio, 10):.4f}, "
        f"p90 {np.percentile(ratio, 90):.4f}, n {len(ratio)})"
    )


def _did_catalog_with(tmp_path: Path, did: int, **changes: Any) -> Path:
    raw = yaml.safe_load(DIDS_PATH.read_text())
    entry = next(e for e in raw["dids"] if e["did"] == did)
    entry.update(changes)
    path = tmp_path / "dids.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


def test_did_agreement_can_fail(did_frame: pd.DataFrame, tmp_path: Path) -> None:
    """The check must be able to reject the two catalogs it replaced.

    Pre-2026-10-04, 0x1102 was a 2-byte "percent" (UpRev's K-line LTFT B1):
    that slice decodes nothing from the 1-byte CAN reply. The K-line reading
    at the right width (scale 1) halves every speed. A +4 % scale edit, the
    size the broadcast suite measured slipping past a 3 % slope bound, is
    caught too.
    """
    case = EXPECTATIONS["did_agreement"][0]

    old = _did_catalog_with(
        tmp_path, 0x1102, byte_length=2, response_length=2, scale=1.0, unit="percent"
    )
    with pytest.raises(InputNotCapturedError):
        compare_channels(did_frame, case["signal"], case["reference"], policy=POLICY, dids_path=old)

    for scale in (1.0, 2.08):
        mutated = _did_catalog_with(tmp_path, 0x1102, scale=scale)
        result = compare_channels(
            did_frame, case["signal"], case["reference"], policy=POLICY, dids_path=mutated
        )
        assert abs(result.slope - 1.0) * 100.0 > case["max_scale_error_pct"], (
            scale,
            result.describe(),
        )
        assert abs(result.difference_bias) > case["max_residual_bias"], (scale, result.describe())
