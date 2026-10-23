"""W08.7: the ECU backup job against a fake ECU on a virtual CAN bus.

The fake answers the recipe's DiagnosticSessionControl and ReadMemoryByAddress
the way the 2008 Xterra's ECM did for UpRev on 2026-10-04 (single-frame
``50 xx``; ISO-TP multi-frame ``63`` + data after flow control), over a small
synthetic image so a whole read takes well under a second. Every refusal, the
claim's release on every exit path, an engine start mid-read, timeout retry
and abort-by-address, the telemetry exclusion, unverified-on-checksum-failure
and the byte-for-byte result are each pinned here.
"""

from __future__ import annotations

import asyncio
import struct
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import can
import numpy as np
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.analysis.rom_map import RomMap
from src.collection import private_traffic, tuning_profile, tx_gate
from src.collection.can_collector import CanReader
from src.collection.capture_profiles import CaptureProfile
from src.config import CanBusConfig
from src.control.rom_backup import (
    HOLDER,
    ROM_BACKUP_FILE,
    BackupRefusedError,
    RomBackupJob,
    VehicleReading,
    load_backup_recipe,
    recipe_extent,
)
from src.control.rom_library import RomLibrary
from src.dashboard.routes.rom_library import (
    capture_activity,
    register_rom_library_routes,
    vehicle_reading,
)
from src.paths import REPO_ROOT
from src.vehicle.platforms import vehicle_data_file
from src.vehicle.profiles import read_registry_file
from tests.configs import isolated_config
from tests.rom_fixtures import PROFILE, xterra_map

SMALL_SIZE = 2048
CAL_ID = b"1ZS44A00000"
#: A synthetic vector table: reset PC, stack, PC, stack (src/analysis/rom_extent.py).
VECTORS_AT = 0x40
STACK = 0xFFFF7FFC
VECTORS = (0x400, STACK, 0x400, STACK)
EXTENT = {
    "ram_from": 0xFFFF0000,
    "vector_tables": [{"offset": VECTORS_AT, "entries": 4, "evidence": "synthetic"}],
}
REQUEST_ID, RESPONSE_ID = 0x7E0, 0x7E8


# -- a small vehicle profile and image ---------------------------------------


def _rom_map_data() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "image_size": SMALL_SIZE,
        "calibration_ids": [CAL_ID.decode()],
        "calibration_id": {"offset": 0x100, "length": 11, "evidence": "synthetic"},
        "checksums": {
            "algorithm": "be32_sum_xor",
            "start": 0x100,
            "excluded": [0x200, 0x204],
            "sum_offset": 0x200,
            "xor_offset": 0x204,
            "markers": {0x10: 0x12345678},
        },
    }


def _write_profile(
    root: Path,
    *,
    retries: int = 2,
    reread: int = 8,
    timeout: float = 1.0,
    block: int = 63,
    ecm_broadcast_ids: list[int] | None = None,
    rom_map: bool = True,
    extent: dict[str, Any] | None = None,
) -> Path:
    profile = root / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    recipe = {
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "response_id": RESPONSE_ID,
        "session": {"enter_once": [0x85], "before_each_read": [0xFB], "exit": [0x81]},
        "read": {"start": 0, "end": SMALL_SIZE - 1, "block_size": block},
        "pacing": {
            "min_cycle_seconds": 0.0,
            "request_timeout_seconds": timeout,
            "retries_per_block": retries,
        },
        "verify": {"reread_blocks": reread},
        "provenance": {"capture": "synthetic", "evidence": "fake ECU"},
    }
    if ecm_broadcast_ids is not None:
        recipe["ecm_broadcast_ids"] = ecm_broadcast_ids
    if extent is not None:
        recipe["extent"] = extent
    if rom_map:
        (profile / "rom_map.yaml").write_text(yaml.safe_dump(_rom_map_data()))
    (profile / "rom_backup.yaml").write_text(yaml.safe_dump(recipe))
    return profile


def _small_image(
    *, cal: bytes = CAL_ID, good_checksum: bool = True, vectors: tuple[int, ...] = ()
) -> bytes:
    image = bytearray(np.random.default_rng(11).integers(0, 255, SMALL_SIZE, np.uint8).tobytes())
    struct.pack_into(">I", image, 0x10, 0x12345678)
    for index, value in enumerate(vectors):
        struct.pack_into(">I", image, VECTORS_AT + 4 * index, value)
    image[0x100 : 0x100 + 11] = cal
    rom_map = RomMap.model_validate(_rom_map_data())
    total, xor = rom_map.checksums.values(bytes(image))
    struct.pack_into(">II", image, 0x200, total if good_checksum else total ^ 1, xor)
    return bytes(image)


# -- the fake ECU ------------------------------------------------------------


class FakeEcu:
    """Answers 0x10 and 0x23 on ``request_id`` like the Xterra ECM, and sends
    its own ``broadcast`` frames every 5 ms while powered."""

    def __init__(self, channel: str, image: bytes, broadcast: tuple[int, ...] = ()) -> None:
        self.bus = can.Bus(interface="virtual", channel=channel)
        #: False is the key turned off: the ECM neither answers nor broadcasts.
        self.powered = True
        self._broadcast = broadcast
        self._broadcast_bus = can.Bus(interface="virtual", channel=channel) if broadcast else None
        self.image = bytearray(image)
        self.log: list[tuple[str, int]] = []
        self.read_counts: dict[int, int] = {}
        self.drop_reads: set[int] = set()
        self.fail_first: dict[int, int] = {}
        self.nrc_reads: dict[int, int] = {}
        self.corrupt_on_reread: set[int] = set()
        self.on_read: Callable[[int, int], None] | None = None
        self._pending: bytes | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="fake-ecu")
        self._broadcaster = threading.Thread(
            target=self._broadcast_run, daemon=True, name="fake-ecu-broadcast"
        )

    def start(self) -> FakeEcu:
        self._thread.start()
        if self._broadcast_bus is not None:
            self._broadcaster.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2)
        if self._broadcast_bus is not None:
            self._broadcaster.join(2)
            self._broadcast_bus.shutdown()
        self.bus.shutdown()

    def _broadcast_run(self) -> None:
        assert self._broadcast_bus is not None
        while not self._stop.wait(0.005):
            if not self.powered:
                continue
            for arbitration_id in self._broadcast:
                self._broadcast_bus.send(
                    can.Message(arbitration_id=arbitration_id, data=bytes(8), is_extended_id=False)
                )

    @property
    def sessions(self) -> list[int]:
        return [value for kind, value in self.log if kind == "session"]

    @property
    def reads(self) -> list[int]:
        return [value for kind, value in self.log if kind == "read"]

    def _send(self, data: bytes) -> None:
        self.bus.send(
            can.Message(arbitration_id=RESPONSE_ID, data=data.ljust(8, b"\0"), is_extended_id=False)
        )

    def _respond(self, payload: bytes) -> None:
        if len(payload) <= 7:
            self._send(bytes([len(payload)]) + payload)
            return
        self._send(bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6])
        self._pending = payload[6:]

    def _run(self) -> None:
        while not self._stop.is_set():
            message = self.bus.recv(0.01)
            if message is None or message.arbitration_id != REQUEST_ID:
                continue
            if not self.powered:
                continue
            data = bytes(message.data)
            kind = data[0] >> 4
            if kind == 3:  # flow control: send the consecutive frames
                rest, seq = self._pending or b"", 1
                self._pending = None
                while rest:
                    self._send(bytes([0x20 | (seq & 0xF)]) + rest[:7])
                    rest, seq = rest[7:], seq + 1
                continue
            if kind != 0:
                continue
            body = data[1 : 1 + (data[0] & 0xF)]
            if body[0] == 0x10:
                self.log.append(("session", body[1]))
                self._respond(bytes([0x50, body[1]]))
            elif body[0] == 0x23:
                address = int.from_bytes(body[1:5], "big")
                length = int.from_bytes(body[5:7], "big")
                count = self.read_counts[address] = self.read_counts.get(address, 0) + 1
                self.log.append(("read", address))
                if self.on_read is not None:
                    self.on_read(address, count)
                if (
                    not self.powered
                    or address in self.drop_reads
                    or self.fail_first.get(address, 0) >= count
                ):
                    continue
                if address in self.nrc_reads:
                    self._respond(bytes([0x7F, 0x23, self.nrc_reads[address]]))
                    continue
                chunk = bytes(self.image[address : address + length])
                if count > 1 and address in self.corrupt_on_reread:
                    chunk = bytes([chunk[0] ^ 0xFF]) + chunk[1:]
                self._respond(b"\x63" + chunk)
            else:
                self._respond(bytes([0x7F, body[0], 0x11]))


@dataclass
class Truck:
    """The vehicle-state view the job polls; tests flip it mid-read."""

    reading: VehicleReading = field(
        default_factory=lambda: VehicleReading(
            state="ignition_only", rpm=0.0, broadcast_rpm=0.0, obd_freshness_seconds=0.0
        )
    )
    capture: str | None = None
    ecu_id: str | None = CAL_ID.decode()


@dataclass
class Rig:
    job: RomBackupJob
    ecu: FakeEcu
    truck: Truck
    library: RomLibrary
    channel: str
    events: list[tuple[str, str, dict[str, Any]]]

    def run(self, timeout: float = 20.0) -> dict[str, Any]:
        self.job.start()
        assert self.job.wait(timeout), "backup did not finish"
        return self.job.state()


@pytest.fixture
def rig_factory(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []

    def make(
        image: bytes | None = None,
        *,
        open_bus: Callable[[], can.BusABC] | None = None,
        bus_wrapper: Callable[[can.BusABC], can.BusABC] | None = None,
        vehicle: Callable[[], VehicleReading] | None = None,
        registry: Callable[[], Any] | None = None,
        obd_source: str = "canable",
        resume_timeout: float = 0.5,
        broadcast: tuple[int, ...] = (),
        **profile_kwargs: Any,
    ) -> Rig:
        channel = f"rom-backup-{uuid.uuid4().hex}"
        profile = _write_profile(tmp_path / f"p{len(made)}", **profile_kwargs)
        image = image if image is not None else _small_image()
        ecu = FakeEcu(channel, image, broadcast).start()
        truck = Truck()
        library = RomLibrary(
            tmp_path / f"lib{len(made)}", profile_dir=profile, registry=registry or (lambda: None)
        )
        events: list[tuple[str, str, dict[str, Any]]] = []

        def virtual() -> can.BusABC:
            bus = can.Bus(interface="virtual", channel=channel)
            return bus_wrapper(bus) if bus_wrapper is not None else bus

        job = RomBackupJob(
            # No platform: the rig's recipe is the profile's own.
            recipe_file=lambda: vehicle_data_file(
                profile,
                ROM_BACKUP_FILE,
                make="",
                model="",
                year=None,
                platforms_dir=tmp_path / "no-platforms",
            ),
            library=library,
            open_bus=open_bus or virtual,
            vehicle=vehicle or (lambda: truck.reading),
            capture_active=lambda: truck.capture,
            ecu_calibration_id=lambda: truck.ecu_id,
            obd_source=obd_source,
            publish=lambda action, result, payload: events.append((action, result, payload)),
            resume_timeout_seconds=resume_timeout,
            pre_read_listen_seconds=0.05,
            recheck_seconds=0.0,  # every block: the small read lasts ~0.1 s
        )
        rig = Rig(job, ecu, truck, library, channel, events)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.job._cancel.set()
        rig.job.wait(10)
        rig.ecu.stop()


def _assert_released() -> None:
    snap = tx_gate.snapshot()
    assert snap["ecm_session_claims"] == {}
    assert snap["ecm_session_owner"] == "can_diagnostics"
    assert private_traffic.snapshot() == {}


# -- the recipe --------------------------------------------------------------


def _shipped_recipe(make: str, model: str, year: int, slug: str) -> Path:
    return vehicle_data_file(
        REPO_ROOT / "data" / slug, ROM_BACKUP_FILE, make=make, model=model, year=year
    ).path


def test_shipped_recipe_reads_the_whole_image_like_uprev() -> None:
    recipe = load_backup_recipe(
        _shipped_recipe("Nissan", "Xterra", 2008, PROFILE.name), xterra_map()
    )
    blocks = recipe.read.blocks()
    assert len(blocks) == 16645
    assert blocks[0] == (0, 63)
    assert blocks[-2] == (0x0FFFFC - 63, 63)
    assert blocks[-1] == (0x0FFFFC, 4)  # the 4-byte tail read
    assert (recipe.request_id, recipe.response_id) == (0x7E0, 0x7E8)
    assert recipe.session.enter_once == [0x85]
    assert recipe.session.before_each_read == [0xFB]
    assert recipe.session.exit == [0x81]
    assert recipe.verify.reread_blocks == 64
    assert recipe.ecm_broadcast_ids == [0x1F9, 0x231, 0x23D, 0x551]
    assert recipe.provenance.capture == "docs/skunkworks/uprev-backup-20261004.md"


def test_every_shipped_xterra_n50_profile_finds_the_shared_recipe() -> None:
    """The 2013 was refused a backup because only the 2008's profile directory
    held a recipe (2026-10-23). Every N50 Xterra in the tracked registry now
    resolves the one platform recipe, and a 2013 that has no map yet can
    still load it."""
    profiles = [
        p
        for p in read_registry_file(REPO_ROOT / "profiles.yaml")
        if p.make.casefold() == "nissan"
        and p.model.casefold() == "xterra"
        and p.year is not None
        and 2005 <= p.year <= 2015
    ]
    assert {p.slug for p in profiles} >= {"nissan-xterra-2008", "nissan-xterra-2013"}
    shared = REPO_ROOT / "data" / "platforms" / "nissan-xterra-n50" / ROM_BACKUP_FILE
    for p in profiles:
        assert p.year is not None
        assert _shipped_recipe(p.make, p.model, p.year, p.slug) == shared, p.slug
    recipe = load_backup_recipe(shared, None)
    assert recipe.read.size == 1 << 20


def test_a_recipe_that_misses_part_of_the_image_is_refused(tmp_path: Path) -> None:
    profile = _write_profile(tmp_path)
    data = yaml.safe_load((profile / "rom_backup.yaml").read_text())
    data["read"]["end"] = SMALL_SIZE - 2
    (profile / "rom_backup.yaml").write_text(yaml.safe_dump(data))
    rom_map = RomMap.model_validate(yaml.safe_load((profile / "rom_map.yaml").read_text()))
    with pytest.raises(ValueError, match="the image is 0x000000-0x0007FF"):
        load_backup_recipe(profile / ROM_BACKUP_FILE, rom_map)
    # Without a map the range is the image: any whole range from 0 loads.
    assert load_backup_recipe(profile / ROM_BACKUP_FILE, None).read.size == SMALL_SIZE - 1


def test_without_a_map_a_recipe_must_still_start_at_zero(tmp_path: Path) -> None:
    profile = _write_profile(tmp_path, rom_map=False)
    data = yaml.safe_load((profile / "rom_backup.yaml").read_text())
    data["read"]["start"] = 0x100
    (profile / "rom_backup.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="reads from 0x000100; an image starts at 0x000000"):
        load_backup_recipe(profile / ROM_BACKUP_FILE, None)


def test_a_diagnostic_id_cannot_stand_for_an_ecm_broadcast(tmp_path: Path) -> None:
    profile = _write_profile(tmp_path, ecm_broadcast_ids=[0x551, RESPONSE_ID])
    rom_map = RomMap.model_validate(yaml.safe_load((profile / "rom_map.yaml").read_text()))
    with pytest.raises(ValueError, match="0x7E8 is a diagnostic id"):
        load_backup_recipe(profile / ROM_BACKUP_FILE, rom_map)


# -- the transmit gate -------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[can.Message] = []

    def send(self, message: can.Message) -> None:
        self.sent.append(message)


def test_a_holder_transmits_only_while_its_claim_is_the_only_one() -> None:
    bus = _Recorder()
    msg = can.Message(arbitration_id=REQUEST_ID, data=b"\x02\x10\x85", is_extended_id=False)
    assert not tx_gate.send(bus, msg, what="t", holder=HOLDER)  # type: ignore[arg-type]
    tx_gate.claim_ecm_session(HOLDER)
    assert tx_gate.is_passive()  # the poll loop pauses
    assert not tx_gate.send(bus, msg, what="t")  # type: ignore[arg-type]
    assert tx_gate.send(bus, msg, what="t", holder=HOLDER)  # type: ignore[arg-type]
    tx_gate.claim_ecm_session("foreign")
    assert not tx_gate.send(bus, msg, what="t", holder=HOLDER)  # type: ignore[arg-type]
    tx_gate.release_ecm_session("foreign")
    tx_gate.set_passive(True, source="test", persist=False)
    assert not tx_gate.send(bus, msg, what="t", holder=HOLDER)  # type: ignore[arg-type]
    tx_gate.set_passive(False, source="test", persist=False)
    assert tx_gate.ecm_session_owner() == "rom_backup"
    assert len(bus.sent) == 1


# -- success -----------------------------------------------------------------


def test_backup_byte_matches_the_fake_ecu_and_releases_everything(
    rig_factory: Callable[..., Rig],
) -> None:
    image = _small_image()
    rig = rig_factory(image)
    during: list[tuple[str, bool, dict[str, list[str]]]] = []

    def observe(address: int, count: int) -> None:
        if address == 63 * 10 and count == 1:
            during.append(
                (tx_gate.ecm_session_owner(), tx_gate.is_passive(), private_traffic.snapshot())
            )

    rig.ecu.on_read = observe
    state = rig.run()
    assert state["state"] == "done", state
    assert state["verified"] is True and state["reason"] is None
    assert state["stored"] is True
    assert state["blocks_done"] == state["blocks_total"] == 33
    assert state["bytes"] == SMALL_SIZE
    assert state["polling_resumed_s"] is not None
    # The claim was held during the read (the OBD loop pauses on it) ...
    assert during == [("rom_backup", True, {HOLDER: ["0x7E0", "0x7E8"]})]
    # ... and released afterwards, with the ECM sent back to the default session.
    _assert_released()
    assert rig.ecu.sessions[0] == 0x85
    assert rig.ecu.sessions[-1] == 0x81
    assert rig.ecu.sessions.count(0xFB) == 33 + 8  # before every read and re-read
    # The library holds the fake ECU's exact bytes, as source ecu_read.
    assert rig.library.image(state["sha256"]) == image
    entry = rig.library.entry(state["sha256"])
    assert entry["source"] == "ecu_read"
    meta = entry["read_meta"]
    assert meta["verified"] is True and meta["unverified_reason"] is None
    assert meta["requests"] == 1 + 2 * 33 + 2 * 8
    assert meta["retries"] == 0
    assert meta["integrity"]["checksum"]["ok"] is True
    assert meta["integrity"]["calibration_id"] == {
        "image": CAL_ID.decode(),
        "ecu": CAL_ID.decode(),
        "ok": True,
        "offset": "0x000100",  # the map's
    }
    assert meta["integrity"]["reread"] == {"blocks": 8, "mismatches": []}
    assert meta["revision_match"]["status"] == "unrecognised"
    for key in ("started_at", "ended_at", "duration_s"):
        assert meta[key] is not None
    actions = [(a, r) for a, r, _ in rig.events]
    assert actions == [("rom_backup.start", "ok"), ("rom_backup.end", "done")]
    assert rig.events[1][2]["sha256"] == state["sha256"]


class _Collect:
    """A ``ThreadHandoff`` stand-in: keeps what the raw-capture tap pushes."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def push(self, record: Any) -> None:
        self.records.append(record)


def _reader(bus: can.BusABC, collect: _Collect, stop: threading.Event) -> CanReader:
    handoff: Any = collect
    return CanReader(bus, handoff, poll_interval=0.01, stop=stop, on_exit=lambda: None)


def _drain(bus: can.BusABC, reader: CanReader, stop: threading.Event) -> None:
    """Let ``reader`` empty ``bus``'s queue, then stop it."""
    for _ in range(500):
        if bus.queue.empty():  # type: ignore[attr-defined]
            break
        time.sleep(0.01)
    time.sleep(0.05)
    stop.set()
    reader.join(2)
    bus.shutdown()


def test_the_reads_traffic_is_not_written_as_telemetry(rig_factory: Callable[..., Rig]) -> None:
    """Two daemon-side taps hear every frame (SocketCAN delivers to every
    socket on the interface): one keeps up during the read; the other reads
    nothing until the hold has ended. Both must drop the exchange, because
    privacy is decided by each frame's own timestamp, not by when the tap
    processes it. The late tap is the deterministic form of the race that
    leaked the ``10 81`` / ``50 81`` tail in ~15 % of runs."""
    rig = rig_factory()
    live_bus = can.Bus(interface="virtual", channel=rig.channel)
    late_bus = can.Bus(interface="virtual", channel=rig.channel)
    live, late = _Collect(), _Collect()
    live_stop, late_stop = threading.Event(), threading.Event()
    live_reader = _reader(live_bus, live, live_stop)
    live_reader.start()
    broadcast = can.Bus(interface="virtual", channel=rig.channel)
    keep_sending = threading.Event()

    def chatter() -> None:
        while not keep_sending.is_set():
            broadcast.send(can.Message(arbitration_id=0x1F9, data=bytes(8), is_extended_id=False))
            time.sleep(0.005)

    sender = threading.Thread(target=chatter, daemon=True)
    sender.start()
    try:
        state = rig.run()
    finally:
        keep_sending.set()
        sender.join(2)
        broadcast.shutdown()
    assert state["state"] == "done", state
    assert private_traffic.snapshot() == {}  # the hold has ended ...
    late_reader = _reader(late_bus, late, late_stop)
    late_reader.start()  # ... before this tap reads its first frame
    _drain(live_bus, live_reader, live_stop)
    _drain(late_bus, late_reader, late_stop)
    for name, collect in (("live", live), ("late", late)):
        ids = [record.metadata["can_id"] for record in collect.records]
        assert REQUEST_ID not in ids and RESPONSE_ID not in ids, name
        assert 0x1F9 in ids, name  # broadcast capture kept running
    # Each tap dropped the whole exchange, frame for frame: 14 per 63-byte
    # block (the 10 FB pair; the 23 request, first frame, flow control and 9
    # consecutive frames), 9 for the 32-byte tail block (4 consecutive
    # frames), 32 + 1 blocks read and 7 + 1 re-read, plus 10 85 and 10 81.
    per_tap = (32 + 7) * 14 + 2 * 9 + 2 + 2
    assert private_traffic.dropped(HOLDER) == 2 * per_tap
    meta = rig.library.entry(state["sha256"])["read_meta"]
    assert meta["frames_excluded"] > 0  # counted at release; late frames come after


def test_privacy_is_decided_by_the_frames_own_timestamp() -> None:
    t0 = time.time()
    private_traffic.hold("t", {RESPONSE_ID}, now=t0)
    private_traffic.release("t", now=t0 + 1.0)

    def frame(stamp: float, arbitration_id: int = RESPONSE_ID) -> can.Message:
        return can.Message(arbitration_id=arbitration_id, timestamp=stamp, data=bytes(8))

    assert private_traffic.exclude(frame(t0 + 0.5))  # inside, processed after release
    assert not private_traffic.exclude(frame(t0 + 2.0))  # after the window
    assert not private_traffic.exclude(frame(t0 - 0.5))  # before it
    assert not private_traffic.exclude(frame(t0 + 0.5, arbitration_id=0x1F9))  # other id
    assert private_traffic.dropped("t") == 1
    assert private_traffic.is_private(RESPONSE_ID, t0 + 1.0)
    # A tap stalled 20 s (past the shared policy's 5 s stamp trust) still
    # judges the frame by its stamp, up to the retention period ...
    assert private_traffic.exclude(frame(t0 + 0.5), now=t0 + 20.0)
    # ... and beyond it falls back to processing time: the documented limit.
    late = t0 + 0.5 + private_traffic.RETAIN_SECONDS + 10.0
    assert not private_traffic.exclude(frame(t0 + 0.5), now=late)
    # A closed window is retained for late taps, then pruned.
    private_traffic.hold("x", {0x123}, now=t0 + private_traffic.RETAIN_SECONDS + 5)
    assert not private_traffic.is_private(RESPONSE_ID, t0 + 0.5)


def test_the_can_obd_collector_drops_held_ids_too() -> None:
    from can.interfaces.virtual import channels as virtual_channels

    from src.collection.can_obd_collector import collect_can_obd_from_config
    from src.collection.obd_scheduler import CanObdSpec

    channel = f"rom-backup-{uuid.uuid4().hex}"
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel=channel, bitrate=500000), pids=()
    )
    tx_gate.set_passive(True, source="test", persist=False)  # the loop issues nothing
    private_traffic.hold(HOLDER, {RESPONSE_ID})

    async def run() -> list[Any]:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        stop = asyncio.Event()
        task = asyncio.create_task(
            collect_can_obd_from_config(spec, queue, stop, reconnect_policy=None)
        )
        # Send only once the collector's own handle exists: a virtual bus
        # delivers to the handles open at send time.
        handles = virtual_channels.get(channel, [])
        for _ in range(500):
            if handles:
                break
            await asyncio.sleep(0.01)
            handles = virtual_channels.get(channel, [])
        sender = can.Bus(interface="virtual", channel=channel)
        for arbitration_id in (RESPONSE_ID, 0x1F9, RESPONSE_ID, 0x1F9):
            sender.send(can.Message(arbitration_id=arbitration_id, data=bytes(8)))
        for _ in range(500):
            if queue.qsize() >= 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 5)
        sender.shutdown()
        records = []
        while not queue.empty():
            records.append(queue.get_nowait())
        return records

    records = asyncio.run(run())
    ids = [r.metadata.get("can_id") for r in records if r.source == "can"]
    assert ids.count(0x1F9) == 2 and RESPONSE_ID not in ids
    assert private_traffic.release(HOLDER) == 2


# -- refusals ----------------------------------------------------------------


def _engine_running(truck: Truck) -> None:
    truck.reading = VehicleReading(state="engine_running", rpm=750.0, broadcast_rpm=750.0)


REFUSALS: dict[str, tuple[Callable[[Truck], None], str]] = {
    "engine running": (_engine_running, "broadcast engine speed 750 rpm >= 400"),
    "broadcast rpm while OBD reads 0": (
        lambda t: setattr(
            t,
            "reading",
            VehicleReading(state="ignition_only", rpm=0.0, broadcast_rpm=800.0),
        ),
        "broadcast engine speed 800 rpm >= 400 (vehicle_state ignition_only)",
    ),
    "engine off": (
        lambda t: setattr(t, "reading", VehicleReading(state="engine_off", broadcast_rpm=0.0)),
        "vehicle_state is engine_off",
    ),
    "no engine watch": (
        lambda t: setattr(
            t,
            "reading",
            VehicleReading(state="ignition_only", rpm=0.0, watch_unavailable="no live source"),
        ),
        "no fresh broadcast engine speed (no live source)",
    ),
    "passive": (
        lambda t: tx_gate.set_passive(True, source="scan-tool", persist=False),
        "passive mode is armed (source scan-tool)",
    ),
    "foreign claim": (
        lambda t: tx_gate.claim_ecm_session("foreign", ttl_seconds=60),
        "another tester holds the ECM session",
    ),
    "foreign heard": (
        lambda t: tx_gate.note_foreign_tester(tester_address=0xFC, module_address=0x10),
        "another tester (K-line FC -> 10) was heard",
    ),
    "kline session": (
        lambda t: tx_gate.claim_ecm_session("kline"),
        "the ECM session is held by kline",
    ),
    "capture": (
        lambda t: setattr(t, "capture", "capture profile 'tuning' is active"),
        "capture profile 'tuning' is active; the read pauses all polling",
    ),
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_each_precondition_refuses_the_start_by_name(
    rig_factory: Callable[..., Rig], name: str
) -> None:
    rig = rig_factory()
    arrange, expected = REFUSALS[name]
    arrange(rig.truck)
    with pytest.raises(BackupRefusedError, match=expected.replace("(", r"\(").replace(")", r"\)")):
        rig.job.start()
    state = rig.job.state()
    assert state["state"] == "refused" and expected in state["reason"]
    assert state["can_start"] is False and expected in state["blocked_by"]
    assert rig.ecu.log == []  # nothing was sent
    assert HOLDER not in tx_gate.snapshot()["ecm_session_claims"]
    assert rig.events == []


def test_elm327_source_is_refused(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(obd_source="elm327")
    with pytest.raises(BackupRefusedError, match="elm327"):
        rig.job.start()


def test_missing_recipe_is_refused(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    (rig.library.profile_dir / "rom_backup.yaml").unlink()
    with pytest.raises(BackupRefusedError, match="no backup recipe") as refused:
        rig.job.start()
    # It names where it looked, and what the platform match saw.
    assert "rom_backup.yaml does not exist, and no platform in" in str(refused.value)
    assert "model '(blank)'" in str(refused.value)


def test_another_backup_running_is_refused(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    release = threading.Event()
    rig.ecu.on_read = lambda address, count: release.wait(5) if address == 63 * 2 else None
    rig.job.start()
    try:
        with pytest.raises(BackupRefusedError, match="another backup is running"):
            rig.job.start()
        assert rig.job.state()["state"] == "running"
    finally:
        release.set()
        rig.job.wait(20)
    assert rig.job.state()["state"] == "done"


def test_preconditions_are_reported_when_idle(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    state = rig.job.state()
    assert state["state"] == "idle" and state["can_start"] is True
    ids = [c["id"] for c in state["preconditions"]]
    assert ids == [
        "backup",
        "recipe",
        "obd_source",
        "vehicle_state",
        "engine_watch",
        "passive",
        "foreign_tester",
        "ecm_session",
        "capture",
    ]
    assert all(c["ok"] for c in state["preconditions"])


# -- aborts, each releasing the claim ----------------------------------------


def test_an_engine_start_mid_read_aborts(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()

    def start_engine(address: int, count: int) -> None:
        if address == 63 * 5:
            _engine_running(rig.truck)

    rig.ecu.on_read = start_engine
    state = rig.run()
    assert state["state"] == "failed"
    assert "the engine started during the read" in state["reason"]
    assert state["sha256"] is None and rig.library.entries() == []
    assert rig.ecu.sessions[-1] == 0x81  # the session was still exited
    _assert_released()
    assert rig.events[-1][1] == "failed"


def test_lost_engine_watch_mid_read_aborts(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()

    def blind(address: int, count: int) -> None:
        if address == 63 * 4:
            rig.truck.reading = VehicleReading(state="passive", rpm=None, broadcast_rpm=None)

    rig.ecu.on_read = blind
    state = rig.run()
    assert state["state"] == "failed" and "lost the fresh broadcast engine speed" in state["reason"]
    _assert_released()


def test_a_timeout_retries_then_aborts_naming_the_address(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory(retries=2, timeout=0.1)
    rig.ecu.drop_reads.add(63 * 7)
    state = rig.run()
    assert state["state"] == "failed"
    assert "read at 0x0001B9 (63 bytes) failed after 3 attempts" in state["reason"]
    assert "no response within 0.1 s" in state["reason"]
    assert state["retries"] == 2 and state["timeouts"] == 3
    assert rig.ecu.read_counts[63 * 7] == 3
    _assert_released()


def test_a_key_off_mid_read_is_named_as_the_ecm_going_silent(
    rig_factory: Callable[..., Rig],
) -> None:
    """W08.9's first truck read ended at a key-off reported only as "no
    response within 0.5 s". The ECM's own broadcasts stop with it, and the
    job's bus hears that."""
    rig = rig_factory(
        retries=2, timeout=0.2, ecm_broadcast_ids=[0x551, 0x1F9], broadcast=(0x1F9, 0x551)
    )

    def key_off(address: int, count: int) -> None:
        if address == 63 * 5:
            rig.ecu.powered = False

    rig.ecu.on_read = key_off
    state = rig.run()
    assert state["state"] == "failed"
    assert state["reason"].startswith(
        "the ECM went silent (key off?): none of its broadcasts (0x1F9, 0x551) heard for "
    ), state["reason"]
    assert "read at 0x00013B (63 bytes) failed after 3 attempts" in state["reason"]
    assert "no response within 0.2 s" in state["reason"]
    _assert_released()


def test_a_timeout_while_the_ecm_still_broadcasts_says_it_is_powered(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory(retries=2, timeout=0.3, ecm_broadcast_ids=[0x1F9], broadcast=(0x1F9,))
    rig.ecu.drop_reads.add(63 * 7)
    state = rig.run()
    assert state["state"] == "failed"
    assert state["reason"].startswith("read at 0x0001B9 (63 bytes) failed after 3 attempts")
    assert "the ECM was still broadcasting" in state["reason"], state["reason"]
    assert "key off" not in state["reason"]
    _assert_released()


def test_declared_broadcasts_never_heard_leave_the_cause_open(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory(retries=1, timeout=0.1, ecm_broadcast_ids=[0x1F9])
    rig.ecu.drop_reads.add(63 * 3)
    state = rig.run()
    assert state["state"] == "failed"
    assert "the ECM's broadcasts (0x1F9) were never heard during the read" in state["reason"]
    _assert_released()


def test_a_negative_response_is_never_blamed_on_a_silent_ecm(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory(retries=1, ecm_broadcast_ids=[0x1F9])
    rig.ecu.nrc_reads[63 * 2] = 0x22
    state = rig.run()
    assert state["state"] == "failed"
    assert "negative response 0x22" in state["reason"]
    assert "broadcast" not in state["reason"]
    _assert_released()


def test_a_single_timeout_retries_and_the_read_completes(
    rig_factory: Callable[..., Rig],
) -> None:
    image = _small_image()
    rig = rig_factory(image, timeout=0.2)
    rig.ecu.fail_first[63 * 3] = 1
    state = rig.run()
    assert state["state"] == "done", state
    assert state["retries"] >= 1 and state["timeouts"] >= 1
    assert rig.ecu.read_counts[63 * 3] == 2  # dropped once, then answered
    assert rig.library.image(state["sha256"]) == image
    _assert_released()


def test_a_negative_response_is_reported_with_its_code(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(retries=1)
    rig.ecu.nrc_reads[63 * 2] = 0x22
    state = rig.run()
    assert state["state"] == "failed"
    assert "read at 0x00007E" in state["reason"]
    assert "negative response 0x22" in state["reason"]
    assert state["negative_responses"] == {"0x22": 2}
    _assert_released()


def test_cancel_stops_the_read_and_releases(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.ecu.on_read = lambda address, count: rig.job.cancel() if address == 63 * 6 else None
    state = rig.run()
    assert state["state"] == "cancelled" and state["reason"] == "cancelled by the operator"
    assert rig.library.entries() == []
    assert rig.ecu.sessions[-1] == 0x81
    _assert_released()
    with pytest.raises(BackupRefusedError, match="no backup is running"):
        rig.job.cancel()


@pytest.mark.parametrize(
    ("arrange", "expected"),
    [
        (lambda: tx_gate.set_passive(True, source="op", persist=False), "passive mode was armed"),
        (lambda: tx_gate.claim_ecm_session("foreign", ttl_seconds=60), "taken by foreign"),
        (
            lambda: tx_gate.note_foreign_tester(tester_address=0xF1, module_address=0x10),
            "heard on K-line (F1)",
        ),
    ],
    ids=["passive", "foreign-claim", "foreign-heard"],
)
def test_a_second_tester_or_passive_mid_read_aborts(
    rig_factory: Callable[..., Rig], arrange: Callable[[], None], expected: str
) -> None:
    rig = rig_factory()
    rig.ecu.on_read = lambda address, count: arrange() if address == 63 * 3 else None
    state = rig.run()
    assert state["state"] == "failed" and expected in state["reason"], state
    assert HOLDER not in tx_gate.snapshot()["ecm_session_claims"]
    assert private_traffic.snapshot() == {}


def test_another_tester_on_can_mid_read_aborts(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    intruder = can.Bus(interface="virtual", channel=rig.channel)

    def intrude(address: int, count: int) -> None:
        if address == 63 * 4:
            intruder.send(
                can.Message(arbitration_id=0x7DF, data=b"\x02\x01\x0c", is_extended_id=False)
            )

    rig.ecu.on_read = intrude
    try:
        state = rig.run()
    finally:
        intruder.shutdown()
    assert state["state"] == "failed"
    assert "another tester transmitted on CAN (id 0x7DF)" in state["reason"]
    _assert_released()


def test_a_capture_started_mid_read_aborts(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()

    def capture(address: int, count: int) -> None:
        if address == 63 * 4:
            rig.truck.capture = "capture window 'fuel' is active"

    rig.ecu.on_read = capture
    state = rig.run()
    assert state["state"] == "failed" and "capture window 'fuel' is active" in state["reason"]
    _assert_released()


def test_bus_open_failure_releases_the_claim(rig_factory: Callable[..., Rig]) -> None:
    def broken() -> can.BusABC:
        raise OSError("can0: no such device")

    rig = rig_factory(open_bus=broken)
    state = rig.run()
    assert state["state"] == "failed"
    assert "could not open the CAN bus: can0: no such device" in state["reason"]
    _assert_released()


def test_an_unexpected_error_still_releases_the_claim(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    calls = {"n": 0}
    healthy = rig.truck.reading

    def flaky() -> VehicleReading:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("state source broke")
        return healthy

    rig.job._vehicle = flaky
    state = rig.run()
    assert state["state"] == "failed"
    assert "unexpected error: RuntimeError: state source broke" in state["reason"]
    _assert_released()


# -- integrity ---------------------------------------------------------------


def test_a_checksum_failure_is_kept_and_marked_unverified(
    rig_factory: Callable[..., Rig],
) -> None:
    image = _small_image(good_checksum=False)
    rig = rig_factory(image)
    state = rig.run()
    assert state["state"] == "failed"
    assert state["reason"].startswith("unverified: checksum mismatch")
    assert state["verified"] is False and state["sha256"] is not None
    entry = rig.library.entry(state["sha256"])
    assert rig.library.image(state["sha256"]) == image  # kept, never hidden
    assert entry["read_meta"]["verified"] is False
    assert "checksum mismatch" in entry["read_meta"]["unverified_reason"]
    assert entry["checksum"]["ok"] is False
    _assert_released()


def test_a_calibration_id_mismatch_is_unverified(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.truck.ecu_id = "1ZS04C00000"
    state = rig.run()
    assert state["state"] == "failed"
    assert "image calibration ID 1ZS44A00000 != ECM-reported 1ZS04C00000" in state["reason"]


def test_a_calibration_the_map_does_not_know_is_still_checked_at_its_offset(
    rig_factory: Callable[..., Rig],
) -> None:
    """A different ID at the map's offset is what makes the map not apply, so
    it cannot excuse the comparison: disagreeing with the ECM still fails.
    Agreeing leaves only the checksums unchecked."""
    other = b"1ZS04C00000"
    rig = rig_factory(_small_image(cal=other))
    state = rig.run()
    assert state["state"] == "failed"
    assert "image calibration ID 1ZS04C00000 != ECM-reported 1ZS44A00000" in state["reason"]
    assert state["integrity"]["calibration_id"]["offset"] == "0x000100"
    rig.truck.ecu_id = other.decode()
    state = rig.run()
    assert state["state"] == "done" and state["verified"] is False
    assert state["reason"] == (
        "unverified: no map definition for calibration 1ZS04C00000: checksums not checked"
    )


def test_an_unreported_ecm_calibration_id_is_done_but_unverified(
    rig_factory: Callable[..., Rig],
) -> None:
    """Nothing to check against is not evidence against the read."""
    rig = rig_factory()
    rig.truck.ecu_id = None
    state = rig.run()
    assert state["state"] == "done" and state["verified"] is False
    assert state["reason"].startswith(
        "unverified: the ECM has not reported its calibration ID (/status calibration), so "
        "the image's 1ZS44A00000 could not be checked against it"
    )
    entry = rig.library.entry(state["sha256"])
    assert entry["read_meta"]["verified"] is False
    assert entry["checksum"]["ok"] is True


# -- no ROM map yet ------------------------------------------------------------


def test_a_vehicle_without_a_rom_map_backs_up_and_keeps_the_image(
    rig_factory: Callable[..., Rig],
) -> None:
    """A backup is how a vehicle gets the image to map, so it cannot need a
    map. The read runs, the image is stored and listed, its calibration ID is
    found where the ECM's own ASCII sits, and only the checksums go
    unchecked."""
    image = _small_image()
    rig = rig_factory(image, rom_map=False)
    checks = {c["id"]: c for c in rig.job.state()["preconditions"]}
    assert checks["recipe"]["ok"] is True
    assert checks["recipe"]["detail"].endswith("no ROM map, so checksums go unchecked")
    state = rig.run()
    assert state["state"] == "done", state
    assert state["verified"] is False and state["stored"] is True
    assert state["reason"] == (
        "unverified: no ROM map for this vehicle (rom_map.yaml): checksums not checked; with "
        "no map, nothing confirms the recipe's 2,048 bytes are the whole flash"
    )
    assert state["integrity"]["checksum"] is None
    assert state["integrity"]["calibration_id"] == {
        "image": CAL_ID.decode(),
        "ecu": CAL_ID.decode(),
        "ok": True,
        "offset": "0x000100",
    }
    assert state["bytes_total"] == SMALL_SIZE
    assert rig.library.image(state["sha256"]) == image
    (entry,) = rig.library.entries()
    assert entry["sha256"] == state["sha256"] and entry["source"] == "ecu_read"
    assert entry["checksum"] is None
    assert entry["map_reason"] == "no ROM map for this vehicle (rom_map.yaml)"
    described = rig.library.describe(state["sha256"])
    assert described["map"] is None and described["tables"] == [] and described["blocks"]
    assert described["map_reason"] == entry["map_reason"]
    start = rig.events[0]
    assert start[0] == "rom_backup.start" and start[2]["rom_map"] is False
    _assert_released()


def test_without_a_map_an_ecm_id_missing_from_the_image_is_unverified(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory(rom_map=False)
    rig.truck.ecu_id = "1ZS04C00000"
    state = rig.run()
    assert state["state"] == "done" and state["verified"] is False
    assert (
        "the ECM reports calibration ID 1ZS04C00000, and the image does not hold it as ASCII"
        in state["reason"]
    )
    assert state["integrity"]["calibration_id"]["ok"] is False
    # Whitespace around the reported ID is not part of it.
    rig.truck.ecu_id = f"  {CAL_ID.decode()} "
    state = rig.run()
    assert state["integrity"]["calibration_id"]["ok"] is True
    assert "does not hold it as ASCII" not in state["reason"]


def test_without_a_map_a_reread_mismatch_still_fails(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(rom_map=False, reread=8)
    rig.ecu.corrupt_on_reread.add(0)
    state = rig.run()
    assert state["state"] == "failed" and state["stored"] is True
    assert state["reason"].startswith("unverified: 1 re-read block(s) differ: 0x000000")


# -- the image's own size ---------------------------------------------------------


def test_without_a_map_the_images_own_pointers_check_its_size(
    rig_factory: Callable[..., Rig],
) -> None:
    """Still unchecked, and the reason says what did check it: a sign, not proof."""
    rig = rig_factory(_small_image(vectors=VECTORS), rom_map=False, extent=EXTENT)
    state = rig.run()
    assert state["state"] == "done" and state["verified"] is False, state
    assert state["reason"].endswith(
        "with no map, only the image's own pointers check its size: nothing points past "
        "0x0007FF: 4 vector-table entries (tables at 0x000040) land inside the image, in RAM "
        "or erased; a sign, not proof, that the recipe's 2,048 bytes are the whole flash"
    )
    assert state["integrity"]["extent"]["verdict"] == "no_sign"
    assert "nothing confirms" not in state["reason"]


def test_a_vector_table_that_is_not_one_keeps_the_plain_caveat(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory(_small_image(vectors=(0x401, STACK)), rom_map=False, extent=EXTENT)
    state = rig.run()
    assert state["state"] == "done" and state["verified"] is False
    assert (
        "with no map, nothing confirms the recipe's 2,048 bytes are the whole flash (no vector "
        "table could be read (0x000040: entry 0 is 0x00000401"
    ) in state["reason"]
    assert state["integrity"]["extent"]["verdict"] == "unknown"


def test_with_a_map_a_sound_extent_leaves_the_read_verified(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory(_small_image(vectors=VECTORS), extent=EXTENT)
    state = rig.run()
    assert state["state"] == "done" and state["verified"] is True, state
    assert state["integrity"]["extent"]["verdict"] == "no_sign"


def test_a_table_pointing_past_the_range_stops_the_read_early(
    rig_factory: Callable[..., Rig],
) -> None:
    """The tables are in after two blocks; the rest is never asked for, and
    nothing past the recipe's range ever is."""
    rig = rig_factory(
        _small_image(vectors=(0x400, STACK, 0x1000, STACK)), rom_map=False, extent=EXTENT
    )
    state = rig.run()
    assert state["state"] == "failed" and state["stored"] is False, state
    assert state["reason"] == (
        "stopped at block 2 of 33: the image points past its end (0x0007FF): the vector table "
        "at 0x000040 to 0x00001000, so the flash is larger than the 2,048 bytes read. The "
        "recipe reads 0x000000-0x0007FF; this ECM needs a rom_backup.yaml in its own profile "
        "with the flash's real size. Nothing was stored"
    )
    assert rig.ecu.reads == [0, 63]
    assert rig.ecu.sessions[-1] == 0x81  # the exit sequence still went out
    assert rig.library.entries() == []
    _assert_released()


def test_a_finished_image_pointing_past_its_end_fails(rig_factory: Callable[..., Rig]) -> None:
    """The post-read verdict, should the early one ever not have run."""
    rig = rig_factory(rom_map=False, extent=EXTENT)
    recipe = load_backup_recipe(rig.library.profile_dir / ROM_BACKUP_FILE, None)
    image = _small_image(vectors=(0x400, STACK, 0x1000, STACK))
    integrity, failures, unchecked = rig.job._integrity(image, recipe, None, [])
    assert failures == [
        "the image points past its end (0x0007FF): the vector table at 0x000040 to "
        "0x00001000, so the flash is larger than the 2,048 bytes read"
    ]
    assert not any("whole flash" in item for item in unchecked)
    assert integrity["extent"]["tables"][0]["beyond"] == ["0x00001000"]


def test_without_an_extent_the_integrity_payload_says_so(rig_factory: Callable[..., Rig]) -> None:
    state = rig_factory(rom_map=False).run()
    assert state["integrity"]["extent"] is None
    assert "nothing confirms the recipe's 2,048 bytes are the whole flash" in state["reason"]


@pytest.mark.parametrize(
    ("extent", "match"),
    [
        (
            {**EXTENT, "vector_tables": [{"offset": 0x7F8, "entries": 4, "evidence": "x"}]},
            "a vector table runs to 0x000808, past the 2,048 bytes the recipe reads",
        ),
        ({**EXTENT, "ram_from": 0x400}, "ram_from 0x00000400 is inside the read range"),
    ],
)
def test_an_extent_the_read_cannot_satisfy_is_refused(
    tmp_path: Path, extent: dict[str, Any], match: str
) -> None:
    profile = _write_profile(tmp_path, rom_map=False, extent=extent)
    with pytest.raises(ValueError, match=match):
        load_backup_recipe(profile / ROM_BACKUP_FILE, None)


def test_a_missing_or_broken_recipe_gives_the_library_no_extent(tmp_path: Path) -> None:
    profile = _write_profile(tmp_path, rom_map=False, extent=EXTENT)

    def recipe() -> Any:
        return vehicle_data_file(
            profile, ROM_BACKUP_FILE, make="", model="", year=None, platforms_dir=tmp_path / "np"
        )

    assert recipe_extent(recipe) is not None
    (profile / ROM_BACKUP_FILE).write_text("read: [unclosed\n")
    assert recipe_extent(recipe) is None
    (profile / ROM_BACKUP_FILE).unlink()
    assert recipe_extent(recipe) is None


def test_a_platform_recipe_serves_a_profile_without_one(
    rig_factory: Callable[..., Rig], tmp_path: Path
) -> None:
    """The profile has no recipe of its own; its platform's is used and named."""
    rig = rig_factory(rom_map=False)
    own = rig.library.profile_dir / ROM_BACKUP_FILE
    platform = tmp_path / "platforms" / "test-gen"
    platform.mkdir(parents=True)
    (platform / ROM_BACKUP_FILE).write_text(own.read_text())
    own.unlink()
    (platform / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "Test generation",
                "vehicles": [{"make": "Nissan", "model": "Xterra", "years": [2005, 2015]}],
                "evidence": "test",
            }
        )
    )
    rig.job._recipe_file = lambda: vehicle_data_file(
        rig.library.profile_dir,
        ROM_BACKUP_FILE,
        make="Nissan",
        model="Xterra",
        year=2013,
        platforms_dir=tmp_path / "platforms",
    )
    detail = {c["id"]: c for c in rig.job.state()["preconditions"]}["recipe"]["detail"]
    assert "platforms/test-gen/rom_backup.yaml (platform Test generation)" in detail
    state = rig.run()
    assert state["state"] == "done" and state["stored"] is True
    meta = rig.library.entry(state["sha256"])["read_meta"]
    assert meta["recipe"].endswith("platforms/test-gen/rom_backup.yaml (platform Test generation)")


def test_a_reread_mismatch_is_unverified(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(reread=8)
    rig.ecu.corrupt_on_reread.add(0)  # the first re-read block
    state = rig.run()
    assert state["state"] == "failed"
    assert "1 re-read block(s) differ: 0x000000" in state["reason"]
    assert rig.library.entry(state["sha256"])["read_meta"]["integrity"]["reread"] == {
        "blocks": 8,
        "mismatches": ["0x000000"],
    }


def test_polling_that_does_not_resume_is_reported(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(resume_timeout=0.3)
    rig.truck.reading = VehicleReading(
        state="ignition_only", rpm=0.0, broadcast_rpm=0.0, obd_freshness_seconds=120.0
    )
    state = rig.run()
    assert state["state"] == "done"
    assert state["polling_resumed_s"] is None
    assert "no OBD reading within 0 s" in state["polling_resume_note"]


# -- routes --------------------------------------------------------------------


def test_backup_routes(tmp_path: Path) -> None:
    config = isolated_config(tmp_path, daemon={"afr_calibration": PROFILE / "afr_calibration.yaml"})
    app = FastAPI()
    register_rom_library_routes(app, config=config, resolved_control_token="secret")
    auth = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        idle = client.get("/control/rom/backup", headers=auth).json()
        assert idle["state"] == "idle" and idle["blocks_done"] == 0
        checks = {c["id"]: c for c in idle["preconditions"]}
        assert checks["recipe"]["ok"] is True  # the shipped Xterra recipe
        assert checks["recipe"]["detail"].startswith(
            "data/platforms/nissan-xterra-n50/rom_backup.yaml (platform Nissan Xterra N50"
        )
        # No live source in this app: the engine watch is unavailable.
        assert checks["engine_watch"]["ok"] is False
        refused = client.post("/control/rom/backup", headers=auth)
        assert refused.status_code == 409
        assert "no fresh broadcast engine speed" in refused.json()["detail"]
        assert client.get("/control/rom/backup", headers=auth).json()["state"] == "refused"
        cancel = client.post("/control/rom/backup/cancel", headers=auth)
        assert cancel.status_code == 409 and cancel.json()["detail"] == "no backup is running"


def test_a_2013_xterra_with_no_profile_data_is_offered_a_backup(tmp_path: Path) -> None:
    """The reported refusal (2026-10-23): ``data/nissan-xterra-2013/`` holds
    neither a recipe nor a ROM map. Only the vehicle is left blocking."""
    profile = tmp_path / "data" / "nissan-xterra-2013"
    config = isolated_config(
        tmp_path,
        daemon={"afr_calibration": profile / "afr_calibration.yaml"},
        vehicle={"make": "Nissan", "model": "Xterra", "year": 2013},
    )
    app = FastAPI()
    register_rom_library_routes(app, config=config, resolved_control_token="secret")
    auth = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        checks = {
            c["id"]: c
            for c in client.get("/control/rom/backup", headers=auth).json()["preconditions"]
        }
        assert checks["recipe"] == {
            "id": "recipe",
            "ok": True,
            "detail": (
                "data/platforms/nissan-xterra-n50/rom_backup.yaml (platform Nissan Xterra N50, "
                "2005-2015): 16,645 reads; no ROM map, so checksums go unchecked"
            ),
        }
        assert client.get("/control/rom/library", headers=auth).json() == {
            "entries": [],
            "count": 0,
        }


def test_capture_activity_names_profile_captures() -> None:
    assert capture_activity(None) is None
    tuning_profile.activate(
        CaptureProfile(
            name="tuning",
            description="test",
            mode01={"RPM": 1.0},
            corpus_eligible=False,
            storage_prefix="_capture-tuning",
        ),
        session_dir="/nowhere",
        source="test",
    )
    assert capture_activity(None) == "capture profile 'tuning' is active"


# -- the engine watch through the real vehicle-state adapter -------------------


class FakeLiveSource:
    """``LiveFeatureSource.vehicle_state_inputs`` as the daemon's live source
    reports it: a fresh OBD rpm and a broadcast rpm, side by side."""

    def __init__(self) -> None:
        self.inputs: dict[str, float | None] = {
            "rpm": 0.0,
            "broadcast_rpm": 0.0,
            "coolant_temp": 60.0,
            "run_time": 0.0,
            "obd_freshness_seconds": 1.0,
            "can_freshness_seconds": 0.05,
        }

    def vehicle_state_inputs(self, now: Any) -> dict[str, float | None]:
        return dict(self.inputs)


def test_vehicle_reading_from_a_live_source(tmp_path: Path) -> None:
    config = isolated_config(tmp_path)
    live = FakeLiveSource()
    reading = vehicle_reading(config, live_source=live, replay_controller=None)
    assert reading.state == "ignition_only"
    assert reading.broadcast_rpm == 0.0 and reading.obd_freshness_seconds == 1.0
    assert reading.watch_unavailable is None
    live.inputs["can_freshness_seconds"] = 30.0  # a stale broadcast is no watch
    stale = vehicle_reading(config, live_source=live, replay_controller=None)
    assert stale.broadcast_rpm is None
    assert stale.watch_unavailable == "broadcast engine speed absent or stale"
    offline = vehicle_reading(config, live_source=None, replay_controller=None)
    assert offline.broadcast_rpm is None
    assert offline.watch_unavailable is not None and "no live telemetry source" in (
        offline.watch_unavailable
    )


def test_a_running_engine_refuses_while_obd_still_reads_a_fresh_zero(
    rig_factory: Callable[..., Rig], tmp_path: Path
) -> None:
    """The classifier prefers the fresh OBD rpm 0 for up to 10 s; the
    broadcast already reads 800. The start is refused on the broadcast."""
    config = isolated_config(tmp_path)
    live = FakeLiveSource()
    live.inputs["broadcast_rpm"] = 800.0
    rig = rig_factory(
        vehicle=lambda: vehicle_reading(config, live_source=live, replay_controller=None)
    )
    assert vehicle_reading(config, live_source=live, replay_controller=None).state == (
        "ignition_only"
    )
    with pytest.raises(BackupRefusedError, match="broadcast engine speed 800 rpm >= 400"):
        rig.job.start()
    assert rig.ecu.log == []


def test_an_engine_start_inside_the_obd_freshness_window_aborts(
    rig_factory: Callable[..., Rig], tmp_path: Path
) -> None:
    config = isolated_config(tmp_path)
    live = FakeLiveSource()
    rig = rig_factory(
        vehicle=lambda: vehicle_reading(config, live_source=live, replay_controller=None)
    )

    def crank(address: int, count: int) -> None:
        if address == 63 * 3:
            live.inputs["broadcast_rpm"] = 820.0  # OBD still a fresh 0

    rig.ecu.on_read = crank
    state = rig.run()
    assert state["state"] == "failed"
    assert "the engine started during the read (broadcast engine speed 820 rpm" in state["reason"]
    assert rig.ecu.sessions[-1] == 0x81
    _assert_released()


# -- the end of the job never strands it --------------------------------------


def test_a_store_failure_keeps_nothing_and_says_so(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()

    def full(*args: Any, **kwargs: Any) -> Any:
        raise OSError(28, "No space left on device")

    rig.library.add_image = full  # type: ignore[method-assign]
    state = rig.run()
    assert state["state"] == "failed"
    assert "could not be stored" in state["reason"] and "nothing was kept" in state["reason"]
    assert state["stored"] is False and state["sha256"] is None
    end = rig.events[-1]
    assert end[0] == "rom_backup.end" and end[2]["stored"] is False
    _assert_released()


def test_a_broken_flash_history_does_not_lose_the_image(rig_factory: Callable[..., Rig]) -> None:
    def broken() -> Any:
        raise yaml.YAMLError("mapping values are not allowed here")

    image = _small_image()
    rig = rig_factory(image, registry=broken)
    state = rig.run()
    assert state["state"] == "done", state
    assert state["stored"] is True and rig.library.image(state["sha256"]) == image
    match = state["revision_match"]
    assert match["status"] == "unknown" and "flash history unreadable" in match["reason"]
    assert rig.library.entry(state["sha256"])["revision_match"]["status"] == "unknown"


def test_an_integrity_check_error_keeps_the_image_unverified(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory()

    def boom() -> str:
        raise RuntimeError("calibration snapshot unavailable")

    rig.job._ecu_calibration_id = boom
    state = rig.run()
    assert state["state"] == "failed" and state["stored"] is True
    assert state["reason"] == (
        "unverified: the integrity check failed: RuntimeError: calibration snapshot unavailable"
    )


def test_any_error_after_the_read_ends_the_job_as_failed(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("unforeseen")

    rig.job._store = explode  # type: ignore[method-assign]
    state = rig.run()
    assert state["state"] == "failed"
    assert state["reason"] == "the read finished but storing it failed: RuntimeError: unforeseen"
    assert [a for a, _, _ in rig.events] == ["rom_backup.start", "rom_backup.end"]
    _assert_released()


# -- shutdown mid-read ---------------------------------------------------------


def test_daemon_shutdown_cancels_the_read_cleanly(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(resume_timeout=30.0)
    rig.ecu.on_read = lambda address, count: time.sleep(0.01)
    rig.job.start()
    for _ in range(500):
        if rig.job.state()["blocks_done"] >= 3:
            break
        time.sleep(0.01)
    assert rig.job.shutdown(timeout=5.0) is True
    state = rig.job.state()
    assert state["state"] == "cancelled"
    assert state["reason"] == "cancelled: the daemon is shutting down"
    assert state["polling_resume_note"] == "not checked: the daemon is shutting down"
    assert rig.ecu.sessions[-1] == 0x81
    assert rig.events[-1][0] == "rom_backup.end" and rig.events[-1][1] == "cancelled"
    _assert_released()
    with pytest.raises(BackupRefusedError, match="the daemon is shutting down"):
        rig.job.start()


def test_the_app_lifespan_shuts_the_job_down(tmp_path: Path) -> None:
    config = isolated_config(tmp_path, daemon={"afr_calibration": PROFILE / "afr_calibration.yaml"})
    app = FastAPI()
    job = register_rom_library_routes(app, config=config, resolved_control_token="secret")
    calls: list[str] = []
    job.shutdown = lambda timeout=5.0: calls.append("shutdown") or True  # type: ignore[method-assign]
    with TestClient(app):
        assert calls == []
    assert calls == ["shutdown"]


# -- a CAN send error is not a gate suppression --------------------------------


def _send_fails(times: int) -> Callable[[can.BusABC], can.BusABC]:
    remaining = {"n": times}

    def wrap(bus: can.BusABC) -> can.BusABC:
        real = bus.send

        def send(message: can.Message, timeout: float | None = None) -> None:
            if message.data[1] == 0x23 and remaining["n"] != 0:
                remaining["n"] -= 1
                raise can.CanOperationError("Transmit buffer full")
            real(message, timeout)

        bus.send = send  # type: ignore[method-assign]
        return bus

    return wrap


def test_a_transient_send_error_is_retried(rig_factory: Callable[..., Rig]) -> None:
    image = _small_image()
    rig = rig_factory(image, bus_wrapper=_send_fails(1))
    state = rig.run()
    assert state["state"] == "done", state
    assert state["send_errors"] == 1 and state["retries"] == 1
    assert rig.library.image(state["sha256"]) == image


def test_a_persistent_send_error_is_named_as_one(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory(bus_wrapper=_send_fails(-1))
    state = rig.run()
    assert state["state"] == "failed"
    assert "read at 0x000000 (63 bytes) failed after 3 attempts" in state["reason"]
    assert "CAN send failed (CanOperationError: Transmit buffer full)" in state["reason"]
    assert "suppressed" not in state["reason"]
    _assert_released()


def test_shutdown_during_the_resume_wait_still_records_the_end(
    rig_factory: Callable[..., Rig],
) -> None:
    """The read is done and stored; the job waits (up to 30 s here, 60 s in
    the daemon) for OBD polling to come back. A daemon stop then ends the wait
    at once instead of sleeping it out, and the end event is still recorded."""
    rig = rig_factory(resume_timeout=30.0)
    rig.truck.reading = VehicleReading(
        state="ignition_only", rpm=0.0, broadcast_rpm=0.0, obd_freshness_seconds=120.0
    )
    rig.job.start()
    for _ in range(1000):
        if rig.job.state()["phase"] == "waiting for polling to resume":
            break
        time.sleep(0.01)
    assert rig.job.state()["phase"] == "waiting for polling to resume"
    began = time.monotonic()
    assert rig.job.shutdown(timeout=5.0) is True
    assert time.monotonic() - began < 2.0
    state = rig.job.state()
    assert state["state"] == "done" and state["stored"] is True
    assert state["polling_resume_note"] == "not checked: the daemon is shutting down"
    assert [(a, r) for a, r, _ in rig.events] == [
        ("rom_backup.start", "ok"),
        ("rom_backup.end", "done"),
    ]
    _assert_released()
