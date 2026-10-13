"""The root entrypoint behind ``canary-update.service`` (phone-ui P3.1).

``canary-update.path`` starts the oneshot when the daemon files
``/run/canary/update_request``; the unit's ``ExecStartPre`` renames it to
``update_request.taken`` first, so a request that crashes this module is
already consumed and cannot loop. This module then:

- reads and validates the taken request (:mod:`src.control.updater`), and
  deletes it;
- for ``check``: confirms a default route, runs ``git fetch``, and records the
  current and upstream commits, the commits behind with their subjects, and
  whether the checkout is dirty;
- for ``update``: confirms a default route and that the remote answers
  (``git ls-remote``, seconds), then runs ``canary-update``
  (``scripts/update_canary.sh``) and relays its ``=== <step> ===`` lines and
  summary block into the status file;
- writes ``/run/canary/update_status`` throughout, with a heartbeat so a
  runner that dies is reported ``lost`` rather than "updating" forever.

It runs on the checkout's venv, like ``canary-provision.service``: the daemon
that files the request runs on the same venv, so the venv being broken already
means there is no request to serve, and this module shares the contract module
with the daemon instead of duplicating it. Everything is imported at start, so
a ``pip install`` or rollback that rewrites the venv or the checkout mid-run
does not change the code that is running.

git runs as the checkout's owner (``runuser``), never as root: a root
``git fetch`` would leave root-owned objects in a checkout the ``canary`` user
updates. ``canary-update`` itself runs as root, as from a shell; it is given
``safe.directory`` for the checkout, because git refuses a repository owned by
another user and a systemd unit has no ``SUDO_UID`` to excuse it.
"""

from __future__ import annotations

import contextlib
import os
import pwd
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

from src.control.updater import (
    DEFAULT_UPDATE_PATHS,
    HEARTBEAT_S,
    STATUS_SCHEMA,
    UpdatePaths,
    UpdateRequest,
    UpdateRequestError,
    exit_meaning,
    has_default_route,
    parse_request,
    read_regular_file,
    read_status,
    utc_iso,
    write_status,
)

#: ``git fetch`` for a check. A default route that leads nowhere fails here.
FETCH_TIMEOUT_S = 60.0
#: ``git ls-remote`` before an update: long enough for a slow link, short
#: enough that "no internet" arrives before anything starts.
PROBE_TIMEOUT_S = 20.0
GIT_TIMEOUT_S = 15.0
#: Commit subjects listed by a check; the count is reported in full.
MAX_COMMITS = 50
LOG_TAIL_LINES = 40
LOG_LINE_CHARS = 300
#: Status rewrites while output streams in, at most this often.
WRITE_INTERVAL_S = 1.0
DEFAULT_UPDATER = "/usr/local/sbin/canary-update"
NO_INTERNET = "no_internet"

#: git's own words for "the network is not there", as opposed to "the remote
#: answered and said no" (credentials, a missing branch).
_NETWORK_ERROR = re.compile(
    r"could not resolve host|temporary failure in name resolution|network is unreachable"
    r"|connection timed out|operation timed out|timed out after|failed to connect"
    r"|connection refused|no route to host|could not resolve hostname"
    r"|name or service not known",
    re.IGNORECASE,
)
_PHASE = re.compile(r"^=== (?P<name>.+) ===$")
SUMMARY_HEADER = "canary-update summary"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Commands(Protocol):
    """The two things the runner does to the system; tests pass a fake."""

    def git(self, *args: str, timeout: float = GIT_TIMEOUT_S) -> CommandResult: ...

    def run_updater(
        self,
        args: Sequence[str],
        on_line: Callable[[str], None],
        on_tick: Callable[[], None],
    ) -> int: ...


# ---------------------------------------------------------------------------
# canary-update's output: the phase markers and the summary block
# ---------------------------------------------------------------------------


def parse_phase(line: str) -> str | None:
    """The step name of an ``=== <step> ===`` line from ``update_canary.sh``
    (its ``step()`` and the summary header), else ``None``."""
    match = _PHASE.match(line.strip())
    return match.group("name") if match else None


def parse_summary_line(line: str) -> tuple[str, str] | None:
    """``key: value`` from a summary-block line."""
    key, sep, value = line.strip().partition(": ")
    if not sep or not key:
        return None
    return key, value


def rollback_reason(summary: Mapping[str, str]) -> str:
    """Why the new code was rolled back, from the summary block."""
    restart = summary.get("restart", "")
    if restart.startswith("not attempted"):
        return f"a deploy step failed before the restart ({restart})"
    if restart.startswith("FAILED"):
        return f"the restart failed ({restart})"
    health = summary.get("health", "")
    if health:
        return f"health check {health}"
    return "the new code did not come up"


# ---------------------------------------------------------------------------
# The status reporter
# ---------------------------------------------------------------------------


class StatusReporter:
    """Builds the status dict for one request and writes it.

    Each change replaces the dict rather than mutating it, and every write is
    the whole file (:func:`src.control.updater.write_status`).
    """

    def __init__(
        self,
        path: Path,
        request: UpdateRequest | None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._clock = clock
        previous = read_status(path)
        now = clock()
        self._status: dict[str, Any] = {
            "schema": STATUS_SCHEMA,
            "state": "checking" if request and request.action == "check" else "updating",
            "action": request.action if request else None,
            "request_id": request.request_id if request else None,
            "requested_at": request.requested_at if request else None,
            "options": {"full": request.full} if request else {},
            "started_at": utc_iso(now),
            "updated_at": utc_iso(now),
            "finished_at": None,
            "phase": None,
            "phases": [],
            "before": None,
            "after": None,
            "target": None,
            "exit_code": None,
            "exit_meaning": None,
            "outcome": None,
            "reason": None,
            "message": None,
            "summary": {},
            "log_tail": [],
            # A check's result outlives the updates after it, until replaced.
            "last_check": previous.get("last_check"),
        }
        self._log: deque[str] = deque(maxlen=LOG_TAIL_LINES)
        self._summary: dict[str, str] = {}
        self._in_summary = False
        self._last_write = 0.0

    @property
    def status(self) -> dict[str, Any]:
        return dict(self._status)

    @property
    def summary(self) -> dict[str, str]:
        return dict(self._summary)

    def update(self, **changes: Any) -> None:
        self._status = {**self._status, **changes}
        self.write()

    def write(self) -> None:
        now = self._clock()
        self._status = {
            **self._status,
            "updated_at": utc_iso(now),
            "log_tail": list(self._log),
            "summary": dict(self._summary),
        }
        try:
            write_status(self._status, self._path)
        except OSError as exc:
            # journald still has every line; the UI loses progress, not the update.
            print(f"canary-update-runner: cannot write {self._path}: {exc}", file=sys.stderr)
        self._last_write = now

    def phase(self, name: str) -> None:
        stamp = utc_iso(self._clock())
        phases = [*self._status["phases"], {"name": name, "at": stamp}]
        self.update(phase=name, phases=phases)

    def line(self, raw: str) -> None:
        """One output line from ``canary-update``."""
        text = raw.rstrip("\n")
        print(text, flush=True)  # journalctl -u canary-update keeps the full log
        if text.strip():
            self._log.append(text[:LOG_LINE_CHARS])
        name = parse_phase(text)
        if name == SUMMARY_HEADER:
            self._in_summary = True
            self.phase("Summary")
            return
        if name is not None and not self._in_summary:
            self.phase(name)
            return
        if self._in_summary:
            pair = parse_summary_line(text)
            if pair is not None:
                self._summary = {**self._summary, pair[0]: pair[1]}
        if self._clock() - self._last_write >= WRITE_INTERVAL_S:
            self.write()

    def tick(self) -> None:
        """Heartbeat while nothing is printed (a long ``pnpm install``)."""
        self.write()

    def finish(self, state: str, **changes: Any) -> None:
        self.update(state=state, finished_at=utc_iso(self._clock()), **changes)


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------


def _failure(reason: str, message: str, *, checked_at: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "message": message, "checked_at": checked_at}


def _network_failure(result: CommandResult, *, what: str) -> tuple[str, str]:
    """``(reason, message)`` for a failed ``git fetch``/``ls-remote``."""
    detail = (result.stderr or result.stdout).strip().splitlines()
    last = detail[-1] if detail else f"exit {result.returncode}"
    if _NETWORK_ERROR.search(result.stderr or "") or result.returncode == 124:
        return NO_INTERNET, f"No internet: {what} could not reach the git remote ({last})."
    return "remote_refused", f"{what} failed: {last}"


def upstream_of(commands: Commands) -> tuple[str, str] | None:
    """``(remote, branch)`` the checkout's branch tracks, or ``None``."""
    result = commands.git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    remote, sep, branch = result.stdout.strip().partition("/")
    if not result.ok or not sep:
        return None
    return remote, branch


def run_check(
    commands: Commands, *, route: bool | None, clock: Callable[[], float]
) -> dict[str, Any]:
    """Fetch and compare. Returns the ``last_check`` payload."""
    checked_at = utc_iso(clock())
    if route is False:
        return _failure(
            NO_INTERNET,
            "No internet: the Pi has no default route. Join a WiFi network with internet "
            "access, then check again.",
            checked_at=checked_at,
        )
    upstream = upstream_of(commands)
    if upstream is None:
        return _failure(
            "no_upstream",
            "The checkout's branch has no upstream to update from (git branch -u).",
            checked_at=checked_at,
        )
    remote, branch = upstream
    fetched = commands.git("fetch", "--quiet", remote, timeout=FETCH_TIMEOUT_S)
    if not fetched.ok:
        reason, message = _network_failure(fetched, what="git fetch")
        return _failure(reason, message, checked_at=checked_at)
    current = commands.git("rev-parse", "--short", "HEAD").stdout.strip()
    target = commands.git("rev-parse", "--short", "@{u}").stdout.strip()
    behind = _count(commands.git("rev-list", "--count", "HEAD..@{u}"))
    ahead = _count(commands.git("rev-list", "--count", "@{u}..HEAD"))
    log = commands.git("log", f"-n{MAX_COMMITS}", "--format=%h%x1f%s", "HEAD..@{u}")
    commits = [
        {"sha": sha, "subject": subject}
        for sha, _, subject in (row.partition("\x1f") for row in log.stdout.splitlines() if row)
    ]
    # The same pathspec update_canary.sh refuses on: tracked changes outside models/.
    dirty = commands.git(
        "status", "--porcelain", "--untracked-files=no", "--", ".", ":(exclude)models"
    )
    dirty_files = [row[3:] for row in dirty.stdout.splitlines() if row.strip()]
    return {
        "ok": True,
        "reason": None,
        "message": None,
        "checked_at": checked_at,
        "remote": remote,
        "branch": branch,
        "current": current,
        "upstream": target,
        "behind": behind,
        "ahead": ahead,
        "commits": commits,
        "commits_truncated": behind > len(commits),
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files[:20],
    }


def _count(result: CommandResult) -> int:
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def _check_message(check: Mapping[str, Any]) -> str:
    if not check.get("ok"):
        return str(check.get("message"))
    behind = int(check.get("behind") or 0)
    if behind == 0:
        return f"Up to date at {check.get('current')}."
    plural = "s" if behind != 1 else ""
    return f"{behind} commit{plural} available: {check.get('current')} -> {check.get('upstream')}."


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def _update_outcome(
    code: int, *, before: str | None, after: str | None, summary: Mapping[str, str]
) -> tuple[str, str, str]:
    """``(state, outcome, message)`` for ``canary-update``'s exit code."""
    meaning = exit_meaning(code) or ""
    if code == 0:
        if before and after and before != after:
            return "done", "updated", f"Updated to {after}."
        return "done", "already_current", f"Already at {after or before}; CANary restarted."
    if code in (3, 4):
        return "refused", "refused", f"Refused: {meaning.removeprefix('refused: ')}."
    if code == 5:
        return "failed", "fetch_failed", f"Fetch failed; nothing changed (still {before})."
    if code == 6:
        return (
            "failed",
            "rolled_back",
            f"Rolled back: {rollback_reason(summary)}. Back on {before}, healthy.",
        )
    if code == 7:
        return (
            "failed",
            "rollback_failed",
            f"Update failed and the rollback did not come up ({rollback_reason(summary)}). "
            "Inspect: sudo journalctl -u canary -n 100 --no-pager",
        )
    if code == 8:
        return "failed", "left_broken", f"Update failed and was left in place: {meaning}."
    return "failed", "failed", f"canary-update failed: {meaning}."


def run_update(
    request: UpdateRequest,
    commands: Commands,
    reporter: StatusReporter,
    *,
    route: bool | None,
) -> None:
    reporter.phase("Checking the internet connection")
    if route is False:
        reporter.finish(
            "refused",
            outcome="refused",
            reason=NO_INTERNET,
            message="No internet: the Pi has no default route. Nothing was started.",
        )
        return
    upstream = upstream_of(commands)
    remote = upstream[0] if upstream else "origin"
    probe = commands.git("ls-remote", "--exit-code", remote, "HEAD", timeout=PROBE_TIMEOUT_S)
    # 2: the remote answered but has no HEAD ref -- reachable all the same.
    if probe.returncode not in (0, 2):
        reason, message = _network_failure(probe, what="git ls-remote")
        reporter.finish(
            "refused", outcome="refused", reason=reason, message=f"{message} Nothing was started."
        )
        return
    before = commands.git("rev-parse", "--short", "HEAD").stdout.strip() or None
    reporter.update(before=before)
    args = ["--full"] if request.full else []
    code = commands.run_updater(args, reporter.line, reporter.tick)
    summary = reporter.summary
    # `after` is what is installed now (a rollback puts <before> back);
    # `target` is the commit the run fetched and tried, from `source:`.
    after = commands.git("rev-parse", "--short", "HEAD").stdout.strip() or None
    source_before, _, source_target = summary.get("source", "").partition(" -> ")
    before = source_before or before
    state, outcome, message = _update_outcome(code, before=before, after=after, summary=summary)
    changes: dict[str, Any] = {
        "before": before,
        "after": after,
        "target": source_target or None,
        "exit_code": code,
        "exit_meaning": exit_meaning(code),
        "outcome": outcome,
        "reason": outcome if state != "done" else None,
        "message": message,
    }
    if outcome == "updated":
        # The check described the commit this replaced.
        changes["last_check"] = None
    reporter.finish(state, **changes)


# ---------------------------------------------------------------------------
# One run of the unit
# ---------------------------------------------------------------------------


def take_request(path: Path) -> UpdateRequest | UpdateRequestError | None:
    """Read and delete the taken request. ``None`` when there is none (the
    unit was started by hand)."""
    try:
        raw = read_regular_file(path)
    except FileNotFoundError:
        return None
    except UpdateRequestError as exc:
        _unlink(path)
        return exc
    _unlink(path)
    try:
        return parse_request(raw)
    except UpdateRequestError as exc:
        return exc


def _unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def run_once(
    paths: UpdatePaths,
    commands: Commands,
    *,
    clock: Callable[[], float] = time.time,
    route: Callable[[], bool | None] | None = None,
) -> int:
    """Serve the taken request, if any. Returns the process exit code: 0 for
    every outcome the status file describes, so a refusal is not a failed
    unit; 1 only when the runner itself broke."""
    taken = take_request(paths.taken)
    if taken is None:
        print("canary-update-runner: no request to serve", file=sys.stderr)
        return 0
    if isinstance(taken, UpdateRequestError):
        reporter = StatusReporter(paths.status, None, clock=clock)
        reporter.finish(
            "refused",
            outcome="refused",
            reason="invalid_request",
            message=f"Ignored an invalid request: {taken}",
        )
        return 0
    reporter = StatusReporter(paths.status, taken, clock=clock)
    reporter.write()
    default_route = (route or (lambda: has_default_route(paths.proc_net)))()
    try:
        if taken.action == "check":
            reporter.phase("Fetching from the git remote")
            check = run_check(commands, route=default_route, clock=clock)
            reporter.finish(
                "done" if check["ok"] else "failed",
                outcome="checked" if check["ok"] else check["reason"],
                reason=None if check["ok"] else check["reason"],
                message=_check_message(check),
                last_check=check,
            )
        else:
            run_update(taken, commands, reporter, route=default_route)
    except BaseException as exc:  # noqa: BLE001 - reported in the status, then re-raised
        reporter.finish(
            "failed",
            outcome="failed",
            reason="runner_error",
            message=f"The update runner stopped: {type(exc).__name__}: {exc}",
        )
        if isinstance(exc, Exception):
            return 1
        raise
    return 0


# ---------------------------------------------------------------------------
# The real system
# ---------------------------------------------------------------------------


def git_env(base: Mapping[str, str], checkout: Path) -> dict[str, str]:
    """``base`` plus what every git under the unit needs: no prompts, the C
    locale (the network-error match reads git's English), and the checkout as
    a ``safe.directory`` (command scope, which git honours for it)."""
    env = dict(base)
    count = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    env[f"GIT_CONFIG_KEY_{count}"] = "safe.directory"
    env[f"GIT_CONFIG_VALUE_{count}"] = str(checkout)
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    return env


def git_argv(checkout: Path, owner: str, args: Sequence[str], *, euid: int) -> list[str]:
    """``git -C <checkout> <args>``, as ``owner`` when running as root."""
    argv = ["git", "-C", str(checkout), *args]
    if euid == 0 and owner != "root":
        return ["runuser", "-u", owner, "--", *argv]
    return argv


def checkout_owner(checkout: Path) -> str:
    uid = checkout.stat().st_uid
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


@dataclass(frozen=True)
class SystemCommands:
    checkout: Path
    owner: str
    updater: str = DEFAULT_UPDATER

    def git(self, *args: str, timeout: float = GIT_TIMEOUT_S) -> CommandResult:
        argv = git_argv(self.checkout, self.owner, args, euid=os.geteuid())
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                env=git_env(os.environ, self.checkout),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", f"timed out after {timeout:.0f} s")
        except OSError as exc:
            return CommandResult(127, "", str(exc))
        return CommandResult(done.returncode, done.stdout, done.stderr)

    def run_updater(
        self,
        args: Sequence[str],
        on_line: Callable[[str], None],
        on_tick: Callable[[], None],
    ) -> int:
        return stream_process(
            [self.updater, *args],
            env=git_env({**os.environ, "CANARY_HOME": str(self.checkout)}, self.checkout),
            on_line=on_line,
            on_tick=on_tick,
        )


def stream_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    on_line: Callable[[str], None],
    on_tick: Callable[[], None],
    tick_s: float = HEARTBEAT_S,
) -> int:
    """Run ``argv``, handing each output line (stdout and stderr merged) to
    ``on_line`` and calling ``on_tick`` whenever ``tick_s`` passes without one."""
    process = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        bufsize=1,
        env=dict(env),
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=pump, name="canary-update-output", daemon=True)
    reader.start()
    while True:
        try:
            line = lines.get(timeout=tick_s)
        except queue.Empty:
            on_tick()
            continue
        if line is None:
            break
        on_line(line)
    reader.join()
    return process.wait()


def _terminate(signum: int, _frame: FrameType | None) -> None:
    # systemd's stop/timeout: raise inside run_once so the status says so.
    raise SystemExit(f"stopped by signal {signal.Signals(signum).name}")


def main() -> int:
    checkout = Path(os.environ.get("CANARY_HOME", "/opt/canary"))
    commands = SystemCommands(
        checkout=checkout,
        owner=checkout_owner(checkout),
        updater=os.environ.get("CANARY_UPDATE_COMMAND", DEFAULT_UPDATER),
    )
    signal.signal(signal.SIGTERM, _terminate)
    return run_once(DEFAULT_UPDATE_PATHS, commands)


if __name__ == "__main__":
    sys.exit(main())
