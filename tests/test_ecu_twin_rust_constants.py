"""R30.12: the Rust crate's hand-copied constants still equal the Python ones.

``rust/`` is outside every gate (no toolchain in CI or the push gate) and its
``pub const`` values are copied by hand from the Python reference. The shared
JSON vectors catch drift only when someone builds the wheel; this test reads the
``pub const`` lines as text and compares them with the Python attributes, so it
runs everywhere. A constant added to the crate must be mapped here or the
completeness check fails.
"""

from __future__ import annotations

import importlib
import re

import pytest

from src.paths import REPO_ROOT
from src.yaml_load import load_yaml

RUST_SRC = REPO_ROOT / "rust" / "ecu_twin_core" / "src"
CONST = re.compile(r"^pub const (\w+): (?:u8|u16|u32|u64|i64|usize|&str) = (.+);$", re.M)

#: Rust file -> {rust constant: (python module, attribute)}.
MAPPED: dict[str, dict[str, tuple[str, str]]] = {
    "fuel_comp_load.rs": {
        name: ("src.decode.fuel_comp_load", name)
        for name in (
            "AREA_INPUT_MULTIPLIER",
            "AREA_TERM_SHIFT",
            "PURGE_TERM_SHIFT",
            "X_SPLIT",
            "LOW_CURVE_MULTIPLIER",
            "BYTE_SHIFT",
            "AREA_ENTRIES",
            "S16_MIN",
        )
    },
    "target_phi.rs": {
        name: ("src.decode.target_phi_command", name)
        for name in ("GATE_STATE_UNKNOWN", "MAX_FLAG_BIT", "MAX_CELL_TO_WORD_SHIFT")
    },
    "lookup.rs": {
        name: ("src.decode.ecm_lookup", name)
        for name in (
            "FRACTION_BITS",
            "SHIFT12_ENTRIES",
            "SHIFT12_INDEX_SHIFT",
            "SHIFT12_LAST_ENTRY_FROM",
        )
    },
    "uprev.rs": {
        name: ("src.analysis.ecu_state_models", name)
        for name in (
            "UPREV_MAGIC",
            "UPREV_INPUT_MASK",
            "UPREV_HOLD",
            "UPREV_STEP",
            "UPREV_SLOTS",
            "UPREV_COUNTDOWN_SHIFT",
        )
    },
    "lib.rs": {
        "KERNEL_VERSION": ("src.decode.ecm_lookup", "KERNEL_VERSION"),
        "STATE_MODEL_VERSION": ("src.analysis.ecu_state_models", "MODEL_VERSION"),
    },
}


def _rust_consts(filename: str) -> dict[str, str]:
    return dict(CONST.findall((RUST_SRC / filename).read_text()))


def _literal(text: str) -> int | str:
    text = text.strip()
    if text.startswith('"'):
        return text.strip('"')
    negative = text.startswith("-")
    value = int(text.lstrip("-").replace("_", ""), 0)
    return -value if negative else value


def _cases() -> list[tuple[str, str, str, str]]:
    return [
        (filename, name, module, attr)
        for filename, names in MAPPED.items()
        for name, (module, attr) in names.items()
    ]


@pytest.mark.parametrize(("filename", "name", "module", "attr"), _cases())
def test_rust_constant_equals_python(filename: str, name: str, module: str, attr: str) -> None:
    rust = _rust_consts(filename)
    assert name in rust, f"{filename} no longer defines {name}; update MAPPED"
    assert _literal(rust[name]) == getattr(importlib.import_module(module), attr), (
        f"rust/ecu_twin_core/src/{filename}: {name} drifted from {module}.{attr}"
    )


def test_every_pub_const_line_is_parsed() -> None:
    """A constant of a type ``CONST`` does not know cannot slip past the check below."""
    declared = re.compile(r"^\s*pub(\(\w+\))?\s+const\s+[A-Z_]", re.M)
    for path in sorted(RUST_SRC.glob("*.rs")):
        text = path.read_text()
        assert len(declared.findall(text)) == len(CONST.findall(text)), (
            f"{path.name}: a `pub const` line is not parsed by CONST (new type?)"
        )


def test_every_rust_constant_is_compared() -> None:
    """A new ``pub const`` in the crate must be mapped (or listed as Rust-only)."""
    rust_only = {("lib.rs", "CONTRACT_VERSION")}  # checked against the contract YAML below
    for path in sorted(RUST_SRC.glob("*.rs")):
        for name in _rust_consts(path.name):
            if (path.name, name) in rust_only:
                continue
            assert name in MAPPED.get(path.name, {}), (
                f"{path.name}: pub const {name} has no Python counterpart in MAPPED"
            )


def test_rust_contract_version_equals_contract_yaml() -> None:
    contract = load_yaml((REPO_ROOT / "data" / "ecu_twin_contract.yaml").read_text())
    assert _literal(_rust_consts("lib.rs")["CONTRACT_VERSION"]) == contract["contract_version"]
