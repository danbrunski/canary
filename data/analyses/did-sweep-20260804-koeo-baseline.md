# Mode 22 DID baseline — KOEO, 2026-08-04

Snapshot: `did-sweep-20260804-koeo-baseline.json` (`tools/did_sweep.py`,
range `0x1000`–`0x1400`, session `0xC0`, header `7E0`). **158 DIDs respond.**

## Why this exists

The UpRev K-line logger has been unreliable mid-drive, so the question was
whether its channels could be read over CAN instead. They can — the ECM's
CONSULT DATA MONITOR set (`data/fsm/signals.json`, EC-431, 65 ECM params) is
served over CAN via Mode 22, and it contains the channels the tuning campaign
actually needs:

| UpRev channel | FSM DATA MONITOR param | DID |
|---|---|---|
| `INTK CAM TIM-B1/B2` | `INT/V TIM (B1)` / `(B2)` | **unidentified** |
| `B-FUEL SCHDL (ms)` | `B/FUEL SCHDL` | **unidentified** |
| `MAS A/F -B1 (V)` | `MAS A/F SE-B1` | unidentified |
| `A/F WB-B1 (V)` | `A/F SEN1 (B1)` | `0x1225` (known) |
| `CAL/LD VALUE (%)` | `CAL/LD VALUE` | unidentified |
| `IGN TIMING (BTDC)` | `IGN TIMING` | Mode 01 `0x0E` |

Note `INJ PULSE-B1` and `B/FUEL SCHDL` are **separate** FSM parameters;
`ECM_INJECTOR_PW_B1` (`0x1208`) is the former, so it is not automatically the
latter. Do not assume they are the same signal.

`TARGET AFR` is **not** in the FSM DATA MONITOR list, so it is most likely
computed by UpRev from the ROM fuel-target table rather than read from the
ECM — the one channel that may be genuinely tool-side. Treat that as a
hypothesis; it has not been tested.

## The point of a KOEO baseline: the jitter floor

Two sweeps 30 s apart, engine off, ignition on: **153 of 158 DIDs byte-identical,
5 jittering.**

| DID | A → B | note |
|---|---|---|
| `0x1106` | `71` → `73` | |
| `0x1114` | `15` → `16` | |
| `0x1204` | `004c` → `004d` | |
| `0x1223` | `01b6` → `01a3` | analog, largest swing |
| `0x1225` | `01b8` → `01b7` | known `ECM_AF_SENSOR_V_B1` (2.20 → 2.195 V) |

That is the noise floor for the next step, and it is remarkably low. **Any DID
that differs between this baseline and an engine-running sweep, other than
these five, is engine-state dependent** — which makes the identification
almost a pure set difference rather than a correlation hunt.

## Next step (needs the engine running, ~2 minutes)

1. `tools/did_sweep.py --start 0x1000 --end 0x1400 --label idle-warm --out …`
2. Same again while holding ~2500–3000 rpm (`--label rev`), because intake cam
   advance is what separates `INT/V TIM` from every other warm-idle signal —
   at idle it parks near 0 ° and cannot be told apart by value.
3. Diff idle-warm vs this baseline → engine-dependent DIDs. Diff rev vs
   idle-warm → the cam-timing and load/fuel-schedule candidates.
4. Confirm scale/offset against a simultaneous UpRev log using the acceptance
   gates in `docs/can-re-workflow.md` ("Ground truth from a second logger") —
   out-of-sample, partial correlation given RPM, and above all **scale
   stability across two windows**.

A rev sweep costs ~1.5 s of bus time per pass, so several passes at different
rpm are cheap. Take them.

## Session-exclusivity bench (same evening, KOEO) — the architecture verdict

Measured with the daemon's TX gated (`/control/passive`), physical `7E0`:

| question | answer |
|---|---|
| Mode 01 inside session 0xC0 | **hard-blocked** — 0/10, functional and physical |
| sustained 0x22 rate in-session | **658 reads/s** (4-DID rotation, 8 s, 0 failures, p50 1.09 ms) |
| does polling keep the session alive | yes — no explicit keepalive needed while reading |
| idle session lapse | alive at 3 s gap, lapsed at 5 s |
| exit `10 01` → Mode 01 back | **no** — dead >30 s (recovers within minutes) |
| lapse → Mode 01 back | **no** — blackout outlives the session, >30 s |
| exit `10 81` → Mode 01 back | **instant** (0.0 s, next request) |
| session enter cost | 1.0 ms |

**Verdict: run tuning captures entirely inside session 0xC0 (all-DID).**
658 reads/s means 8 channels at 20 Hz costs a quarter of the measured
capacity. A Mode 01/Mode 22 interleave was benched too and is not worth the
complexity — and with `10 01` as the exit it produced *zero* Mode 01 readings.
`10 81` is the only exit that restores Mode 01 promptly; anything that opens
0xC0 must exit through it (the daemon's DID-window code already does).

During an all-DID capture, Mode 01 is dark by ECU design. Broadcast CAN keeps
flowing (RPM/pedal/throttle at ~100 Hz, free), so `vehicle_state` should
classify from decoded broadcast channels during captures rather than OBD
freshness — which is also the principled fix for the "tuning mode reads
`no_data`" bug.

Remaining channel gaps for a full UpRev-replacement capture, all DID-side:
`INT/V TIM (B1)/(B2)`, `B/FUEL SCHDL`, `IGN TIMING` (all in the FSM DATA
MONITOR list, DIDs unidentified — the engine-running sweep finds them), plus
an A/F-voltage → AFR transform, which the existing UpRev logs calibrate for
free (`A/F WB-B1 (V)` and `AFR WB-B1` are paired columns in the same rows).
Target AFR: `COMMANDED_EQUIV_RATIO` is Mode 01-only (PID 0x44), so in an
all-DID capture the commanded side must come from a DID if one exists, or be
snapshotted before/after the capture window.

## RETRACTED lead: "UpRev streams via 0xAC + 0x23"

An earlier revision of this file claimed the flash log showed UpRev defining a
parameter block with `AC 81`/`AC 82` and streaming it back via service 0x23,
and that replicating it would beat the per-DID plan. **That was wrong twice
over, and the second error is the instructive one.**

**Wrong on the evidence.** Reassembling the ISO-TP properly (`src/analysis/iso_tp.py`,
which already existed) shows both `AC` requests were **refused — `7F AC 11`,
serviceNotSupported**, as were `A1`/`A2` (`7F A1 11`) and `23 A100`/`23 A200`
(`7F 23 12`). I read request payloads off raw frames and never checked the
replies. The tool was *probing* and being told no.

**Wrong on the premise.** That capture is a *reflash* session; it contains no
datalogging at all — 350 ISO-TP messages in 138 s on the ECM channel, ~2.5/s.
The bulk 742/743 traffic is the ROM transfer.

### What settles it: UpRev datalogging does not use CAN

The sniff log ran 17:41:13 for 76 s on 2026-07-26, and
`UpRev_Log7-26-2026_5-41-32_PM.csv` starts at 17:41:32 — 19 s inside it. So a
full UpRev logging session is bracketed by a full-bus CAN capture. In the 32 s
where UpRev recorded **518 rows at 16.2 Hz across 17 channels**, the CAN bus
carried **51,985 frames and not one diagnostic frame**: zero `7DF`, `7E0`,
`7E8`, `742`, `743`. Only broadcast IDs, plus an unrelated periodic `0x794`
(`03 28 NN`, ~98 Hz, present with or without UpRev).

**UpRev logs over the physical K-line, which a CAN interface cannot see.** Its
streaming protocol is therefore not discoverable by sniffing CAN — not with a
better capture, not ever. Do not spend more time trying.

### Two things this does establish

1. **Service 0x23 ReadMemoryByAddress works on this ECM over CAN.** Confirmed
   format `23 <4-byte address> <2-byte length>`, answered with `63` + the bytes:
   `23 00080000 0010`, `23 000C0000 0010` and `23 00020004 0010` each returned
   16 bytes; `23 00100000 0010` gave NRC 0x22 (conditionsNotCorrect), so the
   address space is gated, not open. This is a genuine packed-multi-value read
   and it is the only mechanism seen that could beat per-DID polling — but it
   needs the RAM address of each parameter, which UpRev has from its ROM
   definitions and we do not. Filed as a capability, not a plan.
2. **The Pi and UpRev can run simultaneously.** They did, right here, for 32 s.
   So the 30-minute CAN blackout on 2026-07-29 was the OBD connector being
   physically unplugged to make room for the UpRev cable, not protocol
   contention. A splitter or second tap makes paired ground-truth capture
   repeatable — which is what the binding work is short of (171 s total).

**The all-DID plan above stands unchanged.** It never depended on this lead,
and 658 reads/s inside session 0xC0 is measured, not inferred.
