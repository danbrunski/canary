"""Per-vehicle behavior recipes: surfaces over a vehicle's own identifiers (R05.7).

The shared catalog (:func:`src.analysis.recipes.shared.shared_recipes`) is built from
standard Mode 01 PIDs and derived channels. Surfaces over a manufacturer's
Mode 22 DIDs (``did:ECM_*`` on the Xterra's VQ40 ECM) belong to the vehicle,
so they live in one Python module per profile (operator decision
2026-09-25): ``src/analysis/recipes/<module>.py``, where ``<module>`` is the
profile slug with ``-`` as ``_`` (:func:`module_name`). The same vehicle's
signal inputs live under the same name in :mod:`src.signals.vehicle_signals`. The module defines
``recipes() -> list[Recipe]``. A second vehicle supplies its own surfaces by
adding its module; a profile without one fits the shared catalog only.
:func:`src.analysis.recipes.catalog.default_recipes` joins the two. Vehicle modules
import the recipe contract and builders
(:mod:`src.analysis.recipe_contract`, :mod:`src.analysis.recipe_builders`), never
the fitting engine (:mod:`src.analysis.refit`).
"""

from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING

from src.vehicle.profiles import profile_module_stem

if TYPE_CHECKING:
    from src.analysis.recipe_contract import Recipe


#: Modules of this package that are not vehicle catalogs. A profile whose slug
#: maps to one of these names has no vehicle module (it must never be imported
#: as one), exactly as an unknown profile has none.
NON_PROFILE_MODULES = frozenset({"catalog", "shared"})


def module_name(slug: str) -> str:
    """``src.analysis.recipes.<module>`` for a profile slug (:func:`profile_module_stem`)."""
    return f"{__name__}.{profile_module_stem(slug)}"


def profile_recipes(slug: str | None) -> list[Recipe]:
    """The recipes ``slug``'s module declares, or ``[]`` when it has none."""
    if slug is None:
        return []
    if profile_module_stem(slug) in NON_PROFILE_MODULES:
        return []
    name = module_name(slug)
    if importlib.util.find_spec(name) is None:
        return []
    module = importlib.import_module(name)
    recipes = getattr(module, "recipes", None)
    if not callable(recipes):
        raise TypeError(f"{name} must define recipes() -> list[Recipe]")
    return list(recipes())


__all__ = ["NON_PROFILE_MODULES", "module_name", "profile_recipes"]
