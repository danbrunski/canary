"""Shared model-manifest schema (Task 5.15).

Both the Isolation Forest (``IsolationForestManifest``) and the LSTM
autoencoder (``LSTMAutoencoderManifest``) already serialize JSON manifests
alongside their joblib / Torch artifacts. This module formalizes the
*common* surface so the dashboard's ``/status`` endpoint (Task 5.3),
``ModelManager`` (Task 5.16, hot-swap), and any future retraining CLI
(Task 5.17) can introspect either kind through one schema rather than
sniffing the file by name.

``ModelManifestCommon`` lists the fields every CANary model manifest is
expected to carry. ``validate_manifest_common(payload)`` accepts the
``to_dict()`` output of either trainer (or a freshly-loaded JSON dict),
checks the invariants, and returns the typed view. Detector-specific
extras stay in ``extras`` — the schema is forward-compatible: adding a
field to a per-trainer manifest doesn't require a schema-version bump
here, only consumers who actually use the new field need to opt in.

A bumped ``artifact_format_version`` does require code changes: it's the
load-time guard that lets a future ``ModelManager`` refuse to deserialize
an artifact whose on-disk format the running daemon can't speak.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Bumped when the *shape* of the manifest changes incompatibly (a field is
#: removed, a type changes meaning). Per-trainer ``artifact_format_version``
#: tracks the artifact's on-disk format separately; this one tracks the
#: manifest's schema. Increment in lockstep with a migration plan.
CURRENT_MANIFEST_FORMAT_VERSION = 1

#: Set of model_type values both trainers emit today. Keep this in sync with
#: ``AnomalyEvent.AnomalySource`` so dashboard payloads round-trip cleanly.
KNOWN_MODEL_TYPES: tuple[str, ...] = ("isolation_forest", "lstm_autoencoder")


class ModelManifestCommon(BaseModel):
    """The fields every CANary model manifest is contractually obliged to carry.

    Detector-specific keys (``modes`` for the IF, ``thresholds`` for the
    LSTM) flow through ``extras`` so the schema doesn't ossify; callers
    who need them pull them out from there. The shared fields below are
    what hot-swap (5.16), the dashboard (5.3), and the eventual
    drift-comparison tooling can all rely on.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    artifact_format_version: int = Field(ge=1)
    model_type: str  # see KNOWN_MODEL_TYPES for the ones we ship today
    model_version: str  # opaque to the manifest; conventionally <type>_<timestamp>
    created_at: str  # ISO-8601 UTC
    training_data_hash: str  # opaque hash; for cache invalidation, not validation
    training_start_timestamp: str | None = None
    training_end_timestamp: str | None = None
    training_sessions: list[str] = Field(default_factory=list)
    input_rows: int = Field(ge=0)
    hyperparams: dict[str, Any] = Field(default_factory=dict)
    schema_: dict[str, Any] = Field(
        default_factory=dict, alias="schema"
    )  # feature column list + scaler info; shape varies per trainer
    extras: dict[str, Any] = Field(default_factory=dict)


class ManifestValidationError(ValueError):
    """Raised when a manifest payload doesn't satisfy the common contract."""


class FeatureSpecValidationError(ValueError):
    """Raised when a model manifest's feature spec is unsafe to score."""


def validate_manifest_common(
    payload: Mapping[str, Any],
    *,
    allowed_model_types: tuple[str, ...] | None = KNOWN_MODEL_TYPES,
    accept_format_version: tuple[int, ...] = (CURRENT_MANIFEST_FORMAT_VERSION,),
) -> ModelManifestCommon:
    """Validate ``payload`` against the common schema.

    ``allowed_model_types`` — restricts ``model_type`` to a known set. Pass
    ``None`` to skip the check (e.g. when a future trainer ships before this
    module gains its name); the schema-level validation still runs.
    ``accept_format_version`` — which manifest format versions this caller
    can speak. The default is "only the current one"; tooling that needs to
    read older artifacts can widen this.

    Raises :class:`ManifestValidationError` with a single human-readable
    message — the only exception type any caller needs to catch.
    """
    # split known shared keys from detector-specific extras
    fields = ModelManifestCommon.model_fields
    known: set[str] = set()
    for name, field in fields.items():
        known.add(field.alias if field.alias is not None else name)
    base: dict[str, Any] = {k: v for k, v in payload.items() if k in known}
    extras: dict[str, Any] = {k: v for k, v in payload.items() if k not in known}
    base.setdefault("extras", {}).update(extras)

    try:
        manifest = ModelManifestCommon.model_validate(base)
    except Exception as exc:  # pydantic.ValidationError, ValueError, …
        raise ManifestValidationError(f"manifest validation failed: {exc}") from exc

    if manifest.artifact_format_version not in accept_format_version:
        raise ManifestValidationError(
            f"unsupported artifact_format_version {manifest.artifact_format_version!r}; "
            f"this code accepts {accept_format_version}"
        )
    if allowed_model_types is not None and manifest.model_type not in allowed_model_types:
        raise ManifestValidationError(
            f"unknown model_type {manifest.model_type!r}; expected one of {allowed_model_types}"
        )
    return manifest


def validate_model_feature_spec(
    schema: Mapping[str, Any],
    *,
    context: str = "model feature spec",
) -> tuple[str, ...]:
    """Validate the feature contract persisted in a model manifest.

    The common manifest schema intentionally stays permissive so old tooling can
    still read historical artifacts. Model loading and hot-swap paths use this
    stricter gate before a detector can score live rows.
    """
    if not isinstance(schema, Mapping):
        raise FeatureSpecValidationError(f"{context}: schema must be a mapping")

    feature_columns = _validate_feature_columns(schema, context=context)
    version = schema.get("feature_spec_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise FeatureSpecValidationError(
            f"{context}: feature_spec_version must be a positive integer"
        )

    dtypes = schema.get("feature_dtypes")
    if not isinstance(dtypes, Mapping):
        raise FeatureSpecValidationError(f"{context}: feature_dtypes must be present")
    missing_dtypes = [column for column in feature_columns if column not in dtypes]
    if missing_dtypes:
        raise FeatureSpecValidationError(
            f"{context}: feature_dtypes missing columns: {', '.join(missing_dtypes)}"
        )

    health = schema.get("feature_health")
    if not isinstance(health, Mapping):
        raise FeatureSpecValidationError(f"{context}: feature_health must be present")
    _validate_feature_health_summary(health, feature_columns, context=context)
    return feature_columns


def load_manifest_common(
    path: Path,
    *,
    allowed_model_types: tuple[str, ...] | None = KNOWN_MODEL_TYPES,
) -> ModelManifestCommon:
    """Read a JSON manifest from disk and validate it via the common schema."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(f"could not read manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestValidationError(
            f"manifest at {path} must be a JSON object, got {type(payload).__name__}"
        )
    return validate_manifest_common(payload, allowed_model_types=allowed_model_types)


def schemas_compatible(
    a: ModelManifestCommon,
    b: ModelManifestCommon,
    *,
    feature_columns_key: str = "feature_columns",
) -> bool:
    """Return True if two manifests describe the same input schema.

    Used by the hot-swap path (5.16): swapping a live model only makes sense
    when the new model's expected feature columns match the running model's.
    Default key matches both trainers' current convention; pass a different
    key if a future trainer parks its column list elsewhere.
    """
    a_cols = a.schema_.get(feature_columns_key)
    b_cols = b.schema_.get(feature_columns_key)
    if a_cols is None or b_cols is None:
        return False
    return list(a_cols) == list(b_cols)


def schema_serviceable(
    running: ModelManifestCommon,
    candidate: ModelManifestCommon,
    *,
    feature_columns_key: str = "feature_columns",
) -> bool:
    """Return True if a hot-swap to ``candidate`` is safe without a restart.

    The conservative rule (``schemas_compatible``) demands identical feature
    columns, which forces a daemon restart for any retrain that changes the
    feature set (e.g. dropping monotonic-counter channels). But the live
    pipeline that fed ``running`` produces *at least* ``running``'s columns, and
    the daemon reconciles each frame to the model's spec at scoring time
    (dropping extras, filling missing). So a candidate whose columns are a
    **subset** of the running model's is guaranteed serviceable — the pipeline
    already produces every column it needs. Schema *expansions* (new columns the
    running pipeline may not produce) still fall back to a restart, where a fresh
    load can't silently serve unsatisfiable features.
    """
    running_cols = running.schema_.get(feature_columns_key)
    candidate_cols = candidate.schema_.get(feature_columns_key)
    if running_cols is None or candidate_cols is None:
        return False
    return set(candidate_cols) <= set(running_cols)


def _validate_feature_columns(schema: Mapping[str, Any], *, context: str) -> tuple[str, ...]:
    raw_columns = schema.get("feature_columns")
    if not isinstance(raw_columns, list | tuple):
        raise FeatureSpecValidationError(f"{context}: feature_columns must be a list")
    columns = tuple(str(column) for column in raw_columns)
    if not columns:
        raise FeatureSpecValidationError(f"{context}: feature_columns must not be empty")
    blanks = [column for column in columns if not column]
    if blanks:
        raise FeatureSpecValidationError(f"{context}: feature_columns contains blank names")
    duplicates = sorted({column for column in columns if columns.count(column) > 1})
    if duplicates:
        raise FeatureSpecValidationError(
            f"{context}: duplicate feature_columns: {', '.join(duplicates)}"
        )
    return columns


def _validate_feature_health_summary(
    health: Mapping[str, Any],
    feature_columns: tuple[str, ...],
    *,
    context: str,
) -> None:
    selected_count = health.get("selected_feature_count")
    if selected_count != len(feature_columns):
        raise FeatureSpecValidationError(
            f"{context}: feature_health selected_feature_count {selected_count!r} "
            f"does not match {len(feature_columns)} feature columns"
        )

    all_nan = [str(column) for column in health.get("all_nan_columns", [])]
    if all_nan:
        raise FeatureSpecValidationError(
            f"{context}: selected feature columns are all-NaN: {', '.join(all_nan)}"
        )
    non_finite = [str(column) for column in health.get("non_finite_columns", [])]
    if non_finite:
        raise FeatureSpecValidationError(
            f"{context}: selected feature columns contain non-finite values: "
            + ", ".join(non_finite)
        )

    columns_health = health.get("columns")
    if not isinstance(columns_health, Mapping):
        raise FeatureSpecValidationError(f"{context}: feature_health columns must be present")
    missing_health = [column for column in feature_columns if column not in columns_health]
    if missing_health:
        raise FeatureSpecValidationError(
            f"{context}: feature_health missing columns: {', '.join(missing_health)}"
        )

    for column in feature_columns:
        column_health = columns_health[column]
        if not isinstance(column_health, Mapping):
            raise FeatureSpecValidationError(
                f"{context}: feature_health for {column!r} must be a mapping"
            )
        non_null = column_health.get("non_null_count")
        finite_count = column_health.get("finite_count")
        non_finite_count = column_health.get("non_finite_count")
        if non_null == 0:
            raise FeatureSpecValidationError(f"{context}: feature {column!r} has no values")
        if finite_count == 0:
            raise FeatureSpecValidationError(f"{context}: feature {column!r} has no finite values")
        if non_finite_count not in (0, None):
            raise FeatureSpecValidationError(
                f"{context}: feature {column!r} contains non-finite values"
            )


__all__ = [
    "CURRENT_MANIFEST_FORMAT_VERSION",
    "FeatureSpecValidationError",
    "KNOWN_MODEL_TYPES",
    "ManifestValidationError",
    "ModelManifestCommon",
    "load_manifest_common",
    "schemas_compatible",
    "schema_serviceable",
    "validate_model_feature_spec",
    "validate_manifest_common",
]


# Type alias for typing-only callers that don't want to import Literal directly.
ModelType = Literal["isolation_forest", "lstm_autoencoder"]
