"""Scripts are thin CLIs over ``src`` and import one another never (R06.4).

A sibling import (``import retrain`` from ``scripts/sequence_bakeoff.py``)
needs ``scripts/`` on ``sys.path``, and 21 test files put it there. The same
file then loaded under two module identities (``retrain`` and
``scripts.retrain``) and a test patching one silently missed code bound to the
other. These checks keep that from coming back.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from src.config import CanaryConfig
from src.models import training_entry

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = REPO_ROOT / "tests"
SCRIPT_NAMES = frozenset(p.stem for p in SCRIPTS_DIR.glob("*.py"))
#: The two modules under scripts/ that are helpers, not scripts, and may be
#: imported by one: ``_cli`` (the config-loading preamble) and ``venv_launch``
#: (standard-library only: the launchers import it *before* ``_cli`` can be,
#: to re-launch under ``.venv`` on a machine where nothing is installed).
SHARED_HELPERS = frozenset({"_cli", "venv_launch"})

#: Test files allowed to touch ``sys.path``, and why.
SYS_PATH_ALLOWED = {
    # Loads each script the way `python scripts/x.py` runs it, scoped with
    # monkeypatch.syspath_prepend, and drops the bare modules afterwards.
    "test_script_cli_snapshot.py",
    # This file: the check below names the calls it looks for.
    "test_script_imports.py",
}


def _scripts() -> list[Path]:
    return sorted(SCRIPTS_DIR.glob("*.py"))


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
            if node.module == "scripts":
                found += [(node.lineno, f"scripts.{alias.name}") for alias in node.names]
    return found


@pytest.mark.parametrize("path", _scripts(), ids=lambda p: p.name)
def test_no_script_imports_another_script_except_cli(path: Path) -> None:
    offenders = []
    for line, module in _imported_modules(ast.parse(path.read_text())):
        parts = module.split(".")
        name = parts[1] if parts[0] == "scripts" and len(parts) > 1 else parts[0]
        if (parts[0] == "scripts" or name in SCRIPT_NAMES) and name not in SHARED_HELPERS:
            if module == "scripts":
                continue  # `from scripts import x` is recorded as scripts.x above
            offenders.append(f"{path.name}:{line} imports {module}")
    assert not offenders, offenders


@pytest.mark.parametrize("path", _scripts(), ids=lambda p: p.name)
def test_a_bare_cli_import_falls_back_to_the_package(path: Path) -> None:
    """``from _cli import`` works when run as a file; a test imports the script
    as ``scripts.<name>``, where only ``scripts._cli`` resolves. Every bare
    import sits in a ``try`` whose ``except ImportError`` imports the package."""
    tree = ast.parse(path.read_text())
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        handler_modules = {
            module for handler in node.handlers for _, module in _imported_modules(handler)
        }
        if "scripts._cli" in handler_modules:
            guarded |= {line for stmt in node.body for line, _ in _imported_modules(stmt)}
    bare = [line for line, module in _imported_modules(tree) if module == "_cli"]
    assert all(line in guarded for line in bare), f"{path.name}: unguarded `_cli` import"


@pytest.mark.parametrize("path", sorted(TESTS_DIR.rglob("*.py")), ids=lambda p: p.name)
def test_no_test_edits_sys_path(path: Path) -> None:
    if path.name in SYS_PATH_ALLOWED:
        return
    text = path.read_text()
    for call in ("sys.path.insert", "sys.path.append", "syspath_prepend"):
        assert call not in text, f"{path.name} uses {call}; import scripts as `scripts.<name>`"


@pytest.mark.parametrize("path", _scripts(), ids=lambda p: p.name)
def test_scripts_benchmark_only_through_the_served_rule(path: Path) -> None:
    """R04.1: ``benchmark_held_out`` is the one caller of the FP benchmark,
    because it is the one that applies the configured ``suppress_channels``."""
    assert "benchmark_false_positives" not in path.read_text(), path.name


def test_benchmark_held_out_leaves_the_configured_suppress_list_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``train_sequence.py --evaluate`` and ``sequence_bakeoff.py`` called the
    benchmark without ``suppress_channels`` (R04.1's remainder); both now go
    through ``benchmark_held_out``, which passes the serving config's list."""
    config = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml")
    config.models.lstm_autoencoder.suppress_channels = cast(Any, ["obd:FUEL_LEVEL"])
    seen: dict[str, Any] = {}

    class _Windows:
        def __len__(self) -> int:
            return 1

    monkeypatch.setattr(training_entry, "build_sequence_dataset", lambda *a, **k: _Windows())

    def _benchmark(trained: object, dataset: object, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "report"

    monkeypatch.setattr(training_entry, "benchmark_false_positives", _benchmark)
    trained = SimpleNamespace(
        manifest=SimpleNamespace(scaler=None, schema={"sequence_length": 30}),
        feature_columns=("obd:RPM", "obd:FUEL_LEVEL"),
    )
    report = training_entry.benchmark_held_out(
        training_entry.SEQUENCE_BACKBONES["lstm"],
        trained,
        [Path("held-out")],
        config,
    )
    assert cast(object, report) == "report"
    assert seen["suppress_channels"] == ("obd:FUEL_LEVEL",)


def test_benchmark_held_out_is_none_without_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    config = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml")
    monkeypatch.setattr(training_entry, "build_sequence_dataset", lambda *a, **k: [])
    monkeypatch.setattr(
        training_entry,
        "benchmark_false_positives",
        lambda *a, **k: pytest.fail("no windows: nothing to benchmark"),
    )
    trained = SimpleNamespace(
        manifest=SimpleNamespace(scaler=None, schema={"sequence_length": 30}),
        feature_columns=("obd:RPM",),
    )
    assert (
        training_entry.benchmark_held_out(
            training_entry.SEQUENCE_BACKBONES["tcn"],
            trained,
            [Path("held-out")],
            config,
        )
        is None
    )
