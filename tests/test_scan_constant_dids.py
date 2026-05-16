"""Tests for the scan_constant_dids CLI: transport selection + wiring."""

from __future__ import annotations

import pytest

from src.analysis.did_scan import CanableMode22Transport
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


def _patch_canable(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeBus, list[dict]]:
    bus = _FakeBus()
    constructed: list[dict] = []

    monkeypatch.setattr(scd.CanaryConfig, "from_yaml", staticmethod(lambda _p: _FakeConfig()))
    monkeypatch.setattr(scd, "bus_factory", lambda _spec: bus)

    def _fake_client(bus_arg, **kwargs):  # type: ignore[no-untyped-def]
        constructed.append({"bus": bus_arg, **kwargs})
        return object()  # CanableMode22Transport only stores it, never calls here

    monkeypatch.setattr(scd, "CanObdClient", _fake_client)
    return bus, constructed


def test_build_transport_canable_wires_request_and_response_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus, constructed = _patch_canable(monkeypatch)
    args = scd.parse_args(["--canable", "--header", "7E0", "--read-timeout", "0.5"])

    transport, close_fn, banner = scd._build_transport(args)

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
    args = scd.parse_args(["--canable", "--header", "7E1"])  # transmission ECU

    scd._build_transport(args)

    assert constructed[0]["request_id"] == 0x7E1
    assert constructed[0]["response_id_min"] == 0x7E9
    assert constructed[0]["response_id_max"] == 0x7E9


def test_build_transport_rejects_non_hex_header() -> None:
    args = scd.parse_args(["--canable", "--header", "ZZZ"])
    with pytest.raises(RuntimeError, match="must be hex"):
        scd._build_transport(args)


def test_build_transport_rejects_out_of_range_header() -> None:
    args = scd.parse_args(["--canable", "--header", "1FFF"])  # > 0x7FF (11-bit)
    with pytest.raises(RuntimeError, match="11-bit CAN range"):
        scd._build_transport(args)
