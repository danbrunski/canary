"""HTTP client for the daemon's passive mode, as seen from a separate process (R06.1).

A standalone tool that transmits on the diagnostic CAN (``tools/did_log``,
``batch_probe``, ``probe_local_identifier`` ...) shares the bus with the
daemon's OBD scheduler. The daemon's own ``tx_gate`` only silences the daemon;
the tool learns whether it is silent by asking ``GET /control/passive``. This
module is that question. ``tools/_bus_tool.require_passive`` turns the answer
into the verdict ``open_client`` demands.

Stdlib-only (``urllib``) so a tool can run from a bare checkout.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: The Pi's control token file (``scripts/install_pi.sh`` writes it).
DEFAULT_TOKEN_PATH = Path("/etc/canary/control.token")
#: Where a tool on the Pi reaches the daemon. One default for every tool —
#: ``verify_memory_channels`` used ``localhost`` while the rest used 127.0.0.1.
DEFAULT_DAEMON_URL = "http://127.0.0.1:8000"
#: The token environment variable (``daemon.control_token_env``'s default).
TOKEN_ENV = "CANARY_CONTROL_TOKEN"
#: Overrides the token file's path, as in ``install_pi.sh`` and ``scripts/canaryctl.py``.
TOKEN_FILE_ENV = "CANARY_CONTROL_TOKEN_FILE"


@dataclass(frozen=True)
class PassiveStatus:
    armed: bool
    ttl_remaining_seconds: float | None
    detail: str


PassiveChecker = Callable[[], PassiveStatus]


def _env_file_values() -> dict[str, str]:
    """``KEY=VALUE`` pairs of ``$CANARY_ENV_FILE`` / ``~/.canary/env``; ``{}`` if unreadable.

    Parsed as ``scripts/canaryctl.py``'s ``_load_env_file`` does (blank lines,
    ``#`` comments, a leading ``export``, one pair of quotes), minus its
    permissions warning. ``tests/test_daemon_http_token.py`` feeds both the
    same files.
    """
    override = os.environ.get("CANARY_ENV_FILE")
    env_file = Path(override).expanduser() if override else Path.home() / ".canary" / "env"
    try:
        text = env_file.read_text()
    except (OSError, UnicodeDecodeError, RuntimeError):
        return {}
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def resolve_token(path: Path | None = None) -> str | None:
    """The control token, in the order ``scripts/canaryctl.py`` uses.

    1. ``$CANARY_CONTROL_TOKEN``, then ``$CANARY_TOKEN`` (the legacy alias);
    2. the same two names in ``$CANARY_ENV_FILE`` (default ``~/.canary/env``);
    3. the token file: ``path``, else ``$CANARY_CONTROL_TOKEN_FILE``, else
       the Pi's ``/etc/canary/control.token``.

    A blank value at any tier falls through to the next. R30.13: ``canaryctl``
    honored the file override, the alias and the env file and this did not, so
    two tools could authenticate differently against one Pi;
    ``tests/test_daemon_http_token.py`` pins the whole table.
    """
    names = (TOKEN_ENV, "CANARY_TOKEN")
    for name in names:
        token = (os.environ.get(name) or "").strip()
        if token:
            return token
    file_values = _env_file_values()
    for name in names:
        token = file_values.get(name, "").strip()
        if token:
            return token
    token_file = path or Path(os.environ.get(TOKEN_FILE_ENV) or DEFAULT_TOKEN_PATH)
    try:
        return token_file.read_text().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def http_passive_checker(
    base_url: str, token: str | None, *, timeout: float = 2.0
) -> PassiveChecker:
    """``GET {base_url}/control/passive`` -> :class:`PassiveStatus`.

    Any failure (unreachable, auth refused, unexpected shape) reads as NOT
    armed: a daemon we cannot see may have restarted, and a restart comes
    back with passive off and its scheduler polling.
    """
    url = base_url.rstrip("/") + "/control/passive"

    def _check() -> PassiveStatus:
        request = urllib.request.Request(url, method="GET")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return PassiveStatus(False, None, f"GET {url} -> HTTP {exc.code}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return PassiveStatus(False, None, f"GET {url} failed: {exc}")
        passive = body.get("passive") if isinstance(body, dict) else None
        if not isinstance(passive, dict) or "enabled" not in passive:
            return PassiveStatus(False, None, f"GET {url}: unexpected response {body!r}")
        ttl = passive.get("ttl_remaining_seconds")
        ttl_value = float(ttl) if isinstance(ttl, int | float) else None
        armed = passive["enabled"] is True
        return PassiveStatus(armed, ttl_value, "armed" if armed else "passive mode is off")

    return _check


def passive_problem(status: PassiveStatus, check_seconds: float) -> str | None:
    """Why a tool must not (continue to) transmit, or ``None`` if it may.

    Armed is not enough: a TTL shorter than the tool's re-check interval means
    the daemon resumes polling before the tool next looks.
    """
    if not status.armed:
        return status.detail
    ttl = status.ttl_remaining_seconds
    if ttl is not None and ttl < check_seconds:
        return (
            f"passive TTL {ttl:.1f}s remaining is below the {check_seconds:g}s check "
            "interval; the daemon will resume polling"
        )
    return None


__all__ = [
    "DEFAULT_DAEMON_URL",
    "DEFAULT_TOKEN_PATH",
    "TOKEN_ENV",
    "TOKEN_FILE_ENV",
    "PassiveChecker",
    "PassiveStatus",
    "http_passive_checker",
    "passive_problem",
    "resolve_token",
]
