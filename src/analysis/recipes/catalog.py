"""The catalog the daemon refits for a profile (R17; was ``refit.default_recipes``).

Shared recipes first, then the vehicle's own. Imports the shared catalog and the
profile loader, never the fitting engine; ``refit`` re-exports
:func:`default_recipes` under its old name.
"""

from __future__ import annotations

from src.analysis.recipe_contract import Recipe
from src.analysis.recipes import profile_recipes
from src.analysis.recipes.shared import shared_recipes


def default_recipes(profile: str | None) -> list[Recipe]:
    """The catalog the daemon refits for ``profile``: shared, then the vehicle's own.

    ``profile`` is the vehicle profile slug (required, so no caller can forget
    it and silently fit another vehicle's DID surfaces, or none); ``None``
    asks for the shared catalog only. The profile's recipes come from
    :func:`src.analysis.recipes.profile_recipes` and follow the shared ones.
    """
    recipes = shared_recipes()
    own = profile_recipes(profile)
    clash = {recipe.name for recipe in recipes} & {recipe.name for recipe in own}
    if clash:
        raise ValueError(f"profile {profile!r} recipes shadow shared ones: {sorted(clash)}")
    return [*recipes, *own]


__all__ = ["default_recipes"]
