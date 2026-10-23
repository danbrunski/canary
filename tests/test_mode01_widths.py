"""Mode 01 registry widths against an independent J1979 table (batch044).

A batched Mode 01 response is sliced by each PID's registered ``byte_length``
(``isotp.split_batched_response``). A wrong width is invisible to a single-PID
read and fatal to a batch: PID 0x33 was registered 2 bytes wide, so it
swallowed the next PID's id byte and every later member timed out. The
expected widths live in test data written from the standard, never from the
registry, so the two sources can actually disagree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.collection.can_obd_client import decode_baro, decode_percent_b_minus_128
from src.collection.can_obd_collector import _batched_pid_width
from src.collection.isotp import split_batched_response
from src.collection.obd_registry import MODE01_REGISTRY
from src.yaml_load import load_yaml

_TABLE_PATH = Path(__file__).parent / "fixtures" / "j1979_mode01_widths.yaml"
J1979_WIDTHS: dict[int, int] = load_yaml(_TABLE_PATH.read_text())


def test_the_table_is_keyed_by_integer_pid() -> None:
    assert J1979_WIDTHS and all(isinstance(pid, int) for pid in J1979_WIDTHS)


def test_every_registry_pid_is_in_the_j1979_table() -> None:
    """A PID added to the registry must bring its width into the table, or its
    width goes unchecked the way 0x33's did."""
    missing = sorted(f"0x{pid:02X}" for pid in MODE01_REGISTRY if pid not in J1979_WIDTHS)
    assert not missing, f"add these to {_TABLE_PATH.name}: {missing}"


@pytest.mark.parametrize("pid", sorted(MODE01_REGISTRY), ids=lambda pid: f"0x{pid:02X}")
def test_registry_width_matches_j1979(pid: int) -> None:
    definition = MODE01_REGISTRY[pid]
    assert definition.byte_length == J1979_WIDTHS[pid], (
        f"0x{pid:02X} {definition.channel}: registry {definition.byte_length}, "
        f"J1979 {J1979_WIDTHS[pid]}"
    )


def test_batch_walk_uses_the_registry_width() -> None:
    """The live walk reads widths through ``_batched_pid_width``; pin that it
    sees the same number the table checks, on the exact bytes seen live."""
    # 41 | 33 51 | 44 80 00 | 0C 0B B8 — baro (81 kPa), commanded phi, rpm.
    payload = bytes.fromhex("33514480000C0BB8")
    readings, clean = split_batched_response(payload, _batched_pid_width)
    assert clean
    assert readings == {0x33: b"\x51", 0x44: b"\x80\x00", 0x0C: b"\x0b\xb8"}


def test_widened_bank_pair_decoders_still_read_byte_a() -> None:
    """0x55-0x58 grew to two bytes (A = bank 1/2, B = bank 3/4). The decoder
    reads A, so the channel value is what a one-byte slice gave before."""
    assert decode_percent_b_minus_128(b"\x90\x00") == decode_percent_b_minus_128(b"\x90")
    assert decode_baro(b"\x51") == (81.0, "kPa")
