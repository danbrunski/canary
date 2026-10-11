"""A03.2/A03.3 — the sequence feature set is a recorded choice, not a comment."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.models.sequence_feature_set import (
    ALL_CHANNELS,
    ENGINE_RESPONSE,
    FEATURE_SET_SCHEMA_KEY,
    context_columns_from_schema,
    load_vehicle_sequence_roles,
    resolve_feature_set,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
XTERRA_ROLES = REPO_ROOT / "data" / "nissan-xterra-2008" / "sequence_roles.yaml"
GROUND_TRUTH = REPO_ROOT / "tests" / "fixtures" / "ground_truth" / "expectations.yaml"
DEPLOYED_MANIFEST = (
    REPO_ROOT / "models" / "nissan-xterra-2008" / "lstm_autoencoder" / "model.manifest.json"
)


def _with_rolling(*bases: str) -> list[str]:
    return [f"{b}{s}" for b in bases for s in ("", "__mean", "__std", "__roc")]


def test_all_channels_is_the_deployed_shape_and_scores_everything() -> None:
    columns = _with_rolling("obd:RPM", "obd:BAROMETRIC_PRESSURE", "obd:MAF")
    resolved = resolve_feature_set(ALL_CHANNELS, columns)
    assert resolved.feature_columns == tuple(columns)
    assert resolved.context_columns == ()
    # A schema without the record is this set: nothing is context.
    assert context_columns_from_schema({}) == ()


def test_engine_response_conditions_on_inputs_and_scores_only_targets() -> None:
    roles = load_vehicle_sequence_roles(XTERRA_ROLES)
    columns = _with_rolling(
        "obd:RPM",
        "obd:BAROMETRIC_PRESSURE",
        "obd:COOLANT_TEMP",
        "decoded:ECM_COOLANT_TEMP",
        "decoded:ECM_COOLANT_TEMP_233",
        "obd:MAF",
        "obd:SHORT_FUEL_TRIM_1",
        "derived:FUEL_RATE_LPH",
        "decoded:TPMS_FL_PRESSURE",
        "decoded:MOTION_SIGNAL_590_2",
    )
    resolved = resolve_feature_set(ENGINE_RESPONSE, columns, vehicle_roles=roles)
    record = resolved.record

    # Inputs keep raw + __roc and are context; targets keep raw only.
    assert set(resolved.context_columns) == {
        "obd:RPM",
        "obd:RPM__roc",
        "obd:BAROMETRIC_PRESSURE",
        "obd:BAROMETRIC_PRESSURE__roc",
        "obd:COOLANT_TEMP",
        "obd:COOLANT_TEMP__roc",
    }
    targets = set(resolved.feature_columns) - set(resolved.context_columns)
    assert targets == {"obd:MAF", "obd:SHORT_FUEL_TRIM_1"}
    # BAROMETRIC_PRESSURE__std (the step detector that led every ranking) is gone.
    assert "obd:BAROMETRIC_PRESSURE__std" in record["dropped"]
    # One source per quantity: the confirmed coolant mirrors drop for the PID.
    assert "mirror of obd:COOLANT_TEMP" in record["dropped"]["decoded:ECM_COOLANT_TEMP_233"]
    assert "restates obd:MAF" in record["dropped"]["derived:FUEL_RATE_LPH"]
    assert "tire pressure" in record["dropped"]["decoded:TPMS_FL_PRESSURE"]
    assert record["unassigned"] == ["decoded:MOTION_SIGNAL_590_2"]
    assert context_columns_from_schema({FEATURE_SET_SCHEMA_KEY: record}) == tuple(
        resolved.context_columns
    )


def test_a_mirror_is_only_dropped_when_its_reference_is_present() -> None:
    roles = load_vehicle_sequence_roles(XTERRA_ROLES)
    resolved = resolve_feature_set(
        ENGINE_RESPONSE, ["decoded:ECM_RPM", "obd:MAF"], vehicle_roles=roles
    )
    # No obd:RPM in this corpus: the mirror is not dropped for an absent PID,
    # and having no role of its own it is named as unassigned.
    assert "mirror" not in resolved.record["dropped"].get("decoded:ECM_RPM", "")
    assert resolved.record["unassigned"] == ["decoded:ECM_RPM"]


def test_every_dropped_mirror_is_confirmed_by_the_ground_truth_suite() -> None:
    """ "Keep one source per physical quantity, chosen by the ground-truth
    suite": a broadcast channel may be dropped as a mirror of a PID only if
    ``unit_agreement`` confirms it 1:1 against that PID."""
    confirmed = {
        (entry["signal"].replace("can:", "decoded:", 1), entry["reference"])
        for entry in yaml.safe_load(GROUND_TRUTH.read_text())["unit_agreement"]
    }
    roles = load_vehicle_sequence_roles(XTERRA_ROLES)
    assert roles.mirrors, "the Xterra file declares mirrors"
    for mirror, reference in roles.mirrors.items():
        assert (mirror, reference) in confirmed, f"{mirror} -> {reference} is not confirmed"


def test_unknown_feature_set_and_a_set_without_targets_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown sequence feature set"):
        resolve_feature_set("everything_but", ["obd:RPM"])
    with pytest.raises(ValueError, match="leaves no target"):
        resolve_feature_set(ENGINE_RESPONSE, ["obd:RPM", "obd:SPEED"])


def test_engine_response_over_the_deployed_schema_names_every_column() -> None:
    """Resolved over the 2026-09-20 artifact's 361 columns, every column is
    kept or dropped with a reason — the record accounts for all of them."""
    if not DEPLOYED_MANIFEST.exists():
        pytest.skip("deployed manifest not present")
    columns = json.loads(DEPLOYED_MANIFEST.read_text())["schema"]["feature_columns"]
    resolved = resolve_feature_set(
        ENGINE_RESPONSE, columns, vehicle_roles=load_vehicle_sequence_roles(XTERRA_ROLES)
    )
    record = resolved.record
    assert len(resolved.feature_columns) + len(record["dropped"]) == len(columns)
    # Pinned so the evidence quoted in A03.2 cannot drift from the code: 80
    # kept (41 context, 39 target columns), 281 dropped with a reason, 13
    # unassigned broadcast channels, one unconfirmed same-quantity pair.
    # (A03.2 quoted 82/43/279: H01.1 took `gps:speed` and `gps:speed__roc`
    # out of the inputs — external sensors never condition a detector.)
    assert len(columns) == 361
    assert len(resolved.feature_columns) == 80
    assert len(resolved.context_columns) == 41
    assert len(record["targets"]) == 39
    assert len(record["dropped"]) == 281
    for gps_column in ("gps:speed", "gps:speed__mean", "gps:speed__std", "gps:speed__roc"):
        assert record["dropped"][gps_column].startswith("source_not_trainable")
    assert len(record["unassigned"]) == 13
    assert all(name.startswith("decoded:") for name in record["unassigned"])
    assert record["same_quantity_unconfirmed"] == {"engine_speed": ["did:ECM_RPM", "obd:RPM"]}
    assert "obd:BAROMETRIC_PRESSURE" in resolved.context_columns
    assert not any(c.startswith("decoded:ECM_COOLANT_TEMP") for c in resolved.feature_columns)


def test_the_source_gate_holds_in_the_feature_set_resolution(tmp_path: Path) -> None:
    """H01.1: an external-sensor column never conditions or scores a sequence
    model — refused outright by ``all_channels`` (whose promise is "every
    candidate is scored"), dropped with the named reason by ``engine_response``,
    and refused when a vehicle roles file assigns it a role."""
    with pytest.raises(ValueError, match="source may not train"):
        resolve_feature_set(ALL_CHANNELS, ["obd:RPM", "imu:ACCEL_X"])
    resolved = resolve_feature_set(
        ENGINE_RESPONSE, ["obd:RPM", "obd:MAF", "gps:speed", "sensor:FUEL_PRESSURE"]
    )
    assert "gps:speed" not in resolved.feature_columns
    assert resolved.record["dropped"]["sensor:FUEL_PRESSURE"].startswith("source_not_trainable")
    roles = tmp_path / "sequence_roles.yaml"
    roles.write_text("inputs:\n  vehicle_speed:\n    - gps:speed\n")
    with pytest.raises(ValueError, match="H00 decision 1"):
        load_vehicle_sequence_roles(roles)
