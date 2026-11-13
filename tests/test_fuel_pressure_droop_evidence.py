"""Fuel-pressure droop evidence: limitations, induction refusals and its
standard error (M01.14).

``mint_fuel_pressure_droop`` reads a ratio of rail *gauge* pressures. That is
the injector's pressure ratio only while manifold pressure equals baro in both
groups, so every record states the limitation, a profile that does not say
whether the engine can boost is refused, and a forced-induction profile is
refused without a manifold-pressure channel. The standard error is checked
against a Monte Carlo of the case that made it wrong: overlapping windows.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

import src.analysis.delivery_evidence as delivery_evidence
from src.analysis.delivery_evidence import (
    DROOP_LIMITATIONS,
    DROOP_TOOL_VERSION,
    MANIFOLD_PRESSURE_SPECS,
    _median_se,
    _non_overlapping,
    _window_means,
    mint_fuel_pressure_droop,
)
from src.analysis.fault_model import FAULT_MODEL_FILE, FaultModel, load_fault_model
from src.analysis.tuning_context import TuningContext
from src.config import CanaryConfig, ProfilesConfig
from src.evidence import Evidence, MemoryLedger
from src.vehicle.profiles import profile_dir, resolve_profile_for_tooling, scope_config
from src.vehicle.profiles.model import read_registry_file

FIXTURES = Path("tests/fixtures/delivery_investigation")
PRESSURE_DRIVE = FIXTURES / "2026-10-04T194342"
NO_COMMAND = FIXTURES / "_capture-rom-trace-2026-09-21T195647"
PROFILE = "nissan-xterra-2008"
NOW = datetime(2026, 11, 13, tzinfo=UTC)


@pytest.fixture(scope="module")
def xterra() -> tuple[TuningContext, FaultModel, str]:
    base = CanaryConfig.from_yaml(Path("canary.dev.example.yaml"))
    config = scope_config(base, resolve_profile_for_tooling(base, PROFILE))
    model, sha = load_fault_model(profile_dir(config) / FAULT_MODEL_FILE)
    return TuningContext.from_config(config), model, sha


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[MemoryLedger]:
    yield MemoryLedger(tmp_path / "memory.jsonl", vehicle=PROFILE)


def _mint(
    ledger: MemoryLedger,
    xterra: tuple[TuningContext, FaultModel, str],
    session: Path = PRESSURE_DRIVE,
    *,
    region: delivery_evidence.DroopRegion = "high_airflow",
    induction: str | None = None,
) -> Evidence:
    context, model, sha = xterra
    if induction is not None:
        context = dataclasses.replace(context, induction=induction)  # type: ignore[arg-type]
    evidence_id = mint_fuel_pressure_droop(
        ledger, session, context, model, sha, region=region, vin=None, program_id=None, now=NOW
    )
    record = ledger.resolve_evidence(evidence_id).record
    assert isinstance(record, Evidence)
    return record


def test_the_dev_xterra_states_naturally_aspirated_and_the_2013_does_not(
    xterra: tuple[TuningContext, FaultModel, str],
) -> None:
    profiles = {p.slug: p for p in read_registry_file(ProfilesConfig().registry_path)}
    assert profiles["nissan-xterra-2008"].parameters.induction == "naturally_aspirated"
    # Another operator's truck: its owner has not said, so it is not assumed.
    assert profiles["nissan-xterra-2013"].parameters.induction == "undeclared"
    assert xterra[0].induction == "naturally_aspirated"  # from_config carries it
    assert TuningContext().induction == "undeclared"


def test_every_droop_record_states_that_manifold_pressure_is_not_in_it(
    ledger: MemoryLedger, xterra: tuple[TuningContext, FaultModel, str]
) -> None:
    assert DROOP_TOOL_VERSION == "3"
    evaluated = _mint(ledger, xterra)
    uncovered = _mint(ledger, xterra, region="enrichment")
    missing = _mint(ledger, xterra, NO_COMMAND)
    assert evaluated.result.status == "evaluated"
    assert uncovered.result.reason == "uncovered"
    assert missing.result.reason == "missing_input"
    for record in (evaluated, uncovered, missing):
        assert record.labels["limitations"] == DROOP_LIMITATIONS
        assert record.labels["induction"] == "naturally_aspirated"
    assert "manifold pressure is not in it" in DROOP_LIMITATIONS


def test_an_undeclared_induction_is_refused_before_anything_is_read(
    ledger: MemoryLedger, xterra: tuple[TuningContext, FaultModel, str]
) -> None:
    record = _mint(ledger, xterra, induction="undeclared")
    assert record.result.status == "unevaluable"
    assert record.result.reason == "unknown_context"
    assert record.labels["empty_kind"] == "not_declared"
    assert "declares no induction" in (record.result.detail or "")
    assert record.labels["limitations"] == DROOP_LIMITATIONS


def test_forced_induction_is_refused_by_name_with_or_without_manifold_pressure(
    ledger: MemoryLedger,
    xterra: tuple[TuningContext, FaultModel, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The 2026-10-04 drive is the NA truck: no MAP channel was recorded.
    regions: tuple[delivery_evidence.DroopRegion, ...] = ("high_airflow", "enrichment")
    for region in regions:
        record = _mint(ledger, xterra, region=region, induction="forced")
        assert record.result.status == "unevaluable" and record.result.reason == "refused"
        detail = record.result.detail or ""
        assert detail.startswith("forced_induction_without_manifold_pressure")
        assert all(spec in detail for spec in MANIFOLD_PRESSURE_SPECS)
        assert record.labels["induction"] == "forced"
    # With a manifold-pressure channel on record it is still refused: the
    # gauge ratio is not the injector's ratio under boost, and the pressure
    # across the injector is not computed yet.
    real = delivery_evidence.session_specs(PRESSURE_DRIVE)
    monkeypatch.setattr(
        delivery_evidence, "session_specs", lambda _s: real | {"obd:INTAKE_PRESSURE"}
    )
    for region in regions:
        record = _mint(ledger, xterra, region=region, induction="forced")
        assert record.result.status == "unevaluable" and record.result.reason == "refused"
        detail = record.result.detail or ""
        assert detail.startswith("forced_induction_delta_p_not_implemented")
        assert record.labels["limitations"] == DROOP_LIMITATIONS


def test_non_overlapping_windows_share_no_pressure_sample() -> None:
    times = np.array([0.0, 0.1, 0.5, 0.51, 1.2, 1.0])
    kept = _non_overlapping(times, 0.25)
    # 0.0 kept; 0.1 and 0.5 overlap it (0.5 shares the edge sample); 0.51 is
    # clear; 1.0 overlaps 0.51; 1.2 is clear of 0.51 by 0.69 s.
    assert sorted(times[kept].tolist()) == [0.0, 0.51, 1.2]
    assert _median_se(np.array([0.0, 0.1]), np.array([1.0, 2.0]), 0.25) == float("inf")


def test_median_se_is_calibrated_on_overlapping_windows() -> None:
    """SYNTHETIC Monte Carlo: 100 Hz rail pressure with AR(1) noise (phi 0.6,
    sd 2 kPa innovations), 60 s per drive, 590 MAF reads at uniform random
    times (10 Hz) each taking the +-0.25 s window mean. Across 300 drives the
    spread of the median is the truth the standard error must predict.

    Measured 2026-11-13 (seed 11): empirical sd of the median 0.0771 kPa; the
    old all-reads SE averaged 0.0355 (1/2.17 of it, the "about 2x too tight"
    of M01.14); the non-overlapping SE 0.0875 (1.14x, conservative because the
    median still uses the reads between the kept windows). The same setup at
    4 and 15 Hz read 1.69x and 2.62x too tight before, 1.03x and 1.11x after.
    """
    rng = np.random.default_rng(11)
    drives, rate_hz, span_s, dt = 300, 10.0, 60.0, 0.01
    pt = np.arange(0.0, span_s, dt)
    noise = rng.normal(0.0, 1.0, (drives, len(pt)))
    for i in range(1, len(pt)):
        noise[:, i] += 0.6 * noise[:, i - 1]
    pressure = 350.0 + 2.0 * noise
    n_reads = int(rate_hz * (span_s - 1.0))
    medians, old, new = [], [], []
    for d in range(drives):
        at = np.sort(rng.uniform(0.5, span_s - 0.5, n_reads))
        means, count = _window_means(pt, pressure[d], at, 0.25)
        assert (count > 0).all()
        medians.append(float(np.median(means)))
        old.append(1.2533 * float(np.std(means, ddof=1)) / np.sqrt(len(means)))
        new.append(_median_se(at, means, 0.25))
    truth = float(np.std(medians, ddof=1))
    assert truth / float(np.mean(old)) > 1.8  # the old error: about 2x too tight
    assert 0.9 <= float(np.mean(new)) / truth <= 1.3  # calibrated, slightly conservative
