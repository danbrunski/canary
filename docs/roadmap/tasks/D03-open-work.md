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
  `c15f66a` added emission counters to the report and unit coverage for the emit
  filter, but `run_case` still replays `DRIVE_SLICE`; the missing end-to-end
  fixture has not been supplied. Include live-GPS contamination and legacy
  payload absence in the regression coverage. This is desk work.

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
  **Before tomorrow's candidate capture:** prepare the revised vehicle profile
  between captures, and inspect the resolved scheduler plan. A 2 Hz DID request
  alone does not prove that `did_cycle_seconds: 0.25` is feasible; resolve the
  cycle warning as well, without raising the bus budget. Include any D03.8
  candidate channels in that calculation. On the drive, retain a 20-minute
  capture, measured per-channel acquisition cadence and pairing skew, loss
  deltas and the complete quality verdict. Update capacity evidence only from
  that exact profile revision. Controller overflow can still independently
  fail persistence quality after the rate request is corrected.

- [~] **D03.6 Trip-end digest isolation and memory-order fix landed; large-session
  measurement and shutdown behavior remain.** The original synchronous digest, not the replay, took the daemon
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

- [ ] **D03.5 The Pi cannot pull from GitHub.** `git pull` on the Pi fails with
  "Please make sure you have the correct access rights" — no deploy key for
  `git@github.com:danbrunski/canary.git`. The first deploy attempt of the day
  therefore restarted the *same* code while reporting success. Worked around
  with `git bundle create <file> <pi-HEAD>..main main` + `scp` +
  `git pull --ff-only /tmp/<file> main`, which is a real fast-forward and keeps
  history. A read-only deploy key, or a scripted bundle path, removes a silent
  failure mode from every future deploy.

## Added by the evening review — next drive and its preparation

Evidence for D03.8–D03.10 and D03.12 is the
[September 17 drive log](../../../data/analyses/2026-09-17-drive-log.md), especially
blocks 2, 5, 6 and 9. These are measurements to resolve, not permission to
promote unverified bindings or to change a tune.

- [ ] **D03.8 Pair target-AFR candidate 0x1134 with PID 0x44 in one capture.**
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

- [ ] **D03.9 Re-evaluate the injector fuel-mass calibration using today's
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

- [ ] **D03.11 Verify the evening fixes through a real drive and stop.**
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

- [ ] **D03.14 Test whether calculated load provides a reconstructible BFS
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

- [ ] **D03.15 Search for a BFS-like DID by separating airflow, base schedule
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

**Shared evidence artifact:** append each experiment's setup, raw-log/session
paths, executed conditions, missing inputs, measured result and next decision
to `data/analyses/2026-09-18-drive-log.md` when run. Close a task with evidence
even if its hypothesis was rejected; leave unexecuted/dependency-blocked work
open. A failed finite search is not proof that K-line exposes something CAN
cannot, and an estimator matching BFS cannot identify where UpRev computes it.

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

## Suggested order for the 2026-09-18 drive

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
