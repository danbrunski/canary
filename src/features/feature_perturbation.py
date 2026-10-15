"""Featurizing a perturbed frame by recomputing only what the change reaches.

Falls back to the complete transform (:func:`featurize_wide_frame`) whenever
the change cannot be proved local.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

import numpy as np
import pandas as pd

from src.behavior.behavior_decoder import DEVIATION_COLUMN_PREFIX, append_deviation_columns
from src.behavior.behavior_models import BehaviorModels
from src.columns import MODE_COLUMN, ROLLING_SUFFIXES
from src.config import CanaryConfig, require_profile_scoped
from src.decode.bindings import Bindings
from src.decode.dids import DidMap
from src.features.feature_dependencies import (
    ROW_COLUMNS,
    behavior_model_input_roots,
    column_root,
    derived_ancestors,
    derived_channels_reached,
    segmenter_read_columns,
)
from src.features.feature_transform import (
    FeaturizedFrame,
    append_derived_channels,
    featurize_wide_frame,
    resolve_feature_config,
    segment_or_constant,
    without_committee_columns,
)
from src.preprocessing.features import CONTEXT_COLUMN_PREFIX, FeatureConfig, compute_features
from src.signals.committees import committee_channel_reason


def _changed_columns(frame: pd.DataFrame, reference: pd.DataFrame) -> list[str] | None:
    """Columns of ``frame`` whose values differ from ``reference``'s (NaN equal
    to NaN; a dtype change is a change), in ``frame``'s order, or ``None`` when
    ``frame`` has a column ``reference`` lacks (not comparable).

    Compared a dtype block at a time: column by column it was most of the
    incremental path's cost.
    """
    if len(frame) != len(reference) or any(c not in reference.columns for c in frame.columns):
        return None
    columns = list(frame.columns)
    ref_dtypes = dict(reference.dtypes.items())
    changed: set[str] = set()
    by_dtype: dict[Any, list[str]] = {}
    for column, dtype in frame.dtypes.items():
        if ref_dtypes[column] != dtype:
            changed.add(str(column))
        else:
            by_dtype.setdefault(dtype, []).append(str(column))
    for dtype, group in by_dtype.items():
        kind = getattr(dtype, "kind", "O")
        if kind in "fc" and isinstance(dtype, np.dtype):
            a = frame[group].to_numpy()
            b = reference[group].to_numpy()
            same = ((a == b) | (np.isnan(a) & np.isnan(b))).all(axis=0)
        elif kind in "biu" and isinstance(dtype, np.dtype):
            same = (frame[group].to_numpy() == reference[group].to_numpy()).all(axis=0)
        else:
            same = np.array(
                [
                    frame[c].reset_index(drop=True).equals(reference[c].reset_index(drop=True))
                    for c in group
                ],
                dtype=bool,
            )
        changed.update(c for c, ok in zip(group, same, strict=True) if not ok)
    return [str(c) for c in columns if str(c) in changed]


def _narrowed(frame: pd.DataFrame, roots: Collection[str]) -> pd.DataFrame:
    """``frame``'s row-structure columns and every column of ``roots``
    (values, gap and arrival flags), in ``frame``'s order."""
    wanted = set(roots)
    return frame.loc[
        :, [c for c in frame.columns if c in ROW_COLUMNS or column_root(str(c)) in wanted]
    ]


def featurize_perturbed_frame(
    clean: FeaturizedFrame,
    clean_wide: pd.DataFrame,
    wide: pd.DataFrame,
    config: CanaryConfig,
    *,
    behavior_models: BehaviorModels | None,
    did_map: DidMap | None,
    feature_config: FeatureConfig | None = None,
    bindings: Bindings | None = None,
) -> pd.DataFrame:
    """``featurize_wide_frame(wide, ...).features``, recomputing only what
    changed (P02.4).

    ``clean`` is :func:`featurize_wide_frame`'s output, with the same
    arguments, for ``clean_wide`` — a frame with ``wide``'s rows and columns,
    such as the one a fault was injected into. The changed columns are found
    by comparing ``wide`` with ``clean_wide``, never taken on trust; only
    what they can reach is recomputed:

    - The operating mode is reused. When a column the segmenter reads
      changed, the frame is re-segmented, and a row whose mode moved — like
      rows or columns that differ — sends the whole frame down the full
      transform.
    - A derived channel is recomputed when a declared input changed
      (:func:`derived_channels_reached`), from its declared inputs alone.
    - A behavior deviation is recomputed when its model names a changed
      channel (:func:`behavior_model_input_roots`), from those channels.
    - Rolling features are recomputed for the changed columns alone:
      :func:`~src.preprocessing.features.compute_features` is per column.

    The rest is the clean frame's. That is exact only while each stage reads
    what it declares, so ``tests/test_feature_pipeline_incremental.py``
    perturbs every *undeclared* column and requires each derived channel and
    deviation to stay put, and compares this against the full transform with
    ``check_exact`` for every realistic fault. Returns features only: the
    sensor-committee columns (detectors, never features) are not recomputed,
    and a deviation that reads one sends the frame down the full path.
    """
    require_profile_scoped(config, reader="featurize_perturbed_frame")
    reference = clean.operating_frame

    def full() -> pd.DataFrame:
        return featurize_wide_frame(
            wide,
            config,
            behavior_models=behavior_models,
            did_map=did_map,
            feature_config=feature_config,
            bindings=bindings,
        ).features

    if MODE_COLUMN in wide.columns or list(wide.columns) != list(clean_wide.columns):
        return full()
    changed_wide = _changed_columns(wide, clean_wide)
    if changed_wide is None or set(changed_wide) & set(ROW_COLUMNS):
        return full()
    if not changed_wide:
        return clean.features
    if set(changed_wide) & segmenter_read_columns():
        # A segmenter input moved (a coolant fault): re-segment, and go on
        # only if not one row changed mode.
        segmented, _ = segment_or_constant(wide, config, bindings=bindings)
        if (
            not segmented[MODE_COLUMN]
            .reset_index(drop=True)
            .equals(reference[MODE_COLUMN].reset_index(drop=True))
        ):
            return full()
    # A column some stage rewrites (a collector-stored ``derived:`` channel
    # that is recomputed here) is not an input the clean run kept as it was:
    # only a column the stages carried through untouched is reused.
    if _changed_columns(clean_wide.loc[:, changed_wide], reference) != []:
        return full()
    values: dict[str, pd.Series] = {c: wide[c] for c in changed_wide}
    reached = derived_channels_reached({column_root(c) for c in changed_wide})
    if reached:
        narrow = _narrowed(wide, derived_ancestors(reached)).copy()
        narrow[MODE_COLUMN] = reference[MODE_COLUMN].to_numpy()
        derived = append_derived_channels(narrow, config, did_map=did_map)
        outputs = derived.loc[:, [c for c in derived.columns if column_root(str(c)) in reached]]
        moved = _changed_columns(outputs, reference)
        if moved is None:
            return full()
        values.update({c: outputs[c] for c in moved})
    cfg = resolve_feature_config(config, feature_config)
    if behavior_models is not None and behavior_models.models:
        roots = {column_root(c) for c in values}
        models = [m for m in behavior_models.models if behavior_model_input_roots(m) & roots]
        if models:
            read = frozenset().union(*(behavior_model_input_roots(m) for m in models))
            if any(committee_channel_reason(root) is not None for root in read):
                return full()  # a committee column is never recomputed here
            before = reference.loc[
                :,
                [
                    c
                    for c in reference.columns
                    if not str(c).startswith((DEVIATION_COLUMN_PREFIX, CONTEXT_COLUMN_PREFIX))
                    and not str(c).endswith(ROLLING_SUFFIXES)
                ],
            ]
            frame = _narrowed(before, read).copy()
            for column, series in values.items():
                if column in frame.columns:
                    frame[column] = series.to_numpy()
            deviations = append_deviation_columns(
                frame,
                behavior_models.model_copy(update={"models": models}),
                rate_window_seconds=cfg.window_seconds,
            )
            added = deviations.loc[:, [c for c in deviations.columns if c not in frame.columns]]
            moved = _changed_columns(added, reference)
            if moved is None:
                return full()
            values.update({c: added[c] for c in moved})
    rolled_input = without_committee_columns(
        pd.concat(
            [
                reference.loc[:, ["session", "timestamp", MODE_COLUMN]],
                *(
                    series.rename(column).set_axis(reference.index)
                    for column, series in values.items()
                ),
            ],
            axis=1,
        )
    )
    rolled = compute_features(rolled_input, config=cfg)
    replaced = [c for c in rolled.columns if c not in ("session", "timestamp", MODE_COLUMN)]
    features = clean.features
    if any(column not in features.columns for column in replaced):
        return full()
    out = features.drop(columns=replaced)
    out = pd.concat([out, rolled.loc[:, replaced]], axis=1)
    return out.loc[:, list(features.columns)]
