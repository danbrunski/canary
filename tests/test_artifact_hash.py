"""R01.6: one streaming file sha256, with a hash-if-present form. R24.5: one canonical-JSON hash."""

from __future__ import annotations

import ast
import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.artifact_hash import sha256_bytes, sha256_file, sha256_file_or_none, sha256_json
from src.paths import REPO_ROOT


def test_streaming_hash_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    data = bytes(range(256)) * 9000  # spans several 1 MiB chunks' worth of reads
    path.write_bytes(data)

    assert sha256_file(path, chunk_size=4096) == hashlib.sha256(data).hexdigest()
    assert sha256_file_or_none(path) == hashlib.sha256(data).hexdigest()


def test_or_none_for_absent_directory_and_none(tmp_path: Path) -> None:
    assert sha256_file_or_none(None) is None
    assert sha256_file_or_none(tmp_path / "missing") is None
    assert sha256_file_or_none(tmp_path) is None


def test_every_old_name_is_the_one_function() -> None:
    import importlib

    from src.models import alert_budget

    assert alert_budget.file_sha256 is sha256_file
    for module, name in (
        ("scripts.retrain", "_file_sha256"),
        ("scripts.evaluate_detectors", "file_sha256"),
        ("scripts.benchmark_live_pipeline", "_sha256"),
    ):
        assert getattr(importlib.import_module(module), name) is sha256_file_or_none


# ---------------------------------------------------------------------------
# R24.5: canonical-JSON hashing. No stored hash may move.
#
# Each ``_old_*`` below is the body the migrated sites carried, verbatim, in
# one of its four flavors. A site is only equivalent if the shared function
# gives the same digest string for every payload shape the copy accepted.
# ---------------------------------------------------------------------------


def _old_compact_str(payload: Any) -> str:
    """hash_json, records._digest, feature_cache._stable_hash, evaluation_cache.digest, ..."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _old_compact(payload: Any) -> str:
    """rom_edit_spec.sha256, limits.fingerprint, twin_command_change, rom_effects, ..."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _old_plain(payload: Any) -> str:
    """plant_validation.rules_sha256, fuel_plant.sha256, profile_digest, ..."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _old_plain_str(payload: Any) -> str:
    """export.config_hash, derived_store._hash, twin_runner.run_digest."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class _Unit:
    def __str__(self) -> str:
        return "unit-é"


PAYLOADS: list[Any] = [
    {},
    [],
    None,
    "text",
    {"b": 1, "a": {"z": [1, 2, {"y": 2.5, "x": None}], "c": True}},
    {"z": 1, "a": 2, "m": {"q": 1, "b": 2}},
    {"zero": 0.0, "neg_zero": -0.0, "tiny": 1e-7, "huge": 1e22, "sum": 0.1 + 0.2, "den": 5e-324},
    {"nan": math.nan, "inf": math.inf, "ninf": -math.inf},
    {"unit": "µ°é日本語", "key-é": "naïve", "emoji": "\U0001f698"},
    {"tuple": (1, 2, (3, 4)), "list": [1, 2, [3, 4]]},
    {1: "a", 2: "b", 10: "c"},
    {"when": datetime(2026, 11, 13, 1, 2, 3, 456, tzinfo=UTC), "path": Path("/tmp/x/é")},
    {"obj": _Unit(), "nested": [{"obj": _Unit()}]},
    ("parts", {"k": 1}, 2.0, None),
    {"big": 2**70, "neg": -(2**63), "flt": 1.0, "int": 1, "bool": False},
]


def _same_or_both_raise(new: Callable[[], str], old: Callable[[], str]) -> None:
    try:
        expected = old()
    except TypeError:
        with pytest.raises(TypeError):
            new()
        return
    assert new() == expected


@pytest.mark.parametrize("payload", PAYLOADS, ids=range(len(PAYLOADS)))
def test_sha256_json_reproduces_each_flavor(payload: Any) -> None:
    _same_or_both_raise(
        lambda: sha256_json(payload, default=str), lambda: _old_compact_str(payload)
    )
    _same_or_both_raise(lambda: sha256_json(payload), lambda: _old_compact(payload))
    _same_or_both_raise(lambda: sha256_json(payload, compact=False), lambda: _old_plain(payload))
    _same_or_both_raise(
        lambda: sha256_json(payload, compact=False, default=str), lambda: _old_plain_str(payload)
    )


def test_the_flavors_really_differ_and_key_order_does_not() -> None:
    payload = {"a": [1, {"b": 2}], "c": "é"}
    digests = {
        sha256_json(payload),
        sha256_json(payload, compact=False),
    }
    assert len(digests) == 2
    assert sha256_json({"x": 1, "y": 2}) == sha256_json({"y": 2, "x": 1})
    # A datetime only encodes through default=; without it the call must refuse.
    with pytest.raises(TypeError):
        sha256_json({"d": datetime(2026, 1, 1)})
    assert sha256_json({"d": datetime(2026, 1, 1)}, default=str) != sha256_json({"d": "x"})


def test_sha256_bytes_is_hashlib() -> None:
    assert sha256_bytes(b"") == hashlib.sha256(b"").hexdigest()
    assert sha256_bytes("é".encode()) == hashlib.sha256("é".encode()).hexdigest()


# --- the payload-level sites, called for real ---------------------------------


def _payload_sites() -> list[tuple[str, Callable[[Any], str], Callable[[Any], str]]]:
    from src.behavior.behavior_models import fingerprint
    from src.evidence.provenance import hash_json
    from src.evidence.records import _digest as records_digest
    from src.features.feature_cache import _stable_hash
    from src.models.adaptive_candidate import _hash as adaptive_hash
    from src.models.candidate_store import _key as candidate_key
    from src.models.change_detection import reference_fingerprint
    from src.models.evaluation_cache import digest as evaluation_digest
    from src.vehicle.interventions import source_digest

    def stripped(payload: Any, *drop: str) -> Any:
        return (
            {k: v for k, v in payload.items() if k not in drop}
            if isinstance(payload, dict)
            else payload
        )

    return [
        ("hash_json", hash_json, _old_compact_str),
        ("records._digest", records_digest, _old_compact_str),
        ("feature_cache._stable_hash", _stable_hash, _old_compact_str),
        ("candidate_store._key", candidate_key, _old_compact_str),
        ("interventions.source_digest", source_digest, _old_compact_str),
        ("behavior_models.fingerprint", fingerprint, lambda p: "sha256:" + _old_compact_str(p)),
        (
            "adaptive_candidate._hash",
            adaptive_hash,
            lambda p: _old_compact_str(stripped(p, "fingerprint")),
        ),
        (
            "change_detection.reference_fingerprint",
            reference_fingerprint,
            lambda p: _old_compact_str(
                {
                    k: v
                    for k, v in p.items()
                    if k != "fingerprint"
                    and not (k in ("promoted_from", "previous_reference") and v is None)
                }
            ),
        ),
        (
            "evaluation_cache.digest",
            lambda p: evaluation_digest(p, 1, "x"),
            lambda p: _old_compact_str((p, 1, "x")),
        ),
    ]


_DICT_PAYLOADS = [p for p in PAYLOADS if isinstance(p, dict) and all(isinstance(k, str) for k in p)]


@pytest.mark.parametrize("payload", _DICT_PAYLOADS, ids=range(len(_DICT_PAYLOADS)))
def test_each_payload_site_keeps_its_digest(payload: dict[str, Any]) -> None:
    for name, new, old in _payload_sites():
        assert new(payload) == old(payload), name


@pytest.mark.parametrize("payload", [{"kind": "k", "p": {"a": 1, "b": [1, 2]}}, {}, {"é": "ü"}])
def test_job_id_and_derived_store_hash_keep_their_digests(payload: dict[str, Any]) -> None:
    from src.control.brain_jobs_store import job_id_for
    from src.preprocessing.derived_store import _hash

    old = _old_compact({"kind": "refit", "params": payload})
    assert job_id_for("refit", payload) == "bj-" + old[:20]
    assert _hash(payload) == _old_plain_str(payload)
    # A string is hashed as it is, not as JSON.
    assert _hash("raw text é") == hashlib.sha256("raw text é".encode()).hexdigest()


# --- the model-backed sites, against real data and the stored hashes ----------


def _spy(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[tuple[Any, dict[str, Any]]]:
    seen: list[tuple[Any, dict[str, Any]]] = []

    def recording(payload: Any, **kwargs: Any) -> str:
        seen.append((payload, kwargs))
        return sha256_json(payload, **kwargs)

    monkeypatch.setattr(module, "sha256_json", recording)
    return seen


PROFILE = REPO_ROOT / "data" / "nissan-xterra-2008"


def test_edit_spec_and_baseline_and_rules_keep_their_stored_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.analysis import fuel_plant, plant_validation, rom_edit_spec

    seen = _spy(monkeypatch, rom_edit_spec)
    spec = rom_edit_spec.load_edit_spec(
        REPO_ROOT / "tests" / "fixtures" / "rom_edit_spec" / "fuel_comp_light_load.yaml"
    )
    assert spec.sha256() == _old_compact(seen[0][0])

    seen = _spy(monkeypatch, fuel_plant)
    plant, _ = fuel_plant.load_fuel_plant(PROFILE)
    assert plant.baseline is not None
    got = plant.baseline.sha256()
    assert got == _old_plain(seen[0][0])
    # fuel_plant.yaml's "baseline_sha256" ledger and the stored sim designs.
    assert got == "6aec1db556989cc693dcf226a3017478c58a19c6c54abf251d9fe9a916e09220"

    seen = _spy(monkeypatch, plant_validation)
    cases = plant_validation.load_interventions(PROFILE).cases
    assert cases
    for case in cases:
        seen.clear()
        got = case.rules_sha256()
        assert got == _old_plain(seen[0][0])
        assert case.frozen_rules is not None
        assert got == case.frozen_rules.sha256
    # plant_interventions.yaml's frozen rules for the fuel-comp restore case.
    assert cases[0].frozen_rules is not None
    assert cases[0].frozen_rules.sha256 == (
        "f522ff5366b9c2ae903cf0ec0bfdbdd2237f7fae713bf57008dd0dcc79ae6ad9"
    )


def test_profile_digest_on_read_hash_and_config_hash_keep_their_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.collection import capture_profile_model
    from src.config import CanaryConfig
    from src.datasets import export
    from src.decode.bindings import load_bindings
    from src.preprocessing import decode
    from src.yaml_load import load_yaml

    seen = _spy(monkeypatch, capture_profile_model)
    catalog = capture_profile_model.CaptureProfileCatalog.model_validate(
        load_yaml((REPO_ROOT / "data" / "capture_profiles.yaml").read_text())
    )
    assert catalog.profiles
    for profile in catalog.profiles:
        seen.clear()
        assert capture_profile_model.profile_digest(profile) == _old_plain(seen[0][0])

    seen = _spy(monkeypatch, decode)
    bindings = load_bindings(REPO_ROOT / "data" / "bindings.yaml")
    assert decode.on_read_hash(bindings) == _old_plain(seen[0][0])[:16]
    assert decode.on_read_hash(None) == _old_plain([])[:16]

    seen = _spy(monkeypatch, export)
    config = CanaryConfig.from_yaml(REPO_ROOT / "canary.dev.example.yaml")
    assert export.config_hash(config) == _old_plain_str(seen[0][0])


def test_behavior_contract_fingerprints_match_the_stored_ones() -> None:
    from src.behavior import behavior_models as bm
    from src.yaml_load import load_yaml

    path = PROFILE / "behavior_models.yaml"
    models = bm.BehaviorModels.model_validate(load_yaml(path.read_text()))
    with_evidence = [m for m in models.models if m.evidence is not None]
    assert with_evidence
    for model in with_evidence:
        assert model.evidence is not None
        assert bm.contract_fingerprint(model) == model.evidence.contract_fingerprint


# --- every call site keeps the flavor its stored hashes were made with -------

#: C = compact separators, P = plain; ``+str`` = ``default=str``. Changing a
#: site's flavor changes every hash it ever stored (R24.5), so a flavor change
#: is a decision made here, never a side effect of an edit.
FLAVORS: dict[str, list[str]] = {
    "src/analysis/fuel_plant.py": ["P"],
    "src/analysis/operating_context.py": ["P"],
    "src/analysis/paired_scenarios.py": ["P", "P"],
    "src/analysis/plant_validation.py": ["P", "P", "P", "P"],
    "src/analysis/rom_edit_spec.py": ["C"],
    "src/analysis/rom_effects.py": ["C", "C"],
    "src/analysis/sim_contract.py": ["C"],
    "src/analysis/twin_command_change.py": ["C"],
    "src/analysis/twin_runner.py": ["P+str"],
    "src/behavior/behavior_models.py": ["C+str"],
    "src/collection/capture_profile_model.py": ["P"],
    "src/control/brain_jobs_store.py": ["C"],
    "src/datasets/export.py": ["P+str"],
    "src/evidence/provenance.py": ["C+str"],
    "src/evidence/records.py": ["C+str"],
    "src/features/feature_cache.py": ["C+str"],
    "src/models/adaptive_candidate.py": ["C", "C+str"],
    "src/models/candidate_store.py": ["C+str"],
    "src/models/change_detection.py": ["C", "C+str"],
    "src/models/evaluation_cache.py": ["C+str"],
    "src/plausibility/limits.py": ["C"],
    "src/preprocessing/decode.py": ["P"],
    "src/preprocessing/derived_store.py": ["P+str"],
    "src/preprocessing/imu_stream.py": ["P"],
    "src/vehicle/interventions.py": ["C+str"],
}


def _flavors_in_src() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        if path.name == "artifact_hash.py":
            continue
        tree = ast.parse(path.read_text())
        calls = sorted(
            (n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_sha256_json(n)),
            key=lambda n: n.lineno,
        )
        if not calls:
            continue
        flavors = []
        for call in calls:
            kwargs = {k.arg: k.value for k in call.keywords}
            compact = kwargs.get("compact")
            plain = isinstance(compact, ast.Constant) and compact.value is False
            flavors.append(("P" if plain else "C") + ("+str" if "default" in kwargs else ""))
        found[str(path.relative_to(REPO_ROOT))] = flavors
    return found


def _is_sha256_json(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Name) and call.func.id == "sha256_json"


def test_every_call_site_keeps_its_flavor() -> None:
    assert _flavors_in_src() == FLAVORS
