"""Which behavior models a capture profile would starve (R09.6).

Moved out of ``src.collection.capture_profiles``: answering it needs the
behavior models, which sit above collection. The profile's own question,
whether it can supply a channel, stays there (:func:`channel_satisfied`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.behavior.behavior_eligibility import required_channel_groups
from src.behavior.behavior_models import load_behavior_models
from src.collection.capture_profiles import (
    DEFAULT_PROFILE_NAME,
    CaptureProfile,
    channel_satisfied,
)


def model_input_warnings(
    profile: CaptureProfile,
    behavior_models_path: Path,
) -> list[dict[str, Any]]:
    """Name active behavior models starved by a saved default profile."""
    if profile.name != DEFAULT_PROFILE_NAME or not behavior_models_path.exists():
        return []
    warnings: list[dict[str, Any]] = []
    for model in load_behavior_models(behavior_models_path).models:
        # Every channel the serving contract reads (B01.4): axes, output,
        # rate-gate bases, engine-state/fuel-status/cold-start channels and
        # any-of required context groups.
        missing = sorted(
            "|".join(group)
            for group in required_channel_groups(model)
            if not any(channel_satisfied(channel, profile, frozenset()) for channel in group)
        )
        if missing:
            warnings.append({"model": model.name, "missing_channels": missing})
    return warnings


__all__ = ["model_input_warnings"]
