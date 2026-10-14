"""R13.4: the stored-session chain inside ``src/preprocessing`` runs one way.

``tests/test_layering.py`` places whole packages, so it cannot see inside
``src/preprocessing``. There, ``loader`` selected the reduction, cache and IMU
work (``resample_and_segment``) while ``decode``, ``derived``, ``derived_store``,
``imu_stream``, ``imu_session`` and ``session_decode`` imported reader
functions and constants back from it, some of them inside functions to dodge
the import-time cycle. The chain is now ranked below, and a module may import
only a strictly lower rank:

======  ====================================================================
rank    modules
======  ====================================================================
0       ``session_contracts``, ``imu_decimation``, ``decode`` (leaf and row
        transforms)
1       ``wide_frame``, ``session_reader`` (frame transform, raw reader)
2       ``decoded_reader``, ``imu_session`` (read-then-decode, IMU sidecar)
3       ``imu_stream`` (framed ``value_number`` stream)
4       ``derived`` (bounded reduction)
5       ``session_decode`` (decoded reduction)
6       ``derived_store`` (cache)
7       ``session_pipeline`` (``resample_and_segment``)
8       ``loader`` (compatibility facade; nothing inside imports it)
======  ====================================================================

The scan is AST-based and sees imports inside functions, ``try`` blocks,
classes and ``if TYPE_CHECKING:`` -- the placements that hid the cycle -- and
resolves relative imports and ``from src.preprocessing import <submodule>``.
A boundary test that reads sources from a mapping can be run on a temporary
source fixture, which is how the tests below prove a forbidden edge is found.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

PACKAGE = "src.preprocessing"
PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "preprocessing"

#: Strictly lower ranks only (module docstring).
RANK: dict[str, int] = {
    "session_contracts": 0,
    "imu_decimation": 0,
    "decode": 0,
    "wide_frame": 1,
    "session_reader": 1,
    "decoded_reader": 2,
    "imu_session": 2,
    "imu_stream": 3,
    "derived": 4,
    "session_decode": 5,
    "derived_store": 6,
    "session_pipeline": 7,
    "loader": 8,
}

#: Code the chain calls but that sits outside it (the grid, the segmenter, the
#: quality gate, the IMU rotation maths). It must not import any ranked module,
#: or the chain would depend on it and it on the chain. Other modules of the
#: package (``features``, ``drives``, ``imu_live`` ...) are consumers and may
#: import the chain freely.
SUPPORT = ("resampler", "segmenter", "quality_gate", "imu_frame")

#: The seven modules of the cycle the review found.
NAMED_CYCLE = (
    "loader",
    "decode",
    "derived",
    "derived_store",
    "imu_stream",
    "imu_session",
    "session_decode",
)

#: Modules the reader and the transforms sit below: none of them may import
#: any of these, however deep in the file.
ORCHESTRATION = (
    "loader",
    "session_pipeline",
    "derived_store",
    "session_decode",
    "derived",
    "imu_stream",
    "imu_session",
    "decoded_reader",
)
READER_AND_CONTRACT_MODULES = (
    "session_contracts",
    "imu_decimation",
    "decode",
    "wide_frame",
    "session_reader",
)

#: Statement-bearing fields: walking only these reaches every import.
_STATEMENT_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")


@dataclass(frozen=True)
class Edge:
    source: str
    target: str  # a ``src.preprocessing.<name>`` module, or the package itself
    kind: str  # "top", "function" or "type_checking"
    line: int

    def __str__(self) -> str:
        return f"{_short(self.source)} -> {_short(self.target)} ({self.kind}, line {self.line})"


def _short(module: str) -> str:
    return module.removeprefix(PACKAGE + ".") if module != PACKAGE else PACKAGE


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports(tree: ast.Module) -> list[tuple[ast.stmt, str]]:
    """Every import statement of ``tree`` with where it sits."""
    found: list[tuple[ast.stmt, str]] = []

    def walk(nodes: list[ast.stmt], kind: str) -> None:
        for node in nodes:
            if isinstance(node, ast.Import | ast.ImportFrom):
                found.append((node, kind))
                continue
            inner = kind
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                inner = "function" if kind == "top" else kind
            elif isinstance(node, ast.If) and _is_type_checking(node.test):
                inner = "type_checking"
            for field in _STATEMENT_FIELDS:
                children = getattr(node, field, None)
                if not children:
                    continue
                for child in children:
                    # ``handlers`` hold ExceptHandler nodes, ``cases`` match_case
                    # nodes: both carry a ``body`` of statements.
                    if isinstance(child, ast.stmt):
                        walk([child], inner)
                    else:
                        walk(list(getattr(child, "body", ())), inner)

    walk(tree.body, "top")
    return found


def scan_edges(sources: Mapping[str, str]) -> list[Edge]:
    """Import edges between the modules in ``sources`` (module name -> text).

    A target is another module in ``sources``, or the package ``__init__``
    facade when a name is imported from ``src.preprocessing`` itself and is not
    a submodule (``from src.preprocessing import read_session``).
    """
    modules = set(sources)
    edges: list[Edge] = []
    for source, text in sources.items():
        is_init = source == PACKAGE
        package = source if is_init else source.rpartition(".")[0]
        for node, kind in _imports(ast.parse(text)):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            else:
                base = node.module or ""
                if node.level:
                    parts = package.split(".")
                    parts = parts[: len(parts) - (node.level - 1)]
                    base = ".".join([*parts, *([node.module] if node.module else [])])
                targets = []
                for alias in node.names:
                    submodule = f"{base}.{alias.name}"
                    targets.append(submodule if submodule in modules else base)
            for target in targets:
                if target in modules and target != source:
                    edges.append(Edge(source, target, kind, node.lineno))
    return list(dict.fromkeys(edges))  # one edge per statement, not per imported name


def read_sources(directory: Path = PACKAGE_DIR) -> dict[str, str]:
    sources = {}
    for path in sorted(directory.glob("*.py")):
        name = PACKAGE if path.stem == "__init__" else f"{PACKAGE}.{path.stem}"
        sources[name] = path.read_text()
    return sources


def rank_violations(edges: list[Edge]) -> list[str]:
    """Edges between ranked modules that do not go strictly down, and edges
    from the support modules into the ranked chain."""
    problems = []
    for edge in edges:
        source, target = _short(edge.source), _short(edge.target)
        if source in RANK and target in RANK and RANK[target] >= RANK[source]:
            problems.append(f"{edge}: rank {RANK[source]} imports rank {RANK[target]}")
        elif source in SUPPORT and target in RANK:
            problems.append(f"{edge}: support module imports the chain")
    return problems


def reader_violations(edges: list[Edge]) -> list[str]:
    """Reader, contract and transform modules importing orchestration, the
    compatibility facade or the package facade."""
    forbidden = {f"{PACKAGE}.{name}" for name in ORCHESTRATION} | {PACKAGE}
    return [
        f"{edge}"
        for edge in edges
        if _short(edge.source) in READER_AND_CONTRACT_MODULES and edge.target in forbidden
    ]


def facade_importers(edges: list[Edge]) -> list[str]:
    """Any module but ``loader`` itself importing the ``loader`` facade or the
    package facade."""
    return [
        f"{edge}"
        for edge in edges
        if edge.target in (f"{PACKAGE}.loader", PACKAGE) and edge.source != PACKAGE
    ]


def cycles(edges: list[Edge]) -> list[list[str]]:
    """Strongly connected components of more than one module (Tarjan)."""
    graph: dict[str, set[str]] = {}
    for edge in edges:
        graph.setdefault(edge.source, set()).add(edge.target)
        graph.setdefault(edge.target, set())
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: list[list[str]] = []
    counter = 0

    def visit(node: str) -> None:
        nonlocal counter
        index[node] = low[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(graph[node]):
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(_short(member))
                if member == node:
                    break
            if len(component) > 1:
                found.append(sorted(component))

    for node in sorted(graph):
        if node not in index:
            visit(node)
    return found


# ---------------------------------------------------------------------------
# The real package
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_edges() -> list[Edge]:
    return scan_edges(read_sources())


def test_every_ranked_module_exists_and_every_new_module_is_ranked() -> None:
    names = {_short(name) for name in read_sources()}
    assert set(RANK) <= names, f"ranked but missing: {sorted(set(RANK) - names)}"
    assert set(NAMED_CYCLE) <= set(RANK)
    assert set(SUPPORT) <= names


def test_imports_inside_the_chain_run_strictly_down(real_edges: list[Edge]) -> None:
    assert rank_violations(real_edges) == []


def test_readers_and_contracts_import_no_orchestration_or_facade(
    real_edges: list[Edge],
) -> None:
    assert reader_violations(real_edges) == []


def test_nothing_in_the_package_imports_a_compatibility_facade(real_edges: list[Edge]) -> None:
    assert facade_importers(real_edges) == []


def test_the_named_seven_module_cycle_is_gone(real_edges: list[Edge]) -> None:
    """Static, function-level and ``TYPE_CHECKING`` edges all counted."""
    named = {f"{PACKAGE}.{name}" for name in NAMED_CYCLE}
    touching = [
        component
        for component in cycles(real_edges)
        if named & {f"{PACKAGE}.{member}" for member in component}
    ]
    assert touching == []
    lazy = [
        str(edge)
        for edge in real_edges
        if edge.kind == "function" and edge.source in named and edge.target in named
    ]
    assert lazy == [], "a function-level import among the named modules is the old workaround"


def test_the_package_has_no_other_import_cycle(real_edges: list[Edge]) -> None:
    assert cycles(real_edges) == []


#: Function-level imports that stay inside the ranked chain, and why. Each is
#: for start-up cost, not for a cycle: a lazy import used to hide the cycle,
#: so any new one must be justified here.
LAZY_BY_DESIGN = {
    ("session_pipeline", "derived"): "start-up: loader is on the recording path",
    ("session_pipeline", "derived_store"): "start-up: loader is on the recording path",
    ("session_pipeline", "imu_stream"): "start-up: loader is on the recording path",
}


def test_function_level_imports_in_the_chain_are_the_declared_ones(
    real_edges: list[Edge],
) -> None:
    lazy = {
        (_short(edge.source), _short(edge.target))
        for edge in real_edges
        if edge.kind == "function" and _short(edge.source) in RANK and _short(edge.target) in RANK
    }
    assert lazy == set(LAZY_BY_DESIGN)


def test_importing_the_loader_facade_loads_none_of_the_services() -> None:
    """``src.models.sequence_dataset`` imports ``loader``, so the daemon's boot
    does (``tests/test_cold_start_imports.py``): the reduction, cache and IMU
    services stay off that path until ``resample_and_segment`` runs."""
    services = ("derived", "derived_store", "imu_stream", "imu_session")
    services += ("session_decode", "decoded_reader")
    heavy = sorted(f"{PACKAGE}.{name}" for name in services)
    code = (
        "import sys\n"
        "import src.preprocessing.loader\n"
        f"print(sorted(name for name in {heavy!r} if name in sys.modules))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PACKAGE_DIR.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_the_only_name_based_import_is_the_decode_compat_shim() -> None:
    """``decode.__getattr__`` resolves two moved names with ``import_module``;
    a static import there would put the reader back under ``decode``. Any
    other string-built import of a package module is a hidden edge."""
    dynamic: set[tuple[str, str]] = set()
    for module, text in read_sources().items():
        if module == PACKAGE:  # the lazy facade: names -> owners, by design
            continue
        for node in ast.walk(ast.parse(text)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and str(node.args[0].value).startswith(PACKAGE)
            ):
                dynamic.add((_short(module), _short(str(node.args[0].value))))
    assert dynamic == {("decode", "decoded_reader")}


# ---------------------------------------------------------------------------
# The checks find what they claim to (temporary-source fixtures)
# ---------------------------------------------------------------------------

_FIXTURE_HEADER = {
    f"{PACKAGE}.session_contracts": "X = 1\n",
    f"{PACKAGE}.session_reader": "",
    f"{PACKAGE}.derived": "def reduce():\n    pass\n",
    f"{PACKAGE}.loader": "",
    f"{PACKAGE}.session_pipeline": "",
    PACKAGE: "",
}


def _fixture(**modules: str) -> dict[str, str]:
    return {**_FIXTURE_HEADER, **{f"{PACKAGE}.{name}": text for name, text in modules.items()}}


@pytest.mark.parametrize(
    ("label", "text", "kind"),
    [
        ("top-level", "from src.preprocessing.derived import reduce\n", "top"),
        (
            "function-level",
            "def read():\n    from src.preprocessing.derived import reduce\n",
            "function",
        ),
        (
            "type-only",
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from src.preprocessing import derived\n",
            "type_checking",
        ),
        ("relative", "from .derived import reduce\n", "top"),
        ("relative-package", "from . import derived\n", "top"),
        ("plain import", "import src.preprocessing.derived\n", "top"),
        (
            "nested in try inside a method",
            "class R:\n"
            "    def go(self):\n"
            "        try:\n"
            "            import src.preprocessing.derived as d\n"
            "        except ImportError:\n"
            "            pass\n",
            "function",
        ),
    ],
)
def test_a_forbidden_reader_edge_is_found(label: str, text: str, kind: str) -> None:
    edges = scan_edges(_fixture(session_reader=text))
    found = [
        e for e in edges if _short(e.source) == "session_reader" and _short(e.target) == "derived"
    ]
    assert [e.kind for e in found] == [kind], label
    assert len(reader_violations(edges)) == 1
    assert len(rank_violations(edges)) == 1


def test_a_facade_import_is_found_even_through_the_package() -> None:
    through_loader = scan_edges(
        _fixture(session_reader="from src.preprocessing.loader import read_session\n")
    )
    assert len(facade_importers(through_loader)) == 1
    through_package = scan_edges(
        _fixture(session_reader="from src.preprocessing import read_session\n")
    )
    assert [str(e) for e in through_package] == [
        "session_reader -> src.preprocessing (top, line 1)"
    ]
    assert len(facade_importers(through_package)) == 1
    submodule = scan_edges(_fixture(session_reader="from src.preprocessing import loader\n"))
    assert len(facade_importers(submodule)) == 1


def test_the_old_cycle_shape_is_reported_as_a_cycle() -> None:
    edges = scan_edges(
        _fixture(
            loader="def resample():\n    from src.preprocessing.derived import reduce\n",
            derived="from src.preprocessing.loader import read_session\n",
        )
    )
    assert cycles(edges) == [["derived", "loader"]]
    assert rank_violations(edges)  # derived (4) importing loader (8)


def test_a_clean_chain_has_no_findings() -> None:
    edges = scan_edges(
        _fixture(
            derived="from src.preprocessing.session_reader import read_session\n",
            session_pipeline="from src.preprocessing.derived import reduce\n",
            loader="from src.preprocessing.session_pipeline import resample_and_segment\n",
        )
    )
    assert cycles(edges) == []
    assert rank_violations(edges) == []
    assert reader_violations(edges) == []
    assert facade_importers(edges) == []


# ---------------------------------------------------------------------------
# Compatibility: old public paths resolve to the owners' objects
# ---------------------------------------------------------------------------

#: ``loader`` name -> owning module.
LOADER_REEXPORTS = {
    "INDEX_COLUMNS": "session_contracts",
    "KEY_COLUMNS": "session_contracts",
    "STORED_COLUMNS": "session_contracts",
    "DecodedRows": "session_contracts",
    "TelemetryValueKind": "session_contracts",
    "SESSION_PART_ROWS": "session_reader",
    "discover_sessions": "session_reader",
    "filter_rows": "session_reader",
    "iter_session_numeric_parts": "session_reader",
    "iter_session_parts": "session_reader",
    "read_session": "session_reader",
    "read_sessions": "session_reader",
    "coerce_numeric_columns": "wide_frame",
    "non_numeric_columns": "wide_frame",
    "numeric_value_columns": "wide_frame",
    "to_wide": "wide_frame",
    "resample_and_segment": "session_pipeline",
}


@pytest.mark.parametrize(("name", "owner"), sorted(LOADER_REEXPORTS.items()))
def test_loader_names_are_the_owners_objects(name: str, owner: str) -> None:
    loader = importlib.import_module(f"{PACKAGE}.loader")
    module = importlib.import_module(f"{PACKAGE}.{owner}")
    assert getattr(loader, name) is getattr(module, name)
    assert getattr(module, name) is not None


def test_loader_exports_nothing_the_table_does_not_name() -> None:
    loader = importlib.import_module(f"{PACKAGE}.loader")
    public = {
        name
        for name in vars(loader)
        if not name.startswith("_") and name not in {"annotations", "channel_key"}
    }
    assert public == set(LOADER_REEXPORTS)


def test_decode_still_resolves_the_readers_that_moved() -> None:
    decode = importlib.import_module(f"{PACKAGE}.decode")
    decoded_reader = importlib.import_module(f"{PACKAGE}.decoded_reader")
    for name in ("read_session_decoded", "read_session_decoded_subset"):
        assert getattr(decode, name) is getattr(decoded_reader, name)
    with pytest.raises(AttributeError):
        decode.not_a_decode_name  # noqa: B018


def test_the_package_facade_resolves_to_owners() -> None:
    package = importlib.import_module(PACKAGE)
    session_reader = importlib.import_module(f"{PACKAGE}.session_reader")
    wide_frame = importlib.import_module(f"{PACKAGE}.wide_frame")
    decoded_reader = importlib.import_module(f"{PACKAGE}.decoded_reader")
    assert package.read_session is session_reader.read_session
    assert package.read_sessions is session_reader.read_sessions
    assert package.to_wide is wide_frame.to_wide
    assert package.read_session_decoded is decoded_reader.read_session_decoded
