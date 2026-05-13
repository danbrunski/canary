# Data-collection plan — what we need before ML, anomaly detection, and behavior-model fitting

This is the acquisition roadmap that gets us from "the rig works" to "we can
train the Phase 2/3 detectors and fit the Phase 4.E behavior surfaces (fuel /
ignition / VE / warmup)." It complements task **1.16** (diverse drives) and the
Phase **4.E** tasks; `docs/behavior-model-inference.md` covers the modeling
approach itself.

## Where we are

The ELM327 (CH340 clone, protocol 6 ISO 15765-4 CAN 11/500, ~125 PIDs/MIDs)
works and `scripts/record.py --obd-port` captures Mode 01 PIDs alongside the
Canable's broadcast capture, robust to the adapter dropping out. Confirmed
bindings so far: `ECM_RPM` (0x1F9:2-3, raw/8), `ECM_COOLANT_TEMP` (0x551:0,
raw−40), plus ~24 more at lower confidence. Captured sessions: a handful of
short/parked ones plus the 18-min drive 2026-05-11T222447 and the three
2026-05-13 rig-bring-up sessions. **Not enough yet** — the model layer wants
many more hours across many more conditions.

## 1. Expanded PID set (in the recorder)

`scripts/record.py` defaults `--obd-pids` to a fuller set so every capture
feeds both the binding cross-reference and the ML/mapping features:

```
RPM, SPEED, COOLANT_TEMP, INTAKE_TEMP, INTAKE_PRESSURE (MAP), MAF, THROTTLE_POS,
ENGINE_LOAD, TIMING_ADVANCE, SHORT_FUEL_TRIM_1, LONG_FUEL_TRIM_1,
SHORT_FUEL_TRIM_2, LONG_FUEL_TRIM_2, O2_B1S1, O2_B2S1, BAROMETRIC_PRESSURE,
CONTROL_MODULE_VOLTAGE, RUN_TIME, FUEL_STATUS
```

~19 PIDs through one ELM327 → roughly 1 Hz per PID, which is fine at the 1 Hz
resample the pipeline uses. PIDs the ECU doesn't support are warned-and-skipped.
Override with `--obd-pids` for a leaner/faster set on transient-heavy segments.

**Narrowband-O2 caveat.** The Xterra's O2 sensors are narrowband — rich/lean of
stoich only, not absolute AFR. A true AFR map needs a wideband sensor (separate
hardware). What we *can* map, and the right anomaly target anyway, is the
**fuel-trim surface** (STFT+LTFT vs RPM×load): a healthy engine sits near zero
trim everywhere; deviations flag vacuum leaks, MAF drift, injector fouling, etc.
The ground-truth *fuel-delivery* signal is the **injector pulse width** — not on
generic Mode 01, but reachable as a Nissan Mode 22 DID (see capture 8 below).

## 2. Captures still needed (prioritized)

All with `scripts/record.py --obd-port /dev/ttyUSB0`, and ideally tagged via
`scripts/guided_session.py --protocol drive` (or at minimum `--vehicle-state`).

1. **Cold-start session** — first start of the day, engine genuinely cold. Idle
   until coolant reaches operating temp (~5–10 min), then a short drive.
   → warmup curve, cold-enrichment fueling, open→closed-loop transition. Can't
   be faked; needs an actually-cold engine.
2. **Warm idle baseline** — fully warm, idle 5–10 min doing nothing. → the
   "nominal idle" reference (RPM stability, trims at idle).
3. **City drive** — 20–40 min stop-and-go on surface streets. → accel/decel
   transients, low-to-moderate load.
4. **Highway drive** — 20–40 min sustained ~65–75 mph + on/off-ramp accels. →
   steady-state high-ish load, plus DFCO (fuel cutoff) on closed-throttle
   coast-down.
5. **Load-sweep / grid-coverage drive** — the key one for surface fitting.
   Deliberately dwell at a grid of (RPM × load) points: 2nd gear ~2000 rpm
   light throttle ~15 s, ~3000 rpm moderate ~15 s, a few WOT pulls through the
   gears, long DFCO coasts. The fuel/timing surfaces are only as good as the
   grid coverage and dwell time.
6. **Off-road / 4-Lo** (if accessible) — crawl in 4WD low. → a distinct
   operating mode (low speed, high throttle, transfer-case state); also
   confirms the 4WD/transfer bindings (e.g. 0x354:1).
7. **A/C on↔off pairs** on the same segment — the compressor load shifts
   MAP/trim/idle. → high-load points + confirms `AC_COMPRESSOR_STATE`,
   `MOTION_29E_5`.
8. **Mode 22 DID exploration** — sweep Nissan Consult DIDs while running, logged
   alongside Mode 01, then `tools/cross_reference.py` (Mode 22 ↔ Mode 01
   regression) identifies them. Prize: **injector pulse width** = the DID that
   tracks `RPM × (1 + STFT/100)` (Phase 4.E.4). Needs the ELM327 doing Mode 22
   reads — extend the recorder's OBD side to also poll a DID list, or build
   `tools/scan_constant_dids.py` (Phase 4.E.5, not built yet).

Opportunistic extras: towing / heavy load (pushes into the high-load corner an
empty truck on flat ground can't reach); a tagged drive specifically for
chassis signals (steering through corners, hard braking) to pin
`STEERING_ANGLE_002` / the brake candidates.

## 3. How much before we can start

You don't need all of it.

- **Isolation Forest (Phase 2) + a first behavior-surface fit:** the expanded
  PID set + cold-start + warm-idle + one city drive + one highway drive + the
  load-sweep drive — ~5 sessions / ~2 hours.
- **LSTM autoencoder (Phase 3):** hungrier — figure ~10–15 drives / 5–10 hours
  total across all the conditions above.
- **Behavior surfaces (fuel/ignition/VE):** a handful of well-covered drives,
  the load-sweep one mattering most; warmup curve from the cold-start sessions.

Every new drive also sharpens the CAN bindings (more PIDs → more
cross-reference → more decoded signals become ML features), so it compounds.

## 4. Software prep status

- [x] Expanded `--obd-pids` default in `scripts/record.py`.
- [x] `drive` protocol in `scripts/guided_session.py` (`--protocol drive`).
- [x] `tools/cross_reference_broadcast.py` — regress broadcast-CAN bytes against
  Mode 01 PIDs (the byte↔PID counterpart to the Mode 22↔Mode 01 tool); reads
  `bindings.yaml` and reports which PID each unpinned signal tracks + scale.
- [x] `data/sessions.md` — running index of captured sessions.
- [ ] `tools/scan_constant_dids.py` — Mode 22 DID sweep (Phase 4.E.5).
- [ ] Extend the recorder (or a separate poller) to log Mode 22 DIDs alongside
  Mode 01 during a drive, so capture 8 above is one command.
