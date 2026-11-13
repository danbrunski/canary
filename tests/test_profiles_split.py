"""``src/vehicle/profiles.py`` became a package (R26.4); nothing may notice.

It was six jobs in one 1,724-line file: schema and registry I/O, edit / delete /
restore, active-profile resolution, path scoping, legacy-calibration migration,
and the vehicle's flash history. The package holds the first five
(``profiles/model.py``, ``edit.py``, ``resolve.py``, ``scope.py``,
``legacy.py``); the flash history is ``src/vehicle/flash_history.py``, beside
``tune_revisions.py``, whose registry it reads.

Four claims, each a test:

* ``scope_config`` returns what it returned before the split. The fixture
  ``fixtures/scope_config_golden.json`` was produced by running
  :func:`scope_snapshot` against the *unsplit* module, for every tracked example
  config and both vehicles of the tracked ``profiles.yaml``.
* Every name that was importable from ``src.vehicle.profiles`` still is, and is
  the owner's object, not a copy.
* The package's own modules import one way (``model`` < ``edit`` < ``legacy`` <
  ``resolve``, ``scope`` above ``model``) and each imports cleanly first, in a
  fresh interpreter.
* Nothing patches a name on the package. After the split a ``monkeypatch.setattr``
  on ``src.vehicle.profiles`` sets the re-export and changes nothing the
  submodules look up, so it would apply and do nothing.
"""

from __future__ import annotations

import ast
import json
import pickle
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import src.vehicle.profiles as profiles
from src.config import CanaryConfig
from src.paths import REPO_ROOT
from src.vehicle.profiles import VehicleProfile, read_registry_file, rescope_like, scope_config

REPO = REPO_ROOT
GOLDEN = Path(__file__).parent / "fixtures" / "scope_config_golden.json"
EXAMPLES = {
    "example": REPO / "canary.example.yaml",
    "pi": REPO / "canary.pi.example.yaml",
    "dev": REPO / "canary.dev.example.yaml",
    "sim": REPO / "canary.sim.example.yaml",
}
#: A profile no registry holds: no year, no VIN, a make and model of its own.
SYNTHETIC = VehicleProfile(slug="synthetic-no-year", vin="", name="", make="Made", model="Up")


# ---------------------------------------------------------------------------
# scope_config, field by field
# ---------------------------------------------------------------------------


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace(str(REPO_ROOT), "<repo>")
    if isinstance(value, dict):
        return {str(key): _norm(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(item) for item in value]
    return value


def _flat(value: Any, prefix: str = "") -> dict[str, Any]:
    """``value`` as ``{"daemon.models_dir": ..., ...}``; a list stays one value."""
    if not isinstance(value, dict):
        return {prefix: value}
    flat: dict[str, Any] = {}
    for key, item in value.items():
        flat.update(_flat(item, f"{prefix}.{key}" if prefix else str(key)))
    return flat


def scope_snapshot(config: CanaryConfig, profile: VehicleProfile) -> dict[str, Any]:
    """Everything ``scope_config(config, profile)`` changes, and the bookkeeping it sets.

    ``changed`` is every dotted field outside the ``vehicle:`` block whose value
    differs between the loaded config and the scoped one (the whole config is
    compared, so a field the scoping touched that nobody listed still shows). The
    ``vehicle:`` block is the profile's own copy, which ``profiles.yaml`` edits
    move (a mass, a VIN): :func:`test_the_vehicle_block_is_the_profiles_own` holds
    it to the property instead of a value. Only the public surface of
    ``scope_config`` is used, so the same function ran against the unsplit module.
    """
    scoped = scope_config(config, profile)
    before = _flat(_norm(config.model_dump(mode="json")))
    after = _flat(_norm(scoped.model_dump(mode="json")))
    changed = {
        key: after[key]
        for key in after
        if before.get(key, object()) != after[key] and not key.startswith("vehicle.")
    }
    try:
        scope_config(scoped, VehicleProfile(slug="another", vin=""))
        refusal = None
    except ValueError as exc:
        refusal = str(exc)
    return {
        "changed": changed,
        "removed": sorted(set(before) - set(after)),
        "scoped_profile": scoped.scoped_profile,
        "scope_source_is_the_profile": scoped._scope_source is profile,
        "same_profile_is_idempotent": scope_config(scoped, profile) is scoped,
        "rescope_refusal": refusal,
        "rescope_like_unscoped_reference": rescope_like(config, config) is config,
        "rescope_like_scoped_reference": _norm(rescope_like(config, scoped).model_dump(mode="json"))
        == _norm(scoped.model_dump(mode="json")),
    }


def all_snapshots() -> dict[str, Any]:
    tracked = read_registry_file(REPO / "profiles.yaml")
    assert {p.slug for p in tracked} >= {"nissan-xterra-2008", "nissan-xterra-2013"}
    out: dict[str, Any] = {}
    for config_name, path in EXAMPLES.items():
        config = CanaryConfig.from_yaml(path)
        for profile in (*tracked, SYNTHETIC):
            out[f"{config_name}/{profile.slug}"] = scope_snapshot(config, profile)
    return out


def test_scope_config_returns_what_it_returned_before_the_split() -> None:
    golden = json.loads(GOLDEN.read_text())
    now = all_snapshots()
    assert sorted(now) == sorted(golden)
    for key in golden:
        assert now[key] == golden[key], key


def test_the_vehicle_block_is_the_profiles_own() -> None:
    """``scope_config`` copies make, model, year (0 when unset), VIN and parameters in."""
    tracked = read_registry_file(REPO / "profiles.yaml")
    for path in EXAMPLES.values():
        config = CanaryConfig.from_yaml(path)
        for profile in (*tracked, SYNTHETIC):
            vehicle = scope_config(config, profile).vehicle
            assert (vehicle.make, vehicle.model, vehicle.vin) == (
                profile.make,
                profile.model,
                profile.vin,
            )
            assert vehicle.year == (profile.year if profile.year is not None else 0)
            assert vehicle.parameters == profile.parameters
    vehicle = scope_config(CanaryConfig.from_yaml(EXAMPLES["pi"]), SYNTHETIC).vehicle
    assert (vehicle.make, vehicle.model, vehicle.year, vehicle.vin) == ("Made", "Up", 0, "")


def test_the_golden_pins_the_paths_that_matter() -> None:
    """A golden that recorded nothing would pass anything: the Xterra's paths are in it."""
    golden = json.loads(GOLDEN.read_text())["pi/nissan-xterra-2008"]
    changed = golden["changed"]
    assert golden["scoped_profile"] == "nissan-xterra-2008"
    assert golden["scope_source_is_the_profile"] is True
    assert changed["collection.storage_path"].endswith("/nissan-xterra-2008")
    for name in profiles.SCOPED_DAEMON_DIRS:
        assert changed[f"daemon.{name}"].endswith("/nissan-xterra-2008"), name
    for name in profiles.SCOPED_DAEMON_FILES:
        assert "/nissan-xterra-2008/" in changed[f"daemon.{name}"], name
    assert "daemon.calibration_dir" in changed
    assert golden["same_profile_is_idempotent"] is True
    assert "already scoped to profile 'nissan-xterra-2008'" in golden["rescope_refusal"]


# ---------------------------------------------------------------------------
# Every name still importable, from the module that owns it
# ---------------------------------------------------------------------------

#: Every public top-level name the unsplit ``profiles.py`` defined, as of the commit
#: before the split. The private ones are not re-exported: the one caller that
#: reached for one (``tests/test_profiles.py``, ``_scope_file``) imports it from
#: its owner.
PUBLIC_NAMES = (
    "ActiveVinMismatchError",
    "DuplicateVinError",
    "EDITABLE_FIELDS",
    "EvidenceRequiredError",
    "LEGACY_CALIBRATION_FILES",
    "LegacyAdoption",
    "MAX_TEXT_LENGTH",
    "MIGRATION_MARKER",
    "NoTrackedProfileError",
    "PARAMETER_EVIDENCE",
    "PARAMETER_FIELDS",
    "ProfileDeleteRefusedError",
    "ProfileDeletion",
    "ProfileEdit",
    "ProfileEditError",
    "ProfileResolution",
    "REGISTRY_SCHEMA_VERSION",
    "REVERTABLE_FIELDS",
    "SCOPED_DAEMON_DIRS",
    "SCOPED_DAEMON_FILES",
    "SEEDED_FROM_PROFILE",
    "VehicleInfoProbe",
    "VehicleInfoProber",
    "VehicleProfile",
    "YEAR_RANGE",
    "active_flash_history",
    "active_registry",
    "active_registry_path",
    "active_registry_sources",
    "delete_profile",
    "delete_refusal",
    "deleted_profiles",
    "deleted_slugs",
    "last_resolution",
    "legacy_adoption",
    "legacy_operational_dir",
    "load_registry",
    "migrate_legacy_calibration",
    "operational_dir",
    "operational_file",
    "operational_registry_path",
    "parameter_schema",
    "parameter_unit",
    "pending_restart",
    "profile_data_paths",
    "profile_dir",
    "profile_module_stem",
    "profile_overrides",
    "read_registry_file",
    "record_resolution",
    "rescope_like",
    "resolve_active_profile",
    "resolve_profile_for_tooling",
    "restore_profile",
    "revert_profile",
    "save_registry",
    "scope_config",
    "seed_machine_owned_files",
    "slugify",
    "source_registry_path",
    "unadopted_legacy_calibration",
    "update_profile",
    "update_profile_name",
    "vehicle_block_slug",
)

PACKAGE = REPO / "src" / "vehicle" / "profiles"
SUBMODULES = ("model", "edit", "legacy", "resolve", "scope")


def test_every_name_the_unsplit_module_exported_is_still_importable() -> None:
    missing = [name for name in PUBLIC_NAMES if not hasattr(profiles, name)]
    assert missing == []


def _importers() -> list[tuple[str, set[str]]]:
    """Every file importing from ``src.vehicle.profiles``, with the names it takes."""
    found: list[tuple[str, set[str]]] = []
    for root in ("src", "scripts", "tools", "tests"):
        for path in sorted((REPO / root).rglob("*.py")):
            if path == Path(__file__).resolve() or path.is_relative_to(PACKAGE):
                continue
            tree = ast.parse(path.read_text())
            names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "src.vehicle.profiles":
                    names.update(alias.name for alias in node.names)
            if names:
                found.append((str(path.relative_to(REPO)), names))
    return found


def test_every_name_any_file_imports_from_the_package_resolves() -> None:
    """The sweep the split promised: no import site was left pointing at a name that moved."""
    unresolved = [
        f"{path}: {name}"
        for path, names in _importers()
        for name in sorted(names)
        if not hasattr(profiles, name)
    ]
    assert unresolved == []


def test_a_re_export_is_the_owners_object() -> None:
    from src.vehicle import flash_history

    owners = {
        "VehicleProfile": "src.vehicle.profiles.model",
        "load_registry": "src.vehicle.profiles.model",
        "update_profile": "src.vehicle.profiles.edit",
        "resolve_active_profile": "src.vehicle.profiles.resolve",
        "scope_config": "src.vehicle.profiles.scope",
        "migrate_legacy_calibration": "src.vehicle.profiles.legacy",
        "operational_file": "src.vehicle.profiles.legacy",
        "active_flash_history": "src.vehicle.flash_history",
        "active_registry": "src.vehicle.flash_history",
    }
    for name, module in owners.items():
        assert getattr(profiles, name).__module__ == module, name
    assert profiles.active_flash_history is flash_history.active_flash_history


def test_the_classes_that_cross_a_process_boundary_pickle_from_their_new_modules() -> None:
    """The scoring child receives the parent's scoped config, whose ``_scope_source`` is a
    ``VehicleProfile``, and an edit error crosses the control child's pipe. Both are
    pickled by reference to the module that now owns them; nothing persists those
    bytes (the score cache is keyed by a hash of every ``src`` file, so a move
    already invalidates it)."""
    scoped = scope_config(CanaryConfig.from_yaml(EXAMPLES["pi"]), SYNTHETIC)
    blob = pickle.dumps(scoped)
    assert b"src.vehicle.profiles.model" in blob
    clone = pickle.loads(blob)
    assert clone.scoped_profile == SYNTHETIC.slug
    assert clone._scope_source == SYNTHETIC
    for error in (
        profiles.ProfileEditError("year", "year must be a whole number"),
        profiles.ProfileDeleteRefusedError("some-slug", "the active profile"),
    ):
        copy = pickle.loads(pickle.dumps(error))
        assert type(copy) is type(error)
        assert str(copy) == str(error)
        assert copy.__reduce__()[0] is type(error)
    assert profiles.ProfileEditError.__module__ == "src.vehicle.profiles.edit"


# ---------------------------------------------------------------------------
# Imports run one way, and each module imports first
# ---------------------------------------------------------------------------

#: Who may import whom among the package's own modules. ``edit`` reads the
#: resolution record and the registry (``model``); ``legacy`` needs the profile
#: data locations (``scope``) and the registry (``model``); ``resolve`` sits on
#: top of everything. Nothing imports ``resolve`` or the package ``__init__``.
ALLOWED = {
    "model": set(),
    "scope": {"model"},
    "edit": {"model"},
    "legacy": {"model", "scope"},
    "resolve": {"model", "scope", "edit", "legacy"},
}


def _sibling_imports(module: str) -> set[str]:
    tree = ast.parse((PACKAGE / f"{module}.py").read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[:3] == ["src", "vehicle", "profiles"]:
                found.add(parts[3] if len(parts) > 3 else "__init__")
        elif isinstance(node, ast.Import):
            found.update(a.name.split(".")[3] for a in node.names if "vehicle.profiles." in a.name)
    return found


@pytest.mark.parametrize("module", SUBMODULES)
def test_a_profiles_module_imports_only_below_itself(module: str) -> None:
    assert _sibling_imports(module) <= ALLOWED[module], module


def test_no_profiles_module_imports_the_flash_history() -> None:
    """``flash_history`` imports ``profiles``; the other way round is the cycle the
    package's lazy re-export (``__getattr__``) exists to avoid."""
    for module in SUBMODULES:
        text = (PACKAGE / f"{module}.py").read_text()
        assert "src.vehicle.flash_history" not in text, module


@pytest.mark.parametrize(
    "module",
    [
        "src.vehicle.profiles.model",
        "src.vehicle.profiles.scope",
        "src.vehicle.profiles.edit",
        "src.vehicle.profiles.legacy",
        "src.vehicle.profiles.resolve",
        "src.vehicle.flash_history",
        "src.vehicle.profiles",
    ],
)
def test_each_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    """A cycle shows up only for the module imported first, so try each one first."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}; from src.vehicle.profiles import {_PROBE}"],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    assert result.returncode == 0, result.stderr


_PROBE = (
    "active_flash_history, active_registry, scope_config, resolve_active_profile, update_profile"
)


# ---------------------------------------------------------------------------
# Nothing patches a name on the package
# ---------------------------------------------------------------------------


def _package_aliases(tree: ast.Module) -> set[str]:
    """Names this file binds to the ``src.vehicle.profiles`` package itself."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(
                (a.asname or a.name)
                for a in node.names
                if a.name == "src.vehicle.profiles" and a.asname
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "src.vehicle":
            aliases.update((a.asname or a.name) for a in node.names if a.name == "profiles")
    return aliases


def _patches_through_the_package(tree: ast.Module) -> list[int]:
    """Lines that set an attribute on the package, or patch ``src.vehicle.profiles.<name>``.

    A name that lives in a submodule is looked up there by the code that calls
    it, so a patch on the package applies to the re-export and changes nothing.
    Patch the owner (``src.vehicle.profiles.resolve``, ``flash_history`` ...).
    """
    aliases = _package_aliases(tree)
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            first = node.args[0] if node.args else None
            if name in {"setattr", "delattr", "setitem", "object", "patch", "dict"}:
                if isinstance(first, ast.Name) and first.id in aliases:
                    lines.append(node.lineno)
                elif (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and (
                        first.value == "src.vehicle.profiles"
                        or first.value.startswith("src.vehicle.profiles.")
                        and first.value.split(".")[3] not in SUBMODULES
                    )
                ):
                    lines.append(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            lines += [
                node.lineno
                for target in targets
                if isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in aliases
            ]
    return lines


def test_the_tripwire_for_a_patch_through_the_package_can_fail() -> None:
    def hits(source: str) -> list[int]:
        return _patches_through_the_package(ast.parse(source))

    assert hits("import src.vehicle.profiles as p\nmonkeypatch.setattr(p, 'load_registry', f)")
    assert hits("from src.vehicle import profiles\nmonkeypatch.setattr(profiles, 'x', 1)")
    assert hits("from src.vehicle import profiles as vp\nvp._resolution = None")
    assert hits("mock.patch('src.vehicle.profiles.load_registry')")
    assert hits("monkeypatch.setattr('src.vehicle.profiles._flash_history_cache', {})")
    assert not hits("mock.patch('src.vehicle.profiles.resolve.load_registry')")
    assert not hits(
        "from src.vehicle.profiles import resolve\nmonkeypatch.setattr(resolve, 'x', 1)"
    )
    assert not hits("import src.vehicle.profiles as p\nvalue = p.load_registry")


def test_nothing_patches_a_name_the_package_only_re_exports() -> None:
    offenders = [
        f"{path.relative_to(REPO)}:{line}"
        for root in ("src", "scripts", "tools", "tests")
        for path in sorted((REPO / root).rglob("*.py"))
        if path != Path(__file__).resolve()
        for line in _patches_through_the_package(ast.parse(path.read_text()))
    ]
    assert offenders == []
