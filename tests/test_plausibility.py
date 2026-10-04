"""A05 — deterministic plausibility checks: resolver, checks, confirmation,
evidence, the live hook and the offline raw-sample stream."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.config import CanaryConfig
from src.decode.bindings import Bindings, load_bindings
from src.decode.dids import load_did_map
from src.plausibility.evaluator import (
    EvidenceRecord,
    EvidenceStore,
    PlausibilityEvaluator,
    render_evidence,
)
from src.plausibility.limits import (
    PLAUSIBILITY_FILENAME,
    PlausibilityProfile,
    load_plausibility_profile,
    resolve_limits,
    resolve_limits_for_config,
)
from src.plausibility.offline import (
    SessionStream,
    detections_in,
    inject_plumbing_fault,
    run_evaluator,
)
from tests.configs import example_config

REPO = Path(__file__).resolve().parents[1]
XTERRA_PROFILE = REPO / "data" / "nissan-xterra-2008" / PLAUSIBILITY_FILENAME
MODES = ("koeo", "warmup", "idle", "city", "highway")


def _profile(**channels: Any) -> PlausibilityProfile:
    return PlausibilityProfile.model_validate(
        {
            "schema_version": 1,
            "freshness_k": 3,
            "tiers": {
                "fast": {"interval_seconds": 1.0, "reason": "test"},
                "medium": {"interval_seconds": 3.0, "reason": "test"},
                "broadcast": {"interval_seconds": 0.1, "reason": "test"},
            },
            "confirm": {
                check: {"window_seconds": 60, "min_separation_seconds": 1.0}
                for check in ("range", "stuck", "freshness", "slew")
            },
            "channels": channels,
        }
    )


def _bindings(*signals: dict[str, Any]) -> Bindings:
    return Bindings.model_validate({"schema_version": 1, "signals": list(signals)})


def _signal(name: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "module": "ECM",
        "can_id": 0x551,
        "byte_start": 0,
        "byte_length": 1,
        "confidence": "confirmed",
        **extra,
    }


# ---------------------------------------------------------------------------
# A05.1 — the resolver
# ---------------------------------------------------------------------------


def test_decoded_binding_limits_are_converted_to_the_raw_units_they_arrive_in() -> None:
    bindings = _bindings(
        _signal(
            "COOLANT",
            scale=1.0,
            offset=-40.0,
            unit="degC",
            plausible_min=-10.0,
            plausible_max=130.0,
        ),
        _signal("INVERTED", scale=-2.0, offset=100.0, plausible_min=0.0, plausible_max=50.0),
    )
    catalog = resolve_limits(
        None, bindings=bindings, include_j1979=False, include_physical_limits=False
    )
    coolant = catalog.get("decoded:COOLANT")
    assert coolant.minimum is not None and coolant.maximum is not None
    assert (coolant.minimum.value, coolant.maximum.value) == (30.0, 170.0)
    assert "bindings.yaml COOLANT" in coolant.maximum.source
    inverted = catalog.get("decoded:INVERTED")
    assert inverted.minimum is not None and inverted.maximum is not None
    # raw = (eng - 100) / -2: eng 50 -> raw 25 (low side), eng 0 -> raw 50.
    assert (inverted.minimum.value, inverted.maximum.value) == (25.0, 50.0)


def test_the_tightest_bound_wins_and_names_its_source() -> None:
    profile = _profile(**{"obd:RPM": {"range": {"max": 7000, "reason": "redline"}}})
    catalog = resolve_limits(profile)
    rpm = catalog.get("obd:RPM")
    assert rpm.maximum is not None and rpm.maximum.value == 7000
    assert "redline" in rpm.maximum.source
    assert rpm.minimum is not None and rpm.minimum.value == 0.0
    speed = catalog.get("obd:SPEED")
    assert speed.maximum is not None and speed.maximum.value == 255.0
    assert "J1979 PID 0x0D encoding range" in speed.maximum.source
    coolant = catalog.get("obd:COOLANT_TEMP")
    assert coolant.minimum is not None and coolant.minimum.value == -39.0
    assert "PHYSICAL_LIMITS" in coolant.minimum.source


def test_a_channel_with_no_stated_limit_reports_unchecked_not_ok() -> None:
    catalog = resolve_limits(_profile(), include_j1979=False, include_physical_limits=False)
    coverage = catalog.get("did:SOMETHING").coverage()
    assert coverage["range"] == "unchecked: no stated limit"
    assert coverage["slew"].startswith("unchecked")
    assert coverage["stuck"].startswith("unchecked")
    assert coverage["freshness"].startswith("unchecked")
    assert "ok" not in json.dumps(coverage)


def test_freshness_needs_a_tier_interval_from_the_profile() -> None:
    catalog = resolve_limits(_profile())
    rpm = catalog.get("obd:RPM")  # J1979 fast tier, profile states 1.0 s
    assert rpm.freshness_threshold_seconds == pytest.approx(3.0)
    assert rpm.coverage()["freshness"] == "checked"
    baro = catalog.get("obd:BAROMETRIC_PRESSURE")  # slow tier, no interval stated
    assert baro.coverage()["freshness"] == (
        f"unchecked: tier 'slow' has no interval in {PLAUSIBILITY_FILENAME}"
    )
    without = resolve_limits(None).get("obd:RPM")
    assert without.coverage()["freshness"].startswith("unchecked: no plausibility.yaml")


def test_profile_mode_names_must_exist_in_the_segmenter() -> None:
    profile = _profile(
        **{
            "obd:RPM": {
                "stuck": {"samples": 5, "modes": ["crusing"], "reason": "typo"},
            }
        }
    )
    with pytest.raises(ValueError, match="crusing"):
        resolve_limits(profile, operating_modes=MODES)


def test_an_unscaled_decoded_channel_refuses_an_engineering_rule() -> None:
    bindings = _bindings(_signal("RAWONLY"))
    profile = _profile(**{"decoded:RAWONLY": {"range": {"max": 10, "reason": "x"}}})
    with pytest.raises(ValueError, match="pinned binding scale"):
        resolve_limits(profile, bindings=bindings)


def test_profile_schema_refuses_missing_confirm_policy_and_unprefixed_channels() -> None:
    with pytest.raises(ValueError, match="confirm policy missing"):
        PlausibilityProfile.model_validate(
            {"schema_version": 1, "freshness_k": 3, "confirm": {"range": {"window_seconds": 5}}}
        )
    with pytest.raises(ValueError, match="source-prefixed"):
        _profile(RPM={"range": {"max": 1, "reason": "x"}})
    with pytest.raises(ValueError, match="needs min, max"):
        _profile(**{"obd:RPM": {"range": {"reason": "x"}}})


def test_resolving_for_a_config_refuses_without_the_vehicle_file(tmp_path: Path) -> None:
    config = example_config(REPO / "canary.example.yaml")
    daemon = config.daemon.model_copy(
        update={"afr_calibration": tmp_path / "vehicle" / "afr_calibration.yaml"}
    )
    config = config.model_copy(update={"daemon": daemon})
    with pytest.raises(FileNotFoundError, match="plausibility.yaml"):
        resolve_limits_for_config(config)


def test_the_xterra_profile_resolves_against_its_catalogs_and_modes() -> None:
    config = example_config(REPO / "canary.example.yaml")
    profile = load_plausibility_profile(XTERRA_PROFILE)
    catalog = resolve_limits(
        profile,
        bindings=load_bindings(REPO / "data" / "bindings.yaml"),
        did_map=load_did_map(REPO / "data" / "dids.yaml"),
        operating_modes=config.preprocessing.operating_modes,
        profile_path=XTERRA_PROFILE,
    )
    rpm = catalog.get("obd:RPM")
    assert rpm.coverage() == {
        "range": "checked",
        "slew": "checked",
        # Steady cruise holds engine speed exactly: no single-channel stuck rule.
        "stuck": "unchecked: no stuck rule (not expected to move in any stated mode)",
        "freshness": "checked",
    }
    assert catalog.get("obd:MAF").coverage()["stuck"] == "checked"
    # Coolant legitimately holds at warm idle: no mode where it must move.
    assert catalog.get("obd:COOLANT_TEMP").coverage()["stuck"].startswith("unchecked")
    # Vehicle numbers live in the vehicle file, not shared code.
    assert rpm.maximum is not None and PLAUSIBILITY_FILENAME in rpm.maximum.source


# ---------------------------------------------------------------------------
# A05.2–A05.7 — the evaluator
# ---------------------------------------------------------------------------


def _evaluator(profile: PlausibilityProfile, **kwargs: Any) -> PlausibilityEvaluator:
    return PlausibilityEvaluator(resolve_limits(profile), session="s1", **kwargs)


def test_range_detection_is_pending_then_confirmed_on_a_separated_repeat() -> None:
    written: list[EvidenceRecord] = []
    evaluator = _evaluator(_profile(), sink=written.append)
    evaluator.observe("obd:SPEED", 50.0, 100.0, "city")
    evaluator.observe("obd:SPEED", 300.0, 100.5, "city")  # above the 255 encoding max
    assert [r.state for r in written] == ["pending"]
    pending = written[0]
    assert (pending.channel, pending.check, pending.value, pending.limit) == (
        "obd:SPEED",
        "range",
        300.0,
        255.0,
    )
    assert pending.operating_mode == "city"
    assert pending.sample_times == ("1970-01-01T00:01:40.500000Z",)
    assert not pending.alerts
    evaluator.observe("obd:SPEED", 300.0, 100.9, "city")  # inside min_separation
    assert len(written) == 1
    evaluator.observe("obd:SPEED", 300.0, 102.0, "city")
    assert [r.state for r in written] == ["pending", "confirmed"]
    assert written[1].alerts and written[1].detections == 2
    assert written[1].detail["first_detected_at"] == pending.detected_at
    assert [r.channel for r in evaluator.confirmed()] == ["obd:SPEED"]


def test_a_pending_detection_expires_outside_its_window() -> None:
    written: list[EvidenceRecord] = []
    evaluator = _evaluator(_profile(), sink=written.append)
    evaluator.observe("obd:SPEED", 300.0, 0.0, "city")
    evaluator.observe("obd:SPEED", 300.0, 61.0, "city")
    assert [r.state for r in written] == ["pending", "pending"]
    assert evaluator.confirmed() == []


def test_range_check_runs_only_in_its_enabled_modes() -> None:
    profile = _profile(
        **{"obd:RPM": {"range": {"max": 7000, "reason": "redline", "modes": ["city"]}}}
    )
    evaluator = _evaluator(profile)
    evaluator.observe("obd:RPM", 9000.0, 1.0, "idle")
    assert evaluator.counters.detections["range"] == 0
    evaluator.observe("obd:RPM", 9000.0, 2.0, "city")
    assert evaluator.counters.detections["range"] == 1


def test_slew_is_scaled_by_the_real_time_between_arrived_samples() -> None:
    profile = _profile(
        **{
            "obd:COOLANT_TEMP": {
                "slew": {"per_second": 2.0, "allowance": 1.0, "reason": "thermal mass"}
            }
        }
    )
    written: list[EvidenceRecord] = []
    evaluator = _evaluator(profile, sink=written.append)
    evaluator.observe("obd:COOLANT_TEMP", 80.0, 0.0, "idle")
    evaluator.observe("obd:COOLANT_TEMP", 100.0, 10.0, "idle")  # 20 C in 10 s: allowed 21
    assert written == []
    evaluator.observe("obd:COOLANT_TEMP", 120.0, 11.0, "idle")  # 20 C in 1 s: allowed 3
    (record,) = written
    assert record.check == "slew"
    assert record.detail["dt_seconds"] == pytest.approx(1.0)
    assert record.detail["delta"] == pytest.approx(20.0)
    assert len(record.sample_times) == 2
    assert "slew limit" in record.text


def test_stuck_fires_only_where_the_channel_is_expected_to_move() -> None:
    profile = _profile(
        **{
            "obd:RPM": {"stuck": {"samples": 4, "modes": ["city"], "reason": "rpm jitters"}},
        }
    )
    evaluator = _evaluator(profile)
    for i in range(10):  # held at idle: not a mode where it must move
        evaluator.observe("obd:RPM", 700.0, float(i), "idle")
    assert evaluator.counters.detections["stuck"] == 0
    for i in range(10, 13):
        evaluator.observe("obd:RPM", 2000.0, float(i), "city")
    assert evaluator.counters.detections["stuck"] == 0  # 3 < 4
    evaluator.observe("obd:RPM", 2000.0, 13.0, "city")
    assert evaluator.counters.detections["stuck"] == 1
    for i in range(14, 18):
        evaluator.observe("obd:RPM", 2000.0, float(i), "city")
    assert evaluator.counters.detections["stuck"] == 2
    (confirmed,) = evaluator.confirmed()
    assert confirmed.detail["run_samples"] == 8


def test_coolant_holding_at_warm_idle_is_not_stuck_in_the_real_profile() -> None:
    config = example_config(REPO / "canary.example.yaml")
    catalog = resolve_limits(
        load_plausibility_profile(XTERRA_PROFILE),
        operating_modes=config.preprocessing.operating_modes,
    )
    evaluator = PlausibilityEvaluator(catalog, session="s")
    for i in range(2000):
        evaluator.observe("obd:COOLANT_TEMP", 88.0, float(i), "idle")
    assert evaluator.counters.detections["stuck"] == 0


def test_freshness_flags_one_silent_channel_while_its_group_answers() -> None:
    written: list[EvidenceRecord] = []
    evaluator = _evaluator(_profile(), sink=written.append)
    for second in range(0, 20):
        evaluator.observe("obd:RPM", 700.0 + second, float(second), "idle")
        if second < 5:
            evaluator.observe("obd:SPEED", 0.0, float(second) + 0.1, "idle")
        evaluator.check_freshness(float(second) + 0.5, "idle")
    freshness = [r for r in written if r.check == "freshness"]
    assert freshness and freshness[0].channel == "obd:SPEED"
    assert freshness[0].detail["group"] == "obd"
    assert freshness[0].limit == pytest.approx(3.0)
    assert [r.state for r in freshness][:2] == ["pending", "confirmed"]


def test_freshness_ignores_a_whole_group_going_quiet_and_restarts_on_resume() -> None:
    written: list[EvidenceRecord] = []
    evaluator = _evaluator(_profile(), sink=written.append)
    for second in range(5):
        evaluator.observe("obd:RPM", 700.0, float(second), "idle")
        evaluator.observe("obd:SPEED", 0.0, float(second), "idle")
    # A Mode 22 window: every Mode 01 PID stops together for 15 s.
    for second in range(5, 20):
        evaluator.check_freshness(float(second), "idle")
    assert written == []
    # Mode 01 resumes; SPEED's clock restarts at the resume, not at t=4.
    evaluator.observe("obd:RPM", 700.0, 20.0, "idle")
    evaluator.check_freshness(21.0, "idle")
    assert written == []
    evaluator.observe("obd:RPM", 700.0, 22.0, "idle")
    evaluator.observe("obd:RPM", 700.0, 23.5, "idle")
    evaluator.check_freshness(23.6, "idle")  # 3.6 s since the resume > 3 s
    assert [r.channel for r in written] == ["obd:SPEED"]


def test_carried_pending_confirms_on_the_next_drive_for_across_drive_checks(
    tmp_path: Path,
) -> None:
    profile = _profile()
    raw = profile.model_dump()
    raw["confirm"]["range"] = {"window_seconds": 60, "across_drives": True}
    profile = PlausibilityProfile.model_validate(raw)
    store = EvidenceStore(tmp_path / "evidence.jsonl")
    first = PlausibilityEvaluator(
        resolve_limits(profile), session="_capture-x-2026-09-01T000000", sink=store.append
    )
    first.observe("obd:SPEED", 300.0, 0.0, "city")
    assert [r.state for r in store.raw_records()] == ["pending"]
    # The next drive sorts *before* the capture by name; write order decides.
    carried = store.carried_pending("2026-09-02T000000")
    assert [r.channel for r in carried] == ["obd:SPEED"]
    second = PlausibilityEvaluator(
        resolve_limits(profile), session="2026-09-02T000000", sink=store.append, carried=carried
    )
    second.observe("obd:SPEED", 300.0, 86_400.0, "city")
    states = [r.state for r in store.raw_records()]
    assert states == ["pending", "confirmed"]
    confirmed = store.raw_records()[-1]
    assert confirmed.detail["confirmed_by"] == "repeat on the next drive"
    assert confirmed.detail["pending_session"] == "_capture-x-2026-09-01T000000"


def test_carried_pending_is_ignored_for_in_drive_checks() -> None:
    evaluator = _evaluator(_profile())
    stale = EvidenceRecord(
        state="pending",
        channel="obd:SPEED",
        check="range",
        value=300.0,
        limit=255.0,
        limit_kind="max",
        limit_source="x",
        unit="kph",
        operating_mode="city",
        sample_times=("t",),
        detected_at="1970-01-01T00:00:00Z",
        session="old",
        detections=1,
    )
    evaluator.reset("new", carried=[stale])
    evaluator.observe("obd:SPEED", 300.0, 5.0, "city")
    assert [r.state for r in evaluator.active()] == ["pending"]


def test_evidence_round_trips_and_text_is_rendered_from_the_record(tmp_path: Path) -> None:
    bindings = load_bindings(REPO / "data" / "bindings.yaml")
    catalog = resolve_limits(_profile(), bindings=bindings)
    store = EvidenceStore(tmp_path / "e.jsonl")
    evaluator = PlausibilityEvaluator(catalog, session="s", sink=store.append)
    evaluator.observe("decoded:ECM_COOLANT_TEMP", 250.0, 1.0, "idle")  # raw 250 = 210 degC
    (record,) = store.raw_records()
    assert record.value == 250.0 and record.engineering_value == pytest.approx(210.0)
    assert record.engineering_limit == pytest.approx(130.0)
    assert "210 degC" in record.text and "130 degC" in record.text
    line = json.loads((tmp_path / "e.jsonl").read_text().splitlines()[0])
    assert line["text"] == render_evidence(EvidenceRecord.from_dict(line))
    assert {"channel", "check", "value", "limit", "operating_mode", "sample_times"} <= set(line)


def test_unknown_channels_are_counted_as_unchecked_samples() -> None:
    evaluator = _evaluator(_profile())
    evaluator.observe("dbc:SOMETHING", 1.0, 0.0, "idle")
    evaluator.observe("obd:RPM", float("nan"), 0.0, "idle")
    assert evaluator.counters.unchecked_samples == 1
    assert evaluator.counters.non_finite == 1
    assert evaluator.coverage_summary()["observed_channels_unchecked"] == 1


def test_an_evaluator_refuses_to_run_without_a_profile() -> None:
    with pytest.raises(ValueError, match="plausibility.yaml"):
        PlausibilityEvaluator(resolve_limits(None), session="s")


# ---------------------------------------------------------------------------
# Live hook and daemon wiring (A05.9)
# ---------------------------------------------------------------------------


def test_the_live_source_hands_every_value_to_the_observer_before_the_quality_gate() -> None:
    from src.features.live import LiveFeatureSource
    from src.telemetry import TelemetryRecord

    config = example_config(REPO / "canary.example.yaml")
    seen: list[tuple[str, float]] = []
    source = LiveFeatureSource(config=config)
    source.sample_observer = lambda channel, value, _at: seen.append((channel, value))
    now = datetime(2026, 9, 22, tzinfo=UTC)
    source.observe(TelemetryRecord(now, "obd", "RPM", 30_000.0, "rpm"))  # gate rejects it
    assert seen == [("obd:RPM", 30_000.0)]
    assert source.reject_counts.get("obd:RPM") == 1


def _live_components(tmp_path: Path, *, enabled: bool, with_profile: bool) -> SimpleNamespace:
    config = example_config(REPO / "canary.example.yaml")
    vehicle = tmp_path / "vehicle"
    vehicle.mkdir(parents=True)
    if with_profile:
        (vehicle / PLAUSIBILITY_FILENAME).write_text(XTERRA_PROFILE.read_text())
    config = config.model_copy(
        update={
            "daemon": config.daemon.model_copy(
                update={
                    "afr_calibration": vehicle / "afr_calibration.yaml",
                    "anomalies_dir": tmp_path / "anomalies",
                }
            ),
            "plausibility": config.plausibility.model_copy(update={"enabled": enabled}),
        }
    )
    writer = SimpleNamespace(paths=SimpleNamespace(root=tmp_path / "2026-09-22T120000"))
    return SimpleNamespace(config=config, replay_controller=None, storage_writer=writer)


def test_plausibility_is_off_by_default_and_withheld_without_a_vehicle_file(
    tmp_path: Path,
) -> None:
    from src.daemon.inference import _build_plausibility
    from src.daemon.inference_metrics import InferenceTimingRecorder
    from src.features.live import LiveFeatureSource

    assert example_config(REPO / "canary.example.yaml").plausibility.enabled is False
    recorder = InferenceTimingRecorder()
    off = _live_components(tmp_path / "a", enabled=False, with_profile=True)
    source = LiveFeatureSource(config=off.config)
    assert _build_plausibility(off, source, recorder) is None  # type: ignore[arg-type]
    assert source.sample_observer is None
    assert recorder.snapshot().to_dict()["plausibility"] is None

    withheld = _live_components(tmp_path / "b", enabled=True, with_profile=False)
    assert _build_plausibility(withheld, source, recorder) is None  # type: ignore[arg-type]
    block = recorder.snapshot().to_dict()["plausibility"]
    assert block["enabled"] is True and "plausibility.yaml" in block["withheld"]


def test_live_plausibility_records_cost_and_evidence_per_tick(tmp_path: Path) -> None:
    from src.daemon.inference import _build_plausibility
    from src.daemon.inference_metrics import InferenceTimingRecorder
    from src.features.live import LiveFeatureSource
    from src.telemetry import TelemetryRecord

    components = _live_components(tmp_path, enabled=True, with_profile=True)
    recorder = InferenceTimingRecorder()
    source = LiveFeatureSource(config=components.config)
    live = _build_plausibility(components, source, recorder)  # type: ignore[arg-type]
    assert live is not None and source.sample_observer is not None
    start = datetime(2026, 9, 22, 12, tzinfo=UTC)
    for i in range(3):
        at = start.replace(second=i * 2)
        source.observe(TelemetryRecord(at, "obd", "SPEED", 400.0, "kph"))
        live.tick_live(components, at, "city")  # type: ignore[arg-type]
    timing = recorder.snapshot().to_dict()
    assert timing["stages"]["plausibility"]["max_ms"] >= 0.0
    block = timing["plausibility"]
    assert block["session"] == "2026-09-22T120000"
    assert block["counters"]["samples"] == 3
    assert block["open_confirmed"] == 1
    assert block["cost"]["last_tick_samples"] == 1
    lines = (tmp_path / "anomalies" / "plausibility-evidence.jsonl").read_text().splitlines()
    assert [json.loads(line)["state"] for line in lines] == ["pending", "confirmed"]


def test_replay_evidence_is_isolated_from_the_live_file(tmp_path: Path) -> None:
    from src.daemon.plausibility_live import evidence_path

    assert evidence_path(tmp_path, None) == tmp_path / "plausibility-evidence.jsonl"
    assert evidence_path(tmp_path, "r1") == tmp_path / "_replay" / "r1.plausibility-evidence.jsonl"


# ---------------------------------------------------------------------------
# Offline stream and fault injection (A05.8's plumbing)
# ---------------------------------------------------------------------------


def _stream(seconds: int = 600) -> SessionStream:
    rng = np.random.default_rng(0)
    rows = []
    for second in range(seconds):
        rows.append((float(second), "obd:RPM", 1500.0 + float(rng.normal(0, 50))))
        rows.append((second + 0.2, "obd:SPEED", 60.0 + float(rng.normal(0, 2))))
    samples = pd.DataFrame(rows, columns=["t", "channel", "value"]).sort_values("t")
    samples = samples.reset_index(drop=True)
    return SessionStream("synthetic", samples, np.full(len(samples), "city", dtype=object))


@pytest.mark.parametrize("kind", ["spike", "scale", "dropout"])
def test_injected_plumbing_faults_are_caught_on_arrived_samples(kind: str) -> None:
    profile = _profile(**{"obd:RPM": {"range": {"max": 7000, "reason": "redline"}}})
    stream, injection = inject_plumbing_fault(
        _stream(), channel="obd:RPM", kind=kind, rng=np.random.default_rng(42)
    )
    assert injection.changed > 0
    evaluator = PlausibilityEvaluator(resolve_limits(profile), session="s")
    records = run_evaluator(evaluator, stream)
    hits = detections_in(
        records, channels=["obd:RPM"], start=injection.start, end=injection.end + 10.0
    )
    assert any(r.state == "confirmed" for r in hits)
    clean = run_evaluator(PlausibilityEvaluator(resolve_limits(profile), session="c"), _stream())
    assert [r for r in clean if r.state == "confirmed"] == []


def test_a_dropout_removes_samples_rather_than_holding_them() -> None:
    stream, injection = inject_plumbing_fault(
        _stream(), channel="obd:RPM", kind="dropout", rng=np.random.default_rng(1)
    )
    rpm = stream.samples.loc[stream.samples["channel"] == "obd:RPM", "t"]
    inside = rpm[(rpm >= injection.start) & (rpm < injection.end)]
    assert inside.empty and injection.changed > 0
    assert len(stream.modes) == len(stream.samples)


def test_slew_floors_dt_at_the_sources_refresh_interval() -> None:
    profile = _profile(
        **{
            "obd:RPM": {
                "slew": {
                    "per_second": 4500,
                    "allowance": 12.5,
                    "update_interval_seconds": 0.2,
                    "reason": "one ECM refresh",
                }
            }
        }
    )
    evaluator = _evaluator(profile)
    evaluator.observe("obd:RPM", 1000.0, 0.0, "city")
    evaluator.observe("obd:RPM", 1400.0, 0.006, "city")  # one refresh straddled
    assert evaluator.counters.detections["slew"] == 0
    evaluator.observe("obd:RPM", 3500.0, 0.012, "city")  # 2,100 rpm: no refresh does that
    assert evaluator.counters.detections["slew"] == 1


def test_a_modes_only_range_rule_gates_the_catalog_bound() -> None:
    bindings = _bindings(
        _signal(
            "COOLANT",
            scale=1.0,
            offset=-40.0,
            unit="degC",
            plausible_min=-10.0,
            plausible_max=130.0,
        ),
    )
    profile = _profile(
        **{"decoded:COOLANT": {"range": {"modes": ["idle"], "reason": "key-on sentinel"}}}
    )
    catalog = resolve_limits(profile, bindings=bindings, operating_modes=MODES)
    coolant = catalog.get("decoded:COOLANT")
    assert coolant.minimum is not None and coolant.minimum.value == 30.0  # bound kept
    evaluator = PlausibilityEvaluator(catalog, session="s")
    evaluator.observe("decoded:COOLANT", 0.0, 1.0, None)  # -40 degC at key-on
    evaluator.observe("decoded:COOLANT", 0.0, 2.0, "koeo")
    assert evaluator.counters.detections["range"] == 0
    evaluator.observe("decoded:COOLANT", 0.0, 3.0, "idle")
    assert evaluator.counters.detections["range"] == 1


def test_non_priority_mode01_channels_report_freshness_unchecked() -> None:
    catalog = resolve_limits(_profile(), priority_channels=("RPM", "THROTTLE_POS"))
    assert catalog.get("obd:RPM").coverage()["freshness"] == "checked"
    assert (
        catalog.get("obd:SPEED")
        .coverage()["freshness"]
        .startswith("unchecked: not a priority channel")
    )


def test_suspended_group_time_does_not_age_a_channel() -> None:
    raw = _profile().model_dump()
    raw["groups"] = {"obd": {"silence_seconds": 2.0, "reason": "test"}}
    evaluator = PlausibilityEvaluator(
        resolve_limits(PlausibilityProfile.model_validate(raw)), session="s"
    )
    for t in (0.0, 0.1, 0.2):  # three arrivals: a cadence to expect
        evaluator.observe("obd:SPEED", 0.0, t, "idle")
    # 2 s of answering, a 30 s Mode 22 window, 0.5 s more: 2.5 s < 3 s.
    for t in (0.5, 1.0, 1.5, 2.0, 32.0, 32.5):
        evaluator.observe("obd:RPM", 700.0, t, "idle")
    evaluator.check_freshness(t + 0.1, "idle")
    assert evaluator.counters.detections["freshness"] == 0
    for t in (33.0, 33.5):
        evaluator.observe("obd:RPM", 700.0, t, "idle")
    evaluator.check_freshness(t + 0.1, "idle")  # 3.5 s answering without SPEED
    assert evaluator.counters.detections["freshness"] == 1
    (record,) = evaluator.active()
    assert record.detail["answering_age_seconds"] == pytest.approx(3.3)
    assert record.detail["age_seconds"] == pytest.approx(33.4)


def test_a_channel_seen_once_is_never_stale() -> None:
    evaluator = _evaluator(_profile())
    evaluator.observe("obd:SPEED", 0.0, 0.0, "idle")  # a re-planned capture's leftover
    for second in range(1, 30):
        evaluator.observe("obd:RPM", 700.0, float(second), "idle")
        evaluator.check_freshness(second + 0.5, "idle")
    assert evaluator.counters.detections["freshness"] == 0


def test_stuck_needs_the_duration_as_well_as_the_count() -> None:
    profile = _profile(
        **{
            "obd:MAF": {
                "stuck": {"samples": 5, "min_seconds": 10.0, "modes": ["city"], "reason": "x"}
            }
        }
    )
    evaluator = _evaluator(profile)
    for i in range(20):  # 20 identical samples in 4.75 s: a fast poll, not a fault
        evaluator.observe("obd:MAF", 12.34, i * 0.25, "city")
    assert evaluator.counters.detections["stuck"] == 0
    for i in range(20, 50):
        evaluator.observe("obd:MAF", 12.34, i * 0.25, "city")
    # 10 s reached at sample 41; the repeat needs 20 s.
    assert evaluator.counters.detections["stuck"] == 1


DRIVE_SLICE = REPO / "tests" / "fixtures" / "ground_truth" / "drive-slice"


def _xterra_catalog() -> tuple[CanaryConfig, Any]:
    config = example_config(REPO / "canary.example.yaml")
    bindings = load_bindings(REPO / "data" / "bindings.yaml")
    catalog = resolve_limits(
        load_plausibility_profile(XTERRA_PROFILE),
        bindings=bindings,
        did_map=load_did_map(REPO / "data" / "dids.yaml"),
        operating_modes=config.preprocessing.operating_modes,
        priority_channels=config.collection.obd_priority_channels,
    )
    return config, (catalog, bindings)


def test_a_real_clean_drive_confirms_nothing_and_a_real_spike_confirms() -> None:
    from src.plausibility.offline import load_stream

    config, (catalog, bindings) = _xterra_catalog()
    stream = load_stream(
        DRIVE_SLICE,
        config,
        catalog,
        bindings=bindings,
        did_map=load_did_map(REPO / "data" / "dids.yaml"),
    )
    assert int((stream.samples["channel"] == "obd:RPM").sum()) > 200
    assert set(stream.modes) - {None} <= set(config.preprocessing.operating_modes) | {"unknown"}
    clean = run_evaluator(PlausibilityEvaluator(catalog, session="slice"), stream)
    assert [r for r in clean if r.state == "confirmed"] == []
    faulted, injection = inject_plumbing_fault(
        stream, channel="obd:RPM", kind="spike", rng=np.random.default_rng(42)
    )
    records = run_evaluator(PlausibilityEvaluator(catalog, session="slice"), faulted)
    hits = detections_in(records, channels=["obd:RPM"], start=injection.start, end=injection.end)
    assert {r.check for r in hits} >= {"range"}
    assert any(r.state == "confirmed" for r in hits)


def test_the_evaluation_script_grades_every_fault_and_names_no_ops(tmp_path: Path) -> None:
    from scripts.evaluate_plausibility import main

    out = tmp_path / "report.json"
    assert (
        main(
            [
                "--config",
                str(REPO / "canary.example.yaml"),
                "--profile",
                "nissan-xterra-2008",
                "--clean",
                str(DRIVE_SLICE),
                "--fault-sessions",
                str(DRIVE_SLICE),
                "--fault-channels",
                "obd:RPM",
                "obd:NOT_A_CHANNEL",
                "--n-injections",
                "1",
                "--workers",
                "1",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    report = json.loads(out.read_text())
    summary = report["summary"]
    assert summary["clean_confirmed_total"] == 0
    assert summary["clean_drives"][0]["result"] == "scored_quiet"
    assert summary["plumbing_by_channel_kind"]["obd:RPM spike"]["confirmed"] == 1
    assert summary["plumbing_by_channel_kind"]["obd:NOT_A_CHANNEL spike"] == {"no_op": 1}


def test_an_enabled_evaluator_with_a_broken_file_is_withheld_not_off(tmp_path: Path) -> None:
    from src.daemon.inference import _build_plausibility
    from src.daemon.inference_metrics import InferenceTimingRecorder
    from src.features.live import LiveFeatureSource

    components = _live_components(tmp_path, enabled=True, with_profile=True)
    (tmp_path / "vehicle" / PLAUSIBILITY_FILENAME).write_text("schema_version: [1\n  tiers: {")
    recorder = InferenceTimingRecorder()
    source = LiveFeatureSource(config=components.config)
    assert _build_plausibility(components, source, recorder) is None  # type: ignore[arg-type]
    block = recorder.snapshot().to_dict()["plausibility"]
    assert block is not None and block["enabled"] is True
    # Refused by the loader as a bad file, not an unexpected crash.
    assert block["withheld"] and not block["withheld"].startswith("evaluator failed")
    assert source.sample_observer is None


def test_an_unexpected_start_failure_is_withheld_not_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.daemon import plausibility_live
    from src.daemon.inference import _build_plausibility
    from src.daemon.inference_metrics import InferenceTimingRecorder
    from src.features.live import LiveFeatureSource

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(plausibility_live.LivePlausibility, "build", boom)
    components = _live_components(tmp_path, enabled=True, with_profile=True)
    recorder = InferenceTimingRecorder()
    source = LiveFeatureSource(config=components.config)
    assert _build_plausibility(components, source, recorder) is None  # type: ignore[arg-type]
    block = recorder.snapshot().to_dict()["plausibility"]
    assert block["enabled"] is True
    assert block["withheld"].startswith("evaluator failed to start: RuntimeError")


def test_offline_mode_labels_read_exactly_the_live_segmenter_columns() -> None:
    from src.features.feature_pipeline import _SEGMENTER_SIGNAL_COLUMNS
    from src.plausibility.offline import segmenter_channels, segmenter_decoded

    expected: set[str] = set()
    for value in _SEGMENTER_SIGNAL_COLUMNS.values():
        expected |= {value} if isinstance(value, str) else set(value)
    assert set(segmenter_channels()) == expected
    assert {f"decoded:{name}" for name in segmenter_decoded()} == {
        c for c in expected if c.startswith("decoded:")
    }


# ---------------------------------------------------------------------------
# A05.9 — confirmed detections enter the anomaly event stream and the push
# ---------------------------------------------------------------------------


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, message: str, *, priority: str = "default") -> None:
        self.calls.append((title, message))


def test_a_confirmed_detection_is_an_anomaly_event_and_a_push(tmp_path: Path) -> None:
    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.event_logger import AnomalyEventLogger, read_events_dir

    notifier = _RecordingNotifier()
    emitter = AnomalyEventEmitter(
        AnomalyEventLogger(tmp_path), min_seconds_between_events=0, notifier=notifier
    )
    written: list[EvidenceRecord] = []
    evaluator = _evaluator(_profile(), sink=written.append, alert_sink=emitter.emit_plausibility)
    evaluator.observe("obd:SPEED", 300.0, 100.0, "city")  # pending: evidence only
    assert read_events_dir(tmp_path) == [] and notifier.calls == []
    evaluator.observe("obd:SPEED", 300.0, 102.0, "city")  # confirmed
    assert [r.state for r in written] == ["pending", "confirmed"]

    events = read_events_dir(tmp_path)
    assert len(events) == 1
    event = events[0]
    assert event.source == "plausibility"
    assert event.model_version == "plausibility/range"
    assert event.affected_channels == ("obd:SPEED",)
    assert event.operating_mode == "city" and event.session == "s1"
    assert event.timestamp == written[1].detected_at
    assert event.detail["cause"] == written[1].text
    assert EvidenceRecord.from_dict(event.detail["plausibility"]) == written[1]
    assert emitter.counters.emitted == 1
    assert notifier.calls == [("CANary: plausibility anomaly", f"{written[1].text} [session s1]")]


def test_a_pending_record_can_never_become_an_event(tmp_path: Path) -> None:
    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.event_logger import AnomalyEventLogger, read_events_dir
    from src.alerts.events import AnomalyEvent

    written: list[EvidenceRecord] = []
    _evaluator(_profile(), sink=written.append).observe("obd:SPEED", 300.0, 100.0, "city")
    pending = written[0]
    assert pending.state == "pending"
    with pytest.raises(ValueError, match="only a confirmed detection"):
        AnomalyEvent.from_plausibility(pending)
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)
    assert emitter.emit_plausibility(pending) is None
    assert emitter.counters.suppressed_not_anomaly == 1 and read_events_dir(tmp_path) == []


def test_plausibility_events_dedupe_per_channel_and_check_and_never_push_in_replay(
    tmp_path: Path,
) -> None:
    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.event_logger import AnomalyEventLogger

    notifier = _RecordingNotifier()
    emitter = AnomalyEventEmitter(
        AnomalyEventLogger(tmp_path), min_seconds_between_events=60.0, notifier=notifier
    )
    confirmed: list[EvidenceRecord] = []
    evaluator = _evaluator(_profile(), alert_sink=confirmed.append)
    for channel in ("obd:SPEED", "obd:RPM"):
        evaluator.observe(channel, 1e6, 100.0, "city")
        evaluator.observe(channel, 1e6, 102.0, "city")
    assert [r.channel for r in confirmed] == ["obd:SPEED", "obd:RPM"]
    # Two channels confirming at the same instant are two events, not one.
    assert all(emitter.emit_plausibility(r) is not None for r in confirmed)
    assert emitter.counters.emitted == 2 and len(notifier.calls) == 2
    # The same (channel, check) inside the refractory window is deduped.
    assert emitter.emit_plausibility(confirmed[0]) is None
    assert emitter.counters.suppressed_dedupe == 1

    emitter.enter_replay_mode()
    assert emitter.emit_plausibility(confirmed[0]) is not None  # fresh replay dedupe
    assert len(notifier.calls) == 2  # a replayed detection never pushes


def test_an_alert_sink_failure_never_costs_the_evidence() -> None:
    def broken(record: EvidenceRecord) -> None:
        raise RuntimeError("emitter down")

    written: list[EvidenceRecord] = []
    evaluator = _evaluator(_profile(), sink=written.append, alert_sink=broken)
    evaluator.observe("obd:SPEED", 300.0, 100.0, "city")
    evaluator.observe("obd:SPEED", 300.0, 102.0, "city")
    assert [r.state for r in written] == ["pending", "confirmed"]
    assert evaluator.counters.confirmed == 1


def test_the_part_by_part_stream_equals_the_whole_session_one(tmp_path: Path) -> None:
    """``load_stream`` reads a session in time-disjoint parts (a 2 h drive
    whole took the A02.11 evaluation to 13.2 GB); samples and modes must
    equal the whole-session chain exactly. A chunk of stored decoded rows
    (values no frame decodes to) changes neither: P02.8, decode always."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    from src.plausibility.offline import (
        _checked_decoded,
        load_stream,
        sample_modes,
        samples_from_long,
        segmenter_decoded,
        session_long,
    )

    config, (catalog, bindings) = _xterra_catalog()
    did_map = load_did_map(REPO / "data" / "dids.yaml")
    table = pq.read_table(DRIVE_SLICE / "telemetry-00000.parquet")
    table = table.take(pc.sort_indices(table, sort_keys=[("timestamp", "ascending")]))
    session = tmp_path / "2026-08-03T231900"
    session.mkdir()
    step = table.num_rows // 6
    for i in range(6):
        piece = table.slice(i * step, step if i < 5 else table.num_rows - i * step)
        pq.write_table(piece, session / f"telemetry-{i:05d}.parquet")
    # One chunk of stored decoded rows for a checked signal, with made-up
    # values: never read (P02.8), in any part; the frames are decoded instead.
    stored = next(iter(sorted(_checked_decoded(catalog))))
    can = table.filter(pc.equal(table["source"], "can")).slice(0, 50)
    n = can.num_rows
    rows = can.set_column(
        can.schema.get_field_index("source"),
        table.schema.field("source"),
        pa.array(["decoded"] * n, type=table.schema.field("source").type),
    )
    rows = rows.set_column(
        rows.schema.get_field_index("channel"),
        table.schema.field("channel"),
        pa.array([stored] * n, type=table.schema.field("channel").type),
    )
    rows = rows.set_column(
        rows.schema.get_field_index("value_number"),
        table.schema.field("value_number"),
        pa.array([float(i) for i in range(n)], type=pa.float64()),
    )
    pq.write_table(rows, session / "telemetry-00006.parquet")

    wanted = _checked_decoded(catalog) | set(segmenter_decoded())
    long = session_long(session, bindings=bindings, did_map=did_map, decoded=wanted)
    samples = samples_from_long(long)
    modes = sample_modes(long, samples, config, bindings=bindings)
    stream = load_stream(
        session, config, catalog, bindings=bindings, did_map=did_map, part_rows=3_000
    )

    assert (samples["channel"] == f"decoded:{stored}").sum() > 0
    pd.testing.assert_frame_equal(samples, stream.samples, check_exact=True)
    assert list(modes) == list(stream.modes)
    (session / "telemetry-00006.parquet").unlink()
    without = load_stream(
        session, config, catalog, bindings=bindings, did_map=did_map, part_rows=3_000
    )
    pd.testing.assert_frame_equal(without.samples, stream.samples, check_exact=True)


def test_the_catalog_fingerprint_is_stable_across_hash_seeds() -> None:
    """The stream cache is keyed by it; a ``repr`` hash followed
    PYTHONHASHSEED (the catalog holds frozensets) and never hit."""
    import os
    import subprocess
    import sys

    code = (
        "from pathlib import Path\n"
        "from src.config import CanaryConfig\n"
        "from src.decode.bindings import load_bindings\n"
        "from src.decode.dids import load_did_map\n"
        "from src.plausibility.limits import load_plausibility_profile, resolve_limits\n"
        f"profile = Path({str(XTERRA_PROFILE)!r})\n"
        "c = CanaryConfig.from_yaml(Path('canary.example.yaml'))\n"
        "cat = resolve_limits(load_plausibility_profile(profile),"
        " bindings=load_bindings(Path('data/bindings.yaml')),"
        " did_map=load_did_map(Path('data/dids.yaml')),"
        " operating_modes=c.preprocessing.operating_modes,"
        " priority_channels=c.collection.obd_priority_channels)\n"
        "print(cat.fingerprint())\n"
    )
    prints = {
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO,
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for seed in ("1", "2", "3")
    }
    assert len(prints) == 1
