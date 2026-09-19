# 2026-09-18 (evening) autonomous drive log

_State file for the third /loop drive of the day. Resume from here; don't redo work._
_Earlier today: `2026-09-18-drive-log.md` (AM), `2026-09-18-pm-drive-log.md` (PM). Plans: `2026-09-18-next-drive-deploy-plan.md` (criteria, per the operator, unchanged) and `2026-09-18-pm-next-drive-deploy-plan.md` (the added tests below)._

## Glance
- **D03.18 fixed, deployed and validated live** (`530398e`, 21:27Z): 10.6 min on `fuel-pw-research` with 0 all-channel stalls, where ~6 were expected. Six silent entries were handled by the new path.
- Earlier: 0x1206/0x1207 = injected pulse PASS (matched air); 0x1134 is not commanded φ; 0x1208 = air charge. D04.3 unresolved at this resolution.
- Back on `default` for the corpus. No maneuvers needed. Health clean.

## For the operator
1. **The tuning provider `injector_pw_b1` still maps to 0x1208 (air charge) at `confirmed`.** You said to fix mislabels and then "we'll fix those later". The plan is in D03.9 (`9b1c788`). Nothing to decide now; it's listed so it isn't lost.
2. **IF retrain corpus (D02.5): which sessions train the new Isolation Forest?** The deployed IF (2026-07-27) scores nothing: 6 of its 88 features were retracted from bindings after it was trained. That's verified live tonight as exactly those 6.
   - **Option A (my suggestion):** every corpus-eligible `default` session after 2026-08-05 (the last retraction), `_`-prefixed captures excluded as always, with thresholds calibrated out-of-sample (leave-one-session-out, per memory). Cost: that corpus mixes pre- and post-batching-fix sample rates (key-off boots ran ~8 reads/s vs ~19.5), and spans 63–80 kPa, where today's OOD firing lives.
   - **Option B:** only sessions from 2026-09-18 onward (batching fixed, current names). Clean, but likely too few hours to train.
   - **Option C:** keep it blind until a planned corpus-collection week. Cost: zero IF coverage meanwhile (0 of 104 events on D02.6).
   - I won't start a retrain without your pick.
3. Carried over: D03.21 memory cgroup (reboot).

## Preconditions
| precondition | state |
|---|---|
| no code pending | met: Pi `63d5e51`; everything since is docs/catalog text. The `dids.yaml` relabel `f78956f` is text only and the Pi doesn't need it for this drive |
| batching after boot | met: 0 "batching disabled" on the 20:01Z boot, max_pids 6, 0 narrowings. **It was a key-ON boot** (`failures_while_ecu_silent` 0), so D03.19's key-off path is still unverified |
| memory cgroup | not met (D03.21) |
| warm engine | met: 91–92 °C |
| D03.18 fixed | **not met** → holds ≥ 10 s |

## Plan
| # | item | serves |
|---|---|---|
| 1 | setup: keepalive 300 min, window `10c6c46e…` on `2026-09-18T200256`, Pi pollers (`poll-…-18c`, `live-…-18c`) | hygiene |
| 2 | `fuel-pw-research-2` (new name, saved live 20:05Z) = `fuel-pw-research` + CONTROL_MODULE_VOLTAGE 4 Hz, INTAKE_TEMP/BAROMETRIC_PRESSURE/LONG_FUEL_TRIM_1 1 Hz, SHORT_FUEL_TRIM_1 2 Hz | D03.9 matched-air test + dead time |
| 3 | closed-loop part-throttle holds at WOT-level air charge (0x1208 ~25–40k) | D03.9 |
| 4 | ≥ 3 WOT holds ≥ 10 s | D03.8, D03.9 |
| 5 | 3–4 tip-ins held ≥ 5 s, lifts | D04.3 |
| 6 | tagged coasts in 3rd and 4th, one in neutral | D04.2 |
| 7 | idle electrical-load step when parked | D03.9 dead time |
| 8 | back to `default` between blocks; ≤ 80 min isolated in total | hygiene / corpus |

**Not possible this drive:** splitting 0x44 and 0x24 into separate Mode 01 batches (no profile control for it). D04.3 stays at ±250 ms resolution.

### Predictions: the operator's plan, unchanged (`2026-09-18-next-drive-deploy-plan.md`)
| claim | passes if | falsified if |
|---|---|---|
| 0x1134 = commanded φ × 64 | `compare_channels` vs 0x44 over ≥ 30 enriched (0x44 > 1.05) stability-gated pairs from ≥ 3 pulls: slope 1.00 ± 0.03, `stable`, residual ≤ 1/64 | slope outside 0.95–1.05, or 0x1134 stays 64 while 0x44 > 1.10 |
| 0x1206 = injected pulse | closed-loop line (27,747 counts/g + 41) predicts enriched rows within ±5 % | rich-row residual beyond ±8 % |
| 0x1208 = air charge | the same test fails by −14..−18 % on rich rows; counts per g air within ±2 % of closed loop | it tracks the enrichment |
| transport delay (D04.3) | lag scan of 0x44 vs 1/λ has one clear minimum, for tip-in and lift separately | no minimum |

The 0x1206 row above is known from the PM drive to be ill-posed (the line was fit on idle and extrapolated). It will be reported as written anyway. The added claims below were written before this drive's data, in the PM plan, and they're what can actually decide it.

### Added predictions (from `2026-09-18-pm-next-drive-deploy-plan.md`, written 18:40Z, before this data)
| claim | passes if | falsified if |
|---|---|---|
| 0x1206 = injected pulse (matched air) | closed-loop rows in this capture at the enriched rows' 0x1208 air charge (±5 %, ≥ 20 rows) predict the enriched rows within ±5 % after scaling by measured φ; if fewer than 20 such rows, **unresolved** | enriched residual beyond ±8 % against matched-air closed-loop rows |
| dead time | at idle with constant 0x1208, a ≥ 0.4 V load step moves 0x1206 by a consistent offset | no step at ≥ 0.4 V |

### This drive only: stall count
Same definitions as the PM log. A stall is ≥ 2.0 s with no sample on any capture channel. A hold is 0x44 > 1.05 for ≥ 3 s, or a tagged wot…lift span. Report stalls, stalls inside holds, and enriched seconds lost.

## Health
| time (Z) | state | drops | q/hw | rx_over | reads/s | narrow | inf p95 | emitted | alt m |
|---|---|---|---|---|---|---|---|---|---|
| 20:05:06 | engine_running (default, 3 min after boot) | 0 | 1/**1320** | 7 | 18.34 | 0 | 39.0 | 2 | 1,982 |
| 20:13:08 | engine_running (fuel-pw-research) | 0 | 1/1320 | 51 | — | 0 | 42.6 | 10 | 2,085 |
| 20:38:55 | engine_running (default) | 0 | 1/**2185** | 128 | 18.12 | 0 | 39.6 | 36 | 2,170 |
| 20:55:46 | engine_running (default) | 0 | 1/**3559** | 200 | 18.41 | 0 | 38.4 | 54 | 2,005 |
| 21:12:07 | engine_running (default) | 0 | 1/3559 (flat) | 228 | 18.33 | 0 | 37.9 | 66 | 2,312 |
| 21:27:22 | engine_running (after the 530398e restart; **counters reset**) | 0 | 1/161 | 0 | 14.49 | 0 | 34.8 | 4 | 2,222 |
| 22:04:06 | engine_running (default) | 0 | 1/356 (max flush 366 ms) | 266 | 20.34 | 0 | 38.7 | 38 | 2,366 |
| 22:30:54 | engine_running (default) | 0 | 1/768 | 480 | 19.82 | 0 | 38.8 | 81 | 2,342 |

## Work notes
- 20:01Z Pi booted (key-on). 20:04Z operator: "new drive is starting now" (4 h).
- 20:05Z Storage high-water 1,320 within 3 min of boot, 0 drops: a boot-time backlog. Watch it.
- 20:05Z `fuel-pw-research-2` saved live (PUT, no warnings). All added channels are in the ECU's supported Mode 01 set. Mirrored into the repo as `dd5e098` (field-for-field; 79 profile tests green; the test rewrite of the file was restored).
- 20:06:03Z capture started; truck already moving at 60 km/h, so the idle step is deferred.
- 20:07Z **`fuel-pw-research-2` defect, seen live at 90 s:** `acquisition_gap` on 6 Mode 01 channels. Single-PID gaps of 2.8–16 s, NOT all-channel stalls. Per-request timeouts in ~2 min: **0x44 37/228**, CONTROL_MODULE_VOLTAGE 31/326, BAROMETRIC_PRESSURE 21/91, 0x24 11/451, MAF 4, IAT 3; RPM/LOAD/TIMING/FUEL_STATUS/trims 0; DIDs ~0. 0x44 got fewer than half of RPM's requests (228 vs 522). Batching: 73 desyncs, 21 failures in the 5 min since boot. **Cause (hypothesis):** adding 5 PIDs changes the 6-PID batch composition, and batches holding 0x44/0x42/0x33 come back partial or desynced. That's the known batch-desync class, now hitting the one channel D03.8 needs.
- 20:08:06Z **Stopped it** (`_capture-fuel-pw-research-2-2026-09-18T200606`, 121 s, verdict fail: acquisition_gap on 7 channels + persistence_loss can0 +16) and **started `fuel-pw-research`** (clean on 0x44 this afternoon) at 20:08:08Z. Tag `switch_to_fuel_pw_research_after_0x44_timeouts`. The dead-time step will get a voltage-only variant at a parked stop, if one comes. **Desk task:** which batch compositions desync on this ECM (the D03.17 batch_probe territory).
- 20:08:40Z **Confirmed on `fuel-pw-research`:** over 7.3 s, 0x44 had +20 requests and +0 timeouts (RPM +20/+0), with 0 new desyncs. The 0x44 timeouts are specific to `-2`'s composition, so the hypothesis stands. **`fuel-pw-research-2` must not be used again until the batch probe says which PIDs can share a batch with 0x44.** (It stays saved; a comment in the repo file should say so, at the desk.)
- 20:35:27Z **Block 1 stopped** (`_capture-fuel-pw-research-2026-09-18T200809`, 27.5 min; the wake-up was 7 min late because the laptop suspended). Isolated time so far 29.4 of 80 min. Verdict **fail**, both clauses explained: `acquisition_gap` on 14 channels (the stalls below, plus the key-off), and `persistence_loss` can0 overflow (the broadcast path, known). Pulled (34 MB).
- **Block 1 analysis** (report `data/analyses/2026-09-18-fuel-blocks-capture-fuel-pw-research-2026-09-18T200809.{md,json}`; scripts `~/canary-data/scripts-0918b/{d038_enriched,matched_air}.py`).
  - **Known answer PASS:** DID RPM vs Mode 01 RPM, n_pairs 2290, slope 0.9992, r² 0.998, `stable`.
  - **Stalls (this drive's rule):** 20 all-channel gaps ≥ 2 s. One is **not a stall**: 20:27:13–20:33:40Z (386 s) was a key-off (poller: `engine_off`/`no_data`, OBD freshness to 380 s). The rest are 16 of 2.25–2.5 s (the D03.18 shape) and **3 of exactly 5.0 s** (20:08:24, 20:11:52, 20:15:24Z). Those are a new shape, longer than D03.18's; D03.18 should cover them or they need a task. **13 holds (0x44 > 1.05 ≥ 3 s), 119.4 enriched s, stalls inside holds 0, enriched s lost 0.**
  - **D03.8, 0x1134 = commanded φ × 64: the claim as written is FALSIFIED; the raw/64 decode survives.**
    - 203 enriched pairs over 14 independent holds (gaps > 3 s). 0x44 values: 1.1641 ×180, 1.1548 ×16, 1.06 ×6, 1.186 ×1. 0x1134 raw: 74 ×196, 64 ×6, 76 ×1.
    - Pre-registered slope: **1.524** (r² 0.979, se 0.016), outside 0.95–1.05, with `scale_stability` **unresolved** (two levels, not a range). On its own the slope clause wouldn't be decisive.
    - **The decisive evidence is a counterexample, not the slope.** In hold 10, 0x44 stayed at 1.060–1.068 for 3.5 s (6 pairs, separation ≤ 0.17 s, not onset lag) while 0x1134 stayed **64**. Separately, 0x44 = 0.8707 (a lean command) on 293 samples with 0x1134 = 64.
    - The other falsifier ("64 while 0x44 > 1.10") didn't fire (0 pairs).
    - Where 0x1134 moves, it's floor(0x44 × 64) on 88.7 % of enriched pairs: 74 ↔ 1.1641 (74/64 = 1.15625, residual exactly −1/128) and 76 ↔ 1.1859.
    - **Reading:** 0x1134 is a *rich-side target* (power-enrichment φ, raw/64) that 0x44 follows when that enrichment is the active source. It's not the final command: other sources, like the 1.06 hold and the lean 0.87, bypass it. **Candidate rename, desk:** `ECM_TARGET_EQUIV_RATIO_CANDIDATE` → a power-enrichment-target candidate.
  - **D03.9 0x1206 = injected pulse.**
    - (a) The operator-plan criterion (fixed 27,747 + 41 line): enriched **+7.4 %** (n 188), between pass (±5) and fail (±8), so **unresolved**. It's the known ill-posed extrapolation; closed loop is also +5–6 % off that line.
    - (b) **The added pre-registered matched-air-charge test: PASS.** 184 of 188 enriched rows each have ≥ 20 closed-loop rows within ±5 % 0x1208 air charge (median 64 rows). Predicted by the φ-scaled closed-loop median: residual **+1.0 %** (p10–p90 −0.6..+3.5; +1.4 % with a 30-count offset).
    - 0x1207: **−0.5 %** (p10–p90 −1.9..+3.2), PASS.
    - The closed-loop air-charge range covers the enriched band (CL p95 33,052; enriched 31,845–33,935), so nothing is extrapolated.
    - Own-session refit: enriched +1.4 % (n 188). DFCO floor 52–57 counts.
  - **D03.9 0x1208 = air charge.** Per g air: closed loop 63,778 vs enriched 64,068 (**+0.5 %**, band ±2): **pass**. Rich residual vs the closed-loop refit: **−13.0 %**, 1.0 pp **outside** the −14..−18 band. At this φ (1.164) pure air charge predicts −14.1 %. The falsifier ("tracks the enrichment") didn't fire: enrichment is +16.4 %, the register moved +0.5 % per g air. So: first clause narrowly missed, second passed; the identification stands (4th session).
  - **D04.3:** 78 tip-in and 49 lift pairs at the best lag. **No clear minimum in either direction** (near-minimum lags scattered, 200–450 ms tip-in, 200–2,700 ms lift). The PM "tip-in 500 ms" **doesn't replicate**. Unresolved: it needs 0x44/0x24 in separate batches.
  - **D04.2:** 41 coasts, 34 with ≥ 2 floor samples; floor 52–57 counts (next value 141). Untagged; gear assignment is desk work.
- 20:40Z `injector-deadtime-idle` saved live and mirrored (`c309688`, 90 profile tests green, file restored after the test rewrite). It's Mode 01 RPM/MAF/0x24/FUEL_STATUS/**0x42** at 4 Hz plus DIDs ECM_RPM/0x1208/0x1206/0x1207 at 2 Hz, **without 0x44**. The dead-time prediction is already written (Added predictions). Storage high-water reached 2,185 around the block switch and my pull (0 drops); watch it.
- ~20:42Z Operator: "not going to park for 3 hours". Dead-time deferred; `injector-deadtime-idle` stays saved for next time. Desk subagent started on the new 5.0 s stall shape (vs D03.18's 2.4 s), using pulled data only.
- ~21:00Z **Desk: D03.18 re-diagnosed** (subagent, pulled data only; `~/canary-data/scripts-0918b/out/stall5s.md`).
  - The 2.4 s stalls are a silent session **entry** (`10 C0`) that sends the next Mode 01 batch into the held session. They are not a timed-out DID read.
  - The 5.0 s stalls are a silent fast **exit** (`10 81`) waiting out the 5.0 s session lapse.
  - **Live check:** `/status` shows `fast_exit_failures` = 3 since boot, equal to the three 5.0 s gaps. Session control: 212 timeouts, 0 NRCs.
  - D03.18 task text updated with both mechanisms and the minimal fixes. This is desk code work; not deploying from the road.
- 20:56Z **Storage high-water is climbing: 1,320 (20:05, boot) → 2,185 (20:33:55, key-on after the stop) → 3,559 (20:46:45)**, with 0 drops and queue depth back at 1 each time.
  - The 20:46:45 step is one slow flush: `max_flushing_write_ms` 2,139. At ~2,355 rows/s that's ~5k rows backed up; 10k needs a ~4 s flush.
  - The journal shows nothing at 20:46:45, and no pull was running. Cause unattributed; an SD write-latency spike is plausible, not shown.
  - The 1 Hz live logger was stopped at 20:57Z (no more captures planned; one less load).
  - **Watching:** if the high-water passes ~7,000, or any drop appears, it's a regression to act on (memory: the storage-writer overload).
- 21:19Z Operator: "feel free to implement fixes for performance issues and deploy them."
- 21:25Z **D03.18 fix committed: `530398e`** (implementer subagent; I reviewed the diff and re-ran 418 targeted tests; ruff/format/mypy clean). A silent session entry now exits first; a silent fast exit is retried once. Both are bounded by the D03.19 "answered since" gate. Sims: 2.16 → 1.18 s and 5.005 → 1.087 s.
- 21:26:55Z **Deployed**: Pi `capture_profiles.yaml` backed up to `/tmp/capture_profiles.pre-530398e.yaml` (the diff was only the daemon's built-in upgrade; both live-saved profiles already matched git) and reset. `git pull --ff-only` gave `a48f30c9` = pushed HEAD. Restarted; HTTP back in 20 s. Window reopened as `7f81cdea…` on `2026-09-18T212710`. Keepalive 240 min.
  - Healthy at 21:27:22Z: 0 drops, 0 errors, 0 "batching disabled", max_pids 6, 0 narrowings. The new `/status` fields are present.
  - **What the restart proves:** the new code runs and `default` is healthy. **What it can't prove:** the fix, because `default` opens few DID windows (1 session failure in 25 min before the deploy).
- **Validation block, prediction written before the data.** 10 min on `fuel-pw-research` (0.5 s DID cycle). Baseline on the same profile this evening: 16 all-channel stalls of 2.25–2.5 s plus 3 of 5.0 s in 27.5 min (≈ 5.8 + 1.1 expected in 10 min).
  - **Pass:** 0 all-channel gaps ≥ 2.0 s other than key-off, AND `unknown_session_exits` + `fast_exit_retries` > 0 (i.e. the paths fired and bounded the gaps).
  - **Unresolved:** the counters stay at 0 (no silent session control happened, so nothing to prove).
  - **Falsified:** ≥ 2 gaps in 2.2–2.5 s or any 5.0 s gap while those counters advance.
- 21:38:09Z **Validation block stopped** (`_capture-fuel-pw-research-2026-09-18T212740`, 10.6 min; isolated total now ~40 of 80 min).
  - **PASS by the pre-written criteria:** 0 all-channel gaps ≥ 2.0 s, and `unknown_session_exits` 6 = `session_failures` 6 (session control: 6 timeouts, 0 NRCs).
  - `fast_exit_failures` 0 and `fast_exit_retries` 0: the exit-retry path wasn't exercised live.
  - Verdict fail on two clauses, both explained. `acquisition_gap` is **0x44 alone** for 2.49 s at +298 s: a single-PID miss, not a session stall; watch whether 0x44 misses recur (compare `fuel-pw-research-2`). `persistence_loss` is can0 overflow +88 (broadcast path, known).
  - One 8.5 s enrichment hold, 0 stalls inside it.
- ~22:25Z Operator: "fix the mislabeled channels now and deploy. Keep making fixes, don't rest." Started an implementer on the rename: 0x1208 → `ECM_AIR_CHARGE`; 0x1206/0x1207 → `ECM_INJECTOR_PULSE_1/_2` (bank unverified); 0x1134 → `ECM_ENRICHMENT_TARGET_EQUIV_RATIO_CANDIDATE`. Old names go into `aliases` behind one shared canonicalization, and the tuning provider is re-pointed. A read-only desk agent in parallel is working on the `fuel-pw-research-2` 0x44 batch timeouts.
- 22:31Z Anomaly emits went 38 → 81 in 27 min. Since 22:04: LSTM 28 (highway 12, city 11, idle_transient 5), fused 5, behavior air-mass/VE/fuel-trim 12 (all `idle_transient`). vehicle_state `engine_running` at ~2,340 m. **Out of distribution, not a fault:** it's the known altitude/air-mass OOD (D03.20) on July models. No action.
- ~22:40Z Operator: "no aliases, remove wrong info." The rename implementer was redirected. It's a hard rename (no `aliases`); the wrong descriptions are deleted, not kept as history; and a one-time migration tool rewrites stored channel names (session parquet, model manifests/artifacts, saved profiles). It runs on the Pi with the daemon stopped.
- ~22:40Z **Desk: the `fuel-pw-research-2` 0x44 timeouts ROOT-CAUSED** (`~/canary-data/scripts-0918b/out/batch044.md`). The registry gives PID **0x33 BAROMETRIC_PRESSURE `byte_length=2`**; J1979 and this ECM give 1.
  - In a batched response, the parser swallows the next PID's id byte as baro's second byte. The result is a desync, and every later member times out.
  - Evidence: 69 of 83 baro rows carry a 2-byte payload whose second byte is a PID id (0x42 ×33, 0x44 ×20, 0x24 ×12…). Single-PID baro reads in the same capture are 1 byte. Swallows plus truncations explain ~90 of 99 desyncs.
  - The byte-count-limit hypothesis is **refuted**: a 19-byte plain batch parsed cleanly 4,493 times.
  - Latent since `afbf682`. It surfaced now because capture rounds ignore tiers and put slow-tier baro into fast batches. The same class of bug is latent in 0x55–0x58 (registry 1, J1979 2).
  - A second implementer is fixing the widths, adding an independent J1979 width table as test data, and a regression test.
  - Side finding (queued, not yet fixed): capture_quality's `batching` block is a lifetime scheduler snapshot, not capture-scoped. That's the "cumulative counters read as capture-scoped" mistake, so this evening's per-capture desync counts were carried over from earlier.
- ~23:00Z **`5eb1464` PID width fix committed and pushed** (implementer; I reviewed the diff and re-ran 175 targeted tests). 0x33 changed 2 → 1 and 0x55–0x58 changed 1 → 2. All 92 registry PIDs are checked against an independent J1979 table in test data, and a capture-round regression walks baro first/middle/last (it fails on the old width). Single-PID values are unchanged. **Deploy is held** so it goes out with the rename (one restart + migration). `default` doesn't batch slow-tier baro, so there's no live urgency.
  - Caveat for other vehicles (logged, not acted on): J1979 tables list 0x55–0x58 as 2 bytes, but a 2-bank ECU might send only A. The robust fix is the report's measured-width guard: learn each PID's width from single reads, and refuse to batch on a mismatch. **Next fix candidate.**
- ~22:45Z **`b9356bd` rename committed** (implementer; no aliases; wrong info removed; `tools/migrate_channel_names.py`). I re-ran 402 targeted tests. The tracked IF/LSTM manifests were migrated in the same commit, so the Pi's pull matches.
- **22:53:07–22:56:04Z deploy (daemon down ~3 min, engine running).**
  - Pi: backups at `/tmp/capture_profiles.pre-b9356bd.yaml` and `/tmp/canary.yaml.pre-b9356bd`. Stopped `canary`+`canary-mcp`, reset the daemon-dirtied profiles file, pulled `b9356bd7`.
  - A full-`--config` dry run was scanning every session parquet (slow). I killed it: the laptop audit found 0 stored rows with old names (sessions store raw `DID_<hex>`).
  - Migrated only what startup needs: `/etc/canary/canary.yaml` (1 name, via `sudo -S`; root ownership preserved) plus IF joblib (6) and LSTM pt (15). The profiles and behaviour models came clean from git.
  - Started; window `9eeb2fb3…` on `2026-09-18T225605`; keepalive 200 min; 0 errors; batching 6/0; `default` mode22 now lists `ECM_AIR_CHARGE`. Dashboard `pnpm build` done on the Pi.
  - IF shows 17/88 absent features: the known blindness (retrain owed) plus slow PIDs not yet read at +10 s. **None are renamed channels.** Re-check later.
  - **Owed after the drive:** migrate the session-level `capture_profile.json` snapshots (refit eligibility keys by recipe name) with the daemon stopped. Also fix the stale comment on `/etc/canary/canary.yaml` line 41 ("fuel-delivery commanded").
- ~22:58Z Operator: "about 1 hour of drive time next, work on candidates for BFS or some combination of telemetry that can be used to estimate BFS so we can make some progress on tuning."
  - Physics: Nissan Tp (UpRev "B-FUEL SCHDL") = K·Qa/Ne, i.e. air per revolution times an injector constant. Candidates: (a) 0x1208 air charge (established tonight), (b) K·MAF/RPM, (c) `derived:AIR_MASS_PER_CYCLE`.
  - A desk subagent is fitting all three against every UpRev B-FUEL SCHDL log on the laptop, with a known-answer RPM/MAF gate, a holdout, and residuals by regime; the idle vs idle+AC rung is the discriminator. Without UpRev along, the road hour can't add BFS ground truth, so it stays on `default` (which logs 0x1208 + MAF + RPM).
- 22:59Z Health clean after the rename deploy: 0 drops, reads 18.6/s, inference p95 46 ms. **IF absent features 17 vs 13 before the deploy.** The 4 new ones are all `gps:speed` variants, while GPS has a fix (11 sats) and GPS rows are flowing. Likely the first post-start windows before the fix; if it's still 17 at ~23:15Z, it's a defect.
- 23:05Z **`03e9d51` committed and pushed:** capture-quality batching counters are now capture-scoped deltas (implementer; I re-ran 46 tests). Deploy is held to batch it with the next fix, since it's low risk and needs a restart.
- 23:07Z **Defect: `/status` structural blindness is sticky from the first post-start window.** `_log_schema_reconciliation_once` records the verdict once per model version from the first diverging report. It's cleared only by a report with no divergence, which never happens (6 retracted features are always absent).
  - So startup sparsity (GPS not yet fixed; slow-tier baro/coolant/IAT not yet read) is reported as "structural, retrain needed". That's the wrong kind of empty.
  - At 23:06Z it was still 17, with `gps:speed` ×4 absent while GPS was fixed and flowing. The pre-deploy 13 had the same startup origin for baro/coolant/IAT. The truly structural set is the 6 retracted features.
  - Implementer dispatched: the structural set becomes the intersection over a sustained window, with `pending` status before that.
- 23:08Z **`ad0a0bb` committed** (implementer; I re-ran 84 tests): structural blindness = the intersection of absent sets over ≥ 120 s and ≥ 10 reports, `pending` until then, with transient absences listed separately.
- 23:11:30Z **Deployed `2f66a1a4`** (carrying `03e9d51` and `ad0a0bb`). Profiles-file dirt (the daemon's rev-3 upgrade) was backed up to `/tmp/capture_profiles.pre-2f66a1a.yaml` and reset. Restart; back 23:11:44Z; window `a5bf8506…` on `2026-09-18T231144`; keepalive 150 min; 0 errors. **Prediction:** after 120 s the IF verdict is `established` with exactly the 6 retracted columns (scores_nothing still true; retrain still owed), and no baro/coolant/IAT/gps.
- 23:16:05Z **Blindness fix verified live, as predicted:** IF `status: established` after 257 s / 259 reports. It has **exactly the 6 retracted columns** (ECM_AIRFLOW_PROXY_23D ×4, ECM_CALC_LOAD_B ×2), and transient is empty. `scores_nothing` true (6/88 = 6.8 % > 5 %), so **the IF retrain (D02.5) is the remaining fix.** Health: 0 drops, 19.05 reads/s, inference p95 39 ms.
- 23:18Z Pi `/etc/canary/canary.yaml` line 41 comment corrected to "air charge per intake event (0x1208)" (sudo; root ownership kept). Two implementers dispatched in parallel: a manifest-producibility CI check (a binding change can't silently blind a deployed model again; allowlist = the 6 known retracted columns) and a measured-width guard for Mode 01 batching.
- ~23:18Z **Desk: BFS. 0x1208 is a strong Base Fuel Schedule candidate** (`~/canary-data/scripts-0918b/out/bfs1208.md`).
  - **K step:** 0x1208 per g air per event stepped +7.5 % (58.5k → 62.8k) at the 07-27 reflash, in lock-step with UpRev BFS/air (28.7 → 30.8 ms per g). Plain air can't step against MAF from the same ECU, but air × K can. That's Nissan Tp = K·Qa/Ne.
  - **Inside the UpRev logs, BFS ∝ air through zero:** it ignores enrichment (31.1 vs 31.9), holds in DFCO, and on idle+A/C went +30 % vs air +31 %.
  - **Scale BFS_ms ≈ raw 0x1208 / 2040** (±1.5 %), derived era-wise, **not same-instant paired**.
  - **MAF route:** BFS = 29.06·K·MAF·20/rpm fits 08-08 at a −0.2 % median residual (p5/p95 −2.1/+2.6 %) and holds out within ±2 % per log. BFS trails MAF ~0.15 s.
  - **Map axis:** UpRev's fuel-target load axis is BFS ms from 8.76 ms, so idle and cruise (2–5 ms) sit in column 1. Steady high-load cell placement is good to ±3 %.
  - **Not confirmable from stored data:** the Pi's retention has swept the full 08-08 session, and the laptop copy ends before the UpRev rungs. **Confirmation needs one drive with UpRev along** (logging BFS/MAF/RPM) while CANary polls 0x1208 fast.
  - Implementer dispatched: a candidate `base_fuel_schedule` tuning provider (0x1208/2040, conditions + evidence in schema) and the 0x1208 catalog evidence.
- ~23:22Z `32fa211` manifest-producibility check committed (implementer; 11 tests re-run). **The LSTM also has 8/263 unproducible features** (the same two retracted bindings), masked under its ceiling, so it still scores; the retrain is owed for both. D02.5 updated.
- ~23:26Z `9fd6032` candidate `base_fuel_schedule` tuning provider committed (0x1208/2040 ms; opt-in `allow_candidates=True`; the envelope is enforced in schema; no automatic analysis uses it). 31 tests re-run. Desk agent dispatched: a known-answer test placing tonight's enriched rows on the transcribed fuel-target map. Commanded φ (0x44) should equal target_afr(rpm, BFS_est) per cell, and a scale sweep independently estimates the 2040 constant.
- 23:28Z (date -u) Correction: the three entries above marked ~23:18/~23:22/~23:26Z were first written with guessed times (23:30/23:40/23:50Z). The order is right; the times are approximate.
- 23:28Z **`ed42ccd` Mode 01 measured-width guard committed** (implementer; I re-ran 324 tests). **Deployed 23:28:56Z as `4ec2f4f0`**, back 23:29:10Z; window `14d268fe…` on `2026-09-18T232910`; keepalive 120 min; no warnings. The profiles-file dirt was backed up to `/tmp/capture_profiles.pre-4ec2f4f.yaml`.
  - **Baseline before (default, 23:11–23:28Z):** 3,683 batches, **0 desyncs**, 45 failures, 19.1 reads/s.
  - **Pass after ~5 min:** desyncs stay 0, `width_mismatches` empty, reads/s ≈ 19, 0 drops.
  - **Fail:** new desyncs from the stricter "unrequested PID" rule (an ECU that pads), or any mismatch on this truck's PIDs.
- 23:31:49Z **Width guard PASS (short window):** 577 batches in 2.8 min, **0 desyncs**, `width_mismatches` {}, no blame, 18.9 reads/s, 0 drops. Matches the baseline, so the stricter "unrequested PID" rule doesn't trip on this ECM. Keep watching at routine checks.
- ~23:31Z **Desk: BFS placed on the fuel-target map** (`~/canary-data/scripts-0918b/out/bfs_map_check.md`; the predictions were written first, with a disclosure that two facts from this log were already known).
  - **P1 FAIL (n 260):** median −14.1 %. **217 of 267 enriched rows sit at φ 1.1641 in 2,000–3,600 rpm × BFS_est 15.2–16.9 ms cells, where the map says stoich (1.000–1.08).** 13 stable closed-loop rows at the same rpm and raw 0x1208 sit at φ ~1.00, 8 of them 2–5.5 s before an enrichment hold. So **this enrichment is not a function of (rpm, BFS)**: it's time-delayed or triggered by something else. It fits 0x1134 = 74/64 as a separate power-enrichment target (0x95/128 equals the map's right-edge column value, but reaching that column would need +33 % BFS, which puts 21 closed-loop rows in rich cells).
  - **Where the map governs (≥ 4,000 rpm, n 33):** commanded φ tracks target_afr(rpm, BFS_est) to median −1.19 % (p10/p90 −2.55/+0.01). With BFS ≥ 15.67 ms, n 25: median −0.78 %.
  - **P2 UNRESOLVED:** the map's high-rpm columns are flat in BFS, so a ±3 % scale moves the target ~1.4 %, the same as 0x44's quantisation. The median crosses zero near a constant of ~1960, and closed loop bounds it at ≥ ~1900. **The map can't separate 2040 from ~1950; the UpRev same-instant window is still required.**
  - **Tuning consequence (for the operator):** the 2,000–3,600 rpm high-load enrichment at φ 1.164 comes from a mechanism outside the fuel-target table. Editing that table's cells won't change it. Find what governs it (candidate: the 0x1134 power-enrichment target, and its delay/trigger) before tuning those cells.
- ~23:50Z **Desk: the mid-rpm φ 1.1641 enrichment is a PEDAL SWITCH into the fuel-target map's LAST load column** (`~/canary-data/scripts-0918b/out/enrich_trigger.md`; hypotheses written first).
  - The broadcast pedal is ≥ 227/255 counts (≈ 89 %; throttle ≥ 223). Commanded φ (0x44) = base × the k106 map's 21.57 ms column at the current rpm: 0x95/128 = 1.1641 for 1,600–3,600 rpm, ramping to 0x99/0x9B/0x9F above.
  - **Rule scores:** 20 errors / 27,415 rows (throttle 16). On 439 rows the φ residual to that column is exactly 0.000 %. Onset median 0.15 s (18/21 within one sample); exit follows pedal release in 19/19; no decay in an 18.3 s hold.
  - **1.1548 holds:** these are 1.1641 × 127/128 (a closed-loop base term multiplies it; 41/41).
  - **Falsified:** H1 time-at-load (76 stoich rows at max air for ≥ 5 s, up to 28 s; enrichment follows the pedal crossing within 0.04–0.15 s) and H4 protection/duration.
  - FUEL_STATUS 2→4 moves with it (21/21) but isn't a separate trigger.
  - **0x1134** switches within one sample of 0x44 and is **the active open-loop rich target** (it also reads 65–77 on map-driven rich runs at pedal 129). **The 1.06-with-0x1134=64 counterexample was after-start enrichment (FUEL_STATUS 1)**, a separate source, and the 0.87 was a one-sample post-DFCO value. The current name `ECM_ENRICHMENT_TARGET_EQUIV_RATIO_CANDIDATE` fits.
  - **UpRev cross-check:** 22/22 onsets to TARGET AFR 12.63 had the pedal ≥ 4.1 V. 13 UpRev runs held stoich 0.1–1.5 s first (mostly pre-k106 tunes); unattributed.
  - **For tuning:** WOT/near-WOT fuelling at 1,600–3,600 rpm is set by the **last column** of the fuel-target table, selected by pedal ≥ ~89 %, not by BFS. That column is what to edit. The BFS map placement applies below the pedal switch.
  - **Mislabels flagged (not edited from the road; bindings changes are feature-spec changes and are desk work):** `ECM_ACCEL_PEDAL_POS_V` and `ECM_THROTTLE_SENSOR_V` read 0–255 counts but are labelled V; decoded `ECM_COOLANT_TEMP` reads 125–136 warm (the +40 offset looks unapplied).
  - **Capture that would pin the rest:** a pedal staircase at 2,000 and 3,000 rpm through 215–240 counts, up and down (thresholds, hysteresis); pedal ≥ 235 at 1,300 rpm (expect ~1.14); pedal 220 at 4,000–4,400 rpm (map path); one ≥ 30 s hold (timer check).
- 00:05Z Health clean: 0 drops, 0 desyncs, no width mismatches, 18 reads/s, 2,190 m.
- 00:07Z **[RETRACTED 00:30Z, see below] "Decode bug confirmed by hand":** the 0x551 payload `82 2d 00 80…` has byte 0 = 130. Stored decoded `ECM_COOLANT_TEMP` = 130.0, where Mode 01 coolant was ~90 °C. `bindings.yaml` (and the ground-truth pin) say offset −40, so **the live decode path drops the offset.** The ground-truth suite should have caught it, so it isn't exercising the live path.
  - **The deployed IF and LSTM both use `decoded:ECM_COOLANT_TEMP` (+__mean/__std/__roc) as features, trained on the un-offset values.** A fix shifts that live LSTM input by −40 °C; it would fire OOD until retrained.
  - Implementer dispatched to fix it at the shared decode layer and make the ground-truth suite exercise the live path. **Deploy held to go out with the retrain** (For the operator 2).
- ~00:30Z **Retraction: there is no decode-offset bug.** `decoded:` rows are RAW by design; the model scaler was fit on raw (FrameDecoder.decode, LiveFeatureSource.decode_for_telemetry, decode_can_rows). Every engineering-unit reader applies scale and offset correctly (read_channels gives 90.86 °C). My 00:07Z entry read a raw row labelled `unit: degC` as engineering units. **That's the same mistake as the real bug below.**
- **Real bug found (implementer, read-only first pass): mode labelling compares RAW decoded counts to engineering thresholds.**
  - Since `998668a` (2026-08-06), the segmenter / feature-pipeline mode signals fall back to `decoded:ECM_RPM` (8× rpm), `decoded:ECM_COOLANT_TEMP` (+40) and `decoded:WHEEL_SPEED_6F7_C` (16× km/h) whenever Mode 01 is stale, then compare them to rpm_max 900, coolant 70 and speed 1/80. A stale-OBD idle reads 6,400 rpm; 60 km/h reads 960 and lands in "highway".
  - The live trip/idle integrator has the same bug (and a fallback to a non-existent `decoded:VEHICLE_SPEED`). `status.py` hides it with a hard-coded `_ECM_RPM_SCALE`.
  - The tests missed it: the segmenter fixtures fed engineering units into `decoded:` columns, and the ground-truth suite only exercises `read_channels`.
  - **Affected:** mode labels in training fits since 2026-08-06, live per-mode threshold selection, behaviour-refit mode filters, and the live idle-time count. **Not affected:** model feature values (raw in and raw out, consistent).
  - Implementer dispatched on Option A: one shared engineering-value accessor on `bindings.apply_scaling`, routed through every physical consumer, with fail-before tests on raw bytes. **The deploy is held for the retrain decision**, since correcting labels changes which per-mode thresholds apply to models calibrated on the wrong labels.
