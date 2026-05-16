# Data-collection plan — what we need before ML, anomaly detection, and behavior-model fitting

This is the acquisition roadmap that gets us from "the rig works" to "we can
train the Phase 2/3 detectors and fit the Phase 4.E behavior surfaces (fuel /
ignition / VE / warmup)." It complements task **1.16** (diverse drives) and the
Phase **4.E** tasks; `docs/behavior-model-inference.md` covers the modeling
approach itself.

## Where we are

The original ELM327 path proved the RE loop and produced the first confirmed
bindings (`ECM_RPM` at 0x1F9:2-3, raw/8; `ECM_COOLANT_TEMP` at 0x551:0,
raw−40), but that adapter is now dead. The active hardware path is the Canable:
it has already completed a 13.6 minute stationary stability run with zero parse
errors, and `tools/can_obd_probe.py` successfully walked 41 supported Mode 01
PIDs and read Mode 03 DTCs over the Canable-native OBD client.

The streaming layer is now in place: `src/collection/can_obd_collector.py`
owns one Canable Bus, records broadcast CAN frames, interleaves read-only Mode
01 / Mode 22 requests, and emits the same `TelemetryRecord` / `pids.jsonl`
shape as the legacy ELM path. Captured sessions remain insufficient for model
validation: a handful of short/parked sessions, one 18-minute drive, three
2026-05-13 rig sessions, and the 2026-05-16 stationary binding sweep.

## 1. Expanded PID set

This is the target set for every OBD-backed capture. The Canable-native path
uses `obd_pids: [auto]` by default, which walks the ECU's supported-PID bitmask
and polls every PID CANary has a decoder for. Pass an explicit list to
`scripts/record.py --obd-source canable --obd-pids ...` for a narrower/faster
drive.

```
RPM, SPEED, COOLANT_TEMP, INTAKE_TEMP, INTAKE_PRESSURE (MAP), MAF, THROTTLE_POS,
ENGINE_LOAD, TIMING_ADVANCE, SHORT_FUEL_TRIM_1, LONG_FUEL_TRIM_1,
SHORT_FUEL_TRIM_2, LONG_FUEL_TRIM_2, O2_B1S1, O2_B2S1, BAROMETRIC_PRESSURE,
CONTROL_MODULE_VOLTAGE, RUN_TIME, FUEL_STATUS
```

~19 PIDs at the 1 Hz resample cadence is enough for the ML and behavior-model
pipeline. The Canable-native collector should auto-discover supported PIDs,
skip unsupported ones, and log the same `pids.jsonl` / telemetry rows the legacy
ELM path produced so downstream tooling stays unchanged.

**Narrowband-O2 caveat.** The Xterra's O2 sensors are narrowband — rich/lean of
stoich only, not absolute AFR. A true AFR map needs a wideband sensor (separate
hardware). What we *can* map, and the right anomaly target anyway, is the
**fuel-trim surface** (STFT+LTFT vs RPM×load): a healthy engine sits near zero
trim everywhere; deviations flag vacuum leaks, MAF drift, injector fouling, etc.
The ground-truth *fuel-delivery* signal is the **injector pulse width** — not on
generic Mode 01, but reachable as a Nissan Mode 22 DID (see capture 8 below).

## 2. Captures still needed (prioritized)

Run these with `scripts/record.py --obd-source canable` and the expanded OBD PID
set. Ideally tag with `scripts/guided_session.py --protocol drive` or at
minimum set `--vehicle-state`.

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
   tracks `RPM × (1 + STFT/100)` (Phase 4.E.4). Use
   `scripts/record.py --obd-source canable --obd-dids ...` for selected
   candidates during a drive. Broad exploratory DID sweeping still needs a
   Canable-native replacement for `tools/scan_constant_dids.py`.

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
- [x] `tools/scan_constant_dids.py` — Mode 22 DID sweep over the legacy ELM327
  path (Phase 4.E.5).
- [x] `scripts/record.py --obd-dids` — logs selected Mode 22 DIDs alongside
  Mode 01 during a legacy ELM-backed drive.
- [x] `src/collection/can_obd_client.py` + `tools/can_obd_probe.py` — read-only
  Canable-native Mode 01 / 03 / 09 one-shot client.
- [x] `src/collection/can_obd_collector.py` — one-bus Canable streaming
  producer for broadcast CAN + OBD Mode 01 + Mode 22 DIDs.
- [x] Config/daemon/recorder wiring: `collection.obd_source: canable` is the
  default, and `scripts/record.py --obd-source canable` writes
  `frames.log` + `pids.jsonl` without an ELM327.
- [ ] Add a Canable-native broad DID sweep CLI to replace the legacy
  `tools/scan_constant_dids.py --port ...` ELM transport for exploratory scans.
