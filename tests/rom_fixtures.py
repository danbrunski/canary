"""Synthetic 1 MiB flash images with the 2008 Xterra's real layout (W08).

No proprietary ROM bytes: the filler is seeded noise, and the only "real"
values placed are ones already in Git as data — the transcribed Fuel Targets
cells and raw axes in ``afr_calibration.yaml``, the calibration ID, K counts
and axis steps quoted in ``docs/uprev-rom-table-verification.md``. The
checksums are recomputed with the vehicle's definition, so the images pass it.

The operator's real files live outside the repo; :data:`OPERATOR_ROMS` names
them and the tests that use them skip when they are absent.
"""

from __future__ import annotations

import functools
import struct
from pathlib import Path

import numpy as np

from src.analysis.osiris import SIGNATURE, Container, Section, transform
from src.analysis.rom_map import RomMap, load_rom_map
from src.signals.afr import load_afr_calibration

REPO = Path(__file__).resolve().parents[1]
PROFILE = REPO / "data" / "nissan-xterra-2008"
IMAGE_SIZE = 1 << 20

UPREV = Path("/mnt/c/Users/thebo/Desktop/Uprev")
#: The operator's files (outside Git). Tests skip when absent.
OPERATOR_ROMS = {
    "base": UPREV / "ROM Store" / "ZS44A.OsirisROM",
    "k106": UPREV / "Tuned ROMs" / "ZS44A-Tuned.OsirisROM",
    "r20260920": UPREV / "Tuned ROMs" / "ZS44A-Tuned-20260920.OsirisROM",
    # Fuel Comp load axis restored to 16..255 and slot-1 cells to base (N02.1).
    "r20261004": UPREV / "Tuned ROMs" / "ZS44A-Tuned-20261004.OsirisROM",
    # k106 with one Fuel Compensation cell edited in UpRev, 103.13 -> 102.34 %
    # (docs/uprev-rom-container-research.md): a known answer for that table.
    "map1change": Path("/home/dan/Uprev/ROMs/ZS44A-Tuned-Map1Change.OsirisROM"),
}
OPERATOR_BACKUP_DIR = UPREV / "ROM Backup"
#: sha256 of the 2026-10-04 ECU read (docs/skunkworks/uprev-backup-20261004.md),
#: equal to the decoded 20260920 file.
ECU_READ_20261004_SHA256 = "a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50"

#: An arbitrary synthetic Osiris key, never an extracted production value.
SYNTHETIC_KEY = 0x12345678


def xterra_map() -> RomMap:
    return load_rom_map(PROFILE)


def target_raw(revision: str) -> list[list[int]]:
    calibration = load_afr_calibration(PROFILE / "afr_calibration.yaml")
    (target,) = [t for t in calibration.target_maps if t.revision == revision]
    assert target.raw_values is not None
    return [list(row) for row in target.raw_values]


def synthetic_image(
    *,
    seed: int = 7,
    k: int = 28715,
    slot1_targets: str = "ZS44A-20260920",
    other_targets: str = "ZS44A-k106",
    calibration_id: bytes = b"1ZS44A00000",
    checksums: bool = True,
) -> bytes:
    """A 1 MiB image laid out like the Xterra's, passing its checksum."""
    rom_map = xterra_map()
    rng = np.random.default_rng(seed)
    image = bytearray(rng.integers(0, 255, IMAGE_SIZE, dtype=np.uint8).tobytes())
    for region in rom_map.regions:
        if region.kind == "erased":
            image[region.start : region.end + 1] = b"\xff" * (region.end - region.start + 1)
    for offset, value in rom_map.checksums.markers.items():
        struct.pack_into(">I", image, offset, value)
    cal = rom_map.calibration_id
    image[cal.offset : cal.offset + cal.length] = calibration_id.ljust(cal.length, b"\0")[
        : cal.length
    ]
    calibration = load_afr_calibration(PROFILE / "afr_calibration.yaml")
    (k106,) = [t for t in calibration.target_maps if t.revision == "ZS44A-k106"]
    assert k106.raw_rpm_breakpoints is not None and k106.raw_load_breakpoints is not None
    image[0xCAA5 : 0xCAA5 + 8] = bytes(k106.raw_rpm_breakpoints)
    image[0xC894 : 0xC894 + 8] = bytes(k106.raw_load_breakpoints)
    image[0xCB29 : 0xCB29 + 16] = bytes([*range(0x10, 0x88, 8), 0x90])
    slots = rom_map.map_slots
    assert slots is not None
    for slot in range(1, slots.count + 1):
        cells = target_raw(slot1_targets if slot == 1 else other_targets)
        base = slots.slot_base(slot)
        image[base + 0x220 : base + 0x260] = bytes(v for row in cells for v in row)
        struct.pack_into(">H", image, base + 0x762, k)
    other = target_raw(other_targets)
    image[0xA169 : 0xA169 + 64] = bytes(v for row in other for v in row)
    if checksums:
        image = bytearray(with_checksums(bytes(image), rom_map))
    assert len(image) == IMAGE_SIZE
    return bytes(image)


def load_byte_image(
    *,
    literal: int = 0x00DC1D00,
    calibration_id: bytes = b"1ZS44A00000",
    fill_low: tuple[int, ...] | None = None,
) -> bytes:
    """:func:`synthetic_image` with invented, monotone Fuel Comp load-byte inputs.

    Round numbers at the offsets ``rom_map.yaml`` declares (N02.1), never ROM
    values; ``literal`` is the program constant the map declares, so a
    different value models another program. ``fill_low`` replaces the low
    fill curve (a falling one models an image whose curves are refused).
    """
    image = bytearray(synthetic_image(seed=11, calibration_id=calibration_id, checksums=False))
    struct.pack_into(">32H", image, 0xEE6A, *(100 * i for i in range(32)))
    struct.pack_into(">32H", image, 0xDD94, *(200 + 30 * i * i for i in range(32)))
    struct.pack_into(">16H", image, 0xEAEC, *(fill_low or tuple(2000 * i for i in range(16))))
    struct.pack_into(">16H", image, 0xEACC, *(30000 + 1000 * i for i in range(16)))
    image[0x8D25] = 150
    struct.pack_into(">H", image, 0x8ED2, 1000)
    struct.pack_into(">H", image, 0x90BE, 700)
    image[0x82FE] = 128
    struct.pack_into(">I", image, 0x3C5E8, literal)
    return bytes(image)


def with_checksums(image: bytes, rom_map: RomMap | None = None) -> bytes:
    """``image`` with its checksum words recomputed."""
    rom_map = rom_map or xterra_map()
    checks = rom_map.checksums
    out = bytearray(image)
    total, xor = checks.values(bytes(out))
    struct.pack_into(">I", out, checks.sum_offset, total)
    struct.pack_into(">I", out, checks.xor_offset, xor)
    return bytes(out)


def osiris_container(image: bytes) -> bytes:
    """``image`` wrapped as an ``.OsirisROM`` with a synthetic key."""
    prefix = SIGNATURE.ljust(96, b"\0")
    return Container(
        prefix,
        (
            Section(b"ROMData ", 0, transform(image, SYNTHETIC_KEY, encode=True)),
            Section(b"Keys    ", 1, bytes(8) + struct.pack(">I", SYNTHETIC_KEY)),
        ),
    ).serialize()


def encrypted_backup_lookalike() -> bytes:
    """1,048,592 bytes starting like an UpRev backup (only the documented prefix)."""
    return bytes.fromhex("32bec582") + bytes(IMAGE_SIZE + 16 - 4)


@functools.cache
def operator_image(name: str) -> bytes | None:
    """The decoded operator file, or ``None`` when it is not on this machine.

    Cached: decoding a real container takes most of a second.
    """
    path = OPERATOR_ROMS[name]
    if not path.exists():
        return None
    return Container.parse(path.read_bytes()).decoded()
