# A01 — scoring correctness at serve time

**Depends on:** nothing. These are defects in how the live daemon scores
ticks, independent of what the models learned and of the deferred retrain.
Follow [A00](A00-anomaly-detectors-README.md).

The sequence detector is stateful: it scores a 30-tick window. Three paths
let that window mix rows that do not belong together, and one path hands a
tick another tick's result. Each fix below must come with a test that fails
on the current code.

## Checklist

- [ ] **A01.1 — Match worker replies to the tick that asked.**
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
- [ ] **A01.2 — Never score the sequence detector in-process while a worker
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
- [ ] **A01.3 — Reset the sequence window at every discontinuity.** The window
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
- [ ] **A01.4 — Replay start and stop reset the sequence engines.**
  `_reset_in_memory_state` (`src/control/replay_control.py`) resets the live
  feature source and the persistence gate, but not the sequence engine in the
  parent or the child. A replay's first windows therefore contain the live
  drive's last rows, and the live drive resumes with the replay's.
  - Reset both engines on replay start and stop.
  - **Test** through the replay control path.
- [ ] **A01.5 — Suppressing a channel must not change the encoder's input.**
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
- [ ] **A01.6 — A model hot-swap is not a scoring timeout.** Loading a new
  artifact in the child takes about 2–7 s against a 3 s reply timeout, which
  is the likely origin of A01.1's lag.
  - Have the child acknowledge readiness after a swap, and have the parent
    treat the swap window as "sequence not ready", not as a timeout.
  - Measure the swap time on the Pi (Pi item).
- [ ] **A01.7 — Check dropped ticks inside a window.** *Suspected.* The
  depth-1 queue (`src/daemon/inference.py`) drops ticks under load, so a
  30-tick window can span more than 30 s of wall time while Δt accounting
  assumes one tick per row.
  - Measure how often a scored window's wall span exceeds 1.5× its length on
    a real drive.
  - If it matters, carry the real interval into Δt or reset the window.
  - Close with the measurement, whichever way it goes.

## Acceptance

Every item lands with a failing-then-passing test. A01.1–A01.4 are
correctness fixes and do not wait for any model decision in A06. After they
land, replay one known-good drive and one drive with a mid-drive stop.
Report sequence flags per window alongside the pre-fix result for the same
rows; this is a same-row comparison, never totals across different drives.
