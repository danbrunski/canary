# R05 — analysis and decode

Opened 2026-09-24 from the refactor review ([R00](R00-refactor-README.md)).

`src/analysis/` is 68 modules and ~25k lines, with 446 import sites. It falls
into these groups:

| group | size | modules |
|---|---|---|
| tuning and fuel | ~6.1k lines | afr, lambda_delivery, tuning, tuning_frame, tuning_digest, workbench, noise_floor, tune_revisions, rom_*, pull_tags, clock_align, osiris |
| behavior models | ~7.8k lines | refit, behavior_validation, *_inventory, warmup_checks, surface_1d/2d, training_disposition, session_deviations, drives, committees |
| session sidecars | ~5.3k lines | digest, trip_summary_sidecar, fingerprint, session_classifier, events/, trends, reference_epoch, derived, capture_coverage |
| signal primitives | | channels, compare, pairing, binned_analysis |
| RE toolkit | 12 modules | did_scan, broadcast_*, iso_tp, fsm, signal_lookup |

**Don't regroup the package.** The flat layout is navigable. What is wrong is
layering, and R01.8 and R05.10 fix it.

Most findings here are the AGENTS.md disciplines turning up again in new
code:

- two signals compared by an aggregate;
- a measured constraint left as a default;
- vehicle facts in shared code;
- two answers to "which registry is live?".

## Rules

- **`compare_channels` is the only sanctioned two-signal comparison.**
  Anything that replaces a hand-rolled one reads `scale_verdict` *and* looks
  at the per-sample ratio.
- **`tests/test_analysis_ground_truth.py` must stay green.** Where a number
  is expected to move (R05.3, R05.5), record the before and after here and say
  why the new one is right.
- **The recipe catalog stays in Python.** The July rejection of YAML stands.
  A recipe contract change flags Pi artifacts `contract_changed:recipe`, so
  R05.7 proves byte-identity first.
- **Vehicle facts move to profile/data files, not into other code.**

---

## Correctness first

- [x] **R05.1 Resolve the vehicle's tuning context once.** *Drift bug,
  partly serve path.*

  **The registry.** `control/calibration.append_flash` writes new flashes to
  the **operational** registry, and `active_registry_path()` prefers it
  (verified 2026-09-24). These callers read `<profile>/tune_revisions.yaml`
  directly, the *source* registry:
  - `analysis/tuning_digest.py`, which runs in the daemon's trip-end digest;
  - `mcp_server/tools/tuning_analysis.py` (×2);
  - `dashboard/routes/rom_maps.py`;
  - `scripts/tuning_report.py` (×3);
  - `tools/import_uprev_log.py`.

  So a flash recorded from the dashboard is invisible to the digest's and
  MCP's revision resolution. That is the "previous capture on this
  calibration" comparison.

  **The noise floor.** `scripts/tuning_report.py` loads it from
  `storage_path.parent/noise_floor.yaml`; every other caller uses the profile
  directory.

  **Stoichiometry.** `afr_stoich: float = 14.7` is the default in 12
  signatures (tuning ×6, lambda_delivery ×2, channels ×2, tuning_frame,
  `WorkbenchInputs`), and no caller ever passes another value. Meanwhile
  `afr.append_afr_channels` (serve path) uses `derived.fuel_properties(vehicle)`
  (gasoline, diesel, E85). The workbench and the dashboard therefore disagree
  for any non-gasoline profile.

  **Do:**
  - Promote `control/workbench_jobs.workbench_inputs()` to
    `TuningContext.from_config(config)`. It resolves the calibration,
    providers, registry (through `active_registry_path`), noise floor,
    lambda-delivery spec, idle spec, and `afr_stoich` from
    `fuel_properties`.
  - Move `profile_dir` and `active_registry_path` below `control`, for
    example into `src/profiles.py`, so analysis can import them.
  - Drop the literal `14.7` defaults and point every caller above at the
    context.

  **Accept:**
  - Failing tests first:
    - a flash appended through `append_flash` resolves in
      `compute_tuning_digest` and the MCP tuning tools;
    - a diesel profile's workbench AFR uses 14.5.
  - `grep -rn "afr_stoich: float = 14.7" src` is empty.

  Size **M**.

  **Done 2026-09-25 (implementer), one remainder.** New
  `src/analysis/tuning_context.py`: `TuningContext.from_config(config)`
  resolves calibration, providers, the *active* registry
  (`src.profiles.active_registry_path`), noise floor, idle spec,
  `lambda_delivery.yaml` (its declared regime floors) and `afr_stoich` from
  `fuel_properties`. `profile_dir` / `source_registry_path` /
  `operational_dir` / `operational_registry_path` / `active_registry_path`
  moved into `src/profiles.py` (`control.calibration` re-exports them).
  `WorkbenchInputs` now extends `TuningContext`; `workbench_jobs.workbench_inputs`
  builds it from `context_fields`. The digest, both MCP tuning tools,
  `scripts/tuning_report.py` (×3, and its noise floor now comes from the
  profile directory, not `storage_path.parent`), `rom_maps.inspect_rom`
  (now a required `registry_path`) and `tools/import_uprev_log.py` (scoped
  to the default profile first; the unscoped path pointed at
  `data/tune_revisions.yaml`) all read the active registry.
  `afr_stoich` has no default in `tuning.py` (×6), `tuning_frame`,
  `lambda_delivery` (×2) or `TuningContext`/`WorkbenchInputs`.
  Failing-first tests `tests/test_tuning_context.py`:
  `test_a_dashboard_flash_resolves_in_the_digest` and
  `..._in_the_mcp_tuning_tools` fail with the source-registry read
  (probe: re-pointing `tuning_context.active_registry_path` at
  `source_registry_path` → 2 failed) and pass now;
  `test_a_diesel_profiles_workbench_afr_uses_its_stoich` (14.5).
  Verifier follow-up (same day): the remaining direct registry reads —
  `analysis/behavior_inventory.build_inventory` (registry and its sha, now
  also `registry_path`), `scripts/refit_behavior_models.py`,
  `scripts/evaluate_behavior_models.py` (default of `--tune-revisions`),
  `scripts/inventory_behavior_inputs.py`, `scripts/evaluate_detectors.py`
  (default of `--tune-revisions`) — go through
  `src.profiles.active_registry_path`. `grep -rn "tune_revisions.yaml" src
  scripts tools` now finds only `src/profiles.py` and prose.
  `test_a_dashboard_flash_resolves_in_the_behavior_inventory` fails with the
  source read (probe → 1 failed) and passes.
  **Remaining:** `grep -rn "afr_stoich: float = 14.7" src` still finds
  `src/analysis/channels.py` ×2 (`read_channels`, `read_aligned`) — held by
  the human session; every R05.1 caller now passes `afr_stoich` explicitly.
  *Remainder done 2026-09-25.* `read_channels`/`read_aligned` take
  `afr_stoich: float | None = None` and `obd:COMMANDED_AFR` refuses without
  one (`ChannelSpecError` naming `TuningContext.afr_stoich`), as it already
  refused without a calibration; specs that do not derive commanded AFR need
  no stoich. `grep -rn "afr_stoich: float = 14.7" src` → nothing. Test:
  `test_analysis_channels.py::test_commanded_afr_without_a_stoich_says_so`; the
  four commanded-AFR tests now pass `afr_stoich=14.7` (27 pass).

- [x] **R05.2 Route every noise-floor significance through one refusing
  function.**
  - AGENTS.md says `conditions` was made a required field. It isn't:
    `noise_floor.ChannelFloor.conditions: str = "unspecified"` (verified
    2026-09-24). The Xterra data file already sets it on both channels, so
    making it required costs nothing.
  - There are four significance implementations:
    - `noise_floor.annotate`: tests only, no production caller.
    - `tuning.compare_sessions`: inline, with a hard-coded 2.0.
    - `tuning_digest`: inline, drops `conditions` entirely.
    - `workbench.pull_comparison`: computes `delta/sd` across all conditions
      against a timing floor measured under WOT. That is the documented
      ~16× understatement; it only *reports* the conditions.
  - Only `lambda_delivery.regime_noise_floor` refuses a conditions mismatch.

  **Do:** make `conditions` required. Add
  `NoiseFloor.significance(channel, deltas, *, conditions) -> Significance |
  FloorRefusal`, which refuses a mismatch and names both regimes. Replace
  the three inline copies with it. **Accept:** a failing test first: an idle
  comparison against the WOT floor refuses. Size **S–M**.

  **Done 2026-09-25.** `ChannelFloor.conditions` is required
  (`Field(min_length=1)`); `NoiseFloor.significance(channel, deltas, *,
  conditions) -> Significance | FloorRefusal` (+ module `significance()`
  for a missing floor) refuses a mismatch naming both regimes. `annotate`
  (tests only) deleted. `tuning.compare_sessions` takes `conditions` (a floor
  without it is a `GateError`; a bare float floor is no longer accepted) and
  reports `floor_refusal`; the digest's previous-capture delta takes its
  conditions from the profile's declared `noise_floors.enrichment`
  (`lambda_delivery.yaml`) via `TuningContext.regime_floors`;
  `workbench.pull_comparison` is an all-conditions comparison
  (`ALL_CONDITIONS`) and now refuses the WOT floor (`significance` block,
  `delta_sigma: None`). MCP `compare_tuning_sessions` and
  `tuning_report.py compare` gained `conditions` / `--conditions` (MCP
  snapshot regenerated). Failing-first:
  `test_an_idle_comparison_against_the_wot_floor_refuses`,
  `test_an_idle_session_comparison_against_the_wot_floor_refuses`,
  `test_a_floor_without_its_conditions_is_not_representable`
  (tests/test_analysis_tuning.py);
  `test_pull_comparison_flags_a_drop_where_timing_was_advanced` updated to
  the refusal. The shipped Xterra digest still quotes its floor (real capture
  `_capture-tuning-2026-09-23T164007`: `delta_sigma 1.84`).

- [x] **R05.3 Replace the hand-rolled two-signal comparisons.**
  - `tuning_digest._cam_tracking` computes `np.corrcoef(commanded, measured)`
    on `read_aligned(grid="1s")` cells. `read_aligned`'s own docstring says
    its output "is not pairing evidence". The only refusal is `len < 3` /
    `std > 0`, and `except Exception: return None` hides which kind of empty
    it was.
  - `tuning.scale_stability` (a two-window polyfit) has no production caller.
    It shares a name with `compare.scale_stability` but has a different
    verdict vocabulary: stable/unstable/indeterminate vs
    stable/drifting/unresolved. `STABLE_DRIFT_PCT = 1.5` is defined in both
    files.

  **Do:** use `compare_channels`, or `align_pairs` plus the per-pair error,
  for cam tracking, and have it report *which* empty. Delete
  `tuning.scale_stability` and its constants. **Accept:** the ground-truth
  suite passes. The digest's cam section is recorded before and after on one
  real capture. Size **S**.

  **Operator decision (separate):** delivery error has two engines:
  - `tuning.delivery_error` / `gate_delivery_samples`: 1 s last-cell pairing,
    used by MCP, the workbench and the digest.
  - `lambda_delivery.delivery_samples`: acquisition-paired, used only by
    `scripts/lambda_delivery_evidence.py`.

  Should they converge on the acquisition-paired `match_ordered`? The numbers
  will shift. Record the answer here. If yes, it is a separate **L** item.

  **Done 2026-09-25 except the operator decision.** `_cam_tracking` pairs
  with `align_pairs` (`CAM_PAIRING` = 250 ms) for the per-pair error and
  reads `compare_channels` for slope/`scale_verdict` (or its refusal reason),
  and says which empty (`not_captured` / `refused`). `tuning.scale_stability`,
  `ScaleStability` and its two constants are deleted. Cam section before →
  after on `sessions-0917/_capture-tuning-hybrid-research-2026-09-17T184821`:
  before `n 1223` (1 s cells), mean err 0.07, max 28.5, `correlation 0.9591`;
  after `n_pairs 2393` (unmatched 8/30), mean err 0.15, p95 |err| 6.5, max
  34.5, fit r² 0.9151 slope 0.9347 offset 1.402 drift 4.09 % `drifting`.
  On `_capture-tuning-2026-09-2{1T232153,2T170459,3T164007}` (no cam DIDs)
  the digest's `cam` is `None` both at HEAD and now: the `cam_tracking`
  capability gate skips the section before `_cam_tracking` runs. Calling
  `_cam_tracking` directly on them returns `not_captured` naming the
  channel. That kind comes from the refusal's type
  (`pairing.InputNotCapturedError`, raised by `compare._refuse_if_absent`),
  not from its message text. Tests:
  `tests/test_tuning_digest_cam.py`; `tests/test_analysis_ground_truth.py`
  green.
  **Operator decision 2026-09-25 (R00, delegated):** yes, converge delivery
  error onto the acquisition-paired `match_ordered` engine; the numbers will
  shift. That is the separate **L** item **R05.16**; R05.3's own scope is done.

- [x] **R05.4 Declare the anomaly signals as data.**
  - `derived.compute_anomaly_signals` is 243 lines, ~105 of them docstring.
    The body repeats one block eight times:
    `a = _series(X); b = _series(Y); if both: out[n] = f(a, b);
    _append_dependency_gap(out, n, (X,), (Y,))`, restating the inputs each
    time.
  - `DERIVED_CHANNEL_INPUTS` covers only 4 channels (verified 2026-09-24:
    `AIR_MASS_PER_CYCLE` and three VE channels).
  - `collection/capture_profiles._channel_satisfied` treats an unmapped
    derived channel as satisfied. So `model_input_warnings` can never warn
    for the models behind 9 of the 19 catalog recipes: `total_trim` ×2,
    `stft/ltft_bank_balance` ×6, `afr_bank_balance`.
  - Nissan DID names (`did:ECM_AF_SENSOR_V_B1/2`,
    `did:ECM_THROTTLE_POS_SENSOR_1`) are hard-coded in shared code.

  **Do:** add `ANOMALY_SIGNALS: tuple[DerivedSignal(name, input_groups,
  compute), ...]`. The function loops over it, `DERIVED_CHANNEL_INPUTS` is
  derived from the table, and the DID-named inputs come from the vehicle
  profile.

  **Accept:**
  - A failing test first: a profile missing the STFT inputs warns for the
    bank-balance models.
  - Output columns are bit-identical on a fixture: IF/LSTM manifests consume
    these names.
  - `test_derived`, `test_features_batched_parity` and
    `test_reconcile_feature_frame_parity` pass.

  Serve path. Size **M**.

  **Done 2026-09-25 except the profile-owned DID names.**
  `derived.ANOMALY_SIGNALS: tuple[DerivedSignal(name, input_groups,
  compute)]`; `compute_anomaly_signals` loops over it and
  `DERIVED_CHANNEL_INPUTS` spreads the table (H01's 38 entries unchanged,
  order included). Bit-identical: `compute_anomaly_signals` output
  `assert_frame_equal(check_exact=True)` + identical column order on two
  synthetic all-input frames, the ground-truth drive slice,
  `2026-09-23T003917` and `_capture-tuning-hybrid-research-2026-09-17T184821`,
  each with and without `vehicle` (all 12 signals exercised). Warn test
  `test_a_profile_missing_the_stft_inputs_warns_for_the_bank_balance_models`
  (tests/test_anomaly_signals_table.py) passes — H01's registry coverage
  had already fixed the mechanism, so it did not fail first here. `test_derived`,
  `test_features_batched_parity`, `test_reconcile_feature_frame_parity`,
  `test_trainability`, `test_capture_profiles` pass. **Remaining:** the
  `did:ECM_AF_SENSOR_V_B1/2` and `did:ECM_THROTTLE_POS_SENSOR_1` inputs are
  still named in the shared table. Sourcing them from the profile needs a
  profile field threaded through `compute_anomaly_signals`' callers in
  `src/preprocessing/**` (R04-held), `capture_profiles._channel_satisfied`
  (R02-held) and `src/config.py` (R07-held).
  *Remainder done 2026-09-25.* `derived.py` and `afr.py` name no `did:`
  identifier (`inspect.getsource` check in the test below). Shared formulas
  over vehicle-specific inputs are `derived.VEHICLE_INPUT_SIGNALS`
  (`AFR_BANK_BALANCE`, `TPS_VOLTAGE_PER_PCT`); the vehicle names its channels
  in `src/analysis/vehicle_signals/<profile module>.py` (`SIGNAL_INPUTS`, and
  `AF_SENSOR_VOLTAGE` per bank for `afr.append_afr_channels`' voltage
  fallback and the `derived:AFR_*` registry entries); the Xterra's is
  `vehicle_signals/nissan_xterra_2008.py`, module name from the same
  `profiles.profile_module_stem(slug)` rule as R05.7's recipes.
  `ANOMALY_SIGNALS = SHARED_ANOMALY_SIGNALS + vehicle-input signals`.
  **Design choice:** `DERIVED_CHANNEL_INPUTS` is module-level and read without
  a config (trainability, capture-profile warnings, the evaluator), so the
  vehicle modules are merged, not selected: each input group is the union of
  the vehicles' alternatives (module-name order), a vehicle may not invent an
  output or change a formula's arity (refused at import), and a signal is
  computed only where its inputs exist, so a vehicle's frames resolve to its
  own identifiers. Selecting by active profile instead would thread a profile
  through those three consumers and the serve-path featurizer. Bit-identical:
  `ANOMALY_SIGNALS` (name, input groups) and all 38 `DERIVED_CHANNEL_INPUTS`
  entries, order included, JSON-identical before vs after;
  `compute_anomaly_signals` on the all-input frame with and without `vehicle`,
  and `append_afr_channels` through the voltage fallback with the Xterra's
  `afr_calibration.yaml`, `assert_frame_equal(check_exact=True)` and identical
  column order. Tests (`tests/test_anomaly_signals_table.py`, 8 pass):
  `test_shared_derived_code_names_no_manufacturer_did`,
  `test_the_xterra_supplies_its_did_inputs_from_its_vehicle_module`,
  `test_a_second_vehicle_adds_alternatives_not_replacements`,
  `test_a_vehicle_cannot_invent_a_signal_or_change_its_arity`; `test_derived`,
  `test_features_batched_parity`, `test_reconcile_feature_frame_parity`,
  `test_trainability`, `test_capture_profiles`, `test_afr*`,
  `test_analysis_ground_truth` pass (190 passed).

- [x] **R05.5 Fingerprints must use the real featurization.**
  - `fingerprint.compute_fingerprint` hand-chains `resample_and_segment →
    compute_anomaly_signals → append_committee_residuals →
    _append_behavior_deviations`.
  - `feature_pipeline.featurize_wide_frame` also runs `append_afr_channels`
    and `compute_instantaneous_channels`, and uses `segment_or_constant`.
  - So fingerprint deviation statistics come from a frame that differs from
    the one the models serve on. The module docstring claims "the same
    derived-signal appenders". `session_deviations` already does this right.

  **Do:** use `featurize_wide_frame(...).operating_frame` and bump
  `FINGERPRINT_VERSION`. **Operator decision:** trend alerts compare against
  a frozen reference epoch, so this needs a re-freeze. Record when. Size
  **S** for the code, **M** with the epoch.

  **Code done 2026-09-25; re-freeze timing is the operator's.**
  `compute_fingerprint` runs `featurize_wide_frame(frame, config,
  behavior_models=...).operating_frame`, keeps the classifier's mode labels
  for the idle/cruise stats (the models' segmenter falls back to one
  constant mode when throttle is absent, where the classifier still labels
  idle), and `FINGERPRINT_VERSION` is `0.2.0`. Measured against 0.1.0 on six
  real sessions (`2026-05-18T021421`, `2026-05-18T032933`,
  `2026-05-24T190027` — three of the five epoch sessions —
  `2026-09-21T010459`, `2026-09-23T003917`,
  `_capture-tuning-hybrid-research-2026-09-17T184821`): **0 stats changed**
  (16/27/32/53/34/15 stats each), so the frozen epoch
  (`data/control/nissan-xterra-2008/reference_epoch.json`, 21 stats, frozen
  2026-07-20) is not silently invalidated and no trend alert fires from the
  bump; it only makes 0.1.0 sidecars stale for recompute. Tests
  `tests/test_fingerprint_featurization.py`, `test_fingerprint*.py`,
  `test_digest.py`, `test_control_trends.py`.
  **Operator decision 2026-09-25 (R00, delegated):** no forced re-freeze;
  re-freeze the trend reference at the next deliberate corpus boundary.

- [x] **R05.6 `session_deviations.compare_sessions` compares aggregates.**
  - `_compare_surface` reports `mean_actual_delta` / `mean_expected_delta`
    over whole sessions. That is composition-dependent: the "K responds 1:1"
    shape.
  - Per-cell deltas have no minimum count and no noise floor.
  - It is exposed through the MCP `tools/behavior.py`.
  - It shares its name with `tuning.compare_sessions`, which has the gates.

  **Do:** drop or cell-match the surface-level means, add `min_n`, use
  R05.2's significance, and rename one of the two functions. **Accept:** a
  failing test first: two sessions with the same per-cell behaviour but a
  different rpm mix report no surface-level difference. Size **S–M**.

  **Done 2026-09-25.** Renamed to
  `session_deviations.compare_session_deviations` (callers: MCP
  `tools/behavior.py`, `scripts/analyze_session.py`, tests). The
  surface-level `mean_actual/expected/deviation_delta` fields are gone;
  `matched_deviation_delta` is the unweighted mean of per-cell deltas over
  cells with `min_n` (default 5) rows on each side, sparse cells carry
  `sparse: true` and no delta, and 1D surfaces are cell-binned too
  (`ONE_D_COMPARISON_CELLS = 10` over the axis domain). Optional
  `noise_floor` + `conditions` route through R05.2's `significance`
  (`significance` field). Failing-first:
  `test_same_per_cell_behaviour_with_a_different_rpm_mix_is_no_difference`
  (old whole-session delta 3.27, cell-matched 0.0) and
  `test_a_deviation_floor_measured_elsewhere_is_refused`
  (tests/test_session_deviations.py, 13 passed). `docs/mcp-server.md`
  updated.

- [x] **R05.7 Collapse the recipe-catalog clones (`refit.default_recipes`,
  574 lines).**
  - The per-recipe minimums can't be set. The trailing loop runs
    `replace(recipe, min_sessions=3, min_cell_sessions=2,
    min_cell_dwell_seconds=10.0)` for every 2D recipe and `min_sessions=3`
    for the rest (verified 2026-09-24), silently overwriting any value a
    recipe declares.
  - The July "warmup clone" is still there: `coolant_warmup`,
    `cold_start_idle_rpm_vs_coolant` and `cat_warmup` share 7 identical
    fields.
  - The rpm × load 2D axis block (7 fields) appears 6 times, plus the
    `total_trim` comprehension.
  - `DeviationConfig(kind="sigma", warning_band=3.0, critical_band=6.0, …)`
    appears 13 times.
  - The cam primary/secondary pair is a clone, and so are the STFT/LTFT
    comprehensions.
  - 18 `did:ECM_*` names sit in the shared catalog.

  **Do:**
  - First, a golden test over `recipe_contract_fingerprint(r)` + `asdict(r)`
    for all 19 recipes.
  - Then add `_warmup_1d(**kw)`, `_rpm_load_2d(**kw)`, `_sigma(floor)` and
    `_bank_balance(kind, mode)`.
  - Fold the trailing overrides into family defaults, so a value set on a
    recipe wins.

  **Accept:** the golden test is unchanged, so no contract change reaches the
  Pi. **Operator decision:** should the DID-named recipes move into a
  per-profile Python module? Record the answer. Size **M**.

  **Done 2026-09-25 except the operator decision.** Golden test first:
  `tests/test_recipe_catalog_golden.py` pins
  `recipe_contract_fingerprint` + `repr(asdict(r))` + type for all **24**
  catalog recipes and their order (`tests/fixtures/recipe_catalog_golden.json`,
  generated from the untouched catalog). Then `_warmup_1d`, `_rpm_load_2d`,
  `_sigma(floor)` (13 sites), `_bank_balance(kind, mode)`,
  `_cam_tracking_2d(role, output=)`, and `_surface_1d/_surface_2d` family
  factories apply `CATALOG_1D_DEFAULTS` / `CATALOG_2D_DEFAULTS` *under* a
  recipe's own fields — the trailing `replace` loop is gone.
  Golden test unchanged after the refactor (no `contract_changed:recipe`
  reaches the Pi); `test_a_minimum_a_recipe_declares_wins_over_the_family_default`
  proves per-recipe minimums now stick.
  **Operator decision 2026-09-25 (R00, delegated):** move the DID-named recipes
  into a per-profile Python module. *Done 2026-09-25.*
  `refit.shared_recipes()` is the vehicle-neutral catalog (18 recipes; no
  `did:` in any output, axis or rate-of-change key); the Xterra's six
  (`air_charge_vs_rpm_throttle_did`, `cam_primary/secondary_vs_command_rpm_did`
  with `_cam_tracking_2d`, `afr_bank_balance_robust` — its input is the
  AF-voltage DID pair — `afr_voltage_vs_rpm_throttle_did`,
  `tps_sensor_1_vs_sensor_2_did`) moved verbatim to
  `src/analysis/recipes/nissan_xterra_2008.py::recipes()`.
  `refit.default_recipes(profile)` takes the slug (**required**, so no caller
  silently fits another vehicle's surfaces, or none; `None` = shared only) and
  appends `recipes.profile_recipes(slug)`, found by
  `profiles.profile_module_stem(slug)`; a profile with no module fits the
  shared catalog, and a profile recipe shadowing a shared name is refused.
  Callers pass the resolved profile's slug: `scripts/refit_behavior_models.py`,
  `scripts/inventory_behavior_inputs.py`, `scripts/evaluate_behavior_models.py`
  (`evaluate`/`export_columns`/`export_rows` take `profile`). The golden test
  `tests/test_recipe_catalog_golden.py` is **unchanged** for
  `default_recipes("nissan-xterra-2008")` (same 24 fingerprints, `asdict`
  reprs and order). `tests/test_profile_recipes.py` (5 pass): shared catalog
  names no DID; the Xterra's six come from its module; a second profile's
  module is used and never gets the Xterra's; a slug with no module (a VIN
  slug) gets the shared catalog; shadowing refused. `test_refit`,
  `test_behavior_*`, `test_refit_behavior_models_script`,
  `test_evaluate_behavior_models_script` pass.

## Structure

- [x] **R05.8 Decompose `refit.fit_recipe` (426 lines).**
  - The July "clean" verdict no longer holds. The function has 24
    `return RecipeResult(recipe.name, None, skipped=True, reason=…)` and 4
    separate `SESSION_COLUMN not in sub.columns` checks.
  - Its phases: capture allowlist; contract-column refusals; context appends;
    session selection (events, runtime continuity); per-row contract;
    drive-grouped folds (~90 lines); fit and validate; publish gate; no-op
    version suppression.

  **Do:** add `_RecipeSkip(reason)`, raised by phase helpers and caught
  once. The helpers: `_authorize_captures`, `_refuse_unenforceable_contract`,
  `_select_training_sessions`, `_eligible_rows`, `_plan_folds -> FoldPlan`
  and `_keep_prior_version`. Check the session-id precondition once.
  **Accept:** 47 `result.reason` assertions pin the text, so keep the strings
  byte-identical. `test_refit.py` and the B02 tests pass. Offline only
  (`scripts/refit_behavior_models.py`). Size **M**.

  **Done 2026-09-25.** `_RecipeSkipError(reason, candidate=)` (ruff N818
  wants the `Error` suffix) is raised by `_authorize_captures`,
  `_refuse_unenforceable_contract`, `_select_event_sessions`,
  `_refuse_missing_columns`, `_append_contexts`,
  `_select_contiguous_runtime`, `_eligible_rows`, `_plan_folds -> FoldPlan`,
  the fit/validate/publish tail, and `_keep_prior_version`; `fit_recipe`
  catches it once. Session-id preconditions go through one
  `_require_sessions(frame, reason)`; each phase keeps its own byte-identical
  reason string, in the original check order. `test_refit.py`,
  `test_refit_event_gating.py`, `test_refit_behavior_models_script.py`,
  `test_behavior_validation*.py`, `test_behavior_serving_contracts.py`,
  `test_behavior_lifecycle.py`, `test_behavior_trim_contracts.py`,
  `test_evaluate_behavior_models_script.py` (incl. slow): 190 passed,
  1 xfailed. Golden catalog unchanged.

- [x] **R05.9 One byte-slice / scale / plausibility layer in `src/decode/`.**
  *Serve path.*
  - Raw extraction has four copies: `bindings.decode_raw`,
    `dids.decode_did_raw`, `dids.decode_memory_raw`, and
    `preprocessing/live._decode_signal_raw`, which R04.4 deletes.
    `sensor_can.py` is the only one that supports `signed`, and it uses
    different field names (`endianness`, `valid_min`).
  - Linear scaling has four copies: bindings `scaling_factors`, dids
    `apply_scaling`, dids memory scaling, sensor_can.
  - `is_plausible` exists twice, AST-identical.
  - The two `Evidence` models have drifted. `dids.Evidence` refuses
    `constant` without `conditions`/`constant_unit`; `bindings.Evidence`
    lacks the fields.
  - `Signal`, `DidEntry` and `MemoryChannel` repeat 9 fields.

  **Do:** create `src/decode/slices.py` with `ByteSlice`, `LinearScale` and
  `PlausibleRange` mixins plus one `Evidence`. Bindings gain the refusal.
  **Accept:** the ground-truth suite's `catalog_pins` pass, and
  `data/bindings.yaml` / `data/dids.yaml` load unchanged. If an existing
  binding violates the stricter `Evidence`, list it here and ask; don't
  weaken the rule. Size **M**.

  **Bindings and sensor slices done 2026-09-25; the DID slice waits.**
  New `src/decode/slices.py`: `slice_raw` (with `signed`), `linear_factors`
  / `linear_value`, `in_range`, method-only mixins `ByteSlice` /
  `LinearScale` / `PlausibleRange` (annotations under `TYPE_CHECKING` so
  pydantic field order is unchanged) and the one `Evidence` (the stricter
  dids rule). `bindings.Signal` uses the mixins and `bindings.Evidence` *is*
  `slices.Evidence` (bindings gain the constant/conditions refusal);
  `sensor_can.decode_frame` uses `slice_raw` + `in_range`. Bit-identity:
  `decode_raw` / `apply_scaling` / `is_plausible` / `scaling_factors` equal
  to HEAD over all 39 bindings × 300 random payloads (7,138 decodes).
  `data/bindings.yaml` (39 signals) and `data/dids.yaml` (27 DIDs, 11 memory)
  load unchanged; **no existing binding violates the stricter Evidence**.
  `tests/test_decode_slices.py`; ground-truth `catalog_pins`, `test_did_decode`,
  `test_sensor_can`, `test_bindings_*`, `test_emit_dbc`, `test_frame_decoder`
  pass (200). **Remaining:** `dids.decode_did_raw`, `dids.decode_memory_raw`,
  `dids.apply_scaling`, the memory scaling, `dids.is_plausible` and
  `dids.Evidence` onto `slices` — `src/decode/dids.py` is held by the human
  session with uncommitted work. (`preprocessing/live._decode_signal_raw` is
  R04.4's.)
  *Remainder done 2026-09-25.* `DidEntry` and `MemoryChannel` take the
  `ByteSlice`/`LinearScale`/`PlausibleRange` mixins (fields and their order
  unchanged); `decode_did_raw`, `decode_memory_raw`, `apply_scaling`,
  `apply_memory_scaling` (sentinel check, then `scaled`), `is_plausible` and
  `decode_memory_window` (`slices.slice_raw` at the window offset) are one-line
  delegations; `dids.Evidence` *is* `slices.Evidence`, so the three catalogs
  share one evidence record. `live._decode_signal_raw` is gone (R04.4).
  Bit-identity over a real session: every `DID_` row (23,489) and `mem:` row
  (6,268) of `sessions-0921c/_capture-tuning-2026-09-21T232153`, decoded with
  HEAD's and this tree's `src/decode/dids.py` against `data/dids.yaml` —
  `decode_raw_did_reading`, then each of the 27 DIDs' raw/scaled/plausible
  over each payload and each of the 11 memory channels'
  window/raw/scaled/sentinel over each window reply — 726,640 results,
  identical (`==` on the pickled lists; 563,736 scaled DID values, 40,360
  scaled memory values, 382 sentinels). `data/dids.yaml` (27 DIDs, 11 memory)
  and `data/bindings.yaml` load. `test_did_decode.py`, `test_decode_slices.py`,
  `test_memory_channels.py`, ground-truth `catalog_pins` pass.

- [x] **R05.10 Move surface evaluation into decode; one bin rule.**
  - The serve-path evaluators (`evaluate_1d/2d`,
    `containing_cell_indices`, `trusted_cell_mask`) live in the analysis
    *fitting* modules, which is why `decode/behavior_decoder` imports
    analysis.
  - Bin helpers are duplicated:
    - `surface_2d._digitize_into_bins` ≡ `binned_analysis._bin_index`.
    - `surface_2d._bin_index` ≡ `session_deviations._bin_index`.
    - Inline copies in `surface_1d` and `behavior_validation`.
  - Reporting uses three `pd.cut` conventions: right-closed (tuning),
    `right=False` (workbench) and `include_lowest` (afr).
  - Latent bug: `tuning.compare_sessions` auto-edges with `linspace(lo, hi,
    7)` and no `include_lowest`, silently dropping the minimum sample.

  **Do:** create `src/decode/surface_eval.py` with a public `bin_indices`.
  Analysis re-exports it. **Accept:** a failing test first for the dropped
  minimum. The decode → analysis import is gone (R01.8's test). Size
  **S–M**.

  **Done 2026-09-25.** New `src/decode/surface_eval.py`: `evaluate_1d`,
  `evaluate_2d`, `cell_eligible`, `trusted_cell_mask`,
  `containing_cell_indices` and the one bin rule `bin_indices` / `bin_index`;
  `surface_1d`/`surface_2d` and `binned_analysis` re-export them.
  `surface_2d._digitize_into_bins`/`_bin_index`, `binned_analysis._bin_index`,
  `session_deviations._bin_index` and the inline copies in the 1D fitter
  and `behavior_validation.envelope_coverage` all call `bin_indices`.
  `decode/behavior_decoder` now imports `decode.surface_eval`; the two
  `OWED` allowances in `tests/test_layering.py` are deleted
  (`test_allowances_are_not_stale` green). Serve path bit-identical: HEAD vs
  new `evaluate_2d`/`containing_cell_indices`/`trusted_cell_mask`/`evaluate_1d`
  over all 12 shipped behavior models (16,055 2D + 8,008 1D evaluations,
  edges included). Failing-first `test_auto_edges_keep_the_minimum_sample`
  (failed on the `linspace` + right-closed `pd.cut` cells, passes on
  `bin_indices`); `test_explicit_edges_still_exclude_out_of_range_samples`.
  `tuning.compare_sessions` cell labels now read `[lo, hi)` / last `[lo, hi]`.
  The WOT-band / delivery-band reporting `pd.cut` conventions are unchanged
  (label-visible; not asked for here).

- [x] **R05.11 Give `GateError` structure.**
  - `workbench._missing_from_gate_error` string-splits the message on "Empty
    in this session: ", " This session" and " Use a Mode 01".
  - `workbench` decides `not_captured` by `"never captured" in str(exc)`.
  - `tuning._refuse_missing_inputs` hand-reads `capture_profile.json`
    instead of `read_capture_manifest`, and bakes an Xterra fact into a
    shared message: "Mode 01 cannot run alongside Mode 22 on this ECM… PID
    0x44".

  **Do:** add `InputsNotCaptured(GateError)` with `kind` and `missing:
  tuple[str, ...]`, read the manifest through its reader, and take the
  vehicle sentence from the profile. **Accept:** no `str(exc)` parsing
  remains in `workbench.py`. Size **S**.

  **Done 2026-09-25.** `tuning.InputsNotCapturedError(GateError)` with
  `kind` (`not_captured` / `not_co_observed`) and `missing` (spec names);
  `_refuse_missing_inputs` reads the manifest through
  `read_capture_manifest`, and the ECM-specific sentence moved to profile
  data: `mode01_exclusion_note` in `data/nissan-xterra-2008/tuning_providers.yaml`
  (`ProviderRegistry.mode01_exclusion_note`, `TuningContext.mode01_exclusion_note`),
  passed by the digest, MCP, report CLI and workbench. `workbench.py` has no
  `str(exc)` parsing left (`_missing_from_gate_error` deleted; the
  `"never captured" in str(exc)` test is gone). Test
  `test_uncaptured_inputs_raise_a_structured_refusal`.

- [x] **R05.12 Split the remaining giant functions.**
  - `tuning_digest.compute_tuning_digest` (284 lines, serve path through the
    trip-end digest) → `_section_wot`, `_rates`, `_coverage_notes`,
    `_capture_quality_notes`, `_delivery` (3-way branch), `_previous` and
    `_cam`, each returning `(payload, notes)`. R05.1 absorbs its 20-line
    context loading. `digest._tuning_section`'s catch-all returns `None` for
    both "not a capture" and "crashed"; make it say which.
  - `workbench.pooled_delivery_error` (150 lines): after R05.11, extract
    `_session_delivery_frame -> DataFrame | Unavailable`.
  - `lambda_delivery.delivery_samples` (181 lines) → `_target_side`,
    `_measured_side`, `_steady_target_mask` (a pure O(n·dwell) Python loop:
    make it testable, then vectorize it) and `_assign_states`.

  **Accept:** digest JSON is byte-identical on a fixture capture, and the
  lambda-delivery evidence script's output is unchanged. Size **M**.

  **Done 2026-09-25.** `compute_tuning_digest` is `_digest_inputs` (R05.1's
  context) + `_section_wot`, `_rates`, `_delivery` (3 branches),
  `_coverage_notes`, `_capture_quality_notes`, `_previous`, `_cam`, each
  returning payload and/or notes in the original note order.
  `digest._tuning_section` returns `{"available": False, "kind": "failed",
  "error": ...}` on a crash instead of the `None` an ordinary drive gets
  (`test_a_crashed_tuning_section_says_so_rather_than_reading_as_no_capture`).
  `workbench._session_delivery_frame -> DataFrame | Unavailable` extracted.
  `lambda_delivery.delivery_samples` → `_target_side`, `_measured_side`,
  `_steady_target_mask` (vectorized: prefix sums + sparse-table range
  min/max; equal to the old walk on 12 randomized traces incl. cuts, NaNs,
  duplicate stamps, gaps — `tests/test_lambda_delivery_steady_mask.py`),
  `_seconds_since_cut`, `_assign_states`. Acceptance:
  `scripts/lambda_delivery_evidence.py` output byte-identical (`cmp`) on
  `_capture-tuning-2026-09-21T232153` + `_capture-tuning-2026-09-23T164007`;
  `compute_tuning_digest(...).to_dict()` JSON byte-identical (before the split vs after R05.12+R05.13) on `_capture-tuning-2026-09-23T164007` (with a previous-capture delta and sigma), `_capture-tuning-2026-09-22T170459`, `_capture-tuning-hybrid-research-2026-09-17T184821` (cam) and `_capture-tuning-hybrid-research-2026-08-07T170918`.

- [x] **R05.13 `behavior_validation.py` (1,811 lines): move the splits
  beside `drives`, and move vehicle facts out of code.**
  - ~300 lines of drive grouping and splits keep their own `Drive` beside
    `drives.PhysicalDrive`, and re-export `parent_session`/`SEGMENT_MARKER`.
  - `group_drives`' directory-grouping fallback contradicts `drives.py`'s
    "Recordings are not drives" rule.
  - The WOT floors are in code: `tuning_digest.WOT_RPM_FLOOR = 2500` and
    `WOT_LOAD_FLOOR = 85`. `WotCriterion` says they are "never in code", and
    the MCP `min_load=85.0` repeats the value.
  - `DEFAULT_RPM_BANDS` tops out at 6500, the VQ40 redline.

  **Do:**
  - Move the splits to `analysis/splits.py` over `drives.PhysicalDrive`, and
    remove the fallback or name why it exists.
  - Move the WOT floors and the rpm-band ceiling into the vehicle profile.
  - Optionally, a facade split of the rest into schema / evaluate / metrics /
    sensitivity / criteria / evidence (14 importers).

  Size **M**.

  **Partly done 2026-09-25.** The drive grouping and grouped splits moved
  verbatim to `src/analysis/splits.py` (`Drive`, `PieceRanges`,
  `piece_time_ranges`, `group_drives`, `DriveSplit`, `chronological_split`,
  `explicit_split`, `training_leaks`, the two errors,
  `DEFAULT_JOIN_GAP_SECONDS`); `behavior_validation` re-exports them. The
  directory-grouping fallback is kept and its reason is now in
  `group_drives`' docstring (ranges with no rows to group on: frames without
  a `timestamp` column, and the bare-range callers). WOT floors and the rpm
  bands (6500 rpm redline) moved to profile data:
  `tuning_providers.yaml` `mode01_wot {load_floor_pct, rpm_floor,
  rpm_bands}` (`LoadWotCriterion`); `tuning.DEFAULT_RPM_BANDS`,
  `tuning_digest.WOT_LOAD_FLOOR/WOT_RPM_FLOOR` are gone and `rpm_bands` /
  `min_load` are required on `delivery_error`, `delivery_semantic_frame`,
  `delivery_error_from_semantic_frame`, `gate_delivery_samples`,
  `delivery_error_from_gated`. Tests `tests/test_wot_profile_facts.py`.
  **Remaining:** (1) the splits still use their own `Drive`, not
  `drives.PhysicalDrive` — the types carry different things (frame pieces
  vs recordings + spans) and every evidence record (`DriveRecord`) is built
  from `Drive`; converting is its own change; (2) the MCP
  `get_delivery_error(min_load=85.0)` and `tuning_report.py --min-load 85`
  defaults still repeat the floor — making them profile-driven changes the
  MCP tool contract; (3) the digest payload key `rpm_over_2500_seconds` still
  names the dev vehicle's floor (now counted against the profile's); (4) the
  optional facade split of `behavior_validation` was not attempted.
  *Remainders 2026-09-25.* (1) Kept `splits.Drive`, reason now in its
  docstring: a split's unit is the frame *piece* (`<session>#<segment>`),
  which `PhysicalDrive` (recordings + activity spans) has no field for, and
  every fold and `DriveRecord` names pieces; which pieces share a drive is
  still `drives`' decision (`row_drive_labels` / `join_spans`), so it is one
  drive definition with two views. (2) **Operator decision 2026-09-25 (R00,
  delegated):** MCP `get_delivery_error(min_load=None)` and
  `tuning_report.py delivery-error --min-load` (default `None`) resolve to the
  profile's `mode01_wot.load_floor_pct` through
  `TuningContext.resolve_min_load` (refuses when the profile has no
  `mode01_wot`); the MCP payload echoes `min_load` + `min_load_source`
  (`profile`/`caller`). `docs/mcp-server.md` and the tool docstring say so;
  `tests/fixtures/mcp_tool_snapshot.json`'s `get_delivery_error` entry
  regenerated (description + `min_load` default `null`, that entry only).
  Tests: `test_wot_profile_facts.py::test_min_load_defaults_to_the_profiles_wot_floor`,
  `test_tuning_context.py::test_a_dashboard_flash_resolves_in_the_mcp_tuning_tools`
  (`(85.0, "profile")`), `test_mcp_tool_snapshot.py`. (3) The digest's
  `pull_inventory.rpm_over_2500_seconds` is now `rpm_over_floor_seconds` with
  the `rpm_floor` it counted against beside it. No alias: nothing reads the
  tuning section of a stored digest back (grep of `src/`, `scripts/`,
  `tools/`, the dashboard TS and MCP found only the writer and
  `tests/test_tuning_digest.py`), and old `digest.json` sidecars keep the old
  key as written. (4) The optional facade split is still not attempted.

- [x] **R05.14 Small cleanups (batch them).**
  - `workbench.session_span` re-walks parquet footers that
    `loader.count_session_rows` / `session_time_span_seconds` already walk.
  - `_hash_xy`/`_hash_xyz` and `_now_iso` are each defined twice.
  - `decode/behavior_models.NoiseFloor` collides with
    `analysis.noise_floor.NoiseFloor`; rename the former.
  - R01.3, R01.4 and R01.5 cover the coercion, column and sidecar helpers
    found here.

  Size **S**.

  **Partly done 2026-09-25.** `_hash_xy`/`_hash_xyz`/`_now_iso` → one
  `src/analysis/fit_provenance.py` (`training_data_hash(*columns)`,
  `fitted_now()`; identical hashes). `decode/behavior_models.NoiseFloor` is
  now `ResolutionFloor` with a `NoiseFloor` alias for existing imports.
  **Remaining:** `workbench.session_span` still walks Parquet footers itself;
  folding it onto the loader needs a (lo, hi, rows) footer reader in
  `src/preprocessing/loader.py` (R04-held).
  *Remainder done 2026-09-25.* `loader.session_footer_span(session_dir) ->
  SessionFooterSpan(first, last, rows)` is the one footer walk (row counts,
  timestamp min/max normalised to UTC, truncated/0-byte chunks skipped);
  `count_session_rows` and `session_time_span_seconds` read it, and
  `workbench.session_span` is a three-line adapter over it (its `_utc` copy is
  gone). Before/after over every session under `~/canary-data` (268 dirs):
  `count_session_rows`, `session_time_span_seconds` and `session_span`
  identical to HEAD's for all 268. `test_workbench_analysis.py`,
  `test_preprocessing_loader.py`, `test_replay*.py`, `test_trip_end_digest.py`
  pass.

- [x] **R05.15 The operational calibration directory is shared by every
  profile.** *Pre-existing; found during R05.1, not fixed.*
  - `profiles.operational_dir(config)` is `config.daemon.jobs_dir.parent /
    "calibration"`.
  - `scope_config` scopes `jobs_dir` to `<jobs>/<slug>`, so for a scoped
    config its parent is the unscoped `jobs` directory. On the Pi that is
    `/data/canary/control/jobs/calibration`.
  - That one directory, inside `jobs/`, holds the operational
    `tune_revisions.yaml` (`append_flash`), `dids.yaml` and
    `memory_verify.json` for **every** profile.
  - A second vehicle's dashboard-recorded flash would land in the Xterra's
    operational registry, and `active_registry_path` would then serve it to
    both vehicles.
  - An unscoped config resolves elsewhere (`data/control/calibration`), so
    tooling that forgets to scope reads a different file again.
  - Fix direction (undecided): a per-profile directory, e.g. scoped like the
    other `daemon.*` paths. It needs a migration for the existing Pi file.

  *Done 2026-09-25.* New `DaemonConfig.calibration_dir` (default
  `CONTROL_DIR / "calibration"`); `operational_dir(config)` reads it when set
  (YAML or `scope_config`) and otherwise stays beside `jobs_dir`, resolved at
  call time so a `model_copy` of `jobs_dir` moves it too. `scope_config` sets
  it to `<that>/<slug>`: on the Pi (`jobs_dir: /data/canary/control/jobs`)
  the Xterra's is `/data/canary/control/calibration/nissan-xterra-2008`, still
  under the data root. **Migration** (reworked after verification: the
  first version adopted by registry prefix only and could orphan the
  Xterra's files silently). `profiles.legacy_adoption(config)` decides
  whether a profile owns the old shared `<jobs>/calibration`, first match
  wins: (1) its own `operational_dir` holds a `migrated_from.json` marker
  naming that directory; (2) the legacy `tune_revisions.yaml` starts with the
  same revision (name + calibration id) as the profile's tracked registry,
  so a flash later hand-added to the tracked file does not break the match;
  (3) the legacy dir has no registry (a dashboard memory *verify* writes only
  `dids.yaml` + `memory_verify.json`) and the profile is the only one in
  `profiles.yaml`. `resolve_active_profile` (the daemon's startup path) now
  calls `migrate_legacy_calibration` on the scoped config: adopted files are
  copied once into the per-vehicle dir (own files win, legacy left intact)
  and the marker is written, so later tracked-registry edits or a second
  registered vehicle cannot orphan them. A failure there is logged, not
  fatal. Offline tools (`resolve_profile_for_tooling`) never migrate. They
  read an adopted legacy file through `profiles.operational_file` (own copy,
  else the adopted legacy copy), which `active_registry_path`,
  `active_memory_path` and the verify record use; the first write through
  `src.control.calibration` also seeds from it. **Never silent:** when
  `<jobs>/calibration` exists and no profile has adopted it (no sibling
  marker names it), the migration logs a WARNING (once per reason) and
  `calibration_status` (flattened into `/status`) carries
  `legacy_calibration_unadopted` with the path and the reason; otherwise
  `None`. Tests (`tests/test_operational_calibration_scope.py`, 11 pass):
  per-profile dirs; Pi paths under `/data/canary`; a second profile's flash
  does not land in the Xterra's registry; the Xterra's shared-location
  registry is still read (and not by another profile); the first flash
  migrates it and leaves the legacy file byte-identical; a legacy registry
  whose history is not the profile's is not adopted; legacy
  `dids.yaml`/`memory_verify.json` read and migrated;
  `test_a_verify_only_legacy_dir_is_migrated_for_the_only_registered_profile`
  (rule 3 at `resolve_active_profile`, marker written, verify outcomes
  survive, and a second vehicle registering afterwards does not un-adopt
  it); `test_an_unowned_legacy_dir_warns_and_is_surfaced` (two registered
  profiles: WARNING logged, `legacy_calibration_unadopted` names the dir);
  `test_a_hand_added_tracked_flash_does_not_orphan_dashboard_flashes`
  (case (b), plus a later unrelated tracked rewrite);
  `test_offline_tools_read_the_legacy_dir_without_migrating`.
  `test_profiles.py`'s every-daemon-path test now covers `Path | None`
  fields. **Not verified:** what the Pi's `/data/canary/control/jobs/calibration/`
  holds, and how many profiles its `profiles.yaml` registers (offline only).
  A registry-less legacy dir on a Pi with more than one registered profile is
  reported, not adopted.

- [~] **R05.16 One delivery-error engine: the acquisition-paired
  `match_ordered`.** *Operator decision 2026-09-25 (R05.3): converge; numbers
  will shift.*
  - Two engines answer "how far is measured AFR from commanded":
    `tuning.delivery_error` / `gate_delivery_samples` pair on 1 s last-value
    cells (`read_aligned`, which says it is not pairing evidence), and
    `lambda_delivery.delivery_samples` pairs by acquisition order
    (`pairing.match_ordered`), used only by
    `scripts/lambda_delivery_evidence.py`.
  - Consumers of the 1 s engine: the trip-end / tuning digest
    (`tuning_digest`), the MCP `get_delivery_error` and
    `compare_tuning_sessions` tools (`src/mcp_server/tools/tuning_analysis.py`),
    `scripts/tuning_report.py`, and the workbench delivery frame
    (`workbench._session_delivery_frame`, dashboard Analysis page).

  **Do:** make `delivery_error` (and the semantic-frame / gated variants)
  build its samples from `lambda_delivery`'s acquisition-paired sides and
  steady-target mask, keep the WOT gates (`mode01_wot` profile facts), and
  delete the 1 s last-cell path. Bump whatever version the digest sidecar
  and the MCP payload carry so stored digests recompute.

  **Accept:** the ground-truth suite passes; before/after delivery error
  recorded on `_capture-tuning-2026-09-21T232153`,
  `_capture-tuning-2026-09-23T164007` and one hybrid-research capture (n,
  mean, p95 per band), with the shift explained; digest, MCP and workbench
  tests updated to the new numbers deliberately, not re-pinned blind.
  Size **L**.

  *Engine converged 2026-09-25; two remainders below (an operator decision
  and a digest recompute path).* `tuning.delivery_error` now reads the
  declared target/measured sources (`read_delivery_inputs`, one filtered
  read, acquisition clock), runs `lambda_delivery.delivery_samples`
  (`match_ordered` at the declaration's 250 ms, steady-target dwell,
  post-cut holdoff, regime validity, rails, censoring), attaches rpm /
  `FUEL_STATUS` / load by the same one-to-one pairing, checks the clock basis
  (`check_clock_support`), then gates `not_enrichment`, `closed_loop` (Mode 01
  only), `below_min_load` and `outside_rpm_bands` (now `(lo, hi]`, matching
  the bins). Two routes, both declared: `mode01_route` (PID 0x44) and
  `semantic_route` (provider registry's `commanded_afr`/`rpm`/A/F role).
  `delivery_semantic_frame`, `delivery_error_from_semantic_frame`, the grid
  `gate_delivery_samples`, `cell_agg` (MCP + `--cell-agg`) are deleted; an
  undeclared measured spec refuses listing the declared ones; no
  `lambda_delivery.yaml` refuses. Payloads carry `engine_version: 2`, a
  `pairing` block and per-band `p05_pct`/`p95_pct`; the digest's tuning
  section carries `schema_version: 2`. Consumers switched: digest (`_delivery`,
  `_previous`), MCP `get_delivery_error`, `tuning_report.py delivery-error`,
  workbench `pooled_delivery_error` (dashboard Analysis page, `PairingNote`).
  `compare_tuning_sessions` never used the delivery engine (`compare_sessions`
  is a per-cell grid comparison) and is unchanged.

  **Before → after**, `obd:O2_WR_LAMBDA_S1`, profile rpm bands. MCP, report
  CLI and digest make the identical call (min_load 85 = profile floor); the
  workbench page defaults to no load floor.

  | capture | consumer | before (1 s last-cell) | after (engine 2) |
  |---|---|---|---|
  | `_capture-tuning-2026-09-21T232153` | MCP/CLI/digest | n 56, band-avg +2.90 %, pooled +2.73 % | n 0 |
  | | workbench | n 65, band-avg +1.97 % | n 0 (`gates_removed_everything`) |
  | `_capture-tuning-2026-09-23T164007` | MCP/CLI/digest | n 28, +2.99 %, pooled +2.87 % | n 0 |
  | | workbench | n 34, +3.29 % | n 0 |
  | `sessions-0917/_capture-tuning-hybrid-research-2026-09-17T184821` | MCP/CLI/digest | n 12, +1.09 %, pooled +0.50 % | n 0 |
  | | workbench | n 16, +0.56 % | n 0 |
  | `sessions/…/_tuning-2026-08-05T142720` (digest reference test) | digest | n 16 | n 1 |

  Before, per band (MCP call, n / mean / p95 %): 232153 — 2500-3000 8 / 4.99 /
  14.38, 3000-3500 18 / 2.33 / 3.62, 3500-4000 5 / 2.89 / 4.29, 4000-4500 13 /
  1.63 / 2.89, 4500-5000 7 / 3.66 / 5.03, 5000-6500 5 / 1.93 / 2.77; 164007 —
  1 / 2.34 / 2.34, 6 / 2.77 / 4.00, 6 / 3.49 / 5.12, 7 / 2.00 / 4.26, 6 / 2.96 /
  4.60, 2 / 4.35 / 5.70; hybrid — (3000-3500) 2 / 7.26 / 12.08, 1 / 2.22, 4 /
  -0.29 / 4.42, 3 / -2.15 / -0.96, 2 / -1.58 / -1.50. After: no band has a
  sample. Gates after (232153): started 5808 PID 0x44 reads (3.8 Hz, against
  1496 one-second cells), `fuel_cut` 712, `measured_missing` 1,
  `measured_rail` 78, `post_fuel_cut` 2111, `target_transient` 2364,
  `source_not_valid_in_regime` 465, `closed_loop` 77. 164007: 2747 /
  190 / 0 / 35 / 876 / 1080 / 560 / 6. Hybrid: 3920 / 201 / 313 / 18 / 840 /
  1825 / 723 / 0.

  **Why.** The pairing is not what moved: WOT reads pair within 0-25 ms
  median (max 30 / 48 / 229 ms), and with the declared dwell and holdoff set
  to 0 the same engine gives n 223 / 107 / 32 at band-avg +2.92 / +3.11 /
  +0.23 % against the old +2.90 / +2.99 / +1.09 % (n rises ~4x: every 3.8 Hz
  read, not one per second). The declared eligibility empties it. Of the WOT
  open-loop reads (`FUEL_STATUS` 4, load > 85: 235 / 110 / 32), 136 / 55 / 20
  fall inside the 15 s post-fuel-cut holdoff (a lift or shift in the manual
  truck cuts fuel just before the pull) and 99 / 55 / 12 sit on a target that
  moved more than the 0.005 λ step tolerance over the prior 3 s (median 0.13 /
  0.14 / 0.15 λ: the target ramps from stoich into enrichment as the pull
  starts, and keeps moving with rpm); 15 / 0 / 0 held for 3 s. Sensitivity:
  dwell 1 s → n 6 / 3 / 0; dwell 1 s and tolerance 0.02 λ → n 58 / 25 / 6,
  band-avg +2.51 / +3.41 / -0.94 %. Those constants were chosen on
  closed-loop / RAM-target (~1 Hz) data (`eligibility.informed_by`), not on
  WOT.

  Tests, updated to the new numbers deliberately: `tests/test_analysis_tuning.py`
  (closed-loop, open-loop gate, lean target → `target_invalid`, band-average,
  pairing block, `not_co_observed` at 600 ms skew, semantic route converts
  voltage and names a censored sample, semantic load floor refuses, no
  declaration refuses, undeclared measurement refuses, clock basis refuses,
  fuel cut + 15 s holdoff); `tests/test_lambda_delivery.py`
  (`test_delivery_error_uses_the_same_residual_as_the_relationship` now runs
  `gate_delivery_samples`); `tests/test_workbench_analysis.py` (dwell rows,
  `pairing`, `engine_version`); `tests/test_tuning_digest.py`
  (`schema_version` 2, n 7 of 10 with `target_transient` 3; reference capture
  16 → 1 with the breakdown); MCP snapshot regenerated (`cell_agg` gone, new
  description); CLI snapshot's `tuning_report.py` entry drops `--cell-agg`;
  `AnalysisPage.test.tsx` asserts the pairing note. Ground-truth suite green.

  **Operator decision 2026-09-25:** hold R05.16; measure the enrichment
  eligibility first (option (b) informed by (c)). Done the same day, desk
  only, then reworked after verification (per-route delays, per-pair
  support, a clean held-out set).

  *Schema.* `lambda_delivery.Eligibility.regimes` has `closed_loop` and
  `enrichment` (`RegimeRules`, both required), each a `RegimeEligibility`
  with required `conditions`, dwell, tolerance, holdoff, `informed_by` and
  `held_out` (no defaults; overlap refused), and `transport_delays`: one
  `RegimeDelay` **per commanded source**, fitted on its own route (the
  declaration refuses a missing or unknown source; a frozen delay needs
  `seconds`, `bracket_s` containing it, `fitted_on` ⊆ `informed_by`, and
  `method`). One global rule, or one delay for every source, is no longer
  representable. `delivery_samples` judges each read by its regime's rule and
  shifts only by its own source's delay. Tools: `estimate_transport_delay(
  regimes=…)`, `delay_bracket` (mean-residual width),
  `per_pair_delay_support` / `per_pair_bracket` / `sign_test_p` (read-by-read
  exact sign test).

  *Transport delay, per route* (reproduce: `scripts/lambda_delivery_evidence.py
  --fit-enrichment-delay <sessions>`, which scans each source on exactly the
  drives its declaration names; reproduced 2026-09-25):
  - **RAM target (`ram_final_target`): frozen 0.10 s, weakly supported.** Its
    reads have their own acquisition time (~110 ms after the 0x24 read,
    p10-p90 80-250 ms). On five 2026-09-20..21 drives, 161 enrichment reads:
    mean |residual| 4.28 % at 0 s, 3.56 % at 0.10 s, 3.60 % at 0.20 s, 4.65 %
    at 0.30 s (mean-residual 5 % bracket 0.05-0.20 s) — a comparison of
    means. Read by read, 0.10 s vs 0 s: 58 better / 41 worse / 62 identical,
    sign test p = 0.11; no candidate 0-0.5 s is distinguishable from 0.10 s,
    so the declared `bracket_s` is the **per-pair bracket 0.0-0.5 s**.
    Kept over 0 s because exhaust transport is strictly positive and the
    reads that change favour it on this route; quantised to ~0.15 s at best
    (the ~0.9 Hz poll dates a step only to its gap, biasing short).
  - **PID 0x44 (`mode01_pid_0x44`, every consumer's default route):
    `not_estimated`.** 0x44 and 0x24 share a Mode 01 batch, so a shift only
    changes pairs at batch multiples, and 0.10 s lands between the same-batch
    read and the next (the verifier's 235224: +0.74 % at 0 s, −0.19 % at
    0.10 s, −0.36 % at 0.20 s). Read by read on seven informing drives (3412
    reads) every shift 0.10-0.60 s is *worse* than 0 s (0.10 s: 1041 / 1150,
    p = 0.02; one cycle, 0.25 s: 1507 / 1681, p = 0.002), so no batch-multiple
    delay is justified; reads pair as acquired, and every result says
    `transport_delay_modelled: false` with the reason (`DELAY_NOT_MODELLED`).
    The "every figure is per-pair" claim in the first pass was wrong for the
    delay choice; it was a comparison of means, now reported beside the
    per-read test.

  *Robustness guard.* Where a route has a frozen delay with a bracket,
  `delivery_error` and `pooled_delivery_error` re-run at both bracket ends and
  return `delay_sensitivity` (`at`: n / band-average / pooled per delay;
  `stable`; `reason`; `spread_pct`). Unstable = the band-averaged sign changes
  across the bracket, or the result exists at one delay and not another. The
  digest adds a note, the CLI prints `UNSTABLE: …`, the Analysis page shows an
  alert. The PID 0x44 route has none (`null`: no delay modelled).

  *Enrichment rule* (re-derived on the informing drives at the route's own
  pairing, PID 0x44 / no delay; full justification in the YAML): dwell 0.6 s
  at 0.01 λ — on 682 WOT open-loop reads the residual median is +2.45 /
  +2.54 % where the target moved < 0.0005 / 0.005-0.01 λ over the prior
  0.6 s, +2.32 % at 0.01-0.02, +3.11 / +4.34 % beyond (the lean reading of a
  ramping target the sensor lags). Holdoff 6 s — across 3412 enrichment reads
  the quartiles drift with time since the last cut until 6 s and hold after.

  *Split.* Informing: `_capture-fuel-target-research-2026-09-20T222101`,
  `-20T224503`, `-21T195844`; `_capture-tuning-2026-09-21T223734`,
  `-21T232153`; plus `_capture-tuning-2026-09-23T164007` and
  `_capture-tuning-hybrid-research-2026-09-17T184821`, which the first pass
  evaluated under candidate constants (so not held out). **Clean held out:**
  `_capture-tuning-2026-09-22T170459`, `-22T235224`, `-23T142926`,
  `_capture-tuning-hybrid-research-2026-09-17T191712`, `-17T201000`.

  *Held-out before → after* (`obd:O2_WR_LAMBDA_S1`; n / band-average %
  (pooled %)). MCP `get_delivery_error`, `tuning_report.py delivery-error`
  and the digest were each run and agree exactly (min_load 85, profile
  floor); workbench = Analysis page default (no load floor). Default route =
  PID 0x44, delay not modelled, so there is no bracket to re-run:

  | clean held-out capture | old 1 s grid, MCP/CLI/digest | new, MCP/CLI/digest | old workbench | new workbench |
  |---|---|---|---|---|
  | `_capture-tuning-2026-09-22T170459` | 12 / +1.27 (+1.02) | 22 / +0.17 (−0.02) | 12 / +1.27 | 22 / +0.17 |
  | `_capture-tuning-2026-09-22T235224` | 21 / +1.10 (+1.05) | 21 / +0.74 (+0.51) | 28 / +2.61 | 22 / +0.63 |
  | `_capture-tuning-2026-09-23T142926` | 12 / +0.68 (+0.14) | 24 / −1.14 (−1.14) | 15 / −0.14 | 24 / −1.14 |
  | `…hybrid-research-2026-09-17T191712` | 49 / +0.30 (−0.10) | 67 / −0.46 (−0.31) | 55 / −0.04 | 71 / −0.45 |
  | `…hybrid-research-2026-09-17T201000` | 23 / +0.27 (−0.25) | 46 / −0.66 (−0.68) | 30 / −1.81 | 46 / −0.66 |

  Per band (new, MCP, n / mean / p95 %): 170459 — 2500-3000 20 / −0.05 /
  1.69, 4000-4500 1 / −0.43, 4500-5000 1 / 0.99; 235224 — 3 / 1.99 / 2.21,
  3500-4000 1 / 1.34, 8 / 0.55 / 1.22, 7 / −0.14 / 0.99, 5000-6500 2 / −0.06
  / 1.03; 142926 — 4000-4500 8 / −0.87 / −0.35, 8 / −1.05 / 0.19, 8 / −1.50 /
  −0.43; 191712 — 3000-3500 16 / 1.81 / 2.97, 10 / 0.56 / 3.12, 19 / −0.48 /
  1.52, 9 / −2.12 / −0.92, 13 / −2.09 / −0.27; 201000 — 4000-4500 11 / −0.29 /
  2.90, 23 / −0.73 / 0.29, 12 / −0.95 / 0.43.

  *Why each moved* (MCP call, one step at a time, n / band-average %):

  | capture | old grid | new pairing only | + 6 s holdoff | + 0.6 s / 0.01 λ steady (final) |
  |---|---|---|---|---|
  | 170459 | 12 / +1.27 | 46 / +1.16 | 44 / +1.08 | 22 / +0.17 |
  | 235224 | 21 / +1.10 | 80 / +1.16 | 58 / +1.22 | 21 / +0.74 |
  | 142926 | 12 / +0.68 | 41 / +0.91 | 41 / +0.91 | 24 / −1.14 |
  | 191712 | 49 / +0.30 | 133 / +0.35 | 99 / +0.26 | 67 / −0.46 |
  | 201000 | 23 / +0.27 | 72 / +0.31 | 71 / +0.28 | 46 / −0.66 |

  Acquisition pairing alone keeps the old answers within ±0.25 pt and gives
  2.7-4x the samples (every 3.8 Hz PID 0x44 read, not one per 1 s cell). The
  holdoff moves little. The steady-target rule is the mover (−0.5…−2.1 pt):
  the old grid had no steady-target test, and the reads it now drops are
  those on a target still moving — pooled residual of the dropped reads, from
  the rung counts (n × pooled): +0.9 (170459), +1.7 (235224), +1.8 (142926),
  +0.7 (191712), −0.4 % (201000): lean on four of five, i.e. the sensor
  lagging a target ramping richer. Where so, the old WOT figure was partly
  that lag.

  *At bracket ends* — the RAM-target route (frozen 0.10 s, bracket 0.0 /
  0.5 s), same gates, the three clean held-out captures that carry it
  (n / band-average % at 0.10 | 0.0 | 0.5 s): 170459 4 / −0.03 | 4 / +0.17 |
  4 / +0.44 → `stable: false` (sign flips around zero, spread 0.46 pt);
  235224 6 / −6.51 | 6 / −1.55 | 6 / +4.06 → `stable: false` (spread
  10.6 pt); 142926 5 / −1.11 | 5 / −1.00 | 5 / −1.30 → `stable: true`. So the
  RAM route's answer holds on one of three and is flagged on two — the guard
  doing its job on a weakly supported delay. For contrast only (not a
  declared delay), the PID 0x44 route at 0 | 0.10 | 0.25 s: 170459 +0.17 |
  +0.17 | −4.60; 235224 +0.74 | −0.19 | −0.36; 142926 −1.14 | −1.14 | −1.87;
  191712 −0.46 | −0.67 | −0.74; 201000 −0.66 | −0.66 | −2.54 %, which is why
  that route models none.

  Tests: `tests/test_lambda_delivery.py` (single global rule not
  representable; provenance fields required incl. `transport_delays`; every
  commanded source declares its own delay; PID 0x44 pairs as acquired;
  inform-and-judge refused; frozen delay in bracket with drives; enrichment
  delay pairs the later read on the RAM route; 6 s holdoff; `delay_bracket`;
  sign test counts reads and does not overflow; regime-scoped scan);
  `tests/test_analysis_tuning.py` (the Mode 01 payload says the delay is not
  modelled and has no `delay_sensitivity`; the semantic/RAM route carries
  sensitivity at 0.1 / 0.0 / 0.5 s and flags a result that does not survive;
  `test_a_result_whose_sign_flips_across_the_delay_bracket_is_unstable`);
  known-answer slice (RAM route, unchanged delay) post_fuel_cut 324 → 316,
  target_transient 29 → 31, eligible 554 → 560, enrichment 2 → 8 (an
  informing drive); workbench / digest tests updated; reference digest
  1 → 15; `AnalysisPage.test.tsx` (not-modelled wording, stability alert).
  Ground-truth suite green.

  **Remaining.** (1) Held for deploy by the operator (2026-09-25).
  (2) Stored `digest.json` files do not recompute: nothing re-reads a digest
  once written (A06.5), so the tuning `schema_version` 2 makes a stale
  section identifiable but not refreshed; a refresh belongs to
  `src/control/trip_end.py` / the digest route.

  *(2) done 2026-10-03; (1), the deploy, is still the operator's.*
  - **Scope: only the tuning section.** A06.5 rules out recomputing a whole
    digest late, because its anomaly window would end at "now". The tuning
    section reads nothing but the session (`_tuning_section`), so it alone
    is recomputed and everything else stays as written at trip end.
  - **Stale** (`digest.stale_tuning_version`) means the tuning section's
    `schema_version`, where a missing one counts as 1, is below
    `TUNING_SCHEMA_VERSION`. A recorded section failure (`kind: failed`) is a
    failure, not stale numbers, and is not retried. An ordinary drive has no
    section.
  - **The refresh** (`trip_end.refresh_stale_tuning_off_loop`) runs
    `tuning_section_under_limit` in the digest's own child: the same
    single-use spawn process, address-space ceiling and timeout, now shared
    through `_in_digest_child`. It is single-flight per session. It rewrites
    `digest.json` and the catalog row with the new section plus
    `tuning_refreshed: {from_schema_version, to_schema_version,
    refreshed_at}`, so a refreshed digest never passes for one computed
    whole.
  - **The digest route** (`GET /control/sessions/{id}/digest`) runs it before
    serving. When it cannot, the stored digest is served with
    `tuning_refresh: {status, ...}` naming why, and disk is untouched:
    - `deferred` / `capture_active` with the profile, while a capture
      records;
    - `running`, when another request has it;
    - `failed`, with the child's reason (`child_timed_out`, `child_failed`,
      `child_out_of_memory`).
  - **Real data.** The 09-17 hybrid capture's stored schema-1 digest, copied
    and refreshed through the real spawn child (91 s, 414 MB peak, the
    P02.2-narrowed reads): delivery now reads n = 67, −0.462 %
    band-averaged, engine 2, the same answer `delivery_error` gives on that
    capture directly. Every other key is unchanged, and disk equals what was
    served. A first attempt run from a stdin script showed the failure path:
    spawn could not re-import `<stdin>`, the child died, and the refresh
    reported `child_failed` and left the digest alone.
  - **Tests.** `tests/test_digest_tuning_refresh.py` (9):
    - which sections are stale (five cases);
    - recompute with nothing else moving, and a second read needing no
      child;
    - capture deferral;
    - a timed-out child leaving disk untouched;
    - the route end to end.

- [x] **R05.17 A flash hand-added to the tracked registry after migration is
  never seen.** *Found by R05.15's rework, 2026-09-25; pre-existing.* Once the
  per-vehicle operational registry exists it wins over the tracked
  `data/<profile>/tune_revisions.yaml`, so a revision committed to the tracked
  file afterwards (the established hand-edit workflow) is invisible to
  `active_registry_path` readers until someone copies it over. **Do:** merge the
  two on read (tracked revisions not in the operational copy are appended,
  ordered by `flashed_at`; a conflicting revision with the same name refuses and
  names both), or make the tracked file the single source and have
  `append_flash` write there. Pick one; test both directions. Size **S-M**.

  *Done 2026-09-25 (merge on read).* Merge, not single-source: the tracked file lives in the Pi's
  git checkout, so making `append_flash` write there would dirty it on every
  dashboard flash and orphan the flashes already in the operational copy.
  `tune_revisions.merge_tune_registries` appends tracked revisions the
  operational copy lacks, sorted by `flashed_at`; a name present in both must
  agree on `calibration_id`, `flashed_at`, `k`, `rom_file`, `rom_sha256` or it
  raises `RegistryConflictError` carrying both paths and the differing
  fields (a notes-only edit is not a conflict; the tracked notes win).
  `profiles.active_registry(config)` is the merged history and
  `active_registry_sources` the files it read; `active_registry_path` is
  documented as a file identity only. Swept to `active_registry`:
  `control.calibration` (`calibration_status` turns a conflict into
  `calibration_alert` with the reason instead of failing `/status`;
  `calibration_payload`; `memory_channels_payload`; `append_flash` refuses a
  duplicate of a tracked-only name and writes the merged history through),
  `collection.collectors` (a conflict logs ERROR and runs with the revision
  unknown), `tuning_context`, `behavior_inventory`, `control.logs`,
  `control.workbench_jobs`, `dashboard/routes/calibration` and `rom_maps`
  (`inspect_rom(revisions=…)`), `scripts/{inventory_behavior_inputs,
  refit_behavior_models,evaluate_behavior_models}.py`,
  `tools/import_uprev_log.py`. Tests (`tests/test_tune_registry_merge.py`, 10):
  `test_a_flash_hand_added_to_the_tracked_file_after_migration_is_seen`,
  `test_dashboard_flashes_survive_a_tracked_file_that_never_heard_of_them`,
  `test_one_name_recorded_as_two_flashes_refuses_naming_both_files`,
  `test_a_conflict_is_a_loud_status_alert_not_a_broken_status`,
  `test_append_flash_refuses_a_name_only_the_tracked_file_holds`,
  `test_append_flash_writes_the_merged_history_through` and ordering /
  notes / one-side cases; `test_operational_calibration_scope.py` and
  `test_rom_maps_api.py` still pass. *Completed after verification:*
  `labelled_events.load_labelled_events` takes `tune_registry` (the history
  already read) and `tune_sources` (cited in each flash event's provenance)
  instead of a single path, and `flash_events` cites every source;
  `scripts/evaluate_detectors.py` reads `active_registry(scoped)` +
  `active_registry_sources(scoped)` (an explicit `--tune-revisions` file
  still wins, read as given); `memory_channels_payload` catches
  `RegistryConflictError` like `calibration_payload` (the alert is carried by
  `calibration_status`). Tests:
  `test_labelled_flash_events_come_from_the_merged_history`,
  `test_the_memory_channel_payload_survives_a_registry_conflict`;
  `tests/test_evaluate_detectors_*` pass. `active_registry_path` remains only
  as a file identity for provenance (`behavior_inventory`,
  `evaluate_behavior_models`). **Raise on conflict (by design, naming both
  files):** `TuningContext.from_config` (so the digest's tuning section, MCP
  tuning tools, `tuning_report.py` and the workbench child refuse),
  `control.logs.list_logs`, `workbench_jobs` revision selection,
  `rom_maps.inspect_rom`'s route, the calibration route's
  `record_revision` check, and the offline scripts. Caught and surfaced:
  `calibration_status` (as `calibration_alert`), `calibration_payload`,
  `memory_channels_payload`, the collectors' startup (ERROR log, revision
  unknown).

## Looked at and fine

- `refit.refit_all` and `_validate_held_out`: coherent.
- `decode/behavior_models.py` (1,476 lines): a pydantic schema with clear
  sections. Splitting it would churn 51 importers.
- `workbench.session_frame`: linear. Decompose it only while touching it.
- `events/` detectors: small and uniform.
- Sub-packaging the RE toolkit: cosmetic.
- `analysis/iso_tp` (offline candump) vs `collection/isotp` (live):
  different jobs. At most, share the UDS name tables.
- `channels`, `compare` and `pairing`: already the canonical primitives.
- `lambda_delivery.target_source_agreement` already uses `align_pairs`.
- `clock_align`'s `corrcoef` estimates a lag; it is not a signal comparison.
- No `sorted()` over session directory names in `src/analysis/`.
- `llm_assist`: small and fine.
