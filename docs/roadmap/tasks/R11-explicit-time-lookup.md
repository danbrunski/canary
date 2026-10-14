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

- [ ] **R11.1 Inventory and pin each current contract.**
  - [ ] Sweep `_as_of`, `_next_within`, `searchsorted` and related wrappers
    in analyses and streams; list actual callers and distinguish matching
    from contextual lookup before selecting migration targets.
  - [ ] Record direction, inclusive age bound, exact-anchor behavior, nearest
    tie behavior, duplicate timestamps, sortedness and NaN assumptions per caller.
  - [ ] Capture representative existing outputs before extraction, including
    previous/nearest disagreement on the same anchors.
- [ ] **R11.2 Define the index API.**
  - [ ] Require a named operation and age limit; no generic `_as_of` or
    direction-changing default. State units and whether inputs must be sorted.
  - [ ] Preserve nearest ties toward the earlier sample for the current
    nearest callers. Preserve the previous operation's last equal-time read.
  - [ ] Preserve `target_phi`'s bracket: an exact-time read is the prior and
    the next is a distinct later read, independently bounded by age.
  - [ ] Keep wrappers responsible for converting scalar/array results and
    choosing NaN versus `None`; do not infer domain validity in the primitive.
  - [ ] Decide and document validation for malformed time arrays from the
    callers' actual contracts; any stricter refusal is a separate behavior change.
- [ ] **R11.3 Extract and migrate one semantic family at a time.**
  - [ ] Move previous-sample mechanics; migrate fuel-compensation callers.
  - [ ] Move nearest-sample mechanics; migrate plant/environment callers.
  - [ ] Move bracketing mechanics; migrate target-phi only after its bracket
    tests show exact-boundary behavior is retained.
  - [ ] Rename domain wrappers to communicate semantics and sweep their callers.
    Retain wrappers that perform provenance checks or domain conversion.
  - [ ] Remove duplicated search/mask code only after output parity is recorded.
- [ ] **R11.4 Verify semantic boundaries.**
  - [ ] Add or extend focused tests where coverage is missing: empty inputs,
    exact anchor, age exactly at/beyond limit, nearest tie, duplicate stamps,
    scalar bracket, and independent missing bracket endpoints.
  - [ ] Include a known fixture where future lookup would be wrong for a
    previous-only question; assert the operations intentionally disagree.
  - [ ] Verify reused context reads do not become counted comparison pairs.
  - [ ] Run `test_fuel_comp_samples.py`, `test_engine_dynamics.py`, the affected
    fuel-plant and target-phi tests, and `test_analysis_ground_truth.py`.
  - [ ] Run layering and the shared gates; `src/signals/` is already in
    `scripts/ml_paths.txt`, so account for its training-tier gate cost.
- [ ] **R11.5 Close out with evidence.**
  - [ ] Record migrated callers and any deliberately retained domain wrappers.
  - [ ] Compare per-anchor indices, values and missing masks on the fixed
    fixture; require equality, rather than matching aggregate means.
  - [ ] Record tests, commits and absence of new upward imports.

## Acceptance

The three rules are explicit at call sites; each repeated search rule has
one implementation. Existing results, tie rules, refusals and pair counts
are unchanged. No lookup is substituted into scientific comparison pairing.
