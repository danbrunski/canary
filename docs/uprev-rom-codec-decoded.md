# UpRev ROM files (`.OsirisROM`)

CANary loads UpRev's `.OsirisROM` tune files. Loading one yields the plain
1 MiB flash image the file carries, and that image is what the ROM library,
the ROM editor, the twin and the offline tools work on. It is the same image
the ECU holds: the 2026-09-20 tune, loaded from
`ZS44A-Tuned-20260920.OsirisROM`, is byte-identical to the flash read from
the ECU on 2026-10-04 (sha256 `a46baa24…be50`, see
`docs/skunkworks/uprev-backup-20261004.md`).

## What loads

| File | Result |
|---|---|
| `.OsirisROM` | Loaded into its image. The file's own integrity checks must pass, and the image must be the vehicle's size. |
| Plain image of the vehicle's exact size (1,048,576 bytes on the Xterra) | Loaded as is. |
| UpRev ROM backup `.bin` | Refused by name. Use the `.OsirisROM` of the same tune, or back up the ECU from CANary. |
| Anything else | Refused. |

`src.analysis.rom_image.load_image` decides which of these a file is, and
`src.analysis.osiris` reads and writes the `.OsirisROM` format. A loaded image
is then checked against the vehicle's ROM map: its calibration ID, its two
internal checksum words, and its revision in `tune_revisions.yaml`.

## Where it is used

- **ROM library and ROM editor:** upload or open an `.OsirisROM` in the
  dashboard (`docs/offline-rom-targets.md`), `POST /control/rom/library`, and
  `POST /control/rom/inspect` for a look without storing
  (`docs/headless-api.md`).
- **The desk:** `--rom-dir` / `$CANARY_ROM_DIRS` adds a folder's `.OsirisROM`
  and plain images to the ROM library (`README.md`).
- **Offline tools:** `scripts/rom_targets.py` (export and patch Fuel Targets),
  `tools/rom_sim.py`, and the fuel-comp tools take an `.OsirisROM` path
  directly.

## Writing an edited file

`scripts/rom_targets.py apply` writes an edited `candidate.OsirisROM` into a
review bundle outside the repository (`docs/offline-rom-targets.md`). Only the
requested cells and the image's two checksum words change, and everything
else in the file is kept as it was. Reproducing the operator's own 2026-09-20
edit this way gave a file byte-identical to the one UpRev saved (sha256
`03d00051…28a2`). Review a candidate in UpRev before it goes anywhere near
the ECU. Flashing stays with UpRev.

## Image checksums

The image uses Nissan checksum style 1. Over the aligned BE32 words from
`0x8204` to the end of the image, excluding the words at `0x20000`, `0x9598`
and `0x95A0`, the modulo-2³² sum is stored at `0x95A0` and the XOR at
`0x9598`. The ROM map declares these (`checksums` in `rom_map.yaml`), and an
edit recomputes both.

## Rules

- ROM files and images stay out of Git, as do any files from UpRev's
  software. Tests use synthetic fixtures, or skip with a named reason when
  the operator's private image is not on the machine.
- Loading or editing a file writes nothing to the ECU. A flash is a
  non-volatile write and follows AGENTS.md: explicit operator permission for
  that write, key on, engine off.
