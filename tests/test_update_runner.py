"""The root runner behind ``canary-update.service`` (phone-ui P3.1/P3.4).

Every test drives :func:`src.control.update_runner.run_once` with a fake
:class:`Commands`: no git, no systemctl, no ``canary-update``. The real script
under the real runner is covered in ``tests/test_update_canary_script.py``.
"""

from __future__ import annotations

import configparser
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from src.control.update_runner import (
    CommandResult,
    StatusReporter,
    git_argv,
    git_env,
    parse_phase,
    rollback_reason,
    run_once,
    stream_process,
)
from src.control.updater import (
    DEFAULT_UPDATE_PATHS,
    UpdatePaths,
    new_request,
    read_status,
    utc_iso,
    write_request,
    write_status,
)

REPO = Path(__file__).resolve().parents[1]
OK = CommandResult(0, "", "")
SUMMARY = [
    "",
    "=== canary-update summary ===",
    "source: aaa1111 -> bbb2222",
    "dashboard build: skipped (no dashboard source change; dist/ is current)",
    "restart: ok",
    "health: ok after 24 s",
    "rollback: none",
    "result: updated (exit 0)",
    "total: 61 s",
]


@dataclass
class FakeCommands:
    """git answers keyed by the leading arguments; ``canary-update`` prints
    ``lines`` and exits ``code``."""

    answers: dict[tuple[str, ...], CommandResult] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    code: int = 0
    heads: list[str] = field(default_factory=lambda: ["aaa1111", "bbb2222"])
    git_calls: list[tuple[str, ...]] = field(default_factory=list)
    updater_calls: list[list[str]] = field(default_factory=list)
    ticks: int = 0

    def git(self, *args: str, timeout: float = 15.0) -> CommandResult:
        self.git_calls.append(args)
        if args == ("rev-parse", "--short", "HEAD"):
            head = self.heads[0] if len(self.heads) == 1 else self.heads.pop(0)
            return CommandResult(0, head + "\n", "")
        for prefix, answer in self.answers.items():
            if args[: len(prefix)] == prefix:
                return answer
        return OK

    def run_updater(
        self,
        args: Sequence[str],
        on_line: Callable[[str], None],
        on_tick: Callable[[], None],
    ) -> int:
        self.updater_calls.append(list(args))
        for _ in range(self.ticks):
            on_tick()
        for line in self.lines:
            on_line(line + "\n")
        return self.code

    def called(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == prefix for call in self.git_calls)


def _upstream_answers(**extra: CommandResult) -> dict[tuple[str, ...], CommandResult]:
    answers = {
        ("rev-parse", "--abbrev-ref"): CommandResult(0, "origin/main\n", ""),
        ("rev-parse", "--short", "@{u}"): CommandResult(0, "ccc3333\n", ""),
        ("rev-list", "--count", "HEAD..@{u}"): CommandResult(0, "2\n", ""),
        ("rev-list", "--count", "@{u}..HEAD"): CommandResult(0, "0\n", ""),
        ("log",): CommandResult(0, "ccc3333\x1ffeat: phone update\nbbb2222\x1ffix: a bug\n", ""),
    }
    for key, value in extra.items():
        answers[tuple(key.split(" "))] = value
    return answers


@pytest.fixture
def paths(tmp_path: Path) -> UpdatePaths:
    return UpdatePaths(
        request=tmp_path / "update_request",
        taken=tmp_path / "update_request.taken",
        status=tmp_path / "update_status",
        trigger_unit=tmp_path / "canary-update.path",
        proc_net=tmp_path / "net",
    )


def _serve(
    paths: UpdatePaths,
    commands: FakeCommands,
    action: str = "check",
    *,
    route: bool | None = True,
    full: bool = False,
) -> dict[str, Any]:
    write_request(new_request(action, full=full), paths.taken)
    assert run_once(paths, commands, route=lambda: route) == 0
    assert not paths.taken.exists(), "every request is consumed"
    return read_status(paths.status)


# -- taking the request ------------------------------------------------------


def test_no_request_writes_nothing(paths: UpdatePaths) -> None:
    assert run_once(paths, FakeCommands()) == 0
    assert not paths.status.exists()


def test_an_invalid_request_is_refused_and_consumed(paths: UpdatePaths) -> None:
    paths.taken.write_text('{"action": "rm -rf /"}')
    commands = FakeCommands()

    assert run_once(paths, commands) == 0

    status = read_status(paths.status)
    assert status["state"] == "refused"
    assert status["reason"] == "invalid_request"
    assert "action must be one of" in status["message"]
    assert not paths.taken.exists()
    assert commands.git_calls == [] and commands.updater_calls == []


def test_a_symlinked_request_is_not_followed(paths: UpdatePaths, tmp_path: Path) -> None:
    target = tmp_path / "shadow"
    target.write_text(json.dumps(new_request("update").to_dict()))
    paths.taken.symlink_to(target)
    commands = FakeCommands()

    assert run_once(paths, commands) == 0

    assert read_status(paths.status)["reason"] == "invalid_request"
    assert target.exists() and not paths.taken.is_symlink()
    assert commands.updater_calls == []


# -- check -------------------------------------------------------------------


def test_check_reports_commits_behind_with_subjects(paths: UpdatePaths) -> None:
    commands = FakeCommands(answers=_upstream_answers(), heads=["aaa1111"])

    status = _serve(paths, commands)

    assert status["state"] == "done"
    assert status["outcome"] == "checked"
    assert status["message"] == "2 commits available: aaa1111 -> ccc3333."
    check = status["last_check"]
    assert check["ok"] is True
    assert (check["remote"], check["branch"], check["current"], check["upstream"]) == (
        "origin",
        "main",
        "aaa1111",
        "ccc3333",
    )
    assert check["behind"] == 2 and check["ahead"] == 0
    assert check["commits"] == [
        {"sha": "ccc3333", "subject": "feat: phone update"},
        {"sha": "bbb2222", "subject": "fix: a bug"},
    ]
    assert check["dirty"] is False
    assert commands.called("fetch", "--quiet", "origin")
    assert commands.updater_calls == [], "a check never runs canary-update"


def test_check_names_a_dirty_checkout(paths: UpdatePaths) -> None:
    answers = _upstream_answers(status=CommandResult(0, " M src/main.py\n", ""))
    status = _serve(paths, FakeCommands(answers=answers))

    assert status["last_check"]["dirty"] is True
    assert status["last_check"]["dirty_files"] == ["src/main.py"]


def test_check_uses_update_canary_shs_dirty_pathspec(paths: UpdatePaths) -> None:
    commands = FakeCommands(answers=_upstream_answers())
    _serve(paths, commands)
    assert ("status", "--porcelain", "--untracked-files=no", "--", ".", ":(exclude)models") in (
        commands.git_calls
    )


def test_check_without_a_default_route_says_no_internet_without_fetching(
    paths: UpdatePaths,
) -> None:
    commands = FakeCommands(answers=_upstream_answers())

    status = _serve(paths, commands, route=False)

    assert status["state"] == "failed"
    assert status["reason"] == "no_internet"
    assert status["message"].startswith("No internet: the Pi has no default route")
    assert status["last_check"]["ok"] is False
    assert not commands.called("fetch")


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (
            CommandResult(128, "", "fatal: unable to access: Could not resolve host: github.com"),
            "no_internet",
        ),
        (CommandResult(124, "", "timed out after 60 s"), "no_internet"),
        (
            CommandResult(128, "", "git@github.com: Permission denied (publickey)."),
            "remote_refused",
        ),
    ],
)
def test_check_fetch_failures_say_which_kind(
    paths: UpdatePaths, result: CommandResult, reason: str
) -> None:
    status = _serve(paths, FakeCommands(answers=_upstream_answers(fetch=result)))

    assert status["state"] == "failed"
    assert status["last_check"]["reason"] == reason
    if reason == "no_internet":
        assert status["message"].startswith("No internet:")


def test_check_without_an_upstream_says_so(paths: UpdatePaths) -> None:
    answers = {("rev-parse", "--abbrev-ref"): CommandResult(128, "", "no upstream")}
    status = _serve(paths, FakeCommands(answers=answers))
    assert status["last_check"]["reason"] == "no_upstream"


# -- update ------------------------------------------------------------------


def test_update_relays_phases_and_reports_the_new_commit(paths: UpdatePaths) -> None:
    write_status({"state": "done", "last_check": {"ok": True, "behind": 1}}, paths.status)
    commands = FakeCommands(
        answers=_upstream_answers(),
        lines=[
            "=== Checking whether CANary can restart ===",
            "restart safe: no capture or ECU backup is active",
            "=== Fetching CANary update ===",
            "=== Restarting services ===",
            *SUMMARY,
        ],
    )

    status = _serve(paths, commands, "update")

    assert status["state"] == "done"
    assert status["outcome"] == "updated"
    assert status["message"] == "Updated to bbb2222."
    assert (status["before"], status["after"], status["target"]) == (
        "aaa1111",
        "bbb2222",
        "bbb2222",
    )
    assert status["exit_code"] == 0
    assert status["exit_meaning"] == "updated (or already current) and healthy"
    assert [p["name"] for p in status["phases"]] == [
        "Checking the internet connection",
        "Checking whether CANary can restart",
        "Fetching CANary update",
        "Restarting services",
        "Summary",
    ]
    assert status["summary"]["health"] == "ok after 24 s"
    assert status["last_check"] is None, "the check described the commit that was replaced"
    assert commands.called("ls-remote", "--exit-code", "origin", "HEAD")
    assert commands.updater_calls == [[]]


def test_full_is_passed_through(paths: UpdatePaths) -> None:
    commands = FakeCommands(answers=_upstream_answers(), lines=SUMMARY)
    _serve(paths, commands, "update", full=True)
    assert commands.updater_calls == [["--full"]]


def test_update_without_a_route_refuses_before_starting(paths: UpdatePaths) -> None:
    commands = FakeCommands(answers=_upstream_answers())

    status = _serve(paths, commands, "update", route=False)

    assert status["state"] == "refused"
    assert status["reason"] == "no_internet"
    assert status["message"].endswith("Nothing was started.")
    assert commands.updater_calls == []
    assert not commands.called("ls-remote")


def test_update_with_an_unreachable_remote_refuses_before_starting(paths: UpdatePaths) -> None:
    probe = CommandResult(
        128, "", "ssh: connect to host github.com port 22: Network is unreachable"
    )
    commands = FakeCommands(answers=_upstream_answers(**{"ls-remote": probe}))

    status = _serve(paths, commands, "update")

    assert status["state"] == "refused"
    assert status["reason"] == "no_internet"
    assert status["message"].startswith("No internet: git ls-remote could not reach")
    assert commands.updater_calls == []


@pytest.mark.parametrize(
    ("code", "state", "outcome", "message"),
    [
        (3, "refused", "refused", "Refused: a capture or ECU backup is active"),
        (4, "refused", "refused", "Refused: the checkout has tracked local changes"),
        (5, "failed", "fetch_failed", "Fetch failed; nothing changed (still aaa1111)."),
        (6, "failed", "rolled_back", "Rolled back: health check FAILED (no answer"),
        (7, "failed", "rollback_failed", "Update failed and the rollback did not come up"),
        (8, "failed", "left_broken", "Update failed and was left in place"),
        (1, "failed", "failed", "canary-update failed: setup error"),
    ],
)
def test_every_exit_code_has_its_outcome(
    paths: UpdatePaths, code: int, state: str, outcome: str, message: str
) -> None:
    lines = [
        "=== canary-update summary ===",
        "source: aaa1111 -> bbb2222",
        "restart: ok",
        "health: FAILED (no answer from http://localhost:8000/status within 90 s)",
        f"result: x (exit {code})",
    ]
    commands = FakeCommands(answers=_upstream_answers(), lines=lines, code=code, heads=["aaa1111"])

    status = _serve(paths, commands, "update")

    assert (status["state"], status["outcome"], status["exit_code"]) == (state, outcome, code)
    assert status["message"].startswith(message), status["message"]
    assert status["after"] == "aaa1111"


def test_a_refused_update_keeps_the_last_check(paths: UpdatePaths) -> None:
    write_status({"state": "done", "last_check": {"ok": True, "behind": 3}}, paths.status)
    commands = FakeCommands(answers=_upstream_answers(), lines=[], code=4, heads=["aaa1111"])

    status = _serve(paths, commands, "update")

    assert status["last_check"] == {"ok": True, "behind": 3}


def test_the_heartbeat_keeps_a_quiet_update_from_going_lost(paths: UpdatePaths) -> None:
    clock = [1_800_000_000.0]
    request = new_request("update", now=clock[0])
    reporter = StatusReporter(paths.status, request, clock=lambda: clock[0])
    reporter.write()
    clock[0] += 170
    reporter.tick()
    clock[0] += 170

    status = read_status(paths.status, now=clock[0])

    assert status["state"] == "updating"
    assert status["updated_at"] == utc_iso(clock[0] - 170)


def test_a_runner_crash_is_reported_not_left_updating(paths: UpdatePaths) -> None:
    class Broken(FakeCommands):
        def run_updater(self, *args: Any, **kwargs: Any) -> int:
            raise RuntimeError("pipe closed")

    write_request(new_request("update"), paths.taken)
    status_code = run_once(paths, Broken(answers=_upstream_answers()), route=lambda: True)

    status = read_status(paths.status)
    assert status_code == 1
    assert status["state"] == "failed"
    assert status["reason"] == "runner_error"
    assert "RuntimeError: pipe closed" in status["message"]


def test_rollback_reason_reads_the_summary() -> None:
    assert rollback_reason({"restart": "FAILED (exit 1)"}) == "the restart failed (FAILED (exit 1))"
    assert rollback_reason({"restart": "not attempted (deploy step failed, exit 2)"}).startswith(
        "a deploy step failed"
    )
    assert rollback_reason({"restart": "ok", "health": "FAILED (x)"}) == "health check FAILED (x)"


def test_parse_phase() -> None:
    assert parse_phase("=== Fetching CANary update ===\n") == "Fetching CANary update"
    assert parse_phase("source: a -> b") is None


# -- the real system, without the system ------------------------------------


def test_git_runs_as_the_checkout_owner_only_under_root(tmp_path: Path) -> None:
    assert git_argv(tmp_path, "canary", ["fetch"], euid=0) == [
        "runuser",
        "-u",
        "canary",
        "--",
        "git",
        "-C",
        str(tmp_path),
        "fetch",
    ]
    assert git_argv(tmp_path, "canary", ["fetch"], euid=1000)[0] == "git"
    assert git_argv(tmp_path, "root", ["fetch"], euid=0)[0] == "git"


def test_git_env_trusts_the_checkout_and_never_prompts(tmp_path: Path) -> None:
    env = git_env({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b"}, tmp_path)
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("safe.directory", str(tmp_path))
    assert env["GIT_CONFIG_KEY_0"] == "a.b"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_stream_process_relays_lines_ticks_and_the_exit_code(tmp_path: Path) -> None:
    script = tmp_path / "fake-update"
    script.write_text("#!/bin/sh\necho one\nsleep 0.3\necho two >&2\nexit 6\n")
    script.chmod(0o755)
    lines: list[str] = []
    ticks: list[int] = []

    code = stream_process(
        [str(script)],
        env={"PATH": "/usr/bin:/bin"},
        on_line=lines.append,
        on_tick=lambda: ticks.append(1),
        tick_s=0.05,
    )

    assert code == 6
    assert lines == ["one\n", "two\n"]
    assert ticks, "a quiet stretch heartbeats"


# -- the units match the contract --------------------------------------------


def _unit(name: str) -> configparser.ConfigParser:
    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.read(REPO / "systemd" / name)
    return unit


def test_the_units_watch_and_take_the_contracts_paths() -> None:
    path_unit = _unit("canary-update.path")
    service = _unit("canary-update.service")

    assert path_unit["Path"]["PathExists"] == str(DEFAULT_UPDATE_PATHS.request)
    assert path_unit["Path"]["Unit"] == "canary-update.service"
    assert service["Service"]["Type"] == "oneshot"
    assert service["Service"]["ExecStartPre"] == (
        f"-/bin/mv -fT {DEFAULT_UPDATE_PATHS.request} {DEFAULT_UPDATE_PATHS.taken}"
    )
    assert "-m src.control.update_runner" in service["Service"]["ExecStart"]
    assert "User" not in service["Service"], "canary-update needs root"
    assert DEFAULT_UPDATE_PATHS.trigger_unit.name == "canary-update.path"
