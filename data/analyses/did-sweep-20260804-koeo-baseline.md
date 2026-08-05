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
