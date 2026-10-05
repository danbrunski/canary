# D04 — CAN-only experiments not yet written down anywhere

Written 2026-09-18 for a drive with **no UpRev / K-line logger available**.
Everything here runs on CANary's own instruments: Mode 01 / Mode 22 polling,
the broadcast bus, GPS, and the `did_log` / `batch_probe` tools.

This is a separate file on purpose. `D03-open-work.md` and `README.md` are
mid-edit by another session (the evening review that added D03.8–D03.17); the
items below were checked against that working-tree version and against every
other roadmap doc so that nothing here repeats them. **Where a question is
already a task, this file points at it and stops.** Same conventions as D03:
task-level truth is the checkboxes here, `[x]` only with the measurement in
hand and the artifact linked.

## Already covered — do not duplicate

| question | lives in |
|---|---|
| 0x1134 vs PID 0x44 (target AFR) | D03.8 |
| injector constant re-fit on open-loop data | D03.9 |
| 0x120D/0x120E pedal pair, 0x1246 voltage | D03.12 (note its units caveat: ×0.005 is volts, Mode 01 pedal is percent, so a linear fit with `scale_verdict`, not a 1:1 demand) |
| `0x21` local-identifier blocks | D03.16 — run `tools/batch_probe.py` |
| `0x23` memory read under `10 FB` | D03.17 — `batch_probe` now sends the `23 <addr32> <len16>` form; `--session FB --address 0x00080000 --length 16`. Read its verdicts strictly: `works` is a payload, `answered` is "exists, refused the argument", and only `works` makes a same-instant candidate |
| `0x2A` periodic | **closed** in `../archive/multi-pid-polling.md` ("not available on this ECM"); `batch_probe` still sends the one request and records the NRC, which is fine, but it is not an experiment |
| receive loss at warm idle, overflow vs burstiness | D01.5, D02.2 |
| VVT duty external scale reference | D03.10/D03.12, needs UpRev — but see D04.7 for what CAN alone can bound |
| coolant DID 0x1101 offset | settled in `data/dids.yaml` (offset −50, high confidence, with the reasoning); no experiment needed |

## How these fit tomorrow's order

The review's *Suggested order for the 2026-09-18 drive* stands. These slot in
without a new block: **D04.1/D04.2** are the warm-idle and first-coast-down
minutes of step 2; **D04.3/D04.4** are analyses over the D03.8 hybrid capture
of step 3, needing only that the drive contain a few throttle steps and three
or four steady plateaus; **D04.5, D04.6, D04.7** are desk work on data that
already exists, with tomorrow adding coverage if convenient; **D04.8** is
opportunistic on any sustained climb.

---

## Needs a few minutes of the truck

- [ ] **D04.1 Is 0x1112 a closed-throttle switch or a fuel-cut flag?**
  Block 5 found a binary register: `1` in 97 % of decel-cut-like samples, `0`
  in every high-load sample, and it covers 104 of the 116 samples where the
  wideband sat at its lean rail — a *superset* of full fuel cut, which fits
  "closed throttle" better than "fuel cut". One condition separates them:
  **warm idle** is closed-throttle *and* fuelled. Reads `1` there → throttle
  switch; reads `0` at idle and `1` during an in-gear coast → fuel-cut flag.
  **Protocol (≈3 min, folds into step 2's warm idle):** daemon passive, TTL
  600; `tools/did_log.py --start 0x1100 --end 0x1219 --batch 1` so 0x1112 is
  logged beside 0x1208 (PW), 0x120F (TPS) and 0x1219 (RPM) from the same
  instrument. 60 s at warm idle, pedal released. Then two closed-throttle
  coasts in gear from ≈2,500 rpm to idle, and one coast in **neutral** from the
  same speed (no DFCO in neutral: if the flag still reads `1`, it is throttle).
  Tag each physical action.
  **Accept:** a per-sample table of `0x1112`, PW, TPS, RPM for each of the
  three conditions — not a correlation. The verdict is which of the two truth
  tables it matches; anything else stays a candidate. Bonus: whether it leads
  or lags the PW floor by more than one sample says whether it is an input
  (switch) or an output (fuelling state).

- [~] **D04.2 Map the decel-fuel-cut boundary.** DFCO only ever appears in
  this repo as an exclusion (`fs == 4` caveats, `censored_ceiling_afr`, the
  lift interval in D03.15). Its *thresholds* — the rpm at which fuel cuts on a
  closed-throttle coast and the rpm at which it resumes, and whether that is
  rpm- or road-speed-gated — are a calibration fact the tuning workflow keeps
  guessing at, and CAN sees it directly: injector PW collapses to its floor
  (≈3,250–3,600 counts on this ECM) and PID 0x24 λ pegs at 2.000.
  **Protocol:** the same coasts as D04.1 plus two more in a different gear
  (3rd and 4th, say), from ≈3,000 rpm, and one from a higher speed in a lower
  gear, so rpm and speed are not confounded. Hybrid profile (Mode 01 4 Hz
  gives λ and FUEL_STATUS; DIDs give PW and RPM at 2 Hz) — or the D04.1
  `did_log` window if it is still running.
  **Accept:** one row per coast — gear (from the rpm/speed ratio), rpm and
  speed at PW-floor onset, rpm and speed at re-fuel, delay from throttle
  closure to cut. Report the hysteresis (entry − exit) per gear. If onset rpm
  agrees across gears but onset *speed* does not, it is rpm-gated; the
  converse is speed-gated. Five events is a result; two is a hint.
  **2026-09-18 PM ([September 18 PM drive log](../../../data/analyses/2026-09-18-pm-drive-log.md)):** there were 32 closed-throttle coasts on
  `fuel-pw-research` (25 with ≥ 2 floor samples), but none were tagged or
  protocol coasts.
  - **Correction to the text above:** the fuel-cut floor is DID **0x1206** at
    **54–58 counts**, not 0x1208. That register is air charge and doesn't floor.
    The floor is confirmed by the λ rail and FUEL_STATUS 4 (94–100 % on long
    coasts).
  - Cut at 1,840–3,600 rpm. The rpm/km·h ratios cluster at ~36.7, ~49.8 and
    ~71 (gears not assigned).
  - Cut/refuel timing is only ±0.5 s at 2 Hz.
  - Broadcast wheel speed reads 8–16 % below GPS, so settle speed first.
  - Table: `~/canary-data/scripts-0918b/out/coasts-_capture-fuel-pw-research-2026-09-18T174742.md`.
  - Still owed: gear assignment, hysteresis per gear, and throttle-to-cut delay.
  **2026-09-22 (desk, all three 09-18 `fuel-pw-research` captures pooled —
  [evidence](../../../data/analyses/2026-09-22-d03-d04-desk-pass.md),
  `~/canary-data/scripts-0922/coasts_d042.py`):** 84 coasts (68 usable). Four
  relative gear clusters at 28.6 / 36.7 / 49.9 / 71.4 rpm per km/h of
  broadcast wheel speed (n 24 / 22 / 13 / 9); absolute gear numbers need the
  gearbox ratio table, which the vehicle profile does not carry.
  - **Throttle closure → cut: 0.4–1.4 s** (cut-bracket lower bound p10/50/90
    0.42 / 0.85 / 2.82 s, n 65, from the 100 Hz broadcast throttle).
  - **Onset rpm is the lift rpm** (1,660–3,600 in every gear); no onset
    threshold is visible above ~1,650 rpm, and the floor was seen down to
    1,462 rpm (lowest gear cluster).
  - **Exit, throttle still closed:** the lowest refuel rpm rises with gear —
    ~1,290–1,640 rpm at 19–27 km/h (D), ~1,340–1,790 at 44–49 (B),
    ~1,800–1,950 at 64–68 (A) — so it is **neither purely rpm- nor purely
    speed-gated**; the shape fits a speed-indexed recovery-rpm table.
    Hysteresis medians (cut − refuel) 125 / 144 / 131 / 262 rpm, of limited
    meaning because the cut rpm is the lift rpm.
  - **Unexplained:** closed-throttle refuels at 2,100–3,500 rpm on long
    descents (e.g. cut 3,112 → refuel 2,387 after 59 s); clutch, brake or a
    maximum-duration rule are all possible and none is on the tags.
  - **Still owed (truck):** tagged protocol coasts, in gear and one in
    neutral, so ECM refuel is separable from driver action.
  **2026-09-26 (desk): gears assigned.** With the FS6R31A ratios in the
  profile (`aa4da33`, H07.11), clusters A/B/C/D are **5th / 4th / 3rd /
  2nd**. The `coasts_d042.py` re-run reproduces 84 coasts, 68 used, and
  medians 28.6/36.7/49.9/71.4. Mapping script and output:
  `~/canary-data/scripts-0926/gears_d042.py`, `out/gears_d042.json`.
  - **Theory**, in rpm per *true* km/h: (1000/60) / 2.609 m × 4.08 × ratio
    gives 113.8 / 65.6 / 45.4 / 33.4 / 26.1 / 20.0 for gears 1–6.
  - **The 8 % correction is established; do not re-measure it.**
    `WHEEL_SPEED_6F7_A`, the speed the clusters use, agrees with PID 0x0D to
    0.55 %. PID 0x0D reads 8 % under true: per-row median obd/gps 0.9203,
    distance 8.2 % under by two independent GPS measures (`bindings.yaml`
    0x6F7 notes). Scaling each coast's rpm/(km/h) by 0.9203 lands the
    cluster medians at 26.28 / 33.79 / 45.93 / 65.74. That is +0.85 / +1.04
    / +1.10 / +0.16 % from 5th / 4th / 3rd / 2nd, and every coast in a
    cluster maps to the same gear. Uncorrected, the medians run 8.8–9.9 %
    high. The nearest gear is unchanged, but individual coasts in cluster B
    straddle 3rd and 4th.
  - **Same-instant check against GPS:** 64 coasts have a GPS sample within
    0.5 s of the cut. Per coast, 6F7/GPS is 0.907 median (p10–p90
    0.863–0.936), which agrees with the documented 0.920. rpm/GPS lands
    within −1.3 / +1.4 / −2.0 % of theory for 5th / 4th / 3rd. 2nd reads
    −6.4 % (k 0.867). On 2 coasts, one in B and one in C, GPS picks a
    different gear from the corrected broadcast. Both fit GPS lag at the
    cut, which falls in the steepest deceleration. The corrected broadcast
    is the better gear key.
  - **Hysteresis per gear** (rpm at cut − rpm at refuel; p10/p50/p90):

    | Gear | n | all refuels | closed-throttle refuels (n) | lowest closed-throttle refuel |
    |---|---|---|---|---|
    | 5th | 24 | 8 / 125 / 506 | 5 / 162 / 555 (12) | 1,800 rpm at ~70 km/h true |
    | 4th | 22 | 25 / 144 / 384 | 3 / 125 / 492 (15) | 1,338 rpm at ~50 km/h |
    | 3rd | 13 (12 refuel) | 43 / 131 / 628 | 32 / 112 / 168 (9) | 1,888 rpm at ~41 km/h |
    | 2nd | 9 | 128 / 262 / 1,055 | 116 / 369 / 1,070 (8) | 1,288 rpm at ~20 km/h |

    True km/h is broadcast ÷ 0.9203. Two limits on these numbers. First,
    the cut rpm is the lift rpm, so the medians say where the driver lifted,
    not an ECM threshold. Second, 3rd's lowest refuel (1,888 rpm at 41 km/h)
    breaks the "refuel rpm rises with gear" ordering above, so the
    speed-indexed recovery-table reading rests on 3 of 4 gears. The tagged
    coasts decide it, so the box stays `[~]`.

## Analyses over the D03.8 capture — needs only throttle steps and plateaus

- [~] **D04.3 Commanded-φ → measured-λ transport delay.** Six retracted
  findings were transient artefacts, and the acquisition-skew caveats in
  `afr_calibration.yaml` (+17 % raw at 12.4 AFR collapsing to +1.4 % when
  stable) are the same thing: nobody has measured how long the ECM's own λ
  reading lags its own command. Without that number, every transient pairing
  is a guess about which lag to allow.
  **Protocol:** during the D03.8 hybrid capture, three or four deliberate
  tip-ins from closed-loop cruise into enrichment and the matching lifts, each
  held ≥5 s, tagged. Nothing else is needed.
  **Analysis (desk, laptop):** a **lag scan**, not a correlation — pair
  `obd:COMMANDED_EQUIV_RATIO` against `1/obd:O2_WR_LAMBDA_S1` with
  `align_pairs` at candidate offsets 0…1,000 ms in 50 ms steps (shift one
  side's timestamps, pair same-instant at each shift), restricted to the
  tagged transients. The lag at which `difference_bias` and `residual_spread`
  are smallest is the transport delay. Report it with its width, and report it
  separately for tip-in and lift (the sensor's rich→lean and lean→rich
  responses differ). This is the vehicle skill's per-channel lag scan applied
  to two OBD channels, and it must refuse — as `compare_channels` does — if
  fewer than a few dozen pairs survive at every lag.
  **Accept:** a delay in ms with a stated uncertainty, plus the stability gate
  it implies (how many samples after a step before "steady" is honest). Put
  the number in `afr_calibration.yaml` next to the skew caveats it explains.
  **2026-09-18 PM ([September 18 PM drive log](../../../data/analyses/2026-09-18-pm-drive-log.md)):** 5 untagged tip-ins and 5 lifts.
  - **Tip-in:** one clear minimum at **500 ms**, bracket 250–750 ms, n_pairs 64.
  - **Lift:** no clear minimum (250 and 500 ms within 10 %).
  - **Resolution limit:** on `fuel-pw-research`, 0x44 and 0x24 sit in the same
    Mode 01 batch (~6 ms apart, 250 ms cycle), so only lags near multiples of
    250 ms get pairs.
  - A 50 ms answer needs the two PIDs in separate batches, or a faster cycle.
  - Script: `~/canary-data/scripts-0918b/lag_scan.py`.
  **2026-09-18 evening ([September 18 evening drive log](../../../data/analyses/2026-09-18-eve-drive-log.md)):** 13 holds. **No clear minimum in either
  direction** (78/49 pairs at best lag), so the PM "500 ms tip-in" doesn't
  replicate. This is blocked on splitting 0x44 and 0x24 into separate batches.
  **Waits on (2026-09-22):** a capture in which 0x44 and 0x24 carry
  independent acquisition times. The profile schema has no per-batch grouping
  control (only `obd_batch_max_pids`), so that means either a profile variant
  with `obd_batch_max_pids: 1` for the pull, or a batch-split feature first;
  no local session can resolve it.
  **2026-09-25 (R05.16, desk): the RAM-target route, enrichment only.** The
  RAM final target `mem:ECM_TARGET_EQUIV_RATIO` is read by service 0x23 with
  its own acquisition time (~110 ms after the 0x24 read in the poll cycle,
  p10-p90 80-250 ms), so it is not locked to the lambda read the way 0x44 is.
  `estimate_transport_delay(regimes=[enrichment])` on five 2026-09-20..21
  drives (161 enrichment target reads, candidates 0-0.6 s by 50 ms): mean
  |residual| 4.28 % at 0 s, 3.56 % at 0.10 s, 3.60 % at 0.20 s, 4.65 % at
  0.30 s. **Frozen 0.10 s for the RAM route in `lambda_delivery.yaml`, weakly
  supported:** read by read, 0.10 s beats 0 s on 58 reads, loses on 41, ties
  on 62 (sign test p = 0.11), and nothing from 0 to 0.5 s is distinguishable
  from 0.10 s per read (per-pair bracket 0.0-0.5 s); the mean-residual 5 %
  bracket is 0.05-0.20 s. It is **quantised to ~0.15 s at best**: the RAM
  target is polled at ~0.9 Hz, so a step is dated only to within its poll
  gap, which also biases any fitted lag short. No tip-in / lift split (too
  few reads).
  - **Why not the PM "500 ms tip-in":** that scan paired 0x44 with 0x24, which
    can only express lags near multiples of the ~250 ms batch cycle (0, 250,
    500, 750 ms), and scored only tagged transients, where wall-film and
    sensor response add to pure transport; it did not replicate that evening.
    The RAM scan scores every enrichment read and is not batch-locked, but its
    ~1 s target sampling pulls short. Neither is a transport-delay measurement
    to 50 ms.
  - **The 0x44/0x24 route remains unresolved:** on seven drives (3412
    enrichment reads) every shift from 0.10 to 0.60 s is worse than 0 s read by
    read (0.10 s: 1041 better / 1150 worse, p = 0.02; 0.25 s: 1507 / 1681,
    p = 0.002). That says the same-batch pair is the best this route can do,
    not that the delay is zero; `lambda_delivery.yaml` declares it
    `not_estimated` and delivery error pairs it as acquired.
  - Still waits on a capture with 0x44 and 0x24 in separate batches (or the
    RAM target polled faster) for a 50 ms answer. Reproduce the above with
    `scripts/lambda_delivery_evidence.py --fit-enrichment-delay`.
  **2026-10-03 correction to the 09-22 "waits on" note:** the schema already
  had per-batch grouping when that note was written. `mode01_batch_groups`
  (`src/collection/capture_profiles.py`, validated as a disjoint subset)
  landed in `7ad695c` on 2026-09-20. The `afr-delay-research` profile uses
  it, putting 0x44 and 0x24 in different partitions with
  `max_tuple_skew_s: 0.05`. That profile was retired into
  `capture_profiles.archive.yaml` on 2026-09-21 without ever being captured,
  and neither live profile (`default`, `tuning`) splits the pair. So the
  blocker is a capture, not a feature. Either restore `afr-delay-research` to
  the catalog for one drive, or give `tuning` the same two groups. That is an
  operator choice, because `tuning` is the one tuning profile by design.

- [x] **D04.4 Where is `ECM_MAF_GPS` linear against Mode 01 MAF?** Block 6
  gave the whole-session answer — slope 0.934, r² 0.909, `drifting`, with the
  DID reading ~5 % low at high flow — but not *where* the drift lives, and
  D03.9 needs that to separate a MAF-scale error from a PW-constant error.
  **Protocol:** steady plateaus at three or four airflows, ≥20 s each, during
  ordinary driving: flat road at ~60 km/h (~30 g/s), ~90 km/h (~50 g/s), a
  sustained climb (~80 g/s), and any pull (>100 g/s). Tag them. Blocks 6 and 9
  may already contain such plateaus — **check them first, at the desk**.
  **Analysis:** `compare_channels("obd:MAF", "did:ECM_MAF_GPS")` within each
  tagged plateau's time window, at 300 ms. One slope and `scale_verdict` per
  band. A slope that is 1.00 at 30 g/s and 0.93 at 100 g/s is a nonlinearity
  with a location; a uniform 0.93 is a scale error; `drifting` inside a single
  plateau is skew, not the sensor.
  **Accept:** the per-band table, and an explicit statement of the flow range
  over which the ×0.01 g/s decode may be used at ±2 %. That range belongs in
  `data/dids.yaml` as a `conditions` field on 0x1209 — a measured constraint
  goes in the schema, not a note.
  **Done at the desk 2026-09-18** (`data/analyses/2026-09-18-maf-did-and-injector-mass-balance.md`,
  09-17 blocks 6/7/9). There's no scale error and no nonlinearity: block 6's
  slope of 0.934 / r² 0.909 reproduces exactly, and it's acquisition skew. The
  DID is read ~115 ms after Mode 01, so rising flow reads high and falling flow
  reads low. With |dMAF/dt| ≤ 5 %/s the median DID/Mode 01 ratio is
  1.000 ± 0.007 in every band from 3 to 134 g/s. The ×0.01 g/s decode holds to
  ±2 % over 3–134 g/s: firm at 20–90 g/s, thin above 90 g/s (81 pairs). **Still
  owed:** the `conditions` field on 0x1209 in `data/dids.yaml` (a catalog edit
  left for the desk).
  **Done 2026-09-22:** 0x1209 now carries an evidence item with
  `constant: 0.01`, `constant_unit: g/s per count` and the required
  `conditions` (3–134 g/s, |dMAF/dt| ≤ 5 %/s; firm 20–90, thin above 90) —
  the schema's own mechanism, which refuses a constant without conditions.
  `tests/test_dids.py` passes on the edited catalog.

## Desk work on data that already exists (tomorrow adds coverage)

- [x] **D04.5 Broadcast inventory under road load.** The vehicle skill's
  `absent_signals` list (ignition timing, wideband AFR, cam timing, injector
  PW, base fuel schedule, load) rests on the **2026-08-08 sweep, which was
  no-load only** — its own caveat says a signal appearing only under road load
  could have been missed. Nine sessions from 2026-09-17 cover 26–126 km/h and
  two WOT pulls, and the broadcast bus was recorded through all of them.
  **Desk:** `tools/inventory_broadcast.py` over `2026-09-17T205003` (the 857-
  chunk session) and over `_capture-tuning-hybrid-research-2026-09-17T191712`
  (the one with the pull). Diff the per-ID set and per-byte activity against
  the 2026-08-08 no-load inventory. Any ID that appears only under load, or any
  byte that is constant at idle and moves under load, is a candidate.
  **Drive (only if the desk pass finds candidates):** `can_activity` windows
  around a tagged tip-in and a tagged pull, restricted to the candidate IDs.
  **Accept:** either "nothing on the bus moves under load that did not move
  at idle" — which finally *earns* the absent-signals claim — or a shortlist
  with `changed_byte_indices` per candidate. Bindings stay untouched either
  way; a candidate is a candidate.
  **Done at the desk 2026-09-22**
  ([evidence](../../../data/analyses/2026-09-22-d03-d04-desk-pass.md),
  `~/canary-data/scripts-0922/broadcast_d045.py`). `2026-09-17T205003` is not
  on the laptop; used `_capture-tuning-hybrid-research-2026-09-17T191712`
  (2.31 M frames, the pull) and `2026-09-18T180630` (2.12 M frames, the
  descent). The 2026-08-08 parquet's `can` rows carry all-zero payloads, so
  the baseline is the sweep's own candump, recovered from
  `re-2026-08-08-rev-ac/re-window.log.gz` (an `ls -l` line prepended to the
  gzip stream; 683,373 frames, 27 ids). **Result: nothing with the
  cardinality or change rate of a continuous engine quantity appears under
  road load that did not already move at no-load.** Ids only under load are
  0x004 (≈150 frames, one 3-valued byte), 0x342/0x512 (2 frames) and 0x682
  (7, constant). Every byte that was constant parked and moves driving is
  either motion/chassis (0x284/0x285/0x6F7 wheel speeds, 0x2D1/0x280/0x354
  speed, 0x590, 0x29E brake, 0x002 steering) or a low-cardinality slow byte:
  `changed_byte_indices` 0x1F9 [6] (23 values, 0.02 % changes), 0x2DE [1, 6,
  7] (2/3/21), 0x231 [6], 0x358 [0], 0x35D [1, 2, 4], 0x385 [2, 3, 5, 6]
  (TPMS), 0x60D [0], 0x625 [0], 0x794 [0, 1]. The absent-signals claim is
  earned for road load to 126 km/h and one WOT pull; the optional
  `can_activity` drive step is not warranted by this list. Bindings untouched.

- [x] **D04.6 Which of the 138 DIDs moved only under load?** The two
  `did_log` surveys (block 3b, no load; block 5, load to 4.13 V TPS) were each
  screened on their own. Nobody has yet diffed them: DIDs that were constant
  in the 7-minute no-load window and moved in the 30-minute loaded one are the
  cheapest possible shortlist of load-dependent registers, and it costs no bus
  time. `screen_didlog.py` already produces the per-DID `moves` fraction.
  **Accept:** the diff, with each newly-moving DID's raw range and the regime
  (cut / high-load / cruise) it moved in. Feeds D03.15's shortlist rather than
  competing with it.
  **Done 2026-09-22**
  ([evidence](../../../data/analyses/2026-09-22-d03-d04-desk-pass.md),
  `~/canary-data/scripts-0922/didlog_screen.py`, block 3b = `roadload-2`
  364 passes / TPS ≤ 3.76 V, block 5 = `roadload-3` 1,546 passes / TPS to
  4.14 V). 138 DIDs in both: 67 constant in both, 57 moving in both, 4 moved
  only in 3b (0x110D, 0x1146, 0x120B, 0x124A), **10 constant in 3b and moving
  in 5**: 0x1134 (64 → 64–75, 34 % off-modal at high load, 0 in cut — the
  known target), **0x122B** (0 → 0–1,861, 4 levels, high 0.32 / cruise 0.25 /
  cut 0), **0x122C** (0 → 0–65,482, 355 values, modal 63,572, off-modal cut
  0.90 / high 0.78 / cruise 0.59), and seven one-count steps that each
  changed exactly once in 1,546 passes (0x1234 33→34, 0x1237 15→16, 0x1239
  10→11, 0x123A 6→7, 0x123D 15→16, 0x1244 0→1, 0x1245 0→1 — a single
  increment, not a load response). Shortlist handed to D03.15: 0x122B, 0x122C.

- [x] **D04.7 Bound the VVT duty scale from saturation, without UpRev.** The
  ×0.5 % and ×100/255 % candidates for 0x1110 (and 0x1139) differ in one
  observable: a percent register on ×0.5 cannot exceed **200**, one on
  ×100/255 cannot exceed **255**. Block 5 already recorded **0x1139 at 227**
  and 0x1110 at 188 under road load — if both registers share a unit, 227
  already rules out ×0.5 for the family.
  **Desk first:** the maximum of each register across every `did_log` window
  on disk, and whether either ever sat *exactly* at 200 or 255 for consecutive
  samples (a ceiling looks like a plateau at the limit, not a single peak).
  **Drive:** any sustained pull (D03.8 wants them anyway) with a `did_log`
  window is enough to see whether 0x1110 pins at 200 or keeps climbing.
  **Accept:** a bound, not a scale — "0x1110 exceeds 200 → not ×0.5" or
  "0x1110 plateaus at 200 → consistent with ×0.5 at 100 %". The exact scale
  still needs D03.10's external reference; this decides what it can *not* be.
  **Done 2026-09-22 (desk, every `did_log` window on disk: 3a, 3b, 5,
  paced47 — [evidence](../../../data/analyses/2026-09-22-d03-d04-desk-pass.md)).**
  - *Superseded 2026-10-04 by firmware (D03.36):* 0x1110 is commanded EVAP
    purge, **×0.5 %** (the PID 0x2E handler's own arithmetic, which
    saturates above raw 200), not VVT duty. 0x1139 is `ECM_UNIDENTIFIED_1139`, identity open, so the
    "shared unit" premise below does not hold.
  - *2026-10-05 (D03.37):* 0x1139 is the bank-2 VTC solenoid duty,
    `ECM_INTAKE_VTC_SOLENOID_DUTY_B2`, at **×0.39 %** from the timer compare.
    The 255 plateau below is the controller's cap (99.4 %), and ×0.5 is ruled
    out as this bullet concluded.
  - **0x1110: max 130 / 149 / 190 / 153 — never ≥ 200, no plateau** (one
    sample at each window's max). Saturation gives it **no bound**: ×0.5 and
    ×100/255 both survive.
  - **0x1139: 221 (3a), 255 (3b, three consecutive samples), 154, 146.** It
    **exceeds 200 → not ×0.5** if it is a percent register, and a 3-sample
    plateau at 255 is a byte-limit ceiling (×100/255 at 100 %, or not a
    percent at all).
  - **The shared-unit premise fails**, so the 0x1139 bound does not transfer
    to 0x1110: in block 5's 1,002 passes with both non-zero, 0x1139/0x1110 is
    0.61 / 0.71 / 1.82 (p10/50/90); at 0x1139's maximum 0x1110 read 0, at
    0x1110's maximum (190) 0x1139 read 99.

## Opportunistic

- [~] **D04.8 `BAROMETRIC_PRESSURE` against GPS altitude.** A free physics
  check with a known answer: the barometric formula predicts ~−11.5 Pa per
  metre near 80 kPa, so a 300 m climb is ~3.5 kPa, against GPS altitude noise
  of ±10–30 m (≈0.1–0.3 kPa). `obd:BAROMETRIC_PRESSURE` polls at 0.05 Hz, so a
  10-minute climb gives ~30 pairs.
  **Analysis:** `compare_channels` of the pressure predicted from
  `gps:altitude` (via the formula, at the day's sea-level-equivalent) against
  the PID, within any tagged climb or descent of ≥200 m. Slope ≈1 with a
  `stable` verdict validates both instruments at once; a stable non-unity
  slope says which one is off, and by how much. This is the cheapest
  ground-truth pair the vehicle carries, and it has never been run.
  **Accept:** slope, offset, verdict, n_pairs, elevation span. Belongs in
  `tests/fixtures/ground_truth/expectations.yaml` if it holds.
  **2026-09-18** (`data/analyses/2026-09-18-altitude.md`): offroad at
  2,870–3,882 m, baro 63–73 kPa. It's consistent but the slope isn't confirmed.
  One sea-level p0 (~102.8 kPa) fits two sessions within 0.45 kPa, with a
  residual of 0 ± 0.47 kPa over 512 m. In-session spans are only 5 kPa and
  2 kPa, so the slope reads 0.91 `drifting` (a range problem), and nothing goes
  into `expectations.yaml`. One continuous ≥10 kPa climb in a single session
  closes it.
  **2026-09-18 PM:** `default` session `2026-09-18T180630` descends
  3,079 → 2,104 m with baro 70 → 79 kPa: **9 kPa in one session**, 1 short of
  the bar. The session is pulled to `~/canary-data/sessions-0918/`. Run the
  analysis at the desk: it may resolve the slope, but it doesn't meet the
  stated ≥ 10 kPa.
  **2026-09-22 (desk — [evidence](../../../data/analyses/2026-09-22-d03-d04-desk-pass.md),
  `~/canary-data/scripts-0922/altitude_d048.py`, `altitude_lag.py`).** Known
  answer first: `2026-09-18T144215` reproduces the 09-18 figures exactly
  (n 114, slope 0.910, offset +6.63, r² 0.866, `drifting`, free P0 102.54).
  Then `2026-09-18T180630`, 3,245 → 2,095 m, baro 79 → 70 kPa: **n_pairs 67,
  slope 0.825, offset +13.74 kPa, r² 0.939, `drifting`**, b−a +1.08 ± 0.69
  kPa; free P0 **102.81 kPa** (residual 0.02 ± 0.71), consistent with the AM
  session's 102.54 — but the free-P0 residual trends **+0.57 → −0.11 →
  −0.44 kPa** across the span, and a lag scan (baro shifted earlier 0–420 s)
  only worsens the fit. **The 09-18 "range-limited slope" reading is
  falsified**: doubling the span from 5 to 9 kPa lowered the slope from 0.91
  to 0.83. ISA gives 10.6 kPa for this descent; the PID moved 9. A warm
  atmosphere explains ≤ 8 % (the slope would need ~328 K), the PID is a
  1 kPa/bit integer, and a delayed estimate is excluded by the lag scan — so
  one of the two instruments is compressed ~15–20 % over the descent and CAN
  alone cannot say which. Nothing goes into `expectations.yaml`.
  **Waits on:** a third instrument (a phone barometer, or surveyed
  elevations along the descent), not a longer single-session climb.

## Not included, and why

- **Mode 01 timing-advance surface vs the ROM timing table.** The method is
  written (`engine-tuning-support.md` 3.5) and the ROM decode is settled, but
  the table's BFS axis is exactly D03.14's unresolved problem; measuring the
  surface on rpm × air-per-event before that axis is settled would produce a
  shape nobody could compare to the table. After D03.14.
- **Bank-2 λ reference.** PID 0x25 has no observations on this ECM and the
  only bank-2 mixture path is the A/F voltage DID through an UpRev-derived
  transfer. There is no CAN-only experiment that adds an independent bank-2
  instrument; D03.12 correctly leaves it open.
- **Idle/closed-loop anchor for the injector constant** (φ = 1 by definition
  in closed loop, yet the mass balance read 1.026 there). Real, but it is the
  first thing D03.9's fit-on-one/validate-on-other will expose; a separate
  task would duplicate it.
