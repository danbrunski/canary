# B04 — timing resolution and airflow consolidation

> **Archived 2026-11-08.** The other items landed. B04.2, B04.6 and B04.8 wait on
> drives below 76 kPa and continue as **D03.57**, **D03.58** and **D03.59** in
> `../D03-open-work.md`.

**Depends on:** B01/B02; B03 governs artifact status and publication.
Addresses the behavior-model part of D03.20. Follow
[B00](../B00-behavior-models-README.md).

**2026-10-04 follow-on:** [B08.3/B08.4](../B08-firmware-informed-behavior.md)
evaluate the newly decoded throttle-fill path and actual timing lookup
coordinates/selection state against the baselines below. B08 owns those new
candidate comparisons; the existing measurements and model statuses remain
historical evidence, not proof that the new candidates work.

## Timing

- [x] **B04.1 — Reproduce the timing baseline.** Record held-out errors,
  coverage and physical warning widths for RPM/load and RPM/IAT, including
  the cooler September drives. Preserve the roughly 24° median warning width
  as an observed limitation, not a desired threshold. Separate calibration,
  temperature, load and steady/transient regimes before comparing candidates.
  *Done 2026-09-22 (no fit):* 31 independent September sessions (10 grouped
  drives contributed evaluated timing rows) incl. the cool 09-18/09-20 ones,
  pinned by content hash in
  `tests/fixtures/behavior_validation/evidence_b04_timing.yaml`; run through
  `scripts/evaluate_behavior_models.py --mode corrected`. RPM/load: 41,317
  eligible → 36,169 evaluated, MAE 5.86°, bias +0.93°, p95 18.4°, R² 0.323,
  **median warning half-width 23.9° (recorded as the observed limitation)**,
  skill 0.228, 2.19 events/h, envelope 1.00; drive MAE 0.05–8.23°, bootstrap
  90 % 3.83–7.35°. RPM/IAT: 5,088 of 34,887 eligible evaluated (coverage
  0.15), MAE 5.52°, skill −0.083, 58 events/h. Regime strata are now data
  (`behavior_validation.yaml: report_regimes`) and reported per model by the
  evaluator (`per_context`): by calibration (k106 5.77° vs post-flash
  ZS44A-20260920 6.94°), IAT, coolant, load (15.3° below 20 % load vs 1.8° at
  20–30 %), baro (+6.99° bias on the single 50–70 kPa drive) and
  steady/transient (4.24° vs 7.51°). Evidence: [report §1](../../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [~] **B04.2 — Build a load-aware context candidate.** → **carried to D03.57** (2026-11-08). Retain RPM and load
  as essential explanatory inputs; assess IAT/coolant and relevant air-density
  context on independent drives. Prefer the smallest interpretable conditional
  model justified by coverage. If additional axes cannot be supported, keep
  the model exploratory with named missing coverage rather than substituting
  an RPM/IAT-only fit or broadening bands until it passes.
  *Assessed 2026-09-23 on the current calibration; stays exploratory.* The
  RPM/load surface was refit on ZS44A-20260920 (candidate v3) and judged on 2
  untouched drive groups. Strata ([report](../../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §4):
  - IAT: [20,30) °C MAE 6.08°, [30,40) 2.89°, [40,50) 0.20°. High IAT is
    idle/low airflow (stagnation), so IAT stands in for operating point, not
    context.
  - Coolant: < 60 °C 8.88° (249 rows, 2 cold starts) against 4.21° at
    90–100 °C.
  - Load: **< 20 % load 10.9°** (overrun/decel) against 1.18° at 20–30 %.
  - Baro: only 76–90 kPa.

  Neither IAT nor coolant explains the dominant error, and no axis is added
  (the fitter is 2-D). **Missing coverage:** any drive below 76 kPa on this
  calibration (no air-density axis is assessable); more cold starts for a
  coolant axis; and a way to model low-load overrun, which is outside what
  IAT/coolant context addresses.
- [x] **B04.3 — Define and test useful timing resolution.** Put the intended
  sustained timing change and its required duration into the evaluation spec,
  justified by measured noise and the diagnostic question. Validate simulated
  shifts, transient retard controls and unmodified held-out data through actual
  eligibility and persistence. Report detection delay and covered envelope;
  an unexpected timing change must not be labelled proven knock retard.
  *Done 2026-09-22 (machinery + spec only; no fit):* `ResolutionSpec` on
  `ValidationCriteria` (sustained change, required duration, delay budget,
  **measured** noise with its basis, diagnostic question, `claim`,
  `not_identified`, transient controls) — declared for both timing recipes in
  `data/nissan-xterra-2008/behavior_validation.yaml`, justified by a measured
  2.19° steady-state within-cell spread (p95 1.0°, p99 6.0°, 6,221 rows), not
  the 0.5° PID LSB. The evaluator now injects each transient control at the
  window's first evaluated row and scores it through the same eligibility and
  persistence gate, and reports detection delay and covered envelope per
  synthetic case. On the untouched September drives the −3°/60 s step is seen
  in 210/718 clean windows (0.29) with a 2.0 s median delay and envelope 1.00,
  while the −6° transients (3 s and 10 s) raise events in 20 % / 22 % of clean
  windows against a declared 0 % ceiling: **the 3-of-5-ticks-in-8 s
  persistence gate cannot express "sustained"**, which is the named remaining
  gap (a per-model persistence requirement, not wider bands). Claims reworded:
  no timing recipe says knock retard any more, and
  `tests/test_behavior_resolution.py` fails if one does. That file also covers
  the spec's refusals (noise ≥ change, control ≥ duration, delay > duration,
  target ≠ meaningful_deviation, no long-enough fault) and the machinery
  end-to-end. **Along the way:** the evaluator's row clock read microsecond
  frames as `int64/1e9`, compressing every interval 1000x — events, dedupe
  and synthetic windows were all wrong before today (`EVALUATOR_VERSION` 2).
  Evidence: [report §0, §2](../../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [x] **B04.4 — Publish only demonstrated improvement.** Use B02's untouched
  test drives, same eligible points and physical-unit errors to compare against
  the existing surface and simple baseline. Publish via B03 if qualified;
  otherwise leave the candidate diagnostic-only. RPM/IAT does not regain an
  independent alert solely because it supplies useful context.
  *Done 2026-09-23 — not qualified, left diagnostic-only.* Same 7976
  eligible rows on the untouched `2026-09-22T154716` + `2026-09-22T233855`
  drives:
  - published v2 → candidate v3: MAE 5.60 → 4.85°, median warning width
    23.1 → 13.2°, off-map 1000 → 715, events 3 → 3
  - skill against the training baseline 0.35 (passes ≥ 0.3)
  - fails: MAE and width > 3°, synthetic −3°/60 s detection 0.56 < 0.8,
    transient controls 0.30/0.38 > 0

  `behavior_policy.yaml` keeps it exploratory. RPM/IAT regains nothing
  (held-out coverage 24.7 %, failed, not published). Evidence: [report](../../../../data/analyses/2026-09-23-behavior-refit-validation-report.md)
  §2–§4, `2026-09-23-behavior-refit-comparison.json`.

## Air charge and VE

- [x] **B04.5 — Preserve physical definitions in the contract.** Trace
  `compute_anomaly_signals` and `_theoretical_air_mass_per_cycle_g`: distinguish
  measured MAP, measured baro and missing/default IAT in model inputs and
  provenance. Do not silently train/serve one VE definition as another.
  Retain raw air-mass/air-charge quantities for inspection and restrict
  diagnostic claims to relationships the measurements actually identify.
  *Done 2026-09-22:* `_theoretical_air_mass_per_cycle_g` preferred MAP, fell
  back to baro and substituted 25 °C for a missing IAT, so one channel name
  meant two quantities (~85 % vs ~25 % at part throttle) depending on the
  capture profile. Split into `derived:VE_MANIFOLD_PERCENT` (measured MAP +
  measured IAT) and `derived:VE_AMBIENT_PERCENT` (measured baro + measured
  IAT) — no fallback between references, no default temperature, a missing
  input means no value. `derived:VE_PERCENT` is kept **bit-identical** and
  labelled legacy because tracked IF/LSTM manifests consume it (the 2026-09
  IF-blind incident); no recipe targets it. `derived.resolved_dependencies()`
  reports which input each group resolved to, and the evaluator records it per
  session (`derived_input_resolution`). `ve_vs_rpm_load` now targets the
  ambient channel and a new `ve_manifold_vs_rpm_load` the MAP one; the shipped
  artifact predates the split, so a carry-over is flagged
  `contract_changed:recipe` and the evaluator labels the substitution in
  `row_contract` (`refit.legacy_reproduction_spec`). Air-mass and VE notes no
  longer claim to identify leaks/restriction/MAF drift, and both VE recipes
  state they share air mass's MAF/RPM measurement. Tests:
  `tests/test_anomaly_signals.py` (four new cases). Evidence:
  [report §3](../../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [~] **B04.6 — Validate normalization across conditions.** → **carried to D03.58** (2026-11-08). Keep the air-charge
  recipe, quantify its missing grid coverage, and compare density-aware
  alternatives on multiple independent low/high-pressure and temperature
  sessions. Record units and calibration dependence of raw DID counts. Missing
  pressure/IAT is an unavailable input, not evidence of stability.
  *Measured 2026-09-23; the pressure comparison is data-blocked.* On the
  current calibration:
  - Air charge (raw DID counts, scale unpinned, calibration-dependent; grid
    edges carried from the ZS44A-k106 prior): 41 of 49 cells populated,
    held-out MAE 1771 counts against a 1500 target. Fails.
  - Air mass and ambient VE: MAE 0.125 g/cycle and 3.17 %, relative
    resolution (MAE / warning width) 0.45 vs 0.40. Both fail B02.
  - Test rows span baro **79–82 kPa** and IAT 20–58 °C.

  **Remaining:** independent drives below 76 kPa (none on ZS44A-20260920).
  Until then, density normalization is not validated across pressure
  ([report](../../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §4).
- [x] **B04.7 — Run the airflow ablation on identical observations.** Compare
  air-mass, VE and their combination using the intersection of eligible rows;
  separately report each model's exclusive coverage. Include no-fault controls
  and explicitly synthetic single-sensor/common-input and airflow-change
  cases. Count unique detections, misses and delays, not just output correlation
  or aggregate alert reduction. Label common-input blind spots honestly.
  *Done 2026-09-23* on the candidates' shared test drives. Coverage: 7139
  rows both; 99 air-mass-only (VE actual stale 51, off-map 48); 3 VE-only.
  No-fault control: 0 events for each on the intersection (2.47 h).
  Input-level synthetic cases, 142 clean 60 s windows each:

  | case | air mass | VE | VE-unique | OR / AND |
  |---|---|---|---|---|
  | MAF −10 % (common input; unmetered air looks the same) | 33 (delay 23 s) | 22 (24 s) | 0 | 33 / 22 |
  | baro −3 kPa (VE-only input) | 0 | 0 | 0 | — |
  | IAT +10 °C (VE-only input) | 0 | 0 | 0 | — |

  Blind spots: air mass cannot see baro/IAT errors by construction; VE's
  ~7.9 % warning width is wider than the 3.4–3.9 % such errors produce;
  neither separates MAF bias from unmetered air. Evidence: [report](../../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §4,
  `2026-09-23-behavior-refit-comparison.json` `b04_7_air_mass_ve_ablation`.
- [~] **B04.8 — Make the conditional retirement decision.** → **carried to D03.59** (2026-11-08). Demote the
  air-mass alert only if validated VE/context coverage preserves useful unique
  sensitivity under the predeclared B02 criteria. Otherwise retain it with
  corrected context or diagnostic-only status and name the unresolved evidence.
  Check fusion grouping so transforms of the same MAF/RPM evidence are not
  treated as independent confirmation. Publish through B03 and link the
  behavior-side evidence back to D03.20.
  *Decided 2026-09-23: air mass is not retired.* VE adds **0** unique
  detections (B04.7) and fails B02, so no validated replacement exists. Air
  mass also fails B02 (MAE 0.125 > 0.08 g, synthetic detection 0.25) and
  stays diagnostic-only (promoted as part of the 2026-09-23 diagnostic-only
  refresh, B03.9).
  **Fusion grouping fixed 2026-09-23:** `FusionScorer.behavior_source` now
  takes the graded `channel` and derives the group itself
  (`src/models/fusion.py::behavior_fusion_group`). Air mass and all VE
  channels vote as one `behavior:airflow` voice, and the caller can no
  longer pass a per-channel group. Evidence:
  `tests/test_fusion.py::test_one_measurement_is_one_voice_across_its_derived_channels`.
  The behavior-side evidence is linked into D03.20 (2026-09-23 paragraph).
  **Unresolved:** the decision must be re-run with a < 76 kPa drive on the
  current calibration.

  [Report](../../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §4.

- [x] **B04.9 — Pair timing with its axes per read, or bound the skew.**
  The evaluator that produced B04.1–B04.4 reads a frame resampled onto a 1 Hz
  grid. Each channel is put on that grid by `.resample().last()` and then
  forward-filled (`src/preprocessing/resampler.py`, `_resample_one_session`).
  A fast channel's fill is bounded at `max_gap`, 2 s on the 1 Hz grid. One
  row can therefore pair a `TIMING_ADVANCE` read with an rpm or load read up
  to about 2 s away, so every residual and warning width above mixes reads
  from different instants.
  B08.4 measured this on `_capture-ram-study-2026-10-04T204557` (2026-10-08):
  470 of 1,131 timing reads had no `obd:RPM` read within 250 ms.
  **Accept:**
  - Measure the timing-to-rpm and timing-to-load separation distribution
    that the grid actually produces on B04.1's evaluation drives.
  - Then do one of two things:
    - grade timing on per-read pairs under a stated `PairingPolicy`
      (`src.analysis.timing_lookup.identical_samples` does this for B08.4);
    - or bound the separation in the evaluation contract, with the bound
      justified by the measured timing noise.
  - Restate the affected B04 numbers under the chosen rule, or record why
    they stand.

  *Done 2026-10-08: timing is graded per read.*
  - **Measured.** B04.1's manifest, the published (pre-`2971d79`) RPM/load
    surface, evaluator at HEAD. For each graded grid row, the separation
    between the acquisition times of the timing read and the rpm/load reads
    the grid put beside it, per row (resampler semantics re-run on read ids):
    - RPM: 24,682 rows; p50 0, p90 0, p95 0.13 s, p99 1.25 s, max 2.65 s;
      992 beyond 0.25 s, 338 beyond 1 s.
    - Load: p95 0.25 s, p99 0.75 s, max 2.65 s; 1,126 beyond 0.25 s.
    - Either axis beyond 0.25 s: 1,282 rows (5.2 %); beyond 1 s, 353.
    - The tail sits in a few sessions. `2026-09-18T134345`: rpm p50 1.25 s,
      253 of 261 rows beyond 0.25 s. `2026-09-18T213818`: 449 rpm and 671 load
      rows beyond 0.25 s. Elsewhere, ≤ 57 rows per session.
    - IAT (RPM/IAT surface, slow tier): p50 0.75 s, p95 2.6 s, max 47.6 s.
  - **The population has no gap at any bound.** For each of the 27,366
    acquired timing reads in B04.1's non-capture sessions, the nearest read
    of each axis:

    | nearest read | 0 s | 0.10–0.15 | 0.20–0.25 | 0.25–0.30 | 0.30–0.40 | 0.40–0.50 | 0.50–0.75 | 0.75–1.0 | 1.0–1.5 | > 1.5 s |
    |---|---|---|---|---|---|---|---|---|---|---|
    | RPM | 24,374 | 1,975 | 283 | 198 | 331 | 140 | 32 | 0 | 0 | 18 |
    | load | 25,520 | 62 | 658 | 352 | 350 | 204 | 108 | 65 | 26 | 9 |

    Bins with fewer than 10 reads in both rows are omitted. Reads continue
    smoothly across 0.25 s, so no bound falls in a gap.
  - **What a substituted axis read does (the bound's basis).** Each
    same-response (timing, rpm, load) tuple is an anchor. One axis is
    replaced by each read of it 0.25–1.5 s away, the other is held, and the
    change in the published surface's expected timing is measured per
    substitution. The noise floor is the 2.2° measured noise.

    | axis, separation | substitutions | axis change p90 / p95 | expected shift p95 | share > 2.2° |
    |---|---|---|---|---|
    | RPM, 0.25–0.5 s | 157 | 17.5 / 27.5 rpm | 0.06° | 0.7 % |
    | RPM, 0.5–1.0 s | 25,777 | 87.5 / 150 rpm | 0.47° | 1.9 % |
    | RPM, 1.0–1.5 s | 21,784 | 87.5 / 150 rpm | 0.48° | 1.9 % |
    | load, 0.25–0.5 s | 267 | 3.1 / 7.5 % | 0.61° | 1.3 % |
    | load, 0.5–1.0 s | 25,638 | 14.1 / 25.5 % | 5.1° | 11.2 % |
    | load, 1.0–1.5 s | 21,749 | 14.5 / 26.7 % | 5.2° | 11.2 % |

    Timing itself moves p90 2° / p95 6° over 0.25–0.5 s (7 % beyond 2.2°)
    and p90 11° / p95 18–19° over 0.5–1.5 s (26 %). IAT changes by at most
    1 °C (one LSB) at p99 up to 4 s apart.
  - **Bound: 0.5 s for RPM and load.** It is the widest bound whose
    substitutions keep the expected timing inside the measured noise. From
    0.5 s, load substitutions push 11 % beyond it. The 0.25–0.5 s sample is
    small (157 and 267 substitutions), because most reads land in the same
    response or a whole round away. 0.5 s is wider than the timing lookup's
    0.25 s `max_acquisition_skew_s` (B08.4); that figure is a proposed
    lookup-tuple limit, not this measurement. An earlier draft of this item
    adopted 0.25 s and claimed it kept "the same response and the adjacent
    read". That described the rows the bound kept, not the population.
  - **Rule.** `behavior_validation.yaml` declares `axis_pairing`
    (`ValidationCriteria.axis_pairing`) on both timing recipes, with the
    measured basis above: RPM and load 0.5 s, IAT 4.0 s.
    `scripts/evaluate_behavior_models.py` (corrected mode) grades such a
    model one row per output read
    (`behavior_validation.per_read_frame`). Each axis read is paired once,
    on the acquisition clock, by `match_ordered` (output as reference),
    within its bound. The other columns come from the grid row of the
    second the read was acquired in.
    - Refusals, by name: `unpaired:<axis>`, `not_captured:<axis>`,
      `clock_unsupported:<spec>` (round or unknown stamps),
      `no_grid_context`.
    - `evaluate_rows` refuses a frame paired for another output or other
      axes, and a frame that does not state its policy.
    - It also refuses a frame mixing policies, including any row with no
      policy (`axis_pairing_mixed`). A grid row concatenated onto a
      per-read frame would otherwise be graded as paired.
    - `FoldMetrics.axis_pairing_s` records the bound. `apply_criteria` fails
      grid-graded metrics (`axis_pairing=grid`) and looser bounds whenever the
      criteria declare pairing, so a grid-joined timing verdict cannot pass.
      This applies in the refit too.
    - Historical mode stays on the grid, because it reproduces persisted
      numbers.
  - **B04.1 restated** (same manifest, same artifact, HEAD evaluator;
    grid → per read at 0.5 s; per read at 0.25 s in brackets):
    - RPM/load:
      - eligible 27,892 → 26,918 (26,188); evaluated 24,712 → 23,818
        (23,208); coverage 0.886 → 0.885.
      - MAE 5.62 → 5.62° (5.60°); bias +1.24 → +1.23°; p95 18.4 → 18.4°;
        R² 0.318 → 0.316.
      - Median warning half-width 23.49 → 23.49°; skill 0.217 → 0.216.
      - Events 16 → 14 (1.87 → 1.66/h).
      - Drives 5 → 5 (4 at 0.25 s). July `2026-07-22T170353` polls PIDs
        unbatched: it had 24 grid rows at MAE 0.05°; per read it keeps 2
        rows at 0.5 s and none at 0.25 s.
      - Graded separations: p95 0.13 s rpm, p99 0.37 s, max 0.50 s.
      - Synthetic −3°/60 s 0.32 → 0.31 (delay 2.0 s); transients
        0.24/0.27 → 0.24/0.27.
      - Load strata move ≤ 0.07° (< 20 % load 15.70 → 15.69°); steady
        3.99 → 4.04°, transient 7.82 → 7.81°. Thinner strata move more:
        IAT [10,20) °C 6.66 → 6.94°, coolant [60,80) °C 0.61 → 0.25°, baro
        [70,76) kPa 6.78 → 6.97°.
    - RPM/IAT: evaluated 4,581 → 1,485, MAE 5.20 → 5.14°, events/h
      27.5 → 13.0. Its IAT is read a third as often as timing, and
      once-only pairing leaves 18,135 reads `unpaired:obd:INTAKE_TEMP`.
      Every timing read on these drives has an IAT read within 4 s.
      This is a cost of the matcher, not of the data.
    - These HEAD figures differ from B04.1's own (36,169 evaluated, MAE
      5.86°). Evaluator versions 3 and 4 refuse tuning captures; pairing
      does not explain the difference.
  - **B04.4 restated** on the same frame (candidate's 30 held-out sessions,
    `2026-09-22T154716`/`155624` read from `_archive/`, default-profile
    rows; grid → per read at 0.5 s; 0.25 s in brackets):
    - Published v2: MAE 4.63 → 4.60° (4.60°), width 14.32 → 14.32°,
      events 3 → 3.
    - Candidate v3: MAE 4.34 → 4.35° (4.35°), width 8.05 → 8.05°, events
      3 → 4.
    - B04.4's recorded 7,976-row figures included tuning captures, which
      R03.12 now refuses. Its conclusion stands: v3 is better than v2, and
      neither resolves 3°.
  - **B04.2/B04.3 stand.** B04.2's strata were measured on v3's held-out
    drives and were not recomputed. On B04.1's drives the stratum its
    conclusion rests on (< 20 % load) moves 0.01°. B04.3's synthetic and
    transient detection fractions move ≤ 0.02.
  - **Refit side effect.** The criteria now include `axis_pairing`, so the
    next refit flags each carried-over timing prior `criteria_changed`
    (`carry_over_flags`) and bumps its version. A grid-graded refit verdict
    also fails `axis_pairing=grid`. Both timing models are already
    diagnostic_only, so their status does not change. (Superseded by the
    follow-up below: the refit now grades per read.)
  - Tests: `tests/test_behavior_axis_pairing.py` (11). They include:
    - the known answer: a grid row 10° off the surface from reads 0.8 s
      apart is 0° per read;
    - a grid row concatenated onto a per-read frame is refused.

    Scratch evidence is not committed.
  - **Not changed:** fitting and serving still use the 1 Hz grid. Whether
    the timing fit should pair per read too is an open operator decision.
    So is whether a slow context axis (IAT) may reuse one read for several
    output reads.

  *Follow-up 2026-10-08 (operator decision 2026-10-08: the refit's evidence
  path grades per read; fitting stays on the grid unless a candidate's
  cells move).*
  - **Rule.** `build_evidence` grades a model whose criteria declare
    `axis_pairing` on `graded_frame`, for the test and calibration folds.
    `graded_frame` is the evaluator's own path:
    `scripts/evaluate_behavior_models.py` now calls it too.
    - The session directories reach it as `ValidationContext.reads`
      (`SessionReads`: directories, bindings and DID catalogs).
      `scripts/refit_behavior_models.py` passes every session it built
      the frame from.
    - A fold session with no directory, or no directories at all, is
      refused as `axis_pairing_no_reads`, naming the session. The refit
      turns that into a skipped recipe, kept as a candidate with no
      evidence. It never falls back to the grid.
    - A recipe without `axis_pairing` is graded on the grid as before and
      reads nothing.
    - Fitting, event-persistence fitting and the baseline still read the
      grid, and so does serving.
  - **Refit run.** B04.1's manifest mixes four calibrations, so the refit
    CLI refuses it as a whole ("mixed tune revisions"). The run used
    its 28 ZS44A-k106 sessions: every evaluation session except the three
    ZS44A-20260920 tuning captures, which are profile-refused anyway.
    - Command: `--config canary.pi.example.yaml --profile nissan-xterra-2008
      --recipe timing_advance_vs_rpm_load --recipe timing_advance_vs_rpm_iat`.
      Output went to a scratch `--out` seeded with a copy of the published
      artifact. 61 min, peak RSS 1.6 GB.
    - Split: 17 training pieces (09-17/09-18); test drives
      `2026-09-20T152758` and `2026-09-20T225927`; no calibration fold.
    - The same process re-fitted each timing recipe with `axis_pairing`
      stripped from its criteria (grid-graded) for comparison.
  - **Cells did not move.** The per-read and grid fits have identical cells
    and an identical split for both recipes. Grading happens after the
    fit, so this is by construction, and the run confirms it. No fitting
    decision is triggered.
  - **Verdicts did not move.** Both recipes fail either way. RPM/load
    publishes v4 (diagnostic-only), and RPM/IAT is skipped on held-out
    coverage (26.6 % per read, 26.5 % grid). The failure lists differ only
    in the pairing failure and one transient control:
    - The grid verdict also fails `axis_pairing=grid` under the declared
      criteria. The per-read verdict does not.
    - RPM/load, per read vs grid: evaluated 5,098 vs 5,217, MAE 2.254 vs
      2.258°, bias −1.81 vs −1.84°, R² 0.281 vs 0.266, warning half-width
      1.5 vs 1.5°, skill 0.247 vs 0.248, events 3 vs 3. Synthetic −3°/60 s
      detection 0.74 vs 0.75, transients 6°/3 s 0.68 vs 0.67, 6°/10 s 0.80
      vs 0.77. Refusals: 4 `unpaired:obd:RPM`.
    - RPM/IAT, per read vs grid: evaluated 458 vs 1,402 (3,461
      `unpaired:obd:INTAKE_TEMP`), MAE 4.64 vs 4.61°, skill −0.589 vs
      −0.592, warning half-width 3.13 vs 3.31°, synthetic 0.45 vs 0.50.
      Transient 6°/3 s 0.00 vs 0.47, so that failure drops out per read.
  - **The refit's numbers are the evaluator's.** The candidates were
    evaluated with `scripts/evaluate_behavior_models.py --mode corrected`
    on the two test sessions. `fit.calibration_sessions` was cleared on a
    copy, because the refit records the test pieces there and the
    evaluator would exclude them as leaks. On both models, all 28
    `FoldMetrics` fields match exactly: evaluated counts, MAE, R², widths,
    events, `axis_pairing_s` and refusals. So do the verdicts, failure
    lists, synthetic and transient fractions and per-drive MAE.
    Graded separations: RPM p95 0.12 s, max 0.48 s; IAT p50 3.5 s, max
    4.0 s.
  - Tests:
    - `tests/test_behavior_axis_pairing.py`:
      `test_a_timing_refit_grades_its_evidence_per_read`,
      `test_a_timing_refit_without_its_session_paths_refuses_by_name`,
      `test_a_recipe_without_axis_pairing_is_graded_on_the_grid_unchanged`.
    - `tests/test_refit_behavior_models_script.py`:
      `test_main_hands_the_session_directories_to_per_read_grading`.

**Done when:** timing has either demonstrated useful resolution or remains
explicitly exploratory, and airflow retention/removal is supported by an
independent-drive, same-observation ablation. Fewer alerts alone is not success.
