# UpRev flash capture: selective programming and ROM address verification

2026-09-20. Offline analysis of the operator's completed flash. No commands
were sent to the cable/ECU, no ROM was modified, and no flasher was built.
Flash implementation remains outside CANary, under the separate bench-only
`canary-rw` boundary described in `ecu-rw-bench-research.md`.

## Main findings

1. The new ROM changes exactly one calibration byte, Fuel Targets Map 1 at
   decoded offset **0xE025C**, plus the two internal checksum words.
2. **Eight captured 8192-byte raw USB uploads exactly match the decoded new
   ROM** at their subsequent programming addresses. These are clear ROM
   bytes, not OsirisROM container bytes or its masked/encrypted ROMData.
3. Sixteen ECU-reported range CRCs partition the full **0x000000–0x0FFFFF**
   flash image. All sixteen before programming match the old tune; all
   sixteen afterward match the new tune. This is CRC-based verification,
   not a complete byte-for-byte ECU readback.
4. Only two erase regions changed: **0x008000–0x01FFFF** and
   **0x0E0000–0x0FFFFF**. UpRev erased them, wrote their eight nonblank
   8-KiB pages, and skipped twenty all-FF pages. Applying those operations
   to the old ROM **in memory only** reconstructs the new ROM exactly.
5. This is a **CAN programming path**, despite the earlier K-line logging
   research: the host configures CAN identifiers 0x7E0/0x7E8, then switches
   to the flash stub's 0x742/0x762 pair. USB captures show cable commands,
   not individual physical CAN frames or their bus-level timing.

## Evidence identity

Capture: Windows workspace `outputs/uprev-flash.pcapng`, 5,689,060 bytes,
89,914 packets, duration 76.629122 seconds. SHA-256:
`99fbd62b7e07938e8ce0fdb87400352bd3756bee8fe1c3b063dc17b80ff06e56`.

Flashed file, as identified by the operator:
`C:\Users\thebo\Desktop\Uprev\Tuned ROMs\ZS44A-Tuned-20260920.OsirisROM`.

- Container SHA-256: `03d00051a59b86251ac0d5d9cf2ac9651682cfcba978edce81f07371f1c128a2`.
- Decoded SHA-256: `a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50`.
- Decoded length: 1,048,576 bytes; whole-image CRC32: `95b993c5`.
- Baseline: `ZS44A-Tuned.OsirisROM`, container SHA-256
  `c5325776b08c3084edd49d8f68587dad4668e02b0e77c7686f7548fb6f7c8fa9`.

All four container-section CRCs and exact outer/inner codec roundtrips
passed. The adjacent `uprev-flash-20260920-evidence.json` contains frame
references, page hashes, all range CRCs, and validation results, without
ROM payloads, uploaded executable bytes, cable identifiers, or keys.
Original capture and private analysis artifacts remain outside Git.

## Fuel Targets edit and checksums

The operator changed Map 1 Fuel Target, BFS **18.20** / **5200 RPM**,
display **11.61 → 11.54**. Differences from the original decoded tune:

| Offset | Old | New | Role |
|---|---|---|---|
| 0x009598 | 0C | 0D | High byte of XOR checksum word |
| 0x0095A0 | F4 | F5 | High byte of sum checksum word |
| 0x0E025C | A2 | A3 | Target AFR raw count, 162 → 163 |

`AFR = 14.7 * 128 / raw`: 11.6148148 → 11.5435583. Thus increasing this
raw value requests a lower AFR; the byte is not AFR scaled linearly.
This edit does **not** include the earlier Fuel Compensation test change:
offset 0xE075F is unchanged from the original tune.

The independent full-table verification in
`../uprev-rom-table-verification.md` is corroborated by this capture:

- Map 1 Fuel Targets: 64 bytes at **0xE0220–0xE025F**, row-major,
  eight RPM rows by eight BFS columns.
- Edited row 7, column 4: `0xE0220 + 7*8 + 4 = 0xE025C`.
- Transcribed RPM-axis byte sequence: **0xCAA5–0xCAAC**.
- Transcribed BFS-axis byte sequence: **0xC894–0xC89B**.

Both axis sequences and the original full table have unique matches in
the original decoded image. The axes are separate from the table, not
an assumed adjacent header. Pointer/reference metadata is still unparsed.
These are now corroborated **flash programming addresses**, since the
containing upload pages exactly match ROM offsets; they are not proof of
RAM mirror addresses or live-read permissions. BFS scaling remains the
existing editor transcription, not a universal approximate multiplier.

The documented Nissan style-1 checksum calculation independently passes
for the new ROM: aligned BE32 words from 0x8204 to end, excluding 0x20000,
0x9598, 0x95A0. Sum modulo 2^32 = **F538FCAA**; XOR = **0D3BBAC0**.
Old values are F438FCAA and 0C3BBAC0. The changed byte is the high byte
of its aligned word, explaining the 0x01000000 checksum changes.

## Observed flash timeline

Times are seconds relative to capture start. Frame numbers are Wireshark
frame numbers, not UPRV message counters. A reassembled message's frame
reference is the frame containing its first byte.

| Relative time | Frames / evidence | Observed phase |
|---|---|---|
| 0.252–20.9 | Starts with `02002211030401` polls | Existing connection; capture does not include the initial cable open |
| 21.049–21.716 | 22129–23179 | CAN setup, diagnostic session FB, ROM-size/location probes |
| 21.716–24.868 | 23193–26423 | Cable/session transitions and A0 exchanges; loader preparation |
| 24.882–31.689 | 26451–35361 | Download setup, 132 × 32-byte stub chunks, transfer exit |
| 31.690–31.752 | 35375–35517 | BF transitions, CAN IDs switch to 742/762, stub initialization |
| 31.758–32.307 | 35539–36327 | Sixteen pre-flash region CRC checks |
| 32.322–33.442 | 36355–37487 | Erase sector 15, then sector 8; both acknowledged |
| 33.450–36.95 | 37507–415xx | Eight buffer-reset/upload/CRC/program cycles |
| 36.958–37.505 | 41567–42353 | Sixteen post-flash region CRC checks |
| 37.509–37.960 | 42370–42821 | Soft reset with whole-ROM CRC32 parameter 95B993C5 |
| 38.980–61.12 | Repeated cable/session checks | Reconnect/checking phase; physical ignition actions cannot be inferred from USB alone |
| 61.129–64.017 | 73517–76389 | Hard-reset command followed by cable error/status 7F04 |
| 64.027 onward | 76461/76471, 76757/76767 | Normal ECU identification responds again: `1ZS44A00000` |
| 64.3–72.45 | Diagnostic and A1/A2 exchanges | Follow-up capability/metadata checks, including negative replies |
| 72.460–76.398 | 85543 onward | Original polling request/response resumes |

The 7F04 response after reset and earlier negative diagnostic responses
must not be classified as an overall failed flash: all post-program CRCs
match and ordinary ECU identification/polling resumes. Nor does this
capture prove engine operation, road behavior, or persistence after an
independently observed power cycle.

### Stub transfer versus ROM transfer

The `020036` sequence consists of **132 consecutively numbered chunks**,
each carrying 32 bytes: **4224 bytes total**, roughly 6.8 seconds. The
payload layout observed is `02 00 36 seq_BE16 20 [32 bytes]`; all 132
receive `66 01 76 77`. Transfer setup is `02003480`; transfer exit begins
`02003765ac`, followed by an intermediate response and final completion.
These bytes are not the calibration image: the subsequent independent
raw uploads are the ROM data. The captured library names the relevant
path `flashUploadStubCANRoutine`. Stub load/execution RAM address and
instruction-level behavior were not recovered; no executable is copied
into repository fixtures.

## Flash memory geometry and selective update

All endpoints below are inclusive. Range-CRC requests encode a BE32
address and BE24 length; the reported CRC matches `zlib.crc32` over the
corresponding decoded ROM bytes.

| Region(s) | Size each | Observation |
|---|---:|---|
| 0x000000–0x007FFF, partitioned at 0x1000 intervals | 4 KiB × 8 | All eight CRCs unchanged; no programming observed |
| 0x008000–0x01FFFF | 96 KiB | Sector 8 erased; checksum words here changed |
| 0x020000–0x0DFFFF, partitioned at 0x20000 intervals | 128 KiB × 6 | All six CRCs unchanged; no programming observed |
| 0x0E0000–0x0FFFFF | 128 KiB | Sector 15 erased; fuel target byte here changed |

This is the observed loader's geometry for this 1-MiB ROM, not a complete
MCU memory map. It does not establish an exact MCU part number, RAM size,
peripheral map, EEPROM location, or a recovery procedure.

Changed region CRCs:

| Region | Before (old ROM) | After (new ROM) |
|---|---|---|
| 0x8000, length 0x18000 | 9771656F | 44AC71EC |
| 0xE0000, length 0x20000 | CFC53D89 | 4690EB8B |

Programmed pages, in order, each 8192 bytes. Every buffer CRC response
also matches the CRC of the raw USB upload before programming:

| Address | USB data frame | Program-command frame | Page CRC32 | Same as old tune? |
|---|---:|---:|---|---|
| 0x008000 | 37537 | 37949 | 2096AFFF | No: checksum changes |
| 0x00A000 | 38047 | 38459 | 6BF20083 | Yes |
| 0x00C000 | 38557 | 38973 | 8F933B10 | Yes |
| 0x00E000 | 39083 | 39501 | 4ED0893D | Yes |
| 0x010000 | 39603 | 40021 | 3D200413 | Yes |
| 0x0E0000 | 40111 | 40515 | 9531F15A | No: target AFR change |
| 0x0E2000 | 40607 | 41027 | 3A17BCDD | Yes |
| 0x0FE000 | 41099 | 41523 | 43386275 | Yes |

Total erased extent: **224 KiB**. Total ROM data uploaded/programmed:
**64 KiB**. Twenty omitted pages (160 KiB) are entirely FF in the new ROM:
seven in sector 8 and thirteen in sector 15. Static inspection of
`FlashMachine.turboFlashRoutine` confirms it checks 8192-byte pages for
all-FF before deciding whether to upload/program them. Thus omission is
not compression, missing USB traffic, or evidence that only changed bytes
were written. Six unchanged but nonblank pages had to be restored after
their containing sectors were erased.

### Memory reads independently tie addresses to image offsets

The pre-flash `02 00 23 [BE32 address] [BE16 length]` probes return:

- 0x80000, 16 bytes: frames 22229/22241, exact original-ROM match.
- 0xC0000, 16 bytes: frames 22355/22365, exact original-ROM match.
- 0x100000, 16 bytes: frames 22481/22487, negative `7F 23 22`.
- 0x20004, 16 bytes: frames 23169/23179, exact original-ROM match.

The 0x100000 rejection is consistent with the end of this image, but
does not prove that all memory beyond it is unmapped: session restrictions
or other address spaces can also produce rejection. Other 0x23 forms later
receive negative replies, so service availability is state-dependent.
No full ECU dump or table-specific live read was performed in this task.

## USB/cable protocol lessons

USBPcap device address 4 carries bulk OUT endpoint 0x02 and IN endpoint
0x81. Strip the first two FTDI status bytes from each IN USB packet.
In this capture all IN payload-bearing completions are at most 50 bytes,
so none needs multi-packet splitting. This does not remove the general
requirement to strip status bytes at **each** USB packet boundary.

Reassembled UPRV records have `UPRV`, BE16 payload length, four reserved
header bytes, BE16 additive payload checksum, then payload. OUT header
and body commonly arrive in separate USB transfers. The framed parser
finds **856 OUT and 862 IN messages, with zero bad checksums or tails**.

**Important parser trap:** a UPRV-only logging parser reports **65,552
discarded OUT bytes** during this flash. All are accounted for: eight
8192-byte raw uploads plus sixteen one-byte `01` writes (two after each
upload). These are outside UPRV framing. Never silently discard them in
a flash analysis or interpret that discard count as capture corruption.
Raw data may itself contain the bytes `UPRV`; a future generic analyzer
must use protocol state/expected lengths, not magic-byte searching.

Observed command meanings, supported by the captured CableInterface
library, not solely guessed from opcode numbers:

| UPRV payload pattern | Meaning/evidence |
|---|---|
| `03 07E0 07E8` / `03 0742 0762` | CAN ID configuration; `TurnOnCan` |
| `89 0742 04 ...` | Stub identification query; response contains 0x261C |
| `89 0742 65 ...` | Turbo-flash initialization (`InitTurboFlash`) |
| `89 0742 20 address_BE32 length_BE24` | Range CRC; all 32 replies independently verified |
| `89 0742 50 sector ...` | `EraseFlashBlock`; sectors 0F then 08 |
| `89 0742 64 ...` | `ResetTurboFlashBuffer` |
| `88 0743 04 00` | `SendFlashRAW` setup for the following 8192 raw bytes |
| `89 0742 22 ...` | Buffer CRC, `GetFlashCRC_2`; eight exact matches |
| `89 0742 55 address_BE32 000000` | `ProgramFlashBlockLarge` |
| `89 0742 67 crc_BE32 000000` | `softResetTurboFlash`; argument matches whole-ROM CRC |
| `89 0742 66 ...` | `hardResetTurboFlash` |

These are descriptive wire observations, **not a safe replay recipe**.
Cable-internal CAN segmentation, bus timing, loader security, prerequisite
state, recovery behavior, and unsupported ECU variants are not established.
The A0 exchanges are not decoded authentication semantics. No generic
seed/key algorithm or licensing behavior is inferred.

USB control setup records include 5136 vendor requests with bmRequestType
0x40, request 0, value 1, index 0, length 0, plus standard descriptor/
configuration requests. No baud-rate/line-format setup is present in the
observed setup records; do not treat this as a complete cold-open transcript.

## Reproduction and handoff

The private Windows workspace is
`C:\Users\thebo\Documents\Codex\2026-09-20\i-hav` (WSL equivalent under
`/mnt/c/Users/thebo/Documents/Codex/2026-09-20/i-hav`). Local research scripts:

- `work/analyze_flash.py`: tshark extraction, framed timeline and private
  message/USB exports; reports out-of-frame bytes rather than treating the
  initial framed parse as complete.
- `work/correlate_flash.py`: decoded-ROM comparison, raw-page association,
  range CRC checks, blank-page checks and in-memory reconstruction.
- `work/audit_flash_evidence.py`: checks all page/range CRCs and internal
  checksums, writes the payload-free JSON evidence manifest.

The scripts are capture-specific local research aids, not production
collectors or generic flash parsers. They do not transmit. Run them in that
order with Windows Python and installed tshark; no new live capture is
needed to reproduce this report. The existing repository decoder remains
`tools/decode_uprev_rom_research.py`. No full repository gate was run.

Loaded-code corroboration uses the already captured editor image:
embedded CableInterface assembly at image offset 0x6D567F, method rows
59 (erase), 82 (hard reset), 83 (init), 90 (program large), 100 (buffer
reset), 101 (raw upload), 115 (soft reset), 124 (upload plus buffer CRC),
342 (stub CAN upload), 358 (selective flash/all-FF skip). No routines were
executed and no proprietary assembly or stub payload is committed.

Useful next work is offline: pin this new tune's provenance as a new
revision, map remaining tables, and add read-only extraction tests using
small synthetic fixtures. Do not silently replace the old tune's table
reference or historical calibration: its original hash remains valid for
older sessions. The operator's completed flash plus this capture establish
a new revision; changing CANary's active tune configuration is a separate
integration step. No flash replay fixtures or write path were added.
