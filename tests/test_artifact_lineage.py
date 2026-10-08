"""Artifact input lineage against the catalog in force at build time (B08.1).

The cases are the shapes the 2026-10-04 CAN DID correction can take: a name
that moved to other bytes, a name migrated with its bytes, a decode that
changed under a name, a retired binding, a fallback that never fed anything,
and the road-speed negative control, which must not qualify as trim under any
name. Known answers come from the committed catalog history and the committed
DID speed slice, never from the current catalog alone.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.analysis.artifact_lineage import (
    Artifact,
    CatalogHistory,
    CatalogSnapshot,
    DerivedDefinitions,
    LineageContext,
    ObservationAliases,
    Rename,
    audit_artifact,
    behavior_artifacts,
    correction_ledger,
    derived_definitions_loader,
    load_catalog_history,
    load_observation_aliases,
    normalise_channel,
    parse_derived_definitions,
    parse_derived_sources,
    recheck_memory,
    recheck_mode01,
    redecode_raw_dids,
    session_obd_rows,
    stale_sidecar_labels,
    trim_qualification,
)
from src.analysis.compare import PairingPolicy, compare_channels
from src.signals.derived import DERIVED_CHANNEL_INPUTS, DERIVED_CHANNEL_OPTIONAL_INPUTS

REPO = Path(__file__).resolve().parents[1]
ALIASES = load_observation_aliases(REPO / "data/nissan-xterra-2008/observation_aliases.yaml")
SPEED_SLICE = REPO / "tests/fixtures/ground_truth/did-speed-slice"

BEFORE = datetime(2026, 10, 1, tzinfo=UTC)
CORRECTED = datetime(2026, 10, 4, 22, 16, tzinfo=UTC)
LATER = datetime(2026, 10, 5, tzinfo=UTC)


def _did(did: int, name: str, **decode: Any) -> dict[str, Any]:
    return {"did": did, "name": name, "byte_start": 0, "byte_length": 1, **decode}


def _binding(name: str, **spec: Any) -> dict[str, Any]:
    return {"name": name, "can_id": 0x233, "byte_start": 2, "byte_length": 1, **spec}


#: The two CAN catalogs either side of c897a62, reduced to what matters.
OLD_DIDS = [
    _did(0x1102, "ECM_LONG_FUEL_TRIM_B1", byte_length=2, scale=1.0, offset=0.0),
    _did(0x1125, "ECM_LONG_FUEL_TRIM_B2", scale=1.0, offset=0.0),
    _did(0x1208, "ECM_INJECTOR_PW_B1"),
    _did(0x1206, "ECM_INJECTOR_PULSE_1", byte_length=2, scale=None, offset=None),
]
NEW_DIDS = [
    _did(0x1102, "ECM_VEHICLE_SPEED_COARSE", scale=2.0, offset=0.0),
    _did(0x1125, "ECM_LONG_FUEL_TRIM_B1", scale=1.0, offset=-100.0),
    _did(0x1126, "ECM_LONG_FUEL_TRIM_B2", scale=1.0, offset=-100.0),
    _did(0x1208, "ECM_AIR_CHARGE"),
    _did(0x1206, "ECM_INJECTOR_PULSE_1", byte_length=2, scale=0.01, offset=0.0),
]


def _snapshot(
    label: str,
    at: datetime | None,
    dids: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    memory: list[dict[str, Any]] | None = None,
) -> CatalogSnapshot:
    return CatalogSnapshot.from_documents(
        label, at, {"dids": dids, "memory": memory or []}, {"signals": bindings}
    )


#: The derived inputs declared now, standing in for the build-time source.
NOW_DERIVED = {name: tuple(map(tuple, groups)) for name, groups in DERIVED_CHANNEL_INPUTS.items()}
NOW_OPTIONAL = {name: tuple(c) for name, c in DERIVED_CHANNEL_OPTIONAL_INPUTS.items()}


def _ctx(
    snapshots: list[CatalogSnapshot],
    current: CatalogSnapshot,
    *,
    aliases: ObservationAliases = ALIASES,
    derived_at_build: dict[str, tuple[tuple[str, ...], ...]] | None = None,
    optional_at_build: dict[str, tuple[str, ...]] | None = None,
) -> LineageContext:
    build_defs = DerivedDefinitions(
        "test",
        NOW_DERIVED if derived_at_build is None else derived_at_build,
        NOW_OPTIONAL if optional_at_build is None else optional_at_build,
    )
    return LineageContext(
        history=CatalogHistory(tuple(snapshots), current),
        renames=(Rename("did", "ECM_INJECTOR_PW_B1", "ECM_AIR_CHARGE", "2026-09-18.yaml"),),
        aliases=aliases,
        derived_inputs=NOW_DERIVED,
        derived_optional_inputs=NOW_OPTIONAL,
        derived_at=lambda _when: build_defs,
    )


def _artifact(inputs: tuple[str, ...], built: datetime | None = BEFORE) -> Artifact:
    return Artifact("test", "behavior_model", "m", built, "fit.fitted_at", inputs, ())


OLD = _snapshot("old", datetime(2026, 9, 1, tzinfo=UTC), OLD_DIDS, [_binding("B", scale=None)])
NEW = _snapshot("new", CORRECTED, NEW_DIDS, [_binding("B", scale=None, offset=0.0)])
CURRENT = _snapshot("working-tree", None, NEW_DIDS, [_binding("B", scale=None, offset=0.0)])
CTX = _ctx([OLD, NEW], CURRENT)


def _only(lineage: Any, channel: str) -> Any:
    found = [leaf for leaf in lineage.leaves if leaf.channel == channel]
    assert len(found) == 1, found
    return found[0]


def test_a_name_that_moved_to_other_bytes_is_affected() -> None:
    """Built before c897a62, ``ECM_LONG_FUEL_TRIM_B1`` read DID 0x1102 (road speed)."""
    lineage = audit_artifact(_artifact(("did:ECM_LONG_FUEL_TRIM_B1",), BEFORE), CTX)

    leaf = _only(lineage, "did:ECM_LONG_FUEL_TRIM_B1")
    assert lineage.verdict == "affected"
    assert leaf.status == "identity_changed"
    assert leaf.identity_at_build.startswith("DID 0x1102")
    assert leaf.identity_now.startswith("DID 0x1125")
    assert "ECM_VEHICLE_SPEED_COARSE" in " ".join(leaf.notes)


def test_the_same_name_built_after_the_correction_is_unaffected() -> None:
    lineage = audit_artifact(_artifact(("did:ECM_LONG_FUEL_TRIM_B1",), LATER), CTX)

    assert lineage.verdict == "unaffected"
    assert _only(lineage, "did:ECM_LONG_FUEL_TRIM_B1").identity_at_build.startswith("DID 0x1125")


def test_a_migrated_name_keeps_its_bytes_and_is_unaffected() -> None:
    """The 2026-09-18 relabel rewrote stored names; the DID number did not move."""
    lineage = audit_artifact(_artifact(("did:ECM_AIR_CHARGE",), BEFORE), CTX)

    leaf = _only(lineage, "did:ECM_AIR_CHARGE")
    assert lineage.verdict == "unaffected"
    assert leaf.identity_at_build == "DID 0x1208 ECM_INJECTOR_PW_B1"
    assert any("migrated from ECM_INJECTOR_PW_B1" in note for note in leaf.notes)


def test_a_pinned_scale_is_a_decode_change_but_a_null_offset_is_not() -> None:
    """Raw counts became milliseconds; ``offset: null -> 0.0`` under a null scale is nothing."""
    lineage = audit_artifact(_artifact(("did:ECM_INJECTOR_PULSE_1", "decoded:B"), BEFORE), CTX)

    assert _only(lineage, "did:ECM_INJECTOR_PULSE_1").status == "decode_changed"
    assert _only(lineage, "decoded:B").status == "unchanged"
    assert lineage.verdict == "affected"


def test_mode01_total_trim_resolves_to_pids_and_qualifies_as_trim() -> None:
    """Current ``TOTAL_TRIM_B1`` consumes Mode 01 STFT/LTFT; no DID is involved."""
    lineage = audit_artifact(_artifact(("derived:TOTAL_TRIM_B1",), BEFORE), CTX)

    labels = {leaf.identity_at_build for leaf in lineage.leaves}
    assert labels == {
        "Mode 01 PID 0x06 SHORT_FUEL_TRIM_1",
        "Mode 01 PID 0x07 LONG_FUEL_TRIM_1",
    }
    assert all(leaf.via == ("derived:TOTAL_TRIM_B1",) for leaf in lineage.leaves)
    assert all(any("trim qualifies" in n for n in leaf.notes) for leaf in lineage.leaves)
    assert lineage.verdict == "unaffected"


def test_road_speed_cannot_qualify_as_trim_under_any_name() -> None:
    """The negative control: the bytes decide, not the label."""
    assert trim_qualification(("did", 0x1102), "fuel_trim_long_b1", ALIASES)[0] is False
    assert trim_qualification(("mode01", 0x0D), "fuel_trim_long_b1", ALIASES)[0] is False
    assert trim_qualification(("did", 0x1125), "fuel_trim_long_b1", ALIASES)[0] is True
    assert trim_qualification(("mode01", 0x07), "fuel_trim_long_b1", ALIASES)[0] is True

    # A catalog that never corrected 0x1102: the name says trim at build AND
    # now, so no identity change is visible. The register still refuses it.
    uncorrected = _snapshot("u", datetime(2026, 9, 1, tzinfo=UTC), OLD_DIDS, [])
    ctx = _ctx([uncorrected], _snapshot("now", None, OLD_DIDS, []))
    lineage = audit_artifact(_artifact(("did:ECM_LONG_FUEL_TRIM_B1",), BEFORE), ctx)

    leaf = _only(lineage, "did:ECM_LONG_FUEL_TRIM_B1")
    assert leaf.status == "trim_identity_refused"
    assert "road_speed" in " ".join(leaf.notes)
    assert lineage.verdict == "affected"


def test_a_retired_binding_is_affected_and_an_unused_fallback_is_not() -> None:
    old = _snapshot("o", datetime(2026, 7, 1, tzinfo=UTC), [], [_binding("CALC_LOAD_B", scale=1.0)])
    ctx = _ctx([old], _snapshot("now", None, [], []))
    lineage = audit_artifact(
        _artifact(("decoded:CALC_LOAD_B", "derived:AIR_MASS_PER_CYCLE"), BEFORE), ctx
    )

    assert _only(lineage, "decoded:CALC_LOAD_B").status == "retired"
    # derived:AIR_MASS_PER_CYCLE falls back to decoded:ECM_RPM, which this
    # catalog never held: nothing could have been decoded under it.
    assert _only(lineage, "decoded:ECM_RPM").status == "never_catalogued"
    assert lineage.verdict == "affected"
    assert lineage.reasons == ("decoded:CALC_LOAD_B: retired",)


def test_a_binding_that_changed_inside_the_recording_window_is_unverifiable() -> None:
    """Before decode-on-read, a trainer read rows the Pi decoded at record time."""
    first = _snapshot("a", datetime(2026, 8, 1, tzinfo=UTC), [], [_binding("B", scale=1.0)])
    second = _snapshot("b", datetime(2026, 8, 10, tzinfo=UTC), [], [_binding("B", scale=2.0)])
    third = _snapshot("c", datetime(2026, 8, 20, tzinfo=UTC), [], [_binding("B", scale=1.0)])
    ctx = _ctx([first, second, third], _snapshot("now", None, [], [_binding("B", scale=1.0)]))
    artifact = Artifact(
        "t", "detector_manifest", "m", datetime(2026, 8, 25, tzinfo=UTC), "created_at",
        ("decoded:B",), ("2026-08-05T120000",),
    )  # fmt: skip

    lineage = audit_artifact(artifact, ctx)

    assert _only(lineage, "decoded:B").status == "as_recorded_varied"
    assert lineage.verdict == "unverifiable"


def test_unknown_build_time_is_unverifiable_not_clean() -> None:
    lineage = audit_artifact(_artifact(("obd:RPM",), None), CTX)
    assert lineage.verdict == "unverifiable"


def test_feature_columns_normalise_to_their_channel() -> None:
    assert normalise_channel("did:ECM_AIR_CHARGE__mean") == "did:ECM_AIR_CHARGE"
    assert normalise_channel("obd:mem:ECM_TARGET_EQUIV_RATIO__roc") == "mem:ECM_TARGET_EQUIV_RATIO"
    assert normalise_channel("derived:TOTAL_TRIM_B1__gap") == "derived:TOTAL_TRIM_B1"
    assert normalise_channel("mode:idle") is None


def test_behavior_artifact_inputs_come_from_the_recipe_not_the_fit(tmp_path: Path) -> None:
    path = tmp_path / "behavior_models.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "models": [
                    {
                        "name": "total_trim",
                        "output": "derived:TOTAL_TRIM_B1",
                        "axes": [{"channel": "obd:RPM"}, {"channel": "obd:ENGINE_LOAD"}],
                        "eligibility": {"engine_state": {"rpm_channel": "obd:RPM"}},
                        "fit": {
                            "fitted_at": "2026-09-24T01:55:23+00:00",
                            "sessions": ["2026-09-21T194247", "2026-09-21T201836#segment-001"],
                            "note": "obd:NOT_AN_INPUT",
                        },
                    }
                ]
            }
        )
    )
    (artifact,) = behavior_artifacts(path)
    assert artifact.inputs == ("derived:TOTAL_TRIM_B1", "obd:ENGINE_LOAD", "obd:RPM")
    assert artifact.sessions == ("2026-09-21T194247", "2026-09-21T201836")
    assert artifact.built_at == datetime(2026, 9, 24, 1, 55, 23, tzinfo=UTC)


def test_mode01_payloads_are_rechecked_against_the_pid_table() -> None:
    rows = [
        ("LONG_FUEL_TRIM_1", b"\x80", 0.0, None),
        ("SHORT_FUEL_TRIM_2", b"\x79", -5.46875, None),
        ("SHORT_FUEL_TRIM_1", b"\x90", 0.0, None),  # stored value disagrees
        ("LONG_FUEL_TRIM_2", None, 1.5, None),  # python-OBD era: no payload
    ]
    check = recheck_mode01(rows)
    assert (check.rows, check.with_payload, check.agree, check.disagree) == (4, 3, 2, 1)


def test_the_old_two_byte_entry_decodes_nothing_from_a_one_byte_can_reply() -> None:
    rows = [("DID_1102", None, None, "2f"), ("DID_1125", None, None, "64")]
    out = redecode_raw_dids(rows, OLD, CURRENT)

    assert out["DID_1102"]["build_decoded"] == 0
    assert out["DID_1102"]["current_range"] == [94.0, 94.0]
    # 0x1125: "LTFT B2 = 100 %" under the old entry, "LTFT B1 = 0 %" now.
    assert out["DID_1125"]["build_range"] == [100.0, 100.0]
    assert out["DID_1125"]["current_range"] == [0.0, 0.0]


def test_a_sidecar_name_that_now_reads_other_bytes_is_reported() -> None:
    sidecar = {
        "resolved_polling_plan": {
            "members": [
                {"identifier": "0x1102", "kind": "did", "profile_channel": "ECM_LONG_FUEL_TRIM_B1"},
                {"identifier": "0x1125", "kind": "did", "profile_channel": "ECM_LONG_FUEL_TRIM_B1"},
            ]
        },
        "channels": [{"channel": "ECM_LONG_FUEL_TRIM_B2", "transport_channel": "DID_1125"}],
    }
    found = stale_sidecar_labels(sidecar, CURRENT)
    assert [(f["request"], f["stored_name"]) for f in found] == [
        ("DID 0x1102", "ECM_LONG_FUEL_TRIM_B1"),
        ("DID 0x1125", "ECM_LONG_FUEL_TRIM_B2"),
    ]
    assert found[0]["request_is_now"] == "ECM_VEHICLE_SPEED_COARSE"


def test_a_bank_swap_cannot_qualify_either() -> None:
    """The correction's other half: 0x1125 was labelled LTFT B2 and reads the B1 word."""
    assert trim_qualification(("did", 0x1125), "fuel_trim_long_b2", ALIASES)[0] is False
    assert trim_qualification(("mode01", 0x09), "fuel_trim_long_b1", ALIASES)[0] is False
    assert trim_qualification(("mode01", 0x06), "fuel_trim_long_b1", ALIASES)[0] is False

    # The uncorrected catalog: B2 -> 0x1125 at build and now, so no identity
    # change is visible. The register is bank 1, so the claim is refused.
    uncorrected = _snapshot("u", datetime(2026, 9, 1, tzinfo=UTC), OLD_DIDS, [])
    ctx = _ctx([uncorrected], _snapshot("now", None, OLD_DIDS, []))
    lineage = audit_artifact(_artifact(("did:ECM_LONG_FUEL_TRIM_B2",), BEFORE), ctx)

    leaf = _only(lineage, "did:ECM_LONG_FUEL_TRIM_B2")
    assert leaf.status == "trim_identity_refused"
    assert "(fuel_trim_long_b1), not the claimed fuel_trim_long_b2" in " ".join(leaf.notes)
    assert lineage.verdict == "affected"


def _mem(name: str, **spec: Any) -> dict[str, Any]:
    return {"name": name, "address": 0xFFFF11C7, "length": 1, "byte_length": 1, **spec}


def test_a_ram_decode_that_changed_inside_the_recording_window_is_not_unaffected() -> None:
    """RAM values are stored decoded at record time, at every build date."""
    first = _snapshot("a", datetime(2026, 10, 1, tzinfo=UTC), [], [], [_mem("T", scale=1.0)])
    second = _snapshot("b", datetime(2026, 10, 2, tzinfo=UTC), [], [], [_mem("T", scale=2.0)])
    third = _snapshot("c", datetime(2026, 10, 3, tzinfo=UTC), [], [], [_mem("T", scale=1.0)])
    ctx = _ctx([first, second, third], _snapshot("now", None, [], [], [_mem("T", scale=1.0)]))
    artifact = Artifact(
        "t", "detector_manifest", "m", datetime(2026, 10, 4, tzinfo=UTC), "created_at",
        ("mem:T",), ("2026-10-01T120000",),
    )  # fmt: skip

    lineage = audit_artifact(artifact, ctx)

    assert _only(lineage, "mem:T").status == "as_recorded_varied"
    assert lineage.verdict == "unverifiable"

    # One decode across the window: unchanged. No dated session: unknown.
    steady = _ctx([first], _snapshot("now", None, [], [], [_mem("T", scale=1.0)]))
    assert audit_artifact(artifact, steady).verdict == "unaffected"
    undated = Artifact(
        "t", "detector_manifest", "m", artifact.built_at, "created_at", ("mem:T",), ()
    )
    assert _only(audit_artifact(undated, steady), "mem:T").status == "as_recorded_unknown"


def test_stored_ram_windows_are_re_decoded_then_and_now() -> None:
    window = [
        {**_mem("A", scale=1.0), "address": 0xFFFF1000, "window": "w"},
        {**_mem("B", scale=1.0), "address": 0xFFFF1002, "window": "w"},
    ]
    recorded = _snapshot("r", datetime(2026, 10, 1, tzinfo=UTC), [], [], window)
    now_same = _snapshot("now", None, [], [], window)
    now_doubled = _snapshot("now", None, [], [], [window[0], {**window[1], "scale": 2.0}])
    rows = [
        ("mem:B", bytes([9, 9, 7]), 7.0, None),  # B at offset 2 of the window
        ("mem:B", bytes([5]), 5.0, None),  # B read alone (offset 0)
        ("mem:B", bytes([1, 2, 3]), 99.0, None),  # no offset reproduces it
    ]

    same = recheck_memory(rows, "B", recorded, now_same, ())
    doubled = recheck_memory(rows, "B", recorded, now_doubled, ())

    assert (same.reproduced_at_recording, same.agree_now, same.offset_unresolved) == (2, 2, 1)
    assert (doubled.agree_now, doubled.disagree_now) == (0, 2)


def test_derived_inputs_come_from_the_build_time_definition() -> None:
    """Expansion uses what was committed at build; a missing or changed definition shows."""
    then = {"derived:TOTAL_TRIM_B1": (("obd:SHORT_FUEL_TRIM_1",), ("did:ECM_LONG_FUEL_TRIM_B1",))}
    ctx = _ctx([OLD, NEW], CURRENT, derived_at_build=then)

    changed = audit_artifact(_artifact(("derived:TOTAL_TRIM_B1",), BEFORE), ctx)
    statuses = {leaf.channel: leaf.status for leaf in changed.leaves}
    assert statuses["derived:TOTAL_TRIM_B1"] == "definition_changed"
    assert statuses["did:ECM_LONG_FUEL_TRIM_B1"] == "identity_changed"
    assert changed.verdict == "affected"

    unlocated = audit_artifact(_artifact(("derived:FUEL_RATE_LPH",), BEFORE), ctx)
    assert [leaf.status for leaf in unlocated.leaves] == ["definition_unlocated"]
    assert unlocated.verdict == "unverifiable"


def test_the_definition_parser_reads_every_historical_form() -> None:
    source = """
VE = "derived:VE_PERCENT"
DERIVED_CHANNEL_INPUTS: dict = {
    "derived:AIR_MASS_PER_CYCLE": (("obd:MAF",), ("obd:RPM", "decoded:ECM_RPM")),
    **{s.name: s.input_groups for s in ANOMALY_SIGNALS},
}
SIGNALS = (
    DerivedSignal(
        "derived:TOTAL_TRIM_B1", (("obd:SHORT_FUEL_TRIM_1",), ("obd:LONG_FUEL_TRIM_1",)), f
    ),
)
def compute(out):
    _append_dependency_gap(out, VE, ("derived:AIR_MASS_PER_CYCLE",), ("obd:BAROMETRIC_PRESSURE",))
    _append_dependency_gap(out, "derived:X", *DERIVED_CHANNEL_INPUTS["derived:AIR_MASS_PER_CYCLE"])
    out["derived:FUEL_RATE_LPH"] = _series(out, "obd:MAF")
"""
    parsed = parse_derived_definitions(source)
    assert parsed == {
        "derived:AIR_MASS_PER_CYCLE": (("obd:MAF",), ("obd:RPM", "decoded:ECM_RPM")),
        "derived:TOTAL_TRIM_B1": (("obd:SHORT_FUEL_TRIM_1",), ("obd:LONG_FUEL_TRIM_1",)),
        "derived:VE_PERCENT": (("derived:AIR_MASS_PER_CYCLE",), ("obd:BAROMETRIC_PRESSURE",)),
    }

    # On today's source every literal declaration matches the imported one.
    current = parse_derived_definitions((REPO / "src/signals/derived.py").read_text())
    assert current["derived:TOTAL_TRIM_B1"] == NOW_DERIVED["derived:TOTAL_TRIM_B1"]
    assert all(NOW_DERIVED[name] == groups for name, groups in current.items())


def test_vehicle_module_literals_are_parsed_and_merged_at_the_build_revision(
    tmp_path: Path,
) -> None:
    """B08.1: a vehicle-named channel is located from its vehicle module's
    literal table at the build's commit, merged across vehicles in name order;
    optional inputs are located too."""
    derived_source = """
DERIVED_CHANNEL_OPTIONAL_INPUTS = {"derived:FUEL_RATE_LPH": ("obd:INTAKE_TEMP",)}
DERIVED_CHANNEL_INPUTS = {"derived:FUEL_RATE_LPH": (("obd:MAF",),)}
"""
    vehicle_a = """
SIGNAL_INPUTS = {"derived:AFR_BANK_BALANCE": (("did:A_V1",), ("did:A_V2",))}
DERIVED_CHANNEL_INPUTS = {"derived:AFR_B1": (("obd:O2_WR_LAMBDA_S1", "did:A_V1"),)}
DERIVED_CHANNEL_OPTIONAL_INPUTS = {"derived:AFR_B1": ("obd:BAROMETRIC_PRESSURE",)}
"""
    vehicle_b = """
DERIVED_CHANNEL_INPUTS = {"derived:AFR_B1": (("obd:O2_WR_LAMBDA_S1", "did:B_V1"),)}
"""
    expected_inputs = {
        "derived:FUEL_RATE_LPH": (("obd:MAF",),),
        "derived:AFR_BANK_BALANCE": (("did:A_V1",), ("did:A_V2",)),
        "derived:AFR_B1": (("obd:O2_WR_LAMBDA_S1", "did:A_V1", "did:B_V1"),),
    }
    expected_optional = {
        "derived:FUEL_RATE_LPH": ("obd:INTAKE_TEMP",),
        "derived:AFR_B1": ("obd:BAROMETRIC_PRESSURE",),
    }
    parsed = parse_derived_sources(derived_source, [vehicle_a, vehicle_b])
    assert dict(parsed.inputs) == expected_inputs
    assert dict(parsed.optional) == expected_optional

    # The same through git, at a revision, without importing anything.
    package = tmp_path / "src/signals/vehicle_signals"
    package.mkdir(parents=True)
    (tmp_path / "src/signals/derived.py").write_text(derived_source)
    (package / "__init__.py").write_text("DERIVED_CHANNEL_INPUTS = {}\n")
    (package / "vehicle_b.py").write_text(vehicle_b)
    (package / "vehicle_a.py").write_text(vehicle_a)
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "build"], check=True)
    definitions = derived_definitions_loader(tmp_path)(datetime.now(UTC))
    assert definitions is not None
    assert dict(definitions.inputs) == expected_inputs
    assert dict(definitions.optional) == expected_optional


def test_optional_inputs_are_expanded_and_compared() -> None:
    """An optional input is an input: its leaf is resolved, and a different
    optional set at build is a changed definition."""
    channel = "derived:FUEL_RATE_LPH"
    lineage = audit_artifact(_artifact((channel,), BEFORE), CTX)
    leaves = {leaf.channel for leaf in lineage.leaves}
    assert set(DERIVED_CHANNEL_OPTIONAL_INPUTS[channel]) <= leaves
    assert lineage.verdict != "affected"

    then = {**NOW_OPTIONAL, channel: ("obd:INTAKE_TEMP",)}
    changed = audit_artifact(
        _artifact((channel,), BEFORE), _ctx([OLD, NEW], CURRENT, optional_at_build=then)
    )
    assert _only(changed, channel).status == "definition_changed"
    assert changed.verdict == "affected"


def test_mode01_channels_resolve_through_the_registry() -> None:
    lineage = audit_artifact(
        _artifact(("obd:ENGINE_FUEL_RATE", "obd:MIL_STATUS", "obd:NOT_A_PID"), BEFORE), CTX
    )
    assert _only(lineage, "obd:ENGINE_FUEL_RATE").identity_at_build == (
        "Mode 01 PID 0x5E ENGINE_FUEL_RATE"
    )
    assert _only(lineage, "obd:MIL_STATUS").kind == "obd_service"
    assert _only(lineage, "obd:NOT_A_PID").status == "unresolved_at_build"
    assert lineage.verdict == "unverifiable"


def test_agreement_kinds_and_trim_claims_are_validated(tmp_path: Path) -> None:
    path = tmp_path / "aliases.yaml"
    path.write_text(
        "dependent_relations:\n"
        "  - identities: [{kind: mode01, pid: 0x44}]\n"
        "    agreement_counts_as: algorithm_validation\n"
    )
    with pytest.raises(ValueError, match="agreement_counts_as"):
        load_observation_aliases(path)
    path.write_text("trim_claims: {obd:X: fuel_trim_long_b3}\n")
    with pytest.raises(ValueError, match="trim_claims"):
        load_observation_aliases(path)


def test_every_trim_channel_in_the_catalogs_declares_its_claim() -> None:
    """Claims are data; this keeps a new trim PID or DID from going unchecked."""
    from src.analysis.artifact_lineage import MODE01_PIDS

    dids = yaml.safe_load((REPO / "data/dids.yaml").read_text())["dids"]
    names = [f"did:{e['name']}" for e in dids if "FUEL_TRIM" in e["name"]]
    names += [f"obd:{n}" for n in MODE01_PIDS if "FUEL_TRIM" in n]
    assert names
    assert sorted(set(names) - set(ALIASES.trim_claims)) == []


def test_shipped_aliases_agree_with_the_current_catalog() -> None:
    """Every DID in a shared register exists, and its unit matches the register."""
    dids = {e["did"]: e for e in yaml.safe_load((REPO / "data/dids.yaml").read_text())["dids"]}
    units = {"road_speed": "km/h"}
    for register in ALIASES.registers:
        for kind, value in register.identities:
            if kind != "did":
                continue
            entry = dids[value]
            expected = units.get(register.quantity, "percent")
            assert entry["unit"] == expected, (hex(value), register.quantity, entry["unit"])
    relation = ALIASES.relations_of(("mode01", 0x44))
    assert [r.agreement_counts_as for r in relation] == ["decode_verification"]


def test_the_committed_slice_is_road_speed_and_old_bytes_decode_nothing() -> None:
    """Data-level negative control on real bytes: DID 0x1102 tracks wheel speed 1:1."""
    rows = session_obd_rows(SPEED_SLICE, ["DID_1102"])
    assert rows, "the speed slice carries raw DID 0x1102 replies"
    summary = redecode_raw_dids(rows, OLD, CURRENT)["DID_1102"]
    assert summary["reply_bytes"] == [1]
    assert summary["build_decoded"] == 0

    result = compare_channels(
        SPEED_SLICE,
        "did:ECM_VEHICLE_SPEED_COARSE",
        "can:WHEEL_SPEED_6F7_A",
        policy=PairingPolicy.within("250ms"),
    )
    assert result.r_squared > 0.99
    assert abs(result.slope - 1.0) < 0.01
    assert trim_qualification(("did", 0x1102), "fuel_trim_long_b1", ALIASES)[0] is False


def test_the_ledger_names_both_sides_of_the_correction() -> None:
    events = correction_ledger(CatalogHistory((OLD, NEW), CURRENT))

    by_key = {(e["catalog"], e["key"]): e for e in events}
    assert by_key[("did", "0x1102")]["name_before"] == "ECM_LONG_FUEL_TRIM_B1"
    assert by_key[("did", "0x1102")]["name_after"] == "ECM_VEHICLE_SPEED_COARSE"
    assert by_key[("did", "ECM_LONG_FUEL_TRIM_B1")]["name_now_reads"] == "0x1125"
    assert by_key[("did", "0x1206")]["decode_changed"] == ["scale", "offset"]
    # offset null -> 0.0 under a null scale is not a decode change.
    assert ("binding", "B") not in by_key


def _has_history() -> bool:
    shallow = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--is-shallow-repository"],
        capture_output=True,
        text=True,
        check=False,
    )
    return shallow.returncode == 0 and shallow.stdout.strip() == "false"


@pytest.mark.skipif(not _has_history(), reason="needs full git history (CI checks out shallow)")
def test_committed_history_reproduces_the_october_correction() -> None:
    """Known answer from git: the name moved from 0x1102 to 0x1125 at c897a62."""
    history = load_catalog_history(REPO, since=datetime(2026, 10, 1, tzinfo=UTC))

    before = history.at(datetime(2026, 10, 4, 12, tzinfo=UTC))
    after = history.at(datetime(2026, 10, 4, 23, tzinfo=UTC))
    assert before is not None and after is not None
    assert before.dids["ECM_LONG_FUEL_TRIM_B1"]["did"] == 0x1102
    assert after.dids["ECM_LONG_FUEL_TRIM_B1"]["did"] == 0x1125
    assert after.label == "c897a62"
