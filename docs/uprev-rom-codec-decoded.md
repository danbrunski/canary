# UpRev ROM payload decoded — Windows handoff, 2026-09-20

This supersedes the **not decoded** / canceled-inspection status in
`uprev-rom-container-research.md` and `uprev-rom-windows-inspection.md`.
Both the current tune and the known one-cell edit now decode, with exact
outer and inner byte-for-byte roundtrips. No input ROM, editor state, or
ECU was changed. This is offline research, not a flashing implementation.

## Verified decoded differences

Offsets below are zero-based offsets in the **decoded 1 MiB ROM**, not the
container. Exactly three bytes differ:

| ROM offset | Current tune | Map1Change | Meaning |
|---|---|---|---|
| `0x959b` | `c0` | `c7` | Low byte of 32-bit XOR checksum at `0x9598` |
| `0x95a3` | `aa` | `a9` | Low byte of 32-bit sum checksum at `0x95a0` |
| `0xe075f` | `84` | `83` | Known Map 1 Fuel Compensation edit, 132 → 131 |

The final change matches `100 * count / 128`: 103.125 → 102.34375,
displayed as 103.13 → 102.34. The operator identified this cell as
7200 RPM / 50.00% Volumetric Flow Ratio. This establishes that cell's
location, **not** the complete table layout or Fuel Targets table location.

Decoded SHA-256:

- Current tune: `e01cc0cb45d5beb879f82931be3054c79b7be3955220b3d93045deaabd23f18e`
- Map1Change: `3dc1f3d9c93615f84c86f463530e666183ff3c7f90f0bd27b4ab9556742a76c5`

Original container hashes and edit provenance remain in the parent note.

## Container codec

The 96-byte prefix precedes sections with 24-byte little-endian header
`<8sIIII`: label, payload length, total section length, encoding option,
CRC32. Total length is payload length + 24. The previously unknown last
header word is the CRC32 of the **unmasked section**, before ROM decryption.

Option 0 is unmasked. Option 1 XORs the payload with a deterministic stream:
for word index j, let `s = (section_crc + 5*j) mod 2^32`; compute standard
CRC32 (`zlib.crc32`) of four copies of BE32(s), then emit the CRC as BE32.
Concatenate and truncate to payload length. XOR is its own inverse.
All four sections in both supplied ROMs pass their CRCs and exact outer
roundtrip checks. This explains why a tiny edit changes almost every
encoded ROMData byte: the changed CRC changes the entire mask stream.

## ROMData word transform

After unmasking, read the ROM key as BE32 at offset 8 of the Keys section.
No key value is recorded here or embedded in the decoder. Each BE32 word
of unmasked ROMData is independently transformed. Let H/L be its upper/
lower 16 bits and KH/KL the key halves; R(x,n) is a 16-bit rotate-left.
Each arithmetic assignment below wraps to 16 bits:

```text
v = KL + L
v = v + R(v,1) - 1
v = v XOR R(v,4) XOR H
w = v + KH
w = w + R(w,2) - 1
w = w XOR L
decoded_word = (w << 16) | v
```

`tools/decode_uprev_rom_research.py` implements this and an inverse used
only for in-memory roundtrip verification. It has no container writer,
checksum repair, hardware access, or flash functionality. By default it
prints validation/hashes/differences and writes nothing. Optional decoded
output must be kept outside Git; existing files are never overwritten.

```sh
.venv/bin/python tools/decode_uprev_rom_research.py \
  /home/dan/Uprev/ROMs/ZS44A-Tuned.OsirisROM \
  /home/dan/Uprev/ROMs/ZS44A-Tuned-Map1Change.OsirisROM
```

This is a narrowly validated research tool, not a general parser for all
Osiris versions. It rejects unsupported signatures/options and bad CRCs;
malformed/truncated inputs can raise a Python exception. No claim is made
that every possible protected ROM uses the same path.

## Internal checksum verification

The loaded editor identifies these ROMs as Nissan checksum style 1:
BE32 at `0x20008` and `0x20010` both equal `0xffff7ffc`.
Iterate aligned BE32 words from `0x8204` through ROM end, excluding words
at `0x20000`, `0x9598`, and `0x95a0`. Their modulo-2^32 sum and XOR equal
the stored words at `0x95a0` and `0x9598`, respectively, in **both** ROMs:

| Image | Sum | XOR |
|---|---|---|
| Current tune | `f438fcaa` | `0c3bbac0` |
| Map1Change | `f438fca9` | `0c3bbac7` |

Thus the only non-checksum decoded difference is the known fuel cell.

## Evidence provenance and remaining work

The operator successfully ran the read-only elevated inspection helper:
9,033 readable 4096-byte pages (37,003,264 bytes). Earlier canceled/failed
attempts and an old error.txt are not the final result. The loaded image
contains usable CLR metadata and an embedded UpRev assembly at image
offset `0x70467f`. Relevant method rows in that assembly:

- FileChunk.ReadFromBinaryReader: 29; getMongData: 31; CRC32.Crc32: 5.
- OsirisROM.get_ROMKey: 224; get_DecryptedROM: 243.
- decryptArray: 206; UnlockInvClone: 211; encryptArray: 208; UnlockClone: 210.
- NissanChecksum.setStyle: 185; VerifyChecksums: 188; Checksum constructor: 288.

The image and decoded binaries remain local at
`C:\Users\thebo\Documents\Codex\2026-09-20\i-hav\work\rom-codec-local`.
Do not commit these, proprietary DLLs, ROMs, or key values. The helper
requested process query/read rights only; it did not patch the editor,
change licensing, or contact an ECU.

ECUDefs.Osiris uses the same outer mask/CRC: both Script00 and Script01
passed outer decoding checks, but their resulting contents have another
encryption layer and are **not decoded definitions**. Main editor method
ECUDefinitions.load_script_from_uprev_file (row 917) applies a symmetric
decryptor with caller-supplied key/IV before Lua processing. Algorithm and
authorized key provenance were not resolved in this pass.

Next: map full tables/axes/scaling against independent editor evidence.
The existing Fuel Targets transcription is referenced in
`data/nissan-xterra-2008/afr_calibration.yaml`; it has not yet been matched
against this decoded ROM. Keep Fuel Targets separate from the proven
Fuel Compensation cell. A valid roundtrip and checksums alone do not
establish correct table semantics or authorize ROM writing. No full gate
was run, as requested.
