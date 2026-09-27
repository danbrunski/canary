"""R01.8 / R09.1: ``src/`` imports run in one direction.

Walks every module under ``src/`` with ``ast`` -- function-level lazy imports
and ``TYPE_CHECKING`` imports included -- and places each top-level package
and each root module (``src/*.py``) in :data:`LAYERS`. Two rules:

- a module imports only from its own layer or a lower one;
- two units in the same layer never import each other. (A pair across layers
  always contains an upward import, which the first rule already reports.)

R01.8 forbade six named edges "rather than imposing a strict layer order".
The pair count rose from 13 to 19 anyway, and the operator replaced that rule
with this order on 2026-09-27 (``docs/roadmap/tasks/R09-package-layering.md``).
The six R01.8 edges are all upward under :data:`LAYERS` and stay pinned in
:func:`test_the_edges_r01_removed_stay_removed`.

Three allowance tables, all checked for staleness so none can outlive the
edge it excuses:

- :data:`OWED` lists the upward imports that existed when the order was
  adopted, each naming the R09 item that removes it. When that item lands the
  edge disappears and this test fails until the entry is deleted.
- :data:`OWED_PAIRS` does the same for same-layer pairs.
- :data:`STANDING` lists the one upward import that stays: the composition
  root serving the app.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

#: Layer of every top-level package and root module under ``src/``. A unit
#: missing from this table fails the test, so a new package must be placed.
LAYERS: dict[str, int] = {
    # 0 foundation: byte -> engineering-unit decoding and the root helpers.
    "decode": 0,
    "artifact_hash": 0,
    "atomic_write": 0,
    "can_ids": 0,
    "coerce": 0,
    "columns": 0,
    "config": 0,
    "dataframe_utils": 0,
    "jsonl_store": 0,
    "log": 0,
    "lsm6dsox_settings": 0,
    "operating": 0,
    "paths": 0,
    "session": 0,
    "telemetry": 0,
    "timefmt": 0,
    "trainability": 0,
    # 1 persistence
    "storage": 1,
    "profiles": 1,
    # 2 acquisition
    "collection": 2,
    # 3 pipeline
    "preprocessing": 3,
    # 4 detection
    "models": 4,
    "alerts": 4,
    "plausibility": 4,
    "datasets": 4,
    # 5 workbench
    "analysis": 5,
    # 6 application
    "control": 6,
    "vehicle_state": 6,
    # 7 runtime
    "daemon": 7,
    "startup": 7,
    # 8 interface
    "dashboard": 8,
    "companion": 8,
    "mcp_server": 8,
    # 9 entry
    "validation": 9,
    "main": 9,
}

#: Upward imports that stay. (file relative to src, imported module).
STANDING: dict[tuple[str, str], str] = {
    ("daemon/server.py", "src.dashboard.api"): "composition root: the daemon serves the app",
}

#: Upward imports present when the order was adopted (2026-09-27). Each value
#: names the R09 item (docs/roadmap/tasks/R09-package-layering.md) that
#: removes the import. (file relative to src, imported module).
#:
#: - R09.2 leaf moves: ``src/columns.py``, ``src/artifact_hash.py``, the dtc
#:   sidecar name, digest budgets, fault injection, storage-writer Protocols,
#:   ``config``'s own validators;
#: - R09.3 ``storage`` owns the session (sessions, manifests, readers);
#: - R09.4 ``src/signals/`` (channel physics out of ``analysis``);
#: - R09.5 ``src/vehicle/`` (profiles, tune revisions, vehicle ledger);
#: - R09.6 ``src/behavior/`` + ``src/features/``, landed together;
#: - R09.7 fusion and feedback into ``models``;
#: - R09.8 daemon-owned objects out of ``dashboard``; Protocols for daemon types.
OWED: dict[tuple[str, str], str] = {
    ("alerts/emitter.py", "src.analysis.committees"): "R09.4",
    ("alerts/events.py", "src.analysis.committees"): "R09.4",
    ("collection/capture_profiles.py", "src.analysis.derived"): "R09.4",
    ("collection/collectors.py", "src.analysis.tune_revisions"): "R09.5",
    ("collection/collectors.py", "src.control.calibration"): "R09.5",
    ("collection/tuning_profile.py", "src.analysis.tune_revisions"): "R09.5",
    ("control/profile_capture.py", "src.daemon.health"): "R09.8",
    ("control/sequence_detector.py", "src.daemon.components"): "R09.8",
    ("daemon/components.py", "src.dashboard.behavior_live"): "R09.8",
    ("daemon/components.py", "src.dashboard.broadcast"): "R09.8",
    ("daemon/detectors/behavior.py", "src.dashboard.behavior_live"): "R09.8",
    ("daemon/detectors/common.py", "src.dashboard.broadcast"): "R09.8",
    ("daemon/detectors/isolation_forest.py", "src.dashboard.broadcast"): "R09.8",
    ("daemon/drain.py", "src.dashboard.broadcast"): "R09.8",
    ("daemon/idle_shutdown.py", "src.dashboard.ups"): "R09.8",
    ("daemon/inference.py", "src.dashboard.broadcast"): "R09.8",
    ("decode/behavior_decoder.py", "src.preprocessing.features"): "R09.6",
    ("decode/behavior_decoder.py", "src.preprocessing.local_range"): "R09.6",
    ("decode/behavior_decoder.py", "src.preprocessing.session_context"): "R09.6",
    ("decode/behavior_eligibility.py", "src.preprocessing.local_range"): "R09.6",
    ("decode/behavior_eligibility.py", "src.preprocessing.session_context"): "R09.6",
    ("models/evaluation.py", "src.analysis.drives"): "R09.4",
    ("models/evaluation.py", "src.analysis.tune_revisions"): "R09.5",
    ("models/isolation_forest.py", "src.analysis.drives"): "R09.4",
    ("models/sequence_autoencoder_training.py", "src.analysis.drives"): "R09.4",
    ("models/sequence_validation_split.py", "src.analysis.drives"): "R09.4",
    ("models/training_entry.py", "src.analysis.drives"): "R09.4",
    ("plausibility/limits.py", "src.analysis.fsm"): "R09.4",
    ("preprocessing/derived.py", "src.analysis.fsm"): "R09.4",
    ("preprocessing/derived_store.py", "src.analysis.fsm"): "R09.4",
    ("preprocessing/feature_pipeline.py", "src.analysis.afr"): "R09.4",
    ("preprocessing/feature_pipeline.py", "src.analysis.committees"): "R09.4",
    ("preprocessing/feature_pipeline.py", "src.analysis.derived"): "R09.4",
    ("preprocessing/feature_pipeline.py", "src.analysis.drives"): "R09.4",
    ("preprocessing/live.py", "src.analysis.derived"): "R09.4",
    ("preprocessing/local_range.py", "src.analysis.pairing"): "R09.4",
    ("preprocessing/quality_gate.py", "src.analysis.fsm"): "R09.4",
    ("preprocessing/session_context.py", "src.analysis.pairing"): "R09.4",
    ("profiles.py", "src.analysis.tune_revisions"): "R09.5",
    ("profiles.py", "src.analysis.vehicle_ledger"): "R09.5",
    ("profiles.py", "src.collection.can_bus"): "R09.5",
    ("profiles.py", "src.collection.can_obd_client"): "R09.5",
    ("profiles.py", "src.collection.obd_registry"): "R09.5",
    ("trainability.py", "src.analysis.committees"): "R09.4",
    ("trainability.py", "src.analysis.derived"): "R09.4",
}

#: Same-layer pairs present when the order was adopted, as sorted unit names.
OWED_PAIRS: dict[tuple[str, str], str] = {
    ("alerts", "models"): "R09.7",
    ("companion", "dashboard"): "R09.8",
}


#: Fields that hold nested statements. Imports are statements, so walking only
#: these reaches every import (function bodies, ``if TYPE_CHECKING:``, ``try``)
#: without visiting the expression nodes that are most of the tree.
_STATEMENT_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        else:
            for field in _STATEMENT_FIELDS:
                stack.extend(getattr(node, field, ()))
    return modules


@cache
def _file_imports(root: Path) -> dict[Path, frozenset[str]]:
    """Every module each source file imports, parsed once per root."""
    return {
        path.relative_to(root): frozenset(
            _imported_modules(ast.parse(path.read_text(), filename=str(path)))
        )
        for path in _source_files(root)
    }


def _source_files(root: Path) -> list[Path]:
    return [
        path for path in sorted(root.rglob("*.py")) if "static" not in path.relative_to(root).parts
    ]


def _unit_of_file(relative: Path) -> str | None:
    """The layer unit a file belongs to: its package, or its own stem at the root."""
    if len(relative.parts) > 1:
        return relative.parts[0]
    return None if relative.stem == "__init__" else relative.stem


def _unit_of_module(module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if parts[0] == "src" and len(parts) > 1 else None


@cache
def _edges(root: Path) -> frozenset[tuple[str, str, str, str]]:
    """Every cross-unit import: (file, importer unit, module, imported unit)."""
    found: set[tuple[str, str, str, str]] = set()
    for relative, modules in _file_imports(root).items():
        importer = _unit_of_file(relative)
        if importer is None:
            continue
        for module in modules:
            target = _unit_of_module(module)
            if target is not None and target != importer:
                found.add((relative.as_posix(), importer, module, target))
    return frozenset(found)


def _units(root: Path) -> set[str]:
    units = {_unit_of_file(relative) for relative in _file_imports(root)}
    return {unit for unit in units if unit is not None}


def _upward(root: Path) -> frozenset[tuple[str, str]]:
    return frozenset(
        (file, module)
        for file, importer, module, target in _edges(root)
        if importer in LAYERS and target in LAYERS and LAYERS[target] > LAYERS[importer]
    )


def _same_layer_pairs(root: Path) -> frozenset[tuple[str, str]]:
    unit_edges = {(importer, target) for _file, importer, _module, target in _edges(root)}
    return frozenset(
        (a, b)
        for a, b in unit_edges
        if a < b and (b, a) in unit_edges and a in LAYERS and b in LAYERS and LAYERS[a] == LAYERS[b]
    )


def test_every_unit_has_a_layer() -> None:
    units = _units(SRC)
    missing = sorted(units - set(LAYERS))
    assert not missing, "place these in LAYERS (tests/test_layering.py): " + ", ".join(missing)
    stale = sorted(set(LAYERS) - units)
    assert not stale, "remove these LAYERS entries, the unit is gone: " + ", ".join(stale)


def test_no_new_upward_imports() -> None:
    unexpected = _upward(SRC) - set(OWED) - set(STANDING)
    assert not unexpected, (
        "import(s) into a higher layer -- move the shared code down to the importer's "
        "layer or lower (see LAYERS): "
        + ", ".join(f"{file} -> {module}" for file, module in sorted(unexpected))
    )


def test_no_new_same_layer_pairs() -> None:
    unexpected = _same_layer_pairs(SRC) - set(OWED_PAIRS)
    assert not unexpected, (
        "units in one layer import each other -- pick a direction and move the shared "
        "code: " + ", ".join(f"{a} <-> {b}" for a, b in sorted(unexpected))
    )


def test_allowances_are_not_stale() -> None:
    """An allowance whose edge is gone must be deleted, so none can linger."""
    upward = _upward(SRC)
    stale = sorted((set(OWED) | set(STANDING)) - upward)
    assert not stale, "remove these allowances, the import is gone: " + ", ".join(
        f"{file} -> {module} ({OWED.get((file, module)) or STANDING[(file, module)]})"
        for file, module in stale
    )
    stale_pairs = sorted(set(OWED_PAIRS) - _same_layer_pairs(SRC))
    assert not stale_pairs, "remove these OWED_PAIRS entries, the pair is gone: " + ", ".join(
        f"{a} <-> {b} ({OWED_PAIRS[(a, b)]})" for a, b in stale_pairs
    )


def test_the_edges_r01_removed_stay_removed() -> None:
    """Pinned explicitly: R01.8's inversions may not return, not even as OWED."""
    edges = {(importer, module) for _file, importer, module, _target in _edges(SRC)}
    assert not any(i == "decode" and m.startswith("src.analysis") for i, m in edges)
    assert not any(i == "control" and m.startswith("src.mcp_server") for i, m in edges)
    assert not any(i == "models" and m == "src.analysis.refit" for i, m in edges)
    assert not any(m == "src.dashboard.paths" for _i, m in edges)
    assert not any(i in {"control", "models"} and m.startswith("src.dashboard") for i, m in edges)


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_scanner_flags_a_lazy_upward_import(tmp_path: Path) -> None:
    """The checks see imports hidden in function bodies, and allow downward ones."""
    _write(tmp_path, "analysis/derived.py", "from src.storage.writer import Writer\n")
    _write(
        tmp_path,
        "storage/writer.py",
        "def flush():\n    from src.analysis.derived import DERIVED\n    return DERIVED\n",
    )
    assert _upward(tmp_path) == {("storage/writer.py", "src.analysis.derived")}


def test_scanner_flags_a_same_layer_pair_through_type_checking(tmp_path: Path) -> None:
    _write(tmp_path, "models/fusion.py", "from src.alerts.emitter import Emitter\n")
    _write(
        tmp_path,
        "alerts/emitter.py",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from src.models.fusion import FusionScorer\n",
    )
    assert _same_layer_pairs(tmp_path) == {("alerts", "models")}
    assert _upward(tmp_path) == frozenset()


def test_src_never_imports_tests() -> None:
    """R07.1: shipped code must not depend on the test tree (or on pytest).

    ``src/validation/synthetic_bus.py`` once imported its scripted ECU from
    ``tests.capture_round_fakes``, which dragged ``pytest`` into
    ``scripts/sustained_validation.py``. Every package under ``src`` is walked.
    """
    offenders = sorted(
        f"{relative.as_posix()} -> {module}"
        for relative, modules in _file_imports(SRC).items()
        for module in modules
        if module == "tests" or module.startswith("tests.") or module == "pytest"
    )
    assert not offenders, "src imports the test tree: " + ", ".join(offenders)


def test_validation_harness_imports_without_pytest() -> None:
    """``import src.validation.harness`` must work where pytest is not installed."""
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'pytest' or name.startswith(('pytest.', '_pytest', 'tests')):\n"
        "            raise ImportError(f'blocked: {name}')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "import src.validation.harness\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=SRC.parent,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
