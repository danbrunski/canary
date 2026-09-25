"""``tools/can_obd_probe.py --batch`` (D03.24 c).

The probe sends a batched Mode 01 read through the shared
``CanObdClient.query_mode_01_batch`` and ``split_batched_response`` — no
second transport — so these tests drive it against a scripted bus with the
6-PID response verified on the truck on 2026-07-26.
"""

from __future__ import annotations

import argparse

import pytest

from src.collection.can_obd_client import CanObdClient
from tests.fakes import CannedResponseBus
from tools.can_obd_probe import batch_groups, parse_args, parse_pid_list, read_batch

#: 41 0C 0000 0D 00 04 00 11 13 0E FF 10 0076 — 15 bytes, first frame + 2 CFs.
SIX_PID_RESPONSE = [
    bytes([0x10, 0x0F, 0x41, 0x0C, 0x00, 0x00, 0x0D, 0x00]),
    bytes([0x21, 0x04, 0x00, 0x11, 0x13, 0x0E, 0xFF, 0x10]),
    bytes([0x22, 0x00, 0x76, 0x00, 0x00, 0x00, 0x00, 0x00]),
]


def test_batch_flag_parses_hex_lists_and_repeats() -> None:
    args = parse_args(["--batch", "0C,0D,04", "--batch", "0x11,0e,10"])
    assert args.batch == [[0x0C, 0x0D, 0x04], [0x11, 0x0E, 0x10]]


@pytest.mark.parametrize("bad", ["0C,0D,04,11,0E,10,05", "0C,0C", "zz", "100", ""])
def test_batch_flag_refuses_what_the_request_builder_would(bad: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_pid_list(bad)


def test_read_batch_sends_one_request_and_splits_every_pid() -> None:
    bus = CannedResponseBus(SIX_PID_RESPONSE)
    client = CanObdClient(bus, timeout_seconds=0.5)  # type: ignore[arg-type]

    lines = read_batch(client, [0x0C, 0x0D, 0x04, 0x11, 0x0E, 0x10])

    assert bus.sent[0].data.hex().upper() == "07010C0D04110E10"
    assert lines[0].startswith("BATCH [0C 0D 04 11 0E 10]: responder 0x7E8, walk clean")
    body = "\n".join(lines[1:])
    for pid in ("0x0C", "0x0D", "0x04", "0x11", "0x0E", "0x10"):
        assert pid in body
    assert "absent" not in body
    # 0x10 (MAF) 0x0076 = 118 / 100 = 1.18 g/s, decoded by the registry decoder.
    maf = next(line for line in lines if line.strip().startswith("0x10"))
    assert "0076" in maf and "1.180" in maf


def test_read_batch_names_a_pid_the_ecu_omitted() -> None:
    # Asked for 0C,0D,05; the ECU answered 0C and 0D only (single frame).
    bus = CannedResponseBus([bytes([0x06, 0x41, 0x0C, 0x0B, 0xB8, 0x0D, 0x32, 0x00])])
    client = CanObdClient(bus, timeout_seconds=0.3)  # type: ignore[arg-type]

    lines = read_batch(client, [0x0C, 0x0D, 0x05])

    assert "walk clean" in lines[0]
    assert lines[-1] == "  absent: 0x05 (omitted by the ECU)"


def test_read_batch_reports_silence_as_no_answer() -> None:
    client = CanObdClient(CannedResponseBus([]), timeout_seconds=0.1)  # type: ignore[arg-type]
    assert read_batch(client, [0x0C])[0].endswith(
        "no answer (timeout, negative response, or passive mode)"
    )


def test_batch_supported_groups_only_splittable_pids_six_at_a_time() -> None:
    supported = [0x04, 0x05, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x1C]
    groups = batch_groups(supported)
    assert [len(g) for g in groups] == [6, 3]
    assert [pid for group in groups for pid in group] == supported
