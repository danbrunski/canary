"""Detector feature selection: trainable columns, the coverage floor and the
Isolation Forest / sequence differences.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.behavior.behavior_decoder import DEVIATION_COLUMN_PREFIX
from src.columns import MODE_COLUMN, UNKNOWN_MODE, base_channel, split_rolling
from src.features.column_health import health_of_column
from src.features.feature_policy import FeaturePolicy, candidate_feature_columns


def trainable_feature_columns(
    features: pd.DataFrame, *, policy: FeaturePolicy | None = None
) -> list[str]:
    """Numeric feature columns with informative variation in the corpus.

    Excludes structural columns (``session``, ``timestamp``, the mode column)
    and gap flags; keeps ``decoded:*`` and OBD channels plus their
    ``__mean``/``__std``/``__roc`` derivatives. Drops all-NaN columns (signals
    whose CAN ID never appeared this session) so the z-score scaler and the
    sequence-window builder don't choke, and drops columns *constant across
    the entire corpus*: a channel with zero training variance teaches the
    detectors nothing, and the moment the sensor comes alive after training
    it dominates every event's attribution (the May/June models trained while
    ``obd:ABSOLUTE_LOAD`` polling was dead-constant; when the PID started
    reporting in July, its family appeared in 37/37 LSTM events). Constancy
    is judged over the whole multi-session frame, so a channel that is merely
    steady *within* one session (coolant at temperature) survives as long as
    the corpus saw it move. ``summarize_feature_health`` documents the drops
    in the manifest (``dropped_constant_columns``).
    """
    cols: list[str] = []
    for col in candidate_feature_columns(features, policy=policy):
        health = health_of_column(features[col])
        if health["finite_count"] == 0:
            continue
        if health["constant"]:
            continue
        cols.append(col)
    return cols


#: Minimum fraction of classified 1 Hz grid rows a feature column must be
#: valued on to be a **detector input** — one floor, shared by the Isolation
#: Forest and the sequence detector (D03.34, 2026-09-26).
#:
#: The IF needs it most: it scores one row at a time and
#: ``TrainedIsolationForest.score`` refuses any row with a NaN in ANY trained
#: feature (``group[feats].isna().any(axis=1)`` → score=inf, missing flag set,
#: and the daemon's fail-safe then suppresses the emit). So every sparse column
#: in the IF's schema multiplies the chance that a row is unscoreable, and a
#: single structurally-absent column makes the detector permanently blind.
#: Measured on the 2026-07-26 corpus: the IF trained 273 features of which NONE
#: reached 75% coverage, so it could not score a single row — matching the Pi's
#: observed ``missing_feature_rate: 1.0``. Offenders split in two:
#: genuinely-absent channels (``derived:TPS_VOLTAGE_PER_PCT`` at 0.21%,
#: ``did:*`` at ~1.5% — polled only inside periodic Mode-22 windows) and
#: channels NaN'd as stale because the resampler's ceiling assumed a poll rate
#: the contended bus never achieves. This threshold removes the former; the
#: staleness ceiling (``preprocessing.max_gap_factor``) governs the latter.
#:
#: The sequence detector gets the same floor on grid *density*
#: (:func:`detector_grid_density`): its GRU-D mask/Δt channels make a
#: forward-filled round-robin PID cheap, but a channel valued on 3 % of the
#: rows of every drive that carries it (the Xterra ``default``'s eight Mode 22
#: DIDs, read once per ~60 s window, and the ``derived:`` channels built from
#: them) is a mostly-empty column, not a signal at the grid's rate. A channel
#: that is dense where present but absent from some drives stays a sequence
#: input: masking is what handles that, and the IF alone additionally needs
#: it valued on the rows it scores (:data:`BELOW_ROW_COVERAGE`).
MIN_DETECTOR_COVERAGE = 0.5

#: The IF's coverage floor *is* the shared detector floor; the old name stays
#: for callers and for the history in the comments that cite it.
MIN_ISOLATION_FOREST_COVERAGE = MIN_DETECTOR_COVERAGE

#: Named reasons a column fails the coverage floor, recorded per column in the
#: manifest under :data:`COVERAGE_FLOOR_SCHEMA_KEY`.
#:
#: ``sparse_for_grid`` — both detectors: the column's *channel* (its base,
#: :func:`~src.columns.base_channel`) is valued on fewer than
#: :data:`MIN_DETECTOR_COVERAGE` of the classified rows of the sessions that
#: carry it, i.e. polled too slowly for the 1 Hz grid. The whole family goes:
#: a 30 s rolling ``__mean`` of a once-a-minute read is valued on over half the
#: rows only because the rolling window holds the one read for 30 s, past the
#: resampler's forward-fill ceiling — the same read smeared, not a 1 Hz signal.
SPARSE_FOR_GRID = "sparse_for_grid"

#: ``below_row_coverage`` — IF only, per column (its pre-D03.34 rule): valued
#: on fewer than :data:`MIN_DETECTOR_COVERAGE` of *all* the classified rows, so
#: the row-wise IF could not score the rest (a channel dense where present but
#: absent from some drives, or a ``__roc`` NaN on every stale tick).
BELOW_ROW_COVERAGE = "below_row_coverage"

#: Manifest ``schema`` key: the coverage floor a detector was cut with and
#: every candidate column it excluded, with the named reason, the measured
#: coverage and the column it was measured on (D03.34).
COVERAGE_FLOOR_SCHEMA_KEY = "coverage_floor_exclusions"


@dataclass(frozen=True)
class CoverageExclusion:
    """Why the coverage floor kept one candidate column out."""

    reason: str
    coverage: float
    #: The column the coverage was measured on: the channel's base column for
    #: :data:`SPARSE_FOR_GRID`, the column itself for :data:`BELOW_ROW_COVERAGE`.
    measured_on: str


@dataclass(frozen=True)
class DetectorFeatureSelection:
    """A detector's feature columns and what the coverage floor kept out.

    The trainers record :attr:`coverage_exclusions` from the *same* object they
    take :attr:`columns` from, so the manifest cannot name a different set than
    the one the model was fit on.
    """

    columns: list[str]
    coverage_exclusions: dict[str, CoverageExclusion]
    min_coverage: float

    def coverage_record(self) -> dict[str, Any]:
        """The manifest record (:data:`COVERAGE_FLOOR_SCHEMA_KEY`)."""
        return {
            "min_coverage": self.min_coverage,
            "measured_over": "classified-mode rows of the training frame",
            "columns": {
                column: {
                    "reason": excluded.reason,
                    "coverage": excluded.coverage,
                    "measured_on": excluded.measured_on,
                }
                for column, excluded in sorted(self.coverage_exclusions.items())
            },
        }


def detector_grid_density(features: pd.DataFrame, columns: Sequence[str]) -> dict[str, float]:
    """``{column: density}``: the share of classified grid rows a column is
    valued on, counted over only the sessions that carry it at all.

    This is the grid-rate half of the coverage question, separated from
    corpus presence: a once-a-minute DID scores ~3 % in every session that
    polls it, while a PID added to polling last week scores ~100 % in the
    sessions that have it. Rows are those of :func:`detector_coverage_rows`;
    a frame without a session column is one session. A column never valued on
    those rows has density 0.
    """
    scored = detector_coverage_rows(features)
    present = list(dict.fromkeys(str(c) for c in columns if c in scored.columns))
    if not present or scored.empty:
        return dict.fromkeys(present, 0.0)
    valued = scored[present].notna()
    if "session" not in scored.columns:
        rows = float(len(scored))
        return {c: float(valued[c].sum()) / rows for c in present}
    sessions = scored["session"].astype(str).to_numpy()
    per_session = valued.groupby(sessions, sort=False).sum()
    session_rows = pd.Series(sessions).value_counts().reindex(per_session.index)
    carried_rows = (per_session > 0).mul(session_rows, axis=0).sum()
    counts = per_session.sum()
    return {
        c: (float(counts[c]) / float(carried_rows[c]) if carried_rows[c] > 0 else 0.0)
        for c in present
    }


def detector_coverage(features: pd.DataFrame, columns: Sequence[str]) -> dict[str, float]:
    """``{column: coverage}`` over every classified row (the IF's measure)."""
    scored = detector_coverage_rows(features)
    rows = len(scored)
    return {
        str(c): (float(scored[c].notna().sum()) / rows if rows else 0.0)
        for c in columns
        if c in scored.columns
    }


def apply_coverage_floor(
    features: pd.DataFrame,
    columns: Sequence[str],
    *,
    require_row_coverage: bool,
    min_coverage: float = MIN_DETECTOR_COVERAGE,
) -> DetectorFeatureSelection:
    """The shared detector coverage rule over already-trainable ``columns``.

    Every detector drops a column whose channel is under ``min_coverage``
    grid density (:data:`SPARSE_FOR_GRID`, judged on the base column when the
    frame has it, so a sparse channel's rolling siblings go with it).
    ``require_row_coverage`` adds the IF's per-column, row-wise requirement:
    the same floor over all classified rows (:data:`BELOW_ROW_COVERAGE`).
    Density is never below whole-frame coverage, so a raw column that passes
    the row check passes the density check too; the IF's only difference from
    its pre-D03.34 selection is the rolling siblings of a sparse channel.
    ``min_coverage <= 0`` (or an empty frame) disables the floor.
    """
    kept = [str(c) for c in columns]
    if min_coverage <= 0 or features.empty:
        return DetectorFeatureSelection(kept, {}, float(min_coverage))
    measured_on = {
        column: (base if (base := base_channel(column)) in features.columns else column)
        for column in kept
    }
    density = detector_grid_density(features, list(measured_on.values()))
    coverage = detector_coverage(features, kept) if require_row_coverage else {}
    excluded: dict[str, CoverageExclusion] = {}
    selected: list[str] = []
    for column in kept:
        channel = measured_on[column]
        if density.get(channel, 0.0) < min_coverage:
            excluded[column] = CoverageExclusion(
                SPARSE_FOR_GRID, round(density.get(channel, 0.0), 6), channel
            )
        elif require_row_coverage and coverage.get(column, 0.0) < min_coverage:
            excluded[column] = CoverageExclusion(
                BELOW_ROW_COVERAGE, round(coverage.get(column, 0.0), 6), column
            )
        else:
            selected.append(column)
    return DetectorFeatureSelection(selected, excluded, float(min_coverage))


def isolation_forest_feature_selection(
    features: pd.DataFrame,
    *,
    min_coverage: float = MIN_DETECTOR_COVERAGE,
    policy: FeaturePolicy | None = None,
) -> DetectorFeatureSelection:
    """:func:`trainable_feature_columns` through the shared coverage floor,
    with the IF's row-coverage requirement (:func:`apply_coverage_floor`)."""
    return apply_coverage_floor(
        features,
        trainable_feature_columns(features, policy=policy),
        require_row_coverage=True,
        min_coverage=min_coverage,
    )


def isolation_forest_feature_columns(
    features: pd.DataFrame,
    *,
    min_coverage: float = MIN_ISOLATION_FOREST_COVERAGE,
    policy: FeaturePolicy | None = None,
) -> list[str]:
    """Trainable columns restricted to those the IF can actually score on.

    :func:`trainable_feature_columns` plus the per-column coverage floor — see
    :data:`MIN_DETECTOR_COVERAGE` for why. ``min_coverage=0`` recovers the old
    behaviour.
    """
    return isolation_forest_feature_selection(
        features, min_coverage=min_coverage, policy=policy
    ).columns


def detector_coverage_rows(features: pd.DataFrame) -> pd.DataFrame:
    """The rows the detector coverage floor is measured over: those in a
    classified operating mode (both detectors, D03.34).

    The IF never fits the ``unknown`` mode (``SKIP_UNCLASSIFIED_MODE``) and
    serving refuses one (A04.8), so an unclassified row is a row it can never
    score. Counting them let parked key-off chatter decide the feature set: in
    the 2026-09-21..23 corpus 72 % of rows were ``unknown`` (the Pi recording
    GPS with the engine off), every engine channel fell under the floor, and
    the retrained IF kept two channels. A frame with no classified row (or no
    mode column) is measured whole.
    """
    if MODE_COLUMN not in features.columns:
        return features
    classified = features[features[MODE_COLUMN].astype(str) != UNKNOWN_MODE]
    return classified if len(classified) else features


def low_coverage_columns(
    features: pd.DataFrame,
    columns: Sequence[str],
    *,
    min_coverage: float = MIN_ISOLATION_FOREST_COVERAGE,
) -> dict[str, float]:
    """``{column: coverage}`` for ``columns`` below the coverage floor.

    Reported in the manifest so a blind-IF diagnosis doesn't require
    re-deriving coverage from the corpus by hand.
    """
    if features.empty:
        return {}
    return {
        column: round(coverage, 6)
        for column, coverage in detector_coverage(features, columns).items()
        if coverage < min_coverage
    }


def restrict_isolation_forest_expansion(
    columns: Sequence[str],
    *,
    drop_suffixes: Sequence[str] = (),
) -> list[str]:
    """Drop rolling-feature-expansion columns ending in ``drop_suffixes`` from
    an already-resolved IF feature list (e.g. ``trainable_feature_columns``'s
    output).

    Task 2.5 (ml-correctness-hardening): each channel expands 4x (raw +
    ``__mean`` + ``__std`` + ``__roc``) and the IF trial benchmark compared
    that full set against raw+``__roc``-only and raw-only subsets, passing
    the trimmed list to ``fit_isolation_forest``'s existing ``feature_columns``
    override (no new config knob needed). The trial found the full 4x set won
    on both detection rate and nominal FP — the diluted-random-split theory
    didn't hold up here because ``__std``/``__roc`` collapsing to ~0 IS the
    primary signature of a "stuck" sensor fault, not a redundant copy of the
    raw channel — so this is unused by the default IF training path today
    (``scripts/train_anomaly_model.py`` keeps calling
    ``trainable_feature_columns`` unfiltered). Kept here, not deleted, so a
    future retrial (a real multi-session corpus, a different channel mix)
    doesn't have to rebuild the plumbing from scratch. Never applied to
    ``sequence_feature_columns`` — the sequence detector keeps the full
    expansion (masking makes it cheap there, per the task).
    """
    return [c for c in columns if not any(c.endswith(suffix) for suffix in drop_suffixes)]


#: Prefix of behavior-model deviation columns (raw + __mean/__std/__roc).
DEVIATION_PREFIX = DEVIATION_COLUMN_PREFIX


def sequence_feature_selection(
    features: pd.DataFrame,
    *,
    min_coverage: float = MIN_DETECTOR_COVERAGE,
    policy: FeaturePolicy | None = None,
) -> DetectorFeatureSelection:
    """GRU-D sequence-autoencoder feature columns, with what the shared
    coverage floor kept out.

    Differs from :func:`trainable_feature_columns` (which the Isolation Forest
    starts from) in one way: it **drops the entire ``deviation:`` family** (raw
    and rolling aggregates). Deviations are behavior-model-derived scores that
    are NaN by contract whenever the model can't speak (off operating-regime) —
    scattered NaN, not a gap. They are designed for the IF path, which imputes;
    an autoencoder reconstructing sensor *sequences* should not consume them.

    Then the detector coverage floor (D03.34, :func:`apply_coverage_floor`) on
    grid density only: GRU-D feeds missingness to the model via the mask/Δt
    channels, so a round-robin PID forward-filled between polls, or a channel
    absent from some drives, is kept. A channel valued on under
    :data:`MIN_DETECTOR_COVERAGE` of the rows of the drives that carry it (a
    DID read once a minute) is not a sequence at the grid's rate, and it and
    its rolling siblings are dropped as :data:`SPARSE_FOR_GRID`. The remaining drops — all-NaN
    everywhere and corpus-constant (zero-variance) columns — are inherited from
    :func:`trainable_feature_columns`.
    """
    candidates = [
        col
        for col in trainable_feature_columns(features, policy=policy)
        if not col.startswith(DEVIATION_PREFIX)
    ]
    return apply_coverage_floor(
        features, candidates, require_row_coverage=False, min_coverage=min_coverage
    )


def sequence_feature_columns(
    features: pd.DataFrame,
    *,
    min_coverage: float = MIN_DETECTOR_COVERAGE,
    policy: FeaturePolicy | None = None,
) -> list[str]:
    """The columns of :func:`sequence_feature_selection`."""
    return sequence_feature_selection(features, min_coverage=min_coverage, policy=policy).columns


def expand_suppress_channels(
    suppress_bases: Sequence[str], feature_columns: Sequence[str]
) -> tuple[str, ...]:
    """Expand each ``suppress_channels`` entry to the matching base column
    plus its rolling-derivative siblings.

    Listing ``obd:FUEL_LEVEL`` matches all of ``obd:FUEL_LEVEL``,
    ``obd:FUEL_LEVEL__mean``, ``obd:FUEL_LEVEL__std``, ``obd:FUEL_LEVEL__roc``.
    Entries that don't match any column are silently dropped (a typo or a
    channel that disappears from the corpus becomes a no-op, not a hard
    failure that breaks the serve path). Order matches ``feature_columns``
    so the result composes cleanly with index lookups.
    """
    suppress_set = {str(s) for s in suppress_bases}
    if not suppress_set:
        return ()
    matched: list[str] = []
    for column in feature_columns:
        if column in suppress_set:
            matched.append(column)
            continue
        # Rolling derivatives follow their base channel, so the operator lists
        # the sensor once and its feature-pipeline siblings come along.
        base, suffix = split_rolling(column)
        if suffix and base in suppress_set:
            matched.append(column)
    return tuple(matched)
