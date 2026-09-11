# D03 — what is still open

Created 2026-09-11, when `D01-daemon-throughput.md`, `D02-drive-findings.md`
and `PENDING-HARDWARE.md` were archived. Their implementation is complete and
their evidence is in the archived docs; this is the eight items that were not
closed, each keeping its **original ID**, its original wording, and a pointer
back to the doc it came from.

Same convention as the file it is modelled on: **task-level truth is the
checkboxes here**, `[x]` done, `[~]` partial with the remaining work named.
Mark `[x]` only with the measurement in hand, and link the artifact.

**Three of the eight need the truck, and no bench run substitutes.** One needs
a decision from the operator. Four can be done at a desk today.

## Rules that still apply

- Read `/status.vehicle_state` before interpreting anything (H0).
- Never widen a queue or relax a threshold to make a contract pass — stop at
  the first failure, name the constraint, fix its source.
- No bus-budget raises; no rate default raised without sustained evidence.
- **Long replays are currently unsafe on the Pi** — see D02.9.

---

## Can be done at a desk

- [ ] **D02.9 Replay grows without bound and OOM-killed the daemon.**
  A 1x replay of a 25-minute drive took the daemon to **7.7 GB resident on an
  8 GB Pi** and the kernel killed it 32 minutes in
  ([evidence](../../../data/analyses/2026-09-11-d026-replay-event-rates.md)).
  It wrote **5,860,000 rows in 586 chunks** while covering **32 %** of a
  source session holding **4,544,560 rows in 511 chunks** — more rows than its
  input contains, over a third of the span. Something amplifies rows and
  memory grows with it. **Do not guess the mechanism**; find it, then decide
  whether the fix belongs at the emit path or the writer.
  This is load-bearing: `start_replay` is the sanctioned way to evaluate
  detectors without the truck (AGENTS.md), and today a long one takes the
  daemon down. Pre-existing, not a D01.9 regression.
  _From D02._

- [ ] **D02.10 Get a known-good baseline session back onto the Pi.**
  D02.6 characterised the anomaly-event rate (3.27/min, sequence detector 60 %
  of it) but could not **judge** it, because retention has swept every session
  the deployed models were trained on. Push one from `~/canary-data` (e.g.
  `2026-07-26T234833`), **pin it** so retention cannot take it, re-run D02.6's
  replay against it, and compare per-detector rates. Until then "3.27 events
  per minute" has no reference. Blocked behind D02.9 for anything long.
  _From D02._

- [ ] **D01.10 Rebuild `reconcile_feature_frame_to_spec` instead of mutating
  it.** 54.6 ms per call on a one-row 327-column frame, every tick in the
  serving path. The naive batching is *slower* (62.7 ms) — measured, reverted
  — because the cost is block-manager churn, not conversion: `iset` /
  `_iset_split_block` / `blocks.delete` dominate, and a bulk column assignment
  splits blocks harder than the per-column loop. The fix is to construct the
  aligned output frame once from a dict of finished Series rather than
  inserting into and mutating `out`. Moderate risk — this function produces
  the model input, so preprocessing parity is non-negotiable and gets proven
  on the real chain the way D01.8 was.
  Worth ~25 % of `compute`, which no longer blocks the event loop, so this is
  a comfort item rather than a deadline one.
  _From D01._

- [~] **D02.5 Trace why the Isolation Forest was blind — retrain still owed.**
  Root cause found and the alarm shipped: six of the artifact's 88 features
  were **corrected out of `data/bindings.yaml` after it was trained**
  (`ECM_AIRFLOW_PROXY_23D` renamed 2026-08-04, `ECM_CALC_LOAD_B` retracted
  2026-08-05, both about a week after the 2026-07-27 artifact), and 6/88 =
  6.8 % clears the 5 % withhold ceiling on **every row, forever**.
  `src/models/feature_spec_health.py` now computes the verdict and
  `/status.detectors.isolation_forest.structural_blindness` carries it.
  **What remains is the retrain against current bindings**, which is a corpus
  decision — which sessions, which modes — and therefore the operator's, not
  something to take automatically. Until it lands the IF contributes nothing:
  D02.6 measured it at **0 of 104 events**.
  **The durable lesson, worth enforcing somewhere: a binding retraction is a
  feature-spec change.** Nothing linked a `bindings.yaml` edit to the
  artifacts depending on it, and fixing a wrong binding silently blinded the
  detector trained on it.
  _From D02._

## Needs the truck

- [ ] **D01.5 Always-draining CAN receive path.** The daemon loses broadcast
  frames at its socket, invisible to `ip -s link` (`rx_dropped` stays flat).
  Re-measured 2026-09-10 at warm idle over 65 s: kernel 120,599 frames vs
  daemon 115,480 = **4.24 % unaccounted**, down from 6.6 % once GIL starvation
  was removed but not gone. Treat 4.2 % as a **floor** — the two counter reads
  are not atomic and the daemon's window is the wider, which biases it low.
  The fix is a dedicated reader thread that owns `recv()` and fans out to the
  tap and the response matcher. **Not a bigger buffer** — that only delays the
  overflow. Re-measure against the same 65 s bracketed protocol.
  _From D01._

- [ ] **D02.2 Measure the MCP2515 controller overflow, separately from the
  socket loss.** `rx_over_errors` rose by **121** across the 2026-09-10 drive
  and by 14 in a 65 s idle window while `rx_dropped` stayed flat. The MCP2515
  has two hardware RX buffers; an overflow there is the SPI driver not
  draining the chip in time — a loss *underneath* D01.5's, which a reader
  thread cannot touch. **Do not fold the two into one number.**
  Config audit done 2026-09-11 on the bench; two candidates, to be tried one
  at a time and each measured against the same window:
  1. **No `spimaxfrequency` is set**, so the overlay's 10 MHz default applies.
     That is the bus speed the driver drains the RX buffers over and the most
     direct lever. 20 MHz is the conventional next rung.
  2. **A second controller is declared that does not exist** —
     `dtoverlay=mcp2515-can1` probes `spi0.0` and fails every boot
     (`err=110`); only `spi0.1` initialises. Removing it stops a real failure
     being background noise.
  Checked and **not** a defect: the overlay declares a 16 MHz oscillator while
  `ip -d link` reports `clock 8000000`. The MCP2515 halves it. Do not "fix"
  this.
  Both changes need a reboot, and ranking them needs bus traffic.
  _From D02._

- [~] **H6 (C10.12) Compare candidate budgets incrementally under the same
  workload.** Stop increasing when a requested contract fails, identify the
  constraint, and fix its source. Do not hide data loss with larger queues or
  relaxed quality thresholds.
  **Its premise died on 2026-09-10.** The idle ladder's conclusion — requests
  flat at ~7.9 req/s across an 8 → 16 → 32 req/s ladder — was a property of
  the **starved event loop**, not the ECM or the bus. The same daemon with
  D01.8 sustained **116.8 req/s** on the H8 drive. Re-run the ladder on a
  drive before quoting any floor from it; the earlier numbers should not be
  cited at all.
  Two rules built into the sweep, not to be re-litigated: the baseline is the
  rung with the **most** headroom, and a ceiling is a failure **above** a rung
  that held.
  _From PENDING-HARDWARE (originally C10)._

## Deferred by decision, not blocked

- [ ] **D01.4 Get the feature build off the event loop.** `source.tick()` runs
  in phase 1 on the loop and holds it for **169 ms** median (was 332 ms before
  D01.8). It cannot simply move to a thread — it is Python-level pandas and
  holds the GIL. Moving it means moving the ring buffer that `observe()` feeds,
  and the behavior detector that reads `source.latest_operating_point`, into
  the child as well.
  **No longer a deadline argument.** The tick fits its budget: 439 ms median at
  idle, 320 ms under the heavier `tuning-did` load, and D01.9 now overlaps the
  snapshot with the score. What remains is that the loop is occupied ~17 % of
  each second, which is a `/status` latency and collection-jitter argument, not
  a missed-deadline one.
  Design it before building it; this is still the larger half of the problem.
  Reopen it if a future drive puts the tick back over budget.
  _From D01._

---

## Suggested order

**D02.9 first** — it is a live defect, it is desk work, and it blocks D02.10
and any further replay-based evaluation.

Then **D02.10**, which gives every future detector claim a reference it
currently lacks. Then **D02.5's retrain**, once the operator has picked the
corpus, since the IF is contributing nothing until it lands.

**D01.5, D02.2 and H6 travel together** — they are one drive's work, the way
the H-items were, and two of them are measurements of the same receive path
at different layers.

**D01.10 and D01.4 are last on purpose.** Neither is forced by a deadline any
more, and both touch the serving path.
