"""Drive-grouped detector evaluation: one yardstick for every detector (A02.4).

Before this there was no number in the repo for how well a detector finds a
fault on a drive it has not seen. The retrain gate scored false alarms on the
sessions its thresholds were cut from, and the fault benchmark pooled
per-second counts across drives. This module is the protocol every later
detector decision (A05, A06) is judged on:

* **Physical drives, not sessions.** Sessions that overlap or follow each
  other within :data:`DRIVE_GAP_SECONDS` (a daemon restart, a capture window
  opening mid-drive) are one drive (:func:`group_into_drives`), ordered by
  parsed start time (``session_started_at``), never by directory name.
* **Eras.** A drive belongs to the tune revision and hardware-intervention
  interval it was recorded in (:class:`Era`); a drive spanning a boundary is
  skipped, and a fold never mixes eras (:class:`Fold`).
* **Held out means unseen.** :class:`HoldoutSplit` refuses a session in both
  the calibration and the evaluation side, and a physical drive split across
  them.
* **Event-level metrics at the deployed threshold.** Alert *episodes* after
  the production persistence gate (:func:`alert_episodes`), false alarms per
  engine-hour, event recall and time to detect. No point-adjust: a detection
  is an episode the fault itself caused (:func:`fault_attributable_detection`).
  :func:`vus_pr` is the only threshold-free number, and only for comparisons.
* **Uncertainty over drives.** :func:`bootstrap_ratio_ci` resamples whole
  drives, never correlated seconds.
* **Thresholds from the alert budget** (A02.3): :func:`alert_budget_threshold`
  picks the lowest threshold whose alert episodes on the given drives stay
  within ``V1_ALERTS_PER_HOUR_BUDGET`` per engine-hour, from per-block maxima
  (independent windows), and says when the exposure cannot resolve the budget.

Every empty result says which kind of empty it is: *withheld* (the detector
had no verdict), *skipped* (the drive or window could not carry the test),
*scored and quiet*, or *flagged*.

The heavy parts — loading sessions, the detectors themselves — live in
:mod:`src.models.evaluation_detectors` and ``scripts/evaluate_detectors.py``;
this module is pure (numpy/pandas) so its rules are unit-testable.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.alerts.fusion import PersistenceGate
from src.config import CanaryConfig
from src.decode.engineering import UnscaledDecodedChannelError, physical_series
from src.models.false_positive_benchmark import V1_ALERTS_PER_HOUR_BUDGET
from src.models.sequence_calibration import block_maxima as max_finite_per_block
from src.models.sequence_registry import MIN_CALIBRATION_SESSIONS
from src.operating import ENGINE_RUNNING_MIN_RPM
from src.preprocessing.drives import (
    DRIVE_GAP_SECONDS,
    SessionSpan,
    drive_id_for,
    join_spans,
    physical_drives,
    recording_units,
)
from src.preprocessing.segmenter import DEFAULT_SIGNAL_COLUMNS
from src.session import SessionKind, session_kind, session_sort_key
from src.storage.session_files import PARQUET_PATTERN
from src.storage.sessions import session_started_at
from src.vehicle.tune_revisions import TuneRevisionRegistry

log = logging.getLogger(__name__)

#: Longest step between rows that still counts as continuous engine time when
#: integrating engine-hours (the 1 Hz grid with a few dropped ticks).
MAX_ENGINE_STEP_SECONDS = 5.0

BARO_CHANNEL = "obd:BAROMETRIC_PRESSURE"
#: J1979 PID 0x46. Intake air temperature is *not* a stand-in: on this project
#: it tracks airflow (stagnation), not the weather.
AMBIENT_CHANNEL = "obd:AMBIENT_AIR_TEMP"

#: Reporting strata. Bands, not vehicle facts: home altitude differs per
#: vehicle, but a kPa is a kPa.
BARO_BANDS_KPA: tuple[tuple[float | None, float | None, str], ...] = (
    (None, 70.0, "<70 kPa"),
    (70.0, 77.0, "70-77 kPa"),
    (77.0, None, ">=77 kPa"),
)
AMBIENT_BANDS_C: tuple[tuple[float | None, float | None, str], ...] = (
    (None, 10.0, "<10 degC"),
    (10.0, 25.0, "10-25 degC"),
    (25.0, None, ">=25 degC"),
)
UNMEASURED = "unmeasured"


# ---------------------------------------------------------------------------
# Sessions, drives, eras
# ---------------------------------------------------------------------------


def session_span(path: Path) -> SessionSpan | None:
    """The span of a recorded session, from parquet row-group statistics.

    Reads only footer metadata (min/max of ``timestamp``), so it is cheap on
    a session of thousands of chunk files. ``None`` when no chunk carries a
    timestamp — an empty session has no span, and grouping it by its
    directory name would be guessing.
    """
    import pyarrow.parquet as pq

    lows: list[pd.Timestamp] = []
    highs: list[pd.Timestamp] = []
    for chunk in path.glob(PARQUET_PATTERN):
        try:
            parquet = pq.ParquetFile(chunk)  # type: ignore[no-untyped-call, unused-ignore]
        except Exception:  # noqa: BLE001 - a torn chunk is skipped, like the loader does
            continue
        try:
            column = parquet.schema_arrow.get_field_index("timestamp")
        except KeyError:
            continue
        if column < 0:
            continue
        metadata = parquet.metadata
        for group in range(metadata.num_row_groups):
            stats = metadata.row_group(group).column(column).statistics
            if stats is None or not stats.has_min_max:
                continue
            lows.append(pd.Timestamp(stats.min))
            highs.append(pd.Timestamp(stats.max))
    if not lows:
        return None
    start = min(lows)
    end = max(highs)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    return SessionSpan(path.name, start.to_pydatetime(), end.to_pydatetime(), path)


@dataclass(frozen=True)
class Era:
    """The calibration and hardware state a drive was recorded in."""

    tune_revision: str | None
    #: How many hardware-intervention boundaries precede the drive.
    boundary_index: int

    @property
    def label(self) -> str:
        return f"{self.tune_revision or 'unregistered'}/hw{self.boundary_index}"


EraResolver = Callable[[datetime], Era]


def era_resolver(
    tune_revisions: TuneRevisionRegistry | None,
    boundaries: Sequence[datetime] = (),
) -> EraResolver:
    """Map a moment to its :class:`Era`.

    ``boundaries`` are hardware-intervention times (labelled events with
    ``corpus_boundary``, vehicle-ledger boundaries); flashes come from the
    tune registry itself.
    """
    ordered = sorted(_utc(moment) for moment in boundaries)

    def resolve(moment: datetime) -> Era:
        when = _utc(moment)
        revision = None if tune_revisions is None else tune_revisions.at(when)
        return Era(
            tune_revision=None if revision is None else revision.revision,
            boundary_index=sum(1 for boundary in ordered if boundary <= when),
        )

    return resolve


@dataclass(frozen=True)
class Drive:
    """One physical drive: sessions that follow each other without a key-off."""

    drive_id: str
    sessions: tuple[SessionSpan, ...]
    start: datetime
    end: datetime
    #: ``None`` when the drive straddles an era boundary (see ``straddles``).
    era: Era | None
    straddles: str | None = None

    @property
    def session_names(self) -> tuple[str, ...]:
        return tuple(span.name for span in self.sessions)


def group_into_drives(
    spans: Sequence[SessionSpan],
    *,
    era_of: EraResolver | None = None,
    max_gap_seconds: float = DRIVE_GAP_SECONDS,
) -> list[Drive]:
    """Group session spans into physical drives, oldest first.

    Ordered by each span's parsed start, so a ``_capture-…`` prefix can never
    put a later session first. A session that overlaps the running drive, or
    starts within ``max_gap_seconds`` of its end, joins it.
    """
    return [
        _drive(tuple(group), era_of=era_of)
        for group in join_spans(spans, gap_seconds=max_gap_seconds)
    ]


def _drive(
    members: tuple[SessionSpan, ...],
    *,
    era_of: EraResolver | None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Drive:
    start = _utc(start) if start is not None else min(_utc(span.start) for span in members)
    end = _utc(end) if end is not None else max(_utc(span.end) for span in members)
    era: Era | None = None
    straddles: str | None = None
    if era_of is not None:
        first, last = era_of(start), era_of(end)
        if first == last:
            era = first
        else:
            straddles = f"spans era {first.label} -> {last.label}"
    return Drive(
        drive_id=drive_id_for(start),
        sessions=members,
        start=start,
        end=end,
        era=era,
        straddles=straddles,
    )


def span_from_name(name: str, *, duration_seconds: float = 0.0) -> SessionSpan | None:
    """A span from a session's name alone (the name's UTC start), for callers
    with no telemetry to read. ``None`` for a name with no timestamp."""
    started = session_started_at(name)
    if started is None:
        return None
    return SessionSpan(name, started, started + timedelta(seconds=duration_seconds))


# ---------------------------------------------------------------------------
# Folds and holdout splits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldoutSplit:
    """Disjoint calibration and evaluation sessions (A02.1).

    Thresholds are cut from ``calibration``; the regression gate and every
    held-out number score ``evaluation`` only. A session on both sides is
    refused: the gate would grade thresholds on the very sessions they were
    cut from, which is how the sequence detector's 75 % flood read as a
    ~0.1 % clean-alert rate.
    """

    calibration: tuple[str, ...]
    evaluation: tuple[str, ...]

    def __post_init__(self) -> None:
        both = sorted(set(self.calibration) & set(self.evaluation))
        if both:
            raise ValueError(
                f"session(s) {both} are in both the calibration and the evaluation holdout; "
                "a gate that scores thresholds on the sessions they were cut from measures "
                "nothing (A02.1)"
            )

    def check_drives(self, drives: Sequence[Drive]) -> None:
        """Refuse a physical drive whose sessions sit on both sides."""
        calibration, evaluation = set(self.calibration), set(self.evaluation)
        for drive in drives:
            names = set(drive.session_names)
            if names & calibration and names & evaluation:
                raise ValueError(
                    f"{drive.drive_id} is split across the calibration "
                    f"({sorted(names & calibration)}) and evaluation "
                    f"({sorted(names & evaluation)}) holdouts; one physical drive is one "
                    "condition and must stay on one side"
                )

    def to_dict(self) -> dict[str, list[str]]:
        return {"calibration": list(self.calibration), "evaluation": list(self.evaluation)}


def drives_of_sessions(
    sessions: Sequence[Path], *, era_of: EraResolver | None = None
) -> list[Drive]:
    """Physical drives among session directories, as units a holdout can take
    whole (:func:`src.preprocessing.drives.physical_drives`, oldest first).

    Drives are vehicle activity joined across recordings, with every sibling
    recording (captures included) as evidence, and split where the activity
    inside one directory pauses. A directory with activity in two drives
    cannot be split across a holdout, so it links them into one unit here.
    A directory with no vehicle activity at all is its own unit, spanning its
    name's start. Each member span covers that directory's activity in the
    unit; the unit's start and end cover the drives, siblings included.
    """
    paths = [Path(s) for s in sessions]
    by_name = {p.name: p for p in paths}
    drives = physical_drives(paths)
    extent: dict[str, tuple[datetime, datetime]] = {}
    unit_extent: dict[str, tuple[datetime, datetime]] = {}
    for drive in drives:
        for span in drive.spans:
            if span.name in by_name:
                lo, hi = extent.get(span.name, (_utc(span.start), _utc(span.end)))
                extent[span.name] = (min(lo, _utc(span.start)), max(hi, _utc(span.end)))
        for name in drive.sessions:
            lo, hi = unit_extent.get(name, (drive.start, drive.end))
            unit_extent[name] = (min(lo, drive.start), max(hi, drive.end))
    units = recording_units(drives)
    placed = {name for unit in units for name in unit}
    out: list[Drive] = []
    for unit in units:
        members = tuple(
            SessionSpan(name, *extent[name], by_name[name])
            for name in sorted(unit, key=lambda n: (extent[n][0], n))
        )
        out.append(
            _drive(
                members,
                era_of=era_of,
                start=min(unit_extent[n][0] for n in unit),
                end=max(unit_extent[n][1] for n in unit),
            )
        )
    for path in paths:
        if path.name in placed:
            continue
        named = span_from_name(path.name)
        if named is not None:
            out.append(
                _drive((SessionSpan(named.name, named.start, named.end, path),), era_of=era_of)
            )
    return sorted(out, key=lambda d: (d.start, d.drive_id))


@dataclass(frozen=True)
class DriveHoldout:
    """Training, calibration and evaluation sessions, split by physical drive."""

    training: tuple[Path, ...]
    calibration: tuple[Path, ...]
    evaluation: tuple[Path, ...]
    split: HoldoutSplit


def holdout_by_drive(
    pool: Sequence[Path],
    *,
    calibration: Sequence[Path] = (),
    evaluation: Sequence[Path] = (),
) -> DriveHoldout:
    """The one place a caller holds sessions out (A02.1).

    Every session recorded in the same physical drive as a named calibration
    or evaluation session joins that side — a daemon restart or a capture
    window leaves one drive in several sessions, and a held-out number on
    one while its sibling trains measures nothing. A drive that would then sit
    on both sides is refused (:class:`HoldoutSplit`). Training is ``pool``
    without either side. ``retrain.py``, ``train_sequence.py`` and
    ``sequence_bakeoff.py`` all split through here, so none of them can skip
    the expansion.
    """
    everything: dict[Path, Path] = {}
    for session in (*pool, *calibration, *evaluation):
        everything.setdefault(Path(session).resolve(), Path(session))
    drives = drives_of_sessions(list(everything.values()))
    members = {
        Path(span.path).resolve(): [Path(m.path) for m in drive.sessions if m.path is not None]
        for drive in drives
        for span in drive.sessions
        if span.path is not None
    }

    def expand(named: Sequence[Path]) -> tuple[Path, ...]:
        out: dict[Path, Path] = {}
        for session in named:
            for member in members.get(Path(session).resolve(), [Path(session)]):
                out.setdefault(member.resolve(), member)
        return tuple(
            sorted(
                out.values(),
                key=session_sort_key,
            )
        )

    held_calibration = expand(calibration)
    held_evaluation = expand(evaluation)
    split = HoldoutSplit(
        calibration=tuple(p.name for p in held_calibration),
        evaluation=tuple(p.name for p in held_evaluation),
    )
    split.check_drives(drives)
    held = {p.resolve() for p in (*held_calibration, *held_evaluation)}
    training = tuple(p for p in pool if Path(p).resolve() not in held)
    return DriveHoldout(training, held_calibration, held_evaluation, split)


def nothing_left_to_train(holdout: DriveHoldout) -> str:
    """Why a holdout left no training session (A03.6), naming both sides.

    Held-out sessions take their whole physical drive with them, so naming one
    session of a drive can empty an explicit list that looked disjoint. The
    wording names the sequence CLIs' flags (``train_sequence.py``,
    ``sequence_bakeoff.py``), its two callers.
    """
    calibration = ", ".join(p.name for p in holdout.calibration) or "none"
    evaluation = ", ".join(p.name for p in holdout.evaluation) or "none"
    return (
        "nothing left to train on: every session is held out (with its whole "
        f"physical drive) by --calibration-sessions [{calibration}] or --evaluate "
        f"[{evaluation}]. Training on held-out drives would make their numbers "
        "in-sample; pass more sessions or hold out fewer."
    )


#: Both detectors calibrate on the same pooled holdout set; the floor is the
#: sequence registry's (one definition: the manifest stamps a shortfall).
MIN_CALIBRATION_HOLDOUT_SESSIONS = MIN_CALIBRATION_SESSIONS

#: Held-out drives the post-retrain gate scores (A02.1), disjoint from the
#: calibration drives. Same count as the calibration floor: one drive is one
#: day's conditions, for grading as much as for calibrating.
EVALUATION_HOLDOUT_DRIVES = MIN_CALIBRATION_HOLDOUT_SESSIONS

#: Minimum total parquet bytes for a session to be considered "substantial"
#: enough to anchor an out-of-sample calibration holdout. A single in-progress
#: capture (1 truncated chunk a few KB on disk) doesn't have the rows to form
#: useful per-mode bands, so auto-holdout skips it and picks an earlier real
#: drive instead. ~1 MB is roughly a minute of telemetry at our chunk rate;
#: way below any actual drive (the 05-18 short idles are still ~1.5 MB).
MIN_CALIBRATION_HOLDOUT_BYTES = 1_000_000

#: A mode must run at least this long in a session (per the classifier
#: sidecar) before the session counts as "covering" that mode for holdout
#: selection: a 5 s highway blip can't calibrate a highway band.
MIN_HOLDOUT_MODE_SECONDS = 60.0


def session_parquet_bytes(session_dir: Path) -> int:
    """Total bytes of a session's telemetry chunks (0 when unreadable)."""
    try:
        return sum(p.stat().st_size for p in session_dir.glob(PARQUET_PATTERN))
    except OSError:
        return 0


def session_mode_coverage(session: Path) -> set[str] | None:
    """Modes a session meaningfully covers, from its classifier sidecar.

    ``None`` when the session hasn't been classified (run
    ``scripts/label_sessions.py``); callers treat that as unknown coverage.
    """
    from src.storage.session_products import read_classification

    classification = read_classification(session)
    if classification is None:
        return None
    return {
        mode
        for mode, seconds in classification.mode_seconds.items()
        if seconds >= MIN_HOLDOUT_MODE_SECONDS and mode != "unknown"
    }


def session_holdout_health(session: Path) -> tuple[str, int] | None:
    """Classifier disposition and observed feature width for holdout screening."""
    from src.storage.session_products import read_classification

    classification = read_classification(session)
    if classification is None:
        return None
    return (
        classification.disposition,
        int(classification.quality.get("feature_columns", 0)),
    )


def _started_at_key(session: Path) -> datetime:
    """Sort key by *parsed* start time (never by directory name: capture
    prefixes sort first). Unparseable names keep their given order, oldest:
    the time half of :func:`src.session.session_sort_key` (R01.1)."""
    return session_sort_key(session)[0]


def pick_calibration_holdouts(
    sessions: Sequence[Path],
    *,
    max_holdouts: int = MIN_CALIBRATION_HOLDOUT_SESSIONS,
) -> list[Path]:
    """Healthy substantial sessions: mode coverage first, then spread in time.

    The historical single auto-holdout had a blind spot: if the freshest
    drive was an idle session, highway/city earned no per-mode band AND the
    global fallback threshold was calibrated from an idle-dominated error
    distribution, too tight or too loose for the first real drive.

    Pass 1 walks newest to oldest: the freshest substantial session anchors
    the holdout (the legacy rule, so unclassified corpora behave exactly as
    before); further sessions are added when their classifier sidecar covers a
    mode not yet held out, until every mode seen in the classified corpus is
    covered or ``max_holdouts`` is reached.

    Pass 2 tops the set up to ``max_holdouts``. Pass 1 alone stops at ONE
    session whenever the freshest drive happens to touch every mode, and a
    single drive is a single day's conditions. The extra sessions are the
    healthy ones *farthest in time* from those already held out, so the
    calibration set spans the corpus rather than clustering on its last day.
    ``scripts/retrain.py`` hands the same list to every detector.

    Returns whole drives, in pick order: a picked session's restart/capture
    siblings are held out with it (callers still split through
    :func:`holdout_by_drive`).
    """
    ordered = sorted(sessions, key=_started_at_key)
    corpus_modes: set[str] = set()
    classified_widths = [
        health[1]
        for session in ordered
        if (health := session_holdout_health(session)) is not None
        and health[0] == "ready"
        and health[1] > 0
    ]
    minimum_compatible_width = 0.9 * max(classified_widths, default=0)
    for session in ordered:
        modes = session_mode_coverage(session)
        if modes:
            corpus_modes |= modes

    healthy: list[Path] = []
    for session in reversed(ordered):
        if session_parquet_bytes(session) < MIN_CALIBRATION_HOLDOUT_BYTES:
            continue
        health = session_holdout_health(session)
        if health is not None and (health[0] != "ready" or health[1] < minimum_compatible_width):
            log.info(
                "skipping unhealthy calibration holdout",
                extra={
                    "session": session.name,
                    "disposition": health[0],
                    "feature_columns": health[1],
                    "minimum_compatible_feature_columns": minimum_compatible_width,
                },
            )
            continue
        healthy.append(session)

    # One pick per physical drive (A02.1/A02.2): three sessions of one day's
    # drive are one drive's conditions, and the sequence trainer refuses a
    # calibration resting on fewer than MIN_CALIBRATION_SESSIONS drives.
    drives = drives_of_sessions(ordered)
    drive_of = {name: drive.drive_id for drive in drives for name in drive.session_names}
    drive_members = {
        drive.drive_id: [span.path for span in drive.sessions if span.path is not None]
        for drive in drives
    }

    def new_drive(candidate: Path, chosen: Sequence[Path]) -> bool:
        taken = {drive_of.get(held.name, held.name) for held in chosen}
        return drive_of.get(candidate.name, candidate.name) not in taken

    selected: list[Path] = []
    covered: set[str] = set()
    for session in healthy:
        if len(selected) >= max_holdouts:
            break
        modes = session_mode_coverage(session)
        if not selected:
            selected.append(session)
            covered |= modes or set()
        elif modes and modes - covered and new_drive(session, selected):
            selected.append(session)
            covered |= modes
        if corpus_modes and covered >= corpus_modes:
            break

    while selected and len(selected) < max_holdouts:
        remaining = [
            session
            for session in healthy
            if session not in selected and new_drive(session, selected)
        ]
        if not remaining:
            break
        selected.append(
            max(
                remaining,
                key=lambda candidate: min(
                    abs((_started_at_key(candidate) - _started_at_key(held)).total_seconds())
                    for held in selected
                ),
            )
        )
    whole: list[Path] = []
    for session in selected:
        for member in drive_members.get(drive_of.get(session.name, ""), [session]):
            if member not in whole:
                whole.append(member)
    return whole


def pick_evaluation_holdouts(
    sessions: Sequence[Path],
    *,
    max_sessions: int,
    max_drives: int = EVALUATION_HOLDOUT_DRIVES,
) -> list[Path]:
    """The newest healthy, substantial drives, for the regression gate only.

    Drawn from ``sessions`` after calibration has taken its drives, so the gate
    never scores a session the thresholds were cut from (A02.1). Whole drives
    or nothing, newest first, within ``max_sessions`` so training keeps its
    floor.
    """
    selected: list[Path] = []
    drives = 0
    for drive in reversed(drives_of_sessions(sessions)):
        if drives >= max_drives:
            break
        members = [span.path for span in drive.sessions if span.path is not None]
        if len(selected) + len(members) > max_sessions:
            continue
        healthy = [
            m
            for m in members
            if session_parquet_bytes(m) >= MIN_CALIBRATION_HOLDOUT_BYTES
            and ((health := session_holdout_health(m)) is None or health[0] == "ready")
        ]
        if not healthy:
            continue
        selected.extend(members)
        drives += 1
    return sorted(selected, key=_started_at_key)


@dataclass(frozen=True)
class Fold:
    """One grouped fold: calibrate on some drives, evaluate on others, one era."""

    era: Era
    calibration: tuple[str, ...]
    evaluation: tuple[str, ...]

    def __post_init__(self) -> None:
        both = sorted(set(self.calibration) & set(self.evaluation))
        if both:
            raise ValueError(
                f"fold puts drive(s) {both} on both the calibration and evaluation side"
            )
        if not self.evaluation:
            raise ValueError("a fold must evaluate at least one drive")

    def check_eras(self, drives: Mapping[str, Drive]) -> None:
        """Refuse a fold whose drives come from another era than its own."""
        for drive_id in (*self.calibration, *self.evaluation):
            drive = drives[drive_id]
            if drive.era != self.era:
                raise ValueError(
                    f"fold for era {self.era.label} includes {drive_id} from "
                    f"{drive.era.label if drive.era else 'a boundary-straddling drive'}; "
                    "a fold never mixes tune or hardware eras"
                )


def grouped_folds(drives: Sequence[Drive]) -> list[Fold]:
    """Leave-one-drive-out folds within each era, in start order.

    Drives that straddle a boundary join no fold. An era with a single drive
    yields no fold — there is nothing to calibrate on.
    """
    by_era: dict[Era, list[Drive]] = {}
    for drive in sorted(drives, key=lambda d: d.start):
        if drive.era is not None:
            by_era.setdefault(drive.era, []).append(drive)
    folds: list[Fold] = []
    for era, members in by_era.items():
        if len(members) < 2:
            continue
        for held in members:
            folds.append(
                Fold(
                    era=era,
                    calibration=tuple(d.drive_id for d in members if d is not held),
                    evaluation=(held.drive_id,),
                )
            )
    return folds


# ---------------------------------------------------------------------------
# Alerts, engine time, detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersistenceSpec:
    """The production M-of-N persistence gate (``alerts.*``)."""

    required: int
    window: int | None
    window_seconds: float | None

    @classmethod
    def from_config(cls, config: CanaryConfig) -> PersistenceSpec:
        return cls(
            config.alerts.min_consecutive_ticks,
            config.alerts.persistence_window_ticks,
            config.alerts.persistence_window_seconds,
        )

    def gate(self) -> PersistenceGate:
        return PersistenceGate(
            self.required, window=self.window, window_seconds=self.window_seconds
        )


def _verdict(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if value is pd.NA:
        return None
    return bool(value)


def sustained_alerts(
    timestamps: Sequence[pd.Timestamp] | pd.Series,
    verdicts: Sequence[Any],
    persistence: PersistenceSpec,
    *,
    pre_gated: bool = False,
) -> np.ndarray[Any, np.dtype[np.bool_]]:
    """Per row: would this tick alert after the persistence gate?

    ``verdicts`` are ``True``/``False``/``None`` (``None``: no verdict — the
    gate's window is left untouched, as in the daemon). ``pre_gated`` passes
    verdicts that were already gated upstream (the fused stream).
    """
    stamps = pd.to_datetime(pd.Series(list(timestamps)), utc=True)
    out = np.zeros(len(stamps), dtype=bool)
    gate = persistence.gate()
    for index, (stamp, raw) in enumerate(zip(stamps, verdicts, strict=True)):
        firing = _verdict(raw)
        if pre_gated:
            out[index] = firing is True
            continue
        sustained = gate.observe("detector", firing, now=stamp.timestamp())
        out[index] = firing is True and sustained
    return out


@dataclass(frozen=True)
class Episode:
    """One alert episode: a maximal run of alerting ticks."""

    start: pd.Timestamp
    end: pd.Timestamp


def alert_episodes(
    timestamps: Sequence[pd.Timestamp] | pd.Series,
    alerting: Sequence[bool] | np.ndarray[Any, Any],
    *,
    verdicts: Sequence[Any] | None = None,
    max_step_seconds: float = MAX_ENGINE_STEP_SECONDS,
) -> list[Episode]:
    """Maximal runs of alerting ticks.

    A run ends at a quiet scored tick or a time gap longer than
    ``max_step_seconds``. A tick with no verdict (``verdicts[i] is None``)
    neither ends nor extends a run — the detector did not look.
    """
    stamps = pd.to_datetime(pd.Series(list(timestamps)), utc=True)
    episodes: list[Episode] = []
    open_start: pd.Timestamp | None = None
    last_alert: pd.Timestamp | None = None
    for index, stamp in enumerate(stamps):
        alert = bool(alerting[index])
        no_verdict = verdicts is not None and _verdict(verdicts[index]) is None
        if last_alert is not None and (stamp - last_alert).total_seconds() > max_step_seconds:
            if open_start is not None:
                episodes.append(Episode(open_start, last_alert))
            open_start = None
            last_alert = None
        if alert:
            if open_start is None:
                open_start = stamp
            last_alert = stamp
        elif not no_verdict and open_start is not None and last_alert is not None:
            episodes.append(Episode(open_start, last_alert))
            open_start = None
            last_alert = None
    if open_start is not None and last_alert is not None:
        episodes.append(Episode(open_start, last_alert))
    return episodes


def default_profile_exclusion(session_dir: Path) -> str | None:
    """Why a session's rows are kept out of detector evaluation, or ``None``.

    A ``_tuning-*`` session, or a capture whose manifest says it is not
    corpus-eligible, is excluded: it still bridges its drive (drive grouping
    keeps a drive's default parts together, so held-out stays honest), but its
    rows reach no scored row, fault injection or metric. A capture without a
    readable manifest is treated as the replay guard treats it (R03.12):
    eligible.
    """
    from src.storage.capture_manifest import read_capture_manifest

    name = Path(session_dir).name
    # Operator decision 2026-09-24: tuning logs do not represent the default
    # profile, so the detectors are neither trained, calibrated, scored live
    # (R03.12) nor evaluated on them. The name rule is src.session's.
    if session_kind(name) is SessionKind.TUNING:
        return "tuning session: not the default profile (operator decision 2026-09-24)"
    manifest = read_capture_manifest(Path(session_dir))
    if manifest is not None and not manifest.corpus_eligible:
        return (
            f"capture profile {manifest.profile!r} is not corpus-eligible "
            "(operator decision 2026-09-24)"
        )
    return None


def engine_running(frame: pd.DataFrame) -> np.ndarray[Any, np.dtype[np.bool_]]:
    """Rows with the engine running (RPM at or above the running floor).

    RPM is the segmenter's own candidate list (``obd:RPM`` first, then the
    broadcast channel in engineering units), coalesced row-wise — so a
    capture whose profile disables Mode 01 still has engine time. Rows with
    no RPM reading at all are not engine time: an engine-hour denominator
    must not count seconds nobody measured.
    """
    rpm: np.ndarray[Any, np.dtype[np.float64]] | None = None
    for column in DEFAULT_SIGNAL_COLUMNS["rpm"]:
        if column not in frame.columns:
            continue
        try:
            values = physical_series(frame[column], column).to_numpy(dtype="float64")
        except UnscaledDecodedChannelError:
            continue
        rpm = values if rpm is None else np.where(np.isfinite(rpm), rpm, values)
    if rpm is None:
        return np.zeros(len(frame), dtype=bool)
    with np.errstate(invalid="ignore"):
        return np.asarray(np.nan_to_num(rpm, nan=0.0) >= ENGINE_RUNNING_MIN_RPM)


def engine_hours(
    timestamps: Sequence[pd.Timestamp] | pd.Series,
    running: Sequence[bool] | np.ndarray[Any, Any],
    *,
    max_step_seconds: float = MAX_ENGINE_STEP_SECONDS,
) -> float:
    """Engine-running time in hours: each running row owns the step to the
    next row, capped at ``max_step_seconds`` (a gap is not engine time)."""
    stamps = pd.to_datetime(pd.Series(list(timestamps)), utc=True)
    if len(stamps) < 2:
        return 0.0
    steps = stamps.diff().dt.total_seconds().to_numpy()[1:]
    steps = np.clip(np.nan_to_num(steps, nan=0.0), 0.0, max_step_seconds)
    owned = np.asarray(running, dtype=bool)[:-1]
    return float(steps[owned].sum() / 3600.0)


DetectionStatus = Literal["detected", "missed", "withheld", "masked"]


@dataclass(frozen=True)
class DetectionOutcome:
    status: DetectionStatus
    time_to_detect_seconds: float | None = None


def fault_attributable_detection(
    timestamps: Sequence[pd.Timestamp] | pd.Series,
    *,
    faulted_alerting: Sequence[bool] | np.ndarray[Any, Any],
    clean_alerting: Sequence[bool] | np.ndarray[Any, Any],
    faulted_verdicts: Sequence[Any],
    start: pd.Timestamp,
    end: pd.Timestamp,
    grace_seconds: float,
) -> DetectionOutcome:
    """Did the fault cause an alert, on the same rows?

    Graded only where the detector was quiet: if it alerts anywhere in
    ``[start, end + grace)`` on the identical *clean* rows, the injection is
    ``masked`` — a flooding detector cannot show a detection there, and one
    alerting near the window would otherwise be credited for its own jitter
    (the first run on real drives gave the same recall at every severity).
    Otherwise it is detected when the faulted rows alert in that span: no
    point-adjust. ``withheld`` when the detector had no verdict on any tick
    of the window: that is not a miss either. Callers report masked and
    withheld counts beside recall, never inside it.
    """
    stamps = pd.to_datetime(pd.Series(list(timestamps)), utc=True)
    horizon = end + pd.Timedelta(seconds=grace_seconds)
    in_window = ((stamps >= start) & (stamps < horizon)).to_numpy()
    scored = np.array([_verdict(v) is not None for v in faulted_verdicts], dtype=bool)
    if not bool((in_window & scored).any()):
        return DetectionOutcome("withheld")
    if bool((in_window & np.asarray(clean_alerting, dtype=bool)).any()):
        return DetectionOutcome("masked")
    caused = in_window & np.asarray(faulted_alerting, dtype=bool)
    hits = np.flatnonzero(caused)
    if hits.size == 0:
        return DetectionOutcome("missed")
    return DetectionOutcome("detected", float((stamps.iloc[int(hits[0])] - start).total_seconds()))


# ---------------------------------------------------------------------------
# Thresholds from the alert budget (A02.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreTrace:
    """One session's per-row scores for threshold setting."""

    timestamps: pd.Series
    scores: np.ndarray[Any, np.dtype[np.float64]]
    running: np.ndarray[Any, np.dtype[np.bool_]]


@dataclass(frozen=True)
class BudgetThreshold:
    """A threshold chosen so alert episodes stay within the budget."""

    threshold: float | None
    budget_per_engine_hour: float
    achieved_per_engine_hour: float | None
    engine_hours: float
    #: Independent (non-overlapping) blocks the candidates were drawn from.
    blocks: int
    #: ``False`` when the exposure cannot tell the budget from zero alerts
    #: (``engine_hours * budget < 1``): the threshold is then simply above
    #: every calibration score, and says nothing about the budget.
    resolvable: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "budget_per_engine_hour": self.budget_per_engine_hour,
            "achieved_per_engine_hour": self.achieved_per_engine_hour,
            "engine_hours": self.engine_hours,
            "blocks": self.blocks,
            "resolvable": self.resolvable,
            "note": self.note,
        }


def block_maxima(
    timestamps: pd.Series,
    scores: np.ndarray[Any, np.dtype[np.float64]],
    *,
    block_seconds: float,
) -> np.ndarray[Any, np.dtype[np.float64]]:
    """Maximum finite score in each non-overlapping ``block_seconds`` block.

    Adjacent 1 Hz scores (and stride-1 windows) are nearly the same sample;
    block maxima are the independent draws a quantile or a budget needs.
    """
    stamps = pd.to_datetime(timestamps, utc=True).reset_index(drop=True)
    if stamps.empty:
        return np.zeros(0, dtype=np.float64)
    seconds = (stamps - stamps.iloc[0]).dt.total_seconds().to_numpy()
    blocks = np.floor(seconds / block_seconds).astype(np.int64)
    # R04.11: the "max finite per block" tail is calibration's, shared.
    return max_finite_per_block(np.asarray(scores, dtype=np.float64), blocks)


def _episode_rate(
    traces: Sequence[ScoreTrace], threshold: float, persistence: PersistenceSpec
) -> tuple[int, float]:
    count = 0
    hours = 0.0
    for trace in traces:
        verdicts = [None if not np.isfinite(s) else bool(s > threshold) for s in trace.scores]
        alerting = sustained_alerts(trace.timestamps, verdicts, persistence)
        count += len(alert_episodes(trace.timestamps, alerting, verdicts=verdicts))
        hours += engine_hours(trace.timestamps, trace.running)
    return count, hours


def alert_budget_threshold(
    traces: Sequence[ScoreTrace],
    *,
    persistence: PersistenceSpec,
    block_seconds: float,
    budget_per_engine_hour: float = V1_ALERTS_PER_HOUR_BUDGET,
) -> BudgetThreshold:
    """The lowest threshold whose alert episodes meet the budget on ``traces``.

    Candidates are the pooled per-block maxima (independent windows, A02.3),
    so the choice never rests on one session's maximum of ~36 overlapping
    windows. The budget is checked the way alerts are produced: persistence
    gate, episodes, divided by engine-hours.
    """
    hours = sum(engine_hours(t.timestamps, t.running) for t in traces)
    maxima = [block_maxima(t.timestamps, t.scores, block_seconds=block_seconds) for t in traces]
    pooled = np.unique(np.concatenate(maxima)) if maxima else np.zeros(0)
    if pooled.size == 0:
        return BudgetThreshold(
            None,
            budget_per_engine_hour,
            None,
            hours,
            0,
            False,
            "withheld: no finite score on any calibration row",
        )
    if hours <= 0.0:
        return BudgetThreshold(
            None,
            budget_per_engine_hour,
            None,
            0.0,
            int(sum(m.size for m in maxima)),
            False,
            "withheld: no engine-running time on the calibration drives",
        )
    resolvable = hours * budget_per_engine_hour >= 1.0
    # Monotone in the threshold up to persistence-gate jitter: binary search
    # the sorted candidates, then report the rate actually achieved.
    low, high = 0, int(pooled.size) - 1
    best = high
    while low <= high:
        middle = (low + high) // 2
        episodes, _ = _episode_rate(traces, float(pooled[middle]), persistence)
        if episodes / hours <= budget_per_engine_hour:
            best = middle
            high = middle - 1
        else:
            low = middle + 1
    threshold = float(pooled[best])
    episodes, _ = _episode_rate(traces, threshold, persistence)
    note = (
        f"{episodes} episode(s) in {hours:.2f} engine-h at the chosen threshold"
        if resolvable
        else (
            f"unresolvable: {hours:.2f} engine-h cannot distinguish "
            f"{budget_per_engine_hour:g}/h from zero alerts; the threshold only clears "
            "every calibration episode"
        )
    )
    return BudgetThreshold(
        threshold,
        budget_per_engine_hour,
        episodes / hours,
        hours,
        int(sum(m.size for m in maxima)),
        resolvable,
        note,
    )


# ---------------------------------------------------------------------------
# Uncertainty and threshold-free comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RatioEstimate:
    """A ratio of per-drive sums with a drive-bootstrap interval."""

    estimate: float | None
    low: float | None
    high: float | None
    drives: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "estimate": self.estimate,
            "low": self.low,
            "high": self.high,
            "drives": self.drives,
            "note": self.note,
        }


def bootstrap_ratio_ci(
    numerators: Sequence[float],
    denominators: Sequence[float],
    *,
    n_boot: int = 2000,
    seed: int = 0,
    alpha: float = 0.05,
) -> RatioEstimate:
    """``sum(num) / sum(den)`` with a percentile interval from resampling drives.

    Each index is one physical drive. Resampling drives — never seconds or
    windows inside a drive, which are correlated — is what makes the
    interval honest about how few independent conditions there are.
    """
    num = np.asarray(numerators, dtype=np.float64)
    den = np.asarray(denominators, dtype=np.float64)
    drives = int(num.size)
    if drives == 0 or den.sum() <= 0:
        return RatioEstimate(None, None, None, drives, "withheld: no denominator")
    estimate = float(num.sum() / den.sum())
    if drives < 2:
        return RatioEstimate(estimate, None, None, drives, "one drive: no interval")
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, drives, size=(n_boot, drives))
    boot_den = den[picks].sum(axis=1)
    valid = boot_den > 0
    ratios = num[picks].sum(axis=1)[valid] / boot_den[valid]
    if ratios.size == 0:
        return RatioEstimate(estimate, None, None, drives, "no resample had a denominator")
    low, high = np.quantile(ratios, [alpha / 2, 1 - alpha / 2])
    return RatioEstimate(estimate, float(low), float(high), drives)


def _soft_labels(labels: np.ndarray[Any, np.dtype[np.bool_]], buffer: int) -> np.ndarray:
    """Range labels widened by ``buffer`` rows with a square-root ramp
    (Paparrizos et al., PVLDB 2022, the VUS definition)."""
    soft = labels.astype(np.float64).copy()
    if buffer <= 0:
        return soft
    half = buffer // 2
    n = labels.size
    edges = np.flatnonzero(np.diff(np.concatenate(([0], labels.astype(np.int8), [0]))))
    for start, stop in zip(edges[::2], edges[1::2], strict=True):
        after = np.arange(stop, min(stop + half, n))
        soft[after] = np.maximum(soft[after], np.sqrt(1 - (after - stop + 1) / buffer))
        before = np.arange(max(start - half, 0), start)
        soft[before] = np.maximum(soft[before], np.sqrt(1 - (start - before) / buffer))
    return soft


def _range_pr_auc(
    scores: np.ndarray[Any, np.dtype[np.float64]],
    labels: np.ndarray[Any, np.dtype[np.bool_]],
    buffer: int,
    thresholds: np.ndarray[Any, np.dtype[np.float64]],
) -> float:
    soft = _soft_labels(labels, buffer)
    extended = soft > 0
    edges = np.flatnonzero(np.diff(np.concatenate(([0], extended.astype(np.int8), [0]))))
    ranges = list(zip(edges[::2], edges[1::2], strict=True))
    positive = (labels.sum() + soft.sum()) / 2.0
    area = 0.0
    previous_recall = 0.0
    # Thresholds descend, so predictions only grow and recall never falls:
    # step-wise average precision, sum of (recall gained) x (precision there).
    # No interpolated (recall 0, precision 1) anchor, which would credit an
    # inverted detector with a free triangle.
    for threshold in thresholds:
        predicted = scores >= threshold
        count = int(predicted.sum())
        if count == 0:
            continue
        true_positive = float((soft * predicted).sum())
        existence = (
            sum(1 for a, b in ranges if predicted[a:b].any()) / len(ranges) if ranges else 0.0
        )
        recall = min(1.0, true_positive / positive) * existence if positive > 0 else 0.0
        if recall > previous_recall:
            area += (recall - previous_recall) * (true_positive / count)
            previous_recall = recall
    return float(area)


def vus_pr(
    scores: Sequence[float] | np.ndarray[Any, Any],
    labels: Sequence[bool] | np.ndarray[Any, Any],
    *,
    max_buffer: int = 30,
    buffer_steps: int = 6,
    n_thresholds: int = 100,
) -> float | None:
    """Volume under the range-based PR surface — threshold-free, for
    comparisons only (TSB-AD's recommended measure).

    Averages range-aware PR-AUC over label buffers ``0..max_buffer`` rows.
    Rows without a score rank lowest (the detector did not look there).
    ``None`` when there is no labelled or no unlabelled row.
    """
    values = np.asarray(scores, dtype=np.float64)
    truth = np.asarray(labels, dtype=bool)
    if truth.size == 0 or not truth.any() or truth.all():
        return None
    floor = np.nanmin(values) - 1.0 if np.isfinite(values).any() else 0.0
    values = np.where(np.isfinite(values), values, floor)
    quantiles = np.linspace(1.0, 0.0, n_thresholds)
    thresholds = np.unique(np.quantile(values, quantiles))[::-1]
    buffers = np.unique(np.linspace(0, max_buffer, buffer_steps).astype(int))
    return float(np.mean([_range_pr_auc(values, truth, int(b), thresholds) for b in buffers]))


# ---------------------------------------------------------------------------
# Strata
# ---------------------------------------------------------------------------


def band_of(value: float | None, bands: Sequence[tuple[float | None, float | None, str]]) -> str:
    if value is None or not math.isfinite(value):
        return UNMEASURED
    for low, high, label in bands:
        if (low is None or value >= low) and (high is None or value < high):
            return label
    return UNMEASURED


def observed_median(frame: pd.DataFrame, column: str) -> float | None:
    """Median of a channel over rows where it was freshly observed (its
    ``__gap`` flag clear), so a forward-filled hold is not re-counted."""
    if column not in frame.columns:
        return None
    values = pd.to_numeric(frame[column], errors="coerce")
    gap = f"{column}__gap"
    if gap in frame.columns:
        values = values[~frame[gap].fillna(True).astype(bool)]
    values = values.dropna()
    return None if values.empty else float(values.median())


# ---------------------------------------------------------------------------
# Known answer: single-quantity shift (A04.1)
# ---------------------------------------------------------------------------

#: Shift sizes, in standard deviations of the quantity within the row's
#: operating mode. The 2026-09-22 review's sweep.
SHIFT_SIGMAS: tuple[float, ...] = (3.0, 10.0, 30.0, 100.0)
#: A detector passes when a shift of this size in *any single* quantity flags
#: at least :data:`SHIFT_PASS_FLAG_RATE` of the rows it scored as quiet.
SHIFT_PASS_SIGMA = 10.0
SHIFT_PASS_FLAG_RATE = 0.5
#: The control: every numeric column moved together by the largest shift
#: size must flag at least :data:`SHIFT_CONTROL_MIN_FLAG_RATE`, or the case is
#: invalid for that detector (the harness, not the detector, is then in
#: question). The review's 3 sigma all-feature control suits the IF, whose
#: score sums over every feature; a max-|z| detector thresholded at 6 sigma
#: legitimately ignores 3 sigma everywhere, so the control uses the largest size.
SHIFT_CONTROL_MIN_FLAG_RATE = 0.9
SHIFT_CONTROL = "_control_all_features"
_SHIFTED_SUFFIXES = ("", "__mean")


def shift_columns(frame: pd.DataFrame, channels: Sequence[str]) -> list[str]:
    """The columns a sustained offset in ``channels`` moves: each raw channel
    and its rolling mean. ``__std``/``__roc`` do not move under a constant
    offset, so they are left alone."""
    return [
        f"{channel}{suffix}"
        for channel in channels
        for suffix in _SHIFTED_SUFFIXES
        if f"{channel}{suffix}" in frame.columns
        and pd.api.types.is_numeric_dtype(frame[f"{channel}{suffix}"])
        and not pd.api.types.is_bool_dtype(frame[f"{channel}{suffix}"])
    ]


def mode_spreads(frame: pd.DataFrame, columns: Sequence[str], mode_column: str) -> pd.DataFrame:
    """Per row, each column's standard deviation within the row's operating
    mode on ``frame`` (the pooled one where a mode has no spread)."""
    values = frame[list(columns)].apply(pd.to_numeric, errors="coerce")
    pooled = values.std()
    if mode_column in frame.columns:
        per_mode = values.groupby(frame[mode_column].astype(str)).transform("std")
        spreads = per_mode.where(per_mode > 0).fillna(pooled)
    else:
        spreads = pd.DataFrame(
            np.broadcast_to(pooled.to_numpy(), values.shape),
            index=frame.index,
            columns=list(columns),
        )
    out: pd.DataFrame = spreads.where(spreads > 0)
    return out


@dataclass(frozen=True)
class ShiftOutcome:
    """One (quantity, size) cell for one detector.

    ``rows`` are the rows the detector scored *quiet* on the clean slice
    where the quantity was freshly observed (every row for the control);
    ``scored`` of them still had a verdict after the shift, and ``flagged``
    of those were flagged. ``flag_rate`` is ``None`` when nothing was scored
    (withheld, never a zero).
    """

    quantity: str
    sigma: float
    columns: tuple[str, ...]
    rows: int
    scored: int
    flagged: int

    @property
    def flag_rate(self) -> float | None:
        return None if self.scored == 0 else self.flagged / self.scored

    def to_dict(self) -> dict[str, Any]:
        return {
            "quantity": self.quantity,
            "sigma": self.sigma,
            "columns": list(self.columns),
            "rows": self.rows,
            "scored": self.scored,
            "flagged": self.flagged,
            "flag_rate": self.flag_rate,
        }


@dataclass(frozen=True)
class ShiftReport:
    """A detector's single-quantity-shift known answer on one slice."""

    outcomes: tuple[ShiftOutcome, ...]
    control: ShiftOutcome | None
    verdict: Literal["pass", "fail", "invalid", "withheld"]
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "note": self.note,
            "control": None if self.control is None else self.control.to_dict(),
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


VerdictScorer = Callable[[pd.DataFrame], Mapping[str, Sequence[Any]]]


def single_quantity_shift(
    score: VerdictScorer,
    frame: pd.DataFrame,
    quantities: Mapping[str, Sequence[str]],
    *,
    mode_column: str,
    sigmas: Sequence[float] = SHIFT_SIGMAS,
) -> dict[str, ShiftReport]:
    """The A04.1 known answer, for every detector ``score`` returns.

    Shift one quantity (every column that carries it) by k standard deviations
    of its operating mode, on rows each detector scored quiet, and count what
    it flags. The deployed IF flagged 0 % at every size up to 100 sigma — a
    value past the training range follows the same branch as the training
    maximum, and one of ~250 features lies on ~3 % of a path — while shifting
    every feature by 3 sigma flagged 100 %. A detector that cannot see a
    single-quantity fault of any size is recorded as failing, not averaged
    away. Row-level verdicts (no persistence gate): this measures whether the
    score moves at all.
    """
    clean = {name: list(v) for name, v in score(frame).items()}
    every = sorted({c for channels in quantities.values() for c in shift_columns(frame, channels)})
    all_numeric = [
        str(c)
        for c in frame.columns
        if pd.api.types.is_numeric_dtype(frame[c])
        and not pd.api.types.is_bool_dtype(frame[c])
        and not str(c).endswith(("__gap", "__arrived"))
        and str(c) not in (mode_column,)
    ]
    spreads = mode_spreads(frame, all_numeric, mode_column)

    def shifted(columns: Sequence[str], sigma: float) -> pd.DataFrame:
        out = frame.copy()
        for column in columns:
            delta = (sigma * spreads[column]).fillna(0.0)
            out[column] = pd.to_numeric(out[column], errors="coerce") + delta
        return out

    def observed_rows(channels: Sequence[str]) -> np.ndarray[Any, np.dtype[np.bool_]]:
        """Rows where some channel of the quantity carries a fresh sample
        (its ``__gap`` flag clear; no flag = always fresh). A fault in a
        quantity polled every 5 s can only show where it was observed: a
        detector reading arrived samples must not be graded on the rows
        between them."""
        seen = np.zeros(len(frame), dtype=bool)
        for channel in channels:
            gap = f"{channel}__gap"
            if channel not in frame.columns:
                continue
            if gap not in frame.columns:
                return np.ones(len(frame), dtype=bool)
            seen |= ~frame[gap].fillna(True).astype(bool).to_numpy()
        return seen

    def outcome(
        name: str,
        quantity: str,
        sigma: float,
        columns: Sequence[str],
        verdicts: Any,
        rows: np.ndarray[Any, np.dtype[np.bool_]] | None = None,
    ) -> ShiftOutcome:
        quiet = [
            i
            for i, v in enumerate(clean[name])
            if _verdict(v) is False and (rows is None or bool(rows[i]))
        ]
        after = [_verdict(verdicts[i]) for i in quiet]
        scored = [v for v in after if v is not None]
        return ShiftOutcome(
            quantity, float(sigma), tuple(columns), len(quiet), len(scored), sum(scored)
        )

    cells: dict[str, list[ShiftOutcome]] = {name: [] for name in clean}
    for quantity, channels in quantities.items():
        columns = shift_columns(frame, channels)
        if not columns:
            continue
        seen = observed_rows(channels)
        for sigma in sigmas:
            result = score(shifted(columns, sigma))
            for name in clean:
                if name in result:
                    cells[name].append(
                        outcome(name, quantity, sigma, columns, list(result[name]), seen)
                    )
    control_columns = all_numeric if all_numeric else every
    control_sigma = float(max(sigmas))
    control_result = score(shifted(control_columns, control_sigma))
    reports: dict[str, ShiftReport] = {}
    for name in clean:
        control = (
            outcome(
                name,
                SHIFT_CONTROL,
                control_sigma,
                control_columns,
                list(control_result[name]),
            )
            if name in control_result
            else None
        )
        reports[name] = _shift_verdict(tuple(cells[name]), control)
    return reports


def _shift_verdict(outcomes: tuple[ShiftOutcome, ...], control: ShiftOutcome | None) -> ShiftReport:
    if control is None or control.rows == 0 or control.flag_rate is None:
        return ShiftReport(
            outcomes, control, "withheld", "withheld: no row the detector scored quiet"
        )
    if control.flag_rate < SHIFT_CONTROL_MIN_FLAG_RATE:
        return ShiftReport(
            outcomes,
            control,
            "invalid",
            f"control flagged {control.flag_rate:.0%} < {SHIFT_CONTROL_MIN_FLAG_RATE:.0%}: "
            "the shift did not reach this detector's inputs",
        )
    at_pass = [o for o in outcomes if o.sigma == SHIFT_PASS_SIGMA]
    blind = sorted(
        o.quantity
        for o in at_pass
        if o.flag_rate is not None and o.flag_rate < SHIFT_PASS_FLAG_RATE
    )
    if not at_pass or all(o.flag_rate is None for o in at_pass):
        return ShiftReport(outcomes, control, "withheld", "withheld: no shifted quantity scored")
    if blind:
        return ShiftReport(
            outcomes,
            control,
            "fail",
            f"a +{SHIFT_PASS_SIGMA:g} sigma shift flagged under {SHIFT_PASS_FLAG_RATE:.0%} "
            f"of rows for: {', '.join(blind)}",
        )
    return ShiftReport(outcomes, control, "pass", "every quantity visible at +10 sigma")


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


@dataclass
class DriveDetectorResult:
    """One detector on one drive's clean rows."""

    rows: int = 0
    scored_rows: int = 0
    flagged_rows: int = 0
    episodes: int = 0
    #: Episodes that start inside a labelled interval, by label kind — reported
    #: beside, never inside, the false-alarm count.
    labelled_episodes: dict[str, int] = field(default_factory=dict)

    @property
    def withheld_rows(self) -> int:
        return self.rows - self.scored_rows

    @property
    def false_alarm_episodes(self) -> int:
        return self.episodes - sum(self.labelled_episodes.values())

    def to_dict(self, hours: float) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "scored_rows": self.scored_rows,
            "withheld_rows": self.withheld_rows,
            "flagged_rows": self.flagged_rows,
            "episodes": self.episodes,
            "labelled_episodes": dict(self.labelled_episodes),
            "false_alarm_episodes": self.false_alarm_episodes,
            "false_alarms_per_engine_hour": (
                None if hours <= 0 else self.false_alarm_episodes / hours
            ),
        }


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


__all__ = [
    "AMBIENT_BANDS_C",
    "AMBIENT_CHANNEL",
    "BARO_BANDS_KPA",
    "BARO_CHANNEL",
    "DRIVE_GAP_SECONDS",
    "BudgetThreshold",
    "DetectionOutcome",
    "Drive",
    "DriveDetectorResult",
    "DriveHoldout",
    "EVALUATION_HOLDOUT_DRIVES",
    "Episode",
    "Era",
    "Fold",
    "HoldoutSplit",
    "MIN_CALIBRATION_HOLDOUT_BYTES",
    "MIN_CALIBRATION_HOLDOUT_SESSIONS",
    "MIN_HOLDOUT_MODE_SECONDS",
    "PersistenceSpec",
    "RatioEstimate",
    "ScoreTrace",
    "SHIFT_CONTROL",
    "SHIFT_SIGMAS",
    "SessionSpan",
    "ShiftOutcome",
    "ShiftReport",
    "alert_budget_threshold",
    "alert_episodes",
    "band_of",
    "block_maxima",
    "bootstrap_ratio_ci",
    "drives_of_sessions",
    "engine_hours",
    "engine_running",
    "era_resolver",
    "fault_attributable_detection",
    "group_into_drives",
    "grouped_folds",
    "holdout_by_drive",
    "nothing_left_to_train",
    "observed_median",
    "pick_calibration_holdouts",
    "pick_evaluation_holdouts",
    "session_holdout_health",
    "session_mode_coverage",
    "session_parquet_bytes",
    "session_span",
    "shift_columns",
    "single_quantity_shift",
    "span_from_name",
    "sustained_alerts",
    "vus_pr",
]
