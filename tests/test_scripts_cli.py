"""`scripts/_cli.py` shared-preamble helpers.

`scoped_models_dir` is what makes every trainer CLI default its artifact
output into the profile-scoped `models/<slug>/` tree the daemon actually
watches — a regression here silently resurrects the pre-profile unscoped
`models/<model_type>` output location.
"""

from pathlib import Path

from scripts._cli import scoped_models_dir
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
