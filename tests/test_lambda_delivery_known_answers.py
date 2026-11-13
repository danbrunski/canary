"""Known answers for the lambda-delivery declaration, on a committed slice (B06.2).

The slice under ``tests/fixtures/lambda_delivery`` is the polled rows of the
2026-09-21 tuning capture ``_capture-tuning-2026-09-21T223734``, restricted to
the ten channels these checks read. Every number below reproduces the
whole-session figure exactly — ``scripts/lambda_delivery_evidence.py`` run on
``~/canary-data/sessions-0921b/<that session>`` gives the same values — so the
fixture is a slice of evidence rather than a separate answer.

Reproduce a known answer before a novel one (AGENTS.md): these assert the
relations the declaration's convention, sentinel and regime claims rest on,
*and* that the same relations fail on neighbouring channels.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import scripts.lambda_delivery_evidence as evidence
from src.analysis.lambda_delivery import (  # noqa: E402
    ELIGIBLE,
    LambdaDeliveryDeclaration,
    delivery_samples,
    load_lambda_delivery,
    target_source_agreement,
)
from src.preprocessing.loader import read_session  # noqa: E402
from src.signals.afr import load_afr_calibration  # noqa: E402
from src.signals.pairing import PairingPolicy  # noqa: E402

PROFILE = Path("data/nissan-xterra-2008")
SLICE = Path("tests/fixtures/lambda_delivery/_capture-tuning-2026-09-21T223734")


@pytest.fixture(scope="module")
def declaration() -> LambdaDeliveryDeclaration:
    return load_lambda_delivery(
        PROFILE / "lambda_delivery.yaml",
        dids_path=Path("data/dids.yaml"),
        providers_path=PROFILE / "tuning_providers.yaml",
    )


@pytest.fixture(scope="module")
def session() -> pd.DataFrame:
    return read_session(SLICE, filters=[("source", "==", "obd")])


def test_ram_target_and_pid_0x44_agree_exactly_on_steady_enrichment(
    session: pd.DataFrame, declaration: LambdaDeliveryDeclaration
) -> None:
    """The chain that pins the RAM word's phi convention.

    PID 0x44 is phi (verified at WOT, 78592f4). On steady enrichment the two
    channels are the same number per sample, so the RAM word is phi too.
    """
    result = target_source_agreement(
        session,
        "mem:ECM_TARGET_EQUIV_RATIO",
        "obd:COMMANDED_EQUIV_RATIO",
        policy=PairingPolicy.within(pd.Timedelta(seconds=0.25)),
        closed_loop_band=declaration.eligibility.closed_loop_band,
        steady_window_s=1.5,
    )
    assert result["n_pairs"] == 926
    enrichment = result["regimes"]["enrichment"]
    assert enrichment["steady_pairs"] == 7
    assert enrichment["exact_fraction"] == 1.0


def test_pid_0x44_is_not_the_closed_loop_target(
    session: pd.DataFrame, declaration: LambdaDeliveryDeclaration
) -> None:
    """Why the declaration restricts PID 0x44 to enrichment.

    With the RAM target holding exactly 1.0, PID 0x44 agrees on a minority of
    steady pairs and reaches 1.148 — this is the measurement behind
    ``valid_regimes: [enrichment]``.
    """
    result = target_source_agreement(
        session,
        "mem:ECM_TARGET_EQUIV_RATIO",
        "obd:COMMANDED_EQUIV_RATIO",
        policy=PairingPolicy.within(pd.Timedelta(seconds=0.25)),
        closed_loop_band=declaration.eligibility.closed_loop_band,
        steady_window_s=1.5,
    )
    closed = result["regimes"]["closed_loop"]
    assert closed["steady_pairs"] == 861
    assert closed["exact_fraction"] == pytest.approx(0.0813008, abs=1e-6)
    assert closed["ratio_p95"] == pytest.approx(1.1484649, abs=1e-6)


def test_a_neighbouring_ram_word_fails_the_same_agreement_test(session: pd.DataFrame) -> None:
    """The negative control: a relation that cannot fail proves nothing."""
    control = evidence._negative_control(
        session, "mem:ECM_TARGET_EQUIV_RATIO", "mem:ECM_ENGINE_SPEED_RAM", 0.25
    )
    assert control["pairs"] == 621
    assert control["exact_fraction"] == 0.0
    assert control["within_0p5pct_fraction"] == 0.0


def test_fuel_cut_sentinel_coincides_with_both_mode01_signatures(
    session: pd.DataFrame, declaration: LambdaDeliveryDeclaration
) -> None:
    """Fuel cut is a state on three independent channels at once."""
    out = evidence.fuel_cut_coincidence(session, declaration, max_separation_s=0.25)
    assert out["sentinel_reads"] == 151
    assert out["pid_0x44_zero"] == {"paired": 151, "agree": 147}
    # The sensor lags the ECU's command, so it is at its lean rail on most —
    # not all — sentinel instants. That is the transport delay, visible.
    assert out["lambda_at_rail"] == {"paired": 151, "agree": 124}
    # 556 PID-0x44 zero reads (4 Hz) against 151 sentinel reads (1 Hz); only
    # 13 of them land within 250 ms of a numeric RAM target, at cut edges.
    assert out["pid_zero_paired_with_ram_number"]["pid_zero_reads"] == 556
    assert out["pid_zero_paired_with_ram_number"]["paired_with_numeric_target"] == 13


def test_pid_0x44_follows_phi_above_stoich_and_the_sensor_below_it(
    session: pd.DataFrame, declaration: LambdaDeliveryDeclaration
) -> None:
    """The two-behaviour finding, judged per side and away from stoich.

    Above 1.0 (enrichment) PID 0x44 is phi, reproducing 78592f4 from an
    independent instrument. Below 1.0 — closed loop — its readings track the
    lambda sensor numerically instead, which is *not* a commanded target.
    """
    out = evidence.convention_evidence(
        session,
        declaration,
        target_spec="obd:COMMANDED_EQUIV_RATIO",
        measured_spec="obd:O2_WR_LAMBDA_S1",
        max_separation_s=0.25,
        steady_window_s=1.0,
        holdoff_s=declaration.eligibility.regimes.closed_loop.post_fuel_cut_holdoff_s,
    )
    above, below = out["above_1"], out["below_1"]
    assert above["verdict"] == "phi"
    assert above["n_pairs_off_stoich"] == 18
    assert above["median_abs_error_as_phi_pct"] == pytest.approx(2.381, abs=1e-3)
    assert above["median_abs_error_as_lambda_pct"] == pytest.approx(24.444, abs=1e-3)
    assert below["verdict"] == "lambda"
    assert below["n_pairs_off_stoich"] == 18


def test_the_convention_check_fails_on_a_channel_that_is_not_a_mixture(
    session: pd.DataFrame, declaration: LambdaDeliveryDeclaration
) -> None:
    out = evidence.convention_evidence(
        session,
        declaration,
        target_spec="mem:ECM_IGNITION_TIMING_RAM",
        measured_spec="obd:O2_WR_LAMBDA_S1",
        max_separation_s=0.25,
        steady_window_s=1.0,
        holdoff_s=declaration.eligibility.regimes.closed_loop.post_fuel_cut_holdoff_s,
    )
    assert out["above_1"]["verdict"] == "neither"
    assert out["above_1"]["n_pairs_off_stoich"] == 244


def test_state_counts_account_for_every_target_observation(
    session: pd.DataFrame, declaration: LambdaDeliveryDeclaration
) -> None:
    """Nothing disappears: cut, rail, post-cut and transient are all counted."""
    target = evidence._long(session, "mem:ECM_TARGET_EQUIV_RATIO")
    sentinel = evidence._long(session, "mem:ECM_TARGET_EQUIV_RATIO_SENTINEL")
    measured = evidence._long(session, "obd:O2_WR_LAMBDA_S1")
    samples = delivery_samples(
        target=target,
        measured=measured,
        sentinel=sentinel,
        declaration=declaration,
        target_source="ram_final_target",
        measured_source="ecm_lambda_pid_0x24",
        calibration=load_afr_calibration(PROFILE / "afr_calibration.yaml"),
    )
    counts = samples.counts
    assert counts["total"] == 1078
    assert counts["fuel_cut"] == 151
    assert counts["measured_rail"] == 19
    assert counts["measured_missing"] == 1
    # R05.16: enrichment rows are judged by the enrichment rule (0.6 s /
    # 0.01 lambda dwell, 6 s holdoff, frozen 0.10 s delay); closed-loop rows
    # by the unchanged closed-loop rule. Against the single global rule:
    # post_fuel_cut 324 -> 316, target_transient 29 -> 31, eligible 554 -> 560.
    assert counts["post_fuel_cut"] == 316
    assert counts["target_transient"] == 31
    assert counts[ELIGIBLE] == 560
    assert sum(counts[state] for state in samples.counts if state != "total") == counts["total"]
    frame = samples.frame
    assert frame.loc[frame["state"] != ELIGIBLE, "residual_pct"].isna().all()
    # Closed loop is untouched: its rule did not change. Enrichment rose from
    # 2 rows (3 s dwell at a 1 Hz target poll) to 8. This slice's drive is in
    # the enrichment rule's informed_by, so these are not held-out evidence.
    eligible = frame[frame["state"] == ELIGIBLE]
    assert eligible["regime"].value_counts().to_dict() == {"closed_loop": 552, "enrichment": 8}
    assert eligible.loc[eligible["regime"] == "enrichment", "residual_pct"].to_numpy() == (
        pytest.approx([4.257, 3.163, 2.989, 3.149, 2.847, 2.864, 1.486, 2.403], abs=1e-3)
    )


def test_acquisition_mismatch_is_within_the_declared_tolerance(
    session: pd.DataFrame, declaration: LambdaDeliveryDeclaration
) -> None:
    """Polling skew, measured — separate from any transport delay."""
    out = evidence.acquisition_separation(
        session,
        "mem:ECM_TARGET_EQUIV_RATIO",
        "obd:O2_WR_LAMBDA_S1",
        max_separation_s=declaration.acquisition.max_separation_s,
    )
    assert out["a_reads"] == 927
    assert out["median_s"] == pytest.approx(0.1014, abs=1e-3)
    assert out["within_declared"] == 926
