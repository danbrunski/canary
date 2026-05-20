"""Auto-detect physical events from a resampled telemetry frame.

Each detector is a pure function:

    detector(segmented: pd.DataFrame, mode_segments: list[ModeSegment])
        -> list[DetectedEvent]

It receives the resampled + segmented frame produced by
:func:`src.analysis.session_classifier.classify_session` and returns
zero or more :class:`DetectedEvent` records. Detectors are deliberately
rule-based — auditable, deterministic, no labels needed — and small
(target: < 60 LoC each).

Adding a new detector:

1. Create ``src/analysis/events/<name>.py`` exporting one callable
   matching :class:`EventDetectorCallable`.
2. Import it in this ``__init__`` and add it to
   :func:`default_detectors` so the classifier picks it up.
3. Add a unit test under ``tests/test_events_<name>.py`` covering
   the trigger, a near-miss, and a no-OBD-data degenerate input.
"""

from __future__ import annotations

from collections.abc import Iterable

from src.analysis.events.cold_start import detect_cold_starts
from src.analysis.events.cruise import detect_cruise
from src.analysis.events.hard_accel import detect_hard_accels
from src.analysis.events.hard_brake import detect_hard_brakes
from src.analysis.events.idle_period import detect_idle_periods
from src.analysis.events.stop import detect_stops
from src.analysis.events.wot_run import detect_wot_runs
from src.analysis.session_classifier import EventDetectorCallable


def default_detectors() -> Iterable[EventDetectorCallable]:
    """The detector pipeline the classifier runs by default."""
    return (
        detect_cold_starts,
        detect_idle_periods,
        detect_stops,
        detect_hard_accels,
        detect_hard_brakes,
        detect_cruise,
        detect_wot_runs,
    )


__all__ = [
    "default_detectors",
    "detect_cold_starts",
    "detect_cruise",
    "detect_hard_accels",
    "detect_hard_brakes",
    "detect_idle_periods",
    "detect_stops",
    "detect_wot_runs",
]
