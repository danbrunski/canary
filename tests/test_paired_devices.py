"""Paired-device tokens (src/control/paired_devices.py)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from src.control.paired_devices import (
    LAST_SEEN_WRITE_S,
    TOKEN_PREFIX,
    PairedDevices,
    clean_name,
    default_path,
)

NOW = 1_800_000_000.0


def test_pair_authenticate_and_remove(tmp_path: Path) -> None:
    store = PairedDevices(tmp_path / "devices.json", clock=lambda: NOW)
    device, token = store.pair("Android phone", method="bluetooth-setup")
    assert token.startswith(TOKEN_PREFIX)
    assert store.authenticate(token) is not None
    assert store.authenticate(token + "x") is None
    assert store.authenticate("the-shared-token") is None
    removed = store.remove(device.id)
    assert removed is not None and removed.id == device.id
    assert store.authenticate(token) is None
    assert store.remove(device.id) is None


def test_only_a_digest_is_stored_and_it_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "control" / "devices.json"
    device, token = PairedDevices(path, clock=lambda: NOW).pair("Pixel", method="test")
    assert token not in path.read_text()
    reloaded = PairedDevices(path, clock=lambda: NOW)
    assert reloaded.authenticate(token) is not None
    assert [d.id for d in reloaded.list()] == [device.id]
    assert "token_sha256" not in device.public()


def test_last_seen_is_written_sparingly(tmp_path: Path) -> None:
    clock = [NOW]
    path = tmp_path / "devices.json"
    store = PairedDevices(path, clock=lambda: clock[0])
    _, token = store.pair("Pixel", method="test")

    def on_disk() -> float | None:
        last_seen: float | None = json.loads(path.read_text())["devices"][0]["last_seen"]
        return last_seen

    store.authenticate(token)
    assert on_disk() == NOW
    clock[0] += 1
    store.authenticate(token)
    assert store.list()[0].last_seen == NOW + 1  # memory moves every request
    assert on_disk() == NOW  # the file does not
    clock[0] += LAST_SEEN_WRITE_S
    store.authenticate(token)
    assert on_disk() == clock[0]


def test_unreadable_store_refuses_everyone_and_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    path.write_text("{not json")
    store = PairedDevices(path)
    assert store.list() == []
    aside = [p for p in tmp_path.iterdir() if ".unreadable-" in p.name]
    assert [p.read_text() for p in aside] == ["{not json"]
    store.pair("Pixel", method="test")  # never overwrites the torn file
    assert aside[0].read_text() == "{not json"


@pytest.mark.parametrize("text", ['{"devices": null}', "[]", '{"devices": [1]}', ""])
def test_malformed_store_does_not_crash(tmp_path: Path, text: str) -> None:
    path = tmp_path / "devices.json"
    path.write_text(text)
    assert PairedDevices(path).list() == []


def test_torn_write_falls_back_to_the_backup(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    store = PairedDevices(path, clock=lambda: NOW)
    _, first = store.pair("Pixel", method="test")
    store.pair("Laptop", method="test")  # the save that moves the first copy to .bak
    path.write_text('{"devices": [')  # power lost mid-write
    reloaded = PairedDevices(path, clock=lambda: NOW)
    assert reloaded.authenticate(first) is not None


def test_store_is_private(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    PairedDevices(path).pair("Pixel", method="test")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_pairing_the_disk_refused_does_not_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PairedDevices(tmp_path / "devices.json")

    def full(*_: object) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr("src.control.paired_devices._durable_write", full)
    with pytest.raises(OSError):
        store.pair("Pixel", method="test")
    assert store.list() == []


def test_names_are_cleaned() -> None:
    assert clean_name("  Pixel\n7 ") == "Pixel7"
    assert clean_name("") == "unnamed device"
    assert len(clean_name("x" * 500)) == 64


def test_default_path_follows_state_dir(tmp_path: Path) -> None:
    assert default_path(tmp_path) == tmp_path / "control" / "paired_devices.json"
    assert default_path(None).name == "paired_devices.json"


def test_rename_is_durable_and_keeps_the_token(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    store = PairedDevices(path, clock=lambda: NOW)
    device, token = store.pair("Android phone", method="key-cycle")
    renamed = store.rename(device.id, "  Dan's\nPixel ")
    assert renamed is not None and renamed.name == "Dan'sPixel"
    assert renamed.id == device.id and renamed.method == "key-cycle"
    reloaded = PairedDevices(path, clock=lambda: NOW)
    assert [d.name for d in reloaded.list()] == ["Dan'sPixel"]
    assert reloaded.authenticate(token) is not None
    assert store.rename("no-such-device", "x") is None


def test_a_rename_the_disk_refused_does_not_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PairedDevices(tmp_path / "devices.json")
    device, _ = store.pair("Pixel", method="test")

    def full(*_: object) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr("src.control.paired_devices._durable_write", full)
    with pytest.raises(OSError):
        store.rename(device.id, "Renamed")
    assert [d.name for d in store.list()] == ["Pixel"]
