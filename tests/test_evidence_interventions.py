"""E01.4: intervention history, prediction order, matched floors, retirement."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.analysis.noise_floor import FloorRefusedError, floor_reference, load_noise_floor
from src.behavior.accepted_baselines import (
    AcceptedBaselineOverride,
    AcceptedBaselines,
    DirectionalBand,
    investigation_links,
    override_reference,
)
from src.behavior.accepted_baselines import Provenance as OverrideProvenance
from src.evidence import (
    Conditions,
    FloorVerificationError,
    Intervention,
    Investigation,
    MemoryLedger,
    NoiseFloorRef,
    Outcome,
    OutcomeRuleError,
    Prediction,
    Quantity,
    Reference,
    Result,
    Retraction,
    VehicleIdentity,
)
from src.evidence.interventions import (
    UnclassifiedInterventionError,
    record_intervention,
    source_drift,
)
from src.paths import DATA_DIR
from src.vehicle.interventions import intervention_history
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry, load_tune_revisions
from src.vehicle.vehicle_ledger import VehicleLedgerStore
from tests.test_evidence_memory import _evidence, _operator, _vehicle

PROFILE = DATA_DIR / "nissan-xterra-2008"
WOT = "WOT: throttle>=85, 3000-5000 rpm, matched on rpm x MAF voltage, 50 ms grid"
IDLE = "idle: rpm<900, closed throttle"
FLASH = datetime(2026, 10, 4, 18, 0, tzinfo=UTC)
IMAGE = "5" * 64
CONTAINER = "c" * 64


def _ledger(tmp_path: Path) -> MemoryLedger:
    return MemoryLedger(tmp_path / "memory.jsonl", vehicle="truck")


def _floor() -> NoiseFloorRef:
    """The tracked profile's measured WOT delivery-error floor."""
    return floor_reference(PROFILE / "noise_floor.yaml", "delivery_error_pct", conditions=WOT)


def _prediction(**overrides: Any) -> Prediction:
    fields: dict[str, Any] = {
        "vehicle": _vehicle(),
        "provenance": _operator(),
        "conditions": Conditions(regime=WOT, vehicle_state="engine_running"),
        "valid_from": FLASH,
        "investigation": None,
        "intervention": None,
        "quantity": "delivery_error_pct",
        "direction": "decrease",
        "magnitude": Quantity(value=3.0, unit="percent"),
        "noise_floor": _floor(),
    }
    fields.update(overrides)
    return Prediction(**fields)


def _flash_intervention(at: datetime = FLASH, **overrides: Any) -> Intervention:
    fields: dict[str, Any] = {
        "vehicle": VehicleIdentity(
            profile="truck", vin=None, program_id=None, calibration="ZS44A-20261004"
        ),
        "provenance": _operator(),
        "conditions": Conditions(regime="all conditions", vehicle_state="ignition_only"),
        "valid_from": at,
        "intervention_kind": "calibration",
        "title": "flash ZS44A-20261004",
        "source": "tune_revision",
        "source_ref": "ZS44A-20261004",
        "source_digest": "d" * 64,
        "affects": ("delivery_error_pct",),
        "effective_precision": "instant",
        "image_sha256": IMAGE,
    }
    fields.update(overrides)
    return Intervention(**fields)


def _outcome(prediction: str, evidence: str, **overrides: Any) -> Outcome:
    fields: dict[str, Any] = {
        "vehicle": _vehicle(),
        "provenance": _operator(),
        "conditions": Conditions(regime=WOT, vehicle_state="engine_running"),
        "valid_from": FLASH + timedelta(days=1),
        "prediction": prediction,
        "intervention": None,
        "disposition": "measured_change",
        "evidence": (evidence,),
        "result": Result(status="evaluated", residual=Quantity(value=-3.4, unit="percent")),
    }
    fields.update(overrides)
    return Outcome(**fields)


# -- prediction order ---------------------------------------------------------------


def test_a_prediction_recorded_after_its_intervention_is_post_hoc_and_never_confirmed(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    flash = ledger.append(_flash_intervention(), now=FLASH - timedelta(hours=3))  # planned
    prior = ledger.append(_prediction(intervention=flash.id), now=FLASH - timedelta(hours=1))
    after = FLASH + timedelta(days=1)
    measured = ledger.append(
        _evidence(
            conditions=Conditions(regime=WOT, vehicle_state="engine_running"),
            valid_from=after,
            valid_to=after + timedelta(minutes=5),
        ),
        now=after + timedelta(hours=1),
    )
    hindsight = ledger.append(
        _prediction(intervention=flash.id, magnitude=Quantity(value=3.5, unit="percent")),
        now=after + timedelta(hours=2),
    )
    assert ledger.prediction_order(prior.id) == "prior"
    assert ledger.prediction_order(hindsight.id) == "post_hoc"

    confirmed = ledger.append(_outcome(prior.id, measured.id), now=after + timedelta(hours=3))
    assessment = ledger.assess(confirmed.id)
    assert (assessment.order, assessment.verdict) == ("prior", "confirmed")

    late = ledger.append(_outcome(hindsight.id, measured.id), now=after + timedelta(hours=4))
    assessment = ledger.assess(late.id)
    assert (assessment.order, assessment.verdict) == ("post_hoc", "post_hoc")
    assert "cannot count as confirmed" in assessment.reason


def test_a_prediction_against_a_day_precision_change_must_precede_the_whole_day(
    tmp_path: Path,
) -> None:
    store = VehicleLedgerStore(tmp_path / "vehicle_ledger.jsonl")
    pump = store.record(
        occurred_at=date(2026, 10, 2),
        kind="mod",
        title="Aeromotive pump installed",
        affected_channels=["delivery_error_pct"],
        intervention="fuel",
    )
    (source,) = intervention_history(None, store.records())
    assert (source.precision, source.effective_at) == ("day", datetime(2026, 10, 2, tzinfo=UTC))
    ledger = _ledger(tmp_path)
    change = record_intervention(
        ledger,
        source,
        vehicle=_vehicle(),
        conditions=Conditions(regime="all conditions", vehicle_state=None),
        provenance=_operator(),
        now=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert change.source_ref == pump.id and change.intervention_kind == "fuel"
    same_day = ledger.append(
        _prediction(intervention=change.id), now=datetime(2026, 10, 2, 9, tzinfo=UTC)
    )
    # The pump may have gone in before 09:00 that day: not provably prior.
    assert ledger.prediction_order(same_day.id) == "post_hoc"


# -- floors and outcomes ------------------------------------------------------------


def test_a_noise_floor_from_another_regime_is_refused() -> None:
    with pytest.raises(ValidationError, match="only valid for its own regime"):
        _prediction(conditions=Conditions(regime=IDLE, vehicle_state="engine_running"))
    path = PROFILE / "noise_floor.yaml"
    with pytest.raises(FloorRefusedError) as refused:
        floor_reference(path, "delivery_error_pct", conditions=IDLE)
    assert refused.value.refusal.kind == "conditions_mismatch"
    with pytest.raises(FloorRefusedError, match="no noise floor"):
        floor_reference(PROFILE / "absent_floor.yaml", "delivery_error_pct", conditions=WOT)
    matched = floor_reference(path, "delivery_error_pct", conditions=WOT)
    assert matched.conditions == WOT
    assert matched.source == "data/nissan-xterra-2008/noise_floor.yaml"
    assert matched.sd.value == load_noise_floor(path).channels["delivery_error_pct"].sd  # type: ignore[union-attr]
    assert _prediction(noise_floor=matched).noise_floor == matched


def test_a_relabelled_or_invented_floor_is_refused_against_its_source(tmp_path: Path) -> None:
    """The copy is re-derived from the cited file at append time (E01.4)."""
    ledger = _ledger(tmp_path)
    wot = _floor()
    relabelled = wot.model_copy(update={"conditions": IDLE})
    idle = Conditions(regime=IDLE, vehicle_state="engine_running")
    with pytest.raises(FloorVerificationError, match="conditions"):
        ledger.append(_prediction(conditions=idle, noise_floor=relabelled))
    shrunk = wot.model_copy(update={"sd": Quantity(value=0.001, unit="percent")})
    with pytest.raises(FloorVerificationError, match="sd"):
        ledger.append(_prediction(noise_floor=shrunk))
    invented = NoiseFloorRef(
        quantity="delivery_error_pct",
        sd=Quantity(value=0.001, unit="percent"),
        conditions=WOT,
        sigma_threshold=2.0,
        source="made/up.yaml",
        source_sha256="e" * 64,
    )
    with pytest.raises(FloorVerificationError, match="does not exist"):
        ledger.append(_prediction(noise_floor=invented))
    rehashed = wot.model_copy(update={"source_sha256": "e" * 64})
    with pytest.raises(FloorVerificationError, match="no longer hashes"):
        ledger.append(_prediction(noise_floor=rehashed))
    with pytest.raises(ValidationError):  # a lower bar than the floor's own is refused
        NoiseFloorRef(**{**wot.model_dump(), "sigma_threshold": 0.1})
    with pytest.raises(ValidationError):  # no hash, no floor
        NoiseFloorRef(**{**wot.model_dump(), "source_sha256": None})
    assert ledger.query() == []
    stored = ledger.append(_prediction())
    assert isinstance(stored, Prediction) and stored.noise_floor == wot


def test_outcomes_say_which_kind_of_null_against_the_matched_floor(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    floorless = ledger.append(_prediction(noise_floor=None), now=FLASH - timedelta(hours=2))
    bounded = ledger.append(_prediction(), now=FLASH - timedelta(hours=1))
    measured = ledger.append(
        _evidence(
            conditions=Conditions(regime=WOT, vehicle_state="engine_running"),
            valid_from=FLASH,
            valid_to=FLASH + timedelta(minutes=5),
        ),
        now=FLASH,
    )
    small = Result(status="evaluated", residual=Quantity(value=-1.0, unit="percent"))
    with pytest.raises(OutcomeRuleError, match="unresolvable"):
        ledger.append(_outcome(floorless.id, measured.id, disposition="no_effect", result=small))
    with pytest.raises(OutcomeRuleError, match="within the noise floor"):
        ledger.append(_outcome(bounded.id, measured.id, result=small))
    with pytest.raises(OutcomeRuleError, match="clears the noise floor"):
        ledger.append(
            _outcome(
                bounded.id,
                measured.id,
                disposition="no_effect",
                result=Result(status="evaluated", residual=Quantity(value=-4.0, unit="percent")),
            )
        )
    with pytest.raises(OutcomeRuleError, match="matched conditions"):
        ledger.append(
            _outcome(
                bounded.id,
                measured.id,
                conditions=Conditions(regime=IDLE, vehicle_state="engine_running"),
            )
        )
    null = ledger.append(
        _outcome(bounded.id, measured.id, disposition="no_effect", result=small),
        now=FLASH + timedelta(hours=1),
    )
    assert ledger.assess(null.id).verdict == "contradicted"
    unresolvable = ledger.append(
        _outcome(
            floorless.id,
            measured.id,
            disposition="unresolvable",
            result=Result(status="unevaluable", reason="uncovered", detail="no WOT cells"),
        ),
        now=FLASH + timedelta(hours=2),
    )
    assert ledger.assess(unresolvable.id).verdict == "unresolvable"


# -- calibration identity and unknowns ------------------------------------------------


def test_a_calibration_record_carries_the_exact_image_hash_or_states_none() -> None:
    with pytest.raises(ValidationError, match="states its exact image_sha256"):
        Intervention(
            **{
                k: v
                for k, v in _flash_intervention().model_dump(by_alias=True).items()
                if k not in {"image_sha256", "kind", "schema", "id", "recorded_at"}
            }
        )
    with pytest.raises(ValidationError, match="only a calibration"):
        _flash_intervention(intervention_kind="hardware", source="none", source_ref=None)
    registry = TuneRevisionRegistry(
        revisions=[
            TuneRevision(
                revision="ZS44A-old",
                flashed_at=datetime(2026, 7, 26, 22, 31, tzinfo=UTC),
                rom_sha256=CONTAINER,
            ),
            TuneRevision(revision="ZS44A-new", flashed_at=FLASH, image_sha256=IMAGE),
        ]
    )
    old, new = intervention_history(registry, [])
    assert old.image_sha256 is None  # a container hash is not an image hash
    assert new.image_sha256 == IMAGE


def test_the_tracked_flash_history_records_into_memory_without_guessing(tmp_path: Path) -> None:
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    history = intervention_history(registry, [])
    by_name = {item.source_ref: item for item in history}
    for revision in registry.revisions:
        assert by_name[revision.revision].image_sha256 == revision.image_sha256
    ledger = _ledger(tmp_path)
    latest = history[-1]
    stored = record_intervention(
        ledger,
        latest,
        vehicle=VehicleIdentity(profile="truck", vin=None, program_id=None, calibration=None),
        conditions=Conditions(regime="all conditions", vehicle_state=None),
        provenance=_operator(),
        affects=("*",),
    )
    assert stored.vehicle.calibration == latest.source_ref
    assert stored.image_sha256 == latest.image_sha256
    assert stored.valid_from == latest.effective_at
    again = record_intervention(
        ledger,
        latest,
        vehicle=VehicleIdentity(profile="truck", vin=None, program_id=None, calibration=None),
        conditions=Conditions(regime="all conditions", vehicle_state=None),
        provenance=_operator(),
        affects=("*",),
    )
    assert again.id == stored.id
    assert source_drift(stored, history) is None
    assert source_drift(stored, history[:-1]) == "removed"


def test_an_unclassified_physical_change_stays_unknown(tmp_path: Path) -> None:
    store = VehicleLedgerStore(tmp_path / "vehicle_ledger.jsonl")
    store.record(occurred_at=date(2026, 8, 1), kind="repair", title="replaced a hose")
    store.record(occurred_at=date(2026, 8, 2), kind="observation", title="noise at idle")
    (unclassified,) = intervention_history(None, store.records())
    assert unclassified.intervention_kind is None
    with pytest.raises(UnclassifiedInterventionError, match="not classified"):
        record_intervention(
            _ledger(tmp_path),
            unclassified,
            vehicle=_vehicle(),
            conditions=Conditions(regime="all conditions", vehicle_state=None),
            provenance=_operator(),
            affects=("*",),
        )


def test_editing_the_source_event_is_detected(tmp_path: Path) -> None:
    store = VehicleLedgerStore(tmp_path / "vehicle_ledger.jsonl")
    event = store.record(
        occurred_at=date(2026, 10, 1),
        kind="mod",
        title="AEM fuel pressure sender",
        affected_channels=["adc:FUEL_PRESSURE"],
        intervention="instrumentation",
    )
    stored = record_intervention(
        _ledger(tmp_path),
        intervention_history(None, store.records())[0],
        vehicle=_vehicle(),
        conditions=Conditions(regime="all conditions", vehicle_state=None),
        provenance=_operator(),
    )
    assert stored.affects == ("adc:FUEL_PRESSURE",)
    store.record(
        occurred_at=date(2026, 10, 2),
        kind="mod",
        title="AEM fuel pressure sender",
        affected_channels=["adc:FUEL_PRESSURE"],
        intervention="instrumentation",
        event_id=event.id,
    )
    assert source_drift(stored, intervention_history(None, store.records())) == "edited"
    assert VehicleLedgerStore(store.path).get(event.id).intervention == "instrumentation"  # type: ignore[union-attr]


# -- references ---------------------------------------------------------------------


def _reference(channels: tuple[str, ...], valid_from: datetime, **overrides: Any) -> Reference:
    fields: dict[str, Any] = {
        "vehicle": _vehicle(),
        "provenance": _operator(),
        "conditions": Conditions(regime="idle: rpm<900, closed throttle", vehicle_state=None),
        "valid_from": valid_from,
        "reference_kind": "behavior_model",
        "name": f"reference for {channels[0]}",
        "channels": channels,
        "source": "behavior_models",
        "source_ref": channels[0],
        "investigation": None,
    }
    fields.update(overrides)
    return Reference(**fields)


def test_a_hardware_intervention_retires_affected_references_without_deleting_them(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    stft = ledger.append(_reference(("obd:SHORT_FUEL_TRIM_1",), t0), now=t0)
    coolant = ledger.append(_reference(("obd:COOLANT_TEMP",), t0), now=t0 + timedelta(hours=1))
    installed = datetime(2026, 9, 20, tzinfo=UTC)
    change = ledger.append(
        Intervention(
            vehicle=_vehicle(),
            provenance=_operator(),
            conditions=Conditions(regime="all conditions", vehicle_state=None),
            valid_from=installed,
            intervention_kind="hardware",
            title="injectors replaced",
            source="none",
            source_ref=None,
            affects=("obd:SHORT_FUEL_TRIM_1", "obd:SHORT_FUEL_TRIM_2"),
            effective_precision="day",
        ),
        now=installed + timedelta(days=1),
    )
    rebuilt = ledger.append(
        _reference(("obd:SHORT_FUEL_TRIM_1",), installed + timedelta(days=3), name="rebuilt"),
        now=installed + timedelta(days=3),
    )
    entry = ledger.entry(stft.id)
    assert entry.retired_by == change.id and entry.retired_at == installed
    assert entry.current and entry.record == stft  # retired, not deleted or superseded
    assert ledger.entry(coolant.id).retired_by is None
    assert ledger.entry(rebuilt.id).retired_by is None  # built after the change
    assert stft.id in (tmp_path / "memory.jsonl").read_text()
    active = ledger.query(kinds=("reference",), include_retired=False)
    assert {e.record.id for e in active} == {coolant.id, rebuilt.id}
    valid_before = ledger.query(kinds=("reference",), valid_at=installed - timedelta(days=1))
    assert {e.record.id for e in valid_before} == {stft.id, coolant.id}
    valid_after = ledger.query(kinds=("reference",), valid_at=installed + timedelta(days=5))
    assert {e.record.id for e in valid_after} == {coolant.id, rebuilt.id}
    # Before the intervention was recorded, nobody knew the reference was retired.
    assert ledger.entry(stft.id, as_of=installed + timedelta(hours=12)).retired_by is None
    # A retracted intervention retires nothing.
    ledger.append(
        Retraction(
            vehicle=_vehicle(),
            provenance=_operator(),
            valid_from=installed,
            retracts=(change.id,),
            reason="entered against the wrong vehicle",
        ),
        now=installed + timedelta(days=4),
    )
    assert ledger.entry(stft.id).retired_by is None


def test_accepted_baselines_link_to_their_investigation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    investigation = ledger.append(
        Investigation(
            vehicle=_vehicle(),
            provenance=_operator(),
            conditions=Conditions(regime="idle: rpm<900, closed throttle", vehicle_state=None),
            valid_from=datetime(2026, 9, 1, tzinfo=UTC),
            title="Weak injector on bank one",
            question="is the lean idle trim an injector or a leak",
        ),
        now=datetime(2026, 9, 2, tzinfo=UTC),
    )
    provenance = OverrideProvenance(
        declared_by="operator",
        declared_at=date(2026, 9, 2),
        review_by=date(2026, 12, 2),
        reason="known weak injector",
    )
    band = DirectionalBand(
        tolerance=1.0, worse_direction="positive", worse_warning=3.0, worse_critical=6.0
    )

    def override(channel: str, link: str | None) -> AcceptedBaselineOverride:
        return AcceptedBaselineOverride(
            channel=channel,
            accepted_center=4.0,
            band=band,
            provenance=provenance,
            investigation=link,
        )

    baselines = AcceptedBaselines(
        schema_version=1,
        overrides=[
            override("obd:SHORT_FUEL_TRIM_1", investigation.id),
            override("obd:SHORT_FUEL_TRIM_2", None),
            override("obd:LONG_FUEL_TRIM_1", "inv-000000000000000000000000"),
        ],
    )
    states = [(link.target, link.state) for link in investigation_links(baselines, ledger)]
    assert states == [
        ("obd:SHORT_FUEL_TRIM_1", "linked"),
        ("obd:SHORT_FUEL_TRIM_2", "unlinked"),
        ("obd:LONG_FUEL_TRIM_1", "unresolved"),
    ]
    reference = ledger.append(
        override_reference(
            baselines.overrides[0],
            vehicle=_vehicle(),
            conditions=Conditions(regime="idle: rpm<900, closed throttle", vehicle_state=None),
            provenance=_operator(),
            valid_from=datetime(2026, 9, 2, tzinfo=UTC),
        ),
        now=datetime(2026, 9, 3, tzinfo=UTC),
    )
    assert isinstance(reference, Reference)
    assert reference.investigation == investigation.id
    assert reference.channels == ("obd:SHORT_FUEL_TRIM_1",)
    # The tracked profile file still loads (the link is optional).
    from src.behavior.accepted_baselines import load_accepted_baselines

    assert load_accepted_baselines(PROFILE / "accepted_baselines.yaml").schema_version >= 1


def test_an_outcome_cannot_rest_on_data_measured_before_the_change(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    flash = ledger.append(_flash_intervention(), now=FLASH - timedelta(hours=3))
    prediction = ledger.append(_prediction(intervention=flash.id), now=FLASH - timedelta(hours=2))
    before = FLASH - timedelta(days=10)
    stale = ledger.append(
        _evidence(
            conditions=Conditions(regime=WOT, vehicle_state="engine_running"),
            subject="pre-flash data",
            valid_from=before,
            valid_to=before + timedelta(minutes=5),
        ),
        now=FLASH - timedelta(hours=1),
    )
    with pytest.raises(OutcomeRuleError, match="before intervention"):
        ledger.append(_outcome(prediction.id, stale.id, intervention=flash.id))
    # Saying it could not be resolved on that data is allowed.
    allowed = ledger.append(
        _outcome(
            prediction.id,
            stale.id,
            disposition="unresolvable",
            result=Result(
                status="unevaluable", reason="uncovered", detail="no post-flash WOT data"
            ),
        ),
        now=FLASH + timedelta(hours=1),
    )
    assert ledger.assess(allowed.id).verdict == "unresolvable"


def test_a_sourced_intervention_carries_its_digest_and_drift_is_named(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="source_digest"):
        _flash_intervention(source_digest=None)
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    history = intervention_history(registry, [])
    latest = history[-1]
    ledger = _ledger(tmp_path)
    honest = record_intervention(
        ledger,
        latest,
        vehicle=_vehicle(),
        conditions=Conditions(regime="all conditions", vehicle_state=None),
        provenance=_operator(),
        affects=("*",),
    )
    assert source_drift(honest, history) is None
    forged = honest.model_copy(update={"valid_from": latest.effective_at + timedelta(days=30)})
    assert source_drift(forged, history) == "mismatch:valid_from"
    undigested = Intervention.model_construct(
        **{**dict(honest), "source_digest": None}  # a record from before the rule
    )
    assert source_drift(undigested, history) == "no_digest"


def test_flash_precision_comes_from_the_revision_never_silently_instant(tmp_path: Path) -> None:
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    precision = {item.source_ref: item.precision for item in intervention_history(registry, [])}
    assert precision == {
        "ZS04C-stock": "floor",
        "ZS44A-osiris-initial": "unknown",
        "ZS44A-k105": "approximate",
        "ZS44A-k106": "approximate",
        "ZS44A-20260920": "instant",
        "ZS44A-20261004": "instant",
    }
    k106 = next(i for i in intervention_history(registry, []) if i.source_ref == "ZS44A-k106")
    ledger = _ledger(tmp_path)
    change = record_intervention(
        ledger,
        k106,
        vehicle=_vehicle(),
        conditions=Conditions(regime=WOT, vehicle_state=None),
        provenance=_operator(),
        affects=("delivery_error_pct",),
        now=k106.effective_at - timedelta(days=2),
    )
    assert change.effective_precision == "approximate"
    prediction = ledger.append(
        _prediction(intervention=change.id), now=k106.effective_at - timedelta(days=1)
    )
    assert ledger.prediction_order(prediction.id) == "undetermined"
    after = k106.effective_at + timedelta(days=1)
    measured = ledger.append(
        _evidence(
            conditions=Conditions(regime=WOT, vehicle_state="engine_running"),
            valid_from=after,
            valid_to=after + timedelta(minutes=5),
        ),
        now=after,
    )
    outcome = ledger.append(_outcome(prediction.id, measured.id), now=after + timedelta(hours=1))
    assessment = ledger.assess(outcome.id)
    assert (assessment.order, assessment.verdict) == ("undetermined", "undetermined")
    # Unset precision is unknown, and the tracked file's precision wins a merge.
    bare = TuneRevision(revision="x", flashed_at=FLASH)
    assert bare.flashed_at_precision is None
    from src.vehicle.tune_revisions import merge_tune_registries

    merged = merge_tune_registries(
        TuneRevisionRegistry(revisions=[bare]),
        Path("operational.yaml"),
        TuneRevisionRegistry(
            revisions=[bare.model_copy(update={"flashed_at_precision": "approximate"})]
        ),
        Path("tracked.yaml"),
    )
    assert merged is not None and merged.revisions[0].flashed_at_precision == "approximate"
