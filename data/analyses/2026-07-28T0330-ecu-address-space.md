# 2026-07-28 — ECU address-space inventory (Mode 22 full map, service 0x21, Mode 06)

**Vehicle state**: parked; engine running for the Mode 06 / rev work, KOEO for the
service-0x21 sweep. Osiris `ZS44A` + K x1.07, CAL_ID `1ZS44A`, CVN `0B27717D`.
**Tool**: ad-hoc `CanObdClient` probes over the Waveshare HAT, daemon in passive mode
(sole tester on 7E0). Scripts kept at `/data/canary/re/` on the Pi.
**Motivation**: `can-re-workflow.md` records the knock-channel inventory as "completed,
negative" while explicitly leaving the broader DID space and unbound broadcast bytes open.
This inventories what was left open.

Ledger events: `4a0c13b3` (DID blocks + latching bits), `2e5b3093` (service 0x21).

## Method trap, recorded first because it invalidated a result

Mode 22 **and** service 0x21 are both gated behind diagnostic session `0xC0`. Outside the
session every address returns *silence* — not a negative response — which is
indistinguishable from "unsupported". Two consequences bit this session:

1. A 16-bit block sweep run without opening the session reported **zero live blocks in the
   entire address space**, including `0x1100`, which is known-good. The gate reads as an
   empty ECU.
2. A later sweep *appeared* to show service 0x21 answering in the default session. It was
   inheriting an open `0xC0` session from an abandoned background process, kept alive by
   its own request stream — every request resets the session timer. Controlled re-test
   (15 s settle → all `None`; enter `0xC0` → all respond; 15 s settle → `None` again)
   withdrew that claim.

`data/dids.yaml` already documented the gate (`7F 22 80` in default session, 2 s keepalive,
`0x3E` TesterPresent rejected). It should have been read first. Also note the daemon opens
its own `0xC0` windows every 60 s for up to 10 s, so a probe running alongside it can
succeed or fail purely on timing — arm passive mode and settle before concluding anything.

## Mode 22: the complete block map

Block-sampled every `0x100` across the full 16-bit space (4 probes per block) inside
session `0xC0`. Exactly four blocks respond:

| block | status |
|---|---|
| `0x1100` | inventoried 2026-05-16 |
| `0x1200` | inventoried 2026-05-16 |
| `0x1300` | **never swept — 20 responding DIDs** |
| `0x1500` | **never swept — 23 responding DIDs** |

The 2026-05-16 inventory covered `0x1100`-`0x12FF` only, so the "negative" knock result
was drawn from roughly half the reachable space. This map is now complete: there is
nothing outside these four blocks.

## The 43 new DIDs

Idle baseline in `/data/canary/re/did_snapshots/dids_idle_new_blocks.json`.
**11 read all-zero** — the dormant shape, indistinguishable at idle from unused addresses:

`0x1301 0x1315 0x1503 0x1506 0x150B 0x150E 0x1511 0x1514 0x1515 0x1516 0x1517`

The 29 non-zero values look like status/bitfield registers rather than scalars — e.g.
`0x130E`/`0x1504`/`0x1507` all read `003f`, and `0x150C`/`0x150F`/`0x1513` all read `00c0`.
`0x3F` is six bits set on a six-cylinder engine, which *suggests* per-cylinder masks; that
is pattern-matching, not evidence.

### State response (idle → 2000 rpm → idle)

8/8/6 passes per state, polling only the live addresses. **Within-state noise floor was
zero in all three states** — nothing jittered — so every change below is real signal.

| DID | idle | 2000 rpm | idle again | reading |
|---|---|---|---|---|
| `0x1302` | `053f` | `043f` | `153f` | bit0 tracks idle; **bit4 latched** |
| `0x1307` | `0642` | `0442` | `4642` | bit1 tracks idle; **bit6 latched** |
| `0x1512` | `70c0` | `73c0` | `70c0` | live state bit, fully reversible |

`0x1512` returning exactly to its idle value also independently confirms the engine was
genuinely back at idle, so the third column is a valid idle sample.

`0x1302`/`0x1307` are the first **latching** behaviour found anywhere in this
investigation — every prior candidate was either frozen forever or rpm-tracking. Most
likely OBD monitor readiness ("test completed this drive cycle"), which latches by design.
Discriminator: rev again. Readiness bits are already complete and will not move; an event
recorder will keep accumulating.

## Service 0x21 (KWP ReadDataByLocalIdentifier) — first enumeration

The 2026-07-26 flash capture recorded this ECM answering 8 service-0x21 requests, so it
was known-supported, but the namespace had never been enumerated. It is entirely separate
from Mode 22 — a negative DID result says nothing about it. 17 of 256 LIDs respond
(inside `0xC0`); five are aliases, so ~12 distinct values.

| LID | payload (after echo) | reading |
|---|---|---|
| `0x00` | `80000000` | supported-LID bitmask, but under-reports (only flags `0x01`) |
| `0x01` | `191bc000` | unknown, 4 bytes |
| `0x02`-`0x05` | `00000000` each | **four consecutive all-zero 4-byte values** |
| `0x10` | ASCII `1ZS44A00000` | **calibration id** — matches Mode 09 `CAL_ID` exactly |
| `0x81` (=`0xC3`) | 19 zero bytes | **dormant** |
| `0x82` (=`0xC4`) | `5246ff00…ff00` | `ff`-padded slot table; `ff` reads as empty-slot |
| `0x83` (=`0xC6`) | ASCII `ZS44A` + `410604…80` | calibration name without leading `1` |
| `0xF0` (=`0xFE`) | ASCII `12345` + `01410604` + `8X61BJCN41` | see below |
| `0xF1` (=`0xFF`) | `0102030405` + ASCII `DABCDNODEx` | see below |

**`0xF0`/`0xF1` look unprogrammed.** `12345`, `0102030405` and `ABCD…` are placeholder
patterns, not data. This is consistent with the known-blank VIN (the ECM returns 17 null
bytes for Mode 09 InfoType 0x02) — these appear to be identity/configuration fields that
were never personalised. Worth confirming against a second VQ40DE ECM before treating it
as fact.

**Dormant candidates**: `0x02`-`0x05` (a *consecutive* zero block reads like a counter
array rather than scattered reserved bytes) and `0x81`.

## Mode 06 — supported, and CANary could not read it

23 MIDs answer when asked with a MID. CANary sent a bare `06` with the identifier stripped
(`can_obd_collector.py:1035` grouped service `0x06` with Mode 03/07/0A, which correctly
take no identifier — Mode 06 requires one), so `MONITOR_RESULTS` timed out 100% since the
feature landed. Supported set, from walking the `0x00/0x20/0x40/0x60/0x80/0xA0` bitmasks:

`01 02 05 06 21 22 39 3B 3C 3D 41 42 45 46 81 82 A1 A2 A3 A4 A5 A6 A7`

**Per-cylinder misfire counters are live** — MIDs `0xA2`-`0xA7` (cylinders 1-6), TID `0x0B`
(EWMA over last 10 drive cycles) reading `1/1/1/1/0/1`, TID `0x0C` (current drive cycle)
all zero, UAS `0x24` = raw counts. Essentially clean, but the channel works. This is the
closest standardised combustion-abnormality signal available on this ECU.

The rest are O2 sensor (`01 02 05 06`), catalyst (`21 22 41 42 45 46`) and EVAP
(`39 3B 3C 3D`) monitors, all with plausible live values. **No knock monitor.** MIDs
`0x81`/`0x82` return count-type TIDs reading zero with `min=0` *and* `max=0` — an
undefined range, which is not the shape of a live monitor; dormant candidates.

## What is identified vs still unknown

**Identified**: `0x21` LID `0x10` (calibration id), `0x83` (calibration name), Mode 06
`0xA2`-`0xA7` (per-cylinder misfire), plus the O2/catalyst/EVAP MIDs by standard
assignment. `0xF0`/`0xF1` identified as *unprogrammed* rather than as a specific field.

**Unknown**: 40 of the 43 new Mode 22 DIDs, `0x21` LIDs `0x00`-`0x05`/`0x81`/`0x82`, and
the two latching bits.

## Next steps

1. **Re-sweep service 0x21 engine-running.** Tonight's was KOEO; the 17-LID list may be
   incomplete.
2. **Repeat-rev discriminator** on `0x1302`/`0x1307` — readiness vs event recorder.
3. **Load test** — idle snapshot → hard WOT pulls → idle snapshot, diffing all 43 DIDs,
   the 47 dormant zeros in `0x1100`/`0x1200`, the five `0x21` candidates, and Mode 06
   MIDs `0x81`/`0x82`. Latching means nothing needs catching mid-pull. **Run a gentle-drive
   control first** or run-time counters will masquerade as hits.
4. **State-matrix identification** for the 40 unknown DIDs — the same cold/warm/rev/drive
   sweep that produced the `0x1100` map, cross-referenced against simultaneous Mode 01.
   Blocked on the Mode 01 / session-`0xC0` exclusivity: the cross-reference needs both,
   so it has to interleave snapshots with driving rather than run concurrently.
5. **Service `0x23` ReadMemoryByAddress — decoded from the flash capture, see below.**

## Service 0x23 ReadMemoryByAddress — decoded from the flash capture

Extracted from `flash-20260726T222616Z.log` by reassembling ISO-TP on 7E0/7E8.
Control-channel totals: 179 requests / 171 responses.

| service | count |
|---|---|
| `0x10` DiagnosticSessionControl | 137 |
| `0x22` ReadDataByIdentifier | 16 |
| `0x21` ReadDataByLocalId | 8 |
| `0x23` ReadMemoryByAddress | 8 |
| `0x14` ClearDiagnosticInformation | 4 |
| `0xA1` / `0xA2` Nissan proprietary | 2 / 2 |
| `0xAC` unknown | 2 |

**Request format is `23 <32-bit address> <16-bit length>`**, 16 bytes per read:

```
23 00080000 0010  ->  63 02674b0b011ae7ff667d3067890b85ec
23 000c0000 0010  ->  63 d30b223b420ec7066103c706410f410f
23 00020004 0010  ->  63 00021c24ffff7ffc00021c24ffff7ffc
23 00100000 0010  ->  7F 23 22   (conditionsNotCorrect)
```

**It requires session `0xFB`, not `0xC0`.** Every successful read was preceded by a
`10 FB` sent milliseconds earlier — `0xFB` is re-sent before *each* read, so it is
short-lived. This is a third session sub-function alongside `0x85` (programming) and
`0xC0` (unlocks Mode 22).

`23 a100` / `23 a200` returned `7F 23 12` — malformed 2-byte probes, not real reads.

### Correction: services 0xA1 / 0xA2 are NOT supported

An earlier note claimed this ECM "answered" the Nissan proprietary services during the
flash. It did not — it **rejected** them:

```
a1 00  ->  7F A1 11      (serviceNotSupported)
a2 00  ->  7F A2 11
```

They were attempted by the tool and refused. Drop them from the candidate list.

### VERIFIED 2026-07-28, KOEO — CANary can read ECM memory

Reproduced live with operator approval. Engine off, key on, parked, daemon in passive
mode. Session `0xFB` was entered, one read issued, then the session left to time out.

```
10 FB accepted:      True
TX:                  07 23 00080000 0010
RESP:                02674b0b011ae7ff667d3067890b85ec   (16 bytes)
flash ground truth:  02674b0b011ae7ff667d3067890b85ec   <- byte-identical
```

**Zero side effects.** Checked immediately after the session expired:

| | before | after |
|---|---|---|
| stored / pending DTCs | none / none | none / none |
| MIL | off | off (`Mode 01 PID 01` = `00076500`) |
| `WARMUPS_SINCE_DTC_CLEAR` | 1 | 1 |
| `DISTANCE_SINCE_DTC_CLEAR` | 25 | 25 |

Mode 01 answers again once `0xFB` times out, so the block is transient exactly like
`0xC0`. No DTCs set, no readiness reset, no counters disturbed.

**This is the largest capability found in this investigation.** Every other dead end came
down to the ECU not *exposing* a parameter; memory reads do not depend on exposure.

### But the known addresses are almost certainly not RAM

`0x00080000` returned **byte-identical** data to a read taken before *two* reflashes
(stock → ZS44A → K x1.07). A region untouched by calibration flashing is fixed code or
bootloader, not volatile state. A knock counter is volatile — it lives in RAM. All three
known-good addresses may be flash.

So the next step is not reading more of the same, it is **locating RAM**:

- Read one address twice, ~1 s apart, engine running. Flash is stable; RAM holding a live
  engine parameter is not. Regions that differ between back-to-back reads are volatile.
- Sweep coarsely for those regions, then diff them across engine states — the same
  latching logic used for the DIDs, but over raw memory rather than exposed parameters.
- `0x00100000` returned NRC `0x22` (conditionsNotCorrect), **not** `0x31`
  (requestOutOfRange). "Above 1 MB is unreachable" is therefore *not* established — it may
  simply need different conditions. Do not treat the 1 MiB ROM size as the address bound.

## Tooling implications

- `obd_request`'s whitelist (01/02/03/06/07/09/22) cannot reach `0x21`, `0x23` or
  `0xA1`/`0xA2`. All are reads.
- More fundamentally, a one-shot request tool **cannot reach `0x21` or `0x22` at all**
  without a way to open and hold session `0xC0`.
- Mode 06 MID discovery that runs once at startup is fragile: it ran during a passive-mode
  window on 2026-07-27, found zero supported MIDs, and scheduled no polls for the life of
  the process, with an empty `service_channels: []` as the only trace.
