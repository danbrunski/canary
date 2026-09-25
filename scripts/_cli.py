"""Shared CLI preamble for `scripts/*.py`: repo-root bootstrap, config load,
session discovery. Not every script needs every helper here — use what fits,
don't force one onto a script whose CLI genuinely differs (e.g. `retrain.py`
and `refit_behavior_models.py` need the full `resolve_sessions` +
`--days`/`--all` combo; most other scripts only want `--config`).

Importing this module bootstraps the repo root onto `sys.path` as a side
effect (below), so scripts that only need `from src... import ...` to work
when run standalone (`.venv/bin/python scripts/foo.py`) can just import this
module instead of hand-rolling `sys.path.insert(0, str(Path(__file__)...))`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Python only auto-adds a standalone script's own directory (`scripts/`) to
# sys.path — `from src...` imports need the repo root too. Insert it here,
# once, as an import-time side effect so every caller gets it for free.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.config import CanaryConfig  # noqa: E402
from src.control.sessions import session_started_at  # noqa: E402
from src.preprocessing import discover_sessions  # noqa: E402
from src.profiles import resolve_profile_for_tooling, scope_config  # noqa: E402
from src.session import LIVE_SESSION_KINDS, iter_sessions  # noqa: E402


def add_common_args(parser: argparse.ArgumentParser, *, help: str = "Path to canary.yaml.") -> None:
    """Add the `--config` flag every script's `CanaryConfig.from_yaml` reads.

    `help` overrides the default text for scripts whose existing wording is
    worth keeping verbatim.
    """
    parser.add_argument("--config", type=Path, default=Path("canary.yaml"), help=help)


def add_profile_arg(parser: argparse.ArgumentParser) -> None:
    """Add the `--profile` flag that `scoped_models_dir` resolves output paths with."""
    parser.add_argument(
        "--profile",
        default=None,
        help="Vehicle profile slug to scope output paths to (see profiles.yaml). "
        "Default: the vehicle: block's profile. No VIN probe is performed.",
    )


def scoped_models_dir(config: CanaryConfig, profile_slug: str | None) -> Path:
    """The profile-scoped ``daemon.models_dir`` (``models/<slug>``) trainer output
    defaults to — the same tree the daemon's ModelManager watches, so a default
    train run lands where serving actually looks instead of the pre-profile
    unscoped ``models/<model_type>``. Offline resolution, same contract as
    `refit_behavior_models`: no VIN probe — ``--profile`` or the ``vehicle:``
    block's default profile.
    """
    if not config.profiles.enabled:
        return config.daemon.models_dir
    profile = resolve_profile_for_tooling(config, profile_slug)
    return scope_config(config, profile).daemon.models_dir


def load_config(args: argparse.Namespace) -> CanaryConfig:
    """`CanaryConfig.from_yaml(args.config)` — the one-liner every script repeats."""
    return CanaryConfig.from_yaml(args.config)


def filter_sessions_by_age(sessions: list[Path], days: int, now: datetime) -> list[Path]:
    """Keep session dirs whose timestamped name falls within `days` of `now`.

    The convention is `YYYY-MM-DDTHHMMSS` (UTC, matches `make_session_dir`).
    Non-conforming names are kept — better to over-include than to silently
    drop something the user explicitly placed under sessions/.
    """
    if days <= 0:
        return sessions
    cutoff = now - timedelta(days=days)
    kept: list[Path] = []
    for session in sessions:
        # The one session-time parser: a `_capture-…` prefix must not read as
        # "unknown format" and slip past the age filter.
        started = session_started_at(session)
        if started is None:
            kept.append(session)  # unknown format → keep
            continue
        if started >= cutoff:
            kept.append(session)
    return kept


def parse_since_argument(value: str) -> datetime:
    """Parse ``--since``'s value (Task 3.1, online-training-strategy.md).

    Accepts a bare date (``YYYY-MM-DD``) or a full ISO-8601 timestamp; a
    timestamp with no timezone is treated as UTC (matching every other
    timestamp convention in this repo — session dir names, manifests,
    ledger records). Raises ``argparse.ArgumentTypeError`` on anything else
    so a typo fails fast at the CLI boundary, not deep inside session
    filtering.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid --since value {value!r}: expected YYYY-MM-DD or ISO-8601"
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def filter_sessions_since(sessions: list[Path], since: datetime) -> list[Path]:
    """Keep session dirs timestamped at/after ``since`` (Task 3.1).

    The sliding-window counterpart to ``filter_sessions_by_age``'s
    relative ``--days``: ``since`` is an absolute cutoff (typically
    "now - window_days", recomputed fresh each scheduled tick, hence
    "rolling"), not a fixed lookback from whenever this function happens to
    run. Same non-conforming-name-is-kept posture as ``filter_sessions_by_age``.
    """
    kept: list[Path] = []
    for session in sessions:
        started = session_started_at(session)
        if started is None:
            kept.append(session)  # unknown format → keep
            continue
        if started >= since:
            kept.append(session)
    return kept


def discover_live_session_dirs(root: Path) -> list[Path]:
    """Sorted live-session dirs directly under `root` matching the naming convention.

    Distinct from `src.preprocessing.discover_sessions` (which is for
    training-corpus discovery and has its own underscore-prefix/telemetry
    rules) — this is the simpler "every well-formed live session" scan
    `label_sessions.py`/`fingerprint.py` both do post-hoc.

    Plain live sessions only (`_replay-*` / `_archive` / captures are
    deliberately excluded: these scripts post-process real drives), oldest
    first by parsed start time (`src.session.iter_sessions`, R01.1).
    """
    return iter_sessions(root, kinds=LIVE_SESSION_KINDS)


def resolve_sessions(args: argparse.Namespace, sessions_root: Path) -> list[Path]:
    """Explicit `args.sessions`, else discover under `sessions_root`.

    Age-filters to `--days` unless `--all` (`args.use_all_sessions`) was
    passed — the `retrain.py`/`refit_behavior_models.py` session-resolution
    preamble (both take a `sessions` positional + `--sessions-root` +
    `--days` + `--all`, byte-identical resolution logic).

    `--since` (Task 3.1, `retrain.py` only) is a third, mutually-exclusive
    selection mode: when `args.since` is set (and truthy), it wins over both
    `--days` and `--all` — a genuine sliding window anchored to an absolute
    cutoff rather than a lookback from "now".
    """
    sessions: list[Path] = list(args.sessions)
    if not sessions:
        sessions = discover_sessions(sessions_root)
        since = getattr(args, "since", None)
        if since is not None:
            sessions = filter_sessions_since(sessions, since)
        elif not args.use_all_sessions:
            sessions = filter_sessions_by_age(sessions, args.days, datetime.now(UTC))
    return sessions


__all__ = [
    "add_common_args",
    "add_profile_arg",
    "scoped_models_dir",
    "load_config",
    "discover_live_session_dirs",
    "filter_sessions_by_age",
    "filter_sessions_since",
    "parse_since_argument",
    "resolve_sessions",
]
