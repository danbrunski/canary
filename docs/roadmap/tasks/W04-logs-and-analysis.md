# W04 — the Logs and Analysis pages

Roadmap tasks W5 and W6. Depends on W01, W02. No truck.

Two pages because they answer different questions. **Logs** shows one capture
as it was recorded. **Analysis** shows derived results and may pool many
sessions — the 2026-09-21 delivery-error result pooled 15. Neither requires the
other.

**Files:** new `panels/tuning/LogsPage.tsx`, `panels/tuning/AnalysisPage.tsx`.
Backend already present: `GET /control/sessions`,
`/control/sessions/{id}/digest`, `/control/sessions/{id}/chart`,
`/control/capture/quality/{id}`, `/analysis/binned`; analysis functions in
`src/analysis/` (`tuning.delivery_error`,
`tuning.delivery_error_from_semantic_frame`, `tuning_frame.build_tuning_frame`,
`channels.read_channels` / `read_aligned`, `compare.compare_channels`).

## Standing constraint

Every endpoint below reads a whole session. Route each through the bounded
single-flight child the digest uses, and **refuse with a named reason while a
capture is active** — "analysis is deferred while `rom-trace` is recording" —
rather than compete with the collector. Offer the laptop path in the message:
pull the session with a tar stream and analyse it there.

## Logs

- [ ] **W04.1 — Capture browser.** Sessions filtered to captures by default
  (directories with a storage prefix), newest first by
  `session_started_at` — never by directory name, since `_capture-…` prefixes
  sort before dates. Columns: profile, start, duration, tune revision and its
  source, quality verdict, `end_reason` (W03.1), row count. A toggle includes
  ordinary sessions.
- [ ] **W04.2 — Selecting a log sets the group's `selectedLogId`** (W02.5), so
  Analysis and ROM Maps open on the same one.
- [ ] **W04.3 — Backend: one log as a wide frame.** New
  `GET /control/sessions/{id}/frame?specs=…&grid=…&regime=…` built on
  `read_aligned`, returning columns on one clock with a per-column unit and the
  count of rows the freshness bound dropped. Cap the row count and say when the
  cap applied; never silently truncate.
- [ ] **W04.4 — Table presentation.** Channels as columns, virtualized rows,
  sortable, with the regime filter: closed loop, enrichment, fuel cut, idle,
  all. Fuel cut comes from the `_SENTINEL` channel where the log has one. CSV
  export of exactly what is shown.
- [ ] **W04.5 — Visual presentation**, same data, toggled: stacked time-series
  strips sharing an x-axis, plus a scatter / heat map of any channel against
  any two axes. Tags from the capture render as vertical markers.
- [ ] **W04.6 — A revision boundary inside a log is shown.** The flash registry
  assigns a revision by session *start*; a log spanning a flash needs a marker
  at the boundary and a warning that figures across it mix two calibrations.

## Analysis

- [ ] **W04.7 — Backend: analysis endpoints**, each returning the result, its
  sample count, its gate report, and — when unavailable — *which kind* of
  unavailable: `GET /analysis/delivery-error` (sessions, measured spec, min
  load), `/analysis/trim-surface` (sessions; STFT × LTFT by rpm × airflow),
  `/analysis/timing` (sessions; final timing by rpm × airflow),
  `/analysis/pull-comparison` (two revisions). Bin on rpm × airflow, never on
  calculated load, which is derived from BFS and moves when fuelling does.
- [ ] **W04.8 — Fix the fuel-cut crash first.** `delivery_error` raises
  `ValueError: equivalence ratio must be positive, got 0.0` when commanded phi
  is zero (fuel cut) instead of gating those samples out; it cost four sessions
  on 2026-09-21. Gate it, with a regression test, before W04.7 exposes it.
- [ ] **W04.9 — Delivery error view.** Error by rpm band with the measured
  noise floor (sd 1.49 %, range ±2.4 % per cell) drawn as a band, so a +2 %
  result is visibly inside it. Sample count on every bar. The measured-AFR
  source is a visible selector, because the ECM's λ and the A/F-voltage path
  disagree by ~3.8 % and the page must not quietly pick a side.
- [ ] **W04.10 — Trim surface view.** rpm × airflow heat map of total
  correction, both banks, cells under a minimum n greyed rather than coloured.
  A per-session split is one click away: the −5 % hump of 2026-09-18 existed
  only on the altitude trip and vanished at home, which a pooled surface hides.
- [ ] **W04.11 — Timing view.** Final timing by cell, with a comparison mode
  between two revisions that highlights a *drop* in a cell that was advanced —
  the ECM's emergency retard, and the only knock proxy this ECM offers.
- [ ] **W04.12 — Every figure states its conditions.** Sessions pooled,
  revision(s), barometric range, gates applied and rows each removed. A number
  without them is how this project got six retractions.

## Empty states

| condition | the page says |
|---|---|
| no log selected (Logs) | the browser, nothing else |
| no sessions chosen (Analysis) | "Choose sessions or a revision" with a one-click "all on the running calibration" |
| gates removed everything | "0 of N samples passed: <gate> removed M" |
| inputs never captured | "This capture's profile does not poll <channel>" — a different message from the row above |
| capture active | "Deferred while `<profile>` is recording", with the laptop path |

## Done when

A log opens in both presentations from a direct route; each analysis view shows
n, gates and conditions; the two kinds of empty are distinguishable on screen.
