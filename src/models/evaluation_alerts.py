"""Alert persistence for the detector evaluation.

The production M-of-N persistence gate as a spec (:class:`PersistenceSpec`),
row verdicts through it (:func:`sustained_alerts`) and the alert *episodes*
those make (:func:`alert_episodes`). Split out of :mod:`src.models.evaluation`
(R27.7), which re-exports every name here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.config import CanaryConfig
from src.models.fusion import PersistenceGate

#: Longest step between rows that still counts as continuous engine time when
#: integrating engine-hours (the 1 Hz grid with a few dropped ticks).
MAX_ENGINE_STEP_SECONDS = 5.0


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
