"""``scripts/decode_uprev_flash.py`` on a synthetic loader session.

The real answers it was checked against are the 2026-10-04 and 2026-11-11
flashes (``docs/skunkworks/uprev-flash-procedure.md``): both captures hold ROM
bytes on 0x743 and stay out of the repo. This builds the same exchange for a
made-up 64 KiB image, so the verdict logic is pinned: a complete capture
verifies, and each way a capture falls short names its own cause.
"""

from __future__ import annotations

import importlib.util
import zlib
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "decode_uprev_flash.py"
PAGE = 8192
SIZE = 8 * PAGE


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("decode_uprev_flash", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


decoder = _module()


def _images() -> tuple[bytes, bytes]:
    old = bytes((i * 7 + (i >> 8)) & 0xFF for i in range(SIZE))
    new = bytearray(old)
    new[PAGE + 5] ^= 0xFF  # page 1
    new[5 * PAGE + 9] ^= 0x0F  # page 5
    return old, bytes(new)


class _Session:
    """A candump -L log of one loader session, built frame by frame."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.now = 1_700_000_000.0

    def frame(self, can_id: int, data: bytes) -> None:
        self.now += 0.001
        self.lines.append(f"({self.now:.6f}) can0 {can_id:03X}#{data.hex().upper()}")

    def exchange(self, command: bytes, reply: bytes | None) -> None:
        self.frame(0x742, command.ljust(8, b"\x00"))
        if reply is not None:
            self.frame(0x762, reply.ljust(8, b"\x00"))

    def crcs(self, flash: bytes) -> None:
        for start in range(0, SIZE, 2 * PAGE):
            length = 2 * PAGE
            command = b"\x20" + start.to_bytes(4, "big") + length.to_bytes(3, "big")
            reply = b"\x20" + zlib.crc32(flash[start : start + length]).to_bytes(4, "big")
            self.exchange(command, reply)

    def page(self, address: int, payload: bytes, *, drop: int = 0) -> None:
        self.exchange(b"\x64", b"\x64")
        sent = b""
        for k in range(0, len(payload), 8):
            if drop and k == 8 * drop:
                continue
            self.frame(0x743, payload[k : k + 8])
            sent += payload[k : k + 8]
        self.exchange(b"\x22", b"\x22" + zlib.crc32(sent).to_bytes(4, "big"))
        self.exchange(b"\x55" + address.to_bytes(4, "big"), b"\x55")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _flash(old: bytes, new: bytes, *, after: bool = True, drop: int = 0) -> str:
    s = _Session()
    s.frame(0x23D, bytes(8))  # broadcast traffic is ignored
    s.exchange(b"\x04", bytes.fromhex("040000261c"))
    s.exchange(b"\x65", b"\x65")
    s.crcs(old)
    s.exchange(b"\x50\x00", b"\x50")
    for address in (PAGE, 5 * PAGE):
        s.page(address, new[address : address + PAGE], drop=drop)
    if after:
        s.crcs(new)
        s.exchange(b"\x67" + zlib.crc32(new).to_bytes(4, "big"), b"\x67")
        s.exchange(b"\x66", None)
    return s.text()


def test_a_complete_capture_verifies_and_dates_the_boundary() -> None:
    old, new = _images()
    text = _flash(old, new)
    result = decoder.decode(decoder.parse_candump(text), old, new)
    assert result["verdict"] == "verified", result["problems"]
    assert result["summary"]["before_crcs_matching_old"] == "4/4"
    assert result["summary"]["after_crcs_matching_new"] == "4/4"
    assert result["summary"]["pages_equal_to_new"] == "2/2"
    assert result["summary"]["changed_pages"] == [hex(PAGE), hex(5 * PAGE)]
    # The boundary is the last range-CRC reply after programming, not the
    # finalize or the exit that follow it.
    assert result["boundary"] == result["range_crcs"][-1]["time"]
    assert [o["command"][:2] for o in result["other"]] == ["04", "65", "67", "66"]
    assert result["other"][-1]["reply"] is None


def test_the_wrong_new_image_is_refused_page_by_page() -> None:
    old, new = _images()
    result = decoder.decode(decoder.parse_candump(_flash(old, new)), old, old)
    assert result["verdict"] == "not_verified" and result["boundary"] is None
    text = "\n".join(result["problems"])
    assert f"page {PAGE:#x}" in text and "NOT equal" in text
    assert "finalize command's CRC" in text


def test_a_capture_that_stops_before_the_final_crcs_says_so() -> None:
    old, new = _images()
    result = decoder.decode(decoder.parse_candump(_flash(old, new, after=False)), old, new)
    assert result["verdict"] == "not_verified"
    assert any("capture ended before" in p for p in result["problems"])
    assert result["summary"]["pages_equal_to_new"] == "2/2"


def test_a_dropped_data_frame_is_a_named_problem_not_a_silent_short_page() -> None:
    old, new = _images()
    result = decoder.decode(decoder.parse_candump(_flash(old, new, drop=3)), old, new)
    assert result["verdict"] == "not_verified"
    assert any(f"{PAGE - 8} bytes received" in p for p in result["problems"])


def test_a_capture_with_no_loader_traffic_is_not_an_empty_success() -> None:
    old, new = _images()
    frames = decoder.parse_candump("(1700000000.000001) can0 23D#0000000000000000\n")
    result = decoder.decode(frames, old, new)
    assert result["verdict"] == "not_verified"
    assert "no loader traffic" in result["problems"][0]


def test_a_different_running_image_is_noted_without_failing_the_flash() -> None:
    old, new = _images()
    other = bytes(SIZE)
    result = decoder.decode(decoder.parse_candump(_flash(old, new)), other, new)
    assert result["before_note"] is not None and "not" in result["before_note"]
    # What the ECU holds now is settled by the final CRCs against --new alone.
    assert result["verdict"] == "verified", result["problems"]
