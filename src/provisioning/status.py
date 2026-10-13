"""The status characteristic (roadmap N1.3).

With no AP, a Pi that joins something unexpected -- or fails to join -- is
otherwise silent and reachable only by cable. This characteristic answers
"where did it go" from any phone in range, provisioning rights or not: open a
generic BLE app (nRF Connect), read it, get the address.

The value is compact UTF-8 JSON, so such an app shows it as text::

    {"v":1,"host":"canary-1a2b","mode":"client","ssid":"Starlink",
     "addr":{"wlan0":["192.168.1.23"],"eth0":["192.168.137.2"]},
     "url":"http://192.168.1.23:8000/","prov":"locked","win":0}

``url`` is built from the address, never from ``.local``: Android does not
reliably resolve mDNS (N1.5). ``prov`` and ``win`` are the gate's reason and
window seconds remaining. A GATT value is at most 512 bytes, so IPv6
addresses go first, then interfaces, if it would not fit.
"""

from __future__ import annotations

import json
from typing import Any

from src.provisioning.gate import GateVerdict
from src.provisioning.network import NetworkStatus

#: CANary's own service, beside the Improv one.
STATUS_SERVICE_UUID = "45e00fc6-5b80-4fcc-9a05-be255d9ef23e"
STATUS_CHAR_UUID = "45e00fc7-5b80-4fcc-9a05-be255d9ef23e"
#: The ATT maximum attribute length.
MAX_STATUS_BYTES = 512
SCHEMA_VERSION = 1


def dashboard_url(status: NetworkStatus, port: int) -> str | None:
    address = status.wifi_ipv4
    return f"http://{address}:{port}/" if address else None


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_status(status: NetworkStatus, verdict: GateVerdict, *, dashboard_port: int) -> bytes:
    payload: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "host": status.hostname,
        "mode": status.mode,
        "ssid": status.ssid,
        "addr": status.addresses,
        "url": dashboard_url(status, dashboard_port),
        "prov": verdict.reason,
        "win": int(verdict.window_remaining_s),
    }
    encoded = _encode(payload)
    if len(encoded) <= MAX_STATUS_BYTES:
        return encoded
    payload["addr"] = {
        name: [a for a in addresses if ":" not in a] for name, addresses in status.addresses.items()
    }
    # Keep the WiFi interface's addresses longest: that is the one a phone uses.
    order = sorted(payload["addr"], key=lambda name: name == status.wifi_interface)
    while len(encoded := _encode(payload)) > MAX_STATUS_BYTES and order:
        del payload["addr"][order.pop(0)]
    return encoded
