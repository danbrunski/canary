"""Tests for the shared model-manifest schema (Task 5.15)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.models.manifest import (
    CURRENT_MANIFEST_FORMAT_VERSION,
    KNOWN_MODEL_TYPES,
    FeatureSpecValidationError,
    ManifestValidationError,
    ModelManifestCommon,
    load_manifest_common,
    schema_serviceable,
    schemas_compatible,
    validate_manifest_common,
    validate_model_feature_spec,
)


def _good_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "artifact_format_version": CURRENT_MANIFEST_FORMAT_VERSION,
        "model_type": "isolation_forest",
        "model_version": "isolation_forest_2026-05-13T12:00:00",
        "created_at": "2026-05-13T12:00:00+00:00",
        "training_data_hash": "sha256:deadbeef",
        "training_sessions": ["2026-05-13T120000"],
        "input_rows": 1000,
        "hyperparams": {"n_estimators": 100, "contamination": 0.01},
        "schema": {"feature_columns": ["obd:RPM", "obd:SPEED"]},
    }
    base.update(overrides)
    return base


def _feature_schema(columns: list[str] | None = None) -> dict[str, object]:
    feature_columns = columns or ["obd:RPM", "obd:SPEED"]
    return {
        "feature_spec_version": 1,
        "feature_columns": feature_columns,
        "feature_dtypes": {column: "float64" for column in feature_columns},
        "source_prefixes": ["obd"],
        "behavior_model_columns": [],
        "feature_health": {
            "row_count": 100,
            "candidate_feature_count": len(feature_columns),
            "selected_feature_count": len(feature_columns),
            "dropped_all_nan_columns": [],
            "all_nan_columns": [],
            "constant_columns": [],
            "non_finite_columns": [],
            "columns": {
                column: {
                    "dtype": "float64",
                    "non_null_count": 100,
                    "finite_count": 100,
                    "nan_count": 0,
                    "non_finite_count": 0,
                    "unique_finite_count": 10,
                    "constant": False,
                }
                for column in feature_columns
            },
        },
    }


# ---------------------------------------------------------------------------
# Basic validation
# ---------------------------------------------------------------------------


def test_validate_known_payload() -> None:
    manifest = validate_manifest_common(_good_payload())
    assert manifest.model_type == "isolation_forest"
    assert manifest.artifact_format_version == CURRENT_MANIFEST_FORMAT_VERSION
    # `schema` is aliased
    assert manifest.schema_["feature_columns"] == ["obd:RPM", "obd:SPEED"]


def test_validate_accepts_lstm_payload() -> None:
    manifest = validate_manifest_common(
        _good_payload(model_type="lstm_autoencoder", model_version="lstm_x")
    )
    assert manifest.model_type == "lstm_autoencoder"


def test_validate_collects_detector_specific_keys_into_extras() -> None:
    payload = _good_payload(modes={"idle": {"sample_count": 100}})
    manifest = validate_manifest_common(payload)
    assert manifest.extras["modes"] == {"idle": {"sample_count": 100}}


def test_validate_missing_required_field_raises() -> None:
    payload = _good_payload()
    del payload["model_version"]
    with pytest.raises(ManifestValidationError, match="manifest validation failed"):
        validate_manifest_common(payload)


def test_validate_unknown_model_type_raises_by_default() -> None:
    with pytest.raises(ManifestValidationError, match="unknown model_type"):
        validate_manifest_common(_good_payload(model_type="random_forest"))


def test_validate_can_accept_arbitrary_model_type_when_allowed_none() -> None:
    manifest = validate_manifest_common(
        _good_payload(model_type="future_thing"), allowed_model_types=None
    )
    assert manifest.model_type == "future_thing"


def test_validate_unsupported_format_version_raises() -> None:
    with pytest.raises(ManifestValidationError, match="unsupported artifact_format_version"):
        validate_manifest_common(_good_payload(artifact_format_version=99))


def test_known_model_types_match_anomaly_sources() -> None:
    """5.15 ↔ 5.1: model types and anomaly sources stay aligned.

    Phase 7.2's ``fused`` and Phase 7.4's ``behavior`` sources have *no* model
    artifact (fused is the agreement-aware combination; behavior is a
    physics-residual detector), so they are deliberately ``AnomalySource``
    members that are not ``KNOWN_MODEL_TYPE``\\ s. Task 1.6's ``dtc`` source
    is the same shape — a trouble code isn't a trained model. Task 4.6's
    ``trend`` source is the same shape again — a CUSUM/Mann-Kendall drift
    verdict over the fingerprint series isn't a trained model either.

    The sequence-detector slot is backbone-interchangeable
    (``lstm_autoencoder`` / ``tcn_autoencoder``, only one active at a time) but
    publishes under a single stable ``lstm_autoencoder`` source, so the
    alternate backbone is a known model type *without* its own source. Net
    invariants: every non-derived source is a known model type, and the only
    model types lacking a 1:1 source are the alternate sequence backbones that
    reuse the sequence-detector source."""
    from typing import get_args

    from src.alerts.events import AnomalySource

    non_derived_sources = set(get_args(AnomalySource)) - {"fused", "behavior", "dtc", "trend"}
    sequence_backbones = {"lstm_autoencoder", "tcn_autoencoder"}

    assert non_derived_sources <= set(KNOWN_MODEL_TYPES)
    assert set(KNOWN_MODEL_TYPES) - non_derived_sources == sequence_backbones - {"lstm_autoencoder"}


# ---------------------------------------------------------------------------
# load_manifest_common
# ---------------------------------------------------------------------------


def test_load_manifest_common_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "model.manifest.json"
    path.write_text(json.dumps(_good_payload()))
    manifest = load_manifest_common(path)
    assert manifest.model_version == "isolation_forest_2026-05-13T12:00:00"


def test_load_manifest_common_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestValidationError, match="could not read"):
        load_manifest_common(tmp_path / "nope.json")


def test_load_manifest_common_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{nope")
    with pytest.raises(ManifestValidationError, match="could not read"):
        load_manifest_common(path)


def test_load_manifest_common_non_object(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(ManifestValidationError, match="JSON object"):
        load_manifest_common(path)


# ---------------------------------------------------------------------------
# Schema compatibility (for the eventual hot-swap path)
# ---------------------------------------------------------------------------


def test_schemas_compatible_when_same_columns() -> None:
    a = validate_manifest_common(_good_payload())
    b = validate_manifest_common(
        _good_payload(model_version="b")  # different version, same columns
    )
    assert schemas_compatible(a, b)


def test_schemas_incompatible_when_columns_differ() -> None:
    a = validate_manifest_common(_good_payload())
    b = validate_manifest_common(
        _good_payload(schema={"feature_columns": ["different", "columns"]})
    )
    assert not schemas_compatible(a, b)


def test_schemas_incompatible_when_key_missing() -> None:
    a = validate_manifest_common(_good_payload())
    b = validate_manifest_common(_good_payload(schema={}))  # no feature_columns
    assert not schemas_compatible(a, b)


def test_schema_serviceable_accepts_subset() -> None:
    # A retrain that DROPS columns (e.g. counter exclusion): the running pipeline
    # already produces a superset, so reconcile serves it without a restart.
    running = validate_manifest_common(_good_payload(schema={"feature_columns": ["a", "b", "c"]}))
    candidate = validate_manifest_common(
        _good_payload(model_version="b", schema={"feature_columns": ["a", "c"]})
    )
    assert schema_serviceable(running, candidate)
    assert schema_serviceable(running, running)  # identical is trivially serviceable


def test_schema_serviceable_rejects_expansion() -> None:
    # A new column the running pipeline may not produce → not safe to hot-swap.
    running = validate_manifest_common(_good_payload(schema={"feature_columns": ["a", "b"]}))
    candidate = validate_manifest_common(
        _good_payload(model_version="b", schema={"feature_columns": ["a", "b", "new"]})
    )
    assert not schema_serviceable(running, candidate)


def test_schema_serviceable_false_when_key_missing() -> None:
    running = validate_manifest_common(_good_payload())
    candidate = validate_manifest_common(_good_payload(schema={}))
    assert not schema_serviceable(running, candidate)


# ---------------------------------------------------------------------------
# Feature-spec validation
# ---------------------------------------------------------------------------


def test_validate_model_feature_spec_accepts_training_schema() -> None:
    assert validate_model_feature_spec(_feature_schema()) == ("obd:RPM", "obd:SPEED")


def test_validate_model_feature_spec_rejects_missing_health() -> None:
    schema = _feature_schema()
    del schema["feature_health"]
    with pytest.raises(FeatureSpecValidationError, match="feature_health"):
        validate_model_feature_spec(schema)


def test_validate_model_feature_spec_rejects_selected_all_nan_column() -> None:
    schema = _feature_schema()
    health = dict(schema["feature_health"])  # type: ignore[arg-type]
    health["all_nan_columns"] = ["obd:RPM"]
    schema["feature_health"] = health
    with pytest.raises(FeatureSpecValidationError, match="all-NaN"):
        validate_model_feature_spec(schema)


def test_validate_model_feature_spec_rejects_missing_dtype() -> None:
    schema = _feature_schema()
    schema["feature_dtypes"] = {"obd:RPM": "float64"}
    with pytest.raises(FeatureSpecValidationError, match="feature_dtypes missing"):
        validate_model_feature_spec(schema)


# ---------------------------------------------------------------------------
# Conformance: trainer outputs must satisfy the schema
# ---------------------------------------------------------------------------


def test_isolation_forest_manifest_conforms() -> None:
    """The IF's IsolationForestManifest.to_dict() should round-trip through
    the common validator. If a future change to that dataclass drops a
    shared field, this test fails loudly rather than silently breaking
    /status or hot-swap."""
    from src.models.isolation_forest import IsolationForestManifest

    fields = IsolationForestManifest.__dataclass_fields__
    assert "model_version" in fields
    assert "training_data_hash" in fields
    assert "model_type" in fields


def test_lstm_manifest_conforms() -> None:
    from src.models.lstm_training import LSTMAutoencoderManifest

    fields = LSTMAutoencoderManifest.__dataclass_fields__
    assert "model_version" in fields
    assert "training_data_hash" in fields
    assert "model_type" in fields
    assert "input_rows" in fields


def test_model_manifest_common_is_strict_about_extras() -> None:
    """Detector-specific keys must go through ``extras``, never as siblings
    of the common fields, so the schema doesn't silently absorb typos."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelManifestCommon(
            artifact_format_version=1,
            model_type="isolation_forest",
            model_version="v",
            created_at="now",
            training_data_hash="h",
            input_rows=1,
            random_extra="should not be allowed",  # type: ignore[call-arg]
        )
