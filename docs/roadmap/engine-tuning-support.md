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

Net effect: volumetric-efficiency shape shifted across the RPM band;
factory feedforward calibration (MAF curve, K, open-loop WOT fueling,
timing/cam schedules) no longer matches the plant. Tuning theory in
one line: make the ECU's model true (measured AFR == target, trims
near zero), then move setpoints (WOT AFR ~12.8–13.2:1, timing toward
MBT under knock supervision, cam phasing for the new scavenging).

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

## G.3 — Separate VE gain from an exhaust leak before rescaling the MAF

**Gates the MAF half of Phase 3.3.** Not a software task; it decides whether
the correction we are about to apply is real.

### What was measured

Short-term fuel trim, filtered to purge-closed, closed-loop, coolant > 70 °C
(the filters matter — EVAP purge alone shifts trim 4.15 points, and UpRev's
logger has no purge parameter at all, so its logs cannot be filtered this way):

| era | sessions | mean | median | range |
|---|---|---|---|---|
| pre-mod (May) | 5 | **2.80 %** | 2.41 % | 0.10 – 6.29 |
| post-mod (July) | 15 | **5.90 %** | 5.25 % | 1.25 – 10.63 |

Roughly +3 points on the mean; the ceiling more than doubles. Bank 2 tracks
bank 1. The step aligns with the 2026-06-28 shorty-header and plenum-spacer
install (ledger `45f52a69`, `corpus_boundary=true`), **not** with the ECU
reflash — an earlier reading of this data blamed the flash and was wrong,
having compared a single pre-flash idle session against a single post-flash
one.

Two things the numbers do *not* say. The session-to-session swing is not new
(pre-mod already ranged 0.1–6.3), so the mods raised the level rather than
introducing the variability. And IAT was 32–40 °C in May against 37–64 °C in
July, so some of the gap is plausibly heat rather than hardware.

### The three candidates

1. **Genuine VE gain.** The engine really does flow more and the stock MAF
   curve under-reports at the new operating points. Benign, and a MAF rescale
   is exactly the right fix.
2. **Exhaust leak at the header flanges or collectors**, upstream of the O2
   sensors. Fresh air past the sensor reads lean and the ECU adds fuel the
   engine never needed. A carb-cleaner test around the *plenum* does not test
   for this — wrong end of the engine. That test was run and came back clean.
3. **Plenum spacer leak** — unmetered air downstream of the MAF. This *was*
   the right target for the carb-cleaner test, so the clean result is real
   evidence against it. Weakest of the three, though a leak that only opens
   once hot can survive a cold spray.

**Why this gates the tune:** if it is VE gain, the ~+9 % correction is real
airflow and rescaling the MAF is correct. If it is an exhaust leak, rescaling
adds fuel the engine does not need, and the truck goes genuinely rich the
moment the leak is repaired.

### The discriminating signal: trim versus load

- A **leak** (exhaust or intake) admits a roughly fixed amount of unmetered
  air. As a *fraction* of total flow that is large at idle and shrinks as load
  rises → **trim starts high and falls steeply with load**.
- **VE gain / MAF scaling** error is proportional to airflow → **trim stays
  roughly flat across load**.

Note that exhaust and intake leaks share this shape; they are separated by
*location* (the carb-cleaner test), not by the load curve. Since the intake
was tested clean, a falling curve now points at the exhaust.

One existing data point, weakly in favour of VE gain: binning A/F CORR by MAF
voltage came out flat (slope −2.16 %/V, spread 4–6 points across 1.0–2.6 V).
But that came from purge-contaminated UpRev data, so it should be redone
before anyone leans on it.

### Collection plan

- [ ] **G.3a — Cold-start warm-up log.** Cold start through to full warm at
  idle, logging trim, coolant, and `CATALYST_TEMP_B1S1`/`B2S1` as an exhaust
  temperature proxy. An exhaust leak commonly seals as the metal expands, so
  trim trending *down* as exhaust temp rises is a leak tell. VE gain is
  temperature-stable apart from IAT density effects.
- [ ] **G.3b — Steady-state load sweep.** Hold several load points at a fixed
  rpm, 15–20 s each, purge-closed. This is the flat-versus-falling test and
  the single most decisive measurement. Blips are useless here — the whole
  point is holding one operating point long enough to fit it.
- [ ] **G.3c — Bank asymmetry check.** Shorty headers are per-bank, so a
  single leaking flange should show as one bank's trim running higher. Bank 1
  already runs 1–2 points above bank 2 *pre-mod*, so the baseline asymmetry is
  pre-existing; what matters is whether the **gap widened** (it looks larger
  post-mod, 2.8–3.5 points, but on few sessions).
- [ ] **G.3d — Physical check, cold.** Header flanges and collector joints
  with soapy water, or listen for a tick that fades as the engine warms. The
  earlier carb-cleaner test covered the plenum only.
- [ ] **G.3e — Re-run the MAF-voltage binning on clean data**, purge-closed
  and closed-loop, to confirm or overturn the flat result above.

All of G.3a–c are satisfied by tuning-mode captures once
`EVAPORATIVE_PURGE` and `FUEL_STATUS` are in the channel set (they are).

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
- [ ] **3.2 — WOT AFR verification (safety gate).** Stock-tune WOT
  pulls through the Phase 1 capture; confirm commanded enrichment is
  delivered and nothing runs lean. Cats are all retained so the
  cat-delete lean condition is excluded, but headers+spacers still
  shift VE — verify, don't assume.
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
