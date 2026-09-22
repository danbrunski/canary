# Behavior-model validation report (B02) — 2026-09-21

> **Re-run under evaluator v2 — see [2026-09-22-behavior-validation-report-v2.md](2026-09-22-behavior-validation-report-v2.md).**
> This report was produced with `EVALUATOR_VERSION 1`, whose row-time
> conversion read `datetime64[us]` frames 1000x short (fixed in `4873388`).
> Every per-row number below (counts, coverage, MAE, bias, quantiles, R²,
> band half-widths, per-row exceedance rates, skill, bootstrap) is unaffected
> and reproduces under v2. Every quantity built from time between rows is
> wrong here: the persisted event counts and control event rates in §3, and
> the synthetic-window and "every window alerts" statements. Read those from
> the v2 report; the numbers below are preserved as written (B02.7).

The reproducible successor to the numbers in
[2026-09-21-behavior-model-review.md](2026-09-21-behavior-model-review.md).
Every figure below comes from `scripts/evaluate_behavior_models.py`, with its
inputs pinned by content hash, and the headline numbers are asserted by
`tests/test_behavior_validation_known_answers.py` against committed slices of
the same sessions. Nothing here was deployed, refitted or published.

## How to reproduce

```
.venv/bin/python scripts/evaluate_behavior_models.py \
    --config canary.pi.example.yaml --profile nissan-xterra-2008 \
    --artifact data/nissan-xterra-2008/behavior_models.yaml \
    --mode historical \
    --manifest tests/fixtures/behavior_validation/evidence_historical.yaml --out historical.json

.venv/bin/python scripts/evaluate_behavior_models.py \
    --config canary.pi.example.yaml --profile nissan-xterra-2008 \
    --artifact data/nissan-xterra-2008/behavior_models.yaml \
    --mode corrected \
    --manifest tests/fixtures/behavior_validation/evidence_corrected.yaml --out corrected.json
```

Inputs recorded in both reports: config `canary.pi.example.yaml@sha256:e95cd6f2…`,
artifact `behavior_models.yaml@sha256:9cd174ce…` (schema 1), criteria
`behavior_validation.yaml@sha256:eadf6f92…`, bindings `sha256:c798f8f6…`, dids `sha256:50943e8e…`, event policy
3-of-5 ticks within 8 s then 30 s dedupe, git `94d027d` plus the uncommitted B02
change set. Session content hashes are in the two manifests; a changed or
missing session refuses to evaluate. Runtime on this laptop: ~20 s historical,
~2.5 min corrected (21 sessions, peak ~2 GB; a whole-bus CAN decode of one
42 MB drive peaked above 6 GB, which is why the evaluator reads raw CAN only for
the ids its decoded channels need).

**Taxonomy.** `canary.pi.example.yaml` matches the approved set in
`behavior_validation.yaml` (koeo, warmup, idle_transient, idle, idle_high,
city, highway; `suburban` retired into `city`). `canary.example.yaml` does not
(it adds the IMU modes launch/hard_brake/cornering), and the local
`canary.train.yaml` still carries `suburban` — `--mode corrected` refuses both.
None of the eight shipped models is gated on a mode the fold changed, so the
historical reproduction is taxonomy-insensitive; the idle mode definition is
identical in both.

**Historical input scope.** The July fits predate the segmenter's decoded
fallback. With `--decode-can none` the idle holdout reproduces at 90 rows;
decoding the broadcast mode inputs gives 114. Historical mode therefore uses
`none`, corrected mode `mode-inputs`.

## 1. Known answer first — persisted holdouts reproduce exactly

Legacy artifacts carry no serving contract, so rows were selected with each
model's catalog recipe contract (the rows the fit actually used).

| Model | held-out | evaluated / eligible | R² | MAE | warning half-width (median) | status |
|---|---|---|---|---|---|---|
| air_charge_vs_rpm_throttle_did | `_capture-tuning-did-2026-08-07T005351` | 1180 / 1443 | 0.872 | 1978 counts | 8901 counts | reproduced |
| warm_idle_rpm_baseline | J26 | 90 / 90 | −0.121 | 13.3 rpm | 75 rpm | reproduced |
| timing_advance_vs_rpm_load | J26 | 57 / 58 | 0.356 | 6.18° | 21.4° | reproduced |
| timing_advance_vs_rpm_iat | J26 | 196 / 215 | −0.046 | 8.80° | 31.3° | reproduced |
| fuel_trim_vs_rpm_load | J26 | 47 / 54 | 0.178 | 3.30 % | 19.1 % | reproduced |
| air_mass_per_cycle_vs_rpm_load | J26 | 79 / 93 | 0.552 | 0.251 g/cycle | 0.687 g/cycle | reproduced |
| ve_vs_rpm_load | J26 | 76 / 90 | 0.551 | 6.99 % | 20.7 % | reproduced |
| cat_warmup | none | — | — | — | — | **no held-out evidence** |

"Reproduced" = evaluated count exact; coverage, R², warning and critical row
rates within 1e-6 of the values persisted in the artifact.

## 2. The July holdout was not independent evidence

B02.3's drive grouping exposes what the string-sorted holdout hid:

- **J26 is the same outing as a training session.** `2026-07-26T223147`
  (training, for six models) ends 22:50:24.9 UTC; J26 (`225058`, the held-out
  session) starts 22:51:00.2 — a 35 s writer rotation. Under the 600 s join gap
  they are one physical drive, so every July "held-out" number above was
  measured on a continuation of a training drive.
- **Mixed calibrations.** Those six models trained on ZS04C-stock sessions
  (07-18 … 07-26 22:31) *and* the post-flash `223147` (ZS44A-osiris-initial),
  and were validated on post-flash J26. `tune_revision` is unset on all seven
  July fits.
- **A session crosses the flash.** `2026-07-26T214348` runs 21:43:49 → 22:31:47
  UTC; the registry's first Osiris flash is 22:31:00. The refit CLI now refuses
  such a session until `behavior_validation.yaml` names a resolution (probably
  "stock — the 22:31:47 rotation is the post-flash restart", but that is the
  operator's call, not recorded here).

## 3. September, independent drives, corrected configuration

Drives sharing any directory with a model's training or held-out sessions are
excluded per model. Baselines are fitted on the supplied training sessions'
eligible rows (training-mode median) — never on evaluation drives. Row
exceedance and events on these unlabelled drives are **not** false positives.

| Model | drives | rows → present → eligible → evaluated | MAE | bias | p95 abs error | R² | warn half-width | skill vs baseline | events (h evaluated) | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| air_charge_vs_rpm_throttle_did | 3 | 34995 → 2436 → 2436 → 1878 | 2146 | −112 | 6202 | 0.817 | 8901 | 0.69 | 1 (0.52 h) | failed |
| warm_idle_rpm_baseline | 2 | 14019 → 5208 → 172 → 172 | 32.9 rpm | +15.0 | 37.5 | −0.010 | 75 | 0.00 | 1 (0.05 h) | insufficient |
| timing_advance_vs_rpm_load | 5 | 6742 → 2719 → 2719 → 2380 | 5.31° | +0.52 | 18.3° | 0.375 | 23.8° | 0.29 | 2 (0.66 h) | failed |
| timing_advance_vs_rpm_iat | 0 | 6044 → 1671 → 1671 → **0** | — | — | — | — | — | — | 0 | insufficient |
| fuel_trim_vs_rpm_load | 2 | 6742 → 189 → 188 → 169 | 6.03 % | −4.51 | 11.9 % | −0.490 | 14.0 % | −0.08 | 1 (0.05 h) | failed |
| air_mass_per_cycle_vs_rpm_load | 3 | 6044 → 2591 → 2591 → 2102 | 0.200 g | −0.011 | 0.651 g | 0.780 | 1.68 g | 0.60 | 2 (0.58 h) | failed |
| ve_vs_rpm_load | 1 | 6044 → 167 → 167 → 114 | 4.71 % | −2.72 | 13.8 % | 0.661 | 22.1 % | 0.31 | 0 (0.03 h) | failed |
| cat_warmup | 3 | 37499 → 1725 → 91 → 91 | 52.8 °C | +47.6 | 102 °C | −2.11 | 30 °C | n/a | 3 (0.03 h) | failed |

Drive-level uncertainty (drive-resampled bootstrap, 90 %, ≥ 3 drives): air
charge MAE 2007–2293 counts; timing/load 2.37–6.48°; air mass 0.195–0.202
g/cycle; cat_warmup 50.0–54.9 °C.

Per-drive numbers match the review exactly where it measured them: timing/load
S17 1084 rows MAE 4.80° R² 0.328, S21 1009 rows 6.71° R² 0.279, S18 R² 0.714;
air mass S17 R² 0.736 (1040 rows), S21 0.756 (949); air charge S21 1011 rows
MAE 2029 R² 0.781; fuel trim S18 142 rows MAE 6.51 R² −3.95; VE S18 114 rows
MAE 4.71 R² 0.661; warm idle S18 154 rows MAE 13.6 rpm, zero exceedances;
catalyst S18 34 rows MAE 55.0 °C R² −0.737, 25 warnings + 1 critical. Air
charge on S17 differs slightly (841 rows vs the review's 826) — probably
because the review applied RPM ≥ 400 to every model while the air-charge
recipe contract is engine-state `any`; not verified further.

Named failures (from the corrected report; criteria in
`data/nissan-xterra-2008/behavior_validation.yaml`, declared before this run):

- **air charge** — best skill (0.69) and envelope coverage (0.91), but its
  3σ cells are ±8901 counts against a declared meaningful deviation of 1500:
  it cannot resolve a 12 % airflow change; synthetic −1500-count step detected
  in 1 of 2 clean windows.
- **timing/load** — skill 0.29 (< 0.3), MAE 5.3° and ±23.8° bands against a
  3° target; a synthetic −3° retard is seen in 1 of 3 clean windows.
- **timing/IAT** — zero evaluable rows: September IAT 13–22 °C vs a fitted
  domain of 29–68 °C (off-map, refused, never graded).
- **fuel trim (STFT)** — worse than its baseline (skill −0.08), −4.5 % bias
  on S18, 0 of 1 synthetic +5 % steps detected.
- **air mass / VE** — useful skill (0.60 / 0.31) but bands 20× / 7× wider than
  the declared deviation.
- **warm idle** — accurate (MAE 13.6 rpm, p95 25 rpm on S18; negative R² is
  irrelevant for a constant baseline) and detects ±150 rpm steps, but has one
  qualifying drive and 0.05 h evaluated. Insufficient, not failed.
- **catalyst** — MAE 52.8 °C > 50 °C target, positive bias 47.6 °C; its
  training sessions (May) are not in the local corpus, so no baseline; every
  window alerts without injection, so synthetic sensitivity is unmeasurable.

## 4. Missing evidence (B02.4 remainder)

- **Independent drives on the current calibration.** No model has two
  qualifying independent drives *and* the declared observation counts under one
  tune revision: S17 and S18 are ZS44A-k106, S21 is ZS44A-20260920 (after the
  2026-09-20 flash), and every shipped model was fitted on earlier revisions
  (the July six on stock + osiris-initial, air charge on ZS44A-k106).
- **Event-gated warmup recipes are blocked by D03.22.** Classifier sidecars
  written before the mode-label fix `f8b7d38` (2026-09-20 04:00 UTC) are
  excluded from event gating by the refit (`mode_labels_stale` provenance), so
  `coolant_warmup`, `cold_start_idle_rpm_vs_coolant` and `cat_warmup` cannot
  refit until D03.22 relabels the corpus.
- **A named resolution for `2026-07-26T214348`** before any refit touches July.
- **cat_warmup's May training sessions** (for a baseline) — or a refit.
- **Labelled faults.** Synthetic detection is reported as synthetic
  sensitivity only; there is no real fault recall to claim.
- **Recipes without declared criteria** (cam, TPS, bank balance, AFR,
  cold-start idle) stay diagnostic-only until B04–B07 declare them.

## 5. A correction made along the way

The engine-start tracker (B01.5) refused S18's genuine cold start: RPM and the
run-time clock are separate polls, and five running rows arrived before the
first `RUN_TIME` (20 s). The first run-time reading of a run now places the
start when it is within the evidence window; a mid-drive join (first reading
600 s) still cannot. With the fix the catalyst numbers above land exactly on
the review's (n=34, R² −0.737, MAE 55 °C).
