"""R14: the feature pipeline's modules depend one way, and the old path is a facade.

``src/features/feature_pipeline.py`` (2,701 lines) held feature policy, session
decoding and worker orchestration, the batch/live transform, perturbation
dependency traversal, detector feature selection and spec health. The owners
are now

======================== =============================================================
``feature_paths``        repo-rooted default catalog / cache paths (leaf)
``column_health``        finite / constant / all-NaN column health (leaf)
``feature_transform``    the one wide -> features transform, ``FeaturizedFrame`` (leaf)
``feature_policy``       ``FeaturePolicy``, exclusions, ``candidate_feature_columns``
``feature_selection``    trainable columns, coverage floor, IF versus sequence
``feature_spec``         the persisted spec, health, reconciliation
``feature_dependencies`` which channels a derived channel / model reads
``feature_perturbation`` ``featurize_perturbed_frame`` and its full-transform fallback
``session_resampling``   one session's decode -> gate -> resample, decode catalogs
``feature_workers``      worker sizing, spawn entry points, serial fallback
``feature_build``        ``build_feature_frame``, the disposition screen
======================== =============================================================

``DEPENDS_ON`` below is the dependency map and the rule: a module may import
exactly the ``src.features`` modules listed for it. The scan is AST-based, sees
imports in function bodies, ``try`` blocks and ``if TYPE_CHECKING:``, and
resolves relative imports against the file's package. ``feature_pipeline``
itself must stay a pure re-export of the owners' objects, and ``feature_cache``
keeps the cache key and version.
"""

from __future__ import annotations

import ast
import importlib
import pickle
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from src.features import feature_pipeline as facade_module
from tests.test_layering import LAYERS

REPO = Path(__file__).resolve().parents[1]
FEATURES = REPO / "src" / "features"
PACKAGE = "src.features"
FACADE = "src.features.feature_pipeline"

#: Each owner and the ``src.features`` modules it may import (and nothing else
#: of the family). ``feature_cache`` and ``live`` have their own owners (R09.6)
#: and are listed so their edges are pinned too.
DEPENDS_ON: dict[str, frozenset[str]] = {
    "feature_paths": frozenset(),
    "column_health": frozenset(),
    "feature_transform": frozenset(),
    "feature_policy": frozenset({"feature_paths"}),
    "feature_selection": frozenset({"column_health", "feature_policy"}),
    "feature_spec": frozenset({"column_health", "feature_policy", "feature_selection"}),
    "feature_dependencies": frozenset({"feature_transform"}),
    "feature_perturbation": frozenset({"feature_dependencies", "feature_transform"}),
    "session_resampling": frozenset({"feature_paths"}),
    "feature_workers": frozenset({"session_resampling"}),
    "feature_build": frozenset(
        {
            "feature_cache",
            "feature_paths",
            "feature_transform",
            "feature_workers",
            "session_resampling",
        }
    ),
    "feature_cache": frozenset(),
    "live": frozenset({"bus_freshness", "feature_transform", "trip_integrals"}),
    # R27.6: state groups extracted from LiveFeatureSource.
    "trip_integrals": frozenset(),
    "bus_freshness": frozenset(),
    "capture_inputs": frozenset(),
}

#: The new owners (the first nine entries above that R14 created).
NEW_OWNERS = tuple(
    name
    for name in DEPENDS_ON
    if name not in {"feature_cache", "live", "capture_inputs", "trip_integrals", "bus_freshness"}
)

#: Packages an owner must never import: the layers above ``features``, its
#: detection-layer peers (``models`` among them) and the entry points that
#: orchestrate it. (``tests/test_layering.py`` covers the
#: rest of ``src``; it does not see ``scripts`` or ``tools``.)
UPPER_ORCHESTRATION = ("scripts", "tools")


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


def _family(imported: set[str]) -> set[str]:
    """The ``src.features`` modules (owners and the facade) an import set names."""
    names = {*DEPENDS_ON, "feature_pipeline"}
    return {
        short
        for module in imported
        for short in (module.removeprefix(f"{PACKAGE}."),)
        if module.startswith(f"{PACKAGE}.") and short in names
    }


def _above_features(imported: set[str]) -> set[str]:
    """Imported ``src`` packages that are not below ``features`` (or an entry
    point): the layers above it and its detection-layer peers other than the
    behavior models it reads (R09.6). Reusable feature policy must not reach
    into ``models``."""
    limit = LAYERS["features"]
    out: set[str] = set()
    for module in imported:
        parts = module.split(".")
        if parts[0] in UPPER_ORCHESTRATION:
            out.add(parts[0])
        elif (
            parts[0] == "src"
            and len(parts) > 1
            and LAYERS.get(parts[1], 0) >= limit
            and parts[1] not in {"features", "behavior"}
        ):
            out.add(f"src.{parts[1]}")
    return out


def _violations(files: Mapping[str, str]) -> list[str]:
    """``module -> imported`` for each import its rule forbids."""
    found: set[str] = set()
    for name, text in files.items():
        imported = _imports(text)
        allowed = DEPENDS_ON.get(name)
        if allowed is None:
            continue
        for dep in sorted(_family(imported) - allowed - {name}):
            found.add(f"{name} -> {dep}")
        for upper in sorted(_above_features(imported)):
            found.add(f"{name} -> {upper}")
    return sorted(found)


def _files() -> dict[str, str]:
    return {name: (FEATURES / f"{name}.py").read_text() for name in DEPENDS_ON}


def test_the_dependency_map_covers_every_module_on_disk() -> None:
    on_disk = {p.stem for p in FEATURES.glob("*.py")} - {"__init__"}
    assert on_disk == {"feature_pipeline", *DEPENDS_ON}


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


def test_selection_and_spec_share_definitions_without_importing_each_other() -> None:
    """Spec/health reads selection's coverage floor and policy; selection never
    reads the spec. The shared column-health definition sits below both."""
    assert "feature_spec" not in DEPENDS_ON["feature_selection"]
    assert "feature_selection" not in DEPENDS_ON["feature_policy"]
    assert "column_health" in DEPENDS_ON["feature_selection"] & DEPENDS_ON["feature_spec"]


def test_nothing_in_the_repo_but_tests_imports_the_facade() -> None:
    """Internal modules, scripts and tools import their owners; the facade is
    for the outside (and the compatibility tests)."""
    offenders = []
    for root in ("src", "scripts", "tools"):
        for path in sorted((REPO / root).rglob("*.py")):
            if path == FEATURES / "feature_pipeline.py":
                continue
            package = ".".join(path.relative_to(REPO).with_suffix("").parts[:-1])
            if FACADE in _imports(path.read_text(), package):
                offenders.append(str(path.relative_to(REPO)))
    assert offenders == []


@pytest.mark.parametrize(
    "label, name, text, expected",
    [
        (
            "top",
            "feature_policy",
            "from src.features.feature_selection import x\n",
            ["feature_selection"],
        ),
        (
            "function",
            "feature_transform",
            "def f():\n    from src.features.feature_build import x\n",
            ["feature_build"],
        ),
        (
            "type_checking",
            "column_health",
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
            "    import src.features.feature_spec\n",
            ["feature_spec"],
        ),
        (
            "try",
            "feature_paths",
            "try:\n    from src.features.feature_policy import x\nexcept ImportError:\n    pass\n",
            ["feature_policy"],
        ),
        (
            "package",
            "feature_policy",
            "from src.features import feature_spec\n",
            ["feature_spec"],
        ),
        ("relative_module", "feature_policy", "from .feature_spec import x\n", ["feature_spec"]),
        ("relative_package", "feature_policy", "from . import feature_spec\n", ["feature_spec"]),
        (
            "relative_function",
            "feature_selection",
            "def f():\n    from .feature_spec import x\n",
            ["feature_spec"],
        ),
        (
            "facade",
            "feature_spec",
            "from src.features.feature_pipeline import x\n",
            ["feature_pipeline"],
        ),
        (
            "facade_package",
            "feature_spec",
            "from src.features import feature_pipeline\n",
            ["feature_pipeline"],
        ),
        (
            "facade_relative",
            "feature_spec",
            "from .feature_pipeline import x\n",
            ["feature_pipeline"],
        ),
        ("facade_import", "live", "import src.features.feature_pipeline\n", ["feature_pipeline"]),
        (
            "facade_function",
            "live",
            "def f():\n    from . import feature_pipeline\n",
            ["feature_pipeline"],
        ),
        (
            "orchestration_package",
            "feature_build",
            "from src.models import isolation_forest\n",
            ["src.models"],
        ),
        (
            "orchestration_daemon",
            "feature_policy",
            "def f():\n    import src.daemon.inference\n",
            ["src.daemon"],
        ),
        (
            "orchestration_script",
            "feature_build",
            "from scripts.retrain import main\n",
            ["scripts"],
        ),
        (
            "sibling_not_allowed",
            "feature_workers",
            "from .feature_build import x\n",
            ["feature_build"],
        ),
        ("allowed", "feature_workers", "from src.features.session_resampling import x\n", []),
        ("allowed_relative", "feature_workers", "from .session_resampling import x\n", []),
        ("lookalike", "feature_workers", "import src.features.feature_build_x\n", []),
        ("unrelated_lower_layer", "feature_policy", "from src.signals.afr import x\n", []),
        ("unrelated_same_layer_name", "feature_policy", "from src.modelsx import y\n", []),
    ],
)
def test_the_scanner_finds_every_form_of_a_forbidden_import(
    label: str, name: str, text: str, expected: list[str]
) -> None:
    assert _violations({name: text}) == [f"{name} -> {dep}" for dep in expected], label


def test_a_selection_module_importing_the_spec_is_found() -> None:
    """The mutual import the split exists to prevent."""
    text = (
        _files()["feature_selection"]
        + "\nfrom src.features.feature_spec import build_feature_spec\n"
    )
    assert _violations({"feature_selection": text}) == ["feature_selection -> feature_spec"]


# ---------------------------------------------------------------------------
# Import-time: loading an owner loads nothing above it, and never the facade.
# ---------------------------------------------------------------------------


def _closure(name: str) -> set[str]:
    out = {name}
    for dep in DEPENDS_ON[name]:
        out |= _closure(dep)
    return out


def _family_loaded_after(module: str) -> set[str]:
    code = (
        "import sys\n"
        f"import {module}\n"
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('src.features'))))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=REPO
    )
    return {m.removeprefix(f"{PACKAGE}.") for m in done.stdout.split()} - {PACKAGE}


@pytest.mark.parametrize("name", NEW_OWNERS)
def test_loading_an_owner_loads_only_its_dependencies(name: str) -> None:
    assert _family_loaded_after(f"{PACKAGE}.{name}") == _closure(name)


def test_the_live_source_loads_the_transform_not_the_facade() -> None:
    loaded = _family_loaded_after(f"{PACKAGE}.live")
    assert "feature_pipeline" not in loaded
    assert "feature_transform" in loaded


# ---------------------------------------------------------------------------
# Compatibility: ``feature_pipeline`` re-exports the owners' own objects.
# ---------------------------------------------------------------------------

#: Owner module -> ``(facade name, owner's name)``; a bare string is both. Every
#: name in ``feature_pipeline.__all__`` is assigned exactly one owner, so a new
#: export has to be placed.
EXPORTS: dict[str, tuple[str | tuple[str, str], ...]] = {
    "src.features.feature_paths": (
        "DEFAULT_BINDINGS_PATH",
        "DEFAULT_CAPTURE_PROFILES_PATH",
        "DEFAULT_DIDS_PATH",
        "DEFAULT_FEATURE_CACHE_DIR",
    ),
    "src.features.feature_policy": (
        "DBC_PREFIX",
        "DERIVED_PREFIX",
        "DID_PREFIX",
        "DID_VIEW_EXCLUSION",
        "MEMORY_CHANNEL_EXCLUSION",
        "MEMORY_FEATURE_PREFIX",
        "NON_TRAINABLE_COUNTER_CHANNELS",
        "NON_TRAINABLE_GPS_CHANNELS",
        "NON_TRAINABLE_STATE_CHANNELS",
        "OBD_PREFIX",
        "FeaturePolicy",
        "candidate_feature_columns",
        "feature_policy_exclusions",
        "resolve_feature_policy",
    ),
    "src.features.feature_transform": (
        "ALL_MODE",
        "SEGMENTER_SIGNAL_COLUMNS",
        "FeaturizedFrame",
        "configured_behavior_models",
        "featurize_wide_frame",
        "segment_or_constant",
        "segmenter_input_columns",
        ("_append_derived_channels", "append_derived_channels"),
        ("_without_committee_columns", "without_committee_columns"),
    ),
    "src.features.feature_dependencies": (
        "behavior_model_input_roots",
        "derived_channel_inputs",
        "derived_channels_reached",
        ("_column_root", "column_root"),
        ("_derived_ancestors", "derived_ancestors"),
    ),
    "src.features.feature_perturbation": ("featurize_perturbed_frame",),
    "src.features.feature_selection": (
        "BELOW_ROW_COVERAGE",
        "COVERAGE_FLOOR_SCHEMA_KEY",
        "DEVIATION_PREFIX",
        "MIN_DETECTOR_COVERAGE",
        "MIN_ISOLATION_FOREST_COVERAGE",
        "SPARSE_FOR_GRID",
        "CoverageExclusion",
        "DetectorFeatureSelection",
        "apply_coverage_floor",
        "detector_coverage",
        "detector_coverage_rows",
        "detector_grid_density",
        "expand_suppress_channels",
        "isolation_forest_feature_columns",
        "isolation_forest_feature_selection",
        "low_coverage_columns",
        "restrict_isolation_forest_expansion",
        "sequence_feature_columns",
        "sequence_feature_selection",
        "trainable_feature_columns",
    ),
    "src.features.feature_spec": (
        "FEATURE_SPEC_VERSION",
        "FeatureSpecReconciliation",
        "_resolve_structural_columns",
        "align_features_to_schema",
        "build_feature_spec",
        "consumed_behavior_fingerprints",
        "reconcile_feature_frame_to_spec",
        "summarize_feature_health",
        "validate_feature_health",
    ),
    "src.features.session_resampling": (
        "default_decode_inputs",
        "drop_fully_stale_rows",
        ("_drop_fully_stale_rows", "_drop_fully_stale_rows"),
        ("_decode_session_to_resampled", "decode_session_to_resampled"),
        "load_resampled_session",
        "resample_long_session",
        "resample_session_bounded",
    ),
    "src.features.feature_build": ("build_feature_frame", "disposition_screen_frames"),
    "src.features.feature_cache": (
        "FEATURE_FRAME_CACHE_VERSION",
        "build_feature_frame_cache_key",
    ),
    "src.preprocessing.resampler": ("resample_config_for",),
}


def _all_exports() -> list[tuple[str, str, str]]:
    out = []
    for module, names in EXPORTS.items():
        for entry in names:
            facade_name, owner_name = (entry, entry) if isinstance(entry, str) else entry
            out.append((module, facade_name, owner_name))
    return out


def test_every_exported_name_has_exactly_one_owner() -> None:
    names = [facade for _, facade, _ in _all_exports()]
    assert len(names) == len(set(names))
    assert sorted(names) == sorted(facade_module.__all__)


@pytest.mark.parametrize("module, facade_name, owner_name", _all_exports())
def test_the_facade_exports_the_owners_object(
    module: str, facade_name: str, owner_name: str
) -> None:
    owner = importlib.import_module(module)
    assert getattr(facade_module, facade_name) is getattr(owner, owner_name)


@pytest.mark.parametrize(
    "module, facade_name, owner_name",
    [
        (m, f, o)
        for m, f, o in _all_exports()
        if m.startswith("src.features.")
        and isinstance(getattr(facade_module, f), type)
        or (m.startswith("src.features.") and callable(getattr(facade_module, f)))
    ],
)
def test_a_class_or_function_is_defined_once_in_its_owner(
    module: str, facade_name: str, owner_name: str
) -> None:
    obj = getattr(facade_module, facade_name)
    assert obj.__module__ == module, facade_name


def test_the_facade_defines_nothing() -> None:
    """Re-exports only: a def, class or assignment here would be logic."""
    tree = ast.parse((FEATURES / "feature_pipeline.py").read_text())
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
    others = [
        type(node).__name__
        for node in tree.body
        if not isinstance(node, ast.ImportFrom | ast.Assign | ast.Expr)
    ]
    assert others == []


# ---------------------------------------------------------------------------
# One transform, one cache owner, spawn-importable workers.
# ---------------------------------------------------------------------------


def test_one_featurize_wide_frame_serves_batch_live_and_the_fast_path() -> None:
    from src.features import feature_build, feature_perturbation, feature_transform, live

    shared = feature_transform.featurize_wide_frame
    assert facade_module.featurize_wide_frame is shared
    assert feature_build.featurize_wide_frame is shared  # type: ignore[attr-defined]
    assert feature_perturbation.featurize_wide_frame is shared  # type: ignore[attr-defined]
    assert live.featurize_wide_frame is shared  # type: ignore[attr-defined]
    defined = [
        str(path.relative_to(REPO))
        for root in ("src", "scripts", "tools")
        for path in sorted((REPO / root).rglob("*.py"))
        if "def featurize_wide_frame(" in path.read_text()
    ]
    assert defined == ["src/features/feature_transform.py"]


def test_the_feature_cache_key_and_version_stay_in_feature_cache() -> None:
    owners = [
        name
        for name in sorted(DEPENDS_ON)
        if "FEATURE_FRAME_CACHE_VERSION =" in (FEATURES / f"{name}.py").read_text()
        or "def build_feature_frame_cache_key(" in (FEATURES / f"{name}.py").read_text()
    ]
    assert owners == ["feature_cache"]


def test_the_deviation_prefix_is_the_behavior_decoders() -> None:
    from src.behavior.behavior_decoder import DEVIATION_COLUMN_PREFIX
    from src.features import feature_selection

    assert feature_selection.DEVIATION_PREFIX == DEVIATION_COLUMN_PREFIX == "deviation:"


def test_the_spawn_entry_points_are_top_level_and_importable_by_name() -> None:
    from src.features import feature_workers

    for fn in (feature_workers._init_feature_worker, feature_workers._worker_process_session):
        assert fn.__module__ == "src.features.feature_workers"
        assert fn.__qualname__ == fn.__name__, "a spawn worker cannot pickle a nested function"
        assert pickle.loads(pickle.dumps(fn)) is fn
    # A fresh interpreter (as a spawn worker is) resolves them by qualified name.
    payload = pickle.dumps(feature_workers._worker_process_session)
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pickle, sys; f = pickle.loads(sys.stdin.buffer.read()); print(f.__module__)",
        ],
        input=payload,
        capture_output=True,
        check=True,
        cwd=REPO,
    )
    assert done.stdout.decode().strip() == "src.features.feature_workers"


def test_a_pickle_naming_the_old_module_still_loads() -> None:
    """Moving a class changes its ``__module__``, so a pickle written before R14
    names ``src.features.feature_pipeline``; the facade's re-export resolves it."""
    from src.features.feature_selection import CoverageExclusion

    original = CoverageExclusion("sparse_for_grid", 0.25, "obd:X")
    text = pickle.dumps(original, protocol=0)  # protocol 0 spells the module in plain text
    assert b"src.features.feature_selection" in text
    old = text.replace(b"src.features.feature_selection", FACADE.encode())
    assert pickle.loads(old) == original
