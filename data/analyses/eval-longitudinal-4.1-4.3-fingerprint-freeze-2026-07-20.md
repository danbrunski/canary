# eval-longitudinal-and-dtc 4.1-4.3 — real-corpus validation (2026-07-20)

Per `OVERNIGHT-QUEUE.md` Q9's own instruction ("freeze from all
currently-ready classified sessions"): ran the fingerprint pipeline for
real against the local corpus.

## Backfill

`scripts/fingerprint.py backfill --all` against the same 14 usable
sessions from tonight's 5.3/Q6 validation runs (21 of 35 local session
dirs still lack `telemetry-*.parquet`, a known pre-existing corpus issue,
unrelated to this feature): `computed=14 skipped=0 error=21`. Per-session
stat counts ranged 2-20 (thin sessions naturally produce fewer stats —
matches the "skip, don't fabricate" contract verified in code review).

## Freeze

`scripts/fingerprint.py freeze` against the `ready`-classified subset:
`sessions=5 stats=21 -> data/control/nissan-xterra-2008/reference_epoch.json`
(gitignored, profile-scoped — confirmed not tracked by git).

This is a real, if thin, first reference epoch — consistent with the
corpus-thinness findings already logged in
`ml-hardening-5.3-resegment-retrain-2026-07-19.md` (87% unknown mode,
only 14/35 usable sessions). A meaningful re-freeze once a fuller
corpus lands (or a `corpus_boundary` ledger event fires) is expected,
not a defect — the reference epoch is explicitly never auto-updated.
