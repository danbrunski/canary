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
capture is active** — "analysis is deferred while `tuning` is recording" —
rather than compete with the collector. Offer the laptop path in the message:
pull the session with a tar stream and analyse it there.

## Logs

- [x] **W04.1 — Capture browser.** Sessions filtered to captures by default
  (directories with a storage prefix), newest first by
  `session_started_at` — never by directory name, since `_capture-…` prefixes
  sort before dates. Columns: profile, start, duration, tune revision and its
  source, quality verdict, `end_reason` (W03.1), row count. A toggle includes
  ordinary sessions.
  Evidence: `GET /control/logs` (`src/control/logs.py`, footers only); `tests/test_workbench_analysis.py::test_capture_browser_orders_by_parsed_start_not_directory_name`; `panels/tuning/LogsPage.test.tsx` "with no log selected shows the browser and nothing else", "toggles ordinary sessions into the listing".
- [x] **W04.2 — Selecting a log sets the group's `selectedLogId`** (W02.5), so
  Analysis and ROM Maps open on the same one.
  Evidence: `panels/tuning/LogsPage.test.tsx` "selecting a log sets the group's selectedLogId"; viewer opens from the persisted id ("LogsPage viewer (direct route with a persisted log)").
- [x] **W04.3 — Backend: one log as a wide frame.** New
  `GET /control/sessions/{id}/frame?specs=…&grid=…&regime=…` built on
  `read_aligned`, returning columns on one clock with a per-column unit and the
  count of rows the freshness bound dropped. Cap the row count and say when the
  cap applied; never silently truncate.
  Evidence: `src/analysis/workbench.py::session_frame` on `channels.align_long` (split out of `read_aligned`); `tests/test_workbench_analysis.py::test_frame_counts_cells_the_freshness_bound_dropped`, `::test_frame_cap_is_stated_and_pages_with_offset`, `::test_frame_reports_a_spec_the_log_never_held`, `::test_frame_route_runs_through_the_child`.
- [x] **W04.4 — Table presentation.** Channels as columns, virtualized rows,
  sortable, with the regime filter: closed loop, enrichment, fuel cut, idle,
  all. Fuel cut comes from the `_SENTINEL` channel where the log has one. CSV
  export of exactly what is shown.
  Evidence: regime filter server-side (`regime=`), windowed rows, click-to-sort, CSV of the sorted page; `tests/test_workbench_analysis.py::test_frame_regime_filter_uses_fuel_status_and_zero_phi`, `::test_frame_fuel_cut_comes_from_the_sentinel_channel`, `::test_frame_names_an_unavailable_regime_instead_of_returning_empty`; `panels/tuning/LogsPage.test.tsx` "opens the table presentation with units, freshness and sorting", "sends the regime filter to the frame endpoint", "exports CSV of exactly the given rows".
- [x] **W04.5 — Visual presentation**, same data, toggled: stacked time-series
  strips sharing an x-axis, plus a scatter / heat map of any channel against
  any two axes. Tags from the capture render as vertical markers.
  Evidence: `panels/tuning/LogsPage.test.tsx` "opens the visual presentation with tag markers on shared strips" (strips + scatter + heat map toggle).
- [x] **W04.6 — A revision boundary inside a log is shown.** The flash registry
  assigns a revision by session *start*; a log spanning a flash needs a marker
  at the boundary and a warning that figures across it mix two calibrations.
  Evidence: `tests/test_workbench_analysis.py::test_frame_carries_tags_and_revision_boundary`, `::test_revision_boundary_excludes_flashes_outside_the_span`; `panels/tuning/LogsPage.test.tsx` "warns and marks a revision boundary inside the log"; browser flags "flash inside".

## Analysis

- [x] **W04.7 — Backend: analysis endpoints**, each returning the result, its
  sample count, its gate report, and — when unavailable — *which kind* of
  unavailable: `GET /analysis/delivery-error` (sessions, measured spec, min
  load), `/analysis/trim-surface` (sessions; STFT × LTFT by rpm × airflow),
  `/analysis/timing` (sessions; final timing by rpm × airflow),
  `/analysis/pull-comparison` (two revisions). Bin on rpm × airflow, never on
  calculated load, which is derived from BFS and moves when fuelling does.
  Evidence: `src/dashboard/routes/workbench.py` + `src/control/workbench_jobs.py` (digest's spawn child + `RLIMIT_AS`, single-flight, row preflight, 409 `analysis_deferred` with laptop path); `tests/test_workbench_analysis.py::test_pooled_delivery_error_pools_and_names_not_captured`, `::test_pooled_delivery_error_gates_removed_everything_is_its_own_kind`, `::test_trim_surface_*`, `::test_surfaces_refuse_calculated_load_as_an_axis`, `::test_pull_comparison_flags_a_drop_where_timing_was_advanced`, `::test_every_whole_session_route_defers_while_a_capture_records`, `::test_a_second_different_analysis_is_refused_as_busy`, `::test_run_off_loop_in_a_real_child`. Delivery pooling gates each session on its own clock (`tuning.gate_delivery_samples`) and band-averages once.
- [x] **W04.8 — Fix the fuel-cut crash first.** `delivery_error` raises
  `ValueError: equivalence ratio must be positive, got 0.0` when commanded phi
  is zero (fuel cut) instead of gating those samples out; it cost four sessions
  on 2026-09-21. Gate it, with a regression test, before W04.7 exposes it.
  Evidence: `delivery_error` reads raw PID 0x44 and gates ratio 0.0 as the named `fuel_cut` gate (`AfrCalibration.commanded_afr_series`); `tests/test_analysis_tuning.py::test_fuel_cut_samples_are_gated_not_fatal`, `::test_an_all_fuel_cut_session_is_gates_removed_everything`, `::test_commanded_afr_series_maps_fuel_cut_to_nan`. (The raise itself had been removed at the reader in 89e2f7c; this makes the cut samples a counted gate.)
- [x] **W04.9 — Delivery error view.** Error by rpm band with the measured
  noise floor (sd 1.49 %, range ±2.4 % per cell) drawn as a band, so a +2 %
  result is visibly inside it. Sample count on every bar. The measured-AFR
  source is a visible selector, because the ECM's λ and the A/F-voltage path
  disagree by ~3.8 % and the page must not quietly pick a side.
  Done: bars by rpm band with n on every bar, the floor drawn as a band from the profile's `noise_floor.yaml` with its `conditions` printed, measured-AFR source is a required selector with no default; `panels/tuning/AnalysisPage.test.tsx` "draws the noise floor, n on every bar, and the conditions", "never picks a measured-AFR source for the operator". **Floor decision (operator, 2026-09-21):** use the profile's recorded floor, `data/nissan-xterra-2008/noise_floor.yaml` `delivery_error_pct` — sd 1.25 % over 5 WOT cells — rather than the sd 1.49 % / -2.38..+1.38 % figures above, which live only in `src/analysis/noise_floor.py`'s docstring. That entry records no per-cell range, so the page draws the 1.25 % band and says "per-cell range not recorded"; `ChannelFloor` now has optional `min`/`max`, filled by `record_null_run`, so a re-recorded floor will carry the range.
- [x] **W04.10 — Trim surface view.** rpm × airflow heat map of total
  correction, both banks, cells under a minimum n greyed rather than coloured.
  A per-session split is one click away: the −5 % hump of 2026-09-18 existed
  only on the altitude trip and vanished at home, which a pooled surface hides.
  Evidence: `tests/test_workbench_analysis.py::test_trim_surface_is_closed_loop_by_rpm_airflow_with_per_session_split`, `::test_trim_surface_greys_sparse_cells`; `panels/tuning/AnalysisPage.test.tsx` "greys sparse trim cells and splits per session".
- [x] **W04.11 — Timing view.** Final timing by cell, with a comparison mode
  between two revisions that highlights a *drop* in a cell that was advanced —
  the ECM's emergency retard, and the only knock proxy this ECM offers.
  Evidence: `tests/test_workbench_analysis.py::test_pull_comparison_flags_a_drop_where_timing_was_advanced`; `panels/tuning/AnalysisPage.test.tsx` "outlines a timing drop between revisions".
- [x] **W04.12 — Every figure states its conditions.** Sessions pooled,
  revision(s), barometric range, gates applied and rows each removed. A number
  without them is how this project got six retractions.
  Evidence: `workbench.conditions_block` on every analysis payload (sessions, revisions + flashes inside, baro range, gate report); `panels/tuning/AnalysisPage.test.tsx` conditions assertions; `tests/test_workbench_analysis.py` asserts `conditions` in the pooled delivery result.

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
