# Data architecture — where each signal comes from, and which path is canonical

What's flowing on the wire, what each interface can see, and which interfaces
the daemon depends on for **production** (live anomaly detection on a
running vehicle) vs **RE** (reverse-engineering the proprietary signals
so production has something to decode against).

Worth pinning explicitly because the four interfaces — Canable, ELM327,
NT510, Mode 22 DIDs — are *not* interchangeable, and over-investing in
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

### 2. OBD-II Mode 01 PIDs (ELM327, request-response) — *RE cross-reference, not production*

OBD-II is standardized: every compliant ECU responds to a Mode 01
request for PID 0x0C with the same RPM-in-known-units format. That's
extremely useful for **ground truth** during RE — "I see byte 2 of
0x1F9 rising; the ELM327 says PID 0x0C is 700 rpm; regression gives
r²=1.0; that byte *is* RPM." It's what pinned `ECM_RPM` to
`confirmed`.

But it's **request-response**, not broadcast. The ECU doesn't emit Mode
01 responses spontaneously; some tool has to ask. The ELM327 is that
tool today.

**Where it lives:** `src/collection/obd_collector.py`,
`scripts/record.py --obd-port`.

**Strengths:** standardized scaling and units; portable across vehicles
without an RE pass; the ECU's *intended* diagnostic interface.

**Weaknesses for production:**
* Polling round-robins through every watched PID on a single ELM327, so
  per-PID refresh rate is roughly `1 / N_pids` Hz. At 35 PIDs that's
  ~3 s per PID — much slower than broadcast.
* Requires an extra adapter plugged into the OBD-II port.
* CH340-based clones over WSL-USB are flaky (the supervised reconnect
  loop in 1.E.1 exists to deal with this).

**Role:** RE-time tool. After enough broadcast signals are bound, the
ELM327 is optional for steady-state telemetry — needed only for
DTC reads (Mode 03/07), VIN / calibration ID (Mode 09), readiness
monitors (Mode 01 PID 0x01 STATUS), and the occasional ground-truth
check.

The default `record.py --obd-pids auto` auto-discovers the ECU's
supported set, so this path *is* portable across vehicles without
per-vehicle hardcoding (commit `0a17...`). But that portability is in
service of RE / diagnostics, not the live data path.

### 3. Mode 22 DIDs (ELM327 or Canable + ISO-TP client) — *manufacturer-extension diagnostic data*

UDS Mode 22 (ReadDataByIdentifier) is the standardized request format
for *manufacturer-defined* data identifiers. Where Mode 01 PIDs are a
shared 256-entry namespace across all vehicles, Mode 22 DIDs are
Nissan-specific (for us) — they expose things like injector pulse
width, fuel rail pressure, individual cylinder corrections — data
points Mode 01 doesn't standardize.

**Tools:** `tools/scan_constant_dids.py` (exploratory sweep),
`scripts/record.py --obd-dids` (live polling of known DIDs alongside
Mode 01).

**Role:** Same as Mode 01 — RE-time cross-reference + occasional
diagnostic reads. The injector-PW DID, once identified (Task 4.E.4),
is the input the fuel-surface behavior model needs.

### 4. Nissan Consult-III via NT510 — *RE only, never production*

The NT510 handheld is Nissan's manufacturer-level scan tool. It speaks
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

## The mapping

| Need | Primary path | Fallback / supplement |
|---|---|---|
| Live engine telemetry (RPM, coolant, MAF, …) for anomaly detection | **Broadcast CAN via Canable** → `bindings.yaml` decode | ELM327 Mode 01 (slower, optional) |
| Live engine state for the dashboard | Same — broadcast CAN | ELM327 Mode 01 — same shape via `obd:*` columns |
| DTC reads (active fault codes) | **ELM327 Mode 03 / 07** | None — broadcast doesn't carry these |
| Vehicle identity (VIN, cal ID) | **ELM327 Mode 09**, one-shot | None |
| Readiness monitors (MIL state, drive-cycle progress) | **ELM327 Mode 01 PID 0x01 (STATUS)** | None |
| Reverse-engineering a new broadcast byte | Canable sniff + ELM327 ground-truth cross-reference | NT510 stimulus to get a clean signal |
| Manufacturer-specific live data (injector PW, etc.) | **Mode 22 DIDs via ELM327** | None until bound |
| RE stimulus (force the engine into a known state) | **NT510 actuator tests** | Drive the vehicle yourself |

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
the live inference — all run from the Canable + `bindings.yaml`. Plug
the ELM327 in for periodic DTC checks, but otherwise it's optional.

## The RE-time setup

```
       ┌────────┐         ┌────────┐         ┌────────┐
       │Canable │ sniff   │ELM327  │ poll    │ NT510  │ stimulate
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

All three devices in parallel during a capture session; analysis tools
correlate across them. Pinning a binding usually takes one good
in-vehicle session per signal once the setup is rehearsed
(`docs/can-re-workflow.md` is the worked-example doc; `ECM_RPM` went
from candidate to confirmed in one rev-protocol session).

## Where the code lives

| Layer | Modules |
|---|---|
| Canable sniff | `src/collection/can_collector.py`, `scripts/record.py` |
| Broadcast decode (the production primary) | `src/decode/bindings.py`, `src/preprocessing/decode.py`, `src/preprocessing/live.py` |
| ELM327 Mode 01 polling | `src/collection/obd_collector.py`, `tools/obd_coverage.py` |
| Mode 22 DID polling + sweep | `src/analysis/did_scan.py`, `tools/scan_constant_dids.py`, `scripts/record.py --obd-dids` |
| NT510 RE protocol | `scripts/guided_session.py` (operator side; the NT510 itself is the device) |
| RE analysis pipeline | `tools/inventory_broadcast.py`, `tools/broadcast_signals.py`, `tools/cross_reference_broadcast.py`, `tools/corpus_report.py` |
| Production daemon | `src/main.py`, `src/daemon.py`, `src/preprocessing/live.py`, `src/models/inference.py`, `src/alerts/emitter.py`, `src/dashboard/api.py` |

## What this implies for the road ahead

1. **More confirmed broadcast bindings is the highest-leverage work.**
   The current `bindings.yaml` has 2 confirmed signals (`ECM_RPM`,
   `ECM_COOLANT_TEMP`). Every additional confirmed signal directly
   adds a feature column to the ML pipeline and a useful card on the
   dashboard, with no further code changes. The corpus report's
   "binding candidates" + "16-bit signal candidates" sections are the
   hot list.

2. **The CAN-native OBD-II client now exists** —
   `src/collection/can_obd_client.py`. ISO 15765-4 single-frame +
   multi-frame ISO-TP reassembly (first-frame / consecutive-frame /
   flow-control). Decodes ~37 standard Mode-01 PIDs (powertrain, fuel
   control, narrowband O2, environment, EGR, EVAP, catalyst temps, …)
   with SAE J1979 scaling; Mode 03 DTC list parsing including system
   letter (P/C/B/U); Mode 09 InfoType multi-frame (VIN, calibration ID);
   `query_supported_mode_01_pids` walks the PIDS_A/B/C/D bitmasks.
   `tools/can_obd_probe.py` is the one-shot CLI — same information as
   `tools/obd_coverage.py` but over the Canable, no ELM327 USB device
   needed. Read-only by construction (the request builder has no path
   for Mode 04 / 0x2E / 0x2F). 28 tests including a multi-frame VIN
   round-trip with flow-control.

   The path is wired but not yet swapped into the production daemon —
   the existing `obd_collector.py` (ELM327) still runs the watch loop.
   Switching to the Canable-native path is a separate piece: one
   `can_obd_collector.py` that wraps `CanObdClient` in the same
   supervisor pattern `supervise_obd_capture` uses, plus a config flag
   to choose between the two paths.

3. **The NT510 stays an RE-only tool.** It doesn't expose an automatable
   interface, and even if it did, every parameter it surfaces is
   either standardized (already in Mode 01) or proprietary
   (better captured via the broadcast bus once bound).
