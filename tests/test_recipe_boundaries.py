"""R17: recipe definitions never depend on the fitting engine.

``refit.py`` once owned the recipe dataclasses, the catalog builders and the
fitting, and the vehicle recipe module imported ``Recipe`` and private builders
back from it while ``default_recipes`` imported the vehicle module: definition
depended on execution and execution on definition. The owners are now

* ``src.analysis.recipe_contract`` -- the typed recipe, its serving spec and
  contract fingerprint (leaf);
* ``src.analysis.recipe_builders`` -- family defaults and builders;
* ``src.analysis.recipes`` -- ``shared`` (vehicle-neutral catalog), ``catalog``
  (``default_recipes``) and one module per vehicle profile.

None of them may import ``src.analysis.refit``, and the contract and builders
may not import any catalog. The scan is AST-based and sees imports in function
bodies, ``try`` blocks and ``if TYPE_CHECKING:``, and resolves
``from src.analysis import refit``. ``tests/fixtures/recipe_serving_specs_golden.json``
pins every Xterra recipe's serving spec as captured before the extraction.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from src.analysis import recipe_builders, recipe_contract, refit
from src.analysis.recipe_contract import recipe_serving_spec
from src.analysis.recipes import NON_PROFILE_MODULES, profile_recipes
from src.analysis.recipes import catalog as recipe_catalog
from src.analysis.recipes import shared as shared_catalog
from src.signals.derived import STFT_BANK_BALANCE_SMOOTHING_SECONDS

ANALYSIS = Path(__file__).resolve().parents[1] / "src" / "analysis"
FIXTURES = Path(__file__).parent / "fixtures"
XTERRA = "nissan-xterra-2008"

ENGINE = "src.analysis.refit"
CATALOG_PACKAGE = "src.analysis.recipes"
#: Leaf definition modules: they may import no catalog and no engine.
LEAVES = ("recipe_contract.py", "recipe_builders.py")


def _package_of(name: str) -> str:
    """The package a scanned file lives in, for resolving relative imports."""
    return "src.analysis.recipes" if name.startswith("recipes/") else "src.analysis"


def _imports(text: str, package: str = "src.analysis") -> set[str]:
    """Every module a source text imports, wherever the import sits.

    Relative imports are resolved against ``package`` (the file's own package).
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
            if node.level == 0:
                target = node.module or ""
            else:
                target = f"{base}.{node.module}" if node.module else base
            if target:
                found.add(target)
                found.update(f"{target}.{alias.name}" for alias in node.names)
    return found


def _names(module: str, forbidden: str) -> bool:
    return module == forbidden or module.startswith(f"{forbidden}.")


def _findings(files: Mapping[str, str], rules: Mapping[str, tuple[str, ...]]) -> list[str]:
    """``file -> forbidden module`` for each import of a module its rule forbids."""
    return sorted(
        {
            f"{name} -> {forbidden}"
            for name, text in files.items()
            for module in _imports(text, _package_of(name))
            for forbidden in rules.get(name, rules["*"])
            if _names(module, forbidden)
        }
    )


def _definition_files() -> dict[str, str]:
    files = {name: (ANALYSIS / name).read_text() for name in LEAVES}
    for path in sorted((ANALYSIS / "recipes").glob("*.py")):
        files[f"recipes/{path.name}"] = path.read_text()
    return files


#: Every recipes/ module (vehicle, shared, catalog) may not import the engine;
#: the two leaves may not import a catalog either.
RULES: dict[str, tuple[str, ...]] = {
    "*": (ENGINE,),
    **{name: (ENGINE, CATALOG_PACKAGE) for name in LEAVES},
}


def test_definition_modules_never_import_the_fitting_engine() -> None:
    files = _definition_files()
    assert {"recipes/shared.py", "recipes/catalog.py", "recipes/nissan_xterra_2008.py"} <= set(
        files
    )
    assert _findings(files, RULES) == []


def test_the_contract_and_builders_import_no_catalog() -> None:
    texts = {name: (ANALYSIS / name).read_text() for name in LEAVES}
    for name, text in texts.items():
        imported = _imports(text, _package_of(name))
        assert not any(_names(m, CATALOG_PACKAGE) or _names(m, ENGINE) for m in imported), name


@pytest.mark.parametrize(
    "label, text, name, forbidden, expected",
    [
        ("top", "from src.analysis.refit import Recipe\n", "x.py", ENGINE, True),
        ("function", "def f():\n    from src.analysis.refit import _sigma\n", "x.py", ENGINE, True),
        (
            "type_checking",
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import src.analysis.refit\n",
            "x.py",
            ENGINE,
            True,
        ),
        (
            "try",
            "try:\n    from src.analysis.refit import fit_recipe\nexcept ImportError:\n    pass\n",
            "x.py",
            ENGINE,
            True,
        ),
        ("package", "from src.analysis import refit\n", "x.py", ENGINE, True),
        # Relative forms, resolved against the file's own package.
        ("relative_module", "from ..refit import X\n", "recipes/v.py", ENGINE, True),
        ("relative_package", "from .. import refit\n", "recipes/v.py", ENGINE, True),
        (
            "relative_sibling",
            "from . import shared\n",
            "recipes/v.py",
            "src.analysis.recipes.shared",
            True,
        ),
        ("relative_from_root", "from .refit import X\n", "recipe_contract.py", ENGINE, True),
        (
            "relative_catalog",
            "from .recipes import shared\n",
            "recipe_contract.py",
            CATALOG_PACKAGE,
            True,
        ),
        ("relative_lookalike", "from . import refit_x\n", "recipe_contract.py", ENGINE, False),
        (
            "clean",
            "from src.analysis.recipe_contract import Recipe\nimport src.analysis.refit_x\n",
            "x.py",
            ENGINE,
            False,
        ),
    ],
)
def test_the_scanner_finds_every_form_of_the_forbidden_import(
    label: str, text: str, name: str, forbidden: str, expected: bool
) -> None:
    found = _findings({name: text}, {"*": (forbidden,)})
    assert found == ([f"{name} -> {forbidden}"] if expected else []), label


def test_a_leaf_importing_a_catalog_is_found() -> None:
    files = {"recipe_contract.py": "def f():\n    from src.analysis.recipes.shared import x\n"}
    assert _findings(files, RULES) == ["recipe_contract.py -> src.analysis.recipes"]


def _loaded_after(statement: str) -> set[str]:
    code = (
        "import sys\n"
        f"{statement}\n"
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('src.analysis.'))))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=ANALYSIS.parents[1],
    )
    return set(done.stdout.split())


@pytest.mark.parametrize(
    "statement",
    [
        "import src.analysis.recipe_contract",
        "import src.analysis.recipe_builders",
        "import src.analysis.recipes.shared",
        "import src.analysis.recipes.catalog",
        "import src.analysis.recipes.nissan_xterra_2008",
        "from src.analysis.recipes import profile_recipes; profile_recipes('nissan-xterra-2008')",
    ],
)
def test_loading_a_definition_never_loads_the_fitting_engine(statement: str) -> None:
    assert ENGINE not in _loaded_after(statement)


def test_the_contract_and_builders_load_no_catalog() -> None:
    for statement in ("import src.analysis.recipe_contract", "import src.analysis.recipe_builders"):
        loaded = _loaded_after(statement)
        assert not {m for m in loaded if _names(m, CATALOG_PACKAGE)}, statement


# ---------------------------------------------------------------------------
# Compatibility: refit's old names are the owners' objects, not copies.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, owner",
    [
        ("Surface1DRecipe", recipe_contract),
        ("Surface2DRecipe", recipe_contract),
        ("Recipe", recipe_contract),
        ("RPM_COLUMN", recipe_contract),
        ("recipe_serving_spec", recipe_contract),
        ("recipe_contract_fingerprint", recipe_contract),
        ("_recipe_columns", recipe_contract),
        ("TOTAL_TRIM_STEADY_RATES", recipe_builders),
        ("CATALOG_1D_DEFAULTS", recipe_builders),
        ("CATALOG_2D_DEFAULTS", recipe_builders),
        ("WARMUP_1D_FIELDS", recipe_builders),
        ("RPM_LOAD_2D_FIELDS", recipe_builders),
        ("shared_recipes", shared_catalog),
        ("TOTAL_TRIM_UNIT", shared_catalog),
        ("STFT_BANK_BALANCE_PERSISTENCE", shared_catalog),
        ("BANK_BALANCE_MODES", shared_catalog),
        ("WARM_IDLE_STEADY_RATES", shared_catalog),
        ("WARM_IDLE_LOCAL_RANGES", shared_catalog),
        ("default_recipes", recipe_catalog),
    ],
)
def test_refit_names_are_the_owners_objects(name: str, owner: object) -> None:
    owner_name = "recipe_columns" if name == "_recipe_columns" else name
    assert getattr(refit, name) is getattr(owner, owner_name)


@pytest.mark.parametrize(
    "old, new",
    [
        ("_surface_1d", "surface_1d_recipe"),
        ("_surface_2d", "surface_2d_recipe"),
        ("_warmup_1d", "warmup_1d_recipe"),
        ("_rpm_load_2d", "rpm_load_2d_recipe"),
        ("_sigma", "sigma_deviation"),
    ],
)
def test_refit_keeps_the_private_builder_aliases(old: str, new: str) -> None:
    assert getattr(refit, old) is getattr(recipe_builders, new)


def test_the_recipe_types_are_defined_once() -> None:
    for kind in (recipe_contract.Surface1DRecipe, recipe_contract.Surface2DRecipe):
        assert kind.__module__ == "src.analysis.recipe_contract"
    assert "class Surface1DRecipe" not in (ANALYSIS / "refit.py").read_text()
    assert "class Surface2DRecipe" not in (ANALYSIS / "refit.py").read_text()


# ---------------------------------------------------------------------------
# Pins captured before the extraction.
# ---------------------------------------------------------------------------


def test_every_recipes_serving_spec_is_unchanged() -> None:
    pinned = json.loads((FIXTURES / "recipe_serving_specs_golden.json").read_text())
    recipes = recipe_catalog.default_recipes(XTERRA)
    assert [r.name for r in recipes] == pinned["__order__"]
    for recipe in recipes:
        assert repr(recipe_serving_spec(recipe)) == pinned[recipe.name], recipe.name


def test_event_persistence_configuration_is_unchanged() -> None:
    by_name = {r.name: r for r in recipe_catalog.default_recipes(XTERRA)}
    for mode in ("idle", "city", "highway"):
        stft = by_name[f"stft_bank_balance_{mode}"].event_persistence
        assert stft is not None
        assert stft.reset_gap_seconds == STFT_BANK_BALANCE_SMOOTHING_SECONDS
        assert by_name[f"ltft_bank_balance_{mode}"].event_persistence is None
    afr = by_name["afr_bank_balance_robust"].event_persistence
    assert afr is not None and afr.reset_gap_seconds == 30.0
    assert sum(r.event_persistence is not None for r in by_name.values()) == 4


def test_a_profile_cannot_name_a_catalog_module() -> None:
    """``shared`` and ``catalog`` are modules of the package, not vehicles."""
    assert NON_PROFILE_MODULES == {"catalog", "shared"}
    for slug in NON_PROFILE_MODULES:
        assert profile_recipes(slug) == []
        assert recipe_catalog.default_recipes(slug) == shared_catalog.shared_recipes()


def test_every_recipes_module_is_a_vehicle_or_declared_not_one() -> None:
    """A module that defines no ``recipes()`` must be in ``NON_PROFILE_MODULES``,
    or a profile slug of that name would raise instead of fitting the shared catalog."""
    for path in sorted((ANALYSIS / "recipes").glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text())
        defines = any(
            isinstance(node, ast.FunctionDef) and node.name == "recipes" for node in tree.body
        )
        assert defines or path.stem in NON_PROFILE_MODULES, (
            f"{path.name} defines no recipes() and is not in NON_PROFILE_MODULES"
        )
        assert not (defines and path.stem in NON_PROFILE_MODULES), path.name
