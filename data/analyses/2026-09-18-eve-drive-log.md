# 2026-09-18 (evening) autonomous drive log

_State file for the third /loop drive of the day. Resume from here; don't redo work._
_Earlier today: `2026-09-18-drive-log.md` (AM), `2026-09-18-pm-drive-log.md` (PM). Plans: `2026-09-18-next-drive-deploy-plan.md` (criteria, per the operator, unchanged) and `2026-09-18-pm-next-drive-deploy-plan.md` (the added tests below)._

## Glance
- **D03.18 fixed, deployed and validated live** (`530398e`, 21:27Z): 10.6 min on `fuel-pw-research` with 0 all-channel stalls, where ~6 were expected. Six silent entries were handled by the new path.
- Earlier: 0x1206/0x1207 = injected pulse PASS (matched air); 0x1134 is not commanded φ; 0x1208 = air charge. D04.3 unresolved at this resolution.
- Back on `default` for the corpus. No maneuvers needed. Health clean.

## For the operator
1. **The tuning provider `injector_pw_b1` still maps to 0x1208 (air charge) at `confirmed`.** You said to fix mislabels and then "we'll fix those later". The plan is in D03.9 (`9b1c788`). Nothing to decide now; it's listed so it isn't lost.
2. Carried over: D03.21 memory cgroup (reboot).

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
