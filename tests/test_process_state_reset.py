"""R07.4: one reset returns every collection process-global to its start-up value."""

from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import Any

import can

from src.collection import (
    bus_budget,
    capture_generation,
    capture_lifetime,
    discovery,
    queueing,
    tuning_profile,
    tx_gate,
)
from src.collection.capture_profiles import CaptureProfile
from src.collection.kline.sessions import MODULE_SESSIONS
from src.collection.process_state import (
    COVERED_GLOBALS,
    COVERED_MODULES,
    reset_process_state_for_tests,
)
from src.telemetry import TelemetryRecord

COLLECTION = Path(__file__).resolve().parents[1] / "src" / "collection"


def _state() -> dict[str, Any]:
    """Every observable piece of collection process state, as plain data."""
    now = time.monotonic()
    return {
        "tx_gate": tx_gate.snapshot(now=now),
        "bus_budget": bus_budget.snapshot(now=now),
        "profile": tuning_profile.snapshot(),
        "calibration": tuning_profile.calibration_snapshot(),
        "profile_paused": tuning_profile.is_paused(),
        "profile_configured": tuning_profile.is_configured(),
        "did_aliases": dict(tuning_profile.view().aliases),
        "capture_lifetime": capture_lifetime.snapshot(now),
        "generation": capture_generation.snapshot(),
        "discovery_vehicles": discovery.registry_snapshot(now)["vehicles"],
        "drop_logs": dict(queueing._CAN_FRAME_DROP_LOGS),
        "unattributed_drops": queueing._UNATTRIBUTED_QUEUE_DROPS.dropped,
        "kline": MODULE_SESSIONS.snapshot(now=now),
    }


def _dirty_everything() -> None:
    tx_gate.set_passive(True, source="test", ttl_seconds=600)
    tx_gate.claim_ecm_session("foreign")
    tx_gate.note_foreign_tester(tester_address=0xFC, module_address=0x10)
    # Suppressed (passive), so the bus is never touched.
    tx_gate.send(None, can.Message(arbitration_id=0x7DF, data=b"\x02\x01\x0c"), what="test")  # type: ignore[arg-type]
    bus_budget.set_override(5.0, source="test", ttl_seconds=600)
    tuning_profile.activate(
        CaptureProfile(
            name="leaky",
            description="set by a test",
            mode01={"RPM": 1.0},
            corpus_eligible=False,
            storage_prefix="_capture-leaky",
        ),
        session_dir="/nowhere",
        source="test",
    )
    tuning_profile.set_paused(True)
    tuning_profile.configure_default(
        CaptureProfile(
            name="default", description="configured", mode01={"RPM": 1.0}, corpus_eligible=True
        )
    )
    tuning_profile.activate(
        CaptureProfile(
            name="leaky-2",
            description="set by a test after configure_default",
            mode01={"RPM": 1.0},
            corpus_eligible=False,
            storage_prefix="_capture-leaky",
        ),
        source="test",
    )
    tuning_profile.set_paused(True)
    tuning_profile.configure_did_aliases({"ECM_RPM": "DID_1234"})
    tuning_profile.configure_running_tune_revision("rev-x", calibration_id="CAL-X")
    tuning_profile.record_ecu_calibration_id("CAL-Y")
    discovery.remember(discovery.DiscoveryState(key="VIN-X", version=discovery.cache_version()))
    queueing._can_frame_drop_log("test-loop")
    queueing._drop_callback(None)(TelemetryRecord.obd(0.0, "RPM", 1.0, "rpm"))
    MODULE_SESSIONS.claim(0x10, "foreign")


def test_reset_returns_every_global_to_the_start_up_value() -> None:
    reset_process_state_for_tests()
    pristine = _state()
    _dirty_everything()
    dirty = _state()
    # Every piece really moved, so the equality below is not vacuous.
    assert {key for key in pristine if dirty[key] == pristine[key]} == set()
    reset_process_state_for_tests()
    assert _state() == pristine
    assert pristine["tx_gate"]["enabled"] is False
    assert pristine["profile"]["name"] == "default"
    assert pristine["generation"] == {"generation": 0, "reason": "startup"}


class TestTheNextTestSeesTheDefault:
    """The autouse fixture, not a hand reset: the second test must see defaults.

    Under xdist the two may run on different workers, where the second passes
    trivially; run serially (``-n0``) it is the order-dependence check.
    """

    def test_a_sets_every_global(self) -> None:
        _dirty_everything()

    def test_b_sees_the_start_up_state(self) -> None:
        state = _state()
        assert state["tx_gate"]["enabled"] is False
        assert state["tx_gate"]["ecm_session_owner"] == "can_diagnostics"
        assert state["bus_budget"]["override_requests_per_second"] is None
        assert state["profile"]["name"] == "default"
        assert state["profile_paused"] is False
        assert state["profile_configured"] is False
        assert state["did_aliases"] == {}
        assert state["calibration"]["ecu_calibration_id"] is None
        assert state["calibration"]["running_tune_revision"] is None
        assert state["capture_lifetime"] is None
        assert state["generation"]["generation"] == 0
        assert state["discovery_vehicles"] == {}
        assert state["drop_logs"] == {}
        assert state["unattributed_drops"] == 0
        assert state["kline"] == {}


def _mutates_module_globals(path: Path) -> bool:
    tree = ast.parse(path.read_text(), filename=str(path))
    return any(isinstance(node, ast.Global) for node in ast.walk(tree))


def test_every_collection_module_with_globals_is_covered() -> None:
    """A new ``global`` in ``src/collection`` must join the reset, or this fails."""
    mutating = {
        path.relative_to(COLLECTION).as_posix()
        for path in COLLECTION.rglob("*.py")
        if _mutates_module_globals(path)
    }
    assert mutating - COVERED_MODULES == set(), (
        "collection module(s) keep process-global state that "
        "src/collection/process_state.py does not reset"
    )


#: Calls whose result cannot be mutated in place.
_IMMUTABLE_CALLS = frozenset(
    {
        "Lock",
        "Path",
        "RLock",
        "TypeVar",
        "bool",
        "bytes",
        "compile",
        "float",
        "fromhex",
        "frozenset",
        "get_args",
        "getLogger",
        "int",
        "str",
        "tuple",
    }
)
_READ_ONLY_TABLE = "lookup table built at import and only read"
#: Module-level mutable containers/instances that are not process state, each
#: with the reason. A new one fails the test until it is reset in
#: ``process_state`` (``COVERED_GLOBALS``) or justified here.
NOT_STATE: dict[tuple[str, str], str] = {
    **dict.fromkeys(
        [
            ("ads1015.py", "PGA_CODES"),
            ("custom_pids.py", "_ALLOWED_BINOPS"),
            ("custom_pids.py", "_ALLOWED_UNARYOPS"),
            ("custom_pids.py", "_ALLOWED_COMPARE"),
            ("custom_pids.py", "_SAFE_FUNCS"),
            ("custom_pids.py", "_TORQUE_COLUMNS"),
            ("isotp.py", "NRC_NAMES"),
            ("kline/protocol.py", "REFUSED_SUBFUNCTIONS"),
            ("kline/protocol.py", "WRITE_SERVICES"),
            ("kline/protocol.py", "NEGATIVE_RESPONSE_CODES"),
            ("kline/protocol.py", "SERVICE_NAMES"),
            ("obd_registry.py", "MODE01_REGISTRY"),
            ("obd_registry.py", "MODE09_REGISTRY"),
            ("obd_registry.py", "SERVICE_REGISTRY"),
            ("obd_scheduler/cadence.py", "TIER_RATE_FACTORS"),
            ("obd_scheduler/spec.py", "DTC_POLL_CHANNELS"),
            ("obd_scheduler/spec.py", "SERVICE_POLL_CHANNELS"),
            ("obd_scheduler/spec.py", "_PID_IDS_BY_NAME"),
            ("pid_decoders.py", "PID_DECODERS"),
            ("pids.py", "PIDS_BY_NAME"),
            ("request_eligibility.py", "MATRIX"),
            ("tx_gate.py", "_TRUTHY"),
        ],
        _READ_ONLY_TABLE,
    ),
    ("capture_profiles.py", "_SERVING_DID_CACHE"): "keyed by both sources' mtimes; never stale",
    ("capture_profiles.py", "_SERVING_MODE01_CACHE"): "keyed by both sources' mtimes; never stale",
    ("gpio_lines.py", "GPIO_V2_GET_LINE_IOCTL"): "an int computed at import",
    ("gpio_lines.py", "GPIO_V2_LINE_SET_VALUES_IOCTL"): "an int computed at import",
    ("reconnect.py", "DEFAULT_RECONNECT_POLICY"): "frozen dataclass",
    ("request_eligibility.py", "ELIGIBLE"): "frozen dataclass",
    ("request_eligibility.py", "_CONTINUOUS"): "frozen dataclass",
    ("request_eligibility.py", "_NO_MODE22"): "frozen dataclass",
    ("request_eligibility.py", "_MODE01_SIDE_ROW"): _READ_ONLY_TABLE,
    ("request_eligibility.py", "_DID_ROW"): _READ_ONLY_TABLE,
    ("request_eligibility.py", "_ALL_CONTINUOUS"): _READ_ONLY_TABLE,
    ("request_eligibility.py", "_ALL_NO_MODE22"): _READ_ONLY_TABLE,
}


def _module_level_mutables(path: Path) -> set[str]:
    """Names bound at module level to a mutable literal or a non-trivial call."""
    found: set[str] = set()
    for node in ast.parse(path.read_text(), filename=str(path)).body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if not names or names == ["__all__"]:
            continue
        mutable = isinstance(
            value, (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)
        )
        if isinstance(value, ast.Call):
            func = value.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            mutable = called not in _IMMUTABLE_CALLS
        if mutable:
            found.update(names)
    return found


def test_every_module_level_mutable_is_reset_or_justified() -> None:
    """A dict/list/set or object instance held at module level is process state
    until shown otherwise -- ``queueing._UNATTRIBUTED_QUEUE_DROPS`` was missed
    because it is neither a ``global`` statement nor a container."""
    found = {
        (path.relative_to(COLLECTION).as_posix(), name)
        for path in COLLECTION.rglob("*.py")
        for name in _module_level_mutables(path)
    }
    unexplained = found - COVERED_GLOBALS - set(NOT_STATE)
    assert not unexplained, (
        "module-level mutable(s) neither reset in process_state.COVERED_GLOBALS "
        f"nor justified in NOT_STATE: {sorted(unexplained)}"
    )
    stale = (COVERED_GLOBALS | set(NOT_STATE)) - found
    assert not stale, f"entries whose assignment is gone: {sorted(stale)}"
    assert not set(NOT_STATE) & COVERED_GLOBALS
