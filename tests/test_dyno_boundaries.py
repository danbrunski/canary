"""R16: the dyno's modules depend one way, and the old import path is a facade.

``src/analysis/dyno.py`` (2,722 lines) held motion contracts, squat, inertia,
road load, comparison, power and the stored profile. The owners are now

====================== ===========================================================
``dyno_contract``      ``DynoRefusal``, ``MotionSamples``, ``MotionSources``, array
                       validation, the protocols (leaf)
``dyno_motion``        specific force, pitch offset, the IMU-frame refusal
``dyno_altitude_noise`` parked GPS-altitude noise
``dyno_squat``         the squat fit and its record
``dyno_driveline``     stored inertia facts, ``Driveline``
``dyno_inertia``       the two-gear engine-inertia fit
``dyno_road_load``     coast-down road load
``dyno_comparison``    run conditions, noise floor, run comparison
``dyno_power``         ``compute_power``
``dyno_profile``       ``DynoProfile`` and its load / save / path
====================== ===========================================================

``DEPENDS_ON`` below is the dependency map and the rule: a module may import
exactly the dyno modules listed for it. The scan is AST-based, sees imports in
function bodies, ``try`` blocks and ``if TYPE_CHECKING:``, and resolves
relative imports against the file's package. ``src.analysis.dyno`` itself must
stay a pure re-export of the owners' objects.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from src.analysis import dyno

ANALYSIS = Path(__file__).resolve().parents[1] / "src" / "analysis"
PACKAGE = "src.analysis"
FACADE = "src.analysis.dyno"

#: Each dyno module and the dyno modules it may import (and nothing else of the
#: dyno family). Input preparation, pull detection and the workbench sit above
#: all of them and are not listed here as importable by any owner.
DEPENDS_ON: dict[str, frozenset[str]] = {
    "dyno_contract": frozenset(),
    "dyno_motion": frozenset({"dyno_contract"}),
    "dyno_altitude_noise": frozenset({"dyno_contract"}),
    "dyno_squat": frozenset({"dyno_contract", "dyno_motion", "dyno_altitude_noise"}),
    "dyno_driveline": frozenset({"dyno_contract"}),
    # The road-load record, so the fit can move its C into the pulls' air (M01.8).
    "dyno_inertia": frozenset({"dyno_contract", "dyno_motion", "dyno_driveline", "dyno_road_load"}),
    "dyno_road_load": frozenset({"dyno_contract", "dyno_motion", "dyno_driveline"}),
    "dyno_comparison": frozenset({"dyno_contract"}),
    "dyno_power": frozenset(
        {"dyno_contract", "dyno_motion", "dyno_driveline", "dyno_road_load", "dyno_comparison"}
    ),
    "dyno_profile": frozenset(
        {"dyno_contract", "dyno_comparison", "dyno_driveline", "dyno_road_load", "dyno_squat"}
    ),
}

#: Modules above the owners: they import owners, never the other way, and never
#: the facade.
CONSUMERS = ("dyno_inputs", "dyno_pulls", "dyno_workbench")

#: Orchestration outside the dyno family an owner must never import (the
#: workbench job layer); the consumers above are caught by the dependency map.
ORCHESTRATION = ("src.analysis.workbench",)


def _imports(text: str, package: str = PACKAGE) -> set[str]:
    """Every module a source text imports, wherever the import sits.

    Relative imports are resolved against ``package`` (the file's own package);
    ``from pkg import name`` also reports ``pkg.name``.
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


def _is(module: str, forbidden: str) -> bool:
    return module == forbidden or module.startswith(f"{forbidden}.")


def _dyno_family(imported: set[str]) -> set[str]:
    """The dyno modules (owners, consumers and the facade) an import set names."""
    names = {*DEPENDS_ON, *CONSUMERS}
    return {
        short
        for module in imported
        for short in (module.removeprefix(f"{PACKAGE}."),)
        if module.startswith(f"{PACKAGE}.") and (short in names or short == "dyno")
    }


def _violations(files: Mapping[str, str]) -> list[str]:
    """``module -> imported`` for each import its rule forbids."""
    found: set[str] = set()
    for name, text in files.items():
        imported = _imports(text)
        allowed = DEPENDS_ON.get(name)
        if allowed is not None:
            for dep in sorted(_dyno_family(imported) - allowed - {name}):
                found.add(f"{name} -> {dep}")
            for banned in ORCHESTRATION:
                if any(_is(module, banned) for module in imported):
                    found.add(f"{name} -> {banned}")
        elif name in CONSUMERS:
            if "dyno" in _dyno_family(imported):
                found.add(f"{name} -> dyno")
    return sorted(found)


def _files() -> dict[str, str]:
    return {name: (ANALYSIS / f"{name}.py").read_text() for name in (*DEPENDS_ON, *CONSUMERS)}


def test_the_dependency_map_covers_every_dyno_module_on_disk() -> None:
    on_disk = {p.stem for p in ANALYSIS.glob("dyno*.py")}
    assert on_disk == {"dyno", *DEPENDS_ON, *CONSUMERS}


def test_every_owner_imports_only_the_modules_the_map_allows() -> None:
    assert _violations(_files()) == []


def test_the_map_has_no_cycle() -> None:
    done: set[str] = set()

    def visit(name: str, trail: tuple[str, ...]) -> None:
        assert name not in trail, f"cycle: {' -> '.join((*trail, name))}"
        if name in done:
            return
        for dep in DEPENDS_ON[name]:
            visit(dep, (*trail, name))
        done.add(name)

    for name in DEPENDS_ON:
        visit(name, ())


def test_nothing_in_analysis_imports_the_facade() -> None:
    """Internal modules import their owners; ``dyno`` is for the outside."""
    offenders = []
    for path in sorted(ANALYSIS.rglob("*.py")):
        if path == ANALYSIS / "dyno.py":
            continue
        package = ".".join(path.relative_to(ANALYSIS.parents[1]).with_suffix("").parts[:-1])
        if FACADE in _imports(path.read_text(), package):
            offenders.append(str(path.relative_to(ANALYSIS)))
    assert offenders == []


@pytest.mark.parametrize(
    "label, name, text, expected",
    [
        ("top", "dyno_contract", "from src.analysis.dyno_motion import x\n", ["dyno_motion"]),
        (
            "function",
            "dyno_contract",
            "def f():\n    from src.analysis.dyno_power import x\n",
            ["dyno_power"],
        ),
        (
            "type_checking",
            "dyno_motion",
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
            "    import src.analysis.dyno_squat\n",
            ["dyno_squat"],
        ),
        (
            "try",
            "dyno_comparison",
            "try:\n    from src.analysis.dyno_profile import x\nexcept ImportError:\n    pass\n",
            ["dyno_profile"],
        ),
        (
            "package",
            "dyno_driveline",
            "from src.analysis import dyno_road_load\n",
            ["dyno_road_load"],
        ),
        ("relative_module", "dyno_contract", "from .dyno_motion import x\n", ["dyno_motion"]),
        ("relative_package", "dyno_contract", "from . import dyno_motion\n", ["dyno_motion"]),
        ("facade", "dyno_power", "from src.analysis.dyno import compute_power\n", ["dyno"]),
        ("facade_package", "dyno_power", "from src.analysis import dyno\n", ["dyno"]),
        ("facade_relative", "dyno_power", "from .dyno import x\n", ["dyno"]),
        (
            "consumer",
            "dyno_power",
            "from src.analysis.dyno_workbench import DynoRequest\n",
            ["dyno_workbench"],
        ),
        ("sibling_not_allowed", "dyno_squat", "from .dyno_inertia import x\n", ["dyno_inertia"]),
        ("allowed", "dyno_squat", "from src.analysis.dyno_motion import distance_m\n", []),
        ("lookalike", "dyno_squat", "import src.analysis.dyno_motion_x\n", []),
        ("unrelated", "dyno_squat", "from src.analysis.channels import read_channels\n", []),
    ],
)
def test_the_scanner_finds_every_form_of_a_forbidden_import(
    label: str, name: str, text: str, expected: list[str]
) -> None:
    assert _violations({name: text}) == [f"{name} -> {dep}" for dep in expected], label


def test_an_owner_importing_the_workbench_or_a_consumer_is_found() -> None:
    files = {
        "dyno_power": "def f():\n    from src.analysis.workbench import WorkbenchSelection\n",
        "dyno_profile": "from src.analysis.dyno_inputs import DynoInputs\n",
    }
    assert _violations(files) == [
        "dyno_power -> src.analysis.workbench",
        "dyno_profile -> dyno_inputs",
    ]


def test_a_consumer_importing_the_facade_is_found() -> None:
    assert _violations({"dyno_pulls": "from src.analysis.dyno import Driveline\n"}) == [
        "dyno_pulls -> dyno"
    ]


# ---------------------------------------------------------------------------
# Import-time: loading an owner loads nothing above it.
# ---------------------------------------------------------------------------


def _closure(name: str) -> set[str]:
    out = {name}
    for dep in DEPENDS_ON[name]:
        out |= _closure(dep)
    return out


def _dyno_modules_loaded_after(module: str) -> set[str]:
    code = (
        "import sys\n"
        f"import {module}\n"
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('src.analysis.dyno'))))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=ANALYSIS.parents[1],
    )
    return {m.removeprefix(f"{PACKAGE}.") for m in done.stdout.split()}


@pytest.mark.parametrize("name", sorted(DEPENDS_ON))
def test_loading_an_owner_loads_only_its_dependencies(name: str) -> None:
    assert _dyno_modules_loaded_after(f"{PACKAGE}.{name}") == _closure(name)


# ---------------------------------------------------------------------------
# Compatibility: ``src.analysis.dyno`` re-exports the owners' own objects.
# ---------------------------------------------------------------------------

#: Public name -> owner. Every name in ``dyno.__all__`` is assigned exactly one
#: owner here, so a new export has to be placed.
EXPORTS: dict[str, tuple[str, ...]] = {
    "dyno_contract": (
        "GPS_SPEED_CHANNEL",
        "MIN_SAMPLES",
        "DynoRefusal",
        "FloatArray",
        "MotionSamples",
        "MotionSources",
        "RefusalKind",
        "SpeedSource",
    ),
    "dyno_motion": (
        "Leg",
        "PitchOffset",
        "correct_specific_force",
        "estimate_pitch_offset",
        "frame_mismatch",
        "pitch_deg",
    ),
    "dyno_altitude_noise": (
        "ALTITUDE_NOISE_MAX_SPEED_MPS",
        "ALTITUDE_NOISE_MIN_REFERENCE_S",
        "AltitudeNoise",
        "measure_altitude_noise",
    ),
    "dyno_squat": (
        "SQUAT_FLAT_FAMILY_ALPHA",
        "SQUAT_GRADE_LEAK_SIGMA",
        "SQUAT_GRADE_SEGMENT_M",
        "SQUAT_GRADE_STEP_M",
        "SQUAT_MAX_GRADE",
        "SQUAT_MIN_ACCEL_SPAN_G",
        "SQUAT_MIN_SEGMENT_SAMPLES",
        "NoiseReferences",
        "SquatCoefficient",
        "SquatConditions",
        "SquatFit",
        "fit_squat",
        "_grade_with_se",
    ),
    "dyno_driveline": (
        "INERTIA_CONVENTION",
        "Driveline",
        "EngineInertiaRecord",
        "InertiaConditions",
        "InertiaData",
        "InertiaFlag",
    ),
    "dyno_inertia": (
        "INERTIA_MIN_ALPHA_DIFFERENCE",
        "INERTIA_MIN_PER_BIN",
        "INERTIA_MIN_RPM_OVERLAP",
        "INERTIA_RPM_BIN",
        "EngineInertiaFit",
        "GearPull",
        "fit_engine_inertia",
    ),
    "dyno_road_load": (
        "COAST_MIN_SPEED_SPAN_KPH",
        "DirectionFit",
        "RoadLoadCoefficients",
        "RoadLoadConditions",
        "RoadLoadFit",
        "fit_road_load",
    ),
    "dyno_comparison": (
        "CURVE_GRID_STEP_RPM",
        "FLOOR_MATCH_FIELDS",
        "FLOOR_SAME_DAY_FIELDS",
        "MIN_PULL_PAIRS",
        "QUANTITY_UNITS",
        "BandVerdict",
        "BandVerdictKind",
        "DynoCurve",
        "DynoNoiseFloor",
        "FloorBand",
        "FloorConditions",
        "Quantity",
        "RunComparison",
        "RunConditions",
        "compare_runs",
        "record_dyno_floor",
    ),
    "dyno_power": (
        "ENGINE_ESTIMATE_LABEL",
        "POWER_GEAR_TOLERANCE",
        "POWER_GRADE_SMOOTH_S",
        "POWER_MAX_GRADE_RANGE",
        "POWER_MIN_BIN_FILL",
        "POWER_MIN_PER_BIN",
        "POWER_MIN_RPM_SPAN",
        "WHEEL_TORQUE_CONVENTION",
        "DynoPower",
        "J1349Correction",
        "PowerCurve",
        "PowerPull",
        "compute_power",
    ),
    "dyno_profile": (
        "DYNO_FILENAME",
        "DynoProfile",
        "dyno_profile_path",
        "load_dyno_profile",
        "save_dyno_profile",
    ),
}

#: Names the single-file module also exposed, kept importable by their old
#: spelling (tests import them): old private name -> owner's name.
PRIVATE_ALIASES = {"_grade_with_se": ("dyno_squat", "_grade_with_se")}

#: Plain data and aliases of other objects have no ``__module__`` worth checking.
DATA_ONLY = {
    "FloatArray",
    "RefusalKind",
    "InertiaFlag",
    "BandVerdictKind",
    "Quantity",
    "NoiseReferences",
}


def _all_exports() -> list[tuple[str, str]]:
    return [(owner, name) for owner, names in EXPORTS.items() for name in names]


def test_every_public_name_has_exactly_one_owner() -> None:
    public = [name for _, name in _all_exports() if not name.startswith("_")]
    assert len(public) == len(set(public))
    assert sorted(public) == sorted(dyno.__all__)


@pytest.mark.parametrize("owner, name", _all_exports())
def test_the_facade_exports_the_owners_object(owner: str, name: str) -> None:
    module = importlib.import_module(f"{PACKAGE}.{owner}")
    assert getattr(dyno, name) is getattr(module, name)


@pytest.mark.parametrize(
    "owner, name",
    [(o, n) for o, n in _all_exports() if n not in DATA_ONLY and not n.startswith("_")],
)
def test_a_class_or_function_is_defined_once_in_its_owner(owner: str, name: str) -> None:
    obj = getattr(dyno, name)
    if isinstance(obj, type) or callable(obj):
        assert obj.__module__ == f"{PACKAGE}.{owner}", name


def test_the_facade_defines_nothing() -> None:
    """Re-exports only: a class or function here would be a duplicate definition."""
    tree = ast.parse((ANALYSIS / "dyno.py").read_text())
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    assert defined == []
    assigned = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id != "__all__"
    ]
    assert assigned == []


def test_the_old_private_helpers_tests_use_are_still_importable() -> None:
    for old, (owner, name) in PRIVATE_ALIASES.items():
        module = importlib.import_module(f"{PACKAGE}.{owner}")
        assert getattr(dyno, old) is getattr(module, name)


def test_the_protocols_the_fits_share_are_defined_once() -> None:
    text = "".join(path.read_text() for path in ANALYSIS.glob("dyno*.py"))
    for protocol in ("class SquatLike", "class RoadLoadLike", "class DynoRefusal"):
        assert text.count(protocol) == 1, protocol
