# D03 — what is still open

Created 2026-09-11, when `D01-daemon-throughput.md`, `D02-drive-findings.md`
and `PENDING-HARDWARE.md` were archived. Their implementation is complete and
their evidence is in the archived docs; this began with the eight items not
closed, each keeping its **original ID**, its original wording, and a pointer
back to the doc it came from.

Same convention as the file it is modelled on: **task-level truth is the
checkboxes here**, `[x]` done, `[~]` partial with the remaining work named.
Mark `[x]` only with the measurement in hand, and link the artifact.

The original IDs are retained below; new drive findings use D03 IDs. The
2026-09-18 drive order is at the end of this file.

**Updated 2026-09-17** by an autonomous run during a ~3 h 45 min drive
([log](../../../data/analyses/2026-09-17-drive-log.md)): D01.5's premise was
re-measured and its headline figure is in doubt, D02.2 gained a controlled pair
and a third candidate fix, and five new items (D03.1–D03.5) were opened, three
of them already fixed and deployed. Afterwards, at the desk, **D02.9 was
root-caused** ([evidence](../../../data/analyses/2026-09-17-d029-replay-amplification.md)):
its 4× row amplification is really 1.29× plus a wall-time-versus-rows
arithmetic error, and its OOM belongs to the trip-end digest, now D03.6. D03.7
records why the validation harness could not have caught either.

**Evening review, through `dcde580` (2026-09-17 MDT):** replay filtering and
emission counters landed in `c15f66a`; compact status and explicit tag refusals
in `3d45680`; service memory limits in `2dfa0aa`; trip-end digest isolation and
refusal sidecars in `dcde580`. These are implementation progress, not evidence
that tomorrow's Pi is running them. D02.9 and D03.6 remain partial for the
specific gaps below. D03.8–D03.13 capture the missing fuel-workflow experiments,
live verification, and review follow-ups. Today's recordings already support
the first calibration analysis; a new drive is not its prerequisite.

**Operator constraint for September 18: CAN only.** The K-line + CAN board is
not ready. D03.10 and the external-reference part of D03.12 are deferred;
D03.14–D03.17 below add experiments that need no new UpRev log, K-line tap or
USB capture. Existing logs may inform preparation, but are not tomorrow's
simultaneous ground truth. These experiments can establish CAN access or an
estimated signal; they cannot establish how UpRev calculates its display.

## Rules that still apply

- Read `/status.vehicle_state` before interpreting anything (H0).
- Never widen a queue or relax a threshold to make a contract pass — stop at
  the first failure, name the constraint, fix its source.
- No bus-budget raises; no rate default raised without sustained evidence.
- Verify the deployed SHA and effective service limits before using the new
  digest protections. D03.6 now isolates automatic trip-end computation and
  refuses oversized sessions; it has not fixed the raw-resolution memory cost
  or isolated the on-demand HTTP computation. Keep bulk analysis on the laptop.
- Replay still admits live sensor rows (D02.9). Do not use a parked replay as
  a detector baseline until input isolation is established; engine-off alone
  does not silence the GPS receiver.

---

## Can be done at a desk

- [~] **D02.9 Replay writes 1.29× its input, and the OOM was never the replay.**
  **Root-caused 2026-09-17, read-only, no replay run**
  ([evidence](../../../data/analyses/2026-09-17-d029-replay-amplification.md)).
  The original framing was wrong in both halves: nothing grows without bound, and
  memory did not grow with rows. Measured per `source` against the real source
  session (counted, not estimated from chunk counts):

  | source | source | replay | ratio |
  |---|---:|---:|---:|
  | `can` | 3,133,682 | 3,110,773 | **0.993** |
  | `obd` | 36,715 | 36,461 | **0.993** |
  | `decoded` | 1,319,681 | 2,620,062 | **1.985** |
  | `derived` | 19,248 | 34,624 | 1.799 |
  | `gps` | 35,234 | 58,080 | 1.648 |
  | total | 4,544,560 | 5,860,000 | **1.289** |

  Three separate things, only one of which is a row amplifier:
  1. **It consumed ~99 % of the source, not 32 %.** `can`/`obd` at 0.993× prove
     it read the whole session; the missing 0.7 % is the chunk in flight at the
     kill. "32 % of the span" was a **wall-time** fraction (1,910 s of clock
     against a 5,889 s recording) multiplied against a row count. A "1x" replay
     outruns its recording because `_MAX_GAP_SECONDS = 0.5`
     (`replay.py:46,160-167`) clamps the *sleep*, so any recorded gap costs at
     most 0.5 s — 18.9× effective on one real local session, 3.08× here — while
     the API, status and log all still say `speed: 1.0`. **Report
     `rows_emitted`/`source_rows_total` and the effective speed; do not add a
     throttle** — the clamp is deliberate, the silence about it is the defect.
  2. **`decoded` is persisted twice, at the emit path.** `_iter_rows`
     (`replay.py:101-126`) re-emits the stored `decoded` rows *and* replayed
     `can` rows keep their payload, so the storage sink re-derives them
     (`drain.py:212-217` → `frame_decoder.py:88-118`). `derived` doubles the
     same way. The copies are not byte-identical (replay rebases timestamps), so
     de-duplication after the fact will not find them.
     **Layer question answered 2026-09-17: the emit path.** The detectors do
     not need the replayed `decoded` rows — `LiveFeatureSource.observe`
     (`live.py:256+`, `335-344`) decodes raw `can` rows itself into the same
     `decoded:<SIGNAL>` current values. Today's behaviour is in fact *ambiguous*,
     not merely redundant: a replayed `decoded` row writes the same key as the
     freshly decoded one, so the detectors see whichever arrived last — and the
     two differ exactly when `bindings.yaml` changed since the recording, which
     is when a replay is most likely being used to check that change.
     One constraint on the fix: payload persistence landed 2026-07-29, so in an
     older session `can` rows carry no payload, nothing can re-derive, and the
     stored `decoded` rows must still be emitted — decided per session, up front,
     and *reported* rather than silently switched.
  3. **Live GPS contaminates the output** — `gps` at 1.648× is ~22,800 rows that
     are not in the source: the Pi's own receiver kept writing into the active
     session directory, which during a replay is the `_replay-*` dir, at ~12
     rows/s. `can`/`obd` escaped only because that replay ran parked. So a
     replay output is the replay **plus whatever the live sensors produced
     meanwhile**, and any comparison of its GPS or derived-speed channels against
     the source is comparing two different drives. New; not previously recorded.

  **Implemented in `c15f66a`:** `probe_rederivability` selects the emit policy
  from sampled raw CAN payloads; modern recordings skip stored `decoded` and
  `derived`, legacy recordings retain them. `ReplayEmissionStats` and
  `ReplayStatus` expose source rows, emitted/skipped/dropped rows, completed
  passes and the policy reason. The validation report carries those counters.
  **Still owed:**
  - Isolate live producers at replay boundaries, including GPS and queued
    records; `ReplayController.start` still shares the live queue and changes
    output directories without stopping their input. Prove restoration on stop.
  - Report effective replay speed / source-time progress; `speed` still means
    the requested multiplier only. Reconcile a completed pass using actual
    emitted + skipped + dropped rows, not elapsed wall time or chunk counts.
  - Run the storage-chain regression in D03.7 and a Pi comparison. Cover both
    payload-bearing and legacy baseline sessions; do not infer that the
    first-three-chunk probe guarantees complete payload coverage in every
    recording. Record the selected policy with each result.
  The memory half is separately tracked in D03.6.
  **2026-09-22 (desk): the speed/progress half landed.** `ReplayEmissionStats`
  and `ReplayStatus` (so `GET /control/replay` and `get_replay_status`) now
  carry `source_span_s` (Parquet footer statistics only,
  `loader.session_time_span_seconds`), `source_time_emitted_s`,
  `wall_elapsed_s`, `effective_speed`, `source_time_progress` and
  `pass_reconciled` (emitted + skipped + dropped == passes × source rows);
  `speed` is documented as the requested multiplier only, and the pass-complete
  and replay-finished logs carry the same numbers. A 600 s recorded pause at
  `speed=1.0` replays in < 3 s wall and reports effective speed > 100× with
  the pass reconciled
  (`tests/test_replay.py::test_a_clamped_gap_is_reported_as_effective_speed_not_hidden_behind_speed`,
  `::test_pass_reconciliation_counts_rows_not_wall_time`,
  `tests/test_replay_control.py::test_status_reports_effective_speed_and_source_time_progress`).
  ~~**Still owed:** live-producer isolation at replay boundaries~~ **Done
  2026-09-25 (desk).** `run_daemon` hands every live collector a
  `src.daemon.ingress.LiveIngress` (a write-only door onto the queue) instead
  of the queue, and a `drain.PipelineBarrier` joins the ingest queue and every
  sink queue. `ReplayController.start` closes the ingress *first*, settles the
  pipeline so live records already queued land in the live session, then
  redirects the writer; on stop it settles the replay's own tail into
  `_replay-*`, switches back, then reopens the ingress (a failed switch
  reopens it too). Withheld live rows are counted per source:
  `ReplayStatus.live_rows_withheld` (live while running, baked into the idle
  status and the "replay finished" log), plus `entry_settled`/`exit_settled`
  (a 5 s bound; `False` says a boundary was cut). Tests:
  `tests/test_replay_live_isolation.py::test_live_gps_is_withheld_from_a_replay_and_restored_after`
  (real `run_daemon`, a 100 Hz live GPS collector through a replay of a
  40-row source: `_replay-*` holds exactly `{RPM: 40, SPEED: 40}`, withheld
  `gps > 0`, live GPS back in the live session after stop; shown failing with
  collectors on the bare queue: `{'LIVE_RECEIVER_LAT': 66, 'RPM': 40, 'SPEED':
  40}`), `tests/test_replay_control.py` boundary-order, failed-switch and
  write-only tests, and the strict-xfail
  `tests/test_replay_row_accounting.py::test_live_gps_does_not_contaminate_replay_output`
  now passes (the validation harness routes its simulated receiver through a
  closed `LiveIngress`; xfail removed). The replay's OBD *polling* is not
  stopped — only its rows are withheld. **Still owed:** the Pi comparison over
  both a payload-bearing and a legacy baseline session, recording the selected
  policy with each result.

  Evidence preserved before the 7-day replay sweep: source `2026-09-10T142711`
  **pinned** on the Pi, and both directories copied to
  `~/canary-data/_d029-evidence/`.
  _From D02._

- [~] **D02.10 Known-good baseline sessions are back on the Pi and pinned.**
  D02.6 characterised the anomaly-event rate (3.27/min, sequence detector 60 %
  of it) but could not **judge** it, because retention had swept every session
  the deployed models were trained on.
  **Done 2026-09-17: four sessions pushed from `~/canary-data` and pinned**, so
  retention cannot take them. Which sessions was verified against the deployed
  manifests rather than assumed — `models/nissan-xterra-2008/*/model.manifest.json`
  on the Pi:
  - `2026-07-26T234833` (80 chunks) — in `training_sessions` of **both** the
    deployed IF (18 sessions) and LSTM (19). The task's own suggestion, now
    confirmed to be training data for the artifacts actually serving.
  - `2026-07-22T163917` (33), `2026-07-26T162332` (243), `2026-07-27T010704`
    (261) — the **`calibration_sessions`** of both models. These are the better
    reference for an event *rate*: they are the sessions the thresholds were
    calibrated on, so the designed false-positive rate is what a replay of them
    should reproduce. Worth using instead of a training session for the
    comparison.
  **What remains is the replay comparison, before or after the drive.** Verify
  `c15f66a` and `dcde580` are deployed, resolve D02.9's live-input contamination,
  then use a calibration session rather than the training one. These July
  sessions predate raw-payload persistence: record `rederivable` and its reason
  rather than assuming they exercise the modern decode path. Record model
  manifests, scored/withheld ticks, operating-mode coverage, per-detector events
  and the source-time denominator. A structurally blind IF remains a withheld
  detector, not a successful zero-false-positive baseline (D02.5). Do not run
  replay during live collection; explicitly stop it and confirm fresh live
  telemetry before starting the drive.
  **Waits on (2026-09-22):** the Pi — the deployed-build check and the replay
  itself run there against the deployed manifests — and D02.9's live-input
  isolation, which is under `src/daemon/*`. Nothing here is a laptop step.
  _From D02._

- [x] **D01.10 Rebuild `reconcile_feature_frame_to_spec` instead of mutating
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
  **Done 2026-09-22.** Every column is finished in a dict first (the frame's own
  columns, then one shared fill Series for every absent feature, then the
  numeric coercion), and the frame is built once. Extras are still judged
  against the filled frame, as before. A frame with duplicate column labels is
  now refused by name instead of half-working.
  **Parity, the D01.8 way:** an exact old-vs-new comparison on the real chain.
  It covered 420 live `LiveFeatureSource` rows (332 columns) plus three real
  sessions through `build_feature_frame` (2026-08-06T034415 2824×723,
  2026-09-20T205117 1506×729, 2026-09-18T134052 145×103, whole-frame and
  one-row slices), each against both real manifests (IF 295, LSTM 361
  features) and three kwarg variants: 2,592 comparisons, all identical. That
  means column order, dtypes, index, `array_equal(equal_nan=True)`,
  `assert_frame_equal(check_exact=True)` and the report. The permanent guard is
  `tests/test_reconcile_feature_frame_parity.py` (25 cases). It keeps the
  verbatim old function as its reference, the same shape as
  `test_features_batched_parity.py`.
  **Timing** (laptop, median of 200, one-row live benchmark frame of 332
  columns): IF spec **75.1 → 27.7 ms** (2.7×), LSTM spec **98.4 → 29.6 ms**
  (3.3×). On a real 729-column offline row: 59.1 → 40.8 ms and 72.1 → 43.3 ms.
  `scripts/benchmark_live_pipeline.py --profile steady` median tick was
  59.70 → 58.08 ms. That is noise, because the benchmark times only the
  feature stage and never calls the reconciler. The gain shows up in the
  scoring child's `compute`.
  _From D01._

- [~] **D03.22 Mode labels used raw broadcast counts (2026-08-06 → 2026-09-19).
  Fixed `f8b7d38`; recompute owed.** The segmenter's `decoded:` fallback compared
  raw counts (ECM_RPM 8×, ECM_COOLANT_TEMP +40, WHEEL_SPEED_6F7_C 16×) against
  engineering thresholds whenever Mode 01 was stale, so warm idle labelled from
  broadcast came out `unknown`, cold read warm, and 60 km/h read `highway`. Live
  trip/idle had it too. `src/decode/engineering.py` is now the one reader of a
  `decoded:` channel as a physical quantity, and an unscalable candidate is
  refused at `SegmenterConfig` construction. Ground truth with Mode 01 blanked:
  agreement 0.379 → 0.986.
  **Blast radius tracks Mode 01 staleness, not the calendar:** ≤ 0.1 % of labels
  on 2026-09-18 high-rate drives, **~16 % on a 2026-08-06 session** (500
  highway→city, 91 unknown→idle_transient); that session had zero `idle` rows
  pre-fix.
  **Owed, laptop-side, in order:** (1) `label_sessions.py` sidecars regenerate
  (mode/event/disposition); (2) IF + sequence retrain and out-of-sample
  threshold recalibration (a fixed held-out set, not leave-one-out — A02.10),
  since per-mode row membership moves; (3)
  `refit_behavior_models.py` after (1); (4) trip sidecars recompute (idle
  seconds were over-counted on stale-OBD rows); (5) `data/cache/feature_frames`
  can be deleted — the cache version bump already ignores stale entries.
  **Deploy:** code only, needs a Pi pull + restart, and the retrain should land
  before or with it, or correctly-labelled rows get scored by models fitted on
  mislabelled ones.
  **2026-09-22, laptop steps (1), (4), (5) done.**
  **Known answer first.** Run over the training chain's head (decode →
  resample → drop fully-stale → `segment_or_constant`, `canary.example.yaml`
  modes), 2026-08-06T162657 reproduces the figure exactly. At `f8b7d38^` vs
  `f8b7d38`: **601 of 3,780 rows (15.9 %)** change, 500 highway→city and
  91 unknown→idle_transient, with 0 idle rows before. `f8b7d38` → HEAD changes
  0 rows on any of the 106 sessions, so every change below is the fix. That
  session does not fit in 15 GB through the full decode. The labels came from
  a decode limited to the three `decoded:` mode candidates, which matched the
  full chain on 0 of 2,824 and 0 of 1,506 rows (2026-08-06T034415,
  2026-09-20T205117).
  **Blast radius over the local corpus** (106 sessions, 142,893 rows: the
  profile corpus, `sessions-0918`, `sessions-0920` and the 2026-05 dirs):
  **16,993 rows (11.9 %)** change, in 65 sessions; 41 are unchanged. It is not
  confined to 2026-08-06 onward. The pre-fix segmenter mislabels any
  stale-Mode-01 row, so July and May sessions move too, up to 90.8 %
  (2026-07-22T163011, unknown→idle 476/524). High-rate 2026-09-18 drives
  change 0.1–0.5 %, but 2026-09-18T134345 moves 28.1 % and 2026-09-21T010459
  moves 37.6 %. The dominant shapes are highway→city (speed read 16×) and
  unknown→idle (rpm read 8×). This is the row membership the (2) retrain will
  see move.
  **(1) Sidecars regenerated** with `label_sessions.py` on every local root.
  `CLASSIFIER_VERSION` 0.2.0 → **0.3.0**, so a plain stale-only run redoes
  every pre-fix sidecar with no `--rebuild` to remember. That covers the Pi
  too. 116 sidecars (plus fingerprints) are now at 0.3.0: ready 64, review 39,
  quarantine 13, and 13 empty dirs skipped. 41 of them had already been
  relabelled 7–11 min after `f8b7d38` landed (2026-09-20 04:07Z), and those
  came out identical. The 11 2026-05 sidecars, from 2026-05-24 code, moved
  1.6–84.8 % of seconds, and 4 went ready→review.
  **Found:** sidecars segment a different frame from training.
  `classify_session` reads the *persisted* `decoded` rows, and 2026-08-06T162657
  has none for `WHEEL_SPEED_6F7_C`. So the sidecar chain moved only 2.9 % of
  that session where the training chain moved 15.9 %, and after the fix the
  sidecar and training labels still disagree on 94 rows (2.5 %). Not fixed
  here.
  **(4) Trip sidecars:** `trip_summary_sidecar.SCHEMA_VERSION` 2 → **3**. v2
  used raw `ECM_RPM` counts for idle time in any session with no Mode 01 RPM.
  The bump makes any cached v2 recompute on first read. All 4 local sidecars
  recomputed: 2 were v1, from before the dt fix, and changed as that fix
  predicts; the 2 v2 ones came out identical, since they had Mode 01 RPM.
  **(5) Cache:** checked that all 20 entries in `data/cache/feature_frames` are
  keyed by the hash of metadata that includes `cache_version`. The 18
  non-v4 entries can therefore never be hit. Deleted (924 MB).
  **Remaining:** (2) the IF + sequence retrain with held-out recalibration
  (fixed holdout, not leave-one-out — A02.10), and (3)
  `refit_behavior_models.py`. Both are coordinated with B03 / D03.25 and need
  the operator's corpus choice. Also the Pi side: pull + restart, then
  `label_sessions.py` (the version bump makes it relabel) and a trip-sidecar
  read-through there, landing with or after the retrain.
  **2026-10-03 status:** (3) is done. `refit_behavior_models.py` ran on
  2026-09-23 on the relabelled corpus, and the set was promoted
  diagnostic-only and deployed 2026-09-24 (B03.9, `a733430`). The sequence
  half of (2) is done too: the r2 LSTM was retrained on 2026-09-23 and
  calibrated on two held-out drives, a thin set the operator accepted. It has
  served since 2026-09-24 (A06.3). **Still
  owed:** the IF half of (2), which is D03.25 / A06.3, blocked on the
  idle/highway drive floor; and the Pi-side `label_sessions.py` plus the
  trip-sidecar read.
  _Found while checking a false "coolant decode" alarm during the 2026-09-18
  evening drive._

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
  **2026-09-18 evening:** that lesson is now enforced (`32fa211`,
  `tests/test_manifest_producibility.py`). Every tracked manifest is checked
  against what the pipeline can produce, against a reviewed allowlist that fails
  when it goes stale. It also found **the LSTM has the same debt: 8 of 263
  features unproducible** (the same two retracted bindings, all 4 variants
  each). Those are masked under its ceiling, so it still scores, but the
  retrain is owed for both models. `/status` blindness is also fixed
  (`ad0a0bb`): it now reports exactly the 6 IF columns, where startup sparsity
  used to show 17. The corpus choice is with the operator (evening drive log,
  For the operator 2).
  _From D02._

## Needs the truck

- [ ] **D01.5 Re-measure socket receive loss before implementing an
  always-draining path.** The original proposal was a dedicated reader thread
  owning `recv()` and fanning out to the tap and response matcher. It relied on
  a 2026-09-10 warm-idle window: kernel 120,599 frames vs daemon 115,480 over
  65 s, reported as **4.24 % unaccounted** while `rx_dropped` stayed flat.
  That figure's claimed lower-bound interpretation is withdrawn: non-atomic
  counter reads were not adequately bracketed. Establish the remaining loss
  before choosing the implementation; a larger buffer is not its remedy.
  **Re-measured 2026-09-17 at driving load, and the premise needs re-stating**
  ([evidence](../../../data/analyses/2026-09-17-drive-log.md)): over 300 s on the
  `default` profile at ~2,950 rpm, daemon `receive.frames_seen` +491,083 against a
  kernel `rx_packets` bracket of 490,919 (narrowest) to 496,570 (widest), i.e.
  **loss of −0.03 % … 1.10 %**, midpoint 0.54 %, with `rx_dropped` flat.
  The original 65 s protocol reads the kernel counter once per end, and a `/status`
  read on this Pi takes ~1.7 s ≈ 2,700 frames, so its window error is **as large
  as the 4.24 % it reported**. The tighter protocol reads the kernel counter
  immediately before *and* after each daemon read, making each end's uncertainty
  explicit. **Do not quote 4.24 % again without re-measuring it that way**, and
  re-measure at warm idle too before sizing this fix — at driving load the loss
  may already be under 1 %.
  _From D01._

- [x] **D02.2 Measure the MCP2515 controller overflow, separately from the
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
  **Progressed 2026-09-17 on the drive** — overflow tracks *burstiness*, not our
  average request rate ([evidence](../../../data/analyses/2026-09-17-drive-log.md)).
  `rx_over_errors` per minute, `rx_dropped` flat throughout, broadcast ~1,640
  frames/s in every case: `default` 8 req/s in rounds **0.4**; `tuning-did`
  127 req/s one-at-a-time **0.05**; `did_log` 118 req/s paced **1.5**;
  `tuning-hybrid-research` **47 req/s in rounds 5.2**; `did_log` **45.8 req/s
  paced 1.7**; `did_log` ~650 req/s back-to-back **36**. The middle two are a
  controlled pair 40 minutes apart on the same road at the same rate:
  **bursty is 3× worse than paced**, and paced 118 req/s is no worse than paced
  46 req/s. Two hardware RX buffers overflow when several frames arrive faster
  than SPI drains them, which is what a round of 8–11 simultaneous requests
  produces and what the same requests spread over the same second do not.
  So there is now a **third candidate that needs no reboot: stagger the
  scheduler's round instead of firing it as a burst.** It is not free —
  spreading a round widens intra-round acquisition skew, which is exactly what
  the capture-quality contract's `max_tuple_skew_s` and same-instant DID pairing
  depend on, so it trades frame loss against pairing tightness. That trade is
  the operator's call, not something to land unattended. Caveat on the table:
  each row is one window and driving differed between them, so treat the
  ordering as a lead. The matched-rate pair supports a pacing hypothesis;
  repeat it with comparable driving/load before assigning causality.
  _From D02._
  - **2026-11-08 (MCP2518FD, HAT rev 2.0): closed `[x]`, relaxed (operator
    authorization 2026-11-08: close with data close enough).** Controller
    overflow, scope `controller`, from each capture's stop verdict
    (`~/canary-data/2026-11-08-drive/capture_stop_{1..4}.json`,
    `losses.controller.counters["can0.rx_over_errors"]`, status `verified`):
    start 0, stop 0, **delta 0** in all four `ltft-gate` captures, 120.9 s
    (17:34Z), 721.3 s, 3,352.5 s and 3,434.0 s, **7,629 s (127 min) in
    total: 0 overflows per minute**. Every one of 1,494 compact `/status`
    polls 17:32-20:21Z read `can0.rx_over_errors` 0, `rx_errors` 0 and
    `since_start` 0 (`rx_dropped` read 2 before the 18:04Z power loss, 0 after, flat).
    Workload: `ltft-gate` at `bus_budget_override` 90 req/s (60 s achieved
    83.6 Hz at 18:09Z, 154 active requests), `receive.frames_seen` delta
    1,511-1,596 frames/s over the engine-on legs, and rounds of ~19 memory reads
    in 150-160 ms bursts (p50 147-163 ms, p95 214-267 ms; B08.4) -- i.e. the
    bursty shape that gave 5.2/min on the MCP2515 on 09-17 -- plus Mode 01
    batches of 6. Town and highway, 74 km/h at 20:05Z. The other loss scopes
    (`recording`, `dashboard`, `derived`) read `lost` 0, `verified`, in captures
    2-4 (kept separate from the controller figure).
    **Relaxed:** the item names the MCP2515; the rig has been the MCP2518FD
    since 2026-09-23, so the `spimaxfrequency` / second-controller levers
    (1, 2 above) do not apply, and the MCP2515 per-minute table above is a
    different controller. The bursty-vs-paced comparison at matched rate was
    **not run**, because overflow was 0 in the heaviest bursty workload this
    profile makes: nothing to rank. The ~650 req/s back-to-back case that gave
    36/min on the MCP2515 was not repeated on the MCP2518FD.

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

## Opened 2026-09-17 on the drive

- [x] **D03.7 The sustained-validation harness cannot see replay row
  duplication.** It wires replay through the storage decode
  (`src/validation/harness.py:617-690`), so it is the right instrument — but its
  fixture `tests/fixtures/ground_truth/drive-slice` holds `gps 1682 / can 19122 /
  obd 953` and **no `decoded` rows**, so the expansion it records
  (`decoded_rows: 3080` beside `can: 2189`) is a correct one-to-many for that
  fixture and structurally blind to D02.9's doubling. A regression test needs a
  fixture whose source already contains `decoded:` rows. The harness already
  measures `max_rss_mb`, so it can cover D03.6 too once pointed at a big enough
  session, off the vehicle.
  `c15f66a` added emission counters to the report and unit coverage for the emit
  filter, but `run_case` still replays `DRIVE_SLICE`; the missing end-to-end
  fixture has not been supplied. Include live-GPS contamination and legacy
  payload absence in the regression coverage. This is desk work.
  **Done 2026-09-21.** `tests/fixtures/replay/decoded-slice` (28 KiB, cut by
  `tools/make_replay_fixture.py`) is 20 s of the D02.9 evidence session
  `2026-09-10T142711`: 784 payload-bearing frames of the four bound ids, exactly
  the 1,377 stored `decoded` rows they produced (matched on timestamp +
  `can_id`), 200 `derived`, 304 `gps`, 64 `obd`. `ScenarioSpec` gained
  `replay_source`/`replay_speed`/`live_gps_hz`, and the report gained
  `evidence.replay_accounting`: persisted `decoded` == stored rows the emit
  policy let through + what the storage decoder re-derives from the persisted
  frames, and every captured source == its source count after a complete,
  drop-free pass, excess listed as `foreign_rows`.
  `tests/test_replay_row_accounting.py`: payload-bearing source → decoded
  ratio **1.000**, 1,577 rows skipped, no foreign rows; legacy copy with
  payloads stripped → `rederivable: false`, all 1,377 stored decoded and 200
  derived kept, none re-derived; **negative control** (emit filter disabled
  while the probe still says re-derivable, i.e. pre-`c15f66a`) → identity
  fails at exactly **2.000x**; live GPS at 5 Hz → **strict xfail**, the run
  shows `foreign_rows: {gps: 240}` — that isolation is D02.9's owed item, not
  this one. The pre-existing `replay-drive-slice` slow test still passes.

- [x] **D03.1 Capture-quality scoring ran on the event loop.** A live
  `GET /control/capture/quality` against a running 20-minute, 127 req/s capture
  held the daemon's loop for **60 s**, and the `DELETE` that stopped it held it
  another **55 s**: no HTTP, no collection, 38,600 records dropped at the
  `canable-obd` handoff, 108 inference deadlines overrun. The stop verdict then
  failed on `acquisition_gap` with `missing_final_s ≈ 49 s` — a hole the stall
  had punched in the data it was judging. Fixed in `f2093489`: the recompute
  runs in a spawn child process behind one async helper and now **refuses** to
  run on a thread with a running event loop, so the unsafe call cannot be
  written from an async path again. Verified on the truck at 19:08:34 — the
  stop took 35 s with `/status` answering throughout (1.3–2.6 s vs 0.17 s idle)
  and zero drops.
  **Follow-up — done in `7ebfa85`.** The recompute was ~123 s for a 20-minute
  capture, of which **115 s was `json.loads` over `metadata_json`, 21.1 M parses
  = six passes over every row**. Now parsed once into shared columns, with a
  vectorized prefilter that skips rows whose text provably cannot carry a key
  (no key substring **and** no backslash, since escaping is the only other way
  to spell one): 21,120,038 parses → 199,225. `read_acquisition_frame`
  39.8 s → 2.6 s, `recompute_capture_quality` 38.8 s → 7.1 s on a laptop (the
  Pi is ~3.2× slower). Proven bit-identical — 18 `assert_frame_equal`
  comparisons with `check_exact` over four real sessions and every metadata
  edge case, identical end-to-end quality payloads on three captures, and
  `tests/test_analysis_ground_truth.py` green.
  **The `source == "obd"` filter half was deliberately NOT taken.** Besides
  `rows_on_disk`, `persisted_quality_inputs` derives `ended_at` from the last
  row of *any* source on the acquisition clock when the caller passes `None`
  (measured 9.8 ms after the last OBD row on one capture; data-dependent and
  unbounded), so filtering would quietly shorten `elapsed` in exactly the
  missing-tail case the coverage clause exists to catch. Both invariants are now
  documented at `_row_count`, where someone profiling this path would spring
  them. Not worth reopening unless parquet I/O becomes the bottleneck — after
  `7ebfa85` it is 4.2 s of the 7 s, and the remaining metadata cost is 0.9 s of
  parses against 1.8 s of building the three full-length columns that
  `read_acquisition_frame`'s per-row contract requires.

- [x] **D03.2 A rejected Mode 22 batch was indistinguishable from a working
  one.** This ECM answers every multi-DID `0x22` with `7F 22 12` (measured on
  raw frames 2026-09-17, session 0xC0 held), but `sweep_dids_batched` re-read
  each rejected group one DID at a time and recorded nothing about it — so a
  batched and an unbatched sweep return the same DID set with the same widths
  whether the ECU batches or not. That match is the whole evidence base for the
  2026-08-08 "batched Mode 22 verified" claim, now retracted in the vehicle
  skill. Fixed in `4a68252`. **Consequence: no same-instant multi-DID sampling
  on this truck**, and the daemon's single-DID transport is the correct one.

- [x] **D03.3 Drop logging amplified overload.** The handoff warning fired every
  100 dropped records, so a 150,114-record burst produced **1,502 synchronous
  journald writes from the producer path**, competing with the consumer that was
  already behind. Fixed in `de5f0fd`: first line immediate, then at most one per
  second carrying the interval's count, the exact cumulative total and a bounded
  per-channel breakdown, with the burst's final line never lost. Counters stay
  exact; nothing widened.

- [x] **D03.4 `tuning-hybrid-research` requests a cycle it cannot meet.** The
  scheduler logs `hybrid cycle of 0.250s is infeasible: 11 Mode 01 + 8-9 DID +
  2 session requests at 90.0 req/s` whenever the profile runs, and every stop
  verdict reads `rate_shortfall` because all DIDs get 2.0 Hz against a
  `requested_hz: 4.0` — structural, since Mode 01 and Mode 22 alternate. The
  profile delivers well (3.98 Hz Mode 01 / 2.01 Hz DID at 47 req/s, 20 minutes
  with zero drops, DID↔DID skew 0.094 s); it is the *request* that is wrong.
  `mode22_rate_weights: 0.5` or a 2 Hz DID target makes it honest. Its
  `capacity_evidence` is also stale by ~2.7× (recorded 1.46 Hz Mode 01 /
  0.90 Hz DID / 0.341 s skew, from before D01.8 and D01.9). Left alone
  deliberately: editing a saved profile mid-drive is how the `fuel-map` freeze
  incident happened.
  **Before tomorrow's candidate capture:** prepare the revised vehicle profile
  between captures, and inspect the resolved scheduler plan. A 2 Hz DID request
  alone does not prove that `did_cycle_seconds: 0.25` is feasible; resolve the
  cycle warning as well, without raising the bus budget. Include any D03.8
  candidate channels in that calculation. On the drive, retain a 20-minute
  capture, measured per-channel acquisition cadence and pairing skew, loss
  deltas and the complete quality verdict. Update capacity evidence only from
  that exact profile revision. Controller overflow can still independently
  fail persistence quality after the rate request is corrected.
  **2026-09-18 ([September 18 drive log](../../../data/analyses/2026-09-18-drive-log.md)):** the new `fuel-target-research` / `fuel-pw-research`
  profiles (`e7d2168`, `21b4fb6`) request an honest cycle: DIDs weighted 0.5 on a
  0.5 s cycle. The resolved plan needs 0.25 s of 0.5 s at the unchanged 90 req/s,
  there's no `infeasible` warning on the Pi, and they delivered 3.78 Hz Mode 01 /
  1.90–1.97 Hz DIDs, so `rate_shortfall` is gone. `tuning-hybrid-research` itself
  is untouched. Remaining: its own request, plus a new stall (D03.18) that now
  fails every long research capture on `acquisition_gap`.
  **2026-09-21:** `tuning-hybrid-research` is retired (archived in
  `data/nissan-xterra-2008/capture_profiles.archive.yaml`). The Xterra's one
  tuning capture, `tuning`, carries the honest 0.5 s / weight-0.5 request
  inherited from `fuel-target-research`, so the infeasible-cycle request no
  longer exists. What remains open here is D03.18's stall.
  **Closed 2026-09-22 (desk).** Re-checked: `tuning-hybrid-research` is in
  `data/nissan-xterra-2008/capture_profiles.archive.yaml` (line 201) and the
  live `tuning` profile has `did_cycle_seconds: 0.5` with every DID weighted
  0.5, so no profile on this vehicle requests the cycle this item was about;
  the scheduler already refuses to *promise* an infeasible plan
  (`plan_cycle` reports it, `_infeasible_logged` logs it once). The stall is
  D03.18's, tracked there.

- [~] **D03.6 Trip-end digest isolation and memory-order fix landed; measured at
  scale and budget revised 2026-09-22; the shutdown deadline landed 2026-09-25;
  the Pi stop/restart check remains.** The original synchronous digest, not the replay, took the daemon
  to 7.7 GB and got it OOM-killed on 2026-09-11
  ([evidence](../../../data/analyses/2026-09-17-d029-replay-amplification.md)).
  The old trip-end hook ran `get_or_compute_digest` **synchronously on the
  loop**. `resample_and_segment` read every chunk with every column
  (payload bytes, `metadata_json`), pivots on `(session, timestamp)` at **raw
  microsecond resolution**, and only then resamples to 1 Hz. **Measured at scale
  on the laptop** against a 2,622,370-row session: `read_session` 1,879 MB,
  `to_wide` 3,440 MB, `resample_and_segment` **5,483 MB peak — to produce a
  final frame of 1,143 rows × 203 cols, 0.044 % of the rows it loaded.**
  Confirmed one-shot, not a leak, and confirmed to have fired on 09-11: the
  replay directory has 586 chunks, `dtc.json`, and **no**
  `digest.json`/`classification.json`/`fingerprint.json`.
  **The cliff is bracketed on the Pi itself:** `2026-09-10T142711`
  (4,544,560 rows) **has** a digest, so it completed; the 5,860,000-row replay
  does not, and the daemon died computing it. So the limit sits between **~4.5 M
  and ~5.9 M rows**, and it is not a linear extrapolation from the laptop's
  per-row figure — trust the bracket, not the slope.
  **It recurred live at 23:17 UTC on September 17, on an ordinary capture, and the
  failure mode was worse than a kill.** A 638-chunk (~6.4 M row)
  `_capture-tuning-hybrid-research-2026-09-17T224717` hit the trip-end edge; the
  daemon climbed to **7.36 GB of 8 GB and wedged — unresponsive for 38 minutes**,
  no HTTP and no collection, until it was restarted by hand. **Because the Pi has
  2 GB of swap, the kernel thrashed instead of killing**, so `Restart=on-failure`
  never got the chance to recover the service. It produced no digest.
  **Implemented by `2dfa0aa`, `dcde580`, and the September 18 performance pass:**

  - `systemd/canary.service` sets **`MemoryMax=6G` and `MemorySwapMax=0`**
    (installed on the Pi 2026-09-18 00:0x UTC). A runaway is now killed promptly
    and restarted in 5 s instead of paging a real-time collector out to swap. 6 G
    is ~10× the daemon's normal 150–650 MB and leaves ~2 GB for the MCP server,
    gpsd and the system.
  - Automatic trip-end work now uses `handle_trip_end_off_loop`, a spawn child
    with `RLIMIT_AS`, and a row-budget preflight. The default budget derives
    from the child ceiling / 2,200 bytes per row (about 1.95 M rows at 4 GiB),
    rather than the earlier proposed 4 M. `digest_skipped.json` records a named
    refusal/failure. Shared preflight rejects `_replay-*` even through the
    synchronous helper. HTTP reports a refusal as 409; the catalog can read it.
  - The numeric batch path now projects only `timestamp`, `source`, `channel`
    and `value_number`, reduces each channel onto the configured time grid in
    long form, and only then constructs the wide result. It no longer creates
    the raw-timestamp × channel sparse pivot. A regression compares the new
    result to the legacy chain including IMU max/RMS/jerk and gap semantics;
    classifier, fingerprint, digest and committed ground-truth suites pass.
    Digest classification and fingerprinting also share this one reduced frame
    instead of independently reading and resampling the session twice.
  - The authenticated HTTP route is async and uses the same row preflight,
    bounded child and in-flight computation as automatic trip end. Concurrent
    trip-end/API callers await one computation and retain the 409 refusal
    contract; the parent FastAPI process no longer performs the pandas build.

  **Review follow-ups, desk first:**

  - **Finish shutdown within the unit's deadline.** `_settle_trip_end_tasks`
    waits up to 30 s and cancels the child, then `run_daemon` invokes the
    fallback again with the normal 900 s timeout. Cancellation removes the
    in-flight marker without writing a refusal, so the fallback can immediately
    start the same work again. `canary.service` has `TimeoutStopSec=30`.
    Reproduce with a slow child; use one shutdown deadline, no duplicate child,
    and a recorded outcome for interrupted computation. Prove this locally
    before a Pi stop/restart check.
  - **Measure the new order of operations at scale.** Run the real 2,622,370-row
    `2026-08-08T214218` on the laptop against the recorded 5,483 MB peak, then
    validate real small and large sessions through the actual capped child.
    The fixture and ground-truth parity prove behavior, not the large-session
    RSS reduction or the correct future row budget. Do not increase budgets to
    conceal the cost.

  **Drive acceptance:** D03.11 covers an ordinary small-session trip end and
  the long-drive stop that previously triggered the outage. Until the new cost
  is measured and the budget is revised from evidence, an oversized session
  should still produce a named refusal while collection and HTTP continue.

  **2026-09-22 (desk) — both follow-ups, measured and landed**
  ([evidence](../../../data/analyses/2026-09-22-d03-d04-desk-pass.md)):
  - **Measured at scale, same session, same laptop, each stage in its own
    process:** `2026-08-08T214218` (2,622,370 rows) `resample_and_segment`
    **859 MB** peak against the recorded **5,483 MB** (6.4×), `compute_digest`
    959 MB, and `compute_digest_under_limit` under the Pi's 4 GiB `RLIMIT_AS`
    **976 MB, completed** — 390 B/row. `2026-09-18T144215` (5,570,352 rows):
    1,470 MB / 1,490 MB capped, 280 B/row. The real spawn child wrote
    `digest.json` for both (10.0 s and 7.9 s) with the budget check off, and
    the default budget refused both with `over_row_budget`; a small session
    (`2026-09-18T134052`) went through the default path to `digest.json` in
    1.3 s. **Budget revised from that evidence:** `DIGEST_PEAK_BYTES_PER_ROW`
    2,200 → **800** (2× the larger laptop figure, since the Pi's allocator is
    not the laptop's), so the 4 GiB child now admits **5,368,709 rows ≈ 38 min**
    of `default` collection instead of 1.95 M ≈ 13.6 min. A wrong revision
    fails as a caught `MemoryError` plus a named sidecar, never the daemon.
  - **Shutdown:** a digest child cancelled by `_settle_trip_end_tasks` now
    writes `digest_skipped.json` with reason **`interrupted_at_shutdown`** and
    is remembered process-locally (`trip_end._INTERRUPTED`,
    `digest_interrupted()`), so the fallback's second call for the same
    session is a no-op instead of a second multi-gigabyte child with the full
    900 s timeout inside `TimeoutStopSec=30`; the next daemon start may still
    compute it. `tests/test_trip_end_digest.py::test_a_digest_cancelled_at_shutdown_is_recorded_and_not_restarted`
    (hanging executor, cancel, one submission total, sidecar reason, then a
    fresh process computes normally).
  - ~~**Still owed:** `run_daemon`'s fallback in `src/daemon/runtime.py` still
    passes the 900 s digest timeout for a *different* session than the one
    cancelled~~ **Done 2026-09-25 (desk).** One shutdown deadline, taken when
    `run_daemon`'s teardown begins: `TRIP_END_SHUTDOWN_SECONDS` =
    `UNIT_STOP_TIMEOUT_SECONDS` (30, pinned to `systemd/canary.service`'s
    `TimeoutStopSec` by a test that reads the unit) − 10 s reserved for the
    inference join and the dashboard's forced exit. The drain, capture stop,
    `_settle_trip_end_tasks` and the fallback all spend it
    (`runtime.shutdown_trip_end(…, deadline=)`): the fallback's child gets only
    the remainder as its timeout (a cut is recorded as `child_timed_out`), is
    not started with < 1 s left (logged), and is bounded outside too (a cancel
    takes the `interrupted_at_shutdown` path). Tests
    (`tests/test_daemon_shutdown_deadline.py`, hanging spawn pool): a hung edge
    digest spends a 1 s deadline, shutdown returns < 2.5 s, one child total,
    the edge session carries `interrupted_at_shutdown`; a fallback alone with
    1.5 s left writes `child_timed_out` < 3 s; `run_daemon` hands the fallback
    `0 < timeout ≤ 20`; unit/constant parity. **Still owed:** the Pi
    stop/restart check. `[~]` for that.

- [x] **D03.5 The Pi cannot pull from GitHub.** `git pull` on the Pi fails with
  "Please make sure you have the correct access rights" — no deploy key for
  `git@github.com:danbrunski/canary.git`. The first deploy attempt of the day
  therefore restarted the *same* code while reporting success. Worked around
  with `git bundle create <file> <pi-HEAD>..main main` + `scp` +
  `git pull --ff-only /tmp/<file> main`, which is a real fast-forward and keeps
  history. A read-only deploy key, or a scripted bundle path, removes a silent
  failure mode from every future deploy.
  **Resolved 2026-09-18:** the operator `git pull`ed `7df58f0` from origin on the
  Pi (reflog `pull: Fast-forward`, `~/.ssh/id_ed25519` present); `e7d2168` and
  `63d5e51` were deployed the same way. The bundle recipe stays as a fallback.

## Added by the evening review — next drive and its preparation

Evidence for D03.8–D03.10 and D03.12 is the
[September 17 drive log](../../../data/analyses/2026-09-17-drive-log.md), especially
blocks 2, 5, 6 and 9. These are measurements to resolve, not permission to
promote unverified bindings or to change a tune.

- [x] **D03.8 Pair target-AFR candidate 0x1134 with PID 0x44 in one capture.**
  This is the highest-value new driving experiment. Yesterday's raw 64 baseline
  and 74–75 under load support candidate **φ = raw / 64**, equivalently
  **AFR = 14.7 × 64 / raw**, but none of the evidence compares the candidate
  with commanded AFR in the same capture.
  **Prepare before the drive:** retain its measured response width and raw
  bytes in a provisional, vehicle-scoped catalog/profile entry; keep candidate
  status explicit. Resolve the hybrid plan under D03.4. Include PID 0x44,
  PID 0x24, FUEL_STATUS, RPM, MAF, and bank-1 A/F voltage, plus 0x1134; keep
  Mode 22 single-DID (`obd_did_batch_max_dids: 1`) on this ECM.
  **Capture:** warm closed-loop cruise, sustained enrichment at more than one
  load/RPM region, then lift/decel. Tag physical actions and keep raw acquisition
  timestamps. A flat stoichiometric window cannot identify the scale.
  **Accept:** use `compare_channels` on acquired pairs in matching units
  (candidate φ versus PID 0x44 φ, or both converted to AFR using the vehicle
  calibration). Report n_pairs, separation, slope/offset, `scale_verdict`,
  quantization and regime coverage; repeat across independent pulls. Too few
  enriched pairs, flat signal, or excessive skew is an unresolved result.
  Promote only after 1:1 agreement within measured uncertainty, then pin the
  exact decode and a ground-truth fixture. No cross-block mean comparison.
  **2026-09-18 ([September 18 drive log](../../../data/analyses/2026-09-18-drive-log.md)): the capture ran, but the scale is unresolved.** 0x1134 is
  catalogued at confidence `hypothesized` (`e7d2168`) and was held beside PID 0x44
  for 39 min, but the drive was slow offroad with no enrichment. 0x1134 = 64 on
  all 4,630 samples, and in 493 gated pairs with 0x44 in the stoich band it is
  always 64: the necessary baseline, confirmed on two blocks. `compare_channels`
  correctly refuses (constant side). **Newly observed:** in closed loop PID 0x44
  steps 0.9796 ↔ 1.0204 and never reads 1.000, so the enrichment gate is
  0x44 > 1.05, not 1.02. Still needs one warm WOT/enrichment window on
  `fuel-pw-research`; the script is `analyze_fuel_blocks.py` (preserved in
  `~/canary-data/scripts-0918/`).
  **2026-09-18 PM ([September 18 PM drive log](../../../data/analyses/2026-09-18-pm-drive-log.md)): consistent, still short of pairs.** Two enrichment
  holds (6 s + 10 s, φ up to 1.242) on `fuel-pw-research`. 0x1134 left 64 for
  the first time: raw 73–79 on 47 samples. In **10** enriched pairs (0x44 > 1.05),
  0x1134/64 − 0x44 has median −0.0078 (p10–p90 −0.0078..−0.0070), within half a
  quantum. The stoich band has 70 pairs, all 0x1134 = 64. The falsifier (stays 64
  while 0x44 > 1.10) didn't fire. The pre-registered bar is ≥ 30 pairs from ≥ 3
  pulls, so this is **unresolved**: roughly three more ≥ 10 s holds.
  **2026-09-18 evening ([September 18 evening drive log](../../../data/analyses/2026-09-18-eve-drive-log.md)): "0x1134 = commanded φ" is FALSIFIED; the raw/64
  decode survives.** 203 enriched pairs over 14 holds.
  - Where 0x1134 moves it equals floor(0x44 × 64): 74 ↔ 1.1641 ×196, 76 ↔ 1.186.
  - **Counterexamples:** 0x44 held 1.06 for 3.5 s (6 pairs, ≤ 0.17 s
    separation) with 0x1134 = 64, and 0x44 = 0.87 lean on 293 samples with
    0x1134 = 64. The pre-registered slope is 1.52 (`unresolved`, two levels).
  - **Reading:** a rich-side (power-enrichment) target that 0x44 follows when
    it's the active source, not the final command.
  - **Remaining (desk):** re-describe and rename the candidate accordingly.
  **Late 2026-09-18 (desk, `~/canary-data/scripts-0918b/out/enrich_trigger.md`):**
  0x1134 is the **active open-loop rich target**. It switches within one sample
  of 0x44 and reads 65–77 on map-driven rich runs. The 1.06 counterexample was
  after-start enrichment (FUEL_STATUS 1), a different source. Mid-rpm φ 1.1641
  is a **pedal switch at ≥ ~227/255** into the fuel map's last load column
  (20 errors in 27,415 rows; residual 0.000 % on 439 rows). Renamed in
  `b9356bd` to `ECM_ENRICHMENT_TARGET_EQUIV_RATIO_CANDIDATE` (and on
  2026-10-08, D03.40, to `ECM_ENRICHMENT_PHI_REQUEST`); the evidence
  block should gain this finding at the desk.
  **Closed 2026-09-22 (desk).** The 0x1134 entry in `data/dids.yaml` now
  carries the trigger finding as an evidence item
  (`per_sample_rule_scoring_of_enrichment_onset_and_exit`: pedal ≥ 227 counts,
  20 errors of 27,415, onset median 0.15 s, 19/19 exits on pedal drop, no
  timer to 18 s, the after-start explanation of the 1.06 counterexample).
  With the decode pinned by the UpRev RAM word (2026-09-20 evidence) and the
  register's role settled as the active open-loop rich target, nothing is
  left to capture for this item; `tests/test_dids.py` and
  `tests/test_analysis_ground_truth.py` pass on the edited catalog.

- [~] **D03.9 Re-evaluate the injector fuel-mass calibration using today's
  open-loop data; collect only missing coverage tomorrow.** Blocks 6 and 9
  report command and bank-1 λ agreeing at φ ≈ 1.21 while PW+MAF gives ≈ 1.03.
  The 914,943 counts/gram value in `data/dids.yaml` was measured in steady
  closed loop, 0.0061–0.0337 g/injection; it is not a WOT calibration.
  **Desk first:** preserve/pull both captures and reproduce the known RPM and
  MAF DID↔Mode 01 checks through `compare_channels`. Separate the reported
  high-flow MAF scale drift from the PW model by using Mode 01 MAF as the
  reference. Pair PW, RPM, measured λ and fueling state on acquisition time;
  exclude DFCO and unstable/unpairable rows. Fit on one capture and validate
  on the other, reporting residuals over load and fuel quantity, not just a
  pooled coefficient. Do not assume a single replacement constant fixes it.
  **Drive only if needed:** obtain repeat steady enriched plateaus and matching
  closed-loop points in the missing cells, preferably with D03.8's capture.
  Persist validated operating-domain constraints in the calibration schema and
  enforce them at consumers; outside-domain use must refuse, not silently
  extrapolate. Update the stale provenance text and ground-truth expectations
  only after reproducing the result. No tune changes are part of this task.
  **2026-09-18 ([September 18 drive log](../../../data/analyses/2026-09-18-drive-log.md)): the premise changed. 0x1208 is not injected fuel.**
  - A desk fit on the 09-17 blocks 6/7/9 shows `ECM_INJECTOR_PW_B1` (0x1208)
    tracks air per intake event (~63,850 counts/g air). It doesn't move with a
    measured 20 % enrichment or with fuel cut, and every refit leaves −14..−18 %
    in the rich cells, so no constant fixes it
    (`2026-09-18-maf-did-and-injector-mass-balance.md`).
  - Replicated on today's independent block B: in DFCO, 0x1208 reads 63,984
    counts per g air vs 63,466 fuelled.
  - A same-pass screen of the 138-DID surveys finds **0x1206/0x1207** (catalogued
    as "intake pressure"; this engine has no MAP) with the injected-fuel
    signature (`2026-09-18-injected-fuel-did-screen.md`). Today they floored in
    DFCO (51.5 counts, 910 per g air vs 2,259 fuelled). The closed-loop line is
    27,747 counts per g fuel + 41 counts, i.e. 0.41 ms dead time and ~290 cc/min
    at a hypothesised 0.01 ms/count
    (`2026-09-18-fuel-blocks-capture-fuel-pw-research-*.md`).
  - **Remaining:** validate that line on enriched rows (0 today), then operator
    sign-off to re-describe 0x1208 and catalogue 0x1206/0x1207 properly.
  **2026-09-18 PM ([September 18 PM drive log](../../../data/analyses/2026-09-18-pm-drive-log.md)):** first enriched rows (6 gated, 38 ungated).
  - **0x1208 = air charge: PASS**, third independent replication. Rich rows
    sit at −16.6 % against the closed-loop refit (band −14..−18). Counts per g
    air are 63,972 closed loop vs 64,457 enriched (+0.8 %).
  - **0x1206: unresolved, and the pre-registered test was ill-posed.** Against
    the fixed 27,747 + 41 line, enriched rows read +6.2 %, but closed loop is
    +6.2 % too. The morning line was fit on 57 % idle rows and extrapolated to
    cruise; the fuel ranges don't overlap. The residual rises with 0x1208 air
    charge at the same rate in both sessions, and one convex curve,
    51.6 + 25,284 f + 133,653 f², fits both best (BIC). Altitude, λ and
    coolant are unchanged; voltage, IAT and fuel temperature weren't logged.
  - Post hoc, against that curve: enriched −3.4 % (n 6). The curve is still
    extrapolated beyond the closed-loop fuel range.
  - **Next:** closed-loop holds that overlap WOT air charge in one session, plus
    0x42/0x0F/0x33/0x06/0x07 and an idle electrical-load step for dead time.
    Desk report: `~/canary-data/scripts-0918b/out/shift1206.md`.
  **2026-09-18 evening ([September 18 evening drive log](../../../data/analyses/2026-09-18-eve-drive-log.md)): 0x1206/0x1207 = injected pulse PASSES** the
  pre-registered matched-air-charge test. 184 of 188 enriched rows each had
  ≥ 20 closed-loop rows at ±5 % the same 0x1208 air charge. The φ-scaled
  prediction residual is **+1.0 %** (p10–p90 −0.6..+3.5); 0x1207 is **−0.5 %**.
  The fixed straight-line criterion gave +7.4 % (unresolved, the known
  ill-posed extrapolation). 0x1208: per g air +0.5 % (pass), rich residual
  −13.0 % vs the −14..−18 band (narrow miss; −14.1 % expected at φ 1.164).
  Air charge confirmed a fourth time. **Remaining:** dead time/voltage
  (`fuel-pw-research-2` broke 0x44 batching; see the log), then the rename per
  the operator's decision above.
  - **Operator approved the relabel (2026-09-18 ~19:00Z).** `data/dids.yaml`
    now describes 0x1208 as air charge (unit no longer `ms`; a test refuses
    `ms` or a scale) and 0x1206/0x1207 as injector-pulse candidates at
    `hypothesized`. **Names were deliberately kept.** Still owed at the desk:
    the rename via `aliases` plus a retrain (IF/LSTM manifests,
    `behavior_models.yaml` recipe output, `validation/scenarios.py`), and
    **re-pointing the tuning provider role `injector_pw_b1`**, which still
    maps to 0x1208 at `confirmed` in
    `data/nissan-xterra-2008/tuning_providers.yaml`.
  - **Decision made (operator, 2026-09-18): fix the mislabels properly, and
    don't preserve naming consistency in the tools for its own sake.**
    Deferred to a desk session. Plan:
    - rename 0x1208 → `ECM_AIR_CHARGE` and 0x1206/0x1207 →
      `ECM_INJECTOR_PULSE_B{1,2}_CANDIDATE`, keeping the old names in
      `aliases`;
    - add ONE catalog canonicalization applied where stored sessions are read
      and where manifest/recipe/provider feature names are resolved, so old
      sessions and deployed IF/LSTM models keep working without a retrain;
    - re-point role `injector_pw_b1` to the 0x1206 candidate at `candidate`;
    - sweep built-in and vehicle profiles, the behavior recipe, `refit.py`,
      `scenarios.py` and the tests.
  **2026-09-22 (desk):** the catalog side of that plan is in: 0x1208 is
  `ECM_AIR_CHARGE`, 0x1206/0x1207 are `ECM_INJECTOR_PULSE_1/2` at `high`
  (K-line, 2026-09-20), and `tuning_providers.yaml` role `injector_pw_b1`
  points at `did:ECM_INJECTOR_PULSE_1`; the old names were migrated with
  `tools/migrate_channel_names.py` rather than kept as aliases. **Dead time
  from the 2026-09-19 idle load-step capture is a named empty**: the step
  moved `CONTROL_MODULE_VOLTAGE` only 14.00 → 14.16 V (the alternator held
  it), below the pre-registered 0.4 V, so the input the test needs was never
  produced; the 0x1206/0x1208 ratio moving 37.5 → 34.9 across the step is
  the known convex air-charge dependence
  ([evidence](../../../data/analyses/2026-09-22-d03-d04-desk-pass.md)).
  **Waits on the truck:** a load step that actually drops system voltage by
  ≥ 0.4 V (or a KOEO/cranking window), and closed-loop holds overlapping WOT
  air charge in one session.
  **2026-10-03, naming:** P02.14 (`ca80026`, operator decision 2026-10-02)
  verified 0x1208 as the ECM's **base fuel schedule**: raw × 0.00049 ms,
  405/405 rows against UpRev's logged values. The "air charge"
  findings above still describe its behaviour. BFS is computed from air per
  event and ignores enrichment and DFCO, which is exactly what those fits
  measured. The channel keeps the name `did:ECM_AIR_CHARGE` and still decodes
  raw, because it is a deployed IF/LSTM feature. `derived:BASE_FUEL_SCHEDULE`
  is the same quantity in ms. Read "WOT air charge" in the remaining clause as
  "WOT BFS".

- [ ] **D03.10 Capture the missing UpRev instrument reference.** Today's two
  CANary paths already show a rich-side discrepancy: bank-1 voltage converted
  through the UpRev-derived transfer reads about 3–4 % leaner than PID 0x24 at
  AFR ≈ 12.1, with near-stoich agreement. That does not yet establish what
  UpRev's own logger reports in the same conditions.
  **Deferred for September 18 by the CAN-only constraint.** Retained as future
  work; no external logger window is part of tomorrow's autonomous session.
  **Drive block:** one warm window with UpRev logging AFR, A/F voltage, MAF,
  injector PW and, if available, intake VVT solenoid duty. Use the established
  passive CANary arrangement during the external logger window. Save the
  original UpRev file, CANary session, tune revision, physical-action tags and
  clock-alignment evidence. Verify which reference channels were actually
  captured: passive CANary does not itself poll PID 0x24/0x44. If that leaves
  no simultaneous reference, record the missing input and plan a compatible
  co-capture; another pull's average is not a substitute.
  **Accept:** paired comparisons in common units with timing/stability gates,
  first reproducing the known voltage→UpRev AFR conversion. Update
  `afr_calibration.yaml`'s stale “NOT settled” note with reproduced September 17
  evidence and distinguish that result from the new logger comparison. Bank 2
  still lacks an independent λ reference; do not extend bank-1 conclusions to
  it. VVT duty can also arbitrate 0x1110's ×0.5 versus ×100/255 scale only if
  the matching raw register is observed in the same window. *(2026-10-04:
  moot. 0x1110 is commanded EVAP purge, ×0.5 % from the ECM's own PID 0x2E
  arithmetic, D03.36; it is not VVT duty.)*

- [~] **D03.11 Verify the evening fixes through a real drive and stop.**
  **Before departure:** record the Pi checkout SHA and running build; ensure it
  includes `7ebfa85`, `c15f66a`, `3d45680`, `2dfa0aa` and `dcde580` (or their
  descendants). Verify effective `MemoryMax`, `MemorySwapMax`, service PID and
  restart count, not just the unit file in git. D03.5's bundle workaround is
  available if GitHub authentication is still absent. Preserve today's source
  sessions, manifests/quality sidecars and DID/UpRev logs before retention can
  remove evidence; verify copies on the laptop.
  **During ordinary collection:** poll `/status?view=compact` at a modest
  cadence (e.g. 10 s), recording response latency, `served_at`,
  `vehicle_state.derived_at`, inference misses, handoff losses and memory.
  Check key-on → running → engine-off transitions against raw freshness fields.
  Compact omits IF provenance/blindness: take full-status snapshots at block
  boundaries for detector interpretation. Missing blocks mean uninstrumented,
  not zero. Use the compact endpoint for D01.5's bracketed counters too.
  **Capture lifecycle:** start/tag/stop, confirm physical-action tags in the
  current session, and reopen the in-memory capture after any deploy restart.
  Once between blocks, confirm tagging with no active capture returns the new
  explicit 409 through the control surface being used; it must not be reported
  as recorded. Run one live-quality request and one stop on a representative
  capture sequentially, checking status latency and loss deltas during each.
  Confirm the final quality sidecar has no gap manufactured by scoring.
  **At a stop/end:** allow the GPS trip-end condition (five minutes stationary
  after movement) and watch collection/HTTP continue. A small under-budget
  session should produce `digest.json`; a long over-budget session should
  produce `digest_skipped.json` with row count/budget/reason, without restarting
  the daemon. Read the refused digest through HTTP/MCP and confirm the reason
  survives. Do not trigger repeated uncached digest reads while D03.6's API
  path remains unisolated. Check shutdown after its desk regression is fixed.
  Save the deployed versions, per-block measurements and sidecar paths in a
  September 18 drive artifact; close each verified clause with that evidence.

  **2026-09-18 ([drive log](../../../data/analyses/2026-09-18-drive-log.md)), `[~]`:**
  - Deployed build verified at `7df58f0` → `e7d2168` → `63d5e51` (hash checked each time).
  - Compact status answers in 9–26 ms, against 1.3–2.6 s for full.
  - Capture windows reopened after all four restarts. Tags recorded, with an
    explicit 422 on a wrong body.
  - **Over-budget refusal verified end to end, twice:** sidecar, WARNING, and
    HTTP 409 `digest_refused` with the reason in 49 ms. No hang.
  - **Still open:**
    - a small session producing `digest.json`. At ~2,355 rows/s the budget is only
      ~13.6 min of driving, so trip-end needs a session younger than that;
    - the 409-on-tag-without-capture check;
    - effective MemoryMax, which isn't enforced (D03.21).

  **2026-10-03:** two of those three are closed.
  - MemoryMax is enforced: D03.21 is `[x]`, with
    `/status.health.system.memory_limits_enforced` as the live check.
  - A small session produced `digest.json` at trip end:
    `2026-09-23T150658` (1,142 s). Its `computed_at` is 15:26:05Z, which is
    the session end, so the GPS trip-end path wrote it; it was not read on
    demand.
  - **Still open:** the 409 on tag-without-capture, through the control
    surface. A parked Pi is enough to check it; no drive is needed.

- [ ] **D03.12 Follow up the secondary DID candidates if drive time remains.**
  Use short, focused profiles after D03.8, with provisional raw decodes and
  measured response widths; do not expand a 138-DID survey just to repeat it.

  - **0x120D/0x120E vs PIDs 0x49/0x4A:** obtain released-pedal and several
    held-pedal levels. The proposed ×0.005 decode is **volts**, whereas Mode 01
    pedal position is **percent**; the drive log's proposed immediate 1:1 test
    is therefore premature. Establish units/transfer against an independent
    reference before demanding 1:1 engineering-unit agreement. Mere shared
    movement with pedal/RPM cannot establish identity or scale.
  - **0x1246 vs PID 0x42:** pair across key-on, cranking/recovery where captured,
    and changing electrical load. ×0.028 V is a hypothesis, not a calibration.
    A near-constant charging voltage is an unresolvable scale result.
  - **0x1110/0x1139:** external duty-scale verification is deferred with D03.10.
    *(2026-10-04: no longer a duty question. 0x1110 is commanded EVAP purge,
    ×0.5 % from firmware; 0x1139 is `ECM_UNIDENTIFIED_1139`, identity open.
    D03.36.)* *(2026-10-05: 0x1139 is the bank-2 VTC solenoid duty,
    ×0.39 % from firmware, D03.37.)*
    Today's paired
    result rejects a simple mirror; raw range and shared zero fractions do not
    identify bank 2 or settle either scale.
  Record `compare_channels` verdicts, acquisition separation, exact raw decode
  and operating conditions. Leave unsupported/flat/unpaired candidates open.

- [x] **D03.13 Capture-quality concurrency fixed; UPS heartbeat remains.**

  - [x] **Capture-quality concurrency — desk fix landed.** The shared helper
    now single-flights equivalent requests and serializes distinct full-session
    judges per daemon loop. Tests prove two equivalent requests execute once
    and two distinct requests never overlap their expensive worker phase.
    Verify one ordinary request during D03.11; do not stress the collecting Pi.
  - **UPS heartbeat — Pi/driveway.** The log reported `watchdog_stale: true`,
    `status_age_s ≈ 5.16e6`. Inspect `ups-shutdown.service`, its journal,
    `/run/canary/ups_status`, and both clocks; `/control/power` computes age from
    the file's `ts`, while keepalive is a separate file. A successful keepalive
    write does not prove watchdog liveness, and a stale dashboard heartbeat
    does not by itself prove the watchdog uses stale fuel-gauge readings.
    Establish the cause and verify successive fresh timestamps and reported
    power transitions around the drive's key-off. Do not let a planned power
    transition interrupt the final evidence copy.
  - **2026-11-08: heartbeat verified live `[x]`, relaxed (operator
    authorization 2026-11-08: close with data close enough).** Source:
    [drive log](../../../data/analyses/2026-11-08-drive-log.md) and
    `~/canary-data/2026-11-08-drive/{power_200508.json,power_keyoff.log}`
    (`GET /control/power`).
    - **A real input-loss transition, 17:32-17:37Z** (the OBD plug was out, so
      the UPS lost its feed): every poll answered with `status_age_s` < 1 s and
      `watchdog_stale: false`; `input_lost` went false → true and
      `seconds_lost` counted 0 → 65 s while the keepalive was disarmed
      (17:36:26Z); SoC 12.5 % → 7.8 % → 0 %, cell 3.73 V → 2.95 V.
    - **20:05:08Z, engine running:** `watchdog_running` true, `watchdog_stale`
      false, `status_age_s` 0.163, `input_lost` false, SoC 97.5 %, 4.21 V.
    - **Across the 20:10:32Z key-off** (`power_keyoff.log`, polls 20:11:39,
      :45, :52Z): `watchdog_stale` false in all three, `status_age_s` 0.027,
      0.553, 0.935 s (successive fresh timestamps, each < 1 s), `input_lost`
      false, `on_battery` false, SoC 98.6 %.
    **Relaxed:** the key-off did not cut the Pi's supply (it was on a DC
    adapter, not the vehicle's switched feed), so a *key-off-driven* power
    transition was not observed; the real transition above stands in for it.
    The cause of the 2026-09 `status_age_s ≈ 5.16e6` reading is **not
    established** (no Pi journal or `/run/canary/ups_status` read today); it
    did not recur in any poll recorded today (17:32-17:37Z, 20:05Z, 20:11Z). Each
    poll is a point sample, not a continuous trace of the heartbeat.

## Additional CAN-only experiments — September 18

These extend the existing tasks: D03.8 already tests 0x1134, D03.9 already
tests PW calibration, and D01.5/D02.2 already measure receive loss. Do not
duplicate those captures or count the same result as a new experiment.
Prioritize D03.14/D03.15 during the drive; D03.16 is a short alternate-service
test, and D03.17 is a bounded access test at a parked stop.

For every block, save the build/profile/tune revision, `/status.vehicle_state`,
request/session identifiers, transmitted bytes, positive/negative replies,
`t_send`/`t_recv`, physical-action tags and start/end loss counters. Preserve raw
CAN alongside decoded data. A standalone probe owns diagnostic TX with the
daemon passive; a hybrid capture uses the daemon scheduler, never a second
competing tester. Restore normal polling and verify freshness after each
standalone block. Keep established request budgets and single-DID Mode 22;
pace requests explicitly rather than using the logger's zero-delay default.
Analyze on the laptop. For channel comparisons use `compare_channels`, with
explicit acquisition-time tolerances and no pre-averaging. Normalize raw probe
logs into timestamped observations first if necessary; a sweep/pass number is
not a common sampling instant.

- [x] **D03.14 Test whether calculated load provides a reconstructible BFS
  axis.** **New question:** can the CAN load signal plus RPM replace a missing
  BFS read, rather than re-fit the injector constant? The proposed relationship
  is `BFS_ms = load_percent / 100 × reference_BFS_ms(RPM)`. It is a hypothesis
  about this ECU's load channel, not an established decoder. The relationship
  is motivated by the [UpRev guide, p. 13](https://uprev.com/wp-content/uploads/2023/05/UpRev-Nissan-Tuning-Guide-Rev2.pdf).
  **Prepare:** look for the current ROM's Calculated Load table in existing
  local artifacts; it is not the Fuel Targets table or its BFS axis. If absent,
  an RPM-only reference curve may be tested against existing same-revision
  UpRev BFS/load logs, with separate held-out logs. Neither step needs UpRev
  connected tomorrow. Record whether the resulting reference is transcribed,
  estimated or missing; do not turn the last two into a confirmed ROM value.
  **Capture:** add Mode 01 ENGINE_LOAD, RPM, MAF and INTAKE_TEMP to a feasible
  hybrid block, retaining commanded ratio, fueling state and the D03.8/D03.9
  DID references. Use repeat warm plateaus at similar RPM but different road
  loads, then other RPM regions; retain enrichment and lift transitions as
  separate regimes. Use naturally occurring windows and report absent regimes
  rather than assuming the operator performed a maneuver.
  **Evaluate:** first check the simpler `air_per_event = 120 × MAF / (RPM ×
  cylinder_count)` hypothesis (20 × MAF/RPM for this profile). Test whether
  load/air-per-event has a repeatable RPM dependence across held-out windows.
  Treat zero RPM, near-zero load and saturated load as separate unusable cases,
  not fitted values. Temperature is a candidate explanatory variable, not an
  automatic density correction to a signal already expressed in g/s.
  **Outcome:** with an independently supported load reference, compare the
  estimated BFS → ROM target prediction against acquired PID 0x44 on held-out
  steady enriched samples. Preserve interpolation and estimator uncertainty;
  an uncertainty interval spanning different target cells is unresolved. Without
  a reference, publish only a normalized-load/air-charge consistency result,
  not BFS in ms. Do not fit the estimator to PID 0x44 and then use that same
  target agreement as independent validation; do not use 0x1134 to validate
  the model intended to judge 0x1134.
  **Prepare step done 2026-09-22 (desk): the reference is missing.** No
  Calculated Load table is transcribed in any local artifact (the docs and
  the 08-08 RE folder only mention it), so any load reference here would be
  estimated, never a ROM value. The evaluate and capture steps wait on the
  truck (a hybrid block with ENGINE_LOAD, RPM, MAF, INTAKE_TEMP and repeat
  plateaus).
  *Closed 2026-10-03, superseded by P02.14 (`ca80026`).* The question assumed
  BFS could not be read directly. It can: DID 0x1208 is the ECM's base fuel
  schedule, raw × 0.00049 ms, matching UpRev's displayed B-FUEL SCHDL on
  405/405 rows (operator decision 2026-10-02). `derived:BASE_FUEL_SCHEDULE`
  carries it in ms and is the Xterra's target-map load axis. A load-based
  reconstruction would only estimate a quantity that is now measured, so the
  capture and evaluate steps are not owed. A load ↔ air-per-event consistency
  check remains possible on `default` sessions, but nothing waits on it.

- [x] **D03.15 Search for a BFS-like DID by separating airflow, base schedule
  and actual injection.** **New question:** which remaining registers survive
  conditions that break the ordinary RPM/MAF/PW correlation? The earlier
  [BFS search](../../../data/analyses/bfs-provider-search-20260805.md) rejected
  the coarse broadcast proxy, and 0x1209 is now MAF; neither should be promoted
  again by a high r² in one drive.
  **Prepare:** re-screen September 17's per-request DID logs for unexplained
  load-sensitive registers, preserving an explicit shortlist and reasons for
  inclusion/exclusion. The old pass-aligned exploratory scripts are not the
  acceptance comparison. If no candidate survives, record that result before
  spending drive time on another undirected 138-DID rotation.
  **Capture:** use at most four shortlisted DIDs per block with raw 0x1208 (PW),
  0x1209 (MAF) and 0x1219 (RPM); use a prepared hybrid profile when Mode 01 load,
  commanded ratio or fueling-state evidence is required. Record actual skew.
  Seek similar RPM/different load, different RPM/similar air-per-event, and
  entry/exit from enrichment. A lift interval adds evidence on whether a field
  follows actual injection; do not define fuel cut using the candidate being
  tested or assume a BFS register must stay nonzero during cut.
  **Accept/reject:** reproduce the known RPM mapping as a positive control,
  then test candidate scales on independent windows and operating regimes.
  A field that only follows g/s is a MAF candidate; one that separates with
  enrichment may be a corrected-fuel quantity. These are discriminating
  observations, not unique identities. Report `scale_verdict`, n_pairs,
  separation and regime coverage, including explicit refusals. Without an
  independent BFS reference retain “BFS-like candidate”, never a confirmed
  binding or automatic target-map lookup. Preserve a shortlist for a future
  K-line reference rather than forcing a winner tomorrow.
  **Prepare step done 2026-09-22 (desk), via D04.6:** diffing the no-load
  survey (block 3b) against the 30-minute road-load survey (block 5) leaves
  **0x122B and 0x122C** as the only registers that were constant without load
  and move with it and are not already worked (0x1134) or a single one-count
  step (0x1234/0x1237/0x1239/0x123A/0x123D/0x1244/0x1245, each changed once
  in 1,546 passes). 0x122C: 0 for all 364 no-load passes, 0–65,482 under load
  with modal 63,572 and 90 % off-modal in fuel cut; 0x122B: 0 → four levels
  to 1,861, only at high load and cruise
  ([evidence](../../../data/analyses/2026-09-22-d03-d04-desk-pass.md)). The
  capture and the positive-control comparison wait on the truck.
  *Closed 2026-10-03, answered by P02.14 (`ca80026`).* The BFS register is
  0x1208 itself, verified against UpRev's logged B-FUEL SCHDL (raw ×
  0.00049 ms, 405/405 rows) rather than by correlation, so the search has its
  answer. 0x122B and 0x122C stay unexplained load-sensitive registers. They
  are no longer BFS candidates, and a future K-line reference can name them
  without reopening this item.

- [ ] **D03.16 Check known Mode 21 blocks for live data absent from the DID
  search.** **New question:** did Mode 22-only discovery miss an independently
  addressed live parameter block? The [July enumeration](../../../data/analyses/2026-07-28T0330-ecu-address-space.md#service-0x21-kwp-readdatabylocalidentifier--first-enumeration)
  already found 17 responding local IDs in session 0xC0. Repeating all 256 IDs
  without a state comparison adds little.
  **Procedure:** with one diagnostic owner, reproduce `21 10` returning the
  current calibration ID as a control, then repeatedly read LIDs 0x01–0x05,
  0x81 and 0x82 across key-on/engine-off, warm idle, idle with accessory load,
  and a short road-load block if the initial probe works. The historically
  zero 0x02–0x05 and 0x81 blocks are hypotheses to test, not assumed counters.
  Aim for 60 s per available parked state and at most two minutes of road
  sampling; log the actual per-field cadence within the existing bus budget.
  Alternate blocks with the known RPM/MAF/PW DIDs in the same held 0xC0 session;
  decode complete ISO-TP responses and preserve the payload and length.
  **Outcome:** classify bytes as unchanged, time/counter-related, or repeatably
  engine-state-related, then apply D03.15's discrimination to dynamic fields.
  Verify alias LIDs only if needed, rather than polling duplicate blocks for
  the whole drive. Positive payload data establishes access; a negative reply
  establishes only that request's failure in that session/state. A multi-value
  response is not proof that its fields were sampled simultaneously. If all
  candidates remain static, close this experiment with the measured coverage,
  not “BFS is unavailable over CAN”. Exit through the established 0xC0 cleanup
  (`10 81`) and verify normal polling resumes.
  **Waits on the truck (2026-09-22):** every step here is a live read of the
  ECM in a held 0xC0 session across engine states; there is no desk half.
  Reproduce the `21 10` control first (W02.1 asks the same read KOEO).

- [ ] **D03.17 Reproduce the known CAN memory read, then test justified live
  memory candidates.** **New question:** is the missing telemetry reachable
  through memory access rather than a DID? The [known control](../../../data/analyses/2026-07-28T0330-ecu-address-space.md#verified-2026-07-28-koeo--canary-can-read-ecm-memory)
  is session `10 FB` followed immediately by `23 00080000 0010`, returning
  `63` plus 16 bytes. It was reproduced KOEO; engine-running access and readable
  live RAM were not established. The address is a static control, not BFS.
  **Tool preparation required:** `tools/batch_probe.py::probe_memory` currently
  tries other formats, omitting this four-byte-address/two-byte-length request.
  Add the measured form and a positive-control regression. Separate successful
  reads from NRC 0x13/0x31 argument refusals (currently labelled `works`), keep
  conditions/session/security failures explicit, and distinguish unsent requests
  from timeout after transmission. Do not interpret its default guessed RAM
  address or an unsupported request form as an ECU memory-map result.
  **Parked test, about five minutes:** reproduce the static control KOEO with
  exact reply bytes saved. If it passes, repeat the same small read at warm
  idle with before/after telemetry and DTC snapshots; record whether 0xFB
  admission/read/return to normal mode behaves the same. A failure at idle is
  useful state-dependent evidence, not a reason to escalate sessions blindly.
  **Conditional extension:** only if existing ECU/firmware artifacts identify
  plausible RAM ranges, record their provenance before sampling at most eight
  candidate addresses, 16 bytes each, twice per state (48 candidate reads
  across idle, accessory load and a rev hold; no retry loop). Preserve
  independent CAN references. Stable bytes need not
  be ROM, and changing bytes may be timers/counters rather than engine signals.
  If no ranges are justified, stop after the control and mark RAM discovery
  blocked on address evidence; preserve the main drive for D03.8/14/15.
  **Outcome:** exact protocol/session/state accessibility plus any repeatable
  dynamic candidates. No reflash, calibration or other ECU non-volatile write
  belongs to this experiment. Let the tested 0xFB session expire as in the
  earlier reproduction and verify normal telemetry returns; do not assume the
  0xC0 exit behavior transfers to it.
  **Tool preparation done (confirmed 2026-09-22):** `tools/batch_probe.py`
  sends the measured `23 <addr32> <len16>` form byte for byte and separates
  `works` (payload) from `answered` (NRC argument refusal) from unsent /
  timeout (`tests/test_batch_probe_tool.py::test_the_d03_17_memory_read_form_is_sent_byte_for_byte`,
  `::test_an_argument_refusal_is_not_a_same_instant_candidate`). What
  remains is the parked KOEO control and the warm-idle repeat — the truck.

**Shared evidence artifact:** append each experiment's setup, raw-log/session
paths, executed conditions, missing inputs, measured result and next decision
to `data/analyses/2026-09-18-drive-log.md` when run. Close a task with evidence
even if its hypothesis was rejected; leave unexecuted/dependency-blocked work
open. A failed finite search is not proof that K-line exposes something CAN
cannot, and an estimator matching BFS cannot identify where UpRev computes it.

## Opened 2026-09-18 on the drive

- [x] **D03.18 One request timeout stalls the whole hybrid cycle ~2.4 s, and
  benches the DID for 30–48 s.** `fuel-pw-research` recorded 12 all-channel,
  both-mode gaps of 2.28–2.62 s in 34 min (≈1.5 % of the capture). It fails
  `acquisition_gap` on every long research capture. Block A tied one to exactly
  one timeout, with no NRC and HTTP responsive. The same timeout put 0x1208 into
  `auto_miss_backoff` for ~48 s. **Desk:** find where a timed-out read inside
  `did_session_windows` (fast exit / `10 C0` re-entry) serialises the cycle and
  bound it at one cycle; don't miss-back-off a profile-selected DID on one
  timeout. [September 18 drive log](../../../data/analyses/2026-09-18-drive-log.md).
  **2026-09-18 evening ([September 18 evening drive log](../../../data/analyses/2026-09-18-eve-drive-log.md)): the mechanism is corrected. It's two session-control
  failures, not a timed-out DID read.** Desk report:
  `~/canary-data/scripts-0918b/out/stall5s.md`; sims in `stall_sim.py`.
  - **~2.4 s gaps (AM 12, PM 6, eve 16):** a silent `10 C0` entry, where the ECM
    entered but the reply was lost or late.
    - The row pattern is Mode 01, Mode 01, one empty round, then the DID sweep.
    - `on_session_response`'s not-ok branch (phase `mode01`) assumes the ECM
      stayed in the default session. It then sends the Mode 01 batch into the
      held session, where Mode 01 is silent, and eats a second 1.0 s timeout.
    - A `ScriptedEcu` sim reproduces a 2.16 s gap with exactly 1 counted timeout.
  - **5.0 s gaps (new, eve 3):** a silent `10 81` fast exit after a complete DID
    sweep. `DidWindowController._close_window` then waits out the full
    `obd_did_session_lapse_seconds` = 5.0 (`on_exit_response` returns early on
    timeout).
    - Verified live: `fast_exit_failures` = **3** since boot, exactly the three
      gaps. Session control shows 212 timeouts and **0 NRCs**.
  - **Minimal fixes:**
    - (1) a silent entry failure means "session state unknown": send the fast
      exit (or retry the entry once) before any Mode 01;
    - (2) retry a silent fast exit once before falling back to the lapse;
    - (3) optional: a shorter, measured session-control timeout.
    - `test_transient_exit_failure_costs_one_lapse_and_is_retried` pins today's
      5 s behaviour and must change with fix 2.
  - The "0x1208 benched 30–48 s" part didn't appear in these three captures.
  **Fixed in `530398e`, deployed 21:27Z, validated live ([September 18 evening drive log](../../../data/analyses/2026-09-18-eve-drive-log.md)).**
  - A 10.6-min `fuel-pw-research` block had **0 all-channel stalls ≥ 2 s**,
    against ~6 expected from the same evening's baseline (16 in 27.5 min).
  - All 6 session failures were silent entries, and all 6 were handled by the
    new exit-first path (`unknown_session_exits` 6).
  - **Still open:** the exit-retry path didn't fire live (`fast_exit_failures`
    0), so it's verified by sims and tests only. The "0x1208 benched" symptom
    was never reproduced. Close this after one live exit retry is observed.
  **2026-09-23 `tuning` capture, a different shape (not investigated):** one
  2.5 s gap at 14:51:01Z on every Mode 01 channel only; the eight DIDs and the
  RAM windows kept their cadence (max gap 2.03 s). The poller counted exactly one
  request timeout between 14:51:03 and 14:51:08Z and nothing in the journal. A
  Mode 01 batch timeout that costs ~2.5 s of Mode 01 while Mode 22 continues is
  not the session-control path fixed above. `fast_exit_failures` was not read.
  **2026-09-26 desk: explained from the stored rows. The cause is ISO-TP
  flow control, not session control and not the scheduler.** The capture
  records the ECM's `0x7E8` frames as `can` rows, and these show the wire
  exchange (`~/canary-data/scripts-0926/out/d0318_obd_rows_145057-145108.txt`,
  scanner `isotp_scan.py`):
  - 01.248 last DID, then 01.258 `50 81`: the fast exit was answered, so
    the exit-retry path was not involved. At 01.320 the Mode 01 batch
    answered with a First Frame, `10 0F 41 0C …`, and **no Consecutive
    Frame followed**. The batch waited out `obd_request_timeout_seconds`
    (1.0 s). It counts as a `batch_failure`, not in `request_timeouts`.
  - 02.322 `50 C0`: by then the cycle-grid window was overdue, and session
    control goes before Mode 01. The 8 DIDs followed at 02.334–02.411. Each
    is a single frame, so they were unaffected; this is why Mode 22 "kept
    its cadence" (≤1.31 s per DID).
  - 02.422: RAM window 1 (`ECM_TARGET_EQUIV_RATIO` + `ECM_ENGINE_PERIOD`)
    got its FF and CF1, then **CF2 never came**. This one also cost 1.0 s.
    It is the single `request_timeouts` increment the poller saw: the count
    went 32162 → 32163 between the 14:51:03.14 and 14:51:08.18 reads. The
    loop publishes metrics every 5 s, and the whole episode falls inside one
    publish interval, which also had 104 requests against the usual ~150.
  - 03.425: RAM window 2 was answered complete. 03.435 `50 81`. 03.527
    Mode 01 resumed. The Mode 01 gap is 01.051 → 03.527 = **2.48 s**, which
    is two 1.0 s ISO-TP timeouts back to back plus the window between them.
    The RAM channels were not unaffected. By `0x7E8` arrival, window 2
    went 01.160 → 03.425 (2.27 s) and window 1 went 01.149 → 03.737
    (2.59 s), against a 1 s cadence.
  - **How often:** 71 of 13,383 multi-frame responses (0.53 %) in this
    capture ended after the FF. The rate is similar elsewhere: 13 of 2,168
    (0.60 %) in `2026-09-23T150658` (`default`) and 30 of 6,109 (0.49 %) in
    `_capture-tuning-2026-09-23T164007`. In 70 of the 71 no CF arrived at
    all, and for 69 of those the 100 Hz broadcast ids within 20 ms show no
    receive loss. So
    either our flow control (`0x7E0 30 00 00`) did not reach the ECM in
    time, or the ECM did not honour it. Our own TX is not recorded, so the
    stored rows cannot tell which. The 02.422 CF2 loss is different: 0x6F7
    and 0x2DE each drop a 100 Hz frame at the same instant, which points to
    receive-side loss.
  - **Not explained by `56bc909`.** Its grid-anchored cadence and its
    engine-start re-probe change when requests fall due. Neither bounds how
    long a lost CF costs, so the same pair of FF-only replies would give the
    same ~2.5 s gap today. The fix would be at the ISO-TP layer: bound the
    wait for a CF after flow control (the ECM sends CFs within ~3 ms) well
    below the 1.0 s request timeout, and maybe re-send flow control once.
    Proposed, not implemented.
  - **Remaining:** (truck) one live fast-exit retry, as above. Separately,
    the FF-only failure needs a decision on whether to bound the CF wait
    under this item or a new one.
  - *CF wait bounded 2026-10-03 (operator: "if it is a good feature then yes
    implement it").* `isotp.await_response`, the one receive policy for every
    exchange (collector and standalone client), now bounds a started
    multi-frame response by ISO 15765-2 N_Cr:
    `CONSECUTIVE_FRAME_TIMEOUT_SECONDS = 0.15`.
    - **Why 150 ms.** We send flow control with STmin 0. The stored 0x7E8
      frames of `2026-09-23T150658` put all 2,155 FF→CF and 2,251 CF→CF gaps
      under 0.1 ms at the recorder's resolution, so 150 ms is far above any
      healthy gap and frees ~0.85 s per loss.
    - **No CF at all after the FF** (70 of 71 losses): flow control is
      re-sent once and the bound restarts. Ours may not have reached the
      ECM, which waits its own N_Bs for it. Then the exchange ends.
    - **A CF lost mid-response** (the 02.422 case: receive-side loss): it
      ends at N_Cr, with no resend.
    - **Visible.** `Exchange.continuation_lost` / `flow_control_resent` name
      the outcome. The loop counts `continuation_losses`,
      `flow_control_resends` and `flow_control_rescues`, all in
      `/status` `health.obd_scheduler.receive`, so a slower ECU would show
      up as losses rather than hide. `consecutive_frame_timeout=None` keeps
      the old wait-to-deadline.
    - **Effect on the 2026-09-23 pair:** two 1.0 s timeouts become at most
      2 × 0.30 s (FF-only, resend included) or 0.15 s (mid-response). Not
      yet observed live; the next drive's `receive` counters will say how
      often the resend rescues.
    - Tests: `tests/test_isotp_continuation_timeout.py` (6: healthy, FF-only
      with resend, a resend rescue, mid-response loss, unbounded, silence and
      single frames not counted) and
      `test_obd_scheduler_receive_metrics.py`.
  - **2026-11-08 live, closed `[x]`, relaxed (operator authorization
    2026-11-08: close with data close enough).** `ltft-gate` captures, daemon
    c4458e5b, full `/status` at 20:05:08Z
    (`~/canary-data/2026-11-08-drive/status_full_200508.json`, scheduler life
    from the 19:09Z start, ~56 min):
    - **The exit retry fired live:** `did_profile` `fast_exits` 3,452,
      `fast_exit_failures` 4, **`fast_exit_retries` 1**, `unknown_session_exits`
      6, `session_failures` 54, `windows_opened` 3,448 = `windows_completed`
      3,448, `last_exit_nrc` null (every failure was silence, no NRC).
    - **No 5 s lapse in the stored rows.** Mode 01 `RPM` / `THROTTLE_POS` over
      the three captures (13,168 RPM rows; 12,788 / 12,344 routine intervals,
      scripts `checklist/gaps2.py`): excluding key-off (84, 98 and 289 s) and the
      capture-boundary gaps, the longest gap is **3.61 s** (RPM) / 2.64 s
      (THROTTLE_POS); 21 / 23 gaps ≥ 2 s, 1 / 0 ≥ 3 s, none ≥ 5 s. A silent
      `10 81` exit that cost the old lapse would be a ≥ 5 s all-channel gap.
    - **CF wait (`isotp.await_response`, N_Cr 0.15 s) live:**
      `receive` 5,644,534 frames, `parse_errors` 0, `continuation_losses` 98,
      `flow_control_resends` 100, `flow_control_rescues` 4. Last poll of each
      engine-on scheduler life (18:08-18:18, 18:18-19:09, 19:09-20:19Z;
      12.31 M frames): losses 10 / 57 / 118, resends 11 / 55 / 120, rescues
      1 / 0 / 4, `parse_errors` 0 throughout. **So a lost CF is ~1 per 66 k
      frames, and one flow-control resend rescued only ~3 % (5 of 186; 4 of 100 in
      the 20:05 snapshot)**; the
      other exchanges end at N_Cr (0.15 s, or 0.30 s with the resend) instead
      of the old 1.0 s timeout.
    **Relaxed:** the counters count the retry sent, not its outcome (a failed
    retry would add a fifth failure, which the 4 / 1 split cannot separate from
    the 3 silent-entry exits), so "one retry observed" is the criterion met,
    not "one retry observed to recover"; the no-5 s-gap result is what stands
    in for recovery. 2.0-3.6 s Mode 01 gaps still occur: 28 RPM gaps of 2-30 s, 24 of which
    have only DID / memory rows inside (Mode 01 silent, Mode 22 kept its
    cadence: the 09-26 CF-loss shape). They were not attributed one by one to
    a CF loss versus a session entry, and the FF-only versus mid-response
    split of the 98 losses is not in the counters.

- [x] **D03.19 A key-off boot disabled multi-PID batching for the whole session.**
  Silent timeouts from a sleeping ECM counted as batch rejections (6→4→2→off in
  6 s), so `default` ran ~8 reads/s instead of ~19.5 on every drive where the Pi
  boots before the engine. Fixed in `63d5e51`: a failure counts only as an NRC
  or after the ECU has answered since the previous failure; a silent probe single
  takes no miss backoff. **Verified live** on an unplanned key-off at ~15:25Z:
  `failures_while_ecu_silent 32`, `size_narrowings 0`, still 6 PIDs per batch.
  [September 18 drive log](../../../data/analyses/2026-09-18-drive-log.md).
  **Key-off BOOT verified 2026-09-20 15:24Z** (the last unverified path): the Pi
  booted with the engine off; 3 min in, 0 "batching disabled" lines, `max_pids`
  6, `size_narrowings` 0, and 10 of 10 batch failures counted as
  `failures_while_ecu_silent`. Pre-fix this collapsed 6→4→2→off in 6 s.

- [ ] **D03.20 Detectors fire out-of-distribution at altitude.** At 63–73 kPa
  (2,870–3,880 m) the anomaly rate rose ~4×. The LSTM's top feature was
  BAROMETRIC_PRESSURE on every event, and the behavior surfaces flagged
  TIMING_ADVANCE and AIR_MASS_PER_CYCLE. The corpus is ~80 kPa. **Desk:**
  condition the surfaces on baro or air density, or add high-altitude sessions to
  the corpus; don't raise thresholds. [September 18 drive log](../../../data/analyses/2026-09-18-drive-log.md).
  *Behavior-side measurement 2026-09-22 (B04.1, does not close this):* on 31
  independent September drives the timing surface carries a **+6.99° bias in
  the 50–70 kPa stratum** (MAE 10.6°, R² −0.08) against +0.03° at 76–82 kPa,
  and air mass MAE 0.208 g/cycle overall — measured per baro stratum by the
  behavior evaluator (`behavior_validation.yaml: report_regimes`). Only **one**
  local drive is below 70 kPa, so an air-density axis cannot yet be supported;
  conditioning the surfaces is B04.2/B04.6, deferred on the operator's
  2026-09-22 "don't retrain yet". The IF/sequence side of this item is
  untouched. [B04/B05 measurements](../../../data/analyses/2026-09-22-b04-b05-measurements.md).
  *Behavior side, 2026-09-23 (B04.2/B04.6/B04.8; does not close this):* the
  surfaces were refit on the current calibration (ZS44A-20260920, operator
  corpus decision 2026-09-23) and promoted diagnostic-only. Every one fails
  B02 and none alerts. That corpus reaches only **76–90 kPa** (test rows
  79–82 kPa), so there is still no drive to fit or validate an air-density
  axis on. Air mass is not retired in favour of VE: on identical rows VE adds
  0 unique detections, and a synthetic baro −3 kPa or IAT +10 °C error is
  caught by neither. Air mass and VE now share one fusion voice
  (`behavior:airflow`), so an altitude excursion on one MAF reading no longer
  counts as two agreeing detectors. **Still owed here:** a drive below
  76 kPa on the current calibration, then B04.2/B04.6 re-run.
  [Refit report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §4.

- [x] **D03.21 The Pi's memory limits are not enforced.** The kernel cmdline
  has `cgroup_disable=memory`, so `MemoryMax=6G` / `MemorySwapMax=0` exist only in
  `systemctl show`, and 2 GB of swap is live. **Operator decision:**
  `cgroup_enable=memory` + reboot (or swap off). Until then D03.6's digest guards
  (child + RLIMIT_AS + row budget) are the only protection. Also verify with
  `/sys/fs/cgroup/.../memory.max`, not `systemctl show`. [September 18 drive log](../../../data/analyses/2026-09-18-drive-log.md).
  **Resolved on the Pi, read 2026-09-22 20:30 MDT (uptime 2 h 51 m, i.e. the
  reboot after the daemon-startup session's `cgroup_enable=memory`):**
  `/sys/fs/cgroup/system.slice/canary.service/memory.max` = 6442450944,
  `memory.swap.max` = 0, `memory.current` ≈ 824 MB, `cgroup.controllers`
  lists `memory`, `/proc/cmdline` carries both `cgroup_disable=memory` and
  `cgroup_enable=memory` (the later flag wins), and `swapon --show` is empty —
  swap is off outright, not merely capped. `/status.health.system.
  memory_limits_enforced` (S02.8, `0c753a5`) reports `true` with the same
  path, so a drive's health line now shows it. The cgroup file was the check,
  as this item asked; `systemctl show` was never consulted.

## Carried from archived roadmaps — 2026-09-21

Four roadmap documents were one small remainder from finished. Those remainders
live here now, so the documents could be archived
(`../archive/README.md`). Each item names where its history is.

- [x] **D03.23 MCP tool registration table.** *From `../archive/refactoring.md`
  2.2.* `src/mcp_server/server.py` hand-writes an `@mcp.tool()` closure per
  tool; most only delegate, with the docstring copied from the `tools/` or
  `write_tools/` function. Replace the pure delegations with a registration
  table `(delegate, needs_audit, gate)` using `functools.wraps`, so FastMCP
  infers schema and docstring from the delegate and the module docstring is the
  single source. Keep the conditionally-registered and real-logic wrappers
  hand-written. **The numbers in the original are stale:** it counted 34
  delegations of 48 tools; the file now has 70 tools in 1,254 lines, so
  re-count first. The original blocker — another session editing `server.py`
  — has cleared (last change 2026-09-17). **Do this first:** a test that
  snapshots every tool's name and input schema, run before and after, because
  the failure mode is a silently changed schema or a dropped registration.
  2.3 used the same adversarial check.
  **Done 2026-09-21.** Re-counted: 70 tools (all conditionals enabled), of
  which 55 were pure delegations. `tests/test_mcp_tool_snapshot.py` pins every
  tool's name, description, input and output schema
  (`tests/fixtures/mcp_tool_snapshot.json`) and was written and run first.
  The 55 are now rows of `READ_DELEGATIONS`/`WRITE_DELEGATIONS`
  (`Delegation(delegate, needs_audit)`); `bind_delegation` uses
  `functools.wraps` and drops the injected `client`/`audit` from the
  signature, refusing a delegate whose leading parameters are not exactly
  those. 15 stay hand-written (validation, branching, config-derived args,
  conditional registration; `get_binned_analysis` because its delegate takes
  a `units` input the tool does not expose). No `gate` column: none of the 55
  is conditionally registered. Before/after: same 70 names, input and output
  schemas identical; 49 descriptions changed on purpose, since the delegate
  docstring is now the source. Facts that only the server docstring had were
  moved into seven delegates first (`list_capture_profiles`, `list_sessions`,
  `start_job`, `get_replay_status`, `get_capture_quality`,
  `disarm_tuning_capture`, `arm_tuning_capture`). `server.py` 1,254 → 835
  lines; the 206 MCP tests pass.
- [~] **D03.24 Multi-PID batching: what was never verified.** (b), (c), (d)
  done 2026-09-21; **(a) remains and needs the truck.** *From
  `../archive/multi-pid-polling.md` 3.4, 4.2, 4.3, 4.4.* Batching has been in
  daily use since 2026-07-26 and these were never run:
  (a) **batched vs single cross-check** — poll the same channels both ways in
  alternating windows and confirm decoded values agree; cheap insurance
  against an offset-walk bug that only shows on real data.
  (b) **timing parity** — batched readings share one timestamp where single
  reads each had their own; confirm the feature pipeline's gap handling and
  the `__roc` derivatives behave when six channels land at once.
  (c) **`tools/can_obd_probe.py` cannot send a batched read**, so the next ECU
  still needs a throwaway script to characterise.
  (d) **`docs/data-architecture.md` §2 still quotes dedicated-bus probe
  figures.** Two measured in-vehicle numbers now exist and belong there, as
  different things: 116.8 req/s sustained by the *daemon* (H8, 2026-09-10) and
  649 req/s back-to-back single-DID reads answered by the *ECM* (2026-09-21).
  Not carried: 3.1 and 3.2, low-value by that document's own assessment.
  **(b) done:** `tests/test_batched_timing_parity.py` runs six channels as one
  shared-timestamp batch and as single reads 1.5 ms apart through `to_wide` →
  `resample_wide` → `compute_features`: value, `__gap`, `__roc`, `__mean`,
  `__std` columns are identical; a whole-batch outage flips all six `__gap`
  flags on the same rows and NaNs their `__roc`; `__roc` equals the known
  slope. The live path (`LiveFeatureSource`) ages the batch as one, identical
  to staggered reads. One property surfaced, shared by both shapes: a
  look-back that lands on a cell carried forward inside `max_gap` reads a
  slightly high slope (1.033 vs 1.000 for 30 s after a 6 s outage).
  **(c) done:** `tools/can_obd_probe.py --batch 0C,0D,04` (repeatable) and
  `--batch-supported` send batched Mode 01 through
  `CanObdClient.query_mode_01_batch` + `split_batched_response` with registry
  widths, printing per-PID raw/decoded values, omitted PIDs and whether the
  walk stayed clean (`tests/test_can_obd_probe.py`).
  **(d) done:** `docs/data-architecture.md` §2 labels the 667/1387 figures as
  dedicated-bus and adds both in-vehicle numbers as different quantities with
  sources: 116.8 req/s daemon sustained (H8, 2026-09-10) and 649 req/s ECM
  answer rate (2026-09-21).
  **Remaining — (a):** the batched-vs-single alternating-window cross-check on
  real data.
  *Desk proxy for (a), 2026-10-03; it does not close it.* An offset walk in
  batched decoding would show as a drifting ratio against an independent
  instrument. Every September session on disk with both, 35 of them, was
  checked: Mode 01 `obd:RPM` against broadcast `decoded:ECM_RPM`, paired
  same-instant (`align_pairs`, 20 ms), above 600 rpm, 200–4,651 pairs per
  session.
  - The per-sample ratio's median is **0.992–0.999** in every session, with
    p05–p95 inside 0.987–1.002.
  - The first and last thirds of each drive differ by at most 0.004.
  - So there is no walk on any drive. The small sub-1 bias is consistent
    across sessions, which is what the 20 ms pairing skew on changing rpm
    would give; it is not a drift.
  - What only the truck can show is a single-read window beside a batched
    one, which is (a) itself.
- [~] **D03.25 Retrain and verify after the Phase-1 distribution change.**
  *From `../archive/ml-correctness-hardening.md` 1.6.* The laptop half landed
  (smoke retrain, LSTM replay diff —
  `data/analyses/ml-hardening-1.6-retrain-replay-diff-2026-07-19.md`). Owed:
  the IF-side replay diff and the Pi deploy. **This is one debt with three
  names** — D02.5 (the IF went blind after a binding retraction) and D03.22
  (mode labels used raw counts) both end in the same retrain, and a fourth
  reason arrived on 2026-09-21: seed revision 4 added the injector-pulse DIDs
  to the default profile, which changes the feature spec again. Do them as one
  retrain, not four.
  **2026-09-22: the retrain is deferred by the operator ("don't retrain yet").**
  Its prerequisites are now in place: the D03.22 laptop relabel is done,
  D01.10 and D03.27 made the serving and training resample paths agree, and
  B03's policy keeps demoted behavior columns out of IF feature selection.
  The corpus choice stays with the operator.
  **2026-09-23: retrained and evaluated on the laptop (A06.3); not
  deployed.** The operator lifted the deferral and chose the corpus: drives
  recorded under ZS44A-20260920 only.
  - **Candidates:** `models/_candidates/2026-09-23-na/`, IF sha256
    `69463c07…52b3c` and LSTM `3edfd8fe…99f2`. Both are built against the
    current bindings, 0.3.0 mode labels and the seed-revision-4 profile DIDs.
  - **Evaluation:** one held-out drive, deployed vs candidate on the same
    rows:
    [`2026-09-23-a06-3-retrain-evaluation.md`](../../../data/analyses/2026-09-23-a06-3-retrain-evaluation.md).
  - **r2, 2026-09-23 (later).** The operator chose to deploy the LSTM only,
    keep the deployed IF, and archive the 14 excluded sessions (done, laptop
    and Pi).
    - The verifier found drive grouping in three places. The r1 LSTM's
      validation drives shared physical drives with training.
    - One definition now serves every caller, `src/analysis/drives.py`:
      vehicle activity, captures bridging, and a pause of more than 300 s
      splitting even one recording.
    - The LSTM was retrained as r2: `models/_candidates/2026-09-23-na-r2/`,
      `model.pt` sha256 `f7389acf…1f7a`, `lstm_autoencoder_2026-09-24T035125.284392Z`.
    - On D6, beside the deployed IF, on the same rows and windows as the
      deployed LSTM (live sessions):
      - global path: 0 episodes against 29;
      - family path: 8 episodes over 131 of 2,269 rows, against 4 episodes
        over 1,431 rows.
      - [Report](../../../data/analyses/2026-09-23-a06-3-retrain-evaluation.md),
        r2 section.
  - **r3, 2026-09-24.** The operator took option (b), dropping parked rows,
    and the IF-only quarantine gap was fixed.
    - Output: `models/_candidates/2026-09-23-na-r3/`, `model.pt` sha256
      `b14fc599…ec65`.
    - 6,869 of 11,084 rows were parked. Training fell to 600 windows, and
      validation became a tail block of D4 (real driving, not an independent
      drive).
    - On D6, on the same rows and windows as r2, r3 is noisier: 15 against 8
      per-channel episodes live, 70 against 32 with captures, 1 against 0
      global live.
    - **Recommendation: deploy r2.** Keep the filter for future retrains.
      [Report](../../../data/analyses/2026-09-23-a06-3-retrain-evaluation.md),
      r3 section.
  - **r2 deployed 2026-09-24 16:03:22Z (10:03 MDT)** beside the unchanged IF:
    `lstm_autoencoder_2026-09-24T035125.284392Z`, `model.pt` sha256
    `f7389acf…1f7a`, hash-checked on the Pi, no version mismatch, prior
    LSTM backed up under `models/nissan-xterra-2008/_prior/2026-09-24-pre-r2/`
    (A06.3).
  - **Remaining:**
    - the Pi side of D03.22 (`label_sessions.py` there, with the Pi idle);
    - an IF retrain once idle and highway clear the 3-drive floor on this
      calibration.
  - **IF-side replay diff, done 2026-10-03 (desk).** Deployed IF
    `isolation_forest_2026-09-20T054703` (`af1720e7…`) against the
    2026-09-23 candidate (`69463c07…`), each beside the served r2 LSTM
    (`f7389acf…`). `tools/replay_event_diff.py run`, same tree, held-out D6
    live session `2026-09-23T150658` (1,142 s, 782 IF ticks):
    - **Events identical:** 5 LSTM `evidence` events on both sides, and no IF
      event from either.
    - **Coverage.** The candidate scores **422 of 782** ticks, the deployed IF
      748. The candidate has no highway model (refused at training on the
      3-drive floor), so all **242 highway ticks** are missing features, and
      it misses 103 city ticks against 20.
    - **Margins.** Neither crosses its threshold. The deployed IF's closest
      approach is 0.002 below (p99 0.010 below), the candidate's 0.019 below.
    - **Reading.** This supports keeping the deployed IF (operator,
      2026-09-23). The candidate would blind the IF on highway for any drive
      like D6. The IF retrain stays owed until idle and highway clear the
      floor on this calibration.
- [~] **D03.26 Backfill the vehicle ledger.** *From
  `../archive/eval-longitudinal-and-dtc.md` 3.6.* The ledger has one placeholder
  entry; real entries need the operator's history and go in through
  `scripts/vehicle_log.py add`. Much of that history is already written down
  and only needs transcribing: the flash history in
  `data/nissan-xterra-2008/tune_revisions.yaml` (five revisions, with times
  and K), the 2026-06-28 header/spacer work and its battery disconnect
  (`engine-tuning-support.md` G.2), the LTFT relearn between 2026-09-10 and
  2026-09-18, and the 2026-07-18 profile migration.
  **Transcribed 2026-09-21** into `scripts/backfill_vehicle_ledger.sh`: eight
  `vehicle_log.py add` entries (the header/spacer mod, its battery disconnect,
  the profile migration, the four post-stock flashes, the LTFT relearn first
  seen 2026-09-18), each citing its source. Re-running skips titles already
  present. Applied to the laptop ledger. **Remaining:** (1) the ledger is a
  gitignored per-host file, so the Pi's, which the daemon reads, needs
  `scripts/backfill_vehicle_ledger.sh --config /etc/canary/canary.yaml` run on
  the Pi; (2) what only the operator knows: odometer readings, the cat-back
  exhaust install date, and which of these events should be
  `corpus_boundary` (all were entered `false`).
- [x] **D03.27 Session-deviation analysis resampled differently from
  training.** *Found during B02.*
  `src/analysis/session_deviations.py::analyze_session` built its own
  `ResampleConfig` from `resample_hz` and `slow_channel_max_gap_seconds` only.
  `max_gap_factor` and `session_gap_seconds` silently took the dataclass
  defaults, so any config that set either put the analysis on a different grid
  from the one the behavior surfaces were fitted on. Latent today: both
  defaults equal the config defaults. **Fixed at source:** it now calls the
  shared `feature_pipeline.resample_config_for(config)`, the one batch recipe
  training uses (39139df), and the hand-built config and its import are gone.
  Regression test:
  `tests/test_session_deviations.py::test_analyze_session_resamples_with_the_training_recipe`
  spies the resample config for a config with non-default `max_gap_factor=5`
  and `session_gap_seconds=45`, and asserts it equals `resample_config_for`.
  It fails on the old code and passes now.
  **Swept the rest of the callers, and made the unsafe form unwritable.**
  `resample_config_for(config, *, hz=None)` now lives beside `ResampleConfig`
  in `src/preprocessing/resampler.py` (re-exported from `feature_pipeline`, so
  existing imports hold); `hz=` overrides only the grid rate, gap rules still
  come from the config. Every caller that means "the training grid" now uses
  it: `scripts/disposition_report.py` (took hz only), `src/models/synthetic_fault_eval.py`,
  `src/datasets/health.py`, `scripts/replay.py`, `scripts/detector_benchmark.py`,
  both constructions in `scripts/train_anomaly_model.py::evaluate` (now one
  shared object), `src/preprocessing/loader.py::resample_and_segment` and
  `src/preprocessing/live.py` (already complete; now the same call), and
  `src/datasets/export.py` at the export's requested rate
  (`resample_config_for(config, hz=spec.resample_hz)`). The sweep found one
  more, a real defect: `build_feature_spec` recorded the manifest's
  `max_gap_seconds` from `ResampleConfig(hz=...)`, i.e. the default factor
  2.0, while `LiveFeatureSource` applied the configured `max_gap_factor`.
  Latent (the config default is 2.0), fixed. **Deliberately different, kept:**
  `src/analysis/binned_analysis.py::rows_to_resampled_frame`, an ad-hoc display
  grid at a caller-chosen rate for the binned-analysis/session-timeseries tools
  with no `CanaryConfig` in scope; the reason is stated at the call site.
  `tests/test_resample_config_construction.py` AST-scans `src/`, `scripts/` and
  `tools/` and fails on any direct `ResampleConfig(...)` outside a two-entry,
  reasoned allowlist (`resampler.py`, `binned_analysis.py`), with a check that
  the allowlist cannot go stale; per-caller spy tests with every gap rule off
  its default cover health, export, replay, detector benchmark, disposition
  report, `train_anomaly_model.evaluate`, the synthetic dropout ceiling, the
  live source and the manifest's `max_gap_seconds`.

## Carried from archived roadmaps — 2026-11-08

Thirteen more roadmap and task documents were one small remainder from
finished, and the operator asked for as many as possible in the archive
(2026-11-08). Those remainders live here now (`../archive/README.md`,
`archive/`), each naming where its history is; `imu-gps-integration.md` and
W10 carry nothing because every box was ticked, and `workbench-workflows.md`
carries nothing because its one stale partial (W2) was ticked on the evidence
that closed its task checklist.

- [ ] **D03.42 Phone UI: the on-device checks.** *From `../archive/phone-ui.md`
  P1.2 and its "Remaining" list.* Everything is built and tested on the laptop
  (Playwright phone project included); what is left needs a Pi, the truck or a
  real phone, in one session there (operator, 2026-10-13). **P1.2 bench
  acceptance** (nothing to build): on a freshly installed Pi with no WiFi
  profile, provision from Android Chrome and confirm the returned dashboard link
  opens, a second provisioning is refused until a window is opened, and nRF
  Connect reads the status characteristic. The same session owes the P2.4 key
  cycle on the truck (lag after ACC; ACC never reads as ON on the Xterra), a
  real update from the phone (P3.1; the `safe.directory` fix was confirmed only
  on git 2.39.5 in a container), P5.2 on Android (progress across screen sleep;
  the download landing in Downloads), and a pass on a real phone and mobile
  Safari with icon masking. **Accept:** each recorded here with date and device.
- [~] **D03.43 External sensors never train: the retrain half of the source
  gate.** *From `archive/H01-source-policy.md` H01.1.* The gate is built and
  tested offline: `TRAINABLE_SOURCES` in `src/telemetry.py`,
  `src/trainability.py`, a refusal in every feature chooser and in behavior
  recipes and artifact load, `tests/test_trainability.py` (26). The corpus
  diff is 348 → 344 columns, exactly the `gps:speed` family. Serve is left
  alone on purpose: the deployed IF (2026-09-20) and LSTM (r2, 2026-09-24)
  manifests still carry `gps:speed` and its `__mean`/`__std`/`__roc`, and
  excluding them at serve time would blind them (the D02.5 failure). The IF
  load audit lists them as `policy_violations`. **Owed:** "the next retrained
  manifests carry no external-sensor feature", which needs a retrain that is
  *deployed*; D03.25 and A06 own the retrain. The undeployed 2026-10-08
  no-memory LSTM candidate (D03.52) already drops the four `gps:speed`
  columns. **Accept:** after that deploy, neither manifest's feature list names
  a `gps:`, `imu:` or `sensor:` column.
- [~] **D03.44 A model hot-swap on the Pi: measure the swap time.** *From
  `archive/A01-scoring-correctness.md` A01.6.* Code done 2026-09-22
  (`src/daemon/inference_worker.py`): the child answers `WorkerReady` after a
  `WorkerSwap`, and until then the parent scores the Isolation Forest
  in-process and drops the sequence job, so a swap is "sequence not ready",
  not a 3 s reply timeout. Seven tests in `tests/test_scoring_continuity.py`,
  all failing against `606e4ab`. **Owed (Pi):** hot-swap one artifact and read
  `last_swap_seconds` and `last_load_seconds` under
  `/status.detectors.inference.worker_swaps` (`startup_seconds` is there from
  the first boot); the load was estimated at 2–7 s against the old 3 s
  timeout. No measurement is recorded in the docs (checked 2026-11-08).
- [~] **D03.45 Fuel Comp fit: re-run on the corpus once the deciding band has
  data.** *From `archive/N02-fuel-comp-fit.md` N02.7.* Run 2026-11-08 at the
  desk (record `data/analyses/2026-11-08-n02-7-fuel-comp-corpus.md`): 112
  sessions from 08-03, 13 drives, 564 samples. Held-out RMS 2.848 % current
  against 3.275 % for the cells, **no improvement**. The 20261004 drives split
  at the fuel-pump change: pre-pump fit 0 proposed, post-pump fit 0 proposed
  and 22 insufficient. **The restore prediction is not passed and unresolved:**
  the deciding band (≥ 144, 4,000–5,600 rpm) has 3 post-flash samples from one
  pull and none pre-pump. Per the item's rule no factor is proposed. **Owed
  (truck):** WOT pulls at 4,000–5,600 rpm on two drives with
  `sensor:FUEL_PRESSURE` logged through the pull, and part-throttle closed
  loop with purge commanded 0 on two drives; then re-run the record's
  `restore_eval.py`. **Accept:** the restore prediction passes (the proposal
  may inform an edit) or fails with the reason recorded (no proposal).
- [~] **D03.46 Cold start: the Pi trace of what recording still imports.**
  *From `archive/S01-cold-start.md` S01.3.* Desk half done 2026-09-22:
  `src.dashboard.api`, `src.daemon.inference`, `src.models.isolation_forest`,
  `src.control.trip_end`, python-OBD with pint, and httpx moved off the
  recording path (the laptop loads 921 modules there, 1,064 before), pinned by
  `tests/test_cold_start_imports.py::test_recording_path_does_not_import` (18
  cases). Not moved: `sequence_training`/`sequence_dataset` through
  `components` (~14 ms, A02's files), and pandas/numpy, which 17 recording
  modules import at the top (a refactor of collection and storage code, not a
  lazy import). `build_components` and the `FrameDecoder.from_path` bindings
  read still run before the collector tasks and are not broken down. **Owed:**
  the Pi cold trace by S01's method (drop caches, restart, `py-spy dump` every
  ~1.5 s until `/status` answers). **Accept:** no non-collection import before
  `first_record_stored`.
- [~] **D03.47 Move the root filesystem off the SD card.** *From
  `archive/S01-cold-start.md` S01.7.* **Decided yes (operator, 2026-10-04), in
  two steps.** Now: a USB 3 drive whose NVMe later moves to the base with its
  data intact, so the SD card is cloned once; set `usb_max_current_enable=1`
  first (the X1202 feeds the Pi through pogo pins, so the Pi assumes 3 A and
  caps USB at 600 mA). Later: an NVMe base under the Pi in the slot the retired
  X1202 frees, with the power side on the supercapacitor board
  (`CANary-PWR-Spec.md`, `CANary-HAT-Revisions.md` §13.3.0). **Landed, a
  different step:** `/data` on a USB NVMe (`61e725ea`, 2026-10-10; all of
  `/data` read cold in 47 s against ~225 s from the SD card), optional per Pi.
  **Owed:** the *root* move as its own task list (clone, EEPROM boot order,
  confirm `memory.max` and the cmdline edit survive), and after each step a
  cold re-measure by S01.1's method. No document records that the root has
  moved. The numbers it is meant to cut: imports 28.6 s cold against 3.8 s
  warm, the IF artifact 34 s against 3.3 s.
- [~] **D03.48 Sequence detector: the `engine_response` ablation.** *From
  `archive/A03-sequence-inputs-and-training.md` A03.2.* Design done 2026-09-23
  as a feature-spec option (`src/models/sequence_feature_set.py`,
  `train_sequence.py --feature-set engine_response`): inputs condition the
  encoder, targets are the only loss, threshold and score; the deployed 361
  columns resolve to 43 context and 39 target columns, 279 dropped with reasons;
  `all_channels` stays the default (`tests/test_sequence_feature_set.py`). **The
  A02-protocol ablation could not run (2026-10-03):** on r2's split
  `engine_response` was refused by the trainer's coverage guard (82.4 % of
  windows against 90.0 % required) because its 39 targets are polled far more
  sparsely than the 43 context columns. A finding, not a fault to bypass; the
  floor is not lowered. **Owed:** the targets polled densely enough on
  `default` (D03.34), or a floor for an `engine_response` artifact as an
  operator decision (A06's); then both sets trained and graded by
  `evaluate_detectors.py` on the same drives, per drive, against A02.11.
- [~] **D03.49 Sequence detector: one source per quantity, ablated.** *From
  `archive/A03-sequence-inputs-and-training.md` A03.3.* Implemented inside
  `engine_response` 2026-09-23: a broadcast channel is dropped for the PID it
  mirrors only when the ground-truth suite confirms the pair
  (`test_every_dropped_mirror_is_confirmed_by_the_ground_truth_suite`); the
  four coolant channels become `obd:COOLANT_TEMP`; targets keep no
  `__mean`/`__std`/`__roc`, inputs keep `__roc` only, so
  `BAROMETRIC_PRESSURE__std` is gone; unconfirmed same-quantity pairs
  (`did:ECM_RPM` and `obd:RPM`) are listed in `same_quantity_unconfirmed`.
  **Owed:** the ablation D03.48 owes, before any `__std`/`__roc` target comes
  back or the set is deployed.
- [~] **D03.50 Sequence detector: a "top feature" that means something.**
  *From `archive/A03-sequence-inputs-and-training.md` A03.7.* The short-term
  half is done 2026-09-23: events carry `detail.channel_ranking =
  "largest_deviation_from_training_envelope"` and a note that it is not a
  cause, with a `ranking_scope` stamp (`all_channels` or
  `engine_response_targets`), and the anomaly row and evidence card say so
  (`lib/evidence.channelRankingCaption`). Tests: `test_event_from_lstm_result`,
  `test_context_inputs_are_never_scored_or_ranked`,
  `ModelEvidenceHealth.test.ts`. **Owed:** attribution over engine-response
  targets only, grouped by channel family, with driving conditions labelled as
  context. The engine already keeps `engine_response` context columns out of
  the ranking; the rest waits on D03.48, D03.49 and A06's decision.
  **Accept:** judged on A02's protocol against the current artifact, same
  held-out drives and rows, per drive.
- [~] **D03.51 Isolation Forest: the next retrain, once idle and highway have
  three drives.** *From `archive/A06-detector-roles.md` A06.3.* Operator
  decision 2026-09-23: (a) a full naturally aspirated retrain. Done: retrained
  and evaluated on the laptop
  ([report](../../../data/analyses/2026-09-23-a06-3-retrain-evaluation.md));
  the LSTM r2 (`lstm_autoencoder_2026-09-24T035125.284392Z`) went to the Pi
  2026-09-24 beside the unchanged `isolation_forest_2026-09-20T054703`, the
  operator having chosen to deploy the LSTM only. The IF replay diff is done
  (D03.25, 2026-10-03): on D6 the candidate scores 422 of 782 ticks and none on
  highway, against the deployed IF's 748; events identical, neither IF crosses
  its threshold. **Owed:** the next IF retrain once idle and highway clear
  `min_training_drives_per_mode` (3; each rested on 2 drives), and the cost of
  600 trees per tick, measured only for an IF that is deployed. Tracked with
  D03.25.
- [~] **D03.52 LSTM retrain without memory features: the operator's choice and
  the deploy.** *From `archive/A06-detector-roles.md` A06.3, nested item
  (operator-approved 2026-10-08; B08.4's owed retrain).* Trained and evaluated
  2026-10-08, not deployed: `models/_candidates/2026-10-08-na-r2-nomem/`
  (gitignored; set aside by the A02.3 gate, D6 holding 0.40 engine-h against 1
  needed). **The feature-set acceptance failed:** it asked for r2 minus four
  memory features, but 254 → 184 columns (also 28 unpolled Mode 01 columns, 4
  `gps:speed`, 38 `sparse_for_grid`; 4 `derived:AFR_CENSORED_B2` added) and the
  r3 parked-row filter left 600/158 windows. On D6 it sits with r3, not clearly
  apart from r2 (per-channel 47.9 FA/eh [28.8, 74.7] against 20.2 [8.7, 39.7];
  global 0 episodes; one drive); every number is in the candidate's
  `evaluation/`. **Owed:** the operator's choice of feature set and an
  `--accept-unbudgeted-thresholds` reason. If deployed: refresh
  `tests/fixtures/deployed_manifests`, drop the stale
  `manifest_unproducible_allowlist.yaml` entry, and rework
  `test_default_profile_b08_context.py::test_the_deployed_lstm_carries_the_spark_window_so_the_guard_can_fail`.
- [~] **D03.53 Sequence tick budget: the Pi measurement and the operator's
  cadence.** *From `archive/A06-detector-roles.md` A06.6.* Desk half done
  2026-10-05, the cadence route (int8/ONNX not taken; ONNX Runtime is not
  installed): `models.sequence_forward_every_n_ticks` (default 1; n ≤ 3 under the
  shipped 3-in-8 s persistence gate) with `ForwardCadence` in
  `src/models/inference.py`, carried through A02.3 budget levels, harness
  grading, A06.5 verdicts and benchmark comparability;
  `tests/test_sequence_cadence.py`, 41 passed. On a held-out real drive
  (`2026-10-04T194342`, r2 LSTM) 0 of 637 scored results differ at n = 3. Laptop
  `observe` median 12.18 ms → 2.42 ms, though `reconcile_feature_frame_to_spec`
  (every tick) is most of the stage there. **Owed (Pi):** `stages.sequence` and
  missed ticks on the truck at n = 1 against n = 2 or 3 on the same drive
  conditions; the operator's choice of n; any artifact later carrying an A02.3
  level must have it chosen at that n. P02's int8/ONNX option is still open.
- [~] **D03.54 Plausibility range check: the benchmark drives through the
  committee-coverage classifier.** *From `archive/A05-plausibility-checks.md`
  A05.2.* Done: the range check runs on every arrived sample before the quality
  gate and reproduces one known answer (pre-`d947afe` PID 0x0E 0xFF stored as
  63.5 degBTDC: 14 confirmed on the 10 pre-fix calibration drives, none later).
  The benchmark's known answer did not reproduce on `2026-08-07T164617` and
  `2026-09-18T173749` (spikes 45/50, scales 20/40; every miss RPM or
  THROTTLE_POS at idle, inside the envelope), so on 2026-10-13 the operator
  restated it (A05.8): the committees own those faults, and plausibility owns
  those that leave the range or slew envelope (25/25 on the committed 08-03
  slice). **Owed:** run `scripts/evaluate_committee_coverage.py` on those two
  drives (`~/canary-data`) to confirm each of the 5 spike and 20 scale misses
  is owner `committee` and plausibility-owned ones confirm at 100 %; by-hand
  analysis only so far. Can share the run with D03.55 (ii).
- [~] **D03.55 Plausibility on A02's harness: what the 2026-10-13 decision left
  open.** *From `archive/A05-plausibility-checks.md` A05.8.* Measured (evidence
  `data/analyses/2026-09-23-a05-plausibility-evaluation.json`): clean 0 confirmed
  on 38 drives and 17 captures (rules revised on them) and on 12 unseen sessions
  (2026-09-26); spike 97.5 %, scale 75 %, drift 72 %, stuck 12 %, dropout 9 %;
  realistic faults 0, as designed; `plausibility` is a baseline in A02's harness.
  Operator decision 2026-10-13: the committees own RPM/THROTTLE faults at idle and
  stuck-at-cruise; priority PIDs retry after 10 s; the unnamed-channel gaps are
  accepted, not a remainder. **Owed:** (i) a committee that reads RPM and
  THROTTLE_POS ([H04.6](H04-committees.md)), since nothing catches the faults
  the decision gave the committees; (ii) `evaluate_committee_coverage.py` on the
  38 evaluation drives (per-owner recall where idle and cruise occur); (iii)
  `plausibility.yaml` `tiers.fast.interval_seconds` stays 33 s until set from
  `default`-profile drives recorded with the 10 s back-off (the 2026-11-08
  `ltft-gate` gaps, 3.6 s RPM and 2.6 s THROTTLE_POS, were a ~2 Hz tuning-family
  capture). Thin margin: a pending range read at engine stop on
  `2026-09-23T164007` (`obd:mem:ECM_ENGINE_PERIOD`) stayed unconfirmed only by
  the 1.0 s `confirm.range.min_separation_seconds`.
- [~] **D03.56 Plausibility live: the Pi cost against the missed-tick budget.**
  *From `archive/A05-plausibility-checks.md` A05.9.* Built and off by default:
  `plausibility.enabled` wires the evaluator into the live tick
  (`src/daemon/plausibility_live.py`), the per-tick cost is
  `/status.detectors.inference.stages.plausibility`, and a confirmed record
  reaches `AnomalyEventEmitter.emit_plausibility` (log, broadcast, ntfy push,
  never in replay; `tests/test_plausibility_live_alerts.py`). Laptop: 1.84 µs
  per sample; replaying `2026-09-18T173749` added 4.2 ms of loop time per 1 s
  tick (stage median 2.5 ms, p95 3.3 ms). **Owed (Pi):** measure the per-tick
  cost on the Pi against the missed-tick budget before enabling it on a drive.
  **Accept:** `stages.plausibility` inside that budget.
- [~] **D03.57 Timing behavior model: coverage for a load-aware context.**
  *From `archive/B04-timing-and-airflow.md` B04.2.* Assessed 2026-09-23 on
  ZS44A-20260920; stays exploratory. The RPM/load surface (candidate v3), judged
  on 2 untouched drive groups
  ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md)
  §4): MAE 8.88° below 60 °C coolant (2 cold starts) against 4.21° at 90–100 °C,
  10.9° below 20 % load (overrun) against 1.18° at 20–30 %, IAT strata 6.08° /
  2.89° / 0.20° (high IAT is idle, so it stands in for operating point), baro
  only 76–90 kPa. Neither IAT nor coolant explains the dominant error. **Owed
  (data):** drives below 76 kPa on the calibration in force (air density), more
  cold starts (coolant), and a way to model low-load overrun; B08.3/B08.4 own
  the newer timing-lookup candidates. **Accept:** exploratory with the missing
  coverage named, never an RPM/IAT-only fit or broadened bands.
- [~] **D03.58 Air-charge normalization across pressure.** *From
  `archive/B04-timing-and-airflow.md` B04.6.* Measured 2026-09-23: the
  air-charge recipe (raw DID counts, scale unpinned, calibration-dependent)
  populated 41 of 49 cells and missed its target, held-out MAE 1771 counts
  against 1500; air mass (MAE 0.125 g/cycle) and ambient VE (3.17 %) both fail
  B02, relative resolution 0.45 against 0.40; the test rows span baro 79–82 kPa
  and IAT 20–58 °C. The pressure comparison is data-blocked. **Owed (data):**
  independent drives below 76 kPa on the calibration in force, to compare
  density-aware alternatives. Until then density normalization is not validated
  across pressure, and missing pressure or IAT is an unavailable input, not
  evidence of stability.
- [~] **D03.59 Re-run the air-mass retirement decision with a low-pressure
  drive.** *From `archive/B04-timing-and-airflow.md` B04.8.* Decided
  2026-09-23: air mass is not retired. VE adds 0 unique detections (B04.7) and
  fails B02, and air mass also fails (MAE 0.125 > 0.08 g, synthetic detection
  0.25), so it stays diagnostic-only (B03.9 refresh). Fusion grouping is fixed:
  `FusionScorer.behavior_source` derives the group from the graded channel
  (`src/models/fusion.py::behavior_fusion_group`), so air mass and every VE
  channel vote as one `behavior:airflow` voice
  (`tests/test_fusion.py::test_one_measurement_is_one_voice_across_its_derived_channels`).
  The behavior-side evidence is linked into D03.20. **Owed (data):** the
  decision re-run with a < 76 kPa drive on the calibration in force, the same
  data D03.58 waits on.
- [~] **D03.60 Behavior models: the deployed inventory.** *From
  `archive/B03-behavior-lifecycle-and-refit.md` B03.1.* Local side done
  2026-09-21: `tools/inventory_behavior_artifacts.py` over
  `src/analysis/behavior_inventory.py` records the artifact (hash, per-model
  identity), policy, criteria, tune registry, overrides, every IF and sequence
  manifest with the `deviation:*` columns it consumes (none do), and a deployed
  comparison that reads `unknown` without a saved `/models/behavior` snapshot
  (consumers in `data/analyses/2026-09-21-behavior-artifact-inventory.md`).
  B03.9 hash-checked the artifact on the Pi on 2026-09-24. **Owed (Pi):** read
  `/status.vehicle_state`, save the deployed `/models/behavior` snapshot, and
  run the tool with `--deployed-snapshot`; do it with D03.62.
- [~] **D03.61 Behavior models: candidates for the data-limited recipes.**
  *From `archive/B03-behavior-lifecycle-and-refit.md` B03.6.* Candidates made
  2026-09-23 (`scripts/refit_behavior_models.py` over 53 manifest paths into
  `data/nissan-xterra-2008/_candidates/2026-09-23/`): 10 recipes fitted with
  B02 evidence on untouched drives, every verdict `failed` (reasons in
  `data/analyses/2026-09-23-behavior-refit-validation-report.md` §2). Found and
  fixed: exposure counted 11 h of a parked truck (`EVALUATOR_VERSION` 3). The set
  went out as a diagnostic-only refresh (B03.9). **Owed (data-limited; criteria
  unchanged, missing data is a partial task):** warmup recipes (2 cold starts
  then, `min_sessions` 3), cam (commanded cam never polled), TPS (244 rows
  against 300), manifold VE (no MAP), LTFT balance (LTFT constantly 0.0 then;
  learning has resumed, `70c1999e`, so re-check). **Accept:** each refits and
  gets a B02 verdict on untouched held-out drives; a failed candidate never
  overwrites the baseline.
- [~] **D03.62 Behavior models: confirm the unscored-row fix on the Pi.** *From
  `archive/B03-behavior-lifecycle-and-refit.md` B03.9.* Operator decision
  2026-09-23: promote the candidate set as a **diagnostic-only refresh**, though
  the qualified set is empty. Deployed 2026-09-24 16:03Z (build `2971d79`,
  `scripts/restart_canary.sh`, no capture): hash `816c150b…e565` matched; 12
  models, all `diagnostic_only` with reasons, seen live ~16:07Z. Desk part
  landed 2026-09-25: `BehaviorObservation` nulls `severity` on an unscored tick
  and `not_scored` names the kind (`off_map`, `actual_stale`, …), in dashboard
  and MCP (`tests/test_behavior_live.py`, `BehaviorModelsPanel.test.ts`).
  Rollback: `data/nissan-xterra-2008/_prior/2026-09-24T024700/` (laptop) or git
  `2971d79^`. **Owed (Pi):** pull the build carrying the 09-25 change and confirm
  a live off-map row reads `severity: null` with its `not_scored` kind.
- [ ] **D03.63 A second trip inside one session gets no digest and no reason.**
  *From the 2026-11-08 drive (leg 3, pulled 2026-11-10).*
  `_capture-ltft-gate-2026-11-08T201601` covered two trips. The first trip
  ended at 21:02:28Z after 300 s stationary, and its `digest.json` was written.
  The second trip (21:02–21:59Z) left nothing behind. `run_trip_end_digest`
  (`src/control/trip_end.py`) returns `None` when `read_digest(session_dir)`
  already exists, and writes no `digest_skipped.json`. A one-hour leg of
  driving is therefore indistinguishable from one that never ended. Whole-drive
  captures under `capture/autostart` make long multi-trip sessions the norm.
  **Accept:** every detected trip end produces either a digest for that trip
  or a skip record naming the reason. Fix it in the shared function, keyed by
  trip rather than by session directory. A test drives two trip ends through
  one session and checks both are on disk.
- [~] **D03.64 Scoring a long capture OOM-kills the daemon.** *From the
  2026-11-10 boot.*
  - **What happened.** At 16:59:58Z the daemon tried to finalize the
    interrupted leg 3 (`_capture-ltft-gate-2026-11-08T201601`: 108 min,
    167 MB, 1,299 files).
    - `recompute_quality_off_loop`'s spawn child reached 6.19 GB anon RSS, and
      the memory-cgroup OOM killer killed it (`MemoryMax` 6 GiB).
    - With `OOMPolicy=stop`, systemd then failed the whole unit
      (`Failed with result 'oom-kill'`, 6G memory peak). It restarted at
      17:00:30Z, with the engine running. About 30 s of collection was lost.
    - Leg 3 never got a `capture_quality.json`.
  - **Laptop reproduction.** `recompute_capture_quality` on a copy of leg 3
    peaked at 9.6 GB in 47 s. On a 15 MB `tuning` capture it peaked at 1.2 GB.
    Memory runs at roughly 60 MB per MB on disk.
  - **Cause.** `persisted_quality_inputs` calls `read_acquisition_frame(session)`.
    That reads every row, with CAN decoded on read. On leg 3 the rows were
    80 % `can`, 7.7 % `sensor` (fuel pressure at ~500 Hz) and 6.7 % `gps`.
    Only 5.2 % were the `obd` rows the profile's requests and tuples are made
    of.
  - **The normal stop takes the same path** (`finalize_capture` →
    `recompute_quality_off_loop`). Any capture longer than about 50–55 min at
    `tuning` rates therefore takes the daemon down when it stops. Whole-drive
    `capture/autostart` captures make that the usual case. On 2026-11-10 the
    drive is split by hand every ~40 min until this lands.
  - **Accept:**
    - The recompute reads only the sources and channels it scores, with no
      decode on read.
    - Its peak memory is measured on leg 3's copy and is bounded by the
      profile's channel count, not by the session's CAN volume.
    - The child caps its own `RLIMIT_AS`, as the digest child does
      (`trip_end.limit_address_space`). A refusal then writes a quality
      record naming the reason, instead of the kernel killing the service.
    - A regression test asserts the CAN rows are never loaded.
  - **Done 2026-11-10 (laptop, offline; nothing deployed).** Measured with
    `recompute_capture_quality` on copies of the two sessions, same machine
    and code path before and after:
    - leg 3 copy (167 MB, 9.21 M rows): **9,544 MB peak RSS / 41.4 s ->
      841-905 MB / 9.9-11.8 s** in process; a real `spawn` child under the
      default 4 GiB cap peaked at 887-913 MB / 9.7-12.8 s (child `VmPeak`
      2.55 GB, of which ~2.4 GB is what pandas and pyarrow map with nothing
      read). The 41 MB `_capture-tuning-2026-11-10T170119` copy: 2,810 MB /
      6.9 s -> 406-410 MB / 2.2-3.2 s. Memory now follows the capture's
      `obd` rows (477,009 of 9.21 M on leg 3, ~1.5 KB each), not its CAN volume.
    - **Verdict unchanged.** The full `CaptureQuality.to_dict()` (channels,
      tuples, interval, failures, notes, losses, verdict `fail` on both) saved
      before the change and compared after: identical on both sessions except
      `persistence.rows_on_disk` and `rows_on_disk_at_recompute` (see the
      owed item below). A real child's result equals the in-process one.
    - [x] *Reads only what it scores, no decode on read.*
      `capture_coverage.read_scored_rows` = `read_acquisition_frame(session,
      decoded="as_recorded", filters=[("source", "==", "obd")],
      columns=SCORED_COLUMNS)`; `read_acquisition_frame` now carries
      `filters`/`columns` down to the parquet read (refused for a frame).
      `persisted_quality_inputs` has no other read of the session, and every
      caller (stop, live, boot, both routes, `validation.evidence`) goes
      through it. `rows_on_disk` is the footers' row count; the `ended_at`
      fallback (last row of *any* source, on the acquisition clock) is
      `last_row_seconds_outside_scored`, an Arrow scan of three columns a
      chunk, asked only when no end was recorded.
    - [x] *Peak memory measured on leg 3's copy* -- the numbers above.
    - [x] *The child caps its own `RLIMIT_AS`.* `capture_finalize.recompute_under_limit`
      puts `child_pool.limit_address_space` (moved down from `trip_end`, which
      re-exports it) on the child before it reads anything; default
      `digest.derived_address_space_bytes()` (RAM-derived, 1-4 GiB). A
      `MemoryError` in the child is a `CaptureQualityUnavailableError` naming
      the cap. On the leg-3 copy: a 1,500 MiB and a 400 MiB cap each ended in
      "ran out of memory under its N MiB address-space cap" in 1-2 s; the
      default scored it. A thread pool is never capped (the limit would stay
      on the daemon or the test worker). Two silent-empty paths found and
      closed on the way: `session_files.session_footer_span` swallowed a
      `MemoryError` as "corrupt chunk" (a capped child then reported "no
      telemetry rows persisted"), and a lazy `pyarrow.parquet` import failing
      under the cap read as an unreadable chunk (now imported before the cap).
    - [x] *Boot-time finalize survives a refusal.*
      `capture_finalize.finalize_interrupted_capture` (never raises, logs the
      reason) replaces the inline block in `daemon/runtime.py`;
      `test_the_daemon_boots_past_an_interrupted_capture_it_could_not_score`
      runs `run_daemon` over an unclosed capture whose score is refused and
      waits for `collector_tasks_created`.
    - [x] *Regression test: CAN rows never loaded.*
      `tests/test_capture_quality_reads.py::test_scoring_a_capture_never_loads_its_can_rows`
      (spy on every `pd.read_parquet`: only `obd` rows, no `payload` or
      `value_number`, `decode_on_read` never called; the same spy sees
      can/sensor/gps/payload on a whole read). Identity against the old
      whole-session read: `test_the_scored_read_gives_the_whole_reads_inputs`
      (obd/can/kline last row, with and without a given end). Refusal:
      `test_a_child_that_runs_out_of_its_cap_is_a_named_refusal` (a real child,
      a real `RLIMIT_AS`, a real `MemoryError`). 22 tests; 731 passed over the
      related files (capture, finalize, digest, daemon, child pool, layering).
    - **Owed.** (1) The Accept's "a refusal then writes a quality record
      naming the reason": not done. A refusal is the named
      `CaptureQualityUnavailableError` (503 on the two routes, logged at stop
      and at boot), and the capture is left with no `capture_quality.json`,
      which every reader takes as unknown. A record would need a schema (a
      new clause or a `capture_quality_skipped.json` like `digest_skipped.json`);
      that is a design decision, not a one-line change. (2) `rows_on_disk` now
      counts stored rows (leg 3: 12,696,668 -> 9,213,483; the tuning copy:
      3,067,924 -> 2,167,360). The old figure was `len` of a frame with the
      CAN decoded on read, i.e. it counted rows synthesised from the frames, so
      it exceeded what is on disk and could hide a real `persistence_loss`
      (`rows_on_disk < rows_written`); `docs/capture-quality.md` always defined
      it as the persisted count. Needs the operator's nod that the corrected
      figure replaces the old one. (3) **Pi, 2026-11-10 18:21Z (commit `0df45715`, deployed by
      bundle, restarted 18:18Z with the truck parked):** `GET
      /control/capture/quality/_capture-ltft-gate-2026-11-08T201601` answered
      in 17 s with verdict `fail` (`rate_shortfall`, `acquisition_gap`,
      `unknown_loss_counters`) and `rows_on_disk` 9,213,483, as on the laptop.
      The daemon's cgroup `MemoryPeak` was 1.48 GB against the 6 GiB limit,
      `NRestarts` 0. This is the capture whose boot-time score reached 6.19 GB
      and stopped the unit that morning. Still to see: a capture of 50 minutes
      or more stopping normally on the new build (next drive).
- [x] **D03.65 A slow-tier plant-validation pin was red since N02.8.** *Found
  2026-11-11 while recording ZS44A-20261110; fixed the same day.*
  - **What.** `tests/test_plant_validation_ecu_outputs.py::test_the_wot_null_control_does_not_pass_on_pre_flash_drives_outside_its_design`
    expected `_capture-tuning-2026-09-23T164007` to have no eligible read
    (`(post_reads, post_eligible, n_pairs) == (1353, 0, 0)`, `filtered`). It
    got `(1353, 271, 0)`.
  - **Cause, confirmed.** The test passes on `cf4422c5^` and fails on
    `cf4422c5` (N02.8, throttle route per read; run in a temporary worktree).
    N02.8's own acceptance is that this drive yields DID-route samples, so
    271 eligible reads is the intended result. The test is in the slow tier
    and needs the private sessions and ROMs, so no gate ran it.
  - **Fix.** The pin and docstring now state what the control measures after
    N02.8: both drives end `insufficient_evidence` / `prediction_unresolvable`
    with 0 resolvable pairs (213 BFS and 106 phi pairs before the rule on
    164007). The verdict, `passed == []`, is unchanged. The plant-validation,
    fuel-comp and sim-design files pass with their slow tests (229 passed).
  - **The whole slow tier, run once afterwards (2026-11-11, 591 tests):** two
    more reds, both from recording ZS44A-20261110 the same evening, not from
    N02.8. `test_brain_routes` wanted `program_current` true: the new image
    was not in `ecu_application_model.yaml`'s `images` (added; its program
    hash equals the model's). `test_detector_evaluation_golden[full]` pins a
    digest of log lines, and one warning lists the recorded revisions (one
    digest changed in the fixture; a full `--write` also rewrites 89 floats by
    up to 3e-14, which was left out). A flash record has to run these too.
- [~] **D03.66 `POST /control/obd_request` cannot address a one-byte local
  identifier.** *Found 2026-11-11 reading the BCM's TPMS registration
  (`data/analyses/2026-11-11-tpms-bcm-read.md`).* The endpoint always encodes a
  two-byte identifier after the service byte, so a KWP-style
  ReadDataByLocalIdentifier (`21 1F` on the BCM, 0x745/0x765) cannot be sent;
  the read was done with `cansend` from a shell on the Pi instead. The ECM's
  Mode 22 (two-byte) path is unaffected.
  **Accept:** a caller can send a service byte followed by a one-byte
  identifier (and by an arbitrary byte string, for services that take no
  identifier), through the HTTP route and the MCP tool; the default stays the
  two-byte encoding, so existing callers are unchanged; docs and tests cover
  both widths; a test pins the exact bytes on the wire for `21 1F` to 0x745.
  The endpoint keeps no service allowlist (AGENTS.md), and nothing here
  changes which services it will send.
  - **Diagnosis corrected (2026-11-11, laptop).** The encoding was never
    always two bytes: `isotp.build_request_payload` infers the width from the
    value (one byte up to 0xFF, two above), so `21 1F` already went out as
    `02 21 1F` (W02.1 sent `21 10` this way). What actually blocked the BCM
    was the reply: the client's window is 0x7E8–0x7EF and a physical header
    is answered only by `header + 8` (0x74D), so 0x765 was dropped as
    silence, and flow control for a multi-frame reply went to
    `responder − 8` (0x75D). The inferred width also could not send a
    two-byte DID below 0x100 (`22 05`, never `22 00 05`).
  - **Done.** New body fields, through the route, `ObdRequestRequest` and the
    MCP tool: `identifier_bytes` (1/2, fixes the width and checks the echo,
    `identifier_echoed` in the reply), `data_hex` (exact bytes after the
    service byte, ≤ 6), `response_id` (declared reply id; a window of one).
    `request_hex` reports what went on the wire. With none of them the request
    is the old inferred encoding, byte for byte — the "default stays two-byte"
    wording above would have changed `01 0C` to `01 00 0C`, so the default is
    the *existing* encoding. Validation is `control.obd_request.encode_request_body`,
    called by the schema's validator: too-wide identifier, width without
    identifier, `data_hex` beside an identifier, bad hex, > 6 bytes are 422s
    naming the problem, never truncated. `isotp.await_response` sends flow
    control to the request id when the caller declared a single reply id
    (identical wherever the pairing is `+8`). No service filter added or
    changed. Docs: `docs/headless-api.md` "Ad-hoc OBD/UDS requests" and the
    control table row, `docs/mcp-server.md` `obd_request` row.
  - **Evidence.** `tests/test_control_obd_request.py`:
    `test_kwp_local_identifier_to_the_bcm_puts_the_exact_bytes_on_the_wire`
    (request on 0x745 is exactly `02 21 1F 00 00 00 00 00` = the inferred
    path's bytes; FC on 0x745, none on 0x75D; 17-byte multi-frame reply from
    0x765 reassembled, echo stripped),
    `test_a_two_byte_did_below_0x100_is_sent_as_two_bytes` (`03 22 00 05`),
    `test_data_hex_is_the_whole_argument_after_the_service_byte`
    (`03 17 FF 00`), `test_the_default_encoding_is_unchanged` (4 cases:
    `01 0C`, `22 11 01`, `21 10`, `03`),
    `test_a_body_that_does_not_fit_is_refused_by_name` (10 cases),
    `test_encode_request_body_refuses_rather_than_truncates`,
    `test_without_response_id_a_reply_on_0x765_is_never_heard`, and the
    original seven tests unchanged; `tests/test_isotp.py`
    `test_flow_control_goes_to_the_declared_physical_request_id`,
    `test_flow_control_is_unchanged_where_the_pairing_is_plus_eight`,
    `test_body_request_payload_pads_like_the_inferred_form`. Run with
    `test_can_obd_client.py`, `test_isotp_continuation_timeout.py`,
    `test_decode_isotp.py`, `test_write_tool_schema_contract.py`,
    `test_route_table_snapshot.py`, `test_memory_window_burst.py`: 176 passed
    (the one red, `test_mcp_tool_snapshot`, was another session's
    `get_session_digest` description; `obd_request`'s entry matches).
  - **Owed (truck, key on):** the live BCM read through the endpoint —
    `10 C0`, then `21 1F` with `identifier_bytes: 1`, `header: 0x745`,
    `response_id: 0x765`, then `10 81` — and its ids compared with the
    2026-11-11 `cansend` read. Each step is its own HTTP request on a fresh
    bus handle with no TesterPresent between them, so the same read also
    shows whether the BCM's 0xC0 session survives the gaps (if not, a
    multi-step form is owed).
  - **Owed (laptop, small):** `tools/_bus_tool.response_window` still assumes
    reply = request + 8 for a physical header, so `--header 745` listens on
    0x74D and misses the BCM's 0x765 the same way the endpoint did. Give the
    bus tools a `--response-id` like the endpoint's.
- [~] **D03.67 `GET /control/power` says `on_battery: true` with input
  present.** *Seen 2026-11-10 17:00Z (SoC 12.9 %, charging, input present)
  and 2026-11-12 04:07Z (SoC 94.5 %, input present at 11.5 V, key on, engine
  off).* `scripts/ups_shutdown_watch.py` `on_battery()` is true when the SoC
  falls faster than `UPS_MIN_DISCHARGE_RATE` **or** sits at or below
  `UPS_CRITICAL_SOC` (15 %), whatever the PLD line says. So the flag means
  "battery draining or critically low", and the payload reads as a
  contradiction (`input_lost: false`, `on_battery: true`) with no reason
  given. The 11-10 case is the critical-SoC clause firing on a battery that
  was being charged. Related, from the same notes: the policy between the
  critical-SoC floor and an armed keep-alive is not written down (which wins
  when both apply).
  **Accept:** the status record and `GET /control/power` say which condition
  holds, in fields that cannot contradict each other (for example the
  discharge state and the critical-SoC state reported separately from input
  loss, with the shutdown countdown's own reason), the MCP tool and the
  dashboard power panel show the same, and the keep-alive vs critical-SoC
  rule is stated in the watchdog's docstring and `docs/headless-api.md` and
  pinned by a test. Deploying the watchdog is a Pi step (it runs on the
  system interpreter under its own unit).
  *Done 2026-11-12 (laptop):* the heartbeat and payload now carry `input_lost`
  (PLD only), `discharging` + `discharge_rate_pct_per_hour` (measured slope),
  `soc_critical` (+ the two thresholds), `shutdown_state`
  (`disarmed`/`input_present`/`held_external_power`/`counting_down`/
  `held_keepalive`) and `countdown_reason`; `on_battery` stays as a legacy
  alias that is never true beside `input_lost: false`. Readers swept
  (`grep -rn on_battery`: only the dashboard panel, its types/tests/e2e fixture
  and the MCP docstring read it; no Python consumer); the panel now shows Input
  power, Battery (with the rate) and SoC-critical separately and the countdown's
  reason, falling back to `on_battery` for a pre-D03.67 heartbeat. MCP tool
  description, `docs/headless-api.md` ("Power / UPS status") and
  `docs/mcp-server.md` updated. **Keep-alive vs critical SoC (the standing
  rule, unchanged):** the loop checks the hold before the debouncer and resets
  the clock, so a keep-alive wins at any SoC; stated in the watchdog docstring
  and headless-api, pinned by `test_keepalive_beats_critical_soc_in_the_main_loop`
  (drives `main()` with a fake GPIO/gauge/clock) beside
  `test_critical_soc_without_keepalive_powers_off_in_the_main_loop` and
  `test_critical_soc_with_input_present_never_counts_in_the_main_loop`.
  Evidence: `pytest tests/test_ups_shutdown.py tests/test_dashboard_ups.py
  tests/test_control_power.py tests/test_mcp_tools.py
  tests/test_mcp_tool_snapshot.py` green; `pnpm typecheck`, `pnpm lint`,
  vitest `PowerPanel.test.ts` (11) green.
  **Owed (Pi):** deploy the watchdog (it runs on the system interpreter under
  its own unit): `install_pi.sh`, or copy the script and restart `ups-shutdown`
  under sudo. Until then the Pi's heartbeat has none of the new fields and the
  daemon passes the old `on_battery` through. Not done on the laptop by design.
  **Operator decision (not taken):** should a critical-SoC floor override a
  keep-alive hold to protect the cells? Today it does not (an armed hold keeps
  the Pi up at 5 % SoC with input lost until the cells cut out). Options: (a)
  keep as is (the operator asked for the hold, in those words); (b) a hard
  floor, e.g. `UPS_CRITICAL_HARD_SOC` (~5 %), below which input loss powers off
  regardless of a hold; (c) cap the hold's length instead. Recommendation: (b)
  with the floor well under 15 %, since a deep discharge is the one outcome the
  hold cannot undo. Behaviour left unchanged pending a decision.
- [~] **D03.68 Broadcast `ECM_COOLANT_TEMP` once read 94/118 °C beside PID
  0x05's 49/78 °C (2026-11-10 drive log, "Loose ends").** *Checked
  2026-11-12 on the laptop.* The stored decode is right: on both 2026-11-12
  captures (33–95 °C), 0x551 byte 0 − 40, 0x233 byte 0 − 50 and 0x23D byte 7 −
  50 each equal PID 0x05 on every paired read (1,147–1,220 pairs per id on the
  warm drive, 423–440 on the warm-up; per-pair difference p5/p50/p95
  0.0/0.0/0.0; `~/canary-data/analysis-scripts/2026-11-12/coolant_bytes.py`).
  118 at 78 °C is exactly 0x551's raw byte (T + 40), so the odd reading was
  most likely a presentation path showing a raw byte, not the binding.
  **Owed (Pi, engine running):** read `/telemetry/latest` (and the MCP
  `get_recent_telemetry`) for `ECM_COOLANT_TEMP` beside `COOLANT_TEMP`; if
  they differ, find the path that skips the offset. No binding change.
## Opened 2026-09-23 on the drive

- [x] **D03.28 `GET /anomalies` parses the whole anomaly history in the collecting
  daemon, and two requests degraded collection mid-drive.** On 2026-09-23 at
  14:53:15Z and ~14:53:40Z, during a `tuning` capture, one curl and one MCP
  `get_recent_anomalies` call, both with `since=14:20Z`, timed out at 20 s. The Pi
  poller shows the cost landing on collection. `/status` latency went from ~20 ms
  to 1.74 s, the storage queue spiked to 567, inference missed 3 ticks (2 → 5,
  "overran 1 deadline" ×3), and achieved reads/s fell 55 → 45. It recovered by
  14:54:12Z with no dropped records. Cause: `read_anomalies_since` calls
  `read_events_dir`, which parses every `anomalies-*.jsonl` (37 files, 169 MB back
  to May) into events *before* applying `since`, inside the daemon process. The
  same reader backs `/control/tuning/estimate` and the companion route.
  - [x] Skip whole daily files that cannot hold an event at or after `since`. The
    file date is the UTC date: `anomalies-2026-09-19.jsonl` was last written at
    00:17Z on 09-19. Keep a one-day margin rather than trusting the boundary.
    *Done in the shared layer:* `event_logger.read_events_dir(since=, limit=,
    keep=)` skips files dated > `FILE_DATE_MARGIN` (1 day) before `since`,
    reads undated names always; `read_anomalies_since` (all three callers:
    `/anomalies`, `/control/tuning/estimate`, companion) passes every bound
    down. `test_since_keeps_exactly_one_day_of_margin`,
    `test_unparseable_filename_is_always_read`,
    `test_since_still_filters_events_inside_kept_files`.
  - [x] Decide the no-`since` behaviour: the MCP tool's docstring says the daemon
    returns "today, typically", but the route reads everything. Either default the
    route to a bounded window or correct the docstring, and name which.
    *Corrected the docstring, no default window:* `limit` without `since` now
    reads days newest-first and stops (plus one day of margin); the MCP tool
    forwards `limit`/`source`/`mode` to the daemon. Neither bound = full read,
    unchanged. `test_limit_without_since_matches_full_read`,
    `test_limit_counts_matching_events_through_the_route_reader`,
    `test_get_recent_anomalies_forwards_limit_source_and_mode`.
  - [x] A test that proves a request with a recent `since` never opens an old file.
    `tests/test_anomaly_log_bounds.py::test_recent_since_never_opens_an_older_daily_file`
    (spies the per-file reader: only 09-22 and 09-23 opened for since=09-23T14:20Z)
    and `test_recent_since_survives_an_unreadable_old_file`.
  - [x] Verify on the Pi while collecting: a `since`-bounded request answers in
    < 1 s and the poller shows no missed ticks around it.
    **Verified 2026-09-24 (Pi at `2971d79`, `engine_running`):** in `default`
    at ~16:07Z, 0.064–0.094 s with missed ticks 2 → 2; during a `tuning`
    capture (`_capture-tuning-2026-09-24T162444`) at 16:25Z, 0.075–0.082 s with
    missed ticks 4 → 4 (ticks 1289 → 1295).
    *Idle half, 2026-09-23 17:48Z (Pi at `0d4b32b`, deployed by bundle,
    `vehicle_state` `no_data`, bench):* three `GET /anomalies?since=<now-30 min>`
    answered 200 in 0.048–0.049 s; inference 85 ticks scheduled / 85 completed /
    1 overrun (startup model load, before the requests). **Still owed:** the
    same with the engine running and a capture recording.

- [x] **D03.29 Trip end is detected from GPS speed alone, so a drive without a fix
  never ends a trip and the trip-end digest never runs.** *(Fix landed; the
  no-fix investigation remains; it needs the truck.)* On 2026-09-23 the GPS
  reported `fix_quality` 0 on all 3,278 samples of a 27-min drive (14:29–14:56Z),
  then had a fix (8 satellites, HDOP 1.16) parked at 15:05Z. `drain_gps` feeds
  `GpsTracker.observe`, which only acts on `gps:speed` rows, so no trip started,
  none ended, and no `digest.json` or `digest_skipped.json` was written. Road speed
  was on the bus the whole time: `decoded:WHEEL_SPEED_6F7_A` is persisted with a
  pinned km/h scale. Trip distance is GPS-integrated too, so it was also lost.
  - [x] Detect the moving → stationary edge from a vehicle speed source that does
    not depend on sky view. Keep it vehicle-agnostic: take the source from the
    profile/bindings (Mode 01 PID 0x0D, or a binding declared as road speed), not
    a hard-coded Xterra CAN id. GPS stays a fallback, not the only input.
    **Done for every install with a tracker:** `Signal.quantity: road_speed` in
    `src/decode/bindings.py` (schema refuses an unscaled/non-km/h declaration and
    two declarations); `data/bindings.yaml` declares it on `WHEEL_SPEED_6F7_A` (the
    field pinned 1:1 to `obd:SPEED` in the ground-truth fixture). `GpsTracker`
    reads fresh vehicle speed (`obd:SPEED`, or the declared binding decoded from
    its raw frame, decimated to 2 Hz in `GpsTracker.accepts`) before GPS; silence
    after a stop (key-off) still ends the trip. No CAN id in shared code.
    The edge lands on the session the stop was seen in: `run_gps_sink` follows
    `active_session_path` before every record and idle tick and tells the tracker
    (`GpsTracker.enter_session`); the tracker stamps the stop with its session and
    `TripEnd.session` routes the digest there, whenever the edge fires (verifier
    repro: stop, total silence, key cycle, one 0 km/h frame — pre-fix semantics
    digested the *new* session, now the first). A rotation no longer ends a trip
    (see the multi-session sub-box below). An idle queue calls `GpsTracker.tick`, so key-off silence ends a
    stopped trip on time with no rows at all (GPS-less, or a receiver that emits
    nothing). The tick never compares `time.time()` with collector timestamps: its
    time is the last live record's timestamp plus *monotonic* seconds since that
    record arrived, so a wall-clock step (chrony, a GPS time fix) cannot end or
    split a trip (`::test_a_record_clock_far_from_the_wall_clock_cannot_end_a_trip_by_tick`;
    `::test_key_off_silence_ends_the_trip_on_the_idle_tick`). Residual: a trip
    end still rests on collector timestamps between *records*, so a ≥300 s step
    inside the record stream while stopped could end a trip early. A digest
    computed later clears any refusal, but an early digest makes that session's
    real trip end return "already handled". Replay output (`_replay-*`) suspends the live
    trip and resumes it after; nothing is reported for a replay directory, and a
    replay cut off mid-drive leaves no "moving" state behind for the live
    session. Tests: `::test_a_stop_then_silence_is_digested_on_its_own_session_not_the_next`,
    `::test_a_stopped_trip_ends_on_the_session_its_stop_was_seen_in`,
    `::test_close_session_leaves_a_moving_trip_open`,
    `::test_tick_ends_a_stopped_trip_through_silence`,
    `::test_a_replay_is_isolated_from_the_live_trip`,
    `::test_the_sink_reports_nothing_for_a_replay_and_resumes_the_live_trip`. Tests:
    `tests/test_trip_speed_sources.py::test_wheel_speed_with_no_gps_fix_ends_the_trip`,
    `::test_mode01_vehicle_speed_alone_ends_the_trip`,
    `::test_fresh_vehicle_speed_outranks_gps`,
    `::test_gps_still_ends_a_trip_when_there_is_no_vehicle_speed`,
    `::test_the_repo_declares_exactly_one_road_speed_binding_with_a_pinned_scale`,
    `::test_a_road_speed_declaration_that_cannot_read_km_h_is_refused`,
    `::test_two_bindings_cannot_both_be_the_road_speed`. Offline on stored drives
    with every `gps:speed` row removed: `2026-09-18T134345` ends at +1051 s and
    `2026-09-18T160921` at +880 s / +1673 s on `can:WHEEL_SPEED_6F7_A`, the same
    edges as with GPS present; the HEAD tracker sees 0 edges on the test's no-fix
    drive. The tracker is now built whether or not `hardware.gps.enabled`
    (`src/daemon/components.py`;
    `tests/test_daemon.py::test_build_components_builds_a_trip_tracker_without_gps`),
    and `/status` reports `gps`/`trip` as absent on a GPS-less install rather than
    a 0 km GPS odometer
    (`tests/test_dashboard_api.py::test_build_status_hides_gps_and_trip_when_gps_is_disabled`).
    Trip distance deliberately stays GPS-only
    (`::test_vehicle_speed_never_reaches_the_gps_odometer`): the 0x6F7 binding's
    own notes forbid absolute distance from it (~8 % under-read vs GPS), and
    `obd:SPEED` shares the under-read.
  - [x] A drive that ends with no usable speed source writes `digest_skipped.json`
    naming that cause, rather than nothing. An empty result says which kind of
    empty it is. New reason `no_speed_source` (`src/analysis/digest.py`), written by
    `src.control.trip_end.record_trip_end_unobservable` in two cases: speed lost
    while moving for the whole trip-end window, and a session that rotates away
    (key cycle, capture boundary) with no trip edge and a blind stretch that long
    — the 2026-09-23 shape. *Blind* means no usable speed from session start, or
    none since a reading of motion; silence after a stationary reading is "no trip
    happened", not blind, and idle ticks are not records. So a quiet driveway or
    garage session is never reported as "could not see"
    (`::test_a_quiet_session_that_never_moved_is_not_reported_as_blind`,
    `::test_close_session_after_ticks_on_a_parked_session_is_none`,
    `::test_silence_after_motion_is_still_blind_at_rotation`). The detail inventories every source (`obd:SPEED`,
    `gps:speed`, the declared binding or "no binding declares quantity:
    road_speed", fix-quality rows with a fix). Never replaces a digest or a more
    specific refusal, never writes into `_replay-*`; a later digest clears it.
    Tests: `::test_speed_lost_while_moving_ends_the_trip_as_unobservable`,
    `::test_a_session_blind_for_the_window_closes_as_unobservable`,
    `::test_a_session_whose_trip_ended_or_that_was_watched_closes_quietly`,
    `::test_record_trip_end_unobservable_writes_the_refusal_once`,
    `::test_record_trip_end_unobservable_never_shadows_a_digest_or_a_replay`,
    `::test_a_rotated_session_nobody_could_see_reports_no_speed_source` (real
    drain + writer rotation).
  - [x] Test: a session with wheel speed and no GPS fix ends a trip and runs the
    digest.
    `tests/test_trip_speed_sources.py::test_a_session_with_wheel_speed_and_no_gps_fix_ends_a_trip_and_runs_the_digest`
    — raw road-speed frames plus `fix_quality` 0 rows through the real
    `drain_queue_with_broadcast` and `ParquetTelemetryWriter`; `on_trip_ended`
    fires once for the session, `handle_trip_end_off_loop` writes `digest.json`,
    no `digest_skipped.json`.
  - [x] A trip can span several sessions (a capture started or stopped mid-drive,
    a daemon restart), and the digest must land on the drive, never on a parked
    session the trip never touched. Live finding (canary-17, 2026-09-23): with a
    tuning capture open through the drive, the GPS edge fired at 16:57:33Z, 300 s
    after the stop, and digested `2026-09-23T165316`, the 257 s parked `default`
    session the writer rotated to when the capture stopped at ~16:53:16; the drive
    in `_capture-tuning-2026-09-23T164007` got no digest.
    **Decision: digest the session the trip ended in, and name every session it
    spanned.** The tracker records each session it sees the trip moving in, plus
    the stop's session; `TripEnd.session` is where the stop was seen (or where the
    vehicle was last seen moving, if speed was lost). `handle_trip_end_off_loop(...,
    trip=)` writes a `trip` block into `digest.json` with `started_at`, `stopped_at`,
    `speed_source`, `end_session`, and `sessions`, captures included, ordered by
    `src.storage.sessions.session_started_at`, never by name. `coverage` is
    `end_session_only` when the trip spanned more than one session: the digest's
    statistics cover the end session only, and the block keeps the rest from being
    silently dropped. A multi-session digest (statistics over every spanned
    session) was not built; it would need `compute_digest` to load several
    sessions. The shutdown fallback is trip-aware too (`runtime.shutdown_trip_target`):
    an open trip is digested on its own session with its block. With no open
    trip, a session that showed no motion gets no fallback digest once the
    tracker has read speed at any point since start (process-lifetime, replays
    excluded). That covers the no-fix variant the round-3 verifier found:
    edge fired on wheel speed in the capture, then a parked session with only
    no-fix `fix_quality` rows, or nothing at all, then shutdown. Before the fix
    that session got a near-empty `digest.json`, the live finding again. It
    gets no refusal either: once speed has been readable since start, a session
    that never produced a usable reading is a key-off parked session ("no trip
    happened"), not a blind one, so `close_session` files nothing for it at a
    rotation or at shutdown (`::test_a_key_off_parked_session_is_no_trip_not_a_blind_one`).
    A `no_speed_source` refusal stays for a session blind after motion and for
    an install that has never read speed. Only an install that has
    never read speed keeps the old digest-the-active-session behaviour. `_capture-*` directories
    digest normally (`is_session_dir_name` includes captures).
    Tests: `::test_a_capture_stopped_after_the_stop_digests_the_capture_not_the_parked_session`
    (the live sequence through `run_gps_sink`: `ended == [capture]`),
    `::test_a_trip_spanning_a_capture_boundary_names_every_session`,
    `::test_the_trip_block_orders_sessions_by_start_time_not_name`,
    `::test_a_capture_session_digests_with_its_trip_block` (real digest on a
    `_capture-tuning-*` dir, `trip` block written),
    `::test_the_shutdown_fallback_digests_the_open_trip_not_the_active_session`,
    `::test_the_shutdown_fallback_skips_a_parked_session_the_trip_never_touched`,
    `::test_the_shutdown_fallback_keeps_digesting_what_it_cannot_read`,
    `::test_the_shutdown_fallback_skips_a_parked_session_with_no_fix_after_the_trip_ended`,
    `::test_the_shutdown_fallback_skips_a_silent_parked_session_after_the_trip_ended`,
    `::test_the_shutdown_fallback_still_digests_a_session_that_moved`,
    `::test_replayed_speed_does_not_make_an_install_readable`.
    Known limit: a session holds one `digest.json`, so a second trip ending in a
    session that already has one is still skipped as "already handled".
  - [x] Unexplained, separate from the fix: why the receiver held no fix for the
    whole drive after being powered all night. Check the next drive's first
    minutes; if it repeats, it is a hardware or antenna question for the operator.
    *2026-10-04: did not repeat.* That day's three drives all had a 3D fix within
    the first minutes (19:45:52Z TPV, 2 min after a boot; 18 satellites 1 min
    after the 20:27Z boot). Cause still unexplained. Separately, that day found
    GPS dark for unrelated reasons: no receiver attached after the 2026-10-03
    gpsd upgrade, and 14 s NMEA lag at 9600 baud, both fixed (H03.3/H03.4).
    The capture-digest evidence D05.2 owed landed too: see D05.2.
    *2026-09-24 16:04Z key-on (Pi up since 04:55Z):* `/status.gps` showed a fix
    within ~1 min of the engine starting (fix_quality 1, 10 satellites, HDOP
    1.07, 2,014 m). One good start; the 09-23 no-fix drive stays unexplained.
- [ ] **`tuning` memory windows gap for seconds right after a daemon start
  (2026-10-27).** `_capture-tuning-2026-10-27T160919` started 29 s after a
  daemon restart and stopped with `fail`: `rate_shortfall` (the 4 Hz batch at
  its planned 2–3.4 Hz, which is expected) and `acquisition_gap`.
  `mem:ECM_TARGET_EQUIV_RATIO` (fuel_target window) gapped five times, 2.5–6.5 s
  each, at 15.7–91.7 s into the capture. The spark window gapped once, 5.0 s at
  214.7 s. Zero socket, storage and controller losses. The 10-26 capture of the
  same revision (512 s) recorded the windows at 2.0 Hz with p95 0.54 s and no
  gaps, so this is new. Owed: check whether it repeats on a capture started
  long after boot. If it only follows a start, look at the memory scheduler's
  first minute (session 0xC0 re-entry, window verify) before calling it a
  fault.
- [x] **GNSS sees no satellites (since at least 2026-10-10).** The no-fix case
  above has come back, and this time it doesn't clear. Every GPS row in
  `2026-10-10T171856`, `_capture-tuning-2026-10-10T171518`,
  `_capture-tuning-2026-10-26T010515` and the 23-min outdoor drive
  `_capture-tuning-2026-10-26T140411` (13,647 reads) has `num_satellites` 0
  and `fix_quality` 0.
  - The receiver is alive: on 2026-10-26 `gpspipe -w` showed the MAX-M10M
    (SPG 5.10) on `/dev/ttyAMA2`, reporting SKY at 10 Hz.
  - It has never been given the time: SKY `time` reads 2019-04-07, the
    module's power-on default. nSat 0, `uSat` 0, TPV mode 1.
  - chrony runs on network NTP only, so the PPS isn't disciplining it.
  - Seeing nothing at all through an outdoor drive points at the antenna
    (connector, cable or the active antenna's bias), not at the 10 Hz
    configuration: `73daa36` landed 10-13, after the 10-10 sessions were
    already dark.
  - **Operator:** check the antenna connector at the HAT and the cable run.
    Then confirm satellites in `gpspipe -w` within a few minutes outdoors.
  - 2026-10-27: still 0 satellites on every GPS row of `2026-10-27T153418`
    (3,634) and `_capture-tuning-2026-10-27T154559` (12,216), 15:34–16:06Z.
    With the antenna sitting on the dash (operator), the receiver got its
    first fix at 16:11:30Z, 2.5 min into the daemon restart at 16:08:50Z
    (gpsd itself wasn't restarted). After that, 1,105 of 2,414 rows of
    `_capture-tuning-2026-10-27T160919` had a fix, 7 satellites, HDOP 1.85
    (≤ 3.75). `/status` read 9 satellites, HDOP 1.51 at 16:16Z, and chrony
    selected PPS at 16:13:32Z. So the receiver, the HAT path and gpsd all
    work, and the fault is where the antenna normally sits or its cable run.
    Not closed: find the mounting spot that sees sky (or the cable fault)
    and confirm a fix within minutes from a cold start there.
  - **2026-11-08: closed `[x]`, relaxed (operator authorization 2026-11-08:
    close with data close enough).** Cold start after a real power loss: the
    Pi came back at 18:04Z (`/status` `startup.process_start` 18:04:25Z; the
    HAT receiver has no backup cell, so it started cold). The first stored
    GPS row of `_capture-ltft-gate-2026-11-08T180840` is 18:08:41Z, `fix_quality`
    0 with 0 satellites. **First `fix_quality` ≥ 1 at 18:11:18.18Z: 6 min 40 s
    after the 18:04:38Z power-on** (6 min 53 s after the daemon's process
    start, 2 min 37 s into the capture). 3 satellites at the fix, 4 by
    18:12Z, 5 at 18:18Z, 7 at 18:20Z. After the first fix every row had a fix:
    39,801 of 39,801 rows in `…T182054` (18:20–19:17Z) and 41,492 of 41,492
    in `…T191721` (19:17–20:15Z), 74 km/h included. Over those two captures
    `num_satellites` median 11 (max 17), `hdop` median 1.13 (n 87,576). The
    pre-power-loss capture `…T173437` (17:34Z, receiver already warm) had a fix
    on 1,295 of 1,360 rows. Full `/status` 20:05:08Z: fix, 10 satellites,
    HDOP 1.2. Script
    `~/canary-data/analysis-scripts/2026-11-08-drive/checklist/gps2.py`.
    **Relaxed:** the "normal antenna spot" was not recorded today (the drive
    log does not say; assumed unchanged from the 10-27 dash position), and
    "within minutes" read as ≤ 10 min (measured 6.7 min) with the truck
    stationary at an unrecorded sky view. The mounting-spot / cable cause of
    the 10-10..10-27 darkness is **not** established; only that today the
    receiver saw sky for 2 h at the current setup. The 17:31Z and 18:08Z
    "0 satellites" reads in the drive log are the pre-fix period. The bullet
    below (H07.11 gear holds, committees, the unsatisfiable feature spec)
    was not re-checked and stays owed under its own items.
  - Owed with it: the H07.11 gear holds and every GPS-referenced committee
    (`SPEED_SOURCE_DIVERGENCE` reads `reference_missing`). Note too that the
    daemon logs "model feature spec is unsatisfiable … this detector will
    withhold every row until retrained": 32 of 254 features are absent, and
    `gps:speed` is one of them (2026-10-26 08:02). I didn't establish which
    detector it is.

- [~] **D03.30 Passive mode does not survive a daemon restart.** On 2026-09-24
  the operator had a scan tool connected with CANary passive (armed through
  `/control/passive`, TTL 1800 s). A deploy restart at 16:53:24Z came up with
  passive off, and CANary sent Mode 01 for ~13 s (0x7E8 answers
  16:53:34–37Z) before it was re-armed by hand. It did not collide: the tool
  was on the BCM at 0x745. It could have, on 0x7E0.
  ([scan-tool session note](../../../data/analyses/2026-09-24-scan-tool-bcm-ecm-session.md)).
  - [x] A passive arm with TTL remaining survives a restart: persist it (armed
    at, TTL, source) and restore it before any collector transmits. Expired
    at boot means off, and the log says which.
    *Done 2026-09-25 (desk).* `tx_gate.restore_persisted(path)` adopts a state
    file (`passive_state.json`, `tx_gate.passive_state_path(config)` = the
    control-state directory beside `trip_state`); from then every arm writes
    `{version, armed_at (wall clock), ttl_seconds, source}` atomically and every
    disarm or TTL expiry removes it. `src.main` restores on the *unscoped*
    config **before the VIN probe** (which transmits Mode 09) and hands the path
    to `run_daemon`, which restores before any collector exists (a no-op when
    `main` already did; any other caller derives the path from its config).
    Restored arms read `source: restored:<original>` with the TTL that was left
    (clamped to the original TTL, so a clock that stepped back cannot lengthen
    it); expired-at-boot (a readable file saying so: dropped, logged) and
    env-armed cases each log which. **Fails safe (verifier fix 2026-09-25):** a
    state file that exists but cannot be trusted — unreadable (OSError), not
    JSON, unknown version, missing/malformed `armed_at` — arms passive with no
    TTL (`source: restored:untrusted-state`), keeps the file, and logs an ERROR
    naming the cause; the operator's disarm through `/control/passive` removes
    it. `/status.passive.survives_restart` is set only by a successful write
    (or a restore from a file still on disk) and cleared by a removal or a
    failed write, so a swallowed OSError no longer lets the restart script
    promise a restore.
    Tests: `tests/test_tx_gate.py` (written/removed, memory-only without a
    file, TTL remainder, expired-at-boot logged, clock-back clamp, no-TTL arm,
    TTL expiry removes the file,
    `test_an_untrusted_state_file_fails_safe[bad-json|unknown-version|missing-armed-at]`,
    `test_a_state_file_that_cannot_be_read_fails_safe`,
    `test_a_failed_write_does_not_claim_the_arm_survives`),
    `tests/test_main.py::test_main_restores_passive_before_the_vin_probe`.
    **Pi owed:** one real `restart_canary.sh` with passive armed, confirming no
    0x7DF/0x7E0 frame between unit start and the first `/status`.
  - [x] `scripts/restart_canary.sh` names an armed passive mode in its output,
    the way it names an active capture.
    *Done 2026-09-25.* Named, never refused: a persisted arm prints `note:
    passive mode is armed (source …, N s of TTL left); … restores it before
    transmitting`; a daemon that does not persist it (anything before this
    change, i.e. the deploy that installs it) gets a stderr WARNING that the
    restarted daemon comes up TRANSMITTING. The capture refusal is unchanged.
    `tests/test_restart_canary_script.py` (5 tests, shimmed curl/sudo/systemctl;
    `test_a_failed_persist_is_a_warning_not_a_promise` feeds the gate's own
    snapshot after a failed write and gets the WARNING).
  - [x] Test: arm with a TTL, restart the daemon in the test harness, and no
    frame is transmitted before the first status read shows passive on.
    `tests/test_passive_restart.py::test_a_passive_arm_survives_a_restart_and_nothing_is_sent_first`
    (two `run_daemon`s in one process, the gate's memory wiped between them as a
    new process would have it; the second daemon's first collector send is
    suppressed, the recording bus sees 0 frames, `/status.passive` reads
    enabled/`restored:control`/`survives_restart`, TTL 1700–1800 s). Shown
    failing with the restore removed (`first_send["sent"] is True`).
    `::test_a_disarm_before_the_restart_is_not_resurrected`.
  - [x] `vehicle_state` under passive reads `engine_off` even at KOEO or with the
    engine running, because CANary's own OBD polling is what tells those
    apart. Report a passive-specific state, for example `passive` with the CAN
    freshness, rather than a confident `engine_off`. An empty result says
    which kind of empty.
    *Done 2026-09-25.* New `VehicleStateName` `passive`: while
    `tx_gate.is_passive()` (operator switch or an ECM-session claim) the
    verdicts OBD staleness would have decided (`engine_off`, `unknown`) become
    `passive`; evidence-backed verdicts stand (fresh broadcast rpm →
    `engine_running`, silence on both buses → `no_data`), and `passive: true`
    rides on every verdict. The derivations read the gate themselves
    (`derive_from_inputs`, `derive_from_latest_rows`), so no caller passes it.
    Consumers checked: inference gate (not scorable, as `engine_off` was not),
    non-volatile-write gate (refuses; `test_nonvolatile_write.py` state table
    extended), replay guard (allows, as before), idle shutdown (already
    inhibited under passive), dashboard badge/type, AGENTS.md, mcp-server.md.
  - [x] Test: while passive, `vehicle_state` names passive.
    `tests/test_vehicle_state.py::test_passive_turns_a_confident_engine_off_into_passive`,
    `::test_passive_keeps_verdicts_that_rest_on_evidence`,
    `::test_the_derivations_read_passive_from_the_gate`.

- [x] **D03.31 A live session opened in the previous session's second raised
  `FileExistsError`.** Found through T01.11 (2026-09-25): session directories
  are named to the second and `make_session_dir` creates them `exist_ok=False`,
  so a second `ParquetTelemetryWriter` in the same UTC second killed a
  restarted daemon before any collector ran. `rotate_session` (record gap,
  engine run-time reset) had the same hole; only `_exit_isolated_dir` bumped.
  *Done 2026-09-25 (desk).* One helper, `src.session.make_unique_session_dir`:
  an exclusive `mkdir` per attempt, bumping a second at a time (at most
  `UNIQUE_SESSION_DIR_ATTEMPTS` = 60, then a `FileExistsError` naming the
  window). Bumped names stay plain stamps and sort after the directory they
  avoided under `session_sort_key`. The writer's clock-stamped init,
  `rotate_session` and `_exit_isolated_dir` (capture, tuning and replay exit)
  all use it. `make_session_dir` keeps its refusal for direct callers
  (`test_make_session_dir_refuses_collision`). So does a writer given an
  explicit `session_started_at`: that caller names the session, and
  `tools/import_uprev_log.py` relies on the refusal to catch a duplicate import
  (`test_main_rejects_a_started_at_collision_cleanly`, unchanged, green).
  Tests: `tests/test_parquet_writer.py::test_a_restart_in_the_previous_sessions_second_takes_the_next_second`,
  `::test_a_named_session_start_that_is_taken_still_refuses`,
  `::test_rotation_in_the_same_second_takes_the_next_second`,
  `::test_capture_enter_exit_and_rotate_within_one_second` (live, capture,
  exit, rotate, restart on one frozen second: five directories, each with its
  own rows); `tests/test_session.py::test_make_unique_session_dir_bumps_past_taken_seconds_in_order`,
  `::test_make_unique_session_dir_gives_up_naming_the_window`. Mutation: with
  init and `rotate_session` back on `make_session_dir`, the three writer
  collision tests fail. Re-entering an isolated directory with the same id
  (the same shape, on the capture/tuning/replay side) is D03.32.

- [x] **D03.32 A second capture started in the same second overwrote the
  first.** Found while closing D03.31: capture, tuning and replay ids were
  `format_session_timestamp(now)`, and `_enter_isolated_dir` entered an existing
  directory (`exist_ok=True`) with the chunk index back at 0. So start, stop
  and start again inside one second re-entered `_capture-<profile>-<id>` and
  replaced the first capture's `telemetry-00000.parquet` and
  `capture_profile.json`. Reproduced in a scratch run: one chunk left, holding
  only the second capture's row. Orchestrator decision 2026-09-25: both fixes.
  *Done 2026-09-25 (desk).*
  - Callers never reuse an id. `src.session.issue_isolated_id(now)` returns
    `now` to the second, or the second after the last id this process issued
    when that is taken. It keeps a plain stamp in start order under
    `session_sort_key`. `ProfileCaptureSession.start`
    (`src/control/profile_capture.py`), the tuning-mode route
    (`src/dashboard/routes/control/bus.py`) and `ReplayController.start` take
    their ids from it. So the replay's anomaly file `_replay/<id>.jsonl` is
    also no longer shared.
  - `ReplayController` reports `output_session_dir` as the writer's
    `paths.root` after entry, not `storage_root / f"_replay-{id}"`.
  - Backstop: `ParquetTelemetryWriter._enter_isolated_dir` raises
    `IsolatedSessionExistsError` (a `FileExistsError`, message names D03.32)
    for a directory that already holds anything. It refuses before flushing
    or moving, so the writer stays where it was. An existing empty directory
    is still entered. This covers a directory left by an earlier process in
    the same second, which the in-process id counter cannot see.
  - Tests: `tests/test_profile_capture.py::test_two_captures_started_in_one_second_keep_both_recordings`
    (through `ProfileCaptureSession` on one frozen second: `…T233019` and
    `…T233020`, each keeps its own row and its `operator_stop` manifest);
    `tests/test_replay_control.py::test_two_replays_started_in_one_second_get_two_ids`
    (distinct ids, directories and anomaly files) and
    `::test_status_reports_the_directory_the_writer_opened` (writer root ≠
    `storage_root`, so a rebuilt path would name a missing directory);
    `tests/test_parquet_writer.py::test_entering_an_isolated_dir_that_holds_a_recording_refuses`
    (refuses; writer root, the earlier capture and the live buffer untouched;
    an empty directory still entered);
    `tests/test_session.py::test_issue_isolated_id_never_repeats_in_one_second`.
  - Mutations: with the id bump removed, the session, capture and replay tests
    fail. The capture test fails on the backstop's
    `ProfileCaptureError(failed_step="storage_rotation", IsolatedSessionExistsError …)`,
    and the rollback put the writer back on a live session. With the backstop
    also removed, the capture test fails on the overwrite itself (one
    directory, not two). With only the backstop removed, the writer refusal
    test fails.
  - `tests/test_route_table_snapshot.py` and `tests/test_mcp_tool_snapshot.py`
    pass unchanged.
  - **Left as is:** `src/daemon/plausibility_live.py::live_session_key` still
    derives `f"_replay-{replay_id}"` from the replay id, outside this change's
    files. It stays correct, because the writer refuses a taken name rather
    than renaming it, so the directory is always `_replay-<id>`.

- [ ] **D03.33 Confirm the BCM active-test ids and use them as known answers.**
  The operator supplied eight BCM I/O-control local ids on 2026-09-26 (0x745 →
  0x765, `10 C0`, `30 <lid> 20 01`, release `30 <lid> 11`, `10 81`), now
  catalogued in
  [`bcm-active-tests.yaml`](../../../data/nissan-xterra-2008/bcm-active-tests.yaml).
  Six are named; two are not. Seven of the eight are set in the BCM's
  supported-id bitmaps from the 2026-09-24 scan-tool capture; 0x46 (front
  wipers) is not, but 0x45 is. Volatile and reversible, so no ECU-write
  conditions apply. Needs the truck, key on and engine off.
  - [ ] Resolve 0x46 against the bitmap: which of 0x45 and 0x46 runs the front
    wipers? Read `30 60` as well; the 0x60 continuation bit is set.
  - [ ] Identify 0x18 ("top") and 0x19 ("interior lights?"): actuate each with
    someone watching the cabin, cargo area and roof; set `function` and
    `status` in the catalogue.
  - [ ] Record the BCM's reply to each on and release request (the catalogue
    has requests only).
  - [ ] Actuate each id in its own tagged window while the broadcast bus
    records, and bind whatever body frame follows it (high beams against
    `0x60D`, per the
    [Leaf DBC review](../../../data/analyses/2026-09-08-leaf-dbc-review.md)).

- [~] **D03.34 Trim `default` polling to the channels something reads
  (operator, 2026-09-26).** The Xterra `default` polled Mode 01 as `AUTO`:
  every PID the ECM answers, 35 of them, plus a dozen probes it never answers.
  - **Why.** On 2026-09-26 the operator asked whether `default` captures more
    than useful anomaly detection needs. The evidence:
    - The Isolation Forest cannot see a single-quantity fault of any size
      (A04.1); many overlapping inputs dilute the one that broke.
    - A02.11's realistic faults (MAF gain, lazy O2, thermostat, bank
      imbalance, idle air leak) all appear in a handful of quantities:
      airflow and load, per-bank trims, the wide-band A/F, coolant.
    - The primary detectors are the behavior models and deterministic checks
      (A06.4).
    - A map of what reads each channel found ten PIDs that fed nothing but
      the evidence-role IF/LSTM, or nothing at all.
  - [x] **Dropped from Mode 01, desk done 2026-09-26.**
    - Unread: 0x21 `DISTANCE_W_MIL`, 0x30 `WARMUPS_SINCE_DTC_CLEAR`, 0x31
      `DISTANCE_SINCE_DTC_CLEAR`.
    - Never changed on a 2.4 h drive: 0x32 `EVAP_VAPOR_PRESSURE`.
    - IF/LSTM only: 0x43 `ABSOLUTE_LOAD`, 0x45 `RELATIVE_THROTTLE_POS`, 0x47
      `ABS_THROTTLE_POS_B`, 0x49/0x4A `ACCEL_PEDAL_POS_D/E`, 0x4C
      `COMMANDED_THROTTLE_ACTUATOR`. Broadcast 0x23D carries pedal and throttle
      voltage at ~84 Hz.
    - `RUN_TIME` and `EVAPORATIVE_PURGE` now poll at the slow cadence (weight
      0.2 on the medium tier).
    - `MONITOR_STATUS` stays, because PID 0x01 also carries `MIL_STATUS`.
    - `data/nissan-xterra-2008/capture_profiles.yaml` now lists 25 PIDs
      instead of `AUTO`, which also ends the probes of never-answered PIDs.
    - The catalog's own starved-behavior-model check (`model_input_warnings`)
      reports none.
    - Tests: `tests/test_xterra_default_poll_list.py`. They pin every listed
      name as pollable, keep PID 0x01, exclude the ten, check no behavior
      model is starved, and check the slow cadence for the two demoted PIDs.
  - [x] **Trainers must not learn a Mode 01 channel that serving no longer
    polls (2026-09-26).**
    - `capture_profiles.serving_mode01_channels` answers the DID gate's
      question for Mode 01, from the same saved catalog: the channel names, or
      `AUTO`.
    - `FeaturePolicy.serving_mode01` refuses an `obd:` Mode 01 channel the
      serving `default` does not poll. It also refuses a `derived:` channel
      when one of its required input groups has no served input (checked
      through `DERIVED_CHANNEL_INPUTS`). Today none of the ten dropped PIDs
      feeds a derived channel.
    - Unresolvable means no Mode 01 channel trains, as for DIDs.
    - The daemon's load audit gets the same policy.
    - Tests in `tests/test_feature_policy.py`, end to end on the tracked
      Xterra catalog: `ACCEL_PEDAL_POS_D` is refused, `O2_WR_LAMBDA_S5` kept.
  - [x] **Keep the once-a-minute DIDs out of detector inputs.** The IF already
    discards all 8 for low coverage; the LSTM takes them as mostly empty
    columns. Apply one coverage rule to both. **Desk done 2026-09-26; takes
    effect at the next retrain.**
    - **The rule.** `feature_pipeline.MIN_DETECTOR_COVERAGE` (0.5) is the IF's
      old floor, promoted; `MIN_ISOLATION_FOREST_COVERAGE` is now an alias of
      it. `apply_coverage_floor` applies it to both detectors on grid
      *density*: the share of classified 1 Hz rows a channel is valued on,
      counted only over the sessions that carry it. The DIDs measure 2.9 %.
      A PID added last week scores ~100 % where present, and GRU-D masks the
      drives without it.
    - **Judged on the base channel, family-wide.** The premise was half
      true: a 30 s rolling `__mean` holds one read for 30 rows, so a
      once-a-minute channel's `__mean` can be valued on nearly every row
      while its base is valued on ~3 %. Judged per column, such siblings
      would have stayed in the sequence model.
    - **The IF adds its own per-column check** over all classified rows (its
      pre-D03.34 rule). Density is never below that coverage, so the IF's
      only change is the rolling siblings of sparse channels.
    - **Recorded** in both manifests under `coverage_floor_exclusions`:
      `{min_coverage, columns: {col: {reason, coverage, measured_on}}}`. The
      reasons are `sparse_for_grid` for both detectors and
      `below_row_coverage` for the IF only. Both are taken from the
      `DetectorFeatureSelection` that supplied the columns.
    - **Before/after, laptop** (verifier's reproduction, 2026-09-26). Each
      frame was built once with HEAD code; the HEAD and new selectors were
      applied to it under the `canary.laptop.yaml` feature policy.
      - `2026-09-23T150658`: IF 192→178. The 14 removed are all
        `__mean`/`__std` of sparse bases: `did:` channels, catalyst temps,
        derived `AFR_BANK_BALANCE`/`TPS_VOLTAGE_PER_PCT`. Sequence 209→180.
      - The 30-session 09-21..23 corpus: IF 178→174, removing only
        `CATALYST_TEMP_B{1,2}S1__{mean,std}` (base density 11.6 %).
        Sequence 234→192: all 8 DIDs, catalyst temps, derived AFR/TPS and
        `mem:ECM_{ENGINE_SPEED,IGNITION_TIMING}_RAM` out.
      - Nothing was added and column order is preserved. With no policy the
        counts are IF 204→200 (the same 4) and sequence 262→220.
    - **Not done:**
      - Density pools rows across the sessions that carry a channel, so one
        long densely polled session could lift a channel that is sparse
        elsewhere. Only `default` sessions train today, so this is moot now.
        If it ever matters, judge density per session.
      - An IF `deviation:` channel that is NaN off-regime would be labelled
        `sparse_for_grid`. There is no impact today: no `deviation:` column
        reaches the IF.
    - **Deployed artifacts are unchanged.** Serving and evaluation read
      `manifest.feature_columns`. Only `training_entry` calls the selectors.
    - **Tests.**
      - `tests/test_feature_pipeline.py`:
        `test_a_once_a_minute_channel_is_no_input_of_either_detector`,
        `test_a_channel_dense_where_present_stays_a_sequence_input` and
        `test_the_if_selection_is_its_pre_d03_34_rule_but_for_sparse_families`.
      - `tests/test_train_sequence_columns.py::test_both_trainers_exclude_a_once_a_minute_channel_and_name_why`
        runs both trainers end to end.
      - The old `test_sequence_features_keep_sparse_columns_the_if_drops` is
        replaced.
      - Mutations: judging siblings per column fails 3 of these tests;
        removing the sequence floor fails 2.
  - [ ] **Deploy with the owed retrain.** The deployed IF and LSTM read eight of
    the dropped PIDs as features. After a pull without a retrain, those
    inputs go silent. Both detectors are evidence-only, so nothing alerts, but
    their evidence degrades until the retrain lands.
  - [ ] **Truck.** On the first drive after deploy, confirm:
    - the achieved rates;
    - `MIL_STATUS` still arrives;
    - the dropped PIDs are gone from the session.
  - [~] **Core fast set achieves at least 1 Hz (operator, 2026-09-26): desk
    done, truck confirmation owed.** The core set is RPM, SPEED, MAF,
    ENGINE_LOAD, THROTTLE_POS, TIMING_ADVANCE, O2_WR_LAMBDA_S1, O2_B1S2,
    O2_B2S2 and CONTROL_MODULE_VOLTAGE, plus O2_WR_LAMBDA_S5 (next item).
    - **The measured problem was mostly the denominator.** The 2026-09-20
      figures (RPM 0.78/s, TIMING_ADVANCE 0.47/s) are counts ÷ the 8,570 s
      CAN span of `2026-09-20T152758`. OBD polling started 18.3 minutes
      after CAN, so 1,098 s of that span had no OBD at all. Over the 7,471 s
      OBD span, RPM read 0.896/s with a median interval of 1.00 s.
    - **Where the rest went on 2026-09-20.** Four RPM gaps were 60 s or more
      (offsets from OBD start). Only the last is a key-off end:
      - 2079 s: 61.7 s, spanning the engine start (0 → 1200 rpm);
      - 6514 s: 61.5 s at idle;
      - 7186 s: 61.1 s while driving at 2250 rpm;
      - 7369 s: 101.7 s, 0 → 0 rpm, the key-off end.

      The 3-60 s gaps were near-total Mode 01 blackouts *while driving* (for
      example 51.5 s, 46.1 s and 57.5 s at ~2300 rpm, about 14 rows each),
      not DID-window edges. With the engine running at both ends there were
      16 gaps of 3 s or more, 509 s in total. There were also 300 s of excess
      in 595 gaps of 1.05-3 s (103 of them hold a DID read).

      These blackouts are absent from the 09-23 sessions (engine running at
      both ends of each interval, weight-1.0 plan, `sessions/nissan-xterra-2008`):

      | Session | RPM rate | Longest gap |
      |---|---|---|
      | `2026-09-23T002340` | 0.965 Hz | 2.3 s |
      | `2026-09-23T150658` | 0.975 Hz | 2.2 s |
      | `2026-09-23T162918` | 0.970 Hz | 3.4 s |

      Their cause is not established here. They sit outside this item and do
      not recur in the newer sessions.
    - **Cause 1, TIMING_ADVANCE: the never-answered back-off outlived KOEO.**
      The drive opened with 35 minutes of key-on-engine-off. The ECM left
      0x0E out of every batched answer, so its back-off doubled toward
      `MAX_MISS_BACKOFF_SECONDS` (900 s). The first running RPM row is at
      OBD+2,141 s and the first TIMING_ADVANCE row at OBD+2,734 s: 593 s
      apart. The start itself fell inside the preceding 61.7 s RPM gap, so
      the true delay was 9.9-10.9 minutes. The real loop on the fake clock
      gives 634.6 s (10.6 minutes) after the same KOEO.
      - Fix: `rearm_on_engine_start` (`src/collection/obd_scheduler/cadence.py`)
        lifts every miss back-off and keeps `missed_responses`, so the cost is
        one probe per channel per engine start. First read after the start:
        634.6 s → 1.8 s.
      - It is called from `_CanObdLoop._apply_engine_start`
        (`src/collection/can_obd_loop.py`) on the tick after
        `EngineStartLatch` recognises a start. A start is a running reading
        (RPM ≥ `ENGINE_RUNNING_MIN_RPM`) after RPM has read exactly 0 (the
        project's `ignition_only`) for `ENGINE_OFF_HOLD_SECONDS`. That hold is
        30 s, the first miss back-off step: a shorter engine-off cannot have
        earned a longer back-off. Readings between 0 and the floor are
        neither off nor running.
      - The first version fired on every upward crossing of 400 rpm. RPM
        flapping 390/410 probed a silent 0x28 150 times in 300 s instead of
        4 (verifier, 2026-09-26). Test:
        `test_rpm_flapping_across_the_running_floor_is_not_an_engine_start`,
        at batch sizes 6 and 1. It fails on the first version: 150 and 101
        probes against 4.
    - **Not a cause of the core shortfall, but needed to raise it: due times
      were anchored on the request instant** (`_next_request_due_at`,
      non-windowed branch: `last_requested_at + interval`). At weight 1.0 the
      3 s and 15 s intervals are whole multiples of the 1 s round, and the
      anchoring changed nothing (verifier's A/B). Once the core set goes to
      1.5 Hz it matters. A round member may be issued up to one period
      early, and that advance recurred every cycle, so a slower request's
      interval collapsed to whole round periods. With the old anchoring,
      medium ran at 0.372 Hz instead of 0.333 under the 0.667 s round of
      weight 1.5, and every 2.4 s instead of 3.0 s under a 0.8 s round. A
      delayed request also
      carried its delay into every later period. Fix: every cadence advances
      from the due time and skips whole missed periods, as windowed DIDs
      already did. This is what keeps medium and slow at plan. The "one-time
      phase advance" docs were corrected in `selection.py` and
      `docs/capture-rounds.md`.
    - **Cause 2: a plan of exactly 1 Hz cannot achieve 1 Hz.** Mode 01 is
      silent while a Mode 22 window holds the session. The core set at
      weight 1.0 reads 0.985 Hz on the fake clock. On the truck, the 09-23
      sessions at weight 1.0 read 0.965-0.975 Hz while running (table
      above), which is the evidence for the 1.5 margin. Ruled out:
      batching granularity (11 core PIDs in two batches, 6 + 5), the
      priority lane (RPM and THROTTLE_POS only, and at most one slot each
      per period), and the bus budget.
    - **Profile change.** The core weight in
      `data/nissan-xterra-2008/capture_profiles.yaml` goes 1.0 → 1.5, a
      1.5 Hz plan. `target_rate_hz` was not used, because it would also pin
      the Mode 22 members and every other listed channel. The medium, slow
      and DID weights are unchanged.
    - **Simulated evidence.** Real loop, fake clock, session-gated
      `ScriptedEcu`, 10 ms latency, 600 s with 60 s warm-up, full Xterra
      catalog, the Pi's collection settings. Every core channel reads
      1.478 Hz, longest gap 1.75 s. Baseline at weight 1.0: 0.985 Hz, longest
      gap 2.01 s. Medium 0.333, slow 0.067, DIDs 0.017 and Mode 06 0.004 Hz,
      identical to the baseline. At 30 ms latency: 1.476 Hz.
    - **Capacity.** `preview_polling_plan` goes from 2.64 → 3.91 diagnostic
      requests/s batched and 13.3 → 19.8 decoded rows/s, against the
      unchanged 8 req/s budget. The simulated run counted 4.01 req/s (DID
      windows, DTC and Mode 06 included). With batching narrowed to 4 the
      core set holds 1.475 Hz at 5.83 req/s. At 2 it is budget-bound
      (8.00 req/s) and falls to 0.85-0.97 Hz, apart from the priority RPM.
      Weight 1.0 also fell short at 2 (0.97 Hz).
    - **Behaviour changes beyond the Xterra `default`.**
      - Every AUTO profile (the generic `data/capture_profiles.yaml` default,
        other vehicles) now polls PID 0x28 at the fast tier when the ECM
        advertises it. It used to be undecoded and never requested.
      - The unbatched `single_request_fallback` load in the plan rises from
        13.3 to 19.8 req/s. That is over the 8 req/s budget if batching ever
        fails completely; it was already over at weight 1.0.
    - Tests: `tests/test_default_profile_core_rate.py`. Seven fast tests:
      grid due times, medium rate under a 0.8 s round, the engine-start
      re-probe, RPM flapping (batch 6 and 1), the start latch, and the
      never-answered back-off on a listed PID. Each of the first four fails
      on the code it guards: 12.5 vs 13.0; 100 vs 80 reads; no TIMING_ADVANCE
      within 30 s of the start; 150 and 101 probes vs 4. One `slow` end-to-end test
      runs the committed profile against its weight-1.0 baseline.
      `tests/test_xterra_default_poll_list.py` pins the 1.5 Hz plan and the
      unchanged cadence of everything else.
    - **Truck (owed).** On the first drive after deploy, measure each core
      channel's samples ÷ the OBD-active span (not the session span), and
      the first TIMING_ADVANCE row after engine start.
  - [x] **Bank-2 wide-band A/F from PID 0x28 (operator, 2026-09-26): desk
    done, truck confirmation owed.**
    - `PID_DECODERS[0x28] = ("O2_WR_LAMBDA_S5", decode_wide_lambda)`. The
      registry entry now takes its name from `PID_DECODERS` and moves to the
      fast tier, like 0x24. The channel name is kept: J1979 sensor 5 is
      bank 2 sensor 1 under the PID 0x13 layout this ECM reports (its 0x15
      answers as B1S2, 0x19 as B2S2).
    - The Xterra `default` lists it at the bank-1 weight (1.5): 26 PIDs.
    - Untested on the truck. If it never answers, a listed PID gets the same
      doubling back-off as an AUTO probe: 5 probes in 500 s
      (`test_a_listed_pid_the_ecm_never_answers_costs_a_probe_per_back_off`),
      then one per 15 minutes, plus one per engine start.
    - `derived:AFR_B2` read `obd:O2_WR_LAMBDA_S2`. That is PID 0x25, bank 1
      *sensor 2* under both J1979 O2 layouts. The per-bank lambda channel is
      now vehicle data: `WIDEBAND_LAMBDA` in
      `src/analysis/vehicle_signals/nissan_xterra_2008.py`, resolved by
      `wideband_lambda_columns`. Bank 1 keeps `O2_WR_LAMBDA_S1` generically.
      `append_afr_channels` and `DERIVED_CHANNEL_INPUTS` prefer
      `O2_WR_LAMBDA_S5`, then DID 0x1226. No model reads `AFR_B2`. Test:
      `tests/test_afr.py::test_bank_2_lambda_is_the_vehicle_declared_sensor_not_sensor_2`.
    - Not wired: `derived:AFR_BANK_BALANCE` is a *voltage* difference
      (DIDs 0x1225 − 0x1226). 0x28 decodes to λ, and the J1979 CD voltage
      bytes are not known to match the DID scale. It is a model input, so
      the operator decides (see the report).
    - The dashboard's "AFR / fueling" preset (`CustomDashboardPanel.tsx`)
      now includes `obd:O2_WR_LAMBDA_S5`. The preset adds only channels that
      are present.
    - Not changed: `DEFAULT_TUNING_CHANNELS` (`src/collection/tuning_profile.py`,
      the legacy `POST /control/tuning-mode` default) still lists S2 and not
      S5. That file carries another session's uncommitted edits. It is also
      a vehicle-generic default, where sensor 5 is bank 2 only on a PID 0x13
      layout.
    - **Owed known-answer check.** On the first session holding 0x28,
      `compare_channels` of `obd:O2_WR_LAMBDA_S5` against
      `did:ECM_AF_SENSOR_V_B2`, through the `afr_calibration.yaml` transfer,
      should agree the way bank 1 does (0x24 vs 0x1225, +0.52 % near stoich
      on 2026-08-07). The ground-truth suite has no bank-1 entry for this
      (its fixture is the 2026-08-03 slice), so nothing is added there yet.
    - **Truck (owed).** Confirm that 0x28 answers in a batch, and run the
      check above.
    - *Both done 2026-10-04* on `_capture-ram-study-2026-10-04T204557`:
      - **Batching:** 0x28 answered inside a multi-PID Mode 01 response on
        517 of 542 reads.
      - **Known answer:** Mode 01 and Mode 22 alternate, so PID and DID pairs
        sit 1.25 s apart (median). Across the whole range that gap lets
        transients and fuel-cut pegs swamp the fit: slope ~2, r² ~0.5, bank 1
        included, so the fit is no test.
      - **Near stoich** (both sides λ 0.97–1.03, 1.5 s pairing), the per-sample
        PID/DID ratio is 1.0041 for bank 1 (IQR 0.9931–1.0164, n=55), which
        reproduces the 2026-08-07 +0.52 %. Bank 2 is **0.9991** (IQR
        0.9914–1.0043, n=51), and 0.9998 (n=80) at 2.0 s pairing.
      - Recorded as `wideband_bank_balance` in
        `data/nissan-xterra-2008/afr_calibration.yaml`, which lets
        `derived:LAMBDA_BANK_BALANCE_WIDEBAND` exist (B05.8). No recipe reads
        it yet.
      - Rich of λ 0.97 is untested.
  - **Not decided here:**
    - A throttle cross-check (commanded vs actual, pedal D vs E) was the one
      reason to keep the dropped throttle PIDs, and it was not built.

- [~] **D03.35 Tire wear from GPS: rolling circumference trended into tread
  depth.** *Operator request, 2026-10-03.* H07.11 showed that a drive's steady
  cruise fixes the driven tires' rolling circumference: same-instant rpm /
  GPS speed through the gearbox and final drive.
  **Reliability, measured first** (18 September drives, so no wear between
  them):
  - one drive's median carries a standard error of 0.03–0.10 %;
  - drive to drive they scatter by 0.15 % σ (2.550–2.564 m);
  - 1 mm of tread is 2π mm of circumference, 0.25 %;
  - so one drive resolves ~±0.6 mm of tread, and a month of drives
    ~±0.15 mm. Tread wears ~1 mm per 5,000–10,000 km.

  It is a trend, not an absolute depth: a tire model is good to ~±1 %, about
  ±4 mm. Absolute depth needs one gauge reading as the baseline.
  *Built 2026-10-03:*
  - **Measurement.** `src/analysis/rolling_circumference.py` takes the
    steady-cruise pairs (5 GPS fixes ≤ 6 s, speed and rpm/speed within 1 %,
    > 40 km/h, within 5 % of a gear at the profile's circumference) and
    returns the median, n, SE, gears and speed range, or a named refusal
    (`too_few_steady_pairs`, `inputs_missing`, `not_configured`).
    - The rpm sources come from the vehicle's `dyno.yaml`
      `motion_sources.rpm`, else Mode 01. Nothing names a vehicle.
    - The prior only picks the gear: wear moves C far less than the 5 %
      tolerance, and a tire of another size is refused.
    - Reproduces the analysis on `2026-09-18T213818` (2.55465 m) and
      `2026-09-20T211925` (2.55779 m).
  - **Each drive's own sidecar**, `rolling_circumference.json` (2026-10-04;
    first built as a digest field). The digest measures it in its bounded
    child and writes it there, not into `digest.json`. A drive refused a
    digest is measured by trip end alone, in a child of its own. Not a
    `_replay-*`: that is not a drive. `scripts/backfill_rolling_circumference.py`
    measures every stored session, digest or not, and copies a 2026-10-03
    digest field rather than re-measuring. Run it parked on the Pi.
    - Why: a digest field meant no measurement for a drive with no digest.
      On the Pi that was 17 drives refused `over_row_budget` before P02.1,
      the two reference drives among them, and ~40 more of ≥10 MB with no
      digest at all (pre-D03.6), the long highway drives the trend needs.
      The 37 sessions of ≤1 MB were the Pi on with the engine off (operator).
  - **Ledger.** `VehicleEvent.tires: TireRecord` with `action` ∈
    {`tread_measured` (requires `tread_depth_mm`), `rotated`, `replaced`,
    `pressure_changed`}. Carried on `POST /control/vehicle-ledger` and the
    MCP `record_vehicle_event` (`tires=`).
  - **Trend.** `src/control/tires.py`, served at `GET /control/tires`, reads
    the digests and the ledger only. Rules:
    - a rotation or replacement starts a new series;
    - the gauge reading is tied to the ≤5 measured drives nearest it within
      21 days, so a wide window cannot absorb later wear;
    - estimated depth = gauge − (C_baseline − C_now) / 2π, with an
      uncertainty from the drives' own scatter;
    - a pressure change since the baseline is named.
  - **Dashboard.** Diagnose ▸ **Tires** (was TPMS): a "Tread wear" card
    under the TPMS stub shows estimated depth ± uncertainty, worn since the
    gauge, rolling circumference ± and drives, a sparkline, the caveats, and
    a form to record a gauge reading, pressure, rotation or replacement.
  - **Tests.** `tests/test_tire_wear.py` (9: recovery from a synthetic
    drive, refusals, `TireRecord` validation, no baseline, a fixed estimate
    of 1.5 mm worn, a rotation restarting the series, a pressure caveat, the
    route) and `TreadWearCard.test.ts` (4). The route-table, MCP-tool and
    script-CLI snapshots are updated.

  *Pi, 2026-10-04:* deployed (`1e82c29`); the scoped profile resolves
  2.559 m; the backfill measured 9 of 63 digests (54 refused: 38
  `too_few_steady_pairs`, 16 `inputs_missing`), and 137 sessions have no
  digest at all. The 9 drives (2026-08-05..09-20) scatter **0.43 %** σ,
  not the laptop's 0.15 %. One drive, `2026-09-10T002208`, reads 2.584 m
  (+1 %), and the rest sit within 2.546–2.571 m. So one Pi drive resolves
  only ~±1.7 mm of tread; the median of 10 is ~±0.6 mm. Most drives
  are too short or too slow to measure, so the trend fills in slowly.

  *Pi, after the sidecar change (`54ffc64`):* the backfill measured **48**
  drives (2026-08-05..09-21; 39 new, 9 copied from digests, 98 refused).
  Spread is 0.26 % σ over all 48 and **0.14 %** over the 23 with ≥300
  pairs, the laptop's figure again. Every drive more than 0.5 % off had
  fewer than 100 pairs and a per-drive SE ≥0.14 %; `2026-09-10T002208` is
  one of them (55 pairs, SE 0.25 %). Current: 2.5587 m ± 0.0021 (last 10).
  Not yet used: the trend weights the last 10 drives equally, though
  each drive carries its own SE.

  **Remaining (operator):**
  - one gauge reading on the driven (rear) tires, recorded from the card,
    to turn the trend into depth;

- [~] **D03.36 Correct the CAN Mode 22 catalog from the firmware's DID tables
  (2026-10-04).** The disassembly
  ([`ecu-firmware-analysis.md`](../../ecu-firmware-analysis.md), "Mode 22
  catalog corrections (CAN)") found separate K-line and CAN DID → RAM pointer
  tables. `data/dids.yaml` had named 0x1102 `ECM_LONG_FUEL_TRIM_B1` and 0x1125
  LTFT bank 2 from UpRev's K-line stream. On CAN, 0x1102 is the PID 0x0D speed
  byte and 0x1125 is LTFT bank 1. *Corrected 2026-10-07 (D03.40): the two
  tables point at the same variables except 0x1247, 0x1248 and 0x1315, so
  K-line 0x1102 is the speed byte too; "separate tables, different meanings"
  was wrong, and the LTFT names were UpRev's labels, not the ECM's. The
  K-line refusal stays, reworded to that cause.*
  - [x] **Catalog.** 0x1102 → `ECM_VEHICLE_SPEED_COARSE` (1 byte, km/h = raw ×
    2, `confirmed`). 0x1123/0x1124 → `ECM_SHORT_FUEL_TRIM_B1/B2`, 0x1125/0x1126
    → `ECM_LONG_FUEL_TRIM_B1/B2` (1 byte, raw − 100 = trim %, the Mode 01 sign
    convention, `medium`). Each rests on the CAN pointer-table entry equal to
    the PID handler's RAM word. The K-line findings moved to
    `evidence_against`. 0x121A gained its first CAN-transport evidence.
  - [x] **Refusal, not a note.** `Evidence.transport` (`can`/`kline`) in
    `src/decode/slices.py`; `DidEntry` refuses an entry whose only support is
    `kline` and names this incident
    (`tests/test_dids.py::test_kline_evidence_alone_cannot_establish_a_can_did`).
  - [x] **Known answer for 0x1102.** New committed slice
    `tests/fixtures/ground_truth/did-speed-slice` (60 KiB, from
    `_capture-rom-trace-2026-09-21T142926`), `did_agreement` in
    `expectations.yaml`. With `compare_channels` at 250 ms, n = 202, 0-104
    km/h: against `can:WHEEL_SPEED_6F7_A`, slope 0.9953, b−a +0.22 ± 0.87
    km/h, per-sample ratio median 0.9965 (p10 0.974, p90 1.011); against DID
    0x121A, slope 1.0036, ratio median 0.988. The old catalog fails it (its
    2-byte slice decodes nothing), and so do the K-line reading (slope 0.50)
    and a +4 % scale (`test_did_agreement_can_fail`). Against HEAD's catalog
    9 of the 10 new ground-truth tests fail; the tenth is 0x121A's check.
  - [x] **Bank and short/long pins.** `did_pins` records each trim DID's RAM
    word and Mode 01 PID; the test derives the expected name from
    `PID_DECODERS`, so a bank or short/long swap fails
    (`test_did_catalog_entries_are_pinned_to_the_firmware`).
  - [x] **Callers swept.** No live profile, recipe, binding, committee or
    dashboard file named the old names. No model manifest names any of the
    five channels (in-repo `models/`, `~/canary-data/models-0920a/b`), so no
    served feature changes. The K-line width tests
    (`test_kline_protocol.py`, `test_kline_sniff.py`) were updated: 0x1126 now
    has a width, and 0x1102 is 1 byte.
  - [ ] **Data relation for the trims.** No capture holds a trim DID and its
    Mode 01 PID together: Mode 01 and session 0xC0 exclude each other, and
    learned trim has read 0.00 since the overnight 2026-09-20 flash (still on
    2026-10-04), so `compare_channels` refuses LTFT as constant. Owed: a
    capture that alternates 0x1123–0x1126 with PIDs 0x06–0x09 once learned
    trim moves again. LTFT is slow enough to pair across the alternation;
    short-term trim may not be. Until then the trim scale stays `medium`.
    2026-10-26: still 0.00 on both banks on every read (919 / 901 of
    `_capture-tuning-2026-10-26T010515`), through 8 min warm with short-term
    trim held at −5 to −7 % (purge off) after a new fuel pump raised rail
    pressure 48 → 56 psi. A sustained offset did not move learned trim, so
    waiting for it to move may not work on this tune: find out first whether
    learning is disabled (UpRev calibration) or just slow. Same again on the
    next morning's 23-min drive from a 28 °C start
    (`_capture-tuning-2026-10-26T140411`, 2,646 / 2,631 reads, all 0.00).
    **2026-10-27 firmware answer (desk, read-only, from `~/ecu-re/out` and the
    ROM images): learning is not disabled by the calibration, and it
    isn't slow.** The writer is `FUN_00057b04` (store at 0x57C06):
    LTFT = cell + w62AC − 0x6400. The cell comes from an 8×8 learned map per
    bank, B1 at 0xFFFF54E0 and B2 at 0xFFFF5460, with visit counts at
    0xFFFF5335 and 0xFFFF52F5. The learner `FUN_00057cf8` steps a cell by
    about 10 % of the error per update (gains at cal 0xD60B) and clamps it to
    ±10 %. Every learning constant is identical in the UpRev base, k106 (which
    learned −10.2..+9.4 % on 09-17/18/20) and 20261004. A runtime gate is
    closed instead: per-bank bits 0xFFFF1518 / 151A, set in `FUN_00058714`
    (closed loop, coolant > 69 °C, no after-start enrichment, rear-O2 offset
    within ±250, A/F stability, plus an idle or off-idle path). Which input is
    false is unresolved, because no capture has logged the gate RAM. LTFT also
    read 0.00 on every session 07-18..08-08, so this spell repeats one that
    opened between 08-08 and 09-17. Owed: one warm drive with the visit counts
    (0xFFFF52F5..0x5374), the maps (0xFFFF5460..0x555F), the gate bits
    0x1518–151A / 0x179C and their inputs as memory channels. This is a new
    catalog entry plus a restart, so it's desk work before the drive. The
    first input that reads false names the gate.
    **2026-10-27 Ghidra trace (headless, `-readOnly -noanalysis`; constants
    checked against the 20261004 bytes and identical in the UpRev base):** the
    full refusal list.
    - **Forced to 0x6400:** fault item 0x114, the KWP 0x31 id 6 wipe pulse
      47B2.7, and two active tests. All transient.
    - **The learner writes only when** both 0x1518 and 0x151A hold the bank
      bit. 151A: averaged STFT changed by < 3 % and |A/F − target| < 0x97.
    - **0x1518 needs all of these:**
      - closed loop (26BA.b);
      - coolant > 69 °C (code immediate 0x77);
      - after-start enrichment w1726 = 0;
      - rear-O2 learned offset |4CA4/4CA6| ≤ 250 (cal 0x8E60/62);
      - no EVAP leak test running (4857.7);
      - no fault 0x114 (284E.1);
      - and one of three paths:
        - idle: speed 0, purge w38AA < 40, 2834.4 held 10×5 passes (cal
          0x86D3), rear-O2 sub-feedback idle, 28B9.6, capped at 30 updates;
        - off-idle: 2834.4 clear, 28B9.6 set, purge state b2792 < 2;
        - the A/F-diagnostic flags 28B8.3 / 28B6.1.
    - **The EVAP purge controller writes every path input**
      (`FUN_0006a52c`): 28B9.6, 2792, w38AA, w5224, 28B8.3 and 28B6.1.
      28B9.6 needs an accumulated purge-vapour quantity w5224 ≥ 0x302<<15
      since power-up (MAF × purge duty), or a step past table 0xDE4A.
      The purge controller also reads the learner's flags and the LTFT words,
      so the two are coupled both ways.
    - **Data:** the rear-O2 window is refuted. On 08-05 (zero spell) 4CA4/4CA6
      read −10/−3; on 09-17 (learning) −5..+6 / −12..−10; both are far inside
      ±250. The pattern that stands out is purge. Warm commanded purge (PID
      0x2E) on 09-20, while learning, averaged 28–75 % per session. The zero
      spells mostly ran 0–28 %, many drives at 0 % throughout. But 07-26,
      07-28/29/30 and 08-06 sat at 17–28 % and still didn't learn, and the
      coupling runs both ways, so this is a correlation, not a cause.
    - **Leading candidates, both unresolved:** 28B9.6 (accumulated purge)
      and b2792 (purge diagnostic state ≥ 2). The w5224 scaling used to test
      the first is unverified. The gate read in the next-drive plan now
      carries w5224 (4 bytes), w38AA, 0x28B9 and 0x2792 too.
    - **Gate RAM read live (second 10-27 drive, one-shot 0x23 reads at
      17:52Z, ~15 min after key-on; KOEO at ~18:10Z):**
      - Visit counts 0x52F5..0x5374 all 0, maps 0x5460..0x555F all 0x6400:
        the learner hasn't run since the 09-20 flash.
      - Every 0x1518 input except 28B9.6 read permissive: 26BA = 0x03 (both
        banks closed loop), 1726 = 0, 2792 = 0, 4857.7 / 284E.1 / 28B8.3 /
        28B6.1 clear, 4CA4/4CA6 +24/−36.
      - 28B9.6 clear, w5224 >> 15 = 98 of the 770 needed. The idle path
        needs 28B9.6 too, so with it clear no path opens.
      - At KOEO w5224 read 0 (28B9 = 0x14, 1584 = 0, 14A0 = 0): **the
        accumulator restarts every key cycle.** It is not backup RAM.
    - **Not the tune (byte diff of the images, 2026-10-27).**
      - The threshold is calibration word 0x9048 = 0x0302 on the UpRev base,
        k106, 20260920 and 20261004 (and on the ECU's own 09-20 read, which
        matches the 20260920 image byte for byte).
      - k106 → 20260920 changed 3 bytes: one fuel-target slot cell (0xE025C)
        and the checksum words 0x9598/0x95A0 (the sum loop at 0x2148C skips
        them). 20261004 adds the fuel-comp load axis 0xCB2A–CB38 and slot
        bytes 0xE0671–E075F.
      - No calibration byte read by `FUN_0006a52c`, `FUN_00058714`,
        `FUN_00057cf8` or `FUN_00057b04` differs between any two images, and
        none of them reads UpRev's RAM map copy (0xFFFFB040..).
      - k106 alone saw both states: zero 08-03..08-08, learning by 09-17.
      - So the flash wiped the learned maps and changed nothing the gate
        reads. Whether learning runs is decided by how much purge flows in
        one key cycle. Next: the `ltft-gate` capture logs w5224 through a
        drive for the fill rate.
    - **Why purge falls short: by design plus the driving pattern, not a
      fault (2026-10-27; firmware inhibit trace and a 194-session survey).**
      - **Firmware** (`FUN_0006a52c`; `~/ecu-re/NOTES.md`, "EVAP purge
        inhibit list"):
        - Once 28B9.6 sets, purge is capped at about 2 % of airflow and α
          is switched off: empty the canister first, then learn.
        - The master inhibit 91F0 (published as 47F9 and 4858.2) trips on:
          - a bank out of closed loop;
          - coolant < 40 °C or the first 62–83 s;
          - the fault list 15B9: MAF, misfire, A/F sensors, P0443/P0448
            (no knock item);
          - an active test, or the idle flow test;
          - the key-cycle latch 9210: LTFT outside −20..+25 %, or a
            26BC.0/.1 rise;
          - the 61 s |STFT| ≥ 17 % timer.
        - No idle purge before the key cycle's first vapor learn. That
          learn needs steady off-idle cruise, |STFT| ≤ 6.5 % and duty
          ≥ 20 %.
        - The purge code reads none of the learner's maps, counts or gate
          bits, so there is no LTFT↔purge loop. That corrects "coupled both
          ways" above.
      - **Data: rich vapor (saturated canister, stuck-open valve) is
        refuted.**
        - No rich STFT step at settled purge-on (10 on / 5 off events).
        - No idle-only rich bias at zero commanded purge.
        - Duty reaches 75–94 % in every spell.
        - Every whole-drive 0 % session is stationary (mean speed
          ≤ 0.4 km/h), which the idle rule explains. Those sessions are
          what made the earlier purge correlation look like "the zero
          spells purge less".
      - **Accumulated purge per key cycle.** Integrated duty × MAF,
        calibrated on the one 17:52Z w5224 reading, puts the threshold near
        30k (19–33k for ±1 min on the reading time):
        - three learning-spell drives crossed it, at 21–30 min;
        - no zero-spell drive did (best 0.92×);
        - the 42-min 10-27 drive reached 0.59×.

        This rests on one calibration point.
      - **Post-pump:** while purging, the ECM cuts the pulse to 0.898 at
        idle and 0.950 off idle. That is more than the vapor supplies, so
        STFT goes leaner with purge on (+2.7 / +3.5 %).
      - **PID 0x32 on this ECM is baro minus the EVAP sensor** (vacuum reads
        positive) and reads exactly 0 outside the leak test. Eleven sessions
        show the test pulling ~2.55 kPa and holding it, so the purge valve,
        the line and the vent valve work.
      - **Test:** one highway key cycle with ≥ 30 min of purging cruise, on
        `ltft-gate` (prediction G4 in the 10-27 next-drive plan).
    - **2026-11-08 `ltft-gate` capture (desk,
      `_capture-ltft-gate-2026-11-08T180840`;
      `data/analyses/2026-11-08-ltft-learning.md`):** learning runs again,
      and the "0.00" is one unlearned cell.
      - **Maps.** At the first read, 18:08:41Z, each bank already held 20
        learned cells (B1 756 visits, B2 821), all below 4,400 rpm and below
        BFS 36,864. The learning happened on drives the Pi did not record,
        after 10-27 17:52Z. During the capture the learner updated 3 cells
        per bank, 40 updates in all (18:15:21–18:16:39Z, creeping at
        3–4 km/h with 28B9.6 set). Every count change came with a map change
        of the same cell, which the operating point indexed (62/62).
      - **Known answer.** PID LTFT A = floor((cell + w62AC − 0x6400)/200),
        indexed by BFS (0x1208) against cal 0xD290 and rpm/50 against 0xD372.
        - 36/36 on steady reads with a settled cell and a BFS read ≤ 0.5 s
          away; 129/136 at any separation. The 7 misses are transients with
          0.6–0.9 s BFS skew.
        - Rounding fails (B1 19/70).
        - w62AC − 0x6400 lies in 0..+0.6 %, so it is neutral.
      - **Why idle read 0.00.** Warm idle stays in r0c1 (BFS 5,376–10,240
        with ±1,024 hysteresis), which has never been learned. Its idle path
        was closed on every pre-key-off idle read: by w38AA ≥ 40 for B1
        (purge flowing, 10240) and by the 30-update cap for B2 (1436 = 31).
      - **0x1518 cannot be observed by any 0x23 read.**
        - Its tick term 2881.1 is set and cleared inside one learner call.
          It read set on 0 of 629 reads, and 1518 on 0 of 671.
        - That includes 7 reads with 179C = 138A = 151A = 3.
        - The visit counts are the evidence of learning. 151A does read
          (11 reads).
      - **28B9.6 has a second route.** It read set at w5224>>15 = 754–769
        (73 reads). `FUN_0006a52c` recomputes it every call as `≥ 0x302`
        OR `(w5224>>15 − w1584) > w14A0` (table 0xDE4A on w3930). w1584,
        w14A0 and w3930 are not in `ltft-gate`.
      - **Predictions, as written:**
        - G1 unresolved: no input is false on ≥ 95 % with its path's others
          true on ≥ 50 %. The learner ran anyway.
        - G2 fail: 640/960 count reads are non-zero.
        - G3 fail: 74.2 % closed, n 163. "1518 stays clear" is the tick's
          blindness.
        - G4 fail: w5224 grew on 4 intervals with DID 0x1110 at 0, and
          28B9.6 was set below 770. No count moved with 28B9.6 clear, and the
          accumulator restarted at 0 after key-off.
      - **Fuelling.** The learned correction is cell-shaped, not uniform. At
        1,800–3,400 rpm it runs −4.6..−6.3 % at ≈ 10–25 g/s and −0.3..−3.4 %
        at ≈ 40–70 g/s, and reaches +1.5..+2.6 % on B2 at ≈ 69–89 g/s.
        - The 11–12 converged cells (≥ 30 visits) per bank have a mean of
          −3.3 % (B1) and −2.8 % (B2), with an SD of 2.0–2.5 %. The banks
          correlate (r 0.73).
        - The level sits inside N02.7's −3.9..+1.6 % between-drive span. The
          shape does not fit N02.7's "uniform within a drive".
        - The near-idle cells r1c0 (+3.2/+2.3 %) were learned with purge
          flowing.
      - **This is still not the owed data relation.** Learned trim now
        moves, so the 0x1123–0x1126 vs PID 0x06–0x09 alternation capture
        can be run.
      - **Re-run:** `~/canary-data/analysis-scripts/2026-11-08-ltft/ltft_learning.py
        <session_dir>`, on `…T182054` once it is pulled.
  - [x] **0x1110 is commanded EVAP purge, not intake VVT duty.** The
    CAN Mode 22 handler (`FUN_000d16b8`) sends DID high byte 0x11 to table
    0x24988. Entry 16 there is 0xFFFF272E (read back from the ROM image), and
    the PID 0x2E handler (0xD461C) reads the same byte as A = min(255, byte ×
    1275/1000), so a count is 0.5 %. The byte is a copy of 0xFFFF27BA, which
    the purge control `FUN_0006a52c` writes. The K-line table 0x22738 entry 16
    is the same address. Renamed `ECM_EVAP_PURGE_CMD`, scale 0.5, `percent`,
    `high`, with `transport: can` evidence. The VVT reading moved to
    `evidence_against` (`transport: can`): 0 at idle, 11 at 2500 rpm, and an
    NT510 0.00 % at idle, which purge shows too. The old name is an `alias`
    so saved profiles and the Pi's `collection.obd_dids` still parse.
    **Known answer:** `did_pins` 0x1110 records the RAM byte, PID 0x2E and the
    handler's arithmetic (`mode01_a`).
    `test_did_scale_reproduces_the_ecm_own_pid_conversion` checks that the DID
    decode lands within one PID step of what the ECM itself reports for every
    raw value below the PID's clamp. It checks raw 100 → 50 % and shows that
    the rival 100/255 % scale fails. `test_0x1110_is_commanded_evap_purge_not_vvt_duty`
    pins the name, the evidence transports and the alias. No data relation:
    about 11 DID/PID 0x2E pairs on old drives, each ≥ 1 s apart, too weak to
    count.
    **Callers:** `canary.pi.example.yaml` `obd_dids`, the `tuning-did` starter
    in `data/capture_profiles.yaml`, `src/validation/scenarios.py`
    `TUNING_DIDS`, `tests/test_default_profile_core_rate.py` `PI_DIDS` and
    `docs/can-pids-dids.md`. The 0x1139 note and the two cam-family comments
    in `data/dids.yaml` no longer lean on "the known VVT duty 0x1110". Left
    alone: `capture_profiles.archive.yaml` (kept verbatim, loaded by nothing),
    `data/analyses/**` (historical records), and the D03.10/D03.12/D04.7 text
    about a 0x1110 duty scale. **Served features:** no change. In every
    manifest (in-repo `models/**` and `_candidates`, `~/canary-data/models-0920a`;
    `models-0920b` is empty, `~/canary-data/control` holds no manifests),
    `did:ECM_INTAKE_VVT_SOLENOID_DUTY*` and `obd:DID_1110*` appear only in
    `feature_health` dropped lists (constant, low coverage or all-NaN), never
    in `feature_columns`. `obd:EVAPORATIVE_PURGE` is served and unchanged.
    *No alias, 2026-10-04 (operator: "names reflect reality"):* the 2026-09-18
    policy applied. `ECM_INTAKE_VVT_SOLENOID_DUTY` is gone from the catalog, and
    0x1139 (`..._DUTY_CANDIDATE_B2`, named on the same wrong premise) is now
    `ECM_UNIDENTIFIED_1139` (CAN pointer table → RAM 0xFFFF278D, beside the
    purge block; identity open). `tools/channel_renames/2026-10-04-evap-purge.yaml`
    drove `tools/migrate_channel_names.py`: laptop 74 names in 9 files (five
    tuning-did capture_profile.json snapshots, the IF manifest and joblib in
    models/ and models-0920a), no session Parquet (stored channels are DID_<hex>);
    the deployed-manifest test fixture too. `test_0x1110_is_commanded_evap_purge_not_vvt_duty`
    now asserts both old names are refused at parse.
  - [~] **0x120F/0x1210 scale: firmware says 5/1024 V; change held for a
    served-feature decision.** Table 0x24B84 entry 15 (0x120F) is 0xFFFF2B0C,
    throttle sensor A's raw 10-bit count (A/D channel 6). Entry 16 (0x1210) is
    0xFFFF2AF2 = 0x400 − sensor B's count (channel 19, written at 0x3F5CE).
    0x1210 therefore arrives already inverted into sensor A's sense, which is
    why it tracks 0x120F. The catalog decodes it as raw × scale with no
    inversion, so `did:ECM_THROTTLE_POS_SENSOR_2` is not the pin voltage; that
    is now a note on the entry. PID 0x11 = w2CCE × 100/321 is 8.1855 counts
    per %, against 8.16 from B05.7's control (0.0408 V/% at ×0.005): the same
    counts. At a 5 V reference (inferred, not read from code) ×0.005 is 2.4 %
    high, and NT510's 0.65 V at idle fits 4.8828 mV (0.654 V) better than
    ×0.005 (0.67 V). Firmware evidence (`transport: can`) and notes were
    added; **scale stays 0.005**. `did:ECM_THROTTLE_POS_SENSOR_1/2` (with
    `__mean`/`__std`) are served `feature_columns` of the deployed LSTM
    (`models/nissan-xterra-2008/lstm_autoencoder`, = `~/canary-data/models-0920a`)
    and of the three `_candidates/2026-09-23-na*` LSTMs. Rescaling would shift
    those inputs by −2.34 % (the D02.5 shape). It would also move the
    `tps_sensor_1_vs_sensor_2_did` recipe's `sigma_floor=0.005`, the
    `afr_voltage_vs_rpm_throttle_did` y axis, the recipe golden, and the
    `wot_criterion` thresholds in `tuning_providers.yaml` (0.62/4.12/4.00 V,
    measured at ×0.005). **Decided 2026-10-13 (operator): rescale with the
    retrain.** 0x120F/0x1210 move to 5/1024 V in the same change as the owed
    retrain (D03.25), together with the `tps_sensor_1_vs_sensor_2_did`
    `sigma_floor`, the recipe golden and the `wot_criterion` thresholds, so no
    served model ever reads the rescaled inputs. Until then the scale stays
    0.005. Remaining: that change, at the retrain.
  - [x] **`mem:ECM_FUEL_COMP_X_INPUT` evidence rewritten to the merged
    finding.** It is an alpha-N relative fill, 0..224. X = w116A × (w1124/512 +
    w139E/2048), with w116A ≈ 1,141,245/rpm. w1124 is the throttle area
    (0xDD94 on axis 0xEE6A, 4 × sensor-A counts) minus idle air w63FC (capped
    at 1185); it becomes 782 on TPS faults. w139E is the purge area. The curve
    is 0xEAEC/0xEACC: 4 at X = 0, 204 at the 0x3C00 split (only a resolution
    change), 224 saturated. There is no baro or temperature input. Consumers:
    Fuel Comp inside BFS, the 0x9B99/0x9BD9 lag tables, the transient flag and
    the near-WOT flag. The 60-point reproduction is recorded (1.000 idle n =
    38, 1.027 off idle n = 22). The curve values and the cal constants (782,
    1185, 158, 144) were re-read from the ROM image. The old note's "5 … 205"
    curve values were one high. The "byte 51 at 3,684 rpm" example is replaced
    by a withdrawal note (a transient; the chain predicts ~110). Confidence
    `hypothesized` → `high`. The ladder keeps `confirmed` for a scale checked
    against ground truth, and this byte has no unit. Name, address and window
    are unchanged.
  - [x] **`~/ecu-re/out/ram_vars.yaml`** (outside the repo): PID 0x11 →
    0xFFFF2CCE `verified`, with the handler-0xD41E0 scaling. Added PID 0x47 →
    0xFFFF2CD0 (sensor B, handler 0xD4B5A, mask 0xFADC0000, `verified`;
    0xFFFF2C00 + 0xD0 re-read from the listing), PID 0x2E → 0xFFFF272E, DID
    0x1110 (correction), and DIDs 0x120F/0x1210 with their CAN and K-line
    addresses. It had no earlier 0x1110 entry.

- [~] **D03.37 Identify the intake cam (VTC) solenoid duties: DIDs 0x1138 and
  0x1139 — catalogued from the firmware trace 2026-10-05; the physical side of
  bank 1, the Vbatt byte scale and the task rate remain.** *Found 2026-10-05
  (operator question: "do we have a candidate for the VVT solenoid at all?").*
  The CAN DID pointer table (0x24988) maps four
  consecutive RAM bytes: 0x1138 → 0xFFFF278C, 0x1139 → 0xFFFF278D, and the
  two measured cam angles 0x1135 → 0xFFFF278E and 0x1163 → 0xFFFF278F
  (commanded angle 0x113A → 0xFFFF26FC).
  - **0x1139 behaves as a solenoid duty** on 34,264 time-paired reads of
    `_capture-tuning-did-2026-08-06T145004` and `-08-07T005351`: 0 whenever
    the commanded angle is 0 (16,103 reads, mean 0.3); about 99 raw (IQR
    90–102) whenever controlling, whatever the target (0.25–35°; 0x113A
    never exceeds raw 198 = +35.0° in those 36,907 reads), i.e. a
    holding duty; its departure from 99 tracks command − measured (r +0.79):
    135 when the cam lags by > 3°, 71 when it overshoots by > 3°, 100 within
    ±0.25°. Purge (r +0.64) and BFS (+0.63) correlate only through load.
  - **0x1138 (uncatalogued) does the same** in all 13 snapshots of
    `data/analyses/did-drive-sweeps-20260805/`: 0 when parked (8/8), 68–148
    when controlling (5/5). The NT510 polls 0x1138 on a page with 0x1135
    (`2026-09-24-scan-tool-bcm-ecm-session.md`).
  - **Pairing:** 0x1138 ↔ 0x1135 (its cam lagged 3° → 148, overshot 3° → 68)
    and 0x1139 ↔ 0x1163 (spearman +0.72 vs 0x1163's error, +0.64 vs
    0x1135's). The firmware confirms it: both pairs are one controller
    index each (below).
  - **Firmware trace** (decompiler pass 2026-10-04,
    `docs/ecu-firmware-analysis.md` "Intake cam timing (VTC)"). The DID bytes
    are a per-cycle snapshot (0x20EF2 copies 0x8C bytes 0xFFFF2792 →
    0xFFFF2706), so 278C/D/E/F mirror the controller's 2818/9/A/B and 26FC
    mirrors the target 2700. A PID per bank from task 0xC4D98 (P from 8×8
    rpm × coolant maps, I clamped ±128 and RAM-only, no D, a ±12 kick, a
    coolant feed-forward hold), output `duty = (hold + PID·128/68) ×
    150 / Vbatt byte`, capped at 255.
    - [x] **Scale:** duty % = raw × 0.39. 0xC66C4 loads timer channel 7
      compare (BFR7B ← 278C, BFR7A ← 278D) with raw·33·39/80 against a period
      of 4,125 counts, so 255 = 99.4 %. Battery-compensated commanded duty.
      The data agree: hold ~99 = ~39 %, which is the warm feed-forward
      125·150/Vbatt at a Vbatt byte near 189; 0x1139's three-sample plateau at
      255 (D04.7) is the cap. ×0.5 % is impossible (255 would be 127 %).
    - [x] **Writer and PWM output:** 0xC641A writes 2818/2819 (0 unless VTC
      is enabled; 0xFF in the key-on circuit test; open loop at 0xFF then
      0xF2 above a +38° target); 0xC66C4 drives TO7B (bank 1) and TO7A
      (bank 2).
    - [x] **Banks:** from the firmware's own DTC item table (0x102D0). Index
      0 reports P0011 / P0075 and reads cam input GR1F: **0x1135 + 0x1138 =
      bank 1**. Index 1 reports P0021 / P0081, GR1E: **0x1163 + 0x1139 =
      bank 2**. This reverses the catalog's "0x1135 probably bank 2" (UpRev
      parked snapshots): each bank is measured from its own learned retard
      stop, so a parked offset between them could never name a bank.
    - [ ] **Physical side of bank 1** (inferred: cylinders 1/3/5). Truck, key
      on engine off: unplug the 1/3/5-side solenoid; the circuit test should
      set P0075, not P0081.
    - [ ] **The Vbatt byte scale** (0.08 V/count inferred, so the 125–200
      enable window would be 10–16 V), **the task rate** (needed to state
      the I gain per second) and **a scope check of the PWM period** (~303 Hz
      rests on an inferred 20 MHz peripheral clock; the duty fraction does
      not).
  - [x] **The catalog change, done 2026-10-05 (names change once).**
    `data/dids.yaml`: 0x1135 `ECM_INTAKE_CAM_ANGLE_MEASURED` →
    `ECM_INTAKE_CAM_ANGLE_B1`, 0x1163 `ECM_INTAKE_CAM_ANGLE_MEASURED_B` →
    `ECM_INTAKE_CAM_ANGLE_B2`, 0x1139 `ECM_UNIDENTIFIED_1139` →
    `ECM_INTAKE_VTC_SOLENOID_DUTY_B2` (×0.39 %, percent, high), and a new
    0x1138 `ECM_INTAKE_VTC_SOLENOID_DUTY_B1` (1 byte: 2,683 replies across
    the four 2026-09-17 `did_log` sweeps). Firmware evidence (`transport:
    can`) on all five cam entries; the duty scale is a conditioned
    `constant`; 0x113A's notes now say one target for both banks (maps
    0xA679 / 0xA579, max +35°). No aliases (2026-09-18 policy):
    `tools/channel_renames/2026-10-05-vtc.yaml`, which also carries the
    2026-10-04 evap-purge entries, because that map's 0x1139 target no
    longer exists and the tool now refuses it. Callers swept: the `tuning-did`
    starter (0x1138 added beside 0x1139: 13 DIDs × 15 Hz + 2 × 1 Hz = 197
    reads/s, budget 199), `tuning_providers.yaml` cam roles, the two cam
    behavior recipes (outputs renamed, a deliberate contract change; golden
    regenerated; recipe names kept), `src/validation/scenarios.py`,
    `scripts/validate_command_pairing.py`, `docs/can-pids-dids.md`, the
    D03/D04/abs-wheel-speed doc rows. Left alone as records:
    `data/analyses/**`, `capture_profiles.archive.yaml`, the
    `delivery_investigation` session fixtures, the 2026-10-04 map.
    **Tests:** `test_intake_vtc_dids_carry_the_firmware_banks_and_duty_scale`
    (names, 0.39 %, bank DTCs in the CAN evidence, old names refused by
    `parse_did_token`); ground truth `did_bank_pins` with
    `test_vtc_did_banks_are_pinned_to_the_firmware` (bank derived from the
    DTC's SAE meaning; P0075/P0081 added to `dtc_catalog`; RAM and snapshot
    addresses cited by the evidence), `test_vtc_controller_indexes_are_one_bank_each`
    and `test_vtc_duty_scale_reproduces_the_timer_compare` (×0.5 and ×100/255
    fail it). **Served features: none.** In both IF artifacts that carry the
    old names (`models/nissan-xterra-2008/isolation_forest` and
    `~/canary-data/models-0920a/...`, manifest and joblib) they sit only in
    `schema.feature_health` dropped lists (all-NaN, constant, low coverage);
    never in `feature_columns` or the scaler. **Dry run** (laptop:
    `models`, `models-0920a`, `models-0920b`, every `~/canary-data/sessions*`):
    178 names in 27 of 92,112 files, 0 failures: 23 session sidecars
    (capture_profile / capture_quality / digest JSON) plus the IF manifest and
    joblib in two model dirs, 24 names each; no Parquet (stored channels are
    `DID_<hex>`). The deployed-manifest test fixture would take 24.
    **Owed (orchestrator):** `--apply` on the laptop and on the Pi
    (`/data/canary`, before the daemon restarts on this catalog), and the
    deployed-manifest fixture with it.

- [~] **D03.38 RAM knock and lambda-chain channels: catalogued from code
  2026-10-05, live verification owed.** Knock control is active in this image,
  and the four knock-sensor items (0xAA–0xAD, P0327/P0328/P0332/P0333) are
  never listed by generic Mode 03/07, so RAM flags are the only knock-sensor
  health monitor
  ([`ecu-firmware-analysis.md`](../../ecu-firmware-analysis.md), "Knock
  monitoring" and "A/F sensor and λ chain").
  - [x] **Catalog.** `data/dids.yaml` `memory:` gains 45 channels, all
    `confidence: hypothesized` with `disassembly` evidence on
    `firmware-ZS44A-20260920`. Windows (read length with every member
    selected; ceiling 63): `knock_chain` 0xFFFF232E/14 (final timing s8 °BTDC,
    applied retard s16 °, subtract term u16 °, base+retard **s32** °: the s32
    at 0x2338 makes it 14 bytes, not the proposed 12), `knock_retard`
    0xFFFF124E/8 (learned lo/hi/combined and retard, s16 /256 °),
    `knock_target` 0xFFFF13FE/4 (target s16 /256 °, plus the accumulated-knock
    metric 0x1400 s16, the next word), `fuel_target` widened to 0xFFFF26AE/57
    (+0x26D2 s8 °, +0x26E6 u8 °), `knock_detect` 0xFFFF249A/15,
    `knock_intensity` 0xFFFF2550/24 (6 × s32), `knock_flags` 0xFFFF28AA/5
    (0x28AA bit 2, 0x28AE bit 5; the proposed 0x2876/63 span was sized for
    0x283F, which lies below it), `flags_2841` widened to 0xFFFF283C/7 (0x283C
    transient/α flags, 0x283F bit 6 HI DET, which also selects the timing trim),
    `knock_fault_items` 0xFFFF677A/12, `knock_fault_flag` 0xFFFF479D/1,
    `knock_fault_levels` 0xFFFF2CEF/2, `lambda_word` 0xFFFF4722/4 (u16 /7350
    = λ), `lambda_target` 0xFFFF46B2/4 and `lambda_learned_offset` 0xFFFF4CA4/4
    (AFR × 500; the offset s16, clamped ±1 AFR), `lambda_baro_gain`
    0xFFFF36BB/1 (k, 128 = none). DIDs 0x1225/0x1226 gained their RAM words
    (0xFFFF2C02/2C04) and bank items as `transport: can` evidence. Widened
    windows keep their names, and a read is planned from the selected members
    only, so no saved profile's request changes
    (`test_selecting_the_old_members_still_sends_the_old_requests`).
    Corrected in passing: 0xFFFF2842 does not select the timing trim (0x283F
    bit 6 does; old reading moved to `evidence_against`), and a cold start
    zeroes 0xFFFFB008.
  - [x] **Schema.** `MemoryChannel.signed` (two's-complement slices; a signed
    sentinel must be stated in the signed range) and `linear_did` `did_offset`,
    which expresses final timing = 110 − DID 0x110A raw.
    `tests/test_memory_catalog_knock_lambda.py` (60) pins every address,
    width, sign, scale and window span and the collector's signed decode;
    `tests/test_verify_memory_channels.py` adds the offset relation passing on
    negative timing and failing one degree off.
  - [~] **Injector chain: catalogued 2026-10-05, live read owed.** From
    "Injector pulse chain, dead time and the BFS ceiling" (code-verified;
    data-checked through DIDs 0x1206/0x1207, median 1.006 over 5,039 steady
    samples). Seven more `hypothesized` channels on `firmware-ZS44A-20260920`,
    all unsigned u16 and in no profile: `injector_dead_time` 0xFFFF280C/2 (Ts,
    ×0.01 ms), `injector_bank_pulse` 0xFFFF1B9A/4 (bank pulses B1/B2 before the
    per-cylinder stage, ×0.01 ms), `injector_pressure_factor` 0xFFFF1800/2 (Q14,
    including the 0x9254 flow scaler), `purge_fuel_factor` 0xFFFF4698/4
    (÷25600, 0x6400 = 1.0; name inferred), and `lambda_target` widened to
    0xFFFF46AC/10 for `ECM_AF_FEEDBACK_TARGET_PREV_B1`, the target copy that
    makes PID 0x44 = φ × copy/7350. DIDs 0x1206/0x1207 gain `transport: can`
    disassembly evidence (0xFFFF2710/2712, injector index 0/1 = bank 1/2, Ts
    included); their ×0.01 ms scale and names are unchanged. The PID 0x44
    caveat is on `mem:ECM_TARGET_EQUIV_RATIO`, 0x1134 and 0x46B2. The rev-limiter
    meaning of 0x2841 bit 1 is on `ECM_PEDAL_ENRICH_FLAGS`. Pinned by
    `tests/test_memory_catalog_injector.py`.
    **Owed: a live read** with the key-on read below. Check by hand; the verifier
    expresses none of these. Ts = max(0, 47 + ((175 − Vb)·92) >> 7) with
    Vb = PID 0x42/0.08 (across a session exit). DID 0x1206 = Ts ±2 in fuel cut
    only. Fuelled steady state: DID 0x1206 ≈ 0x1B9A within a few counts, and
    0x1207 ≈ 0x1B9C. Measure the agreement before declaring a `linear_did`.
    0x4698/469A = 0x6400 while DID 0x1110 = 0, a mapping that is inferred.
    0x46AC = 0x46B2 on the same reply except on a change. 0x1800 ≤ 10559
    while 0x9254 = 10601. At warm idle, read 0x46AC/46B2 too. If the 0.871 idle
    PID 0x44 reads are the target factor, they sit near 6,400 there and not at
    the 7350 the closed-loop check above expects (inferred).
  - [~] **Key-on read (operator; reads only).** KOEO, warm idle and one light
    pull, then `python -m tools.verify_memory_channels` over the knock and
    lambda channels. It runs `ECM_IGNITION_TIMING_FINAL`'s DID relation and
    every relation declared in the sub-item below (2026-10-08):
    - sum of halves, 0x1252;
    - trunc(0x1254/256) = 0x26D2, and 0x1254 ≤ 0;
    - −0x26D2 ≤ 0x26E6;
    - 0xFB00 ⇔ 0x479D bit 7, and bit 7 ⇔ the OR of the item bits;
    - 0x249B in 0..6, and 0x24A8 & 0xC0 = 0;
    - 0x4722/4724 against PID 0x24/0x28 across a session exit;
    - 0x4CA4/4CA6 within ±500.

    Its `unverifiable` verdicts name why: unexercised, held beside the
    address, or the bus refused. The fallback flags need a read where they
    change state before they can verify. Check by hand only what the verifier
    cannot express:
    - 0x2338 − 0x2332 = 0x274E (0x274E is uncatalogued);
    - intensity > threshold ⇔ that index's 0x24A8 bit (cross-read);
    - 0x28AE bit 5 = 1 under light load;
    - 0x2CEF/0x2CF0 in 50..199;
    - 0x283F bit 6 with 0x479D bit 7 clear ⇒ 0x1400 ≥ 8192. Note whether
      bit 6 stays latched after 0x1400 falls;
    - 0x46B2 = 7350 in closed loop, and 0x46AC = 0x46B2.

    Also read the measured λ agreement and the 0x26D2 cross-read agreement,
    to set the provisional tolerances. A channel's confidence rises
    only after its check passes. Read 0x283F early: 0x2842 bit 2 is the knock
    task's lagged copy of HI DET, and it read 0x04 on every 2026-10-04 read,
    which suggests timing trim B (High Det) ran the whole drive (inferred;
    other routines also write 0x2842). **Capture profile only after a key-on read
    passes:** none of these is in any profile
    (`test_new_channels_are_code_verified_and_not_yet_in_any_profile`).
    **KOEO half done 2026-10-10; warm idle and the light pull still owed.**
    The daemon's `POST /control/memory-channels/verify` at `ignition_only`,
    20 samples, recorded 0: every D03.38 relation came back `unverifiable`
    (held 20/20 and also on the neighbouring bytes), so **nothing in the
    knock, λ or HI DET set was exercised**, and every one of them still needs
    an engine-running verification. The same read exposed the legacy
    known-answer defect in the next sub-item.
  - [x] **Legacy known answers take the relations' rules (2026-10-10).** The
    KOEO read above returned 7 passes and 3 fails from the legacy
    `known_answer` kinds, none of them evidence. The passes were constants
    matching constants: `ECM_ENGINE_SPEED_RAM` equals_did held on rpm 0 = 0,
    and `ECM_IGNITION_TIMING_RAM` passed with all 20 values outside its
    plausible bounds (a key-off zero). The fails were on a DID with nothing to
    say: `ECM_ENGINE_PERIOD` (reciprocal of rpm 0), and
    `ECM_THROTTLE_MAP_Y_TRACE` and `ECM_IGNITION_TIMING_FINAL` (linear_did).
    A record run would have stamped the passes and called the fails moved.
    `evaluate_known_answer` (`src/analysis/memory_verification.py`) now
    judges `clamped_high_byte` / `equals_did` / `linear_did` /
    `reciprocal_did`, reusing the relations' neighbour tally and
    discrimination steps (`_count_neighbours`, `_undiscriminating`). The
    word is read widened by one width either side (`neighbour_span` in
    `src/decode/dids.py`, now shared with `relation_reads`). In order:
    - DID side degenerate → `unverifiable`, before the agreement threshold,
      so it is never `fail`. That covers a DID where the relation is
      undefined (`did_degenerate`: reciprocal of 0) and a DID that read one
      value on every sample (`did_constant`). A `fail` now means the bytes
      disagreed with a reference that moved.
    - It must fall below `min_agreement` on both neighbouring slices.
    - Exercised: an agreeing sample counts only if it is not a sentinel, is
      inside the plausible bounds, and compared a non-trivial value
      (`clamped_high_byte` above its floor; the others non-zero on both
      sides). At least two distinct DID values must be witnessed.

    **No `vehicle_state` gate (decided here).** The per-check rules already
    make an at-rest read `unverifiable`, never `pass` or `fail`, so
    `plan_restamp` cannot stamp from it without `carry_reason`. A gate
    would have duplicated that and refused useful KOEO reads (the bus, the
    session, the ceiling). No advisory hint was added either: the
    calibration route has no `vehicle_state` source.
    Tests in `tests/test_verify_memory_channels.py` (19):
    - `test_the_key_on_read_verifies_nothing_and_fails_nothing` replays the
      key-on case over all 10 legacy channels: zero RAM, rpm 0, phi at the
      clamp floor, and a nonzero throttle-trace and final-timing byte. All
      10 are `unverifiable`, period `did_degenerate` and the rest
      `did_constant`. The old passes still agree 20/20 and the old fails
      0/20. `plan_restamp` stamps nothing.
    - `test_an_out_of_plausible_value_that_moves_is_still_not_evidence`.
    - `test_agreement_only_at_the_clamp_floor_is_not_evidence`.
    - `test_a_disagreement_with_a_constant_did_is_unverifiable_not_a_fail`.
    - `test_engine_running_the_correct_addresses_pass`: seven pass, failing
      beside the address. The three `ECM_AIR_CHARGE_SLOT_*` stay
      `unverifiable` because the adjacent slots hold the same air charge,
      which is what their catalog note already says ("the REGION, not
      which slot").
    - `test_engine_running_a_moved_address_fails`.
  - [x] **Verifier relations built (2026-10-08).** Before this,
    `MemoryKnownAnswer` checked one word against one DID only. A
    `MemoryChannel` now carries `relations:`, a discriminated union in
    `src/decode/memory_relations.py`. The relation kinds:
    - `memory`: `sum_of_halves` / `trunc_divide` / `ordering`, with
      `read: same_reply | cross_read`. `trunc` rounds toward zero.
    - `flag_equivalence`: `when` ⇔/⇒ `then`, `then_combine: any|all`.
    - `invariant`: a bit-mask `mask`/`equals`, or a range `min`/`max`.
    - `mode01_pid`: a per-sample ratio `linear|reciprocal`, with
      `pid_saturation`, `relative_tolerance` and `min_agreement` all required.

    `verify_channel` (now `catalog=` required, plus `exit_session`/grace)
    reads each relation's channels one window per reply. A Mode 01 relation
    exits the session (0x81), waits the access block's grace, reads the PID
    and re-enters 0xC0. A relation verifies **every channel it reads**
    (`memory_checks`, `is_memory_checkable`). It counts as a pass only if
    three things hold:
    - it agrees on at least `min_agreement` of the samples;
    - it **falls below that on both neighbouring slices**, which the widened
      read carries;
    - it was **exercised**, which depends on the kind:
      - value kinds: the compared value (the result for sum and divide, the
        bound for ordering) was non-zero on an agreeing sample;
      - invariants: at least 2 distinct raw values. I chose this stricter
        rule; it is satisfiable on real data where the byte moves, such as
        0x249B and an active retard;
      - `iff`: the condition was seen true and false;
      - `implies`: the condition was seen true.

    Otherwise the verdict is `unverifiable`, never `pass`. A `fail` means
    bytes were read and disagreed. Bus silence is `unverifiable` with a
    name: `session_entry_refused`, `reads_unanswered`,
    `session_exit_refused`, `pid_unanswered`, `session_reentry_refused`.

    The dashboard route claims the ECM session as `memory_verify` (`tx_gate`,
    the ROM-backup pattern), which pauses the poll loop's Mode 01/22/RAM
    polling. The K-line reader yields the ECM to it as it does to the backup.
    The route works as follows:
    - it lets in-flight traffic settle for 0.5 s;
    - it enters 0xC0 with one retry, else 503 `session_entry_refused`;
    - it renews the claim before every sample;
    - it stops with a 409 naming the claimant as soon as `memory_verify` is
      not the only claim, recording nothing;
    - it refuses by name while passive is armed, another holder owns the
      session, or another verify runs (a non-blocking lock plus
      `claim_held`).

    The `DidMap` loader refuses, naming the channel and relation:
    - an unknown operand;
    - `same_reply` across windows, or `cross_read` within one window;
    - a cross-window relation with no stated `min_agreement`;
    - a read over the 63-byte ceiling, neighbours included;
    - a self-operand;
    - a flag relation that never reads its own channel.

    Declared in `data/dids.yaml` (D03.38 hand checks):
    - `ECM_KNOCK_LEARN`: sum of halves.
    - `ECM_KNOCK_RETARD`: ≤ 0.
    - `ECM_KNOCK_RETARD_DEG`: `trunc(0x1254/256)`, cross-read, 0.90.
    - `ECM_KNOCK_RETARD_LIMIT`: ≥ −0x26D2.
    - `ECM_KNOCK_RETARD_TARGET`: = −1280 ⇔ 0x479D bit 7.
    - `ECM_KS_FAULT_FLAGS`: bit 7 ⇔ OR of item byte 2 bit 0.
    - `ECM_KNOCK_WINDOW_INDEX`: 0..6.
    - `ECM_KNOCK_FLAGS_BY_INDEX`: `& 0xC0 == 0`.
    - `ECM_AF_LAMBDA_B1`/`B2`: PID 0x24/0x28 A,B = word·16384/3675, 3 %,
      0.70 (both provisional).
      *Operator decision 2026-10-08: keep the provisional λ tolerance (3 %,
      min_agreement 0.70) and `ECM_KNOCK_RETARD_DEG`'s cross-read 0.90, and
      adjust them from the key-on read.* The 0x283F bit 6 ⇒ 0x1400 relation
      is not declared: the firmware session (2026-10-08) found the latch sets
      at 0x1400 <= -8192 and clears above +8192 only while two unidentified
      gate bits (2876.6, 4782.5) allow it, and 0x1400 and 0x283F cannot be
      read in one window, so a hard relation would fail correct addresses
      near the thresholds (catalog notes on `ECM_HIGH_DET_FLAGS`).
    - `ECM_AF_LEARNED_OFFSET_B1`/`B2`: ±500.

    Every `fuel_target` member is now checkable, so a passing read restamps
    it without `--carry-unverifiable`
    (`test_every_fuel_target_member_is_checkable_so_a_restamp_needs_no_carry`,
    `test_a_window_is_stamped_whole_or_not_at_all`). Whether they *pass* is
    data: the knock bytes discriminate only while a retard is applied (the
    −5° fallback counts). At rest they read `unverifiable`, zero matching
    zero.

    **Still without a relation, with the reason recorded in each entry's
    notes:**
    - 0x2338 − 0x2332 = 0x274E: 0x274E is uncatalogued.
    - intensity > threshold ⇔ 0x24A8 bit: windows 0xA8 apart, both
      rewritten every firing window.
    - 0x28AE bit 5 under light load: needs an operating-point gate.
    - 0x2CEF/2CF0 50..199: a real fault would read as a moved address.
    - 0x283F bit 6 ∧ ¬0x479D bit 7 ⇒ 0x1400 ≥ 8192: the latch may outlive
      the metric.
    - 0x46B2 = 7350: a constant, and 0x46B4 beside it is 7350 too.
    - 0x46AC = 0x46B2: constant against constant.

    So these channels stay uncheckable:
    - `knock_chain`'s other three;
    - `ECM_KNOCK_HIGHDET_METRIC`, `ECM_HIGH_DET_FLAGS`;
    - `ECM_KNOCK_EVENT_COUNT`, thresholds, intensities;
    - `knock_flags`, `ECM_KS1/2_LEVEL`;
    - the `lambda_target` trio, `ECM_AF_BARO_GAIN_K`;
    - the injector channels.

    **Fail-on-neighbour evidence** is in `tests/test_memory_relations.py`
    (65, including the 2026-10-08 adversarial reviews' cases: constant
    subject, truncate-to-zero, never-set flag, bus refusal, route claim,
    foreign claim mid-verify, per-sample renewal, entry retry, concurrent
    verify). `test_kline_collector.py`/`test_kline_actions.py` show the
    K-line yield. Each kind passes on a synthetic RAM image, its neighbours'
    agreements are below the samples, and a moved or floored word fails.
    The weak cases come back `unverifiable`: ordering with no retard,
    flag-clear ⇔, banks moving together, a range beside a small counter,
    and zero = trunc(0/256). Stored 2026-10-04 ram-study slices (committed
    fixtures):
    - same-reply `trunc(0x11D8/4)` = 0x11CA held 180/180, and 0/180 at
      0x11C9, 0x11CB and 0x11D6;
    - 0x26AE against Mode 01 rpm across the session exits, as a 5 % ratio:
      154/174 pairs, 0/174 at 0x26B0, and a 0.5 % tolerance fails. 0x26AC
      was never stored, so that check reads honestly as `unverifiable`.

    Tests: `test_verify_memory_channels.py`, `test_memory_channels.py`,
    `test_memory_catalog_knock_lambda.py`, `test_memory_catalog_injector.py`,
    `test_memory_relations.py`, `test_layering.py`, `test_ecu_calibration_id.py`,
    `test_operational_calibration_scope.py`, `test_tune_registry_merge.py`
    (260 passed). `CalibrationPage.test.tsx` passed 9; it names a relation
    "via" its owner. The tolerances and the pass rates on the truck remain
    the key-on read's job.
  - [x] **Per-read cadence: desk work done 2026-10-08, truck rate owed.** The
    fault reads want about 1 Hz, but every memory read rode one rate and a
    profile could not weight memory channels.
    - `CaptureProfile.memory_rate_weights` (channel → weight, missing = 1.0)
      has the same meaning as `mode22_rate_weights`. In a windowed profile it
      picks which session windows the read is due in (1.0 = every window,
      never two per window). In a continuous profile it scales the rate. Under
      `target_rate_hz` it multiplies the target.
    - **Window mapping: the fastest selected member's weight.** One 0x23 read
      serves every member, so it cannot run at two rates. The fastest
      member's demand must be met, and slower members get extra samples at no
      bus cost. Keyed by channel rather than window because a window is
      catalog layout: it is widened or regrouped without its channels being
      renamed, and a profile names channels everywhere else.
      `ProfileView.memory_window_weight` is used by the scheduler, the preview
      and `request_absolute_rate_hz`.
    - **Refusals, by name:**
      - a weight on a channel `memory:` does not select;
      - a key naming a window: "is a memory window, not a channel", listing
        its members;
      - a weight that is not positive or not finite.

      An unknown channel fails the existing catalog check (b986365) at save
      and at daemon start.
    - **Three defects found and fixed on the way:**
      - `_controller_engaged` asked `tuning_profile.mode22()` only, so a
        memory-only profile never opened the session. Every read went out in
        the default session, was refused and backed off. It now asks
        `tuning_profile.owns_session()`.
      - The preview keyed plan members by (service, request id, identifier),
        and every 0x23 request carries identifier 0. All selected windows
        therefore folded into one member, and three windows previewed as one
        read. Members are now keyed by address, with the read length as the
        reply width.
      - The capture-quality verdict scored a memory member under its window
        name (`mem:fuel_target`). The ledger and the rows count readings
        (`mem:<NAME>`), so the window was always `missing` and landed in
        `unanswered_input`, even when every read was answered (verifier
        repro, `tuning` on the fake clock).
        - A plan member now carries `decoded_channels`: the profile's
          selected members of the read.
        - `capture_quality._build_channels` scores one row per decoded
          channel, under its catalog name. It falls back to the
          `mem:<NAME>_SENTINEL` row for a read that only returned the
          sentinel.
        - `profile_requests` lists memory channels, so a verdict rebuilt from
          rows counts them.
        - A plan written before this has one member at identifier 0 and no
          `decoded_channels`. It expands to the profile's whole `memory:`
          selection at that member's rate, which is exact for those plans:
          every memory read then ran at one rate.
        - Stored `_capture-ram-study-2026-10-04T204557`, recomputed read-only:
          its sidecar had `mem:fuel_target missing` (requested 0.2 Hz). It
          now scores 12 rows at 0.14 Hz each, `shortfall`, in line with that
          capture's other DIDs, and no memory row in `unanswered_input`.
    - **Budget:** memory demand is charged per window in the preview, which
      shows `fallback_feasible`/`best_case_feasible` the same way it does for
      DIDs.
    - **Backward compatibility:**
      - Every tracked Xterra profile has `{}` weights.
      - Its digest is unchanged: an empty field is left out of
        `profile_digest`, so pre-field `base_digests` still match.
      - `tuning`'s two windows still read at 2.0 Hz.
      - A Pi-style overrides file without the key loads and wins by name, and
        it raises no "repo changed" warning.
    - **Dashboard:** the editor gets memory weights in the advanced weights
      picker, and deselecting a channel drops its weight.
    - **Evidence:** `tests/test_memory_rate_weights.py` (23). It runs the fake
      clock against the `ScriptedEcu`, which now answers 0x23. The fault
      window ran at 1.00 Hz and another window at 0.20 Hz. Both rates are
      medians of consecutive-reading gaps over 60 s, ≥55 and ≥10 pairs. The
      session entries stayed on the 0.25 s grid, and unweighted reads ran
      every window at 4 Hz.
      - Live and stop verdicts: no memory row is `missing` when the reads were
        answered, and a silent window is still reported missing. The stop
        verdict runs `recompute_capture_quality` over a new plan and over a
        pre-change plan (identifier 0).
      - `test_capture_profile_preview_parity.py` gains `memory-weighted`.
      - The dashboard scheduler fixture was regenerated (only
        `decoded_channels` was added).
      - The `CaptureProfileEditor` vitest shows that deselecting a memory
        channel drops its weight.
      - 758 passed across the scheduler, capture-profile, parity, memory,
        layering, tuning-profile, hybrid, capture-quality, capture-coverage,
        capture-finalize and polling-plan files. Vitest passed 586, then 38
        on re-run of the scheduler, editor and runner files.
    - **Owed (truck):** arm a profile weighting the knock-fault windows to
      about 1 Hz. Read the achieved rate from the session's own rows as
      consecutive pairs, not `achieved_hz_capture` count ÷ wall-clock.
    - **2026-11-08 truck rate, closed `[x]`, relaxed (operator authorization
      2026-11-08: close with data close enough).** The `ltft-gate` capture
      (`profile_snapshot.memory_rate_weights`: 0.5 on the knock-retard and
      LTFT gate windows, 0.25 on the purge / learn-state windows, 0.125 on the
      knock-fault and LTFT map/count windows; `target_rate_hz` 4.0,
      `did_cycle_seconds` 0.5) was armed for two captures. Rates are 1 / median
      of consecutive *acquisition-time* pairs (`metadata_json.acquired_at`,
      distinct per read; pairs ≥ 10 s dropped, so key-off gaps do not count),
      per `mem:` channel, script `checklist/mem.py`:

      | weight | channels | `…T182054` (56 min) | `…T191721` (58 min) | median p95 interval |
      |---|---|---|---|---|
      | 0.125 | 23 | 0.4999-0.5004 Hz | 0.4984-0.5004 Hz | 2.25-2.35 s |
      | 0.25 | 17 | 0.9959-0.9984 Hz | 0.9953-0.9972 Hz | 1.31-1.34 s |
      | 0.5 | 8 | 1.0065-1.0076 Hz | 1.027-1.034 Hz | 1.27 s |
      | none (1.0) `fuel_target` | 4 | 1.0066-1.0096 Hz | 1.0275-1.0297 Hz | 1.25 s |

      **Below the ceiling, rate is proportional to weight, and equals
      `target_rate_hz` × weight exactly:** 0.125 → 0.50 Hz and 0.25 → 1.00 Hz
      (ratio 1.99-2.00; 0.5 and 4.0 × weight predict 2.0 and 4.0 Hz).
      **Weights 0.5 and 1.0 are capped at ~1.0 Hz**, not 2 / 4 Hz: the cadence
      rule is `min(target × weight, 1 / window period)`
      (`cadence._request_rate_hz`), and with the 36 extra windows the session
      window period ran ~1 s (3,448 windows opened by 20:05Z, about one a
      second) against the 0.5 s the DID cycle asks for. So at this profile a weight of 0.5
      buys nothing over 1.0: the profile's own capacity note ("expect the 4 Hz
      batch to slow") is the same fact. The knock-fault windows weighted for
      ~1 Hz (0.5 here) achieved **1.007 Hz / 1.028 Hz** (median pairs, p95
      interval 1.27 s); every read in this profile held members of one weight, so
      the fastest-member rule for a mixed-weight read was not exercised.
      **Relaxed:** the profile weights the knock windows at 0.5 (capped at
      ~1 Hz by the window period), not a profile built to hit exactly
      1 Hz; the ≈ 1 Hz target is met by the ceiling, so the *proportionality*
      below the ceiling (0.125 vs 0.25: 2.00 ×) is the part that tests the
      weight mechanism. A knock-focused profile at its own weights was not run.
  - [~] **0xFFFF2842 renamed `ECM_TIMING_TRIM_SELECT_FLAGS` →
    `ECM_KNOCK_TRIM_DIFF_FLAGS` (2026-10-05; operator: "fix
    ECM_TIMING_TRIM_SELECT_FLAGS to be correct"). Code done, store migration
    owed.** Bit 2 is the knock task's copy of 0x2840 bit 6 (0x04A5D2–0x04A5FE;
    0x2840 is the cycle snapshot of 0x283F, stored at 0x021220), so it mirrors
    HI DET and gates the trim-difference branch (0x04A512, `tst #4`); the trim
    table is chosen by 0x283F bit 6 (0x04E6FE). No alias (2026-09-18 policy).
    `data/dids.yaml`: entry renamed, address/window/length/revision kept,
    evidence and notes rewritten (the 191 constant-0x04 reads of 2026-10-04 and
    the unconfirmed "HI DET stayed latched, trim B" inference kept; the other
    writes the listing annotates, 0x045320/0x045324, 0x063652, 0x0637B0 and
    0x064242, touch bits 7, 4 and 3 only). `tools/migrate_channel_names.py`
    gained a `memory_channels` map section, checked against the catalog's
    `memory:` (target present, source gone from memory and DIDs), covering
    `mem:<NAME>`, `mem:<NAME>_SENTINEL`, `obd:mem:<NAME>[__suffix]`, bare names,
    and the K-line `<module>:MEM_<NAME>`, `MEM_<NAME>` and `kline:<NAME>` forms.
    Map: `tools/channel_renames/2026-10-05-knock-trim-diff-flags.yaml`. A saved
    profile naming a memory channel the catalog lacks used to be dropped without
    a word (`CanObdSpec.from_config` filtered the catalog); it is now refused
    by name at daemon start, in the preview and at capture start
    (`profile_memory_union`) and at save (`CaptureProfileStore._check_memory`).
    Dry runs (no `--apply`): `models/`, `models-0920a`, `models-0920b` 0;
    `sessions-0917`…`0921c` 0; `~/canary-data/sessions` 192 names in 192 files
    (191 Parquet rows, one per chunk, and the `capture_profile.json` of
    `_capture-ram-study-2026-10-04T204557`); a copy of the Pi's `ram-study`
    override 1 name. Tests: `test_migrate_channel_names.py` (memory map: Parquet,
    profile YAML, specs, JSON sidecars, catalog refusals, whole-token boundary),
    `test_memory_channels.py` (unknown name refused at plan, preview and save),
    `test_the_2842_byte_is_named_for_the_knock_gate_and_the_old_name_is_refused`.
    **Owed:** `--apply --daemon-stopped` on `~/canary-data/sessions` and on the
    Pi (`--config /etc/canary/canary.yaml`, which takes in the overrides file).
    The Pi's `ram-study` override must be migrated **before** the Pi restarts on
    this code, or the daemon refuses to start. Left as records:
    `data/analyses/**` and the `tests/fixtures/fuel_comp/_capture-ram-study-…`
    session snapshot.

- [x] **D03.39 Resume the parked λ-chain transfer/baro-gain rework (canary-18,
  unfinished).** *Closed 2026-10-07: superseded by
  [B06.7](B06-lambda-delivery-error.md), which rebuilt the work on current HEAD
  instead of applying the old patch (do not `git apply` it). The voltage
  transfer is ECM table 0xE644 with provenance, the A/F baro gain is applied
  per sample, and every caller is migrated; see B06.7 for evidence and what
  remains.* *Parked 2026-10-05 by operator decision.* The firmware session
  ended with an uncommitted, half-migrated change that kept every session's
  pre-commit hook red. The exact diff (against 022614a-era HEAD) is saved at
  `~/canary-wip/2026-10-05-canary18-lambda-chain-transfer-baro-gain.patch`,
  with a README beside it. Scope: `src/signals/afr.py` models the A/F voltage
  transfer from the ECM's own table with provenance, plus the A/F sensor baro
  gain (k at 0xFFFF36BB; firmware doc, λ chain); `lambda_delivery.py`;
  `afr_calibration.yaml`. **To finish:** `git apply --3way` the patch; migrate
  the `voltage_to_afr(voltage, baro, AfrCalibration)` callers
  (`tuning_frame.py`, `tuning_digest.py`, `lambda_delivery.py`); give the
  `test_voltage_transfer_censoring_is_a_named_state` fixture a provenance;
  drop the unused `BaroSamples` import; then run the lambda/tuning tests.

- [~] **D03.40 Apply the firmware Mode 22 DID audit to `data/dids.yaml`
  (2026-10-07).** The firmware session checked all 172 CAN DIDs against both
  pointer tables and the 2026-09-17 did_log sweeps (`~/ecu-re/out/did_audit.yaml`,
  `~/ecu-re/NOTES.md` session 8, 2026-10-05; summary in
  [`ecu-firmware-analysis.md`](../../ecu-firmware-analysis.md), "Mode 22 DID
  audit"). Operator: "all, one migration". The catalog side is done; the
  measurements below are open.
  - [x] **Two wrong identities, renamed with no alias.** 0x1107 is the final
    timing byte 0xFFFF11FF (the PID 0x0E / 0x110A byte), not A/F heater duty:
    `ECM_AF_SENSOR_HEATER_DUTY` → `ECM_IGNITION_TIMING_1107`, decode ×0.5 % →
    110 − raw °BTDC. 0x123F is an increment-only monitor run counter (u16
    0xFFFF5268), not intake air temperature: `ECM_INTAKE_AIR_TEMP` →
    `ECM_MONITOR_RUN_COUNTER_123F`, offset −40 °C → raw count. Map:
    `tools/channel_renames/2026-10-07-did-audit.yaml`. Old evidence moved to
    `evidence_against`; the 2026-08-08 timing reading that had been rejected
    moved to `evidence`.
  - [x] **0x1117 rescaled 100/256 → 100/255 %** (the PID 0x04 byte, sent
    unchanged).
  - [x] **0x1106 is the IAT, `confirmed`** (raw − 50; the PID 0x0F byte is this
    byte − 10). Conflict note replaced. It keeps the `_1106` suffix so a config
    naming the old IAT name fails at parse instead of switching DIDs.
  - [x] **0x1134 updated, not renamed.** Firmware evidence (high byte of Q14
    0xFFFF26C0, the Target-AFR-map enrichment request; φ = raw/64; 64 below
    cell ~0x82; an rpm table under HI DET), `hypothesized` → `high`. The
    `mem:ECM_TARGET_EQUIV_RATIO` known answer (0x1134 = max(64, word >> 8))
    now says it holds only while the map request sets the final word, never
    in after-start or coolant enrichment. `lambda_delivery.yaml`'s refusal
    reason was corrected the same way.
  - [x] **Confirmed and tightened, with CAN firmware evidence:** 0x1123–0x1126
    (1 %/count, truncated high byte; `medium` → `high`), 0x1209 (the PID 0x10
    word; `high` → `confirmed`, response width 2 recorded), 0x1219 (1 rpm per
    count in 3.125-rpm steps), 0x122A (the ASCD speed), 0x110A (pointer-table
    evidence added).
  - [x] **Added (catalogued only; no profile polls them, no model reads
    them):** 0x1103 `ECM_BATTERY_VOLTAGE`, 0x1104 `ECM_FUEL_TANK_TEMP`
    (inferred), 0x1115/0x1116 `ECM_EVAP_PRESSURE_SLOW`/`ECM_EVAP_PRESSURE`,
    0x1129 `ECM_BARO_PRESSURE`, 0x111A/0x111B `ECM_REAR_O2_V_B1/B2`, 0x111E
    `ECM_RELATIVE_THROTTLE_POS`, 0x114E/0x114F `ECM_AF_SENSOR_HEATER_DUTY_B1/B2`,
    0x1204 `ECM_MAF_SENSOR_V`, 0x120B `ECM_FIRST_FLAGGED_DTC`, 0x120E
    `ECM_ACCEL_PEDAL_POS_2`, 0x122B/0x122C `ECM_ASCD_SET_SPEED` /
    `ECM_ASCD_SPEED_DEVIATION`, 0x1246 `ECM_BATTERY_CURRENT_SENSOR_COUNT`.
    Signed words (0x122C) carry no scale: the DID schema decodes unsigned.
    0x1249/0x124A were not duplicated: they are the words
    `mem:ECM_AF_LEARNED_OFFSET_B1/B2` already decode signed, and those
    entries now cite the DIDs.
  - [x] **The transport-split claim corrected.** Both pointer tables send every
    shared DID to the same variable except 0x1247, 0x1248 and 0x1315, so
    K-line 0x1102 is speed too; the LTFT names were UpRev's labels. Fixed in
    the catalog comments and 0x1102/0x1123/0x1125 notes, `Evidence.transport`'s
    docstring, `ecu-firmware-analysis.md`, `can-pids-dids.md`,
    `observation_aliases.yaml`, the ground-truth expectations and
    investigation case, the vehicle skill (SKILL.md, diagnostic-access.md) and
    D03.36. **The refusal stays** (`DidEntry._kline_evidence_cannot_name_a_can_did`):
    the real failure was trusting a third-party tool's label, which a K-line-only
    entry still is. Its docstring and message now say that and no longer claim
    the tables differ (`test_kline_evidence_alone_cannot_establish_a_can_did`
    asserts "differ" is absent).
  - [x] **Known answers.** `did_pins` gained 0x1107 and 0x110A (same RAM byte;
    `test_dids_sharing_a_ram_byte_decode_identically`), 0x1117 (PID 0x04 byte;
    exact agreement, which 100/256 fails), 0x1106 (PID 0x0F = byte − 10) and
    0x111E (PID 0x45). `test_the_2026_10_07_did_audit_corrections_hold` pins
    names, decodes and that both old names are refused;
    `test_signed_audit_dids_are_not_given_an_unsigned_scale`;
    `test_the_did_audit_map_agrees_with_the_catalog` and a saved-profile
    rewrite test (the 0x123F name must not clip `ECM_INTAKE_AIR_TEMP_1106`).
  - [x] **K-line widths.** 0x1117, 0x1204, 0x1209 and 0x122B now have catalog
    widths (1/2/2/2), so UpRev's K-line poll reply splits from the catalog
    alone; the K-line protocol/sniff tests changed from "refused" to "splits",
    and the "no width" refusal tests use uncatalogued 0x1108.
  - [x] **Served features and callers.** No manifest (repo `models/**` incl.
    `_candidates`, `~/canary-data/models-0920a/b`, the deployed-manifest
    fixture) names 0x1107, 0x123F, 0x1117 or 0x1106 in any form; 0x1134
    appears only in IF `feature_health.dropped_constant_columns`. So no
    served feature is renamed or rescaled. Callers: `src/validation/scenarios.py`
    (`FUEL_DIDS`, fuel-did-research `required_tuple` → the 0x1106 IAT),
    `tests/test_capture_profiles.py`, `docs/roadmap/engine-analysis-and-mapping.md`.
    Left as records: `capture_profiles.archive.yaml` (loaded by nothing; 2
    names) and `data/analyses/**`. Dry run of the map: repo `models`,
    `models-0920a/b` 0 names in 33 files; every `~/canary-data/sessions*`
    root 0 in 92,079 files; repo profiles, configs and test fixtures 2 names
    in 1 file (the archive).
  - [ ] **Owed: `--apply --daemon-stopped` on the Pi** (`--config
    /etc/canary/canary.yaml`, which includes the capture-profile overrides)
    before it restarts on this code. Its `obd_dids` names none of the old
    names, but a saved override (a fuel-did-research copy, for one) could;
    the daemon refuses an unknown DID name.
  - [x] **Operator: rename 0x1134?** Yes (operator, 2026-10-08), no alias:
    `ECM_ENRICHMENT_TARGET_EQUIV_RATIO_CANDIDATE` → `ECM_ENRICHMENT_PHI_REQUEST`.
    Map `tools/channel_renames/2026-10-08-enrichment-phi.yaml` also carries
    the pre-09-18 `ECM_TARGET_EQUIV_RATIO_CANDIDATE` (VTC precedent), and the
    09-18 map's 0x1134 entry was re-pointed at the new name so it stays
    runnable. Callers: `lambda_delivery.yaml` (refused_commanded),
    `ecu_application_model.yaml` (0x1134 → 0xFFFF26C0 `observed_as` promoted
    `inferred` → `code`: the pointer table is read from the code; no same-read
    0xFFFF26C0 check yet), `test_dids.py`, `test_capture_profiles.py`,
    `test_migrate_channel_names.py`; the rom-trace capture-profile fixture (3
    names) and the deployed IF manifest fixture (8) migrated with the tool.
    Served: 0x1134 appears only in IF `feature_health` dropped lists (repo
    `models/**`, `models-0920a`; `models-0920b` is empty), manifest and joblib
    payload alike. Dry run of the new map: repo `models` 16 names in 2 of 29
    files, `models-0920a` 16 in 2 of 4; sessions: `sessions` 52 in 22 of
    78,057 files, `sessions-0918` 32 in 12 of 7,950, `sessions-0921` 4 in 2 of
    292, `-0917`/`-0920`/`-0921b`/`-0921c` 0. Tests:
    `test_the_2026_10_08_enrichment_phi_rename_holds` (old name refused),
    `test_the_enrichment_phi_map_agrees_with_the_catalog`,
    `test_the_enrichment_phi_map_renames_both_earlier_names_whole`, and
    `test_the_shipped_map_agrees_with_the_catalog` on the re-pointed 09-18 map.
    **Owed:** the same `--apply --daemon-stopped` on the Pi (models dir,
    sessions root, `--config /etc/canary/canary.yaml`) before it restarts on
    this code, and on the laptop stores above when the operator wants them.
  - [ ] **0x120D/0x120F/0x1210 volts per count.** Counts are code-exact;
    5/1024 V assumes a 5.000 V A/D reference, while the ECM's own PID 0x24
    voltage uses 5.0024 mV per count (×0.005). Settle: a DMM on the TPS1
    signal pin (or AVref) at a fixed throttle while reading 0x120F. This also
    decides D03.36's held TPS rescale, which assumed 5/1024.
  - [ ] **0x110D** (state byte 0xFFFF26BD, 61 on warm reads). Settle: read it
    through a cold start and see what it tracks.
  - [ ] **0x1159 / 0x115A** (mux AN15 slot 3 / AN3 slot 8). Settle: unplug
    candidate sensors at key-on, engine off and watch which moves.
  - [ ] **0x117B** (likely alternator duty, 91 typical). Settle: run the ALT
    DUTY active test, or load the electrics (headlights + blower) at idle.
  - [ ] **0x1218 / 0x1223** (idle air demand / AN3 slot 5, refrigerant
    pressure candidates). Settle: A/C on and off at warm idle.
  - [x] **0x120B reads P0328 while driving: is the knock fallback engaged?**
    Bit 0 of the KS items is what `mem:ECM_KS_FAULT_FLAGS` bit 7 ORs, and that
    bit forces the fixed −5° fallback, so it is likely engaged on every drive
    since at least 2026-08-05 (reopens the D03.38 verdict). Settle, no writes:
    at KOEO and engine running read 0xFFFF479D bit 7, 0xFFFF677D–677F (item
    0xAB status), 0xFFFF2CEF (KS1 DC level, band 50–199), and 0xFFFF13FE under
    light load.
    *Closed 2026-10-08 by a live read: yes, engaged.* CAN 0x7E0, session
    0xC0, service 0x23, CANary passive-armed. KOEO (~17:05Z): every knock
    byte 0 (479D 00, 677A–6785 12×00, 2CEF/2CF0 0000, 13FE 0000, 1400 0000,
    283F 00, 28AE 00, 1254 0000), DID 0x120B 0000, 2876 0x60, 4782 0x10.
    **Idle, engine running (~17:10Z), two reads — KS1 fault confirmed live:**
    DID 0x120B = 0328 (P0328); 479D = 0x80 (bit 7 set); item 0xAB (677D–677F)
    = 00 C0 4B, i.e. byte 1 bit 7 failing now and byte 2 0x4B early flag set,
    pending, not confirmed (items 0xAA/0xAC/0xAD 00 00 00); 2CEF = 0xFF (KS1
    pegged high, outside 50–199); 2CF0 = 0x7F then 0x80 (KS2 mid-scale,
    healthy); 13FE = 0xFB00 (the fixed −5° retard target); 1400 = 0; 283F =
    0x40 (HI DET set, so timing-trim table B); 28AE = 0x00 (knock loop not
    active); 1254 = 0; 2876 = 0x60, 4782 = 0x10 (the HI DET gate on). All of
    it matches the firmware: 479D bit 7 forces 13FE = 0xFB00 and the HI DET
    latch. **Upshot:** timing runs in the knock-sensor fallback (fixed −5°
    target, trim B, no knock loop) on every running drive, so timing work is
    meaningless until the KS1 circuit is fixed; the sensors and harness are
    being replaced in the supercharger build. The read raised the channels it
    covered to `medium` with `live_read` evidence in `data/dids.yaml`, and
    `tuning` now polls those windows (the knock_chain, knock_detect and
    knock_intensity windows, the learned words 0x124E–1252 and 0x28AA stay
    hypothesized and out of every profile).
    *Addendum 2026-10-09: the fault predates 2026-08-05 and the 08-01 unplug
    test.* The ECM answered the scan tool's `17 FF 00` with `57 01 03 28 C0`
    (P0328) on 2026-07-27 and 2026-09-24. UpRev's fallback signature is in
    every UpRev log since 2026-07-26. Next: measure knock sensor 1 from the
    sensor side (terminal 1 to block, 532–588 kΩ), then wiggle-test at idle.
    See `docs/ecu-firmware-analysis.md`, "Knock monitoring".

- [x] **D03.41 Measured AFR's declaration claims the A/F voltage suffices
  without baro (found 2026-10-08, B08.1 follow-up).** `derived:AFR_B1/B2`
  and `AFR_CENSORED_B1/B2` declare one group, `(wideband lambda PID, A/F
  voltage DID)`, with baro only *optional*
  (`vehicle_signals/nissan_xterra_2008.py` `DERIVED_CHANNEL_INPUTS` /
  `DERIVED_CHANNEL_OPTIONAL_INPUTS`). `AFR_ERROR_B1/B2` inherits that through
  its `derived:AFR_B*` group. But since B06.7 the voltage route converts each
  row at the ECM's barometric gain, and `append_afr_channels` withholds every
  row with no fresh baro (`baro_missing`/`baro_stale`). The voltage DID alone
  therefore never produces AFR.
  - **Repro:** `channel_satisfied` accepts a profile that can never produce
    AFR, for example a tuning-style profile with `mode01={'RPM'}` and
    `mode22=[ECM_AF_SENSOR_V_B1, ECM_AF_SENSOR_V_B2, ECM_AIR_CHARGE]`:
    ```python
    p = CaptureProfile(name="repro", storage_prefix="_capture-repro",
                       mode01={"RPM": 1.0},
                       mode22=["ECM_AF_SENSOR_V_B1", "ECM_AF_SENSOR_V_B2", "ECM_AIR_CHARGE"])
    channel_satisfied("derived:AFR_B1", p, frozenset())  # True; should be False
    ```
    This also returns True for `AFR_CENSORED_B1` and `AFR_ERROR_B1`.
    `LAMBDA_BANK_BALANCE_AF_VOLTAGE_BARO`, which already lists baro as a
    required group, correctly returns False. Neither real profile is affected
    today, because both poll Mode 01 baro or the lambda PID.
  - **Fix (conjunctive form, like `FUEL_RATE_LPH`):** `(("obd:O2_WR_LAMBDA_S1",
    "did:ECM_AF_SENSOR_V_B1"), ("obd:O2_WR_LAMBDA_S1", "obd:BAROMETRIC_PRESSURE"))`,
    with S5 / `_B2` for bank 2, and baro dropped from the optional table.
    `src.signals.derived._VEHICLE_INPUTS` builds the entry from the vehicle
    declarations, and the import check requires the literal to match it, so
    both change together. Check the capture-profile and serving
    (`_unserved_group`) consumers, because the change makes them stricter on
    purpose.
  - **Why the B08.1 drift test cannot see it:**
    `tests/test_derived_inputs_declared.py::declaration_problems` passes a
    channel when *any* frame produces it (`produced = any(...)`). It does not
    check producibility. Add a per-context assertion that every minimal frame
    of the declaration produces the channel. That would have failed here on
    the frame `{did:ECM_AF_SENSOR_V_B1}`.
  - **Done 2026-10-13.** `derived:AFR_B1/B2` and `AFR_CENSORED_B1/B2` are
    now `((lambda, V), (lambda, baro))` in both
    `nissan_xterra_2008.DERIVED_CHANNEL_INPUTS` and the builder
    `src.signals.derived._VEHICLE_INPUTS` (baro group present where a vehicle
    declares `AF_SENSOR_BARO`, like `LAMBDA_BANK_BALANCE_AF_VOLTAGE_BARO`).
    The vehicle `DERIVED_CHANNEL_OPTIONAL_INPUTS` table is gone;
    `_VEHICLE_OPTIONAL_INPUTS` is kept empty so a vehicle literal optional
    entry still refuses at import. Leaf sets are unchanged, so trainability,
    `derived_channel_inputs` and `engine_dynamics` see the same inputs;
    `channel_satisfied` and `FeaturePolicy._unserved_group` iterate groups and
    become stricter with no code change. `declaration_problems` now reports
    `not produced from the minimal frame [...]` for every base context that
    produces nothing. Before the fix the new check failed exactly the four
    AFR families (`AFR_B1+AFR_CENSORED_B1`, `AFR_B2+…`, `AFR_ERROR_B1`,
    `AFR_ERROR_B2`, log: `baro_missing: derived:AFR_B2 needs
    obd:BAROMETRIC_PRESSURE`) and nothing else. Evidence:
    `tests/test_derived_inputs_declared.py`
    `::test_a_declared_context_that_cannot_produce_the_channel_is_caught`
    (old declaration → `not produced from the minimal frame
    ['did:ECM_AF_SENSOR_V_B1']`),
    `::test_a_profile_without_baro_or_lambda_cannot_supply_measured_afr`
    (the repro profile: `channel_satisfied` False and `_unserved_group` not
    None for AFR/CENSORED/ERROR B1+B2 and the baro balance; the old
    declaration returned True for all three B1 channels),
    `::test_either_route_supplies_measured_afr` (lambda, or voltage+baro);
    `test_a_missing_alternative_is_caught` keeps baro in the frame so the
    undeclared voltage read is still named. `pytest -n 4` over
    test_derived_inputs_declared, test_afr, test_anomaly_signals_table,
    test_artifact_lineage, test_base_fuel_schedule, test_capture_profiles,
    test_feature_pipeline_incremental, test_feature_policy,
    test_featurization_profile_scope, test_target_afr_load_axis,
    test_train_anomaly_model, test_trainability, test_xterra_default_poll_list,
    test_layering, test_state_dir: 371 passed. Side effect: a model built
    2026-10-08..10-13 that reads `derived:AFR_*` now audits as
    `definition_changed` (the 09-20/09-24 deployed fixtures predate the
    literal form and are unaffected).

## Deferred by decision, not blocked

- [x] **D01.4 Get the feature build off the event loop — implemented; Pi
  measurement remains.** `source.tick()` ran in phase 1 on the loop and held
  it for **169 ms** median (was 332 ms before D01.8). The live source now
  separates a cheap, serialized `prepare_tick` from the pure pandas transform
  and `apply_built_tick`: the daemon retains ownership of observations, the
  bounded ring and trip-integral side effects; one persistent spawned process
  runs the exact shared `featurize_wide_frame` transform and returns only the
  newest feature row, operating point and fallback verdict. This avoids a
  thread's GIL contention without duplicating the training transform or
  sending one IPC message per telemetry record. A source generation attached
  to each request makes an in-flight pre-reset/replay result unapplyable.

  `models.out_of_process_feature_build` defaults on and is explicitly on in
  the Pi example; the general example disables it for deterministic
  single-process development. The timing recorder now separates `feature_wait`
  from `loop_occupancy`, so elapsed worker time is not falsely reported as
  event-loop occupation. Spawn-process parity and reset-race tests pin the two
  load-bearing contracts. **Still owed:** deploy and compare `feature_wait`,
  `loop_occupancy`, request rate, HTTP latency and missed ticks over a matched
  live window before closing the checkbox. The worker still recomputes the
  bounded rolling frame; this change removes collection-loop starvation, not
  the transform's total CPU cost.
  **No longer a deadline argument.** The tick fits its budget: 439 ms median at
  idle, 320 ms under the heavier `tuning-did` load, and D01.9 now overlaps the
  snapshot with the score. What remains is that the loop is occupied ~17 % of
  each second, which is a `/status` latency and collection-jitter argument, not
  a missed-deadline one.
  Design it before building it; this is still the larger half of the problem.
  Reopen it if a future drive puts the tick back over budget.
  **2026-11-08 live on the Pi, closed `[x]`, relaxed (operator authorization
  2026-11-08: close with data close enough).** Daemon c4458e5b with
  `out_of_process_feature_build` on, `ltft-gate` capture (154 active requests,
  ~78-84 req/s), full `/status` at 20:05:08Z
  (`~/canary-data/2026-11-08-drive/status_full_200508.json`), `detectors.inference`
  window of the last 300 ticks, 7,013 ticks since the 18:08Z start:
  - **`loop_occupancy` median 70.5 ms, p95 83.5 ms (max 127.9)** = ~7 % of each
    second, against the ~17 % (~170 ms) recorded above (`source.tick()`
    169 ms median on the loop after D01.8).
  - `feature_wait` (the worker's elapsed time, off the loop) median 272.8 ms,
    p95 355.7; `feature` stage 325.5 ms median, p95 404.8, max 647; whole tick
    `total` 349.2 ms median, p95 427.0, max 667.7, under the 1 s budget
    (439 ms at idle and 320 ms under `tuning-did` recorded above). `lateness`
    median 0.5 ms, p95 3.3.
  - **Missed ticks: 4 of 7,013 (0.06 %), all `overrun`**; the poller shows all 4
    had accrued by tick 4,308 and none in the next 3,000 (19:20-20:10Z,
    50 min). On 2026-10-04 the cold-boot drive missed 23 of 2,271 (1 %).
    `stale_results_discarded` 1, `compute_failures` 0, `worker_fallbacks` {}.
  - Scoring (IF + sequence, 1 per ~21 s): `compute` 540 ms median (IF 182,
    sequence 357), `compute_wait` 546; behavior 3.8 ms; lambda delivery
    14.1 ms.
  - **HTTP latency:** the laptop's compact `/status` polls over Wi-Fi
    (`_ttfb`, 596 polls 19:17-20:15Z, engine running): median 47 ms, p95 96 ms,
    max 368 ms. Request rate `achieved_hz_60s_total` median 78.2 Hz (p5-p95
    73.7-83.8, n 556). System at 20:05Z: load 0.28 per CPU, 67 °C, memory
    limits enforced (6 GiB), `mem_used_fraction` 0.156.
  **Relaxed:** there is no same-window A/B against the in-process build (that
  build cannot run on this daemon), so "matched live window" is the figures
  recorded earlier on other loads (a heavier request mix than `tuning-did`);
  the comparison is against the 17 % / 169 ms and 439 / 320 ms above, and the
  7 % loop occupancy and 0.06 % missed ticks are what close it. The worker's
  CPU cost (feature stage ~325 ms) is unchanged by design (see above).
  _From D01._

---

- **DAMP matrix-profile spike** *(from `../archive/ml-correctness-hardening.md`
  9.2, optional).* Prototype left-discords (DAMP, KDD'22) offline over a few
  sessions as a model-free third opinion, and adopt it only if it finds
  detections the current stack misses. Deferred, not blocked: it shares no
  failure modes with the reconstruction and isolation detectors and is
  trivial at 1 Hz, but nothing is waiting on it.

## Suggested order for the next drive (written 2026-09-18)

The 2026-09-18 order ran as far as a slow offroad drive allowed; what it
produced is in the [drive log](../../../data/analyses/2026-09-18-drive-log.md).
The next drive's order, its written-down predictions and the desk work that
must precede it live in
[`2026-09-18-next-drive-deploy-plan.md`](../../../data/analyses/2026-09-18-next-drive-deploy-plan.md).
In short:

1. **Desk first:** the two operator decisions (D03.21; 0x1208 / 0x1206–0x1207
   catalog text), then **D03.18**, because a 2.4 s stall can erase a 4 s pull.
   Then `analyze_fuel_blocks.py` into `tools/` with a known-answer fixture, and
   a 3 Hz minimal pull profile if it resolves feasible at 90 req/s.
2. **Boot check:** 0 "batching disabled" lines on a key-off boot (D03.19's last
   unverified path).
3. **One capture on `tuning`, warm, paved** (it supersedes `fuel-pw-research`,
   retired 2026-09-21, and carries the same fuel channels):
   - closed-loop plateaus;
   - ≥30 s of held enrichment across ≥3 independent pulls (D03.8, D03.9);
   - tip-ins/lifts held ≥5 s (D04.3);
   - in-gear and neutral coasts from ~3,000 rpm (D04.2, using 0x1206's floor as
     the fuel-cut marker).
4. **Restart ≤10 min before parking** so D03.11's small-session `digest.json`
   can be observed (unless D03.6 has landed).
5. **Any drive with a ≥10 kPa elevation change, on `default`:** D04.8 and
   0x1208-vs-baro in one session, and altitude data for D03.20.
6. D03.14–D03.17, D02.2 and D01.5 wait until the fuel workflow closes.

<details><summary>The 2026-09-18 order, as written before the drive</summary>



1. **Before departure:** D03.11 deployment/evidence checks and D03.13 UPS check;
   prepare D03.4's feasible candidate profile and D03.8's provisional channel.
   Do D03.9's first analysis on the laptop. D03.6's API/shutdown and D03.13's
   concurrency fixes are desk work; verify them locally before exercising them
   on the Pi. If the candidate profile is not ready, collect the normal drive
   and receive-path measurements without claiming a target-AFR test ran.
   Prepare D03.14's load reference and D03.15's shortlist from existing files;
   include only their necessary extra channels in the resolved hybrid plan.
   D03.16/17 require the probe preparation above. D03.17's KOEO control can
   use the initial key-on window or the final parked stop.
2. **Warm idle, then driving:** D01.5 with kernel counters immediately before
   and after each compact status sample, at both ends of a ≥300 s window.
   Report the full uncertainty bracket. Measure D02.2 controller overflow
   separately in the same windows; reject windows spanning a daemon/counter
   reset. Do not assume the reader-thread fix is justified by the old 4.24 %.
3. **Main drive block:** D03.8 with D03.4 quality evidence and D03.9's missing
   coverage. Allocate at least one sustained enriched interval plus a repeat;
   ordinary cruise alone leaves the target-AFR candidate unresolved.
4. **CAN-only discovery:** run D03.14/D03.15 in the prepared hybrid windows,
   sharing D03.8's capture where cadence permits. Give D03.16 a short separate
   diagnostic-ownership window (about 5–10 minutes including restoration).
   D03.12's pedal/voltage candidates are optional; its external duty reference
   and all of D03.10 are deferred. No new K-line capture is required.
5. **Remaining steady driving:** repeat D02.2's paced/bursty comparison at
   matched achieved wire rate, recording raw counter deltas, duration and
   workload. Change one factor at a time and report intra-round skew. Hardware
   SPI changes require checking the installed board/controller's supported
   clock first; the earlier 20 MHz suggestion is not verified here. Defer H6's
   ladder until the baseline contract holds and a candidate profile change is
   agreed; stop at the first failing rung, with no default budget increase.
6. **Final stop:** finish D03.11 trip-end verification, preserve/pull the
   evidence and restore the normal profile. D02.9/D03.7 and D02.10 replay
   evaluation belong before/after live collection, once isolation is proven.
   D02.5 still needs the operator's corpus choice; D01.10/D01.4 remain last.

</details>
