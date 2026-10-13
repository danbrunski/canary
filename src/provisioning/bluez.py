"""The BlueZ side: a GATT application and an LE advertisement over D-Bus.

Two services are registered with ``org.bluez.GattManager1``:

- the **Improv** service (:mod:`.improv`), driven by :class:`ImprovController`;
- CANary's **status** service (:mod:`.status`), one read-only characteristic.

The advertisement carries the Improv service UUID and its 6-byte service data
in the advertising packet itself (spec v2.1: neither may sit in the scan
response). With BlueZ's flags that fills all 31 bytes, so the local name goes
in the scan response, which BlueZ builds itself. When the Improv state
changes the advertisement is re-registered, since its service data carries
the state.

D-Bus calls into BlueZ are made with explicit messages rather than generated
proxies, so every call is typed and names its interface and signature.

No ``from __future__ import annotations`` here: dbus-fast reads the
``Annotated`` D-Bus signatures from the live annotations.
"""

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from dbus_fast import BusType, Message, MessageType, PropertyAccess, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import (
    DBusBool,
    DBusBytes,
    DBusDict,
    DBusObjectPath,
    DBusSignature,
    DBusStr,
)
from dbus_fast.service import ServiceInterface, dbus_method, dbus_property

from src.provisioning.controller import ImprovController
from src.provisioning.improv import (
    CHAR_CAPABILITIES_UUID,
    CHAR_ERROR_UUID,
    CHAR_RPC_COMMAND_UUID,
    CHAR_RPC_RESULT_UUID,
    CHAR_STATE_UUID,
    SERVICE_DATA_UUID,
    SERVICE_UUID,
    Capability,
    State,
    service_data,
)
from src.provisioning.status import STATUS_CHAR_UUID, STATUS_SERVICE_UUID

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
APP_PATH = "/org/canary/provision"
ADVERT_PATH = f"{APP_PATH}/advertisement0"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHAR_IFACE = "org.bluez.GattCharacteristic1"
ADVERT_IFACE = "org.bluez.LEAdvertisement1"

DBusStrList = Annotated[list[str], DBusSignature("as")]

ReadHandler = Callable[[int], Awaitable[bytes]]
WriteHandler = Callable[[bytes, int | None], Awaitable[None]]


def _option_int(options: dict[str, Variant], key: str) -> int | None:
    variant = options.get(key)
    return int(variant.value) if variant is not None else None


class GattService(ServiceInterface):
    def __init__(self, path: str, uuid: str) -> None:
        super().__init__(GATT_SERVICE_IFACE)
        self.path = path
        self._uuid = uuid

    @dbus_property(access=PropertyAccess.READ, name="UUID")
    def uuid(self) -> DBusStr:
        return self._uuid

    @dbus_property(access=PropertyAccess.READ, name="Primary")
    def primary(self) -> DBusBool:
        return True


class GattCharacteristic(ServiceInterface):
    """One characteristic; reads and writes are delegated to handlers."""

    def __init__(
        self,
        path: str,
        uuid: str,
        service: GattService,
        flags: list[str],
        *,
        read: ReadHandler | None = None,
        write: WriteHandler | None = None,
    ) -> None:
        super().__init__(GATT_CHAR_IFACE)
        self.path = path
        self._uuid = uuid
        self._service_path = service.path
        self._flags = flags
        self._read = read
        self._write = write
        self._value = b""
        self._notifying = False

    @dbus_property(access=PropertyAccess.READ, name="UUID")
    def uuid(self) -> DBusStr:
        return self._uuid

    @dbus_property(access=PropertyAccess.READ, name="Service")
    def service(self) -> DBusObjectPath:
        return self._service_path

    @dbus_property(access=PropertyAccess.READ, name="Flags")
    def flags(self) -> DBusStrList:
        return self._flags

    @dbus_property(access=PropertyAccess.READ, name="Value")
    def value(self) -> DBusBytes:
        return self._value

    @dbus_property(access=PropertyAccess.READ, name="Notifying")
    def notifying(self) -> DBusBool:
        return self._notifying

    @dbus_method(name="ReadValue")
    async def read_value(self, options: DBusDict) -> DBusBytes:
        offset = _option_int(options, "offset") or 0
        if self._read is None:
            return b""
        return (await self._read(offset))[offset:]

    @dbus_method(name="WriteValue")
    async def write_value(self, value: DBusBytes, options: DBusDict) -> None:
        if self._write is not None:
            await self._write(bytes(value), _option_int(options, "mtu"))

    @dbus_method(name="StartNotify")
    def start_notify(self) -> None:
        self._notifying = True

    @dbus_method(name="StopNotify")
    def stop_notify(self) -> None:
        self._notifying = False

    def push(self, value: bytes) -> None:
        """Update the value; BlueZ turns the change into a notification."""
        self._value = value
        if self._notifying:
            self.emit_properties_changed({"Value": value})


class Advertisement(ServiceInterface):
    def __init__(self, local_name: str, state: State, capabilities: int) -> None:
        super().__init__(ADVERT_IFACE)
        #: Called when BlueZ drops the advertisement (adapter reset, rfkill).
        self.on_release: Callable[[], None] = lambda: None
        self._local_name = local_name
        self.state = state
        self.capabilities = capabilities

    @dbus_property(access=PropertyAccess.READ, name="Type")
    def type(self) -> DBusStr:
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ, name="ServiceUUIDs")
    def service_uuids(self) -> DBusStrList:
        return [SERVICE_UUID]

    @dbus_property(access=PropertyAccess.READ, name="ServiceData")
    def service_data(self) -> DBusDict:
        data = service_data(self.state, Capability(self.capabilities))
        return {SERVICE_DATA_UUID: Variant("ay", data)}

    @dbus_property(access=PropertyAccess.READ, name="Discoverable")
    def discoverable(self) -> DBusBool:
        # Sets the general-discoverable flag without making the adapter
        # discoverable for classic Bluetooth.
        return True

    @dbus_property(access=PropertyAccess.READ, name="LocalName")
    def local_name(self) -> DBusStr:
        return self._local_name

    @dbus_method(name="Release")
    def release(self) -> None:
        log.info("BlueZ released the advertisement")
        self.on_release()


class UnnamedAdvertisement(Advertisement):
    """The fallback when BlueZ will not move the local name to the scan response.

    The Improv UUID and service data must stay in the advertising packet, so
    the name is what gives way; the chooser then shows the device's address.
    """

    @dbus_property(access=PropertyAccess.READ, name="LocalName", disabled=True)
    def local_name(self) -> DBusStr:
        return self._local_name


async def _call(
    bus: MessageBus, path: str, interface: str, member: str, signature: str, body: list[Any]
) -> list[Any]:
    reply = await bus.call(
        Message(
            destination=BLUEZ,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=body,
        )
    )
    if reply is None or reply.message_type == MessageType.ERROR:
        detail = reply.body if reply is not None else "no reply"
        raise RuntimeError(
            f"{interface}.{member} on {path} failed: {reply and reply.error_name} {detail}"
        )
    return list(reply.body)


async def find_adapter(bus: MessageBus, name: str | None) -> str:
    """The object path of a BlueZ adapter that can serve GATT and advertise."""
    (objects,) = await _call(
        bus, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", "", []
    )
    for path, interfaces in sorted(objects.items()):
        if name is not None and not path.endswith(f"/{name}"):
            continue
        if (
            "org.bluez.GattManager1" in interfaces
            and "org.bluez.LEAdvertisingManager1" in interfaces
        ):
            return str(path)
    raise RuntimeError(
        f"no BlueZ adapter{f' named {name}' if name else ''} with GATT and LE advertising"
    )


async def _set_adapter(bus: MessageBus, adapter: str, prop: str, value: Variant) -> None:
    await _call(
        bus,
        adapter,
        "org.freedesktop.DBus.Properties",
        "Set",
        "ssv",
        ["org.bluez.Adapter1", prop, value],
    )


class BluezSink:
    """The controller's :class:`~.controller.Sink`, backed by GATT objects."""

    def __init__(self) -> None:
        self.characteristics: dict[str, GattCharacteristic] = {}
        self.on_state: Callable[[State], None] = lambda state: None

    def notify(self, char_uuid: str, value: bytes) -> None:
        char = self.characteristics.get(char_uuid)
        if char is not None:
            char.push(value)

    def state_changed(self, state: State) -> None:
        self.on_state(state)


class ProvisioningServer:
    """Owns the bus objects; :meth:`run` registers them and ticks the gate."""

    def __init__(
        self,
        bus: MessageBus,
        adapter: str,
        controller: ImprovController,
        sink: BluezSink,
        *,
        local_name: str,
        tick_s: float = 2.0,
    ) -> None:
        self._bus = bus
        self._adapter = adapter
        self._controller = controller
        self._tick_s = tick_s
        self._status_cache = b""
        self._advert = Advertisement(
            local_name, controller.state, controller.value(CHAR_CAPABILITIES_UUID)[0]
        )
        self._advert.on_release = self._released
        self._advert_lock = asyncio.Lock()
        self._advertising = False
        self._pending: set[asyncio.Task[None]] = set()
        sink.on_state = self._state_changed
        self.objects: list[GattService | GattCharacteristic] = []
        self._build(sink)

    def _build(self, sink: BluezSink) -> None:
        improv = GattService(f"{APP_PATH}/service0", SERVICE_UUID)
        status = GattService(f"{APP_PATH}/service1", STATUS_SERVICE_UUID)
        self.objects += [improv, status]

        def stored(char_uuid: str) -> ReadHandler:
            async def read(offset: int) -> bytes:
                return self._controller.value(char_uuid)

            return read

        async def write_rpc(value: bytes, mtu: int | None) -> None:
            await self._controller.on_write(value, mtu=mtu)

        specs: list[tuple[str, list[str]]] = [
            (CHAR_STATE_UUID, ["read", "notify"]),
            (CHAR_ERROR_UUID, ["read", "notify"]),
            (CHAR_RPC_COMMAND_UUID, ["write", "write-without-response"]),
            (CHAR_RPC_RESULT_UUID, ["read", "notify"]),
            (CHAR_CAPABILITIES_UUID, ["read"]),
        ]
        for index, (char_uuid, flags) in enumerate(specs):
            char = GattCharacteristic(
                f"{improv.path}/char{index}",
                char_uuid,
                improv,
                flags,
                read=stored(char_uuid) if "read" in flags else None,
                write=write_rpc if char_uuid == CHAR_RPC_COMMAND_UUID else None,
            )
            char.push(self._controller.value(char_uuid))
            sink.characteristics[char_uuid] = char
            self.objects.append(char)

        self.objects.append(
            GattCharacteristic(
                f"{status.path}/char0", STATUS_CHAR_UUID, status, ["read"], read=self._read_status
            )
        )

    async def _read_status(self, offset: int) -> bytes:
        # A long read arrives as several ReadValue calls with rising offsets;
        # build the value once so the pieces belong to the same JSON document.
        if offset == 0 or not self._status_cache:
            self._status_cache = await self._controller.status_value()
        return self._status_cache

    def _state_changed(self, state: State) -> None:
        self._advert.state = state
        task = asyncio.get_running_loop().create_task(self._readvertise())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _register_advert(self) -> None:
        await _call(
            self._bus,
            self._adapter,
            "org.bluez.LEAdvertisingManager1",
            "RegisterAdvertisement",
            "oa{sv}",
            [ADVERT_PATH, {}],
        )
        self._advertising = True

    async def _readvertise(self) -> None:
        async with self._advert_lock:
            try:
                if self._advertising:
                    await _call(
                        self._bus,
                        self._adapter,
                        "org.bluez.LEAdvertisingManager1",
                        "UnregisterAdvertisement",
                        "o",
                        [ADVERT_PATH],
                    )
                    self._advertising = False
                await self._register_advert()
            except RuntimeError:
                # Left unadvertised; the next tick tries again.
                log.exception("re-advertising failed")

    def _released(self) -> None:
        self._advertising = False

    async def _tick(self) -> None:
        await self._controller.refresh()
        if not self._advertising:
            await self._readvertise()

    async def run(self) -> None:
        for obj in self.objects:
            self._bus.export(obj.path, obj)
        self._bus.export(ADVERT_PATH, self._advert)
        await self._controller.refresh()
        await _call(
            self._bus,
            self._adapter,
            "org.bluez.GattManager1",
            "RegisterApplication",
            "oa{sv}",
            [APP_PATH, {}],
        )
        async with self._advert_lock:
            try:
                await self._register_advert()
            except RuntimeError:
                log.warning(
                    "advertisement refused with a local name; retrying without", exc_info=True
                )
                self._bus.unexport(ADVERT_PATH, self._advert)
                self._advert = UnnamedAdvertisement(
                    "", self._advert.state, self._advert.capabilities
                )
                self._advert.on_release = self._released
                self._bus.export(ADVERT_PATH, self._advert)
                await self._register_advert()
        log.info(
            "provisioning: advertising on %s, state %s", self._adapter, self._controller.state.name
        )
        while True:
            await asyncio.sleep(self._tick_s)
            try:
                await self._tick()
            except Exception:  # one bad tick must not take the service down
                log.exception("provisioning tick failed")


async def serve(
    make_controller: Callable[[BluezSink], ImprovController],
    *,
    adapter_name: str | None = None,
) -> None:
    """Connect to the system bus, power the adapter, and serve until cancelled."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    adapter = await find_adapter(bus, adapter_name)
    hostname = socket.gethostname()
    await _set_adapter(bus, adapter, "Powered", Variant("b", True))
    await _set_adapter(bus, adapter, "Alias", Variant("s", hostname))
    sink = BluezSink()
    server = ProvisioningServer(bus, adapter, make_controller(sink), sink, local_name=hostname)
    try:
        await server.run()
    finally:
        bus.disconnect()
