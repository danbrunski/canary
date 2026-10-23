"""E05.3 (d): the command overrides in the ECM application model.

Each warm-up, closed-loop, transient, fuel-cut and start override is data in
``ecu_application_model.yaml``: where it is in the program, its condition and
effect, how it was read and how far it has been checked. These tests hold the
schema to that honesty, and run the one check the committed 2026-10-04 slice
can make: the fuel-cut state byte and the zero command word come back in the
same reply.

Two kinds of evidence, labelled per test:

- **schema**: refusals on edited copies of the real manifest;
- **fixture**: the committed 2026-10-04 ram-study slice. Its 18-byte reads
  from ``0xFFFF26AE`` carry the cut state, both short-term trims, the
  correction gate byte and the command word in one reply.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.analysis.ecu_application import (
    ApplicationModel,
    applicability,
    coverage_report,
    load_application_model,
)
from src.yaml_load import load_yaml

REPO = Path(__file__).resolve().parents[1]
PROFILE = REPO / "data" / "nissan-xterra-2008"
EXPECT = load_yaml((REPO / "tests/fixtures/ground_truth/target_phi.yaml").read_text())

#: The 2026-10-04 fuel_target read started at 0xFFFF26AE and held 18 bytes;
#: the command word is its last two (pinned against the decoded channel below).
WINDOW_START = 0xFFFF26AE
COMMAND = 0xFFFF26BE - WINDOW_START
CUT_STATE = 0xFFFF26B8 - WINDOW_START
STFT_B1 = 0xFFFF26B4 - WINDOW_START
STFT_B2 = 0xFFFF26B6 - WINDOW_START
GATE_26B9 = 0xFFFF26B9 - WINDOW_START
NEUTRAL_TRIM = 0x6400


def _raw() -> dict[str, Any]:
    loaded: dict[str, Any] = load_yaml((PROFILE / "ecu_application_model.yaml").read_text())
    return loaded


def _override(raw: dict[str, Any], override_id: str) -> dict[str, Any]:
    found: dict[str, Any] = next(o for o in raw["overrides"] if o["id"] == override_id)
    return found


def _output(raw: dict[str, Any], output_id: str) -> dict[str, Any]:
    found: dict[str, Any] = next(o for o in raw["outputs"] if o["id"] == output_id)
    return found


def _refuses(raw: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ApplicationModel.model_validate(raw)


def _slice() -> pd.DataFrame:
    (part,) = [p for p in EXPECT["telemetry"] if "target-phi-slice" in p["path"]]
    path = REPO / part["path"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == part["sha256"]
    frame = pd.read_parquet(path)
    frame["acquired_at"] = frame["metadata_json"].map(lambda m: json.loads(m)["acquired_at"])
    return frame


def _command_reads() -> pd.DataFrame:
    """Every 0xFFFF26AE read: value rows and fuel-cut (sentinel) rows alike."""
    frame = _slice()
    channels = {"mem:ECM_TARGET_EQUIV_RATIO", "mem:ECM_TARGET_EQUIV_RATIO_SENTINEL"}
    reads = frame[frame["channel"].isin(channels)].copy()
    assert set(reads["payload"].map(len)) == {COMMAND + 2}
    return reads


def _word(payload: bytes, offset: int) -> int:
    return int.from_bytes(payload[offset : offset + 2], "big")


# -- what the manifest claims ----------------------------------------------------------


def test_every_regime_has_an_identified_override_and_only_one_is_checked() -> None:
    """(schema) Warm-up, closed loop, transient and fuel cut each have traced
    overrides (start too). Only the fuel-cut zero word is above ``traced``,
    at ``fixture``; nothing claims the vehicle."""
    model, _ = load_application_model(PROFILE)
    regimes = Counter(o.regime for o in model.overrides)
    assert set(regimes) == {"start", "warm_up", "closed_loop", "transient", "fuel_cut"}
    qualified = {o.id: o.qualification for o in model.overrides if o.qualification != "traced"}
    assert qualified == {"fuel_cut_zero_command": "fixture"}
    for override in model.overrides:
        assert (override.confidence == "observed") == (override.id in qualified), override.id
        assert override.evidence and override.verify_with, override.id
        if override.routine is None or override.established_by != "listing":
            # A decompile-only or unlocated trace says what it lacks.
            assert override.gaps, override.id


def test_qualified_by_names_tests_that_exist() -> None:
    model, _ = load_application_model(PROFILE)
    named = [q for o in model.overrides for q in o.qualified_by]
    assert named
    for entry in named:
        path, _, test = entry.partition("::")
        source = (REPO / path).read_text()
        assert re.search(rf"^def {re.escape(test)}\(", source, re.M), entry


def test_overrides_leave_every_outputs_blockers_and_twin_as_they_were() -> None:
    """(schema) Adding the overrides is data: no output's blockers, twin or
    status move, so no prediction changes."""
    model, sha = load_application_model(PROFILE)
    assert applicability(model, sha, "target_equiv_ratio").blockers == (
        "final_command_path_unverified",
        "ram_copy_unverified",
    )
    assert [b.name for b in model.output("base_fuel_schedule_word").blockers] == [
        "air_charge_ram_copy_unverified",
        "airflow_unmodelled",
        "fill_byte_not_computed",
        "fill_dynamics_reference_only",
    ]
    target = model.output("target_equiv_ratio")
    assert target.twin is not None and target.twin.predicts == "enrichment_gate"
    assert {o.status for o in model.outputs} == {"conditional"}


def test_the_report_lists_each_override_with_its_qualification() -> None:
    model, sha = load_application_model(PROFILE)
    report = coverage_report(model, sha)
    rows = {o["id"]: o for o in report["overrides"]}
    assert rows["fuel_cut_zero_command"]["qualification"] == "fixture"
    assert rows["fuel_cut_zero_command"]["routine"] == "0x055B5E"
    assert rows["stationary_cut"]["routine"] is None and rows["stationary_cut"]["gaps"]
    assert rows["closed_loop_feedback_stft"]["acts_on_ram"] == ["0xFFFF26B4", "0xFFFF26B6"]
    outputs = {o["id"]: o for o in report["outputs"]}
    assert "fuel_cut_zero_command" in outputs["target_equiv_ratio"]["overrides"]
    assert outputs["base_fuel_schedule_word"]["overrides"] == ["cranking_tp", "bfs_fill_transient"]
    assert outputs["ignition_timing"]["overrides"] == []
    regions = {r["id"]: r for r in outputs["target_equiv_ratio"]["regions"]}
    assert regions["fuel_cut"] == {
        "id": "fuel_cut",
        "status": "unevaluable",
        "when": regions["fuel_cut"]["when"],
        "nodes": ["final_command_word"],
        "overrides": ["fuel_cut_zero_command"],
    }


# -- refusals: a traced override cannot pass as a verified one --------------------------


def test_an_override_on_an_output_is_held_out_until_it_qualifies_on_the_vehicle() -> None:
    raw = copy.deepcopy(_raw())
    region = next(r for r in _output(raw, "target_equiv_ratio")["regions"] if r["id"] == "fuel_cut")
    region["overrides"] = []
    _refuses(raw, "fuel_cut_zero_command is fixture, not vehicle-qualified")
    # Vehicle-qualified, it may leave the region.
    override = _override(raw, "fuel_cut_zero_command")
    override.update(qualification="vehicle", qualified_by=["session 2026-11-01T120000"])
    ApplicationModel.model_validate(raw)


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        (
            lambda r: _override(r, "enrichment_ramp").update(qualification="vehicle"),
            "qualified_by is required",
        ),
        (
            lambda r: _override(r, "enrichment_ramp").update(qualified_by=["tests/x.py::test_y"]),
            "qualified_by is required",
        ),
        (
            lambda r: _override(r, "injector_wall_film").update(
                established_by="inferred",
                qualification="fixture",
                qualified_by=["tests/x.py::test_y"],
                confidence="observed",
            ),
            "inferred trace cannot qualify",
        ),
        (
            lambda r: _override(r, "enrichment_ramp").update(confidence="observed"),
            "confidence observed goes with",
        ),
        (
            lambda r: _override(r, "fuel_cut_zero_command").update(confidence="code"),
            "confidence observed goes with",
        ),
        (
            lambda r: _override(r, "injector_wall_film").update(established_by="inferred"),
            None,
        ),
        (
            lambda r: _override(r, "cranking_tp").update(established_by="inferred"),
            "not confidence code",
        ),
        (lambda r: _override(r, "stationary_cut").update(gaps=[]), "unrecorded routine"),
        (
            lambda r: _override(r, "enrichment_ramp").update(routine=0x055B5E, span=None),
            "is in 0x055DB2, not routine 0x055B5E",
        ),
        (
            lambda r: _override(r, "cranking_tp").update(acts_on=["target_equiv_ratio"]),
            "not on that output's path",
        ),
        (
            lambda r: _override(r, "closed_loop_feedback_stft")["reads"].append(0xFFFF0000),
            "not declared under `ram`",
        ),
        (
            lambda r: _override(r, "fuel_cut_zero_command").update(
                condition="fuel-cut state 0xFFFF26B8 = 0x3F and 0xFFFF2841 bit 0 clear"
            ),
            r"cites RAM it neither reads nor writes: \['0xFFFF2841'\]",
        ),
        (
            lambda r: next(
                g for g in _output(r, "target_equiv_ratio")["regions"] if g["id"] == "cold_state_5"
            )["overrides"].append("cranking_tp"),
            r"\['cranking_tp'\] are not overrides acting on this output",
        ),
        (lambda r: r["overrides"].append(dict(r["overrides"][0])), "duplicate override ids"),
        (lambda r: _override(r, "limp_cut").update(acts_on=["engine_torque"]), "undeclared output"),
    ],
)
def test_the_schema_refuses_an_override_that_overstates_or_floats(
    edit: Any, match: str | None
) -> None:
    raw = copy.deepcopy(_raw())
    edit(raw)
    if match is None:
        # An inferred trace is allowed, as long as it claims no more than that.
        ApplicationModel.model_validate(raw)
    else:
        _refuses(raw, match)


def test_an_override_names_what_it_acts_on_or_the_gap_that_hides_it() -> None:
    raw = copy.deepcopy(_raw())
    _override(raw, "rev_limiter_cut").update(gaps=[])
    _refuses(raw, "name what it acts on")


# -- the 2026-10-04 slice (fixture) ------------------------------------------------------


def test_the_fuel_cut_state_and_the_zero_word_agree_on_every_october_4_read() -> None:
    """Qualifies ``fuel_cut_zero_command`` at ``fixture``. The same reply
    carries the condition (0xFFFF26B8) and the effect (0xFFFF26BE): 0x3F with
    a zero word on all 21 sentinel reads, 0x00 with a non-zero word on all 153
    others. The window's alignment is pinned by the decoded channel itself,
    and the neighbouring bytes do not separate cut from running, so the
    relation could have failed."""
    reads = _command_reads()
    values = reads[reads["channel"] == "mem:ECM_TARGET_EQUIV_RATIO"]
    # Alignment: the catalog channel's value (phi, Q14) is the word at offset 16.
    assert all(
        _word(p, COMMAND) == round(v * 0x4000)
        for p, v in zip(values["payload"], values["value_number"], strict=True)
    )
    table = Counter(
        (p[CUT_STATE], _word(p, COMMAND) == 0, channel.endswith("_SENTINEL"))
        for p, channel in zip(reads["payload"], reads["channel"], strict=True)
    )
    assert table == {(0x3F, True, True): 21, (0x00, False, False): 153}
    for neighbour in (CUT_STATE - 1, CUT_STATE + 1):
        by_cut = {
            cut: {p[neighbour] for p in reads["payload"] if (_word(p, COMMAND) == 0) == cut}
            for cut in (True, False)
        }
        assert by_cut[True] & by_cut[False], hex(WINDOW_START + neighbour)


def test_both_trims_sit_at_neutral_on_every_october_4_fuel_cut_read() -> None:
    """Consistent with, not qualifying, ``closed_loop_feedback_stft``: both
    short-term trims read exactly 0x6400 on every fuel-cut read and seldom
    otherwise. The code that holds them there is not traced."""
    reads = _command_reads()
    cut = [p for p in reads["payload"] if p[CUT_STATE] == 0x3F]
    running = [p for p in reads["payload"] if p[CUT_STATE] != 0x3F]
    assert (len(cut), len(running)) == (21, 153)
    assert {(_word(p, STFT_B1), _word(p, STFT_B2)) for p in cut} == {(NEUTRAL_TRIM, NEUTRAL_TRIM)}
    assert sum(_word(p, STFT_B1) == NEUTRAL_TRIM for p in running) == 13
    assert sum(_word(p, STFT_B2) == NEUTRAL_TRIM for p in running) == 12


def test_the_correction_gate_byte_is_set_on_almost_every_october_4_read() -> None:
    """0xFFFF26B9 bits 0 and 1 gate correction_175c off and the second
    0xFFFF26E8 term and the curve term on. 0x03 on 173 of 174 reads, while the
    command read exactly 0x4000 on 149 of the 153 non-cut reads: those terms
    were zero or cancelled there. Their factors were not read, so this
    qualifies nothing."""
    reads = _command_reads()
    assert Counter(p[GATE_26B9] for p in reads["payload"]) == {0x03: 173, 0x00: 1}
    running = [p for p in reads["payload"] if p[CUT_STATE] != 0x3F]
    assert sum(_word(p, COMMAND) == 0x4000 for p in running) == 149


def test_the_rev_limiter_alternate_flag_was_clear_all_october_4() -> None:
    """0xFFFF2841 bit 1 selects the 6,200 / 5,800 rpm limiter pair; it never
    set, so the 6,350 / 6,150 pair was in force all drive."""
    frame = _slice()
    flags = frame[frame["channel"] == "mem:ECM_PEDAL_ENRICH_FLAGS"]
    assert len(flags) == 191
    assert not any(p[0] & 0x02 for p in flags["payload"])
