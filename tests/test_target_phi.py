"""The target-phi twin (E02.1, E02.3-E02.5): applicability, refusals, the
October 4 known answers, and full sample accounting.

No private ROM here: the table comes from ``afr_calibration.yaml`` and the
known answers from committed telemetry
(``tests/fixtures/ground_truth/target_phi.yaml``). The image-backed checks
live in ``tests/test_target_phi_private_rom.py`` and skip by name without it.

Regenerate the fixture's ``records`` block with
``.venv/bin/python -m tests.test_target_phi`` and review the diff.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import struct
from typing import Any

import pandas as pd
import pytest

from src.analysis.channels import read_channels
from src.analysis.ecu_application import (
    ApplicationModel,
    program_sha256,
)
from src.analysis.rom_trace import AxisTrace, ViewerDefinition
from src.analysis.target_phi import (
    ResolvedTable,
    TwinDefinition,
    TwinRefusalError,
    bind_application,
    duplicate_use,
    evaluate_session,
    predict,
    resolve_slot,
    resolve_table,
    table_from_calibration,
    x_formula_check,
)
from src.signals.afr import load_afr_calibration
from src.yaml_load import load_yaml
from tests.rom_fixtures import synthetic_image, xterra_map
from tests.target_phi_builders import (
    EXPECT,
    IMAGE_SHA,
    NAMED_STATES,
    PROFILE,
    REPO,
    fixture_frame,
    hysteresis_table,
    phi_definition,
    phi_evaluate,
    phi_model,
    phi_seconds,
    phi_session,
    phi_table,
    phi_viewer,
    record_lines,
)

# -- E02.1: the declared contract ---------------------------------------------------


def test_the_shipped_definition_declares_every_input_gate_and_limit() -> None:
    definition, _ = phi_definition()
    assert definition.inputs.channels() == {
        "load": "mem:ECM_FUEL_TARGET_MAP_X_TRACE",
        "rpm": "mem:ECM_ENGINE_SPEED_RAM",
        "flag": "mem:ECM_PEDAL_ENRICH_FLAGS",
    }
    assert {i.units for i in (definition.inputs.load, definition.inputs.rpm)} == {"raw_counts"}
    assert definition.map_identity.channels() == [
        "mem:ECM_UPREV_MAGIC",
        "mem:ECM_ACTIVE_MAP",
        "mem:ECM_UPREV_MAP_SELECTOR",
    ]
    assert definition.reference.channel == "mem:ECM_TARGET_EQUIV_RATIO"
    assert definition.reference.sentinel_channel == "mem:ECM_TARGET_EQUIV_RATIO_SENTINEL"
    assert definition.x_formula.bfs_channel == "mem:ECM_AIR_CHARGE_SLOT_103A"
    assert definition.gates.coolant.sources == ["did:ECM_COOLANT_TEMP", "obd:COOLANT_TEMP"]
    assert definition.gates.mode.channel == "obd:FUEL_STATUS"
    frozen = EXPECT["frozen_gates"]
    gates = definition.gates
    assert (
        gates.max_acquisition_skew_s,
        gates.clock,
        definition.map_identity.max_age_s,
        gates.coolant.min_c,
        gates.coolant.max_age_s,
        gates.mode.eligible,
        gates.mode.max_age_s,
        gates.steadiness.dwell_s,
    ) == (
        frozen["max_acquisition_skew_s"],
        frozen["clock"],
        frozen["map_identity_max_age_s"],
        frozen["coolant_min_c"],
        frozen["coolant_max_age_s"],
        frozen["mode_eligible"],
        frozen["mode_max_age_s"],
        frozen["steadiness_dwell_s"],
    )


def _raw_definition() -> dict[str, Any]:
    loaded: dict[str, Any] = load_yaml((PROFILE / "target_phi_twin.yaml").read_text())
    return loaded


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        (("inputs", "load", "units"), "display", "raw_counts"),
        (("gates", "mode", "eligible"), ["open_loop_bogus"], "names no declared state"),
        (("gates", "clock"), "round", "acquired"),
        (("gates", "steadiness", "channels"), [{"channel": "obd:RPM"}], "exactly one"),
        (("regions", "ordinary", "load_raw"), None, "load_raw"),
        # Qualification status belongs to the application model (E05.1): the
        # twin's own file cannot declare its path verified.
        (("final_command", "status"), "verified", "Extra inputs"),
        (("map_identity", "ram_copy"), {"status": "verified"}, "Extra inputs"),
    ],
)
def test_the_definition_refuses_an_undeclared_or_unsafe_contract(
    path: tuple[str, ...], value: Any, match: str
) -> None:
    raw = _raw_definition()
    node = raw
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValueError, match=match):
        TwinDefinition.model_validate(raw)


def test_a_missing_gate_section_is_refused() -> None:
    raw = _raw_definition()
    del raw["gates"]["coolant"]
    with pytest.raises(ValueError, match="coolant"):
        TwinDefinition.model_validate(raw)


# -- the pure prediction ------------------------------------------------------------


def test_the_pedal_flag_forces_the_last_column_whatever_the_load_byte() -> None:
    definition, _ = phi_definition()
    table = phi_table(definition)
    for load in (0, 10, 100, 128):
        prediction = predict(table, definition, load_raw=load, rpm_raw=236, flag_raw=1)
        assert (prediction.branch, prediction.cell, prediction.word) == ("override", 149, 19072)
        assert prediction.phi == 1.1640625
    # Bit 0 only: another bit set does not force the column.
    assert predict(table, definition, load_raw=10, rpm_raw=236, flag_raw=2).branch == "ordinary"


def test_an_ordinary_lookup_below_the_enrichment_threshold_commands_stoich() -> None:
    definition, _ = phi_definition()
    table = phi_table(definition)
    prediction = predict(table, definition, load_raw=10, rpm_raw=236, flag_raw=0)
    assert (prediction.cell, prediction.word, prediction.phi) == (128, 16384, 1.0)
    # An enriched interior cell passes the gate unchanged: 146 (see test_ecm_lookup).
    assert predict(table, definition, load_raw=0x6D, rpm_raw=256, flag_raw=0).word == 146 << 7


def test_a_lookup_inside_the_gates_hysteresis_band_is_ambiguous() -> None:
    definition, _ = phi_definition()
    base = phi_table(definition)
    # 128 -> 132 a quarter of the way: 129, inside [129, 130).
    table = ResolvedTable(
        **{
            **base.__dict__,
            "cells": tuple(tuple([128, 132] + [128] * 6) for _ in range(8)),
        }
    )
    prediction = predict(table, definition, load_raw=0x38, rpm_raw=96, flag_raw=0)
    assert prediction.cell == 129
    assert (prediction.word, prediction.reason) == (None, "enrichment_gate_state_unknown")


# -- E02.2 (resolution part): refusals by name ----------------------------------------


def _image_with_modelled_constants(**kwargs: Any) -> bytes:
    """A synthetic image carrying what the twin and the application model
    check: the gate and scale constants, UpRev's pointer redirect, and every
    calibration condition the model's dead paths depend on."""
    image = bytearray(synthetic_image(**kwargs))
    image[0x8B39] = 130
    image[0x8B3D] = 129
    image[0xD247 : 0xD247 + 8] = bytes([128] * 8)
    struct.pack_into(">H", image, 0x9416, 44019)
    model, _ = phi_model()
    for marker in model.program.patch.markers:
        raw = bytes.fromhex(marker.bytes)
        image[marker.offset : marker.offset + len(raw)] = raw
    for node in model.nodes:
        for condition in node.calibration_conditions:
            for k in range(condition.count):
                at = condition.offset + k
                image[at] = (image[at] & ~condition.mask & 0xFF) | condition.equals
    return bytes(image)


def _synthetic_program(image: bytes) -> tuple[ApplicationModel, str]:
    """The real application model, with its program identity re-pinned to a
    synthetic image's program spans: the stand-in for "the same program"."""
    model, sha = phi_model()
    identity = model.program.identity.model_copy(update={"sha256": program_sha256(image, model)})
    program = model.program.model_copy(update={"identity": identity})
    return model.model_copy(update={"program": program}), sha


def test_resolution_reads_the_slot_table_axes_and_scaler_from_the_image() -> None:
    definition, _ = phi_definition()
    image = _image_with_modelled_constants()
    sha = hashlib.sha256(image).hexdigest()
    table = resolve_table(
        image,
        xterra_map(),
        definition,
        viewer=phi_viewer(),
        application_model=_synthetic_program(image),
        expected_sha256=sha,
        slot=1,
    )
    reference = phi_table(definition)
    assert table.cells == reference.cells
    assert table.row_breakpoints == tuple(b * 4 for b in table.row_axis_raw)
    assert table.load_point_scaler == 44019
    assert table.final_command_constants_checked
    assert table.provenance()["table_source"] == "rom_library"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("hash", "image_hash_mismatch"),
        ("program", "wrong_program"),
        ("program_hash", "wrong_program"),
        ("patch", "patch_missing"),
        ("dead_path", "unmodelled_logic"),
        ("slot", "table_unverified"),
        ("kind", "table_missing"),
        ("gate", "unmodelled_logic"),
        ("curve", "unmodelled_logic"),
    ],
)
def test_resolution_refuses_by_name(change: str, reason: str) -> None:
    """``program`` is the calibration ID; ``program_hash`` is a different
    program under the same calibration ID (the application model's identity,
    never the ID alone); ``patch`` loses UpRev's pointer redirect;
    ``dead_path`` schedules the alternate multiplier writer the model treats
    as calibration-dead."""
    definition, _ = phi_definition()
    kwargs: dict[str, Any] = {}
    if change == "program":
        kwargs["calibration_id"] = b"1ZS04C00000"
    image = bytearray(_image_with_modelled_constants(**kwargs))
    if change == "gate":
        image[0x8B39] = 140
    if change == "curve":
        image[0xD247 + 3] = 140
    if change == "patch":
        image[0x055EC4 + 3] ^= 0xFF
    if change == "dead_path":
        image[0x8AD3] |= 0x10
    application = phi_model() if change == "program_hash" else _synthetic_program(bytes(image))
    sha = hashlib.sha256(bytes(image)).hexdigest()
    slot = 2 if change == "slot" else 1
    if change == "hash":
        sha = IMAGE_SHA
    rom_map = xterra_map()
    if change == "kind":
        # A map definition that places no Target AFR table at all.
        rom_map = rom_map.model_copy(
            update={"tables": [t for t in rom_map.tables if t.kind != "fuel_targets"]}
        )
    with pytest.raises(TwinRefusalError) as refused:
        resolve_table(
            bytes(image),
            rom_map,
            definition,
            viewer=phi_viewer(),
            application_model=application,
            expected_sha256=sha,
            slot=slot,
        )
    assert refused.value.reason == reason


def _viewer_with(axis: str, **changes: Any) -> ViewerDefinition:
    viewer = phi_viewer()
    info = viewer.table("fuel_targets")
    assert info is not None and info.trace is not None
    old = getattr(info.trace, axis)
    replaced = AxisTrace.model_validate({**old.model_dump(), **changes})
    trace = info.trace.model_copy(update={axis: replaced})
    tables = [
        t.model_copy(update={"trace": trace}) if t.kind == "fuel_targets" else t
        for t in viewer.tables
    ]
    return viewer.model_copy(update={"tables": tables})


@pytest.mark.parametrize(
    ("viewer_change", "reason"),
    [
        (
            (
                "x",
                {"axis_mapping": "unverified", "axis_units": None, "scale": None, "offset": None},
            ),
            "mapping_unverified",
        ),
        (("x", {"axis_units": "display", "scale": 0.250859}), "mapping_mismatch"),
        (("y", {"channel": "mem:ECM_CAM_MAP_RPM_TRACE"}), "mapping_mismatch"),
    ],
)
def test_resolution_consumes_w05s_trace_mapping_and_refuses_by_name(
    viewer_change: tuple[str, dict[str, Any]], reason: str
) -> None:
    """The twin reads W05.1's verdict on how the ECM's coordinates land on
    the axes; an unverified or different mapping refuses both table paths."""
    definition, _ = phi_definition()
    viewer = _viewer_with(viewer_change[0], **viewer_change[1])
    calibration = load_afr_calibration(PROFILE / "afr_calibration.yaml")
    with pytest.raises(TwinRefusalError) as refused:
        table_from_calibration(
            calibration,
            definition,
            viewer=viewer,
            application_model=phi_model(),
            revision="ZS44A-20260920",
            image_sha256=IMAGE_SHA,
            slot=1,
        )
    assert refused.value.reason == reason
    image = _image_with_modelled_constants()
    with pytest.raises(TwinRefusalError) as refused:
        resolve_table(
            image,
            xterra_map(),
            definition,
            viewer=viewer,
            application_model=_synthetic_program(image),
            expected_sha256=hashlib.sha256(image).hexdigest(),
            slot=1,
        )
    assert refused.value.reason == reason


@pytest.mark.parametrize(
    ("magic", "active", "selector", "slot", "reason"),
    [
        (0xAA5555AA, 0, 0, 1, None),
        (0xAA5555AA, 3, 3, 3, None),
        (0x00000000, 0, 0, None, "map_uninitialised"),
        (0xAA5555AA, 0, 2, None, "slot_ambiguous"),
        (0xAA5555AA, 9, 0, None, "slot_ambiguous"),
    ],
)
def test_slot_resolution_names_unresolved_state(
    magic: int, active: int, selector: int, slot: int | None, reason: str | None
) -> None:
    definition, _ = phi_definition()
    resolved = resolve_slot(definition, magic=magic, active_map=active, selector=selector)
    assert (resolved.slot, resolved.reason) == (slot, reason)


# -- E02.5: synthetic sessions, one named reason each ---------------------------------


def _only(result: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any]
    (record,) = result["records"]
    return record


def test_a_complete_steady_open_loop_override_tuple_is_eligible_and_passes() -> None:
    result = phi_evaluate(phi_session())
    record = _only(result)
    assert (record["state"], record["reason"], record["branch"]) == ("eligible", None, "override")
    assert (record["predicted_word"], record["difference_words"]) == (19072, 0)
    assert record["within_tolerance"] is True
    # rpm word 236 is 12 of 64 counts past row 3; the flag clamps to column 7.
    assert record["lookup"]["cells"] == [[3, 7], [4, 7]]
    assert record["lookup"]["cell_value"] == 149
    assert (record["lookup"]["row_fraction_q16"], record["lookup"]["column_edge"]) == (
        12288,
        "above",
    )
    assert record["inputs"]["load"]["same_read"] is True
    assert record["tuple_skew_s"] == pytest.approx(0.12)
    # Eligible, but the final-command path and RAM copy are unverified: the
    # difference is kept, the qualified residual is not issued.
    assert record["qualified_residual_phi"] is None
    assert "final_command_path_unverified" in result["qualification_blockers"]
    assert result["accounting"]["eligible"]["override"] == {"pass": 1, "miss": 0}
    assert result["accounting"]["duplicate_use"] == 0
    assert record["provenance"]["image_sha256"] == IMAGE_SHA
    assert record["provenance"]["kernel_version"] >= 1
    assert record["provenance"]["application_model"]["output"] == "target_equiv_ratio"


def test_qualification_follows_the_application_models_blockers() -> None:
    """The twin restates no qualification status: the blockers are the
    application model's (E05.1). With none left and the constants checked on
    an image, the same eligible pass issues its qualified residual; the
    model's blockers alone withhold it."""
    definition, _ = phi_definition()
    base = phi_table(definition)
    assert base.application.blockers == ("final_command_path_unverified", "ram_copy_unverified")
    checked = ResolvedTable(**{**base.__dict__, "final_command_constants_checked": True})
    record = _only(phi_evaluate(phi_session(), table=checked))
    assert record["qualified_residual_phi"] is None
    resolved = ResolvedTable(
        **{
            **checked.__dict__,
            "application": dataclasses.replace(base.application, blockers=()),
        }
    )
    result = phi_evaluate(phi_session(), table=resolved)
    assert result["qualification_blockers"] == []
    assert _only(result)["qualified_residual_phi"] == 0.0


def test_the_twin_refuses_an_application_model_that_describes_other_inputs() -> None:
    definition, _ = phi_definition()
    model, sha = phi_model()
    observed = model.node("pedal_enrichment_flag").observed_as[0]
    moved = observed.model_copy(update={"spec": "mem:ECM_PEDAL_ENRICH_FLAGS_OTHER"})
    nodes = [
        n.model_copy(update={"observed_as": [moved]}) if n.id == "pedal_enrichment_flag" else n
        for n in model.nodes
    ]
    with pytest.raises(TwinRefusalError) as refused:
        bind_application((model.model_copy(update={"nodes": nodes}), sha), definition)
    assert refused.value.reason == "application_model_mismatch"
    with pytest.raises(TwinRefusalError) as refused:
        bind_application(
            (model, sha), definition.model_copy(update={"application_output": "ignition"})
        )
    assert refused.value.reason == "output_undeclared"
    calibration = load_afr_calibration(PROFILE / "afr_calibration.yaml")
    with pytest.raises(TwinRefusalError) as refused:
        table_from_calibration(
            calibration,
            definition,
            viewer=phi_viewer(),
            application_model=(model, sha),
            revision=EXPECT["calibration"]["revision"],
            image_sha256="0" * 64,
            slot=1,
        )
    assert refused.value.reason == "program_unverified"


def test_a_miss_is_counted_not_absorbed() -> None:
    record = _only(phi_evaluate(phi_session(word=19072 + 2)))
    assert (record["state"], record["difference_words"], record["within_tolerance"]) == (
        "eligible",
        2,
        False,
    )


@pytest.mark.parametrize(
    ("kwargs", "edit", "evaluate", "state", "reason"),
    NAMED_STATES,
    ids=[f"{state}-{reason}" for _, _, _, state, reason in NAMED_STATES],
)
def test_each_unmet_condition_has_a_named_state_and_a_null_qualified_residual(
    kwargs: dict[str, Any], edit: Any, evaluate: dict[str, Any], state: str, reason: str
) -> None:
    frame = phi_session(**kwargs)
    if edit is not None:
        frame = edit(frame)
    evaluate = dict(evaluate)
    if evaluate.pop("table", None) == "hysteresis":
        evaluate["table"] = hysteresis_table()
    record = _only(phi_evaluate(frame, **evaluate))
    assert record["state"] == state
    # The primary reason when it is the only one; otherwise among the named
    # reasons of that state (a capture still recording waits on several reads).
    if state == "pending":
        assert reason in record["reasons"]
    else:
        assert record["reason"] == reason
    assert record["qualified_residual_phi"] is None


def test_every_reason_the_evaluator_names_is_reached_by_a_test() -> None:
    """A named state no test reaches is either dead or unverified; this keeps
    the list above and the evaluator's vocabulary the same set."""
    import inspect
    import re

    from src.analysis import target_phi

    source = inspect.getsource(target_phi)
    named = set(re.findall(r'\("(?:unevaluable|pending|excluded)", f?"([a-z_]+)(?::|")', source))
    reached = {reason.split(":")[0] for *_, reason in NAMED_STATES}
    # ``closed_loop`` is a mode-state name the profile supplies, not a literal.
    assert named <= reached, sorted(named - reached)


def test_a_switch_right_after_an_identity_read_at_the_sample_is_caught() -> None:
    """An identity read stamped exactly at the sample is the *prior*; the
    bracket still needs the next read. Taking the one read as both sides hid
    this switch (slot 1 at t, slot 2 at t + 5 s)."""
    frame = phi_session(t=30.0, active=1, active_after=2)
    assert (phi_seconds(frame[frame["channel"] == "mem:ECM_ACTIVE_MAP"]) == 30.0).any()
    record = _only(phi_evaluate(frame))
    assert record["reason"] == "map_switching"
    assert [record["map_identity"][side]["slot"] for side in ("before", "after")] == [1, 2]


def test_an_unreadable_gate_source_refuses_the_session() -> None:
    definition, sha = phi_definition()
    gates = definition.gates
    broken = definition.model_copy(
        update={
            "gates": gates.model_copy(
                update={
                    "coolant": gates.coolant.model_copy(update={"sources": ["did:NO_SUCH_DID"]})
                }
            )
        }
    )
    with pytest.raises(TwinRefusalError) as refused:
        evaluate_session(phi_session(), broken, phi_table(definition), definition_sha256=sha)
    assert refused.value.reason == "gate_unreadable"


def test_a_window_the_catalog_disagrees_with_refuses_the_session() -> None:
    definition, sha = phi_definition()
    acquisition = definition.acquisition
    windows = {**acquisition.windows}
    windows["spark"], windows["flags_2841"] = windows["flags_2841"], windows["spark"]
    moved = definition.model_copy(
        update={"acquisition": acquisition.model_copy(update={"windows": windows})}
    )
    with pytest.raises(TwinRefusalError) as refused:
        evaluate_session(phi_session(), moved, phi_table(definition), definition_sha256=sha)
    assert refused.value.reason == "window_mismatch"


def test_duplicate_use_counts_an_input_read_placed_in_two_tuples() -> None:
    """The check can fail: two records citing one X read count once each way."""
    ref = {"spec": "mem:ECM_FUEL_TARGET_MAP_X_TRACE", "row": 7}
    records = [
        {"sample": {"spec": "mem:ECM_TARGET_EQUIV_RATIO", "row": 0}, "inputs": {"load": ref}},
        {"sample": {"spec": "mem:ECM_TARGET_EQUIV_RATIO", "row": 1}, "inputs": {"load": ref}},
    ]
    assert duplicate_use(records) == 1
    # Two commands 0.1 s apart with one X read between them: pairing gives it
    # to one, and the other is stale rather than sharing it.
    frame = phi_session()
    second = frame[frame["channel"] == "mem:ECM_TARGET_EQUIV_RATIO"].copy()
    second["timestamp"] = second["timestamp"] + pd.Timedelta("100ms")
    second["metadata_json"] = [
        json.dumps({"round_id": 1, "acquired_at": t.timestamp()}) for t in second["timestamp"]
    ]
    frame = pd.concat([frame, second]).sort_values("timestamp", kind="stable")
    result = phi_evaluate(frame.reset_index(drop=True))
    assert result["accounting"]["duplicate_use"] == 0
    assert sorted(r["reason"] or "" for r in result["records"])[-1].startswith("input_stale")


def test_a_transient_keeps_its_diagnostic_difference_separately() -> None:
    result = phi_evaluate(phi_session(rpm_span=400.0, word=19072 + 5))
    record = _only(result)
    assert (record["state"], record["difference_words"]) == ("excluded", 5)
    assert result["accounting"]["transient_differences"] == {"outside_tolerance": 1}
    assert result["accounting"]["eligible"]["override"] == {"pass": 0, "miss": 0}


def test_a_sample_still_waiting_for_reads_is_pending_while_the_session_records() -> None:
    frame = phi_session(t=58.0, end=58.0)
    assert _only(phi_evaluate(frame, session_complete=False))["state"] == "pending"
    assert _only(phi_evaluate(frame))["reason"] == "map_identity_unbracketed"


def test_a_load_byte_stored_under_another_decode_refuses_the_session() -> None:
    frame = phi_session()
    rows = frame["channel"] == "mem:ECM_FUEL_TARGET_MAP_X_TRACE"
    frame.loc[rows, "value_number"] = 100.0  # raw counts, not raw x 0.250859
    with pytest.raises(TwinRefusalError) as refused:
        phi_evaluate(frame)
    assert refused.value.reason == "wrong_units"


# -- E02.3: the October 4 known answers -----------------------------------------------


def test_the_fixture_pins_the_decodes_it_was_written_under() -> None:
    from src.analysis.rom_trace import stored_decode

    for spec, pin in EXPECT["decode_pins"].items():
        assert stored_decode(spec, REPO / "data" / "dids.yaml") == (pin["scale"], pin["offset"])
    frame = fixture_frame()
    counts = frame.groupby("channel").size().to_dict()
    for part in EXPECT["telemetry"]:
        for channel, n in part["channels"].items():
            assert counts[channel] == n, channel


def _nearest_before(long: pd.DataFrame, spec: str, at: pd.Timestamp) -> tuple[float, float]:
    """The last ``spec`` reading acquired before ``at``: (value, seconds earlier)."""
    rows = long[(long["spec"] == spec) & (long["timestamp"] < at)]
    last = rows.iloc[-1]
    return float(last["value"]), (at - last["timestamp"]).total_seconds()


def test_the_two_enrichment_commands_are_exactly_the_forced_last_column() -> None:
    definition, _ = phi_definition()
    table = phi_table(definition)
    frame = fixture_frame()
    result = phi_evaluate(frame)
    # Exactly two commands read 0x4A80 all drive, at the recorded instants,
    # and neither is a live pass: their inputs were read ~0.9 s earlier.
    at_override = [
        record for record in result["records"] if record["sample"]["observed_word"] == 0x4A80
    ]
    assert [line.split()[0] for line in record_lines({"records": at_override})] == [
        case["command_at"] for case in EXPECT["known_override"]
    ]
    assert {record["state"] for record in at_override} == {"unevaluable"}
    # The lookup vectors come out of the telemetry, not the YAML: the last rpm
    # word and flag byte read before each command, undone to raw counts.
    long = read_channels(
        frame,
        ["mem:ECM_ENGINE_SPEED_RAM", "mem:ECM_PEDAL_ENRICH_FLAGS"],
        acquired_time=True,
    )
    for case, record in zip(EXPECT["known_override"], at_override, strict=True):
        at = pd.Timestamp(record["sample"]["acquired_at"])
        rpm, rpm_age = _nearest_before(long, "mem:ECM_ENGINE_SPEED_RAM", at)
        flag, flag_age = _nearest_before(long, "mem:ECM_PEDAL_ENRICH_FLAGS", at)
        rpm_raw = round(rpm / 12.5)
        assert rpm_raw * 12.5 == rpm
        assert (rpm_raw, int(flag)) == (case["rpm_raw"], case["nearest_flag_raw"])
        assert round(rpm_age, 3) == case["rpm_read_s_before"]
        assert round(flag_age, 3) == case["flag_read_s_before"]
        assert case["observed_word"] == 0x4A80 == 0x95 << 7
        assert case["observed_word"] / 16384 == 1.1640625 == 0x95 / 128
        # Forced column: the load byte is replaced, so whatever was read.
        prediction = predict(table, definition, load_raw=0, rpm_raw=rpm_raw, flag_raw=1)
        assert (prediction.cell, prediction.word) == (
            case["predicted_cell"],
            case["predicted_word"],
        )
        assert prediction.word == case["observed_word"]
        assert prediction.phi == 1.1640625
        # The same vector with the flag as last read: only the first command's
        # flag read was set, so only it reproduces without the forced column.
        as_read = predict(table, definition, load_raw=0, rpm_raw=rpm_raw, flag_raw=int(flag))
        assert (as_read.word == case["observed_word"]) == bool(case["nearest_flag_raw"])


def test_the_override_known_answer_exposes_an_rpm_decode_mistake() -> None:
    """Negative control: reading the rpm word at the axis's 50 rpm per count
    (instead of 12.5) lands in row 0, whose last column is 0x92, not 0x95."""
    definition, _ = phi_definition()
    table = phi_table(definition)
    for case in EXPECT["known_override"]:
        wrong = round(case["rpm_raw"] * 12.5 / 50)
        prediction = predict(table, definition, load_raw=0, rpm_raw=wrong, flag_raw=1)
        assert prediction.word != case["observed_word"]


def test_the_x_relation_reproduces_168_of_194_and_its_misses_both_signs() -> None:
    definition, _ = phi_definition()
    expected = EXPECT["x_relation"]
    result = x_formula_check(
        fixture_frame(), definition, scaler=EXPECT["calibration"]["load_point_scaler"]
    )
    assert (result["n_pairs"], result["exact"]) == (expected["n_pairs"], expected["exact"])
    assert result["differences"] == expected["differences"]
    misses = [p["difference"] for p in result["pairs"] if p["difference"]]
    assert len(misses) == 26 and min(misses) < 0 < max(misses)
    assert all(1 <= abs(d) <= 4 for d in misses)
    assert max(p["separation_s"] for p in result["pairs"]) == expected["max_separation_s"]


def test_the_x_relation_exposes_an_x_scale_mistake() -> None:
    """Negative control: comparing the stored (display-scaled) X with the
    formula, as if it were the raw byte, matches no pair; so does the wrong
    scaler."""
    definition, _ = phi_definition()
    frame = fixture_frame()
    good = x_formula_check(frame, definition, scaler=44019)
    stored = [p["x"] * 0.250859 for p in good["pairs"]]
    assert sum(round(s) == p["x_from_bfs"] for s, p in zip(stored, good["pairs"], strict=True)) < 10
    wrong = x_formula_check(frame, definition, scaler=round(44019 * 1.1))
    assert wrong["exact"] < 100


def test_every_october_4_command_sample_is_accounted_for() -> None:
    result = phi_evaluate(fixture_frame())
    accounting = result["accounting"]
    expected = EXPECT["accounting"]
    assert accounting["candidates"] == expected["candidates"]
    assert accounting["states"] == expected["states"]
    assert accounting["reasons"] == expected["reasons"]
    assert accounting["eligible"] == expected["eligible"]
    assert accounting["duplicate_use"] == expected["duplicate_use"] == 0
    assert accounting["input_pairing"] == expected["input_pairing"]
    assert sum(accounting["states"].values()) == accounting["candidates"]
    assert result["capture_requirement"]["reason"] == EXPECT["capture_requirement"]
    assert result["qualification_blockers"] == EXPECT["qualification_blockers"]
    assert record_lines(result) == EXPECT["records"]
    for record in result["records"]:
        assert record["sample"]["acquired_at"] and record["sample"]["row"] >= 0
        assert record["qualified_residual_phi"] is None


if __name__ == "__main__":  # pragma: no cover - fixture minting
    for line in record_lines(phi_evaluate(fixture_frame())):
        print(f"  - '{line}'")
