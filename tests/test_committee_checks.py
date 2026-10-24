"""Sensor committees as detectors (H04.1-H04.4).

Relations are profile data (``data/<profile>/committees.yaml``); the evaluator
turns a sustained departure from them into a flag, and says which kind of
no-verdict every silent tick was. The ground-truth drive slice and the
synthetic 25 % VSS drop pin the speed committee's two acceptance claims.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger, read_events_dir
from src.alerts.events import AnomalyEvent
from src.decode.bindings import load_bindings
from src.signals.committees import (
    ACCEL_SPAN_SECONDS,
    COMMITTEES,
    GPS_SPEED_ACCEL_G,
    LONGITUDINAL_ACCEL_COMMITTEE,
    NO_VERDICT_REASONS,
    OBD_SPEED_ACCEL_G,
    SPEED_COMMITTEE,
    STANDARD_GRAVITY,
    CommitteeEvaluator,
    CommitteeFlag,
    CommitteeProfile,
    append_committee_member_transforms,
    append_committee_residuals,
    committee_channel_reason,
    evaluate_committee_frame,
    load_committee_profile,
    physical_committee_point,
    speed_accel_g,
)

REPO = Path(__file__).resolve().parents[1]
XTERRA_COMMITTEES = REPO / "data" / "nissan-xterra-2008" / "committees.yaml"
GROUND_TRUTH_SLICE = REPO / "tests" / "fixtures" / "ground_truth" / "drive-slice"
RATIO = 0.9203
T0 = datetime(2026, 9, 25, 12, tzinfo=UTC)

_GOOD_FIX = {"gps:num_satellites": 10.0, "gps:hdop": 0.9}


def _profile(**tolerance: Any) -> CommitteeProfile:
    spec: dict[str, Any] = {
        "kind": "relative",
        "max_departure": 0.15,
        "sustain_seconds": 10,
        "min_verdicts": 5,
        "max_verdict_gap_seconds": 3,
        "conditions": [
            {"channel": "gps:speed", "min": 20, "reason": "slow"},
            {"channel": GPS_SPEED_ACCEL_G, "max_abs": 0.05, "reason": "accelerating"},
        ],
        "evidence": "synthetic",
    }
    spec.update(tolerance)
    return CommitteeProfile.model_validate(
        {
            "schema_version": 1,
            "committees": {
                SPEED_COMMITTEE.name: {
                    "reference": "gps:speed",
                    "relations": [
                        {"member": "obd:SPEED", "ratio": RATIO, "evidence": "synthetic"},
                        {
                            "member": "decoded:WHEEL_SPEED_6F7_A",
                            "ratio": RATIO,
                            "evidence": "synthetic",
                        },
                    ],
                    "tolerance": spec,
                }
            },
        }
    )


def _point(gps: float, obd: float | None, **extra: Any) -> dict[str, Any]:
    point: dict[str, Any] = {"gps:speed": gps, GPS_SPEED_ACCEL_G: 0.0, **_GOOD_FIX}
    if obd is not None:
        point["obd:SPEED"] = obd
    point.update(extra)
    return point


def _run(
    evaluator: CommitteeEvaluator, points: Sequence[Mapping[str, Any]], start: float = 0.0
) -> list[CommitteeFlag]:
    flags: list[CommitteeFlag] = []
    base = T0.timestamp() + start
    for i, point in enumerate(points):
        flags.extend(evaluator.observe(point, base + i))
    return flags


def _speed_state(evaluator: CommitteeEvaluator) -> dict[str, Any]:
    state: dict[str, Any] = evaluator.snapshot()["committees"][SPEED_COMMITTEE.name]
    return state


# ---------------------------------------------------------------------------
# H04.1 — relations are profile data; a committee without one is named
# ---------------------------------------------------------------------------


def test_the_xterra_profile_loads_and_configures_only_the_speed_committee() -> None:
    profile = load_committee_profile(XTERRA_COMMITTEES)
    assert set(profile.committees) == {SPEED_COMMITTEE.name}
    spec = profile.committees[SPEED_COMMITTEE.name]
    assert spec.reference == "gps:speed"
    assert [r.ratio for r in spec.relations] == [RATIO, RATIO]
    assert all(r.evidence for r in spec.relations)
    assert spec.tolerance.conditions, "the tolerance states what it was measured under"


def test_no_committee_data_runs_every_committee_as_not_configured_named() -> None:
    evaluator = CommitteeEvaluator(None, profile_path="data/x/committees.yaml")
    flags = evaluator.observe(_point(80.0, 10.0), T0.timestamp())
    assert flags == [], "never run with defaults: a known offset would fire every drive"
    snapshot = evaluator.snapshot()["committees"]
    assert set(snapshot) == {c.name for c in COMMITTEES}
    for name, state in snapshot.items():
        assert state["state"] == "no_verdict"
        assert state["reason"] == "not_configured"
        assert state["configured"] is False
        assert name in state["detail"] and "data/x/committees.yaml" in state["detail"]


def test_a_partial_profile_leaves_the_rest_not_configured() -> None:
    evaluator = CommitteeEvaluator(_profile())
    evaluator.observe(_point(80.0, RATIO * 80.0), T0.timestamp())
    snapshot = evaluator.snapshot()["committees"]
    assert snapshot[SPEED_COMMITTEE.name]["state"] == "agree"
    assert snapshot[LONGITUDINAL_ACCEL_COMMITTEE.name]["reason"] == "not_configured"


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"committees": {"NOT_A_COMMITTEE": {}}}, "not a declared committee"),
        (
            {"reference": "gps:altitude"},
            "not members",
        ),
        ({"tolerance_drop": "conditions"}, "conditions"),
        ({"condition": {"channel": "gps:speed", "reason": "no bound"}}, "states no bound"),
        ({"relation_drop": "evidence"}, "evidence"),
    ],
)
def test_a_malformed_profile_is_refused(patch: dict[str, Any], match: str) -> None:
    raw = _profile().model_dump(mode="json")
    spec = raw["committees"][SPEED_COMMITTEE.name]
    if "committees" in patch:
        raw["committees"] = {"NOT_A_COMMITTEE": spec}
    if "reference" in patch:
        spec["reference"] = patch["reference"]
    if "tolerance_drop" in patch:
        del spec["tolerance"][patch["tolerance_drop"]]
    if "condition" in patch:
        spec["tolerance"]["conditions"] = [patch["condition"]]
    if "relation_drop" in patch:
        del spec["relations"][0][patch["relation_drop"]]
    with pytest.raises(ValidationError, match=match):
        CommitteeProfile.model_validate(raw)


def test_every_committee_channel_is_refused_as_a_feature() -> None:
    from src.trainability import source_exclusion_reason

    channels = [c.output for c in COMMITTEES] + [OBD_SPEED_ACCEL_G, GPS_SPEED_ACCEL_G]
    for channel in channels:
        assert committee_channel_reason(channel) is not None
        for column in (channel, f"{channel}__mean", f"{channel}__roc"):
            reason = source_exclusion_reason(column)
            assert reason is not None and "committee" in reason, column
    # OBD_SPEED_ACCEL_G reads only ECU data; the refusal is by name, not source.
    assert "detectors, not features" in (source_exclusion_reason(OBD_SPEED_ACCEL_G) or "")


# ---------------------------------------------------------------------------
# H04.2 — verdicts, debounce, events
# ---------------------------------------------------------------------------


def test_agree_on_the_known_relation() -> None:
    evaluator = CommitteeEvaluator(_profile())
    flags = _run(evaluator, [_point(80.0, RATIO * 80.0 * 1.05)] * 30)
    assert flags == []
    state = _speed_state(evaluator)
    assert state["state"] == "agree"
    assert state["counts"] == {"agree": 30}
    obd = state["relations"][0]
    assert obd["member"] == "obd:SPEED"
    assert obd["departure"] == pytest.approx(0.05)


def test_sustained_disagreement_fires_once_and_rearms_after_agreement() -> None:
    evaluator = CommitteeEvaluator(_profile(), session="s1")
    evaluator.mode = "highway"
    bad = _point(80.0, RATIO * 80.0 * 0.75)
    good = _point(80.0, RATIO * 80.0)
    flags = _run(evaluator, [bad] * 30)
    assert len(flags) == 1, "one flag per episode, not one per tick"
    flag = flags[0]
    assert flag.detected_at - flag.started_at == pytest.approx(10.0)
    assert flag.member == "obd:SPEED" and flag.reference == "gps:speed"
    assert flag.departure == pytest.approx(-0.25)
    assert flag.detector == f"committee:{SPEED_COMMITTEE.name}"
    assert flag.session == "s1" and flag.operating_mode == "highway"
    assert "obd:SPEED reads 55.22" in flag.text and "0.9203 x gps:speed" in flag.text
    assert _speed_state(evaluator)["alerting"] is True
    # Agreement ends the episode; a new disagreement is a new flag.
    assert _run(evaluator, [good] * 3, start=30) == []
    assert _speed_state(evaluator)["alerting"] is False
    assert len(_run(evaluator, [bad] * 12, start=33)) == 1


def test_isolated_departures_do_not_fire() -> None:
    evaluator = CommitteeEvaluator(_profile())
    bad = _point(80.0, RATIO * 80.0 * 0.5)
    good = _point(80.0, RATIO * 80.0)
    assert _run(evaluator, ([bad] * 4 + [good]) * 20) == []
    assert _speed_state(evaluator)["counts"]["disagree"] == 80


def test_a_verdict_gap_longer_than_allowed_restarts_the_run() -> None:
    evaluator = CommitteeEvaluator(_profile())
    bad = _point(80.0, RATIO * 80.0 * 0.5)
    slow = _point(10.0, 5.0)  # below the 20 km/h condition: no verdict
    # 8 s departed, 4 s without a verdict (> 3 s), 8 s departed: never 10 s running.
    assert _run(evaluator, [bad] * 8 + [slow] * 4 + [bad] * 8) == []
    # The same with a 2 s hole is one continuous run.
    evaluator = CommitteeEvaluator(_profile())
    assert len(_run(evaluator, [bad] * 8 + [slow] * 2 + [bad] * 8)) == 1


@pytest.mark.parametrize(
    ("point", "reason", "detail"),
    [
        ({**_GOOD_FIX, "obd:SPEED": 60.0}, "reference_missing", "gps:speed has no value"),
        (_point(80.0, None), "member_missing", "obd:SPEED has no value"),
        (
            _point(80.0, 73.6, **{"obd:SPEED__arrived": False}),
            "stale",
            "obd:SPEED did not arrive",
        ),
        (
            _point(80.0, 73.6, **{"gps:num_satellites": 5.0}),
            "gate",
            "gps:num_satellites 5 < 7",
        ),
        ({"gps:speed": 80.0, "obd:SPEED": 73.6, GPS_SPEED_ACCEL_G: 0.0}, "gate", "no value"),
        (_point(12.0, 11.0), "condition", "gps:speed 12 < 20 (slow)"),
        (
            _point(80.0, 73.6, **{GPS_SPEED_ACCEL_G: -0.2}),
            "condition",
            "0.2 > 0.05 (accelerating)",
        ),
    ],
    ids=["reference_missing", "member_missing", "stale", "gate", "gate_absent", "slow", "accel"],
)
def test_each_no_verdict_reason_is_named(point: dict[str, Any], reason: str, detail: str) -> None:
    assert reason in NO_VERDICT_REASONS
    evaluator = CommitteeEvaluator(_profile())
    assert evaluator.observe(point, T0.timestamp()) == []
    state = _speed_state(evaluator)
    assert state["state"] == "no_verdict"
    assert state["reason"] == reason
    assert detail in state["detail"]
    assert state["counts"] == {f"no_verdict:{reason}": 1}


def test_either_ecu_member_gives_a_verdict() -> None:
    """``tuning`` polls no speed PID: the broadcast copy carries the committee."""
    evaluator = CommitteeEvaluator(_profile())
    point = _point(80.0, None, **{"decoded:WHEEL_SPEED_6F7_A": RATIO * 80.0 * 0.75})
    flags = _run(evaluator, [point] * 12)
    assert [f.member for f in flags] == ["decoded:WHEEL_SPEED_6F7_A"]


def test_a_repeated_timestamp_is_not_a_new_verdict() -> None:
    evaluator = CommitteeEvaluator(_profile())
    evaluator.observe(_point(80.0, RATIO * 80.0), T0.timestamp())
    evaluator.observe(_point(80.0, RATIO * 80.0), T0.timestamp())
    assert _speed_state(evaluator)["counts"] == {"agree": 1}


def test_reset_drops_runs_across_sessions() -> None:
    evaluator = CommitteeEvaluator(_profile(), session="a")
    bad = _point(80.0, RATIO * 80.0 * 0.5)
    _run(evaluator, [bad] * 8)
    evaluator.reset("b")
    assert _run(evaluator, [bad] * 8, start=100) == [], "8 s in each session, never 10"


def test_physical_committee_point_scales_decoded_members() -> None:
    bindings = load_bindings(REPO / "data" / "bindings.yaml")
    point = physical_committee_point({"decoded:WHEEL_SPEED_6F7_A": 1600.0}, bindings)
    assert point["decoded:WHEEL_SPEED_6F7_A"] == pytest.approx(100.0)  # 1/16 km/h


def _flag() -> CommitteeFlag:
    evaluator = CommitteeEvaluator(_profile(), session="2026-09-25T120000")
    flags = _run(evaluator, [_point(80.0, RATIO * 80.0 * 0.7)] * 11)
    assert len(flags) == 1
    return flags[0]


def test_the_event_names_both_members_the_relation_and_the_departure() -> None:
    event = AnomalyEvent.from_committee(_flag())
    assert event.source == "committee"
    assert event.model_version == f"committee/{SPEED_COMMITTEE.name}"
    assert event.detail["detector"] == f"committee:{SPEED_COMMITTEE.name}"
    assert event.affected_channels == ("obd:SPEED", "gps:speed")
    assert event.detail["relation"]["ratio"] == RATIO
    assert event.detail["relation"]["evidence"] == "synthetic"
    assert event.detail["departure"] == pytest.approx(-0.3)
    assert event.score == pytest.approx(0.3 / 0.15) and event.threshold == 1.0
    assert event.detail["cause"].startswith(f"{SPEED_COMMITTEE.name}: obd:SPEED reads")
    assert event.session == "2026-09-25T120000"
    assert AnomalyEvent.from_dict(event.to_dict()) == event


def test_the_emitter_logs_committee_events_with_roles_and_dedupe(tmp_path: Path) -> None:
    emitter = AnomalyEventEmitter(
        AnomalyEventLogger(tmp_path), roles={"committee": "evidence"}, min_seconds_between_events=5
    )
    flag = _flag()
    first = emitter.emit_committee(flag)
    assert first is not None and first.role == "evidence"
    assert emitter.emit_committee(flag) is None, "same committee+member inside the window"
    assert emitter.counters.suppressed_dedupe == 1
    assert [e.source for e in read_events_dir(tmp_path)] == ["committee"]


# ---------------------------------------------------------------------------
# H04.3 — the speed committee: synthetic drop, and the ground-truth drive
# ---------------------------------------------------------------------------


def _cruise_frame(seconds: int, drop_at: int | None) -> pd.DataFrame:
    """A 1 Hz cruise at 90 km/h GPS with the ECU on its known relation."""
    stamps = pd.date_range(T0, periods=seconds, freq="s")
    gps = np.full(seconds, 90.0)
    obd = RATIO * gps
    if drop_at is not None:
        obd[drop_at:] *= 0.75
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "gps:speed": gps,
            "obd:SPEED": np.round(obd),  # the PID is whole km/h
            "gps:num_satellites": 10.0,
            "gps:hdop": 0.9,
            "gps:speed__arrived": True,
            "obd:SPEED__arrived": True,
        }
    )


def test_a_25_percent_vss_drop_fires_within_the_debounce_window() -> None:
    profile = load_committee_profile(XTERRA_COMMITTEES)
    assert evaluate_committee_frame(_cruise_frame(120, None), profile).flags == []
    result = evaluate_committee_frame(_cruise_frame(120, drop_at=60), profile)
    assert len(result.flags) == 1
    flag = result.flags[0]
    onset = (T0 + timedelta(seconds=60)).timestamp()
    sustain = profile.committees[SPEED_COMMITTEE.name].tolerance.sustain_seconds
    # The step itself reads as a hard deceleration on the ECU derivative, so
    # the first ~2 ticks after it are `condition` no-verdicts; then the run.
    assert onset <= flag.started_at <= onset + ACCEL_SPAN_SECONDS + 1
    assert flag.detected_at - flag.started_at == pytest.approx(sustain)
    assert flag.detected_at - onset <= sustain + ACCEL_SPAN_SECONDS + 1
    assert flag.departure == pytest.approx(-0.25, abs=0.01)


def _launch(profile: CommitteeProfile) -> list[CommitteeFlag]:
    """Pulling away at 30-40 km/h, GPS 3-5 s behind the wheels
    (_capture-tuning-2026-09-22T235224, 00:09:12-00:09:22Z): the wheels read
    0.13 g, the lagging GPS derivative stays under 0.05 g, and the ratio sits
    +16.6 % off its relation for 15 s."""
    evaluator = CommitteeEvaluator(profile)
    gps = 30.0
    point = {
        "gps:speed": gps,
        "decoded:WHEEL_SPEED_6F7_A": RATIO * gps * 1.166,
        GPS_SPEED_ACCEL_G: 0.03,
        OBD_SPEED_ACCEL_G: 0.13,
        **_GOOD_FIX,
    }
    return _run(evaluator, [point] * 15)


def test_a_launch_with_gps_lagging_the_wheels_is_not_an_event() -> None:
    profile = load_committee_profile(XTERRA_COMMITTEES)
    assert _launch(profile) == []
    # ...and it is the ECU-acceleration condition that stops it.
    raw = profile.model_dump(mode="json")
    conditions = raw["committees"][SPEED_COMMITTEE.name]["tolerance"]["conditions"]
    raw["committees"][SPEED_COMMITTEE.name]["tolerance"]["conditions"] = [
        c for c in conditions if c["channel"] != OBD_SPEED_ACCEL_G
    ]
    assert len(_launch(CommitteeProfile.model_validate(raw))) == 1


def _ground_truth_frame() -> pd.DataFrame:
    from src.config import CanaryConfig
    from src.preprocessing.loader import resample_and_segment

    config = CanaryConfig.from_yaml(REPO / "canary.example.yaml")
    return resample_and_segment([GROUND_TRUTH_SLICE], config)


def test_the_ground_truth_slice_gives_no_event_and_says_why() -> None:
    """The slice was cut without the fix-health channels (its source session,
    2026-08-03T163440, recorded them), so as committed every tick fails the
    satellite gate closed: zero events, and the counts say which kind of zero."""
    frame = _ground_truth_frame()
    profile = load_committee_profile(XTERRA_COMMITTEES)
    result = evaluate_committee_frame(frame, profile)
    assert result.flags == []
    counts = result.counts[SPEED_COMMITTEE.name]
    assert "agree" not in counts and "disagree" not in counts
    assert counts.get("no_verdict:gate", 0) > 0


def test_the_ground_truth_slice_agrees_when_its_fix_is_trusted() -> None:
    """Same slice, fix-health asserted good (satellites/HDOP are the only thing
    stripped from it): real verdicts, and still no event."""
    frame = _ground_truth_frame().assign(**{"gps:num_satellites": 10.0, "gps:hdop": 0.9})
    profile = load_committee_profile(XTERRA_COMMITTEES)
    result = evaluate_committee_frame(frame, profile)
    assert result.flags == []
    counts = result.counts[SPEED_COMMITTEE.name]
    assert counts.get("agree", 0) >= 50
    assert counts.get("agree", 0) > 5 * counts.get("disagree", 0)


# ---------------------------------------------------------------------------
# H04.4 — longitudinal-acceleration members from existing channels
# ---------------------------------------------------------------------------


def test_speed_accel_g_recovers_a_known_constant_acceleration() -> None:
    stamps = pd.Series(pd.date_range(T0, periods=20, freq="s"))
    accel_ms2 = 1.5
    speed_kmh = pd.Series(10.0 + 3.6 * accel_ms2 * np.arange(20))
    accel = speed_accel_g(speed_kmh, stamps)
    assert accel.iloc[:2].isna().all(), "no 2 s history yet"
    assert np.allclose(accel.iloc[2:], accel_ms2 / STANDARD_GRAVITY)


def test_speed_accel_g_uses_real_elapsed_time_and_refuses_holes() -> None:
    offsets = [0.0, 1.0, 2.5, 3.0, 4.0, 12.0, 13.0, 14.0]
    stamps = pd.Series([T0 + timedelta(seconds=s) for s in offsets])
    speed = pd.Series([3.6 * 2.0 * s for s in offsets])  # 2 m/s² throughout
    accel = speed_accel_g(speed, stamps)
    expected = 2.0 / STANDARD_GRAVITY
    assert accel.iloc[2] == pytest.approx(expected)  # 2.5 s span, not "2 rows"
    assert pd.isna(accel.iloc[5]), "an 8 s hole is not a rate"
    assert accel.iloc[7] == pytest.approx(expected)


def test_member_transforms_prefer_the_100hz_broadcast_speed() -> None:
    stamps = pd.date_range(T0, periods=10, freq="s")
    frame = pd.DataFrame(
        {
            "timestamp": stamps,
            # 0x2D1 counts at 0.1 km/h: 36 counts/s = 3.6 km/h/s = 1 m/s².
            "decoded:VEHICLE_SPEED_2D1": 36.0 * np.arange(10),
            "obd:SPEED": np.zeros(10),  # would read 0 g if it were used
            "gps:speed": 3.6 * 0.5 * np.arange(10),
        }
    )
    out = append_committee_member_transforms(frame)
    assert np.allclose(out[OBD_SPEED_ACCEL_G].iloc[2:], 1.0 / STANDARD_GRAVITY)
    assert np.allclose(out[GPS_SPEED_ACCEL_G].iloc[2:], 0.5 / STANDARD_GRAVITY)


def test_member_transforms_need_a_timestamp_and_a_source() -> None:
    no_time = pd.DataFrame({"obd:SPEED": [10.0, 20.0]})
    assert list(append_committee_member_transforms(no_time).columns) == ["obd:SPEED"]
    stamps = pd.date_range(T0, periods=3, freq="s")
    no_speed = pd.DataFrame({"timestamp": stamps, "obd:RPM": [800.0] * 3})
    assert OBD_SPEED_ACCEL_G not in append_committee_member_transforms(no_speed).columns


def test_residuals_compare_decoded_members_in_engineering_units() -> None:
    frame = pd.DataFrame(
        [{"decoded:WHEEL_SPEED_6F7_A": 1600.0, "gps:speed": 104.0, **_GOOD_FIX}]
    )  # 1600 counts = 100 km/h
    out = append_committee_residuals(frame, committees=[SPEED_COMMITTEE])
    assert out[SPEED_COMMITTEE.output].iloc[0] == pytest.approx(4.0)
