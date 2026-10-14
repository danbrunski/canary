# R11 — Explicit shared time lookup

Opened 2026-10-14. Shared rules and order: [R10](R10-refactor-README.md).
Size S–M. No prerequisite. Numerical behavior must stay unchanged.

## Problem and target

`fuel_comp_samples._as_of` and `fuel_comp_inputs._as_of` select the last read
at or before an anchor. `engine_dynamics._as_of` and
`fuel_plant_eval._as_of` select the nearest read, including a future one.
`target_phi._as_of` returns distinct prior/next indices. A shared private name
hides three contracts, and each family repeats search and age-mask mechanics.

Create `src/signals/time_lookup.py`, a layer-0 array/index primitive. Give the
operations explicit names, such as `previous_sample_indices`,
`nearest_sample_indices` and `bracketing_sample_indices`. Return indices with
an explicit missing marker; callers retain domain values, timestamp provenance
and output shape. No imports from `analysis` or `preprocessing`.

This is contextual lookup with reuse permitted. It must not replace
`match_ordered` or `compare_channels` for one-to-one signal comparisons.

## Implementation checklist

- [x] **R11.1 Inventory and pin each current contract.**
  - [x] Sweep `_as_of`, `_next_within`, `searchsorted` and related wrappers
    in analyses and streams; list actual callers and distinguish matching
    from contextual lookup before selecting migration targets.
    Inventory in the close-out table below (`rg "_as_of|_next_within|searchsorted"`
    over `src scripts tools tests`; no test, script or tool referenced any of the
    private wrappers, and no monkeypatch targets them).
  - [x] Record direction, inclusive age bound, exact-anchor behavior, nearest
    tie behavior, duplicate timestamps, sortedness and NaN assumptions per caller.
    Close-out table and the `src/signals/time_lookup.py` module docstring.
  - [x] Capture representative existing outputs before extraction, including
    previous/nearest disagreement on the same anchors.
    Scratchpad `r11_baseline.py capture` ran against the unmodified wrappers
    (204 per-anchor result sets: five sizes, duplicate stamps, exact hits,
    midpoint ties, NaN/inf/out-of-range anchors, six age limits incl. 0 and
    inf, each of the six families); `compare` after migration: 204 compared,
    0 diffs (indices-as-values, NaN masks, dtypes, `None`/int types).
- [x] **R11.2 Define the index API.**
  - [x] Require a named operation and age limit; no generic `_as_of` or
    direction-changing default. State units and whether inputs must be sorted.
    `previous_/next_/nearest_/bracketing_sample_indices(times, at, *, max_age_s)`;
    keyword-only, no default (`test_max_age_is_required_and_keyword_only`);
    float seconds, `times` sorted ascending, unchecked.
  - [x] Preserve nearest ties toward the earlier sample for the current
    nearest callers. Preserve the previous operation's last equal-time read.
    `test_nearest_midpoint_tie_takes_the_earlier_read`, `test_duplicate_stamps`,
    and the verbatim-legacy comparison in
    `test_each_operation_reproduces_its_legacy_family_per_anchor`.
  - [x] Preserve `target_phi`'s bracket: an exact-time read is the prior and
    the next is a distinct later read, independently bounded by age.
    `test_exact_anchor_is_previous_and_the_bracket_still_needs_a_later_read`,
    `test_bracket_endpoints_are_bounded_independently`.
  - [x] Keep wrappers responsible for converting scalar/array results and
    choosing NaN versus `None`; do not infer domain validity in the primitive.
    The primitive returns `np.intp` indices with `MISSING = -1`; `target_phi._bracket`
    converts to `int | None`, the four value wrappers use `values_or_nan`.
  - [x] Decide and document validation for malformed time arrays from the
    callers' actual contracts; any stricter refusal is a separate behavior change.
    Decision: none. No caller validated sortedness or finiteness, so the
    primitive reproduces the same arithmetic on unsorted input and treats a
    NaN instant as missing (`test_non_finite_instants_follow_the_legacy_arithmetic`).
- [x] **R11.3 Extract and migrate one semantic family at a time.**
  - [x] Move previous-sample mechanics; migrate fuel-compensation callers.
    `fuel_comp_samples._as_of` -> `_value_at_or_before`, `fuel_comp_inputs._as_of`
    -> `_value_at_or_before`; also `fuel_comp_droop._running_maf` (same search).
  - [x] Move nearest-sample mechanics; migrate plant/environment callers.
    `engine_dynamics._as_of` and `fuel_plant_eval._as_of` -> `_nearest_value`.
  - [x] Move bracketing mechanics; migrate target-phi only after its bracket
    tests show exact-boundary behavior is retained.
    `fuel_comp_samples._next_within` -> `_value_after` (the bracket's next side);
    `target_phi._as_of` -> `_bracket`. Boundary tests above were green first.
  - [x] Rename domain wrappers to communicate semantics and sweep their callers.
    Retain wrappers that perform provenance checks or domain conversion.
    Every call site renamed (no external callers existed). All six wrappers are
    retained as thin domain adapters (value gather / `None` conversion).
  - [x] Remove duplicated search/mask code only after output parity is recorded.
    Parity recorded (R11.1) before the old bodies were deleted; the four
    `searchsorted`+age-mask bodies and the two `_bracket` bodies are gone.
- [x] **R11.4 Verify semantic boundaries.**
  - [x] Add or extend focused tests where coverage is missing: empty inputs,
    exact anchor, age exactly at/beyond limit, nearest tie, duplicate stamps,
    scalar bracket, and independent missing bracket endpoints.
    New `tests/test_time_lookup.py` (13 tests).
  - [x] Include a known fixture where future lookup would be wrong for a
    previous-only question; assert the operations intentionally disagree.
    `test_previous_takes_nothing_from_the_future_and_nearest_does`,
    `test_domain_wrappers_keep_their_distinct_contracts_on_one_fixture`.
  - [x] Verify reused context reads do not become counted comparison pairs.
    `test_reused_context_reads_are_not_counted_pairs` (ten anchors reuse one
    read; `match_ordered` on the same inputs yields one pair) and
    `test_lookup_stays_out_of_scientific_pairing` (`pairing.py`/`compare.py`
    do not import the module).
  - [x] Run `test_fuel_comp_samples.py`, `test_engine_dynamics.py`, the affected
    fuel-plant and target-phi tests, and `test_analysis_ground_truth.py`.
    `pytest -n 4` over test_time_lookup, test_fuel_comp_{samples,inputs,droop},
    test_engine_dynamics, test_fuel_plant{,_mv3}, test_target_phi{,_private_rom},
    test_analysis_ground_truth, test_layering: 262 passed, 12 skipped. `-m slow`
    on the same files: 4 passed, 1 skipped. Downstream importers
    (test_fuel_comp_restore/load/fit, test_plant_validation, test_paired_scenarios,
    test_residual_chain, test_delivery_investigation; not slow): 125 passed.
  - [x] Run layering and the shared gates; `src/signals/` is already in
    `scripts/ml_paths.txt`, so account for its training-tier gate cost.
    `ruff check .`, `ruff format --check .`, `mypy src` (550 files) and
    `test_layering.py` green. The `ml_training` tier is run by
    `scripts/push.sh` on the R11 commit because `src/signals/` changed (no
    training code imports `time_lookup`); the push is its verdict.
- [x] **R11.5 Close out with evidence.**
  - [x] Record migrated callers and any deliberately retained domain wrappers.
    See close-out below.
  - [x] Compare per-anchor indices, values and missing masks on the fixed
    fixture; require equality, rather than matching aggregate means.
    204 result sets, 0 diffs; kept as `test_each_operation_reproduces_its_legacy_family_per_anchor`
    (verbatim legacy bodies vs the primitive, `assert_array_equal`).
  - [x] Record tests, commits and absence of new upward imports.
    Tests recorded above; `test_layering.py` passes with `OWED`/`OWED_PAIRS`
    empty and `time_lookup.py` importing only numpy. Commit: `refactor(signals): explicit shared
    time lookup (R11)` on `main` (one commit for the checklist; its hash is the
    commit that moves this file to `archive/`).

## Acceptance

The three rules are explicit at call sites; each repeated search rule has
one implementation. Existing results, tie rules, refusals and pair counts
are unchanged. No lookup is substituted into scientific comparison pairing.

## Close-out record (2026-10-14)

Primitive: `src/signals/time_lookup.py` (layer 0, numpy only). Tests:
`tests/test_time_lookup.py`.

Migrated (old private name -> wrapper over the primitive):

| Module | Old | New | Operation |
|---|---|---|---|
| `fuel_comp_samples` | `_as_of` | `_value_at_or_before` | previous |
| `fuel_comp_samples` | `_next_within` | `_value_after` | next (bracket upper side) |
| `fuel_comp_inputs` | `_as_of` | `_value_at_or_before` | previous |
| `fuel_comp_droop` | `_running_maf` (inline) | same, calls `previous_sample_indices` | previous |
| `engine_dynamics` | `_as_of` | `_nearest_value` | nearest |
| `fuel_plant_eval` | `_as_of` | `_nearest_value` | nearest |
| `target_phi` | `_as_of` | `_bracket` | bracketing (scalar, `None` for missing) |

Contracts pinned (all unchanged): previous = last read at or before, inclusive
age, last of equal stamps; next = first read strictly after, inclusive gap;
nearest = either side, inclusive, exact hit on duplicates takes the first equal
stamp, midpoint tie takes the earlier; NaN instants are always missing; no
caller validated sortedness.

Retained, deliberately not migrated (different contracts or outside scope):

- Matching, never lookup: `pairing.match_ordered`, `compare_channels`,
  `compare.py` held-fill (inside scientific pairing), `timing_lookup`.
- Different bracket contracts: `fuel_plant_eval._bracketed` and
  `delivery_evidence` fuel-status hold (left-side search, so an exact-time read
  is the *next*, not the prior, and the two reads must agree), `fuel_comp_inputs._bracket`
  (requires both sides or `None`, scalar).
- Previous-style but returning ages/positions rather than context values:
  `fuel_comp_samples._seconds_since`, `engine_dynamics.hold_on_grid`,
  `signals/afr.py` (x2), `lambda_delivery`, `pulls`, `behavior_validation`,
  `tune_revisions`, `derived`, `resampler`; `air_charge_twin._nearest` is a
  scalar nearest with a different (earlier-on-tie via `min`) rule.
  Candidates for a later pass; none are `_as_of` families.
- Window/range searches (`searchsorted` pairs for `[lo, hi]`): not point lookup.

Behaviour: unchanged (204 per-anchor parity sets, 0 diffs).
