# Roadmap — multi-PID Mode 01 polling

Status: **Phases 1 and 2 landed** (commit `78abf88`, 2026-07-26). Phase 3
partly re-scoped after an audit — see 3.1 and 3.5. Phase 4 needs a drive.
Straight to `main`, no feature branches.

Phases 1–2 are pure software and overnight-eligible. Phase 3 changes what
the daemon puts on the bus and wants the truck. Phase 4 is validation and
needs a real drive.

## Why this exists

CANary polls one PID per request-response round trip. With ~42 requests
sharing the default 8 req/s budget, each channel refreshes every 2–15
seconds. That is adequate for anomaly detection across a whole drive and
useless for anything that needs samples inside a single operating point —
the 2026-07-26 post-flash drive produced 196 `TIMING_ADVANCE` samples in 57
minutes, roughly 10 per rpm × load cell.

The cost is the **round trip, not the bytes**. Measured on the Xterra ECM
with the engine running and the bus dedicated:

| approach | requests/s | per-channel (12 channels) |
|---|---|---|
| single PID per request | 85.1 | 7.09 Hz |
| **multi-PID, 6 per request** | **67.7** | **33.85 Hz** |
| CANary today | 8 | 0.06–0.5 Hz |

80 batched requests, zero failures, 1.18 s. Both figures are **floors** —
the probe blocked ~10 ms per cycle in its own drain.

Two independent levers, and they multiply:

1. **Batching.** Multi-PID Mode 01 is standard SAE J1979 — up to six PIDs
   per request, each returned as a `41 <pid> <data…>` triple. Confirmed at
   2, 4 and 6 PIDs against live values.
2. **The budget.** `BUS_BUDGET_MULTIPLIER` (8.0) × `poll_rate_hz` (1) =
   8 req/s is a heuristic with no measured basis. The ECU answered 85/s.

Full evidence: [`docs/data-architecture.md`](../data-architecture.md) §2,
"How fast Mode 01 can actually go".

## Guiding constraints

- **Pad to DLC=8.** This ECM ignores short frames entirely — no response,
  not a negative response. Indistinguishable from an unsupported service,
  and it cost two probe rounds to spot.
- **Parse defensively.** An ECU may omit an unsupported PID from a batch
  rather than rejecting the whole request. Credit only the channels
  actually returned, or a missing PID silently poisons the backoff
  accounting of everything batched alongside it.
- **Don't batch across poll tiers.** A `slow`-tier PID in a `fast` batch
  either drags the fast one down or drags the slow one up; both waste
  budget. Group within tier.
- **Vehicle-general, not Xterra-tuned.** Batch size 6 is the J1979 ceiling,
  but not every ECU honours it. Probe the achievable size and fall back.
- **Do not raise the global budget by default.** Sustained 68 req/s
  alongside 84 Hz broadcast capture pushes the MCP2515 harder than
  anything yet run; the HAT already logged `rx_over_errors` during the
  reflash. Raise it for bounded captures first, globally only with
  evidence.

## Not available on this ECM — do not pursue

All probed 2026-07-26 and refused:

| service | result |
|---|---|
| `2A` ReadDataByPeriodicIdentifier | `serviceNotSupported` — no CAN streaming |
| `2C` DynamicallyDefineDataIdentifier | `subFunctionNotSupported` |
| `22` with multiple DIDs | `subFunctionNotSupported` |

Mode 22 DIDs therefore stay one-at-a-time regardless of what happens here.

## Phase 1 — Batching in the request/response core

- [x] **1.1 — Build a batched request.** Extend `build_request_payload`
  (or add a sibling) to emit `01 <pid> <pid> …` for up to N PIDs, padded
  to DLC=8. A 6-PID request is exactly 8 bytes including the PCI, so it
  still fits a single frame — no ISO-TP on the request side.
- [x] **1.2 — Parse a batched response.** Walk `41 <pid> <data…>` triples,
  splitting on each PID's declared byte length from
  `obd_registry`/`PID_DECODERS`. A 6-PID response is 15 bytes, so this is
  multi-frame ISO-TP inbound and needs the existing flow-control path.
  Return one reading per PID actually present.
- [x] **1.3 — Partial-response handling.** A batch that comes back missing
  a PID must credit the ones returned and mark only the absent one as
  unanswered. Test with a synthetic response that omits a middle PID —
  the offset walk must not desynchronise.
- [x] **1.4 — Negative-response handling.** A `7F 01 <nrc>` to a batch
  applies to the whole request. **Settled: fall back, and credit nobody.**
  A whole-batch failure is evidence about the *batch*, not about any member,
  so applying six misses would wrongly back off six healthy channels on an
  ECU that merely dislikes batching.
- [x] **1.5 — Client method.** `CanObdClient.query_mode_01_batch(pids)`
  alongside the existing single-PID `query_mode_01`, so ad-hoc callers and
  control routes get the same benefit.

## Phase 2 — Wire batching into the scheduler

- [x] **2.1 — Group requests into batches.** In `build_poll_requests`,
  group `kind="pid"`, `service=0x01` requests by `poll_tier` into batches
  of ≤ `max_batch_size`. Batch membership is a scheduling detail — the
  emitted `ObdReading`s must stay per-channel so nothing downstream
  changes.
- [x] **2.2 — Per-batch scheduling state.** `_PollRequestState` currently
  tracks due-time and backoff per request. A batch needs one due-time but
  per-member response accounting, so a single dead PID backs off alone
  rather than the batch.
- [x] **2.3 — Scheduler metrics.** `_scheduler_metrics` should report
  batches issued, mean PIDs per batch, and achieved per-channel Hz. That
  last number is the one that tells an operator whether a tuning capture
  is worth analysing.
- [x] **2.4 — Probe the achievable batch size.** Implemented *adaptively*
  rather than as a startup probe: three consecutive whole-batch failures
  narrow the size 6 → 4 → 2 → 1, and reaching 1 disables batching for the
  session. Better than a startup probe because it also catches an ECU that
  degrades mid-drive, and it costs nothing when batching works. Surfaced as
  `batching.size_narrowings` in the scheduler metrics.

## Phase 3 — Apply it everywhere it pays

- [ ] **3.1 — Supported-PID discovery.** *Re-scoped after the 3.5 audit —
  worth less than first claimed, and harder.* The walk is **conditional**:
  it stops as soon as no responder sets the continuation bit, so a typical
  ECU costs ~4 requests, not 7. Batching all seven upfront would *add*
  requests for groups the ECU doesn't support. It also runs through
  `_request_many`, which merges bitmasks from *multiple* responders on the
  functional broadcast — a path the current batching code doesn't handle,
  since it assumes one responder. Net saving is a few requests, once, at
  startup. Do it only if the multi-responder merge is reworked anyway.
- [ ] **3.2 — Mode 02 freeze frame.** `FREEZE_FRAME_PIDS` is five PIDs
  read one-shot when a DTC appears. J1979 allows multi-PID for Mode 02
  as well (each PID paired with its frame number). Low volume, but it is
  the same mechanism and the one-shot read gets closer to a coherent
  single instant — which is the entire point of a freeze frame.
- [ ] **3.3 — Tuning capture profile.** `tuning_profile` narrows the
  rotation to 15 Mode 01 channels; batched that is 3 requests instead of
  15. Combined with a raised budget for the bounded capture window, this
  is what makes ~30 Hz per channel real rather than theoretical.
- [ ] **3.4 — `tools/can_obd_probe.py`.** The probe CLI should be able to
  exercise batched reads, so the next ECU can be characterised without
  writing a throwaway script (as was done on 2026-07-26).
- [x] **3.5 — Audit for missed sites.** Done 2026-07-26. **The poll
  rotation was the only high-value site**, and it is now batched. The
  complete set of other Mode 01 callers:
  * `query_supported_mode_01_pids` — see 3.1, re-scoped.
  * `read_pid` (`can_obd_client`) — a single ad-hoc convenience read, not a
    loop. Nothing to batch.
  Confirmed non-beneficiaries: Mode 03/07/0A (no PIDs), Mode 06 (single
  sweep), Mode 22 (batching refused by this ECM), broadcast CAN (free).

## Phase 4 — Validation

- [ ] **4.1 — Sustained-rate test.** Everything measured so far was a
  ~1.2 s burst on a dedicated bus. Run a full drive at the raised rate and
  watch `can0` `rx_over_errors` / `rx_dropped` against the pre-run
  baseline. The MCP2515 has two RX buffers and an SPI drain; this is where
  it will complain if it is going to.
- [ ] **4.2 — Cross-check batched vs single.** Poll the same channels both
  ways in alternating windows and confirm the decoded values agree. Cheap
  insurance against an offset-walk bug that only shows on real data.
- [ ] **4.3 — Timing-parity check.** Batched readings share one timestamp
  where single-PID readings each had their own. Confirm the feature
  pipeline's gap handling and the `__roc` derivatives behave sensibly when
  six channels land simultaneously — this changes the arrival pattern the
  resampler sees.
- [ ] **4.4 — Re-measure and record.** Update the table in
  `data-architecture.md` §2 with achieved in-daemon rates, as distinct
  from the dedicated-bus probe figures.

## Cross-links

- [`docs/data-architecture.md`](../data-architecture.md) §2 — measured
  rates, the DLC=8 gotcha, unsupported services
- [`docs/roadmap/engine-tuning-support.md`](./engine-tuning-support.md)
  Phase 1.1 — the focused tuning profile this multiplies
- `src/collection/tuning_profile.py` — the runtime channel-set gate
- `src/collection/obd_scheduler.py` — `build_poll_requests`,
  `_bus_budget_requests_per_second`, `TIER_RATE_FACTORS`
- `src/collection/can_obd_client.py` — `query_mode_01`,
  `query_supported_mode_01_pids`, DLC padding
