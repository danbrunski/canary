# Scan-tool session, 2026-09-24: BCM TPMS IDs over CAN, and the ECM "All signals" screen

**Status: observations and hypotheses, not bindings.** The companion ABS note
is [2026-09-24-abs-kline-scan-tool.md](2026-09-24-abs-kline-scan-tool.md),
which covers the ABS live data, the steering-angle calibration, and the lamp
and switch bits.

## Setup

- 2008 Xterra. The Pi ran `a28459f`, then `6892c2c` from 16:53:24Z.
- CANary was in passive mode (no CAN transmit), except for about 13 s after a
  restart (see "CANary's own traffic").
- The K-line sniffer was listen-only; broadcast and diagnostic CAN were
  recorded.
- Session `2026-09-24T165319` on the Pi.
- The operator drove the scan tool: first the BCM's TPMS ID read, then the
  ECM's "All signals" data list, engine running.

## BCM over CAN (request 0x745, response 0x765)

- **Session:** `10 C0` → `50 C0`, the same Nissan extended session the ECM
  uses. It was kept alive with `3E 01` every 1.5 s and closed with
  `10 81` → `50 81`.
- **`21 83`:** `61 83` with a 24-byte block beginning with ASCII
  **"ZQ01B3B"**, probably a part or software identifier.
- **At screen load, 16:53:37Z:**

  | Request | Reply |
  |---|---|
  | `30 00` | `70 00 02 2B F1 81` |
  | `30 20` | `70 20 E6 77 05 EB` |
  | `30 40` | `70 40 C9 10 10 01` |
  | `3B 20` (sent as `3B 00`/`3B 60`) | `7B 20 C2 6F 71 53` |
  | `3B 40` | `7B 40 C2 A1 90 01` |

  Each reply carries 4 bytes per slot. Not interpreted; these are standard-
  looking I/O-control and write-local-id service numbers used as reads.
- **`21 32` data monitor:** polled at about 2 Hz from 16:54:37 to 16:54:48Z,
  returning a 76-byte block that was identical on every read while parked.
  Probably the BCM data-monitor screen. Unbound.
- **Air pressure (TPMS) ID read, 16:55:11Z:** `21 1F` → `61 1F` + 30 bytes.
  That is the same 15-byte block twice, which reads as five 3-byte slots:

  | slot | hex | decimal |
  |---|---|---|
  | 1 | `85 5D 9B` | 8,740,251 |
  | 2 | `C5 B3 FA` | 12,956,666 |
  | 3 | `85 AD F0` | 8,760,816 |
  | 4 | `85 B8 FD` | 8,763,645 |
  | 5 | `00 00 00` | empty (spare?) |

  Most likely the four registered wheel sensors plus an empty spare slot.
  The wheel order and the ID format the scan tool displays are not confirmed.
  Why the block repeats is unknown: current vs stored? two sets?
- **Why it matters:** the BCM answers on CAN in the same 0xC0 session CANary
  already drives for the ECM. Reading TPMS IDs, and probably pressures from
  `21 32`, needs no K-line work: it's a CAN diagnostics target like the ECM.

## ECM "All signals" (request 0x7E0, response 0x7E8), 16:55:40–16:58:56Z

- 845 requests over 135 distinct request types. **96 Mode 22 DIDs** were read
  in blocks 0x11xx, 0x12xx, 0x13xx and 0x15xx.
- **No new DIDs.** Every DID the scan tool read is already among the 181
  responders in the July address-space survey
  ([2026-07-28T0330-ecu-address-space.md](2026-07-28T0330-ecu-address-space.md)).
  The scan tool skips 85 of those 181, presumably internal or configuration.
  That split is itself new information: the 96 are the ones Nissan presents
  as signals.
- **Poll counts show what was on screen:**

  | reads | DIDs |
  |---|---|
  | ~85 | 0x1110, 0x1111, 0x110A, 0x1106 |
  | ~54 | 0x1114, 0x1115 |
  | ~36 | 0x1101 (ECM_COOLANT_TEMP), 0x1102, 0x1103 |
  | ~13 | 0x1117, 0x1138, 0x114E, 0x1125, 0x1123, 0x1201, 0x111A, 0x1135, 0x1203 |
  | 1–11 | the rest |

  The ~85 group is the page that was visible longest. Its names can be read
  off the screen.
- **Reply widths observed** (for `dids.yaml` `response_length`): 0x11xx are
  1 byte, except the 4-byte 0x1100, 0x1120, 0x1140 and 0x1160; 0x12xx are 2
  bytes, except the 4-byte 0x1200, 0x1220 and 0x1240; 0x13xx and 0x15xx are
  2 bytes, except the 4-byte 0x1300 and 0x1500. The 4-byte 0x1n00/0x1n20/…
  entries look like block bitmaps (supported-DID masks).
- **Other services at screen load:**
  - Mode 21 local ids: `21 80`, `21 81`, `21 83`, `21 10`, `21 FE`, `21 01`
    answered; `21 12` → `7F 21 12`. This bears on D03.16 (live data in
    Mode 21 blocks).
  - `17 FF 00` → `57 01 03 28 C0`: the Nissan self-diagnosis code read
    returned one DTC, **P0328** (knock sensor 1 circuit high), with status
    `C0`. *(Corrected 2026-10-09: this line first recorded only `57 01`.)*
    `/obd/dtc` cannot cross-check it: generic Mode 03/07 never list the
    knock-sensor items. The same reply came on 2026-07-27, before the 08-01
    unplug test; see `docs/ecu-firmware-analysis.md`, "Knock monitoring".
  - `10 03` → `7F 10 12`.
  - `22 F1 A0` → NRC.
  - 20× `7F 22 12`: unsupported DIDs probed during screen build.
  - I/O-control and routine probes `30 xx`, `31 00`, `32 00`, `3B xx`.
- Unresolved: the ECM 0x1210 read `0x009B` here, next to 0x120D/0x120E/0x120F
  at 0x009D/0x009A/0x009C. That's consistent with the four throttle/pedal
  sensor tracks near 0.8 V, but not bound from this session.

## CANary's own traffic

After the restart at 16:53:24Z, passive mode was off until re-armed at about
16:53:40Z. CANary sent Mode 01 requests for about 13 s (0x7E8 answers
16:53:34–37Z) while the operator's scan tool was connected. No collision
resulted; the scan tool was talking to the BCM on 0x745. Passive mode does not
survive a restart; tracked as D03.30.

## Next

1. **Names for the ECM signals.** The operator may record the scan tool's
   "All signals" screen to a file on the tool and download it. Its rows,
   with timestamps and values, bind DIDs by value match, the way the ABS
   decel G and yaw fell out. Short of that, reading the names and values
   aloud a page at a time works.
2. **TPMS:** read the per-wheel IDs off the scan tool's screen to confirm the
   byte format and wheel order. Decode `21 32` against pressures shown on the
   scan tool (a known answer: inflate or deflate one tire).
3. **BCM as a CAN diagnostics target:** a small follow-up to K01/D03. The
   same 0xC0 session plumbing, with a separate request/response pair.

*Addendum 2026-09-26:* the `30 00`/`30 20`/`30 40` replies above decode as
supported-id bitmaps for the BCM's service-0x30 active tests (Mode 01-style;
the 0x20 and 0x40 continuation bits are set exactly where the tool asked for
the next block). An operator-supplied list of eight ids is catalogued with the
decoded bitmap in
[`bcm-active-tests.yaml`](../nissan-xterra-2008/bcm-active-tests.yaml). Seven
are advertised. Front wipers (0x46) is not, but 0x45 is, and two ids are
unnamed. Follow-up is D03.33.
