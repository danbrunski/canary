# Roadmap — engine tuning support (UpRev campaign)

Status: **planned, gated on hardware** (2026-07-20). Straight to
`main`, no feature branches.

Blocked until both are true (operator confirms):

- [x] **G.1 — UpRev Tuner cable + license delivered** (Z1 Offroad
  order; license locks to this ECU). Done — first flash 2026-07-26,
  stock `ZS04C` → Osiris `ZS44A`, CAL_ID `1ZS04C` → `1ZS44A`.
- [~] **G.2 — LTFT is not going to relearn. Gate restated.**
  Original wording assumed `LONG_FUEL_TRIM`'s flat 0.0 % was a
  pending relearn ("needs warm steady-state drive cycles"). That is
  now disproven:

  * It survived a **full ECU reflash** — about as hard an adaptive
    reset as exists — still 0.00 across 96/98 samples at 80 °C.
  * UpRev's `A/F ADJ-B1`/`-B2` read **0.000 / −0.002 constant** over
    518 samples. That is a *completely independent transport
    (K-line) and decoder* reaching the same answer, which rules out
    a CANary decode bug.

  So the ECU genuinely does not learn long-term fuel trim on this
  calibration. **Phase 2 must not wait for LTFT.** Restated gate:
  trim-based analysis uses **short-term trim alone**, and every
  correction surface must be labelled as such — with LTFT pinned at
  zero, STFT carries the entire correction permanently rather than
  the residual it normally represents.

  Cross-check that validates the approach: UpRev `A/F CORR` read
  +9.1 % / +7.9 % while CANary `SHORT_FUEL_TRIM_1/2` read
  +9.18 % / +7.42 % over the same period — two toolchains, two
  transports, same answer.

Software phases (1–2) don't need the cable and are overnight-eligible;
the campaign itself (Phase 3) needs the truck, the cable, and the
operator.

## Why this exists

The dev vehicle (2008 Xterra, VQ40DE, manual) has a factory upstream
wideband A/F sensor (λ PIDs 0x24/0x25, polled since
engine-analysis-and-mapping 2.1) and is getting an UpRev Tuner
license for ECU reflashing. The goal: use CANary as the long-duration
data engine and map-correction calculator behind UpRev's ROM Editor.

**Division of labor (fixed):** UpRev owns flashing, consult-protocol
logging (Cipher: knock strength, base fuel schedule, injector PW),
and every write to the ECU. CANary stays **read-only on the bus** —
it logs every drive, aggregates AFR error and fuel trims into binned
correction surfaces, and guards against post-flash regressions. No
tuning action ever flows through CANary; NFR-06 is untouched.

**Vehicle mod state** (profile-level facts for the dev vehicle — keep
out of shared code paths):

- Shorty headers — **all 4 catalytic converters retained** (the
  UpRev-documented VQ40DE lean condition from primary-cat delete does
  not apply, but WOT AFR still gets verified before anything else).
- Aftermarket cat-back exhaust.
- Intake manifold spacers.

Net effect on the *open-loop* calibration (WOT fuelling, timing and
cam schedules): the VE shape shifted across the RPM band, so the
factory feedforward tables no longer match the plant. Tuning theory in
one line: make the ECU's model true (measured AFR == target, trims
near zero), then move setpoints (WOT AFR ~12.8–13.2:1, timing toward
MBT under knock supervision, cam phasing for the new scavenging).

**Do not extend that reasoning to closed-loop fuel trim.** This is a
MAF-based engine, so a VE change does not by itself put the ECU's
airflow measurement wrong — the extra air still passes the sensor.
See G.3, which measured what actually drives trim here (intake air
temperature) and rules VE gain out as a direct mechanism. The headers
running without heat shields matter for trim *thermally*, not
volumetrically.

## Cross-links

- `engine-analysis-and-mapping.md` — 2.1 (wideband λ polling) and 2.6
  (custom-PID engine) are landed prerequisites; **5.1 binned-analysis
  endpoint is the natural substrate for Phase 2 here** — if 5.1 lands
  first, Phase 2 becomes a thin client of it; if this lands first,
  build 5.1's DuckDB binning core here and let 5.1 reuse it. Its
  Phase-4 total-trim map is the substrate for 2.2; 5.4 A/B session
  comparison covers the before/after halves of 3.5/4.3.
- `eval-longitudinal-and-dtc.md` 6.4 (Q12) — the `parquet_query`
  underscore-dir filter **must keep an explicit-name escape hatch**
  (noted in that task's text): Phase 2 here queries `_tuning-*`
  sessions by name through that layer.
- `data-collection-plan.md` — capture-session conventions; tuning
  drives follow the physical-action tagging convention.
- Memory `project_ltft_zero_ecu_side` — background on G.2.

## Guiding constraints

- Whole-repo gate before every commit: `ruff check . && ruff format
  --check . && mypy src && pytest` (+ `pnpm test` / `pnpm typecheck`
  for FE work).
- **Read-only on the bus (NFR-06), permanently.** Mode 01/02/06/09/22
  reads only. UpRev hardware is the sole writer, on its own cable.
- Nothing Xterra-specific in shared code: capture profiles, PID sets,
  and bin axes are config/profile data, not constants.
- **Tuning drives are not "normal" telemetry.** Flat-AFR-target test
  maps force open loop and produce fueling the behavior models have
  never seen. Keep tuning sessions out of the training corpus via the
  existing `_`-prefix storage isolation (decision 1.2 below).
- Timing/knock work stays on UpRev's side. Generic OBD does not
  expose knock strength; CANary observes timing-advance behavior but
  never drives advance decisions. UpRev's own guide: don't trust
  ECU knock params for aggressive work — use Cipher logs and, if it
  ever gets serious, a dedicated knock monitor.
- Each accepted flash changes the engine's "normal." Model refits are
  part of the campaign (Phase 4), not an afterthought.

---

## Phase 1 — Tuning capture mode (software; no truck needed)

A focused fast-poll capture profile: the wide default PID rotation is
a few Hz per channel, too slow to resolve a ~5 s WOT pull per cell.

- [ ] **1.1 — Focused poll profile.** A named capture profile (config,
  per-profile) that restricts the Mode 01 rotation to a small tuning
  set — λ B1/B2 (0x24/0x25), MAF, RPM, calculated load, timing
  advance, STFT/LTFT B1/B2, commanded equivalence ratio (0x44), IAT,
  TPS — and suspends Mode-22 DID windows (no `10 C0` blackouts) for
  the duration. Target the max per-channel rate the bus allows;
  measure and record achieved Hz in session metadata. Activation via
  capture API/UI flag, auto-reverts when the capture ends.
- [ ] **1.2 — Corpus quarantine for tuning sessions.** Tuning-mode
  captures write under a `_tuning-*` session prefix (excluded from
  training discovery, live listing, retention sweep — the existing
  `_` convention) with `session_type: tuning` metadata. *Default
  decision: `_` prefix, not post-hoc archiving — open-loop AFR-test
  drives must never be discoverable by training even transiently.*
  Operator can override for post-tune "new normal" sessions, which
  deliberately DO enter the corpus (Phase 4).
- [ ] **1.3 — Pull markers.** Reuse physical-action event tagging for
  tuning runs (`wot_pull_3rd`, `steady_cruise_2k`, …) so analysis can
  slice by maneuver. Predefine the tag vocabulary in config; tags are
  data, not code.

## G.3 — Fuel trim tracks intake air temperature; find out why before rescaling the MAF

**Gates the MAF half of Phase 3.3.** Not a software task. It decides whether
there is a fixed correction to apply at all — and the current answer is no.

### What was measured

Short-term fuel trim, filtered to purge-closed, closed-loop, coolant > 70 °C.
The filters matter: EVAP purge alone shifts trim 4.15 points, and UpRev's
logger has no purge parameter at all, so its logs cannot be filtered this way.

Across 15 post-mod sessions (n ≈ 83 samples each), bank-1 trim spans
**1.15 % – 10.63 %** — a nine-point swing on *fixed hardware*. That variability,
not the mean, is the finding.

It is almost entirely explained by intake air temperature:

| | |
|---|---|
| corr(IAT, bank 1 trim) | **+0.73** |
| corr(IAT, bank 2 trim) | **+0.74** |
| corr(bank 1, bank 2) | **+0.92** |
| slope | **+0.45 trim points per °C** |
| across the observed 37–54 °C | **+7.6 points** of the 9.5-point spread |

| IAT | sessions | mean trim |
|---|---|---|
| ≤ 42 °C | 4 | **2.76 %** |
| ≥ 49 °C | 5 | **8.44 %** |

### The apparent step at the mod boundary is a temperature confound

An earlier revision of this section attributed a ~+3-point step to the
2026-06-28 header and spacer install. **That does not survive controlling for
temperature**, and the correction matters because it was gating the whole
investigation on the wrong question.

| era | sessions | mean IAT | mean trim | IAT range |
|---|---|---|---|---|
| May (pre-mod) | 6 | 33.7 °C | 2.58 % | 31–40 °C |
| July (post-mod) | 15 | 45.6 °C | 5.99 % | 37–54 °C |

The eras barely overlap in temperature, so comparing era means compares
temperatures. The raw difference is +3.41 points; temperature alone predicts
**+5.34**, i.e. it *over*-predicts the step. Restricted to the overlapping
36–41 °C band, post-mod trim is **3.49 points lower** than pre-mod (1 May
session against 4 July — thin, so read it as "no mod effect is detectable",
not as "the mods improved it").

### But the mods plausibly act *through* temperature

Splitting IAT into ambient and underhood contributions — session-minimum IAT
approximates ambient after a soak; the rise to warm-running is engine-bay heat:

| | May | July | Δ |
|---|---|---|---|
| IAT at start (≈ ambient) | 34.2 °C | 44.1 °C | **+9.9** |
| **rise, cold → warm** (underhood) | **+2.5 °C** | **+5.6 °C** | **+3.1** |

Ambient genuinely rose ~10 °C (unusually hot weather), *and* the underhood
contribution more than doubled — consistent with the **shorty headers running
without heat shields**. Excluding two July sessions that begin at 70 °C (hot
restarts, not cold soaks): +6.7 °C ambient, +3.7 °C larger rise. Same
conclusion.

So the mods likely do raise trim — **indirectly, by heating intake air**, not
by changing VE or by leaking. That points at a different remedy than a MAF
rescale: heat shielding, intake-tract isolation, or a colder air source.

### What this rules out

- **A fixed MAF scaling error.** A scaling error is a constant percentage.
  This one triples across ordinary operating temperatures, so no single
  multiplier is correct. **Do not apply one derived from a single log.**
- **The ECU reflash.** Post-flash trim sits inside the pre-flash range.
- **A single-bank exhaust leak.** Both banks correlate with IAT almost
  identically (0.73 / 0.74) and with each other at 0.92. An earlier reading
  of a bank-1 bias as a per-bank fault was an artifact of five sessions; the
  split grows with the overall level (corr 0.87), which is one bank having
  slightly more gain on a *shared* cause, not a separate leak.
- **VE gain as a direct mechanism.** This is a MAF-based engine — PID 0x0B
  (manifold pressure) is refused by this ECU, consistent with MAF-primary
  fuelling. A MAF measures *mass*, and in steady state every gram the engine
  ingests passed the sensor. More VE means the MAF reads more and fuel scales
  with it. "VE gain needs a rescale" is a **speed-density** mechanism, where
  the ECU infers airflow from RPM × MAP × a VE table; it does not transfer.
  Larger plenum volume from spacers is a *buffer*, not a source — it changes
  transient manifold filling, which washes out in steady state.

### Candidates that remain

All must be **temperature-dependent and common to both banks**:

1. **Fuel heat soak.** Hot fuel is less dense; injectors meter volume, so the
   same pulse width delivers less mass. Lean, trim adds. Scales with engine-bay
   temperature, affects both banks equally.
2. **MAF temperature-compensation error.** A hot-film sensor is mass-sensing
   but its compensation is imperfect, and a contaminated element degrades worse
   hot. Under-reads → lean → trim adds.
3. **A thermally-opening intake leak** at the plenum or spacer gaskets. The
   carb-cleaner test was clean, but it was run *cold* — exactly when such a
   leak would be shut.

### Collection plan

The decisive change is to stop comparing sessions and compare **within** one:
between-session means confound ambient, run time, traffic, and heat soak.

- [ ] **G.3a — Single-session IAT sweep.** One long session from cold start
  through full heat soak, returning to the *same* load/rpm holds at several
  points as IAT climbs. If trim tracks IAT inside a single session, the
  between-session confounds vanish. This is now the most valuable single log.
- [ ] **G.3b — Steady-state load sweep, repeated at two IATs.** Hold several
  load points 15–20 s each, purge-closed, once cool and once heat-soaked.
  Flat across load = scaling or fuel; falling with load = a leak; wavy with
  rpm peaking in a band = manifold-resonance/VE-model mismatch. Doing it at
  two temperatures also separates the load shape from the thermal effect.
- [ ] **G.3c — Ambient reference.** `AMBIANT_AIR_TEMP` (PID 0x46) is **not
  supported** by this ECU, so ambient cannot be separated from underhood
  directly. Session-minimum IAT is the current proxy and it fails on hot
  restarts. Consider an external temperature source, or restrict the proxy to
  sessions following a verified cold soak.
- [ ] **G.3d — Physical check, hot and cold.** Intake tract *and* header
  flanges, checked both cold and heat-soaked, since a thermally-opening leak
  is consistent with everything above and the existing clean result was cold.
- [ ] **G.3e — Re-run the MAF-voltage binning on clean data**, purge-closed
  and closed-loop, at matched IAT. The earlier flat result (slope −2.16 %/V)
  came from purge-contaminated UpRev data and should not be leaned on.

G.3a, G.3b and G.3e are satisfied by tuning-mode captures now that
`EVAPORATIVE_PURGE` and `FUEL_STATUS` are in the channel set.

## G.4 — Stale-join contamination: a methodology gate on every binned analysis

**Not a task. A standing constraint on how binned analysis is done here**,
recorded because it already invalidated one published finding.

### What happened

A ZS04C→ZS44A timing comparison reported that the tune had retarded high-load
ignition by 8–11°, described as confirmed by two independent measurement paths.
**That finding is withdrawn.** It was an artifact of unbounded ASOF joins.

`TIMING_ADVANCE` polls at ~0.06 Hz and `ENGINE_LOAD` at ~0.08 Hz. An ASOF join
pairs each timing reading with the *most recent* load reading regardless of
age, so a sample can be filed into an rpm × load cell the engine occupied ten
or more seconds earlier. Adding a 3 s freshness bound to the same query drops
**717 of 1701** pre-flash samples and **132 of 196** post-flash — and every
high-load cell then falls to n = 0–3, i.e. there was never enough clean
post-flash high-load data to support the claim.

The claim is *unsupported*, not disproven. Pre-flash high-load cells survive
the filter (n ≈ 46–59); the post-flash side does not.

### The same failure in a second place

Four WOT samples appeared to show a rich cluster at 12.63 AFR. They are one
lambda reading at 24–35 s age, re-paired four times (see 3.2). Without
carrying the source sample age they are indistinguishable from measurements.

### The rules that follow

- **Always bound ASOF joins by sample age**, and make the bound explicit
  rather than defaulted. `scripts/timing_compare.py` takes `--max-join-age`
  (default 3 s) and prints how many rows it dropped.
- **Always carry and report the joined sample's age** in any per-sample table.
  A stale value looks exactly like a fresh one.
- **Report the drop count.** If a freshness bound discards two-thirds of the
  data, that is the headline, not a footnote — it means the conclusion rested
  on almost nothing.
- **Prefer synchronously-sampled sources for binned work.** UpRev's K-line log
  puts every parameter in one frame, so it needs no join and has no staleness.
  That is why the lean-WOT reading in 3.2 survives while the CANary-side
  timing delta does not.
- **This is the strongest argument for multi-PID batching.** At the current
  0.06–0.5 Hz per channel, most joins are stale by construction. At ~33 Hz the
  freshness bound costs almost nothing. See `multi-pid-polling.md`.

## Phase 2 — Correction-surface analysis (software; needs G.2 for the trim half)

The deliverable UpRev workflow needs: for each cell, how far off is
fueling, with how much confidence.

- [ ] **2.1 — Binned AFR-error surface.** DuckDB over session Parquet:
  bin by RPM × calculated load (axes configurable to mirror the ROM's
  fuel-table axes once known) and by MAF voltage (single axis, for
  the MAF transfer curve). Per cell: median and IQR of
  (measured λ − commanded λ), sample count, achieved poll rate.
  Sparse cells flagged, never interpolated silently.
- [ ] **2.2 — Trim-based correction surface.** Same binning over
  (STFT + LTFT) from *normal closed-loop driving* — this is the
  every-drive advantage; it needs G.2. Cross-check against 2.1: the
  two surfaces should agree in closed-loop regions; divergence is a
  data-quality finding to surface, not average away.
- [ ] **2.3 — Correction-table export.** Emit a correction grid
  (percent per cell) aligned to UpRev fuel-compensation / MAF-table
  axes, as CSV + rendered heatmap. Include per-cell confidence so
  low-N cells are visibly untrustworthy. CLI first
  (`analysis/tuning_report.py` or similar); MCP/HTTP + dashboard
  panel only if the CLI proves out.
- [ ] **2.4 — Coverage report.** Which cells lack data → generates
  the next drive plan ("need 2500–3500 RPM at 60–80% load"). Pairs
  with 1.3 tags.

## Phase 3 — The campaign itself (needs truck + cable + operator; blocked overnight)

Order is safety → model-truth → setpoints, per UpRev's guide.

- [ ] **3.1 — Baseline.** Pin 2–3 known-good pre-tune sessions
  (replay regression baseline + before/after comparison). ECU ROM
  dump saved and backed up before any flash.
- [~] **3.2 — WOT AFR verification (safety gate).** *First data in
  2026-07-27; not yet enough to act on, and it inverts the usual
  assumption.* Stock-tune WOT pulls through the Phase 1 capture;
  confirm commanded enrichment is delivered and nothing runs lean.
  Cats are all retained so the cat-delete lean condition is excluded,
  but headers+spacers still shift VE — verify, don't assume.

  **How to find WOT in the corpus.** `FUEL_STATUS` (PID 0x03) `== 4` is
  "open loop due to engine load" — the ECU telling you directly that it
  entered power enrichment. That is the reliable detector. **Do not use
  RPM as a WOT proxy**: on a manual, WOT pulls shifted at 4–5k never
  reach redline, and an earlier analysis wrongly concluded "no WOT data
  exists" from an RPM histogram. Note `FUEL_STATUS == 4` *also* covers
  deceleration fuel cut, so pair it with throttle — DFCO is closed
  throttle and lean (λ ≈ 1.16), enrichment is open throttle and rich.

  **What the July corpus actually contains:** 25 samples at 70–88.6 %
  throttle, 83–90 % engine load, 2050–4163 rpm, with `FUEL_STATUS == 4`
  present — genuine WOT events.

  **Only 4 carry usable AFR**, because `O2_WR_LAMBDA_S1` polls at
  ~0.13 Hz:

  | throttle | rpm | load | AFR | λ sample age |
  |---|---|---|---|---|
  | 82.0 % | 4163 | 87.5 | **13.17** | 1.2 s |
  | 88.6 % | 2338 | 89.0 | **13.26** | 0.8 s |
  | 80.0 % | 2338 | 84.7 | **13.52** | 0.4 s |
  | 88.6 % | 2863 | 86.7 | **13.52** | 0.4 s |

  The July 18–19 WOT samples have **no** lambda (the wideband PID was
  not yet polled), and four samples reading "12.63" are the *same stale
  value* at 24–35 s age — an ASOF-join artifact, not measurements.
  **Always carry the source sample age when joining slow-polled
  channels**; without it those four look like a rich cluster.

  **Reading:** best power on gasoline is ~12.8–13.2 (λ 0.87–0.90), so
  this engine runs **at or lean of best power at WOT — not rich.** The
  common assumption that a stock NA calibration runs rich at WOT with
  room to lean out does **not** hold here, and altitude (~80 kPa) does
  not make it richer as one might expect. If anything the move is
  slightly *richer*, toward 12.8–13.0, which would gain a little power
  and buy knock margin that is currently invisible (see G.3 and the
  knock note in Guiding constraints).

  (An earlier revision paired this with a claim that ZS44A had pulled
  8–11° of high-load timing. **That claim is withdrawn** — see G.4. The
  lean-WOT reading here stands on its own; it comes from UpRev's
  synchronously-sampled log, not from an ASOF join.)

  **Not actionable yet — 4 samples, two at the same rpm, one pre-flash
  and three post.** Do not move a WOT fuel table on this. What it does
  establish is that the measurement is possible and worth doing
  properly. See `multi-pid-polling.md`: at ~33 Hz a single 5 s pull
  yields ~165 time-aligned samples per channel instead of one.
- [ ] **3.3 — K + MAF calibration loop.** UpRev flat AFR-test table
  (open loop) → drive with tuning capture → Phase 2 report → adjust
  K, then MAF curve, then fuel comp in ROM Editor → reflash →
  repeat until measured tracks target across the surface. Each
  iteration's session tagged with the ROM revision.
- [ ] **3.4 — AFR targets.** With the model true: stoich (closed
  loop) where the factory ran stoich; WOT/high-load targets per
  operator choice (~12.8–13.2:1 NA power mixture). Verify delivery
  with 2.1 on fresh pulls.
- [ ] **3.5 — Timing & cam phasing (conservative).** Incremental,
  UpRev-side, knock supervision via Cipher logging. CANary's role:
  before/after comparison of timing-advance behavior and pull-vs-pull
  performance deltas (estimated HP/torque channels) at matched
  conditions. No aggressive advance without dedicated knock hardware.
- [ ] **3.6 — Throttle/ETC and drivability passes.** WOT-at-all-speeds
  fix and any map-switching setup; CANary just documents before/after.

## Phase 4 — Post-tune model management

- [ ] **4.1 — "New normal" cutover.** After a tune revision is
  accepted: collect fresh normal-driving sessions (NOT `_`-prefixed),
  refit behavior models on the post-tune corpus, and archive-or-keep
  pre-tune sessions per operator call. Expect the learned surfaces
  (fueling, timing) to shift — that's the tune working, not a
  regression.
- [ ] **4.2 — Flash-revision provenance.** Record active ROM/tune
  revision in session metadata (manual entry is fine) so corpus
  filters and model provenance can partition by tune, same as they
  partition by profile/VIN.
- [ ] **4.3 — Replay regression guard.** After each flash: replay a
  pinned 3.1 baseline session to confirm the *pipeline* is unchanged,
  then compare live post-flash behavior against pre-flash surfaces to
  quantify what the tune moved.
