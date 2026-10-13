"""The Improv state machine, independent of the BLE transport.

The BlueZ layer (:mod:`.bluez`) calls :meth:`ImprovController.on_write` with
each chunk written to the RPC Command characteristic, reads characteristic
values from :meth:`ImprovController.value`, and is told what to notify
through a :class:`Sink`. Everything that blocks (``nmcli``, ``ip``) runs in a
worker thread so a GATT read or write is answered while a join is under way.

States follow the spec, with authorization supplied by :mod:`.gate`:

- *authorization required* -- the gate is locked; a WiFi write is refused.
- *authorized* -- unprovisioned, or a window is open.
- *provisioning* -- a join is running.
- *provisioned* -- this process joined a network. It stays so until a window
  opens after it, which reads as the operator asking to change networks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.provisioning.gate import (
    DEFAULT_WINDOW_PATH,
    GateVerdict,
    decide,
    read_window_remaining,
)
from src.provisioning.improv import (
    CHAR_CAPABILITIES_UUID,
    CHAR_ERROR_UUID,
    CHAR_RPC_RESULT_UUID,
    CHAR_STATE_UUID,
    Capability,
    Command,
    CommandAssembler,
    ImprovError,
    ImprovPacketError,
    State,
    WifiCredentials,
    encode_result,
    encode_scan_result,
    parse_command,
    parse_wifi_settings,
)
from src.provisioning.network import JoinResult, NetworkStatus, WifiNetwork
from src.provisioning.status import build_status, dashboard_url

log = logging.getLogger(__name__)

CAPABILITIES = Capability.DEVICE_INFO | Capability.SCAN_WIFI
#: Without a negotiated MTU a client gets a 20-byte notification payload, too
#: small for any scan result; BlueZ reports the MTU in each write's options,
#: and Web Bluetooth clients negotiate far more. Assume the frame maximum.
DEFAULT_MAX_FRAME = 258
DEFAULT_JOIN_TIMEOUT_S = 45.0
#: How long a profile count stays fresh. The window file is read every tick;
#: profiles change only when someone provisions or edits NetworkManager.
PROFILE_TTL_S = 15.0
#: A partial frame older than this is abandoned. Without it, two stray bytes
#: from anyone in range (``00 ff``) would swallow the next client's frames.
ASSEMBLY_IDLE_S = 2.0


class NetworkBackend(Protocol):
    def scan(self) -> list[WifiNetwork]: ...
    def client_profiles(self) -> list[str]: ...
    def status(self) -> NetworkStatus: ...
    def join(self, creds: WifiCredentials, *, timeout_s: float) -> JoinResult: ...


class Sink(Protocol):
    """What the transport does when a value changes."""

    def notify(self, char_uuid: str, value: bytes) -> None: ...
    def state_changed(self, state: State) -> None: ...


@dataclass(frozen=True)
class DeviceInfo:
    firmware: str
    version: str
    hardware: str
    name: str

    def as_strings(self) -> list[str]:
        return [self.firmware, self.version, self.hardware, self.name]


class ImprovController:
    def __init__(
        self,
        network: NetworkBackend,
        sink: Sink,
        *,
        device_info: DeviceInfo,
        dashboard_port: int,
        window_path: Path = DEFAULT_WINDOW_PATH,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._network = network
        self._sink = sink
        self._device_info = device_info
        self._dashboard_port = dashboard_port
        self._window_path = window_path
        self._join_timeout_s = join_timeout_s
        self._clock = clock
        self._assembler = CommandAssembler()
        self._values: dict[str, bytes] = {
            CHAR_STATE_UUID: bytes([State.AUTHORIZATION_REQUIRED]),
            CHAR_ERROR_UUID: bytes([ImprovError.NONE]),
            CHAR_RPC_RESULT_UUID: b"",
            CHAR_CAPABILITIES_UUID: bytes([CAPABILITIES]),
        }
        self._profiles: int | None = None
        self._profiles_at = 0.0
        self._last_reason = ""
        self._provisioned = False
        self._joining: asyncio.Task[None] | None = None
        self._max_frame = DEFAULT_MAX_FRAME
        self._last_chunk_at = float("-inf")

    # --- reads ------------------------------------------------------------

    def value(self, char_uuid: str) -> bytes:
        return self._values.get(char_uuid, b"")

    @property
    def state(self) -> State:
        return State(self._values[CHAR_STATE_UUID][0])

    @property
    def error(self) -> ImprovError:
        return ImprovError(self._values[CHAR_ERROR_UUID][0])

    async def status_value(self) -> bytes:
        status = await asyncio.to_thread(self._network.status)
        return build_status(status, await self._verdict(), dashboard_port=self._dashboard_port)

    # --- gate -------------------------------------------------------------

    async def _verdict(self, *, fresh: bool = False) -> GateVerdict:
        now = self._clock()
        if fresh or self._profiles is None or now - self._profiles_at > PROFILE_TTL_S:
            try:
                self._profiles = len(await asyncio.to_thread(self._network.client_profiles))
            except RuntimeError as exc:
                # Unknown counts as provisioned: the gate fails closed.
                log.warning("provisioning gate: %s", exc)
                self._profiles = 1
            self._profiles_at = now
        return decide(
            client_profiles=self._profiles,
            window_remaining_s=read_window_remaining(self._window_path, now=now),
        )

    def _resting_state(self, verdict: GateVerdict) -> State:
        if self._provisioned:
            return State.PROVISIONED
        return State.AUTHORIZED if verdict.authorized else State.AUTHORIZATION_REQUIRED

    async def refresh(self) -> None:
        """Re-read the gate and publish a state change; call it every tick."""
        verdict = await self._verdict()
        if verdict.reason == "window" and self._last_reason != "window":
            self._provisioned = False
        self._last_reason = verdict.reason
        if self._joining is None or self._joining.done():
            self._set_state(self._resting_state(verdict))

    # --- writes -----------------------------------------------------------

    async def on_write(self, chunk: bytes, *, mtu: int | None = None) -> None:
        if mtu:
            self._max_frame = mtu - 3
        now = self._clock()
        if now - self._last_chunk_at > ASSEMBLY_IDLE_S:
            self._assembler.reset()
        self._last_chunk_at = now
        frame = self._assembler.feed(chunk)
        if frame is None:
            return
        # Clear the error first, so an error that repeats the last one still
        # notifies: a client waits on that notification for its answer.
        self._set_error(ImprovError.NONE)
        try:
            command = parse_command(frame)
            await self._dispatch(command.command, command.data)
        except ImprovPacketError as exc:
            log.warning("improv: rejected write: %s", exc)
            self._set_error(exc.error)

    async def _dispatch(self, command: int, data: bytes) -> None:
        if command == Command.WIFI_SETTINGS:
            await self._wifi_settings(data)
        elif command == Command.DEVICE_INFO:
            self._publish_result(encode_result(command, self._device_info.as_strings()))
        elif command == Command.SCAN_WIFI:
            networks = await asyncio.to_thread(self._network.scan)
            triples = [(n.ssid, n.rssi_dbm, n.improv_auth) for n in networks]
            self._publish_result(encode_scan_result(triples, self._max_frame))
        else:
            raise ImprovPacketError(
                ImprovError.UNKNOWN_RPC_COMMAND, f"command 0x{command:02x} is not supported"
            )

    async def _wifi_settings(self, data: bytes) -> None:
        creds = parse_wifi_settings(data)
        verdict = await self._verdict(fresh=True)
        if not verdict.authorized:
            log.warning("improv: WiFi settings for %r refused, gate is locked", creds.ssid)
            self._set_error(ImprovError.NOT_AUTHORIZED)
            return
        if self._joining is not None and not self._joining.done():
            log.warning("improv: WiFi settings for %r refused, a join is running", creds.ssid)
            self._set_error(ImprovError.UNKNOWN)
            return
        log.info("improv: joining %r (gate: %s)", creds.ssid, verdict.reason)
        self._set_state(State.PROVISIONING)
        self._joining = asyncio.create_task(self._join(creds))

    async def _join(self, creds: WifiCredentials) -> None:
        try:
            result = await asyncio.to_thread(
                self._network.join, creds, timeout_s=self._join_timeout_s
            )
        except Exception as exc:  # a crashed join must not wedge the state at PROVISIONING
            log.exception("improv: join of %r crashed", creds.ssid)
            result = JoinResult(False, str(exc))
        self._profiles = None
        if not result.ok:
            log.warning("improv: could not join %r: %s", creds.ssid, result.detail)
            self._set_error(ImprovError.UNABLE_TO_CONNECT)
            self._set_state(self._resting_state(await self._verdict(fresh=True)))
            return
        status = await asyncio.to_thread(self._network.status)
        url = dashboard_url(status, self._dashboard_port)
        log.info("improv: joined %r, dashboard at %s", creds.ssid, url)
        self._provisioned = True
        self._set_state(State.PROVISIONED)
        # A plain URL: nothing secret crosses Bluetooth. The dashboard it opens
        # pairs the phone by key cycle (src/control/key_cycle_pairing.py).
        self._publish_result(encode_result(Command.WIFI_SETTINGS, [url] if url else []))

    async def wait_for_join(self) -> None:
        """Await a running join (tests, and an orderly shutdown)."""
        if self._joining is not None:
            await self._joining

    # --- publishing -------------------------------------------------------

    def _publish(self, char_uuid: str, value: bytes) -> bool:
        if self._values.get(char_uuid) == value:
            return False
        self._values[char_uuid] = value
        self._sink.notify(char_uuid, value)
        return True

    def _set_state(self, state: State) -> None:
        if self._publish(CHAR_STATE_UUID, bytes([state])):
            self._sink.state_changed(state)

    def _set_error(self, error: ImprovError) -> None:
        self._publish(CHAR_ERROR_UUID, bytes([error]))

    def _publish_result(self, frame: bytes) -> None:
        # Always notify, even when identical: a client waits for the result
        # of the command it just sent, and a repeated scan can repeat itself.
        self._values[CHAR_RPC_RESULT_UUID] = frame
        self._sink.notify(CHAR_RPC_RESULT_UUID, frame)
