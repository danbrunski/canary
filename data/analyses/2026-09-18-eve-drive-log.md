# 2026-09-18 (evening) autonomous drive log

_State file for the third /loop drive of the day. Resume from here; don't redo work._
_Earlier today: `2026-09-18-drive-log.md` (AM), `2026-09-18-pm-drive-log.md` (PM). Plans: `2026-09-18-next-drive-deploy-plan.md` (criteria, per the operator, unchanged) and `2026-09-18-pm-next-drive-deploy-plan.md` (the added tests below)._

## Glance
- **Next: 3+ WOT holds ≥ 10 s** (3rd/4th, uphill), full lift after each, **and 20-s part-throttle closed-loop holds in 3rd at 2,500–3,500 rpm uphill**. Say "wot"/"lift" and I tag them. At a parked stop: the 1-min idle lights/blower/defrost on-off step.
- `fuel-pw-research-2` capture running since 20:06:03Z (4 h drive, so the ⅓ budget is 80 min). Warm (ECT 91 °C), 1,980 m.
- D03.18 is NOT fixed: holds, not pulls; stalls inside holds are counted.

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

## Work notes
- 20:01Z Pi booted (key-on). 20:04Z operator: "new drive is starting now" (4 h).
- 20:05Z Storage high-water 1,320 within 3 min of boot, 0 drops: a boot-time backlog. Watch it.
- 20:05Z `fuel-pw-research-2` saved live (PUT, no warnings). All added channels are in the ECU's supported Mode 01 set. **Still to do: mirror it into the repo's profiles file within the hour.**
- 20:06:03Z capture started; truck already moving at 60 km/h, so the idle step is deferred.
