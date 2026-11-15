"""One way for a script or tool to scope the config it loaded (R30.3).

``scope_config`` was spelled at about 25 call sites; each was a place the P02.11
rule (refuse an unscoped config while profiles are enabled) could be forgotten,
and ``tuning_report`` had forgotten the ``profiles.enabled`` half. These tests
pin the one function, the re-export the scripts use, the pandas-free ``tools``
counterpart, and ``tools/replay_window.py``, which never scoped at all and died
with ``UnscopedConfigError`` on any config with profiles enabled.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from src.config import CanaryConfig, UnscopedConfigError, require_profile_scoped
from src.paths import REPO_ROOT
from src.vehicle.profiles import resolve_profile_for_tooling, scoped_config_for_tooling
from tests.configs import profiles_disabled, write_isolated_config

TOOLS_DIR = REPO_ROOT / "tools"
#: The tools that scope through ``tools._cli`` (run as files in the checks below).
SCOPING_TOOLS = (
    "change_detection_report",
    "check_manifest_producibility",
    "import_uprev_log",
    "inventory_behavior_artifacts",
    "investigate_delivery",
    "memory",
    "migrate_channel_names",
    "mint_comparison",
    "record_command_change",
    "replay_window",
)


def _base(tmp_path: Path) -> tuple[Path, CanaryConfig]:
    path = write_isolated_config(tmp_path, "example")
    config = CanaryConfig.from_yaml(path)
    assert config.profiles.enabled
    return path, config


# ---------------------------------------------------------------------------
# src.vehicle.profiles.scoped_config_for_tooling
# ---------------------------------------------------------------------------


def test_a_profiles_enabled_config_comes_back_scoped(tmp_path: Path) -> None:
    _, base = _base(tmp_path)
    with pytest.raises(UnscopedConfigError):  # what an unscoped tool config met
        require_profile_scoped(base, reader="featurize_wide_frame")

    scoped = scoped_config_for_tooling(base, None)

    require_profile_scoped(scoped, reader="featurize_wide_frame")
    slug = resolve_profile_for_tooling(base, None).slug
    assert scoped.scoped_profile == slug
    assert scoped.collection.storage_path == base.collection.storage_path / slug
    assert scoped.daemon.models_dir == base.daemon.models_dir / slug


def test_a_slug_picks_the_profile_and_scoping_twice_is_a_no_op(tmp_path: Path) -> None:
    _, base = _base(tmp_path)
    scoped = scoped_config_for_tooling(base, "truck")
    assert scoped.scoped_profile == "truck"
    assert scoped_config_for_tooling(scoped, None) is scoped
    assert scoped_config_for_tooling(scoped, "truck") is scoped
    with pytest.raises(ValueError, match="already scoped"):
        scoped_config_for_tooling(scoped, "another-vehicle")


def test_a_profiles_disabled_config_is_left_alone(tmp_path: Path) -> None:
    """Its base paths are the vehicle's; scoping would nest them under a slug
    the daemon never wrote (``tuning_report`` did, without this guard)."""
    _, base = _base(tmp_path)
    single = profiles_disabled(base)
    assert scoped_config_for_tooling(single, None) is single
    assert scoped_config_for_tooling(single, "truck") is single


def test_the_scripts_scope_through_the_same_function() -> None:
    from scripts import _cli

    assert _cli.scoped_config is scoped_config_for_tooling


# ---------------------------------------------------------------------------
# tools/_cli.py
# ---------------------------------------------------------------------------


def test_tools_cli_imports_no_pandas() -> None:
    """The reason ``tools/`` has a ``_cli`` of its own: ``scripts/_cli`` pulls in
    pandas through session discovery, which a tool reading a ledger does not need."""
    probe = (
        "import sys\n"
        "import tools._cli\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in {'pandas', 'numpy'})\n"
        "assert 'pandas' not in sys.modules, loaded\n"
        "assert 'scripts._cli' not in sys.modules\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


def test_the_config_and_profile_pair_has_one_shape() -> None:
    from tools._cli import add_config_profile_args

    parser = argparse.ArgumentParser()
    add_config_profile_args(parser)
    with pytest.raises(SystemExit):  # --config is required
        parser.parse_args([])
    args = parser.parse_args(["--config", "c.yaml"])
    assert (args.config, args.profile) == (Path("c.yaml"), None)
    assert parser.parse_args(["--config", "c.yaml", "--profile", "truck"]).profile == "truck"

    optional = argparse.ArgumentParser()
    add_config_profile_args(optional, config_required=False, profile_help="for --config")
    args = optional.parse_args([])
    assert (args.config, args.profile) == (None, None)
    assert "for --config" in optional.format_help()


def test_load_scoped_reads_the_flags_and_scopes_with_profiles_enabled(tmp_path: Path) -> None:
    from tools._cli import load_scoped

    path, base = _base(tmp_path)
    scoped = load_scoped(argparse.Namespace(config=path, profile="truck"))
    assert scoped.scoped_profile == "truck"
    assert scoped.collection.storage_path == base.collection.storage_path / "truck"
    # A tool with no --profile gets the vehicle: block's profile.
    assert load_scoped(argparse.Namespace(config=path)).scoped_profile == (
        resolve_profile_for_tooling(base, None).slug
    )


def test_load_scoped_leaves_a_profiles_disabled_config_unscoped(tmp_path: Path) -> None:
    from tools._cli import load_scoped

    path = write_isolated_config(tmp_path, "example", profiles={"enabled": False})
    loaded = load_scoped(argparse.Namespace(config=path, profile=None))
    assert loaded.scoped_profile is None
    assert loaded.collection.storage_path == CanaryConfig.from_yaml(path).collection.storage_path


#: Run as a file with no path edit of their own: ``src`` is installed (editable).
NO_PATH_EDIT_TOOLS = ("audit_artifact_lineage", "make_investigation_cases")


@pytest.mark.parametrize("name", (*SCOPING_TOOLS, *NO_PATH_EDIT_TOOLS))
def test_a_tool_run_as_a_file_finds_its_cli_preamble(name: str, tmp_path: Path) -> None:
    """The docs run tools as ``.venv/bin/python tools/<name>.py`` from anywhere:
    ``tools`` is not installed, so the preamble must put the repo root on the path
    (the bare ``_cli`` import), as the in-``main()`` path edit used to."""
    done = subprocess.run(
        [sys.executable, str(TOOLS_DIR / f"{name}.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "usage:" in done.stdout


def _tool_trees() -> list[tuple[Path, ast.Module]]:
    return [(p, ast.parse(p.read_text())) for p in sorted(TOOLS_DIR.glob("*.py"))]


def test_a_tool_never_inserts_the_repo_root_inside_a_function() -> None:
    """Eleven tools did, in ``main()``, so ``src`` imported after the call. ``src``
    is installed (editable) and ``tools/_cli`` puts the root on the path once, at
    import; a module-level shim (``replay_behavior_compare``) is the other shape."""
    offenders: list[str] = []
    for path, tree in _tool_trees():
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "insert"
                    and ast.unparse(node.func.value) == "sys.path"
                    and "REPO_ROOT" in ast.unparse(node)
                ):
                    offenders.append(f"{path.name}:{node.lineno} in {function.name}()")
    assert offenders == []


def test_a_bare_cli_import_in_a_tool_falls_back_to_the_package() -> None:
    """``from _cli import`` works when run as a file; a test imports the tool as
    ``tools.<name>``, where only ``tools._cli`` resolves."""
    for path, tree in _tool_trees():
        if path.name == "_cli.py":
            continue
        guarded: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                fallback = {
                    handler_node.module
                    for handler in node.handlers
                    for handler_node in ast.walk(handler)
                    if isinstance(handler_node, ast.ImportFrom)
                }
                if "tools._cli" in fallback:
                    guarded |= {
                        stmt.lineno for stmt in node.body if isinstance(stmt, ast.ImportFrom)
                    }
        bare = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "_cli"
        ]
        assert all(line in guarded for line in bare), f"{path.name}: unguarded `_cli` import"


# ---------------------------------------------------------------------------
# tools/replay_window.py
# ---------------------------------------------------------------------------


class _Report:
    def to_dict(self) -> dict[str, Any]:
        return {"summary": {"rows_scored": 0}}


def test_replay_window_scopes_its_config_when_profiles_are_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It never scoped: on a normal config (profiles enabled) the run died with
    ``UnscopedConfigError: featurize_wide_frame needs a profile-scoped config``
    and only ``profiles.enabled: false`` ran. The sessions, the models and the
    anomaly log it reads are the vehicle's, under ``<base>/<slug>/``."""
    from tools import replay_window as tool

    path, base = _base(tmp_path)
    slug = resolve_profile_for_tooling(base, None).slug
    session = base.collection.storage_path / slug / "2026-11-15T120000"
    session.mkdir(parents=True)
    seen: dict[str, Any] = {}

    def _replay(**kwargs: Any) -> _Report:
        # The library's first act on the config is featurization's refusal.
        require_profile_scoped(kwargs["config"], reader="featurize_wide_frame")
        seen.update(kwargs)
        return _Report()

    monkeypatch.setattr(tool, "replay_window", _replay)
    out = tmp_path / "out" / "report.json"

    code = tool.main(["--session-id", session.name, "--config", str(path), "--out", str(out)])

    assert code == 0
    assert json.loads(out.read_text()) == {"summary": {"rows_scored": 0}}
    config = seen["config"]
    assert config.scoped_profile == slug
    assert seen["session_dir"] == session
    assert seen["if_model_path"] == (
        base.daemon.models_dir / slug / "isolation_forest" / "model.joblib"
    )
    assert seen["lstm_model_path"] == base.daemon.models_dir / slug / "lstm_autoencoder"
    assert seen["anomalies_dir"] == base.daemon.anomalies_dir / slug


def test_replay_window_still_runs_on_a_profiles_disabled_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools import replay_window as tool

    path = write_isolated_config(tmp_path, "example", profiles={"enabled": False})
    base = CanaryConfig.from_yaml(path)
    session = base.collection.storage_path / "2026-11-15T120000"
    session.mkdir(parents=True)
    seen: dict[str, Any] = {}

    def _replay(**kwargs: Any) -> _Report:
        seen.update(kwargs)
        return _Report()

    monkeypatch.setattr(tool, "replay_window", _replay)
    out = tmp_path / "report.json"

    assert tool.main(["--session-id", session.name, "--config", str(path), "--out", str(out)]) == 0
    assert seen["session_dir"] == session
    assert seen["config"].scoped_profile is None
