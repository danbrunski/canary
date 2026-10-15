"""The persisted feature contract: spec creation, health, schema alignment and
reconciliation of a frame to a manifest's spec.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.behavior.behavior_decoder import deviation_model_name
from src.behavior.behavior_models import BehaviorModels, artifact_fingerprint
from src.columns import MODE_COLUMN, base_channel
from src.config import CanaryConfig, require_profile_scoped
from src.features.column_health import health_of_column, is_constant_column
from src.features.feature_policy import (
    FeaturePolicy,
    candidate_feature_columns,
    feature_policy_exclusions,
    resolve_feature_policy,
)
from src.features.feature_selection import low_coverage_columns
from src.preprocessing.resampler import resample_config_for
from src.telemetry import source_of

log = logging.getLogger(__name__)


FEATURE_SPEC_VERSION = 1


def align_features_to_schema(
    features: pd.DataFrame, feature_columns: Sequence[str]
) -> pd.DataFrame:
    """Ensure every model feature column exists, adding absent ones as all-NaN.

    A drive may legitimately lack a whole sensor family — e.g. no GPS fix, so no
    ``gps:*`` channels — while the model's schema (trained on a corpus that did
    have it) still expects those columns. The GRU-D mask/Δt path already treats
    an all-NaN channel as unobserved (it imputes the cell and masks it, so the
    loss never scores a fill), which is exactly how the model saw GPS-less
    sessions at training time. So an absent column should be scored as
    masked-missing, never raise in the scaler or the ``.loc`` window slice.

    Returns ``features`` unchanged when nothing is missing.
    """
    missing = [str(column) for column in feature_columns if column not in features.columns]
    if not missing:
        return features
    aligned = features.copy()
    for column in missing:
        aligned[column] = np.nan
    log.info(
        "aligned feature frame to model schema; absent columns masked as unobserved",
        extra={"missing_columns": missing, "missing_count": len(missing)},
    )
    return aligned


def _afr_calibration_provenance(config: CanaryConfig) -> dict[str, str | None]:
    """The ``afr_calibration.yaml`` featurization reads for ``config``, by path
    and content hash (``None`` when absent)."""
    require_profile_scoped(config, reader="feature spec (afr_calibration)")
    return _file_provenance(config.daemon.afr_calibration)


def _behavior_models_provenance(config: CanaryConfig) -> dict[str, str | None]:
    """The ``behavior_models.yaml`` :func:`build_feature_frame` reads for
    ``config`` by default, by path and content hash (P02.12)."""
    require_profile_scoped(config, reader="feature spec (behavior_models)")
    return _file_provenance(config.daemon.behavior_models)


def _file_provenance(path: Path) -> dict[str, str | None]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return {"path": str(path), "sha256": digest}


def build_feature_spec(
    features: pd.DataFrame,
    feature_columns: Sequence[str],
    *,
    config: CanaryConfig | None = None,
    sequence_length: int | None = None,
    sequence_stride: int | None = None,
    policy: FeaturePolicy | None = None,
    behavior_models: BehaviorModels | None = None,
) -> dict[str, Any]:
    """Return the versioned feature contract persisted in model manifests.

    This is the first explicit train/serve schema artifact for Phase 7:
    ordered feature columns, their dtypes, the preprocessing knobs that shape
    them, source families, and a compact health report. Later 7.1 work can
    compare this spec against the live source before scoring.

    ``behavior_models`` is the artifact set that produced the frame's
    ``deviation:*`` columns. When given, the spec records the
    :func:`~src.behavior.behavior_models.artifact_fingerprint` of every model a
    consumed deviation column came from (``behavior_model_fingerprints``), so
    a later refit that changes that column's meaning is caught by the
    producibility check instead of being fed to this artifact (B03.4). Left
    ``None``, the field is absent and the provenance is *unknown* — which the
    check reports as such, never as a pass.
    """
    columns = [str(col) for col in feature_columns]
    resolved_policy = policy if policy is not None else resolve_feature_policy()
    candidate_columns = candidate_feature_columns(features, policy=resolved_policy)
    spec: dict[str, Any] = {
        "feature_spec_version": FEATURE_SPEC_VERSION,
        "feature_columns": columns,
        "feature_dtypes": {column: str(features[column].dtype) for column in columns},
        "source_prefixes": sorted({_source_prefix(column) for column in columns}),
        "behavior_model_columns": sorted(
            {base_channel(column) for column in columns if column.startswith("deviation:")}
        ),
        "feature_health": summarize_feature_health(
            features,
            columns,
            candidate_columns=candidate_columns,
        ),
        # Channels present in the corpus that policy kept out, with the reason
        # as it stood at training time (binding confidence / opt-out, DID not
        # served). Empty when nothing was gated.
        "feature_policy_exclusions": feature_policy_exclusions(features, resolved_policy),
        # P02.13: channels the policy refuses by name, present in the frame or
        # not, so the manifest says why ``derived:TARGET_AFR`` is absent.
        "feature_policy_standing_exclusions": resolved_policy.standing_exclusions(),
    }
    if behavior_models is not None:
        spec["behavior_model_fingerprints"] = consumed_behavior_fingerprints(
            columns, behavior_models
        )
    if config is not None:
        spec["preprocessing"] = {
            "resample_hz": config.preprocessing.resample_hz,
            "rolling_window_seconds": config.preprocessing.rolling_window_seconds,
            "operating_modes": sorted(config.preprocessing.operating_modes),
        }
        # The resampler's max_gap is what flips a channel's ``__gap`` flag to
        # stale at training time. GRU-D serve must mirror it exactly (it has no
        # resampler), so persist it on the spec: the training recipe's
        # max_gap, (1 / hz) * config.preprocessing.max_gap_factor. This once
        # built ResampleConfig(hz=...) and so recorded the default factor 2.0
        # whatever the config said (D03.27).
        spec["max_gap_seconds"] = float(resample_config_for(config).max_gap.total_seconds())
        # P02.10: broadcast channels (``can:``/``decoded:``/``dbc:``) have their
        # own stale class, the broadcast stale timeout, which moved them from
        # 2 s to 10 s; a model trained on one class and served with another
        # reads different ``__gap``/``__arrived`` flags, so the class travels
        # with the model beside the other two.
        spec["broadcast_max_gap_seconds"] = float(
            resample_config_for(config).broadcast_max_gap_seconds
        )
        spec["slow_channel_max_gap_seconds"] = float(
            config.preprocessing.slow_channel_max_gap_seconds
        )
        # P02.11: which calibration the derived AFR channels were built with,
        # by content. ``sha256: None`` says "featurized without one" — the
        # state every pre-P02.11 trainer frame was in, silently.
        spec["afr_calibration"] = _afr_calibration_provenance(config)
        # P02.12: the behavior-model file the frame's ``deviation:*`` columns
        # came from, by content (``sha256: None``: none was read).
        spec["behavior_models"] = _behavior_models_provenance(config)
    if sequence_length is not None:
        spec["sequence_length"] = sequence_length
    if sequence_stride is not None:
        spec["sequence_stride"] = sequence_stride
    return spec


def consumed_behavior_fingerprints(
    feature_columns: Sequence[str], behavior_models: BehaviorModels
) -> dict[str, str]:
    """``{behavior model name: artifact_fingerprint}`` for every consumed ``deviation:*`` column.

    Refuses a consumed column whose model is not in ``behavior_models``: the
    frame was then featurized against a different artifact set than the one
    being recorded (a stale feature cache, or a model retired since), which
    is exactly the unknown provenance this record exists to prevent — rebuild
    the feature cache against the current ``behavior_models.yaml`` instead.
    """
    by_name = {model.name: model for model in behavior_models.models}
    consumed = sorted(
        {name for column in feature_columns if (name := deviation_model_name(column)) is not None}
    )
    missing = [name for name in consumed if name not in by_name]
    if missing:
        raise ValueError(
            "feature spec consumes deviation columns of behavior models not in the "
            f"loaded artifact set: {', '.join(missing)} — the frame was featurized "
            "against a different behavior_models.yaml (stale feature cache?); rebuild it"
        )
    return {name: artifact_fingerprint(by_name[name]) for name in consumed}


def summarize_feature_health(
    features: pd.DataFrame,
    feature_columns: Sequence[str],
    *,
    candidate_columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Summarize missingness, finiteness, and constant features.

    ``candidate_columns`` lets callers report numeric columns dropped before
    training, especially all-NaN decoded/deviation columns.
    """
    columns = [str(col) for col in feature_columns]
    candidates = [str(col) for col in (candidate_columns or columns)]
    selected = set(columns)

    all_nan_columns: list[str] = []
    constant_columns: list[str] = []
    non_finite_columns: list[str] = []
    column_health: dict[str, dict[str, Any]] = {}

    for column in columns:
        health = health_of_column(features[column])
        column_health[column] = health
        if health["non_null_count"] == 0:
            all_nan_columns.append(column)
        if health["non_finite_count"] > 0:
            non_finite_columns.append(column)
        if health["constant"]:
            constant_columns.append(column)

    dropped_all_nan_columns = [
        column
        for column in candidates
        if column not in selected
        and column in features.columns
        and pd.api.types.is_numeric_dtype(features[column])
        and not bool(features[column].notna().any())
    ]
    # Candidates excluded by trainable_feature_columns' zero-variance drop —
    # observed but never moving across the whole corpus (usually a dead poll,
    # e.g. ABSOLUTE_LOAD before its PID worked). Documented here so a manifest
    # answers "why isn't channel X in the model?" without re-running training.
    dropped_constant_columns = [
        column
        for column in candidates
        if column not in selected
        and column in features.columns
        and pd.api.types.is_numeric_dtype(features[column])
        and bool(features[column].notna().any())
        and is_constant_column(features[column])
    ]
    # Per-column coverage for everything that IS selected, plus the sub-floor
    # coverage of what the IF's filter excluded. A blind detector
    # (missing_feature_rate 1.0) is diagnosed straight from here: if any
    # selected column's coverage is well under 1.0, per-row scoring will
    # frequently hit a NaN and self-suppress.
    coverage = {
        column: round(float(features[column].notna().sum()) / len(features), 6)
        for column in columns
        if len(features)
    }
    dropped_low_coverage_columns = low_coverage_columns(
        features, [c for c in candidates if c not in selected]
    )

    return {
        "row_count": int(len(features)),
        "candidate_feature_count": len(candidates),
        "selected_feature_count": len(columns),
        "dropped_all_nan_columns": dropped_all_nan_columns,
        "dropped_constant_columns": dropped_constant_columns,
        "dropped_low_coverage_columns": dropped_low_coverage_columns,
        "min_selected_coverage": (min(coverage.values()) if coverage else None),
        "all_nan_columns": all_nan_columns,
        "constant_columns": constant_columns,
        "non_finite_columns": non_finite_columns,
        "columns": column_health,
    }


def validate_feature_health(feature_health: Mapping[str, Any]) -> None:
    """Fail fast on selected columns that cannot produce meaningful scores."""
    all_nan = [str(col) for col in feature_health.get("all_nan_columns", [])]
    if all_nan:
        raise ValueError("selected feature columns are all-NaN: " + ", ".join(all_nan))


@dataclass(frozen=True)
class FeatureSpecReconciliation:
    """Result of aligning a live/offline frame to a persisted feature spec."""

    row_count: int
    expected_feature_columns: tuple[str, ...]
    missing_feature_columns: tuple[str, ...]
    extra_feature_columns: tuple[str, ...]
    filled_feature_columns: tuple[str, ...]
    coerced_feature_columns: tuple[str, ...]
    output_columns: tuple[str, ...]

    @property
    def has_divergence(self) -> bool:
        return bool(
            self.missing_feature_columns
            or self.extra_feature_columns
            or self.coerced_feature_columns
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_count": self.row_count,
            "expected_feature_columns": list(self.expected_feature_columns),
            "missing_feature_columns": list(self.missing_feature_columns),
            "extra_feature_columns": list(self.extra_feature_columns),
            "filled_feature_columns": list(self.filled_feature_columns),
            "coerced_feature_columns": list(self.coerced_feature_columns),
            "output_columns": list(self.output_columns),
            "has_divergence": self.has_divergence,
        }


def reconcile_feature_frame_to_spec(
    frame: pd.DataFrame,
    schema: Mapping[str, Any],
    *,
    structural_columns: Sequence[str] | None = None,
    fill_missing: float = float("nan"),
    coerce_numeric: bool = True,
) -> tuple[pd.DataFrame, FeatureSpecReconciliation]:
    """Align ``frame`` to the model feature spec persisted in a manifest.

    The serving policy is deliberately explicit:

    * expected feature columns absent from the live row are added as ``NaN``;
    * extra live feature columns are dropped before scoring;
    * expected feature columns are coerced numeric because every trained
      feature column is numeric by construction.

    Structural columns (timestamp/session/mode) are preserved ahead of feature
    columns so model adapters still have the metadata they need.
    """
    feature_columns = tuple(str(column) for column in schema.get("feature_columns", ()))
    if not feature_columns:
        raise ValueError("feature schema has no feature_columns")

    # Every column finished before any frame is built (D01.10). The previous
    # form copied ``frame`` and then inserted each fill and re-assigned each
    # coerced column in place: ~650 block-manager writes (``iset`` /
    # ``_iset_split_block`` / ``blocks.delete``) per one-row tick, which was
    # the whole cost. Keys follow the order the old insert sequence left them
    # in — ``frame``'s own columns, then the fills — so the frame the extras
    # are judged against is the same one.
    if frame.columns.has_duplicates:
        duplicated = sorted({str(c) for c in frame.columns[frame.columns.duplicated()]})
        raise ValueError("feature frame has duplicate columns: " + ", ".join(duplicated))
    finished: dict[Any, pd.Series] = dict(frame.items())
    present = {str(column) for column in frame.columns}
    missing = tuple(column for column in feature_columns if column not in present)
    # One fill column shared by every missing key (the frame build copies it
    # into place), so a spec that is mostly absent costs one Series, not one
    # per absent feature.
    fill = pd.Series(fill_missing, index=frame.index)
    for column in missing:
        finished[column] = fill

    dtype_spec = schema.get("feature_dtypes", {})
    coerced: list[str] = []
    if coerce_numeric:
        coerced_fill: pd.Series | None = None
        for column in feature_columns:
            series = finished[column]
            before = str(series.dtype)
            if series is fill:
                if coerced_fill is None:
                    coerced_fill = pd.to_numeric(fill, errors="coerce")
                series = coerced_fill
            else:
                series = pd.to_numeric(series, errors="coerce")
            finished[column] = series
            after = str(series.dtype)
            expected_dtype = (
                str(dtype_spec.get(column, "")) if isinstance(dtype_spec, Mapping) else ""
            )
            if before != after or (expected_dtype and after != expected_dtype):
                coerced.append(column)

    out = pd.DataFrame(finished, index=frame.index, copy=False)
    extras = tuple(
        column for column in candidate_feature_columns(out) if column not in feature_columns
    )
    structural = _resolve_structural_columns(out, schema, structural_columns)
    output_columns = tuple(dict.fromkeys([*structural, *feature_columns]))
    aligned = out.loc[:, [column for column in output_columns if column in out.columns]].copy()
    report = FeatureSpecReconciliation(
        row_count=int(len(aligned)),
        expected_feature_columns=feature_columns,
        missing_feature_columns=missing,
        extra_feature_columns=extras,
        filled_feature_columns=missing,
        coerced_feature_columns=tuple(coerced),
        output_columns=tuple(str(column) for column in aligned.columns),
    )
    return aligned, report


def _resolve_structural_columns(
    frame: pd.DataFrame,
    schema: Mapping[str, Any],
    override: Sequence[str] | None,
) -> tuple[str, ...]:
    if override is not None:
        return tuple(str(column) for column in override if str(column) in frame.columns)

    candidates = [
        schema.get("timestamp_column", "timestamp"),
        schema.get("session_column", "session"),
        schema.get("mode_column", MODE_COLUMN),
    ]
    return tuple(
        dict.fromkeys(str(column) for column in candidates if str(column) in frame.columns)
    )


def _source_prefix(column: str) -> str:
    return source_of(base_channel(column)) or "derived"
