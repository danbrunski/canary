# C01 — Compare measurements at acquisition time

Priority: high. Dependencies: coordinate with C02's acquisition/round schema;
the comparison defect can be reproduced and fixed independently.

## Problem and code entry points

`align_pairs()` in [compare.py](../../../src/analysis/compare.py) calls
`read_channels()` without `acquired_time=True`, independently resamples both
signals with `.mean()`, then calls the resulting bins pairs. The default grid
is one second. Averaging can cancel contradictory dynamics and logical round
stamps can pair observations that were acquired far apart.

The local counterexample used two samples per second for 60 seconds. Signal A
was `1000 + second + 50` then `1000 + second - 50`; signal B reversed those
two values. Every simultaneous residual had magnitude 100, yet the current
comparison returned r²=1, slope=1, near-zero offset, and `stable`.

Relevant consumers include [channels.py](../../../src/analysis/channels.py),
[tuning.py](../../../src/analysis/tuning.py),
[tuning_frame.py](../../../src/analysis/tuning_frame.py), and the analysis tests.

## Implementation checklist

- [ ] C01.1 Add the counterexample as a regression test through the public
  `compare_channels()` path using telemetry-shaped rows. Add a second case
  with identical logical round stamps but acquisition times outside tolerance.
- [ ] C01.2 Define a typed pairing policy with an explicit maximum temporal
  separation. Pair real samples in acquisition order, use each sample at most
  once, and resolve equal-distance ties deterministically. Make reference-side
  selection explicit for unequal-rate instruments; avoid a sample-reusing
  ASOF join that inflates `n_pairs`.
- [ ] C01.3 Use acquisition timestamps through the shared reader. Expose
  timestamp provenance: measured arrival, legacy stored timestamp, or logical
  round without known acquisition time. Refuse precision claims where the
  available clock cannot support the requested tolerance.
- [ ] C01.4 Replace independent mean-bin pairing in `align_pairs()`. Keep any
  requested aggregate visualization in a separately named summary path;
  summary bins must not be reported as raw paired observations.
- [ ] C01.5 Define migration for the existing `grid` argument. Sweep
  `align_pairs`, `compare_channels`, `read_aligned`, and result serializers.
  Do not silently reinterpret `grid="1s"` as permission for a one-second
  acquisition mismatch. Require a pairing policy at comparison call sites.
- [ ] C01.6 Extend results with timestamp basis, pairing policy, unmatched
  counts for each input, and median/p95/max absolute separation. Report paired
  residual bias and spread in engineering units alongside slope/r². Preserve
  the minimum-pair, signal-motion, and scale-stability checks.
- [ ] C01.7 Audit tuning cross-source comparisons and dynamic/lag consumers
  for alternate aggregate pairing. Route comparison conclusions through the
  shared contract; document intentional summary-only consumers.
- [ ] C01.8 Measure algorithm complexity and memory on a realistic drive.
  Use ordered timestamp matching rather than an all-pairs distance matrix.

## Regression and acceptance checklist

- [ ] C01.9 Test the known counterexample, exact simultaneous agreement,
  unequal sample rates, tolerance boundaries, ties, duplicate timestamps,
  missing acquisition metadata, non-overlap, and insufficient motion.
- [ ] C01.10 Verify unmatched values are never forward-filled into evidence,
  samples are not reused, and `n_pairs` equals actual accepted sample pairs.
- [ ] C01.11 Run [test_analysis_ground_truth.py](../../../tests/test_analysis_ground_truth.py)
  against the committed drive slice and reproduce independent instrument
  agreement. Investigate changed results; do not weaken expected bounds merely
  to accommodate the new algorithm. Keep exact catalog decode pins intact.
- [ ] C01.12 Exercise [test_analysis_compare.py](../../../tests/test_analysis_compare.py),
  [test_analysis_channels.py](../../../tests/test_analysis_channels.py), and
  relevant tuning tests. Add production Parquet coverage for clock metadata.
- [ ] C01.13 Acceptance: the counterexample cannot receive a false 1:1
  agreement conclusion; every reported pair is backed by two identifiable
  observations within the declared temporal tolerance.

## Handoff

- [ ] C01.14 Record the before/after counterexample and real-fixture results,
  the chosen pairing policy, compatibility behavior, and unresolved timestamp
  limitations. Apply the shared validation gate from the task index.
