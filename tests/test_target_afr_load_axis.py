"""P02.13: ``derived:TARGET_AFR`` reads the target map's declared load axis.

The lookup read calculated load (``obd:ENGINE_LOAD``, %) on a map whose axis
is base fuel schedule (ms), so every ``TARGET_AFR`` and ``AFR_ERROR_B*`` value
was dimensionally wrong, on the daemon too. The load input is now the
vehicle's verified channel for the axis (``vehicle_signals.LOAD_AXIS_CHANNELS``),
and none is verified yet, so the channels are absent and the feature policy
keeps them out of detector features, by name, on every manifest.
(``tests/test_afr.py`` covers the lookup itself.)
"""

from __future__ import annotations

import dataclasses
import types

import pandas as pd
import pytest

from src.preprocessing.channel_units import catalog_channel_unit
from src.preprocessing.feature_pipeline import (
    FeaturePolicy,
    build_feature_spec,
    candidate_feature_columns,
    derived_channels_reached,
    feature_policy_exclusions,
)
from src.signals import vehicle_signals
from src.signals.afr import (
    DEFAULT_TARGET_LOAD_AXIS,
    DEFAULT_TARGET_LOAD_AXIS_UNIT,
    LOAD_AXIS_UNVERIFIED,
    TARGET_AFR,
    TARGET_AFR_CHANNELS,
    TargetAfrMap,
    load_axis_refusal,
)
from src.signals.derived import DERIVED_CHANNEL_INPUTS
from src.signals.vehicle_signals import LoadAxisChannel, load_axis_channels, load_axis_columns
from src.trainability import SOURCE_NOT_TRAINABLE, source_exclusion_reason

POLICY = FeaturePolicy(bindings=None, serving_dids=frozenset(), serving_mode01=frozenset({"AUTO"}))


def _declare(monkeypatch: pytest.MonkeyPatch, channel: LoadAxisChannel) -> None:
    module = types.SimpleNamespace(LOAD_AXIS_CHANNELS=(channel,))
    real = vehicle_signals.vehicle_modules()
    monkeypatch.setattr(vehicle_signals, "vehicle_modules", lambda: (*real, module))


def _verified() -> LoadAxisChannel:
    """A declaration the real catalog admits: ``dids.yaml`` scales this DID to
    ms. It is not the load axis. It stands in for a verified BFS channel so
    the policy's own catalog lookup can pass."""
    return LoadAxisChannel(
        axis=DEFAULT_TARGET_LOAD_AXIS,
        channel="did:ECM_INJECTOR_PULSE_1",
        evidence="synthetic test vehicle",
    )


def test_the_map_default_axis_is_the_one_the_registry_resolves() -> None:
    fields = TargetAfrMap.model_fields
    assert fields["load_axis_name"].default == DEFAULT_TARGET_LOAD_AXIS
    assert fields["load_axis_unit"].default == DEFAULT_TARGET_LOAD_AXIS_UNIT
    rpm, load = DERIVED_CHANNEL_INPUTS[TARGET_AFR]
    assert rpm == ("obd:RPM",)
    assert load == load_axis_columns(DEFAULT_TARGET_LOAD_AXIS)


def test_no_registry_input_of_the_target_is_calculated_load() -> None:
    for channel in TARGET_AFR_CHANNELS:
        for group in DERIVED_CHANNEL_INPUTS[channel]:
            assert "obd:ENGINE_LOAD" not in group, channel


def test_every_channel_built_on_the_target_is_covered_by_the_exclusion() -> None:
    """A new channel computed from ``TARGET_AFR`` inherits its wrong axis; it
    must join :data:`TARGET_AFR_CHANNELS` or this fails."""
    assert derived_channels_reached({TARGET_AFR}) <= set(TARGET_AFR_CHANNELS)


def test_no_vehicle_declares_a_verified_axis_channel_today() -> None:
    """The operator decision 2026-09-27: 0x1208 is only a candidate."""
    assert load_axis_channels(DEFAULT_TARGET_LOAD_AXIS) == ()
    refusal = load_axis_refusal(channel_units=catalog_channel_unit)
    assert refusal is not None and refusal.startswith(LOAD_AXIS_UNVERIFIED)


# ---------------------------------------------------------------------------
# The feature-policy exclusion, recorded on the manifest
# ---------------------------------------------------------------------------


def _frame_with_target_columns() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "session": "s",
            "timestamp": pd.date_range("2026-09-20", periods=4, freq="1s", tz="UTC"),
            "obd:RPM": [800.0, 900.0, 1000.0, 1100.0],
            **{channel: [14.7, 13.0, 12.5, 0.2] for channel in TARGET_AFR_CHANNELS},
            **{f"{channel}__mean": [14.7, 13.0, 12.5, 0.2] for channel in TARGET_AFR_CHANNELS},
        }
    )


def test_the_policy_refuses_the_target_channels_by_name() -> None:
    for channel in TARGET_AFR_CHANNELS:
        reason = POLICY.exclusion_reason(channel)
        assert reason is not None and reason.startswith(LOAD_AXIS_UNVERIFIED), channel
    frame = _frame_with_target_columns()
    candidates = candidate_feature_columns(frame, policy=POLICY)
    assert not [c for c in candidates if c.split("__")[0] in TARGET_AFR_CHANNELS]
    excluded = feature_policy_exclusions(frame, POLICY)
    for channel in TARGET_AFR_CHANNELS:
        assert excluded[channel].startswith(LOAD_AXIS_UNVERIFIED)


def test_the_manifest_records_the_refusal_even_when_the_frame_lacks_the_channels() -> None:
    frame = pd.DataFrame({"session": "s", "obd:RPM": [800.0, 900.0]})
    spec = build_feature_spec(frame, ["obd:RPM"], policy=POLICY)
    standing = spec["feature_policy_standing_exclusions"]
    assert sorted(standing) == sorted(TARGET_AFR_CHANNELS)
    assert all(reason.startswith(LOAD_AXIS_UNVERIFIED) for reason in standing.values())
    assert TARGET_AFR not in spec["feature_policy_exclusions"], "absent from this frame"


def test_the_exclusion_lifts_once_a_vehicle_verifies_the_axis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _declare(monkeypatch, _verified())
    assert load_axis_refusal(channel_units=catalog_channel_unit) is None
    assert load_axis_refusal(channel_units=None) is not None, "no catalog: refused"
    assert POLICY.standing_exclusions() == {}
    for channel in TARGET_AFR_CHANNELS:
        reason = POLICY.exclusion_reason(channel)
        assert reason is None or not reason.startswith(LOAD_AXIS_UNVERIFIED)


def test_a_declaration_in_the_wrong_unit_keeps_the_exclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _declare(
        monkeypatch,
        LoadAxisChannel(axis=DEFAULT_TARGET_LOAD_AXIS, channel="obd:ENGINE_LOAD", evidence="x"),
    )
    reason = POLICY.exclusion_reason(TARGET_AFR)
    assert reason is not None and "obd:ENGINE_LOAD (catalog unit %)" in reason


# ---------------------------------------------------------------------------
# The unit is the catalog's, resolved at runtime (verifier, P02.13)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "catalog_unit"),
    [
        ("obd:ENGINE_LOAD", "%"),
        ("obd:ABSOLUTE_LOAD", "%"),
        ("did:ECM_AIR_CHARGE", "unknown"),
        ("did:ECM_INJECTOR_PULSE_1", "ms"),
        ("mem:ECM_ENGINE_PERIOD", "unknown"),
        ("mem:ECM_TARGET_EQUIV_RATIO", "phi"),
        ("decoded:ECM_RPM", None),
        ("derived:TARGET_AFR", None),
        ("did:NO_SUCH_DID", None),
    ],
)
def test_the_catalog_resolves_each_namespace(channel: str, catalog_unit: str | None) -> None:
    assert catalog_channel_unit(channel) == catalog_unit


@pytest.mark.parametrize(
    "channel",
    [
        "obd:ENGINE_LOAD",
        "obd:ABSOLUTE_LOAD",
        "did:ECM_AIR_CHARGE",
        "mem:ECM_ENGINE_PERIOD",
        "mem:ECM_TARGET_EQUIV_RATIO",
    ],
)
def test_a_declaration_cannot_relabel_a_channel_the_catalog_does_not_put_in_ms(
    monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    """A declaration carries no unit, so a % load, raw counts or a phi word
    cannot be claimed as ms. The runtime refusal, the manifest exclusion and
    the lookup all read the catalog."""
    _declare(
        monkeypatch, LoadAxisChannel(axis=DEFAULT_TARGET_LOAD_AXIS, channel=channel, evidence="x")
    )
    reason = load_axis_refusal(channel_units=catalog_channel_unit)
    assert reason is not None and reason.startswith(LOAD_AXIS_UNVERIFIED)
    assert channel in reason
    assert POLICY.standing_exclusions()[TARGET_AFR] == reason
    assert "unit" not in {field.name for field in dataclasses.fields(LoadAxisChannel)}


# ---------------------------------------------------------------------------
# The source gate: an empty required input group is not trainable
# ---------------------------------------------------------------------------


def test_the_source_gate_refuses_the_target_while_its_load_group_is_empty() -> None:
    """``_derived_detail`` flattened the groups, so the empty load group
    vanished and ``TARGET_AFR`` read as trainable on ``obd:RPM`` alone."""
    reason = source_exclusion_reason(TARGET_AFR)
    assert reason is not None and reason.startswith(SOURCE_NOT_TRAINABLE)
    assert LOAD_AXIS_UNVERIFIED in reason
    for error in TARGET_AFR_CHANNELS[1:]:
        detail = source_exclusion_reason(error)
        assert detail is not None and TARGET_AFR in detail
    # The policy still names the specific reason first.
    assert POLICY.exclusion_reason(TARGET_AFR) == load_axis_refusal(channel_units=None)
