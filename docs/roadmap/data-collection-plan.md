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
8. **Mode 22 DID exploration** — DONE for the engine ECM (sessions
   2026-05-16T214006 / T223328). Key facts now established and tooled:
   - The 2008 Nissan ECM **gates Mode 22 behind UDS session `10 C0`**
     (`7F 22 80` otherwise); `3E 00` TesterPresent is rejected, so the
     session is kept alive by re-sending `10 C0` every ~2 s. Session
     `0xC0` is **mutually exclusive with standard OBD Mode 01**.
   - `tools/scan_constant_dids.py --canable --session C0` does the
     Canable-native sweep + constant-vs-varying classification (no ELM327;
     early-returns on negative responses so a full sweep is minutes).
   - **Prize captured: injector pulse width = DID `0x1208`** (2-byte,
     scale ≈ raw×4e-4 ms, exact constant TBD). Plus RPM `0x1219`, coolant
     `0x1101`, IAT `0x123F`, TPS `0x120F/10`, A/F sensor `0x1225/26`, VVT
     `0x1110`, A/F-heater `0x1107..0A`. See `data/analyses/2026-05-16T223328.md`
     for the full DID map. Remaining: calibrate the `0x1208` scale and
     structure the map as `data/dids.yaml` (parallel to `bindings.yaml`).

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

### Status (2026-05-17; moved to docs/roadmap/ 2026-07-18) — pipeline proven, corpus not yet sufficient

> 2026-07-18 note: the software-prep checklist below is essentially
> complete and the models have since been retrained on a multi-session
> corpus (see `docs/implementation-tasks.md` Phases 5/7/9). The part of
> this doc that remains open — and the reason it lives in roadmap/ — is
> the **drive-capture backlog** in §2: cold-start, load-sweep, off-road,
> and A/C sessions are still the corpus gaps that matter.

- **Isolation Forest baseline trained** (`models/isolation_forest/`, gitignored
  binary + tracked manifest) on the single 18-min drive `2026-05-11T222447`:
  664 samples, **184 decoded features** (incl. the `dbc:` broadcast bindings
  via the emitted DBC). This proves the full pipeline end-to-end
  (frames → bindings/DBC decode → features → scaler → IF) but is **not a
  deployable detector**: one session, single operating mode (`all` — no
  `obd:` PID columns to segment idle/city/highway/wot), and **unvalidated**
  (no FPR — `fp_benchmark` is LSTM-only; no LSTM trained). Treat as a
  smoke-test artifact, not Phase 2 done.
- **Behavior-model refit blocked**: all 5 recipes skip — the recent corpus is
  Mode-22/broadcast-only (no `obd:` Mode 01 PID columns), a direct
  consequence of the DID-focused sessions (Mode 22 ⊥ Mode 01). Needs the
  drive backlog above *with parallel Mode 01 logging* (`--obd-source canable`,
  not in a `0xC0` DID session).

## 4. Software prep status

- [x] Expanded `--obd-pids` default in `scripts/record.py`.
- [x] `drive` protocol in `scripts/guided_session.py` (`--protocol drive`).
- [x] `tools/cross_reference_broadcast.py` — regress broadcast-CAN bytes against
  Mode 01 PIDs (the byte↔PID counterpart to the Mode 22↔Mode 01 tool); reads
  `bindings.yaml` and reports which PID each unpinned signal tracks + scale.
- [x] `data/sessions.md` — running index of captured sessions.
- [x] `tools/scan_constant_dids.py` — Mode 22 DID sweep + constant-vs-varying
  classification (Phase 4.E.5). Canable-native (`--canable`) or legacy ELM327
  (`--port`); `--session C0` enters + keeps alive the Nissan diagnostic
  session that gates 0x22; early-returns on negative responses.
- [x] `scripts/record.py --obd-dids` — logs selected Mode 22 DIDs alongside
  Mode 01 during a legacy ELM-backed drive.
- [x] `src/collection/can_obd_client.py` + `tools/can_obd_probe.py` — read-only
  Canable-native Mode 01 / 03 / 09 one-shot client.
- [x] `src/collection/can_obd_collector.py` — one-bus Canable streaming
  producer for broadcast CAN + OBD Mode 01 + Mode 22 DIDs.
- [x] Config/daemon/recorder wiring: `collection.obd_source: canable` is the
  default, and `scripts/record.py --obd-source canable` writes
  `frames.log` + `pids.jsonl` without an ELM327.
- [x] Add a Canable-native broad DID sweep CLI to replace the legacy
  `tools/scan_constant_dids.py --port ...` ELM transport for exploratory scans.
  Already landed (commits `15fd8d5`/`eb90d1b`, 2026-05-16) — this box
  was simply left unchecked by the later doc-reorg pass (`b46a759`).
  `--canable` builds `CanObdClient` + `CanableMode22Transport`
  directly over the CAN bus, not routed through ELM327; independently
  confirmed exercised by `tests/test_did_scan.py`/
  `tests/test_scan_constant_dids.py`'s Canable-specific cases.

### Capture-readiness hardening (done 2026-05-16)

The drive backlog is the blocker; these remove the ways a drive silently
fails to produce usable data. Operational checklist: `docs/capture-runbook.md`.

- [x] **Recorder auto-reconnect parity.** `record_can_obd` (the
  `--obd-source canable` path) now rebuilds the bus on a mid-drive USB
  drop into the *same* continuous `frames.log`, with a `max_reconnects`
  cap (`reconnect_exhausted`) — the resilience the CAN-only `record()` and
  the daemon path already had. Previously a single `OSError` ended the drive.
- [x] **`scripts/preflight.py`** — go/no-go before a capture: config,
  disk, `can0` up at bitrate, frames arriving, and **ECU answers Mode 01**
  (catches the `0xC0`-DID-session trap that caused the Mode-22-only corpus).
- [x] **Auto-recovering `can0`** — `systemd/can0.service` +
  `systemd/70-canable-can0.rules` + idempotent `scripts/canup.sh` bring the
  link back after reboot / USB re-enumeration without a human.
