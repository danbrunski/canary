"""``python -m src.provisioning`` -- the BLE provisioning service and its tools.

::

    python -m src.provisioning run                 # the service (systemd, as root)
    python -m src.provisioning open-window [-m 5]  # let a phone change networks
    python -m src.provisioning close-window
    python -m src.provisioning status              # what the status characteristic says
    python -m src.provisioning scan                # what a phone would be offered

``run`` is configured from the environment (``systemd/canary-provision.service``):

- ``CANARY_PROVISION_ADAPTER`` -- BlueZ adapter, e.g. ``hci0`` (default: first able one)
- ``CANARY_PROVISION_WIFI_IFACE`` -- WiFi interface to manage (default: NetworkManager's)
- ``CANARY_DASHBOARD_PORT`` -- port in the URL handed back (default 8000)
- ``CANARY_PROVISION_WINDOW_FILE`` -- the window file (default ``/run/canary/provision_window``)
- ``CANARY_PROVISION_JOIN_TIMEOUT`` -- seconds to wait for a join (default 45)
- ``CANARY_NETWORK_SOCKET`` -- the network-control socket the dashboard uses
  (default ``/run/canary/network.sock``); ``CANARY_NETWORK_SOCKET_GROUP`` --
  the group allowed to use it (default ``canary``);
  ``CANARY_NETWORK_JOIN_TIMEOUT`` -- seconds a join asked for over it may wait
  (default 30)

The service serves the BLE server and that socket side by side: a Pi with no
Bluetooth adapter still answers the socket (:mod:`src.provisioning.service`).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import logging
import os
import socket
import sys
from collections.abc import Sequence
from pathlib import Path

from src.log import configure_logging
from src.provisioning.controller import DEFAULT_JOIN_TIMEOUT_S, DeviceInfo, ImprovController
from src.provisioning.gate import (
    DEFAULT_WINDOW_MINUTES,
    DEFAULT_WINDOW_PATH,
    decide,
    read_window_remaining,
    write_window,
)
from src.provisioning.netsock import DEFAULT_JOIN_TIMEOUT_S as DEFAULT_NETWORK_JOIN_TIMEOUT_S
from src.provisioning.network import NetworkManagerCli
from src.provisioning.service import cancel_on_signals, run_service
from src.provisioning.status import build_status

log = logging.getLogger(__name__)

DEFAULT_DASHBOARD_PORT = 8000
_MODEL_PATH = Path("/proc/device-tree/model")


def _env(name: str) -> str | None:
    return os.environ.get(name) or None


def _window_path() -> Path:
    return Path(_env("CANARY_PROVISION_WINDOW_FILE") or DEFAULT_WINDOW_PATH)


def _dashboard_port() -> int:
    return int(_env("CANARY_DASHBOARD_PORT") or DEFAULT_DASHBOARD_PORT)


def _network() -> NetworkManagerCli:
    return NetworkManagerCli(interface=_env("CANARY_PROVISION_WIFI_IFACE"))


def device_info() -> DeviceInfo:
    try:
        version = importlib.metadata.version("canary")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    try:
        hardware = _MODEL_PATH.read_text().rstrip("\x00\n")
    except OSError:
        hardware = "unknown"
    return DeviceInfo("CANary", version, hardware, socket.gethostname())


def _run() -> int:
    network = _network()
    network.recover()  # logs each join it undoes
    timeout = float(_env("CANARY_PROVISION_JOIN_TIMEOUT") or DEFAULT_JOIN_TIMEOUT_S)

    async def ble() -> None:
        from src.provisioning.bluez import serve  # needs dbus-fast and a system bus

        await serve(
            lambda sink: ImprovController(
                network,
                sink,
                device_info=device_info(),
                dashboard_port=_dashboard_port(),
                window_path=_window_path(),
                join_timeout_s=timeout,
            ),
            adapter_name=_env("CANARY_PROVISION_ADAPTER"),
        )

    async def main() -> None:
        task = asyncio.current_task()
        assert task is not None
        cancel_on_signals(task)
        await run_service(
            network,
            ble,
            join_timeout_s=float(
                _env("CANARY_NETWORK_JOIN_TIMEOUT") or DEFAULT_NETWORK_JOIN_TIMEOUT_S
            ),
        )

    try:
        asyncio.run(main())
    except asyncio.CancelledError:  # SIGTERM from systemd
        log.info("provisioning: stopping")
    return 0


def _status() -> int:
    network = _network()
    verdict = decide(
        client_profiles=len(network.client_profiles()),
        window_remaining_s=read_window_remaining(_window_path()),
    )
    payload = build_status(network.status(), verdict, dashboard_port=_dashboard_port())
    print(json.dumps(json.loads(payload), indent=2))
    return 0


def _scan() -> int:
    for n in _network().scan():
        print(f"{n.rssi_dbm:>5} dBm  {n.improv_auth:<10} {n.ssid}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.provisioning", description=__doc__)
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="serve Improv WiFi and the status characteristic over BLE")
    opener = sub.add_parser("open-window", help="allow provisioning for a few minutes")
    opener.add_argument("-m", "--minutes", type=float, default=DEFAULT_WINDOW_MINUTES)
    sub.add_parser("close-window", help="end the provisioning window now")
    sub.add_parser("status", help="print the status characteristic's value")
    sub.add_parser("scan", help="list the networks a phone would be offered")
    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    if args.command == "run":
        return _run()
    if args.command == "open-window":
        try:
            write_window(args.minutes, _window_path())
        except ValueError as exc:
            parser.error(str(exc))
        print(f"provisioning window open for {args.minutes:g} min")
        return 0
    if args.command == "close-window":
        write_window(0, _window_path())
        print("provisioning window closed")
        return 0
    if args.command == "status":
        return _status()
    return _scan()


if __name__ == "__main__":
    sys.exit(main())
