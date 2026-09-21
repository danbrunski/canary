"""Offline, read-only container decoder; derived outputs stay outside Git."""

import argparse
import hashlib
import struct
import zlib
from pathlib import Path


def mask(seed, length):
    return b"".join(
        struct.pack(">I", zlib.crc32(struct.pack(">I", (seed + 5 * j) & 0xFFFFFFFF) * 4))
        for j in range((length + 3) // 4)
    )[:length]


def read_sections(path):
    data = Path(path).read_bytes()
    if not data.startswith(b"UpRev Osiris ROM File Ver 1.0000") or len(data) < 96:
        raise ValueError("Unsupported ROM container signature")
    sections = {}
    offset = 96
    while offset < len(data):
        name, length, total, option, crc = struct.unpack_from("<8sIIII", data, offset)
        if total != length + 24 or offset + total > len(data):
            raise ValueError("Invalid section bounds")
        raw = data[offset + 24 : offset + total]
        if option not in (0, 1):
            raise ValueError("Unsupported section encoding")
        clear = bytes(a ^ b for a, b in zip(raw, mask(crc, length), strict=True)) if option else raw
        if zlib.crc32(clear) != crc:
            raise ValueError(f"{name!r}: CRC mismatch")
        if (
            bytes(a ^ b for a, b in zip(clear, mask(crc, length), strict=True)) if option else clear
        ) != raw:
            raise ValueError("Outer roundtrip mismatch")
        if name.decode().strip() in sections:
            raise ValueError("Duplicate section")
        sections[name.decode().strip()] = clear
        print(
            Path(path).name,
            name.decode().strip(),
            "length",
            length,
            "CRC verified; exact outer roundtrip",
        )
        offset += total
    return sections


def rol16(v, n):
    return ((v << n) | (v >> (16 - n))) & 65535


def decode_rom(data, key):
    output = bytearray()
    for (word,) in struct.iter_unpack(">I", data):
        hi, lo = word >> 16, word & 65535
        v = ((key & 65535) + lo) & 65535
        v = (v + rol16(v, 1) - 1) & 65535
        v = v ^ rol16(v, 4) ^ hi
        w = (v + (key >> 16)) & 65535
        w = (w + rol16(w, 2) - 1) & 65535
        w ^= lo
        output.extend(struct.pack(">I", (w << 16) | v))
    return bytes(output)


def encode_rom(data, key):
    output = bytearray()
    for (word,) in struct.iter_unpack(">I", data):
        lo, hi = word & 65535, word >> 16
        v = (lo + (key >> 16)) & 65535
        w = ((rol16(v, 2) + v - 1) & 65535) ^ hi
        v = ((key & 65535) + w) & 65535
        v = (rol16(v, 1) + v - 1) & 65535
        v = v ^ rol16(v, 4) ^ lo
        output.extend(struct.pack(">I", (v << 16) | w))
    return bytes(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Offline research decoder; never writes input ROMs or contacts hardware."
    )
    parser.add_argument("roms", nargs="+", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Optional private output directory outside Git; existing files are never overwritten",
    )
    args = parser.parse_args()
    output = args.output_dir
    if output:
        output.mkdir(parents=True, exist_ok=True)
    results = []
    for arg in args.roms:
        sections = read_sections(arg)
        if "ROMData" in sections:
            key = struct.unpack_from(">I", sections["Keys"], 8)[0]
            decoded = decode_rom(sections["ROMData"], key)
            if encode_rom(decoded, key) != sections["ROMData"]:
                raise ValueError("Inner roundtrip mismatch")
            print("exact inner transform roundtrip verified")
            if output:
                dest = output / (Path(arg).stem + ".decoded.bin")
                with dest.open("xb") as stream:
                    stream.write(decoded)
            print("ROM decoded SHA256", hashlib.sha256(decoded).hexdigest())
            results.append(decoded)
        else:
            raise ValueError("Missing ROMData section")
    if len(results) == 2:
        diffs = [(i, a, b) for i, (a, b) in enumerate(zip(*results, strict=True)) if a != b]
        print("DECODED_DIFF_COUNT", len(diffs))
        print("DECODED_DIFFS", [(hex(i), hex(a), hex(b)) for i, a, b in diffs[:40]])
