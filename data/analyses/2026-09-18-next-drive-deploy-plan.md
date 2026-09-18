# After the 2026-09-18 drive: what we learned, and the next drive

Written 2026-09-18 ~16:45Z, after the operator powered the Pi off at 16:32Z.
Evidence and timeline: `data/analyses/2026-09-18-drive-log.md`. Task truth:
`docs/roadmap/tasks/D03-open-work.md`, `D04-can-only-experiments.md`.

## Pending for the Pi

**No code is pending.** The Pi is at `63d5e51` (hash verified 15:21Z); every
commit after it is docs or analysis output. On the next boot, check two things
before anything else:

1. `journalctl -u canary -b | grep -c "batching disabled"` → **0**. This is the
   first real key-off *boot* since the batching fix. It was verified on a
   mid-session key-off, not on a boot.
2. `/status` → `health.obd_scheduler.batching`: `max_pids 6`,
   `size_narrowings 0`, `failures_while_ecu_silent > 0`.

`MemoryMax`/`MemorySwapMax` will still read 6442450944 / 0 and will still be
**unenforced** until the operator decides D03.21.

## What this drive taught us

### About the engine's instruments (the outcome)

| belief going in | what the data says now | strength |
|---|---|---|
| 0x1208 is injector pulse width; its constant is ~15 % low at WOT | **0x1208 is air charge**, ~63,850 counts per gram of air per intake event. It ignores a measured 20 % enrichment and fuel cut, and doesn't scale with baro across 63–80 kPa | strong: desk on 09-17, replicated on independent 09-18 data with the prediction made first |
| 0x1206/0x1207 are intake pressure (this engine has no MAP) | **injected-fuel pulse, B1/B2 (candidate)**: +17 % in a pull, fixed ~52-count floor in fuel cut, closed-loop line gives 0.41 ms dead time and ~290 cc/min at 0.01 ms/count | medium: the fuel-cut floor is replicated; the rich side rests on 8 samples from one pull |
| 0x1134 may be target φ = raw/64 | baseline confirmed: 64 on 4,630 of 4,630 samples while PID 0x44 sits at stoich | necessary, not sufficient: no enriched sample exists in any same-capture pairing |
| ECM_MAF_GPS reads ~5 % low at high flow | **1:1 within ±2 % from 3 to 134 g/s.** The 0.934 slope was the DID being read ~115 ms after Mode 01 | strong below 90 g/s, thin above (81 pairs) |
| PID 0x44 reads 1.000 in closed loop | it dithers between 0.9796 and 1.0204 and never reads 1.000; the enrichment gate has to be >1.05 | strong |
| operating altitude is ~80 kPa | 80 kPa is home. This drive ran at 63–73 kPa, and the models fire out-of-distribution there on baro, timing and air mass | strong |

The pattern is the one AGENTS.md warns about. Three "findings" from 09-17 were
artifacts of one wrong label (0x1208) and one timing skew. None of them was
about the engine.

### About the rig

- **A key-off boot silently halved the default profile's data rate** for the
  whole session: batching narrowed itself off against a sleeping ECM. This is
  fixed (`63d5e51`) and verified live. Every earlier drive where the Pi booted
  before the engine ran single-PID, so corpus per-channel rates differ by boot
  order.
- **One request timeout stalls a hybrid capture for ~2.4 s on every channel,
  and benches the affected DID for 30–48 s** (D03.18). That happened 12 times
  in 34 minutes. A WOT pull lasts 3–5 s, so one stall can erase the pull the
  drive exists for.
- **The digest budget covers only ~13.6 min of default driving** (~2,355
  rows/s), so no normal drive gets a digest (D03.6). The refusal path itself
  works: sidecar, WARNING, HTTP 409.
- **The Pi's memory limits are decorative** (kernel `cgroup_disable=memory`);
  D03.21.
- **My laptop poller has 12–15 min holes** (WSL suspends between wake-ups).
  Cumulative daemon counters bridged them; rate claims from polling can't.

### About method

- The day's biggest result came from **desk analysis of data already on disk**,
  done while the truck idled. No capture was involved.
- The known-answer gate (DID RPM vs Mode 01 RPM) correctly refused a pure-idle
  window and passed a driving one.
- A prediction written down before the capture (0x1206 floors in fuel cut,
  0x1208 doesn't) is what turned a desk finding into a replicated one.

## Desk work before the next drive, in order

1. **Operator decisions** (see the drive log, "For the operator"):
   - D03.21: memory cgroup plus reboot;
   - accept "0x1208 = air charge", catalogue 0x1206/0x1207 as injector-pulse
     candidates, and correct the 0x1208 text in `data/dids.yaml`.
2. **D03.18: bound the hybrid-cycle timeout stall.** Do this before the pull
   drive, not after: it is the one defect that can void the experiment.
3. **Move `analyze_fuel_blocks.py` into `tools/` with tests.** Today it lives in
   `~/canary-data/scripts-0918/`. A fixture slice of 09-17 block 6 gives it a
   known answer: 0x1208 must fail the rich-row validation by −14..−18 %.
4. **A pull-rate profile.** At 2 Hz per DID, a 4 s pull yields ~8 samples of
   0x1134 and ~4 after the stability gates. Resolve a minimal plan (PIDs RPM,
   MAF, 0x44, 0x24, FUEL_STATUS; DIDs 0x1134, 0x1206, 0x1208, RPM) and see
   whether a 0.33 s cycle (3 Hz) is feasible at the unchanged 90 req/s. Don't
   raise the budget.
5. **D03.6 digest cost fix** (needs the parity treatment; not unattended).
6. D04.4's `conditions` field on 0x1209, and D04.6/D04.7 (no bus time needed).
7. **A poller that survives laptop sleep:** keep the laptop awake for the
   drive, or run the 5 s curl loop on the Pi writing to `/tmp`. It costs
   15 ms per request and is collection, not analysis.

## The next drive: one experiment closes three tasks

**Goal:** enriched samples in a capture that holds 0x1134, 0x1206/0x1207,
0x1208, PID 0x44, PID 0x24 and Mode 01 MAF together. That single dataset
decides D03.8, D03.9 and D04.3, and gives D04.2 for free.

**Needs:** paved road, warm engine (coolant ≥ 85 °C), `fuel-pw-research` (or
the 3 Hz variant from desk item 4), and D03.18 fixed.

### Predictions, written before the data exists

| claim | passes if | falsified if |
|---|---|---|
| 0x1134 = commanded φ × 64 | `compare_channels` vs PID 0x44 over ≥30 enriched (0x44 > 1.05), stability-gated pairs from ≥3 independent pulls: slope 1.00 ± 0.03, `stable`, residual ≤ one quantum (1/64 = 0.0156) | slope outside 0.95–1.05, or 0x1134 stays 64 while 0x44 > 1.10 |
| 0x1206 = injected pulse | the closed-loop line (27,747 counts/g + 41) predicts enriched rows within ±5 % | rich-row residual beyond ±8 % |
| 0x1208 = air charge | the same test fails by −14..−18 % on rich rows, and counts per g air stays within ±2 % of closed loop | it tracks the enrichment |
| transport delay (D04.3) | the lag scan of 0x44 vs 1/λ has one clear minimum, reported separately for tip-in and lift | no minimum: the pairing gate can't be tightened |

**Sample-size arithmetic:** ≥30 gated enriched pairs at 2 Hz and roughly 50 %
gate survival means ~30 s of held enrichment. That's **six 5-s pulls**, or
better, **two or three long uphill 4th-gear WOT holds of 10 s or more**, which
are also steadier. One pull isn't a result (09-17's n = 8 is why 0x1206 is
still a candidate).

### Drive order (≈45 min, isolated capture ≤ 1/3 of a longer drive)

1. **Boot check** (above), then 10 min plain `default` while warming up.
2. **Start the capture. 8–10 min closed-loop cruise** with 20-s holds at
   ~60, ~90 and ~110 km/h. This fills closed-loop cells at high air per event,
   which were empty on 09-17.
3. **Enrichment set:** 3+ long WOT holds (3rd/4th gear, 2,500→5,000 rpm, uphill
   if available), each followed by a full lift. Tag each `wot_start`/`lift`.
4. **Three or four tip-ins from cruise, held ≥5 s, then lifts** (D04.3).
5. **Coasts (D04.2):** closed throttle in 3rd and in 4th from ~3,000 rpm down to
   idle, one in neutral. 0x1206's floor now marks fuel cut directly, so this is
   a per-coast table of rpm and speed at cut and at refuel.
6. **Stop the capture while still moving**, read the verdict, return to
   `default`.
7. **Restart the daemon ≤10 min before parking** (not 15), so D03.11's
   small-session `digest.json` can finally be observed. Skip this if D03.6 has
   landed.

### Optional, if the UpRev laptop comes along

One parked, warm window with UpRev logging `INJ PULSE-B1` (ms) and base fuel
schedule. It is the cheapest *external* known answer for 0x1206's 0.01 ms/count
scale and for "0x1208 = base fuel schedule / air charge" (D03.10). UpRev and
CANary share 7E0/7E8, so run CANary passive with a TTL and compare in time, or
confirm first that single-DID polling can coexist.

### Free, on any drive with a big elevation change

**D04.8 and 0x1208-vs-baro need one continuous ≥10 kPa span in a single
session.** Today's two sessions spanned 5 and 2 kPa. The drive home from
~2,900 m to ~2,070 m is about 9 kPa: boot the Pi before leaving, stay on
`default` (that also gives the corpus the altitude data D03.20 needs), and tag
the top and the bottom.

## Not next

- D03.14/D03.15 (BFS axis) and D03.16/D03.17 (Mode 21 / memory read) didn't run
  today and don't block the fuel workflow. Run them after the pull drive.
- D02.2 stagger and D01.5 re-measure are hygiene; don't spend pull-drive
  minutes on them.
