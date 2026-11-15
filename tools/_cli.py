"""Shared ``--config`` / ``--profile`` preamble for ``tools/*.py`` (R30.3).

``scripts/_cli.py`` has the same job for the scripts, but it imports pandas
(session discovery), which a tool that only reads a ledger or a manifest should
not pay for. This one imports ``src.config`` and ``src.vehicle.profiles`` and
nothing else; ``tests/test_tooling_config_scope.py::test_tools_cli_imports_no_pandas``
keeps pandas out of it.

Importing this module puts the repo root on ``sys.path``, as ``scripts/_cli.py``
does, so a tool run as a file (``.venv/bin/python tools/memory.py``, how the
docs and the operator run them) finds ``src`` and ``tools`` without each tool
inserting the path itself. A tool imports it as::

    try:
        from _cli import add_config_profile_args, load_scoped  # type: ignore[import-not-found]
    except ImportError:  # loaded as `tools.<name>`: repo root on path
        from tools._cli import add_config_profile_args, load_scoped
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.config import CanaryConfig  # noqa: E402
from src.vehicle.profiles import scoped_config_for_tooling  # noqa: E402


def add_config_profile_args(
    parser: argparse.ArgumentParser,
    *,
    config_required: bool = True,
    config_help: str = "canary.yaml to scope by.",
    profile_help: str = "Vehicle profile slug.",
) -> None:
    """Add ``--config`` and ``--profile``, the pair :func:`load_scoped` reads.

    ``--config`` is required unless ``config_required=False`` (a tool that can
    also take explicit paths, ``migrate_channel_names``); ``--profile`` is a
    slug, defaulting to the ``vehicle:`` block's profile (no VIN probe). The help
    strings are the one thing a tool may word for itself.
    """
    parser.add_argument("--config", type=Path, required=config_required, help=config_help)
    parser.add_argument("--profile", default=None, help=profile_help)


def load_scoped(args: argparse.Namespace) -> CanaryConfig:
    """``args.config`` loaded and scoped to ``args.profile``'s vehicle.

    The one way a tool scopes its config
    (:func:`src.vehicle.profiles.scoped_config_for_tooling`): with profiles
    enabled the result is always profile-scoped, so a vehicle-data reader never
    meets the unscoped path (P02.11). A tool without ``--profile`` (it has no
    ``args.profile``) gets the ``vehicle:`` block's profile.
    """
    return scoped_config_for_tooling(
        CanaryConfig.from_yaml(args.config), getattr(args, "profile", None)
    )


__all__ = ["add_config_profile_args", "load_scoped"]
