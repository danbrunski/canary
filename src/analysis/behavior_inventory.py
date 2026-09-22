"""Inventory of served behavior artifacts and what depends on them (B03.1).

Answers, offline and read-only, "which behavior artifact set is this, what
may each model do, and which detectors consume its columns?" — the record a
demotion, refit or rollout starts from:

* the local artifact (path, content hash, per-model identity from
  :func:`src.decode.behavior_models.artifact_identity`, serving status with
  lifecycle and permitted uses, validation scope);
* the profile policy governing it (``behavior_policy.yaml``), the validation
  criteria and accepted-baseline overrides beside it, and the calibration in
  force according to the tune registry;
* every IF/sequence manifest under the scoped models dir, with the
  ``deviation:*`` columns it consumes and any consumed column a demotion now
  withholds (``spec_producibility.behavior_feature_conflicts``);
* the deployed identity — ``unknown`` unless a saved ``GET /models/behavior``
  snapshot is supplied. This module never contacts the Pi or the daemon: an
  identity nobody observed is recorded as unknown, not assumed equal to the
  local one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.analysis.behavior_validation import file_sha256, validation_document_path
from src.analysis.tune_revisions import load_tune_revisions
from src.config import CanaryConfig
from src.decode.accepted_baselines import load_accepted_baselines
from src.decode.behavior_decoder import deviation_model_name
from src.decode.behavior_models import (
    BehaviorModels,
    artifact_identity,
    behavior_policy_path,
    load_behavior_models,
    policy_unmatched_entries,
    serving_status,
    validation_scope,
)
from src.models.spec_producibility import (
    behavior_feature_conflicts,
    behavior_fingerprint_provenance,
    manifest_behavior_fingerprints,
    manifest_feature_columns,
)

INVENTORY_VERSION = 1


def _models_block(models: BehaviorModels) -> list[dict[str, Any]]:
    return [
        {
            "artifact": artifact_identity(model),
            "kind": model.kind,
            "output": model.output,
            "operating_mode": model.operating_mode,
            "serving_status": serving_status(model).to_dict(),
            "validation_scope": validation_scope(model),
        }
        for model in models.models
    ]


def _manifest_block(path: Path, models: BehaviorModels | None) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text())
        columns = manifest_feature_columns(manifest)
        recorded = manifest_behavior_fingerprints(manifest)
    except (OSError, ValueError) as exc:
        return {"path": str(path), "error": str(exc)}
    consumed = sorted(
        {name for column in columns if (name := deviation_model_name(column)) is not None}
    )
    return {
        "path": str(path),
        "model_type": manifest.get("model_type"),
        "model_version": manifest.get("model_version"),
        "feature_count": len(columns),
        "behavior_models_consumed": consumed,
        "behavior_feature_conflicts": behavior_feature_conflicts(columns, models),
        # B03.4 — per consumed model: match / mismatch / unknown (no record).
        "behavior_fingerprint_provenance": behavior_fingerprint_provenance(
            columns, recorded, models
        ),
    }


def compare_deployed(
    local: BehaviorModels | None, snapshot: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Local-vs-deployed identity; ``unknown`` without an observed snapshot.

    ``snapshot`` is a saved ``GET /models/behavior`` payload. Models without
    an ``artifact`` block (a daemon older than B03.8) are compared by
    ``version`` and ``fit.fitted_at`` and say so.
    """
    if snapshot is None:
        return {
            "state": "unknown",
            "reason": "no deployed /models/behavior snapshot was observed; the inventory "
            "never contacts the Pi",
        }
    local_by_name = {m.name: artifact_identity(m) for m in (local.models if local else [])}
    rows: dict[str, dict[str, Any]] = {}
    for entry in snapshot.get("models", []):
        name = str(entry.get("name"))
        deployed = entry.get("artifact")
        mine = local_by_name.get(name)
        if mine is None:
            rows[name] = {"match": False, "reason": "deployed only"}
        elif isinstance(deployed, Mapping) and deployed.get("fingerprint"):
            rows[name] = {"match": deployed.get("fingerprint") == mine["fingerprint"]}
        else:
            fit = entry.get("fit") or {}
            rows[name] = {
                "match": entry.get("version") == mine["version"]
                and fit.get("fitted_at") == mine["fitted_at"],
                "reason": "compared by version/fitted_at (deployed daemon predates B03.8)",
            }
    for name in local_by_name.keys() - rows.keys():
        rows[name] = {"match": False, "reason": "local only"}
    return {"state": "observed", "models": dict(sorted(rows.items()))}


def build_inventory(
    config: CanaryConfig,
    *,
    config_path: Path | None = None,
    profile_slug: str | None = None,
    deployed_snapshot: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The B03.1 inventory for a *profile-scoped* ``config``."""
    artifact_path = config.daemon.behavior_models
    models = load_behavior_models(artifact_path) if artifact_path.exists() else None
    policy_path = behavior_policy_path(artifact_path)
    registry = load_tune_revisions(artifact_path.parent / "tune_revisions.yaml")
    current = registry.at(now or datetime.now(UTC)) if registry is not None else None
    baselines = load_accepted_baselines(config.daemon.accepted_baselines)
    manifests = sorted(config.daemon.models_dir.glob("**/model.manifest.json"))
    return {
        "inventory_version": INVENTORY_VERSION,
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "profile": profile_slug,
        "config": {
            "path": None if config_path is None else str(config_path),
            "sha256": None if config_path is None else file_sha256(config_path),
            "operating_modes": sorted(config.preprocessing.operating_modes),
        },
        "local_artifact": {
            "path": str(artifact_path),
            "sha256": file_sha256(artifact_path),
            "schema_version": models.schema_version if models is not None else None,
            "models": _models_block(models) if models is not None else [],
        },
        "policy": {
            "path": str(policy_path),
            "sha256": file_sha256(policy_path),
            "unmatched_entries": policy_unmatched_entries(models) if models is not None else [],
        },
        "validation_criteria": {
            "path": str(validation_document_path(artifact_path)),
            "sha256": file_sha256(validation_document_path(artifact_path)),
        },
        "calibration": {
            "registry_sha256": file_sha256(artifact_path.parent / "tune_revisions.yaml"),
            "current_revision": current.revision if current is not None else None,
            "flashed_at": current.flashed_at.isoformat() if current is not None else None,
        },
        "accepted_baseline_overrides": [
            {
                "target": override.target,
                "operating_mode": override.operating_mode,
                "review_by": override.provenance.review_by.isoformat(),
            }
            for override in baselines.overrides
        ],
        "dependent_manifests": [_manifest_block(path, models) for path in manifests],
        "deployed": compare_deployed(models, deployed_snapshot),
    }


__all__ = ["INVENTORY_VERSION", "build_inventory", "compare_deployed"]
