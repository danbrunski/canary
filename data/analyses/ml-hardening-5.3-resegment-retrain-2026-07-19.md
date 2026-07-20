# ml-hardening 5.3 — re-segment + refit validation (2026-07-19)

Validates that ml-hardening 5.1 (mode taxonomy fall-through/dead-zone
fixes) and 5.2 (dwell hysteresis) actually change classification on
real recorded sessions, per the task's acceptance: "re-run
`label_sessions.py --rebuild`, retrain, and compare mode-second
distributions before/after."

## Before/after mode-second comparison

Computed directly via `classify_session` (offline, no live daemon —
`get_corpus_mode_summary` requires one) over every real session under
`data/sessions/`, once with the pre-5.1/5.2 `canary.example.yaml`
(commit `8a1790a`) and once with the current (fixed) config.

35 session directories discovered; **21 have no `telemetry-*.parquet`
files** and were skipped by both runs (pre-existing data-quality issue,
unrelated to 5.1/5.2 — see below). The comparison below is over the
remaining 14 sessions, 58763s of classified time in both runs.

| mode | before | after | delta |
|---|---|---|---|
| unknown | 87.67% | 87.06% | -0.61pp |
| city | 3.26% | 1.65% | -1.61pp |
| suburban | 0.00% | 1.28% | +1.28pp |
| idle | 2.21% | 2.75% | +0.54pp |
| koeo | 2.65% | 2.88% | +0.23pp |
| warmup | 1.17% | 1.35% | +0.18pp |
| idle_transient | 0.39% | 0.42% | +0.04pp |
| idle_high | 0.00% | 0.03% | +0.03pp |
| highway | 2.65% | 2.56% | -0.09pp |

**Confirms the fix works as designed:**
- `suburban` (new mode, closes the 50-80km/h dead zone) picked up real
  time (1.28pp) that was previously `unknown`.
- `city`'s share dropped 1.61pp — consistent with 5.1's fall-through
  fix: `city` was over-broadly absorbing warm/high-rpm-idle rows that
  now correctly land in `idle`/`idle_high`/`warmup` instead (those
  three combined gained ~0.75pp, plus `idle_high` itself is new).
- `unknown` decreased overall (-0.61pp), not increased — the 0-5km/h
  creep dead zone caught by review (see `ml-correctness-hardening.md`
  5.1's landed-note, fixed in commit `9636926` before this comparison
  ran) did not reopen a gap.

## Real `label_sessions.py --all --rebuild` run

Ran for real (writes `classification.json` sidecars) against
`data/sessions` with the current `canary.example.yaml`:

```
done classifier_version=0.2.0  ready=5 review=5 quarantine=4 skipped=0 error=21
```

`2026-05-24T190535` (a real drive session) now classifies into
`['city', 'highway', 'idle_high', 'idle_transient', 'suburban',
'unknown']` — both new modes appear on real data, not just the
synthetic fixtures the unit tests use.

## Retrain smoke test

Bounded IF-only retrain (`scripts/retrain.py --skip-sequence
--skip-benchmark --no-ledger`, ~2 min) against the 14 usable sessions.
Trained and saved successfully. Per-mode fit-row counts from the
manifest's `skipped_modes`:

| mode | fit rows | floor |
|---|---|---|
| warmup | 54 | 100 |
| idle | 44 | 100 |
| city | 28 | 100 |
| highway | 28 | 100 |
| suburban | 8 | 100 |
| koeo | 5 | 100 |
| idle_transient | 2 | 100 |

Every mode was skipped by ml-hardening 2.2's `min_samples_per_mode`
floor (100) — this corpus is too thin for a meaningful per-mode IF fit
in any mode, `suburban` included. This is 2.2 working exactly as
designed (no model fit on a handful of rows), not a bug. The retrain
pipeline itself completed without error and correctly picked up the
new mode taxonomy end-to-end (segment → featurize → per-mode split →
floor check → skip), which is what this smoke test set out to prove.

## Findings for morning review (not fixed here — out of scope for 5.1-5.3)

1. **21 of 35 local session directories have no `telemetry-*.parquet`
   files** and are silently skipped by both `classify_session` and
   `label_sessions.py`. These are almost certainly pre-dating the
   CAN-only capture bug already fixed in `record.py` (see memory
   `project_capture_paths_and_archive`) and are exactly what
   `scripts/classify_sessions.py` is meant to sweep into `_archive/` —
   it doesn't look like it's been run recently on this laptop. Running
   it would clean up the corpus and make session counts/percentages in
   future analyses honest.
2. **Even among the 14 usable sessions, `unknown` mode dominates at
   ~87%** of classified time, both before and after this fix. 5.1/5.2
   closed the two dead zones the ml-hardening review specifically
   flagged, but they were never going to move an 87% baseline much —
   whatever else is landing in `unknown` (very likely long stretches of
   parked/KOEO/off-road-adjacent time outside any driving-mode band,
   or genuinely short/thin sessions) is a separate, much larger
   question than 5.1-5.3's scope. Worth a dedicated look if per-mode
   model coverage matters for an upcoming retrain.
3. **This local corpus is too thin for any per-mode IF model** at the
   current `min_samples_per_mode: 100` floor. A real production
   retrain needs either more/longer sessions or a deliberate corpus
   pull from the Pi before it can produce usable per-mode artifacts —
   not a code issue, a data-volume one.
