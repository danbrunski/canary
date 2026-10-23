"""Every YAML load goes through ``src.yaml_load.load_yaml`` (libyaml's parser).

The pure-Python ``yaml.safe_load`` was over a third of the push gate's profiled
test time on 2026-10-23, so a new call to it is refused here rather than left
to a docstring.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

from src.yaml_load import SAFE_LOADER, load_yaml

REPO = Path(__file__).resolve().parents[1]
SCANNED = ("src", "scripts", "tools", "tests")
#: The loader itself, and this file, which compares it with the pure-Python one.
EXEMPT = frozenset({REPO / "src" / "yaml_load.py", Path(__file__).resolve()})
#: PyYAML's loading entry points, each parsing in pure Python unless told otherwise.
LOADS = frozenset(
    {"safe_load", "safe_load_all", "load", "load_all", "full_load", "full_load_all", "unsafe_load"}
)


def _direct_loads(path: Path) -> list[tuple[int, str]]:
    """``(line, spelling)`` of each PyYAML load in ``path``, in line order."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in LOADS
            and isinstance(node.value, ast.Name)
            and node.value.id == "yaml"
        ):
            found.append((node.lineno, f"yaml.{node.attr}"))
        elif isinstance(node, ast.ImportFrom) and node.module == "yaml":
            found += [
                (node.lineno, f"from yaml import {alias.name}")
                for alias in node.names
                if alias.name in LOADS
            ]
    return sorted(found)


def test_nothing_loads_yaml_but_the_one_loader() -> None:
    offenders = [
        f"{path.relative_to(REPO)}:{line} {spelling}"
        for root in SCANNED
        for path in sorted((REPO / root).rglob("*.py"))
        if path not in EXEMPT and "__pycache__" not in path.parts
        for line, spelling in _direct_loads(path)
    ]
    assert not offenders, "use src.yaml_load.load_yaml:\n  " + "\n  ".join(offenders)


def test_the_scan_sees_each_spelling(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import yaml\nfrom yaml import safe_load\n"
        "a = yaml.safe_load('x')\nb = yaml.load('x', Loader=yaml.CSafeLoader)\n"
        "c = yaml.safe_dump({})\n"
    )
    assert _direct_loads(probe) == [
        (2, "from yaml import safe_load"),
        (3, "yaml.safe_load"),
        (4, "yaml.load"),
    ]


def test_the_loader_is_libyaml_where_pyyaml_has_it() -> None:
    if not yaml.__with_libyaml__:
        pytest.skip("this PyYAML was built without libyaml")
    assert SAFE_LOADER is yaml.CSafeLoader


def test_it_loads_what_the_pure_python_safe_loader_loads() -> None:
    document = """
    when: 2026-08-03T00:00:00Z
    day: 2026-10-23
    hex: 0x1F
    ratio: 1.0e-3
    missing: ~
    flags: [yes, no, on, off]
    base: &base {a: 1, b: [x, y]}
    merged: {<<: *base, c: 3}
    text: |
      two
      lines
    """
    loaded = load_yaml(document)
    assert loaded == yaml.safe_load(document)
    assert loaded["when"] == datetime(2026, 8, 3, tzinfo=UTC)
    assert loaded["day"] == date(2026, 10, 23)
    assert loaded["merged"] == {"a": 1, "b": ["x", "y"], "c": 3}


def test_a_malformed_document_raises_pyyamls_error() -> None:
    with pytest.raises(yaml.YAMLError):
        load_yaml("key: [unclosed")
    with pytest.raises(yaml.YAMLError):
        load_yaml(b"a: 1\n  b: 2\n")
