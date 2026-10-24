"""`scripts/_cli.py` shared-preamble helpers.

`scoped_models_dir` is what makes every trainer CLI default its artifact
output into the profile-scoped `models/<slug>/` tree the daemon actually
watches — a regression here silently resurrects the pre-profile unscoped
`models/<model_type>` output location.
"""

import argparse
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from scripts._cli import (
    filter_sessions_since,
    parse_since_argument,
    resolve_sessions,
    scoped_models_dir,
)
from src.config import CanaryConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _config() -> CanaryConfig:
    return CanaryConfig.from_yaml(EXAMPLE_YAML)


def test_default_profile_scopes_models_dir():
    config = _config()
    # The example config's vehicle: block (Nissan Xterra 2008) is the default
    # profile when no slug is given — no VIN probe, no registry write.
    assert scoped_models_dir(config, None) == config.daemon.models_dir / "nissan-xterra-2008"


def test_explicit_slug_wins():
    config = _config()
    assert scoped_models_dir(config, "other-truck") == config.daemon.models_dir / "other-truck"


def test_profiles_disabled_falls_back_to_unscoped():
    config = _config()
    config = config.model_copy(
        update={"profiles": config.profiles.model_copy(update={"enabled": False})}
    )
    assert scoped_models_dir(config, None) == config.daemon.models_dir


# ---------------------------------------------------------------------------
# --since / rolling-window selection (Task 3.1, online-training-strategy.md)
# ---------------------------------------------------------------------------


def test_parse_since_argument_bare_date_is_utc_midnight():
    parsed = parse_since_argument("2026-07-01")
    assert parsed == datetime(2026, 7, 1, tzinfo=UTC)


def test_parse_since_argument_naive_datetime_assumed_utc():
    parsed = parse_since_argument("2026-07-01T12:30:00")
    assert parsed == datetime(2026, 7, 1, 12, 30, tzinfo=UTC)


def test_parse_since_argument_rejects_garbage():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_since_argument("not-a-date")


def test_filter_sessions_since_keeps_at_or_after_cutoff(tmp_path: Path) -> None:
    old = tmp_path / "2026-06-01T120000"
    new = tmp_path / "2026-07-05T120000"
    unknown = tmp_path / "not-a-timestamp"
    for p in (old, new, unknown):
        p.mkdir()
    since = datetime(2026, 7, 1, tzinfo=UTC)
    kept = filter_sessions_since([old, new, unknown], since)
    # `old` is dropped; `new` and the non-conforming name are kept.
    assert kept == [new, unknown]


def test_resolve_sessions_since_wins_over_days(tmp_path: Path) -> None:
    old = tmp_path / "2026-06-01T120000"
    new = tmp_path / "2026-07-05T120000"
    for p in (old, new):
        p.mkdir()
        (p / "telemetry-0000.parquet").write_bytes(b"not empty")
    args = cast(
        argparse.Namespace,
        SimpleNamespace(
            sessions=[],
            use_all_sessions=False,
            days=1,  # would exclude both if --since weren't set
            since=datetime(2026, 7, 1, tzinfo=UTC),
        ),
    )
    assert resolve_sessions(args, tmp_path) == [new]
