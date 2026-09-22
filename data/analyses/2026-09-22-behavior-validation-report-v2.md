# Behavior-model validation report (B02) — 2026-09-22 re-run, evaluator v2

A re-run of [2026-09-21-behavior-validation-report.md](2026-09-21-behavior-validation-report.md)
under `EVALUATOR_VERSION 2`. The 09-21 figures were produced by evaluator
version 1, whose row-time-to-seconds conversion read the session frames'
`datetime64[us]` timestamps as `int64 / 1e9` — 1000x short. Every quantity
built from *time between rows* was therefore wrong in that report: the
persistence window (3-of-5 ticks within 8 s), the 30 s event dedupe, the
synthetic-fault window length and injection duration, and so the persisted
event counts, control event rates and synthetic sensitivity. Per-row
quantities (counts, coverage, MAE, bias, quantiles, R², band half-widths,
per-row exceedance rates, skill, envelope coverage, drive bootstrap) never
touched the time axis and were not affected. The 09-21 report's numbers are
preserved as written; this report states what changed and what did not.
Fixed in `4873388` (`pairing.to_seconds` is now the one converter; a guard
test refuses the pattern elsewhere). Nothing here was deployed, refitted or
published.

## How to reproduce

The same two commands as the 09-21 report, unchanged:

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

Inputs recorded in both reports (`evaluator_version: 2`): config
`canary.pi.example.yaml@sha256:e95cd6f2…` (same), artifact
`behavior_models.yaml@sha256:9cd174ce…` (same, schema 1), bindings
`sha256:c798f8f6…` (same), dids `sha256:50943e8e…` (same), tune registry
`tune_revisions.yaml@sha256:63154293…`, event policy 3-of-5 ticks within 8 s
then 30 s dedupe (same policy, now measured in real seconds), git `4824ce3`
plus an uncommitted B03.4 change set that does not touch the evaluator
(`scripts/train_*.py`, `src/analysis/behavior_inventory.py`,
`src/models/spec_producibility.py`, `src/preprocessing/feature_pipeline.py`).
Session content hashes are unchanged from the two manifests (a changed session
refuses to evaluate). Corpus under `~/canary-data`, same 2 historical and 21
corrected sessions.

**One input differs from 09-21:** criteria `behavior_validation.yaml` is now
`sha256:96e4c345…` (was `eadf6f92…`). The diff since the 09-21 run:
`provenance_resolutions` excludes `2026-07-26T214348` (operator decision
2026-09-22, `fec733c`); `report_regimes` strata and a B04.3 `resolution`
block (declared timing resolution plus two −6° *transient controls* of 3 s
and 10 s, `max_transient_detection: 0.0`) on the two timing recipes; the
total-trim recipes' unit relabelled (`4873388`). No `min_skill_vs_baseline`,
`meaningful_deviation`, `max_mae`, event-rate or synthetic-fault criterion of
the eight shipped models changed. The transient controls are the only new
judgement, and they appear only under `timing_advance_vs_rpm_load` below.

Runtime on this laptop: ~15 s historical, ~2.5 min corrected.

**Denominator (same day, after the first v2 pass).** `evaluated_hours` was
`evaluated rows × 1 s`, i.e. it assumed the detector watched one second per
evaluated row. B02.5 asks for *actual* event rates, and a rate needs the
time the detector was watching: a slowly polled output is evaluated in 1 s
bursts far apart, so the row count understated exposure and inflated
events/h (catalyst: 0.03 h from 91 rows spread over 0.34 h). It is now the
wall-clock span of each drive's evaluated rows plus one tick, summed over
drives (`behavior_validation._evaluated_hours`; contiguous 1 Hz rows give
the old value exactly; `tests/test_behavior_validation.py::test_evaluated_hours_is_wall_clock_exposure_not_a_row_count`).
Every figure below is from the re-run with that denominator; event counts,
per-row metrics and verdicts are identical between the two v2 passes, and
where an events/h figure moved the first-pass value is shown struck through.

## 1. Known answer first — persisted holdouts still reproduce exactly

Unchanged from 09-21, as expected: the reproduction compares evaluated count,
coverage, R² and per-row warning/critical rates, none of which use the time
axis. All seven legacy holdouts are `reproduced` (mismatched: none) with the
same evaluated/eligible counts, R², MAE and median warning half-widths as the
09-21 §1 table; `cat_warmup` still has no held-out evidence.

What v1 did not report here and v2 now measures on the July holdouts — events
after persistence and dedupe, in real seconds: `air_charge_vs_rpm_throttle_did`
1 event in 0.40 h watched (2.5/h, ~~3.1/h~~) on `_capture-tuning-did-2026-08-07T005351`;
`timing_advance_vs_rpm_iat` 1 event in 0.46 h (2.2/h, ~~18/h~~) on J26; the
other five 0 events over 0.06–0.46 h each.

## 2. The July holdout was not independent evidence

Unchanged; see the 09-21 report §2. The one open provenance question there
(`2026-07-26T214348` crossing the flash) has since been resolved by exclusion
in `behavior_validation.yaml`.

## 3. September, independent drives, corrected configuration

Same drive grouping, exclusions and training-fitted baselines as 09-21. Row
exceedance and events on these unlabelled drives are **not** false positives.

**Per-row columns are identical to the 09-21 table** — checked value by value:
drives, rows → present → eligible → evaluated, MAE, bias, p95 |error|, R²,
warning half-width, skill vs baseline, envelope coverage, per-drive MAE range
and the drive-resampled bootstrap intervals (air charge 2007–2293 counts;
timing/load 2.37–6.48°; air mass 0.195–0.202 g/cycle; catalyst 50.0–54.9 °C).
The per-drive figures quoted in 09-21 §3 (timing/load S17 1084 rows MAE 4.80°
R² 0.328, S21 1009 rows 6.71° R² 0.279, S18 R² 0.714; air mass S17 R² 0.736,
S21 0.756; air charge S21 1011 rows MAE 2029 R² 0.781; fuel trim S18 142 rows
MAE 6.51 R² −3.95; VE S18 114 rows MAE 4.71 R² 0.661; warm idle S18 154 rows
MAE 13.6 rpm; catalyst S18 34 rows MAE 55.0 °C R² −0.737, 25 warnings + 1
critical) all reproduce under v2.

**What changed — events and control rates** (v1 → v2; hours evaluated
unchanged):

| Model | events v1 | events v2 | h watched (v2, wall-clock; ~~rows×1 s~~) | v2 events/h | per-drive events (v2) | verdict v1 → v2 |
|---|---|---|---|---|---|---|
| air_charge_vs_rpm_throttle_did | 1 | 1 | 0.92 (~~0.52~~) | 1.1 (~~1.9~~) | S21 1 | failed → failed |
| warm_idle_rpm_baseline | 1 | 1 | 0.05 | (1 event is not a rate) | `2026-07-19T202721` 1 (18 rows) | insufficient → insufficient |
| timing_advance_vs_rpm_load | 2 | **3** | 1.02 (~~0.66~~) | **2.9** (~~4.5~~) | S18 1, S21 2 | failed → failed (+ control rate) |
| timing_advance_vs_rpm_iat | 0 | 0 | 0 | — | 0 evaluable rows | insufficient → insufficient |
| fuel_trim_vs_rpm_load | 1 | **0** | 0.34 (~~0.05~~) | (0 events is not a rate) | — | failed → failed |
| air_mass_per_cycle_vs_rpm_load | 2 | **10** | 0.95 (~~0.58~~) | **10.6** (~~17.1~~) | S18 4, S21 6 | failed → failed (+ control rate) |
| ve_vs_rpm_load | 0 | 0 | 0.26 (~~0.03~~) | (0 events is not a rate) | — | failed → failed |
| cat_warmup | 3 | **0** | 0.34 (~~0.03~~) | (0 events is not a rate) | — | failed → failed |

Two models now additionally fail the declared control event-rate criterion
(`max_control_events_per_hour: 2.0`): `timing_advance_vs_rpm_load` at 2.9/h
and `air_mass_per_cycle_vs_rpm_load` at 10.6/h (per drive: timing/load S18
3.7/h, S21 6.0/h; air mass S18 15.1/h, S21 18.1/h). Under v1 the 8 s persistence
window was 8 ms and the 30 s dedupe 30 ms, so events could only latch on rows
whose compressed timestamps still fell within a window — most sustained
exceedances were never counted (air mass 2 → 10) while some were counted that
real time dedupes away (fuel trim 1 → 0, catalyst 3 → 0).

**The catalyst zero is a cadence fact, not a clean result.** `cat_warmup` on
S18 fires on 26 of 34 evaluated rows (per-row exceedance unchanged), yet emits
0 events because the evaluated rows arrive as 1 s pairs ~17 s apart, so a
3-of-5-ticks-within-8 s gate can never accumulate three verdicts. It is the
poll tier, not the eligibility window: PID 0x3C is on the `slow` tier
(`obd_scheduler`, 1/15 Hz; 17–18 s between value changes on S18), the
resampler keeps each read fresh for two 1 s rows, and 235 of the 401 rows in
S18's warm-up window are refused `actual_stale:obd:CATALYST_TEMP_B1S1` while
mode and cold-start eligibility hold for 312 of them. The daemon's
`PersistenceGate` would behave identically on this input: at this cadence
the model cannot produce a persisted event at all, alerting or not. The v1
"every window alerts without injection" statement for this model was a
compressed-window artifact (see synthetic windows below).

**What changed — synthetic sensitivity.** Under v1 the synthetic windows were
the recipe's `min_duration_seconds` in *compressed* time, so each drive
yielded one to three windows and a "60 s" injection lasted 60 ms. Under v2
(`windows` / `clean_windows` = windows with no control event; `detected` of
clean; detection fraction judged against the declared 0.8):

| Model | fault | v1 (from 09-21 text) | v2 windows → clean | v2 detected | fraction | median / max delay |
|---|---|---|---|---|---|---|
| air_charge_vs_rpm_throttle_did | −1500 counts, 60 s | 1 of 2 | 57 → 56 | 10 | 0.18 | 19 s / 58 s |
| warm_idle_rpm_baseline | ±150 rpm, 60 s | detects | 5 → 4 (each sign) | 4 | 1.00 | 2 s / 2 s |
| timing_advance_vs_rpm_load | −3°, 60 s | 1 of 3 | 65 → 62 | 19 | 0.31 | 6 s / 54 s |
| fuel_trim_vs_rpm_load | +5 %, 120 s | 0 of 1 | 11 → 11 | 0 | 0.00 | — |
| air_mass_per_cycle_vs_rpm_load | −0.08 g/cycle, 60 s | (not stated) | 58 → 48 | 4 | 0.08 | 22.5 s / 29 s |
| ve_vs_rpm_load | −3 %, 60 s | (not stated) | 16 → 16 | 4 | 0.25 | 7.5 s / 36 s |
| cat_warmup | −60 °C, 120 s | unmeasurable (every window alerted) | 14 → 14 | 0 | 0.00 | — |

The direction of every conclusion is the same — no surface resolves its
declared deviation reliably except the constant warm-idle baseline — but the
v2 fractions are now measured over tens of real windows rather than one to
three, and carry detection delays. Warm idle detects ±150 rpm steps in 4 of 4
clean windows within 2 s (its one control event is on the thin July drive).
The catalyst curve, far from alerting everywhere, cannot latch at all (above).

**New under the changed criteria — transient controls** (timing/load only):
a −6° retard held for 3 s alerts in 9.7 % of clean windows and for 10 s in
19.4 %, against a declared maximum of 0 %. The 3-of-5-in-8 s gate cannot
express "sustained for 60 s"; this is the B04.3 limitation already recorded
in `4873388`, now in the evidence.

**Named failures under v2** (`would_be_verdict` per model; criteria as
declared):

- **air charge** — `mae 2146 > 1500`, `warning half-width 8901 > 1500`,
  synthetic −1500-count detection 0.18 < 0.8. Skill 0.69 and envelope
  coverage 0.91 unchanged.
- **timing/load** — skill 0.29 < 0.3, MAE 5.3° and ±23.8° bands against 3°,
  **control events 2.9/h > 2.0**, synthetic −3° detection 0.31 < 0.8,
  transient −6° 3 s / 10 s detection 0.10 / 0.19 > 0.
- **timing/IAT** — no evaluated test rows (off-map September IAT; unchanged).
- **fuel trim (STFT)** — skill −0.08, MAE 6.0 % > 5, bands 14 % > 5,
  synthetic +5 % detection 0 of 11; insufficient on drives (1 < 2),
  eligible (188 < 300) and control hours (0.34 < 0.5).
- **air mass** — MAE 0.20 > 0.08, bands 1.68 > 0.08, **control events
  10.6/h > 2.0**, synthetic −0.08 g/cycle detection 0.08 < 0.8.
- **VE** — MAE 4.7 > 3, bands 22 > 3, synthetic detection 0.25 < 0.8;
  insufficient on drives, eligible (167 < 300), envelope (0.29 < 0.3) and
  control hours (0.26 < 0.5).
- **warm idle** — accurate and sensitive; insufficient: one qualifying drive
  and 0.05 h evaluated (1 event on the 18-row July drive is not a rate).
- **catalyst** — MAE 52.8 > 50, synthetic −60 °C detection 0 of 14 (cannot
  latch); insufficient: eligible 91 < 120, no training baseline, 0.34 h.

## 4. Missing evidence and 5. the engine-start correction

Unchanged from the 09-21 report §4–§5, except that the `2026-07-26T214348`
resolution is no longer missing (excluded, `behavior_validation.yaml`).

## What this re-run does and does not change

- The 09-21 report's per-row conclusions stand; its event counts, control
  rates and synthetic-window statements should be read from this report.
- No verdict flipped. Two models gained a control-rate failure they already
  deserved on other criteria; the catalyst model's "events" became a cadence
  limitation to be fixed at the polling/recipe layer, not a detector result.
- Nothing was fitted, published or deployed; the artifact hash is unchanged.
