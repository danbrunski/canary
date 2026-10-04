# Airbag (SRS) over K-line: scan-tool code read and clear, 2026-10-04

**Status: protocol observed, DIDs not interpreted.** The companion notes are
[2026-09-24-abs-kline-scan-tool.md](2026-09-24-abs-kline-scan-tool.md) and
[2026-09-24-scan-tool-bcm-ecm-session.md](2026-09-24-scan-tool-bcm-ecm-session.md).

## Setup

- 2008 Xterra. The engine was running when the window opened (19:48:24Z) and
  the operator switched to KOEO before the clear.
- CANary was in passive mode (no CAN transmit). The K-line collector sniffed
  listen-only on `/dev/ttyAMA0`.
- Session `2026-10-04T194342` on the Pi, capture `6bd4761e…`, tag
  "operator: airbag clear done (KOEO)" at 19:54:39Z.
- The operator's scan tool read the airbag codes, then cleared them.

## CAN first, then K-line

At **19:48:54.56Z** the tool sent `02 10 C0` (DiagnosticSessionControl 0xC0,
the Nissan session) on **CAN 0x752**, the usual Nissan airbag request id.
Nothing answered: no 0x772 frame anywhere in the session. 3.4 s later it
started on the K-line. **On this truck the airbag module is K-line only.**

## K-line exchange (module address 0x58, tester 0xFC)

Each StartCommunication is `81 58 FC 81 56` → `83 FC 58 C1 6D 8F 94`. The key
bytes `6D 8F` are the same as the ABS module's. After it, frames use the
length-only header, like the ABS.

| Time (Z) | Tester | Module | Reading |
|---|---|---|---|
| 19:48:57.98 | `81` StartComm | `C1 6D 8F` | connect #1 |
| 19:48:58.67 | `1A 80` | `5A 30 3A` | identification; the reply is 2 bytes and does not echo `80` |
| 19:48:58.78 | `82` | `C2` | StopCommunication |
| 19:49:00.52 | `81` StartComm | `C1 6D 8F` | connect #2 (code screen) |
| 19:49:01.20 | `1A 80` | `5A 30 3A` | |
| 19:49:01.40 | `22 11 00 04 01` | `62 11 00 80 41 FF FF` | 4 data bytes |
| 19:49:01.58 | `22 11 20 04 01` | `62 11 20 FF FF FF F9` | 4 data bytes |
| 19:49:01.76 | `22 11 3D 04 01` | `62 11 3D 0C` | 1 data byte |
| 19:49:30.96 | `81` StartComm | `C1 6D 8F` | connect #3 (clear) |
| 19:49:31.63 | **`14`** | `7F 14 78` ×15, then **`54`** at 19:49:31.98 | ClearDiagnosticInformation with no group byte: about 350 ms of responsePending, then positive |
| 19:49:33.07–19:50:10.58 | `3E` every 1.50 s | `7E` | TesterPresent, 26 exchanges |
| 19:50:11.50 | `82` | `C2` | StopCommunication |

Notes:

- The reads use the same `22 <DID> 04 01` form (with the `04 01` trailer)
  that the scan tool uses for the ABS and that UpRev uses for the ECM.
- The tool never used KWP `18`/`17` (ReadDTCByStatus or by number). The code
  screen came from the three `22` reads, so a DTC record is probably among
  `0x1100`, `0x1120` and `0x113D`. That is unconfirmed. Reading the same DIDs
  again with a known code stored would settle it.
- The DIDs were not read again after the clear, so the sniff cannot show
  whether the codes stayed cleared (whether each fault was past or current).
  The scan tool's display and the warning lamp are the evidence for that.

## What CANary can do with this

The full clear is `81 58 FC 81 56` → `14` → wait through `7F 14 78` for `54`
→ `82`. It is a volatile, reversible write that AGENTS.md allows on any
module. `kline.yaml` now lists module `0x58` as `SRS` with no `reader`, so
CANary still never commands it. Adding a reader and a clear action is a
separate step.
