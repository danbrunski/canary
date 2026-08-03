# Roadmap — engine tuning support (UpRev campaign)

Status: **planned, gated on hardware** (2026-07-20). Straight to
`main`, no feature branches.

Blocked until both are true (operator confirms):

- [x] **G.1 — UpRev Tuner cable + license delivered** (Z1 Offroad
  order; license locks to this ECU). Done — first flash 2026-07-26,
  stock `ZS04C` → Osiris `ZS44A`, CAL_ID `1ZS04C` → `1ZS44A`.
- [~] **G.2 — LTFT was cleared and has not relearned. Gate restated.**
  The battery disconnect for the 2026-06-28 header/spacer work cleared the
  learned trim. LTFT was active before that event: across the May corpus,
  bank 1 averaged **+1.88 %** and bank 2 **+2.98 %**, with the later May 24
  drives sitting around **+4.1–4.5 % B1 / +5.8–6.3 % B2** and both banks
  reaching **+9.375 %**. STFT was near zero in those drives because LTFT was
  carrying the persistent correction.

  Since the disconnect, `LONG_FUEL_TRIM` has remained flat at 0.0 %. A full
  ECU reflash reset it again and did not make it resume learning: it was still
  0.00 across 96/98 samples at 80 °C. UpRev's `A/F ADJ-B1`/`-B2` likewise read
  **0.000 / −0.002 constant** over 518 samples, an independent K-line
  confirmation that CANary is decoding the post-reset state correctly.

  Do **not** generalize this to "the ECU never learns LTFT" or "this
  calibration cannot learn." The May records prove that it did learn before
  the battery disconnect. The supported statement is narrower: it **has not
  relearned yet** after being cleared, despite substantial warm driving.
  **Phase 2 must not wait for it.** Until it changes, trim-based analysis uses
  STFT alone and labels that limitation explicitly.

  Cross-check that validates the approach: UpRev `A/F CORR` read
  +9.1 % / +7.9 % while CANary `SHORT_FUEL_TRIM_1/2` read
  +9.18 % / +7.42 % over the same period — two toolchains, two
  transports, same answer.

  **Reconfirmed 2026-07-27 under ideal learning conditions.** A 41-minute
  drive, fully warm (100 % of the first log above 70 °C, plus a complete
  46→93 °C warm-up in the autosave), mostly closed loop, with short-term
  trim actively working the whole time (`A/F CORR-B1` swinging 76–125 %).
  Across ~4 600 samples: UpRev `A/F ADJ-B1/B2` = 0.000 / −0.002,
  CANary `LONG_FUEL_TRIM_1/2` = 0.0 exactly. The ECU had a large,
  persistent correction sitting in short-term trim and migrated none of
  it. Operationally LTFT remains pinned, but its pre-disconnect history means
  a future relearn is not ruled out.

  This is also why 3.2 matters: while LTFT remains zero, short-term re-derives
  the base-model error every start and open-loop WOT does not receive that
  closed-loop correction.

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
- **SETTLED 2026-08-01 — the ECM does not stream knock, by design. Stop
  treating the missing channel as a blocker or a fault.** The FSM
  (`xterra_fsm/ec.pdf`) answers this directly, so no further discovery
  effort is warranted:

  - **EC-34 (Electric Ignition System):** *"The knock sensor retard system
    is designed only for emergencies. The basic ignition timing is
    programmed within the anti-knocking zone, if recommended fuel is used
    under dry conditions. The retard system does not operate under normal
    driving conditions. If engine knocking occurs, the knock sensor
    monitors the condition. The signal is transmitted to the ECM. The ECM
    retards the ignition timing to eliminate the knocking condition."*
    Knock **protection is functional and continuous**; retard is an
    emergency action, not a routine trim. Recommended fuel for this engine
    is 87 octane, so the base map is designed knock-free on regular.
  - **EC-69 (CONSULT-III application table):** the knock sensor is marked
    under *self-diagnostic results* only — **not DATA MONITOR**. Sensors
    that are streamable (MAF, ECT, A/F sensor) carry marks in both. This
    matches `data/fsm/signals.json`: of 333 extracted DATA MONITOR
    parameters, **none is knock-related**.

  Conclusion: this ECU exposes knock through exactly one interface, a DTC.
  There is no knock telemetry to find — the earlier "unconfirmed, not ruled
  out" framing is superseded. UpRev's constant knock fields (`HI DET` = 1,
  `KNOCK STRENGTH` = 1280/0x500, `KNOCK EVENT` = 0,
  `KNOCK THRESHOLD %` = 1.95) therefore cannot be reading a diagnostic
  channel; they are RAM addresses from UpRev's ROM definitions, and a
  definition that does not match this `ZS44A` calibration is far likelier
  than dead hardware. Do not spend further capture, sweep, or scan-tool
  effort hunting a knock channel, and do not gate fuel or timing work on
  acquiring one.

  **Knock health is monitored via DTCs, not telemetry.** Codes are
  P0327/P0328 (bank 1) and P0332/P0333 (bank 2) — note EC-211: *"The MIL
  will not light up for these diagnoses,"* so a knock-sensor failure is
  **silent** and only a DTC poll will reveal it. That poll is the standing
  knock-health check.

  Sensor hardware, for anyone tempted to suspect it (FSM EC-211/213):
  two sensors, bank 1 on ECM terminal 15 and bank 2 on terminal 36, on the
  cylinder block in the valley **under the intake manifold** (replacement
  is a >$1000 job — require strong evidence). Terminal 1 = signal,
  terminal 2 = shield with continuity to ground. Resistance
  **532–588 kΩ at 20 °C**, measured terminal 1 to ground; the DTC logic is
  a voltage-range check, so *code on disconnect + no code on reconnect
  proves the element and harness are electrically intact*. The ECM biases
  the signal line to **~2.5 V at idle** with the mV-scale ~6–8 kHz knock
  signal riding on top, so a DC-coupled scope shows a flat 2.5 V line that
  looks like "no activity" — use AC coupling at 20–100 mV/div, and tap the
  **block**, not the manifold (gaskets isolate it). Verified on the truck
  2026-08-01: disconnect set the code, reconnect cleared it.

  Observed behaviour agrees with the FSM: exactly **one** retard event in a
  21-minute drive (2200 rpm / 89 % load, timing 11 → 8 → recovering), and
  identical WOT timing (±0.2°) between two sessions running 2–3 AFR apart —
  an engine near its knock limit would not ignore that much change in charge
  cooling. **This engine is not knock-limited as calibrated**, which is also
  why higher-octane fuel buys nothing at stock timing.

  **Operator decision 2026-07-27: external knock supervision is waived; the
  operator tunes timing in-house.** The reasoning is a precedent argument, not
  a delegation: Z1's e-tune service adjusts timing from UpRev datalogs alone
  and does not ask customers for knock hardware, so the same datalog-driven
  method is available here. No third party is involved. The risk is accepted
  deliberately, not overlooked.

  What follows from it: the *logs* become the control, so log quality is the
  safety margin — dense high-rpm WOT coverage, the knock parameters included
  even though they read constant, and `A/F CORR-B1` in the parameter list.
  The available proxy is mapped-versus-logged final `IGN TIMING` at matched
  rpm x load (`scripts/timing_compare.py`), bearing in mind that IAT, coolant
  and cam compensations also move final timing, so a delta is a signal and not
  proof of knock retard.
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
there is a fixed correction to apply at all.

**Scope note (2026-07-27), to avoid reading this against 3.3.** This section
concludes "no single multiplier" for **closed-loop** fuelling, because trim
appeared to triple across operating temperatures. 3.3 concludes a flat **+8 %**
*is* right for **WOT**. Both hold, because they cover disjoint conditions: WOT
occurs only at 33–37 °C intake (3.2a), a band inside which no temperature
variation exists to defeat a scalar. Two further caveats now apply here:

- The temperature dependence this section rests on is **itself in question** —
  see the stagnation subsection below. It may be an idle-fraction effect.
- A comparable base-model correction predates the modifications: May LTFT
  commonly carried 5–7 % total correction while STFT sat near zero. The
  temperature story and the scalar story are not competing explanations; the
  scalar is the floor and the temperature term (if it survives) rides on top.

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

Separate the two things being explained. The **level** is a longstanding
base-fuel correction: before the battery disconnect LTFT carried much of it;
afterward STFT carries it in closed loop and it appears raw at WOT. Plausible
causes include loaded fuel-pressure loss (pump, in-tank filter/regulator, or
pump voltage), uniform injector-flow loss, or MAF absolute calibration. It
needs no temperature term. What follows are candidates for the **variability**
only, and all are conditional on that variability surviving the stagnation
confound below.

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

### IAT is stagnation-dominated — this qualifies everything above

**Measured 2026-07-27, and it weakens the whole temperature story rather than
supporting it.** Intake air temperature on this vehicle is not a slowly-drifting
thermal state. It is dominated by whether air is *moving*:

| | |
|---|---|
| corr(IAT, RPM) | **−0.797** |
| corr(IAT, MAF) | **−0.542** |
| corr(IAT, ENGINE_LOAD) | −0.379 |
| corr(IAT, wall-clock) | +0.866 |

| airflow | mean MAF | mean IAT | IAT range |
|---|---|---|---|
| idle / coast < 8 g/s | 4.7 | **43.4 °C** | 34 – **52** |
| light 8–20 | 12.3 | 35.6 | 34 – 36 |
| cruise 20–45 | 33.6 | 35.2 | 34 – 36 |
| **heavy 45+** | 60.6 | **34.7** | 33 – 36 |

(n = 91, warm + fresh-join filtered per G.4.)

**Whenever air moves, IAT collapses to 34–36 °C — essentially ambient —
regardless of how hard the engine is working.** The 50 °C readings occur only
at idle, where the sensor is measuring stagnant air sitting in a hot engine
bay. Opening the throttle flushes it within seconds.

Consequences:

- **"IAT rose through the drive" is an idle-fraction artifact.** The +0.866
  time correlation is not heat accumulating; it is more stopped time later in
  the drive. The three IAT bands interleave throughout the session — soaked
  samples as early as 09:28, cool ones as late as 10:02 — so there is no
  monotonic soak ramp to point at.
- **`ENGINE_LOAD` does not control for this.** Calculated load reads ~20 % at
  idle *and* at light cruise, so binning on it mixes stationary with moving.
  Use **MAF** to separate them. This invalidates the load-control claimed in
  the first G.3a attempt.
- **Whatever underhood soak exists is measured precisely when it cannot
  affect fuelling.** The heat-shield hypothesis is not disproven, but this
  drive cannot demonstrate it, and the "+3.1 °C larger underhood rise" figure
  above is itself computed from session-mean IAT and therefore carries the
  same idle-fraction confound.
- **High IAT and WOT are close to mutually exclusive** — see 3.2a.

### Collection plan

The decisive change is to stop comparing sessions and compare **within** one:
between-session means confound ambient, run time, traffic, and heat soak.
But note that *within*-session comparison has its own confound (stagnation,
above), so the operating point must be matched on **MAF and rpm**, not load.

- [~] **G.3a — Single-session IAT sweep. First attempt 2026-07-27: direction
  confirmed, not clean.** 41-minute drive, IAT 33→47 °C, purge-closed
  closed-loop warm samples only. Trim rose monotonically with IAT —
  **2.11 → 4.49 → 5.78 → 5.89 %** across the bands, a slope of ~+0.35 %/°C
  against the +0.45 between-session estimate. But correlation was only
  **0.30** (n=110), and the bands are **load-confounded**: the low-IAT samples
  sat at 34 % load versus 23 % for the rest.

  A second reading of the same session claimed the confound was resolved by
  binning into drive-fifths at matched 20–24.5 % load, giving +0.50 %/°C with
  coolant flat at 91 °C. **That claim is withdrawn.** `ENGINE_LOAD` reads ~20 %
  at idle and at light cruise alike, so it never separated stationary from
  moving, and IAT here is stagnation-dominated (corr with rpm **−0.797** — see
  the subsection above). The apparent temperature effect may be an
  idle-versus-moving effect wearing temperature's clothes; the two are nearly
  collinear in this data and cannot be told apart from it.

  **Retry design, revised.** Match on **MAF and rpm**, not load — hold two or
  three specific points 15–20 s each. And because a hot intake charge does not
  survive airflow, the two temperatures almost certainly have to come from
  **two different sessions** (cool morning vs hot afternoon) rather than from
  early-vs-late within one drive. A within-session sweep can only vary IAT by
  varying stagnation, which is the confound itself.
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
- **Match the bound to how fast the channel changes, and budget for the
  compounding.** Requiring N joins to be simultaneously fresh multiplies the
  loss. On 2026-07-27 a within-session IAT analysis with six uniformly-bounded
  joins kept **8 of 594** STFT samples. Re-running with per-channel bounds —
  20 s for temperature (IAT, coolant move on a minutes scale), 6 s for purge,
  loop status and load (which move fast) — recovered 110. A uniform bound is
  either too loose for the fast channels or too tight for the slow ones.
- **This is the strongest argument for multi-PID batching.** At the current
  0.06–0.5 Hz per channel, most joins are stale by construction, and every
  additional channel in a filter compounds the loss. Batched channels share a
  timestamp, so the whole problem disappears. See `multi-pid-polling.md`.
- **Verify that a control variable actually controls what you think.** Binning
  on `ENGINE_LOAD` to "hold operating point constant" failed twice on
  2026-07-27, because calculated load reads ~20 % at idle *and* at light
  cruise. Two populations with completely different airflow landed in one bin
  and the conclusion drawn from it was wrong. **For anything airflow- or
  temperature-related, control on MAF and rpm**, and print the within-bin
  range of the control variable to prove the bin is actually narrow.
- **Treat a wall-clock trend as a hypothesis, not a mechanism.** "X rose
  through the drive" invites a thermal-accumulation story. Check first whether
  it tracks *duty cycle* instead: IAT's +0.866 correlation with time was
  idle-fraction, not heat soak (G.3). Before attributing a trend to time,
  regress it against what the engine was doing.

## ROM table reference — axes and decodes (settled 2026-08-03)

Sourced from the UpRev Nissan Tuning Guide (Rev 8 Feb 2022, `~/UpRev-Nissan-
Tuning-Guide-Rev2.pdf`, pp. 9–14) and verified against the 7-29 / 7-31 / 8-03
K-line logs. **2.1 and 2.3 below say "once known" — this is that.**

| table | X axis | Y axis | decode |
|---|---|---|---|
| Fuel Targets | **BFS** (theoretical injector PW) | RPM | `AFR = 14.7 × 128 / N` — verified exactly against logged `TARGET AFR` |
| Fuel Compensation Main | **Volumetric Flow Ratio** | RPM | `128 = 100 %` of measured plenum air used; above adds injector PW, below removes |
| Ignition Timing Trim (Timing Main) | **BFS** | RPM | **none — no conversion to degrees BTDC exists** |

**Fuel Compensation's X axis is Volumetric Flow Ratio, not throttle-position
percent.** Earlier notes in this campaign described its columns as "46.88/50 %
TP" — that was wrong and any analysis binned on throttle position to mirror
those columns is mis-aligned.

**There is no base timing table.** Nissan computes MBT from an internal formula;
what the ROM editor exposes is a *trim* on it:
`RPM → load → MAF → calculated MBT → ignition timing trim → knock feedback =
final timing`. Consequences: (a) counts cannot be converted to degrees — the
guide says so outright, and a bilinear regression of logged `IGN TIMING` against
the trim table over 8045 warm steady samples confirms it (R² 0.18, residual
±9.9°, per-band slopes 0.67–3.66); (b) higher values yield more advance, which
is the whole available semantics; (c) the ECU **re-advances on knock feedback
over consecutive runs**, so a single post-flash pull does not show steady state,
and "timing unchanged between sessions" may mean the ECU self-advanced to its
own limit rather than that the map is authoritative. Sibling tables:
`Timing Main` (normal), `Timing High Det` (ECU switches here on excess knock
sensor activity — the only knock-*visibility* channel this ECU offers, since it
streams no knock data), `Timing Cold` (below 70 °C coolant).

Other facts worth not re-deriving:

- **Calculated load is derived from BFS**, so any fuel-comp change inflates
  `CAL/LD` *and* walks every BFS-indexed lookup (timing, cam phasing, fuel
  targets) rightward. Measured: the 8-03 fuel-comp raise moved the spark lookup
  +0.6–0.85 columns at 4000–5500 rpm, which is the ~1.1° of advance lost there
  without touching the timing table. **Fuel and spark are not separable
  variables on this ECU** — one category per flash is a correctness requirement,
  not tidiness.
- **Closed loop runs only where the AFR target is exactly 14.7**; any other
  target puts that cell in open loop. This is what partitions the logs.
- **Rescale via the Load Point Scaler, never by editing axis numbers** (guide
  p. 13, explicit).
- **Maps 1–5 are switchable tune slots**, not an octane or knock/non-knock pair;
  there is a "Copy Map to Other Slots" utility. On this vehicle only Map 1 has
  been edited.
- Use the ROM editor's **map tracing** feature to confirm the crosshair stays
  inside a table during a WOT pull, rather than inferring cell coverage from
  logged axis channels.

## Phase 2 — Correction-surface analysis (software; needs G.2 for the trim half)

The deliverable UpRev workflow needs: for each cell, how far off is
fueling, with how much confidence.

- [ ] **2.1 — Binned AFR-error surface.** DuckDB over session Parquet:
  bin by RPM × calculated load (axes configurable to mirror the ROM's
  fuel-table axes — now documented under "ROM table reference" above;
  note Fuel Compensation bins on **Volumetric Flow Ratio**, Fuel Targets
  on **BFS**, so the two surfaces do not share one axis) and by MAF voltage (single axis, for
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
- [x] **3.2 — WOT AFR verification (safety gate). MEASURED 2026-07-27: the
  engine runs ~1.0 AFR (8 %) LEAN OF ITS OWN COMMANDED TARGET at WOT.**

  537 true-WOT samples from UpRev's synchronously-sampled K-line log
  (throttle sensor ≥ 3.85 V of a 4.19 V max, rpm > 1500):

  | rpm | n | load | target AFR | measured B1 | error | timing |
  |---|---|---|---|---|---|---|
  | 2000–2500 | 82 | 91 % | 12.63 | 13.69 | **+1.06** | 10.4 |
  | 2500–3000 | 121 | 89 % | 12.64 | 13.69 | **+1.05** | 10.1 |
  | 3000–3500 | 132 | 91 % | 12.80 | 13.79 | **+0.99** | 9.6 |
  | 3500–4000 | 135 | 91 % | 12.53 | 13.55 | **+1.02** | 9.0 |
  | 4000–5000 | 63 | 89 % | 12.26 | 13.13 | **+0.87** | 10.5 |

  Overall target 12.61, measured 13.62 (B1) / 13.58 (B2) — **8.0 % lean**,
  consistent across every rpm bin and both banks.

  **This unifies the closed-loop and open-loop pictures into one root cause.**
  Before the battery disconnect, May LTFT carried roughly 5–7 % (occasionally
  9.375 %) while STFT sat near zero. After the reset, LTFT remained zero and
  closed-loop STFT had to carry the correction. At WOT there is no closed-loop
  feedback, so the same base-model shortfall appears raw as an AFR miss.

  The chronology also argues that UpRev did **not** create the shortfall. Two
  sparse post-modification but **pre-flash** CANary enrichment events on
  2026-07-26 imply approximately **6.5 % and 7.1 % lean of commanded**. Their
  asynchronous samples make them supporting evidence rather than the dense
  UpRev log's ground truth, but the pre-modification LTFT independently shows
  that a substantial correction existed even before the headers and spacers.

  **Safety reading:** 13.6 AFR at ~90 % load, with timing already pulled to
  9–10°, is the strongest argument for fixing the base fuel model before
  touching anything else. (Knock telemetry is unavailable by ECM design, not
  missing — see the settled note under Guiding constraints. Knock *protection*
  is functional, so this is a "fix the fuelling first" argument, not a
  "we're flying blind" one.) Cats are all
  retained so the cat-delete lean condition is excluded; this is a fuelling
  shortfall, not that.

  **How to find WOT in the corpus.** `FUEL_STATUS` (PID 0x03) `== 4` is "open
  loop due to engine load" — the ECU stating it entered power enrichment.
  **Do not use RPM as a WOT proxy**: on a manual, pulls shifted at 4–5k never
  reach redline, and an earlier analysis wrongly concluded "no WOT data exists"
  from an RPM histogram. `FUEL_STATUS == 4` also covers deceleration fuel cut,
  so pair it with throttle — DFCO is closed throttle and lean, enrichment is
  open throttle and rich. On the UpRev side, throttle *sensor voltage* near its
  maximum is the cleaner discriminator; the OBD throttle *percentage* tops out
  around 89 % and understates true WOT.

  **An earlier reading of 13.2–13.5 "lean of best power" is superseded.** It
  came from CANary samples at 70–88 % OBD throttle, which were not full WOT,
  and had no target to compare against. The finding is not that the engine is
  lean of an ideal — it is lean of what its own calibration commands.

  **Rate is why this needed UpRev.** Over the same 40-minute drive CANary in
  tuning mode caught **23** throttle ≥ 70 % samples at ~0.33 Hz; UpRev caught
  **537**. Until the budget rises (multi-pid-polling.md), WOT work belongs on
  the synchronously-sampled log.

  **3.2a — WOT flushes the IAT sensor, and later heat-soaked pulls did not
  worsen.** Instantaneous high IAT and WOT do not co-occur because opening the
  throttle rapidly replaces stagnant hot air. That does not mean a pull cannot
  begin with a thermally soaked engine bay and intake tract; classifying pulls
  by their *in-pull* IAT erases that prior thermal state. CANary's session
  2026-07-27T152444, split by instantaneous IAT:

  | IAT band | n | peak throttle | peak load | samples ≥ 60 % load |
  |---|---|---|---|---|
  | cool < 38 °C | 216 | **89.4 %** | **92.2 %** | **44** |
  | warm 38–43 °C | 65 | 9.4 % | 27.1 % | **0** |
  | soaked 44 °C+ | 79 | 9.4 % | 26.3 % | **0** |

  Every heavy-throttle sample sat below 38 °C; above 38 °C the throttle never
  exceeded 9.4 %. Chronology supplies the missing thermal-state comparison:
  earlier pulls needed approximately **+8.3–9.0 %**, while later pulls after the
  engine bay and intake tract were fully heat soaked needed **+6.7–7.3 %**.
  The error did not worsen with soak; if anything it decreased slightly. A flat
  scalar is therefore the supported first correction, with no added IAT term.

- [~] **3.3 — K + MAF calibration loop.** UpRev flat AFR-test table
  (open loop) → drive with tuning capture → Phase 2 report → adjust
  K, then MAF curve, then fuel comp in ROM Editor → reflash →
  repeat until measured tracks target across the surface. Each
  iteration's session tagged with the ROM revision.

  **First K iteration, 2026-07-27:** the operator multiplied `K Fuel
  Multiplier` by 1.07. On comparable enriched samples (`load >= 85 %`, target
  AFR 12–13, RPM >= 1,800), the pre-change UpRev log measured **13.44 AFR
  against 12.53 commanded** (`measured/target = 1.073`, 298 samples). The
  post-change log measured **12.70 against 12.61** (`1.007`, 256 samples).
  That is the expected scalar response and reduces the remaining mean error to
  approximately **0.7 % lean**, within the resolution/condition mismatch of
  this road test. Individual stabilized pull segments ranged roughly
  `0.994`–`1.019`; bank 1 averaged only ~0.11 AFR leaner than bank 2.

  CANary session `2026-07-27T225711` overlaps the post-change UpRev log and
  independently shows load to 99 %, RPM to 4,025, LTFT still zero, and median
  STFT approximately -1.6 % / -3.1 % during the logged window—consistent with
  the flat correction being very close and perhaps slightly rich in closed
  loop. Do not make another K change from this result alone. This log only
  validates enriched operation through about 4,300 rpm; it does not validate
  the richer target region above that speed. Fuel-pressure verification under
  load remains mechanically prudent even though the calibration test worked.

  **Which knob: discriminator run 2026-07-27 (533 WOT samples).** UpRev logs
  carry injector pulse width (`B-FUEL SCHDL`), `MAF GM/S` and `AFR WB-B1`
  together, which lets the MAF reading be checked against the air implied by
  fuel actually delivered.

  | rpm | MAF g/s | implied VE | R = MAF/(pw·rpm·afr) |
  |---|---|---|---|
  | 2305 | 59.3 | 85.2 % | 12.140 |
  | 2790 | 74.8 | 88.5 % | 12.165 |
  | 3274 | 90.0 | 90.5 % | 11.983 |
  | 3744 | 103.7 | 91.1 % | 12.144 |
  | 4290 | 120.2 | **92.1 %** | 12.362 |

  Two readings:

  - **The MAF curve *shape* is correct.** R varies only **3.2 %** across a 2×
    airflow range (59 → 120 g/s) with no trend. There is no region where the
    sensor diverges from what fuel delivery implies, so **no reshape is
    warranted — the error is a single scalar** (13.62/12.61 = **+8.0 %** in
    the pooled sample; individual pull estimates span roughly 6.7–9.0 %).
  - **The MAF's absolute reading is physically credible.** Implied VE rises
    smoothly to 92 % at 4290 rpm — a textbook NA V6 curve for an engine with
    headers and spacers. If the MAF under-read by 8 %, true VE would be
    92–100 %, implausibly high with all four cats retained. That argues the
    airflow measurement is roughly right and the shortfall is on the
    fuel-delivery side.

  **Adjust injector/K scaling, not MAF scaling.** For *fuel* the two are
  mathematically degenerate — both multiply delivered fuel by the same
  scalar — but MAF scaling also moves the ECU's load calculation, which
  indexes timing, cam phasing and every other load-referenced table. Correcting K fixes fuelling
  without silently shifting the operating point everywhere else. This supersedes
  the task's original "adjust K, **then** MAF curve" ordering for the current
  error: the MAF curve needs nothing.

  **Check fuel pressure under load before compensating.** The VQ40DE returnless
  system specifies approximately **350 kPa / 51 psi at idle** and needs an
  inline adapter at the engine-side quick connector (there is no convenient
  rail Schrader). Observe it during the same loaded pull with an electronic
  transducer, a chassis dyno, or a mechanical gauge secured outside the cabin.
  Injector flow scales with the square root of pressure, so an 8 % mass-flow
  deficit corresponds to roughly `51 × 0.92² = 43 psi`; a fall toward the low
  40s could explain nearly the entire miss. Correct pressure at idle but sag
  under load points to pump/filter/regulator capacity or pump voltage. Normal
  pressure throughout moves uniform injector-flow loss and MAF absolute
  calibration higher on the list.

  **DONE 2026-08-03 — supply is sound; this gate is cleared.** Operator watched a
  mechanical gauge through the WOT pulls of the `8-3-2026_9-26-33_AM` log:
  **48 psi typical under load, 47 psi at the worst momentary dip**, against 49
  psi static. Because MAP ≈ baro at WOT, gauge psi *is* injector ΔP here, so the
  altitude caveat (~80 kPa, see the vehicle profile notes) does not distort the
  reading. Converting by `√(ΔP)`:

  | condition | pressure | fuel-flow effect |
  |---|---|---|
  | static → typical WOT | 49 → 48 psi | **−1.0 %** |
  | static → worst momentary | 49 → 47 psi | **−2.1 %** |
  | spec → measured WOT | 51 → 48 psi | **−3.0 %** |
  | *anticipated failure case* | 49 → 40 psi | *−9.7 %* |

  So **dynamic sag accounts for ~1 point of the residual, 2 at worst** — real but
  not the driver, and nowhere near the low-40s collapse this task anticipated.
  Two further reasons to stop treating supply as the suspect: the sag pattern is
  the *opposite* shape to the error (fuel demand peaks at 5500–6000 rpm, which is
  the best-matched band at 2.1–2.7 % lean, while the worst band is 4500–5000 at
  4.8 % on lower flow); and a supply restriction would bite hardest exactly where
  it doesn't.

  **Open lead, not a conclusion: the 3 psi static offset against spec.** Running
  48 where the calibration may assume 51 is a flat −3.0 % at *every* condition,
  which is essentially the whole residual lean. Two things stop this being called
  solved: closed-loop trims run *below* 100 (`A/F CORR` median 94/97 — the ECU
  removing fuel at cruise), whereas a globally under-delivering fuel system
  should push trims above 100; and 3 psi sits inside the tolerance of many
  mechanical gauges, so part of the offset may be the instrument. Cheap next
  step is confirming the 51 psi spec figure and cross-checking the gauge against
  a second one. Per this task's own logic, normal pressure throughout now moves
  **uniform injector-flow loss and MAF absolute calibration** to the top of the
  list — see 3.9, which is where this residual belongs.

  This check matters because the correction predates both the 2026-06-28 mods
  and the UpRev flash: May LTFT commonly carried 5–7 %, and the sparse July 26
  pre-flash WOT events already show a 6–7 % miss. Scaling K around a weakening
  mechanical component would mask it. If pressure remains near specification,
  K is the least disruptive calibration correction.

  **First correction and validation:** if loaded pressure is sound, start with
  `K_new = K_current × 1.07`, not a full pooled +8 % in one jump. Measured WOT
  AFR should move from about 13.6 toward 12.7 against the commanded ~12.6, and
  closed-loop STFT should move toward zero. Change fuel first, re-log, and only
  then touch timing. While LTFT remains zero, it will not hide an
  over-correction, so re-measure before taking a second step.
- [ ] **3.4 — AFR targets.** With the model true: stoich (closed
  loop) where the factory ran stoich; WOT/high-load targets per
  operator choice (~12.8–13.2:1 NA power mixture). Verify delivery
  with 2.1 on fresh pulls.

  Do not use target changes to conceal delivery error. First make measured AFR
  follow the existing commanded curve with K. The base tune currently commands
  roughly 12.6 through much of 2,000–3,500 rpm and richens toward ~12.2 above
  4,000 rpm. Once delivery is accurate, test whether a smoother ~12.7–13.0
  upper-rpm target maintains or improves same-gear acceleration. Change one
  target region at a time and retain the richer value unless repeatable pulls
  demonstrate a benefit.
- [ ] **3.5 — Timing & cam phasing (conservative).** Incremental, tuned
  in-house from datalogs (external knock supervision waived — see the
  2026-07-27 operator decision under Guiding constraints). CANary's role:
  before/after comparison of timing-advance behavior and pull-vs-pull
  performance deltas (estimated HP/torque channels) at matched
  conditions. Advance goes up 1–2° per iteration with a re-log between
  each. `KNOCK STRENGTH` is **not** a usable feedback signal — it reads a
  constant `0x500` because the ECM streams no knock data at all (settled
  2026-08-01; see Guiding constraints). The observable proxy is final
  `IGN TIMING` at matched rpm × load: a *retard* the map didn't command is the
  ECM's emergency knock response and means the last increment was too much.

  **Three-era observation, freshness-bounded 2026-07-27.** The flash boundary
  is pinned by capture metadata: stock `ZS04C/1ZS04C` through 16:21 MDT on
  2026-07-26; Osiris `ZS44A/1ZS44A` begins with the 16:31 session. Re-running
  the comparison with a 3 s RPM/load/timing freshness limit discarded 3,103 of
  8,491 timing records. In cells with usable post-flash coverage, idle was
  effectively unchanged (16.8° stock-after-mods → 15.9° UpRev), while several
  loaded cells were retarded: 2,000–2,500 rpm / 55–65 % load **25.3° → 18.0°**;
  2,500–3,000 / 35–45 % **32.5° → 27.9°**; and 3,000–3,500 / 85–100 %
  **24.6° → 13.7°**. The last cell is well axis-matched but still sparse (5
  stock samples across 3 sessions; 6 UpRev samples across 2 sessions).

  Treat the old observed timing as a reference ceiling, not a table to restore
  wholesale. After AFR is corrected, add only **1–2° per iteration** in the
  affected loaded cells and re-measure. Do not add the full observed 7–11° in
  one flash — not because knock is unobservable (the ECM never streams it; that
  is settled), but because the only feedback available is *after-the-fact*: the
  ECM's emergency retard shows up as logged `IGN TIMING` falling below what the
  map commands. Small increments keep that signal interpretable. Note the
  baseline is favourable — the engine is **not knock-limited as calibrated**
  (one retard event in 21 min; WOT timing identical across sessions 2–3 AFR
  apart), so 3000–4500 rpm at high load, where the map is most conservative, is
  where advance has the most to give.

  **Cam phasing comes after fuel and ignition.** The headers and plenum spacers
  can shift the rpm regions where overlap, scavenging and manifold resonance
  help. Candidate tests are modest low/midrange intake-cam advance and a
  different taper through 3,000–5,000 rpm. Change one rpm region per revision
  and compare same-gear acceleration. Cam changes alter airflow, so every
  accepted change triggers another commanded-versus-measured AFR check.
- [ ] **3.6 — Throttle/ETC and drivability passes.** WOT-at-all-speeds
  fix and any map-switching setup; CANary just documents before/after.

  Logged throttle reaches its physical maximum during pulls, so there is no
  evidence of a universal WOT restriction. Investigate only specific
  requested-versus-actual divergences: low-speed or gear-dependent limits,
  closure near shifts, and pedal-map response. Preserve fine control at low
  pedal positions rather than maximizing the whole map.

  For the manual transmission, separately evaluate throttle decay, DFCO
  entry/exit, fuel restoration near idle, and rev hang between shifts. These
  are drivability changes, not power claims; capture pedal, throttle, lambda,
  rpm and fuel status around each event before altering them.

- [ ] **3.7 — Limiters and idle/startup only for a demonstrated need.** A
  higher rev limit does not create power. Raise it only if measured power is
  still increasing and the change avoids an otherwise unnecessary shift;
  change the speed limiter only for a concrete use case. Likewise, leave idle
  and startup calibration alone unless logs and symptoms show hot-idle
  instability, cold-start difficulty, accessory-load dip, flare or rev hang.
  Current idle timing is close to the previous calibration and idle is
  generally stable.

- [ ] **3.8 — Temperature compensation: verify protection, do not tune the
  stationary IAT artifact.** Do not add fuel merely because IAT rises while
  stopped; airflow flushes the sensor and later thermally soaked pulls did not
  become leaner. Instead verify that ordinary summer moving-air temperatures
  do not cause unnecessary retard, and retain high-temperature enrichment and
  timing protection. A temperature-table change requires matched moving-air
  observations, not idle-versus-cruise bins.

- [ ] **3.9 — Residual injector-latency / MAF verification.** Revisit these
  only after pressure, K, AFR targets, timing and cam revisions are stable:

  - Correct high-pulse-width delivery but residual idle error points toward
    injector latency/dead time.
  - The same percentage error everywhere remains a K/scalar issue.
  - A repeatable error versus MAF voltage or airflow region is the condition
    for reconsidering MAF shape.

  Do not change latency and K together. The existing MAF curve is a
  verification item, not an active tuning candidate: its WOT shape is
  internally consistent and its implied VE is credible. Reopen it only if
  clean, matched-condition residuals cannot be reconciled with K and latency.

**Campaign order after mechanical checks:** fuel pressure (**cleared 2026-08-03**,
see 3.3) → K correction →
verify commanded versus measured AFR → recover modest timing → optimize AFR
target → test cam timing → address throttle/shift drivability → revisit
latency/MAF only if a residual pattern demands it. Use one variable category
per flash so every observed change remains attributable.

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
