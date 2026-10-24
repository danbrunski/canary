"""Per-read cadence for memory windows (D03.38 "Per-read cadence").

The knock-fault reads want about 1 Hz; before this every memory read rode one
rate (every session window, or the capture's target) and a profile could not
weight one window against another. ``CaptureProfile.memory_rate_weights`` now
weights memory channels the way ``mode22_rate_weights`` weights DIDs, and a
window -- one service-0x23 read serving every member -- runs at its fastest
selected member's weight.

The rate assertions run the real collector loop on the fake clock against the
session-gated :class:`ScriptedEcu`, and measure each channel's achieved rate
from consecutive *pairs* of its own readings (median gap), never as a count
over wall-clock (AGENTS.md: ``achieved_hz_capture``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.collection import tuning_profile, tx_gate
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.capture_profiles import (
    CaptureProfile,
    CaptureProfileCatalog,
    CaptureProfileOverrides,
    CaptureProfileSources,
    CaptureProfileStore,
    layer_catalog,
    load_capture_profiles,
    profile_digest,
)
from src.collection.obd_collector import ObdReading
from src.collection.obd_scheduler import (
    CanObdSpec,
    DidSessionSpec,
    PollRequest,
    _request_rate_hz,
    build_poll_requests,
    preview_polling_plan,
)
from src.collection.profile_view import ProfileView
from src.config import CanaryConfig, CanBusConfig
from src.decode.dids import MEMORY_SOURCE, memory_channels, select_memory_channels
from src.yaml_load import load_yaml
from tests.capture_round_fakes import FakeClock, ScriptedEcu, install_clock

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml").collection
XTERRA_CATALOG = REPO_ROOT / "data" / "nissan-xterra-2008" / "capture_profiles.yaml"

#: A one-byte window (the knock-sensor fault flag) and a one-word window of
#: its own, so each read decodes exactly one channel and its rate is that
#: channel's rate.
FAULT = "ECM_KS_FAULT_FLAGS"
OTHER = "ECM_INJECTOR_DEAD_TIME"
#: Two members of one window (`knock_fault_levels`), for the max rule.
LEVEL_1 = "ECM_KS1_LEVEL"
LEVEL_2 = "ECM_KS2_LEVEL"
CYCLE = 0.25
START = 1000.0


def _windows() -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for channel in memory_channels():
        if channel.window:
            out.setdefault(channel.window, []).append(channel.name)
    return {name: tuple(members) for name, members in out.items()}


def _profile(**update: object) -> CaptureProfile:
    fields: dict[str, object] = {
        "name": "fault-reads",
        "mode01": {"RPM": 1.0},
        "memory": (FAULT, OTHER),
        "did_cycle_seconds": CYCLE,
        "storage_prefix": "_capture-fault-reads",
    }
    fields.update(update)
    return CaptureProfile.model_validate(fields)


def _spec(names: tuple[str, ...] = (FAULT, OTHER)) -> CanObdSpec:
    return CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=4.0,
        pids=("RPM",),
        batch_max_pids=1,
        priority_channels=(),
        dtc_poll_interval_seconds=0.0,
        service_poll_interval_seconds=0.0,
        request_timeout_seconds=0.2,
        capture_profiles_enabled=True,
        memory=select_memory_channels(names),
        did_session=DidSessionSpec(
            session=0xC0,
            exit_session=0x81,
            exit_grace_seconds=0.05,
            keepalive_seconds=2.0,
            # A loop that *starts* under a cycle profile opens its first window
            # one config interval in; kept short so the run measures cadence.
            window_interval_seconds=CYCLE,
            window_max_seconds=10.0,
            lapse_seconds=5.0,
        ),
    )


def _memory_request(spec: CanObdSpec, window: str) -> PollRequest:
    for request in build_poll_requests(None, spec):
        if request.kind == "memory" and request.channel == f"{MEMORY_SOURCE}:{window}":
            return request
    raise AssertionError(f"no memory read for {window}")


# ---------------------------------------------------------------------------
# Schema: named refusals
# ---------------------------------------------------------------------------


def test_a_weight_on_a_channel_memory_does_not_select_is_refused() -> None:
    with pytest.raises(ValueError, match="not selected by memory: ECM_KS1_LEVEL"):
        _profile(memory_rate_weights={LEVEL_1: 0.5})


def test_a_weight_keyed_by_a_window_is_refused_naming_its_members() -> None:
    windows = _windows()
    assert windows["knock_fault_levels"] == (LEVEL_1, LEVEL_2)
    with pytest.raises(ValueError) as refused:
        _profile(memory=(LEVEL_1, LEVEL_2), memory_rate_weights={"knock_fault_levels": 0.5})
    message = str(refused.value)
    assert "knock_fault_levels is a memory window, not a channel" in message
    assert f"({LEVEL_1}, {LEVEL_2})" in message


@pytest.mark.parametrize("weight", [0.0, -1.0, float("inf"), float("nan")])
def test_a_weight_must_be_finite_and_positive(weight: float) -> None:
    with pytest.raises(ValueError, match="must be finite and positive"):
        _profile(memory_rate_weights={FAULT: weight})


def test_the_store_refuses_a_weighted_channel_the_catalog_does_not_hold(tmp_path: Path) -> None:
    """Selected-but-unknown fails the same catalog check ``memory:`` always
    had (b986365), so the weight cannot smuggle an unknown name past it."""
    store = CaptureProfileStore(
        CaptureProfileSources(tmp_path / "catalog.yaml", tmp_path / "overrides.yaml"),
        EXAMPLE,
        tmp_path / "bm.yaml",
    )
    profile = _profile(memory=(FAULT, "ECM_NO_SUCH_WORD"), memory_rate_weights={FAULT: 0.25})
    with pytest.raises(ValueError, match="memory: .*ECM_NO_SUCH_WORD"):
        store.save(profile)
    assert not (tmp_path / "overrides.yaml").exists()


# ---------------------------------------------------------------------------
# The window mapping: the fastest selected member's weight
# ---------------------------------------------------------------------------


def test_a_window_runs_at_its_fastest_selected_members_weight() -> None:
    spec = _spec((LEVEL_1, LEVEL_2))
    request = _memory_request(spec, "knock_fault_levels")
    both = _profile(memory=(LEVEL_1, LEVEL_2), memory_rate_weights={LEVEL_1: 0.2, LEVEL_2: 0.5})
    assert ProfileView(both).memory_window_weight(request) == 0.5
    # An unweighted member weighs 1.0, so it keeps the window at every boundary.
    one = _profile(memory=(LEVEL_1, LEVEL_2), memory_rate_weights={LEVEL_1: 0.2})
    assert ProfileView(one).memory_window_weight(request) == 1.0
    # A member this profile does not select contributes nothing, even though
    # another profile's selection put it in the read.
    only = _profile(memory=(LEVEL_1,), memory_rate_weights={LEVEL_1: 0.2})
    assert ProfileView(only).memory_window_weight(request) == 0.2
    # A profile selecting no member has no weight for the read at all.
    assert ProfileView(_profile()).memory_window_weight(request) is None


def test_windowed_rate_is_weight_over_window_period_capped_at_one_per_window() -> None:
    spec = _spec()
    reads = {
        r.memory_read.channels[0].name: r
        for r in build_poll_requests(None, spec)
        if r.memory_read is not None
    }
    weighted = ProfileView(_profile(memory_rate_weights={FAULT: 0.25, OTHER: 0.05}))
    assert _request_rate_hz(reads[FAULT], spec, weighted) == pytest.approx(1.0)
    assert _request_rate_hz(reads[OTHER], spec, weighted) == pytest.approx(0.2)
    # Above 1.0 cannot add a second read to one window.
    fast = ProfileView(_profile(memory_rate_weights={FAULT: 3.0}))
    assert _request_rate_hz(reads[FAULT], spec, fast) == pytest.approx(1 / CYCLE)


def test_a_target_rate_multiplies_the_window_weight() -> None:
    spec = _spec()
    reads = {
        r.memory_read.channels[0].name: r
        for r in build_poll_requests(None, spec)
        if r.memory_read is not None
    }
    view = ProfileView(
        _profile(
            did_cycle_seconds=None,
            did_continuous=True,
            mode01={},
            mode01_enabled=False,
            mode22=("ECM_RPM",),
            target_rate_hz=10.0,
            memory_rate_weights={FAULT: 0.1, OTHER: 0.5},
        )
    )
    assert view.request_absolute_rate_hz(reads[FAULT]) == pytest.approx(1.0)
    assert _request_rate_hz(reads[FAULT], spec, view) == pytest.approx(1.0)
    assert _request_rate_hz(reads[OTHER], spec, view) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Achieved rate on the fake clock, measured as pairs over time
# ---------------------------------------------------------------------------


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock(monotonic=START)
    install_clock(monkeypatch, fake)
    return fake


@pytest.fixture(autouse=True)
def runtime_reset() -> object:
    yield None
    tuning_profile.set_paused(False)
    tuning_profile.configure_did_aliases({})
    tx_gate.set_passive(False, source="test")
    tuning_profile.set_active(False, source="test")


def _run(
    clock: FakeClock, profile: CaptureProfile, seconds: float
) -> tuple[ScriptedEcu, list[Any]]:
    tuning_profile.activate(profile, source="test")
    bus = ScriptedEcu(clock, session_gated=True)
    readings: list[ObdReading] = []
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + seconds,
    )
    return bus, readings


def _paired_hz(readings: list[ObdReading], channel: str) -> tuple[float, int]:
    """Median-gap rate from consecutive readings of ``channel``, and the pair count."""
    stamps = sorted(
        float(r.acquired_at if r.acquired_at is not None else r.timestamp)
        for r in readings
        if r.pid == channel
    )
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert gaps, f"{channel}: fewer than two readings"
    return 1.0 / statistics.median(gaps), len(gaps)


def test_a_fault_window_runs_at_one_hz_while_another_runs_slower(clock: FakeClock) -> None:
    profile = _profile(memory_rate_weights={FAULT: 0.25, OTHER: 0.05})
    bus, readings = _run(clock, profile, seconds=60.0)
    fault_hz, fault_pairs = _paired_hz(readings, f"{MEMORY_SOURCE}:{FAULT}")
    other_hz, other_pairs = _paired_hz(readings, f"{MEMORY_SOURCE}:{OTHER}")
    assert fault_hz == pytest.approx(1.0, rel=0.02)
    assert other_hz == pytest.approx(0.2, rel=0.02)
    # Enough pairs that the medians mean something: one minute of each.
    assert fault_pairs >= 55
    assert other_pairs >= 10
    # A weight skips windows on the cycle grid; it never moves the grid. Every
    # session entry lands on a whole number of cycles from the first (a window
    # with nothing due is not opened at all, so gaps of several cycles occur).
    entries = [at for at, msg in bus.sent if bytes(msg.data)[1:3] == b"\x10\xc0"]
    cycles = [(at - entries[0]) / CYCLE for at in entries]
    assert all(abs(k - round(k)) < 0.05 for k in cycles)
    # Each fault read is one read on the wire: no duplicates inside a window.
    fault_reads = [at for at, address, _n in bus.memory_reads if address == 0xFFFF479D]
    assert len(fault_reads) == fault_pairs + 1


def test_without_weights_every_window_reads_every_member(clock: FakeClock) -> None:
    """The pre-D03.38 behaviour, unchanged: every selected read in every window."""
    _bus, readings = _run(clock, _profile(), seconds=20.0)
    for name in (FAULT, OTHER):
        hz, _pairs = _paired_hz(readings, f"{MEMORY_SOURCE}:{name}")
        assert hz == pytest.approx(1 / CYCLE, rel=0.02)


# ---------------------------------------------------------------------------
# Backward compatibility: saved profiles and Pi overrides load and behave as before
# ---------------------------------------------------------------------------


def _pre_d0338_digest(profile: CaptureProfile) -> str:
    """``profile_digest`` as it was computed before the field existed."""
    dumped = profile.model_dump(mode="json")
    dumped.pop("memory_rate_weights")
    return hashlib.sha256(json.dumps(dumped, sort_keys=True).encode()).hexdigest()


def test_tracked_xterra_profiles_keep_their_digests_and_memory_rates() -> None:
    """Every tracked Xterra profile loads, an unweighted one digests as it
    did, and memory reads run at the rate they ran at before: `tuning` reads
    its two original windows at its 4 Hz target capped to one per 0.5 s
    cycle, i.e. 2 Hz. The knock windows it gained on 2026-10-08 are weighted:
    0.5 still hits the 2 Hz cap, 0.125 runs at 0.5 Hz."""
    catalog = CaptureProfileCatalog.model_validate(load_yaml(XTERRA_CATALOG.read_text()))
    for profile in catalog.profiles:
        if not profile.memory_rate_weights:
            assert profile_digest(profile) == _pre_d0338_digest(profile)
    tuning = catalog.by_name("tuning")
    assert tuning is not None and tuning.memory
    spec = CanObdSpec.from_config(
        EXAMPLE,
        CanBusConfig(interface="virtual", channel="compat", bitrate=500000),
        catalog=catalog.profiles,
    )
    view = ProfileView(tuning)
    rates = {
        request.channel: _request_rate_hz(request, spec, view)
        for request in build_poll_requests(None, spec)
        if request.kind == "memory" and view.request_eligible(request)
    }
    assert rates == {
        f"{MEMORY_SOURCE}:fuel_target": 2.0,
        f"{MEMORY_SOURCE}:spark": 2.0,
        **{
            f"{MEMORY_SOURCE}:{window}": 2.0
            for window in ("flags_2841", "knock_retard", "knock_target", "knock_flags")
        },
        **{
            f"{MEMORY_SOURCE}:{window}": 0.5
            for window in ("knock_fault_items", "knock_fault_flag", "knock_fault_levels")
        },
    }


def test_a_pi_override_saved_before_the_field_loads_and_is_not_reported_shadowed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An overrides file written before D03.38 -- no ``memory_rate_weights``
    key anywhere, ``base_digests`` from the old digest -- loads, wins by name,
    and does not trigger the "repo's version has changed" warning."""
    tracked = CaptureProfileCatalog.model_validate(load_yaml(XTERRA_CATALOG.read_text()))
    # A profile the repo has not given weights since: `tuning` gained
    # weighted knock windows on 2026-10-08, so an override of it saved before
    # then is correctly reported as shadowing a changed repo version.
    base = next(p for p in tracked.profiles if not p.memory_rate_weights)
    name = base.name
    saved = base.model_dump(mode="json")
    saved.pop("memory_rate_weights")
    saved["description"] = "edited in the Pi dashboard"
    overrides_path = tmp_path / "overrides.yaml"
    overrides_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "profiles": [saved],
                "deleted": [],
                "base_digests": {name: _pre_d0338_digest(base)},
            },
            sort_keys=False,
        )
    )
    assert "memory_rate_weights" not in overrides_path.read_text()
    with caplog.at_level(logging.WARNING, logger="src.collection.capture_profiles"):
        effective = load_capture_profiles(
            CaptureProfileSources(XTERRA_CATALOG, overrides_path), EXAMPLE
        )
    layered = effective.by_name(name)
    assert layered is not None
    assert layered.description == "edited in the Pi dashboard"
    assert layered.memory == base.memory
    assert layered.memory_rate_weights == {}
    assert not [r for r in caplog.records if "overridden on this machine" in r.getMessage()]
    # The same comparison layer_catalog makes, stated directly.
    overrides = CaptureProfileOverrides.model_validate(load_yaml(overrides_path.read_text()))
    assert overrides.base_digests[name] == profile_digest(base)
    assert layer_catalog(tracked, overrides).by_name(name) == layered


def test_a_weight_changes_the_digest() -> None:
    plain = _profile()
    weighted = _profile(memory_rate_weights={FAULT: 0.25})
    assert profile_digest(plain) != profile_digest(weighted)


# ---------------------------------------------------------------------------
# The preview charges memory reads to the budget, one member per read
# ---------------------------------------------------------------------------


def test_the_preview_counts_every_window_and_its_weighted_rate() -> None:
    """Every memory read used to carry identifier 0, so the plan's
    (service, request id, identifier) key folded all selected windows into
    one member: three windows previewed as one read."""
    profile = _profile(
        memory=(FAULT, OTHER, LEVEL_1),
        memory_rate_weights={FAULT: 0.25, OTHER: 0.05},
    )
    plan = preview_polling_plan(EXAMPLE, profile)
    memory = {m["channel"]: m for m in plan["members"] if m["kind"] == "memory"}
    assert set(memory) == {
        f"{MEMORY_SOURCE}:knock_fault_flag",
        f"{MEMORY_SOURCE}:injector_dead_time",
        f"{MEMORY_SOURCE}:knock_fault_levels",
    }
    assert memory[f"{MEMORY_SOURCE}:knock_fault_flag"]["rate_hz"] == pytest.approx(1.0)
    assert memory[f"{MEMORY_SOURCE}:injector_dead_time"]["rate_hz"] == pytest.approx(0.2)
    assert memory[f"{MEMORY_SOURCE}:knock_fault_levels"]["rate_hz"] == pytest.approx(4.0)
    assert memory[f"{MEMORY_SOURCE}:knock_fault_flag"]["identifier"] == "0xFFFF479D"
    assert memory[f"{MEMORY_SOURCE}:knock_fault_flag"]["response_payload_bytes"] == 1


def test_memory_weighted_beyond_the_budget_shows_infeasible_in_the_preview() -> None:
    """Every catalog window at one read per 0.05 s window is 26 reads x 20 Hz,
    far over a 10 req/s budget; weighting them down to 0.05 brings the same
    selection back inside it. The preview says which, as it does for DIDs."""
    every = tuple(channel.name for channel in memory_channels())
    heavy = _profile(memory=every, did_cycle_seconds=0.05, bus_budget_override=10.0)
    plan = preview_polling_plan(EXAMPLE, heavy)
    windows = {m["channel"] for m in plan["members"] if m["kind"] == "memory"}
    assert len(windows) == len({c.window or c.name for c in memory_channels()})
    assert plan["fallback_feasible"] is False
    assert plan["best_case_feasible"] is False

    light = heavy.model_copy(update={"memory_rate_weights": dict.fromkeys(every, 0.005)})
    light = CaptureProfile.model_validate(light.model_dump())
    light_plan = preview_polling_plan(EXAMPLE, light)
    memory_rps = sum(m["rate_hz"] for m in light_plan["members"] if m["kind"] == "memory")
    assert memory_rps == pytest.approx(len(windows) * 0.1)
    assert sum(m["rate_hz"] for m in plan["members"] if m["kind"] == "memory") == pytest.approx(
        len(windows) * 20.0
    )


# ---------------------------------------------------------------------------
# Capture quality scores a memory read by the channels it decoded
# ---------------------------------------------------------------------------
#
# A memory plan member is named after its window (``mem:knock_fault_flag``);
# the ledger and the rows count readings (``mem:ECM_KS_FAULT_FLAGS``). Scored
# under the window name, every window was reported ``missing`` and landed in
# ``unanswered_input`` although every read was answered.


def _memory_rows(quality: dict[str, Any]) -> dict[str, str]:
    return {row["channel"]: row["status"] for row in quality["channels"] if row["kind"] == "memory"}


def _unanswered(quality: dict[str, Any]) -> list[str]:
    return [
        name
        for failure in quality["failures"]
        if failure["clause"] == "unanswered_input"
        for name in failure["channels"]
    ]


@pytest.mark.parametrize("silent_window", [None, 0xFFFF280C])
def test_the_live_verdict_scores_memory_reads_by_their_decoded_channels(
    clock: FakeClock, silent_window: int | None
) -> None:
    from src.collection import capture_lifetime
    from src.collection.capture_quality import evaluate_capture_quality
    from src.collection.obd_scheduler import ObdSchedulerMetrics

    profile = _profile(memory=(FAULT, OTHER, LEVEL_1), memory_rate_weights={OTHER: 0.5})
    tuning_profile.activate(profile, source="test")
    capture_lifetime.begin(clock.monotonic(), profile=profile.name, generation=1)
    bus = ScriptedEcu(
        clock,
        session_gated=True,
        silent_memory=() if silent_window is None else (silent_window,),
    )
    snapshots: list[ObdSchedulerMetrics] = []
    try:
        run_can_obd_loop(
            bus,  # type: ignore[arg-type]
            _spec((FAULT, OTHER, LEVEL_1)),
            on_frame=lambda _m: None,
            on_reading=lambda _r: None,
            stop_event=threading.Event(),
            deadline_monotonic=clock.monotonic() + 20.0,
            on_scheduler_metrics=snapshots.append,
            scheduler_metrics_interval_seconds=1.0,
        )
        snapshot = capture_lifetime.snapshot(clock.monotonic())
        assert snapshot is not None
        lifetime = snapshot.to_dict()
    finally:
        capture_lifetime.end()
    scheduler = snapshots[-1].to_dict()
    plan_memory = [m for m in scheduler["resolved_plan"]["members"] if m["kind"] == "memory"]
    assert sorted(m["decoded_channels"] for m in plan_memory) == sorted(
        [[f"{MEMORY_SOURCE}:{FAULT}"], [f"{MEMORY_SOURCE}:{OTHER}"], [f"{MEMORY_SOURCE}:{LEVEL_1}"]]
    )
    quality = evaluate_capture_quality(
        profile=profile.model_dump(mode="json"), scheduler=scheduler, lifetime=lifetime
    ).to_dict()
    rows = _memory_rows(quality)
    # Scored under the channels, never under a window name.
    assert set(rows) == {FAULT, OTHER, LEVEL_1}
    if silent_window is None:
        assert "missing" not in rows.values()
        assert not set(_unanswered(quality)) & set(rows)
    else:
        # The control: a window the ECU never answers is still reported.
        assert rows[OTHER] == "missing"
        assert OTHER in _unanswered(quality)
        assert rows[FAULT] != "missing" and rows[LEVEL_1] != "missing"


def _plan_member(window: str, address: int, decoded: list[str] | None) -> dict[str, Any]:
    member: dict[str, Any] = {
        "kind": "memory",
        "service": "0x23",
        "request_id": "0x7E0",
        "identifier": f"0x{address:04X}",
        "channel": f"{MEMORY_SOURCE}:{window}",
        "profile_channel": None,
        "rate_hz": 1.0,
        "configured_rate_hz": 1.0,
        "capture_selected": True,
    }
    if decoded is not None:
        member["decoded_channels"] = decoded
    return member


#: The plan written at arm time since D03.38: one member per window.
_NEW_PLAN = [
    _plan_member("knock_fault_flag", 0xFFFF479D, [f"{MEMORY_SOURCE}:{FAULT}"]),
    _plan_member("injector_dead_time", 0xFFFF280C, [f"{MEMORY_SOURCE}:{OTHER}"]),
]
#: A plan written before it: every window folded into one member at identifier 0.
_OLD_PLAN = [_plan_member("knock_fault_flag", 0x0000, None)]


@pytest.mark.parametrize("plan", [_NEW_PLAN, _OLD_PLAN], ids=["new-plan", "pre-d0338-plan"])
@pytest.mark.parametrize("answered", [(FAULT, OTHER), (FAULT,)], ids=["all", "one-silent"])
def test_the_stop_verdict_scores_memory_from_the_manifest_plan(
    tmp_path: Path, plan: list[dict[str, Any]], answered: tuple[str, ...]
) -> None:
    """The recompute path (``manifest.resolved_polling_plan`` over persisted
    rows), for a plan written since D03.38 and for one written before it."""
    from src.analysis.capture_coverage import recompute_capture_quality
    from tests.builders import CAPTURE_T0, capture_obd_record, write_capture_session

    records = [
        capture_obd_record(CAPTURE_T0 + k, f"{MEMORY_SOURCE}:{name}", round_id=k)
        for k in range(30)
        for name in answered
    ]
    session = write_capture_session(
        tmp_path,
        records,
        profile={
            "mode01": {},
            "required_tuple": [],
            "min_achieved_rate_hz": None,
            "target_rate_hz": None,
            "memory": [FAULT, OTHER],
        },
    )
    manifest_path = session / "capture_profile.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["resolved_polling_plan"] = {"members": plan}
    manifest_path.write_text(json.dumps(manifest))

    quality = recompute_capture_quality(session, ended_at=CAPTURE_T0 + 30).to_dict()
    rows = _memory_rows(quality)
    assert set(rows) == {FAULT, OTHER}
    assert rows[FAULT] != "missing"
    if OTHER in answered:
        assert "missing" not in rows.values()
        assert not _unanswered(quality)
    else:
        assert rows[OTHER] == "missing"
        assert _unanswered(quality) == [OTHER]
