"""E06.1/E06.2: the contract's one stateful path (UpRev map selection, E05.3's
``src.analysis.ecu_state_models``) as language-neutral vectors.

``tests/fixtures/ecu_twin/state_vectors.json`` is exactly what
``tests/ecu_twin_vectors.py`` builds from the reference; every vector, hand
answer and refusal reproduces; the model version agrees with the contract and
the Rust core; and the module computes from supplied values alone.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.analysis import ecu_state_models
from tests import ecu_twin_vectors as vectors

REPO = Path(__file__).resolve().parents[1]

STATE_OPS = {"uprev_power_on", "uprev_step", "loaded_slot"}


def _state_file() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(vectors.STATE_PATH.read_text())
    return data


def test_the_committed_state_vectors_are_exactly_what_the_reference_builds() -> None:
    assert vectors.STATE_PATH.read_text() == vectors.dumps(vectors.build_state())


def test_the_reference_reproduces_every_state_vector_and_hand_answer() -> None:
    contract = vectors.contract()
    document = _state_file()
    assert document["contract_version"] == contract["contract_version"]
    assert document["state_model_version"] == contract["state_model_version"]
    declared = {op["id"] for op in contract["operations"]}
    pinned = 0
    for vector in document["vectors"]:
        op = vectors.COMPOSED.get(vector["op"], vector["op"])
        assert op in declared, vector["id"]
        result = vectors.run(vector, {}, contract["refusals"])
        assert result == {k: vector[k] for k in ("out", "refuse") if k in vector}, vector["id"]
        for key, value in vector.get("pinned", {}).items():
            assert vectors.pinned_value(vector, key) == value, (vector["id"], key)
            pinned += 1
    ops = {vectors.COMPOSED.get(v["op"], v["op"]) for v in document["vectors"]}
    assert ops == STATE_OPS
    hand = [v for v in document["vectors"] if v["source"].startswith("hand:")]
    assert len(hand) >= 10 and pinned >= 25
    # Missing history is exercised: every unknown-state reason is reached.
    reasons = {
        step.get("reason")
        for v in document["vectors"]
        for step in v.get("out", {}).get("steps", [v.get("out", {})])
    }
    assert {
        "uprev_countdown_unknown",
        "uprev_debounce_unknown",
        "uprev_selector_unknown",
        "uprev_state_unobserved",
        "uprev_not_initialised",
        "uprev_active_map_invalid",
    } <= reasons


def test_the_state_model_version_agrees_with_the_contract_and_the_rust_core() -> None:
    contract = vectors.contract()
    assert contract["state_model_version"] == ecu_state_models.MODEL_VERSION
    lib = (REPO / "rust" / "ecu_twin_core" / "src" / "lib.rs").read_text()
    assert re.search(
        rf"pub const STATE_MODEL_VERSION: u32 = {ecu_state_models.MODEL_VERSION};", lib
    )
    for op in contract["operations"]:
        if op["id"] in STATE_OPS:
            module, attr = op["python"].split(":")
            assert module == "src.analysis.ecu_state_models"
            assert callable(getattr(ecu_state_models, attr))


def test_every_inventoried_state_model_kernel_exists() -> None:
    """The contract's inventory names E05.3/E05.4 kernels still outside it."""
    for entry in vectors.contract()["inventory"]:
        module, _, attr = entry["python"].partition(":")
        if module == "src.analysis.ecu_state_models":
            assert callable(getattr(ecu_state_models, attr)), entry["python"]


def test_the_state_models_compute_from_supplied_values_alone() -> None:
    code = (
        "import sys, json\nimport src.analysis.ecu_state_models\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('src.'))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=REPO,
    )
    loaded = set(json.loads(out.stdout))
    assert loaded <= {"src.analysis", "src.analysis.ecu_state_models", "src.decode",
                      "src.decode.ecm_lookup"}, loaded  # fmt: skip
