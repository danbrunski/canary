"""R07.2: tests cannot write into the repo's ``data/`` or ``models/``.

Three layers, each pinned here: :func:`src.config.rebase_state_paths` (derived
from the model, so a new ``DaemonConfig`` path is covered without a list to
update), ``tests/configs.py`` (the one way to build an isolated test config
or retrain argv), and the audit-hook guard in ``tests/repo_guard.py``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from src.config import CanaryConfig, DaemonConfig, _daemon_path_fields, rebase_state_paths
from tests import repo_guard
from tests.configs import EXAMPLE_YAML, isolated_config, retrain_argv
from tests.repo_guard import REPO, RepoTreeGuard, RepoTreeWriteError

#: ``data/control/trip_odometer.json`` and ``catalog.db`` were rewritten by a
#: test run on 2026-09-24; both are among these.
PATH_FIELDS = sorted(_daemon_path_fields())


def _under(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def test_rebase_covers_every_path_field_of_the_daemon_config(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    rebased = rebase_state_paths(config, tmp_path)
    assert len(PATH_FIELDS) >= 19
    for name in PATH_FIELDS:
        path = getattr(rebased.daemon, name)
        assert _under(path, tmp_path), name
        # Relative defaults keep their shape, so two fields never collide.
        original = getattr(config.daemon, name)
        if not original.is_absolute():
            assert path == tmp_path / original
    assert rebased.daemon.model_poll_seconds == config.daemon.model_poll_seconds


def test_no_daemon_field_holds_a_path_the_rebase_skips() -> None:
    """Any field whose annotation mentions ``Path`` is in the rebased set --
    judged by the annotation's text, independently of how
    ``_daemon_path_fields`` reads it."""
    mentions = {
        name
        for name, field in DaemonConfig.model_fields.items()
        if "Path" in repr(field.annotation) or "path" in repr(field.annotation).lower()
    }
    assert mentions, "the check itself must see the path fields"
    assert mentions <= set(PATH_FIELDS), sorted(mentions - set(PATH_FIELDS))


def test_path_or_none_fields_are_rebased_and_other_path_shapes_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import create_model

    import src.config as config_module

    optional = create_model(
        "DaemonWithOptional",
        __base__=DaemonConfig,
        extra_log=(Path | None, Path("data/control/extra.jsonl")),
        unset_log=(Path | None, None),
    )
    monkeypatch.setattr(config_module, "DaemonConfig", optional)
    assert {"extra_log", "unset_log"} <= set(config_module._daemon_path_fields())
    base = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = base.model_copy(update={"daemon": optional(**base.daemon.model_dump())})
    rebased = config_module.rebase_state_paths(config, tmp_path)
    assert rebased.daemon.extra_log == tmp_path / "data/control/extra.jsonl"  # type: ignore[attr-defined]
    assert rebased.daemon.unset_log is None  # type: ignore[attr-defined]

    listed = create_model(
        "DaemonWithList", __base__=DaemonConfig, logs=(list[Path], [Path("data/x")])
    )
    monkeypatch.setattr(config_module, "DaemonConfig", listed)
    with pytest.raises(TypeError, match="DaemonConfig.logs"):
        config_module._daemon_path_fields()


def test_rebase_reroots_absolute_paths_and_keeps_named_inputs(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={
            "daemon": config.daemon.model_copy(
                update={"trip_state": Path("/var/lib/canary/trip.json")}
            )
        }
    )
    rebased = rebase_state_paths(config, tmp_path, keep=("behavior_models",))
    assert rebased.daemon.trip_state == tmp_path / "var/lib/canary/trip.json"
    assert rebased.daemon.behavior_models == config.daemon.behavior_models


def test_rebase_refuses_a_keep_name_that_is_not_a_path_field(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    with pytest.raises(ValueError, match="not Path fields"):
        rebase_state_paths(config, tmp_path, keep=("trip_odometer",))
    with pytest.raises(ValueError, match="not Path fields"):
        rebase_state_paths(config, tmp_path, keep=("model_poll_seconds",))


def test_validation_harness_rebases_everything_but_its_declared_inputs(tmp_path: Path) -> None:
    from src.validation.harness import _REAL_TREE_DAEMON_INPUTS, harness_config

    config = harness_config(tmp_path / "scratch", models_enabled=False)
    for name in PATH_FIELDS:
        path = getattr(config.daemon, name)
        if name in _REAL_TREE_DAEMON_INPUTS:
            continue
        assert _under(path, tmp_path / "scratch"), name
    # The hand-kept list this replaced had missed it.
    assert _under(config.daemon.capture_profiles, tmp_path / "scratch")


@pytest.mark.parametrize("base", ["minimal", "daemon", "example"])
def test_isolated_config_has_no_writable_path_in_the_repo(tmp_path: Path, base: str) -> None:
    config = isolated_config(tmp_path, base)  # type: ignore[arg-type]
    for name in PATH_FIELDS:
        assert _under(getattr(config.daemon, name), tmp_path), name
    assert _under(config.collection.storage_path, tmp_path)
    assert _under(config.profiles.registry_path, tmp_path)
    assert _under(config.llm_assist.cards_path, tmp_path)


def test_isolated_config_overrides_apply_after_the_rebase(tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere" / "custom_pids.yaml"
    config = isolated_config(
        tmp_path,
        daemon={"custom_pids": custom},
        models={"isolation_forest": {"enabled": False}},
    )
    assert config.daemon.custom_pids == custom
    assert config.models.isolation_forest.enabled is False
    assert config.models.isolation_forest.n_estimators == 100


def test_retrain_argv_puts_every_output_under_tmp_path(tmp_path: Path) -> None:
    from scripts import retrain

    args = retrain.parse_args(retrain_argv(tmp_path, "--all"))
    for value in (args.if_out, args.sequence_out, args.ledger, args.analyses_dir):
        assert _under(value, tmp_path)
    assert args.no_feedback_policy is True
    with_feedback = retrain.parse_args(retrain_argv(tmp_path, feedback=tmp_path / "fb.jsonl"))
    assert with_feedback.no_feedback_policy is False
    assert with_feedback.feedback == tmp_path / "fb.jsonl"
    # A test naming a flag itself wins.
    own = retrain.parse_args(retrain_argv(tmp_path, "--if-out", str(tmp_path / "mine")))
    assert own.if_out == tmp_path / "mine"


# ---------------------------------------------------------------------------
# the audit-hook guard
# ---------------------------------------------------------------------------

#: A name no real file uses, so a guard failure cannot clobber anything.
_PROBE = REPO / "data" / "control" / "_r07_2_guard_probe.json"


def test_guard_refuses_open_for_write(repo_write_guard: RepoTreeGuard) -> None:
    with repo_write_guard.expect() as refused, pytest.raises(RepoTreeWriteError):
        _PROBE.write_text("{}")
    assert refused and "_r07_2_guard_probe.json" in refused[0]
    assert not _PROBE.exists()


def test_guard_refuses_append_os_open_replace_and_models(
    repo_write_guard: RepoTreeGuard, tmp_path: Path
) -> None:
    staged = tmp_path / "staged.json"
    staged.write_text("{}")
    with repo_write_guard.expect() as refused:
        with pytest.raises(RepoTreeWriteError):
            _PROBE.open("a")
        with pytest.raises(RepoTreeWriteError):
            os.open(REPO / "models" / "_r07_2_probe.bin", os.O_WRONLY | os.O_CREAT)
        with pytest.raises(RepoTreeWriteError):
            os.replace(staged, _PROBE)
    assert len(refused) == 3
    assert staged.exists() and not _PROBE.exists()


def test_guard_refuses_a_sqlite_database_in_the_repo(repo_write_guard: RepoTreeGuard) -> None:
    probe = REPO / "data" / "control" / "_r07_2_probe.db"
    with repo_write_guard.expect() as refused, pytest.raises(RepoTreeWriteError):
        sqlite3.connect(probe)
    assert refused
    assert not probe.exists()


def test_guard_allows_reads_and_refuses_even_a_no_op_mkdir(
    tmp_path: Path, repo_write_guard: RepoTreeGuard
) -> None:
    """``data/`` exists in every checkout, ``data/control`` only in a working
    tree: a mkdir must be refused either way, or a test passes here and fails
    on a clean checkout (2026-09-25, five tests)."""
    assert (REPO / "data" / "dids.yaml").read_text()
    with repo_write_guard.expect() as refused:
        # Path.mkdir(exist_ok=True) swallows the refusal (any OSError on an
        # existing dir); the guard's record is what fails the test.
        (REPO / "data").mkdir(exist_ok=True)
    assert refused and "os.mkdir" in refused[0]
    (tmp_path / "data" / "control").mkdir(parents=True)
    (tmp_path / "data" / "control" / "x.json").write_text("{}")


def test_guard_records_a_write_the_code_under_test_swallowed(
    repo_write_guard: RepoTreeGuard,
) -> None:
    """``retrain._ingest_ledger`` catches ``OSError``; the test still fails."""
    nodeid = "tests/test_repo_tree_isolation.py::fake"
    previous = repo_write_guard.current_test
    repo_write_guard.current_test = nodeid
    try:
        try:
            _PROBE.write_text("{}")
        except OSError:
            pass
    finally:
        repo_write_guard.current_test = previous
    assert repo_write_guard.violations.pop(nodeid)


_SWALLOWED_IN_TEARDOWN = """
import pytest

PROBE = {probe!r}


@pytest.fixture(scope="module")
def leaky():
    yield
    try:  # a module fixture's teardown, after the last test's own check
        open(PROBE, "w").write("x")
    except OSError:
        pass


def test_uses_it(leaky):
    pass
"""


@pytest.mark.parametrize("workers", ["0", "2"])
def test_a_refusal_outside_any_test_phase_fails_the_run(tmp_path: Path, workers: str) -> None:
    """A refused write in module teardown, swallowed by the code, fails the
    run -- under xdist too, where the worker hands it to the controller."""
    import subprocess
    import sys

    probe = REPO / "data" / "control" / "_r07_2_teardown_probe.json"
    (tmp_path / "test_leak.py").write_text(_SWALLOWED_IN_TEARDOWN.format(probe=str(probe)))
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n",
            workers,
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            str(tmp_path / "test_leak.py"),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "_r07_2_teardown_probe.json" in output and "refused" in output, output
    assert not probe.exists()


def test_only_changes_the_run_alone_could_make_fail_the_end_of_run_diff() -> None:
    start = repo_guard.RunBaseline(files={}, tracked_clean=frozenset({"data/dids.yaml"}))
    assert repo_guard._attributable("data/control/catalog.db", start)
    assert repo_guard._attributable("data/dids.yaml", start)
    # Already dirty at the start (another session editing it), or new and
    # outside data/control (another session's analysis output): a warning.
    assert not repo_guard._attributable("data/nissan-xterra-2008/kline.yaml", start)
    assert not repo_guard._attributable("data/analyses/2026-09-25-note.md", start)


def test_isolate_keeps_the_yamls_tmp_paths_and_moves_every_repo_default(tmp_path: Path) -> None:
    """The per-file YAML shape: four tmp_path dirs named, the rest left at
    repo defaults (``trip_state`` among them, the 2026-09-24 leak)."""
    from tests.configs import isolate

    base = CanaryConfig.from_yaml(EXAMPLE_YAML)
    named = tmp_path / "anomalies"
    config = base.model_copy(
        update={"daemon": base.daemon.model_copy(update={"anomalies_dir": named})}
    )
    isolated = isolate(config, tmp_path)
    assert isolated.daemon.anomalies_dir == named
    for name in PATH_FIELDS:
        assert _under(getattr(isolated.daemon, name), tmp_path), name
    assert _under(isolated.profiles.registry_path, tmp_path)
    assert _under(isolated.llm_assist.cards_path, tmp_path)
