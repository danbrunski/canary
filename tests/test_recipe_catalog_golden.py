"""R05.7 — the default recipe catalog is pinned byte-for-byte.

A recipe contract change flags every Pi artifact ``contract_changed:recipe``,
so a restructure of :func:`src.analysis.refit.default_recipes` must leave each
recipe identical: its contract fingerprint (what the Pi compares) *and* its
full ``asdict`` form (what the next fit reads). ``repr`` keeps tuple/list and
int/float distinctions that JSON would erase.

Regenerate only for an *intended* catalog change::

    CANARY_UPDATE_RECIPE_GOLDEN=1 .venv/bin/python -m pytest -n0 tests/test_recipe_catalog_golden.py
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from src.analysis.refit import default_recipes, recipe_contract_fingerprint

GOLDEN = Path(__file__).parent / "fixtures" / "recipe_catalog_golden.json"


def _catalog() -> dict[str, object]:
    recipes = default_recipes("nissan-xterra-2008")
    catalog: dict[str, object] = {
        recipe.name: {
            "type": type(recipe).__name__,
            "contract_fingerprint": recipe_contract_fingerprint(recipe),
            "asdict_repr": repr(asdict(recipe)),
        }
        for recipe in recipes
    }
    # Refit reports and the artifact list follow catalog order.
    catalog["__order__"] = [recipe.name for recipe in recipes]
    return catalog


def test_the_default_catalog_is_unchanged() -> None:
    current = _catalog()
    if os.environ.get("CANARY_UPDATE_RECIPE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
    pinned = json.loads(GOLDEN.read_text())
    assert current["__order__"] == pinned["__order__"], "recipes added, removed or reordered"
    for name, entry in current.items():
        assert entry == pinned[name], f"recipe {name!r} changed"


def test_a_minimum_a_recipe_declares_wins_over_the_family_default() -> None:
    """The old trailing loop ``replace``-d min_sessions / min_cell_sessions /
    min_cell_dwell_seconds over every recipe, so none could be set per recipe."""
    from src.analysis.refit import _surface_1d, _surface_2d

    grid = _surface_2d(
        name="probe_2d",
        output="obd:MAF",
        x_column="obd:RPM",
        y_column="obd:ENGINE_LOAD",
        min_sessions=7,
        min_cell_sessions=4,
        min_cell_dwell_seconds=2.5,
    )
    assert (grid.min_sessions, grid.min_cell_sessions, grid.min_cell_dwell_seconds) == (7, 4, 2.5)
    line = _surface_1d(name="probe_1d", output="obd:MAF", x_column="obd:RPM", min_sessions=5)
    assert line.min_sessions == 5
    # And the family default still applies when the recipe says nothing.
    default = _surface_2d(name="d", output="obd:MAF", x_column="obd:RPM", y_column="obd:SPEED")
    assert (default.min_sessions, default.min_cell_sessions) == (3, 2)
