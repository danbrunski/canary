"""E06.2: the Rust core (through its PyO3 binding) against the Python reference.

Skipped by name unless the optional ``canary_ecu_twin`` extension is
importable: CI and the push gate have no Rust toolchain, and the extension is
never installed by them. To run it, build the wheel and put it on the path::

    maturin build --release -m rust/ecu_twin_py/Cargo.toml -o <dir>
    # unpack <dir>/canary_ecu_twin-*.whl into <site>, then
    PYTHONPATH=<site> .venv/bin/python -m pytest tests/test_ecu_twin_core_rust.py

The vectors (``tests/fixtures/ecu_twin``) are the same immutable files
``cargo test`` runs inside the crate; here the comparison crosses the binding,
and a seeded random differential adds tuples neither file holds. Python parity
is not ECU validation: the hand/capture answers and wrong-table controls in
the vectors are what tie either implementation to the firmware.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from typing import Any

import pytest

from src.analysis import ecu_state_models as sm
from src.decode import ecm_lookup, fuel_comp_load
from src.decode.target_phi_command import GateConstants, target_command
from tests import ecu_twin_vectors as vectors

ext = pytest.importorskip(
    "canary_ecu_twin",
    reason="E06.2 Rust extension canary_ecu_twin is not built (optional; see module docstring)",
)

Json = dict[str, Any]


def _refusal(error: Exception) -> str:
    if isinstance(error, TypeError):
        return "not_whole_count"  # a float where the binding takes an int
    if isinstance(error, OverflowError):
        return "out_of_domain"  # wider than i64
    return str(error).split(":", 1)[0]


def _ext_run(vector: Json, named: Json) -> Json:
    op, i = vector["op"], vector["in"]
    try:
        if op == "search_axis":
            axis = named["axes"][i["axis"]] if "axis" in i else i["breakpoints"]
            out: Json = {"hit": ext.search_axis(axis, i["value"])}
        elif op == "interpolate_q16":
            out = {"value": ext.interpolate_q16(i["a"], i["b"], i["fraction"])}
        elif op == "lookup_2d":
            t = named["tables"][i["table"]]
            out = ext.lookup_2d(
                t["cells"],
                t["row_breakpoints"],
                t["column_breakpoints"],
                i["row_value"],
                i["column_value"],
            )
        elif op == "lookup_curve_q16":
            c = named["curves"][i["curve"]]
            out = {"value": ext.lookup_curve_q16(c["values"], c["breakpoints"], i["value"])}
        elif op == "lookup_shift12_q16":
            out = {"value": ext.lookup_shift12_q16(named["tables16"][i["table16"]], i["value"])}
        elif op == "target_command":
            t, k = named["tables"][i["table"]], named["constants"][i["constants"]]
            table = ext.TargetPhiTable(
                t["cells"],
                t["row_breakpoints"],
                t["column_breakpoints"],
                k["flag_bit"],
                k["load_bits"],
                k["floor_cell"],
                k["threshold_cell"],
                k["hysteresis_cells"],
                k["cell_to_word_shift"],
            )
            out = table.command(i["rpm_raw"], i["load_raw"], i["flag_raw"])
        elif op == "x_from_bfs":
            out = {"value": ext.x_from_bfs(i["bfs"], i["scaler"], i["shift"], i["cap"])}
        elif op in ("load_input_curves", "fuel_comp_load"):
            c = named["load_curves"][i["curves"]]
            curves = ext.LoadCurves(
                c["area_breakpoints"],
                c["area_values"],
                c["fill_low"],
                c["fill_high"],
                c["rpm_multiplier"],
                c["reciprocal_numerator"],
                c["idle_air_cap"],
                c["limp_home_area"],
            )
            if op == "load_input_curves":
                out = {"valid": True}
            else:
                out = curves.reconstruct(
                    i["throttle_counts"],
                    i["rpm_word"],
                    i["idle_air"],
                    i["purge_area"],
                    i["limp_home"],
                )
        elif op == "uprev_power_on":
            out = ext.uprev_power_on(tuple(i["retained"]))
        elif op == "uprev_step":
            out = ext.uprev_step(tuple(i["state"]), i["inputs_2ee0"])
        elif op == "uprev_sequence":
            out = {"steps": ext.uprev_sequence(tuple(i["state"]), i["inputs"])}
        elif op == "loaded_slot":
            out = ext.loaded_slot(i["magic"], i["active_map"])
        else:  # pragma: no cover - a new op must be wired here
            raise AssertionError(f"unwired op {op}")
    except (ValueError, TypeError, OverflowError) as error:
        return {"refuse": _refusal(error)}
    return {"out": out}


def test_the_extension_implements_this_contract_version() -> None:
    contract = vectors.contract()
    assert ext.BACKEND == "rust"
    assert ext.CONTRACT_VERSION == contract["contract_version"]
    assert ext.KERNEL_VERSION == ecm_lookup.KERNEL_VERSION == contract["kernel_version"]
    assert ext.STATE_MODEL_VERSION == sm.MODEL_VERSION == contract["state_model_version"]


@pytest.mark.parametrize("path", [vectors.LOOKUP_PATH, vectors.STATE_PATH], ids=["lookup", "state"])
def test_every_vector_crosses_the_binding_bit_for_bit(path: Any) -> None:
    document = json.loads(path.read_text())
    failures = []
    for vector in document["vectors"]:
        got = _ext_run(vector, document["named"])
        expected = {k: vector[k] for k in ("out", "refuse") if k in vector}
        if got != expected:
            failures.append((vector["id"], vector["source"], got, expected))
        if vector.get("kind") == "control":
            for key, wrong in vector["must_differ"].items():
                assert got["out"][key] != wrong, vector["id"]
    assert failures == [], failures[:5]


# -- seeded random differential -----------------------------------------------------------


def _random_table(rng: random.Random) -> Json:
    rows, columns = rng.randint(1, 16), rng.randint(1, 16)
    cell_max = rng.choice([0xFF, 0x7FFF, 0xFFFF])
    return {
        "cells": [[rng.randint(0, cell_max) for _ in range(columns)] for _ in range(rows)],
        "row_breakpoints": sorted(rng.sample(range(0x10000), rows)),
        "column_breakpoints": sorted(rng.sample(range(0x10000), columns)),
    }


def test_random_tables_and_tuples_agree_with_the_reference() -> None:
    rng = random.Random(0xE0602)
    checked = 0
    for _ in range(200):
        t = _random_table(rng)
        k = {
            "flag_bit": rng.randint(0, 31),
            "load_bits": rng.choice([8, 16]),
            "floor_cell": rng.randint(0, 0xFFFF),
            "threshold_cell": rng.randint(0, 0xFFFF),
            "hysteresis_cells": rng.randint(0, 0xFFFF),
            "cell_to_word_shift": rng.randint(0, 16),
        }
        table = ext.TargetPhiTable(*t.values(), *k.values())
        tuples = [
            (rng.randint(0, 0xFFFF), rng.randint(0, 0xFFFF), rng.randint(0, 0xFFFFFFFF))
            for _ in range(100)
        ]
        words = table.word_batch(tuples)
        for (rpm, load, flag), batched in zip(tuples, words, strict=True):
            reference = target_command(
                t["cells"],
                t["row_breakpoints"],
                t["column_breakpoints"],
                rpm_raw=rpm,
                load_raw=load,
                flag_raw=flag,
                flag_bit=k["flag_bit"],
                load_bits=k["load_bits"],
                gate=GateConstants(k["floor_cell"], k["threshold_cell"], k["hysteresis_cells"]),
                cell_to_word_shift=k["cell_to_word_shift"],
            )
            native = table.command(rpm, load, flag)
            assert native["word"] == reference.word == batched
            assert native["branch"] == reference.branch and native["cell"] == reference.cell
            assert native["reason"] == reference.reason
            assert [tuple(c) for c in native["lookup"]["cells"]] == list(reference.lookup.cells)
            checked += 1
    assert checked == 20_000


def test_random_load_chains_agree_with_the_reference() -> None:
    rng = random.Random(0xE0603)
    named = vectors.named_objects()["load_curves"]
    for name in ("synthetic", "saturating"):
        c = named[name]
        native = ext.LoadCurves(*c.values())
        reference = fuel_comp_load.LoadInputCurves(
            area_breakpoints=tuple(c["area_breakpoints"]),
            area_values=tuple(c["area_values"]),
            fill_low=tuple(c["fill_low"]),
            fill_high=tuple(c["fill_high"]),
            rpm_multiplier=c["rpm_multiplier"],
            reciprocal_numerator=c["reciprocal_numerator"],
            idle_air_cap=c["idle_air_cap"],
            limp_home_area=c["limp_home_area"],
        )
        for _ in range(5000):
            throttle, rpm = rng.randint(0, 0xFFFF), rng.randint(0, 0xFFFF)
            idle, purge = rng.randint(-0x8000, c["idle_air_cap"]), rng.randint(0, 0xFFFF)
            limp = rng.random() < 0.1
            trace = fuel_comp_load.reconstruct(
                throttle_counts=throttle,
                rpm_word=rpm,
                idle_air=idle,
                purge_area=purge,
                curves=reference,
                limp_home=limp,
            )
            assert native.reconstruct(throttle, rpm, idle, purge, limp) == {
                "byte": trace.byte,
                "x": trace.x,
                "area": trace.area,
                "w1124": trace.w1124,
                "w116a": trace.w116a,
            }


def _maybe(rng: random.Random, value: Callable[[], int]) -> int | None:
    return None if rng.random() < 0.15 else value()


def test_random_uprev_cycles_agree_with_the_reference() -> None:
    rng = random.Random(0xE0604)
    for _ in range(20_000):
        state = (
            _maybe(rng, lambda: rng.choice([0, 1, 0x20, 0x21, rng.randint(0, 0xFFFF)])),
            _maybe(rng, lambda: rng.choice([0, 2, 8, 10, rng.randint(0, 0xFF)])),
            _maybe(rng, lambda: rng.randint(0, 0xFF)),
            _maybe(rng, lambda: rng.choice([0xAA5555AA, rng.randint(0, 0xFFFFFFFF)])),
            _maybe(rng, lambda: rng.randint(0, 0xFF)),
            _maybe(rng, lambda: rng.randint(0, 0xFFFFFFFF)),
        )
        inputs = rng.randint(0, 0xFF)
        step = sm.uprev_step(sm.UpRevState(*state), inputs)
        native = ext.uprev_step(state, inputs)
        if step.value is None:
            assert native == {"reason": step.reason}
        else:
            assert native["copied"] == step.value.copied
            assert native["indicator"] == step.value.indicator
            assert tuple(native["state"].values()) == (
                step.value.state.countdown,
                step.value.state.debounce,
                step.value.state.selector,
                step.value.state.magic,
                step.value.state.active_map,
                step.value.state.history,
            )


def test_a_batch_past_the_bound_is_refused() -> None:
    t = vectors.named_objects()["tables"]["map1"]
    table = ext.TargetPhiTable(*t.values(), 0, 8, 128, 130, 1, 7)
    assert table.word_batch([(236, 0, 1)]) == [0x4A80]
    with pytest.raises(ValueError, match="exceeds"):
        table.word_batch([(236, 0, 1)] * (ext.MAX_BATCH + 1))
    assert table.footprint_bytes == 2 * (64 + 8 + 8)
