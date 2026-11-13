"""``src.jsonable``: the one JSON-safe converter (R24.10).

Nine private copies once handled different subsets of numpy, dataclass and
pydantic values, so one number serialized differently across the reports under
``data/analyses/``. These pin the rule and keep a tenth from growing back.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import BaseModel

from src.jsonable import json_default, jsonable
from src.paths import REPO_ROOT


@dataclasses.dataclass
class Inner:
    x: float
    arr: object = None


@dataclasses.dataclass
class Outer:
    name: str
    inner: Inner
    tags: tuple[object, ...] = ()


class Color(enum.Enum):
    RED = "red"


class Level(enum.IntEnum):
    ONE = 1


class Model(BaseModel):
    a: float
    b: list[float]
    when: dt.datetime
    where: Path


def dumps(value: object, **kw: object) -> str:
    return json.dumps(jsonable(value, **kw), sort_keys=True, allow_nan=False)  # type: ignore[arg-type]


def test_plain_values_pass_through() -> None:
    value = {"a": 1, "b": [1.5, "s", None, True], "c": (1, 2)}
    assert dumps(value) == '{"a": 1, "b": [1.5, "s", null, true], "c": [1, 2]}'


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), np.float64("nan")])
def test_non_finite_floats_are_none(bad: float) -> None:
    assert jsonable(bad) is None
    assert jsonable({"v": [bad]}, ndigits=3) == {"v": [None]}


def test_ndigits_rounds_every_float_numpy_included() -> None:
    value = {"f": 1 / 3, "np": np.float64(2 / 3), "f32": np.float32(0.5), "n": 7, "l": [1 / 7]}
    assert jsonable(value, ndigits=6) == {
        "f": 0.333333,
        "np": 0.666667,
        "f32": 0.5,
        "n": 7,
        "l": [0.142857],
    }
    assert jsonable(1 / 3) == 1 / 3  # no rounding unless asked


def test_numpy_scalars_and_arrays() -> None:
    assert jsonable(np.int64(7)) == 7 and type(jsonable(np.int64(7))) is int
    assert jsonable(np.bool_(True)) is True
    assert jsonable(np.array([1.0, np.nan])) == [1.0, None]
    assert jsonable(np.array([[1, 2], [3, 4]])) == [[1, 2], [3, 4]]
    assert jsonable(np.array([1 / 3]), ndigits=2) == [0.33]


def test_bool_is_not_widened_to_an_int() -> None:
    assert jsonable([True, np.bool_(False), 1]) == [True, False, 1]


def test_mapping_keys_are_strings() -> None:
    assert jsonable({1: "a", 2.5: "b", None: "c"}) == {"1": "a", "2.5": "b", "None": "c"}


def test_sets_become_sorted_lists() -> None:
    assert jsonable({3, 1, 2}) == [1, 2, 3]
    assert jsonable(frozenset({"b", "a"})) == ["a", "b"]
    assert jsonable({1, "a"}) == [1, "a"]  # mixed types still deterministic


def test_dataclasses_nest_and_convert_their_numpy() -> None:
    value = Outer("o", Inner(1 / 3, np.float64(2 / 3)), (1, np.int64(2)))
    assert jsonable(value, ndigits=3) == {
        "name": "o",
        "inner": {"x": 0.333, "arr": 0.667},
        "tags": [1, 2],
    }
    assert jsonable(Inner) is Inner  # a class is not an instance


def test_pydantic_is_dumped_in_json_mode_then_converted() -> None:
    model = Model(
        a=1 / 3, b=[0.1, float("nan")], when=dt.datetime(2026, 1, 2, 3, 4, 5), where=Path("/x")
    )
    assert jsonable(model, ndigits=2) == {
        "a": 0.33,
        "b": [0.1, None],
        "when": "2026-01-02T03:04:05",
        "where": "/x",
    }


def test_time_path_enum_and_bytes() -> None:
    assert jsonable(dt.datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"
    assert jsonable(dt.date(2026, 1, 2)) == "2026-01-02"
    assert jsonable(pd.Timestamp("2026-01-02T03:04:05")) == "2026-01-02T03:04:05"
    assert jsonable(dt.timedelta(seconds=90)) == 90.0
    assert jsonable([pd.NaT, pd.NA]) == [None, None]
    assert jsonable(Path("/tmp/x")) == "/tmp/x"
    assert jsonable([Color.RED, Level.ONE]) == ["red", 1]
    assert jsonable(b"\x01\xff") == "01ff"


def test_numpy_time_scalars_are_not_taken_for_integers() -> None:
    """``np.timedelta64`` is an ``np.signedinteger``: ``int()`` of it raised."""
    assert jsonable(np.timedelta64(5, "s")) == 5.0
    assert jsonable(np.timedelta64(1500, "ms"), ndigits=0) == 2.0
    assert jsonable(np.timedelta64("NaT")) is None
    assert jsonable(np.datetime64("2026-01-02T03:04:05")) == "2026-01-02T03:04:05"
    assert jsonable(np.datetime64("2026-01-02")) == "2026-01-02T00:00:00"
    assert jsonable(np.datetime64("NaT")) is None
    text = json.dumps({"x": np.timedelta64(5, "s")}, default=json_default)
    assert json.loads(text) == {"x": 5.0}


def test_pandas_frames_and_series() -> None:
    frame = pd.DataFrame({"a": [1, 2], "b": [0.5, np.nan]})
    assert jsonable(frame) == [{"a": 1, "b": 0.5}, {"a": 2, "b": None}]
    assert jsonable(pd.Series([1.0, np.nan])) == [1.0, None]


def test_unknown_types_are_left_for_json_to_refuse() -> None:
    marker = complex(1, 2)
    assert jsonable(marker) is marker
    with pytest.raises(TypeError):
        json.dumps(jsonable({"v": marker}))


def test_stringify_names_unknown_types_instead() -> None:
    assert jsonable({"v": complex(1, 2)}, stringify=True) == {"v": "(1+2j)"}


def test_json_default_is_the_callback_form() -> None:
    value = {"np": np.int64(3), "when": pd.Timestamp("2026-01-02"), "z": complex(0, 1)}
    text = json.dumps(value, sort_keys=True, default=json_default)
    assert json.loads(text) == {"np": 3, "when": "2026-01-02T00:00:00", "z": "1j"}


def test_json_default_does_not_stringify_numpy_scalars() -> None:
    """``digest`` used to write ``"7"`` and ``"False"`` for these."""
    text = json.dumps({"n": np.int64(7), "f": np.bool_(False)}, default=json_default)
    assert json.loads(text) == {"n": 7, "f": False}


#: Module-level functions that turn values into JSON-safe ones, by any name.
CONVERTER_NAMES = (
    r"_?jsonable|_?json_default|_?json_safe|_?to_jsonable|_?json_clean|_default|_plain"
)
PRIVATE_COPY = re.compile(rf"^(?:async )?def ({CONVERTER_NAMES})\(", re.MULTILINE)
#: Deliberate exceptions, each with its reason (never add one without it).
EXEMPT = {
    # Dataclass-only and strict: keeps NaN as NaN and refuses an unknown type;
    # ``jsonable`` would turn NaN into ``null`` in ``RomEffects.to_dict``.
    ("src/analysis/rom_effects.py", "_plain"),
}


def test_no_private_copy_of_the_converter_grows_back() -> None:
    """A fix every caller must remember is at the wrong layer: one converter."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{match.group(1)}"
        for top in ("src", "tools", "scripts")
        for path in sorted((REPO_ROOT / top).rglob("*.py"))
        if path.name != "jsonable.py"
        for match in PRIVATE_COPY.finditer(path.read_text())
        if (str(path.relative_to(REPO_ROOT)), match.group(1)) not in EXEMPT
    ]
    assert not offenders, f"use src.jsonable instead of a private copy: {offenders}"


def test_a_method_named_jsonable_is_not_a_copy() -> None:
    assert not PRIVATE_COPY.search("class A:\n    def jsonable(self):\n        return 1\n")
    assert PRIVATE_COPY.search("def _jsonable(value):\n    return value\n")


def test_replay_event_diff_loads_the_converter_by_path_not_from_the_tree() -> None:
    """``ab --before-tree`` may predate ``src/jsonable.py``; the tool brings its own."""
    from tools import replay_event_diff

    assert replay_event_diff._convert({"x": np.int64(3), "z": complex(1, 2)}) == {
        "x": 3,
        "z": "(1+2j)",
    }
    assert replay_event_diff._CONVERTER is not jsonable
