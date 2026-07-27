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
