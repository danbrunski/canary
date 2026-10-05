"""E01.1: investigation-memory record contracts and the per-vehicle ledger."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.evidence import (
    Conditions,
    CorruptRecordError,
    CrossVehicleReferenceError,
    Evidence,
    Hypothesis,
    Intervention,
    Investigation,
    MemoryLedger,
    MemoryReferenceError,
    NoiseFloorRef,
    Outcome,
    Prediction,
    Provenance,
    Quality,
    Quantity,
    Result,
    Retraction,
    SourceRef,
    UnknownRecordError,
    UnscopedMemoryError,
    VehicleIdentity,
    memory_ledger_for,
    record_from_dict,
    record_to_dict,
)
from tests.configs import isolated_config, scoped_test_config

T0 = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
HASH = "a" * 64


def _vehicle(profile: str = "truck") -> VehicleIdentity:
    return VehicleIdentity(profile=profile, vin=None, program_id=None, calibration="ZS44A")


def _conditions(regime: str = "WOT: throttle>=85, 3000-5000 rpm") -> Conditions:
    return Conditions(regime=regime, vehicle_state="engine_running")


def _tool() -> Provenance:
    return Provenance(
        producer="compare_channels", producer_kind="tool", version="1", code_version="c"
    )


def _operator() -> Provenance:
    return Provenance(producer="operator", producer_kind="operator", version="n/a")


def _evidence(**overrides: Any) -> Evidence:
    fields: dict[str, Any] = {
        "vehicle": _vehicle(),
        "provenance": _tool(),
        "conditions": _conditions(),
        "valid_from": T0,
        "valid_to": T0 + timedelta(minutes=5),
        "subject": "compare_channels:2026-09-20T180000:obd:RPM:can:ECM_RPM:WOT",
        "sources": (
            SourceRef(
                session="2026-09-20T180000",
                window_start=T0,
                window_end=T0 + timedelta(minutes=5),
                channels=("obd:RPM", "can:ECM_RPM"),
                n_samples=300,
            ),
        ),
        "input_hashes": {"pairs": HASH},
        "policy": {"pairing": {"max_separation_s": 0.05}},
        "values": {"difference_bias": Quantity(value=0.4, unit="rpm")},
        "quality": Quality(n_observations=300, timestamp_basis="acquired"),
        "result": Result(status="evaluated", residual=Quantity(value=0.4, unit="rpm")),
    }
    fields.update(overrides)
    return Evidence(**fields)


def _ledger(tmp_path: Path, vehicle: str = "truck") -> MemoryLedger:
    return MemoryLedger(tmp_path / "memory.jsonl", vehicle=vehicle)


# -- schema refusals -----------------------------------------------------------


@pytest.mark.parametrize("missing", ["conditions", "provenance", "vehicle", "valid_from"])
def test_a_record_without_conditions_provenance_identity_or_valid_time_is_refused(
    missing: str,
) -> None:
    payload = record_to_dict(_evidence())
    payload.pop(missing)
    with pytest.raises(ValidationError):
        record_from_dict(payload)


def test_unknown_identity_must_be_stated_not_omitted() -> None:
    with pytest.raises(ValidationError):
        VehicleIdentity(profile="truck", vin=None, calibration=None)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Conditions(regime="  ", vehicle_state=None)


def test_evidence_must_carry_sources_inputs_policy_and_a_tool() -> None:
    with pytest.raises(ValidationError):
        _evidence(sources=())
    with pytest.raises(ValidationError):
        _evidence(input_hashes={})
    with pytest.raises(ValidationError):
        _evidence(input_hashes={"pairs": "not-a-hash"})
    with pytest.raises(ValidationError):
        _evidence(policy={})
    with pytest.raises(ValidationError, match="minted by tools"):
        _evidence(provenance=_operator())
    with pytest.raises(ValidationError, match="code_version"):
        Provenance(producer="t", producer_kind="tool", version="1")


def test_an_unevaluable_result_cannot_carry_a_qualified_residual() -> None:
    with pytest.raises(ValidationError, match="cannot carry a qualified residual"):
        Result(
            status="unevaluable",
            reason="missing_input",
            detail="commanded AFR absent: tuning-did disables Mode 01",
            residual=Quantity(value=0.0, unit="%"),
        )
    with pytest.raises(ValidationError, match="names its reason"):
        Result(status="unevaluable", detail="x")
    with pytest.raises(ValidationError):
        Result(status="evaluated", reason="stale_input")
    ok = Result(status="unevaluable", reason="sentinel", detail="fuel cut: target word zero")
    assert ok.residual is None


def test_an_unevaluable_outcome_is_unresolvable_and_a_change_has_a_residual() -> None:
    common: dict[str, Any] = {
        "vehicle": _vehicle(),
        "provenance": _operator(),
        "conditions": _conditions(),
        "valid_from": T0,
        "prediction": "pred-x",
        "intervention": None,
        "evidence": ("ev-x",),
    }
    with pytest.raises(ValidationError, match="unresolvable"):
        Outcome(
            **common,
            disposition="no_effect",
            result=Result(status="unevaluable", reason="uncovered", detail="no WOT cells"),
        )
    with pytest.raises(ValidationError, match="residual"):
        Outcome(**common, disposition="measured_change", result=Result(status="evaluated"))


def test_times_must_be_aware_and_ordered() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _evidence(valid_from=datetime(2026, 9, 20, 18, 0))
    with pytest.raises(ValidationError, match="valid_to precedes"):
        _evidence(valid_to=T0 - timedelta(seconds=1))


def test_quantities_have_units_and_finite_values() -> None:
    with pytest.raises(ValidationError):
        Quantity(value=1.0, unit="")
    with pytest.raises(ValidationError, match="finite"):
        Quantity(value=float("nan"), unit="%")


def test_prediction_states_magnitude_and_floor_in_one_unit() -> None:
    with pytest.raises(ValidationError, match="like with like"):
        Prediction(
            vehicle=_vehicle(),
            provenance=_operator(),
            conditions=_conditions(),
            valid_from=T0,
            investigation=None,
            intervention=None,
            quantity="delivery_error_pct",
            direction="decrease",
            magnitude=Quantity(value=3.0, unit="%"),
            noise_floor=NoiseFloorRef(
                quantity="delivery_error_pct",
                sd=Quantity(value=1.25, unit="percent"),
                conditions=_conditions().regime,
                sigma_threshold=2.0,
                source="noise_floor.yaml",
                source_sha256=HASH,
            ),
        )


def test_an_intervention_points_at_the_existing_record() -> None:
    with pytest.raises(ValidationError, match="calibration"):
        Intervention(
            vehicle=_vehicle(),
            provenance=_operator(),
            conditions=_conditions("all conditions"),
            valid_from=T0,
            intervention_kind="hardware",
            title="flash",
            source="tune_revision",
            source_ref="ZS44A-20261004",
            source_digest=HASH,
            affects=("obd:SHORT_FUEL_TRIM_1",),
            effective_precision="instant",
        )
    with pytest.raises(ValidationError, match="source_ref"):
        Intervention(
            vehicle=_vehicle(),
            provenance=_operator(),
            conditions=_conditions("all conditions"),
            valid_from=T0,
            intervention_kind="hardware",
            title="pump",
            source="vehicle_ledger",
            source_ref=None,
            affects=("obd:SHORT_FUEL_TRIM_1",),
            effective_precision="instant",
        )


# -- the ledger ------------------------------------------------------------------


def test_the_ledger_mints_id_and_record_time_and_refuses_producer_supplied_ones(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    stored = ledger.append(_evidence())
    assert stored.id.startswith("ev-")
    assert stored.recorded_at is not None
    with pytest.raises(ValueError, match="minted by the ledger"):
        ledger.append(_evidence(id="ev-invented"))
    with pytest.raises(ValueError, match="minted by the ledger"):
        ledger.append(_evidence(recorded_at=T0))


def test_identical_content_is_one_record(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    first = ledger.append(_evidence())
    again = ledger.append(_evidence())
    assert again.id == first.id
    assert len(ledger.entries()) == 1


def test_invented_and_cross_vehicle_references_are_refused(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    with pytest.raises(UnknownRecordError):
        ledger.resolve_evidence("ev-000000000000000000000000")
    with pytest.raises(CrossVehicleReferenceError):
        ledger.append(_evidence(vehicle=_vehicle("other")))

    # A file shared by two vehicles (an unscoped laptop path): an id that
    # exists but belongs to the other vehicle is named as such.
    other = _ledger(tmp_path, vehicle="other")
    foreign = other.append(_evidence(vehicle=_vehicle("other")))
    with pytest.raises(CrossVehicleReferenceError, match="belongs to vehicle 'other'"):
        ledger.resolve_evidence(foreign.id)
    investigation = Investigation(
        vehicle=_vehicle(),
        provenance=_operator(),
        conditions=_conditions(),
        valid_from=T0,
        title="lean at WOT",
        question="why lean",
        hypotheses=(Hypothesis(label="pump droop", supporting=(foreign.id,)),),
    )
    with pytest.raises(CrossVehicleReferenceError):
        ledger.append(investigation)


def test_a_reference_must_have_the_kind_it_names(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    evidence = ledger.append(_evidence())
    outcome = Outcome(
        vehicle=_vehicle(),
        provenance=_operator(),
        conditions=_conditions(),
        valid_from=T0,
        prediction=evidence.id,  # an evidence id where a prediction belongs
        intervention=None,
        disposition="no_effect",
        evidence=(evidence.id,),
        result=Result(status="evaluated"),
    )
    with pytest.raises(MemoryReferenceError, match="not prediction"):
        ledger.append(outcome)


def test_recompute_supersedes_and_keeps_the_old_record(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    old = ledger.append(_evidence(), now=T0 + timedelta(hours=1))
    new = ledger.append(
        _evidence(
            values={"difference_bias": Quantity(value=0.6, unit="rpm")},
            result=Result(status="evaluated", residual=Quantity(value=0.6, unit="rpm")),
            input_hashes={"pairs": "b" * 64},
        ),
        now=T0 + timedelta(hours=2),
    )
    assert new.supersedes == (old.id,)
    assert new.supersession_reason
    old_entry = ledger.entry(old.id)
    assert old_entry.record == old  # unchanged, still on disk
    assert old_entry.superseded_at == T0 + timedelta(hours=2)
    assert old_entry.superseded_by == (new.id,)
    assert ledger.entry(new.id).current
    assert [e.record.id for e in ledger.history(new.id)] == [old.id, new.id]


def test_a_retraction_withdraws_without_deleting(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    finding = ledger.append(_evidence(), now=T0)
    retraction = ledger.append(
        Retraction(
            vehicle=_vehicle(),
            provenance=_operator(),
            valid_from=T0 + timedelta(days=1),
            retracts=(finding.id,),
            reason="byte was the low byte of a 16-bit counter; means matched by accident",
        ),
        now=T0 + timedelta(days=1),
    )
    entry = ledger.entry(finding.id)
    assert entry.retracted_by == retraction.id
    assert entry.superseded_at == T0 + timedelta(days=1)
    assert entry.record == finding


def test_record_time_never_moves_backwards(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.append(_evidence(), now=T0 + timedelta(hours=1))
    with pytest.raises(ValueError, match="backwards"):
        ledger.append(_evidence(subject="other"), now=T0)


def test_replaying_the_log_preserves_both_times_and_supersession_history(
    tmp_path: Path,
) -> None:
    """Restart / replay: a fresh ledger on the same file reconstructs it all."""
    ledger = _ledger(tmp_path)
    first = ledger.append(_evidence(), now=T0 + timedelta(hours=1))
    second = ledger.append(_evidence(input_hashes={"pairs": "c" * 64}), now=T0 + timedelta(hours=2))
    investigation = ledger.append(
        Investigation(
            vehicle=_vehicle(),
            provenance=_operator(),
            conditions=_conditions(),
            valid_from=T0,
            valid_to=T0 + timedelta(days=3),
            title="WOT lean",
            question="is the pump drooping",
            observations=(second.id,),
        ),
        now=T0 + timedelta(hours=3),
    )
    before = {e.record.id: e for e in ledger.entries()}

    replayed = _ledger(tmp_path)
    after = {e.record.id: e for e in replayed.entries()}
    assert after == before
    assert after[first.id].record.valid_from == T0
    assert after[first.id].record.recorded_at == T0 + timedelta(hours=1)
    assert after[first.id].superseded_at == T0 + timedelta(hours=2)
    assert after[investigation.id].record.valid_to == T0 + timedelta(days=3)
    for line in (tmp_path / "memory.jsonl").read_text().splitlines():
        assert record_to_dict(record_from_dict(json.loads(line))) == json.loads(line)


def test_the_ledger_needs_a_scoped_config(tmp_path: Path) -> None:
    config = isolated_config(tmp_path)
    with pytest.raises(UnscopedMemoryError):
        memory_ledger_for(config)
    scoped = scoped_test_config(config)
    ledger = memory_ledger_for(scoped)
    assert ledger.vehicle == "test-vehicle"
    assert ledger.path == scoped.daemon.memory_ledger
    assert "test-vehicle" in ledger.path.parts


def test_a_to_b_to_a_appends_a_new_current_record_not_the_superseded_one(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    a_values = {"input_hashes": {"pairs": "a" * 64}}
    b_values = {"input_hashes": {"pairs": "b" * 64}}
    first = ledger.append(_evidence(**a_values), now=T0 + timedelta(hours=1))
    second = ledger.append(_evidence(**b_values), now=T0 + timedelta(hours=2))
    third = ledger.append(_evidence(**a_values), now=T0 + timedelta(hours=3))
    assert third.id not in {first.id, second.id}
    assert third.supersedes == (second.id,)
    assert ledger.entry(third.id).current
    assert not ledger.entry(first.id).current
    assert ledger.entry(second.id).superseded_by == (third.id,)
    # And re-stating the now-current content is still idempotent.
    assert ledger.append(_evidence(**a_values), now=T0 + timedelta(hours=4)).id == third.id


def test_re_minting_after_a_retraction_is_a_new_record(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    finding = ledger.append(_evidence(), now=T0)
    ledger.append(
        Retraction(
            vehicle=_vehicle(),
            provenance=_operator(),
            valid_from=T0,
            retracts=(finding.id,),
            reason="window included a fuel cut",
        ),
        now=T0 + timedelta(hours=1),
    )
    again = ledger.append(_evidence(), now=T0 + timedelta(hours=2))
    assert again.id != finding.id
    assert ledger.entry(again.id).current
    assert ledger.entry(finding.id).retracted_by is not None


def test_a_schema_invalid_line_is_a_corrupt_record_not_an_unminted_id(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    stored = ledger.append(_evidence())
    line = record_to_dict(stored)
    line["id"] = "ev-corruptcorruptcorrupt0"
    line.pop("conditions")
    with ledger.path.open("a") as handle:
        handle.write(json.dumps(line) + "\n")
    with pytest.raises(CorruptRecordError, match="corrupt record"):
        ledger.resolve_evidence("ev-corruptcorruptcorrupt0")
    with pytest.raises(UnknownRecordError, match="was not minted"):
        ledger.resolve_evidence("ev-000000000000000000000000")
