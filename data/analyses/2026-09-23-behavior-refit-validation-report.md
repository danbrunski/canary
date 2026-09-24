# Behavior-model refit on the current calibration (B02 report) — 2026-09-23

The first refit since the operator lifted the 2026-09-22 "don't retrain yet"
deferral (operator, 2026-09-23). Corpus: only drives on the calibration in
force now, **ZS44A-20260920**, from `~/canary-data/sessions/nissan-xterra-2008`,
captures included. Candidates were written to an isolated directory; the
published artifact `data/nissan-xterra-2008/behavior_models.yaml` was not
touched. Nothing was deployed.

**Outcome: no model qualifies for promotion.** Ten recipes fitted and were
validated on untouched held-out drives; every one fails at least one declared
B02 criterion. Three looked like passes on the first run. They were not:
the evaluator counted 11 h of a parked truck as watched time, which made
their event rates read about 10x too low. That is fixed (evaluator v3, §1).

Machine-readable evidence: [2026-09-23-behavior-refit-comparison.json](2026-09-23-behavior-refit-comparison.json).
Frozen corpus: [2026-09-23-behavior-corpus-manifest.yaml](2026-09-23-behavior-corpus-manifest.yaml).
Candidate set: `data/nissan-xterra-2008/_candidates/2026-09-23/`
(`behavior_models.yaml`, `behavior_models.yaml.report.json`, and a verbatim
copy of `behavior_policy.yaml`, which the loader reads from beside the artifact).

## 0. How it was run

```
# B03.5 / D03.22 prerequisite: mode/event sidecars for every non-capture session in the corpus
.venv/bin/python scripts/label_sessions.py --config canary.pi.example.yaml \
    --sessions-root ~/canary-data/sessions/nissan-xterra-2008 --session <each of 40>
# B03.6: refit, seeded with a copy of the published artifact so versions and grids carry
.venv/bin/python scripts/refit_behavior_models.py --config canary.pi.example.yaml \
    --profile nissan-xterra-2008 \
    --out data/nissan-xterra-2008/_candidates/2026-09-23/behavior_models.yaml \
    <the 53 manifest sessions, explicit paths>
```

- **Corpus (B03.5).** `resolve_session_revision() == ZS44A-20260920` gives
  **53 directories** (40 sessions + 13 captures), 482 MB. The count of 86
  quoted with the decision is the number of directories *named*
  2026-09-20..23. 33 of those started before the flash at
  2026-09-21T03:08:45Z (names are UTC), so they belong to ZS44A-k106 and are
  excluded. There are no flash crossings, and every session resolves to one
  revision (13 of them via their capture manifests).
- **Drives.** `group_drives` (600 s join gap) forms 6 groups, 5 of them with
  eligible rows. One group (`2026-09-22T233855`, 17 pieces) runs from 23:38
  to 17:00 the next day. `2026-09-23T032755` recorded the parked truck from
  03:27 to 14:26 (GPS plus derived chatter only, no OBD) and so joins the
  evening drive to the morning one. Counting them as one drive is
  conservative for independence, because nothing leaks across folds. It was
  not conservative for exposure (§1).
- **Capture grants.** All 13 captures were passed as explicit paths, which
  under the CLI's documented semantics grants them to every recipe being
  refit. Their sidecar allowlists grant almost nothing: `[]` on the tuning
  and fuel-target-research captures, and air-charge/cam only on the two
  rom-trace captures. Both are recorded per capture in the manifest.
- **Labels.** Classifier 0.3.0, the `canary.pi.example.yaml` taxonomy. 38
  sidecars were written and 2 were already current. Dispositions came out
  14 ready, 7 review, 17 quarantine; they are recorded, not used as a
  filter, because each recipe gates its own rows.
- **Legacy fits (B03.5).** Seven of the eight published models carry no
  `tune_revision`. Their training sessions resolve to ZS04C-stock and
  ZS44A-osiris-initial. `air_charge` carries ZS44A-k106. None was fitted on
  the current calibration.
- **Known answers first.**
  1. The published artifact's seven persisted holdouts still reproduce
     exactly (`--mode historical`, `evidence_historical.yaml`).
  2. All 10 candidate fits' persisted held-out numbers (evaluated count,
     coverage, R², warning and critical rates) reproduce exactly through the
     *independent* evaluator frame path (`build_evaluation_frame`,
     mode-inputs decode) — `candidate_historical_reproduction` in the JSON.
- **Runtime.** 1 h 17 min, with a peak RSS of 12.7 GB. A 2-worker parallel
  build was OOM-killed beside the concurrent IF retrain and fell back to
  serial. The first run took 45 min.

## 1. Defect found: parked hours counted as exposure (evaluator v3)

`_evaluated_hours` measured each drive's exposure from its first evaluated
row to its last. On the group above, that span includes 11 h of the truck
parked. The first run (evaluator v2) therefore rated:

| model | events | exposure v2 | events/h v2 | exposure v3 | events/h v3 | criterion |
|---|---|---|---|---|---|---|
| warm_idle_rpm_baseline | 6 | 17.43 h | 0.34 | 0.68 h | **8.89** | ≤ 2 |
| stft_bank_balance_city | 5 | 17.38 h | 0.29 | 1.75 h | **2.86** | ≤ 2 |
| stft_bank_balance_highway | 9 | 16.95 h | 0.53 | 0.65 h | **13.9** | ≤ 2 |

All three were stamped `passed`, and `serving_status` granted them `alert`.
The fix: exposure now breaks at any gap between evaluated rows longer than
the drive join gap (`EventPolicy.exposure_break_seconds`, taken from the
profile's `drive_join_gap_seconds`). Rows that would have been two drives
in two directories count as two watches in one directory as well. Sparse
polling (the catalyst's 17 s pairs) is still exposure. `EVALUATOR_VERSION`
3; `tests/test_behavior_validation.py::test_parked_hours_inside_one_drive_are_not_exposure`.
The fit and every other number are bit-identical between the runs (same
`training_data_hash`); only exposure, and the verdicts that depend on it,
changed.

## 2. Candidate verdicts (untouched held-out drives)

Split by `chronological_split`: the newest qualifying drives form the test
fold, with no calibration fold. Test drives were `2026-09-22T154716` +
`2026-09-22T233855`, except warm idle (`2026-09-21T223515` +
`2026-09-22T233855`).

| model (candidate version) | eligible→evaluated | MAE | bias | p95 | warn ½-width | skill | events/h | synthetic | verdict / failures |
|---|---|---|---|---|---|---|---|---|---|
| warm_idle_rpm_baseline v3 | 1614→1614 | 21.3 rpm | +17.3 | 37.5 | 75 | — | 8.89 | ±150 rpm: 1.00 | **failed**: control events 8.89/h |
| timing_advance_vs_rpm_load v3 | 7976→7261 | 4.85° | −1.94 | 17.9 | 13.2 | 0.35 | 1.21 | −3°/60 s: 0.56 | **failed**: MAE, width, synthetic, transients 0.30/0.38 |
| timing_advance_vs_rpm_iat | 7959→1964 | 3.50° | +0.65 | 18.2 | 1.77 | 0.08 | 5.62 | — | not published (held-out coverage 24.7 % < 50 %); failed |
| fuel_trim_vs_rpm_load v3 | 6640→6157 | 4.09 % | −0.48 | 11.8 | 17.1 | −0.02 | 2.42 | +5/120 s: 0.18 | **failed**: skill, width, synthetic, events |
| air_mass_per_cycle_vs_rpm_load v3 | 7982→7238 | 0.125 g | +0.025 | 0.54 | 0.28 | 0.72 | 0 | −0.08/60 s: 0.25 | **failed**: MAE, width, synthetic |
| ve_vs_rpm_load v3 (ambient) | 7921→7142 | 3.17 % | +0.42 | 13.2 | 7.91 | 0.73 | 0 | −3/60 s: 0.38 | **failed**: MAE, width, synthetic |
| total_trim_vs_rpm_load_b1 v1 | 1679→1470 | 3.59 pp | +0.14 | 9.65 | 12.1 | 0.11 | 0.06 | +5/120 s: 0.04 | **failed**: skill, width, synthetic |
| total_trim_vs_rpm_load_b2 v1 | 1647→1433 | 3.44 pp | +0.35 | 9.32 | 10.7 | −0.01 | 0.06 | +5/120 s: 0.07 | **failed**: skill, width, synthetic |
| stft_bank_balance_city v1 | 2151→2151 | 0.94 % | +0.04 | 2.49 | 3.13 | — | 2.86 | ±5/120 s: 0.93/0.96 | **failed**: control events 2.86/h |
| stft_bank_balance_highway v1 | 1539→1539 | 1.16 % | +0.22 | 3.13 | 2.66 | — | 13.9 | ±5/120 s: 0.94/0.94 | **failed**: control events 13.9/h |
| stft_bank_balance_idle | 1041→1041 | 1.79 % | +1.77 | 3.63 | 2.40 | — | 17.9 | — | not published (held-out warning rate 18 % > 10 %); failed |
| air_charge_vs_rpm_throttle_did v2 | 4275→4009 | 1771 counts | +538 | 6035 | 8317 | 0.71 | 0.45 | −1500/60 s: 0.17 | **failed**: MAE, width, synthetic |
| afr_bank_balance_robust | 3675→3675 | 0.102 V | −0.05 | 0.43 | 0.05 | — | 40.2 | — | not published (critical rate 18 %); no declared criteria |
| afr_voltage_vs_rpm_throttle_did | 3638→3565 | 0.49 V | −0.02 | 1.85 | 0.10 | — | 45.5 | — | not published (critical rate 45 %); no declared criteria |

Not fitted, with the reason each gives:

| recipe | reason |
|---|---|
| `coolant_warmup`, `cold_start_idle_rpm_vs_coolant`, `cat_warmup` | "no session satisfies the run-time continuity selection". There are only 2 genuine cold starts on this calibration (§7), below `min_sessions=3` whatever the continuity result. |
| `cam_primary/secondary_vs_command_rpm_did` | `did:ECM_INTAKE_CAM_ANGLE_COMMANDED` is never polled on this calibration (neither the rom-trace nor the tuning profile carries it). |
| `tps_sensor_1_vs_sensor_2_did` | 244 clean rows < 300 (TPS2 sparse, one drive). |
| `ve_manifold_vs_rpm_load` | No MAP sensor. |
| `ltft_bank_balance_*` | Every LTFT sample on this calibration is exactly 0.0; the fit refuses a zero-width domain. |

**Qualified set for promotion: empty.** Per-row band exceedance and events
on these unlabelled drives are no-fault controls, not false positives.
Synthetic detections are synthetic sensitivity, not fault recall.

## 3. B03.7 — baseline vs candidate on the same recordings (offline)

The daemon's runtime replay (`start_replay`, `_replay-*` outputs) needs a
running daemon and was not used; `scripts/replay.py` has no behavior-event
path. The offline equivalent runs the evaluator (`evaluate()` in
`scripts/evaluate_behavior_models.py`: the serving contract, then the
daemon's `PersistenceGate` + emitter dedupe) with the published artifact on
each candidate's own test drives. The published legacy artifacts have no
contract and are graded through their catalog recipe contract, and are
labelled as such. The eligible rows are identical by construction.

| model | evaluated pub → cand | MAE pub → cand | warn ½-width pub → cand | events pub → cand | synthetic pub → cand |
|---|---|---|---|---|---|
| warm_idle_rpm_baseline | 1614 → 1614 | 18.7 → 21.3 rpm (bias +4.8 → +17.3) | 75 → 75 | 6 → 6 | 1.00 → 1.00 |
| timing_advance_vs_rpm_load | 6976 → 7261 | 5.60 → 4.85° | 23.1 → 13.2 | 3 → 3 | 0.52 → 0.56 |
| fuel_trim_vs_rpm_load | 5416 → 6157 | 5.39 → 4.09 % (bias −3.60 → −0.48) | 19.1 → 17.1 | **44 → 6** | 0.04 → 0.18 |
| air_mass_per_cycle_vs_rpm_load | 6516 → 7238 | 0.183 → 0.125 | 0.95 → 0.28 | 8 → 0 | 0.36 → 0.25 |
| ve_vs_rpm_load | 6451 → 7142 | 4.82 → 3.17 | 27.1 → 7.9 | 17 → 0 | 0.44 → 0.38 |
| air_charge_vs_rpm_throttle_did | 3720 → 4009 | 1873 → 1771 | 6510 → 8317 | 0 → 1 | 0.18 → 0.17 |

The top refusal on every row set is `engine_state_stale:obd:RPM` (about
53k rows), i.e. the parked recording. Off-map drops (published → candidate)
are timing 1000 → 715, fuel trim 1224 → 483, air mass 1466 → 744, VE
1470 → 779. The published VE row contract labels its actual as the legacy
`derived:VE_PERCENT`, the candidate as `derived:VE_AMBIENT_PERCENT` (B04.5).
**Fusion contribution:** none on either side. No published or candidate
model holds `alert`, so neither casts a fusion vote. Fusion groups are
`behavior:<output channel>`, which matters in §4 and §5.
**IF/sequence compatibility:** no `deviation:*` column is consumed by the
tracked manifests (`models/nissan-xterra-2008/*`) or by the concurrent
retrain's candidates (`models/_candidates/2026-09-23-na/*`, read-only
grep), so promoting any behavior set strands nothing.

## 4. Timing and airflow (B04)

- **B04.2 — context.** Candidate timing residuals on the test drives, split
  by stratum:
  - IAT: [20,30) °C MAE 6.08° (bias −2.62), [30,40) 2.89°, [40,50) 0.20°.
    High IAT is low airflow and idle (IAT is stagnation, not heat), so IAT
    here stands in for operating point rather than supplying context.
  - Coolant: < 60 °C MAE 8.88° (bias −5.62, 249 rows from 2 cold starts)
    against 4.21° at 90–100.
  - Load: < 20 % MAE **10.9°**, the decel/overrun rows, against 1.18° at
    20–30 %.
  - Baro: only 76–82 and 82–90 kPa (MAE 5.11 / 4.41°).
  - Steady 3.25° vs transient 5.82°.

  Neither IAT nor coolant explains the dominant error, which is low-load
  overrun. An air-density axis cannot be assessed: no drive on this
  calibration is below 76 kPa. The surface stays exploratory.
- **B04.4 — improvement.** Candidate against published on the same eligible
  rows: MAE 5.60 → 4.85°, median warning width 23.1 → 13.2°, skill 0.35
  (passes ≥ 0.3). It still fails MAE ≤ 3°, width ≤ 3°, synthetic −3°/60 s
  detection 0.56 < 0.8, and transient controls 0.30/0.38 > 0 (the
  3-of-5-in-8 s gate cannot express "sustained", B04.3). It remains
  diagnostic-only; `behavior_policy.yaml` keeps it `exploratory`. RPM/IAT
  regains nothing: held-out coverage is 24.7 % and it failed.
- **B04.6 — normalization.** Test rows cover baro 79–82 kPa and IAT
  20–58 °C, so density normalization cannot be validated across pressure on
  this calibration. Air mass and VE fail the same way at similar relative
  resolution (MAE / warning width 0.45 and 0.40). Air charge (raw DID
  counts, scale unpinned, grid edges from the ZS44A-k106 prior) has 41 of
  49 cells populated and MAE 1771 counts against a 1500-count target.
- **B04.7 — ablation on identical rows.** Air mass evaluated 7238 rows and
  VE 7142; the intersection is 7139, with 99 rows air-mass-only (VE actual
  stale 51, VE off-map 48) and 3 VE-only. On the intersection: air mass MAE
  0.125 g, width 0.281, 0 events; VE MAE 3.17 %, width 7.92, 0 events. The
  input-level synthetic cases (60 s windows, 142 clean windows each) came
  out as follows:

  | case | air mass | VE | unique to VE |
  |---|---|---|---|
  | MAF reads 10 % low (common input; also how unmetered air looks) | 0.23, delay 23 s | 0.15, delay 24 s | 0 (all 22 VE detections are air-mass detections) |
  | baro reads 3 kPa low (VE-only input) | 0 | 0 | 0 |
  | IAT reads 10 °C high (VE-only input) | 0 | 0 | 0 |

  Blind spots, stated honestly: air mass cannot see a baro or IAT error by
  construction. VE could in principle, but its ~7.9 % warning width is
  wider than the 3.4–3.9 % such an error produces. Neither can separate a
  MAF bias from unmetered air.
- **B04.8 — retirement decision.** VE preserves **no** unique sensitivity
  (0 unique detections) and fails B02, so the air-mass surface is **not**
  retired in favour of VE. Air mass itself fails B02 (MAE 0.125 > 0.08,
  synthetic 0.25), so it stays diagnostic-only. Fusion: the two surfaces
  vote in different groups (`behavior:derived:AIR_MASS_PER_CYCLE`,
  `behavior:derived:VE_AMBIENT_PERCENT`). If both ever alerted, one
  MAF/RPM measurement would count as two independent confirmations. That
  needs a shared group before either can qualify.

## 5. Trim, cam, TPS, AFR (B05)

- **B05.3.**
  - *Total trim:* B1/B2 fail skill (0.11 / −0.01 < 0.2), warning width
    (12.1 / 10.7 > 5) and synthetic +5 %/120 s detection (0.04 / 0.07).
  - *LTFT:* exactly 0.0 on every sample on this calibration, so total trim
    here is STFT under another name, **and LTFT adaptation cannot be
    tested**.
  - *STFT bank balance* (criteria declared 2026-09-23, before the refit,
    `behavior_validation.yaml`): city and highway meet MAE/p95/width and
    detect bank-specific ±5 % steps (0.93–0.96), but fail the no-fault
    control rate (2.86 / 13.9 events/h > 2). Idle has bias +1.77 % and a
    held-out warning rate of 18 %, and is not published.
  - A symmetric change (both banks +5) leaves bank balance unchanged by
    construction; total trim is the recipe that should see it, and it
    detects 4–7 %.
  - Fusion: `total_trim_b1`/`_b2` vote as two groups for one ECU
    correction, and would need a shared group before either could alert.
- **B05.4 — same rows.** On the 1405 rows all three trim surfaces evaluate:
  - published STFT v2: MAE 5.29 %, bias −3.98, 17 events, synthetic 0.00
  - candidate STFT v3: MAE 3.81 %, 2 events, synthetic 0.03
  - total trim B1: MAE 3.48 %, 1 event, synthetic 0.03

  Outside the intersection, STFT v3 has 4721 rows that total trim refuses
  (rate gates 3030, open loop/fuel cut 971, warmup 349). No replacement
  qualifies. The old surface stays diagnostic-only: legacy, no contract, it
  cannot alert.
- **B05.6 — cam.** Commanded cam is absent from every session on this
  calibration: 0 eligible rows.
- **B05.8 — AFR bank balance.** It fits, but is not published: held-out
  critical rate 18 %, 97 events in 2.4 h. The recipe has no fuel-state,
  mode or steady-state gate, so enrichment and fuel-cut rails are graded,
  and no criterion is declared. The voltage→AFR transfer is verified
  (`afr_calibration.yaml`), but a voltage difference is non-linear in
  lambda (≈ 2 AFR/V rich of 14.7, ≈ 3.9 AFR/V lean). It stays
  unpublished.

## 6. Lambda delivery (B06)

Delivery states on the five tuning captures after the lambda declaration's
three `informed_by` drives (RAM target vs PID 0x24): closed-loop eligible
448 / 556 / 11 / 989 / 473, **enrichment eligible 0 / 1 / 0 / 1 / 0**. The
sustained enrichment that B06.5 needs is still absent on this calibration.

`estimate_transport_delay` on the three informing drives (candidates
0–3 s in 0.25 s steps, 2140 rows) picks **0.25 s**. That is the first
non-zero step and equals the acquisition tolerance, with a 4 % score change
(2.32 → 2.22). Frozen and applied to four independent captures, it tightens
the closed-loop p05/p95 on one (−4.52/3.25 → −3.56/2.93 %) and widens p95
on the other three. At the ~1 Hz target cadence, lag is not resolved beyond
acquisition skew. It is not written into `lambda_delivery.yaml`: the
declaration has no numeric field for a frozen delay, and a lag written into
the `note` would be a constraint recorded as a comment.

## 7. Warmup and idle (B07)

- **Cold starts on this calibration: 2.** `2026-09-22T163415` at 16:53Z
  (coolant 24 °C, IAT 27 °C) and `2026-09-23T032755` at 14:26Z (26 / 27 °C).
  Both fall in the test fold's drive groups. Across the 40 sessions
  (`inventory_cold_starts` rule, 17 drives by its own grouping) there were
  26 engine runs: 2 cold, 8 warm restarts and 16 of unknown initial
  temperature. The usable cold-start drive count is 2 against a floor of 8,
  so `choose_warmup_confounding_strategy` returns **`widen_bands`**. The
  recipes already carry the widened bands.
- **cat_warmup.** 36 eligible rows, no fit. The quarantined v4 artifact is
  carried into the candidate set still quarantined (policy fingerprint) and
  still `display` only.
- **Warm idle envelope** (candidate v3, test drives):
  - by coolant: steady rows MAE 13.7 rpm; transient 190 (48 rows); coolant
    60–80 °C bias **+40.6 rpm** (3 of the 6 events). *Corrected in §10:*
    these are rpm excursions inside `idle` on one session, not
    post-cold-start fast idle.
  - by load: 20–30 % bias +12.6; 30–40 % +99 (35 rows).
  - The published v2 has the same 6 events and a smaller bias (+4.8). The
    candidate trained on one drive (`2026-09-21T194247`, 631 rows).
  - No accessory-load (A/C, electrical) observation is tagged anywhere in
    this corpus.

  The baseline covers settled warm idle only. It is not a detector of
  elevated idle in general.

## 8. What promotion would change (B03.9, the orchestrator's)

Promotion would copy `_candidates/2026-09-23/behavior_models.yaml` (and its
`.report.json`) over `data/nissan-xterra-2008/behavior_models.yaml`, after
preserving the published pair under `data/nissan-xterra-2008/_prior/<UTC stamp>/`
(the refit CLI's `preserve_prior_artifact` convention).
`behavior_policy.yaml` is unchanged; the candidate directory's copy is
byte-identical. The model list would change as follows:

- **Refit on ZS44A-20260920**, with a serving contract and failed B02
  evidence: warm_idle v2→v3, timing_rpm_load v2→v3, fuel_trim v2→v3,
  air_mass v2→v3, ve v2→v3, air_charge v1→v2.
- **New:** total_trim_b1 v1, total_trim_b2 v1, stft_bank_balance_city v1,
  stft_bank_balance_highway v1.
- **Carried over unchanged, flagged `contract_changed:recipe`:**
  timing_rpm_iat v2 (demoted), cat_warmup v4 (quarantined).
- **No model gains `alert`.**

`learned_feature` stays granted to the unvalidated models (as today), and
no IF/sequence manifest consumes a deviation column. Promoting is therefore
a display/diagnostic refresh: current-calibration surfaces with contracts,
tighter widths and fewer off-map rows. It is not an alerting change.

## 9. What this does not show

- No labelled faults. Every detection above is synthetic sensitivity, and
  per-row exceedance is not a false positive.
- Two test drive groups, one of which is two physical drives joined by the
  parked recording. Independence is under-counted, never over-counted.
- One calibration and one altitude band (76–90 kPa). No MAP sensor. LTFT is
  constant 0.0.

## 10. Follow-up, same day (after the verifier and operator decisions)

**Promotion (operator, 2026-09-23).** The candidate set was promoted as a
diagnostic-only refresh by the orchestrator. The published pair is now
byte-identical to `_candidates/2026-09-23/`, and the replaced pair is in
`data/nissan-xterra-2008/_prior/2026-09-24T024700/`. The known-answer tests
load the replaced artifact from
`tests/fixtures/behavior_validation/behavior_models_2026-09-22.yaml`. The Pi
deploy is owed (B03.9).

**Fusion groups (B04.8, B05.3).** Fixed at the shared layer.
`FusionScorer.behavior_source(name, band, *, channel=...)` derives the group
from the graded channel's measurement family
(`src/alerts/fusion.py::behavior_fusion_group`), and the `group=` argument
is gone, so a caller cannot group per channel again:
- `airflow` = MAF, air mass and every VE channel;
- `fuel_trim` = STFT/LTFT per bank, total trim and both bank balances;
- anything else keeps its per-channel group.

Before this, `fuel_trim_vs_rpm_load` (STFT B1) and `total_trim_b1`
(STFT B1 + LTFT B1) were two voices over one reading. No shipped model
alerts, so no live fused verdict changes today.

**Warm idle, coolant floor (B07.6): not added.** At 70–80 °C coolant on the
test drives, the idle rows come from two sessions:

| session | rows | median rpm | mean rpm |
|---|---|---|---|
| `2026-09-22T233855` | 131 | 756–775 | 823–837 |
| `2026-09-23T162918` | 127 | 762 | 758–762 |

The first session's mean ≫ median means rpm excursions inside `idle`
mode. The second idles normally at the same coolant, and neither is a
cold-start session. On the training drive, idle at 70–80 °C is 750 rpm
(215 rows), the same as at 90 °C. Coolant is therefore not the driver. A
floor chosen from these test rows would also let the test fold choose the
contract (B02.3). The stratum pointing at the problem is steady (MAE 13.7)
against transient (MAE 190, 48 rows). A steady-idle rate gate on `obd:RPM`
is the candidate fix, and its limit is an operator decision. **No refit was
run and nothing was re-promoted.**

**Cold-start context boundaries (B07.7).** Re-verified by code reading
(`EngineStartTracker`, `append_start_context`, `LiveFeatureSource.reset`,
`ReplayController._reset_in_memory_state`) and a new test:
- the batch fold ends a start at each session directory;
- a mid-drive daemon start is unknown and refuses
  `cold_start_no_start_evidence`;
- a replay reset drops an observed start.

Found: a writer rotation inside one engine run (a capture arm or disarm
past the 60 s start window) ends the context in batch but not live. This is
pinned as a strict xfail.

## 11. Warm idle with a steady-rpm gate (operator decision 2026-09-23)

**Contract.** `warm_idle_rpm_baseline` now persists `obd:RPM__roc ≤ 10 rpm/s`
(`refit.WARM_IDLE_STEADY_RATES`). The limit is B05.2's recorded value,
adopted as named and not re-derived.

Its provenance capture, `_capture-tuning-2026-09-21T223734`, sits inside
warm idle's test drive group `2026-09-21T223515`. It was measured on
2026-09-22 for closed-loop trim rows (STFT σ), before this split existed,
and reads no idle residual.

Warm idle's training fold (`2026-09-21T194247`) cannot choose between
limits:

| limit (rpm/s) | rows passing | locally transient passing | RPM sd |
|---|---|---|---|
| 75 | 561 | 1 | 19.0 |
| 20 | 561 | 1 | 19.0 |
| 10 | 561 | 1 | 19.0 |
| 5 | 560 | 0 | 15.7 |

That fold has 631 idle rows, 627 of them locally steady (sd 15.9). The 5 rpm/s
row differs from the others by one row.

**Refit.** Recipe `warm_idle_rpm_baseline` only, over the frozen manifest's
53 sessions. Fourteen of them now live in `_archive/`; their content hashes
match the manifest. The run used a detached worktree of committed code
(`1c8aa31`) plus only my evaluator-v3 and recipe hunks, so the
drive-grouping rewrite in progress in the working tree played no part.
Runtime 2 h 17 min (the parallel build was OOM-killed and fell back to
serial); peak RSS 10.8 GB. Split unchanged: train `2026-09-21T194247`, test
`2026-09-21T223515` + `2026-09-22T233855`.

| | v3 (ungated, promoted) | v4 (gated candidate) |
|---|---|---|
| eligible → evaluated | 1614 → 1614 | 1350 → 1350 |
| MAE / bias | 21.3 / +17.3 rpm | 13.9 / +10.2 rpm |
| p95, warning ½-width | 37.5, 75 | 37.5, 75 |
| synthetic ±150 rpm / 60 s | 1.00 | 0.96 (delay 2 s) |
| control events / exposure | 6 in 0.68 h (8.89/h) | 2 in 0.46 h |
| verdict | failed | **insufficient_evidence** (control_hours 0.46 < 0.5) |

v4 refusals: `rate_insufficient_history` 218 (the first 30 s),
`rate_exceeded` 46.

**The two remaining events are rpm blips that pass a 30 s net-change
gate:**
- 2026-09-22 23:42:40 — 725 → 938 → 750 in about 10 s;
- 2026-09-23 00:26:10 — 725 → 1412, then drive-away.

Two events in 0.46 h is 4.3/h, above the 2/h limit, so the model would fail
once there is enough exposure. **Not alert-eligible, not promoted.** The
candidate is `data/nissan-xterra-2008/_candidates/2026-09-23-warm-idle-steady/`
(artifact `sha256:579073ea…`); the published set is untouched.

**Defect found on the way.** Reproducing a legacy artifact used the
*current* recipe's rate gates. With the new gate, the committed warm-idle
known answers evaluated 0 rows. `refit.legacy_reproduction_spec` now uses
the artifact's own persisted gates and names the substitution; test
`tests/test_behavior_validation.py::test_legacy_artifact_is_reproduced_with_its_own_gates_not_a_later_recipe_gate`.

## 12. Warm idle with a local-range steady gate (operator decision 2026-09-23)

**Decision.** The 10 rpm/s limit is accepted as is, with the provenance
caveat in §11. A local-range condition is added as a B01 serving-contract
schema addition.

**Contract.** Warm idle persists, in addition to the rate gate, a 5 s rpm
range ≤ 100 (`LocalRangeCondition`). The definition is B05.2's "locally
transient", adopted as defined.

Semantics, per session in time order:
- the window is `t − 5 s < t_row ≤ t`;
- the value is defined only when the recording reaches back past `t − 5 s`
  and every sample in the window is fresh;
- otherwise the row is refused by name, as `range_insufficient_history` or
  `range_stale`.

The context column `context:range_5s:obd:RPM` is built by the shared
prerequisite builder on batch frames and on the live ring buffer. The
batch/live parity test is
`tests/test_behavior_serving_contracts.py::test_local_range_gate_refuses_a_blip_identically_batch_and_live`.
An empty `local_ranges` is excluded from the contract fingerprint, and every
promoted model's evidence still matches.

**Refit.** Same frozen manifest, same folds, committed code (`aa1e3be`) in a
worktree plus my hunks only. Runtime 26.5 min, peak RSS 10.2 GB.

| | v3 (published) | rate gate only | rate + local range |
|---|---|---|---|
| eligible → evaluated | 1614 → 1614 | 1350 → 1350 | 1283 → 1283 |
| MAE / bias (rpm) | 21.3 / +17.3 | 13.9 / +10.2 | **12.1 / +8.7** |
| p95 abs error | 37.5 | 37.5 | **25.0** |
| synthetic ±150 rpm / 60 s | 1.00 | 0.96 | **1.00** (delay 2 s) |
| control events / exposure | 6 / 0.68 h | 2 / 0.46 h | **0 / 0.46 h** |
| verdict | failed | insufficient_evidence | **insufficient_evidence** |

The only unmet criterion is control exposure: 0.46 h < 0.5 h. **Not
promoted; the published set is unchanged.**

The candidate is `data/nissan-xterra-2008/_candidates/2026-09-23-warm-idle-local-range/`
(artifact `sha256:2b130149…`). It supersedes `2026-09-23-warm-idle-steady/`.

The remainder is truck-side: more independent idle drives on
ZS44A-20260920, then the same refit with no code change.
