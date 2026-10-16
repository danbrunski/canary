"""R20: behavior validation has input, evaluation, metrics, criteria and evidence owners.

``src/analysis/behavior_validation.py`` was 2,204 lines: the validation
document, profile exclusion, per-read pairing, row grading, events, metrics,
synthetic sensitivity, criteria, provenance and evidence assembly. The owners
are now (all in ``src.analysis``, the layer the module was in)

=================================== =================================================
``behavior_validation_schema``      the document, ``EVALUATOR_VERSION``, file/session identity
``behavior_validation_inputs``      default-profile stamps, per-read axis pairing
``behavior_validation_frame``       ``build_evaluation_frame`` (the report's input)
``behavior_validation_evaluation``  ``RowEvaluation``, ``evaluate_rows``, events, persistence fit
``behavior_validation_metrics``     fold metrics, baselines, envelope, bootstrap, strata
``behavior_validation_sensitivity`` synthetic faults, transient controls
``behavior_validation_criteria``    ``apply_criteria``
``behavior_validation_evidence``    ``ValidationContext``, ``build_evidence``, carry-over
=================================== =================================================

``DEPENDS_ON`` is the dependency map and the rule: a module may import exactly
the family modules listed for it. The scan is AST-based, sees imports in
function bodies, ``try`` blocks and ``if TYPE_CHECKING:``, and resolves relative
imports against the file's package. No owner imports the compatibility facade,
a consumer of the family, ``scripts`` or ``tools``; no library module, script
or tool imports the facade at all.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.test_layering import LAYERS

REPO = Path(__file__).resolve().parents[1]
A = "src.analysis."
FACADE = f"{A}behavior_validation"

SCHEMA = f"{A}behavior_validation_schema"
INPUTS = f"{A}behavior_validation_inputs"
FRAME = f"{A}behavior_validation_frame"
EVALUATION = f"{A}behavior_validation_evaluation"
METRICS = f"{A}behavior_validation_metrics"
SENSITIVITY = f"{A}behavior_validation_sensitivity"
CRITERIA = f"{A}behavior_validation_criteria"
EVIDENCE = f"{A}behavior_validation_evidence"

#: Each owner and the family modules it may import (and nothing else of the family).
DEPENDS_ON: dict[str, frozenset[str]] = {
    SCHEMA: frozenset(),
    INPUTS: frozenset(),
    CRITERIA: frozenset(),
    FRAME: frozenset({INPUTS}),
    EVALUATION: frozenset({SCHEMA, INPUTS}),
    METRICS: frozenset({SCHEMA, EVALUATION}),
    SENSITIVITY: frozenset({EVALUATION, METRICS}),
    EVIDENCE: frozenset({SCHEMA, INPUTS, EVALUATION, METRICS, SENSITIVITY, CRITERIA}),
}

#: The family's consumers: an owner importing one would be a cycle through it.
CONSUMERS = frozenset(
    {
        FACADE,
        f"{A}refit",
        f"{A}timing_lookup",
        f"{A}behavior_inventory",
        f"{A}lambda_delivery",
        f"{A}cold_start_inventory",
    }
)

#: Every name the 2,204-line module defined, by the owner that now defines it.
OWNERS: dict[str, tuple[str, ...]] = {
    SCHEMA: (
        "EVALUATOR_VERSION",
        "VALIDATION_FILENAME",
        "MODE_LABEL_FIX_COMMIT",
        "MODE_LABEL_FIX_AT",
        "mode_labels_predate_fix",
        "_Strict",
        "ProvenanceResolution",
        "RegimeAxis",
        "SteadyStateRegime",
        "ReportRegimes",
        "BehaviorValidationDocument",
        "validation_document_path",
        "load_validation_document",
        "file_identity",
        "file_sha256",
        "decoder_identity",
        "session_content_identity",
    ),
    INPUTS: (
        "PROFILE_EXCLUSION_COLUMN",
        "PROFILE_EXCLUDED",
        "PROFILE_UNVERIFIED",
        "_PROFILE_SCOPED_KINDS",
        "ProfileScope",
        "UnverifiedProfileError",
        "profile_exclusions",
        "stamp_profile_exclusions",
        "profile_refusals",
        "AXIS_PAIRING_COLUMN",
        "AXIS_PAIRING_POLICY_COLUMN",
        "AXIS_SEPARATION_PREFIX",
        "_PAIRABLE_BASES",
        "AxisPairingRefusedError",
        "axis_separation_column",
        "_model_axes",
        "require_axis_pairing_covers",
        "axis_pairing_bounds",
        "read_model_reads",
        "per_read_frame",
        "_pair_session_reads",
        "SessionReads",
        "graded_frame",
    ),
    FRAME: ("evaluation_decoded_signals", "build_evaluation_frame"),
    EVALUATION: (
        "EventPolicy",
        "axis_separation_summary",
        "RowEvaluation",
        "_seconds",
        "evaluate_rows",
        "_verdicts",
        "count_events",
        "event_times",
        "band_excess_event_times",
        "model_event_times",
        "PERSISTENCE_NO_TRAINING_EXCURSION",
        "fit_band_excess_persistence",
    ),
    METRICS: (
        "MIN_BOOTSTRAP_DRIVES",
        "ENVELOPE_MIN_ROWS",
        "BaselineKind",
        "fit_baseline",
        "baseline_predictions",
        "envelope_coverage",
        "_r_squared",
        "_median_or_none",
        "_evaluated_hours",
        "fold_metrics",
        "_drive_bootstrap",
        "qualifying_drive_count",
        "breakdown",
        "breakdown_by_labels",
        "_bin_labels",
        "regime_labels",
    ),
    SENSITIVITY: (
        "synthetic_sensitivity",
        "transient_controls",
        "_first_evaluated_seconds",
        "_first_event_per_window",
        "_windows",
        "_windows_with_event",
    ),
    CRITERIA: ("apply_criteria",),
    EVIDENCE: ("ValidationContext", "build_evidence", "carry_over_flags"),
}

#: R05.13's grouped splits, re-exported by the facade since before R20.
SPLIT_NAMES = (
    "DEFAULT_JOIN_GAP_SECONDS",
    "Drive",
    "DriveSplit",
    "LeakedDriveError",
    "PieceRanges",
    "UnorderedSessionsError",
    "chronological_split",
    "explicit_split",
    "group_drives",
    "parent_session",
    "piece_time_ranges",
    "training_leaks",
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


def _violations(files: Mapping[str, str]) -> list[str]:
    found: set[str] = set()
    for name, text in files.items():
        imported = _imports(text, _package(name))
        for dep in sorted((imported & set(DEPENDS_ON)) - DEPENDS_ON[name] - {name}):
            found.add(f"{name} -> {dep}")
        for dep in sorted(imported & CONSUMERS):
            found.add(f"{name} -> {dep}")
        for module in imported:
            parts = module.split(".")
            if parts[0] in {"scripts", "tools"}:
                found.add(f"{name} -> {parts[0]}")
            elif parts[0] == "src" and len(parts) > 1:
                if LAYERS.get(parts[1], 0) > LAYERS["analysis"]:
                    found.add(f"{name} -> src.{parts[1]}")
    return sorted(found)


def _files() -> dict[str, str]:
    return {name: _path(name).read_text() for name in DEPENDS_ON}


def _facade_importers(roots: tuple[str, ...]) -> list[str]:
    offenders = []
    for root in roots:
        for path in sorted((REPO / root).rglob("*.py")):
            rel = path.relative_to(REPO)
            module = ".".join(rel.with_suffix("").parts)
            if module == FACADE:
                continue
            if FACADE in _imports(path.read_text(), _package(module)):
                offenders.append(str(rel))
    return offenders


def test_every_module_of_the_map_exists_in_the_analysis_layer() -> None:
    for name in DEPENDS_ON:
        assert _path(name).is_file(), name
        assert name.split(".")[1] == "analysis" and "analysis" in LAYERS


def test_every_owner_imports_only_the_modules_the_map_allows() -> None:
    assert _violations(_files()) == []


def test_the_map_has_no_cycle_and_names_only_its_own_modules() -> None:
    done: set[str] = set()

    def visit(name: str, trail: tuple[str, ...]) -> None:
        assert name not in trail, f"cycle: {' -> '.join((*trail, name))}"
        if name in done:
            return
        for dep in DEPENDS_ON[name]:
            visit(dep, (*trail, name))
        done.add(name)

    for name, deps in DEPENDS_ON.items():
        assert deps <= set(DEPENDS_ON), name
        visit(name, ())


def test_no_library_module_script_or_tool_imports_the_facade() -> None:
    """Internal callers import the owner; the facade is for compatible callers
    (tests, notebooks). ``refit``, the evaluation CLI, the inventories and the
    timing lookup were migrated in R20."""
    assert _facade_importers(("src", "scripts", "tools")) == []


def test_evidence_orchestration_is_imported_only_by_its_consumers() -> None:
    """The leaves never reach the orchestration boundary: no family module but
    the facade imports ``behavior_validation_evidence``."""
    for name, text in _files().items():
        if name != EVIDENCE:
            assert EVIDENCE not in _imports(text, _package(name)), name


@pytest.mark.parametrize(
    "label, name, text, expected",
    [
        ("top", SCHEMA, f"from {EVIDENCE} import build_evidence\n", [EVIDENCE]),
        ("function", EVALUATION, f"def f():\n    from {EVIDENCE} import x\n", [EVIDENCE]),
        (
            "type_checking",
            INPUTS,
            f"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import {EVALUATION}\n",
            [EVALUATION],
        ),
        (
            "try",
            CRITERIA,
            f"try:\n    from {METRICS} import x\nexcept ImportError:\n    pass\n",
            [METRICS],
        ),
        (
            "package",
            METRICS,
            "from src.analysis import behavior_validation_sensitivity\n",
            [SENSITIVITY],
        ),
        (
            "relative_module",
            EVALUATION,
            "from .behavior_validation_evidence import x\n",
            [EVIDENCE],
        ),
        ("relative_package", SCHEMA, "from . import behavior_validation_inputs\n", [INPUTS]),
        (
            "relative_function",
            INPUTS,
            "def f():\n    from .behavior_validation_evaluation import x\n",
            [EVALUATION],
        ),
        ("facade", METRICS, f"from {FACADE} import fold_metrics\n", [FACADE]),
        ("facade_relative", EVIDENCE, "from . import behavior_validation\n", [FACADE]),
        ("facade_parent", SENSITIVITY, "from ..analysis.behavior_validation import x\n", [FACADE]),
        (
            "consumer",
            EVIDENCE,
            "def f():\n    from src.analysis.refit import fit_recipe\n",
            [f"{A}refit"],
        ),
        ("script", FRAME, "from scripts.evaluate_behavior_models import evaluate\n", ["scripts"]),
        ("tool", SCHEMA, "def f():\n    import tools.timing_lookup_coverage\n", ["tools"]),
        ("upward", INPUTS, "from src.daemon.app import x\n", ["src.daemon"]),
        ("allowed", EVIDENCE, f"from {CRITERIA} import apply_criteria\n", []),
        ("allowed_relative", METRICS, "from .behavior_validation_evaluation import x\n", []),
        ("lookalike", SCHEMA, "import src.analysis.behavior_validation_schemas_x\n", []),
        (
            "lower_layer",
            EVALUATION,
            "from src.behavior.behavior_eligibility import serving_spec\n",
            [],
        ),
    ],
)
def test_the_scanner_finds_every_form_of_a_forbidden_import(
    label: str, name: str, text: str, expected: list[str]
) -> None:
    assert _violations({name: text}) == [f"{name} -> {dep}" for dep in expected], label


def test_an_evaluation_owner_importing_evidence_is_found() -> None:
    """The cycle the split exists to prevent: evidence builds on evaluation."""
    text = _files()[EVALUATION] + f"\nfrom {EVIDENCE} import build_evidence\n"
    assert _violations({EVALUATION: text}) == [f"{EVALUATION} -> {EVIDENCE}"]


def test_a_library_module_importing_the_facade_is_found(tmp_path: Path) -> None:
    """The facade scan reads real files: a planted importer is reported."""
    root = tmp_path / "src" / "analysis"
    root.mkdir(parents=True)
    (root / "planted.py").write_text("def f():\n    from . import behavior_validation\n")
    found = []
    for path in sorted(root.rglob("*.py")):
        module = ".".join(path.relative_to(tmp_path).with_suffix("").parts)
        if FACADE in _imports(path.read_text(), _package(module)):
            found.append(module)
    assert found == [f"{A}planted"]


# ---------------------------------------------------------------------------
# One definition, one owner; the facade re-exports the owner's object.
# ---------------------------------------------------------------------------


def _defined(module: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(_path(module).read_text()).body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_every_name_has_one_owner() -> None:
    seen: dict[str, str] = {}
    for module, names in OWNERS.items():
        defined = _defined(module)
        assert set(names) == defined, module
        for name in names:
            assert name not in seen, f"{name}: {seen.get(name)} and {module}"
            seen[name] = module


def test_the_facade_defines_nothing_but_its_logger() -> None:
    assert _defined(FACADE) == {"log", "__all__"}


def test_the_facade_exports_the_owners_object() -> None:
    facade = importlib.import_module(FACADE)
    for module, names in OWNERS.items():
        owner = importlib.import_module(module)
        for name in names:
            assert getattr(facade, name) is getattr(owner, name), name
    splits = importlib.import_module(f"{A}splits")
    for name in SPLIT_NAMES:
        assert getattr(facade, name) is getattr(splits, name), name


def test_the_facade_keeps_its_public_list_and_logger() -> None:
    facade = importlib.import_module(FACADE)
    owned = {name for names in OWNERS.values() for name in names}
    assert set(facade.__all__) <= owned | set(SPLIT_NAMES)
    assert len(facade.__all__) == len(set(facade.__all__)) == 58
    assert facade.log.name == FACADE


def test_split_drives_stay_distinct_from_physical_drives() -> None:
    """R05.13/B02: ``splits.Drive`` (a split's unit) is not the trainers'
    ``PhysicalDrive``; the facade re-exports the former only."""
    facade = importlib.import_module(FACADE)
    drives = importlib.import_module("src.preprocessing.drives")
    assert facade.Drive is importlib.import_module(f"{A}splits").Drive
    assert facade.Drive is not drives.PhysicalDrive
    assert not hasattr(facade, "PhysicalDrive")
