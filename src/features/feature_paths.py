"""Where the feature pipeline looks for its repo-rooted catalogs and caches."""

from __future__ import annotations

from pathlib import Path

from src.paths import BINDINGS_YAML, DATA_DIR, DIDS_YAML

#: Default location of the hand-edited CAN signal bindings file -- the repo's
#: own, never the working directory's (R01.2).
DEFAULT_BINDINGS_PATH = BINDINGS_YAML

#: Default location of the Mode-22 DID map (data/dids.yaml), repo-rooted. Used
#: to decode raw ``DID_<hex>`` poll readings into named ``did:<NAME>`` signals.
DEFAULT_DIDS_PATH = DIDS_YAML

#: Default retrain-time cache for model-ready feature frames.
DEFAULT_FEATURE_CACHE_DIR = DATA_DIR / "cache" / "feature_frames"

#: Repo-root-anchored serving capture-profile catalog, used when no config is
#: threaded (same anchoring as ``engineering.DEFAULT_BINDINGS_PATH``).
DEFAULT_CAPTURE_PROFILES_PATH = Path(__file__).resolve().parents[2] / "data/capture_profiles.yaml"
