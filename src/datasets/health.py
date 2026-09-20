"""Dataset health analysis — "is this corpus ready to train?" (Task 6.23).

Before a retraining job burns Pi cycles on a thin or skewed corpus, the
operator (or the companion app) should be able to see what's actually
there: per-session coverage by operating mode, the value ranges that
matter (speed / load / coolant), tags, channel availability, gap/dropout
rate, the stale/dirty/pinned flags, and a blunt go/no-go readiness verdict
with reasons.

This is the backend the 6.23 dashboard renders and the gate
``scripts/retrain.py`` callers consult. Pure analysis over recorded
sessions — reuses the existing loader / resampler / segmenter so the
"coverage" it reports is exactly the grid the trainers see, not a parallel
estimate.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CanaryConfig
from src.control.sessions import read_session_metadata
from src.decode.engineering import UnscaledDecodedChannelError, physical_series
from src.preprocessing.feature_pipeline import segment_or_constant
from src.preprocessing.loader import read_session, to_wide
from src.preprocessing.resampler import GAP_SUFFIX, ResampleConfig, resample_wide
from src.preprocessing.segmenter import MODE_COLUMN
from src.session import MANIFEST_FILENAME, TAGS_FILENAME, load_tags, session_paths

log = logging.getLogger(__name__)

__all__ = [
    "CorpusHealth",
    "ReadinessCheck",
    "SessionHealth",
    "analyze_corpus",
    "analyze_session",
]

#: Channels a useful anomaly model needs at minimum. Absence is a hard
#: "not ready" — a model trained without speed/RPM can't separate modes.
KEY_CHANNELS: tuple[str, ...] = ("obd:RPM", "obd:SPEED")

#: Range channels worth reporting min/max for (operating-envelope coverage).
#: Reported in engineering units: a ``decoded:`` fallback is raw binding counts
#: and is scaled via ``src.decode.engineering.physical_series`` below.
_RANGE_CHANNELS = {
    "speed": ("obd:SPEED", "decoded:WHEEL_SPEED_6F7_C"),
    "engine_load": ("obd:ENGINE_LOAD", "obd:ABSOLUTE_LOAD"),
    "coolant_temp": ("obd:COOLANT_TEMP", "decoded:ECM_COOLANT_TEMP"),
    "rpm": ("obd:RPM", "decoded:ECM_RPM"),
}


@dataclass
class SessionHealth:
    """Per-session coverage + quality."""

    session_id: str
    row_count: int
    duration_seconds: float | None
    channels: list[str]
    channel_nan_fraction: dict[str, float]
    mode_coverage: dict[str, int]
    value_ranges: dict[str, dict[str, float] | None]
    tags: list[str]
    gap_fraction: float | None
    pinned: bool
    training_candidate: bool
    parse_error_ratio: float | None
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReadinessCheck:
    ready: bool
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CorpusHealth:
    sessions: list[SessionHealth]
    total_rows: int
    total_duration_seconds: float
    mode_coverage: dict[str, int]
    channel_availability: dict[str, float]  # channel -> fraction of sessions present
    readiness: ReadinessCheck
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": [s.to_dict() for s in self.sessions],
            "total_rows": self.total_rows,
            "total_duration_seconds": self.total_duration_seconds,
            "mode_coverage": self.mode_coverage,
            "channel_availability": self.channel_availability,
            "readiness": self.readiness.to_dict(),
            "warnings": self.warnings,
        }


def _first_physical(wide: pd.DataFrame, names: tuple[str, ...]) -> pd.Series | None:
    """First candidate column carrying data, already in engineering units.

    Returns the *values*, never the column name: a caller holding the name
    would re-read the raw column, and a ``decoded:`` column holds binding
    counts (reporting those as a value range is the same defect the mode
    segmenter carried from 2026-08-06 to 2026-09-18). A ``decoded:`` candidate
    whose binding has no pinned scale has no engineering value at all, so it is
    skipped exactly like an absent column instead of being reported as counts.
    """
    for n in names:
        if n in wide.columns and wide[n].notna().any():
            try:
                return physical_series(wide[n], n)
            except UnscaledDecodedChannelError:
                log.warning(
                    "skipping unscaled decoded channel in value ranges",
                    extra={"channel": n},
                )
    return None


def _parse_error_ratio(session_dir: Path) -> float | None:
    path = session_dir / MANIFEST_FILENAME
    if not path.exists():
        return None
    try:
        import json

        meta = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    errors = meta.get("parse_errors")
    frames = meta.get("frames_recorded")
    if isinstance(errors, int) and isinstance(frames, int) and frames > 0:
        return round(errors / frames, 4)
    return None


def analyze_session(session_dir: Path, config: CanaryConfig) -> SessionHealth:
    """Coverage + quality for one session. Never raises on a bad session —
    an unreadable/empty session becomes a zero-row health row with a
    warning, so one bad capture can't blind the whole corpus view."""
    session_id = session_dir.name
    warnings: list[str] = []
    metadata = read_session_metadata(session_dir)
    pinned = bool(metadata.get("pinned", False))
    training_candidate = bool(metadata.get("training_candidate", False))
    parse_ratio = _parse_error_ratio(session_dir)
    if parse_ratio is not None and parse_ratio > 1.0:
        warnings.append(f"parse_errors exceed frames ({parse_ratio:.1f}×) — slcan noise")

    try:
        long = read_session(session_dir)
    except (OSError, ValueError) as exc:
        warnings.append(f"unreadable session: {exc}")
        long = pd.DataFrame()

    if long.empty:
        warnings.append("no telemetry rows")
        return SessionHealth(
            session_id=session_id,
            row_count=0,
            duration_seconds=None,
            channels=[],
            channel_nan_fraction={},
            mode_coverage={},
            value_ranges={},
            tags=_session_tags(session_dir),
            gap_fraction=None,
            pinned=pinned,
            training_candidate=training_candidate,
            parse_error_ratio=parse_ratio,
            warnings=warnings,
        )

    wide = to_wide(long, coerce_numeric=True)
    resampled = resample_wide(
        wide,
        config=ResampleConfig(
            hz=config.preprocessing.resample_hz,
            slow_channel_max_gap_seconds=config.preprocessing.slow_channel_max_gap_seconds,
        ),
    )
    segmented, _fell_back = segment_or_constant(resampled, config)
    channels = sorted(
        c for c in wide.columns if c not in ("session", "timestamp") and not c.endswith(GAP_SUFFIX)
    )
    nan_fraction = {c: round(float(wide[c].isna().mean()), 4) for c in channels}
    mode_coverage = {str(k): int(v) for k, v in segmented[MODE_COLUMN].value_counts().items()}
    value_ranges: dict[str, dict[str, float] | None] = {}
    for name, candidates in _RANGE_CHANNELS.items():
        resolved = _first_physical(wide, candidates)
        if resolved is None:
            value_ranges[name] = None
        else:
            series = resolved.dropna()
            value_ranges[name] = (
                {"min": round(float(series.min()), 3), "max": round(float(series.max()), 3)}
                if not series.empty
                else None
            )

    gap_cols = [c for c in resampled.columns if c.endswith(GAP_SUFFIX)]
    gap_fraction: float | None = None
    if gap_cols and len(resampled):
        gap_fraction = round(float(resampled[gap_cols].any(axis=1).mean()), 4)

    ts = pd.to_datetime(long["timestamp"], utc=True)
    duration = float((ts.max() - ts.min()).total_seconds()) if len(ts) > 1 else 0.0
    if duration < 60:
        warnings.append(f"short capture ({duration:.0f}s) — limited training value")
    for key in KEY_CHANNELS:
        if key not in channels:
            warnings.append(f"missing key channel {key}")

    return SessionHealth(
        session_id=session_id,
        row_count=int(len(wide)),
        duration_seconds=duration,
        channels=channels,
        channel_nan_fraction=nan_fraction,
        mode_coverage=mode_coverage,
        value_ranges=value_ranges,
        tags=_session_tags(session_dir),
        gap_fraction=gap_fraction,
        pinned=pinned,
        training_candidate=training_candidate,
        parse_error_ratio=parse_ratio,
        warnings=warnings,
    )


def _session_tags(session_dir: Path) -> list[str]:
    paths = session_paths(session_dir)
    if not (session_dir / TAGS_FILENAME).exists():
        return []
    return sorted({str(t["label"]) for t in load_tags(paths.tags)})


def _session_started_date(session_dir: Path) -> date | None:
    """Parse the ``YYYY-MM-DDTHHMMSS`` session-dir naming convention (matches
    ``scripts/refit_behavior_models.py``'s ``_filter_sessions_by_age``).
    ``None`` for a non-conforming name (e.g. a manual import) — callers treat
    that as "can't place it in time", not as before/after the boundary."""
    try:
        return datetime.strptime(session_dir.name, "%Y-%m-%dT%H%M%S").date()
    except ValueError:
        return None


def analyze_corpus(
    session_dirs: list[Path],
    config: CanaryConfig,
    *,
    min_sessions: int = 3,
    min_total_rows: int = 1000,
    corpus_boundary: date | None = None,
) -> CorpusHealth:
    """Aggregate health + a go/no-go readiness verdict.

    ``ready`` is intentionally conservative — it gates a retraining job, and
    a model trained on a thin/biased corpus that looks fine is the exact
    failure 6.23 exists to prevent. Every blocking condition is listed in
    ``reasons`` so the operator knows what to capture next.

    ``corpus_boundary`` (Task 3.5) is the latest ``corpus_boundary`` vehicle-
    ledger event's ``occurred_at``, if any. When it's set and ``session_dirs``
    contains sessions both before and on/after it, a note is appended to
    ``warnings`` flagging the corpus as bisected — surfacing it rather than
    silently filtering, since only ``--since-boundary`` opts a caller into
    dropping the older sessions.
    """
    healths = [analyze_session(d, config) for d in session_dirs]
    usable = [h for h in healths if h.row_count > 0]
    total_rows = sum(h.row_count for h in usable)
    total_duration = sum(h.duration_seconds or 0.0 for h in usable)

    mode_coverage: dict[str, int] = {}
    for h in usable:
        for mode, n in h.mode_coverage.items():
            mode_coverage[mode] = mode_coverage.get(mode, 0) + n

    channel_availability: dict[str, float] = {}
    if usable:
        all_channels: set[str] = set()
        for h in usable:
            all_channels.update(h.channels)
        for ch in sorted(all_channels):
            present = sum(1 for h in usable if ch in h.channels)
            channel_availability[ch] = round(present / len(usable), 3)

    reasons: list[str] = []
    if len(usable) < min_sessions:
        reasons.append(f"only {len(usable)} usable sessions (need ≥ {min_sessions})")
    if total_rows < min_total_rows:
        reasons.append(f"only {total_rows} rows (need ≥ {min_total_rows})")
    for key in KEY_CHANNELS:
        if channel_availability.get(key, 0.0) <= 0.0:
            reasons.append(f"key channel {key} absent from every session")
    if usable and not any(not h.warnings for h in usable):
        reasons.append("no warning-free baseline session (every session has a quality flag)")

    warnings: list[str] = []
    if not usable:
        warnings.append("no usable sessions on disk")

    if corpus_boundary is not None:
        started = [_session_started_date(d) for d in session_dirs]
        known = [d for d in started if d is not None]
        older = sum(1 for d in known if d < corpus_boundary)
        newer_or_on = sum(1 for d in known if d >= corpus_boundary)
        if older and newer_or_on:
            warnings.append(
                f"corpus bisected by a vehicle-ledger boundary event on "
                f"{corpus_boundary.isoformat()}: {older} session(s) predate it, "
                f"{newer_or_on} don't — pass --since-boundary to train only on "
                "post-boundary sessions"
            )

    return CorpusHealth(
        sessions=healths,
        total_rows=total_rows,
        total_duration_seconds=round(total_duration, 1),
        mode_coverage=mode_coverage,
        channel_availability=channel_availability,
        readiness=ReadinessCheck(ready=not reasons and bool(usable), reasons=reasons),
        warnings=warnings,
    )
