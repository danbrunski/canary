"""Every script's argparse surface, pinned (R06.2) — the safety net for R06.3/R06.4.

``scripts/retrain.py`` and ``train_sequence.py`` are about to be broken up. A
refactor of a CLI has one silent failure mode: the flags still parse, with a
different default, type or requiredness, and the ``JobController`` (which
builds argv for retrain / refit / did_sweep) or a hand-typed command keeps
"working". This pins, per script, each option's strings, dest, default, type,
nargs, choices, action and required flag — not help text — and round-trips
every ``src/control/jobs.py`` job kind's argv through the real parser.

The parser is captured by intercepting ``ArgumentParser.parse_args`` while the
script's own ``main``/``parse_args`` builds it, so no script needs a
``build_parser`` seam for this.

Regenerate after an *intended* CLI change::

    CANARY_UPDATE_CLI_SNAPSHOT=1 .venv/bin/python -m pytest -n0 tests/test_script_cli_snapshot.py
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import json
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from src.config import CanaryConfig
from src.control.jobs import ALLOWED_JOB_KINDS, JobController
from tests.configs import write_isolated_config

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SNAPSHOT = Path(__file__).parent / "fixtures" / "script_cli_snapshot.json"

#: Python files under scripts/ that are not command-line entry points.
NOT_A_CLI = frozenset({"_cli.py"})

#: Scripts whose CLI is hand-parsed rather than argparse. Listed so a new
#: hand-parsed script is a decision, not an omission.
NO_ARGPARSE = frozenset({"check_dev_env.py", "ups_shutdown_watch.py"})

#: Scripts whose entry point needs argv to reach its main parser.
#: ``train_sequence`` strips a required ``--backbone`` with a pre-parser first;
#: the tcn backbone's surface is pinned through ``train_tcn.py``.
ENTRY_ARGV: dict[str, list[str]] = {"train_sequence.py": ["--backbone", "lstm"]}


#: Environment variables some scripts read for a default. Cleared while a
#: parser is built, so the snapshot never holds the operator's host or token.
ENV_DEFAULTS = (
    "CANARY_COMPANION_TOKEN",
    "CANARY_CONFIG",
    "CANARY_CONTROL_TOKEN",
    "CANARY_HOST",
    "CANARY_TOKEN",
    "CANARY_URL",
)

#: Defaults that depend on the machine, pinned as a marker rather than a value.
#: ``--device`` is ``default_training_device()``: cuda on the laptop, cpu on CI.
MACHINE_DEFAULTS = frozenset({"--device"})


class _Captured(Exception):  # noqa: N818 - control flow, not an error
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__(parser.prog)
        self.parser = parser


def _script_paths() -> list[Path]:
    return sorted(p for p in SCRIPTS_DIR.glob("*.py") if p.name not in NOT_A_CLI)


@pytest.fixture(autouse=True)
def _forget_bare_script_modules() -> Iterator[None]:
    """Drop every module this test loaded from ``scripts/`` under a bare name.

    ``_load_script`` runs a script the way ``python scripts/x.py`` does, with
    ``scripts/`` on ``sys.path`` (the one justified ``sys.path`` change in the
    suite, R06.4), so its ``from _cli import`` binds a bare ``_cli``. Left in
    ``sys.modules``, that bare module would answer a later test's ``import
    scripts.x`` too (the dance tries the bare name first), and a patch of
    ``scripts._cli`` would miss code bound to it: the dual-identity trap the
    sys.path hacks were deleted for.
    """
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        module_file = getattr(sys.modules.get(name), "__file__", None)
        if (
            not name.startswith(("scripts.", "_cli_snapshot_"))  # the latter: monkeypatch's
            and module_file
            and Path(module_file).resolve().parent == SCRIPTS_DIR
        ):
            del sys.modules[name]


def _load_script(path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import ``scripts/<name>.py`` the way it runs: its own directory first on
    ``sys.path`` (for ``from _cli import ...``), scoped to this test."""
    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))
    for var in ENV_DEFAULTS:
        monkeypatch.delenv(var, raising=False)
    # canaryctl falls back to ~/.canary/env; point it at nothing.
    monkeypatch.setenv("CANARY_ENV_FILE", str(SCRIPTS_DIR / "_no_such_env_file"))
    name = f"_cli_snapshot_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as exc:
        # CI has no torch (operator decision 2026-09-23): a script that imports
        # it at module level is pinned only by the local gate.
        if exc.name == "torch":
            pytest.skip(f"{path.name} imports torch")
        raise
    return module


def _capture_parser(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> argparse.ArgumentParser | None:
    """The parser ``module`` builds, stopped at its first ``parse_args`` call."""

    def _intercept(self: argparse.ArgumentParser, *_a: Any, **_k: Any) -> Any:
        raise _Captured(self)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", _intercept)
    monkeypatch.setattr(sys, "argv", [module.__name__])
    for entry in ("build_parser", "parse_args", "main"):
        func: Callable[..., Any] | None = getattr(module, entry, None)
        if func is None:
            continue
        try:
            if entry == "build_parser":
                result = func()
                return result if isinstance(result, argparse.ArgumentParser) else None
            argv = ENTRY_ARGV.get(Path(str(module.__file__)).name, [])
            takes_argv = bool(inspect.signature(func).parameters)
            func(list(argv)) if takes_argv else func()
        except _Captured as captured:
            return captured.parser
        except (TypeError, AttributeError):
            # e.g. train_sequence.parse_args(backend, argv): not an argv-only
            # entry point; its main() is tried next.
            continue
    return None


def _name(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "__qualname__", None) or type(value).__name__


def _normalise(value: Any) -> Any:
    """JSON-stable default: repo- and home-rooted paths made relative."""
    if isinstance(value, Path | str) and not isinstance(value, bool):
        text = str(value)
        text = text.replace(str(REPO_ROOT), "<repo>").replace(str(Path.home()), "<home>")
        return text if isinstance(value, str) else f"Path({text})"
    if isinstance(value, list | tuple | set | frozenset):
        items = [_normalise(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, set | frozenset) else items
    if isinstance(value, dict):
        return {str(k): _normalise(v) for k, v in value.items()}
    if isinstance(value, int | float | bool) or value is None:
        return value
    return f"<{type(value).__name__}>"


def _surface(parser: argparse.ArgumentParser, *, top: bool = True) -> dict[str, Any]:
    options: dict[str, Any] = {}
    subcommands: dict[str, Any] = {}
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public walk
        if isinstance(action, argparse._HelpAction):  # noqa: SLF001
            continue
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, sub in sorted(action.choices.items()):
                subcommands[name] = _surface(sub, top=False)
            continue
        key = action.option_strings[0] if action.option_strings else action.dest
        options[key] = {
            "option_strings": list(action.option_strings),
            "dest": action.dest,
            "action": type(action).__name__,
            "default": (
                "<SUPPRESS>"
                if action.default is argparse.SUPPRESS
                else "<machine>"
                if top and key in MACHINE_DEFAULTS
                else _normalise(action.default)
            ),
            "type": _name(action.type),
            "nargs": action.nargs,
            "choices": _normalise(list(action.choices)) if action.choices is not None else None,
            "required": action.required,
        }
    surface: dict[str, Any] = {"options": options}
    if subcommands:
        surface["subcommands"] = subcommands
    return surface


def _parser_for(path: Path, monkeypatch: pytest.MonkeyPatch) -> argparse.ArgumentParser:
    module = _load_script(path, monkeypatch)
    parser = _capture_parser(module, monkeypatch)
    assert parser is not None, f"{path.name}: no argparse parser captured"
    # Later calls in the same test parse for real.
    monkeypatch.undo()
    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))
    return parser


def _snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}


@pytest.fixture(scope="module")
def recorded() -> Iterator[dict[str, Any]]:
    """Collects surfaces for ``CANARY_UPDATE_CLI_SNAPSHOT=1`` and writes them once."""
    surfaces: dict[str, Any] = {}
    yield surfaces
    if os.environ.get("CANARY_UPDATE_CLI_SNAPSHOT") and surfaces:
        merged = {**_snapshot(), **surfaces}
        SNAPSHOT.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")


def test_every_script_is_snapshotted_or_listed() -> None:
    names = {p.name for p in _script_paths()}
    expected = set(_snapshot()) | NO_ARGPARSE
    assert names == expected, "a script was added or removed; regenerate the snapshot"


@pytest.mark.parametrize(
    "path", [p for p in _script_paths() if p.name not in NO_ARGPARSE], ids=lambda p: p.name
)
def test_script_cli_matches_snapshot(
    path: Path, monkeypatch: pytest.MonkeyPatch, recorded: dict[str, Any]
) -> None:
    surface = _surface(_parser_for(path, monkeypatch))
    if os.environ.get("CANARY_UPDATE_CLI_SNAPSHOT"):
        recorded[path.name] = surface
        return
    expected = _snapshot().get(path.name)
    assert expected is not None, f"{path.name} is not in {SNAPSHOT.name}; regenerate"
    assert surface == expected, f"{path.name}: CLI surface changed"


# ---------------------------------------------------------------------------
# JobController argv -> the real parser, for every job kind
# ---------------------------------------------------------------------------

SESSION_ID = "2026-09-01T100000"

#: One representative params dict per kind, exercising every params-to-flag path.
JOB_PARAMS: dict[str, list[dict[str, Any]]] = {
    "retrain_if": [
        {},
        {"days": 7},
        {"all": True},
        {"since": "2026-09-01"},
        {"session_ids": [SESSION_ID]},
    ],
    "retrain_lstm": [{"device": "cpu", "accept_thin_calibration": "two drives only"}],
    "retrain_tcn": [{}],
    "retrain_all": [{"accept_thin_calibration": "bench"}],
    "refit_behavior_models": [{"recipes": ["fuel_trim"], "dry_run": True}],
    "corpus_report": [{}],
    "replay_window": [
        {
            "session_id": SESSION_ID,
            "start": "2026-09-01T10:00:00",
            "end": "2026-09-01T10:05:00",
            "if_model": "models/isolation_forest",
            "lstm_model": "models/lstm_autoencoder",
            "anomalies_dir": "data/anomalies",
            "max_rows": 100,
            "max_windows": 10,
        }
    ],
    "export_dataset": [
        {
            "session_ids": [SESSION_ID],
            "channels": ["RPM"],
            "resample_hz": 5,
            "include_tags": ["wot"],
            "exclude_tags": ["idle"],
            "formats": ["csv", "parquet"],
            "purpose": "check",
            "zip": False,
            "out_dir": "data/exports",
        }
    ],
    "did_sweep": [
        {
            "start": "0x1100",
            "end": "0x12FF",
            "label": "control",
            "header": "7E0",
            "session": "C0",
            "keepalive": 1.5,
            "delay_seconds": 0.01,
            "read_timeout": 0.5,
            "batch": 3,
            "widths_from": "snap.json",
        }
    ],
}


def test_every_job_kind_has_round_trip_params() -> None:
    assert set(JOB_PARAMS) == ALLOWED_JOB_KINDS


def _controller(tmp_path: Path) -> JobController:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / SESSION_ID).mkdir()
    cfg_path = write_isolated_config(tmp_path, "daemon", storage=storage)
    return JobController(
        config=CanaryConfig.from_yaml(cfg_path),
        repo_root=tmp_path,
        config_path=cfg_path,
        jobs_dir=tmp_path / "jobs",
        python=Path(sys.executable),
    )


@pytest.mark.parametrize(
    ("kind", "params"),
    [(kind, params) for kind, cases in sorted(JOB_PARAMS.items()) for params in cases],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_job_argv_parses_with_the_real_parser(
    kind: str, params: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = list(
        _controller(tmp_path)._build_command(kind, params, job_id="job-1")  # noqa: SLF001
    )
    script = REPO_ROOT / command[1]
    assert script.is_file(), f"{kind}: {command[1]} does not exist"
    module = (
        _load_script(script, monkeypatch)
        if script.parent == SCRIPTS_DIR
        else _load_tool(script, monkeypatch)
    )
    parser = _capture_parser(module, monkeypatch)
    assert parser is not None
    monkeypatch.undo()
    parsed = parser.parse_args(command[2:])  # SystemExit here is the failure
    if "accept_thin_calibration" in params:
        assert parsed.accept_thin_calibration == params["accept_thin_calibration"]


def _load_tool(path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    try:
        return importlib.import_module(f"tools.{path.stem}")
    except ModuleNotFoundError as exc:
        if exc.name == "torch":
            pytest.skip(f"tools/{path.name} imports torch")
        raise


# ---------------------------------------------------------------------------
# --accept-* flags: every one is a stated reason (A02.2 / A04.4 / A06.5)
# ---------------------------------------------------------------------------

#: Every ``--accept-*`` flag across scripts/, and a minimal argv that satisfies
#: the rest of that parser.
ACCEPT_FLAGS: dict[tuple[str, str], list[str]] = {
    ("retrain.py", "--accept-thin-calibration"): [],
    ("retrain.py", "--accept-alert-budget"): [],
    ("train_sequence.py", "--accept-thin-calibration"): [],
    ("sequence_bakeoff.py", "--accept-thin-calibration"): ["--evaluate", "eval-session"],
}


def test_every_accept_flag_is_listed() -> None:
    found = set()
    for path in _script_paths():
        for line in path.read_text().splitlines():
            stripped = line.strip().strip(",")
            if stripped.startswith('"--accept-'):
                found.add((path.name, stripped.strip('"')))
    assert found == set(ACCEPT_FLAGS)


@pytest.mark.parametrize(("script", "flag"), sorted(ACCEPT_FLAGS), ids=lambda v: v)
@pytest.mark.parametrize("reason", ["", "  ", "\t\n"])
def test_accept_flag_refuses_a_blank_reason(
    script: str,
    flag: str,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A whitespace reason used to pass the IF path's ``if calibration_override:``
    check (A04.4) while the sequence path stripped it and refused."""
    parser = _parser_for(SCRIPTS_DIR / script, monkeypatch)
    with pytest.raises(SystemExit):
        parser.parse_args([*ACCEPT_FLAGS[(script, flag)], flag, reason])
    assert "stated reason" in capsys.readouterr().err


@pytest.mark.parametrize(("script", "flag"), sorted(ACCEPT_FLAGS), ids=lambda v: v)
def test_accept_flag_keeps_a_stated_reason_stripped(
    script: str, flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    parser = _parser_for(SCRIPTS_DIR / script, monkeypatch)
    parsed = parser.parse_args([*ACCEPT_FLAGS[(script, flag)], flag, "  two drives only "])
    assert getattr(parsed, flag.removeprefix("--").replace("-", "_")) == "two drives only"


@pytest.mark.parametrize(("script", "flag"), sorted(ACCEPT_FLAGS), ids=lambda v: v)
def test_accept_flag_goes_through_the_shared_helper(
    script: str, flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    parser = _parser_for(SCRIPTS_DIR / script, monkeypatch)
    action = next(a for a in parser._actions if flag in a.option_strings)  # noqa: SLF001
    assert _name(action.type) == "override_reason"
    assert action.metavar == "REASON" and action.default is None
