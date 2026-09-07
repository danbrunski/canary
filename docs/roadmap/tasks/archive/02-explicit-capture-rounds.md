# C02 — Make capture rounds explicit and preserve acquisition clocks

Priority: high. Dependencies: none for the scheduler regression; coordinate
the persisted timing contract with C01 and tuple accounting with C03.

## Problem and code entry points

`run_can_obd_loop()` in
[can_obd_collector.py](../../../../src/collection/can_obd_collector.py) holds
`round_wall_start` until it runs out of due or eligible work. Under sustained
demand that may never happen. A synthetic two-channel capture asking for
10 Hz per channel with an 8 request/s budget, batching disabled and no priority
channels, produced eight readings spanning 875 ms with one logical timestamp.

`acquired_at` currently records a userspace clock read after response handling,
not necessarily the kernel frame-arrival time. Both its precision and its
fallback behavior need explicit semantics.

Other entry points: [obd_scheduler.py](../../../../src/collection/obd_scheduler.py),
[obd_collector.py](../../../../src/collection/obd_collector.py),
[telemetry.py](../../../../src/telemetry.py),
[parquet_writer.py](../../../../src/storage/parquet_writer.py), and
[channels.py](../../../../src/analysis/channels.py).

## Implementation checklist

- [x] C02.1 Reproduce the overloaded-round failure with a deterministic fake
  clock and bus. Include a batch failure/fallback variant so correctness does
  not depend on batching being available.
  _done 2026-09-07: `tests/capture_round_fakes.py` (FakeClock + ScriptedEcu) reproduces 2 ch @ 10 Hz on an 8 req/s budget — before: 24 readings on one stamp over 2.875 s (15 with a refused batch and single-PID fallback); both variants now inverted in `tests/test_capture_rounds.py`._
- [x] C02.2 Introduce explicit round state: capture generation, round ID,
  scheduled start, deadline, selected members due in this round, attempted
  members, and successful observations. End a round on membership completion
  or deadline, even while later work is due.
  _done: frozen `RoundState` (generation, round_id, scheduled start, deadline, members, attempted, observations, first/last acquired → skew) + `RoundScheduler`; rounds end on completion or deadline even while work is due; expiry runs before selection so no reading is stamped with an expired round._
- [x] C02.3 Define mixed-rate membership. Fast channels belong to every fast
  round; context channels join only when their cadence requires it. A channel
  must not be attempted repeatedly in one round simply because it is overdue.
  _done: round period = fastest pollable cadence; members = requests due before the deadline; 4 Hz/1 Hz test shows context every 4th round; one attempt per member per round (`among=remaining`)._
- [x] C02.4 Schedule rounds on monotonic deadlines. Mark missed/incomplete
  rounds explicitly and skip obsolete deadlines rather than issuing a burst
  of synthetic catch-up samples. Keep request budgeting separate from desired
  sampling rate.
  _done: deadlines `anchor + k·period` on the monotonic clock; slots never reused; falling behind counts `rounds_skipped` with no catch-up burst; budget pacing (`next_budget_at`) stays separate and a shortfall ends the round `incomplete`, counted._
- [x] C02.5 Select same-round Mode 01 companions together, bounded by ECU and
  transport capability. Prioritize the required tuple's completion without
  violating configured rates or starving other due members. Record actual
  cross-batch acquisition skew.
  _done: `select_batch_companions(members=, preferred=)` batches this round's members with the profile's `required_tuple` first; per-round acquisition skew measured and exposed (`round_skew_max_s`, `last_skew_s`). On-demand (rate-0) work opens a nominal round so a triggered freeze frame still goes out and the bus keeps being received (verifier finding fixed)._
- [x] C02.6 Carry acquisition time and round identity separately through
  `ObdReading`, `TelemetryRecord`, Parquet, replay, and public readers. Prefer
  acquisition time for measurement chronology. Keep any legacy presentation
  stamp explicitly labeled and provide versioned compatibility for old files.
  _done: `ObdReading` gains `acquired_clock`, `response_interval_s`, `generation`, `round_id`; `ts_schema` 2 metadata always persists `acquired_at` (+ clock); Parquet → replay → `read_acquisition_frame`/`read_channels` round-trip tested; legacy rows keep `stored`/`round` basis per C01 rules; nothing reconstructed._
- [x] C02.7 Preserve received-frame timing through response reassembly.
  Declare whether a multiframe response uses first-frame or completion time;
  retain a response interval when needed. Do not describe a shared response
  timestamp as proof that the ECU sampled all sensors atomically.
  _done: python-can `Message.timestamp` used as kernel arrival when within 5 s of wall clock (else userspace, recorded); multiframe = first-frame arrival + `response_interval_s`; batched Mode 01 share receipt time with an explicit "not atomic sampling" docstring._
- [x] C02.8 Reset round state on profile changes, stop/start, pause/resume,
  reconnect, and replay transitions. Tag or finish in-flight requests against
  the generation in which they began so they cannot complete a new capture's
  tuple.
  _done: `capture_generation` bumps on profile activate, pause/resume (on change), passive toggle, replay start/finish, and each loop start (reconnect); in-flight requests keep the issuing generation/round (tested)._
- [x] C02.9 Sweep timestamp consumers: live freshness, status, storage session
  rotation, preprocessing pivot/resampling, derived rows, rate summaries,
  tuning reports, and replay. Preserve readable historic sessions without
  claiming that missing acquisition evidence can be reconstructed exactly.
  _done: consumer sweep table in `docs/capture-rounds.md` (freshness lag now bounded by one round period; preprocessing identical for round-stamped rows; scheduler `/status` payload JSON-safe; dashboard `CaptureRoundStatus` type added, `pnpm typecheck`/`test` green; replay carries and rebases metadata and no longer crashes on nullable `value_bool`)._

## Regression and acceptance checklist

- [x] C02.10 Verify bounded round duration under overload, timeouts, permanent
  refusals, partial batches, and single-PID fallback. The 875 ms counterexample
  must produce distinct rounds or explicitly incomplete rounds.
  _done: bounded rounds under overload, timeouts longer than the period, permanent NRC, partial batch, single-PID fallback; counterexample yields distinct rounds with the shortfall counted incomplete._
- [x] C02.11 Verify six-PID batches plus additional members share a round ID
  while retaining distinct response times. Test 4 Hz fast / 1 Hz context
  scheduling and continuously due 15 Hz DID work.
  _done: six-PID batch + extra members share a round id with distinct `acquired_at`; 4 Hz/1 Hz membership; continuously due 15 Hz DID work steps exactly 1/15 s._
- [x] C02.12 Round-trip metadata through the real writer and readers, not
  only mocked records. Test no repeated-round timestamp can collapse distinct
  acquired measurements in analysis.
  _done: round-trip through the real writer and readers; `compare_channels` pairs round-stamped rows on acquisition (40 pairs at 50 ms, refused at 10 ms) — a shared stamp cannot collapse distinct measurements._
- [x] C02.13 Extend [test_multi_pid_batching.py](../../../../tests/test_multi_pid_batching.py),
  [test_can_obd_collector.py](../../../../tests/test_can_obd_collector.py), and
  [test_tuning_frame.py](../../../../tests/test_tuning_frame.py). Check preprocessing
  rate-of-change and gap handling against persisted data.
  _done: multi-PID batching, collector, tuning-frame, replay and resampler/gap-flag tests extended (20 new tests in `tests/test_capture_rounds.py` plus 7 across existing files)._
- [x] C02.14 Acceptance: each successful reading belongs to one capture/round
  generation, every round terminates, and round identity never substitutes for
  measured temporal coherence.
  _done: every reading carries exactly one (generation, round); every round terminates at its deadline; skew is measured per round and exposed rather than inferred from identity._

## Handoff

- [x] C02.15 Document the timestamp/round schema, legacy-reader behavior, and
  the maximum-skew evidence C03 can consume. Apply the shared validation gate.
  _done: `docs/capture-rounds.md` (schema versions, round/generation rules, multiframe and batched-receipt policy, legacy-reader table, `round_skew()` as C03's evidence, consumer sweep, measurements). Gate green._
