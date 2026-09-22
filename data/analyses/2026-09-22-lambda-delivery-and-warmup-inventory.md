# Lambda delivery declaration and warmup input inventory — 2026-09-22

Desk pass for the non-fitting parts of [B06](../../docs/roadmap/tasks/B06-lambda-delivery-error.md)
and [B07](../../docs/roadmap/tasks/B07-warmup-and-idle.md). **Nothing was
fitted, published or deployed**: no `behavior_models.yaml`, no `models/`, no
Pi (operator decision 2026-09-22, "don't retrain yet"). Everything here is
measurement, declaration and code contract.

Reproduce:

```
.venv/bin/python scripts/lambda_delivery_evidence.py \
    --declaration data/nissan-xterra-2008/lambda_delivery.yaml \
    --out /tmp/lambda-evidence.json \
    ~/canary-data/sessions-0921/_capture-fuel-target-research-2026-09-21T195844 \
    ~/canary-data/sessions-0921b/_capture-tuning-2026-09-21T223734 \
    ~/canary-data/sessions-0921c/_capture-tuning-2026-09-21T232153

.venv/bin/python scripts/inventory_cold_starts.py --root ~/canary-data \
    --out /tmp/cold-starts.json
```

The lambda figures are additionally asserted against a committed slice of
`_capture-tuning-2026-09-21T223734` in
`tests/test_lambda_delivery_known_answers.py`; the warmup figures against a
committed slice of the 2026-09-18 cold start in `tests/test_warmup_inputs.py`.
Both slices reproduce the whole-session numbers exactly.

## 1. What the commanded and measured quantities are (B06.1)

`data/nissan-xterra-2008/lambda_delivery.yaml` now declares them as data:
which channel may be a target, in which regime, what quantity it is, how fuel
cut appears on it, its plausible range, and the evidence for each claim. The
residual is pinned in the schema as
`100 * (lambda_measured / lambda_target - 1)`, percent, **positive = lean of
target**, and `residual_pct()` refuses phi outright — the same ratio of phis
would report a lean delivery as rich. `src.analysis.tuning.delivery_error`
now computes its `error_pct` through that one function, so the analysis path
and any later serving path cannot drift apart in sign or definition.

Sources, and why:

| Source | Quantity | Valid regimes | Fuel cut appears as |
|---|---|---|---|
| `mem:ECM_TARGET_EQUIV_RATIO` | phi | closed loop + enrichment | `_SENTINEL` row (no number at all) |
| `obd:COMMANDED_EQUIV_RATIO` (PID 0x44) | phi, from `afr_calibration.yaml` | **enrichment only** | the value 0.0 |
| `did:ECM_ENRICHMENT_TARGET_EQUIV_RATIO_CANDIDATE` (0x1134) | — | refused | indistinguishable from stoich (clamped at raw 64) |

Measured sources are `obd:O2_WR_LAMBDA_S1` (lambda, rails at 2.000) and the
A/F sensor voltages through the calibrated transfer (AFR, censored outside
the transfer bounds). The ~3.8 % ECM-lambda-vs-A/F-voltage disagreement is
declared, with its conditions and `status: unresolved`, so no result can
quietly pick a side: every figure below names the measured source it used.

## 2. Known answers, and the relations that must fail (B06.2)

All pairing is acquisition-time (`match_ordered` / `align_pairs`), per sample,
with the per-sample ratio inspected — never two aggregates.

**The RAM target's phi convention, by chain.** On steady enrichment the RAM
word equals PID 0x44 *exactly*: 3/3, 7/7 and 10/10 steady pairs on the three
2026-09-21 drives (250 ms tolerance). PID 0x44 above stoich is phi — the
78592f4 WOT result, re-derived here from an independent instrument: judged
against the lambda sensor on 223734, 18 off-stoich pairs give a median
absolute error of **2.38 % as phi** against **24.44 % as lambda**.

A *direct* check of the RAM word against the lambda sensor is **unresolvable**
on these drives and says so: enrichment holds the declared 3 s dwell so
rarely at a ~1 Hz target poll that only 0–5 steady off-stoich pairs exist per
drive, below the 10-pair floor. `check_target_convention` returns
`unresolvable` rather than a verdict, and refuses to judge at stoich at all
(both conventions read 1.0 there).

**PID 0x44 has two behaviours, and only one of them is a target.** With the
RAM word holding exactly 1.0 in closed loop, PID 0x44 reads quantized values
(0.8707, 0.9796, 1.0204, …) and matches the RAM word on only 8.1 % / 9.6 % /
28.9 % of steady closed-loop pairs (ratio p95 = 1.1485). Judged per side
against the lambda sensor, its readings **below 1.0 track measured lambda**
(median absolute error 2.40 % as lambda vs 24.09 % as phi, 18 pairs on
223734; 1.34 %, 43 pairs on 232153). That is a sensor-like quantity, not a
command — hence `valid_regimes: [enrichment]`, and closed-loop rows sourced
from PID 0x44 are refused as `source_not_valid_in_regime` (504–1,154 rows per
drive) instead of producing a residual.

**Fuel cut is a state on three channels at once.** Of 151 sentinel reads on
223734, 147 pair with PID 0x44 = 0.0 and 124 with the lambda sensor at its
2.000 rail (the sensor lags the command — that is the transport delay, made
visible). Conversely only 13 of 556 PID-0x44 zero reads land within 250 ms of
a *numeric* RAM target, at cut edges.

**Negative controls.** The agreement test fails where it must: the
neighbouring RAM word `mem:ECM_ENGINE_SPEED_RAM` (same read window) matches
the target on 0 of 621 pairs, and `mem:ECM_IGNITION_TIMING_RAM` judged as if
it were a mixture target returns `neither` (median error 1,592 % as phi,
94 % as lambda, 244 pairs).

**Ineligible states are named, never numbers.** `delivery_samples()` emits one
row per target observation *and* per fuel-cut observation, with
`residual_pct` left NaN unless the row is eligible. On 223734 (RAM target,
lambda sensor): 1,078 target observations = 151 `fuel_cut` + 324
`post_fuel_cut` + 29 `target_transient` + 19 `measured_rail` + 1
`measured_missing` + 554 eligible. The counts sum to the total by
construction and are asserted as such.

## 3. Eligibility and transport delay (B06.3)

The declared rule is a steady-target rule, justified by the cadence rather
than fitted: 3 s dwell within 0.005 lambda, gaps over 2.5 s break the dwell,
and a 15 s post-fuel-cut holdoff. The holdoff's evidence: over 112 cut
episodes on the three drives, measured lambda settles back within 3 % of
target (and stays there 2 s) after a median 5.6–8.5 s, p90 12.2–17.6 s, max
18.4–22.9 s — and 39 episodes never settled before the next cut or a 30 s
horizon. Those three drives are recorded in the declaration as
`informed_by`, and `delivery_samples(..., evaluation=True)` **refuses** them:
a drive that chose the constants is not independent evidence for the rule.

Acquisition mismatch is declared separately from response delay: the RAM
target and the Mode 01 PIDs are read in one poll cycle, median separation
0.093–0.104 s, p95 0.130–0.142 s, and 1085/1085, 926/927, 1375/1376 target
reads are inside the declared 0.25 s.

`estimate_transport_delay()` exists and is tested (it recovers a 1.0 s
injected lag from a synthetic drive), but **no delay is estimated**: the
declaration says `status: not_estimated`. The machinery refuses leakage in
three ways — a frozen `TransportDelay` cannot be applied to a drive in its
own `fitted_on`, applying one without naming the drive is refused, and
scoring is over all paired rows (a median reads zero at every candidate, and
scoring post-step rows alone rewards an arbitrarily large lag, since
over-shifting moves the mismatch to the rows *before* the step).

## 4. Shared analysis/serving semantics (B06.4)

* One residual, used by both `delivery_error` and the new relationship.
* The RAM target's fuel-cut sentinel is now emitted by `build_tuning_frame`
  itself as a `fuel_cut` column, so every consumer of a semantic frame
  (workbench Analysis, the tuning digest) counts cut samples through the
  named `fuel_cut` gate instead of letting them vanish before the gate
  report starts. A declared-but-never-observed sentinel still adds the
  column, so "no cut on this drive" stays distinguishable from "cut was not
  observable".
* Noise floors are condition-scoped in code: `regime_noise_floor()` hands the
  WOT-measured `delivery_error_pct` floor to the enrichment regime only, and
  refuses it for closed loop (no floor declared) and for any floor whose
  recorded `conditions` differ from the declared string or whose unit is not
  percent.
* The pooled workbench path now states its pairing (`1s grid, last
  observation per cell`, `claims_instantaneous_target_tracking: false`) and
  points at the acquisition-time path.

**Descriptive residuals on the informing drives** (eligible rows only, RAM
target; these drives set the constants, so these are measurements, not
validation): closed loop median −0.02 % / −0.38 % / −0.10 % with p05 −4.8 %
to −7.0 % and p95 +2.4 % to +3.3 % against the lambda sensor; the A/F
voltage path gives −0.06 % / −0.33 % / −0.33 % (B1) on the same drives.
Enrichment that survives the dwell is almost absent: 2 eligible rows on
223734 (+1.94 %, +2.83 %), 0 on the other two.

## 5. Warmup inputs verified before modelling (B07.1)

Measured on the committed 2026-09-18 cold start
(`sessions-0918/2026-09-18T134345`):

* **Run-time clock.** 164 reads, median rate 0.992 s per wall second (p05
  0.85, p95 1.09 — the clock is integer seconds sampled every ~6 s), **0
  resets**, 0 gaps over 30 s, first reading 20 s, max 971 s. It is a
  seconds-since-start clock, and its first answer is what places this start.
* **Coolant identity** is not re-derived here: the ground-truth suite already
  asserts the broadcast binding and Mode 01 PID 0x05 agree 1:1
  (`tests/fixtures/ground_truth/expectations.yaml`).
* **The catalyst channel is ECU-estimated, not measured.** Three independent
  indicators, each per sample: its first reading is **0 °C while intake air
  reads 16 °C at the same instant** (a probe in a running exhaust cannot be
  colder than the air entering it); the two banks return **identical** values
  on 78 % of 45 same-instant pairs (two separately plumbed thermocouples do
  not); and readings land only on a **4 °C grid**, coarser than the PID's
  1 °C. `catalyst_estimation_evidence()` requires two of the three and
  reports `unresolved` otherwise.

  Consequence for B07.5: any qualified output on this channel is a
  *temperature-trajectory* deviation. Conversion efficiency and catalyst
  aging are not observable through it, whatever the fit's error bounds.
* **The legacy 30/80 °C artifact is preserved as regression evidence** —
  version 4, `validation_sample_count: 0`, quarantined to `display` in
  `behavior_policy.yaml` — and a test now fails if its bands are quietly
  replaced by the recipe's newer 45/115 °C.
* **Correction to the B02 report's missing-evidence list:** `cat_warmup`'s
  eleven May training sessions (`2026-05-18T015823` … `2026-05-24T193045`)
  **are** in the local corpus at `~/canary-data/<name>`, with telemetry
  chunks. The B02 report recorded them as absent, which is why its catalyst
  row has no baseline and no skill. A baseline for them is computable
  without new data; it is not computed here because that is a fitting step.

## 6. Cold-start inventory (B07.2)

`scripts/inventory_cold_starts.py` over `~/canary-data` (170 session
directories, replays excluded, duplicate copies deduplicated by name), judged
by the **serving** rule — `EngineStartTracker`, the implementation the daemon
uses — per *engine run*, then grouped into independent physical drives by
parsed start time. 21 s for the whole corpus.

| | count |
|---|---|
| session directories scanned | 170 |
| no polled rows at all | 24 |
| missing the cold-start contract channels (RPM / RUN_TIME / COOLANT_TEMP) | 28 |
| engine runs found | 94 |
| **cold starts (observed start, initial coolant ≤ 40 °C)** | **12** |
| warm restarts | 30 |
| starts with unknown initial temperature (recording began mid-run) | 52 |
| independent drives | 88 |
| drives carrying a cold start | 12 (one each) |
| cold starts in non-capture sessions (discoverable for training) | 12 |
| cold-start drives with usable load/IAT spread in warmup | 6 |

The twelve cold starts, by initial coolant: 5, 17, 22, 24, 27, 31, 31, 32,
32, 34, 37, 39 °C — initial IAT 8–39 °C. Every one has catalyst rows. Only 6
of the 12 can say whether the vehicle was stopped before the start (no speed
observation covers the preceding minute in the others), and none has an
ambient-air PID: `AMBIENT_AIR_TEMP` is absent from every profile in this
corpus, so ambient context is *missing*, not zero.

**The classifier sidecar over-reports cold starts.** 122 sessions carry a
sidecar; on 15 it disagrees with the serving rule, and 14 of those are
"sidecar says cold start, the contract says no" — e.g. `2026-08-05T031055`,
whose Mode 01 RPM never leaves 0 (key-on, engine off) yet carries a
`cold_start` event. 11 of the disagreeing sidecars predate the mode-label fix
`f8b7d38`. This is the measured version of B03's finding that *a session
event is not a cold-start history*: a recipe gated only on
`require_session_events=("cold_start",)` would train on 14 recordings the
serving contract refuses.

## 7. The warmup context decision, re-run (B07.3)

`count_usable_cold_start_sessions`' rule applied to the current inventory:
**6** cold-start drives carry the required load/IAT spread during warmup,
against a floor of 8, so `choose_warmup_confounding_strategy` returns
**`widen_bands`** — the same answer as the July note, now from a count that
is current, auditable and per drive rather than per directory. Two further
qualifications, both unchanged by this run: insufficient evidence means *no
trusted alerting model*, not automatic qualification by broad bands (B02);
and the warmup recipes cannot refit at all until D03.22's relabel, because
event gating rejects sidecars written before `f8b7d38`.

## 8. What is deferred, and why

Per the operator's 2026-09-22 decision ("don't retrain yet"), nothing was
fitted or published. Still open, with the need named:

* **B06.3 (lag), B06.5, B06.6** — a frozen transport delay, a sensitivity
  evaluation and publication all need fitting and an independent corpus:
  deferred pending the operator's corpus choice, and additionally drives that
  hold enrichment past the declared dwell (the three 2026-09-21 drives give
  2 eligible enrichment rows between them).
* **B06.4 serving side** — a live path needs a published model; its 1 Hz
  pairing must declare that it is not instantaneous target tracking.
* **B07.3–B07.7** — fitting, catalyst reinstatement and publication. The
  catalyst decision now has one input it lacked: the channel is ECU-estimated,
  so reinstatement can only ever describe a temperature trajectory.
* **New cold-start drives** remain the binding constraint for a context axis:
  6 of the required 8 usable drives exist, and 6 of 12 cold starts cannot say
  whether the vehicle was stationary beforehand.
