"""Run a launcher script under the repo's venv, creating it when missing.

Shared by ``scripts/canary_desk.py`` and ``scripts/canary_companion.py``.
Standard library only, and meant to run before any project import, so a bare
``python3 scripts/<launcher>.py`` on a machine with nothing installed still
works: the script re-launches itself under ``.venv/bin/python``, and when
there is no ``.venv`` it creates one with ``scripts/install_deps.py`` first
(the project's own installer; Torch left out, since the launchers run no
model).

Usage, at the top of a launcher, after the ``sys.path`` insert and before the
first ``import src...``::

    from scripts.venv_launch import ensure_venv  # noqa: E402

    if __name__ == "__main__":
        ensure_venv(Path(__file__).resolve())

``launch_plan`` is pure (every fact is an argument), so the rules are tested
without a venv, an exec or an install.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VENV_DIR = REPO / ".venv"
INSTALLER = REPO / "scripts" / "install_deps.py"
#: Set on the re-launch after an install, so a venv that still lacks its
#: packages is a refusal, never a loop.
BOOTSTRAPPED_ENV = "CANARY_DESK_BOOTSTRAPPED"
REQUIRED_PYTHON = (3, 12)
#: Modules whose absence means the venv was never installed into.
VENV_MARKER_MODULES = ("pydantic", "yaml")
NO_BOOTSTRAP_FLAG = "--no-bootstrap"
NO_BOOTSTRAP_HELP = (
    "never create .venv or install packages; refuse by name instead "
    "(the script still re-launches under an existing .venv)"
)
#: What the desk launcher installs: the dev toolchain plus the mDNS browse
#: for companion mode (``zeroconf``), never Torch.
INSTALL_EXTRAS = "dev,companion"
MANUAL_INSTALL = f"python3 scripts/install_deps.py --extras {INSTALL_EXTRAS}"


def venv_python(venv: Path = VENV_DIR) -> Path:
    """The interpreter inside ``venv`` (``install_deps.venv_python``'s rule)."""
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def running_in(venv: Path, prefix: str = sys.prefix) -> bool:
    """Whether this interpreter is ``venv``'s own."""
    try:
        return Path(prefix).resolve() == venv.resolve()
    except OSError:
        return False


@dataclass(frozen=True)
class Launch:
    """What the prologue does: ``run`` here, ``exec`` the venv, ``install`` then exec, or refuse.

    ``command`` is the exec or install command; ``reason``/``message`` the refusal.
    """

    action: str
    command: tuple[str, ...] = ()
    reason: str = ""
    message: str = ""


def _module_present(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def launch_plan(
    argv: Sequence[str],
    *,
    script: Path,
    venv: Path = VENV_DIR,
    prefix: str = sys.prefix,
    version: tuple[int, int] = sys.version_info[:2],
    executable: str = sys.executable,
    environ: Mapping[str, str] = os.environ,
    module_present: Callable[[str], bool] = _module_present,
) -> Launch:
    """Decide how to run ``script``, from the facts alone."""
    python = venv_python(venv)
    install = (
        executable,
        str(INSTALLER),
        "--venv",
        str(venv),
        "--extras",
        INSTALL_EXTRAS,
        "--torch",
        "none",
    )
    if running_in(venv, prefix):
        missing = [name for name in VENV_MARKER_MODULES if not module_present(name)]
        if not missing:
            return Launch("run")
        why = f"{venv} lacks {', '.join(missing)}"
        if environ.get(BOOTSTRAPPED_ENV):
            return Launch(
                "refuse",
                reason="venv_incomplete",
                message=f"{why} even after installing; run `{MANUAL_INSTALL}` and read its output",
            )
        if NO_BOOTSTRAP_FLAG in argv:
            return Launch(
                "refuse",
                reason="venv_incomplete",
                message=f"{why}; run `{MANUAL_INSTALL}` or drop {NO_BOOTSTRAP_FLAG}",
            )
        return Launch("install", (str(python), *install[1:]))
    if python.exists():
        return Launch("exec", (str(python), str(script), *argv))
    if NO_BOOTSTRAP_FLAG in argv:
        return Launch(
            "refuse",
            reason="venv_missing",
            message=f"no {venv}; run `{MANUAL_INSTALL}` or drop {NO_BOOTSTRAP_FLAG}",
        )
    if version < REQUIRED_PYTHON:
        want = ".".join(str(part) for part in REQUIRED_PYTHON)
        have = ".".join(str(part) for part in version)
        try:
            shown = script.relative_to(REPO)
        except ValueError:
            shown = script
        return Launch(
            "refuse",
            reason="python_too_old",
            message=f"no {venv}, and this is Python {have}; CANary needs {want}+ to create one "
            f"(install python{want}, then run `python{want} {shown}`)",
        )
    return Launch("install", install)


def ensure_venv(script: Path, argv: Sequence[str] | None = None) -> None:
    """Carry out ``launch_plan``: return only when this process may run ``script``."""
    args = list(sys.argv[1:] if argv is None else argv)
    name = script.name
    # Read at call time, so a test (or a caller) can point VENV_DIR elsewhere.
    plan = launch_plan(
        args, script=script, venv=VENV_DIR, prefix=sys.prefix, executable=sys.executable
    )
    if plan.action == "run":
        return
    if plan.action == "refuse":
        print(f"refused: {plan.reason}: {plan.message}", file=sys.stderr)
        raise SystemExit(2)
    env = dict(os.environ)
    if plan.action == "install":
        print(
            f"{name}: installing CANary into {VENV_DIR} (one time, a few minutes)",
            file=sys.stderr,
        )
        print("+ " + " ".join(plan.command), file=sys.stderr, flush=True)
        rc = subprocess.call(list(plan.command), cwd=REPO)  # noqa: S603 - our own installer
        if rc != 0:
            print(
                f"refused: install_failed: `{' '.join(plan.command)}` exited {rc}; "
                "read its output above",
                file=sys.stderr,
            )
            raise SystemExit(2)
        env[BOOTSTRAPPED_ENV] = "1"
    python = venv_python(VENV_DIR)
    command = [str(python), str(script), *args]
    print(f"{name}: re-launching under {python}", file=sys.stderr, flush=True)
    if sys.platform == "win32":
        raise SystemExit(subprocess.call(command, env=env))  # noqa: S603 - our own script
    os.execve(str(python), command, env)


__all__ = [
    "BOOTSTRAPPED_ENV",
    "INSTALLER",
    "MANUAL_INSTALL",
    "NO_BOOTSTRAP_FLAG",
    "NO_BOOTSTRAP_HELP",
    "REPO",
    "REQUIRED_PYTHON",
    "VENV_DIR",
    "VENV_MARKER_MODULES",
    "Launch",
    "ensure_venv",
    "launch_plan",
    "running_in",
    "venv_python",
]
