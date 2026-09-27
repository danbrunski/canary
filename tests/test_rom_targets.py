"""Synthetic fixtures only: no proprietary ROM bytes, keys or executables."""

import json
import struct
from datetime import UTC, date, datetime, timedelta

import pytest
import yaml

from scripts.rom_targets import main
from src.analysis.osiris import SIGNATURE, Container, Section, transform
from src.analysis.rom_targets import Definition, Patch, TargetEditor, sha256
from src.signals.afr import TargetAfrMap, load_afr_calibration
from src.vehicle.tune_revisions import load_tune_revisions


@pytest.fixture
def editor():
    definition = Definition.model_validate(
        {
            "schema_version": 1,
            "table": "fuel_targets",
            "map_slot": 1,
            "image_size": 128,
            "table_offset": 64,
            "rpm_axis_offset": 32,
            "load_axis_offset": 34,
            "afr_numerator": 1881.6,
            "calibration_file": "calibration.yaml",
            "checksums": {
                "algorithm": "be32_sum_xor",
                "start": 0,
                "excluded": [8, 12],
                "sum_offset": 8,
                "xor_offset": 12,
                "markers": {16: 0x12345678},
            },
        }
    )
    rom = bytearray(128)
    struct.pack_into(">I", rom, 16, 0x12345678)
    rom[32:36] = bytes([24, 32, 52, 68])
    rom[64:68] = bytes([128, 129, 130, 131])
    total, xor = definition.checksums.values(bytes(rom))
    struct.pack_into(">II", rom, 8, total, xor)
    # This is an arbitrary synthetic key, never an extracted production value.
    key = 0x12345678
    prefix = SIGNATURE.ljust(96, b"\0")
    source = Container(
        prefix,
        (
            Section(b"ROMData ", 1, transform(bytes(rom), key, encode=True)),
            Section(b"Keys    ", 1, bytes(8) + struct.pack(">I", key)),
            Section(b"Unknown ", 0, b"preserve this section verbatim"),
            Section(b"Notes   ", 1, b""),
        ),
    ).serialize()
    target = TargetAfrMap(
        revision="synthetic",
        rpm_breakpoints=[1200, 1600],
        load_breakpoints=[8.76, 11.46],
        raw_rpm_breakpoints=[24, 32],
        raw_load_breakpoints=[52, 68],
        raw_values=[[128, 129], [130, 131]],
        afr=[[1881.6 / n for n in row] for row in [[128, 129], [130, 131]]],
        provenance={
            "method": "verified_rom_decode",
            "rom_file": "synthetic.OsirisROM",
            "rom_sha256": sha256(source),
            "rom_modified_at": datetime(2026, 1, 1, tzinfo=UTC),
            "recorded_on": date(2026, 1, 1),
            "map_slot": 1,
            "cell_decode": "1881.6 / raw",
            "guide_reference": "synthetic fixture",
        },
    )
    return TargetEditor(source, definition, target)


def patch_for(editor, **change):
    return Patch.model_validate(
        {
            "schema_version": 1,
            "source_sha256": sha256(editor.source),
            "table": "fuel_targets",
            "map_slot": 1,
            "changes": [
                {
                    "row": 1,
                    "column": 1,
                    "rpm": 1600.0,
                    "bfs_ms": 11.46,
                    "old_raw": 131,
                    "new_raw": 132,
                    "rationale": "synthetic test",
                    **change,
                }
            ],
        }
    )


@pytest.mark.parametrize("key", [0, 1, 0x12345678, 0xFFFFFFFF])
def test_codec_inverse(key):
    data = bytes(range(256))
    assert transform(transform(data, key, encode=True), key) == data
    assert transform(transform(data, key), key, encode=True) == data


def test_patch_roundtrip_and_preservation(editor):
    candidate, report = editor.apply(patch_for(editor))
    parsed = Container.parse(candidate)
    decoded = parsed.decoded()
    assert decoded[67] == 132
    assert set(report["decoded_changed_offsets"]) <= {hex(x) for x in [*range(8, 16), 67]}
    assert parsed.sections[1:] == editor.container.sections[1:]
    assert parsed.prefix == editor.container.prefix
    editor.definition.checksums.verify(decoded)
    assert editor.rom[67] == 131
    assert Container.parse(editor.source).serialize() == editor.source
    assert report["candidate_sha256"] == sha256(candidate)


@pytest.mark.parametrize(
    "change,match",
    [
        ({"old_raw": 130}, "old cell"),
        ({"rpm": 2000.0}, "axis labels"),
        ({"bfs_ms": 12.0}, "axis labels"),
        ({"row": 2}, "outside table"),
        ({"column": 2}, "outside table"),
        ({"new_raw": 131}, "No-op"),
    ],
)
def test_bad_cell_refused(editor, change, match):
    with pytest.raises(ValueError, match=match):
        editor.apply(patch_for(editor, **change))


@pytest.mark.parametrize("value", [0, 256, True, 131.5, "132"])
def test_raw_values_must_be_nonzero_bytes(editor, value):
    with pytest.raises(ValueError):
        patch_for(editor, new_raw=value)


def test_patch_identity_and_duplicates(editor):
    patch = patch_for(editor)
    for changes, message in [
        ({"source_sha256": "0" * 64}, "hash"),
        ({"map_slot": 2}, "slot"),
        ({"changes": patch.changes * 2}, "Duplicate"),
    ]:
        with pytest.raises(ValueError, match=message):
            editor.apply(patch.model_copy(update=changes))


def test_unknown_hash_and_axes_refused(editor):
    with pytest.raises(ValueError, match="hash"):
        TargetEditor(editor.source + b"x", editor.definition, editor.target)
    bad = editor.target.model_copy(update={"raw_rpm_breakpoints": [25, 32]})
    with pytest.raises(ValueError, match="axis bytes"):
        TargetEditor(editor.source, editor.definition, bad)


def test_wrong_table_scaling_and_overlapping_definition(editor):
    bad = editor.definition.model_copy(update={"afr_numerator": 1800.0})
    with pytest.raises(ValueError, match="scaling"):
        TargetEditor(editor.source, bad, editor.target)
    bad = editor.definition.model_copy(update={"table_offset": 126})
    with pytest.raises(ValueError, match="Table/axis"):
        TargetEditor(editor.source, bad, editor.target)
    bad = editor.definition.model_copy(update={"map_slot": 2})
    with pytest.raises(ValueError, match="slot"):
        TargetEditor(editor.source, bad, editor.target)
    checksums = editor.definition.checksums.model_copy(update={"sum_offset": 9})
    with pytest.raises(ValueError, match="offsets"):
        checksums.values(editor.rom)


def test_patch_cannot_smuggle_other_tables_or_empty_changes(editor):
    base = patch_for(editor).model_dump()
    for fields in [{"table": "fuel_compensation"}, {"changes": []}, {"axes": [1, 2]}]:
        with pytest.raises(ValueError):
            Patch.model_validate({**base, **fields})


@pytest.mark.parametrize("length", [0, 30, 95, 97, 119, 121])
def test_truncated_container(editor, length):
    with pytest.raises(ValueError):
        Container.parse(editor.source[:length])


def test_bad_sections(editor):
    for offset, value, message in [(112, 2, "encoding"), (108, 0, "bounds")]:
        bad = bytearray(editor.source)
        struct.pack_into("<I", bad, offset, value)
        with pytest.raises(ValueError, match=message):
            Container.parse(bytes(bad))
    bad = bytearray(editor.source)
    bad[120] ^= 1
    with pytest.raises(ValueError, match="CRC"):
        Container.parse(bytes(bad))
    with pytest.raises(ValueError, match="Duplicate"):
        Container.parse(editor.source + editor.container.sections[0].serialize())
    with pytest.raises(ValueError, match="Missing"):
        Container(editor.container.prefix, ()).decoded()
    with pytest.raises(ValueError, match="Keys"):
        Container(editor.container.prefix, (Section(b"Keys    ", 0, b"short"),))._key()


def test_bad_internal_checksum_and_style(editor):
    for offset, message in [(70, "checksum mismatch"), (16, "checksum style")]:
        bad = bytearray(editor.rom)
        bad[offset] ^= 1
        with pytest.raises(ValueError, match=message):
            editor.definition.checksums.verify(bytes(bad))


def test_cli_dry_run_and_exclusive_bundle(editor, tmp_path, monkeypatch, capsys):
    rom = tmp_path / "source.OsirisROM"
    rom.write_bytes(editor.source)
    definition = tmp_path / "definition.yaml"
    definition.write_text(yaml.safe_dump(editor.definition.model_dump()))
    (tmp_path / "calibration.yaml").write_text(
        yaml.safe_dump(
            {
                "target_maps": [editor.target.model_dump(mode="json")],
            }
        )
    )
    patch = tmp_path / "patch.json"
    patch.write_text(patch_for(editor).model_dump_json())
    base = [
        "rom_targets",
        "apply",
        str(rom),
        "--definition",
        str(definition),
        "--patch",
        str(patch),
    ]
    monkeypatch.setattr("sys.argv", base)
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["dry_run"]
    expected = tmp_path / "expected.OsirisROM"
    expected.write_bytes(editor.apply(patch_for(editor))[0])
    output = tmp_path / "review"
    monkeypatch.setattr(
        "sys.argv", [*base, "--output-dir", str(output), "--expect-rom", str(expected)]
    )
    assert main() == 0
    assert (output / "candidate.OsirisROM").read_bytes() == expected.read_bytes()
    assert json.loads((output / "review.json").read_text())["expected_rom_byte_exact"]
    assert main() == 2  # Never replace a review directory.
    assert rom.read_bytes() == editor.source
    expected.write_bytes(b"wrong")
    monkeypatch.setattr("sys.argv", [*base, "--expect-rom", str(expected)])
    assert main() == 2


def test_actual_revision_boundary_and_historical_table():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "data/nissan-xterra-2008"
    registry = load_tune_revisions(root / "tune_revisions.yaml")
    boundary = datetime(2026, 9, 21, 3, 8, 45, 954035, tzinfo=UTC)
    assert registry.at(boundary - timedelta(microseconds=1)).revision == "ZS44A-k106"
    assert registry.at(boundary).revision == "ZS44A-20260920"
    calibration = load_afr_calibration(root / "afr_calibration.yaml")
    old, new = calibration.target_map("ZS44A-k106"), calibration.target_map("ZS44A-20260920")
    assert old.raw_values[7][4] == 162
    assert new.raw_values[7][4] == 163
    differences = [
        (r, c) for r in range(8) for c in range(8) if old.raw_values[r][c] != new.raw_values[r][c]
    ]
    assert differences == [(7, 4)]
    assert calibration.active_revision == new.revision
