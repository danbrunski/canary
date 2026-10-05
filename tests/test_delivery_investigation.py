"""E03.1: the fuel-delivery evidence/memory loop.

**Real drives** — committed slices (``tests/fixtures/delivery_investigation``,
cut with ``tools/make_ground_truth_fixture.py``; each directory is named as
its source session so calibration and B06's ``informed_by`` resolve as on the
truck; each slice reproduces its full session's delivery error exactly):

* ``_capture-tuning-2026-09-23T164007`` — the WOT-lean drive (+3.30 %, 44
  rows). It is in B06's enrichment ``informed_by``: in-sample, so it opens
  nothing.
* the five held-out enrichment drives of ``lambda_delivery.yaml``
  (``_capture-tuning-2026-09-23T142926`` −1.14 %, ``…09-22T170459`` +0.17 %,
  ``…09-22T235224`` +0.74 %, ``…hybrid-research-2026-09-17T191712`` −0.46 %,
  ``…T201000`` −0.66 %): every one within the 2.50 % floor. Out of sample
  there is no qualified discrepancy today.
* ``2026-10-04T194342`` — first drive with fuel pressure: no full-throttle
  pull (enrichment uncovered), closed-loop droop at high airflow.
* ``_capture-rom-trace-2026-09-21T195647`` — Mode 01 disabled: commanded AFR
  absent by construction.

**Labelled synthetic drives** (generated here, never committed, named
``_synthetic-…``) carry the open → predict → verify loop the real data cannot:
``A`` is lean 4 % at enrichment without fuel pressure; ``B``, a later drive,
is lean 4 % again with fuel pressure that holds through the pulls.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import pytest

from src.analysis.delivery_evidence import (
    DELIVERY_INCIDENT,
    DELIVERY_TOOL,
    INFORMED_REFUSAL,
    mint_delivery_error,
    mint_fuel_pressure_droop,
)
from src.analysis.delivery_investigation import (
    InvestigationStep,
    NotEngineResponseError,
    NotNextDriveError,
    detect_discrepancy,
    evaluate_outcome,
    investigate,
    predict_pressure_flow_loss,
    predict_recurrence,
    retrieve,
)
from src.analysis.fault_model import FAULT_MODEL_FILE, FaultModel, Feature, load_fault_model
from src.analysis.tuning import delivery_error
from src.analysis.tuning_context import TuningContext
from src.config import CanaryConfig
from src.evidence import (
    Conditions,
    Evidence,
    Investigation,
    MemoryLedger,
    Outcome,
    Prediction,
    Provenance,
    Quality,
    Quantity,
    Result,
    SourceRef,
    investigation_card,
    render_text,
)
from src.vehicle.profiles import profile_dir, resolve_profile_for_tooling, scope_config

FIXTURES = Path("tests/fixtures/delivery_investigation")
WOT_LEAN = FIXTURES / "_capture-tuning-2026-09-23T164007"
PRESSURE_DRIVE = FIXTURES / "2026-10-04T194342"
NO_COMMAND = FIXTURES / "_capture-rom-trace-2026-09-21T195647"
#: lambda_delivery.yaml enrichment held_out, with the residual each gives.
HELD_OUT = {
    "_capture-tuning-2026-09-23T142926": (-1.14, 24),
    "_capture-tuning-2026-09-22T170459": (0.17, 22),
    "_capture-tuning-2026-09-22T235224": (0.74, 21),
    "_capture-tuning-hybrid-research-2026-09-17T191712": (-0.46, 67),
    "_capture-tuning-hybrid-research-2026-09-17T201000": (-0.66, 46),
}
FUEL_PRESSURE_MEASUREMENT = "Log fuel pressure through a full-throttle pull"
PROFILE = "nissan-xterra-2008"
REGIONS: tuple[Literal["high_airflow", "enrichment"], ...] = ("high_airflow", "enrichment")


@dataclass
class Vehicle:
    context: TuningContext
    model: FaultModel
    model_sha: str
    data: Path

    @property
    def floor_conditions(self) -> str:
        assert self.context.lambda_delivery is not None
        return self.context.lambda_delivery.noise_floors["enrichment"].conditions


@pytest.fixture(scope="module")
def vehicle() -> Vehicle:
    base = CanaryConfig.from_yaml(Path("canary.dev.example.yaml"))
    config = scope_config(base, resolve_profile_for_tooling(base, PROFILE))
    data = profile_dir(config)
    model, sha = load_fault_model(data / FAULT_MODEL_FILE)
    return Vehicle(TuningContext.from_config(config), model, sha, data)


class Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def at(self, start: datetime) -> None:
        self.t = max(self.t, start)

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


def _evidence(ledger: MemoryLedger, evidence_id: str) -> Evidence:
    record = ledger.resolve_evidence(evidence_id).record
    assert isinstance(record, Evidence)
    return record


def _mint_all(
    ledger: MemoryLedger, vehicle: Vehicle, session: Path, clock: Clock
) -> tuple[str, str, str]:
    """Delivery, closed-loop droop and enrichment flow loss for one drive."""
    delivery = mint_delivery_error(
        ledger, session, vehicle.context, vehicle.model, vehicle.model_sha,
        vin=None, program_id=None, now=clock(),
    )  # fmt: skip
    high, loss = (
        mint_fuel_pressure_droop(
            ledger,
            session,
            vehicle.context,
            vehicle.model,
            vehicle.model_sha,
            region=region,
            vin=None,
            program_id=None,
            now=clock(),
        )  # fmt: skip
        for region in REGIONS
    )
    return delivery, high, loss


# ---------------------------------------------------------------------------
# Real drives


@dataclass
class Real:
    ledger: MemoryLedger
    wot_lean: str
    wot_lean_step: InvestigationStep
    held_out: dict[str, str]
    pressure: tuple[str, str, str]
    no_command: tuple[str, str, str]


@pytest.fixture(scope="module")
def real(vehicle: Vehicle, tmp_path_factory: pytest.TempPathFactory) -> Real:
    ledger = MemoryLedger(tmp_path_factory.mktemp("real") / "memory.jsonl", vehicle=PROFILE)
    clock = Clock(datetime(2026, 9, 24, tzinfo=UTC))
    wot = mint_delivery_error(
        ledger, WOT_LEAN, vehicle.context, vehicle.model, vehicle.model_sha,
        vin=None, program_id=None, now=clock(),
    )  # fmt: skip
    step = investigate(ledger, vehicle.model, delivery_evidence=wot, now=clock())
    held = {}
    for name in HELD_OUT:
        held[name] = mint_delivery_error(
            ledger, FIXTURES / name, vehicle.context, vehicle.model, vehicle.model_sha,
            vin=None, program_id=None, now=clock(),
        )  # fmt: skip
        investigate(ledger, vehicle.model, delivery_evidence=held[name], now=clock())
    clock.at(datetime(2026, 10, 4, 21, tzinfo=UTC))
    pressure = _mint_all(ledger, vehicle, PRESSURE_DRIVE, clock)
    no_command = _mint_all(ledger, vehicle, NO_COMMAND, clock)
    return Real(ledger, wot, step, held, pressure, no_command)


def test_delivery_error_mints_evidence_from_b06_without_rebuilding_it(
    vehicle: Vehicle, real: Real
) -> None:
    ctx = vehicle.context
    assert ctx.lambda_delivery is not None and ctx.mode01_wot is not None
    direct = delivery_error(
        WOT_LEAN,
        declaration=ctx.lambda_delivery,
        calibration=ctx.calibration,
        afr_stoich=ctx.afr_stoich,
        rpm_bands=ctx.mode01_wot.rpm_bands,
        min_load=ctx.mode01_wot.load_floor_pct,
        revisions=ctx.registry,
    )
    record = _evidence(real.ledger, real.wot_lean)
    assert record.provenance.producer == DELIVERY_TOOL
    assert record.incident == DELIVERY_INCIDENT
    assert record.result.residual is not None
    assert record.result.residual.value == pytest.approx(direct.band_averaged_pct)
    assert record.values["eligible_rows"].value == direct.n == 44
    assert record.result.residual.value == pytest.approx(3.30, abs=0.01)
    assert record.labels["evidence_class"] == "engine_response"
    assert record.vehicle.calibration == "ZS44A-20260920"
    assert "sensor:FUEL_PRESSURE" not in record.labels["fault_model_channels"]


def test_the_in_sample_wot_lean_drive_opens_nothing(real: Real) -> None:
    """164007 chose B06's enrichment constants: its +3.30 % is in-sample."""
    record = _evidence(real.ledger, real.wot_lean)
    assert record.labels["session_informed_constants"] == "true"
    assert "sign" not in record.labels
    assert record.labels["sign_refused"] == INFORMED_REFUSAL
    assert "informed_constants" in record.quality.flags
    found = detect_discrepancy(real.ledger, real.wot_lean)
    assert found.status == "unqualified" and found.reason == "in_sample"
    assert real.wot_lean_step.investigation is None


def test_the_held_out_drives_show_no_qualified_discrepancy(real: Real) -> None:
    """Out of sample the WOT lean does not reproduce: all five within the floor."""
    for name, (expected, rows) in HELD_OUT.items():
        record = _evidence(real.ledger, real.held_out[name])
        assert record.labels["session_informed_constants"] == "false"
        assert record.result.residual is not None
        assert record.result.residual.value == pytest.approx(expected, abs=0.01), name
        assert record.values["eligible_rows"].value == rows
        assert record.labels["sign"] == "0", name
        assert detect_discrepancy(real.ledger, real.held_out[name]).status == "within_floor"
    # The two 2026-09-17 drives ran an earlier calibration.
    hybrid = _evidence(
        real.ledger, real.held_out["_capture-tuning-hybrid-research-2026-09-17T201000"]
    )
    assert hybrid.vehicle.calibration == "ZS44A-k106"
    assert real.ledger.query(kinds=("investigation",)) == []


def test_missing_command_and_uncovered_regime_are_different_explicit_results(
    real: Real,
) -> None:
    uncovered = _evidence(real.ledger, real.pressure[0])
    missing = _evidence(real.ledger, real.no_command[0])
    assert uncovered.result.reason == "uncovered"
    assert "not a zero error" in uncovered.result.detail
    assert missing.result.reason == "missing_input"
    assert "disables Mode 01" in missing.result.detail
    for evidence in (uncovered, missing):
        assert evidence.result.residual is None and not evidence.values
    for evidence_id in (real.pressure[0], real.no_command[0]):
        assert detect_discrepancy(real.ledger, evidence_id).status == "unevaluable"


def test_closed_loop_droop_is_minted_and_enrichment_pressure_is_uncovered(real: Real) -> None:
    high = _evidence(real.ledger, real.pressure[1])
    assert high.labels["residual"] == "fuel_pressure_droop"
    assert high.labels["region"] == "high_airflow"
    assert high.result.status == "evaluated" and high.labels["sign"] == "+"
    assert high.values["droop"].value == pytest.approx(2.0, abs=0.3)
    assert high.values["max_maf"].value < 120.0
    assert "closed-loop" in high.conditions.regime
    loss = _evidence(real.ledger, real.pressure[2])
    assert loss.labels["residual"] == "pressure_flow_loss"
    assert loss.result.reason == "uncovered"
    assert "B06-eligible enrichment" in loss.result.detail
    for evidence_id in real.no_command[1:]:
        assert _evidence(real.ledger, evidence_id).result.reason == "missing_input"


# ---------------------------------------------------------------------------
# Labelled synthetic drives


def _synthetic_drive(
    root: Path, name: str, start: datetime, *, lean_pct: float, pressure_drop_pct: float | None
) -> Path:
    """SYNTHETIC: idle, three cruise-then-WOT cycles, idle; Mode 01 at 4 Hz.

    At WOT the ECM commands phi 1.17 open loop and the measured lambda is
    ``lean_pct`` lean of it; pressure (when logged, 100 Hz) falls by
    ``pressure_drop_pct`` at WOT only.
    """
    rng = np.random.default_rng(7)
    segments: list[tuple[float, str]] = [(60.0, "idle")]
    for _ in range(3):
        segments += [(20.0, "cruise"), (10.0, "wot")]
    segments.append((40.0, "idle"))
    rows: list[dict[str, object]] = []
    t0 = start.timestamp()
    t = 0.0
    round_id = 0
    for duration, kind in segments:
        steps = int(duration * 4)
        for k in range(steps):
            stamp = t + k * 0.25
            frac = k / max(steps - 1, 1)
            if kind == "wot":
                rpm, maf, load, status, phi = 3000 + 2000 * frac, 90 + 50 * frac, 95.0, 4.0, 1.17
                lam = (1 / phi) * (1 + lean_pct / 100)
            else:
                rpm, maf, load, status, phi = (
                    (800.0, 5.0, 20.0, 2.0, 1.0)
                    if kind == "idle"
                    else (2000.0, 25.0, 40.0, 2.0, 1.0)
                )
                lam = 1.0 + float(rng.normal(0, 0.003))
            round_id += 1
            meta = json.dumps(
                {"acquired_at": t0 + stamp, "acquired_clock": "kernel", "round_id": round_id,
                 "ts_schema": 2}
            )  # fmt: skip
            for channel, value, unit in (
                ("RPM", rpm, "rpm"),
                ("MAF", maf, "g/s"),
                ("ENGINE_LOAD", load, "%"),
                ("FUEL_STATUS", status, "bitmask"),
                ("COMMANDED_EQUIV_RATIO", phi, "ratio"),
                ("O2_WR_LAMBDA_S1", lam, "ratio"),
            ):
                rows.append(
                    {"t": t0 + stamp, "source": "obd", "channel": channel, "value": value,
                     "unit": unit, "meta": meta}
                )  # fmt: skip
        if pressure_drop_pct is not None:
            for k in range(int(duration * 100)):
                level = 350.0 * (1 - pressure_drop_pct / 100) if kind == "wot" else 350.0
                rows.append(
                    {"t": t0 + t + k * 0.01, "source": "sensor", "channel": "FUEL_PRESSURE",
                     "value": level + float(rng.normal(0, 0.5)), "unit": "kPa", "meta": "{}"}
                )  # fmt: skip
        t += duration
    frame = pd.DataFrame(rows).sort_values("t", kind="stable")
    out = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(frame["t"], unit="s", utc=True),
            "source": frame["source"],
            "channel": frame["channel"],
            "value_number": frame["value"].astype(float),
            "value_text": pd.Series([None] * len(frame), dtype=object),
            "value_bool": pd.Series([None] * len(frame), dtype=object),
            "unit": frame["unit"],
            "payload": [b""] * len(frame),
            "metadata_json": frame["meta"],
        }
    )
    session = root / name
    session.mkdir(parents=True)
    out.to_parquet(session / "telemetry-00000.parquet", index=False)
    return session


@dataclass
class Synthetic:
    ledger: MemoryLedger
    drive_a: tuple[str, str, str]
    opened: InvestigationStep
    recurrence: str
    flow_loss: str
    retrieval: tuple[str, ...]
    drive_b: tuple[str, str, str]
    updated: InvestigationStep
    on_in_sample: str
    on_held_out: str
    recurred: str
    flow_held: str


@pytest.fixture(scope="module")
def synthetic(vehicle: Vehicle, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Synthetic]:
    root = tmp_path_factory.mktemp("synthetic")
    a = _synthetic_drive(
        root, "_synthetic-wot-lean-2026-09-01T120000", datetime(2026, 9, 1, 12, tzinfo=UTC),
        lean_pct=4.0, pressure_drop_pct=None,
    )  # fmt: skip
    b = _synthetic_drive(
        root, "_synthetic-wot-lean-2026-11-01T120000", datetime(2026, 11, 1, 12, tzinfo=UTC),
        lean_pct=4.0, pressure_drop_pct=0.2,
    )  # fmt: skip
    ledger = MemoryLedger(root / "memory.jsonl", vehicle=PROFILE)
    clock = Clock(datetime(2026, 9, 1, 13, tzinfo=UTC))
    # Observe and investigate, after drive A.
    drive_a = _mint_all(ledger, vehicle, a, clock)
    opened = investigate(
        ledger, vehicle.model, delivery_evidence=drive_a[0], other_evidence=drive_a[1:],
        now=clock(),
    )  # fmt: skip
    assert opened.investigation is not None
    # Predict, before any later drive.
    floor = {"floor_path": vehicle.data / "noise_floor.yaml",
             "floor_conditions": vehicle.floor_conditions}  # fmt: skip
    recurrence = predict_recurrence(ledger, opened.investigation, now=clock(), **floor)  # type: ignore[arg-type]
    flow_loss = predict_pressure_flow_loss(ledger, opened.investigation, now=clock(), **floor)  # type: ignore[arg-type]
    # Verify on later real drives: the in-sample one and a held-out one.
    clock.at(datetime(2026, 9, 24, tzinfo=UTC))
    in_sample = mint_delivery_error(
        ledger, WOT_LEAN, vehicle.context, vehicle.model, vehicle.model_sha,
        vin=None, program_id=None, now=clock(),
    )  # fmt: skip
    held = mint_delivery_error(
        ledger, FIXTURES / "_capture-tuning-2026-09-23T142926", vehicle.context, vehicle.model,
        vehicle.model_sha, vin=None, program_id=None, now=clock(),
    )  # fmt: skip
    on_in_sample = evaluate_outcome(ledger, recurrence, in_sample, now=clock())
    on_held_out = evaluate_outcome(ledger, recurrence, held, now=clock())
    # Drive B, with fuel pressure.
    clock.at(datetime(2026, 11, 1, 13, tzinfo=UTC))
    drive_b = _mint_all(ledger, vehicle, b, clock)
    found = retrieve(ledger, drive_b[0])
    updated = investigate(
        ledger, vehicle.model, delivery_evidence=drive_b[0], other_evidence=drive_b[1:],
        now=clock(),
    )  # fmt: skip
    recurred = evaluate_outcome(ledger, recurrence, drive_b[0], now=clock())
    flow_held = evaluate_outcome(ledger, flow_loss, drive_b[2], now=clock())
    yield Synthetic(
        ledger, drive_a, opened, recurrence, flow_loss, found.investigations, drive_b, updated,
        on_in_sample, on_held_out, recurred, flow_held,
    )  # fmt: skip


def test_synthetic_a_qualified_discrepancy_opens_an_investigation(synthetic: Synthetic) -> None:
    step = synthetic.opened
    assert step.discrepancy.qualified and step.discrepancy.direction == "lean"
    assert not step.retrieved
    assert set(step.consistent) == {
        "maf_transfer_error",
        "fuel_delivery_sag",
        "lambda_sensor_bias",
    }
    assert ("maf_transfer_error", "fuel_delivery_sag") in {(p.a, p.b) for p in step.unresolved}
    assert step.measurements[0].measurement.startswith(FUEL_PRESSURE_MEASUREMENT)
    assert step.measurements[0].actionable
    record = synthetic.ledger.get(step.investigation or "")
    assert isinstance(record, Investigation) and record.status == "open"
    status = {h.label.split(";")[0]: h.status for h in record.hypotheses}
    assert status["No fault"] == "refuted"
    assert status["MAF transfer error"] == status["Fuel-delivery sag"] == "open"
    # Drive A logged no pressure: unevaluable, named, not zero.
    loss = _evidence(synthetic.ledger, synthetic.drive_a[2])
    assert loss.result.reason == "missing_input" and loss.result.residual is None
    (report,) = step.structural.values()
    assert any(
        e.feature.residual == "pressure_flow_loss" and e.kind == "channel_absent"
        for e in report.excluded
    )


def test_synthetic_predictions_carry_the_condition_matched_floor(
    synthetic: Synthetic, vehicle: Vehicle
) -> None:
    for prediction_id in (synthetic.recurrence, synthetic.flow_loss):
        prediction = synthetic.ledger.get(prediction_id)
        assert isinstance(prediction, Prediction)
        assert prediction.noise_floor is not None
        assert prediction.noise_floor.quantity == "delivery_error_pct"
        assert prediction.conditions.regime == vehicle.floor_conditions
    flow = synthetic.ledger.get(synthetic.flow_loss)
    assert isinstance(flow, Prediction) and flow.magnitude is not None
    assert flow.magnitude.value == pytest.approx(4.0, abs=0.3)


def test_outcomes_need_the_next_independent_drive(synthetic: Synthetic) -> None:
    ledger = synthetic.ledger
    with pytest.raises(NotNextDriveError, match="the drive the prediction was made from"):
        evaluate_outcome(ledger, synthetic.recurrence, synthetic.drive_a[0])
    in_sample = ledger.get(synthetic.on_in_sample)
    assert isinstance(in_sample, Outcome)
    assert in_sample.disposition == "unresolvable"
    assert in_sample.result.reason == "refused"
    assert in_sample.result.detail == INFORMED_REFUSAL
    held = ledger.get(synthetic.on_held_out)
    assert isinstance(held, Outcome) and held.disposition == "no_effect"
    assert ledger.assess(synthetic.on_held_out).verdict == "contradicted"


def test_synthetic_loop_verifies_and_resolves_the_maf_sag_pair(synthetic: Synthetic) -> None:
    ledger = synthetic.ledger
    assert synthetic.retrieval == (synthetic.opened.investigation,)
    recurred = ledger.assess(synthetic.recurred)
    assert recurred.order == "prior" and recurred.verdict == "confirmed"
    loss = _evidence(ledger, synthetic.drive_b[2])
    assert loss.labels["sign"] == "0"
    assert abs(loss.result.residual.value) < 0.5  # type: ignore[union-attr]
    flow = ledger.assess(synthetic.flow_held)
    assert flow.order == "prior" and flow.verdict == "contradicted"
    step = synthetic.updated
    assert step.retrieved
    assert "fuel_delivery_sag" not in step.consistent
    assert ("maf_transfer_error", "fuel_delivery_sag") not in {(p.a, p.b) for p in step.unresolved}
    # MAF error and sensor bias still look alike: open, wideband named.
    assert ("maf_transfer_error", "lambda_sensor_bias") in {(p.a, p.b) for p in step.unresolved}
    record = ledger.get(step.investigation or "")
    assert isinstance(record, Investigation) and record.status == "open"
    sag = next(h for h in record.hypotheses if h.label.startswith("Fuel-delivery sag"))
    assert sag.status == "refuted" and synthetic.drive_b[2] in sag.refuting
    assert any("wideband" in m for m in record.next_measurements)
    # Per drive, on each drive's own channels.
    assert len(step.structural) == 2
    by_drive = {name: report for name, report in step.structural.items()}
    later = by_drive["_synthetic-wot-lean-2026-11-01T120000"]
    assert Feature("pressure_flow_loss", "enrichment") in later.usable
    earlier = synthetic.ledger.entry(synthetic.opened.investigation or "")
    assert not earlier.current


def test_ecu_computation_evidence_cannot_feed_the_delivery_loop(real: Real, tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.jsonl", vehicle=PROFILE)
    template = _evidence(real.ledger, real.wot_lean)
    stamp = template.valid_from
    twin = Evidence(
        vehicle=template.vehicle,
        provenance=Provenance(
            producer="target_phi_twin", producer_kind="tool", version="1", code_version="x"
        ),
        conditions=Conditions(regime="target phi", vehicle_state=None),
        valid_from=stamp,
        subject="twin:target",
        sources=(
            SourceRef(
                session="2026-11-02T120000",
                window_start=stamp,
                window_end=stamp,
                channels=("mem:ECM_TARGET_EQUIV_RATIO",),
                n_samples=1,
            ),
        ),
        input_hashes={"x": "0" * 64},
        policy={"x": 1},
        values={"miss": Quantity(value=1.0, unit="1")},
        labels={
            "evidence_class": "ecu_computation",
            "residual": "delivery_error",
            "region": "enrichment",
            "sign": "+",
            "session_informed_constants": "false",
        },
        quality=Quality(n_observations=1, timestamp_basis=None),
        result=Result(status="evaluated", residual=Quantity(value=1.0, unit="percent")),
    )
    twin_id = ledger.append(twin).id
    with pytest.raises(NotEngineResponseError, match="ECU-computation"):
        detect_discrepancy(ledger, twin_id)


def test_the_card_renders_the_loop_without_calling_unevaluable_normal(
    synthetic: Synthetic,
) -> None:
    text = render_text(investigation_card(synthetic.ledger, synthetic.updated.investigation or ""))
    assert "UNEVALUABLE (missing_input)" in text
    assert "maf_transfer_error and lambda_sensor_bias are not yet separated" in text
    earlier = investigation_card(synthetic.ledger, synthetic.opened.investigation or "")
    assert synthetic.drive_b[2] not in render_text(earlier)


def test_re_minting_the_same_drive_is_the_same_record(vehicle: Vehicle, tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.jsonl", vehicle=PROFILE)
    args = (ledger, NO_COMMAND, vehicle.context, vehicle.model, vehicle.model_sha)
    first = mint_delivery_error(*args, vin=None, program_id=None)
    again = mint_delivery_error(*args, vin=None, program_id=None)
    assert first == again
    assert len(ledger.entries()) == 1
