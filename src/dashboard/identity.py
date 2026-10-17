"""What this daemon says about itself to anyone on the network (``GET /identity``).

Unauthenticated and cheap, so a laptop can sweep a LAN for CANary daemons
(``src.companion.discovery``) without the full ``/status`` (1–3 s on the Pi)
and without a token. The payload names the product, the host, whether the host
is a Raspberry Pi, the active vehicle profile, the version and the dashboard
port. It never carries a token, a session or a path.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from src.config import CanaryConfig
from src.dashboard.version import _canary_version, _git_sha

PRODUCT = "canary"
IDENTITY_API = "identity/1"
DEVICE_TREE_MODEL = Path("/proc/device-tree/model")


def host_kind(model_path: Path = DEVICE_TREE_MODEL) -> str:
    """``raspberry_pi`` when the kernel's device tree says so, else ``host``."""
    try:
        model = model_path.read_bytes().rstrip(b"\x00").decode("utf-8", "replace")
    except OSError:
        return "host"
    return "raspberry_pi" if "raspberry pi" in model.lower() else "host"


def identity_payload(
    config: CanaryConfig,
    *,
    hostname: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    scoped = getattr(config, "scoped_profile", None)
    return {
        "product": PRODUCT,
        "api": IDENTITY_API,
        "hostname": hostname or socket.gethostname(),
        "host_kind": kind or host_kind(),
        "profile": scoped if isinstance(scoped, str) else None,
        "version": _canary_version(),
        "git_sha": _git_sha(),
        "dashboard_port": config.dashboard.port,
    }


__all__ = ["IDENTITY_API", "PRODUCT", "host_kind", "identity_payload"]
