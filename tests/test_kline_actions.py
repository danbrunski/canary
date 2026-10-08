"""K-line diagnostic functions: the collector runs a queued code clear in a
session of its own, and the control routes offer it only for modules whose
profile records how (the airbag module, sniffed from a scan tool 2026-10-04)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.collection import tx_gate
from src.collection.kline import protocol as kp
from src.collection.kline.actions import (
    KLINE_ACTIONS,
    KlineAction,
    KlineActionResult,
    KlineActions,
)
from src.collection.kline.profile import KlineProfile
from src.collection.kline.sessions import MODULE_SESSIONS
from src.collection.kline_collector import KlineCollector
from src.collection.reconnect import ReconnectPolicy
from src.config import CanaryConfig, KlineConfig
from src.dashboard.api import create_app
from src.telemetry import TelemetryRecord
from tests.waits import HANG_GUARD_SECONDS

REPO_ROOT = Path(__file__).resolve().parents[1]
DIDS = REPO_ROOT / "data/dids.yaml"
PROFILE = REPO_ROOT / "data/nissan-xterra-2008/kline.yaml"
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"
FAST = ReconnectPolicy(initial_delay_seconds=0.01, max_delay_seconds=0.01)
SRS = 0x58


@pytest.fixture(autouse=True)
def _clean_gate() -> Iterator[None]:
    tx_gate.reset_ecm_session_claims()
    tx_gate.set_passive(False, source="test")
    yield
    tx_gate.reset_ecm_session_claims()
    tx_gate.set_passive(False, source="test")


class ClearingModule:
    """Answers the clear as the airbag module did: responsePending is the
    transport's to absorb, so the fake returns the positive reply's (empty)
    parameters, or the configured refusal."""

    def __init__(self, profile: KlineProfile, refuse: int | None = None) -> None:
        self.profile = profile
        self.refuse = refuse
        self.calls: list[str] = []

    def open(self) -> None:
        self.calls.append("open")

    def close(self) -> None:
        self.calls.append("close")

    def start_communication(self, target: int | None = None) -> kp.StartCommunicationReply:
        self.calls.append(f"start {target:02x}" if target is not None else "start")
        return kp.StartCommunicationReply(0xFC, SRS, b"\x6d\x8f")

    def request(self, payload: bytes) -> bytes:
        kp.check_request(payload)
        self.calls.append(f"request {payload.hex()}")
        if self.refuse is not None:
            raise kp.NegativeResponseError(payload[0], self.refuse)
        return b""

    def stop_communication(self) -> None:
        self.calls.append("stop")


def _collector(
    actions: KlineActions, transports: list[ClearingModule], **kwargs: object
) -> KlineCollector:
    def factory(profile: KlineProfile, mode: object) -> ClearingModule:
        transport = ClearingModule(profile, **kwargs)  # type: ignore[arg-type]
        transports.append(transport)
        return transport

    return KlineCollector(
        KlineConfig(enabled=True, poll_interval_seconds=0.01, sniff=False),
        PROFILE,
        DIDS,
        entries=lambda: (),
        transport_factory=factory,  # type: ignore[arg-type]
        reconnect_policy=FAST,
        actions=actions,
    )


def _clear() -> KlineAction:
    return KlineAction(kind="clear_codes", module="SRS", address=SRS, request=b"\x14")


async def _run_one(
    actions: KlineActions, collector: KlineCollector, action: KlineAction
) -> KlineActionResult:
    stop = asyncio.Event()
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    task = asyncio.create_task(collector.run(queue, stop))
    await asyncio.sleep(0)
    future = actions.submit(action)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=HANG_GUARD_SECONDS)
    finally:
        stop.set()
        await task


async def test_a_queued_clear_runs_in_a_session_of_its_own() -> None:
    actions = KlineActions()
    transports: list[ClearingModule] = []
    result = await _run_one(actions, _collector(actions, transports), _clear())
    assert result.ok and result.reply_hex == "54"
    assert transports[0].calls == ["open", "start 58", "request 14", "stop", "close"]
    assert MODULE_SESSIONS.owner(SRS) == "none"  # released
    assert not actions.available  # the collector detached on stop


async def test_a_refused_clear_reports_the_refusal() -> None:
    actions = KlineActions()
    transports: list[ClearingModule] = []
    result = await _run_one(actions, _collector(actions, transports, refuse=0x22), _clear())
    assert not result.ok
    assert "SRS refused" in result.detail
    assert transports[0].calls[-1] == "close"


async def test_passive_mode_refuses_without_touching_the_line() -> None:
    tx_gate.set_passive(True, source="test")
    actions = KlineActions()
    transports: list[ClearingModule] = []
    result = await _run_one(actions, _collector(actions, transports), _clear())
    assert not result.ok and "passive" in result.detail
    assert transports == []


def test_finishing_a_cancelled_action_does_not_raise() -> None:
    """A waiter that gave up cancels the future; the collector finishing the
    action afterwards must not crash its loop (InvalidStateError, seen under
    load in the push gate 2026-10-05)."""
    action = _clear()
    action.future.cancel()
    action.finish(ok=True, started=0.0, reply=b"\x54", detail="positive response")
    assert action.future.cancelled()


def test_one_function_at_a_time_and_none_without_a_collector() -> None:
    actions = KlineActions()
    with pytest.raises(RuntimeError, match="not running"):
        actions.submit(_clear())
    actions.attach()
    actions.submit(_clear())
    with pytest.raises(RuntimeError, match="already queued"):
        actions.submit(_clear())
    pending = actions.take()
    assert pending is not None
    actions.detach()


def test_the_airbag_module_records_its_clear() -> None:
    from src.collection.kline.profile import load_kline_profile

    module = load_kline_profile(PROFILE).modules[SRS]
    assert module.name == "SRS"
    assert module.clear_codes is not None and module.clear_codes.request == (0x14,)


# --------------------------------------------------------------------------- routes


def _api(tmp_path: Path) -> TestClient:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config = config.model_copy(
        update={"daemon": config.daemon.model_copy(update={"kline_profile": PROFILE})}
    )
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    return TestClient(app)


AUTH = {"Authorization": "Bearer secret"}


def test_functions_lists_only_modules_with_a_recorded_clear(tmp_path: Path) -> None:
    client = _api(tmp_path)
    assert client.get("/control/diagnostics/functions").status_code == 401
    body = client.get("/control/diagnostics/functions", headers=AUTH).json()
    functions = body["diagnostic_functions"]["functions"]
    assert [(f["module"], f["address"], f["request_hex"]) for f in functions] == [
        ("SRS", "0x58", "14")
    ]


def test_clear_refuses_what_it_cannot_do(tmp_path: Path) -> None:
    client = _api(tmp_path)

    def post(module: str) -> httpx.Response:
        return client.post(
            "/control/diagnostics/clear_codes", json={"module": module}, headers=AUTH
        )

    assert post("TCM").status_code == 404
    ecm = post("ECM")
    assert ecm.status_code == 409 and "no clear_codes" in ecm.json()["detail"]
    assert post("SRS").status_code == 503  # no collector in a bare app
    tx_gate.set_passive(True, source="test")
    KLINE_ACTIONS.attach()
    try:
        armed = post("SRS")
        assert armed.status_code == 409 and "passive" in armed.json()["detail"]
    finally:
        KLINE_ACTIONS.detach()


def test_clear_returns_the_collectors_result(tmp_path: Path) -> None:
    client = _api(tmp_path)
    KLINE_ACTIONS.attach()
    done = threading.Event()

    def collector() -> None:
        while not done.is_set():
            action = KLINE_ACTIONS.take()
            if action is not None:
                assert (action.address, action.request) == (SRS, b"\x14")
                action.finish(ok=True, started=0.0, reply=b"\x54", detail="positive response")
                KLINE_ACTIONS.done(action)
                return
            done.wait(0.01)

    worker = threading.Thread(target=collector)
    worker.start()
    try:
        response = client.post(
            "/control/diagnostics/clear_codes", json={"module": "srs"}, headers=AUTH
        )
    finally:
        done.set()
        worker.join()
        KLINE_ACTIONS.detach()
    assert response.status_code == 200
    body = response.json()["clear_codes"]
    assert (body["module"], body["address"], body["ok"], body["reply_hex"]) == (
        "SRS",
        "0x58",
        True,
        "54",
    )


async def test_a_module_off_the_ecm_runs_during_an_ecu_backup() -> None:
    """W08.7 holds the ECM's diagnostic session, not the K-line wire: a code
    clear on the airbag module goes ahead while the backup reads."""
    tx_gate.claim_ecm_session("rom_backup", ttl_seconds=60.0)
    actions = KlineActions()
    transports: list[ClearingModule] = []
    result = await _run_one(actions, _collector(actions, transports), _clear())
    assert result.ok, result.detail
    assert transports and transports[0].calls[:2] == ["open", "start 58"]


async def test_an_ecm_function_is_refused_while_the_ecu_backup_holds_it() -> None:
    tx_gate.claim_ecm_session("rom_backup", ttl_seconds=60.0)
    actions = KlineActions()
    transports: list[ClearingModule] = []
    ecm = KlineAction(kind="clear_codes", module="ECM", address=0x10, request=b"\x14")
    result = await _run_one(actions, _collector(actions, transports), ecm)
    assert not result.ok
    assert result.detail == "refused: the ECU backup holds the ECM session"
    assert transports == []  # not one byte, not even the transceiver
    assert tx_gate.snapshot()["ecm_session_claims"].keys() == {"rom_backup"}


async def test_an_ecm_function_is_refused_while_the_ram_verifier_holds_it() -> None:
    """D03.38: the RAM verifier holds the ECM's session the way the backup
    does, so a K-line ECM function waits it out, named."""
    tx_gate.claim_ecm_session("memory_verify", ttl_seconds=60.0)
    actions = KlineActions()
    transports: list[ClearingModule] = []
    ecm = KlineAction(kind="clear_codes", module="ECM", address=0x10, request=b"\x14")
    result = await _run_one(actions, _collector(actions, transports), ecm)
    assert not result.ok
    assert result.detail == "refused: the RAM verifier holds the ECM session"
    assert transports == []
    assert tx_gate.snapshot()["ecm_session_claims"].keys() == {"memory_verify"}
