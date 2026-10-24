"""E03.4: track the hypothesis set, rank the next measurement, propose it only
when the drive enters its region, and never displace an operator capture.

Drives are E03.1's **labelled synthetic** sessions (generated here with
``tests/test_delivery_investigation.py``'s generator, named ``_synthetic-…``):
``A`` is 4 % lean at enrichment without fuel pressure; ``B`` is the same with
fuel pressure logged; ``C``, later still, is on the command (0 %). The fault
model and measurement plan are the vehicle's committed artifacts; one test
uses a labelled variant of the model whose signatures are determinate, to
exercise information gain where the real model honestly cannot.
"""

from __future__ import annotations

import random
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pandas as pd
import pytest
import yaml

from src.analysis.delivery_investigation import investigate, select_measurement, unresolved_pairs
from src.analysis.fault_model import (
    FAULT_MODEL_FILE,
    FaultModel,
    Feature,
    Likelihood,
    load_fault_model,
)
from src.analysis.hypothesis_tracking import (
    rank_measurements,
    track_hypotheses,
    update_investigation,
)
from src.analysis.measurement_plan import (
    MEASUREMENT_PLAN_FILE,
    CaptureState,
    MeasurementPlan,
    MeasurementPlanError,
    Proposal,
    load_measurement_plan,
    profile_shortfall,
    replay_proposals,
)
from src.analysis.tuning_context import TuningContext
from src.collection.capture_profiles import CaptureProfileCatalog
from src.config import CanaryConfig
from src.evidence import Evidence, Investigation, MemoryLedger
from src.vehicle.profiles import profile_dir, resolve_profile_for_tooling, scope_config
from src.yaml_load import load_yaml
from tests.test_delivery_investigation import PROFILE, Clock, Vehicle, _mint_all, _synthetic_drive

PRESSURE = "Log fuel pressure through a full-throttle pull (supercharger-build NA2)."
WIDEBAND_PREFIX = "Fit an independent wideband lambda sensor"
BUDGET = 8.0


@dataclass
class World:
    vehicle: Vehicle
    plan: MeasurementPlan
    catalog: CaptureProfileCatalog
    ledger: MemoryLedger
    drive_a: tuple[str, str, str]
    drive_b: tuple[str, str, str]
    drive_c: tuple[str, str, str]
    session_a: Path
    session_b: Path
    opened: str


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    base = CanaryConfig.from_yaml(Path("canary.dev.example.yaml"))
    config = scope_config(base, resolve_profile_for_tooling(base, PROFILE))
    data = profile_dir(config)
    model, sha = load_fault_model(data / FAULT_MODEL_FILE)
    context = TuningContext.from_config(config)
    vehicle = Vehicle(context, model, sha, data)
    plan, _sha = load_measurement_plan(
        data / MEASUREMENT_PLAN_FILE, model, mode01_wot=context.mode01_wot
    )
    catalog = CaptureProfileCatalog.model_validate(
        load_yaml((data / "capture_profiles.yaml").read_text())
    )
    root = tmp_path_factory.mktemp("tracking")
    a = _synthetic_drive(
        root, "_synthetic-wot-lean-2026-09-01T120000", datetime(2026, 9, 1, 12, tzinfo=UTC),
        lean_pct=4.0, pressure_drop_pct=None,
    )  # fmt: skip
    b = _synthetic_drive(
        root, "_synthetic-wot-lean-2026-11-01T120000", datetime(2026, 11, 1, 12, tzinfo=UTC),
        lean_pct=4.0, pressure_drop_pct=0.2,
    )  # fmt: skip
    c = _synthetic_drive(
        root, "_synthetic-wot-on-command-2026-11-08T120000",
        datetime(2026, 11, 8, 12, tzinfo=UTC), lean_pct=0.0, pressure_drop_pct=None,
    )  # fmt: skip
    ledger = MemoryLedger(root / "memory.jsonl", vehicle=PROFILE)
    clock = Clock(datetime(2026, 9, 1, 13, tzinfo=UTC))
    drive_a = _mint_all(ledger, vehicle, a, clock)
    opened = investigate(
        ledger, model, delivery_evidence=drive_a[0], other_evidence=drive_a[1:], now=clock()
    )
    assert opened.investigation is not None
    clock.at(datetime(2026, 11, 1, 13, tzinfo=UTC))
    drive_b = _mint_all(ledger, vehicle, b, clock)
    clock.at(datetime(2026, 11, 8, 13, tzinfo=UTC))
    drive_c = _mint_all(ledger, vehicle, c, clock)
    return World(
        vehicle, plan, catalog, ledger, drive_a, drive_b, drive_c, a, b, opened.investigation
    )


def _rows(session: Path) -> list[tuple[float, str, float]]:
    frame = pd.read_parquet(session)
    t = frame["timestamp"].astype("int64").to_numpy() / 1e9
    specs = (frame["source"] + ":" + frame["channel"]).to_numpy()
    values = frame["value_number"].to_numpy()
    return sorted(zip(t.tolist(), specs.tolist(), values.tolist(), strict=True))


_CLOCK = [datetime(2027, 1, 1, tzinfo=UTC)]


def _later() -> datetime:
    """Record times after everything the fixture wrote, always increasing."""
    _CLOCK[0] += timedelta(minutes=1)
    return _CLOCK[0]


def _fork(world: World, tmp_path: Path) -> MemoryLedger:
    """A private copy of the shared ledger, for a test that appends to it."""
    path = tmp_path / "memory.jsonl"
    shutil.copyfile(world.ledger.path, path)
    return MemoryLedger(path, vehicle=PROFILE)


# -- tracking --------------------------------------------------------------------


def test_drive_a_ranks_open_hypotheses_and_names_fuel_pressure_first(world: World) -> None:
    track = track_hypotheses(world.vehicle.model, world.ledger, world.drive_a)
    assert set(track.open) == {"maf_transfer_error", "fuel_delivery_sag", "lambda_sensor_bias"}
    assert track.ranking[:3] == ("maf_transfer_error", "fuel_delivery_sag", "lambda_sensor_bias")
    sag = track.of("fuel_delivery_sag")
    assert world.drive_a[0] in sag.supporting and sag.supporting_drives == 1
    assert world.drive_a[0] in track.of("no_fault").refuting

    pairs = unresolved_pairs(
        world.vehicle.model, [], track.signed(), track.open
    )  # no drive report: every residual counts as unobserved
    ranking = rank_measurements(
        world.vehicle.model, world.plan, world.ledger, track.open, select_measurement(pairs)
    )
    # No hypothesis carries an evidence-backed likelihood: structural order,
    # and each missing likelihood is named instead of a number.
    assert ranking.method == "structural"
    assert all(m.information_bits is None for m in ranking.measurements)
    assert {line.split(":")[0] for line in ranking.missing_likelihoods} == set(track.open)


def test_update_records_the_ranking_and_leaves_indistinguishable_pairs_open(
    world: World, tmp_path: Path
) -> None:
    ledger = _fork(world, tmp_path)
    step = update_investigation(
        ledger, world.vehicle.model, world.plan, world.opened, world.drive_a, now=_later()
    )
    record = ledger.get(step.investigation)
    assert isinstance(record, Investigation)
    assert record.next_measurements[0] == PRESSURE  # the known separating measurement
    assert record.status == "open"
    labels = [h.label for h in record.hypotheses]
    assert labels[:3] == [
        world.vehicle.model.hypothesis(n).label
        for n in ("maf_transfer_error", "fuel_delivery_sag", "lambda_sensor_bias")
    ]
    unknowns = " ".join(record.unknowns)
    assert "maf_transfer_error and lambda_sensor_bias are not yet separated" in unknowns
    assert "ranked structurally" in unknowns
    wideband = next(
        m for m in step.ranking.measurements if m.measurement.startswith(WIDEBAND_PREFIX)
    )
    assert (
        not wideband.actionable
        and ("maf_transfer_error", "lambda_sensor_bias") in wideband.separates
    )


def test_repeated_evidence_is_counted_once(world: World, tmp_path: Path) -> None:
    model = world.vehicle.model
    ledger = _fork(world, tmp_path)
    first = track_hypotheses(model, ledger, world.drive_a)
    again = track_hypotheses(model, ledger, [*world.drive_a, *world.drive_a])
    assert again.counted == first.counted and again.ranking == first.ranking
    assert again.of("fuel_delivery_sag") == first.of("fuel_delivery_sag")

    # A recompute supersedes: the old id counts as the record that replaced it.
    old = ledger.get(world.drive_a[0])
    assert isinstance(old, Evidence)
    newer = ledger.append(
        old.model_copy(
            update={"id": "", "recorded_at": None, "labels": {**old.labels, "recomputed": "yes"}}
        ),
        now=_later(),
    ).id
    recomputed = track_hypotheses(model, ledger, [*world.drive_a, newer])
    assert recomputed.skipped[world.drive_a[0]] == f"superseded by {newer}"
    assert len(recomputed.counted) == len(first.counted)
    assert recomputed.of("fuel_delivery_sag").supporting_drives == 1

    # A second record of the same feature on the same drive is one occurrence.
    twin = ledger.append(
        old.model_copy(update={"id": "", "recorded_at": None, "subject": old.subject + ":again"}),
        now=_later(),
    ).id
    doubled = track_hypotheses(model, ledger, [newer, twin, *world.drive_a[1:]])
    assert doubled.of("fuel_delivery_sag").supporting_drives == 1
    # Whichever of the two sorts first stands for the occurrence; the other is skipped.
    assert any("same occurrence" in doubled.skipped.get(rid, "") for rid in (newer, twin))


def test_a_record_pooling_the_drive_with_another_session_is_the_same_drive(
    world: World, tmp_path: Path
) -> None:
    """Occurrences group by session overlap, not by the exact session set."""
    ledger = _fork(world, tmp_path)
    old = ledger.get(world.drive_a[0])
    assert isinstance(old, Evidence)
    first = old.sources[0]
    extra = first.model_copy(update={"session": first.session + "-extra"})
    pooled = ledger.append(
        old.model_copy(
            update={
                "id": "",
                "recorded_at": None,
                "subject": old.subject + ":pooled",
                "sources": (*old.sources, extra),
            }
        ),
        now=_later(),
    ).id
    track = track_hypotheses(world.vehicle.model, ledger, [*world.drive_a, pooled])
    assert track.of("fuel_delivery_sag").supporting_drives == 1
    assert any(
        "same occurrence" in track.skipped.get(rid, "") for rid in (pooled, world.drive_a[0])
    )


def test_updating_with_counted_evidence_returns_the_same_record(
    world: World, tmp_path: Path
) -> None:
    ledger = _fork(world, tmp_path)
    first = update_investigation(
        ledger, world.vehicle.model, world.plan, world.opened, world.drive_b, now=_later()
    )
    again = update_investigation(
        ledger, world.vehicle.model, world.plan, first.investigation,
        [*world.drive_b, *world.drive_b, *world.drive_a], now=_later(),
    )  # fmt: skip
    assert first.changed and not again.changed
    assert again.investigation == first.investigation
    # Pressure held with drive B: sag is refuted, by drive B's flow-loss record.
    assert "fuel_delivery_sag" not in first.track.open
    assert world.drive_b[2] in first.track.of("fuel_delivery_sag").refuting


def test_conflicting_evidence_changes_the_ranking_reproducibly(world: World) -> None:
    model = world.vehicle.model
    before = track_hypotheses(model, world.ledger, [*world.drive_a, *world.drive_b])
    ids = [*world.drive_a, *world.drive_b, *world.drive_c]
    after = track_hypotheses(model, world.ledger, ids)
    assert after.ranking != before.ranking
    # Drive C (on the command) refutes every hypothesis that needs a lean.
    assert world.drive_c[0] in after.of("maf_transfer_error").refuting
    assert after.of("no_fault").supporting_drives >= 1
    for seed in range(5):
        shuffled = ids[:]
        random.Random(seed).shuffle(shuffled)
        assert track_hypotheses(model, world.ledger, shuffled).ranking == after.ranking
    assert after.open == ()


def test_within_one_drive_a_sign_conflict_is_excluded_and_named(
    world: World, tmp_path: Path
) -> None:
    ledger = _fork(world, tmp_path)
    old = ledger.get(world.drive_c[0])
    assert isinstance(old, Evidence)
    contrary = ledger.append(
        old.model_copy(
            update={
                "id": "",
                "recorded_at": None,
                "subject": old.subject + ":contrary",
                "labels": {**old.labels, "sign": "+"},
            }
        ),
        now=_later(),
    ).id
    track = track_hypotheses(world.vehicle.model, ledger, [world.drive_c[0], contrary])
    (conflict,) = track.conflicts
    assert conflict.feature == Feature("delivery_error", "enrichment")
    assert set(conflict.evidence) == {world.drive_c[0], contrary}
    assert all(not h.refuting for h in track.hypotheses)


def _determinate(model: FaultModel, evidence: str, weights: dict[str, float]) -> FaultModel:
    """LABELLED VARIANT: the real model with pressure and wideband outcomes made
    determinate and likelihoods citing a real ledger record."""
    signatures = {
        "maf_transfer_error": {"pressure_flow_loss": {"enrichment": "0"}},
        "lambda_sensor_bias": {
            "pressure_flow_loss": {"enrichment": "0"},
            "independent_lambda": {"enrichment": "+"},
        },
    }
    hypotheses = []
    for h in model.hypotheses:
        signature = {k: dict(v) for k, v in h.signature.items()}
        for residual, regions in signatures.get(h.name, {}).items():
            signature[residual].update(cast(Any, regions))
        likelihood = (
            Likelihood(value=weights[h.name], evidence=(evidence,), method="labelled test weight")
            if h.name in weights
            else None
        )
        hypotheses.append(h.model_copy(update={"signature": signature, "likelihood": likelihood}))
    return FaultModel.model_validate(
        {**model.model_dump(by_alias=True), "hypotheses": [x.model_dump() for x in hypotheses]}
    )


def test_information_gain_per_cost_ranks_only_with_evidence_backed_likelihoods(
    world: World,
) -> None:
    weights = {"maf_transfer_error": 0.5, "fuel_delivery_sag": 0.3, "lambda_sensor_bias": 0.2}
    model = _determinate(world.vehicle.model, world.drive_a[0], weights)
    track = track_hypotheses(model, world.ledger, world.drive_a)
    every = select_measurement(unresolved_pairs(model, [], track.signed(), track.open))
    # Qualification work has no outcome model (trims are 'any' under MAF error),
    # so ranking every choice stays structural; rank the two instruments.
    assert rank_measurements(model, world.plan, world.ledger, track.open, every).method == (
        "structural"
    )
    choices = [
        c for c in every if c.measurement == PRESSURE or c.measurement.startswith(WIDEBAND_PREFIX)
    ]
    ranking = rank_measurements(model, world.plan, world.ledger, track.open, choices)
    assert ranking.method == "information_gain_per_cost"
    pressure = next(m for m in ranking.measurements if m.measurement == PRESSURE)
    wideband = next(m for m in ranking.measurements if m.measurement.startswith(WIDEBAND_PREFIX))
    assert pressure.information_bits == pytest.approx(0.8813, abs=1e-4)  # H(0.7, 0.3)
    assert wideband.information_bits == pytest.approx(0.7219, abs=1e-4)  # H(0.8, 0.2)
    assert ranking.order.index(PRESSURE) < ranking.order.index(wideband.measurement)
    # The cost is the denominator: make pressure dear and the wideband wins.
    dear = world.plan.model_copy(
        update={
            "measurements": tuple(
                m.model_copy(update={"cost": 10_000.0})
                if m.residual in ("pressure_flow_loss", "delivery_error")
                else m
                for m in world.plan.measurements
            )
        }
    )
    flipped = rank_measurements(model, dear, world.ledger, track.open, choices)
    assert flipped.order.index(wideband.measurement) < flipped.order.index(PRESSURE)
    # A likelihood citing evidence that is not on this ledger is not one.
    bogus = _determinate(world.vehicle.model, "ev-000000000000000000000000", weights)
    refused = rank_measurements(bogus, world.plan, world.ledger, track.open, choices)
    assert refused.method == "structural" and refused.missing_likelihoods


# -- proposals (replayed drives) ----------------------------------------------------


def _ranked(world: World) -> list[Any]:
    """Drive A's measurement ranking (the order proposals are made in)."""
    track = track_hypotheses(world.vehicle.model, world.ledger, world.drive_a)
    choices = select_measurement(
        unresolved_pairs(world.vehicle.model, [], track.signed(), track.open)
    )
    ranking = rank_measurements(world.vehicle.model, world.plan, world.ledger, track.open, choices)
    return list(ranking.measurements)


def _replay(world: World, session: Path, capture: CaptureState, **kw: Any) -> list[Proposal]:
    ticks = replay_proposals(
        world.plan, world.vehicle.model, world.catalog, _ranked(world), _rows(session),
        capture=capture, default_budget=BUDGET, **kw,
    )  # fmt: skip
    return [p for _t, proposals in ticks for p in proposals]


NO_CAPTURE = CaptureState(active=False, profile="default")


def test_replay_proposes_the_tuning_capture_only_inside_enrichment(world: World) -> None:
    proposals = [
        p for p in _replay(world, world.session_b, NO_CAPTURE) if p.measurement == PRESSURE
    ]
    opened = [p for p in proposals if p.kind == "open_capture"]
    assert opened and {p.profile for p in opened} == {"tuning"}
    assert {p.region for p in opened} == {"enrichment"}
    assert {p.reason for p in proposals if p.kind == "defer"} == {"region_not_entered"}
    # Physical actions go to review after the drive; nothing is a driver prompt.
    reviews = [p for p in _replay(world, world.session_b, NO_CAPTURE) if p.kind == "review"]
    assert reviews and {p.delivery for p in reviews} == {"after_drive"}
    assert any(p.measurement.startswith(WIDEBAND_PREFIX) for p in reviews)


def test_replay_cannot_displace_an_operator_capture(world: World) -> None:
    operator = CaptureState(active=True, profile="fuel-pulsation", source="control")
    proposals = _replay(world, world.session_b, operator)
    assert not [p for p in proposals if p.kind == "open_capture"]
    deferred = {p.reason for p in proposals if p.measurement == PRESSURE and p.kind == "defer"}
    assert "operator_capture_active" in deferred
    # The operator's own tuning capture already collects it.
    theirs = CaptureState(active=True, profile="tuning", source="mcp")
    collected = [p for p in _replay(world, world.session_b, theirs) if p.measurement == PRESSURE]
    assert "operator_capture_collects_it" in {p.reason for p in collected}
    with pytest.raises(MeasurementPlanError, match="never displaces"):
        Proposal(PRESSURE, "pressure_flow_loss", "enrichment", "open_capture", "brain",
                 "region_entered", profile="tuning", during_capture="fuel-pulsation")  # fmt: skip


def test_replay_without_the_sensor_asks_for_it_after_the_drive(world: World) -> None:
    proposals = [
        p for p in _replay(world, world.session_a, NO_CAPTURE) if p.measurement == PRESSURE
    ]
    assert proposals and {(p.kind, p.reason, p.delivery) for p in proposals} == {
        ("review", "instrument_absent", "after_drive")
    }


def test_no_proposal_unless_the_engine_runs_and_canary_transmits(world: World) -> None:
    proposals = _replay(world, world.session_b, NO_CAPTURE, vehicle_state="passive")
    assert not [p for p in proposals if p.kind in ("open_capture", "collecting")]
    assert "vehicle_state:passive" in {p.reason for p in proposals}


def test_missing_or_stale_region_inputs_defer_with_a_named_reason(world: World) -> None:
    def reasons(rows: list[tuple[float, str, float]]) -> set[str]:
        ticks = replay_proposals(
            world.plan, world.vehicle.model, world.catalog, _ranked(world), rows,
            capture=NO_CAPTURE, default_budget=BUDGET,
        )  # fmt: skip
        return {p.reason for _t, ps in ticks for p in ps if p.measurement == PRESSURE}

    rows = _rows(world.session_b)
    # Without RPM the region is unknown, never guessed.
    assert reasons([r for r in rows if r[1] != "obd:RPM"]) == {"region_missing_input"}
    # RPM that stopped arriving after the first minute is stale, not current.
    start = rows[0][0]
    cut = [r for r in rows if r[1] != "obd:RPM" or r[0] < start + 60.0]
    found = reasons(cut)
    assert "region_stale_input" in found and "region_entered" not in found


# -- the plan ------------------------------------------------------------------------


def test_the_plan_refuses_thresholds_that_differ_from_their_sources(
    world: World, tmp_path: Path
) -> None:
    data = load_yaml((world.vehicle.data / MEASUREMENT_PLAN_FILE).read_text())
    data["regions"]["enrichment"]["all"][0]["value"] = 80.0
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(MeasurementPlanError, match="enrichment"):
        load_measurement_plan(
            path, world.vehicle.model, mode01_wot=world.vehicle.context.mode01_wot
        )
    data = load_yaml((world.vehicle.data / MEASUREMENT_PLAN_FILE).read_text())
    data["measurements"] = data["measurements"][:-1]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(MeasurementPlanError, match="independent_lambda"):
        load_measurement_plan(path, world.vehicle.model)
    data = load_yaml((world.vehicle.data / MEASUREMENT_PLAN_FILE).read_text())
    data["fault_model_version"] = 1
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(MeasurementPlanError, match="v1"):
        load_measurement_plan(path, world.vehicle.model)


def test_profile_capacity_and_complete_tuples(world: World) -> None:
    delivery = world.vehicle.model.residual("delivery_error").channel_sets
    tuning = world.catalog.by_name("tuning")
    assert tuning is not None
    assert profile_shortfall(tuning, delivery, default_budget=BUDGET) is None
    starved = tuning.model_copy(update={"bus_budget_override": 40.0})
    assert (profile_shortfall(starved, delivery, default_budget=BUDGET) or "").startswith(
        "over_capacity"
    )
    undeclared = tuning.model_copy(update={"expected_total_demand_hz": None})
    assert (profile_shortfall(undeclared, delivery, default_budget=BUDGET) or "").startswith(
        "capacity_undeclared"
    )
    partial = tuning.model_copy(
        update={"mode01": {k: v for k, v in tuning.mode01.items() if k != "COMMANDED_EQUIV_RATIO"}}
    )
    assert (profile_shortfall(partial, delivery, default_budget=BUDGET) or "").startswith(
        "incomplete_tuple"
    )
    assert profile_shortfall(None, delivery, default_budget=BUDGET).startswith("profile_missing")  # type: ignore[union-attr]
