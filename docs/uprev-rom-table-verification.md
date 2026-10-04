# Independent ROM decode and Fuel Targets verification

2026-09-20, WSL follow-up to `uprev-rom-codec-decoded.md`. Read-only analysis;
no ROM outputs, editor changes, ECU communication, or calibration edits.

## Reproduced Windows results

Ran the supplied `tools/decode_uprev_rom_research.py` against the original
and Map1Change under `/home/dan/Uprev/ROMs/`. Every section passed its CRC
and exact outer roundtrip, and both ROMData payloads passed the exact inner
transform roundtrip. Decoded hashes matched the Windows report:

- Original: `e01cc0cb45d5beb879f82931be3054c79b7be3955220b3d93045deaabd23f18e`
- Edit: `3dc1f3d9c93615f84c86f463530e666183ff3c7f90f0bd27b4ab9556742a76c5`

Exactly three decoded byte differences reproduced: `0x959b` c0->c7,
`0x95a3` aa->a9, `0xe075f` 84->83. Independently recomputed the original
image's BE32 sum/XOR checksums over the documented range/exclusions and
matched both stored values. Windows also verified the edited image checksums.

## Second known answer: complete Fuel Targets table

Used the **existing**, independently transcribed data in
`data/nissan-xterra-2008/afr_calibration.yaml`, not expectations generated
from the decoded bytes. Its provenance binds the transcription to original
container SHA-256
`c5325776b08c3084edd49d8f68587dad4668e02b0e77c7686f7548fb6f7c8fa9`.

Searching the complete decoded original yielded:

| Reference sequence | Decoded ROM offset | Matches |
|---|---|---:|
| All 64 Fuel Targets bytes, 8 RPM rows x 8 BFS columns, row-major | `0xe0220` | 1 |
| Transposed (column-major) full target table | none | 0 |
| Raw RPM axis `18 20 28 38 48 50 58 68` | `0xcaa5` | 1 |
| Raw BFS axis `34 44 5d 64 6c 6e 7f 80` | `0xc894` | 1 |

Thus the full transcribed Fuel Targets table is reproduced at
`0xe0220..0xe025f`, with the expected row-major layout. Its documented
decode is `AFR = 14.7 * 128 / raw`, and RPM breakpoints are raw * 50.
The BFS engineering values remain the existing editor transcription;
do not replace those with an approximate universal scale.

Axis matches establish their byte locations; no table-pointer/reference
structure was decoded in this pass. These are **offsets in the decoded ROM**,
not Osiris container offsets or verified ECU runtime addresses. Do not
assume they generalize to another ROM revision or map slot.

This is a different table from the operator's known **Fuel Compensation**
edit at `0xe075f`. That one-cell edit does not yet establish the compensation
table's full dimensions, start offset, axes, or stride.

## Remaining work

- Identify complete table metadata and independently confirm the Fuel
  Compensation layout; then K, timing, cam, and other requested maps.
- Keep the Windows research decoder separate from a production editing
  interface. Malformed-input handling, tests, source-hash/old-value guards,
  and whole-container verification need explicit coverage before a writer.
- Keep proprietary decoded ROMs/assemblies and key values outside Git.
- No edit-ready ROM was generated. No full repository test gate was run;
  this follow-up changes documentation only.

## 2026-10-04: image map and three candidate tables

Source: the 1 MiB image read from the ECU on 2026-10-04, byte-identical to
the decoded `ZS44A-Tuned-20260920.OsirisROM` (`docs/skunkworks/uprev-backup-20261004.md`).
An annotated map is published as an artifact ("Xterra ECM Flash Map").

**Structure, verified from the bytes:**
- Boot vector table at 0x000000: reset 0x0000261C, stack 0xFFFF7FFC.
- Application vector table at 0x020004: reset 0x00021C24, behind the header
  word 0x1F95FFFF at 0x020000, which the checksum skips.
- Calibration ID `1ZS44A00000` at 0x008204, where the checksum range starts.
- Erased: 0x006000–0x007FFF, 0x012000–0x01FFFF, 0x0DF000–0x0DFFFF and
  0x0E3000–0x0FEFFF.
- The text `UpRev` at 0x0DE194, in a sparse block.
- End values 0x01 at 0x0FFF80 and 0x4E4855F9 at 0x0FFF84 (meaning unknown).

**UpRev map slots:** five slots of 2 KiB at 0x0E0000 + n × 0x800. The Fuel
Targets table sits at slot +0x220 in all five. In the UpRev base ROM all
five copies equal the calibration's own table at 0x00A169. In the tunes only
slot 1 (UpRev's "Map 1") was edited.

**Candidates from the base → k106 diff, inferred, each needing a deliberate
single-change save to confirm:**
- **K at slot +0x762, BE16.** Base 27,089, k106 28,715; the ratio 1.0600
  matches the registry's "K 1.06". It was written to all five slots.
- **Fuel Compensation rpm axis at 0x00CB29.** Sixteen steps, raw × 50. Base
  0x10…0xF0, 0xFF (800–12,750 rpm); k106 0x10…0x90 (800–7,200). This matches
  the k105 note exactly.
- **Fuel Compensation table at ~slot +0x670–0x761.** Its span comes from the
  changed bytes, around the one cell verified earlier (slot 1 +0x75F).
- **Ignition timing (probable) at slot 1 +0x09C–0x0BF.** These cells rose one
  count in k106, whose note says "added spark at 3000–4500 high load". The
  table's bounds and axes are unknown.

The base → k106 diff is 248 bytes in nine regions: the checksums, the axis,
slot 1's timing-like cells, Fuel Targets, Fuel Compensation with K, and K in
slots 2–5. k106 → 20260920 is three bytes: the checksums and 0x0E025C.
