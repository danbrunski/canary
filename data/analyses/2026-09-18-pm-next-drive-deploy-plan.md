# After the 2026-09-18 afternoon drive: the next drive

Written 2026-09-18 ~18:40Z, near the end of the afternoon drive. Evidence:
`data/analyses/2026-09-18-pm-drive-log.md`. It supersedes
`2026-09-18-next-drive-deploy-plan.md` for the fuel workflow. That file's
boot checks and "Not next" list still stand.

## Pending for the Pi

**No code is pending.** The Pi is at `63d5e51`. A new profile, if the desk
builds one (below), is live-saved via `PUT /control/capture/profiles` if all
its DIDs and PIDs are already on the startup list; otherwise it needs a
restart. Check the key-off **boot**: it was never observed, because the Pi
stayed up on keepalive all day.

## Where the fuel workflow stands

| claim | state after 09-18 PM | what closes it |
|---|---|---|
| 0x1208 = air charge (not fuel) | **passed**, 3rd independent replication (rich −16.6 %, per g air +0.8 %) | done. Needs operator sign-off to re-describe it in `dids.yaml` |
| 0x1134 = commanded φ × 64 | consistent: 10 enriched pairs within ½ quantum, 2 holds | **≥ 30 enriched pairs from ≥ 3 holds** (criterion unchanged) |
| 0x1206/0x1207 = injected pulse | unresolved: the pre-registered straight line was ill-posed (idle-fit, extrapolated) | the new criterion below, on closed-loop rows that overlap WOT air charge |
| D04.3 transport delay | tip-in ~500 ms ± 250; lift unresolved | 0x44 and 0x24 in **separate** Mode 01 batches, or a faster cycle |
| D04.2 DFCO boundary | 32 untagged coasts; floor = 0x1206 at 54–58 counts | tagged coasts in 2 gears + gear assignment + wheel-speed-vs-GPS settled |
| D04.8 baro vs altitude | 9 kPa span in one session (`2026-09-18T180630`) | desk analysis of that session; a 10 kPa span closes it |

## Desk before the next drive, in order

1. **Operator:** sign off on 0x1208 = air charge, and catalogue 0x1206/0x1207
   as injector-pulse candidates (carried over).
2. **D03.18** (the hybrid-cycle timeout stall). This drive got lucky with 0
   of 6 stalls inside a hold; at ~1.3 % of time it will eventually eat one.
3. **A `fuel-pw-research-2` profile** (new name; don't edit the saved one):
   - add Mode 01 0x42 (module voltage), 0x0F (IAT), 0x33 (baro) and 0x06/0x07
     (bank-1 fuel trims);
   - put 0x44 and 0x24 in **different batches**, so D04.3 can resolve below
     250 ms;
   - measure that the batch plan resolves at 90 req/s; don't raise the budget.
4. **D04.8** on `2026-09-18T180630` (9 kPa span, pulled).
5. Move `analyze_fuel_blocks.py`, `stall_holds.py`, `lag_scan.py`,
   `coasts.py` and `shift1206.py` from `~/canary-data/scripts-0918*/` into
   `tools/` with fixtures (block B = no-enrichment named empty; 09-18 PM block
   = 2 holds, 6 stalls).

## Predictions for the next capture, written now

| claim | passes if | falsified if |
|---|---|---|
| 0x1134 = φ × 64 | as before: `compare_channels` vs 0x44 over ≥ 30 enriched, gated pairs from ≥ 3 holds, slope 1.00 ± 0.03, `stable`, residual ≤ 1/64 | slope outside 0.95–1.05, or 64 while 0x44 > 1.10 |
| 0x1206 = injected pulse (**replaces the straight-line test**) | **closed-loop rows in the same capture at the enriched rows' 0x1208 air charge (±5 %)** predict enriched rows within ±5 % after scaling by measured φ; the needed closed-loop rows must exist (≥ 20), else unresolved | enriched residual beyond ±8 % against matched-air closed-loop rows |
| dead time (0x1206 offset vs voltage) | at idle with constant 0x1208, an electrical-load step of ≥ 0.4 V moves 0x1206 by a consistent offset | no step at ≥ 0.4 V → voltage isn't the curvature cause |
| D04.3 | lag scan with 0x44/0x24 in separate batches has one clear minimum per direction, ≤ 100 ms wide | no minimum |

The "matched air charge" requirement is the lesson of 09-18 PM: the
0x1206-vs-fuel relation is convex in air charge (+~7 % idle → cruise), so any
test that extrapolates a line from one operating region to another fails
for that reason alone.

## Drive order (~45 min, isolated capture ≤ ⅓)

1. Boot check, then 10 min `default` warm-up.
2. Start `fuel-pw-research-2` (or `fuel-pw-research` if the desk item isn't done).
3. **Idle dead-time step (1 min):** parked, warm, idle. Headlights + blower +
   rear defrost on for 20 s, off for 20 s, twice. Tag each switch.
4. **Closed-loop at high air charge:** in 3rd, part-throttle uphill at
   2,500–3,500 rpm, held 20 s each at two loads. This fills the air-charge band
   that WOT reaches (0x1208 ~25–40k counts) while staying closed loop.
5. **≥ 3 WOT holds ≥ 10 s**, 3rd/4th, uphill, full lift after each; tag
   `wot`/`lift`.
6. **Tagged coasts** in 3rd and in 4th from ~3,000 rpm, plus one in neutral.
7. Stop the capture while moving; read the verdict.
8. Restart ≤ 10 min before parking (skip if D03.6 has landed).

## Rig lessons from this drive (in `drive.md`)

- **A Pi-side live-value logger started late gave me a wrong picture.** I
  reported "no enrichment" twice while the capture held two holds. The capture
  is the truth; a sampled live view is steering only.
- **The keepalive keeps the Pi up on battery after key-off.** "The Pi's off"
  wasn't true this morning. Disarm it at wrap-up.
- The stale drive window from a previous drive stays open across engine starts
  on the same daemon. Stop it and reopen on the current session.
