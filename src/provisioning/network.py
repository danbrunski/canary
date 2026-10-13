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
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from src.provisioning.improv import WifiCredentials

log = logging.getLogger(__name__)

DEFAULT_CONNECTIONS_DIR = Path("/etc/NetworkManager/system-connections")
#: Profiles this service writes are named and filed under this prefix, so an
#: operator can tell them from hand-made ones (``canary-home``).
PROFILE_PREFIX = "canary-wifi-"
_PROFILE_NAMESPACE = uuid.UUID("6c1d7f5e-2f0a-4b8e-9a51-4ca9a7e0b1d3")
_NMCLI_TIMEOUT_S = 20.0
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
            raise JoinRefusedError("the network needs a passphrase")
        return "sae"
    if not (8 <= len(password) <= 63 and password.isascii() and password.isprintable()) and not (
        _HEX_PSK.match(password)
    ):
        raise JoinRefusedError(
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


class NetworkManagerCli:
    def __init__(
        self,
        runner: Runner = run_command,
        *,
        connections_dir: Path = DEFAULT_CONNECTIONS_DIR,
        interface: str | None = None,
    ) -> None:
        self._run = runner
        self._connections_dir = connections_dir
        self._interface = interface

    def _nmcli(self, *args: str, timeout: float = _NMCLI_TIMEOUT_S) -> RunResult:
        result = self._run(["nmcli", *args], timeout=timeout)
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
        seen = {n.ssid: n for n in self.scan()}
        network = seen.get(creds.ssid)
        try:
            key_mgmt = choose_key_mgmt(network.security if network else None, creds.password)
        except JoinRefusedError as exc:
            return JoinResult(False, str(exc))

        path = self._connections_dir / profile_filename(creds.ssid)
        previous = path.read_text() if path.exists() else None
        con_uuid = profile_uuid(creds.ssid)
        journal = _journal_path(path)
        try:
            write_secret_file(journal, json.dumps({"uuid": con_uuid, "previous": previous}))
            write_secret_file(
                path,
                render_keyfile(creds, key_mgmt, hidden=network is None, interface=self._interface),
            )
        except OSError as exc:
            return JoinResult(False, f"cannot write {path}: {exc}")
        loaded = self._nmcli("connection", "load", str(path))
        if loaded.returncode == 0:
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
        else:
            detail = loaded.stderr.strip() or "NetworkManager refused the profile"
        self._restore(path, con_uuid, previous)
        journal.unlink(missing_ok=True)
        return JoinResult(False, detail)

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
