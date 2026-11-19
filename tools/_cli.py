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
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.config import CanaryConfig  # noqa: E402
from src.paths import DATA_DIR  # noqa: E402
from src.vehicle.platforms import vehicle_data_file  # noqa: E402
from src.vehicle.profiles import (  # noqa: E402
    resolve_profile_for_tooling,
    scoped_config_for_tooling,
)


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


def load_scoped(args: argparse.Namespace, *, per_vehicle: bool = False) -> CanaryConfig:
    """``args.config`` loaded and scoped to ``args.profile``'s vehicle.

    The one way a tool scopes its config
    (:func:`src.vehicle.profiles.scoped_config_for_tooling`): with profiles
    enabled the result is always profile-scoped, so a vehicle-data reader never
    meets the unscoped path (P02.11). A tool without ``--profile`` (it has no
    ``args.profile``) gets the ``vehicle:`` block's profile.

    ``per_vehicle`` is for a tool whose data exists only per vehicle (the
    memory ledger): a profiles-disabled config has no vehicle to scope to, so
    the tool exits with one line naming why instead of a traceback. Before
    R30.3 these tools scoped such a config under the default slug anyway and
    read a directory nothing writes ("0 record(s)"); refusing is the operator's
    decision of 2026-11-15. ``tests/test_tooling_config_scope.py`` finds every
    tool that reads the memory ledger and holds it to this.
    """
    config = scoped_config_for_tooling(
        CanaryConfig.from_yaml(args.config), getattr(args, "profile", None)
    )
    if per_vehicle and config.scoped_profile is None:
        raise SystemExit(refusal_for_unscoped(args.config))
    return config


def refusal_for_unscoped(config_path: Path | str) -> str:
    """The one line a per-vehicle tool exits with on a profiles-disabled config."""
    return (
        f"refused: {config_path} has profiles.enabled: false, and this tool reads "
        "the per-vehicle memory ledger (it lives under <state>/<vehicle>/, so a "
        "config with no vehicle has none). Use a config with profiles enabled."
    )


# ---------------------------------------------------------------------------
# --profile as a slug, for the tools that read a vehicle's data directory (R30.4)
# ---------------------------------------------------------------------------


class ProfileDataError(ValueError):
    """The ``--profile`` / ``--config`` / ``--sessions-root`` flags do not name a vehicle's data."""


@dataclass(frozen=True)
class ProfileData:
    """One vehicle's data directory and sessions, as a tool's flags named them."""

    slug: str
    #: ``<data dir>/<slug>``: the vehicle's own tracked profile data.
    directory: Path
    #: The config's scoped storage for this vehicle, ``--sessions-root`` over it.
    #: ``None`` when neither flag was given; :meth:`require_sessions_root` refuses.
    sessions_root: Path | None
    #: Loaded and scoped when ``--config`` was given (it knows make, model and year).
    config: CanaryConfig | None

    def require_sessions_root(self) -> Path:
        """The sessions root, or a refusal naming the flags that would give one."""
        if self.sessions_root is None:
            raise ProfileDataError(
                "no sessions root: pass --sessions-root DIR (the directory holding this "
                "vehicle's session folders), or --config canary.yaml to use its storage "
                f"path for {self.slug}"
            )
        return self.sessions_root

    def file(self, name: str) -> Path:
        """``<directory>/<name>``, else the platform's (:func:`vehicle_data_file`).

        The platform is found from the config's vehicle (make, model, year), so
        a file shared by a generation (``data/platforms/<id>/``) resolves only
        with ``--config``; without one, a file missing from ``directory`` is
        refused naming that.
        """
        own = self.directory / name
        if own.exists():
            return own
        if self.config is None:
            raise ProfileDataError(
                f"{own} does not exist; a file shared by the vehicle's platform is found "
                "only with --config (it names the vehicle's make, model and year)"
            )
        vehicle = self.config.vehicle
        try:
            return vehicle_data_file(
                self.directory,
                name,
                make=vehicle.make,
                model=vehicle.model,
                year=vehicle.year or None,
            ).path
        except (FileNotFoundError, ValueError) as exc:
            raise ProfileDataError(str(exc)) from exc


def add_profile_dir_args(
    parser: argparse.ArgumentParser,
    *,
    sessions_flag: str | None = "--sessions-root",
    sessions_help: str = "Directory holding this vehicle's session folders.",
) -> None:
    """``--profile`` (a slug), ``--config``, the sessions flag and ``--data-dir``.

    The one spelling of the vehicle flags for a tool that reads
    ``data/<slug>/`` and a vehicle's stored sessions (R30.4). ``--profile`` is
    a slug everywhere, never a directory; there is no default slug, so a tool
    never reads one vehicle's data because the caller said nothing (the old
    defaults named the 2008 Xterra and one operator's home directory).
    :func:`load_profile_data` resolves them.
    """
    parser.add_argument(
        "--profile",
        default=None,
        metavar="SLUG",
        help="Vehicle profile slug (the directory name under data/). Required unless "
        "--config names the profile.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="canary.yaml: its profile when --profile is absent, its storage path as the "
        "sessions root when the sessions flag is absent, and the vehicle (make, model, year) "
        "that finds platform files.",
    )
    if sessions_flag is not None:  # a tool that reads sessions in one subcommand adds its own
        parser.add_argument(
            sessions_flag, type=Path, default=None, dest="sessions_root", help=sessions_help
        )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Directory holding the <slug>/ profile directories (default: the checkout's data/).",
    )


def load_profile_data(args: argparse.Namespace, *, require_directory: bool = True) -> ProfileData:
    """What :func:`add_profile_dir_args`' flags name, or :class:`ProfileDataError`.

    ``--profile`` wins; otherwise the profile the scoped ``--config`` carries.
    A value with a path separator is refused by name: before R30.4 these tools'
    ``--profile`` was a directory, and a stale invocation should say so rather
    than look for ``data/data/nissan-xterra-2008``.

    ``require_directory=False`` is for a tool that may need only a platform's
    files: a vehicle of a covered generation need not have a ``data/<slug>/``
    of its own (R30.4), so a missing directory is not yet a refusal there. It
    is accepted only for the vehicle the ``--config`` itself describes (the
    profile it resolves to without ``--profile``): otherwise a typo'd slug would
    fall through to the platform's file, and a vehicle with its own override
    would silently get the generation's.
    """
    unscoped = CanaryConfig.from_yaml(args.config) if args.config is not None else None
    config = scoped_config_for_tooling(unscoped, args.profile) if unscoped is not None else None
    slug = args.profile or (config.scoped_profile if config is not None else None)
    if not slug:
        raise ProfileDataError(
            "--profile SLUG is required (the directory name under data/); there is no "
            "default vehicle. A --config with profiles enabled supplies its own."
        )
    if "/" in slug or "\\" in slug or slug in {".", ".."}:
        raise ProfileDataError(
            f"--profile takes a slug, not a path (got {slug!r}); use the directory's name, "
            f"--profile {Path(slug).name or slug}, and --data-dir for another data directory"
        )
    data_dir = args.data_dir if args.data_dir is not None else DATA_DIR
    directory = Path(data_dir).expanduser() / slug
    if not directory.is_dir():
        if require_directory:
            raise ProfileDataError(f"no profile directory {directory} for --profile {slug}")
        if unscoped is not None:
            own = resolve_profile_for_tooling(unscoped, None).slug
            if slug != own:
                raise ProfileDataError(
                    f"no profile directory {directory} and --profile {slug} is not the "
                    f"config's vehicle ({own})"
                )
    sessions = getattr(args, "sessions_root", None)
    if sessions is None and config is not None:
        sessions = config.collection.storage_path
    return ProfileData(
        slug=slug,
        directory=directory,
        sessions_root=None if sessions is None else Path(sessions).expanduser(),
        config=config,
    )


def profile_data_or_exit(
    args: argparse.Namespace, *, need_sessions: bool = False, require_directory: bool = True
) -> ProfileData:
    """:func:`load_profile_data`, exiting with one ``refused:`` line (no traceback).

    ``need_sessions`` is for a tool that always reads sessions: it refuses here,
    naming the flags, instead of at the first read.
    """
    try:
        data = load_profile_data(args, require_directory=require_directory)
        if need_sessions:
            data.require_sessions_root()
        return data
    except ProfileDataError as exc:
        raise SystemExit(f"refused: {exc}") from exc


__all__ = [
    "ProfileData",
    "ProfileDataError",
    "add_config_profile_args",
    "add_profile_dir_args",
    "load_profile_data",
    "load_scoped",
    "profile_data_or_exit",
    "refusal_for_unscoped",
]
