"""The adaptive-candidate fixtures: policies, references, alarms and mixed drives (R30.2).

Moved out of the ``tests/test_*.py`` files that provided them, bodies unchanged; private helpers
got public names."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from src.evidence.ledger import MemoryLedger
from src.evidence.records import (
    Conditions,
    Evidence,
    Provenance,
    Quality,
    Quantity,
    Result,
    SourceRef,
    VehicleIdentity,
)
from src.models.adaptive_candidate import CandidateSpec
from src.models.change_detection import Block, ChangePolicy, FrozenReference, fit_reference

T0 = 1.8e9


DAY = 86_400.0


TRUE_THETA = (-1.0, 4.0)  # proportional %, unmetered % * g/s


CHANNELS = ("obd:SHORT_FUEL_TRIM_1", "obd:SHORT_FUEL_TRIM_2", "obd:MAF")


PROFILE = "test-vehicle"


def candidate_policy() -> ChangePolicy:
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


def candidate_blocks(
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


def candidate_mixed(drive: str, n: int, *, seed: int, t0: float = T0, **kw: object) -> list[Block]:
    # A drive: idle at the lights, cruise between them.
    rng = np.random.default_rng(seed + 100)
    mafs = np.where(rng.random(n) < 0.4, rng.uniform(3.5, 4.8, n), rng.uniform(15.0, 60.0, n))
    return candidate_blocks(drive, mafs, seed=seed, t0=t0, **kw)  # type: ignore[arg-type]


def candidate_reference(seed: int = 0) -> FrozenReference:
    train = candidate_mixed("t1", 30, seed=1 + seed, t0=T0 - 4 * DAY) + candidate_mixed(
        "t2", 30, seed=2 + seed, t0=T0 - 3 * DAY
    )
    cal = candidate_mixed("c1", 30, seed=3 + seed, t0=T0 - 2 * DAY) + candidate_mixed(
        "c2", 30, seed=4 + seed, t0=T0 - DAY
    )
    return fit_reference(candidate_policy(), train, cal, name="balance", calibration_revision="REV")


def candidate_spec(**overrides: object) -> CandidateSpec:
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


def candidate_alarm(ledger: MemoryLedger) -> str:
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
