# 2026-09-18 (afternoon) autonomous drive log

_State file for the second /loop drive of the day. Resume from here; don't redo work._
_Morning drive: `2026-09-18-drive-log.md`. Plan: `2026-09-18-next-drive-deploy-plan.md`._

## Summary

**Run: 2026-09-18, 17:45Z to ~18:55Z, ~75 min of driving from 2,880 m up to 3,354 m and down to ~2,000 m.** Wrap-up requested by the operator at ~18:51Z. One isolated capture block (18.8 min, 1/4 of driving), two planned restarts, **0 dropped records all drive**. No code deployed.

### The OUTCOME: advanced, not closed
| claim | state | evidence |
|---|---|---|
| D03.9 0x1208 = air charge | **PASS** (pre-registered), 3rd independent replication | rich rows −16.6 % vs closed-loop refit (band −14..−18); per g air +0.8 % (band ±2); n 6 gated / 38 ungated |
| D03.8 0x1134 = φ × 64 | **unresolved, consistent**; falsifier did not fire | first values off 64 (raw 73–79); 10 enriched pairs, residual −0.0078 (½ quantum); bar is ≥ 30 pairs from ≥ 3 holds and only 2 holds happened |
| D03.9 0x1206/0x1207 = injected pulse | **unresolved: the pre-registered test was ill-posed** | the straight line was fit on idle rows and extrapolated. Closed loop and rich both land +6.2 % off it. Desk: the relation is convex in air charge, and one curve fits both sessions. Post hoc against that curve: rich −3.4 %, still extrapolated |
| D04.3 transport delay | tip-in **~500 ms ± 250** (one clear minimum); lift unresolved | 5+5 untagged transients; resolution limited by the shared Mode 01 batch |
| D04.2 DFCO boundary | table produced, analysis partial | 32 coasts; the floor is 0x1206 at 54–58 counts (the task text was corrected); gear groups visible, not yet assigned |
| D04.8 baro vs altitude (free) | 9 kPa span in one session (bar 10) | `2026-09-18T180630`, 70 → 79 kPa, pulled |
| THIS DRIVE: stalls inside holds | **0 of 6 stalls** fell inside the 2 holds; 0 enriched s lost | luck at ~1.3 % stall time; D03.18 is still owed |

### Beliefs verified or falsified
- **Falsified:** "the Pi was powered off after the morning drive". The keepalive held it up on battery (For the operator 1).
- **Falsified:** "the 0x1206 closed-loop line is 27,747 counts/g + 41". That's valid only near idle; the relation is convex in air charge.
- **Falsified (mine, mid-drive):** "no enrichment yet" at 17:56Z/18:01Z. My live view started after both holds. `drive.md` now says a live view is steering only.
- **Verified:** batching healthy after two warm restarts (max_pids 6, 0 narrowings). The key-off *boot* is still unobserved.

### Commits
`f401053` (log, block analysis, roadmap D03.8/D03.9/D04.2/D04.3/D04.8, next-drive plan, `drive.md` lessons), plus the final wrap-up commit.

### Candidates (not bindings)
Unchanged: 0x1134 = target φ (raw/64); 0x1206/0x1207 = injector pulse B1/B2. The "0x1208 = air charge" re-description awaits operator sign-off.

### Next drive
`data/analyses/2026-09-18-pm-next-drive-deploy-plan.md`: ≥ 3 WOT holds ≥ 10 s **plus closed-loop holds at WOT-level air charge in the same capture**; a split-batch profile with 0x42/0x0F/0x33/0x06/0x07; an idle electrical-load step; tagged coasts. D03.18 first.

### Preserved
Sessions: `~/canary-data/sessions-0918/` (all of today's closed sessions, both captures). Pollers and scripts: `~/canary-data/drive-0918pm/`, `~/canary-data/scripts-0918b/`.

## Glance
- **Drive wrapping up (operator, 18:51Z).** Restarted at 18:51:06Z so the final session (`2026-09-18T185113`) is small enough for its trip-end digest. Keepalive is 25 min (expires ~19:16Z), so the Pi shuts down on its own after parking.
- **Results:** 0x1208 = air charge PASSES; 0x1134 is consistent (10/30 pairs); 0x1206 is unresolved (ill-posed line; the next plan fixes the test); D04.3 tip-in ~500 ± 250 ms.
- Next drive: `2026-09-18-pm-next-drive-deploy-plan.md`. Decisions for you are below.

## For the operator
1. **The Pi was never powered off after the morning drive.** The 330-min keepalive
   armed at 13:45Z kept `ups-shutdown` from shutting down on input loss
   (`input lost but keep-alive hold active (7427s left) — not shutting down`,
   11:11 MDT). Uptime runs from 13:38Z and the daemon from the 16:09Z restart.
   - **Keep as is:** a keepalive keeps the Pi on battery after key-off until the
     keepalive expires. That's intended, but it means "the Pi's off" wasn't true.
   - **Change:** disarm the keepalive at the end of a drive (I'll do it at wrap-up
     when you say you're parking). It costs nothing, and the UPS then shuts down
     cleanly on input loss.
   - **Done at wrap-up:** keepalive cut to 25 min at 18:51Z instead of disarmed, so the digest runs after parking.
   - Consequence: the key-off **boot** check (D03.19's last unverified path) can't
     run today. There's been no boot since the fix.
3. **One more short capture past the ⅓ budget?** Block 1 used 18.8 min of a 60-min drive. D03.8 needs ≥ 30 enriched pairs from ≥ 3 pulls and has 10 from 2. Three more 10-s holds would likely close it.
   - **Yes (say "uphill"):** I open `fuel-pw-research` for ≤ 5 min around the holds, then back to `default`. Isolated time ≈ 24/60 min. Cost: ~5 min less corpus.
   - **No:** D03.8 stays unresolved (consistent, not proven) until the next pull drive.
2. Carried over from the morning, still open: D03.21 memory cgroup (reboot);
   accepting 0x1208 = air charge and catalogue text for 0x1206/0x1207.

## Preconditions (plan) — met / not met
| precondition | state |
|---|---|
| no code pending for the Pi | met: Pi at `63d5e51`, laptop HEAD `2ed8da0` is docs only |
| 0 "batching disabled" on a key-off boot | **not observable**: no boot since the fix (see For the operator 1). 0 lines since the 16:09Z restart; the 2 in this boot are 13:41Z/13:43Z, pre-fix |
| batching `max_pids 6`, `size_narrowings 0`, `failures_while_ecu_silent > 0` | met: 6 / 0 / 1,585; achieved_hz_60s_total 19.07 on `default` |
| memory cgroup | not met (D03.21 undecided): `/sys/fs/cgroup/system.slice/canary.service/memory.max` absent, `cgroup_disable=memory` |
| warm engine (ECT ≥ 85 °C) | met: 91–93 °C |
| paved road | assumed (operator: pulls possible) |
| D03.18 fixed | **not met**, so holds ≥ 10 s instead of pulls (operator's rule for this drive) |

## Plan (drive order from the deploy plan; criteria not moved)
| # | item | serves |
|---|---|---|
| 1 | boot/health checks, keepalive, drive window, Pi-side poller | hygiene |
| 2 | ~10 min `default` warm-up: engine started ~17:37Z, capture started 17:47:42Z | hygiene / corpus |
| 3 | 8–10 min closed-loop cruise holds at 60/90/110 | D03.9 closed-loop line at high air |
| 4 | 3+ WOT holds ≥ 10 s, full lift each | D03.8, D03.9 rich side |
| 5 | 3–4 tip-ins from cruise held ≥ 5 s, then lifts | D04.3 |
| 6 | coasts: closed throttle in 3rd and 4th from ~3,000 rpm, one in neutral | D04.2 |
| 7 | stop the capture while moving, read the verdict | outcome |
| 8 | restart the daemon ≤ 10 min before parking (digest, D03.11) | hygiene |

Capture budget: ≤ 20 min of the 60 min drive.

### Predictions (copied unchanged from the plan, written before the data)
| claim | passes if | falsified if |
|---|---|---|
| 0x1134 = commanded φ × 64 | `compare_channels` vs 0x44 over ≥30 enriched (0x44 > 1.05) stability-gated pairs from ≥3 pulls: slope 1.00 ± 0.03, `stable`, residual ≤ 1/64 | slope outside 0.95–1.05, or 0x1134 stays 64 while 0x44 > 1.10 |
| 0x1206 = injected pulse | closed-loop line (27,747 counts/g + 41) predicts enriched rows within ±5 % | rich-row residual beyond ±8 % |
| 0x1208 = air charge | the same test fails by −14..−18 % on rich rows; counts per g air within ±2 % of closed loop | it tracks the enrichment |
| transport delay (D04.3) | lag scan of 0x44 vs 1/λ has one clear minimum, reported for tip-in and lift separately | no minimum |

### This drive only: stall count (defined before the data)
A **stall** = an interval ≥ 2.0 s with no new sample on ANY capture channel
(both modes), in the capture session. A **hold** = a contiguous run with
PID 0x44 > 1.05 lasting ≥ 3 s (or a tagged `wot`…`lift` span if tags exist).
Report: stalls total, stalls overlapping a hold, and the enriched seconds lost
to them. That counts D03.18's cost against this experiment directly.

## Setup
- Pi at 192.168.137.2 (cable). Daemon active since 16:09:17Z, NRestarts n/a.
- The **stale drive window** from 16:09Z (session `2026-09-18T160921`) was still open; stopped and reopened as `9451810895aa4a1db0f730438e9efc70` on session `2026-09-18T173749` at 17:46:56Z.
- Keepalive 120 min armed 17:46Z (expiry 1789760799 ≈ 19:46Z).
- Pi poller: `/tmp/poll0918b.sh` → `/tmp/poll-2026-09-18b.jsonl` (5 s compact /status with time_connect/starttransfer/http code), started ~17:47Z.
- Pi tree: `capture_profiles.yaml` dirty (daemon's own upgrade); no deploy, so left alone.

## Health
| time (Z) | state | drops | q/hw | rx_over (since start) | reads/s | narrow | inf p95 ms | emitted | alt m |
|---|---|---|---|---|---|---|---|---|---|
| 17:46:31 | engine_running (default) | 0 | 1/326 | 126 | 19.07 (60 s) | 0 | 37.9 | 109 | 2,881 |
| 17:47:47 | engine_running (fuel-pw-research) | 0 | 1/326 | 135 | — | 0 | 38.8 | 112 | 2,885 |
| 17:53:06 | engine_running (fuel-pw-research) | 0 | 1/326 | 183 | — | 0 | 39.2 | 117 | 3,237 |
| 17:56:13 | engine_running (fuel-pw-research) | 0 | 1/394 | 201 | — | 0 | 39.4 | 119 | 3,354 |
| 18:01:09 | engine_running (fuel-pw-research) | 0 | 1/394 | 250 | — | 0 | 39.4 | 129 | 3,016 |
| 18:08:37 | engine_running (default) | 0 | 2/394 | 297 | — | 0 | 39.6 | 137 | 3,079 |
| 18:15:12 | engine_running (default) | 0 | 1/512 | 339 | — | 0 | 38.8 | 144 | 2,712 |
| 18:23:05 | engine_running (default) | 0 | 1/**1049** | 388 | — | 0 | 39.5 | 155 | 2,488 |
| 18:28:06 | engine_running (default) | 0 | 1/1049 | 438 | — | 0 | 40.5 | 159 | 2,104 |
| 18:28:43 | engine_running (after restart; **counters reset**) | 0 | 1/180 | 0 | — | 0 | 37.1 | 2 | 2,097 |
| 18:37:00 | engine_running (default) | 0 | 1/343 | 10 | 18.98 | 0 | 39.7 | 12 | 2,023 |

## Work notes
- 17:45Z first wake-up. Resume docs read; plan says no code pending.
- 17:47:42Z capture `_capture-fuel-pw-research-2026-09-18T174742` started; tag `fuel_pw_capture_start` (t=1789753667.9) on the drive window.
- 17:53Z live capture quality: `fail` on `acquisition_gap`, two all-channel stalls of 2.55 s at +141 s and +178 s (D03.18 shape, as expected). No enrichment in the window.
- 17:55Z Pi-side 1 Hz live-value logger `/tmp/live0918b.py` → `/tmp/live-2026-09-18b.jsonl` (`/telemetry/latest?n=600`, no auth needed, 60–70 ms). For steering only: it's a sampled view, not data. **Don't use `per_channel=true` at 1 Hz: its TTFB is 1.34 s.** Pi `sudo` needs a password, so the status poller runs unauthenticated (compact /status answers 200 without a token).
- 17:56Z Scripts ready (subagent), `~/canary-data/scripts-0918b/{stall_holds,lag_scan,coasts}.py`. Known answer: stall_holds finds **12 stalls on the morning block B, matching the log's 12**. Durations come out 2.14–2.49 s against the logged 2.28–2.62 s (a per-channel vs all-channel measurement difference). The count is the checked fact.
  - **D04.3 limit, stated before the data:** 0x44 and 0x24 are in the same Mode 01 batch (~6 ms apart, ~250 ms cycle), so a lag scan on this profile can only resolve lags near multiples of the cycle, not 50 ms steps. A "clear minimum" here bounds the delay to ~±250 ms at best. The pass criterion is unchanged; its resolution is limited.
  - D04.2: the 0x1206 fuel-cut floor re-derives as 47–57 counts (next value 144). Broadcast wheel speed `WHEEL_SPEED_6F7_A` reads 12–16 % below GPS in every coast. Check that before any speed-gated DFCO claim.
- 18:06:18Z **Block 1 stopped** (budget; my 1 Hz live logger had started only at 17:55, so it missed both holds and I wrongly reported "no enrichment" at 17:56/18:01; the capture itself caught them). Verdict **fail**, both clauses explained:
  - `acquisition_gap`: all 14 channels, the D03.18 stall shape, known;
  - `persistence_loss`: `can0.rx_over_errors` +147 during the capture, which is MCP2515 overflow on the broadcast path and a separate issue from OBD.
  - Session pulled to `~/canary-data/sessions-0918/` (31 MB, 0.8 s).
- 18:07Z **Block 1 analysis** (`_capture-fuel-pw-research-2026-09-18T174742`, 17:47:43–18:06:30Z, 18.8 min). Report `data/analyses/2026-09-18-fuel-blocks-capture-fuel-pw-research-2026-09-18T174742.{md,json}`, plus `~/canary-data/scripts-0918b/out/`.
  - **Known answer PASS:** DID RPM vs Mode 01 RPM, n_pairs 2192, slope 0.9992, r² 0.998, stable.
  - **Stall count (this drive's rule):** 6 stalls of 2.33–2.48 s; holds (0x44 > 1.05 ≥ 3 s): 17:52:18Z 6.3 s and 17:54:09Z 9.8 s; **stalls inside holds 0; enriched s lost 0**. That's luck at 6 stalls per 18.8 min (~1.3 % of time), not evidence that D03.18 is harmless.
  - **D03.8 (0x1134 = φ × 64): UNRESOLVED, consistent, not falsified.** The first samples off 64 in any session: census 64 ×2141, 73–79 ×47. Enriched pairs **n = 10** (need ≥ 30 from ≥ 3 pulls; this has 2); residual 0x1134 − 0x44 median −0.0078, p10–p90 −0.0078..−0.0070, i.e. within half a quantum (1/128). Stoich band n = 70, all 0x1134 = 64. The falsifier ("stays 64 while 0x44 > 1.10") did NOT fire. Slope test not run (range too thin at n = 10).
  - **D03.9 0x1208 = air charge: PASS** on the pre-registered test. Closed-loop refit, rich rows residual **−16.6 %** (band −14..−18); counts per g air 63,972 closed loop vs 64,457 enriched (**+0.8 %**, band ±2 %). Gated enriched n = 6 (38 ungated, 64,282 per g). Second independent replication.
  - **D03.9 0x1206 = injected pulse: UNRESOLVED.** Against the **pre-registered** line (27,747 counts/g + 41): enriched **+6.2 %** (n = 6; +8.0 % on 38 ungated), which sits in the gap between pass (±5) and fail (±8). But **closed loop is also +6.2 %** (n = 161) against that line, so the line's absolute scale moved between sessions, not the enrichment tracking. Refit on this session's closed loop (30,909 counts/g + 0.6, r² 0.969, `drifting`): enriched −1.2 %. 0x1207 is the same shape (+5.8 % CL, +7.2 % rich). **A new question for the desk: what moved the counts-per-g line +6 % between 14:08Z and 17:48Z?** Candidates: baro/altitude (3,000–3,350 m now), battery voltage (dead time), fuel temperature. It's not decided here.
  - **D04.3 transport delay:** 5 tip-ins and 5 lifts. **Tip-in: one clear minimum at 500 ms** (bracket 250–750; rms 0.080 vs 0.093 at 250 and 0.091 at 750; n_pairs 64). **Lift: no clear minimum** (250 and 500 within 10 %). Resolution is ±250 ms (the same-batch limit written above). So: tip-in passes at coarse resolution; lift is unresolved.
  - **D04.2 coasts:** 32 coasts, 25 with ≥ 2 floor samples. The 0x1206 floor re-derives as **54–58 counts** (morning 47–57), with 94–100 % λ-rail and FUEL_STATUS 4 agreement on long coasts. Cut at 1,840–3,600 rpm; the rpm/km·h groups (~36.7, ~49.8, ~71) look like three gears (not assigned). Wheel speed reads ~8–12 % below GPS here.
  - The ECT fell from 93 to 84 °C during the long descending coasts.
- 18:16Z **Desk (subagent): the 0x1206 "+6 % shift" isn't a shift.** `~/canary-data/scripts-0918b/out/shift1206.{md,json}`.
  - **Morning line reproduced:** 27,747 + 41.2, n 610, r² 0.964, `drifting` 8.4 %.
  - **Operating points don't overlap:** 57 % of the AM closed-loop rows are idle; all PM closed-loop rows are cruise. Fuel ranges: AM p95 0.0146 g, PM p5 0.0163 g.
  - **The residual rises with 0x1208 air charge at the same rate in both sessions** (3.8e-4 vs 3.7e-4 %/count). With it in the model, the pooled session step drops from +5.46 ± 0.49 % to −0.51 ± 0.98 %.
  - **One convex curve fits both sessions best** (lowest BIC): 51.6 + 25,284 f + 133,653 f². A per-session offset (dead time) is the worst model.
  - **Altitude is ruled out:** both captures ran at a median ~3,195 m GPS; the AM baro read 72 → 65 kPa, not 80. λ, coolant and MAF-vs-0x1208 are all unchanged. Voltage, IAT and fuel temperature weren't logged.
  - **So the pre-registered 0x1206 criterion was ill-posed:** a straight line fit on idle-dominated rows and extrapolated to WOT. Its +6.2 % result is recorded as-is (**unresolved**) and is not evidence against "0x1206 = injected pulse". The criterion isn't moved for this drive. The next plan replaces it.
- 18:18Z **POST-HOC, labelled as such:** enriched rows against the common curve.
  - PM closed loop +0.2 % (n 161); **PM enriched −3.4 % (n 6, p10–p90 −4.5..−1.6)**; 38 ungated rows −1.8 %; 0x1207 enriched −2.6 %.
  - The enriched fuel (0.037–0.039 g) is still **beyond** the closed-loop range (max 0.032 g), so this extrapolates too.
  - It's consistent with injected pulse, but not a pass. A decisive test needs closed-loop rows at the enriched rows' air charge (see the next-plan items).
- **Next-drive requirements for D03.9 (from the desk):**
  1. closed-loop steady holds idle → ~3,000 rpm × light → heavy load in ONE session, same rpm in two gears;
  2. add Mode 01 0x42 (voltage), 0x0F (IAT), 0x33 (baro), 0x06/0x07 (fuel trims) beside 0x1206/0x1208/MAF/0x24/0x44;
  3. an idle dead-time step: electrical loads on/off at constant air charge.
- 18:20Z Pulled every closed session from today that wasn't already on the laptop into `~/canary-data/sessions-0918/`: 134052, 140202, 152144, 153509, 160921, 171938, 173235, 173749. The open `default` session is 180630. Storage queue high-water rose to 512, still with 0 drops.
- 18:23Z **Storage high-water 1,049 of 10,000**, 0 drops, no warnings. `max_flushing_write_ms` 968: one slow flush. It may coincide with my 18:20 pulls of 8 sessions (nice 19, ionice idle), but that's not established. Watch it; not a fault.
- 18:23Z **Elevation: 3,354 m (17:56Z) → 2,488 m (18:23Z), ~870 m.** The `default` session `2026-09-18T180630` alone spans 3,079 → 2,488 m (~590 m). Whether it covers D04.8's ≥ 10 kPa baro span is desk work on the pulled session (BAROMETRIC_PRESSURE is in `default`'s AUTO set). Without an operator statement there's nothing to tag.
- 18:28:10Z **Pre-parking restart** (`sudo -n systemctl restart canary`, passwordless for systemctl). HTTP was back within 5 s. The drive window reopened as `ea2be8405c2646ed97f8b79c1738385d` on `2026-09-18T182827`; keepalive re-armed for 60 min so the trip-end digest can run after parking. After the restart: batching max_pids 6, narrowings 0, 0 "batching disabled", 0 errors. **What the restart proves:** only that a warm key-on restart is healthy; the key-off *boot* path is still unverified. The Pi-side pollers survived (separate processes).
- 18:30Z Pulled `2026-09-18T180630` (32 MB). **Baro in that one session: 70 → 79 kPa (9 kPa, 67 samples)** over 3,079 → 2,104 m.
- 18:32Z Roadmap updated: D03.8 and D03.9 notes; D04.2 `[ ]`→`[~]` (the text's fuel-cut register corrected to 0x1206); D04.3 `[ ]`→`[~]`; D04.8 note.
- 18:40Z Next-drive plan written: `data/analyses/2026-09-18-pm-next-drive-deploy-plan.md`. It replaces the 0x1206 straight-line criterion with a matched-air-charge test (written before any such data), and adds a split-batch profile for D04.3 plus an idle dead-time step. `drive.md` gained three lessons: the keepalive holds the Pi up after key-off, the stale drive window, and a live view is steering only.
- 18:51:06Z **Operator: "drive's over soon, wrap everything up."** Restart #2 (`sudo -n systemctl restart canary`), healthy in 7 s. Window reopened as `b61e7d16374e4a8fbfa53e3982ba3875` on `2026-09-18T185113`; keepalive 25 min. Batching 6 / 0 narrowings.
- 18:52Z Pulled `2026-09-18T182827` (35 MB). Pollers and live JSONL, scripts and status snapshots copied to `~/canary-data/drive-0918pm/`. Stopped the Pi live logger. The status poller keeps running until parking (it's in `/tmp` and dies at shutdown).
- Not done, and why: **the drive window on `185113` is still open.** Stopping it before parking would cut the window short. It's in-memory and dies with the Pi at keepalive expiry. Tags are already on disk (`CaptureController.add_tag` → `write_tag`, per tag); **the only loss is the durable stop tag** `stop_capture` would write, so session `185113` will have no window-stop marker.
- 18:55Z Operator: "drive's over, parked". Drive window `b61e7d16…` stopped with its durable stop tag (`reason: operator parked`). The engine was still idling at 18:57Z.
- ~19:00Z **Operator approved the 0x1208/0x1206/0x1207 relabel** → `f78956f` (catalog text; a test refuses `ms` or a scale on 0x1208). Then: "if something is mislabeled it should be fixed", followed by "we'll fix those later". The full rename plan is recorded in D03.9 (`9b1c788`). A rename subagent was stopped before it edited anything.
- **Trip-end digest: NOT observed (D03.11 still open).** The previous boot ended with an orderly shutdown at 18:59:15Z (systemd SIGTERM to every unit; ups-shutdown was stopped, not triggered), 8 min after the 18:51 restart and while the engine was still reported running. The digest needs 5 min stationary, and the journal of boot −1 has no digest/trip line, so it never fired. Session `185113` has no `digest*.json`.
- 20:01Z **The Pi booted again** (new session `2026-09-18T200256`, engine running at 20:04Z). This boot: 0 "batching disabled", max_pids 6, 0 narrowings, **`failures_while_ecu_silent` 0**, i.e. the ECU answered from the start. So this was a key-ON boot, and **D03.19's key-off boot path is still unverified.** I opened no drive window: the drive is over.
