"""B08.5: distinct residuals on one drive connected to investigation evidence.

**Known cases** (labelled synthetic drives generated here, never committed):
each drive holds a complete target-phi twin tuple (``tests/test_target_phi.py``)
and three cruise-then-WOT cycles (``tests/test_delivery_investigation.py``)
in one session, and every residual is minted by its real producer —
:func:`~src.analysis.twin_evidence.evaluate_and_mint` and
:func:`~src.analysis.delivery_evidence.mint_delivery_error` — never typed:

* **wrong ROM**: the twin reads another image's table (cells moved by two
  counts); the measured mixture follows the command.
* **wrong branch**: the pedal flag reads clear while the ECM wrote the
  override word, so the reproduction takes the ordinary branch.
* **injected delivery deviation, correct command**: the twin agrees; the
  measured mixture is four percent lean of the command.
* **missing target data**: neither the target RAM word nor commanded phi
  was captured.

The twin's table is the committed one with the application model's blockers
lifted (as ``tests/test_twin_investigation._qualified``), the same labelled
qualification E02.6's tests use; with the shipped blockers the ECU stage is
``unvalidated`` (tested below). B08 behavior residuals go through the real
:func:`~src.analysis.behavior_residual_evidence.mint_behavior_residual`.

Each drive spec is built and minted once per module (:func:`minted`); every
test works on its own copy of that drive's ledger file.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from src.analysis.behavior_residual_evidence import (
    BehaviorModelIdentity,
    QualificationError,
    RomIdentity,
    mint_behavior_residual,
)
from src.analysis.change_evaluation import TOOL as CHANGE_TOOL
from src.analysis.delivery_evidence import mint_delivery_error
from src.analysis.delivery_investigation import NotEngineResponseError, detect_discrepancy
from src.analysis.ecu_application import (
    MODEL_FILE,
    ApplicationModel,
    load_application_model,
    program_sha256,
)
from src.analysis.fault_model import FAULT_MODEL_FILE, FaultModel, FaultModelError, load_fault_model
from src.analysis.operating_context import ContextProvider, image_tables
from src.analysis.residual_chain import (
    ENGINE_HEALTH_UNKNOWN,
    ContractMismatchError,
    DependencyResolver,
    MixedDrivesError,
    NotAResidualError,
    build_chain,
    dependency_resolver,
    drive_residuals,
    investigate_residual_chain,
)
from src.analysis.target_phi import ResolvedTable, TwinDefinition, table_from_calibration
from src.analysis.tuning_context import TuningContext
from src.analysis.twin_evidence import TwinSessionIdentity, evaluate_and_mint
from src.analysis.twin_investigation import NotEcuCommandError, investigate_command_residual
from src.config import CanaryConfig
from src.evidence import (
    Conditions,
    Evidence,
    ForeignSupersessionError,
    Intervention,
    Investigation,
    MemoryLedger,
    Provenance,
    Quality,
    Quantity,
    Reference,
    Result,
    SourceRef,
    VehicleIdentity,
    investigation_card,
    render_text,
)
from src.evidence.records import Retraction
from src.signals.afr import load_afr_calibration
from src.vehicle.profiles import profile_dir, resolve_profile_for_tooling, scope_config
from tests.rom_fixtures import xterra_map
from tests.test_evidence_cards import _allowed_tokens, _unresolved_numbers
from tests.test_operating_context import context_image, definition
from tests.test_operating_context import resolve as _resolve_context
from tests.test_target_phi import EXPECT, IMAGE_SHA, PROFILE, T0, _definition, _session, _viewer
from tests.test_twin_investigation import REVISION, SLUG, _store

WRONG_IMAGE = "ab" * 32
TWIN_COMMAND_GROUP = "relation:memory:0xFFFF26BE+mode01:0x44"
OPERATOR = Provenance(producer="operator", producer_kind="operator", version="test")


@functools.cache
def _definition_cached() -> tuple[TwinDefinition, str]:
    return _definition()


@functools.cache
def _application_model() -> tuple[ApplicationModel, str]:
    """``load_application_model(PROFILE)`` loaded once, parsed with libyaml
    (the pure-Python loader dominated a worker's first test). Same model:
    ``test_the_cached_application_model_is_the_loaders``."""
    text = (PROFILE / MODEL_FILE).read_bytes()
    loaded = yaml.load(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))  # noqa: S506
    return ApplicationModel.model_validate(loaded), hashlib.sha256(text).hexdigest()


@functools.cache
def _blocked_table() -> ResolvedTable:
    """``tests.test_target_phi._table``: the committed table under the shipped
    application model, from the cached model."""
    return table_from_calibration(
        load_afr_calibration(PROFILE / "afr_calibration.yaml"),
        _definition_cached()[0],
        viewer=_viewer(),
        application_model=_application_model(),
        revision=EXPECT["calibration"]["revision"],
        image_sha256=IMAGE_SHA,
        slot=EXPECT["calibration"]["slot"],
    )


def _synthetic_program(image: bytes) -> tuple[ApplicationModel, str]:
    """``tests.test_operating_context.synthetic_program`` on the cached model."""
    model, sha = _application_model()
    identity = model.program.identity.model_copy(update={"sha256": program_sha256(image, model)})
    program = model.program.model_copy(update={"identity": identity})
    return model.model_copy(update={"program": program}), sha


@functools.cache
def _qualified_table() -> ResolvedTable:
    """``tests.test_twin_investigation._qualified`` without a second model load:
    the blockers lifted and the constants marked checked."""
    base = _blocked_table()
    return ResolvedTable(
        **{
            **base.__dict__,
            "final_command_constants_checked": True,
            "application": dataclasses.replace(base.application, blockers=()),
        }
    )


@functools.cache
def _wrong_rom() -> ResolvedTable:
    base = _qualified_table()
    return ResolvedTable(
        **{
            **base.__dict__,
            "cells": tuple(tuple(cell + 2 for cell in row) for row in base.cells),
            "image_sha256": WRONG_IMAGE,
        }
    )


def _program_id() -> str:
    return _qualified_table().application.program_id


@dataclass
class Vehicle:
    context: TuningContext
    model: FaultModel
    model_sha: str


@pytest.fixture(scope="module")
def vehicle() -> Vehicle:
    base = CanaryConfig.from_yaml(Path("canary.dev.example.yaml"))
    config = scope_config(base, resolve_profile_for_tooling(base, SLUG))
    model, sha = load_fault_model(profile_dir(config) / FAULT_MODEL_FILE)
    return Vehicle(TuningContext.from_config(config), model, sha)


@pytest.fixture(scope="module")
def resolver() -> DependencyResolver:
    return dependency_resolver(PROFILE)


#: The twin tuple's command sample and the end of its gate/identity reads
#: (seconds after ``T0``): the least that brackets the sample with identity
#: reads and steady gates on both sides.
TUPLE_AT, TUPLE_END = 10.0, 20.0
#: The B06 part, after the tuple: idle, one cruise-then-WOT cycle, idle
#: (seconds), Mode 01 at 4 Hz. One six-second WOT pull is enough for B06 to
#: reach a signed verdict (asserted per case below), not a row count.
IDLE_S, CRUISE_S, WOT_S, MODE01_HZ = 5.0, 5.0, 6.0, 4


def _pulls(start: pd.Timestamp, lean_pct: float) -> pd.DataFrame:
    """SYNTHETIC (after ``tests/test_delivery_investigation._synthetic_drive``):
    at WOT the ECM commands phi 1.17 open loop and the measured lambda is
    ``lean_pct`` lean of it; elsewhere closed loop near stoichiometric."""
    rng = np.random.default_rng(7)
    segments = [(IDLE_S, "idle"), (CRUISE_S, "cruise"), (WOT_S, "wot"), (IDLE_S, "idle")]
    rows: list[dict[str, Any]] = []
    t0 = start.timestamp()
    offset = 0.0
    round_id = 0
    for duration, kind in segments:
        steps = int(duration * MODE01_HZ)
        for k in range(steps):
            stamp = t0 + offset + k / MODE01_HZ
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
                {"acquired_at": stamp, "acquired_clock": "kernel", "round_id": round_id,
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
                    {"timestamp": pd.Timestamp(stamp, unit="s", tz="UTC"), "source": "obd",
                     "channel": channel, "value_number": value, "value_text": None,
                     "unit": unit, "metadata_json": meta}
                )  # fmt: skip
        offset += duration
    return pd.DataFrame(rows)


def _drive(
    root: Path,
    name: str,
    *,
    lean_pct: float = 0.0,
    flag: int = 1,
    drop: tuple[str, ...] = (),
) -> Path:
    """SYNTHETIC: a twin tuple at ``T0 + TUPLE_AT`` and one WOT pull after the
    tuple's reads end, stored as one session in the writer's schema."""
    tuple_ = _session(flag=flag, t=TUPLE_AT, end=TUPLE_END)
    pulls = _pulls(T0 + pd.Timedelta(seconds=TUPLE_END + 5), lean_pct)
    frame = pd.concat([tuple_, pulls[tuple_.columns]], ignore_index=True)
    frame = frame[~frame["channel"].isin(drop)]
    frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
    return _store(root, frame, name)


def _twin(ledger: MemoryLedger, session: Path, *, table: ResolvedTable | None = None) -> Evidence:
    definition, sha = _definition_cached()
    chosen = table or _qualified_table()
    minted = evaluate_and_mint(
        ledger,
        session,
        definition=definition,
        definition_sha256=sha,
        identity=TwinSessionIdentity(
            profile=SLUG,
            vin=None,
            calibration=REVISION,
            calibration_source="registry",
            image_sha256=IMAGE_SHA,
        ),
        program_id=chosen.application.program_id,
        table_for_slot=lambda _slot: chosen,
    )
    (evidence_id,) = minted["evidence"]
    return _evidence(ledger, evidence_id)


def _delivery(
    ledger: MemoryLedger, vehicle: Vehicle, session: Path, *, context: TuningContext | None = None
) -> Evidence:
    evidence_id = mint_delivery_error(
        ledger, session, context or vehicle.context, vehicle.model, vehicle.model_sha,
        vin=None, program_id=_program_id(),
    )  # fmt: skip
    return _evidence(ledger, evidence_id)


def _evidence(ledger: MemoryLedger, evidence_id: str) -> Evidence:
    record = ledger.resolve_evidence(evidence_id).record
    assert isinstance(record, Evidence)
    return record


@dataclass
class Case:
    ledger: MemoryLedger
    session: Path
    twin: Evidence
    delivery: Evidence


@dataclass(frozen=True)
class Minted:
    """One drive spec built and minted once per module: its session, and a
    ledger holding the twin and delivery records that each test copies."""

    ledger_path: Path
    session: Path
    twin: str
    delivery: str


#: name -> (twin table, drive kwargs). The ``mixed`` ledger (built beside
#: these) holds ``correct``'s twin record and ``lean``'s delivery record.
DRIVES: dict[str, tuple[Callable[[], ResolvedTable], dict[str, Any]]] = {
    "correct": (_qualified_table, {}),
    "wrong-rom": (_wrong_rom, {}),
    "wrong-branch": (_qualified_table, {"flag": 0}),
    "lean": (_qualified_table, {"lean_pct": 4.0}),
    "blocked": (_blocked_table, {"lean_pct": 4.0}),
    "no-target": (
        _qualified_table,
        {"drop": ("mem:ECM_TARGET_EQUIV_RATIO", "COMMANDED_EQUIV_RATIO")},
    ),
}


class Drives:
    """Drive specs built and minted on first use, then kept for the module.

    Each session is built once and B06 runs once per session (on a scratch
    ledger); its record is re-stated into every drive ledger sharing the
    session, beside that drive's own twin mint. Lazy, so an xdist worker
    builds only the drives its tests use.
    """

    def __init__(self, vehicle: Vehicle, root: Path) -> None:
        self.vehicle = vehicle
        self.root = root
        self.scratch = MemoryLedger(root / "scratch.jsonl", vehicle=SLUG)
        self.sessions: dict[str, tuple[Path, Evidence]] = {}
        self.built: dict[str, Minted] = {}

    def _session(self, name: str, drive: dict[str, Any]) -> tuple[Path, Evidence]:
        key = repr(sorted(drive.items()))
        if key not in self.sessions:
            session = _drive(
                self.root / "sessions", f"_synthetic-chain-{name}-2026-10-04T210000", **drive
            )
            self.sessions[key] = (session, _delivery(self.scratch, self.vehicle, session))
        return self.sessions[key]

    def __getitem__(self, name: str) -> Minted:
        if name not in self.built:
            self.built[name] = self._mixed() if name == "mixed" else self._build(name)
        return self.built[name]

    def _build(self, name: str) -> Minted:
        table, drive = DRIVES[name]
        session, delivery = self._session(name, drive)
        ledger = MemoryLedger(self.root / f"{name}.jsonl", vehicle=SLUG)
        twin = _twin(ledger, session, table=table())
        return Minted(ledger.path, session, twin.id, ledger.append(_copy(delivery)).id)

    def _mixed(self) -> Minted:
        """Two drives in one ledger: the correct drive's twin beside the lean
        drive's delivery record."""
        correct, lean = self["correct"], self["lean"]
        ledger = MemoryLedger(self.root / "mixed.jsonl", vehicle=SLUG)
        twin = _evidence(MemoryLedger(correct.ledger_path, vehicle=SLUG), correct.twin)
        delivery = _evidence(MemoryLedger(lean.ledger_path, vehicle=SLUG), lean.delivery)
        twin_id = ledger.append(_copy(twin)).id
        return Minted(ledger.path, lean.session, twin_id, ledger.append(_copy(delivery)).id)


@pytest.fixture(scope="module")
def minted(vehicle: Vehicle, tmp_path_factory: pytest.TempPathFactory) -> Drives:
    return Drives(vehicle, tmp_path_factory.mktemp("residual-chain"))


def _case(tmp_path: Path, minted: Drives, name: str) -> Case:
    """A private copy of one minted drive's ledger."""
    spec = minted[name]
    path = tmp_path / f"{name}.jsonl"
    shutil.copyfile(spec.ledger_path, path)
    ledger = MemoryLedger(path, vehicle=SLUG)
    return Case(
        ledger, spec.session, _evidence(ledger, spec.twin), _evidence(ledger, spec.delivery)
    )


def _copy(record: Evidence, **changes: Any) -> Evidence:
    """``record`` re-stated with ``changes`` (an unminted record, for appending)."""
    fields = {
        k: getattr(record, k) for k in Evidence.model_fields if k not in ("id", "recorded_at")
    }
    return Evidence(**{**fields, "supersedes": (), "supersession_reason": None, **changes})


# ---------------------------------------------------------------------------
# Known cases


def test_a_wrong_rom_is_an_ecu_computation_residual(
    tmp_path: Path, minted: Drives, resolver: DependencyResolver
) -> None:
    case = _case(tmp_path, minted, "wrong-rom")
    assert case.twin.labels["residual_sign"] == "negative"
    assert case.twin.labels["image_sha256"] == WRONG_IMAGE
    assert case.delivery.labels["sign"] == "0"
    step = investigate_residual_chain(
        case.ledger, [case.twin.id, case.delivery.id], resolver=resolver
    )
    chain = step.chain
    assert chain.outcome == "disagreement"
    assert chain.earliest_disagreement == "ecu_computation"
    assert chain.upstream_unestablished == ()
    assert (chain.stages["ecu_computation"].state, chain.stages["ecu_computation"].direction) == (
        "disagrees",
        "leaner",
    )
    assert chain.stages["command_response"].state == "agrees"
    assert chain.stages["historical"].state == "absent"
    # The ROM each stage read is visible: the twin read the wrong image.
    assert chain.rom_identities["ecu_computation"] == (WRONG_IMAGE,)
    # The twin's residual is the record's, not recomputed.
    assert chain.entry(case.twin.id).residual == case.twin.result.residual
    _assert_card_names_stage_without_a_cause(case.ledger, step.investigation, "ECU computation")


def test_a_wrong_branch_is_an_ecu_computation_residual(
    tmp_path: Path, minted: Drives, resolver: DependencyResolver
) -> None:
    case = _case(tmp_path, minted, "wrong-branch")
    assert case.twin.result.status == "evaluated"
    assert case.twin.values["ordinary_miss"].value == 1
    chain = build_chain(case.ledger, [case.twin.id, case.delivery.id], resolver=resolver)
    assert chain.earliest_disagreement == "ecu_computation"
    assert chain.stages["command_response"].state == "agrees"


def test_a_delivery_deviation_with_a_correct_command_is_a_command_response_residual(
    tmp_path: Path, minted: Drives, vehicle: Vehicle, resolver: DependencyResolver
) -> None:
    case = _case(tmp_path, minted, "lean")
    assert case.twin.labels["residual_sign"] == "zero"
    assert case.delivery.labels["sign"] == "+"
    step = investigate_residual_chain(
        case.ledger, [case.twin.id, case.delivery.id], resolver=resolver,
        fault_model=vehicle.model,
    )  # fmt: skip
    chain = step.chain
    assert chain.earliest_disagreement == "command_response"
    assert chain.stages["ecu_computation"].state == "agrees"
    assert chain.upstream_unestablished == ()
    assert chain.stages["command_response"].direction == "lean"
    # The command is one observation read two ways (PID 0x44 and RAM 0x26BE):
    # common to both stages, named rather than counted as corroboration.
    assert chain.shared_across_stages[TWIN_COMMAND_GROUP] == (
        "ecu_computation",
        "command_response",
    )
    _assert_card_names_stage_without_a_cause(
        case.ledger, step.investigation, "command and response"
    )
    # E03.2's own reading: the hypotheses evaluate_observations leaves
    # consistent with the lean sign, plus the pairing alternative.
    investigation = case.ledger.get(step.investigation)
    assert isinstance(investigation, Investigation)
    labels = {h.label for h in investigation.hypotheses}
    by_name = {h.name: h.label for h in vehicle.model.modelled}
    admitted = ("maf_transfer_error", "fuel_delivery_sag", "lambda_sensor_bias")
    assert {by_name[name] for name in admitted} < labels
    assert by_name["no_fault"] not in labels and by_name["fixed_quantity_error"] not in labels
    # The two loops still refuse each other's evidence.
    with pytest.raises(NotEngineResponseError):
        detect_discrepancy(case.ledger, case.twin.id)
    with pytest.raises(NotEcuCommandError):
        investigate_command_residual(case.ledger, case.delivery.id)


def test_opposite_signs_in_the_command_response_stage_are_refused_by_the_fault_model(
    tmp_path: Path, minted: Drives, vehicle: Vehicle, resolver: DependencyResolver
) -> None:
    case = _case(tmp_path, minted, "lean")
    rich = case.ledger.append(
        _copy(
            case.delivery,
            subject=case.delivery.subject + ":rich-copy",
            labels={**case.delivery.labels, "sign": "-"},
        )
    )
    with pytest.raises(FaultModelError, match="resolve the data conflict"):
        investigate_residual_chain(
            case.ledger, [case.twin.id, case.delivery.id, rich.id], resolver=resolver,
            fault_model=vehicle.model,
        )  # fmt: skip


def test_missing_target_data_is_named_empty_never_a_pass(
    tmp_path: Path, minted: Drives, resolver: DependencyResolver
) -> None:
    case = _case(tmp_path, minted, "no-target")
    assert (case.twin.result.status, case.twin.result.reason) == ("unevaluable", "missing_input")
    assert (case.delivery.result.status, case.delivery.result.reason) == (
        "unevaluable",
        "missing_input",
    )
    step = investigate_residual_chain(
        case.ledger, drive_residuals(case.ledger, case.session.name), resolver=resolver
    )
    chain = step.chain
    assert chain.outcome == "not_established"
    assert chain.earliest_disagreement is None
    for stage in ("ecu_computation", "command_response"):
        verdict = chain.stages[stage]
        assert verdict.state == "unevaluable"
        assert all(r.startswith("missing_input") for r in verdict.reasons.values())
    assert all(e.residual is None for e in chain.entries)
    investigation = case.ledger.get(step.investigation)
    assert isinstance(investigation, Investigation)
    assert investigation.hypotheses == ()
    assert "not established" in investigation.title
    text = render_text(investigation_card(case.ledger, step.investigation)).lower()
    words = set(investigation.title.lower().replace(":", " ").split())
    assert not words & {"agree", "agrees", "agreement", "healthy", "normal", "pass", "passes"}
    assert "missing_input" in text


def test_an_unqualified_delivery_sign_is_diagnostic_only(
    tmp_path: Path, minted: Drives, vehicle: Vehicle, resolver: DependencyResolver
) -> None:
    """B06's residual without a condition-matched floor carries no sign: it is
    diagnostic, never agreement, even beside an agreeing twin."""
    case = _case(tmp_path, minted, "lean")
    no_floor = dataclasses.replace(vehicle.context, noise_floor=None)
    refused = _delivery(case.ledger, vehicle, case.session, context=no_floor)
    assert refused.result.status == "evaluated" and "sign" not in refused.labels
    assert refused.labels["sign_refused"].startswith("no condition-matched floor")
    chain = build_chain(case.ledger, [case.twin.id, refused.id], resolver=resolver)
    verdict = chain.stages["command_response"]
    assert verdict.state == "diagnostic_only"
    assert verdict.reasons[refused.id].startswith("sign_refused")
    assert chain.outcome == "not_established" and chain.earliest_disagreement is None


def test_shipped_blockers_leave_the_upstream_stage_unestablished(
    tmp_path: Path, minted: Drives, resolver: DependencyResolver
) -> None:
    """With the application model's real blockers the twin is ``unvalidated``:
    a lean delivery is still the earliest *supported* disagreement, and the
    card says the ECU stage before it is not established."""
    case = _case(tmp_path, minted, "blocked")
    assert case.twin.result.reason == "unvalidated"
    step = investigate_residual_chain(
        case.ledger, [case.twin.id, case.delivery.id], resolver=resolver
    )
    assert step.chain.earliest_disagreement == "command_response"
    assert step.chain.upstream_unestablished == ("ecu_computation",)
    investigation = case.ledger.get(step.investigation)
    assert isinstance(investigation, Investigation)
    assert any(
        "upstream of the disagreement is not established" in u for u in investigation.unknowns
    )


def _assert_card_names_stage_without_a_cause(
    ledger: MemoryLedger, investigation_id: str, stage_words: str
) -> None:
    investigation = ledger.get(investigation_id)
    assert isinstance(investigation, Investigation)
    assert stage_words in investigation.title
    assert "Earliest supported disagreement" in investigation.title
    # Several alternatives, all open, none credited: no unique cause.
    assert len(investigation.hypotheses) >= 3
    assert all(h.status == "open" for h in investigation.hypotheses)
    assert all(h.supporting == () and h.refuting == () for h in investigation.hypotheses)
    assert ENGINE_HEALTH_UNKNOWN in investigation.unknowns
    card = investigation_card(ledger, investigation_id)
    text = render_text(card)
    assert stage_words in text
    names = {
        card.vehicle,
        card.program_id or "",
        *(k for v in card.what_happened for k in v.labels),
    }
    assert _unresolved_numbers(text, _allowed_tokens(card, ledger) | names) == []


# ---------------------------------------------------------------------------
# B08 residuals: diagnostic-only until qualified, exact ROM and model versions

MODEL = BehaviorModelIdentity(name="throttle_fill_vs_maf", version="0", sha256="c" * 64)


def _rom() -> RomIdentity:
    return RomIdentity(
        image_sha256=IMAGE_SHA,
        program_id=_program_id(),
        revision=REVISION,
        slot=1,
        context_fingerprint="sha256:" + "d" * 64,
    )


def _window() -> tuple[datetime, datetime]:
    start = T0.to_pydatetime()
    return start, start + timedelta(minutes=3)


def _b08(
    ledger: MemoryLedger,
    session: str,
    *,
    model: BehaviorModelIdentity = MODEL,
    residual: float | None = 6.0,
    inputs: tuple[str, ...] = ("mem:ECM_FUEL_COMP_X_INPUT", "obd:MAF"),
    qualification: str | None = None,
    beside: tuple[str, ...] = (),
    rom: RomIdentity | None = None,
) -> str:
    return mint_behavior_residual(
        ledger,
        session=session,
        window=_window(),
        model=model,
        rom=rom if rom is not None else _rom(),
        quantity="fill_vs_maf_pct",
        residual=None if residual is None else Quantity(value=residual, unit="percent"),
        unevaluable=None if residual is not None else ("missing_input", "fill byte not captured"),
        n_pairs=40,
        residual_inputs=inputs,
        regime="closed loop cruise, rpm and fill matched cells",
        fuel_state="closed_loop",
        input_hashes={"samples": "e" * 64},
        detectable=Quantity(value=2.0, unit="percent") if qualification else None,
        qualification=qualification,
        beside=beside,
    )


def _qualify(ledger: MemoryLedger, model: BehaviorModelIdentity, channels: tuple[str, ...]) -> str:
    rom = _rom()
    reference = Reference(
        vehicle=VehicleIdentity(
            profile=SLUG, vin=None, program_id=rom.program_id, calibration=rom.revision
        ),
        provenance=OPERATOR,
        conditions=Conditions(
            regime="held-out drives under the candidate's cells", vehicle_state=None
        ),
        valid_from=datetime(2026, 10, 1, tzinfo=UTC),
        reference_kind="behavior_model",
        name=model.name,
        channels=channels,
        source="behavior_models",
        source_ref=model.key,
        investigation=None,
    )
    return ledger.append(reference).id


def _retire(ledger: MemoryLedger, affects: tuple[str, ...]) -> str:
    """A hardware change after every reference here, affecting ``affects``."""
    return ledger.append(
        Intervention(
            vehicle=VehicleIdentity(profile=SLUG, vin=None, program_id=None, calibration=None),
            provenance=OPERATOR,
            conditions=Conditions(regime="all conditions", vehicle_state=None),
            valid_from=datetime(2026, 10, 5, tzinfo=UTC),
            intervention_kind="hardware",
            title="Throttle body replaced",
            source="none",
            source_ref=None,
            affects=affects,
            effective_precision="day",
        )
    ).id


def test_an_unqualified_b08_residual_is_diagnostic_only(
    tmp_path: Path, minted: Drives, resolver: DependencyResolver
) -> None:
    case = _case(tmp_path, minted, "lean")
    rid = _b08(case.ledger, case.session.name)
    record = _evidence(case.ledger, rid)
    assert (record.result.status, record.result.reason) == ("unevaluable", "unvalidated")
    assert record.result.residual is None
    assert record.values["diagnostic_residual"].value == 6.0
    assert record.labels["qualification"] == "diagnostic_only"
    # Exact ROM and model versions.
    assert record.labels["image_sha256"] == IMAGE_SHA
    assert record.labels["program_id"] == record.vehicle.program_id == _rom().program_id
    assert record.labels["revision"] == record.vehicle.calibration == REVISION
    assert (record.labels["model"], record.labels["model_version"]) == ("throttle_fill_vs_maf", "0")
    assert record.labels["model_sha256"] == "c" * 64
    assert record.labels["context_fingerprint"] == "sha256:" + "d" * 64
    assert record.input_hashes["model"] == "c" * 64
    assert record.input_hashes["rom_image"] == IMAGE_SHA
    assert record.labels["residual_class"] == "historical"
    chain = build_chain(case.ledger, [case.twin.id, case.delivery.id, rid], resolver=resolver)
    assert chain.stages["historical"].state == "diagnostic_only"
    assert chain.earliest_disagreement == "command_response"
    # Neither existing loop accepts it.
    with pytest.raises(NotEngineResponseError):
        detect_discrepancy(case.ledger, rid)
    with pytest.raises(NotEcuCommandError):
        investigate_command_residual(case.ledger, rid)


def test_a_qualified_b08_residual_counts_until_its_reference_is_retired(
    tmp_path: Path, minted: Drives, resolver: DependencyResolver
) -> None:
    case = _case(tmp_path, minted, "correct")
    channels = ("mem:ECM_FUEL_COMP_X_INPUT", "obd:MAF")
    reference = _qualify(case.ledger, MODEL, channels)
    rid = _b08(case.ledger, case.session.name, qualification=reference)
    record = _evidence(case.ledger, rid)
    assert record.result.status == "evaluated" and record.labels["sign"] == "+"
    assert reference in record.provenance.derived_from
    ids = [case.twin.id, case.delivery.id, rid]
    chain = build_chain(case.ledger, ids, resolver=resolver)
    assert chain.earliest_disagreement == "historical"
    assert chain.stages["historical"].direction == "above"
    # A hardware change retires the reference: the residual is diagnostic again.
    _retire(case.ledger, ("obd:MAF",))
    lapsed = build_chain(case.ledger, ids, resolver=resolver)
    assert lapsed.stages["historical"].state == "diagnostic_only"
    assert lapsed.stages["historical"].reasons[rid].startswith("qualification_lapsed")
    # Diagnostic evidence is not agreement either: nothing is established.
    assert (lapsed.outcome, lapsed.earliest_disagreement) == ("not_established", None)


def test_a_qualification_must_name_this_model_version_and_rom(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "m.jsonl", vehicle=SLUG)
    session = "_synthetic-chain-q-2026-10-04T210000"
    other = BehaviorModelIdentity(name="throttle_fill_vs_maf", version="1", sha256="c" * 64)
    reference = _qualify(ledger, other, ("obd:MAF",))
    with pytest.raises(QualificationError, match="not 'throttle_fill_vs_maf@0'"):
        _b08(ledger, session, qualification=reference)
    flashed = RomIdentity(
        image_sha256="f" * 64, program_id=_rom().program_id, revision="ZS44A-20261004",
        slot=1, context_fingerprint=None,
    )  # fmt: skip
    with pytest.raises(QualificationError, match="calibration"):
        _b08(ledger, session, model=other, qualification=reference, rom=flashed)
    evidence = _b08(ledger, session)
    with pytest.raises(QualificationError, match="not a behavior-model reference"):
        _b08(ledger, session, qualification=evidence)
    with pytest.raises(QualificationError, match="does not resolve"):
        _b08(ledger, session, qualification="ref-000000000000000000000000")


def test_without_a_rom_a_b08_residual_is_unknown_context(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "m.jsonl", vehicle=SLUG)
    rid = mint_behavior_residual(
        ledger, session="_synthetic-chain-r-2026-10-04T210000", window=_window(), model=MODEL,
        rom=None, quantity="fill_vs_maf_pct", residual=Quantity(value=6.0, unit="percent"),
        n_pairs=40, residual_inputs=("obd:MAF",), regime="closed loop cruise",
        fuel_state="closed_loop", input_hashes={"samples": "e" * 64},
    )  # fmt: skip
    record = _evidence(ledger, rid)
    assert (record.result.reason, record.labels["image_sha256"]) == ("unknown_context", "unknown")
    with pytest.raises(ValueError, match="fuel_state"):
        mint_behavior_residual(
            ledger, session="s", window=_window(), model=MODEL, rom=_rom(), quantity="q",
            residual=Quantity(value=1.0, unit="1"), n_pairs=1, residual_inputs=("obd:MAF",),
            regime="r", fuel_state="open_loop", input_hashes={"samples": "e" * 64},
        )  # fmt: skip


def test_the_cached_application_model_is_the_loaders() -> None:
    assert _application_model() == load_application_model(PROFILE)


def test_the_rom_identity_comes_from_the_operating_context() -> None:
    image = context_image()
    model = _synthetic_program(image)
    resolved = _resolve_context(image, model=model)
    tables = image_tables(
        image,
        xterra_map(),
        definition(),
        application_model=model,
        expected_sha256=hashlib.sha256(image).hexdigest(),
    )
    provider = ContextProvider.from_profile(
        PROFILE, rom_map=xterra_map(), application_model=model, tables=tables
    )
    fingerprint = provider.contract("serve")["fingerprint"]
    rom = RomIdentity.from_context(resolved, contract_fingerprint=fingerprint)
    assert rom.image_sha256 == hashlib.sha256(image).hexdigest()
    assert (rom.program_id, rom.slot) == (resolved.program_id, 1)
    assert rom.context_fingerprint == fingerprint


# ---------------------------------------------------------------------------
# Historical stage: change detection against a frozen reference

FINGERPRINT = "7" * 64
CHANGE_CHANNELS = ("obd:SHORT_FUEL_TRIM_1", "obd:SHORT_FUEL_TRIM_2")


def _change(
    ledger: MemoryLedger,
    sessions: str,
    *,
    subject: str,
    labels: dict[str, str],
    residual: float | None = None,
) -> str:
    """A change-detection record as ``change_evaluation`` mints it."""
    start = T0.to_pydatetime()
    return ledger.append(
        Evidence(
            vehicle=VehicleIdentity(
                profile=SLUG, vin=None, program_id=_program_id(), calibration=REVISION
            ),
            provenance=Provenance(
                producer=CHANGE_TOOL, producer_kind="tool", version="2", code_version="0" * 64
            ),
            conditions=Conditions(regime="closed loop, warm", vehicle_state=None),
            valid_from=start,
            valid_to=start + timedelta(minutes=3),
            subject=subject,
            incident="change:stft_bank_balance",
            sources=(
                SourceRef(
                    session=sessions,
                    window_start=start,
                    window_end=start + timedelta(minutes=3),
                    channels=CHANGE_CHANNELS,
                    n_samples=30,
                ),
            ),
            input_hashes={"samples": "e" * 64},
            policy={"policy": "frozen"},
            values={"blocks": Quantity(value=6.0, unit="1")},
            labels=labels,
            quality=Quality(n_observations=6, timestamp_basis="acquired"),
            result=Result(
                status="evaluated",
                residual=None if residual is None else Quantity(value=residual, unit="percent"),
            ),
        )
    ).id


def _change_reference(ledger: MemoryLedger) -> str:
    return ledger.append(
        Reference(
            vehicle=VehicleIdentity(
                profile=SLUG, vin=None, program_id=_program_id(), calibration=REVISION
            ),
            provenance=Provenance(
                producer=CHANGE_TOOL, producer_kind="tool", version="2", code_version="0" * 64
            ),
            conditions=Conditions(regime="closed loop, warm", vehicle_state=None),
            valid_from=datetime(2026, 10, 1, tzinfo=UTC),
            reference_kind="evidence",
            name="stft_bank_balance",
            channels=CHANGE_CHANNELS,
            source="memory",
            source_ref=f"change_reference:{FINGERPRINT}",
            investigation=None,
        )
    ).id


def test_drive_residuals_returns_only_what_the_chain_accepts(
    tmp_path: Path, minted: Drives, resolver: DependencyResolver
) -> None:
    case = _case(tmp_path, minted, "correct")
    name = case.session.name
    _change_reference(case.ledger)
    observed = {"evidence_class": "engine_response", "data_origin": "observed",
                "residual": "stft_bank_balance", "reference": FINGERPRINT[:16]}  # fmt: skip
    # A drive change detection pooled with another session (comma-joined source).
    pooled = _change(
        case.ledger,
        f"{name},2026-10-04T230000",
        subject="change_detection:observed",
        labels=observed,
    )
    # E03.3's policy freeze carries no evidence class; a synthetic shift is a test.
    frozen = _change(
        case.ledger, name, subject="change_detection:policy",
        labels={"kind": "policy_frozen", "residual": "stft_bank_balance"},
    )  # fmt: skip
    synthetic = _change(
        case.ledger, name, subject="change_detection:synthetic",
        labels={**observed, "data_origin": "synthetic"},
    )  # fmt: skip
    found = drive_residuals(case.ledger, name)
    assert set(found) == {case.twin.id, case.delivery.id, pooled}
    assert frozen not in found and synthetic not in found
    step = investigate_residual_chain(case.ledger, found, resolver=resolver)
    assert step.chain.stages["historical"].state == "agrees"
    # The refusals drive_residuals mirrors.
    with pytest.raises(NotAResidualError, match="synthetic"):
        build_chain(case.ledger, [synthetic], resolver=resolver)
    with pytest.raises(NotAResidualError, match="no evidence class"):
        build_chain(case.ledger, [frozen], resolver=resolver)


def test_a_change_alarm_counts_only_while_its_reference_stands(
    tmp_path: Path, minted: Drives, resolver: DependencyResolver
) -> None:
    case = _case(tmp_path, minted, "correct")
    alarm_labels = {"kind": "change_alarm", "evidence_class": "engine_response",
                    "data_origin": "observed", "direction": "up",
                    "reference": FINGERPRINT[:16]}  # fmt: skip
    alarm = _change(
        case.ledger, case.session.name, subject="change_alarm:x", labels=alarm_labels, residual=2.0
    )
    ids = [case.twin.id, case.delivery.id, alarm]
    unrecorded = build_chain(case.ledger, ids, resolver=resolver)
    assert unrecorded.stages["historical"].state == "diagnostic_only"
    assert unrecorded.stages["historical"].reasons[alarm].startswith("reference_lapsed")
    _change_reference(case.ledger)
    chain = build_chain(case.ledger, ids, resolver=resolver)
    assert (chain.earliest_disagreement, chain.stages["historical"].direction) == (
        "historical",
        "up",
    )
    _retire(case.ledger, ("obd:SHORT_FUEL_TRIM_1",))
    lapsed = build_chain(case.ledger, ids, resolver=resolver)
    assert lapsed.stages["historical"].state == "diagnostic_only"
    assert "retired by intervention" in lapsed.stages["historical"].reasons[alarm]
    assert lapsed.outcome == "not_established"


# ---------------------------------------------------------------------------
# Learner updates cannot remove or renormalize the twin discrepancy


def test_a_learned_correction_cannot_absorb_the_twin_discrepancy(
    tmp_path: Path, minted: Drives, resolver: DependencyResolver
) -> None:
    case = _case(tmp_path, minted, "wrong-rom")
    first = investigate_residual_chain(
        case.ledger, [case.twin.id, case.delivery.id], resolver=resolver
    )
    # A learned offset around the decoded prediction absorbs the departure:
    # its residual is zero. It is a separate, versioned historical record.
    offset = BehaviorModelIdentity(name="phi_offset_learner", version="3", sha256="9" * 64)
    reference = _qualify(case.ledger, offset, ("mem:ECM_TARGET_EQUIV_RATIO",))
    learned = _b08(
        case.ledger, case.session.name, model=offset, residual=0.1, qualification=reference,
        inputs=("mem:ECM_TARGET_EQUIV_RATIO",), beside=(case.twin.id,),
    )  # fmt: skip
    assert case.twin.id in _evidence(case.ledger, learned).provenance.derived_from
    updated = investigate_residual_chain(case.ledger, [learned], resolver=resolver)
    chain = updated.chain
    # The twin record still stands in its stage, with its own residual.
    assert chain.earliest_disagreement == "ecu_computation"
    assert chain.stages["ecu_computation"].counted == (case.twin.id,)
    assert chain.entry(case.twin.id).residual == case.twin.result.residual
    assert chain.stages["historical"].state == "agrees"
    investigation = case.ledger.get(updated.investigation)
    assert isinstance(investigation, Investigation)
    assert investigation.supersedes == (first.investigation,)
    assert {case.twin.id, case.delivery.id, learned} <= set(investigation.observations)

    # Renormalizing it under the twin's own subject: the ledger refuses the append.
    learner = Provenance(
        producer="phi_offset_learner", producer_kind="tool", version="3", code_version="0" * 64
    )
    zeroed = {**case.twin.values, "max_qualified_residual_phi": Quantity(value=0.0, unit="1")}
    size = len(case.ledger.entries())
    with pytest.raises(ForeignSupersessionError, match="phi_offset_learner"):
        case.ledger.append(_copy(case.twin, provenance=learner, values=zeroed))
    # Nor under another subject that names the twin record as superseded.
    with pytest.raises(ForeignSupersessionError, match="phi_offset_learner"):
        case.ledger.append(
            _copy(
                case.twin,
                provenance=learner,
                subject="phi_offset_learner:renormalized",
                values=zeroed,
                supersedes=(case.twin.id,),
                supersession_reason="absorbed by the learned offset",
            )
        )
    assert len(case.ledger.entries()) == size
    assert case.ledger.resolve_evidence(case.twin.id).current


def test_only_the_operator_retracts_another_producers_evidence(
    tmp_path: Path, minted: Drives, resolver: DependencyResolver
) -> None:
    case = _case(tmp_path, minted, "wrong-rom")
    step = investigate_residual_chain(
        case.ledger, [case.twin.id, case.delivery.id], resolver=resolver
    )
    tool = Provenance(
        producer="adaptive_candidate", producer_kind="tool", version="1", code_version="0" * 64
    )
    with pytest.raises(ForeignSupersessionError, match="adaptive_candidate"):
        case.ledger.append(
            Retraction(
                vehicle=case.twin.vehicle,
                provenance=tool,
                valid_from=case.twin.valid_from,
                retracts=(case.twin.id,),
                reason="absorbed by the learned offset",
            )
        )
    # The operator withdrawing a record is not a rewrite. It is no longer
    # evidence, and a later update of the drive's investigation drops it and
    # says so instead of failing.
    case.ledger.append(
        Retraction(
            vehicle=case.delivery.vehicle,
            provenance=OPERATOR,
            valid_from=case.delivery.valid_from,
            retracts=(case.delivery.id,),
            reason="wrong session pulled",
        )
    )
    with pytest.raises(NotAResidualError, match="retracted"):
        build_chain(case.ledger, [case.delivery.id], resolver=resolver)
    updated = investigate_residual_chain(case.ledger, [case.twin.id], resolver=resolver)
    investigation = case.ledger.get(updated.investigation)
    assert isinstance(investigation, Investigation)
    assert investigation.supersedes == (step.investigation,)
    assert investigation.observations == (case.twin.id,)
    assert any("operator withdrew" in u for u in investigation.unknowns)
    assert case.delivery.id in investigation.narrative
    assert updated.chain.stages["command_response"].state == "absent"


# ---------------------------------------------------------------------------
# Alias and common-input evidence is not doubled


@pytest.mark.parametrize(
    ("first", "second", "dependent"),
    [
        # One register read two ways (Mode 01 PID 0x07 and CAN DID 0x1125).
        (("obd:LONG_FUEL_TRIM_1",), ("did:ECM_LONG_FUEL_TRIM_B1",), True),
        # A derived channel and its own input.
        (("derived:AIR_MASS_PER_CYCLE",), ("obd:MAF",), True),
        # Controls: another term of the same bank, and the road-speed register
        # DID 0x1102 was once catalogued as trim under.
        (("obd:LONG_FUEL_TRIM_1",), ("obd:SHORT_FUEL_TRIM_1",), False),
        (("obd:LONG_FUEL_TRIM_1",), ("did:ECM_VEHICLE_SPEED_COARSE",), False),
    ],
)
def test_alias_and_common_input_evidence_is_counted_once(
    tmp_path: Path,
    resolver: DependencyResolver,
    first: tuple[str, ...],
    second: tuple[str, ...],
    dependent: bool,
) -> None:
    ledger = MemoryLedger(tmp_path / "m.jsonl", vehicle=SLUG)
    session = "_synthetic-chain-alias-2026-10-04T210000"
    a = BehaviorModelIdentity(name="model_a", version="1", sha256="1" * 64)
    b = BehaviorModelIdentity(name="model_b", version="1", sha256="2" * 64)
    ra = _b08(ledger, session, model=a, inputs=first, qualification=_qualify(ledger, a, first))
    rb = _b08(ledger, session, model=b, inputs=second, qualification=_qualify(ledger, b, second))
    chain = build_chain(ledger, [ra, rb], resolver=resolver)
    verdict = chain.stages["historical"]
    assert verdict.state == "disagrees"
    if dependent:
        assert len(verdict.counted) == 1 and len(verdict.dependent) == 1
        (other,) = verdict.dependent
        assert chain.entry(other).dependent_on == verdict.counted[0]
    else:
        assert set(verdict.counted) == {ra, rb} and verdict.dependent == ()


def test_dependency_groups_resolve_through_the_profiles_aliases(
    resolver: DependencyResolver,
) -> None:
    groups, unresolved = resolver.groups(["obd:LONG_FUEL_TRIM_1", "did:ECM_LONG_FUEL_TRIM_B1"])
    assert groups == {"register:0xFFFF26DE:fuel_trim_long_b1"}
    assert unresolved == ()
    groups, _ = resolver.groups(["obd:COMMANDED_EQUIV_RATIO"])
    assert groups == resolver.groups(["mem:ECM_TARGET_EQUIV_RATIO"])[0] == {TWIN_COMMAND_GROUP}
    groups, _ = resolver.groups(["derived:AIR_MASS_PER_CYCLE"])
    assert "mode01:0x10" in groups  # MAF
    groups, unresolved = resolver.groups(["obd:NOT_A_PID"])
    assert groups == {"channel:obd:NOT_A_PID"} and unresolved == ("obd:NOT_A_PID",)


# ---------------------------------------------------------------------------
# Refusals


def test_records_outside_the_chain_or_from_another_drive_are_refused(
    tmp_path: Path, minted: Drives, resolver: DependencyResolver
) -> None:
    mixed = _case(tmp_path, minted, "mixed")
    with pytest.raises(MixedDrivesError):
        build_chain(mixed.ledger, [mixed.twin.id, mixed.delivery.id], resolver=resolver)
    one = _case(tmp_path, minted, "correct")
    foreign = one.ledger.append(
        _copy(
            one.delivery,
            subject="compare_channels:x",
            provenance=Provenance(
                producer="compare_channels",
                producer_kind="tool",
                version="1",
                code_version="0" * 64,
            ),  # fmt: skip
        )
    )
    with pytest.raises(NotAResidualError, match="compare_channels"):
        build_chain(one.ledger, [foreign.id], resolver=resolver)
    relabelled = one.ledger.append(
        _copy(
            one.twin,
            subject="target_phi_twin:relabelled",
            labels={**one.twin.labels, "evidence_class": "engine_response"},
        )
    )
    with pytest.raises(NotAResidualError, match="ecu_command"):
        build_chain(one.ledger, [relabelled.id], resolver=resolver)
    off_region = one.ledger.append(
        _copy(
            one.delivery,
            subject="lambda_delivery_error:off-region",
            labels={**one.delivery.labels, "region": "cruise"},
        )
    )
    with pytest.raises(ContractMismatchError, match="cruise"):
        build_chain(one.ledger, [off_region.id], resolver=resolver)
    assert one.delivery.result.residual is not None
    off_unit = one.ledger.append(
        _copy(
            one.delivery,
            subject="lambda_delivery_error:off-unit",
            result=Result(
                status="evaluated",
                residual=Quantity(value=one.delivery.result.residual.value / 100, unit="1"),
            ),
        )
    )
    with pytest.raises(ContractMismatchError, match="'percent'"):
        build_chain(one.ledger, [off_unit.id], resolver=resolver)


def test_fuel_pressure_droop_stands_in_no_stage(
    tmp_path: Path, minted: Drives, resolver: DependencyResolver
) -> None:
    one = _case(tmp_path, minted, "correct")
    droop = one.ledger.append(
        _copy(
            one.delivery,
            subject="fuel_pressure_droop:x",
            provenance=one.delivery.provenance.model_copy(
                update={"producer": "fuel_pressure_droop"}
            ),
        )
    )
    with pytest.raises(NotAResidualError, match="separates the command/response"):
        build_chain(one.ledger, [droop.id], resolver=resolver)
