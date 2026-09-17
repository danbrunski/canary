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

Working knowledge for reading meaning out of an ECU — the standardized diagnostic
channel and the proprietary broadcast bus the car runs on. Grounded in *The Car
Hacker's Handbook* (Craig Smith) + standard SAE decoding.

**Scope:** CAN + engine management, read-only (sniff, request, decode). Out of
scope: defeating vehicle security for its own sake (key-fob/immobilizer/RF
attacks) and TPMS (that's SDR/RF, not CAN). Access-control services here
(SecurityAccess, seed-key) matter only as the gate to read *engine* data on your
own vehicle.

**Do it through CANary, not external CLI tools.** This repo's daemon already
implements the sniff/request/decode/compare loop as read-only, audited MCP/HTTP
tools — see the mapping below. External tools (can-utils, CaringCaribou, WinOLS…)
are named only as the *reference behavior* for a capability CANary lacks, so you
can say "CANary can't do X yet; the reference is Y — a candidate feature." Never
tell the user to go install and run a separate tool when a CANary tool covers it.

**Answer briefly.** Lead with the answer (the decoded value, the yes/no, the one
command to run). Keep byte walkthroughs to a compact table, not prose. Pull detail
from the reference files only when the task needs it. Depth on demand, not by
default — a correct two-line answer beats a correct two-page one.

## The one distinction that organizes everything

Every signal lives in **one of two worlds**. Name the world first; the method follows.

| | **Diagnostic world** (request→response) | **Broadcast world** (the real bus) |
|---|---|---|
| How you get it | *You ask*: send mode+PID / UDS request, ECU replies | *You listen*: the car floods these to run itself |
| IDs | `0x7DF` req / `0x7E8`+ resp; discovered IDs for UDS | Proprietary arbitration IDs, one per message |
| Standardized? | Modes/PIDs SAE-standard; UDS DIDs per-OEM | **No** — unique to make/model/year, must be reversed |
| Decode by | Applying the published PID formula | Correlating byte changes to physical actions |
| In CANary | `obd_request`, `get_obd_coverage`, `get_dids`, `get_dtc` | `can_activity`, `get_bindings`, `compare_channels` |

The diagnostic world is a read-only window the OEM chose to expose. The broadcast
world is everything else — usually a *different, compressed encoding* of the same
quantity. Decoding a stream well means working both sides and making them agree.

## CANary tool map

| Task | CANary tool |
|---|---|
| Send a specific PID/DID now (Mode 01/02/03/06/07/09/0A/22); `header` addresses a module (`0x7E0`=ECM) | `obd_request` |
| Which standard PIDs the ECU answers | `get_obd_coverage` |
| Known DIDs / broadcast bindings inventory | `get_dids` / `get_bindings` |
| Stored/pending/permanent DTCs + MIL | `get_dtc` |
| Force a scheduled channel to poll now | `trigger_poll` |
| Raw CAN for N s → per-ID rate + **which byte indices changed** | `can_activity` |
| Capture with physical-action tags for offline analysis | `start_capture` / `tag_current_capture` / `record_vehicle_event` |
| Targeted high-rate PID/DID capture (profile + rate/budget, verified) | `arm_tuning_capture` |
| Correlate two channels the sanctioned way (same-instant pairing) | `compare_channels` (`src.analysis.compare`) |
| Re-decode any stored frame's raw bytes | `query_telemetry` with `hex(payload)` |
| Replay a stored session through the pipeline | `start_replay` / `replay_window` |

## Decoding the diagnostic world

`obd_request` sends the request and decodes the reply. To read a raw response by
hand, the anatomy of `7DF#02010D` → `7E8 03 41 0D 00`:

| Byte | Request | Response |
|---|---|---|
| ID | `7DF` functional (`7E0`–`7E7`=specific ECU) | `7E8` = req ID **+ 0x8** |
| len | `02` ISO-TP length | `03` |
| mode/PID | `01` mode, `0D` PID | `41` = mode **+ 0x40** (positive; `7F`=negative), `0D` echoed |
| data | — | `00` — apply the PID's formula |

Two rules cover most parsing:
- **Positive = request + 0x40; negative = `0x7F` + service + NRC.**
- **> 7 data bytes ⇒ ISO-TP multi-frame** (`10 …` first frame, `2N …` consecutive)
  — reassemble before decoding. `obd_request` handles this; a raw sniffer shows
  only the first fragment.

Apply the SAE J1979 scaling to get engineering units (RPM `0x0C` = `(256·A+B)/4`,
coolant `0x05` = `A−40` °C, …). **Read the exact formula from
`references/obd-uds-reference.md`** — an off-by-one scale/offset is the classic
silent bug. That file also has modes, the full PID/DID/NRC/DTC tables, and CAN/ISO-TP
framing.

## Probing: what will the ECU tell you

Cheapest first:
1. **Supported-PID bitmask** — `get_obd_coverage`, or `obd_request` Mode 01 PID `00`
   (4-byte mask of `0x01`–`0x20`; `0x20`/`0x40`/… chain to the next block). One
   request maps the whole standard surface — don't guess PIDs.
2. **A specific enhanced read** — `obd_request` Mode `22` with the 2-byte DID and
   `header` `0x7E0` to hit the ECM directly; `get_dids` lists what's catalogued.
3. **DTCs** — `get_dtc` (Mode 03/07/0A, read-only, no clear).

**Say which kind of empty you got** — three different answers, opposite next steps:
`obd_request` `responded:false` = no reply (wrong ID/bus/timing), a `0x7F` NRC =
service/PID genuinely unsupported (or not-in-this-session), a decoded `0x00`/`0x80`
sentinel = ECU-side not-available (a real supported PID, not a decode bug). Never
collapse them into "no data."

**Session-gated DIDs ARE reachable — but not via `obd_request`.** Many ECUs gate
service `0x22` behind a non-default diagnostic session, and CANary can hold one:
`CanableMode22Transport` (used by `did_sweep` and `sweep_dids_batched`) enters the
session before reads and re-enters it every `keepalive_seconds`, and the OBD
scheduler runs its own enter/exit cycle. **Never hard-code the sub-function** —
`--session auto` resolves it from the vehicle's `data/dids.yaml` `access` block,
which is where a per-vehicle value belongs.

**Don't assume the keep-alive is TesterPresent.** `0x3E` every 1–2 s to reset the
S3 timer is the textbook pattern, but some ECUs reject `0x3E` outright, in which
case the session is held by re-sending `10 <sub-function>` periodically. Which one
applies is a per-vehicle fact: read it from the `access` block, and if it isn't
recorded there, determine it and record it rather than guessing.

What remains true everywhere: **`obd_request` is one-shot** and its allowlist
excludes `0x10` and `0x3E`, so an ad-hoc single read of a session-gated DID through
that route returns a negative response. Use a sweep/scheduler path for those — and
never report "unsupported" from an `obd_request` NRC alone when the DID may simply
be session-gated. That distinction is the difference between "this ECU lacks the
channel" and "you asked the wrong way."

**Batched Mode 22 is available** (`--batch`, up to 3 DIDs/request — the frame
ceiling for 2-byte identifiers, vs 6 for one-byte Mode 01 PIDs). Its value is
same-instant sampling, not speed: DIDs in one request share one ECU sample, where
sequential polls are rotation-sampled with unknown relative age. It requires
measured payload widths (`--widths-from` a prior snapshot) because a multi-DID
response is not self-delimiting. Support for batched `0x22` is itself per-ECU,
so verify it before relying on it, **on the raw response**: a positive `62` that
carries several DID groups. A batched sweep returning the same DID set as an
unbatched one proves nothing if the tool re-reads rejected groups one DID at a
time. That exact check once "verified" batching on an ECU that answers every
multi-DID request with `7F 22 12`.

## Reversing the broadcast bus

Bind a byte to a function through a **controlled physical action**, never by
inspection. In CANary this is one tool:

- **`can_activity`** for N seconds while you do one clean physical thing (blip the
  throttle, press the brake) — **tag what you physically did** (`tag_current_capture`
  / `record_vehicle_event`), per AGENTS.md. Its `changed_byte_indices` per
  arbitration ID answers "which ID and byte carry that signal" directly — no
  offline candump-and-diff. Restrict with `ids` once you have a candidate.

The underlying method (for offline logs or when explaining it): record → do one
action → play back to confirm → bisect the log to one ID → watch the changed byte.
A live grouped changed-byte view like `cansniffer` is the reference; `can_activity`
is CANary's equivalent for a bracketed window.

Decoding the field: expect **multi-byte big-endian** (`256·A+B`) with a scale/offset
to solve, or a **bitfield**. Values are usually **shifted/compressed** — raw hex is
not the reading (RPM may appear as `0xFA0`). The diagnostic PID for the same quantity
is a *different* encoding, but gives you a ground-truth value to solve the scale
against. Method detail: `references/reversing-broadcast.md`.

## Validation discipline — read before asserting any binding

Reverse-engineered bindings are where confident-wrong answers come from. The most
expensive error is **binding by matching aggregates** ("byte X's window-mean tracks
quantity Y's mean, r²=0.9"). It's almost always spurious and has been retracted many
times here. Guard:

- **Correlate same-instant values, never two aggregates.** Use `compare_channels` —
  it pairs same-instant samples itself and refuses too-few-pairs / barely-moving
  signals (the cases where an aggregate lies). That's why the method above hangs on a
  *controlled action*, not passive staring.
- **Reproduce a known answer first.** Strongest near-free check: a broadcast binding
  and the Mode-01 PID for the same quantity must agree **1:1** in engineering units —
  catches wrong offset/endianness/scale/unit with no prior "right answer."
  `tests/test_analysis_ground_truth.py` enforces exactly this. (Blind spot: slope is
  a ratio, so a +4% scale error reads as only −2.9% slope error — pin exact decode
  params separately.)
- **r² alone is not a binding.** Over one drive nearly everything correlates with RPM.

## Firmware / ROM reversing (outside CANary)

CANary does not do firmware analysis — this is external-tooling territory
(disassemblers, WinOLS). When the task crosses from "decode bytes on the wire" into
"understand or change what the ECU computes" — calibration tables, seed-key
recovery, OEM DID meanings — see `references/firmware-reversing.md` for the
front-door/backdoor tradeoffs, finding tables by their physics-determined shape, and
disassembly breadcrumbs.

## Vehicle-specific facts live elsewhere

This skill is deliberately ECU-agnostic — another CANary install may be on a
completely different vehicle. Gating sub-functions, keep-alive services, which
quantities exist on the broadcast bus, sensor fitment, and altitude are all
**per-vehicle** and belong in that vehicle's skill (e.g.
`vehicle-nissan-xterra-2008`) or, better, in `data/dids.yaml` / `data/bindings.yaml`
where tooling can read them. If you learn a fact about one truck, do not write it
here.

## Reference files
- `references/obd-uds-reference.md` — modes, PIDs + SAE scaling, Mode 09, UDS
  services, NRCs, DTC layout, CAN/ISO-TP framing. **The lookup tables.**
- `references/reversing-broadcast.md` — the record→act→bisect method and how it maps
  to `can_activity`; shifted-value decoding; external references (cansniffer, ICSim,
  OpenXC) as candidate features.
- `references/firmware-reversing.md` — ROM/firmware reversing (external tooling).
