# Fuel Targets verification and the image layout

How the ROM map's Fuel Targets entry was checked against an independent
answer, and what the 1 MiB image holds. Loading `.OsirisROM` files is in
`docs/uprev-rom-codec-decoded.md`.

## Fuel Targets, against an independent transcription

The known answer is the Fuel Targets table transcribed by hand from UpRev's
editor into `data/nissan-xterra-2008/afr_calibration.yaml`, bound there to
the `ZS44A-Tuned.OsirisROM` file (sha256 `c5325776…fa9`). It was not
generated from the image. Searching the whole image loaded from that file:

| Reference sequence | Image offset | Matches |
|---|---|---:|
| All 64 Fuel Targets bytes, 8 rpm rows × 8 BFS columns, row-major | `0xE0220` | 1 |
| The same table transposed (column-major) | none | 0 |
| Raw rpm axis `18 20 28 38 48 50 58 68` | `0xCAA5` | 1 |
| Raw BFS axis `34 44 5d 64 6c 6e 7f 80` | `0xC894` | 1 |

So the table sits at `0xE0220..0xE025F`, row-major, as expected. It decodes
as `AFR = 14.7 × 128 / raw`, and the rpm breakpoints are `raw × 50`. The BFS
breakpoints keep the editor transcription's engineering values; don't swap
in an approximate universal scale.

The `0xE075F` Fuel Compensation cell in
`docs/uprev-rom-container-research.md` is a different table.

## Image layout (2026-10-04)

From the 1 MiB image read from the ECU on 2026-10-04, byte-identical to the
image in `ZS44A-Tuned-20260920.OsirisROM`. The ROM map
(`data/nissan-xterra-2008/rom_map.yaml`) is the maintained form of this, and
an annotated map is published as an artifact ("Xterra ECM Flash Map").

- Boot vector table at `0x000000`: reset `0x0000261C`, stack `0xFFFF7FFC`.
- Application vector table at `0x020004`: reset `0x00021C24`, behind the
  header word `0x1F95FFFF` at `0x020000`, which the checksum skips.
- Calibration ID `1ZS44A00000` at `0x008204`, where the checksum range starts.
- Erased: `0x006000–0x007FFF` (except 48 bytes at `0x006EFE–0x006F2D`),
  `0x012000–0x01FFFF`, `0x0DF000–0x0DFFFF` and `0x0E3000–0x0FEFFF`.
- UpRev's additions: the block at `0x0DE000–0x0DEFFF` (data to `0x0DE47F`)
  and the map sector `0x0E0000–0x0FFFFF` (`added_by_uprev` in the map).
- End values `0x01` at `0x0FFF80` and `0x4E4855F9` at `0x0FFF84` (meaning
  unknown).

**Map slots.** An UpRev tune holds five 2 KiB map slots at
`0x0E0000 + (n − 1) × 0x800`, UpRev's Map 1 to Map 5. Fuel Targets sits at
slot `+0x220` in all five. In the UpRev base image all five copies equal the
calibration's own table at `0x00A169`; in the tunes only Map 1 was edited.
The other slot tables (Fuel Compensation, K, the timing trims) and how each
was confirmed are in `docs/ecu-firmware-analysis.md` → "Map-slot tables" and
in `rom_map.yaml`.
