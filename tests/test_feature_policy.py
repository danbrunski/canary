"""Trainability policy: which sourced channels may become model features.

The 2026-09-20 retrain fixed false alarms and went nearly blind (IF fault recall
4-16 %). Part of the cause was the feature set: 16 ``decoded:`` bindings at
confidence low/hypothesized, brake/steering/HVAC/TPMS body channels, the fuel
tank level, per-cylinder misfire counters, and two ``did:`` channels the serving
capture profile never polls. Every gate here lives in
``feature_pipeline.candidate_feature_columns`` — the one function both detectors
select through — so none of it can be skipped by a caller.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.collection.capture_profiles import (
    DEFAULT_PROFILE_NAME,
    CaptureProfile,
    CaptureProfileCatalog,
    CaptureProfileSources,
    save_capture_profiles,
    seed_catalog,
    serving_mode22_dids,
)
from src.columns import GAP_SUFFIX, MODE_COLUMN
from src.config import CanaryConfig
from src.decode.bindings import (
    TRAINABLE_CONFIDENCES,
    Bindings,
    Signal,
    is_trainable_binding,
    load_bindings,
)
from src.decode.dids import load_repo_did_map
from src.features.feature_pipeline import (
    MEMORY_CHANNEL_EXCLUSION,
    SEGMENTER_SIGNAL_COLUMNS,
    FeaturePolicy,
    build_feature_spec,
    candidate_feature_columns,
    feature_policy_exclusions,
    isolation_forest_feature_columns,
    resolve_feature_policy,
    sequence_feature_columns,
)
from src.models.isolation_forest import feature_policy_violations

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_BINDINGS = REPO_ROOT / "data" / "bindings.yaml"
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"
#: A serving profile that polls every supported Mode 01 PID (``AUTO``).
EVERY_PID = frozenset({"AUTO"})


def _signal(name: str, confidence: str, **extra: object) -> Signal:
    return Signal(
        name=name,
        module="ECM",
        can_id=0x100,
        byte_start=0,
        byte_length=1,
        confidence=confidence,  # type: ignore[arg-type]
        **extra,  # type: ignore[arg-type]
    )


def _bindings(*signals: Signal) -> Bindings:
    return Bindings(schema_version=1, signals=list(signals))


def _frame(*bases: str, rows: int = 6) -> pd.DataFrame:
    """A feature frame carrying each base channel plus its rolling siblings."""
    data: dict[str, object] = {
        "session": ["s1"] * rows,
        "timestamp": pd.date_range("2026-09-20T12:00:00Z", periods=rows, freq="1s"),
        MODE_COLUMN: ["city"] * rows,
    }
    for offset, base in enumerate(bases):
        values = [float(offset + i) for i in range(rows)]
        for suffix in ("", "__mean", "__std", "__roc"):
            data[f"{base}{suffix}"] = values
        data[f"{base}{GAP_SUFFIX}"] = [False] * rows
    return pd.DataFrame(data)


def _family(base: str) -> set[str]:
    return {base, f"{base}__mean", f"{base}__std", f"{base}__roc"}


# ---------------------------------------------------------------------------
# 1. binding-confidence gate
# ---------------------------------------------------------------------------


def test_hypothesized_binding_is_not_a_candidate_and_a_high_one_is() -> None:
    policy = FeaturePolicy(
        bindings=_bindings(_signal("GUESS", "hypothesized"), _signal("SOLID", "high")),
        serving_mode01=EVERY_PID,
        serving_dids=frozenset(),
        did_map=load_repo_did_map(),
    )
    cols = set(candidate_feature_columns(_frame("decoded:GUESS", "decoded:SOLID"), policy=policy))
    assert cols == _family("decoded:SOLID")  # raw AND __mean/__std/__roc gated together


@pytest.mark.parametrize("confidence", ["medium", "low", "hypothesized"])
def test_every_confidence_below_high_is_gated(confidence: str) -> None:
    policy = FeaturePolicy(
        bindings=_bindings(_signal("X", confidence)),
        serving_mode01=EVERY_PID,
        serving_dids=frozenset(),
        did_map=load_repo_did_map(),
    )
    assert candidate_feature_columns(_frame("decoded:X"), policy=policy) == []
    assert confidence not in TRAINABLE_CONFIDENCES


def test_confirmed_binding_is_a_candidate() -> None:
    policy = FeaturePolicy(
        bindings=_bindings(_signal("X", "confirmed")),
        serving_mode01=EVERY_PID,
        serving_dids=frozenset(),
        did_map=load_repo_did_map(),
    )
    assert set(candidate_feature_columns(_frame("decoded:X"), policy=policy)) == _family(
        "decoded:X"
    )


def test_a_binding_correction_removes_the_feature_rather_than_blinding_the_model() -> None:
    """The failure this gate exists for: a binding is demoted after training.
    With the gate, the demoted channel simply stops being selected next time."""
    frame = _frame("decoded:X")
    before = FeaturePolicy(
        bindings=_bindings(_signal("X", "high")),
        serving_mode01=EVERY_PID,
        serving_dids=frozenset(),
        did_map=load_repo_did_map(),
    )
    after = FeaturePolicy(
        bindings=_bindings(_signal("X", "low")),
        serving_mode01=EVERY_PID,
        serving_dids=frozenset(),
        did_map=load_repo_did_map(),
    )
    assert candidate_feature_columns(frame, policy=before)
    assert candidate_feature_columns(frame, policy=after) == []


def test_decoded_column_absent_from_the_catalog_is_not_a_candidate() -> None:
    policy = FeaturePolicy(
        bindings=_bindings(_signal("X", "high")),
        serving_mode01=EVERY_PID,
        serving_dids=frozenset(),
        did_map=load_repo_did_map(),
    )
    frame = _frame("decoded:NOT_A_BINDING")
    assert candidate_feature_columns(frame, policy=policy) == []
    assert feature_policy_exclusions(frame, policy) == {
        "decoded:NOT_A_BINDING": "not in the bindings catalog"
    }


def test_no_catalog_means_no_decoded_features() -> None:
    policy = FeaturePolicy(
        bindings=None,
        serving_mode01=EVERY_PID,
        serving_dids=frozenset(),
        did_map=load_repo_did_map(),
    )
    frame = _frame("decoded:X", "obd:RPM")
    assert set(candidate_feature_columns(frame, policy=policy)) == _family("obd:RPM")
    assert feature_policy_exclusions(frame, policy) == {"decoded:X": "no bindings catalog loaded"}


def test_dbc_copy_of_a_gated_binding_is_gated_but_external_dbc_trains() -> None:
    policy = FeaturePolicy(
        bindings=_bindings(_signal("GUESS", "low")),
        serving_mode01=EVERY_PID,
        serving_dids=frozenset(),
        did_map=load_repo_did_map(),
    )
    cols = set(candidate_feature_columns(_frame("dbc:GUESS", "dbc:VENDOR_ONLY"), policy=policy))
    assert cols == _family("dbc:VENDOR_ONLY")


# ---------------------------------------------------------------------------
# 2. per-signal opt-out in the data file
# ---------------------------------------------------------------------------


def test_model_feature_false_excludes_a_high_confidence_binding() -> None:
    brake = _signal("BRAKE", "high", model_feature=False)
    policy = FeaturePolicy(
        bindings=_bindings(brake, _signal("RPM", "high")),
        serving_mode01=EVERY_PID,
        serving_dids=None,
        did_map=load_repo_did_map(),
    )
    frame = _frame("decoded:BRAKE", "decoded:RPM")
    assert set(candidate_feature_columns(frame, policy=policy)) == _family("decoded:RPM")
    assert not is_trainable_binding(brake)
    assert feature_policy_exclusions(frame, policy) == {
        "decoded:BRAKE": "binding opted out (model_feature: false)"
    }


def test_model_feature_defaults_true_so_existing_catalogs_load_unchanged() -> None:
    assert _signal("X", "high").model_feature is True
    assert is_trainable_binding(_signal("X", "high"))


def test_repo_bindings_opt_out_body_channels_and_keep_engine_loads() -> None:
    """Vehicle facts live in data/bindings.yaml; this pins the ones that matter."""
    trainable = {
        sig.name for sig in load_bindings(REPO_BINDINGS).signals if is_trainable_binding(sig)
    }
    by_name = {sig.name: sig for sig in load_bindings(REPO_BINDINGS).signals}
    # Real engine loads stay.
    assert {"AC_COMPRESSOR_STATE", "COOLING_FAN_1F9_0", "ECM_RPM", "ECM_COOLANT_TEMP"} <= trainable
    # The segmenter's road-speed fallback stays; its redundant copies go.
    assert "WHEEL_SPEED_6F7_C" in trainable
    assert not {"WHEEL_SPEED_6F7_A", "WHEEL_SPEED_6F7_B"} & trainable
    # High-confidence but not engine behaviour: opted out in the DATA file, so
    # promoting/demoting them later cannot silently bring them back.
    for name in ("BRAKE_PRESSURE_29E_3", "BRAKE_ACTIVITY_29E_7", "MOTION_29E_5"):
        assert by_name[name].confidence in TRAINABLE_CONFIDENCES
        assert by_name[name].model_feature is False
    for prefix in ("STEERING_", "TPMS_", "MOTION_", "BRAKE_", "HVAC_BLOWER", "POWER_STATE"):
        matched = [sig for sig in by_name.values() if sig.name.startswith(prefix)]
        assert matched, prefix
        assert all(not sig.model_feature for sig in matched), prefix


def test_every_decoded_segmenter_input_stays_trainable() -> None:
    """The channels that decide the operating mode must also be model features;
    gating one would be a silent train/serve split."""
    trainable = {
        f"decoded:{sig.name}"
        for sig in load_bindings(REPO_BINDINGS).signals
        if is_trainable_binding(sig)
    }
    for value in SEGMENTER_SIGNAL_COLUMNS.values():
        for column in (value,) if isinstance(value, str) else value:
            if column.startswith("decoded:"):
                assert column in trainable, column


def test_default_policy_applies_the_repo_gates_without_any_argument() -> None:
    """A caller that passes nothing still cannot select a gated channel."""
    frame = _frame(
        "decoded:ECM_RPM",
        "decoded:STEERING_ANGLE_002",  # low
        "decoded:TPMS_FL_PRESSURE",  # hypothesized
        "decoded:BRAKE_PRESSURE_29E_3",  # high, model_feature: false
        "decoded:WHEEL_SPEED_6F7_C",
    )
    expected = _family("decoded:ECM_RPM") | _family("decoded:WHEEL_SPEED_6F7_C")
    assert set(candidate_feature_columns(frame)) == expected
    assert set(sequence_feature_columns(frame)) == expected
    assert set(isolation_forest_feature_columns(frame)) == expected


# ---------------------------------------------------------------------------
# 3. non-trainable OBD state / counters
# ---------------------------------------------------------------------------


def test_fuel_level_and_misfire_counters_are_kept_in_frame_but_never_candidates() -> None:
    frame = _frame(
        "obd:FUEL_LEVEL",
        "obd:MISFIRE_COUNT_CYL1",
        "obd:MISFIRE_COUNT_CYL6",
        "obd:MISFIRE_COUNT_CYL8",  # not enumerated anywhere: matched by registry prefix
        "obd:RPM",
    )
    cols = set(candidate_feature_columns(frame))
    assert cols == _family("obd:RPM")
    assert "obd:FUEL_LEVEL" in frame.columns  # excluded from features, not from the frame


# ---------------------------------------------------------------------------
# 4. did: features must be servable
# ---------------------------------------------------------------------------


def test_did_not_polled_by_the_serving_profile_is_excluded_and_a_polled_one_kept() -> None:
    policy = FeaturePolicy(
        bindings=None,
        serving_mode01=EVERY_PID,
        serving_dids=frozenset({"ECM_AIR_CHARGE"}),
        did_map=load_repo_did_map(),
    )
    frame = _frame("did:ECM_AIR_CHARGE", "did:ECM_INJECTOR_PULSE_1")
    assert set(candidate_feature_columns(frame, policy=policy)) == _family("did:ECM_AIR_CHARGE")
    assert feature_policy_exclusions(frame, policy) == {
        "did:ECM_INJECTOR_PULSE_1": "not polled by the serving (default) capture profile"
    }


def test_unresolvable_serving_profile_excludes_every_did() -> None:
    policy = FeaturePolicy(
        bindings=None, serving_mode01=EVERY_PID, serving_dids=None, did_map=load_repo_did_map()
    )
    frame = _frame("did:ECM_AIR_CHARGE")
    assert candidate_feature_columns(frame, policy=policy) == []
    assert feature_policy_exclusions(frame, policy) == {
        "did:ECM_AIR_CHARGE": "serving capture profile unresolvable"
    }


# ---------------------------------------------------------------------------
# 4b. Mode 01 features must be servable (D03.34)
# ---------------------------------------------------------------------------


def test_mode01_pid_the_serving_profile_dropped_is_excluded() -> None:
    """The Xterra ``default`` dropped ten PIDs on 2026-09-26. The old corpus
    still carries them, so without this gate the next retrain learns them."""
    policy = FeaturePolicy(
        bindings=None,
        serving_dids=frozenset(),
        serving_mode01=frozenset({"RPM", "MAF"}),
        did_map=load_repo_did_map(),
    )
    frame = _frame("obd:RPM", "obd:ABSOLUTE_LOAD")
    assert set(candidate_feature_columns(frame, policy=policy)) == _family("obd:RPM")
    assert feature_policy_exclusions(frame, policy) == {
        "obd:ABSOLUTE_LOAD": "not polled by the serving (default) capture profile"
    }


def test_auto_serves_every_mode01_pid_and_unresolvable_serves_none() -> None:
    frame = _frame("obd:RPM", "obd:ABSOLUTE_LOAD")
    auto = FeaturePolicy(
        bindings=None,
        serving_dids=frozenset(),
        serving_mode01=EVERY_PID,
        did_map=load_repo_did_map(),
    )
    assert feature_policy_exclusions(frame, auto) == {}
    unknown = FeaturePolicy(
        bindings=None, serving_dids=frozenset(), serving_mode01=None, did_map=load_repo_did_map()
    )
    assert feature_policy_exclusions(frame, unknown) == {
        "obd:RPM": "serving capture profile unresolvable",
        "obd:ABSOLUTE_LOAD": "serving capture profile unresolvable",
    }


def test_a_derived_channel_needs_a_served_input_in_every_required_group() -> None:
    """``AIR_MASS_PER_CYCLE`` needs MAF and one of RPM / broadcast ECM_RPM."""
    frame = _frame("derived:AIR_MASS_PER_CYCLE")
    served = FeaturePolicy(
        bindings=None,
        serving_dids=frozenset(),
        serving_mode01=frozenset({"MAF"}),
        did_map=load_repo_did_map(),
    )
    # RPM is not polled, but the broadcast ECM_RPM alternative is always there.
    assert feature_policy_exclusions(frame, served) == {}
    starved = FeaturePolicy(
        bindings=None,
        serving_dids=frozenset(),
        serving_mode01=frozenset({"RPM"}),
        did_map=load_repo_did_map(),
    )
    reason = feature_policy_exclusions(frame, starved)["derived:AIR_MASS_PER_CYCLE"]
    assert "obd:MAF" in reason and "does not poll" in reason


def test_the_xterra_serving_profile_gates_its_dropped_pids() -> None:
    """End to end on the tracked Xterra catalog."""
    from src.collection.capture_profiles import serving_mode01_channels

    served = serving_mode01_channels(
        CaptureProfileSources(REPO_ROOT / "data/nissan-xterra-2008/capture_profiles.yaml", None)
    )
    assert served is not None and "AUTO" not in served
    policy = FeaturePolicy(
        bindings=None, serving_dids=frozenset(), serving_mode01=served, did_map=load_repo_did_map()
    )
    frame = _frame("obd:RPM", "obd:O2_WR_LAMBDA_S5", "obd:ACCEL_PEDAL_POS_D")
    assert feature_policy_exclusions(frame, policy) == {
        "obd:ACCEL_PEDAL_POS_D": "not polled by the serving (default) capture profile"
    }


# ---------------------------------------------------------------------------
# 4c. memory (service 0x23) channels are never detector features (B08.4)
# ---------------------------------------------------------------------------


def test_a_memory_channel_is_never_a_detector_feature_however_it_is_served() -> None:
    """``obd:mem:*`` passed the source gate (source ``obd``) and no serving gate
    covered it, being neither a Mode 01 PID nor a DID. The 2026-09-24 sequence
    artifact learned two such channels from drives that polled them; putting
    the B08 windows in the ``default`` profile must not make them features."""
    policy = FeaturePolicy(
        bindings=None,
        serving_mode01=EVERY_PID,
        serving_dids=frozenset({"ECM_AIR_CHARGE"}),
        did_map=load_repo_did_map(),
    )
    frame = _frame(
        "obd:RPM",
        "did:ECM_AIR_CHARGE",
        "obd:mem:ECM_ENGINE_SPEED_RAM",
        "obd:mem:ECM_IGNITION_TIMING_RAM",
    )
    assert set(candidate_feature_columns(frame, policy=policy)) == _family("obd:RPM") | _family(
        "did:ECM_AIR_CHARGE"
    )
    assert feature_policy_exclusions(frame, policy) == {
        "obd:mem:ECM_ENGINE_SPEED_RAM": MEMORY_CHANNEL_EXCLUSION,
        "obd:mem:ECM_IGNITION_TIMING_RAM": MEMORY_CHANNEL_EXCLUSION,
    }
    # A deployed artifact that already carries one is named at load (A04.6),
    # rolling expansions included.
    assert feature_policy_violations(["obd:RPM", "obd:mem:ECM_ENGINE_SPEED_RAM__mean"], policy) == {
        "obd:mem:ECM_ENGINE_SPEED_RAM__mean": MEMORY_CHANNEL_EXCLUSION
    }


def test_a_derived_channel_whose_only_input_is_a_memory_channel_is_not_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal holds through a ``derived:`` channel computed from RAM, and a
    memory alternative inside a group does not starve a served input."""
    from src.signals import derived

    monkeypatch.setitem(
        derived.DERIVED_CHANNEL_INPUTS,
        "derived:FROM_RAM_ONLY",
        (("obd:mem:ECM_ENGINE_SPEED_RAM",),),
    )
    monkeypatch.setitem(
        derived.DERIVED_CHANNEL_INPUTS,
        "derived:FROM_RAM_OR_PID",
        (("obd:mem:ECM_ENGINE_SPEED_RAM", "obd:RPM"),),
    )
    policy = FeaturePolicy(
        bindings=None, serving_dids=frozenset(), serving_mode01=EVERY_PID, did_map=None
    )
    reason = policy.exclusion_reason("derived:FROM_RAM_ONLY")
    assert reason is not None and "obd:mem:ECM_ENGINE_SPEED_RAM" in reason
    assert policy.exclusion_reason("derived:FROM_RAM_OR_PID") is None


def _write_catalog(path: Path, *, mode22: list[str], enabled: bool = True) -> None:
    """A valid catalog whose ``default`` polls ``mode22``, beside a research
    capture profile that polls a DID the default does not."""
    collection = CanaryConfig.from_yaml(EXAMPLE_YAML).collection
    default = seed_catalog(collection).by_name(DEFAULT_PROFILE_NAME)
    assert default is not None
    research = CaptureProfile(
        name="research",
        mode22=("ECM_INJECTOR_PULSE_1",),
        storage_prefix="_capture-research",
    )
    save_capture_profiles(
        path,
        CaptureProfileCatalog(
            profiles=[
                default.model_copy(update={"mode22": tuple(mode22), "mode22_enabled": enabled}),
                research,
            ]
        ),
    )


def test_serving_dids_come_from_the_default_profile_only(tmp_path: Path) -> None:
    catalog = tmp_path / "capture_profiles.yaml"
    _write_catalog(catalog, mode22=["ecm_rpm", "ECM_AIR_CHARGE"])
    # The research profile's DID is in the corpus but never served.
    assert serving_mode22_dids(CaptureProfileSources(catalog, None)) == frozenset(
        {"ECM_RPM", "ECM_AIR_CHARGE"}
    )


def test_serving_dids_empty_when_the_default_profile_disables_mode22(tmp_path: Path) -> None:
    catalog = tmp_path / "capture_profiles.yaml"
    _write_catalog(catalog, mode22=["ECM_RPM"], enabled=False)
    assert serving_mode22_dids(CaptureProfileSources(catalog, None)) == frozenset()


def test_serving_dids_without_a_catalog_seed_from_config_and_never_write(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "capture_profiles.yaml"
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    seeded = serving_mode22_dids(CaptureProfileSources(missing, None), config.collection)
    assert seeded == frozenset(name.upper() for name in config.collection.obd_dids)
    assert seeded  # the example config polls DIDs; an empty set would prove nothing
    # Unresolvable is a different answer from "polls nothing".
    assert serving_mode22_dids(CaptureProfileSources(missing, None)) is None
    assert not missing.exists() and not missing.parent.exists()  # strictly read-only


def test_resolve_feature_policy_reads_the_vehicle_scoped_catalog(tmp_path: Path) -> None:
    base = CanaryConfig.from_yaml(EXAMPLE_YAML)
    unscoped = tmp_path / "capture_profiles.yaml"
    config = base.model_copy(
        update={"daemon": base.daemon.model_copy(update={"capture_profiles": unscoped})}
    )
    assert config.profiles.enabled
    scoped = tmp_path / "some-truck" / "capture_profiles.yaml"
    scoped.parent.mkdir()
    _write_catalog(unscoped, mode22=["GLOBAL_ONLY"])
    _write_catalog(scoped, mode22=["TRUCK_ONLY"])

    policy = resolve_feature_policy(config, profile_slug="some-truck")
    assert policy.serving_dids == frozenset({"TRUCK_ONLY"})
    assert policy.bindings is not None  # repo catalog by default


def test_resolve_feature_policy_threads_an_explicit_catalog() -> None:
    explicit = _bindings(_signal("ONLY_ME", "high"))
    assert resolve_feature_policy(bindings=explicit).bindings is explicit


def test_repo_default_profile_serves_the_injector_pulse_dids() -> None:
    """The 2026-09-20 LSTM trained on these two and could not observe them.

    That was fixed from the *collection* side rather than by excluding them:
    the UpRev cable identified 0x1206/0x1207 as the Bank 1/2 injected pulse
    and pinned both scales at ``raw * 0.01 ms``, so seed revision 4 added
    them to the default profile and they became legitimately observable at
    serve time.

    This test therefore asserts the opposite of what it did before, on
    purpose. The invariant it guards is unchanged and still enforced
    generally by
    ``test_did_not_polled_by_the_serving_profile_is_excluded_and_a_polled_one_kept``:
    a model may only train on what the serving profile actually polls. What
    changed is which side of that line these two channels sit on.
    """
    policy = resolve_feature_policy()
    assert policy.serving_dids is not None
    assert {"ECM_INJECTOR_PULSE_1", "ECM_INJECTOR_PULSE_2"} <= policy.serving_dids
    frame = _frame("did:ECM_INJECTOR_PULSE_1", "did:ECM_INJECTOR_PULSE_2", "did:ECM_RPM")
    assert set(candidate_feature_columns(frame, policy=policy)) == (
        _family("did:ECM_INJECTOR_PULSE_1")
        | _family("did:ECM_INJECTOR_PULSE_2")
        | _family("did:ECM_RPM")
    )


# ---------------------------------------------------------------------------
# the manifest says what was gated
# ---------------------------------------------------------------------------


def test_feature_spec_records_policy_exclusions_with_reasons() -> None:
    policy = FeaturePolicy(
        bindings=_bindings(_signal("GUESS", "hypothesized"), _signal("SOLID", "high")),
        serving_mode01=EVERY_PID,
        serving_dids=frozenset(),
        did_map=load_repo_did_map(),
    )
    frame = _frame("decoded:GUESS", "decoded:SOLID", "did:UNSERVED")
    columns = candidate_feature_columns(frame, policy=policy)
    spec = build_feature_spec(frame, columns, policy=policy)
    assert spec["feature_columns"] == columns
    assert spec["feature_policy_exclusions"] == {
        "decoded:GUESS": "binding confidence 'hypothesized' is below 'high'",
        "did:UNSERVED": "not polled by the serving (default) capture profile",
    }
