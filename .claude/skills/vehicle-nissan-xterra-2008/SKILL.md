---
name: vehicle-nissan-xterra-2008
description: >-
  Vehicle-specific facts for the 2008 Nissan Xterra (VQ40DE, manual 6-speed) —
  the development vehicle for this CANary install, profile slug
  `nissan-xterra-2008`. Use this whenever a task touches THIS truck's ECU or
  telemetry: which diagnostic session gates Mode 22, what the keep-alive is,
  which quantities are and are not on its broadcast bus, what its UpRev K-line
  logger does, and the physical facts (no MAP sensor, ~80 kPa operating
  altitude, no TCM) that change how its data must be read. Load it alongside
  `ecu-reverse-engineering` (generic decode method) or `engine-management`
  (generic engine behaviour) — those two stay vehicle-agnostic on purpose, and
  this file is where the Xterra-isms live. Do NOT apply anything here to
  another vehicle.
---

# 2008 Nissan Xterra — vehicle-specific ECU facts

Profile slug `nissan-xterra-2008`. Engine VQ40DE, 4.0 V6, port injection,
naturally aspirated, drive-by-wire. **Manual 6-speed — there is no TCM on the
bus**, so skip TCM steps in any capture protocol.

**This file is per-vehicle. Another CANary user may be on a completely
different ECU.** Generic method belongs in `ecu-reverse-engineering` /
`engine-management`; if you learn something here that is true of ECUs in
general, put it there instead.

## Planned: supercharger build (from late 2026-09)

The engine is about to change: Z1 Level 2 supercharger (Harrop HTV1900),
Aeromotive 340 pump assembly, Bosch 700 cc injectors, 75 mm throttle body, and a cone
intake. **Check which side of that boundary a session is on before comparing
anything.** NA baselines, open questions, and the first-start plan are in
`docs/roadmap/supercharger-build.md`. Boost and fuel pressure are not readable
from this ECM (PIDs 0x0B, 0x0A, 0x22, 0x23, 0x59, 0x43 all unsupported).

## Canonical homes — check these before trusting anything below

Values drift; data files are the truth. This file exists for facts that have no
structured home, and it **points at** the ones that do rather than copying them.

| Fact | Lives in |
|---|---|
| Broadcast byte→signal bindings, scales, evidence | `data/bindings.yaml` |
| Quantities searched for and NOT found on the bus | `data/bindings.yaml` → `absent_signals` |
| Mode 22 DID catalogue, decode params | `data/dids.yaml` |
| Gating session + keep-alive sub-function | `data/dids.yaml` → `access` |
| Which PIDs the ECM actually answers | `get_obd_coverage` (live) |
| Per-vehicle partitioning of all learned state | `src/profiles.py`, keyed by VIN |

## Diagnostic access

- **Mode 22 is gated behind diagnostic session `0xC0`.** Reads outside it come
  back negative. `did_sweep --session auto` resolves this from the `access`
  block — don't hard-code it at a call site.
- **TesterPresent (`0x3E`) is REJECTED by this ECM.** The session is held by
  re-sending `10 C0` periodically instead. The textbook `0x3E`-every-1–2 s
  keep-alive does not work here — reaching for it is a known dead end.
- The OBD scheduler runs a `10 C0` → `10 81` cycle roughly every 60 s.
- **Batched Mode 22 is REJECTED — one DID per request.** Raw frames,
  2026-09-17, engine running, `10 C0` held: `03 22 12 01` → `05 62 12 01 00
  A2`, but `05 22 12 01 12 03` and `07 22 12 01 12 03 12 04` both → `03 7F 22
  12`. The earlier "verified 2026-08-08" claim rested on batched and unbatched
  sweeps returning the same 138-DID set. `sweep_dids_batched` silently re-read
  every rejected group one DID at a time, so that match could not tell success
  from fallback. UpRev's own two-DID `22 11 03 04 01` is rejected the same way
  (see below). Don't plan on same-instant multi-DID sampling here.
- **No MAP sensor.** The VQ40DE is mass-airflow based, not speed-density.
  **Mode 01 PID `0x0B` is unsupported** — never reach for it as a reference.
  `BAROMETRIC_PRESSURE` (`0x33`) and `EVAP_VAPOR_PRESSURE` (`0x32`) *are*
  supported. A "manifold pressure" label on any byte of this vehicle is wrong
  by construction; that mistake sat in `bindings.yaml` for three months.
- DID space: **138 of 512 DIDs in `0x1100`–`0x12FF` answer** at idle; **27 of
  them vary** at constant idle (the live engine channels). The rest are static
  configuration/status. Sweep cost is ~1.9 s over the whole range; the 138
  answering DIDs alone take ~0.2 s at full speed, or 1.17 s paced at ~118 req/s.

## Broadcast bus

500 kbit/s, ~1640 frames/s at idle, 27 distinct IDs passively (29 when CANary
is also polling — `7DF` and `7E8` are the two extras).

**Confirmed absent from the broadcast bus** (well-powered no-load sweep,
2026-08-08 — see `absent_signals` for the range each search covered): ignition
timing, wideband AFR, intake cam timing, injector pulse width, base fuel
schedule, calculated load, and any fine-grained MAF. If you need those, Mode 22
or the UpRev K-line log are the only sources. **Caveat: that sweep was no-load
only**, so a signal appearing only under road load could still have been missed.

`ECM_RPM` (`0x1F9` bytes 2–3 big-endian, ×0.125) is the strongest control for
validating any time alignment or decode chain — it fits slope 1.0000 against
UpRev RPM with scale stability 0.9985–1.0011 across 750–3700 rpm.

## K-line (OBD pin 7)

Read off UpRev's own session with the CANary-HAT sniffing, then reproduced by
the HAT alone (2026-09-23, KOEO). Details and the full exchange:
`docs/uprev-usb-kline-research.md` → "On-wire view".

- **10400 baud 8N1, KWP2000 (ISO 14230).** ECM address **`0x10`**.
- **Tester address `0xFC`, not the ISO 14230-4 default `0xF1`.** A
  StartCommunication from `0xF1` (or to `0x33`/`0x11`/`0x01`/`0x12`) gets
  silence — which reads exactly like dead hardware.
- **The standard 25 ms fast init is ignored.** The wake-up this ECM answers is
  **518.9 ms low, then 18.1 ms high**, then `81 10 FC 81 0E` sent back-to-back
  → `83 FC 10 C1 5D 8F 3C` (key bytes `5D 8F`).
- After StartCommunication, frames are `<len> <data…> <sum & 0xff>` with no
  address bytes. StopCommunication is `01 82 83` → `01 C2 C3`.
- **Known answer:** `05 22 11 03 04 01 40` → `04 62 11 03 <value> <cs>` (value
  0x99–0x9A at KOEO). The same two-DID request is rejected on CAN (below); on
  K-line it is answered.
- **One diagnostic session, two transports.** The ECM's session state is
  shared between CAN and K-line: put CANary in passive mode
  (`POST /control/passive` with a TTL) while anything else owns K-line.

## The UpRev logger on this truck

- **Its logging is K-line (pins 7/15), not CAN.** With CANary passive it emits
  *zero* CAN frames. Broadcast logging alongside it is completely safe.
- If CANary transmits, UpRev reacts on CAN: it flow-controls CANary's
  multi-frame Mode 06 responses on `7E8`, and may fire its own `22 11 03 04 01`
  reads which this ECM rejects. Harmless to UpRev's logging, but it means both
  tools share the `7E0`/`7E8` endpoint. `POST /control/passive` gates every
  CANary transmit for simultaneous windows.
- **UpRev dies on its own every few minutes.** Confirmed independent of
  CANary — the operator reproduced it with no Y-cable and no CANary present.
  Do not diagnose this as bus interference. Keep windows ≤ ~3 minutes.
- **It is rotation-polled: channels do NOT share a timestamp.** Aligning a CSV
  to CAN on RPM leaves other channels skewed — MAF sits **+0.10 s** later than
  RPM. Always scan a per-channel lag before concluding a channel has no
  broadcast home; ignoring this nearly killed a valid airflow binding.
- CSV `Time` is milliseconds from log start; the filename timestamp is t₀
  (verified against file mtime). It logs to memory — the operator saves the CSV
  manually, so mtime is the save time, not the stop time.
- Aligning by RPM cross-correlation **fails at idle** (CANary polls RPM every
  5–7 s and idle blips alias). Use a rev sweep, or decode RPM off `0x1F9` for a
  ~100 Hz reference.

## Physical / environmental

- **Operating altitude ~80 kPa ambient (~2072 m).** Never assume sea level: it
  fakes a ~20 % "restriction" in airflow and power math, and drops absolute
  load from ~0.93 to ~0.73 on a perfectly healthy engine. Compare VE or
  `MAP ÷ measured baro`, never raw g/s or absolute load.
- **A/C cannot run with the blower off** by design. The reachable states are
  both-off, blower-only, and blower+A/C — which is still enough to separate
  blower bits from A/C bits by nesting.
- `LONG_FUEL_TRIM` reading flat 0.0 % is the ECU returning the `0x80` sentinel,
  not a decode bug and not a fault. It needs a warm steady-state relearn cycle.
