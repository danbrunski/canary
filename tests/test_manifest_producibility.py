"""Every tracked model's feature spec must stay producible by the catalog.

D02.5: "a binding retraction is a feature-spec change". The July IF went blind
because 6 of its 88 features were corrected out of ``data/bindings.yaml``
after training, and nothing noticed until a drive read
``missing_feature_rate: 1.0``. This suite makes the next such edit fail CI:
any column of a tracked ``models/**/model.manifest.json`` that no current
source produces fails, unless it is listed — pinned to the model version — in
``tests/fixtures/manifest_unproducible_allowlist.yaml``. The allowlist can
only shrink: an entry that is producible again, gone from the manifest, or
belongs to a retrained model is itself a failure.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.config import CanaryConfig
from src.models.spec_producibility import (
    ProducibleCatalog,
    build_producible_catalog,
    manifest_feature_columns,
    unproducible_feature_columns,
)
from src.profiles import resolve_profile_for_tooling, scope_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"
ALLOWLIST_PATH = REPO_ROOT / "tests" / "fixtures" / "manifest_unproducible_allowlist.yaml"
DIDS_PATH = REPO_ROOT / "data" / "dids.yaml"


def _tracked_manifests() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "models/**/model.manifest.json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(line for line in out.stdout.splitlines() if line.strip())


TRACKED_MANIFESTS = _tracked_manifests()


def _slug(manifest: str) -> str:
    # models/<slug>/<model_type>/model.manifest.json
    return Path(manifest).parts[1]


_CATALOGS: dict[tuple[str, str], ProducibleCatalog] = {}


def _scoped_config(slug: str) -> CanaryConfig:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    return scope_config(config, resolve_profile_for_tooling(config, slug))


def _catalog(slug: str, dids_path: Path = DIDS_PATH) -> ProducibleCatalog:
    key = (slug, str(dids_path))
    if key not in _CATALOGS:
        _CATALOGS[key] = build_producible_catalog(_scoped_config(slug), dids_path=dids_path)
    return _CATALOGS[key]


def _load_allowlist() -> list[dict[str, Any]]:
    doc = yaml.safe_load(ALLOWLIST_PATH.read_text()) or {}
    entries = doc.get("entries") or []
    for entry in entries:
        missing = {"manifest", "model_version", "reason", "columns"} - set(entry)
        assert not missing, f"allowlist entry {entry!r} lacks {sorted(missing)}"
        assert str(entry["reason"]).strip(), "allowlist entries need a reviewed reason"
    return list(entries)


def allowlist_violations(
    manifest: str,
    model_version: str,
    feature_columns: Sequence[str],
    unproducible: Mapping[str, str],
    entries: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Human-readable failures for one manifest against the allowlist."""
    problems: list[str] = []
    mine = [e for e in entries if e["manifest"] == manifest]
    allowed: set[str] = set()
    for entry in mine:
        if entry["model_version"] != model_version:
            problems.append(
                f"stale allowlist entry: {manifest} is now {model_version}, the entry pins "
                f"{entry['model_version']} (retrained? drop the entry)"
            )
            continue
        allowed |= {str(c) for c in entry["columns"]}
    present = set(feature_columns)
    for column in sorted(allowed):
        if column not in present:
            problems.append(f"stale allowlist entry: {column} is not in {manifest}")
        elif column not in unproducible:
            problems.append(f"stale allowlist entry: {column} is producible again ({manifest})")
    for column, reason in unproducible.items():
        if column not in allowed:
            problems.append(f"unproducible: {column} — {reason}")
    return problems


def test_tracked_manifests_exist() -> None:
    # An empty discovery would make every check below vacuous.
    assert TRACKED_MANIFESTS, "no tracked models/**/model.manifest.json found"


@pytest.mark.parametrize("manifest", TRACKED_MANIFESTS)
def test_tracked_manifest_feature_spec_is_producible(manifest: str) -> None:
    doc = json.loads((REPO_ROOT / manifest).read_text())
    columns = manifest_feature_columns(doc)
    unproducible = unproducible_feature_columns(columns, _catalog(_slug(manifest)))
    problems = allowlist_violations(
        manifest, str(doc["model_version"]), columns, unproducible, _load_allowlist()
    )
    assert not problems, (
        f"{manifest} ({doc['model_version']}): a catalog edit made its feature spec "
        "unsatisfiable. Migrate the manifest (tools/migrate_channel_names.py) or "
        "retrain; allowlist only after review.\n  " + "\n  ".join(problems)
    )


def test_allowlist_names_only_tracked_manifests() -> None:
    for entry in _load_allowlist():
        assert entry["manifest"] in TRACKED_MANIFESTS, (
            f"stale allowlist entry: {entry['manifest']} is not a tracked manifest"
        )


def test_catches_the_2026_09_18_did_rename() -> None:
    """A manifest still naming the pre-relabel DID is reported, the new name isn't."""
    catalog = _catalog("nissan-xterra-2008")
    columns = [
        "did:ECM_AIR_CHARGE",
        "did:ECM_AIR_CHARGE__mean",
        "did:ECM_INJECTOR_PW_B1",
        "did:ECM_INJECTOR_PW_B1__mean",
        "did:ECM_INJECTOR_PW_B1__roc",
    ]
    unproducible = unproducible_feature_columns(columns, catalog)
    assert list(unproducible) == [
        "did:ECM_INJECTOR_PW_B1",
        "did:ECM_INJECTOR_PW_B1__mean",
        "did:ECM_INJECTOR_PW_B1__roc",
    ]
    assert "dids.yaml" in unproducible["did:ECM_INJECTOR_PW_B1"]


def test_catches_the_retracted_bindings() -> None:
    catalog = _catalog("nissan-xterra-2008")
    unproducible = unproducible_feature_columns(
        ["decoded:ECM_RPM", "decoded:ECM_CALC_LOAD_B__mean"], catalog
    )
    assert list(unproducible) == ["decoded:ECM_CALC_LOAD_B__mean"]
    assert "bindings.yaml" in unproducible["decoded:ECM_CALC_LOAD_B__mean"]


def test_derived_channel_stranded_by_its_input_is_caught(tmp_path: Path) -> None:
    """Removing a DID strands the derived channel computed from it, transitively."""
    doc = yaml.safe_load(DIDS_PATH.read_text())
    doc["dids"] = [d for d in doc["dids"] if d["name"] != "ECM_AF_SENSOR_V_B2"]
    trimmed = tmp_path / "dids.yaml"
    trimmed.write_text(yaml.safe_dump(doc, sort_keys=False))

    columns = ["derived:AFR_BANK_BALANCE", "derived:AFR_BANK_BALANCE__std", "obd:RPM"]
    assert unproducible_feature_columns(columns, _catalog("nissan-xterra-2008")) == {}
    unproducible = unproducible_feature_columns(
        columns, _catalog("nissan-xterra-2008", dids_path=trimmed)
    )
    assert list(unproducible) == ["derived:AFR_BANK_BALANCE", "derived:AFR_BANK_BALANCE__std"]
    assert "feature transform does not produce" in unproducible["derived:AFR_BANK_BALANCE"]


def test_unmodelled_namespace_is_reported_not_passed() -> None:
    unproducible = unproducible_feature_columns(["imu:ACCEL_X"], _catalog("nissan-xterra-2008"))
    assert "not modelled" in unproducible["imu:ACCEL_X"]


def test_manifest_without_feature_spec_is_refused() -> None:
    with pytest.raises(ValueError, match="feature_columns"):
        manifest_feature_columns({"schema": {"feature_columns": []}})
    with pytest.raises(ValueError, match="feature_columns"):
        manifest_feature_columns({})


def test_allowlist_violations_flags_new_and_stale() -> None:
    entries = [
        {
            "manifest": "m.json",
            "model_version": "v1",
            "reason": "r",
            "columns": ["decoded:GONE", "decoded:BACK", "decoded:DROPPED"],
        }
    ]
    features = ["decoded:GONE", "decoded:BACK", "decoded:NEW"]
    unproducible = {"decoded:GONE": "x", "decoded:NEW": "y"}
    problems = allowlist_violations("m.json", "v1", features, unproducible, entries)
    assert any("unproducible: decoded:NEW" in p for p in problems)
    assert any("decoded:BACK is producible again" in p for p in problems)
    assert any("decoded:DROPPED is not in" in p for p in problems)
    assert not any("decoded:GONE" in p for p in problems)

    retrained = allowlist_violations("m.json", "v2", features, unproducible, entries)
    assert any("pins v1" in p for p in retrained)
    assert any("unproducible: decoded:GONE" in p for p in retrained)


def test_cli_exit_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tools.check_manifest_producibility import main

    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"schema": {"feature_columns": ["obd:RPM", "obd:RPM__mean"]}}))
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({"schema": {"feature_columns": ["did:ECM_INJECTOR_PW_B1"]}}))
    base = ["--config", str(EXAMPLE_YAML), "--profile", "nissan-xterra-2008"]

    assert main([*base, "--manifest", str(ok)]) == 0
    assert main([*base, "--manifest", str(ok), "--manifest", str(stale)]) == 1
    assert "did:ECM_INJECTOR_PW_B1" in capsys.readouterr().out


def test_cli_flags_a_refit_behavior_artifact_and_reports_unknown_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """B03.4: a consumed column pinned to a different artifact fails; no pin is unknown."""
    from src.decode.behavior_models import artifact_fingerprint, load_behavior_models
    from tools.check_manifest_producibility import main

    base = ["--config", str(EXAMPLE_YAML), "--profile", "nissan-xterra-2008"]
    shipped = load_behavior_models(
        REPO_ROOT / "data" / "nissan-xterra-2008" / "behavior_models.yaml"
    )
    # A model that produces its column and permits learned_feature (no B03.2 conflict).
    model = next(m for m in shipped.models if m.name == "air_charge_vs_rpm_throttle_did")
    columns = ["obd:RPM", f"deviation:{model.name}"]

    pinned = tmp_path / "pinned.json"
    pinned.write_text(
        json.dumps(
            {
                "schema": {
                    "feature_columns": columns,
                    "behavior_model_fingerprints": {model.name: artifact_fingerprint(model)},
                }
            }
        )
    )
    assert main([*base, "--manifest", str(pinned)]) == 0
    assert f"ok behavior:{model.name}" in capsys.readouterr().out

    refit = tmp_path / "refit.json"
    refit.write_text(
        json.dumps(
            {
                "schema": {
                    "feature_columns": columns,
                    "behavior_model_fingerprints": {model.name: "0000deadbeef0000"},
                }
            }
        )
    )
    assert main([*base, "--manifest", str(refit)]) == 1
    out = capsys.readouterr().out
    assert "0/2 unproducible" in out and "1 behavior artifact(s) refit since training" in out
    assert f"FAIL behavior:{model.name}" in out

    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"schema": {"feature_columns": columns}}))
    assert main([*base, "--manifest", str(legacy)]) == 0
    out = capsys.readouterr().out
    assert "OK   " in out and f"UNKNOWN behavior:{model.name}" in out

    assert main([*base, "--manifest", str(refit), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report[str(refit)]["behavior_fingerprint_provenance"][model.name]["status"] == "mismatch"
