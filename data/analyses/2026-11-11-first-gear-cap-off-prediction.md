# ZS44A-Tuned-20261111-2: first-gear torque cap off — edit, evidence, frozen predictions

Written 2026-11-11, **before any flash**, at the operator's request ("let's add
support for editing those values ... and create a new tune file for it"). The
file is `Tuned ROMs/ZS44A-Tuned-20261111-2.OsirisROM`, built by CANary from
`ZS44A-Tuned-20261111.OsirisROM` (the running tune). It is the second tune file
built on 2026-11-11, hence the `-2`; the name is a build date, not a flash date
(it was first written as `ZS44A-Tuned-20261112` and renamed the same day at the
operator's request, bytes unchanged). If it is flashed, the revision is
recorded as `ZS44A-20261111-2`. The operator reviews it and flashes it; nothing
here wrote to the ECU.

- Container sha256 `3a01904fbd579860889c46465dd75d25d230c1cad8a8e4e412c39e240cdd79e7`
- Image sha256 `108bb32b0156e52c7c4ae6c2fe551bdc995a44302ce1a091a7a27ba8e4e2f1ba`
- Checksum words: sum `0x7E0504CC`, xor `0x18344D4C` (parent `0x0C0604CC`, `0x69CB4D4C`)
- Build script and review file:
  `~/canary-data/analysis-scripts/2026-11-11/build_rom_first_gear.py`,
  `~/canary-data/rom-candidates/2026-11-11-first-gear/review.json`
- Checks: the same build path reproduces UpRev's own `ZS44A-Tuned-20260920.OsirisROM`
  byte for byte from k106; checksum verify; container round trip; the image
  differs from 20261111 at six bytes only (the word and the high bytes of the two
  checksum words), all in the 8 KiB page `0x8000–0x9FFF`.

## What changed

**One calibration word, `0x9308`: 3,584 → 32,767** (`first_gear_ratio_threshold`
in `rom_map.yaml`). It is the transmission ratio (× 1,024, so 3.50) at or above
which the ECM applies its first-gear torque cap. The ECM clamps its ratio estimate
to 4,696, so at 32,767 the comparison is never true and the cap never applies on
account of the gear.

Nothing else: fuel, timing, the map slots, the cap value `0x8EBC` (3,138) and the
axle word `0x91F8` are byte-identical to 20261111.

UpRev's editor shows no field for this word as far as its Tuning Guide goes
(Rev 8, Feb 2022: no first-gear limit, tire or axle setting). Its "ETC Control"
setting is a different thing: it switches all torque management off, makes the
throttle follow the pedal and disables cruise.

## Why

- First-gear full pedal is held to about 40 % throttle from 1,800 to 5,600 rpm by
  a flat torque cap of 3,138 (`docs/ecu-firmware-analysis.md`, "What limits first
  gear"). Against the ECM's own torque map the cap is 90 % of full-load torque at
  2,400 rpm, 87 % at 3,200, 84 % at 4,000, 88 % at 4,800 and 95 % at 5,600.
- **Why the threshold and not the cap value.** The same value also caps torque
  when a state byte (`0xFFFF2208` = 1, 8 or 9) says so, and nobody has identified
  that state. Moving the threshold removes the gear trigger alone.

## What this changes on the road, beyond more torque

- First-gear full pedal becomes a full-throttle condition: it will read the last
  Fuel Targets column (AFR 12.6 at 2,000–3,600 rpm, 12.3 from 4,000) and the
  full-load timing cells, as second gear does now.
- A standing start is covered too: the ratio estimate defaults to 4,696 below
  5 km/h, so the cap applies at launch today and will not after the flash.
- Axle torque in first is already 10.5 % above stock at the same engine torque
  (4.08 against 3.692). This adds up to 16 % at 4,000 rpm.

## Operator's call before flashing

- **KS1 is still open.** Knock control is in its fallback. This adds full-load
  running in first gear with no knock feedback. The 2026-11-10 and 11-11
  write-ups deferred further steps to the harness repair for the same reason.
- **First main-calibration edit.** Every earlier edit was in the map slots. UpRev
  reprograms this page on every flash (the checksum words live in it), so the
  transport is proven; an edit to a word in it has not been watched. Record the
  flash as usual (`docs/skunkworks/uprev-flash-procedure.md`):
  `decode_uprev_flash.py` compares the ECM's post-program CRCs with this file.
- **No map-switch fallback.** The word is outside the five slots. Undoing it is
  flashing `ZS44A-Tuned-20261111.OsirisROM`.

## Baseline (frozen from data on disk)

First gear (rpm ÷ 0x284 wheel word 0.68–0.90), 0x23D pedal ≥ 250, every local
session on disk (first-gear frames from 2026-07-27 on): 8,755 frames, 23 sessions
(`~/canary-data/analysis-scripts/2026-11-11/first_gear_cap_check.py`).

| rpm band | frames | throttle byte, median | frames at ≥ 250 |
|---|---|---|---|
| 2,000–2,200 | 329 | 125 | 0 % |
| 2,400–2,600 | 532 | 99 | 0 % |
| 3,000–3,200 | 570 | 104 | 0 % |
| 3,600–3,800 | 581 | 117 | 0 % |
| 4,000–4,200 | 503 | 110 | 0 % |
| 4,400–4,600 | 397 | 130 | 0 % |
| 5,000–5,200 | 287 | 175 | 0 % |
| 5,400–5,600 | 201 | 203 | 0 % |

On the 2026-11-11 drive the three first-gear pulls read load-indexed targets (raw
134–160) with PID 0x11 throttle 41–73 %.

## Predictions (frozen)

For first-gear full-pedal frames, same selection, after the flash:

1. **Throttle byte ≥ 250 on at least 90 % of frames in every 200 rpm band from
   2,000 to 5,600 rpm** (now 0 %). PID 0x11 reads 88–89 % once a pull is
   established.
2. **The enrichment flag sets:** `mem:ECM_PEDAL_ENRICH_FLAGS` bit 0 = 1, and RAM
   target reads are the last column: 149 below 3,500 rpm, 153 from 4,125.
3. **Second to fifth gear unchanged:** median throttle byte 255 in every band
   2,000–5,500 rpm.
4. **No new trouble code** beyond what a flash normally leaves (readiness reset),
   in particular no P0605, and cruise control still works.
5. **Measured AFR in first-gear pulls follows the last column** inside the 2.5 %
   floor: 12.3 from 4,125 rpm (was 11.8–12.0 above 4,600).

Expected, not scored: 10–16 % more engine torque in first between 2,000 and
5,600 rpm. Road grade and launch technique move first-gear acceleration by more
than that, so one drive cannot score it.

## Falsifiers

- First-gear throttle still 99–117 at 2,400–4,200 rpm: the ratio trigger is not
  what holds it. Read `0xFFFF3C62`, `0xFFFF285F` and `0xFFFF2208` in a pull before
  touching anything else.
- Throttle opens but the flag stays clear, or targets stay load-indexed: the
  switch reading in the firmware note is wrong.
- P0605, a limp-home throttle, or cruise control refusing to set: the ECM
  cross-checks something this word feeds. Flash 20261111 back.

## What was not changed, and why

- **The cap value `0x8EBC`:** shared with the unidentified state trigger.
- **The axle word `0x91F8`:** the regear makes the ECM read every gear about 11 %
  short, but sizing the correction needs a steady fifth-gear read of `0xFFFF27D2`,
  and two of its four readers are untraced. Its own flash, later.
- **Fuel Targets part-pedal cells:** unnecessary if this works, since first gear
  then reads the last column.
