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
    "fuel_comp_fit",
    "fuel_comp_restore_reproduction",
    "fuel_comp_samples",
    "fuel_plant_check",
    "import_uprev_log",
    "inventory_behavior_artifacts",
    "investigate_delivery",
    "memory",
    "manifold_check",
    "migrate_channel_names",
    "mint_comparison",
    "plant_validation",
    "record_command_change",
    "replay_window",
    "timing_lookup_coverage",
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


def test_a_per_vehicle_tool_refuses_a_profiles_disabled_config_in_one_line(
    tmp_path: Path,
) -> None:
    """Operator decision 2026-11-15: the memory-ledger tools refuse a
    profiles-disabled config. Before R30.3 they scoped it under the default
    slug and read a directory nothing writes ("0 record(s)"); after it they
    died with a traceback. Now: one line naming the cause."""
    from tools._cli import load_scoped

    path = write_isolated_config(tmp_path, "example", profiles={"enabled": False})
    with pytest.raises(SystemExit) as refused:
        load_scoped(argparse.Namespace(config=path, profile=None), per_vehicle=True)
    message = str(refused.value.code)
    assert message.startswith("refused: ") and "profiles.enabled: false" in message
    assert str(path) in message


def test_a_per_vehicle_tool_runs_on_a_profiles_enabled_config(tmp_path: Path) -> None:
    from tools._cli import load_scoped

    path = write_isolated_config(tmp_path, "example")
    loaded = load_scoped(argparse.Namespace(config=path, profile=None), per_vehicle=True)
    assert loaded.scoped_profile is not None


def _memory_ledger_tools() -> list[str]:
    """Every tool that opens the per-vehicle memory ledger, found, not listed."""
    return sorted(
        path.stem
        for path in (REPO_ROOT / "tools").glob("*.py")
        if "memory_ledger_for" in path.read_text(encoding="utf-8")
    )


def test_every_memory_ledger_tool_loads_its_config_per_vehicle() -> None:
    """A new tool that reads the ledger cannot forget the refusal: each
    ``load_scoped(...)`` call in such a tool passes ``per_vehicle=True``. A tool
    that takes its config through ``add_profile_dir_args`` (``change_detection_report``,
    where ``--config`` is only one way to name a ledger) refuses a profiles-disabled
    config itself, with ``refusal_for_unscoped``."""
    tools = _memory_ledger_tools()
    assert {"memory", "mint_comparison", "investigate_delivery"} <= set(tools)
    missing: list[str] = []
    for name in tools:
        source = (REPO_ROOT / "tools" / f"{name}.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "load_scoped"
        ]
        per_vehicle = [
            call
            for call in calls
            if any(
                kw.arg == "per_vehicle"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in call.keywords
            )
        ]
        if not calls and "refusal_for_unscoped(" in source:
            continue
        if not calls or len(per_vehicle) != len(calls):
            missing.append(name)
    assert missing == []


def test_the_memory_tool_refuses_without_a_traceback(tmp_path: Path) -> None:
    path = write_isolated_config(tmp_path, "example", profiles={"enabled": False})
    run = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "memory.py"), "--config", str(path), "query"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
    )
    assert run.returncode == 1
    assert "Traceback" not in run.stderr
    assert run.stderr.strip().startswith("refused: ")


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


# ---------------------------------------------------------------------------
# The MCP read tools scope as their script twins do (R30.3)
# ---------------------------------------------------------------------------


def _storage_the_mcp_tools_read(
    path: Path, monkeypatch: pytest.MonkeyPatch, profile: str | None = None
) -> dict[str, Path]:
    """The session root each local MCP tool resolves a session under, for the
    config at ``path``. Each stub refuses, so the tool stops right after scoping."""
    from src.mcp_server.tools import behavior, tuning_analysis

    seen: dict[str, Path] = {}

    def _spy(name: str) -> Any:
        def _resolve(root: Path, session: str) -> Path:
            seen[name] = root
            raise FileNotFoundError(session)

        return _resolve

    monkeypatch.setattr(behavior, "resolve_session", _spy("get_behavior_deviations"))
    monkeypatch.setattr(tuning_analysis, "resolve_session", _spy("get_delivery_error"))
    behavior.get_behavior_deviations(path, session="s")
    tuning_analysis.get_delivery_error(path, session="s", profile=profile)
    tuning_analysis.compare_tuning_sessions(
        path, session="a", against="b", value="obd:MAF", profile=profile
    )
    return seen


@pytest.mark.parametrize("profiles", ["enabled", "disabled"])
def test_the_mcp_read_tools_scope_as_their_script_twins_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profiles: str
) -> None:
    """``scripts/analyze_session.py`` and ``get_behavior_deviations`` (and the
    ``tuning_report`` / tuning-analysis pair) read one vehicle's sessions. They
    agreed with profiles enabled, and disagreed with profiles disabled: the
    script read the base paths, the MCP tool nested them under the default
    slug, a directory nothing writes."""
    from scripts import _cli

    overrides: dict[str, Any] = {"profiles": {"enabled": False}} if profiles == "disabled" else {}
    path = write_isolated_config(tmp_path, "example", **overrides)
    base = CanaryConfig.from_yaml(path)
    expected = _cli.scoped_config(base, None).collection.storage_path
    if profiles == "disabled":
        assert expected == base.collection.storage_path

    seen = _storage_the_mcp_tools_read(path, monkeypatch)

    assert seen == {"get_behavior_deviations": expected, "get_delivery_error": expected}


def test_the_mcp_tuning_tools_take_a_profile_slug_as_the_scripts_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import _cli

    path = write_isolated_config(tmp_path, "example")
    expected = _cli.scoped_config(CanaryConfig.from_yaml(path), "truck").collection.storage_path
    assert expected.name == "truck"

    seen = _storage_the_mcp_tools_read(path, monkeypatch, profile="truck")

    assert seen["get_delivery_error"] == expected


# ---------------------------------------------------------------------------
# --profile is a slug, with no default, in the tools that read data/<slug>/ (R30.4)
# ---------------------------------------------------------------------------


def _profile_dir_parser(**kwargs: Any) -> argparse.ArgumentParser:
    from tools._cli import add_profile_dir_args

    parser = argparse.ArgumentParser()
    add_profile_dir_args(parser, **kwargs)
    return parser


def test_the_profile_flags_have_no_default_vehicle() -> None:
    args = _profile_dir_parser().parse_args([])
    assert (args.profile, args.config, args.sessions_root, args.data_dir) == (
        None,
        None,
        None,
        None,
    )
    named = _profile_dir_parser(sessions_flag="--sessions-dir").parse_args(
        ["--sessions-dir", "s", "--profile", "truck"]
    )
    assert (named.profile, named.sessions_root) == ("truck", Path("s"))
    assert "--sessions-root" not in _profile_dir_parser(sessions_flag=None).format_help()


def test_a_missing_profile_refuses_in_one_line() -> None:
    from tools._cli import profile_data_or_exit

    args = _profile_dir_parser().parse_args([])
    with pytest.raises(SystemExit) as refused:
        profile_data_or_exit(args)
    message = str(refused.value)
    assert message.startswith("refused: --profile SLUG is required")
    assert "\n" not in message


def test_a_profile_path_is_refused_by_name_not_looked_up(tmp_path: Path) -> None:
    """Before R30.4 these tools took ``--profile data/<slug>``; a stale invocation
    says what to write instead."""
    from tools._cli import ProfileDataError, load_profile_data

    args = _profile_dir_parser().parse_args(["--profile", "data/nissan-xterra-2008"])
    with pytest.raises(ProfileDataError, match=r"slug, not a path.*--profile nissan-xterra-2008"):
        load_profile_data(args)


def test_a_slug_resolves_under_the_data_dir_and_an_unknown_one_refuses(tmp_path: Path) -> None:
    from tools._cli import ProfileDataError, load_profile_data

    (tmp_path / "truck").mkdir()
    parser = _profile_dir_parser()
    data = load_profile_data(parser.parse_args(["--profile", "truck", "--data-dir", str(tmp_path)]))
    assert (data.slug, data.directory, data.sessions_root, data.config) == (
        "truck",
        tmp_path / "truck",
        None,
        None,
    )
    with pytest.raises(ProfileDataError, match="sessions root"):
        data.require_sessions_root()
    with pytest.raises(ProfileDataError, match="no profile directory"):
        load_profile_data(parser.parse_args(["--profile", "van", "--data-dir", str(tmp_path)]))
    # Without --data-dir it is the checkout's data/, where a real profile is.
    shipped = load_profile_data(parser.parse_args(["--profile", "nissan-xterra-2008"]))
    assert shipped.directory == REPO_ROOT / "data" / "nissan-xterra-2008"


def test_the_config_gives_the_profile_and_the_sessions_root(tmp_path: Path) -> None:
    from tools._cli import load_profile_data

    path, base = _base(tmp_path)
    slug = resolve_profile_for_tooling(base, None).slug
    (tmp_path / "data" / slug).mkdir(parents=True)
    parser = _profile_dir_parser()
    flags = ["--config", str(path), "--data-dir", str(tmp_path / "data")]

    data = load_profile_data(parser.parse_args(flags))
    assert data.slug == slug
    assert data.sessions_root == base.collection.storage_path / slug  # the scoped storage
    assert data.config is not None and data.config.scoped_profile == slug

    explicit = load_profile_data(parser.parse_args([*flags, "--sessions-root", str(tmp_path)]))
    assert explicit.sessions_root == tmp_path  # the flag wins over the config


def test_a_profiles_disabled_config_cannot_name_the_profile(tmp_path: Path) -> None:
    """Its paths are the vehicle's and carry no slug, so ``--profile`` is needed."""
    from tools._cli import ProfileDataError, load_profile_data

    path = write_isolated_config(tmp_path, "example", profiles={"enabled": False})
    with pytest.raises(ProfileDataError, match="--profile SLUG is required"):
        load_profile_data(_profile_dir_parser().parse_args(["--config", str(path)]))


def test_a_platform_file_is_found_through_the_configs_vehicle(tmp_path: Path) -> None:
    """``data/platforms/<id>/rom_backup.yaml`` serves every profile of the
    generation: found with the config's make, model and year, refused without."""
    from tools._cli import ProfileDataError, load_profile_data

    path = write_isolated_config(tmp_path, "example")
    (tmp_path / "data" / "nissan-xterra-2013").mkdir(parents=True)
    flags = ["--profile", "nissan-xterra-2013", "--data-dir", str(tmp_path / "data")]
    parser = _profile_dir_parser()

    without = load_profile_data(parser.parse_args(flags))
    with pytest.raises(ProfileDataError, match="only with --config"):
        without.file("rom_backup.yaml")

    config = CanaryConfig.from_yaml(path)
    vehicle = config.vehicle.model_copy(update={"make": "Nissan", "model": "Xterra", "year": 2013})
    stated = config.model_copy(update={"vehicle": vehicle})
    with_config = load_profile_data(parser.parse_args(flags)).__class__(
        slug="nissan-xterra-2013",
        directory=tmp_path / "data" / "nissan-xterra-2013",
        sessions_root=None,
        config=stated,
    )
    found = with_config.file("rom_backup.yaml")
    assert found.parent.parent == REPO_ROOT / "data" / "platforms"
    own = tmp_path / "data" / "nissan-xterra-2013" / "rom_backup.yaml"
    own.write_text("{}\n")
    assert with_config.file("rom_backup.yaml") == own  # a profile's own file wins


#: (module, argv that satisfies everything but the vehicle flags; ``@`` marks where
#: they go when it is not the end: ``manifold_check`` takes them before its subcommand)
PROFILE_DIR_TOOLS: tuple[tuple[str, list[str]], ...] = (
    ("fuel_plant_check", ["chain"]),
    ("manifold_check", ["@", "study"]),
    ("plant_validation", ["inventory"]),
    ("fuel_comp_samples", ["--out", "o"]),
    ("fuel_comp_restore_reproduction", ["--session", "s", "--out", "o"]),
    ("fuel_comp_fit", ["--out", "o"]),
    ("timing_lookup_coverage", ["--session", "s", "--pairing-ms", "1", "--out", "o"]),
    ("change_detection_report", []),
    ("audit_artifact_lineage", []),
)


@pytest.mark.parametrize(("module", "argv"), PROFILE_DIR_TOOLS)
def test_a_vehicle_data_tool_refuses_without_a_profile_and_takes_no_path(
    module: str, argv: list[str]
) -> None:
    import importlib

    main = importlib.import_module(f"tools.{module}").main

    def call(*flags: str) -> Any:
        at = argv.index("@") if "@" in argv else len(argv)
        return main([*argv[:at], *flags, *(a for a in argv[at:] if a != "@")])

    with pytest.raises(SystemExit, match=r"^refused: --profile SLUG is required"):
        call()
    with pytest.raises(SystemExit, match=r"^refused: --profile takes a slug, not a path"):
        call("--profile", "data/nissan-xterra-2008")
    with pytest.raises(SystemExit, match=r"^refused: no profile directory"):
        call("--profile", "no-such-vehicle")


def test_rom_sim_refuses_a_missing_profile_by_name(capsys: pytest.CaptureFixture[str]) -> None:
    from tools import rom_sim

    for flags in ([], ["--profile", "data/nissan-xterra-2008"]):
        code = rom_sim.main(["inspect", "--edits", "e.yaml", *flags])
        payload = json.loads(capsys.readouterr().out)
        assert (code, payload["refused"]) == (2, "profile_not_found")
        assert "--profile" in payload["message"]


def test_a_drive_design_needs_a_sessions_root_only_for_a_bare_session_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tools import rom_sim

    code = rom_sim.main(
        [
            "design", "drives", "--profile", "nissan-xterra-2008", "--base-revision", "r",
            "--sessions", "2026-10-04T194342", "--out", "/tmp/never-written",
        ]
    )  # fmt: skip
    payload = json.loads(capsys.readouterr().out)
    assert (code, payload["refused"]) == (2, "sessions_root_unstated")
    assert "--sessions-root" in payload["message"] and "--config" in payload["message"]
