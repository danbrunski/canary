"""R03.9: the deterministic replay tick driver and its event diff.

``tools/replay_event_diff.py`` replays a stored session through
``LiveInferencePipeline`` on the session's own clock so a before/after
refactor diff compares detector output, not timing. These tests pin the two
properties that make the diff meaningful: the same session replayed twice
yields identical events and scores, and the diff separates a categorical
change from float noise.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.config import CanaryConfig
from src.validation.harness import StubIsolationForest, StubModelManager
from tools.replay_event_diff import compare_values, diff_runs, drive_session

DRIVE_SLICE = Path("tests/fixtures/ground_truth/drive-slice")
#: RPM above which the stub scores anomalous: the slice's first minute holds
#: both sides of it, so the persistence gate sees runs of firing and quiet.
FIRE_RPM = 1400.0


@dataclass
class _RpmStub(StubIsolationForest):
    """Scores ``obd:RPM / 1000``; calibrated anomaly above ``FIRE_RPM``."""

    def score(self, frame: Any) -> Any:
        out = super().score(frame)
        rpm = out["obd:RPM"].astype(float).fillna(0.0) if "obd:RPM" in out else 0.0
        out["isolation_forest_score"] = rpm / 1000.0
        out["isolation_forest_calibrated_threshold"] = FIRE_RPM / 1000.0
        out["isolation_forest_calibrated_anomaly"] = rpm >= FIRE_RPM
        out["isolation_forest_label"] = (rpm >= FIRE_RPM).map({True: -1, False: 1})
        return out


def _if_only(config: CanaryConfig) -> CanaryConfig:
    models = config.models.model_copy(
        update={
            "lstm_autoencoder": config.models.lstm_autoencoder.model_copy(update={"enabled": False})
        }
    )
    return config.model_copy(update={"models": models})


def _install_stub(components: Any) -> None:
    components.model_managers["isolation_forest"] = StubModelManager(_RpmStub())


def _replay(scratch: Path) -> dict[str, Any]:
    return asyncio.run(
        drive_session(
            DRIVE_SLICE,
            scratch=scratch,
            models_dir=scratch / "models",
            start_offset_seconds=0.0,
            seconds=30.0,
            configure=_if_only,
            prepare_components=_install_stub,
        )
    )


@pytest.mark.slow
def test_the_same_session_replays_to_identical_events_and_scores(tmp_path: Path) -> None:
    first = _replay(tmp_path / "a")
    second = _replay(tmp_path / "b")

    assert first["meta"]["ticks"] == 30
    assert first["scores"], "the stub IF published no scores"
    assert any(e["source"] == "isolation_forest" for e in first["events"]), first["counters"]
    reports = diff_runs(first, second, rel_tol=0.0, abs_tol=0.0)
    assert {name: r.differences for name, r in reports.items()} == {name: [] for name in reports}
    # Every tick is stamped in session time, never wall time.
    stamps = [s["timestamp"] for s in first["scores"] if s.get("timestamp")]
    assert stamps and all(stamp.startswith("2026-08-03T23:") for stamp in stamps)
    # The session clock is handed back: no src module keeps the patched time.
    patched = [
        name
        for name, module in list(sys.modules.items())
        if name.startswith("src.") and getattr(module, "time", time) is not time
    ]
    assert patched == []


def test_diff_is_exact_on_categories_and_tolerant_on_floats() -> None:
    before = {"source": "isolation_forest", "score": 0.1, "latency_ms": 3.0, "modes": ["city"]}

    assert compare_values(before, {**before, "score": 0.1 + 1e-12}).identical
    # Wall-clock latency is not detector output.
    assert compare_values(before, {**before, "latency_ms": 99.0}).identical

    score = compare_values(before, {**before, "score": 0.2})
    assert [d.path for d in score.differences] == ["score"]
    source = compare_values(before, {**before, "source": "fused"})
    assert [d.path for d in source.differences] == ["source"]
    modes = compare_values(before, {**before, "modes": ["city", "idle"]})
    assert [d.path for d in modes.differences] == ["modes[len]"]


@pytest.mark.parametrize("section", ["events", "scores", "counters"])
def test_diff_runs_reports_each_section(section: str) -> None:
    run = {"meta": {"tree": "a", "ticks": 2}, "events": [], "scores": [], "counters": {}}
    changed = {**run, "meta": {"tree": "b", "ticks": 2}, section: [{"x": 1}]}

    reports = diff_runs(run, changed)

    assert not reports[section].identical
    assert reports["meta"].identical, "the tree path is expected to differ"


def test_an_extra_record_is_reported_once_not_as_a_shift() -> None:
    scores = [
        {"timestamp": f"2026-09-18T21:38:{s:02d}Z", "source": "isolation_forest", "score": 0.1}
        for s in range(20, 25)
    ]
    extra = {"timestamp": "2026-09-18T21:38:19Z", "source": "isolation_forest", "score": 0.1}
    run = {"meta": {}, "events": [], "scores": scores, "counters": {}}

    report = diff_runs(run, {**run, "scores": [extra, *scores]})["scores"]

    assert report.only_before == []
    assert report.only_after == ["2026-09-18T21:38:19Z|isolation_forest|0"]
    assert len(report.differences) == 1
    assert report.differing_by_source == {}
    assert report.compared_by_source == {"isolation_forest": 5}
