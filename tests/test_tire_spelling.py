"""US-English rename (2026-10-11): data written as "tyre" still loads as "tire".

Registries, machine overlays, session manifests and the vehicle ledger on a Pi
were all written with the old spelling before the rename. They must load
unchanged, and a file mixing the two must not keep one silently.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.config import ProfilesConfig, VehicleParameters
from src.vehicle.profiles import load_registry
from src.vehicle.vehicle_ledger import VehicleEvent, VehicleLedgerStore

EVIDENCE = "published ratios; GPS cruise"


def test_vehicle_parameters_read_the_old_key() -> None:
    params = VehicleParameters(
        final_drive=4.08,
        gearbox_ratios=(4.368, 1.0),
        tyre_loaded_circumference_m=2.559,  # type: ignore[call-arg]
        driveline_evidence=EVIDENCE,
    )
    assert params.tire_loaded_circumference_m == 2.559
    assert "tyre_loaded_circumference_m" not in params.model_dump()


def test_vehicle_parameters_refuse_both_spellings() -> None:
    with pytest.raises(ValidationError, match="old spelling"):
        VehicleParameters.model_validate(
            {
                "tyre_loaded_circumference_m": 2.6,
                "tire_loaded_circumference_m": 2.559,
                "driveline_evidence": EVIDENCE,
            }
        )


def test_a_tracked_old_key_and_an_overlay_new_key_merge_as_the_overlay(tmp_path: Path) -> None:
    """The tracked registry may still say tyre_ while this machine's overlay,
    written after the rename, says tire_: the overlay wins, as for any field."""
    profiles = ProfilesConfig(
        registry_path=tmp_path / "profiles.yaml",
        registry_overlay_path=tmp_path / "state" / "profiles.yaml",
    )
    tracked = {
        "slug": "xterra",
        "vin": "",
        "make": "Nissan",
        "model": "Xterra",
        "year": 2008,
        "parameters": {
            "final_drive": 4.08,
            "gearbox_ratios": [4.368, 1.0],
            "tyre_loaded_circumference_m": 2.559,
            "driveline_evidence": EVIDENCE,
        },
    }
    profiles.registry_path.write_text(yaml.safe_dump({"schema_version": 1, "profiles": [tracked]}))
    (profile,) = load_registry(profiles)
    assert profile.parameters.tire_loaded_circumference_m == 2.559

    overlay = {
        "slug": "xterra",
        "parameters": {
            "final_drive": 4.08,
            "gearbox_ratios": [4.368, 1.0],
            "tire_loaded_circumference_m": 2.571,
            "driveline_evidence": "GPS cruise, 2026-10-11",
        },
    }
    profiles.registry_overlay_path.parent.mkdir(parents=True)
    profiles.registry_overlay_path.write_text(
        yaml.safe_dump({"schema_version": 1, "profiles": [overlay]})
    )
    (profile,) = load_registry(profiles)
    assert profile.parameters.tire_loaded_circumference_m == 2.571


def test_a_ledger_line_written_with_tyres_reads_as_tires(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    old = {
        "id": "a" * 32,
        "recorded_at": "2026-10-04T18:00:00+00:00",
        "occurred_at": "2026-10-04",
        "kind": "maintenance",
        "title": "tread gauge",
        "tyres": {"action": "tread_measured", "tread_depth_mm": 12.07},
    }
    path.write_text(json.dumps(old) + "\n")
    (event,) = VehicleLedgerStore(path).records()
    assert event.tires is not None and event.tires.tread_depth_mm == 12.07
    assert event.occurred_at == date(2026, 10, 4)
    assert "tyres" not in event.to_dict()


def test_a_ledger_event_with_both_spellings_is_refused() -> None:
    with pytest.raises(ValidationError, match="old spelling"):
        VehicleEvent.model_validate(
            {
                "occurred_at": "2026-10-04",
                "kind": "maintenance",
                "title": "x",
                "tyres": {"action": "rotated"},
                "tires": {"action": "rotated"},
            }
        )
