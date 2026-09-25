"""R05.7 — DID-named behavior recipes live in a per-profile module.

Operator decision 2026-09-25: the shared catalog names no manufacturer DID;
a vehicle's Mode 22 surfaces come from ``src/analysis/recipes/<slug>.py``.
The Xterra's catalog stays byte-identical (``test_recipe_catalog_golden``).
"""

from __future__ import annotations

import sys
import types
from dataclasses import asdict, replace

import pytest

from src.analysis.recipes import module_name, profile_recipes
from src.analysis.refit import default_recipes, shared_recipes

XTERRA = "nissan-xterra-2008"


def _columns(recipe: object) -> set[str]:
    fields = asdict(recipe)  # type: ignore[call-overload]
    named = {fields.get(key) for key in ("output", "x_column", "y_column")}
    named |= set((fields.get("max_abs_rate_of_change") or {}).keys())
    return {str(column) for column in named if column}


def test_the_shared_catalog_names_no_did() -> None:
    shared = shared_recipes()
    assert shared == default_recipes(None)
    assert not any(column.startswith("did:") for r in shared for column in _columns(r))


def test_the_xterras_did_recipes_come_from_its_module() -> None:
    own = profile_recipes(XTERRA)
    assert module_name(XTERRA) == "src.analysis.recipes.nissan_xterra_2008"
    assert {r.name for r in own} == {
        "air_charge_vs_rpm_throttle_did",
        "cam_primary_vs_command_rpm_did",
        "cam_secondary_vs_command_rpm_did",
        "afr_bank_balance_robust",
        "afr_voltage_vs_rpm_throttle_did",
        "tps_sensor_1_vs_sensor_2_did",
    }
    assert default_recipes(XTERRA) == [*shared_recipes(), *own]


def test_a_second_vehicle_supplies_its_own_and_never_gets_the_xterras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = replace(shared_recipes()[0], name="work_truck_probe", output="did:WT_PROBE")
    module = types.ModuleType(module_name("work-truck"))
    module.recipes = lambda: [probe]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())

    catalog = default_recipes("work-truck")
    assert catalog[-1] == probe
    assert not {r.name for r in profile_recipes(XTERRA)} & {r.name for r in catalog}


def test_a_profile_without_a_module_fits_the_shared_catalog() -> None:
    assert profile_recipes("1n6ad07w48c123456") == []
    assert module_name("1n6ad07w48c123456").rsplit(".", 1)[1].isidentifier()
    assert default_recipes("1n6ad07w48c123456") == shared_recipes()


def test_a_profile_recipe_may_not_shadow_a_shared_one(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType(module_name("clash"))
    module.recipes = lambda: [shared_recipes()[0]]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    with pytest.raises(ValueError, match="shadow"):
        default_recipes("clash")
