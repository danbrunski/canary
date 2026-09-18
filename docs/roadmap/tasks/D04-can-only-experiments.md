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
| `0x2A` periodic | **closed** in `multi-pid-polling.md` ("not available on this ECM"); `batch_probe` still sends the one request and records the NRC, which is fine, but it is not an experiment |
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

- [ ] **D04.2 Map the decel-fuel-cut boundary.** DFCO only ever appears in
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

## Analyses over the D03.8 capture — needs only throttle steps and plateaus

- [ ] **D04.3 Commanded-φ → measured-λ transport delay.** Six retracted
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

- [ ] **D04.4 Where is `ECM_MAF_GPS` linear against Mode 01 MAF?** Block 6
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

## Desk work on data that already exists (tomorrow adds coverage)

- [ ] **D04.5 Broadcast inventory under road load.** The vehicle skill's
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

- [ ] **D04.6 Which of the 138 DIDs moved only under load?** The two
  `did_log` surveys (block 3b, no load; block 5, load to 4.13 V TPS) were each
  screened on their own. Nobody has yet diffed them: DIDs that were constant
  in the 7-minute no-load window and moved in the 30-minute loaded one are the
  cheapest possible shortlist of load-dependent registers, and it costs no bus
  time. `screen_didlog.py` already produces the per-DID `moves` fraction.
  **Accept:** the diff, with each newly-moving DID's raw range and the regime
  (cut / high-load / cruise) it moved in. Feeds D03.15's shortlist rather than
  competing with it.

- [ ] **D04.7 Bound the VVT duty scale from saturation, without UpRev.** The
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

## Opportunistic

- [ ] **D04.8 `BAROMETRIC_PRESSURE` against GPS altitude.** A free physics
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
