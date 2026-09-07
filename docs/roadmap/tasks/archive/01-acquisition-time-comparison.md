# C01 — Compare measurements at acquisition time

Priority: high. Dependencies: coordinate with C02's acquisition/round schema;
the comparison defect can be reproduced and fixed independently.

## Problem and code entry points

`align_pairs()` in [compare.py](../../../../src/analysis/compare.py) calls
`read_channels()` without `acquired_time=True`, independently resamples both
signals with `.mean()`, then calls the resulting bins pairs. The default grid
is one second. Averaging can cancel contradictory dynamics and logical round
stamps can pair observations that were acquired far apart.

The local counterexample used two samples per second for 60 seconds. Signal A
was `1000 + second + 50` then `1000 + second - 50`; signal B reversed those
two values. Every simultaneous residual had magnitude 100, yet the current
comparison returned r²=1, slope=1, near-zero offset, and `stable`.

Relevant consumers include [channels.py](../../../../src/analysis/channels.py),
[tuning.py](../../../../src/analysis/tuning.py),
[tuning_frame.py](../../../../src/analysis/tuning_frame.py), and the analysis tests.

## Implementation checklist

- [x] C01.1 Add the counterexample as a regression test through the public
  `compare_channels()` path using telemetry-shaped rows. Add a second case
  with identical logical round stamps but acquisition times outside tolerance.
  _done 2026-09-06: `tests/test_analysis_compare.py` counterexample via `compare_channels()` (HEAD: r²=1, slope=1, stable → now r²=0.62, slope=−0.79, b−a spread 100); identical round stamps with `acquired_at` 0.8 s apart refuse at 100 ms._
- [x] C01.2 Define a typed pairing policy with an explicit maximum temporal
  separation. Pair real samples in acquisition order, use each sample at most
  once, and resolve equal-distance ties deterministically. Make reference-side
  selection explicit for unequal-rate instruments; avoid a sample-reusing
  ASOF join that inflates `n_pairs`.
  _done: `src/analysis/pairing.py` frozen `PairingPolicy(max_separation, reference a|b|auto, tie_break)`; `match_ordered()` walks the sparser/named side in acquisition order, nearest unused partner, each sample once, deterministic ties (fuzzed against brute force, 400 trials)._
- [x] C01.3 Use acquisition timestamps through the shared reader. Expose
  timestamp provenance: measured arrival, legacy stored timestamp, or logical
  round without known acquisition time. Refuse precision claims where the
  available clock cannot support the requested tolerance.
  _done: `timestamp_basis` column (acquired / stored / round / unknown) from the shared reader; `check_clock_support()` refuses tolerances below the clock floor by name; an absent side is refused as "no observations", never blamed on the clock._
- [x] C01.4 Replace independent mean-bin pairing in `align_pairs()`. Keep any
  requested aggregate visualization in a separately named summary path;
  summary bins must not be reported as raw paired observations.
  _done: `align_pairs()` returns `AlignedPairs` (per-pair indices, timestamps, separation); `binned_summary()` is the separately named picture and is never fed to `regress`._
- [x] C01.5 Define migration for the existing `grid` argument. Sweep
  `align_pairs`, `compare_channels`, `read_aligned`, and result serializers.
  Do not silently reinterpret `grid="1s"` as permission for a one-second
  acquisition mismatch. Require a pairing policy at comparison call sites.
  _done: `policy=` required on `align_pairs`/`compare_channels`/`sweep_session`; `grid=` raises `TypeError` naming the migration; `read_aligned` keeps `grid` documented as the cell-based frame API, not pairing evidence; ground-truth test uses `within("250ms")` justified from measured cadences._
- [x] C01.6 Extend results with timestamp basis, pairing policy, unmatched
  counts for each input, and median/p95/max absolute separation. Report paired
  residual bias and spread in engineering units alongside slope/r². Preserve
  the minimum-pair, signal-motion, and scale-stability checks.
  _done: `ChannelComparison` carries policy, reference, per-side basis, unmatched counts, separation median/p95/max, post-fit residual bias/spread and `difference_bias/spread` (b−a); `as_dict()` added; MIN_PAIRS / motion / stability checks unchanged._
- [x] C01.7 Audit tuning cross-source comparisons and dynamic/lag consumers
  for alternate aggregate pairing. Route comparison conclusions through the
  shared contract; document intentional summary-only consumers.
  _done: `broadcast_discovery.sweep_session` routed through the shared matcher (rediscovers 0x1F9 at 0.1247, noise sweep empty); `cross_reference.pair_by_timestamp` audited (no reuse); tuning/digest `read_aligned` consumers documented as aligned-frame, not comparison (`docs/analysis-pairing.md`)._
- [x] C01.8 Measure algorithm complexity and memory on a realistic drive.
  Use ordered timestamp matching rather than an all-pairs distance matrix.
  _done: ordered matching via sorted arrays + union-find; 180k × 27k (30 min 100 Hz vs 15 Hz) in ~250 ms / 21 MB peak, linear in rows._

## Regression and acceptance checklist

- [x] C01.9 Test the known counterexample, exact simultaneous agreement,
  unequal sample rates, tolerance boundaries, ties, duplicate timestamps,
  missing acquisition metadata, non-overlap, and insufficient motion.
  _done: `tests/test_analysis_pairing.py` (15) + compare tests cover exact, unequal rates, inclusive boundary (250 vs 249 ms), ties, duplicates, missing metadata, non-overlap, insufficient motion._
- [x] C01.10 Verify unmatched values are never forward-filled into evidence,
  samples are not reused, and `n_pairs` equals actual accepted sample pairs.
  _done: no forward-fill, no reuse, `n_pairs == len(pairs)` asserted; verifier fuzz confirmed unique indices and |dt| ≤ tolerance._
- [x] C01.11 Run [test_analysis_ground_truth.py](../../../../tests/test_analysis_ground_truth.py)
  against the committed drive slice and reproduce independent instrument
  agreement. Investigate changed results; do not weaken expected bounds merely
  to accommodate the new algorithm. Keep exact catalog decode pins intact.
  _done: ground truth passes; fast signals improved (ECM_RPM slope err 0.96% → 0.26%, offset −28.7 → +1.7 rpm). Only the three coolant slope bounds were re-measured under the corrected estimator (0x233 0.8→1.9, 0x23D 0.9→1.4, 0x551 unchanged) because OLS on a 7-count quantised range is attenuation-limited and the old binned estimator averaged that away; a stronger `max_residual_bias/spread` (b−a, engineering units) check was added to every case and catches scale, offset, byte-offset and endianness edits (offset edit is caught by it alone). Catalog pins and byte relationships untouched._
- [x] C01.12 Exercise [test_analysis_compare.py](../../../../tests/test_analysis_compare.py),
  [test_analysis_channels.py](../../../../tests/test_analysis_channels.py), and
  relevant tuning tests. Add production Parquet coverage for clock metadata.
  _done: compare/channels/pairing/ground-truth/broadcast suites (81) + `-k tuning` (205) green; production-Parquet provenance round-trip test added._
- [x] C01.13 Acceptance: the counterexample cannot receive a false 1:1
  agreement conclusion; every reported pair is backed by two identifiable
  observations within the declared temporal tolerance.
  _done: acceptance test maps every reported pair to two identifiable `read_channels` rows within tolerance; counterexample cannot yield 1:1 stable at any tolerance._

## Handoff

- [x] C01.14 Record the before/after counterexample and real-fixture results,
  the chosen pairing policy, compatibility behavior, and unresolved timestamp
  limitations. Apply the shared validation gate from the task index.
  _done: `docs/analysis-pairing.md` (before/after, policy, `grid` migration, consumer audit, fixture measurements, perturbation check, complexity, unresolved limitations). Gate green._
