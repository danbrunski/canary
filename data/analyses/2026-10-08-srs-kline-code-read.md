# Airbag (SRS) module code read over K-line, sniffed 2026-10-08

Session `2026-10-08T172943` (laptop copy:
`~/canary-data/sessions/nissan-xterra-2008/2026-10-08T172943`). K-line rows are
`source='kline'`, channel `FRAME`, frame hex in `value_text`, direction in
`metadata_json`. Key on, engine off. The operator's scan tool read the airbag
module's codes, cleared them and read them again. The CANary-HAT sniffed the
whole exchange passively.

Follows `2026-10-04-srs-kline-scan-tool.md`, which recorded the clear and
guessed that the code screen came from the `22 1100/1120/113D` reads. It does
not. The code read is service `A2`.

## Exchange (module 0x58, tester 0xFC, length-only frames, checksum last)

| Time (Z) | Tester frame | Module frame | Reading |
|---|---|---|---|
| 17:57:22.162 | `81 58 FC 81 56` | `83 FC 58 C1 6D 8F 94` | StartCommunication |
| 17:57:22.871 | `02 A2 00 A4` | `02 E2 00 E4` | `A2 00`: no codes |
| 17:57:22.996 | `02 A2 01 A5` | `04 E2 01 90 23 9A` | `A2 01`: one code, word `0x9023` = **B1023** |
| 17:57:24–17:58:16 | `01 3E 3F` every 1.5 s | `01 7E 7F` | TesterPresent |
| 17:58:19.859 | `81 58 FC 81 56` | `83 FC 58 C1 6D 8F 94` | StartCommunication (clear) |
| 17:58:20.533 | `01 14 15` | `03 7F 14 78 0E` ×15, then `01 54 55` at 17:58:20.883 | ClearDiagnosticInformation: ~350 ms of responsePending, then positive |
| 17:58:36.376 | `81 58 FC 81 56` | `83 FC 58 C1 6D 8F 94` | StartCommunication (re-read) |
| 17:58:37.087 | `02 A2 00 A4` | `02 E2 00 E4` | no codes |
| 17:58:37.213 | `02 A2 01 A5` | `02 E2 00 E4` | no codes |
| 17:58:51 | `A2 00`, `A2 01` | `E2 00`, `E2 00` | no codes, read again |

During the first read the scan tool showed **B1023 PAST**. After the clear it
showed "System pass, no fault found".

## Decode

- Reply `E2 <n> [<hi> <lo>]×n`: a count, then n two-byte code words. The second
  byte is the count, not an echo of the request's `00`/`01`: `A2 01` answered
  `E2 01 …` before the clear and `E2 00` after it.
- Code words use the SAE J2012 layout. The top two bits give the letter (`00` P,
  `01` C, `10` B, `11` U), and the other fourteen bits give four hex digits.
  `0x9023` = `10 01 0000 0010 0011` → B, 1, 0, 2, 3 → **B1023**.
- `A2 00` = current codes, `A2 01` = past codes. **Inferred** from a single
  observation: `A2 00` was empty while the tool said PAST, and `A2 01` held the
  code. We never saw a code under `A2 00` while a fault was present, so the
  mapping is recorded as `status_confidence: inferred` in `kline.yaml`.
- The `22 1100/1120/113D 04 01` reads come when the module screen opens. They
  are not the code read.

## What CANary does with it

`data/nissan-xterra-2008/kline.yaml` `modules.0x58.read_codes` records both
requests (with status and confidence), the reply format, the code-word decode
and this reply as its known answer. The profile refuses to load if `E2 01 90
23` stops decoding to B1023. `POST /control/diagnostics/read_codes
{"module": "SRS"}` and the **Read codes** action on Diagnose → Diagnostic
Functions run the same exchange: StartCommunication, `A2 00`, `A2 01`,
StopCommunication (`docs/headless-api.md` "Diagnostic functions"). The
collector tests replay these frames over a fake serial port
(`tests/test_kline_actions.py`).

`A2` is now on the K-line client's allowlist (`SID_READ_STORED_CODES`) and is
the only code-read service a profile may name (`CODE_READ_SERVICES`). Another
module's code read on a different service needs its reply layout seen on the
wire first.

## Diagnosis (operator)

B1023 was a past fault, not a present one. The most likely cause is the flaky
connector under the seat. The clear took it to "no fault found", and the
re-reads at 17:58:37Z and 17:58:51Z returned no codes on either request. If
the fault returns, read the codes while it is present. Seeing it under `A2 00`
would confirm the current/past split, which is still inferred.
