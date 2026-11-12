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

## Result: flashed 2026-11-12T01:02:19.999390Z, first drive scored the same evening

**Flash.** Recorded and verified from the capture
(`docs/skunkworks/uprev-flash-20261111-2-evidence.json`): 16/16 range CRCs
before programming match 20261111, all 8 uploaded pages equal this image, only
page `0x8000` differs from the old one, 16/16 range CRCs after programming match
it. Revision `ZS44A-20261111-2` in `tune_revisions.yaml`.

**Drive.** `_capture-tuning-2026-11-12T010305`, 494 s, a few minutes around the
block from a 24 °C coolant start (68 °C at the pulls). Scripts:
`~/canary-data/analysis-scripts/2026-11-12/score_first_gear.py`,
`first_gear_partpedal.py`, `pull_reads.py`.

**The frozen selection is empty for first gear, because the regime was not
driven:** the pedal byte never reached 250 in first gear (maximum 230). The one
full-pedal moment of the drive was in second gear. So predictions 1, 2 and 5 are
not scored. That is a statement about the drive, not about the edit.

| Prediction | Verdict | What the drive shows |
|---|---|---|
| 1. First gear, pedal ≥ 250: throttle byte ≥ 250 on ≥ 90 % of frames, 2,000–5,600 rpm | **not scored** (no frames) | See the matched-pedal table below: the falsifier (throttle still 99–117) did not occur |
| 2. Enrichment flag sets, targets are the last column | **not scored** | No first-gear read above the switch. `mem:ECM_PEDAL_ENRICH_FLAGS` is not in the `tuning` profile, which the prediction overlooked; the last-column read is the usable evidence next time |
| 3. Second to fifth gear unchanged | **consistent, thin** | One full-pedal moment in second (104 frames, 1.1 s, 3,065–3,569 rpm): throttle byte median 255, ≥ 250 on 100 % of frames at 3,200–3,600 rpm (tip-in at 3,000–3,200: 64 %, minimum 244). RAM target raw 149 at 3,372 rpm, the last column |
| 4. No new trouble code, cruise works | **codes: held; cruise: not tested** | No stored or pending code, MIL off, at 01:03:49Z and 01:07:39Z |
| 5. First-gear AFR follows the last column | **not scored** | No first-gear read above the switch |

**What the drive does show (not a frozen prediction; same frame selection, pedal
below 250).** First gear (rpm ÷ wheel word 0.68–0.90), per 0x23D frame, the
throttle byte at the same pedal byte and rpm, this drive against six earlier
tuning captures (2026-10-26 to 2026-11-11, 13,842 first-gear frames):

| pedal byte | rpm | frames new / old | throttle byte new: p10 / median / max | old: p10 / median / max |
|---|---|---|---|---|
| 220–250 | 3,000–3,600 | 26 / 183 | 218 / 222 / 225 | 104 / 107 / 120 |
| 220–250 | 3,600–4,200 | 21 / 133 | 220 / 224 / 225 | 108 / 111 / 120 |
| 180–220 | 3,000–3,600 | 29 / 158 | 186 / 200 / 214 | 104 / 105 / 119 |
| 180–220 | 3,600–4,200 | 28 / 58 | 207 / 208 / 217 | 110 / 116 / 120 |
| 180–220 | 4,200–4,800 | 29 / 75 | 188 / 207 / 209 | 115 / 128 / 151 |
| 140–180 | 2,400–3,000 | 51 / 251 | 136 / 142 / 172 | 98 / 101 / 104 |
| 60–100 | 2,400–3,000 | 124 / 976 | 85 / 85 / 88 | 57 / 75 / 97 |

Above pedal byte ~140 the old throttle sat on the cap whatever the pedal did;
the new throttle follows the pedal, about 5 counts under it (pedal 227 →
throttle 222). The distributions do not overlap in any cell from pedal 140 up.
Below the cap (pedal 60–100) old and new overlap, as they should. PID 0x11 read
73.7 % at pedal 215 and 79.6 % at pedal 228 in first gear (41–73 % at pedal 255
on the 2026-11-11 drive).

**Limits.** All new first-gear frames above pedal 180 come from one pull
(250.8–252.3 s, 2,890–4,560 rpm), on a part-warm engine. Nothing in first gear
below 2,400 or above 4,800 rpm at high pedal, and no standing-start launch at
full pedal. The cells are the same pedal and rpm on both sides, so the
comparison does not depend on how the drives differed, but it is one pull.

**Verdict: keep.** The edit did what the firmware reading said the word does,
and nothing it could have broken shows. Still owed, on any later drive: one
first-gear pull with the pedal on the floor from ~2,000 rpm (scores 1 and 5,
and 2 through the last-column target read), and setting cruise once (4).

## Second drive, 2026-11-12 03:42Z: the full-pedal predictions scored

`_capture-tuning-2026-11-12T034249`, 1,240 s, warm (coolant 84–92 °C at the
pulls), 81–82 kPa. The Pi had powered off while parked (UPS grace after input
power dropped at 03:13:35Z), so the first ~6 minutes of the drive are not
recorded. Scripts in `~/canary-data/analysis-scripts/2026-11-12/`:
`score_first_gear.py`, `first_gear_fullpedal.py`, `upper_gear_pedal.py`.

Four first-gear full-pedal pulls (frozen selection: pedal byte ≥ 250, rpm ÷
wheel word 0.68–0.90): 699 frames, 1,575–5,569 rpm, rpm rising 1,115–1,314 rpm/s.

| Prediction | Verdict | What the drive shows |
|---|---|---|
| 1. Throttle byte ≥ 250 on ≥ 90 % of frames in every 200 rpm band, 2,000–5,600 | **Held in substance; missed to the letter in 2 of 18 bands** | 687 of 699 frames at ≥ 250, median 255 in every band from 1,600 to 5,600 (baseline: 0 % from 2,000 to 5,600). The 12 other frames are the first 0.04 s of a pull (11) or its last frame (1), throttle 219–249. They put 2,200–2,400 at 83 % (18 frames) and 3,400–3,600 at 89.8 % (49 frames); every other band is 97.7–100 %. PID 0x11 reads 87.8–89.4 % in the pulls |
| 2. Targets are the last column | **Held on 12 of 14 reads, one unexplained** | RAM target at first-gear full pedal: 149 at 2,122 / 2,441 / 2,794 / 3,053 / 3,466 rpm, 152 at 3,737, 153 at 4,069 / 4,094 / 4,372 / 4,622 / 4,716 / 5,281. Exceptions: 139 at 3,509 rpm 0.04 s after tip-in (PID 0x11 68.6 %, still opening), and **160 at 5,172 rpm in the first pull** (lookup 159; throttle byte 255, pedal 255 for another 0.08 s). That one read is the load-indexed cell, with the throttle fully open. The flag channel is not in the profile, so the flag itself was not read |
| 3. Second to fifth gear unchanged | **Held; the frozen statistic was the wrong one** | At pedal byte 255 in upper gears the throttle byte is ≥ 250 on 99.7 % of 759 frames (98.1–99.4 % in five earlier captures). The band medians dip to 246 and 250 at 3,200 and 3,800 rpm because this drive has 239 frames at pedal 250–254, where the throttle sits 5 counts under the pedal on every drive, before and after. A band median over "pedal ≥ 250" moves with how hard the pedal was held, not with the tune |
| 4. No new code, cruise works | **Codes held; cruise not reported** | No stored or pending code, MIL off, at 03:52Z and 04:02Z |
| 5. First-gear AFR follows the last column inside 2.5 % | **Direction held, magnitude just outside** | PID 0x24 × 14.7 at least 0.45 s into a first-gear pull, against the RAM target at that instant: 12.52 / 12.67 / 12.76 / 12.63 / 12.62 at 4,153–5,525 rpm against 12.30 (+1.8, +3.0, +3.7, +2.7, +2.6 %), and 12.98 / 13.02 / 12.97 at 2,509–3,363 rpm against 12.63 (+2.7 to +3.1 %). It was 11.8–12.0 above 4,600 rpm before. Upper gears on the same drive: 12.43–12.63 against 12.30 (+1.1 to +2.7 %) |

**What stands.** The cap was the gear trigger, and moving the threshold removed
it: first gear at full pedal is a full-throttle regime, reads the last Fuel
Targets column, and runs about AFR 12.5–12.8 from 4,100 rpm where it ran
11.8–12.0. Upper gears are unchanged at matched pedal. No code.

**What is open.**

- **One load-indexed read at full throttle** (160 at 5,172 rpm, 0.9 s into a
  pull that began at 4,190 rpm). The same rpm in the next pull read 153. One
  read in fourteen; not explained. Logging the flag byte
  (`mem:ECM_PEDAL_ENRICH_FLAGS`, `0xFFFF2841`) in the `tuning` profile would
  say whether the flag dropped.
- **First gear reads 2.6–3.7 % lean of its target, upper gears 1–2.7 %.** rpm
  rises 1,100–1,300 rpm/s in first, three to five times the upper-gear pulls,
  so a delivery lag on rising airflow and the sensor's own lag both push the
  same way. Seven reads over four pulls; not separated. AFR 12.5–13.0 at full
  load is not a lean condition, but it is leaner than the 12.3 the table asks
  for, with knock control still in fallback.
- **Cruise control** has not been reported.
- **Statistics to freeze next time:** exclude the first 0.05 s after the pedal
  crosses the threshold, and select on pedal = 255 (or compare at matched pedal
  byte), not pedal ≥ 250.

**Verdict: keep.**
