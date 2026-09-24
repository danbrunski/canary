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

- [ ] **R05.1 Resolve the vehicle's tuning context once.** *Drift bug,
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

- [ ] **R05.2 Route every noise-floor significance through one refusing
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

- [ ] **R05.3 Replace the hand-rolled two-signal comparisons.**
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

- [ ] **R05.4 Declare the anomaly signals as data.**
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

- [ ] **R05.5 Fingerprints must use the real featurization.**
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

- [ ] **R05.6 `session_deviations.compare_sessions` compares aggregates.**
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

- [ ] **R05.7 Collapse the recipe-catalog clones (`refit.default_recipes`,
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

## Structure

- [ ] **R05.8 Decompose `refit.fit_recipe` (426 lines).**
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

- [ ] **R05.9 One byte-slice / scale / plausibility layer in `src/decode/`.**
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

- [ ] **R05.10 Move surface evaluation into decode; one bin rule.**
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

- [ ] **R05.11 Give `GateError` structure.**
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

- [ ] **R05.12 Split the remaining giant functions.**
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

- [ ] **R05.13 `behavior_validation.py` (1,811 lines): move the splits
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

- [ ] **R05.14 Small cleanups (batch them).**
  - `workbench.session_span` re-walks parquet footers that
    `loader.count_session_rows` / `session_time_span_seconds` already walk.
  - `_hash_xy`/`_hash_xyz` and `_now_iso` are each defined twice.
  - `decode/behavior_models.NoiseFloor` collides with
    `analysis.noise_floor.NoiseFloor`; rename the former.
  - R01.3, R01.4 and R01.5 cover the coercion, column and sidecar helpers
    found here.

  Size **S**.

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
