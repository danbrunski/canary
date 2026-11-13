"""R30.12: ``tests/slow_tests.txt`` still names tests that exist.

``tests/conftest.py`` marks every id in that file ``slow`` by exact node id, so a
renamed or deleted test silently stops being marked and rejoins the one-minute
push gate. Static check (AST, no collection): each id's file exists and defines
the function (inside the named class, when there is one), and the list is
sorted and free of duplicates, as ``scripts/mark_slow_tests.py`` writes it.
Regenerate with ``scripts/mark_slow_tests.py`` after a rename; never hand-edit.
"""

from __future__ import annotations

import ast
from functools import cache

from src.paths import REPO_ROOT

SLOW_FILE = REPO_ROOT / "tests" / "slow_tests.txt"
REGENERATE = "regenerate tests/slow_tests.txt with scripts/mark_slow_tests.py"


def _ids() -> list[str]:
    return [
        line.split()[0]
        for line in SLOW_FILE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


@cache
def _definitions(relpath: str) -> dict[tuple[str, ...], ast.AST]:
    """``("test_x",)`` and ``("TestC", "test_y")`` for one test file."""
    found: dict[tuple[str, ...], ast.AST] = {}

    def walk(body: list[ast.stmt], prefix: tuple[str, ...]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[(*prefix, node.name)] = node
            elif isinstance(node, ast.ClassDef):
                found[(*prefix, node.name)] = node
                walk(node.body, (*prefix, node.name))

    walk(ast.parse((REPO_ROOT / relpath).read_text()).body, ())
    return found


def test_listed_ids_are_sorted_and_unique() -> None:
    ids = _ids()
    assert ids == sorted(ids), f"slow_tests.txt is not sorted; {REGENERATE}"
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"duplicate ids {duplicates}; {REGENERATE}"


def stale_ids(ids: list[str]) -> list[str]:
    """Each id whose file or function (in its class, if named) does not exist."""
    stale: list[str] = []
    for node_id in ids:
        relpath, _, rest = node_id.partition("::")
        if not (REPO_ROOT / relpath).is_file():
            stale.append(f"{node_id}: no such file")
            continue
        qualified = tuple(rest.split("[", 1)[0].split("::"))
        if qualified not in _definitions(relpath):
            stale.append(f"{node_id}: no {'::'.join(qualified)} in {relpath}")
    return stale


def test_every_listed_id_names_an_existing_test() -> None:
    stale = stale_ids(_ids())
    assert not stale, f"{len(stale)} stale ids ({REGENERATE}):\n" + "\n".join(stale[:20])


def test_a_renamed_or_deleted_test_is_reported_stale() -> None:
    here = "tests/test_slow_tests_list.py"
    assert stale_ids([f"{here}::test_a_renamed_or_deleted_test_is_reported_stale"]) == []
    assert len(stale_ids([f"{here}::test_renamed_away", "tests/no_such_file.py::test_x"])) == 2
    assert stale_ids([f"{here}::test_listed_ids_are_sorted_and_unique[param]"]) == []
