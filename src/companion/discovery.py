"""Find CANary daemons on the network, from a laptop or another Pi.

The companion (``scripts/canary_companion.py``) and the desk launcher
(``scripts/canary_desk.py``) use this to run in companion mode without the
operator typing the Pi's address. Standard library only.

A daemon is recognised by its unauthenticated ``GET /identity``
(``src.dashboard.identity``); a daemon older than that route is recognised by
``GET /status?view=compact`` answering with a ``vehicle_state``. Anything else
on port 8000 is not a CANary.

Candidates are tried in this order, and a daemon found earlier wins over the
same daemon found later (``hostname`` is the identity):

1. ``CANARY_HOST`` / ``CANARY_URL`` in the environment (the operator's own
   ``~/.canary/env``);
2. the conventional names (``canary.local``; the installer names a Pi
   ``canary-<mac4>`` and mDNS adds ``.local``), when this host can resolve
   them at all (WSL cannot, see network-and-provisioning N4.3);
3. the wired address every Pi answers on its Ethernet link
   (``192.168.137.2``, AGENTS.md);
4. a sweep of each directly attached IPv4 network, bounded to
   ``SWEEP_LIMIT`` addresses, skipping loopback, link-local, Docker and
   bridge interfaces. The sweep is 254 TCP connects on a home LAN; it is
   what works from WSL, where multicast DNS does not.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

PRODUCT = "canary"
DEFAULT_PORT = 8000
WIRED_ADDRESS = "192.168.137.2"
CONVENTIONAL_NAMES: tuple[str, ...] = ("canary.local",)
IDENTITY_PATH = "/identity"
STATUS_FALLBACK_PATH = "/status?view=compact"
ENV_VARS: tuple[str, ...] = ("CANARY_HOST", "CANARY_URL")
#: Prefixes shorter than this are not swept (a /16 is 65k connects).
SWEEP_MIN_PREFIX = 22
SWEEP_LIMIT = 1024
SKIPPED_INTERFACE_PREFIXES: tuple[str, ...] = ("lo", "docker", "br-", "veth", "virbr")
#: What a probe returns: HTTP status (0 when nothing answered) and the JSON body.
Fetch = Callable[[str, float], tuple[int, Any]]

Source = str  # "env" | "name" | "wired" | "sweep" | "configured"


@dataclass(frozen=True)
class DaemonIdentity:
    """What a daemon says about itself (``GET /identity``, or inferred from ``/status``)."""

    product: str
    hostname: str
    host_kind: str
    version: str | None
    profile: str | None
    dashboard_port: int
    source_path: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, path: str, host: str) -> DaemonIdentity:
        if path == IDENTITY_PATH:
            if payload.get("product") != PRODUCT:
                raise ValueError(f"not a CANary identity: product={payload.get('product')!r}")
            return cls(
                product=PRODUCT,
                hostname=str(payload.get("hostname") or host),
                host_kind=str(payload.get("host_kind") or "unknown"),
                version=_optional_str(payload.get("version")),
                profile=_optional_str(payload.get("profile")),
                dashboard_port=int(payload.get("dashboard_port") or DEFAULT_PORT),
                source_path=IDENTITY_PATH,
            )
        if "vehicle_state" not in payload:
            raise ValueError("not a CANary status: no vehicle_state")
        return cls(
            product=PRODUCT,
            hostname=host,
            host_kind="unknown",
            version=None,
            profile=None,
            dashboard_port=DEFAULT_PORT,
            source_path="/status",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveredPi:
    base_url: str
    host: str
    source: Source
    identity: DaemonIdentity

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "host": self.host,
            "source": self.source,
            "identity": self.identity.to_dict(),
        }

    def label(self) -> str:
        who = self.identity.hostname
        if self.identity.profile:
            who += f" ({self.identity.profile})"
        return f"{who} at {self.base_url} [{self.source}]"


def _optional_str(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def base_url_of(host_or_url: str, port: int = DEFAULT_PORT) -> str:
    """``canary.local`` → ``http://canary.local:8000``; a URL is kept, minus a trailing slash."""
    text = host_or_url.strip().rstrip("/")
    if "://" in text:
        return text
    if ":" in text and not text.startswith("["):
        return f"http://{text}"
    return f"http://{text}:{port}"


def host_of(base_url: str) -> str:
    return urllib.parse.urlsplit(base_url).hostname or base_url


def env_candidates(environ: Mapping[str, str] = os.environ) -> list[tuple[str, Source]]:
    out: list[tuple[str, Source]] = []
    for name in ENV_VARS:
        value = environ.get(name, "").strip()
        if value:
            out.append((base_url_of(value), "env"))
    return out


def name_candidates(
    names: Sequence[str] = CONVENTIONAL_NAMES,
    *,
    resolve: Callable[[str], bool] | None = None,
) -> list[tuple[str, Source]]:
    """The conventional names this host can resolve; an unresolvable one is skipped, not probed."""
    can = resolve or _resolves
    return [(base_url_of(name), "name") for name in names if can(name)]


def _resolves(name: str) -> bool:
    try:
        socket.getaddrinfo(name, None, socket.AF_INET)
    except OSError:
        return False
    return True


def interface_networks(ip_addr_output: str) -> list[ipaddress.IPv4Network]:
    """Parse ``ip -4 -o addr show`` into the networks worth sweeping."""
    networks: list[ipaddress.IPv4Network] = []
    for line in ip_addr_output.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        interface = parts[1]
        if interface.startswith(SKIPPED_INTERFACE_PREFIXES):
            continue
        try:
            iface = ipaddress.IPv4Interface(parts[3])
        except ValueError:
            continue
        if iface.ip.is_loopback or iface.ip.is_link_local:
            continue
        if iface.network.prefixlen < SWEEP_MIN_PREFIX or iface.network.prefixlen >= 31:
            continue
        if iface.network not in networks:
            networks.append(iface.network)
    return networks


def local_networks() -> list[ipaddress.IPv4Network]:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            ["ip", "-4", "-o", "addr", "show"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return interface_networks(out.stdout) if out.returncode == 0 else []


def sweep_candidates(
    networks: Iterable[ipaddress.IPv4Network],
    *,
    port: int = DEFAULT_PORT,
    limit: int = SWEEP_LIMIT,
) -> list[tuple[str, Source]]:
    out: list[tuple[str, Source]] = []
    for network in networks:
        for address in network.hosts():
            if len(out) >= limit:
                return out
            out.append((f"http://{address}:{port}", "sweep"))
    return out


def urlopen_json(url: str, timeout: float) -> tuple[int, Any]:
    """The default probe: status code and parsed body; ``(0, None)`` when nothing answers."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - http(s) only
            body = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def probe(
    base_url: str, *, fetch: Fetch = urlopen_json, timeout: float = 0.5
) -> DaemonIdentity | None:
    """Whether a CANary daemon answers at ``base_url``, and who it says it is."""
    host = host_of(base_url)
    status, payload = fetch(base_url + IDENTITY_PATH, timeout)
    if status == 200 and isinstance(payload, Mapping):
        try:
            return DaemonIdentity.from_payload(payload, path=IDENTITY_PATH, host=host)
        except ValueError:
            return None
    if status == 0:
        return None
    status, payload = fetch(base_url + STATUS_FALLBACK_PATH, timeout)
    if status == 200 and isinstance(payload, Mapping):
        try:
            return DaemonIdentity.from_payload(payload, path=STATUS_FALLBACK_PATH, host=host)
        except ValueError:
            return None
    return None


def candidates(
    *,
    environ: Mapping[str, str] = os.environ,
    names: Sequence[str] = CONVENTIONAL_NAMES,
    resolve: Callable[[str], bool] | None = None,
    wired: str | None = WIRED_ADDRESS,
    sweep: bool = True,
    networks: Iterable[ipaddress.IPv4Network] | None = None,
    port: int = DEFAULT_PORT,
) -> list[tuple[str, Source]]:
    """Every address to try, in priority order, without duplicates."""
    ordered: list[tuple[str, Source]] = []
    ordered.extend(env_candidates(environ))
    ordered.extend(name_candidates(names, resolve=resolve))
    if wired:
        ordered.append((base_url_of(wired, port), "wired"))
    if sweep:
        ordered.extend(
            sweep_candidates(local_networks() if networks is None else networks, port=port)
        )
    seen: set[str] = set()
    out: list[tuple[str, Source]] = []
    for base_url, source in ordered:
        if base_url in seen:
            continue
        seen.add(base_url)
        out.append((base_url, source))
    return out


def discover(
    *,
    fetch: Fetch = urlopen_json,
    timeout: float = 0.5,
    workers: int = 64,
    **candidate_options: Any,
) -> list[DiscoveredPi]:
    """Probe every candidate in parallel; one entry per daemon, best source first."""
    todo = candidates(**candidate_options)
    if not todo:
        return []

    def one(item: tuple[str, Source]) -> DiscoveredPi | None:
        base_url, source = item
        identity = probe(base_url, fetch=fetch, timeout=timeout)
        if identity is None:
            return None
        return DiscoveredPi(
            base_url=base_url, host=host_of(base_url), source=source, identity=identity
        )

    with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as pool:
        results = list(pool.map(one, todo))
    found: list[DiscoveredPi] = []
    seen: set[str] = set()
    for item in results:
        if item is None or item.identity.hostname in seen:
            continue
        seen.add(item.identity.hostname)
        found.append(item)
    return found


def choose(found: Sequence[DiscoveredPi]) -> tuple[DiscoveredPi | None, str]:
    """One Pi to connect to, or why not: none, or more than one (the operator picks)."""
    if not found:
        return None, "no CANary daemon found on the network"
    if len(found) == 1:
        return found[0], f"found {found[0].label()}"
    listing = "; ".join(item.label() for item in found)
    return None, f"{len(found)} CANary daemons found, choose one with --pi-host: {listing}"


__all__ = [
    "CONVENTIONAL_NAMES",
    "DEFAULT_PORT",
    "IDENTITY_PATH",
    "PRODUCT",
    "WIRED_ADDRESS",
    "DaemonIdentity",
    "DiscoveredPi",
    "base_url_of",
    "candidates",
    "choose",
    "discover",
    "env_candidates",
    "host_of",
    "interface_networks",
    "local_networks",
    "name_candidates",
    "probe",
    "sweep_candidates",
    "urlopen_json",
]
