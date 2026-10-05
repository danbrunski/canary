"""E02.6: command-change predictions (what-ifs) from the reproduced target logic.

Committed vectors only: the transcribed Fuel Targets tables of ZS44A-k106,
ZS44A-20260920 and ZS44A-20261004 (``afr_calibration.yaml``, image hashes
from ``tune_revisions.yaml``) and the Fuel Targets editor's known edit
(``rom-targets-known-edit.json``, the operator's k106 -> 20260920 cell). The
real images are exercised separately in
``tests/test_twin_command_change_private_rom.py``, which skips by name.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.analysis.delivery_investigation import detect_discrepancy
from src.analysis.rom_targets import Patch
from src.analysis.target_phi import ResolvedTable, TwinDefinition, table_from_calibration
from src.analysis.twin_command_change import (
    WHAT_IF_QUANTITY,
    CommandChangeRefusalError,
    ProposedChange,
    Region,
    change_from_cell_edits,
    change_from_images,
    command_change_order,
    predict_command_change,
    record_command_change,
)
from src.evidence import (
    Conditions,
    Hypothesis,
    Intervention,
    Investigation,
    MemoryLedger,
    MemoryReferenceError,
    Outcome,
    Prediction,
    Provenance,
    Result,
    VehicleIdentity,
    investigation_card,
    render_text,
)
from src.signals.afr import load_afr_calibration
from src.vehicle.tune_revisions import TuneRevisionRegistry, load_tune_revisions
from tests.test_target_phi import PROFILE, _definition, _model, _viewer

SLUG = "nissan-xterra-2008"
PATCH = PROFILE / "rom-targets-known-edit.json"
T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _registry() -> TuneRevisionRegistry:
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    return registry


def _image(revision: str) -> str:
    sha = next(r.image_sha256 for r in _registry().revisions if r.revision == revision)
    assert sha is not None
    return sha


def _transcribed(revision: str) -> ResolvedTable:
    definition, _ = _definition()
    return table_from_calibration(
        load_afr_calibration(PROFILE / "afr_calibration.yaml"),
        definition,
        viewer=_viewer(),
        application_model=_model(),
        revision=revision,
        image_sha256=_image(revision),
        slot=1,
    )


def _patch(**edit: Any) -> Patch:
    raw = json.loads(PATCH.read_text())
    raw["changes"][0].update(edit)
    return Patch.model_validate(raw)


def _vehicle(table: ResolvedTable) -> VehicleIdentity:
    return VehicleIdentity(
        profile=SLUG, vin=None, program_id=table.application.program_id, calibration=table.revision
    )


def _definition_only() -> TwinDefinition:
    return _definition()[0]


def _with_cells(table: ResolvedTable, edits: dict[tuple[int, int], int]) -> ResolvedTable:
    cells = [list(row) for row in table.cells]
    for (r, c), value in edits.items():
        cells[r][c] = value
    return ResolvedTable(
        **{
            **table.__dict__,
            "cells": tuple(tuple(row) for row in cells),
            "image_sha256": "f" * 64,
            "revision": "synthetic",
        }
    )


# -- the computation ----------------------------------------------------------------


def test_a_cell_edit_set_predicts_exactly_the_cell_it_changes() -> None:
    definition = _definition_only()
    k106 = _transcribed("ZS44A-k106")
    change = change_from_cell_edits(
        k106,
        Patch.model_validate_json(PATCH.read_text()),
        registry=_registry(),
        definition=definition,
    )
    assert change.changed_cells() == [(7, 4)]
    assert change.target_image_sha256 is None and change.change_set_sha256 is not None
    (only,) = predict_command_change(change, definition)
    assert (only.region.key(), only.direction) == ("ordinary:r7-7:c4-4", "increase")
    (point,) = only.points
    # 0xA2 -> 0xA3 at the cell's own breakpoints: phi = cell / 128, exactly.
    assert point.before is not None and point.after is not None
    assert (point.before.value, point.after.value) == (0xA2 / 128, 0xA3 / 128)
    assert only.magnitude is not None and only.magnitude.value == 1 / 128
    # Stated raw inputs: rpm word = axis byte 0x68 x 4, load byte = axis 0x6C.
    assert {k: q.value for k, q in point.inputs.items()} == {
        "rpm_word": 0x68 * 4,
        "load_input": 0x6C,
        "pedal_flag": 0,
    }
    # The edit set and the flashed image's transcription give the same table.
    assert change.target.cells == _transcribed("ZS44A-20260920").cells


def test_the_image_pair_and_the_cell_edit_set_agree() -> None:
    definition = _definition_only()
    k106, r920 = _transcribed("ZS44A-k106"), _transcribed("ZS44A-20260920")
    by_images = predict_command_change(change_from_images(k106, r920), definition)
    by_edits = predict_command_change(
        change_from_cell_edits(
            k106,
            Patch.model_validate_json(PATCH.read_text()),
            registry=_registry(),
            definition=definition,
        ),
        definition,
    )
    assert [(p.region, p.points, p.direction) for p in by_images] == [
        (p.region, p.points, p.direction) for p in by_edits
    ]


def test_a_change_outside_the_target_table_predicts_no_change_at_equal_inputs() -> None:
    """The Fuel Comp restore (ZS44A-20261004) left Target AFR byte-identical:
    at equal lookup inputs the command cannot change. What it changed acts on
    the load input itself, which the what-if states rather than predicts, and
    without the images' bytes it says the rest is unknown."""
    definition = _definition_only()
    change = change_from_images(_transcribed("ZS44A-20260920"), _transcribed("ZS44A-20261004"))
    regions = predict_command_change(change, definition)
    assert [(r.region.key(), r.direction) for r in regions] == [
        ("ordinary:r0-7:c0-7", "no_change"),
        ("override:r0-7", "no_change"),
    ]
    assert [len(r.points) for r in regions] == [64, 8]
    assert all(p.change == 0 for r in regions for p in r.points)
    (unknown,) = change.not_reproduced
    assert unknown.startswith("unknown:")


def test_a_point_in_the_enrichment_gate_band_is_named_not_valued() -> None:
    definition = _definition_only()
    base = _transcribed("ZS44A-20260920")
    banded = _with_cells(base, {(0, 0): 129})
    change = ProposedChange(base, banded, banded.image_sha256, None, None, ())
    with pytest.raises(CommandChangeRefusalError, match="no_evaluable_point"):
        predict_command_change(change, definition, [Region((0, 0), (0, 0), "ordinary")])
    mixed = predict_command_change(change, definition, [Region((0, 0), (0, 1), "ordinary")])
    named = [p for p in mixed[0].points if p.reason]
    assert [(p.key, p.reason, p.before, p.after) for p in named] == [
        ("r0c0", "enrichment_gate_state_unknown", None, None)
    ]


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda b, d: change_from_images(b, b), "no_change"),
        (
            lambda b, d: change_from_images(
                b, ResolvedTable(**{**b.__dict__, "slot": 2, "image_sha256": "e" * 64})
            ),
            "table_mismatch",
        ),
        (
            lambda b, d: change_from_images(
                b,
                ResolvedTable(
                    **{
                        **b.__dict__,
                        "image_sha256": "e" * 64,
                        "row_breakpoints": (0, *b.row_breakpoints[1:]),
                    }
                ),
            ),
            "axis_changed",
        ),
        (
            lambda b, d: change_from_cell_edits(
                _transcribed("ZS44A-20260920"), _patch(), registry=_registry(), definition=d
            ),
            "patch_source_mismatch",
        ),
        (
            lambda b, d: change_from_cell_edits(
                b, _patch(old_raw=0xA1), registry=_registry(), definition=d
            ),
            "old_value_mismatch",
        ),
        (
            lambda b, d: change_from_cell_edits(
                b, _patch(new_raw=0xA2), registry=_registry(), definition=d
            ),
            "no_op_edit",
        ),
        (
            lambda b, d: change_from_cell_edits(
                b, _patch(row=8), registry=_registry(), definition=d
            ),
            "cell_outside_table",
        ),
        (
            lambda b, d: change_from_cell_edits(
                b,
                Patch.model_validate(
                    {
                        **json.loads(PATCH.read_text()),
                        "changes": json.loads(PATCH.read_text())["changes"] * 2,
                    }
                ),
                registry=_registry(),
                definition=d,
            ),
            "duplicate_edit",
        ),
        (
            lambda b, d: change_from_cell_edits(
                b, _patch().model_copy(update={"map_slot": 2}), registry=_registry(), definition=d
            ),
            "table_mismatch",
        ),
    ],
)
def test_an_inexact_change_is_refused_by_name(make: Any, reason: str) -> None:
    with pytest.raises(CommandChangeRefusalError) as caught:
        make(_transcribed("ZS44A-k106"), _definition_only())
    assert caught.value.reason == reason


def test_a_region_that_rises_and_falls_must_be_split() -> None:
    definition = _definition_only()
    base = _transcribed("ZS44A-20260920")
    target = _with_cells(base, {(7, 0): base.cells[7][0] + 2, (7, 1): base.cells[7][1] - 2})
    change = ProposedChange(base, target, target.image_sha256, None, None, ())
    with pytest.raises(CommandChangeRefusalError, match="mixed_direction"):
        predict_command_change(change, definition, [Region((7, 7), (0, 1), "ordinary")])
    split = predict_command_change(change, definition)
    assert [(r.region.key(), r.direction) for r in split] == [
        ("ordinary:r7-7:c0-0", "increase"),
        ("ordinary:r7-7:c1-1", "decrease"),
    ]
    # A region whose changes differ in size keeps every point, and no single magnitude.
    wide = _with_cells(base, {(7, 0): base.cells[7][0] + 2, (7, 1): base.cells[7][1] + 1})
    (both,) = predict_command_change(
        ProposedChange(base, wide, wide.image_sha256, None, None, ()),
        definition,
        [Region((7, 7), (0, 1), "ordinary")],
    )
    assert both.direction == "increase" and both.magnitude is None
    # Zeros count: a changed cell beside an unchanged one has no one magnitude.
    (partly,) = predict_command_change(
        ProposedChange(base, target, target.image_sha256, None, None, ()),
        definition,
        [Region((6, 7), (0, 0), "ordinary")],
    )
    assert [p.change for p in partly.points] == [0.0, 2 / 128]
    assert partly.direction == "increase" and partly.magnitude is None


def test_an_override_row_change_is_predicted_on_the_override_branch() -> None:
    definition = _definition_only()
    base = _transcribed("ZS44A-20260920")
    target = _with_cells(base, {(3, 7): base.cells[3][7] + 1})
    change = ProposedChange(base, target, target.image_sha256, None, None, ())
    keys = [(r.region.key(), r.direction) for r in predict_command_change(change, definition)]
    assert keys == [("ordinary:r3-3:c7-7", "increase"), ("override:r3-3", "increase")]
    (override,) = [
        r for r in predict_command_change(change, definition) if r.region.branch == "override"
    ]
    (point,) = override.points
    assert point.inputs["pedal_flag"].value == 1 << definition.inputs.forced_column_flag.bit
    assert point.inputs["load_input"].value == (1 << definition.inputs.load.bits) - 1


# -- the record ---------------------------------------------------------------------


def _ledger(tmp_path: Path) -> MemoryLedger:
    return MemoryLedger(tmp_path / "memory.jsonl", vehicle=SLUG)


def _flash(ledger: MemoryLedger, image_sha256: str, at: datetime, now: datetime) -> Intervention:
    stored = ledger.append(
        Intervention(
            vehicle=VehicleIdentity(
                profile=SLUG, vin=None, program_id=None, calibration="ZS44A-20260920"
            ),
            provenance=Provenance(producer="operator", producer_kind="operator", version="1"),
            conditions=Conditions(regime="all conditions", vehicle_state="ignition_only"),
            valid_from=at,
            intervention_kind="calibration",
            title="Fuel Targets edit",
            source="none",
            source_ref=None,
            affects=("mem:ECM_TARGET_EQUIV_RATIO",),
            effective_precision="instant",
            image_sha256=image_sha256,
        ),
        now=now,
    )
    assert isinstance(stored, Intervention)
    return stored


def test_a_what_if_is_a_labelled_prediction_recorded_before_the_flash(tmp_path: Path) -> None:
    definition = _definition_only()
    ledger = _ledger(tmp_path)
    k106, r920 = _transcribed("ZS44A-k106"), _transcribed("ZS44A-20260920")
    _flash(ledger, k106.image_sha256, T0 - timedelta(days=30), now=T0 - timedelta(minutes=1))
    (pid,) = record_command_change(
        ledger, change_from_images(k106, r920), definition, vehicle=_vehicle(k106), now=T0
    )
    prediction = ledger.get(pid)
    assert isinstance(prediction, Prediction) and prediction.what_if is not None
    assert prediction.intervention is None
    assert prediction.kind == "prediction" and prediction.quantity == WHAT_IF_QUANTITY
    assert prediction.what_if.label == "what_if"
    assert prediction.what_if.base_image_sha256 == k106.image_sha256
    assert prediction.what_if.target_image_sha256 == r920.image_sha256
    assert prediction.what_if.blockers == k106.application.blockers
    assert prediction.direction == "increase" and prediction.noise_floor is None
    assert prediction.conditions.bounds["rpm_word_min"].value == 0x68 * 4
    assert prediction.vehicle.calibration == "ZS44A-k106"
    # No flash yet: no order to derive. The flash then lands after it: prior.
    assert command_change_order(ledger, pid) is None
    _flash(ledger, r920.image_sha256, T0 + timedelta(hours=2), now=T0 + timedelta(hours=3))
    assert command_change_order(ledger, pid) == "prior"
    # Recorded after that flash (made since the base became current), the
    # same what-if names it and is post-hoc.
    (late,) = record_command_change(
        ledger,
        change_from_images(k106, r920),
        definition,
        vehicle=_vehicle(k106),
        now=T0 + timedelta(hours=4),
    )
    late_record = ledger.get(late)
    assert isinstance(late_record, Prediction) and late_record.intervention is not None
    assert command_change_order(ledger, late) == "post_hoc"


def test_re_flashing_a_previous_image_is_not_that_flash_again(tmp_path: Path) -> None:
    """Restore: base ZS44A-20261004, target the earlier ZS44A-20260920. Its
    September flash precedes the base, so it is not this change: the what-if
    stays unlinked until a flash of that image *after* it is recorded."""
    definition = _definition_only()
    ledger = _ledger(tmp_path)
    r920, r1004 = _transcribed("ZS44A-20260920"), _transcribed("ZS44A-20261004")
    september = _flash(ledger, r920.image_sha256, T0 - timedelta(days=10), now=T0)
    _flash(ledger, r1004.image_sha256, T0 + timedelta(days=3), now=T0 + timedelta(days=3))
    restore = change_from_images(r1004, r920)
    investigation = ledger.append(
        Investigation(
            vehicle=_vehicle(r1004),
            provenance=Provenance(producer="operator", producer_kind="operator", version="1"),
            conditions=Conditions(regime="all conditions", vehicle_state=None),
            valid_from=T0,
            title="Restore the September calibration",
            question="does the restore change the command at equal inputs",
        ),
        now=T0 + timedelta(days=4),
    )
    ids = record_command_change(
        ledger,
        restore,
        definition,
        vehicle=_vehicle(r1004),
        investigation=investigation.id,
        now=T0 + timedelta(days=5),
    )
    assert len(ids) == 2  # whole table, both branches: no change at equal inputs
    for pid in ids:
        record = ledger.get(pid)
        assert isinstance(record, Prediction) and record.intervention is None
        assert command_change_order(ledger, pid) is None
    card = investigation_card(ledger, investigation.id)
    assert {v.what_if.order for v in card.predictions if v.what_if} == {None}
    assert all(
        v.what_if is not None and "no flash of the target image" in v.what_if.order_note
        for v in card.predictions
    )
    flashed = _flash(ledger, r920.image_sha256, T0 + timedelta(days=6), now=T0 + timedelta(days=6))
    assert {command_change_order(ledger, pid) for pid in ids} == {"prior"}
    assert ledger.what_if_intervention(ids[0]) == flashed.id != september.id
    card = investigation_card(ledger, investigation.id)
    for view in card.predictions:
        assert view.what_if is not None
        assert (view.what_if.order, view.what_if.ordered_against) == ("prior", flashed.id)
        assert (view.order, view.intervention) == ("prior", flashed.id)
    assert f"ordered against flash {flashed.id}: prior" in render_text(card)


def test_a_named_intervention_must_install_the_target(tmp_path: Path) -> None:
    definition = _definition_only()
    ledger = _ledger(tmp_path)
    k106, r920 = _transcribed("ZS44A-k106"), _transcribed("ZS44A-20260920")
    other = _flash(ledger, k106.image_sha256, T0 + timedelta(days=1), now=T0)
    right = _flash(ledger, r920.image_sha256, T0 + timedelta(days=2), now=T0)
    with pytest.raises(CommandChangeRefusalError) as caught:
        record_command_change(
            ledger,
            change_from_images(k106, r920),
            definition,
            vehicle=_vehicle(k106),
            intervention=other.id,
            now=T0 + timedelta(minutes=1),
        )
    assert caught.value.reason == "intervention_mismatch"
    (pid,) = record_command_change(
        ledger,
        change_from_images(k106, r920),
        definition,
        vehicle=_vehicle(k106),
        intervention=right.id,
        now=T0 + timedelta(minutes=1),
    )
    assert command_change_order(ledger, pid) == "prior"


def test_a_what_if_cannot_stand_where_measurements_go(tmp_path: Path) -> None:
    definition = _definition_only()
    ledger = _ledger(tmp_path)
    k106, r920 = _transcribed("ZS44A-k106"), _transcribed("ZS44A-20260920")
    (pid,) = record_command_change(
        ledger, change_from_images(k106, r920), definition, vehicle=_vehicle(k106), now=T0
    )
    prediction = ledger.get(pid)
    assert isinstance(prediction, Prediction)
    with pytest.raises(MemoryReferenceError, match="not evidence"):
        ledger.resolve_evidence(pid)
    with pytest.raises(MemoryReferenceError, match="not evidence"):
        detect_discrepancy(ledger, pid)
    base = {
        "vehicle": prediction.vehicle,
        "conditions": prediction.conditions,
        "valid_from": T0,
    }
    tool = Provenance(producer="t", producer_kind="tool", version="1", code_version="x")
    with pytest.raises(MemoryReferenceError, match="not evidence"):
        ledger.append(
            Outcome(
                **base,
                provenance=tool,
                prediction=pid,
                intervention=None,
                disposition="unresolvable",
                evidence=(pid,),
                result=Result(status="unevaluable", reason="refused", detail="a what-if"),
            ),
            now=T0 + timedelta(minutes=1),
        )
    with pytest.raises(MemoryReferenceError, match="not evidence"):
        ledger.append(
            Investigation(
                **base,
                provenance=tool,
                title="what-if offered as an observation",
                question="can a computed change be observed",
                observations=(pid,),
                hypotheses=(Hypothesis(label="it was measured", supporting=(pid,)),),
            ),
            now=T0 + timedelta(minutes=1),
        )
    # The label cannot be dropped or borrowed.
    fields = prediction.model_dump(exclude={"id", "recorded_at"})
    with pytest.raises(ValidationError, match="what_if:"):
        Prediction.model_validate({**fields, "quantity": "target_equiv_ratio"})
    with pytest.raises(ValidationError, match="what_if:"):
        Prediction.model_validate({**fields, "what_if": None})
    with pytest.raises(ValidationError, match="never stated by hand"):
        Prediction.model_validate(
            {
                **fields,
                "provenance": {"producer": "me", "producer_kind": "operator", "version": "1"},
            }
        )


def test_the_card_shows_a_what_if_as_one_with_every_value_from_the_record(
    tmp_path: Path,
) -> None:
    definition = _definition_only()
    ledger = _ledger(tmp_path)
    investigation = ledger.append(
        Investigation(
            vehicle=VehicleIdentity(
                profile=SLUG, vin=None, program_id=None, calibration="ZS44A-k106"
            ),
            provenance=Provenance(producer="operator", producer_kind="operator", version="1"),
            conditions=Conditions(regime="all conditions", vehicle_state=None),
            valid_from=T0,
            title="Richen the top right of the Fuel Targets",
            question="what command does the proposed edit give",
        ),
        now=T0,
    )
    k106 = _transcribed("ZS44A-k106")
    (pid,) = record_command_change(
        ledger,
        change_from_cell_edits(
            k106,
            Patch.model_validate_json(PATCH.read_text()),
            registry=_registry(),
            definition=definition,
        ),
        definition,
        vehicle=_vehicle(k106),
        investigation=investigation.id,
        now=T0 + timedelta(minutes=1),
    )
    card = investigation_card(ledger, investigation.id)
    assert card.what_happened == () and card.evidence_state == "no_evidence"
    (view,) = card.predictions
    assert view.id == pid and view.what_if is not None
    assert view.what_if.label == "what_if" and "Not a measurement" in view.what_if.notice
    (point,) = view.what_if.points
    assert point.before is not None and point.after is not None and point.change is not None
    assert {v.record_id for v in (point.before, point.after, point.change, *point.inputs)} == {pid}
    assert point.change.value == 1 / 128
    text = render_text(card)
    assert "WHAT-IF" in text and "no ECU write was requested" in text
    # A cell edit set names no image, so no flash can be matched to it.
    assert view.what_if.order is None and view.order is None
    assert "a cell edit set names no image" in view.what_if.order_note
    assert f"cell edit set {view.what_if.change_set_sha256}" in text


# -- no write path ------------------------------------------------------------------

#: Modules that can transmit on a bus or authorize a non-volatile write. None
#: may be imported by computing or recording a what-if.
_WRITE_PATHS = ("src.control", "src.daemon", "src.dashboard", "src.mcp_server", "can", "serial")
#: The one collection module the shared helpers load: K-line row naming (pure).
_ALLOWED_COLLECTION = {"src.collection", "src.collection.kline", "src.collection.kline.names"}


def test_computing_and_recording_a_what_if_requests_no_ecu_write(tmp_path: Path) -> None:
    script = textwrap.dedent(
        f"""
        import json, sys
        from pathlib import Path
        from tests.test_twin_command_change import (
            _definition_only, _registry, _transcribed, _vehicle, PATCH, SLUG,
        )
        from src.analysis.rom_targets import Patch
        from src.analysis.twin_command_change import (
            change_from_cell_edits, change_from_images, record_command_change,
        )
        from src.evidence import MemoryLedger
        definition = _definition_only()
        k106 = _transcribed("ZS44A-k106")
        ledger = MemoryLedger(Path({str(tmp_path)!r}) / "memory.jsonl", vehicle=SLUG)
        for change in (
            change_from_images(k106, _transcribed("ZS44A-20260920")),
            change_from_cell_edits(
                k106, Patch.model_validate_json(PATCH.read_text()),
                registry=_registry(), definition=definition,
            ),
        ):
            record_command_change(ledger, change, definition, vehicle=_vehicle(k106))
        print(json.dumps(sorted(sys.modules)))
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        timeout=300,
    )
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    reached = [
        m
        for m in loaded
        if any(m == p or m.startswith(p + ".") for p in _WRITE_PATHS)
        or (m.startswith("src.collection") and m not in _ALLOWED_COLLECTION)
    ]
    assert reached == []
    assert len(_ledger(tmp_path).entries()) == 2


# -- the existing brain read paths ----------------------------------------------------


def test_the_brain_memory_and_card_routes_serve_a_what_if_unchanged(tmp_path: Path) -> None:
    import asyncio

    from src.control import brain_reads
    from src.dashboard.api import create_app
    from src.evidence import memory_ledger_for
    from src.mcp_server.tools import brain as brain_tool
    from tests.test_brain_jobs import scoped_config
    from tests.test_brain_routes import AUTH, TOKEN, _mcp_client

    config = scoped_config(tmp_path)
    ledger = memory_ledger_for(config)
    definition = _definition_only()
    k106 = _transcribed("ZS44A-k106")
    investigation = ledger.append(
        Investigation(
            vehicle=VehicleIdentity(profile=SLUG, vin=None, program_id=None, calibration=None),
            provenance=Provenance(producer="operator", producer_kind="operator", version="1"),
            conditions=Conditions(regime="all conditions", vehicle_state=None),
            valid_from=T0,
            title="Proposed Fuel Targets edit",
            question="what command does the proposed edit give",
        )
    )
    (pid,) = record_command_change(
        ledger,
        change_from_images(k106, _transcribed("ZS44A-20260920")),
        definition,
        vehicle=_vehicle(k106),
        investigation=investigation.id,
    )
    app = create_app(config, anomalies_dir=tmp_path / "an", control_token=TOKEN)
    http = TestClient(app)
    memory = http.get("/control/brain/memory", params={"kind": "prediction"}, headers=AUTH).json()
    assert memory == brain_reads.memory_query(config, {"kind": "prediction"})
    (row,) = memory["records"]
    assert row["record"]["id"] == pid and row["record"]["quantity"] == WHAT_IF_QUANTITY
    assert row["record"]["what_if"]["label"] == "what_if"
    card = http.get(f"/control/brain/investigations/{investigation.id}/card", headers=AUTH).json()
    (shown,) = card["card"]["predictions"]
    assert shown["what_if"]["label"] == "what_if"
    # The what-if is not evidence: the evidence route refuses its id.
    refused = http.get(f"/control/brain/evidence/{pid}", headers=AUTH)
    assert refused.status_code == 400

    async def through_mcp() -> tuple[Any, Any]:
        client = _mcp_client(app)
        try:
            return (
                await brain_tool.query_memory(client, kind="prediction"),
                await brain_tool.get_investigation_card(client, investigation_id=investigation.id),
            )
        finally:
            await client.aclose()

    assert asyncio.run(through_mcp()) == (memory, card)


# -- the operator's entry point ----------------------------------------------------


def test_the_cli_records_a_what_if_from_an_image_pair_or_a_patch(tmp_path: Path) -> None:
    from src.evidence import memory_ledger_for
    from tests.test_brain_jobs import scoped_config
    from tools.record_command_change import parse_args, record

    config = scoped_config(tmp_path)
    by_images = record(config, base="ZS44A-k106", target="ZS44A-20260920")
    (row,) = by_images
    assert row["kind"] == "prediction" and row["quantity"] == WHAT_IF_QUANTITY
    assert row["what_if"]["target_image_sha256"] == _image("ZS44A-20260920")
    # No library image held here: the transcribed tables, the rest unknown.
    assert row["what_if"]["not_reproduced"][0].startswith("unknown:")
    by_patch = record(config, base="ZS44A-k106", patch=PATCH, regions=["ordinary:7-7:3-4"])
    (region,) = by_patch
    assert region["what_if"]["change_set_sha256"] is not None
    assert [p["key"] for p in region["what_if"]["points"]] == ["r7c3", "r7c4"]
    assert region["direction"] == "increase" and region["magnitude"] is None
    ledger = memory_ledger_for(config)
    assert {e.record.id for e in ledger.query(kinds=("prediction",))} == {
        row["id"],
        region["id"],
    }
    with pytest.raises(ValueError, match="no revision"):
        record(config, base="ZS44A-nonesuch", target="ZS44A-20260920")
    args = parse_args(
        [
            "--config",
            "c.yaml",
            "--base",
            "ZS44A-k106",
            "--patch",
            str(PATCH),
            "--region",
            "override:0-7",
        ]
    )
    assert (args.target, args.patch, args.region) == (None, PATCH, ["override:0-7"])
    with pytest.raises(SystemExit):
        parse_args(["--config", "c.yaml", "--base", "x", "--target", "y", "--patch", "z"])
