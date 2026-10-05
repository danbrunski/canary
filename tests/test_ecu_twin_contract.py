"""E06.1: the ECU twin's computation contract is machine-checked, its Python
reference runs from supplied values alone, and both twin adapters use it.

- ``data/ecu_twin_contract.yaml`` validates against a strict schema; every
  operation names a Python reference that resolves and a Rust symbol that
  exists in ``rust/ecu_twin_core``; the versions agree everywhere.
- ``tests/fixtures/ecu_twin/lookup_vectors.json`` is exactly what
  ``tests/ecu_twin_vectors.py`` builds from the reference, every vector
  reproduces, every hand/capture answer is pinned, and every wrong-table /
  wrong-branch control still fails.
- The kernels import nothing but each other and the standard library (a
  fresh interpreter, like the brain-jobs torch check): no session, daemon,
  storage, pandas or pydantic.
- The stored evaluator and the live twin reach the same kernel call with the
  same raw tuple.

The state vectors (UpRev selection, E05.3's models) are checked by
``tests/test_ecu_twin_state_vectors.py``; the Rust parity by
``tests/test_ecu_twin_core_rust.py`` (skipped unless the extension is built)
and ``cargo test`` in ``rust/``.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from src.decode import ecm_lookup
from tests import ecu_twin_vectors as vectors

REPO = Path(__file__).resolve().parents[1]
RUST_SRC = REPO / "rust" / "ecu_twin_core" / "src"

#: The pure kernels: computable from supplied values with no I/O.
KERNEL_MODULES = (
    "src.decode.ecm_lookup",
    "src.decode.target_phi_command",
    "src.decode.fuel_comp_load",
    "src.decode.air_charge",
)

#: Operations whose vectors live in the state file.
STATE_OPS = {"uprev_power_on", "uprev_step", "loaded_slot"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Operation(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    firmware: list[str] = Field(min_length=1)
    python: str = Field(pattern=r"^src(\.[a-z0-9_]+)+:[A-Za-z_][A-Za-z0-9_]*$")
    rust: str = Field(pattern=r"^[a-z0-9_]+(::[A-Za-z0-9_]+)+$")
    inputs: dict[str, Any] = Field(min_length=1)
    outputs: dict[str, Any] = Field(min_length=1)
    state: str
    status: Literal["decoded", "decoded_partial"]
    arithmetic: list[str] = []
    invariants: list[str] = []
    invalid_inputs: list[str] = []
    open: list[str] = []


class StateSpec(_Strict):
    snapshot_version: int = Field(ge=1)
    fields: dict[str, str] = Field(min_length=1)
    init: str
    reset: str
    snapshot: str


class Blocker(_Strict):
    id: str
    affects: list[str] = Field(min_length=1)
    what: str
    needs: str


class InventoryEntry(_Strict):
    """A kernel of E02/E05 that is not (yet) an operation, and why."""

    python: str = Field(pattern=r"^src(\.[a-z0-9_]+)+(:[A-Za-z_][A-Za-z0-9_]*)?$")
    firmware: list[str]
    standing: Literal["decoded", "decoded_partial", "blocked", "composition"]
    why_not_yet: str = Field(min_length=1)


class Contract(_Strict):
    contract_version: int = Field(ge=1)
    kernel_version: int = Field(ge=1)
    state_model_version: int = Field(ge=1)
    program: dict[str, str]
    conventions: dict[str, str]
    refusals: dict[str, str]
    operations: list[Operation] = Field(min_length=1)
    inventory: list[InventoryEntry]
    state: dict[str, StateSpec]
    blockers: list[Blocker]


def _contract() -> Contract:
    return Contract.model_validate(vectors.contract())


def _lookup_file() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(vectors.LOOKUP_PATH.read_text())
    return data


# -- the contract artifact ------------------------------------------------------------


def test_the_contract_validates_and_names_every_state_refusal_and_blocker() -> None:
    contract = _contract()
    ids = [op.id for op in contract.operations]
    assert len(ids) == len(set(ids))
    blockers = {b.id for b in contract.blockers}
    assert len(blockers) == len(contract.blockers)
    for op in contract.operations:
        assert op.state == "none" or op.state in contract.state, op.id
        assert set(op.open) <= blockers, op.id
        # A partially decoded operation must name what is missing.
        assert op.status == "decoded" or op.open, op.id
    # Every refusal kind the Rust core can return is declared, and vice versa.
    rust_kinds = set(re.findall(r'=> "([a-z_]+)",', (RUST_SRC / "lib.rs").read_text()))
    assert rust_kinds == set(contract.refusals)


def test_every_python_reference_resolves_and_every_rust_symbol_exists() -> None:
    for op in _contract().operations:
        module_name, attr = op.python.split(":")
        if module_name == "src.analysis.ecu_state_models":
            continue  # checked by test_ecu_twin_state_vectors.py (E05.3's module)
        assert callable(getattr(importlib.import_module(module_name), attr)), op.python
        *path, name = op.rust.split("::")
        source = (RUST_SRC / f"{path[0]}.rs").read_text()
        if len(path) == 2:  # Type::method
            assert re.search(rf"pub struct {path[1]}\b", source), op.rust
        assert re.search(rf"pub fn {name}\b", source), op.rust


def test_the_versions_agree_across_contract_python_rust_and_vectors() -> None:
    contract = _contract()
    assert contract.kernel_version == ecm_lookup.KERNEL_VERSION
    lib = (RUST_SRC / "lib.rs").read_text()
    for constant, value in (
        ("CONTRACT_VERSION", contract.contract_version),
        ("KERNEL_VERSION", contract.kernel_version),
        ("STATE_MODEL_VERSION", contract.state_model_version),
    ):
        assert re.search(rf"pub const {constant}: u32 = {value};", lib), constant
    document = _lookup_file()
    assert document["format"] == vectors.FORMAT
    assert document["contract_version"] == contract.contract_version
    assert document["kernel_version"] == contract.kernel_version


# -- the vectors ------------------------------------------------------------------------


def test_the_committed_lookup_vectors_are_exactly_what_the_reference_builds() -> None:
    """Regenerate with ``.venv/bin/python -m tests.ecu_twin_vectors`` after a
    reviewed reference change (and a version bump if the arithmetic moved)."""
    assert vectors.LOOKUP_PATH.read_text() == vectors.dumps(vectors.build_lookup())


def test_the_reference_reproduces_every_lookup_vector_and_every_pinned_answer() -> None:
    document = _lookup_file()
    contract = _contract()
    declared = {op.id for op in contract.operations}
    counts = {"vectors": 0, "pinned": 0, "controls": 0, "refusals": 0}
    for vector in document["vectors"]:
        assert vector["op"] in declared, vector["id"]
        result = vectors.run(vector, document["named"], contract.refusals)
        assert result == {k: vector[k] for k in ("out", "refuse") if k in vector}, vector["id"]
        for key, value in vector.get("pinned", {}).items():
            assert vectors.pinned_value(vector, key) == value, (vector["id"], key)
            counts["pinned"] += 1
        if vector.get("kind") == "control":
            counts["controls"] += 1
            for key, wrong in vector["must_differ"].items():
                assert vector["out"][key] != wrong, vector["id"]
        counts["refusals"] += "refuse" in vector
        counts["vectors"] += 1
    covered = {v["op"] for v in document["vectors"]}
    assert covered == declared - STATE_OPS
    assert counts["vectors"] > 1000 and counts["controls"] >= 8 and counts["refusals"] >= 20
    independent = [v for v in document["vectors"] if not v["source"].startswith("generated:")]
    assert len(independent) >= 90 and all("pinned" in v or "must_differ" in v for v in independent)


def test_every_refusal_kind_the_reference_raises_is_reached_by_a_vector() -> None:
    document = _lookup_file()
    reached = {v["refuse"] for v in document["vectors"] if "refuse" in v}
    assert reached == set(_contract().refusals) - {"retained_magic_unknown"}


def test_the_capture_known_answers_and_their_controls_come_from_the_fixture() -> None:
    """The October 4 override words are read from target_phi.yaml, and each
    has its wrong-rpm-decode, wrong-flag-bit and transposed-table control."""
    document = _lookup_file()
    capture = [v for v in document["vectors"] if v["source"].startswith("capture:")]
    plain = [v for v in capture if v.get("kind") != "control"]
    controls = [v for v in capture if v.get("kind") == "control"]
    assert len(plain) == 2 and all(v["out"]["word"] == 0x4A80 for v in plain)
    assert sorted(v["source"].rsplit(":", 1)[1] for v in controls) == sorted(
        ["rpm_at_axis_scale", "flag_bit_1", "table_transposed"] * 2
    )


# -- E06.1: the kernel runs from supplied values alone -----------------------------------


FORBIDDEN_PREFIXES = (
    "pandas",
    "numpy",
    "yaml",
    "pydantic",
    "torch",
    "duckdb",
    "pyarrow",
    "src.analysis",
    "src.storage",
    "src.daemon",
    "src.collection",
    "src.control",
    "src.vehicle",
    "src.config",
    "src.paths",
    "src.signals",
)


def test_the_kernels_import_nothing_but_each_other_and_the_standard_library() -> None:
    """A fresh interpreter (the brain-jobs torch check's shape): importing the
    kernels loads no session, daemon, storage or data-frame module."""
    code = (
        "import sys, json\n"
        + "".join(f"import {name}\n" for name in KERNEL_MODULES)
        + "print(json.dumps(sorted(sys.modules)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=REPO,
    )
    loaded = json.loads(out.stdout)
    offending = [m for m in loaded if m.startswith(FORBIDDEN_PREFIXES)]
    assert offending == [], offending
    src_loaded = {m for m in loaded if m.startswith("src.")}
    assert src_loaded <= {"src.decode", *KERNEL_MODULES}, src_loaded


@pytest.mark.parametrize("module", KERNEL_MODULES)
def test_each_kernel_module_imports_only_kernels_statically(module: str) -> None:
    """The same, statically, so a function-level import cannot hide one."""
    path = REPO / (module.replace(".", "/") + ".py")
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if name.startswith("src"):
                assert name in KERNEL_MODULES, (module, name)
            else:
                assert name.split(".")[0] in sys.stdlib_module_names, (module, name)


def test_the_kernel_computes_a_known_answer_from_plain_values() -> None:
    """No definition, table object or session: lists, ints and the gate."""
    from src.decode.target_phi_command import GateConstants, target_command

    named = vectors.named_objects()["tables"]["map1"]
    result = target_command(
        named["cells"],
        named["row_breakpoints"],
        named["column_breakpoints"],
        rpm_raw=236,
        load_raw=0,
        flag_raw=1,
        flag_bit=0,
        load_bits=8,
        gate=GateConstants(128, 130, 1),
        cell_to_word_shift=7,
    )
    assert (result.branch, result.cell, result.word) == ("override", 149, 0x4A80)


# -- E06.1: the stored and live adapters use the same kernel --------------------------------


def test_the_stored_and_live_twins_reach_the_same_kernel_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.analysis import target_phi
    from src.decode import target_phi_command
    from tests.test_lambda_delivery_live import _records
    from tests.test_target_phi import _evaluate, _session
    from tests.test_twin_live import _drive, _twin

    calls: list[tuple[int, int, int, int | None]] = []
    real = target_phi_command.target_command

    def spy(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        raw = (int(kwargs["rpm_raw"]), int(kwargs["load_raw"]), int(kwargs["flag_raw"]))
        calls.append((*raw, result.word))
        return result

    monkeypatch.setattr(target_phi, "target_command", spy)
    session = _session(flag=0, load=100)
    stored = _evaluate(session)
    stored_calls, calls[:] = list(calls), []
    twin, live = _twin()
    _drive(twin, _records(session))
    live_calls = list(calls)
    assert stored_calls and stored_calls == live_calls
    (record,) = stored["records"]
    assert live == stored["records"]
    assert record["predicted_word"] == stored_calls[-1][3]
