# N02.7: Fuel Comp fit on the corpus and the 20261004 drives (2026-11-08)

Desk work on the laptop corpus. The Pi was off (no power), so no new drive
went into this. **Nothing here writes or exports a ROM, and nothing here is a
proposal to flash.** The restore prediction did not pass (section 2). N02.7's
rule then says record why and do not propose (section 3).

Outputs, scripts and logs are in
`~/canary-data/analysis-scripts/2026-11-08-n02/` (not in the repo):
`samples-{before,after}/`, `fit-corpus/`, `fit-1004-pre-pump/`,
`fit-post-pump/`, `restore_eval.json`, `post_pump_cells_descriptive.csv` and
`scripts/`. Samples are `SAMPLES_VERSION` 4 (N02.8: the throttle route is
chosen per read). Profile files are at HEAD, as each fit's `fit.json`
`profile_files` records. The images came from `Desktop/Uprev`
(`Tuned ROMs/`, `ROM Store/`) and were checked by hash.

## 1. Inputs and the three phases

Calibration ZS44A-20261004 has been running since 2026-10-04T23:02Z. Its K,
curves and rpm axis are identical to 20260920's (checked from the images:
K 28,715, `curves` equal), so a logged rpm × byte is the same operating point
on both.

- **Pre-flash (ZS44A-20260920, stock pump):** eligible drives 09-21 (three),
  09-22 (two), 09-23 (the tuning captures 142926..164007), 10-04 (190056..194342,
  202737..ram-study).
- **Post-flash, pre-pump (20261004, stock pump):** 2026-10-08T172943,
  2026-10-08T182550; 2026-10-10T022015, 024445, 134622..153117 (134622, 141532,
  145212, 153117) and 165941..171856 (165941, 170455,
  `_capture-tuning-2026-10-10T171518`, 171856). 2026-10-10T191803 and the 10-08
  fuel-pulsation capture never captured trims or a commanded target.
- **Post-pump (20261004, new pump):** these group into three drives:
  - 2026-10-26T003159..`_capture-tuning-2026-10-26T140411` (003705, 004831,
    010217, 011432, `_capture-tuning-2026-10-26T010515`, `…T140411`);
  - 2026-10-27T153418..161320 (with `_capture-tuning-2026-10-27T154559` and
    `…T160919`);
  - 2026-10-27T171152..175815.

  Plain sessions 10-26T003159 and 011352 have no candidates.

**The pump, measured from the same-instant pairs.** For each MAF read, rail
pressure is the mean of `sensor:FUEL_PRESSURE` reads within ±0.25 s (at least
30 reads). The table compares 10-27 against 10-04/08/10 (`scripts/pump_pressure.py`).

| MAF g/s | pre-pump psi (n) | post-pump psi (n) | Δ psi | flow √ |
|---|---|---|---|---|
| 2–5 | 50.32 (6,300) | 55.12 (1,484) | +4.80 | +4.7 % |
| 20–40 | 50.13 (2,612) | 53.84 (520) | +3.71 | +3.6 % |
| 40–60 | 49.94 (2,589) | 52.83 (265) | +2.89 | +2.9 % |
| 80–100 | 49.48 (176) | 51.27 (18) | +1.79 | +1.8 % |
| 100–120 | 48.98 (44) | 50.31 (4) | +1.33 | +1.3 % |
| > 120 | 48.6 (11) | none | – | – |

- **The pump's gain is not uniform.** It shrinks with flow, from +4.7 % at
  idle to +1.3 % near 110 g/s, so it is MAF-shaped.
- 10-26 logged no pressure.
- WOT above 120 g/s has no post-pump pressure read.
- The two 10-27 drives' low-flow references differ: 53.73 and 55.67 psi.

LTFT reads 0.00 on every eligible sample of both revisions, so the
closed-loop error is STFT alone.

## 2. The restore prediction (`2026-10-04-fuel-comp-restore-prediction.md`)

**Method** (`scripts/restore_eval.py`; criteria written in its docstring
before the numbers were read):
- Bins are matched rpm × load byte: 400 rpm × 8 counts, on the reconstructed
  byte.
- Each bin is the median of post minus the median of pre, weighted by
  min(n), with a 1,000-resample drive bootstrap 95 % CI.
- The predicted Δerror is −(FC_20261004/FC_20260920 − 1) at each sample.
- The floor is the pre-vs-pre placebo |Δ|: the 20260920 drives split in time.
  For WOT it is at least `noise_floor.yaml`'s 1.25 %.
- Rules:
  - **PASS:** the CI holds the prediction and excludes 0.
  - **FAIL:** the CI misses the prediction by more than the floor.
  - Otherwise **UNRESOLVED**.
- The n rule is the frozen `plant_interventions.yaml` minimum: ≥ 20 per side
  and ≥ 2 drives. **It is relaxed for the WOT bands to ≥ 5 per side and
  ≥ 1 drive**, because 20261004 has only 15 eligible open-loop samples in all
  (operator-authorised).
- The formal E07.4 route, the logged byte `mem:ECM_FUEL_COMP_X_INPUT`, still
  has 0 post-flash pairs. This evaluation uses the N02 reconstructed byte
  instead.

| Band (as written) | Comparison | n pre / post (drives) | Δ measured [95 % CI] | Δ predicted | Verdict |
|---|---|---|---|---|---|
| < 64, closed loop (STFT ≈ +1 %) | pre-pump | 113 / 36 (6 / 4) | +5.22 [−2.85, +11.12] | +1.31 | UNRESOLVED |
| | pre-pump, purge commanded 0 | 107 / 33 (5 / 4) | +5.72 [−4.46, +10.33] | +1.47 | UNRESOLVED |
| | post-pump, purge 0, raw | 115 / 304 (7 / 3) | −6.75 [−7.58, +0.31] | +1.01 | UNRESOLVED |
| | post-pump, purge 0, pump-corrected | 115 / 304 (7 / 3) | −2.36 [−3.19, +4.64] | +1.01 | UNRESOLVED |
| 64–144, closed loop (≈ +0.2..+0.5) | pre-pump | 16 / 7 (6 / 2) | −0.31 [−5.03, +4.30] | +0.55 | UNRESOLVED (n) |
| | post-pump, raw | 31 / 69 (7 / 2) | +5.02 [−1.95, +6.14] | +0.06 | UNRESOLVED |
| | post-pump, pump-corrected | 31 / 69 (7 / 2) | +8.35 [+1.50, +9.52] | +0.06 | FAIL |
| ≥ 144, 4,000–5,600 rpm, open loop (λ −3..−5.5 %) | pre-pump | 0 / 0 | – | – | UNRESOLVED (no samples) |
| | post-pump, raw | 10 / 3 (3 / 1) | −7.93 [−12.29, −4.02] | −2.94 | UNRESOLVED (n < 5) |
| | post-pump, pump-corrected | 10 / 3 (3 / 1) | −6.68 [−11.09, −2.74] | −2.94 | UNRESOLVED (n < 5) |
| ≥ 144, any rpm (relaxed band, not as written) | post-pump, raw | 26 / 14 (4 / 2) | −4.95 [−8.35, −3.19] | −1.89 | FAIL (richer than predicted) |
| | post-pump, pump-corrected | 26 / 14 (4 / 2) | −3.22 [−6.23, −1.05] | −1.89 | PASS |

**Verdict: the restore prediction is not passed. It is unresolved, not
refuted.**

- **The deciding band** (≥ 144 at 4,000–5,600 rpm) has 3 post-flash samples,
  all from the one 10-27 pull: 4,200 / 4,288 / 4,850 rpm, −4.4 / −4.3 /
  −10.9 %. None is pre-pump, because no 10-08/10-10 open-loop row was
  eligible: target transients and unpaired λ filtered them, and the 10-10
  tuning capture's 414 open-loop candidates were all closed-loop regime.
- **WOT direction.** Over the whole ≥ 144 region WOT moved richer, as
  predicted. The measured shift is 2.6× the prediction raw and 1.7×
  pump-corrected, which passes only with the rpm restriction dropped and
  thresholds relaxed. The pump correction above 120 g/s is extrapolated
  (+1.3 %).
- **Closed loop cannot resolve a 1 % effect.** Same revision, same pump, the
  closed-loop drive medians (mostly warm idle) span −7.4 to +4.3 % (20260920) and −1.6 to +7.8 %
  (20261004 pre-pump: 10-10 drives +7.8 and +6.6, 10-08 −0.8). The placebo
  CI is [−8.4, +7.0]. The prediction's ±1 % sits far inside that.
- **The 64–144 FAIL is on 2 drives, with purge never commanded off** (n = 0
  purge-off in that band on any phase). The comparison cannot separate purge
  vapour from Fuel Comp there, so it is not evidence about Fuel Comp.
- The caveat that the prediction's authors flagged held: LTFT reset, so STFT
  and λ were compared.

## 3. Proposal: none

N02.7's rule is that the proposal can inform an edit only if the restore
prediction passes. It did not, so **no per-cell factor is proposed**. The fit
would refuse anyway.

**Post-pump fit** (`fit-post-pump`, declared configuration, 16 sessions
listed, 3 drives):
- 1,831 eligible: 1,648 are `cell_ambiguous` (idle air at warm idle) and 5
  are `droop_none`, leaving **178 usable, 169 of them from one drive** (10-27
  153418..161320).
- Cells: **0 proposed, 22 insufficient, 234 not covered.** No cell has ≥ 2
  drives with ≥ 1 weighted pair.
- Held-out (3 folds, 178 samples):

  | Model | RMS | Mean signed |
  |---|---|---|
  | current | 2.898 % | −1.94 % |
  | global factor | 4.480 % | +3.55 % (2 drives better, 1 worse) |

  MAF bins and cells are equal to current (nothing passes). Verdict "no
  improvement (no cell passed the refusals in any fold)".

**Descriptive only, not a proposal:** median required/current factor on the
20261004 axes, nearest cell. Each row is one drive.

| cell (rpm, byte) | n | drives | factor | after a global −1.8 % |
|---|---|---|---|---|
| 2,400, 144 | 1 | 1 | 0.916 | 0.932 |
| 2,000, 128 | 1 | 1 | 0.925 | 0.942 |
| 2,400, 224 | 2 | 1 | 0.927 | 0.943 |
| 2,000, 224 | 2 | 1 | 0.940 | 0.957 |
| 2,400, 208 | 3 | 1 | 0.943 | 0.960 |
| 2,800, 112 | 8 | 1 | 0.950 | 0.967 |
| 2,800, 144 | 107 | 1 | 0.982 | 1.000 |
| 2,800, 128 | 14 | 1 | 0.985 | 1.002 |
| 2,800, 160 | 39 | 1 | 0.988 | 1.006 |

**Knock-on:** nothing was proposed, so N02.6 produced none.

**Corpus fit** (`fit-corpus`, 112 sessions ≥ 2026-08-03, all revisions and
both pumps pooled):
- 564 samples, 13 drives (DID excluded): 10 proposed / 50 / 196.
- Held-out RMS: current 2.848 %, global 2.555 %, bins 2.798 %, cells 3.275 %.
- Bootstrap: global < current 0.808, cells < current 0.348. **No
  improvement.**
- DID included: 634 samples, 15 drives, 14 proposed, no improvement.

It pools a pump change and a day-to-day spread larger than any cell effect,
so it is not usable for today's truck either.

**Pre-pump 20261004 fit:** 17 usable samples, 3 drives, 0 proposed.

## 4. Uniform versus cell-shaped error, post-pump

Closed-loop median error by drive (all eligible, MAF band):

| Drive | 0–5 g/s | 40–60 g/s |
|---|---|---|
| 10-26 | −3.9 % (n 982) | −2.5 % (n 2) |
| 10-27a | +1.6 % (602) | +0.8 % (157) |
| 10-27b | −0.8 % (41) | – |

Open loop, error relative to the RAM target:
- 10-26: median −2.0 % (2,100–3,400 rpm, n 9).
- 10-27a: median −2.7 % (n 6); −10.9 % at 4,850 rpm.

**Within one drive the error is nearly uniform across load. Between drives
the uniform offset changes sign** (−3.9 % to +1.6 %). That between-drive
offset is the largest component of the post-pump error.

- **A single global factor is not supported.** It made the held-out drive
  worse (4.48 % vs 2.90 % RMS).
- **If a global change is taken anyway** (for example in 0x9254, currently
  10601 per the 10-27 log), the sample-median of the required factor is
  0.982 (−1.8 %). That figure is dominated by 10-27a. The drive-equal mean is
  0.953 (−4.7 %), and on the raw error instead of the droop remainder it is
  1.008.
  - After −1.8 %, the residual cell factors at the only well-covered cells
    (2,800 rpm, bytes 128–160, n 14–107) are 1.000–1.006, so nothing
    cell-shaped is left there.
  - The −4 to −8 % cells are n 1–8 on one drive.
  - Do not count the global change twice: those residuals already include it.
- **The WOT residual after each drive's closed-loop offset is removed:**
  - 10-26: about +1.9 % (2,100–3,400 rpm);
  - 10-27a: about −3.5 to −4.3 %, and −12 % at 4,850 rpm.

  These are one pull per drive.
- **The pump's own +4.7 % → +1.3 % slope is MAF-shaped.** N02.4's droop
  remainder takes it out only relative to each drive's own idle pressure.

## 5. What would resolve it

- WOT pulls at 4,000–5,600 rpm on two drives, with `sensor:FUEL_PRESSURE`
  logging through the pull. The 10-26 drives and above 120 g/s have none.
  This gives ≥ 5 samples per side.
- Part-throttle closed loop with purge commanded 0 (PID 0x2E, or the
  `tuning` profile with purge logged), on at least two drives.
- The idle offset (+7.8 % on 10-10, −3.9 % on 10-26) needs a cause before any
  global change. Fuel blend and purge loading are unmeasured. Pump pressure
  on 10-10 was normal (references 50.1–50.5 psi).
