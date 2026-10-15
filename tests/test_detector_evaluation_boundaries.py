"""R15: the detector evaluation is a library with one dependency direction.

``scripts/evaluate_detectors.py`` was 3,054 lines: the CLI, the scoring kernel
and its worker pool, the cache identity, the summaries, the Markdown and a
694-line ``_evaluate``. The owners are now

====================================== ===============================================
``models.evaluation_records``          ``SessionScore``, ``DriveRecord``, ``drop_in_sample``
``models.evaluation_summaries``        recall, false alarms, VUS-PR, shift, budget threshold
``models.evaluation_streams``          arrived-sample streams, read back without holding
``models.evaluation_identity``         ``KeySources``, ``key_inputs``, the feature key
``models.evaluation_scorer``           ``DriveScorer``, ``DriveTask``/``DriveResult``
``models.evaluation_workers``          the spawned scoring pool and its entry points
``models.evaluation_loading``          sessions through the derived-frame store
``models.evaluation_heldout``          what each artifact has seen, per detector
``models.evaluation_baselines``        the fitted baselines and their thresholds
``models.evaluation_report``           the Markdown renderer
``analysis.detector_evaluation_*``     the phases that need the labelled events
``cgroup_memory``                      the cgroup ceiling parser (was ``daemon.health``)
====================================== ===============================================

``DEPENDS_ON`` is the dependency map and the rule: a module may import exactly
the evaluation modules listed for it. The scan is AST-based, sees imports in
function bodies, ``try`` blocks and ``if TYPE_CHECKING:``, and resolves relative
imports against the file's package. No library module may import ``scripts`` or
``tools``; the script is the only caller and re-exports the owners' objects.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pickle
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.test_layering import LAYERS

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "evaluate_detectors.py"

M = "src.models."
A = "src.analysis."

#: Each evaluation module and the evaluation modules it may import (and
#: nothing else of the family). ``evaluation``, ``evaluation_detectors`` and
#: ``evaluation_cache`` pre-date R15 and are the lower layer the new ones sit on.
DEPENDS_ON: dict[str, frozenset[str]] = {
    f"{M}evaluation": frozenset(),
    f"{M}evaluation_cache": frozenset(),
    f"{M}evaluation_detectors": frozenset(),
    f"{M}evaluation_records": frozenset({f"{M}evaluation", f"{M}evaluation_detectors"}),
    f"{M}evaluation_summaries": frozenset({f"{M}evaluation", f"{M}evaluation_records"}),
    f"{M}evaluation_streams": frozenset({f"{M}evaluation_detectors"}),
    f"{M}evaluation_identity": frozenset({f"{M}evaluation_cache"}),
    f"{M}evaluation_loading": frozenset(),
    f"{M}evaluation_heldout": frozenset(),
    f"{M}evaluation_report": frozenset({f"{M}evaluation"}),
    f"{M}evaluation_scorer": frozenset(
        {
            f"{M}evaluation",
            f"{M}evaluation_cache",
            f"{M}evaluation_detectors",
            f"{M}evaluation_records",
            f"{M}evaluation_streams",
        }
    ),
    f"{M}evaluation_workers": frozenset(
        {
            f"{M}evaluation",
            f"{M}evaluation_cache",
            f"{M}evaluation_detectors",
            f"{M}evaluation_identity",
            f"{M}evaluation_scorer",
        }
    ),
    f"{M}evaluation_baselines": frozenset(
        {
            f"{M}evaluation",
            f"{M}evaluation_cache",
            f"{M}evaluation_detectors",
            f"{M}evaluation_scorer",
        }
    ),
    f"{A}detector_evaluation_drives": frozenset({f"{M}evaluation", f"{M}evaluation_records"}),
    f"{A}detector_evaluation_inputs": frozenset(
        {
            f"{M}evaluation",
            f"{M}evaluation_heldout",
            f"{M}evaluation_identity",
        }
    ),
    f"{A}detector_evaluation_plan": frozenset(
        {
            f"{A}detector_evaluation_inputs",
            f"{M}evaluation",
            f"{M}evaluation_heldout",
            f"{M}evaluation_loading",
        }
    ),
    f"{A}detector_evaluation_run": frozenset(
        {
            f"{A}detector_evaluation_drives",
            f"{A}detector_evaluation_inputs",
            f"{A}detector_evaluation_plan",
            f"{M}evaluation_baselines",
            f"{M}evaluation_cache",
            f"{M}evaluation_detectors",
            f"{M}evaluation_identity",
            f"{M}evaluation_records",
            f"{M}evaluation_scorer",
            f"{M}evaluation_streams",
            f"{M}evaluation_workers",
        }
    ),
    f"{A}detector_evaluation_report": frozenset(
        {
            f"{A}detector_evaluation_drives",
            f"{A}detector_evaluation_inputs",
            f"{A}detector_evaluation_plan",
            f"{A}detector_evaluation_run",
            f"{M}evaluation_baselines",
            f"{M}evaluation_detectors",
            f"{M}evaluation_summaries",
        }
    ),
}

#: The modules R15 created (the rest of the map is pinned so its edges cannot grow).
NEW_MODULES = tuple(
    name
    for name in DEPENDS_ON
    if name.rsplit(".", 1)[1] not in {"evaluation", "evaluation_cache", "evaluation_detectors"}
)


def _path(module: str) -> Path:
    return REPO / (module.replace(".", "/") + ".py")


def _package(module: str) -> str:
    return module.rsplit(".", 1)[0]


def _imports(text: str, package: str) -> set[str]:
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


def _family(imported: set[str]) -> set[str]:
    return {module for module in imported if module in DEPENDS_ON}


def _upward(imported: set[str]) -> set[str]:
    """Imports an evaluation module must not make: the entry points, and any
    ``src`` package above ``models`` (the analysis phases are the only modules
    of the family allowed to reach the workbench layer, and they do not reach
    higher)."""
    out: set[str] = set()
    for module in imported:
        parts = module.split(".")
        if parts[0] in {"scripts", "tools"}:
            out.add(parts[0])
        elif parts[0] == "src" and len(parts) > 1 and LAYERS.get(parts[1], 0) > LAYERS["analysis"]:
            out.add(f"src.{parts[1]}")
    return out


def _above_models(imported: set[str]) -> set[str]:
    out: set[str] = set()
    for module in imported:
        parts = module.split(".")
        if parts[0] == "src" and len(parts) > 1 and LAYERS.get(parts[1], 0) > LAYERS["models"]:
            out.add(f"src.{parts[1]}")
    return out


def _violations(files: Mapping[str, str]) -> list[str]:
    found: set[str] = set()
    for name, text in files.items():
        imported = _imports(text, _package(name))
        for dep in sorted(_family(imported) - DEPENDS_ON[name] - {name}):
            found.add(f"{name} -> {dep}")
        for upper in sorted(_upward(imported)):
            found.add(f"{name} -> {upper}")
        if name.startswith(M):
            for upper in sorted(_above_models(imported)):
                found.add(f"{name} -> {upper}")
    return sorted(found)


def _files() -> dict[str, str]:
    return {name: _path(name).read_text() for name in DEPENDS_ON}


def test_every_module_of_the_map_exists_and_is_in_its_layer() -> None:
    for name in DEPENDS_ON:
        assert _path(name).is_file(), name


def test_every_evaluation_module_imports_only_the_modules_the_map_allows() -> None:
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


def test_the_map_names_only_modules_that_exist() -> None:
    for deps in DEPENDS_ON.values():
        assert deps <= set(DEPENDS_ON)


def test_models_modules_import_no_analysis_and_analysis_modules_only_models_below_them() -> None:
    """The split by import: what needs the labelled events (the workbench) is
    analysis, what does not is models. A models module reaching ``src.analysis``
    would be an upward import ``tests/test_layering.py`` also refuses."""
    for name in DEPENDS_ON:
        imported = _imports(_path(name).read_text(), _package(name))
        analysis = {m for m in imported if m == "src.analysis" or m.startswith(A)}
        if name.startswith(M):
            assert analysis == set(), name


def test_no_module_of_src_imports_a_script_or_a_tool() -> None:
    """``src -> scripts`` is invisible to ``tests/test_layering.py`` (its unit is
    a package under ``src``): the evaluation lived in a script, and a library
    that imported it back would be a cycle through the CLI."""
    offenders = []
    for path in sorted((REPO / "src").rglob("*.py")):
        package = ".".join(path.relative_to(REPO).with_suffix("").parts[:-1])
        for module in _imports(path.read_text(), package):
            if module.split(".")[0] in {"scripts", "tools"}:
                offenders.append(f"{path.relative_to(REPO)} -> {module}")
    assert offenders == []


@pytest.mark.parametrize(
    "label, name, text, expected",
    [
        (
            "top",
            f"{M}evaluation_records",
            "from src.models.evaluation_scorer import x\n",
            [f"{M}evaluation_scorer"],
        ),
        (
            "function",
            f"{M}evaluation_summaries",
            "def f():\n    from src.models.evaluation_workers import x\n",
            [f"{M}evaluation_workers"],
        ),
        (
            "type_checking",
            f"{M}evaluation_identity",
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
            "    import src.models.evaluation_scorer\n",
            [f"{M}evaluation_scorer"],
        ),
        (
            "try",
            f"{M}evaluation_loading",
            "try:\n    from src.models.evaluation_heldout import x\nexcept ImportError:\n"
            "    pass\n",
            [f"{M}evaluation_heldout"],
        ),
        (
            "package",
            f"{M}evaluation_report",
            "from src.models import evaluation_workers\n",
            [f"{M}evaluation_workers"],
        ),
        (
            "relative_module",
            f"{M}evaluation_report",
            "from .evaluation_workers import x\n",
            [f"{M}evaluation_workers"],
        ),
        (
            "relative_package",
            f"{M}evaluation_report",
            "from . import evaluation_workers\n",
            [f"{M}evaluation_workers"],
        ),
        (
            "relative_function",
            f"{M}evaluation_streams",
            "def f():\n    from .evaluation_scorer import x\n",
            [f"{M}evaluation_scorer"],
        ),
        (
            "relative_parent",
            f"{A}detector_evaluation_drives",
            "from ..models.evaluation_workers import x\n",
            [f"{M}evaluation_workers"],
        ),
        (
            "analysis_from_models",
            f"{M}evaluation_report",
            "from src.analysis.labelled_events import LabelledEvent\n",
            ["src.analysis"],
        ),
        (
            "analysis_relative",
            f"{M}evaluation_report",
            "from ..analysis import labelled_events\n",
            ["src.analysis"],
        ),
        (
            "daemon_from_models",
            f"{M}evaluation_workers",
            "def f():\n    from src.daemon.health import memory_limit_status\n",
            ["src.daemon"],
        ),
        (
            "script_from_library",
            f"{A}detector_evaluation_run",
            "from scripts.evaluate_detectors import main\n",
            ["scripts"],
        ),
        (
            "script_function",
            f"{M}evaluation_scorer",
            "def f():\n    import scripts.evaluate_detectors\n",
            ["scripts"],
        ),
        (
            "tool",
            f"{A}detector_evaluation_report",
            "from tools import replay_event_diff\n",
            ["tools"],
        ),
        (
            "allowed",
            f"{M}evaluation_workers",
            "from src.models.evaluation_scorer import DriveScorer\n",
            [],
        ),
        ("allowed_relative", f"{M}evaluation_workers", "from .evaluation_scorer import x\n", []),
        ("lookalike", f"{M}evaluation_records", "import src.models.evaluation_scorer_x\n", []),
        (
            "lower_layer",
            f"{M}evaluation_records",
            "from src.features.feature_transform import x\n",
            [],
        ),
    ],
)
def test_the_scanner_finds_every_form_of_a_forbidden_import(
    label: str, name: str, text: str, expected: list[str]
) -> None:
    assert _violations({name: text}) == [f"{name} -> {dep}" for dep in expected], label


def test_a_records_module_importing_the_scorer_is_found() -> None:
    """The cycle the split exists to prevent: the scorer builds on the records."""
    text = (
        _files()[f"{M}evaluation_records"]
        + "\nfrom src.models.evaluation_scorer import DriveScorer\n"
    )
    assert _violations({f"{M}evaluation_records": text}) == [
        f"{M}evaluation_records -> {M}evaluation_scorer"
    ]


# ---------------------------------------------------------------------------
# One definition, one owner.
# ---------------------------------------------------------------------------


def _defined(path: Path) -> list[str]:
    return [
        node.name
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def test_the_script_defines_only_the_cli() -> None:
    """The script owns the arguments, the request, ``main`` and ``_evaluate``;
    everything it used to define is a re-export of its owner."""
    assert _defined(SCRIPT) == [
        "parse_args",
        "_utc_date",
        "_profile_data_dir",
        "_harness_files",
        "request_from_args",
        "main",
        "_evaluate",
    ]


def test_no_library_class_or_function_is_defined_twice() -> None:
    seen: dict[str, str] = {}
    duplicates = []
    for name in NEW_MODULES:
        for definition in _defined(_path(name)):
            if definition.startswith("__"):
                continue
            if definition in seen:
                duplicates.append(f"{definition}: {seen[definition]} and {name}")
            seen[definition] = name
    assert duplicates == []


def test_the_cgroup_parser_has_one_definition_and_health_re_exports_it() -> None:
    from src import cgroup_memory
    from src.daemon import health

    assert health.memory_limit_status is cgroup_memory.memory_limit_status
    assert health.CGROUP_ROOT is cgroup_memory.CGROUP_ROOT
    defined = [
        str(path.relative_to(REPO))
        for root in ("src", "scripts", "tools")
        for path in sorted((REPO / root).rglob("*.py"))
        if "def memory_limit_status(" in path.read_text()
    ]
    assert defined == ["src/cgroup_memory.py"]


# ---------------------------------------------------------------------------
# Compatibility: the script re-exports the owners' own objects.
# ---------------------------------------------------------------------------

#: Owner module -> ``(script name, owner's name)``; a bare string is both. Every
#: name the script's ``__all__`` re-exports is assigned exactly one owner, so a
#: new export has to be placed.
EXPORTS: dict[str, tuple[str | tuple[str, str], ...]] = {
    f"{M}evaluation_records": ("DriveRecord", "SessionScore", "drop_in_sample", "session_scores"),
    f"{M}evaluation_summaries": (
        "budget_thresholds_on",
        "compact_injections",
        "summarize_false_alarms",
        "summarize_recall",
        "summarize_shift",
        "summarize_vus",
    ),
    f"{M}evaluation_streams": ("CachedStreams", "load_raw_streams"),
    f"{M}evaluation_identity": (
        "KeySources",
        "changed_key_inputs",
        "file_sha256",
        "key_inputs",
        "sequence_identity",
    ),
    f"{M}evaluation_scorer": (
        "ROWS_ENTRY",
        "SHIFT_ROWS_ENTRY",
        "Component",
        "DriveResult",
        "DriveScorer",
        "DriveTask",
        "SweepSpec",
        "detector_components",
        "score_model_identity",
        "shift_slice",
    ),
    f"{M}evaluation_workers": (
        "DEFAULT_SCORING_THREADS",
        "SCORING_MEMORY_FRACTION",
        "SCORING_WORKER_PEAK_BYTES",
        "ScorerSpec",
        "_cgroup_memory_headroom",
        "_init_scoring_worker",
        "build_worker_scorer",
        "pin_threads",
        "score_drives",
        "scoring_workers",
    ),
    f"{M}evaluation_loading": ("EVALUATION_SESSION_KINDS", "load_resampled_sessions"),
    f"{M}evaluation_heldout": (
        "DETECTORS_OF_ARTIFACT",
        "PER_DETECTOR_SEEN_ROLES",
        "in_sample_detectors",
        "record_seen",
    ),
    f"{M}evaluation_report": ("render_markdown",),
    f"{A}detector_evaluation_drives": ("clean_drive_record", "summarize_events"),
}

#: Names the script defines or owns itself and still lists in ``__all__``.
OWN = {
    "DEFAULT_CACHE_DIR",
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_LEAD_SECONDS",
    "DEFAULT_WINDOW_SECONDS",
    "_evaluate",
    "main",
    "parse_args",
}


def _script() -> object:
    return importlib.import_module("scripts.evaluate_detectors")


def _all_exports() -> list[tuple[str, str, str]]:
    out = []
    for module, names in EXPORTS.items():
        for entry in names:
            script_name, owner_name = (entry, entry) if isinstance(entry, str) else entry
            out.append((module, script_name, owner_name))
    return out


def test_every_exported_name_has_exactly_one_owner() -> None:
    script = _script()
    names = [name for _, name, _ in _all_exports()]
    assert len(names) == len(set(names))
    assert set(names) | OWN == set(script.__all__)  # type: ignore[attr-defined]
    assert not set(names) & OWN


@pytest.mark.parametrize("module, script_name, owner_name", _all_exports())
def test_the_script_exports_the_owners_object(
    module: str, script_name: str, owner_name: str
) -> None:
    script = _script()
    owner = importlib.import_module(module)
    assert getattr(script, script_name) is getattr(owner, owner_name)


#: Re-exports that are an alias of a function owned elsewhere.
ALIASES = {"file_sha256"}


@pytest.mark.parametrize("module, script_name, owner_name", _all_exports())
def test_a_class_or_function_is_defined_once_in_its_owner(
    module: str, script_name: str, owner_name: str
) -> None:
    obj = getattr(_script(), script_name)
    if script_name in ALIASES or not (inspect.isclass(obj) or inspect.isfunction(obj)):
        return
    assert obj.__module__ == module, script_name


# ---------------------------------------------------------------------------
# Phase orchestration, spawn-importable workers, one cache owner.
# ---------------------------------------------------------------------------


def test_evaluate_runs_the_phases_in_order() -> None:
    """``_evaluate`` reads as phase orchestration: each phase a library call, in
    the order the cache-key discipline needs (hash first, load, assemble, score,
    settle the cache, then build the report)."""
    tree = ast.parse(SCRIPT.read_text())
    (evaluate,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_evaluate"]
    calls = sorted(
        (node.lineno, node.col_offset, node.func.id)
        for node in ast.walk(evaluate)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    )
    called = [name for _, _, name in calls]
    order = [
        "request_from_args",
        "prepare_inputs",
        "plan_drives",
        "render_plan",
        "prepare_behavior",
        "load_planned_sessions",
        "ScoreCache",
        "scoring_keys",
        "assemble_detectors",
        "load_fault_channel_roles",
        "sweep_spec",
        "plan_scoring_tasks",
        "score_planned_drives",
        "aggregate_results",
        "settle_score_cache",
        "build_report",
        "compare_to_baseline",
    ]
    positions = [called.index(name) for name in order]
    assert positions == sorted(positions), dict(zip(order, positions, strict=True))
    assert evaluate.end_lineno is not None
    assert evaluate.end_lineno - evaluate.lineno < 120, "_evaluate grew back into a god function"


def test_the_spawn_entry_points_are_top_level_and_importable_by_name() -> None:
    from src.models import evaluation_workers

    for fn in (evaluation_workers._init_scoring_worker, evaluation_workers._score_drive_task):
        assert fn.__module__ == "src.models.evaluation_workers"
        assert fn.__qualname__ == fn.__name__, "a spawn worker cannot pickle a nested function"
        assert pickle.loads(pickle.dumps(fn)) is fn
    # A fresh interpreter (as a spawn worker is) resolves them by qualified name,
    # without ever importing the script.
    payload = pickle.dumps(evaluation_workers._init_scoring_worker)
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pickle, sys; f = pickle.loads(sys.stdin.buffer.read());"
            " print(f.__module__, 'scripts.evaluate_detectors' in sys.modules)",
        ],
        input=payload,
        capture_output=True,
        check=True,
        cwd=REPO,
    )
    assert done.stdout.decode().split() == ["src.models.evaluation_workers", "False"]


def test_the_worker_state_and_its_types_live_beside_the_pool() -> None:
    """What crosses the process boundary is defined where the pool unpickles it."""
    from src.models import evaluation_scorer, evaluation_workers

    assert evaluation_workers.ScorerSpec.__module__ == "src.models.evaluation_workers"
    assert evaluation_scorer.DriveTask.__module__ == "src.models.evaluation_scorer"
    assert evaluation_scorer.DriveResult.__module__ == "src.models.evaluation_scorer"
    assert hasattr(evaluation_workers, "_WORKER_SCORER")


def test_the_score_cache_identity_covers_every_extracted_module() -> None:
    """``code_version`` hashes every ``src/**/*.py``: a moved or edited module
    changes the identity, so cached scores are never served across a move. The
    extracted libraries are all under ``src/``; the CLI files stay the harness
    extras."""
    for name in NEW_MODULES:
        assert _path(name).is_relative_to(REPO / "src"), name
    from scripts import _cli  # noqa: F401

    script = _script()
    assert script._harness_files() == (SCRIPT, SCRIPT.with_name("_cli.py"))  # type: ignore[attr-defined]


def test_code_version_changes_when_a_moved_module_is_edited_renamed_or_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.models import evaluation_cache

    root = tmp_path / "repo"
    (root / "src" / "models").mkdir(parents=True)
    module = root / "src" / "models" / "evaluation_scorer.py"
    module.write_text("x = 1\n")
    monkeypatch.setattr(evaluation_cache, "REPO_ROOT", root)
    base = evaluation_cache.code_version()
    assert evaluation_cache.code_version() == base
    module.write_text("x = 2\n")
    edited = evaluation_cache.code_version()
    assert edited != base
    renamed = module.with_name("evaluation_workers.py")
    module.rename(renamed)
    assert evaluation_cache.code_version() not in {base, edited}
    renamed.rename(module)
    (root / "src" / "models" / "evaluation_extra.py").write_text("y = 1\n")
    assert evaluation_cache.code_version() != edited
    harness = tmp_path / "evaluate_detectors.py"
    harness.write_text("z = 1\n")
    first = evaluation_cache.code_version([harness])
    harness.write_text("z = 2\n")
    assert evaluation_cache.code_version([harness]) != first


# ---------------------------------------------------------------------------
# A library prints nothing, reads no argv, exits nothing, picks no output path.
# ---------------------------------------------------------------------------


def _calls_and_names(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text())
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    dotted = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            dotted.add(f"{node.value.id}.{node.attr}")
    return calls, dotted


@pytest.mark.parametrize("name", NEW_MODULES)
def test_a_library_module_does_not_print_exit_or_read_the_command_line(name: str) -> None:
    calls, dotted = _calls_and_names(_path(name))
    assert "print" not in calls, "report through a return value or EvaluationRefusedError"
    assert not dotted & {"sys.exit", "sys.argv", "sys.stdout", "sys.stderr"}
    assert "argparse" not in _imports(_path(name).read_text(), _package(name))
    assert "exit" not in calls and "quit" not in calls


@pytest.mark.parametrize("name", NEW_MODULES)
def test_a_library_module_writes_no_report_file(name: str) -> None:
    """Report publication stays in the CLI: no library module opens a file for
    writing under a path it chose (the score cache and the stream cache are
    handed their roots)."""
    text = _path(name).read_text()
    assert ".write_text(" not in text
    assert "json.dump(" not in text and "mkdir(" not in text.replace(
        "path.parent.mkdir(parents=True, exist_ok=True)", ""
    )
