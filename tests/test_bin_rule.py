"""The one bin rule (R05.10, R24.6), and every page that groups samples by it.

Cell ``i`` is ``[edges[i], edges[i+1])`` and the last cell ``[lo, hi]``. Each
caller below is fed a value exactly on every edge: that is where the copies
this replaced disagreed (right-closed ``pd.cut`` put an interior-edge value in
the cell below; ``right=False`` / ``np.digitize`` / ``searchsorted`` dropped
the top edge), so a value on an edge landed in a different cell depending on
which page drew it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis.dyno_inertia import _binned
from src.analysis.fuel_comp_fit import _maf_bins
from src.analysis.tuning import GateReport, _cell_labels, delivery_error_from_gated
from src.analysis.tuning_digest import _bands
from src.analysis.workbench import _cells
from src.signals.bins import bin_index, bin_indices, cell_codes, cell_names

EDGES = (500.0, 1000.0, 2000.0, 4000.0)
#: The cell each edge belongs to: an interior edge starts its cell, the last
#: edge closes the last cell.
EDGE_CELLS = [0, 1, 2, 2]
NAMES = ["[500.0, 1000.0)", "[1000.0, 2000.0)", "[2000.0, 4000.0]"]


# -- the rule itself ---------------------------------------------------------------


def test_every_edge_lands_in_the_cell_it_starts_and_the_last_closes() -> None:
    for mode in ("drop", "clip"):
        assert cell_codes(EDGES, EDGES, out_of_range=mode).tolist() == EDGE_CELLS
    assert bin_indices(np.asarray(EDGES), np.asarray(EDGES)).tolist() == EDGE_CELLS
    assert [bin_index(e, np.asarray(EDGES)) for e in EDGES] == EDGE_CELLS
    # Either side of an interior edge: the cell below, then the cell above.
    assert cell_codes([999.999, 1000.0, 1000.001], EDGES, out_of_range="drop").tolist() == [
        0,
        1,
        1,
    ]


def test_out_of_range_is_the_callers_choice() -> None:
    outside = [499.999, 4000.001, -1e9, 1e9]
    assert cell_codes(outside, EDGES, out_of_range="drop").tolist() == [-1, -1, -1, -1]
    assert cell_codes(outside, EDGES, out_of_range="clip").tolist() == [0, 2, 0, 2]
    # bin_indices is the clip answer, unchanged for every finite value.
    assert bin_indices(np.asarray(outside), np.asarray(EDGES)).tolist() == [0, 2, 0, 2]
    with pytest.raises(TypeError):
        cell_codes(outside, EDGES)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="out_of_range"):
        cell_codes(outside, EDGES, out_of_range="nearest")  # type: ignore[arg-type]


def test_a_non_finite_value_has_no_cell_in_either_mode() -> None:
    odd = [np.nan, np.inf, -np.inf]
    for mode in ("drop", "clip"):
        assert cell_codes(odd, EDGES, out_of_range=mode).tolist() == [-1, -1, -1]
    # bin_indices does not look: np.digitize sends NaN past the last edge, so it
    # clips into the last cell. Its serve-path callers mask non-finite values
    # first (containing_cell_indices, evaluate_2d's extent check); pinned so a
    # change to that is a decision, not an accident.
    assert bin_indices(np.asarray([np.nan]), np.asarray(EDGES)).tolist() == [2]


def test_cell_codes_agrees_with_bin_indices_inside_the_edges() -> None:
    rng = np.random.default_rng(246)
    values = np.concatenate([rng.uniform(EDGES[0], EDGES[-1], 2000), np.asarray(EDGES)])
    expected = bin_indices(values, np.asarray(EDGES))
    assert np.array_equal(cell_codes(values, EDGES, out_of_range="drop"), expected)
    assert np.array_equal(cell_codes(pd.Series(values), list(EDGES), out_of_range="clip"), expected)
    # Integer telemetry (OBD rpm) bins like its float value.
    assert cell_codes(pd.Series([500, 1000, 4000]), EDGES, out_of_range="drop").tolist() == [
        0,
        1,
        2,
    ]


def test_cell_names_say_which_side_is_closed() -> None:
    assert cell_names(EDGES) == NAMES


# -- every caller, at every edge -----------------------------------------------------


def test_tuning_cell_labels_at_every_edge() -> None:
    labels = _cell_labels(pd.Series(EDGES), EDGES)
    assert labels.tolist() == [NAMES[c] for c in EDGE_CELLS]


def test_delivery_bands_at_every_edge() -> None:
    """Was right-closed ``pd.cut``: 500 fell in no band and 1000/2000 in the band below."""
    values = pd.DataFrame(
        {
            "rpm": [*EDGES, 4000.001],
            "error_pct": [1.0, 2.0, 3.0, 4.0, 99.0],
            "commanded_afr": [12.5] * 5,
            "measured_afr": [12.5] * 5,
        }
    )
    result = delivery_error_from_gated(
        values, GateReport(started=5, remaining=5), measured_spec="m", rpm_bands=EDGES
    )
    bands = result.bands.set_index("rpm_band")
    assert bands["n"].to_dict() == {NAMES[0]: 1, NAMES[1]: 1, NAMES[2]: 2}
    assert bands["error_pct"].to_dict() == {NAMES[0]: 1.0, NAMES[1]: 2.0, NAMES[2]: 3.5}


def test_wot_digest_bands_at_every_edge() -> None:
    """Was right-closed ``pd.cut``: the same shape as the delivery bands."""
    rows = _bands(pd.Series([*EDGES, 499.0, np.nan]), EDGES)
    assert rows == [
        {"rpm_band": NAMES[0], "seconds": 1},
        {"rpm_band": NAMES[1], "seconds": 1},
        {"rpm_band": NAMES[2], "seconds": 2},
    ]


def test_workbench_cells_at_every_edge() -> None:
    """Was ``pd.cut(right=False)``: the top edge fell in no cell."""
    airflow = (0.0, 10.0, 20.0)
    frame = pd.DataFrame(
        {
            "rpm": [*EDGES, *EDGES],
            "air": [0.0, 10.0, 20.0, 20.0, 5.0, 5.0, 5.0, 20.000001],
            "v": [1.0] * 8,
        }
    )
    cells = _cells(
        frame,
        rpm="rpm",
        airflow="air",
        values={"mean_v": "v"},
        rpm_edges=EDGES,
        airflow_edges=airflow,
        min_n=1,
    )
    counts = {(c["rpm_index"], c["airflow_index"]): c["n"] for c in cells}
    # The 4000 rpm / 20.000001 g/s row is above the airflow edges: no cell.
    assert counts == {(0, 0): 2, (1, 0): 1, (1, 1): 1, (2, 0): 1, (2, 1): 2}


def test_inertia_bins_at_every_edge() -> None:
    """Was ``np.digitize`` with no clip: the top edge fell in no bin."""
    means, counts = _binned(np.asarray([1.0, 2.0, 3.0, 4.0]), np.asarray(EDGES), np.asarray(EDGES))
    assert counts.tolist() == [1, 1, 2]
    assert means.tolist() == [1.0, 2.0, 3.5]


def test_maf_bins_at_every_edge() -> None:
    """Was ``searchsorted(side='right')``: the top edge fell in no bin."""
    maf = np.asarray([*EDGES, np.nan, 4000.5])
    assert _maf_bins(maf, EDGES).tolist() == [*EDGE_CELLS, -1, -1]


# -- no copy grows back -------------------------------------------------------------

#: Binning calls in ``src/`` that are not the rule. Each one is a copy to
#: migrate, not a precedent: the list only shrinks.
UNMIGRATED: dict[str, int] = {
    # Two MAF-band ``pd.cut(..., right=False)`` (top edge dropped): the open
    # R24.6 sub-item in docs/roadmap/tasks/R24-shared-primitives.md.
    "src/analysis/fuel_plant_eval.py": 2,
}

#: Functions that bin by a rule of their own, by the module that defines them.
#: ``searchsorted`` is not here: it is also the time lookup's primitive.
_OWN_RULES = {"pandas": {"cut", "qcut"}, "numpy": {"digitize"}}
_ALIASES = {"pd": "pandas", "pandas": "pandas", "np": "numpy", "numpy": "numpy"}


def _binning_calls(path: Path) -> int:
    """``pd.cut`` / ``pandas.qcut`` / ``np.digitize`` calls, and names imported
    from those modules (``from numpy import digitize``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.attr in _OWN_RULES.get(_ALIASES.get(node.func.value.id, ""), set())
        ):
            count += 1
        elif isinstance(node, ast.ImportFrom) and node.module in _OWN_RULES:
            count += sum(alias.name in _OWN_RULES[node.module] for alias in node.names)
    return count


def test_the_guard_sees_every_spelling(tmp_path: Path) -> None:
    source = tmp_path / "copy.py"
    source.write_text(
        "import numpy\nimport pandas as pd\nfrom numpy import digitize\n"
        "from pandas import qcut\n"
        "a = pd.cut(x, e)\nb = pandas.qcut(x, 4)\nc = numpy.digitize(x, e)\n"
        "d = np.searchsorted(e, x)\n"
    )
    assert _binning_calls(source) == 5


def test_no_page_bins_with_its_own_rule() -> None:
    found = {
        str(path): n
        for path in sorted(Path("src").rglob("*.py"))
        if path.as_posix() != "src/signals/bins.py" and (n := _binning_calls(path))
    }
    assert found == UNMIGRATED, (
        "bin with src.signals.bins.cell_codes (R24.6), not pd.cut / pd.qcut / np.digitize"
    )
