"""Repo-rooted default paths for the version-controlled data catalogs (R01.2).

The same data files used to have several defaults, some rooted at the repo
(``Path(__file__).resolve().parents[2] / "data" / ...``) and some at the
working directory (a bare relative ``data/dids.yaml``). A process started anywhere
but the repo root silently loaded nothing from the second kind: the training
feature pipeline dropped every ``did:`` feature, and its cache key hashed a
*different* constant, so it could not tell.

These files ship in the clone, so their defaults are anchored to this file,
never to the working directory. Leaf module: imports nothing from ``src``.
"""

from __future__ import annotations

from pathlib import Path

#: The checkout root (``src/paths.py`` -> up 2).
REPO_ROOT = Path(__file__).resolve().parents[1]
#: The version-controlled ``data/`` directory in the checkout.
DATA_DIR = REPO_ROOT / "data"

#: The Mode 22 DID catalog (and the ``memory:`` RAM-channel catalog beside it).
DIDS_YAML = DATA_DIR / "dids.yaml"
#: Hand-edited broadcast CAN signal bindings.
BINDINGS_YAML = DATA_DIR / "bindings.yaml"
#: Directory of ``.dbc`` files decoded alongside the bindings.
DBC_DIR = DATA_DIR / "dbc"
#: Scanned directory of YAML test-session profiles for the Test panel.
TEST_PROFILES_DIR = DATA_DIR / "test_profiles"

# Runtime state (gitignored, written by the daemon and tools). Anchored to the
# checkout for the same reason as the catalogs: a tool started from another
# directory must read and write the daemon's files, not a stray ``data/`` of
# its own. The systemd units ``cd`` to the clone, so on the Pi these are the
# paths the old working-directory-relative defaults already resolved to; a
# ``canary.yaml`` value (``/data/canary/...``) still wins.

#: Control-plane state: jobs, ledgers, audit logs, trip odometer, catalog.
CONTROL_DIR = DATA_DIR / "control"
#: The anomaly event log directory.
ANOMALIES_DIR = DATA_DIR / "anomalies"
#: Analysis outputs (reports, LLM-assist cards).
ANALYSES_DIR = DATA_DIR / "analyses"

__all__ = [
    "ANALYSES_DIR",
    "ANOMALIES_DIR",
    "BINDINGS_YAML",
    "CONTROL_DIR",
    "DATA_DIR",
    "DBC_DIR",
    "DIDS_YAML",
    "REPO_ROOT",
    "TEST_PROFILES_DIR",
]
