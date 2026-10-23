"""R21: the configuration package depends one way, and ``src.config`` stays layer 0.

``src/config.py`` became ``src/config/``. This holds the dependency map and
fails on a real forbidden edge, whatever form the import takes: at module
level, in a function, under ``TYPE_CHECKING`` or ``try``, absolute or relative.

::

    base           none              _Strict, broadcast constants, validator
    vehicle        base              vehicle:
    hardware       base              hardware:           (+ src.lsm6dsox_settings, src.paths)
    collection     base              collection:
    preprocessing  base              preprocessing:
    models         base              models: and alerts:
    daemon         base              dashboard: and daemon: (+ src.paths)
    auxiliary      base              the small independent sections (+ src.paths)
    root           base + the seven  CanaryConfig and the validators that read two sections
                   section modules
    scoping        daemon, root      rebase_state_paths, require_profile_scoped
    __init__       every module      the facade: defines nothing

The facade is imported by callers outside the package and never by a module
inside it; a section schema never imports ``root`` or ``scoping``; outside the
package only the two layer-0 leaves ``src.paths`` and ``src.lsm6dsox_settings``
are imported (the layering test places ``config`` in layer 0), and only
``pydantic`` and ``yaml`` beyond the standard library, so importing the
configuration cannot pull in torch, CAN IO, the daemon, the dashboard or the
vehicle-profile lookup.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.test_layering import LAYERS

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE = SRC / "config"
FACADE = "src.config"

#: Modules a module may import from inside the package.
DEPENDS_ON: dict[str, frozenset[str]] = {
    "base": frozenset(),
    "vehicle": frozenset({"base"}),
    "hardware": frozenset({"base"}),
    "collection": frozenset({"base"}),
    "preprocessing": frozenset({"base"}),
    "models": frozenset({"base"}),
    "daemon": frozenset({"base"}),
    "auxiliary": frozenset({"base"}),
    "root": frozenset(
        {
            "base",
            "vehicle",
            "hardware",
            "collection",
            "preprocessing",
            "models",
            "daemon",
            "auxiliary",
        }
    ),
    "scoping": frozenset({"daemon", "root"}),
    "__init__": frozenset(
        {
            "base",
            "vehicle",
            "hardware",
            "collection",
            "preprocessing",
            "models",
            "daemon",
            "auxiliary",
            "root",
            "scoping",
        }
    ),
}
#: Modules that define section schemas: they sit below the root assembly.
SECTION_SCHEMAS = frozenset(
    {"vehicle", "hardware", "collection", "preprocessing", "models", "daemon", "auxiliary"}
)
#: ``src`` modules the package may import, and the third-party roots.
ALLOWED_SRC = frozenset(
    {
        "src.paths",
        "src.lsm6dsox_settings",
        # The repo's one YAML loader (tests/test_yaml_load.py): yaml alone.
        "src.yaml_load",
    }
)
ALLOWED_THIRD_PARTY = frozenset({"pydantic", "yaml"})

#: Every name ``src/config.py`` defined at the commit before R21, public and
#: private: the old import surface the facade keeps.
OLD_NAMES: tuple[str, ...] = (
    "AlertsConfig",
    "AutoTagConfig",
    "CanBusConfig",
    "CanaryConfig",
    "CollectionConfig",
    "CommitteesConfig",
    "DAEMON_STATE_LAYOUT",
    "DAEMON_TRACKED_PATHS",
    "DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS",
    "DEFAULT_PRIORITY_CHANNELS",
    "DaemonConfig",
    "DashboardConfig",
    "DetectorRole",
    "DetectorRolesConfig",
    "FusionConfig",
    "GpsConfig",
    "HardwareConfig",
    "HatAdcConfig",
    "HatRevisionSetting",
    "IdleShutdownConfig",
    "ImuConfig",
    "IsolationForestConfig",
    "KlineConfig",
    "LLMAssistConfig",
    "LSTMAutoencoderConfig",
    "MIN_BROADCAST_STALE_TIMEOUT_SECONDS",
    "ModelsConfig",
    "NonvolatileWriteConfig",
    "NotificationsConfig",
    "OnlineTrainingConfig",
    "OperatingModeSpec",
    "PlausibilityConfig",
    "PreprocessingConfig",
    "ProfilesConfig",
    "REWRITE_LEAD_SECONDS",
    "RecordArmingConfig",
    "SensorCanConfig",
    "SupplyVoltageConfig",
    "TCNAutoencoderConfig",
    "TrendsConfig",
    "TuningReadinessConfig",
    "TwinLiveConfig",
    "UnscopedConfigError",
    "UsbAdcConfig",
    "VehicleConfig",
    "VehicleParameters",
    "_SequenceTrainingConfig",
    "_Strict",
    "_daemon_path_fields",
    "_mentions_path",
    "log",
    "rebase_state_paths",
    "require_profile_scoped",
    "validate_broadcast_stale_timeout",
)


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------


def _resolve(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    """The absolute module a ``from`` statement names, relative forms included."""
    if node.level == 0:
        return node.module or ""
    parts = module.split(".")
    if not is_package:
        parts = parts[:-1]
    parts = parts[: len(parts) - (node.level - 1)]
    if node.module:
        parts = [*parts, node.module]
    return ".".join(parts)


def imports_of(
    source: str, module: str, *, is_package: bool = False
) -> list[tuple[str, str | None]]:
    """Every import in ``source`` as ``(module, name)``: ``name`` is the imported
    name of a ``from`` statement, ``None`` for a plain ``import``. Statements
    anywhere count -- functions, classes, ``if TYPE_CHECKING``, ``try``."""
    found: list[tuple[str, str | None]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend((alias.name, None) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve(node, module, is_package)
            found.extend((resolved, alias.name) for alias in node.names)
    return found


def _classify(
    imports: list[tuple[str, str | None]], submodules: frozenset[str]
) -> tuple[set[str], set[str], set[str]]:
    """Split imports into (package modules, facade imports, everything else)."""
    internal: set[str] = set()
    facade: set[str] = set()
    external: set[str] = set()
    for module, name in imports:
        if module == FACADE:
            if name in submodules:
                internal.add(name)
            else:
                facade.add(name or "<module>")
        elif module.startswith(FACADE + "."):
            internal.add(module.split(".")[2])
        elif module == "src" and name == "config":
            facade.add("config")
        elif module == "src" and name is not None:
            external.add(f"src.{name}")
        else:
            external.add(module)
    return internal, facade, external


def _package_files(root: Path = PACKAGE) -> dict[str, Path]:
    return {path.stem: path for path in sorted(root.glob("*.py"))}


def _scan(root: Path = PACKAGE) -> dict[str, tuple[set[str], set[str], set[str]]]:
    files = _package_files(root)
    submodules = frozenset(files) - {"__init__"}
    return {
        stem: _classify(
            imports_of(
                path.read_text(),
                FACADE if stem == "__init__" else f"{FACADE}.{stem}",
                is_package=stem == "__init__",
            ),
            submodules,
        )
        for stem, path in files.items()
    }


def _violations(scan: dict[str, tuple[set[str], set[str], set[str]]]) -> list[str]:
    problems: list[str] = []
    for stem, (internal, facade, external) in sorted(scan.items()):
        if stem != "__init__" and facade:
            problems.append(f"{stem}: imports the facade ({', '.join(sorted(facade))})")
        allowed = DEPENDS_ON.get(stem)
        if allowed is None:
            problems.append(f"{stem}: not in DEPENDS_ON")
            continue
        for other in sorted(internal - allowed - {stem}):
            problems.append(f"{stem}: imports {other}, which it may not depend on")
        for module in sorted(external):
            root_name = module.split(".")[0]
            if module in ALLOWED_SRC or root_name in sys.stdlib_module_names:
                continue
            if root_name in ALLOWED_THIRD_PARTY:
                continue
            problems.append(f"{stem}: imports {module}, outside the layer-0 allowance")
    return problems


# ---------------------------------------------------------------------------
# The package as committed
# ---------------------------------------------------------------------------


def test_the_single_file_module_is_gone_and_the_package_owns_the_name() -> None:
    assert not (SRC / "config.py").exists(), "src/config.py and src/config/ cannot coexist"
    config = importlib.import_module("src.config")
    assert Path(config.__file__ or "") == PACKAGE / "__init__.py"
    assert [Path(p) for p in config.__path__] == [PACKAGE]


def test_every_module_in_the_package_is_in_the_dependency_map() -> None:
    assert set(_package_files()) == set(DEPENDS_ON)
    assert not [p for p in PACKAGE.rglob("*.py") if p.parent != PACKAGE], "no nested packages"


def test_the_package_follows_the_dependency_map() -> None:
    problems = _violations(_scan())
    assert not problems, "\n".join(problems)


def test_no_module_inside_the_package_imports_the_facade() -> None:
    offenders = {stem: facade for stem, (_i, facade, _e) in _scan().items() if facade}
    offenders.pop("__init__", None)
    assert not offenders, f"import the owning module, not src.config: {offenders}"


def test_section_schemas_never_import_the_root_or_the_scoping_helpers() -> None:
    scan = _scan()
    offenders = {
        stem: sorted(scan[stem][0] & {"root", "scoping", "__init__"}) for stem in SECTION_SCHEMAS
    }
    assert not any(offenders.values()), offenders
    assert scan["base"][0] == set(), "base depends on no other module in the package"


def test_the_map_is_acyclic_and_only_the_scoping_helpers_sit_above_the_root() -> None:
    def reaches(start: str) -> set[str]:
        seen: set[str] = set()
        stack = list(DEPENDS_ON[start])
        while stack:
            node = stack.pop()
            if node not in seen:
                seen.add(node)
                stack.extend(DEPENDS_ON[node])
        return seen

    for stem in DEPENDS_ON:
        assert stem not in reaches(stem), f"{stem} depends on itself"
    above_root = {stem for stem in DEPENDS_ON if "root" in reaches(stem)}
    assert above_root == {"scoping", "__init__"}


def test_the_package_imports_no_higher_layer_and_nothing_heavy() -> None:
    externals = {m for _i, _f, external in _scan().values() for m in external}
    sources = {m for m in externals if m.split(".")[0] == "src"}
    assert sources <= ALLOWED_SRC, sorted(sources - ALLOWED_SRC)
    assert all(LAYERS[m.split(".")[1]] == 0 for m in sources)
    assert LAYERS["config"] == 0
    third_party = {
        m.split(".")[0]
        for m in externals
        if m.split(".")[0] not in sys.stdlib_module_names | {"src"}
    }
    assert third_party <= ALLOWED_THIRD_PARTY, sorted(third_party - ALLOWED_THIRD_PARTY)


# ---------------------------------------------------------------------------
# The facade
# ---------------------------------------------------------------------------


def _owner_modules() -> dict[str, ModuleType]:
    return {
        stem: importlib.import_module(f"{FACADE}.{stem}")
        for stem in DEPENDS_ON
        if stem != "__init__"
    }


def test_the_facade_defines_nothing_and_re_exports_each_name_as_itself() -> None:
    tree = ast.parse((PACKAGE / "__init__.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # the docstring
        assert isinstance(node, ast.ImportFrom), ast.dump(node)[:80]
        if node.module == "__future__":
            continue
        for alias in node.names:
            assert alias.asname == alias.name, f"{alias.name} is not re-exported as itself"


def test_the_facade_exports_the_owners_object_for_every_old_name() -> None:
    facade = importlib.import_module(FACADE)
    owners = _owner_modules()
    for name in OLD_NAMES:
        defined_in = [stem for stem, module in owners.items() if name in vars(module)]
        assert len(defined_in) >= 1, f"{name} is defined nowhere"
        owner = owners[defined_in[0]]
        assert getattr(facade, name) is getattr(owner, name), name
        value = getattr(facade, name)
        if isinstance(value, type) or inspect.isfunction(value):
            assert value.__module__ == f"{FACADE}.{defined_in[0]}", (name, value.__module__)


def test_each_old_name_is_defined_in_exactly_one_module() -> None:
    owners = _owner_modules()
    for name in OLD_NAMES:
        # A re-import of another module's object is not a second definition.
        defined = [
            stem
            for stem, module in owners.items()
            if name in vars(module) and _defines(PACKAGE / f"{stem}.py", name)
        ]
        assert len(defined) == 1, f"{name} is defined in {defined}"


def _defines(path: Path, name: str) -> bool:
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name:
            return True
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            return True
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
            return True
    return False


def test_old_and_new_paths_name_the_same_classes() -> None:
    import src.config as facade
    import src.config.root as owner

    assert facade.CanaryConfig is owner.CanaryConfig
    assert owner.CanaryConfig.__module__ == "src.config.root"


def test_every_model_resolves_its_annotations_in_the_module_that_owns_it() -> None:
    """``from __future__ import annotations`` leaves field types as strings that
    pydantic resolves in the *owning* module; a type moved without its import
    leaves the model incomplete or raises here."""
    import typing

    from pydantic import BaseModel

    facade = importlib.import_module(FACADE)
    models = {
        name: value
        for name, value in vars(facade).items()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__.startswith(f"{FACADE}.")
    }
    assert len(models) == 40  # +CompanionConfig (2026-10-16)
    for name, cls in models.items():
        assert cls.__pydantic_complete__, name
        typing.get_type_hints(cls)
        for field_name, field in cls.model_fields.items():
            assert field.annotation is not None, f"{name}.{field_name}"


# ---------------------------------------------------------------------------
# The scanner finds each form of the forbidden import
# ---------------------------------------------------------------------------


def _write(root: Path, stem: str, text: str) -> None:
    (root / f"{stem}.py").write_text(text)


def _planted(tmp_path: Path, stem: str, text: str) -> list[str]:
    """Violations when ``stem.py`` holds ``text`` and every other module is clean."""
    for name in DEPENDS_ON:
        _write(tmp_path, name, "")
    _write(tmp_path, stem, text)
    return _violations(_scan(tmp_path))


@pytest.mark.parametrize(
    "text",
    [
        "from src.config import CanaryConfig\n",
        "import src.config\n",
        "from src import config\n",
        "from src.config import *\n",
        "def f():\n    from src.config import CanaryConfig\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
        "    from src.config import CanaryConfig\n",
        "try:\n    from src.config import CanaryConfig\nexcept ImportError:\n    pass\n",
        "from . import CanaryConfig\n",
        "from .. import config\n",
        "from .. import config as c\nfrom src.config import X\n",
        "from src.config import __init__\n",
    ],
)
def test_a_facade_import_inside_the_package_is_found(tmp_path: Path, text: str) -> None:
    problems = _planted(tmp_path, "vehicle", text)
    assert any("imports the facade" in p for p in problems), (text, problems)


@pytest.mark.parametrize(
    "text",
    [
        "from src.config.root import CanaryConfig\n",
        "import src.config.root\n",
        "from src.config import root\n",
        "from . import root\n",
        "from .root import CanaryConfig\n",
        "from .scoping import rebase_state_paths\n",
        "def f():\n    from src.config.root import CanaryConfig\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
        "    from src.config.root import CanaryConfig\n",
        "try:\n    from src.config.scoping import rebase_state_paths\n"
        "except ImportError:\n    pass\n",
        "class K:\n    def m(self):\n        import src.config.root as r\n",
    ],
)
def test_a_section_schema_importing_the_root_or_scoping_is_found(tmp_path: Path, text: str) -> None:
    problems = _planted(tmp_path, "vehicle", text)
    assert any("may not depend on" in p for p in problems), (text, problems)


@pytest.mark.parametrize(
    "text",
    [
        "import torch\n",
        "def f():\n    import numpy\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import pandas\n",
        "from src.vehicle.profiles import VehicleProfile\n",
        "from src.daemon.runtime import run\n",
        "def f():\n    from src.storage.sessions import latest_session\n",
        "from src import vehicle\n",
        "from ..vehicle import profiles\n",
        "from ..collection import hat_revision\n",
        "import can\n",
    ],
)
def test_an_import_outside_the_layer_zero_allowance_is_found(tmp_path: Path, text: str) -> None:
    problems = _planted(tmp_path, "hardware", text)
    assert any("outside the layer-0 allowance" in p for p in problems), (text, problems)


@pytest.mark.parametrize(
    "text",
    [
        "from src.config.base import _Strict\n",
        "from . import base\n",
        "from .base import _Strict\n",
        "from src.paths import DATA_DIR\n",
        "from src.lsm6dsox_settings import ImuSettings\n",
        "import yaml\nfrom pydantic import Field\nfrom pathlib import Path\n",
        "from src.configuration import Thing\n",  # a look-alike name, still outside
    ],
)
def test_the_allowed_imports_are_not_flagged(tmp_path: Path, text: str) -> None:
    problems = _planted(tmp_path, "hardware", text)
    if "src.configuration" in text:
        assert any("outside the layer-0 allowance" in p for p in problems)
    else:
        assert not problems, problems


def test_the_root_and_scoping_may_import_what_the_map_gives_them(tmp_path: Path) -> None:
    assert not _planted(tmp_path, "root", "from src.config.vehicle import VehicleConfig\n")
    assert not _planted(tmp_path, "scoping", "from src.config.root import CanaryConfig\n")
    assert _planted(tmp_path, "root", "from src.config.scoping import rebase_state_paths\n")
    assert _planted(tmp_path, "base", "from src.config.vehicle import VehicleConfig\n")


def test_a_module_missing_from_the_map_is_reported(tmp_path: Path) -> None:
    for name in DEPENDS_ON:
        _write(tmp_path, name, "")
    _write(tmp_path, "stray", "")
    assert any("stray: not in DEPENDS_ON" in p for p in _violations(_scan(tmp_path)))
