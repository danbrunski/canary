"""NetworkManager, as the provisioning service needs it.

Reads go through ``nmcli`` in terse mode and ``ip -j``. A join writes the
profile as a keyfile instead of passing the passphrase to ``nmcli``, so the
secret never appears in a process's argv, where any local user can read it
from ``/proc``. Keyfiles are what NetworkManager stores system-owned secrets
in anyway; the file is created 0600, root-owned, as NM requires.

Every subprocess call goes through an injectable :class:`Runner`, so the
parsing and the join sequence are tested against recorded ``nmcli`` output.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from src.provisioning.improv import WifiCredentials

log = logging.getLogger(__name__)

DEFAULT_CONNECTIONS_DIR = Path("/etc/NetworkManager/system-connections")
#: Profiles this service writes are named and filed under this prefix, so an
#: operator can tell them from hand-made ones (``canary-home``).
PROFILE_PREFIX = "canary-wifi-"
_PROFILE_NAMESPACE = uuid.UUID("6c1d7f5e-2f0a-4b8e-9a51-4ca9a7e0b1d3")
_NMCLI_TIMEOUT_S = 20.0
#: ``--rescan yes`` waits for the radio to finish a scan.
_RESCAN_TIMEOUT_S = 30.0
_WIFI_TYPES = frozenset({"802-11-wireless", "wifi"})
#: Beside a profile while its join runs, so a join cut short by a crash, a
#: restart or a power cut is undone at the next start (:meth:`recover`). NM's
#: keyfile plugin ignores dot-files.
_JOURNAL_SUFFIX = ".pending"

_SAFE_SSID = re.compile(r"^[A-Za-z0-9_.\- ]+$")
_HEX_PSK = re.compile(r"^[0-9A-Fa-f]{64}$")


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, timeout: float) -> RunResult: ...


def run_command(argv: Sequence[str], *, timeout: float) -> RunResult:
    """Run a command with C-locale output; never raises for a failed command."""
    env = {**os.environ, "LC_ALL": "C"}
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False, env=env
        )
    except FileNotFoundError as exc:
        return RunResult(127, "", str(exc))
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"{argv[0]} timed out after {timeout:g}s")
    return RunResult(done.returncode, done.stdout, done.stderr)


# --- nmcli terse output ---------------------------------------------------


def split_terse(line: str) -> list[str]:
    """Split one ``nmcli -t`` line, honouring its ``\\:`` and ``\\\\`` escapes."""
    fields: list[str] = []
    current: list[str] = []
    chars = iter(line)
    for ch in chars:
        if ch == "\\":
            current.append(next(chars, ""))
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


def unescape_value(text: str) -> str:
    """One ``nmcli -g`` value: the same escapes, no field separator."""
    return ":".join(split_terse(text))


# --- scan -----------------------------------------------------------------


@dataclass(frozen=True)
class WifiNetwork:
    ssid: str
    #: NetworkManager's 0-100 signal quality.
    signal: int
    #: nmcli's SECURITY column, e.g. ``WPA1 WPA2``, ``WPA2 802.1X``, ``""``.
    security: str

    @property
    def rssi_dbm(self) -> int:
        # NetworkManager derives quality as 2 * (dBm + 100), clamped; invert it.
        return round(self.signal / 2) - 100

    @property
    def improv_auth(self) -> str:
        """The scan result's auth string in Improv's vocabulary."""
        tokens = _security_tokens(self.security)
        if not tokens:
            return "NO"
        if "802.1X" in tokens:
            return "WPA2 EAP"
        if "WEP" in tokens:
            return "WEP"
        names = {"WPA1": "WPA", "WPA2": "WPA2", "WPA3": "WPA3"}
        mapped = [names[t] for t in ("WPA1", "WPA2", "WPA3") if t in tokens]
        return "/".join(mapped) or "/".join(tokens)


def _security_tokens(security: str) -> list[str]:
    return [t for t in security.split() if t != "--"]


def parse_scan(output: str) -> list[WifiNetwork]:
    """Parse ``nmcli -t -f SSID,SIGNAL,SECURITY device wifi list``.

    One entry per SSID (the strongest BSSID), hidden networks dropped,
    strongest first.
    """
    best: dict[str, WifiNetwork] = {}
    for line in output.splitlines():
        parts = split_terse(line)
        if len(parts) != 3 or not parts[0]:
            continue
        ssid, signal_text, security = parts
        try:
            signal = int(signal_text)
        except ValueError:
            continue
        found = WifiNetwork(ssid=ssid, signal=signal, security=security)
        if ssid not in best or best[ssid].signal < signal:
            best[ssid] = found
    return sorted(best.values(), key=lambda n: (-n.signal, n.ssid))


# --- addresses ------------------------------------------------------------


def parse_ip_addresses(output: str) -> dict[str, list[str]]:
    """Parse ``ip -j addr show`` into ``{interface: [address, ...]}``.

    Keeps what a phone could reach: every IPv4 address (link-local included,
    since a direct cable link may run link-local) and global IPv6. Loopback
    and IPv6 link-local are dropped.
    """
    try:
        interfaces = json.loads(output or "[]")
    except json.JSONDecodeError:
        return {}
    result: dict[str, list[str]] = {}
    for iface in interfaces if isinstance(interfaces, list) else []:
        name = iface.get("ifname")
        if not isinstance(name, str) or name == "lo":
            continue
        addresses = [
            str(info["local"])
            for info in iface.get("addr_info", [])
            if "local" in info
            and (
                info.get("family") == "inet"
                or (info.get("family") == "inet6" and info.get("scope") == "global")
            )
        ]
        if addresses:
            result[name] = addresses
    return result


# --- keyfiles -------------------------------------------------------------


class JoinRefusedError(ValueError):
    """Credentials this service will not try, with the reason to log."""


class InvalidCredentialsError(JoinRefusedError):
    """A passphrase of the wrong shape, as distinct from a network type refused.

    The caller can fix this one (retype it); an enterprise or WEP network it
    cannot, so the saved-network operations report the two differently.
    """


def choose_key_mgmt(security: str | None, password: str) -> str | None:
    """The keyfile ``key-mgmt`` for a network, or ``None`` for an open one.

    ``security`` is the scan's SECURITY column, ``None`` when the network was
    not in the scan (hidden): then the password decides between open and PSK.
    """
    tokens = _security_tokens(security or "")
    if security is None:
        tokens = ["WPA2"] if password else []
    if "802.1X" in tokens:
        raise JoinRefusedError("enterprise (802.1X) networks need a certificate or identity")
    if "WEP" in tokens:
        raise JoinRefusedError("WEP is not supported")
    if not tokens:
        return None
    if "WPA3" in tokens and not {"WPA1", "WPA2"} & set(tokens):
        if not password:
            raise InvalidCredentialsError("the network needs a passphrase")
        return "sae"
    if not (8 <= len(password) <= 63 and password.isascii() and password.isprintable()) and not (
        _HEX_PSK.match(password)
    ):
        raise InvalidCredentialsError(
            "a WPA passphrase is 8-63 printable ASCII characters or 64 hex digits"
        )
    return "wpa-psk"


def _escape_keyfile(value: str) -> str:
    """GKeyFile string escaping (spaces become ``\\s`` so none are trimmed)."""
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace(" ", "\\s")
    )


def _keyfile_ssid(ssid: str) -> str:
    # A plain name is written as text. Anything else -- ';', non-ASCII, an
    # all-digit name the reader could take for a byte -- as the byte list
    # NetworkManager's keyfile reader also accepts.
    if _SAFE_SSID.match(ssid) and not ssid.isdigit():
        return _escape_keyfile(ssid)
    return "".join(f"{b};" for b in ssid.encode("utf-8"))


def profile_uuid(ssid: str) -> str:
    """Stable per SSID, so provisioning the same network again replaces it."""
    return str(uuid.uuid5(_PROFILE_NAMESPACE, ssid))


def profile_filename(ssid: str) -> str:
    digest = hashlib.sha256(ssid.encode("utf-8")).hexdigest()[:10]
    return f"{PROFILE_PREFIX}{digest}.nmconnection"


def render_keyfile(
    creds: WifiCredentials, key_mgmt: str | None, *, hidden: bool, interface: str | None
) -> str:
    lines = [
        "[connection]",
        f"id={_escape_keyfile(PROFILE_PREFIX + creds.ssid)}",
        f"uuid={profile_uuid(creds.ssid)}",
        "type=wifi",
        "autoconnect=true",
    ]
    if interface:
        lines.append(f"interface-name={interface}")
    lines += ["", "[wifi]", "mode=infrastructure", f"ssid={_keyfile_ssid(creds.ssid)}"]
    if hidden:
        lines.append("hidden=true")
    if key_mgmt is not None:
        lines += [
            "",
            "[wifi-security]",
            f"key-mgmt={key_mgmt}",
            f"psk={_escape_keyfile(creds.password)}",
        ]
    lines += ["", "[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
    return "\n".join(lines)


def _journal_path(path: Path) -> Path:
    return path.with_name(f".{path.name}{_JOURNAL_SUFFIX}")


def write_secret_file(path: Path, text: str) -> None:
    """Atomically write a 0600 file: the secret is never readable, even briefly."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# --- the backend ----------------------------------------------------------


@dataclass(frozen=True)
class NetworkStatus:
    hostname: str
    #: ``client``, ``ap``, ``disconnected``, or ``no-wifi``.
    mode: str
    ssid: str | None
    wifi_interface: str | None
    addresses: dict[str, list[str]] = field(default_factory=dict)

    @property
    def wifi_ipv4(self) -> str | None:
        if self.wifi_interface is None:
            return None
        for address in self.addresses.get(self.wifi_interface, []):
            if ":" not in address:
                return address
        return None


@dataclass(frozen=True)
class JoinResult:
    ok: bool
    detail: str = ""
    #: Why it failed, in :class:`WifiOpError` vocabulary; empty on success.
    kind: str = ""


# --- saved networks (roadmap N5.2) ----------------------------------------

#: Failure kinds a saved-network operation reports. The control socket sends
#: them as ``error`` and the daemon maps each to an HTTP status.
KIND_NOT_FOUND = "not_found"
KIND_REFUSED = "refused"
KIND_INVALID = "invalid"
KIND_JOIN_FAILED = "join_failed"
KIND_INTERNAL = "internal"

#: ``connection.autoconnect-priority`` values :meth:`set_order` hands out, so
#: the first network wins and the last still beats an untouched profile (0).
_TOP_PRIORITY = 100
_MAX_ORDERED = _TOP_PRIORITY - 1
_MAX_SSID_BYTES = 32

_PROFILE_PROPERTIES = (
    "connection.autoconnect-priority",
    "connection.autoconnect",
    "802-11-wireless.mode",
    "802-11-wireless.ssid",
    "802-11-wireless.hidden",
    "802-11-wireless-security.key-mgmt",
)


class WifiOpError(Exception):
    """A saved-network operation that did not happen, and why."""

    def __init__(self, kind: str, detail: str) -> None:
        # Both in args, so pickle rebuilds it (tests/test_exceptions_pickle.py).
        super().__init__(kind, detail)
        self.kind = kind
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True)
class SavedNetwork:
    """One WiFi client profile NetworkManager holds. Never carries a secret."""

    #: The connection UUID.
    id: str
    #: ``connection.id``; ``canary-wifi-*`` marks the profiles this service wrote.
    name: str
    ssid: str
    priority: int
    autoconnect: bool
    hidden: bool
    #: ``key-mgmt`` (``wpa-psk``, ``sae``, ``wpa-eap``...); ``None`` is open.
    security: str | None
    active: bool
    in_range: bool = False
    signal: int | None = None

    @property
    def managed_by(self) -> str:
        return "canary" if self.name.startswith(PROFILE_PREFIX) else "other"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ssid": self.ssid,
            "priority": self.priority,
            "autoconnect": self.autoconnect,
            "hidden": self.hidden,
            "security": self.security,
            "active": self.active,
            "in_range": self.in_range,
            "signal": self.signal,
            "managed_by": self.managed_by,
        }


@dataclass(frozen=True)
class WifiState:
    """What the Network page shows: saved networks beside the last scan."""

    networks: list[SavedNetwork]
    scan: list[WifiNetwork]
    #: Epoch seconds of the last rescan this service made; ``None`` when it has
    #: made none, so ``scan`` is NetworkManager's cache of unknown age.
    scanned_at: float | None
    interface: str | None

    def network(self, con_id: str) -> SavedNetwork | None:
        return next((n for n in self.networks if n.id == con_id), None)

    def to_dict(self) -> dict[str, Any]:
        saved = {n.ssid for n in self.networks}
        return {
            "networks": [n.to_dict() for n in self.networks],
            "scan": [
                {
                    "ssid": e.ssid,
                    "signal": e.signal,
                    "security": e.security if e.security != "--" else "",
                    "saved": e.ssid in saved,
                }
                for e in self.scan
            ],
            "scanned_at": self.scanned_at,
            "interface": self.interface,
        }


def build_wifi_state(
    saved: Sequence[SavedNetwork],
    scan: Sequence[WifiNetwork],
    *,
    scanned_at: float | None,
    interface: str | None,
) -> WifiState:
    """Join the saved profiles to a scan: range and signal, in priority order."""
    heard = {n.ssid: n for n in scan}
    networks = []
    for net in saved:
        seen = heard.get(net.ssid)
        # The network in use is in range by definition, even a hidden one a
        # scan cannot name.
        networks.append(
            replace(
                net,
                in_range=seen is not None or net.active,
                signal=None if seen is None else seen.signal,
            )
        )
    networks.sort(key=lambda n: (-n.priority, n.ssid))
    return WifiState(networks, list(scan), scanned_at, interface)


def parse_profile_properties(output: str) -> dict[str, str]:
    """Parse ``nmcli -t -f <prop,...> connection show uuid X`` into ``{prop: value}``.

    Each line is ``property:value``; an unset value prints as ``--`` or
    nothing, and both read as ``""``.
    """
    values: dict[str, str] = {}
    for line in output.splitlines():
        name, *rest = split_terse(line)
        if not rest:
            continue
        value = ":".join(rest)
        values[name.lower()] = "" if value == "--" else value
    return values


def _is_client_mode(mode: str) -> bool:
    return mode in ("", "infrastructure")


def _yes(value: str) -> bool:
    return value.strip().lower() in ("yes", "true", "1", "on")


def _priority(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _checked_ssid(ssid: object) -> str:
    if not isinstance(ssid, str) or not ssid:
        raise WifiOpError(KIND_INVALID, "the network name is empty")
    try:
        size = len(ssid.encode("utf-8"))
    except UnicodeEncodeError:
        raise WifiOpError(KIND_INVALID, "the network name is not valid text") from None
    if size > _MAX_SSID_BYTES:
        raise WifiOpError(
            KIND_INVALID, f"a network name is at most {_MAX_SSID_BYTES} bytes, not {size}"
        )
    return ssid


class NetworkManagerCli:
    def __init__(
        self,
        runner: Runner = run_command,
        *,
        connections_dir: Path = DEFAULT_CONNECTIONS_DIR,
        interface: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._run = runner
        self._connections_dir = connections_dir
        self._interface = interface
        self._clock = clock
        #: Held by everything that changes or rescans WiFi, so a join over
        #: Bluetooth and one asked for over the control socket (and a rescan
        #: that would disturb either) never overlap.
        self._lock = threading.RLock()
        #: The passphrase of the profile being installed, only while ``_lock``
        #: is held for it; :meth:`_nmcli` scrubs it from what nmcli says.
        self._secret = ""
        self._scanned_at: float | None = None

    def _nmcli(self, *args: str, timeout: float = _NMCLI_TIMEOUT_S) -> RunResult:
        result = self._run(["nmcli", *args], timeout=timeout)
        if self._secret:
            # A passphrase being installed never leaves this method in nmcli's
            # words, whether it is logged below or returned in a failure detail.
            result = RunResult(
                result.returncode,
                result.stdout.replace(self._secret, "***"),
                result.stderr.replace(self._secret, "***"),
            )
        if result.returncode != 0:
            log.warning("nmcli %s failed (%d): %s", args[:3], result.returncode, result.stderr)
        return result

    def _ifname(self) -> list[str]:
        return ["ifname", self._interface] if self._interface else []

    def scan(self) -> list[WifiNetwork]:
        result = self._nmcli(
            "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", *self._ifname()
        )
        return parse_scan(result.stdout) if result.returncode == 0 else []

    def client_profiles(self) -> list[str]:
        """UUIDs of the WiFi profiles that join a network (AP profiles excluded)."""
        listed = self._nmcli("-t", "-f", "UUID,TYPE", "connection", "show")
        if listed.returncode != 0:
            # Unknown is not "none": treating a failed query as unprovisioned
            # would open the gate whenever nmcli hiccups.
            raise RuntimeError(f"cannot list NetworkManager profiles: {listed.stderr.strip()}")
        clients = []
        for line in listed.stdout.splitlines():
            parts = split_terse(line)
            # Terse mode prints the setting name; accept the alias too, since a
            # miss here would make every Pi look unprovisioned (an open gate).
            if len(parts) != 2 or parts[1] not in _WIFI_TYPES:
                continue
            mode = self._nmcli("-g", "802-11-wireless.mode", "connection", "show", "uuid", parts[0])
            if mode.returncode != 0:
                raise RuntimeError(f"cannot read the mode of profile {parts[0]}")
            if unescape_value(mode.stdout.strip()) in ("", "infrastructure"):
                clients.append(parts[0])
        return clients

    def status(self) -> NetworkStatus:
        addresses = parse_ip_addresses(self._run(["ip", "-j", "addr", "show"], timeout=5).stdout)
        hostname = socket.gethostname()
        devices = self._nmcli("-t", "-f", "DEVICE,TYPE,STATE,CON-UUID", "device", "status")
        wifi = [
            parts
            for parts in map(split_terse, devices.stdout.splitlines())
            if len(parts) == 4
            and parts[1] == "wifi"
            and (self._interface is None or parts[0] == self._interface)
        ]
        if not wifi:
            return NetworkStatus(hostname, "no-wifi", None, None, addresses)
        device, _, state, con_uuid = wifi[0]
        if not state.startswith("connected") or not con_uuid:
            return NetworkStatus(hostname, "disconnected", None, device, addresses)
        mode = self._nmcli("-g", "802-11-wireless.mode", "connection", "show", "uuid", con_uuid)
        ssid = self._nmcli("-g", "802-11-wireless.ssid", "connection", "show", "uuid", con_uuid)
        is_ap = unescape_value(mode.stdout.strip()) == "ap"
        return NetworkStatus(
            hostname,
            "ap" if is_ap else "client",
            unescape_value(ssid.stdout.strip()) or None,
            device,
            addresses,
        )

    def join(self, creds: WifiCredentials, *, timeout_s: float) -> JoinResult:
        """Write a profile for ``creds``, bring it up, and undo it on failure.

        A failed join leaves the profiles exactly as they were: a mistyped
        passphrase must not persist, and must not close the gate by counting
        as a configured network.
        """
        return self._install(creds, hidden=False, timeout_s=timeout_s)

    def _install(
        self, creds: WifiCredentials, *, hidden: bool, timeout_s: float | None
    ) -> JoinResult:
        """Write and load a profile; bring it up too when ``timeout_s`` is given.

        ``hidden`` is the caller's word that the network does not announce
        itself. A join also treats a network missing from the scan as hidden
        (so NetworkManager probes for it); saving one for later does not,
        since "not here right now" is the usual reason it is missing.
        """
        with self._lock:
            self._secret = creds.password if len(creds.password) >= 4 else ""
            try:
                return self._install_locked(creds, hidden=hidden, timeout_s=timeout_s)
            finally:
                self._secret = ""

    def _install_locked(
        self, creds: WifiCredentials, *, hidden: bool, timeout_s: float | None
    ) -> JoinResult:
        activate = timeout_s is not None
        seen = {n.ssid: n for n in self.scan()}
        network = seen.get(creds.ssid)
        try:
            key_mgmt = choose_key_mgmt(network.security if network else None, creds.password)
        except InvalidCredentialsError as exc:
            return JoinResult(False, str(exc), KIND_INVALID)
        except JoinRefusedError as exc:
            return JoinResult(False, str(exc), KIND_REFUSED)

        path = self._connections_dir / profile_filename(creds.ssid)
        previous = path.read_text() if path.exists() else None
        con_uuid = profile_uuid(creds.ssid)
        journal = _journal_path(path)
        text = render_keyfile(
            creds,
            key_mgmt,
            hidden=hidden or (network is None and activate),
            interface=self._interface,
        )
        try:
            # Only a join is journalled: a profile that is written and
            # loaded but never activated cannot cost anyone their link.
            if activate:
                write_secret_file(journal, json.dumps({"uuid": con_uuid, "previous": previous}))
            write_secret_file(path, text)
        except OSError as exc:
            return JoinResult(False, f"cannot write {path}: {exc}", KIND_INTERNAL)
        loaded = self._nmcli("connection", "load", str(path))
        if loaded.returncode == 0:
            if timeout_s is None:
                return JoinResult(True)
            up = self._nmcli(
                "--wait",
                f"{timeout_s:g}",
                "connection",
                "up",
                "uuid",
                con_uuid,
                *self._ifname(),
                timeout=timeout_s + 10,
            )
            if up.returncode == 0:
                journal.unlink(missing_ok=True)
                return JoinResult(True)
            detail = up.stderr.strip() or "activation failed"
            kind = KIND_JOIN_FAILED
        else:
            detail = loaded.stderr.strip() or "NetworkManager refused the profile"
            kind = KIND_INVALID
        self._restore(path, con_uuid, previous)
        journal.unlink(missing_ok=True)
        return JoinResult(False, detail, kind)

    # --- saved networks (N5.2) --------------------------------------------

    def _connection_rows(self) -> list[tuple[str, str, str, bool]]:
        """``(uuid, name, type, active)`` of every profile NetworkManager holds."""
        listed = self._nmcli("-t", "-f", "UUID,NAME,TYPE,ACTIVE", "connection", "show")
        if listed.returncode != 0:
            # Unknown is not "none": an empty list here would read as "nothing
            # saved" to a page that is about to offer to add the first one.
            raise WifiOpError(
                KIND_INTERNAL, f"cannot list NetworkManager profiles: {listed.stderr.strip()}"
            )
        rows = []
        for line in listed.stdout.splitlines():
            parts = split_terse(line)
            if len(parts) == 4:
                rows.append((parts[0], parts[1], parts[2], _yes(parts[3])))
        return rows

    def _profile(self, row: tuple[str, str, str, bool]) -> tuple[str, SavedNetwork]:
        """``(mode, network)`` for a WiFi row, from the profile's own settings."""
        con_uuid, name, _, active = row
        shown = self._nmcli(
            "-t", "-f", ",".join(_PROFILE_PROPERTIES), "connection", "show", "uuid", con_uuid
        )
        if shown.returncode != 0:
            raise WifiOpError(
                KIND_INTERNAL, f"cannot read profile {con_uuid}: {shown.stderr.strip()}"
            )
        props = parse_profile_properties(shown.stdout)
        autoconnect = props.get("connection.autoconnect", "yes")
        network = SavedNetwork(
            id=con_uuid,
            name=name,
            ssid=props.get("802-11-wireless.ssid", ""),
            priority=_priority(props.get("connection.autoconnect-priority", "0")),
            autoconnect=_yes(autoconnect) if autoconnect else True,
            hidden=_yes(props.get("802-11-wireless.hidden", "")),
            security=props.get("802-11-wireless-security.key-mgmt") or None,
            active=active,
        )
        return props.get("802-11-wireless.mode", ""), network

    def saved_networks(self) -> list[SavedNetwork]:
        """Every WiFi client profile -- ours or not, never an access point."""
        with self._lock:
            networks = []
            for row in self._connection_rows():
                if row[2] not in _WIFI_TYPES:
                    continue
                mode, network = self._profile(row)
                if _is_client_mode(mode):
                    networks.append(network)
            return networks

    def _client_profile(self, con_id: object) -> SavedNetwork:
        """The saved client profile ``con_id`` names; never any other profile.

        Decided by what NetworkManager says the profile *is*, not by its name:
        ``forget`` must not be able to delete ``canary-wired``, an access-point
        profile, or anything else this page does not manage.
        """
        row = next((r for r in self._connection_rows() if r[0] == con_id), None)
        if row is None:
            raise WifiOpError(KIND_NOT_FOUND, f"no saved network {con_id!r}")
        if row[2] not in _WIFI_TYPES:
            raise WifiOpError(KIND_REFUSED, f"{row[1]!r} is not a WiFi network profile")
        mode, network = self._profile(row)
        if not _is_client_mode(mode):
            raise WifiOpError(
                KIND_REFUSED, f"{row[1]!r} is a WiFi {mode} profile, not a network to join"
            )
        return network

    def _wifi_interface(self) -> str | None:
        if self._interface:
            return self._interface
        devices = self._nmcli("-t", "-f", "DEVICE,TYPE", "device", "status")
        for parts in map(split_terse, devices.stdout.splitlines()):
            if len(parts) == 2 and parts[1] == "wifi":
                return parts[0]
        return None

    def wifi_state(self, *, rescan: bool = False) -> WifiState:
        """Saved networks beside the scan; ``rescan`` asks the radio first.

        A listing that cannot be read raises instead of answering empty. The
        scan alone may fail quietly on a plain read (the saved networks are
        still worth showing); a rescan someone asked for may not.
        """
        with self._lock:
            saved = self.saved_networks()
            interface = self._wifi_interface()
            scan: list[WifiNetwork] = []
            if interface is not None:
                if rescan:
                    ask = self._nmcli(
                        "-t",
                        "-f",
                        "SSID,SIGNAL,SECURITY",
                        "device",
                        "wifi",
                        "list",
                        "ifname",
                        interface,
                        "--rescan",
                        "yes",
                        timeout=_RESCAN_TIMEOUT_S,
                    )
                    if ask.returncode == 0:
                        self._scanned_at = self._clock()
                        scan = parse_scan(ask.stdout)
                    else:
                        # NetworkManager refuses a second scan within seconds
                        # of the last, and while the radio is busy; the cache
                        # it keeps is still the best answer.
                        log.warning("rescan refused (%d): %s", ask.returncode, ask.stderr.strip())
                        scan = self._cached_scan(interface)
                else:
                    scan = self._cached_scan(interface)
            return build_wifi_state(saved, scan, scanned_at=self._scanned_at, interface=interface)

    def _cached_scan(self, interface: str) -> list[WifiNetwork]:
        listed = self._nmcli(
            "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", interface
        )
        return parse_scan(listed.stdout) if listed.returncode == 0 else []

    def add_network(
        self,
        ssid: object,
        password: object,
        *,
        hidden: bool,
        join: bool,
        timeout_s: float,
    ) -> str:
        """Save a network, joining it too when ``join``; returns its profile id.

        Joining is undone if it fails (:meth:`join`). Saving for later writes
        and loads the profile and activates nothing. Either way an SSID that
        another profile already holds is refused rather than duplicated: two
        profiles for one network make "which one will it use" a coin toss.
        """
        name = _checked_ssid(ssid)
        if password is not None and not isinstance(password, str):
            raise WifiOpError(KIND_INVALID, "the passphrase must be text")
        creds = WifiCredentials(name, password or "")
        con_uuid = profile_uuid(name)
        with self._lock:
            for held in self.saved_networks():
                if held.ssid == name and held.id != con_uuid:
                    raise WifiOpError(
                        KIND_REFUSED,
                        f"{held.name!r} already holds {name!r}; forget it first, then add it again",
                    )
            result = self._install(creds, hidden=hidden, timeout_s=timeout_s if join else None)
        if not result.ok:
            raise WifiOpError(result.kind or KIND_JOIN_FAILED, result.detail)
        return con_uuid

    def connect_network(self, con_id: object, *, timeout_s: float) -> None:
        """Bring a saved network up now."""
        with self._lock:
            network = self._client_profile(con_id)
            up = self._nmcli(
                "--wait",
                f"{timeout_s:g}",
                "connection",
                "up",
                "uuid",
                network.id,
                *self._ifname(),
                timeout=timeout_s + 10,
            )
        if up.returncode != 0:
            raise WifiOpError(KIND_JOIN_FAILED, up.stderr.strip() or "activation failed")

    def forget_network(self, con_id: object) -> None:
        """Delete a saved WiFi client profile; nothing else can be deleted here."""
        with self._lock:
            network = self._client_profile(con_id)
            gone = self._nmcli("connection", "delete", "uuid", network.id)
        if gone.returncode != 0:
            raise WifiOpError(KIND_INTERNAL, gone.stderr.strip() or "NetworkManager refused")

    def set_order(self, ids: object) -> None:
        """Rank the saved networks, first wins: priorities 100, 90, 80, ...

        ``ids`` must be exactly the saved client profiles. A partial list would
        leave the rest ranked against numbers the caller never saw, and the
        order on screen would not be the order NetworkManager uses.
        """
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise WifiOpError(KIND_INVALID, "ids must be a list of profile ids")
        with self._lock:
            saved = self.saved_networks()
            held = {n.id: n for n in saved}
            problems = []
            if len(set(ids)) != len(ids):
                problems.append("an id is listed twice")
            if unknown := sorted(set(ids) - set(held)):
                problems.append(f"not saved WiFi networks: {', '.join(unknown)}")
            if missing := sorted(set(held) - set(ids)):
                problems.append(f"missing from the order: {', '.join(missing)}")
            if problems:
                raise WifiOpError(KIND_INVALID, "; ".join(problems))
            if len(ids) > _MAX_ORDERED:
                raise WifiOpError(KIND_INVALID, f"at most {_MAX_ORDERED} networks can be ordered")
            step = max(1, min(10, (_TOP_PRIORITY - 1) // max(len(ids), 1)))
            changed: list[SavedNetwork] = []
            for rank, con_id in enumerate(ids):
                priority = _TOP_PRIORITY - rank * step
                done = self._nmcli(
                    "connection",
                    "modify",
                    "uuid",
                    con_id,
                    "connection.autoconnect-priority",
                    str(priority),
                )
                if done.returncode != 0:
                    for earlier in changed:  # best effort: leave the old order
                        self._nmcli(
                            "connection",
                            "modify",
                            "uuid",
                            earlier.id,
                            "connection.autoconnect-priority",
                            str(earlier.priority),
                        )
                    raise WifiOpError(
                        KIND_INTERNAL,
                        f"could not set the priority of {held[con_id].ssid!r}: "
                        f"{done.stderr.strip() or 'NetworkManager refused'}",
                    )
                changed.append(held[con_id])

    def recover(self) -> list[str]:
        """Undo joins that never finished; returns the profile files restored.

        A journal left behind means the process died between writing a
        profile and learning whether it works. That profile must not survive:
        it would count as configured and lock the gate on credentials nobody
        validated. One that is active did work, and is kept.
        """
        restored = []
        for journal in sorted(self._connections_dir.glob(f".{PROFILE_PREFIX}*{_JOURNAL_SUFFIX}")):
            path = journal.with_name(journal.name[1 : -len(_JOURNAL_SUFFIX)])
            try:
                entry = json.loads(journal.read_text())
                con_uuid, previous = str(entry["uuid"]), entry["previous"]
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("unreadable join journal %s; leaving %s as it is", journal, path)
                journal.unlink(missing_ok=True)
                continue
            state = self._nmcli("-g", "GENERAL.STATE", "connection", "show", "uuid", con_uuid)
            if unescape_value(state.stdout.strip()) != "activated":
                log.warning("undoing an interrupted join: %s", path.name)
                self._restore(path, con_uuid, previous if isinstance(previous, str) else None)
                restored.append(path.name)
            journal.unlink(missing_ok=True)
        return restored

    def _restore(self, path: Path, con_uuid: str, previous: str | None) -> None:
        if previous is None:
            self._nmcli("connection", "delete", "uuid", con_uuid)
            path.unlink(missing_ok=True)
            return
        write_secret_file(path, previous)
        self._nmcli("connection", "load", str(path))
