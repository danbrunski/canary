"""Unit contracts for the lambda-delivery relationship (B06.1-B06.4).

Synthetic data only; the committed-slice known answers are in
``tests/test_lambda_delivery_known_answers.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from src.analysis.afr import AfrCalibration, VoltageAfrPoint, VoltageAfrTransfer
from src.analysis.behavior_validation import LeakedDriveError
from src.analysis.lambda_delivery import (
    ELIGIBLE,
    DriveInputs,
    LambdaDeliveryError,
    check_target_convention,
    delivery_samples,
    estimate_transport_delay,
    load_lambda_delivery,
    regime_noise_floor,
    residual_pct,
    to_lambda,
)
from src.analysis.noise_floor import ChannelFloor, NoiseFloor
from src.analysis.tuning import DeliveryInputs, gate_delivery_samples, mode01_route

T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
PROFILE = Path("data/nissan-xterra-2008")
DECLARATION_PATH = PROFILE / "lambda_delivery.yaml"


def declaration(**overrides: Any) -> Any:
    doc = yaml.safe_load(DECLARATION_PATH.read_text())
    doc.update(overrides)
    from src.analysis.lambda_delivery import LambdaDeliveryDeclaration

    return LambdaDeliveryDeclaration.model_validate(doc)


def _long(values: list[float | None], *, start: float = 0.0, step: float = 1.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": [T0 + timedelta(seconds=start + i * step) for i in range(len(values))],
            "value": values,
        }
    )


# ---------------------------------------------------------------------------
# B06.1 — conventions and the single residual


def test_phi_and_lambda_agree_at_stoich_and_diverge_away_from_it() -> None:
    at_stoich = np.array([1.0])
    assert to_lambda(at_stoich, "phi", afr_stoich=14.7) == pytest.approx(
        to_lambda(at_stoich, "lambda", afr_stoich=14.7)
    )
    rich = np.array([1.25])
    assert to_lambda(rich, "phi", afr_stoich=14.7) == pytest.approx([0.8])
    assert to_lambda(rich, "lambda", afr_stoich=14.7) == pytest.approx([1.25])
    assert to_lambda(np.array([11.76]), "afr", afr_stoich=14.7) == pytest.approx([0.8])


def test_non_positive_mixture_readings_are_not_numbers() -> None:
    out = to_lambda(np.array([0.0, -1.0, np.nan, 1.2]), "phi", afr_stoich=14.7)
    assert np.isnan(out[:3]).all()
    assert out[3] == pytest.approx(1 / 1.2)


def test_residual_is_signed_lean_positive_and_refuses_phi() -> None:
    assert residual_pct(np.array([1.03]), np.array([1.0]), quantity="lambda") == pytest.approx(
        [3.0]
    )
    assert residual_pct(np.array([14.7]), np.array([15.141]), quantity="afr") == pytest.approx(
        [-2.913], abs=1e-3
    )
    with pytest.raises(LambdaDeliveryError, match="inverts the sign"):
        residual_pct(np.array([1.0]), np.array([1.0]), quantity="phi")  # type: ignore[arg-type]


def test_delivery_error_uses_the_same_residual_as_the_relationship() -> None:
    """Since R05.16 delivery error *is* this relationship: its samples are
    :func:`delivery_samples` rows, so its residual cannot drift in sign or
    definition from :func:`residual_pct`."""
    from src.analysis.lambda_delivery import load_lambda_delivery

    declaration = load_lambda_delivery(DECLARATION_PATH)
    start = datetime(2026, 9, 21, 12, tzinfo=UTC)
    stamps = [start + timedelta(seconds=i) for i in range(6)]

    def long(values: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(stamps, utc=True),
                "value": values,
                "timestamp_basis": "acquired",
            }
        )

    measured_lambda = [0.85, 0.85, 0.85, 0.80, 0.90, 0.86]
    inputs = DeliveryInputs(
        target=long([1.2] * 6),
        sentinel=None,
        measured=long(measured_lambda),
        rpm=long([3200.0] * 3 + [4200.0] * 3),
        fuel_status=long([4.0] * 6),
        load=None,
    )
    route = mode01_route(declaration, measured_spec="obd:O2_WR_LAMBDA_S1", min_load=None)
    values, report = gate_delivery_samples(
        inputs,
        route,
        declaration=declaration,
        calibration=AfrCalibration(commanded_equiv_ratio_convention="phi"),
        afr_stoich=14.7,
        rpm_bands=(2500.0, 3000.0, 3500.0, 4000.0, 4500.0, 5000.0, 6500.0),
        min_load=None,
    )
    # The first read has no earlier target to show the 0.6 s enrichment dwell.
    assert report.remaining == 5
    expected = residual_pct(
        values["measured_afr"].to_numpy(), values["commanded_afr"].to_numpy(), quantity="afr"
    )
    assert values["error_pct"].to_numpy() == pytest.approx(expected)
    assert values["error_pct"].to_numpy() == pytest.approx(
        [(m * 1.2 - 1.0) * 100.0 for m in measured_lambda[1:]]
    )


def test_convention_cannot_be_judged_at_stoich() -> None:
    target = np.full(50, 1.0)
    measured = np.full(50, 1.0)
    check = check_target_convention(target, measured, off_stoich_band=0.03, tolerance_pct=3.0)
    assert check.verdict == "unresolvable"
    assert check.n_pairs_off_stoich == 0


def test_convention_resolves_away_from_stoich_and_per_side() -> None:
    phi = np.full(20, 1.25)
    measured = np.full(20, 0.8)  # lambda of a phi 1.25 command
    assert (
        check_target_convention(phi, measured, off_stoich_band=0.03, tolerance_pct=3.0).verdict
        == "phi"
    )
    lam = np.full(20, 0.8)
    assert (
        check_target_convention(lam, measured, off_stoich_band=0.03, tolerance_pct=3.0).verdict
        == "lambda"
    )
    both = np.concatenate([phi, lam])
    doubled = np.concatenate([measured, measured])
    above = check_target_convention(
        both, doubled, off_stoich_band=0.03, tolerance_pct=3.0, side="above"
    )
    below = check_target_convention(
        both, doubled, off_stoich_band=0.03, tolerance_pct=3.0, side="below"
    )
    assert (above.verdict, below.verdict) == ("phi", "lambda")


# ---------------------------------------------------------------------------
# B06.2 — named ineligible states, never manufactured numbers


def test_fuel_cut_sentinel_is_a_state_not_a_lean_delivery() -> None:
    doc = declaration()
    target = _long([1.0] * 10)
    measured = _long([1.0] * 10, start=0.05)
    sentinel = pd.DataFrame({"timestamp": [T0 + timedelta(seconds=11)], "value": [1.0]})
    cut_measured = _long([2.0], start=11.05)
    samples = delivery_samples(
        target=target,
        measured=pd.concat([measured, cut_measured], ignore_index=True),
        sentinel=sentinel,
        declaration=doc,
        target_source="ram_final_target",
        measured_source="ecm_lambda_pid_0x24",
        afr_stoich=14.7,
    )
    frame = samples.frame
    assert samples.counts["fuel_cut"] == 1
    assert frame.loc[frame["state"] == "fuel_cut", "residual_pct"].isna().all()
    assert frame.loc[frame["state"] != ELIGIBLE, "residual_pct"].isna().all()


def test_every_ineligible_state_is_named_and_carries_no_residual() -> None:
    doc = declaration()
    # 0-9 s steady stoich; 10 s an implausible target; 12 s a target step;
    # measured absent at 6 s and at the sensor rail at 7 s.
    target_values = [1.0] * 10 + [0.5] + [1.0] + [1.2]
    target = _long(target_values)
    measured_times = [i for i in range(13) if i != 6]
    measured_values = [1.0] * len(measured_times)
    measured_values[measured_times.index(7)] = 2.0  # lean rail
    measured = pd.DataFrame(
        {
            "timestamp": [T0 + timedelta(seconds=t + 0.05) for t in measured_times],
            "value": measured_values,
        }
    )
    samples = delivery_samples(
        target=target,
        measured=measured,
        declaration=doc,
        target_source="ram_final_target",
        measured_source="ecm_lambda_pid_0x24",
        afr_stoich=14.7,
    )
    states = dict(zip(samples.frame["state"], samples.frame["target_raw"], strict=True))
    counts = samples.counts
    assert counts["target_invalid"] == 1
    assert counts["measured_missing"] == 1
    assert counts["measured_rail"] == 1
    assert counts["target_transient"] >= 1
    assert counts[ELIGIBLE] > 0
    assert "target_invalid" in states
    assert samples.frame.loc[samples.frame["state"] != ELIGIBLE, "residual_pct"].isna().all()
    assert samples.frame.loc[samples.frame["state"] == ELIGIBLE, "residual_pct"].notna().all()


def test_post_fuel_cut_recovery_is_not_a_rich_delivery_finding() -> None:
    doc = declaration()
    target = _long([1.0] * 30)
    measured = _long([0.87] * 30, start=0.05)
    sentinel = pd.DataFrame({"timestamp": [T0 + timedelta(seconds=2)], "value": [1.0]})
    samples = delivery_samples(
        target=target,
        measured=measured,
        sentinel=sentinel,
        declaration=doc,
        target_source="ram_final_target",
        measured_source="ecm_lambda_pid_0x24",
        afr_stoich=14.7,
    )
    frame = samples.frame
    holdoff = doc.eligibility.regimes.closed_loop.post_fuel_cut_holdoff_s
    within = frame[
        frame["timestamp"] < frame["timestamp"].iloc[0] + pd.Timedelta(seconds=2 + holdoff)
    ]
    assert (within["state"] != ELIGIBLE).all()
    assert samples.counts["post_fuel_cut"] >= 10
    # After the holdoff the same rich reading *is* a residual.
    late = frame[frame["state"] == ELIGIBLE]
    assert len(late) > 0
    assert (late["residual_pct"] < -10).all()


def test_pid_0x44_is_refused_as_a_closed_loop_target() -> None:
    doc = declaration()
    target = _long([1.0] * 10)
    measured = _long([0.98] * 10, start=0.05)
    samples = delivery_samples(
        target=target,
        measured=measured,
        declaration=doc,
        target_source="mode01_pid_0x44",
        measured_source="ecm_lambda_pid_0x24",
        calibration=AfrCalibration(commanded_equiv_ratio_convention="phi"),
        afr_stoich=14.7,
    )
    assert samples.counts["source_not_valid_in_regime"] > 0
    assert samples.counts[ELIGIBLE] == 0


def test_unverified_convention_refuses_rather_than_guessing() -> None:
    doc = declaration()
    samples = delivery_samples(
        target=_long([1.2] * 6),
        measured=_long([0.83] * 6, start=0.05),
        declaration=doc,
        target_source="mode01_pid_0x44",
        measured_source="ecm_lambda_pid_0x24",
        calibration=AfrCalibration(commanded_equiv_ratio_convention="unverified"),
        afr_stoich=14.7,
    )
    assert samples.counts["convention_unverified"] == 6
    assert samples.frame["residual_pct"].isna().all()


def test_voltage_measured_source_without_a_transfer_is_named_not_dropped() -> None:
    doc = declaration()
    samples = delivery_samples(
        target=_long([1.0] * 6),
        measured=_long([1.5] * 6, start=0.05),
        declaration=doc,
        target_source="ram_final_target",
        measured_source="af_voltage_b1",
        calibration=AfrCalibration(),
        afr_stoich=14.7,
    )
    assert samples.counts["calibration_unknown"] == 6


def test_voltage_transfer_censoring_is_a_named_state() -> None:
    calibration = AfrCalibration(
        voltage_transfer=VoltageAfrTransfer(
            points=[VoltageAfrPoint(voltage=1.0, afr=11.0), VoltageAfrPoint(voltage=3.0, afr=17.0)],
            censored_floor_afr=11.5,
            censored_ceiling_afr=16.0,
        )
    )
    samples = delivery_samples(
        target=_long([1.0] * 6),
        measured=_long([1.05] * 6, start=0.05),  # below the censoring floor
        declaration=declaration(),
        target_source="ram_final_target",
        measured_source="af_voltage_b1",
        calibration=calibration,
        afr_stoich=14.7,
    )
    assert samples.counts["measured_censored"] == 6


# ---------------------------------------------------------------------------
# B06.3 — transport delay, frozen on training drives


def _lagged_drive(lag_s: float, *, n: int = 120, tau_s: float = 0.4) -> DriveInputs:
    """A target that steps rich every 20 s, with the sensor following it after
    ``lag_s`` of transport delay and a first-order response of ``tau_s``."""
    phi = np.where((np.arange(n) // 20) % 2 == 1, 1.25, 1.0)
    target = _long(list(phi), step=1.0)
    measured_times = np.arange(0, n, 0.5)
    commanded = np.interp(measured_times - lag_s, np.arange(n), phi, left=phi[0])
    measured_phi = np.empty_like(commanded)
    state = commanded[0]
    for i, value in enumerate(commanded):
        state += (value - state) * (1.0 - np.exp(-0.5 / tau_s))
        measured_phi[i] = state
    measured = pd.DataFrame(
        {
            "timestamp": [T0 + timedelta(seconds=float(t)) for t in measured_times],
            "value": 1.0 / measured_phi,
        }
    )
    return DriveInputs(target=target, measured=measured)


def test_transport_delay_is_estimated_on_training_drives_and_frozen() -> None:
    doc = declaration()
    delay = estimate_transport_delay(
        {"driveA": _lagged_drive(1.0)},
        declaration=doc,
        candidates=[0.0, 0.5, 1.0, 1.5, 2.0],
        target_source="ram_final_target",
        measured_source="ecm_lambda_pid_0x24",
        min_rows=10,
        afr_stoich=14.7,
    )
    assert delay.seconds == pytest.approx(1.0)
    assert delay.fitted_on == ("driveA",)


def test_a_test_drive_cannot_pick_its_own_lag() -> None:
    doc = declaration()
    delay = estimate_transport_delay(
        {"driveA": _lagged_drive(1.0)},
        declaration=doc,
        candidates=[0.0, 1.0],
        target_source="ram_final_target",
        measured_source="ecm_lambda_pid_0x24",
        min_rows=10,
        afr_stoich=14.7,
    )
    inputs = _lagged_drive(1.0)
    with pytest.raises(LeakedDriveError, match="may not pick its own lag"):
        delivery_samples(
            target=inputs.target,
            measured=inputs.measured,
            declaration=doc,
            target_source="ram_final_target",
            measured_source="ecm_lambda_pid_0x24",
            delay=delay,
            drive_id="driveA",
            afr_stoich=14.7,
        )
    with pytest.raises(LambdaDeliveryError, match="needs the drive id"):
        delivery_samples(
            target=inputs.target,
            measured=inputs.measured,
            declaration=doc,
            target_source="ram_final_target",
            measured_source="ecm_lambda_pid_0x24",
            delay=delay,
            afr_stoich=14.7,
        )


def test_drives_that_chose_the_eligibility_constants_are_not_evaluation_evidence() -> None:
    doc = declaration()
    informing = doc.eligibility.informed_by[0]
    with pytest.raises(LeakedDriveError, match="informed the declared eligibility"):
        delivery_samples(
            target=_long([1.0] * 5),
            measured=_long([1.0] * 5, start=0.05),
            declaration=doc,
            target_source="ram_final_target",
            measured_source="ecm_lambda_pid_0x24",
            drive_id=informing,
            evaluation=True,
            afr_stoich=14.7,
        )


def test_transport_delay_refuses_insufficient_training_evidence() -> None:
    with pytest.raises(LambdaDeliveryError, match="insufficient training evidence"):
        estimate_transport_delay(
            {"driveA": _lagged_drive(1.0, n=6)},
            declaration=declaration(),
            candidates=[0.0, 1.0],
            target_source="ram_final_target",
            measured_source="ecm_lambda_pid_0x24",
            min_rows=500,
            afr_stoich=14.7,
        )


# ---------------------------------------------------------------------------
# B06.4 — shared semantics: condition-scoped noise floors, declaration checks


def _floor(conditions: str, *, unit: str = "percent") -> NoiseFloor:
    return NoiseFloor(
        channels={
            "delivery_error_pct": ChannelFloor(unit=unit, conditions=conditions, sd=1.25, n_cells=5)
        }
    )


def test_a_wot_noise_floor_is_not_quoted_against_closed_loop() -> None:
    doc = declaration()
    conditions = doc.noise_floors["enrichment"].conditions
    floor = _floor(conditions)
    assert regime_noise_floor(floor, "enrichment", doc)["available"] is True
    closed = regime_noise_floor(floor, "closed_loop", doc)
    assert closed["available"] is False
    assert "closed_loop" in closed["reason"]
    mismatched = regime_noise_floor(_floor("idle, all conditions"), "enrichment", doc)
    assert mismatched["available"] is False
    assert "measured under" in mismatched["reason"]
    wrong_unit = regime_noise_floor(_floor(conditions, unit="lambda"), "enrichment", doc)
    assert wrong_unit["available"] is False


def test_declaration_is_cross_checked_against_the_catalogs() -> None:
    doc = load_lambda_delivery(
        DECLARATION_PATH,
        dids_path=Path("data/dids.yaml"),
        providers_path=PROFILE / "tuning_providers.yaml",
    )
    assert doc.commanded("ram_final_target").spec == "mem:ECM_TARGET_EQUIV_RATIO"
    assert doc.residual.positive == "lean"


def test_a_target_whose_catalog_unit_disagrees_is_refused(tmp_path: Path) -> None:
    doc = yaml.safe_load(DECLARATION_PATH.read_text())
    doc["commanded_sources"][0]["quantity"] = "lambda"
    path = tmp_path / "lambda_delivery.yaml"
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(LambdaDeliveryError, match="disagrees with declared quantity"):
        load_lambda_delivery(path, dids_path=Path("data/dids.yaml"))


def test_a_target_without_its_declared_sentinel_is_refused(tmp_path: Path) -> None:
    doc = yaml.safe_load(DECLARATION_PATH.read_text())
    doc["commanded_sources"][0]["sentinel_spec"] = None
    doc["commanded_sources"][0]["fuel_cut_value"] = 0.0
    path = tmp_path / "lambda_delivery.yaml"
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(LambdaDeliveryError, match="sentinel_spec must be"):
        load_lambda_delivery(path, dids_path=Path("data/dids.yaml"))


def test_a_commanded_source_must_say_how_fuel_cut_appears_on_it(tmp_path: Path) -> None:
    doc = yaml.safe_load(DECLARATION_PATH.read_text())
    doc["commanded_sources"][1].pop("fuel_cut_value")
    path = tmp_path / "lambda_delivery.yaml"
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(ValueError, match="how fuel cut appears"):
        load_lambda_delivery(path)


def test_the_provider_registry_and_the_declaration_must_name_one_target(tmp_path: Path) -> None:
    doc = yaml.safe_load(DECLARATION_PATH.read_text())
    doc["commanded_sources"] = [doc["commanded_sources"][1]]
    for rule in doc["eligibility"]["regimes"].values():
        del rule["transport_delays"]["ram_final_target"]
    path = tmp_path / "lambda_delivery.yaml"
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(LambdaDeliveryError, match="which .* does not declare"):
        load_lambda_delivery(
            path, dids_path=Path("data/dids.yaml"), providers_path=PROFILE / "tuning_providers.yaml"
        )


# ---------------------------------------------------------------------------
# R05.16 — one eligibility rule per regime, each with its own provenance


def _doc_with(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    doc = yaml.safe_load(DECLARATION_PATH.read_text())
    node = doc
    for key in path[:-1]:
        node = node[key]
    if value is _DROP:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return doc


_DROP = object()


def test_a_single_global_eligibility_rule_is_not_representable() -> None:
    """The closed-loop numbers on WOT pulls emptied every enrichment result.
    Each regime states its own rule; a declaration missing one refuses."""
    from pydantic import ValidationError

    from src.analysis.lambda_delivery import LambdaDeliveryDeclaration

    with pytest.raises(ValidationError, match="enrichment"):
        LambdaDeliveryDeclaration.model_validate(
            _doc_with(("eligibility", "regimes", "enrichment"), _DROP)
        )
    with pytest.raises(ValidationError, match="regimes"):
        LambdaDeliveryDeclaration.model_validate(_doc_with(("eligibility", "regimes"), _DROP))


@pytest.mark.parametrize("field", ["informed_by", "held_out", "conditions", "transport_delays"])
def test_a_regime_rule_must_state_its_provenance(field: str) -> None:
    """A measured constraint is a required field: which drives chose it, which
    judged it, and under what conditions — never a default."""
    from pydantic import ValidationError

    from src.analysis.lambda_delivery import LambdaDeliveryDeclaration

    with pytest.raises(ValidationError, match=field):
        LambdaDeliveryDeclaration.model_validate(
            _doc_with(("eligibility", "regimes", "enrichment", field), _DROP)
        )


def test_a_drive_cannot_both_inform_and_judge_a_rule() -> None:
    from pydantic import ValidationError

    from src.analysis.lambda_delivery import LambdaDeliveryDeclaration

    doc = yaml.safe_load(DECLARATION_PATH.read_text())
    rule = doc["eligibility"]["regimes"]["enrichment"]
    rule["held_out"] = [*rule["held_out"], rule["informed_by"][0]]
    with pytest.raises(ValidationError, match="both informed and held out"):
        LambdaDeliveryDeclaration.model_validate(doc)


def test_a_frozen_delay_names_its_drives_and_sits_inside_its_bracket() -> None:
    from pydantic import ValidationError

    from src.analysis.lambda_delivery import LambdaDeliveryDeclaration

    path = ("eligibility", "regimes", "enrichment", "transport_delays", "ram_final_target")
    delay = yaml.safe_load(DECLARATION_PATH.read_text())
    for key in path:
        delay = delay[key]
    with pytest.raises(ValidationError, match="outside its bracket"):
        LambdaDeliveryDeclaration.model_validate(_doc_with(path, {**delay, "seconds": 0.7}))
    with pytest.raises(ValidationError, match="needs seconds, bracket_s, fitted_on"):
        LambdaDeliveryDeclaration.model_validate(_doc_with(path, {**delay, "fitted_on": []}))
    with pytest.raises(ValidationError, match="not in informed_by"):
        LambdaDeliveryDeclaration.model_validate(
            _doc_with(path, {**delay, "fitted_on": ["_capture-elsewhere-2026-01-01T000000"]})
        )
    with pytest.raises(ValidationError, match="carries no seconds"):
        LambdaDeliveryDeclaration.model_validate(
            _doc_with(path, {**delay, "status": "not_estimated", "fitted_on": []})
        )


def test_the_enrichment_delay_pairs_a_later_reading_and_closed_loop_does_not() -> None:
    """The frozen enrichment delay shifts only enrichment rows: a target read at
    t pairs with the lambda read near t + delay, still inside 250 ms."""
    doc = declaration()
    delay = doc.eligibility.regimes.delay("enrichment", "ram_final_target").seconds
    assert delay == pytest.approx(0.1)
    # Measured reads at t (the "same instant" one, wrong) and t + 0.1 (right).
    target = _long([1.2] * 4 + [1.0] * 4)  # phi: enrichment, then stoich
    early = _long([0.83] * 4 + [1.0] * 4)
    late = _long([0.80] * 4 + [1.0] * 4, start=delay)
    measured = pd.concat([early, late], ignore_index=True)
    frame = delivery_samples(
        target=target,
        measured=measured,
        declaration=doc,
        target_source="ram_final_target",
        measured_source="ecm_lambda_pid_0x24",
        afr_stoich=14.7,
    ).frame
    enrich = frame[frame["regime"] == "enrichment"]
    closed = frame[frame["regime"] == "closed_loop"]
    assert enrich["measured_lambda"].tolist() == pytest.approx([0.80] * 4)
    assert enrich["separation_s"].max() == pytest.approx(0.0, abs=1e-6)
    assert closed["separation_s"].max() == pytest.approx(0.0, abs=1e-6)


def test_the_enrichment_rule_keeps_a_ramping_pull_the_closed_loop_rule_would_drop() -> None:
    """A WOT target ramps with rpm and a shift cuts fuel just before it: 6 s
    after the cut and 0.6 s of target within 0.01 lambda is steady enough at
    WOT, where the closed-loop 15 s / 3 s rule admitted nothing (R05.16)."""
    doc = declaration()
    cut_then_pull = [0.0] + [1.18] * 12  # cut at t=0, then a steady rich target
    frame = delivery_samples(
        target=_long(cut_then_pull),
        measured=_long([2.0] + [1 / 1.18] * 12, start=0.1),
        declaration=doc,
        target_source="mode01_pid_0x44",
        measured_source="ecm_lambda_pid_0x24",
        calibration=AfrCalibration(commanded_equiv_ratio_convention="phi"),
        afr_stoich=14.7,
    ).frame
    states = frame["state"].tolist()
    assert states[0] == "fuel_cut"
    assert states[1:6] == ["post_fuel_cut"] * 5  # 1..5 s after the cut
    assert states[6:] == [ELIGIBLE] * 7


def test_delay_bracket_is_every_candidate_near_the_best() -> None:
    from src.analysis.lambda_delivery import TransportDelay, delay_bracket

    delay = TransportDelay(
        seconds=0.1,
        fitted_on=("a",),
        method="scan",
        candidates=(0.0, 0.05, 0.1, 0.15, 0.2, 0.3),
        scores=(4.28, 3.60, 3.56, 3.56, 3.60, 4.65),
        n_rows=161,
    )
    assert delay_bracket(delay, margin=0.05) == (0.05, 0.2)


def test_a_regime_scoped_delay_scan_scores_only_that_regime() -> None:
    """Closed-loop rows (target exactly 1.0) must not dilute an enrichment
    delay: with them excluded the drive is too thin to score."""
    doc = declaration()
    inputs = _lagged_drive(1.0)
    with pytest.raises(LambdaDeliveryError, match="insufficient training evidence"):
        estimate_transport_delay(
            {"driveA": DriveInputs(target=_long([1.0] * 40), measured=_long([1.0] * 40))},
            declaration=doc,
            candidates=[0.0, 1.0],
            target_source="ram_final_target",
            measured_source="ecm_lambda_pid_0x24",
            min_rows=10,
            afr_stoich=14.7,
            regimes=["enrichment"],
        )
    scoped = estimate_transport_delay(
        {"driveA": inputs},
        declaration=doc,
        candidates=[0.0, 0.5, 1.0, 1.5],
        target_source="ram_final_target",
        measured_source="ecm_lambda_pid_0x24",
        min_rows=5,
        afr_stoich=14.7,
        regimes=["enrichment"],
    )
    assert scoped.seconds == pytest.approx(1.0)
    assert "enrichment rows" in scoped.method


def test_every_commanded_source_declares_its_own_delay() -> None:
    """R05.16 verification: a delay fitted on the RAM route was applied to PID
    0x44, whose reads share a batch with the lambda read. Each source now
    carries its own entry, and a missing one refuses."""
    from pydantic import ValidationError

    from src.analysis.lambda_delivery import LambdaDeliveryDeclaration

    with pytest.raises(ValidationError, match="must name every commanded source"):
        LambdaDeliveryDeclaration.model_validate(
            _doc_with(
                ("eligibility", "regimes", "enrichment", "transport_delays", "mode01_pid_0x44"),
                _DROP,
            )
        )


def test_the_pid_0x44_route_pairs_as_acquired() -> None:
    """Its delay is not estimated: the same-batch lambda read pairs, not the
    one nearest ``t + 0.10 s``."""
    doc = declaration()
    assert doc.eligibility.regimes.delay("enrichment", "mode01_pid_0x44").status == "not_estimated"
    target = _long([1.2] * 4)
    same_batch = _long([0.83] * 4, start=0.006)
    next_batch = _long([0.80] * 4, start=0.25)
    samples = delivery_samples(
        target=target,
        measured=pd.concat([same_batch, next_batch], ignore_index=True),
        declaration=doc,
        target_source="mode01_pid_0x44",
        measured_source="ecm_lambda_pid_0x24",
        calibration=AfrCalibration(commanded_equiv_ratio_convention="phi"),
        afr_stoich=14.7,
    )
    assert samples.delays_s == {"closed_loop": 0.0, "enrichment": 0.0}
    assert samples.frame["measured_lambda"].tolist() == pytest.approx([0.83] * 4)


def test_the_sign_test_counts_reads_not_means() -> None:
    from src.analysis.lambda_delivery import PairwiseDelaySupport, per_pair_bracket, sign_test_p

    # The RAM-route 0.10 s vs 0 s split on the informing drives.
    assert sign_test_p(58, 41) == pytest.approx(0.107, abs=0.002)
    assert sign_test_p(0, 0) == 1.0
    assert sign_test_p(1681, 1507) < 0.01  # large n does not overflow
    supports = [
        PairwiseDelaySupport(0.1, 0.0, 41, 58, 62, sign_test_p(41, 58)),
        PairwiseDelaySupport(0.1, 0.1, 0, 0, 161, 1.0),
        PairwiseDelaySupport(0.1, 0.55, 62, 96, 1, sign_test_p(62, 96)),
    ]
    assert per_pair_bracket(supports) == (0.0, 0.1)
