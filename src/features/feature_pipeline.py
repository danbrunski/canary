"""Compatibility facade over the focused feature-pipeline modules (R14).

``feature_pipeline`` was one 2,701-line module. Its responsibilities now live
in modules that depend one way (``tests/test_feature_pipeline_boundaries.py``
holds the map):

====================== ===========================================================
``feature_paths``      repo-rooted default catalog / cache paths (leaf)
``column_health``      finite / constant / all-NaN column health (leaf)
``feature_policy``     ``FeaturePolicy``, exclusions, ``candidate_feature_columns``
``feature_transform``  the one wide -> features transform, ``FeaturizedFrame``
``feature_dependencies`` channel / derived / behavior-model reach
``feature_perturbation`` ``featurize_perturbed_frame`` and its full-transform fallback
``feature_selection``  trainable columns, coverage floor, IF versus sequence
``feature_spec``       the persisted spec, health, reconciliation
``session_resampling`` one session's decode -> gate -> resample, decode catalogs
``feature_workers``    worker sizing, spawn entry points, serial fallback
``feature_build``      ``build_feature_frame``, the disposition screen
====================== ===========================================================

This file defines nothing: every name below is the owner's object, kept so the
old import path keeps working. Library code imports the owners.
"""

from __future__ import annotations

from src.features.feature_build import (
    build_feature_frame as build_feature_frame,
)
from src.features.feature_build import (
    disposition_screen_frames as disposition_screen_frames,
)
from src.features.feature_cache import (
    FEATURE_FRAME_CACHE_VERSION as FEATURE_FRAME_CACHE_VERSION,
)
from src.features.feature_cache import (
    build_feature_frame_cache_key as build_feature_frame_cache_key,
)
from src.features.feature_dependencies import (
    behavior_model_input_roots as behavior_model_input_roots,
)
from src.features.feature_dependencies import (
    column_root as _column_root,
)
from src.features.feature_dependencies import (
    derived_ancestors as _derived_ancestors,
)
from src.features.feature_dependencies import (
    derived_channel_inputs as derived_channel_inputs,
)
from src.features.feature_dependencies import (
    derived_channels_reached as derived_channels_reached,
)
from src.features.feature_paths import (
    DEFAULT_BINDINGS_PATH as DEFAULT_BINDINGS_PATH,
)
from src.features.feature_paths import (
    DEFAULT_CAPTURE_PROFILES_PATH as DEFAULT_CAPTURE_PROFILES_PATH,
)
from src.features.feature_paths import (
    DEFAULT_DIDS_PATH as DEFAULT_DIDS_PATH,
)
from src.features.feature_paths import (
    DEFAULT_FEATURE_CACHE_DIR as DEFAULT_FEATURE_CACHE_DIR,
)
from src.features.feature_perturbation import (
    featurize_perturbed_frame as featurize_perturbed_frame,
)
from src.features.feature_policy import (
    DBC_PREFIX as DBC_PREFIX,
)
from src.features.feature_policy import (
    DERIVED_PREFIX as DERIVED_PREFIX,
)
from src.features.feature_policy import (
    DID_PREFIX as DID_PREFIX,
)
from src.features.feature_policy import (
    DID_VIEW_EXCLUSION as DID_VIEW_EXCLUSION,
)
from src.features.feature_policy import (
    MEMORY_CHANNEL_EXCLUSION as MEMORY_CHANNEL_EXCLUSION,
)
from src.features.feature_policy import (
    MEMORY_FEATURE_PREFIX as MEMORY_FEATURE_PREFIX,
)
from src.features.feature_policy import (
    NON_TRAINABLE_COUNTER_CHANNELS as NON_TRAINABLE_COUNTER_CHANNELS,
)
from src.features.feature_policy import (
    NON_TRAINABLE_GPS_CHANNELS as NON_TRAINABLE_GPS_CHANNELS,
)
from src.features.feature_policy import (
    NON_TRAINABLE_STATE_CHANNELS as NON_TRAINABLE_STATE_CHANNELS,
)
from src.features.feature_policy import (
    OBD_PREFIX as OBD_PREFIX,
)
from src.features.feature_policy import (
    FeaturePolicy as FeaturePolicy,
)
from src.features.feature_policy import (
    candidate_feature_columns as candidate_feature_columns,
)
from src.features.feature_policy import (
    feature_policy_exclusions as feature_policy_exclusions,
)
from src.features.feature_policy import (
    resolve_feature_policy as resolve_feature_policy,
)
from src.features.feature_selection import (
    BELOW_ROW_COVERAGE as BELOW_ROW_COVERAGE,
)
from src.features.feature_selection import (
    COVERAGE_FLOOR_SCHEMA_KEY as COVERAGE_FLOOR_SCHEMA_KEY,
)
from src.features.feature_selection import (
    DEVIATION_PREFIX as DEVIATION_PREFIX,
)
from src.features.feature_selection import (
    MIN_DETECTOR_COVERAGE as MIN_DETECTOR_COVERAGE,
)
from src.features.feature_selection import (
    MIN_ISOLATION_FOREST_COVERAGE as MIN_ISOLATION_FOREST_COVERAGE,
)
from src.features.feature_selection import (
    SPARSE_FOR_GRID as SPARSE_FOR_GRID,
)
from src.features.feature_selection import (
    CoverageExclusion as CoverageExclusion,
)
from src.features.feature_selection import (
    DetectorFeatureSelection as DetectorFeatureSelection,
)
from src.features.feature_selection import (
    apply_coverage_floor as apply_coverage_floor,
)
from src.features.feature_selection import (
    detector_coverage as detector_coverage,
)
from src.features.feature_selection import (
    detector_coverage_rows as detector_coverage_rows,
)
from src.features.feature_selection import (
    detector_grid_density as detector_grid_density,
)
from src.features.feature_selection import (
    expand_suppress_channels as expand_suppress_channels,
)
from src.features.feature_selection import (
    isolation_forest_feature_columns as isolation_forest_feature_columns,
)
from src.features.feature_selection import (
    isolation_forest_feature_selection as isolation_forest_feature_selection,
)
from src.features.feature_selection import (
    low_coverage_columns as low_coverage_columns,
)
from src.features.feature_selection import (
    restrict_isolation_forest_expansion as restrict_isolation_forest_expansion,
)
from src.features.feature_selection import (
    sequence_feature_columns as sequence_feature_columns,
)
from src.features.feature_selection import (
    sequence_feature_selection as sequence_feature_selection,
)
from src.features.feature_selection import (
    trainable_feature_columns as trainable_feature_columns,
)
from src.features.feature_spec import (
    FEATURE_SPEC_VERSION as FEATURE_SPEC_VERSION,
)
from src.features.feature_spec import (
    FeatureSpecReconciliation as FeatureSpecReconciliation,
)
from src.features.feature_spec import (
    _resolve_structural_columns as _resolve_structural_columns,
)
from src.features.feature_spec import (
    align_features_to_schema as align_features_to_schema,
)
from src.features.feature_spec import (
    build_feature_spec as build_feature_spec,
)
from src.features.feature_spec import (
    consumed_behavior_fingerprints as consumed_behavior_fingerprints,
)
from src.features.feature_spec import (
    reconcile_feature_frame_to_spec as reconcile_feature_frame_to_spec,
)
from src.features.feature_spec import (
    summarize_feature_health as summarize_feature_health,
)
from src.features.feature_spec import (
    validate_feature_health as validate_feature_health,
)
from src.features.feature_transform import (
    ALL_MODE as ALL_MODE,
)
from src.features.feature_transform import (
    SEGMENTER_SIGNAL_COLUMNS as SEGMENTER_SIGNAL_COLUMNS,
)
from src.features.feature_transform import (
    FeaturizedFrame as FeaturizedFrame,
)
from src.features.feature_transform import (
    append_derived_channels as _append_derived_channels,
)
from src.features.feature_transform import (
    configured_behavior_models as configured_behavior_models,
)
from src.features.feature_transform import (
    featurize_wide_frame as featurize_wide_frame,
)
from src.features.feature_transform import (
    segment_or_constant as segment_or_constant,
)
from src.features.feature_transform import (
    segmenter_input_columns as segmenter_input_columns,
)
from src.features.feature_transform import (
    without_committee_columns as _without_committee_columns,
)
from src.features.session_resampling import (
    _drop_fully_stale_rows as _drop_fully_stale_rows,
)
from src.features.session_resampling import (
    decode_session_to_resampled as _decode_session_to_resampled,
)
from src.features.session_resampling import (
    default_decode_inputs as default_decode_inputs,
)
from src.features.session_resampling import (
    drop_fully_stale_rows as drop_fully_stale_rows,
)
from src.features.session_resampling import (
    load_resampled_session as load_resampled_session,
)
from src.features.session_resampling import (
    resample_long_session as resample_long_session,
)
from src.features.session_resampling import (
    resample_session_bounded as resample_session_bounded,
)
from src.preprocessing.resampler import resample_config_for as resample_config_for

__all__ = [
    "align_features_to_schema",
    "ALL_MODE",
    "_append_derived_channels",
    "apply_coverage_floor",
    "behavior_model_input_roots",
    "BELOW_ROW_COVERAGE",
    "build_feature_frame",
    "build_feature_frame_cache_key",
    "build_feature_spec",
    "candidate_feature_columns",
    "_column_root",
    "configured_behavior_models",
    "consumed_behavior_fingerprints",
    "COVERAGE_FLOOR_SCHEMA_KEY",
    "CoverageExclusion",
    "DBC_PREFIX",
    "_decode_session_to_resampled",
    "DEFAULT_BINDINGS_PATH",
    "DEFAULT_CAPTURE_PROFILES_PATH",
    "default_decode_inputs",
    "DEFAULT_DIDS_PATH",
    "DEFAULT_FEATURE_CACHE_DIR",
    "_derived_ancestors",
    "derived_channel_inputs",
    "derived_channels_reached",
    "DERIVED_PREFIX",
    "detector_coverage",
    "detector_coverage_rows",
    "detector_grid_density",
    "DetectorFeatureSelection",
    "DEVIATION_PREFIX",
    "DID_PREFIX",
    "DID_VIEW_EXCLUSION",
    "disposition_screen_frames",
    "drop_fully_stale_rows",
    "_drop_fully_stale_rows",
    "expand_suppress_channels",
    "FEATURE_FRAME_CACHE_VERSION",
    "feature_policy_exclusions",
    "FEATURE_SPEC_VERSION",
    "FeaturePolicy",
    "FeatureSpecReconciliation",
    "featurize_perturbed_frame",
    "featurize_wide_frame",
    "FeaturizedFrame",
    "isolation_forest_feature_columns",
    "isolation_forest_feature_selection",
    "load_resampled_session",
    "low_coverage_columns",
    "MEMORY_CHANNEL_EXCLUSION",
    "MEMORY_FEATURE_PREFIX",
    "MIN_DETECTOR_COVERAGE",
    "MIN_ISOLATION_FOREST_COVERAGE",
    "NON_TRAINABLE_COUNTER_CHANNELS",
    "NON_TRAINABLE_GPS_CHANNELS",
    "NON_TRAINABLE_STATE_CHANNELS",
    "OBD_PREFIX",
    "reconcile_feature_frame_to_spec",
    "resample_config_for",
    "resample_long_session",
    "resample_session_bounded",
    "resolve_feature_policy",
    "_resolve_structural_columns",
    "restrict_isolation_forest_expansion",
    "segment_or_constant",
    "segmenter_input_columns",
    "SEGMENTER_SIGNAL_COLUMNS",
    "sequence_feature_columns",
    "sequence_feature_selection",
    "SPARSE_FOR_GRID",
    "summarize_feature_health",
    "trainable_feature_columns",
    "validate_feature_health",
    "_without_committee_columns",
]
