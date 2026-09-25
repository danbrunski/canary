"""The shared bus-tool module (R06.1): no transmitting tool opens the bus
without a passive verdict.

``probe_local_identifier`` and ``scan_constant_dids`` used to open the CAN bus
with no passive check at all, so a hand run beside the live daemon interleaved
its session and requests with the daemon's scheduler. The refusal tests below
stub only the HTTP answer (``urlopen``) and the config loader, so they hold
whatever module the passive check lives in.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
from pathlib import Path
from typing import Any

import pytest

from src.config import CanaryConfig
from src.control import passive_client
from tools import _bus_tool, probe_local_identifier, scan_constant_dids

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@pytest.fixture
def daemon_transmitting(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The daemon answers ``GET /control/passive`` with passive OFF, and any
    attempt to load the bus config (the first step of opening the bus) fails
    the test."""
    urls: list[str] = []

    def _urlopen(request: Any, timeout: float) -> _FakeResponse:
        urls.append(request.full_url)
        body = {"passive": {"enabled": False, "ttl_remaining_seconds": None}}
        return _FakeResponse(json.dumps(body).encode())

    def _never(_path: Any) -> Any:
        raise AssertionError("the bus was opened while the daemon was transmitting")

    monkeypatch.setenv("CANARY_CONTROL_TOKEN", "tok")
    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    monkeypatch.setattr(CanaryConfig, "from_yaml", staticmethod(_never))
    return urls


def test_probe_local_identifier_refuses_while_daemon_transmits(
    daemon_transmitting: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = probe_local_identifier.main(["--config", "unused.yaml"])
    assert rc == 2
    assert "refusing" in capsys.readouterr().err
    assert daemon_transmitting == ["http://127.0.0.1:8000/control/passive"]


@pytest.mark.parametrize("transport", [["--canable"], ["--port", "/dev/ttyUSB0"]])
def test_scan_constant_dids_refuses_while_daemon_transmits(
    daemon_transmitting: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    transport: list[str],
) -> None:
    def _no_serial(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the serial adapter was opened while the daemon was transmitting")

    monkeypatch.setattr(scan_constant_dids, "_open_serial", _no_serial)
    rc = scan_constant_dids.main([*transport, "--config", "unused.yaml"])
    assert rc == 2
    assert "refusing" in capsys.readouterr().err
    assert daemon_transmitting == ["http://127.0.0.1:8000/control/passive"]


# ---------------------------------------------------------------------------
# the verdict is required, not remembered
# ---------------------------------------------------------------------------


def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    _bus_tool.add_bus_tool_args(parser)
    return parser.parse_args(list(argv))


def test_open_client_refuses_anything_but_a_verdict() -> None:
    args = _args()
    with pytest.raises(TypeError, match="PassiveVerdict"):
        with _bus_tool.open_client(args, True):  # type: ignore[arg-type]
            pass


def test_a_verdict_cannot_be_constructed_by_hand() -> None:
    with pytest.raises(TypeError, match="require_passive"):
        _bus_tool.PassiveVerdict("armed", None, 5.0, object())


def test_require_passive_refuses_disarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _bus_tool,
        "http_passive_checker",
        lambda *a, **k: lambda: passive_client.PassiveStatus(False, None, "passive mode is off"),
    )
    with pytest.raises(_bus_tool.PassiveRefusedError, match="passive mode is off"):
        _bus_tool.require_passive(_args())


def test_require_passive_refuses_a_ttl_shorter_than_the_check_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _bus_tool,
        "http_passive_checker",
        lambda *a, **k: lambda: passive_client.PassiveStatus(True, 2.0, "armed"),
    )
    with pytest.raises(_bus_tool.PassiveRefusedError, match="TTL"):
        _bus_tool.require_passive(_args("--passive-check-seconds", "5"))


def test_require_passive_armed_carries_the_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    status = passive_client.PassiveStatus(True, 600.0, "armed")
    monkeypatch.setattr(_bus_tool, "http_passive_checker", lambda *a, **k: lambda: status)
    verdict = _bus_tool.require_passive(_args())
    assert verdict.source == "armed"
    assert verdict.checker is not None and verdict.checker() == status


def test_no_daemon_skips_the_http_check(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("--no-daemon must not query the daemon")

    monkeypatch.setattr(_bus_tool, "http_passive_checker", _boom)
    verdict = _bus_tool.require_passive(_args("--no-daemon"))
    assert verdict.source == "no_daemon" and verdict.checker is None


def test_one_daemon_url_default() -> None:
    """``verify_memory_channels`` defaulted to ``localhost``, the rest to 127.0.0.1."""
    assert _args().daemon_url == passive_client.DEFAULT_DAEMON_URL == "http://127.0.0.1:8000"


# ---------------------------------------------------------------------------
# structure: no tool reaches into another tool's private names, and every
# transmitting tool opens the bus through _bus_tool.open_client
# ---------------------------------------------------------------------------


def _tool_sources() -> list[Path]:
    return sorted(p for p in TOOLS_DIR.glob("*.py") if p.name != "__init__.py")


def test_no_tool_imports_another_tools_private_names() -> None:
    offenders: list[str] = []
    for path in _tool_sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if not node.module.startswith("tools.") or node.module == "tools._bus_tool":
                continue
            private = [alias.name for alias in node.names if alias.name.startswith("_")]
            if private:
                offenders.append(f"{path.name}: from {node.module} import {private}")
    assert offenders == []


#: Tools that build a ``CanObdClient`` of their own. Every one opens it through
#: ``_bus_tool.open_client``, which needs a verdict.
TRANSMITTING_TOOLS = (
    "batch_probe.py",
    "can_obd_probe.py",
    "can_memory_log.py",
    "did_log.py",
    "did_sweep.py",
    "probe_local_identifier.py",
    "scan_constant_dids.py",
    "verify_memory_channels.py",
)


def test_no_tool_constructs_a_client_itself() -> None:
    offenders: list[str] = []
    for path in _tool_sources():
        if path.name == "_bus_tool.py":  # the one place a client is built
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name in {"CanObdClient", "bus_factory"}:
                    offenders.append(f"{path.name}:{node.lineno} {name}(...)")
    assert offenders == []
    for name in TRANSMITTING_TOOLS:
        assert "open_client(" in (TOOLS_DIR / name).read_text(), name


def test_only_the_job_controller_worker_skips_the_passive_check() -> None:
    """``did_sweep`` runs under the JobController and skips the HTTP check on
    purpose; no hand-run tool may borrow its verdict."""
    users = sorted(p.name for p in _tool_sources() if "job_controller()" in p.read_text())
    assert users == ["did_sweep.py"]
    assert "DELIBERATELY" in (TOOLS_DIR / "did_sweep.py").read_text()


def test_bus_tool_jsonl_shape_is_one_shape() -> None:
    header = _bus_tool.header_record("x", started_at=12.5, label="l")
    assert header == {"kind": "header", "tool": "x", "started_at": 12.5, "label": "l"}
    assert _bus_tool.record_kind({"type": "read"}) == "read"  # pre-R06.1 can_memory_log
    assert _bus_tool.record_kind({"kind": "read"}) == "read"


# ---------------------------------------------------------------------------
# responder window: a physical header outside 0x7E0-0x7E7 must still be heard
# ---------------------------------------------------------------------------


def test_response_window_pins_a_physical_header_and_keeps_functional_open() -> None:
    assert _bus_tool.response_window(0x740) == (0x748, 0x748)
    assert _bus_tool.response_window(0x7E0) == (0x7E8, 0x7E8)
    assert _bus_tool.response_window(0x7DF) == (0x7E8, 0x7EF)
    assert _bus_tool.responder_for(0x740) == 0x748
    assert _bus_tool.responder_for(0x7DF) is None


def test_open_client_hears_a_module_answering_outside_the_obd_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--header 740`` is answered on 0x748. The client's default window is
    0x7E8-0x7EF, so an unpinned client dropped the reply (verifier, R06.1)."""
    import threading

    import can

    channel = "bus-tool-740"

    class _Can:
        pass

    class _Hardware:
        can = _Can()

    class _Config:
        hardware = _Hardware()

    monkeypatch.setattr(CanaryConfig, "from_yaml", staticmethod(lambda _p: _Config()))
    monkeypatch.setattr(
        _bus_tool,
        "bus_factory",
        lambda _spec: can.interface.Bus(interface="virtual", channel=channel),
    )
    stop = threading.Event()
    ready = threading.Event()

    def _module() -> None:
        ecu = can.interface.Bus(interface="virtual", channel=channel)
        ready.set()
        try:
            while not stop.is_set():
                frame = ecu.recv(timeout=0.05)
                if frame is None or frame.arbitration_id != 0x740:
                    continue
                data = bytes(frame.data)
                if data[1] == 0x22:
                    reply = bytes([0x05, 0x62, data[2], data[3], 0xAB, 0xCD, 0x00, 0x00])
                    ecu.send(can.Message(arbitration_id=0x748, data=reply, is_extended_id=False))
        finally:
            ecu.shutdown()

    thread = threading.Thread(target=_module, daemon=True)
    thread.start()
    assert ready.wait(timeout=5.0)
    try:
        args = _args("--no-daemon", "--header", "740", "--read-timeout", "0.5")
        with _bus_tool.open_client(args, _bus_tool.require_passive(args)) as client:
            response = client.query_mode_22(0x1101, request_id=0x740)
    finally:
        stop.set()
        thread.join(timeout=1.0)
    assert response is not None
    assert response.payload == bytes([0xAB, 0xCD])
    assert response.responder_id == 0x748


def test_can_obd_probe_keeps_timeout_as_an_alias() -> None:
    """``--timeout`` was this tool's spelling before R06.1."""
    from tools import can_obd_probe

    assert can_obd_probe.parse_args(["--timeout", "0.3"]).read_timeout == 0.3
    assert can_obd_probe.parse_args(["--read-timeout", "0.4"]).read_timeout == 0.4


def test_probe_local_identifier_keeps_timeout_as_an_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []

    class _StopError(Exception):
        pass

    def _open(args: argparse.Namespace, passive: Any) -> Any:
        seen.append(args.read_timeout)
        raise _StopError

    monkeypatch.setattr(probe_local_identifier, "open_client", _open)
    with pytest.raises(_StopError):
        probe_local_identifier.main(["--no-daemon", "--timeout", "0.3"])
    assert seen == [0.3]
