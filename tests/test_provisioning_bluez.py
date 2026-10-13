"""BlueZ GATT objects (src/provisioning/bluez.py) without a system bus.

BlueZ calls these objects by D-Bus signature, so the signatures are the
contract worth pinning: ``ReadValue(a{sv}) -> ay``, ``WriteValue(ay, a{sv})``.
"""

from __future__ import annotations

from typing import Any

from dbus_fast import Variant

from src.provisioning.bluez import (
    Advertisement,
    GattCharacteristic,
    GattService,
    UnnamedAdvertisement,
)
from src.provisioning.improv import SERVICE_DATA_UUID, SERVICE_UUID, Capability, State


def _methods(obj: Any) -> dict[str, tuple[str, str]]:
    xml = obj.introspect()
    return {
        m.name: (
            "".join(a.signature for a in m.in_args),
            "".join(a.signature for a in m.out_args),
        )
        for m in xml.methods
    }


def _properties(obj: Any) -> dict[str, str]:
    return {p.name: p.signature for p in obj.introspect().properties}


def _call(obj: Any, name: str, *args: Any) -> Any:
    # dbus_method wraps the handler; the original is kept for dispatch.
    return getattr(type(obj), name).__dict__["__DBUS_METHOD"].fn(obj, *args)


def test_characteristic_signatures() -> None:
    service = GattService("/app/service0", SERVICE_UUID)
    char = GattCharacteristic("/app/service0/char0", "uuid", service, ["read", "notify"])
    assert _methods(char) == {
        "ReadValue": ("a{sv}", "ay"),
        "WriteValue": ("aya{sv}", ""),
        "StartNotify": ("", ""),
        "StopNotify": ("", ""),
    }
    assert _properties(char) == {
        "UUID": "s",
        "Service": "o",
        "Flags": "as",
        "Value": "ay",
        "Notifying": "b",
    }
    assert _properties(service) == {"UUID": "s", "Primary": "b"}


async def test_read_honours_the_long_read_offset() -> None:
    service = GattService("/app/service1", "status")

    async def read(offset: int) -> bytes:
        return b"0123456789"

    char = GattCharacteristic("/app/service1/char0", "s", service, ["read"], read=read)
    assert await _call(char, "read_value", {}) == b"0123456789"
    assert await _call(char, "read_value", {"offset": Variant("q", 4)}) == b"456789"


async def test_write_passes_the_mtu() -> None:
    service = GattService("/app/service0", SERVICE_UUID)
    seen: list[tuple[bytes, int | None]] = []

    async def write(value: bytes, mtu: int | None) -> None:
        seen.append((value, mtu))

    char = GattCharacteristic("/app/service0/char2", "rpc", service, ["write"], write=write)
    await _call(char, "write_value", b"\x03\x00\x03", {"mtu": Variant("q", 185)})
    await _call(char, "write_value", b"\x03\x00\x03", {})
    assert seen == [(b"\x03\x00\x03", 185), (b"\x03\x00\x03", None)]


def test_advertisement_carries_improv_service_data() -> None:
    advert = Advertisement("canary-1a2b", State.AUTHORIZED, Capability.SCAN_WIFI)
    assert _properties(advert)["ServiceData"] == "a{sv}"
    assert advert.service_uuids == [SERVICE_UUID]
    data = advert.service_data[SERVICE_DATA_UUID]
    assert data.signature == "ay"
    assert data.value == bytes([State.AUTHORIZED, Capability.SCAN_WIFI, 0, 0, 0, 0])
    advert.state = State.PROVISIONED
    assert advert.service_data[SERVICE_DATA_UUID].value[0] == State.PROVISIONED


def test_unnamed_advertisement_drops_only_the_name() -> None:
    advert = UnnamedAdvertisement("", State.AUTHORIZED, Capability.SCAN_WIFI)
    props = _properties(advert)
    assert "LocalName" not in props
    assert {"Type", "ServiceUUIDs", "ServiceData"} <= set(props)


def test_advertisement_is_discoverable_and_reports_release() -> None:
    advert = Advertisement("canary-1a2b", State.AUTHORIZED, Capability.SCAN_WIFI)
    assert _properties(advert)["Discoverable"] == "b"
    released: list[bool] = []
    advert.on_release = lambda: released.append(True)
    _call(advert, "release")
    assert released == [True]
