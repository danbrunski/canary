#!/usr/bin/env python3
"""Desk mode: bring up the CANary GUI on a laptop, with no truck (E08.9).

One command starts the whole daemon on a virtual CAN bus with OBD disabled, so
it never transmits, loads your ROM images into the ROM Library, and opens the
ROM Editor, where an edit can be previewed and run through the simulator:

    .venv/bin/python scripts/canary_desk.py --rom-dir "/path/to/Tuned ROMs"

Everything the daemon writes lives under ``--state-dir`` (default
``~/canary-data/desk``), never in the checkout: the ROM Library holds
proprietary images, and ``canary.sim.example.yaml`` on its own would put
control state under ``data/sim/``. The config is generated from that file (so
the two cannot drift): loaded, overridden in the few named places, validated
through ``CanaryConfig``, and written beside the state as ``canary.desk.yaml``.

Standard library plus this repo's own modules. Never uses ``sudo``, and never
touches ``/etc/canary`` or ``/data/canary``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from src.config import DAEMON_STATE_LAYOUT, CanaryConfig  # noqa: E402
from src.control.rom_library import RomLibraryError  # noqa: E402
from src.paths import REPO_ROOT  # noqa: E402

SIM_CONFIG = REPO_ROOT / "canary.sim.example.yaml"
STATIC_DIR = REPO_ROOT / "src" / "dashboard" / "static"
DEFAULT_STATE_DIR = Path("~/canary-data/desk")
DEFAULT_PROFILE = "nissan-xterra-2008"
DEFAULT_PORT = 8000
DEFAULT_TIMEOUT_SECONDS = 120.0
#: The ROM Editor's page id in ``nav.ts``; ``tests/test_canary_desk.py`` reads
#: that file so a renamed page cannot leave this pointing at nothing.
ROM_EDITOR_ITEM = "tuning-rom-editor"
#: The daemon reads its token from this variable, named in the generated
#: config (``daemon.control_token_env``). Not ``CANARY_CONTROL_TOKEN``, so the
#: operator's real token can never be picked up by a desk daemon.
TOKEN_ENV = "CANARY_DESK_CONTROL_TOKEN"
TOKEN_FILENAME = "desk.token"
CONFIG_FILENAME = "canary.desk.yaml"
LOG_FILENAME = "desk.log"
ROM_SUFFIXES = (".osirisrom", ".bin")
#: State directories that belong to a real install; a desk run never uses them.
RESERVED_STATE_DIRS = (Path("/etc/canary"), Path("/data/canary"))
STOP_GRACE_SECONDS = 20.0


class DeskRefusalError(Exception):
    """A refusal, named: ``reason`` is the stable name, the message says why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# State directory and the generated config
# ---------------------------------------------------------------------------


def resolve_state_dir(state_dir: Path) -> Path:
    """The absolute state directory, or the refusal.

    Symlinks are resolved first, so a link into the checkout is caught."""
    resolved = state_dir.expanduser().resolve()
    if resolved == REPO_ROOT or resolved.is_relative_to(REPO_ROOT):
        raise DeskRefusalError(
            "inside_checkout",
            f"{resolved} is inside the checkout ({REPO_ROOT}). The ROM Library holds "
            "proprietary images and the daemon writes ledgers; keep them outside the repo "
            "(the default is ~/canary-data/desk).",
        )
    for reserved in RESERVED_STATE_DIRS:
        if resolved == reserved or resolved.is_relative_to(reserved):
            raise DeskRefusalError(
                "reserved_state_dir",
                f"{resolved} belongs to a real CANary install ({reserved}); desk mode never "
                "touches it.",
            )
    return resolved


def build_config(
    state_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    replay: Path | None = None,
    base: Path = SIM_CONFIG,
) -> dict[str, Any]:
    """``canary.sim.example.yaml`` with the desk overrides, validated.

    Overrides, and only these: ``daemon.state_dir`` (with every explicit
    runtime path the sim file names dropped, so S03's re-rooting applies),
    ``collection.storage_path``, ``dashboard.host``/``port``, the replay
    session (none unless given), and ``daemon.control_token_env``. Models stay
    as the sim file has them (disabled). Returns the dict that is written."""
    state = resolve_state_dir(state_dir)
    raw: dict[str, Any] = yaml.safe_load(base.read_text())
    daemon = raw.setdefault("daemon", {})
    for name in DAEMON_STATE_LAYOUT:
        daemon.pop(name, None)
    daemon["state_dir"] = str(state)
    daemon["control_token_env"] = TOKEN_ENV
    collection = raw.setdefault("collection", {})
    collection["storage_path"] = str(state / "sessions")
    collection["replay_session"] = None if replay is None else str(replay.expanduser().resolve())
    dashboard = raw.setdefault("dashboard", {})
    dashboard["host"] = host
    dashboard["port"] = port
    assert_state_outside_checkout(CanaryConfig.model_validate(raw), state)
    return raw


def assert_state_outside_checkout(config: CanaryConfig, state: Path) -> None:
    """Every path the daemon writes is under ``state``; the refusal names it.

    The sim file's relative ``data/sim/...`` paths would otherwise land in the
    checkout, so this runs on the validated result, not on the overrides."""
    written: dict[str, Path] = {
        f"daemon.{name}": getattr(config.daemon, name) for name in DAEMON_STATE_LAYOUT
    }
    written["collection.storage_path"] = config.collection.storage_path
    written["profiles.registry_overlay_path"] = config.profiles.registry_overlay_path
    written["llm_assist.cards_path"] = config.llm_assist.cards_path
    for name, path in written.items():
        resolved = path.resolve()
        if not resolved.is_relative_to(state) or resolved.is_relative_to(REPO_ROOT):
            raise DeskRefusalError(
                "inside_checkout", f"{name} = {path} is not under the state dir {state}"
            )


def write_config(state: Path, raw: dict[str, Any]) -> Path:
    state.mkdir(parents=True, exist_ok=True)
    path = state / CONFIG_FILENAME
    path.write_text(
        "# Generated by scripts/canary_desk.py from canary.sim.example.yaml; "
        "rewritten on every run.\n" + yaml.safe_dump(raw, sort_keys=False)
    )
    return path


# ---------------------------------------------------------------------------
# Control token
# ---------------------------------------------------------------------------


def token_path(state: Path) -> Path:
    return state / "control" / TOKEN_FILENAME


def ensure_token(state: Path) -> str:
    """The desk control token: generated once, kept at mode 0600, reused after.

    Created with ``O_EXCL`` and mode 0600 so there is no moment it is readable
    by anyone else; an existing file with looser permissions is tightened."""
    path = token_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = path.read_text().strip()
        if existing:
            if path.stat().st_mode & 0o077:
                path.chmod(0o600)
            return existing
        path.unlink()
        return ensure_token(state)
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    return token


# ---------------------------------------------------------------------------
# Dashboard bundle
# ---------------------------------------------------------------------------


def stale_reason(static_dir: Path = STATIC_DIR) -> str | None:
    """Why ``dist/`` must be rebuilt, or ``None`` when it is current.

    The rule ``scripts/update_canary.sh`` (``decide_dashboard``) uses: missing,
    or any file under the frontend, ``node_modules`` and ``dist`` aside, newer
    than ``dist/index.html``. One difference: ``*.tsbuildinfo`` are ``tsc``'s
    caches (git-ignored build output), rewritten by every ``pnpm typecheck``,
    so they would keep a freshly built bundle looking stale."""
    built = static_dir / "dist" / "index.html"
    if not built.is_file():
        return "dist/ missing"
    built_at = built.stat().st_mtime
    for root, dirs, files in os.walk(static_dir):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "dist")]
        for name in files:
            if name.endswith(".tsbuildinfo"):
                continue
            path = Path(root, name)
            try:
                if path.stat().st_mtime > built_at:
                    return f"dist/ older than {path.relative_to(static_dir)}"
            except OSError:
                continue
    return None


def ensure_dashboard(
    static_dir: Path = STATIC_DIR,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> str | None:
    """Build ``dist/`` when it is missing or stale. Returns why it ran, or ``None``."""
    reason = stale_reason(static_dir)
    if reason is None:
        return None
    pnpm = which("pnpm")
    if pnpm is None:
        raise DeskRefusalError(
            "pnpm_missing",
            f"the dashboard bundle needs a build ({reason}) and pnpm is not on PATH. Install "
            "Node.js 20+ and pnpm (https://pnpm.io/installation, e.g. `corepack enable`), then "
            "run this again. This script never installs tools.",
        )
    print(f"building the dashboard ({reason}) ...", flush=True)
    if not (static_dir / "node_modules").is_dir():
        _pnpm(run, pnpm, ["install", "--frozen-lockfile"], static_dir)
    _pnpm(run, pnpm, ["build"], static_dir)
    return reason


def _pnpm(run: Callable[..., Any], pnpm: str, args: list[str], cwd: Path) -> None:
    result = run([pnpm, *args], cwd=cwd, check=False)
    if result.returncode != 0:
        raise DeskRefusalError(
            "dashboard_build_failed",
            f"`pnpm {' '.join(args)}` exited {result.returncode} (output above)",
        )


# ---------------------------------------------------------------------------
# ROMs
# ---------------------------------------------------------------------------

#: ``add(data, filename) -> (entry, created)``; ``RomLibrary.add_upload`` has
#: exactly this shape, and so does the daemon's upload route behind
#: :func:`http_add`. A refusal raises :class:`RomLibraryError`.
AddRom = Callable[[bytes, str], tuple[dict[str, Any], bool]]


@dataclass(frozen=True)
class RomOutcome:
    path: Path
    entry: dict[str, Any] | None
    created: bool
    refusal: str | None

    @property
    def sha256(self) -> str | None:
        return None if self.entry is None else str(self.entry["sha256"])

    def line(self) -> str:
        if self.entry is None:
            return f"  skipped  {self.path.name}: {self.refusal}"
        match = self.entry.get("revision_match") or {}
        status = match.get("status", "?")
        named = match.get("revision") or match.get("reference")
        verdict = f"{status} ({named})" if named else str(status)
        can = "can simulate" if status == "matched" else "preview only"
        return (
            f"  {'added  ' if self.created else 'present'}  {self.path.name}\n"
            f"           sha256 {self.sha256}\n"
            f"           revision_match: {verdict} -- {can}"
        )


def rom_dirs(cli_dirs: Sequence[Path], environ: dict[str, str] | None = None) -> list[Path]:
    """``--rom-dir`` values, then ``$CANARY_ROM_DIRS`` (``os.pathsep``-separated)."""
    env = os.environ if environ is None else environ
    dirs = [Path(d).expanduser() for d in cli_dirs]
    dirs += [Path(d).expanduser() for d in env.get("CANARY_ROM_DIRS", "").split(os.pathsep) if d]
    return list(dict.fromkeys(dirs))


def rom_files(directories: Iterable[Path]) -> list[Path]:
    """The ROM-looking files directly inside each directory, in name order.

    A missing directory is a refusal: a typo would otherwise read as "no ROMs"."""
    files: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            raise DeskRefusalError("rom_dir_missing", f"{directory} is not a directory")
        files += sorted(
            p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in ROM_SUFFIXES
        )
    return files


def import_roms(files: Iterable[Path], add: AddRom) -> list[RomOutcome]:
    """Add each file to the ROM Library. Idempotent by sha256 (the library
    dedupes). A refusal (e.g. UpRev's encrypted ``.bin`` backup, named by
    ``rom_image``) is reported and skipped, never fatal. Files are only read."""
    outcomes: list[RomOutcome] = []
    for path in files:
        try:
            entry, created = add(path.read_bytes(), path.name)
        except RomLibraryError as exc:
            outcomes.append(RomOutcome(path, None, False, str(exc)))
        else:
            outcomes.append(RomOutcome(path, entry, created, None))
    return outcomes


def http_add(base_url: str, token: str) -> AddRom:
    """``AddRom`` over the daemon's own upload route, so the library the
    dashboard shows is the one written (vehicle scoping included)."""

    def add(data: bytes, filename: str) -> tuple[dict[str, Any], bool]:
        url = f"{base_url}/control/rom/library?{urllib.parse.urlencode({'filename': filename})}"
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            if exc.code in (413, 415, 422):
                raise RomLibraryError(detail) from exc
            raise DeskRefusalError("rom_upload_failed", f"HTTP {exc.code}: {detail}") from exc
        created = bool(body.pop("created", False))
        return body, created

    return add


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        detail = json.load(exc).get("detail", exc.reason)
    except (ValueError, AttributeError):
        return str(exc.reason)
    return detail if isinstance(detail, str) else json.dumps(detail)


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------


def is_wsl(
    environ: dict[str, str] | None = None, proc_version: Path = Path("/proc/version")
) -> bool:
    env = os.environ if environ is None else environ
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in proc_version.read_text().lower()
    except OSError:
        return False


def browser_command(
    url: str, *, wsl: bool, which: Callable[[str], str | None] = shutil.which
) -> list[str] | None:
    """The argv that opens ``url`` in the Windows browser on WSL, else ``None``
    (use :mod:`webbrowser`)."""
    if not wsl:
        return None
    if which("wslview"):
        return ["wslview", url]
    if which("cmd.exe"):
        return ["cmd.exe", "/c", "start", "", url]
    return None


def open_browser(url: str, *, shown: str | None = None) -> None:
    """Open ``url``. Failure messages name ``shown`` (default ``url``), so a
    link carrying the token is never printed."""
    argv = browser_command(url, wsl=is_wsl())
    shown = url if shown is None else shown
    try:
        if argv is None:
            if not webbrowser.open(url):
                print(f"could not find a browser; open {shown} yourself")
        else:
            subprocess.run(argv, check=False, timeout=20, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"could not open a browser ({exc}); open {shown} yourself")


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


def require_free_port(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise DeskRefusalError(
                "port_in_use",
                f"{host}:{port} is not free ({exc.strerror or exc}); another CANary or "
                "dashboard is probably running. Pick another with --port.",
            ) from exc


def browse_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "") else host  # noqa: S104


def editor_url(host: str, port: int) -> str:
    return f"http://{browse_host(host)}:{port}/#wb/{ROM_EDITOR_ITEM}"


#: What ``secrets.token_urlsafe`` produces. Asserted rather than escaped: the
#: link may become an argument of ``cmd.exe /c start``, where ``&`` and ``^``
#: would mean something.
_TOKEN_ALPHABET = re.compile(r"[A-Za-z0-9_-]+")


def handoff_url(host: str, port: int, token: str) -> str:
    """``editor_url`` carrying the token for the dashboard's handoff
    (``src/dashboard/static/src/lib/tokenHandoff.ts``): the query sits inside
    the fragment, after the route, so it is never sent to the server."""
    if not _TOKEN_ALPHABET.fullmatch(token):
        raise DeskRefusalError(
            "token_not_urlsafe",
            "the desk token has characters outside [A-Za-z0-9_-]; delete "
            f"{TOKEN_FILENAME} under the state dir and run again.",
        )
    return f"{editor_url(host, port)}?token={urllib.parse.quote(token, safe='')}"


def daemon_command(config_path: Path, profile: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "src.main",
        "--config",
        str(config_path),
        "--profile",
        profile,
    ]


def daemon_env(token: str, environ: dict[str, str] | None = None) -> dict[str, str]:
    """The child's environment: the desk token under its own name, and the
    operator's real control token removed."""
    env = dict(os.environ if environ is None else environ)
    env.pop("CANARY_CONTROL_TOKEN", None)
    env[TOKEN_ENV] = token
    env["PYTHONUNBUFFERED"] = "1"
    return env


def wait_until_up(
    base_url: str,
    child: subprocess.Popen[bytes],
    *,
    timeout: float,
    log_path: Path,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Poll ``GET /status`` until it answers, the child dies, or ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = child.poll()
        if code is not None:
            raise DeskRefusalError(
                "daemon_exited",
                f"the daemon exited with status {code} before it answered.\n{_log_tail(log_path)}",
            )
        try:
            with urllib.request.urlopen(f"{base_url}/status", timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        sleep(0.25)
    raise DeskRefusalError(
        "startup_timeout",
        f"the dashboard did not answer within {timeout:g} s (--timeout).\n{_log_tail(log_path)}",
    )


def _log_tail(log_path: Path, lines: int = 25) -> str:
    try:
        tail = log_path.read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return f"(no log at {log_path})"
    return f"last lines of {log_path}:\n" + "\n".join(tail)


def stop_child(child: subprocess.Popen[bytes], grace: float = STOP_GRACE_SECONDS) -> int | None:
    """SIGTERM (the daemon stops cleanly on it), then SIGKILL after ``grace``.

    A second Ctrl+C while waiting escalates to the kill, quietly."""
    if child.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            child.terminate()
        try:
            child.wait(timeout=grace)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            with contextlib.suppress(ProcessLookupError):
                child.kill()
            while True:
                try:
                    child.wait()
                    break
                except KeyboardInterrupt:
                    continue
    return child.returncode


@contextlib.contextmanager
def _stop_on_signal() -> Iterator[None]:
    """Ctrl+C and SIGTERM both take the one clean-stop path.

    SIGINT is set explicitly: a script started from a non-interactive shell's
    background inherits it as ignored, and Python then never raises
    ``KeyboardInterrupt``."""

    def handler(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bring up the CANary GUI on a laptop with no truck: the ROM Editor "
        "plus the simulator, on a virtual CAN bus that never transmits.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"where the daemon keeps everything (default {DEFAULT_STATE_DIR}); "
        "never inside the checkout",
    )
    parser.add_argument(
        "--rom-dir",
        type=Path,
        action="append",
        default=[],
        help="a folder of .OsirisROM / plain images to add to the ROM Library "
        "(repeatable; also $CANARY_ROM_DIRS). Only read, never written.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default 127.0.0.1: only this machine). 0.0.0.0 exposes the "
        "dashboard to your network.",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="vehicle profile slug")
    parser.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="a stored session to replay into the dashboard (default: none)",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="use src/dashboard/static/dist as it is, even when missing or stale",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds to wait for the dashboard to answer (default {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    state = resolve_state_dir(args.state_dir)
    files = rom_files(rom_dirs(args.rom_dir))
    require_free_port(args.host, args.port)
    raw = build_config(state, host=args.host, port=args.port, replay=args.replay)
    if not args.no_build:
        ensure_dashboard()
    token = ensure_token(state)
    config_path = write_config(state, raw)
    log_path = state / LOG_FILENAME
    base_url = f"http://{browse_host(args.host)}:{args.port}"

    print(f"state dir : {state}")
    print(f"config    : {config_path}")
    print(f"log       : {log_path}")
    with log_path.open("ab") as log, _stop_on_signal():
        child = subprocess.Popen(  # noqa: S603 - our own interpreter and module
            daemon_command(config_path, args.profile),
            cwd=REPO_ROOT,
            env=daemon_env(token),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            # Ctrl+C reaches this script only; it stops the daemon in order.
            start_new_session=True,
        )
        try:
            print(f"daemon pid: {child.pid}", flush=True)
            wait_until_up(base_url, child, timeout=args.timeout, log_path=log_path)
            if files:
                print("ROM Library:")
                for outcome in import_roms(files, http_add(base_url, token)):
                    print(outcome.line())
            else:
                print("ROM Library: open a ROM from the dashboard (ROM Library upload).")
            url = editor_url(args.host, args.port)
            link = handoff_url(args.host, args.port, token)
            print(f"\nCANary desk mode is up: {url}")
            if args.no_browser:
                print("Open this link (it carries the control token; keep it to yourself):")
                print(f"  {link}")
            print(f"Control token kept at {token_path(state)}. Ctrl+C stops it.", flush=True)
            if not args.no_browser:
                open_browser(link, shown=url)
            while child.poll() is None:
                time.sleep(0.5)
            print(f"the daemon exited with status {child.returncode}.\n{_log_tail(log_path)}")
            return 1
        except KeyboardInterrupt:
            print("\nstopping ...", flush=True)
            return 0
        finally:
            stop_child(child)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except DeskRefusalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
