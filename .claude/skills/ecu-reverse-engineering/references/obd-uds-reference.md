# OBD-II / UDS / CAN decoding reference

Lookup tables for hand-decoding diagnostic bytes. Grounded in *The Car Hacker's
Handbook* Ch. 2, Ch. 4, and Appendix B; PID scaling formulas are standard SAE
J1979 (the book lists PID numbers and points to the OBD-II PID list for formulas).

## Contents
- [CAN frame layout](#can-frame-layout)
- [ISO-TP multi-frame](#iso-tp-multi-frame)
- [Request / response byte anatomy](#request--response-byte-anatomy)
- [OBD-II modes (services)](#obd-ii-modes-services)
- [Common Mode 01/02 PIDs + scaling](#common-mode-0102-pids--scaling)
- [Mode 09 vehicle information](#mode-09-vehicle-information)
- [UDS / ISO 14229 services](#uds--iso-14229-services)
- [UDS negative response codes (NRCs)](#uds-negative-response-codes-nrcs)
- [DTC byte layout](#dtc-byte-layout)

---

## CAN frame layout

Two frame types: **standard** (11-bit ID) and **extended** (29-bit ID).

Standard frame key fields:
- **Arbitration ID** — identifies the sending message/device; also sets priority
  (lower ID wins simultaneous transmission). Broadcast: every controller sees
  every frame, and nothing authenticates the sender, so any node can impersonate
  any ID.
- **IDE** — identifier-extension bit; `0` for standard, `1` for extended.
- **DLC** — data length code, 0–8 bytes.
- **Data** — 0–8 bytes (some systems pad to 8).

Extended frames carry an 18-bit identifier appended to the standard 11-bit one
(29 bits total), use SRR in place of RTR, and set IDE=1. They're backward
compatible — a standard-only node ignores them without breaking.

## ISO-TP (ISO 15765-2) multi-frame

CAN caps data at 8 bytes; ISO-TP chains frames to carry up to 4095 bytes. The
**first data byte is a PCI (protocol control info) nibble+length**:

| First byte | Frame type | Meaning |
|---|---|---|
| `0X` | Single Frame | low nibble `X` = data length (≤7). E.g. `02 01 0D` = 2 bytes: `01 0D`. |
| `1X XX` | First Frame | 12-bit total length `XXX` across the two nibbles; data starts next byte. E.g. `10 14 49 02 01 …` = 0x14 (20) byte message. |
| `2N` | Consecutive Frame | `N` = sequence number 1..F wrapping. Reassemble in order. |
| `3X` | Flow Control | receiver→sender: clear-to-send / wait / block size. |

A **raw** sniffer (candump/Wireshark) does **not** reassemble ISO-TP — you see
the first frame then loose consecutive frames. Use `isotpsniffer` / `isotprecv`
+ `isotpsend` (needs the `can-isotp` kernel module) to work at the message level.

## Request / response byte anatomy

Request `7DF#02010D` and response `7E8 03 41 0D 00`:

| Byte | Request | Response |
|---|---|---|
| ID | `7DF` functional request (`7E0`–`7E7` = specific ECU) | `7E8` = request ID **+ 0x8** |
| 1 | `02` ISO-TP length | `03` ISO-TP length |
| 2 | `01` mode | `41` = mode **+ 0x40** (positive); `7F` = negative |
| 3 | `0D` PID | `0D` PID echoed (on `7F`, this is the rejected service) |
| 4+ | — | data bytes (`00`); on `7F`, an NRC |

- Positive response = **request service + 0x40**.
- Negative response = **`0x7F`**, followed by the service and a 1-byte NRC.

## OBD-II modes (services)

| Mode | Function |
|---|---|
| `0x01` | Show current data. PID `0x00` returns a 4-byte bitmask of supported PIDs `0x01`–`0x20`. |
| `0x02` | Show freeze-frame data (same PIDs as `0x01`, sampled at DTC time). |
| `0x03` | Show stored (confirmed) DTCs. |
| `0x04` | Clear DTCs + freeze-frame / diagnostic history. |
| `0x07` | Show pending DTCs (seen once, unconfirmed). |
| `0x08` | Control on-board component/actuator (non-standard; dealer-tool territory). |
| `0x09` | Request vehicle information (see Mode 09). |
| `0x0A` | Permanent DTCs (cleared only when the PCM re-verifies the fault is gone). |

## Common Mode 01/02 PIDs + scaling

`A`, `B`, `C`, `D` = successive data bytes (unsigned) after the echoed PID.

| PID | Quantity | Formula | Unit |
|---|---|---|---|
| `0x00` | Supported PIDs `01–20` | bitmask | — |
| `0x01` | Monitor status / MIL | bit-encoded (bit7 of A = MIL on; A&0x7F = DTC count) | — |
| `0x04` | Calculated engine load | `A · 100 / 255` | % |
| `0x05` | Engine coolant temp | `A − 40` | °C |
| `0x06` | Short-term fuel trim (bank 1) | `(A − 128) · 100 / 128` | % |
| `0x07` | Long-term fuel trim (bank 1) | `(A − 128) · 100 / 128` | % |
| `0x0B` | Intake manifold abs pressure | `A` | kPa |
| `0x0C` | Engine RPM | `(256·A + B) / 4` | rpm |
| `0x0D` | Vehicle speed | `A` | km/h |
| `0x0E` | Timing advance | `A/2 − 64` | ° before TDC |
| `0x0F` | Intake air temp | `A − 40` | °C |
| `0x10` | MAF air flow rate | `(256·A + B) / 100` | g/s |
| `0x11` | Throttle position | `A · 100 / 255` | % |
| `0x1C` | OBD standard conformed to | enumerated | — |
| `0x1F` | Run time since engine start | `256·A + B` | s |
| `0x20` | Supported PIDs `21–40` | bitmask | — |
| `0x31` | Distance since DTCs cleared | `256·A + B` | km |
| `0x40`/`0x60`/`0x80`/`0xA0`/`0xC0` | next supported-PID bitmask block | bitmask | — |
| `0x4D` | Time run with MIL on | `256·A + B` | min |

Sentinel note: some ECUs answer a *supported* PID with a not-available sentinel
(e.g. fuel-trim byte `0x80` → 0.0%). That is "ECU-side not available," not a decode
bug and not "missing data." Full PID list: <https://en.wikipedia.org/wiki/OBD-II_PIDs>.

## Mode 09 vehicle information

| PID | Returns |
|---|---|
| `0x00` | Supported Mode-09 PIDs `01–20` (bitmask) |
| `0x02` | VIN (ASCII; arrives via ISO-TP multi-frame) |
| `0x04` | Calibration ID |
| `0x06` | Calibration verification numbers (CVN) |
| `0x0A` | ECU name |

Worked VIN example: `09 02` →
`49 02 01 31 47 31 5A 54 …` — `0x49`=service `09`+`0x40`, `0x02`=PID, `0x01`=count
of data items, remaining bytes = ASCII VIN.

## UDS / ISO 14229 services

Modes above `0x10` are the UDS/enhanced services (per-OEM data & operations):

| SID | Service |
|---|---|
| `0x10` | DiagnosticSessionControl (initiate diagnostics) |
| `0x11` | ECUReset |
| `0x14` | ClearDiagnosticInformation |
| `0x22` | **ReadDataByIdentifier** (2-byte DID; returns OEM data OBD can't) |
| `0x23` | ReadMemoryByAddress |
| `0x27` | **SecurityAccess** (seed→key challenge) |
| `0x2E` | WriteDataByIdentifier |
| `0x34` | RequestDownload |
| `0x35` | RequestUpload |
| `0x36` | TransferData |
| `0x37` | RequestTransferExit |
| `0x3D` | WriteMemoryByAddress |
| `0x3E` | **TesterPresent** (heartbeat to hold the session; send every 1–2 s) |

## UDS negative response codes (NRCs)

4th byte of a `0x7F` response:

| NRC | Meaning |
|---|---|
| `0x10` | General reject |
| `0x11` | Service not supported |
| `0x12` | Subfunction not supported |
| `0x13` | Incorrect message length or invalid format |
| `0x14` | Response too long |
| `0x21` | Busy — repeat request |
| `0x22` | Conditions not correct |
| `0x24` | Request sequence error |
| `0x25` | No response from subnet component |
| `0x26` | Failure prevents execution of requested action |
| `0x31` | Request out of range |
| `0x33` | Security access denied |
| `0x35` | Invalid key |
| `0x36` | Exceeded number of attempts |
| `0x37` | Required time delay not expired |
| `0x70` | Upload/download not accepted |
| `0x71` | Transfer data suspended |
| `0x72` | General programming failure |
| `0x73` | Wrong block sequence counter |
| `0x78` | Request correctly received, response pending (keep waiting) |
| `0x7E` | Subfunction not supported in active session |
| `0x7F` | Service not supported in active session |

`0x78` (response-pending) is not a failure — the ECU is telling you to wait for a
later positive response; don't treat it as a dead end.

## DTC byte layout

A DTC is 5 characters packed into **2 raw bytes**. First two **bits** of byte 1
select the system letter; the rest map 1:1 to the digits.

| First 2 bits | System |
|---|---|
| `00` | P — powertrain |
| `01` | C — chassis |
| `10` | B — body |
| `11` | U — network |

Next 2 bits = second character (`0` SAE-standard, `1`/`3` manufacturer-specific;
`2` also SAE). Remaining nibbles = the last three hex digits.

Example ranges (powertrain): `P0001–P0099` fuel/air metering & aux emissions ·
`P0100–P0199` fuel/air metering · `P0200–P0299` injector circuit · `P0300–P0399`
ignition/misfire · `P0400–P0499` aux emissions · `P0500–P0599` speed/idle control ·
`P0600–P0699` computer output circuit · `P0700–P0799` transmission.

**Freeze-frame** (Mode 02) snapshots at fault time typically include: DTC, engine
load, RPM, coolant temp, fuel trim, MAP/MAF, open/closed-loop mode, throttle
position, vehicle speed — recorded ~5 s after the DTC triggers (so brief transients
may be missed).
