# Capture rounds and acquisition clocks

Roadmap task C02 (`docs/roadmap/tasks/archive/02-explicit-capture-rounds.md`).
This is the contract for *when* a polled OBD/DID reading was taken, how the
scheduler groups readings into rounds, and what a reader may and may not
infer from the persisted fields. Pairing rules for analysis live in
`docs/analysis-pairing.md`; this document is what feeds them.

## The defect this replaces

`run_can_obd_loop()` used to take one wall-clock read when the first due
request of a burst was selected and reuse it for every reading until the loop
ran out of due work. Under sustained demand that never happens. Reproduced
deterministically in `tests/test_capture_rounds.py` (fake clock, scripted
ECU): two channels at 10 Hz on the default 8 request/s budget, batching off,
no priority channels — **24 readings on one stamp spanning 2.875 s** over a
3 s window; with batching enabled but refused by the ECU (NRC, single-PID
fallback) **15 readings on one stamp, same 2.875 s span**. The roadmap's
"eight readings across 875 ms" is the same defect over a shorter window.

After the fix the same runs produce 24 / 15 distinct stamps, none covering
more than one 100 ms round, and the budget shortfall is reported as
`rounds_incomplete` rather than hidden inside a shared stamp.

Separately, `acquired_at` was a `time.time()` read *after* response handling,
set only when a profile round-stamped, with no statement of which clock it
came from. All three are now explicit.

## Rounds

A **round** is one scheduled sampling window (`RoundState` in
`src/collection/obd_scheduler.py`, driven by `RoundScheduler`):

| field | meaning |
|---|---|
| `generation` | capture generation (below) |
| `round_id` | monotonic within the generation, from 0 |
| `scheduled_start`, `deadline` | monotonic; `deadline = scheduled_start + period_s` |
| `period_s` | the fastest configured cadence among pollable requests at open time |
| `wall_start` | wall clock at `scheduled_start`; the presentation stamp when the profile asks for `round_timestamps` |
| `members` | request indices due before `deadline` when the round opened |
| `attempted`, `observations` | per-member accounting; `succeeded` = members with ≥ 1 reading |
| `first_acquired_at`, `last_acquired_at` | measured acquisition spread → `skew_s` |

Rules, each pinned by a test:

- A round **ends on membership completion or at its deadline**, whichever
  comes first — never "when the loop runs out of work". The deadline check
  runs before any selection, so no reading is ever stamped with an expired
  round.
- Rounds are scheduled at `anchor + k · period`. If the loop falls behind
  (a long timeout, a session-control exchange) the periods it missed are
  counted as `rounds_skipped` and the next round opens at the next *future*
  boundary. No catch-up burst is issued.
- When nothing is pollable (passive mode, a Mode-22 lapse, everything backed
  off) the schedule is re-anchored on the next open; idle time is not
  reported as skipped rounds.
- **Mixed rates:** the period is the fastest cadence; a slower request is a
  member only of rounds whose window contains its due time (a 1 Hz context
  channel joins every fourth 250 ms round). A member is attempted **once**
  per round; a miss waits for the next round it is due in (the miss backoff
  makes that explicit).
- A member is due inside the window by construction, so it may be issued as
  soon as the round is open rather than at its own due instant. This is a
  one-time phase advance of under one period, after which the round's
  members share one cadence (and one batch); the configured rate is
  unchanged because the next due time is still `issued + interval`.
- **Budget stays separate from rate.** `next_budget_at` paces requests as
  before. When the budget cannot serve every member inside the window the
  round closes **incomplete** and the counter says so.
- **Batching inside a round:** `select_batch_companions(members=…)` takes
  companions from the round's unattempted members only. The profile's
  `required_tuple` (`tuning_profile.required_tuple()`) fills the batch first
  and ranks just below the priority lane in leader selection, so the tuple
  completes before the budget runs out without making any request due
  earlier than its rate.

`CanObdLoopStats` carries `rounds_started`, `rounds_complete`,
`rounds_incomplete`, `rounds_skipped` and `round_skew_max_s`;
`ObdSchedulerMetrics.rounds` (and therefore `/status.obd_scheduler.rounds`)
exposes them with the generation, period, last-round skew and the open
round's progress.

## Generations

`src/collection/capture_generation.py` is a process-wide counter. It is
bumped by:

- every `run_can_obd_loop` start — including each reconnect attempt;
- `tuning_profile.activate` (any profile change), `set_paused` on change;
- `tx_gate.set_passive` on change;
- `ReplayController.start` / replay finish.

The loop reads it once per tick; on change it closes the open round and
restarts numbering. A request issued under generation *g* is tagged *g* even
if its response lands after the bump: the raw evidence is emitted, but it
cannot complete a tuple in the capture that started afterwards. Round
identity is scoped to `(session, generation, round_id)` — the counter
restarts with the process, and the session boundary separates two processes'
generation 1.

## Per-reading timing fields (`ts_schema` 2)

`ObdReading` carries, separately: `timestamp` (presentation), `acquired_at`,
`acquired_clock`, `response_interval_s`, `generation`, `round_id`.
`reading_to_record` persists them in `metadata_json`:

```json
{"ts_schema": 2, "acquired_at": 1757203200.0051, "acquired_clock": "kernel",
 "generation": 3, "round_id": 41, "response_interval_s": 0.002}
```

- `acquired_at` is **always** set by the Canable collector now, round-stamped
  or not. When the active profile does not ask for `round_timestamps`, the
  presentation `timestamp` *is* `acquired_at`. When it does, `timestamp` is
  the round's `wall_start`.
- `acquired_clock` is `"kernel"` when python-can's `Message.timestamp` (the
  SocketCAN `SO_TIMESTAMP`, or the driver's receive-time read on slcan) was
  within `KERNEL_STAMP_MAX_SKEW_SECONDS` (5 s) of `time.time()`, else
  `"userspace"` — a `time.time()` read after receipt, with the row saying so.
  Adapters that report `0.0` land in the fallback.
- **Multi-frame ISO-TP policy:** `acquired_at` is the **first frame's**
  arrival. `response_interval_s` is completion minus first frame, recorded
  when > 0 and omitted for single-frame responses.
- **Batched Mode 01:** every PID in one batched response shares that
  response's `acquired_at`. That is the receipt time of one payload. It is
  **not proof that the ECU sampled those PIDs atomically**, and no reader may
  treat it as such (the sentence also lives in `_response_to_reading`'s
  docstring).
- `response_interval_s` and the round fields are omitted when absent rather
  than written as null.

`replay.py` carries `metadata_json` through; `acquired_at` is shifted by the
same offset as the row's rebased timestamp so their separation survives,
while `generation` / `round_id` are carried as recorded (they name the
*source* capture's rounds).

## Legacy readers and versions

| persisted shape | how `timestamp_basis` reads it |
|---|---|
| no `acquired_at` (pre-C01 rows, the ELM327 path) | `stored`, or `round` when the session manifest says `round_timestamps` — unchanged from C01 |
| `acquired_at` without `ts_schema` (C01, v1) | `acquired`; the field was written only when round-stamped |
| `round_id` present, no `acquired_at` | `round` — the row itself says the stamp is a round stamp, no manifest needed |
| `ts_schema: 2` | `acquired`, always; round fields available as columns |

Nothing reconstructs missing acquisition evidence: a legacy `round` row's
true arrival is unknown to within a round period and `check_clock_support`
keeps refusing sub-period tolerances for it. Historic sessions are neither
rewritten nor migrated.

`src/analysis/channels.py`:

- `read_channels(…, acquired_time=True)` — unchanged contract; rows with
  `acquired_at` are placed on it.
- `read_acquisition_frame()` now also yields `acquired_at`, `generation`,
  `round_id` columns (nullable).
- `round_skew(frame)` — one row per `(generation, round_id)` with `n`,
  `first_acquired_at`, `skew_s`. **This is the maximum-skew evidence C03
  should consume** for tuple validation: sharing a `round_id` says the
  scheduler asked for the members together; `skew_s` says how far apart they
  actually arrived. An empty result means no round evidence was persisted,
  not that skew was zero.

## Consumers swept (C02.9)

| consumer | reads | effect |
|---|---|---|
| `LiveFeatureSource` / `/status` freshness (`src/preprocessing/live.py`, `src/dashboard/status.py` — C08-owned, not edited) | `TelemetryRecord.timestamp` | presentation stamp; during a round-stamped capture freshness lags acquisition by at most one round period (≤ the fastest configured interval), previously unbounded. No change needed. |
| storage rotation (`parquet_writer`) | `timestamp` gaps, `RUN_TIME` value | unaffected: rotation thresholds are seconds to minutes, round quantisation is ≤ 1 period |
| preprocessing pivot / resampler / gap flags | `timestamp` | identical output for round-stamped and plain rows with the same stamps — `test_resampler_and_gap_flags_see_the_presentation_stamp_unchanged` |
| derived rows (`preprocessing.decode`) | broadcast `can:` rows | untouched; their `metadata_json` never carried acquisition fields |
| rate summaries (`achieved_hz`, `capture_rates`) | monotonic read times | unchanged |
| tuning reports (`tuning_frame`, `tuning_digest`, `compare_channels`) | `acquired_time=True` | already on the acquisition clock; now also for non-round captures |
| replay | `metadata_json` | carried through, `acquired_at` rebased with the row |
| dashboard `ObdSchedulerStatus` (`types/control.ts`) | `/status.obd_scheduler` | the new `rounds` key is typed as `CaptureRoundStatus` (with `CaptureRoundOpenStatus` for the open round), matching `RoundScheduler.snapshot()` key for key; `pnpm typecheck` and `pnpm test` pass. The panel does not render it yet. |

## What was measured (fake clock, `tests/test_capture_rounds.py`)

- Counterexample before/after: 24 readings / 1 stamp / 2.875 s → 24 stamps,
  max 1 reading per stamp; refused-batch variant 15 / 1 / 2.875 s → 15
  stamps.
- Six-PID batch plus two more members: both requests share one round id and
  one presentation stamp; two distinct `acquired_at` values are retained;
  `round_skew_max_s` equals the measured spread.
- 4 Hz fast / 1 Hz context: period 250 ms; context member every fourth
  round; no incomplete rounds; no channel attempted twice in a round.
- 15 Hz continuously due DID work on a 32 req/s budget: both DIDs in every
  settled round, zero incomplete, zero skipped, stamps advance by exactly
  1/15 s.
- Silent + refused channels: every round terminates
  (`started == complete + incomplete`), one attempt per member per round.
- Multi-frame batch response: `acquired_at` = first frame, interval = the
  consecutive-frame gaps.
- Kernel stamp preferred; `0.0` stamp → userspace fallback recorded.

Not measured: anything on the truck. Real SocketCAN stamp skew, slcan stamp
behaviour and the Pi's achievable round completion rate are C10's.
