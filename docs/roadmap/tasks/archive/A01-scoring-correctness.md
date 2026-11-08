# A01 — scoring correctness at serve time

> **Archived 2026-11-08.** Six of seven items landed. A01.6's remainder, the Pi
> measurement of a hot-swap, continues as **D03.44** in `../D03-open-work.md`.

**Depends on:** nothing. These are defects in how the live daemon scores
ticks, independent of what the models learned and of the deferred retrain.
Follow [A00](../A00-anomaly-detectors-README.md).

The sequence detector is stateful: it scores a 30-tick window. Three paths
let that window mix rows that do not belong together, and one path hands a
tick another tick's result. Each fix below must come with a test that fails
on the current code.

## Checklist

- [x] **A01.1 — Match worker replies to the tick that asked.**
  `InferenceWorker.compute` (`src/daemon/inference_worker.py`) sends a tick,
  waits `result_timeout_seconds`, and on timeout returns `None`. The late
  reply stays in the pipe. The next `compute` sends tick N+1, finds tick N's
  reply already waiting, and returns it. `WorkerReply.index` exists but is
  never compared with `tick.index`. From the first timeout on, every tick
  receives the previous tick's result, and each further timeout adds a tick
  of lag. `compute_wait` looks healthy throughout, because a reply is always
  already waiting.
  - Drain and discard replies whose `index` is not the current tick's, and
    count them.
  - Expose the stale-reply count beside the existing fallback counters in
    `/status`.
  - **Test:** a fake child that answers tick 0 after the timeout; assert that
    tick N receives result N, not N−1.
  - **Likely trigger:** a model hot-swap in the child (2–7 s to load) against
    the 3 s timeout (A01.6).

  *Done 2026-09-22.* `InferenceWorker.compute`
  (`src/daemon/inference_worker.py`) reads replies until one carries this
  tick's `index`. It discards and counts earlier ones on the same deadline, so
  a lone stale reply is still a timeout. The count is published as
  `/status.inference.worker_stale_replies`.
  - Tests (`tests/test_scoring_continuity.py`):
    `test_a_late_reply_is_never_applied_to_the_next_tick`,
    `test_a_late_reply_alone_is_a_timeout_not_a_result`,
    `test_discarded_late_replies_reach_the_timing_snapshot`.
- [x] **A01.2 — Never score the sequence detector in-process while a worker
  exists.** On a worker timeout, `_compute` (`src/daemon/inference.py`) falls
  back to `_compute_tick`, which scores on the parent's own sequence engine.
  That engine's buffer advances only on fallback ticks, so its windows are
  stitched from rows that can be hours apart, while the child's window misses
  those rows. The Isolation Forest scores one row at a time and can keep its
  in-process fallback; the sequence detector cannot.
  - On a worker timeout, record the sequence result as skipped with a named
    reason (`worker_timeout`); do not compute it in the parent.
  - **Test:** after a timeout, no sequence result comes from the parent
    engine.

  *Done 2026-09-22, by a different route than written above.* Skipping the
  in-process sequence score outright would leave the detector silent for the
  rest of a run whenever the child dies. Instead, the window buffer now
  clears on a time gap (A01.3). After a single timeout, the parent engine's
  last row is from its previous fallback run, so it clears and scores
  nothing. It scores again only if a run of consecutive fallback ticks
  refills it, which is correct scoring on contiguous rows, never a window
  stitched across hours. The `_compute` docstring records this.
  - Test: `test_a_fallback_engine_idle_since_long_ago_scores_nothing`.
- [x] **A01.3 — Reset the sequence window at every discontinuity.** The window
  buffer (`src/models/inference.py`) clears only on a session change or a tick
  with no finite feature. Neither happens in practice:
  - Live rows carry `session="live"` always (`src/preprocessing/live.py`), so
    the session-change clear never fires.
  - `SequenceDetector.prepare` returns before appending when
    `ctx.scoring_allowed` is false, so key-off, stalls and stale-OBD periods
    freeze the buffer instead of clearing it.

  Clear the window on:
  - a transition from not-scorable back to scorable;
  - a timestamp jump larger than a configured number of ticks;
  - a pipeline generation change;
  - a model hot-swap.

  The window then refills before scoring resumes. Do not rely on the
  `"live"` label.
  - **Reproduction to turn into a test:** feed the tail of one drive, then
    the start of another 20 minutes later. Today 11 of 29 straddling windows
    were flagged, against 0.47 % on the same data scored cleanly. After the
    fix, no scored window may span the gap.
  - **Scope note:** the UPS shuts the Pi down 300 s after key-off, so a
    reboot already resets an overnight gap. What straddles today is short
    stops, stale-OBD stretches mid-drive, and replays.

  *Done 2026-09-22.* Two rules, each one place:
  - **Time gap.** `WINDOW_MAX_GAP_SECONDS` (5 s, `src/models/sequence_dataset.py`)
    is read by both the live `SequenceWindowBuffer` and offline
    `build_sequence_windows`. A row more than 5 s after the previous one, or
    earlier than it, starts a new window. Δt restarts there too, offline and
    live alike, so a model is calibrated on the windows it serves. This
    changes training windows only where a drive has a hole longer than 5 s,
    and it takes effect at the next retrain.
  - **Generation change.** `SequenceDetector.prepare` compares the
    pipeline's `GenerationToken` with the previous job's. A change (replay,
    capture or model version) marks the job `reset`, which clears the window
    in-process and in the child (`WorkerTick.reset_sequence`).

  *Not done, deliberately:* a reset on every not-scorable → scorable
  transition. A classifier flickering for one tick would blind the detector
  for 30 s each time. Interruptions longer than 5 s are caught by the gap
  rule.
  - Tests: `test_a_live_window_never_straddles_a_stop`,
    `test_a_short_gap_keeps_the_window`,
    `test_time_going_backwards_restarts_the_window`,
    `test_offline_windows_and_live_delta_restart_at_the_same_gap`,
    `test_a_generation_change_asks_for_a_window_reset`,
    `test_in_process_compute_clears_the_window_when_asked`,
    `test_the_reset_rides_to_the_scoring_child`.
  - Run against the unfixed tree (`69c5064`), 10 of the 11 new tests fail.
    The one that passes is the short-gap guard, which must pass on both.
- [x] **A01.4 — Replay start and stop reset the sequence engines.**
  `_reset_in_memory_state` (`src/control/replay_control.py`) resets the live
  feature source and the persistence gate, but not the sequence engine in the
  parent or the child. A replay's first windows therefore contain the live
  drive's last rows, and the live drive resumes with the replay's.
  - Reset both engines on replay start and stop.
  - **Test** through the replay control path.

  *Done 2026-09-22 through A01.3's generation rule, not in `replay_control`.*
  The replay id is part of the `GenerationToken`, so the first job after a
  replay starts or stops resets the window in whichever process scores it.
  That also covers a capture starting and a model hot-swap, without
  `replay_control` having to reach into either engine.
  - Test: `test_a_generation_change_asks_for_a_window_reset` walks
    start → stop.
- [x] **A01.5 — Suppressing a channel must not change the encoder's input.**
  `score_window` sets the mask to 0 for suppressed columns. The mask is also an
  encoder input (GRU-D and TCN), and Δt stays 0, a combination never seen in
  training. The 2026-09-20 artifact contains all 12 columns that `canary.yaml`
  suppresses. The measured effect on other channels is small (median shift
  0.024, p99 0.31, against a typical channel threshold of 1.57), but the
  mismatch is structural.
  - Exclude suppressed channels from the *score and attribution only*; feed
    the encoder what it was trained on.
  - Record the training-time suppress list in the manifest, and name any
    difference at load in `/status`.

  *Encoder half done 2026-09-22.* `score_window` (`src/models/inference.py`)
  passes the caller's mask to the model. A separate `score_mask` zeroes the
  suppressed columns for the error, the global score and the top-channel
  evidence.
  - Test: `test_suppressing_a_channel_does_not_change_what_the_encoder_sees`.
  - The existing
    `test_sequence_inference_suppress_channels_silences_alert_from_noise_channel`
    still passes.

  *Manifest half done 2026-09-22.* Training drops config-suppressed channels
  from the schema (`scripts/train_sequence.py`), so a trained column that
  serving suppresses means the two ran with different `suppress_channels`;
  the 2026-09-20 artifact holds all 12 that the Pi's `canary.yaml` suppresses.
  - `scripts/train_sequence.py` writes `schema.suppress_channels =
    {configured, excluded_columns}` into the manifest
    (`training_suppress_record`, `src/models/inference.py`).
  - `SequenceInferenceEngine` builds a `SuppressionComparison` at load and
    logs a warning when it differs. `SequenceDetector.prepare` publishes it
    at the first scorable tick after the engine is built (so it is absent
    while the truck stays engine-off) as
    `/status.health.sequence_suppression`: `training`, `serving`,
    `added_at_serve`, `dropped_at_serve`, `differs`, and
    `trained_columns_suppressed_at_serve`.
  - An artifact that predates the record shows `recorded_at_training: false`,
    with added/dropped `null` (unknown). The trained columns that serving
    suppresses are still named, because a suppressed column left in the
    schema was not suppressed at training. For the 2026-09-20 artifact that
    names the 12 columns.
  - Tests: `test_train_records_the_suppress_list_it_ran_with`
    (`tests/test_train_lstm.py`),
    `test_the_engine_names_a_suppress_list_that_differs_from_training`
    (`tests/test_lstm_inference.py`),
    `test_an_unrecorded_suppress_list_still_names_the_trained_columns_serving_drops`,
    `test_a_recorded_suppress_list_names_what_serving_added_and_dropped`,
    `test_the_suppress_comparison_reaches_status_when_the_engine_loads`
    (`tests/test_scoring_continuity.py`). All 5 fail against `606e4ab`.
  - The thresholds of the 2026-09-20 artifact still count those 12 columns.
    Only a retrain with a matching list removes that (D03.25), and it is not
    a code item.
- [~] **A01.6 — A model hot-swap is not a scoring timeout.** → **carried to D03.44** (2026-11-08). Loading a new
  artifact in the child takes about 2–7 s against a 3 s reply timeout, which
  is the likely origin of A01.1's lag.
  - Have the child acknowledge readiness after a swap, and have the parent
    treat the swap window as "sequence not ready", not as a timeout.
  - Measure the swap time on the Pi (Pi item).

  *Code done 2026-09-22.* The protocol lives in
  `src/daemon/inference_worker.py`.
  - When a tick needs a model version the child has not acknowledged,
    `InferenceWorker.ready_for` sends a `WorkerSwap` instead of the tick.
  - The child (`_WorkerState.swap`) loads, rebuilds its engine, clears the
    window, and answers `WorkerReady`. Startup is the same: the child sends
    `WorkerReady(0)` once its models are loaded.
  - Until the child answers, `LiveInferencePipeline._compute` scores the
    Isolation Forest in-process and drops the sequence job
    (`_without_sequence`). Nothing is sent to the child in that window, so
    no reply can arrive late.
  - A load longer than `SWAP_TIMEOUT_SECONDS` (30 s) counts as
    `worker_fallbacks.swap_timeout`. An acknowledgement of other versions (a
    manifest landing between the two processes' polls) is re-asked after
    `SWAP_RETRY_SECONDS` (5 s) and counted in `version_mismatches`.
  - `/status.detectors.inference.worker_swaps` reports `startup_seconds`,
    `last_swap_seconds`, `max_swap_seconds`, `last_load_seconds` (the
    child's own time), `completed`, `in_progress`, `not_ready_ticks` and
    `version_mismatches`.
  - Tests (`tests/test_scoring_continuity.py`):
    `test_a_hot_swap_holds_ticks_back_instead_of_timing_out` (asserts
    `fallbacks == {}`),
    `test_a_child_that_loaded_another_version_is_asked_again_later`,
    `test_a_late_load_acknowledgement_is_not_a_bad_reply`,
    `test_the_pipeline_scores_the_forest_but_not_the_sequence_while_the_child_loads`,
    `test_the_child_loads_then_acknowledges_a_swap`,
    `test_a_started_child_acknowledges_readiness` (a real spawned child),
    `test_swap_counters_reach_the_timing_snapshot`. All 7 fail against
    `606e4ab`.

  *Remaining (Pi):* deploy, then hot-swap one artifact and read
  `worker_swaps.last_swap_seconds` and `last_load_seconds` from `/status`.
  `startup_seconds` is available from the first boot.
- [x] **A01.7 — Check dropped ticks inside a window.** *Suspected.* The
  depth-1 queue (`src/daemon/inference.py`) drops ticks under load, so a
  30-tick window can span more than 30 s of wall time while Δt accounting
  assumes one tick per row.
  - Measure how often a scored window's wall span exceeds 1.5× its length on
    a real drive.
  - If it matters, carry the real interval into Δt or reset the window.
  - Close with the measurement, whichever way it goes.

  *Instrumentation done 2026-09-22.*
  - Every `SequenceInferenceResult` carries `window_rows`,
    `window_span_seconds` and `window_max_step_seconds`
    (`SequenceWindowBuffer.span`).
  - `LiveInferencePipeline._apply` feeds each scored window to
    `InferenceTimingRecorder.note_sequence_window`. The ratio is span over
    `(rows − 1) × tick`, so consecutive ticks read 1.0.
  - `/status.detectors.inference.sequence_windows` reports `scored`,
    `stretched` (ratio > `STRETCHED_WINDOW_RATIO`, 1.5), `unmeasured`, and
    the rolling median, p95 and max of the ratio, and the p95 and max of the
    longest step.
  - Tests: `test_a_window_with_a_dropped_tick_reports_its_real_span`,
    `test_stretched_windows_are_counted_against_the_tick_period`,
    `test_applying_a_sequence_result_records_its_window_span`
    (`tests/test_scoring_continuity.py`), and
    `test_a_scored_window_carries_its_wall_span`
    (`tests/test_lstm_inference.py`). All 4 fail against `606e4ab`.

  *Offline measurements, from what is stored:*
  - **Current scheduler, one real drive.**
    `~/canary-data/poll-20260922.jsonl` holds 400 compact `/status` polls
    about 5 s apart.
    - While `engine_running` (16:54:02–17:18:34 UTC), 1,473 ticks were
      scheduled over 293 intervals. 2 were missed, both `compute_busy` in one
      interval (16:54:12–17).
    - `skipped_vehicle_state` did not move while running.
    - So no 30-row window lost more than 2 rows. Its ratio was at most
      31/29 = 1.07, and none can have exceeded 1.5×. Over the whole log, 3 of
      3,184 ticks were missed.
    - This is a bound from counters, not a per-window count.
  - **Pre-scheduler pipeline.** Source: the Pi's anomaly logs,
    `~/canary-data/anomalies/nissan-xterra-2008/`, 2026-05-25 to 2026-08-06.
    That is 9,425 *flagged* sequence windows, all 30 rows. Every one predates
    the C08 deadline scheduler (`a690044`, 2026-09-07).
    - Per day, 89.6–100 % of them spanned more than 1.5×. The per-day median
      ratio was 1.61–2.30.
    - 0–6.7 % per day spanned more than 5×. Those windows contained a hole
      longer than 5 s, which A01.3 now resets.
    - The old loop slept a full interval *after* each tick's work, so a
      nominal 1 s tick ran at about 1.6 s. This set is flagged windows only
      and says nothing about the current loop.
    - Script:
      `a017_spans.py` (scratch, not committed): span =
      `window_end − window_start`, rows = length of `reconstruction_series`.
  - **Decision on the evidence so far:** no Δt or reset change. With at most
    2 lost rows per window on the current scheduler, the effect does not
    matter.

  *Closed 2026-10-04 drive (Pi `a1da058`, daemon up 19:43:17Z, profile `default`; engine running 19:43–20:16Z, passive 19:48:23–~20:00Z by another caller, non-passive otherwise):* `sequence_windows` read **1,739 scored, 0 stretched, 0
  unmeasured**; span ratio median 1.000, p95 1.000, max 1.001; longest step
  p95 and max 1.02 s, over 2,271 ticks with 23 missed (8 `compute_busy`, 15
  `overrun`). A dropped tick does not stretch a scored window on the truck,
  so Δt needs no change. *Was:* one drive with this code deployed, then read
  `detectors.inference.sequence_windows` (`stretched` / `scored`) to replace
  the counter bound with a per-window count and close.

## Acceptance

Every item lands with a failing-then-passing test. A01.1–A01.4 are
correctness fixes and do not wait for any model decision in A06. After they
land, replay one known-good drive and one drive with a mid-drive stop.
Report sequence flags per window alongside the pre-fix result for the same
rows; this is a same-row comparison, never totals across different drives.
