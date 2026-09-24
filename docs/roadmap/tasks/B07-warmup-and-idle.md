# B07 — warmup validation and idle coverage

**Depends on:** B01/B02; B03 demotes the old catalyst artifact and handles
publication. Follow [B00](B00-behavior-models-README.md).

Keep the useful warm-idle baseline. Rebuild confidence in coolant/catalyst
warmup from actual cold starts rather than carrying old fits forward. The
review's catalyst exceedances establish a failed expectation, not a failed cat.

## Checklist

- [x] **B07.1 — Verify warmup measurements before modeling them.** Check
  coolant, run time and catalyst-temperature availability, scaling, freshness,
  resets and plausible trajectories against known-answer captures/catalogs.
  Establish whether the catalyst channel is measured or ECU-estimated before
  making independent-health claims. Preserve the old 30/80 °C artifact and
  its failed September result as regression evidence, not a baseline to bless
  by merely applying the newer 45/115 °C bands.
  *Done 2026-09-22* on a committed slice of the 2026-09-18 cold start
  ([`src/analysis/warmup_checks.py`](../../../src/analysis/warmup_checks.py),
  `tests/test_warmup_inputs.py`, report
  [2026-09-22](../../../data/analyses/2026-09-22-lambda-delivery-and-warmup-inventory.md)):
  **run time** is a seconds-since-start clock — 164 reads, median rate 0.992 s
  per wall second, 0 resets, 0 gaps > 30 s, first answer 20 s (which is what
  places this start), max 971 s. **Coolant** identity is not re-derived: the
  ground-truth suite already asserts the broadcast binding and PID 0x05 agree
  1:1. **Catalyst temperature is ECU-estimated, not measured** — three
  independent per-sample indicators, two required for the verdict: its first
  reading is 0 °C while intake air reads 16 °C at the same instant, the two
  banks return identical values on 78 % of 45 same-instant pairs, and readings
  land only on a 4 °C grid (PID resolution 1 °C). Consequence carried into
  B07.5: a qualified output there is a temperature-trajectory deviation, never
  a conversion-efficiency or aging claim. The legacy artifact is preserved as
  regression evidence (version 4, bands 30/80 °C, `validation_sample_count: 0`,
  quarantined to `display`) and
  `::test_the_legacy_catalyst_artifact_is_preserved_and_quarantined` fails if
  the newer 45/115 °C bands are applied to it. *Correction to the B02 report:*
  `cat_warmup`'s eleven May training sessions **are** present locally under
  `~/canary-data/<name>`; the report listed them as missing, which is why its
  catalyst row carries no baseline. *Cadence (2026-09-22, evaluator v2):*
  PID 0x3C sits on the `slow` poll tier (`obd_scheduler`: 1/15 Hz; measured
  17–18 s between value changes on the 2026-09-18 cold start), so the
  resampler marks it fresh for two 1 s rows per read and 235 of the 401
  rows in the warm-up window are refused `actual_stale:obd:CATALYST_TEMP_B1S1`
  while mode/cold-start eligibility holds for 312 — it is the poll tier, not
  the eligibility window, that makes a 3-of-5-ticks-within-8 s event
  impossible for `cat_warmup` (0 events from 26/34 firing rows in the
  [v2 report](../../../data/analyses/2026-09-22-behavior-validation-report-v2.md)).
  Named, not fixed: neither the gate nor the tier is changed here.
- [x] **B07.2 — Inventory independent genuine cold starts.** Record initial
  coolant, ambient/IAT, runtime continuity, stopped/moving history and load
  coverage per physical drive. Exclude warm restarts and mid-session fragments
  from cold-start evidence. Distinguish missing channels/sidecars from a drive
  that simply never contained an eligible start.
  *Done 2026-09-22:* [`src/analysis/cold_start_inventory.py`](../../../src/analysis/cold_start_inventory.py)
  and [`scripts/inventory_cold_starts.py`](../../../scripts/inventory_cold_starts.py),
  judged per **engine run** by the serving rule (`EngineStartTracker`, not a
  copy) and grouped into drives by parsed start time (`group_drives`). Over
  `~/canary-data` (170 session directories, replays excluded, duplicate pulls
  deduplicated; 21 s): **12 cold starts** in 12 independent drives, initial
  coolant 5-39 °C / IAT 8-39 °C, all in non-capture sessions; 30 warm restarts
  and 52 starts with **unknown** initial temperature (recording began
  mid-run) excluded by name; 24 sessions with no polled rows and 28 missing
  the contract channels reported as their own reasons. Ambient is *missing*,
  not zero — no profile in this corpus polls `AMBIENT_AIR_TEMP` — and only 6
  of the 12 can say the vehicle was stopped beforehand. **The sidecar
  over-reports:** of 122 sidecars, 15 disagree with the serving rule and 14 of
  those claim a `cold_start` the contract refuses (e.g. `2026-08-05T031055`,
  whose RPM never leaves 0 — key-on, engine off); 11 predate `f8b7d38`. A
  recipe gated only on `require_session_events=("cold_start",)` would train on
  those 14. Evidence: report §6, `tests/test_warmup_inputs.py` (committed
  cold-start slice, mid-drive and warm-restart cases, two runs in one
  recording, drive grouping, the microsecond-timestamp regression for the
  start window, and the per-directory deduplication the scan needs).
- [~] **B07.3 — Apply the existing warmup decision.** Re-run
  `count_usable_cold_start_sessions`/`choose_warmup_confounding_strategy`.
  Follow the approved rule: at least eight usable cold-start sessions with
  load/IAT spread supports adding context; otherwise use the documented wider
  bands and limitations. B02 validation still applies: insufficient evidence
  means no trusted alerting model, not automatic qualification by broad bands.
  *Decision re-run 2026-09-22:* 6 of the 12 cold-start drives carry the
  required load/IAT spread during warmup against a floor of 8, so
  `choose_warmup_confounding_strategy` returns **`widen_bands`** — the same
  answer as the July note, now from a current per-drive count (report §7).
  *Re-run on the current calibration 2026-09-23:* the corpus has **2**
  cold-start drives: `2026-09-22T163415` (coolant 24 °C, IAT 27 °C) and
  `2026-09-23T032755` (26 / 27 °C). That is 2 usable against a floor of 8,
  so the decision is again **`widen_bands`**. D03.22 is satisfied: every
  corpus sidecar is at classifier 0.3.0. **Remaining:** the refit that
  applies it skipped every warmup recipe ("no session satisfies the run-time
  continuity selection"). With 2 cold-start drives it would fall below
  `min_sessions=3` regardless, so ≥ 1 more genuine cold start on this
  calibration is needed, and 6 more for a context axis ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §7).
- [~] **B07.4 — Fit coolant warmup and cold-start idle appropriately.**
  Reuse current recipes with B01 cold-start/initial-temperature contracts.
  Restrict an idle curve to actual stationary eligible operation; moving
  warmup data must not redefine fast idle. Evaluate across distinct initial
  temperatures/load regimes, with independent held-out starts and labelled
  synthetic delayed warmup/idle-offset controls.
  *Attempted 2026-09-23 — data-blocked.* `coolant_warmup` and
  `cold_start_idle_rpm_vs_coolant` skip on the ZS44A-20260920 corpus. It has
  2 genuine cold starts (initial coolant 24 and 26 °C, one temperature
  regime), both inside what would be the test fold. That is below
  `min_sessions=3`, and neither session passes the whole-session run-time
  continuity selection. **Remaining:** ≥ 3 independent cold-start drives
  on this calibration, with spread in initial temperature, before any
  held-out evaluation.
- [x] **B07.5 — Decide whether catalyst warmup earns reinstatement.** Require
  verified input semantics, multiple held-out cold starts, meaningful error
  bounds and useful sensitivity after load/ambient context. Keep the current
  artifact quarantined if those criteria fail. Describe any qualified output
  as temperature-trajectory deviation; conversion efficiency/aging needs
  independent diagnostic evidence beyond this channel's trajectory.
  *Decided 2026-09-23: not reinstated; stays quarantined.* Semantics are
  settled (B07.1: the channel is ECU-estimated). The criteria still fail on
  the current calibration: 36 eligible rows, 0 qualifying drives, no
  held-out start, so there is no fit. The v4 artifact is carried into the
  2026-09-23 candidate set still quarantined by `artifact_fingerprint`
  (`display` only, flagged `contract_changed:recipe`), so a refit cannot
  restore it ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §2, §7). A future qualified fit would describe a
  temperature-trajectory deviation only.
- [~] **B07.6 — Validate the retained warm-idle baseline's envelope.** Include
  settled warm idle, recent-motion/transient idle and available accessory-load
  observations, tagged by physical action. Check RPM bias/spread and normal
  load steps. Document which modes the baseline covers: an `idle` RPM ceiling
  must not be advertised as detection of all elevated idle. Assess a separate
  idle-roughness or broader idle-state residual only if the data and diagnostic
  need support it; avoid self-gating the target fault out of its own evaluation.
  *Envelope measured 2026-09-23* on untouched drives (candidate v3, now
  promoted diagnostic-only; trained on one drive):
  - overall: MAE 21.3 rpm, bias +17.3 (published v2 +4.8), p95 37.5; ±150 rpm
    steps detected 1.00
  - steady rows: MAE 13.7
  - transient rows: MAE 190 (48 rows)
  - load 30–40 %: bias +99 (35 rows)
  - control rate 6 events in 0.68 h = 8.89/h > 2, so it **fails**; the
    published v2 fires the same 6

  **Correction, same day:** the coolant 60–80 °C stratum (bias +40.6) is
  *not* post-cold-start fast idle. Neither cold-start session
  (`2026-09-22T163415`, `2026-09-23T032755`) contributes a row to it.
  At 70–80 °C one session, `2026-09-22T233855`, has rpm excursions inside
  `idle` mode (mean 823–837, median 756–775). Another,
  `2026-09-23T162918`, idles normally at the same coolant (median 762, mean
  758–762). On the training drive, idle at 70–80 °C is 750 rpm, the same as
  at 90 °C.

  **Coolant floor: not added.** The data does not support one, and a floor
  read off test drives would let the test fold choose the contract (B02.3).

  **Steady-rpm gate added and refit (operator decision 2026-09-23,
  option (a)).** The recipe now persists `obd:RPM__roc ≤ 10 rpm/s`
  (`refit.WARM_IDLE_STEADY_RATES`).
  - *Limit:* B05.2's recorded 10 rpm/s, adopted as the operator named it. It
    was not re-derived.
  - *Provenance caveat:* that value was measured on
    `_capture-tuning-2026-09-21T223734`, which lies in warm idle's **test**
    drive group `2026-09-21T223515`. It was measured for closed-loop trim
    rows (STFT σ) on 2026-09-22, before this split existed, and read no
    idle residual. So it is non-test *information* but not non-test
    *data*.
  - *Training fold (`2026-09-21T194247`) cannot choose a limit:*
    - 631 idle rows, 627 of them locally steady;
    - 75, 20 and 10 rpm/s all pass the same 561 rate-defined rows;
    - the declared tie-break (passing-row sd within 10 % of locally-steady
      sd) would pick 5 rpm/s on the strength of one row, which I did not
      treat as evidence.
  - *Refit:* `--recipe warm_idle_rpm_baseline` on the frozen manifest's 53
    sessions (14 read from `_archive/`, all hashes matched). It ran from a
    detached worktree of committed code at `1c8aa31`, with only my
    evaluator-v3 and recipe hunks applied, so the in-progress drive-grouping
    edits played no part. Held-out drives unchanged: train
    `2026-09-21T194247`, test `2026-09-21T223515` + `2026-09-22T233855`.
    Output: `data/nissan-xterra-2008/_candidates/2026-09-23-warm-idle-steady/`
    (v4, artifact `sha256:579073ea…`).
  - *B02 result on those test drives:*
    - 1350 eligible → 1350 evaluated (was 1614; 46 `rate_exceeded`, 218
      without 30 s of rate history);
    - MAE 21.3 → **13.9 rpm** (ungated rows → gated rows), bias +17.3 → +10.2, p95 37.5, warning
      half-width 75;
    - ±150 rpm/60 s synthetic detection 0.96, median delay 2 s;
    - so it meets MAE ≤ 40, p95 ≤ 75, width ≤ 100 and detection ≥ 0.8.
  - *Verdict:* **`insufficient_evidence`.** Control exposure is 0.46 h,
    under the 0.5 h minimum, with 2 events (4.3/h, which would fail the ≤ 2/h
    limit once exposure suffices).
  - *Both events are rpm blips that pass the net-change gate:*
    - 2026-09-22 23:42:40 — 725 → 938 → 750 within ~10 s;
    - 2026-09-23 00:26:10 — 725 → 1412 → drive-away.

    This is the up-and-back case B05.2 recorded: a 30 s net-change gate
    cannot see a blip.
  - Not alert-eligible, so **nothing is promoted and the published set is
    unchanged**. The recipe keeps the gate (the next refit of warm idle
    flags the published v3 `contract_changed:recipe`).

  **Limit accepted as is (operator decision 2026-09-23, option (a)).** The
  10 rpm/s stands despite the provenance caveat above.

  **Local-range steady gate added (operator decision 2026-09-23, option
  (c)).** This is a B01 serving-contract schema addition:
  - `ServingEligibility.local_ranges: list[LocalRangeCondition]` (channel,
    window, max range, required `basis`). Empty lists are left out of the
    contract fingerprint, so every earlier contract and its evidence keeps
    its identity.
  - The context is built by `src/preprocessing/local_range.py` and reached
    through the shared prerequisite builder, so batch frames and the live
    ring buffer compute it identically.
  - It is evaluated in the one eligibility evaluator:
    `range_unavailable`, `range_stale`, `range_insufficient_history`,
    `range_exceeded`.
  - The refit builds it from whole sessions before selecting rows.
  - Legacy artifacts are reproduced without it.

  Warm idle now also persists **5 s rpm range ≤ 100**
  (`refit.WARM_IDLE_LOCAL_RANGES`). The basis is B05.2's "locally
  transient" definition, adopted as defined, not tuned. The same caveat
  applies: it was defined on the capture inside warm idle's test group. On
  the training fold 627 of 631 idle rows satisfy it.

  Tests:
  - `tests/test_behavior_serving_contracts.py::test_local_range_gate_refuses_a_blip_identically_batch_and_live`
  - `::test_empty_local_ranges_keep_every_existing_contract_fingerprint`
  - `::test_fit_recipe_builds_and_persists_the_local_range_contract`
  - `tests/test_behavior_validation.py::test_legacy_artifact_is_reproduced_with_its_own_gates_not_a_later_recipe_gate`

  **Refit:** same manifest, same folds (train `2026-09-21T194247`; test
  `2026-09-21T223515` + `2026-09-22T233855`). It ran from a detached worktree
  of committed `aa1e3be` plus my hunks only. Output:
  `_candidates/2026-09-23-warm-idle-local-range/` (v4, artifact
  `sha256:2b130149…`).
  - Rows: 1283 eligible → 1283 evaluated. `range_exceeded` 32 and
    `range_insufficient_history` 35 on top of the rate gate's 46 + 218.
  - Accuracy: MAE **12.1 rpm**, bias +8.7, p95 25.0, warning half-width 75.
  - Synthetic ±150 rpm/60 s: detection **1.00**, delay 2 s.
  - No-fault control: **0 events**.
  - Every accuracy, sensitivity and control criterion passes. The verdict
    is still **`insufficient_evidence`**, on one item only: control exposure
    0.46 h < 0.5 h.
  - Not a full B02 pass, so **not promoted**. The published set is
    unchanged.

  Covered: settled warm idle at steady rpm and a steady 5 s spread.
  **Not** covered: recent-motion idle, and any claim about elevated idle in
  general. **Remaining (truck):**
  - more independent idle drives on this calibration: the held-out fold
    needs ≥ 0.5 h of control exposure (it has 0.46 h) — then refit and
    re-validate with no code change;
  - accessory-load observations tagged by physical action (none exist).

  ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §7, §10, §11, §12).
- [~] **B07.7 — Report and publish the qualified subset.** Archive B02 reports,
  preserve explicit lack of coverage and use B03's rollout. Verify cold-start
  context reset across session/replay boundaries and unknown context after
  mid-drive daemon startup. Leave missing cold-start/accessory evidence named
  and unchecked rather than counting code completion as validation.
  *2026-09-23:* report archived ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md)). The qualified subset is
  empty. The published warm idle fails its control rate. Its rate- and
  local-range-gated refit (B07.6) passes every criterion except exposure:
  0 events in 0.46 h, `insufficient_evidence`, not promoted. More idle
  driving closes it. The warmup recipes cannot fit (2 cold starts), and cat_warmup
  stays quarantined. The set went out as a diagnostic-only refresh (B03.9).
  The revision-scoped shipped-artifact test is kept as a tripwire (operator
  2026-09-23; B03.9).
  **Context reset re-verified** (code reading plus
  `tests/test_behavior_serving_contracts.py::test_start_context_boundaries_session_replay_and_mid_drive_daemon_start`):
  - the batch fold ends a start at each session directory;
  - a daemon (fresh `LiveFeatureSource`) joining mid-drive has no evidence
    and refuses `cold_start_no_start_evidence`;
  - `LiveFeatureSource.reset()`, which `ReplayController` calls on replay
    entry and exit (`tests/test_replay_control.py::test_start_resets_live_source_and_persistence_gate`,
    `::test_stop_resets_live_source_and_persistence_gate_again`), drops an
    observed cold start.

  **Found, not fixed:** a writer rotation (capture arm or disarm) inside one
  engine run ends the context in batch, but the live source is never told
  about it and keeps the context. Live would serve a warmup model on rows
  the evaluation refuses. This is pinned as a strict xfail,
  `::test_start_context_live_matches_batch_across_a_writer_rotation`. The
  fix belongs with the drive-grouping change (continuity across pieces of
  one engine run), which another implementer owns.
  **Remaining:** that rotation parity; publication once a model qualifies.

**Done when:** the retained idle baseline has a measured applicability envelope,
each warmup candidate either meets held-out criteria or remains visibly withheld,
and the legacy catalyst fit cannot reappear as trusted through carry-over.
