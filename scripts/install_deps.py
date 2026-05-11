#!/usr/bin/env python3
"""Install CANary dependencies with CUDA-aware PyTorch wheel selection.

The important bit is avoiding a plain `pip install .[ml]` on machines whose
NVIDIA driver cannot run PyTorch's current default CUDA wheel. We install the
project extras first, then install Torch from the PyTorch wheel index that
matches the local driver capability.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

CUDA_VERSION_RE = re.compile(r"CUDA Version:\s*(?P<major>\d+)\.(?P<minor>\d+)")
VersionTuple = tuple[int, int]


@dataclass(frozen=True)
class TorchWheel:
    """A PyTorch wheel index and the minimum driver CUDA capability it needs."""

    name: str
    index_url: str
    min_cuda: VersionTuple | None


TORCH_WHEELS: tuple[TorchWheel, ...] = (
    TorchWheel("cu130", "https://download.pytorch.org/whl/cu130", (13, 0)),
    TorchWheel("cu128", "https://download.pytorch.org/whl/cu128", (12, 8)),
    TorchWheel("cu126", "https://download.pytorch.org/whl/cu126", (12, 6)),
    TorchWheel("cu118", "https://download.pytorch.org/whl/cu118", (11, 8)),
    TorchWheel("cpu", "https://download.pytorch.org/whl/cpu", None),
)
TORCH_WHEEL_BY_NAME = {wheel.name: wheel for wheel in TORCH_WHEELS}
TORCH_CHOICES = ("auto", "none", *TORCH_WHEEL_BY_NAME.keys())


@dataclass(frozen=True)
class TorchSelection:
    """The chosen Torch install source plus a human-readable reason."""

    wheel: TorchWheel | None
    reason: str


def parse_cuda_version(text: str) -> VersionTuple | None:
    """Parse the CUDA capability version from `nvidia-smi` output."""
    match = CUDA_VERSION_RE.search(text)
    if match is None:
        return None
    return int(match.group("major")), int(match.group("minor"))


def select_torch_wheel(
    requested: str,
    detected_cuda: VersionTuple | None,
) -> TorchSelection:
    """Choose a Torch wheel index from a user request and detected CUDA version."""
    if requested == "none":
        return TorchSelection(None, "Torch install skipped by --torch none.")

    if requested != "auto":
        wheel = TORCH_WHEEL_BY_NAME[requested]
        return TorchSelection(wheel, f"Torch wheel forced by --torch {requested}.")

    if detected_cuda is None:
        return TorchSelection(
            TORCH_WHEEL_BY_NAME["cpu"],
            "No usable CUDA version was detected from nvidia-smi; selecting CPU Torch.",
        )

    for wheel in TORCH_WHEELS:
        if wheel.min_cuda is not None and detected_cuda >= wheel.min_cuda:
            return TorchSelection(
                wheel,
                (
                    f"Detected driver CUDA capability {format_version(detected_cuda)}; "
                    f"selecting {wheel.name} Torch."
                ),
            )

    return TorchSelection(
        TORCH_WHEEL_BY_NAME["cpu"],
        (
            f"Detected driver CUDA capability {format_version(detected_cuda)}, "
            "which is older than the supported CUDA wheel set; selecting CPU Torch."
        ),
    )


def detect_cuda_with_nvidia_smi() -> tuple[VersionTuple | None, str]:
    """Return detected driver CUDA capability and a short diagnostic string."""
    try:
        completed = subprocess.run(
            ["nvidia-smi"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None, "nvidia-smi was not found."
    except subprocess.TimeoutExpired:
        return None, "nvidia-smi timed out."

    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    detected = parse_cuda_version(combined)
    if completed.returncode != 0:
        detail = combined.strip().splitlines()
        message = detail[0] if detail else f"nvidia-smi exited {completed.returncode}."
        return detected, message
    if detected is None:
        return None, "nvidia-smi ran, but no CUDA Version field was found."
    return detected, f"nvidia-smi reported CUDA Version {format_version(detected)}."


def editable_project_spec(extras: Sequence[str]) -> str:
    """Return the pip editable target for the requested project extras."""
    normalized = [extra.strip() for extra in extras if extra.strip()]
    if not normalized:
        return "."
    return f".[{','.join(normalized)}]"


def parse_extras(value: str) -> list[str]:
    """Parse a comma-separated extra list; `none` and empty mean no extras."""
    if value.strip().lower() in {"", "none"}:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def project_extras_for_install(extras: Sequence[str]) -> list[str]:
    """Extras to pass to the editable project install.

    The current `ml` extra is Torch-only, and Torch needs a CUDA-aware wheel
    index. Installing `.[ml]` first would let PyPI choose the wrong CUDA build
    and then force a second huge reinstall, so this script owns Torch directly.
    """
    return [extra for extra in extras if extra != "ml"]


def venv_python(venv: Path) -> Path:
    """Return the Python executable path inside a virtualenv."""
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def run_command(command: Sequence[str], *, dry_run: bool) -> None:
    """Print and optionally execute a command."""
    printable = " ".join(command)
    print(f"+ {printable}")
    if dry_run:
        return
    subprocess.run(list(command), check=True)


def install(
    *,
    venv: Path,
    extras: Sequence[str],
    torch: str,
    torch_package: str,
    upgrade_pip: bool,
    pip_check: bool,
    dry_run: bool,
) -> int:
    """Create/update the venv and install project dependencies."""
    python = venv_python(venv)
    if not python.exists():
        run_command([sys.executable, "-m", "venv", str(venv)], dry_run=dry_run)

    if upgrade_pip:
        run_command([str(python), "-m", "pip", "install", "--upgrade", "pip"], dry_run=dry_run)

    project_extras = project_extras_for_install(extras)
    if "ml" in extras:
        print("Skipping project extra 'ml'; Torch is installed via the selected wheel index.")
    project_spec = editable_project_spec(project_extras)
    run_command([str(python), "-m", "pip", "install", "-e", project_spec], dry_run=dry_run)

    if torch == "auto":
        detected_cuda, detection_note = detect_cuda_with_nvidia_smi()
    else:
        detected_cuda = None
        detection_note = f"CUDA detection skipped because --torch {torch} was specified."
    selection = select_torch_wheel(torch, detected_cuda)
    print(detection_note)
    print(selection.reason)
    if selection.wheel is not None:
        run_command(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--force-reinstall",
                "--index-url",
                selection.wheel.index_url,
                torch_package,
            ],
            dry_run=dry_run,
        )

    if pip_check:
        run_command([str(python), "-m", "pip", "check"], dry_run=dry_run)

    return 0


def format_version(version: VersionTuple) -> str:
    """Format a `(major, minor)` version tuple."""
    return f"{version[0]}.{version[1]}"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create/update the CANary virtualenv and install dependencies. "
            "Torch is installed from a PyTorch wheel index selected from the "
            "local NVIDIA driver CUDA capability."
        )
    )
    parser.add_argument(
        "--venv",
        type=Path,
        default=Path(".venv"),
        help="Virtualenv path to create/update (default: .venv).",
    )
    parser.add_argument(
        "--extras",
        default="dev",
        help="Comma-separated project extras to install before Torch; use 'none' for base only.",
    )
    parser.add_argument(
        "--torch",
        choices=TORCH_CHOICES,
        default="auto",
        help=(
            "Torch wheel source: auto, none, cpu, cu118, cu126, cu128, or cu130 (default: auto)."
        ),
    )
    parser.add_argument(
        "--torch-package",
        default="torch",
        help="Torch package spec to install from the selected index (default: torch).",
    )
    parser.add_argument(
        "--skip-pip-upgrade",
        action="store_true",
        help="Do not upgrade pip before installing dependencies.",
    )
    parser.add_argument(
        "--no-pip-check",
        action="store_true",
        help="Skip the final `pip check` verification.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the commands and detected Torch choice without executing installs.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return install(
        venv=args.venv,
        extras=parse_extras(args.extras),
        torch=args.torch,
        torch_package=args.torch_package,
        upgrade_pip=not args.skip_pip_upgrade,
        pip_check=not args.no_pip_check,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
