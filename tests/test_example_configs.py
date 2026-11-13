"""R30.12: the example configs are copies of ``canary.example.yaml`` plus named overrides.

``canary.pi.example.yaml``, ``canary.dev.example.yaml`` and
``canary.sim.example.yaml`` repeat most of the base file's leaf keys (measured
2026-11-13, the Pi one: 87 of its 97; the checklist's earlier "74 of 96" counted
differently). A copied key silently stops following the base when the base's
default changes. So every key a variant shares with the base must equal it,
unless it is on that variant's override list below, and every key on the list
must really differ (so a list cannot outlive its reason). Adding an override
is a one-line edit here, which is the review.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.paths import REPO_ROOT
from src.yaml_load import load_yaml

#: variant -> dotted keys it deliberately sets differently from the base.
OVERRIDES: dict[str, frozenset[str]] = {
    "canary.pi.example.yaml": frozenset(
        {
            "hardware.can.interface",
            "hardware.can.channel",
            "hardware.gps.enabled",
            "hardware.gps.device",
            "hardware.usb_adc.enabled",
            "hardware.usb_adc.port",
            "collection.obd_dids",
            "collection.storage_path",
            "collection.storage_max_mb",
            "models.out_of_process_feature_build",
            "models.isolation_forest.target_flag_rate",
            "models.lstm_autoencoder.enabled",
        }
    ),
    "canary.dev.example.yaml": frozenset(
        {
            "hardware.can.interface",
            "hardware.can.channel",
            "collection.obd_dids",
            "collection.storage_path",
            "collection.storage_max_mb",
            "models.isolation_forest.target_flag_rate",
            "models.lstm_autoencoder.enabled",
        }
    ),
    "canary.sim.example.yaml": frozenset(
        {
            "hardware.elm327_port",
            "hardware.can.interface",
            "hardware.can.channel",
            "collection.obd_source",
            "collection.obd_pids",
            "collection.obd_dids",
            "collection.storage_path",
            "collection.session_retention_days",
            "collection.storage_max_mb",
            "models.isolation_forest.enabled",
            "models.isolation_forest.n_estimators",
            "models.isolation_forest.target_flag_rate",
            "models.lstm_autoencoder.enabled",
        }
    ),
}


def flatten(tree: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Dotted-key leaves; a list or an empty mapping is one leaf."""
    leaves: dict[str, Any] = {}
    for key, value in tree.items():
        if isinstance(value, dict) and value:
            leaves.update(flatten(value, f"{prefix}{key}."))
        else:
            leaves[f"{prefix}{key}"] = value
    return leaves


def _load(name: str) -> dict[str, Any]:
    return flatten(load_yaml((REPO_ROOT / name).read_text()))


def differing_shared_keys(base: dict[str, Any], variant: dict[str, Any]) -> set[str]:
    return {key for key in variant if key in base and variant[key] != base[key]}


@pytest.mark.parametrize("name", sorted(OVERRIDES))
def test_shared_keys_equal_the_base_or_are_named_overrides(name: str) -> None:
    differing = differing_shared_keys(_load("canary.example.yaml"), _load(name))
    assert differing == OVERRIDES[name], (
        f"{name}: keys that differ from canary.example.yaml without being listed: "
        f"{sorted(differing - OVERRIDES[name])}; listed overrides that now equal the base: "
        f"{sorted(OVERRIDES[name] - differing)}"
    )


def test_a_drifted_copy_is_detected() -> None:
    base = {"a.b": 1, "a.c": [1, 2], "d": "x"}
    assert differing_shared_keys(base, {"a.b": 1, "a.c": [1, 2], "e": 9}) == set()
    assert differing_shared_keys(base, {"a.b": 2, "a.c": [1], "e": 9}) == {"a.b", "a.c"}
    assert flatten({"a": {"b": 1, "c": {}}, "d": [1]}) == {"a.b": 1, "a.c": {}, "d": [1]}
