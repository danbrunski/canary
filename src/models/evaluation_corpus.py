"""Corpus structure for the detector evaluation: sessions, drives, eras, holdouts.

Which recordings exist, how they group into physical drives, which tune or
hardware era a drive belongs to, and how a holdout takes whole drives
(:class:`HoldoutSplit`, :func:`holdout_by_drive`, the calibration and
evaluation pickers, :func:`grouped_folds`). Also
:func:`refuse_non_default_profile_sessions`, the other half of "which sessions
may a detector see": it is the corpus-eligibility rule applied at the trainer.

Split out of :mod:`src.models.evaluation` (R27.7), which re-exports every name
here so existing imports keep working.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.models.sequence_registry import MIN_CALIBRATION_SESSIONS
from src.preprocessing.drives import (
    DRIVE_GAP_SECONDS,
    SessionSpan,
    drive_id_for,
    join_spans,
    physical_drives,
    recording_units,
)
from src.session import session_sort_key
from src.storage.corpus_eligibility import default_profile_exclusion
from src.storage.session_files import PARQUET_PATTERN
from src.storage.sessions import session_started_at
from src.timefmt import as_utc
from src.vehicle.tune_revisions import TuneRevisionRegistry

log = logging.getLogger(__name__)


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
    ordered = sorted(as_utc(moment) for moment in boundaries)

    def resolve(moment: datetime) -> Era:
        when = as_utc(moment)
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
    start = as_utc(start) if start is not None else min(as_utc(span.start) for span in members)
    end = as_utc(end) if end is not None else max(as_utc(span.end) for span in members)
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
                lo, hi = extent.get(span.name, (as_utc(span.start), as_utc(span.end)))
                extent[span.name] = (min(lo, as_utc(span.start)), max(hi, as_utc(span.end)))
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
# Which recordings may train
# ---------------------------------------------------------------------------


class NonDefaultProfileTrainingError(ValueError):
    """A non-default-profile recording was handed to a detector trainer."""


def refuse_non_default_profile_sessions(sessions: Sequence[Path], *, purpose: str) -> None:
    """Refuse to train or calibrate a detector on a non-default-profile recording.

    Operator decision 2026-09-24/25 (R03.12/R04.6): tuning captures never
    train, calibrate, evaluate or score the IF, the sequence detector or the
    behavior deviations those detectors read as features. Training discovery
    already leaves every ``_``-prefixed recording out
    (``preprocessing.loader.discover_sessions``); this is the same rule at the
    trainer, so an explicitly named capture cannot bring its rows in. Since
    P02.12 the training frame carries ``deviation:*`` columns, and this keeps
    their training rows default-profile only, as the daemon's gate keeps their
    served rows.
    """
    refused = {
        Path(session).name: reason
        for session in sessions
        if (reason := default_profile_exclusion(Path(session))) is not None
    }
    if refused:
        raise NonDefaultProfileTrainingError(
            f"refusing to {purpose} on non-default-profile recordings (R03.12, operator "
            "decision 2026-09-24): "
            + "; ".join(f"{name}: {reason}" for name, reason in sorted(refused.items()))
        )
