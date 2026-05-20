"""Tests for the ``require_session_events`` filter in :func:`fit_recipe`."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.analysis.refit import Surface1DRecipe, fit_recipe
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN


def _frame(sessions_to_rows: dict[str, int]) -> pd.DataFrame:
    """Build a synthetic frame with one mode and N rows per named session."""
    rng = np.random.default_rng(42)
    rows = []
    for session_id, n in sessions_to_rows.items():
        for i in range(n):
            rows.append(
                {
                    SESSION_COLUMN: session_id,
                    TIMESTAMP_COLUMN: pd.Timestamp("2026-05-20T12:00:00Z")
                    + pd.Timedelta(seconds=i),
                    MODE_COLUMN: "warmup",
                    "obd:RUN_TIME": float(i + 1),
                    "obd:COOLANT_TEMP": float(25.0 + i * 0.5 + rng.normal(0, 0.2)),
                }
            )
    return pd.DataFrame(rows)


def _recipe() -> Surface1DRecipe:
    return Surface1DRecipe(
        name="coolant_warmup_test",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        x_axis_name="time_since_start",
        operating_mode="warmup",
        require_session_events=("cold_start",),
        fit_method="poly",
        degree=2,
        min_samples=20,
    )


def test_event_gate_drops_sessions_without_required_event() -> None:
    """Only sessions whose classifier sidecar reports a ``cold_start`` feed
    the recipe. Sessions tagged with a different event (or none) get
    filtered out.
    """
    frame = _frame({"with_cold_start": 60, "without": 60})
    session_events = {
        "with_cold_start": {"cold_start", "idle_period"},
        "without": {"idle_period"},
    }
    result = fit_recipe(_recipe(), frame, session_events=session_events)
    assert result.fitted
    # The fitted model only saw the 60-row session, so its sample_count
    # reflects that (the other session was filtered before min_samples).
    assert result.model is not None
    assert result.model.fit.sample_count <= 60


def test_event_gate_skips_recipe_when_no_session_qualifies() -> None:
    frame = _frame({"a": 80, "b": 80})
    session_events = {"a": {"idle_period"}, "b": set()}
    result = fit_recipe(_recipe(), frame, session_events=session_events)
    assert not result.fitted
    assert "no session in corpus carries every required event" in (result.reason or "")


def test_event_gate_noop_when_session_events_not_provided() -> None:
    """Back-compat: callers that haven't wired classification yet still fit."""
    frame = _frame({"a": 80, "b": 80})
    result = fit_recipe(_recipe(), frame, session_events=None)
    assert result.fitted


def test_event_gate_requires_session_column_in_frame() -> None:
    """A frame without ``session`` can't be event-gated — recipe is skipped
    with a helpful reason rather than silently producing a bad fit.
    """
    frame = _frame({"a": 80})
    frame = frame.drop(columns=[SESSION_COLUMN])
    session_events = {"a": {"cold_start"}}
    result = fit_recipe(_recipe(), frame, session_events=session_events)
    assert not result.fitted
    assert "can't enforce event gate" in (result.reason or "")
