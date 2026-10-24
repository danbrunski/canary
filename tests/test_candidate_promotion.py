"""E03.6: evaluate an adaptive candidate on independent drives, promote it
explicitly and atomically, and keep everything it replaced.

**Every drive here is labelled synthetic** (the E03.5 kernel's generator,
``tests/test_adaptive_candidate.py``): no real reference can be frozen yet
(E03.3), so no real candidate exists. Decisions on synthetic drives promote
only on an isolated (replay) ledger; the live-ledger refusal is tested too.

Scenario: the reference is fitted on the true relation; afterwards the engine
sits ``DRIFT`` points off it (inside half the shift of interest, so no alarm).
The candidate learns that on four drives; its prediction is registered; then
five later drives are the independent evidence (three test, two calibrating
the promoted reference).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.analysis.candidate_promotion import (
    CandidateDecision,
    CandidateDrive,
    PromotionError,
    PromotionRules,
    candidate_status,
    evaluate_candidate,
    floor_quantity,
    promote,
    register_prediction,
)
from src.analysis.change_evaluation import (
    CHANGE_RESIDUALS_FILE,
    load_change_residuals,
    replay_candidate,
)
from src.config import CanaryConfig
from src.control import brain_jobs
from src.control.brain_jobs import BrainJobRefusedError, BrainJobStore, canonical_params
from src.evidence.ledger import MemoryLedger
from src.evidence.records import (
    Conditions,
    Intervention,
    Outcome,
    Provenance,
    Reference,
    VehicleIdentity,
)
from src.models.adaptive_candidate import CandidateCheckpoint, CandidateSpec
from src.models.candidate_store import CandidateStore, CandidateStoreError, StaleActivationError
from src.models.change_detection import (
    ChangePolicy,
    FrozenReference,
    fit_reference,
    reference_fingerprint,
)
from src.vehicle.profiles import profile_dir
from tests.test_adaptive_candidate import (
    CHANNELS,
    DAY,
    PROFILE,
    T0,
    _alarm,
    _mixed,
    _policy,
    _reference,
    _spec,
)
from tests.test_brain_jobs import scheduler, scoped_config

RESIDUAL = "balance"
DRIFT = 0.6
CONDITIONS = "synthetic: one drive's mean balance residual, all calibrated cells"
VEHICLE = VehicleIdentity(profile=PROFILE, vin=None, program_id=None, calibration="REV")


def _at(days: float) -> datetime:
    return datetime.fromtimestamp(T0 + days * DAY, UTC)


def _drives(
    prefix: str, n: int, *, first_day: float, offset: float, seed: int, **kw: Any
) -> list[CandidateDrive]:
    return [
        CandidateDrive(
            f"{prefix}{k}",
            (f"{prefix}{k}",),
            "REV",
            tuple(
                _mixed(
                    f"{prefix}{k}",
                    30,
                    seed=seed + k,
                    t0=T0 + (first_day + k) * DAY,
                    offset=offset,
                    **kw,
                )
            ),  # fmt: skip
            "synthetic",
        )
        for k in range(n)
    ]


def _floor(path: Path, sd: float = 0.05) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "provenance": {"note": "SYNTHETIC floor for the E03.6 test"},
                "channels": {
                    floor_quantity(RESIDUAL): {
                        "unit": "percent",
                        "conditions": CONDITIONS,
                        "sd": sd,
                        "mean": 0.0,
                        "n_cells": 5,
                    }
                },
            }
        )
    )
    return path


@dataclass
class World:
    ledger: MemoryLedger
    store: CandidateStore
    reference: FrozenReference
    checkpoint: str
    prediction: str
    learning: list[CandidateDrive]
    floor: Path


def _world(
    root: Path,
    *,
    drift: float = DRIFT,
    floor: bool = True,
    ledger: MemoryLedger | None = None,
    alarm_before_learning: bool = False,
) -> World:
    ledger = ledger or MemoryLedger(
        root / "memory.jsonl", vehicle=PROFILE, replay_id="e036-synthetic"
    )
    if alarm_before_learning:
        _alarm(ledger)
    store = CandidateStore(root / "candidates")
    reference = _reference()
    store.install_reference(RESIDUAL, reference, _policy(), now=_at(0))
    learning = _drives("l", 4, first_day=0, offset=drift, seed=500)
    _traces, candidate = replay_candidate(
        [(d.drive, list(d.blocks)) for d in learning], reference, _spec(), ledger=ledger,
        calibration="REV", channels=CHANNELS, now=_at(4.5),
    )  # fmt: skip
    checkpoint = store.save_checkpoint(RESIDUAL, candidate.checkpoint(), _spec())
    floor_path = _floor(root / "noise_floor.yaml")
    prediction = ""
    if candidate.checkpoint().valid_to is not None:  # a frozen lineage predicts nothing
        prediction = register_prediction(
            ledger, store, RESIDUAL, checkpoint, vehicle=VEHICLE,
            floor_path=floor_path if floor else None, floor_conditions=CONDITIONS, now=_at(4.6),
        )  # fmt: skip
    return World(ledger, store, reference, checkpoint, prediction, learning, floor_path)


def _evaluate(world: World, drives: list[CandidateDrive], **kw: Any) -> CandidateDecision:
    return evaluate_candidate(
        world.ledger, world.store, RESIDUAL, world.checkpoint, drives, channels=CHANNELS,
        vehicle=VEHICLE, rules=kw.pop("rules", PromotionRules()), now=kw.pop("now", _at(20)),
    )  # fmt: skip


def _independent(offset: float = DRIFT, **kw: Any) -> list[CandidateDrive]:
    return _drives("e", 5, first_day=5, offset=offset, seed=600, **kw)


# -- the qualified path --------------------------------------------------------


def test_a_qualified_candidate_promotes_atomically_and_the_old_reference_stays(tmp_path):
    world = _world(tmp_path)
    old = world.reference.fingerprint
    old_record = world.ledger.append(
        Reference(
            vehicle=VEHICLE,
            provenance=Provenance(
                producer="change_detection", producer_kind="tool", version="1", code_version="x"
            ),
            conditions=Conditions(regime="synthetic reference", vehicle_state="engine_running"),
            valid_from=world.reference.valid_from,
            reference_kind="evidence",
            name=world.reference.name,
            channels=CHANNELS,
            source="memory",
            source_ref=f"change_reference:{old}",
            investigation=None,
        ),
        now=_at(4.7),
    ).id
    decision = _evaluate(world, _independent())
    assert decision.status == "qualified", decision.reasons
    assert decision.test_drives == ["e0", "e1", "e2"]
    assert decision.calibration_drives == ["e3", "e4"]
    assert all(o["verdict"] == "confirmed" for o in decision.outcomes.values())
    assert (
        decision.checks["known_answers"]["promoted"] >= decision.checks["known_answers"]["current"]
    )
    # Evaluating never promotes.
    assert world.store.active_fingerprint(RESIDUAL) == old

    result = promote(
        world.ledger, world.store, RESIDUAL, decision.key or "", vehicle=VEHICLE,
        channels=CHANNELS, now=_at(21),
    )  # fmt: skip
    assert result.changed and result.previous == old
    promoted = world.store.active(RESIDUAL)
    assert promoted is not None and promoted.fingerprint == decision.promoted_reference
    assert promoted.lineage == "promoted_candidate"
    assert promoted.promoted_from == world.checkpoint and promoted.previous_reference == old
    assert set(promoted.calibration_drives) == {"e3", "e4"}
    # The old reference is kept, in the store and on the ledger, and recoverable.
    assert world.store.reference(RESIDUAL, old).fingerprint == old
    (moved,) = world.store.history(RESIDUAL)
    assert moved["reference"] == old and moved["decision"] == decision.key
    entry = world.ledger.entry(old_record)
    assert not entry.current and result.record in entry.superseded_by
    world.store.restore(RESIDUAL, old, reason="operator rollback", now=_at(22))
    assert world.store.active_fingerprint(RESIDUAL) == old
    assert world.store.reference(RESIDUAL, promoted.fingerprint) == promoted


def test_replaying_the_same_evidence_creates_no_second_outcome_or_promotion(tmp_path):
    world = _world(tmp_path)
    first = _evaluate(world, _independent())
    records = len(world.ledger.entries())
    again = _evaluate(world, _independent())
    assert again.key == first.key and again.outcomes == first.outcomes
    assert len(world.ledger.entries()) == records
    assert len(world.store.decisions(RESIDUAL)) == 1
    one = promote(world.ledger, world.store, RESIDUAL, first.key or "", vehicle=VEHICLE,
                  channels=CHANNELS, now=_at(21))  # fmt: skip
    two = promote(world.ledger, world.store, RESIDUAL, first.key or "", vehicle=VEHICLE,
                  channels=CHANNELS, now=_at(22))  # fmt: skip
    assert one.changed and not two.changed and two.record == one.record
    assert len(world.store.history(RESIDUAL)) == 1
    outcomes = [e for e in world.ledger.entries() if isinstance(e.record, Outcome)]
    assert len(outcomes) == 3


def test_a_stale_decision_cannot_overwrite_a_newer_reference(tmp_path):
    world = _world(tmp_path)
    decision = _evaluate(world, _independent())
    promote(world.ledger, world.store, RESIDUAL, decision.key or "", vehicle=VEHICLE,
            channels=CHANNELS, now=_at(21))  # fmt: skip
    other = world.store.reference(RESIDUAL, world.reference.fingerprint)
    with pytest.raises(StaleActivationError):
        world.store.activate(
            RESIDUAL, _reference(seed=7), _policy(), expected_active=other.fingerprint,
            decision="stale",
        )  # fmt: skip


def test_a_synthetic_decision_cannot_promote_in_live_memory(tmp_path):
    live = MemoryLedger(tmp_path / "live.jsonl", vehicle=PROFILE)
    world = _world(tmp_path, ledger=live)
    decision = _evaluate(world, _independent())
    assert decision.status == "qualified" and decision.data_origin == "synthetic"
    with pytest.raises(PromotionError, match="synthetic"):
        promote(live, world.store, RESIDUAL, decision.key or "", vehicle=VEHICLE,
                channels=CHANNELS)  # fmt: skip
    assert world.store.active_fingerprint(RESIDUAL) == world.reference.fingerprint


# -- what cannot promote ---------------------------------------------------------


def test_a_better_recent_fit_alone_cannot_promote(tmp_path):
    """The candidate fits its own drives better than the reference does, by
    construction. Those drives are in-sample and never counted; and when the
    independent drives no longer carry the drift, the outcome contradicts the
    prediction and the candidate is rejected (and kept)."""
    world = _world(tmp_path)
    own = _evaluate(world, world.learning)
    assert own.status == "pending" and not own.test_drives
    assert set(own.excluded.values()) == {
        "in_sample: it begins within the candidate's learned data"
    }
    assert own.reasons[0].startswith("insufficient_independent_drives")

    gone = _evaluate(world, _independent(offset=0.0))
    assert gone.status == "rejected" and gone.reasons[0].startswith("contradicted")
    assert {o["verdict"] for o in gone.outcomes.values()} == {"contradicted"}
    assert world.checkpoint in world.store.checkpoints(RESIDUAL)
    assert [d["status"] for d in world.store.decisions(RESIDUAL)] == ["pending", "rejected"]
    with pytest.raises(PromotionError, match="rejected"):
        promote(world.ledger, world.store, RESIDUAL, gone.key or "", vehicle=VEHICLE,
                channels=CHANNELS)  # fmt: skip


def test_insufficient_independent_evidence_stays_pending(tmp_path):
    world = _world(tmp_path)
    two = _evaluate(world, _independent()[:2])
    assert two.status == "pending"
    assert two.reasons[0].startswith("insufficient_independent_drives: 2 of 3")
    # Three test drives but nothing to calibrate the promoted reference on.
    three = _evaluate(world, _independent()[:3])
    assert three.status == "pending" and three.reasons[0].startswith("recalibration_pending")
    # A drive that began before the prediction was recorded is post hoc.
    early = _drives("p", 1, first_day=4.55, offset=DRIFT, seed=700)
    assert _evaluate(world, early).excluded == {
        "p0": "post_hoc: it began before the prediction was recorded"
    }


def test_without_a_noise_floor_every_outcome_is_unresolvable_and_pending(tmp_path):
    world = _world(tmp_path, floor=False)
    decision = _evaluate(world, _independent())
    assert decision.status == "pending"
    assert {o["verdict"] for o in decision.outcomes.values()} == {"unresolvable"}
    assert floor_quantity(RESIDUAL) in decision.reasons[0]


def test_a_promoted_reference_that_would_miss_known_faults_is_rejected(tmp_path, monkeypatch):
    """Recall non-regression on the declared shifts (B03's injected-fault gate):
    a promoted reference that detects fewer of them than the current one is
    rejected, however much better it predicts."""
    from src.analysis import candidate_promotion

    real = candidate_promotion._detected
    monkeypatch.setattr(
        candidate_promotion,
        "_detected",
        lambda ref, *a: real(ref, *a) and ref.lineage == "held_out_fit",
    )
    world = _world(tmp_path)
    decision = _evaluate(world, _independent())
    assert decision.status == "rejected", decision.reasons
    assert decision.reasons[0].startswith("recall_regression")
    assert decision.checks["known_answers"]["promoted"] == 0
    assert decision.checks["known_answers"]["current"] == 3


def test_an_alarm_freezes_the_lineage_and_an_open_one_keeps_promotion_pending(tmp_path):
    frozen = _world(tmp_path / "frozen", alarm_before_learning=True)
    decision = _evaluate(frozen, _independent())
    assert decision.status == "rejected"
    assert any(r.startswith("frozen_by_alarm") or r.startswith("nothing_learned")
               for r in decision.reasons)  # fmt: skip

    world = _world(tmp_path / "open")
    # An observed alarm, raised after the candidate learned.
    scratch = MemoryLedger(tmp_path / "scratch.jsonl", vehicle=PROFILE)
    alarm = scratch.get(_alarm(scratch))
    world.ledger.append(alarm.model_copy(update={"id": "", "recorded_at": None}), now=_at(4.8))
    held = _evaluate(world, _independent())
    assert held.status == "pending" and held.reasons[0].startswith("unresolved_change_alarm")


# -- the store ---------------------------------------------------------------------


def test_the_store_refuses_edited_files_and_a_second_installation(tmp_path):
    world = _world(tmp_path)
    with pytest.raises(CandidateStoreError, match="promotion"):
        world.store.install_reference(RESIDUAL, _reference(seed=7), _policy())
    path = tmp_path / "candidates" / RESIDUAL / "references" / f"{world.reference.fingerprint}.json"
    data = json.loads(path.read_text())
    data["reference"]["theta"][0] += 0.5
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="fingerprint"):
        world.store.reference(RESIDUAL, world.reference.fingerprint)
    checkpoint, spec = world.store.checkpoint(RESIDUAL, world.checkpoint)
    assert isinstance(checkpoint, CandidateCheckpoint) and isinstance(spec, CandidateSpec)


def test_a_promoted_reference_names_its_lineage():
    reference = _reference()
    payload = reference.model_dump(mode="json")
    payload["lineage"] = "promoted_candidate"
    with pytest.raises(ValueError, match="promoted reference"):
        FrozenReference.model_validate(payload)


# -- through E04's brain jobs, on the real vehicle profile ------------------------


def _stft_policy(config: CanaryConfig) -> tuple[ChangePolicy, CandidateSpec]:
    definition = load_change_residuals(profile_dir(config) / CHANGE_RESIDUALS_FILE)[0].residual(
        "stft_bank_balance"
    )
    policy = _policy().model_copy(update={"residual": definition.name})
    return ChangePolicy.model_validate(policy.model_dump(mode="json", by_alias=True)), (
        definition.candidate
    )


def _stft_checkpoint(config: CanaryConfig, ledger: MemoryLedger) -> tuple[str, FrozenReference]:
    policy, spec = _stft_policy(config)
    train = _mixed("t1", 30, seed=1, t0=T0 - 4 * DAY) + _mixed("t2", 30, seed=2, t0=T0 - 3 * DAY)
    cal = _mixed("c1", 30, seed=3, t0=T0 - 2 * DAY) + _mixed("c2", 30, seed=4, t0=T0 - DAY)
    reference = fit_reference(policy, train, cal, name="stft", calibration_revision="REV")
    learning = [(f"l{k}", _mixed(f"l{k}", 30, seed=500 + k, t0=T0 + k * DAY)) for k in range(3)]
    _t, candidate = replay_candidate(
        learning, reference, spec, ledger=ledger, calibration="REV", channels=CHANNELS,
        now=_at(4),
    )  # fmt: skip
    store = brain_jobs.candidate_store(config)
    return store.save_checkpoint(policy.residual, candidate.checkpoint(), spec), reference


def test_the_vehicle_job_stays_pending_until_independent_evidence_exists(tmp_path):
    config = scoped_config(tmp_path)
    ledger = MemoryLedger(tmp_path / "scratch.jsonl", vehicle=PROFILE)
    checkpoint, reference = _stft_checkpoint(config, ledger)
    params = canonical_params(
        config, "candidate_evaluation", {"residual": "stft_bank_balance", "checkpoint": checkpoint}
    )
    jobs = BrainJobStore.for_config(config)
    job, _created = jobs.submit("candidate_evaluation", params)
    # Today's real state: no reference has been frozen (E03.3).
    ran = asyncio.run(scheduler(config).step())
    assert ran is None
    pending = jobs.get(job.job_id)
    assert pending is not None
    assert pending.state == "pending" and pending.reason == "no_frozen_reference"
    status = candidate_status(brain_jobs.candidate_store(config), "stft_bank_balance")
    assert status["state"] == "no_frozen_reference" and status["checkpoints"] == [checkpoint]

    # A reference exists, but no drive has been recorded since: the run
    # leaves the job pending, and it is not re-run until a new drive exists.
    policy, _spec = _stft_policy(config)
    brain_jobs.candidate_store(config).install_reference("stft_bank_balance", reference, policy)
    calls: list[str] = []

    def counted(kind: str, params: dict[str, Any], cfg: CanaryConfig, **kw: Any) -> Any:
        calls.append(kind)
        kw["address_space_bytes"] = None
        return brain_jobs.run_brain_job(kind, params, cfg, **kw)

    ran = asyncio.run(scheduler(config, runner=counted).step())
    assert ran is not None and ran.state == "pending", ran
    assert ran.reason == "no_prediction" and ran.progress["sessions_seen"] == 0
    assert asyncio.run(scheduler(config, runner=counted).step()) is None
    assert calls == ["candidate_evaluation"]
    # Promotion is a separate, explicit request, refused for a decision that
    # is not qualified.
    key = brain_jobs.candidate_store(config).decisions("stft_bank_balance")[-1]["key"]
    with pytest.raises(BrainJobRefusedError) as refused:
        canonical_params(
            config, "candidate_promotion", {"residual": "stft_bank_balance", "decision": key}
        )
    assert refused.value.status == 409


def _observed_alarm(ledger: MemoryLedger, tmp_path: Path, now: datetime) -> str:
    scratch = MemoryLedger(tmp_path / "alarm-scratch.jsonl", vehicle=PROFILE)
    alarm = scratch.get(_alarm(scratch))
    return ledger.append(alarm.model_copy(update={"id": "", "recorded_at": None}), now=now).id


def test_promotion_re_checks_an_alarm_raised_after_the_decision(tmp_path):
    world = _world(tmp_path)
    decision = _evaluate(world, _independent())
    assert decision.status == "qualified"
    _observed_alarm(world.ledger, tmp_path, _at(20.5))
    with pytest.raises(PromotionError, match="unresolved_change_alarm"):
        promote(world.ledger, world.store, RESIDUAL, decision.key or "", vehicle=VEHICLE,
                channels=CHANNELS, now=_at(21))  # fmt: skip
    assert world.store.active_fingerprint(RESIDUAL) == world.reference.fingerprint


def test_promotion_re_checks_an_intervention_recorded_after_the_decision(tmp_path):
    world = _world(tmp_path)
    decision = _evaluate(world, _independent())
    assert decision.status == "qualified"
    world.ledger.append(
        Intervention(
            vehicle=VEHICLE,
            provenance=Provenance(producer="operator", producer_kind="operator", version="1"),
            conditions=Conditions(regime="all conditions", vehicle_state=None),
            valid_from=_at(20.5),
            intervention_kind="hardware",
            title="SYNTHETIC: injector swap",
            source="none",
            source_ref=None,
            affects=CHANNELS,
            effective_precision="instant",
        ),
        now=_at(20.6),
    )
    with pytest.raises(PromotionError, match="retired"):
        promote(world.ledger, world.store, RESIDUAL, decision.key or "", vehicle=VEHICLE,
                channels=CHANNELS, now=_at(21))  # fmt: skip
    assert world.store.active_fingerprint(RESIDUAL) == world.reference.fingerprint


def test_a_held_out_fit_hashes_as_it_did_before_promotion_existed():
    """Unset promotion fields stay out of the hash, so a held-out fit hashes
    exactly as it did before promotion existed (6738aa0).

    Not pinned to a literal digest: the reference is fitted, and its floats
    differ in the last bits across machines (CI shard 3/4 on 798db53 gave
    fc80c781... where the laptop gives 36844796...). The property is pinned
    instead, on the reference's own payload."""
    ref = _reference()
    payload = ref.model_dump(mode="json")
    assert payload["promoted_from"] is None and payload["previous_reference"] is None
    pre_promotion = {
        k: v for k, v in payload.items() if k not in ("promoted_from", "previous_reference")
    }
    assert ref.fingerprint == reference_fingerprint(pre_promotion)
    # A set lineage field does enter the hash.
    assert reference_fingerprint({**payload, "promoted_from": "ckpt-x"}) != ref.fingerprint


def test_the_brain_jobs_module_does_not_import_torch():
    """The brain child shares the digest's address-space cap; torch alone
    took 7.7 s and 4.9 GB of address space there."""
    code = "import sys, src.control.brain_jobs; print('torch' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=300
    )
    assert out.stdout.strip() == "False", out.stderr
