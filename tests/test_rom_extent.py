"""Does an image hold the whole flash? (src/analysis/rom_extent.py)

Synthetic images pin each rule; the 2013 Xterra's ECU read and the operator's
2008 ROMs are known answers where they exist on this machine, and skip where
they do not (decoded ROMs stay out of Git).
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.analysis.rom_extent import (
    ExtentSpec,
    early_extent,
    read_extent,
    scan_sh2_calls,
)
from src.control.rom_backup import load_backup_recipe
from src.control.rom_library import RomLibrary
from tests.rom_fixtures import (
    OPERATOR_ROMS,
    XTERRA_2013_READ,
    XTERRA_2013_SHA256,
    operator_image,
)
from tools import rom_extent as cli

SIZE = 0x800
RAM = 0xFFFF0000
STACK = 0xFFFF7FFC
N50_RECIPE = (
    Path(__file__).resolve().parents[1] / "data/platforms/nissan-xterra-n50/rom_backup.yaml"
)


def _spec(*tables: tuple[int, int], calls: bool = False) -> ExtentSpec:
    return ExtentSpec.model_validate(
        {
            "ram_from": RAM,
            "vector_tables": [
                {"offset": offset, "entries": entries, "evidence": "test"}
                for offset, entries in tables
            ],
            "call_scan": "sh2" if calls else None,
        }
    )


def _image(vectors: list[int], *, at: int = 0x40, size: int = SIZE) -> bytearray:
    image = bytearray(size)
    for index, value in enumerate(vectors):
        struct.pack_into(">I", image, at + 4 * index, value)
    return image


GOOD = [0x400, STACK, 0x400, STACK, 0x402, 0xFFFFFFFF, 0x0]


def _call(image: bytearray, pc: int, target: int, *, gap: int = 0, jump: bool = False) -> None:
    """``mov.l @(disp,PC),r3`` at ``pc``, ``jsr``/``jmp @r3`` ``gap`` nops later."""
    disp = 0x10
    struct.pack_into(">H", image, pc, 0xD300 | disp)
    for n in range(gap):
        struct.pack_into(">H", image, pc + 2 + 2 * n, 0x0009)
    struct.pack_into(">H", image, pc + 2 + 2 * gap, 0x432B if jump else 0x430B)
    struct.pack_into(">I", image, (pc & ~3) + 4 + disp * 4, target)


def test_a_valid_table_inside_the_image_shows_no_sign() -> None:
    reading = read_extent(bytes(_image(GOOD)), _spec((0x40, len(GOOD))))
    assert reading.verdict == "no_sign"
    (table,) = reading.tables
    assert (table.valid, table.inside, table.ram, table.erased) == (True, 4, 2, 1)
    assert reading.summary() == (
        "nothing points past 0x0007FF: 7 vector-table entries (tables at 0x000040) land "
        "inside the image, in RAM or erased"
    )


def test_an_entry_past_the_image_and_below_ram_reads_larger() -> None:
    reading = read_extent(bytes(_image([*GOOD, 0x1000, 0x1000])), _spec((0x40, 9)))
    assert reading.verdict == "larger"
    assert reading.tables[0].beyond == (0x1000,)
    assert reading.summary() == (
        "the image points past its end (0x0007FF): the vector table at 0x000040 to "
        "0x00001000, so the flash is larger than the 2,048 bytes read"
    )
    assert reading.to_dict()["tables"][0]["beyond"] == ["0x00001000"]


@pytest.mark.parametrize(
    ("vectors", "reason"),
    [
        ([0x400, STACK, 0x401, STACK], "entry 2 is 0x00000401, an odd address below RAM"),
        ([0x1000, STACK, 0x400, STACK], "entry 0 is 0x00001000, not a reset address inside"),
        ([0x401, STACK, 0x400, STACK], "entry 0 is 0x00000401, not a reset address inside"),
        ([STACK, STACK, 0x400, STACK], "entry 0 is 0xFFFF7FFC, not a reset address inside"),
    ],
)
def test_a_table_that_is_not_all_addresses_says_nothing(vectors: list[int], reason: str) -> None:
    """Code read as a table must not become a pointer past the image."""
    reading = read_extent(bytes(_image([*vectors, 0x1000])), _spec((0x40, 5)))
    assert reading.verdict == "unknown"
    assert reading.tables[0].valid is False and reading.tables[0].beyond == ()
    assert reason in (reading.tables[0].reason or "")
    assert reading.summary().startswith("no vector table could be read (0x000040: entry")


def test_one_valid_table_is_enough_and_an_invalid_one_is_named() -> None:
    image = _image(GOOD)
    struct.pack_into(">I", image, 0x80, 0x401)  # a second "table" of code
    reading = read_extent(bytes(image), _spec((0x40, 4), (0x80, 4)))
    assert reading.verdict == "no_sign"
    assert [t.valid for t in reading.tables] == [True, False]


def test_a_table_past_the_image_or_the_bytes_given_is_invalid() -> None:
    spec = _spec((SIZE - 8, 4))
    reading = read_extent(bytes(_image(GOOD)), spec)
    assert reading.tables[0].reason == "the table runs to 0x000808, past the image's end"
    prefix = bytes(_image(GOOD)[:0x48])
    early = early_extent(prefix, _spec((0x40, 4)), image_size=SIZE)
    assert early.tables[0].reason == "only 0x000048 bytes were given, short of 0x000050"


def test_a_prefix_is_judged_against_the_whole_image() -> None:
    """The early check holds only the first blocks; past-the-end means past
    the recipe's whole range, not past the bytes read so far."""
    prefix = bytes(_image([0x400, STACK, 0x900, STACK], size=0x50))
    spec = _spec((0x40, 4))
    assert early_extent(prefix, spec, image_size=0x800).verdict == "larger"
    assert early_extent(prefix, spec, image_size=0x1000).verdict == "no_sign"
    assert spec.tables_end == 0x50


def test_call_targets_are_counted_and_never_the_verdict() -> None:
    image = _image(GOOD)
    _call(image, 0x100, 0x500)  # inside
    _call(image, 0x200, 0x2000, gap=2, jump=True)  # past the image, jmp 3 instructions on
    _call(image, 0x300, 0xFFFF1000)  # RAM
    _call(image, 0x400, 0x3000, gap=3)  # jsr 4 instructions on: not a pair
    reading = read_extent(bytes(image), _spec((0x40, 4), calls=True))
    calls = reading.calls
    assert calls is not None
    assert (calls.pairs, calls.inside, calls.ram, calls.beyond, calls.beyond_pairs) == (
        3,
        1,
        1,
        (0x2000,),
        1,
    )
    assert reading.verdict == "no_sign"
    assert reading.summary().endswith(
        "and 2 call targets land inside the image or in RAM; 1 call-like pair(s) target "
        "0x00002000, past it, which no vector table confirms"
    )


def test_a_call_scan_needs_the_register_it_loaded() -> None:
    image = _image(GOOD)
    _call(image, 0x100, 0x2000)
    struct.pack_into(">H", image, 0x102, 0x440B)  # jsr @r4, not @r3
    assert scan_sh2_calls(bytes(image), ram_from=RAM).pairs == 0


@pytest.mark.parametrize(
    "spec",
    [
        {"ram_from": RAM, "vector_tables": []},
        {"ram_from": RAM, "vector_tables": [{"offset": 0, "entries": 3, "evidence": "x"}]},
        {"ram_from": RAM, "vector_tables": [{"offset": 0, "entries": 4}]},
        {"ram_from": RAM, "vector_tables": [{"offset": 0, "entries": 4, "evidence": "x"}], "x": 1},
        {
            "ram_from": RAM,
            "vector_tables": [{"offset": 0, "entries": 4, "evidence": "x"}],
            "call_scan": "arm",
        },
    ],
)
def test_an_incomplete_spec_is_refused(spec: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ExtentSpec.model_validate(spec)


def test_the_shipped_n50_recipe_names_both_tables() -> None:
    spec = load_backup_recipe(N50_RECIPE, None).extent
    assert spec is not None and spec.call_scan == "sh2" and spec.ram_from == RAM
    assert [(t.offset, t.entries) for t in spec.vector_tables] == [(0, 4), (0x020004, 256)]


# -- known answers -------------------------------------------------------------


def _n50() -> ExtentSpec:
    spec = load_backup_recipe(N50_RECIPE, None).extent
    assert spec is not None
    return spec


def test_the_2013_xterra_read_holds_its_whole_flash() -> None:
    if not XTERRA_2013_READ.exists():
        pytest.skip(f"{XTERRA_2013_READ} is not on this machine")
    image = XTERRA_2013_READ.read_bytes()
    assert hashlib.sha256(image).hexdigest() == XTERRA_2013_SHA256
    reading = read_extent(image, _n50())
    assert reading.verdict == "no_sign"
    boot, app = reading.tables
    assert boot.valid and (boot.inside, boot.ram) == (2, 2)
    assert app.valid and (app.inside, app.ram, app.beyond) == (254, 2, ())
    assert reading.calls is not None
    assert (reading.calls.pairs, reading.calls.ram, reading.calls.beyond_pairs) == (4159, 5, 0)
    # A boot table read past its 230 entries reads code, which the rule
    # refuses rather than turning into a pointer past the image.
    long_boot = ExtentSpec.model_validate(
        {"ram_from": RAM, "vector_tables": [{"offset": 0, "entries": 256, "evidence": "x"}]}
    )
    assert read_extent(image, long_boot).verdict == "unknown"


@pytest.mark.parametrize("name", sorted(OPERATOR_ROMS))
def test_the_operators_2008_roms_do_not_point_past_1_mib(name: str) -> None:
    image = operator_image(name)
    if image is None:
        pytest.skip(f"{OPERATOR_ROMS[name]} is not on this machine")
    assert read_extent(image, _n50()).verdict != "larger"


# -- the library and the CLI ---------------------------------------------------


def test_the_library_shows_each_images_extent(tmp_path: Path) -> None:
    spec = _spec((0x40, 4))
    library = RomLibrary(
        tmp_path / "lib",
        profile_dir=tmp_path / "profile",
        registry=lambda: None,
        extent_spec=lambda: spec,
    )
    entry, _ = library.add_image(bytes(_image(GOOD)), source="upload_plain")
    assert entry["extent"]["verdict"] == "no_sign"
    assert library.describe(entry["sha256"])["entry"]["extent"]["verdict"] == "no_sign"
    bare = RomLibrary(tmp_path / "lib", profile_dir=tmp_path / "profile", registry=lambda: None)
    assert bare.entry(entry["sha256"])["extent"] is None


def _recipe(tmp_path: Path, *, extent: bool = True) -> Path:
    text = N50_RECIPE.read_text()
    if not extent:
        text = text.split("\nextent:", 1)[0] + "\nprovenance:" + text.split("\nprovenance:", 1)[1]
    path = tmp_path / "rom_backup.yaml"
    path.write_text(text)
    return path


def test_the_cli_reads_each_file_and_names_what_it_cannot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = _image([0x400, STACK, 0x400, STACK], at=0, size=0x100000)
    struct.pack_into(">IIII", image, 0x020004, 0x21000, STACK, 0x21000, STACK)
    good = tmp_path / "good.bin"
    good.write_bytes(bytes(image))
    assert cli.main([str(good), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["verdict"] == "no_sign"
    assert result["sha256"] == hashlib.sha256(image).hexdigest()
    assert cli.main([str(good), str(tmp_path / "missing.bin")]) == 1
    captured = capsys.readouterr()
    assert "verdict no_sign: nothing points past 0x0FFFFF" in captured.out
    assert "missing.bin: could not read" in captured.err
    assert cli.main(["--recipe", str(_recipe(tmp_path, extent=False)), str(good)]) == 2
