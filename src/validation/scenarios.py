"""The C10 scenario table: one named workload per row of the task's table.

Profiles are written as plain mappings, not loaded from the vehicle catalogue,
so the *same* workload runs before and after an optimisation even when the
two trees disagree about which profile fields exist (``build_profile`` drops
what a tree does not know and the report says so). Every scenario declares
the evidence the task table asks for and, for the deliberately overloaded
cases, the named failure it must produce.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from typing import Any

from src.collection.capture_profiles import CaptureProfile
from src.collection.obd_scheduler import CanObdSpec
from src.validation.synthetic_bus import BroadcastPlan

#: Every Mode 01 channel the synthetic ECU answers, by registry name.
MODE01_CHANNELS: tuple[str, ...] = (
    "FUEL_STATUS",
    "ENGINE_LOAD",
    "COOLANT_TEMP",
    "SHORT_FUEL_TRIM_1",
    "LONG_FUEL_TRIM_1",
    "SHORT_FUEL_TRIM_2",
    "LONG_FUEL_TRIM_2",
    "RPM",
    "SPEED",
    "TIMING_ADVANCE",
    "INTAKE_TEMP",
    "MAF",
    "THROTTLE_POS",
    "RUN_TIME",
    "O2_WR_LAMBDA_S1",
    "EVAPORATIVE_PURGE",
    "BAROMETRIC_PRESSURE",
    "COMMANDED_EQUIV_RATIO",
)

TUNING_DIDS: tuple[str, ...] = (
    "ECM_RPM",
    "ECM_AF_SENSOR_V_B1",
    "ECM_AF_SENSOR_V_B2",
    "ECM_INJECTOR_PW_B1",
    "ECM_INTAKE_CAM_ANGLE_MEASURED",
    "ECM_INTAKE_CAM_ANGLE_COMMANDED",
    "ECM_INTAKE_CAM_ANGLE_MEASURED_B",
    "ECM_INTAKE_VVT_SOLENOID_DUTY",
    "ECM_INTAKE_VVT_SOLENOID_DUTY_CANDIDATE_B2",
    "ECM_THROTTLE_POS_SENSOR_1",
    "ECM_COOLANT_TEMP",
)
FUEL_DIDS: tuple[str, ...] = (
    "ECM_RPM",
    "ECM_AF_SENSOR_V_B1",
    "ECM_AF_SENSOR_V_B2",
    "ECM_INJECTOR_PW_B1",
    "ECM_THROTTLE_POS_SENSOR_1",
    "ECM_INTAKE_AIR_TEMP",
    "ECM_MAF_GPS",
)
HYBRID_DIDS: tuple[str, ...] = (
    "ECM_RPM",
    "ECM_AF_SENSOR_V_B1",
    "ECM_AF_SENSOR_V_B2",
    "ECM_INJECTOR_PW_B1",
    "ECM_INTAKE_CAM_ANGLE_MEASURED",
    "ECM_INTAKE_CAM_ANGLE_COMMANDED",
    "ECM_INTAKE_CAM_ANGLE_MEASURED_B",
    "ECM_THROTTLE_POS_SENSOR_1",
    "ECM_MAF_GPS",
)
ALL_DIDS: tuple[str, ...] = tuple(dict.fromkeys([*TUNING_DIDS, *FUEL_DIDS, *HYBRID_DIDS]))

FUEL_MAP_MODE01: dict[str, float] = {
    "RPM": 1.0,
    "ENGINE_LOAD": 1.0,
    "MAF": 1.0,
    "TIMING_ADVANCE": 1.0,
    "THROTTLE_POS": 1.0,
    "O2_WR_LAMBDA_S1": 1.0,
    "COMMANDED_EQUIV_RATIO": 1.0,
    "FUEL_STATUS": 1.0,
    "INTAKE_TEMP": 0.25,
    "SPEED": 0.25,
    "COOLANT_TEMP": 0.25,
    "SHORT_FUEL_TRIM_1": 0.25,
    "SHORT_FUEL_TRIM_2": 0.25,
    "LONG_FUEL_TRIM_1": 0.25,
    "LONG_FUEL_TRIM_2": 0.25,
    "EVAPORATIVE_PURGE": 0.25,
}
FUEL_TUPLE: tuple[str, ...] = (
    "RPM",
    "ENGINE_LOAD",
    "FUEL_STATUS",
    "COMMANDED_EQUIV_RATIO",
    "O2_WR_LAMBDA_S1",
)

DEFAULT_PROFILE: dict[str, Any] = {
    "name": "default",
    "description": "Normal telemetry and anomaly-detection collection",
    "mode01": {"AUTO": 1.0},
    "mode22": (),
    "corpus_eligible": True,
    "broadcast_decode": True,
}


def _capture(name: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": name,
        "description": f"C10 synthetic workload: {name}",
        "round_timestamps": True,
        "corpus_eligible": False,
        "broadcast_decode": True,
        "storage_prefix": f"_capture-{name}",
        "capacity_evidence": "C10 synthetic-bus workload; no hardware capacity is claimed here.",
    }
    base.update(fields)
    return base


FUEL_MAP_PROFILE = _capture(
    "fuel-map",
    mode01=FUEL_MAP_MODE01,
    target_rate_hz=4.0,
    bus_budget_override=20.0,
    required_tuple=FUEL_TUPLE,
    min_achieved_rate_hz=3.0,
)
TUNING_DID_PROFILE = _capture(
    "tuning-did",
    mode01={},
    mode01_enabled=False,
    mode22=TUNING_DIDS,
    mode22_rate_weights={"ECM_COOLANT_TEMP": 0.2, "ECM_INTAKE_VVT_SOLENOID_DUTY_CANDIDATE_B2": 0.5},
    target_rate_hz=15.0,
    did_continuous=True,
    bus_budget_override=180.0,
    min_achieved_rate_hz=12.0,
)
FUEL_DID_PROFILE = _capture(
    "fuel-did-research",
    mode01={},
    mode01_enabled=False,
    mode22=FUEL_DIDS,
    target_rate_hz=20.0,
    did_continuous=True,
    bus_budget_override=160.0,
    required_tuple=("ECM_RPM", "ECM_MAF_GPS", "ECM_INTAKE_AIR_TEMP", "ECM_INJECTOR_PW_B1"),
    min_achieved_rate_hz=15.0,
)
HYBRID_PROFILE = _capture(
    "tuning-hybrid-research",
    mode01={
        "RPM": 1.0,
        "ENGINE_LOAD": 1.0,
        "MAF": 1.0,
        "TIMING_ADVANCE": 1.0,
        "INTAKE_TEMP": 1.0,
        "COMMANDED_EQUIV_RATIO": 1.0,
        "FUEL_STATUS": 1.0,
        "O2_WR_LAMBDA_S1": 1.0,
    },
    mode22=HYBRID_DIDS,
    did_interval_seconds=0.25,
    did_cycle_seconds=0.25,
    target_rate_hz=4.0,
    bus_budget_override=90.0,
)
#: The same hybrid workload on the **interval** policy instead of the cycle
#: policy: ``did_cycle_seconds`` takes precedence wherever it is set
#: (``capture_profiles`` resolves cycle → interval → collection defaults), so
#: dropping it — and nothing else — selects the older end-anchored behaviour.
#: C04.12 asks for equivalent captures compared across the two policies; this
#: is the workstation half of that comparison.
HYBRID_INTERVAL_PROFILE = {
    **{k: v for k, v in HYBRID_PROFILE.items() if k != "did_cycle_seconds"},
    "name": "tuning-hybrid-interval",
    "storage_prefix": "_capture-tuning-hybrid-interval",
}

RATE_OVERLOAD_PROFILE = _capture(
    "fuel-map-overload",
    mode01=FUEL_MAP_MODE01,
    target_rate_hz=4.0,
    bus_budget_override=4.0,
    required_tuple=FUEL_TUPLE,
    min_achieved_rate_hz=3.0,
)


#: Captures are armed after the collector's staggered initial due times have
#: all passed (31 requests at the 8 req/s default spacing = 3.9 s). Armed
#: earlier, a continuous-DID profile finds no DID due at that instant and
#: ``DidWindowController.session_control_due`` defers its first window by the
#: *default* 60 s interval, and Mode 01 members keep the due time the default
#: profile gave them (medium tier: 3 s) — both reproduced while building this
#: harness (``docs/sustained-validation.md``, findings). The workload is
#: unchanged; the arm instant is chosen so a scenario measures its path
#: rather than the arm-time deferrals, which are reported on their own.
ARMED_WARMUP_S = 5.0


@dataclass(frozen=True)
class TimelineAction:
    """Something the harness does to the running daemon at ``at_s`` after arm."""

    at_s: float
    action: str  # pause | resume | activate:<profile-name>
    profile: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class StorageStall:
    """Sleep inside every writer flush between ``start_s`` and ``end_s``
    (seconds after the writer was built) — a deliberately wedged disk."""

    start_s: float
    end_s: float
    sleep_s: float


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    kind: str  # canable | passive | replay
    table_row: str
    profile: Mapping[str, Any]
    duration_s: float
    warmup_s: float = ARMED_WARMUP_S
    spec_overrides: Mapping[str, Any] = field(default_factory=dict)
    ecu: Mapping[str, Any] = field(default_factory=dict)
    broadcast: BroadcastPlan = field(default_factory=BroadcastPlan)
    timeline: tuple[TimelineAction, ...] = ()
    inference_tick_s: float = 1.0
    storage_stall: StorageStall | None = None
    queue_max_size: int = 10_000
    fault_at_elapsed_s: float | None = None
    required_evidence: tuple[str, ...] = ()
    expected_failures: tuple[str, ...] = ()
    #: Names of which **at least one** must appear, for a scenario whose
    #: outcome has more than one legitimate name. Starvation is the case that
    #: forced this: a channel answering too slowly yields ``rate_shortfall``,
    #: one never reaching the wire at all yields ``unanswered_input``, and the
    #: C10.12 sweep measured the boundary between them sitting right at the
    #: overload scenario's budget. Declaring one name made the scenario's own
    #: "expected failure met" flag depend on host timing.
    expected_failures_any: tuple[str, ...] = ()
    notes: str = ""
    known_answer: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "table_row": self.table_row,
            "profile": dict(self.profile),
            "duration_s": self.duration_s,
            "warmup_s": self.warmup_s,
            "spec_overrides": {k: _jsonable(v) for k, v in self.spec_overrides.items()},
            "ecu": {k: _jsonable(v) for k, v in self.ecu.items()},
            "broadcast": self.broadcast.__dict__,
            "timeline": [
                {"at_s": a.at_s, "action": a.action, "profile": a.profile and a.profile["name"]}
                for a in self.timeline
            ],
            "inference_tick_s": self.inference_tick_s,
            "storage_stall": None if self.storage_stall is None else self.storage_stall.__dict__,
            "queue_max_size": self.queue_max_size,
            "fault_at_elapsed_s": self.fault_at_elapsed_s,
            "required_evidence": list(self.required_evidence),
            "expected_failures": list(self.expected_failures),
            "expected_failures_any": list(self.expected_failures_any),
            "notes": self.notes,
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def build_profile(mapping: Mapping[str, Any]) -> tuple[CaptureProfile, list[str]]:
    """A ``CaptureProfile`` from a scenario mapping, minus the fields this
    tree's schema does not know (returned so the report can say so)."""
    known = set(CaptureProfile.model_fields)
    accepted = {k: v for k, v in mapping.items() if k in known}
    dropped = sorted(k for k in mapping if k not in known)
    return CaptureProfile(**accepted), dropped


def build_spec_kwargs(mapping: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Filter ``CanObdSpec`` kwargs the same way (C09 added several)."""
    known = {f.name for f in dataclass_fields(CanObdSpec)}
    return {k: v for k, v in mapping.items() if k in known}, sorted(
        k for k in mapping if k not in known
    )


STEADY = BroadcastPlan(rate_hz=100.0, burst_size=100, burst_interval_s=2.5)
QUIET = BroadcastPlan(rate_hz=50.0)
FLOOD = BroadcastPlan(rate_hz=400.0, burst_size=300, burst_interval_s=1.0)

SCENARIOS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        name="normal-monitoring",
        kind="canable",
        table_row="Normal monitoring with discovery",
        profile=DEFAULT_PROFILE,
        duration_s=12.0,
        spec_overrides={
            "pids": ("auto",),
            "poll_rate_hz": 1.0,
            "batch_max_pids": 6,
            "dtc_poll_interval_seconds": 5.0,
        },
        ecu={"latency": 0.005, "supports_mode01_map": True},
        broadcast=STEADY,
        required_evidence=(
            "critical-input gaps",
            "probe cost",
            "fairness",
            "startup/retry raw-frame continuity",
        ),
        notes="Support discovery walks the Mode 01 bitmap groups; the DTC poll is "
        "unanswered by the synthetic ECU (a timeout, counted).",
    ),
    ScenarioSpec(
        name="fuel-map",
        kind="canable",
        table_row="Fuel-map",
        profile=FUEL_MAP_PROFILE,
        duration_s=10.0,
        spec_overrides={"batch_max_pids": 6},
        ecu={"latency": 0.005},
        broadcast=STEADY,
        required_evidence=(
            "complete required fueling tuples",
            "actual skew",
            "mixed-rate context",
            "batch occupancy",
        ),
    ),
    ScenarioSpec(
        name="tuning-did",
        kind="canable",
        table_row="Continuous tuning DID",
        profile=TUNING_DID_PROFILE,
        duration_s=10.0,
        spec_overrides={"did_batch_max_dids": 3},
        ecu={"latency": 0.002, "session_gated": True},
        broadcast=STEADY,
        required_evidence=(
            "requested DID cadence",
            "raw frame continuity",
            "per-DID context weights",
            "session continuity",
        ),
    ),
    ScenarioSpec(
        name="fuel-did-research",
        kind="canable",
        table_row="Fuel DID research",
        profile=FUEL_DID_PROFILE,
        duration_s=10.0,
        spec_overrides={"did_batch_max_dids": 3},
        ecu={"latency": 0.002, "session_gated": True},
        broadcast=STEADY,
        required_evidence=(
            "single/batched request demand",
            "measured width/capability",
            "persisted tuple yield",
        ),
    ),
    ScenarioSpec(
        name="hybrid-mode01-mode22",
        kind="canable",
        table_row="Hybrid Mode 01/22",
        profile=HYBRID_PROFILE,
        duration_s=10.0,
        spec_overrides={"batch_max_pids": 6, "did_batch_max_dids": 3},
        ecu={"latency": 0.005, "session_gated": True},
        broadcast=STEADY,
        required_evidence=(
            "both services progress",
            "entry/exit delays",
            "cycle deadlines",
            "cross-mode pairing limits",
        ),
    ),
    ScenarioSpec(
        name="hybrid-interval-policy",
        kind="canable",
        table_row="Hybrid Mode 01/22",
        profile=HYBRID_INTERVAL_PROFILE,
        duration_s=10.0,
        spec_overrides={"batch_max_pids": 6, "did_batch_max_dids": 3},
        ecu={"latency": 0.005, "session_gated": True},
        broadcast=STEADY,
        required_evidence=(
            "cadence and gaps under the interval policy",
            "transition timing",
            "complete tuples",
        ),
        notes=(
            "The hybrid workload with did_cycle_seconds dropped, so the interval policy "
            "schedules the Mode 22 windows. Paired with hybrid-mode01-mode22 for C04.12."
        ),
    ),
    ScenarioSpec(
        name="passive-sniffer",
        kind="passive",
        table_row="Passive / legacy ELM327 companion",
        profile=DEFAULT_PROFILE,
        duration_s=8.0,
        broadcast=STEADY,
        required_evidence=(
            "receive-loop responsiveness",
            "raw capture completeness",
            "no sniffer sends",
        ),
        known_answer=False,
        notes="The ELM327 companion is not fakeable offline without the "
        "ELM327-emulator serial integration tier; declared not exercised.",
    ),
    ScenarioSpec(
        name="fault-channels",
        kind="canable",
        table_row="Fault and transition cases (silent/unsupported/delayed)",
        profile=FUEL_MAP_PROFILE,
        duration_s=10.0,
        spec_overrides={"batch_max_pids": 6, "request_timeout_seconds": 0.25},
        ecu={"latency": 0.06, "silent_pids": (0x0F,), "nrc_pids": (0x2E,)},
        broadcast=STEADY,
        required_evidence=("silent channel", "unsupported channel", "delayed response"),
        expected_failures=("unanswered_input",),
        notes="INTAKE_TEMP never answers, EVAPORATIVE_PURGE is refused (NRC 0x12), "
        "and every reply takes 60 ms.",
    ),
    ScenarioSpec(
        name="transitions",
        kind="canable",
        table_row="Fault and transition cases (pause/profile change/reconnect/flush)",
        profile=FUEL_MAP_PROFILE,
        spec_overrides={"batch_max_pids": 6, "did_batch_max_dids": 3},
        ecu={"latency": 0.005, "session_gated": True},
        broadcast=STEADY,
        duration_s=13.5,
        timeline=(
            TimelineAction(5.0, "pause"),
            TimelineAction(6.5, "resume"),
            TimelineAction(9.5, "activate", TUNING_DID_PROFILE),
        ),
        # Adapter fault 3 s after arm (8.0 s after the collector started):
        # the rebuilt loop's staggered initial dues have all passed before the
        # profile change at 9.5 s, so the change measures the transition and
        # not the arm-time deferral (see ARMED_WARMUP_S).
        fault_at_elapsed_s=ARMED_WARMUP_S + 3.0,
        required_evidence=("pause", "profile change", "reconnect", "final flush"),
        expected_failures=("unanswered_input",),
        notes="Quality is judged against the fuel-map manifest the capture was armed "
        "with, but tuning_profile.activate restarts the capture ledger, so the "
        "stop-time interval is the tuning-did phase and every fuel-map channel is "
        "an unanswered input there — the named failure is expected. The pause "
        "window and the fuel-map phase are kept in generation_events.",
    ),
    ScenarioSpec(
        name="overload-storage-stall",
        kind="canable",
        table_row="Overload (accounting proof)",
        profile=FUEL_MAP_PROFILE,
        duration_s=9.0,
        spec_overrides={"batch_max_pids": 6},
        ecu={"latency": 0.005},
        broadcast=FLOOD,
        # Writer-relative: the writer is built before the warmup, so the stall
        # sits inside the armed interval (arm at ~5.2 s, stop at ~14.2 s).
        storage_stall=StorageStall(
            start_s=ARMED_WARMUP_S + 2.0, end_s=ARMED_WARMUP_S + 7.5, sleep_s=2.5
        ),
        queue_max_size=256,
        required_evidence=("accepted + dropped == injected",),
        expected_failures=("persistence_loss",),
        notes="A wedged writer under a flooded bus with a small queue: loss must "
        "be counted at the stage it happens, never hidden.",
    ),
    ScenarioSpec(
        name="overload-rate",
        kind="canable",
        table_row="Overload (rate contract)",
        profile=RATE_OVERLOAD_PROFILE,
        duration_s=9.0,
        spec_overrides={"batch_max_pids": 1},
        ecu={"latency": 0.005},
        broadcast=QUIET,
        required_evidence=("requested contract fails by name",),
        expected_failures_any=("rate_shortfall", "unanswered_input"),
        notes="Sixteen channels at 4 Hz on a 4 req/s budget with batching off.",
    ),
    ScenarioSpec(
        name="replay-drive-slice",
        kind="replay",
        table_row="Replay (C10.9)",
        profile=DEFAULT_PROFILE,
        duration_s=24.0,
        warmup_s=0.0,
        inference_tick_s=0.5,
        required_evidence=("feature/detector behaviour on a known-good session",),
        known_answer=True,
        notes="tests/fixtures/ground_truth/drive-slice at 2x through the isolated "
        "replay path. Labelled replay: no transport or ECU capacity claim.",
    ),
)

SCENARIOS_BY_NAME: dict[str, ScenarioSpec] = {s.name: s for s in SCENARIOS}


def scenario(name: str) -> ScenarioSpec:
    try:
        return SCENARIOS_BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown scenario {name!r}; known: {sorted(SCENARIOS_BY_NAME)}") from None


__all__ = [
    "ALL_DIDS",
    "DEFAULT_PROFILE",
    "FUEL_MAP_PROFILE",
    "MODE01_CHANNELS",
    "SCENARIOS",
    "SCENARIOS_BY_NAME",
    "ScenarioSpec",
    "StorageStall",
    "TimelineAction",
    "build_profile",
    "build_spec_kwargs",
    "scenario",
]
