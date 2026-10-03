"""Tests for the accepted_baselines.yaml schema + loader (Task 1.3)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.behavior.accepted_baselines import (
    AcceptedBaselineOverride,
    AcceptedBaselines,
    DirectionalBand,
    Provenance,
    dump_accepted_baselines,
    load_accepted_baselines,
)


def _band(**overrides: object) -> DirectionalBand:
    defaults: dict[str, object] = {
        "tolerance": 3.0,
        "worse_direction": "positive",
        "worse_warning": 3.0,
        "worse_critical": 6.0,
    }
    defaults.update(overrides)
    return DirectionalBand.model_validate(defaults)


def _provenance(**overrides: object) -> Provenance:
    defaults: dict[str, object] = {
        "declared_by": "dan",
        "declared_at": date(2026, 5, 17),
        "review_by": date(2026, 11, 17),
        "reason": "known weak #3 injector; owner not replacing",
    }
    defaults.update(overrides)
    return Provenance.model_validate(defaults)


def _channel_override(**overrides: object) -> AcceptedBaselineOverride:
    defaults: dict[str, object] = {
        "channel": "obd:SHORT_FUEL_TRIM_1",
        "operating_mode": "idle",
        "accepted_center": 12.0,
        "band": _band(),
        "provenance": _provenance(),
    }
    defaults.update(overrides)
    return AcceptedBaselineOverride.model_validate(defaults)


# ---------------------------------------------------------------------------
# DirectionalBand / Provenance validation
# ---------------------------------------------------------------------------


def test_directional_band_requires_warning_below_critical() -> None:
    with pytest.raises(ValidationError, match="worse_warning"):
        _band(worse_warning=6.0, worse_critical=3.0)


def test_provenance_requires_review_by_after_declared_at() -> None:
    with pytest.raises(ValidationError, match="review_by"):
        _provenance(review_by=date(2026, 5, 1))


# ---------------------------------------------------------------------------
# AcceptedBaselineOverride target validation
# ---------------------------------------------------------------------------


def test_override_rejects_neither_channel_nor_behavior_model() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        _channel_override(channel=None)


def test_override_rejects_both_channel_and_behavior_model() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        _channel_override(behavior_model="fuel_trim_vs_rpm_load")


def test_channel_override_requires_accepted_center() -> None:
    with pytest.raises(ValidationError, match="accepted_center"):
        _channel_override(accepted_center=None)


def test_behavior_model_override_does_not_require_accepted_center() -> None:
    override = _channel_override(
        channel=None, behavior_model="fuel_trim_vs_rpm_load", accepted_center=None
    )
    assert override.target == "fuel_trim_vs_rpm_load"


def test_override_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match="BOGUS"):
        AcceptedBaselineOverride.model_validate(
            {
                "channel": "obd:SHORT_FUEL_TRIM_1",
                "accepted_center": 12.0,
                "band": _band().model_dump(),
                "provenance": _provenance().model_dump(),
                "BOGUS": True,
            }
        )


# ---------------------------------------------------------------------------
# applies_to / is_expired / active_override_for
# ---------------------------------------------------------------------------


def test_applies_to_matches_channel_and_mode() -> None:
    override = _channel_override()
    assert override.applies_to("obd:SHORT_FUEL_TRIM_1", "idle") is True
    assert override.applies_to("obd:SHORT_FUEL_TRIM_1", "highway") is False
    assert override.applies_to("obd:LONG_FUEL_TRIM_1", "idle") is False


def test_applies_to_unscoped_override_matches_any_mode() -> None:
    override = _channel_override(operating_mode=None)
    assert override.applies_to("obd:SHORT_FUEL_TRIM_1", "idle") is True
    assert override.applies_to("obd:SHORT_FUEL_TRIM_1", "highway") is True


def test_is_expired_past_review_by() -> None:
    override = _channel_override()
    assert override.is_expired(today=date(2026, 11, 18)) is True
    assert override.is_expired(today=date(2026, 11, 16)) is False


def test_active_override_for_skips_expired_and_mismatched() -> None:
    baselines = AcceptedBaselines(schema_version=1, overrides=[_channel_override()])
    assert (
        baselines.active_override_for("obd:SHORT_FUEL_TRIM_1", "idle", today=date(2026, 6, 1))
        is not None
    )
    assert (
        baselines.active_override_for("obd:SHORT_FUEL_TRIM_1", "idle", today=date(2026, 12, 1))
        is None
    )
    assert (
        baselines.active_override_for("obd:SHORT_FUEL_TRIM_1", "highway", today=date(2026, 6, 1))
        is None
    )
    assert baselines.active_override_for("obd:UNRELATED", "idle") is None


# ---------------------------------------------------------------------------
# YAML round-trip / loader
# ---------------------------------------------------------------------------


def test_yaml_round_trip(tmp_path: Path) -> None:
    baselines = AcceptedBaselines(schema_version=1, overrides=[_channel_override()])
    path = tmp_path / "accepted_baselines.yaml"
    dump_accepted_baselines(baselines, path)

    loaded = load_accepted_baselines(path)
    assert loaded.schema_version == 1
    assert len(loaded.overrides) == 1
    assert loaded.overrides[0].channel == "obd:SHORT_FUEL_TRIM_1"
    assert loaded.overrides[0].provenance.declared_by == "dan"


def test_load_missing_file_is_empty_document(tmp_path: Path) -> None:
    loaded = load_accepted_baselines(tmp_path / "does_not_exist.yaml")
    assert loaded.schema_version == 1
    assert loaded.overrides == []


def test_load_rejects_malformed_yaml(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "overrides": [
                    {
                        "channel": "obd:SHORT_FUEL_TRIM_1",
                        "accepted_center": 12.0,
                        "band": {
                            "tolerance": 3.0,
                            "worse_direction": "positive",
                            "worse_warning": 6.0,  # >= critical → invalid
                            "worse_critical": 3.0,
                        },
                        "provenance": {
                            "declared_by": "dan",
                            "declared_at": "2026-05-17",
                            "review_by": "2026-11-17",
                            "reason": "test",
                        },
                    }
                ],
            }
        )
    )
    with pytest.raises(ValidationError):
        load_accepted_baselines(path)


def test_shipped_xterra_file_has_zero_active_overrides() -> None:
    """The 1.3 acceptance clause: the real committed file ships with
    `schema_version: 1` and zero active overrides — authoring a real one is
    an explicit operator morning task, never an unattended one."""
    path = Path("data/nissan-xterra-2008/accepted_baselines.yaml")
    if not path.exists():
        pytest.skip("data/nissan-xterra-2008/accepted_baselines.yaml not present")
    loaded = load_accepted_baselines(path)
    assert loaded.schema_version == 1
    assert loaded.overrides == []
