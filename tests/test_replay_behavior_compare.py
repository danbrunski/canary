"""B03.7: replay a recording through the live behavior pipeline, served set vs candidate.

``tools/replay_behavior_compare.py`` is the behavior-event path for replay:
each side is one ``drive_session`` pass with the side's artifact staged under
an isolated ``_replay-*`` state directory. These tests pin what makes the
comparison honest: refusals are counted as refusals (never a zero residual),
the profile's policy governs a staged candidate, a capture the daemon would
not score is refused, and the same artifact on both sides compares as equal.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.behavior.behavior_models import load_behavior_models, serving_status
from src.paths import DATA_DIR
from src.storage.capture_manifest import CAPTURE_MANIFEST_FILENAME
from tools.replay_behavior_compare import (
    OFF_MODE,
    BehaviorReplayRecorder,
    ModelTally,
    ReplayCompareRefusedError,
    compare_on_session,
    compare_sides,
    paired_residuals,
    refuse_ineligible_session,
    stage_artifact,
)

PROFILE = "nissan-xterra-2008"
SERVED = DATA_DIR / PROFILE / "behavior_models.yaml"
DRIVE_SLICE = Path("tests/fixtures/ground_truth/drive-slice")


def _residual(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "name": "m",
        "mode_match": True,
        "reason": None,
        "deviation": 1.0,
        "severity": "ok",
        "alerting": False,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@dataclass(frozen=True)
class _ResidualRecord:
    name: str
    alerting: bool
    mode_match: bool = True
    reason: str | None = None
    deviation: float | None = 0.5
    severity: str = "ok"


def test_refused_and_off_mode_ticks_are_refusals_not_zero_residuals() -> None:
    tally = ModelTally()
    tally.observe(_residual(mode_match=False, deviation=None), "t0")
    tally.observe(_residual(reason="off_map", deviation=None), "t1")
    tally.observe(_residual(reason="engine_state_stale:obd:RPM", deviation=None), "t2")
    tally.observe(_residual(deviation=2.0, severity="warning"), "t3")
    tally.observe(_residual(deviation=-4.0), "t4")

    summary = tally.summary()

    assert summary["ticks"] == 5
    assert summary["in_mode_ticks"] == 4
    assert summary["scored_ticks"] == 2
    assert summary["coverage"] == pytest.approx(0.5)
    assert summary["refusals"] == {
        OFF_MODE: 1,
        "engine_state_stale:obd:RPM": 1,
        "off_map": 1,
    }
    assert summary["deviation_by_tick"] == {"t3": 2.0, "t4": -4.0}
    assert summary["refusal_by_tick"]["t1"] == "off_map"
    assert summary["band_ticks"] == {"ok": 1, "warning": 1}
    assert summary["alerting_as_served"] is False
    # One side's own pooled residual statistics are not reported at all.
    assert "mae" not in summary and "bias" not in summary


def test_a_model_that_never_scored_has_no_paired_residuals() -> None:
    tally = ModelTally()
    tally.observe(_residual(reason="input_missing:did:ECM_RPM", deviation=None), "t0")
    summary = tally.summary()

    paired = paired_residuals(summary, summary)

    assert summary["coverage"] == 0.0
    assert paired["n_pairs"] == 0
    assert paired["mae"] == [None, None] and paired["mae_delta"] is None


def test_residuals_compare_only_on_ticks_both_sides_scored() -> None:
    """The VE shape from B03.7: the candidate's wider map scores ticks the
    served map refused off-map, where residuals are large. Pooled, the
    candidate's MAE would move by composition; paired, it does not move."""
    served, candidate = ModelTally(), ModelTally()
    for tick in ("t0", "t1", "t2", "t3"):
        served.observe(_residual(deviation=1.0), tick)
        candidate.observe(_residual(deviation=1.0), tick)
    for tick in ("t4", "t5"):
        served.observe(_residual(reason="off_map", deviation=None), tick)
        candidate.observe(_residual(deviation=9.0), tick)
    candidate.observe(_residual(reason="engine_state_stale:obd:RPM", deviation=None), "t6")
    served.observe(_residual(deviation=-3.0), "t6")

    paired = paired_residuals(served.summary(), candidate.summary())

    assert paired["n_pairs"] == 4
    assert paired["mae"] == [pytest.approx(1.0), pytest.approx(1.0)]
    assert paired["mae_delta"] == pytest.approx(0.0)
    assert paired["only_candidate_scored"]["ticks"] == 2
    assert paired["only_candidate_scored"]["other_side_refusals"] == {"off_map": 2}
    assert paired["only_candidate_scored"]["mae"] == pytest.approx(9.0)
    assert paired["only_served_scored"] == {
        "ticks": 1,
        "other_side_refusals": {"engine_state_stale:obd:RPM": 1},
        "mae": pytest.approx(3.0),
        "bias": pytest.approx(-3.0),
        "abs_deviation_p95": pytest.approx(3.0),
    }


def test_shadow_alerting_hands_every_residual_to_apply_as_alerting() -> None:
    seen: list[bool] = []

    class _Detector:
        def prepare(self, row: Any, ctx: Any) -> list[Any]:
            return [_ResidualRecord(name="m", alerting=False)]

        def apply(self, residuals: list[Any], ctx: Any) -> list[Any]:
            seen.extend(r.alerting for r in residuals)
            return [SimpleNamespace(name="behavior:m")]

    detector = _Detector()
    recorder = BehaviorReplayRecorder(shadow_alerting=True)
    recorder.install(SimpleNamespace(detectors=(None, None, detector)))

    ctx = SimpleNamespace(now=datetime(2026, 9, 22, 23, 39, tzinfo=UTC))
    detector.apply(detector.prepare(None, ctx), ctx)

    assert seen == [True]
    assert recorder.ticks == 1
    assert recorder.tick_stamps == ["2026-09-22T23:39:00+00:00"]
    assert recorder.models["m"].alerting_as_served is False, "recorded as served, not shadow"
    assert recorder.models["m"].fusion_votes == 1


def test_a_staged_candidate_is_governed_by_the_profile_policy(tmp_path: Path) -> None:
    staged = stage_artifact(SERVED, tmp_path / "_replay-x-candidate", PROFILE)

    models = load_behavior_models(staged)
    lifecycles = {m.name: serving_status(m).lifecycle for m in models.models}

    assert staged.parent == tmp_path / "_replay-x-candidate" / "models" / PROFILE
    # B03.3's demotion follows the artifact into the replay.
    assert lifecycles["timing_advance_vs_rpm_iat"] == "demoted"


def test_staging_that_would_shed_the_policy_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ReplayCompareRefusedError, match="tracked policy"):
        stage_artifact(SERVED, tmp_path / "_replay-x-candidate", "no-such-profile")


def test_a_capture_the_daemon_would_not_score_is_refused(tmp_path: Path) -> None:
    session = tmp_path / "_capture-tuning-2026-09-22T154716"
    session.mkdir()
    (session / CAPTURE_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "profile": "tuning",
                "started_at": "2026-09-22T15:47:16Z",
                "profile_snapshot": {"corpus_eligible": False},
            }
        )
    )

    with pytest.raises(ReplayCompareRefusedError, match="R03.12b"):
        refuse_ineligible_session(session)
    refuse_ineligible_session(DRIVE_SLICE)  # an ordinary session passes


TICKS = ["2026-09-22T23:39:00+00:00", "2026-09-22T23:39:01+00:00"]


def _side(fingerprint: str, **model: Any) -> dict[str, Any]:
    base = {
        "artifact": {"fingerprint": fingerprint, "version": 1},
        "serving_status": {"lifecycle": "unvalidated"},
        "alerting_as_served": False,
        "scored_ticks": 2,
        "coverage": 1.0,
        "persisted_events": 0,
        "fusion_votes": 0,
        "fused_events_contributed": 0,
        "refusals": {"off_map": 3},
        "calibration_context": "match",
        "deviation_by_tick": {TICKS[0]: 2.0, TICKS[1]: -2.0},
        "refusal_by_tick": {},
    }
    base.update(model)
    return base


def test_compare_sides_reports_deltas_presence_and_refusals() -> None:
    served = {"tick_stamps": TICKS, "models": {"a": _side("f1"), "gone": _side("g")}}
    candidate = {
        "tick_stamps": TICKS,
        "models": {
            "a": _side(
                "f2",
                deviation_by_tick={TICKS[0]: 1.0, TICKS[1]: -2.0},
                persisted_events=2,
                refusals={"engine_not_running": 1},
            ),
            "new": _side("n"),
        },
    }

    out = compare_sides(served, candidate)

    assert out["gone"] == {"presence": "served_only"}
    assert out["new"] == {"presence": "candidate_only"}
    assert out["a"]["same_artifact"] is False
    assert out["a"]["paired"]["n_pairs"] == 2
    assert out["a"]["paired"]["mae_delta"] == pytest.approx(-0.5)
    assert "mae" not in out["a"], "no unpaired residual aggregate is compared"
    assert out["a"]["persisted_events"] == [0, 2]
    assert out["a"]["refusals"] == {"engine_not_running": [0, 1], "off_map": [3, 0]}


def test_sides_that_graded_different_ticks_are_not_compared() -> None:
    """Same count, different instants: compared by timestamp, not by length."""
    served = {"tick_stamps": TICKS, "models": {"a": _side("f1")}}
    shifted = ["2026-09-22T23:39:00.500000+00:00", "2026-09-22T23:39:01.500000+00:00"]
    candidate = {"tick_stamps": shifted, "models": {"a": _side("f1")}}

    with pytest.raises(ReplayCompareRefusedError, match="different ticks"):
        compare_sides(served, candidate)


@pytest.mark.slow
def test_the_same_artifact_on_both_sides_replays_identically(tmp_path: Path) -> None:
    result = asyncio.run(
        compare_on_session(
            DRIVE_SLICE,
            served=SERVED,
            candidate=SERVED,
            out_dir=tmp_path,
            run_id="t",
            shadow_alerting=True,
            seconds=30.0,
        )
    )

    served, candidate = result["served"], result["candidate"]
    assert result["same_ticks"] and served["ticks"] == 30
    assert served["behavior_ticks"] == served["ticks"]
    # Every side's state is an isolated _replay-* directory under --out.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["_replay-t-candidate", "_replay-t-served"]
    assert served["governing_policy"] == str(DATA_DIR / PROFILE / "behavior_policy.yaml")
    assert served["models"].keys() == candidate["models"].keys()
    for name, entry in result["per_model"].items():
        assert entry["same_artifact"], name
        for key in ("scored_ticks", "persisted_events", "fusion_votes"):
            assert entry[key][0] == entry[key][1], (name, key)
        paired = entry["paired"]
        assert paired["n_pairs"] == entry["scored_ticks"][0], name
        assert paired["mae"][0] == paired["mae"][1], name
        assert paired["only_served_scored"]["ticks"] == 0, name
        assert paired["only_candidate_scored"]["ticks"] == 0, name
        assert entry["refusals"] == {
            reason: [count, count] for reason, (count, _) in entry["refusals"].items()
        }
    # The slice is a running engine: some model scored a residual.
    assert any(m["scored_ticks"] for m in served["models"].values())


def test_the_archived_summary_drops_per_tick_records_but_keeps_pairing() -> None:
    from tools.replay_behavior_compare import without_per_tick

    served = {"tick_stamps": TICKS, "models": {"a": _side("f1")}}
    result = {"served": served, "per_model": compare_sides(served, served)}

    slim = without_per_tick(result)

    assert "tick_stamps" not in slim["served"]
    assert "deviation_by_tick" not in slim["served"]["models"]["a"]
    assert slim["per_model"]["a"]["paired"]["n_pairs"] == 2
