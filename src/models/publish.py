"""Staging and publishing a retrained artifact behind the alert-budget gate (A06.5).

An alert-role artifact trains into a ``_staging-<name>`` sibling of the
directory the daemon serves from, and reaches the served directory only
through the alert-budget gate: the daemon's ``ModelManager`` can never
hot-swap an artifact the gate then refuses. The verdict travels with the
binary (operator decision 2026-09-23): models reach the Pi by scp and
``data/analyses`` never does, so the daemon serves an ``alert`` role only
beside an accepted verdict that names this artifact's sha256
(:mod:`src.models.role_gate` is the reader).

``scripts/retrain.py`` is the one caller; the rules live here so they are not
a script's private functions.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from src.config import CanaryConfig
from src.models.alert_budget import (
    ALERT_BUDGET_VERDICT_SUFFIX,
    AlertBudgetCheck,
    file_sha256,
    verdict_record,
    write_verdict,
)

log = logging.getLogger(__name__)

#: The event source each retrained artifact publishes under
#: (``DetectorRolesConfig.by_source``): both sequence backbones fill the one
#: ``lstm_autoencoder`` slot.
ISOLATION_FOREST_SOURCE = "isolation_forest"
SEQUENCE_SOURCE = "lstm_autoencoder"

#: Prefix of the directory a refused artifact is set aside in, beside the
#: served one. ``_``-prefixed, so neither the ModelManager nor training
#: discovery reads it.
REFUSED_PREFIX = "_refused-alert-budget-"


def staging_dir(served: Path) -> Path:
    """Where an alert-role artifact trains before the budget gate: a
    ``_``-prefixed sibling of the served directory, which neither the
    ModelManager nor training discovery reads. Emptied first, so a stale
    artifact from an earlier run can never be the one that gets published."""
    staging = served.parent / f"_staging-{served.name}"
    if staging.exists():
        shutil.rmtree(staging)
    return staging


def sequence_artifact_name(config: CanaryConfig, sequence_model_type: str) -> str:
    """The sequence binary's file name (``model.pt``) for ``sequence_model_type``."""
    if sequence_model_type == "tcn_autoencoder":
        return Path(config.daemon.tcn_artifact).name
    return Path(config.daemon.lstm_artifact).name


def artifact_name(config: CanaryConfig, source: str, sequence_model_type: str) -> str:
    """The binary a gated source's verdict names (``model.joblib`` / ``model.pt``)."""
    if source == ISOLATION_FOREST_SOURCE:
        return Path(config.daemon.isolation_forest_artifact).name
    return sequence_artifact_name(config, sequence_model_type)


def write_artifact_verdict(check: AlertBudgetCheck, source: str, artifact: Path) -> None:
    """The gate's verdict for ``source``, beside ``artifact`` and naming its sha256."""
    if not artifact.exists():
        log.warning("no artifact at %s to write an alert-budget verdict beside", artifact)
        return
    write_verdict(
        artifact,
        verdict_record(check, source=source, artifact_path=artifact, sha256=file_sha256(artifact)),
    )


def publish_staged(staged: Path, served: Path) -> None:
    """Move a gated artifact into the served directory: the alert-budget
    verdict first, then the binaries, the manifest last. A poller then never
    pairs a new manifest with an old binary, and never sees the new binary
    without its verdict (which would serve it as evidence until the next
    poll)."""
    served.mkdir(parents=True, exist_ok=True)

    def order(path: Path) -> int:
        if path.name.endswith(ALERT_BUDGET_VERDICT_SUFFIX):
            return 0
        return 2 if path.name.endswith(".manifest.json") else 1

    files = sorted(staged.iterdir(), key=order)
    for path in files:
        path.replace(served / path.name)
    staged.rmdir()


def set_aside_refused(staged: Path, served: Path) -> Path:
    """Keep a refused artifact for inspection, out of every served path."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = served.parent / f"{REFUSED_PREFIX}{stamp}-{served.name}"
    staged.replace(target)
    return target


__all__ = [
    "ISOLATION_FOREST_SOURCE",
    "REFUSED_PREFIX",
    "SEQUENCE_SOURCE",
    "artifact_name",
    "publish_staged",
    "sequence_artifact_name",
    "set_aside_refused",
    "staging_dir",
    "write_artifact_verdict",
]
