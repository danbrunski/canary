#!/usr/bin/env python3
"""Cleanly stop a running `scripts/record.py` from another shell.

Reads the session's ``recorder.pid`` (written by record.py at startup) and
sends exactly one SIGINT — the recorder's first-press handler flushes the
writer and finalizes ``manifest.json``. This exists because finding the
recorder with ``pgrep`` is unreliable: under a shell/eval wrapper the match
includes the wrapper (signalling it does nothing), and with several sessions
on disk there's no way to tell which process owns which. The pidfile is the
stable handle.

Usage:
  scripts/stop_recording.py                       # latest session under data/sessions
  scripts/stop_recording.py --session data/sessions/<ts>
  scripts/stop_recording.py --sessions-root /data/canary --timeout 45

Behavior:
  * pidfile present + process alive  → one SIGINT, wait up to --timeout for a
    finalized manifest, print its summary.
  * pidfile present + process gone   → recorder already exited; print the
    manifest summary if present, remove the stale pidfile.
  * no pidfile, manifest present     → already stopped cleanly; nothing to do.
  * no pidfile, no manifest          → nothing to stop (error).

Safety: before signalling, the PID's /proc cmdline is checked to still be a
record.py process — defends against the rare case where the recorded PID was
recycled by an unrelated process. (Linux/WSL only, which is the dev target.)
Only ever sends ONE SIGINT: a second press restores record.py's default
handler, which would risk a KeyboardInterrupt mid-manifest.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from src.collection.tags import find_latest_session
from src.log import configure_logging
from src.session import MANIFEST_FILENAME, PID_FILENAME

log = logging.getLogger(__name__)

DEFAULT_SESSIONS_ROOT = Path("data/sessions")


def resolve_session(session: Path | None, sessions_root: Path) -> Path:
    """Return the session dir to act on: explicit `session`, else the latest."""
    if session is not None:
        if not session.is_dir():
            raise FileNotFoundError(f"session directory does not exist: {session}")
        return session
    return find_latest_session(sessions_root)


def read_recorder_pid(session: Path) -> int | None:
    """Read the recorder PID from `<session>/recorder.pid`.

    Returns None if the file is absent or doesn't contain a plain integer
    (truncated/garbage files are treated as "no usable handle", not a crash).
    """
    pid_path = session / PID_FILENAME
    try:
        text = pid_path.read_text()
    except FileNotFoundError:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def process_alive(pid: int) -> bool:
    """True if a process with `pid` exists. Signal 0 probes without delivering."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


def _looks_like_recorder(cmdline: str) -> bool:
    """Heuristic: does this /proc cmdline belong to our recorder?"""
    return "record.py" in cmdline


def proc_is_recorder(pid: int) -> bool:
    """Best-effort check that `pid` is still a record.py process.

    Reads /proc/<pid>/cmdline (NUL-separated argv). On any platform without
    /proc, or any read error, returns True — we don't want a missing /proc
    to block a legitimate stop; the pidfile is still the primary signal.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True
    return _looks_like_recorder(raw.replace(b"\x00", b" ").decode("utf-8", "replace"))


def plan_stop(pid: int | None, alive: bool, manifest_exists: bool) -> str:
    """Pure decision: what to do given the observed state.

    Returns one of: "signal", "already-stopped", "stale-no-manifest",
    "no-target". Kept side-effect-free so the branching is unit-tested
    without spawning processes.
    """
    if pid is None:
        return "already-stopped" if manifest_exists else "no-target"
    if not alive:
        return "already-stopped" if manifest_exists else "stale-no-manifest"
    return "signal"


def summarize_manifest(manifest: Path) -> str | None:
    """One-line summary of a finalized manifest, or None if absent/invalid."""
    try:
        data = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return (
        f"frames={data.get('frames_recorded')} "
        f"elapsed={data.get('duration_seconds')}s "
        f"parse_errors={data.get('parse_errors')} "
        f"reconnects={data.get('reconnects')} "
        f"dropped_writes={data.get('dropped_writes')} "
        f"stopped_by={data.get('stopped_by')!r}"
    )


def _wait_for_exit(pid: int, manifest: Path, timeout: float, *, poll: float = 0.25) -> bool:
    """Block until `pid` exits (clean stop), or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            # Give the finally-block a beat to finish writing the manifest.
            for _ in range(20):
                if manifest.exists():
                    break
                time.sleep(poll)
            return True
        time.sleep(poll)
    return False


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="stop_recording",
        description="Cleanly stop a running record.py via its session pidfile.",
    )
    parser.add_argument(
        "--session",
        type=Path,
        default=None,
        help="Session directory to stop. Default: the latest under --sessions-root.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=DEFAULT_SESSIONS_ROOT,
        help=f"Where session dirs live (default: {DEFAULT_SESSIONS_ROOT}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for a clean stop + finalized manifest (default 30).",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)

    try:
        session = resolve_session(args.session, args.sessions_root)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    pid = read_recorder_pid(session)
    manifest = session / MANIFEST_FILENAME
    alive = pid is not None and process_alive(pid)
    action = plan_stop(pid, alive, manifest.exists())

    if action == "no-target":
        print(
            f"error: no {PID_FILENAME} and no {MANIFEST_FILENAME} in {session} "
            f"— nothing to stop.",
            file=sys.stderr,
        )
        return 1

    if action == "stale-no-manifest":
        print(
            f"warning: {session}/{PID_FILENAME} points at pid {pid}, which is "
            f"not running, and there's no manifest — the recorder died "
            f"uncleanly (frames.log/tags.jsonl are still intact). Removing the "
            f"stale pidfile.",
            file=sys.stderr,
        )
        (session / PID_FILENAME).unlink(missing_ok=True)
        return 1

    if action == "already-stopped":
        summary = summarize_manifest(manifest)
        print(f"already stopped: {session}")
        if summary:
            print(f"  {summary}")
        (session / PID_FILENAME).unlink(missing_ok=True)
        return 0

    # action == "signal"
    assert pid is not None
    if not proc_is_recorder(pid):
        print(
            f"error: pid {pid} from {session}/{PID_FILENAME} is alive but does "
            f"not look like a record.py process (PID likely recycled). Refusing "
            f"to signal it. Inspect manually.",
            file=sys.stderr,
        )
        return 1

    print(f"stopping {session} (pid {pid}) — SIGINT…")
    try:
        os.kill(pid, signal.SIGINT)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            print("  process exited before the signal landed.", file=sys.stderr)
        else:
            print(f"  failed to signal pid {pid}: {exc}", file=sys.stderr)
            return 1

    if _wait_for_exit(pid, manifest, args.timeout):
        summary = summarize_manifest(manifest)
        print(f"stopped cleanly: {session}")
        if summary:
            print(f"  {summary}")
        else:
            print(
                "  warning: process exited but no readable manifest — "
                "check the recorder log.",
                file=sys.stderr,
            )
        return 0

    print(
        f"error: pid {pid} still running {args.timeout:.0f}s after SIGINT. "
        f"Not sending a second signal (it restores the default handler and "
        f"risks a KeyboardInterrupt mid-manifest). If the adapter is wedged, "
        f"investigate before forcing.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
