"""B04.1 — reporting strata: calibration, context bins, steady vs transient.

Strata are profile data (`behavior_validation.yaml: report_regimes`) and they
only *report*: a stratum never selects rows for fitting or serving. What they
must not do is quietly fold an unavailable context into a numeric bin, which
is how "the model is fine at high IAT" gets said about rows where IAT was
stale.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.behavior_validation import (
    EventPolicy,
    ReportRegimes,
    breakdown_by_labels,
    evaluate_rows,
    load_validation_document,
    regime_labels,
)
from src.analysis.refit import fit_recipe
from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column
from tests.builders import (
    BEHAVIOR_PROFILE_CRITERIA,
    behavior_corpus,
    behavior_criteria,
    behavior_recipe,
)

REGIMES = ReportRegimes.model_validate(
    {
        "axes": {"load": {"channel": "obd:ENGINE_LOAD", "unit": "%", "edges": [0, 30, 60, 101]}},
        "steady_state": {
            "max_abs_rate_per_second": {"obd:RPM": 10.0},
            "basis": "test",
        },
    }
)


def _frame() -> pd.DataFrame:
    n = 120
    load = np.concatenate([np.full(40, 10.0), np.full(40, 45.0), np.full(40, np.nan)])
    rpm = np.concatenate([np.full(60, 1000.0), np.linspace(1000.0, 4000.0, 60)])
    frame = pd.DataFrame(
        {
            TIMESTAMP_COLUMN: pd.date_range("2026-09-18T12:00:00Z", periods=n, freq="1s"),
            SESSION_COLUMN: "2026-09-18T120000",
            MODE_COLUMN: "city",
            "obd:RPM": rpm,
            "obd:ENGINE_LOAD": load,
        }
    )
    stale = np.zeros(n, dtype=bool)
    stale[20:30] = True  # recorded but not fresh
    frame[gap_column("obd:ENGINE_LOAD")] = stale
    return frame


def test_unavailable_context_is_its_own_stratum() -> None:
    labels = regime_labels(_frame(), REGIMES)["load"]
    assert set(labels) == {"[0,30) %", "[30,60) %", "missing", "stale"}
    # Stale rows are never counted into the bin their (stale) value implies.
    assert (labels[20:30] == "stale").all()
    assert (labels[80:] == "missing").all()


def test_steady_and_transient_split_by_the_persisted_derivative() -> None:
    labels = regime_labels(_frame(), REGIMES)["steady_state"]
    counts = pd.Series(labels).value_counts().to_dict()
    # The rolling horizon has to fill before a rate exists at all.
    assert counts["rate_unavailable"] > 0
    assert counts["steady"] > 0
    assert counts["transient"] > 0
    # The ramp is the transient half.
    assert (labels[-10:] == "transient").all()


def test_calibration_stratum_names_the_revision_or_says_unknown() -> None:
    frame = pd.concat([_frame(), _frame().assign(**{SESSION_COLUMN: "unparseable"})])
    revisions = {"2026-09-18T120000": "ZS44A-k106"}
    labels = regime_labels(frame, None, revision_of_piece=revisions.get)["calibration"]
    assert set(labels) == {"ZS44A-k106", "unknown"}


def test_breakdown_reports_only_groups_the_model_could_speak_for() -> None:
    corpus = behavior_corpus()
    model = fit_recipe(behavior_recipe(behavior_criteria()), corpus).model
    assert model is not None
    ev = evaluate_rows(model, corpus)
    labels = np.where(corpus["obd:ENGINE_LOAD"].to_numpy() < 50.0, "low", "high")
    metrics = breakdown_by_labels(model, ev, labels, drive_of_piece={}, policy=EventPolicy())
    assert set(metrics) == {"low", "high"}
    assert sum(m.evaluated for m in metrics.values()) == int(ev.evaluated.sum())
    # A label with no eligible row is absent rather than reported as zero error.
    none_eligible = np.full(len(corpus), "nowhere", dtype=object)
    none_eligible[ev.eligible] = "somewhere"
    assert set(
        breakdown_by_labels(model, ev, none_eligible, drive_of_piece={}, policy=EventPolicy())
    ) == {"somewhere"}


def test_profile_declares_the_strata_the_report_quotes() -> None:
    document = load_validation_document(BEHAVIOR_PROFILE_CRITERIA)
    assert document is not None
    regimes = document.report_regimes
    assert regimes is not None
    assert set(regimes.axes) == {"intake_temp", "coolant", "load", "baro"}
    assert regimes.steady_state is not None
    assert set(regimes.steady_state.max_abs_rate_per_second) == {
        "obd:RPM",
        "obd:ENGINE_LOAD",
    }


def test_edges_must_increase() -> None:
    with pytest.raises(ValueError, match="edges must strictly increase"):
        ReportRegimes.model_validate(
            {"axes": {"bad": {"channel": "obd:RPM", "unit": "rpm", "edges": [100, 100]}}}
        )
