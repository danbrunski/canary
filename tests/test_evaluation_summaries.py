"""The evaluation's summaries, report and per-drive records, tested at their owners (R15).

Moved from ``tests/test_evaluate_detectors_script.py`` when the library left the
script: recall and false alarms over independent drives
(:mod:`src.models.evaluation_summaries`), the label-aware drive record
(:mod:`src.analysis.detector_evaluation_drives`) and the Markdown renderer
(:mod:`src.models.evaluation_report`). The script re-exports the same objects
(``tests/test_detector_evaluation_boundaries.py``); the end-to-end run stays in
``tests/test_evaluate_detectors_script.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis.detector_evaluation_drives import clean_drive_record
from src.analysis.labelled_events import LabelledEvent
from src.models.evaluation import SessionSpan, group_into_drives
from src.models.evaluation_records import DriveRecord, SessionScore
from src.models.evaluation_report import render_markdown
from src.models.evaluation_summaries import (
    compact_injections,
    summarize_false_alarms,
    summarize_recall,
)
from tests.configs import example_config

EXAMPLE_YAML = Path(__file__).resolve().parents[1] / "canary.example.yaml"


def test_recall_excludes_withheld_outcomes_from_its_denominator() -> None:
    outcomes = [
        {"drive": "d1", "kind": "k", "severity": 1.0, "detector": "x", "status": "detected",
         "time_to_detect_seconds": 4.0},
        {"drive": "d1", "kind": "k", "severity": 1.0, "detector": "x", "status": "missed",
         "time_to_detect_seconds": None},
        {"drive": "d2", "kind": "k", "severity": 1.0, "detector": "x", "status": "withheld",
         "time_to_detect_seconds": None},
    ]  # fmt: skip
    block = summarize_recall(outcomes, ["x"], seed=0)["x"]["k"]["1.0"]
    assert block["graded"] == 2 and block["withheld"] == 1
    assert block["recall"]["estimate"] == pytest.approx(0.5)
    assert block["median_time_to_detect_seconds"] == 4.0


def test_only_fault_labels_take_episodes_out_of_the_false_alarms() -> None:
    stamps = pd.Series(pd.date_range("2026-09-18T12:00:00Z", periods=60, freq="1s"))
    frame = pd.DataFrame({"timestamp": stamps, "obd:RPM": 800.0})
    alerting = np.zeros(60, dtype=bool)
    alerting[5:8] = True  # inside the label
    alerting[40:43] = True  # outside it
    score = SessionScore(stamps, np.full(60, True, dtype=object), np.zeros(60), alerting)
    span = SessionSpan("s", stamps.iloc[0].to_pydatetime(), stamps.iloc[-1].to_pydatetime())
    (drive,) = group_into_drives([span])
    label = LabelledEvent(
        id="altitude",
        kind="novelty",
        title="t",
        start=datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC),
        end=datetime(2026, 9, 18, 12, 0, 20, tzinfo=UTC),
        end_basis="measured",
        provenance="test",
    )
    record = clean_drive_record(drive, {"s": frame}, {"s": {"x": score}}, ["x"], [label])
    block = record.detectors["x"]
    assert block["episodes"] == 2
    assert block["labelled_episodes"] == {"novelty": 1}
    # An altitude alert is explained, not excused: still a false alarm.
    assert block["false_alarm_episodes"] == 2
    assert record.labels == ["altitude"]

    fault = label.model_copy(update={"id": "leak", "kind": "fault"})
    record = clean_drive_record(drive, {"s": frame}, {"s": {"x": score}}, ["x"], [fault])
    assert record.detectors["x"]["false_alarm_episodes"] == 1


def test_markdown_renders_a_minimal_report() -> None:
    report = {
        "generated_at": "2026-09-22T00:00:00Z",
        "detectors": ["x"],
        "artifacts": {},
        "persistence": {},
        "budget_per_engine_hour": 1.0,
        "sequence_parity": None,
        "training_eras": [],
        "drives": [],
        "false_alarms": {
            "x": {
                "false_alarms_per_engine_hour": {"estimate": None, "note": "withheld"},
                "withheld_share": None,
                "strata": {"baro": {}},
            }
        },
        "recall": {},
        "vus_pr": {},
        "events": [{"id": "e", "kind": "novelty", "start": "a", "end": "b", "status": "skipped"}],
        "skipped_drives": [{"drive": "d", "sessions": ["s"], "reason": "skipped: seen"}],
    }
    text = render_markdown(report)
    assert "| x | withheld |" in text
    assert "- d (s): skipped: seen" in text
    assert "## Not loaded" not in text


def test_a_baseline_session_that_did_not_load_is_named_not_counted() -> None:
    """A02.11 verifier: the report named a threshold session it never loaded.
    What the baselines rest on and what failed to load are separate lines."""
    report = {
        "generated_at": "2026-09-23T00:00:00Z",
        "detectors": [],
        "artifacts": {},
        "persistence": {},
        "budget_per_engine_hour": 1.0,
        "sequence_parity": None,
        "training_eras": [],
        "drives": [],
        "false_alarms": {},
        "recall": {},
        "vus_pr": {},
        "events": [],
        "skipped_drives": [],
        "baseline_threshold_sessions": ["a"],
        "baseline_threshold_sessions_not_loaded": ["b"],
        "load_errors": {"b": "not decoded yet (--cache-only)"},
    }
    text = render_markdown(report)
    assert "Baseline threshold sessions: ['a'] (requested but not loaded: ['b'])" in text
    assert "## Not loaded" in text
    assert "- b: not decoded yet (--cache-only)" in text


def test_each_injection_is_recorded_once_with_every_detectors_outcome() -> None:
    injection = {"kind": "k", "severity": 1.0, "perturbed": ["obd:MAF"]}
    base = {"drive": "d", "session": "s", "kind": "k", "severity": 1.0, "rep": 0}
    compact = compact_injections(
        [
            {**base, "detector": "a", "status": "detected", "time_to_detect_seconds": 3.0,
             "injection": injection},
            {**base, "detector": "b", "status": "missed", "time_to_detect_seconds": None,
             "injection": injection},
        ]
    )  # fmt: skip
    assert len(compact) == 1
    assert compact[0]["perturbed"] == ["obd:MAF"]
    assert compact[0]["outcomes"] == {
        "a": {"status": "detected", "time_to_detect_seconds": 3.0},
        "b": {"status": "missed", "time_to_detect_seconds": None},
    }


def test_evidence_role_detectors_are_not_held_to_the_alert_budget() -> None:
    """A06.5: an evidence-role detector still reports its episodes per drive,
    but only alert-role detectors count against the budget."""
    from src.models.evaluation_detectors import detector_roles

    config = example_config(EXAMPLE_YAML)
    config.models.roles.isolation_forest = "alert"  # one of each role
    config.models.roles.sequence = "evidence"
    roles = detector_roles(config, ["isolation_forest", "sequence", "sequence_global", "fused"])
    assert roles == {
        "isolation_forest": "alert",
        "sequence": "evidence",
        "sequence_global": "evidence",
        "fused": "alert",
    }

    stamps = pd.Series(pd.date_range("2026-09-18T12:00:00Z", periods=2, freq="1s"))
    span = SessionSpan("s", stamps.iloc[0].to_pydatetime(), stamps.iloc[-1].to_pydatetime())
    (drive,) = group_into_drives([span])
    block = {"rows": 10, "withheld_rows": 0, "false_alarm_episodes": 4}
    record = DriveRecord(drive, ["s"], rows=10, hours=2.0)
    record.detectors = {"isolation_forest": dict(block), "sequence": dict(block)}
    summary = summarize_false_alarms(
        [record], ["isolation_forest", "sequence"], seed=0, roles=roles
    )
    assert summary["isolation_forest"]["false_alarms_per_engine_hour"]["estimate"] == 2.0
    assert summary["sequence"]["role"] == "evidence"
    assert summary["sequence"]["false_alarms_per_engine_hour"]["estimate"] is None
