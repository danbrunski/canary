"""Reproducible behavior-model validation (B02).

One evaluator for every question the refit and the offline review ask of a
behavior model: which rows could it speak for, how wrong was it there in
physical units, would it have alerted, could it have seen a fault of the size
the operator cares about, and on how many *independent* drives was that
measured.

The pieces, each used by both ``src.analysis.refit`` (publication) and
``scripts/evaluate_behavior_models.py`` (the reproducible report):

* **Drive grouping** (B02.3). A physical drive is split across directories —
  a capture rotates the writer into ``_capture-…`` and back — and the
  resampler splits a directory at gaps into ``#segment-NNN`` pieces. Pieces
  are grouped by parent directory, then adjacent directories are joined when
  the gap between them is at most ``join_gap_seconds``. Order comes from
  parsed start times (``src.storage.sessions.session_started_at``) or the
  frame's own timestamps — never from directory names, where a capture prefix
  sorts a later drive "earlier".
* **Grouped splits.** Newest drives form the test fold, the next ones the
  calibration fold, the rest train. No drive's pieces may cross folds; an
  explicit assignment that tries is refused (:class:`LeakedDriveError`).
* **Row evaluation** through the single B01 eligibility evaluator, with
  denominators kept apart: rows, input-present, fresh, eligible (passes the
  serving contract), evaluated (on-map with an actual). An ineligible row is
  never an ``ok`` measurement.
* **Metrics in physical units** — MAE, signed bias, absolute-error quantiles,
  R² only as a descriptor, warning/critical band half-widths — plus event
  counts after the daemon's own persistence gate and emitter dedupe, reported
  separately from per-row band exceedance. Drive-level uncertainty: per-drive
  MAE spread, and a drive-resampled bootstrap once three drives exist
  (adjacent seconds are never treated as independent).
* **Skill and sensitivity** (B02.6): a training-fitted simple baseline scored
  on the same rows, and explicitly synthetic step faults of declared
  magnitude/duration run through the same gate. Synthetic detection is never
  called fault recall.
* **Criteria** come from the vehicle profile's ``behavior_validation.yaml``;
  :func:`apply_criteria` turns metrics into ``passed`` / ``failed`` /
  ``insufficient_evidence`` with every failure named, and
  :func:`src.behavior.behavior_models.serving_status` is the only consumer of
  that verdict.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.splits import (
    DEFAULT_JOIN_GAP_SECONDS,
    Drive,
    DriveSplit,
    LeakedDriveError,
    PieceRanges,
    UnorderedSessionsError,
    chronological_split,
    explicit_split,
    group_drives,
    parent_session,
    piece_time_ranges,
    training_leaks,
)
from src.artifact_hash import sha256_file_or_none
from src.behavior.behavior_decoder import band_half_widths, explain_expected_column, grade_deviation
from src.behavior.behavior_eligibility import (
    OFF_MAP,
    ServingSpec,
    eligibility_reasons,
    serving_spec,
)
from src.behavior.behavior_models import (
    BandExcessPersistence,
    BehaviorModel1D,
    BehaviorModel2D,
    BehaviorModels,
    FittedBandExcessPersistence,
    FoldMetrics,
    RobustBaselineFit,
    SyntheticSensitivity,
    TransientControlResult,
    ValidationCriteria,
    ValidationEvidence,
    contract_fingerprint,
)
from src.behavior.session_context import (
    StartContextSpec,
    append_start_context,
    start_context_columns,
)
from src.behavior.surface_eval import bin_indices, containing_cell_indices, trusted_cell_mask
from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column
from src.config import CanaryConfig
from src.dataframe_utils import numeric_values
from src.models.evaluation import default_profile_exclusion
from src.models.fusion import BandExcessAccumulator, PersistenceGate
from src.preprocessing import drives as _drives
from src.preprocessing.decode import decode_on_read, read_session_decoded_subset
from src.preprocessing.features import compute_rate_of_change
from src.session import SessionKind, session_kind
from src.signals.pairing import to_seconds

log = logging.getLogger(__name__)

#: Bump when the evaluator's definitions change (denominators, metrics,
#: event accounting). Persisted in every evidence record.
#: 2 (2026-09-22, B04.3): row times in true seconds for microsecond frames
#: (events, dedupe and synthetic windows were 1000x compressed), detection
#: delay, covered envelope and transient controls.
#: 3 (2026-09-23, B03.6): evaluated-hours exposure breaks at gaps longer than
#: the drive join gap. A directory that kept recording 11 h of a parked
#: truck (2026-09-23T032755) joined an evening and a morning drive into one
#: group, and its span counted the parked hours as watched time -- control
#: event rates came out ~10x too low.
#: 4 (2026-09-26, B05.3): a model carrying its own fitted
#: ``BandExcessPersistence`` is evented through it (``model_event_times``),
#: as the daemon does; every other model is evented exactly as in 3. Rows
#: stamped as not default-profile evidence (tuning captures, R03.12) are
#: refused as ``profile_excluded:*``, and evidence refuses an unstamped
#: capture piece.
EVALUATOR_VERSION = 4

#: Drives needed before a drive-resampled bootstrap interval is reported.
MIN_BOOTSTRAP_DRIVES = 3

#: Evaluated rows a cell / decile needs to count as visited (envelope coverage).
ENVELOPE_MIN_ROWS = 5

#: Name of the profile-data file holding the criteria (next to behavior_models.yaml).
VALIDATION_FILENAME = "behavior_validation.yaml"

#: D03.22 — classifier sidecars written before this commit derived mode
#: labels (and the events keyed on them) from raw broadcast counts whenever
#: Mode 01 was stale. Rows in a refit frame are re-segmented with current
#: code; the sidecar *events* that gate warmup recipes are not, so a sidecar
#: classified before the fix is not evidence until D03.22 relabels it.
MODE_LABEL_FIX_COMMIT = "f8b7d38"
MODE_LABEL_FIX_AT = datetime(2026, 9, 20, 4, 0, 7, tzinfo=UTC)


def mode_labels_predate_fix(classified_at: str) -> bool:
    """True when a sidecar was classified before :data:`MODE_LABEL_FIX_AT`
    (or its timestamp cannot be read — unknown is not current)."""
    try:
        stamp = datetime.fromisoformat(classified_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp < MODE_LABEL_FIX_AT


# ---------------------------------------------------------------------------
# Profile data: criteria document
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class ProvenanceResolution(_Strict):
    """A named decision for a session whose calibration identity is ambiguous.

    Sessions with an unknown tune revision, or whose recording crosses a
    flash, are refused until one of these names what to do and why.
    """

    session: str
    #: The calibration the session's rows belong to, or ``None`` with
    #: ``exclude: true`` to drop it from behavior-model evidence.
    revision: str | None = None
    exclude: bool = False
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _one_decision(self) -> ProvenanceResolution:
        if self.exclude == (self.revision is not None):
            raise ValueError("a resolution either names a revision or excludes the session")
        return self


class RegimeAxis(_Strict):
    """One reporting stratum: ``channel`` binned at ``edges`` (B04.1)."""

    channel: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    edges: list[float] = Field(min_length=2)

    @model_validator(mode="after")
    def _increasing(self) -> RegimeAxis:
        if any(b <= a for a, b in zip(self.edges, self.edges[1:], strict=False)):
            raise ValueError(f"{self.channel}: edges must strictly increase")
        return self


class SteadyStateRegime(_Strict):
    """Steady vs transient split: every listed rate within its limit (per s).

    Rates come from the feature pipeline's one rate-of-change implementation
    at its rolling horizon — the same derivative a persisted rate gate reads.
    """

    max_abs_rate_per_second: dict[str, float] = Field(min_length=1)
    basis: str = Field(min_length=1)


class ReportRegimes(_Strict):
    """How evaluation reports stratify rows before any comparison (B04.1).

    Reporting only: a regime never selects rows for fitting or serving. The
    calibration stratum is always the session's tune revision.
    """

    axes: dict[str, RegimeAxis] = Field(default_factory=dict)
    steady_state: SteadyStateRegime | None = None


class BehaviorValidationDocument(_Strict):
    """``behavior_validation.yaml`` — per-vehicle validation policy (B02.4/B02.6)."""

    schema_version: Literal[1] = 1
    #: The operator-approved segmenter vocabulary. A config whose modes
    #: differ can reproduce history but not produce corrected evidence.
    approved_operating_modes: list[str] = Field(min_length=1)
    #: Modes that existed historically and what replaced them.
    retired_operating_modes: dict[str, str] = Field(default_factory=dict)
    drive_join_gap_seconds: float = Field(default=DEFAULT_JOIN_GAP_SECONDS, ge=0)
    provenance_resolutions: list[ProvenanceResolution] = Field(default_factory=list)
    report_regimes: ReportRegimes | None = None
    recipes: dict[str, ValidationCriteria] = Field(default_factory=dict)

    def resolution_for(self, session: str) -> ProvenanceResolution | None:
        for resolution in self.provenance_resolutions:
            if resolution.session == session:
                return resolution
        return None

    def taxonomy_problems(self, configured_modes: Iterable[str]) -> list[str]:
        """Named differences between a config's modes and the approved set."""
        configured = set(configured_modes)
        approved = set(self.approved_operating_modes)
        problems = [
            f"retired_mode_present:{mode}->{self.retired_operating_modes[mode]}"
            for mode in sorted(configured & set(self.retired_operating_modes))
        ]
        problems += [
            f"unapproved_mode:{mode}"
            for mode in sorted(configured - approved - set(self.retired_operating_modes))
        ]
        problems += [f"approved_mode_missing:{mode}" for mode in sorted(approved - configured)]
        return problems


def validation_document_path(behavior_models_path: Path) -> Path:
    return behavior_models_path.parent / VALIDATION_FILENAME


def load_validation_document(path: Path) -> BehaviorValidationDocument | None:
    if not path.exists():
        return None
    return BehaviorValidationDocument.model_validate(yaml.safe_load(path.read_text()))


def file_identity(path: Path) -> str:
    """``<path>@sha256:<hex>`` or ``<path>@missing`` — for provenance records."""
    digest = sha256_file_or_none(path)
    return f"{path}@missing" if digest is None else f"{path}@sha256:{digest}"


def file_sha256(path: Path) -> str | None:
    """``sha256:<hex>`` of ``path``, or ``None`` when it is not a file."""
    digest = sha256_file_or_none(path)
    return None if digest is None else "sha256:" + digest


def decoder_identity(
    *, bindings_path: Path, dids_path: Path, dbc_dir: Path | None = None
) -> dict[str, str]:
    """Content identity of every catalog that turns bytes into model inputs."""
    identity = {
        "bindings": file_sha256(bindings_path) or "missing",
        "dids": file_sha256(dids_path) or "missing",
    }
    if dbc_dir is not None:
        digest = hashlib.sha256()
        files = sorted(dbc_dir.glob("*.dbc")) if dbc_dir.exists() else []
        for dbc in files:
            digest.update(dbc.name.encode())
            digest.update(dbc.read_bytes())
        identity["dbc"] = "sha256:" + digest.hexdigest() if files else "none"
    return identity


# ---------------------------------------------------------------------------
# Drive grouping and grouped splits (B02.3) live in src.analysis.splits
# (R05.13); the names are re-exported above for existing importers.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Row evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventPolicy:
    """The daemon's alert path, offline: persistence gate + emitter dedupe."""

    required: int = 3
    window: int | None = 5
    window_seconds: float | None = 8.0
    dedupe_seconds: float = 30.0
    interval_seconds: float = 1.0
    #: Exposure (the events/h denominator) stops accumulating across a gap
    #: between evaluated rows longer than this: the drive join gap, so rows
    #: that would have been two drives in two directories are two watches in
    #: one directory too (see :func:`_evaluated_hours`).
    exposure_break_seconds: float = DEFAULT_JOIN_GAP_SECONDS

    @classmethod
    def from_config(
        cls, config: CanaryConfig, document: BehaviorValidationDocument | None = None
    ) -> EventPolicy:
        alerts = config.alerts
        return cls(
            required=alerts.min_consecutive_ticks,
            window=alerts.persistence_window_ticks,
            window_seconds=alerts.persistence_window_seconds,
            dedupe_seconds=alerts.min_seconds_between_events,
            interval_seconds=1.0 / config.preprocessing.resample_hz,
            exposure_break_seconds=(
                document.drive_join_gap_seconds
                if document is not None
                else DEFAULT_JOIN_GAP_SECONDS
            ),
        )

    def describe(self) -> str:
        return (
            f"{self.required}-of-{self.window or self.required} ticks within "
            f"{self.window_seconds} s, then {self.dedupe_seconds} s dedupe"
        )


# ---------------------------------------------------------------------------
# Default-profile evidence only (B05.3; R03.12, operator 2026-09-24/25)
# ---------------------------------------------------------------------------

#: Per-row stamp naming why a row's session is not behavior-model evidence
#: (``None``: it is). Written by :func:`stamp_profile_exclusions` from the
#: session directories themselves; read by :func:`evaluate_rows` and the refit.
PROFILE_EXCLUSION_COLUMN = "context:profile_exclusion"
#: Refusal code prefix for a row whose session is not the default profile.
PROFILE_EXCLUDED = "profile_excluded"
#: Why a behavior fit or its evidence refused a frame that carries capture or
#: tuning pieces with no :data:`PROFILE_EXCLUSION_COLUMN` verdict.
PROFILE_UNVERIFIED = "profile_unverified"

_PROFILE_SCOPED_KINDS = frozenset({SessionKind.CAPTURE, SessionKind.TUNING})

#: Which recordings count as evidence: ``"default_profile"`` (every caller) or
#: ``"every_recording"`` (a historical reproduction only).
ProfileScope = Literal["default_profile", "every_recording"]


class UnverifiedProfileError(ValueError):
    """Capture or tuning pieces reached behavior evidence without a verdict."""


def profile_exclusions(sessions: Sequence[Path]) -> dict[str, str]:
    """``{session name: reason}`` for the sessions that are not behavior evidence.

    The operator's standing rule (2026-09-24/25; R03.12(a)/(b)) is that
    tuning captures never train, evaluate or score the IF, the sequence model
    or the behavior deviations. The decision per directory is R04.6's
    :func:`src.models.evaluation.default_profile_exclusion`, reused rather
    than restated: a ``_tuning-*`` session, or a capture whose own manifest
    says it is not corpus-eligible.
    """

    return {
        Path(session).name: reason
        for session in sessions
        if (reason := default_profile_exclusion(Path(session))) is not None
    }


def stamp_profile_exclusions(frame: pd.DataFrame, sessions: Sequence[Path]) -> pd.DataFrame:
    """``frame`` with :data:`PROFILE_EXCLUSION_COLUMN` set from ``sessions``.

    Excluded rows stay in the frame so drive grouping still sees the capture
    that bridged two parts of one drive (a held-out fold stays honest), but
    they are refused wherever evidence is counted. A capture or tuning piece
    whose directory is not among ``sessions`` cannot be checked and is
    refused too, never assumed eligible.
    """
    if SESSION_COLUMN not in frame.columns:
        raise UnverifiedProfileError(
            f"{PROFILE_UNVERIFIED}: no {SESSION_COLUMN} column to attribute rows to sessions"
        )
    names = {Path(session).name for session in sessions}
    excluded = profile_exclusions(sessions)
    codes: dict[str, str | None] = {}
    for parent in pd.unique(frame[SESSION_COLUMN].astype(str).map(parent_session)):
        if parent in excluded:
            codes[parent] = f"{PROFILE_EXCLUDED}:not_default_profile"
        elif parent not in names and session_kind(parent) in _PROFILE_SCOPED_KINDS:
            codes[parent] = f"{PROFILE_EXCLUDED}:no_session_directory"
        else:
            codes[parent] = None
    parents = frame[SESSION_COLUMN].astype(str).map(parent_session)
    column = pd.Series([codes[parent] for parent in parents], index=frame.index, dtype=object)
    return frame.assign(**{PROFILE_EXCLUSION_COLUMN: column})


def profile_refusals(frame: pd.DataFrame) -> np.ndarray:
    """Per-row profile refusal code (``None``: default-profile evidence).

    Raises :class:`UnverifiedProfileError` when the frame holds a capture or
    tuning piece but was never stamped: silently counting one would be the
    failure this exists to prevent. An unstamped frame of plain sessions has
    nothing to exclude.
    """
    if PROFILE_EXCLUSION_COLUMN in frame.columns:
        values = frame[PROFILE_EXCLUSION_COLUMN].to_numpy(dtype=object)
        return np.asarray([None if pd.isna(v) else str(v) for v in values], dtype=object)
    if SESSION_COLUMN in frame.columns:
        scoped = sorted(
            parent
            for parent in pd.unique(frame[SESSION_COLUMN].astype(str).map(parent_session))
            if session_kind(parent) in _PROFILE_SCOPED_KINDS
        )
        if scoped:
            raise UnverifiedProfileError(
                f"{PROFILE_UNVERIFIED}: capture/tuning piece(s) {', '.join(scoped)} carry no "
                "corpus-eligibility verdict; stamp the frame with stamp_profile_exclusions "
                "(tuning captures are never behavior-model evidence, R03.12)"
            )
    return np.full(len(frame), None, dtype=object)


@dataclass(frozen=True)
class RowEvaluation:
    """Per-row outcome arrays for one model over one frame."""

    pieces: np.ndarray
    seconds: np.ndarray
    modes: np.ndarray
    present: np.ndarray
    fresh: np.ndarray
    reasons: np.ndarray
    expected: np.ndarray
    actual: np.ndarray
    severity: np.ndarray
    warning_half_width: np.ndarray
    critical_half_width: np.ndarray
    x: np.ndarray
    y: np.ndarray

    @property
    def evaluated(self) -> np.ndarray:
        return np.asarray(pd.isna(self.reasons), dtype=bool)

    @property
    def eligible(self) -> np.ndarray:
        off_map = np.asarray([reason == OFF_MAP for reason in self.reasons], dtype=bool)
        return np.asarray(self.evaluated | off_map, dtype=bool)

    @property
    def residual(self) -> np.ndarray:
        return np.asarray(self.actual - self.expected, dtype=float)

    def subset(self, mask: np.ndarray) -> RowEvaluation:
        return RowEvaluation(
            **{name: getattr(self, name)[mask] for name in self.__dataclass_fields__}
        )


def _seconds(frame: pd.DataFrame, interval: float) -> np.ndarray:
    """Epoch seconds per row.

    The unit is pinned to nanoseconds before the integer view: session frames
    carry ``datetime64[us]`` (pandas 3), and ``int64 / 1e9`` on microseconds
    compressed every interval 1000x — the persistence window (8 s) and dedupe
    (30 s) then spanned hours of rows and each synthetic window a whole
    drive. Evaluator version 2 fixed that (B04.3).
    """
    if TIMESTAMP_COLUMN in frame.columns:
        return to_seconds(frame[TIMESTAMP_COLUMN])
    return np.arange(len(frame), dtype=float) * interval


def evaluate_rows(
    model: BehaviorModel1D | BehaviorModel2D,
    frame: pd.DataFrame,
    *,
    spec: ServingSpec | None = None,
    interval_seconds: float = 1.0,
    output_offset: float | np.ndarray = 0.0,
    profile_scope: ProfileScope = "default_profile",
) -> RowEvaluation:
    """Evaluate ``model`` on every row of ``frame`` through the B01 evaluator.

    ``spec`` replaces the row-selection contract — used to reproduce a legacy
    artifact (no persisted contract) under its catalog recipe's contract; the
    model's own gates still apply on top. ``output_offset`` adds an explicitly
    synthetic step to the actual output column (and to any input that *is*
    that column), then re-evaluates — derived columns are not recomputed. A
    per-row array (aligned with ``frame``) injects a shaped excursion, e.g. a
    short transient control (B04.3).

    ``profile_scope`` (B05.3; R03.12): by default every row goes through
    :func:`profile_refusals`, so a stamped non-default-profile row is refused
    by name and an *unstamped* frame holding a capture or tuning piece raises
    :class:`UnverifiedProfileError` instead of being graded. Only a historical
    reproduction of numbers an artifact recorded before the rule passes
    ``"every_recording"``.
    """
    if profile_scope == "default_profile":
        profile_refusals(frame)  # an unverified frame is refused before any work
    model_spec = serving_spec(model)
    spec = spec or model_spec
    output = model_spec.output
    work = frame
    cold = spec.eligibility.cold_start if spec.eligibility is not None else None
    if cold is not None and SESSION_COLUMN in work.columns:
        # Same engine-start fold the refit and live source run; a legacy
        # model's frame was built without a contract that asked for it.
        if any(column not in work.columns for column in start_context_columns(cold)):
            work = append_start_context(work, [StartContextSpec.from_requirement(cold)])
    offset = np.asarray(output_offset, dtype=float)
    if offset.ndim > 0 and offset.shape != (len(work),):
        raise ValueError(f"output_offset has {offset.shape[0]} rows, frame has {len(work)}")
    if np.any(offset != 0.0):
        work = work.copy()
        work[output] = pd.to_numeric(work[output], errors="coerce").to_numpy(dtype=float) + offset
    channels = [*model_spec.inputs, output]
    present = np.ones(len(work), dtype=bool)
    fresh = np.ones(len(work), dtype=bool)
    for channel in channels:
        values = numeric_values(work, channel)
        present &= np.isfinite(values)
        gap = gap_column(channel)
        if gap in work.columns:
            fresh &= ~work[gap].fillna(False).astype(bool).to_numpy()
    fresh &= present

    contract_reasons = eligibility_reasons(work, spec, include_actual=True)
    expected, model_reasons = explain_expected_column(work, model)
    reasons = np.where(pd.isna(contract_reasons), model_reasons, contract_reasons).astype(object)
    if profile_scope == "default_profile":
        # B05.3 — a stamped non-default-profile row is refused by name first.
        profile = profile_refusals(work)
        reasons = np.where(pd.isna(profile), reasons, profile).astype(object)
    actual = numeric_values(work, output)
    evaluated = np.asarray(pd.isna(reasons), dtype=bool) & np.isfinite(expected)
    evaluated &= np.isfinite(actual)
    # A row the contract passed with a finite expected but no finite actual
    # already carries an actual_* reason from ``include_actual``.
    severity = np.full(len(work), None, dtype=object)
    warn = np.full(len(work), np.nan)
    crit = np.full(len(work), np.nan)
    axis_channels = (
        [model.axis.channel]
        if isinstance(model, BehaviorModel1D)
        else [axis.channel for axis in model.axes]
    )
    axis_values = [numeric_values(work, channel) for channel in axis_channels]
    for i in np.flatnonzero(evaluated):
        point = {
            channel: float(values[i])
            for channel, values in zip(axis_channels, axis_values, strict=True)
        }
        severity[i] = grade_deviation(model, point, float(actual[i] - expected[i]))
        w, c = band_half_widths(model, point)
        warn[i] = np.nan if w is None else w
        crit[i] = np.nan if c is None else c
    pieces = (
        work[SESSION_COLUMN].astype(str).to_numpy()
        if SESSION_COLUMN in work.columns
        else np.full(len(work), "", dtype=object)
    )
    modes = (
        work[MODE_COLUMN].astype(str).to_numpy()
        if MODE_COLUMN in work.columns
        else np.full(len(work), "all", dtype=object)
    )
    return RowEvaluation(
        pieces=pieces,
        seconds=_seconds(work, interval_seconds),
        modes=modes,
        present=present,
        fresh=fresh,
        reasons=reasons,
        expected=np.where(evaluated, expected, np.nan),
        actual=actual,
        severity=severity,
        warning_half_width=warn,
        critical_half_width=crit,
        x=axis_values[0],
        y=axis_values[1] if len(axis_values) > 1 else np.full(len(work), np.nan),
    )


# ---------------------------------------------------------------------------
# Events, baselines, envelope
# ---------------------------------------------------------------------------


def _verdicts(ev: RowEvaluation) -> np.ndarray:
    """Per-row detector verdict: ``None`` (no measurement), True/False."""
    out = np.full(len(ev.reasons), None, dtype=object)
    evaluated = ev.evaluated
    out[evaluated] = [sev in ("warning", "critical") for sev in ev.severity[evaluated]]
    return out


def count_events(
    verdicts: np.ndarray,
    seconds: np.ndarray,
    groups: np.ndarray,
    policy: EventPolicy,
) -> int:
    """Events emitted by the daemon's persistence gate + dedupe, per group.

    ``groups`` resets the gate (one per drive or synthetic window); refused
    rows are missing verdicts that freeze the window, exactly as live.
    """
    return len(event_times(verdicts, seconds, groups, policy))


def event_times(
    verdicts: np.ndarray,
    seconds: np.ndarray,
    groups: np.ndarray,
    policy: EventPolicy,
) -> list[tuple[object, float]]:
    """``(group, emit time)`` for every event :func:`count_events` counts."""
    events: list[tuple[object, float]] = []
    # One sort instead of a mask per group: synthetic windows number in the
    # thousands on a month of drives. Within a group rows are time-ordered,
    # ties keeping row order (lexsort is stable).
    codes, uniques = pd.factorize(pd.Series(groups, dtype=object), sort=False)
    ordered = np.lexsort((seconds, codes))
    boundaries = np.flatnonzero(np.diff(codes[ordered])) + 1
    for order in np.split(ordered, boundaries):
        if not order.size:
            continue
        code = codes[order[0]]
        group = uniques[code] if code >= 0 else groups[order[0]]
        gate = PersistenceGate(
            policy.required, window=policy.window, window_seconds=policy.window_seconds
        )
        last_emit: float | None = None
        for i in order:
            verdict = verdicts[i]
            now = float(seconds[i]) if np.isfinite(seconds[i]) else 0.0
            sustained = gate.observe("behavior", verdict, now=now)
            if verdict is not True or not sustained:
                continue
            if last_emit is not None and now - last_emit < policy.dedupe_seconds:
                continue
            last_emit = now
            events.append((group, now))
    return events


def band_excess_event_times(
    ev: RowEvaluation,
    groups: np.ndarray,
    persistence: FittedBandExcessPersistence,
    *,
    dedupe_seconds: float,
) -> list[tuple[object, float]]:
    """``(group, emit time)`` under a model's fitted band-excess persistence (B05.3).

    The daemon's path, offline: per group a fresh :class:`PersistenceGate`
    whose :meth:`~PersistenceGate.observe_band_excess` holds the accumulator,
    an event only on a firing tick that is sustained, then the emitter
    dedupe. Refused rows are missing verdicts that leave the sums untouched.
    """
    events: list[tuple[object, float]] = []
    verdicts = _verdicts(ev)
    residual = ev.residual
    half_width = ev.warning_half_width
    codes, uniques = pd.factorize(pd.Series(groups, dtype=object), sort=False)
    ordered = np.lexsort((ev.seconds, codes))
    boundaries = np.flatnonzero(np.diff(codes[ordered])) + 1
    for order in np.split(ordered, boundaries):
        if not order.size:
            continue
        code = codes[order[0]]
        group = uniques[code] if code >= 0 else groups[order[0]]
        gate = PersistenceGate(1)
        last_emit: float | None = None
        for i in order:
            verdict = verdicts[i]
            now = float(ev.seconds[i]) if np.isfinite(ev.seconds[i]) else 0.0
            width = float(half_width[i]) if np.isfinite(half_width[i]) else None
            sustained = gate.observe_band_excess(
                "behavior",
                float(residual[i]) if verdict is not None else None,
                width,
                firing=verdict,
                limit=persistence.limit,
                reset_gap_seconds=persistence.rule.reset_gap_seconds,
                now=now,
            )
            if verdict is not True or not sustained:
                continue
            if last_emit is not None and now - last_emit < dedupe_seconds:
                continue
            last_emit = now
            events.append((group, now))
    return events


def model_event_times(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    groups: np.ndarray,
    policy: EventPolicy,
) -> list[tuple[object, float]]:
    """Events ``model`` emits over ``ev``: its own persistence, else the daemon gate.

    The one place the evaluator decides how a model is evented, so held-out
    control rates, synthetic sensitivity and transient controls all count
    what the daemon would emit for *this* model (B05.3).
    """
    if model.event_persistence is not None:
        return band_excess_event_times(
            ev, groups, model.event_persistence, dedupe_seconds=policy.dedupe_seconds
        )
    return event_times(_verdicts(ev), ev.seconds, groups, policy)


#: B05.3 — why a band-excess persistence could not be fitted on a training fold.
PERSISTENCE_NO_TRAINING_EXCURSION = "event_persistence_unfittable:no_training_excursion"


def fit_band_excess_persistence(
    rule: BandExcessPersistence,
    train_ev: RowEvaluation,
    groups: np.ndarray,
) -> FittedBandExcessPersistence | str:
    """Fit ``rule``'s limit on the training fold's own rows (B02.3, B05.3).

    Runs the accumulator over every evaluated training row, per drive group
    and in time order, and takes the largest accumulated excess reached: the
    limit an event must strictly exceed, so no ordinary excursion on the
    training drives completes one. Test drives never enter. Returns a named
    refusal instead of a zero limit when no training row ever left the band
    (nothing says how large ordinary control gets), or when there are no
    evaluated rows at all.
    """
    evaluated = train_ev.evaluated & np.isfinite(train_ev.warning_half_width)
    if not evaluated.any():
        return f"{PERSISTENCE_NO_TRAINING_EXCURSION}: no evaluated training rows"
    largest = 0.0
    excursions = 0
    drives: set[str] = set()
    residual = train_ev.residual
    width = train_ev.warning_half_width
    for group in pd.unique(pd.Series(groups[evaluated], dtype=object)):
        rows = np.flatnonzero(evaluated & (groups == group))
        rows = rows[np.argsort(train_ev.seconds[rows], kind="stable")]
        drives.add(str(group))
        accumulator = BandExcessAccumulator(rule.reset_gap_seconds)
        for i in rows:
            now = float(train_ev.seconds[i])
            last = accumulator.last_seconds
            restarts = last is None or now - last > rule.reset_gap_seconds
            idle = restarts or accumulator.statistic == 0.0
            after = accumulator.update(float(residual[i]), float(width[i]), now)
            if idle and after > 0.0:
                excursions += 1
            largest = max(largest, after)
    if largest <= 0.0:
        return (
            f"{PERSISTENCE_NO_TRAINING_EXCURSION}: {int(evaluated.sum())} evaluated training "
            "rows never left the warning band"
        )
    return FittedBandExcessPersistence(
        rule=rule,
        limit=largest,
        training_drives=sorted(drives),
        training_rows=int(evaluated.sum()),
        training_excursions=excursions,
    )


BaselineKind = Literal["training_median", "training_mode_median"]


def fit_baseline(actual: np.ndarray, modes: np.ndarray, kind: BaselineKind) -> dict[str, float]:
    """Training-fitted simple baseline: robust median, optionally per mode."""
    finite = np.isfinite(actual)
    if not finite.any():
        return {}
    baseline = {"__all__": float(np.median(actual[finite]))}
    if kind == "training_mode_median":
        for mode in pd.unique(modes[finite]):
            values = actual[finite & (modes == mode)]
            if values.size:
                baseline[str(mode)] = float(np.median(values))
    return baseline


def baseline_predictions(baseline: Mapping[str, float], modes: np.ndarray) -> np.ndarray:
    if not baseline:
        return np.full(len(modes), np.nan)
    fallback = baseline["__all__"]
    return np.asarray([baseline.get(str(mode), fallback) for mode in modes], dtype=float)


def envelope_coverage(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    *,
    min_rows: int = ENVELOPE_MIN_ROWS,
) -> float | None:
    """Fraction of the model's trusted operating envelope the rows visited.

    2D: trusted cells (the cells serving uses) with ``min_rows`` evaluated
    rows. 1D lookup/poly: domain deciles. Robust constant baselines have no
    envelope (``None``).
    """
    evaluated = ev.evaluated
    if isinstance(model, BehaviorModel2D):
        trusted = trusted_cell_mask(model)
        if not trusted.any():
            return 0.0
        idx = containing_cell_indices(model, ev.x[evaluated], ev.y[evaluated])
        counts = np.bincount(idx[idx >= 0], minlength=len(trusted))
        return float(((counts >= min_rows) & trusted).sum() / trusted.sum())
    if isinstance(model.fit, RobustBaselineFit):
        return None
    lo, hi = model.axis.domain
    edges = np.linspace(lo, hi, 11)
    bins = bin_indices(ev.x[evaluated], edges)
    counts = np.bincount(bins, minlength=10)
    return float((counts >= min_rows).sum() / 10)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _r_squared(actual: np.ndarray, predicted: np.ndarray) -> float | None:
    if actual.size < 2:
        return None
    denominator = float(np.sum((actual - actual.mean()) ** 2))
    if denominator <= 0:
        return None
    return float(1.0 - np.sum((actual - predicted) ** 2) / denominator)


def _median_or_none(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    return float(np.median(finite)) if finite.size else None


def _evaluated_hours(
    seconds: np.ndarray,
    evaluated: np.ndarray,
    drives: np.ndarray,
    *,
    interval_seconds: float,
    break_seconds: float = DEFAULT_JOIN_GAP_SECONDS,
) -> float:
    """Wall-clock exposure of the evaluated rows, in hours (B02.5).

    An event *rate* needs the time the detector was actually watching, not a
    row count: a slowly polled output (the catalyst PID on its slow tier) is
    evaluated in 1 s pairs ~17 s apart, and ``rows / 3600`` would call that
    exposure a twentieth of what it was. Per drive, first-to-last evaluated
    time plus one tick (so a lone row counts one interval and contiguous
    1 Hz rows give exactly the old ``n`` seconds), summed over drives.

    A gap between consecutive evaluated rows longer than ``break_seconds``
    (the drive join gap) ends one watch and starts the next: a directory
    that kept recording a parked truck overnight is one "drive" to
    :func:`group_drives`, and its span is not time anything was watched
    (EVALUATOR_VERSION 3).
    """
    total = 0.0
    for drive in set(drives[evaluated].tolist()):
        stamps = seconds[evaluated & (drives == drive)]
        stamps = np.sort(stamps[np.isfinite(stamps)])
        if not stamps.size:
            continue
        breaks = np.flatnonzero(np.diff(stamps) > break_seconds)
        starts = np.concatenate(([0], breaks + 1))
        ends = np.concatenate((breaks, [stamps.size - 1]))
        total += float(np.sum(stamps[ends] - stamps[starts])) + interval_seconds * starts.size
    return total / 3600.0


def fold_metrics(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    *,
    drive_of_piece: Mapping[str, str],
    policy: EventPolicy,
    baseline: Mapping[str, float] | None = None,
    bootstrap_seed: int = 0,
) -> FoldMetrics:
    """Denominators, physical-unit accuracy and events over ``ev``'s rows."""
    evaluated = ev.evaluated
    eligible = ev.eligible
    drives = np.asarray([drive_of_piece.get(str(p), str(p)) for p in ev.pieces], dtype=object)
    refusals = Counter(str(r) for r in ev.reasons[~evaluated])
    residual = ev.residual[evaluated]
    abs_err = np.abs(residual)
    actual = ev.actual[evaluated]
    expected = ev.expected[evaluated]
    n = int(evaluated.sum())
    severity = ev.severity[evaluated]
    metrics: dict[str, Any] = {
        "drives": len(set(drives[evaluated].tolist())),
        "rows": len(ev.reasons),
        "input_present": int(ev.present.sum()),
        "fresh": int(ev.fresh.sum()),
        "eligible": int(eligible.sum()),
        "evaluated": n,
        "coverage": (n / int(eligible.sum())) if eligible.any() else None,
        "refusals": dict(sorted(refusals.items(), key=lambda kv: (-kv[1], kv[0]))),
        "evaluated_hours": _evaluated_hours(
            ev.seconds,
            evaluated,
            drives,
            interval_seconds=policy.interval_seconds,
            break_seconds=policy.exposure_break_seconds,
        ),
        "envelope_coverage": envelope_coverage(model, ev),
    }
    if n:
        metrics.update(
            mae=float(abs_err.mean()),
            bias=float(residual.mean()),
            abs_error_p50=float(np.quantile(abs_err, 0.50)),
            abs_error_p90=float(np.quantile(abs_err, 0.90)),
            abs_error_p95=float(np.quantile(abs_err, 0.95)),
            r_squared=_r_squared(actual, expected),
            warning_half_width_median=_median_or_none(ev.warning_half_width[evaluated]),
            critical_half_width_median=_median_or_none(ev.critical_half_width[evaluated]),
            warning_row_rate=float(np.mean(severity == "warning")),
            critical_row_rate=float(np.mean(severity == "critical")),
        )
        events = len(model_event_times(model, ev, drives, policy))
        metrics["events"] = events
        hours = metrics["evaluated_hours"]
        metrics["events_per_hour"] = events / hours if hours > 0 else None
        if baseline:
            base = baseline_predictions(baseline, ev.modes[evaluated])
            same = np.isfinite(base)
            if same.any():
                base_mae = float(np.abs(actual[same] - base[same]).mean())
                model_mae = float(abs_err[same].mean())
                metrics["baseline_mae"] = base_mae
                metrics["skill_vs_baseline"] = 1.0 - model_mae / base_mae if base_mae > 0 else None
        per_drive = {
            drive: float(abs_err[drives[evaluated] == drive].mean())
            for drive in pd.unique(drives[evaluated])
        }
        metrics["drive_mae_min"] = min(per_drive.values())
        metrics["drive_mae_max"] = max(per_drive.values())
        if len(per_drive) >= MIN_BOOTSTRAP_DRIVES:
            metrics["mae_drive_bootstrap_90"] = _drive_bootstrap(
                abs_err, drives[evaluated], seed=bootstrap_seed
            )
    return FoldMetrics(**metrics)


def _drive_bootstrap(
    abs_err: np.ndarray, drives: np.ndarray, *, seed: int, reps: int = 2000
) -> list[float]:
    """90 % interval of pooled MAE, resampling whole drives (not rows)."""
    rng = np.random.default_rng(seed)
    labels = pd.unique(drives)
    sums = np.asarray([abs_err[drives == d].sum() for d in labels])
    counts = np.asarray([(drives == d).sum() for d in labels], dtype=float)
    draws = rng.integers(0, len(labels), size=(reps, len(labels)))
    pooled = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
    return [float(np.quantile(pooled, 0.05)), float(np.quantile(pooled, 0.95))]


def breakdown(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    *,
    by: Literal["drive", "regime"],
    drive_of_piece: Mapping[str, str],
    policy: EventPolicy,
    baseline: Mapping[str, float] | None = None,
) -> dict[str, FoldMetrics]:
    keys = (
        np.asarray([drive_of_piece.get(str(p), str(p)) for p in ev.pieces], dtype=object)
        if by == "drive"
        else ev.modes
    )
    return breakdown_by_labels(
        model, ev, keys, drive_of_piece=drive_of_piece, policy=policy, baseline=baseline
    )


def breakdown_by_labels(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    labels: np.ndarray,
    *,
    drive_of_piece: Mapping[str, str],
    policy: EventPolicy,
    baseline: Mapping[str, float] | None = None,
) -> dict[str, FoldMetrics]:
    """:func:`fold_metrics` per label (one label per row of ``ev``).

    Only groups the model could have spoken for: a group with no eligible
    row says nothing about accuracy (its refusals are in the fold totals).
    Events restart per drive *within* a group, so a stratum's event count is
    the persistence gate run on that stratum's rows only.
    """
    eligible = ev.eligible
    return {
        str(key): fold_metrics(
            model,
            ev.subset(labels == key),
            drive_of_piece=drive_of_piece,
            policy=policy,
            baseline=baseline,
        )
        for key in pd.unique(labels[eligible])
    }


def _bin_labels(values: np.ndarray, axis: RegimeAxis) -> np.ndarray:
    labels = np.full(len(values), "missing", dtype=object)
    edges = axis.edges
    finite = np.isfinite(values)
    labels[finite & (values < edges[0])] = f"<{edges[0]:g} {axis.unit}"
    labels[finite & (values >= edges[-1])] = f">={edges[-1]:g} {axis.unit}"
    for lo, hi in zip(edges, edges[1:], strict=False):
        labels[finite & (values >= lo) & (values < hi)] = f"[{lo:g},{hi:g}) {axis.unit}"
    return labels


def regime_labels(
    frame: pd.DataFrame,
    regimes: ReportRegimes | None,
    *,
    revision_of_piece: Callable[[str], str | None] | None = None,
) -> dict[str, np.ndarray]:
    """Per-row stratum labels for evaluation reports (B04.1).

    ``calibration`` is the tune revision of each row's session (``unknown``
    when the registry cannot place it). Axis strata label a stale or absent
    channel ``stale`` / ``missing`` — an unavailable context is its own
    stratum, never folded into a numeric bin. The steady-state split reads
    each rate from :func:`src.preprocessing.features.compute_rate_of_change`
    and labels a row ``transient`` if any listed rate exceeds its limit,
    ``rate_unavailable`` if any rate is undefined, else ``steady``.
    """
    from src.columns import roc_column

    out: dict[str, np.ndarray] = {}
    if revision_of_piece is not None and SESSION_COLUMN in frame.columns:
        pieces = frame[SESSION_COLUMN].astype(str).to_numpy()
        cache: dict[str, str] = {}
        revisions: list[str] = []
        for piece in pieces:
            if piece not in cache:
                cache[piece] = revision_of_piece(piece) or "unknown"
            revisions.append(cache[piece])
        out["calibration"] = np.asarray(revisions, dtype=object)
    if regimes is None:
        return out
    for name, axis in regimes.axes.items():
        labels = _bin_labels(numeric_values(frame, axis.channel), axis)
        gap = gap_column(axis.channel)
        if gap in frame.columns:
            stale = frame[gap].fillna(True).astype(bool).to_numpy()
            labels[stale] = "stale"
        out[name] = labels
    steady = regimes.steady_state
    if steady is not None:
        channels = list(steady.max_abs_rate_per_second)
        missing_roc = [c for c in channels if roc_column(c) not in frame.columns]
        rates = compute_rate_of_change(frame, missing_roc) if missing_roc else frame
        transient = np.zeros(len(frame), dtype=bool)
        undefined = np.zeros(len(frame), dtype=bool)
        for channel, limit in steady.max_abs_rate_per_second.items():
            rate = numeric_values(rates, roc_column(channel))
            undefined |= ~np.isfinite(rate)
            with np.errstate(invalid="ignore"):
                transient |= np.abs(rate) > limit
        split = np.full(len(frame), "steady", dtype=object)
        split[undefined] = "rate_unavailable"
        split[transient] = "transient"
        out["steady_state"] = split
    return out


# ---------------------------------------------------------------------------
# Synthetic sensitivity (B02.6)
# ---------------------------------------------------------------------------


def synthetic_sensitivity(
    model: BehaviorModel1D | BehaviorModel2D,
    frame: pd.DataFrame,
    base: RowEvaluation,
    criteria: ValidationCriteria,
    *,
    policy: EventPolicy,
    spec: ServingSpec | None = None,
) -> list[SyntheticSensitivity]:
    """Detection of each declared synthetic step on non-overlapping windows.

    Every ``duration_seconds`` window of each piece holding at least one
    evaluated row is scored twice with a fresh gate (no carried hits): as
    recorded (the no-fault control) and with the step added. Only windows
    whose control stayed quiet are trials — a window that alerts anyway
    cannot show the injection was what the detector saw.
    """
    out: list[SyntheticSensitivity] = []
    for fault in criteria.synthetic_faults:
        offset = fault.magnitude if fault.direction == "positive" else -fault.magnitude
        injected = evaluate_rows(
            model,
            frame,
            spec=spec,
            interval_seconds=policy.interval_seconds,
            output_offset=offset,
        )
        windows = _windows(base, fault.duration_seconds)
        valid = windows >= 0
        first_events = _first_event_per_window(model, injected, windows, valid, policy)
        detected = set(first_events)
        control = _windows_with_event(model, base, windows, valid, policy)
        scored = {int(w) for w, ok in zip(windows, valid, strict=True) if ok}
        scored = {w for w in scored if base.evaluated[windows == w].any()}
        control &= scored
        clean = scored - control
        detected &= clean
        onsets = _first_evaluated_seconds(base, windows)
        delays = np.asarray([first_events[w] - onsets[w] for w in sorted(detected)], dtype=float)
        clean_rows = np.isin(windows, sorted(clean)) & base.evaluated
        out.append(
            SyntheticSensitivity(
                magnitude=fault.magnitude,
                direction=fault.direction,
                duration_seconds=fault.duration_seconds,
                windows=len(scored),
                control_detected=len(control),
                clean_windows=len(clean),
                detected=len(detected),
                detection_fraction=(len(detected) / len(clean)) if clean else None,
                detection_delay_seconds_median=float(np.median(delays)) if delays.size else None,
                detection_delay_seconds_max=float(delays.max()) if delays.size else None,
                covered_envelope=(
                    envelope_coverage(model, base.subset(clean_rows)) if clean else None
                ),
            )
        )
    return out


def transient_controls(
    model: BehaviorModel1D | BehaviorModel2D,
    frame: pd.DataFrame,
    base: RowEvaluation,
    criteria: ValidationCriteria,
    *,
    policy: EventPolicy,
    spec: ServingSpec | None = None,
) -> list[TransientControlResult]:
    """Events raised by each declared *short* synthetic excursion (B04.3).

    Windows are the resolution's ``min_duration_seconds``. In each window
    holding an evaluated row whose unmodified control stayed quiet, the
    excursion is added for ``duration_seconds`` starting at the window's first
    evaluated row, then scored through the same eligibility and persistence
    gate. An event is the model mistaking a transient for the sustained
    change it is meant to resolve.
    """
    resolution = criteria.resolution
    if resolution is None:
        return []
    windows = _windows(base, resolution.min_duration_seconds)
    valid = windows >= 0
    scored = {int(w) for w, ok in zip(windows, valid, strict=True) if ok}
    scored = {w for w in scored if base.evaluated[windows == w].any()}
    control = _windows_with_event(model, base, windows, valid, policy) & scored
    clean = scored - control
    onsets = _first_evaluated_seconds(base, windows)
    out: list[TransientControlResult] = []
    for case in resolution.transient_controls:
        sign = 1.0 if case.direction == "positive" else -1.0
        offset = np.zeros(len(base.seconds), dtype=float)
        for window in clean:
            rows = windows == window
            start = onsets[window]
            within = rows & (base.seconds >= start) & (base.seconds < start + case.duration_seconds)
            offset[within] = sign * case.magnitude
        injected = evaluate_rows(
            model, frame, spec=spec, interval_seconds=policy.interval_seconds, output_offset=offset
        )
        detected = _windows_with_event(model, injected, windows, valid, policy) & clean
        out.append(
            TransientControlResult(
                magnitude=case.magnitude,
                direction=case.direction,
                duration_seconds=case.duration_seconds,
                windows=len(scored),
                clean_windows=len(clean),
                detected=len(detected),
                detection_fraction=(len(detected) / len(clean)) if clean else None,
            )
        )
    return out


def _first_evaluated_seconds(ev: RowEvaluation, windows: np.ndarray) -> dict[int, float]:
    """Per window, the time of its first evaluated row (the visible onset)."""
    out: dict[int, float] = {}
    evaluated = ev.evaluated & (windows >= 0) & np.isfinite(ev.seconds)
    for window in pd.unique(windows[evaluated]):
        out[int(window)] = float(np.min(ev.seconds[evaluated & (windows == window)]))
    return out


def _first_event_per_window(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    windows: np.ndarray,
    valid: np.ndarray,
    policy: EventPolicy,
) -> dict[int, float]:
    first: dict[int, float] = {}
    for window, when in model_event_times(model, ev.subset(valid), windows[valid], policy):
        key = int(window)  # type: ignore[call-overload]
        first[key] = min(when, first.get(key, when))
    return first


def _windows(ev: RowEvaluation, duration: float) -> np.ndarray:
    """Window id per row (non-overlapping ``duration`` s, per piece); -1 unknown."""
    out = np.full(len(ev.seconds), -1, dtype=int)
    next_id = 0
    for piece in pd.unique(ev.pieces):
        idx = np.flatnonzero(ev.pieces == piece)
        secs = ev.seconds[idx]
        finite = np.isfinite(secs)
        if not finite.any():
            continue
        start = np.nanmin(secs)
        local = np.floor((secs - start) / duration)
        local_ids = np.where(finite, local, -1).astype(int)
        uniq = sorted({int(v) for v in local_ids if v >= 0})
        mapping = {v: next_id + k for k, v in enumerate(uniq)}
        next_id += len(uniq)
        out[idx] = [mapping.get(int(v), -1) for v in local_ids]
    return out


def _windows_with_event(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    windows: np.ndarray,
    valid: np.ndarray,
    policy: EventPolicy,
) -> set[int]:
    return set(_first_event_per_window(model, ev, windows, valid, policy))


# ---------------------------------------------------------------------------
# Criteria → verdict
# ---------------------------------------------------------------------------


def apply_criteria(
    criteria: ValidationCriteria | None,
    test: FoldMetrics | None,
    sensitivity: Sequence[SyntheticSensitivity],
    *,
    qualifying_test_drives: int,
    transients: Sequence[TransientControlResult] = (),
) -> tuple[Literal["passed", "failed", "insufficient_evidence"], list[str]]:
    """Every criterion checked and named; no universal R² floor anywhere."""
    if criteria is None:
        return "insufficient_evidence", ["no_declared_criteria"]
    if test is None or test.evaluated == 0:
        return "insufficient_evidence", ["no_evaluated_test_rows"]
    insufficient: list[str] = []
    failed: list[str] = []
    if qualifying_test_drives < criteria.min_validation_drives:
        insufficient.append(
            f"test_drives={qualifying_test_drives}<{criteria.min_validation_drives}"
            f" (each >= {criteria.min_observations_per_drive} evaluated rows)"
        )
    if test.eligible < criteria.min_eligible_observations:
        insufficient.append(f"eligible={test.eligible}<{criteria.min_eligible_observations}")
    if (
        test.envelope_coverage is not None
        and test.envelope_coverage < criteria.min_envelope_coverage
    ):
        insufficient.append(
            f"envelope_coverage={test.envelope_coverage:.2f}<{criteria.min_envelope_coverage}"
        )
    if test.coverage is not None and test.coverage < criteria.min_coverage:
        failed.append(f"coverage={test.coverage:.2f}<{criteria.min_coverage}")
    if criteria.kind == "predictive_surface":
        assert criteria.min_skill_vs_baseline is not None
        if test.skill_vs_baseline is None:
            insufficient.append("skill_vs_baseline=unavailable (no training baseline)")
        elif test.skill_vs_baseline < criteria.min_skill_vs_baseline:
            failed.append(
                f"skill_vs_baseline={test.skill_vs_baseline:.3f}<{criteria.min_skill_vs_baseline}"
            )
    # Physical resolution, for every family: a typical error larger than the
    # deviation the model must resolve means it cannot resolve it.
    if test.mae is not None and test.mae > criteria.meaningful_deviation:
        failed.append(f"mae={test.mae:.4g}>meaningful_deviation={criteria.meaningful_deviation}")
    if criteria.max_mae is not None and test.mae is not None and test.mae > criteria.max_mae:
        failed.append(f"mae={test.mae:.4g}>{criteria.max_mae}")
    if (
        criteria.max_abs_error_p95 is not None
        and test.abs_error_p95 is not None
        and test.abs_error_p95 > criteria.max_abs_error_p95
    ):
        failed.append(f"abs_error_p95={test.abs_error_p95:.4g}>{criteria.max_abs_error_p95}")
    width = test.warning_half_width_median
    if width is None:
        failed.append("warning_half_width=unavailable")
    elif width > criteria.meaningful_deviation:
        failed.append(
            f"warning_half_width_median={width:.4g}>meaningful_deviation="
            f"{criteria.meaningful_deviation}"
        )
    if criteria.max_control_events_per_hour is not None and test.events_per_hour is not None:
        if test.evaluated_hours < criteria.min_control_hours:
            insufficient.append(
                f"control_hours={test.evaluated_hours:.2f}<{criteria.min_control_hours} "
                f"({test.events} event(s) is not a rate)"
            )
        elif test.events_per_hour > criteria.max_control_events_per_hour:
            failed.append(
                f"control_events_per_hour={test.events_per_hour:.3g}>"
                f"{criteria.max_control_events_per_hour}"
            )
    for case in sensitivity:
        label = f"synthetic_{case.direction}_{case.magnitude:g}_{case.duration_seconds:g}s"
        if case.detection_fraction is None:
            insufficient.append(f"{label}=no_clean_windows ({case.windows} scored)")
        elif case.detection_fraction < criteria.min_synthetic_detection:
            failed.append(
                f"{label}_detection={case.detection_fraction:.2f}<{criteria.min_synthetic_detection}"
            )
    resolution = criteria.resolution
    if resolution is not None:
        for case in sensitivity:
            if (
                case.magnitude < resolution.sustained_change
                or case.duration_seconds < resolution.min_duration_seconds
                or case.detection_delay_seconds_median is None
            ):
                continue
            label = f"synthetic_{case.direction}_{case.magnitude:g}_{case.duration_seconds:g}s"
            if case.detection_delay_seconds_median > resolution.max_detection_delay_seconds:
                failed.append(
                    f"{label}_delay_median={case.detection_delay_seconds_median:.3g}s>"
                    f"{resolution.max_detection_delay_seconds}s"
                )
        if not transients:
            insufficient.append("transient_controls=not_evaluated")
        for result in transients:
            label = (
                f"transient_{result.direction}_{result.magnitude:g}_{result.duration_seconds:g}s"
            )
            if result.detection_fraction is None:
                insufficient.append(f"{label}=no_clean_windows ({result.windows} scored)")
            elif result.detection_fraction > resolution.max_transient_detection:
                failed.append(
                    f"{label}_detection={result.detection_fraction:.2f}>"
                    f"{resolution.max_transient_detection}"
                )
    if failed:
        return "failed", failed + [f"insufficient:{item}" for item in insufficient]
    if insufficient:
        return "insufficient_evidence", insufficient
    return "passed", []


# ---------------------------------------------------------------------------
# Evidence assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationContext:
    """What a publication route knows beyond the frame (B02.4/B02.7)."""

    policy: EventPolicy = field(default_factory=EventPolicy)
    decoder_identity: Mapping[str, str] = field(default_factory=dict)
    mode_taxonomy: tuple[str, ...] = ()
    criteria_source: str | None = None
    join_gap_seconds: float = DEFAULT_JOIN_GAP_SECONDS
    provenance: Mapping[str, Sequence[str]] = field(default_factory=dict)
    #: Differences between the config's operating modes and the approved
    #: taxonomy (:meth:`BehaviorValidationDocument.taxonomy_problems`). Any
    #: entry keeps the evidence from passing: it was not gathered under the
    #: operator-approved segmentation.
    taxonomy_problems: tuple[str, ...] = ()

    @classmethod
    def for_run(
        cls,
        config: CanaryConfig,
        document: BehaviorValidationDocument | None,
        *,
        decoder: Mapping[str, str],
        criteria_source: str | None,
        provenance: Mapping[str, Sequence[str]] | None = None,
    ) -> ValidationContext:
        modes = tuple(config.preprocessing.operating_modes)
        return cls(
            policy=EventPolicy.from_config(config, document),
            decoder_identity=dict(decoder),
            mode_taxonomy=modes,
            criteria_source=criteria_source,
            join_gap_seconds=(
                document.drive_join_gap_seconds
                if document is not None
                else DEFAULT_JOIN_GAP_SECONDS
            ),
            provenance=dict(provenance or {}),
            taxonomy_problems=tuple(document.taxonomy_problems(modes)) if document else (),
        )


def qualifying_drive_count(
    ev: RowEvaluation, drive_of_piece: Mapping[str, str], minimum: int
) -> int:
    evaluated = ev.evaluated
    drives = [drive_of_piece.get(str(p), str(p)) for p in ev.pieces[evaluated]]
    return sum(1 for count in Counter(drives).values() if count >= minimum)


def build_evidence(
    model: BehaviorModel1D | BehaviorModel2D,
    *,
    split: DriveSplit,
    test_frame: pd.DataFrame,
    calibration_frame: pd.DataFrame | None,
    criteria: ValidationCriteria | None,
    baseline: Mapping[str, float],
    context: ValidationContext,
    tune_revision: str | None,
    spec: ServingSpec | None = None,
) -> tuple[ValidationEvidence, RowEvaluation]:
    """Score the untouched test drives and turn the numbers into a verdict.

    Refuses (:class:`UnverifiedProfileError`) a test or calibration frame
    holding capture/tuning pieces without a profile verdict: evidence is
    counted on default-profile rows only.
    """
    profile_refusals(test_frame)
    if calibration_frame is not None:
        profile_refusals(calibration_frame)
    policy = context.policy
    drive_of_piece = {
        piece: drive.drive_id
        for drive in (*split.train, *split.calibration, *split.test)
        for piece in drive.pieces
    }
    test_ev = evaluate_rows(model, test_frame, spec=spec, interval_seconds=policy.interval_seconds)
    test = fold_metrics(
        model, test_ev, drive_of_piece=drive_of_piece, policy=policy, baseline=baseline
    )
    calibration = None
    if calibration_frame is not None and not calibration_frame.empty:
        cal_ev = evaluate_rows(
            model, calibration_frame, spec=spec, interval_seconds=policy.interval_seconds
        )
        calibration = fold_metrics(
            model, cal_ev, drive_of_piece=drive_of_piece, policy=policy, baseline=baseline
        )
    sensitivity = (
        synthetic_sensitivity(model, test_frame, test_ev, criteria, policy=policy, spec=spec)
        if criteria is not None and test_ev.evaluated.any()
        else []
    )
    transients = (
        transient_controls(model, test_frame, test_ev, criteria, policy=policy, spec=spec)
        if criteria is not None and test_ev.evaluated.any()
        else []
    )
    minimum = criteria.min_observations_per_drive if criteria is not None else 1
    verdict, failures = apply_criteria(
        criteria,
        test,
        sensitivity,
        qualifying_test_drives=qualifying_drive_count(test_ev, drive_of_piece, minimum),
        transients=transients,
    )
    if context.taxonomy_problems:
        failures = [*failures, *(f"mode_taxonomy:{p}" for p in context.taxonomy_problems)]
        if verdict == "passed":
            verdict = "insufficient_evidence"
    evidence = ValidationEvidence(
        evaluator_version=EVALUATOR_VERSION,
        evaluated_at=datetime.now(UTC).isoformat(),
        verdict=verdict,
        failures=failures,
        criteria=criteria,
        criteria_source=context.criteria_source,
        contract_fingerprint=contract_fingerprint(model),
        decoder_identity=dict(context.decoder_identity),
        mode_taxonomy=list(context.mode_taxonomy),
        tune_revision=tune_revision,
        split=split.record(),
        baseline=dict(baseline),
        calibration=calibration,
        test=test,
        per_drive=breakdown(
            model,
            test_ev,
            by="drive",
            drive_of_piece=drive_of_piece,
            policy=policy,
            baseline=baseline,
        ),
        per_regime=breakdown(
            model,
            test_ev,
            by="regime",
            drive_of_piece=drive_of_piece,
            policy=policy,
            baseline=baseline,
        ),
        sensitivity=sensitivity,
        transient_controls=transients,
        provenance={key: list(values) for key, values in context.provenance.items()},
    )
    return evidence, test_ev


# ---------------------------------------------------------------------------
# Carry-over (B02.7)
# ---------------------------------------------------------------------------


def carry_over_flags(
    prior: BehaviorModel1D | BehaviorModel2D,
    *,
    recipe_contract: str | None,
    criteria: ValidationCriteria | None,
    context: ValidationContext,
) -> list[str]:
    """Why a prior kept on disk may no longer be trusted, without rewriting it.

    Compared against the evidence the prior was published with: a changed
    recipe contract, decoder catalog, mode taxonomy or declared criteria each
    means that evidence no longer describes what would be served.
    """
    flags: list[str] = []
    if recipe_contract is not None and recipe_contract != contract_fingerprint(prior):
        flags.append("contract_changed:recipe")
    evidence = prior.evidence
    if evidence is None:
        return flags
    for key, value in sorted(context.decoder_identity.items()):
        recorded = evidence.decoder_identity.get(key)
        if recorded is not None and recorded != value:
            flags.append(f"decoder_changed:{key}")
    if (
        context.mode_taxonomy
        and evidence.mode_taxonomy
        and sorted(context.mode_taxonomy) != sorted(evidence.mode_taxonomy)
    ):
        flags.append("context_changed:mode_taxonomy")
    if criteria is not None and evidence.criteria is not None and criteria != evidence.criteria:
        flags.append("criteria_changed")
    return flags


# ---------------------------------------------------------------------------
# Bounded offline frame builder (the reproducible report's input)
# ---------------------------------------------------------------------------


def evaluation_decoded_signals(models: BehaviorModels) -> set[str]:
    """Binding names the evaluation frame must decode from raw CAN: the
    segmenter's ``decoded:`` mode candidates plus any ``decoded:`` channel a
    model reads (input, output, gate or required context)."""
    from src.behavior.behavior_eligibility import required_channel_groups
    from src.features.feature_pipeline import segmenter_input_columns

    names: set[str] = set()
    columns: list[str] = list(segmenter_input_columns())
    for model in models.models:
        for group in required_channel_groups(model):
            columns.extend(group)
        columns.append(serving_spec(model).output)
    for column in columns:
        if column.startswith("decoded:"):
            names.add(column.split(":", 1)[1])
    return names


def build_evaluation_frame(
    sessions: Sequence[Path],
    config: CanaryConfig,
    models: BehaviorModels,
    *,
    bindings_path: Path,
    dids_path: Path,
    decode_can: Literal["none", "mode-inputs"] = "mode-inputs",
    profile_scope: ProfileScope = "default_profile",
) -> pd.DataFrame:
    """The training preprocessing chain, bounded to what evaluation reads.

    ``profile_scope`` (B05.3): by default every row is stamped with its
    session's profile verdict (:func:`stamp_profile_exclusions`), so tuning
    captures are refused wherever evidence is counted. ``"every_recording"``
    leaves the frame unstamped, for reproducing numbers an artifact recorded
    before that rule; evidence built on it refuses a capture piece.

    Identical decode → reject gate → resample → derive → segment → prerequisite
    chain as ``build_feature_frame`` (shared functions, not copies), except raw
    CAN frames are read only for the ids of the bindings in
    :func:`evaluation_decoded_signals` — a whole-bus decode of one 42 MB drive
    peaked above 6 GB. The bounded read is
    :func:`src.preprocessing.decode.read_session_decoded_subset`, the one
    reader that pushes the ids down under every era's channel spelling
    (R04.14; this reader used to push ``0x{id:X}`` only and silently decoded
    nothing from an older session). DBC decoding is not run. Returns the operating frame
    (deviation and prerequisite columns included), one row per resampled
    second, with ``session`` naming the piece.

    ``decode_can="none"`` skips raw CAN entirely: the segmenter's decoded
    fallback candidates did not exist when the July 2026 artifacts were fit,
    so reproducing their persisted held-out numbers needs the historical
    input scope (the idle holdout is 90 rows without it, 114 with it). That
    scope has no ``decoded:`` channel at all (P02.8): neither the loader's
    decode on read nor an old session's stored rows, so a July session reads
    as it did when those numbers were made. ``"mode-inputs"`` reads the
    session with its ``decoded:`` rows decoded on read, as every reader does,
    and decodes its own signals over them.
    """
    from src.decode.bindings import load_bindings
    from src.decode.dids import load_did_map
    from src.features.feature_pipeline import (
        drop_fully_stale_rows,
        featurize_wide_frame,
        resample_config_for,
        resample_long_session,
    )

    bindings = load_bindings(bindings_path) if bindings_path.exists() else None
    did_map = load_did_map(dids_path) if dids_path.exists() else None
    wanted = evaluation_decoded_signals(models) if decode_can == "mode-inputs" else set()
    resample_config = resample_config_for(config)
    parts = []
    for session in sessions:
        if decode_can == "none":
            long, subset = read_session_decoded_subset(
                Path(session), bindings=bindings, decoded=wanted, rows="as_recorded"
            )
            long = decode_on_read(long, None)  # drops stored decoded rows, decodes none
        else:
            long, subset = read_session_decoded_subset(
                Path(session), bindings=bindings, decoded=wanted
            )
        parts.append(
            resample_long_session(
                long, subset, None, did_map, resample_config, session_name=Path(session).name
            )
        )
    resampled = drop_fully_stale_rows(pd.concat(parts, ignore_index=True))
    operating = featurize_wide_frame(
        resampled, config, behavior_models=models, did_map=did_map, bindings=bindings
    ).operating_frame
    # Each row's physical drive, as build_feature_frame stamps it, so the
    # evaluator's drive grouping is the trainers' (src.preprocessing.drives).
    stamped = _drives.stamp_physical_drives(operating, [Path(s) for s in sessions])
    if profile_scope == "every_recording":
        return stamped
    return stamp_profile_exclusions(stamped, [Path(s) for s in sessions])


def session_content_identity(session: Path) -> dict[str, Any]:
    """Content hash of a session's telemetry chunks, for evidence manifests."""
    digest = hashlib.sha256()
    files = sorted(session.glob("telemetry-*.parquet"))
    total = 0
    for chunk in files:
        data = chunk.read_bytes()
        total += len(data)
        digest.update(chunk.name.encode())
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return {
        "name": session.name,
        "chunks": len(files),
        "bytes": total,
        "sha256": "sha256:" + digest.hexdigest(),
    }


__all__ = [
    "DEFAULT_JOIN_GAP_SECONDS",
    "EVALUATOR_VERSION",
    "PROFILE_EXCLUDED",
    "PROFILE_EXCLUSION_COLUMN",
    "PROFILE_UNVERIFIED",
    "UnverifiedProfileError",
    "BehaviorValidationDocument",
    "Drive",
    "DriveSplit",
    "EventPolicy",
    "LeakedDriveError",
    "ProvenanceResolution",
    "RowEvaluation",
    "UnorderedSessionsError",
    "ValidationContext",
    "ReportRegimes",
    "apply_criteria",
    "band_excess_event_times",
    "breakdown_by_labels",
    "build_evaluation_frame",
    "build_evidence",
    "carry_over_flags",
    "chronological_split",
    "count_events",
    "decoder_identity",
    "event_times",
    "evaluate_rows",
    "explicit_split",
    "fit_band_excess_persistence",
    "fit_baseline",
    "fold_metrics",
    "group_drives",
    "load_validation_document",
    "model_event_times",
    "parent_session",
    "PieceRanges",
    "piece_time_ranges",
    "profile_exclusions",
    "profile_refusals",
    "regime_labels",
    "session_content_identity",
    "stamp_profile_exclusions",
    "synthetic_sensitivity",
    "training_leaks",
    "transient_controls",
    "validation_document_path",
]
