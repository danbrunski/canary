# How UpRev flashes the Xterra ECM, as observed

What UpRev does on the wire when the operator flashes a ROM, from three
recorded flashes: 2026-09-20 (a USB capture of UpRev's cable,
`uprev-flash-20260920.md`), 2026-10-04 (candump) and 2026-11-11 (candump plus
the daemon's K-line sniffer). The three agree step for step. This is the
reference for recording and verifying the next flash, and the groundwork for
CANary doing one itself.

**Flashing stays with UpRev.** Nothing here was sent by CANary, and nothing
here changes AGENTS.md: a non-volatile write needs the operator's explicit
permission for that write, key on, engine off.

## The sequence

Times are the 2026-11-11 flash (UTC); the K-line column is what
`source = 'kline'` holds in session `2026-11-11T173212`.

| When | Where | What |
|---|---|---|
| connected | K-line | UpRev polls DID 0x1103 at about 3.6 Hz the whole time it is connected (2,778 frames from 17:37:51). |
| operator starts the flash | CAN 0x7E0 | Pre-checks (10-04 capture; this one started later): `10 FB`, then `23` reads of 16 bytes at 0x080000, 0x0C0000 and 0x020004, and one at 0x100000 that the ECM refuses (`7F 23 22`, past the end of flash). |
| 17:44:15 | K-line | Polling stops. Four wake-up attempts, the last answered at 17:44:21.5. |
| 17:44:21.6 | K-line | `A0 84 D8 F0 0D` → `E0 84` (unexplained; `A0 34 …` is refused first). `27 03` → a 4-byte seed, `27 04` + 4-byte key → `67 02`. `34 80` → `74 80`. |
| 17:44:21.8 – 28.5 | K-line | The loader: 132 `36` blocks of 32 bytes, numbered 0–131, each acknowledged `76`. 4,224 bytes in 6.7 s. Its sha256 is `ca8f06a0…f959406c`, the same bytes as on 2026-09-20. The ECM's own program stops as the upload begins: its broadcast frames (0x23D and the rest) leave the bus (on 2026-10-04, 6.9 s before the loader's first CAN frame). |
| 17:44:28.58 | K-line | `37` + 3 bytes → `7F 37 78` (pending) → `77`. `BF 00` → `FF 00`, `BF 01` → `FF 01`. The loader is now running. |
| 17:44:28.63 | CAN 0x742 / 0x762 | `04` → `04 00 00 26 1C`; `65` → `65`. |
| 17:44:28.65 – 29.20 | CAN | Sixteen range CRCs (`20`) over the whole image: eight 4 KiB spans, 0x8000–0x1FFFF, then seven 128 KiB spans. All sixteen matched the image that was running. |
| 17:44:29.21 – 30.13 | CAN | Erase sector 15 (0xE0000–0xFFFFF, 0.28 s), then sector 8 (0x8000–0x1FFFF, 0.64 s). Only those two, every time. |
| 17:44:30.15 – 33.63 | CAN, data on 0x743 | Eight pages of 8 KiB. Per page: `64`, 1,024 raw frames on 0x743 in 0.38 s, `22` → the loader's CRC-32 of what it received, `55 <address>` → programmed. Pages 0x8000, 0xA000, 0xC000, 0xE000, 0x10000, 0xE0000, 0xE2000, 0xFE000. The other twenty pages of the two sectors are blank (0xFF) and are not sent. |
| 17:44:33.64 – 34.19 | CAN | The same sixteen range CRCs again. All sixteen matched the new image. **The last reply is the flash boundary: 17:44:34.185467Z.** |
| 17:44:34.19 | CAN | `67` + the CRC-32 of the whole 1 MiB image → `67 00` after 0.45 s. The ECM's program restarts and its broadcast frames return. |
| 17:44:35.7 – 42.1 | CAN 0x7E0 | UpRev asks the operator to switch the ignition off and polls `10 85` every 78 ms until the ECM stops answering. |
| 17:44:42.1 – 53.5 | — | Ignition off (ECM silent for 11 s). UpRev waits, then asks for ignition on. |
| 17:44:53.5 | CAN 0x7E0 | ECM back. `10 FB`, `10 C0`, then `14 FF 00` → `54 FF 00`: **UpRev clears the ECM's trouble codes.** |
| 17:44:58.4 | CAN 0x742 | `66`, no reply (nothing is listening by then). |
| 17:45:01 | CAN 0x7E0, then K-line | Identification again (`21 10` reads `1ZS44A00000`), and K-line polling resumes. |

From the first K-line wake-up to the last CRC is 13 seconds; flash memory is
being erased or written for 4.4 of them.

## What a flash resets

- Long-term fuel trim, back to 0.00.
- The readiness monitors and stored codes, by UpRev's own `14 FF 00` after the
  key cycle. With any monitor incomplete the MIL blinks for 10 s at key-on
  (FSM EC-52), which is what the operator saw on 2026-11-11. It is not a fault.
- The calibration id does not change (`1ZS44A00000`), so it cannot be used to
  tell whether a flash happened.

## Side effects on the bus

- Another module answers on 0x763 to a few of the 0x743 data frames that happen
  to parse as a diagnostic request addressed to it (`7F xx 11`, service not supported):
  five on 2026-11-11, nine on 2026-10-04. Harmless.
- The ECM broadcasts nothing for about 13 s. Other modules may log a lost-
  communication code for it.

## Recording the next flash

1. Pi on the OBD port, key on, engine off. Arm passive mode
   (`POST /control/passive {"enabled": true}`), so CANary's own polling is off
   the bus. The daemon keeps listening on CAN and K-line.
2. **Start candump before UpRev's flash button, not after:**
   `nohup candump -L can0,0:0,#FFFFFFFF > /data/canary/flash-captures/flash-$(date -u +%Y%m%dT%H%M%SZ).candump.log &`
   (`-e` cannot be combined with `-L`). On 2026-11-11 it started 2.5 s into
   the loader upload. That lost nothing on CAN, but the pre-checks on
   0x7E0 were missed.
3. Let the flash, the ignition-off and the ignition-on finish. Stop candump
   after UpRev shows the ECU identification again, about 30 s after "done".
4. Verify on the laptop:
   `.venv/bin/python scripts/decode_uprev_flash.py CAPTURE.log --old <running>.OsirisROM --new <flashed>.OsirisROM --json evidence.json`.
   `verified` means the post-program CRCs cover the whole image and all match
   the new one. The printed `boundary` is `flashed_at`.
5. The K-line half needs nothing extra: it is in the session that was live,
   as `source = 'kline'`, `channel = 'FRAME'`.
6. Record the revision (`tune_revisions.yaml`, `afr_calibration.yaml`,
   `ecu_application_model.yaml`), disarm passive, re-arm capture autostart.

The decoder was checked against two flashes whose answers were already on
record: it reproduces the 2026-10-04 boundary to the microsecond
(23:02:33.183902Z) with 16/16, 8/8 and 16/16, and refuses the 2026-11-11
capture when handed the wrong image.

## What CANary would still need to flash by itself

Everything UpRev sends is now on record, on both buses. What is missing is
understanding, not observation:

- **The security key.** One seed and key pair per flash has been seen. The
  algorithm that turns a seed into a key has not been worked out.
- **The loader.** The 4,224 bytes UpRev uploads are UpRev's program. Running
  the same sequence means either sending their loader or writing one.
- **Three commands nobody has explained:** K-line `A0 84 D8 F0 0D` and `BF 00`
  / `BF 01`, and the loader's `65`.
- **Which sectors a different edit needs.** Every flash so far touched sectors
  8 and 15 only, because every edit was in the map slots (0xE0000) and moved
  the checksum words (0x9598, 0x95A0). An edit anywhere else has never been
  watched.

The seed, the key and the loader bytes are deliberately not in this repo. They
are in the session and capture files on the Pi and the laptop.

## Evidence

| Flash | Capture | sha256 |
|---|---|---|
| 2026-09-20 | USB capture of UpRev's cable | `99fbd62b…f06e56` (`uprev-flash-20260920-evidence.json`) |
| 2026-10-04 | `/data/canary/re/uprev-flash-20261004/can0-20261004T230137Z.log` | `b6dc201e…699e991` |
| 2026-11-11 | `/data/canary/flash-captures/flash-20261111T174424Z.candump.log`; K-line in session `2026-11-11T173212` | `33a2974c…48b77331` (`uprev-flash-20261111-evidence.json`) |
