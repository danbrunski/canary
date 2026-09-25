"""R01.8: the layering inversions R01 removed must not come back.

Walks every module under ``src/`` with ``ast`` (function-level lazy imports
included) and fails on an import edge from a lower layer into a higher one:

- ``decode`` -> ``analysis``;
- ``daemon`` / ``control`` / ``models`` -> ``dashboard``;
- ``control`` -> ``mcp_server``;
- ``models`` -> ``analysis.refit``.

This forbids edges rather than imposing a strict layer order: ``models`` and
``preprocessing`` importing ``analysis.drives`` / ``derived`` / ``pairing`` is
legitimate today (those are the single sources), and so are the source-policy
imports H01 relies on.

Two allowance tables, both checked for staleness so neither can outlive the
edge it excuses:

- :data:`OWED` lists edges a later roadmap item removes. When that item lands
  the edge disappears and this test fails until the entry is deleted.
- :data:`STANDING` lists existing edges R01.8 did not name. They are recorded
  rather than hidden, so a *new* one still fails.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

#: (importing top-level package, forbidden module prefix).
FORBIDDEN: tuple[tuple[str, str], ...] = (
    ("decode", "src.analysis"),
    ("daemon", "src.dashboard"),
    ("control", "src.dashboard"),
    ("models", "src.dashboard"),
    ("control", "src.mcp_server"),
    ("models", "src.analysis.refit"),
)

_IMPORTERS = frozenset(importer for importer, _target in FORBIDDEN)

#: Temporary: removed by a named later item. (file relative to src, module).
#: Empty since R05.10 moved surface evaluation into ``decode.surface_eval``.
OWED: dict[tuple[str, str], str] = {}

#: Existing daemon -> dashboard edges that R01.8 does not name. Recorded so a
#: new edge of this kind still fails; each one wants a home decision of its own.
STANDING: dict[tuple[str, str], str] = {
    ("daemon/server.py", "src.dashboard.api"): "composition root: the daemon serves the app",
    ("daemon/components.py", "src.dashboard.broadcast"): "live broadcaster owned by dashboard",
    ("daemon/drain.py", "src.dashboard.broadcast"): "live broadcaster owned by dashboard",
    ("daemon/inference.py", "src.dashboard.broadcast"): "live broadcaster owned by dashboard",
    # R03.9 moved these two edges out of daemon/inference.py with the detectors.
    (
        "daemon/detectors/common.py",
        "src.dashboard.broadcast",
    ): "live broadcaster owned by dashboard",
    ("daemon/detectors/isolation_forest.py", "src.dashboard.broadcast"): (
        "live broadcaster owned by dashboard"
    ),
    ("daemon/components.py", "src.dashboard.behavior_live"): "behavior-live state in dashboard",
    ("daemon/detectors/behavior.py", "src.dashboard.behavior_live"): (
        "behavior-live state in dashboard"
    ),
    ("daemon/idle_shutdown.py", "src.dashboard.ups"): "UPS keep-alive reader in dashboard",
}


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


@cache
def _violations() -> frozenset[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC)
        if "static" in relative.parts:
            continue
        package = relative.parts[0]
        if package not in _IMPORTERS:
            continue
        for module in _imported_modules(ast.parse(path.read_text(), filename=str(path))):
            for importer, target in FORBIDDEN:
                if package == importer and (module == target or module.startswith(target + ".")):
                    found.add((relative.as_posix(), module))
    return frozenset(found)


def test_no_new_layering_inversions() -> None:
    unexpected = _violations() - set(OWED) - set(STANDING)
    assert not unexpected, (
        "layering inversion(s) — move the code to the lower layer instead: "
        + ", ".join(f"{file} -> {module}" for file, module in sorted(unexpected))
    )


def test_allowances_are_not_stale() -> None:
    """An allowance whose edge is gone must be deleted, so none can linger."""
    stale = (set(OWED) | set(STANDING)) - _violations()
    assert not stale, "remove these allowances, the edge is gone: " + ", ".join(
        f"{file} -> {module} ({OWED.get((file, module)) or STANDING[(file, module)]})"
        for file, module in sorted(stale)
    )


def test_the_edges_r01_removed_stay_removed() -> None:
    """Pinned explicitly: these were the R01-owned inversions."""
    edges = _violations()
    assert not any(module.startswith("src.mcp_server") for _file, module in edges)
    assert not any(module == "src.analysis.refit" for _file, module in edges)
    assert not any(module == "src.dashboard.paths" for _file, module in edges)


def test_src_never_imports_tests() -> None:
    """R07.1: shipped code must not depend on the test tree (or on pytest).

    ``src/validation/synthetic_bus.py`` once imported its scripted ECU from
    ``tests.capture_round_fakes``, which dragged ``pytest`` into
    ``scripts/sustained_validation.py``. Every package under ``src`` is walked,
    not only the :data:`FORBIDDEN` importers.
    """
    offenders = sorted(
        f"{path.relative_to(SRC).as_posix()} -> {module}"
        for path in SRC.rglob("*.py")
        if "static" not in path.relative_to(SRC).parts
        for module in _imported_modules(ast.parse(path.read_text(), filename=str(path)))
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
