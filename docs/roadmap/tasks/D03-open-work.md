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
  `b9356bd` to `ECM_ENRICHMENT_TARGET_EQUIV_RATIO_CANDIDATE`; the evidence
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
  (UpRev USB, 2026-09-20), and `tuning_providers.yaml` role `injector_pw_b1`
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
  405/405 rows against UpRev's own stream definition. The "air charge"
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
  the matching raw register is observed in the same window.

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
    Today's paired
    result rejects a simple mirror; raw range and shared zero fractions do not
    identify bank 2 or settle either scale.
  Record `compare_channels` verdicts, acquisition separation, exact raw decode
  and operating conditions. Leave unsupported/flat/unpaired candidates open.

- [~] **D03.13 Capture-quality concurrency fixed; UPS heartbeat remains.**

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
  0x1208 itself, verified against UpRev's own stream definition (raw ×
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

- [~] **D03.18 One request timeout stalls the whole hybrid cycle ~2.4 s, and
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

- [~] **D03.29 Trip end is detected from GPS speed alone, so a drive without a fix
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
  - [ ] Unexplained, separate from the fix: why the receiver held no fix for the
    whole drive after being powered all night. Check the next drive's first
    minutes; if it repeats, it is a hardware or antenna question for the operator.
    *2026-09-24 16:04Z key-on (Pi up since 04:55Z):* `/status.gps` showed a fix
    within ~1 min of the engine starting (fix_quality 1, 10 satellites, HDOP
    1.07, 2,014 m). One good start; the 09-23 no-fix drive stays unexplained.

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
  - [~] **Bank-2 wide-band A/F from PID 0x28 (operator, 2026-09-26): desk
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
  - **Not decided here:**
    - A throttle cross-check (commanded vs actual, pedal D vs E) was the one
      reason to keep the dropped throttle PIDs, and it was not built.

## Deferred by decision, not blocked

- [~] **D01.4 Get the feature build off the event loop — implemented; Pi
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
   and all of D03.10 are deferred. No new UpRev/K-line/USB capture is required.
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
