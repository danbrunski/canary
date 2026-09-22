# B04 / B05 measurements — timing baseline, resolution machinery, airflow contract, trim & cam inputs

Desk pass, 2026-09-22, laptop only (the Pi is unreachable). **Nothing was
fitted, published or deployed** — the operator's 2026-09-22 "don't retrain
yet" decision holds, so every item that needs a candidate fit or a
publication stays open. `data/<profile>/behavior_models.yaml` and `models/`
are untouched.

Companion to
[2026-09-21-behavior-model-review.md](2026-09-21-behavior-model-review.md)
and [2026-09-21-behavior-validation-report.md](2026-09-21-behavior-validation-report.md);
it supersedes neither, but it does correct one of the evaluator's definitions
(§0).

## 0. A defect in the evaluator's clock — every event count before today was wrong

`behavior_validation._seconds` read row times as `int64 / 1e9`. Session
frames are `datetime64[us]` (pandas 3), so **every interval came out 1000x
too small**: the daemon's 8 s persistence window spanned 8000 rows, its 30 s
dedupe spanned ~8 hours (so a drive could emit at most one event), and a
"60 s" synthetic-fault window covered a whole session — which is why the
2026-09-21 report could only find 1–3 windows per model to inject into.

Fixed by pinning the unit (`stamps.dt.as_unit("ns")`); `EVALUATOR_VERSION` is
now 2. The reproduced *accuracy* numbers in §1 of the validation report are
unaffected (they are per-row), but its **event counts, events/hour and
synthetic-detection fractions are superseded by this document**.

The same pattern is still live in two files this task does not own, and both
matter:

- `src/preprocessing/session_context.py:249` — the engine-start tracker
  (B01.5) computes `epoch_seconds` the same way, so its start-evidence window
  is compressed 1000x. It decides which rows may feed the cold-start
  contracts (`coolant_warmup`, `cold_start_idle_rpm_vs_coolant`,
  `cat_warmup`). **B07 owns those recipes — fix it there.**
- `src/validation/evidence.py:420,437` — capture interarrival statistics fall
  back to stored timestamps the same way, so a `stored_timestamp`-basis
  achieved-rate figure is 1000x off.

## 1. B04.1 — the timing baseline, reproduced on every local September drive

Command (inputs pinned by content hash; a changed session refuses to run):

```
.venv/bin/python scripts/evaluate_behavior_models.py \
    --config canary.pi.example.yaml --profile nissan-xterra-2008 \
    --artifact data/nissan-xterra-2008/behavior_models.yaml --mode corrected \
    --model timing_advance_vs_rpm_load --model timing_advance_vs_rpm_iat \
    --model air_mass_per_cycle_vs_rpm_load --model ve_vs_rpm_load \
    --manifest tests/fixtures/behavior_validation/evidence_b04_timing.yaml \
    --out <report.json>
```

31 September *sessions* with Mode 01 timing (including the cool 09-18 and
09-20 drives) plus the 15 July training sessions for the baseline only;
83,496 resampled rows, 18 grouped physical drives in total, of which 10
contributed evaluated rows to the timing surface (9 to air mass, 6 to VE and
to timing/IAT). Row selection is each artifact's catalog recipe contract
(these are legacy artifacts with none of their own); drives sharing a
directory with a model's training or held-out sessions are excluded per
model.

| Model | rows → present → eligible → evaluated | MAE | bias | p95 | R² | warn half-width | skill | events/h | envelope | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| timing_advance_vs_rpm_load | 52739 → 41360 → 41317 → 36169 | 5.86° | +0.93 | 18.4° | 0.323 | 23.9° | 0.228 | 2.19 | 1.00 | failed |
| timing_advance_vs_rpm_iat | 52041 → 34925 → 34887 → 5088 | 5.52° | +2.97 | 21.8° | −0.064 | 4.6° | −0.083 | 58.0 | 0.85 | failed |
| air_mass_per_cycle_vs_rpm_load | 52041 → 41955 → 41931 → 34446 | 0.208 g | −0.07 | 0.66 g | 0.828 | 1.63 g | 0.546 | 18.8 | 1.00 | failed |
| ve_vs_rpm_load (legacy `derived:VE_PERCENT`) | 52041 → 30926 → 30911 → 25446 | 4.80 % | −1.78 | 16.7 % | 0.857 | 42.7 % | 0.590 | 7.36 | 1.00 | failed |

Drive-resampled 90 % MAE intervals: timing/load 3.83–7.35°, timing/IAT
1.68–9.83°, air mass 0.156–0.245 g, VE 3.37–5.64 %. Per-drive MAE spans
0.05–8.23° for timing/load: **the surface's accuracy is a property of the
drive's operating mix, not a constant.**

**The ~24° median warning half-width is preserved as an observed limitation**
(it is the fitted cells' 3σ, not a chosen threshold) and it is what the
failure line `warning_half_width_median=23.86>meaningful_deviation=3.0`
names.

### Regimes (B04.1's "separate before comparing")

`behavior_validation.yaml` now declares the reporting strata
(`report_regimes`), and the evaluator reports every model per stratum. An
absent or stale context channel is its own stratum, never a numeric bin.
`timing_advance_vs_rpm_load`:

| Stratum | eligible | evaluated | MAE | bias | R² | warn hw | events/h |
|---|---|---|---|---|---|---|---|
| calibration ZS44A-k106 | 37604 | 33056 | 5.77° | +1.11 | 0.325 | 23.8° | 2.0 |
| calibration ZS44A-20260920 (post-flash) | 3689 | 3089 | 6.94° | −0.93 | 0.260 | 24.2° | 4.7 |
| calibration ZS04C-stock | 24 | 24 | 0.05° | +0.05 | −0.055 | 1.9° | 0.0 |
| IAT [10,20) °C | 4754 | 3914 | 6.51° | +0.98 | 0.288 | 24.4° | 1.8 |
| IAT [20,30) °C | 20420 | 17672 | 6.51° | +0.88 | 0.245 | 24.3° | 0.8 |
| IAT [30,40) °C | 6929 | 6339 | 4.78° | +1.16 | 0.424 | 22.6° | 5.7 |
| IAT stale | 6579 | 5668 | 7.00° | +1.20 | 0.329 | 24.4° | 3.2 |
| coolant [80,90) °C | 5975 | 5196 | 11.56° | +1.17 | 0.116 | 44.0° | 0.7 |
| coolant [90,100) °C | 23755 | 20962 | 4.38° | +0.98 | 0.425 | 23.3° | 2.6 |
| load [0,20) % | 9314 | 8032 | 15.33° | +1.05 | 0.015 | 44.0° | 0.0 |
| load [20,30) % | 12150 | 9837 | 1.83° | +1.42 | 0.640 | 1.5° | 6.6 |
| load [30,40) % | 4596 | 4591 | 5.07° | +4.76 | −0.531 | 22.9° | 0.8 |
| load [40,60) % | 8169 | 8103 | 3.13° | +1.20 | 0.116 | 23.7° | 0.4 |
| load [60,101) % | 7088 | 5606 | 3.96° | −3.61 | −0.435 | 25.0° | 0.0 |
| baro [50,70) kPa (one drive) | 4030 | 3669 | 10.59° | +6.99 | −0.076 | 27.1° | 3.9 |
| baro [70,76) kPa | 5147 | 4491 | 6.78° | +1.28 | 0.138 | 22.8° | 5.6 |
| baro [76,82) kPa | 18401 | 16266 | 4.35° | +0.03 | 0.437 | 23.1° | 1.3 |
| steady | 18664 | 17250 | 4.24° | +1.06 | 0.420 | 22.6° | 2.3 |
| transient | 21274 | 17656 | 7.51° | +0.92 | 0.246 | 24.7° | 2.7 |

Read together: error is worst at low load (15.3° MAE below 20 % load, where
the bands are also widest), worse in transients than steady (7.5° vs 4.2°),
and carries a +7.0° bias on the single high-altitude drive (50–70 kPa) — the
altitude conditioning D03.20 asks about, measured on the behavior side but
**with one drive only**, which is not enough to condition a model on.

`timing_advance_vs_rpm_iat` evaluates 5,088 of 34,887 eligible rows
(coverage 0.15, a failure): September IAT is mostly below its 29 °C fitted
domain, and every evaluable row sits in the domain's cold edge. Its 58
events/h and −0.083 skill confirm the review's decision to keep it demoted.

## 2. B04.3 — the declared timing resolution, and what it costs

`ValidationCriteria.resolution` (new, profile data) states the intended
sustained change, its required duration, the measured noise that justifies
it, the diagnostic question, what an event may be claimed as, what it may
**not** be claimed as, and the transient excursions the model must not alert
on. The evaluator runs all of it through the real eligibility and the
daemon's persistence gate.

**Measured noise (not an LSB).** Standard deviation of `TIMING_ADVANCE` about
its own 60 s × (400 rpm × 10 % load) cell median, on locally steady rows
(5 s rpm range ≤ 100, load range ≤ 5 %), 6,221 rows across seven September
drives: **2.19°** (p95 1.0°, p99 6.0°). The PID LSB is 0.5° and
`noise_floor.yaml`'s WOT figure is smaller still; the declared 3° sustained
target clears the measured spread, but only by 1.4x.

**Results on the untouched September drives** (timing/load): a synthetic
−3° step lasting 60 s is seen in 210 of 718 clean windows (0.29, needs 0.80),
median detection delay 2.0 s, covered envelope 1.00. Both transient controls
— a −6° excursion lasting 3 s and 10 s, i.e. ordinary knock-control activity —
raise events in 20 % and 22 % of clean windows against a declared ceiling of
0 %. **The daemon's 3-of-5-ticks-in-8 s persistence gate cannot distinguish a
3 s excursion from a 60 s shift**, so "sustained" is not currently expressible
in the alert path; a per-model persistence requirement (or an explicit
sustained-change detector) is the missing piece, not a wider band.

For `timing_advance_vs_rpm_iat` the same controls fire in 52 % / 67 % of
windows.

An unexpected timing change is **not** proven knock retard: the recipes'
`diagnostic_capabilities` now say "unexpected sustained timing change", the
limitation "does not identify knock retard or any other cause" is attached to
both timing recipes, and `resolution.not_identified` carries the same
sentence into the evidence record.
`tests/test_behavior_resolution.py` fails if a timing recipe claims knock
identification again.

## 3. B04.5 — MAP, baro and a defaulted IAT are three different things

`compute_anomaly_signals` → `_theoretical_air_mass_per_cycle_g` prefers MAP,
falls back to barometric pressure, and substitutes 25 °C when no IAT column
exists. At part throttle MAP is far below baro, so the same name
(`derived:VE_PERCENT`) meant ~85 % on a MAP-equipped session and ~25 % on a
baro-only one — the definition depended on the capture profile.

Now:

- `derived:VE_MANIFOLD_PERCENT` — measured MAP **and** measured IAT.
- `derived:VE_AMBIENT_PERCENT` — measured baro **and** measured IAT.
- Neither falls back to the other, and neither has a default temperature: a
  missing reference or IAT means **no value**, not a different quantity.
- `derived:VE_PERCENT` is unchanged and explicitly legacy: tracked IF and
  LSTM manifests consume it (`models/nissan-xterra-2008/*/model.manifest.json`),
  and removing a column an existing detector was trained on is the 2026-09
  IF-blind incident. No recipe targets it any more.
- `derived.resolved_dependencies(frame, channel)` reports which input each
  dependency group actually resolved to, and the evaluator records it per
  session as `derived_input_resolution`, so a report can say which VE
  definition a drive produced.
- `ve_vs_rpm_load` now targets the ambient-referenced channel (what this
  vehicle, which has no MAP sensor, can actually produce) and a new
  `ve_manifold_vs_rpm_load` targets the manifold-referenced one. The shipped
  `ve_vs_rpm_load` artifact predates the split, so a carry-over is flagged
  `contract_changed:recipe` rather than served as either definition, and the
  evaluator labels the substitution in `row_contract`.
- Follow-up for D03.25's retrain specs (not done here): on a vehicle without
  MAP, `derived:VE_AMBIENT_PERCENT` is numerically identical to the legacy
  `derived:VE_PERCENT`, so a future IF/sequence feature selection would see
  two copies of one column. Pick one there; nothing is withheld today,
  because withholding a column an existing detector consumes is the failure
  mode this split exists to avoid.
- `air_mass_per_cycle_vs_rpm_load`'s notes no longer claim to "detect intake
  leak / exhaust restriction / MAF drift"; it is the raw measured quantity,
  kept for inspection, and both VE recipes now state that they share its
  MAF/RPM measurement and are **not** independent confirmation of it.

## 4. B05.2 — fuel-state and steady-state semantics for total trim

The total-trim recipes now persist, on the contract that fit and serve both
read:

- `fuel_control` = J1979 `0x02` only (already B01's membership: excludes
  open-loop warmup `0x01`, enrichment / fuel cut `0x04`, and both fault
  states),
- `excluded_operating_modes = ["warmup"]` — a new contract field, evaluated
  first, with the refusal `operating_mode_excluded:warmup`; a row with **no**
  mode label is refused (`operating_mode_unknown`) rather than assumed warm,
- steady-state rate gates `obd:RPM ≤ 10 rpm/s`, `obd:ENGINE_LOAD ≤ 0.5 %/s`,
- `output_unit = "percentage points (additive STFT+LTFT index)"`, matching
  the criteria unit; the limitation "an additive index, not a calibrated
  injector multiplier" is attached to both banks.

Steady-gate measurement, on the dense-trim capture
`_capture-tuning-2026-09-21T223734` (801 closed-loop, non-warmup rows):

| Gate (rpm/s, %/s) | rows passing | of which locally transient | STFT σ |
|---|---|---|---|
| 75 / 5 (the cam recipes' limits) | 724 | 443 | 3.47 % |
| 20 / 1.0 | 323 | 135 | 2.32 % |
| 10 / 0.5 (chosen) | 247 | 74 | 1.16 % |
| 5 / 0.25 | 174 | 46 | 1.16 % |

"Locally transient" = 5 s rpm range > 100 or load range > 5 %. **A rate gate
is a net-change gate:** `<channel>__roc` is the change over the feature
pipeline's 30 s rolling horizon divided by elapsed time, so an up-and-back
excursion inside the horizon passes it (74 of the 247 rows above). 10 rpm/s
is therefore an upper bound on steadiness, not a definition of it; a
local-range condition kind would be a B01-schema addition, recorded here as a
gap rather than bolted on.

**Bank 2 is gated on bank 1's fuel-system state.** PID 0x03 returns two
bitmask bytes; `decode_fuel_status` keeps byte A only. The raw payloads are
stored, and across 137 local sessions the two systems disagree on 770 of the
77,797 samples that carry a payload (0.99 %) — `open_loop/closed_loop` 562,
`closed_loop/open_loop` 206, `insufficient_temperature/closed_loop` 2. The
recipe now names this in its limitations. The fix is at collection (emit
fuel system 2 as its own channel; the corpus can be backfilled from the
stored payloads) and is **not** done here.

## 5. B05.1 — what the corpus can actually offer each recipe

```
.venv/bin/python scripts/inventory_behavior_inputs.py \
    --config canary.pi.example.yaml --profile nissan-xterra-2008 \
    --out <inventory.json> ~/canary-data/sessions/nissan-xterra-2008/* \
    ~/canary-data/sessions/* ~/canary-data/sessions-09*/*
```

134 unique sessions (5 duplicate paths skipped — the same drive is pulled
twice under two roots), 53 grouped drives. Every count below is rows the
recipe's **own serving contract** accepts, on frames built by the same
decode → resample → derive → segment chain the refit uses; a channel that was
never recorded is reported separately from one that was stale, and both are
separate from a contract refusal.

| Recipe | eligible rows | drives with rows | qualifying drives (≥30 rows) | most-often absent input (sessions) |
|---|---|---|---|---|
| `timing_advance_vs_rpm_load` | 54975 | 46 | 42 | obd:TIMING_ADVANCE (31) |
| `timing_advance_vs_rpm_iat` | 51756 | 46 | 41 | obd:TIMING_ADVANCE (31) |
| `air_mass_per_cycle_vs_rpm_load` | 57248 | 48 | 44 | derived:AIR_MASS_PER_CYCLE (30) |
| `ve_vs_rpm_load` (ambient) | 39873 | 42 | 38 | derived:VE_AMBIENT_PERCENT (53) |
| `ve_manifold_vs_rpm_load` | **0** | 0 | 0 | derived:VE_MANIFOLD_PERCENT (134) |
| `fuel_trim_vs_rpm_load` | 28633 | 46 | 38 | obd:SHORT_FUEL_TRIM_1 (25) |
| `total_trim_vs_rpm_load_b1` | 10082 | 30 | 11 | derived:TOTAL_TRIM_B1 (26) |
| `total_trim_vs_rpm_load_b2` | 9542 | 28 | 11 | derived:TOTAL_TRIM_B2 (28) |
| `stft_bank_balance_idle/city/highway` | 3908 / 6734 / 5979 | 22 / 26 / 16 | 9 / 12 / 8 | derived:STFT_BANK_BALANCE (27) |
| `ltft_bank_balance_idle/city/highway` | 3863 / 6370 / 5882 | 23 / 25 / 16 | 10 / 10 / 7 | derived:LTFT_BANK_BALANCE (29) |
| `air_charge_vs_rpm_throttle_did` | 20075 | 35 | 23 | did:ECM_RPM (55) |
| `afr_bank_balance_robust` | 14481 | 42 | 31 | derived:AFR_BANK_BALANCE (45) |
| `afr_voltage_vs_rpm_throttle_did` | 20334 | 35 | 23 | did:ECM_RPM (55) |
| `cam_primary_vs_command_rpm_did` | 4562 | 8 | 8 | did:ECM_INTAKE_CAM_ANGLE_COMMANDED (123) |
| `cam_secondary_vs_command_rpm_did` | 4622 | 8 | 8 | did:ECM_INTAKE_CAM_ANGLE_COMMANDED (123) |
| `tps_sensor_1_vs_sensor_2_did` | 1920 | 29 | 18 | did:ECM_THROTTLE_POS_SENSOR_2 (75) |
| `warm_idle_rpm_baseline` | 13520 | 24 | 21 | obd:RPM (11) |
| `coolant_warmup` / `cold_start_idle_rpm_vs_coolant` | 2234 / 2314 | 10 | 10 | obd:RUN_TIME (38) |
| `cat_warmup` | 250 | 10 | 3 | obd:CATALYST_TEMP_B1S1 (40) |

Trim/bank facts over the same corpus, on running rows: bank 1 paired
(STFT **and** LTFT fresh on the row) 27,209; bank 2 paired 27,238; **both**
banks paired 23,524; exactly one bank paired (missing-bank coverage) 7,399;
both banks paired *and* in closed loop 19,949, of which 946 are in `warmup`
mode and the new contract excludes. STFT bank difference exactly zero on
5,476 rows — reported as its own number, never merged with the 7,399
missing-bank rows.

Readings that matter for B05.3/B05.4:

- **Pairing is not the blocker it looked like on one session.** The short
  warmup session `2026-09-18T134345` pairs both banks on 0 of 920 running
  rows (its round leaves each trim fresh for ~2 s out of ~6.6), but across
  the corpus most drives pair fine: 2,566 closed-loop paired rows on
  `2026-09-20T152758`, 2,105 on `2026-09-18T213818`. The total-trim contract
  accepts 10,082 rows over 30 drives, 11 of them with ≥30 rows.
- **The dense-trim captures have no LTFT content.** On both 2026-09-21
  tuning captures (trims at ~2.2 Hz) every `LONG_FUEL_TRIM_*` sample is
  exactly 0.0 — LTFT was reset by the 2026-09-20 flash and had not relearned
  — so a total-trim fit there is an STFT fit under another name. LTFT carries
  content on the 09-18 and 09-20 drives (B1 mean −3.15 %, range −7.03 …
  +1.56 on `2026-09-18T213818`), which are also the drives with the ordinary
  0.33 Hz cadence.
- **Units check out.** Every `SHORT_/LONG_FUEL_TRIM_*` sample in the sessions
  checked lies on the 100/128 % lattice inside the ECM's ±25 % short-trim
  clamp (`behavior_input_inventory.trim_unit_checks` reports an all-zero
  channel separately from an off-lattice one — a relearning LTFT is a state,
  not trim content).
- **Why total trim loses rows**, in order: `engine_state_stale` 60,270,
  `fuel_status_stale` 25,033, `engine_not_running` 22,117, `rate_exceeded`
  17,191, `operating_mode_excluded` 6,229, `fuel_status_excluded` 6,010.
  The two stale counts are a polling-cadence statement, not a fault.
- **A resampled row is not an observation.** `tps_sensor_1_vs_sensor_2_did`
  shows 1,920 eligible rows because each DID sample stays fresh for up to
  2 s; the honest count is the acquisition-time pairing in §6 (~70 pairs in
  the best session).
- `ve_manifold_vs_rpm_load` has **zero** eligible rows in 134 sessions: this
  vehicle has no MAP sensor, which is the point of B04.5's split — the
  manifold-referenced quantity is unavailable, not silently substituted.

## 6. B05.5 / B05.7 — command/measured identities, by acquisition-time pairing

`scripts/validate_command_pairing.py` (new) runs a known-answer control, the
pair under test on all rows and on the steady subset only, a negative
control, and the per-sample ratio, all through `compare_channels` with an
explicit `PairingPolicy`.

**Cam (B05.5).** Four captures, 50 ms tolerance; steady = commanded angle
unchanged for ≥ 5 s (trimmed 2.5 s each end) and RPM range ≤ 200 inside the
span.

- *Known answer*: `obd:RPM → did:ECM_RPM` r²=0.9995, slope 0.996, offset 13.4
  on 292 pairs (`_capture-tuning-hybrid-research-2026-09-17T201000`). In the
  Mode 22-only captures the control refuses with "no observations … this is
  an unavailable input" — the right answer, not a number.
- *Negative control*: commanded cam → `did:ECM_AF_SENSOR_V_B1` r²=0.05–0.12,
  slope ≈ −0.02 on 2,081–17,889 pairs. The method can fail.
- *Steady tracking*: both measured channels track the command ~1:1.

| Session | pair | n | slope | offset | 1:1 residual (b−a) | ratio median (spread) |
|---|---|---|---|---|---|---|
| 09-17T201000 | cmd → MEASURED | 63 | 1.0072 | +0.04 | +0.111 ± 0.710 | ratio refused (denominator ≈ 0) |
| 09-17T201000 | cmd → MEASURED_B | 154 | 0.9999 | +0.27 | +0.273 ± 0.826 | 1.014 (±6.5 %) |
| 08-06T145004 | cmd → MEASURED | 745 | 1.0224 | +0.06 | +0.348 ± 1.051 | 1.043 (±6.4 %) |
| 08-06T145004 | cmd → MEASURED_B | 757 | 1.0171 | −0.03 | +0.193 ± 1.197 | 1.021 (±5.7 %) |
| 08-07T005351 | cmd → MEASURED | 913 | 0.9676 | +0.75 | +0.690 ± 0.573 | 0.971 (±5.7 %) |
| 08-07T005351 | cmd → MEASURED_B | 911 | 0.9661 | +1.00 | +0.935 ± 0.863 | 1.014 (±3.6 %) |

  On *all* rows (transitions included) the same pairs read r²=0.90–0.96 with
  residual spread ~3° and a ±35–50 % ratio spread: **response lag, not a
  tracking fault** — which is exactly why the recipes gate on a steady
  command and why B05.6 must characterise lag separately. Bank names stay
  neutral (`MEASURED` / `MEASURED_B`): nothing here identifies a physical
  bank, and the ~0.2–0.9° offsets between them are within the same spread.

**TPS (B05.7).** `did:ECM_THROTTLE_POS_SENSOR_2` is **never** polled densely:
its best local coverage is 122 samples in one session (`2026-09-20T152758`),
71 in another, ≤ 61 elsewhere, and **zero** in every high-rate capture. On
`2026-09-18T213818` (500 ms tolerance; the two DIDs are read ~125 ms apart):
TPS2 → TPS1 r²=0.9897, slope 0.9918, offset +0.0100 V, 1:1 residual
+0.0004 ± 0.0436 V on 71 pairs, with the known-answer control
`obd:THROTTLE_POS → did:ECM_THROTTLE_POS_SENSOR_1` landing at r²=0.995,
slope 0.0408 V/% on 70 pairs. On `2026-09-20T152758` (250 ms tolerance; the
DIDs are ~248 ms apart there) the tracks read r²=0.9988, slope 0.9933, offset
+0.0090 V on 67 pairs, but **the known-answer control could not be paired at
all** there (0 of 122 DID samples found an `obd:RPM` partner within 250 ms),
so that session is not evidence. The negative control never reached 30 pairs on any session.

Conclusion: the two tracks are consistent at ~1:1 with a ~0.01 V offset, on
~70 paired samples in one session — far below the recipe's 300 samples × 3
independent drives, with no KOEO sweep and no working negative control.
**Smallest missing capture: a KOEO throttle sweep (idle → WOT → idle, slowly)
polling `0x120F` and `0x1210` in the same round at ≥ 5 Hz, plus the same pair
during a normal drive.** Until then `tps_sensor_1_vs_sensor_2_did` stays
unfitted, and sequential-read skew during rapid movement (125–250 ms apart,
i.e. up to ~0.4 V of real throttle travel) cannot be separated from sensor
disagreement.

## 7. What this does not do

- No fit, no publication, no deployment (operator decision 2026-09-22).
- No labelled faults: every detection number above is *synthetic*
  sensitivity on unlabelled drives, and per-row exceedance is not a false
  positive.
- The altitude evidence is one drive (D03.20 stays open on the behavior side
  until a second high-altitude drive exists).
