---
name: ecu-reverse-engineering
description: >-
  Knowledge and method for reverse engineering engine management systems (ECUs)
  and decoding vehicle telemetry streams. Use this whenever the task involves
  decoding a CAN / OBD-II / UDS stream, working out what a raw CAN payload or
  byte means, mapping a broadcast signal to a physical quantity (RPM, speed,
  coolant, fuel trim, load, MAF), probing an ECU for supported PIDs / DIDs /
  diagnostic services, framing OBD or UDS requests (modes, PIDs, ISO-TP,
  tester-present, security access), decoding DTCs, or reverse engineering ECU
  firmware / ROM tables. Reach for it even when the user only says "what does
  this byte mean", "which PID gives X", "why does this signal look wrong", or
  drops a candump / hex dump. Consult it BEFORE hand-decoding bytes or asserting
  that a signal is bound to a quantity — the validation discipline here exists
  to stop confident-but-wrong bindings.
---

# Reverse engineering engine management systems

This skill is the working knowledge for **reading meaning out of an ECU** — both
the standardized diagnostic channel and the proprietary broadcast bus the car
actually runs on. It is grounded in *The Car Hacker's Handbook* (Craig Smith,
No Starch Press) and supplemented with standard SAE decoding conventions.

Scope is **CAN bus + engine management**, read/decode/probe: sniffing, requesting,
decoding, and binding signals to physical quantities, plus firmware/ROM reversing
for calibration and OEM data. It is written vehicle-generic. Where a concrete tool
helps, this repo's CANary control surface is called out as *"in this repo, …"* —
those asides are examples, not requirements; the method stands on its own for any
vehicle.

**Out of scope** (deliberately): defeating vehicle security for its own sake —
key-fob/immobilizer capture-replay, RF relay attacks, unlocking or starting a car
you're probing. Techniques here that touch access control (SecurityAccess,
seed-key) are covered only as the gate you pass to read **engine** calibration and
diagnostic data on your own vehicle. Tire-pressure (TPMS) sensors are a legitimate
adjacent target but live on **SDR/RF**, not the CAN bus — that's a separate area
this skill does not cover.

> **Authorization & safety.** This is for decoding telemetry from a vehicle you
> own or are authorized to test, and it stays **read-only on the bus** (sniff,
> request, decode — no `bus.send` actuation). Injecting packets into a *moving*
> vehicle is dangerous and out of scope; immobilize the vehicle for any write
> testing. Fuzzing/replay can flash warning lights or drain the battery — annoying,
> not damaging, but expect it.

## The one distinction that organizes everything

A signal you want to decode lives in **one of two worlds**. Name the world first;
the decoding method follows from it.

| | **Diagnostic world** (request→response) | **Broadcast world** (the real bus) |
|---|---|---|
| How you get it | *You ask*: send a mode+PID or UDS request, ECU replies | *You listen*: the car floods these continuously to run itself |
| IDs | `0x7DF` request / `0x7E8`+ response (OBD); discovered IDs for UDS | Proprietary arbitration IDs, one per message |
| Standardized? | Modes/PIDs are SAE-standard; UDS DIDs are per-OEM | **No.** Unique to make/model/year — must be reversed |
| Decode by | Applying the published PID formula to the response bytes | Correlating byte changes to physical actions |
| Rate | Slow, polled (you pace it) | Fast, native (10s–100s Hz) |

The diagnostic world is a **read-only window** the manufacturer chose to expose.
The broadcast world is everything else — the packets the car uses to *act*. Most
of the interesting telemetry (true RPM, cam angle, per-cylinder data) is only on
the broadcast side, often in a *different, compressed encoding* than the
diagnostic PID reports for the same quantity. Decoding a stream well usually means
working both sides and making them agree.

---

## Decoding the diagnostic world (OBD-II / UDS)

Every diagnostic exchange is a few bytes with a rigid anatomy. Learn to read it
by hand; it makes captures self-evident.

**Request** (`cansend can0 7DF#02010D`):
- `7DF` — functional OBD request ID (broadcast to all ECUs). A specific ECU is
  addressed at `0x7E0`–`0x7E7`.
- `02` — **ISO-TP length**: number of payload bytes that follow (here, 2).
- `01` — **mode** (a.k.a. service). `01` = show current data.
- `0D` — **PID** (the parameter you want; `0D` = vehicle speed).

**Response** (`7E8 03 41 0D 00`):
- `7E8` — responding ECU (request ID **+ 0x8**).
- `03` — length (3 bytes follow).
- `41` — **positive response**: mode **+ 0x40** (`0x01 + 0x40`). A *negative*
  response is `0x7F`.
- `0D` — the PID echoed back.
- `00` — the data (speed 0). Apply the PID's formula to these data bytes.

Two rules that fall out of this and cover most parsing:
- **Positive = request + 0x40, negative = 0x7F.** `7E8 03 7F 11 11` means service
  `0x11` was rejected with NRC `0x11` (service-not-supported). NRC table →
  `references/obd-uds-reference.md`.
- **> 7 data bytes ⇒ ISO-TP multi-frame.** A single CAN frame holds ≤ 8 bytes, so
  a VIN or long DID reply arrives as a first-frame + consecutive-frames sequence
  (`10 14 …` then `21 …`, `22 …`). Reassemble before decoding, or a raw sniffer
  shows you only the first fragment. Framing details → `references/obd-uds-reference.md`.

**Applying PID scaling.** The response data bytes are raw; the physical value
comes from the SAE J1979 formula for that PID. E.g. RPM (PID `0x0C`) =
`(256·A + B) / 4`, coolant (`0x05`) = `A − 40` °C, load (`0x04`) = `A·100/255` %.
The full table (modes, common PIDs, mode-09 vehicle info, scaling formulas) is in
`references/obd-uds-reference.md` — **read the exact formula there; do not decode
a PID from memory**, off-by-one scale/offset errors are the classic silent bug.

*In this repo:* raw frame bytes are stored, so `query_telemetry` with
`hex(payload)` lets you re-decode any historical frame; `get_obd_coverage` shows
which PIDs a session actually captured.

---

## Probing: discovering what an ECU will tell you

You rarely know up front what's supported. Discovery, cheapest first:

1. **Supported-PID bitmask.** Mode `01` PID `00` returns a 4-byte bitmask of which
   PIDs in `0x01`–`0x20` exist; `0x20`, `0x40`, `0x60`… chain to the next block.
   One request maps the whole standard surface — do this before guessing.
2. **UDS service discovery.** Beyond the ~10 legacy OBD modes, ISO 14229 services
   (`0x22` ReadDataByID, `0x27` SecurityAccess, `0x2E` WriteDataByID, `0x23`
   ReadMemoryByAddress, `0x3E` TesterPresent…) expose OEM data. Discover
   responding diagnostic IDs and supported services by sweeping — CaringCaribou's
   `dcm discovery` / `dcm services` sends TesterPresent to every arbitration ID
   and reports valid (`0x40+svc`) or error (`0x7F`) replies. Tool notes →
   `references/tooling.md`.
3. **Enhanced reads.** `0x22` ReadDataByID (2-byte DID) returns data standard OBD
   can't. DIDs are per-OEM — you'll be reversing/brute-forcing the DID space.
4. **Hold the session open.** Long operations need the ECU kept in a diagnostic
   state: send **TesterPresent** (`7DF#013E`) as a heartbeat every 1–2 s
   (`cangen -g 1000 -I 7DF -D 013E -L 2 can0`), else the session times out.
5. **SecurityAccess (`0x27`)** gates protected data/actions behind a seed→key
   challenge. If you don't have the key algorithm you must recover it (see
   firmware reversing) or brute-force — often not worth it vs. reversing the tool.

**When a probe comes back empty, say *which kind of empty*.** "The ECU replied
`0x7F` service-not-supported" (the surface isn't there), "no response at all" (wrong
ID / bus / ECU asleep), and "supported but the value is `0x00`/`0x80` sentinel"
(ECU-side not-available) are three different answers demanding opposite next steps.
Never collapse them into "no data." *In this repo the LTFT-reads-0 and delivery-error
incidents were exactly this confusion — a `0x80` sentinel and a mode-disabled-by-profile
capture, both misread as "missing."*

---

## Reversing the broadcast bus (the real work)

Generic packet analysis fails here — packets are vehicle-unique and the bus is
noisy. The universal method binds a byte to a function through a **controlled
physical action**, not through inspection:

1. **Record** the bus.
2. **Do one physical thing** to the vehicle (unlock a door, blip the throttle).
3. **Stop** recording.
4. **Play it back** and confirm the action reproduces (door unlocks, tach moves).
   If it doesn't: wrong bus, the signal is hardwired not on the bus, or a
   collision stomped it — try another bus / another instance of the action.
5. **Halve** the capture repeatedly (bisection), replaying each half, until one
   arbitration ID remains. Then sniff that ID and watch which **byte/bit changes**
   when you repeat the action.

`cansniffer` is built for step 5: it groups by arbitration ID and highlights bytes
that changed since that ID was last seen (`-c` colorizes), so a single-bit change
pops out of the flood. Filter to one ID (`-000000` then `+301`) to isolate it.

Then decode the field the same way you'd decode a lock (bit `0x01` = driver door,
`0x02` = passenger… → it's a **bitfield**) or a gauge:

- **Values are often "shifted"/compressed**, not the plain hex of the reading.
  RPM may appear as `0xFA0` for ~1000 because the firmware bit-shifts (×/÷) to pack
  it. Expect **multi-byte** fields (big-endian `256·A + B` is the common case) and
  scale/offset you must solve for.
- The diagnostic PID for the same quantity is usually a **different encoding** than
  the broadcast field — you can't assume the PID formula decodes the raw packet.
  But the PID gives you a *ground-truth value* to solve the broadcast scale against
  (see below).

Details, `cansniffer` bitmask filtering, record/playback formats, ICSim practice
rig, OpenXC → `references/reversing-broadcast.md` and `references/tooling.md`.

*In this repo:* the sanctioned way to run this correlation on stored data is
`compare_channels` (it pairs same-instant samples itself), and `start_replay`
lets you re-run a known session through the pipeline instead of needing the
vehicle. UpRev logs give simultaneous ground-truth windows for labeling the bus.

---

## The validation discipline (read this before asserting any binding)

Reverse-engineered bindings are where confident, wrong answers come from. The
single most expensive class of error is **binding a signal by matching
aggregates** — "byte X's average matches quantity Y's average, so X = Y." It is
almost always spurious: a mid-scale-hovering low byte of a counter matches
anything else near mid-scale; two signals averaged over windows with different
composition "agree" by accident. This mistake has been made and retracted many
times. Guard against it:

- **Correlate same-instant values, never two aggregates.** Bind X to Y only if
  X and Y move together *sample-by-sample* under a physical action you controlled
  — which is exactly why the record→act→playback method above is built around a
  deliberate action, not passive staring.
- **Reproduce a known answer before trusting a novel one.** The strongest, nearly
  free check: a broadcast binding and the **Mode-01 PID for the same quantity must
  agree 1:1 once both are in engineering units.** That one assertion catches a wrong
  byte offset, endianness, scale, offset, or unit without anyone knowing the "right"
  answer in advance. (Blind spot: slope is a ratio, so a +4% scale error shows as
  only −2.9% slope error — pin exact decode params separately.)
- **r² alone is not a binding.** Over one drive nearly every engine signal
  correlates with RPM. High correlation ≠ correct scale. Separate *unstable* from
  *unresolvable* rather than ranking by r².

*In this repo these are enforced, not just advised:* use `compare_channels` (it
refuses too-few-pairs and barely-moving signals — the two cases where an aggregate
lies), and validate against `tests/test_analysis_ground_truth.py`, which runs the
real chain over a committed real drive and asserts a broadcast binding and its PID
agree 1:1.

---

## Reversing ECU firmware / ROM tables

When decoding the live stream isn't enough — you need the calibration tables, the
seed-key algorithm, or the meaning of an OEM DID — you go into the firmware. This
is a large, separate effort (hundreds of hours for cold code analysis). The
breadcrumbs, front-door (J2534/KWP2000/seed-key) vs backdoor (chip-off) tradeoffs,
how to find calibration tables by their physics-determined shape (e.g. a MAF VQ
curve), and disassembly starting points are in **`references/firmware-reversing.md`**.
Read it when the task crosses from "decode the bytes on the wire" into "understand
or change what the ECU computes."

---

## Reference files

- `references/obd-uds-reference.md` — OBD-II modes, common Mode-01/02 PIDs with SAE
  J1979 scaling formulas, Mode-09 vehicle info, UDS/ISO-14229 services, UDS negative
  response codes (NRCs), DTC byte layout, CAN standard/extended framing, ISO-TP
  multi-frame structure. **The lookup tables — open this to decode a specific byte.**
- `references/reversing-broadcast.md` — the full record→act→playback→bisect method,
  `cansniffer` grouping and bitmask filters, record/playback formats, shifted-value
  decoding, ICSim practice rig, OpenXC translation.
- `references/firmware-reversing.md` — front-door (J2534, KWP2000, seed-key) and
  backdoor attack surfaces, finding calibration tables in ROM, comparing bins to
  isolate parameters, disassembly breadcrumbs (interrupt vectors, self-diagnostic
  routines, table-lookup signatures).
- `references/tooling.md` — can-utils suite, CaringCaribou, ICSim, Kayak, OpenXC,
  J2534 — what each does and when to reach for it.
