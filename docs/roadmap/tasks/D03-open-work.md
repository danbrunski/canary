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

**Updated 2026-09-17** by an autonomous run during a 5-hour drive
([log](../../../data/analyses/2026-09-17-drive-log.md)): D01.5's premise was
re-measured and its headline figure is in doubt, D02.2 gained a controlled pair
and a third candidate fix, and five new items (D03.1–D03.5) were opened, three
of them already fixed and deployed. Afterwards, at the desk, **D02.9 was
root-caused** ([evidence](../../../data/analyses/2026-09-17-d029-replay-amplification.md)):
its 4× row amplification is really 1.29× plus a wall-time-versus-rows
arithmetic error, and its OOM belongs to the trip-end digest, now D03.6. D03.7
records why the validation harness could not have caught either.

## Rules that still apply

- Read `/status.vehicle_state` before interpreting anything (H0).
- Never widen a queue or relax a threshold to make a contract pass — stop at
  the first failure, name the constraint, fix its source.
- No bus-budget raises; no rate default raised without sustained evidence.
- **Long replays are currently unsafe on the Pi** — but not for the reason
  D02.9 originally gave. The kill was the trip-end digest loading the whole
  output at raw resolution (D03.6), not the replay itself. Until D03.6 lands, a
  replay whose output exceeds roughly 2.5–3 M rows will OOM an 8 GB Pi when the
  trip ends, and so will a long enough live drive.

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

  What remains of D02.9 itself is items 1 and 2 above. The memory half is a
  different defect and is now D03.6.

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
  **What remains is the replay comparison.** It is no longer blocked by D02.9 in
  the way this task assumed: the OOM was never the replay (D03.6 — it is the
  trip-end digest), and the cliff sits between ~4.5 M and ~5.9 M rows in the
  *output*. These sessions are 700 k–2.5 M rows, so their replays land well
  under it and are safe to run today. Two things to do first: deploy `c15f66a`
  (replay now emits only captured sources — otherwise the output is inflated
  1.29× and the detectors read whichever `decoded` copy landed last), and use a
  calibration session rather than the training one. Do not run it while the
  truck is collecting: a replay replaces live collection.
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
  overflow.
  **Re-measured 2026-09-17 at driving load, and the premise needs re-stating**
  ([evidence](../../../data/analyses/2026-09-17-drive-log.md)): over 300 s on the
  `default` profile at ~2,950 rpm, daemon `receive.frames_seen` +491,083 against a
  kernel `rx_packets` bracket of 490,919 (narrowest) to 496,570 (widest), i.e.
  **loss of −0.03 % … 1.10 %**, midpoint 0.54 %, with `rx_dropped` flat.
  The 65 s protocol above reads the kernel counter once per end, and a `/status`
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
  ordering as a lead and the controlled pair as the finding.
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

- [ ] **D03.7 The sustained-validation harness cannot see replay row
  duplication.** It wires replay through the storage decode
  (`src/validation/harness.py:617-690`), so it is the right instrument — but its
  fixture `tests/fixtures/ground_truth/drive-slice` holds `gps 1682 / can 19122 /
  obd 953` and **no `decoded` rows**, so the expansion it records
  (`decoded_rows: 3080` beside `can: 2189`) is a correct one-to-many for that
  fixture and structurally blind to D02.9's doubling. A regression test needs a
  fixture whose source already contains `decoded:` rows. The harness already
  measures `max_rss_mb`, so it can cover D03.6 too once pointed at a big enough
  session, off the vehicle.

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

- [ ] **D03.4 `tuning-hybrid-research` requests a cycle it cannot meet.** The
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

- [ ] **D03.6 The trip-end digest loads a whole session at raw resolution, on
  the event loop.** This, not the replay, is what took the daemon to 7.7 GB and
  got it OOM-killed on 2026-09-11
  ([evidence](../../../data/analyses/2026-09-17-d029-replay-amplification.md)).
  The trip-end hook (`drain.py:556-558` → `runtime.py:249` →
  `control/trip_end.py:60`) runs `get_or_compute_digest` **synchronously on the
  loop**, and `resample_and_segment` reads every chunk with every column
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
  **Live exposure is real, today:** this drive's open session
  `2026-09-17T205003` reached **857 chunks (~8 M rows)**, i.e. larger than the
  replay that OOM-killed the daemon. Nothing fired only because a trip-end needs
  5 minutes of sustained stationary and the truck kept moving (and three deploy
  restarts rotated the earlier sessions). Of today's eleven sessions, the three
  with a `digest.json` are the 235-, 112- and 17-chunk ones; the 425-, 551- and
  857-chunk ones have none. **A long drive that ends in a 5-minute stop is the
  trigger, and the consequence is a systemd restart plus no digest for the
  session you most wanted one for.**
  Two parts, and the second is the real one:
  1. **Push the existing refusal into the shared function.** `GET` for the same
     digest already refuses `_replay-*` (`control/digest.py:30` →
     `control/sessions.py:166-178`); the trip-end hook is the one caller that
     bypasses it. Cheap, and it names the incident — but it only moves the cliff.
  2. **Fix the order of operations.** Peak memory is O(rows × channels) because
     the pivot precedes the resample, so a long *live* drive OOMs the same way:
     2.6 M rows already needs ~3.9 GB on an 8 GB Pi. Select only the columns the
     digest needs and reduce each channel onto the resample grid **before** the
     pivot (~1,900 × 100 instead of ~4 M × 100), and run it off the loop. Every
     real drive so far has a `digest.json`, so this has never fired live; the
     replay was simply the first session big enough. Do not treat "it has not
     happened on a drive yet" as headroom — it is ~2× away.
  Measure, don't extrapolate: re-run `resample_and_segment` against the pulled
  2.6 M-row `2026-08-08T214218` under `ru_maxrss` **on the laptop**. That needs
  ~4 GB and must never be run on the Pi.

- [ ] **D03.5 The Pi cannot pull from GitHub.** `git pull` on the Pi fails with
  "Please make sure you have the correct access rights" — no deploy key for
  `git@github.com:danbrunski/canary.git`. The first deploy attempt of the day
  therefore restarted the *same* code while reporting success. Worked around
  with `git bundle create <file> <pi-HEAD>..main main` + `scp` +
  `git pull --ff-only /tmp/<file> main`, which is a real fast-forward and keeps
  history. A read-only deploy key, or a scripted bundle path, removes a silent
  failure mode from every future deploy.

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
