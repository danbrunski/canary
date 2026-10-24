"""Tests for the scan_constant_dids CLI: transport selection + wiring."""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable
from typing import Any

import pytest

from src.analysis.did_scan import CanableMode22Transport
from src.config import CanaryConfig
from tools import _bus_tool
from tools import scan_constant_dids as scd

# ---------------------------------------------------------------------------
# argparse: --canable / --port are a required, mutually-exclusive pair
# ---------------------------------------------------------------------------


def test_canable_flag_parses() -> None:
    args = scd.parse_args(["--canable"])
    assert args.canable is True and args.port is None


def test_port_flag_parses() -> None:
    args = scd.parse_args(["--port", "/dev/ttyUSB0"])
    assert args.canable is False and args.port == "/dev/ttyUSB0"


def test_transport_is_required() -> None:
    with pytest.raises(SystemExit):
        scd.parse_args([])


def test_transport_is_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        scd.parse_args(["--canable", "--port", "/dev/ttyUSB0"])


# ---------------------------------------------------------------------------
# _build_transport wiring
# ---------------------------------------------------------------------------


class _FakeBus:
    def __init__(self) -> None:
        self.shutdown_called = False

    def shutdown(self) -> None:
        self.shutdown_called = True


class _FakeCan:
    channel = "/dev/ttyACM0"


class _FakeConfigHardware:
    can = _FakeCan()


class _FakeConfig:
    hardware = _FakeConfigHardware()


def _patch_canable(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeBus, list[dict[str, Any]]]:
    bus = _FakeBus()
    constructed: list[dict[str, Any]] = []

    monkeypatch.setattr(CanaryConfig, "from_yaml", staticmethod(lambda _p: _FakeConfig()))
    monkeypatch.setattr(_bus_tool, "bus_factory", lambda _spec: bus)

    def _fake_client(bus_arg: object, **kwargs: object) -> object:
        constructed.append({"bus": bus_arg, **kwargs})
        return object()  # CanableMode22Transport only stores it, never calls here

    monkeypatch.setattr(_bus_tool, "CanObdClient", _fake_client)
    return bus, constructed


def _build(args: Namespace) -> tuple[Any, Callable[[], None], str]:
    """``_build_transport`` with a bench verdict (``--no-daemon``)."""
    return scd._build_transport(args, _bus_tool.require_passive(args))


def test_build_transport_canable_wires_request_and_response_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus, constructed = _patch_canable(monkeypatch)
    args = scd.parse_args(["--canable", "--no-daemon", "--header", "7E0", "--read-timeout", "0.5"])

    transport, close_fn, banner = _build(args)

    assert isinstance(transport, CanableMode22Transport)
    assert constructed == [
        {
            "bus": bus,
            "request_id": 0x7E0,
            "response_id_min": 0x7E8,
            "response_id_max": 0x7E8,
            "timeout_seconds": 0.5,
        }
    ]
    assert "0x7E0" in banner and "0x7E8" in banner
    close_fn()
    assert bus.shutdown_called is True


def test_build_transport_canable_respects_alternate_module_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bus, constructed = _patch_canable(monkeypatch)
    args = scd.parse_args(["--canable", "--no-daemon", "--header", "7E1"])  # transmission ECU

    _transport, _close, banner = _build(args)

    assert constructed[0]["request_id"] == 0x7E1
    assert constructed[0]["response_id_min"] == 0x7E9
    assert constructed[0]["response_id_max"] == 0x7E9
    assert "0x7E9" in banner


def test_build_transport_canable_passes_session_and_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_canable(monkeypatch)
    args = scd.parse_args(["--canable", "--no-daemon", "--session", "C0", "--keepalive", "1.5"])
    transport, _close, _banner = _build(args)
    assert transport._session == 0xC0
    assert transport._keepalive == 1.5


def test_build_transport_canable_defaults_to_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_canable(monkeypatch)
    transport, _close, _banner = _build(scd.parse_args(["--canable", "--no-daemon"]))
    assert transport._session is None


def test_build_transport_rejects_non_hex_header() -> None:
    args = scd.parse_args(["--canable", "--no-daemon", "--header", "ZZZ"])
    with pytest.raises(RuntimeError, match="must be hex"):
        _build(args)


def test_build_transport_rejects_out_of_range_header() -> None:
    args = scd.parse_args(["--canable", "--no-daemon", "--header", "1FFF"])  # > 0x7FF (11-bit)
    with pytest.raises(RuntimeError, match="11-bit CAN range"):
        _build(args)
