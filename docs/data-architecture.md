# Data architecture — where each signal comes from, and which path is canonical

What's flowing on the wire, what each interface can see, and which interfaces
the daemon depends on for **production** (live anomaly detection on a
running vehicle) vs **RE** (reverse-engineering the proprietary signals
so production has something to decode against).

Worth pinning explicitly because the four interfaces — Canable broadcast
sniffing, Canable-native OBD, legacy ELM327, NT510, Mode 22 DIDs — are *not*
interchangeable, and over-investing in
the wrong layer is the easiest way to waste effort.

## The four data sources

### 1. Broadcast CAN (Canable, passive sniffer) — *the production primary*

The ECU emits broadcast frames every 10–100 ms whether anyone is asking
or not: `0x1F9` (RPM), `0x551` (coolant), `0x231/0x233/0x23D` (engine
state), `0x002` (steering / motion), and ~50 other IDs on the Xterra.
These carry the bulk of live engine telemetry. A passive sniffer sees
them *for free* — no requests, no contention.

What the Canable picks up: every frame on the bus. Decoded into named
signals via `data/bindings.yaml` + `src/decode/bindings.py`. The shape
of one binding entry:

```yaml
- name: ECM_COOLANT_TEMP
  can_id: 0x551
  byte_start: 0
  byte_length: 1
  scale: 1.0
  offset: -40.0
  unit: degC
  confidence: confirmed
```

Once a binding is `confirmed` (or `high`), the daemon's live preprocessing
pipeline produces a `decoded:ECM_COOLANT_TEMP` numeric column on every
tick — same column the IF / LSTM consume as a feature, same column the
dashboard renders in engineering units.

**Production code path:** `record.py` (or `src/main.py` daemon) →
`src/collection/can_collector.py` → `src/preprocessing/live.py` →
inference.

**Strengths:** zero polling cost, no contention, deterministic cadence,
high refresh rate (10–100 Hz per signal). The right primary for live
anomaly detection.

**Weaknesses:** proprietary. The mapping byte → signal is per-vehicle
and per-model-year, and we have to reverse-engineer it. This is the
work of `docs/can-re-workflow.md`.

### 2. OBD-II Mode 01 PIDs (Canable-native or legacy ELM327, request-response) — *ground truth and supplemental telemetry*

OBD-II is standardized: every compliant ECU responds to a Mode 01
request for PID 0x0C with the same RPM-in-known-units format. That's
extremely useful for **ground truth** during RE — "I see byte 2 of
0x1F9 rising; Mode 01 PID 0x0C says 700 rpm; regression gives
r²=1.0; that byte *is* RPM." It's what pinned `ECM_RPM` to
`confirmed`.

But it's **request-response**, not broadcast. The ECU doesn't emit Mode
01 responses spontaneously; some tool has to ask. Historically that was the
ELM327. Current hardware development is moving this onto the Canable-native
ISO-TP client because the ELM327 has failed.

**Where it lives:** `src/collection/obd_collector.py`,
`scripts/record.py --obd-port` for the legacy ELM327 path;
`src/collection/can_obd_client.py` and `tools/can_obd_probe.py` for the
Canable-native path.

**Strengths:** standardized scaling and units; portable across vehicles
without an RE pass; the ECU's *intended* diagnostic interface.

**Weaknesses for production:**
* Polling round-robins through every watched PID, so per-PID refresh rate
  is roughly `budget / N_pids` Hz. At 42 requests against the default
  8 req/s budget that is 0.06–0.5 Hz per channel — far slower than
  broadcast. **This is a self-imposed pacing choice, not a bus or ECU
  limit** (see "How fast Mode 01 can actually go" below).
* The legacy ELM327 path requires an extra adapter plugged into the OBD-II port.
* CH340-based clones over WSL-USB are flaky (the supervised reconnect
  loop in 1.E.1 exists to deal with this).
* The Canable-native streaming path now owns one Bus and interleaves OBD
  requests with broadcast capture; long-drive validation is still pending.

**Role:** ground truth during RE, diagnostics, and supplemental standardized
telemetry until enough broadcast signals are confirmed. DTC reads (Mode 03/07),
VIN / calibration ID (Mode 09), readiness monitors (Mode 01 PID 0x01 STATUS),
and occasional ground-truth checks should use the Canable-native client where it
already supports the service.

The default `record.py --obd-pids auto` auto-discovers the ECU's
supported set, so this path *is* portable across vehicles without
per-vehicle hardcoding (commit `0a17...`). But that portability is in
service of RE / diagnostics, not the live data path.

#### How fast Mode 01 can actually go

| approach | channel-reads/s | speedup |
|---|---|---|
| single PID per request | 667 | — |
| **multi-PID, 6 per request** | **1387** | **2.1x** |
| (for reference) CANary today | ~8 | — |

Measured 2026-07-27 through `CanObdClient` itself (not a probe harness), KOEO,
daemon in passive mode, 180 channel-reads each way. Batched values matched
single-PID values exactly, 0/6 mismatches.

**Revised down from an earlier estimate of ~5x.** That figure came from a probe
loop that blocked ~10 ms in its own drain before every request, which throttled
both paths and flattered the batched one. The real difference is smaller
because a 6-PID response is 15 bytes — multi-frame ISO-TP, so it costs a
flow-control round trip and five frames, against two frames for a single-PID
read. **Batching trades frames for round trips, and the multi-frame response
eats over half the gain.**

The more important number is that *both* are two orders of magnitude above the
8 req/s the daemon actually uses. **The dominant constraint is the self-imposed
budget, not the protocol** — raising it is a bigger lever than batching, and
batching then multiplies whatever it is set to by ~2x. Note the 667 req/s
figure is back-to-back synchronous requests with no pacing on an otherwise
idle bus; sustained operation there is a different proposition, and the budget
exists partly to be a good citizen.

**Multi-PID Mode 01 is standard SAE J1979**: a request may carry up to six
PIDs and the response returns each as a `41 <pid> <data…>` triple. Verified
at 2, 4 and 6 PIDs:

```
01 0C 0D 04 11 0E 10  →  41 0C 0000 0D 00 04 00 11 13 0E FF 10 0076
```

**Gotcha that looks exactly like a dead ECU:** this ECM answers **only
DLC=8 frames**. A short frame gets no response at all — not a negative
response, silence. Requests must be padded to 8 bytes. `CanObdClient`
already pads; anything new must too. Two probe rounds were lost to this,
and the failure mode is indistinguishable from an unsupported service.

**What this ECM does *not* support** (all probed and refused, so don't
spend time on them):

| service | result |
|---|---|
| `2A` ReadDataByPeriodicIdentifier | `serviceNotSupported` — no CAN streaming |
| `2C` DynamicallyDefineDataIdentifier | `subFunctionNotSupported` |
| `22` with multiple DIDs | `subFunctionNotSupported` — Mode 22 stays one-at-a-time |

So the cost is the round trip rather than the byte count — but the binding
constraint today is the request budget, and batching is a ~2x multiplier on
top of whatever that is set to, not a substitute for raising it.
Implementation is tracked in
[`docs/roadmap/multi-pid-polling.md`](./roadmap/multi-pid-polling.md).

### 3. Mode 22 DIDs (Canable-native selected polling; broad sweep pending) — *manufacturer-extension diagnostic data*

UDS Mode 22 (ReadDataByIdentifier) is the standardized request format
for *manufacturer-defined* data identifiers. Where Mode 01 PIDs are a
shared 256-entry namespace across all vehicles, Mode 22 DIDs are
Nissan-specific (for us) — they expose things like injector pulse
width, fuel rail pressure, individual cylinder corrections — data
points Mode 01 doesn't standardize.

**Tools:** `scripts/record.py --obd-source canable --obd-dids ...` (live polling
of known DIDs alongside Mode 01) on the current Canable path. The legacy
`tools/scan_constant_dids.py --port ...` exploratory sweep still uses ELM327
transport and should be replaced with a Canable-native sweep CLI.

**Role:** Same as Mode 01 — RE-time cross-reference + occasional
diagnostic reads. The injector-PW DID, once identified (Task 4.E.4),
is the input the fuel-surface behavior model needs.

### 4. Nissan Consult-III via scan tool — *RE only, never production*

The operator has a Z1 Nissan scan tool; earlier captures used or referred to
the NT510 handheld. These manufacturer-level tools speak
the proprietary Consult-III protocol on top of UDS. We use it for:

* **Stimulating the bus** during RE — actuator tests (cooling fan,
  IACV, power balance) force a sensor or actuator to a known state,
  producing a clean signal in the Canable sniff that's easy to label.
* **Additional live-data parameters** Mode 01 doesn't expose, for
  cross-reference.

It's a *human-driven RE stimulus*, not a daemon-driven anything.
`scripts/guided_session.py` codifies the NT510 protocol for
hands-busy-in-vehicle tag generation.

**Role:** RE only. Never in production.

### 5. UpRev Osiris datalogger over K-line — *external tool; not reachable from CANary*

The UpRev tuning cable is the operator's tuning toolchain. Two of its
functions use **different transports**, and conflating them wastes time:

* **Reflashing runs over CAN.** Captured end-to-end on 2026-07-26
  (`ZS04C` → `ZS44A`, 410 547 frames / 239 s). Control chatter on
  `7E0`/`7E8` is session control plus KWP reads; the firmware payload
  streams as **raw 8-byte frames on Nissan-specific `0x743`** with no
  ISO-TP framing — 73 728 frames, 576 KiB, 31.8 s, ~59 % bus load.
  Notably **no standard UDS reprogramming services at all**: no
  `0x27` SecurityAccess, no `0x34` RequestDownload, no `0x36`
  TransferData. See `scripts/decode_flash.py`.
* **Datalogging does not touch CAN.** Proven the same evening: with the
  daemon in passive mode (bus verified silent — zero tester frames in a
  4 s pre-check), a 76 s `candump` recorded 124 086 frames while UpRev
  logged 518 rows at 16.2 Hz. **Zero frames on `7E0`/`7DF`/`0x743`, and
  not one arbitration ID that wasn't already present as normal vehicle
  broadcast.** The logger is on the K-line.

**Consequence:** CANary cannot observe or sniff UpRev's datalogging over
CAN — there is no protocol on our wire to copy.

It *can*, however, match and exceed the rate. An earlier revision of this
section claimed otherwise; that was wrong, and the correction matters
because it changes whether K-line hardware is worth building. Measured on
the ECM 2026-07-26, engine running, dedicated bus: **85 single-PID
requests/sec**, and **67.7 multi-PID requests/sec each returning 6 PIDs —
33.9 Hz per channel over a 12-channel set, zero failures**. Both figures
are floors (the probe spent ~10 ms of each ~12 ms cycle in its own drain).

The bottleneck was never the bus or the ECU. It is the default request
budget: `BUS_BUDGET_MULTIPLIER` (8.0) x `poll_rate_hz` (1) = 8 req/s
spread across ~42 requests. That is a conservative heuristic, not a
measured limit.

**What K-line would actually buy** — less than it first appears:

| | |
|---|---|
| ~~Rate~~ | **Not a K-line advantage.** CAN reaches 33.9 Hz/channel batched — see the correction above |
| MAF **sensor voltage** (`MAS A/F SE-B1`) | no known DID; distinct from the A/F sensor voltage at `0x1225` |
| Throttle X/Y **map traces** | the ETC map's live axes (throttle % × RPM) |
| `A/F ADJ` (long-term trim) | see below |

**Knock is not streamed by this ECM — settled 2026-08-01 from the FSM, not
inferred from failed searches.** `xterra_fsm/ec.pdf` EC-69's CONSULT-III
application table marks the knock sensor under *self-diagnostic results* only,
**not DATA MONITOR**, while streamable sensors (MAF, ECT, A/F sensor) are marked
in both; `data/fsm/signals.json` agrees, with no knock item among 333 extracted
DATA MONITOR parameters. The ECM exposes knock through exactly one interface, a
**DTC** (P0327/P0328 bank 1, P0332/P0333 bank 2). There is no knock channel to
discover, so **do not plan DID sweeps, broadcast RE, or scan-tool captures
around finding one.**

Knock *protection* is nonetheless working. EC-34: the retard system "is designed
only for emergencies… does not operate under normal driving conditions," with
base timing programmed inside the anti-knock zone on recommended (87 octane)
fuel. UpRev's constant knock-labelled fields (`HI DET` = 1, `KNOCK STRENGTH` =
`0x500`, `KNOCK EVENT` = 0, `KNOCK THRESHOLD %` = 1.95) therefore cannot be
reading a diagnostic channel — they are RAM addresses from UpRev's ROM
definitions, and a mismatch against this `ZS44A` calibration is far likelier
than dead hardware. Sensors verified electrically intact on the truck
2026-08-01 (disconnect set the code, reconnect cleared it; DTC logic is a
voltage-range check against a 532–588 kΩ spec). Because "the MIL will not light
up for these diagnoses" (EC-211), a knock-sensor failure is silent — **periodic
DTC polling is the knock-health monitor.**

**Role:** external operator tool. CANary's job is to cover what CAN can
reach (see the gap analysis in `docs/roadmap/engine-tuning-support.md`)
and to treat UpRev logs as an importable reference, not a live source.
`tools/import_uprev_log.py` does the importing: normalizes an exported CSV
into the same long-format telemetry schema every other source uses (unit
normalization included — UpRev's temperature columns carry no unit suffix
and are Fahrenheit, the exact bug a hand-written CSV parser is one keystroke
from reproducing) and lands it under `<storage_path>/_uprev/<session>/`,
quarantined from training discovery like `_archive` but reachable via
`query_telemetry(session=..., ...)` — see "DID sweep + diff" and "Raw CAN
activity capture" in `docs/headless-api.md` for the sibling ad-hoc tools this
session's tooling pass added. Unlike those, the UpRev importer is a plain
CLI step, not MCP-exposed: UpRev's logger runs on the operator's own
laptop (K-line, not CAN — see above), so the CSV and the importer are
naturally on the same machine already. It's mostly a local-analysis tool
against a local `canary.yaml`/storage path, not something that needs to
run on the Pi.

## The mapping

| Need | Primary path | Fallback / supplement |
|---|---|---|
| Live engine telemetry (RPM, coolant, MAF, …) for anomaly detection | **Broadcast CAN via Canable** → `bindings.yaml` decode | Canable-native Mode 01 streaming |
| Live engine state for the dashboard | Same — broadcast CAN | Canable-native Mode 01 — same shape via `obd:*` columns |
| DTC reads (active fault codes) | **Canable-native Mode 03** | Legacy ELM327 Mode 03 / 07 fallback |
| Vehicle identity (VIN, cal ID) | **Canable-native Mode 09**, one-shot | Legacy ELM327 Mode 09 |
| Readiness monitors (MIL state, drive-cycle progress) | **Canable-native Mode 01 PID 0x01 (STATUS)** | Legacy ELM327 Mode 01 |
| Reverse-engineering a new broadcast byte | Canable sniff + Mode 01 ground-truth cross-reference | NT510 stimulus to get a clean signal |
| Manufacturer-specific live data (injector PW, etc.) | **Canable-native Mode 22 selected DID polling** | Broad sweep CLI pending |
| RE stimulus (force the engine into a known state) | **NT510 actuator tests** | Drive the vehicle yourself |
| High-rate tuning parameters (MAF sensor V, throttle map traces, A/F ADJ) | **UpRev logger over K-line** — external tool, import its CSV | No CAN path exists; optional future K-line hardware |
| Knock / detonation | **Not streamed by design** (FSM EC-69: self-diagnostic results only, not DATA MONITOR). Health is observed via **DTC polling** — P0327/P0328, P0332/P0333, which set no MIL | None needed; do not plan discovery captures. Retard is emergency-only (EC-34) and the engine is not knock-limited as calibrated |
| Watching another tool own a diagnostic session | **Passive mode** (`tx_gate`) + `candump`, decoded by `scripts/decode_flash.py` | — |

## Signal interpretation gotchas

Facts about what a channel *means* that are not visible in its decode, and
that have each already produced a wrong conclusion.

- **`INTAKE_TEMP` is stagnation-dominated, not a thermal state.** It tracks
  whether air is *moving*, not how hot the engine bay has become:
  `corr(IAT, RPM) = −0.797`, `corr(IAT, MAF) = −0.542`. Above ~8 g/s of
  airflow it pins to ambient (34–36 °C measured); the 50 °C readings occur
  only at idle. Do not read a rising IAT trend as heat soak without first
  checking idle fraction, and do not expect high IAT to coincide with high
  load — it essentially never does. See `roadmap/engine-tuning-support.md`
  G.3.
- **`ENGINE_LOAD` does not separate idle from light cruise** — calculated load
  reads ~20 % in both. It is not a valid control variable for anything
  airflow-related; use MAF and rpm.
- **`LONG_FUEL_TRIM_*` reads a true 0.0 on this ECU** — it does not learn, and
  this survived a full reflash. A flat zero here is the ECU's behaviour, not a
  decode bug or a pending relearn. Short-term trim carries the whole
  correction permanently.
- **OBD `THROTTLE_POS` tops out near 89 %** and understates true WOT. For
  wide-open-throttle detection use `FUEL_STATUS == 4` (open loop due to load),
  paired with throttle to exclude decel fuel cut, which is also status 4.
- **`AMBIANT_AIR_TEMP` (PID 0x46) is unsupported** by this ECU, so ambient
  cannot be separated from underhood temperature from OBD alone.

## The production daemon's actual dependencies

```
                  ┌─────────────────┐
                  │ Canable on the  │  ← cheap, always-on, no contention
                  │ vehicle's OBD-II│
                  │ port            │
                  └────────┬────────┘
                           │ slcan / SocketCAN
                           ▼
                  ┌─────────────────┐
                  │ src.collection. │
                  │ can_collector   │
                  └────────┬────────┘
                           │ TelemetryRecord (source="can")
                           ▼
                  ┌─────────────────┐
                  │ src.preprocess. │
                  │ live (decode    │
                  │ via bindings.   │
                  │ yaml)           │
                  └────────┬────────┘
                           │ decoded:* feature rows
                           ▼
                  IF / LSTM inference → AnomalyEventEmitter → dashboard
```

**No ELM327 in the steady-state path.** The dashboard, the alerts log,
the live inference — all run from the Canable + `bindings.yaml`. The same
Canable can also handle standardized OBD probes; streaming OBD collection is the
next integration task.

## The RE-time setup

```
       ┌────────┐         ┌────────┐         ┌────────┐
       │Canable │ sniff   │ELM327  │ legacy  │ NT510  │ stimulate
       │        │ + OBD   │fallback│         │        │
       └────┬───┘         └────┬───┘         └────┬───┘
            │                   │                   │
            ▼                   ▼                   ▼
        frames.log          pids.jsonl         (operator tags)
            │                   │                   │
            └───────────────────┴───────────────────┘
                                │
                                ▼
                  tools/broadcast_signals.py
                  tools/cross_reference_broadcast.py
                  tools/scan_constant_dids.py
                                │
                                ▼
                  data/bindings.yaml (hand-edited)
```

The Canable is both the sniffer and the standardized Mode 01/03/09/22 OBD
requester for current captures. The ELM327 remains only as legacy emulator /
fallback coverage. Analysis tools correlate across the logs. Pinning a binding
usually takes one good
in-vehicle session per signal once the setup is rehearsed
(`docs/can-re-workflow.md` is the worked-example doc; `ECM_RPM` went
from candidate to confirmed in one rev-protocol session).

## Where the code lives

| Layer | Modules |
|---|---|
| Canable sniff | `src/collection/can_collector.py`, `scripts/record.py` |
| Broadcast decode (the production primary) | `src/decode/bindings.py`, `src/preprocessing/decode.py`, `src/preprocessing/live.py` |
| ELM327 Mode 01 polling | `src/collection/obd_collector.py`, `tools/obd_coverage.py` |
| Canable-native OBD Mode 01/03/09/22 | `src/collection/can_obd_client.py`, `src/collection/can_obd_collector.py`, `tools/can_obd_probe.py`, `scripts/record.py --obd-source canable` |
| Mode 22 DID sweep | `src/analysis/did_scan.py`, `tools/scan_constant_dids.py` (legacy ELM transport; Canable-native replacement pending) |
| NT510 RE protocol | `scripts/guided_session.py` (operator side; the NT510 itself is the device) |
| RE analysis pipeline | `tools/inventory_broadcast.py`, `tools/broadcast_signals.py`, `tools/cross_reference_broadcast.py`, `tools/corpus_report.py` |
| Production daemon | `src/main.py`, `src/daemon.py`, `src/preprocessing/live.py`, `src/models/inference.py`, `src/alerts/emitter.py`, `src/dashboard/api.py` |

## What this implies for the road ahead

1. **Validate Canable-native streaming OBD on a real drive.**
   `CanObdClient`, `can_obd_collector.py`, daemon wiring, and
   `record.py --obd-source canable` are in place for Mode 01 and selected
   Mode 22 DID polling. The next capture should prove that one Canable can
   sustain broadcast capture plus OBD polling over vibration and load.

2. **More confirmed broadcast bindings remains high leverage.**
   The current `bindings.yaml` has 2 confirmed signals (`ECM_RPM`,
   `ECM_COOLANT_TEMP`) plus several high-confidence candidates. Every
   additional confirmed signal directly adds a feature column to the ML pipeline
   and a useful dashboard card, with no further code changes. The corpus report's
   "binding candidates" + "16-bit signal candidates" sections are the
   hot list.

3. **The CAN-native OBD-II client now exists** —
   `src/collection/can_obd_client.py`. ISO 15765-4 single-frame +
   multi-frame ISO-TP reassembly (first-frame / consecutive-frame /
   flow-control). Decodes ~37 standard Mode-01 PIDs (powertrain, fuel
   control, narrowband O2, environment, EGR, EVAP, catalyst temps, …)
   with SAE J1979 scaling; Mode 03 DTC list parsing including system
   letter (P/C/B/U); Mode 09 InfoType multi-frame (VIN, calibration ID);
   `query_supported_mode_01_pids` walks the PIDS_A/B/C/D bitmasks.
   `tools/can_obd_probe.py` is the one-shot CLI — same information as
   `tools/obd_coverage.py` but over the Canable, no ELM327 USB device
   needed. The request builder takes any service byte, Mode 04 / 0x2E /
   0x2F included. 28 tests including a multi-frame VIN round-trip with
   flow-control.

   The streaming path is now the default when `collection.obd_source: canable`.
   The legacy `obd_collector.py` remains available only when
   `collection.obd_source: elm327`.

4. **The NT510 stays an RE-only tool.** It doesn't expose an automatable
   interface, and even if it did, every parameter it surfaces is
   either standardized (already in Mode 01) or proprietary
   (better captured via the broadcast bus once bound).
