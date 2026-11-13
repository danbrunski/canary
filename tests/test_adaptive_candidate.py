"""E03.5 kernel: the adaptive candidate cannot absorb a fault (synthetic data).

Every block here is synthetic.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

import src.models.adaptive_candidate as candidate_module
import src.models.change_detection as change_module
from src.analysis.change_evaluation import replay_candidate
from src.evidence.ledger import MemoryLedger
from src.evidence.records import (
    Conditions,
    Evidence,
    Investigation,
    Provenance,
    Quality,
    Quantity,
    Result,
    SourceRef,
    VehicleIdentity,
)
from src.models.adaptive_candidate import (
    CalibrationBoundaryError,
    CandidateCheckpoint,
    CandidateSpec,
    Disposition,
    InterventionBoundaryError,
    KalmanCandidate,
    PredictionOrderError,
    batch_fit,
    candidate_from_ledger,
)
from src.models.change_detection import (
    Block,
    ChangeMonitor,
    ChangePolicy,
    FrozenReference,
    fit_reference,
)

T0 = 1.8e9
DAY = 86_400.0
TRUE_THETA = (-1.0, 4.0)  # proportional %, unmetered % * g/s
CHANNELS = ("obd:SHORT_FUEL_TRIM_1", "obd:SHORT_FUEL_TRIM_2", "obd:MAF")
PROFILE = "test-vehicle"


def _policy() -> ChangePolicy:
    return ChangePolicy.model_validate(
        {
            "schema": "canary-change-detection/2",
            "residual": "balance",
            "unit": "percent",
            "features": ["1", "1/obd:MAF"],
            "excitation": {
                "condition": "obd:MAF",
                "unit": "g/s",
                "cells": [
                    {"name": "idle", "lower": 0.0, "upper": 6.0},
                    {"name": "part_load", "lower": 6.0, "upper": 400.0},
                ],
                "min_block_samples": 3,
            },
            "dependence": {
                "block_seconds": 60.0,
                "max_gap_seconds": 10.0,
                "correction": "ar1_variance_inflation",
                "max_within_drive_lag1": 0.5,
            },
            "calibration": {
                "miscoverage": 0.1,
                "n_calibration_drives": 2,
                "n_test_drives": 1,
                "min_train_drives": 2,
                "min_cell_blocks": 8,
                "min_cell_drives": 2,
                "max_bias_fraction": 0.5,
            },
            "detectors": {
                "shift_of_interest": 3.0,
                "cusum_h": 5.0,
                "glr_window_blocks": 30,
                "glr_h": 8.0,
            },
            "drive_level": {
                "cusum_h": 4.0,
                "glr_h": 5.0,
                "window_drives": 10,
                "between_drive_sd_floor": 0.3,
                "min_drive_blocks": 3,
            },
            "persistence": {"min_consecutive_blocks": 2, "clip_sigma": 3.0},
            "deduplication": {"merge_seconds": 600.0, "reset_at_drive_boundary": True},
            "shifts": [
                {
                    "name": "s",
                    "kind": "step",
                    "magnitude": 3.0,
                    "start_after_seconds": 0.0,
                    "ramp_seconds": 0.0,
                    "synthetic": True,
                }
            ],
        }
    )


def _blocks(
    drive: str,
    mafs: np.ndarray,
    *,
    seed: int,
    t0: float = T0,
    theta: tuple[float, float] = TRUE_THETA,
    noise: float = 0.3,
    offset: float = 0.0,
) -> list[Block]:
    rng = np.random.default_rng(seed)
    out = []
    for i, maf in enumerate(mafs):
        x = (1.0, 1.0 / float(maf))
        y = theta[0] + theta[1] * x[1] + offset + float(rng.normal(0.0, noise))
        start = t0 + 60.0 * i
        cell = "idle" if maf < 6.0 else "part_load"
        out.append(Block(drive, start, start + 57.0, cell, 20, y, x, float(maf)))
    return out


def _mixed(drive: str, n: int, *, seed: int, t0: float = T0, **kw: object) -> list[Block]:
    # A drive: idle at the lights, cruise between them.
    rng = np.random.default_rng(seed + 100)
    mafs = np.where(rng.random(n) < 0.4, rng.uniform(3.5, 4.8, n), rng.uniform(15.0, 60.0, n))
    return _blocks(drive, mafs, seed=seed, t0=t0, **kw)  # type: ignore[arg-type]


def _idle(drive: str, n: int, *, seed: int, t0: float = T0) -> list[Block]:
    mafs = np.random.default_rng(seed + 100).uniform(3.6, 4.6, n)
    return _blocks(drive, mafs, seed=seed, t0=t0)


def _reference(seed: int = 0) -> FrozenReference:
    train = _mixed("t1", 30, seed=1 + seed, t0=T0 - 4 * DAY) + _mixed(
        "t2", 30, seed=2 + seed, t0=T0 - 3 * DAY
    )
    cal = _mixed("c1", 30, seed=3 + seed, t0=T0 - 2 * DAY) + _mixed(
        "c2", 30, seed=4 + seed, t0=T0 - DAY
    )
    return fit_reference(_policy(), train, cal, name="balance", calibration_revision="REV")


def _spec(**overrides: object) -> CandidateSpec:
    raw: dict[str, object] = {
        "schema": "canary-adaptive-candidate/2",
        "name": "balance_kalman",
        "residual": "balance",
        "evidence_class": "engine_response",
        "features": ["1", "1/obd:MAF"],
        "parameters": ["proportional_imbalance", "unmetered_air_imbalance"],
        "units": ["percent", "percent*g/s"],
        "prior_sd": [2.0, 10.0],
        "random_walk_sd_per_hour": [0.05, 0.25],
        "observation_sd": 0.3,
        "excitation_window_blocks": 10,
        "min_information_eigenvalue": 0.002,
        "identified_sd": [0.5, 2.0],
        "accepted_dispositions": ["accept"],
        "divergence_probes": [[1.0, 0.25], [1.0, 1 / 30], [1.0, 0.01]],
    }
    raw.update(overrides)
    return CandidateSpec.model_validate(raw)


@pytest.fixture
def ledger(tmp_path: Path) -> MemoryLedger:
    return MemoryLedger(tmp_path / "memory.jsonl", vehicle=PROFILE)


def _make(
    ledger: MemoryLedger, reference: FrozenReference | None = None, **kw: Any
) -> KalmanCandidate:
    spec = kw.pop("spec", _spec())
    return candidate_from_ledger(
        ledger,
        spec,
        reference or _reference(),
        calibration=kw.pop("calibration", "REV"),
        channels=CHANNELS,
        now=datetime.fromtimestamp(T0 + 30 * DAY, UTC),
        **kw,
    )


def _run(
    candidate: KalmanCandidate, blocks: list[Block], disposition: Disposition = "accept"
) -> list[str]:
    """Monitor, predict, learn each block of one drive (the replay order)."""
    monitor = candidate.monitor
    monitor.start_drive(blocks[0].drive)
    candidate.start_drive()
    out: list[str] = []
    for b in blocks:
        verdict = monitor.observe(b)
        out.append(
            candidate.learn(
                candidate.predict(b), verdict, disposition=disposition, calibration="REV"
            )
        )
    monitor.close_drive()
    return out


# -- construction ----------------------------------------------------------


def test_a_candidate_is_only_built_from_memory(ledger):
    reference = _reference()
    with pytest.raises(TypeError, match="candidate_from_ledger"):
        KalmanCandidate(
            object(), _spec(), reference, monitor=ChangeMonitor(reference), calibration="REV"
        )


def test_the_factory_reads_unresolved_alarms_from_memory(ledger):
    alarm = _alarm(ledger)
    candidate = _make(ledger)
    assert alarm in candidate.monitor.open_alarms
    assert set(_run(candidate, _mixed("d", 20, seed=1))) == {"frozen"}
    ledger.append(
        Investigation(
            vehicle=VehicleIdentity(profile=PROFILE, vin=None, program_id=None, calibration="REV"),
            provenance=Provenance(producer="operator", producer_kind="operator", version="1"),
            conditions=Conditions(regime="all conditions", vehicle_state=None),
            valid_from=datetime.fromtimestamp(T0, UTC),
            title="Bank balance moved",
            question="Did the bank balance change?",
            status="resolved",
            observations=(alarm,),
        )
    )
    assert "learned" in _run(_make(ledger), _mixed("d", 40, seed=1))


# -- monitor, predict, learn ---------------------------------------------------


def test_every_learned_block_was_predicted_from_the_state_before_it(ledger):
    candidate = _make(ledger)
    blocks = _mixed("d", 60, seed=7)
    monitor = candidate.monitor
    monitor.start_drive("d")
    theta_before = []
    for b in blocks:
        theta_before.append(candidate.theta)
        verdict = monitor.observe(b)
        candidate.learn(candidate.predict(b), verdict, disposition="accept", calibration="REV")
    log = list(candidate.log)
    assert sum(r.outcome == "learned" for r in log) >= 10
    for record, theta, block in zip(log, theta_before, blocks, strict=True):
        assert record.prediction == pytest.approx(float(np.dot(theta, block.x)))


def test_learning_needs_a_fresh_prediction_and_the_monitors_verdict_for_that_block(ledger):
    candidate = _make(
        ledger, spec=_spec(excitation_window_blocks=2, min_information_eigenvalue=1e-9)
    )
    monitor = candidate.monitor
    a, b, c, d = _mixed("d", 4, seed=1)
    monitor.start_drive("d")
    stale = candidate.predict(a)
    va = monitor.observe(a)
    vb = monitor.observe(b)
    with pytest.raises(PredictionOrderError, match="verdict"):
        candidate.learn(candidate.predict(a), va, disposition="accept", calibration="REV")
    with pytest.raises(PredictionOrderError, match="verdict"):  # verdict of another block
        candidate.learn(candidate.predict(a), vb, disposition="accept", calibration="REV")
    other = ChangeMonitor(candidate.monitor.reference)
    other.start_drive("d")
    with pytest.raises(PredictionOrderError, match="verdict"):  # another monitor's verdict
        candidate.learn(
            candidate.predict(b), other.observe(b), disposition="accept", calibration="REV"
        )
    vc = monitor.observe(c)
    used = candidate.predict(c)
    assert candidate.learn(used, vc, disposition="accept", calibration="REV") in (
        "learned",
        "unexcited",
    )
    with pytest.raises(PredictionOrderError):
        candidate.learn(used, vc, disposition="accept", calibration="REV")
    vd = monitor.observe(d)
    candidate.learn(candidate.predict(d), vd, disposition="accept", calibration="REV")
    if candidate.log[-1].outcome == "learned":
        with pytest.raises(PredictionOrderError, match="stale"):
            candidate.learn(stale, vd, disposition="accept", calibration="REV")


def test_an_in_drive_alarm_freezes_the_rest_of_the_drive_and_later_drives(ledger):
    candidate = _make(ledger)
    shifted = _mixed("d", 60, seed=2, offset=4.0)
    outcomes = _run(candidate, shifted)
    first = outcomes.index("frozen")
    assert set(outcomes[first:]) == {"frozen"}
    assert candidate.monitor.events, "the frozen reference sees the shift"
    assert set(_run(candidate, _mixed("e", 30, seed=3, t0=T0 + DAY))) == {"frozen"}


def test_an_unmonitored_block_is_not_learned(ledger):
    raw = _policy().model_dump(mode="json", by_alias=True)
    raw["excitation"]["cells"].append({"name": "high", "lower": 400.0, "upper": 1000.0})
    candidate = _make(ledger)
    high = _blocks("h", np.full(12, 500.0), seed=1)
    high = [dataclasses.replace(b, cell="high") for b in high]
    assert set(_run(candidate, high)) == {"unmonitored"}


# -- excitation ------------------------------------------------------------


def test_idle_only_data_cannot_identify_the_airflow_split(ledger):
    candidate = _make(ledger)
    start = candidate.theta
    assert set(_run(candidate, _idle("idle", 120, seed=3))) == {"unexcited"}
    assert candidate.theta == start
    eigen = candidate.information_min_eigenvalue()
    assert eigen is not None and eigen < 0.002
    assert not any(v["identified"] for v in candidate.identifiability().values())


def test_idle_only_least_squares_fixes_one_combination_not_each_parameter():
    idle_theta, idle_cov = batch_fit(_idle("a", 60, seed=11))
    mixed_theta, mixed_cov = batch_fit(_mixed("m", 60, seed=13))
    assert np.sqrt(idle_cov[1, 1]) > 5 * np.sqrt(mixed_cov[1, 1])
    at_idle = np.array([1.0, 1.0 / 4.1])
    assert float(np.sqrt(at_idle @ idle_cov @ at_idle)) < 0.1
    assert np.allclose(mixed_theta, TRUE_THETA, atol=0.5)
    assert idle_theta.shape == (2,)


def test_mixed_airflow_identifies_both_parameters(ledger):
    candidate = _make(ledger)
    for k in range(4):
        _run(candidate, _mixed(f"d{k}", 50, seed=5 + k, t0=T0 + k * DAY, theta=(-0.5, 5.0)))
    assert all(v["identified"] for v in candidate.identifiability().values())
    assert candidate.theta == pytest.approx((-0.5, 5.0), abs=0.6)


# -- divergence guard ----------------------------------------------------------


def test_the_candidate_cannot_drift_past_the_shift_of_interest(ledger):
    """A slow change too small per drive for the per-block detector: the
    divergence guard stops the candidate at the reference's shift of interest."""
    reference = _reference()
    candidate = _make(ledger, reference)
    limit = reference.alarm.shift_of_interest / 2.0
    outcomes: list[str] = []
    for k in range(20):
        blocks = _mixed(f"d{k}", 25, seed=40 + k, t0=T0 + k * DAY, offset=0.3 * k)
        outcomes += _run(candidate, blocks)
        assert candidate.divergence() <= limit + 1e-9
    assert candidate.divergence_alarms or candidate.monitor.events
    if candidate.divergence_alarms:
        assert "diverged" in outcomes


# -- gates -----------------------------------------------------------------------


def test_quarantined_data_is_never_learned(ledger):
    candidate = _make(ledger)
    start = candidate.theta
    assert set(_run(candidate, _mixed("q", 30, seed=1), "quarantine")) == {"rejected_disposition"}
    assert candidate.theta == start


def test_a_candidate_cannot_learn_across_an_invalidating_intervention(ledger):
    boundary = datetime.fromtimestamp(T0 + 600.0, UTC)
    candidate = candidate_from_ledger(
        ledger, _spec(), _reference(), calibration="REV", channels=CHANNELS
    )
    candidate._boundary = boundary  # the factory reads it from the ledger; tested there too
    with pytest.raises(InterventionBoundaryError, match="intervention"):
        _run(candidate, _mixed("d", 30, seed=1))
    assert all(r.end < boundary.timestamp() for r in candidate.log)


def test_a_candidate_belongs_to_its_reference_calibration(ledger):
    with pytest.raises(CalibrationBoundaryError):
        _make(ledger, calibration="REV-2")
    candidate = _make(ledger)
    block = _mixed("d", 1, seed=1)[0]
    candidate.monitor.start_drive("d")
    verdict = candidate.monitor.observe(block)
    with pytest.raises(CalibrationBoundaryError):
        candidate.learn(
            candidate.predict(block), verdict, disposition="accept", calibration="REV-2"
        )


# -- checkpoints and lineage -------------------------------------------------


def test_checkpoints_carry_valid_time_and_lineage(ledger):
    reference = _reference()
    candidate = _make(ledger, reference)
    _run(candidate, _mixed("d", 40, seed=1))
    first = candidate.checkpoint()
    _run(candidate, _mixed("e", 40, seed=2, t0=T0 + DAY))
    second = candidate.checkpoint()
    assert first.parent is None and second.parent == first.fingerprint
    assert second.reference_fingerprint == reference.fingerprint
    assert second.calibration == "REV"
    assert first.valid_to is not None and second.valid_to is not None
    assert second.valid_to > first.valid_to and second.valid_from == first.valid_from
    resumed = _make(ledger, reference, parent=second)
    assert resumed.theta == candidate.theta
    tampered = second.model_dump(mode="json")
    tampered["theta"] = [9.0, 9.0]
    with pytest.raises(ValidationError, match="fingerprint"):
        CandidateCheckpoint.model_validate(tampered)
    with pytest.raises(ValueError, match="lineage"):
        _make(ledger, _reference(seed=20), parent=second)


# -- the twin stays exact ------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"evidence_class": "ecu_computation"},
        {"features": ["1", "twin:TARGET_PHI"]},
        {"parameters": ["proportional_imbalance", "rom:TARGET_AFR_TABLE"]},
        {"residual": "twin:target_phi"},
    ],
)
def test_a_candidate_cannot_name_ecu_computation(override):
    with pytest.raises(ValidationError):
        _spec(**override)


def test_the_learning_kernels_cannot_reach_the_twin():
    for module in (candidate_module, change_module):
        tree = ast.parse(Path(str(module.__file__)).read_text())
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        src = {name for name in imported if name.startswith("src.")}
        # ``src.artifact_hash`` is a stdlib-only leaf (R24.5): it cannot reach the twin.
        assert all(
            name in ("src.models.change_detection", "src.artifact_hash")
            or name.startswith("src.evidence.")
            for name in src
        ), src


# -- replay: a fault developing between drives (synthetic) ---------------------


def _replay_drives(n: int, per_drive: float, start: int) -> list[tuple[str, list[Block]]]:
    return [
        (
            f"r{k}",
            _mixed(
                f"r{k}", 20, seed=200 + k, t0=T0 + k * DAY, offset=per_drive * max(0, k - start + 1)
            ),
        )
        for k in range(n)
    ]


def test_replay_a_fault_developing_between_drives_alarms_freezes_and_bounds_the_candidate(ledger):
    """+0.4 points per drive from drive 3 on: (a) the drive level alarms,
    (b) the candidate is frozen from the next drive on, (c) it never moves
    further than the shift of interest from the reference."""
    reference = _reference()
    frozen = reference.model_dump(mode="json")
    drives = _replay_drives(16, 0.4, start=3)
    traces, candidate = replay_candidate(
        drives, reference, _spec(), ledger=ledger, calibration="REV", channels=CHANNELS,
        now=datetime.fromtimestamp(T0 + 40 * DAY, UTC),
    )  # fmt: skip
    first = next(
        i for i, t in enumerate(traces) if t.drive_alarms or t.block_alarms or t.divergence_alarms
    )
    assert any(t.drive_alarms for t in traces), "(a) the drive-level detector alarms"
    assert all(t.frozen_at_start for t in traces[first + 1 :])
    assert all(t.outcomes.get("learned", 0) == 0 for t in traces[first + 1 :]), "(b)"
    assert max(t.divergence for t in traces) <= reference.alarm.shift_of_interest / 2.0, "(c)"
    assert reference.model_dump(mode="json") == frozen
    assert traces[-1].reference_residual is not None and traces[-1].reference_residual > 3.0


def test_replay_an_intra_drive_ramp_bounds_absorption_before_the_alarm(ledger):
    reference = _reference()
    blocks = _mixed("ramp", 60, seed=300, t0=T0)
    ramp = [dataclasses.replace(b, y=b.y + 4.0 * min(1.0, i / 15)) for i, b in enumerate(blocks)]
    traces, candidate = replay_candidate(
        [("ramp", ramp)], reference, _spec(), ledger=ledger, calibration="REV", channels=CHANNELS,
        now=datetime.fromtimestamp(T0 + DAY, UTC),
    )  # fmt: skip
    assert traces[0].block_alarms
    learned_before = [r for r in candidate.log if r.outcome == "learned"]
    # Before the alarm the candidate moved its prediction by less than half
    # the injected ramp's final size, and stayed inside the guard.
    assert traces[0].divergence < 2.0
    assert all(r.outcome in ("learned", "unexcited", "frozen") for r in candidate.log)
    assert len(learned_before) < len(ramp)


def _alarm(ledger: MemoryLedger) -> str:
    t = datetime.fromtimestamp(T0, UTC)
    return ledger.append(
        Evidence(
            vehicle=VehicleIdentity(profile=PROFILE, vin=None, program_id=None, calibration="REV"),
            provenance=Provenance(
                producer="change_detection", producer_kind="tool", version="1", code_version="x"
            ),
            conditions=Conditions(regime="test", vehicle_state="engine_running"),
            valid_from=t,
            subject="change_alarm:balance:test",
            incident="change:balance",
            sources=(
                SourceRef(
                    session="2026-09-25T120000",
                    window_start=t,
                    window_end=t + timedelta(minutes=5),
                    channels=("obd:SHORT_FUEL_TRIM_1",),
                    n_samples=10,
                ),
            ),
            input_hashes={"samples": "0" * 64},
            policy={"p": 1},
            values={"shift_estimate": Quantity(value=3.0, unit="percent")},
            labels={"kind": "change_alarm", "data_origin": "observed"},
            quality=Quality(n_observations=2, timestamp_basis="acquired"),
            result=Result(status="evaluated"),
        )
    ).id
